# BugX 全生命周期状态机、门禁与故障恢复速查卡

> 适用场景：全库深度审计调度、`engine pre` 退出码 `2` 交互、Worker 并发监督、常见阻断（`FATAL_BLOCKED` / `RECOVERY_REQUIRED` / `WinError 32`）与报告生成。

## 1. 全库审计五步状态机
```text
1. bugx engine --root <ROOT> status --json
   ├─ UNINITIALIZED ──────────────────────────▶ 可运行 engine pre 安全建局
   ├─ FATAL_BLOCKED / CORRUPTED / 未定阻断 ───▶ 严禁 init / reset / --force 掩盖，先保留现场与 logs
   └─ READY / ACTIVE ─────────────────────────▶ 恢复或进入下一阶段

2. bugx engine --root <ROOT> pre
   ├─ Exit 2 (BUGX_INTERACTION_REQUEST) ──────▶ 原样调用宿主单选控件（无原生控件走 bugx interaction answer）
   └─ Exit 0 ─────────────────────────────────▶ 仅表示启动门禁通过，尚未完成挖掘

3. bugx engine --root <ROOT> workers → 派发 Worker（claim → worker-start → heartbeat → probe → checkpoint）
4. bugx supervisor status / findings / gate / finish --root <ROOT>
5. bugx engine --root <ROOT> post → bugx report both --root <ROOT> --format both
```

## 2. 非交互环境交互回灌通道（`bugx interaction`）
当 CI / 无原生界面宿主导致 `engine pre` 阻塞等待单选确认时：
```bash
bugx interaction list --root . --json
bugx interaction show <QUESTION_ID> --root . --json
bugx interaction answer <QUESTION_ID> --value <OPTION_VALUE> --root . --json
# 仅取消陈旧问题：bugx interaction cancel <QUESTION_ID> --root . --reason "..." --json
```

## 3. 高频故障与安全自愈速查
| 故障态 / 错误码 | 真实原因 | 唯一授权恢复动作 | 禁止误操作 |
|---|---|---|---|
| `FATAL_BLOCKED` / `identity mismatch` | `.bugx/manifest.json` 绑错项目路径或损坏 | `bugx project rebind --root . --json`（迁移需显式确认）；查看 `bugx log --root . --errors` | 禁止手删 `.bugx/` 或硬加 `--force` / `reset` |
| `WinError 32` / 文件占用 | Windows 下历史 Worker 或编辑器句柄锁定旧文件 | 等待只读句柄释放或关闭残留子进程；新版 `docs.sqlite3` 采用不可变切换 + `.gc-pending` 延迟清理，自动绕过占用崩溃 | 禁止破坏性 `taskkill` 无关用户进程 |
| `MissingAgentAssetError` (exit 2) | 缺失安装资产或旧全局引擎不完整 | 生产态执行 `bugx update --repair-assets`；源码开发显式使用 `BUGX_DEV_MODE=1` | 禁止在生产目录套用开发仓库补丁 |
| `session_occupied` / 租约失效 | 双开 `bugx-supervisor` 或 Worker 超过 heartbeat 窗口 | 复用当前会话已挂载 MCP；确认无活动工作后运行 `bugx supervisor recover --root .` | 禁止直接编辑 `supervisor.json` |
| `MCP_RUNTIME_CORRUPT` | 离线 runtime 安装半成品或锁哈希校验不通过 | `bugx mcp-runtime verify --json` 后执行 `bugx mcp-runtime rollback --json` 再重装 | 禁止跳过哈希锁强制运行 |

## 4. 终态判定与双轨报告交付
* 唯一合法审计完成标志：`COMPLETE`（覆盖清单全部 `covered` 且 `open_findings` 清空），绝不能仅靠模型总结文字或单个脚本 exit 0 宣布完成。
* 报告交付：
  ```bash
  bugx report both --root . --out .bugx/reports --format both   # formal(商务正式) + visual(可视化)
  bugx report serve --root . --port 8080                        # 启动 BugX Studio 本地预览台
  bugx advisory export --cve CVE-2026-9001 --format markdown    # 生成本地 GHSA 草案（不主动发布）
  ```
