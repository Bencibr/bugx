# Changelog

All notable changes to the **BugX** project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note for Users**: Granular internal commit notes from previous alpha/patch development cycles are kept in internal development records.

## [0.1.1] - 2026-11-19

### 宿主矩阵：zcode 全轴闭合
- zcode CLI wrapper（ZCode Desktop 内置 `zcode.cjs` v0.17.1）挂载到 PATH；探针修正为用户级注册语义（`~/.zcode/agents/bugx.md` 存在即宿主可用），`bugx hosts` 双真。
- ZCode Desktop 真实加载三个 BugX MCP 服务，会话内 MCP 真调全通（status/gate/coverage/next_round/findings/verify/confirm）。
- 8 个 zcode 原生子智能体 worker 走通 `claim → worker_start → heartbeat → probe → checkpoint` 全链路，双轮 next_round；2 个并发满诚实拒止。证据入档宿主迁移清单覆盖矩阵。

### 移动轴真机挖掘完整闭环
- 靶场 exposure 14/14 全 covered；9/9 findings 全部 CONFIRMED（high×7 / medium×2），每条经引擎 verify 双正例+负对照实跑（SQL 注入 / 命令注入 / JSContext 动态执行 / Manifest 六种配置缺陷）。

### 引擎修复
- macOS memory pressure 探针恒 100% 根治：vm_stat 解析正则 `\s+` 字面转义永不命中五字段 → usage 恒 100% → 自适应并发永久压到 min=2；修复后实测恢复真实值（72.58%）。
- zcode host `project_marker` 用户级注册语义对齐。

### 版本治理
- 语义版本 0.1.0 → 0.1.1（37 处声明，人工确认 `--confirm-version-transition`）；补齐 5 处此前不被 bump 覆盖的 `BUGX_VERSION` env 回退默认值声明。
- 第三方 MCP 依赖刷新：`@mobilenext/mobile-mcp` 1.0.18、`@playwright/mcp` 0.0.86、`@bytebase/dbhub` 2.0.0（上游 breaking）；postmcp/codindex/redis-mcp-server 已最新。
- 全仓文档版本标头统一至 `0.1.1` 基线；历史审计快照保持当时基线。

