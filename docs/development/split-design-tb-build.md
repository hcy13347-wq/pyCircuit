# 设计源码与 Testbench 构建解耦说明

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

本次新增 split build 入口：

```bash
python3 -m pycircuit.cli build \
  --design <design.py> \
  --tb <tb.py> \
  --out-dir <dir> \
  --target cpp
```

兼容性保持不变：旧的单文件入口仍然可用。

```bash
python3 -m pycircuit.cli build <tb_or_top.py> --out-dir <dir>
```

当使用 split 入口时：

- `build()` 只从 `--design` 指定的模块读取。
- `@testbench tb()` 只从 `--tb` 指定的模块读取。
- API contract scan 会分别覆盖 design 文件和 TB 文件。
- probe 解析会同时扫描 design module 和 TB module，避免 split 入口漏掉
  定义在任一侧的 `@probe`。

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

## 期望的增量行为

| 修改内容 | 期望行为 |
| --- | --- |
| 只改 TB 行为 | 设计 JIT cache hit；不重新生成 DUT C++；不重新编译 DUT object；重新生成/编译 TB 并 relink |
| 改 DUT 实现但接口不变 | 重新生成/编译 DUT；TB 源码可保持不变；最终 relink |
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
路径，但避免依赖完整 MLIR 后端输出。测试流程：

1. 写入一个最小 design 和一个独立 TB。
2. 第一次 split build，记录 DUT object 和 TB object 的 mtime。
3. 只修改 TB 文件。
4. 第二次 split build，检查：
   - stdout 包含 `jit-cache: hit`。
   - `.build_cache.json` 中 `last_pycc_job_names == ["tb-cpp:tb_dut"]`。
   - DUT object mtime 不变。
   - TB object mtime 更新。

## 当前边界

- 旧的单文件入口仍可用，但它天然会把 design/TB 放在同一个 Python 入口里；
  推荐新项目和 PR 验收使用 `--design/--tb`。
- CMake 目标结构暂未改成独立 DUT library target。当前已经是 DUT `.cpp`
  和 TB `.cpp` 分别编译成 object，再链接成同一个 `pyc_tb`，足够覆盖本次
  TB-only 增量目标。
- 后续如果需要一个 DUT 对多个 TB 复用，可以再把 DUT C++ sources 提升成
  `OBJECT` 或 `STATIC` target。
