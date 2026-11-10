# BugX Core Infrastructure & Primitives (核心基础设施与底层操作系统原语子系统)

> **版本契约**: `0.1.0` | **所属分层**: `Layer 0: 基础设施与操作系统抽象`  
> **公开架构说明**: 参见 [`docs/02_architecture/01_ARCHITECTURE.md`](../../docs/02_architecture/01_ARCHITECTURE.md)。

`core/infra/` 构成了 BugX 微内核底层的纯净基础设施与底层操作系统原生能力抽象，为上层分析规划、攻防探针、并发调度与宿主集成提供高可靠、零脆弱性的基础原语支撑。

---

## 1. 基础设施子系统目录拓扑树 (Subsystem Directory Topology)

```text
core/infra/
├── __init__.py              # 统一导出门面
├── build_info.py            # 固化单调自增 Build ID，提供运行时版本与编译元数据
├── client_metadata.py       # 采集宿主硬件架构、操作系统版本与 Node/Python 运行时信息
├── python_runtime.py        # 探测并驱动独立的受控 Python 解释器执行隔离代码
└── safe_xml.py              # 防御 XXE (XML 外部实体注入) 的安全解析适配器
```

---

## 2. 物理条目与核心职责索引 (Physical Module Directory)

| 物理文件 | 架构层级 / 角色 | 核心职责说明 | 关键接口 / 实体 |
| :--- | :--- | :--- | :--- |
| `__init__.py` | 统一导出门面 | 聚合导出核心基础设施与系统原语接口 | `__all__` |
| `build_info.py` | 构建信息固化 | 固化单调自增 Build ID，提供运行时版本、Build ID 与编译元数据查询 | `build_id`, `build_version` |
| `client_metadata.py` | 元数据采集器 | 采集当前宿主机硬件架构、操作系统内核与 Node/Python 运行时环境信息 | `collect_client_metadata` |
| `python_runtime.py` | 隔离解释器 | 探测、校验并驱动独立的受控 Python 解释器环境执行隔离代码 | `transparent_reexec_if_needed`, `resolve_python` |
| `safe_xml.py` | 安全 XML 解析 | 防御 XXE (XML 外部实体注入)、DTD 实体膨胀与超深 XML 递归的安全解析适配器 | `parse_xml`, `parse_xml_file`, `XmlSecurityError` |

---

## 3. 设计契约与防御公理

1. **零外部重依赖**：本子系统所有原语仅依赖 Python 标准库与底层系统调用，杜绝循环引用与第三方污染；
2. **强类型与安全防御**：`safe_xml.py` 默认禁用外部实体解析与 DTD，抵御十亿笑脸攻击与 SSRF 隐式探测；`python_runtime.py` 严格校验 `sys.version_info >= (3, 11)`。
