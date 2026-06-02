from __future__ import annotations

import pytest

from pycircuit import Tb
from pycircuit.tb import TbError


def test_ready_valid_dsl_records_transactions_and_backpressure() -> None:
    t = Tb()
    src = t.ready_valid_source(name="cmd_source", valid="cmd_valid", ready="cmd_ready", payload="cmd_data")
    sink = t.ready_valid_sink(name="result_sink", valid="result_valid", ready="result_ready", payload="result_data")

    src.send(0x1234)
    src.send(0x5678)
    sink.expect(0x1234)
    sink.expect(0x5678)
    sink.backpressure(period=8, stall=2)

    assert src.transactions == (0x1234, 0x5678)
    assert sink.expected == (0x1234, 0x5678)
    assert sink.ready_period == 8
    assert sink.ready_stall == 2


def test_generated_ready_valid_records_seeded_workload_metadata() -> None:
    t = Tb()
    t.generated_ready_valid(
        source_valid="cmd_valid",
        source_payload="cmd_data",
        source_ready="cmd_ready",
        sink_valid="result_valid",
        sink_payload="result_data",
        sink_ready="result_ready",
        count=1024,
        data_width=32,
        seed=7,
        multiplier=0x45D9F3B,
        ready_period=16,
        ready_stall=3,
    )

    workload = t.generated_ready_valid_workloads[0]
    assert workload.count == 1024
    assert workload.data_width == 32
    assert workload.generator_id == "lcg_payload_v0"
    assert workload.seed == 7
    assert workload.ready_period == 16
    assert workload.ready_stall == 3


def test_workload_section_metadata_apis_record_external_and_checker_specs() -> None:
    t = Tb()
    t.instruction_stream(
        name="issue_stream",
        isa="mock_npu_v0",
        encoding="raw_le32_inline",
        source="pytest",
        issue_protocol="cmd",
        word_bits=32,
        words=[0x10001, 0x10002],
    )
    t.external_workload(
        name="external_trace",
        path="/tmp/external_trace.raw",
        format="mock_raw_u32_le",
        word_bits=32,
        count=2,
        chunk_size=4096,
        issue_protocol="cmd",
        byte_size=8,
    )
    t.expect_policy(
        name="ordered_result_policy",
        policy="ordered_payload",
        target="result",
        reference="cmd",
        signature="pass_through_payload_v0",
    )

    assert t.instruction_streams[0].words == (0x10001, 0x10002)
    assert t.external_workloads[0].format == "mock_raw_u32_le"
    assert t.external_workloads[0].byte_size == 8
    assert t.scoreboard_policies[0].policy == "ordered_payload"


@pytest.mark.parametrize(
    "period,stall",
    [
        (0, 1),
        (4, 4),
        (4, -1),
    ],
)
def test_ready_valid_backpressure_rejects_invalid_patterns(period: int, stall: int) -> None:
    t = Tb()
    sink = t.ready_valid_sink(name="sink", valid="valid", ready="ready", payload="payload")
    with pytest.raises(TbError):
        sink.backpressure(period=period, stall=stall)

