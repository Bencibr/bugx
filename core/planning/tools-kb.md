# 工具知识库

这是公开 BugX 的通用工具目录。它只记录适用于源码、API、CLI、TUI、Web UI、
桌面/移动应用和运行时质量验证的工具。目标专用工具应在对应扩展中登记。

## 30 天有效目录

| 工具 | 用途 | 来源 | 验证版本 | 验证日期 |
|------|------|------|----------|----------|
| ripgrep | 文本、错误消息和配置值搜索 | https://github.com/BurntSushi/ripgrep | 15.2.0 | 2026-10-16 |
| ast-grep | AST 结构搜索与局部模式核对 | https://github.com/ast-grep/ast-grep | 0.45.3 | 2026-10-16 |
| jq | JSON/SARIF 输出筛选 | https://github.com/jqlang/jq | 1.8.2 | 2026-10-16 |
| Playwright | Web UI 快照、交互、网络和截图证据 | https://playwright.dev | 0.0.79（仓库锁定） | 2026-09-23 |
| postmcp | API 契约、状态码和响应断言 | https://github.com/Bencibr/postmcp | 1.0.3（仓库锁定） | 2026-09-23 |
| Schemathesis | OpenAPI 属性与有状态 API 测试 | https://github.com/schemathesis/schemathesis | 4.27.1 | 2026-10-16 |
| RESTler | OpenAPI 驱动的 REST 状态序列测试 | https://github.com/microsoft/restler-fuzzer | 9.3.1 | 2026-10-16 |
| k6 | 授权测试环境的限速并发和幂等验证 | https://github.com/grafana/k6 | 2.2.0 | 2026-10-16 |
| Vegeta | 有上限的 HTTP 速率验证 | https://github.com/tsenart/vegeta | 12.13.0 | 2026-10-16 |
| codebase-memory-mcp | 持久化符号、调用和架构关系图 | https://github.com/DeusData/codebase-memory-mcp | 固定规格见 `core/runtime/tool_install_targets.py` | 2026-10-30 |
| Semgrep | 规则驱动 SAST：已知漏洞模式扫描（SQLi/XSS/SSRF/RCE） | https://github.com/semgrep/semgrep | 1.80.0 | 2026-09-27 |
| CodeQL | 语义级污点分析与变体分析（Source→Sink 数据流） | https://github.com/github/codeql | 2.18.0 | 2026-09-27 |
| Trivy | 依赖包漏洞（SCA）与容器配置扫描 | https://github.com/aquasecurity/trivy | 0.54.0 | 2026-09-27 |
| Bandit | Python 轻量 SAST（安全反模式检测） | https://github.com/PyCQA/bandit | 1.8.6 | 2026-09-27 |
| BugX Web Conformance | 内置 HTML/ARIA 静态基线与浏览器动态合规清单 | 本地 `web_conformance.py` | 0.6.46 | 2026-09-27 |
| Maestro | 移动端声明式 E2E：YAML 弹窗/滚动/长文本遍历（Tier-2） | https://github.com/mobile-dev-inc/maestro | 1.40.0 | 2026-09-27 |
| Appium | 跨平台设备驱动 REST：`/source`、`/element/{id}/click` 透传（Tier-3） | https://github.com/appium/appium | 2.12.0 | 2026-09-27 |
| uiautomator2 | Android 免权限 UI 树快速提取（Tier-1 备选，**不在 mobile_probe.py 探测范围**；需用时手动 `pip install` 并记录版本） | https://github.com/openatx/uiautomator2 | 待验证 | 2026-09-27 |
| mitmproxy | 移动端 API 流量抓包与并发 Fuzzing（网络轨，**不在 mobile_probe.py 探测范围**；需用时手动安装并记录版本） | https://github.com/mitmproxy/mitmproxy | 待验证 | 2026-09-27 |
## 使用规则

- 先查本地目录；验证日期超过 30 天必须重新核对来源和版本。
- 工具不存在、版本不兼容或输出解析失败时标记 `DEGRADED/BLOCKED`，不能伪造 PASS。
- 必备 MCP（Playwright、postmcp）使用仓库锁定版本；codebase-memory-mcp 的安装与探针只读取
  `core/runtime/tool_install_targets.py` 中的固定规格，不得另设版本来源。启动记录仍保存实际解析到的版本，便于复盘。
- 只选当前目标需要的工具，不为“工具齐全”自动安装全部依赖。
- 主动请求、生产写入和外部服务访问必须由目标所有者明确授权，并记录白名单、速率、
  并发、总量、时长和停止条件。
- 私有专项插件有自己的工具知识库和准备门禁；核心 Agent 不跨边界复用其结论。
