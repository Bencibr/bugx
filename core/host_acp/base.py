#!/usr/bin/env python3
"""core/host_acp/base.py - 宿主 ACP 适配插件的接口契约。

设计目标（2026-10-23 收敛）：宿主差异**只能**写在宿主自己的插件模块里。探针
（``scripts/smoke_host_acp.py``）只按名字取插件并调用接口，不准再出现
``if adapter.name == "xxx"`` 这类分支——那是「每加一个宿主就要改多处、还可能漏改」
的根因。新增宿主的完整步骤只有两步：

1. 在 ``core/host_acp/`` 下加一个模块（导出 ``PLUGIN``）；
2. 把它从 ``TODO_HOSTS`` 里删掉（同一个文件的账目）。

约定与失败姿态：

* ``status = "ready"`` 必须真正实现 :meth:`AcpPlugin.acp_argv`，否则**导入期**
  fail-fast（``RuntimeError``）：探活不允许拿「声明支持」的空实现去跑真实宿主；
* ``status = "todo"`` 是合法一等状态：宿主被显式登记为「尚未适配」，探针据此
  直接 ``BLOCKED`` + ``adapter_status=todo``，不使用任何隐式默认值；
* ``status = "unavailable"`` 是**产品事实**而非进度：宿主 CLI 本身没有 ACP 入口
  （如 ``codex-cli 0.163.0``）。它必须与 ``todo`` 可区分——``todo`` 是「本仓欠的
  活」，``unavailable`` 是「上游没有这个协议面，ACP 探活不适用」；混用会让审计把
  「等上游」误读成「本仓拖欠」，也会让用户以为补个适配器就能探；
* 本包只依赖标准库。插件**禁止** import ``scripts/*``（否则与探针形成环），
  需要探针侧的产物（如 MCP 服务器定义）时由调用方以回调注入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

__all__ = ["AcpPlugin", "McpServersBuilder"]

# 探针注入的「构造 BugX stdio MCP 定义」回调：入参是只保留的服务器名集合，
# 返回 ``{name: spec}``。插件不得自己拼服务器定义（那是探针的口径）。
McpServersBuilder = Callable[[Any], dict[str, dict[str, Any]]]


class AcpPlugin:
    """宿主 ACP 能力插件基类（只描述 ACP 探活需要的差异面）。"""

    #: 规范宿主名，必须与 ``core/host_registry.SUPPORTED_HOSTS`` 一致。
    name: str = ""
    #: 宿主可执行名（仅用于报告/诊断，实际拉起以 argv[0] 为准）。
    executable: str = ""
    #: ``ready``（已适配）/ ``todo``（本仓尚未适配）/ ``unavailable``（宿主产品
    #: 无 ACP 入口，探活不适用）。
    status: str = "todo"
    #: 非 ``ready`` 时的可读原因（进报告，便于审计「为什么这个宿主不跑 ACP」）。
    reason: str = ""
    #: 是否支持项目级 permissions 规则授权
    supports_project_permissions: bool = False

    def supports_acp(self) -> bool:
        """Return whether this plugin may be launched for a real ACP probe."""
        return self.status == "ready"

    def headless_probe_argv(self, argv: list[str]) -> list[str]:
        """Return probe-only argv adjustments without changing worker commands."""
        return list(argv)

    def acp_argv(self, *, model: str | None = None, data_dir: Path | str | None = None) -> list[str]:
        """Return the host's ACP entry-point argv.

        ``model`` / ``data_dir`` 只有在宿主入口真的支持对应 flag 时才允许拼入；
        其它宿主即使被显式传值也必须忽略（误传 flag 会让宿主打印 usage 后静默
        退出，形成「零输出的死会话」，比报错更难归因）。
        """
        raise NotImplementedError

    def launch_policy(self) -> dict[str, Any]:
        """Return the auditable launching policy (auto-approve / model flag)."""
        return {
            "auto_approve": False,
            "auto_approve_flag": None,
            "model_flag": None,
            "reason": f"{self.name or 'unknown'} 未声明 ACP 启动策略",
        }

    def prepare_isolation(
        self,
        root: Path,
        keep: set[str] | None,
        *,
        servers: McpServersBuilder,
    ) -> tuple[dict[str, str], Path | None, dict[str, Any]]:
        """Return ``(env_overrides, mcp_settings_path, info)`` for probe isolation.

        默认不隔离：项目级宿主直接使用探活项目自带的配置。需要用户级数据目录
        沙箱的宿主（如 cline）在自己的插件里覆盖本方法。
        """
        del root, keep, servers
        return {}, None, {}

    def ensure_probe_servers(
        self,
        project_root: Path,
        config_paths: tuple[Path, ...],
        servers: dict[str, dict[str, Any]],
    ) -> list[str]:
        """Align ``servers`` in the throwaway probe project; return changed names.

        为什么需要：探活项目的宿主 MCP 配置是从真实项目**拷贝**来的，真实项目可能只
        注册了 BugX 的一路（实测本仓 ``opencode.json`` 只有 worker）。照抄会让探活的
        prompt 要求 host 工具而宿主根本没有该工具，最终把「项目配置少一路」误报成
        「宿主/适配器不行」。探针必须在**一次性项目**里补齐两路，并如实报告补了哪些。

        只允许改写 ``project_root`` 内部的文件；越界路径一律跳过（不得篡改用户全局配置）。
        默认不注入：需要用户级沙箱的宿主（cline）由 :meth:`prepare_isolation` 自己声明服务器。
        """
        del project_root, config_paths, servers
        return []
