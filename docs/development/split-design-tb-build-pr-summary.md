# PR 说明：Design / Testbench 解耦构建

## 完成状态

本 PR 已完成 `pycircuit.cli build` 路径下的 design / testbench 解耦构建改造。

这里的“完成”指的是：

- design Python 入口和 TB Python 入口可以通过 CLI 显式拆开。
- 只修改 TB 时，design JIT cache 不应失效。
- 只修改 TB 时，不应重新生成 DUT C++。
- 只修改 TB 时，不应重新编译 DUT object。
- TB 修改后仍会重新生成/编译 TB，并 relink 最终仿真可执行文件。

最终 `pyc_tb` 可执行文件仍然会链接 DUT object、TB object 和 runtime。这是仿真运行所需的正常耦合，不属于本 PR 要消除的目标。

需要特别澄清的是：当前并不是把 DUT 和 TB 合成同一个 `.cpp` 文件。DUT
会生成自己的 C++ source/header，例如 `device/cpp/<dut>/<dut>.cpp` 和
`device/cpp/<dut>/<dut>.hpp`；TB 会生成自己的 C++ source，例如
`tb/tb_<dut>.cpp`。CMake 会把这些 source 放进同一个 `pyc_tb` executable
target 中，但它们仍然作为不同的 C++ translation unit 分别编译成 object，
最后再链接成同一个仿真可执行文件。

## 新 CLI 用法

新增 split build 入口：

```bash
python3 -m pycircuit.cli build \
  --design <design.py> \
  --tb <tb.py> \
  --out-dir <dir> \
  --target cpp
```

旧入口仍然兼容：

```bash
python3 -m pycircuit.cli build <tb_or_top.py> --out-dir <dir>
```

## 主要改动

- `build()` 只从 `--design` 模块读取。
- `@testbench tb()` 只从 `--tb` 模块读取。
- design cache key 与 TB backend cache key 分离。
- device C++、device Verilog、TB C++、TB SV 的 backend flag cache 分离。
- probe 扫描同时覆盖 design module 和 TB module。
- `.build_cache.json` 增加 `last_pycc_job_names`，用于验证本次增量构建行为。
- 新增回归测试覆盖 TB-only 修改场景。

## 期望增量行为

| 修改内容 | 期望行为 |
| --- | --- |
| 只改 TB | design cache hit；不重跑 DUT C++ 生成；不重编 DUT object；只重编 TB 并 relink |
| 改 DUT 实现但接口不变 | 重新生成/编译 DUT；最终 relink |
| 改 DUT 接口/header | DUT 和 TB 都按 C++ header 依赖重新编译 |
| 改 runtime/common header | 依赖相关 header 的对象按 CMake/Ninja 依赖重新编译 |

## 验证

已增加并运行：

```bash
PYTHONPATH=compiler/frontend .venv/bin/python -m pytest tests -q
git diff --check
```

测试覆盖点：

- 第一次 split build 生成 DUT/TB/CMake 产物。
- 只修改 TB 文件。
- 第二次 split build 命中 design cache。
- `.build_cache.json` 中 `last_pycc_job_names` 只包含 `tb-cpp:*`。
- DUT object mtime 不变。
- TB object mtime 更新。

## Review 范围

重点看以下源码位置：

| 源码路径 | 行号 | 修改内容 |
| --- | --- | --- |
| `compiler/frontend/pycircuit/cli.py` | `80-95` | 允许 build 命令把 DUT 文件和 TB 文件分开传入，同时旧用法还能继续用。 |
| `compiler/frontend/pycircuit/cli.py` | `2633-2671` | 查找 probe 时同时看 DUT 文件和 TB 文件，避免拆文件后漏掉 probe。 |
| `compiler/frontend/pycircuit/cli.py` | `2729-2748` | build 流程开始时分别读取 DUT 和 TB，不再默认它们来自同一个 Python 文件。 |
| `compiler/frontend/pycircuit/cli.py` | `2756-2775` | 判断 DUT 是否需要重新处理时，只看 DUT 相关输入，不再被 TB 修改影响。 |
| `compiler/frontend/pycircuit/cli.py` | `2824-2844` | 把 DUT 生成参数和 TB 生成参数分开记录，避免一边变化误伤另一边。 |
| `compiler/frontend/pycircuit/cli.py` | `2933-3005` | 决定是否重跑代码生成时，DUT 和 TB 分开判断；只改 TB 时跳过 DUT 生成。 |
| `compiler/frontend/pycircuit/cli.py` | `3182-3205` | 把本次构建实际重跑了哪些生成任务写进 cache，方便排查增量行为。 |
| `compiler/frontend/pycircuit/cli.py` | `3339-3354` | 给 build 命令增加 `--design` 和 `--tb` 两个入口参数。 |
| `flows/tools/gen_cmake_from_manifest.py` | `73-79` | 这里说明最终仍是同一个仿真程序，但 DUT `.cpp` 和 TB `.cpp` 是分开编译的文件。 |
| `tests/test_cli_split_build_incremental.py` | `137-170` | 测试里按新用法分别传入 DUT 文件和 TB 文件。 |
| `tests/test_cli_split_build_incremental.py` | `180-250` | 测试只修改 TB 后，确认 DUT 没有重新编译，只有 TB 相关任务重跑。 |

## 当前边界

本 PR 不调整 CMake target 结构。当前仍然是 DUT `.cpp` 和 TB `.cpp` 分别编译为 object，最后链接成同一个 `pyc_tb`。因此这里的“解耦编译”指的是 source 生成、pycc backend job 和 C++ object 编译的增量边界解耦；最终仿真程序仍然是一个 executable。

如果后续要支持一个 DUT 对多个 TB 复用，可以继续把 DUT C++ sources 提升成独立 `OBJECT` 或 `STATIC` target。
