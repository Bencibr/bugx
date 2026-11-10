# Stage 80: Deterministic Evidence & Regression (确定性红绿重放验证)
<!-- L1:START -->
1. 聚合前置所有阶段产出的漏洞证据与 PoC。
2. 在隔离沙箱中执行红灯重放：确认漏洞在修复前 100% 触发并复现。
3. 生成确定性验证脚本与负向拦截对照基线。
4. 输出最终 `verification-report.json`，准出退出码必须为 0。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 所有的 PoC 必须满足 Zero-LLM Replay 规范，即任何工程师单凭脚本即可脱机一键复现。
- 包含清晰的 CVSS 评分、漏洞危害、影响组件及精准修复代码建议。
<!-- L2:END -->
