from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _write_fake_pycc(path: Path) -> None:
    path.write_text(
        r'''#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


def _arg_value(args: list[str], name: str) -> str:
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return ""


def _top_from_pyc(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"pyc\.top = @([A-Za-z_][A-Za-z0-9_]*)", text)
    if m:
        return m.group(1)
    return path.stem


def _top_from_tb_pyc(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r'pyc\.tb\.payload = ("(?:\\.|[^"])*")', text)
    if not m:
        return "dut"
    payload_json = json.loads(m.group(1))
    payload = json.loads(payload_json)
    return str(payload.get("top_symbol", "dut"))


def main() -> int:
    args = sys.argv[1:]
    src = Path(args[0]).resolve()
    if "--probe-manifest" in args:
        out = Path(_arg_value(args, "--probe-manifest")).resolve()
        top = _top_from_pyc(src)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "version": 1,
                    "top": top,
                    "root_instance": "dut",
                    "instances": [{"module": top, "instance_path": "dut"}],
                    "entries": [],
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 0

    if "-cpp" in args:
        out = Path(args[args.index("-cpp") + 1]).resolve()
        top = _top_from_tb_pyc(src)
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            f'#include "{top}.hpp"\n'
            f"// tb-pyc-sha256: {digest}\n"
            "int main() {\n"
            f"  pyc::gen::{top} dut;\n"
            "  return dut.value;\n"
            "}\n",
            encoding="utf-8",
        )
        return 0

    if "-verilog" in args:
        out = Path(args[args.index("-verilog") + 1]).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("module tb; initial $finish; endmodule\n", encoding="utf-8")
        return 0

    emit = _arg_value(args, "--emit")
    if emit == "cpp":
        out_dir_raw = _arg_value(args, "--out-dir")
        out_dir = Path(out_dir_raw).resolve()
        top = _top_from_pyc(src)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{top}.hpp").write_text(
            "#pragma once\n"
            "namespace pyc { namespace gen {\n"
            f"struct {top} {{ int value = 0; }};\n"
            "} }\n",
            encoding="utf-8",
        )
        (out_dir / f"{top}.cpp").write_text(
            f'#include "{top}.hpp"\n'
            "namespace pyc { namespace gen {\n"
            f"int {top}_anchor(const {top}& d) {{ return d.value; }}\n"
            "} }\n",
            encoding="utf-8",
        )
        return 0

    if emit == "verilog":
        out_dir_raw = _arg_value(args, "--out-dir")
        out_dir = Path(out_dir_raw).resolve()
        top = _top_from_pyc(src)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{top}.v").write_text(f"module {top}; endmodule\n", encoding="utf-8")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_build(
    repo: Path,
    design: Path,
    tb: Path,
    out_dir: Path,
    fake_pycc: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run_build_args(
        repo,
        [
            "--dut",
            str(design),
            "--tb",
            str(tb),
            "--out-dir",
            str(out_dir),
            "--target",
            "cpp",
            "--jobs",
            "1",
        ],
        fake_pycc,
        extra_env=extra_env,
    )


def _run_build_args(
    repo: Path,
    build_args: list[str],
    fake_pycc: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo / "compiler" / "frontend")
    env["PYCC"] = str(fake_pycc)
    env["PYC_TOOLCHAIN_ROOT"] = str(repo / ".pycircuit_out" / "toolchain" / "install")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pycircuit.cli",
            "build",
            *build_args,
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def _single_object(out_dir: Path, name: str) -> Path:
    obj_root = out_dir / "cpp_build" / "build" / "CMakeFiles" / "pyc_tb.dir"
    matches = sorted(obj_root.rglob(name))
    assert len(matches) == 1
    return matches[0]


def test_build_dut_only_generates_only_dut_cpp(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    design = case_dir / "dut_design.py"
    out_dir = tmp_path / "out"
    fake_pycc = tmp_path / "fake_pycc.py"
    _write_fake_pycc(fake_pycc)

    design.write_text(
        "\n".join(
            [
                "from pycircuit import Circuit, module, u",
                "",
                "@module",
                "def build(m: Circuit, width: int = 8) -> None:",
                "    x = m.input('x', width=width)",
                "    m.output('y', x + u(width, 0))",
                "",
                "build.__pycircuit_name__ = 'dut'",
                "",
            ]
        ),
        encoding="utf-8",
    )

    first = _run_build_args(
        repo,
        [
            "--dut",
            str(design),
            "--out-dir",
            str(out_dir),
            "--target",
            "cpp",
            "--jobs",
            "1",
        ],
        fake_pycc,
    )

    assert "jit-cache: miss" in first.stdout
    manifest = json.loads((out_dir / "project_manifest.json").read_text(encoding="utf-8"))
    cache = json.loads((out_dir / ".build_cache.json").read_text(encoding="utf-8"))
    assert "testbench" not in manifest
    assert "cpp_executable" not in manifest
    assert (out_dir / "device" / "cpp" / "dut" / "dut.cpp").is_file()
    assert (out_dir / "device" / "cpp" / "dut" / "dut.hpp").is_file()
    assert not (out_dir / "tb").exists()
    assert not (out_dir / "cpp_build").exists()
    assert cache["build_mode"] == "dut-only"
    assert cache["last_pycc_job_names"] == ["probe-catalog", "cpp:dut"]


def test_build_tb_only_reuses_design_cache_without_importing_dut(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    design = case_dir / "dut_design.py"
    tb = case_dir / "dut_tb.py"
    out_dir = tmp_path / "out"
    fake_pycc = tmp_path / "fake_pycc.py"
    _write_fake_pycc(fake_pycc)

    design.write_text(
        "\n".join(
            [
                "import os",
                "from pycircuit import Circuit, module, u",
                "",
                "if os.environ.get('DESIGN_IMPORT_FAIL'):",
                "    raise RuntimeError('DUT import should be skipped on explicit TB-only build')",
                "",
                "@module",
                "def build(m: Circuit, width: int = 8) -> None:",
                "    x = m.input('x', width=width)",
                "    m.output('y', x + u(width, 0))",
                "",
                "build.__pycircuit_name__ = 'dut'",
                "",
            ]
        ),
        encoding="utf-8",
    )
    tb.write_text(
        "\n".join(
            [
                "from pycircuit import Tb, testbench",
                "",
                "@testbench",
                "def tb(t: Tb) -> None:",
                "    t.drive('x', 1, at=0)",
                "    t.expect('y', 1, at=0)",
                "    t.finish(at=1)",
                "",
            ]
        ),
        encoding="utf-8",
    )

    design_build = _run_build_args(
        repo,
        [
            "--dut",
            str(design),
            "--out-dir",
            str(out_dir),
            "--target",
            "cpp",
            "--jobs",
            "1",
        ],
        fake_pycc,
    )
    assert "jit-cache: miss" in design_build.stdout

    tb_build = _run_build_args(
        repo,
        [
            "--tb",
            str(tb),
            "--out-dir",
            str(out_dir),
            "--target",
            "cpp",
            "--jobs",
            "1",
        ],
        fake_pycc,
        extra_env={"DESIGN_IMPORT_FAIL": "1"},
    )

    assert "jit-cache: hit" in tb_build.stdout
    cache = json.loads((out_dir / ".build_cache.json").read_text(encoding="utf-8"))
    assert cache["build_mode"] == "tb-only"
    assert cache["dut_src"] == str(design)
    assert cache["design_cache_fast_path"] is True
    assert cache["last_pycc_job_names"] == ["tb-cpp:tb_dut"]
    assert (out_dir / "tb" / "tb_dut.cpp").is_file()


def test_split_build_tb_only_change_does_not_recompile_dut(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    design = case_dir / "dut_design.py"
    tb = case_dir / "dut_tb.py"
    out_dir = tmp_path / "out"
    fake_pycc = tmp_path / "fake_pycc.py"
    _write_fake_pycc(fake_pycc)

    design.write_text(
        "\n".join(
            [
                "from pycircuit import Circuit, module, u",
                "",
                "@module",
                "def build(m: Circuit, width: int = 8) -> None:",
                "    x = m.input('x', width=width)",
                "    m.output('y', x + u(width, 0))",
                "",
                "build.__pycircuit_name__ = 'dut'",
                "",
            ]
        ),
        encoding="utf-8",
    )
    tb.write_text(
        "\n".join(
            [
                "from pycircuit import Tb, testbench",
                "",
                "@testbench",
                "def tb(t: Tb) -> None:",
                "    t.drive('x', 1, at=0)",
                "    t.expect('y', 1, at=0)",
                "    t.finish(at=1)",
                "",
            ]
        ),
        encoding="utf-8",
    )

    first = _run_build(repo, design, tb, out_dir, fake_pycc)
    assert "jit-cache: miss" in first.stdout
    dut_obj = _single_object(out_dir, "dut.cpp.o")
    tb_obj = _single_object(out_dir, "tb_dut.cpp.o")
    dut_obj_mtime = dut_obj.stat().st_mtime_ns
    tb_obj_mtime = tb_obj.stat().st_mtime_ns

    time.sleep(1.1)
    tb.write_text(
        "\n".join(
            [
                "from pycircuit import Tb, testbench",
                "",
                "@testbench",
                "def tb(t: Tb) -> None:",
                "    t.drive('x', 2, at=0)",
                "    t.expect('y', 2, at=0)",
                "    t.finish(at=1)",
                "",
            ]
        ),
        encoding="utf-8",
    )

    second = _run_build(repo, design, tb, out_dir, fake_pycc)
    assert "jit-cache: hit" in second.stdout
    cache = json.loads((out_dir / ".build_cache.json").read_text(encoding="utf-8"))
    assert cache["last_pycc_job_names"] == ["tb-cpp:tb_dut"]
    assert cache["design_cache_fast_path"] is True
    assert dut_obj.stat().st_mtime_ns == dut_obj_mtime
    assert tb_obj.stat().st_mtime_ns > tb_obj_mtime


def test_split_build_tb_only_fast_path_does_not_import_dut(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    design = case_dir / "dut_design.py"
    tb = case_dir / "dut_tb.py"
    out_dir = tmp_path / "out"
    fake_pycc = tmp_path / "fake_pycc.py"
    _write_fake_pycc(fake_pycc)

    design.write_text(
        "\n".join(
            [
                "import os",
                "from pycircuit import Circuit, module, u",
                "",
                "if os.environ.get('DESIGN_IMPORT_FAIL'):",
                "    raise RuntimeError('DUT import should be skipped on TB-only rebuild')",
                "",
                "@module",
                "def build(m: Circuit, width: int = 8) -> None:",
                "    x = m.input('x', width=width)",
                "    m.output('y', x + u(width, 0))",
                "",
                "build.__pycircuit_name__ = 'dut'",
                "",
            ]
        ),
        encoding="utf-8",
    )
    tb.write_text(
        "\n".join(
            [
                "from pycircuit import Tb, testbench",
                "",
                "@testbench",
                "def tb(t: Tb) -> None:",
                "    t.drive('x', 1, at=0)",
                "    t.expect('y', 1, at=0)",
                "    t.finish(at=1)",
                "",
            ]
        ),
        encoding="utf-8",
    )

    first = _run_build(repo, design, tb, out_dir, fake_pycc)
    assert "jit-cache: miss" in first.stdout

    time.sleep(1.1)
    tb.write_text(
        "\n".join(
            [
                "from pycircuit import Tb, testbench",
                "",
                "@testbench",
                "def tb(t: Tb) -> None:",
                "    t.drive('x', 2, at=0)",
                "    t.expect('y', 2, at=0)",
                "    t.finish(at=1)",
                "",
            ]
        ),
        encoding="utf-8",
    )

    second = _run_build(repo, design, tb, out_dir, fake_pycc, extra_env={"DESIGN_IMPORT_FAIL": "1"})
    assert "jit-cache: hit" in second.stdout
    cache = json.loads((out_dir / ".build_cache.json").read_text(encoding="utf-8"))
    assert cache["design_cache_fast_path"] is True
    assert cache["last_pycc_job_names"] == ["tb-cpp:tb_dut"]
