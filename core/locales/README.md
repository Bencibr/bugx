# BugX Locales (多语言国际化文案字典)

`core/locales/` 维护了 BugX 命令行、Web 工作台、交互提示以及报告导出的多语言文本资产。

---

## 1. 语言文件清单 (File Inventory)

| 文件名 | 语言代码 | 覆盖范围与说明 |
| :--- | :--- | :--- |
| `zh-CN.json` | 简体中文 (Chinese Simplified) | CLI 交互问答、阶段状态输出、漏洞分类中文对照、Web 工作台标签与报告文案 |
| `en-US.json` | 英语 (English US) | 国际化标准输出、海外 CI/CD 日志对齐、英文漏洞报告模板与 MCP 工具描述文案 |

---

## 2. 维护契约与同步规范

- **Key 键名强对齐**：`en-US.json` 与 `zh-CN.json` 的顶级与嵌套键名必须 100% 保持一一对应，严禁单侧存在悬空缺失 Key。
- **参数占位符一致**：所有动态插值占位符（如 `{path}`, `{count}`, `{duration}`）在不同语言中必须语义和名称相同。
