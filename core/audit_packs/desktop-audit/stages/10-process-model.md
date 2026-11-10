# Stage 10: Process Model & Privilege Boundary (白盒：进程模型与权限边界)
<!-- L1:START -->
1. 梳理主进程 / 渲染进程 / 工具进程的边界与 IPC 通道。
2. 核对渲染进程隔离配置（`contextIsolation`、`nodeIntegration`、`sandbox`、CSP）。
3. 检查提权路径：安装器、自更新、后台服务、开机自启是否以高权限运行。
4. 产出 `process-boundary-findings`。
<!-- L1:END -->
<!-- L2:START -->
## 操作指南
- 以**配置与代码**为证据：逐项核对安全开关的实际取值，不凭框架默认值推断。
- IPC 面必须枚举"通道名 + 入参校验 + 可达调用方"；缺失校验即为候选缺陷。
- 提权路径必须给出"谁能触发 + 触发条件 + 影响面"。
- 与 Stage 30 交叉印证：配置正确但行为异常时以行为证据为准。
<!-- L2:END -->
