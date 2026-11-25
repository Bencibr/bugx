# BugX 错题经验治理、先验语料与隐私红线速查卡

> 适用场景：复用历史经验防重复误判、区分 `bugx-docs` 与 `lesson-ledger` 职责、排查遥测/隐私拦截。

## 1. 三套知识系统的正交边界（勿混淆）

| 子系统 | 底层载体 | 写入机制与隐私作用域 | 典型查询入口 |
|---|---|---|---|
| **文档检索 (`bugx-docs`)** | `~/.bugx/cache/docs/<ver>/`（官档） + `<ROOT>/.bugx/knowledge/docs.sqlite3`（项目文档） | 只读 Markdown RAG 索引；项目库**绝不上云**，官档可热同步 | MCP `search_docs` / `fetch_doc` / `list_sections` 或 `bugx guide "<关键词>"` |
| **错题经验集 (`lesson-ledger`)** | `.bugx/knowledge/lesson-ledger.sqlite3` + `~/.bugx/knowledge/global.sqlite3` | 18 张强状态关系表（`hit_count`、误报源码防线 `fp_ledger`、V2 双向云同步） | MCP `bugx_knowledge_route` / `bugx_lesson_retrieve` 或 `bugx lesson retrieve|incident-lookup` |
| **冷启动数据集 (`seed_corpus` & `pitfall_packs`)** | `core/seed_corpus/*.txt` & `core/audit_packs/pitfalls/*.json` | 随核心引擎签名静态分发，按项目语言/框架过滤 + 双路 RRF 装配先验 | `engine pre` 与 Worker `worker_prior` 自动注入（高危 API 与误报防线） |

> **工程红线**：经验集支持状态反馈、因果指纹与 V2 游标同步；数据集按编程语言硬隔离。两者均**不迁移合并**进无状态全文文档库 `bugx-docs`，确保各自召回信噪比与安全性。

## 2. 日常错题检索与复盘闭环
```bash
# 按故障/模块查同类历史模式
bugx lesson retrieve --root . --query "SQLite lock concurrency" --json
# 查某报错签名的完整事故上下文
bugx lesson incident-lookup --root . --query "WinError 32" --json
# 查哪款工具在该类问题命中率最高
bugx lesson tool-lookup --root . --query "sast python" --json
# 检查账本健康与知识包状态
bugx lesson validate --root . --deep --json
bugx knowledge status --root . --json
```
* **禁止误用**：当 marker 存在但 SQLite 损坏缺失时，先备份恢复，**禁止直接跑 `rebuild --from-seed --confirm-data-loss`** 抹除历史状态。`promote*` 修改全局账本，不等价于 `cloud sync`。

## 3. 隐私、遥测与敏感到网红线（Hard Gates）
| 控制维度 | 设置项 / 环境变量 | 行为与绝对禁止边界 |
|---|---|---|
| **遥测与数据分享门禁** | `bugx config set telemetry.data_sharing false`<br>`BUGX_TELEMETRY_ENABLED=0`（进程级） | 设 `false` 将**物理清空本地飞轮 outbox** 并**强硬阻断 `cloud sync` push/pull**，不可用 env 绕过。AI 不得擅自传入 `--accept-data-sharing`。 |
| **敏感文本策略** | `bugx config set cloud.sensitive_policy block|redact|allow` | 缺省为 `redact`（安全默认脱敏）；`block` 命中秘钥立即阻断；**原样上云需显式 `allow`**。 |
| **匿名文档拉取隔离** | `BUGX_DOCS_OFFLINE=1` / `cloud.docs_remote_enabled=false` | 启用后 `bugx-docs` 出网请求归零；联网拉取官档仅传递版本号与语种，**严禁外发任何被测项目路径、源码或密钥**。 |
| **报告与日志严禁泄露凭据** | `auth status` / `log` / `export` | 严禁把 API Key、Token、JWT 写入日志、Chat 输出或 GHSA 报告。 |
