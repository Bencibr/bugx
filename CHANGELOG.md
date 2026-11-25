# Changelog

All notable changes to the **BugX** project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note for Users**: For granular internal commit notes from previous alpha/patch development cycles, see the internal development records in `docs/08_internal/`.

## [0.1.4] - 2026-11-25

### Added / 功能
- **文档服务远程优先与无感本地兜底 (Remote-First with Fallback)**：`bugx-docs` 全面重构为远程优先体系，默认直连官方端点，网络异常或离线模式毫秒级无感降级到本地缓存。
- **问题与异常上报命令**：新增 `bugx issue report` 与 `feedback` 命令，支持本地安全脱敏打包与一键反馈。
- **Antigravity 宿主原生支持**：支持 Antigravity 宿主运行时与 ACP 协议，提供插件一键安装管理。
- **随包离线官方文档**：随包内置离线官方文档与场景速查手册，离线环境下支持快速检索。

### Changed / 优化
- **全平台分支协同**：统一各分支代码基线，实现多端代码无缝对齐。
- **CLI 帮助快照同步**：Argparse 命令注册表与终端帮助快照实时同步，提升命令响应速度。

### Fixed / 修复
- **评测管道死锁消除**：重定向标准输入流，消除后台评测与自动化管道中的进程挂起问题。
- **Agent 显示标识规范**：统一 Antigravity 宿主内的智能体显示名称为 BugX。
