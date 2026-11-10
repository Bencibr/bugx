# BugX MCP Runtime Support (MCP 运行时支撑工具库)

`core/mcp_runtime_support/` 提供了 MCP 服务端与客户端通信过程中的低层环境检测、跨平台进程包装与参数合法性校验辅助逻辑。

---

## 1. 模块文件物理索引与职责清单 (File Inventory)

| 文件名 | 职责与功能定位 | 关键类 / 函数 |
| :--- | :--- | :--- |
| `__init__.py` | Python 包初始化与核心模块符号导出 | `__all__` |
| `common.py` | MCP 运行时底层共享工具函数，包含 JSON 解析容错、标准输入输出流读写安全封装 | `safe_json_loads`, `write_mcp_message` |
| `environment.py` | 运行宿主环境感知器，负责解析环境变量、检测 Node.js / Python 运行时版本与路径 | `detect_mcp_environment` |
| `verifier.py` | MCP 工具调用参数反向校验器，确保传入的路径在允许的工作树白名单边界内 | `verify_tool_argument_boundaries` |

---

## 2. 核心设计契约与公理 (Design Axioms)

1. **路径越界防御 (Path Traversal Defense)**：`verifier.py` 对所有 MCP 工具接收的文件路径参数做强制规范化与工作区根目录沙箱边界检查，严禁访问工作区之外的敏感文件。
2. **健壮的流式序列化**：`common.py` 在向标准输出写出 MCP 响应帧时必须强制 Flush，杜绝由于缓冲区残留导致的宿主死锁等待。
