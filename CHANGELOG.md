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
- **Windows 跨平台进程启动与文件锁阻断消除**：底层统一支持 Windows 批处理包装（`.cmd` / `.bat` / `.exe`）自动归一化寻址，消除 `CreateProcess` 找不到指定文件的异常；快照制作与符号索引全面排除 `.dart_tool`、`.gradle`、IDE 缓存及内部锁文件，杜绝文件独占锁占用报错。
- **Kimi 与各宿主模型参数全链路透传**：消解无头与自治模式下宿主访问模型参数的属性缺失异常，模型参数 100% 贯通穿透至底层命令行。
- **显式覆盖清单与多语言代码适配**：显式指定审计范围时自动标记显式模式，免除全仓差集漏项误判；补齐 Flutter / Dart 源码在覆盖清单、质量框架与结构化代码分词模式匹配器的全链路识别。
- **发布包运行时边界隔离**：彻底将渗透与故障矩阵核心组件迁入核心引擎库，消除发布包缺少开发脚本导致的模块导入缺失错误。
