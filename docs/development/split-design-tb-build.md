# DUT 源码与 Testbench 构建解耦说明

本文描述本次 PR 对 `pycircuit.cli build` 的增量构建改动。目标是让
testbench-only 修改尽量表现得像常规语言工程：只重新生成和编译 TB，
不重新编译 DUT。

## 背景

原有 `build` 入口只接受一个 Python 文件：

```bash
python3 -m pycircuit.cli build <tb_or_top.py> --out-dir <dir>
```

这个文件通常同时 import/暴露 `build()` 和定义 `@testbench tb()`。因此
前端 cache key 会把设计入口和 TB 入口混在一起。只修改 TB 时，设计 JIT
阶段也可能被重新触发；同时部分 TB 相关参数也会混入 DUT 后端 cache
判断，导致 DUT C++ 生成任务不够独立。

## 新接口

本次新增 split build 入口。完整构建同时给 DUT 和 TB：

```bash
python3 -m pycircuit.cli build \
  --dut <dut.py> \
  --tb <tb.py> \
  --out-dir <dir> \
  --target cpp
```

只重新生成 DUT 侧 pycc 输出时，可以只给 `--dut`：

```bash
python3 -m pycircuit.cli build \
  --dut <dut.py> \
  --out-dir <dir> \
  --target cpp
```

只重新生成 TB 侧 pycc 输出时，可以只给 `--tb`。该模式不会 import/JIT/pycc
DUT，要求 `<dir>` 中已经有同一 pycc flags/target 对应的 DUT cache：

```bash
python3 -m pycircuit.cli build \
  --tb <tb.py> \
  --out-dir <dir> \
  --target cpp
```

兼容性保持不变：旧的单文件入口仍然可用。

```bash
python3 -m pycircuit.cli build <tb_or_top.py> --out-dir <dir>
```

当使用完整 split 入口时：

- `build()` 只从 `--dut` 指定的模块读取。
- `@testbench tb()` 只从 `--tb` 指定的模块读取。
- API contract scan 会分别覆盖 DUT 文件和 TB 文件。
- probe 解析会同时扫描 DUT module 和 TB module，避免 split 入口漏掉
  定义在任一侧的 `@probe`。

当使用 dut-only 入口时：

- 只读取 `--dut`，生成 `device/design.pyc`、`device/modules/*.pyc`、
  `probe_manifest.json`、`probe_plan.json` 和目标对应的 DUT C++/Verilog。
- 不要求 TB 文件，不生成 TB `.pyc`、TB C++/SV，也不生成 CMake testbench
  executable。

当使用 tb-only 入口时：

- 只读取 `--tb`，从 `--out-dir` 的 `project_manifest.json` 和 cache 还原 DUT
  接口、module `.pyc`、probe manifest/plan 以及目标 DUT artifacts。
- 不 import `--dut`，也不会回退到 DUT JIT 或 DUT pycc。
- 不带 `--param` 时会保留缓存中的 DUT 参数；若显式传入 `--param`，必须与
  缓存中的 DUT 参数完全一致，否则命令失败并要求先重新运行 DUT 构建。
- 若 DUT cache 缺失、目标 artifacts 缺失、pycc/device flags 不匹配，命令会
  失败并要求先运行 dut-only 或完整 split 构建。

## Cache 边界

本次把原先较粗的 build flag/cache 拆成几组独立判断：

- `design_cache_key`
  - 只包含 design 源依赖、JIT 参数、top name 和 frontend contract。
  - TB-only 修改不应改变该 key。
- `device_backend_flags_hash`
  - 影响 probe catalog 生成。
- `device_cpp_backend_flags_hash`
  - 影响 DUT C++ 生成。
  - 包含 `probe_plan_hash`，因为 probe alias 变化会影响 DUT C++ 输出。
- `device_verilog_backend_flags_hash`
  - 影响 DUT Verilog 生成。
- `tb_backend_flags_hash`
  - 影响 TB C++/SV 生成。
  - 包含 `tb_schedule_mode` 和 `tb_schedule_format`。

`.build_cache.json` 现在额外记录 `last_pycc_job_names`，用于调试和回归测试。
例如 TB-only 修改后，期望只看到：

```json
["tb-cpp:tb_dut"]
```

当 split 入口命中完整 DUT cache 时，`build` 会走 TB-only fast path：

- 不 import `--dut` Python module。
- 不重新执行 DUT API contract scan。
- 不重新执行 DUT JIT elaboration。
- 不重新运行 DUT `pycc` C++/Verilog/probe-catalog 任务。
- 只 import `--tb`、重新生成 TB `.pyc`，并按目标重新生成/编译 TB。

fast path 依赖上一次完整构建写出的 `project_manifest.json`、`device/*.pyc`、
`probe_manifest.json`、`probe_plan.json`、DUT C++/Verilog artifacts 以及
`.build_cache.json` 中的 design dependency fingerprint。若这些 artifact
缺失、DUT 源依赖 hash 变化、`--param` 覆盖变化、pycc/device flags 变化、
目标 artifact 不完整，或者 TB 文件定义了新的 `@probe`，构建会退回完整路径。

## 期望的增量行为

| 修改内容 | 期望行为 |
| --- | --- |
| 只改 TB 行为 | 命中 TB-only fast path；不 import/JIT/pycc/编译 DUT；重新生成/编译 TB 并 relink |
| 改 DUT 实现但接口不变 | 可用 `--dut` 单独重新生成 DUT C++；TB 源码可保持不变 |
| 改 DUT 接口/header | DUT 和 TB 都需要重新编译，因为 TB include DUT header |
| 改 runtime/common header | 依赖相关 header 的对象按 CMake/Ninja 依赖重新编译 |

本次 PR 主要解决第一类场景。

## 回归测试

新增测试：

```bash
PYTHONPATH=compiler/frontend \
  .venv/bin/python -m pytest tests/test_cli_split_build_incremental.py -q
```

测试使用一个 fake `pycc`，真实执行 CLI、cache、manifest、CMake 和 Ninja
路径，但避免依赖完整 MLIR 后端输出。覆盖流程：

1. `--dut` 单独构建，检查只生成 DUT C++，不生成 TB/CMake artifacts。
2. 先 `--dut`，再 `--tb`，检查 TB-only 不 import DUT 且只运行
   `["tb-cpp:tb_dut"]`。
3. 完整 split build 后只修改 TB 文件，记录 DUT object 和 TB object 的 mtime。
4. 第二次 split build，检查：
   - stdout 包含 `jit-cache: hit`。
   - `.build_cache.json` 中 `design_cache_fast_path == true`。
   - `.build_cache.json` 中 `last_pycc_job_names == ["tb-cpp:tb_dut"]`。
   - DUT object mtime 不变。
   - TB object mtime 更新。

另有一条回归会在第二次构建时设置环境变量，让 DUT module 一旦被 import
就抛异常；该测试用于证明 TB-only fast path 不执行 DUT Python 入口。

## 当前边界

- 旧的单文件入口仍可用，但它天然会把 DUT/TB 放在同一个 Python 入口里；
  推荐新项目和 PR 验收使用 `--dut/--tb`。
- TB 文件必须保持独立；如果 TB 自己 `import` DUT design 文件，Python import
  仍会执行 DUT 顶层代码，CLI 无法替 TB 隔离这种依赖。
- TB 侧新增或修改 `@probe` 定义时，为保证 probe plan 正确性，当前会退回完整
  DUT/TB probe 解析路径。
- CMake 目标结构暂未改成独立 DUT library target。当前已经是 DUT `.cpp`
  和 TB `.cpp` 分别编译成 object，再链接成同一个 `pyc_tb`，足够覆盖本次
  TB-only 增量目标。
- 后续如果需要一个 DUT 对多个 TB 复用，可以再把 DUT C++ sources 提升成
  `OBJECT` 或 `STATIC` target。
