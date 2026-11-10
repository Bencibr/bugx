# BugX 项目运行目录说明

本文件由 `bugx init` 或 `bugx install` 复制为项目根目录的 `.bugx/README.md`。
它只说明当前项目的运行数据，不是 AI 执行协议；AI 执行规则请通过 `bugx engine path --name bugx.md` 读取全局引擎协议。

```text
.bugx/
├── README.md                 # 本目录说明，给人阅读
├── logs/                     # CLI/宿主诊断日志
│   ├── bugx.jsonl            # 结构化 JSONL，默认 0600，自动轮转
│   └── bugx.jsonl.1..3       # 历史日志（存在时）
├── pollings/                 # Session 运行容器（内部物理目录名）
│   └── polling-000001/
│       ├── manifest.json     # 轮询身份和授权策略
│       ├── state.json        # 轮询状态和停止原因
│       └── lifecycles/
│           └── lifecycle-000001/
│               ├── manifest.json
│               ├── state/    # life、runtime、Supervisor、覆盖和锁
│               ├── rounds/   # Life 检查点及 Worker 结果
│               ├── events.jsonl
│               ├── report.md
│               ├── conclusion.json
│               └── integrity.json
├── knowledge/                # 项目级 SQLite 错题集和初始化快照
├── backups/                  # 配置、宿主入口和卸载前的可恢复备份
├── artifacts/                # 大型证据文件；按内容哈希归档
├── index/                    # 查询索引，不是事实源
├── manifest.json             # 项目身份和布局版本
├── current.json              # 当前 polling/lifecycle 指针
└── hosts.json                # 已安装宿主清单
```

边界规则：

- 项目 `.bugx/` 不包含可执行引擎；引擎单点升级，项目内不放实体协议文件。已初始化项目里，日志、生命值、报告和错题集只能写入项目状态路径。
- `logs/bugx.jsonl` 只用于排查 CLI、安装和运行异常；Finding 过程事件仍写入对应生命周期的 `events.jsonl`。
- **未初始化目录例外**：`.bugx/logs/` 由项目布局初始化器创建。尚无 `.bugx/manifest.json` 的目录里，诊断日志回退到用户级 `~/.bugx/logs/projects/<project_id>/bugx.jsonl`（`BUGX_GLOBAL_LOG_ROOT` 可覆盖），而不是创建 `<root>/.bugx`。否则任意目录跑一次只读命令（`doctor`/`hosts`/`log`）就会留下"有 `.bugx` 但没有 manifest"的半初始化状态，并让该目录的 `git status` 变脏——BugX 不管理 `.gitignore`。
- 回退日志与项目日志同级私有权限（目录 `0700`、文件 `0600`），同样拒绝符号链接路径。
- `report.md`、`conclusion.json` 和 `integrity.json` 是终局产物，不能用普通日志替代。
- 日志默认脱敏 Token、密码、Cookie、API Key 等敏感值；发现疑似敏感内容时仍应人工复核。
- `.bugx/` 默认不提交 Git；需要共享报告时由用户显式选择并先检查敏感数据。
