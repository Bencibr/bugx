# 配置目录说明

`config/config.yaml` 是后端服务的示例部署模板，不是 BugX CLI 的运行配置。
当前 CLI 使用项目根目录的 `opencode.json`；云端客户端使用
`BUGX_CLOUD_CONFIG` 指定的文件，未指定时使用 `~/.bugx/cloud-config.json`。

`config.yaml` 中的 `${BUGX_...}` 值必须由部署系统或 Secret Manager 注入并渲染后才能
供后端使用。本模板不提供环境变量自动展开，也不应直接用于生产。生产部署至少应满足：

- `app.debug` 为 `false`；
- 服务、数据库和 Redis 的 TLS 配置显式启用并完成证书校验；
- `BUGX_JWT_SECRET`、数据库/Redis/邮件凭据来自密钥管理系统，不写入仓库；
- 未渲染的占位符不会被当作真实配置启动。
