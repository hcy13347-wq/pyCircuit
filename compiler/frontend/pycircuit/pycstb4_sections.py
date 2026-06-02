from __future__ import annotations

import json
import struct
import hashlib
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Iterable, Mapping


PYCSTB4_MAGIC = b"PYCSTB4\n"
PYCSTB4_HEADER_FMT = "<8sBHHHHQIIQII"
PYCSTB4_DIR_FMT = "<HHIQQQ"
PYCSTB4_PORT_FMT = "<IIBBHIII"
PYCSTB4_ACTOR_BUNDLE_HEADER_FMT = "<4sHHIIII"
PYCSTB4_EXTERNAL_SOURCE_FMT = "<IIIQQQII"
PYCSTB4_ACTOR_RECORD_FMT = "<HHIIIIIQQIIHHQQQQQQQ"
PYCSTB4_SCOREBOARD_RECORD_FMT = "<HHIIII"
PYCSTB4_ACTOR_PAYLOAD_TABLE_HEADER_FMT = "<4sHHI"
PYCSTB4_ACTOR_PAYLOAD_TABLE_RECORD_HEADER_FMT = "<IIII"
PYCSTB4_INSTRUCTION_STREAM_HEADER_FMT = "<4sHHII"
PYCSTB4_INSTRUCTION_STREAM_RECORD_FMT = "<IIIIIIQQQ"
PYCSTB4_EXTERNAL_STREAM_HEADER_FMT = "<4sHHII"
PYCSTB4_EXTERNAL_STREAM_RECORD_FMT = "<IIIIQQQQ"
PYCSTB4_SCOREBOARD_POLICY_HEADER_FMT = "<4sHHII"
PYCSTB4_SCOREBOARD_POLICY_RECORD_FMT = "<IIIIIQQQ"
PYCSTB4_SEEDED_WORKLOAD_HEADER_FMT = "<4sHHII"
PYCSTB4_SEEDED_WORKLOAD_RECORD_FMT = "<IIIIQQQQII"
PYCSTB4_HEADER_SIZE = struct.calcsize(PYCSTB4_HEADER_FMT)
PYCSTB4_DIR_SIZE = struct.calcsize(PYCSTB4_DIR_FMT)
PYCSTB4_NONE = 0xFFFFFFFF

_DIRECTION_NAME = {0: "input", 1: "output", 2: "inout"}
_ROLE_NAME = {0: "unknown", 1: "data", 2: "valid", 3: "ready", 4: "tag", 5: "control", 6: "clock", 7: "reset"}
_ACTOR_KIND_NAME = {0: "ready_valid_source", 1: "ready_valid_sink"}
_ACTOR_POLICY_NAME = {0: "hold_valid", 1: "on_handshake"}
_SCOREBOARD_KIND_NAME = {0: "ordered", 1: "tagged", 2: "masked"}
_READY_KIND_NAME = {0: "none", 1: "constant", 2: "periodic_drive"}


class SectionKind(IntEnum):
    STRING_TABLE = 1
    PORT_TABLE = 2
    EVENT_TABLE = 3
    FRAME_TABLE = 4
    PATTERN_TABLE = 5
    ACTOR_BUNDLE = 16
    ACTOR_PAYLOAD_BLOB = 17
    ACTOR_PAYLOAD_TABLE = 18
    INSTRUCTION_STREAM = 19
    SEEDED_WORKLOAD_GENERATOR = 20
    EXTERNAL_STREAM_SOURCE = 21
    SCOREBOARD_POLICY = 22


@dataclass(frozen=True)
class SectionDescriptor:
    kind: int
    name: str
    major: int = 0
    minor: int = 1
    required: bool = False
    experimental: bool = False
    deprecated: bool = False
    dependencies: tuple[int, ...] = ()
    runtime_tags: tuple[str, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class SectionDirectoryEntry:
    kind: int
    flags: int
    offset: int
    size: int
    count: int
    name: str
    known: bool
    required: bool
    experimental: bool
    deprecated: bool


@dataclass(frozen=True)
class Pycstb4Header:
    endian: int
    header_size: int
    major: int
    minor: int
    patch: int
    flags: int
    section_count: int
    max_words: int
    max_cycle: int
    reset_cycles: int
    reserved: int


@dataclass(frozen=True)
class PackedSection:
    kind: int
    data: bytes
    count: int
    flags: int = 1
    name: str = ""


class SectionRegistry:
    def __init__(self, descriptors: Iterable[SectionDescriptor] = ()) -> None:
        self._by_kind: dict[int, SectionDescriptor] = {}
        self._by_name: dict[str, SectionDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: SectionDescriptor) -> None:
        kind = int(descriptor.kind)
        name = str(descriptor.name)
        if kind in self._by_kind:
            raise ValueError(f"duplicate PYCSTB4 section kind: {kind}")
        if name in self._by_name:
            raise ValueError(f"duplicate PYCSTB4 section name: {name}")
        self._by_kind[kind] = descriptor
        self._by_name[name] = descriptor

    def by_kind(self, kind: int) -> SectionDescriptor | None:
        return self._by_kind.get(int(kind))

    def by_name(self, name: str) -> SectionDescriptor | None:
        return self._by_name.get(str(name))

    def descriptors(self) -> list[SectionDescriptor]:
        return [self._by_kind[kind] for kind in sorted(self._by_kind)]

    def describe_kind(self, kind: int) -> str:
        descriptor = self.by_kind(kind)
        return descriptor.name if descriptor is not None else f"unknown_{int(kind)}"

    def required_kinds(self) -> set[int]:
        return {kind for kind, descriptor in self._by_kind.items() if descriptor.required}


def default_section_registry() -> SectionRegistry:
    return SectionRegistry(
        [
            SectionDescriptor(
                kind=SectionKind.STRING_TABLE,
                name="string_table",
                required=True,
                runtime_tags=("container", "runtime-loop", "actor-fastpath"),
                summary="Global string pool referenced by other sections.",
            ),
            SectionDescriptor(
                kind=SectionKind.PORT_TABLE,
                name="port_table",
                required=True,
                dependencies=(SectionKind.STRING_TABLE,),
                runtime_tags=("runtime-loop", "actor-fastpath"),
                summary="DUT port ids, names, directions, widths, roles, and protocol hints.",
            ),
            SectionDescriptor(
                kind=SectionKind.EVENT_TABLE,
                name="event_table",
                required=True,
                dependencies=(SectionKind.PORT_TABLE,),
                runtime_tags=("runtime-loop",),
                summary="Cycle-level expect/sample events.",
            ),
            SectionDescriptor(
                kind=SectionKind.FRAME_TABLE,
                name="frame_table",
                required=True,
                dependencies=(SectionKind.PORT_TABLE,),
                runtime_tags=("runtime-loop",),
                summary="Cycle-level drive frames.",
            ),
            SectionDescriptor(
                kind=SectionKind.PATTERN_TABLE,
                name="pattern_table",
                dependencies=(SectionKind.PORT_TABLE,),
                runtime_tags=("runtime-loop", "actor-fastpath"),
                summary="Compact periodic drive/backpressure patterns.",
            ),
            SectionDescriptor(
                kind=SectionKind.ACTOR_BUNDLE,
                name="actor_bundle",
                experimental=True,
                dependencies=(SectionKind.PORT_TABLE,),
                runtime_tags=("actor-fastpath",),
                summary="Ready-valid source/sink actors and scoreboard binding metadata.",
            ),
            SectionDescriptor(
                kind=SectionKind.ACTOR_PAYLOAD_BLOB,
                name="actor_payload_blob",
                experimental=True,
                dependencies=(SectionKind.ACTOR_BUNDLE,),
                runtime_tags=("actor-fastpath",),
                summary="Legacy materialized actor payload blob.",
            ),
            SectionDescriptor(
                kind=SectionKind.ACTOR_PAYLOAD_TABLE,
                name="actor_payload_table",
                experimental=True,
                dependencies=(SectionKind.ACTOR_BUNDLE,),
                runtime_tags=("actor-fastpath",),
                summary="Structured materialized actor transaction payload tables.",
            ),
            SectionDescriptor(
                kind=SectionKind.INSTRUCTION_STREAM,
                name="instruction_stream",
                experimental=True,
                dependencies=(SectionKind.STRING_TABLE,),
                runtime_tags=("workload",),
                summary="Inline instruction/raw word stream.",
            ),
            SectionDescriptor(
                kind=SectionKind.SEEDED_WORKLOAD_GENERATOR,
                name="seeded_workload_generator",
                experimental=True,
                dependencies=(SectionKind.PORT_TABLE,),
                runtime_tags=("actor-fastpath", "workload"),
                summary="Seeded/generated workload metadata.",
            ),
            SectionDescriptor(
                kind=SectionKind.EXTERNAL_STREAM_SOURCE,
                name="external_stream_source",
                experimental=True,
                dependencies=(SectionKind.STRING_TABLE,),
                runtime_tags=("workload",),
                summary="External trace/workload reference metadata.",
            ),
            SectionDescriptor(
                kind=SectionKind.SCOREBOARD_POLICY,
                name="scoreboard_policy",
                experimental=True,
                dependencies=(SectionKind.PORT_TABLE,),
                runtime_tags=("checker",),
                summary="Checker policy metadata.",
            ),
        ]
    )


def section_registry_manifest(registry: SectionRegistry | None = None) -> dict[str, Any]:
    registry = default_section_registry() if registry is None else registry
    sections = [
        {
            "kind": int(descriptor.kind),
            "name": descriptor.name,
            "major": int(descriptor.major),
            "minor": int(descriptor.minor),
            "required": bool(descriptor.required),
            "experimental": bool(descriptor.experimental),
            "deprecated": bool(descriptor.deprecated),
            "dependencies": [int(dep) for dep in descriptor.dependencies],
            "runtime_tags": list(descriptor.runtime_tags),
            "summary": descriptor.summary,
        }
        for descriptor in registry.descriptors()
    ]
    payload = json.dumps(sections, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema": "pycircuit.pycstb4.section_registry",
        "schema_version": {"major": 0, "minor": 1},
        "section_count": len(sections),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "sections": sections,
    }


def _read_exact_header(data: bytes) -> tuple[Pycstb4Header | None, list[str]]:
    errors: list[str] = []
    if len(data) < PYCSTB4_HEADER_SIZE:
        return None, [f"file too small for PYCSTB4 header: {len(data)} bytes"]
    unpacked = struct.unpack_from(PYCSTB4_HEADER_FMT, data, 0)
    magic = unpacked[0]
    if magic != PYCSTB4_MAGIC:
        errors.append("invalid PYCSTB4 magic")
    header = Pycstb4Header(
        endian=int(unpacked[1]),
        header_size=int(unpacked[2]),
        major=int(unpacked[3]),
        minor=int(unpacked[4]),
        patch=int(unpacked[5]),
        flags=int(unpacked[6]),
        section_count=int(unpacked[7]),
        max_words=int(unpacked[8]),
        max_cycle=int(unpacked[9]),
        reset_cycles=int(unpacked[10]),
        reserved=int(unpacked[11]),
    )
    return header, errors


def _slice_section(data: bytes, section: SectionDirectoryEntry) -> bytes:
    return data[section.offset : section.offset + section.size]


def _decode_string_ref(strings: list[str], sid: int) -> str | None:
    if sid == PYCSTB4_NONE:
        return None
    if 0 <= sid < len(strings):
        return strings[sid]
    return f"<bad-string-ref:{sid}>"


def _decode_string_table(blob: bytes) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    if len(blob) < 4:
        return [], ["string_table too small"]
    count = int.from_bytes(blob[0:4], "little", signed=False)
    pos = 4
    out: list[str] = []
    for idx in range(count):
        if pos + 4 > len(blob):
            errors.append(f"string_table truncated before string length index={idx}")
            break
        n = int.from_bytes(blob[pos : pos + 4], "little", signed=False)
        pos += 4
        if pos + n > len(blob):
            errors.append(f"string_table truncated string index={idx} length={n}")
            break
        out.append(blob[pos : pos + n].decode("utf-8", errors="replace"))
        pos += n
    return out, errors


def _decode_ports(blob: bytes, count: int, strings: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    record_size = struct.calcsize(PYCSTB4_PORT_FMT)
    out: list[dict[str, Any]] = []
    for idx in range(count):
        pos = idx * record_size
        if pos + record_size > len(blob):
            errors.append(f"port_table truncated at record {idx}")
            break
        port_id, name_sid, direction, role, _reserved, bit_width, word_count, protocol_sid = struct.unpack_from(
            PYCSTB4_PORT_FMT, blob, pos
        )
        out.append(
            {
                "id": int(port_id),
                "name": _decode_string_ref(strings, int(name_sid)),
                "direction": _DIRECTION_NAME.get(int(direction), f"unknown_{int(direction)}"),
                "role": _ROLE_NAME.get(int(role), f"unknown_{int(role)}"),
                "bit_width": int(bit_width),
                "word_count": int(word_count),
                "protocol": _decode_string_ref(strings, int(protocol_sid)),
            }
        )
    return out, errors


def _decode_actor_bundle_summary(blob: bytes) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    header_size = struct.calcsize(PYCSTB4_ACTOR_BUNDLE_HEADER_FMT)
    if len(blob) < header_size:
        return {}, ["actor_bundle too small"]
    magic, major, minor, external_source_count, actor_count, scoreboard_count, port_ref_count = struct.unpack_from(
        PYCSTB4_ACTOR_BUNDLE_HEADER_FMT, blob, 0
    )
    if magic != b"ACTR":
        errors.append("actor_bundle magic mismatch")
    pos = header_size
    port_refs: list[int] = []
    for idx in range(int(port_ref_count)):
        if pos + 4 > len(blob):
            errors.append(f"actor_bundle port_refs truncated at {idx}")
            break
        port_refs.append(int.from_bytes(blob[pos : pos + 4], "little", signed=False))
        pos += 4

    external_record_size = struct.calcsize(PYCSTB4_EXTERNAL_SOURCE_FMT)
    external_sources: list[dict[str, Any]] = []
    for idx in range(int(external_source_count)):
        if pos + external_record_size > len(blob):
            errors.append(f"actor_bundle external source truncated at {idx}")
            break
        kind, path_sid, count, byte_offset, byte_size, _reserved, first_payload, payload_count = struct.unpack_from(
            PYCSTB4_EXTERNAL_SOURCE_FMT, blob, pos
        )
        external_sources.append(
            {
                "kind": int(kind),
                "path_sid": int(path_sid),
                "count": int(count),
                "byte_offset": int(byte_offset),
                "byte_size": int(byte_size),
                "payload_ports": port_refs[int(first_payload) : int(first_payload) + int(payload_count)],
            }
        )
        pos += external_record_size

    actor_record_size = struct.calcsize(PYCSTB4_ACTOR_RECORD_FMT)
    actors: list[dict[str, Any]] = []
    for idx in range(int(actor_count)):
        if pos + actor_record_size > len(blob):
            errors.append(f"actor_bundle actor record truncated at {idx}")
            break
        (
            kind,
            policy,
            name_sid,
            valid_port,
            ready_port,
            first_payload,
            payload_count,
            start_cycle,
            end_cycle,
            source_ref,
            scoreboard_ref,
            ready_kind,
            _reserved_ready,
            ready_period,
            ready_active_cycles,
            ready_phase_cycle,
            ready_start_cycle,
            ready_end_cycle,
            ready_active_value,
            ready_default_value,
        ) = struct.unpack_from(PYCSTB4_ACTOR_RECORD_FMT, blob, pos)
        actors.append(
            {
                "kind": _ACTOR_KIND_NAME.get(int(kind), f"unknown_{int(kind)}"),
                "policy": _ACTOR_POLICY_NAME.get(int(policy), f"unknown_{int(policy)}"),
                "name_sid": int(name_sid),
                "valid_port": int(valid_port),
                "ready_port": int(ready_port),
                "payload_ports": port_refs[int(first_payload) : int(first_payload) + int(payload_count)],
                "start_cycle": int(start_cycle),
                "end_cycle": int(end_cycle),
                "source_ref": int(source_ref),
                "scoreboard_ref": int(scoreboard_ref),
                "ready_kind": _READY_KIND_NAME.get(int(ready_kind), f"unknown_{int(ready_kind)}"),
                "ready_period": int(ready_period),
                "ready_active_cycles": int(ready_active_cycles),
                "ready_phase_cycle": int(ready_phase_cycle),
                "ready_start_cycle": int(ready_start_cycle),
                "ready_end_cycle": int(ready_end_cycle),
                "ready_active_value": int(ready_active_value),
                "ready_default_value": int(ready_default_value),
            }
        )
        pos += actor_record_size

    scoreboard_record_size = struct.calcsize(PYCSTB4_SCOREBOARD_RECORD_FMT)
    scoreboards: list[dict[str, Any]] = []
    for idx in range(int(scoreboard_count)):
        if pos + scoreboard_record_size > len(blob):
            errors.append(f"actor_bundle scoreboard record truncated at {idx}")
            break
        kind, flags, name_sid, first_payload, payload_count, expected_ref = struct.unpack_from(
            PYCSTB4_SCOREBOARD_RECORD_FMT, blob, pos
        )
        scoreboards.append(
            {
                "kind": _SCOREBOARD_KIND_NAME.get(int(kind), f"unknown_{int(kind)}"),
                "flags": int(flags),
                "name_sid": int(name_sid),
                "payload_ports": port_refs[int(first_payload) : int(first_payload) + int(payload_count)],
                "expected_ref": int(expected_ref),
            }
        )
        pos += scoreboard_record_size
    return (
        {
            "major": int(major),
            "minor": int(minor),
            "external_source_count": int(external_source_count),
            "actor_count": int(actor_count),
            "scoreboard_count": int(scoreboard_count),
            "port_ref_count": int(port_ref_count),
            "external_sources": external_sources,
            "actors": actors,
            "scoreboards": scoreboards,
        },
        errors,
    )


def _decode_actor_payload_tables(blob: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    header_size = struct.calcsize(PYCSTB4_ACTOR_PAYLOAD_TABLE_HEADER_FMT)
    table_header_size = struct.calcsize(PYCSTB4_ACTOR_PAYLOAD_TABLE_RECORD_HEADER_FMT)
    if len(blob) < header_size:
        return [], ["actor_payload_table too small"]
    magic, major, minor, table_count = struct.unpack_from(PYCSTB4_ACTOR_PAYLOAD_TABLE_HEADER_FMT, blob, 0)
    if magic != b"ATXN":
        errors.append("actor_payload_table magic mismatch")
    pos = header_size
    out: list[dict[str, Any]] = []
    for table_index in range(int(table_count)):
        if pos + table_header_size > len(blob):
            errors.append(f"actor_payload_table truncated before table {table_index}")
            break
        payload_port_count, transaction_count, payload_word_count, flags = struct.unpack_from(
            PYCSTB4_ACTOR_PAYLOAD_TABLE_RECORD_HEADER_FMT, blob, pos
        )
        pos += table_header_size
        ports: list[int] = []
        for idx in range(int(payload_port_count)):
            if pos + 4 > len(blob):
                errors.append(f"actor_payload_table ports truncated table={table_index} index={idx}")
                break
            ports.append(int.from_bytes(blob[pos : pos + 4], "little", signed=False))
            pos += 4
        word_count = int(transaction_count) * int(payload_port_count) * int(payload_word_count)
        byte_count = word_count * 8
        if pos + byte_count > len(blob):
            errors.append(f"actor_payload_table words truncated table={table_index}")
            byte_count = max(0, len(blob) - pos)
        pos += byte_count
        out.append(
            {
                "section_major": int(major),
                "section_minor": int(minor),
                "payload_ports": ports,
                "transaction_count": int(transaction_count),
                "payload_word_count": int(payload_word_count),
                "flags": int(flags),
                "word_count": word_count,
                "byte_count": byte_count,
            }
        )
    return out, errors


def _decode_seeded_workload_generators(
    blob: bytes, strings: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    header_size = struct.calcsize(PYCSTB4_SEEDED_WORKLOAD_HEADER_FMT)
    record_size = struct.calcsize(PYCSTB4_SEEDED_WORKLOAD_RECORD_FMT)
    if len(blob) < header_size:
        return [], ["seeded_workload_generator section too small"]
    magic, major, minor, generator_count, port_ref_count = struct.unpack_from(PYCSTB4_SEEDED_WORKLOAD_HEADER_FMT, blob, 0)
    if magic != b"WGEN":
        errors.append("seeded_workload_generator magic mismatch")
    port_refs_start = header_size
    records_start = port_refs_start + int(port_ref_count) * 4
    if records_start > len(blob):
        return [], [*errors, "seeded_workload_generator port_refs truncated"]
    port_refs = [
        int.from_bytes(blob[port_refs_start + idx * 4 : port_refs_start + idx * 4 + 4], "little", signed=False)
        for idx in range(int(port_ref_count))
    ]
    out: list[dict[str, Any]] = []
    for idx in range(int(generator_count)):
        pos = records_start + idx * record_size
        if pos + record_size > len(blob):
            errors.append(f"seeded_workload_generator record truncated at {idx}")
            break
        (
            name_sid,
            generator_id_sid,
            profile_sid,
            constraints_sid,
            seed,
            count,
            start_index,
            flags,
            first_output_ref,
            output_ref_count,
        ) = struct.unpack_from(PYCSTB4_SEEDED_WORKLOAD_RECORD_FMT, blob, pos)
        output_ports = port_refs[int(first_output_ref) : int(first_output_ref) + int(output_ref_count)]
        out.append(
            {
                "name": _decode_string_ref(strings, int(name_sid)),
                "generator_id": _decode_string_ref(strings, int(generator_id_sid)),
                "profile": _decode_string_ref(strings, int(profile_sid)),
                "constraints_json": _decode_string_ref(strings, int(constraints_sid)),
                "seed": int(seed),
                "count": int(count),
                "start_index": int(start_index),
                "flags": int(flags),
                "output_ports": output_ports,
                "section_major": int(major),
                "section_minor": int(minor),
            }
        )
    return out, errors


def _decode_instruction_streams(blob: bytes, strings: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    header_size = struct.calcsize(PYCSTB4_INSTRUCTION_STREAM_HEADER_FMT)
    record_size = struct.calcsize(PYCSTB4_INSTRUCTION_STREAM_RECORD_FMT)
    if len(blob) < header_size:
        return [], ["instruction_stream section too small"]
    magic, major, minor, stream_count, _reserved = struct.unpack_from(PYCSTB4_INSTRUCTION_STREAM_HEADER_FMT, blob, 0)
    if magic != b"INST":
        errors.append("instruction_stream magic mismatch")
    out: list[dict[str, Any]] = []
    for idx in range(int(stream_count)):
        pos = header_size + idx * record_size
        if pos + record_size > len(blob):
            errors.append(f"instruction_stream record truncated at {idx}")
            break
        name_sid, isa_sid, encoding_sid, source_sid, instruction_width, flags, count, payload_offset, payload_size = (
            struct.unpack_from(PYCSTB4_INSTRUCTION_STREAM_RECORD_FMT, blob, pos)
        )
        out.append(
            {
                "name": _decode_string_ref(strings, int(name_sid)),
                "isa": _decode_string_ref(strings, int(isa_sid)),
                "encoding": _decode_string_ref(strings, int(encoding_sid)),
                "source": _decode_string_ref(strings, int(source_sid)),
                "instruction_width": int(instruction_width),
                "flags": int(flags),
                "count": int(count),
                "payload_offset": int(payload_offset),
                "payload_size": int(payload_size),
                "section_major": int(major),
                "section_minor": int(minor),
            }
        )
    return out, errors


def _decode_external_stream_sources(blob: bytes, strings: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    header_size = struct.calcsize(PYCSTB4_EXTERNAL_STREAM_HEADER_FMT)
    record_size = struct.calcsize(PYCSTB4_EXTERNAL_STREAM_RECORD_FMT)
    if len(blob) < header_size:
        return [], ["external_stream_source section too small"]
    magic, major, minor, source_count, _reserved = struct.unpack_from(PYCSTB4_EXTERNAL_STREAM_HEADER_FMT, blob, 0)
    if magic != b"XSTR":
        errors.append("external_stream_source magic mismatch")
    out: list[dict[str, Any]] = []
    for idx in range(int(source_count)):
        pos = header_size + idx * record_size
        if pos + record_size > len(blob):
            errors.append(f"external_stream_source record truncated at {idx}")
            break
        name_sid, path_sid, format_sid, hash_sid, offset, byte_size, chunk_size, flags = struct.unpack_from(
            PYCSTB4_EXTERNAL_STREAM_RECORD_FMT, blob, pos
        )
        out.append(
            {
                "name": _decode_string_ref(strings, int(name_sid)),
                "path": _decode_string_ref(strings, int(path_sid)),
                "format": _decode_string_ref(strings, int(format_sid)),
                "hash": _decode_string_ref(strings, int(hash_sid)),
                "offset": int(offset),
                "byte_size": int(byte_size),
                "chunk_size": int(chunk_size),
                "flags": int(flags),
                "section_major": int(major),
                "section_minor": int(minor),
            }
        )
    return out, errors


def _decode_scoreboard_policies(blob: bytes, strings: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    header_size = struct.calcsize(PYCSTB4_SCOREBOARD_POLICY_HEADER_FMT)
    record_size = struct.calcsize(PYCSTB4_SCOREBOARD_POLICY_RECORD_FMT)
    if len(blob) < header_size:
        return [], ["scoreboard_policy section too small"]
    magic, major, minor, policy_count, _reserved = struct.unpack_from(PYCSTB4_SCOREBOARD_POLICY_HEADER_FMT, blob, 0)
    if magic != b"SCBP":
        errors.append("scoreboard_policy magic mismatch")
    out: list[dict[str, Any]] = []
    for idx in range(int(policy_count)):
        pos = header_size + idx * record_size
        if pos + record_size > len(blob):
            errors.append(f"scoreboard_policy record truncated at {idx}")
            break
        name_sid, kind_sid, target_sid, reference_sid, signature_sid, sample_period, max_mismatches, flags = (
            struct.unpack_from(PYCSTB4_SCOREBOARD_POLICY_RECORD_FMT, blob, pos)
        )
        out.append(
            {
                "name": _decode_string_ref(strings, int(name_sid)),
                "kind": _decode_string_ref(strings, int(kind_sid)),
                "target": _decode_string_ref(strings, int(target_sid)),
                "reference": _decode_string_ref(strings, int(reference_sid)),
                "signature": _decode_string_ref(strings, int(signature_sid)),
                "sample_period": int(sample_period),
                "max_mismatches": int(max_mismatches),
                "flags": int(flags),
                "section_major": int(major),
                "section_minor": int(minor),
            }
        )
    return out, errors


def inspect_pycstb4_file(path: str | Path, *, registry: SectionRegistry | None = None) -> dict[str, Any]:
    registry = default_section_registry() if registry is None else registry
    p = Path(path)
    data = p.read_bytes()
    header, errors = _read_exact_header(data)
    warnings: list[str] = []
    sections: list[SectionDirectoryEntry] = []
    if header is None:
        return {
            "path": str(p),
            "size_bytes": len(data),
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "header": None,
            "sections": [],
            "summary": {},
        }
    if header.endian != 1:
        errors.append(f"unsupported endian marker: {header.endian}")
    if header.header_size < PYCSTB4_HEADER_SIZE:
        errors.append(f"header_size too small: {header.header_size}")
    directory_start = header.header_size
    directory_size = header.section_count * PYCSTB4_DIR_SIZE
    directory_end = directory_start + directory_size
    if directory_end > len(data):
        errors.append(
            f"section directory extends past end of file: end={directory_end} file_size={len(data)}"
        )
    else:
        seen_kinds: set[int] = set()
        ranges: list[tuple[int, int, int]] = []
        for idx in range(header.section_count):
            pos = directory_start + idx * PYCSTB4_DIR_SIZE
            kind, flags, _reserved, offset, size, count = struct.unpack_from(PYCSTB4_DIR_FMT, data, pos)
            descriptor = registry.by_kind(kind)
            if kind in seen_kinds:
                errors.append(f"duplicate section kind: {kind}")
            seen_kinds.add(kind)
            end = int(offset) + int(size)
            if end > len(data):
                errors.append(f"section kind={kind} extends past end of file: end={end} file_size={len(data)}")
            if int(offset) < directory_end:
                errors.append(f"section kind={kind} overlaps header/directory: offset={offset}")
            ranges.append((int(offset), end, int(kind)))
            sections.append(
                SectionDirectoryEntry(
                    kind=int(kind),
                    flags=int(flags),
                    offset=int(offset),
                    size=int(size),
                    count=int(count),
                    name=descriptor.name if descriptor is not None else f"unknown_{int(kind)}",
                    known=descriptor is not None,
                    required=bool(descriptor.required) if descriptor is not None else False,
                    experimental=bool(descriptor.experimental) if descriptor is not None else False,
                    deprecated=bool(descriptor.deprecated) if descriptor is not None else False,
                )
            )
        for prev, cur in zip(sorted(ranges), sorted(ranges)[1:]):
            if cur[0] < prev[1]:
                errors.append(f"section payload overlap: kind={prev[2]} and kind={cur[2]}")
        present_kinds = {section.kind for section in sections}
        for required_kind in sorted(registry.required_kinds()):
            if required_kind not in present_kinds:
                descriptor = registry.by_kind(required_kind)
                name = descriptor.name if descriptor is not None else str(required_kind)
                errors.append(f"missing required section: {required_kind} ({name})")
        for section in sections:
            descriptor = registry.by_kind(section.kind)
            if descriptor is None:
                warnings.append(f"unknown section kind={section.kind} will be ignored by framework-level tooling")
                continue
            for dep in descriptor.dependencies:
                if int(dep) not in present_kinds:
                    dep_desc = registry.by_kind(dep)
                    dep_name = dep_desc.name if dep_desc is not None else str(int(dep))
                    errors.append(f"section {section.name} requires missing dependency {int(dep)} ({dep_name})")
    section_by_kind = {section.kind: section for section in sections}
    decoded: dict[str, Any] = {}
    if not errors:
        string_section = section_by_kind.get(int(SectionKind.STRING_TABLE))
        strings: list[str] = []
        if string_section is not None:
            strings, decode_errors = _decode_string_table(_slice_section(data, string_section))
            errors.extend(decode_errors)
            decoded["strings"] = {"count": len(strings), "preview": strings[:16]}
        port_section = section_by_kind.get(int(SectionKind.PORT_TABLE))
        if port_section is not None:
            ports, decode_errors = _decode_ports(_slice_section(data, port_section), port_section.count, strings)
            errors.extend(decode_errors)
            decoded["ports"] = ports
        actor_section = section_by_kind.get(int(SectionKind.ACTOR_BUNDLE))
        if actor_section is not None:
            actor_summary, decode_errors = _decode_actor_bundle_summary(_slice_section(data, actor_section))
            errors.extend(decode_errors)
            decoded["actor_bundle"] = actor_summary
        actor_payload_table_section = section_by_kind.get(int(SectionKind.ACTOR_PAYLOAD_TABLE))
        if actor_payload_table_section is not None:
            tables, decode_errors = _decode_actor_payload_tables(_slice_section(data, actor_payload_table_section))
            errors.extend(decode_errors)
            decoded["actor_payload_tables"] = tables
        instruction_section = section_by_kind.get(int(SectionKind.INSTRUCTION_STREAM))
        if instruction_section is not None:
            streams, decode_errors = _decode_instruction_streams(_slice_section(data, instruction_section), strings)
            errors.extend(decode_errors)
            decoded["instruction_streams"] = streams
        wgen_section = section_by_kind.get(int(SectionKind.SEEDED_WORKLOAD_GENERATOR))
        if wgen_section is not None:
            generators, decode_errors = _decode_seeded_workload_generators(_slice_section(data, wgen_section), strings)
            errors.extend(decode_errors)
            decoded["seeded_workload_generators"] = generators
        external_section = section_by_kind.get(int(SectionKind.EXTERNAL_STREAM_SOURCE))
        if external_section is not None:
            sources, decode_errors = _decode_external_stream_sources(_slice_section(data, external_section), strings)
            errors.extend(decode_errors)
            decoded["external_stream_sources"] = sources
        policy_section = section_by_kind.get(int(SectionKind.SCOREBOARD_POLICY))
        if policy_section is not None:
            policies, decode_errors = _decode_scoreboard_policies(_slice_section(data, policy_section), strings)
            errors.extend(decode_errors)
            decoded["scoreboard_policies"] = policies

    section_dicts = [
        {
            "kind": section.kind,
            "name": section.name,
            "flags": section.flags,
            "offset": section.offset,
            "size": section.size,
            "count": section.count,
            "known": section.known,
            "required": section.required,
            "experimental": section.experimental,
            "deprecated": section.deprecated,
        }
        for section in sections
    ]
    summary = {
        "section_count": len(sections),
        "known_section_count": sum(1 for section in sections if section.known),
        "unknown_section_count": sum(1 for section in sections if not section.known),
        "experimental_section_count": sum(1 for section in sections if section.experimental),
        "total_section_payload_bytes": sum(section.size for section in sections),
        "sections_by_name": {section.name: {"kind": section.kind, "count": section.count, "size": section.size} for section in sections},
    }
    return {
        "path": str(p),
        "size_bytes": len(data),
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "header": {
            "endian": header.endian,
            "header_size": header.header_size,
            "major": header.major,
            "minor": header.minor,
            "patch": header.patch,
            "flags": header.flags,
            "section_count": header.section_count,
            "max_words": header.max_words,
            "max_cycle": header.max_cycle,
            "reset_cycles": header.reset_cycles,
            "reserved": header.reserved,
        },
        "sections": section_dicts,
        "decoded": decoded,
        "summary": summary,
    }


def render_pycstb4_inspect_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"PYCSTB4 file: {report.get('path')}")
    lines.append(f"valid: {bool(report.get('valid'))}")
    lines.append(f"size_bytes: {int(report.get('size_bytes', 0))}")
    header = report.get("header")
    if isinstance(header, dict):
        lines.append(
            "container: "
            f"v{header.get('major')}.{header.get('minor')}.{header.get('patch')} "
            f"sections={header.get('section_count')} "
            f"max_words={header.get('max_words')} "
            f"max_cycle={header.get('max_cycle')}"
        )
    errors = report.get("errors") or []
    warnings = report.get("warnings") or []
    if errors:
        lines.append("errors:")
        for item in errors:
            lines.append(f"  - {item}")
    if warnings:
        lines.append("warnings:")
        for item in warnings:
            lines.append(f"  - {item}")
    lines.append("")
    lines.append("sections:")
    lines.append("  kind  name                         count        size  flags  status")
    for section in report.get("sections", []):
        status: list[str] = []
        if section.get("required"):
            status.append("required")
        if section.get("experimental"):
            status.append("experimental")
        if section.get("deprecated"):
            status.append("deprecated")
        if not section.get("known"):
            status.append("unknown")
        status_text = ",".join(status) if status else "optional"
        lines.append(
            "  "
            f"{int(section.get('kind', 0)):>4}  "
            f"{str(section.get('name', '')):<28} "
            f"{int(section.get('count', 0)):>8}  "
            f"{int(section.get('size', 0)):>10}  "
            f"0x{int(section.get('flags', 0)):04x}  "
            f"{status_text}"
        )
    decoded = report.get("decoded")
    if isinstance(decoded, dict) and decoded:
        lines.append("")
        if isinstance(decoded.get("ports"), list):
            lines.append(f"decoded_ports: {len(decoded['ports'])}")
            for port in decoded["ports"][:8]:
                lines.append(
                    "  "
                    f"id={port.get('id')} "
                    f"name={port.get('name')} "
                    f"dir={port.get('direction')} "
                    f"role={port.get('role')} "
                    f"width={port.get('bit_width')} "
                    f"protocol={port.get('protocol')}"
                )
        if isinstance(decoded.get("actor_bundle"), dict):
            actor = decoded["actor_bundle"]
            lines.append(
                "actor_bundle: "
                f"actors={actor.get('actor_count')} "
                f"scoreboards={actor.get('scoreboard_count')} "
                f"external_sources={actor.get('external_source_count')}"
            )
            for item in actor.get("actors", [])[:4]:
                lines.append(
                    "  "
                    f"actor kind={item.get('kind')} "
                    f"valid={item.get('valid_port')} "
                    f"ready={item.get('ready_port')} "
                    f"payload={item.get('payload_ports')} "
                    f"cycles={item.get('start_cycle')}..{item.get('end_cycle')}"
                )
        if isinstance(decoded.get("actor_payload_tables"), list):
            lines.append(f"actor_payload_tables: {len(decoded['actor_payload_tables'])}")
            for table in decoded["actor_payload_tables"][:4]:
                lines.append(
                    "  "
                    f"ports={table.get('payload_ports')} "
                    f"tx={table.get('transaction_count')} "
                    f"word_count={table.get('word_count')} "
                    f"bytes={table.get('byte_count')}"
                )
        if isinstance(decoded.get("instruction_streams"), list):
            lines.append(f"instruction_streams: {len(decoded['instruction_streams'])}")
            for stream in decoded["instruction_streams"][:4]:
                lines.append(
                    "  "
                    f"name={stream.get('name')} "
                    f"isa={stream.get('isa')} "
                    f"encoding={stream.get('encoding')} "
                    f"count={stream.get('count')} "
                    f"payload_size={stream.get('payload_size')}"
                )
        if isinstance(decoded.get("seeded_workload_generators"), list):
            lines.append(f"seeded_workload_generators: {len(decoded['seeded_workload_generators'])}")
            for generator in decoded["seeded_workload_generators"][:4]:
                lines.append(
                    "  "
                    f"name={generator.get('name')} "
                    f"id={generator.get('generator_id')} "
                    f"count={generator.get('count')} "
                    f"output_ports={generator.get('output_ports')}"
                )
        if isinstance(decoded.get("external_stream_sources"), list):
            lines.append(f"external_stream_sources: {len(decoded['external_stream_sources'])}")
            for source in decoded["external_stream_sources"][:4]:
                lines.append(
                    "  "
                    f"name={source.get('name')} "
                    f"format={source.get('format')} "
                    f"bytes={source.get('byte_size')} "
                    f"chunk={source.get('chunk_size')}"
                )
        if isinstance(decoded.get("scoreboard_policies"), list):
            lines.append(f"scoreboard_policies: {len(decoded['scoreboard_policies'])}")
            for policy in decoded["scoreboard_policies"][:4]:
                lines.append(
                    "  "
                    f"name={policy.get('name')} "
                    f"kind={policy.get('kind')} "
                    f"target={policy.get('target')} "
                    f"sample_period={policy.get('sample_period')}"
                )
    return "\n".join(lines) + "\n"


def pycstb4_report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, indent=2) + "\n"


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, str) and value.startswith(("0x", "0X")):
            return int(value, 16)
        return int(value)
    except (TypeError, ValueError):
        return None


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def verify_schedule_ir_for_pycstb4(schedule_ir: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    ports = _mapping_items(schedule_ir.get("ports"))
    port_ids: set[int] = set()
    port_widths: dict[int, int] = {}
    for port in ports:
        pid = _int_or_none(port.get("id"))
        width = _int_or_none(port.get("bit_width"))
        if pid is None:
            errors.append("port entry missing integer id")
            continue
        if pid in port_ids:
            errors.append(f"duplicate port id: {pid}")
        port_ids.add(pid)
        if width is None or width <= 0:
            errors.append(f"port id={pid} has invalid bit_width: {port.get('bit_width')!r}")
        else:
            port_widths[pid] = width
        if str(port.get("name", "")).strip() == "":
            errors.append(f"port id={pid} has empty name")

    def require_port(section: str, field: str, value: Any) -> int | None:
        pid = _int_or_none(value)
        if pid is None:
            errors.append(f"{section}.{field} is not an integer port id: {value!r}")
            return None
        if pid not in port_ids:
            errors.append(f"{section}.{field} references missing port id: {pid}")
        return pid

    for event in _mapping_items(schedule_ir.get("events")):
        if event.get("port") is not None:
            require_port("event", "port", event.get("port"))
        if _int_or_none(event.get("cycle")) is None:
            errors.append(f"event has invalid cycle: {event.get('cycle')!r}")

    for frame in _mapping_items(schedule_ir.get("frames")):
        if _int_or_none(frame.get("cycle")) is None:
            errors.append(f"frame has invalid cycle: {frame.get('cycle')!r}")
        for item in _mapping_items(frame.get("items")):
            require_port("frame_item", "port", item.get("port"))

    for pattern in _mapping_items(schedule_ir.get("patterns")):
        require_port("pattern", "port", pattern.get("port"))
        if str(pattern.get("kind", "")) == "periodic_drive":
            period = _int_or_none(pattern.get("period"))
            active_cycles = _int_or_none(pattern.get("active_cycles"))
            if period is None or period <= 0:
                errors.append(f"periodic pattern has invalid period: {pattern.get('period')!r}")
            if active_cycles is None or active_cycles < 0:
                errors.append(f"periodic pattern has invalid active_cycles: {pattern.get('active_cycles')!r}")
            if period is not None and active_cycles is not None and active_cycles > period:
                errors.append(f"periodic pattern active_cycles exceeds period: {active_cycles} > {period}")

    for actor in _mapping_items(schedule_ir.get("actors")):
        label = f"actor[{actor.get('name', '')}]"
        require_port(label, "valid_port", actor.get("valid_port"))
        require_port(label, "ready_port", actor.get("ready_port"))
        payload_ports = actor.get("payload_ports", [])
        if not isinstance(payload_ports, list):
            errors.append(f"{label}.payload_ports must be a list")
        else:
            for port in payload_ports:
                require_port(label, "payload_ports", port)
        start = _int_or_none(actor.get("start_cycle", 0))
        end = _int_or_none(actor.get("end_cycle", 0))
        if start is None or end is None or start > end:
            errors.append(f"{label} has invalid cycle range: start={actor.get('start_cycle')} end={actor.get('end_cycle')}")

    for scoreboard in _mapping_items(schedule_ir.get("scoreboards")):
        label = f"scoreboard[{scoreboard.get('name', '')}]"
        payload_ports = scoreboard.get("payload_ports", [])
        if not isinstance(payload_ports, list):
            errors.append(f"{label}.payload_ports must be a list")
        else:
            for port in payload_ports:
                require_port(label, "payload_ports", port)

    for generator in _mapping_items(schedule_ir.get("workload_generators")):
        label = f"workload_generator[{generator.get('name', '')}]"
        if str(generator.get("generator_id", "")).strip() == "":
            errors.append(f"{label} has empty generator_id")
        count = _int_or_none(generator.get("count", 0))
        if count is None or count < 0:
            errors.append(f"{label} has invalid count: {generator.get('count')!r}")
        output_ports = generator.get("output_ports", [])
        if not isinstance(output_ports, list) or not output_ports:
            errors.append(f"{label}.output_ports must be a non-empty list")
        else:
            for port in output_ports:
                require_port(label, "output_ports", port)
        constraints = generator.get("constraints")
        if isinstance(constraints, Mapping):
            data_width = _int_or_none(constraints.get("data_width"))
            if data_width is not None:
                for port in output_ports if isinstance(output_ports, list) else []:
                    pid = _int_or_none(port)
                    if pid in port_widths and port_widths[pid] != data_width:
                        errors.append(
                            f"{label} data_width={data_width} does not match port id={pid} width={port_widths[pid]}"
                        )

    for source in _mapping_items(schedule_ir.get("external_stream_sources")):
        label = f"external_stream_source[{source.get('name', '')}]"
        if str(source.get("path", "")).strip() == "":
            errors.append(f"{label} has empty path")
        if str(source.get("format", "")).strip() == "":
            errors.append(f"{label} has empty format")
        for field in ("offset", "byte_size", "chunk_size"):
            value = _int_or_none(source.get(field, 0))
            if value is None or value < 0:
                errors.append(f"{label} has invalid {field}: {source.get(field)!r}")

    for policy in _mapping_items(schedule_ir.get("scoreboard_policies")):
        label = f"scoreboard_policy[{policy.get('name', '')}]"
        if str(policy.get("kind", "")).strip() == "":
            errors.append(f"{label} has empty kind")

    return errors
