# Stage 00: Scope & Boundaries (侦察与边界界定)
<!-- L1:START -->
1. 界定测试边界：解析 URL 白名单、排除黑名单与静态资源路径。
2. 检测服务存活状态、WAF 规则与防护拦截行为。
3. 确定速率限制阈值，初始化测试环境配置。
4. 产出并校验 `scope-manifest.json`，触达 100% 范围准出硬门禁。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 仔细阅读 target 与 scope 定义，严禁对白名单外的外部主机或生产基础设施发起任何破坏性探测。
- 如果检测到 Cloudflare / AWS WAF 等防护盾，动态调低并发线程并开启反指纹探测。
- 最终产出合规的 `scope-manifest.json` 供后续阶段消费。
<!-- L2:END -->
