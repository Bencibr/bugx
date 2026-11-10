#!/usr/bin/env python3
"""core/goal_governor.py - BugX Goal 模式与目标导向长程自愈状态机 (DOC-149).

提供：
  - GoalSpec / GoalOracle: 目标条件定义与唯一收敛判定器
  - GoalFailureClassifier: 结构化失败分类（废除脆弱的纯文本 "BLOCKED" 判定）
  - QuarantineManager: 坏单元连续失败隔离机制（防止 1% 异常卡死 99% 主线）
  - SelfHealingController: 预算自动续约、租约自愈与反思上下文注入
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_CORE_DIR = Path(__file__).resolve().parent.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))


@dataclass
class GoalSpec:
    """目标规格说明：定义无人值守任务的收敛 Oracle 条件。"""
    description: str = "全量覆盖与漏洞闭环"
    target_coverage: float = 100.0  # 目标覆盖率百分比 (0.0 ~ 100.0)
    require_zero_open_findings: bool = True  # 要求 open_findings 归零
    allow_quarantined_completion: bool = True  # 允许已被隔离的坏单元不阻塞收敛
    max_wall_time: float = 14400.0  # 全局硬熔断时间（秒，0=无限制）
    max_turns: int = 0  # 全局最大回合数（0=无限制）
    max_unit_retries: int = 3  # 单单元最大连续失败重试次数
    max_consecutive_transient_failures: int = 5  # 连续瞬态失败重试上限

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "target_coverage": self.target_coverage,
            "require_zero_open_findings": self.require_zero_open_findings,
            "allow_quarantined_completion": self.allow_quarantined_completion,
            "max_wall_time": self.max_wall_time,
            "max_turns": self.max_turns,
            "max_unit_retries": self.max_unit_retries,
            "max_consecutive_transient_failures": self.max_consecutive_transient_failures,
        }


@dataclass
class GoalProgress:
    """目标推进进度快照。"""
    coverage_percent: float = 0.0
    covered_units: int = 0
    total_units: int = 0
    open_findings: int = 0
    confirmed_findings: int = 0
    fixed_findings: int = 0
    quarantined_units: list[str] = field(default_factory=list)
    is_satisfied: bool = False
    status_summary: str = "initializing"
    unmet_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage_percent": self.coverage_percent,
            "covered_units": self.covered_units,
            "total_units": self.total_units,
            "open_findings": self.open_findings,
            "confirmed_findings": self.confirmed_findings,
            "fixed_findings": self.fixed_findings,
            "quarantined_units": list(self.quarantined_units),
            "is_satisfied": self.is_satisfied,
            "status_summary": self.status_summary,
            "unmet_reasons": list(self.unmet_reasons),
        }


class FailureKind:
    """失败类型枚举分类。"""
    TRANSIENT = "transient"                # 瞬态错误（网络闪断、偶发子进程退出、临时超时）
    RECOVERABLE_BUDGET = "budget"          # 轮次预算耗尽（900s 限制、Token 预算耗尽）
    RECOVERABLE_LEASE = "lease"            # 租约异常（并发槽位满、僵尸租约、未结算 claim）
    REPAIR_NEEDED = "repair_needed"        # 发现漏洞需要转入自动修复流水线
    PROVIDER_LIMIT = "provider_limit"      # 宿主模型提供方限流，等待外部配额恢复
    PROVIDER_AUTH_REQUIRED = "provider_auth_required"  # 宿主模型提供方认证缺失（需人工恢复）
    TERMINAL = "terminal"                  # 真正不可恢复的硬致命错误（认证无效、Git 脏冲突等）


class ClassificationResult(tuple):
    """分类结果元组，同时支持 (kind, detail) 传统解构与结构化字段访问。"""

    failure_kind: str
    detail: str
    retry_window_seconds: int | None
    marker_detail: str

    def __new__(
        cls,
        failure_kind: str,
        detail: str,
        retry_window_seconds: int | None = None,
        marker_detail: str = "",
    ):
        instance = super().__new__(cls, (failure_kind, detail))
        instance.failure_kind = failure_kind
        instance.detail = detail
        instance.retry_window_seconds = retry_window_seconds
        instance.marker_detail = marker_detail
        return instance


class QuarantineManager:
    """坏单元隔离管理器：防止单一单元死锁阻断全局。"""

    def __init__(self, max_retries: int = 3) -> None:
        self.max_retries = max_retries
        self.unit_failures: dict[str, int] = {}
        self.quarantined: dict[str, str] = {}  # unit -> reason

    def record_failure(self, unit: str, reason: str = "") -> bool:
        """记录一次失败，若达到重试上限则隔离，返回是否新被隔离。"""
        count = self.unit_failures.get(unit, 0) + 1
        self.unit_failures[unit] = count
        if count >= self.max_retries and unit not in self.quarantined:
            self.quarantined[unit] = (
                reason or f"连续 {count} 次尝试执行失败，已自动移入隔离区"
            )
            return True
        return False

    def record_success(self, unit: str) -> None:
        """成功则清除失败计数与隔离状态。"""
        self.unit_failures.pop(unit, None)
        self.quarantined.pop(unit, None)

    def is_quarantined(self, unit: str) -> bool:
        return unit in self.quarantined

    def get_quarantined_list(self) -> list[str]:
        return sorted(self.quarantined.keys())


class GoalOracle:
    """目标收敛裁决机：以全局状态与总账本为唯一真源。"""

    def __init__(self, spec: GoalSpec, quarantine: QuarantineManager | None = None) -> None:
        self.spec = spec
        self.quarantine = quarantine or QuarantineManager(spec.max_unit_retries)

    def evaluate(self, root: Path, status_payload: Mapping[str, Any] | None = None) -> GoalProgress:
        """裁决当前项目是否已达成目标。"""
        root = root.resolve()
        ledger_path = root / ".bugx" / "coverage-ledger.json"

        covered_units = 0
        total_units = 0
        open_findings = 0
        confirmed_findings = 0
        fixed_findings = 0

        def _count(value: Any) -> int | None:
            if isinstance(value, bool):
                return None
            try:
                parsed = int(value or 0)
            except (TypeError, ValueError):
                return None
            return max(0, parsed)

        known_units: set[str] = set()
        known_unit_statuses: dict[str, str] = {}

        def _extract_known_units(cov_data: Any) -> None:
            if not isinstance(cov_data, dict):
                return
            units_map = cov_data.get("units")
            if isinstance(units_map, dict):
                for u, st in units_map.items():
                    u_str = str(u)
                    known_units.add(u_str)
                    st_val = st.get("status") if isinstance(st, dict) else st
                    known_unit_statuses[u_str] = str(st_val or "").lower()
            else:
                reserved_keys = {
                    "overall", "total", "covered", "open_findings",
                    "by_kind", "status", "claims", "runtime", "workers",
                }
                for k, v in cov_data.items():
                    if k not in reserved_keys:
                        u_str = str(k)
                        known_units.add(u_str)
                        st_val = v.get("status") if isinstance(v, dict) else v
                        known_unit_statuses[u_str] = str(st_val or "").lower()

        # Layout 2 stores authoritative coverage in the status payload and
        # findings in the active lifecycle.  Keep the legacy files only for
        # uninitialised compatibility projects; never mix both layouts.
        active_scope = None
        try:
            try:
                import core.bugx_paths as bugx_paths
            except ImportError:
                from core import bugx_paths
            active_scope = bugx_paths.require(root)
        except (ImportError, OSError, ValueError, RuntimeError):
            pass

        if active_scope is not None:
            cov = status_payload.get("coverage") if status_payload else None
            if isinstance(cov, dict):
                _extract_known_units(cov)

                overall = cov.get("overall")
                if isinstance(overall, dict):
                    parsed_total = _count(overall.get("total"))
                    parsed_covered = _count(overall.get("covered"))
                else:  # status payload compatibility during staged upgrades
                    parsed_total = _count(cov.get("total"))
                    parsed_covered = _count(cov.get("covered"))
                if parsed_total is None or parsed_covered is None:
                    open_findings = max(open_findings, 1)
                else:
                    total_units = parsed_total
                    covered_units = parsed_covered
                parsed_open = _count(cov.get("open_findings"))
                if parsed_open is not None:
                    open_findings = max(open_findings, parsed_open)
                elif "open_findings" in cov:
                    open_findings = max(open_findings, 1)
            elif status_payload and "coverage" in status_payload and status_payload.get("coverage") is not None:
                # P1: coverage 存在但非 dict 属损坏状态，强制 fail-closed
                open_findings = max(open_findings, 1)
            runtime = status_payload.get("runtime") if status_payload else None
            if isinstance(runtime, dict):
                parsed_open = _count(runtime.get("open_findings"))
                if parsed_open is not None:
                    open_findings = max(open_findings, parsed_open)
                elif "open_findings" in runtime and runtime.get("open_findings") is not None:
                    open_findings = max(open_findings, 1)

            findings_path = active_scope.lifecycle_root / "findings.json"
            if findings_path.is_symlink():
                # 符号链接在安全边界内属非法篡改，强制 fail-closed 阻断
                open_findings = max(open_findings, 1)
            elif findings_path.is_file():
                try:
                    payload = json.loads(findings_path.read_text(encoding="utf-8"))
                    findings = payload.get("findings") if isinstance(payload, dict) else payload
                    if not isinstance(findings, list):
                        raise ValueError("findings 必须是数组")
                    try:
                        from core.ledger import finding_confirmation
                    except ImportError:
                        from core import finding_confirmation
                    open_rows = finding_confirmation.open_findings(findings, root)
                    open_findings = max(open_findings, len(open_rows))
                    confirmed_findings = sum(
                        1 for item in findings
                        if isinstance(item, dict)
                        and isinstance(item.get("confirmation"), dict)
                        and item["confirmation"].get("state") == "confirmed"
                    )
                    fixed_findings = max(0, confirmed_findings - len(open_rows))
                except (ImportError, OSError, ValueError, json.JSONDecodeError):
                    # A malformed active ledger must not unlock goal completion.
                    open_findings = max(open_findings, 1)
        else:
            if ledger_path.is_file():
                try:
                    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                    units = ledger.get("units", {})
                    if isinstance(units, dict):
                        for u, d in units.items():
                            u_str = str(u)
                            known_units.add(u_str)
                            if isinstance(d, dict) and d.get("covered"):
                                known_unit_statuses[u_str] = "covered"
                        total_units = len(units)
                        covered_units = sum(
                            1 for d in units.values()
                            if isinstance(d, dict) and d.get("covered")
                        )
                except (OSError, json.JSONDecodeError):
                    open_findings = max(open_findings, 1)

            if status_payload:
                cov = status_payload.get("coverage")
                if isinstance(cov, dict):
                    _extract_known_units(cov)
                    if total_units == 0:
                        overall = cov.get("overall") if isinstance(cov.get("overall"), dict) else cov
                        parsed_t = _count(overall.get("total"))
                        parsed_c = _count(overall.get("covered"))
                        if parsed_t is None or parsed_c is None:
                            open_findings = max(open_findings, 1)
                        else:
                            total_units = parsed_t
                            covered_units = parsed_c
                    parsed_cov_open = _count(cov.get("open_findings"))
                    if parsed_cov_open is not None:
                        open_findings = max(open_findings, parsed_cov_open)
                runtime = status_payload.get("runtime")
                if isinstance(runtime, dict):
                    parsed_rt_open = _count(runtime.get("open_findings"))
                    if parsed_rt_open is not None:
                        open_findings = max(open_findings, parsed_rt_open)

            findings_dir = root / ".bugx" / "findings"
            if findings_dir.is_dir():
                for f in findings_dir.glob("*.json"):
                    if f.name.endswith(".evidence.json"):
                        continue
                    try:
                        f_data = json.loads(f.read_text(encoding="utf-8"))
                        confirmed_findings += 1
                        status = f_data.get("status", "open")
                        if status in {"fixed", "verified_fixed", "resolved"}:
                            fixed_findings += 1
                        else:
                            open_findings += 1
                    except (OSError, json.JSONDecodeError):
                        # P5b: 单个 finding 损坏不可读时，强制 fail-closed 计入 open_findings 阻断收敛
                        open_findings = max(open_findings, 1)

        # 4. 计算覆盖率
        quarantined_list = self.quarantine.get_quarantined_list()
        if known_units:
            # 只有确实属于当前项目的隔离单元才参与分母扣减，杜绝幽灵名穿透
            effective_quarantined = [u for u in quarantined_list if u in known_units]
            # 只有未覆盖的隔离单元才扣减分母（已覆盖的不重复扣减）
            quarantined_to_deduct = sum(
                1 for u in effective_quarantined
                if known_unit_statuses.get(u) not in {"covered", "passed", "verified"}
            )
            # 若所有单元都被隔离，effective_total 必须归零，防止虚假达成
            if len(effective_quarantined) >= total_units:
                quarantined_to_deduct = total_units
        else:
            quarantined_to_deduct = min(len(quarantined_list), total_units)

        effective_total = total_units
        if self.spec.allow_quarantined_completion:
            effective_total = max(0, total_units - quarantined_to_deduct)

        if effective_total > 0 and total_units > 0:
            coverage_pct = round((covered_units / effective_total) * 100.0, 2)
            coverage_pct = min(100.0, coverage_pct)
        else:
            coverage_pct = 0.0

        # 5. 校验收敛条件
        unmet: list[str] = []
        if coverage_pct < self.spec.target_coverage:
            unmet.append(
                f"覆盖率未达标: 当前 {coverage_pct:.1f}% < 目标 {self.spec.target_coverage:.1f}% "
                f"({covered_units}/{total_units} 单元)"
            )
        if self.spec.require_zero_open_findings and open_findings > 0:
            unmet.append(f"存在 {open_findings} 个未闭环 Finding（需自动修复）")

        is_satisfied = (len(unmet) == 0 and total_units > 0)
        summary = (
            "GOAL_SATISFIED" if is_satisfied
            else f"IN_PROGRESS (覆盖率 {coverage_pct:.1f}%, 待修复漏洞 {open_findings})"
        )

        return GoalProgress(
            coverage_percent=coverage_pct,
            covered_units=covered_units,
            total_units=total_units,
            open_findings=open_findings,
            confirmed_findings=confirmed_findings,
            fixed_findings=fixed_findings,
            quarantined_units=self.quarantine.get_quarantined_list(),
            is_satisfied=is_satisfied,
            status_summary=summary,
            unmet_reasons=unmet,
        )


class GoalFailureClassifier:
    """失败分类器：彻底杜绝纯文本 'BLOCKED' 误杀。"""

    # provider 限流的强标记：这些词只可能出自模型提供方自己的错误信封，
    # 不会出现在被审计应用的正常输出里。
    # 只看**provider 信封独有**的标识。通用 HTTP 措辞（`too many requests` /
    # `rate limit exceeded` / `http 429`）已降级为「需 provider 语境」——它们
    # 正是被测应用自己返回 429 时的标准 reason phrase，无条件命中会把被测应用
    # 的真实缺陷吃成 provider 限流，永远进不了修复流水线（缺陷 #10/#11）。
    PROVIDER_LIMIT_MARKERS = (
        "freeusagelimiterror",
        "free usage limit reached",
        "provider.free-limit",
        "provider_free_limit",
        "provider.quota",
        "provider_quota",
        "provider.throttl",
        "provider_throttled",
        "provider.limit",
        "insufficient_quota",
        "usage limit reached",
        # 裸 free-limit / free_limit：CORE-302 §7 第 9 行把 free-limit 列为触发词。
        "free-limit",
        "free_limit",
        "free limit",
        # 真实宿主余额/配额耗尽形态（Cline 3.0 / OpenAI / Anthropic 等平台）。
        # 注意：``http 429`` 这类**通用** HTTP 措辞不再是无条件强标记，见下方
        # ``STATUS_429_RE`` + provider 语境的合取规则。
        "insufficient balance",
        "insufficient_credits",
        "credits balance",
        "quota exceeded",
        "exceeded your current quota",
    )

    # 429 形态：只认「状态位上下文里的 429」，不认任意出现的 429 数字
    # （端口号、行号、哈希片段都可能带 429）。四种真实形态都必须命中：
    #   ``"status": 429`` / ``status=429``  —— 字段形态（含 statusCode / http_status）
    #   ``status 429``                       —— 空格状态行（cline → OpenRouter 实测载荷）
    #   ``"code": 429``                      —— OpenRouter/Phala 错误对象
    #   ``HTTP 429`` / ``HTTP/1.1 429``      —— HTTP 状态行
    STATUS_429_RE = re.compile(
        r"(?:"
        r"(?:status|status_?code|http_?status|httpstatus|statuscode)"
        r"[\"']?\s*[:=]?\s*[\"']?429\b"
        r"|\bcode[\"']?\s*[:=]\s*[\"']?429\b"
        r"|\bhttp(?:/\d(?:\.\d)?)?\s+429\b"
        r")"
    )

    # provider 语境：只有 provider 自身的错误信封里出现的 429 才算 provider 限流。
    # 被审计应用自己返回的 429 是**真实缺陷**，必须能进修复流水线，不能被限流吃掉。
    # 这里只放 provider **信封标识**（键名/错误码/上游来源字段），不放通用 HTTP 措辞
    # （``too many requests`` / ``rate limit exceeded``）：后者是被测应用 429 响应的
    # 标准 reason phrase，放进语境会让「429 形态 且 provider 语境」的合取规则重新被
    # 通用措辞绕过（缺陷 #10）。厂商名同样不放——引擎内部消息里也会出现厂商名。
    PROVIDER_CONTEXT_RE = re.compile(
        r"\b(?:"
        r"provider|insufficient_quota|free[._\- ]?limit|usage[._\- ]?limit|quota|"
        r"byok|limit_source|upstream|upstream_provider|credits"
        r")\b",
        re.IGNORECASE,
    )

    # 重试窗触发词（CORE-302 §7 第 9 行：retry-after 是独立的 provider 限额触发词）。
    RETRY_HEADER_RE = re.compile(r"\bretry[-_]?after[\"']?\s*[:=]|\bretryafterms[\"']?\s*[:=]", re.IGNORECASE)

    # provider 认证失败的稳定信封标记与认证缺失的**短语**形态。
    # OpenCode 的过期凭据使用 provider.auth + api_key_expired；它不是瞬态错误，
    # 不能因现有“认证缺失”文案差异而被自治循环重复请求。
    # 绝不匹配裸 key ``"auth_required"``——
    # 那是仓内攻击图谱 schema 的必填字段（core/schemas/attack-graph.schema.json），
    # 正常输出里出现它不代表认证失败。
    AUTH_REQUIRED_RE = re.compile(
        r"\bprovider\.auth\b|\bapi_key_(?:expired|invalid|revoked)\b|"
        r"\binvalid_api_key\b|\[auth_required\]|provider authentication required|"
        r"authentication required[^\n]{0,80}provider|"
        r"provider[^\n]{0,80}authentication required",
        re.IGNORECASE,
    )

    # 重试窗上限：超过 30 天的窗口不可能是真实配额窗口，视为取不到窗口。
    MAX_RETRY_WINDOW_SECONDS = 30 * 24 * 3600

    @staticmethod
    def _safe_window_seconds(digits: str, divisor: int = 1) -> int | None:
        """把数字串转成秒级窗口；超长/越界一律按"取不到窗口"处理。

        彻底移除 digits[:15] 截断（防止 21 位前导零导致窗口误判为 0）；
        超长数字串（>100 位）直接拒绝，防止极端大数。
        """

        try:
            if len(digits) > 100:
                return None
            value = int(digits) // divisor
        except ValueError:
            return None
        if value > GoalFailureClassifier.MAX_RETRY_WINDOW_SECONDS:
            return None
        return value

    @staticmethod
    def _retry_window_seconds(lowered: str) -> int | None:
        """提取 provider 重试窗（秒）。入参必须是**已小写**的文本。

        两种真实载荷形态都要认（见 ``state/l1b-acceptance`` 的 429 取证）：

        * ``retry-after: 15385`` / ``"retry-after": 15385`` —— HTTP 头，单位秒；
        * ``"retryAfterMs": 58466000`` —— 宿主 provider 错误对象，单位毫秒。

        L2-C2 要求「重试窗由 retryAfterMs 决定」，取不到窗口就不能凭空编一个。
        使用 ``\\b`` 边界前缀严格匹配，彻底杜绝 maxRetryAfterMs 等假字段误触发。
        """

        millis = re.search(r"\bretryafterms[\"']?\s*[:=]\s*[\"']?(\d+)", lowered)
        if millis:
            return GoalFailureClassifier._safe_window_seconds(millis.group(1), 1000)
        seconds = re.search(r"\bretry[-_]?after[\"']?\s*[:=]\s*[\"']?(\d+)", lowered)
        if seconds:
            return GoalFailureClassifier._safe_window_seconds(seconds.group(1))
        return None

    @staticmethod
    def classify(
        output: str,
        exit_code: int,
        runtime_state: str = "",
        stop_reason: str = "",
        timed_out: bool = False,
    ) -> ClassificationResult:
        """将执行结果分类为 FailureKind 及其详细原因，并附带结构化元数据。"""
        diagnostic = "\n".join(part for part in (output, stop_reason) if part)
        lowered = diagnostic.lower()

        # 1. 宿主指令超时
        if timed_out:
            return ClassificationResult(FailureKind.TRANSIENT, "宿主回合超时，可自愈重试")

        # 2. 轮次预算耗尽（正向续命信号）
        budget_markers = {
            "budget_exhausted", "lifecycle_wall_time_budget",
            "max_round_wall_seconds", "autonomous_wall_time_budget",
        }
        if stop_reason in budget_markers or any(m in output for m in budget_markers):
            return ClassificationResult(FailureKind.RECOVERABLE_BUDGET, "单轮预算耗尽，需自动重置预算 Epoch")

        # 3. 真正不可恢复的硬致命错误：优先级高于 Provider 限流（Defect #21），绝不被限流抢断
        hard_fatal_markers = [
            "GIT_DIRTY_CONFLICT", "AUTH_CREDENTIALS_INVALID",
            "REFUSING_EXTERNAL_SYMLINK", "ENGINE_INTEGRITY_TAMPERED",
            "CANNOT_RESOLVE_PROJECT_ROOT",
        ]
        for marker in hard_fatal_markers:
            if marker in output or marker in stop_reason:
                return ClassificationResult(FailureKind.TERMINAL, f"硬致命配置或完整性错误: {marker}", marker_detail=marker)

        # 4. 发现漏洞需要转入自动修复：优先级高于限流分支（Defect #36），绝不被误判为限流
        if "session_completed_without_zero_open_findings" in stop_reason or (
            runtime_state in {"completed", "running"} and "open findings" in output.lower()
        ):
            return ClassificationResult(FailureKind.REPAIR_NEEDED, "存在已确认漏洞，自动派发修复流水线")

        # 5. 租约/槽位等待（正常调度队列，绝不是致命阻断）
        lease_markers = [
            "当前并发槽位已满", "等待已有 worker 结算",
            "current concurrency slots full", "worker_slots_full",
        ]
        if any(m in output for m in lease_markers):
            return ClassificationResult(FailureKind.RECOVERABLE_LEASE, "并发槽位满，等待租约回收结算")

        # 6. Provider 限流合取判定：【429 形态】 AND 【Provider 上下文语境】，或独立的 retry-after 头
        provider_limit = any(
            marker in lowered for marker in GoalFailureClassifier.PROVIDER_LIMIT_MARKERS
        )
        if not provider_limit:
            # 合取规则（L2-C2 定序）：429 **形态** 且 provider **语境**。
            has_429 = bool(GoalFailureClassifier.STATUS_429_RE.search(lowered))
            has_provider_context = bool(
                GoalFailureClassifier.PROVIDER_CONTEXT_RE.search(lowered)
            )
            provider_limit = (has_429 and has_provider_context) or bool(
                GoalFailureClassifier.RETRY_HEADER_RE.search(lowered)
            )
        if provider_limit:
            retry_after = GoalFailureClassifier._retry_window_seconds(lowered)
            if any(k in lowered for k in ("insufficient", "credit", "quota")):
                detail = "宿主模型提供方余额或配额耗尽（provider credits/quota exhausted）"
            else:
                detail = "宿主模型提供方限流（HTTP 429）"
            if retry_after is not None:
                detail += f"；retry-after={retry_after}秒"
            return ClassificationResult(
                FailureKind.PROVIDER_LIMIT,
                detail,
                retry_window_seconds=retry_after,
                marker_detail="provider_rate_limited",
            )

        # 7. provider 认证缺失：必须排在硬致命与修复流水线判定之后。
        # 未认证会话重试永远不会自愈，所以不能按瞬态错误无限重试；但它也绝不能
        # 抢走硬致命（凭据损坏）与已确认漏洞（自动修复流水线）的判定。
        # 只认短语形态，绝不匹配裸 key "auth_required"——那是仓内攻击图谱 schema
        # 的必填字段（core/schemas/attack-graph.schema.json），正常输出里出现它
        # 不代表认证失败。
        if GoalFailureClassifier.AUTH_REQUIRED_RE.search(lowered):
            return ClassificationResult(
                FailureKind.PROVIDER_AUTH_REQUIRED,
                "宿主模型提供方认证缺失或无效（provider auth required/invalid）",
                marker_detail="provider_auth_required",
            )

        # 8. 显式终态标记（以行首 BLOCKED: 或 RECOVERY_REQUIRED 为指令，废除全局子串误杀）
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("BLOCKED:") or stripped == "BLOCKED":
                return ClassificationResult(FailureKind.TERMINAL, stripped)
            if "RECOVERY_REQUIRED" in stripped:
                return ClassificationResult(FailureKind.RECOVERABLE_LEASE, "宿主请求租约/状态恢复 (RECOVERY_REQUIRED)")

        # 9. 单步非零退出或普通异常 -> 统归为可自愈的瞬态错误
        if exit_code != 0:
            return ClassificationResult(FailureKind.TRANSIENT, f"子任务非零退出 (exit={exit_code})，注入上下文自愈重试")

        # 10. 普通运行态
        return ClassificationResult("none", "normal")


class SelfHealingController:
    """自愈执行器：执行预算续约、租约收敛与反思提示词生成。"""

    @staticmethod
    def reset_budget(root: Path) -> bool:
        """调用 audit_supervisor.py reset-budget 自动续约新 Epoch。"""
        try:
            import core.bugx_paths as bugx_paths
        except ImportError:
            from core import bugx_paths

        root = Path(root).resolve()
        engine_sibling = Path(__file__).resolve().parent.parent / "supervisor" / "audit_supervisor.py"
        script_candidates = [
            bugx_paths.global_engine_root() / "supervisor" / "audit_supervisor.py" if os.environ.get("BUGX_GLOBAL_ENGINE_ROOT") else None,
            engine_sibling,
            bugx_paths.global_engine_root() / "supervisor" / "audit_supervisor.py",
            root / "core" / "supervisor" / "audit_supervisor.py",
            root / ".bugx" / "core" / "supervisor" / "audit_supervisor.py",
        ]
        supervisor_script: Path | None = None
        for candidate in script_candidates:
            if candidate is not None and candidate.is_file():
                supervisor_script = candidate
                break

        if supervisor_script is None:
            logger.warning(
                "自治续约失败：未找到 audit_supervisor.py 脚本 (尝试路径: %s)",
                [str(p) for p in script_candidates],
            )
            return False

        try:
            scope = bugx_paths.require(root)
            state_file = scope.supervisor
        except Exception as exc:
            logger.warning("自治续约失败：解析状态文件路径异常: %s", exc)
            return False

        if not state_file.is_file():
            logger.warning("自治续约失败：状态文件不存在 %s", state_file)
            return False

        try:
            res = subprocess.run(
                [
                    sys.executable,
                    str(supervisor_script),
                    "reset-budget",
                    "--root",
                    str(root),
                    "--state",
                    str(state_file),
                    "--force",
                    "--renew-wall-clock",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if res.returncode != 0:
                logger.warning(
                    "自治续约失败 rc=%s: %s",
                    res.returncode,
                    (res.stderr or res.stdout or "")[-500:],
                )
            return res.returncode == 0
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("自治续约执行异常: %s", exc)
            return False

    @staticmethod
    def recover_leases(root: Path) -> bool:
        """触发 Supervisor 租约自愈与僵尸回收。"""
        try:
            # 直接依赖 Supervisor 的持久化原语，避免 Goal governor 与宿主
            # 控制器互相导入；宿主控制器本身仍复用同一段恢复契约。
            try:
                import core.bugx_paths as bugx_paths
            except ImportError:
                from core import bugx_paths
            from core.supervisor.audit_supervisor import Supervisor

            scope = bugx_paths.require(root)
            Supervisor(root, state_path=scope.supervisor).recover()
            return True
        except Exception:
            return False

    @staticmethod
    def build_reflexive_prompt(
        base_prompt: str,
        turn_no: int,
        progress: GoalProgress,
        last_failure_kind: str,
        last_failure_reason: str,
        last_output_snippet: str = "",
    ) -> str:
        """构建注入了自愈反思与目标推进进度的宿主 Prompt。"""
        quarantined_str = (
            f"已隔离故障单元（不阻塞当前轮）：{', '.join(progress.quarantined_units)}"
            if progress.quarantined_units else "无"
        )
        feedback_lines = [
            "",
            "==================================================",
            f"【BugX Goal 自治推进状态看板 - 回合 #{turn_no}】",
            f"- 目标进度: 覆盖率 {progress.coverage_percent:.1f}% ({progress.covered_units}/{progress.total_units} 单元已闭环)",
            f"- 待修复漏洞: {progress.open_findings} 个 (已确认: {progress.confirmed_findings}, 已修复: {progress.fixed_findings})",
            f"- 坏单元隔离: {quarantined_str}",
        ]

        if last_failure_kind == FailureKind.RECOVERABLE_BUDGET:
            feedback_lines.extend([
                "▶ 自愈动作已执行：检测到上一轮墙上时间预算耗尽，已自动调用 reset-budget 续约新 Epoch。",
                "▶ 当前行动指引：请继续运行 `bugx engine --root . pre --autonomous` 推进新轮次挖掘。",
            ])
        elif last_failure_kind == FailureKind.REPAIR_NEEDED:
            feedback_lines.extend([
                "▶ 模式自动切换：当前已发现待修复漏洞，优先进入自动修复流水线。",
                "▶ 当前行动指引：请读取 confirmed findings，生成红测 -> 应用补丁 -> 定向单测复验，修复完成后提交并推进 post。",
            ])
        elif last_failure_kind == FailureKind.TRANSIENT:
            feedback_lines.extend([
                f"▶ 上次回合遇到瞬态异常: {last_failure_reason}",
                "▶ 自愈重试建议：请先读取权威 status，避免重复操作，换用备选方案或微调参数后重试。",
            ])
            if last_output_snippet:
                feedback_lines.append(f"【上回错误尾部摘要】:\n{last_output_snippet[-1500:]}")
        else:
            feedback_lines.append(
                "▶ 当前行动指引：读取最新状态，运行 workers 派发任务，未完成前不要主动停机。"
            )

        feedback_lines.append("==================================================")
        feedback_lines.append("")
        return base_prompt + "\n" + "\n".join(feedback_lines)
