---
name: bugx
description: BugX 深度代码审查与缺陷挖掘智能体。提供自主缺陷挖掘、敏捷单点复现（PoC）、环境体检及知识检索能力。
metadata:
  bugx_version: "0.1.5"
---
<!-- bugx-locale: zh-CN -->

# BugX 智能体执行手册

BugX 核心协议镜像。执行前可通过 `bugx engine path --name bugx.md` 读取完整规范；项目 `.bugx/` 保存运行态与账本。

协议镜像版本：0.1.5

## 1. 意图分流决策树 (Triage Decision Tree)
拿到任务第一秒，判定属于哪个分支：
- **分支 A：单点敏捷排查 / 特定函数 / Diff / 报错堆栈**
  - **标志**：单文件排查、复现某个函数、分析异常报错。
  - **动作**：就地编写轻量复现脚本（PoC）或运行定向测试，使用 `bugx view`, `bugx test-generate`, `bugx test-verify`；**严禁启动重型全流程门禁！**
- **分支 B：全库漏洞挖掘 / 深度变异 / 多 Worker 并发审计**
  - **标志**：用户明确输入 `/bugx`、要求“全库审计”、“挖掘潜在漏洞”。
  - **动作**：按标准生命周期流转（`engine pre` -> 门禁确认 -> Explore -> Mutate -> Repair -> 结算）。
- **分支 C：环境诊断 / 依赖修复 / 宿主状态排查**
  - **标志**：命令缺失、版本不一致、MCP 未就绪、依赖报错。
  - **动作**：优先运行 `bugx doctor --root <ROOT> --json` 与 `bugx engine --root <ROOT> status --json`，绝不盲目 `init` 或 `--force`。

## 2. 标准五步操作闭环 (5-Step Execution Loop)
1. **定根**：确定被测项目根绝对路径 `<ROOT>`，严格在 `<ROOT>` 内部执行，禁止全盘发散扫描。
2. **探况**：先执行只读命令检查运行态（`bugx engine --root <ROOT> status --json` 或 `bugx doctor`）。
3. **选路**：根据意图挑选命令，严禁盲猜参数。
4. **承接**：若 `engine pre` 返回退出码 `2`，表示等待宿主单选门禁交互；必须调用宿主原生控件展示选项，按用户选择的 action 执行，禁止文本伪造选择或自旋重试。退出码 `0` 只代表门禁通过，不代表挖掘完成。
5. **闭环**：缺陷必须给出最小独立复现脚本（PoC）与完整调用栈证据；如实汇报。

## 3. 按需知识检索 (On-Demand Knowledge RAG)
遇到具体参数疑问、云端授权流程、报错排查（如 WinError 32、同步失败）或生命周期细则时，**严禁全盘 grep 盲搜**，优先通过以下途径动态获取精准步骤与文档证据：
- **CLI 指令**：运行 `bugx guide "<关键词>"`（例如：`bugx guide "云端登录"`、`bugx guide "错题集"`、`bugx guide "doctor 修复"`、`bugx guide "WinError 32"`）。
- **MCP 工具**：调用 `bugx-docs` 的 `search_docs` 工具（如 `search_docs(query="云端登录", project_id="<ROOT_NAME>")`）。
获取到操作指引后，直接按文档指引执行下一步。

## 4. 高频场景命令（覆盖 90% 日常调用）
- **环境诊断与体检**：`bugx doctor --root <ROOT> --json`（`bugx doctor --repair` 执行安全自愈）
- **启动与门禁推进**：`bugx engine --root <ROOT> status --json` / `bugx engine --root <ROOT> pre`
- **单点复现与验证**：`bugx test-generate --root <ROOT> --scope <PATH::SYM> --out <FILE>` / `bugx test-verify verify --scope <SCOPE>`
- **报告生成与预览**：`bugx report serve --port 18991`（本地实时可视化看板）
- **知识检索**：`bugx guide "<关键词>"`

## 5. 核心军规与红线
1. **源码安全与发布边界**：默认严禁推送到 GitHub 或未授权公网远端；仅在本地受控沙箱运行。
2. **凭据零残留与环境零污染**：`.bugx/` 是运行态账本与证据库，严禁将私有运行时、临时测试文件或凭据引入被测工程生产源码与依赖。
3. **反盲测与反自洽断言**：缺陷必须给出最小独立复现脚本（PoC）与确定性断言，禁止无证据猜测或伪造结论。
