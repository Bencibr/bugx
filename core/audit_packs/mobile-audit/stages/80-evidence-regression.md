# Stage 80: Evidence Regression（证据回归与结论收敛）
<!-- L1:START -->
1. 每条 Finding 必须绑定可复现证据：文件位置 + 触发命令/输入 + 实际结果。
2. 区分三态：`CONFIRMED`（已复现）、`SUSPECTED`（静态可读但未复现）、`UNVERIFIED`（能力缺失无法验证）。
3. 重跑静态规则确认结论稳定（同一规则、同一引擎版本）。
4. 产出 `verification-report.json`，附覆盖率与缺口清单。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 引擎版本必须记录 `pySemgrep` 版本（不是 `semgrep` shim 的输出）。
- `UNVERIFIED` 不得被汇总成"未发现漏洞"；必须单列在报告缺口区。
- 删除无法复现且无静态依据的条目，不保留"以后可能有用"的猜测。
<!-- L2:END -->
