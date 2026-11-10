#!/usr/bin/env python3
"""core/host_acp/cline.py - Cline 的 ACP 适配插件（含用户级数据目录隔离）。

实测与复核状态（版本兼容以**运行时探测**为准，不靠硬编码版本号）：

* （2026-10-22，cline CLI **3.0.70** 实测）ACP 服务端构造函数是
  ``defaultAutoApproveTools = autoApproveTools ?? false``，未显式下发时每个工具
  都会走 ``session/request_permission``；探针按契约拒绝授权 ⇒ 工具永不执行、永远
  拿不到在带证据。因此必须用宿主**官方** flag ``--auto-approve true``（CLI 侧解析
  为 ``autoApproveOverride=true``），且只在一次性 0700 探活项目内生效，不触碰用户
  全局设置。
* （2026-10-22，3.0.70 实测）用户级 MCP 配置只能用 ``CLINE_DATA_DIR`` 沙箱隔离：
  对照实验（只差一个环境变量）显示 ``CLINE_DATA_DIR=<沙箱>`` 时假 MCP server 在首个
  prompt 被 spawn，而 ``CLINE_MCP_SETTINGS_PATH`` **从未**生效（死开关），项目级
  ``<cwd>/.cline/mcp.json`` 同样从未被读取。**该对照实验未在 3.0.73 重跑**
  （标记为未复核事实）；当前实现同时下发 ``--data-dir`` 与 ``CLINE_DATA_DIR``，
  任一对新版本生效即可，且两者都不依赖 ``CLINE_MCP_SETTINGS_PATH``。
* （2026-10-23，3.0.73 复核）``cline --help`` 仍声明 ``--acp``、``--auto-approve``、
  ``-m/--model``、``--data-dir``；插件因此按 ``--help`` 探测决定是否追加
  ``--data-dir``/``-m``，探测失败（无二进制或旧版无该 flag）时自动降级到环境变量
  通道，避免「宿主升级/降级后 argv 直接不可用」。
* MCP 是**首个 prompt 才惰性拉起**：``session/new`` 之后没有任何 MCP 子进程属正常，
  不能据此判定配置无效。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import core.hosts as host_plugins

from .base import AcpPlugin, McpServersBuilder

__all__ = ["ClineAcpPlugin", "CLINE_SANDBOX_SYNC_FILES", "PLUGIN"]

#: 宿主名由模块名派生（``core/host_acp/<host>.py``），实现体不写字面量。
_HOST_NAME = __name__.rsplit(".", 1)[-1]
_PLUGIN = host_plugins.get(_HOST_NAME)
if _PLUGIN is None:  # pragma: no cover - 插件被删除即导入期 fail-fast
    raise RuntimeError(f"{__name__} 需要 core/hosts/{_HOST_NAME}.py 声明；插件缺失")

# 沙箱数据目录需要同步的认证/模型文件。**唯一声明点**是
# ``core/hosts/cline.py`` 的 ``worker_runtime.sandbox_sync``；本模块只读插件，
# 生产 Worker 沙箱（``core/host_runtime/cline.py``）读的是同一字段。
CLINE_SANDBOX_SYNC_FILES: tuple[str, ...] = tuple(_PLUGIN.worker_runtime.sandbox_sync)
#: 沙箱内 MCP 配置文件名（插件全局作用域路径的末段）。
_MCP_SETTINGS_NAME = Path(_PLUGIN.mcp_write.global_scope or "").name or "cline_mcp_settings.json"


@lru_cache(maxsize=8)
def probe_host_flags(executable: str) -> frozenset[str]:
    """``<executable> --help`` 里**真实存在**的 ACP 相关 flag（缓存）。

    这是「宿主版本漂移」的唯一防线：不写死版本号、不假设 flag 集合。任何探测
    失败（无二进制、非零退出、超时、空输出）都返回空集——调用方据此只下发
    基线 flag，并依赖 ``CLINE_DATA_DIR`` 环境变量通道完成隔离。
    """
    try:
        result = subprocess.run(
            [executable, "--help"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    text = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or not text.strip():
        return frozenset()
    return frozenset(
        flag for flag in ("--acp", "--auto-approve", "--data-dir", "-m", "--model")
        if flag in text
    )


def cline_data_dir_source() -> Path:
    """宿主真实 data 目录（认证文件来源；``<HOST>_DATA_DIR`` 已被用户设置时跟随）。

    未设置环境变量时回落到 ``~/.<install.marker>/data``——目录名来自插件
    ``install.marker``，实现体不再写宿主目录字面量。
    """
    configured = os.environ.get(f"{_PLUGIN.name.upper()}_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    marker = _PLUGIN.install.marker or _PLUGIN.identity.markers[0]
    return Path.home() / marker / "data"


class ClineAcpPlugin(AcpPlugin):
    """Cline ACP 插件：官方 auto-approve + ``CLINE_DATA_DIR`` 沙箱。"""

    name = _PLUGIN.name
    executable = _PLUGIN.identity.executable
    status = "ready"

    def supported_flags(self) -> set[str]:
        """探测宿主当前版本实际声明了哪些 flag（版本兼容的唯一依据）。

        ``cline --help`` 是**运行时真值**：新版本新增/重命名 flag、旧版本缺少
        ``--data-dir`` 时，硬编码 argv 会直接失败。探测任何异常（无二进制、
        help 非零退出、超时）都返回空集，由调用方回落到「只下发基线 flag」+
        环境变量通道，绝不猜测。结果按可执行文件路径缓存（一次会话只探测一次）。
        """
        return set(probe_host_flags(self.executable))

    def acp_argv(
        self,
        *,
        model: str | None = None,
        data_dir: Path | str | None = None,
    ) -> list[str]:
        flags = self.supported_flags()
        command = [self.executable]
        if not flags or "--acp" in flags:
            command.append("--acp")
        if not flags or "--auto-approve" in flags:
            command += ["--auto-approve", "true"]
        # 探测到 ``--data-dir`` 才下发；``prepare_isolation`` 同时导出
        # ``CLINE_DATA_DIR``，两条通道互为备份（任一对当前版本生效即可）。
        if data_dir and (not flags or "--data-dir" in flags):
            command += ["--data-dir", str(data_dir)]
        if model:
            if not flags or "-m" in flags:
                command += ["-m", model]
            elif "--model" in flags:
                command += ["--model", model]
        return command

    def launch_policy(self) -> dict:
        return {
            "auto_approve": True,
            "auto_approve_flag": "--auto-approve true",
            "model_flag": "-m",
            "reason": "cline ACP 默认 autoApproveTools=false，未开则每个工具都要求 client 授权",
        }

    def prepare_isolation(
        self,
        root: Path,
        keep: set[str] | None,
        *,
        servers: McpServersBuilder,
    ) -> tuple[dict[str, str], Path | None, dict[str, Any]]:
        """建立「只暴露目标通道」的 ``CLINE_DATA_DIR`` 沙箱。

        认证文件按 ``core/hosts/cline.py::worker_runtime.sandbox_sync`` 同源清单
        实体拷贝（解析符号链接、不保留外部所有者），目录 0700 / 文件 0600；沙箱在
        一次性探活项目（``$TMPDIR``）内部，凭据不落被测工作区。用户全局配置只读不写。

        **初始与补证都隔离**：用户级 ``~/.cline`` 通常只注册两路之一（甚至没有），
        且 auto 模式会自动连后台 hub 守护进程导致 MCP 丢失，所以首次轮次也必须进
        沙箱。代价是「真实用户环境装完就能用」这一问不在本沙箱内回答——那需要另一次
        不隔离的探活，属显式未覆盖项，不得计入本次结论。
        """
        # 沙箱目录名与生产 Worker 沙箱同源（插件 ``worker_runtime.sandbox_dir``）。
        sandbox = root / _PLUGIN.worker_runtime.sandbox_dir
        settings_dir = sandbox / "settings"
        try:
            if sandbox.is_symlink() or settings_dir.is_symlink():
                return {}, None, {"mode": "unavailable", "reason": "sandbox_path_is_symlink"}
            settings_dir.mkdir(parents=True, exist_ok=True)
            sandbox.chmod(0o700)
            settings_dir.chmod(0o700)
        except OSError as exc:
            return {}, None, {"mode": "unavailable", "reason": f"mkdir_failed: {exc}"}
        source = cline_data_dir_source()
        synced: list[str] = []
        for rel_path in CLINE_SANDBOX_SYNC_FILES:
            src_file = source / rel_path
            try:
                if src_file.is_symlink() or not src_file.is_file():
                    continue
                dest = sandbox / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.parent.chmod(0o700)
                shutil.copyfile(src_file.resolve(), dest)
                dest.chmod(0o600)
            except OSError:
                continue
            synced.append(rel_path)
        settings = settings_dir / _MCP_SETTINGS_NAME
        payload = {"mcpServers": servers(keep)}
        try:
            settings.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            settings.chmod(0o600)
        except OSError as exc:
            return {}, None, {"mode": "unavailable", "reason": f"settings_write_failed: {exc}"}

        # DOC-243: 就地自愈对齐沙箱内 providers.json（首选真免费，遇阻/有有效订阅优先切订阅，避开 0 点数通道）
        prov_file = settings_dir / "providers.json"
        models_file = settings_dir / "models.json"
        if prov_file.is_file():
            try:
                try:
                    from core.host_runtime.host_provider_arbitrator import arbitrate_cline_provider
                except ImportError:  # pragma: no cover
                    from core.host_provider_arbitrator import arbitrate_cline_provider

                prov_data = json.loads(prov_file.read_text(encoding="utf-8"))
                models_data = json.loads(models_file.read_text(encoding="utf-8")) if models_file.is_file() else None
                best_prov, best_model = arbitrate_cline_provider(
                    prov_data, models_data, data_dir=sandbox
                )
                if best_prov:
                    prov_data["lastUsedProvider"] = best_prov
                    if best_model and best_prov in prov_data.get("providers", {}):
                        st_item = prov_data["providers"][best_prov].setdefault("settings", {})
                        if isinstance(st_item, dict):
                            st_item["model"] = best_model
                    prov_file.write_text(json.dumps(prov_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    prov_file.chmod(0o600)
            except Exception:
                pass

        info = {
            "mode": "data-dir",
            "sandbox": str(sandbox),
            "settings": str(settings),
            "synced_credentials": synced,
            "servers": sorted(payload["mcpServers"]),
        }
        return {
            "CLINE_DATA_DIR": str(sandbox),
            "CLINE_SESSION_BACKEND_MODE": "local",
        }, settings, info


PLUGIN = ClineAcpPlugin
