from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .pycstb4_sections import (
    PackedSection,
    SectionKind,
    SectionRegistry,
    default_section_registry,
    verify_schedule_ir_for_pycstb4,
)


def infer_port_role(name: str, ty: str) -> str:
    nm = str(name).lower()
    if ty == "!pyc.clock" or nm in {"clk", "clock"} or nm.endswith("_clk"):
        return "clock"
    if ty == "!pyc.reset" or nm in {"rst", "reset"} or nm.endswith("_rst") or nm.endswith("_reset"):
        return "reset"
    if nm == "valid" or nm.endswith("_valid"):
        return "valid"
    if nm == "ready" or nm.endswith("_ready"):
        return "ready"
    if nm == "tag" or nm.endswith("_tag"):
        return "tag"
    if nm == "data" or nm.endswith("_data") or nm.endswith("_payload"):
        return "data"
    return "control"


def infer_port_protocol(name: str) -> str | None:
    nm = str(name)
    for suffix in ("_valid", "_ready", "_data", "_payload", "_tag"):
        if nm.endswith(suffix) and len(nm) > len(suffix):
            return nm[: -len(suffix)]
    return None


def _value_from_words(words: Sequence[int]) -> int:
    value = 0
    for idx, word in enumerate(words):
        value |= int(word) << (64 * idx)
    return value


def _expect_events_to_ir(
    *,
    phase: str,
    rows: Iterable[tuple[int, int, str, int, list[int], str]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for cyc, pid, _sn, _w, words, msg in rows:
        events.append(
            {
                "kind": "expect",
                "cycle": int(cyc),
                "phase": phase,
                "port": int(pid),
                "value": f"0x{_value_from_words(words):x}",
                "message": str(msg),
            }
        )
    return events


def _drive_frames_to_ir(
    *,
    drive_ports: Sequence[tuple[int, str, int]],
    drive_frame_rows: Iterable[tuple[int, list[int], list[list[int]]]],
) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for cyc, masks, values in drive_frame_rows:
        items: list[dict[str, Any]] = []
        for slot, (pid, _sn, _w) in enumerate(drive_ports):
            if ((int(masks[slot // 64]) >> (slot % 64)) & 1) == 0:
                continue
            items.append({"port": int(pid), "value": f"0x{_value_from_words(values[slot]):x}"})
        frames.append({"kind": "drive_frame", "cycle": int(cyc), "items": items})
    return frames


def _detect_periodic_drive_patterns(
    *,
    ports: Sequence[Mapping[str, Any]],
    frames: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    port_by_id = {int(port["id"]): port for port in ports}
    samples_by_port: dict[int, list[tuple[int, int]]] = {}
    for frame in frames:
        cyc = int(frame["cycle"])
        for item in frame.get("items", []):
            if not isinstance(item, Mapping):
                continue
            pid = int(item["port"])
            port = port_by_id.get(pid)
            if port is None:
                continue
            if str(port.get("direction")) != "input" or str(port.get("role")) != "ready":
                continue
            value = _hex_to_int(item.get("value"))
            if value not in {0, 1}:
                continue
            samples_by_port.setdefault(pid, []).append((cyc, int(value)))

    patterns: list[dict[str, Any]] = []

    def zero_runs(candidate: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
        runs: list[tuple[int, int]] = []
        run_start: int | None = None
        last_cycle: int | None = None
        for cyc, value in candidate:
            if value == 0 and run_start is None:
                run_start = cyc
            if value != 0 and run_start is not None:
                runs.append((run_start, int(last_cycle) + 1))
                run_start = None
            last_cycle = cyc
        if run_start is not None and last_cycle is not None:
            runs.append((run_start, int(last_cycle) + 1))
        return runs

    for pid, samples in sorted(samples_by_port.items()):
        if len(samples) < 16:
            continue
        samples = sorted(samples)
        detected: tuple[int, int, int, int, int, list[tuple[int, int]]] | None = None
        for drop_prefix in range(0, min(4, len(samples) - 16) + 1):
            candidate = samples[drop_prefix:]
            start = candidate[0][0]
            end = candidate[-1][0] + 1
            if len(candidate) != end - start:
                continue
            runs = zero_runs(candidate)
            if len(runs) < 2:
                continue
            # The first run can be truncated by reset/setup drives. Prefer two
            # full later runs when available, and only fall back to the first
            # two runs for schedules that start exactly at a steady pattern.
            ref_run_idx = 1 if len(runs) >= 3 else 0
            period = runs[ref_run_idx + 1][0] - runs[ref_run_idx][0]
            active_cycles = runs[ref_run_idx][1] - runs[ref_run_idx][0]
            if period <= 1 or active_cycles <= 0 or active_cycles >= period:
                continue
            phase_cycle = runs[ref_run_idx][0] % period
            ok = True
            for cyc, value in candidate:
                expected = 0 if ((cyc - phase_cycle) % period) < active_cycles else 1
                if value != expected:
                    ok = False
                    break
            if ok:
                detected = (period, active_cycles, phase_cycle, start, end, candidate)
                break
            if detected is not None:
                break
        if detected is None:
            continue
        period, active_cycles, phase_cycle, start, end, covered_samples = detected
        port = port_by_id[pid]
        patterns.append(
            {
                "kind": "periodic_drive",
                "name": f"{port.get('name', f'port_{pid}')}_periodic_drive",
                "port": int(pid),
                "start_cycle": int(start),
                "end_cycle": int(end),
                "period": int(period),
                "active_cycles": int(active_cycles),
                "phase_cycle": int(phase_cycle),
                "active_value": "0x0",
                "default_value": "0x1",
                "source": "detected_annotation",
                "covered_samples": len(covered_samples),
                "ignored_setup_samples": len(samples) - len(covered_samples),
            }
        )
    return patterns


def build_runtime_loop_schedule_ir(
    *,
    top_symbol: str,
    schedule_path: Path,
    ports: Iterable[Mapping[str, Any]],
    timeout_cycles: int,
    reset_cycles: int,
    clocking: str,
    schedule_bytes: int,
    max_event_words: int,
    drive_events: Sequence[tuple[int, int, str, int, list[int]]],
    drive_ports: Sequence[tuple[int, str, int]],
    drive_frame_rows: Sequence[tuple[int, list[int], list[list[int]]]],
    pre_expect_events: Sequence[tuple[int, int, str, int, list[int], str]],
    post_expect_events: Sequence[tuple[int, int, str, int, list[int], str]],
    generate_s: float,
) -> dict[str, Any]:
    events = [
        *_expect_events_to_ir(phase="pre", rows=pre_expect_events),
        *_expect_events_to_ir(phase="post", rows=post_expect_events),
    ]
    frames = _drive_frames_to_ir(drive_ports=drive_ports, drive_frame_rows=drive_frame_rows)
    port_list = [dict(port) for port in ports]
    patterns = _detect_periodic_drive_patterns(ports=port_list, frames=frames)
    stats = {
        "event_count": len(events),
        "frame_count": len(frames),
        "pattern_count": len(patterns),
        "actor_count": 0,
        "port_count": len(port_list),
        "max_cycle": int(timeout_cycles),
        "schedule_bytes": int(schedule_bytes),
        "json_bytes": 0,
        "generate_s": float(generate_s),
        "legacy_pycstb3": {
            "drive_events": len(drive_events),
            "drive_frames": len(drive_frame_rows),
            "pre_expect_events": len(pre_expect_events),
            "post_expect_events": len(post_expect_events),
            "max_event_words": int(max_event_words),
        },
    }
    return {
        "schema": "pycircuit.schedule_ir",
        "version": {"major": 1, "minor": 0, "patch": 0},
        "metadata": {
            "case_name": f"tb_{top_symbol}",
            "generator": "pycircuit.runtime_loop",
            "generator_version": "prototype-pycstb3",
            "source": str(schedule_path),
            "notes": "Generated alongside legacy PYCSTB3 runtime-loop schedule.",
        },
        "timebase": {
            "unit": "cycle",
            "max_cycle": int(timeout_cycles),
            "reset_cycles": int(reset_cycles),
            "clocking": str(clocking),
        },
        "ports": sorted(port_list, key=lambda x: int(x["id"])),
        "events": sorted(events, key=lambda x: (int(x["cycle"]), str(x.get("phase", "post")), int(x["port"]))),
        "frames": frames,
        "patterns": patterns,
        "actors": [],
        "scoreboards": [],
        "stats": stats,
    }


def render_schedule_ir_json(schedule_ir: dict[str, Any]) -> str:
    for _ in range(4):
        text = json.dumps(schedule_ir, sort_keys=True, indent=2) + "\n"
        json_bytes = len(text.encode("utf-8"))
        if int(schedule_ir["stats"]["json_bytes"]) == json_bytes:
            return text
        schedule_ir["stats"]["json_bytes"] = json_bytes
    return json.dumps(schedule_ir, sort_keys=True, indent=2) + "\n"


_PYCSTB4_MAGIC = b"PYCSTB4\n"
_PYCSTB4_HEADER_FMT = "<8sBHHHHQIIQII"
_PYCSTB4_DIR_FMT = "<HHIQQQ"
_PYCSTB4_PORT_FMT = "<IIBBHIII"
_PYCSTB4_EVENT_PREFIX_FMT = "<QBBHIII"
_PYCSTB4_FRAME_PREFIX_FMT = "<QBBHI"
_PYCSTB4_FRAME_ITEM_PREFIX_FMT = "<IIII"
_PYCSTB4_PATTERN_PREFIX_FMT = "<HHIQQQQQII"
_PYCSTB4_ACTOR_BUNDLE_HEADER_FMT = "<4sHHIIII"
_PYCSTB4_EXTERNAL_SOURCE_FMT = "<IIIQQQII"
_PYCSTB4_ACTOR_RECORD_FMT = "<HHIIIIIQQIIHHQQQQQQQ"
_PYCSTB4_SCOREBOARD_RECORD_FMT = "<HHIIII"
_PYCSTB4_NONE = 0xFFFFFFFF
_PYCSTB4_ACTOR_PAYLOAD_BLOB_SECTION = int(SectionKind.ACTOR_PAYLOAD_BLOB)
_PYCSTB4_ACTOR_PAYLOAD_TABLE_SECTION = int(SectionKind.ACTOR_PAYLOAD_TABLE)
_PYCSTB4_INSTRUCTION_STREAM_SECTION = int(SectionKind.INSTRUCTION_STREAM)
_PYCSTB4_SEEDED_WORKLOAD_GENERATOR_SECTION = int(SectionKind.SEEDED_WORKLOAD_GENERATOR)
_PYCSTB4_EXTERNAL_STREAM_SOURCE_SECTION = int(SectionKind.EXTERNAL_STREAM_SOURCE)
_PYCSTB4_SCOREBOARD_POLICY_SECTION = int(SectionKind.SCOREBOARD_POLICY)

_DIRECTION_ID = {"input": 0, "output": 1, "inout": 2}
_ROLE_ID = {"unknown": 0, "data": 1, "valid": 2, "ready": 3, "tag": 4, "control": 5, "clock": 6, "reset": 7}
_EVENT_KIND_ID = {"drive": 0, "expect": 1, "sample": 2, "marker": 3}
_FRAME_KIND_ID = {"drive_frame": 0, "expect_frame": 1}
_PHASE_ID = {"pre": 0, "post": 1}
_ACTOR_KIND_ID = {"ready_valid_source": 0, "ready_valid_sink": 1}
_ACTOR_POLICY_ID = {"hold_valid": 0, "on_handshake": 1}
_SCOREBOARD_KIND_ID = {"ordered": 0, "tagged": 1, "masked": 2}
_EXTERNAL_SOURCE_KIND_ID = {"external_pactr0": 0, "external_pycstb4_actor": 1}
_READY_PATTERN_KIND_ID = {"none": 0, "constant": 1, "periodic_drive": 2}


class _StringTable:
    def __init__(self) -> None:
        self.ids: dict[str, int] = {}
        self.items: list[str] = []

    def add(self, value: str | None) -> int:
        if value is None or value == "":
            return _PYCSTB4_NONE
        text = str(value)
        sid = self.ids.get(text)
        if sid is not None:
            return sid
        sid = len(self.items)
        self.ids[text] = sid
        self.items.append(text)
        return sid

    def to_bytes(self) -> bytes:
        blob = bytearray()
        blob.extend(len(self.items).to_bytes(4, "little", signed=False))
        for item in self.items:
            raw = item.encode("utf-8")
            blob.extend(len(raw).to_bytes(4, "little", signed=False))
            blob.extend(raw)
        return bytes(blob)


def _collect_pycstb4_strings(schedule_ir: Mapping[str, Any]) -> _StringTable:
    table = _StringTable()
    for port in schedule_ir.get("ports", []):
        if isinstance(port, Mapping):
            table.add(str(port.get("name", "")))
            table.add(None if port.get("protocol") is None else str(port.get("protocol")))
    for event in schedule_ir.get("events", []):
        if isinstance(event, Mapping):
            table.add(None if event.get("message") is None else str(event.get("message")))
    for frame in schedule_ir.get("frames", []):
        if not isinstance(frame, Mapping):
            continue
        for item in frame.get("items", []):
            if isinstance(item, Mapping):
                table.add(None if item.get("message") is None else str(item.get("message")))
    for actor in schedule_ir.get("actors", []):
        if not isinstance(actor, Mapping):
            continue
        table.add(None if actor.get("name") is None else str(actor.get("name")))
        source = actor.get("transaction_source", {})
        if isinstance(source, Mapping):
            table.add(None if source.get("path") is None else str(source.get("path")))
    for scoreboard in schedule_ir.get("scoreboards", []):
        if not isinstance(scoreboard, Mapping):
            continue
        table.add(None if scoreboard.get("name") is None else str(scoreboard.get("name")))
        source = scoreboard.get("expected_source", {})
        if isinstance(source, Mapping):
            table.add(None if source.get("path") is None else str(source.get("path")))
    for generator in schedule_ir.get("workload_generators", []):
        if not isinstance(generator, Mapping):
            continue
        table.add(None if generator.get("name") is None else str(generator.get("name")))
        table.add(None if generator.get("generator_id") is None else str(generator.get("generator_id")))
        table.add(None if generator.get("profile") is None else str(generator.get("profile")))
        constraints = generator.get("constraints")
        if constraints is not None:
            table.add(json.dumps(constraints, sort_keys=True, separators=(",", ":")))
    for stream in schedule_ir.get("instruction_streams", []):
        if not isinstance(stream, Mapping):
            continue
        table.add(None if stream.get("name") is None else str(stream.get("name")))
        table.add(None if stream.get("isa") is None else str(stream.get("isa")))
        table.add(None if stream.get("encoding") is None else str(stream.get("encoding")))
        table.add(None if stream.get("source") is None else str(stream.get("source")))
    for source in schedule_ir.get("external_stream_sources", []):
        if not isinstance(source, Mapping):
            continue
        table.add(None if source.get("name") is None else str(source.get("name")))
        table.add(None if source.get("path") is None else str(source.get("path")))
        table.add(None if source.get("format") is None else str(source.get("format")))
        table.add(None if source.get("hash") is None else str(source.get("hash")))
    for policy in schedule_ir.get("scoreboard_policies", []):
        if not isinstance(policy, Mapping):
            continue
        table.add(None if policy.get("name") is None else str(policy.get("name")))
        table.add(None if policy.get("kind") is None else str(policy.get("kind")))
        table.add(None if policy.get("target") is None else str(policy.get("target")))
        table.add(None if policy.get("reference") is None else str(policy.get("reference")))
        table.add(None if policy.get("signature") is None else str(policy.get("signature")))
    return table


def _hex_to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return int(value)
    text = str(value)
    return int(text, 16) if text.startswith(("0x", "0X")) else int(text)


def _value_words(value: int | None, max_words: int) -> bytes:
    vv = 0 if value is None else int(value)
    blob = bytearray()
    for idx in range(max_words):
        blob.extend(((vv >> (64 * idx)) & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little", signed=False))
    return bytes(blob)


def _nwords(value: int | None) -> int:
    if value is None:
        return 0
    return max(1, (int(value).bit_length() + 63) // 64)


def _pycstb4_max_words(schedule_ir: Mapping[str, Any]) -> int:
    max_words = 1
    for port in schedule_ir.get("ports", []):
        if isinstance(port, Mapping):
            max_words = max(max_words, int(port.get("word_count", 1)))
    return max_words


def _pack_pycstb4_ports(schedule_ir: Mapping[str, Any], strings: _StringTable) -> bytes:
    blob = bytearray()
    for port in schedule_ir.get("ports", []):
        if not isinstance(port, Mapping):
            continue
        blob.extend(
            struct.pack(
                _PYCSTB4_PORT_FMT,
                int(port["id"]),
                strings.add(str(port["name"])),
                _DIRECTION_ID.get(str(port.get("direction", "input")), 0),
                _ROLE_ID.get(str(port.get("role", "unknown")), 0),
                0,
                int(port["bit_width"]),
                int(port["word_count"]),
                strings.add(None if port.get("protocol") is None else str(port.get("protocol"))),
            )
        )
    return bytes(blob)


def _pack_pycstb4_events(schedule_ir: Mapping[str, Any], strings: _StringTable, max_words: int) -> bytes:
    blob = bytearray()
    for event in schedule_ir.get("events", []):
        if not isinstance(event, Mapping):
            continue
        value = _hex_to_int(event.get("value"))
        mask = _hex_to_int(event.get("mask"))
        port = _PYCSTB4_NONE if event.get("port") is None else int(event["port"])
        blob.extend(
            struct.pack(
                _PYCSTB4_EVENT_PREFIX_FMT,
                int(event["cycle"]),
                _EVENT_KIND_ID.get(str(event.get("kind", "marker")), 3),
                _PHASE_ID.get(str(event.get("phase", "post")), 1),
                0,
                port,
                _nwords(value),
                strings.add(None if event.get("message") is None else str(event.get("message"))),
            )
        )
        blob.extend(_value_words(value, max_words))
        blob.extend(_value_words(mask, max_words))
    return bytes(blob)


def _pack_pycstb4_frames(schedule_ir: Mapping[str, Any], strings: _StringTable, max_words: int) -> bytes:
    blob = bytearray()
    covered: set[tuple[int, int]] = set()
    for pattern in schedule_ir.get("patterns", []):
        if not isinstance(pattern, Mapping) or str(pattern.get("kind")) != "periodic_drive":
            continue
        port = int(pattern["port"])
        for cycle in range(int(pattern["start_cycle"]), int(pattern["end_cycle"])):
            covered.add((cycle, port))
    for frame in schedule_ir.get("frames", []):
        if not isinstance(frame, Mapping):
            continue
        kind = str(frame.get("kind", "drive_frame"))
        cycle = int(frame["cycle"])
        items = [
            item
            for item in frame.get("items", [])
            if isinstance(item, Mapping) and not (kind == "drive_frame" and (cycle, int(item["port"])) in covered)
        ]
        if not items:
            continue
        blob.extend(
            struct.pack(
                _PYCSTB4_FRAME_PREFIX_FMT,
                cycle,
                _FRAME_KIND_ID.get(kind, 0),
                0 if kind == "drive_frame" else 1,
                0,
                len(items),
            )
        )
        for item in items:
            value = _hex_to_int(item.get("value"))
            mask = _hex_to_int(item.get("mask"))
            blob.extend(
                struct.pack(
                    _PYCSTB4_FRAME_ITEM_PREFIX_FMT,
                    int(item["port"]),
                    _nwords(value),
                    strings.add(None if item.get("message") is None else str(item.get("message"))),
                    0,
                )
            )
            blob.extend(_value_words(value, max_words))
            blob.extend(_value_words(mask, max_words))
    return bytes(blob)


def _pack_pycstb4_patterns(schedule_ir: Mapping[str, Any], max_words: int) -> bytes:
    blob = bytearray()
    for pattern in schedule_ir.get("patterns", []):
        if not isinstance(pattern, Mapping) or str(pattern.get("kind")) != "periodic_drive":
            continue
        active = _hex_to_int(pattern.get("active_value"))
        default = _hex_to_int(pattern.get("default_value"))
        blob.extend(
            struct.pack(
                _PYCSTB4_PATTERN_PREFIX_FMT,
                1,
                0,
                int(pattern["port"]),
                int(pattern["start_cycle"]),
                int(pattern["end_cycle"]),
                int(pattern["period"]),
                int(pattern["active_cycles"]),
                int(pattern.get("phase_cycle", int(pattern["start_cycle"]))),
                _nwords(active),
                _nwords(default),
            )
        )
        blob.extend(_value_words(active, max_words))
        blob.extend(_value_words(default, max_words))
    return bytes(blob)


def _external_source_key(source: Mapping[str, Any]) -> tuple[str, str, int, int, int, tuple[int, ...]]:
    payload_ports = source.get("payload_ports", [])
    payload_key = tuple(int(port) for port in payload_ports) if isinstance(payload_ports, (list, tuple)) else tuple()
    return (
        str(source.get("kind", "")),
        str(source.get("path", "")),
        int(source.get("count", 0)),
        int(source.get("byte_offset", 0)),
        int(source.get("byte_size", 0)),
        payload_key,
    )


def _pack_pycstb4_actor_bundle(schedule_ir: Mapping[str, Any], strings: _StringTable) -> tuple[bytes, int]:
    actors = [actor for actor in schedule_ir.get("actors", []) if isinstance(actor, Mapping)]
    scoreboards = [scoreboard for scoreboard in schedule_ir.get("scoreboards", []) if isinstance(scoreboard, Mapping)]
    if not actors and not scoreboards:
        return b"", 0

    port_refs: list[int] = []

    def add_port_refs(values: Any) -> tuple[int, int]:
        first = len(port_refs)
        if isinstance(values, (list, tuple)):
            for value in values:
                port_refs.append(int(value))
        return first, len(port_refs) - first

    external_sources: list[Mapping[str, Any]] = []
    external_source_indices: dict[tuple[str, str, int, int, int, tuple[int, ...]], int] = {}

    def add_external_source(source: Any) -> int:
        if not isinstance(source, Mapping):
            return _PYCSTB4_NONE
        key = _external_source_key(source)
        if key in external_source_indices:
            return external_source_indices[key]
        index = len(external_sources)
        external_source_indices[key] = index
        external_sources.append(source)
        return index

    scoreboard_index_by_name = {str(scoreboard.get("name", "")): idx for idx, scoreboard in enumerate(scoreboards)}
    scoreboard_records = bytearray()
    for scoreboard in scoreboards:
        first_payload, payload_count = add_port_refs(scoreboard.get("payload_ports", []))
        expected_ref = add_external_source(scoreboard.get("expected_source"))
        scoreboard_records.extend(
            struct.pack(
                _PYCSTB4_SCOREBOARD_RECORD_FMT,
                _SCOREBOARD_KIND_ID.get(str(scoreboard.get("kind", "ordered")), 0),
                0,
                strings.add(str(scoreboard.get("name", ""))),
                first_payload,
                payload_count,
                expected_ref,
            )
        )

    actor_records = bytearray()
    for actor in actors:
        first_payload, payload_count = add_port_refs(actor.get("payload_ports", []))
        source_ref = add_external_source(actor.get("transaction_source"))
        scoreboard_ref = scoreboard_index_by_name.get(str(actor.get("scoreboard", "")), _PYCSTB4_NONE)
        policy = str(actor.get("policy", actor.get("sample_policy", "hold_valid")))
        ready_pattern = actor.get("ready_pattern", {})
        if not isinstance(ready_pattern, Mapping):
            ready_pattern = {}
        ready_kind = str(ready_pattern.get("kind", "none"))
        ready_value = ready_pattern.get("value", "0x1")
        actor_records.extend(
            struct.pack(
                _PYCSTB4_ACTOR_RECORD_FMT,
                _ACTOR_KIND_ID.get(str(actor.get("kind", "ready_valid_source")), 0),
                _ACTOR_POLICY_ID.get(policy, 0),
                strings.add(str(actor.get("name", ""))),
                int(actor.get("valid_port", _PYCSTB4_NONE)),
                int(actor.get("ready_port", _PYCSTB4_NONE)),
                first_payload,
                payload_count,
                int(actor.get("start_cycle", 0)),
                int(actor.get("end_cycle", 0)),
                source_ref,
                scoreboard_ref,
                _READY_PATTERN_KIND_ID.get(ready_kind, 0),
                0,
                int(ready_pattern.get("period", 1)),
                int(ready_pattern.get("active_cycles", 0)),
                int(ready_pattern.get("phase_cycle", 0)),
                int(ready_pattern.get("start_cycle", actor.get("start_cycle", 0))),
                int(ready_pattern.get("end_cycle", actor.get("end_cycle", 0))),
                int(_hex_to_int(ready_pattern.get("active_value", ready_value)) or 1),
                int(_hex_to_int(ready_pattern.get("default_value", ready_value)) or 1),
            )
        )

    external_records = bytearray()
    for source in external_sources:
        first_payload, payload_count = add_port_refs(source.get("payload_ports", []))
        external_records.extend(
            struct.pack(
                _PYCSTB4_EXTERNAL_SOURCE_FMT,
                _EXTERNAL_SOURCE_KIND_ID.get(str(source.get("kind", "")), 0),
                strings.add(str(source.get("path", ""))),
                int(source.get("count", 0)),
                int(source.get("byte_offset", 0)),
                int(source.get("byte_size", 0)),
                0,
                first_payload,
                payload_count,
            )
        )

    blob = bytearray()
    blob.extend(
        struct.pack(
            _PYCSTB4_ACTOR_BUNDLE_HEADER_FMT,
            b"ACTR",
            0,
            1,
            len(external_sources),
            len(actors),
            len(scoreboards),
            len(port_refs),
        )
    )
    for port_id in port_refs:
        blob.extend(int(port_id).to_bytes(4, "little", signed=False))
    blob.extend(external_records)
    blob.extend(actor_records)
    blob.extend(scoreboard_records)
    return bytes(blob), len(actors)


def _pack_pycstb4_actor_payload_blob(schedule_ir: Mapping[str, Any]) -> bytes:
    metadata = schedule_ir.get("metadata", {})
    if not isinstance(metadata, Mapping):
        return b""
    path = metadata.get("actor_payload_blob")
    if path is None or str(path) == "":
        return b""
    return Path(str(path)).read_bytes()


def _pack_pycstb4_actor_payload_table(schedule_ir: Mapping[str, Any]) -> bytes:
    metadata = schedule_ir.get("metadata", {})
    if not isinstance(metadata, Mapping):
        return b""
    path = metadata.get("actor_payload_blob")
    if path is None or str(path) == "":
        return b""
    data = Path(str(path)).read_bytes()
    if len(data) < 28 or data[:8] != b"PACTR0\0\0":
        return b""
    version = int.from_bytes(data[8:12], "little", signed=False)
    tx_count = int.from_bytes(data[12:16], "little", signed=False)
    source_ports = int.from_bytes(data[16:20], "little", signed=False)
    expected_ports = int.from_bytes(data[20:24], "little", signed=False)
    if version != 1 or source_ports == 0 or expected_ports == 0:
        return b""
    source_payload_ports: list[int] = []
    expected_payload_ports: list[int] = []
    for actor in schedule_ir.get("actors", []):
        if isinstance(actor, Mapping) and str(actor.get("kind")) == "ready_valid_source":
            source_payload_ports = [int(port) for port in actor.get("payload_ports", [])]
            break
    for scoreboard in schedule_ir.get("scoreboards", []):
        if isinstance(scoreboard, Mapping) and str(scoreboard.get("kind")) == "ordered":
            expected_payload_ports = [int(port) for port in scoreboard.get("payload_ports", [])]
            break
    if len(source_payload_ports) != source_ports or len(expected_payload_ports) != expected_ports:
        return b""

    values_offset = 28
    words_per_tx = source_ports + expected_ports
    expected_size = values_offset + tx_count * words_per_tx * 8
    if len(data) < expected_size:
        return b""
    source_values: list[list[int]] = []
    expected_values: list[list[int]] = []
    for tx in range(tx_count):
        off = values_offset + tx * words_per_tx * 8
        source_row: list[int] = []
        expected_row: list[int] = []
        for _ in range(source_ports):
            source_row.append(int.from_bytes(data[off : off + 8], "little", signed=False))
            off += 8
        for _ in range(expected_ports):
            expected_row.append(int.from_bytes(data[off : off + 8], "little", signed=False))
            off += 8
        source_values.append(source_row)
        expected_values.append(expected_row)

    blob = bytearray()
    blob.extend(b"ATXN")
    blob.extend((0).to_bytes(2, "little", signed=False))
    blob.extend((1).to_bytes(2, "little", signed=False))
    blob.extend((2).to_bytes(4, "little", signed=False))
    for payload_ports, rows in ((source_payload_ports, source_values), (expected_payload_ports, expected_values)):
        blob.extend(len(payload_ports).to_bytes(4, "little", signed=False))
        blob.extend(tx_count.to_bytes(4, "little", signed=False))
        blob.extend((1).to_bytes(4, "little", signed=False))
        blob.extend((0).to_bytes(4, "little", signed=False))
        for port_id in payload_ports:
            blob.extend(int(port_id).to_bytes(4, "little", signed=False))
        for row in rows:
            for value in row:
                blob.extend(int(value).to_bytes(8, "little", signed=False))
    return bytes(blob)


def _pack_pycstb4_seeded_workload_generators(schedule_ir: Mapping[str, Any], strings: _StringTable) -> tuple[bytes, int]:
    generators = [item for item in schedule_ir.get("workload_generators", []) if isinstance(item, Mapping)]
    if not generators:
        return b"", 0
    port_refs: list[int] = []
    records = bytearray()
    for generator in generators:
        output_ports = [int(port) for port in generator.get("output_ports", [])]
        first_output_ref = len(port_refs)
        port_refs.extend(output_ports)
        constraints = generator.get("constraints")
        constraints_sid = _PYCSTB4_NONE
        if constraints is not None:
            constraints_sid = strings.add(json.dumps(constraints, sort_keys=True, separators=(",", ":")))
        flags = int(generator.get("flags", 0))
        if bool(generator.get("deterministic", True)):
            flags |= 1
        records.extend(
            struct.pack(
                "<IIIIQQQQII",
                strings.add(None if generator.get("name") is None else str(generator.get("name"))),
                strings.add(None if generator.get("generator_id") is None else str(generator.get("generator_id"))),
                strings.add(None if generator.get("profile") is None else str(generator.get("profile"))),
                constraints_sid,
                int(generator.get("seed", 0)) & 0xFFFFFFFFFFFFFFFF,
                int(generator.get("count", 0)),
                int(generator.get("start_index", 0)),
                flags,
                first_output_ref,
                len(output_ports),
            )
        )
    blob = bytearray()
    blob.extend(struct.pack("<4sHHII", b"WGEN", 0, 1, len(generators), len(port_refs)))
    for port_id in port_refs:
        blob.extend(int(port_id).to_bytes(4, "little", signed=False))
    blob.extend(records)
    return bytes(blob), len(generators)


def _pack_pycstb4_instruction_streams(schedule_ir: Mapping[str, Any], strings: _StringTable) -> tuple[bytes, int]:
    streams = [item for item in schedule_ir.get("instruction_streams", []) if isinstance(item, Mapping)]
    if not streams:
        return b"", 0
    records = bytearray()
    payload = bytearray()
    record_size = struct.calcsize("<IIIIIIQQQ")
    payload_base = struct.calcsize("<4sHHII") + len(streams) * record_size
    payload_cursor = 0
    for stream in streams:
        instructions = [int(value) for value in stream.get("instructions", [])]
        inst_width = int(stream.get("instruction_width", 32))
        word_bytes = max(1, (inst_width + 7) // 8)
        if word_bytes not in {1, 2, 4, 8}:
            word_bytes = 8
        stream_payload = bytearray()
        for instruction in instructions:
            stream_payload.extend(int(instruction).to_bytes(word_bytes, "little", signed=False))
        flags = int(stream.get("flags", 0))
        if instructions:
            flags |= 1
        payload_offset = payload_base + payload_cursor if stream_payload else 0
        payload_size = len(stream_payload)
        payload.extend(stream_payload)
        payload_cursor += payload_size
        records.extend(
            struct.pack(
                "<IIIIIIQQQ",
                strings.add(None if stream.get("name") is None else str(stream.get("name"))),
                strings.add(None if stream.get("isa") is None else str(stream.get("isa"))),
                strings.add(None if stream.get("encoding") is None else str(stream.get("encoding"))),
                strings.add(None if stream.get("source") is None else str(stream.get("source"))),
                inst_width,
                flags,
                int(stream.get("count", len(instructions))),
                payload_offset,
                payload_size,
            )
        )
    blob = bytearray()
    blob.extend(struct.pack("<4sHHII", b"INST", 0, 1, len(streams), 0))
    blob.extend(records)
    blob.extend(payload)
    return bytes(blob), len(streams)


def _pack_pycstb4_external_stream_sources(schedule_ir: Mapping[str, Any], strings: _StringTable) -> tuple[bytes, int]:
    sources = [item for item in schedule_ir.get("external_stream_sources", []) if isinstance(item, Mapping)]
    if not sources:
        return b"", 0
    records = bytearray()
    for source in sources:
        records.extend(
            struct.pack(
                "<IIIIQQQQ",
                strings.add(None if source.get("name") is None else str(source.get("name"))),
                strings.add(None if source.get("path") is None else str(source.get("path"))),
                strings.add(None if source.get("format") is None else str(source.get("format"))),
                strings.add(None if source.get("hash") is None else str(source.get("hash"))),
                int(source.get("offset", 0)),
                int(source.get("byte_size", 0)),
                int(source.get("chunk_size", 0)),
                int(source.get("flags", 0)),
            )
        )
    blob = bytearray()
    blob.extend(struct.pack("<4sHHII", b"XSTR", 0, 1, len(sources), 0))
    blob.extend(records)
    return bytes(blob), len(sources)


def _pack_pycstb4_scoreboard_policies(schedule_ir: Mapping[str, Any], strings: _StringTable) -> tuple[bytes, int]:
    policies = [item for item in schedule_ir.get("scoreboard_policies", []) if isinstance(item, Mapping)]
    if not policies:
        return b"", 0
    records = bytearray()
    for policy in policies:
        records.extend(
            struct.pack(
                "<IIIIIQQQ",
                strings.add(None if policy.get("name") is None else str(policy.get("name"))),
                strings.add(None if policy.get("kind") is None else str(policy.get("kind"))),
                strings.add(None if policy.get("target") is None else str(policy.get("target"))),
                strings.add(None if policy.get("reference") is None else str(policy.get("reference"))),
                strings.add(None if policy.get("signature") is None else str(policy.get("signature"))),
                int(policy.get("sample_period", 0)),
                int(policy.get("max_mismatches", 0)),
                int(policy.get("flags", 0)),
            )
        )
    blob = bytearray()
    blob.extend(struct.pack("<4sHHII", b"SCBP", 0, 1, len(policies), 0))
    blob.extend(records)
    return bytes(blob), len(policies)


def _pycstb4_frame_count_after_pattern_compaction(schedule_ir: Mapping[str, Any]) -> int:
    frame_count = 0
    for frame in schedule_ir.get("frames", []):
        if not isinstance(frame, Mapping):
            continue
        kind = str(frame.get("kind", "drive_frame"))
        cycle = int(frame["cycle"])
        covered = {
            int(pattern["port"])
            for pattern in schedule_ir.get("patterns", [])
            if isinstance(pattern, Mapping)
            and str(pattern.get("kind")) == "periodic_drive"
            and int(pattern["start_cycle"]) <= cycle < int(pattern["end_cycle"])
        }
        items = [
            item
            for item in frame.get("items", [])
            if isinstance(item, Mapping) and not (kind == "drive_frame" and int(item["port"]) in covered)
        ]
        if items:
            frame_count += 1
    return frame_count


@dataclass(frozen=True)
class _Pycstb4SectionBuildState:
    schedule_ir: Mapping[str, Any]
    strings: _StringTable
    max_words: int
    pattern_blob: bytes
    pattern_count: int
    frame_blob: bytes
    frame_count: int
    actor_bundle_blob: bytes
    actor_bundle_count: int
    actor_payload_blob: bytes
    actor_payload_table_blob: bytes
    instruction_stream_blob: bytes
    instruction_stream_count: int
    seeded_generator_blob: bytes
    seeded_generator_count: int
    external_stream_blob: bytes
    external_stream_count: int
    scoreboard_policy_blob: bytes
    scoreboard_policy_count: int


_Pycstb4SectionEmitResult = tuple[bytes, int] | None
_Pycstb4SectionEmitter = Callable[[_Pycstb4SectionBuildState], _Pycstb4SectionEmitResult]


def _pycstb4_pattern_count(schedule_ir: Mapping[str, Any]) -> int:
    return sum(
        1
        for pattern in schedule_ir.get("patterns", [])
        if isinstance(pattern, Mapping) and str(pattern.get("kind")) == "periodic_drive"
    )


def _build_pycstb4_section_state(
    schedule_ir: Mapping[str, Any], strings: _StringTable, max_words: int
) -> _Pycstb4SectionBuildState:
    actor_bundle_blob, actor_bundle_count = _pack_pycstb4_actor_bundle(schedule_ir, strings)
    instruction_stream_blob, instruction_stream_count = _pack_pycstb4_instruction_streams(schedule_ir, strings)
    seeded_generator_blob, seeded_generator_count = _pack_pycstb4_seeded_workload_generators(schedule_ir, strings)
    external_stream_blob, external_stream_count = _pack_pycstb4_external_stream_sources(schedule_ir, strings)
    scoreboard_policy_blob, scoreboard_policy_count = _pack_pycstb4_scoreboard_policies(schedule_ir, strings)
    return _Pycstb4SectionBuildState(
        schedule_ir=schedule_ir,
        strings=strings,
        max_words=max_words,
        pattern_blob=_pack_pycstb4_patterns(schedule_ir, max_words),
        pattern_count=_pycstb4_pattern_count(schedule_ir),
        frame_blob=_pack_pycstb4_frames(schedule_ir, strings, max_words),
        frame_count=_pycstb4_frame_count_after_pattern_compaction(schedule_ir),
        actor_bundle_blob=actor_bundle_blob,
        actor_bundle_count=actor_bundle_count,
        actor_payload_blob=_pack_pycstb4_actor_payload_blob(schedule_ir),
        actor_payload_table_blob=_pack_pycstb4_actor_payload_table(schedule_ir),
        instruction_stream_blob=instruction_stream_blob,
        instruction_stream_count=instruction_stream_count,
        seeded_generator_blob=seeded_generator_blob,
        seeded_generator_count=seeded_generator_count,
        external_stream_blob=external_stream_blob,
        external_stream_count=external_stream_count,
        scoreboard_policy_blob=scoreboard_policy_blob,
        scoreboard_policy_count=scoreboard_policy_count,
    )


def _emit_string_table_section(state: _Pycstb4SectionBuildState) -> _Pycstb4SectionEmitResult:
    return state.strings.to_bytes(), len(state.strings.items)


def _emit_port_table_section(state: _Pycstb4SectionBuildState) -> _Pycstb4SectionEmitResult:
    return _pack_pycstb4_ports(state.schedule_ir, state.strings), len(state.schedule_ir.get("ports", []))


def _emit_event_table_section(state: _Pycstb4SectionBuildState) -> _Pycstb4SectionEmitResult:
    return _pack_pycstb4_events(state.schedule_ir, state.strings, state.max_words), len(state.schedule_ir.get("events", []))


def _emit_frame_table_section(state: _Pycstb4SectionBuildState) -> _Pycstb4SectionEmitResult:
    return state.frame_blob, state.frame_count


def _emit_pattern_table_section(state: _Pycstb4SectionBuildState) -> _Pycstb4SectionEmitResult:
    return (state.pattern_blob, state.pattern_count) if state.pattern_count else None


def _emit_actor_bundle_section(state: _Pycstb4SectionBuildState) -> _Pycstb4SectionEmitResult:
    return (state.actor_bundle_blob, state.actor_bundle_count) if state.actor_bundle_blob else None


def _emit_actor_payload_blob_section(state: _Pycstb4SectionBuildState) -> _Pycstb4SectionEmitResult:
    if state.actor_payload_blob and not state.actor_payload_table_blob:
        return state.actor_payload_blob, 1
    return None


def _emit_actor_payload_table_section(state: _Pycstb4SectionBuildState) -> _Pycstb4SectionEmitResult:
    return (state.actor_payload_table_blob, 2) if state.actor_payload_table_blob else None


def _emit_instruction_stream_section(state: _Pycstb4SectionBuildState) -> _Pycstb4SectionEmitResult:
    return (state.instruction_stream_blob, state.instruction_stream_count) if state.instruction_stream_blob else None


def _emit_seeded_workload_generator_section(state: _Pycstb4SectionBuildState) -> _Pycstb4SectionEmitResult:
    return (state.seeded_generator_blob, state.seeded_generator_count) if state.seeded_generator_blob else None


def _emit_external_stream_source_section(state: _Pycstb4SectionBuildState) -> _Pycstb4SectionEmitResult:
    return (state.external_stream_blob, state.external_stream_count) if state.external_stream_blob else None


def _emit_scoreboard_policy_section(state: _Pycstb4SectionBuildState) -> _Pycstb4SectionEmitResult:
    return (state.scoreboard_policy_blob, state.scoreboard_policy_count) if state.scoreboard_policy_blob else None


_PYCSTB4_SECTION_EMITTERS: tuple[tuple[int, _Pycstb4SectionEmitter], ...] = (
    (int(SectionKind.STRING_TABLE), _emit_string_table_section),
    (int(SectionKind.PORT_TABLE), _emit_port_table_section),
    (int(SectionKind.EVENT_TABLE), _emit_event_table_section),
    (int(SectionKind.FRAME_TABLE), _emit_frame_table_section),
    (int(SectionKind.PATTERN_TABLE), _emit_pattern_table_section),
    (int(SectionKind.ACTOR_BUNDLE), _emit_actor_bundle_section),
    (_PYCSTB4_ACTOR_PAYLOAD_BLOB_SECTION, _emit_actor_payload_blob_section),
    (_PYCSTB4_ACTOR_PAYLOAD_TABLE_SECTION, _emit_actor_payload_table_section),
    (_PYCSTB4_INSTRUCTION_STREAM_SECTION, _emit_instruction_stream_section),
    (_PYCSTB4_SEEDED_WORKLOAD_GENERATOR_SECTION, _emit_seeded_workload_generator_section),
    (_PYCSTB4_EXTERNAL_STREAM_SOURCE_SECTION, _emit_external_stream_source_section),
    (_PYCSTB4_SCOREBOARD_POLICY_SECTION, _emit_scoreboard_policy_section),
)


def _packed_section(
    registry: SectionRegistry, kind: SectionKind | int, data: bytes, count: int, *, flags: int = 1
) -> PackedSection:
    descriptor = registry.by_kind(int(kind))
    return PackedSection(
        kind=int(kind),
        data=data,
        count=int(count),
        flags=int(flags),
        name=descriptor.name if descriptor is not None else f"unknown_{int(kind)}",
    )


def build_pycstb4_section_plan(schedule_ir: Mapping[str, Any], strings: _StringTable, max_words: int) -> list[PackedSection]:
    registry = default_section_registry()
    state = _build_pycstb4_section_state(schedule_ir, strings, max_words)
    sections: list[PackedSection] = []
    for kind, emitter in _PYCSTB4_SECTION_EMITTERS:
        result = emitter(state)
        if result is None:
            continue
        data, count = result
        sections.append(_packed_section(registry, kind, data, count))
    return sections


def schedule_ir_to_pycstb4_bytes(schedule_ir: Mapping[str, Any]) -> bytes:
    validation_errors = verify_schedule_ir_for_pycstb4(schedule_ir)
    if validation_errors:
        joined = "\n".join(f"- {item}" for item in validation_errors)
        raise ValueError(f"invalid PYCSTB4 schedule IR:\n{joined}")
    version = schedule_ir.get("version", {})
    timebase = schedule_ir.get("timebase", {})
    strings = _collect_pycstb4_strings(schedule_ir)
    max_words = _pycstb4_max_words(schedule_ir)
    sections = build_pycstb4_section_plan(schedule_ir, strings, max_words)
    header_size = struct.calcsize(_PYCSTB4_HEADER_FMT)
    dir_size = struct.calcsize(_PYCSTB4_DIR_FMT)
    offset = header_size + len(sections) * dir_size
    directory = bytearray()
    payload = bytearray()
    for packed in sections:
        directory.extend(
            struct.pack(
                _PYCSTB4_DIR_FMT,
                int(packed.kind),
                int(packed.flags),
                0,
                int(offset),
                len(packed.data),
                int(packed.count),
            )
        )
        payload.extend(packed.data)
        offset += len(packed.data)
    header = struct.pack(
        _PYCSTB4_HEADER_FMT,
        _PYCSTB4_MAGIC,
        1,
        header_size,
        int(version.get("major", 1)),
        int(version.get("minor", 0)),
        int(version.get("patch", 0)),
        0,
        len(sections),
        max_words,
        int(timebase.get("max_cycle", 0)),
        int(timebase.get("reset_cycles", 0)),
        0,
    )
    return header + bytes(directory) + bytes(payload)
