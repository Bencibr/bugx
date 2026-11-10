#!/usr/bin/env python3
"""core/mcp_handlers/mining_handlers.py - 漏洞挖掘、模糊测试与用例变异验证工具处理器 (DOC-070)。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

import core.bugx_paths as bugx_paths
from core.coverage import module_coverage

from .base import (
    McpProtocolError,
    check_json_depth_and_keys,
    number_argument,
    optional_string,
    MAX_COMMAND_LENGTH,
)

# Worker 唯一允许写入的目录前缀（DOC-004 / bugx-worker.md）。
# 用「目录白名单」而不是「basename 黑名单」：黑名单只能枚举已知的高价值文件，
# 漏掉 opencode.json、state/supervisor.json、**/adapters/*.json 等高价值目标，
# 而这些正是自我提权与运行态篡改的入口。
WORKER_ARTIFACT_DIR_NAME = "workers"


def _is_worker_artifact_path(relative: Path) -> bool:
    """Return whether a project-relative path is inside a Worker artifact dir."""
    parts = relative.parts
    if len(parts) < 3 or parts[0] != ".bugx":
        return False
    if WORKER_ARTIFACT_DIR_NAME not in parts:
        return False
    # ``workers`` 之后必须还有内容：目录本身不是可写文件。
    return parts.index(WORKER_ARTIFACT_DIR_NAME) < len(parts) - 1


def _confined_output_path(service: Any, output_file: str) -> Path:
    """Resolve ``output_file`` to a writable Worker artifact path."""
    out_path = (service.root / output_file).resolve()
    try:
        relative = out_path.relative_to(service.root)
    except ValueError:
        raise McpProtocolError("output_file 禁止跨越项目根目录", code=-32602)
    if not _is_worker_artifact_path(relative):
        raise McpProtocolError(
            f"output_file 必须位于 .bugx/**/{WORKER_ARTIFACT_DIR_NAME}/** 证据目录内: {relative}",
            code=-32602,
        )
    bugx_paths.assert_no_symlink_components(out_path, error_cls=McpProtocolError)
    return out_path


def handle_fuzz_concurrency(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    raw_target_cmd = arguments.get("target_cmd")
    # target_cmd 必须是 argv 数组：字符串命令会退化为 shell 解释（命令拼接、
    # 管道、重定向），而执行发生在 MCP server 进程内，宿主 permission.bash
    # 的 deny 规则不适用。协议层在此显式拒绝 str，不做「看起来安全就放行」的
    # 猜测。
    if isinstance(raw_target_cmd, (str, bytes)):
        raise McpProtocolError(
            "target_cmd 必须是 argv 数组（如 [\"python3\",\"-m\",\"unittest\",\"tests/test_x.py\"]），"
            "不接受字符串命令",
            code=-32602,
        )
    if not isinstance(raw_target_cmd, list) or not raw_target_cmd:
        raise McpProtocolError("target_cmd 必须是非空 argv 数组", code=-32602)
    target_cmd: list[str] = []
    for item in raw_target_cmd:
        if not isinstance(item, str) or not item:
            raise McpProtocolError("target_cmd 的每个元素必须是非空字符串", code=-32602)
        target_cmd.append(item)
    if sum(len(item) for item in target_cmd) > MAX_COMMAND_LENGTH:
        raise McpProtocolError(f"target_cmd 过长（>{MAX_COMMAND_LENGTH}）", code=-32602)
    threads = int(number_argument(arguments, "threads", 8))
    iterations = int(number_argument(arguments, "iterations", 30))
    timeout = number_argument(arguments, "timeout", 2.0)
    from core.fuzzing import concurrency_fuzzer
    try:
        res = concurrency_fuzzer.fuzz_command(
            target_cmd,
            cwd=service.root,
            concurrency=threads,
            iterations=iterations,
            timeout_per_iter=timeout,
        )
        return {"status": "ok", "report": res}
    except McpProtocolError:
        raise
    except Exception as exc:
        raise McpProtocolError(f"并发时序扰动执行失败: {exc}", code=-32603) from exc


def handle_fuzz_stateful(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    raw_transitions = arguments.get("transitions")
    if not isinstance(raw_transitions, list):
        raise McpProtocolError("transitions 必须是跃迁数组", code=-32602)
    max_length = int(number_argument(arguments, "max_length", 4))
    max_sequences = int(number_argument(arguments, "max_sequences", 20))
    from core.fuzzing import stateful_fuzzer
    try:
        res = stateful_fuzzer.fuzz_transitions(
            raw_transitions,
            max_length=max_length,
            max_sequences=max_sequences,
        )
        return {"status": "ok", "report": res}
    except Exception as exc:
        raise McpProtocolError(f"状态机变异推演失败: {exc}", code=-32603) from exc


def handle_verify_differential_poc(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    cmd_template = arguments.get("command_template")
    poc_input = arguments.get("poc_input")
    if not isinstance(cmd_template, str) or not cmd_template.strip():
        raise McpProtocolError("command_template 必须是非空字符串", code=-32602)
    if isinstance(poc_input, str):
        if not poc_input.strip():
            raise McpProtocolError("poc_input 字符串不得为空", code=-32602)
    elif isinstance(poc_input, os.PathLike):
        # PathLike is useful for direct library callers; JSON MCP clients use
        # a string path and are resolved relative to the audited project.
        if not str(poc_input).strip():
            raise McpProtocolError("poc_input 路径不得为空", code=-32602)
    elif isinstance(poc_input, (dict, list)):
        check_json_depth_and_keys(poc_input)
    else:
        raise McpProtocolError(
            "poc_input 必须是非空字符串/路径，或 JSON 对象/数组", code=-32602,
        )
    timeout = number_argument(arguments, "timeout", 10.0)
    from core.fuzzing import differential_oracle
    try:
        res = differential_oracle.verify_differential_poc(
            cmd_template,
            poc_input,
            timeout=timeout,
            allowed_root=service.root,
        )
        return {"status": "ok", "differential_report": res}
    except Exception as exc:
        raise McpProtocolError(f"差分沙盒双跑裁决失败: {exc}", code=-32603) from exc


def handle_ingest_coverage(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    report_path = arguments.get("report_path")
    if not isinstance(report_path, str) or not report_path.strip():
        raise McpProtocolError("report_path 必须是非空字符串", code=-32602)
    # 与 handle_test_verify / handle_mining_harden 对齐：报告必须落在项目内。
    # 否则 Worker 可让引擎解析项目外任意路径的 XML/JSON（把 XML 实体膨胀的
    # 触发面从「项目内报告」扩大到「宿主任意路径下攻击者预先投放的文件」）。
    resolved_report = (service.root / report_path).resolve()
    try:
        resolved_report.relative_to(service.root)
    except ValueError:
        raise McpProtocolError("report_path 禁止跨越项目根目录", code=-32602)
    format_hint = optional_string(arguments, "format_hint")
    try:
        res = module_coverage.ingest_test_coverage_report(
            service.root,
            resolved_report,
            format_hint=format_hint,
        )
        return {"status": "ok", "ingest_result": res}
    except McpProtocolError:
        raise
    except Exception as exc:
        raise McpProtocolError(f"测试覆盖报告回填失败: {exc}", code=-32603) from exc


def handle_inspect_mobile_ui(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    dump_content = arguments.get("dump_path_or_content")
    if not isinstance(dump_content, str) or not dump_content.strip():
        raise McpProtocolError("dump_path_or_content 必须是非空字符串", code=-32602)
    is_ios = bool(arguments.get("is_ios", False))
    density = number_argument(arguments, "density", 1.0)
    from core.skills import mobile_ui_inspector
    try:
        res = mobile_ui_inspector.inspect_ui_dump(
            dump_content,
            is_ios=is_ios,
            density=density,
        )
        return {"status": "ok", "ui_report": res}
    except Exception as exc:
        raise McpProtocolError(f"移动 UI 几何 Oracle 校验失败: {exc}", code=-32603) from exc


def _optional_project_path(
    service: Any, arguments: Mapping[str, object], key: str,
) -> str | None:
    """Read an optional path argument and prove it stays inside the project.

    Path-typed MCP parameters are the boundary where "Worker asked for a file"
    must be reduced to "a file inside the audited project".  Reading happens in
    the MCP server process, so the host's own ``permission.read`` deny rules do
    not apply here.
    """
    raw = optional_string(arguments, key)
    if raw is None:
        return None
    resolved = (service.root / raw).resolve()
    try:
        resolved.relative_to(service.root)
    except ValueError:
        raise McpProtocolError(f"{key} 禁止跨越项目根目录", code=-32602)
    return raw


def handle_fuzz_input(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    target_cmd = arguments.get("cmd")
    if not isinstance(target_cmd, str) or not target_cmd.strip():
        raise McpProtocolError("cmd 必须是非空字符串", code=-32602)
    # input_content 是纯内容；需要「从项目内文件取种子」时用 input_path。
    # 二者不可混用，避免内容被静默当成路径（任意文件读取）。
    input_content = optional_string(arguments, "input_content")
    input_path = _optional_project_path(service, arguments, "input_path")
    if input_content is not None and input_path is not None:
        raise McpProtocolError("input_content 与 input_path 不能同时提供", code=-32602)
    iterations = int(number_argument(arguments, "iterations", 30))
    timeout = number_argument(arguments, "timeout", 2.0)
    language = optional_string(arguments, "language")
    seed = None
    if "seed" in arguments and arguments["seed"] is not None:
        seed = int(number_argument(arguments, "seed", 0))
    from core.fuzzing import fuzz_input
    try:
        res = fuzz_input.fuzz_target(
            target_cmd,
            input_content=input_content,
            input_path=input_path,
            root=service.root,
            count=iterations,
            timeout=timeout,
            lang=language,
            seed=seed,
        )
        return {"status": "ok", "report": res}
    except McpProtocolError:
        raise
    except Exception as exc:
        raise McpProtocolError(f"输入变异模糊测试执行失败: {exc}", code=-32603) from exc


def handle_minimize_repro(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    target_cmd = arguments.get("cmd")
    if not isinstance(target_cmd, str) or not target_cmd.strip():
        raise McpProtocolError("cmd 必须是非空字符串", code=-32602)
    input_data = optional_string(arguments, "input_data")
    input_path = _optional_project_path(service, arguments, "input_path")
    if input_data is None and input_path is None:
        raise McpProtocolError("必须提供 input_data 或 input_path", code=-32602)
    if input_data is not None and input_path is not None:
        raise McpProtocolError("input_data 与 input_path 不能同时提供", code=-32602)
    timeout = number_argument(arguments, "timeout", 5.0)
    hierarchical = bool(arguments.get("hierarchical", True))
    from core.repair import minimize_repro
    try:
        res = minimize_repro.minimize_repro_input(
            target_cmd,
            input_data,
            input_path=input_path,
            root=service.root,
            timeout=timeout,
            hierarchical=hierarchical,
        )
        return {"status": "ok", "report": res}
    except McpProtocolError:
        raise
    except Exception as exc:
        raise McpProtocolError(f"PoC 失败样本最小化切除失败: {exc}", code=-32603) from exc


def handle_query_behavior_graph(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    file_filter = optional_string(arguments, "file_filter")
    include_anomalies = bool(arguments.get("include_anomalies", True))
    from core.autonomous_cve import behavior_graph
    try:
        res = behavior_graph.query_behavior_graph(
            service.root,
            file_filter=file_filter,
            include_anomalies=include_anomalies,
        )
        return {"status": "ok", "graph_report": res}
    except Exception as exc:
        raise McpProtocolError(f"行为图谱查询与异常推断失败: {exc}", code=-32603) from exc


def handle_audit_web_conformance(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    raw_files = arguments.get("files")
    files = None
    if raw_files is not None:
        if not isinstance(raw_files, list):
            raise McpProtocolError("files 必须是文件列表", code=-32602)
        files = [str(f) for f in raw_files]
    from core.report_engine import web_conformance
    try:
        res = web_conformance.audit_web_conformance(
            service.root,
            files=files,
        )
        return {"status": "ok", "audit_report": res}
    except Exception as exc:
        raise McpProtocolError(f"Web 合规质检审计失败: {exc}", code=-32603) from exc


def handle_mining_explore(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    subpath = optional_string(arguments, "subpath")
    if subpath:
        target_sub = (service.root / subpath).resolve()
        try:
            target_sub.relative_to(service.root)
        except ValueError:
            raise McpProtocolError("subpath 禁止跨越项目根目录", code=-32602)
    risk_level = (optional_string(arguments, "risk_level") or "ALL").upper()
    valid_risks = {"ALL", "CRITICAL", "HIGH", "MEDIUM", "LOW"}
    if risk_level not in valid_risks:
        raise McpProtocolError(f"risk_level 必须是 {valid_risks} 之一", code=-32602)
    risk_filter = None if risk_level == "ALL" else risk_level
    from core.runtime import mining_quality_framework
    try:
        explorer = mining_quality_framework.MiningSurfaceExplorer(service.root)
        subjects = explorer.explore(subpath=subpath)
        if risk_filter:
            subjects = [s for s in subjects if s.risk_level == risk_filter]
        kind_counts: dict[str, int] = {}
        risk_counts: dict[str, int] = {}
        for s in subjects:
            kind_counts[s.kind] = kind_counts.get(s.kind, 0) + 1
            risk_counts[s.risk_level] = risk_counts.get(s.risk_level, 0) + 1
        return {
            "status": "degraded" if explorer.errors else "ok",
            "total": len(subjects),
            "subjects": [s.to_dict() for s in subjects],
            "exploration_errors": list(explorer.errors),
            "summary": {
                "kinds": kind_counts,
                "risk_levels": risk_counts,
                "exploration_errors": len(explorer.errors),
            },
        }
    except Exception as exc:
        if isinstance(exc, McpProtocolError):
            raise
        raise McpProtocolError(f"可挖掘功能主体全息勘测失败: {exc}", code=-32603) from exc


def handle_mining_audit(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    raw_plan = arguments.get("plan")
    plan_file = optional_string(arguments, "plan_file")
    min_score = float(number_argument(arguments, "min_score", 80.0))
    strict = bool(arguments.get("strict", False))
    allow_degraded = bool(arguments.get("allow_degraded", False))
    plan_dict: dict[str, Any] | None = None
    if raw_plan is not None:
        if not isinstance(raw_plan, dict):
            raise McpProtocolError("plan 必须是对象", code=-32602)
        check_json_depth_and_keys(raw_plan)
        plan_dict = raw_plan
    elif plan_file:
        target_file = (service.root / plan_file).resolve()
        try:
            target_file.relative_to(service.root)
        except ValueError:
            raise McpProtocolError("plan_file 禁止跨越项目根目录", code=-32602)
        if not target_file.is_file():
            raise McpProtocolError(f"plan_file 文件不存在: {plan_file}", code=-32602)
        try:
            loaded = json.loads(target_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                plan_dict = loaded
            else:
                raise McpProtocolError("plan_file 内容必须是 JSON 对象", code=-32602)
        except Exception as exc:
            raise McpProtocolError(f"读取 plan_file 失败: {exc}", code=-32602) from exc
    else:
        default_campaign = service.root / ".bugx" / "audit-campaign.json"
        if default_campaign.is_file():
            try:
                plan_dict = json.loads(default_campaign.read_text(encoding="utf-8"))
            except Exception:
                plan_dict = {"units": []}
        else:
            plan_dict = {"units": []}
    from core.runtime import mining_quality_framework
    try:
        auditor = mining_quality_framework.MiningPlanAuditor(service.root)
        report = auditor.audit(plan_dict, min_score=min_score, strict=strict)
        # MCP 没有退出码，必须把门禁结论显式结构化返回：DEGRADED 不能与 PASS
        # 共用同一个「通过」语义，否则调用方会把降级方案当成验证通过执行。
        level = report.get("level")
        degraded = level == "DEGRADED"
        passed = level == "PASS" or (degraded and allow_degraded)
        return {
            "status": "ok" if passed else "blocked",
            "gate": {
                "level": level,
                "passed": passed,
                "degraded": degraded,
                "allow_degraded": allow_degraded,
                "requires_user_decision": degraded and not allow_degraded,
                "score": report.get("score"),
                "min_score": report.get("min_score"),
            },
            "audit_report": report,
        }
    except Exception as exc:
        if isinstance(exc, McpProtocolError):
            raise
        raise McpProtocolError(f"挖掘方案量化审计失败: {exc}", code=-32603) from exc


def handle_mining_harden(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    raw_plan = arguments.get("plan")
    plan_file = optional_string(arguments, "plan_file")
    output_file = optional_string(arguments, "output_file")
    plan_dict: dict[str, Any] | None = None
    if raw_plan is not None:
        if not isinstance(raw_plan, dict):
            raise McpProtocolError("plan 必须是对象", code=-32602)
        check_json_depth_and_keys(raw_plan)
        plan_dict = raw_plan
    elif plan_file:
        target_file = (service.root / plan_file).resolve()
        try:
            target_file.relative_to(service.root)
        except ValueError:
            raise McpProtocolError("plan_file 禁止跨越项目根目录", code=-32602)
        if not target_file.is_file():
            raise McpProtocolError(f"plan_file 文件不存在: {plan_file}", code=-32602)
        try:
            loaded = json.loads(target_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                plan_dict = loaded
            else:
                raise McpProtocolError("plan_file 内容必须是 JSON 对象", code=-32602)
        except Exception as exc:
            raise McpProtocolError(f"读取 plan_file 失败: {exc}", code=-32602) from exc
    else:
        default_campaign = service.root / ".bugx" / "audit-campaign.json"
        if default_campaign.is_file():
            try:
                plan_dict = json.loads(default_campaign.read_text(encoding="utf-8"))
            except Exception:
                plan_dict = {"units": []}
        else:
            plan_dict = {"units": []}
    from core.runtime import mining_quality_framework
    try:
        hardened = mining_quality_framework.MiningPlanHardener.harden(plan_dict, service.root)
        before_units = len(plan_dict.get("units", []))
        after_units = len(hardened.get("units", []))
        added = max(0, after_units - before_units)
        hardened_oracles = 0
        for u in hardened.get("units", []):
            for t in u.get("techniques", []):
                if t.get("oracle_hardened"):
                    hardened_oracles += 1
        result: dict[str, Any] = {
            "status": "ok",
            "hardened_plan": hardened,
            "added_units_count": added,
            "hardened_oracles_count": hardened_oracles,
        }
        if output_file:
            out_path = _confined_output_path(service, output_file)
            bugx_paths.ensure_dir(out_path.parent)
            out_path.write_text(json.dumps(hardened, ensure_ascii=False, indent=2), encoding="utf-8")
            result["saved_to"] = str(out_path.relative_to(service.root))
        return result
    except Exception as exc:
        if isinstance(exc, McpProtocolError):
            raise
        raise McpProtocolError(f"挖掘方案自动加固失败: {exc}", code=-32603) from exc


def handle_test_verify(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    target_path_str = arguments.get("target_path")
    if not isinstance(target_path_str, str) or not target_path_str.strip():
        raise McpProtocolError("target_path 必须是非空字符串", code=-32602)
    test_target = arguments.get("test_target")
    if not isinstance(test_target, str) or not test_target.strip():
        raise McpProtocolError("test_target 必须是非空字符串", code=-32602)
    if len(test_target) > MAX_COMMAND_LENGTH:
        raise McpProtocolError(f"test_target 过长（>{MAX_COMMAND_LENGTH}）", code=-32602)
    if re.search(r"\brm\s+-rf\s+[/~]", test_target):
        raise McpProtocolError("test_target 包含高危全盘删除指令", code=-32602)
    from core.planning import test_capability_verifier
    # 协议层先做入口白名单校验：只有 Python 解释器可以作为测试入口，
    # ``sh -c ...`` 一类可执行名在这里就被拒绝（返回 -32602 而非执行失败）。
    try:
        normalized_target = test_capability_verifier.normalize_test_command(test_target)
    except ValueError as exc:
        raise McpProtocolError(str(exc), code=-32602) from exc
    target_file = (service.root / target_path_str).resolve()
    try:
        target_file.relative_to(service.root)
    except ValueError:
        raise McpProtocolError("target_path 禁止跨越项目根目录", code=-32602)
    if not target_file.is_file():
        raise McpProtocolError(f"target_path 文件不存在: {target_path_str}", code=-32602)
    if target_file.suffix != ".py":
        raise McpProtocolError("target_path 必须是 Python 源码文件 (.py)", code=-32602)
    max_mutants = int(number_argument(arguments, "max_mutants", 20))
    if max_mutants < 1 or max_mutants > 50:
        raise McpProtocolError("max_mutants 必须在 1~50 之间", code=-32602)
    requested_timeout = float(number_argument(arguments, "timeout_sec", 2.0))
    # Native autonomous agents sometimes use a generic test timeout. Preserve
    # the verifier's hard safety window while making that bounded input usable.
    timeout_sec = min(10.0, max(0.5, requested_timeout))
    operators = arguments.get("operators")
    if operators is not None:
        if not isinstance(operators, list):
            raise McpProtocolError("operators 必须是列表", code=-32602)
        operators = [str(op) for op in operators]
    try:
        summary = test_capability_verifier.verify_test_capability(
            target_path=target_file,
            test_target=normalized_target,
            max_mutants=max_mutants,
            timeout_sec=timeout_sec,
            operators=operators,
            cwd=service.root,
        )
        return {"status": "ok", "capability_report": summary.to_dict()}
    except Exception as exc:
        if isinstance(exc, McpProtocolError):
            raise
        raise McpProtocolError(f"测试用例变异验证执行失败: {exc}", code=-32603) from exc


def handle_test_mutate(service: Any, arguments: Mapping[str, object]) -> dict[str, object]:
    target_path_str = arguments.get("target_path")
    if not isinstance(target_path_str, str) or not target_path_str.strip():
        raise McpProtocolError("target_path 必须是非空字符串", code=-32602)
    target_file = (service.root / target_path_str).resolve()
    try:
        target_file.relative_to(service.root)
    except ValueError:
        raise McpProtocolError("target_path 禁止跨越项目根目录", code=-32602)
    if not target_file.is_file():
        raise McpProtocolError(f"target_path 文件不存在: {target_path_str}", code=-32602)
    if target_file.suffix != ".py":
        raise McpProtocolError("target_path 必须是 Python 源码文件 (.py)", code=-32602)
    max_mutants = int(number_argument(arguments, "max_mutants", 30))
    if max_mutants < 1 or max_mutants > 100:
        raise McpProtocolError("max_mutants 必须在 1~100 之间", code=-32602)
    operators = arguments.get("operators")
    if operators is not None:
        if not isinstance(operators, list):
            raise McpProtocolError("operators 必须是列表", code=-32602)
        operators = [str(op) for op in operators]
    from core.planning import test_capability_verifier
    try:
        mutator = test_capability_verifier.SemanticMutator(target_file)
        mutants = mutator.generate_mutants(max_mutants=max_mutants, operators=operators)
        return {
            "status": "ok",
            "total": len(mutants),
            "mutants": [m.to_dict() for m in mutants],
        }
    except Exception as exc:
        if isinstance(exc, McpProtocolError):
            raise
        raise McpProtocolError(f"AST 变异体生成失败: {exc}", code=-32603) from exc


MINING_HANDLERS: dict[str, Any] = {
    "bugx_fuzz_concurrency": handle_fuzz_concurrency,
    "bugx_fuzz_stateful": handle_fuzz_stateful,
    "bugx_verify_differential_poc": handle_verify_differential_poc,
    "bugx_ingest_coverage": handle_ingest_coverage,
    "bugx_inspect_mobile_ui": handle_inspect_mobile_ui,
    "bugx_fuzz_input": handle_fuzz_input,
    "bugx_minimize_repro": handle_minimize_repro,
    "bugx_query_behavior_graph": handle_query_behavior_graph,
    "bugx_audit_web_conformance": handle_audit_web_conformance,
    "bugx_mining_explore": handle_mining_explore,
    "bugx_mining_audit": handle_mining_audit,
    "bugx_mining_harden": handle_mining_harden,
    "bugx_test_verify": handle_test_verify,
    "bugx_test_mutate": handle_test_mutate,
}
