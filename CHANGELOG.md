# Changelog

All notable changes to the **BugX** project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note for Users**: Granular internal commit notes from previous alpha/patch development cycles are kept in internal development records.

## [0.1.2] - 2026-11-23

### 独立盲审自纠错闭环 (P0)
- 新增独立盲审验证协议（`core/prompts/stages/stage_verify.md`），验证员在隔离沙箱环境中基于最小信息差复现缺陷，彻底消除首发模型的认知偏见与幻觉自洽。
- 扩充 8 类精确诊断标签（`VerificationDiagnostic`），引入 4KB 严格脱敏截断、AST 负对照语法树校验与源码 SHA-256 指纹比对，确保 PoC 验证期间被测代码零篡改。
- 实现有界自纠错循环，支持最多 3 次报错自纠错重试，集成验证死锁自旋熔断与预算耗尽保护。

### 冷启动漏洞先验包与负样本误报账本 (P1)
- 内置 6 大技术栈（Python / Go / JavaScript / Java / 鉴权边界 / 输入输出）25 组高危反模式机读先验包，支持 Reciprocal Rank Fusion (RRF) 检索融合与字符预算上限控制。
- 引入负样本误报账本（FP Ledger），验证实跑证伪的假阳性案例自动沉淀入库并绑定源文件 SHA-256；文件变更后自动失效，杜绝历史误报重复骚扰的同时防止掩盖新漏洞。

### Recon 轻量语义侦察叠加层 (P2)
- 自动提炼隐式路由映射、鉴权边界划分与关键业务不变量；双通道自适应（优先 LLM 语义概括，离线无网时自动降级至 AST 纯静态启发式分析）。
- 物理级真实锚点核验：所有提取的路由与函数必须对应磁盘真实存在的物理符号，虚构实体丢弃率 100%。

### 提示词策略引擎 (Prompt Policy)
- 针对安全漏洞扫描时大模型平台（如 OpenAI Codex 等）触发的风控拦截问题，构建配置驱动的提示词策略引擎（`standard`、`defensive_qa`、`strict_compliance` Profile）。
- 支持响应式拦截探测与防御性表达自适应重试，代码围栏与 CWE 编号 100% 物理保留。

### CLI 命令契约与 SEO 增强
- `bugx update` 精准匹配安装来源（pip / npm wrapper）进行差异更新；统一全系统 CLI 布尔开关参数规范。
- 注入实测高价值安全与审计关键词（`vibe-coding`, `pr-review`, `zero-false-positive`, `poc-generator`, `mcp-security`）。
- 静态引擎完整性清单 `core/engine-integrity.json` 重新计算并对齐。

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

