"""cloud_sync/consent.py - 云端数据共享同意的唯一真源（L2 F1 修复）。

语义（三态，禁止双轨实现）：

- ``True``  —— 用户显式同意共享；云端 push/pull 仍需项目 onboarding 完成。
- ``False`` —— 用户显式撤销；任何云端 egress（push 或 pull）一律拒绝，
  且不得写项目侧状态（撤销是用户级决定，不改写 ``.bugx/cloud-onboard.json``）。
- ``None``  —— 用户尚未表态；沿用既有 onboarding 状态判定，保持旧行为不变。

历史缺陷（F1）：``bugx cloud telemetry-config telemetry.data_sharing false`` 只清空
飞轮 outbox 并写 ``~/.bugx/cloud-config.json``，但 ``sync_layers`` / ``auto_sync``
只看 ``.bugx/cloud-onboard.json`` 的 ``state == "done"``，于是「已撤销共享」的项目
仍会继续上传 ledger/report。修复方式是把撤销做成读取期硬闸门（本模块），而不是
改写项目状态（不改状态机词表、无需迁移）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_ENV = "BUGX_CLOUD_CONFIG"
REVOKED_ERROR = "data_sharing_revoked"
REVOKED_DETAIL = "用户已撤销 telemetry.data_sharing，云端同步被拒绝"


class CloudConsentRevoked(RuntimeError):
    """撤销态下的强阻断异常：继承 RuntimeError，既有 `except RuntimeError`
    调用点会捕获并打印可读原因，不会把 traceback 泄给用户，也不会静默放行。"""

    code = REVOKED_ERROR


def require_sharing(action: str = "cloud sync") -> None:
    """底层 push/pull 通用硬闸门（fail-closed，禁止任何静默降级）。"""
    if sharing_revoked():
        raise CloudConsentRevoked(f"{REVOKED_DETAIL}（{action}）")


def config_path() -> Path:
    """用户级 cloud-config.json；与 CLI `_telemetry_config_path()` 同源。"""
    default = Path(os.environ.get("BUGX_HOME", str(Path.home() / ".bugx"))) / "cloud-config.json"
    return Path(os.environ.get(CONFIG_ENV, str(default))).expanduser()


def data_sharing_flag() -> bool | None:
    """三态同意判定；损坏/缺失配置返回 ``None``（未表态），绝不默认成同意。"""
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    telemetry = data.get("telemetry")
    if not isinstance(telemetry, dict):
        return None
    value = telemetry.get("data_sharing")
    return value if isinstance(value, bool) else None


def sharing_revoked() -> bool:
    """仅「显式 false」算撤销；未表态不算撤销。"""
    return data_sharing_flag() is False


def sharing_allowed() -> bool:
    """仅「显式 true」算同意。"""
    return data_sharing_flag() is True


def revoked_summary(scopes: set[str] | list[str] | None) -> dict:
    """与 ``sync_layers`` 返回结构对齐的拒绝汇总（零 egress、零状态变更）。"""
    scope_list = sorted(scopes) if scopes else ["global", "project"]
    return {
        "ok": False,
        "error": REVOKED_ERROR,
        "detail": REVOKED_DETAIL,
        "skipped": "consent_revoked",
        "scopes": scope_list,
    }
