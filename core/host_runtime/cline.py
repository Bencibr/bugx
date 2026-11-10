#!/usr/bin/env python3
"""core/host_runtime/cline.py - Cline CLI 进程执行器（DOC-239 S2 续）。

**实现体**，不是声明点：事实（沙箱目录名、沙箱同步清单、provider 标签、MCP 配置
文件名、宿主名）全部来自 ``core/hosts/cline.py``；本模块只负责把这些事实落到
真实子进程上：

1. claim 级 ``.cline_data`` 沙箱建在 artifact_root 下、**仓库外**，权限 0o700；
2. 同步宿主已认证的 provider/模型/首选项文件（实体拷贝，0o600，禁软链接）；
3. 写 worker 专用 MCP 配置（``bugx-supervisor-worker`` + ``bugx-docs``）；
4. 解析 effective (provider, model) 并把沙箱 ``providers.json`` 就地自愈对齐；
5. 以 ``--auto-approve --data-dir -c --timeout`` 启动 Cline，启动前先
   ``supervisor.start_worker(process_pid=...)``，让孤儿租约回收有真实 PID。

原实现位于 ``core/worker_runtime.py``；迁移保证逐字等价（含错误文案），
只是把「宿主事实」换成了插件读取。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import core.hosts as host_plugins

from .common import (
    WorkerRuntimeError,
    claim_checkpoint_id,
    ensure_workspace_gitignores_bugx,
    terminate_process_group,
)

__all__ = [
    "PROVIDER_KEY",
    "SANDBOX_DIR",
    "SANDBOX_SYNC_FILES",
    "clean_legacy_workspace_sandbox",
    "execute_worker",
    "prepare_worker_sandbox",
    "resolve_model_and_provider",
    "resolve_sandbox_data_dir",
]

logger = logging.getLogger(__name__)

#: 宿主名由模块名派生（``core/host_runtime/<host>.py``），实现体不写字面量。
_HOST_NAME = __name__.rsplit(".", 1)[-1]
_PLUGIN = host_plugins.get(_HOST_NAME)
if _PLUGIN is None:  # pragma: no cover - 插件被删除即导入期 fail-fast
    raise RuntimeError(f"{__name__} 需要 core/hosts/{_HOST_NAME}.py 声明；插件缺失")

_NAME = _PLUGIN.name
#: 沙箱内数据目录名（``artifact_root/<sandbox_dir>``）。
SANDBOX_DIR = _PLUGIN.worker_runtime.sandbox_dir
#: 必须同步的宿主认证/模型文件（唯一声明点是插件）。
SANDBOX_SYNC_FILES: tuple[str, ...] = tuple(_PLUGIN.worker_runtime.sandbox_sync)
#: Supervisor ``provider=`` 标签。
PROVIDER_KEY = _PLUGIN.worker_runtime.provider_key or _NAME
#: 沙箱内 MCP 配置文件名（来自插件声明的全局作用域路径，末段即文件名）。
MCP_SETTINGS_NAME = Path(_PLUGIN.mcp_write.global_scope or "").name or "cline_mcp_settings.json"

_SANDBOX_DIR_ENV = f"BUGX_{_NAME.upper()}_SANDBOX_DIR"
_SOURCE_DATA_ENV = f"{_NAME.upper()}_SOURCE_DATA_DIR"
_PROVIDER_ENVS = (f"BUGX_{_NAME.upper()}_PROVIDER", f"{_NAME.upper()}_PROVIDER")
_MODEL_ENVS = (f"BUGX_{_NAME.upper()}_MODEL", f"{_NAME.upper()}_MODEL")
_DATA_DIR_ENV = f"{_NAME.upper()}_DATA_DIR"
#: 凭据存在性探测（含第三方 provider 的通用 key；与宿主无关）。
_CREDENTIAL_ENVS = (*_PROVIDER_ENVS, f"{_NAME.upper()}_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def _first_env(names: tuple[str, ...]) -> str | None:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def clean_legacy_workspace_sandbox(workspace_root: Path) -> list[Path]:
    """Scan and remove legacy sandbox directories from workspace_root to prevent credential leaks."""

    cleaned: list[Path] = []
    bugx_dir = (workspace_root / ".bugx").resolve()
    if bugx_dir.is_dir():
        try:
            for path in list(bugx_dir.rglob(SANDBOX_DIR)):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                    if not path.exists():
                        cleaned.append(path)
                    else:
                        logger.warning("未能彻底删除遗留 %s: %s", SANDBOX_DIR, path)
        except OSError as exc:
            logger.warning("清理遗留 %s 失败: %s", SANDBOX_DIR, exc)
    return cleaned


def resolve_sandbox_data_dir(
    workspace_root: Path,
    artifact_dir: Path,
    *,
    sandbox_base_dir: Path | None = None,
) -> Path:
    """Resolve an isolated host data sandbox directory outside the target repository workspace."""

    base: Path | None = sandbox_base_dir
    if base is None and os.environ.get(_SANDBOX_DIR_ENV):
        base = Path(os.environ[_SANDBOX_DIR_ENV]).expanduser()
    if base is None:
        home_base = Path.home() / ".bugx" / "sandboxes" / _NAME
        try:
            home_base.mkdir(parents=True, exist_ok=True)
            test_probe = home_base / ".probe_write"
            test_probe.touch()
            test_probe.unlink(missing_ok=True)
            base = home_base
        except (OSError, PermissionError):
            base = Path(tempfile.gettempdir()) / "bugx-sandboxes" / _NAME

    ws_hash = hashlib.sha256(str(workspace_root.resolve()).encode("utf-8")).hexdigest()[:12]
    try:
        rel_claim = artifact_dir.resolve().relative_to(workspace_root.resolve())
    except ValueError:
        rel_claim = Path(artifact_dir.name)
    return (base / ws_hash / rel_claim / SANDBOX_DIR).resolve()


def _mcp_payload(workspace_root: Path) -> dict[str, Any]:
    """Worker 专用 MCP 配置：只暴露 supervisor(worker 角色) 与只读 docs。"""

    cli = shutil.which("bugx") or "bugx"
    return {
        "mcpServers": {
            "bugx-supervisor-worker": {
                "type": "stdio",
                "command": cli,
                "args": [
                    "mcp", "serve", "--root", str(workspace_root), "--role", "worker",
                ],
                "disabled": False,
                "autoApprove": [],
            },
            "bugx-docs": {
                "type": "stdio",
                "command": cli,
                "args": [
                    "mcp", "docs", "serve", "--root", str(workspace_root),
                    "--db", str(workspace_root / ".bugx" / "knowledge" / "docs.sqlite3"),
                ],
                "disabled": False,
                "autoApprove": [],
            },
        },
    }


def prepare_worker_sandbox(
    workspace_root: Path,
    artifact_dir: Path,
    *,
    source_data_dir: Path | None = None,
    sandbox_base_dir: Path | None = None,
) -> Path:
    """Create the claim-local data dir outside target repo, sync credentials, write worker MCP config."""

    workspace_root = workspace_root.resolve()
    artifact_dir = artifact_dir.resolve()
    if artifact_dir == workspace_root or workspace_root not in artifact_dir.parents:
        raise WorkerRuntimeError(f"Cline artifact_root 越界: {artifact_dir}")

    ensure_workspace_gitignores_bugx(workspace_root)
    clean_legacy_workspace_sandbox(workspace_root)

    data_dir = resolve_sandbox_data_dir(
        workspace_root, artifact_dir, sandbox_base_dir=sandbox_base_dir
    )
    settings_dir = data_dir / "settings"
    if data_dir.is_symlink() or settings_dir.is_symlink():
        raise WorkerRuntimeError(
            f"Cline artifact_root 内的 {SANDBOX_DIR}/settings 不得是符号链接"
        )
    settings_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o700)
    settings_dir.chmod(0o700)

    # 安全同步宿主已认证的 provider、模型与首选项白名单文件，严格禁止软链接
    source_base = (
        source_data_dir
        or (
            Path(os.environ[_SOURCE_DATA_ENV]).expanduser()
            if os.environ.get(_SOURCE_DATA_ENV)
            else None
        )
        or (Path.home() / f".{_NAME}" / "data")
    )
    if source_base.is_dir():
        for rel_path in SANDBOX_SYNC_FILES:
            src_file = source_base / rel_path
            if src_file.is_file():
                dest_file = data_dir / rel_path
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                dest_file.parent.chmod(0o700)
                try:
                    # 实体拷贝内容（不保留外部所有者，解析链接目标后写入实体），权限收紧至 0o600
                    shutil.copy2(src_file.resolve(), dest_file, follow_symlinks=True)
                    dest_file.chmod(0o600)
                except OSError as exc:
                    logger.warning("Cline 沙箱凭据同步失败 %s -> %s: %s", src_file, dest_file, exc)

    settings_path = settings_dir / MCP_SETTINGS_NAME
    if settings_path.is_symlink():
        raise WorkerRuntimeError("Cline MCP 配置不得是符号链接")
    settings_path.write_text(
        json.dumps(_mcp_payload(workspace_root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    settings_path.chmod(0o600)
    return data_dir


def resolve_model_and_provider(
    data_dir: Path,
    *,
    requested_model: str | None = None,
    requested_provider: str | None = None,
    excluded_providers: set[str] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve effective (provider, model) for Cline CLI and align sandbox state."""

    settings_dir = data_dir / "settings"
    providers_path = settings_dir / "providers.json"
    models_path = settings_dir / "models.json"

    providers_data: dict[str, Any] = {}
    if providers_path.is_file():
        try:
            providers_data = json.loads(providers_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    models_data: dict[str, Any] = {}
    if models_path.is_file():
        try:
            models_data = json.loads(models_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    prov_map: dict[str, Any] = providers_data.get("providers", {}) if isinstance(providers_data, dict) else {}

    env_provider = _first_env(_PROVIDER_ENVS) or None
    env_model = _first_env(_MODEL_ENVS) or None

    final_provider = str(requested_provider or "").strip() or env_provider or None
    final_model = str(requested_model or "").strip() or env_model or None

    if not final_provider or not final_model:
        # DOC-243: 使用统一的五级通道阶梯裁决引擎（Free > Subscription > Direct Key > Router > Credits）
        try:
            from core.host_runtime.host_provider_arbitrator import arbitrate_cline_provider
        except ImportError:  # pragma: no cover
            from core.host_provider_arbitrator import arbitrate_cline_provider

        arb_provider, arb_model = arbitrate_cline_provider(
            providers_data,
            models_data,
            requested_provider=final_provider,
            requested_model=final_model,
            excluded_providers=excluded_providers,
            data_dir=data_dir,
        )
        if arb_provider and not final_provider:
            final_provider = arb_provider
        if arb_model and not final_model:
            final_model = arb_model

    # 对齐沙箱内的 providers.json（就地自愈，确保 CLI 参数与沙箱全局状态一致）
    if providers_data and final_provider:
        modified = False
        if providers_data.get("lastUsedProvider") != final_provider:
            providers_data["lastUsedProvider"] = final_provider
            modified = True
        if final_model and final_provider in prov_map and isinstance(prov_map[final_provider], dict):
            st = prov_map[final_provider].setdefault("settings", {})
            if st.get("model") != final_model:
                st["model"] = final_model
                modified = True
        if modified:
            try:
                providers_path.write_text(
                    json.dumps(providers_data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                providers_path.chmod(0o600)
            except OSError as exc:
                logger.warning("更新 Cline providers.json 失败: %s", exc)

    return final_provider, final_model


def execute_worker(
    workspace_root: Path,
    claim: Any,
    task_spec: str,
    *,
    supervisor: Any,
    stage_name: str,
    executable: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Run one pre-claimed unit in a real, claim-local Cline CLI process."""

    from core.supervisor.worker_runtime import resolve_worker_timeout

    failure = f"{_NAME}_worker_failed"
    ws_root = workspace_root.resolve()
    worker_id = getattr(claim, "worker_id", str(claim))
    execution_id = getattr(claim, "execution_id", f"exec-{worker_id}")
    artifact_root_rel = getattr(claim, "artifact_root", "")
    if not artifact_root_rel:
        raise WorkerRuntimeError(f"{failure}: claim 缺少 artifact_root")
    artifact_dir = (ws_root / artifact_root_rel).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    data_dir = prepare_worker_sandbox(ws_root, artifact_dir)
    eff_provider, eff_model = resolve_model_and_provider(
        data_dir,
        requested_model=model,
        requested_provider=provider,
    )
    providers_file = data_dir / "settings" / "providers.json"
    has_providers_config = providers_file.is_file() and providers_file.stat().st_size > 0
    has_env_credentials = any(bool((os.environ.get(k) or "").strip()) for k in _CREDENTIAL_ENVS)
    if (not has_providers_config or not eff_provider) and not has_env_credentials:
        raise WorkerRuntimeError(
            f"{failure}: 未检测到任何可用的 Cline Provider 配置或凭据 "
            "(缺少 providers.json 且未设置凭据环境变量)"
        )
    timeout_seconds = resolve_worker_timeout(
        model_tier={
            "exploit": "tier_2_exploit",
            "verify": "tier_3_verify",
        }.get(str(stage_name).split("-")[-1], "tier_1_recon"),
        attempt=int(getattr(claim, "attempt", 1) or 1),
    )
    host_executable = _PLUGIN.identity.executable
    executable = executable or shutil.which(host_executable) or host_executable
    command = [
        executable,
        "--auto-approve", "true",
        "--data-dir", str(data_dir),
        "-c", str(ws_root),
        "--timeout", str(int(math.ceil(timeout_seconds))),
    ]
    if eff_provider:
        command.extend(["-P", eff_provider])
    if eff_model:
        command.extend(["-m", eff_model])
    command.append(task_spec)
    environment = os.environ.copy()
    environment[_DATA_DIR_ENV] = str(data_dir)
    popen_kwargs: dict[str, Any] = {
        "cwd": str(ws_root),
        "env": environment,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "posix":
        popen_kwargs["preexec_fn"] = os.setsid
    elif os.name == "nt":  # pragma: no cover - exercised on Windows hosts
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    process = subprocess.Popen(command, **popen_kwargs)
    try:
        supervisor.start_worker(
            worker_id,
            provider=PROVIDER_KEY,
            execution_id=execution_id,
            process_pid=process.pid,
        )
    except Exception:
        terminate_process_group(process)
        try:
            process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            terminate_process_group(process, force=True)
        raise

    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        terminate_process_group(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            terminate_process_group(process, force=True)
            stdout, stderr = "", ""
        checkpoint_id = claim_checkpoint_id(supervisor, worker_id, execution_id)
        if checkpoint_id is None:
            raise WorkerRuntimeError(
                f"{failure}: timeout after {timeout_seconds:.1f}s"
            ) from exc
        return {
            "worker_id": worker_id,
            "checkpoint_id": checkpoint_id,
            "status": "checkpointed",
            "exit_code": process.returncode,
            "stdout": (stdout or "")[-2000:],
            "stderr": (stderr or "")[-2000:],
        }

    checkpoint_id = claim_checkpoint_id(supervisor, worker_id, execution_id)
    if checkpoint_id is None:
        detail = (stderr or stdout or "").strip()[-500:]
        raise WorkerRuntimeError(
            f"{failure}: process exited without a valid Checkpoint"
            + (f": {detail}" if detail else "")
        )
    return {
        "worker_id": worker_id,
        "checkpoint_id": checkpoint_id,
        "status": "checkpointed",
        "exit_code": process.returncode,
        "stdout": (stdout or "")[-2000:],
        "stderr": (stderr or "")[-2000:],
    }

