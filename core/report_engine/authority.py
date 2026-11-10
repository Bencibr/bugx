"""Supervisor 权威发现源 (Supervisor Authority Loader).

报告引擎的历史缺陷（BUGX-AUDIT）：``ReportDataBundle.from_eval_dir`` 只读
``verification/evals/oss/*_all_categories_audit.json`` 这类**引擎自测收据**，
从不读项目真实运行态 ``.bugx/.../state/supervisor.json`` 里的 ``findings``。
于是「5 条已 CONFIRMED 漏洞」的项目跑 ``bugx report both`` 得到的是
「0 漏洞 / PASS (COMPLIANT) / 官方会签」——报告与权威状态彻底脱钩。

本模块是报告引擎唯一的「发现事实来源」：按
``Project → Session → Lifecycle`` 解析运行态路径，把 Supervisor 快照里的
``findings`` / ``confirmation_gate`` / ``coverage`` 原样取出，交给
``ReportDataBundle`` 渲染。取不到就返回 ``None``，由调用方 fail-closed，
绝不回退到「编一份好看的空白报告」。

平台约束：所有路径走 ``pathlib`` 与 ``core.bugx_paths``；不拼接分隔符，
不假设 POSIX；状态文件按只读方式打开，不写盘。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

#: Supervisor 快照相对项目根的候选位置（布局 2.x：pollings/<p>/lifecycles/<l>/state/）。
_CURRENT_RELPATH = Path(".bugx") / "current.json"
_MANIFEST_RELPATH = Path(".bugx") / "manifest.json"

#: 严重度 → 处置 SLA（小时）。只用于呈现，不参与门禁。
_SLA_BY_SEVERITY = {"CRITICAL": 24, "HIGH": 72, "MEDIUM": 168, "LOW": 720}

#: 报告承认的「已确认」状态集合。
CONFIRMED_STATUSES = frozenset({"CONFIRMED"})
PENDING_STATUSES = frozenset({"CANDIDATE", "PENDING", "UNVERIFIED", "REJECTED"})


class AuthorityUnavailable(RuntimeError):
    """项目运行态不可用（无 .bugx / 无 supervisor 快照 / JSON 损坏）。"""


@dataclass(frozen=True)
class Authority:
    """一次报告可引用的事实集合，全部来自磁盘上已有的权威状态。"""

    project_root: Path
    supervisor_path: Path
    run_id: str
    status: str
    revision: int
    findings: List[Mapping[str, Any]] = field(default_factory=list)
    confirmation_gate: Mapping[str, Any] = field(default_factory=dict)
    coverage: Mapping[str, str] = field(default_factory=dict)
    lifecycle_id: str = ""
    polling_id: str = ""
    #: 阻断「官方会签」的硬约束（空 = 门禁无阻断）。
    gate_blockers: tuple[str, ...] = ()

    @property
    def confirmed(self) -> List[Mapping[str, Any]]:
        return [f for f in self.findings if _status_of(f) in CONFIRMED_STATUSES]

    @property
    def pending(self) -> List[Mapping[str, Any]]:
        return [f for f in self.findings if _status_of(f) not in CONFIRMED_STATUSES]

    @property
    def verification_executed(self) -> int:
        """真正跑过「双正例 + 固定负对照」的发现条数。

        只有 ``verification`` 块里正例次数 >= 2 且负对照实测通过才计数；只是
        打了 confirm 标记但没有实测记录的发现不算，报告不得据此宣称已裁决。
        """
        count = 0
        for finding in self.findings:
            block = finding.get("verification")
            if not isinstance(block, Mapping):
                continue
            positives = block.get("positive_runs")
            negative = block.get("negative_control")
            if not isinstance(positives, list) or len(positives) < 2:
                continue
            if not isinstance(negative, Mapping):
                continue
            if negative.get("oracle_matched") is not True and negative.get("exit_code") is None:
                continue
            count += 1
        return count


def _status_of(finding: Mapping[str, Any]) -> str:
    """从发现记录推导确认状态。

    Supervisor 用 ``confirmation``/``verification_confirmed`` 块表达确认，
    而不是一个 status 字段；两者缺一即未确认。
    """
    if isinstance(finding.get("confirmation"), Mapping):
        return "CONFIRMED"
    if isinstance(finding.get("verification_confirmed"), Mapping):
        return "CONFIRMED"
    if isinstance(finding.get("verification"), Mapping):
        return "CANDIDATE"
    return "UNVERIFIED"


def _read_json(path: Path) -> Any:
    """读取 JSON；错误信息只带文件名，不把宿主机绝对路径写进报告。"""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise AuthorityUnavailable(f"缺少文件 {path.name}") from exc
    except OSError as exc:
        raise AuthorityUnavailable(f"无法读取 {path.name}: {exc.strerror or exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorityUnavailable(f"JSON 无法解析 {path.name}: {exc}") from exc


def _lifecycle_state_dir(root: Path) -> Optional[Path]:
    """解析当前生命周期的 ``state/`` 目录。

    优先用 ``.bugx/current.json`` 的 ``polling_id``/``lifecycle_id``；缺失时
    退回扫描 ``.bugx/pollings/*/lifecycles/*/state``（只取最新 mtime 的一个），
    这样「current.json 被清掉但证据还在」的项目仍能出报告。
    """
    current = root / _CURRENT_RELPATH
    if current.is_file():
        try:
            payload = _read_json(current)
        except AuthorityUnavailable:
            payload = {}
        polling_id = payload.get("polling_id")
        lifecycle_id = payload.get("lifecycle_id")
        if isinstance(polling_id, str) and isinstance(lifecycle_id, str):
            candidate = (
                root / ".bugx" / "pollings" / polling_id
                / "lifecycles" / lifecycle_id / "state"
            )
            if candidate.is_dir():
                return candidate
    pollings = root / ".bugx" / "pollings"
    if not pollings.is_dir():
        return None
    best: Optional[tuple[float, Path]] = None
    for state_dir in pollings.glob("*/lifecycles/*/state"):
        snapshot = state_dir / "supervisor.json"
        if not snapshot.is_file():
            continue
        try:
            stamp = snapshot.stat().st_mtime
        except OSError:
            continue
        if best is None or stamp > best[0]:
            best = (stamp, state_dir)
    return best[1] if best else None


def discover_project_root(start: Path) -> Optional[Path]:
    """从 ``start`` 向上寻找带 ``.bugx/`` 的项目根。"""
    probe = Path(start).expanduser()
    try:
        probe = probe.resolve()
    except OSError:
        probe = Path(start)
    for candidate in (probe, *probe.parents):
        if (candidate / _CURRENT_RELPATH).is_file() or (
            candidate / _MANIFEST_RELPATH
        ).is_file():
            return candidate
    return None


def _gate_blockers(
    gate: Mapping[str, Any], findings: Iterable[Mapping[str, Any]],
) -> tuple[str, ...]:
    """推导阻断官方会签的硬约束。

    只有「明确写着未就绪」或「发现有未确认项」两种情形被当作阻断；其余
    （例如 gate 里没有 ``ready`` 字段的老快照）不臆断，交给发现状态判定。
    """
    blockers: List[str] = []
    ready = gate.get("ready")
    if ready is False:
        pending = gate.get("pending")
        detail = f"confirmation_gate.ready=false"
        if isinstance(pending, int) and pending > 0:
            detail += f"（{pending} 项待确认）"
        blockers.append(detail)
    unconfirmed = [
        str(f.get("id") or f.get("fingerprint") or "?")
        for f in findings
        if _status_of(f) not in CONFIRMED_STATUSES
    ]
    if unconfirmed:
        shown = ", ".join(unconfirmed[:5])
        if len(unconfirmed) > 5:
            shown += f" 等 {len(unconfirmed)} 项"
        blockers.append(f"存在未确认发现: {shown}")
    return tuple(blockers)


def load(root: Path | str) -> Authority:
    """读取项目运行态的权威发现集合。

    Raises:
        AuthorityUnavailable: 无 ``.bugx``、无 supervisor 快照或快照不可解析。
    """
    project_root = Path(root).expanduser()
    try:
        project_root = project_root.resolve()
    except OSError as exc:
        raise AuthorityUnavailable("项目根路径不可解析") from exc
    if not (project_root / ".bugx").is_dir():
        raise AuthorityUnavailable("不是 BugX 项目（缺少 .bugx 目录）")

    state_dir = _lifecycle_state_dir(project_root)
    if state_dir is None:
        raise AuthorityUnavailable(
            "没有可用的生命周期运行态（.bugx/pollings/*/lifecycles/*/state/supervisor.json）"
        )
    supervisor_path = state_dir / "supervisor.json"
    payload = _read_json(supervisor_path)
    if not isinstance(payload, Mapping):
        raise AuthorityUnavailable(f"{supervisor_path.name} 不是对象")

    raw_findings = payload.get("findings") or []
    if not isinstance(raw_findings, list):
        raise AuthorityUnavailable(f"{supervisor_path.name} 的 findings 不是数组")
    findings = [item for item in raw_findings if isinstance(item, Mapping)]

    gate = payload.get("confirmation_gate")
    gate = dict(gate) if isinstance(gate, Mapping) else {}
    coverage = payload.get("coverage")
    coverage = {
        str(key): str(value) for key, value in coverage.items()
    } if isinstance(coverage, Mapping) else {}

    try:
        revision = int(payload.get("revision") or 0)
    except (TypeError, ValueError):
        revision = 0

    return Authority(
        project_root=project_root,
        supervisor_path=supervisor_path,
        run_id=str(payload.get("run_id") or ""),
        status=str(payload.get("status") or "UNKNOWN"),
        revision=revision,
        findings=findings,
        confirmation_gate=gate,
        coverage=coverage,
        lifecycle_id=str(payload.get("life_id") or state_dir.parent.name),
        polling_id=str(payload.get("session_id") or state_dir.parents[2].name),
        gate_blockers=_gate_blockers(gate, findings),
    )
