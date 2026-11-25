# BugX 云端鉴权、同步与文档实时更新速查卡

> 适用场景：云端登录阻塞超时、非阻塞设备码授权、多scope同步失败、本地文档库同步与离线隔离。

## 1. 云端极速鉴权（推荐两种无阻塞方式）

### 方式一：API Key 瞬时鉴权（0.1s 零交互，适合 CI / Agent / 无头环境）
```bash
bugx auth set-key --api-key "<KEY>"
# 或环境变量注入：export BUGX_CLOUD_API_KEY="<KEY>"
bugx auth status --root . --verify --json   # 确认 auth_verified=true
```

### 方式二：非阻塞 Device Code 登录（彻底告别 600s 轮询卡死）
Agent 通过工具执行云端登录时，**必须加 `--non-blocking`**（或 CLI 检测到非 TTY 时自动非阻塞）：
```bash
# 1. 立即获取 verification_uri 与 user_code（< 500ms 返回，pending 状态落盘 .bugx/cloud-auth-pending.json）
bugx auth login --root . --non-blocking --json

# 2. 展示授权链接和用户码给用户点击确认后，执行单次核验保存凭据：
bugx auth verify --device-code "<DEVICE_CODE>" --root .
# 若不传 --device-code，自动读取最近未过期的 cloud-auth-pending.json

# 3. 核验登录与路由状态：
bugx auth status --root . --verify --json
```

## 2. 标准云同步流程与作用域
* **检查状态**：`bugx cloud status --root . --scope all --json`（纯本地只读，不联网）
* **全量授权同步**（仅当用户授权项目登记 + 项目知识 + 全局知识时）：
  ```bash
  bugx cloud projects add --root .
  bugx cloud sync --root . --scope all
  ```
* **显式本地离线模式**：`bugx cloud onboard off --root .` 或 `bugx init --root . --cloud-sync local`

## 3. `bugx-docs` 实时同步与防污染策略
* **手动强制更新官方文档缓存**：`bugx mcp docs sync --root . --force --json`
* **缓存目录**：官方文档按 `contract_version`（如 `0.1.3`）落盘 `~/.bugx/cache/docs/<version>/<sha256>.sqlite3`，只换不改、切完物理删除旧库；项目私有文档严格留在 `<ROOT>/.bugx/knowledge/docs.sqlite3`，**绝不上云**。
* **严格离线/空气隔离**：设置 `export BUGX_DOCS_OFFLINE=1` 或传 `--offline`，将仅使用随包离线基线，零出网连接。

## 4. 高频报错对照处置
| 症状/错误 | 根因 | 标准处置 |
|---|---|---|
| `SYNC_UNAUTHENTICATED` / `401` | 无有效 Bearer Token 或已过期 | 运行 `bugx auth login --root . --non-blocking` 重新授权后 `auth verify` |
| `429` Provider Rate Limit | 宿主模型或网关限流（`.bugx/state/provider-window.json`） | 等待窗口结束再重试，禁止删除限制文件绕过 |
| Darwin 私网 `Errno 65/51` | macOS 本地网络权限 (TCC) 拦截 Python 直连 | 查看 `bugx doctor --root .` 的 `/usr/bin/curl` 影子探活结果，授予定期权限 |
| `ack_pending=true` | 本地导入已提交但远端 ACK 未完成 | 保留本地 ledger 与 cursor，排查网络认证后重新执行原 scope 的 `cloud sync` |
