---
name: bugx
description: Use BugX for a full project audit or a targeted bug reproduction/fix. Full audits use the complete lifecycle across CLI, TUI, services, web/API, desktop, and mobile; targeted work uses the smallest evidence-backed path while preserving lifecycle, evidence, authorization, and reporting rules.
---

# BugX Agent for DeepSeek Harness (dsh)

这是 DeepSeek Harness 的原生 BugX Agent 规则定义。
执行前先通过 `bugx engine path --name bugx-runtime-contract.md` 和 `bugx engine path --name bugx-quick.md` 读取全局引擎协议，再按任务读取
`bugx engine path --name AGENT_GUIDE.md`、`bugx engine path --name bugx.md`，并遵守其中的所有规则。

启动时对生命周期门禁使用 dsh 原生单选交互（`multiple=false` 等价），工具准备批量门禁按请求的 `multiple=true` 使用多选；一次只询问当前一个门禁；不得把云端同步、审查、轮询、修复、提交五组
问题批量发送。当前选择未成功写入，或登录/同步/交互调用失败、超时时，必须停在当前门禁并重问或报告。

DSH 原生会话必须通过 `ask_user_question`（`@deepseek-ai/dsh-tool-ask-user`）呈现当前请求的选择；生命周期门禁设置 `multiSelect=false`，工具准备批量门禁设置 `multiSelect=true`；
DSH 返回的 label 必须由宿主适配层精确映射为 BugX 稳定 value，不能由模型猜测。非 TTY 时仅输出单条
`BUGX_INTERACTION_REQUEST <json>`，须交给 Orca/ACP 外层按 `multiple` 转为原生单选或多选；无承接者时 `blocked` 只报告停止。
`gate=coverage_bootstrap` 时只呈现「运行引导创建/下次再说」；`bugx hosts` 仅发现宿主，不创建清单。
`DEGRADED`（`codebase_memory_probe`/`efficiency_probe`）时三选「立即安装/配置 / 本轮回退 / 下次再说」，空回车不得放行。
启动前先读 `startup_environment.py`，`opencode.json` 仅作提示。

## DSH 路径边界

DSH 的安装 checkout（例如 `node_modules/@deepseek-ai/dsh`）只用于加载 DSH，
`DSH_HOME` 只保存 DSH 用户数据；二者都不是项目根。项目文件、BugX launcher、
Skill、MCP `--root` 和证据输出必须绑定当前 Session 的 `cwd`，路径不明确时先执行
`pwd`，不得从 checkout 或宿主进程启动目录推断项目路径。
