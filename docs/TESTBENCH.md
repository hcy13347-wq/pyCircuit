# Testbench

`@testbench` lets pyCircuit keep host/device simulation intent in the same frontend flow:
- frontend emits a TB `.pyc` payload (JSON encoded in module attrs)
- backend (`pycc`) lowers that payload to C++ or SystemVerilog testbench text

Observation points (pyc4.0):

- `phase="pre"` samples at **TICK-OBS** (after combinational settle, before state commit).
- `phase="post"` samples at **XFER-OBS** (after state commit).

## Authoring

Write a module `build` and a decorated testbench:

```python
from pycircuit import Circuit, Tb, module, testbench

@module
def build(m: Circuit):
    ...

@testbench
def tb(t: Tb):
    t.clock("clk")
    t.reset("rst", cycles_asserted=2, cycles_deasserted=1)
    t.timeout(100)
    t.drive("in_valid", 0, at=0)
    t.expect("out_valid", 0, at=0, phase="pre")
    t.finish(at=10)
```

`pycircuit build` expects `tb` to be decorated with `@testbench`.

## Tb API (selected)

- `t.clock(port, half_period_steps=..., phase_steps=..., start_high=...)`
- `t.reset(port, cycles_asserted=..., cycles_deasserted=...)`
- `t.drive(port, value, at=cycle)`
- `t.expect(port, value, at=cycle, phase="pre"|"post", msg=None)`
- `t.timeout(cycles)`
- `t.finish(at=cycle)`
- `t.print(fmt, at=cycle, ports=[...])`
- `t.print_every(fmt, start=0, every=1, ports=[...])`
- `t.sva_assert(expr, clock=..., reset=..., name=..., msg=...)`

## Experimental scalable testbench APIs

Large protocol tests can avoid expanding every cycle into inline C++ by using
runtime-loop or actor-fastpath schedules.

Build modes:

- `--tb-schedule-mode inline`: emit the original inline C++ testbench.
- `--tb-schedule-mode runtime-loop`: emit a small runner and external schedule sidecars.
- `--tb-schedule-mode actor-fastpath`: emit a ready-valid transaction runner.
- `--tb-schedule-format pycstb4`: use the experimental PYCSTB4 container for runtime-loop schedules.

Ready-valid transaction DSL:

```python
src = t.ready_valid_source(name="cmd_source", valid="cmd_valid", ready="cmd_ready", payload="cmd_data")
sink = t.ready_valid_sink(name="result_sink", valid="result_valid", ready="result_ready", payload="result_data")

src.send(0x1234)
sink.expect(0x1234)
sink.backpressure(period=8, stall=2)
```

Generated ready-valid workload:

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
)
```

Workload/checker metadata:

- `t.instruction_stream(...)`: inline instruction/raw-word stream metadata.
- `t.external_workload(...)`: external trace/workload file metadata.
- `t.expect_policy(...)`: checker policy metadata.

These APIs are software testbench metadata. They do not describe synthesizable
hardware. See `docs/PYCSTB4_RUNTIME_LOOP.md` for section layout, CLI examples,
environment switches, and current limitations.
