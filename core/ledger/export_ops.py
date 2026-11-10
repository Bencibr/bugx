#!/usr/bin/env python3
"""core/ledger/export_ops.py - 模式晋升、经验书导出与数据备份 (DOC-036)。"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
import hashlib
import re
from typing import Any, Iterable, Mapping, Sequence

# DES-02：原先这里的 ``_LedgerProxy`` 经 ``sys.modules.get("lesson_ledger")``
# 反向取符号，与顶层 Facade 构成双向依赖环；现改为经同级解析表
# （``core/ledger/_facade.py``）取**定义该符号的兄弟模块**，且延迟导入，
# 因此 ``ledger/*`` 不再依赖 ``lesson_ledger``，方向变为单向。
from core.ledger._facade import LedgerProxy

_PROJECT_URL_PATTERN = re.compile(r"(?i)\b(?:https?|ssh)://[^\s<>\"'`]+")
_WINDOWS_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/][^\\/\s]+[\\/])"
    r"[^\s<>\"'`|;,)}\]]*"
)
_POSIX_PATH_PATTERN = re.compile(
    r"(?<![\w:])/(?:[^\s/\\<>\"'`|;,)}\]]+/)+[^\s/\\<>\"'`|;,)}\]]+"
    r"(?::\d+)?"
)
_RELATIVE_PATH_PATTERN = re.compile(
    r"(?<![\w./-])(?:\.{1,2}[\\/])?(?:[A-Za-z0-9_.@+-]+[\\/])+"
    r"[A-Za-z0-9_.@+-]+(?::\d+)?"
)


def _sanitize_source_refs(refs: Sequence[str] | None) -> list[str]:
    """Drop project-specific file names, paths, and commit hashes from global provenance."""
    del refs
    return ["global-promoted"]


def _new_promotion_redactor():
    try:
        from core.governance.secret_redactor import SecretRedactor

        return SecretRedactor(test_context=False, redact_pii=True)
    except Exception as exc:
        raise LessonLedgerError("全局晋升脱敏器不可用，已拒绝写入") from exc


def _sanitize_project_paths(text: str) -> str:
    sanitized = _PROJECT_URL_PATTERN.sub("<project-url>", text)
    sanitized = _WINDOWS_PATH_PATTERN.sub("<project-path>", sanitized)
    sanitized = _POSIX_PATH_PATTERN.sub("<project-path>", sanitized)
    return _RELATIVE_PATH_PATTERN.sub("<project-path>", sanitized)


def _sanitize_promotion_value(value: Any, redactor: Any) -> Any:
    if isinstance(value, str):
        try:
            sanitized = redactor.redact_text(
                value, language="text", test_context=False, guard_syntax=False,
            )
        except Exception as exc:
            raise LessonLedgerError("全局晋升文本脱敏失败，已拒绝写入") from exc
        return _sanitize_project_paths(sanitized)
    if isinstance(value, dict):
        return {key: _sanitize_promotion_value(item, redactor) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_promotion_value(item, redactor) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_promotion_value(item, redactor) for item in value)
    return value


def _sanitize_promotion_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    with _new_promotion_redactor() as redactor:
        return _sanitize_promotion_value(dict(payload), redactor)


_ledger = LedgerProxy()
class _ErrorMeta(type):
    def __instancecheck__(cls, instance):
        return isinstance(instance, _ledger.LessonLedgerError)
    def __subclasscheck__(cls, subclass):
        return issubclass(subclass, _ledger.LessonLedgerError)


class LessonLedgerError(Exception, metaclass=_ErrorMeta):
    def __new__(cls, *args, **kwargs):
        return _ledger.LessonLedgerError(*args, **kwargs)


def database_path(*args, **kwargs): return _ledger.database_path(*args, **kwargs)
def _conn(*args, **kwargs): return _ledger.connect_ledger(*args, **kwargs)
def _tx(*args, **kwargs): return _ledger.ledger_transaction(*args, **kwargs)
def _text(*args, **kwargs): return _ledger.sanitize_text(*args, **kwargs)
def _now(*args, **kwargs): return _ledger.now_iso(*args, **kwargs)
def _event(*args, **kwargs): return _ledger.record_event(*args, **kwargs)
def _insert(*args, **kwargs): return _ledger.insert_pattern(*args, **kwargs)
def _lessons(*args, **kwargs): return _ledger.lessons_dict(*args, **kwargs)
def _pattern_value(*args, **kwargs): return _ledger.pattern_value(*args, **kwargs)
def _regular(*args, **kwargs): return _ledger.is_regular_file(*args, **kwargs)
def _validate(*args, **kwargs): return _ledger.validate_pattern(*args, **kwargs)
def import_patterns(*args, **kwargs): return _ledger.import_patterns(*args, **kwargs)
def read(*args, **kwargs): return _ledger.read(*args, **kwargs)


def _promoted_incident_id(project_db: Path, incident_id: str) -> str:
    """Return a stable global key scoped to one canonical project ledger."""
    namespace = project_db.resolve().as_posix()
    digest = hashlib.sha256(f"{namespace}\0{incident_id}".encode("utf-8")).hexdigest()
    return "I-" + digest[:12]

def promote_pattern(project_ledger: str | os.PathLike[str],
                    global_ledger: str | os.PathLike[str],
                    pattern_id: str,
                    *, actor: str = "publisher") -> dict[str, Any]:
    """DOC-004：将项目特异错题中多生命周期验证的高价值模式晋升至全局通用错题集。

    规则：
    1) 模式在项目库中必须存在且状态为 active；
    2) hit_count ≥ 1（在至少 1 个独立生命周期中验证通过）；
    3) 清理具体项目专有的敏感 source_refs/提交 Hash，提炼为通用定义；
    4) 以 publisher 身份注入全局库，状态置为 active；
    5) 项目库原模式增加 promoted 标记事件。
    """
    if actor != "publisher":
        raise LessonLedgerError("模式晋升必须由 publisher 执行")
    pid = _text(pattern_id, "pattern_id")

    # 读取项目库中的模式定义
    p_db = database_path(project_ledger)
    with _conn(p_db, readonly=True) as pc:
        _validate(pc, p_db)
        pattern = _pattern_value(pc, pid)

    if pattern["status"] != "active":
        raise LessonLedgerError(f"仅 active 状态的模式可晋升全局，当前状态: {pattern['status']}")
    if pattern["hit_count"] < 1:
        raise LessonLedgerError(f"模式至少需 1 次 hit 验证方可晋升全局，当前 hit: {pattern['hit_count']}")

    # 提炼通用卡片：清除项目特异的具体的代码提交/哈希/指纹引用并深度脱敏
    cleaned = dict(pattern)
    cleaned["status"] = "active"
    # Global provenance must not retain a project-specific file name or path.
    cleaned["source_refs"] = _sanitize_source_refs(pattern.get("source_refs", []))
    cleaned["finding_fingerprints"] = []
    cleaned["superseded_by"] = None
    # Explicit source-code fields are not portable knowledge and are never promoted.
    cleaned["before_code"] = ""
    cleaned["after_code"] = ""
    cleaned = _sanitize_promotion_payload(cleaned)

    # 写入全局库
    g_db = database_path(global_ledger)
    result = import_patterns(
        g_db, [cleaned],
        bundle_id=f"promote-{pid}-{int(time.time())}",
        reviewer=actor,
        actor="publisher",
    )

    # 标记项目库事件
    def record_project_event(c):
        _event(c, "pattern-promoted-to-global", {
            "pattern_id": pid,
            "global_ledger": str(g_db),
            "promoted_at": _now(),
        })
        return {"status": "promoted", "pattern_id": pid, "global_result": result}

    _tx(project_ledger, record_project_event)
    return {"status": "promoted", "pattern_id": pid, "global_result": result}


def promote_incident(project_ledger: str | os.PathLike[str],
                     global_ledger: str | os.PathLike[str],
                     incident_id: str, evidence_receipt_id: str,
                     *, actor: str = "publisher") -> dict[str, Any]:
    """Promote one verified project incident without leaking project refs."""
    if actor != "publisher":
        raise LessonLedgerError("incident 晋升必须由 publisher 执行")
    pid = database_path(project_ledger)
    gid = database_path(global_ledger)
    if pid.resolve() == gid.resolve():
        raise LessonLedgerError("项目账本与全局账本必须是不同文件")
    key = _text(incident_id, "incident_id")
    receipt = _text(evidence_receipt_id, "evidence_receipt_id")
    with _conn(pid, readonly=True) as pc:
        _validate(pc, pid)
        incident = _ledger.incident_value(pc, key)
        if incident["status"] != "active":
            raise LessonLedgerError("仅 active incident 可晋升全局")
        proof = pc.execute(
            "SELECT subject_type,subject_id,lifecycle_id,round,run_id,artifact_hash "
            "FROM evidence_receipts WHERE receipt_id=?", (receipt,),
        ).fetchone()
        if not proof or proof[0] != "incident" or proof[1] != key:
            raise LessonLedgerError("evidence receipt 未绑定当前 incident")
        if incident["lifecycle_id"] is not None and proof[2] != incident["lifecycle_id"]:
            raise LessonLedgerError("evidence receipt lifecycle 与 incident 不匹配")
        if incident["round"] is not None and int(proof[3]) != int(incident["round"]):
            raise LessonLedgerError("evidence receipt round 与 incident 不匹配")
        from .sync_ops import require_verified_incident_evidence
        require_verified_incident_evidence(project_ledger, incident, {
            "subject_type": proof[0], "subject_id": proof[1],
            "lifecycle_id": proof[2], "round": proof[3],
            "run_id": proof[4], "artifact_hash": proof[5],
        })

    pattern_id = incident.get("pattern_id")
    if pattern_id:
        with _conn(gid, readonly=True) as gc:
            _validate(gc, gid)
            parent = gc.execute(
                "SELECT status FROM patterns WHERE pattern_id=?", (pattern_id,),
            ).fetchone()
            if not parent or parent[0] != "active":
                raise LessonLedgerError("incident 关联的全局 pattern 未 active")
    cleaned = {name: incident.get(name) for name in (
        "pattern_id", "signature", "decision", "duration_seconds",
        "routing_tags",
    )}
    # D7-05：每次晋升都先在项目命名空间内派生全局 ID，避免
    # “查重后再决定 ID”的并发窗口；格式仍由 ledger schema 固定为 I-12hex。
    global_key = _promoted_incident_id(pid, key)

    cleaned["incident_id"] = global_key
    cleaned["summary"] = incident.get("summary")
    # Raw source and project context are deliberately excluded from the shared ledger.
    cleaned["context"] = "<project-context-redacted>"
    cleaned["before_snippet"] = ""
    cleaned["after_snippet"] = ""
    cleaned.update({"diff_ref": None, "commit_ref": None,
                    "lifecycle_id": None, "round": None,
                    "status": "candidate", "actor": "supervisor"})
    cleaned = _sanitize_promotion_payload(cleaned)
    result = _ledger.add_incident(gid, cleaned)

    def activate(c):
        current = _ledger.incident_value(c, global_key)
        if current["status"] == "active":
            return "duplicate"
        if current["status"] != "candidate":
            raise LessonLedgerError("全局 incident 不是可激活的 candidate")
        c.execute("UPDATE incidents SET status='active',updated_at=? WHERE incident_id=?",
                  (_now(), global_key))
        _event(c, "incident-promoted-to-global", {
            "incident_id": global_key, "source_incident_id": key, "source_evidence_receipt_id": receipt,
        })
        return "activated"

    status = _tx(gid, activate)
    _tx(pid, lambda c: _event(c, "incident-promoted-to-global", {
        "incident_id": key, "global_incident_id": global_key, "global_ledger": str(gid),
        "evidence_receipt_id": receipt, "promoted_at": _now(),
    }) or {"status": "promoted"})
    return {"status": "promoted", "incident_id": key, "global_incident_id": global_key,
            "global_result": result, "activation": status}


def promote_tool_experience(project_ledger: str | os.PathLike[str],
                            global_ledger: str | os.PathLike[str],
                            tool_name: str, category: str,
                            *, actor: str = "publisher") -> dict[str, Any]:
    """Promote an active project Tool Experience into the global ledger."""
    if actor != "publisher":
        raise LessonLedgerError("工具经验晋升必须由 publisher 执行")
    name = _text(tool_name, "tool_name")
    cat = _text(category, "category")
    source = next((item for item in _ledger.list_tool_experience(project_ledger)
                   if item["tool_name"] == name and item["category"] == cat), None)
    if not source or source.get("experience_level") != "active":
        raise LessonLedgerError("仅 active 工具经验可晋升全局")
    source = {field: source.get(field) for field in _ledger.TOOL_EXPERIENCE_FIELDS}
    existing = next((item for item in _ledger.list_tool_experience(global_ledger)
                     if item["tool_name"] == name and item["category"] == cat), None)
    if existing and existing.get("experience_level") == "active":
        return {"status": "duplicate", "tool_name": name, "category": cat}
    imported = _ledger.import_tool_experience(global_ledger, [source], actor="supervisor")
    activated = _ledger.activate_tool_experience(global_ledger, name, cat, actor=actor)
    _tx(project_ledger, lambda c: _event(c, "tool-experience-promoted-to-global", {
        "tool_name": name, "category": cat, "global_ledger": str(database_path(global_ledger)),
        "promoted_at": _now(),
    }) or {"status": "promoted"})
    return {"status": "promoted", "tool_name": name, "category": cat,
            "import": imported, "activation": activated}


def inherit_patterns(project_ledger: str | os.PathLike[str],
                     global_ledger: str | os.PathLike[str]) -> dict[str, Any]:
    """DOC-004：新项目 init 时从全局通用错题集继承 active 模式。

    只继承全局库中状态为 active 的通用模式，不覆盖项目库中已存在的模式。
    """
    g_db = database_path(global_ledger)
    if not g_db.is_file():
        return {"status": "inherited", "count": 0, "reason": "全局库不存在"}

    with _conn(g_db, readonly=True) as gc:
        _validate(gc, g_db)
        global_lessons = _lessons(gc)

    active_patterns = [
        p for p in global_lessons.values()
        if p.get("status") == "active"
    ]
    if not active_patterns:
        return {"status": "inherited", "count": 0, "reason": "全局库无 active 模式"}

    p_db = database_path(project_ledger)
    def go(c):
        imported = 0
        skipped = 0
        for p in active_patterns:
            pid = p["pattern_id"]
            exists = c.execute("SELECT 1 FROM patterns WHERE pattern_id=?", (pid,)).fetchone()
            if exists:
                skipped += 1
                continue
            item = dict(p)
            item["status"] = "candidate"  # 在项目中初始化为 candidate，由项目生命周期验证后再转 active
            _insert(c, item)
            imported += 1
        if imported > 0:
            _event(c, "knowledge-inherited-from-global", {
                "count": imported,
                "global_ledger": str(g_db),
                "inherited_at": _now(),
            })
        return {"status": "inherited", "imported": imported, "skipped": skipped}

    return _tx(project_ledger, go)


def export_snapshot(path: str | os.PathLike[str], output: str | os.PathLike[str]) -> dict[str, Any]:
    """Write a human-reviewable JSON snapshot without changing the fact source."""
    target = Path(output)
    if target.is_symlink():
        raise LessonLedgerError(f"拒绝覆盖符号链接导出目标: {target}")
    state = read(path)
    payload = {
        "schema_version": _ledger.SCHEMA_VERSION,
        "snapshot_type": "lesson-ledger",
        "exported_at": _now(),
        "state_hash": state["state_hash"],
        "head_hash": state["head_hash"],
        "lessons": state["lessons"],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temp = Path(name)
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, target)
    finally:
        if temp.is_file(): temp.unlink()
    return {"status": "exported", "output": str(target), "state_hash": state["state_hash"], "lessons": len(state["lessons"])}



BOOK_PREAMBLE = """# BUG 错题集（Mistake Book）

> 本文件由 `lesson_ledger.py export-book` 从 SQLite 错题账本自动生成，**请勿手工编辑**。
> 历史事实源是账本本身：新增/复用经验走 cycle-review（`review`），工具归因走
> `tool-feedback`；本文件只是人读快照，修改这里不会影响检索与门禁。
> 每轮勘察前通读本文件（或 `retrieve` 检索），把「同类排查点」当作主动排查清单——
> 与 `history`（根因去重，管"别重复报"）互补：错题集管**往哪挖**。
"""


def book_line(label: str, values: Iterable[str]) -> list[str]:
    items = [x for x in values if x and x.strip()]
    if not items:
        return []
    return [f"- {label}：" + "；".join(items)]


def export_book(path: str | os.PathLike[str], output: str | os.PathLike[str]) -> dict[str, Any]:
    """Render the human-readable mistake book from the authoritative ledger.

    The book used to be hand-maintained next to the JSON seed and drifted from
    it.  Generation is deterministic from ledger state, so the seed/SQLite
    store stays the single source of truth.
    """
    target = Path(output)
    if target.is_symlink():
        raise LessonLedgerError(f"拒绝覆盖符号链接导出目标: {target}")
    state = read(path)
    lessons = sorted(
        state["lessons"].values(),
        key=lambda x: (x["category"], x["pattern_id"]),
    )
    lines: list[str] = [BOOK_PREAMBLE, ""]
    lines.append(f"- 生成时间：{_now()}；state_hash：`{state['state_hash'][:16]}…`；"
                 f"模式总数：{len(lessons)}（revision {state['revision']}）")
    lines.append("")
    lines.append("## 分类速查")
    lines.append("")
    lines.append("| 类别 | 代表模式 | 状态 | 命中/使用 |")
    lines.append("|---|---|---|---|")
    for item in lessons:
        lines.append(
            f"| {item['category']} | {item['title']} | {item['status']} | "
            f"{item['hit_count']}/{item['use_count']} |"
        )
    lines += ["", "## 错题条目", ""]
    for item in lessons:
        lines.append(f"### [{item['category']}] {item['title']}")
        lines.append(f"- 根因：{item['root_cause']}")
        lines.append(f"- 反思：{item['reflection']}")
        lines += book_line("同类排查点", item["probes"])
        lines += book_line("适用场景", item["applicability"])
        lines += book_line("误报特征", item["false_positive_patterns"])
        lines += book_line("Oracle 提示", item["oracle_hints"])
        lines += book_line("负对照", item["negative_controls"])
        lines += book_line("排查路径", item["playbook"])
        lines += book_line("已尝试方案", item["attempts_tried"])
        lines += book_line("成功方案", item["successful_solution"])
        lines += book_line("实例", item["source_refs"])
        lines.append(
            f"- 战绩：status={item['status']}，use={item['use_count']}，"
            f"hit={item['hit_count']}，miss={item['miss_count']}，"
            f"false_positive={item['false_positive_count']}，"
            f"rejected={item['rejected_count']}"
        )
        lines.append("")
    content = "\n".join(lines) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Windows AV/索引器会锁住新建的 .tmp 文件（可达数秒）；固定临时名重试
    # 仍会撞锁，因此每轮换新临时名，有界重试吸收竞态。
    last_error: OSError | None = None
    temp = Path()
    try:
        for attempt in range(8):
            fd, name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
            )
            os.close(fd)
            temp = Path(name)
            try:
                temp.write_text(content, encoding="utf-8")
                os.replace(temp, target)
                temp = Path()
                break
            except PermissionError as exc:
                last_error = exc
                try:
                    temp.unlink()
                except OSError:
                    pass
                time.sleep(0.2 * (attempt + 1))
        else:
            raise last_error if last_error else OSError("export_book 写入失败")
    finally:
        if temp and temp.is_file():
            temp.unlink()
    return {
        "status": "exported", "output": str(target),
        "state_hash": state["state_hash"], "lessons": len(lessons),
    }


def backup_database(path: str | os.PathLike[str], output: str | os.PathLike[str]) -> dict[str, Any]:
    """Create an atomic SQLite backup and validate it before publishing."""
    source = database_path(path)
    target = Path(output)
    if target.is_symlink():
        raise LessonLedgerError(f"拒绝覆盖符号链接备份目标: {target}")
    if target.resolve() == source.resolve():
        raise LessonLedgerError("备份目标不能与运行库相同")
    _regular(source, "SQLite 运行库")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    temp = Path(name)
    try:
        with _conn(source, readonly=True) as src:
            dst = sqlite3.connect(str(temp))
            try:
                src.backup(dst)
                # A source in WAL mode transfers that mode to the backup
                # header.  Remove sidecars before publishing the main file;
                # otherwise an atomic rename can leave an orphan WAL/SHM pair.
                try:
                    dst.execute("PRAGMA journal_mode=DELETE")
                except sqlite3.DatabaseError:
                    pass
                dst.commit()
            finally:
                dst.close()
        for sidecar in (Path(str(temp) + "-wal"), Path(str(temp) + "-shm")):
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass
        # Do not switch the temporary file to WAL: its sidecar must not be
        # orphaned when the main file is atomically renamed to the target.
        with _conn(temp, enable_wal=False) as check:
            _validate(check, temp)
        os.replace(temp, target)
    finally:
        if temp.is_file():
            temp.unlink()
    return {"status": "backed_up", "output": str(target), "state_hash": read(target)["state_hash"]}


def synthesize_skill_pack(project_ledger: str | os.PathLike[str],
                          incident_id: str,
                          skills_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """显式合成绑定账本回执的 active incident Skill 包 (L7 N7.1 / DOC-125)。"""
    pid = database_path(project_ledger)
    key = _text(incident_id, "incident_id")
    with _conn(pid, readonly=True) as pc:
        _validate(pc, pid)
        incident = _ledger.incident_value(pc, key)
        if incident.get("status") != "active":
            raise LessonLedgerError("仅 active incident 可合成 Skill")
        lifecycle_id = incident.get("lifecycle_id")
        round_no = incident.get("round")
        proof = pc.execute(
            "SELECT receipt_id,subject_type,subject_id,lifecycle_id,round,run_id,artifact_hash "
            "FROM evidence_receipts "
            "WHERE subject_type='incident' AND subject_id=? "
            "AND (? IS NULL OR lifecycle_id=?) "
            "AND (? IS NULL OR round=?) "
            "ORDER BY created_at DESC,receipt_id LIMIT 1",
            (key, lifecycle_id, lifecycle_id, round_no, round_no),
        ).fetchone()
        if not proof:
            raise LessonLedgerError("Skill 合成缺少与 incident 绑定的 evidence receipt")
        from .sync_ops import require_verified_incident_evidence
        require_verified_incident_evidence(pid, incident, {
            "subject_type": proof[1], "subject_id": proof[2],
            "lifecycle_id": proof[3], "round": proof[4],
            "run_id": proof[5], "artifact_hash": proof[6],
        })
    from core.skills import skill_synthesizer
    pack_dir = skill_synthesizer.synthesize_skill_from_incident(incident, output_dir=skills_dir)
    return {
        "status": "synthesized",
        "incident_id": key,
        "evidence_receipt_id": proof[0],
        "skill_dir": str(pack_dir),
        "skill_id": pack_dir.name,
    }
