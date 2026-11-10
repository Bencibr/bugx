# BugX Dev Harness (开发与测试桩运行夹具)

`core/dev_harness/` 提供了开发者在脱机、无网络或 CI 自动化回归测试场景下所需的测试夹具（Test Fixtures）与预设交互环境（Interaction Presets）。

---

## 1. 模块文件物理索引与职责清单 (File Inventory)

| 文件名 | 职责与功能定位 | 关键类 / 函数 |
| :--- | :--- | :--- |
| `__init__.py` | Python 包初始化与核心模块符号导出 | `__all__` |
| `interaction_preset.py` | 交互式终端与问答环境预设器，支持在无人工参与的自动化测试中模拟输入、选项确认与参数注入 | `InteractionPreset` |
| `INTERFACE.md` | 开发桩接口契约说明文档，规范测试环境的 Mock 注入契约 | 接口定义 |
| `SPEC.md` | 测试桩设计规范，阐述单测无副作用隔离公理与资源清理保证 | 规范说明 |

---

## 2. 核心设计契约与公理 (Design Axioms)

1. **绝对确定性 (Determinism)**：所有预设桩返回的数据必须是完全确定、无随机波动的值，确保测试用例在并发或多机器运行下 100% 可复现。
2. **零环境外溢**：使用 `interaction_preset.py` 进行环境注入时，必须在上下文退出（`__exit__`）后完全还原全局状态。
