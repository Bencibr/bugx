"""报告背书判定门 (Attestation Gate).

报告引擎的历史缺陷（BUGX-AUDIT）：正式/视觉/PDF 三条渲染通道各自硬编码
``PASS (COMPLIANT)``、``ZERO FP``、``OFFICIALLY AUDITED & COUNTERSIGNED``
与 ``42 TARGETS``，即使收据为空、零发现、零变异，也照抄全篇合规话术。
后果是 ``bugx report both`` 在只有 5 条已确认发现、零变异样本的项目上输出
「0 漏洞 / PASS / 官方会签报告」——把「没测」渲染成「测过且全优」。

本模块把「能否背书」收敛成一处判定，供三条渲染通道共用：

* 未执行对抗性变异 → 击杀率评级与「零假阳性」背书一律 ``NOT-EXERCISED``；
* 没有任何已确认发现 → 不出具官方签章/会签；
* 指标低于基线 → 显式 ``FAIL (BELOW BASELINE)``，不得写 ``PASS``。

所有判定只读 ``ReportDataBundle`` 里已有的实测字段与 ``authority`` 溯源块，
不引入新的指标来源。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

#: 判定词表。渲染层只允许输出这三个词之一，禁止就地拼字符串。
PASS = "PASS (COMPLIANT)"
FAIL = "FAIL (BELOW BASELINE)"
NOT_EXERCISED = "NOT-EXERCISED"
MEASURED = "MEASURED"

#: 靶标数基线（正式报告对标 ">= 20 个开源项目"）。
#: 视为「本引擎已裁决」的发现状态。``ADVISORY-IMPORTED`` 刻意不在其中。
_VERIFIED_STATUSES = frozenset({"CONFIRMED", "VERIFIED_EXPLOITABLE"})

TARGET_BASELINE = 20
#: 变异击杀率基线（ELITE 门槛）。
KILL_RATE_BASELINE = 0.85
#: 误报率容忍上限（百分比）。
FALSE_POSITIVE_BASELINE = 1.0


def measured_verdict(measured: bool, ok: bool) -> str:
    """把「是否实测 + 是否达基线」折叠成一个判定词。

    未实测永远不能升级为 ``PASS``：这是本模块存在的全部理由。
    """
    if not measured:
        return NOT_EXERCISED
    return PASS if ok else FAIL


@dataclass(frozen=True)
class Attestation:
    """一次报告出具能拿到的最强背书等级。

    ``countersigned`` 是唯一允许渲染「官方会签 / 密码学审计印章」的开关：
    它要求**同时**满足「变异实测过」与「至少有一条已确认发现」，因为
    「零变异 + 零发现」的会签报告等于给空白页盖章。
    """

    mutation_exercised: bool
    negative_control_exercised: bool
    confirmed_findings: int
    total_findings: int
    #: 真正跑过「双正例 + 固定负对照」的发现条数（不是打了 confirm 标记的条数）。
    verification_executed: int = 0
    gate_blockers: tuple[str, ...] = ()

    @property
    def findings_present(self) -> bool:
        return self.total_findings > 0

    @property
    def countersigned(self) -> bool:
        """是否具备出具「官方审计会签」的资格。"""
        return (
            self.mutation_exercised
            and self.confirmed_findings > 0
            and not self.gate_blockers
        )

    @property
    def status_line(self) -> str:
        """正式报告签名栏 ``STATUS:`` 字段。"""
        if self.countersigned:
            return "OFFICIALLY AUDITED & COUNTERSIGNED"
        if self.gate_blockers:
            return "NOT ATTESTED // GATE BLOCKED: " + "; ".join(self.gate_blockers)
        if not self.mutation_exercised:
            return "TRIAGE ONLY // MUTATION NOT EXERCISED - NOT COUNTERSIGNED"
        return "NO CONFIRMED FINDINGS // NOT COUNTERSIGNED"

    @property
    def seal_title(self) -> str:
        """HTML/PDF 印章标题。"""
        if self.countersigned:
            return "OFFICIAL CRYPTOGRAPHIC AUDIT SEAL // TAMPER-PROOF ATTESTATION"
        return "UNATTESTED DRAFT // SHA-256 INTEGRITY DIGEST ONLY (NOT AN AUDIT SEAL)"

    @property
    def ascii_status(self) -> str:
        """纯 ASCII 状态串（PDF/日志用）。

        PDF 文本流是 latin-1，中文会整段变成 ``?``；门禁原因里带中文路径，
        因此这里只输出机器可读的英文结论，细节留给 HTML/Markdown 通道。
        """
        if self.countersigned:
            return "OFFICIALLY AUDITED & COUNTERSIGNED"
        if self.gate_blockers:
            return f"NOT ATTESTED // {len(self.gate_blockers)} GATE BLOCKER(S)"
        if not self.mutation_exercised:
            return "TRIAGE ONLY // MUTATION NOT EXERCISED - NOT COUNTERSIGNED"
        return "NO CONFIRMED FINDINGS // NOT COUNTERSIGNED"

    @property
    def pdf_attestation_line(self) -> str:
        """PDF 尾注。"""
        if self.countersigned:
            return "OFFICIAL ATTESTATION: Valid for Board & Regulatory Compliance Audits"
        return "DRAFT - NOT ATTESTED: triage-only export, not valid for compliance attestation"

    @property
    def hero_status(self) -> str:
        """视觉报告顶部状态胶囊。"""
        if self.countersigned:
            return "🟢 ATTESTED // MUTATION EXERCISED"
        if not self.mutation_exercised:
            return "⚪ TRIAGE VERIFIED (变异未执行)"
        return "🟡 FINDINGS UNCONFIRMED"

    @property
    def hero_kill_rate_tag(self) -> str:
        return "[ELITE]" if self.mutation_exercised else "[NOT-EXERCISED]"

    def false_positive_verdict(self) -> str:
        """误报率判定。

        「零假阳性」必须**逐条**可证：只有每条已报告发现都跑过固定负对照
        （``verification_executed == total_findings`` 且全通过）才记为实测。
        零发现（空集统计）、或只验证了其中一部分，都只能标 ``NOT-EXERCISED``。
        """
        measured = (
            self.findings_present
            and self.verification_executed == self.total_findings
        ) or self.mutation_exercised
        return measured_verdict(measured, True)

    def negative_control_verdict(self) -> str:
        """负对照判定。

        只有在**真的执行过**负对照（有 verification 记录）或跑过变异矩阵时才
        能写 ``PASS``；零发现 + 零变异时「100% 放行」是空集统计。
        """
        return measured_verdict(
            self.negative_control_exercised
            or self.verification_executed > 0
            or self.mutation_exercised,
            True,
        )

    def kill_rate_cell(self, overall_kill_rate: float) -> str:
        """击杀率的展示文案：未实测时给文字而不是数字。

        表格/大字块里写 ``0.00%`` 会被读成「击杀率为零」，写 ``97.35%`` 会被
        当成真实测量。两种误读都必须避免——未实测就只写「未测量」。
        """
        return f"{overall_kill_rate * 100:.2f}%" if self.mutation_exercised else "未测量"

    def target_verdict(self, total_targets: int) -> str:
        return measured_verdict(total_targets > 0, total_targets >= TARGET_BASELINE)

    def kill_rate_verdict(self, overall_kill_rate: float) -> str:
        return measured_verdict(
            self.mutation_exercised, overall_kill_rate >= KILL_RATE_BASELINE,
        )

    def surface_coverage_verdict(self, total_surfaces: int) -> str:
        """暴露面「全覆盖」只有在真的扫到面且覆盖率可证时才成立。"""
        return measured_verdict(total_surfaces > 0, total_surfaces > 0)


def from_bundle(bundle: Any) -> Attestation:
    """从 ``ReportDataBundle`` 推导背书等级。

    判定只依赖实测字段：``metrics.total_mutations``（变异是否真的跑过）、
    发现项状态（确认/待验证）与 ``authority`` 里的门禁阻断原因。
    """
    metrics = bundle.metrics
    authority: Mapping[str, Any] = getattr(bundle, "authority", None) or {}

    # 只有「本引擎亲自裁决过」的状态才计数。``ADVISORY-IMPORTED`` 是对外
    # GHSA/CVE 公告的转述（``from_eval_dir`` 导入路径的默认值），不是本轮
    # 验证结果，因此不计入已裁决数——否则导入 6 条公告就能宣称「6 项确认」。
    confirmed = 0
    for finding in getattr(bundle, "findings", ()) or ():
        status = str(getattr(finding, "status", "") or "").upper()
        if status in _VERIFIED_STATUSES:
            confirmed += 1

    blockers_raw = authority.get("gate_blockers") or ()
    if isinstance(blockers_raw, str):
        blockers_raw = (blockers_raw,)
    blockers = tuple(str(item) for item in blockers_raw if str(item).strip())

    # ``measurements: synthetic`` 表示这些指标来自确定性演示夹具（例如
    # 6-worker 突防收据里的常量探针数），不是对被测代码的真实测量。此时任何
    # 「PASS/95.23% 击杀率」都是对夹具的描述，一律按未实测处理。
    synthetic = str(authority.get("measurements") or "").lower() == "synthetic"
    mutation_exercised = (
        not synthetic and int(getattr(metrics, "total_mutations", 0) or 0) > 0
    )
    verification_executed = int(authority.get("verification_executed") or 0)
    negative_exercised = (
        mutation_exercised
        or (not synthetic and int(getattr(metrics, "total_probes", 0) or 0) > 0)
        or verification_executed > 0
    )
    return Attestation(
        mutation_exercised=mutation_exercised,
        negative_control_exercised=negative_exercised,
        confirmed_findings=confirmed,
        total_findings=len(getattr(bundle, "findings", ()) or ()),
        verification_executed=verification_executed,
        gate_blockers=blockers,
    )
