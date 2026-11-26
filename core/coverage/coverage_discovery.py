#!/usr/bin/env python3
"""覆盖清单的**发现单元与业务切面**原语（DES-07 二期）。

本模块由 ``module_coverage.py`` 拆分而来，承载「发现」与「业务发现」两簇。它们
只依赖 ``coverage_manifest`` 的叶子原语与 ``symbol_index`` / ``project_graph``，
彼此单向（业务簇 → 发现簇），且**不依赖** ``module_coverage`` 里的对账门禁、
校验、自动回填、命令入口四簇。依赖方向因此是单向无环：

    module_coverage → coverage_discovery → coverage_manifest

``module_coverage`` 会**再导出**这里的全部公开名字，既有调用方
（``coverage_bootstrap``、``launch_phases.*``、``check`` / ``status`` 等）无需改动。

* D 发现单元 —— ``discover_minimum_unit_paths`` / ``discover_asset_unit_paths`` /
  ``discover_audit_unit_paths`` / ``discovery_units`` / ``discovery_rows`` /
  ``discovery_row`` / ``row_text`` / ``scope_path`` / ``coverage_unit_id`` /
  ``evidence_manifest_errors`` / ``participant_ids`` / ``asset_units``
* F 业务发现 —— ``extract_feature_surface`` / ``announce_feature_surface`` /
  ``business_discovery`` / ``business_discovery_rows`` / ``graph_scope`` /
  ``PassContext`` / ``coverage_summary``（含端点/CLI 模式常量）

恢复结算合账语义（DIM3，2026-11-17 用户裁决"新证据为准 + chain 保留"）的判定
原语也落在本模块：``inherited_unit_coverage_verdicts``（维度 1 链判定）与
``recovery_closeout_coverage_census``（维度 2 普查）。单元证据分三层——L1 本轮
本地记录 / L2 本 Life 历史本地记录 / L3 沿链继承（``inherited_from`` 备注 +
``resolve_inherited_evidence`` 溯源）；合账判定读取优先级为 L1 > L2 > L3，继承
chain 只作历史层留痕、不参与判定权威，双记录并存不互写背书。语义细则与扩展点
见 ``docs/08_internal/01_standards/RECOVERY_SETTLEMENT_SEMANTICS_2026-11-17.md``。
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from core.runtime import project_graph
from core.retrieval import symbol_index

from core.coverage.coverage_manifest import EvidenceProbe, ModuleRow, parse_manifest
# unit_platform 属 platform 簇（七簇层次 platform(0) <- discovery(1)）。
# 原先误写为从 coverage_manifest 导入，导致 ImportError：
# 18 个文件受影响，module_coverage 无法导入，pre/post 端到端全失败。
from core.coverage.coverage_platform import unit_platform


def discover_minimum_unit_paths(root: Path, strict: bool = False) -> set[str]:
    """递归发现产品源码文件，作为最小单元清单的机械下限。"""
    return {
        path.relative_to(root.resolve()).as_posix()
        for path in symbol_index.discover_source_paths(root, strict=strict)
    }


def discover_asset_unit_paths(
    root: Path, strict: bool = False, asset_paths: list[Path] | None = None,
) -> set[str]:
    """Return configuration/dependency/build/CI/deployment assets in scope.

    ``asset_paths`` 允许调用方复用同一次校验内已经枚举好的资产列表，避免在
    ``discover_audit_unit_paths`` 与 ``discovery_units`` 里各走一遍全仓遍历。
    """
    if asset_paths is None:
        asset_paths = symbol_index.discover_asset_paths(root, strict=strict)
    return {
        path.relative_to(root.resolve()).as_posix()
        for path in asset_paths
    }


def discover_audit_unit_paths(
    root: Path, strict: bool = False, asset_paths: list[Path] | None = None,
) -> set[str]:
    """Return the file-level lower bound for source and behavior assets."""
    return discover_minimum_unit_paths(root, strict=strict) | discover_asset_unit_paths(
        root, strict=strict, asset_paths=asset_paths,
    )


def scope_path(path: str) -> str:
    """把 ``file::symbol`` 归一到源码文件路径。"""
    return path.strip().strip("`").rstrip("/").split("::", 1)[0]


def coverage_unit_id(row: ModuleRow) -> str:
    """Return the stable unit id used by ``coverage_evidence`` submissions."""
    scope = row.path.strip().strip("`")
    if not scope or scope in {"-", "待定"}:
        scope = row.name.strip()
    return f"{row.phase}:{row.number}:{scope}"


def cached_unit_risk_score(
    file_path: str,
    root: Path,
    content_hash: str,
    cache: dict[tuple[str, str, str], float | None],
) -> float | None:
    """Analyze one source snapshot once per discovery/reconciliation operation."""
    key = (str(root.resolve()), file_path, content_hash)
    if key not in cache:
        try:
            from core.repair import test_strategy

            cache[key] = test_strategy.calculate_unit_risk_score(file_path, root)
        except Exception:
            # Risk is prioritization metadata, not coverage evidence. Preserve the
            # existing default-difficulty behavior and don't retry per symbol row.
            cache[key] = None
    return cache[key]


# F3'（对抗审计重设计）：跨 Session 继承覆盖的备注锚点，与
# ``coverage_bootstrap.apply_inherited_coverage`` 写入的
# ``inherited_from=<polling_id>/<lifecycle_id>`` 同形。
INHERITED_FROM_RE = re.compile(
    r"inherited_from=(polling-[0-9]{6,})/(lifecycle-[0-9]{6,})"
)


def inherited_evidence_reference(note: str) -> tuple[str, str] | None:
    """从清单行备注解析 ``inherited_from=<polling>/<lifecycle>`` 引用。"""
    match = INHERITED_FROM_RE.search(note or "")
    if match is None:
        return None
    return match.group(1), match.group(2)


def inheritable_scope(row: object) -> str:
    """跨 Session 稳定的内部单元身份（与 coverage_bootstrap 同一口径：scope）。"""
    scope = (getattr(row, "path", "") or "").strip().strip("`").rstrip("/")
    if not scope or scope in {"-", "待定", "N/A"}:
        return ""
    return scope


# 继承链溯源深度上限：只为防环与病态链路兜底，正常链条（每个 lifecycle 一代）
# 远达不到该值；超过即按完整性异常 fail-closed。
MAX_INHERITED_CHAIN_DEPTH = 32


def _covered_source_verdict(
    source_state: Path, scope: str,
) -> tuple[str, str, str]:
    """Resolve the newest covered manifest verdict for ``scope`` in one hop.

    返回 ``(unit_id, note, reason)``：成功时 ``unit_id`` 非空且 ``reason`` 为空，
    ``note`` 是该清单行备注（供继承链继续溯源）；失败时 ``unit_id`` 为空且
    ``reason`` 说明拒绝原因。
    """
    source_manifest = source_state / "module-coverage.md"
    if source_manifest.is_symlink() or not source_manifest.is_file():
        return "", "", f"源清单缺失: {source_manifest}"
    try:
        source_rows, parse_errors = parse_manifest(source_manifest)
    except (OSError, UnicodeError, ValueError) as exc:
        return "", "", f"源清单无法解析: {source_manifest}: {exc}"
    if parse_errors:
        return "", "", f"源清单无效: {source_manifest}: {'; '.join(parse_errors[:3])}"
    source_unit_id = ""
    source_note = ""
    for source_row in source_rows:
        if (
            getattr(source_row, "status", "") == "已覆盖"
            and inheritable_scope(source_row) == scope
        ):
            # 后写覆盖先写，与 historical_unit_verdicts 的「最近一次判定」一致。
            source_unit_id = coverage_unit_id(source_row)
            source_note = getattr(source_row, "note", "") or ""
    if not source_unit_id:
        return "", "", f"源清单没有该 scope 的 covered 判定: scope={scope or '-'}"
    return source_unit_id, source_note, ""


def _passed_source_evidence(
    source_state: Path, source_unit_id: str, *, workspace_root: Path,
) -> tuple[bool, str, bool]:
    """Check one hop's local evidence for a passed record of ``source_unit_id``.

    返回 ``(passed, reason, found)``：``found=False`` 表示该跳没有可判定的本地
    记录（文件缺失/无法解析/没有该单元记录）；``found=True`` 且非 passed 表示
    本地存在**矛盾证据**（该跳自报 covered 却有 failed/error 记录）。
    """
    source_evidence = source_state / "coverage-evidence.json"
    if source_evidence.is_symlink() or not source_evidence.is_file():
        return False, f"源覆盖证据缺失: {source_evidence}", False
    try:
        payload = json.loads(source_evidence.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return False, f"源覆盖证据无法解析: {source_evidence}: {exc}", False
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return False, f"源覆盖证据缺少 records 数组: {source_evidence}", False
    from core.coverage import coverage_evidence

    for item in records:
        if not isinstance(item, dict) or item.get("unit_id") != source_unit_id:
            continue
        try:
            record = coverage_evidence.EvidenceRecord.from_dict(item)
        except ValueError as exc:
            return False, f"源证据记录损坏 unit={source_unit_id}: {exc}", False
        # OQ-5：沿链跳与本地 P1 同口径补 sha 完整性 + target_drifted 标志
        # 校验；**不**启用当前指纹一致性——祖先记录必然来自不同会话的
        # 指纹，按当前指纹判 stale 会杀死跨 Session 继承机制本身。
        # 校验失败的记录不构成该跳的可判定依据（既非 passed 权威、也不
        # 构成矛盾证据——被篡改的 failed 记录不得作为推翻依据），继续
        # 找该单元的下一条记录。
        ok, check_reason = coverage_evidence.record_authority_check(
            record, workspace_root, require_current_fingerprint=False,
        )
        if not ok:
            continue
        if record.status != "passed" or record.exit_code != 0:
            return False, (
                f"源证据记录非 passed unit={source_unit_id} "
                f"status={record.status} exit_code={record.exit_code}"
            ), True
        return True, "", True
    return False, (
        f"源覆盖证据没有该单元可判定记录: unit={source_unit_id} in {source_evidence}"
    ), False


def resolve_inherited_evidence(
    root: Path, row: object, polling_id: str, lifecycle_id: str,
) -> tuple[bool, str]:
    """Transitive provenance validation of a bootstrap-inherited reference.

    门禁对带 ``inherited_from`` 备注的已覆盖单元，只做**源 lifecycle 证据在案**
    校验：引用链最终必须在某个祖先 lifecycle 的 ``coverage-evidence.json`` 中
    落到该 scope（单元编号跨 Session 重排，scope 才是稳定身份）的 passed
    记录（形状完整）。

    F3''（宿主矩阵 E2 实证修复）：``historical_unit_verdicts`` 的继承引用总是
    指向**最近一个写入该 covered 判定的 lifecycle**，而该判定自身可能也是继承
    而来（清单行带 ``inherited_from`` 备注、本地证据文件没有该单元记录，
    实证：靶场 polling-000003→000002→000001 链条，002 的证据文件只有它自己
    4 条 worker 记录）。因此当一跳没有本地 passed 记录、但该跳清单行的
    covered 判定本身带 ``inherited_from`` 备注时，沿备注继续向祖先溯源。

    以下情形一律 fail-closed，不随链条放宽：
    * 引用链任一跳清单缺失/无法解析/scope 无 covered 判定（伪造引用在此被拦）；
    * 任一跳存在**矛盾的本地非 passed 记录**（自报 covered 却有 failed/error）；
    * 链条成环或深度超过 ``MAX_INHERITED_CHAIN_DEPTH``；
    * 链条终点（判定自称本地产出）却没有本地 passed 记录。

    绝不复制证据文件、绝不改写任何字段——源记录的 ``target_fingerprint`` 漂移
    与 ``observed_at`` 年龄信号原样保留，引擎也不为继承背书写任何新记录
    （语义审计 BLOCK 条款）。多跳溯源成功时返回 ``chain=a/b -> c/d`` 供结算
    日志留痕；单跳成功返回空串（与既有日志形态一致）。
    """
    import core.bugx_paths as bugx_paths

    scope = inheritable_scope(row)
    if not scope:
        return False, "继承单元缺少稳定 scope，拒绝溯源"
    base = bugx_paths.bugx_root(root) / "pollings"
    polling_id, lifecycle_id = str(polling_id), str(lifecycle_id)
    visited: set[tuple[str, str]] = set()
    chain: list[str] = []
    while True:
        chain.append(f"{polling_id}/{lifecycle_id}")
        key = (polling_id, lifecycle_id)
        if key in visited:
            return False, "继承链成环: " + " -> ".join(chain)
        visited.add(key)
        if len(visited) > MAX_INHERITED_CHAIN_DEPTH:
            return False, (
                f"继承链深度超过上限 {MAX_INHERITED_CHAIN_DEPTH}: "
                + " -> ".join(chain)
            )
        source_state = base / polling_id / "lifecycles" / lifecycle_id / "state"
        source_unit_id, source_note, reason = _covered_source_verdict(
            source_state, scope,
        )
        if not source_unit_id:
            return False, reason
        passed, reason, found = _passed_source_evidence(
            source_state, source_unit_id, workspace_root=root,
        )
        if passed:
            if len(chain) == 1:
                return True, ""
            return True, "chain=" + " -> ".join(chain)
        if found:
            # 本地存在可解析的非 passed 记录：矛盾证据，禁止沿链绕过。
            return False, reason
        reference = inherited_evidence_reference(source_note)
        if reference is None:
            # 该跳的 covered 判定自称本地产出，却没有本地 passed 记录：
            # 完整性异常，保持 fail-closed（伪造清单在此被拦截）。
            return False, reason
        polling_id, lifecycle_id = reference


def accept_inherited_evidence(
    rows: list[ModuleRow], root: Path, missing: list[str],
) -> set[str]:
    """对缺失证据的单元尝试 bootstrap 继承引用，返回通过核实的 unit_id 集合。

    接受时在结算输出记录 ``covered via inherited evidence from`` 引用；拒绝时
    向 stderr 打印含源路径与原因的显式告警（R7 复盘第三节：继承失败不得静默）。
    """
    if not missing:
        return set()
    rows_by_id = {coverage_unit_id(row): row for row in rows}
    accepted: set[str] = set()
    for unit_id in missing:
        row = rows_by_id.get(unit_id)
        if row is None or getattr(row, "status", "") != "已覆盖":
            continue
        reference = inherited_evidence_reference(getattr(row, "note", "") or "")
        if reference is None:
            continue
        polling_id, lifecycle_id = reference
        ok, reason = resolve_inherited_evidence(root, row, polling_id, lifecycle_id)
        if ok:
            accepted.add(unit_id)
            # 多跳溯源时 reason 形如 chain=a/b -> c/d，必须随结算日志留痕，
            # 保证继承通过的证据始终可追溯到原始证据 lifecycle。
            detail = f" ({reason})" if reason else ""
            print(
                f"[coverage] {unit_id} covered via inherited evidence "
                f"from {polling_id}/{lifecycle_id}{detail}"
            )
        else:
            print(
                f"[coverage] ⚠ 继承证据引用校验失败 unit={unit_id} "
                f"source={polling_id}/{lifecycle_id}: {reason}",
                file=sys.stderr,
            )
    return accepted


def _observed_record_epoch(observed_at: str) -> float | None:
    """把证据记录的 ``observed_at``（ISO-8601）解析为 epoch 秒；失败返回 None。

    只读解析，不做任何宽恕：调用方对 None 按各自语义 fail-closed 处理。
    """
    text = str(observed_at or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _local_evidence_records(
    evidence_path: Path,
) -> tuple[list[dict[str, object]], str]:
    """只读装载本 lifecycle 的本地证据记录（R-1 P1 本地预查的数据基础）。

    返回 ``(records, error)``：``error`` 非空表示证据文件**存在但无法解析**
    或形状非法（census 对此整体 fail-closed；verdict P1 同
    ``_passed_source_evidence`` 口径视为「无可判定本地记录」落到 P2 沿链）。
    文件缺失返回 ``([], "")``——纯继承 Life 的合法形态（L3-only），P1 无本地
    记录、判定自然落到 P2。本函数绝不写任何文件。
    """
    if evidence_path.is_symlink() or not evidence_path.is_file():
        return [], ""
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return [], f"证据文件无法解析: {evidence_path}: {exc}"
    raw_records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(raw_records, list):
        return [], f"证据文件缺少 records 数组: {evidence_path}"
    return [item for item in raw_records if isinstance(item, dict)], ""


def _latest_local_record(
    records_by_unit: dict[str, list[dict[str, object]]], unit_id: str,
    *, workspace_root: Path,
) -> tuple[dict[str, object] | None, float | None, list[str]]:
    """取该单元按 ``observed_at`` **最新**的本地记录（R-1 P1「取最新」口径）。

    返回 ``(record, observed_epoch, exclusions)``：无可用记录返回
    ``(None, None, exclusions)``；时间戳不可解析时 ``observed_epoch`` 为
    None，调用方按 fail-closed 口径处理（无法证明早于本轮 → 视为本轮新增）。
    排序规则：不可解析时间戳视为最新（fail-closed 保守），同 epoch 按文件序
    （append = checkpoint 时序）取后者。结论只依赖证据文件当前状态，可确定
    性重算。

    记录候选门槛（OQ-5 用户裁决 2026-11-18）：``EvidenceRecord.from_dict``
    形状校验之外，须通过 ``record_authority_check``（sha 完整性 +
    ``target_drifted`` 标志 + 当前指纹一致性，store/validate 同口径）。
    校验失败的记录不构成可判定候选——**排除原因逐条记入 ``exclusions``
    留痕**（不静默丢弃）；完好记录仍正常参与排序，仅当该单元不存在任何
    完好候选时才落 ``None`` 交由调用方走下一优先级。
    """
    from core.coverage import coverage_evidence

    candidates = records_by_unit.get(unit_id) or []
    best: tuple[tuple[float, float, int], dict[str, object], float | None] | None = None
    exclusions: list[str] = []
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            continue
        try:
            record = coverage_evidence.EvidenceRecord.from_dict(item)
        except ValueError as exc:
            # 损坏记录（同口径 _passed_source_evidence found=False）：不构成
            # 可判定候选，也不阻断对其余记录的判定。
            exclusions.append(f"records[{index}] 形状损坏: {exc}")
            continue
        ok, reason = coverage_evidence.record_authority_check(
            record, workspace_root, require_current_fingerprint=True,
        )
        if not ok:
            exclusions.append(f"records[{index}] 权威校验失败: {reason}")
            continue
        epoch = _observed_record_epoch(str(item.get("observed_at") or ""))
        # 不可解析 → 排序键第一分量取 2（恒新于任何可解析记录，fail-closed）。
        order_key = (2, 0.0, index) if epoch is None else (1, epoch, index)
        if best is None or order_key > best[0]:
            best = (order_key, item, epoch)
    if best is None:
        return None, None, exclusions
    return best[1], best[2], exclusions


def _resolve_unit_backing(
    root: Path,
    row: ModuleRow,
    *,
    local_record: dict[str, object] | None,
    observed_epoch: float | None,
    round_started_epoch: float,
    record_exclusions: list[str] | None = None,
) -> dict[str, object]:
    """按合账语义 R-1 优先级解析单个 covered 判定行的归属。

    P1 本地最新记录（L1/L2，``observed_at`` 取最新）> P2 沿链继承（L3，
    ``resolve_inherited_evidence`` 现行 fail-closed 规则逐字不变）> P3
    unbacked。返回 ``{covered, backing, reference, chain, reason, detail,
    local_record}``：

    * P1.a passed/exit 0 → ``covered=True``，``backing=local_round``（本轮）
      或 ``local_historical``（早于本轮开始；时间戳不可解析按 fail-closed
      归入本轮）；清单行带 ``inherited_from`` 备注时尽力沿链解析 ``chain``
      作双记录审计留痕（S6：祖先矛盾**不阻断**本地判定，只记入 reason）。
    * P1.b 非 passed（failed/error）→ ``covered=False``，
      ``backing=local_overturns``（新证据为准，继承判定被推翻，R-4/R-5）。
    * P2.c 溯源可达祖先 passed → ``covered=True``，``backing=inherited``。
    * P2.d/P3 → ``covered=False``，``backing=unbacked``（伪造/无凭据，
      fail-closed）。

    ``inherited`` 判定（维度 1 豁免决策字段）由调用方按
    ``backing == "inherited"`` 推导：字面 DIM1 只有沿链继承豁免（R-6）。
    """
    note = getattr(row, "note", "") or ""
    reference = inherited_evidence_reference(note)
    reference_str = f"{reference[0]}/{reference[1]}" if reference else None
    # OQ-5：被权威校验排除的记录逐条留痕（sha 不符/漂移标志/指纹漂移），
    # 不静默丢弃——判定结论无论落到哪一优先级，审计者都能看到排除依据。
    exclusion_note = (
        "；记录排除: " + "; ".join(record_exclusions)
        if record_exclusions else ""
    )
    result: dict[str, object] = {
        "covered": False,
        "backing": "unbacked",
        "reference": reference_str,
        "chain": "",
        "reason": "",
        "detail": "",
        "local_record": None,
    }
    if local_record is not None:
        status = str(local_record.get("status") or "")
        result["local_record"] = {
            "observed_at": str(local_record.get("observed_at") or ""),
            "worker_id": str(local_record.get("worker_id") or ""),
            "status": status,
        }
        if status == "passed" and local_record.get("exit_code") == 0:
            result["covered"] = True
            if (
                observed_epoch is not None
                and observed_epoch < float(round_started_epoch)
            ):
                result["backing"] = "local_historical"
                detail = "本地历史 passed 证据（早于本轮开始）"
            else:
                # observed_at 不可解析 → 无法证明早于本轮，fail-closed 归入本轮。
                result["backing"] = "local_round"
                detail = "本轮本地 passed 记录（新证据为准）"
            if reference is not None and inheritable_scope(row):
                chain_ok, chain_reason = resolve_inherited_evidence(
                    root, row, reference[0], reference[1],
                )
                if chain_ok:
                    # 双记录并存（R-2）：chain=<沿链串> 与 local record 同时在案。
                    result["chain"] = chain_reason or reference_str or ""
                    detail += f"；双记录并存，沿链 {result['chain']}"
                else:
                    # S6/P4：祖先矛盾不阻断本 lifecycle 判定（P1 短路），
                    # 溯源失败只作审计留痕。
                    result["reason"] = (
                        "祖先链溯源失败（本地实测权威，不阻断判定）: "
                        f"{chain_reason}"
                    )
            result["detail"] = detail + exclusion_note
            return result
        result["backing"] = "local_overturns"
        result["reason"] = (
            f"本地最新记录非 passed（status={status}），"
            "清单/继承判定被新证据推翻（R-1 P1.b）"
        )
        result["detail"] = result["reason"] + exclusion_note
        return result
    # P2：无本地记录 → 清单行 inherited_from 备注 → 沿链溯源（现行规则）。
    if reference is not None:
        if not inheritable_scope(row):
            result["reason"] = "无稳定 scope，无法沿链溯源继承引用"
            result["detail"] = result["reason"] + exclusion_note
            return result
        ok, reason = resolve_inherited_evidence(root, row, reference[0], reference[1])
        if ok:
            result["covered"] = True
            result["backing"] = "inherited"
            result["chain"] = reason or reference_str or ""
            result["detail"] = result["chain"] + exclusion_note
            return result
        result["reason"] = f"继承链溯源失败: {reason}"
        result["detail"] = result["reason"] + exclusion_note
        return result
    # P3：无本地记录且无备注。
    result["reason"] = (
        "covered 行无 inherited_from 备注（继承豁免只认沿链继承覆盖）"
        if inheritable_scope(row)
        else "无稳定 scope 且无本地记录或 inherited_from 备注"
    )
    result["detail"] = result["reason"] + exclusion_note
    return result


def inherited_unit_coverage_verdicts(
    root: Path, manifest_path: Path, scopes: Iterable[str], *,
    evidence_path: Path, round_started_epoch: float,
) -> dict[str, dict[str, object]]:
    """只读判定：每个 scope 最近一次 covered 判定的合账归属（R-1 优先级）。

    供恢复会话结算语义的维度 1（继承豁免）使用。判定按 R-1 读取优先级：

    P1 先查本 lifecycle ``coverage-evidence.json`` 中该单元（scope → 当前
    清单行 unit_id）的最新本地记录（L1/L2，同 ``_passed_source_evidence``
    口径校验记录形状）：passed/exit 0 → ``covered=True`` 且 ``backing``
    为 ``local_round``/``local_historical``；非 passed → ``covered=False``
    且 ``backing=local_overturns``（新证据推翻继承判定，R-4）。P1 无本地
    记录才走 P2：清单行带 ``inherited_from`` 备注 →
    :func:`resolve_inherited_evidence` 沿链溯源（现行 fail-closed 规则逐字
    不变），可达祖先 passed → ``backing=inherited``；失败/无备注 →
    ``backing=unbacked``。清单行遗留的 ``inherited_from`` 备注不再遮蔽更新
    的本地实测（双记录场景 R-2：本地为判定权威，chain 作历史层留痕并存）。

    **维度 1 决策字段不变（R-6 字面裁决）**：``inherited=True`` 当且仅当
    ``backing == "inherited"``（沿链继承豁免）；本地证据覆盖
    （``local_round``/``local_historical``）不触发豁免（OQ-1 有意的
    fail-closed 浪费，放宽与否待用户裁决）。supersede 快照经 ``backing``
    可区分「凭继承豁免」与「凭本地证据豁免」。

    fail-closed 红线：scope 无 covered 判定、清单行**无** ``inherited_from``
    备注（伪造继承覆盖）、或链溯源失败（伪造引用在此被拦），一律
    ``inherited=False`` 并附拒绝原因。本地证据文件缺失/无法解析时 P1 视为
    无本地记录（同 ``_passed_source_evidence`` 口径），判定落到 P2——与
    legacy（纯链判定）行为一致，不因基础设施故障放宽。本函数绝不写任何
    文件、绝不改写清单。
    """
    wanted = [str(scope).strip() for scope in scopes if str(scope).strip()]
    if not wanted:
        return {}

    def _denied(reason: str, reference: str | None = None) -> dict[str, object]:
        return {
            "inherited": False, "covered": False, "backing": "unbacked",
            "reference": reference, "chain": "", "reason": reason,
            "detail": reason, "local_record": None,
        }

    if manifest_path.is_symlink() or not manifest_path.is_file():
        return {s: _denied(f"清单缺失: {manifest_path}") for s in wanted}
    try:
        rows, parse_errors = parse_manifest(manifest_path)
    except (OSError, UnicodeError, ValueError) as exc:
        return {s: _denied(f"清单无法解析: {exc}") for s in wanted}
    if parse_errors:
        return {
            s: _denied(f"清单无效: {'; '.join(parse_errors[:3])}")
            for s in wanted
        }
    # R-1 P1 数据基础：本 lifecycle 本地证据记录（缺失/损坏 → 无本地记录）。
    records, _evidence_error = _local_evidence_records(evidence_path)
    records_by_unit: dict[str, list[dict[str, object]]] = {}
    for record in records:
        unit_key = str(record.get("unit_id") or "")
        if unit_key:
            records_by_unit.setdefault(unit_key, []).append(record)
    # 与 _covered_source_verdict 同口径：后写覆盖先写，取最近一次 covered 判定。
    newest_covered: dict[str, ModuleRow] = {}
    for row in rows:
        if getattr(row, "status", "") == "已覆盖":
            scope = inheritable_scope(row)
            if scope:
                newest_covered[scope] = row
    verdicts: dict[str, dict[str, object]] = {}
    for scope in wanted:
        row = newest_covered.get(scope)
        if row is None:
            verdicts[scope] = _denied(
                f"清单没有该 scope 的 covered 判定: scope={scope}"
            )
            continue
        local_record, observed_epoch, exclusions = _latest_local_record(
            records_by_unit, coverage_unit_id(row), workspace_root=root,
        )
        resolution = _resolve_unit_backing(
            root, row,
            local_record=local_record, observed_epoch=observed_epoch,
            round_started_epoch=round_started_epoch,
            record_exclusions=exclusions,
        )
        verdicts[scope] = {
            "inherited": resolution["backing"] == "inherited",
            "covered": bool(resolution["covered"]),
            "backing": str(resolution["backing"]),
            "reference": resolution["reference"],
            "chain": str(resolution["chain"]),
            "reason": str(resolution["reason"]),
            "detail": str(resolution.get("detail", "")),
            "local_record": resolution["local_record"],
        }
    return verdicts


def recovery_closeout_coverage_census(
    root: Path,
    manifest_path: Path,
    evidence_path: Path,
    round_started_epoch: float,
) -> dict[str, object]:
    """只读普查：恢复结算维度 2 条件①/②的覆盖来源账本（R-1 归属口径）。

    回答两个问题（语义见 budget_governor 恢复会话结算注释块）：

    1. 本 Life 的 covered 判定是否 100% 来自「沿链继承」或「本 Life 历史
       证据」（observed_at 早于本轮开始时间的本地 passed 记录）——
       即零新增挖掘意图（条件①）；
    2. 本 round 是否**零新增证据记录**——OQ-2 加强口径：统计全部状态的
       记录（含 failed/error，不只 passed），任何新证据都证明本轮存在真实
       挖掘/重派动作，并发证明债豁免必须整体退场（条件②）。

    每条 covered 判定的归属按合账语义 R-1 优先级判定（**本地直查先于沿链**，
    反转 legacy「有备注先沿链」在双记录场景的误标）：本 lifecycle 最新本地
    记录（P1：passed → ``local_round``/``local_historical``；非 passed →
    ``local_overturns``）> 沿链继承（P2：备注 + ``resolve_inherited_evidence``
    溯源通过 → ``inherited``；溯源失败 → ``unbacked``）> unbacked。entries
    携带本地记录身份（observed_at/worker_id/status）与 ``chain_reference``；
    清单行自称 covered 而判定为推翻/溯源失败的单元记入 ``divergences``
    （R-5 显性化，不做清单行自动翻转）。清单/证据文件损坏、解析失败一律
    fail-closed（available=False 且两条件为 False）；证据文件缺失是纯继承
    Life 的合法形态。
    """
    fail: dict[str, object] = {
        "available": False,
        "covered_all_inherited_or_historical": False,
        "no_new_evidence_records_this_round": False,
    }
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return {**fail, "reason": f"清单缺失: {manifest_path}"}
    try:
        rows, parse_errors = parse_manifest(manifest_path)
    except (OSError, UnicodeError, ValueError) as exc:
        return {**fail, "reason": f"清单无法解析: {exc}"}
    if parse_errors:
        return {
            **fail,
            "reason": f"清单无效: {'; '.join(parse_errors[:3])}",
        }

    evidence_absent = evidence_path.is_symlink() or not evidence_path.is_file()
    records, evidence_error = _local_evidence_records(evidence_path)
    evidence_note = ""
    if evidence_error:
        # 证据文件存在但损坏/形状非法：基础设施故障，整体 fail-closed。
        return {**fail, "reason": evidence_error}
    if evidence_absent:
        evidence_note = "证据文件缺失（纯继承 Life 的合法形态）"

    # OQ-2 加强口径：本轮新增证据记录 = 全部状态（含 failed/error）。
    # None = 时间戳不可解析：无法证明不是本轮新增，fail-closed 计入。
    new_evidence_this_round = 0
    for record in records:
        epoch = _observed_record_epoch(str(record.get("observed_at") or ""))
        if epoch is None or epoch >= float(round_started_epoch):
            new_evidence_this_round += 1
    records_by_unit: dict[str, list[dict[str, object]]] = {}
    for record in records:
        unit_key = str(record.get("unit_id") or "")
        if unit_key:
            records_by_unit.setdefault(unit_key, []).append(record)

    entries: list[dict[str, object]] = []
    unbacked = 0
    inherited_total = 0
    historical_total = 0
    local_round_total = 0
    overturns_total = 0
    # 与 _covered_source_verdict 同口径：后写覆盖先写。身份用 scope；scope
    # 不稳定（业务行 path 为 ``-``）的行退回 unit_id 身份，不得静默跳过。
    newest_covered: dict[str, ModuleRow] = {}
    order: list[str] = []
    for row in rows:
        if getattr(row, "status", "") != "已覆盖":
            continue
        scope = inheritable_scope(row)
        identity = scope or f"unit:{coverage_unit_id(row)}"
        if identity not in newest_covered:
            order.append(identity)
        newest_covered[identity] = row
    for identity in order:
        row = newest_covered[identity]
        unit_id = coverage_unit_id(row)
        local_record, observed_epoch, exclusions = _latest_local_record(
            records_by_unit, unit_id, workspace_root=root,
        )
        resolution = _resolve_unit_backing(
            root, row,
            local_record=local_record, observed_epoch=observed_epoch,
            round_started_epoch=round_started_epoch,
            record_exclusions=exclusions,
        )
        backing = str(resolution["backing"])
        if backing == "inherited":
            inherited_total += 1
        elif backing == "local_historical":
            historical_total += 1
        elif backing == "local_round":
            local_round_total += 1
        elif backing == "local_overturns":
            overturns_total += 1
        else:
            unbacked += 1
        entries.append({
            "identity": identity, "unit_id": unit_id,
            "backing": backing,
            "detail": str(resolution.get("detail", "")),
            "covered": bool(resolution["covered"]),
            "manifest_row_status": getattr(row, "status", ""),
            "evidence_verdict": (
                "covered" if resolution["covered"] else "not_covered"
            ),
            "chain_reference": resolution["reference"],
            "chain": str(resolution["chain"]),
            "reason": str(resolution["reason"]),
            "local_record": resolution["local_record"],
        })
    # R-5 divergence 显性化：清单行自称已覆盖（含 inherited_from 引用）而
    # 判定为「新证据推翻」或「沿链溯源失败」。不做清单行自动翻转（OQ-3），
    # 后果走诚实路径：该单元不在有效 covered 集 → coverage_complete 不成立。
    divergences = [
        entry for entry in entries
        if entry["backing"] == "local_overturns"
        or (entry["backing"] == "unbacked" and entry["chain_reference"])
    ]
    return {
        "available": True,
        "covered_total": len(entries),
        # entries 全量在案（R-3.4 审计四跳：快照 → 本地记录 → 祖先记录 → 清单
        # 备注引用，全部机读可达；backing/local_record/chain_reference 逐行在案）。
        "entries": entries,
        "inherited_total": inherited_total,
        "historical_local_total": historical_total,
        "local_round_total": local_round_total,
        "local_overturns_total": overturns_total,
        "unbacked_total": unbacked,
        "unbacked_entries": [e for e in entries if e["backing"] == "unbacked"],
        "divergences": divergences,
        "new_evidence_records_this_round": new_evidence_this_round,
        "evidence_note": evidence_note,
        # 条件①：全部归属落在 {inherited, historical_local} 才算「零新增
        # 挖掘意图」；local_round（新证据）/local_overturns（推翻）/unbacked
        # （伪造）任一在场即 False。
        "covered_all_inherited_or_historical": bool(entries) and all(
            entry["backing"] in {"inherited", "local_historical"}
            for entry in entries
        ),
        # 条件②（OQ-2）：零新增证据记录（含 failed/error）。
        "no_new_evidence_records_this_round": new_evidence_this_round == 0,
    }


def evidence_manifest_errors(
    rows: list[ModuleRow], root: Path, manifest: Path | None, *, final: bool,
    require: bool = False,
) -> list[str]:
    """Validate worker evidence when an evidence manifest is supplied."""
    if manifest is None:
        if require:
            return ["覆盖证据 manifest 为必需项，不能只凭 Markdown 自报"]
        return []
    expected = {
        coverage_unit_id(row)
        for row in rows
        if row.status == "已覆盖" or final
    }
    if not manifest.is_file():
        # F3'：全新 Session 的首个 lifecycle 可能尚无本地证据文件。若全部期望
        # 单元都能经 bootstrap 继承引用在源 lifecycle 核实，则不因文件缺席直接
        # 拒绝；任何单元核实失败，维持原有拒绝（保留缺文件错误本身）。
        if expected and accept_inherited_evidence(rows, root, sorted(expected)) >= expected:
            return []
        # F-RSI-20261118-008b：``expected`` 为空（无已覆盖单元的 allow-incomplete
        # 收口轮）即不存在任何证据义务——manifest 缺席是诚实状态，不再阻断
        # （移动靶场 0/14 覆盖轮 post 实证：与 allow-incomplete 的
        # 「接受诚实的未覆盖债务」语义矛盾）。
        if not expected:
            return []
        return [f"覆盖证据 manifest 不存在: {manifest}"]
    try:
        from core.coverage import coverage_evidence

        report = coverage_evidence.validate_manifest(
            manifest, workspace_root=root, expected_units=expected,
        )
    except (OSError, ValueError) as exc:
        return [f"覆盖证据 manifest 无法验证: {exc}"]
    errors: list[str] = []
    if report.missing:
        accepted = accept_inherited_evidence(rows, root, list(report.missing))
        unresolved = [unit for unit in report.missing if unit not in accepted]
        if unresolved:
            errors.append("缺少覆盖证据: " + ", ".join(unresolved))
    for key, items in (
        ("invalid", report.invalid), ("stale", report.stale),
        ("duplicate", report.duplicate), ("conflict", report.conflicts),
    ):
        if items:
            errors.append(f"覆盖证据 {key}: {json.dumps(list(items), ensure_ascii=False)}")
    return errors


def participant_ids(value: str) -> set[str]:
    return set(re.findall(r"#?([A-Za-z0-9_.-]+)", value))


def asset_units(root: Path, strict: bool = True,
                 asset_paths: list[Path] | None = None) -> list[dict]:
    units: list[dict] = []
    if asset_paths is None:
        asset_paths = symbol_index.discover_asset_paths(root, strict=strict)
    for path in asset_paths:
        relative = path.relative_to(root.resolve()).as_posix()
        units.append({
            "scope": relative,
            "path": relative,
            "name": path.name,
            "kind": "asset",
            "line": 1,
            "parser": "asset-inventory",
            "error": None,
            "language": "asset",
            "content_hash": symbol_index.file_sha256(path),
        })
    return units


def discovery_units(root: Path, symbols: bool = True, strict: bool = True,
                    asset_paths: list[Path] | None = None) -> list[dict]:
    """Build the authoritative source + behavior-asset discovery list."""
    root = root.resolve()
    if symbols:
        units = symbol_index.audit_units(root, strict=strict)
    else:
        units = [
            {
                "scope": path,
                "path": path,
                "name": Path(path).stem,
                "kind": "file",
                "line": 1,
                "parser": "files-only",
                "error": None,
                "content_hash": symbol_index.file_sha256(root / path),
            }
            for path in sorted(discover_minimum_unit_paths(root, strict=strict))
        ]
    return units + asset_units(root, strict=strict, asset_paths=asset_paths)


def row_text(row: ModuleRow) -> str:
    path = row.path if row.path.startswith("`") else f"`{row.path}`"
    values = [
        row.number, row.phase, row.parent, row.name, path,
        row.participants, row.minimality, row.difficulty, row.hit, row.tool,
        row.owner, row.deps, row.status, row.findings, row.evidence, row.note,
    ]
    return "| " + " | ".join(values) + " |"


def discovery_row(number: str, unit: dict, *, reset: bool = True,
                   previous: ModuleRow | None = None,
                   root: Path | None = None,
                   risk_cache: dict[tuple[str, str, str], float | None] | None = None) -> ModuleRow:
    file_path = str(unit["path"])
    scope = str(unit["scope"])
    source = Path(file_path)
    parent = str(source.parent) if str(source.parent) != "." else "ROOT"
    kind = str(unit["kind"])
    minimality = (
        "源码文件（未发现可继续拆分的声明）" if kind == "file"
        else "配置/依赖/构建资产，作为独立行为边界"
        if kind == "asset"
        else f"{kind}，可独立构造输入与验证行为"
    )
    digest = str(unit.get("content_hash") or "")
    note = f"{unit.get('parser', 'unknown')} line {unit.get('line', 1)}"
    if digest:
        note += f" sha256:{digest}"
    unit_platform_name = unit_platform(scope)
    if unit_platform_name:
        note += f" platform:{unit_platform_name}"
    difficulty = "3"
    calc_risk: float | None = None
    if root is not None and unit.get("parser") != "files-only":
        try:
            operation_cache = risk_cache if risk_cache is not None else {}
            calc_risk = cached_unit_risk_score(file_path, root, digest, operation_cache)
            if calc_risk is not None:
                from core.repair import test_strategy

                difficulty = test_strategy.risk_score_to_difficulty(calc_risk)
                note += f" risk:{int(calc_risk)}"
        except Exception:
            pass
    if previous is not None and not reset:
        prev_note = previous.note or note
        if calc_risk is not None:
            if re.search(r"\brisk:\d+", prev_note):
                prev_note = re.sub(r"\brisk:\d+", f"risk:{int(calc_risk)}", prev_note)
            else:
                prev_note += f" risk:{int(calc_risk)}"
        return ModuleRow(
            number=number, phase="内部", parent=parent,
            name=str(unit["name"]), path=scope,
            participants=previous.participants, minimality=previous.minimality,
            difficulty=difficulty if previous.difficulty in {"1", "2", "3", "4", "5"} else previous.difficulty,
            hit=previous.hit,
            tool=previous.tool, owner=previous.owner, deps=previous.deps,
            status=previous.status, findings=previous.findings,
            evidence=previous.evidence, note=prev_note,
        )
    return ModuleRow(
        number=number, phase="内部", parent=parent,
        name=str(unit["name"]), path=scope, participants="-",
        minimality=minimality, difficulty=difficulty, hit="3", tool="待定",
        owner="待定", deps="无", status="未覆盖", findings="0",
        evidence="待补", note=note,
    )


def discovery_rows(root: Path, symbols: bool = True) -> list[str]:
    """生成源码、配置和依赖资产的内部最小单元 Markdown 行。"""
    units = discovery_units(root, symbols=symbols, strict=True)
    unit_errors = [unit for unit in units if unit.get("error")]
    if unit_errors:
        raise symbol_index.SourceDiscoveryError(
            "源码解析不完整，拒绝生成覆盖清单: "
            + "; ".join(
                f"{unit['path']}: {unit['error']}" for unit in unit_errors[:5]
            )
        )
    risk_cache: dict[tuple[str, str, str], float | None] = {}
    return [row_text(discovery_row(str(index), unit, root=root, risk_cache=risk_cache))
            for index, unit in enumerate(units, 1)]


WEB_ENDPOINT_PATTERNS = [
    # FastAPI / Flask / Bottle / Tornado: @app.get('/path'), @router.post("/path"), @bp.route("/path", methods=[...])
    (
        re.compile(
            r"""@(?:app|router|bp|blueprint|api)\.(get|post|put|delete|patch|options|head|route)\s*\(\s*["']([^"']+)["']""",
            re.IGNORECASE,
        ),
        "python_route",
    ),
    # Express / Koa / Router (JS/TS): app.get('/path', ...), router.post('/path', ...)
    (
        re.compile(
            r"""\b(?:app|router)\.(get|post|put|delete|patch|options|head)\s*\(\s*["']([^"']+)["']""",
            re.IGNORECASE,
        ),
        "js_route",
    ),
    # NestJS decorators (@Get('/path'), @Post('/path'))
    (
        re.compile(
            r"""@(Get|Post|Put|Delete|Patch|Options|Head)\s*\(\s*["']([^"']+)["']""",
        ),
        "nestjs_route",
    ),
    # Go Gin / Echo: r.GET("/path", ...), e.POST("/path", ...)
    (
        re.compile(
            r"""\.(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s*\(\s*["']([^"']+)["']""",
        ),
        "go_route",
    ),
    # Go net/http: http.HandleFunc("/path", ...)
    (
        re.compile(
            r"""http\.HandleFunc\s*\(\s*["']([^"']+)["']""",
        ),
        "go_http",
    ),
]

CLI_COMMAND_PATTERNS = [
    # Click: @click.command("name") or @click.group("name") or @cli.command("name")
    (
        re.compile(
            r"""@(?:click|cli|group|cmd)\.(?:command|group)\s*(?:\(\s*(?:name\s*=\s*)?["']([^"']+)["']|\(\s*\))?""",
        ),
        "click_cli",
    ),
    # Argparse add_parser: subparsers.add_parser("cmd", ...)
    (
        re.compile(
            r"""(?:add_parser|add_command)\s*\(\s*["']([^"']+)["']""",
        ),
        "argparse_cli",
    ),
]


def announce_feature_surface(root: Path, surfaces: list[dict[str, Any]]) -> None:
    """DOC-125 C125-2：端点切面一旦被抽取出来，就即刻喂给活图谱。

    这里是全仓**唯一**真正产出 endpoint 事实的地方（业务清单的 EP 行同源），
    所以 ``endpoint_discovered`` / ``endpoints_ready`` 从这根管线发出，图谱才不再是
    无人写的死库。清单生成绝不能因为图谱写不进去而失败，因此：未初始化的工作区
    （``bugx_paths.require`` 抛 LayoutError）、引擎副本缺 ``attack_graph*.py``、
    快照损坏等一律静默降级——图谱的可观测性由 pre/post 的 ``attack-graph-*``
    运行事件负责，那条路径在拥有完整布局的地方跑。
    """
    if not surfaces:
        return
    try:
        from core.autonomous_cve import attack_graph_store

        attack_graph_store.absorb_surface(Path(root), surfaces)
    except Exception:  # noqa: BLE001 - 图谱降级不得冒泡到清单生成
        pass


def extract_feature_surface(root: Path) -> list[dict[str, Any]]:
    """提取工程中的业务特性切面与端点（Web API 路由、CLI 命令端点等）。"""
    root = root.resolve()
    surfaces: list[dict[str, Any]] = []
    seen: set[str] = set()

    try:
        source_paths = sorted(discover_audit_unit_paths(root, strict=False))
    except Exception:
        source_paths = []

    for rel_path in source_paths:
        file_path = root / rel_path
        if not file_path.is_file() or file_path.stat().st_size > 512 * 1024:
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue

        for line_no, line in enumerate(content.splitlines(), 1):
            line_str = line.strip()
            if not line_str or line_str.startswith(("#", "//", "/*", "*")):
                continue

            # 1. 匹配 Web API 路由
            for pattern, fw in WEB_ENDPOINT_PATTERNS:
                m = pattern.search(line_str)
                if m:
                    if fw == "go_http":
                        method = "ALL"
                        route_path = m.group(1)
                    else:
                        method = m.group(1).upper()
                        route_path = m.group(2)
                        if method == "ROUTE":
                            method = "ALL"
                    if not route_path.startswith("/"):
                        route_path = "/" + route_path
                    name = f"endpoint:{method}:{route_path}"
                    if name not in seen:
                        seen.add(name)
                        surfaces.append({
                            "type": "endpoint",
                            "name": name,
                            "method": method,
                            "path": route_path,
                            "source_file": rel_path,
                            "line": line_no,
                            "framework": fw,
                        })

            # 2. 匹配 CLI 命令
            for pattern, fw in CLI_COMMAND_PATTERNS:
                m = pattern.search(line_str)
                if m:
                    cmd_name = m.group(1) if m.groups() and m.group(1) else ""
                    if not cmd_name:
                        continue
                    name = f"cli:{cmd_name}"
                    if name not in seen:
                        seen.add(name)
                        surfaces.append({
                            "type": "cli",
                            "name": name,
                            "method": "CLI",
                            "path": cmd_name,
                            "source_file": rel_path,
                            "line": line_no,
                            "framework": fw,
                        })

    announce_feature_surface(root, surfaces)
    return surfaces


def graph_scope(node_id: str) -> str:
    if node_id.startswith("symbol:"):
        return node_id[len("symbol:"):]
    if node_id.startswith("file:"):
        return node_id[len("file:"):]
    return node_id


def business_discovery(root: Path, rows: list[ModuleRow]) -> dict:
    """把自动关系图中的 scope 映射到清单内部单元编号。"""
    graph = project_graph.build_graph(root)
    flows = project_graph.discover_business_flows(graph) if graph.get("complete") else []
    scope_to_id = {
        row.path.strip().strip("`").rstrip("/"): row.number
        for row in rows if row.phase == "内部"
    }

    def closest_scheduled_scope(scope: str) -> str | None:
        """Map parser-only nested callables to their scheduled owner."""
        candidate = scope
        while candidate:
            if candidate in scope_to_id:
                return candidate
            if "::" not in candidate:
                return None
            file_path, qualified = candidate.split("::", 1)
            if "." not in qualified:
                return None
            candidate = f"{file_path}::{qualified.rsplit('.', 1)[0]}"
        return None

    mapped = []
    for flow in flows:
        scopes = [graph_scope(node) for node in flow["participants"]]
        participants = list(dict.fromkeys(
            scope_to_id[scheduled]
            for scope in scopes
            if (scheduled := closest_scheduled_scope(scope)) is not None
        ))
        mapped.append({**flow, "scopes": scopes, "participant_ids": participants})
    return {
        "schema_version": 1,
        "complete": graph.get("complete", False),
        "errors": graph.get("parse_errors", []),
        "flows": mapped,
    }


class PassContext:
    """一次覆盖校验的共享上下文：证据探测缓存 + 全仓枚举结果。

    生命周期严格限定在单次 ``validate_rows`` 调用（以及调用方显式复用的同一轮
    门禁，如 ``startup_gate_errors`` 的「发现单元 + 校验」两步）。跨会话或跨进程
    复用会把旧的文件系统快照当成新结论，对安全门禁是正确性风险，因此这里只做
    轮内去重，不做全局 memo：``probe`` 缓存证据文件事实，其余槽位缓存
    ``discover_audit_unit_paths`` / ``discovery_units`` / ``business_discovery``
    的全仓遍历结果，三者在一轮内各自只计算一次。
    """

    __slots__ = ("root", "probe", "_asset_paths", "_audit_paths",
                 "_units", "_units_ready", "_business", "_business_ready")

    def __init__(self, root: Path):
        self.root = root
        self.probe = EvidenceProbe(root)
        self._asset_paths: list[Path] | None = None
        self._audit_paths: set[str] | None = None
        self._units: list[dict] | None = None
        self._units_ready = False
        self._business: dict | None = None
        self._business_ready = False

    def asset_paths(self) -> list[Path]:
        """全仓资产枚举只走一次，供审计单元路径与符号单元共用。"""
        if self._asset_paths is None:
            self._asset_paths = symbol_index.discover_asset_paths(
                self.root, strict=True,
            )
        return self._asset_paths

    def audit_unit_paths(self) -> set[str]:
        """全仓源码/资产路径枚举只跑一次；失败照旧抛给调用方处理。"""
        if self._audit_paths is None:
            self._audit_paths = discover_audit_unit_paths(
                self.root, strict=True, asset_paths=self.asset_paths(),
            )
        return self._audit_paths

    def units(self) -> list[dict]:
        """符号级最小单元枚举只跑一次，复用已枚举的资产列表。"""
        if not self._units_ready:
            self._units = discovery_units(
                self.root, symbols=True, strict=True,
                asset_paths=self.asset_paths(),
            )
            self._units_ready = True
        return self._units

    def business(self, rows: list[ModuleRow]) -> dict:
        """自动关系图只构建一次；``rows`` 在同一轮内是稳定入参。"""
        if not self._business_ready:
            self._business = business_discovery(self.root, rows)
            self._business_ready = True
        return self._business


def auto_business_cap() -> int:
    """DOC-203 D1-c：自动业务单元生成上限。

    环境变量 ``BUGX_MAX_AUTO_BUSINESS_UNITS`` 可调；<1、非整数或超上界
    回退默认 64 并告警（fail-closed 到保守值，不静默）。硬上界 4096 防
    env 注入把覆盖义务重新撑爆。
    """
    default_cap, max_cap = 64, 4096
    raw = os.environ.get("BUGX_MAX_AUTO_BUSINESS_UNITS")
    if not raw:
        return default_cap
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        print(
            f"[coverage] ⚠ BUGX_MAX_AUTO_BUSINESS_UNITS={raw!r} 非法，"
            f"回退默认 {default_cap}",
            file=sys.stderr,
        )
        return default_cap
    if value < 1:
        print(
            f"[coverage] ⚠ BUGX_MAX_AUTO_BUSINESS_UNITS={value} < 1，"
            f"回退默认 {default_cap}",
            file=sys.stderr,
        )
        return default_cap
    if value > max_cap:
        print(
            f"[coverage] ⚠ BUGX_MAX_AUTO_BUSINESS_UNITS={value} 超过硬上界 "
            f"{max_cap}，按 {max_cap} 执行",
            file=sys.stderr,
        )
        return max_cap
    return value


def business_discovery_rows(root: Path, rows: list[ModuleRow]) -> list[str]:
    """生成自动业务链路/端点面清单行（DOC-203 D1-c 加上限）。

    截断只发生在清单**生成**时刻（bootstrap），确定性排序为
    ``(tier, -confidence, id)``：flow 行按 confidence 降序（非有限/缺失
    视为最低档），EP 端点行无置信度语义，固定排在 flow 之后按 id 序。
    绝不对已绑定 target_fingerprint 的存量清单做事后截断/收缩/重编号。
    """
    # (tier, neg_confidence, id, row) —— tier 0=flow, 1=EP。
    entries: list[tuple[int, float, str, str]] = []
    # 1. 关系图自动发现的跨模块业务流
    for flow in business_discovery(root, rows)["flows"]:
        p_ids = flow.get("participant_ids") or []
        if len(p_ids) < 2:
            continue
        participants = ",".join(p_ids)
        evidence = flow["evidence"][0] if flow["evidence"] else "待补"
        try:
            confidence = float(flow["confidence"])
        except (KeyError, TypeError, ValueError):
            confidence = float("-inf")
        if not math.isfinite(confidence):
            confidence = float("-inf")
        entries.append((
            0,
            -confidence,
            str(flow["id"]),
            f"| {flow['id']} | 业务 | AUTO | 自动业务链路 {flow['id']} | - | "
            f"{participants} | project_graph 自动发现跨模块关系 | 3 | 4 | "
            f"project_graph | 待定 | {participants} | 未覆盖 | 0 | {evidence} | "
            f"flow:{flow['id']} confidence={flow['confidence']:.2f} |",
        ))

    # 2. 功能特性端点与 API 路由切面 (DOC-046)
    surfaces = extract_feature_surface(root)
    if surfaces:
        internal_file_to_id: dict[str, str] = {}
        for r in rows:
            if r.phase == "内部":
                clean_p = r.path.strip().strip("`").split("::")[0].rstrip("/")
                if clean_p not in internal_file_to_id:
                    internal_file_to_id[clean_p] = r.number
        all_internals = [r.number for r in rows if r.phase == "内部"]

        for idx, s in enumerate(surfaces, 1):
            s_id = f"EP{idx:02d}"
            s_name = s["name"]
            s_file = s["source_file"]
            p_ids = []
            if s_file in internal_file_to_id:
                p_ids.append(internal_file_to_id[s_file])
            for uid in all_internals:
                if uid not in p_ids:
                    p_ids.append(uid)
                    if len(p_ids) >= 2:
                        break
            participants = ",".join(p_ids) if len(p_ids) >= 2 else (",".join(all_internals[:2]) if len(all_internals) >= 2 else "UNRESOLVED")
            parent = "API" if s["type"] == "endpoint" else "CLI"
            
            diff = "3"
            try:
                from core.repair import test_strategy
                risk = test_strategy.calculate_unit_risk_score(s_file, root)
                diff = test_strategy.risk_score_to_difficulty(risk)
                r_note = f" risk:{int(risk)}"
            except Exception:
                r_note = ""

            entries.append((
                1,
                0.0,
                s_id,
                f"| {s_id} | 业务 | {parent} | {s_name} | - | "
                f"{participants} | 功能特性端点，驱动内部服务与状态迁移 | {diff} | 4 | "
                f"project_graph | 待定 | {participants} | 未覆盖 | 0 | 待补 | "
                f"feature:{s_name}{r_note} framework={s['framework']} |",
            ))

    cap = auto_business_cap()
    ordered = sorted(entries, key=lambda entry: (entry[0], entry[1], entry[2]))
    if len(ordered) > cap:
        print(
            f"[coverage] ⚠ 自动业务单元生成 {len(ordered)} 条，超过上限 "
            f"{cap}（BUGX_MAX_AUTO_BUSINESS_UNITS），已按确定性排序截断；"
            f"被截单元视同发现范围之外，不伪造覆盖",
            file=sys.stderr,
        )
        ordered = ordered[:cap]
    return [entry[3] for entry in ordered]


def coverage_summary(rows: list[ModuleRow], errors: list[str] | None = None) -> dict:
    """返回供状态页/自动化消费的两阶段进度，不把未完成当成命令失败。"""
    internals = [row for row in rows if row.phase == "内部"]
    business = [row for row in rows if row.phase == "业务"]
    internal_covered = sum(row.status == "已覆盖" for row in internals)
    business_covered = sum(row.status == "已覆盖" for row in business)
    active_rows = [row for row in rows if row.status in {"挖掘中", "未知"}]
    if not active_rows:
        active_rows = [row for row in rows if row.status == "挂起"]
    if internal_covered < len(internals):
        phase = "内部"
    elif business_covered < len(business):
        phase = "业务"
    elif rows:
        phase = "完成"
    else:
        phase = "未规划"
    total = len(internals) + len(business)
    covered = internal_covered + business_covered
    unknown_internal = sum(row.status == "未知" for row in internals)
    unknown_business = sum(row.status == "未知" for row in business)
    return {
        "phase": phase,
        "current_units": [
            {
                "id": row.number,
                "name": row.name,
                "scope": row.path if row.phase == "内部" else row.participants,
                "status": row.status,
            }
            for row in active_rows
        ],
        "internal": {
            "covered": internal_covered,
            "total": len(internals),
            "percent": round(internal_covered / len(internals) * 100, 1)
            if internals else 0.0,
        },
        "business": {
            "covered": business_covered,
            "total": len(business),
            "percent": round(business_covered / len(business) * 100, 1)
            if business else 0.0,
        },
        "unknown": {
            "internal": unknown_internal,
            "business": unknown_business,
            "total": unknown_internal + unknown_business,
        },
        "overall": {
            "covered": covered,
            "total": total,
            "percent": round(covered / total * 100, 1) if total else 0.0,
        },
        "structure_valid": not errors,
        "errors": list(errors or []),
    }
