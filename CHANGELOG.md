# Changelog

All notable changes to the **BugX** project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note for Users**: For granular internal commit notes from previous alpha/patch development cycles, see the internal development records in `docs/08_internal/`.

## [0.1.3] - 2026-11-24

### Added / 功能
- **发版前 GitHub Issue 闭环强门禁**：新增 `scripts/check_release_issues.py`，发版前自动扫描 GitHub open issues 并核验修复状态；对未修复或未在 GitHub 关单的缺陷实行 Fail-Closed 阻断（退出码 1），杜绝带病发版。
- **GitHub Releases 自动化创建规程**：规范化 `gh release create v<X.Y.Z>` 发布 SOP，强制全量挂载 Python Wheel 与 NPM TGZ 双包分发资产并绑定权威 Release Notes。
- **项目级 MCP 配置变更后强制重启宿主门禁**：在 `bugx init`、`bugx doctor --repair-assets`、`bugx install --agent` 配置完成项目 MCP 后增加醒目的强制重启提示（CLI 警告块、pre 门禁检查、WebUI 弹窗引导），解决宿主旧 MCP 列表无法感知 BugX 的问题。

### Changed / 优化
- **版本号规范与正式版免 Build ID 机制**：全局基线版本升级至 `0.1.3`（33 处声明一致）；发新版本时 Build ID 重置为 `1`；正式版（Release / Stable 通道）`bugx --version` 仅输出纯语义版本（如 `bugx 0.1.3`），不携带 `+build.N`，仅 dev 模式下展示构建号。
- **全平台中英文产品描述母语级对齐**：统一全生态各宿主插件（Kimi Code、MiniMax Code、OpenCode、DSH、README、manifest 等）产品标题为 `BugX`，母语级规范化中英文描述。
- **UI 视觉挖掘 Node 版本要求放宽**：将 `core/runtime/setup_ui_env.py` 中的 `NODE_MIN_MAJOR` 放宽至 20（兼容 Node 20.x、22.x LTS 及 Node 24+ 等现代环境），彻底废除 `NODE_MAX_MAJOR` 人为上限拦截。

### Fixed / 修复
- **Windows DSH Desktop 预设插件安装修复 (Issue #1)**：增加 Windows 安装路径候选与递归 asar 解析；修复 `_dsh_directory_exists` 对 Windows junction/断链软链的解析，解决写入失效复数包名 `@deepseek-ai/dsh-agent-presets` 的缺陷，已回归验证并关闭 GitHub Issue #1。
- **CLI 终端编码与 Windows GBK 管道崩溃消除**：在 npm wrapper (`bugx.js`) 默认注入 `PYTHONUTF8: '1'` 与 `PYTHONIOENCODING: 'utf-8'`；在 Python CLI、Supervisor 入口重置 stdout/stderr 为 UTF-8 + replace 容错，彻底消除非 UTF-8 管道与 Windows GBK 环境下的 `UnicodeEncodeError` 崩溃。
- **资产自愈门禁与 cloud-onboard.json 自动恢复**：在 `bugx doctor --repair-assets` 增加 `.bugx/cloud-onboard.json` 自动补齐与就地自愈能力。
