#!/usr/bin/env python3
"""core/ledger/cli.py - Lesson Ledger 命令行工具入口 (DOC-036)。"""

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
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


_CURRENT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _CURRENT_DIR.parent
_REPO_ROOT = _CORE_DIR.parent
for _p in (str(_REPO_ROOT), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# DES-02：原先这里的 ``_LedgerProxy`` 经 ``sys.modules.get("lesson_ledger")``
# 反向取符号，与顶层 Facade 构成双向依赖环；现改为经同级解析表
# （``core/ledger/_facade.py``）取**定义该符号的兄弟模块**，且延迟导入，
# 因此 ``ledger/*`` 不再依赖 ``lesson_ledger``，方向变为单向。
from core.ledger._facade import LedgerProxy

_ledger = LedgerProxy()
def _review_payload_from_findings(path: Path, lifecycle_id: str | None, round_no: int | None) -> dict[str, Any]:
    """Build the exact cycle-review counts from a findings gate payload."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
        raise LessonLedgerError("--from-findings 必须指向包含 findings 数组的 JSON")
    lifecycle = lifecycle_id or payload.get("lifecycle_id")
    current_round = round_no if round_no is not None else payload.get("round")
    if not isinstance(lifecycle, str) or not lifecycle.strip():
        raise LessonLedgerError("--from-findings 缺少 lifecycle_id，请显式提供 --lifecycle-id")
    if not isinstance(current_round, int) or current_round < 1:
        raise LessonLedgerError("--from-findings 缺少有效 round，请显式提供 --round")
    confirmed = [
        item for item in payload["findings"]
        if isinstance(item, dict)
        and isinstance(item.get("confirmation"), dict)
        and item["confirmation"].get("state") == "confirmed"
    ]
    # PE-20261005-004 集成：与结算侧 credited_cap() 同源，避免 BUGX_CREDITED_CAP
    # 生效时复盘 credited_count 与结算口径分叉。
    try:
        from core.launch_phases.finding_settlement import credited_cap
        credited = min(len(confirmed), credited_cap())
    except ImportError:
        credited = min(len(confirmed), 5)
    try:
        from .life_ledger import finding_fingerprint
        fingerprints = [finding_fingerprint(item) for item in confirmed[:credited]]
    except (ImportError, TypeError, ValueError) as exc:
        raise LessonLedgerError(f"无法为 confirmed finding 计算指纹: {exc}") from exc
    return {
        "actor": "supervisor",
        "lifecycle_id": lifecycle.strip(),
        "round": current_round,
        "finding_count": len(confirmed),
        "credited_count": credited,
        "lesson_ids": [],
        "finding_fingerprints": fingerprints,
        "new_lessons": [],
        "no_new_lesson_reason": "auto: 本轮未提供新增 candidate pattern",
    }

# DOC-212：与 schema.DEFAULT_LEDGER 保持同值哨兵；锚定解析统一走
# schema.resolve_default_ledger（main 入口对 --ledger 默认值应用一次）。
DEFAULT_LEDGER = Path(".bugx/knowledge/lesson-ledger.json")
DEFAULT_LIMIT = 6
DEFAULT_TOKEN_BUDGET = 1500
DEFAULT_BYTE_BUDGET = 12000

class _ErrorMeta(type):
    def __instancecheck__(cls, instance):
        return isinstance(instance, _ledger.LessonLedgerError)
    def __subclasscheck__(cls, subclass):
        return issubclass(subclass, _ledger.LessonLedgerError)

class LessonLedgerError(Exception, metaclass=_ErrorMeta):
    def __new__(cls, *args, **kwargs):
        return _ledger.LessonLedgerError(*args, **kwargs)

def database_path(*args, **kwargs): return _ledger.database_path(*args, **kwargs)
def initialize(*args, **kwargs): return _ledger.initialize(*args, **kwargs)
def metrics(*args, **kwargs): return _ledger.metrics(*args, **kwargs)
def __getattr__(name: str) -> Any:
    return getattr(_ledger, name)


def main(argv: list[str] | None = None) -> int:
    gl = globals()
    for _k in dir(_ledger):
        if not _k.startswith("__") and _k not in gl:
            gl[_k] = getattr(_ledger, _k)

    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("command",choices=("init","bootstrap","migrate","validate","validate-deep","rebuild-fts","archive-events","retrieve","consume","record","review","feedback","metrics","hygiene","merge","retire","revoke","export","backup","incident-add","incident-list","incident-lookup","evidence-add","incident-archive","incident-activate","incident-revoke","tool-feedback","tool-metrics","tool-lookup","tool-matrix","tool-exp-export","tool-exp-import","tool-exp-activate","export-book","promote","promote-incident","promote-tool","inherit","synthesize-skill")); parser.add_argument("--skills-dir",type=Path,help="自动合成 Skill 包的目标目录"); parser.add_argument("--ledger",type=Path,default=DEFAULT_LEDGER); parser.add_argument("--seed",type=Path); parser.add_argument("--json",action="store_true"); parser.add_argument("--query",default=""); parser.add_argument("--category"); parser.add_argument("--limit",type=int,default=DEFAULT_LIMIT); parser.add_argument("--offset",type=int,default=0); parser.add_argument("--token-budget",type=int,default=DEFAULT_TOKEN_BUDGET); parser.add_argument("--byte-budget",type=int,default=DEFAULT_BYTE_BUDGET); parser.add_argument("--exclude-lesson-id",action="append",default=[]); parser.add_argument("--input",type=Path); parser.add_argument("--from-findings",type=Path,help="review：从当前 findings.json 自动派生 cycle-review"); parser.add_argument("--supersede",action="store_true",help="review：显式替换同 lifecycle_id + round 的既有 review（跨 polling 撞键自愈）"); parser.add_argument("--output",type=Path); parser.add_argument("--keep-tail",type=int,default=0); parser.add_argument("--older-than-days",type=int,default=90); parser.add_argument("--max-archive",type=int,default=50); parser.add_argument("--category-value",dest="record_category"); parser.add_argument("--title"); parser.add_argument("--root-cause"); parser.add_argument("--reflection"); parser.add_argument("--probe",action="append",default=[]); parser.add_argument("--source-ref",action="append",default=[]); parser.add_argument("--finding-fingerprint"); parser.add_argument("--finding",type=Path,help="hit 反馈对应的已确认 finding JSON"); parser.add_argument("--tool",action="append",default=[],help="tool-feedback 归因的工具名，可重复"); parser.add_argument("--signature",action="append",default=[]); parser.add_argument("--summary"); parser.add_argument("--context"); parser.add_argument("--before",dest="before_snippet"); parser.add_argument("--after",dest="after_snippet"); parser.add_argument("--diff-ref"); parser.add_argument("--commit-ref"); parser.add_argument("--decision"); parser.add_argument("--duration-seconds",type=float,default=0.0); parser.add_argument("--pattern-id"); parser.add_argument("--lesson-id"); parser.add_argument("--incident-id"); parser.add_argument("--lifecycle-id"); parser.add_argument("--run-id"); parser.add_argument("--round",type=int); parser.add_argument("--outcome"); parser.add_argument("--event-id"); parser.add_argument("--subject-type"); parser.add_argument("--subject-id"); parser.add_argument("--event-type"); parser.add_argument("--evidence-receipt-id"); parser.add_argument("--source-pattern-id"); parser.add_argument("--target-pattern-id"); parser.add_argument("--reason"); parser.add_argument("--min-feedback",type=int,default=3); parser.add_argument("--global-ledger",type=Path,help="promote/inherit 的全局错题集路径（默认 ~/.bugx/knowledge/global.json）"); args=parser.parse_args(argv)
    # DOC-212：入口一次性锚定默认账本哨兵（读/写命令一致），结果统一回显
    # 真实落盘路径（可观测：宿主一眼核对落在哪个账本）。
    from core.ledger.schema import resolve_default_ledger as _resolve_default_ledger
    args.ledger = _resolve_default_ledger(args.ledger)
    try:
        if args.command in {"init","bootstrap"}:
            existed = database_path(args.ledger).is_file()
            initialize(args.ledger,seed=args.seed)
            result={"status":"exists" if existed else "created","ledger":str(database_path(args.ledger)),"metrics":metrics(args.ledger)}
        elif args.command=="migrate": result={"status":"migrated",**_ledger.migrate(args.ledger)}
        elif args.command=="validate":
            state = _ledger.read(args.ledger); result={"status":"valid",**{k:state[k] for k in ("revision","state_hash","head_hash","database")}}
        elif args.command=="validate-deep": result=_ledger.validate_deep(args.ledger)
        elif args.command=="rebuild-fts": result=_ledger.rebuild_fts(args.ledger)
        elif args.command=="archive-events":
            if args.output is None: raise LessonLedgerError("archive-events 必须提供 --output（归档目录）")
            result=_ledger.archive_events(args.ledger,args.output,keep_tail=args.keep_tail)
        elif args.command=="tool-exp-export":
            result={"status":"ok","records":_ledger.export_tool_experience(args.ledger)}
        elif args.command=="tool-exp-import":
            if args.input is None: raise LessonLedgerError("tool-exp-import 必须提供 --input")
            payload=json.loads(args.input.read_text(encoding="utf-8"))
            records=payload.get("records") if isinstance(payload,dict) else payload
            result=_ledger.import_tool_experience(args.ledger,records or [])
        elif args.command=="tool-exp-activate":
            if not args.tool or not args.category:
                raise LessonLedgerError("tool-exp-activate 必须提供 --tool 和 --category")
            result=_ledger.activate_tool_experience(args.ledger, args.tool[0], args.category)
        elif args.command=="retrieve": result={"status":"ok","lessons":_ledger.retrieve(args.ledger,args.query,args.category,args.limit,token_budget=args.token_budget,byte_budget=args.byte_budget,exclude_lesson_ids=args.exclude_lesson_id)}
        elif args.command=="consume":
            payload = json.loads(args.input.read_text(encoding="utf-8")) if args.input else {
                "event_id": args.event_id, "subject_type": args.subject_type,
                "subject_id": args.subject_id, "event_type": args.event_type,
                "lifecycle_id": args.lifecycle_id, "run_id": args.run_id,
            }
            result = _ledger.record_consumption_event(args.ledger, payload)
        elif args.command=="record":
            payload=json.loads(args.input.read_text(encoding="utf-8")) if args.input else {"category":args.record_category or args.category,"title":args.title,"root_cause":args.root_cause,"reflection":args.reflection,"probes":args.probe,"source_refs":args.source_ref,"finding_fingerprints":[args.finding_fingerprint] if args.finding_fingerprint else []}; result=_ledger.add_lesson(args.ledger,payload)
        elif args.command=="review":
            if args.from_findings is not None:
                if args.input is not None:
                    raise LessonLedgerError("review 不能同时使用 --input 与 --from-findings")
                payload = _review_payload_from_findings(
                    args.from_findings, args.lifecycle_id, args.round,
                )
            else:
                if args.input is None:
                    raise LessonLedgerError("review 必须提供 --input 或 --from-findings")
                payload = json.loads(args.input.read_text(encoding="utf-8"))
            if args.supersede:
                payload["supersede"] = True
            result=_ledger.apply_review(args.ledger,payload)
        elif args.command=="feedback":
            feedback = {"lifecycle_id":args.lifecycle_id,"round":args.round,"lesson_id":args.lesson_id,"finding_fingerprint":args.finding_fingerprint,"outcome":args.outcome,"run_id":args.run_id}
            if args.signature:
                feedback["signature"] = args.signature[0]
            if args.finding:
                feedback["finding"] = json.loads(args.finding.read_text(encoding="utf-8"))
            result = _ledger.record_feedback(args.ledger, feedback)
        elif args.command=="incident-add":
            if args.input:
                payload = json.loads(args.input.read_text(encoding="utf-8"))
            else:
                payload = {
                    "signature": (args.signature or [None])[0], "summary": args.summary,
                    "context": args.context, "before_snippet": args.before_snippet,
                    "after_snippet": args.after_snippet, "diff_ref": args.diff_ref,
                    "commit_ref": args.commit_ref, "decision": args.decision,
                    "duration_seconds": args.duration_seconds,
                    "lifecycle_id": args.lifecycle_id, "round": args.round,
                    "pattern_id": args.pattern_id,
                }
            result = _ledger.add_incident(args.ledger, payload)
        elif args.command=="incident-lookup":
            result = {
                "status": "ok",
                "incidents": _ledger.lookup_incidents(
                    args.ledger, query=args.query, signatures=args.signature,
                    limit=args.limit, token_budget=args.token_budget,
                    byte_budget=args.byte_budget,
                ),
            }
        elif args.command=="incident-list":
            result = {
                "status": "ok",
                "incidents": _ledger.list_incidents(
                    args.ledger, pattern_id=args.pattern_id,
                    limit=args.limit, offset=args.offset,
                ),
            }
        elif args.command=="evidence-add":
            if args.input is None:
                raise LessonLedgerError("evidence-add 必须提供 --input")
            result = _ledger.record_evidence_receipt(
                args.ledger, json.loads(args.input.read_text(encoding="utf-8")),
            )
        elif args.command=="incident-activate":
            result = _ledger.activate_incident(args.ledger, args.incident_id, evidence_receipt_id=args.evidence_receipt_id)
        elif args.command=="tool-feedback":
            if args.input:
                payload = json.loads(args.input.read_text(encoding="utf-8"))
            else:
                payload = {
                    "lifecycle_id": args.lifecycle_id, "round": args.round,
                    "category": args.category, "tools": args.tool,
                    "outcome": args.outcome,
                    "finding_fingerprint": args.finding_fingerprint,
                }
                if args.finding:
                    payload["finding"] = json.loads(args.finding.read_text(encoding="utf-8"))
            result = _ledger.record_tool_feedback(args.ledger, payload)
        elif args.command=="tool-metrics": result=_ledger.tool_metrics(args.ledger)
        elif args.command=="tool-lookup":
            result={"status":"ok","tools":_ledger.lookup_tools(args.ledger,query=args.query,category=args.category,limit=args.limit,token_budget=args.token_budget,byte_budget=args.byte_budget)}
        elif args.command=="tool-matrix": result=_ledger.tool_matrix(args.ledger)
        elif args.command=="export-book":
            if args.output is None: raise LessonLedgerError("export-book 必须提供 --output")
            result=_ledger.export_book(args.ledger,args.output)
        elif args.command=="incident-archive":
            result=_ledger.archive_incidents(args.ledger,older_than_days=args.older_than_days,max_archive=args.max_archive)
        elif args.command=="hygiene": result=_ledger.hygiene(args.ledger,min_feedback=args.min_feedback)
        elif args.command=="merge": result=_ledger.merge_patterns(args.ledger,args.source_pattern_id,args.target_pattern_id,args.reason)
        elif args.command=="retire": result=_ledger.retire_pattern(args.ledger,args.lesson_id,args.reason)
        elif args.command=="revoke": result=_ledger.revoke_pattern(args.ledger,args.lesson_id,args.reason)
        elif args.command=="incident-revoke": result=_ledger.revoke_incident(args.ledger,args.incident_id,args.reason)
        elif args.command=="export":
            if args.output is None: raise LessonLedgerError("export 必须提供 --output")
            result=_ledger.export_snapshot(args.ledger,args.output)
        elif args.command=="backup":
            if args.output is None: raise LessonLedgerError("backup 必须提供 --output")
            result=_ledger.backup_database(args.ledger,args.output)
        elif args.command=="promote":
            if not args.pattern_id and not args.lesson_id: raise LessonLedgerError("promote 必须提供 --pattern-id 或 --lesson-id")
            g_ledger = args.global_ledger or (Path.home() / ".bugx" / "knowledge" / "global.json")
            result=_ledger.promote_pattern(args.ledger, g_ledger, args.pattern_id or args.lesson_id)
        elif args.command=="promote-incident":
            if not args.incident_id or not args.evidence_receipt_id:
                raise LessonLedgerError("promote-incident 必须提供 --incident-id 和 --evidence-receipt-id")
            g_ledger = args.global_ledger or (Path.home() / ".bugx" / "knowledge" / "global.json")
            result=_ledger.promote_incident(args.ledger, g_ledger, args.incident_id, args.evidence_receipt_id)
        elif args.command=="promote-tool":
            if not args.tool or not args.category:
                raise LessonLedgerError("promote-tool 必须提供 --tool 和 --category")
            g_ledger = args.global_ledger or (Path.home() / ".bugx" / "knowledge" / "global.json")
            result=_ledger.promote_tool_experience(args.ledger, g_ledger, args.tool[0], args.category)
        elif args.command=="inherit":
            g_ledger = args.global_ledger or (Path.home() / ".bugx" / "knowledge" / "global.json")
            result=_ledger.inherit_patterns(args.ledger, g_ledger)
        elif args.command=="synthesize-skill":
            if not args.incident_id:
                raise LessonLedgerError("synthesize-skill 必须提供 --incident-id")
            result=_ledger.synthesize_skill_pack(args.ledger, args.incident_id, skills_dir=args.skills_dir)
        else: result=metrics(args.ledger)
        if isinstance(result, dict):
            result.setdefault("ledger_path", str(args.ledger))
        print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
    except (LessonLedgerError,OSError,sqlite3.Error, json.JSONDecodeError) as exc: print(f"[lesson_ledger] ✗ {exc}",file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
