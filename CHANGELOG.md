# Changelog

All notable changes to the **BugX** project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note for Users**: For granular internal commit notes from previous alpha/patch development cycles, see the internal development records in `docs/08_internal/`.

## [0.1.5] - 2026-11-26

### Added / 功能
- **环境解释器自适应复用**：深度适配 Windows、macOS 与 Linux 本机已安装的 Python 环境（包括当前运行解释器、系统 PATH 以及系统标准安装目录），只要满足兼容要求即优先复用本机环境，杜绝无端触发外部工具下载多余 Python 运行时。

### Changed / 优化
- **Python 版本上限约束放宽**：废除 MCP Runtime 依赖中人为设置的 `<3.14` 版本阻断限制，全面放宽至 `>=3.12`，无缝兼容 Python 3.12、3.13 及更高版本环境。
- **门面与分发包 README 深度重构与四位一体强同步**：确立并实施十段式高信噪比标准架构，剔除冗长小作文与底层特化长文（篇幅精简 55%+），并强制对齐主门面、英文门面、PyPI 及 npm 分发包说明；在发布 Skill 中固化强同步门禁。
- **SEO/GEO 搜索关键词全面增补**：在中英文门面及分发包说明中对齐覆盖 Google Antigravity、Gemini 4、Claude 5.5、Cursor、Trae、Vibe Coding 安全等前沿生态热词。

### Fixed / 修复
- **Windows Console Script 入口与 PE 二进制解析修复**：消解 Windows 虚拟环境下因可执行文件后缀缺失导致找不到 `Scripts/redis-mcp-server.exe` 的阻断缺陷；免除 Windows PE 可执行二进制（`.exe` / `MZ` 头部）与 Shell 脚本的 Unix 文本 shebang 门禁校验。
