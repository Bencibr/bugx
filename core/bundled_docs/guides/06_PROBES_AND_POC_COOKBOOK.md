# BugX 探针选择、单点 PoC 与变异验证速查卡

> 适用场景：单文件敏捷漏洞复现（分支 A）、11 类专精探针选用、AST 变异测试杀伤率验证。

## 1. 分支 A：单点敏捷排查与最小复现闭环
当仅需定位/复现特定文件、函数或异常堆栈时，**严禁启动重型全库 `engine pre` 生命周期**，按四步极速收敛：
```bash
# 1. 安全读取目标源码切片（UTF-8 容错 + 越界自愈）
bugx view --root . src/auth.py --lines 1-120

# 2. 为目标符号生成标准化测试或最小自包含 PoC 草稿
bugx test-generate --root . --scope "src/auth.py::verify_token" --out "$TMPDIR/poc_auth.py"

# 3. 审计现有测试资产的断言与覆盖质量（四维量化）
bugx test-audit --scope "src/auth.py::verify_token" tests/test_auth.py --json

# 4. 沙箱变异杀伤力验证（mutate 仅预览变异体；verify 在隔离沙箱真实执行）
bugx test-verify mutate --root . --scope "src/auth.py::verify_token"
bugx test-verify verify --root . --scope "src/auth.py::verify_token"
```

## 2. 全量 11 类专项探针（`bugx probe <KIND>`）
调用格式：`bugx probe [--root ROOT] [--timeout-seconds 120..1800] <KIND> [probe_args ...]`

| `probe_kind` | 专项覆盖能力 | 典型触发场景与注意 |
|---|---|---|
| `sast` | 多语言 AST/正则静态危险汇扫描 | 全库初筛或 PR 增量静态扫描 |
| `differential` | 双输入行为差分对比（BASE vs PAYLOAD） | 逻辑越权、解析差异、安全绕过 (`SENTINEL=True`) |
| `pattern` | 高危已知反模式特征扫描 | 快速命中同类已知脆弱模式 |
| `codebase-memory` | 图谱调用链、影响面与未覆盖盲区 | 验证 `codebase-memory-mcp` 必装就绪状态 |
| `memory` | 内存泄漏/UAF/PSS 趋势专项 | 原生 C/C++/Rust（ASan/Valgrind）、Python/JVM/Android RSS |
| `ui` | 真实无头浏览器 Web UI 视觉、无障碍与键盘流 | 依赖 `bugx-cli[ui]` + Chromium；自带随包 `axe-core` |
| `mobile` | Android 组件暴露面、DeepLink、Intent 劫持 | 需配好 APK/设备证据边界 |
| `ios_binary` | Apple IPA/Mach-O/Plist 二进制安全检查 | iOS/macOS 原生二进制目标 |
| `platform` | 宿主与运行平台环境/权限探测 | 跨平台路径、符号链接、工具链路体检 |
| `efficiency` | 运行时热点与执行性能回归基线 | 性能瓶颈定位与吞吐验证 |
| `openwiki` | 外部参考知识辅助查证 | 仅授权公开语义查证 |

### Web UI 严格巡检示例
```bash
bugx probe ui --root . --url http://127.0.0.1:8080 --journeys --strict
# 支持 --color-scheme dark、--viewport NAME=WIDTHxHEIGHT、--reference-url <对比图>
```

## 3. Finding 立案四项铁律
1. 必须绑定真实文件与精确行号，禁止猜测函数名。
2. 必须同时运行正例输入（稳定触发缺陷）+ 负例对照（正常输入通过）。
3. `evidence.status` 只能填 `passed | failed | error | skipped | blocked`；`coverage` 才能填 `covered | blocked`。
4. 验证过程中创建的临时脚本统一置于 `$TMPDIR/bugx-tests-*`，验证完毕立即物理删除，**绝不遗留进项目生产源码**。
