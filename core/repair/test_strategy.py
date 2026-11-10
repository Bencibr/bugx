#!/usr/bin/env python3
"""按复杂度、系统行为图、历史问题和覆盖缺口生成风险优先测试策略。"""

from __future__ import annotations
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if str(Path(__file__).resolve().parent.parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.autonomous_cve import behavior_graph
from core.runtime import project_graph
from core.retrieval import symbol_index
from core.report_engine import web_conformance

SCHEMA_VERSION = 1
CONTROL_RE = re.compile(r"\b(if|elif|else|for|while|case|match|switch|catch|except)\b")
CONCURRENCY_RE = re.compile(r"\b(lock|mutex|semaphore|atomic|thread|async|await|transaction)\b", re.I)
STATE_RE = re.compile(r"\b(state|status|transition|workflow|lifecycle|retry|rollback)\b", re.I)
INPUT_RE = re.compile(r"\b(request|input|payload|argv|query|body|params|deserialize|parse)\b", re.I)


class StrategyError(ValueError):
    """测试策略输入或计划错误。"""


def _load(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StrategyError(f"无法读取 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StrategyError(f"{path} 根必须是对象")
    return value


def _unit_ranges(root: Path) -> dict[str, tuple[int, int]]:
    result = {}
    try:
        index = symbol_index.index_repository(root, strict=True)
    except symbol_index.SourceDiscoveryError as exc:
        raise StrategyError(str(exc)) from exc
    errors = [item for item in index["files"] if item.get("error")]
    if errors:
        detail = "; ".join(f"{item['path']}: {item['error']}" for item in errors[:5])
        raise StrategyError(f"符号索引不完整，拒绝生成测试计划: {detail}")
    for file_item in index["files"]:
        for symbol in file_item["symbols"]:
            result[symbol["scope"]] = (symbol["start_line"], symbol["end_line"])
    return result


def _graph_degree(root: Path) -> dict[str, int]:
    try:
        graph = project_graph.build_graph(root)
    except project_graph.GraphError as exc:
        raise StrategyError(str(exc)) from exc
    if not graph.get("complete", False):
        detail = "; ".join(
            f"{item['path']}: {item['error']}" for item in graph.get("parse_errors", [])[:5]
        )
        raise StrategyError(f"项目关系图不完整，拒绝生成测试计划: {detail}")
    degree: Counter[str] = Counter()
    for edge in graph["edges"]:
        if edge["type"] not in {"calls", "imports"}:
            continue
        source = edge.get("source", edge.get("from"))
        target = edge.get("target", edge.get("to"))
        if not isinstance(source, str) or not isinstance(target, str):
            raise StrategyError("项目关系图 calls/imports 边缺有效 source/target")
        for node in (source, target):
            path = node.split(":", 1)[-1].split("::", 1)[0]
            degree[path] += 1
    return dict(degree)


def _history_counts(findings: dict | None) -> dict[str, int]:
    result: Counter[str] = Counter()
    for finding in (findings or {}).get("findings", []):
        if not isinstance(finding, dict):
            continue
        confirmation = finding.get("confirmation") or {}
        if confirmation.get("state") == "rejected":
            continue
        for evidence in finding.get("evidence", []):
            if isinstance(evidence, dict) and isinstance(evidence.get("path"), str):
                result[evidence["path"].replace("\\", "/")] += 1
    return dict(result)


def _coverage_gaps(coverage: dict | None) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = defaultdict(list)
    for unit in (coverage or {}).get("units", []):
        if isinstance(unit, dict) and isinstance(unit.get("path"), str):
            result[unit["path"]].extend(unit.get("gaps") or [])
    return result


def _is_web_unit(path: str, snippet: str) -> bool:
    """判断代码单元是否真正归属于 Web/前端范围，杜绝纯后端单元被泛化污染。"""
    norm = path.replace("\\", "/").lower()
    ext = Path(norm).suffix
    if ext in {".html", ".htm", ".jsx", ".tsx", ".vue", ".svelte", ".css", ".scss", ".less"}:
        return True
    if any(d in norm for d in ("/frontend/", "/ui/", "/static/", "/templates/", "/views/", "/components/", "/public/")):
        return True
    if any(k in snippet for k in ("document.", "window.", "addEventListener", "render_template", "ReactDOM", "createApp", "HTMLElement")):
        return True
    return False


def calculate_unit_risk_score(
    path: str,
    root: Path | None = None,
    *,
    symbol_range: tuple[int, int] | None = None,
    snippet: str | None = None,
    degree: int = 0,
    history_count: int = 0,
    has_surfaces: bool = False,
    gaps_count: int = 0,
    is_changed: bool = False,
    ai_hypotheses: list[dict] | None = None,
    is_priority_focus: bool = False,
) -> float:
    """计算单个代码单元的脆弱性与内在缺陷风险得分 (10.0 ~ 100.0)。"""
    if snippet is None:
        if root is not None:
            try:
                target_file = root / path.split("::")[0]
                if target_file.is_file() and target_file.stat().st_size <= 1024 * 1024:
                    lines = target_file.read_text(encoding="utf-8", errors="replace").splitlines()
                    if symbol_range:
                        start, end = symbol_range
                        snippet = "\n".join(lines[max(start - 1, 0):max(end, start)])
                    else:
                        snippet = "\n".join(lines)
                else:
                    snippet = ""
            except (OSError, UnicodeDecodeError):
                snippet = ""
        else:
            snippet = ""

    # DOC-241: AI 语义复核与假说动态关联
    rel_file = path.split("::")[0].replace("\\", "/")
    if ai_hypotheses is None and root is not None:
        try:
            from core.autonomous_cve import attack_graph_store
            graph = attack_graph_store.load_graph(root)
            if graph is not None:
                matched_hypos = []
                for h in graph.hypotheses.values():
                    h_file = str(h.get("source_file") or "").replace("\\", "/")
                    if h_file and (h_file == rel_file or rel_file.endswith(h_file) or h_file.endswith(rel_file)):
                        matched_hypos.append(h)
                ai_hypotheses = matched_hypos
        except Exception:
            ai_hypotheses = []

    if not is_priority_focus and root is not None:
        try:
            rev_file = root / ".bugx" / "state" / "l1-semantic-review.json"
            if rev_file.is_file():
                rev_data = json.loads(rev_file.read_text(encoding="utf-8"))
                focus_list = (rev_data.get("audit_strategy_verdict") or {}).get("priority_focus") or []
                if any(rel_file.endswith(str(x).replace("\\", "/")) or str(x).replace("\\", "/").endswith(rel_file) for x in focus_list):
                    is_priority_focus = True
        except Exception:
            pass

    from core.retrieval import code_analyzer
    ext = Path(rel_file).suffix.lower()
    lang = code_analyzer.LANGUAGE_BY_EXTENSION.get(ext, "generic")
    analysis = code_analyzer.analyze_file_risk(
        rel_file, lang, sloc=len(snippet.splitlines()), is_test=False,
        raw_bytes=snippet.encode("utf-8", errors="ignore"),
    )
    critical_sinks = analysis.get("critical_sinks", [])
    taints = analysis.get("taint_flows", [])
    mitigations = analysis.get("mitigations", [])
    secrets = analysis.get("secrets", [])

    # 暴露面加权 (补丁 7)
    norm_snippet = snippet.lower()
    if has_surfaces or any(p in norm_snippet for p in ("@app.route", "@app.get", "@app.post", "@router.", "router.", "exports.", "handler(")):
        exposure = 1.3
    elif any(p in norm_snippet for p in ("sys.argv", "argparse", "click", "os.environ", "getenv")):
        exposure = 1.1
    else:
        exposure = 0.9

    ai_boost = 0.0
    if ai_hypotheses:
        has_critical = any(
            str(h.get("severity") or "").lower() in ("critical", "high")
            or str(h.get("type") or "").lower() in ("rce", "command_injection", "sql_injection", "deserialization", "path_traversal", "hardcoded_secret", "secret")
            for h in ai_hypotheses
        )
        ai_boost = 20.0 if has_critical else 10.0
    elif is_priority_focus:
        ai_boost = 10.0

    if len(critical_sinks) > 0 or len(taints) > 0 or len(secrets) > 0:
        raw_base = 85.0 + min(len(critical_sinks) * 5.0 + len(taints) * 5.0 + len(secrets) * 5.0, 15.0) + ai_boost
        final_score = max(80.0, raw_base * exposure)
    elif len(mitigations) > 0:
        raw_base = max(10.0, 30.0 - len(mitigations) * 15.0) + ai_boost
        final_score = raw_base * exposure
    else:
        raw_base = (
            10.0
            + (15 if has_surfaces else (8 if INPUT_RE.search(snippet) else 0))
            + min(degree * 2, 20)
            + (10 if CONCURRENCY_RE.search(snippet) else 0)
            + (10 if STATE_RE.search(snippet) else 0)
            + min(history_count * 5, 15)
            + min(gaps_count * 5, 20)
            + (20 if is_changed else 0)
            + ai_boost
        )
        final_score = raw_base * exposure

    if ai_boost > 0:
        final_score = max(final_score, 80.0 + (5.0 if ai_boost >= 20.0 else 0.0))

    return float(min(max(final_score, 10.0), 100.0))


def risk_score_to_difficulty(score: float) -> str:
    """将风险得分 (10.0 ~ 100.0) 映射为覆盖清单难度等级 ("1" ~ "5")。"""
    if score >= 80:
        return "5"
    if score >= 60:
        return "4"
    if score >= 40:
        return "3"
    if score >= 25:
        return "2"
    return "1"


def _techniques(
    snippet: str, surfaces: list[dict], gaps: list[dict],
    *, web: bool = False, ai_hypotheses: list[dict] | None = None,
) -> list[dict]:
    kinds = {item["kind"] for item in surfaces}
    techniques = [
        {"name": "boundary", "oracle": "contract", "reason": "最小单元边界与非法输入"},
        {"name": "negative-control", "oracle": "invariant", "reason": "确认异常仅由目标条件触发"},
    ]
    if CONTROL_RE.search(snippet) or INPUT_RE.search(snippet):
        techniques.append({"name": "property-based", "oracle": "invariant", "reason": "分支/输入空间适合属性生成"})
        techniques.append({"name": "guided-fuzz", "oracle": "contract", "reason": "按类型与分支反馈扩充输入"})
    if STATE_RE.search(snippet) or kinds & {"event", "database"}:
        techniques.append({"name": "state-machine", "oracle": "invariant", "reason": "覆盖合法与非法状态迁移"})
        techniques.append({"name": "fault-injection", "oracle": "invariant", "reason": "验证重试、部分失败与恢复"})
    if CONCURRENCY_RE.search(snippet) or "concurrency" in kinds:
        techniques.append({"name": "concurrency", "oracle": "invariant", "reason": "验证竞态、幂等与锁顺序"})
    if kinds & {"http-route", "external-service"}:
        techniques.append({"name": "contract-differential", "oracle": "contract", "reason": "比较接口 Schema、实现或版本"})
        techniques.append({"name": "metamorphic", "oracle": "invariant", "reason": "验证输入变换后的输出关系"})
    if any(item.get("metric") == "mutation" for item in gaps):
        techniques.append({"name": "mutation", "oracle": "test", "reason": "现有测试未证明能杀死行为变异"})
    if web:
        techniques.append({
            "name": "web-standards", "oracle": "standard",
            "reason": "WCAG 2.2 AA、WAI-ARIA 1.2、HTML/WHATWG 语义与浏览器动态证据",
        })
    if ai_hypotheses:
        techniques.append({
            "name": "ai-hypothesis-verification",
            "oracle": "invariant",
            "reason": f"AI Recon 假说验证: {'; '.join(str(h.get('title') or h.get('type') or 'vulnerability') for h in ai_hypotheses[:2])}",
        })
        if any(
            str(h.get("severity") or "").lower() in ("critical", "high")
            or str(h.get("type") or "").lower() in ("rce", "command_injection", "sql_injection", "deserialization", "path_traversal", "hardcoded_secret", "secret")
            for h in ai_hypotheses
        ):
            techniques.append({
                "name": "exploit-synthesis",
                "oracle": "contract",
                "reason": "针对 AI 识别出的高危假说合成利用与复现验证",
            })
    deduplicated = {item["name"]: item for item in techniques}
    return [deduplicated[name] for name in deduplicated]


def build_plan(root: Path, coverage: dict | None = None,
               findings: dict | None = None, base: str | None = None) -> dict:
    root = root.resolve()
    web_required = web_conformance.detect_web_project(root)
    web_plan = web_conformance.plan_metadata(root)
    try:
        units = symbol_index.audit_units(root, strict=True)
    except symbol_index.SourceDiscoveryError as exc:
        raise StrategyError(str(exc)) from exc
    if not units:
        raise StrategyError("没有发现可测试源码单元")
    unit_errors = [unit for unit in units if unit.get("error")]
    if unit_errors:
        detail = "; ".join(
            f"{unit['path']}: {unit['error']}" for unit in unit_errors[:5]
        )
        raise StrategyError(f"源码单元解析不完整，拒绝生成测试计划: {detail}")
    ranges = _unit_ranges(root)
    degree = _graph_degree(root)
    try:
        behavior = behavior_graph.build_behavior_graph(root)
    except behavior_graph.BehaviorGraphError as exc:
        raise StrategyError(str(exc)) from exc
    surfaces_by_path: dict[str, list[dict]] = defaultdict(list)
    for item in behavior["surfaces"]:
        surfaces_by_path[item["path"]].append(item)
    historical = _history_counts(findings)
    gaps_by_path = _coverage_gaps(coverage)
    changed: set[str] = set()
    if base:
        try:
            changed = set(project_graph.changed_paths(root, base))
        except project_graph.GraphError as exc:
            raise StrategyError(str(exc)) from exc

    # DOC-241: 载入活体攻击图谱与 AI 语义复核假说
    ai_hypotheses_by_file: dict[str, list[dict]] = defaultdict(list)
    priority_focus_paths: set[str] = set()
    try:
        from core.autonomous_cve import attack_graph_store
        graph = attack_graph_store.load_graph(root)
        if graph is not None:
            for hypo in graph.hypotheses.values():
                src = str(hypo.get("source_file") or "").replace("\\", "/")
                if src:
                    ai_hypotheses_by_file[src].append(hypo)
    except Exception:
        pass

    rev_file = root / ".bugx" / "state" / "l1-semantic-review.json"
    if rev_file.is_file():
        try:
            rev_data = json.loads(rev_file.read_text(encoding="utf-8"))
            for p in (rev_data.get("audit_strategy_verdict") or {}).get("priority_focus") or []:
                priority_focus_paths.add(str(p).replace("\\", "/"))
        except Exception:
            pass

    planned = []
    total_ai_hypotheses = 0
    for unit in units:
        path = unit["path"]
        try:
            lines = (root / path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise StrategyError(f"无法读取测试单元 {path}: {exc}") from exc
        start, end = ranges.get(unit["scope"], (1, len(lines) or 1))
        snippet = "\n".join(lines[max(start - 1, 0):max(end, start)])
        unit_surfaces = [
            item for item in surfaces_by_path.get(path, [])
            if max(1, start - 3) <= item["line"] <= end
        ]
        gaps = gaps_by_path.get(path, [])
        unit_file = path.replace("\\", "/").split("::")[0]
        matched_hypos = ai_hypotheses_by_file.get(unit_file, [])
        is_focus = any(unit_file.endswith(p) or p.endswith(unit_file) for p in priority_focus_paths)
        if matched_hypos:
            total_ai_hypotheses += len(matched_hypos)

        score = calculate_unit_risk_score(
            path,
            root,
            symbol_range=(start, end),
            snippet=snippet,
            degree=degree.get(path, 0),
            history_count=historical.get(path, 0),
            has_surfaces=bool(unit_surfaces),
            gaps_count=len(gaps),
            is_changed=(path in changed),
            ai_hypotheses=matched_hypos,
            is_priority_focus=is_focus,
        )
        is_web = web_required and _is_web_unit(path, snippet)
        unit_techniques = _techniques(snippet, unit_surfaces, gaps, web=is_web, ai_hypotheses=matched_hypos)
        planned.append({
            **unit,
            "risk_score": score,
            "ai_hypotheses": matched_hypos,
            "ai_priority_focus": is_focus,
            "risk_components": {
                "complexity": min(len(CONTROL_RE.findall(snippet)), 10),
                "graph_centrality": min(degree.get(path, 0) * 2, 20),
                "attack_surface": 15 if unit_surfaces else (8 if INPUT_RE.search(snippet) else 0),
                "concurrency": 10 if CONCURRENCY_RE.search(snippet) else 0,
                "statefulness": 10 if STATE_RE.search(snippet) else 0,
                "history": min(historical.get(path, 0) * 5, 15),
                "coverage_gap": min(len(gaps) * 5, 20),
                "changed": 20 if path in changed else 0,
                "ai_hypotheses_bonus": 20 if matched_hypos else (10 if is_focus else 0),
            },
            "behavior_surfaces": unit_surfaces,
            "coverage_gaps": gaps,
            "techniques": unit_techniques,
            "scaffold_ready": True,
            "suggested_test_file": f"tests/test_{Path(path).stem}.py",
        })
    planned.sort(key=lambda item: (-item["risk_score"], item["scope"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "root": ".",
        "full_coverage_required": True,
        "strategy": "risk-priority-with-final-full-coverage",
        "units": planned,
        "summary": {
            "total": len(planned),
            "high_risk": sum(item["risk_score"] >= 60 for item in planned),
            "ai_hypotheses_targeted": sum(bool(item.get("ai_hypotheses")) for item in planned),
            "ai_recon_active": bool(total_ai_hypotheses > 0 or priority_focus_paths),
            "behavior_surfaces": behavior["summary"]["surfaces"],
            "system_flows": len(behavior_graph.discover_system_flows(behavior)),
            "techniques": dict(sorted(Counter(
                technique["name"] for item in planned for technique in item["techniques"]
            ).items())),
            "web_standards_required": web_required,
        },
        "web_standards": web_plan,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "scaffold"), nargs="?", default="plan")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--coverage", type=Path)
    parser.add_argument("--findings", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--unit", help="scaffold 模式下指定的单元 scope")
    parser.add_argument("--out", type=Path, help="生成的测试脚手架输出路径")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "scaffold":
        if not args.unit:
            print("[test_strategy] ✗ scaffold 命令必须提供 --unit <scope>", file=sys.stderr)
            return 2
        root = args.root.resolve()
        scope = args.unit.strip()
        target_path = (root / scope.split("::", 1)[0]).resolve()
        try:
            from core.repair import test_generator
            symbol = test_generator.extract_target_symbol(target_path, scope)
            code = test_generator.generate_unittest_suite(symbol, output_path=args.out)
            if args.json:
                print(json.dumps({
                    "status": "success",
                    "unit": scope,
                    "target_symbol": symbol.qualname,
                    "output_path": str(args.out) if args.out else None,
                    "lines_count": len(code.splitlines()),
                }, ensure_ascii=False, indent=2))
            else:
                if args.out:
                    print(f"[test_strategy] ✓ 已生成测试脚手架: {args.out}")
                else:
                    print(code)
            return 0
        except Exception as exc:
            print(f"[test_strategy] ✗ 生成脚手架失败: {exc}", file=sys.stderr)
            return 2

    try:
        payload = build_plan(
            args.root, _load(args.coverage), _load(args.findings), args.base,
        )
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"[test_strategy] 单元 {payload['summary']['total']}，"
                f"高风险 {payload['summary']['high_risk']}，"
                f"行为面 {payload['summary']['behavior_surfaces']}"
            )
            for item in payload["units"][:10]:
                names = ",".join(value["name"] for value in item["techniques"])
                print(f"  {item['risk_score']:3d} {item['scope']} [{names}]")
        return 0
    except StrategyError as exc:
        print(f"[test_strategy] ✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
