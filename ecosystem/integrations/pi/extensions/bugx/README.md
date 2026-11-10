# BugX Pi Extension

这是 BugX 的 Pi 宿主兼容层，由 `bugx install --agent pi` 自动安装。

它不实现审计逻辑，只负责：

- 提供 BugX 状态、轮询、暂停、恢复和停止命令；
- 调用用户级全局引擎（默认 `~/.bugx/core`，可由 `BUGX_GLOBAL_ENGINE_ROOT` 指定）中的共享 Python 运行时；
- 对受保护的协议脚本和状态文件提供 Pi 侧的防误写拦截；
- 将 Pi 的宿主状态显示在界面中。

项目目录必须先被 Pi 信任。扩展只提供宿主兼容层，不替代宿主的授权和执行策略。
