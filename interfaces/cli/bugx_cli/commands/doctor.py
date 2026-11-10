from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import __version__
from ..common import *
from ..infra import _mcp_session_hygiene_check
from ..infra.engine_installer import (
    _engine_build_id,
    _engine_receipt_build_id,
    _engine_integrity_status,
    _engine_self_integrity,
)


_CLOUD_ENDPOINT_CHECK_NAME = "云端端点连通性"
_CLOUD_DEFAULT_BASE_URL = "https://bugx.dev"
_CLOUD_HEALTH_PATHS = ("/health", "/api/v1/health")
_CLOUD_PROBE_TIMEOUT = 1.5


def _normalize_cloud_probe_base_url(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("云端地址为空")
    value = raw.strip()
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("云端地址必须是带 host 的 http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("云端地址不能包含用户名、密码、查询参数或 fragment")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("远程云端地址必须使用 HTTPS；HTTP 只允许本机测试服务")
    base_url = value.rstrip("/")
    if base_url.endswith("/api/v1"):
        base_url = base_url[: -len("/api/v1")]
    return base_url


def _project_cloud_mode_enabled(root: Path) -> bool:
    onboarding = root / ".bugx" / "cloud-onboard.json"
    try:
        data = json.loads(onboarding.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("state") in {"done", "sync"}


def _cloud_probe_target(root: Path) -> tuple[str | None, bool, str | None]:
    """Resolve an explicit cloud target without triggering route selection or DNS."""
    env_base = os.environ.get("BUGX_CLOUD_API_BASE", "").strip()
    if env_base:
        try:
            return _normalize_cloud_probe_base_url(env_base), True, None
        except ValueError as exc:
            return None, True, f"BUGX_CLOUD_API_BASE 无效：{exc}"

    try:
        from .cloud import _telemetry_config_path

        config_path = _telemetry_config_path()
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        return None, True, f"云端配置读取失败：{exc}"
    if not isinstance(config, dict):
        return None, True, "云端配置必须是 JSON 对象"

    raw_base = config.get("base_url")
    cloud_mode = _project_cloud_mode_enabled(root)
    if raw_base is not None and (not isinstance(raw_base, str) or raw_base.strip()):
        try:
            base_url = _normalize_cloud_probe_base_url(raw_base)
        except ValueError as exc:
            return None, True, f"cloud-config.json 的 base_url 无效：{exc}"
        # The stock endpoint is intentionally skipped for a local-only project;
        # an explicit onboarding state still means the user opted into cloud mode.
        return base_url, base_url != _CLOUD_DEFAULT_BASE_URL or cloud_mode, None
    if cloud_mode:
        return _CLOUD_DEFAULT_BASE_URL, True, None
    return None, False, None


def _cloud_endpoint_check(root: Path) -> tuple[str, bool, str]:
    base_url, explicit, config_error = _cloud_probe_target(root)
    if config_error:
        return _CLOUD_ENDPOINT_CHECK_NAME, False, config_error
    if not explicit or not base_url:
        return _CLOUD_ENDPOINT_CHECK_NAME, True, "跳过（未配置自定义云端端点）"

    _ensure_core_importable()
    ctx = None
    try:
        from core.infra.client_metadata import tls_context
        ctx = tls_context(base_url=base_url)
    except Exception:
        pass

    last_error: Exception | None = None
    for path in _CLOUD_HEALTH_PATHS:
        health_url = f"{base_url}{path}"
        try:
            kwargs: dict[str, Any] = {"timeout": _CLOUD_PROBE_TIMEOUT}
            if ctx is not None:
                kwargs["context"] = ctx
            response = urllib.request.urlopen(health_url, **kwargs)
            try:
                status = getattr(response, "status", None)
                if not isinstance(status, int):
                    status = response.getcode()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if status in (200, 204):
                return _CLOUD_ENDPOINT_CHECK_NAME, True, f"{base_url} 在线 (HTTP {status})"
            if status in (401, 403):
                return _CLOUD_ENDPOINT_CHECK_NAME, True, f"{base_url} 在线 (HTTP {status} 需鉴权)"
            return _CLOUD_ENDPOINT_CHECK_NAME, False, f"{base_url} 端点可达，但健康检查返回 HTTP {status}"
        except urllib.error.HTTPError as exc:
            if exc.code in (200, 204):
                return _CLOUD_ENDPOINT_CHECK_NAME, True, f"{base_url} 在线 (HTTP {exc.code})"
            if exc.code in (401, 403):
                return _CLOUD_ENDPOINT_CHECK_NAME, True, f"{base_url} 在线 (HTTP {exc.code} 需鉴权)"
            if exc.code == 404 and path != _CLOUD_HEALTH_PATHS[-1]:
                last_error = exc
                continue
            return _CLOUD_ENDPOINT_CHECK_NAME, False, f"{base_url} 端点可达，但健康检查返回 HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - doctor must report, not crash, on network errors.
            last_error = exc
            break

    if last_error is not None:
        try:
            from core.cloud_sync.cloud_client import diagnose_network_error
            diag = diagnose_network_error(last_error, health_url)
            if diag.get("code") in {
                "LOCAL_NETWORK_BLOCKED",
                "LOCAL_NETWORK_UNREACHABLE",
                "LOCAL_NETWORK_PROBE_UNAVAILABLE",
            }:
                return _CLOUD_ENDPOINT_CHECK_NAME, False, f"{base_url} 连通性诊断：{diag['message']}"
        except Exception:
            pass
        return _CLOUD_ENDPOINT_CHECK_NAME, False, f"{base_url} 连通性检查失败：{last_error}"

    return _CLOUD_ENDPOINT_CHECK_NAME, False, f"{base_url} 健康检查未返回结果"


def _heal_project_manifest_version(root: Path, *, force: bool = False) -> tuple[str, str] | None:
    """把滞后于当前 CLI 的 ``manifest.bugx_version`` 就地对齐（DOC-180）。

    返回 ``(旧值, 新值)``；无需对齐时返回 ``None``。只改这一个字段，绝不触碰
    运行态（生命、结算、错题集）。

    历史缺陷：这段自愈原先嵌在 ``if not version_ok:`` 里，而
    ``_version_contract_status`` 的归一化会把 ``0.1.0+2`` 视同 ``0.1.0`` 并返回
    ``version_ok=True``，于是**自愈分支永远进不去**——清单永久停在旧标记，而引擎
    门禁（另一套不归一化的比较）却一直拒绝启动。虚假阻断就这样被"修"成了永久阻断。
    """
    manifest_file = root / ".bugx" / "manifest.json"
    if not manifest_file.is_file() or manifest_file.is_symlink():
        return None
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    old = data.get("bugx_version")
    if not isinstance(old, str) or old == __version__:
        return None
    from ..infra.engine_installer import _contract_version
    if not force and _contract_version(old) != _contract_version(__version__):
        return None
    data["bugx_version"] = __version__
    try:
        _doctor_atomic_replace(
            manifest_file,
            (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            mode=manifest_file.stat().st_mode & 0o777,
        )
    except OSError:
        return None
    return old, __version__


def _asset_can_replace_global_engine() -> tuple[bool, str]:
    """Doctor repair may replace the global engine only from a strictly newer asset."""
    asset_id = _engine_build_id(asset_root())
    global_id = _engine_build_id(global_engine_root())
    if asset_id is None:
        return False, "安装资源（asset_root）缺少有效的 Build ID 标记，无法自动修复；请重新安装/升级 CLI。"
    if global_id is None:
        return False, (
            "全局引擎缺少 Build ID 标记（可能损坏或未初始化）；无法确认目标版本，拒绝自动替换。"
            "请先备份并核对共享引擎路径与可信安装资源；Doctor 不建议运行 `bugx install --force`。"
        )
    if global_id > asset_id:
        return False, (
            f"CLI 安装资源 build_id={asset_id} 低于全局引擎 build_id={global_id}；"
            f"拒绝自动降级。请先升级或重装 build_id >= {global_id} 的 CLI wheel，"
            "再重跑 doctor；不要运行 `bugx install --force`，它会将共享引擎降级。"
        )
    if global_id == asset_id:
        # 同 build **不等于**同内容：引擎文件被改坏/删缺时，安装资源仍是完好的
        # 同一份 build，此时重放它就是唯一正确的修复。原实现无条件 `return
        # False`，于是「引擎已损坏 + 资源同 build」变成永久死锁——doctor 每轮
        # 都报完整性失败，却永远拒绝修复，且提示让用户「先升级到更高 build」，
        # 而同源安装的 CLI 根本产不出更高 build（工具自己的 AI 诊断都写明了
        # 这一点）。这里只在**引擎自身校验失败**时放行重放。
        # 这里必须问「引擎与**源**是否一致」，不能问「引擎与自己的清单是否一致」。
        # 后者在同源安装下恒为真：改了源码后清单与引擎文件一起漂移，两边自洽，
        # 于是 doctor 一边报 bundle_sha256 不一致、一边判定「无需替换」——永久
        # 死锁，且提示让用户「升级到更高 build」，而同源 CLI 产不出更高 build。
        # 2026-11-03 实测复现：global 与 asset 同 build，前者缺 engine_run_node_flags
        # 修复，_engine_self_integrity 仍返回 (True, '488 个静态文件与自身清单一致')。
        healthy, _detail = _engine_integrity_status(asset_root(), global_engine_root())
        if healthy:
            return False, (
                f"全局引擎与安装资源 Build ID 相同 (build_id={global_id})"
                "且内容与源一致，无需替换。"
            )
        # 放行前必须确认**来源本身**完好：损坏的引擎配损坏的资源时重放，
        # 只会把坏字节原样覆盖回去，还顺手毁掉现场。此前的 `==` 分支无条件
        # 拒绝，连带把这条本可救的场景一起挡死了。
        source_ok, source_detail = _engine_self_integrity(asset_root())
        if not source_ok:
            # 缺陷 #11：两条判据（「引擎与源是否一致」和「源自身是否完好」）
            # 用了两套口径，用户因此拿到自相矛盾的处方——doctor 一边报
            # 「引擎与源不一致」，一边又说「同 build 无需替换/请升级到更高 build」，
            # 而同源安装根本产不出更高 build。这里必须把**两条判据的实际取值**
            # 一起说清，并给出唯一真正可执行的动作。
            return False, (
                "全局引擎与安装资源都不自洽："
                f"引擎↔源={_detail or '不一致'}；源自身={source_detail}。"
                "此时重放资源只会把坏字节原样覆盖回去，故拒绝修复。"
                "可执行动作：重装/升级 CLI（写入新的安装资源）后重跑 doctor；"
                "若资源是新装的仍报不自洽，说明该 wheel 本身损坏。"
            )
        return True, ""
    return True, ""


def _user_site_asset_candidates() -> list[tuple[str, Path]]:
    """Enumerate user-site engine payloads across interpreters and platforms.

    ``site.getuserbase()`` describes only the *running* interpreter, yet the
    replica that actually drifts is the one another interpreter installed (a
    second pyenv version, a Homebrew Python, or ``pip install --user`` under a
    different toolchain).  Those land in well-known per-platform bases, so all
    of them are enumerated instead of trusting a single lookup.
    """
    home = Path.home()
    candidates: list[tuple[str, Path]] = []

    def add(label: str, base: Path | None) -> None:
        if base is not None:
            candidates.append((label, base / "share" / "bugx-agent" / "core"))

    try:
        add("user-site", Path(site.getuserbase()))
    except Exception:  # noqa: BLE001 - discovery degrades, never aborts the check.
        pass
    # macOS: pip --user installs into ~/Library/Python/<X.Y>/
    mac_root = home / "Library" / "Python"
    if mac_root.is_dir():
        for base in sorted(mac_root.iterdir()):
            if base.is_dir():
                add(f"user-site:macos/{base.name}", base)
    # POSIX/pyenv: pip --user installs into ~/.local
    add("user-site:posix", home / ".local")
    # Windows: %APPDATA%\Python and %APPDATA%\Python<XY>
    appdata = os.environ.get("APPDATA")
    if appdata:
        roaming = Path(appdata)
        add("user-site:windows", roaming / "Python")
        if roaming.is_dir():
            for base in sorted(roaming.glob("Python*")):
                if base.is_dir():
                    add(f"user-site:windows/{base.name}", base)
    return candidates


def _path_entry_asset_candidates() -> list[tuple[str, Path]]:
    """Derive engine payload paths from every BugX console entry on PATH.

    A console script lives at ``<prefix>/bin/bugx`` (POSIX) or
    ``<prefix>\\Scripts\\bugx.exe`` (Windows), and pip installs the engine payload
    relative to that same prefix.  This catches interpreters whose data
    directory is unrelated to the running one (extra venvs, pipx, pyenv).
    """
    candidates: list[tuple[str, Path]] = []
    try:
        entries = _bugx_cli_paths()
    except Exception:  # noqa: BLE001 - discovery degrades, never aborts the check.
        return candidates
    for entry in entries:
        prefix = entry.parent.parent
        candidates.append((
            f"cli-prefix:{prefix.name or prefix}",
            prefix / "share" / "bugx-agent" / "core",
        ))
    return candidates


def _engine_replica_consistency_check() -> tuple[bool, str]:
    """Compare installed engine copies and the receipt that identifies the global copy."""
    global_root = global_engine_root()
    global_id = _engine_build_id(global_root)
    asset_build_id: int | None = None
    issues: list[str] = []
    replica_build_ids: list[tuple[str, int]] = []
    replica_inventory_complete = True
    replicas: list[tuple[str, Path]] = [("global", global_root)]
    try:
        replicas.append(("asset", asset_root()))
    except Exception as exc:  # Doctor reports discovery errors instead of aborting.
        issues.append(f"asset_root 无法读取：{exc}")
        replica_inventory_complete = False
    for label, user_asset in _user_site_asset_candidates() + _path_entry_asset_candidates():
        try:
            if user_asset.exists() or user_asset.is_symlink():
                replicas.append((label, user_asset))
        except OSError as exc:
            issues.append(f"{label} 安装资源路径无法读取：{user_asset}：{exc}")
            replica_inventory_complete = False

    seen: set[Path] = set()
    replica_details: list[str] = []
    for name, path in replicas:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as exc:
            issues.append(f"{name} 引擎副本路径无法解析：{path} ({exc})")
            replica_inventory_complete = False
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        build_id = _engine_build_id(path)
        if name == "asset":
            asset_build_id = build_id
        replica_details.append(f"{name}={path} build_id={build_id!r}")
        if build_id is None:
            issues.append(f"{name} 引擎副本缺少有效 Build ID：{path}")
            replica_inventory_complete = False
        else:
            replica_build_ids.append((name, build_id))
            if name != "global" and build_id != global_id:
                issues.append(f"{name} build_id={build_id} 与 global build_id={global_id!r} 不一致")

    receipt_path = global_root.parent / "engine-receipt.json"
    receipt_id = _engine_receipt_build_id(receipt_path)
    if receipt_id is None:
        issues.append(f"全局引擎安装回执缺失或无效：{receipt_path}")
    elif receipt_id != global_id:
        issues.append(f"安装回执 engine_build_id={receipt_id} 与 global build_id={global_id!r} 不一致")

    if issues:
        detail = "；".join(issues)
        if (global_id is not None and asset_build_id is not None
                and asset_build_id < global_id):
            detail += (
                f"；CLI 安装资源 build_id={asset_build_id} 低于全局引擎 build_id={global_id}，"
                f"拒绝自动降级。请先升级或重装 build_id >= {global_id} 的 CLI wheel 后重跑 doctor；"
                "不要运行 `bugx install --force`，它会将共享引擎降级并影响所有项目。"
            )
        elif asset_build_id is None or global_id is None or not replica_inventory_complete:
            detail += (
                "；当前 asset、全局引擎或至少一个已发现副本缺少有效 Build ID/无法读取，"
                "无法证明替换来源安全；不要运行 `bugx install --force`。"
                "先核对上述路径、安装包来源与 Build ID，再按确认后的目标修复。"
            )
        else:
            newer_replicas = [
                (name, build_id) for name, build_id in replica_build_ids
                if name != "asset" and build_id > asset_build_id
            ]
            if newer_replicas:
                newest_name, newest_id = max(newer_replicas, key=lambda item: item[1])
                detail += (
                    f"；已发现 {newest_name} build_id={newest_id} 高于当前 CLI 安装资源 "
                    f"build_id={asset_build_id}；不要运行 `bugx install --force`，"
                    f"它会用较旧资源覆盖共享引擎。请先改用或升级到安装资源 build_id >= {newest_id} 的 CLI。"
                )
            elif asset_build_id == global_id:
                detail += (
                    f"；当前 CLI 安装资源与共享全局引擎均为 build_id={global_id}，"
                    "同号不能证明逐文件内容可安全替换；不要运行 `bugx install --force`。"
                    "该命令只重建共享引擎，不会同步 user-site/cli-prefix 等独立副本；"
                    "请按上列路径与 Build ID 逐项处理冲突副本。"
                )
            else:
                detail += (
                    f"；当前 CLI 安装资源 build_id={asset_build_id} 严格高于共享全局引擎 "
                    f"build_id={global_id}，且不低于所有已发现副本；如需重建共享引擎，"
                    "可运行 `bugx install --force`。该命令不会同步较旧的 user-site/cli-prefix 副本。"
                )
        return False, detail
    return True, "；".join(replica_details) + f"；receipt engine_build_id={receipt_id}"


def _bugx_cli_paths() -> list[Path]:
    """Find every BugX console entry visible through PATH, one per directory.

    npm publishes several shim spellings (``bugx``/``bugx.cmd``/``bugx.ps1``)
    side by side in the same global bin directory.  Those are one install
    source, not an overwrite conflict, so they are collapsed to a single
    representative; only entries resolving into different directories are
    reported as distinct sources.

    pyenv also introduces shims (``~/.pyenv/shims/bugx``) that proxy execution
    to the active pyenv Python installation (e.g. ``~/.pyenv/versions/.../bin/bugx``).
    When both the shim and the resolved pyenv executable are present, they are
    collapsed to a single pyenv source rather than reported as conflicting installations.
    """
    names = ("bugx", "bugx.exe", "bugx.cmd", "bugx.bat") if os.name == "nt" else ("bugx",)
    found: list[Path] = []
    found_dirs: set[str] = set()
    for raw_entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = Path(raw_entry or os.curdir)
        for name in names:
            candidate = (entry / name).resolve()
            if not candidate.is_file():
                continue
            if candidate.parent.name == "shims" and (
                ".pyenv" in str(candidate) or candidate.parent.parent.name == ".pyenv"
            ):
                try:
                    res = subprocess.run(
                        ["pyenv", "which", candidate.name],
                        capture_output=True,
                        text=True,
                        timeout=1.0,
                    )
                    if res.returncode == 0 and res.stdout.strip():
                        target = Path(res.stdout.strip()).resolve()
                        if target.is_file():
                            candidate = target
                except (subprocess.SubprocessError, OSError):
                    pass
            directory = os.path.normcase(str(candidate.parent.resolve()))
            if directory in found_dirs:
                continue
            if ".pyenv" in directory and any(".pyenv" in d for d in found_dirs):
                continue
            found_dirs.add(directory)
            found.append(candidate)
    return found


def _inspect_cli_entry(path: Path) -> dict[str, Any]:
    """Classify a BugX executable path and sample its version."""
    path_str = str(path)
    source_type = "python"
    if "node_modules" in path_str or ".nvm" in path_str:
        source_type = "npm"
    elif path.is_file():
        try:
            head = path.read_bytes()[:256]
            if b"node" in head or b"bugx.js" in head:
                source_type = "npm"
            elif b"uv" in head or "uv/tools" in path_str:
                source_type = "uv"
        except OSError:
            pass

    if source_type != "npm":
        if "uv/tools" in path_str or ".local/share/uv" in path_str:
            source_type = "uv"
        elif "pyenv" in path_str:
            source_type = "pyenv"
        elif "pip" in path_str or "site-packages" in path_str:
            source_type = "pip"

    version: str | None = None
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        if proc.returncode == 0:
            out = proc.stdout.strip()
            parts = out.split()
            if len(parts) >= 2 and parts[0] in ("bugx", "bugx-cli"):
                version = parts[1]
            elif parts:
                version = parts[-1]
    except (subprocess.SubprocessError, OSError):
        pass

    return {
        "path": path,
        "type": source_type,
        "version": version,
    }


def _doctor_atomic_replace(target: Path, payload: bytes, *, mode: int | None = None) -> None:
    """Replace a regular project file atomically without following a target symlink."""
    if target.is_symlink():
        raise OSError(f"拒绝修复软链接目标：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _doctor_repair_assets(root: Path, layout: Any) -> list[str]:
    """修复损坏或缺失的运行态、覆盖清单与调度状态。"""
    actions: list[str] = []
    if layout is None:
        return actions
    def failed(step: str, exc: BaseException) -> None:
        actions.append(f"ERROR: {step}失败：{exc}")

    try:
        runtime_path = layout.runtime
        if runtime_path.is_symlink():
            raise OSError(f"拒绝修复软链接目标：{runtime_path}")
        bak_path = runtime_path.with_name(runtime_path.name + ".bak")
        need_repair = False
        if runtime_path.is_file() and not runtime_path.is_symlink():
            try:
                content = runtime_path.read_text(encoding="utf-8").strip()
                if not content:
                    need_repair = True
                else:
                    json.loads(content)
            except (json.JSONDecodeError, OSError):
                need_repair = True
        elif runtime_path.is_symlink() or bak_path.is_file():
            need_repair = True

        if need_repair:
            restored = False
            if bak_path.is_file() and not bak_path.is_symlink():
                try:
                    bak_data = json.loads(bak_path.read_text(encoding="utf-8").strip())
                    if isinstance(bak_data, dict):
                        _doctor_atomic_replace(
                            runtime_path, bak_path.read_bytes(),
                            mode=bak_path.stat().st_mode & 0o777,
                        )
                        actions.append("runtime.json 从 .bak 恢复")
                        restored = True
                except Exception as exc:
                    failed("runtime.json 备份恢复", exc)
            if not restored:
                if not any(item.startswith("ERROR: runtime.json 备份恢复") for item in actions):
                    actions.append("ERROR: runtime.json 损坏且无有效 .bak，拒绝重建；请从可信备份恢复")
    except Exception as exc:
        failed("runtime.json 修复", exc)

    try:
        project_file = root / ".bugx-project.json"
        if project_file.is_symlink():
            raise OSError(f"拒绝修复软链接目标：{project_file}")
        if not project_file.is_file():
            onboarding_file = root / ".bugx" / "cloud-onboard.json"
            if onboarding_file.is_file():
                try:
                    ob = json.loads(onboarding_file.read_text(encoding="utf-8"))
                    if isinstance(ob, dict) and ob.get("state") == "done":
                        import uuid
                        proj_id = f"proj-{uuid.uuid4().hex[:12]}"
                        payload = {
                            "project_id": proj_id,
                            "slug": root.name,
                            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        }
                        _doctor_atomic_replace(
                            project_file,
                            json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
                        )
                        actions.append("已自动补齐缺失的项目身份文件 .bugx-project.json")
                except Exception as exc:
                    failed("项目身份文件修复", exc)
    except Exception as exc:
        failed("项目身份文件修复", exc)

    try:
        sup_state = layout.supervisor
        if sup_state.is_symlink():
            raise OSError(f"拒绝修复软链接目标：{sup_state}")
        if sup_state.is_file():
            try:
                json.loads(sup_state.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                corrupted = sup_state.parent / f"{sup_state.name}.corrupted.{int(time.time())}"
                os.replace(sup_state, corrupted)
                bak = sup_state.with_name(sup_state.name + ".bak")
                if bak.is_file():
                    try:
                        bak_data = json.loads(bak.read_text(encoding="utf-8"))
                        if not isinstance(bak_data, dict):
                            raise ValueError("备份内容不是 JSON 对象")
                        _doctor_atomic_replace(
                            sup_state, bak.read_bytes(), mode=bak.stat().st_mode & 0o777,
                        )
                        actions.append("audit-supervisor.json 从 .bak 恢复")
                    except Exception as exc:
                        failed("audit-supervisor.json 备份恢复", exc)
                else:
                    actions.append("ERROR: audit-supervisor.json 损坏且无 .bak，拒绝重建")
    except Exception as exc:
        failed("audit-supervisor.json 修复", exc)

    try:
        repaired_links = _ensure_global_cli_symlinks()
        for link, status in repaired_links:
            if status == "created":
                actions.append(f"已创建全局 CLI 软链接：{link}")
    except Exception as exc:
        failed("全局 CLI 软链接修复", exc)

    try:
        from .lifecycle import _ensure_project_startup_todo
        startup_todo = root / ".bugx" / "startup-todo.md"
        if startup_todo.is_symlink():
            raise OSError(f"拒绝修复软链接目标：{startup_todo}")
        if not startup_todo.is_file():
            _ensure_project_startup_todo(root)
            actions.append("已重新生成 .bugx/startup-todo.md")
    except Exception as exc:
        failed("startup-todo.md 修复", exc)
    return actions


def _check_nested_worktrees_hygiene(root: Path) -> tuple[bool, str]:
    """检查 Git 注册的、位于项目根目录内的 linked worktree。"""
    git_dir = root / ".git"
    if not git_dir.exists():
        return True, "非 Git 仓库"

    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(root), capture_output=True, text=True, check=False, timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"无法读取 Git linked worktree：{exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "未知 Git 错误"
        return False, f"无法读取 Git linked worktree：{detail}"

    # `git worktree list --porcelain` emits one `worktree <path>` line per
    # registered worktree.  The first entry is the current checkout itself;
    # only linked worktrees nested below root can pollute this repository.
    nested_worktrees: list[Path] = []
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line.removeprefix("worktree ").strip()).expanduser()
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        if resolved != root.resolve():
            nested_worktrees.append(resolved)

    if nested_worktrees:
        unignored: list[str] = []
        for wt in nested_worktrees:
            rel = wt.relative_to(root.resolve())
            try:
                res = subprocess.run(
                    ["git", "check-ignore", "-q", "--", str(rel)],
                    cwd=str(root),
                    capture_output=True,
                    timeout=2,
                )
                if res.returncode != 0:
                    unignored.append(str(rel))
            except Exception:
                unignored.append(str(rel))
        if unignored:
            return False, f"发现未被 .gitignore 忽略的嵌套 worktree（{', '.join(unignored)}），极易污染索引，建议加入 .gitignore 或移出仓库"
    return True, "工作树隔离正常（无未忽略嵌套 worktree）"



def _host_runtime_gate(
    host_checks: list[tuple[str, bool, str]],
    installed_host_names: list[str] | set[str],
    detected_hosts: dict[str, dict[str, object]],
) -> bool:
    """Decide whether every recorded host is runtime-ready.

    ``installed_host_names`` comes from ``.bugx/hosts.json`` (BugX wiring was
    installed); ``detected_hosts`` comes from live discovery, where
    ``installed`` means "the host CLI is on PATH" and ``project_marker`` means
    "the project-level wiring exists". Those are different facts (P3f: the dsh
    adapter deliberately decouples marker from CLI presence), so a host whose
    CLI is missing from PATH but whose complete bundle is wired into the project
    must not flip ``runtime_ready`` to False — that produced the contradictory
    "44 checks green, runtime_ready=false" report.
    """
    if not installed_host_names:
        return True
    ready = True
    for name in installed_host_names:
        try:
            adapter = get_adapter(name)
        except ValueError:
            ready = False
            continue
        bundle_ok = any(
            item[0] == f"{adapter.label} 完整适配包" and item[1]
            for item in host_checks
        )
        detected = detected_hosts.get(name, {})
        usable = bool(detected.get("installed") or detected.get("project_marker"))
        ready = ready and bundle_ok and usable
    return ready


def _doctor_auto_repairable_checks(
    checks: list[tuple[str, bool, str]],
    *,
    repair_requested: bool,
    installation_only: bool,
) -> set[str]:
    """Return only checks with a concrete, currently available Doctor repair path.

    ``doctor --repair`` has already attempted its repairs before diagnostics are
    rendered, so remaining failures must not be offered the same repair again.
    Host bundle replacement is deliberately excluded because it used to force
    rewrite project-owned files without preserving local edits.
    """
    if repair_requested:
        return set()

    failed = {name for name, ok, _detail in checks if not ok}
    eligible: set[str] = set()
    if not installation_only and "Tree-sitter 多语言增强" in failed:
        eligible.add("Tree-sitter 多语言增强")

    engine_checks = failed & {"全局引擎完整性", "BugX 版本一致性"}
    if engine_checks:
        try:
            can_replace, _detail = _asset_can_replace_global_engine()
        except (CliError, OSError, ValueError):
            can_replace = False
        if can_replace:
            eligible.update(engine_checks)
    return eligible


def _generate_ai_diagnostics(
    checks: list[tuple[str, bool, str]],
    installation_required: set[str],
    root: Path,
    strict: bool = False,
    auto_repairable_checks: set[str] | None = None,
) -> dict[str, Any]:
    """基于失败项生成 AI 根因分析和本地确定性修复提示。"""
    from ..infra.ai_advisor import DiagnosticAIArbitrator
    arbitrator = DiagnosticAIArbitrator(root)
    result = arbitrator.diagnose(
        checks,
        installation_required,
        strict=strict,
        auto_repairable_checks=auto_repairable_checks,
    )
    return result.as_dict()


def _render_ai_diagnostics_panel(ai_diag: dict[str, Any], is_explicit_ai: bool = False) -> None:
    items = ai_diag.get("items", [])
    if items:
        print("\n╭── 🤖 BugX AI 根因分析与确定性修复提示 ─────────────────────────────────╮")
        print(f"│ {ai_diag.get('summary', '')}")
        print("│")
        for idx, item in enumerate(items, 1):
            req_badge = "[必需]" if item.get("required") else "[可选]"
            repair_badge = " (可由 doctor --repair 尝试修复)" if item.get("auto_repairable") else ""
            print(f"│ {idx}. {req_badge} 【{item['name']}】{repair_badge}")
            print(f"│    • 根因分析: {item['root_cause']}")
            print(f"│    • 修复处方: {item['prescription']}")
            if idx < len(items):
                print("│")
        if ai_diag.get("can_auto_repair"):
            print("│")
            print("│ Doctor 检测到可尝试的修复项；执行前请确认其影响范围:")
            print(f"│    👉 {ai_diag['auto_repair_command']}")
        print("╰───────────────────────────────────────────────────────────────────────╯")
    elif is_explicit_ai:
        print("\n╭── 🤖 BugX AI 智能排障分析 ──────────────────────────────────────────╮")
        print(f"│ {ai_diag.get('summary', '')}")
        print("╰───────────────────────────────────────────────────────────────────────╯")


def command_doctor(args: argparse.Namespace) -> int:
    from .agent import _check_host_mcp_config
    from .lifecycle import _global_opencode_integrity_check, _global_pi_integrity_check
    from .agent import _CliProgress

    progress = _CliProgress(args)
    progress.update(0, "准备诊断")
    root = args.root.resolve()
    if getattr(args, "target", "all") == "mcp-runtime":
        progress.update(20, "正在诊断 MCP runtime")
        runtime_root = default_mcp_runtime_root()
        code, payload = _mcp_runtime_doctor_payload(root, runtime_root)
        progress.finish("MCP runtime 诊断完成")
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"BugX {get_full_version()} · MCP 离线 runtime 诊断")
            for item in payload.get("checks", []):
                marker = "✓" if item["ok"] else "!"
                print(f"  {marker} {item['name']}: {item['detail']}")
            for issue in payload.get("issues", []):
                print(f"  ! 不一致：{issue}")
        return code
    checks = []
    checks.append(("Python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0]))
    try:
        assets = asset_root()
        checks.append(("安装资源", True, str(assets)))
    except CliError as exc:
        checks.append(("安装资源", False, str(exc)))
    manifest_ok = (root / ".bugx" / "manifest.json").is_file()
    layout = None
    layout_error = None
    repair_actions: list[str] = []
    init_issues: list[str] = []

    if manifest_ok:
        try:
            layout_module = _layout_module(root)
            if args.repair:
                layout = layout_module.repair(root)
                repair_actions = _doctor_repair_assets(root, layout)
            else:
                layout = layout_module.require(root)
        except (CliError, OSError, ValueError, RuntimeError) as exc:
            layout_error = str(exc)

        if not getattr(args, "installation_only", False):
            onboarding_file = root / ".bugx" / "cloud-onboard.json"
            if not onboarding_file.is_file():
                init_issues.append("缺少云端 onboarding 记录（.bugx/cloud-onboard.json）")
            else:
                try:
                    ob_data = json.loads(onboarding_file.read_text(encoding="utf-8"))
                    ob_state = ob_data.get("state") if isinstance(ob_data, dict) else None
                    if ob_state not in ("done", "local", "sync"):
                        init_issues.append(f"云端 onboarding 未决（状态: {ob_state}）")
                    elif ob_state in ("done", "sync"):
                        project_file = root / ".bugx-project.json"
                        if not project_file.is_file():
                            init_issues.append("开启了云端同步但缺少项目身份文件（.bugx-project.json）")
                except Exception as exc:
                    init_issues.append(f"云端 onboarding 记录损坏: {exc}")

            startup_todo = root / ".bugx" / "startup-todo.md"
            if not startup_todo.is_file():
                init_issues.append("缺少启动视图（.bugx/startup-todo.md）")
    else:
        init_issues.append("缺少 .bugx/manifest.json")

    if layout_error:
        init_issues.insert(0, f".bugx 项目布局校验失败: {layout_error}")

    initialized = manifest_ok and layout is not None and not init_issues
    status_guidance = (
        "先只读运行 `bugx engine --root <ROOT> status --json`；"
        "只有状态精确为 `UNINITIALIZED` 时才运行 `bugx engine --root <ROOT> pre`。"
        "返回 `FATAL_BLOCKED`、其他状态或检查失败时保留现场并停止；"
        "不得用 `bugx init`、`bugx reset` 或 `--force` 绕过。"
    )
    if initialized:
        init_detail = str(root / ".bugx")
    elif not manifest_ok:
        init_detail = f"项目状态未判定（缺少 .bugx/manifest.json）。{status_guidance}"
    else:
        init_detail = f"初始化未就绪（{'；'.join(init_issues)}）。{status_guidance}"

    checks.append(("项目初始化状态", initialized, init_detail))
    checks.append((".bugx 运行目录", layout is not None, layout_error or str(root / ".bugx")))
    wt_ok, wt_detail = _check_nested_worktrees_hygiene(root)
    checks.append(("工作树卫生与嵌套隔离", wt_ok, wt_detail))
    if getattr(args, "installation_only", False):
        checks.append((_CLOUD_ENDPOINT_CHECK_NAME, True, "跳过（--installation-only）"))
    else:
        checks.append(_cloud_endpoint_check(root))

    if args.repair:
        repair_errors = [item for item in repair_actions if item.startswith("ERROR:")]
        checks.append((
            "项目资产修复",
            not repair_errors,
            "；".join(repair_errors or repair_actions or ["无需修复"]),
        ))
    raw_requested_hosts = []
    for item in getattr(args, "agent", []):
        for sub in str(item).split(","):
            sub = sub.strip()
            if sub:
                raw_requested_hosts.append(sub)
    target_hosts = set(raw_requested_hosts) if raw_requested_hosts else None

    progress.update(25, "项目和宿主检查完成")
    host_checks, installed_host_names = _host_bundle_checks(root, target_hosts=target_hosts)
    if target_hosts:
        installed_host_names = installed_host_names | target_hosts
    host_registry_fallback = any(
        name == "宿主安装清单缺失或损坏" for name, _, _ in host_checks
    )
    host_checks = [
        (
            name,
            ok,
            detail + (
                "；Doctor 不会强制重装整套宿主资产，以免覆盖项目内本地修改；"
                "先检查并保存目标文件，再对单个宿主执行 `bugx install --root <ROOT> --agent <HOST>`。"
            ) if not ok and name.endswith("完整适配包") else detail,
        )
        for name, ok, detail in host_checks
    ]
    checks.extend(host_checks)
    try:
        engine_ok, engine_detail = _engine_integrity_status(
            asset_root(), global_engine_root(),
        )
    except (CliError, OSError, ValueError) as exc:
        engine_ok, engine_detail = False, f"全局引擎无法校验：{exc}"
    if not engine_ok and getattr(args, "repair", False):
        can_replace, guard_detail = _asset_can_replace_global_engine()
        if not can_replace:
            engine_detail = guard_detail
        else:
            try:
                _install_engine(asset_root(), force=True)
                engine_ok, engine_detail = _engine_integrity_status(
                    asset_root(), global_engine_root(),
                )
                engine_detail += "（已自动修复恢复完整性）"
            except Exception as exc:
                engine_detail = f"自动修复全局引擎失败：{exc}"
    checks.append(("全局引擎完整性", engine_ok, engine_detail))
    try:
        version_ok, version_detail = _version_contract_status(
            root, global_engine_root(),
        )
    except (CliError, OSError, ValueError) as exc:
        version_ok, version_detail = False, f"版本契约无法校验：{exc}"
    # 项目 manifest 只存语义版本；它的基础版本自愈必须独立于完整 Build ID 比对。
    if version_ok and getattr(args, "repair", False):
        healed = _heal_project_manifest_version(root)
        if healed is not None:
            version_detail += f"（已就地对齐项目清单版本 {healed[0]} → {healed[1]}）"
    if not version_ok and getattr(args, "repair", False):
        can_replace, guard_detail = _asset_can_replace_global_engine()
        if not can_replace:
            version_detail = guard_detail
        else:
            try:
                _install_engine(asset_root(), force=True)
                _heal_project_manifest_version(root, force=True)
                version_ok, version_detail = _version_contract_status(
                    root, global_engine_root(),
                )
                version_detail += "（已自动刷新全局引擎与项目清单；旧项目入口会在 init/install 时直接删除）"
            except Exception as exc:
                version_detail = f"自动对齐版本失败：{exc}"
    checks.append(("BugX 版本一致性", version_ok, version_detail))
    replicas_ok, replicas_detail = _engine_replica_consistency_check()
    checks.append(("引擎副本一致性", replicas_ok, replicas_detail))
    progress.update(45, "引擎和版本检查完成")
    if "opencode" in installed_host_names and (target_hosts is None or "opencode" in target_hosts):
        checks.append(("OpenCode 全局入口完整性", *_global_opencode_integrity_check()))
    if "pi" in installed_host_names and (target_hosts is None or "pi" in target_hosts):
        checks.append(("Pi 全局 Skill 完整性", *_global_pi_integrity_check(root)))
    detected_hosts = {item["name"]: item for item in discover_hosts(root)}
    for host_name in sorted(installed_host_names):
        if target_hosts is not None and host_name not in target_hosts:
            continue
        if host_name not in {adapter.name for adapter in HOST_ADAPTERS}:
            # hosts.json 可能由更新版本的 CLI 写入（降级/前向兼容场景）。
            # doctor 必须结构化报告未知宿主，禁止以未处理异常崩溃。
            checks.append((
                f"{host_name} MCP 配置", False,
                "当前 CLI 不识别该宿主（可能由更新版本的 BugX 安装）；请升级 CLI 后重试",
            ))
            continue
        ok, detail = _check_host_mcp_config(
            root, host_name,
            repair=getattr(args, "repair", False) and not host_registry_fallback,
        )
        adapter = get_adapter(host_name)
        checks.append((f"{adapter.label} MCP 配置", ok, detail))
    is_installation_only = bool(getattr(args, "installation_only", False))
    if is_installation_only:
        lesson_ready = True
        checks.append(("错题集账本", True, "跳过运行态检查（--installation-only）"))
        if layout is not None:
            checks.append(("全局错题集（可选）", True, "跳过运行态检查（--installation-only）"))
        graph = importlib.util.find_spec("tree_sitter_language_pack") is not None
        checks.append(("Tree-sitter 多语言增强", graph, "已安装" if graph else "可选：pip install 'bugx-cli[graph]'"))
        checks.append(("codebase-memory 推荐加速", True, "跳过运行态检查（--installation-only）"))
        checks.append(("基础效率工具", True, "跳过运行态检查（--installation-only）"))
        mcp_runtime_ok, mcp_runtime_detail = _mcp_runtime_check(root)
        checks.append(("MCP 离线 runtime", mcp_runtime_ok, mcp_runtime_detail))
        progress.update(80, "运行态检查已跳过")
        checks.append(("UI 视觉与浏览器挖掘环境（可选）", True, "跳过运行态检查（--installation-only）"))
    else:
        lesson_json = layout.knowledge_seed if layout is not None else root / ".bugx" / "knowledge" / "lesson-ledger.json"
        lesson_db = lesson_json.with_suffix(".sqlite3")
        lesson_ready = False
        lesson_detail = "项目尚未初始化"
        if initialized:
            if not lesson_db.is_file() or lesson_db.is_symlink():
                lesson_detail = "缺少或不安全的 SQLite 运行库；请从备份恢复，禁止静默重建"
            else:
                try:
                    result = subprocess.run(
                        [sys.executable, str(_script(root, "lesson_ledger.py")),
                         "validate", "--ledger", str(lesson_json), "--json"],
                        cwd=root, capture_output=True, text=True, check=False, timeout=30,
                        env=_python_environment(),
                    )
                    lesson_ready = result.returncode == 0
                    lesson_detail = ("SQLite 运行库有效" if lesson_ready else (
                        (result.stderr or result.stdout).strip()[:500] or "SQLite 校验失败"
                    ))
                    if lesson_ready and (not lesson_json.is_file() or lesson_json.is_symlink()):
                        lesson_detail += "；JSON 种子缺失（不影响读取，恢复时需人工确认）"
                except (OSError, subprocess.TimeoutExpired, CliError) as exc:
                    lesson_detail = f"SQLite 校验异常：{exc}"
        checks.append(("错题集账本", lesson_ready, lesson_detail))
        if layout is not None:
            global_db = layout.global_knowledge_db
            global_marker = layout.global_knowledge_root / "manifest.json"
            if not global_marker.exists() and not global_db.exists():
                global_ok, global_detail = True, "尚未初始化；项目库可独立运行"
            elif not global_db.is_file() or global_db.is_symlink():
                global_ok, global_detail = False, "全局 SQLite 缺失；不会静默从 JSON 重建"
            else:
                try:
                    result = subprocess.run(
                        [sys.executable, str(_script(root, "lesson_ledger.py")),
                         "validate", "--ledger", str(layout.global_knowledge_seed), "--json"],
                        cwd=root, capture_output=True, text=True, check=False, timeout=30,
                        env=_python_environment(),
                    )
                    global_ok = result.returncode == 0
                    global_detail = "全局 SQLite 有效（只读投影源）" if global_ok else (
                        (result.stderr or result.stdout).strip()[:500] or "全局 SQLite 校验失败"
                    )
                except (OSError, subprocess.TimeoutExpired, CliError) as exc:
                    global_ok, global_detail = False, f"全局 SQLite 校验异常：{exc}"
            checks.append(("全局错题集（可选）", global_ok, global_detail))
        graph = importlib.util.find_spec("tree_sitter_language_pack") is not None
        if not graph and getattr(args, "repair", False):
            # 只有在明确指定 --repair 时才尝试自动安装，普通诊断快速探测不挂起
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "tree-sitter-language-pack>=1.14.3", "--no-warn-script-location"],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                if proc.returncode == 0:
                    importlib.invalidate_caches()
                    graph = importlib.util.find_spec("tree_sitter_language_pack") is not None
            except Exception:
                pass
        graph_detail = "已就绪" if graph else "可选：缺失（执行 bugx doctor --repair 或 pip install 'bugx-cli[graph]' 启用）"
        checks.append(("Tree-sitter 多语言增强", graph, graph_detail))
        # 优化：优先尝试内存级快速 inspect，失败回退子进程
        try:
            from core.probes import codebase_memory_probe
            cbm = codebase_memory_probe.inspect(root)
            cbm_ok = cbm.get("status") == "READY"
            cbm_detail = (
                f"{cbm.get('status')}，版本 {cbm.get('version') or '未安装'}"
                + (f"；项目索引已就绪" if cbm_ok else f"；未就绪：{cbm.get('fallback')}")
            )
        except Exception:
            try:
                cbm_result = subprocess.run(
                    [sys.executable, str(_script(root, "codebase_memory_probe.py")),
                     "status", "--root", str(root), "--json"],
                    cwd=root, capture_output=True, text=True, check=False, timeout=60,
                    env=_python_environment(),
                )
                cbm = json.loads(cbm_result.stdout)
                cbm_ok = cbm.get("status") == "READY"
                cbm_detail = (
                    f"{cbm.get('status')}，版本 {cbm.get('version') or '未安装'}"
                    + (f"；项目索引已就绪" if cbm_ok else f"；未就绪：{cbm.get('fallback')}")
                )
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, CliError) as exc:
                cbm_ok, cbm_detail = False, f"DEGRADED：{exc}"
        checks.append(("codebase-memory 推荐加速", cbm_ok, cbm_detail))
        try:
            from core.probes import efficiency_probe
            efficiency = efficiency_probe.inspect(root)
            efficiency_ok = efficiency.get("status") == "READY"
            missing = [
                item.get("name", "unknown")
                for item in efficiency.get("base_tools", []) if not item.get("ok")
            ]
            efficiency_detail = efficiency.get("status", "DEGRADED")
            if missing:
                efficiency_detail += f"；待补充 {', '.join(missing)}"
            efficiency_detail += "；内置回退可用"
        except Exception:
            try:
                efficiency_result = subprocess.run(
                    [sys.executable, str(_script(root, "efficiency_probe.py")),
                     "status", "--root", str(root), "--json"],
                    cwd=root, capture_output=True, text=True, check=False, timeout=45,
                    env=_python_environment(),
                )
                efficiency = json.loads(efficiency_result.stdout)
                efficiency_ok = efficiency.get("status") == "READY"
                missing = [
                    item.get("name", "unknown")
                    for item in efficiency.get("base_tools", []) if not item.get("ok")
                ]
                efficiency_detail = efficiency.get("status", "DEGRADED")
                if missing:
                    efficiency_detail += f"；待补充 {', '.join(missing)}"
                efficiency_detail += "；内置回退可用"
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, CliError) as exc:
                efficiency_ok, efficiency_detail = False, f"DEGRADED：{exc}"
        checks.append(("基础效率工具", efficiency_ok, efficiency_detail))
        mcp_runtime_ok, mcp_runtime_detail = _mcp_runtime_check(root)
        checks.append(("MCP 离线 runtime", mcp_runtime_ok, mcp_runtime_detail))
        # DOC-230 3.2 第 6 项：MCP 会话进程卫生（阈值化 + advisory，不进
        # installation_required —— 审计 Q7）。传 state_root 以做**活动感知**判定，
        # 避免把长命但健康的会话误报为泄漏。
        try:
            import core.bugx_paths as bugx_paths

            mcp_state_root = bugx_paths.require(root).state_root
        except Exception:  # noqa: BLE001 - 拿不到状态根时退化为跳过遗弃判定
            mcp_state_root = None
        mcp_hygiene_ok, mcp_hygiene_detail = _mcp_session_hygiene_check(root, mcp_state_root)
        checks.append(("MCP 会话进程卫生", mcp_hygiene_ok, mcp_hygiene_detail))
        progress.update(80, "运行态检查完成")
        # DOC-065: UI 视觉与浏览器挖掘环境诊断（可选）
        try:
            ui_script = _script(root, "setup_ui_env.py")
            if ui_script.is_file():
                ui_res = subprocess.run(
                    [sys.executable, str(ui_script), "status"],
                    cwd=root, capture_output=True, text=True, check=False, timeout=30,
                    env=_python_environment(),
                )
                ui_ok = ui_res.returncode == 0
                if ui_ok:
                    ui_detail = "UI/TUI 环境就绪（Playwright / TUI 可用）"
                else:
                    missing_lines = [
                        line.strip() for line in (ui_res.stdout or "").splitlines()
                        if "缺失" in line or "✗" in line
                    ]
                    ui_detail = missing_lines[0] if missing_lines else "部分依赖缺失；运行 python3 setup_ui_env.py install 补装"
                checks.append(("UI 视觉与浏览器挖掘环境（可选）", ui_ok, ui_detail))
        except Exception as exc:
            checks.append(("UI 视觉与浏览器挖掘环境（可选）", False, f"检测跳过: {exc}"))
    cli_paths = _bugx_cli_paths()
    if len(cli_paths) > 1:
        inspected = [_inspect_cli_entry(p) for p in cli_paths]
        active_entry = shutil.which("bugx") or (str(cli_paths[0]) if cli_paths else None)
        active_path = Path(active_entry).resolve() if active_entry else cli_paths[0]

        has_npm = any(item["type"] == "npm" for item in inspected)
        has_pip = any(item["type"] in ("pip", "uv", "pyenv", "python") for item in inspected)
        versions = {item["version"] for item in inspected if item["version"]}

        parts_desc = []
        for item in inspected:
            is_active = (item["path"] == active_path)
            tag = "生效中" if is_active else "被覆盖"
            ver_str = f" v{item['version']}" if item["version"] else ""
            parts_desc.append(f"{item['path']} ({item['type']}{ver_str}, {tag})")

        if has_npm and has_pip:
            if len(versions) > 1:
                cli_detail = (
                    "发现 npm 与 pip/uv 双重安装且版本冲突："
                    + "；".join(parts_desc)
                    + "。建议卸载多余版本：npm 运行 npm uninstall -g bugx-cli，"
                    "pip 运行 pip uninstall bugx-cli 或 uv tool uninstall bugx-cli"
                )
            else:
                cli_detail = (
                    "发现 npm 与 pip/uv 均存在安装（当前版本一致）："
                    + "；".join(parts_desc)
                    + "。建议保留单一包管理器以避免后续升级漂移"
                )
        else:
            cli_detail = "发现多个 PATH 中的 BugX 入口，可能存在安装覆盖：" + "；".join(parts_desc)
    elif cli_paths:
        cli_detail = str(cli_paths[0])
    else:
        cli_detail = "PATH 中未发现 bugx 入口"
    checks.append(("BugX CLI 路径", len(cli_paths) <= 1, cli_detail))
    installation_required = {
        "Python >= 3.11", "安装资源", "全局引擎完整性",
        "BugX 版本一致性",
        "引擎副本一致性",
    }
    # F-L0-01 整改：``--installation-only`` 是「安装体检」，不得因为目标目录
    # 尚未初始化（没有 .bugx/manifest.json）而判定安装失败——安装完整性与
    # 项目状态是两个独立维度。但**已存在项目**（manifest 已落盘）却状态损坏
    # 时必须继续 fail-closed（DOC-227 半初始化拒绝放行），因此仅在
    # 「manifest 存在」或「非 installation-only 模式」时才把项目初始化状态
    # 计入必需项。
    if manifest_ok or not args.installation_only:
        installation_required.add("项目初始化状态")
    strict = bool(getattr(args, "strict", False))
    if strict:
        installation_required.add("工作树卫生与嵌套隔离")
        installation_required.add(_CLOUD_ENDPOINT_CHECK_NAME)
    if args.repair:
        installation_required.add("项目资产修复")
    installation_required.update(name for name, _, _ in host_checks)
    if "opencode" in installed_host_names:
        installation_required.update({"OpenCode MCP 配置", "OpenCode 全局入口完整性"})
    if "pi" in installed_host_names:
        installation_required.add("Pi 全局 Skill 完整性")
    installation_ready = all(
        ok for name, ok, detail in checks if name in installation_required
    )
    if installed_host_names:
        host_runtime_ready = _host_runtime_gate(
            host_checks, installed_host_names, detected_hosts,
        )
        runtime_ready = installation_ready and lesson_ready and host_runtime_ready
    else:
        # BugX core is host-neutral.  With no host selected, doctor validates
        # the core and knowledge stores only; an AI host may be installed later.
        runtime_ready = installation_ready and lesson_ready
    if (default_mcp_runtime_root()).exists():
        # 已安装过 runtime 时才参与就绪门禁；未安装只是可选项，不阻断。
        runtime_ready = runtime_ready and mcp_runtime_ok
    # 可选项（Tree-sitter、codebase-memory、效率工具、UI 环境等）失败只降级，
    # 不再把退出码拖到 1；只有 --strict 时才把它们计入。
    optional_degraded = [
        name for name, ok, _detail in checks
        if not ok and name not in installation_required
    ]
    if args.installation_only:
        ready = installation_ready
    else:
        ready = runtime_ready if strict else installation_ready
    failed_required = [
        {"name": name, "detail": detail}
        for name, ok, detail in checks
        if not ok and name in installation_required
    ]
    auto_repairable_checks = _doctor_auto_repairable_checks(
        checks,
        repair_requested=bool(getattr(args, "repair", False)),
        installation_only=is_installation_only,
    )
    ai_diag = _generate_ai_diagnostics(
        checks,
        installation_required,
        root,
        strict=strict,
        auto_repairable_checks=auto_repairable_checks,
    )
    escalation = None
    if not ready:
        escalation = {
            "required": True,
            "failed_checks": failed_required,
            "suggested_llm_action": "diagnose_environment_failure",
            "message": "程序级检查未完全通过，已生成 AI 智能诊断分析与自愈处方",
            "ai_prescriptions": [
                {
                    "name": item["name"],
                    "root_cause": item["root_cause"],
                    "prescription": item["prescription"],
                    "auto_repairable": item["auto_repairable"],
                }
                for item in ai_diag.get("items", [])
                if item.get("required")
            ],
            "auto_repair_command": ai_diag.get("auto_repair_command"),
        }
    payload = {
        "version": __version__, "root": str(root),
        "mode": "installation-only" if args.installation_only else "runtime",
        "installation_ready": installation_ready,
        "runtime_ready": runtime_ready,
        "optional_degraded": optional_degraded,
        "strict": strict,
        "ready": ready,
        "escalation": escalation,
        "ai_diagnostics": ai_diag,
        "checks": [{"name": name, "ok": ok, "detail": detail} for name, ok, detail in checks],
    }
    progress.finish("诊断完成")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"BugX {get_full_version()}")
        for name, ok, detail in checks:
            optional_miss = (not ok) and name not in installation_required
            marker = "✓" if ok else ("△" if optional_miss else "!")
            suffix = "（可选增强缺失，不影响退出码）" if optional_miss and not strict else ""
            print(f"  {marker} {name}: {detail}{suffix}")
        if getattr(args, "ai", False) or not payload["ready"] or (strict and optional_degraded):
            _render_ai_diagnostics_panel(ai_diag, is_explicit_ai=bool(getattr(args, "ai", False)))
    return 0 if payload["ready"] else 1
