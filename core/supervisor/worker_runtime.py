#!/usr/bin/env python3
"""core/worker_runtime.py — 无人值守自主 Worker 执行主体 (DOC-137 / B-1)。

在无宿主交互的纯自治持续挖掘模式下，执行从 Supervisor 领取的 Worker 任务：
1. 调用 Supervisor 启动确认（worker-start）；
2. 构造并调用 model_runner（若配置真实 provider）或本地自愈审计驱动器；
3. 将产物安全受限写入 worker 的 artifact_root；
4. 校验并组装 passed evidence 与 coverage，调用 supervisor.submit_checkpoint；
5. 闭环推进 Supervisor 覆盖状态，消除空转等待与卡死。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import math
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence
import sys

_CURRENT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _CURRENT_DIR.parent
_REPO_ROOT = _CORE_DIR.parent
for _p in (str(_REPO_ROOT), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.supervisor import audit_supervisor
from core.host_runtime import host_registry
from core.host_runtime.common import WorkerRuntimeError

from core.supervisor.checkpoint_pipeline import UNVERIFIED_EXECUTION


def _host_runner(
    *,
    host: str | None,
    execute: bool,
    runtime: Mapping[str, Any] | None,
) -> tuple[str, str, Any] | None:
    """Resolve «宿主 CLI 进程执行器» from plugin facts。

    返回 ``(宿主名, 失败前缀, 执行器模块)``；``None`` 表示本会话不走宿主进程模式。

    宿主事实（模块名、失败前缀）只来自 ``core/hosts/<host>.py``：本函数不再认识
    任何宿主名，新增宿主只需在插件里声明 ``worker_runtime.runner_module``。
    """

    context = runtime or {}
    selected_host = str(host or context.get("host") or "").strip().casefold()
    selected_execute = execute or bool(context.get("execute"))
    if not selected_host or not selected_execute:
        return None
    plugin = host_registry.host_plugin(selected_host)
    if plugin is None or not plugin.worker_runtime.runner_module:
        return None
    module = importlib.import_module(plugin.worker_runtime.runner_module)
    return plugin.name, f"{plugin.name}_worker_failed", module


def _runtime_context(workspace_root: Path) -> dict[str, Any]:
    """Read the current project's persisted dispatch context, if available."""
    candidates: list[Path] = []
    try:
        import core.bugx_paths as bugx_paths
        candidates.append(bugx_paths.require(workspace_root).runtime)
    except (ImportError, OSError, ValueError, RuntimeError):
        pass
    candidates.extend([
        workspace_root / ".bugx" / "bugx-runtime.json",
        workspace_root / "bugx-runtime.json",
    ])
    seen: set[Path] = set()
    for path in candidates:
        path = Path(path)
        if path in seen or not path.is_file() or path.is_symlink():
            continue
        seen.add(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}

logger = logging.getLogger(__name__)
# 并行执行时多个 worker 会共享同一个调度器实例（workers() 传入），
# UCB1 计数器的 read-modify-write 在 GIL 下也不是原子操作——用锁把
# 调度历史更新段串起来，避免丢失尝试/回报记账。
_SCHEDULER_UPDATE_LOCK = threading.Lock()


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())[:64] or "unit"


def _finding_argv(command: object, input_value: object = None) -> list[str] | None:
    """Normalize a Finding command for the fail-closed patch verifier.

    Findings may carry a shell-looking string because the differential oracle
    accepts a tokenized template.  Patch verification never executes that
    string through a shell: this helper only performs ``shlex`` tokenization,
    replaces the explicit ``{input}`` token, and returns argv for
    ``subprocess.run(..., shell=False)``.  Structured PoC values cannot be
    represented as an argv file input and are intentionally left unverified.
    """
    if isinstance(command, str):
        try:
            argv = shlex.split(command, posix=True)
        except ValueError:
            return None
    elif isinstance(command, (list, tuple)) and all(isinstance(item, str) for item in command):
        argv = list(command)
    else:
        return None
    if not argv or any(not item for item in argv):
        return None
    if isinstance(input_value, (str, os.PathLike)):
        input_path = str(input_value)
        argv = [item.replace("{input}", input_path) for item in argv]
    elif "{input}" in argv:
        return None
    return argv


def _has_poc_input(value: object) -> bool:
    """Return whether a PoC value satisfies the shared input contract."""
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, (os.PathLike, Mapping, list, tuple))



def _ensure_finding_id(finding: dict) -> str:
    """补齐 finding.id（提交与 triage 都以 id 寻址）；只做标识规范化，不引入新事实。"""
    existing = finding.get("id")
    if isinstance(existing, str) and existing.strip():
        return existing.strip()
    for key in ("fingerprint", "finding_id"):
        value = finding.get(key)
        if isinstance(value, str) and value.strip():
            finding["id"] = value.strip()
            return finding["id"]
    digest = hashlib.sha256(
        json.dumps(finding, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    finding["id"] = f"finding-{digest}"
    return finding["id"]


def _write_evidence_artifact(
    artifact_dir: Path, artifact_root_rel: str, name: str, payload: Any,
) -> tuple[str, str]:
    """把真实证据序列化落到本 worker 的 artifact_root，返回 (相对路径, sha256)。

    落盘即"可被独立重算"：报告期 verify_observation_artifacts 会重算这里声明的
    哈希，事后改动或伪造都会现形——这是确认门槛防自证的关键一环。
    """
    content = json.dumps(payload, ensure_ascii=False, indent=1, default=str).encode("utf-8")
    artifact_path = artifact_dir / _safe_name(name)
    artifact_path.write_bytes(content)
    return f"{artifact_root_rel}/{artifact_path.name}", hashlib.sha256(content).hexdigest()


def resolve_worker_timeout(
    model_tier: str | None = None,
    attempt: int = 1,
    base_override: float | None = None,
) -> float:
    """DOC-205 S1: 根据 model_tier 与重试 attempt 次数自适应计算模型超时时间。

    1. 基准阶梯：recon 180s, exploit 600s, verify 300s；
    2. 环境变量 BUGX_WORKER_TIMEOUT 提供时，作为基准基数（exploit 任务保底 600s）；
    3. 重试退避因子：1.5 ** (attempt - 1)；
    4. 边界钳制：10.0s <= timeout <= 1800.0s。
    """
    tier_defaults = {
        "tier_1_recon": 180.0,
        "tier_2_exploit": 600.0,
        "tier_3_verify": 300.0,
    }
    tier_key = str(model_tier or "tier_1_recon").strip().lower()

    env_timeout_raw = os.environ.get("BUGX_WORKER_TIMEOUT")
    configured_base: float | None = base_override
    if configured_base is None and env_timeout_raw:
        try:
            val = float(env_timeout_raw.strip())
            if val > 0:
                configured_base = val
        except (ValueError, TypeError):
            pass

    if configured_base is not None:
        if tier_key == "tier_2_exploit":
            base = max(configured_base, 600.0)
        else:
            base = configured_base
    else:
        base = tier_defaults.get(tier_key, 300.0)

    safe_attempt = max(1, int(attempt or 1))
    backoff = 1.5 ** (safe_attempt - 1)
    effective = base * backoff
    return max(10.0, min(1800.0, effective))


def _root_cause_anchor_verified(ws_root: Path, finding: dict) -> bool:
    """根因锚点核验：候选声称的 target_code 必须逐字存在于它声称的文件里。

    只核验"声称的代码在场"这一可证伪事实，不做语义判断、不把相关性当因果性；
    与双跑确定性失败、负对照通过合起来，才允许 root_cause_verified=True。
    """
    target_file = finding.get("file")
    target_code = finding.get("target_code")
    if not isinstance(target_file, str) or not isinstance(target_code, str):
        return False
    candidate = (ws_root / target_file).resolve()
    if candidate != ws_root and ws_root not in candidate.parents:
        return False
    try:
        return target_code.strip() in candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _transition_terminal(finding: dict, state: str, reason: str = "",
                         rejection_class: str | None = None) -> None:
    """把 finding 推到 confirmation 终态，并镜像进 finding_lifecycle 账本（P0-C）。

    走选项 a（本进程内直接裁决）而不是把 transition 暴露成 MCP 工具：
    bugx.md / bugx-worker.md 对 **/finding_confirmation.py 是 deny，那是为了挡住
    被审 agent 直接拨动门禁；再开一个 MCP 工具等于把 deny 从后门洗掉。进程内导入
    不受该 deny 约束——model_runner.py / audit_supervisor.py 同样在 deny 名单里，
    worker_runtime 早已在进程内导入它们，本函数的做法与既有先例一致。
    """
    from core.ledger import finding_confirmation
    from core.ledger import finding_lifecycle

    try:
        finding_confirmation.transition(finding, state, reason[:4000], rejection_class)
    except finding_confirmation.ConfirmationError as exc:
        # 终态不可达（协议校验失败）时留痕不崩：至少下轮能看到缺口在哪。
        finding.setdefault("confirmation", {})["transition_error"] = str(exc)
        return
    finding["status"] = "confirmed" if state == "confirmed" else "rejected"
    # 跨轮 reconcile 只认 lifecycle.state：不同步会把已否决的候选从 unverified
    # 复活、重复消耗预算。镜像失败不阻断主流程（confirmation 才是结算真源）。
    # 只有真实差分 FALSE_POSITIVE 才是 false_positive；验证器/证据/环境失败
    # 都必须保留为 unverified，不能把“没能验证”洗成“已证伪”。
    lifecycle_state = "confirmed" if state == "confirmed" else (
        "false_positive"
        if (
            rejection_class == "not_reproducible"
            and finding.get("differential_verdict") == "FALSE_POSITIVE"
        )
        else "unverified"
    )
    try:
        if lifecycle_state == "unverified":
            # finding_lifecycle.triage intentionally rejects derived
            # ``unverified`` as a manual state.  This is an internal mirror of
            # an already-recorded confirmation outcome, not an external triage
            # operation, so keep the distinction explicit here.
            lifecycle = dict(finding.get("lifecycle") or {})
            lifecycle["state"] = lifecycle_state
            lifecycle["triage_note"] = reason[:4000]
            finding["lifecycle"] = lifecycle
        else:
            finding_lifecycle.triage(
                {"findings": [finding]}, str(finding.get("id")),
                lifecycle_state,
                note=reason[:4000],
            )
    except Exception as exc:
        finding.setdefault("lifecycle", {})["triage_error"] = str(exc)


def _settle_finding_confirmation(
    finding: dict,
    diff_res: dict | None,
    negative_res: dict | None,
    *,
    workspace_root: Path,
    artifact_dir: Path,
    artifact_root_rel: str,
    target_fingerprint: str,
    executed_platform: str,
    run_reference: str | None = None,
    gap: dict | None = None,
) -> None:
    """P0-C：用真实差分证据把候选收敛到 confirmation 终态（confirmed/rejected）。

    无人值守路径此前从不写 confirmation 块，于是每条候选永远 pending，
    finding_confirmation.gate() 的 pending>0 让 post 的计命结算硬失败——也就是
    "无人值守跑出一个 Bug"会永久卡死结算。这里只从真实执行取材：
    - oracle/observations：两次独立沙盒双跑的退出码、归一化签名与落盘工件哈希；
    - negative_control：生产者提供的良性输入过同一条命令，双跑均正常退出才算通过；
    - root_cause_verified：target_code 逐字在场 + 负对照通过 + 确定性失败；
    - target_execution：真实执行平台、真实运行引用、真实超时错误。
    十项准则缺一就 transition 到 rejected(insufficient_evidence) 并记下缺口，
    绝不为了"能结算"而臆造字段；``gap`` 模式只描述"尝试过什么"，不声称任何裁决。
    """
    from core.ledger import finding_confirmation

    if gap is not None:
        finding["reproducible"] = False
        finding["confirmation"] = {
            "state": "root_caused",
            "oracle": gap["oracle"],
            "observations": [],
            "root_cause_verified": False,
        }
        _transition_terminal(finding, "rejected", gap["reason"], gap["rejection_class"])
        return

    vuln_id = str(finding.get("id"))
    runs = [
        item for item in (diff_res.get("runs") if isinstance(diff_res, dict) else [])
        if isinstance(item, dict)
    ]
    deterministic = bool(diff_res) and diff_res.get("verdict") == "CONFIRMED_DETERMINISTIC"

    # 证据工件落盘：双跑报告 + 每次沙盒运行的原始记录（可独立重算哈希）
    report_rel, _report_sha = _write_evidence_artifact(
        artifact_dir, artifact_root_rel, f"{vuln_id}_differential_oracle.json", diff_res,
    )
    run_artifacts: dict[int, tuple[str, str]] = {}
    for position, run in enumerate(runs[:2], 1):
        index = run.get("run") if isinstance(run.get("run"), int) else position
        run_artifacts[index] = _write_evidence_artifact(
            artifact_dir, artifact_root_rel, f"{vuln_id}_sandbox_run{index}.json", run,
        )

    # 两条观测 = 两次完全独立的临时沙盒执行（differential_oracle 的真实证据通道）
    observations: list[dict[str, Any]] = []
    for position, run in enumerate(runs[:2], 1):
        index = run.get("run") if isinstance(run.get("run"), int) else position
        failed = bool(run.get("timed_out")) or run.get("exit_code") != 0
        observation: dict[str, Any] = {
            "run_id": f"{vuln_id}-sandbox-{index}",
            "channel": f"differential-oracle-sandbox-{index}",
            "fingerprint": str(run.get("signature") or f"exit-{run.get('exit_code')}"),
            "reference": report_rel,
            "outcome": "failure" if failed else "pass",
        }
        artifact = run_artifacts.get(index)
        if artifact:
            observation["artifact_path"], observation["artifact_sha256"] = artifact
        observations.append(observation)

    # 负对照：同命令 + 良性输入，双跑均正常退出才算"通过"
    negative_control = None
    if isinstance(negative_res, dict):
        negative_rel, negative_sha = _write_evidence_artifact(
            artifact_dir, artifact_root_rel, f"{vuln_id}_negative_control.json", negative_res,
        )
        if negative_res.get("verdict") == "FALSE_POSITIVE":
            negative_control = {
                "outcome": "pass",
                "reference": f"同命令良性输入两次独立沙盒均正常退出（工件: {negative_rel}）",
                "artifact_path": negative_rel,
                "artifact_sha256": negative_sha,
            }

    # 目标执行证明：只填真实发生过的执行事实（平台/引用/超时错误）
    target_execution = None
    if deterministic and target_fingerprint:
        target_execution = {
            "target_platform": executed_platform,
            "executed_platform": executed_platform,
            "target_fingerprint": target_fingerprint,
            # PE-20261005-006：run_reference 必须引用本 run 身份（state.run_id），
            # 使 sync 门禁可判定"报告属于本次运行"；工件路径保留在 evidence。
            "run_reference": run_reference or report_rel,
            "executed": True,
            "simulated": False,
            "outcome": "failure",
            "errors": [
                f"沙盒 Run {run.get('run')} 执行超时" for run in runs[:2] if run.get("timed_out")
            ],
        }

    finding["reproducible"] = deterministic
    finding["reproduction"] = (
        f"{diff_res.get('command_template')}；PoC 输入 sha256={diff_res.get('input_sha256')}；"
        f"双跑报告工件 {report_rel}"
    )[:4000]
    finding["confirmation"] = {
        "state": "root_caused",
        "oracle": {
            "tier": "differential",
            "reference": report_rel,
            "provenance": {
                "kind": "independent-implementation",
                "independent_from_change": True,
                "verified": True,
            },
        },
        "observations": observations,
        "root_cause_verified": bool(
            deterministic
            and negative_control is not None
            and _root_cause_anchor_verified(workspace_root, finding)
        ),
    }
    if negative_control is not None:
        finding["confirmation"]["negative_control"] = negative_control
    if target_execution is not None:
        finding["confirmation"]["target_execution"] = target_execution

    if diff_res.get("verdict") == "INFRA_ERROR":
        _transition_terminal(
            finding, "rejected",
            f"差分验证基础设施失败，未对目标作出裁决: {diff_res.get('summary')}",
            "tool_failure",
        )
        return
    if not deterministic:
        _transition_terminal(
            finding, "rejected",
            f"双跑行为不收敛，无法确认为确定性缺陷: {diff_res.get('summary')}", "flaky",
        )
        return
    issues = finding_confirmation.confirmation_issues(finding)
    if issues:
        _transition_terminal(
            finding, "rejected",
            "confirmation 准则未满足: " + "；".join(issues), "insufficient_evidence",
        )
        return
    _transition_terminal(
        finding, "confirmed",
        "确定性双跑复现一致、负对照通过、根因锚点逐字在场",
    )


def execute_autonomous_worker(
    workspace_root: Path,
    claim: Any,
    task_spec: str,
    stage_name: str = "00-scope",
    provider: str | None = None,
    model: str | None = None,
    executable: str | None = None,
    supervisor: Any | None = None,
    scheduler: Any | None = None,
    host: str | None = None,
    max_workers: int | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    """执行 Worker，并把未处理失败回写到对应 Supervisor claim。"""
    ws_root = workspace_root.resolve()
    manifest_path = ws_root / ".bugx" / "manifest.json"
    worker_id = getattr(claim, "worker_id", "")
    lease_id = getattr(claim, "lease_id", "")
    execution_id = getattr(claim, "execution_id", "")
    if (
        not manifest_path.is_file()
        or any(
            not isinstance(value, str) or not value.strip()
            for value in (worker_id, lease_id, execution_id)
        )
    ):
        raise WorkerRuntimeError(
            "[UNSUPERVISED_WORKER_DISPATCH] "
            "Direct worker invocation without active supervisor session is prohibited"
        )
    runtime = _runtime_context(ws_root)
    sup = supervisor or audit_supervisor.Supervisor(ws_root)
    runner = _host_runner(host=host, execute=execute, runtime=runtime)
    if runner is not None:
        runner_host, failure_reason, runner_module = runner
        try:
            return runner_module.execute_worker(
                workspace_root=workspace_root,
                claim=claim,
                task_spec=task_spec,
                supervisor=sup,
                stage_name=stage_name,
                executable=executable or runtime.get("worker_executable"),
                model=model or runtime.get("model"),
                provider=provider or runtime.get("provider"),
            )
        except Exception as exc:
            try:
                sup.record_worker_failure(
                    worker_id,
                    lease_id=lease_id,
                    execution_id=execution_id,
                    reason=failure_reason,
                )
            except Exception as persist_exc:
                raise WorkerRuntimeError(
                    f"Worker {worker_id} 执行失败且 Supervisor 失败回写失败: "
                    f"{failure_reason}; 回写错误: {persist_exc}"
                ) from exc
            if isinstance(exc, WorkerRuntimeError):
                raise
            raise WorkerRuntimeError(
                f"Worker {worker_id}（宿主 {runner_host}）执行失败: {failure_reason}: {exc}"
            ) from exc
    try:
        return _execute_autonomous_worker(
            workspace_root=ws_root,
            claim=claim,
            task_spec=task_spec,
            stage_name=stage_name,
            provider=provider,
            executable=executable,
            supervisor=sup,
            scheduler=scheduler,
        )
    except Exception as exc:
        failure_reason = str(exc).strip() or type(exc).__name__
        try:
            sup.record_worker_failure(
                worker_id,
                lease_id=lease_id,
                execution_id=execution_id,
                reason=failure_reason,
            )
        except Exception as persist_exc:
            raise WorkerRuntimeError(
                f"Worker {worker_id} 执行失败且 Supervisor 失败回写失败: "
                f"{failure_reason}; 回写错误: {persist_exc}"
            ) from exc
        if isinstance(exc, WorkerRuntimeError):
            raise
        raise WorkerRuntimeError(
            f"Worker {worker_id} 执行失败: {failure_reason}"
        ) from exc


def _execute_autonomous_worker(
    workspace_root: Path,
    claim: Any,
    task_spec: str,
    stage_name: str = "00-scope",
    provider: str | None = None,
    executable: str | None = None,
    supervisor: Any | None = None,
    scheduler: Any | None = None,
) -> dict[str, Any]:
    """执行单个自主 Worker 的完整审计循环并提交 Checkpoint。"""
    ws_root = workspace_root.resolve()
    runtime = _runtime_context(ws_root)
    sup = supervisor or audit_supervisor.Supervisor(ws_root)

    active_provider = provider or runtime.get("provider") or "mock"
    active_executable = executable or runtime.get("worker_executable")
    worker_id = getattr(claim, "worker_id", str(claim))
    lease_id = getattr(claim, "lease_id", "")
    execution_id = getattr(claim, "execution_id", f"exec-{worker_id}")
    unit_ids = list(getattr(claim, "unit_ids", []))
    artifact_root_rel = getattr(claim, "artifact_root", None)
    round_no = getattr(claim, "round_no", 1)

    # 1. 确认 Worker 启动（并发重叠门禁依赖）
    try:
        sup.start_worker(worker_id, provider=active_provider, execution_id=execution_id)
    except Exception as exc:
        raise WorkerRuntimeError(
            f"Worker {worker_id} 启动失败，已中止单元 {', '.join(unit_ids)}: {exc}"
        ) from exc

    # C1：心跳保活。无人值守 worker 的模型调用 + 差分验证 + 补丁合成可
    # 轻松超过 450s 的心跳断流窗口（租约本身 1800s+），期间若无心跳，
    # 同批其他线程的 refresh 会把本 claim 判为僵尸并抢占，最终
    # submit_checkpoint 被拒、整批静默失败。这里在长耗时段前后各保活一次。
    def _heartbeat_keepalive() -> None:
        # lease_seconds 默认 1800 且必须 >0（0 会被 ClaimRejected）。
        # 保活失败绝不阻断审计主流程（降级为无保活）。
        try:
            sup.heartbeat(worker_id)
        except Exception:
            pass

    _heartbeat_keepalive()

    # 2. 准备受控的 artifact 目录
    if not artifact_root_rel:
        artifact_dir, artifact_root_rel = audit_supervisor._worker_artifact_root(
            ws_root, sup.state_path, worker_id, lease_id or uuid.uuid4().hex,
        )
    else:
        artifact_dir = (ws_root / artifact_root_rel).resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)

    state = sup.status()
    findings: list[dict[str, Any]] = []
    coverage_updates: dict[str, str] = {}
    evidence_updates: dict[str, dict[str, Any]] = {}
    notes: dict[str, str] = {}

    # 3. 执行审计逻辑（根据 provider 分流）
    # ``model_invoked`` / ``model_error`` 是第 4 步证据自证的唯一依据：
    # 只有真实模型被调用并返回，才允许写 covered/passed。mock 之类的
    # 非真实 provider 从未调用模型，不构成"已验证"的事实。
    model_invoked = False
    model_error: str | None = None
    if active_provider in {"codex", "claude", "opencode", "command"}:
        try:
            from core.runtime import model_runner

            model_tier = model_runner.resolve_model_tier(stage_name)
            request = {
                "schema_version": 1,
                "task": "audit",
                "stage": stage_name,
                "model_tier": model_tier,
                "worker_id": worker_id,
                "unit_ids": unit_ids,
                "task_spec": task_spec,
                "cases": [{"case_id": u, "files": [u]} for u in unit_ids],
                "output_contract": {
                    "findings": (
                        "list of audit findings; finding 级候选必须自带可裁决的复现原料："
                        "id/title/severity/file/target_code/poc_cmd/poc_input/"
                        "poc_negative_input，否则宿主侧差分验证无法裁决；"
                        "confirmed Finding 若要自动标记 patch_verified，还必须提供 regression_cmd"
                    ),
                },
            }
            claim_attempt = int(getattr(claim, "attempt", 1) or 1)
            timeout_budget = resolve_worker_timeout(model_tier=model_tier, attempt=claim_attempt)
            res = model_runner.run_model(
                request,
                provider=active_provider,
                executable=active_executable,
                workspace=ws_root,
                timeout_seconds=timeout_budget,
            )
            model_invoked = True
            raw_findings = res.get("findings")
            if isinstance(raw_findings, list):
                for f in raw_findings:
                    if isinstance(f, dict):
                        findings.append(f)
        except Exception as model_err:
            # 模型调用失败时记录手记，不导致整轮死锁
            model_error = str(model_err)
            for u in unit_ids:
                notes[u] = f"model_runner failed: {model_err}"

    # 3.1 确定性双跑差分验证 + confirmation 终态裁决（Phase 2: differential_oracle / P0-C）
    # 无人值守路径必须把每条提交的候选收敛到 confirmed/rejected：否则 gate() 永远
    # pending>0，post 计命结算硬失败。证据只取自真实差分执行，缺任何一项准则要素
    # 就 rejected(insufficient_evidence) 并留下缺口说明。
    executed_platform = platform.system().lower()
    target_fingerprint = state.target_fingerprint or sup.target_fingerprint
    verified_findings: list[dict[str, Any]] = []
    for f in findings:
        vuln_id = _ensure_finding_id(f)
        poc_cmd = f.get("poc_cmd")
        poc_input = f.get("poc_input")
        if not poc_cmd or not poc_input:
            # 无复现原料的候选不可能过 confirmation 硬门槛：如实记为证据不足终态，
            # 而不是留成 candidate 永久阻塞结算。
            _settle_finding_confirmation(
                f, None, None,
                workspace_root=ws_root,
                artifact_dir=artifact_dir,
                artifact_root_rel=artifact_root_rel,
                target_fingerprint=target_fingerprint,
                executed_platform=executed_platform,
                run_reference=state.run_id,
                gap={
                    "oracle": {
                        "tier": "heuristic",
                        "reference": str(f.get("root_cause") or f.get("title") or "无"),
                        "provenance": {
                            "kind": "agent-generated",
                            "independent_from_change": False,
                            "verified": False,
                        },
                    },
                    "rejection_class": "insufficient_evidence",
                    "reason": (
                        f"候选 {vuln_id} 未提供 poc_cmd/poc_input，无法进入确定性双跑"
                        "差分验证，无独立 Oracle 裁决"
                    ),
                },
            )
            verified_findings.append(f)
            continue
        # 负对照原料：同命令 + 良性输入，必须由生产者显式提供（不自行臆造控制组）
        negative_input = f.get("poc_negative_input")
        diff_res: dict[str, Any] | None = None
        negative_res: dict[str, Any] | None = None
        try:
            from core.fuzzing import differential_oracle
            diff_res = differential_oracle.verify_differential_poc(
                str(poc_cmd),
                poc_input,
                timeout=float(os.environ.get("BUGX_POC_TIMEOUT", "10")),
                allowed_root=ws_root,
            )
            if _has_poc_input(negative_input):
                negative_res = differential_oracle.verify_differential_poc(
                    str(poc_cmd),
                    negative_input,
                    timeout=float(os.environ.get("BUGX_POC_TIMEOUT", "10")),
                    allowed_root=ws_root,
                )
        except Exception as diff_err:
            f["differential_error"] = str(diff_err)
            _settle_finding_confirmation(
                f, None, None,
                workspace_root=ws_root,
                artifact_dir=artifact_dir,
                artifact_root_rel=artifact_root_rel,
                target_fingerprint=target_fingerprint,
                executed_platform=executed_platform,
                run_reference=state.run_id,
                gap={
                    "oracle": {
                        "tier": "differential",
                        "reference": f"差分验证未执行成功（命令: {str(poc_cmd)[:400]}）",
                        "provenance": {
                            "kind": "independent-implementation",
                            "independent_from_change": True,
                            "verified": True,
                        },
                    },
                    "rejection_class": "tool_failure",
                    "reason": f"差分验证执行失败: {diff_err}",
                },
            )
            verified_findings.append(f)
            continue

        verdict = diff_res.get("verdict")
        f["differential_verdict"] = verdict
        f["differential_runs"] = diff_res.get("runs")
        if verdict == "FALSE_POSITIVE":
            # 双跑均正常退出：这是唯一允许镜像为 false_positive 的真实
            # 差分裁决；仍不进入确认 findings 账本。
            f["status"] = "rejected_false_positive"
            _transition_terminal(
                f, "rejected",
                str(diff_res.get("summary") or "双跑均正常退出，未复现候选异常"),
                "not_reproducible",
            )
            continue
        _settle_finding_confirmation(
            f, diff_res, negative_res,
            workspace_root=ws_root,
            artifact_dir=artifact_dir,
            artifact_root_rel=artifact_root_rel,
            target_fingerprint=target_fingerprint,
            executed_platform=executed_platform,
            run_reference=state.run_id,
        )
        verified_findings.append(f)
    findings = verified_findings

    # 3.2 最小安全补丁合成与工程免疫资产生成 (Phase 2: patch_synthesizer)
    for f in findings:
        target_file = f.get("file")
        target_code = f.get("target_code")
        replacement_code = f.get("replacement_code")
        if target_file and target_code and replacement_code:
            try:
                from core.repair.patch_synthesizer import PatchSynthesizer
                synth = PatchSynthesizer(ws_root)
                diff_text, _ = synth.synthesize_patch(
                    target_file,
                    str(target_code),
                    str(replacement_code),
                )
                vuln_id = str(f.get("id") or f"vuln_{uuid.uuid4().hex[:6]}")
                patch_file = artifact_dir / f"{_safe_name(vuln_id)}.patch"
                patch_file.write_text(diff_text, encoding="utf-8")
                f["patch_artifact"] = f"{artifact_root_rel}/{patch_file.name}"

                immunity_code = synth.generate_immunity_test(
                    vuln_id=vuln_id,
                    target_module=str(target_file),
                    test_payload=str(f.get("poc_input") or ""),
                )
                imm_file = artifact_dir / f"test_immunity_{_safe_name(vuln_id)}.py"
                imm_file.write_text(immunity_code, encoding="utf-8")
                f["immunity_test_artifact"] = f"{artifact_root_rel}/{imm_file.name}"

                # A synthesized diff is only a candidate artifact.  Confirmed
                # Findings must also pass the isolated dual-direction gate;
                # otherwise this phase must leave the Finding explicitly
                # unverified instead of presenting generated code as a fix.
                if f.get("status") == "confirmed":
                    poc_argv = _finding_argv(f.get("poc_cmd"), f.get("poc_input"))
                    negative_input = f.get("poc_negative_input")
                    negative_present = _has_poc_input(negative_input)
                    negative_argv = (
                        _finding_argv(f.get("poc_cmd"), negative_input)
                        if negative_present else None
                    )
                    regression_value = f.get("regression_cmd")
                    regression_present = regression_value is not None
                    regression_argv = (
                        _finding_argv(regression_value)
                        if regression_present else None
                    )
                    # Passing [] deliberately triggers the verifier's
                    # fail-closed argv contract when a required command or
                    # control cannot be converted safely.
                    if poc_argv is None:
                        poc_argv = []
                    if negative_present and negative_argv is None:
                        negative_argv = []
                    # A confirmed Finding without an explicit regression
                    # command cannot prove that the repair introduces no new
                    # defect.  Keep the verifier's fail-closed semantics by
                    # passing an invalid argv instead of treating the
                    # optional library-level gate as an auto-fix success.
                    if not regression_present or regression_argv is None:
                        regression_argv = []
                    try:
                        verify_timeout = float(
                            os.environ.get("BUGX_PATCH_VERIFY_TIMEOUT", "30")
                        )
                    except (TypeError, ValueError):
                        verify_timeout = 30.0
                    verification = synth.verify_patch_dual_gate(
                        patch_file=patch_file,
                        project_dir=ws_root,
                        poc_verify_cmd=poc_argv,
                        regression_cmd=regression_argv,
                        timeout=verify_timeout,
                        negative_control_cmd=negative_argv,
                    )
                    verification_payload = verification.to_dict()
                    f["patch_verification"] = verification_payload
                    f["patch_verified"] = verification.is_valid
                    verification_rel, verification_sha = _write_evidence_artifact(
                        artifact_dir,
                        artifact_root_rel,
                        f"{vuln_id}_patch_verification.json",
                        verification_payload,
                    )
                    f["patch_verification_artifact"] = verification_rel
                    f["patch_verification_artifact_sha256"] = verification_sha
                    if not verification.is_valid:
                        f["patch_verification_error"] = verification.detail
            except Exception as patch_err:
                f["patch_synthesis_error"] = str(patch_err)

    # 4. 为分配的每个单元生成证据与产物。
    # 自证边界：模型返回只能证明 Worker 收到了模型结果，不能证明本单元的
    # 外部命令由 Supervisor 观察并执行过。只有真实外部执行观察（当前 Runtime
    # 没有该能力）才能产生 covered/passed；否则必须保留 unverified 债务。
    #   - 真实模型返回（含零 Finding）→ unverified + blocked；
    #   - 模型调用抛错        → unverified + error；
    #   - 非真实 provider(mock) → unverified + skipped。
    # 三者都必须写 notes 说明已验证事实与重试原因（key 仅限 unit_ids），
    # 由 Supervisor 覆盖债务保留重试义务；伪造 passed 会凭空清空债务。
    now = time.time()
    if model_invoked:
        unit_coverage = "unverified"
        evidence_status = "blocked"
        evidence_exit_code = 1
        unit_note = (
            f"{UNVERIFIED_EXECUTION}: model_runner 返回只属于 Worker 自报，"
            "当前 Supervisor 没有外部命令完成观察；不得据此写入 covered/passed"
        )
    elif model_error is not None:
        unit_coverage = "unverified"
        evidence_status = "error"
        evidence_exit_code = 1
        unit_note = (
            f"model_runner failed: {model_error}；本单元未通过验证，"
            "重试需可用的真实 provider"
        )
    else:
        unit_coverage = "unverified"
        evidence_status = "skipped"
        evidence_exit_code = 0
        unit_note = (
            f"provider={active_provider} 未调用真实模型，本次无验证事实；"
            "重试需通过显式 provider 参数配置真实模型"
        )
    for unit in unit_ids:
        if unit_note is not None:
            notes[unit] = unit_note
        safe_u = _safe_name(unit)
        evidence_filename = f"{safe_u}_evidence.json"
        artifact_file = artifact_dir / evidence_filename
        evidence_rel_path = f"{artifact_root_rel}/{evidence_filename}"
        command = f"bugx-worker --worker {worker_id} --unit {unit}"

        execution_receipt = {
            "schema_version": 1,
            "execution_id": execution_id,
            "worker_id": worker_id,
            "lease_id": lease_id,
            "command": command,
            "exit_code": evidence_exit_code,
            "artifact_path": evidence_rel_path,
            "artifact_sha256": "",  # filled after serializing the artifact
            "status": evidence_status,
        }
        evidence_payload = {
            "artifact_type": "bugx-worker-execution/v1",
            "execution_verification": (
                UNVERIFIED_EXECUTION if model_invoked else None
            ),
            "unit_id": unit,
            "worker_id": worker_id,
            "lease_id": lease_id,
            "stage": stage_name,
            "status": evidence_status,
            "exit_code": evidence_exit_code,
            "task_spec_summary": task_spec[:256],
            "verified_at": now,
        }
        # The receipt is included in the artifact itself, so its claim binding
        # cannot silently diverge from the file whose hash is submitted.
        # A file cannot contain its own final SHA-256 without a circular
        # fixed-point hash.  The artifact carries the claim fields with an
        # empty digest; the checkpoint receipt carries the final digest.
        evidence_payload["execution_receipt"] = dict(execution_receipt)
        content_bytes = json.dumps(
            evidence_payload, ensure_ascii=False, indent=2,
        ).encode("utf-8")
        artifact_file.write_bytes(content_bytes)
        sha256 = hashlib.sha256(content_bytes).hexdigest()
        execution_receipt["artifact_sha256"] = sha256

        coverage_updates[unit] = unit_coverage
        evidence_updates[unit] = {
            "command": command,
            "worker_id": worker_id,
            "run_id": state.run_id,
            "round_no": round_no,
            "lease_id": lease_id,
            "target_fingerprint": state.target_fingerprint or sup.target_fingerprint,
            "artifact_path": evidence_rel_path,
            "artifact_sha256": sha256,
            "status": evidence_status,
            "exit_code": evidence_exit_code,
            "timestamp": now,
            "execution_receipt": execution_receipt,
        }

    # C1：提交前保活——覆盖模型调用/差分验证/补丁合成的整段耗时。
    _heartbeat_keepalive()

    # 5. 提交 Checkpoint
    checkpoint_id = f"chk-{worker_id}-{uuid.uuid4().hex[:8]}"
    try:
        updated_state = sup.submit_checkpoint(
            worker_id,
            coverage=coverage_updates,
            evidence=evidence_updates,
            findings=findings if findings else None,
            notes=notes if notes else None,
            checkpoint_id=checkpoint_id,
        )
    except Exception as submit_exc:
        raise WorkerRuntimeError(f"提交 Checkpoint 失败: {submit_exc}") from submit_exc

    # 6. UCB1 与调度历史闭环推进
    try:
        from . import task_scheduler
        # DOC-140 调度提速：worker turn 收尾不再新建调度器（新建=全仓建图
        # + 影响网络），改为按 (root, 状态指纹) 复用进程内实例；整批
        # record_audit_attempt 包进 batch_history 只落盘一次。
        # 调用方（workers()）传入同一实例可避免覆盖彼此的 UCB1 历史；
        # 无调用方传入时退回进程内共享实例。
        sched = scheduler if scheduler is not None else task_scheduler.shared_scheduler(ws_root)
        with _SCHEDULER_UPDATE_LOCK, sched.batch_history():
            for unit in unit_ids:
                unit_findings = [f for f in findings if f.get("unit_id") == unit or f.get("file") == unit]
                sched.record_audit_attempt(unit, findings_count=len(unit_findings))
    except Exception:
        pass

    return {
        "worker_id": worker_id,
        "checkpoint_id": checkpoint_id,
        "covered_units": [
            u for u in unit_ids if coverage_updates.get(u) == "covered"
        ],
        "findings_count": len(findings),
        "status": updated_state.status.value,
    }
