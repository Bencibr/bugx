#!/usr/bin/env python3
"""Device identity, local installation token, and wire record mapping for cloud sync."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.report_engine.report_client import atomic_write_nofollow, load_installation_secret
from core.secure_atomic import AtomicWriteError, reject_symlink_components

logger = logging.getLogger(__name__)

CRED_PATH = os.environ.get(
    "BUGX_CLOUD_CREDENTIALS", str(Path.home() / ".bugx" / "cloud-credentials.json")
)
DEVICE_ID_PATH = os.environ.get(
    "BUGX_CLOUD_DEVICE_ID", str(Path.home() / ".bugx" / "cloud-device-id")
)
CLOUD_CONFIG_PATH = os.environ.get(
    "BUGX_CLOUD_CONFIG", str(Path.home() / ".bugx" / "cloud-config.json")
)
CLOUD_ROUTE_CACHE_PATH = os.environ.get(
    "BUGX_CLOUD_ROUTE_CACHE", str(Path.home() / ".bugx" / "cloud-route-cache.json")
)
def token_expires_in(token: str | None) -> float | None:
    """Return remaining seconds before token expiration, or None if opaque/unparseable."""
    if not token:
        return None
    parts = str(token).split(".")
    if len(parts) == 3:
        try:
            encoded = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(
                base64.urlsafe_b64decode(encoded).decode("utf-8")
            )
            exp = payload.get("exp") if isinstance(payload, dict) else None
            if isinstance(exp, (int, float)) and not isinstance(exp, bool):
                return float(exp) - time.time()
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            pass
    return None


@dataclass
class CloudCredentials:
    token: str | None = None
    refresh_token: str | None = None
    api_key: str | None = None
    user: dict | None = None
    cloud_region: str | None = None
    cloud_api_base: str | None = None
    cloud_route_environment: str | None = None
    cloud_route_expires_at: str | None = None
    cloud_route_version: str | None = None
    # 服务端确认的状态跨 CLI 进程保留；None 表示按 token/JWT 本地推断。
    auth_state_override: str | None = None

    def is_authenticated(self) -> bool:
        return bool(self.token or self.api_key)

    def auth_state(self) -> str:
        """Return missing/valid/expired for startup menu selection.

        Opaque tokens remain valid until the server rejects them. JWT payloads
        are decoded without trusting the signature solely to choose the menu;
        real API requests still perform server-side authentication.
        """
        # QUA-05：只读公开字段 auth_state_override。历史私有别名
        # _auth_state_override 已随破坏性更新移除（不再双写、不再兼容读取）。
        override = self.auth_state_override
        if override in {"valid", "expired", "missing"}:
            return override
        if self.api_key:
            return "valid"
        if not self.token:
            return "missing"
        exp_in = token_expires_in(self.token)
        if exp_in is not None and exp_in <= 0:
            return "expired"
        return "valid"

    @classmethod
    def _default_cred_path(cls) -> Path:
        import sys
        mod = sys.modules.get("core.cloud_sync.cloud_client")
        if mod and hasattr(mod, "CRED_PATH"):
            return Path(mod.CRED_PATH)
        return Path(os.environ.get("BUGX_CLOUD_CREDENTIALS", CRED_PATH))

    @classmethod
    def load(cls, path: str | Path | None = None) -> "CloudCredentials":
        p = Path(path) if path is not None else cls._default_cred_path()
        try:
            reject_symlink_components(p)
        except AtomicWriteError as exc:
            raise ValueError(f"云端凭据路径不安全: {p}") from exc
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return cls()
        return cls(token=data.get("token"), refresh_token=data.get("refresh_token"),
                   api_key=data.get("api_key"), user=data.get("user"),
                   auth_state_override=data.get("auth_state_override"),
                   cloud_region=data.get("cloud_region"),
                   cloud_api_base=data.get("cloud_api_base"),
                   cloud_route_environment=data.get("cloud_route_environment"),
                   cloud_route_expires_at=data.get("cloud_route_expires_at"),
                   cloud_route_version=data.get("cloud_route_version"))

    def save(self, path: str | Path | None = None) -> None:
        p = Path(path) if path is not None else self._default_cred_path()
        atomic_write_nofollow(
            p, json.dumps(asdict(self), ensure_ascii=False, indent=2).encode("utf-8"))

    def clear(self, path: str | Path | None = None) -> None:
        try:
            p = Path(path) if path is not None else self._default_cred_path()
            p.unlink()
        except OSError:
            pass


def get_device_label() -> str:
    """生成人类可读的设备显示名："{主机名} ({系统} {架构})"。

    用于同一账号多台电脑的 CLI 在云端设备列表中互相区分
    （服务端 CloudDevice.Name，size:200）。主机名裁剪 FQDN 域名
    后缀（不泄露内网拓扑）、剔除空白；不上传用户名。
    """
    import platform as _platform

    hostname = (_platform.node() or "").strip().split(".")[0]
    hostname = re.sub(r"\s+", "-", hostname) or "unknown-host"
    system = {"Darwin": "macOS", "Windows": "Windows"}.get(
        _platform.system(), _platform.system() or "unknown"
    )
    machine = (_platform.machine() or "").strip() or "unknown-arch"
    label = f"{hostname} ({system} {machine})"
    return label[:200]


def get_device_id(path: str | Path | None = None) -> str:
    """读取或创建本机 device_id（跨命令稳定）。拒绝符号链接。"""
    if path is None:
        import sys
        mod = sys.modules.get("core.cloud_sync.cloud_client")
        if mod and hasattr(mod, "DEVICE_ID_PATH"):
            path = mod.DEVICE_ID_PATH
        else:
            path = os.environ.get("BUGX_CLOUD_DEVICE_ID", DEVICE_ID_PATH)
    p = Path(path)
    try:
        reject_symlink_components(p)
    except AtomicWriteError as exc:
        raise ValueError(f"设备标识路径不安全: {p}") from exc
    try:
        val = p.read_text(encoding="utf-8").strip()
        if val:
            return val
    except OSError:
        pass
    derived: str | None = None
    try:
        secret = load_installation_secret(create=False)
        derived = hashlib.sha256(b"bugx-device-id-v1\x00" + secret).hexdigest()[:32]
    except (OSError, ValueError):
        derived = None
    if derived:
        try:
            atomic_write_nofollow(p, derived.encode("utf-8"))
        except (OSError, ValueError):
            pass  # 自愈回写失败也不影响本次返回的派生值（同机仍稳定）
        return derived
    pid = uuid.uuid4().hex
    try:
        atomic_write_nofollow(p, pid.encode("utf-8"))
    except OSError:
        pass  # 无法持久化时仍返回本次生成的 id（保持容错语义）
    return pid


# ---- 线格式映射：bugx ledger 记录 -> 云端记录 ----

# 错题卡质量闭环（联调方案 §2.4）：服务端 outcome 枚举与单请求上限
LESSON_FEEDBACK_OUTCOMES = ("hit", "use", "false_positive", "miss", "rejected")
LESSON_FEEDBACK_MAX_ITEMS = 100

def lesson_to_wire(pat: dict[str, Any]) -> dict[str, Any]:
    """bugx pattern 卡 -> SyncLessonRecord。

    probes/source_refs 的 wire 字符串字段是逗号 join（协议保留，兼容旧 pull 方），
    元素含逗号时有损；无损列表同步走 details["__lists"]，pull 端优先还原。
    """
    def joinkey(k):
        v = pat.get(k, [])
        if isinstance(v, (list, tuple)):
            return ",".join(str(x) for x in v)
        return str(v or "")
    details = {
        key: [str(x) for x in (pat.get(key) or [])]
        for key in (
            "playbook", "attempts_tried", "successful_solution", "applicability",
            "false_positive_patterns", "oracle_hints", "tags", "aliases",
            "negative_controls", "evidence_requirements",
        )
        if isinstance(pat.get(key), (list, tuple)) and pat.get(key)
    }
    # 无损列表通道：逗号 join 会破坏含逗号的元素（真实事故：probes 含
    # "web_route.go:90, rate_limiter.go:158" 拉回被拆成两条），列表原文进
    # details，pull 端优先还原。
    for key in ("probes", "source_refs"):
        if isinstance(pat.get(key), (list, tuple)) and pat.get(key):
            details[f"{key}"] = [str(x) for x in pat[key]]
    routing_tags = {
        str(key): [str(value) for value in values]
        for key, values in (pat.get("routing_tags") or {}).items()
        if isinstance(values, (list, tuple)) and values
    } if isinstance(pat.get("routing_tags"), dict) else {}
    return {
        "lesson_id": str(pat.get("pattern_id") or pat.get("lesson_id") or ""),
        "category": str(pat.get("category", "")),
        "title": str(pat.get("title", "")),
        "root_cause": str(pat.get("root_cause", "")),
        "reflection": str(pat.get("reflection", "") or ""),
        "probes": joinkey("probes"),
        "source_refs": joinkey("source_refs"),
        "status": str(pat.get("status", "") or ""),
        "experience_level": str(pat.get("experience_level", "") or ""),
        "hit_count": int(pat.get("hit_count", 0) or 0),
        "use_count": int(pat.get("use_count", 0) or 0),
        "miss_count": int(pat.get("miss_count", 0) or 0),
        "false_positive_count": int(pat.get("false_positive_count", 0) or 0),
        "rejected_count": int(pat.get("rejected_count", 0) or 0),
        "version": int(pat.get("version", 0) or 0),
        "superseded_by": str(pat.get("superseded_by", "") or ""),
        "deleted": bool(pat.get("deleted", False)),
        "device_id": str(pat.get("device_id", "") or ""),
        "details": details,
        "routing_tags": routing_tags,
        # 可复用模式卡（联调方案 §2.1）：拿不到就空串，tags 列表转逗号串
        "severity": str(pat.get("severity", "") or ""),
        "language": str(pat.get("language", "") or ""),
        "cwe_id": str(pat.get("cwe_id", "") or ""),
        "error_signature": str(pat.get("error_signature", "") or ""),
        "before_code": str(pat.get("before_code", "") or ""),
        "after_code": str(pat.get("after_code", "") or ""),
        "fix_summary": str(pat.get("fix_summary", "") or ""),
        "repro_oracle": str(pat.get("repro_oracle", "") or ""),
        "tags": joinkey("tags"),
        # 指纹是精确复用键，使用 JSON 字符串而不是逗号串，避免指纹内的
        # 分隔符被误拆；服务端按不透明字符串保存，pull 时再还原数组。
        "finding_fingerprints": json.dumps(
            [str(item) for item in (pat.get("finding_fingerprints") or [])],
            ensure_ascii=False, separators=(",", ":"),
        ) if isinstance(pat.get("finding_fingerprints"), (list, tuple)) else str(
            pat.get("finding_fingerprints") or "[]"
        ),
    }


def incident_to_wire(inc: dict[str, Any]) -> dict[str, Any]:
    """bugx incident -> SyncIncidentRecord。"""
    return {
        "incident_id": str(inc.get("incident_id", "") or ""),
        "lesson_id": str(inc.get("pattern_id") or inc.get("lesson_id") or ""),
        "category": str(inc.get("category", "") or ""),
        "title": str(inc.get("title", "") or ""),
        "root_cause": str(inc.get("root_cause", "") or ""),
        "summary": str(inc.get("summary", "") or ""),
        "before_code": str(inc.get("before_code") or inc.get("before_snippet") or ""),
        "after_code": str(inc.get("after_code") or inc.get("after_snippet") or ""),
        "decision_chain": str(inc.get("decision_chain") or inc.get("decision") or ""),
        "duration": int(inc.get("duration", inc.get("duration_seconds", 0)) or 0),
        "status": str(inc.get("status", "") or ""),
        "version": int(inc.get("version", 0) or 0),
        "deleted": bool(inc.get("deleted", False)),
        "device_id": str(inc.get("device_id", "") or ""),
        "details": {"context": str(inc.get("context", "") or "")},
        "routing_tags": {
            str(key): [str(value) for value in values]
            for key, values in (inc.get("routing_tags") or {}).items()
            if isinstance(values, (list, tuple)) and values
        } if isinstance(inc.get("routing_tags"), dict) else {},
        # 工具轨迹与经验回流（联调方案 §2.2/§2.3）：可选，缺省空串
        "tools_used": str(inc.get("tools_used", "") or ""),
        "tool_attempts": str(inc.get("tool_attempts", "") or ""),
        # 本地 incident 的 signature 就是服务端 error_signature 的来源；
        # repro_oracle 通常由关联 Pattern 注入，缺失时诚实留空。
        "error_signature": str(
            inc.get("error_signature") or inc.get("signature") or ""
        ),
        "repro_oracle": str(inc.get("repro_oracle") or ""),
    }



# Compatibility aliases
_token_expires_in = token_expires_in
