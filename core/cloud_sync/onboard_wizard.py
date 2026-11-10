#!/usr/bin/env python3
"""cloud_sync/onboard_wizard.py - 云端首启向导、登录/设备登记、交互菜单与身份门禁 (DOC-072)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from core.cloud_sync.cloud_onboard import (
    CloudOnboardError,
    CloudOnboardDecisionRequired,
    get_account_id,
    committed_identity_path,
    get_imports,
    load_committed_identity,
    load_project_binding_info,
    derive_project_key,
    save_committed_identity,
    validate_project_key,
    get_state,
    set_state,
    sync_layers,
)
from core.cloud_sync.sync_push import replay_local_pending_outbox

try:
    from core.runtime import interaction
except ImportError:
    interaction = None  # type: ignore[assignment]


IDENTITY_UNCOMMITTED_WARNING = (
    "项目身份文件未提交到 git，多设备同步将产生重复项目；请提交 .bugx-project.json"
)


def warn_uncommitted_identity(root: Path, *, output=None) -> bool:
    """git 仓库中 ``.bugx-project.json`` 未被跟踪时，输出一次强提示（DOC-031 P0）。

    身份文件是跨设备对上同一云端项目的唯一稳定锚点。文件存在但未提交时，
    换机/克隆会落入「身份缺失」路径并可能注册出重复项目，必须在同步开始前
    明确提醒。检测只读：非 git 仓库、git 不可用或探测失败一律跳过，绝不因
    提示逻辑阻断同步。
    """
    if not committed_identity_path(root).is_file():
        return False
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".bugx-project.json"],
            cwd=str(root), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    stderr = result.stderr or ""
    if result.returncode == 0:
        return False
    if result.returncode == 128 or "fatal" in stderr.lower():
        # 非 git 仓库（fatal: not a git repository）或 git 自身错误：跳过检测。
        return False
    if output is None:
        def output(text: str) -> None:
            print(text, file=sys.stderr)
    output(f"[cloud] 警告: {IDENTITY_UNCOMMITTED_WARNING}")
    return True


def ensure_project_identity(root: Path, requested_slug: str | None = None, *,
                             create_if_missing: bool = True) -> dict[str, Any]:
    """Load or create repository identity before any project request.

    The key is persisted before the network call, so an interrupted or failed
    registration retries with the same identity instead of creating another
    candidate. A v1 slug remains available as a server-side migration hint.

    ``create_if_missing=False`` 用于同步链路：身份文件缺失且无运行态绑定时
    不再静默生成新 key（DOC-030 §5.4/§11.3、DOC-031 P0——静默生成会在云端
    注册出重复项目），改为返回 ``needs_decision=True``，由调用方通过交互
    组件拿到用户显式决策。显式命令路径（如 ``bugx cloud projects add``）
    保持默认 ``create_if_missing=True``，用户意图已经明确。
    """
    binding = load_project_binding_info(root)
    committed = load_committed_identity(root)
    binding_key = binding.get("project_key", "")
    committed_key = committed.get("project_key", "")
    if binding_key and committed_key and binding_key != committed_key:
        raise CloudOnboardError("本地运行态与仓库 project_key 不一致，拒绝跨项目同步")
    # A committed identity or local binding is the cross-device source of
    # truth.  The caller's slug is only a bootstrap hint for a brand-new
    # project; allowing it to override an existing identity can register the
    # same stable project_key under a different slug and trigger a remote 409.
    slug = (binding.get("slug") or committed.get("slug") or
            requested_slug or root.name).strip()
    if not slug:
        raise CloudOnboardError("项目 slug 不能为空")
    if not (binding_key or committed_key) and not create_if_missing:
        return {"needs_decision": True, "project_key": "", "slug": slug,
                "project_id": binding.get("project_id", ""),
                "account_id": binding.get("account_id", "")}
    key = binding_key or committed_key or derive_project_key(root)
    validate_project_key(key)
    if committed_key != key or not committed or committed.get("slug") != slug:
        save_committed_identity(root, slug, key)
        if not committed:
            print("[cloud] ✓ 已自动生成确定性项目身份文件 .bugx-project.json（便于多设备协同识别，建议随 Git 提交）", file=sys.stderr)
    return {"project_key": key, "slug": slug,
            "project_id": binding.get("project_id", ""),
            "account_id": binding.get("account_id", "")}


def _tty() -> bool:
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return False
    if os.environ.get("BUGX_NONINTERACTIVE", "") == "1" or os.environ.get("BUGX_NON_INTERACTIVE") or os.environ.get("BUGX_HEADLESS") or os.environ.get("CI"):
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (OSError, AttributeError):
        return False


def _interaction_environment(root: Path) -> Any:
    """探测当前交互通道；TTY 优先用方向键控件，headless 保持 blocked。"""
    try:
        from core.runtime import startup_environment
        is_tty = bool(_tty())
        environment = startup_environment.detect(
            root, stdin_tty=is_tty, stdout_tty=is_tty,
        )
        if hasattr(environment, "as_dict"):
            environment = environment.as_dict()
        else:
            environment = dict(environment)
        if is_tty:
            environment["interaction"] = "tty-radio"
        else:
            environment["interaction"] = "blocked"
    except (ImportError, OSError, ValueError, RuntimeError):
        environment = {
            "host": "unknown", "interface": "tty" if _tty() else "headless",
            "interaction": "tty-radio" if _tty() else "blocked",
        }
    return environment


def _ask_menu(root: Path, *, auth_state: str = "valid") -> str:
    """用统一的「运行模式」菜单询问云端同步策略。"""
    state = auth_state if auth_state in {"valid", "expired", "missing"} else "missing"
    if state == "valid":
        title = "检测到有效云端登录态，请选择运行模式："
        sync_label = "云端同步"
    elif state == "expired":
        title = "检测到云端登录态已过期，请选择运行模式："
        sync_label = "重新登录并同步"
    else:
        title = "未检测到有效云端登录态，请选择运行模式："
        sync_label = "立即登录并同步"
    if interaction is None:
        raise CloudOnboardDecisionRequired("统一交互组件不可用")
    environment = _interaction_environment(root)
    result = interaction.ask(
        interaction.InteractionQuestion(
            question_id="cloud_sync",
            phase="startup",
            kind="select",
            title=title,
            question="请选择本项目的云端多设备同步模式",
            options=(
                interaction.InteractionOption(
                    "sync", sync_label, "完成 device-code 登录后同步当前项目", "cloud_sync",
                ),
                interaction.InteractionOption(
                    "local", "本地模式", "持久化本地运行，不自动同步", "set_cloud_local",
                ),
                interaction.InteractionOption(
                    "pending", "下次再说", "保持待决定，当前启动停止", "stop_startup",
                ),
            ),
            default=2,
        ),
        environment,
    )
    if result.status == interaction.ANSWERED and isinstance(result.value, str):
        return result.value
    raise CloudOnboardDecisionRequired(
        "本项目需要完成云端同步模式选择；当前问题未得到有效答案",
        request_emitted=result.status == interaction.NEEDS_DECISION,
    )


def ask_project_identity_decision(root: Path) -> str:
    """身份文件缺失时的自愈处理（废弃假性交互弹窗，开箱即用自动创建）。

    历史版本中曾因担忧随机 key 冲突而弹窗询问用户（DOC-031），现已全面切换至
    确定性 key 推导算法（derive_project_key）。同一仓库无论在何处克隆均幂等推导
    出相同 project_key，因此直接返回 'create' 就地自愈，不再弹窗阻断自动化流程。
    """
    return "create"


def _is_auth_failure(summary: object) -> bool:
    """识别服务端拒绝认证，令 onboarding 回到统一过期菜单。"""
    try:
        text = json.dumps(summary, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        text = str(summary).lower()
    return any(marker in text for marker in (
        "401", "unauthorized", "unauthenticated", "token_expired", "expired_token",
        "登录态已过期", "凭据过期",
    ))


def _is_transient_sync_failure(summary: object) -> bool:
    try:
        text = json.dumps(summary, ensure_ascii=False).upper()
    except (TypeError, ValueError):
        text = str(summary).upper()
    return any(code in text for code in (
        "SYNC_NETWORK_UNAVAILABLE", "SYNC_SERVER_UNAVAILABLE",
        "SYNC_MAINTENANCE", "SYNC_RATE_LIMITED",
    ))


def _login_and_register(client: Any, ns: dict[str, Any], *, reason: str = "") -> None:
    """Run the real device-code login before allowing sync to continue.

    A credential file can exist while its opaque token has already expired.
    This helper is used both for an initially missing credential and for a
    server-side 401.  ``CloudClient.login`` prints and opens the verification
    URI, then blocks until the user completes authorization or the device code
    expires.  No caller may mark onboarding complete before this succeeds.
    """
    if reason:
        print(f"  （{reason}，正在打开浏览器授权页…）", flush=True)
    login_result = client.login(name=ns["get_device_label"]())
    login_ok = getattr(login_result, "ok", None)
    if login_ok is not True:
        raise CloudOnboardError(
            getattr(login_result, "error", None) or "授权未完成"
        )
    from core.cloud_sync.cloud_client import get_device_label as _device_label
    device_result = client.register_device(name=_device_label())
    device_ok = getattr(device_result, "ok", None)
    if device_ok is not True:
        raise CloudOnboardError(
            getattr(device_result, "error", None) or
            f"设备登记失败 code={getattr(device_result, 'status', '')}"
        )


def _register_existing_device(client: Any) -> None:
    """Refresh this device's server registration for every enabled sync run."""
    register = getattr(client, "register_device", None)
    if not callable(register):
        # Lightweight protocol test doubles and older host adapters may not
        # expose device registration; the real CloudClient always does.
        return
    device_result = register(name="bugx-cli")
    if getattr(device_result, "ok", True) is False:
        raise CloudOnboardError(
            getattr(device_result, "error", None) or
            f"设备登记失败 code={getattr(device_result, 'status', '')}"
        )


def maybe_onboard(root: Path, client: Any = None, *,
                  require_interaction: bool = False,
                  perform_sync: bool = True,
                  decision: str | None = None,
                  allow_identity_create: bool = False,
                  _reauth_attempted: bool = False,
                  _device_registered: bool = False) -> bool:
    """项目启动时处理 onboarding，返回本次是否成功完成同步。

    ``require_interaction`` 用于 Agent 的真实启动路径：旧项目没有 onboarding
    状态时，非交互进程必须明确告知调用方并停止，不能静默绕过同步/登录决策。
    ``done`` 表示用户已开启云端同步，不表示后续启动可以跳过同步；每次启动都要
    使用当前登录态执行一次 push/pull。调用方可传 ``perform_sync=False`` 只记录意图，
    但正式启动路径必须使用 ``perform_sync=True``，让项目身份和经验在 lesson context
    构建前可用；授权未完成时不得把 onboarding 标成完成或进入下一启动 gate。网络同步
    失败可记录 ``sync_pending`` 而不阻断本地工作。

    ``decision`` 是已经由自治策略或交互宿主作出的运行模式选择，显式
    提供时不再读取 TTY。``allow_identity_create`` 只供用户明确启动的
    YOLO 模式，允许新项目自动创建稳定 project key；普通同步仍保持
    身份缺失必须询问的安全门禁。
    """
    if decision not in {None, "sync", "local", "pending"}:
        raise ValueError("cloud onboarding decision 无效")
    state = get_state(root)
    if state == "dismissed":
        # 旧版本把“下次再说”错误持久化为 dismissed，导致后续启动静默跳过
        # 云端同步门禁。它不是用户确认的 local，必须迁移回 pending。
        set_state(root, "pending")
        state = "pending"
    if state == "local":
        # 本地模式：不拉取新知识、不创建项目，仅幂等重放此前已授权的 pending outbox（DOC-010 §4.5）
        try:
            ns_local = get_imports()
            c = client if client is not None else ns_local["CloudClient"]()
            try:
                acc = get_account_id(c)
            except CloudOnboardError:
                acc = ""
            if acc:
                replay_local_pending_outbox(root, c, ns_local, acc)
        except Exception:
            # 保留可恢复证据，不伪装成功；失败保留 outbox 供下次重放
            pass
        return False
    ns = get_imports()
    if client is None:
        client = ns["CloudClient"]()
    auth_state = None
    state_probe = getattr(client.cred, "auth_state", None)
    if callable(state_probe):
        candidate = state_probe()
        if isinstance(candidate, str) and candidate in {"valid", "expired", "missing"}:
            auth_state = candidate
    if auth_state is None:
        auth_state = "valid" if bool(client.cred.is_authenticated()) else "missing"
    authenticated = auth_state == "valid"
    # 自动化关键路径：access token 过期但 refresh_token 在手时，先静默续期；
    # 续期成功即视为已认证，续期失败才回落到交互菜单——auto 模式不应因
    # 例行 token 轮换被打断要求人工重登。
    if not authenticated and auth_state == "expired"             and getattr(client.cred, "refresh_token", None):
        try:
            if client._refresh():
                authenticated = True
                auth_state = "valid"
        except Exception:
            pass
    # A persisted sync preference is an automatic startup action when the
    # credential is usable. It must work in headless/TUI-less hosts too.
    # ``done`` 与 ``sync`` 都表示「用户已确认要云端同步」：
    #   * ``done``：已完成一次 onboarding（历史词）；
    #   * ``sync``：``bugx init --cloud-sync sync`` 显式选择同步。
    # 此前只认 ``done``，于是「用户显式选 sync」被当成「决策尚未完成」——
    # ``pre`` 直接 BLOCKED「云端多设备同步决策尚未完成」，用户还必须再手动跑一次
    # onboard 才算数（build 288 实测：init --cloud-sync sync 后 pre --autonomous
    # 仍 BLOCKED，且不生成 cloud-project.json 绑定）。两个状态词必须同权。
    automatic_sync = state in {"done", "sync"} and authenticated
    if decision is not None:
        choice = decision
    elif automatic_sync:
        choice = "sync"
    else:
        if not _tty() and not require_interaction:
            return False
        resolved_state = auth_state or ("valid" if authenticated else "missing")
        choice = _ask_menu(root, auth_state=resolved_state)
    if choice == "pending":     # 下次再说
        set_state(root, "pending")
        print("  （本次不同步，下次启动再问）")
        return False
    if choice == "local":  # 本地模式
        set_state(root, "local")
        print("  （已选择本地模式；本项目后续启动不自动同步，需显式执行 bugx cloud sync）")
        return False
    # choice == "sync"
    if not authenticated and not require_interaction and not _tty():
        raise CloudOnboardDecisionRequired(
            "自治云端同步需要已有有效登录态；不能在无交互宿主中自动打开登录流程"
        )
    logged_in = False
    if not authenticated:
        try:
            print("  （未登录，先完成登录授权…）")
            _login_and_register(client, ns)
            logged_in = True
        except (CloudOnboardError, RuntimeError) as exc:
            print(f"  （登录未完成：{exc}，当前启动停止）")
            # ``done`` only means the cloud-sync preference was accepted.  It
            # must not survive a failed authentication attempt: otherwise a
            # stale locally-available token can make the next startup treat
            # the gate as complete and skip the login question entirely.
            set_state(root, "pending")
            return False
    if not perform_sync:
        set_state(root, "done")
        print("  （已记录云端同步意图；正式知识同步将在 Session 提交后的工具准备阶段执行）")
        return True
    try:
        # Existing credentials can be valid while this device has never been
        # registered (or its registration was revoked).  Registration is a
        # deterministic sync prerequisite, not a login-only side effect.
        if not logged_in and not _device_registered:
            _register_existing_device(client)
        summary = sync_layers(
            client, root, Path(root).name,
            allow_identity_create=allow_identity_create,
        )
        if summary.get("ok") is not True:
            if _is_auth_failure(summary) and not _reauth_attempted:
                print(
                    "  （同步被服务端拒绝，当前登录态无效；尚未进入下一阶段，"
                    "正在重新授权…）"
                )
                try:
                    _login_and_register(
                        client, ns,
                        reason="服务端拒绝当前登录态",
                    )
                except (CloudOnboardError, RuntimeError) as exc:
                    print(f"  （重新授权未完成：{exc}，当前启动停止）")
                    # Authentication failure is a gate failure, regardless
                    # of the previous preference.  Force the next startup to
                    # ask again instead of silently inheriting ``done``.
                    set_state(root, "pending")
                    return False
                return maybe_onboard(
                    root, client,
                    require_interaction=require_interaction,
                    perform_sync=perform_sync,
                    decision=decision,
                    allow_identity_create=allow_identity_create,
                    _reauth_attempted=True,
                    _device_registered=True,
                )
            if _is_transient_sync_failure(summary):
                set_state(root, "done")
                print("  （云端暂不可达；已保留同步队列，本地自动挖掘继续）")
                return False
            # 失败时**不得抹掉用户的显式选择**：``sync`` 是用户通过
            # `init --cloud-sync sync` 做出的决策，把它改回 ``pending`` 会让下游门禁
            # 认定「用户还没选」，从而给出误导的「决策尚未完成」指引
            # （build 288 实测：用户已选 sync，却被要求去完成选择）。
            # 只有本来就没决定（pending）时才保持 pending；sync 保持不变以保留意图。
            if state not in {"done", "sync"}:
                set_state(root, "pending")
            detail = summary.get("error") or summary.get("project") or "未知错误"
            print(f"  （同步未完成：{detail}，当前启动停止）")
            # 把真实原因落到可机读位置：调用方的门禁文案此前只会说「云端多设备同步
            # 决策尚未完成」，把「已授权但被隐私门禁拒绝」误报成「用户还没选」——
            # 用户被要求去做他已经做过的事（build 288 实测 data_sharing_revoked）。
            try:
                import json as _json
                (Path(root) / ".bugx" / "cloud-last-error.json").write_text(
                    _json.dumps({"error": detail, "at": time.time()}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except (OSError, ValueError):
                pass
            return False
        set_state(root, "done")
        for k, v in summary.items():
            print(f"  [cloud] {k}: {v}")
        print("  （本项目本次启动已完成云端同步）")
        return True
    except CloudOnboardDecisionRequired:
        # 结构化宿主交互必须回到启动层；吞掉它会把“项目身份待决策”
        # 误报成可本地降级，最终既不创建项目也不使用云端经验。
        raise
    except Exception as exc:  # noqa: BLE001
        if _is_transient_sync_failure(exc):
            set_state(root, "done")
            print("  （云端暂不可达；已保留同步队列，本地自动挖掘继续）")
            return False
        if state != "done":
            set_state(root, "pending")
        print(f"  （同步失败：{exc}，当前启动停止）")
        if _is_auth_failure(exc) and not _reauth_attempted:
            print(
                "  （同步被服务端拒绝，当前登录态无效；尚未进入下一阶段，"
                "正在重新授权…）"
            )
            try:
                _login_and_register(
                    client, ns,
                    reason="服务端拒绝当前登录态",
                )
            except (CloudOnboardError, RuntimeError) as auth_exc:
                print(f"  （重新授权未完成：{auth_exc}，当前启动停止）")
                # Do not preserve ``done`` after a failed reauthentication;
                # the next startup must remain at the cloud gate.
                set_state(root, "pending")
                return False
            return maybe_onboard(
                root, client,
                require_interaction=require_interaction,
                perform_sync=perform_sync,
                decision=decision,
                allow_identity_create=allow_identity_create,
                _reauth_attempted=True,
                _device_registered=True,
            )
        return False

_ensure_project_identity = ensure_project_identity
