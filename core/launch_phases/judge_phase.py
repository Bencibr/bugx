#!/usr/bin/env python3
"""judge_phase.py — 判读式并发审计的宿主编排（DOC-257）。

两段式命令，机器与 AI 各司其职：

``bugx engine judge``
    宿主确定性完成：core 批量 claim → 边界电池实跑 → artifact/收据落盘 →
    输出 N 个判读包提示词（宿主并发派给 judge 子代理，一回合出裁决）。

``bugx engine judge --settle <verdicts.json>``
    宿主确定性完成：解析裁决 → 实跑每条 finding 的复现命令取证 →
    构造 checkpoint payload → 提交 supervisor。全程零模型回合。

判读包与裁决持久化在 lifecycle ``state/judge/`` 下，中断可恢复；
claim 由宿主 core 通道预占，judge 子代理不持有任何租约。
"""

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

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

if __package__:
    from core.launch_phases.common import resolve_project_root, supervisor_state_path
else:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core.launch_phases.common import resolve_project_root, supervisor_state_path

from core.supervisor import audit_supervisor
from core.planning import judge_pipeline, unit_kind


def _judge_dir() -> Path:
    root = resolve_project_root()
    state = supervisor_state_path().parent
    judge = state / "judge"
    judge.mkdir(parents=True, exist_ok=True)
    return judge


def _pending_internal_units(supervisor: Any, limit: int) -> list[str]:
    """按调度优先序取可领取的内部单元（业务单元由门禁另行解锁）。"""
    state = supervisor.status()
    # unverified/partial/blocked 同样是待办债务：旧过滤器只认 pending/stale，
    # 导致电池无 passed 证据而降级的单元永远不会被重领（实测 8 个单元卡死）。
    pending = [
        unit for unit, status in (state.coverage or {}).items()
        if status in {"pending", "stale", "unverified", "partial", "blocked"}
        and unit_kind.is_internal(unit)
    ]
    try:
        from core.supervisor import task_scheduler
        sched = task_scheduler.shared_scheduler(resolve_project_root())
        return sched.rank_units(pending)[:limit]
    except Exception:
        return pending[:limit]


def _claim_business_flows(supervisor: Any, root: Path, rules: dict[str, Any]) -> int:
    """业务链路组合关单（DOC-257 续）：参与单元全 covered + 图边证据在案。

    judge v1 没有流程图探针；但自动业务链路是 project_graph 实测的调用边，
    两端单元若都已 covered，流本身可用"双端冒烟探针"（各自模块可导入）
    产出真实 passed 收据后机器关单，不再消耗判读回合。
    """
    from core.coverage import module_coverage
    state = supervisor.status()
    manifest = supervisor_state_path().parent / "module-coverage.md"
    if not manifest.is_file():
        return 0
    try:
        rows, _ = module_coverage.parse_manifest(manifest)
    except Exception:
        return 0
    by_number = {str(getattr(r, "number", "")): r for r in rows}
    coverage = state.coverage or {}
    number_to_unit: dict[str, str] = {}
    for unit in coverage:
        if unit_kind.is_internal(unit):
            num = unit.split(":", 2)[1]
            number_to_unit[num] = unit
    closed = 0
    for unit, status in list(coverage.items()):
        if not unit_kind.is_business(unit) or status == "covered":
            continue
        number = unit.split(":", 3)[1] if unit.count(":") >= 2 else ""
        row = by_number.get(number)
        if row is None:
            continue
        participants = [
            number_to_unit[p.strip()]
            for p in str(getattr(row, "participants", "") or "").split(",")
            if p.strip() in number_to_unit
        ]
        if not participants or any(coverage.get(u) != "covered" for u in participants):
            continue
        files = sorted({judge_pipeline.unit_parts(u)[0] for u in participants})
        worker_id = f"worker-judge-flow-{number.lower()}"
        claim = None
        try:
            claim = supervisor.claim_worker(worker_id, unit_ids=[unit], lease_seconds=600)
        except audit_supervisor.ClaimRejected:
            # 单元已被本轮早先的 claim 占用：复用该活动 claim 落账，
            # 不白等 1200s 租约过期（实测手工预领后组合关单被卡）。
            holder = next(
                (c for c in state.claims.values()
                 if unit in (c.unit_ids or []) and c.state == "active"),
                None,
            )
            if holder is None:
                continue
            worker_id = holder.worker_id
            claim = holder
        if getattr(claim, "execution_state", "running") == "claimed":
            supervisor.start_worker(worker_id, provider="judge-flow")
        modules = [judge_pipeline._module_name(f) for f in files if f.endswith(".py")]
        code = ("import importlib\n"
                + "\n".join(f"importlib.import_module({m!r})" for m in modules)
                + f"\nprint('flow_smoke_ok modules={len(modules)}')")
        ev = judge_pipeline.run_battery_probe(
            root, unit, root / claim.artifact_root, claim.lease_id, claim.execution_id,
            worker_id, state.run_id, state.target_fingerprint or supervisor.target_fingerprint,
            code, round_no=state.round_no, desc=f"flow-smoke {unit}",
        )
        if ev is None or ev.get("exit_code") != 0:
            continue
        supervisor.submit_checkpoint(
            worker_id,
            checkpoint_id=f"flow-{worker_id}",
            coverage={unit: "covered"},
            evidence={unit: ev},
            notes={unit: f"flow composed: participants covered + graph edge "
                         f"({getattr(row, 'evidence', '')})"},
            life_delta=0,
        )
        closed += 1
        print(f"[judge] ✓ 业务链路组合关单 {unit}")
    return closed


_LOG_ADAPTER = None


def _judge_log(event: str, **fields: Any) -> None:
    """结构化审计日志（JSONL，落 state/judge/judge-log.jsonl）。

    人读输出仍走 print；本函数只负责机器可读的事件流，便于事后逐跳复盘
    （此前日志只到终端，跨批次排查要靠翻 artifact 目录）。
    """
    global _LOG_ADAPTER
    try:
        record = {"ts": round(time.time(), 3), "event": event, **fields}
        path = _judge_dir() / "judge-log.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def collect(count: int = 8, claim_all: bool = False) -> int:
    """批量预占 + 电池取证 + 输出判读包。

    ``claim_all=True``（--all）：按文件分组的全部 pending 单元一次领完，
    宿主可一轮并发派发全部判读员，批次碎片化归零。
    """
    root = resolve_project_root()
    supervisor = audit_supervisor.Supervisor(root, state_path=supervisor_state_path())
    # 全量候选池：rank 只定优先序。按文件分组打包（DOC-257 打包修复）：
    # 同文件的全部 pending 单元并成一个 bundle claim——电池逐符号取证、
    # 一个判读包覆盖整文件。旧实现一文件一单元，同文件剩余单元必须等
    # settle 释放 file 锁，15 单元碎成 4 批（实测 B3=2、B4=1 个单元）。
    rules = judge_pipeline.load_rules(resolve_project_root() / ".bugx")
    units = _pending_internal_units(supervisor, 100_000)
    if not units:
        closed = _claim_business_flows(supervisor, resolve_project_root(), rules)
        if closed:
            print(f"[judge] 业务链路组合关单 {closed} 条")
        else:
            print("[judge] 无可领取内部单元（业务链路需内部 100% 或参与单元未覆盖）")
        return 0
    by_file: dict[str, list[str]] = {}
    order: list[str] = []
    for unit in units:
        file_path, _ = judge_pipeline.unit_parts(unit)
        if file_path not in by_file:
            order.append(file_path)
            by_file[file_path] = []
        by_file[file_path].append(unit)
    # L1 分类粒度修复：claim 前先按符号静态平凡性切分——文件里任一风险符号
    # 不应把同文件其余平凡符号捆进判读（旧行为整文件分类）。
    dangerous_units: set[str] = set()
    plans: list[tuple[str, list[str], bool]] = []
    for file_path in order:
        try:
            file_source = (root / file_path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            file_source = ""
        trivial_group: list[str] = []
        risky_group: list[str] = []
        for unit in by_file[file_path]:
            _, sym = judge_pipeline.unit_parts(unit)
            unit_slice = judge_pipeline.symbol_source(file_source, sym) if sym else file_source
            # 三级分级（单一真相源 risk_grade）：dangerous 命中风险 token 标签
            # （含反哺）或执行语料——AI 兜底必审级；unknown 仅是非白名单调用，
            # 可机器关单。旧实现把两者混为一谈，兜底会退化为全量送审。
            grade = judge_pipeline.risk_grade(unit_slice, rules=rules)
            if grade == "dangerous":
                dangerous_units.add(unit)
            (trivial_group if grade == "trivial" else risky_group).append(unit)
        for grp, is_risky in ((trivial_group, False), (risky_group, True)):
            if grp:
                plans.append((file_path, grp, is_risky))
    state = supervisor.status()
    run_id = state.run_id
    fingerprint = state.target_fingerprint or supervisor.target_fingerprint
    packages: list[dict[str, Any]] = []
    auto_cleaned = 0
    # 规则是项目级资产（与错题集同级）：跨生命周期/跨轮次累积，
    # 不随 lifecycle state 目录轮换丢失。
    started = time.time()
    # Phase A（串行）：claim/start 必须串行改 Supervisor 状态。
    claimed_jobs: list[dict[str, Any]] = []
    for file_path, unit_group, group_is_risky in plans:
        if not claim_all and len(claimed_jobs) >= max(1, count):
            break
        unit = unit_group[0]
        worker_id = f"worker-judge-{uuid.uuid4().hex[:8]}"
        try:
            claim = supervisor.claim_worker(
                worker_id, unit_ids=unit_group, lease_seconds=1800.0,
                allow_bundle=len(unit_group) > 1,
            )
        except audit_supervisor.ClaimRejected:
            # 文件被并发占用或 bundle 不可用：退回单单元领取，仍冲突则跳过。
            try:
                claim = supervisor.claim_worker(
                    worker_id, unit_ids=[unit], lease_seconds=1800.0,
                )
                unit_group = [unit]
            except audit_supervisor.ClaimRejected as exc:
                print(f"[judge] 跳过 {unit_group[0]}：{str(exc)[:120]}")
                continue
        supervisor.start_worker(worker_id, provider="judge-collect")
        claimed_jobs.append({
            "file_path": file_path,
            "unit_group": list(claim.unit_ids),
            "claim": claim,
            "worker_id": worker_id,
            "artifact_root": root / claim.artifact_root,
            "group_is_risky": group_is_risky,
            "dangerous": any(u in dangerous_units for u in claim.unit_ids),
        })
    # Phase B（并行）：电池是子进程 IO，组间无共享可变状态（各自
    # artifact_root），跨组并发执行——耗时主瓶颈（实测 collect 电池
    # 串行 ~13-16s/单元、一批 6-8 组 90-110s）由此收敛近线性。
    def _run_group_battery(job: dict[str, Any]) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for one_unit in job["unit_group"]:
            try:
                evidence.extend(judge_pipeline.run_battery(
                    root, one_unit, job["artifact_root"],
                    job["claim"].lease_id, job["claim"].execution_id,
                    job["worker_id"], run_id, fingerprint,
                    round_no=state.round_no, rules=rules,
                ))
            except Exception as exc:
                print(f"[judge] ⚠ 电池异常 {one_unit}：{str(exc)[:100]}")
        return evidence

    evidence_by_worker: dict[str, list[dict[str, Any]]] = {}
    if claimed_jobs:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(4, max(1, len(claimed_jobs))),
        ) as pool:
            futures = {
                pool.submit(_run_group_battery, job): job for job in claimed_jobs
            }
            for fut in concurrent.futures.as_completed(futures):
                job = futures[fut]
                try:
                    evidence_by_worker[job["worker_id"]] = fut.result()
                except Exception as exc:
                    evidence_by_worker[job["worker_id"]] = []
                    print(f"[judge] ⚠ 组电池失败 {job['worker_id']}：{str(exc)[:120]}")
    # Phase C（串行）：分类/机器关单/判读包组装（改 Supervisor 状态）。
    for job in claimed_jobs:
        file_path = job["file_path"]
        unit_group = job["unit_group"]
        claim = job["claim"]
        worker_id = job["worker_id"]
        evidence = evidence_by_worker.get(worker_id, [])
        try:
            file_source = (root / file_path).read_text(
                encoding="utf-8", errors="ignore")
        except OSError:
            file_source = ""
        auto_units: list[str] = []
        auto_routes: dict[str, dict[str, object]] = {}
        pre: dict[str, Any] = {"route": "judge", "signals": [], "sentinel": False}
        group_signals: set[str] = set()
        for one_unit in claim.unit_ids:
            _, sym = judge_pipeline.unit_parts(one_unit)
            unit_slice = judge_pipeline.symbol_source(file_source, sym) if sym else file_source
            ev_unit = [e for e in evidence if e.get("_unit_id") == one_unit] or evidence
            pre_u = judge_pipeline.classify_unit(unit_slice, ev_unit, rules)
            # 组级信号并集聚合（2026-11-08 复盘修复）：last-write-wins 会被
            # 组内最后一个无信号单元（如 circle_area）把前面单元命中的风险
            # 标签（如 run_formula 的 code-exec）整组冲掉，judge 失去风险
            # 上下文、AI 兜底也不触发。语义：组内任一单元命中即整组携带。
            group_signals |= set(pre_u.get("signals") or [])
            ev_unit_errors = sum(1 for e in ev_unit if e.get("exit_code") != 0)
            _judge_log(
                "classify", unit=one_unit, route=pre_u["route"],
                signals=pre_u.get("signals"), artifact_only=pre_u.get("artifact_only"),
                sentinel=pre_u.get("sentinel"), probes=len(ev_unit),
                errors=ev_unit_errors,
            )
            print(
                f"  [初审] {one_unit} → {pre_u['route']}"
                f"（probes={len(ev_unit)} errors={ev_unit_errors}"
                f" signals={pre_u.get('signals') or '-'}"
                f"{' artifact-only' if pre_u.get('artifact_only') else ''}"
                f"{' sentinel' if pre_u.get('sentinel') else ''}）"
            )
            if pre_u["route"] == "auto_clean":
                auto_units.append(one_unit)
                auto_routes[one_unit] = pre_u
            else:
                pre = pre_u  # 任一无辜单元不平凡 → 整组按该单元的信号送审
        pre["signals"] = sorted(group_signals)
        if len(auto_units) == len(claim.unit_ids):
            # 机器初审直接关单（DOC-257 pre-screen）：静态平凡 + 动态全过，
            # print("hello") 类单元零模型回合。artifact-only 组记 not_applicable
            # 终态（不可探测单元不得以 unverified 永久重领烧电池）。
            # AI 兜底（dangerous 级）：命中风险 token 标签（shell-exec/code-exec/
            # deserialization/raw-input/sql-concat，含反哺）或执行语料的组即使
            # 电池全是伪影也不机器关单——零行为证据 ≠ 无缺陷，交 judge 静态复核。
            # unknown 级（仅非白名单调用）维持机器关单：把"非白名单"当"危险"
            # 会让兜底退化为全量送审（2026-11-05 审计结论）。
            all_artifact_only = all(
                auto_routes[u].get("artifact_only") for u in auto_units
            )
            if job.get("dangerous") and all_artifact_only:
                backstop_signals = sorted({
                    str(sig)
                    for route in auto_routes.values()
                    for sig in (route.get("signals") or [])
                })
                print(
                    f"  [初审] {unit_group[0]} 组为 dangerous 级"
                    f"（signals={backstop_signals or '-'}）且仅伪影证据"
                    " → 送 AI 静态兜底复核（不机器关单）"
                )
                _judge_log(
                    "ai_backstop", unit=unit_group[0],
                    route="judge", signals=backstop_signals,
                    grade="dangerous",
                    artifact_only=True, sentinel=False, probes=len(evidence),
                    errors=sum(1 for e in evidence if e.get("exit_code") != 0),
                )
                job["backstop_signals"] = backstop_signals
            else:
                payload = judge_pipeline.checkpoint_payload_from_verdict(
                    {"worker_id": worker_id, "unit_ids": list(claim.unit_ids)},
                    {"verdict": "clean", "findings": [],
                     "artifact_only": all_artifact_only}, evidence, None,
                )
                supervisor.submit_checkpoint(
                    worker_id,
                    checkpoint_id=payload["checkpoint_id"],
                    coverage=payload["coverage"], evidence=payload["evidence"],
                    notes={u: "auto-clean: static trivial + dynamic pass"
                              " (DOC-257 pre-screen)"
                          for u in claim.unit_ids},
                    life_delta=0,
                )
                auto_cleaned += len(claim.unit_ids)
                continue
        unit = unit_group[0]
        package = judge_pipeline.build_package(unit, root, evidence)
        if package is None:
            package = {"schema_version": 1, "unit_id": unit, "file": "",
                       "symbol": "", "source": "", "observations": []}
        package["unit_group"] = list(claim.unit_ids)
        package["static_signals"] = pre["signals"]
        if job.get("backstop_signals"):
            # AI 兜底包必须携带风险上下文：judge 要知道为什么电池全伪影
            # 还被送来（dangerous 级命中了哪些标签），否则静态复核无的放矢。
            merged_signals = sorted(set(pre["signals"]) | set(job["backstop_signals"]))
            package["static_signals"] = merged_signals
            package["risk_backstop"] = {
                "grade": "dangerous",
                "signals": merged_signals,
                "reason": "电池证据全为伪影但源码命中风险 token 标签，"
                          "按 AI 兜底契约静态复核",
            }
        if pre.get("machine_flag"):
            package["machine_flag"] = pre["machine_flag"]
        package["_claim"] = {
            "worker_id": worker_id,
            "lease_id": claim.lease_id,
            "execution_id": claim.execution_id,
            "artifact_root": claim.artifact_root,
            "unit_ids": list(claim.unit_ids),
        }
        packages.append(package)
        if len(packages) >= max(1, count):
            break
    out = _judge_dir() / f"packages-{int(time.time())}.json"
    out.write_text(json.dumps(packages, ensure_ascii=False, indent=1), encoding="utf-8")
    battery_ms = round((time.time() - started) * 1000)
    budget = getattr(state, "budget", None)
    capacity = getattr(budget, "max_concurrency", None) if budget else None
    print(
        f"[judge] 已预占 {len(packages) + auto_cleaned} 单元"
        f"（机器初审直接关单 {auto_cleaned} 个，判读 {len(packages)} 个；"
        f"并发容量={capacity or 'auto'}），电池取证 {battery_ms}ms → {out}"
    )
    _judge_log("collect_summary", claimed=len(packages) + auto_cleaned,
               auto_cleaned=auto_cleaned, packages=len(packages),
               battery_ms=battery_ms, packages_file=str(out),
               claim_all=claim_all)
    for index, package in enumerate(packages, 1):
        prompt = judge_pipeline.package_prompt(package)
        print(f"\n----- [{index}/{len(packages)}] {package['unit_id']} -----")
        print(prompt)
    print(
        "\n[judge] 派发：把上述每个判读包提示词并发交给 judge 子代理"
        "（一回合出裁决 JSON，不给协议工具）。"
        "收齐后把各裁决 JSON 合并成数组写入一个文件，执行："
        "bugx engine judge --settle <verdicts.json>"
    )
    return 0


def _merge_fingerprint_variants(state: Any, findings: list[dict[str, Any]]) -> int:
    """L7：同一漏洞在跨批次/跨单元产生不同指纹时，按证据行重叠归并到主指纹。

    实测：同一 SQL 注入被 poc_sqli.main 与 users.find_user_by_name 各报一次，
    指纹 slug 不同（sql-injection-fstring-concat vs sqli-fstring-concat），
    导致重复计命。判据用 (path, line) 重叠而非字符串相似度——确定性且不误并。

    F-RSI-20261110-019 误并向修复：同 (path, line) 重叠只是**必要条件**——
    六假说探针 C 实测同点独立缺陷（SQL 注入 vs XSS 恰好同文件同行）会被
    无条件归并=丢 finding。补充**slug 语义词重叠**条件：两个指纹的最后一段
    短 slug 按 `-` 分词后必须共享至少一个核心 token（≥3 字符），同缺陷变体
    （sql-injection / sqli-injection 共享 sql+injection）仍归并，异类缺陷
    （sql-injection vs xss-reflected 无共享词）不再误并。
    """
    def ev_keys(finding: dict[str, Any]) -> set[tuple[str, int]]:
        # F-RSI-20261110-019 修漏并向：路径 os.path.normpath 归一化——
        # "./src/db.py" 与 "src/db.py" 是同一文件（别名不归一=同缺陷
        # 重复计命，六假说探针 A/B 实锤）。绝对路径不与相对路径合并
        # （无法可靠判定同文件，保守不归并——宁可漏不误并）。
        out = set()
        for item in finding.get("evidence") or []:
            if isinstance(item, dict) and item.get("path"):
                line = item.get("line")
                if isinstance(line, int):
                    path = os.path.normpath(str(item["path"]))
                    if not path.startswith("..") and not os.path.isabs(path):
                        out.add((path, line))
        return out

    def slug_tokens(fingerprint: str) -> set[str]:
        """指纹最后一段短 slug 的语义词集合（≥3 字符；过滤通用词）。"""
        slug = fingerprint.rsplit("::", 1)[-1]
        generic = {"src", "core", "py", "test", "tests", "main", "module"}
        return {
            tok.lower() for tok in re.split(r"[-_]", slug)
            if len(tok) >= 3 and tok.lower() not in generic
        }

    def tokens_related(t1: set[str], t2: set[str]) -> bool:
        """两词集合存在前缀关系 token 对（sql/sqli、eval/evaluate 同源变体）。"""
        return any(
            a.startswith(b) or b.startswith(a)
            for a in t1 for b in t2
        )

    merged = 0
    existing = [
        (str(f.get("fingerprint") or ""), ev_keys(f))
        for f in (getattr(state, "findings", None) or [])
        if f.get("fingerprint")
    ]
    for finding in findings:
        mine = ev_keys(finding)
        if not mine:
            continue
        mine_tokens = slug_tokens(str(finding.get("fingerprint") or ""))
        for fp, theirs in existing:
            if not fp or fp == finding.get("fingerprint") or not (mine & theirs):
                continue
            # 语义词重叠才归并：同 (path,line) 但语义无关的独立缺陷不并
            if tokens_related(mine_tokens, slug_tokens(fp)):
                finding["fingerprint"] = fp  # 归并到既有主指纹（同 key 即去重）
                merged += 1
                break
    return merged


def settle(verdicts_path: Path) -> int:
    """裁决收口：复现实跑取证 + 机械 checkpoint。"""
    root = resolve_project_root()
    supervisor = audit_supervisor.Supervisor(root, state_path=supervisor_state_path())
    raw = json.loads(Path(verdicts_path).read_text(encoding="utf-8"))
    verdicts = raw if isinstance(raw, list) else [raw]
    feedback = judge_pipeline.harvest_feedback(
        [v for v in verdicts if isinstance(v, dict)],
        resolve_project_root() / ".bugx",
    )
    state = supervisor.status()
    fingerprint = state.target_fingerprint or supervisor.target_fingerprint
    accepted = rejected = 0
    for verdict in verdicts:
        if not isinstance(verdict, Mapping) or not verdict.get("unit_id"):
            continue
        unit_id = str(verdict["unit_id"])
        claim_state = next(
            (c for c in state.claims.values()
             if unit_id in (c.unit_ids or []) and c.state == "active"),
            None,
        )
        if claim_state is None:
            # F-RSI-20261110-013：多单元 claim 的首个 verdict 已把 claim 推到
            # "submitted"——同 claim 其余单元的 verdict 走追加通道落账，
            # 不能因 claim 非 active 而跳过（单元会僵死在已探针未裁决）。
            # F-RSI-20261110-015：worker 前缀限制移除——dsh 等宿主原生 worker
            # （dsh-wN）提交后同单元的 verdict 同样走追加通道，前缀过滤会把
            # 非 judge 命名的原生 claim 排除在外。
            claim_state = next(
                (c for c in state.claims.values()
                 if unit_id in (c.unit_ids or []) and c.state == "submitted"),
                None,
            )
        if claim_state is None:
            print(f"[judge] 跳过 {unit_id}：无活动 judge claim（可能已过期）")
            continue
        worker_id = claim_state.worker_id
        battery_evidence = _battery_evidence_for(root, claim_state)
        requests = verdict.get("probe_requests")
        slug_u = judge_pipeline.unit_parts(unit_id)[0].replace("/", "_") or "unit"
        already_roundup = any(
            p.stem == slug_u for p in _judge_dir().glob("roundup-*/*.json"))
        if already_roundup:
            requests = None  # 服务端一跳上限：该单元已定向探针过，禁止二跳
        if (verdict.get("verdict") == "insufficient_data"
                and requests and not verdict.get("probe_round")):
            # 定向探针一跳：宿主执行 judge 指定的探针，把增广证据打包回给
            # judge 复审；本单元本轮暂不结算（避免 insufficient 冒充覆盖）。
            probe_evidence = judge_pipeline.run_probe_requests(
                root, unit_id, root / claim_state.artifact_root,
                claim_state.lease_id, claim_state.execution_id,
                worker_id, state.run_id,
                state.target_fingerprint or supervisor.target_fingerprint,
                requests,
                round_no=int(getattr(claim_state, "round_no", 1) or 1),
            )
            if probe_evidence:
                augmented = judge_pipeline.build_package(
                    unit_id, root, battery_evidence + probe_evidence)
                if augmented is not None:
                    augmented["probe_round"] = 1
                    augmented["_claim"] = {
                        "worker_id": worker_id,
                        "lease_id": claim_state.lease_id,
                        "execution_id": claim_state.execution_id,
                        "artifact_root": claim_state.artifact_root,
                        "unit_ids": list(claim_state.unit_ids),
                    }
                    out_dir = _judge_dir() / f"roundup-{int(time.time())}"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    slug = judge_pipeline.unit_parts(unit_id)[0].replace("/", "_") or "unit"
                    (out_dir / f"{slug}.json").write_text(
                        json.dumps(augmented, ensure_ascii=False, indent=1),
                        encoding="utf-8")
                    print(f"[judge] ↻ {unit_id} 定向探针 {len(probe_evidence)} 条已取证，"
                          f"增广包 → {out_dir}（待复审）")
                    continue
        reproduction = None
        for item in (verdict.get("findings") or []):
            if isinstance(item, Mapping) and item.get("reproduction"):
                reproduction = judge_pipeline.execute_reproduction(
                    root, str(item["reproduction"]),
                    root / claim_state.artifact_root,
                    claim_state.lease_id, claim_state.execution_id,
                    worker_id, state.run_id, fingerprint,
                    round_no=int(getattr(claim_state, "round_no", 1) or 1),
                )
                if reproduction is not None:
                    break
        payload = judge_pipeline.checkpoint_payload_from_verdict(
            {"worker_id": worker_id, "unit_ids": list(claim_state.unit_ids),
             "primary_unit": unit_id},
            verdict, battery_evidence, reproduction,
        )
        if payload is None:
            print(f"[judge] {unit_id}：无可绑定证据，标 blocked")
            supervisor.submit_checkpoint(
                worker_id,
                checkpoint_id=f"judge-{worker_id}-blocked",
                coverage={unit_id: "blocked"},
                notes={unit_id: "judge battery produced no bindable evidence"},
            )
            rejected += 1
            continue
        merged_n = 0
        if payload["findings"]:
            merged_n = _merge_fingerprint_variants(state, payload["findings"])
            if merged_n:
                print(f"[judge] ↺ {unit_id}: {merged_n} 条指纹变体归并到既有主指纹")
        verdict_started = time.time()
        try:
            supervisor.submit_checkpoint(
                worker_id,
                checkpoint_id=payload["checkpoint_id"],
                coverage=payload["coverage"],
                evidence=payload["evidence"],
                findings=payload["findings"],
                life_delta=0,
                append_verdict=True,
            )
            accepted += 1
            bugs = len(payload["findings"])
            elapsed_ms = round((time.time() - verdict_started) * 1000)
            print(f"[judge] ✓ {unit_id} verdict={verdict.get('verdict')} "
                  f"findings={bugs} {elapsed_ms}ms")
            _judge_log("settle_ok", unit=unit_id, verdict=verdict.get("verdict"),
                       findings=bugs, merged=merged_n if payload["findings"] else 0,
                       elapsed_ms=elapsed_ms)
        except audit_supervisor.SupervisorError as exc:
            print(f"[judge] ✗ {unit_id}: {exc}")
            rejected += 1
    print(f"[judge] 收口完成：accepted={accepted} rejected={rejected}")
    print(
        "[judge] 规则反哺：risk_tokens+" + str(feedback.get("risk_tokens", 0))
        + " safe_calls候选+" + str(feedback.get("safe_calls_new", 0))
        + " artifact候选+" + str(feedback.get("artifact_markers_new", 0))
        + " 晋升=" + str(feedback.get("promoted", 0))
        + " 拒收=" + str(feedback.get("rejected", 0))
    )
    _judge_log("settle_summary", accepted=accepted, rejected=rejected,
               feedback=feedback)
    return 0 if rejected == 0 else 1


def _battery_evidence_for(root: Path, claim: Any) -> list[dict[str, Any]]:
    """从 claim 的 artifact_root 重读电池 evidence（中断恢复用）。"""
    evidence: list[dict[str, Any]] = []
    artifact_root = root / claim.artifact_root
    for path in sorted(artifact_root.glob("probe-*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rel = path.relative_to(root).as_posix()
        import hashlib
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        status = "passed" if record.get("exit_code") == 0 else "error"
        evidence.append({
            "_unit_id": str(record.get("unit_id") or ""),
            "command": record.get("command") or " ".join(record.get("argv") or []),
            "exit_code": int(record.get("exit_code") or 0),
            "artifact_path": rel,
            "artifact_sha256": sha,
            "target_fingerprint": record.get("target_fingerprint") or "",
            "timestamp": record.get("timestamp") or time.time(),
            "worker_id": claim.worker_id,
            "run_id": record.get("run_id") or "",
            "round_no": int(getattr(claim, "round_no", 1) or 1),
            "lease_id": claim.lease_id,
            "status": status,
            "execution_receipt": {
                "schema_version": 1,
                "execution_id": claim.execution_id,
                "worker_id": claim.worker_id,
                "lease_id": claim.lease_id,
                "command": record.get("command") or "",
                "exit_code": int(record.get("exit_code") or 0),
                "artifact_path": rel,
                "artifact_sha256": sha,
                "status": status,
            },
        })
    return evidence


def settle_dir(path: Path) -> int:
    """收割目录下全部 *.json 裁决文件并统一收口（judge 自写 verdict 专用）。"""
    import glob
    files = sorted(Path(path).glob("*.json"))
    if not files:
        print(f"[judge] {path} 下没有裁决文件")
        return 1
    verdicts: list[dict[str, Any]] = []
    skipped_no_verdict: list[str] = []
    for file in files:
        data: dict[str, Any] | list[dict[str, Any]] | None = None
        try:
            raw = file.read_text(encoding="utf-8")
        except OSError:
            print(f"[judge] 跳过无法读取的裁决: {file.name}")
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            # judge 自写裁决常见裸控制字符——宽容复解析，避免静默跳过真裁决。
            try:
                data = json.loads(raw, strict=False)
            except ValueError:
                print(f"[judge] 跳过无法解析的裁决: {file.name}")
                continue
        if data is None:
            continue
            continue
        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            # 目录收割只认显式 verdict 字段。带 unit_id 但缺
            # verdict 的 JSON 是判读包/包片段等噪声——此前 parse_verdict 会把
            # 缺省裁决兜成 insufficient_data 并按 0 findings 关单，目录里残留
            # 的陈旧包按文件序先消费就抢走真裁决的单元；因此不得复用旧目录。
            if isinstance(entry, dict) and entry.get("unit_id"):
                if not entry.get("verdict"):
                    skipped_no_verdict.append(str(entry.get("unit_id")))
                    continue
                verdicts.append(judge_pipeline.parse_verdict(
                    json.dumps(entry, ensure_ascii=False), entry["unit_id"]))
            elif isinstance(entry, dict) and not entry.get("verdict"):
                skipped_no_verdict.append(file.name)
    merged = _judge_dir() / f"verdicts-{int(time.time())}.json"
    merged.write_text(json.dumps(verdicts, ensure_ascii=False), encoding="utf-8")
    print(f"[judge] 收割 {len(verdicts)} 份裁决 → {merged}")
    if skipped_no_verdict:
        print(f"[judge] 跳过缺 verdict 字段的非裁决文件 {len(skipped_no_verdict)} 个"
              f"（判读包/噪声不参与关单）: {', '.join(skipped_no_verdict[:6])}")
    _judge_log("settle_dir", files=[f.name for f in files], verdicts=len(verdicts),
               skipped_no_verdict=len(skipped_no_verdict), merged=str(merged))
    result = settle(merged)
    processed = Path(path) / "processed"
    processed.mkdir(exist_ok=True)
    for file in files:
        try:
            file.rename(processed / file.name)
        except OSError:
            pass
    return result


def main(argv: list[str]) -> int:
    if "--settle-dir" in argv:
        index = argv.index("--settle-dir")
        if index + 1 >= len(argv):
            print("用法：bugx engine judge --settle-dir <dir>")
            return 2
        return settle_dir(Path(argv[index + 1]))
    claim_all = "--all" in argv
    if "--settle" in argv:
        index = argv.index("--settle")
        if index + 1 >= len(argv):
            print("用法：bugx engine judge --settle <verdicts.json>")
            return 2
        return settle(Path(argv[index + 1]))
    count = 8
    for item in argv:
        if str(item).isdigit():
            count = int(item)
    return collect(count, claim_all=claim_all)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
