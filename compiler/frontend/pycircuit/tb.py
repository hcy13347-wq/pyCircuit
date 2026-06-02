from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


class TbError(RuntimeError):
    pass


def _sanitize_id(s: str) -> str:
    out: list[str] = []
    for c in str(s):
        if ("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9") or (c == "_"):
            out.append(c)
        else:
            out.append("_")
    if not out or ("0" <= out[0] <= "9"):
        out.insert(0, "_")
    return "".join(out)


def _unique_names(raw: Iterable[str]) -> list[str]:
    used: dict[str, int] = {}
    out: list[str] = []
    for r in raw:
        base = _sanitize_id(r)
        n = used.get(base, 0) + 1
        used[base] = n
        out.append(base if n == 1 else f"{base}_{n}")
    return out


class SvaExpr:
    def __init__(self, text: str) -> None:
        t = str(text).strip()
        if not t:
            raise TbError("SVA expression must be non-empty")
        self.text = t

    def __str__(self) -> str:
        return self.text

    def _bin(self, op: str, other: Any) -> "SvaExpr":
        o = _as_sva_expr(other)
        return SvaExpr(f"({self}) {op} ({o})")

    def __and__(self, other: Any) -> "SvaExpr":
        return self._bin("&&", other)

    def __or__(self, other: Any) -> "SvaExpr":
        return self._bin("||", other)

    def __add__(self, other: Any) -> "SvaExpr":
        return self._bin("+", other)

    def __sub__(self, other: Any) -> "SvaExpr":
        return self._bin("-", other)

    def __invert__(self) -> "SvaExpr":
        return SvaExpr(f"!({self})")

    def __eq__(self, other: Any) -> "SvaExpr":  # type: ignore[override]
        return self._bin("==", other)

    def __ne__(self, other: Any) -> "SvaExpr":  # type: ignore[override]
        return self._bin("!=", other)

    def __lt__(self, other: Any) -> "SvaExpr":
        return self._bin("<", other)

    def __le__(self, other: Any) -> "SvaExpr":
        return self._bin("<=", other)

    def __gt__(self, other: Any) -> "SvaExpr":
        return self._bin(">", other)

    def __ge__(self, other: Any) -> "SvaExpr":
        return self._bin(">=", other)


def _as_sva_expr(v: Any) -> SvaExpr:
    if isinstance(v, SvaExpr):
        return v
    if isinstance(v, str):
        return SvaExpr(_sanitize_id(v))
    if isinstance(v, bool):
        return SvaExpr("1" if v else "0")
    if isinstance(v, int):
        return SvaExpr(str(int(v)))
    raise TbError(f"unsupported SVA value: {type(v).__name__}")


class sva:
    @staticmethod
    def id(name: str) -> SvaExpr:
        return _as_sva_expr(name)

    @staticmethod
    def past(sig: Any, n: int = 1) -> SvaExpr:
        if int(n) < 1:
            raise TbError("sva.past(n) requires n >= 1")
        s = _as_sva_expr(sig)
        return SvaExpr(f"$past({s}, {int(n)})")

    @staticmethod
    def rose(sig: Any) -> SvaExpr:
        s = _as_sva_expr(sig)
        return SvaExpr(f"$rose({s})")

    @staticmethod
    def fell(sig: Any) -> SvaExpr:
        s = _as_sva_expr(sig)
        return SvaExpr(f"$fell({s})")

    @staticmethod
    def stable(sig: Any) -> SvaExpr:
        s = _as_sva_expr(sig)
        return SvaExpr(f"$stable({s})")


@dataclass(frozen=True)
class ClockSpec:
    port: str
    half_period_steps: int = 1
    phase_steps: int = 0
    start_high: bool = False


@dataclass(frozen=True)
class ResetSpec:
    port: str
    cycles_asserted: int = 2
    cycles_deasserted: int = 1


@dataclass(frozen=True)
class Drive:
    port: str
    value: int | bool
    at: int


@dataclass(frozen=True)
class Expect:
    port: str
    value: int | bool
    at: int
    phase: str = "post"
    msg: str | None = None


@dataclass(frozen=True)
class SvaAssert:
    expr: SvaExpr
    clock: str
    reset: str | None = None
    name: str | None = None
    msg: str | None = None


@dataclass(frozen=True)
class RandomStream:
    port: str
    seed: int = 1
    start: int = 0
    every: int = 1


@dataclass(frozen=True)
class PrintAction:
    fmt: str
    ports: tuple[str, ...] = ()
    at: int | None = None
    start: int | None = None
    every: int | None = None


@dataclass(frozen=True)
class GeneratedReadyValidWorkload:
    source_valid: str
    source_payload: str
    source_ready: str
    sink_valid: str
    sink_payload: str
    sink_ready: str
    count: int
    data_width: int
    start_cycle: int = 1
    generator_id: str = "lcg_payload_v0"
    seed: int = 0
    multiplier: int = 0x45D9F3B
    ready_period: int = 0
    ready_stall: int = 0


@dataclass(frozen=True)
class InstructionStreamSpec:
    name: str
    words: tuple[int, ...]
    word_bits: int = 32
    isa: str = "raw"
    encoding: str = "raw_le32_inline"
    source: str = ""
    issue_protocol: str = "cmd"
    flags: int = 0


@dataclass(frozen=True)
class ExternalWorkloadSpec:
    name: str
    path: str
    format: str
    word_bits: int = 32
    count: int = 0
    chunk_size: int = 4096
    sha256: str = ""
    issue_protocol: str = "cmd"
    offset: int = 0
    byte_size: int = 0
    flags: int = 0


@dataclass(frozen=True)
class ScoreboardPolicySpec:
    name: str
    policy: str
    target: str = ""
    reference: str = ""
    signature: str = ""
    sample_period: int = 0
    max_mismatches: int = 0
    flags: int = 0


class ReadyValidSourceBuilder:
    def __init__(self, tb: "Tb", *, name: str, valid: str, ready: str, payload: str) -> None:
        self._tb = tb
        self.name = str(name)
        self.valid = str(valid)
        self.ready = str(ready)
        self.payload = str(payload)
        self._transactions: list[int] = []

    def send(self, value: int | bool) -> None:
        if not isinstance(value, (bool, int)):
            raise TbError("ready_valid_source.send value must be bool or int")
        self._transactions.append(int(value))

    @property
    def transactions(self) -> tuple[int, ...]:
        return tuple(self._transactions)


class ReadyValidSinkBuilder:
    def __init__(self, tb: "Tb", *, name: str, valid: str, ready: str, payload: str) -> None:
        self._tb = tb
        self.name = str(name)
        self.valid = str(valid)
        self.ready = str(ready)
        self.payload = str(payload)
        self._expected: list[int] = []
        self.ready_period = 0
        self.ready_stall = 0

    def expect(self, value: int | bool) -> None:
        if not isinstance(value, (bool, int)):
            raise TbError("ready_valid_sink.expect value must be bool or int")
        self._expected.append(int(value))

    def backpressure(self, *, period: int, stall: int) -> None:
        p = int(period)
        s = int(stall)
        if p < 0 or s < 0 or (p == 0 and s != 0) or (p > 0 and s >= p):
            raise TbError("ready_valid_sink.backpressure requires period=0,stall=0 or 0 <= stall < period")
        self.ready_period = p
        self.ready_stall = s

    @property
    def expected(self) -> tuple[int, ...]:
        return tuple(self._expected)


@dataclass
class Tb:
    """A tiny, cycle-based testbench description (prototype).

    This builder is intentionally backend-neutral: it can be rendered into a
    C++ testbench (fast) or a SystemVerilog testbench with SVA.
    """

    clocks: list[ClockSpec] = field(default_factory=list)
    reset_spec: ResetSpec | None = None
    drives: list[Drive] = field(default_factory=list)
    expects: list[Expect] = field(default_factory=list)
    sva_asserts: list[SvaAssert] = field(default_factory=list)
    random_streams: list[RandomStream] = field(default_factory=list)
    prints: list[PrintAction] = field(default_factory=list)
    generated_ready_valid_workloads: list[GeneratedReadyValidWorkload] = field(default_factory=list)
    ready_valid_sources: list[ReadyValidSourceBuilder] = field(default_factory=list)
    ready_valid_sinks: list[ReadyValidSinkBuilder] = field(default_factory=list)
    instruction_streams: list[InstructionStreamSpec] = field(default_factory=list)
    external_workloads: list[ExternalWorkloadSpec] = field(default_factory=list)
    scoreboard_policies: list[ScoreboardPolicySpec] = field(default_factory=list)

    timeout_cycles: int = 1000
    finish_cycle: int | None = None

    def clock(self, port: str, *, half_period_steps: int = 1, phase_steps: int = 0, start_high: bool = False) -> None:
        p = str(port).strip()
        if not p:
            raise TbError("clock port must be non-empty")
        hp = int(half_period_steps)
        if hp <= 0:
            raise TbError("half_period_steps must be > 0")
        self.clocks.append(ClockSpec(port=p, half_period_steps=hp, phase_steps=int(phase_steps), start_high=bool(start_high)))

    def reset(self, port: str, *, cycles_asserted: int = 2, cycles_deasserted: int = 1) -> None:
        p = str(port).strip()
        if not p:
            raise TbError("reset port must be non-empty")
        ca = int(cycles_asserted)
        cd = int(cycles_deasserted)
        if ca < 0 or cd < 0:
            raise TbError("reset cycles must be >= 0")
        self.reset_spec = ResetSpec(port=p, cycles_asserted=ca, cycles_deasserted=cd)

    def drive(self, port: str, value: int | bool, *, at: int) -> None:
        p = str(port).strip()
        if not p:
            raise TbError("drive port must be non-empty")
        cyc = int(at)
        if cyc < 0:
            raise TbError("drive cycle must be >= 0")
        if not isinstance(value, (bool, int)):
            raise TbError("drive value must be bool or int")
        self.drives.append(Drive(port=p, value=value, at=cyc))

    def expect(
        self,
        port: str,
        value: int | bool,
        *,
        at: int,
        phase: str = "post",
        msg: str | None = None,
    ) -> None:
        p = str(port).strip()
        if not p:
            raise TbError("expect port must be non-empty")
        cyc = int(at)
        if cyc < 0:
            raise TbError("expect cycle must be >= 0")
        if not isinstance(value, (bool, int)):
            raise TbError("expect value must be bool or int")
        ph = str(phase).strip().lower()
        if ph not in {"pre", "post"}:
            raise TbError("expect phase must be 'pre' or 'post'")
        self.expects.append(Expect(port=p, value=value, at=cyc, phase=ph, msg=(None if msg is None else str(msg))))

    def timeout(self, cycles: int) -> None:
        t = int(cycles)
        if t <= 0:
            raise TbError("timeout cycles must be > 0")
        self.timeout_cycles = t

    def finish(self, *, at: int) -> None:
        cyc = int(at)
        if cyc < 0:
            raise TbError("finish cycle must be >= 0")
        self.finish_cycle = cyc

    def sva_assert(
        self,
        expr: Any,
        *,
        clock: str,
        reset: str | None = None,
        name: str | None = None,
        msg: str | None = None,
    ) -> None:
        e = _as_sva_expr(expr)
        clk = str(clock).strip()
        if not clk:
            raise TbError("sva_assert clock must be non-empty")
        rst = None if reset is None else str(reset).strip()
        if rst == "":
            rst = None
        nm = None if name is None else _sanitize_id(str(name))
        if nm == "":
            nm = None
        self.sva_asserts.append(SvaAssert(expr=e, clock=clk, reset=rst, name=nm, msg=(None if msg is None else str(msg))))

    def random(self, port: str, *, seed: int = 1, start: int = 0, every: int = 1) -> None:
        """Drive an input port with a deterministic pseudo-random stream.

        Notes:
        - The stream is rendered in both the generated C++ and SV testbenches.
        - Random drives are applied before any explicit `drive(...)` calls in the
          same cycle (explicit drives override random).
        """

        p = str(port).strip()
        if not p:
            raise TbError("random port must be non-empty")
        st = int(start)
        if st < 0:
            raise TbError("random start cycle must be >= 0")
        ev = int(every)
        if ev <= 0:
            raise TbError("random every must be > 0")
        self.random_streams.append(RandomStream(port=p, seed=int(seed), start=st, every=ev))

    def generated_ready_valid(
        self,
        *,
        source_valid: str,
        source_payload: str,
        source_ready: str,
        sink_valid: str,
        sink_payload: str,
        sink_ready: str,
        count: int,
        data_width: int,
        start_cycle: int = 1,
        generator_id: str = "lcg_payload_v0",
        seed: int = 0,
        multiplier: int = 0x45D9F3B,
        ready_period: int = 0,
        ready_stall: int = 0,
    ) -> None:
        """Attach an experimental generated ready-valid workload.

        This is software testbench metadata, not hardware. It lets scalable
        testbenches describe long protocol workloads without expanding every
        cycle into `drive` and `expect` rows.
        """

        names = [source_valid, source_payload, source_ready, sink_valid, sink_payload, sink_ready]
        if any(not str(name).strip() for name in names):
            raise TbError("generated_ready_valid ports must be non-empty")
        n = int(count)
        if n < 0:
            raise TbError("generated_ready_valid count must be >= 0")
        width = int(data_width)
        if width <= 0 or width > 64:
            raise TbError("generated_ready_valid data_width must be in 1..64")
        start = int(start_cycle)
        if start < 0:
            raise TbError("generated_ready_valid start_cycle must be >= 0")
        period = int(ready_period)
        stall = int(ready_stall)
        if period < 0 or stall < 0 or (period == 0 and stall != 0) or (period > 0 and stall >= period):
            raise TbError("generated_ready_valid ready pattern requires period=0,stall=0 or 0 <= stall < period")
        self.generated_ready_valid_workloads.append(
            GeneratedReadyValidWorkload(
                source_valid=str(source_valid).strip(),
                source_payload=str(source_payload).strip(),
                source_ready=str(source_ready).strip(),
                sink_valid=str(sink_valid).strip(),
                sink_payload=str(sink_payload).strip(),
                sink_ready=str(sink_ready).strip(),
                count=n,
                data_width=width,
                start_cycle=start,
                generator_id=str(generator_id),
                seed=int(seed),
                multiplier=int(multiplier),
                ready_period=period,
                ready_stall=stall,
            )
        )

    def ready_valid_source(self, *, name: str, valid: str, ready: str, payload: str) -> ReadyValidSourceBuilder:
        if not str(name).strip():
            raise TbError("ready_valid_source name must be non-empty")
        builder = ReadyValidSourceBuilder(self, name=name, valid=valid, ready=ready, payload=payload)
        self.ready_valid_sources.append(builder)
        return builder

    def ready_valid_sink(self, *, name: str, valid: str, ready: str, payload: str) -> ReadyValidSinkBuilder:
        if not str(name).strip():
            raise TbError("ready_valid_sink name must be non-empty")
        builder = ReadyValidSinkBuilder(self, name=name, valid=valid, ready=ready, payload=payload)
        self.ready_valid_sinks.append(builder)
        return builder

    def instruction_stream(
        self,
        *,
        name: str,
        words: Iterable[int],
        word_bits: int = 32,
        isa: str = "raw",
        encoding: str = "raw_le32_inline",
        source: str = "",
        issue_protocol: str = "cmd",
        flags: int = 0,
    ) -> None:
        if not str(name).strip():
            raise TbError("instruction_stream name must be non-empty")
        width = int(word_bits)
        if width <= 0 or width > 64:
            raise TbError("instruction_stream word_bits must be in 1..64")
        self.instruction_streams.append(
            InstructionStreamSpec(
                name=str(name).strip(),
                words=tuple(int(word) for word in words),
                word_bits=width,
                isa=str(isa),
                encoding=str(encoding),
                source=str(source),
                issue_protocol=str(issue_protocol),
                flags=int(flags),
            )
        )

    def external_workload(
        self,
        *,
        name: str,
        path: str,
        format: str,
        word_bits: int = 32,
        count: int = 0,
        chunk_size: int = 4096,
        sha256: str = "",
        issue_protocol: str = "cmd",
        offset: int = 0,
        byte_size: int = 0,
        flags: int = 0,
    ) -> None:
        if not str(name).strip():
            raise TbError("external_workload name must be non-empty")
        if not str(path).strip():
            raise TbError("external_workload path must be non-empty")
        if not str(format).strip():
            raise TbError("external_workload format must be non-empty")
        width = int(word_bits)
        if width <= 0 or width > 64:
            raise TbError("external_workload word_bits must be in 1..64")
        n = int(count)
        if n < 0:
            raise TbError("external_workload count must be >= 0")
        chunk = int(chunk_size)
        if chunk <= 0:
            raise TbError("external_workload chunk_size must be > 0")
        off = int(offset)
        size = int(byte_size)
        if off < 0 or size < 0:
            raise TbError("external_workload offset and byte_size must be >= 0")
        self.external_workloads.append(
            ExternalWorkloadSpec(
                name=str(name).strip(),
                path=str(path).strip(),
                format=str(format).strip(),
                word_bits=width,
                count=n,
                chunk_size=chunk,
                sha256=str(sha256),
                issue_protocol=str(issue_protocol),
                offset=off,
                byte_size=size,
                flags=int(flags),
            )
        )

    def expect_policy(
        self,
        *,
        name: str,
        policy: str,
        target: str = "",
        reference: str = "",
        signature: str = "",
        sample_period: int = 0,
        max_mismatches: int = 0,
        flags: int = 0,
    ) -> None:
        if not str(name).strip():
            raise TbError("expect_policy name must be non-empty")
        if not str(policy).strip():
            raise TbError("expect_policy policy must be non-empty")
        sample = int(sample_period)
        mismatches = int(max_mismatches)
        if sample < 0 or mismatches < 0:
            raise TbError("expect_policy sample_period and max_mismatches must be >= 0")
        self.scoreboard_policies.append(
            ScoreboardPolicySpec(
                name=str(name).strip(),
                policy=str(policy).strip(),
                target=str(target),
                reference=str(reference),
                signature=str(signature),
                sample_period=sample,
                max_mismatches=mismatches,
                flags=int(flags),
            )
        )

    def print(self, fmt: str, *, at: int, ports: Iterable[str] = ()) -> None:
        s = str(fmt)
        if not s.strip():
            raise TbError("print fmt must be non-empty")
        cyc = int(at)
        if cyc < 0:
            raise TbError("print cycle must be >= 0")
        ps = tuple(str(p).strip() for p in ports)
        if any(not p for p in ps):
            raise TbError("print ports must be non-empty names")
        self.prints.append(PrintAction(fmt=s, ports=ps, at=cyc))

    def print_every(self, fmt: str, *, start: int = 0, every: int = 1, ports: Iterable[str] = ()) -> None:
        s = str(fmt)
        if not s.strip():
            raise TbError("print_every fmt must be non-empty")
        st = int(start)
        if st < 0:
            raise TbError("print_every start must be >= 0")
        ev = int(every)
        if ev <= 0:
            raise TbError("print_every every must be > 0")
        ps = tuple(str(p).strip() for p in ports)
        if any(not p for p in ps):
            raise TbError("print_every ports must be non-empty names")
        self.prints.append(PrintAction(fmt=s, ports=ps, start=st, every=ev))
