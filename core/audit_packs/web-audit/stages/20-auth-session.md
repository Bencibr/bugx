# Stage 20: Authentication & Session Matrix (认证与凭证矩阵)
<!-- L1:START -->
1. 建立身份矩阵：至少获取 2 个不同租户/角色的有效测试凭证 (User A / User B)。
2. 提取并验证鉴权传递机制：Bearer Token, Session Cookie, API Key, CSRF Token。
3. 测试 Token 生命周期：失效、刷新、跨域传递与重放行为。
4. 将角色凭证与有效上下文登记到 Attack Graph `roles` 节点。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 必须确保测试拥有充足的角色上下文以支持后续越权推演。
- 针对 JWT 检查其签名算法、有效期 (exp)、关键 claim (sub, tenant_id, role)。
- 记录认证相关的拦截响应格式（如 401 Unauthorized vs 403 Forbidden）。
<!-- L2:END -->
