# PR Review Guide: DUT / Testbench Split Build

本文用于 review 本 PR 的代码修改。review 重点是确认 `pycircuit.cli build`
在 `pycc -> C++/Verilog source generation` 层面完成 DUT/TB 解耦，同时不破坏
旧的单文件 build 入口。

## Review 结论先验

本 PR 的目标不是把最终仿真程序拆成多个独立 CMake target。当前仍然生成一个
`pyc_tb` executable，里面链接 DUT object、TB object 和 runtime。

本 PR 要验证的是：

- DUT Python 入口和 TB Python 入口可以拆开传入。
- 只改 TB 时，不 import DUT，不 JIT DUT，不运行 DUT pycc job。
- 只跑 `--dut` 时，不要求 TB，不生成 TB `.pyc`、TB C++/SV、CMake executable。
- 只跑 `--tb` 时，只复用 `--out-dir` 中已有 DUT cache/artifacts，不自动回退编 DUT。
- 参数化 DUT 的 `--param` cache 不会被 tb-only 构建污染。

## CLI 行为

新增公开 CLI：

```bash
python3 -m pycircuit.cli build --dut <dut.py> --tb <tb.py> --out-dir <dir> --target cpp
python3 -m pycircuit.cli build --dut <dut.py> --out-dir <dir> --target cpp
python3 -m pycircuit.cli build --tb <tb.py> --out-dir <dir> --target cpp
```

旧入口仍兼容：

```bash
python3 -m pycircuit.cli build <tb_or_top.py> --out-dir <dir>
```

`--design` 目前只作为隐藏兼容别名保留，不出现在 help 文本中。review 时应确认
公开文档和测试都使用 `--dut`。

## 四种 Build Mode

模式判定在 `compiler/frontend/pycircuit/cli.py` 的
`_resolve_build_source_args()`，当前大约位于 `80-101` 行。

| mode | 触发命令 | 语义 |
| --- | --- | --- |
| `single` | `build top.py` | 旧模式。DUT `build()` 和 TB `tb()` 都从同一个 Python 入口读取。 |
| `split` | `build --dut dut.py --tb tb.py` | 完整 split build。CLI 同时知道 DUT 和 TB，可自动增量。 |
| `dut-only` | `build --dut dut.py` | 只生成 DUT 侧 `.pyc` 和 pycc outputs，不生成 TB。 |
| `tb-only` | `build --tb tb.py` | 只生成 TB 侧 `.pyc` 和 pycc outputs，要求复用已有 DUT cache/artifacts。 |

Review 要点：

- `single` 仍把同一个 path 返回给 DUT/TB，保证旧用法兼容。
- `split` 返回两个不同 path，后续流程可分别 import/scan。
- `dut-only` 的 `tb_src` 必须是 `None`。
- `tb-only` 的 `design_src` 必须是 `None`，后续不得触发 DUT import/JIT。

## 代码 Review 路径

建议按下面顺序 review。

### 1. 参数解析

文件：`compiler/frontend/pycircuit/cli.py`

重点位置：

- `_resolve_build_source_args()`：约 `80-101` 行
- argparse `build.add_argument(...)`：约 `3649-3666` 行

需要确认：

- `--dut` 是公开参数。
- `--tb` 是公开参数。
- positional 与 `--dut/--tb` 不能混用。
- `--design` 只作为 `argparse.SUPPRESS` 的兼容别名。
- 错误提示使用 `--dut/--tb`，不再引导用户使用 `--design`。

### 2. Cached DUT artifact loader

文件：`compiler/frontend/pycircuit/cli.py`

重点位置：

- `_load_cached_design_artifacts()`：约 `2640-2676` 行
- `_try_load_cached_design_artifacts()`：约 `2679-2704` 行

需要确认：

- tb-only 只读取 `project_manifest.json`、`.build_cache.json`、`device/design.pyc`
  和 `device/modules/*.pyc`。
- 这里不 import DUT Python 源文件。
- split fast path 会检查 `dut_src/design_src`、`param_overrides` 和 design dependency fingerprint。
- 缓存 key 使用 `_canonical_hash(design_cache_inputs)` 重新校验，不能只信任 cache 字段。

### 3. TB-only fast path

文件：`compiler/frontend/pycircuit/cli.py`

重点位置：

- `_cmd_build()` 开头：约 `2851-2872` 行
- tb-only cache 加载：约 `2947-3053` 行
- DUT pycc job 禁止回退：约 `3133-3201` 行
- TB pycc job 生成：约 `3298-3324` 行

需要确认：

- `tb_only` 时如果显式传入不同 `--param`，必须失败。
- `tb_only` 时如果没有传 `--param`，应保留 cache 中已有 DUT 参数。
- `tb_only` 只加载 cached design artifacts，不进入 DUT import/JIT 路径。
- `tb_only` 如果缺 probe catalog、DUT C++、DUT Verilog 等 artifacts，必须失败。
- `tb_only` 不能自动追加 `probe-catalog`、`cpp:<dut>`、`verilog:<dut>` jobs。
- `tb_only` 只允许追加 `tb-cpp:<tb>` 或 `tb-sv:<tb>` jobs。
- TB 文件如果新增 `@probe`，当前不能在 tb-only 下重新解析 DUT，应失败并提示跑完整 split build。

### 4. DUT-only path

文件：`compiler/frontend/pycircuit/cli.py`

重点位置：

- `has_tb == False` guard：约 `2889-2896` 行
- 清理 TB manifest 字段：约 `3254-3256` 行
- TB/CMake/Verilator guard：约 `3298` 行之后的 `has_tb` 条件

需要确认：

- `--dut` 单独可以完成 DUT JIT、multi `.pyc` emit、probe manifest/plan、DUT C++/Verilog emit。
- `--dut` 单独不生成 `tb/*.pyc`、`tb/*.cpp`、`tb/*.sv`。
- `--dut` 单独不生成 `cpp_build`/`pyc_tb` executable。
- `--trace-config`、`--run-verilator`、`--run-arg` 在没有 TB 时应直接失败。

### 5. Probe 行为

文件：`compiler/frontend/pycircuit/cli.py`

重点位置：

- `_resolve_probe_outputs()`：约 `2755-2848` 行
- 调用处：约 `3161-3174` 行

需要确认：

- full split build 会同时扫描 DUT module 和 TB module。
- tb-only fast path 不重新解析 DUT probe plan，只复用缓存中的
  `probe_manifest.json` 和 `probe_plan.json`。
- TB 侧新增 `@probe` 时不能静默复用旧 probe plan。

### 6. Cache 输出

文件：`compiler/frontend/pycircuit/cli.py`

重点位置：

- cache 写回：约 `3485-3515` 行

需要确认：

- `dut_src` 被写入 cache。
- 旧 `design_src` 仍写入，用于兼容旧 cache。
- `param_overrides` 在 tb-only 下保持 DUT cache 中的值，不被空列表覆盖。
- `last_pycc_job_names` 可用于验证本次实际运行了哪些 pycc job。
- `device_backend_flags_hash`、`device_cpp_backend_flags_hash`、
  `device_verilog_backend_flags_hash`、`tb_backend_flags_hash` 分别记录。

## 测试 Review

文件：`tests/test_cli_split_build_incremental.py`

重点测试：

- `test_build_dut_only_generates_only_dut_cpp`
  - 验证 `--dut` 单独不生成 TB/CMake artifacts。
  - 期望 jobs: `["probe-catalog", "cpp:dut"]`

- `test_build_tb_only_reuses_design_cache_without_importing_dut`
  - 先跑 `--dut`，再跑 `--tb`。
  - 第二次设置环境变量让 DUT import 必崩。
  - 如果 tb-only 误 import DUT，测试会失败。

- `test_build_tb_only_preserves_cached_dut_params`
  - 先用 `--param width=16` 跑 `--dut`。
  - 再跑 `--tb`，确认 cache 中参数仍是 `["width=16"]`。
  - tb-only 显式传不同 `--param width=8` 必须失败。

- `test_split_build_tb_only_change_does_not_recompile_dut`
  - 完整 split build 后只改 TB。
  - 检查 `last_pycc_job_names == ["tb-cpp:tb_dut"]`。
  - 检查 DUT object mtime 不变。

- `test_split_build_tb_only_fast_path_does_not_import_dut`
  - 完整 split build 后只改 TB。
  - 第二次构建设置环境变量让 DUT import 必崩。
  - 验证 split fast path 不 import DUT。

Review 时重点看 fake `pycc` 是否能覆盖 cache/manifest/CMake/Ninja 路径，而不是只 mock
Python 函数。当前测试通过真实 CLI 子进程、真实 `.build_cache.json`、真实 CMake/Ninja
路径，只替换 pycc binary。

## 推荐验证命令

基础检查：

```bash
cd /home/hcy/arch_exp/low_precision_alu_lang/pyCircuit

python -m py_compile compiler/frontend/pycircuit/cli.py

PYTHONPATH=compiler/frontend python -m pytest \
  tests/test_cli_split_build_incremental.py \
  tests/test_pycstb4_sections.py \
  tests/test_tb_section_api.py -q

git diff --check
```

检查公开 CLI：

```bash
PYTHONPATH=compiler/frontend python -m pycircuit.cli build --help
```

期望：

- help 中出现 `--dut DUT`
- help 中出现 `--tb TB`
- help 中不出现公开 `--design`

手工 smoke test 可以使用 `docs/development/split-design-tb-build.md` 中的命令，
或直接运行：

```bash
python -m pycircuit.cli build --dut /tmp/pyc_split_cli_test/dut.py \
  --out-dir .pycircuit_out/manual_split_cli --target cpp --jobs 1

python -m pycircuit.cli build --tb /tmp/pyc_split_cli_test/tb.py \
  --out-dir .pycircuit_out/manual_split_cli --target cpp --jobs 1
```

检查 cache：

```bash
python - <<'PY'
import json
from pathlib import Path
out = Path(".pycircuit_out/manual_split_cli")
cache = json.loads((out / ".build_cache.json").read_text())
print("mode:", cache.get("build_mode"))
print("fast_path:", cache.get("design_cache_fast_path"))
print("jobs:", cache.get("last_pycc_job_names"))
print("dut_src:", cache.get("dut_src"))
print("params:", cache.get("param_overrides"))
PY
```

## Review 通过标准

可以认为本 PR 通过代码 review 的条件：

- `--dut`、`--tb`、`--dut --tb`、legacy positional 四种入口语义清楚。
- tb-only 不可能 import/JIT/pycc DUT。
- tb-only 缺 DUT cache/artifacts 时失败，不自动回退。
- dut-only 不生成 TB artifacts。
- 参数化 DUT 的 cache 不会被 tb-only 污染。
- split build 只改 TB 时 `last_pycc_job_names` 只包含 TB jobs。
- 文档、help、测试都使用 `--dut` 作为公开入口。

## 当前边界

- 本 PR 不拆 CMake target。最终仍是一个 `pyc_tb` executable。
- CMake/Ninja 是否重编 object 由 C++ 依赖决定；本 PR 主要保证 pycc source
  generation 层面的 DUT/TB 解耦。
- TB 文件如果自己 `import` DUT Python 文件，Python import 仍会执行 DUT 顶层代码；
  CLI 无法替 TB 隔离这种显式依赖。
- TB 侧新增或修改 `@probe` 时，当前需要完整 split build，因为 probe plan 依赖
  DUT probe catalog 和 DUT hierarchy artifacts。
