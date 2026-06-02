# PYCSTB4 runtime-loop and actor-fastpath

This page documents the experimental scalable testbench path added for large
cycle-count simulations.

The original C++ testbench renderer emits `drive()` and `expect()` calls inline.
That is easy to inspect, but it scales poorly when a testbench contains many
cycles or many repeated protocol events. The runtime-loop path moves schedule
data out of generated C++ and into a sidecar container, so generated C++ stays
small and executes the schedule at runtime.

## Modes

| Mode | CLI | Schedule data | Intended use |
|---|---|---|---|
| Inline | `--tb-schedule-mode inline` | C++ source code | Small directed tests and debug-friendly cases. |
| Runtime loop | `--tb-schedule-mode runtime-loop` | `.json`, `.bin`, and optional `.pycstb4` sidecars | Long cycle-level `drive` / `expect` tests. |
| Actor fastpath | `--tb-schedule-mode actor-fastpath` | PACTR0 and `.schedule.pycstb4` sidecars | Ready-valid transaction workloads. |

`runtime-loop` defaults to the legacy binary sidecar format. Use
`--tb-schedule-format pycstb4` to generate and consume the PYCSTB4 container.

## PYCSTB4 container

PYCSTB4 is a sectioned binary container for testbench schedules. The current
schema is experimental and intentionally versioned by section. The required
sections are:

| Section | Purpose |
|---|---|
| `string_table` | Shared string pool for section metadata. |
| `port_table` | DUT port ids, names, direction, width, role, and protocol. |
| `event_table` | Cycle-level expect/sample events. |
| `frame_table` | Cycle-level drive frames. |

The optional experimental sections are:

| Section | Purpose |
|---|---|
| `pattern_table` | Compact periodic drive/backpressure patterns. |
| `actor_bundle` | Ready-valid source/sink actors and scoreboard bindings. |
| `actor_payload_blob` | Legacy materialized actor payload bytes. |
| `actor_payload_table` | Structured materialized transaction payload table. |
| `instruction_stream` | Inline instruction/raw-word stream metadata. |
| `seeded_workload_generator` | Deterministic generated workload metadata. |
| `external_stream_source` | External trace/workload file reference metadata. |
| `scoreboard_policy` | Checker policy metadata. |

## Inspecting sidecars

Use the `pycstb4` subcommand to inspect generated files:

```bash
pycircuit pycstb4 inspect path/to/tb.schedule.pycstb4 --strict
pycircuit pycstb4 stats path/to/tb.schedule.pycstb4 --strict
pycircuit pycstb4 dump-json path/to/tb.schedule.pycstb4 --strict
pycircuit pycstb4 registry
```

## Ready-valid actor DSL

Actor-fastpath supports explicit ready-valid transaction testbenches:

```python
src = t.ready_valid_source(
    name="cmd_source",
    valid="cmd_valid",
    ready="cmd_ready",
    payload="cmd_data",
)
sink = t.ready_valid_sink(
    name="result_sink",
    valid="result_valid",
    ready="result_ready",
    payload="result_data",
)

for value in payloads:
    src.send(value)
    sink.expect(value)

sink.backpressure(period=8, stall=2)
```

It also supports seeded workload metadata:

```python
t.generated_ready_valid(
    source_valid="cmd_valid",
    source_payload="cmd_data",
    source_ready="cmd_ready",
    sink_valid="result_valid",
    sink_payload="result_data",
    sink_ready="result_ready",
    count=100_000,
    data_width=32,
    generator_id="lcg_payload_v0",
    seed=0,
    multiplier=0x45D9F3B,
    ready_period=8,
    ready_stall=2,
)
```

## Workload sections

The section APIs are software testbench metadata. They are not hardware
constructs.

```python
t.instruction_stream(
    name="issue_stream",
    isa="mock_npu_v0",
    encoding="raw_le32_inline",
    source="unit-test",
    issue_protocol="cmd",
    word_bits=32,
    words=[0x10001, 0x10002],
)

t.external_workload(
    name="external_payloads",
    path="/path/to/payload.raw",
    format="mock_raw_u32_le",
    word_bits=32,
    count=1024,
    chunk_size=4096,
    issue_protocol="cmd",
    byte_size=4096,
)

t.expect_policy(
    name="ordered_result_policy",
    policy="ordered_payload",
    target="result",
    reference="cmd",
    signature="pass_through_payload_v0",
)
```

## Environment switches

The current implementation keeps some experimental execution choices behind
environment variables:

| Variable | Effect |
|---|---|
| `PYC_TB_PYCSTB4_CHECK=1` | Load and sanity-check the runtime-loop PYCSTB4 sidecar. |
| `PYC_TB_ACTOR_PYCSTB4_CHECK=1` | Load and sanity-check the actor-fastpath PYCSTB4 sidecar. |
| `PYC_TB_ACTOR_SECTION_SUMMARY=1` | Print runtime section summary. |
| `PYC_TB_ACTOR_FROM_PYCSTB4=1` | Use actor payload tables from `.schedule.pycstb4`. |
| `PYC_TB_ACTOR_FROM_WGEN=1` | Use seeded workload generator section metadata. |
| `PYC_TB_ACTOR_FROM_EXTERNAL=1` | Use supported external stream source metadata as transaction source. |

For upstream stabilization, these switches should either become explicit CLI
options or remain clearly marked as experimental.

## Current limitations

- Actor-fastpath v0 supports one ready-valid source and one ready-valid sink.
- The generated actor runner supports one payload word per transaction.
- `external_workload` execution currently supports raw little-endian u32 payload streams only.
- Tagged, masked, out-of-order, and multi-channel scoreboards are not stable.
- Runtime-loop and actor-fastpath are experimental and should not replace inline mode for small debug testbenches.

## PR readiness checklist

Before opening a non-draft PR, keep the change split small:

- Runtime-loop schedule sidecar support should be separated from actor-fastpath.
- PYCSTB4 container inspection/verification should be separately reviewable.
- Actor-fastpath ready-valid workload execution should be submitted after the container and runtime-loop path have landed.
- In-tree tests should pass with `PYTHONPATH=compiler/frontend:. python -m pytest`.

