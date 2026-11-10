# Stage 40: Input Injection & Boundary Exploits (输入注入与深层利用)
<!-- L1:START -->
1. 探测强类型与过滤边界：SQL/NoSQL 注入、命令执行、原型污染、XSS。
2. 发送边界 Payload 并监控延迟、错误堆栈与行为差异。
3. 对疑似漏洞构造无害验证 PoC (如 sleep(2), id, calc)。
4. 产出沙箱可复现 PoC 脚本与服务端异常响应证据。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 严禁使用破坏性 payload (如 DROP TABLE, rm -rf)。
- 使用时间盲注或基于错误的回显作为核心实锤证据。
- 对每个可疑注入点，记录完整的 Request/Response 原始 HTTP 流。
<!-- L2:END -->
