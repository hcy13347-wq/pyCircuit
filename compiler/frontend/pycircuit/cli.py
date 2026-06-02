from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping

from .api_contract import collect_local_python_graph, nearest_project_root, scan_file
from .diagnostics import render_diagnostic
from .dsl import Module
from .design import FRONTEND_CONTRACT, Design, DesignError, value_params_of
from .jit import JitError, compile
from .packaged_toolchain import bundled_toolchain_root, tool_executable
from .probe import (
    ProbeError,
    TbProbes,
    build_resolved_probe_manifest,
    collect_probe_functions,
    load_probe_catalog,
    resolve_probe_function,
)
from .pycstb4_sections import (
    default_section_registry,
    inspect_pycstb4_file,
    pycstb4_report_json,
    render_pycstb4_inspect_text,
    section_registry_manifest,
)
from .tb import Tb, TbError, _sanitize_id
from .testbench import emit_testbench_pyc, testbench_payload_from_tb
from .trace_dsl import (
    TraceConfigError,
    TracePlan,
    compute_trace_plan,
    compute_trace_plan_from_artifacts,
    load_trace_config,
)


def _default_top_name(src: Path) -> str:
    parts = [p for p in src.stem.replace("-", "_").split("_") if p]
    if not parts:
        return "Top"
    return "".join(p[:1].upper() + p[1:] for p in parts)


def _tool_script(name: str) -> Path:
    candidates = [
        Path(__file__).resolve().parent / "_tools" / name,
        Path(__file__).resolve().parents[3] / "flows" / "tools" / name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit(f"required pyCircuit helper script not found: {name}")


def _load_py_file(path: Path) -> object:
    path = path.resolve()
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to import {path}")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def _resolve_emit_source(src_arg: str) -> tuple[Path | None, object]:
    if "." in src_arg and not Path(src_arg).exists():
        spec = importlib.util.find_spec(src_arg)
        src: Path | None = None
        if spec is not None and isinstance(spec.origin, str) and spec.origin.endswith(".py"):
            src = Path(spec.origin).resolve()
        mod = importlib.import_module(src_arg)
        return src, mod
    src = Path(src_arg).resolve()
    return src, _load_py_file(src)


def _scan_api_contract(entry: Path, *, project_root_override: str | None = None) -> None:
    if not entry.is_file():
        return
    root = Path(project_root_override).resolve() if project_root_override else nearest_project_root(entry)
    files = collect_local_python_graph(entry.resolve(), project_root=root)
    diags = []
    for f in files:
        diags.extend(scan_file(f, stage="api-contract"))
    if not diags:
        return
    for d in diags:
        print(render_diagnostic(d), file=sys.stderr)
    raise SystemExit(f"api contract check failed: {len(diags)} violation(s)")


def _project_root(entry: Path, *, project_root_override: str | None = None) -> Path:
    if project_root_override:
        return Path(project_root_override).resolve()
    return nearest_project_root(entry)


def _collect_jit_params(build: Any, *, overrides: list[str]) -> dict[str, object]:
    if not callable(build):
        raise SystemExit("build must be a callable @module entrypoint: `def build(m: Circuit, ...)`")

    sig = inspect.signature(build)
    params = list(sig.parameters.values())
    if not params:
        raise SystemExit("build must use JIT entry semantics: `@module def build(m: Circuit, ...)`")
    value_param_names = set(value_params_of(build).keys())

    # Collect JIT-time parameters from defaults.
    jit_params: dict[str, object] = {}
    missing: list[str] = []
    for p in params[1:]:
        if p.name in value_param_names:
            continue
        if p.default is inspect._empty:
            missing.append(p.name)
        else:
            jit_params[p.name] = p.default
    if missing:
        raise SystemExit(
            f"build() is treated as a JIT design function but missing default values for: {', '.join(missing)}"
        )

    # Apply CLI overrides.
    for spec in overrides:
        if "=" not in spec:
            raise SystemExit(f"--param expects name=value, got: {spec!r}")
        name, raw = spec.split("=", 1)
        name = name.strip()
        raw = raw.strip()
        if not name:
            raise SystemExit(f"--param expects name=value, got: {spec!r}")
        if name not in jit_params:
            raise SystemExit(f"unknown JIT parameter: {name!r} (available: {', '.join(jit_params.keys())})")
        try:
            val = ast.literal_eval(raw)
        except Exception:
            val = raw
        jit_params[name] = val

    return jit_params


def _top_name_for_build(src: Path, build: Any) -> str:
    top_name = _default_top_name(src)
    override = getattr(build, "__pycircuit_name__", None)
    if isinstance(override, str) and override.strip():
        top_name = override.strip()
    return top_name


def _cmd_emit(args: argparse.Namespace) -> int:
    src_arg = args.python_file
    out = Path(args.output)
    src, mod = _resolve_emit_source(src_arg)
    if src is not None:
        _scan_api_contract(src, project_root_override=args.project_root)
    if not hasattr(mod, "build"):
        raise SystemExit(f"{src_arg} must define a pyCircuit entrypoint: `@module def build(m: Circuit, ...)`")
    build = getattr(mod, "build")

    jit_params = _collect_jit_params(build, overrides=list(args.param or []))
    top_name = _top_name_for_build(src if src is not None else Path(src_arg.replace(".", "/") + ".py"), build)
    try:
        design = compile(build, name=top_name, **jit_params)
    except (DesignError, JitError) as e:
        raise SystemExit(f"design compile failed: {e}") from e

    if isinstance(design, Design):
        out.write_text(design.emit_mlir(), encoding="utf-8")
        if getattr(args, "module_graph_out", None):
            tool = _tool_script("pyc_module_graph.py")
            cmd = [
                sys.executable,
                str(tool),
                "--pyc",
                str(out),
                "--out",
                str(args.module_graph_out),
                "--edge-label-mode",
                str(getattr(args, "module_graph_edge_label_mode", "ports")),
                "--edge-label-limit",
                str(int(getattr(args, "module_graph_edge_label_limit", 4))),
                "--max-nodes",
                str(int(getattr(args, "module_graph_max_nodes", 500))),
                "--max-edges",
                str(int(getattr(args, "module_graph_max_edges", 2000))),
            ]
            if getattr(args, "module_graph_module", ""):
                cmd += ["--module", str(args.module_graph_module)]
            if bool(getattr(args, "module_graph_recursive", False)):
                # "Recursive nest" = expand the full instance hierarchy (bounded by tool guardrails).
                cmd += ["--hierarchical", "--expand-all", "--expand-depth", "64"]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(
                    "module-graph generation failed.\n"
                    f"cmd: {' '.join(cmd)}\n"
                    f"stdout:\n{r.stdout}\n"
                    f"stderr:\n{r.stderr}\n"
                )
        return 0

    raise SystemExit("internal error: compile did not return a Design")
    return 0


def _detect_pycc() -> Path:
    env = os.environ.get("PYCC")
    if env:
        p = Path(env)
        if p.is_file() and os.access(p, os.X_OK):
            return p
        raise SystemExit(f"PYCC is set but not executable: {p}")

    root = Path(__file__).resolve().parents[3]
    toolchain_root_env = os.environ.get("PYC_TOOLCHAIN_ROOT")
    candidates = [
        tool_executable("pycc"),
        Path(toolchain_root_env) / "bin" / "pycc" if toolchain_root_env else None,
        root / ".pycircuit_out" / "toolchain" / "install" / "bin" / "pycc",
        root / "dist" / "pycircuit" / "bin" / "pycc",
        root / "build-top" / "bin" / "pycc",
        root / "build" / "bin" / "pycc",
        root / "compiler" / "mlir" / "build2" / "bin" / "pycc",
        root / "compiler" / "mlir" / "build" / "bin" / "pycc",
    ]
    for c in candidates:
        if c is None:
            continue
        if c.is_file() and os.access(c, os.X_OK):
            return c

    found = shutil.which("pycc")
    if found:
        return Path(found)

    raise SystemExit("missing pycc (set PYCC=... or build it with: flows/scripts/pyc build)")


def _toolchain_roots(pycc: Path | None = None) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            rp = path.resolve()
        except OSError:
            return
        if rp in seen:
            return
        seen.add(rp)
        roots.append(rp)

    env = os.environ.get("PYC_TOOLCHAIN_ROOT")
    if env:
        add(Path(env))

    add(bundled_toolchain_root())

    if pycc is not None:
        try:
            resolved_pycc = pycc.resolve()
        except OSError:
            resolved_pycc = pycc
        if resolved_pycc.parent.name == "bin":
            add(resolved_pycc.parent.parent)

    repo_root = Path(__file__).resolve().parents[3]
    add(repo_root / ".pycircuit_out" / "toolchain" / "install")
    add(repo_root / "dist" / "pycircuit")
    return roots


def _runtime_lib_filename() -> str:
    return "pyc4_runtime.lib" if os.name == "nt" else "libpyc4_runtime.a"


def _detect_toolchain_root(pycc: Path | None = None) -> Path | None:
    for root in _toolchain_roots(pycc):
        cmake_cfg = root / "share" / "pycircuit" / "cmake" / "pycircuitConfig.cmake"
        runtime_lib = root / "lib" / _runtime_lib_filename()
        if cmake_cfg.is_file() or runtime_lib.is_file():
            return root
    return None


def _runtime_manifest_for_toolchain(toolchain_root: Path | None) -> dict[str, object]:
    if toolchain_root is None:
        raise SystemExit(
            "missing pyc toolchain root (set PYC_TOOLCHAIN_ROOT or use flows/scripts/pyc build to stage an install tree)"
        )

    include_dir = (toolchain_root / "include").resolve()
    lib_dir = (toolchain_root / "lib").resolve()
    cmake_config_dir = (toolchain_root / "share" / "pycircuit" / "cmake").resolve()
    runtime_lib = (lib_dir / _runtime_lib_filename()).resolve()

    if not include_dir.is_dir():
        raise SystemExit(f"invalid toolchain root: missing include dir: {include_dir}")
    if not runtime_lib.is_file():
        raise SystemExit(f"invalid toolchain root: missing runtime library: {runtime_lib}")

    return {
        "mode": "prebuilt",
        "cmake_package": "pycircuit",
        "cmake_target": "pycircuit::pyc4_runtime",
        "toolchain_root_hint": str(toolchain_root.resolve()),
        "cmake_config_dir": str(cmake_config_dir),
        "include_dirs": [str(include_dir)],
        "lib_dirs": [str(lib_dir)],
        "libs": ["pyc4_runtime"],
        "library_files": [str(runtime_lib)],
    }


def _as_int_width(ty: str) -> int:
    if ty == "!pyc.clock" or ty == "!pyc.reset":
        return 1
    if not ty.startswith("i"):
        raise SystemExit(f"unsupported port type for TB generation: {ty!r}")
    return int(ty[1:])


def _collect_build(mod: object, src: Path, args: argparse.Namespace) -> Module | Design:
    if not hasattr(mod, "build"):
        raise SystemExit(f"{src} must define a pyCircuit entrypoint: `@module def build(m: Circuit, ...)`")
    build = getattr(mod, "build")

    jit_params = _collect_jit_params(build, overrides=list(getattr(args, "param", []) or []))
    top_name = _top_name_for_build(src, build)
    try:
        return compile(build, name=top_name, **jit_params)
    except (DesignError, JitError) as e:
        raise SystemExit(f"design compile failed: {e}") from e


class _TopIface:
    def __init__(self, *, sym: str, in_raw: list[str], in_tys: list[str], out_raw: list[str], out_tys: list[str]) -> None:
        self.sym = str(sym)
        self.in_raw = list(in_raw)
        self.in_tys = list(in_tys)
        self.out_raw = list(out_raw)
        self.out_tys = list(out_tys)

        all_raw = [*self.in_raw, *self.out_raw]
        if len(set(all_raw)) != len(all_raw):
            raise SystemExit("TB generation requires unique port names across inputs and outputs")

        used: dict[str, int] = {}
        all_names: list[str] = []
        for r in all_raw:
            base = _sanitize_id(r)
            n = used.get(base, 0) + 1
            used[base] = n
            all_names.append(base if n == 1 else f"{base}_{n}")
        self.in_names = all_names[: len(self.in_raw)]
        self.out_names = all_names[len(self.in_raw) :]

        self._by_raw: dict[str, tuple[str, str, str]] = {}
        for rn, sn, ty in zip(self.in_raw, self.in_names, self.in_tys):
            self._by_raw[rn] = ("in", sn, ty)
        for rn, sn, ty in zip(self.out_raw, self.out_names, self.out_tys):
            self._by_raw[rn] = ("out", sn, ty)

    def resolve(self, raw_name: str) -> tuple[str, str, str]:
        r = str(raw_name).strip()
        if r not in self._by_raw:
            raise SystemExit(f"unknown DUT port referenced by TB: {r!r}")
        return self._by_raw[r]


def _top_iface(design: Module | Design) -> _TopIface:
    if isinstance(design, Design):
        cm = design.lookup(design.top)
        if cm is None:
            raise SystemExit(f"internal: missing top module {design.top!r} in Design")
        return _TopIface(
            sym=cm.sym_name,
            in_raw=list(cm.arg_names),
            in_tys=list(cm.arg_types),
            out_raw=list(cm.result_names),
            out_tys=list(cm.result_types),
        )

    in_raw = [n for n, _ in getattr(design, "_args", [])]  # noqa: SLF001
    in_tys = [sig.ty for _, sig in getattr(design, "_args", [])]  # noqa: SLF001
    out_raw = [n for n, _ in getattr(design, "_results", [])]  # noqa: SLF001
    out_tys = [sig.ty for _, sig in getattr(design, "_results", [])]  # noqa: SLF001
    return _TopIface(sym=str(getattr(design, "name", "Top")), in_raw=in_raw, in_tys=in_tys, out_raw=out_raw, out_tys=out_tys)


def _top_iface_from_manifest(manifest: Mapping[str, Any]) -> _TopIface:
    top = str(manifest.get("top", "")).strip()
    modules = manifest.get("modules", None)
    if not top or not isinstance(modules, list):
        raise SystemExit("invalid project_manifest.json: missing `top` or `modules`")
    for m in modules:
        if not isinstance(m, Mapping):
            continue
        if str(m.get("name", "")).strip() != top:
            continue
        in_raw = [str(x) for x in (m.get("arg_names") or [])]
        in_tys = [str(x) for x in (m.get("arg_types") or [])]
        out_raw = [str(x) for x in (m.get("result_names") or [])]
        out_tys = [str(x) for x in (m.get("result_types") or [])]
        return _TopIface(sym=top, in_raw=in_raw, in_tys=in_tys, out_raw=out_raw, out_tys=out_tys)
    raise SystemExit(f"invalid project_manifest.json: top module {top!r} not found in modules list")


def _module_paths_from_manifest(manifest: Mapping[str, Any], *, out_dir: Path) -> dict[str, Path]:
    modules = manifest.get("modules", None)
    if not isinstance(modules, list) or not modules:
        raise SystemExit("invalid project_manifest.json: missing `modules` list")
    out: dict[str, Path] = {}
    for m in modules:
        if not isinstance(m, Mapping):
            continue
        name = str(m.get("name", "")).strip()
        pyc_rel = str(m.get("pyc", "")).strip()
        if not name or not pyc_rel:
            continue
        out[name] = (out_dir / pyc_rel).resolve()
    if not out:
        raise SystemExit("invalid project_manifest.json: module list is empty")
    return out


def _render_tb_cpp_runtime_loop(
    iface: _TopIface,
    t: Tb,
    *,
    trace_plan: TracePlan | None = None,
    schedule_path: Path | None = None,
    schedule_format: str = "pycstb3",
) -> str:
    has_clocks = bool(t.clocks)
    has_reset = t.reset_spec is not None
    if has_reset and not has_clocks:
        raise SystemExit("tb() with reset requires at least one clock via t.clock(...)")
    if trace_plan and trace_plan.enabled_signals:
        raise SystemExit("runtime-loop C++ TB currently does not support trace-config binary traces; use --tb-schedule-mode=inline")
    if schedule_path is None:
        raise SystemExit("runtime-loop C++ TB requires an external schedule path")
    fmt = str(schedule_format).strip().lower()
    if fmt not in {"pycstb3", "pycstb4"}:
        raise SystemExit(f"unsupported runtime-loop schedule format: {schedule_format!r}")
    from .schedule_ir import (
        build_runtime_loop_schedule_ir,
        infer_port_protocol,
        infer_port_role,
        render_schedule_ir_json,
        schedule_ir_to_pycstb4_bytes,
    )

    top = _sanitize_id(iface.sym)
    hdr = f"{iface.sym}.hpp"

    def mask_value(v: int | bool, width: int) -> int:
        if isinstance(v, bool):
            vv = 1 if v else 0
        else:
            vv = int(v)
        if width <= 0:
            raise SystemExit("internal: invalid width")
        return vv & ((1 << width) - 1)

    def nwords(width: int) -> int:
        return (int(width) + 63) // 64

    def value_words(v: int | bool, width: int) -> list[int]:
        vv = mask_value(v, width)
        return [((vv >> (64 * i)) & ((1 << 64) - 1)) for i in range(nwords(width))]

    def words_array_literal(words: list[int], max_words: int) -> str:
        padded = list(words) + [0] * max(0, max_words - len(words))
        return "{{" + ", ".join(f"0x{int(w) & ((1 << 64) - 1):x}ull" for w in padded[:max_words]) + "}}"

    def wire_from_event_expr(width: int) -> str:
        return f"pyc::cpp::Wire<{int(width)}>({{{', '.join(f'ev.words[{i}]' for i in range(nwords(width)))}}})"

    def wire_from_frame_expr(width: int, slot: int) -> str:
        return f"pyc::cpp::Wire<{int(width)}>({{{', '.join(f'frame.words[{int(slot)}][{i}]' for i in range(nwords(width)))}}})"

    port_ids: dict[str, int] = {}
    port_meta_by_sn: dict[str, dict[str, Any]] = {}

    def get_port_id(sn: str) -> int:
        key = str(sn)
        if key not in port_ids:
            port_ids[key] = len(port_ids)
        return port_ids[key]

    def register_port(raw: str, direction: str, ty: str) -> None:
        _dir, sn, resolved_ty = iface.resolve(raw)
        w = 1 if resolved_ty in {"!pyc.clock", "!pyc.reset"} else _as_int_width(resolved_ty)
        pid = get_port_id(sn)
        meta: dict[str, Any] = {
            "id": int(pid),
            "name": str(sn),
            "direction": direction,
            "bit_width": int(w),
            "word_count": int(nwords(w)),
            "role": infer_port_role(sn, resolved_ty),
        }
        protocol = infer_port_protocol(sn)
        if protocol is not None:
            meta["protocol"] = protocol
        port_meta_by_sn[sn] = meta

    for raw, ty in zip(iface.in_raw, iface.in_tys):
        register_port(str(raw), "input", str(ty))
    for raw, ty in zip(iface.out_raw, iface.out_tys):
        register_port(str(raw), "output", str(ty))

    drive_events: list[tuple[int, int, str, int, list[int]]] = []
    pre_expect_events: list[tuple[int, int, str, int, list[int], str]] = []
    post_expect_events: list[tuple[int, int, str, int, list[int], str]] = []

    for d in t.drives:
        dir_, sn, ty = iface.resolve(d.port)
        if dir_ != "in":
            raise SystemExit(f"drive() requires input port, got output: {d.port!r}")
        w = _as_int_width(ty)
        drive_events.append((int(d.at), get_port_id(sn), sn, w, value_words(d.value, w)))

    for e in t.expects:
        _dir, sn, ty = iface.resolve(e.port)
        w = _as_int_width(ty)
        msg = e.msg if e.msg is not None else f"{sn} mismatch"
        row = (int(e.at), get_port_id(sn), sn, w, value_words(e.value, w), str(msg))
        ph = str(getattr(e, "phase", "post")).strip().lower()
        if ph == "pre":
            pre_expect_events.append(row)
        else:
            post_expect_events.append(row)

    prints_at: dict[int, list[tuple[str, list[tuple[str, str, int]]]]] = {}
    prints_every: list[tuple[str, int, int, list[tuple[str, str, int]]]] = []
    for p in getattr(t, "prints", []):
        fmt = str(p.fmt)
        port_specs: list[tuple[str, str, int]] = []
        for raw in p.ports:
            _dir, sn, ty = iface.resolve(raw)
            w = _as_int_width(ty)
            if w > 64:
                raise SystemExit(f"print() for i{w} not supported in runtime-loop C++ TB generator")
            port_specs.append((str(raw), sn, w))
        if p.at is not None:
            prints_at.setdefault(int(p.at), []).append((fmt, port_specs))
        else:
            st = 0 if p.start is None else int(p.start)
            ev = 1 if p.every is None else int(p.every)
            prints_every.append((fmt, st, ev, port_specs))

    drive_events.sort(key=lambda x: (x[0], x[1]))
    pre_expect_events.sort(key=lambda x: (x[0], x[1]))
    post_expect_events.sort(key=lambda x: (x[0], x[1]))

    rand_specs: list[tuple[str, int, int, int, int]] = []
    if t.random_streams:
        used_ports: set[str] = set()
        for r in t.random_streams:
            dir_, sn, ty = iface.resolve(r.port)
            if dir_ != "in":
                raise SystemExit(f"random() requires input port, got output: {r.port!r}")
            if ty == "!pyc.clock" or ty == "!pyc.reset":
                raise SystemExit(f"random() cannot target clock/reset ports: {r.port!r}")
            if sn in used_ports:
                raise SystemExit(f"duplicate random() stream for port: {r.port!r}")
            used_ports.add(sn)
            w = _as_int_width(ty)
            if w > 64:
                raise SystemExit(f"random() for i{w} not supported in runtime-loop C++ TB generator")
            rand_specs.append((sn, w, int(r.seed), int(r.start), int(r.every)))

    clk_sn = ""
    rst_sn = ""
    ca = 0
    cd = 0
    if has_clocks:
        clk = t.clocks[0].port
        _, clk_sn, _clk_ty = iface.resolve(clk)
    if has_reset:
        rst = t.reset_spec.port
        _, rst_sn, _rst_ty = iface.resolve(rst)
        ca = int(t.reset_spec.cycles_asserted)
        cd = int(t.reset_spec.cycles_deasserted)

    max_words = 1
    for _cyc, _pid, _sn, _w, words in drive_events:
        max_words = max(max_words, len(words))
    for _cyc, _pid, _sn, _w, words, _msg in pre_expect_events:
        max_words = max(max_words, len(words))
    for _cyc, _pid, _sn, _w, words, _msg in post_expect_events:
        max_words = max(max_words, len(words))

    drive_ports = sorted({(pid, sn, w) for _cyc, pid, sn, w, _words in drive_events}, key=lambda x: x[0])
    drive_slot_by_pid = {int(pid): slot for slot, (pid, _sn, _w) in enumerate(drive_ports)}
    drive_frame_rows: list[tuple[int, list[int], list[list[int]]]] = []
    if drive_events:
        by_cycle: dict[int, list[tuple[int, list[int]]]] = {}
        for cyc, pid, _sn, _w, words in drive_events:
            by_cycle.setdefault(int(cyc), []).append((int(pid), list(words)))
        mask_words = (len(drive_ports) + 63) // 64
        for cyc in sorted(by_cycle.keys()):
            masks = [0] * mask_words
            values = [[0] * max_words for _ in drive_ports]
            for pid, words in by_cycle[cyc]:
                slot = drive_slot_by_pid[pid]
                masks[slot // 64] |= 1 << (slot % 64)
                for word_idx, word in enumerate(words[:max_words]):
                    values[slot][word_idx] = int(word) & ((1 << 64) - 1)
            drive_frame_rows.append((int(cyc), masks, values))
    expect_ports = sorted(
        {(pid, sn, w) for _cyc, pid, sn, w, _words, _msg in [*pre_expect_events, *post_expect_events]},
        key=lambda x: x[0],
    )

    def emit_event_array(name: str, rows: list[tuple[int, int, str, int, list[int]]] | list[tuple[int, int, str, int, list[int], str]]) -> list[str]:
        out: list[str] = []
        if not rows:
            out.append(f"static constexpr std::array<TbEvent, 0> {name} = {{}};\n\n")
            return out
        out.append(f"static constexpr std::array<TbEvent, {len(rows)}> {name} = {{\n")
        out.append("  {\n")
        for row in rows:
            cyc = int(row[0])
            pid = int(row[1])
            words = list(row[4])
            msg_lit = "nullptr"
            if len(row) >= 6:
                msg_lit = json.dumps(str(row[5]))
            out.append(
                f"    TbEvent{{{cyc}ull, {pid}u, {len(words)}u, {words_array_literal(words, max_words)}, {msg_lit}}},\n"
            )
        out.append("  }\n")
        out.append("};\n\n")
        return out

    schedule_t0 = time.perf_counter()

    def append_schedule_event(blob: bytearray, row: tuple[int, int, str, int, list[int]] | tuple[int, int, str, int, list[int], str]) -> None:
        cyc = int(row[0])
        pid = int(row[1])
        words = list(row[4])
        msg = str(row[5]).encode("utf-8") if len(row) >= 6 else b""
        blob.extend(cyc.to_bytes(8, "little", signed=False))
        blob.extend(pid.to_bytes(4, "little", signed=False))
        blob.extend(len(words).to_bytes(4, "little", signed=False))
        blob.extend(len(msg).to_bytes(4, "little", signed=False))
        for i in range(max_words):
            word = int(words[i]) if i < len(words) else 0
            blob.extend((word & ((1 << 64) - 1)).to_bytes(8, "little", signed=False))
        blob.extend(msg)

    def append_drive_frame(blob: bytearray, frame: tuple[int, list[int], list[list[int]]]) -> None:
        cyc, masks, values = frame
        blob.extend(int(cyc).to_bytes(8, "little", signed=False))
        for mask in masks:
            blob.extend((int(mask) & ((1 << 64) - 1)).to_bytes(8, "little", signed=False))
        for port_words in values:
            for word in port_words[:max_words]:
                blob.extend((int(word) & ((1 << 64) - 1)).to_bytes(8, "little", signed=False))

    schedule_blob = bytearray()
    schedule_blob.extend(b"PYCSTB3\n")
    schedule_blob.extend(int(max_words).to_bytes(4, "little", signed=False))
    schedule_blob.extend(len(drive_ports).to_bytes(4, "little", signed=False))
    schedule_blob.extend(len(drive_frame_rows).to_bytes(8, "little", signed=False))
    schedule_blob.extend(len(pre_expect_events).to_bytes(8, "little", signed=False))
    schedule_blob.extend(len(post_expect_events).to_bytes(8, "little", signed=False))
    for frame in drive_frame_rows:
        append_drive_frame(schedule_blob, frame)
    for row in pre_expect_events:
        append_schedule_event(schedule_blob, row)
    for row in post_expect_events:
        append_schedule_event(schedule_blob, row)
    schedule_path.parent.mkdir(parents=True, exist_ok=True)
    schedule_path.write_bytes(bytes(schedule_blob))
    schedule_generate_s = time.perf_counter() - schedule_t0
    schedule_stats = {
        "version": 3,
        "format": "PYCSTB3",
        "schedule": str(schedule_path),
        "schedule_bytes": len(schedule_blob),
        "schedule_json": str(schedule_path.with_suffix(".json")),
        "max_event_words": int(max_words),
        "drive_frames": len(drive_frame_rows),
        "drive_ports": len(drive_ports),
        "drive_events": len(drive_events),
        "pre_expect_events": len(pre_expect_events),
        "post_expect_events": len(post_expect_events),
        "total_events": len(drive_events) + len(pre_expect_events) + len(post_expect_events),
        "generate_s": schedule_generate_s,
    }
    schedule_ir = build_runtime_loop_schedule_ir(
        top_symbol=iface.sym,
        schedule_path=schedule_path,
        ports=port_meta_by_sn.values(),
        timeout_cycles=int(t.timeout_cycles),
        reset_cycles=int(ca + cd) if has_reset else 0,
        clocking="single_clock" if has_clocks else "none",
        schedule_bytes=len(schedule_blob),
        max_event_words=int(max_words),
        drive_events=drive_events,
        drive_ports=drive_ports,
        drive_frame_rows=drive_frame_rows,
        pre_expect_events=pre_expect_events,
        post_expect_events=post_expect_events,
        generate_s=schedule_generate_s,
    )
    schedule_pycstb4_blob = schedule_ir_to_pycstb4_bytes(schedule_ir)
    schedule_ir["stats"]["pycstb4_bytes"] = len(schedule_pycstb4_blob)
    schedule_json_text = render_schedule_ir_json(schedule_ir)
    schedule_path.with_suffix(".json").write_text(schedule_json_text, encoding="utf-8")
    schedule_path.with_suffix(".pycstb4").write_bytes(schedule_pycstb4_blob)
    schedule_stats["schedule_json_bytes"] = len(schedule_json_text.encode("utf-8"))
    schedule_stats["schedule_pycstb4"] = str(schedule_path.with_suffix(".pycstb4"))
    schedule_stats["schedule_pycstb4_bytes"] = len(schedule_pycstb4_blob)
    schedule_path.with_suffix(".stats.json").write_text(json.dumps(schedule_stats, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    drive_port_ids_literal = "{" + ", ".join(f"{int(pid)}u" for pid, _sn, _w in drive_ports) + "}"

    lines: list[str] = []
    lines.append("// Generated by pycircuit (prototype runtime-loop TB)\n")
    lines.append("#include <array>\n")
    lines.append("#include <cstdint>\n")
    lines.append("#include <cstdlib>\n")
    lines.append("#include <filesystem>\n")
    lines.append("#include <iostream>\n")
    lines.append("#include <optional>\n")
    lines.append("#include <string>\n\n")
    lines.append("#include <cpp/pyc_tb.hpp>\n\n")
    lines.append("#include <cpp/pyc_tb_runtime_loop.hpp>\n\n")
    lines.append("#include <cpp/pyc_tb_pycstb4.hpp>\n\n")
    lines.append(f"#include \"{hdr}\"\n\n")
    lines.append("using pyc::cpp::Testbench;\n\n")
    lines.append("namespace {\n\n")
    lines.append(f"static constexpr std::uint32_t kMaxEventWords = {int(max_words)}u;\n\n")
    lines.append(f"static constexpr std::uint32_t kDrivePortCount = {len(drive_ports)}u;\n")
    lines.append(f"static constexpr std::array<std::uint32_t, kDrivePortCount> kDrivePortIds = {drive_port_ids_literal};\n")
    lines.append(f"static constexpr const char *kScheduleFormat = {json.dumps(fmt)};\n")
    lines.append("using RuntimeLoopEvent = pyc::cpp::RuntimeLoopEvent<kMaxEventWords>;\n")
    lines.append("using RuntimeLoopDriveFrame = pyc::cpp::RuntimeLoopDriveFrame<kMaxEventWords, kDrivePortCount>;\n\n")

    lines.append("template <typename Dut>\n")
    lines.append("void applyDriveFrame(Dut &dut, const RuntimeLoopDriveFrame &frame) {\n")
    lines.append("  auto hasDrive = [&](std::uint32_t slot) -> bool {\n")
    lines.append("    return ((frame.port_mask[slot / 64u] >> (slot % 64u)) & 1ull) != 0ull;\n")
    lines.append("  };\n")
    for slot, (_pid, sn, w) in enumerate(drive_ports):
        lines.append(f"  if (hasDrive({int(slot)}u)) dut.{sn} = {wire_from_frame_expr(w, slot)};\n")
    lines.append("}\n\n")

    lines.append("template <typename Dut>\n")
    lines.append("void applyPeriodicDrive(Dut &dut, const pyc::cpp::Pycstb4PeriodicDrive &pattern, std::uint64_t cyc) {\n")
    lines.append("  const auto &words = pattern.activeAt(cyc) ? pattern.active_words : pattern.default_words;\n")
    lines.append("  switch (pattern.port_id) {\n")
    for pid, sn, w in drive_ports:
        lines.append(f"  case {int(pid)}u:\n")
        lines.append(f"    dut.{sn} = pyc::cpp::Wire<{int(w)}>({{{', '.join(f'words[{i}]' for i in range(nwords(w)))}}});\n")
        lines.append("    return;\n")
    lines.append("  default:\n")
    lines.append("    std::cerr << \"ERROR: invalid periodic drive port_id=\" << pattern.port_id << \" at cycle=\" << cyc << \"\\n\";\n")
    lines.append("    std::exit(1);\n")
    lines.append("  }\n")
    lines.append("}\n\n")

    lines.append("template <typename WireT>\n")
    lines.append("void printWireHex(const WireT &v) {\n")
    lines.append("  for (int i = static_cast<int>(WireT::kWords) - 1; i >= 0; --i) {\n")
    lines.append("    std::cerr << v.word(static_cast<unsigned>(i));\n")
    lines.append("  }\n")
    lines.append("}\n\n")

    lines.append("template <typename Dut>\n")
    lines.append("bool checkExpect(Dut &dut, const RuntimeLoopEvent &ev, const char *phase) {\n")
    lines.append("  switch (ev.port_id) {\n")
    for pid, sn, w in expect_ports:
        exp_expr = wire_from_event_expr(w)
        lines.append(f"  case {int(pid)}u: {{\n")
        lines.append(f"    const auto expected = {exp_expr};\n")
        lines.append(f"    if (!(dut.{sn} == expected)) {{\n")
        lines.append("      std::cerr << \"ERROR(\" << phase << \"): cycle=\" << ev.cycle")
        lines.append(f" << \" port={sn}\";\n")
        lines.append("      if (!ev.msg.empty()) std::cerr << \" msg=\" << ev.msg;\n")
        lines.append("      std::cerr << \" got=0x\" << std::hex;\n")
        lines.append(f"      printWireHex(dut.{sn});\n")
        lines.append("      std::cerr << \" exp=0x\";\n")
        lines.append("      printWireHex(expected);\n")
        lines.append("      std::cerr << std::dec << \"\\n\";\n")
        lines.append("      return false;\n")
        lines.append("    }\n")
        lines.append("    return true;\n")
        lines.append("  }\n")
    lines.append("  default:\n")
    lines.append("    std::cerr << \"ERROR(\" << phase << \"): invalid expect port_id=\" << ev.port_id << \" at cycle=\" << ev.cycle << \"\\n\";\n")
    lines.append("    return false;\n")
    lines.append("  }\n")
    lines.append("}\n\n")
    lines.append("} // namespace\n\n")

    lines.append("int main() {\n")
    lines.append(f"  pyc::gen::{top} dut;\n")
    lines.append(f"  Testbench<pyc::gen::{top}> tb(dut);\n\n")
    lines.append("  const char *schedule_env = std::getenv(\"PYC_TB_SCHEDULE\");\n")
    lines.append(
        f"  const std::filesystem::path schedule_path = schedule_env != nullptr && schedule_env[0] != '\\0' ? std::filesystem::path(schedule_env) : std::filesystem::path({json.dumps(str(schedule_path))});\n"
    )
    lines.append("  pyc::cpp::RuntimeLoopSchedule<kMaxEventWords, kDrivePortCount> schedule;\n")
    lines.append("  pyc::cpp::Pycstb4Schedule pycstb4_schedule;\n")
    lines.append("  const bool using_pycstb4 = std::string(kScheduleFormat) == \"pycstb4\";\n")
    lines.append("  if (using_pycstb4) {\n")
    lines.append("    std::filesystem::path pycstb4_path = schedule_path;\n")
    lines.append("    pycstb4_path.replace_extension(\".pycstb4\");\n")
    lines.append("    std::string pycstb4_error;\n")
    lines.append("    if (!pyc::cpp::loadPycstb4Schedule(pycstb4_path, &pycstb4_schedule, &pycstb4_error)) {\n")
    lines.append("      std::cerr << \"ERROR: failed to load PYCSTB4 schedule: \" << pycstb4_error << \"\\n\";\n")
    lines.append("      return 1;\n")
    lines.append("    }\n")
    lines.append("    if (!pyc::cpp::convertPycstb4ToRuntimeLoopSchedule(pycstb4_schedule, kDrivePortIds, &schedule, &pycstb4_error)) {\n")
    lines.append("      std::cerr << \"ERROR: failed to convert PYCSTB4 schedule: \" << pycstb4_error << \"\\n\";\n")
    lines.append("      return 1;\n")
    lines.append("    }\n")
    lines.append("  } else {\n")
    lines.append("    if (!pyc::cpp::loadRuntimeLoopSchedule(schedule_path, schedule)) return 1;\n")
    lines.append("  }\n\n")
    lines.append("  const char *pycstb4_check_env = std::getenv(\"PYC_TB_PYCSTB4_CHECK\");\n")
    lines.append("  if (pycstb4_check_env != nullptr && pycstb4_check_env[0] != '\\0') {\n")
    lines.append("    std::filesystem::path pycstb4_path = schedule_path;\n")
    lines.append("    pycstb4_path.replace_extension(\".pycstb4\");\n")
    lines.append("    pyc::cpp::Pycstb4Schedule pycstb4_schedule;\n")
    lines.append("    std::string pycstb4_error;\n")
    lines.append("    if (!pyc::cpp::loadPycstb4Schedule(pycstb4_path, &pycstb4_schedule, &pycstb4_error)) {\n")
    lines.append("      std::cerr << \"ERROR: failed to load PYCSTB4 sidecar: \" << pycstb4_error << \"\\n\";\n")
    lines.append("      return 1;\n")
    lines.append("    }\n")
    lines.append("    const std::size_t pycstb4_expect_events = pycstb4_schedule.events.size();\n")
    lines.append("    const std::size_t pycstb3_expect_events = schedule.pre_expect_events.size() + schedule.post_expect_events.size();\n")
    lines.append("    if (pycstb4_schedule.frames.size() > schedule.drive_frames.size() || pycstb4_expect_events != pycstb3_expect_events) {\n")
    lines.append("      std::cerr << \"ERROR: PYCSTB4 sidecar shape mismatch: frames=\" << pycstb4_schedule.frames.size()\n")
    lines.append("                << \" max_expected_frames=\" << schedule.drive_frames.size()\n")
    lines.append("                << \" events=\" << pycstb4_expect_events\n")
    lines.append("                << \" expected_events=\" << pycstb3_expect_events << \"\\n\";\n")
    lines.append("      return 1;\n")
    lines.append("    }\n")
    lines.append("  }\n\n")
    if rand_specs:
        lines.append("  // Random streams (deterministic).\n")
        for sn, _w, seed, _st, _ev in rand_specs:
            seed64 = int(seed) & ((1 << 64) - 1)
            lines.append(f"  std::uint64_t rng_{sn} = 0x{seed64:x}ull;\n")
        lines.append("\n")

    lines.append("  const char *trace_dir_env = std::getenv(\"PYC_TRACE_DIR\");\n")
    lines.append("  const bool trace_env_enabled = (trace_dir_env != nullptr) && (std::string(trace_dir_env).size() != 0);\n")
    lines.append("  if (trace_env_enabled) {\n")
    lines.append("    std::filesystem::path out_dir = std::filesystem::path(trace_dir_env);\n")
    lines.append(f"    out_dir /= \"tb_{iface.sym}\";\n")
    lines.append("    std::filesystem::create_directories(out_dir);\n")
    lines.append(f"    tb.enableVcd((out_dir / \"tb_{iface.sym}.vcd\").string(), /*top=*/\"tb_{iface.sym}\");\n")
    for sn in [*iface.in_names, *iface.out_names]:
        lines.append(f"    tb.vcdTrace(dut.{sn}, \"{sn}\");\n")
    lines.append("  }\n\n")

    if has_clocks:
        for c in t.clocks:
            dir_, sn, _ = iface.resolve(c.port)
            if dir_ != "in":
                raise SystemExit(f"clock must be an input port, got output: {c.port!r}")
            lines.append(
                f"  tb.addClock(dut.{sn}, /*halfPeriodSteps=*/{int(c.half_period_steps)}, /*phaseSteps=*/{int(c.phase_steps)}, /*startHigh=*/{str(bool(c.start_high)).lower()});\n"
            )
    if has_reset:
        lines.append(f"  tb.reset(dut.{rst_sn}, /*cyclesAsserted=*/{int(ca)}, /*cyclesDeasserted=*/{int(cd)});\n\n")

    lines.append(f"  const std::uint64_t timeout_cycles = {int(t.timeout_cycles)}ull;\n")
    lines.append(f"  bool ok = {str(t.finish_cycle is None).lower()};\n")
    lines.append("  std::size_t drive_frame_idx = 0;\n")
    lines.append("  std::size_t pre_expect_idx = 0;\n")
    lines.append("  std::size_t post_expect_idx = 0;\n")
    lines.append("  for (std::uint64_t cyc = 0; cyc < timeout_cycles; ++cyc) {\n")

    if rand_specs:
        lines.append("    // Random drives for this cycle (applied before explicit drives).\n")
        for sn, w, _seed, st, ev in rand_specs:
            mask = (1 << w) - 1 if w < 64 else (1 << 64) - 1
            lines.append(
                f"    if (cyc >= {int(st)}ull && ((cyc - {int(st)}ull) % {int(ev)}ull) == 0ull) {{\n"
                f"      rng_{sn} = rng_{sn} * 6364136223846793005ull + 1ull;\n"
                f"      dut.{sn} = pyc::cpp::Wire<{w}>(0x{mask:x}ull & rng_{sn});\n"
                f"    }}\n"
            )
        lines.append("\n")

    lines.append("    if (using_pycstb4) {\n")
    lines.append("      for (const auto &pattern : pycstb4_schedule.periodic_drives) {\n")
    lines.append("        if (cyc >= pattern.start_cycle && cyc < pattern.end_cycle) applyPeriodicDrive(dut, pattern, cyc);\n")
    lines.append("      }\n")
    lines.append("    }\n")
    lines.append("    while (drive_frame_idx < schedule.drive_frames.size() && schedule.drive_frames[drive_frame_idx].cycle == cyc) {\n")
    lines.append("      applyDriveFrame(dut, schedule.drive_frames[drive_frame_idx]);\n")
    lines.append("      ++drive_frame_idx;\n")
    lines.append("    }\n")
    lines.append("    if (pre_expect_idx < schedule.pre_expect_events.size() && schedule.pre_expect_events[pre_expect_idx].cycle == cyc) {\n")
    lines.append("      pyc::cpp::detail::maybe_comb(dut);\n")
    lines.append("    }\n")
    lines.append("    while (pre_expect_idx < schedule.pre_expect_events.size() && schedule.pre_expect_events[pre_expect_idx].cycle == cyc) {\n")
    lines.append("      if (!checkExpect(dut, schedule.pre_expect_events[pre_expect_idx], \"pre\")) return 1;\n")
    lines.append("      ++pre_expect_idx;\n")
    lines.append("    }\n")
    if has_clocks:
        lines.append("    tb.runCycleAutoTrace(cyc, nullptr);\n")
    else:
        lines.append("    tb.runSteps(1);\n")
    lines.append("    while (post_expect_idx < schedule.post_expect_events.size() && schedule.post_expect_events[post_expect_idx].cycle == cyc) {\n")
    lines.append("      if (!checkExpect(dut, schedule.post_expect_events[post_expect_idx], \"post\")) return 1;\n")
    lines.append("      ++post_expect_idx;\n")
    lines.append("    }\n")
    if prints_at or prints_every:
        if prints_at:
            lines.append("    // Per-cycle prints.\n")
            lines.append("    switch (cyc) {\n")
            for cyc in sorted(prints_at.keys()):
                lines.append(f"    case {cyc}: {{\n")
                for fmt, ports in prints_at[cyc]:
                    msg_lit = json.dumps(f" {fmt}")
                    lines.append(f"      std::cerr << \"[tb] cyc=\" << cyc << {msg_lit}")
                    for raw, sn, w in ports:
                        raw_lit = json.dumps(f" {raw}=")
                        if w == 1:
                            lines.append(f" << {raw_lit} << dut.{sn}.value()")
                        else:
                            lines.append(f" << {raw_lit} << \"0x\" << std::hex << dut.{sn}.value() << std::dec")
                    lines.append(" << \"\\n\";\n")
                lines.append("      break; }\n")
            lines.append("    default: break;\n")
            lines.append("    }\n")
        if prints_every:
            lines.append("    // Periodic prints.\n")
            for fmt, st, ev, ports in prints_every:
                msg_lit = json.dumps(f" {fmt}")
                lines.append(f"    if (cyc >= {st}ull && ((cyc - {st}ull) % {ev}ull) == 0ull) {{\n")
                lines.append(f"      std::cerr << \"[tb] cyc=\" << cyc << {msg_lit}")
                for raw, sn, w in ports:
                    raw_lit = json.dumps(f" {raw}=")
                    if w == 1:
                        lines.append(f" << {raw_lit} << dut.{sn}.value()")
                    else:
                        lines.append(f" << {raw_lit} << \"0x\" << std::hex << dut.{sn}.value() << std::dec")
                lines.append(" << \"\\n\";\n")
                lines.append("    }\n")
    if t.finish_cycle is not None:
        lines.append(f"    if (cyc >= {int(t.finish_cycle)}ull) {{ ok = true; break; }}\n")
    lines.append("  }\n\n")
    lines.append("  if (!ok) {\n")
    lines.append("    std::cerr << \"TIMEOUT: finish cycle not reached within \" << timeout_cycles << \" cycles\\n\";\n")
    lines.append("    return 1;\n")
    lines.append("  }\n")
    lines.append("  return 0;\n")
    lines.append("}\n")
    return "".join(lines)


def _render_tb_cpp_actor_fastpath(
    iface: _TopIface,
    t: Tb,
    *,
    trace_plan: TracePlan | None = None,
    actor_data_path: Path | None = None,
) -> str:
    has_clocks = bool(t.clocks)
    has_reset = t.reset_spec is not None
    if has_reset and not has_clocks:
        raise SystemExit("tb() with reset requires at least one clock via t.clock(...)")
    if trace_plan and trace_plan.enabled_signals:
        raise SystemExit("actor-fastpath C++ TB currently does not support trace-config binary traces")
    if actor_data_path is None:
        raise SystemExit("actor-fastpath C++ TB requires an external actor data path")

    top = _sanitize_id(iface.sym)
    hdr = f"{iface.sym}.hpp"

    def mask_value(v: int | bool, width: int) -> int:
        vv = 1 if isinstance(v, bool) and v else 0 if isinstance(v, bool) else int(v)
        return vv & ((1 << int(width)) - 1)

    ports: dict[str, tuple[str, str, int]] = {}
    for raw, ty in zip(iface.in_raw, iface.in_tys):
        _dir, sn, resolved_ty = iface.resolve(str(raw))
        width = 1 if resolved_ty in {"!pyc.clock", "!pyc.reset"} else _as_int_width(resolved_ty)
        ports[str(sn)] = ("input", str(resolved_ty), int(width))
    for raw, ty in zip(iface.out_raw, iface.out_tys):
        _dir, sn, resolved_ty = iface.resolve(str(raw))
        width = 1 if resolved_ty in {"!pyc.clock", "!pyc.reset"} else _as_int_width(resolved_ty)
        ports[str(sn)] = ("output", str(resolved_ty), int(width))

    pure_wgen_requested = bool(os.environ.get("PYC_TB_ACTOR_PURE_WGEN"))
    generated_workloads = list(getattr(t, "generated_ready_valid_workloads", []))
    generated_workload = generated_workloads[0] if pure_wgen_requested and generated_workloads else None
    rv_sources = list(getattr(t, "ready_valid_sources", []))
    rv_sinks = list(getattr(t, "ready_valid_sinks", []))
    explicit_ready_valid = rv_sources[0] if rv_sources and rv_sinks else None
    explicit_ready_valid_sink = rv_sinks[0] if rv_sources and rv_sinks else None

    source_valid_raw = "cmd_valid"
    source_payload_raw = "cmd_data"
    source_ready_raw = "cmd_ready"
    sink_valid_raw = "result_valid"
    sink_payload_raw = "result_data"
    sink_ready_raw = "result_ready"
    source_protocol = "cmd"
    sink_protocol = "result"
    source_actor_name = "cmd_source"
    sink_actor_name = "result_sink"
    scoreboard_name = "result_scoreboard"
    if generated_workload is not None:
        source_valid_raw = str(generated_workload.source_valid)
        source_payload_raw = str(generated_workload.source_payload)
        source_ready_raw = str(generated_workload.source_ready)
        sink_valid_raw = str(generated_workload.sink_valid)
        sink_payload_raw = str(generated_workload.sink_payload)
        sink_ready_raw = str(generated_workload.sink_ready)
    elif explicit_ready_valid is not None and explicit_ready_valid_sink is not None:
        source_valid_raw = str(explicit_ready_valid.valid)
        source_payload_raw = str(explicit_ready_valid.payload)
        source_ready_raw = str(explicit_ready_valid.ready)
        sink_valid_raw = str(explicit_ready_valid_sink.valid)
        sink_payload_raw = str(explicit_ready_valid_sink.payload)
        sink_ready_raw = str(explicit_ready_valid_sink.ready)
        source_actor_name = str(explicit_ready_valid.name)
        sink_actor_name = str(explicit_ready_valid_sink.name)
        source_protocol = source_actor_name
        sink_protocol = sink_actor_name
        scoreboard_name = f"{sink_actor_name}_scoreboard"

    def resolve_actor_port(raw_name: str, expected_dir: str) -> tuple[str, str, int]:
        dir_, sn, ty = iface.resolve(raw_name)
        actual_dir = "input" if dir_ == "in" else "output"
        if actual_dir != expected_dir:
            raise SystemExit(f"actor-fastpath port {raw_name!r} must be {expected_dir}, got {actual_dir}")
        width = 1 if ty in {"!pyc.clock", "!pyc.reset"} else _as_int_width(ty)
        return str(sn), str(ty), int(width)

    source_valid_sn, _source_valid_ty, source_valid_w = resolve_actor_port(source_valid_raw, "input")
    source_payload_sn, _source_payload_ty, data_w = resolve_actor_port(source_payload_raw, "input")
    source_ready_sn, _source_ready_ty, source_ready_w = resolve_actor_port(source_ready_raw, "output")
    sink_valid_sn, _sink_valid_ty, sink_valid_w = resolve_actor_port(sink_valid_raw, "output")
    sink_payload_sn, _sink_payload_ty, result_w = resolve_actor_port(sink_payload_raw, "output")
    sink_ready_sn, _sink_ready_ty, sink_ready_w = resolve_actor_port(sink_ready_raw, "input")
    if source_valid_w != 1 or source_ready_w != 1 or sink_valid_w != 1 or sink_ready_w != 1:
        raise SystemExit("actor-fastpath ready-valid control ports must be 1-bit")
    if data_w != result_w:
        raise SystemExit("actor-fastpath requires source and sink payload ports to have the same width")
    if data_w > 64:
        raise SystemExit("actor-fastpath prototype currently supports payload width <= 64")

    drives_by_sn: dict[str, list[tuple[int, int]]] = {}
    for d in t.drives:
        dir_, sn, ty = iface.resolve(d.port)
        if dir_ != "in":
            raise SystemExit(f"drive() requires input port, got output: {d.port!r}")
        w = _as_int_width(ty)
        if w > 64:
            raise SystemExit("actor-fastpath prototype currently supports drive width <= 64")
        drives_by_sn.setdefault(str(sn), []).append((int(d.at), mask_value(d.value, w)))
    for rows in drives_by_sn.values():
        rows.sort(key=lambda x: x[0])

    from .actor_fastpath import infer_ready_valid_actor_timing, write_ready_valid_actor_sidecars

    source_payloads: list[int] | None = None
    transaction_count = 0
    workload_generator: dict[str, object] | None = None
    if generated_workload is not None:
        if int(generated_workload.data_width) != int(data_w):
            raise SystemExit("actor-fastpath generated_ready_valid data_width does not match source payload width")
        transaction_count = int(generated_workload.count)
        start_cycle = int(generated_workload.start_cycle)
        if int(generated_workload.ready_period) > 0 and int(generated_workload.ready_stall) > 0:
            ready_pattern = {
                "kind": "periodic",
                "period": int(generated_workload.ready_period),
                "active_cycles": int(generated_workload.ready_stall),
                "phase_cycle": 0,
            }
            accept_cycles = int(ready_pattern["period"]) - int(ready_pattern["active_cycles"])
            estimated_cycles = (transaction_count * int(ready_pattern["period"]) + accept_cycles - 1) // accept_cycles
        else:
            ready_pattern = {"kind": "constant", "value": "0x1"}
            estimated_cycles = transaction_count
        actor_end_cycle = max(int(t.timeout_cycles), start_cycle + estimated_cycles + 64)
        workload_generator = {
            "name": f"{source_protocol}_seeded_source",
            "generator_id": str(generated_workload.generator_id),
            "profile": "synthetic_ready_valid_payload",
            "seed": int(generated_workload.seed),
            "count": transaction_count,
            "start_index": 0,
            "output_ports": [1],
            "constraints": {
                "data_width": int(data_w),
                "multiplier": hex(int(generated_workload.multiplier)),
                "formula": "((index + 1) * multiplier + seed) & mask",
            },
            "deterministic": True,
        }
    elif explicit_ready_valid is not None and explicit_ready_valid_sink is not None:
        source_payloads = [mask_value(value, data_w) for value in explicit_ready_valid.transactions]
        expected_payloads = [mask_value(value, data_w) for value in explicit_ready_valid_sink.expected]
        if len(source_payloads) != len(expected_payloads):
            raise SystemExit("actor-fastpath explicit ready_valid source/sink transaction count mismatch")
        for idx, (got, want) in enumerate(zip(source_payloads, expected_payloads)):
            if got != want:
                raise SystemExit(f"actor-fastpath explicit ready_valid v0 expects pass-through payloads, mismatch at tx {idx}")
        transaction_count = len(source_payloads)
        ready_pattern = (
            {
                "kind": "periodic",
                "period": int(explicit_ready_valid_sink.ready_period),
                "active_cycles": int(explicit_ready_valid_sink.ready_stall),
                "phase_cycle": 0,
            }
            if int(explicit_ready_valid_sink.ready_period) > 0 and int(explicit_ready_valid_sink.ready_stall) > 0
            else {"kind": "constant", "value": "0x1"}
        )
        start_cycle = 1
        if str(ready_pattern.get("kind")) == "periodic":
            accept_cycles = int(ready_pattern["period"]) - int(ready_pattern["active_cycles"])
            estimated_cycles = (transaction_count * int(ready_pattern["period"]) + accept_cycles - 1) // accept_cycles
        else:
            estimated_cycles = transaction_count
        actor_end_cycle = max(int(t.timeout_cycles), start_cycle + estimated_cycles + 64)
    else:
        tx_rows = [(cyc, value) for cyc, value in drives_by_sn.get(source_payload_sn, []) if int(cyc) > 0]
        if not tx_rows:
            raise SystemExit(f"actor-fastpath could not find {source_payload_raw} transaction drives after cycle 0")
        source_payloads = [value for _cyc, value in tx_rows]
        transaction_count = len(source_payloads)

        ready_rows = [(cyc, value & 1) for cyc, value in drives_by_sn.get(sink_ready_sn, []) if int(cyc) > 0]
        actor_timing = infer_ready_valid_actor_timing(
            ready_samples=ready_rows,
            transaction_count=transaction_count,
            timeout_cycles=int(t.timeout_cycles),
        )
        ready_pattern = actor_timing["ready_pattern"]
        start_cycle = int(actor_timing["start_cycle"])
        actor_end_cycle = int(actor_timing["actor_end_cycle"])

    clk_sn = ""
    rst_sn = ""
    ca = 0
    cd = 0
    if has_clocks:
        clk = t.clocks[0].port
        _, clk_sn, _clk_ty = iface.resolve(clk)
    if has_reset:
        rst = t.reset_spec.port
        _, rst_sn, _rst_ty = iface.resolve(rst)
        ca = int(t.reset_spec.cycles_asserted)
        cd = int(t.reset_spec.cycles_deasserted)

    instruction_streams = [
        {
            "name": str(stream.name),
            "isa": str(stream.isa),
            "encoding": str(stream.encoding),
            "source": str(stream.source),
            "issue_protocol": str(stream.issue_protocol),
            "instruction_width": int(stream.word_bits),
            "flags": int(stream.flags),
            "count": len(stream.words),
            "instructions": [int(word) for word in stream.words],
        }
        for stream in getattr(t, "instruction_streams", [])
    ]
    external_stream_sources = [
        {
            "name": str(source.name),
            "path": str(source.path),
            "format": str(source.format),
            "hash": str(source.sha256),
            "sha256": str(source.sha256),
            "issue_protocol": str(source.issue_protocol),
            "word_bits": int(source.word_bits),
            "count": int(source.count),
            "offset": int(source.offset),
            "byte_size": int(source.byte_size),
            "chunk_size": int(source.chunk_size),
            "flags": int(source.flags),
        }
        for source in getattr(t, "external_workloads", [])
    ]
    scoreboard_policies = [
        {
            "name": str(policy.name),
            "kind": str(policy.policy),
            "policy": str(policy.policy),
            "target": str(policy.target),
            "reference": str(policy.reference),
            "signature": str(policy.signature),
            "sample_period": int(policy.sample_period),
            "max_mismatches": int(policy.max_mismatches),
            "flags": int(policy.flags),
        }
        for policy in getattr(t, "scoreboard_policies", [])
    ]

    write_ready_valid_actor_sidecars(
        actor_data_path=actor_data_path,
        top_name=str(iface.sym),
        data_width=int(data_w),
        source_payloads=source_payloads,
        ready_pattern=ready_pattern,
        start_cycle=int(start_cycle),
        actor_end_cycle=int(actor_end_cycle),
        reset_cycles=int(ca + cd) if has_reset else 0,
        clocking="single_clock" if has_clocks else "none",
        pure_wgen=pure_wgen_requested,
        transaction_count=transaction_count,
        workload_generator=workload_generator,
        source_valid_name=source_valid_sn,
        source_payload_name=source_payload_sn,
        source_ready_name=source_ready_sn,
        sink_valid_name=sink_valid_sn,
        sink_payload_name=sink_payload_sn,
        sink_ready_name=sink_ready_sn,
        source_protocol=source_protocol,
        sink_protocol=sink_protocol,
        source_actor_name=source_actor_name,
        sink_actor_name=sink_actor_name,
        scoreboard_name=scoreboard_name,
        instruction_streams=instruction_streams,
        external_stream_sources=external_stream_sources,
        scoreboard_policies=scoreboard_policies,
    )

    lines: list[str] = []
    lines.append("// Generated by pycircuit (experimental actor-fastpath TB)\n")
    lines.append("#include <array>\n")
    lines.append("#include <cstdint>\n")
    lines.append("#include <cstdlib>\n")
    lines.append("#include <filesystem>\n")
    lines.append("#include <fstream>\n")
    lines.append("#include <iostream>\n")
    lines.append("#include <stdexcept>\n")
    lines.append("#include <string>\n")
    lines.append("#include <utility>\n")
    lines.append("#include <vector>\n\n")
    lines.append("#include <cpp/pyc_tb.hpp>\n\n")
    lines.append("#include <cpp/pyc_tb_actor_v0.hpp>\n\n")
    lines.append("#include <cpp/pyc_tb_pycstb4.hpp>\n\n")
    lines.append("#include <cpp/pyc_tb_workload_v0.hpp>\n\n")
    lines.append(f"#include \"{hdr}\"\n\n")
    lines.append("using pyc::cpp::Testbench;\n\n")
    lines.append("int main() {\n")
    lines.append(f"  pyc::gen::{top} dut;\n")
    lines.append(f"  Testbench<pyc::gen::{top}> tb(dut);\n\n")
    lines.append("  const char *actor_data_env = std::getenv(\"PYC_TB_ACTOR_DATA\");\n")
    lines.append(
        f"  const std::filesystem::path actor_data_path = actor_data_env != nullptr && actor_data_env[0] != '\\0' ? std::filesystem::path(actor_data_env) : std::filesystem::path({json.dumps(str(actor_data_path))});\n"
    )
    lines.append("  const char *actor_pycstb4_check_env = std::getenv(\"PYC_TB_ACTOR_PYCSTB4_CHECK\");\n")
    lines.append("  const char *actor_from_pycstb4_env = std::getenv(\"PYC_TB_ACTOR_FROM_PYCSTB4\");\n")
    lines.append("  const char *actor_from_wgen_env = std::getenv(\"PYC_TB_ACTOR_FROM_WGEN\");\n")
    lines.append("  const char *actor_from_external_env = std::getenv(\"PYC_TB_ACTOR_FROM_EXTERNAL\");\n")
    lines.append("  const bool actor_pycstb4_check = actor_pycstb4_check_env != nullptr && actor_pycstb4_check_env[0] != '\\0';\n")
    lines.append("  const char *actor_section_summary_env = std::getenv(\"PYC_TB_ACTOR_SECTION_SUMMARY\");\n")
    lines.append("  const bool actor_section_summary = actor_section_summary_env != nullptr && actor_section_summary_env[0] != '\\0';\n")
    lines.append("  if (actor_section_summary) {\n")
    lines.append("    std::filesystem::path actor_schedule_path = actor_data_path;\n")
    lines.append("    if (actor_from_wgen_env != nullptr && actor_from_wgen_env[0] != '\\0') {\n")
    lines.append("      actor_schedule_path.replace_extension(\".schedule.wgen.pycstb4\");\n")
    lines.append("    } else {\n")
    lines.append("      actor_schedule_path.replace_extension(\".schedule.pycstb4\");\n")
    lines.append("    }\n")
    lines.append("    pyc::cpp::Pycstb4Schedule actor_section_schedule;\n")
    lines.append("    std::string actor_section_error;\n")
    lines.append("    if (!pyc::cpp::loadPycstb4Schedule(actor_schedule_path, &actor_section_schedule, &actor_section_error)) {\n")
    lines.append("      std::cerr << \"ERROR: failed to load actor PYCSTB4 section summary: \" << actor_section_error << \"\\n\";\n")
    lines.append("      return 1;\n")
    lines.append("    }\n")
    lines.append("    const auto section_summary = pyc::workload_v0::summarizePycstb4RuntimeSections(actor_section_schedule);\n")
    lines.append("    std::cerr << \"[pycstb4] instruction_streams=\" << section_summary.instruction_stream_count\n")
    lines.append("              << \" instruction_words=\" << section_summary.instruction_word_count\n")
    lines.append("              << \" external_sources=\" << section_summary.external_stream_source_count\n")
    lines.append("              << \" external_bytes=\" << section_summary.external_declared_bytes\n")
    lines.append("              << \" seeded_generators=\" << section_summary.seeded_generator_count\n")
    lines.append("              << \" seeded_transactions=\" << section_summary.seeded_transaction_count\n")
    lines.append("              << \" payload_tables=\" << section_summary.actor_payload_table_count\n")
    lines.append("              << \" payload_transactions=\" << section_summary.actor_payload_transaction_count\n")
    lines.append("              << \" scoreboard_policies=\" << section_summary.scoreboard_policy_count\n")
    lines.append("              << \" metadata_only_policies=\" << section_summary.metadata_only_policy_count\n")
    lines.append("              << \" unsupported_or_invalid=\" << section_summary.unsupported_or_invalid_count << \"\\n\";\n")
    lines.append("    for (const auto &message : section_summary.messages) {\n")
    lines.append("      std::cerr << \"[pycstb4] \" << message << \"\\n\";\n")
    lines.append("    }\n")
    lines.append("  }\n")
    lines.append("  const bool actor_from_pycstb4 = actor_from_pycstb4_env != nullptr && actor_from_pycstb4_env[0] != '\\0';\n")
    lines.append("  const bool actor_from_wgen = actor_from_wgen_env != nullptr && actor_from_wgen_env[0] != '\\0';\n")
    lines.append("  const bool actor_from_external = actor_from_external_env != nullptr && actor_from_external_env[0] != '\\0';\n")
    lines.append("  const bool actor_from_sidecar = actor_from_pycstb4 || actor_from_wgen || actor_from_external;\n")
    lines.append("  pyc::cpp::Pycstb4Schedule actor_schedule;\n")
    lines.append("  if (actor_pycstb4_check || actor_from_sidecar) {\n")
    lines.append("    std::filesystem::path actor_pycstb4_path = actor_data_path;\n")
    lines.append("    actor_pycstb4_path.replace_extension(\".schedule.pycstb4\");\n")
    lines.append("    if (actor_from_wgen) {\n")
    lines.append("      std::filesystem::path actor_wgen_pycstb4_path = actor_data_path;\n")
    lines.append("      actor_wgen_pycstb4_path.replace_extension(\".schedule.wgen.pycstb4\");\n")
    lines.append("      if (std::filesystem::exists(actor_wgen_pycstb4_path)) actor_pycstb4_path = actor_wgen_pycstb4_path;\n")
    lines.append("    }\n")
    lines.append("    std::string actor_pycstb4_error;\n")
    lines.append("    if (!pyc::cpp::loadPycstb4Schedule(actor_pycstb4_path, &actor_schedule, &actor_pycstb4_error)) {\n")
    lines.append("      std::cerr << \"ERROR: failed to load actor PYCSTB4 sidecar: \" << actor_pycstb4_error << \"\\n\";\n")
    lines.append("      return 1;\n")
    lines.append("    }\n")
    lines.append("    if (actor_schedule.actors.size() != 2u || actor_schedule.scoreboards.size() != 1u || (!actor_from_wgen && actor_schedule.actor_external_sources.size() != 2u)) {\n")
    lines.append("      std::cerr << \"ERROR: actor PYCSTB4 shape mismatch: actors=\" << actor_schedule.actors.size()\n")
    lines.append("                << \" scoreboards=\" << actor_schedule.scoreboards.size()\n")
    lines.append("                << \" external_sources=\" << actor_schedule.actor_external_sources.size() << \"\\n\";\n")
    lines.append("      return 1;\n")
    lines.append("    }\n")
    lines.append("  }\n")
    lines.append("  std::vector<std::uint32_t> source_payload_ports{1u};\n")
    lines.append("  std::vector<std::uint32_t> expected_payload_ports{4u};\n")
    lines.append("  if (actor_from_sidecar) {\n")
    lines.append("    bool found_source_payload = false;\n")
    lines.append("    bool found_expected_payload = false;\n")
    lines.append("    for (const auto &actor : actor_schedule.actors) {\n")
    lines.append("      if (actor.kind == 0u && !actor.payload_ports.empty()) {\n")
    lines.append("        source_payload_ports = actor.payload_ports;\n")
    lines.append("        found_source_payload = true;\n")
    lines.append("      }\n")
    lines.append("    }\n")
    lines.append("    for (const auto &scoreboard : actor_schedule.scoreboards) {\n")
    lines.append("      if (scoreboard.kind == 0u && !scoreboard.payload_ports.empty()) {\n")
    lines.append("        expected_payload_ports = scoreboard.payload_ports;\n")
    lines.append("        found_expected_payload = true;\n")
    lines.append("      }\n")
    lines.append("    }\n")
    lines.append("    if (!found_source_payload || !found_expected_payload) { std::cerr << \"ERROR: actor PYCSTB4 missing payload port metadata\\n\"; return 1; }\n")
    lines.append("  }\n")
    lines.append("  const bool actor_use_payload_tables = actor_from_pycstb4 && !actor_schedule.actor_payload_tables.empty();\n")
    lines.append("  const bool actor_use_workload_generator = actor_from_wgen && !actor_schedule.seeded_workload_generators.empty();\n")
    lines.append("  const bool actor_use_external_stream_source = actor_from_external && !actor_schedule.external_stream_sources.empty();\n")
    lines.append("  if (actor_from_wgen && !actor_use_workload_generator) {\n")
    lines.append("    std::cerr << \"ERROR: actor PYCSTB4 has no seeded workload generator section\\n\";\n")
    lines.append("    return 1;\n")
    lines.append("  }\n")
    lines.append("  if (actor_from_external && !actor_use_external_stream_source) {\n")
    lines.append("    std::cerr << \"ERROR: actor PYCSTB4 has no external stream source section\\n\";\n")
    lines.append("    return 1;\n")
    lines.append("  }\n")
    lines.append("  if (actor_from_pycstb4 && !actor_use_payload_tables && actor_schedule.actor_payload_blob.empty()) {\n")
    lines.append("    std::cerr << \"ERROR: actor PYCSTB4 has neither payload table nor payload blob\\n\";\n")
    lines.append("    return 1;\n")
    lines.append("  }\n")
    lines.append("  std::ifstream actor_data_file;\n")
    lines.append("  std::size_t actor_payload_pos = 0;\n")
    lines.append("  if (!actor_from_sidecar) {\n")
    lines.append("    actor_data_file.open(actor_data_path, std::ios::binary);\n")
    lines.append("    if (!actor_data_file) { std::cerr << \"ERROR: failed to open actor data: \" << actor_data_path << \"\\n\"; return 1; }\n")
    lines.append("  }\n")
    lines.append("  auto read_actor_bytes = [&](char *dst, std::size_t len) {\n")
    lines.append("    if (actor_from_sidecar) {\n")
    lines.append("      if (actor_use_payload_tables) throw std::runtime_error(\"actor payload table path does not expose raw bytes\");\n")
    lines.append("      if (actor_use_workload_generator) throw std::runtime_error(\"actor workload generator path does not expose raw bytes\");\n")
    lines.append("      if (actor_use_external_stream_source) throw std::runtime_error(\"actor external stream source path does not expose raw bytes\");\n")
    lines.append("      if (actor_payload_pos + len > actor_schedule.actor_payload_blob.size()) throw std::runtime_error(\"truncated actor PYCSTB4 payload blob\");\n")
    lines.append("      for (std::size_t i = 0; i < len; ++i) dst[i] = static_cast<char>(actor_schedule.actor_payload_blob[actor_payload_pos + i]);\n")
    lines.append("      actor_payload_pos += len;\n")
    lines.append("      return;\n")
    lines.append("    }\n")
    lines.append("    actor_data_file.read(dst, static_cast<std::streamsize>(len));\n")
    lines.append("    if (!actor_data_file) throw std::runtime_error(\"truncated actor data file\");\n")
    lines.append("  };\n")
    lines.append("  auto read_u32 = [&]() -> std::uint32_t { std::uint32_t value = 0; read_actor_bytes(reinterpret_cast<char*>(&value), sizeof(value)); return value; };\n")
    lines.append("  auto read_u64 = [&]() -> std::uint64_t { std::uint64_t value = 0; read_actor_bytes(reinterpret_cast<char*>(&value), sizeof(value)); return value; };\n")
    lines.append("  std::uint32_t actor_data_transactions = 0;\n")
    lines.append("  const pyc::cpp::Pycstb4SeededWorkloadGenerator *workload_generator_meta = nullptr;\n")
    lines.append("  const pyc::cpp::Pycstb4ExternalStreamSource *external_stream_source_meta = nullptr;\n")
    lines.append("  const pyc::cpp::Pycstb4ActorPayloadTable *source_payload_table_meta = nullptr;\n")
    lines.append("  const pyc::cpp::Pycstb4ActorPayloadTable *expected_payload_table_meta = nullptr;\n")
    lines.append("  std::vector<pyc::actor_v0::Transaction> source_transactions;\n")
    lines.append("  std::vector<pyc::actor_v0::Transaction> expected_transactions;\n")
    lines.append("  if (actor_use_external_stream_source) {\n")
    lines.append("    for (const auto &candidate : actor_schedule.external_stream_sources) {\n")
    lines.append("      if (pyc::workload_v0::WorkloadSourceRegistry::supportsExternalPayloadSource(candidate)) { external_stream_source_meta = &candidate; break; }\n")
    lines.append("    }\n")
    lines.append("    if (external_stream_source_meta == nullptr) { std::cerr << \"ERROR: actor PYCSTB4 has no supported external payload source\\n\"; return 1; }\n")
    lines.append("    if ((external_stream_source_meta->byte_size % 4u) != 0u) { std::cerr << \"ERROR: external payload source byte_size is not u32 aligned\\n\"; return 1; }\n")
    lines.append("    actor_data_transactions = static_cast<std::uint32_t>(external_stream_source_meta->byte_size / 4u);\n")
    lines.append("  } else if (actor_use_workload_generator) {\n")
    lines.append("    for (const auto &candidate : actor_schedule.seeded_workload_generators) {\n")
    lines.append("      if (candidate.output_ports == source_payload_ports) { workload_generator_meta = &candidate; break; }\n")
    lines.append("    }\n")
    lines.append("    if (workload_generator_meta == nullptr) { std::cerr << \"ERROR: actor PYCSTB4 workload generator does not match source payload ports\\n\"; return 1; }\n")
    lines.append("    actor_data_transactions = static_cast<std::uint32_t>(workload_generator_meta->count);\n")
    lines.append("  } else if (actor_use_payload_tables) {\n")
    lines.append("    auto find_payload_table = [&](const std::vector<std::uint32_t> &payload_ports) -> const pyc::cpp::Pycstb4ActorPayloadTable * {\n")
    lines.append("      for (const auto &table : actor_schedule.actor_payload_tables) {\n")
    lines.append("        if (table.payload_ports == payload_ports) return &table;\n")
    lines.append("      }\n")
    lines.append("      return nullptr;\n")
    lines.append("    };\n")
    lines.append("    source_payload_table_meta = find_payload_table(source_payload_ports);\n")
    lines.append("    expected_payload_table_meta = find_payload_table(expected_payload_ports);\n")
    lines.append("    if (source_payload_table_meta == nullptr || expected_payload_table_meta == nullptr) { std::cerr << \"ERROR: actor PYCSTB4 payload table missing source or expected table\\n\"; return 1; }\n")
    lines.append("    if (source_payload_table_meta->transaction_count != expected_payload_table_meta->transaction_count) { std::cerr << \"ERROR: actor PYCSTB4 payload table transaction count mismatch\\n\"; return 1; }\n")
    lines.append("    actor_data_transactions = source_payload_table_meta->transaction_count;\n")
    lines.append("  } else {\n")
    lines.append("    std::array<char, 8> actor_magic{};\n")
    lines.append("    read_actor_bytes(actor_magic.data(), actor_magic.size());\n")
    lines.append("    if (std::string(actor_magic.data(), actor_magic.size()) != std::string(\"PACTR0\\0\\0\", 8)) { std::cerr << \"ERROR: bad actor data magic\\n\"; return 1; }\n")
    lines.append("    const std::uint32_t actor_data_version = read_u32();\n")
    lines.append("    actor_data_transactions = read_u32();\n")
    lines.append("    const std::uint32_t actor_data_source_ports = read_u32();\n")
    lines.append("    const std::uint32_t actor_data_expected_ports = read_u32();\n")
    lines.append("    (void)read_u32();\n")
    lines.append("    if (actor_data_version != 1 || actor_data_source_ports != static_cast<std::uint32_t>(source_payload_ports.size()) || actor_data_expected_ports != static_cast<std::uint32_t>(expected_payload_ports.size())) { std::cerr << \"ERROR: unsupported actor data shape\\n\"; return 1; }\n")
    lines.append("    source_transactions.reserve(actor_data_transactions);\n")
    lines.append("    expected_transactions.reserve(actor_data_transactions);\n")
    lines.append("    for (std::uint32_t tx = 0; tx < actor_data_transactions; ++tx) {\n")
    lines.append("      pyc::actor_v0::Transaction source_tx;\n")
    lines.append("      source_tx.payload.reserve(source_payload_ports.size());\n")
    lines.append("      for (const auto port : source_payload_ports) source_tx.payload.push_back(pyc::actor_v0::PayloadWord{port, read_u64()});\n")
    lines.append("      pyc::actor_v0::Transaction expected_tx;\n")
    lines.append("      expected_tx.payload.reserve(expected_payload_ports.size());\n")
    lines.append("      for (const auto port : expected_payload_ports) expected_tx.payload.push_back(pyc::actor_v0::PayloadWord{port, read_u64()});\n")
    lines.append("      source_transactions.push_back(std::move(source_tx));\n")
    lines.append("      expected_transactions.push_back(std::move(expected_tx));\n")
    lines.append("    }\n")
    lines.append("  }\n\n")
    lines.append("  auto read_port = [&](std::uint32_t port) -> std::uint64_t {\n")
    lines.append("    switch (port) {\n")
    lines.append(f"    case 0u: return dut.{source_valid_sn}.value();\n")
    lines.append(f"    case 1u: return dut.{source_payload_sn}.value();\n")
    lines.append(f"    case 2u: return dut.{source_ready_sn}.value();\n")
    lines.append(f"    case 3u: return dut.{sink_valid_sn}.value();\n")
    lines.append(f"    case 4u: return dut.{sink_payload_sn}.value();\n")
    lines.append(f"    case 5u: return dut.{sink_ready_sn}.value();\n")
    lines.append("    default: throw std::runtime_error(\"invalid actor port read\");\n")
    lines.append("    }\n")
    lines.append("  };\n")
    lines.append("  auto write_port = [&](std::uint32_t port, std::uint64_t value) {\n")
    lines.append("    switch (port) {\n")
    lines.append(f"    case 0u: dut.{source_valid_sn} = pyc::cpp::Wire<1>(value); return;\n")
    lines.append(f"    case 1u: dut.{source_payload_sn} = pyc::cpp::Wire<{data_w}>(value); return;\n")
    lines.append(f"    case 5u: dut.{sink_ready_sn} = pyc::cpp::Wire<1>(value); return;\n")
    lines.append("    default: throw std::runtime_error(\"invalid actor port write\");\n")
    lines.append("    }\n")
    lines.append("  };\n")
    lines.append("  pyc::actor_v0::ReadyValidActorRuntime runtime(pyc::actor_v0::PortIo{read_port, write_port});\n")
    lines.append("  auto readyPatternFromActor = [](const pyc::cpp::Pycstb4ActorRecord &actor) -> pyc::actor_v0::ReadyPattern {\n")
    lines.append("    pyc::actor_v0::ReadyPattern pattern;\n")
    lines.append("    if (actor.ready_kind == 2u) {\n")
    lines.append("      pattern.kind = pyc::actor_v0::ReadyPattern::Kind::PeriodicDrive;\n")
    lines.append("      pattern.period = actor.ready_period;\n")
    lines.append("      pattern.active_cycles = actor.ready_active_cycles;\n")
    lines.append("      pattern.phase_cycle = actor.ready_phase_cycle;\n")
    lines.append("      pattern.start_cycle = actor.ready_start_cycle;\n")
    lines.append("      pattern.end_cycle = actor.ready_end_cycle;\n")
    lines.append("      pattern.active_value = actor.ready_active_value;\n")
    lines.append("      pattern.default_value = actor.ready_default_value;\n")
    lines.append("    } else if (actor.ready_kind == 1u) {\n")
    lines.append("      pattern.kind = pyc::actor_v0::ReadyPattern::Kind::Constant;\n")
    lines.append("      pattern.constant_value = actor.ready_default_value;\n")
    lines.append("    }\n")
    lines.append("    return pattern;\n")
    lines.append("  };\n")
    lines.append("  if (actor_from_sidecar && !actor_use_workload_generator && !actor_use_payload_tables && !actor_use_external_stream_source) {\n")
    lines.append("    if (actor_schedule.scoreboards.size() != 1u) { std::cerr << \"ERROR: actor-fastpath v0 expects exactly one scoreboard\\n\"; return 1; }\n")
    lines.append("    const auto &scoreboard_meta = actor_schedule.scoreboards.front();\n")
    lines.append("    if (scoreboard_meta.kind != 0u) { std::cerr << \"ERROR: actor-fastpath v0 supports only ordered scoreboard\\n\"; return 1; }\n")
    lines.append("    runtime.addScoreboard(pyc::actor_v0::OrderedScoreboard(scoreboard_meta.name, scoreboard_meta.payload_ports, std::move(expected_transactions)));\n")
    lines.append("    bool added_source = false;\n")
    lines.append("    bool added_sink = false;\n")
    lines.append("    for (const auto &actor_meta : actor_schedule.actors) {\n")
    lines.append("      if (actor_meta.kind == 0u) {\n")
    lines.append("        if (actor_meta.policy != 0u) { std::cerr << \"ERROR: actor-fastpath v0 source policy must be hold_valid\\n\"; return 1; }\n")
    lines.append("        runtime.addSource(pyc::actor_v0::ReadyValidSource(actor_meta.name, actor_meta.valid_port, actor_meta.ready_port, std::move(source_transactions), actor_meta.start_cycle, actor_meta.end_cycle));\n")
    lines.append("        added_source = true;\n")
    lines.append("      } else if (actor_meta.kind == 1u) {\n")
    lines.append("        if (actor_meta.policy != 1u) { std::cerr << \"ERROR: actor-fastpath v0 sink policy must be on_handshake\\n\"; return 1; }\n")
    lines.append("        pyc::actor_v0::OrderedScoreboard *scoreboard_ptr = actor_meta.scoreboard_ref == 0xffffffffu ? nullptr : runtime.scoreboard(actor_meta.scoreboard_ref);\n")
    lines.append("        if (scoreboard_ptr == nullptr) { std::cerr << \"ERROR: actor-fastpath sink missing scoreboard binding\\n\"; return 1; }\n")
    lines.append("        runtime.addSink(pyc::actor_v0::ReadyValidSink(actor_meta.name, actor_meta.valid_port, actor_meta.ready_port, actor_meta.payload_ports, readyPatternFromActor(actor_meta), actor_meta.start_cycle, actor_meta.end_cycle, scoreboard_ptr));\n")
    lines.append("        added_sink = true;\n")
    lines.append("      } else {\n")
    lines.append("        std::cerr << \"ERROR: actor-fastpath encountered unsupported actor kind=\" << actor_meta.kind << \"\\n\";\n")
    lines.append("        return 1;\n")
    lines.append("      }\n")
    lines.append("    }\n")
    lines.append("    if (!added_source || !added_sink) { std::cerr << \"ERROR: actor-fastpath PYCSTB4 metadata did not instantiate source and sink\\n\"; return 1; }\n")
    lines.append("  } else if (!actor_use_workload_generator && !actor_use_payload_tables && !actor_use_external_stream_source) {\n")
    lines.append(f"  runtime.addScoreboard(pyc::actor_v0::OrderedScoreboard({json.dumps(scoreboard_name)}, std::vector<std::uint32_t>{{4u}}, std::move(expected_transactions)));\n")
    lines.append(
        f"  runtime.addSource(pyc::actor_v0::ReadyValidSource({json.dumps(source_actor_name)}, 0u, 2u, std::move(source_transactions), {start_cycle}ull, {actor_end_cycle}ull));\n"
    )
    lines.append("  pyc::actor_v0::ReadyPattern ready_pattern;\n")
    if ready_pattern["kind"] == "periodic":
        lines.append("  ready_pattern.kind = pyc::actor_v0::ReadyPattern::Kind::PeriodicDrive;\n")
        lines.append("  ready_pattern.active_value = 0ull;\n")
        lines.append("  ready_pattern.default_value = 1ull;\n")
        lines.append("  ready_pattern.start_cycle = 0ull;\n")
        lines.append(f"  ready_pattern.end_cycle = {actor_end_cycle}ull;\n")
        lines.append(f"  ready_pattern.period = {int(ready_pattern['period'])}ull;\n")
        lines.append(f"  ready_pattern.active_cycles = {int(ready_pattern['active_cycles'])}ull;\n")
        lines.append(f"  ready_pattern.phase_cycle = {int(ready_pattern['phase_cycle'])}ull;\n")
    else:
        lines.append("  ready_pattern.kind = pyc::actor_v0::ReadyPattern::Kind::Constant;\n")
        lines.append("  ready_pattern.constant_value = 1ull;\n")
    lines.append(
        f"  runtime.addSink(pyc::actor_v0::ReadyValidSink({json.dumps(sink_actor_name)}, 3u, 5u, std::vector<std::uint32_t>{{4u}}, ready_pattern, {start_cycle}ull, {actor_end_cycle}ull, runtime.scoreboard(0)));\n\n"
    )
    lines.append("  }\n\n")
    lines.append("  const char *trace_dir_env = std::getenv(\"PYC_TRACE_DIR\");\n")
    lines.append("  const bool trace_env_enabled = (trace_dir_env != nullptr) && (std::string(trace_dir_env).size() != 0);\n")
    lines.append("  if (trace_env_enabled) {\n")
    lines.append("    std::filesystem::path out_dir = std::filesystem::path(trace_dir_env);\n")
    lines.append(f"    out_dir /= \"tb_{iface.sym}\";\n")
    lines.append("    std::filesystem::create_directories(out_dir);\n")
    lines.append(f"    tb.enableVcd((out_dir / \"tb_{iface.sym}.vcd\").string(), /*top=*/\"tb_{iface.sym}\");\n")
    for sn in [*iface.in_names, *iface.out_names]:
        lines.append(f"    tb.vcdTrace(dut.{sn}, \"{sn}\");\n")
    lines.append("  }\n\n")
    if has_clocks:
        for c in t.clocks:
            dir_, sn, _ = iface.resolve(c.port)
            if dir_ != "in":
                raise SystemExit(f"clock must be an input port, got output: {c.port!r}")
            lines.append(
                f"  tb.addClock(dut.{sn}, /*halfPeriodSteps=*/{int(c.half_period_steps)}, /*phaseSteps=*/{int(c.phase_steps)}, /*startHigh=*/{str(bool(c.start_high)).lower()});\n"
            )
    if has_reset:
        lines.append(f"  tb.reset(dut.{rst_sn}, /*cyclesAsserted=*/{int(ca)}, /*cyclesDeasserted=*/{int(cd)});\n\n")
    lines.append("  auto eval = [&](std::uint64_t cycle) {\n")
    if has_clocks:
        lines.append("    tb.runCycleAutoTrace(cycle, nullptr);\n")
    else:
        lines.append("    (void)cycle;\n")
        lines.append("    tb.runSteps(1);\n")
    lines.append("  };\n")
    lines.append("  std::uint64_t mismatches = 0;\n")
    lines.append("  std::uint64_t actor_results = 0;\n")
    lines.append("  if (actor_use_workload_generator || actor_use_payload_tables || actor_use_external_stream_source) {\n")
    lines.append("    if (actor_use_workload_generator && workload_generator_meta == nullptr) { std::cerr << \"ERROR: missing workload generator metadata\\n\"; return 1; }\n")
    lines.append("    if (actor_use_payload_tables && source_payload_table_meta == nullptr) { std::cerr << \"ERROR: missing source payload table metadata\\n\"; return 1; }\n")
    lines.append("    if (actor_use_external_stream_source && external_stream_source_meta == nullptr) { std::cerr << \"ERROR: missing external payload stream metadata\\n\"; return 1; }\n")
    lines.append("    const pyc::cpp::Pycstb4ActorRecord *source_actor_meta = nullptr;\n")
    lines.append("    const pyc::cpp::Pycstb4ActorRecord *sink_actor_meta = nullptr;\n")
    lines.append("    for (const auto &actor_meta : actor_schedule.actors) {\n")
    lines.append("      if (actor_meta.kind == 0u) source_actor_meta = &actor_meta;\n")
    lines.append("      if (actor_meta.kind == 1u) sink_actor_meta = &actor_meta;\n")
    lines.append("    }\n")
    lines.append("    if (source_actor_meta == nullptr || sink_actor_meta == nullptr) { std::cerr << \"ERROR: workload generator path missing source/sink actor metadata\\n\"; return 1; }\n")
    lines.append("    auto run_transaction_source = [&](pyc::workload_v0::GeneratedTransactionSource transaction_source) {\n")
    lines.append("      pyc::workload_v0::GeneratedReadyValidRuntime generated_runtime(\n")
    lines.append("          pyc::actor_v0::PortIo{read_port, write_port},\n")
    lines.append("          std::move(transaction_source),\n")
    lines.append("          source_actor_meta->valid_port,\n")
    lines.append("          source_actor_meta->ready_port,\n")
    lines.append("          source_actor_meta->payload_ports,\n")
    lines.append("          sink_actor_meta->valid_port,\n")
    lines.append("          sink_actor_meta->ready_port,\n")
    lines.append("          sink_actor_meta->payload_ports,\n")
    lines.append("          readyPatternFromActor(*sink_actor_meta),\n")
    lines.append("          source_actor_meta->start_cycle,\n")
    lines.append("          source_actor_meta->end_cycle);\n")
    lines.append(f"      return generated_runtime.run(0ull, {actor_end_cycle}ull, eval);\n")
    lines.append("    };\n")
    lines.append("    const auto generated_result = [&]() {\n")
    lines.append("      if (actor_use_workload_generator) return run_transaction_source(pyc::workload_v0::GeneratedTransactionSource(*workload_generator_meta));\n")
    lines.append("      if (actor_use_payload_tables) return run_transaction_source(pyc::workload_v0::GeneratedTransactionSource(*source_payload_table_meta));\n")
    lines.append(f"      return run_transaction_source(pyc::workload_v0::GeneratedTransactionSource(*external_stream_source_meta, source_payload_ports, {int(data_w)}u));\n")
    lines.append("    }();\n")
    lines.append("    mismatches = generated_result.mismatch_count;\n")
    lines.append("    actor_results = generated_result.actual_count;\n")
    lines.append(f"    std::cerr << \"[actor-fastpath] scoreboard={scoreboard_name} expected=\" << generated_result.expected_count << \" actual=\" << generated_result.actual_count << \" mismatches=\" << generated_result.mismatch_count << \"\\n\";\n")
    lines.append("    for (const auto &message : generated_result.messages) std::cerr << \"  \" << message << \"\\n\";\n")
    lines.append("  } else {\n")
    lines.append(f"    const auto result = runtime.run(0ull, {actor_end_cycle}ull, eval);\n")
    lines.append("    for (const auto &scoreboard : result.scoreboards) {\n")
    lines.append("      mismatches += scoreboard.mismatch_count;\n")
    lines.append("      actor_results += scoreboard.actual_count;\n")
    lines.append("      std::cerr << \"[actor-fastpath] scoreboard=\" << scoreboard.name << \" expected=\" << scoreboard.expected_count << \" actual=\" << scoreboard.actual_count << \" mismatches=\" << scoreboard.mismatch_count << \"\\n\";\n")
    lines.append("      for (const auto &message : scoreboard.messages) std::cerr << \"  \" << message << \"\\n\";\n")
    lines.append("    }\n")
    lines.append("  }\n")
    lines.append("  if (mismatches != 0) return 1;\n")
    lines.append(f"  std::cerr << \"[SIM_PERF] task=actor-fastpath cycles={actor_end_cycle + 1} commands=\" << actor_data_transactions << \" results=\" << actor_results << \"\\n\";\n")
    lines.append("  return 0;\n")
    lines.append("}\n")
    return "".join(lines)


def _render_tb_cpp(
    iface: _TopIface,
    t: Tb,
    *,
    trace_plan: TracePlan | None = None,
    schedule_mode: str = "inline",
    schedule_path: Path | None = None,
    schedule_format: str = "pycstb3",
    actor_data_path: Path | None = None,
) -> str:
    mode = str(schedule_mode).strip().lower()
    if mode == "runtime-loop":
        return _render_tb_cpp_runtime_loop(
            iface,
            t,
            trace_plan=trace_plan,
            schedule_path=schedule_path,
            schedule_format=schedule_format,
        )
    if mode == "actor-fastpath":
        return _render_tb_cpp_actor_fastpath(
            iface,
            t,
            trace_plan=trace_plan,
            actor_data_path=actor_data_path,
        )
    if mode != "inline":
        raise SystemExit(f"unsupported C++ TB schedule mode: {schedule_mode!r}")

    has_clocks = bool(t.clocks)
    has_reset = t.reset_spec is not None
    if has_reset and not has_clocks:
        raise SystemExit("tb() with reset requires at least one clock via t.clock(...)")

    top = _sanitize_id(iface.sym)
    hdr = f"{iface.sym}.hpp"

    def mask_value(v: int | bool, width: int) -> int:
        if isinstance(v, bool):
            vv = 1 if v else 0
        else:
            vv = int(v)
        if width <= 0:
            raise SystemExit("internal: invalid width")
        return vv & ((1 << width) - 1)

    def wire_literal(v: int | bool, width: int) -> str:
        vv = mask_value(v, width)
        words = (width + 63) // 64
        raw_words = []
        for i in range(words):
            raw_words.append(f"0x{((vv >> (64 * i)) & ((1 << 64) - 1)):x}ull")
        return f"pyc::cpp::Wire<{width}>({{{', '.join(raw_words)}}})"

    # Group actions by cycle for compact emission.
    drives_by: dict[int, list[tuple[str, int | bool, str]]] = {}
    expects_pre_by: dict[int, list[tuple[str, int | bool, str | None, str]]] = {}
    expects_post_by: dict[int, list[tuple[str, int | bool, str | None, str]]] = {}
    prints_at: dict[int, list[tuple[str, list[tuple[str, str, int]]]]] = {}
    prints_every: list[tuple[str, int, int, list[tuple[str, str, int]]]] = []
    for d in t.drives:
        dir_, sn, ty = iface.resolve(d.port)
        if dir_ != "in":
            raise SystemExit(f"drive() requires input port, got output: {d.port!r}")
        drives_by.setdefault(int(d.at), []).append((sn, d.value, ty))
    for e in t.expects:
        _dir, sn, ty = iface.resolve(e.port)
        ph = str(getattr(e, "phase", "post")).strip().lower()
        if ph == "pre":
            expects_pre_by.setdefault(int(e.at), []).append((sn, e.value, e.msg, ty))
        else:
            expects_post_by.setdefault(int(e.at), []).append((sn, e.value, e.msg, ty))

    for p in getattr(t, "prints", []):
        fmt = str(p.fmt)
        port_specs: list[tuple[str, str, int]] = []
        for raw in p.ports:
            _dir, sn, ty = iface.resolve(raw)
            w = _as_int_width(ty)
            if w > 64:
                raise SystemExit(f"print() for i{w} not supported in C++ TB generator (prototype limitation)")
            port_specs.append((str(raw), sn, w))
        if p.at is not None:
            prints_at.setdefault(int(p.at), []).append((fmt, port_specs))
        else:
            st = 0 if p.start is None else int(p.start)
            ev = 1 if p.every is None else int(p.every)
            prints_every.append((fmt, st, ev, port_specs))

    rand_specs: list[tuple[str, int, int, int, int]] = []
    if t.random_streams:
        used_ports: set[str] = set()
        for r in t.random_streams:
            dir_, sn, ty = iface.resolve(r.port)
            if dir_ != "in":
                raise SystemExit(f"random() requires input port, got output: {r.port!r}")
            if ty == "!pyc.clock" or ty == "!pyc.reset":
                raise SystemExit(f"random() cannot target clock/reset ports: {r.port!r}")
            if sn in used_ports:
                raise SystemExit(f"duplicate random() stream for port: {r.port!r}")
            used_ports.add(sn)
            w = _as_int_width(ty)
            if w > 64:
                raise SystemExit(f"random() for i{w} not supported in C++ TB generator (prototype limitation)")
            rand_specs.append((sn, w, int(r.seed), int(r.start), int(r.every)))

    clk_sn = ""
    rst_sn = ""
    ca = 0
    cd = 0
    if has_clocks:
        clk = t.clocks[0].port
        _, clk_sn, _clk_ty = iface.resolve(clk)
    if has_reset:
        rst = t.reset_spec.port
        _, rst_sn, _rst_ty = iface.resolve(rst)
        ca = int(t.reset_spec.cycles_asserted)
        cd = int(t.reset_spec.cycles_deasserted)

    lines: list[str] = []
    lines.append("// Generated by pycircuit (prototype)\n")
    lines.append("#include <algorithm>\n")
    lines.append("#include <array>\n")
    lines.append("#include <cstdint>\n")
    lines.append("#include <cstdlib>\n")
    lines.append("#include <filesystem>\n")
    lines.append("#include <iostream>\n\n")
    lines.append("#include <iterator>\n")
    lines.append("#include <string>\n")
    lines.append("#include <string_view>\n\n")
    lines.append("#include <cpp/pyc_tb.hpp>\n\n")
    lines.append("#include <cpp/pyc_trace_bin.hpp>\n\n")
    lines.append(f"#include \"{hdr}\"\n\n")
    lines.append("using pyc::cpp::Testbench;\n\n")
    lines.append("int main() {\n")
    lines.append(f"  pyc::gen::{top} dut;\n")
    lines.append(f"  Testbench<pyc::gen::{top}> tb(dut);\n\n")
    lines.append("  std::optional<pyc::cpp::PycTraceBinWriter> bin_trace;\n\n")
    if rand_specs:
        lines.append("  // Random streams (deterministic).\n")
        for sn, _w, seed, _st, _ev in rand_specs:
            seed64 = int(seed) & ((1 << 64) - 1)
            lines.append(f"  std::uint64_t rng_{sn} = 0x{seed64:x}ull;\n")
        lines.append("\n")
    lines.append("  // Optional traces (Decision 0145).\n")
    lines.append("  const char *trace_dir_env = std::getenv(\"PYC_TRACE_DIR\");\n")
    lines.append(
        "  const bool trace_env_enabled = (trace_dir_env != nullptr) && (std::string(trace_dir_env).size() != 0);\n"
    )
    lines.append(f"  const bool trace_cfg_enabled = {str(bool(trace_plan and trace_plan.enabled_signals)).lower()};\n")
    lines.append("  if (trace_env_enabled || trace_cfg_enabled) {\n")
    lines.append(
        "    std::filesystem::path out_dir = trace_env_enabled ? std::filesystem::path(trace_dir_env) : std::filesystem::path(\".\");\n"
    )
    lines.append(f"    out_dir /= \"tb_{iface.sym}\";\n")
    lines.append("    std::filesystem::create_directories(out_dir);\n")
    lines.append(f"    tb.enableVcd((out_dir / \"tb_{iface.sym}.vcd\").string(), /*top=*/\"tb_{iface.sym}\");\n")
    if trace_plan and trace_plan.enabled_signals:
        sigs = list(trace_plan.enabled_signals)
        insts = list(trace_plan.enabled_instances)
        sig_obs = dict(getattr(trace_plan, "signal_obs", {}) or {})
        # Ensure stable ordering for reproducible generated TB text.
        sigs = sorted(set(str(s) for s in sigs))
        insts = sorted(set(str(s) for s in insts))
        sig_obs = {str(k): str(v).strip().lower() for k, v in sig_obs.items() if str(k) in set(sigs)}
        tick_sigs = sorted([k for k, v in sig_obs.items() if v == "tick"])
        xfer_sigs = sorted([k for k, v in sig_obs.items() if v == "xfer"])
        lines.append("    // Trace config selected signals (generated from trace DSL).\n")
        lines.append("    static constexpr std::string_view kEnabledInstances[] = {\n")
        for s in insts:
            lines.append(f"      {json.dumps(s)},\n")
        lines.append("    };\n")
        lines.append("    static constexpr std::string_view kEnabledSignals[] = {\n")
        for s in sigs:
            lines.append(f"      {json.dumps(s)},\n")
        lines.append("    };\n")
        lines.append("    // Per-signal observation points (Decision 0113 / 0140).\n")
        lines.append(
            f"    static constexpr std::array<std::string_view, {len(tick_sigs)}> kTickObsSignals = {{\n"
        )
        for s in tick_sigs:
            lines.append(f"      {json.dumps(s)},\n")
        lines.append("    };\n")
        lines.append(
            f"    static constexpr std::array<std::string_view, {len(xfer_sigs)}> kXferObsSignals = {{\n"
        )
        for s in xfer_sigs:
            lines.append(f"      {json.dumps(s)},\n")
        lines.append("    };\n")
        lines.append(
            "    auto enabledInstance = [&](std::string_view p) -> bool {\n"
            "      return std::binary_search(std::begin(kEnabledInstances), std::end(kEnabledInstances), p);\n"
            "    };\n"
        )
        lines.append(
            "    auto enabledSignal = [&](std::string_view p) -> bool {\n"
            "      return std::binary_search(std::begin(kEnabledSignals), std::end(kEnabledSignals), p);\n"
            "    };\n"
        )
        lines.append(
            "    auto sampleAtForSignal = [&](std::string_view p) -> pyc::cpp::PycTraceBinWriter::SampleAt {\n"
            "      if (std::binary_search(kTickObsSignals.begin(), kTickObsSignals.end(), p))\n"
            "        return pyc::cpp::PycTraceBinWriter::SampleAt::Tick;\n"
            "      if (std::binary_search(kXferObsSignals.begin(), kXferObsSignals.end(), p))\n"
            "        return pyc::cpp::PycTraceBinWriter::SampleAt::Commit;\n"
            "      return pyc::cpp::PycTraceBinWriter::SampleAt::Auto;\n"
            "    };\n"
        )
        lines.append("    dut.pyc_trace_vcd(tb, /*prefix=*/\"dut\", enabledInstance, enabledSignal);\n")
        lines.append("    // Binary trace event stream (Decision 0016).\n")
        lines.append("    pyc::cpp::ProbeRegistry reg;\n")
        lines.append("    dut.pyc_register_probes(reg, /*prefix=*/\"dut\");\n")
        lines.append("    std::vector<const pyc::cpp::ProbeRegistry::Entry *> trace_probes;\n")
        lines.append("    std::vector<pyc::cpp::PycTraceBinWriter::SampleAt> trace_sample_at;\n")
        lines.append("    trace_probes.reserve(std::size(kEnabledSignals));\n")
        lines.append("    trace_sample_at.reserve(std::size(kEnabledSignals));\n")
        lines.append("    for (auto p : kEnabledSignals) {\n")
        lines.append(
            "      if (const auto *e = reg.findByPath(p)) { trace_probes.push_back(e); trace_sample_at.push_back(sampleAtForSignal(p)); }\n"
        )
        lines.append("    }\n")
        lines.append("    bin_trace.emplace();\n")
        lines.append(
            f"    if (!bin_trace->open(out_dir / \"tb_{iface.sym}.pyctrace\", std::move(trace_probes), /*external_manifest=*/true, std::move(trace_sample_at))) {{\n"
        )
        lines.append("      std::cerr << \"WARN: failed to open pyc binary trace output\\n\";\n")
        lines.append("      bin_trace.reset();\n")
        lines.append("    }\n")
    else:
        for sn in [*iface.in_names, *iface.out_names]:
            lines.append(f"    tb.vcdTrace(dut.{sn}, \"{sn}\");\n")
    lines.append("  }\n\n")

    if has_clocks:
        for c in t.clocks:
            dir_, sn, _ = iface.resolve(c.port)
            if dir_ != "in":
                raise SystemExit(f"clock must be an input port, got output: {c.port!r}")
            lines.append(
                f"  tb.addClock(dut.{sn}, /*halfPeriodSteps=*/{int(c.half_period_steps)}, /*phaseSteps=*/{int(c.phase_steps)}, /*startHigh=*/{str(bool(c.start_high)).lower()});\n"
            )
    if has_reset:
        lines.append("  if (bin_trace) {\n")
        lines.append("    const std::uint64_t __pyc_reset_assert_cycle = 0ull;\n")
        lines.append(
            f"    const std::uint64_t __pyc_reset_deassert_cycle = ({int(ca)}ull == 0ull) ? 0ull : ({int(ca)}ull - 1ull);\n"
        )
        lines.append(
            "    bin_trace->writeInvalidate(__pyc_reset_assert_cycle, pyc::cpp::PycTraceBinWriter::Phase::Tick, "
            "\"global\", pyc::cpp::PycTraceBinWriter::InvalidateReason::WarmReset, \"global\", \"tb.reset\");\n"
        )
        lines.append(
            "    bin_trace->writeResetAssert(__pyc_reset_assert_cycle, pyc::cpp::PycTraceBinWriter::Phase::Tick, "
            "\"global\", pyc::cpp::PycTraceBinWriter::ResetKind::Warm);\n"
        )
        lines.append(
            "    bin_trace->writeResetDeassert(__pyc_reset_deassert_cycle, pyc::cpp::PycTraceBinWriter::Phase::Tick, "
            "\"global\", pyc::cpp::PycTraceBinWriter::ResetKind::Warm);\n"
        )
        lines.append("  }\n")
        lines.append(f"  tb.reset(dut.{rst_sn}, /*cyclesAsserted=*/{int(ca)}, /*cyclesDeasserted=*/{int(cd)});\n\n")

    if trace_plan and trace_plan.enabled_signals and trace_plan.window:
        begin = trace_plan.window.begin_cycle
        end = trace_plan.window.end_cycle
        if begin is not None and end is not None:
            hp = int(t.clocks[0].half_period_steps) if has_clocks else 0
            steps_per_cycle = 1 if not has_clocks else max(1, 2 * hp)
            lines.append("  // Bounded trace window (cycles are relative to post-reset cycle 0).\n")
            lines.append("  if (trace_cfg_enabled) {\n")
            lines.append("    const std::uint64_t trace_base_steps = tb.timeSteps();\n")
            lines.append(f"    const std::uint64_t steps_per_cycle = {int(steps_per_cycle)}ull;\n")
            lines.append(
                f"    tb.setVcdWindow(trace_base_steps + ({int(begin)}ull * steps_per_cycle), "
                f"trace_base_steps + (({int(end)}ull + 1ull) * steps_per_cycle) - 1ull);\n"
            )
            lines.append("  }\n\n")

    lines.append(f"  const std::uint64_t timeout_cycles = {int(t.timeout_cycles)}ull;\n")
    lines.append("  bool ok = false;\n")
    lines.append("  for (std::uint64_t cyc = 0; cyc < timeout_cycles; ++cyc) {\n")

    if rand_specs:
        lines.append("    // Random drives for this cycle (applied before explicit drives).\n")
        for sn, w, _seed, st, ev in rand_specs:
            mask = (1 << w) - 1 if w < 64 else (1 << 64) - 1
            lines.append(
                f"    if (cyc >= {int(st)}ull && ((cyc - {int(st)}ull) % {int(ev)}ull) == 0ull) {{\n"
                f"      rng_{sn} = rng_{sn} * 6364136223846793005ull + 1ull;\n"
                f"      dut.{sn} = pyc::cpp::Wire<{w}>(0x{mask:x}ull & rng_{sn});\n"
                f"    }}\n"
            )
        lines.append("\n")

    if drives_by:
        lines.append("    switch (cyc) {\n")
        for cyc in sorted(drives_by.keys()):
            lines.append(f"    case {cyc}:\n")
            for sn, val, ty in drives_by[cyc]:
                w = _as_int_width(ty)
                lines.append(f"      dut.{sn} = {wire_literal(val, w)};\n")
            lines.append("      break;\n")
        lines.append("    default: break;\n")
        lines.append("    }\n")

    if expects_pre_by:
        # In the generated C++ TB, combinational logic only updates when we call
        # `dut.comb()`. For pre-step (TICK-OBS) sampling, ensure values reflect
        # the drives applied for this cycle before checking expectations.
        lines.append("    dut.comb();\n")
        lines.append("    // Pre-step expects for this cycle.\n")
        lines.append("    switch (cyc) {\n")
        for cyc in sorted(expects_pre_by.keys()):
            lines.append(f"    case {cyc}: {{\n")
            for sn, val, msg, ty in expects_pre_by[cyc]:
                w = _as_int_width(ty)
                vv = mask_value(val, w)
                exp = wire_literal(val, w)
                m = msg if msg is not None else f"{sn} mismatch"
                if w == 1:
                    lines.append(
                        f"      if (dut.{sn}.value() != {vv}u) {{ std::cerr << \"ERROR(pre): {m}: got=\" << dut.{sn}.value() << \" exp={vv}\\n\"; return 1; }}\n"
                    )
                elif w <= 64:
                    lines.append(
                        f"      if (dut.{sn}.value() != {vv}u) {{ std::cerr << \"ERROR(pre): {m}: got=0x\" << std::hex << dut.{sn}.value() << \" exp=0x{vv:x}\" << std::dec << \"\\n\"; return 1; }}\n"
                    )
                else:
                    lines.append(f"      if (!(dut.{sn} == {exp})) {{ std::cerr << \"ERROR(pre): {m}\\n\"; return 1; }}\n")
            lines.append("      break; }\n")
        lines.append("    default: break;\n")
        lines.append("    }\n")

    if has_clocks:
        if trace_plan and trace_plan.enabled_signals and trace_plan.window:
            begin = trace_plan.window.begin_cycle
            end = trace_plan.window.end_cycle
            if begin is not None and end is not None:
                lines.append(
                    f"    tb.runCycleAutoTrace(cyc, (bin_trace && cyc >= {int(begin)}ull && cyc <= {int(end)}ull) ? &*bin_trace : nullptr);\n"
                )
            else:
                lines.append("    tb.runCycleAutoTrace(cyc, bin_trace ? &*bin_trace : nullptr);\n")
        else:
            lines.append("    tb.runCycleAutoTrace(cyc, bin_trace ? &*bin_trace : nullptr);\n")
    else:
        lines.append("    tb.runSteps(1);\n")

    # Binary trace sampling is performed inside Testbench stepping (Decision 0113).

    if expects_post_by:
        lines.append("    // Post-step expects for this cycle.\n")
        lines.append("    switch (cyc) {\n")
        for cyc in sorted(expects_post_by.keys()):
            lines.append(f"    case {cyc}: {{\n")
            for sn, val, msg, ty in expects_post_by[cyc]:
                w = _as_int_width(ty)
                vv = mask_value(val, w)
                exp = wire_literal(val, w)
                m = msg if msg is not None else f"{sn} mismatch"
                # Print decimal for i1, hex for <=64 wider signals.
                if w == 1:
                    lines.append(
                        f"      if (dut.{sn}.value() != {vv}u) {{ std::cerr << \"ERROR: {m}: got=\" << dut.{sn}.value() << \" exp={vv}\\n\"; return 1; }}\n"
                    )
                elif w <= 64:
                    lines.append(
                        f"      if (dut.{sn}.value() != {vv}u) {{ std::cerr << \"ERROR: {m}: got=0x\" << std::hex << dut.{sn}.value() << \" exp=0x{vv:x}\" << std::dec << \"\\n\"; return 1; }}\n"
                    )
                else:
                    lines.append(f"      if (!(dut.{sn} == {exp})) {{ std::cerr << \"ERROR: {m}\\n\"; return 1; }}\n")
            lines.append("      break; }\n")
        lines.append("    default: break;\n")
        lines.append("    }\n")

    if prints_at or prints_every:
        if prints_at:
            lines.append("    // Per-cycle prints.\n")
            lines.append("    switch (cyc) {\n")
            for cyc in sorted(prints_at.keys()):
                lines.append(f"    case {cyc}: {{\n")
                for fmt, ports in prints_at[cyc]:
                    msg_lit = json.dumps(f" {fmt}")
                    lines.append(f"      std::cerr << \"[tb] cyc=\" << cyc << {msg_lit}")
                    for raw, sn, w in ports:
                        raw_lit = json.dumps(f" {raw}=")
                        if w == 1:
                            lines.append(f" << {raw_lit} << dut.{sn}.value()")
                        else:
                            lines.append(f" << {raw_lit} << \"0x\" << std::hex << dut.{sn}.value() << std::dec")
                    lines.append(" << \"\\n\";\n")
                lines.append("      break; }\n")
            lines.append("    default: break;\n")
            lines.append("    }\n")
        if prints_every:
            lines.append("    // Periodic prints.\n")
            for fmt, st, ev, ports in prints_every:
                msg_lit = json.dumps(f" {fmt}")
                lines.append(f"    if (cyc >= {st}ull && ((cyc - {st}ull) % {ev}ull) == 0ull) {{\n")
                lines.append(f"      std::cerr << \"[tb] cyc=\" << cyc << {msg_lit}")
                for raw, sn, w in ports:
                    raw_lit = json.dumps(f" {raw}=")
                    if w == 1:
                        lines.append(f" << {raw_lit} << dut.{sn}.value()")
                    else:
                        lines.append(f" << {raw_lit} << \"0x\" << std::hex << dut.{sn}.value() << std::dec")
                lines.append(" << \"\\n\";\n")
                lines.append("    }\n")

    if t.finish_cycle is not None:
        lines.append(f"    if (cyc == {int(t.finish_cycle)}ull) {{ ok = true; break; }}\n")

    lines.append("  }\n")
    lines.append("  if (!ok) { std::cerr << \"TIMEOUT\\n\"; return 1; }\n")
    lines.append("  std::cerr << \"OK\\n\";\n")
    lines.append("  return 0;\n")
    lines.append("}\n")
    return "".join(lines)


def _render_tb_sv(iface: _TopIface, t: Tb, *, trace_plan: TracePlan | None = None) -> str:
    has_clocks = bool(t.clocks)
    has_reset = t.reset_spec is not None
    if has_reset and not has_clocks:
        raise SystemExit("tb() with reset requires at least one clock via t.clock(...)")

    top = str(iface.sym)
    mod_name = top  # func sym name is already a valid Verilog identifier in this repo.

    def sv_lit(width: int, v: int | bool) -> str:
        if isinstance(v, bool):
            vv = 1 if v else 0
        else:
            vv = int(v)
        if width <= 0:
            raise SystemExit("internal: invalid width")
        vv &= (1 << width) - 1
        if width == 1:
            return f"1'b{vv}"
        return f"{width}'h{vv:x}"

    def decl(name: str, ty: str) -> str:
        w = _as_int_width(ty)
        if w == 1:
            return f"  logic {name};\n"
        return f"  logic [{w - 1}:0] {name};\n"

    drives_by: dict[int, list[tuple[str, int | bool, str]]] = {}
    expects_pre_by: dict[int, list[tuple[str, int | bool, str | None, str]]] = {}
    expects_post_by: dict[int, list[tuple[str, int | bool, str | None, str]]] = {}
    prints_at: dict[int, list[tuple[str, list[str]]]] = {}
    prints_every: list[tuple[str, int, int, list[str]]] = []
    for d in t.drives:
        dir_, sn, ty = iface.resolve(d.port)
        if dir_ != "in":
            raise SystemExit(f"drive() requires input port, got output: {d.port!r}")
        drives_by.setdefault(int(d.at), []).append((sn, d.value, ty))
    for e in t.expects:
        _dir, sn, ty = iface.resolve(e.port)
        ph = str(getattr(e, "phase", "post")).strip().lower()
        if ph == "pre":
            expects_pre_by.setdefault(int(e.at), []).append((sn, e.value, e.msg, ty))
        else:
            expects_post_by.setdefault(int(e.at), []).append((sn, e.value, e.msg, ty))
    for p in getattr(t, "prints", []):
        fmt = str(p.fmt)
        ports = []
        for raw in p.ports:
            _dir, sn, _ty = iface.resolve(raw)
            ports.append(sn)
        if p.at is not None:
            prints_at.setdefault(int(p.at), []).append((fmt, ports))
        else:
            st = 0 if p.start is None else int(p.start)
            ev = 1 if p.every is None else int(p.every)
            prints_every.append((fmt, st, ev, ports))

    rand_specs: list[tuple[str, int, int, int, int]] = []
    if t.random_streams:
        used_ports: set[str] = set()
        for r in t.random_streams:
            dir_, sn, ty = iface.resolve(r.port)
            if dir_ != "in":
                raise SystemExit(f"random() requires input port, got output: {r.port!r}")
            if ty == "!pyc.clock" or ty == "!pyc.reset":
                raise SystemExit(f"random() cannot target clock/reset ports: {r.port!r}")
            if sn in used_ports:
                raise SystemExit(f"duplicate random() stream for port: {r.port!r}")
            used_ports.add(sn)
            w = _as_int_width(ty)
            if w > 64:
                raise SystemExit(f"random() for i{w} not supported in SV TB generator (prototype limitation)")
            rand_specs.append((sn, w, int(r.seed), int(r.start), int(r.every)))

    clk_sn = ""
    rst_sn = ""
    ca = 0
    cd = 0
    if has_clocks:
        clk = t.clocks[0].port
        _, clk_sn, _clk_ty = iface.resolve(clk)
    if has_reset:
        rst = t.reset_spec.port
        _, rst_sn, _rst_ty = iface.resolve(rst)
        ca = int(t.reset_spec.cycles_asserted)
        cd = int(t.reset_spec.cycles_deasserted)

    lines: list[str] = []
    lines.append("// Generated by pycircuit (prototype)\n")
    lines.append("`timescale 1ns/1ps\n\n")
    lines.append(f"module tb_{top};\n")
    lines.append("  /* verilator lint_off UNUSEDSIGNAL */\n")

    for n, ty in zip(iface.in_names, iface.in_tys):
        lines.append(decl(n, ty))
    for n, ty in zip(iface.out_names, iface.out_tys):
        lines.append(decl(n, ty))
    if rand_specs:
        lines.append("\n")
        lines.append("  // Random stream state.\n")
        for sn, _w, _seed, _st, _ev in rand_specs:
            lines.append(f"  longint unsigned rng_{sn};\n")
    lines.append("  integer timeout_cycles;\n")
    lines.append("  integer cyc;\n")
    lines.append("  logic __pyc_tb_active;\n")
    lines.append("  initial __pyc_tb_active = 1'b0;\n")
    lines.append("  logic __pyc_tb_done;\n")
    lines.append("  initial __pyc_tb_done = 1'b0;\n")
    lines.append("\n")

    lines.append(f"  {mod_name} dut (\n")
    conns = [f"    .{sn}({sn})" for sn in [*iface.in_names, *iface.out_names]]
    lines.append(",\n".join(conns))
    lines.append("\n  );\n\n")

    # Optional VCD tracing via `$dumpvars` (Decision 0145).
    if trace_plan and trace_plan.enabled_signals:
        # Decision 0023: enabled_signals are canonical `<instance_path>:<field_path>` strings.
        # SystemVerilog `$dumpvars` expects hierarchical references, so map ":" -> ".".
        sigs = sorted(set(str(s) for s in trace_plan.enabled_signals))

        def canonical_to_sv_ref(p: str) -> str:
            inst, sep, field = str(p).partition(":")
            if not sep:
                return str(p)
            if not inst:
                return _sanitize_id(field)
            if not field:
                return inst
            # Verilog/SV identifiers cannot contain `.` or `[]` separators used
            # by canonical field paths (Decisions 0009/0024). The Verilog
            # backend applies `_sanitize_id` on port names, so do the same here.
            return f"{inst}.{_sanitize_id(field)}"

        sv_sigs = sorted(set(canonical_to_sv_ref(s) for s in sigs))
        lines.append("  // Optional traces (generated from trace DSL).\n")
        lines.append("  initial begin : __pyc_tb_trace\n")
        lines.append(f"    $dumpfile(\"tb_{top}.vcd\");\n")
        # Chunk long `$dumpvars` arg lists to keep tool limits reasonable.
        chunk = 64
        for i in range(0, len(sv_sigs), chunk):
            args = ", ".join(sv_sigs[i : i + chunk])
            lines.append(f"    $dumpvars(0, {args});\n")
        if trace_plan.window and trace_plan.window.begin_cycle is not None and trace_plan.window.end_cycle is not None:
            if int(trace_plan.window.begin_cycle) > 0:
                lines.append("    $dumpoff;\n")
        lines.append("  end\n\n")

    # Clock generation: currently only supports the first clock.
    if has_clocks:
        hp = int(t.clocks[0].half_period_steps)
        if hp != 1:
            lines.append("  // NOTE: half_period_steps != 1 is approximated by scaling delay.\n")
        lines.append("  initial begin\n")
        lines.append(f"    {clk_sn} = {1 if (t.clocks and t.clocks[0].start_high) else 0};\n")
        lines.append("  end\n")
        lines.append(f"  always #{hp} {clk_sn} = ~{clk_sn};\n\n")

    # Main stimulus loop.
    lines.append("  initial begin : __pyc_tb_main\n")
    # Initialize all driven inputs to 0.
    for sn, ty in zip(iface.in_names, iface.in_tys):
        if sn == clk_sn:
            continue
        w = _as_int_width(ty)
        lines.append(f"    {sn} = {w}'d0;\n")
    lines.append("    __pyc_tb_active = 1'b0;\n")
    lines.append("    __pyc_tb_done = 1'b0;\n")
    if rand_specs:
        lines.append("\n")
        lines.append("    // Random stream seeds.\n")
        for sn, _w, seed, _st, _ev in rand_specs:
            seed64 = int(seed) & ((1 << 64) - 1)
            lines.append(f"    rng_{sn} = 64'h{seed64:016x};\n")
    lines.append("\n")
    if has_reset:
        lines.append(f"    {rst_sn} = 1'b1;\n")
        lines.append(f"    repeat ({int(ca)}) @(posedge {clk_sn});\n")
        # Deassert reset away from a posedge to avoid races with posedge-triggered state.
        lines.append(f"    @(negedge {clk_sn});\n")
        lines.append(f"    {rst_sn} = 1'b0;\n")
        lines.append(f"    repeat ({int(cd)}) @(posedge {clk_sn});\n")
        # Ensure cycle 0 starts on a negedge after any post-reset settle cycles.
        lines.append(f"    if ({int(cd)} != 0) @(negedge {clk_sn});\n\n")
    elif has_clocks:
        # Align stimulus so cycle 0 drives are applied on a negedge, avoiding races
        # with posedge-triggered sequential logic in the DUT.
        lines.append(f"    @(negedge {clk_sn});\n\n")

    lines.append(f"    timeout_cycles = {int(t.timeout_cycles)};\n")
    lines.append("    for (cyc = 0; cyc < timeout_cycles; cyc = cyc + 1) begin\n")

    if trace_plan and trace_plan.enabled_signals and trace_plan.window:
        b = trace_plan.window.begin_cycle
        e = trace_plan.window.end_cycle
        if b is not None and e is not None:
            lines.append("      // Trace window toggles.\n")
            lines.append(f"      if (cyc == {int(b)}) $dumpon;\n")
            lines.append(f"      if (cyc == {int(e) + 1}) $dumpoff;\n\n")

    if rand_specs:
        lines.append("      // Random drives for this cycle (applied before explicit drives).\n")
        for sn, w, _seed, st, ev in rand_specs:
            hi = 63 if w >= 64 else (w - 1)
            lines.append(f"      if (cyc >= {int(st)} && (((cyc - {int(st)}) % {int(ev)}) == 0)) begin\n")
            lines.append("        // LCG: state = state * 6364136223846793005 + 1.\n")
            lines.append(f"        rng_{sn} = (rng_{sn} * 64'd6364136223846793005) + 64'd1;\n")
            lines.append(f"        {sn} = rng_{sn}[{hi}:0];\n")
            lines.append("      end\n")
        lines.append("\n")

    if drives_by:
        lines.append("      // Drives for this cycle (applied before posedge).\n")
        lines.append("      unique case (cyc)\n")
        for cyc in sorted(drives_by.keys()):
            lines.append(f"        {cyc}: begin\n")
            for sn, val, ty in drives_by[cyc]:
                w = _as_int_width(ty)
                lines.append(f"          {sn} = {sv_lit(w, val)};\n")
            lines.append("        end\n")
        lines.append("        default: begin end\n")
        lines.append("      endcase\n")

    if expects_pre_by:
        # Allow a delta-cycle for combinational logic to settle after procedural
        # drives in this TB process. This keeps pre-step sampling stable and
        # avoids racey reads of DUT outputs.
        lines.append("      #0;\n")
        lines.append("      // Pre-step expects for this cycle (checked before posedge).\n")
        lines.append("      unique case (cyc)\n")
        for cyc in sorted(expects_pre_by.keys()):
            lines.append(f"        {cyc}: begin\n")
            for sn, val, msg, ty in expects_pre_by[cyc]:
                w = _as_int_width(ty)
                m = msg if msg is not None else f"{sn} mismatch"
                lines.append(f"          if ({sn} !== {sv_lit(w, val)}) $fatal(1, \"PRE: {m}\");\n")
            lines.append("        end\n")
        lines.append("        default: begin end\n")
        lines.append("      endcase\n")

    if has_clocks:
        lines.append(f"      @(posedge {clk_sn});\n")
        lines.append(f"      @(negedge {clk_sn});\n")
    else:
        lines.append("      #1;\n")
    lines.append("      __pyc_tb_active = 1'b1;\n")

    if expects_post_by:
        lines.append("      // Expects for this cycle (checked after posedge updates).\n")
        lines.append("      unique case (cyc)\n")
        for cyc in sorted(expects_post_by.keys()):
            lines.append(f"        {cyc}: begin\n")
            for sn, val, msg, ty in expects_post_by[cyc]:
                w = _as_int_width(ty)
                m = msg if msg is not None else f"{sn} mismatch"
                lines.append(f"          if ({sn} !== {sv_lit(w, val)}) $fatal(1, \"{m}\");\n")
            lines.append("        end\n")
        lines.append("        default: begin end\n")
        lines.append("      endcase\n")

    if prints_at:
        lines.append("      // Per-cycle prints.\n")
        lines.append("      unique case (cyc)\n")
        for cyc in sorted(prints_at.keys()):
            lines.append(f"        {cyc}: begin\n")
            for fmt, ports in prints_at[cyc]:
                esc = str(fmt).replace("\\", "\\\\").replace("\"", "\\\"")
                if ports:
                    suffix = "".join(f" {p}=%0h" for p in ports)
                    args = ", ".join(["cyc", *ports])
                    lines.append(f"          $display(\"[tb] cyc=%0d {esc}{suffix}\", {args});\n")
                else:
                    lines.append(f"          $display(\"[tb] cyc=%0d {esc}\", cyc);\n")
            lines.append("        end\n")
        lines.append("        default: begin end\n")
        lines.append("      endcase\n")

    if prints_every:
        lines.append("      // Periodic prints.\n")
        for fmt, st, ev, ports in prints_every:
            esc = str(fmt).replace("\\", "\\\\").replace("\"", "\\\"")
            lines.append(f"      if (cyc >= {st} && (((cyc - {st}) % {ev}) == 0)) begin\n")
            if ports:
                suffix = "".join(f" {p}=%0h" for p in ports)
                args = ", ".join(["cyc", *ports])
                lines.append(f"        $display(\"[tb] cyc=%0d {esc}{suffix}\", {args});\n")
            else:
                lines.append(f"        $display(\"[tb] cyc=%0d {esc}\", cyc);\n")
            lines.append("      end\n")

    if t.finish_cycle is not None:
        lines.append(f"      if (cyc == {int(t.finish_cycle)}) begin\n")
        lines.append("        __pyc_tb_done = 1'b1;\n")
        lines.append("        $display(\"OK\");\n")
        lines.append("        $finish;\n")
        lines.append("        disable __pyc_tb_main;\n")
        lines.append("      end\n")

    lines.append("    end\n")
    if t.finish_cycle is None:
        lines.append("    if (!__pyc_tb_done) $fatal(1, \"TIMEOUT\");\n")
    lines.append("  end\n\n")

    # SVA assertions.
    if t.sva_asserts:
        if not has_clocks:
            raise SystemExit("sva_assert requires t.clock(...) in testbench")
        lines.append("  // SVA assertions.\n")
        for i, a in enumerate(t.sva_asserts):
            nm = a.name or f"sva_{i}"
            clk_dir, clk_port, _ = iface.resolve(a.clock)
            if clk_dir != "in":
                raise SystemExit(f"sva_assert clock must be an input port, got output: {a.clock!r}")
            pv = f"__pyc_sva_past_valid_{i}"
            # Guard against `$past` being undefined in the first sampled cycle by
            # generating a per-assertion past-valid bit.
            lines.append(f"  logic {pv};\n")
            lines.append(f"  initial {pv} = 1'b0;\n")
            disable_terms = ["!__pyc_tb_active"]
            if a.reset:
                rst_dir, rst_port, _ = iface.resolve(a.reset)
                if rst_dir != "in":
                    raise SystemExit(f"sva_assert reset must be an input port, got output: {a.reset!r}")
                disable_terms.insert(0, rst_port)
                lines.append(f"  always_ff @(posedge {clk_port}) begin\n")
                lines.append(f"    if ({rst_port}) {pv} <= 1'b0; else {pv} <= 1'b1;\n")
                lines.append("  end\n")
            else:
                lines.append(f"  always_ff @(posedge {clk_port}) begin\n")
                lines.append(f"    {pv} <= 1'b1;\n")
                lines.append("  end\n")
            rst_expr = f" disable iff ({' || '.join(disable_terms)})"
            msg = a.msg or f"SVA {nm} failed"
            expr = f"(!{pv}) || ({a.expr})"
            # Sample on negedge so assertions observe values after posedge-triggered
            # sequential updates in common designs.
            lines.append(
                f"  assert property (@(negedge {clk_port}){rst_expr} {expr}) else $fatal(1, \"{msg}\");\n"
            )
        lines.append("\n")

    lines.append("  /* verilator lint_on UNUSEDSIGNAL */\n")
    lines.append("endmodule\n")
    return "".join(lines)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    if path.is_file():
        try:
            if path.read_bytes() == data:
                return
        except OSError:
            # Fall back to overwrite if we can't read for comparison.
            pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _run_backend_job(job: tuple[str, list[str]]) -> tuple[str, str]:
    name, cmd = job
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.strip()
        out = proc.stdout.strip()
        raise RuntimeError(f"backend job {name!r} failed ({proc.returncode})\ncmd: {' '.join(cmd)}\n{err}\n{out}")
    return (name, proc.stdout.strip())


def _emit_multi_pyc_artifacts(design: Design, *, out_dir: Path) -> tuple[Path, dict[str, Any], dict[str, Path], Path]:
    module_map = design.emit_module_mlir_map()
    module_dir = out_dir / "device" / "modules"
    module_dir.mkdir(parents=True, exist_ok=True)

    module_paths: dict[str, Path] = {}
    for sym in sorted(module_map.keys()):
        p = module_dir / f"{sym}.pyc"
        _write_text_atomic(p, module_map[sym])
        module_paths[sym] = p

    design_pyc_path = out_dir / "device" / "design.pyc"
    _write_text_atomic(design_pyc_path, design.emit_mlir())

    manifest = design.emit_project_manifest(module_dir_rel="device/modules")
    manifest["design_pyc"] = str(design_pyc_path.relative_to(out_dir))
    manifest_path = out_dir / "project_manifest.json"
    _write_text_atomic(manifest_path, json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return (manifest_path, manifest, module_paths, design_pyc_path)


def _collect_testbench_payload(
    mod: object,
    iface: _TopIface,
    *,
    trace_plan: TracePlan | None = None,
    tb_probes: TbProbes | None = None,
    tb_schedule_mode: str = "inline",
    tb_schedule_format: str = "pycstb3",
    tb_schedule_dir: Path | None = None,
) -> tuple[str, str]:
    if not hasattr(mod, "tb") or not callable(getattr(mod, "tb")):
        raise SystemExit("build requires `@testbench def tb(t: Tb): ...`")
    tb_fn = getattr(mod, "tb")
    if not bool(getattr(tb_fn, "__pycircuit_testbench__", False)):
        raise SystemExit("build requires tb(...) to be decorated with `@testbench`")
    t = Tb()
    try:
        tb_sig = inspect.signature(tb_fn)
        if len(tb_sig.parameters) >= 2:
            tb_fn(t, TbProbes([]) if tb_probes is None else tb_probes)
        else:
            tb_fn(t)
    except TbError as e:
        raise SystemExit(f"tb() failed: {e}") from e
    except ProbeError as e:
        raise SystemExit(f"tb() probe access failed: {e}") from e
    payload_obj = testbench_payload_from_tb(
        top_symbol=iface.sym,
        in_raw=list(iface.in_raw),
        in_tys=list(iface.in_tys),
        out_raw=list(iface.out_raw),
        out_tys=list(iface.out_tys),
        tb=t,
        probes=tb_probes,
    )
    tb_name = getattr(tb_fn, "__pycircuit_module_name__", None)
    if not isinstance(tb_name, str) or not tb_name.strip():
        tb_name = f"tb_{iface.sym}"
    tb_name = _sanitize_id(str(tb_name))
    payload = payload_obj.as_dict()
    payload["tb_name"] = str(tb_name)
    payload["tb_schedule_mode"] = str(tb_schedule_mode)
    payload["tb_schedule_format"] = str(tb_schedule_format)
    tb_runtime_schedule_path: Path | None = None
    tb_actor_data_path: Path | None = None
    if str(tb_schedule_mode).strip().lower() == "runtime-loop":
        if tb_schedule_dir is None:
            raise SystemExit("runtime-loop TB requires a schedule output directory")
        tb_runtime_schedule_path = tb_schedule_dir / f"{tb_name}.schedule.bin"
        payload["tb_schedule"] = str(tb_runtime_schedule_path)
        payload["tb_schedule_json"] = str(tb_runtime_schedule_path.with_suffix(".json"))
        payload["tb_schedule_pycstb4"] = str(tb_runtime_schedule_path.with_suffix(".pycstb4"))
    if str(tb_schedule_mode).strip().lower() == "actor-fastpath":
        if tb_schedule_dir is None:
            raise SystemExit("actor-fastpath TB requires a schedule output directory")
        tb_actor_data_path = tb_schedule_dir / f"{tb_name}.actor.pactr0"
        payload["tb_actor_data"] = str(tb_actor_data_path)
        payload["tb_actor_stats"] = str(tb_actor_data_path.with_suffix(".stats.json"))
        payload["tb_actor_schedule_json"] = str(tb_actor_data_path.with_suffix(".schedule.json"))
        payload["tb_actor_schedule_pycstb4"] = str(tb_actor_data_path.with_suffix(".schedule.pycstb4"))
    if trace_plan is not None:
        payload["trace_plan"] = trace_plan.as_dict()
    payload["cpp_text"] = _render_tb_cpp(
        iface,
        t,
        trace_plan=trace_plan,
        schedule_mode=tb_schedule_mode,
        schedule_path=tb_runtime_schedule_path,
        schedule_format=tb_schedule_format,
        actor_data_path=tb_actor_data_path,
    )
    payload["sv_text"] = _render_tb_sv(iface, t, trace_plan=trace_plan)
    return (str(tb_name), json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _emit_testbench_pyc_file(
    *,
    out_dir: Path,
    tb_name: str,
    payload_json: str,
) -> Path:
    tb_dir = out_dir / "tb"
    tb_dir.mkdir(parents=True, exist_ok=True)
    tb_pyc_path = tb_dir / f"{tb_name}.pyc"
    payload = json.loads(payload_json)
    _write_text_atomic(
        tb_pyc_path,
        emit_testbench_pyc(payload=payload, tb_name=tb_name, frontend_contract=FRONTEND_CONTRACT),
    )
    return tb_pyc_path


def _gather_cpp_sources(cpp_root: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(cpp_root.rglob("*.cpp")):
        if p.is_file():
            out.append(p)
    return out


def _gather_cpp_headers(cpp_root: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(cpp_root.rglob("*.hpp")):
        if p.is_file():
            out.append(p)
    return out


def _module_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _deps_hash(entry: Path, *, project_root: Path) -> str:
    root = project_root.resolve()
    files = collect_local_python_graph(entry.resolve(), project_root=root)
    h = hashlib.sha256()
    for p in files:
        try:
            rel = str(p.relative_to(root))
        except ValueError:
            rel = str(p)
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest())
        h.update(b"\0")
    return h.hexdigest()


def _canonical_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: dict[str, Any]) -> None:
    _write_text_atomic(path, json.dumps(data, sort_keys=True, indent=2) + "\n")


def _base_name_of(fn: Any) -> str:
    override = getattr(fn, "__pycircuit_module_name__", None)
    if isinstance(override, str) and override.strip():
        return override.strip()
    return getattr(fn, "__name__", "Module")


def _module_params_from_manifest(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    modules = manifest.get("modules", [])
    if not isinstance(modules, list):
        return out
    for raw in modules:
        if not isinstance(raw, Mapping):
            continue
        sym = str(raw.get("name", "")).strip()
        params_json = str(raw.get("params_json", "{}"))
        if not sym:
            continue
        try:
            params = json.loads(params_json)
        except Exception:
            params = {}
        if isinstance(params, Mapping):
            out[sym] = dict(params)
        else:
            out[sym] = {}
    return out


def _module_bases_from_manifest(manifest: Mapping[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    modules = manifest.get("modules", [])
    if not isinstance(modules, list):
        return out
    for raw in modules:
        if not isinstance(raw, Mapping):
            continue
        sym = str(raw.get("name", "")).strip()
        base = str(raw.get("base", "")).strip()
        if not sym or not base:
            continue
        out.setdefault(base, []).append(sym)
    for key in list(out.keys()):
        out[key] = sorted(set(out[key]))
    return out


def _resolve_probe_outputs(
    *,
    mod: object,
    manifest: Mapping[str, Any],
    probe_catalog_path: Path,
    out_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    catalog = load_probe_catalog(probe_catalog_path)
    params_by_symbol = _module_params_from_manifest(manifest)
    bases = _module_bases_from_manifest(manifest)
    explicit_plans = []
    probe_entries: list[dict[str, Any]] = []
    probe_dir = out_dir / "device" / "probes"
    probe_dir.mkdir(parents=True, exist_ok=True)

    probe_modules: list[object] = []
    seen_module_ids: set[int] = set()

    def add_probe_module(candidate: object | None) -> None:
        if candidate is None:
            return
        mod_id = id(candidate)
        if mod_id in seen_module_ids:
            return
        seen_module_ids.add(mod_id)
        probe_modules.append(candidate)

    add_probe_module(mod)
    for value in vars(mod).values():
        owner = inspect.getmodule(value) if callable(value) else None
        if owner is not None:
            add_probe_module(owner)

    seen_probe_fns: set[int] = set()
    probe_fns: list[Any] = []
    for probe_mod in probe_modules:
        for probe_fn in collect_probe_functions(probe_mod):
            probe_id = id(probe_fn)
            if probe_id in seen_probe_fns:
                continue
            seen_probe_fns.add(probe_id)
            probe_fns.append(probe_fn)

    for probe_fn in probe_fns:
        target_fn = getattr(probe_fn, "__pycircuit_probe_target__", None)
        if target_fn is None:
            raise SystemExit(f"invalid @probe without target: {getattr(probe_fn, '__name__', probe_fn)!r}")
        target_base = _base_name_of(target_fn)
        target_symbols = bases.get(target_base, [])
        plan = resolve_probe_function(
            probe_fn,
            catalog=catalog,
            target_base=target_base,
            target_symbols=target_symbols,
            params_by_symbol=params_by_symbol,
        )
        explicit_plans.append(plan)
        rel = Path("device") / "probes" / f"{plan.name}.json"
        _save_json(out_dir / rel, plan.as_dict())
        probe_entries.append(
            {
                "name": plan.name,
                "target_base": target_base,
                "target_symbols": list(plan.target_symbols),
                "json": str(rel),
                "leaf_count": len(plan.leaves),
            }
        )

    probe_manifest = build_resolved_probe_manifest(
        top=str(manifest.get("top", "")),
        root_instance="dut",
        explicit_plans=explicit_plans,
        catalog=catalog,
    )
    probe_plan = {
        "version": 1,
        "top_symbol": str(manifest.get("top", "")),
        "aliases": [
            {"canonical_path": leaf.canonical_path, "source_path": leaf.source_path}
            for plan in explicit_plans
            for leaf in plan.leaves
        ],
    }
    probe_plan_path = out_dir / "probe_plan.json"
    _save_json(probe_plan_path, probe_plan)
    return (probe_manifest, {"version": 1, "probes": probe_entries}, probe_plan_path)


def _cmd_build(args: argparse.Namespace) -> int:
    src = Path(args.python_file).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    cache_path = out_dir / ".build_cache.json"
    cache = _load_json(cache_path) if cache_path.is_file() else {"module_hashes": {}}

    project_root = _project_root(src, project_root_override=args.project_root)
    _scan_api_contract(src, project_root_override=str(project_root))
    mod = _load_py_file(src)
    if not hasattr(mod, "build") or not callable(getattr(mod, "build")):
        raise SystemExit(f"{src} must define a pyCircuit entrypoint: `@module def build(m: Circuit, ...)`")
    build = getattr(mod, "build")
    jit_params = _collect_jit_params(build, overrides=list(getattr(args, "param", []) or []))
    top_name = _top_name_for_build(src, build)

    from .design import canonical_params_json

    try:
        jit_params_json = canonical_params_json(jit_params, path="jit_params")
    except DesignError as e:
        raise SystemExit(f"JIT param canonicalization failed: {e}") from e
    jit_inputs = {
        "version": 1,
        "entry_hash": _module_hash(src),
        "deps_hash": _deps_hash(src, project_root=project_root),
        "jit_params_json": jit_params_json,
        "top_name": top_name,
        "frontend_contract": FRONTEND_CONTRACT,
    }
    jit_key = _canonical_hash(jit_inputs)

    manifest_path = out_dir / "project_manifest.json"
    design: Design | None = None
    manifest: dict[str, Any]
    module_paths: dict[str, Path]
    design_pyc_path: Path
    iface: _TopIface

    cached_key = str(cache.get("jit_cache_key", "")).strip()
    cache_hit = cached_key == jit_key and manifest_path.is_file()
    if cache_hit:
        try:
            manifest = _load_json(manifest_path)
            module_paths = _module_paths_from_manifest(manifest, out_dir=out_dir)
            if not all(p.is_file() for p in module_paths.values()):
                raise FileNotFoundError("missing cached .pyc modules")
            design_pyc_rel = str(manifest.get("design_pyc", "")).strip()
            design_pyc_path = (out_dir / design_pyc_rel) if design_pyc_rel else (out_dir / "device" / "design.pyc")
            if not design_pyc_path.is_file():
                raise FileNotFoundError("missing cached design.pyc")
            iface = _top_iface_from_manifest(manifest)
            print("jit-cache: hit")
        except Exception:
            cache_hit = False

    if not cache_hit:
        try:
            design_obj = compile(build, name=top_name, **jit_params)
        except (DesignError, JitError) as e:
            raise SystemExit(f"design compile failed: {e}") from e
        if not isinstance(design_obj, Design):
            raise SystemExit("internal error: expected Design from compile(...)")
        design = design_obj
        iface = _top_iface(design)
        manifest_path, manifest, module_paths, design_pyc_path = _emit_multi_pyc_artifacts(design, out_dir=out_dir)
        print("jit-cache: miss")

    pycc = _detect_pycc()
    jobs = max(1, int(args.jobs))
    if int(args.logic_depth) <= 0:
        raise SystemExit("--logic-depth must be > 0")
    logic_depth = int(args.logic_depth)

    device_cpp_root = out_dir / "device" / "cpp"
    device_v_root = out_dir / "device" / "verilog"
    device_cpp_root.mkdir(parents=True, exist_ok=True)
    device_v_root.mkdir(parents=True, exist_ok=True)

    target = str(args.target)
    do_cpp = target in {"cpp", "both"}
    do_v = target in {"verilator", "both"}
    pycc_build_profile = "dev-fast" if str(args.profile) == "dev" else "release"
    pycc_hard_hierarchy_flags = [
        f"--build-profile={pycc_build_profile}",
        "--inline-policy=off",
        "--hierarchy-policy=strict",
    ]

    build_flags = {
        "pycc": str(pycc.resolve()),
        "logic_depth": logic_depth,
        "profile": str(args.profile),
        "pycc_build_profile": pycc_build_profile,
        "inline_policy": "off",
        "hierarchy_policy": "strict",
        "target": target,
        "tb_schedule_mode": str(args.tb_schedule_mode),
        "frontend_contract": FRONTEND_CONTRACT,
    }
    build_flags_hash = _canonical_hash(build_flags)
    same_flags = str(cache.get("build_flags_hash", "")) == build_flags_hash

    design_key = "__design_pyc"
    old_hashes = dict(cache.get("module_hashes", {}))
    module_hashes: dict[str, str] = {}
    design_hash = _module_hash(design_pyc_path)
    module_hashes[design_key] = design_hash
    probe_catalog_path = out_dir / "device" / "probe_catalog.json"
    probe_catalog_ready = probe_catalog_path.is_file()
    probe_unchanged = same_flags and old_hashes.get(design_key) == design_hash
    pycc_jobs: list[tuple[str, list[str]]] = []
    if not (probe_unchanged and probe_catalog_ready):
        pycc_jobs.append(
            (
                "probe-catalog",
                [
                    str(pycc),
                    str(design_pyc_path),
                    "--emit=none",
                    *pycc_hard_hierarchy_flags,
                    "--probe-manifest",
                    str(probe_catalog_path),
                    f"--logic-depth={logic_depth}",
                ],
            )
        )
    if pycc_jobs:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            futs = {pool.submit(_run_backend_job, j): j[0] for j in pycc_jobs}
            for fut in as_completed(futs):
                _ = fut.result()
        pycc_jobs = []

    try:
        probe_manifest_obj, probe_section, probe_plan_path = _resolve_probe_outputs(
            mod=mod,
            manifest=manifest,
            probe_catalog_path=probe_catalog_path,
            out_dir=out_dir,
        )
    except ProbeError as e:
        raise SystemExit(f"probe resolution failed: {e}") from e
    probe_manifest_path = out_dir / "probe_manifest.json"
    _save_json(probe_manifest_path, probe_manifest_obj)
    manifest["probe_manifest"] = str(probe_manifest_path.relative_to(out_dir))
    manifest["probes"] = list(probe_section.get("probes", []))

    trace_plan: TracePlan | None = None
    trace_cfg_path = getattr(args, "trace_config", None)
    if trace_cfg_path is not None:
        raw = str(trace_cfg_path).strip()
        if raw:
            try:
                cfg = load_trace_config(Path(raw))
                trace_plan = compute_trace_plan_from_artifacts(
                    manifest=manifest,
                    module_paths=module_paths,
                    config=cfg,
                    probe_manifest=probe_manifest_obj,
                )
            except TraceConfigError as e:
                raise SystemExit(f"trace config error: {e}") from e

    tb_probes = TbProbes.from_probe_manifest(probe_manifest_obj)
    tb_name, tb_payload_json = _collect_testbench_payload(
        mod,
        iface,
        trace_plan=trace_plan,
        tb_probes=tb_probes,
        tb_schedule_mode=str(args.tb_schedule_mode),
        tb_schedule_format=str(args.tb_schedule_format),
        tb_schedule_dir=out_dir / "tb",
    )
    tb_pyc_path = _emit_testbench_pyc_file(out_dir=out_dir, tb_name=tb_name, payload_json=tb_payload_json)
    manifest["testbench"] = {"name": tb_name, "pyc": str(tb_pyc_path.relative_to(out_dir))}
    if trace_plan is not None:
        trace_path = out_dir / "trace_plan.json"
        _save_json(trace_path, trace_plan.as_dict())
        manifest["trace_plan"] = str(trace_path.relative_to(out_dir))

    tb_cpp_out = out_dir / "tb" / f"{tb_name}.cpp"
    tb_sv_out = out_dir / "tb" / f"{tb_name}.sv"
    for sym in sorted(module_paths.keys()):
        mp = module_paths[sym]
        h = _module_hash(mp)
        module_hashes[sym] = h
        unchanged = same_flags and old_hashes.get(sym) == h

        cpp_out_dir = device_cpp_root / sym
        cpp_ready = cpp_out_dir.is_dir() and any(cpp_out_dir.glob("*.cpp")) and any(cpp_out_dir.glob("*.hpp"))
        if do_cpp and not (unchanged and cpp_ready):
            cpp_out_dir.mkdir(parents=True, exist_ok=True)
            pycc_jobs.append(
                (
                    f"cpp:{sym}",
                    [
                        str(pycc),
                        str(mp),
                        "--emit=cpp",
                        *pycc_hard_hierarchy_flags,
                        "--out-dir",
                        str(cpp_out_dir),
                        "--cpp-split=module",
                        "--probe-plan",
                        str(probe_plan_path),
                        f"--logic-depth={logic_depth}",
                    ],
                )
            )

        verilog_out_dir = device_v_root / sym
        verilog_ready = verilog_out_dir.is_dir() and any(verilog_out_dir.glob("*.v"))
        if do_v and not (unchanged and verilog_ready):
            verilog_out_dir.mkdir(parents=True, exist_ok=True)
            pycc_jobs.append(
                (
                    f"verilog:{sym}",
                    [
                        str(pycc),
                        str(mp),
                        "--emit=verilog",
                        *pycc_hard_hierarchy_flags,
                        "--out-dir",
                        str(verilog_out_dir),
                        f"--logic-depth={logic_depth}",
                    ],
                )
            )

    if do_cpp:
        tb_key = f"tb:{tb_name}"
        tb_hash = _module_hash(tb_pyc_path)
        module_hashes[tb_key] = tb_hash
        tb_unchanged = same_flags and old_hashes.get(tb_key) == tb_hash
        if not (tb_unchanged and tb_cpp_out.is_file()):
            pycc_jobs.append(
                (
                    f"tb-cpp:{tb_name}",
                    [str(pycc), str(tb_pyc_path), *pycc_hard_hierarchy_flags, "-cpp", str(tb_cpp_out)],
                )
            )
    if do_v:
        tb_key = f"tb:{tb_name}"
        tb_hash = module_hashes.get(tb_key) or _module_hash(tb_pyc_path)
        module_hashes[tb_key] = tb_hash
        tb_unchanged = same_flags and old_hashes.get(tb_key) == tb_hash
        if not (tb_unchanged and tb_sv_out.is_file()):
            pycc_jobs.append(
                (
                    f"tb-sv:{tb_name}",
                    [str(pycc), str(tb_pyc_path), *pycc_hard_hierarchy_flags, "-verilog", str(tb_sv_out)],
                )
            )

    if pycc_jobs:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            futs = {pool.submit(_run_backend_job, j): j[0] for j in pycc_jobs}
            for fut in as_completed(futs):
                _ = fut.result()

    if do_cpp:
        cpp_sources = _gather_cpp_sources(device_cpp_root)
        if not cpp_sources:
            raise SystemExit("build(cpp): no generated C++ sources found")
        if not tb_cpp_out.is_file():
            raise SystemExit(f"build(cpp): missing generated TB C++ source: {tb_cpp_out}")
        cpp_headers = _gather_cpp_headers(device_cpp_root)
        include_dirs: list[str] = []
        include_dirs.append(str(device_cpp_root))
        runtime_source_include = Path(__file__).resolve().parents[3] / "runtime"
        if runtime_source_include.is_dir():
            include_dirs.append(str(runtime_source_include))
        for p in [*cpp_sources, *cpp_headers]:
            parent = str(p.parent)
            if parent not in include_dirs:
                include_dirs.append(parent)

        runtime = _runtime_manifest_for_toolchain(_detect_toolchain_root(pycc))

        build_manifest = {
            "version": 3,
            "target_name": iface.sym,
            "tb_cpp": str(tb_cpp_out),
            "sources": [str(p) for p in cpp_sources],
            "headers": [str(p) for p in cpp_headers],
            "include_dirs": include_dirs,
            "runtime": runtime,
            "cxx_standard": "c++17",
            "profile": str(args.profile),
        }
        cpp_manifest = out_dir / "cpp_project_manifest.json"
        _save_json(cpp_manifest, build_manifest)

        gen_script = _tool_script("gen_cmake_from_manifest.py")
        cmake_src = out_dir / "cpp_build" / "src"
        cmake_build = out_dir / "cpp_build" / "build"
        cmake_src.mkdir(parents=True, exist_ok=True)
        cmake_build.mkdir(parents=True, exist_ok=True)

        subprocess.run(
            [sys.executable, str(gen_script), "--manifest", str(cpp_manifest), "--out-dir", str(cmake_src)],
            check=True,
        )
        build_type = "Release" if str(args.profile) == "release" else "RelWithDebInfo"

        # Windows/MSYS2: `ninja.exe --version` can intermittently fail with
        # STATUS_DLL_INIT_FAILED in subprocesses. Prefer Makefiles here for
        # robustness.
        cmake_cmd = [
            "cmake",
            "-G",
            "Ninja",
            "-S",
            str(cmake_src),
            "-B",
            str(cmake_build),
            f"-DCMAKE_BUILD_TYPE={build_type}",
        ]
        if os.name == "nt":
            cmake_cmd = [
                "cmake",
                "-G",
                "MinGW Makefiles",
                "-S",
                str(cmake_src),
                "-B",
                str(cmake_build),
                f"-DCMAKE_BUILD_TYPE={build_type}",
                "-DCMAKE_MAKE_PROGRAM=mingw32-make",
            ]

        subprocess.run(cmake_cmd, check=True)
        subprocess.run(["cmake", "--build", str(cmake_build), "-j", str(jobs)], check=True)
        manifest["cpp_executable"] = str(cmake_build / "pyc_tb")

    if do_v:
        if not tb_sv_out.is_file():
            raise SystemExit(f"build(verilator): missing generated TB SV source: {tb_sv_out}")
        prim_file: Path | None = None
        verilog_module_sources: list[str] = []
        for p in sorted(device_v_root.rglob("*.v")):
            if not p.is_file():
                continue
            if p.name == "pyc_primitives.v":
                if prim_file is None:
                    prim_file = p
                continue
            verilog_module_sources.append(str(p))
        if not verilog_module_sources:
            raise SystemExit("build(verilator): no generated Verilog sources found")
        verilog_sources = ([str(prim_file)] if prim_file is not None else []) + verilog_module_sources
        verilog_manifest = {
            "version": 1,
            "top": tb_name,
            "tb_sv": str(tb_sv_out),
            "sources": verilog_sources,
            "include_dirs": [str(device_v_root)],
        }
        sim_manifest = out_dir / "verilator_manifest.json"
        _save_json(sim_manifest, verilog_manifest)
        manifest["verilator_manifest"] = str(sim_manifest.relative_to(out_dir))
        if bool(args.run_verilator):
            vbuild = out_dir / "verilator_build"

            # On Windows, MSYS2's `verilator` is typically a script (shebang) and
            # cannot be launched via CreateProcess directly. Prefer the real exe.
            verilator_exe = "verilator"
            if os.name == "nt":
                verilator_exe = (
                    shutil.which("verilator_bin.exe")
                    or shutil.which("verilator_bin")
                    or "verilator_bin.exe"
                )

            # Verilator needs a valid VERILATOR_ROOT on Windows; otherwise it may
            # form mixed /path\\include\\... strings and fail to locate std SV.
            run_env = None
            if os.name == "nt":
                run_env = os.environ.copy()
                vb = shutil.which(str(verilator_exe))
                if vb:
                    prefix = Path(vb).resolve().parents[1]
                    run_env["VERILATOR_ROOT"] = str(prefix / "share" / "verilator")

            cmd = [
                verilator_exe,
                "--binary",
                "-Wall",
                "-Wno-fatal",
                "-Wno-DECLFILENAME",
                "-Wno-UNUSEDSIGNAL",
                "-Wno-WIDTHEXPAND",
                "--quiet",
                # MSYS2/Windows Verilator wrapper does not support --quiet-build.
                "--timing",
                "--trace",
                "--top-module",
                tb_name,
                "--Mdir",
                str(vbuild),
                str(tb_sv_out),
                *verilog_sources,
            ]
            subprocess.run(cmd, check=True, env=run_env)
            vbin = vbuild / f"V{tb_name}"
            if os.name == "nt" and not vbin.is_file():
                vbin_exe = vbin.with_suffix(".exe")
                if vbin_exe.is_file():
                    vbin = vbin_exe
            manifest["verilator_binary"] = str(vbin)
            if not vbin.is_file():
                raise SystemExit(f"build(verilator): expected binary not found: {vbin}")
            run_args = list(getattr(args, "run_arg", []) or [])
            subprocess.run([str(vbin), *run_args], cwd=str(out_dir), check=True)

    cache_out = dict(cache)
    cache_out.update(
        {
            "module_hashes": module_hashes,
            "pycc": str(pycc),
            "build_flags": build_flags,
            "build_flags_hash": build_flags_hash,
            "jit_cache_key": jit_key,
            "jit_cache_inputs": jit_inputs,
            "last_pycc_jobs": int(len(pycc_jobs)),
        }
    )
    _save_json(cache_path, cache_out)
    _save_json(manifest_path, manifest)
    print(str(manifest_path))
    return 0


def _cmd_pycstb4_inspect(args: argparse.Namespace) -> int:
    report = inspect_pycstb4_file(Path(args.file))
    sys.stdout.write(render_pycstb4_inspect_text(report))
    return 1 if bool(report.get("errors")) and bool(getattr(args, "strict", False)) else 0


def _cmd_pycstb4_dump_json(args: argparse.Namespace) -> int:
    report = inspect_pycstb4_file(Path(args.file))
    sys.stdout.write(pycstb4_report_json(report))
    return 1 if bool(report.get("errors")) and bool(getattr(args, "strict", False)) else 0


def _cmd_pycstb4_verify(args: argparse.Namespace) -> int:
    report = inspect_pycstb4_file(Path(args.file))
    if report.get("errors"):
        for item in report["errors"]:
            print(f"ERROR: {item}", file=sys.stderr)
    if report.get("warnings"):
        for item in report["warnings"]:
            print(f"WARNING: {item}", file=sys.stderr)
    if report.get("valid"):
        print("PYCSTB4 verify: ok")
        return 0
    print("PYCSTB4 verify: failed", file=sys.stderr)
    return 1


def _cmd_pycstb4_stats(args: argparse.Namespace) -> int:
    report = inspect_pycstb4_file(Path(args.file))
    sys.stdout.write(json.dumps(report.get("summary", {}), sort_keys=True, indent=2) + "\n")
    return 1 if bool(report.get("errors")) and bool(getattr(args, "strict", False)) else 0


def _cmd_pycstb4_registry(args: argparse.Namespace) -> int:
    registry = default_section_registry()
    manifest = section_registry_manifest(registry)
    rows = list(manifest["sections"])
    if bool(getattr(args, "json", False)):
        sys.stdout.write(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        return 0
    print(f"schema: {manifest['schema']} v{manifest['schema_version']['major']}.{manifest['schema_version']['minor']}")
    print(f"section_count: {manifest['section_count']}")
    print(f"sha256: {manifest['sha256']}")
    print("")
    print("kind  name                         req  exp  deps        runtime_tags")
    for row in rows:
        deps = ",".join(str(dep) for dep in row["dependencies"]) or "-"
        tags = ",".join(str(tag) for tag in row["runtime_tags"]) or "-"
        print(
            f"{int(row['kind']):>4}  "
            f"{str(row['name']):<28} "
            f"{'yes' if row['required'] else 'no ':<3}  "
            f"{'yes' if row['experimental'] else 'no ':<3}  "
            f"{deps:<10}  "
            f"{tags}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pycircuit")
    sub = p.add_subparsers(dest="cmd", required=True)

    emit = sub.add_parser("emit", help="Emit PYC MLIR (*.pyc) from a Python design file.")
    emit.add_argument("python_file", help="Python source defining `@module def build(m: Circuit, ...)`")
    emit.add_argument("-o", "--output", required=True, help="Output .pyc path")
    emit.add_argument(
        "--param",
        action="append",
        default=[],
        help="Override a JIT parameter (repeatable): name=value (parsed as a Python literal when possible)",
    )
    emit.add_argument(
        "--project-root",
        default=None,
        help="Optional project root for strict API contract scan (defaults to nearest .git/pyproject.toml).",
    )
    emit.add_argument(
        "--module-graph-out",
        dest="module_graph_out",
        default=None,
        help="Optional: emit a module-instance connectivity graph from the emitted .pyc (DOT/SVG via Graphviz).",
    )
    emit.add_argument(
        "--module-graph-module",
        dest="module_graph_module",
        default="",
        help="Target module symbol for the graph (default: module attribute pyc.top).",
    )
    emit.add_argument(
        "--module-graph-recursive",
        dest="module_graph_recursive",
        action="store_true",
        help="Recursively expand module instances (module nest) in the graph.",
    )
    emit.add_argument(
        "--module-graph-edge-label-mode",
        dest="module_graph_edge_label_mode",
        choices=["ports", "count", "none"],
        default="ports",
        help="Edge label mode for module graph.",
    )
    emit.add_argument(
        "--module-graph-edge-label-limit",
        dest="module_graph_edge_label_limit",
        type=int,
        default=4,
        help="Max port mappings per edge label (module graph).",
    )
    emit.add_argument(
        "--module-graph-max-nodes",
        dest="module_graph_max_nodes",
        type=int,
        default=500,
        help="Max instance nodes before aborting (module graph).",
    )
    emit.add_argument(
        "--module-graph-max-edges",
        dest="module_graph_max_edges",
        type=int,
        default=2000,
        help="Max instance edges before aborting (module graph).",
    )
    emit.set_defaults(fn=_cmd_emit)

    build = sub.add_parser("build", help="Canonical flow: multi-.pyc emit + parallel pycc + CMake/Verilator.")
    build.add_argument("python_file", help="Python source defining `@module build(...)` and `@testbench tb(...)`")
    build.add_argument("--out-dir", required=True, help="Output directory for project artifacts")
    build.add_argument(
        "--param",
        action="append",
        default=[],
        help="Override a JIT parameter (repeatable): name=value (parsed as a Python literal when possible)",
    )
    build.add_argument(
        "--project-root",
        default=None,
        help="Optional project root for strict API contract scan (defaults to nearest .git/pyproject.toml).",
    )
    build.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1), help="Parallel backend jobs")
    build.add_argument("--profile", choices=["dev", "release"], default="release", help="C++ build profile")
    build.add_argument(
        "--target",
        choices=["cpp", "verilator", "both"],
        default="both",
        help="Backend targets to generate/build",
    )
    build.add_argument("--logic-depth", type=int, default=32, help="Max combinational logic depth for pycc")
    build.add_argument(
        "--trace-config",
        default=None,
        help="Optional trace configuration JSON (instance globs + probe tags + windows) for VCD generation.",
    )
    build.add_argument(
        "--tb-schedule-mode",
        choices=["inline", "runtime-loop", "actor-fastpath"],
        default="inline",
        help="C++ testbench schedule rendering mode: inline preserves legacy per-cycle emission; runtime-loop emits a fixed runner plus event tables; actor-fastpath is an experimental ready-valid actor runner.",
    )
    build.add_argument(
        "--tb-schedule-format",
        choices=["pycstb3", "pycstb4"],
        default="pycstb3",
        help="Runtime-loop schedule execution format. pycstb3 is the legacy default; pycstb4 is experimental.",
    )
    build.add_argument(
        "--run-verilator",
        action="store_true",
        help="Also run generated Verilator binary after build",
    )
    build.add_argument(
        "--run-arg",
        action="append",
        default=[],
        help="Argument passed to the Verilator binary when --run-verilator is set (repeatable).",
    )
    build.set_defaults(fn=_cmd_build)

    pycstb4 = sub.add_parser("pycstb4", help="Inspect, dump, and verify PYCSTB4 container files.")
    pycstb4_sub = pycstb4.add_subparsers(dest="pycstb4_cmd", required=True)

    pycstb4_inspect = pycstb4_sub.add_parser("inspect", help="Print a human-readable PYCSTB4 section summary.")
    pycstb4_inspect.add_argument("file", help="PYCSTB4 file path")
    pycstb4_inspect.add_argument("--strict", action="store_true", help="Return non-zero if framework-level errors exist.")
    pycstb4_inspect.set_defaults(fn=_cmd_pycstb4_inspect)

    pycstb4_dump_json = pycstb4_sub.add_parser("dump-json", help="Dump PYCSTB4 header and section metadata as JSON.")
    pycstb4_dump_json.add_argument("file", help="PYCSTB4 file path")
    pycstb4_dump_json.add_argument("--strict", action="store_true", help="Return non-zero if framework-level errors exist.")
    pycstb4_dump_json.set_defaults(fn=_cmd_pycstb4_dump_json)

    pycstb4_verify = pycstb4_sub.add_parser("verify", help="Verify PYCSTB4 container and section-directory consistency.")
    pycstb4_verify.add_argument("file", help="PYCSTB4 file path")
    pycstb4_verify.set_defaults(fn=_cmd_pycstb4_verify)

    pycstb4_stats = pycstb4_sub.add_parser("stats", help="Print PYCSTB4 section summary stats as JSON.")
    pycstb4_stats.add_argument("file", help="PYCSTB4 file path")
    pycstb4_stats.add_argument("--strict", action="store_true", help="Return non-zero if framework-level errors exist.")
    pycstb4_stats.set_defaults(fn=_cmd_pycstb4_stats)

    pycstb4_registry = pycstb4_sub.add_parser("registry", help="List registered PYCSTB4 section descriptors.")
    pycstb4_registry.add_argument("--json", action="store_true", help="Output registry as JSON.")
    pycstb4_registry.set_defaults(fn=_cmd_pycstb4_registry)

    ns = p.parse_args(argv)
    return int(ns.fn(ns))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
