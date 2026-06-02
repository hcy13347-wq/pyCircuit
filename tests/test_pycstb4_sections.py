from __future__ import annotations

from pathlib import Path

from pycircuit.pycstb4_sections import (
    PYCSTB4_MAGIC,
    inspect_pycstb4_file,
    section_registry_manifest,
)
from pycircuit.schedule_ir import schedule_ir_to_pycstb4_bytes


def _ready_valid_schedule_ir(tmp_path: Path) -> dict[str, object]:
    external_path = tmp_path / "payload.raw"
    external_path.write_bytes((0x1234).to_bytes(4, "little"))
    return {
        "schema": "pycircuit.schedule_ir",
        "version": {"major": 1, "minor": 0, "patch": 0},
        "metadata": {
            "case_name": "tb_section_smoke",
            "generator": "pytest",
            "generator_version": "v0",
            "source": "tests/test_pycstb4_sections.py",
        },
        "timebase": {
            "unit": "cycle",
            "max_cycle": 8,
            "reset_cycles": 0,
            "clocking": "none",
        },
        "ports": [
            {"id": 0, "name": "cmd_valid", "direction": "input", "bit_width": 1, "word_count": 1, "role": "valid", "protocol": "cmd"},
            {"id": 1, "name": "cmd_data", "direction": "input", "bit_width": 32, "word_count": 1, "role": "data", "protocol": "cmd"},
            {"id": 2, "name": "cmd_ready", "direction": "output", "bit_width": 1, "word_count": 1, "role": "ready", "protocol": "cmd"},
            {"id": 3, "name": "result_valid", "direction": "output", "bit_width": 1, "word_count": 1, "role": "valid", "protocol": "result"},
            {"id": 4, "name": "result_data", "direction": "output", "bit_width": 32, "word_count": 1, "role": "data", "protocol": "result"},
            {"id": 5, "name": "result_ready", "direction": "input", "bit_width": 1, "word_count": 1, "role": "ready", "protocol": "result"},
        ],
        "events": [
            {"kind": "expect", "cycle": 1, "phase": "post", "port": 3, "value": "0x1", "message": "valid asserted"},
            {"kind": "expect", "cycle": 1, "phase": "post", "port": 4, "value": "0x1234", "message": "payload loopback"},
        ],
        "frames": [
            {
                "kind": "drive_frame",
                "cycle": 1,
                "items": [
                    {"port": 0, "value": "0x1"},
                    {"port": 1, "value": "0x1234"},
                    {"port": 5, "value": "0x1"},
                ],
            }
        ],
        "patterns": [
            {
                "kind": "periodic_drive",
                "port": 5,
                "start_cycle": 2,
                "end_cycle": 8,
                "period": 4,
                "active_cycles": 1,
                "phase_cycle": 0,
                "active_value": "0x0",
                "default_value": "0x1",
            }
        ],
        "actors": [
            {
                "kind": "ready_valid_source",
                "name": "cmd_source",
                "valid_port": 0,
                "ready_port": 2,
                "payload_ports": [1],
                "start_cycle": 1,
                "end_cycle": 8,
                "policy": "hold_valid",
            },
            {
                "kind": "ready_valid_sink",
                "name": "result_sink",
                "valid_port": 3,
                "ready_port": 5,
                "payload_ports": [4],
                "start_cycle": 1,
                "end_cycle": 8,
                "sample_policy": "on_handshake",
                "ready_pattern": {"kind": "constant", "value": "0x1"},
                "scoreboard": "result_scoreboard",
            },
        ],
        "scoreboards": [
            {"kind": "ordered", "name": "result_scoreboard", "payload_ports": [4]},
        ],
        "workload_generators": [
            {
                "name": "cmd_seeded_source",
                "generator_id": "lcg_payload_v0",
                "profile": "synthetic_ready_valid_payload",
                "seed": 0,
                "count": 1,
                "start_index": 0,
                "output_ports": [1],
                "constraints": {"data_width": 32, "multiplier": "0x45d9f3b"},
            }
        ],
        "instruction_streams": [
            {
                "name": "mock_issue",
                "isa": "mock_npu_v0",
                "encoding": "raw_le32_inline",
                "source": "pytest",
                "issue_protocol": "cmd",
                "word_bits": 32,
                "flags": 0,
                "count": 1,
                "instructions": [0x10001],
            }
        ],
        "external_stream_sources": [
            {
                "name": "external_payload",
                "path": str(external_path),
                "format": "mock_raw_u32_le",
                "hash": "",
                "issue_protocol": "cmd",
                "word_bits": 32,
                "count": 1,
                "offset": 0,
                "byte_size": 4,
                "chunk_size": 4096,
                "flags": 0,
            }
        ],
        "scoreboard_policies": [
            {
                "name": "ordered_result_policy",
                "kind": "ordered_payload",
                "target": "result",
                "reference": "cmd",
                "signature": "pass_through_payload_v0",
                "sample_period": 0,
                "max_mismatches": 0,
                "flags": 0,
            }
        ],
    }


def test_section_registry_manifest_exposes_runtime_sections() -> None:
    manifest = section_registry_manifest()
    sections = {item["name"]: item for item in manifest["sections"]}

    assert manifest["schema"] == "pycircuit.pycstb4.section_registry"
    assert sections["string_table"]["required"] is True
    assert sections["port_table"]["required"] is True
    assert sections["actor_bundle"]["experimental"] is True
    assert sections["seeded_workload_generator"]["experimental"] is True
    assert sections["external_stream_source"]["experimental"] is True


def test_schedule_ir_round_trips_through_pycstb4_container(tmp_path: Path) -> None:
    blob = schedule_ir_to_pycstb4_bytes(_ready_valid_schedule_ir(tmp_path))
    out = tmp_path / "tb.schedule.pycstb4"
    out.write_bytes(blob)

    report = inspect_pycstb4_file(out)
    sections = report["summary"]["sections_by_name"]

    assert blob.startswith(PYCSTB4_MAGIC)
    assert report["valid"] is True
    assert report["errors"] == []
    assert sections["port_table"]["count"] == 6
    assert sections["event_table"]["count"] == 2
    assert sections["frame_table"]["count"] == 1
    assert sections["actor_bundle"]["count"] == 2
    assert sections["seeded_workload_generator"]["count"] == 1
    assert sections["external_stream_source"]["count"] == 1

    decoded = report["decoded"]
    assert decoded["ports"][1]["name"] == "cmd_data"
    assert decoded["actor_bundle"]["actor_count"] == 2
    assert decoded["seeded_workload_generators"][0]["generator_id"] == "lcg_payload_v0"
    assert decoded["external_stream_sources"][0]["format"] == "mock_raw_u32_le"

