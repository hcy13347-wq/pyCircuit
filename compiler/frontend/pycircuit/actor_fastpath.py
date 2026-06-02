from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .schedule_ir import schedule_ir_to_pycstb4_bytes


def _ready_pattern_ir(ready_pattern: Mapping[str, Any], actor_end_cycle: int) -> dict[str, Any]:
    if ready_pattern.get("kind") == "periodic":
        return {
            "kind": "periodic_drive",
            "period": int(ready_pattern["period"]),
            "active_cycles": int(ready_pattern["active_cycles"]),
            "phase_cycle": int(ready_pattern["phase_cycle"]),
            "start_cycle": 0,
            "end_cycle": int(actor_end_cycle),
            "active_value": "0x0",
            "default_value": "0x1",
        }
    return {"kind": "constant", "value": "0x1"}


def detect_ready_pattern(samples: Sequence[tuple[int, int]]) -> dict[str, int | str]:
    """Infer the compact ready pattern accepted by the actor-fastpath runner.

    This keeps protocol-shape inference out of the CLI renderer. The v0 actor
    fast path intentionally supports only constant-ready and simple periodic
    backpressure because those are the two shapes used by current scalable
    synthetic and LowpAlu proxy workloads.
    """

    if not samples or all(int(value) == 1 for _cycle, value in samples):
        return {"kind": "constant", "value": "0x1"}

    ordered = sorted((int(cycle), int(value) & 1) for cycle, value in samples)
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    last_cycle: int | None = None
    for cycle, value in ordered:
        if value == 0 and run_start is None:
            run_start = cycle
        if value != 0 and run_start is not None:
            runs.append((run_start, int(last_cycle) + 1))
            run_start = None
        last_cycle = cycle
    if run_start is not None and last_cycle is not None:
        runs.append((run_start, last_cycle + 1))

    if len(runs) < 2:
        raise SystemExit("actor-fastpath currently supports only constant or periodic result_ready")

    ref_idx = 1 if len(runs) >= 3 else 0
    period = int(runs[ref_idx + 1][0] - runs[ref_idx][0])
    active_cycles = int(runs[ref_idx][1] - runs[ref_idx][0])
    if period <= 1 or active_cycles <= 0 or active_cycles >= period:
        raise SystemExit("actor-fastpath failed to infer periodic result_ready")

    phase_cycle = (-int(runs[ref_idx][0])) % period
    for cycle, value in ordered:
        expected = 0 if ((int(cycle) + phase_cycle) % period) < active_cycles else 1
        if int(value) != expected:
            raise SystemExit("actor-fastpath result_ready pattern is not purely periodic")

    return {
        "kind": "periodic",
        "period": period,
        "active_cycles": active_cycles,
        "phase_cycle": phase_cycle,
    }


def infer_ready_valid_actor_timing(
    *,
    ready_samples: Sequence[tuple[int, int]],
    transaction_count: int,
    timeout_cycles: int,
    start_cycle: int = 1,
    slack_cycles: int = 64,
) -> dict[str, Any]:
    ready_pattern = detect_ready_pattern(ready_samples)
    if ready_pattern["kind"] == "periodic":
        accept_cycles = int(ready_pattern["period"]) - int(ready_pattern["active_cycles"])
        estimated_cycles = (int(transaction_count) * int(ready_pattern["period"]) + accept_cycles - 1) // accept_cycles
    else:
        estimated_cycles = int(transaction_count)
    actor_end_cycle = max(int(timeout_cycles), int(start_cycle) + estimated_cycles + int(slack_cycles))
    return {
        "ready_pattern": ready_pattern,
        "start_cycle": int(start_cycle),
        "actor_end_cycle": int(actor_end_cycle),
    }


def write_ready_valid_actor_sidecars(
    *,
    actor_data_path: Path,
    top_name: str,
    data_width: int,
    source_payloads: Sequence[int] | None,
    ready_pattern: Mapping[str, Any],
    start_cycle: int,
    actor_end_cycle: int,
    reset_cycles: int,
    clocking: str,
    pure_wgen: bool = False,
    transaction_count: int | None = None,
    workload_generator: Mapping[str, Any] | None = None,
    source_valid_name: str = "cmd_valid",
    source_payload_name: str = "cmd_data",
    source_ready_name: str = "cmd_ready",
    sink_valid_name: str = "result_valid",
    sink_payload_name: str = "result_data",
    sink_ready_name: str = "result_ready",
    source_protocol: str = "cmd",
    sink_protocol: str = "result",
    source_actor_name: str = "cmd_source",
    sink_actor_name: str = "result_sink",
    scoreboard_name: str = "result_scoreboard",
    instruction_streams: Sequence[Mapping[str, Any]] | None = None,
    external_stream_sources: Sequence[Mapping[str, Any]] | None = None,
    scoreboard_policies: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write experimental ready-valid actor sidecars for actor-fastpath.

    This module intentionally owns sidecar formats and Schedule IR construction.
    The CLI renderer should only infer the ready-valid workload and generate the
    small C++ runner that consumes these sidecars.
    """

    actor_data_path.parent.mkdir(parents=True, exist_ok=True)
    lcg_multiplier = 0x45D9F3B
    payload_mask = (1 << int(data_width)) - 1 if int(data_width) < 64 else 0xFFFFFFFFFFFFFFFF
    payload_count = int(transaction_count) if transaction_count is not None else (len(source_payloads) if source_payloads is not None else 0)
    lcg_matches = False
    if source_payloads is not None:
        lcg_matches = all((int(value) & payload_mask) == (((idx + 1) * lcg_multiplier) & payload_mask) for idx, value in enumerate(source_payloads))
    workload_generators: list[dict[str, Any]] = []
    if workload_generator is not None:
        workload_generators.append(dict(workload_generator))
    elif lcg_matches:
        workload_generators.append(
            {
                "name": f"{source_protocol}_seeded_source",
                "generator_id": "lcg_payload_v0",
                "profile": "synthetic_ready_valid_payload",
                "seed": 0,
                "count": payload_count,
                "start_index": 0,
                "output_ports": [1],
                "constraints": {
                    "data_width": int(data_width),
                    "multiplier": hex(lcg_multiplier),
                    "formula": "((index + 1) * multiplier + seed) & mask",
                },
                "deterministic": True,
            }
        )
    if pure_wgen and not workload_generators:
        raise SystemExit("actor-fastpath pure WGEN requires a recognized seeded workload generator")
    if not pure_wgen and source_payloads is None:
        raise SystemExit("actor-fastpath materialized payload path requires source payloads")

    actor_blob = bytearray()
    if not pure_wgen:
        actor_blob.extend(b"PACTR0\0\0")
        actor_blob.extend((1).to_bytes(4, "little", signed=False))
        actor_blob.extend(payload_count.to_bytes(4, "little", signed=False))
        actor_blob.extend((1).to_bytes(4, "little", signed=False))
        actor_blob.extend((1).to_bytes(4, "little", signed=False))
        actor_blob.extend((0).to_bytes(4, "little", signed=False))
        for value in source_payloads or ():
            actor_blob.extend(int(value).to_bytes(8, "little", signed=False))
            actor_blob.extend(int(value).to_bytes(8, "little", signed=False))
        actor_data_path.write_bytes(bytes(actor_blob))

    source_actor: dict[str, Any] = {
        "kind": "ready_valid_source",
        "name": str(source_actor_name),
        "valid_port": 0,
        "ready_port": 2,
        "payload_ports": [1],
        "start_cycle": int(start_cycle),
        "end_cycle": int(actor_end_cycle),
        "policy": "hold_valid",
    }
    result_scoreboard: dict[str, Any] = {
        "kind": "ordered",
        "name": str(scoreboard_name),
        "payload_ports": [4],
    }
    if not pure_wgen:
        source_actor["transaction_source"] = {
            "kind": "external_pactr0",
            "path": str(actor_data_path),
            "count": payload_count,
            "payload_ports": [1],
            "byte_offset": 28,
            "byte_size": len(actor_blob) - 28,
        }
        result_scoreboard["expected_source"] = {
            "kind": "external_pactr0",
            "path": str(actor_data_path),
            "count": payload_count,
            "payload_ports": [4],
            "byte_offset": 28,
            "byte_size": len(actor_blob) - 28,
        }

    metadata = {
        "case_name": f"tb_{top_name}",
        "generator": "pycircuit.actor_fastpath",
        "generator_version": "prototype-wgen0" if pure_wgen else "prototype-pactr0",
        "source": str(actor_data_path),
        "notes": "Experimental ready-valid actor fastpath metadata. Transaction payloads are generated dynamically from seeded workload generator section 20."
        if pure_wgen
        else "Experimental ready-valid actor fastpath metadata. Transaction payloads are stored in the external PACTR0 sidecar and converted into PYCSTB4 section 18 when possible.",
    }
    if not pure_wgen:
        metadata["actor_payload_blob"] = str(actor_data_path)

    actor_schedule_ir = {
        "schema": "pycircuit.schedule_ir",
        "version": {"major": 1, "minor": 0, "patch": 0},
        "metadata": metadata,
        "timebase": {
            "unit": "cycle",
            "max_cycle": int(actor_end_cycle),
            "reset_cycles": int(reset_cycles),
            "clocking": str(clocking),
        },
        "ports": [
            {"id": 0, "name": str(source_valid_name), "direction": "input", "bit_width": 1, "word_count": 1, "role": "valid", "protocol": str(source_protocol)},
            {"id": 1, "name": str(source_payload_name), "direction": "input", "bit_width": int(data_width), "word_count": 1, "role": "data", "protocol": str(source_protocol)},
            {"id": 2, "name": str(source_ready_name), "direction": "output", "bit_width": 1, "word_count": 1, "role": "ready", "protocol": str(source_protocol)},
            {"id": 3, "name": str(sink_valid_name), "direction": "output", "bit_width": 1, "word_count": 1, "role": "valid", "protocol": str(sink_protocol)},
            {"id": 4, "name": str(sink_payload_name), "direction": "output", "bit_width": int(data_width), "word_count": 1, "role": "data", "protocol": str(sink_protocol)},
            {"id": 5, "name": str(sink_ready_name), "direction": "input", "bit_width": 1, "word_count": 1, "role": "ready", "protocol": str(sink_protocol)},
        ],
        "events": [],
        "frames": [],
        "patterns": [],
        "workload_generators": workload_generators,
        "instruction_streams": [dict(stream) for stream in (instruction_streams or ())],
        "external_stream_sources": [dict(source) for source in (external_stream_sources or ())],
        "actors": [
            source_actor,
            {
                "kind": "ready_valid_sink",
                "name": str(sink_actor_name),
                "valid_port": 3,
                "ready_port": 5,
                "payload_ports": [4],
                "start_cycle": int(start_cycle),
                "end_cycle": int(actor_end_cycle),
                "sample_policy": "on_handshake",
                "ready_pattern": _ready_pattern_ir(ready_pattern, actor_end_cycle),
                "scoreboard": str(scoreboard_name),
            },
        ],
        "scoreboards": [result_scoreboard],
        "scoreboard_policies": [dict(policy) for policy in (scoreboard_policies or ())],
        "stats": {
            "event_count": 0,
            "frame_count": 0,
            "pattern_count": 0,
            "actor_count": 2,
            "port_count": 6,
            "instruction_stream_count": len(instruction_streams or ()),
            "external_stream_source_count": len(external_stream_sources or ()),
            "scoreboard_policy_count": len(scoreboard_policies or ()),
            "max_cycle": int(actor_end_cycle),
            "schedule_bytes": 0,
            "actor_data_bytes": len(actor_blob),
            "transaction_count": payload_count,
            "pure_wgen": bool(pure_wgen),
        },
    }

    actor_schedule_json_text = json.dumps(actor_schedule_ir, sort_keys=True, indent=2) + "\n"
    actor_schedule_json_path = actor_data_path.with_suffix(".schedule.json")
    actor_schedule_json_path.write_text(actor_schedule_json_text, encoding="utf-8")

    actor_schedule_pycstb4_path = actor_data_path.with_suffix(".schedule.pycstb4")
    actor_schedule_pycstb4 = b""
    if not pure_wgen:
        actor_schedule_pycstb4 = schedule_ir_to_pycstb4_bytes(actor_schedule_ir)
        actor_schedule_pycstb4_path.write_bytes(actor_schedule_pycstb4)
    actor_schedule_wgen_pycstb4_path = actor_data_path.with_suffix(".schedule.wgen.pycstb4")
    actor_schedule_wgen_pycstb4_bytes = 0
    if workload_generators:
        wgen_schedule_ir = dict(actor_schedule_ir)
        wgen_metadata = dict(actor_schedule_ir["metadata"])
        wgen_metadata.pop("actor_payload_blob", None)
        wgen_metadata["generator_version"] = "prototype-wgen0"
        wgen_metadata["notes"] = "Experimental ready-valid actor fastpath metadata. Transaction payloads are generated dynamically from seeded workload generator section 20."
        wgen_schedule_ir["metadata"] = wgen_metadata
        actor_schedule_wgen_pycstb4 = schedule_ir_to_pycstb4_bytes(wgen_schedule_ir)
        actor_schedule_wgen_pycstb4_path.write_bytes(actor_schedule_wgen_pycstb4)
        actor_schedule_wgen_pycstb4_bytes = len(actor_schedule_wgen_pycstb4)

    actor_stats = {
        "version": 0,
        "format": "PACTR0",
        "pure_wgen": bool(pure_wgen),
        "materialized_payload": not bool(pure_wgen),
        "actor_data": "" if pure_wgen else str(actor_data_path),
        "actor_data_bytes": len(actor_blob),
        "transactions": payload_count,
        "payload_width": int(data_width),
        "start_cycle": int(start_cycle),
        "end_cycle": int(actor_end_cycle),
        "ready_pattern": dict(ready_pattern),
        "workload_generator_count": len(workload_generators),
        "actor_schedule_json": str(actor_schedule_json_path),
        "actor_schedule_json_bytes": len(actor_schedule_json_text.encode("utf-8")),
        "actor_schedule_pycstb4": "" if pure_wgen else str(actor_schedule_pycstb4_path),
        "actor_schedule_pycstb4_bytes": len(actor_schedule_pycstb4),
        "actor_schedule_wgen_pycstb4": str(actor_schedule_wgen_pycstb4_path) if workload_generators else "",
        "actor_schedule_wgen_pycstb4_bytes": actor_schedule_wgen_pycstb4_bytes,
        "instruction_stream_count": len(instruction_streams or ()),
        "external_stream_source_count": len(external_stream_sources or ()),
        "scoreboard_policy_count": len(scoreboard_policies or ()),
    }
    actor_data_path.with_suffix(".stats.json").write_text(json.dumps(actor_stats, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return actor_stats
