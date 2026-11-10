# Stage 10: Schema & Migration Inventory (结构与迁移盘点)
<!-- L1:START -->
1. 通过 `dbhub` 只读列举表/视图/索引/约束，建立结构快照。
2. 盘点迁移与模型定义，比对"代码声明"与"库内实际"的漂移。
3. 标注敏感列（凭据、PII、令牌）与其可访问角色。
4. 产出 `schema-inventory` 与 `migration-findings`。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 只读命令：列表、`DESCRIBE`、`SHOW CREATE`、只读 `SELECT`（带 `LIMIT`）。
- 关注漂移：迁移未执行、约束缺失、默认值不一致、索引与查询不匹配。
- 敏感列命中时只记录**列名与类型**，严禁拉取真实数据行。
- 与 `00-scope` 的边界清单比对：越界对象立即剔除并记入 findings。
<!-- L2:END -->
