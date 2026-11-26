# Changelog

All notable changes to the **BugX** project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note for Users**: For granular internal commit notes from previous alpha/patch development cycles, see the internal development records in `docs/08_internal/`.

## [0.1.6] - 2026-11-26

### Added / 功能
- **GitHub Copilot 官方一级受支持宿主与端到端装配**：新增对 GitHub Copilot 的全面原生支持（`bugx install --agent copilot`，别名 `github-copilot`、`gh-copilot`、`copilot-chat`）。自动生成并管理规范的 Agent 入口与指令（项目级 `.github/agents/bugx.agent.md` + `.github/copilot-instructions.md`，用户级 `~/.copilot/agents/bugx.agent.md`），自动写入与镜像项目级 MCP 配置（`.vscode/mcp.json` 与 `.mcp.json`），提供 `bugx doctor --agent copilot` 自动诊断与自愈。
- **WorkBuddy / CodeBuddy 原生宿主深度接入**：在各平台文档矩阵与 CLI 安装体系中完整对齐 WorkBuddy / CodeBuddy 支持，提供原生 Skill、桌面端多变体镜像及项目级 MCP 装配。

### Changed / 优化
- **宿主矩阵与分发文档同步**：同步更新 README、README_EN、OVERVIEW、NPM/PyPI 分发文档中的支持宿主对照表，全方位覆盖 GitHub Copilot、WorkBuddy、Qoder、Trae、Cline 等现代 AI 编程助手。
- **双语国际化文案对齐**：补齐 Copilot 中英文交互指令与引导头部模板，确保多语言环境下宿主入口平滑加载。

### Fixed / 修复
- **用户级 Agent 注册事务回执契约修复**：修复用户级 Agent 安装过程中 `ensure_user_agents` 回执缺失 `"written"` 字段导致的 `KeyError` 异常，完善跨宿主用户级资源部署事务原子性与备份回滚能力。
