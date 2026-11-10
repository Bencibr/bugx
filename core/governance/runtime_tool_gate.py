"""Fail-closed readiness gate for the selected Skill Pack's required tools."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

try:
    import core.secure_atomic as secure_atomic
except ImportError:
    from core import secure_atomic


def _canonical_target(value: object) -> str:
    target = str(value or "").strip().casefold().replace("_", "-")
    aliases = {
        "apple": "ios", "iphone": "ios", "ipad": "ios", "ipados": "ios",
        "google-android": "android",
    }
    return aliases.get(target, target)


def _known_targets(classification: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("targets", "declared_targets", "platforms"):
        raw = classification.get(key)
        if isinstance(raw, (list, tuple, set)):
            values.update(
                _canonical_target(value) for value in raw
                if isinstance(value, str) and value.strip()
            )
    build_targets = classification.get("build_targets")
    if isinstance(build_targets, (list, tuple)):
        for item in build_targets:
            if not isinstance(item, Mapping):
                continue
            for key in ("platform", "name"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    values.add(_canonical_target(value))
    return values


def _check_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = report.get("checks")
    if not isinstance(raw, list):
        return []
    rows = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        rows.append({
            "name": str(item.get("name") or "unnamed")[:120],
            "ok": item.get("ok") is True,
            "required": item.get("required") is not False,
            "detail": str(item.get("detail") or "")[:1000],
        })
    return rows


def _report_ready(report: object) -> bool:
    if not isinstance(report, Mapping):
        return False
    status = str(report.get("status") or "").strip().upper()
    if status not in {"READY", "DEGRADED"}:
        return False
    checks = _check_rows(report)
    if not checks:
        return False
    required_checks = [row for row in checks if row["required"]]
    return bool(required_checks) and all(row["ok"] for row in required_checks)


def _classification_hash(classification: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        classification, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _requirements(skill_plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    if "required_runtime" not in skill_plan:
        raise ValueError("skill_plan.required_runtime is missing")
    runtime = skill_plan.get("required_runtime")
    if not isinstance(runtime, Mapping):
        raise ValueError("skill_plan.required_runtime must be an object")
    tools = runtime.get("tools")
    if not isinstance(tools, list):
        raise ValueError("skill_plan.required_runtime.tools must be a list")
    if not tools:
        return []
    raw_stages = skill_plan.get("stage_pipeline")
    if not isinstance(raw_stages, list):
        raise ValueError("skill_plan.stage_pipeline must be a list")
    stage_ids: list[str] = []
    active_stages: set[str] = set()
    for stage in raw_stages:
        if not isinstance(stage, Mapping):
            raise ValueError("skill_plan.stage_pipeline contains an invalid stage")
        stage_id = str(stage.get("stage_id") or "").strip()
        if not stage_id:
            raise ValueError("skill_plan.stage_pipeline contains a stage without stage_id")
        required = stage.get("required")
        if not isinstance(required, bool):
            raise ValueError(
                f"skill_plan.stage_pipeline.{stage_id}.required must be a boolean"
            )
        stage_ids.append(stage_id)
        if required:
            active_stages.add(stage_id)
    if len(set(stage_ids)) != len(stage_ids):
        raise ValueError("skill_plan.stage_pipeline contains duplicate stage_id values")
    declared_stages = set(stage_ids)
    requirements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in tools:
        if not isinstance(item, Mapping):
            raise ValueError("skill_plan.required_runtime.tools has an invalid entry")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("skill_plan.required_runtime.tools entry has no name")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"duplicate required runtime tool: {name}")
        seen.add(key)
        tier = str(item.get("tier") or "required").strip()
        if tier not in {"required", "optional"}:
            raise ValueError(f"invalid runtime tool tier for {name}: {tier}")
        stage_ids = item.get("stage_ids")
        if not isinstance(stage_ids, list) or not stage_ids or any(
            not isinstance(stage, str) or not stage.strip() for stage in stage_ids
        ):
            raise ValueError(f"invalid stage_ids for required runtime tool: {name}")
        normalized_stages = [stage.strip() for stage in stage_ids]
        if len(set(normalized_stages)) != len(normalized_stages):
            raise ValueError(f"duplicate stage_ids for required runtime tool: {name}")
        unknown_stages = sorted(set(normalized_stages) - declared_stages)
        if unknown_stages:
            raise ValueError(
                f"runtime tool {name} references stage_id values absent from "
                f"stage_pipeline: {', '.join(unknown_stages)}"
            )
        targets = item.get("targets", [])
        if not isinstance(targets, list) or any(
            not isinstance(target, str) or not target.strip() for target in targets
        ):
            raise ValueError(f"invalid target conditions for runtime tool: {name}")
        canonical_targets = [_canonical_target(target) for target in targets]
        if len(set(canonical_targets)) != len(canonical_targets):
            raise ValueError(f"duplicate target conditions for runtime tool: {name}")
        requirements.append({
            "name": name,
            "tier": tier,
            "stage_ids": normalized_stages,
            "active": bool(set(normalized_stages) & active_stages),
            "targets": canonical_targets,
        })
    return requirements


def required_tool_ids_for_active_stages(
    skill_plan: Mapping[str, Any] | None,
) -> set[str]:
    """Return required probe IDs whose declared stages remain in the plan."""
    if skill_plan is None:
        return set()
    return {
        item["name"] for item in _requirements(skill_plan)
        if item["tier"] == "required" and item["active"]
    }


def _target_applicability(
    required_targets: list[str], project_targets: set[str],
) -> str | None:
    if not required_targets or not project_targets:
        return None
    recognized_targets = {
        "android", "ios", "macos", "windows", "linux", "web", "watchos",
        "tvos", "visionos",
    }
    if not project_targets.issubset(recognized_targets):
        return None
    if project_targets.isdisjoint(required_targets):
        return (
            f"项目已声明目标 {sorted(project_targets)}，与工具适用目标 "
            f"{sorted(required_targets)} 不相交"
        )
    return None


def _receipt_tool_result(
    name: str,
    tier: str,
    report: object,
    source: str,
) -> dict[str, Any]:
    normalized = report if isinstance(report, Mapping) else {}
    ready = _report_ready(normalized)
    observed_status = str(normalized.get("status") or "BLOCKED").upper()
    checks = _check_rows(normalized)
    if ready:
        result_status = "READY"
    elif observed_status == "NOT_APPLICABLE":
        # A probe cannot waive its own required status. Applicability is
        # authorized only by the selected plan's explicit target condition.
        result_status = "BLOCKED"
        checks.append({
            "name": "applicability",
            "ok": False,
            "required": True,
            "detail": "探针自行报告 NOT_APPLICABLE；仅 Skill Pack 显式目标条件可授权跳过",
        })
    elif observed_status in {"READY", "DEGRADED"} and not any(
        row["required"] for row in checks
    ):
        # Optional-only checks do not prove a required tool is available.
        result_status = "BLOCKED"
        checks.append({
            "name": "required_check",
            "ok": False,
            "required": True,
            "detail": "就绪报告未提供任何必需检查，无法证明工具可用",
        })
    else:
        result_status = observed_status
    return {
        "name": name,
        "tier": tier,
        "status": result_status,
        "source": source,
        "checks": checks,
    }


def enforce_required_runtime_tools(
    root: Path,
    skill_plan: Mapping[str, Any] | None,
    classification: Mapping[str, Any] | None,
    preflight_profile: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Recheck selected Skill Pack tools and persist a plan-bound receipt.

    Existing READY preflight reports are reused. A failed preflight tool is
    rerun after the startup self-heal pass. Runtime tools are invoked only via
    descriptor-declared safe availability actions.
    """
    if skill_plan is None:
        return None
    root = root.resolve()
    if not isinstance(classification, Mapping):
        classification = {}
    skill_id = str(skill_plan.get("skill_id") or "").strip()
    plan_hash = str(skill_plan.get("plan_hash") or "").strip()
    results: list[dict[str, Any]] = []
    error: str | None = None
    try:
        requirements = _requirements(skill_plan)
        if requirements and (not skill_id or not plan_hash):
            raise ValueError("required tools need a skill_id and plan_hash")
        try:
            from core.probes import probe_registry
        except ImportError:
            from core import probe_registry

        project_targets = _known_targets(classification)
        project_forms = {
            str(value).casefold() for value in classification.get("forms", [])
            if isinstance(value, str)
        } if isinstance(classification.get("forms"), list) else set()
        project_languages = {
            str(value).casefold() for value in classification.get("languages", [])
            if isinstance(value, str)
        } if isinstance(classification.get("languages"), list) else set()
        project_frameworks = {
            str(value).casefold() for value in classification.get("frameworks", [])
            if isinstance(value, str)
        } if isinstance(classification.get("frameworks"), list) else set()
        preflight_reports = (
            preflight_profile.get("probes", {})
            if isinstance(preflight_profile, Mapping)
            and isinstance(preflight_profile.get("probes"), Mapping)
            else {}
        )

        for requirement in requirements:
            name = requirement["name"]
            tier = requirement["tier"]
            if not requirement["active"]:
                results.append({
                    "name": name, "tier": tier, "status": "NOT_IN_STAGE_PLAN",
                    "source": "stage_plan", "stage_ids": requirement["stage_ids"],
                    "checks": [],
                })
                continue
            descriptor = probe_registry.get_probe(name)
            if descriptor is None:
                report = {"status": "BLOCKED", "checks": [{
                    "name": "descriptor", "ok": False,
                    "detail": f"未注册的 BugX probe: {name}",
                }]}
                results.append(_receipt_tool_result(name, tier, report, "registry"))
                continue
            if tier == "optional":
                results.append({
                    "name": name, "tier": tier, "status": "OPTIONAL",
                    "source": "registry", "checks": [],
                })
                continue
            not_applicable_reason = _target_applicability(
                requirement["targets"], project_targets,
            )
            if not_applicable_reason:
                results.append({
                    "name": name, "tier": tier, "status": "NOT_APPLICABLE",
                    "source": "project_classification", "detail": not_applicable_reason,
                    "checks": [],
                })
                continue
            report = preflight_reports.get(name) if descriptor.lifecycle == "preflight" else None
            source = "preflight_profile"
            should_run_readiness = descriptor.lifecycle == "runtime" or report is None
            should_run_readiness = should_run_readiness or (
                not _report_ready(report) and bool(descriptor.readiness_args)
            )
            if should_run_readiness:
                report = probe_registry.run_probe_readiness(
                    name,
                    root,
                    forms=project_forms,
                    languages=project_languages,
                    targets=project_targets,
                    frameworks=project_frameworks,
                )
                source = "safe_readiness_action"
            results.append(_receipt_tool_result(name, tier, report, source))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:1000]

    ready = error is None and all(
        item.get("status") in {
            "READY", "NOT_APPLICABLE", "NOT_IN_STAGE_PLAN", "OPTIONAL",
        }
        for item in results
    )
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": time.time(),
        "project_root": str(root),
        "skill_id": skill_id,
        "skill_plan_hash": plan_hash,
        "project_classification_hash": _classification_hash(classification),
        "status": "READY" if ready else "BLOCKED",
        "tools": results,
    }
    if error:
        receipt["error"] = error

    receipt_path = root / ".bugx" / "state" / "runtime-tool-readiness.json"
    try:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        secure_atomic.atomic_write_json(receipt_path, receipt)
        receipt["receipt_path"] = str(receipt_path)
    except Exception as exc:
        receipt["status"] = "BLOCKED"
        receipt["error"] = (
            f"readiness receipt write failed: {type(exc).__name__}: {exc}"
        )[:1000]
    return receipt
