# Stage 20: Query & Integrity (查询与完整性验证)
<!-- L1:START -->
1. 只读执行查询计划（`EXPLAIN`）识别全表扫描、缺失索引与 N+1 模式。
2. 验证事务边界、并发写与幂等性假设是否成立。
3. 生成最小可复现证据（查询 + 计划 + 观测），不含真实业务数据。
4. 产出 `query-plan-evidence` 与 `integrity-findings`。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 只允许 `EXPLAIN`/`EXPLAIN ANALYZE`（在安全前提下）与只读聚合查询。
- N+1 判定必须给出"调用点 + 查询次数 + 放大倍率"，不得只凭 ORM 写法推断。
- 事务/并发问题必须给出可复现步骤与观测差异，禁止仅凭代码阅读下结论。
- 所有证据写入 finding 的 `reproduction` 字段，并脱敏（不得包含凭据或真实数据）。
<!-- L2:END -->
