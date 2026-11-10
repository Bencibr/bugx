"""覆盖债务取证的**唯一权威实现**（L2-C1 / R2 审计 P2-3 + P2-4）。

为什么下沉到 ``core/``：

* ``post`` 封存 ``INCOMPLETE`` 时，判定依据在 supervisor 的覆盖账本里
  （``coverage`` 单元表 + ``parallel_gate`` 的 pending/unclaimed 计数）。
* 改造前这份口径只存在于 ``scripts/smoke_host_native_task.py``，于是
  「产品侧结算收据」无法导出债务数字（L2-C1 的「回执含 ``coverage_debt_units``」
  只由测试 harness 满足），而仓内另有 2 套口径各自算账（P2-4）。
* 现在 L1 探针、L2 探针、产品侧结算三处都引用本模块——**口径只有一份**。

口径定义（不得私自改动）：``coverage_debt_units = max(coverage_units - covered_units, 0)``，
其中 ``coverage_units`` 取 ``parallel_gate.coverage_units``（缺失时退化为覆盖账本条目数），
``covered_units`` 是覆盖状态属于 supervisor 权威 ``TERMINAL_COVERED``
（``covered`` / ``not_applicable`` / ``removed`` / ``superseded``）的单元数——
**逐字匹配，不并入任何宽松词、不做大小写归一化**（supervisor 侧会把未知状态
判为非法账本，见 ``core/supervisor_models.py`` 的 ``VALID_COVERAGE_STATES``）。

**「读不出来」与「没有欠账」必须分开**（R2 审计 P1-4 / 修复轮复核 E3-R2-D5 /
E4-BL-N13）：账本为空或不可读时，本模块返回 ``None``（= 无法判断），**不是** ``0``。
改造前 ``converge_forensics`` 与 ``coverage_debt_units`` 两个出口对同一份空账本
分别给出 ``0`` 与 ``None``——「读不出来」被悄悄折叠成「没有债务」，收据因此可以
声明一个无法核实的债务数字。本模块现在**只有一个**债务口径出口，两个函数共用它。
"""

from __future__ import annotations

from typing import Any

# 覆盖状态词表**只用** supervisor 的权威定义，不另起一份（R2 审计 P2-4 / 修复轮
# 复核 E2-D6 / E3-D2a）：改造前本模块自造 `{covered, complete, completed, verified}`
# 词表，把 supervisor 认作「已终结覆盖」的 `not_applicable` / `removed` /
# `superseded` 当成债务 ⇒ **收据高估债务**；而 supervisor 自己的
# `coverage_complete` 判定用的是 `TERMINAL_COVERED` ⇒ 同一账本两套口径、两个数字。
from core.supervisor.supervisor_models import TERMINAL_COVERED  # noqa: F401 — 词表唯一真源

#: 计入「已覆盖」的覆盖状态词表 = supervisor 的权威 ``TERMINAL_COVERED``
#: （covered / not_applicable / removed / superseded）。
#:
#: **本模块不再并入任何「宽松词」**（修复轮复核 E2-D5 实测）：``complete`` /
#: ``completed`` / ``verified`` 在 supervisor 侧**根本不是合法覆盖状态**
#: （``core/supervisor_models.py`` 的 ``VALID_COVERAGE_STATES`` 校验会对未知状态抛
#: ``PersistenceError("coverage 含未知状态")``）。把它们当「已覆盖」，等于凭空
#: 造出第三套语义：账本写 ``verified`` 时本模块报「债务 0 / 覆盖已达成」，而
#: supervisor 侧判定该账本**非法**。⇒ 词表与 supervisor 逐字一致，**不做大小写
#: 归一化**（``Covered`` / ``COVERED`` 都不是合法状态，不得被当已覆盖）。
COVERED_STATES = frozenset(TERMINAL_COVERED)


def converge_forensics(
    runtime: dict[str, Any] | None, supervisor: dict[str, Any] | None,
) -> dict[str, Any]:
    """把「为什么是 INCOMPLETE」变成可读数字，而不是让人重跑一遍猜。

    ``post`` 封存 ``INCOMPLETE`` 时，真正的判定依据在 supervisor 的覆盖账本里
    （``coverage`` 单元表 + ``parallel_gate`` 的 pending/unclaimed 计数）。C 路径
    探针只驱动有限次 claim，因此**覆盖债务是预期内的诚实结果**；缺了这层取证，
    后来者无法区分「探针故意少覆盖」与「收敛逻辑坏了」。

    覆盖债务与覆盖达成与否**共用** ``coverage_debt_units()`` 这一个口径出口
    （修复轮复核 E3-R2-D5）：账本读不出来时，两者都是 ``None``，绝不折叠成 ``0``。
    """

    runtime = runtime or {}
    supervisor = supervisor or {}
    coverage = supervisor.get("coverage") if isinstance(supervisor.get("coverage"), dict) else {}
    gate = supervisor.get("parallel_gate") if isinstance(supervisor.get("parallel_gate"), dict) else {}
    covered = [unit for unit, state in coverage.items() if str(state) in COVERED_STATES]
    forensic: dict[str, Any] = {
        "runtime_state": runtime.get("state"),
        "lifecycle_outcome": runtime.get("lifecycle_outcome"),
        "coverage_units": gate.get("coverage_units", len(coverage)),
        "covered_units": len(covered),
        "pending_units": gate.get("pending_units"),
        "unclaimed_pending_units": gate.get("unclaimed_pending_units"),
        "parallel_satisfied": gate.get("satisfied"),
        "parallel_serial_reason": gate.get("serial_reason"),
        "max_active_workers": gate.get("max_active_workers"),
    }
    debt = coverage_debt_units(supervisor)
    forensic["coverage_debt_units"] = debt
    # 债务读不出来 ⇒ 覆盖是否达成同样无法判断（不得谎报 True）。
    forensic["coverage_complete"] = None if debt is None else (debt == 0)
    return forensic


def coverage_debt_units(supervisor: dict[str, Any] | None) -> int | None:
    """只取覆盖债务数字；账本不可用时返回 ``None``（不猜、不填 0）。

    ``None`` 与 ``0`` 语义不同：``0`` 是「确实没有欠账」的结论，``None`` 是
    「账本读不出来、无法判断」。收据里必须区分这两者，否则「读不出来」会被
    悄悄当成「没有债务」（R2 审计 P1-4：改 0/999/-5 仍 VERIFIED）。

    判定「账本可用」的标准是**存在非空覆盖账本**；``parallel_gate`` 缺失时
    仍可由账本条目数推出分母，不算不可用。

    这是本模块**唯一**的债务口径出口——``converge_forensics`` 也走它，
    两个出口对同一份账本必须给出同一个数（修复轮复核 E3-R2-D5）。
    """

    if not isinstance(supervisor, dict):
        return None
    coverage = supervisor.get("coverage")
    if not isinstance(coverage, dict) or not coverage:
        return None
    gate = supervisor.get("parallel_gate")
    if not isinstance(gate, dict):
        gate = {}
    total = gate.get("coverage_units", len(coverage))
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    covered = sum(
        1 for state in coverage.values() if str(state) in COVERED_STATES
    )
    return max(total - covered, 0)
