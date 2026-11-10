# Stage 10: Attack Surface Discovery (资产与端点发现)
<!-- L1:START -->
1. 逆向 API 路由：解析 OpenAPI/Swagger/Postman 或从代码 AST 抽取路由。
2. 提取入参模式：路径参数、查询参数、请求体字段及强类型定义。
3. 流式向 Living Attack Graph 写入 `routes` 与 `parameters` 节点。
4. 识别特殊协议与能力端点，按需触发条件扩展模块 (GraphQL/WebSocket/Upload)。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 遍历代码库中的 Controller / Router 定义，抽取 HTTP 方法与路由模式。
- 注意识别隐藏端点、内部管理接口（如 `/internal/`, `/admin/`, `/actuator/`）。
- 将发现的端点与参数转换为 Attack Graph 标准节点格式实时追加。
<!-- L2:END -->
