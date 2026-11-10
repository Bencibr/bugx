# BugX CLI 命令选路与使用手册 (Command Routing Guide)

## 如何使用本技能（AI 执行元指南与意图决策树）

当你（AI）触发并阅读本技能时，请无条件遵循以下认知模型与执行时序：

### 1. 意图决策树（Triage Decision Tree）
拿到任务第一秒，判定属于哪个分支：
- **分支 A：单点敏捷排查 / 特定函数 / Diff / 报错堆栈**
  - **标志**：单文件排查、复现某个函数、分析异常报错。
  - **动作**：就地编写轻量复现脚本（PoC）或运行定向测试，使用 `bugx view`, `bugx test-generate`, `bugx test-verify`；**严禁启动重型全流程门禁！**
- **分支 B：全库漏洞挖掘 / 深度变异 / 多 Worker 并发审计**
  - **标志**：用户明确输入 `/bugx`、要求“全库审计”、“挖掘潜在漏洞”。
  - **动作**：按标准生命周期流转（`engine pre` -> 门禁确认 -> Explore -> Mutate -> Repair -> 结算）。
- **分支 C：环境诊断 / 依赖修复 / 宿主状态排查**
  - **动作**：优先运行 `bugx doctor --root <ROOT> --json` 与 `bugx engine --root <ROOT> status --json`，绝不盲目 `init` 或 `--force`。

### 2. 标准五步操作闭环（5-Step Execution Loop）
1. **定根**：确定被测项目根绝对路径 `<ROOT>`，严格在 `<ROOT>` 内部执行，禁止全盘发散扫描。
2. **探况**：先执行只读命令检查运行态（`bugx engine --root <ROOT> status --json` 或 `bugx doctor`）。
3. **选路**：根据意图挑选命令；若需查阅参数，运行 `bugx help <顶层命令>`（如 `bugx help cloud`）按需动态读取，严禁盲猜。
4. **承接**：若 `engine pre` 返回退出码 `2`，表示等待宿主单选门禁交互；必须调用宿主原生控件展示选项，按用户选择的 action 执行，禁止文本伪造选择或自旋重试。退出码 `0` 只代表门禁通过，不代表挖掘完成。
5. **闭环**：缺陷必须给出最小独立复现脚本（PoC）与完整调用栈证据；使用 6 大标准汇报用词（只读检查、项目就绪、门禁就绪、测试执行、挖掘启动、挖掘完成）如实汇报。

### 3. 高频场景命令（覆盖 90% 日常调用）
- **环境诊断**：`bugx doctor --root <ROOT> --json`（`--repair-assets` 按需修复）
- **启动与门禁**：`bugx engine --root <ROOT> status --json` / `bugx engine --root <ROOT> pre`（全新项目使用 `bugx init --root <ROOT>` 初始化）
- **单点复现与变异**：`bugx test-generate --root <ROOT> --scope <PATH::SYM> --out <FILE>` / `bugx test-verify verify --scope <SCOPE>`
- **Supervisor 与并发排查**：`bugx supervisor status --root <ROOT>`
- **脱敏飞轮与日志**：`bugx telemetry inspect` / `bugx log --root <ROOT> --errors`
- **双轨报告与预览**：`bugx report both --eval-dir evals/oss --out evals/oss` / `bugx report serve --port 18991`

### 4. 紧凑型命令全景雷达（41 个顶层命令分类索引）
如需调用以下命令，通过 `bugx help <命令>` 动态获取具体参数与子命令，严禁盲猜：
- **诊断探索 (9)**：`help`, `doctor`, `hosts`, `completion`, `log`, `stats`, `view`, `project`, `self-audit`
- **核心生命周期 (14)**：`init`, `engine`, `supervisor`, `daemon`, `config`, `reset`, `install`, `uninstall`, `dsh`, `plugin`, `mcp`, `mcp-runtime`, `capability`, `logic-analysis`
- **深度审计验证 (10)**：`audit`, `probe`, `test-generate`, `test-audit`, `test-verify`, `patch-forge`, `test-strategy`, `fuzz-input`, `differential-probe`, `mining-audit`
- **知识飞轮交付 (8)**：`report`, `lesson`, `knowledge`, `skill`, `cloud`, `auth`, `telemetry`, `advisory`

### 5. 核心军规与废弃命令重定向
- **阻断不授权暴力修复**：`FATAL_BLOCKED` 严禁使用 `init`、`reset`、`--force` 绕过，先保留报告和日志。
- **废弃命令重定向**：`matrix` -> `bugx audit`；`console` -> `bugx report serve`；`watch` -> `bugx log --watch`；`test` 为内部脚本改用 `scripts/test_selector.py`。
