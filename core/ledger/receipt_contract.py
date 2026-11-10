"""收据口径契约的**唯一权威实现**（CLI 核验器与产品侧收据核验共用）。

为什么抽成一份：``scripts/verify_acceptance_evidence.py``（CLI，核验**引擎仓**收据）
与 ``core/launch_phases/post_phase.py::verify_acceptance_receipt``（产品侧，核验
**目标项目轮次封条**）本应是同一套规则的「孪生函数」——审计报告 §8 第 7 项已把它
列为**未实测**的共享缺陷面；实测进一步证实：CLI 里那两个 import（``verify_acceptance_receipt``
与 ``converge_forensics``）**从未被调用**，是死导入，孪生函数**从未接线**。

真实 live 轮次（codex 宿主，`pre → workers → post --final --allow-incomplete`）
的实测结论：产品侧那个孪生函数对 ``coverage_debt_units`` / ``coverage_complete`` /
``allow_incomplete`` / ``lifecycle_stop_reason`` **零校验**，且**工件缺失时静默跳过**
（fail-open）——四种伪造全部「校验通过」：

1. 覆盖债务改成 ``999``（真实账本重算值是 9）⇒ 仍然「校验通过」；
2. 删掉一个被封存工件 ⇒ 仍然「校验通过」；
3. 删掉两个被封存工件 ⇒ 仍然「校验通过」；
4. ``outcome`` 改成 ``COMPLETE``（债务仍 9，终态与债务自相矛盾）且删掉
   ``allow_incomplete`` ⇒ 仍然「校验通过」。

规则**只留一份实现**，两侧共用 ⇒ 孪生分叉从根上消除：CLI 的错误文案与判据逐字
不变（现有回归全绿），产品侧立刻获得同一套 fail-closed 约束。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# 覆盖债务取证的唯一权威实现（R2 审计 P2-3 / P2-4）：必须用**同一份口径**从密封
# 账本重算债务，否则「收据写死字面量 + 工件写另一个字面量」可以自洽。
from core.coverage.coverage_forensics import coverage_debt_units  # noqa: F401 - 本模块对外暴露


def verify_receipt_coverage_contract(
    data: dict[str, Any],
    artifact_files: Mapping[str, Path],
    *,
    contract_ids: Sequence[str] = (),
    strict: bool = True,
    sealed_life_required: bool = True,
) -> tuple[list[str], list[str]]:
    """L2-C1 覆盖债务口径契约（唯一实现）；返回 ``(errors, warnings)``。

    ``artifact_files`` 用**规范名**作键（``"bugx-life.json"`` /
    ``"audit-supervisor.json"`` / …），值是磁盘上的实际路径；调用方负责把
    ``life.json`` / ``supervisor.json`` 这类别名映射过来。

    ``sealed_life_required`` 区分**两种封条形态**（这是唯一一处形态差异，且必须
    显式声明而不是各写一套）：

    - ``True``（**引擎仓封条**，CLI 核验器用）：``bugx-life.json`` 是**覆盖密封件**，
      声明了债务就必须在场、且必须带 ``coverage_debt_units`` 供交叉一致
      （修复轮复核 E4-BL-N14：默认模式曾把它降级成告警 ⇒ 交叉校验被静默跳过）。
    - ``False``（**目标项目轮次封条**，产品侧用）：``life.json`` 是**寿命计数器**
      （``{"life":…, "history":…}``），**不是覆盖密封件**，合法地不含
      ``coverage_debt_units``；那里的权威交叉校验是**按账本重算**
      （``audit-supervisor.json`` → ``coverage_debt_units()``）——真实 live 轮次
      实测该守卫有效（债务改成 999 被精确拦下）。

    两种形态共用同一套**声明一致性 / 终态一致性 / 账本重算**判据。
    """
    errors: list[str] = []
    warnings: list[str] = []
    contract_ids = tuple(contract_ids)

    has_field = "coverage_debt_units" in data
    declared = data.get("coverage_debt_units")
    declares_contract = bool(contract_ids)

    if not has_field:
        if declares_contract:
            errors.append(
                "收据缺少覆盖债务字段 (coverage_debt_units)：声明了契约裁定的收据必须给出债务"
            )
        else:
            warnings.append("收据未声明覆盖债务 (coverage_debt_units)")
    elif declared is None:
        # 显式 null = 账本不可读（≠ 0，也≠ 未声明）
        warnings.append("收据显式声明 coverage_debt_units=null：覆盖账本不可读，无法判断债务")
        if declares_contract:
            errors.append("声明了契约裁定的收据不得以 null 声明覆盖债务：债务必须可读")
    elif isinstance(declared, bool) or not isinstance(declared, int):
        errors.append(f"coverage_debt_units 必须是整数，实际为 {declared!r}")
    elif declared < 0:
        errors.append(f"coverage_debt_units 不得为负数，实际为 {declared}")
    else:
        outcome = str(data.get("outcome") or "").strip().upper()
        complete_claim = data.get("coverage_complete")
        if not isinstance(complete_claim, bool):
            errors.append(
                "收据声明了覆盖债务就必须同时显式声明 coverage_complete（覆盖是否达成）："
                f"当前为 {complete_claim!r}。终态 × 债务的互斥判定不得挂在收据自报的 "
                "layer 字段上——否则改一个字段就能把矛盾降级成告警"
                "（修复轮复核 E3-R2-D1 实测）"
            )
        elif complete_claim and declared > 0:
            errors.append(
                f"收据声明覆盖已达成（coverage_complete=true）但覆盖债务为 {declared}：自相矛盾"
            )
        elif not complete_claim and declared == 0:
            errors.append(
                "收据声明 coverage_complete=false（覆盖未达成）但覆盖债务为 0：自相矛盾"
                "（债务为 0 即范围内无未覆盖单元）"
            )
        elif outcome == "COMPLETE" and declared > 0:
            message = (
                f"终态声明为 COMPLETE 但覆盖债务为 {declared}：终态与债务自相矛盾"
            )
            if declares_contract:
                errors.append(
                    message
                    + f"（收据声明了契约裁定 {sorted(contract_ids)}，"
                    "不得在未覆盖时宣称终态 COMPLETE）"
                )
            else:
                warnings.append(
                    message
                    + "（未声明契约裁定：视为「生命周期终态 + 覆盖未完成」，仅告警）"
                )

    # 收据**声明了**债务（或声明了契约裁定）时，密封件缺字段 / 损坏就是**失败**
    # ——修复轮复核 E4-BL-6 实测：把密封件写成 ``{}`` 或损坏 JSON 时，整段交叉
    # 校验曾被**静默跳过且零告警**，收据声明 ``coverage_debt_units=999`` 照样
    # ``VERIFIED``。收据压根没声明债务时（L1 封条的既有形态），密封件缺该
    # 字段只降级为告警——否则会把合法的历史封条判失败。
    debt_expected = has_field or declares_contract

    # 交叉校验 1：密封件 bugx-life.json 的同名字段。
    # **目标项目轮次封条**形态下这一步不适用（life.json 是寿命计数器，不是覆盖
    # 密封件）⇒ 权威交叉校验改为「按账本重算」（交叉校验 2）。引擎仓封条形态下
    # 它仍是硬要求（E4-BL-N14：默认模式曾把它降级成告警 ⇒ 交叉校验 1 被静默跳过）。
    life_path = artifact_files.get("bugx-life.json")
    if not sealed_life_required:
        life = None
    elif life_path is None or not life_path.is_file():
        message = "找不到密封件 bugx-life.json：覆盖债务无法交叉校验"
        # E4-BL-N14：默认模式曾把这一步降级成告警 ⇒ 交叉校验 1 被静默跳过。
        # 收据声明了债务时，它在**任何模式**下都必须在场。
        (errors if debt_expected else warnings).append(message)
    else:
        try:
            life = json.loads(life_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — 解析失败不得静默跳过交叉校验
            message = f"密封件 bugx-life.json 解析失败，无法交叉校验覆盖债务: {exc}"
            (errors if debt_expected else warnings).append(message)
            life = None
        if isinstance(life, dict):
            if "coverage_debt_units" not in life:
                message = (
                    "密封件 bugx-life.json 缺少 coverage_debt_units：覆盖债务无法交叉校验"
                )
                (errors if debt_expected else warnings).append(message)
            else:
                sealed_debt = life.get("coverage_debt_units")
                if not isinstance(sealed_debt, int) or isinstance(sealed_debt, bool):
                    errors.append(
                        f"密封件 bugx-life.json 的 coverage_debt_units 非法: {sealed_debt!r}"
                    )
                elif declared is None:
                    warnings.append(
                        f"收据未声明覆盖债务，密封件声明为 {sealed_debt}（无法交叉校验）"
                    )
                elif isinstance(declared, int) and not isinstance(declared, bool):
                    if declared != sealed_debt:
                        errors.append(
                            f"收据声明的覆盖债务 {declared} 与密封件 bugx-life.json 的 "
                            f"{sealed_debt} 不一致"
                        )
        else:
            # 密封件**类型错**（合法 JSON 但不是对象）时，交叉校验此前被**静默
            # 跳过、零告警**——修复轮复核 E2-D8 实测：``bugx-life.json`` 写成
            # 数组时，收据声明 ``coverage_debt_units=999`` 仍 ``VERIFIED``。
            message = (
                f"密封件 bugx-life.json 结构非法（应为对象）：{type(life).__name__}："
                "覆盖债务无法交叉校验"
            )
            (errors if debt_expected else warnings).append(message)

    # 交叉校验 2：按唯一权威口径从密封账本重算（E2-D1：收据写硬编码字面量时
    # 唯一能揭穿它的办法——重算值与声明值必须相等）
    supervisor_path = artifact_files.get("audit-supervisor.json")
    if supervisor_path is not None and supervisor_path.is_file():
        try:
            supervisor = json.loads(supervisor_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            message = f"密封件 audit-supervisor.json 解析失败，无法重算覆盖债务: {exc}"
            (errors if debt_expected else warnings).append(message)
            supervisor = None
        if isinstance(supervisor, dict):
            # 走「读不出来 ⇒ None」那条路：``converge_forensics`` 在空账本时会给
            # 0（= 没有债务），那会把「读不出来」重新折叠成「没有债务」
            # （修复轮复核 E3-R2-D5 / E4-BL-N13）。
            recomputed = coverage_debt_units(supervisor)
            if recomputed is None:
                if isinstance(declared, int) and not isinstance(declared, bool):
                    errors.append(
                        f"密封账本不可用或为空：无法按权威口径重算覆盖债务，"
                        f"收据声明的 {declared} 无法核实"
                        "（『读不出来』不得被折叠成『没有债务』——修复轮复核 E3-R2-D5）"
                    )
            elif isinstance(declared, int) and not isinstance(declared, bool):
                if recomputed != declared:
                    errors.append(
                        f"收据声明的覆盖债务 {declared} 与密封账本按权威口径重算的 "
                        f"{recomputed} 不一致（收据必须由账本算出，不得写死字面量）"
                    )
        else:
            # 密封账本**类型错**（合法 JSON 但不是对象）时，整段重算此前被**静默
            # 跳过、零告警**——修复轮复核 E2-D1 实测（定级 P0）：把
            # ``audit-supervisor.json`` 写成 JSON 数组，收据与 ``bugx-life.json``
            # 双双声明 ``coverage_debt_units=999``，核验器仍 ``VERIFIED`` +
            # 退出码 0。解析成功但类型错，与解析失败一样必须判失败。
            message = (
                f"密封件 audit-supervisor.json 结构非法（应为对象）："
                f"{type(supervisor).__name__}：覆盖债务无法按账本重算校验"
            )
            (errors if debt_expected else warnings).append(message)
    elif debt_expected:
        errors.append("找不到密封件 audit-supervisor.json：覆盖债务无法按账本重算校验")
    else:
        warnings.append("找不到密封件 audit-supervisor.json：覆盖债务无法按账本重算校验")

    return errors, warnings


def verify_receipt_terminal_contract(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """「静默 INCOMPLETE」禁止（CORE-306 §9）——唯一实现；返回 ``(errors, warnings)``。

    只有显式 ``--allow-incomplete`` 才能把 INCOMPLETE 写成终态，且必须同时写明
    停止原因。修复轮复核 E2-D9 实测：``allow_incomplete`` / ``lifecycle_stop_reason``
    曾是**零消费者**——删掉这两字段收据仍 ``VERIFIED``，契约承诺没有任何机器强制。
    """

    errors: list[str] = []
    warnings: list[str] = []
    for terminal_field in ("outcome", "lifecycle_status"):
        terminal = str(data.get(terminal_field) or "").strip().upper()
        if terminal != "INCOMPLETE":
            continue
        if data.get("allow_incomplete") is not True:
            errors.append(
                f"收据把 {terminal_field}=INCOMPLETE 写成终态，但未显式声明 "
                "allow_incomplete=true：禁止「静默 INCOMPLETE」（CORE-306 §9）"
            )
        stop_reason = data.get("lifecycle_stop_reason")
        if not isinstance(stop_reason, str) or not stop_reason.strip():
            errors.append(
                f"收据把 {terminal_field}=INCOMPLETE 写成终态，但未写明 "
                "lifecycle_stop_reason：禁止「静默 INCOMPLETE」（CORE-306 §9）"
            )
    return errors, warnings
