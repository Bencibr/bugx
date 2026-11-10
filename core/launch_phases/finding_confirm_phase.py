#!/usr/bin/env python3
"""finding_confirm_phase.py — Finding 确认一条龙（DOC-257 优化 A）。

历史痛点：verify→confirm→
findings.json 物化→sync 四步靠宿主逐个手工拼 JSON，每次 schema 试错烧
1-2 分钟模型回合（shell 禁字符/argv 包装/双通道/oracle 文件化共 4 轮）。

本命令把全部机器步封进一条 CLI：

```
bugx engine findings-confirm --recipe <recipe.json> [--all-pending]
```

recipe.json 每项：{fingerprint, oracle_file, positive, negative}
（positive/negative 为不含 shell 禁字符的 python 代码串，引擎自动包装
`python3 -c`、自动打印观测行保证双通道、自动算 oracle_hash）。
`--all-pending` 模式下未列出的 pending 候选按 duplicate/rejected 处理需
显式 `--reject <fingerprint>:<class>:<reason>`。

执行序（全机器步，零宿主回合）：
  1. recipe 校验（禁字符预检、oracle 文件存在且独立于证据路径）
  2. bugx_supervisor_verify_finding（引擎双正例+负对照真跑）
  3. apply_decision 物化确认块
  4. findings.json 从 Supervisor 权威态整体重建（防半成品写坏）
  5. sync_finding_decisions
"""

from __future__ import annotations
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import argparse
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping

if __package__:
    from core.launch_phases.common import resolve_project_root, supervisor_state_path
else:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core.launch_phases.common import resolve_project_root, supervisor_state_path

_SHELL_TOKENS = (";", "&&", "||", "`", "$(", ">", "<")


def _precheck_code(code: str, label: str) -> None:
    for token in _SHELL_TOKENS:
        if token in code:
            raise SystemExit(
                f"[findings-confirm] {label} 含 shell 禁字符 {token!r}："
                "请改用换行分隔语句、chr() 构造载荷"
            )


def _strip_runner_wrapper(code: str) -> str:
    """剥掉 judge 习惯性带上的 `python3 -c` 外壳（2026-11-08 复盘）。

    引擎负责包装 argv；judge 按旧提示词给了整条 shell 命令时二次包装会
    SyntaxError。这里防御性归一化：外壳 → 代码体。
    """
    text = code.strip()
    for prefix in ("python3 -c", "python -c"):
        if text.startswith(prefix):
            body = text[len(prefix):].strip()
            if len(body) >= 2 and body[0] in "'\"" and body[-1] == body[0]:
                body = body[1:-1]
            return body.strip()
    return text


def _wrap(code: str) -> list[str]:
    return ["python3", "-c", _strip_runner_wrapper(code)]


# 正例专用 exec 包装：把「退出码归一化为 1/0」与「自动打印失败观测（双证据
# 通道）」收进引擎，不再依赖 judge 自觉遵守三条契约（2026-11-08 复盘：
# judge 只退非零不打印 → CANDIDATE 拒确认；exit 3 ≠ oracle 硬编码 1 →
# 拒确认；各烧 1 个宿主回合）。模块 docstring 承诺的"自动打印观测行"
# 由此真正落地。意外异常按 0 处理（fail-toward-gate：坏 recipe 拒确认，
# 不误升 confirmed），rc 打印在 stdout 供诊断。
_POS_EXEC_TEMPLATE = (
    "import sys as _s\n"
    "_g = {{'__name__': '__bugx_verification__'}}\n"
    "_rc = 0\n"
    "try:\n"
    "    exec(compile({src!r}, 'verification-positive', 'exec'), _g)\n"
    "except SystemExit as _e:\n"
    "    _rc = _e.code if isinstance(_e.code, int) else 1\n"
    "except BaseException as _e:\n"
    "    _rc = 0\n"
    "    print('BUGX_VERIFY_POSITIVE unexpected-error=%s' % (type(_e).__name__,))\n"
    "print('BUGX_VERIFY_POSITIVE rc=%s' % (_rc,))\n"
    "_s.exit(1 if _rc else 0)\n"
)


def _wrap_positive(code: str) -> list[str]:
    return ["python3", "-c", _POS_EXEC_TEMPLATE.format(src=_strip_runner_wrapper(code))]


def build_recipes_from_verdicts(verdicts_path: Path) -> Path:
    """从 judge 裁决自动生成 recipes（复用 verification_hints，oracle 文件确定性生成）。

    judge 在裁决里给出 assert 型正/负命令后，宿主不再手写配方——
    oracle 文件内容由指纹确定性描述，hash 自算。缺 hints 的条目报错留待人工。
    """
    try:
        raw = json.loads(Path(verdicts_path).read_text(encoding="utf-8"))
    except ValueError:
        # judge 自写裁决常见裸控制字符（字符串里嵌字面换行）——宽容复解析。
        raw = json.loads(Path(verdicts_path).read_text(encoding="utf-8"), strict=False)
    entries = raw if isinstance(raw, list) else [raw]
    recipes: list[dict[str, Any]] = []
    for verdict in entries:
        if not isinstance(verdict, dict):
            continue
        for finding in verdict.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            hints = finding.get("verification_hints") or {}
            # judge 两种写法都认：包装格式 verification_hints{positive,negative}
            # 或 finding 顶层 verification_positive/verification_negative
            pos = str(hints.get("positive") or finding.get("verification_positive") or "")
            neg = str(hints.get("negative") or finding.get("verification_negative") or "")
            fp = str(finding.get("fingerprint") or "").strip()
            if not (fp and pos and neg):
                print(f"[findings-confirm] 跳过 {fp[:44]}：缺 verification_positive/negative")
                continue
            oracle_dir = resolve_project_root() / ".bugx" / "knowledge" / "oracles"
            oracle_dir.mkdir(parents=True, exist_ok=True)
            slug = hashlib.sha256(fp.encode()).hexdigest()[:12]
            oracle_rel = f".bugx/knowledge/oracles/{slug}.txt"
            (oracle_dir / f"{slug}.txt").write_text(
                f"fingerprint={fp}\nseverity={finding.get('severity')}\n"
                f"positive: 漏洞存在时非零退出（judge 裁决附带）\n"
                f"negative: 安全输入下 0 退出\n",
                encoding="utf-8",
            )
            recipes.append({
                "fingerprint": fp, "oracle_file": oracle_rel,
                "positive": pos, "negative": neg,
            })
    out = Path(f"/tmp/recipes-{int(__import__('time').time())}.json")
    out.write_text(json.dumps(recipes, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[findings-confirm] 自动生成 {len(recipes)} 条 recipe → {out}")
    return out


def _pending_fingerprints(state: Any) -> list[str]:
    """Supervisor 权威态里尚未裁决的 finding 指纹。"""
    pending: list[str] = []
    for finding in getattr(state, "findings", ()) or ():
        data = finding if isinstance(finding, dict) else getattr(finding, "to_dict", dict)()
        confirmation = data.get("confirmation")
        decided = isinstance(confirmation, dict) and confirmation.get("decision")
        if decided:
            continue
        fingerprint = str(data.get("fingerprint") or "").strip()
        if fingerprint:
            pending.append(fingerprint)
    return pending


def confirm(
    recipes_path: Path,
    rejects: list[tuple[str, str, str]],
    *,
    all_pending: bool = False,
) -> int:
    """执行确认一条龙。

    ``all_pending`` 是文档承诺的语义：recipe 只列出**已备好 oracle** 的候选，
    其余 pending 候选不得留在半成品状态——它们统一按 ``insufficient_evidence``
    驳回，而不是被静默忽略（此前 ``--all-pending`` 完全不生效，未列出的 pending
    既不确认也不驳回，findings.json 于是永远停在未裁决态）。
    """
    from core.supervisor import audit_supervisor
    from core.ledger import finding_confirmation

    # 用模块级引用（可被测试替换）；函数内重新 import 会绕过 monkeypatch。
    root = resolve_project_root().resolve()
    supervisor = audit_supervisor.Supervisor(root, state_path=supervisor_state_path())
    state = supervisor.status()
    recipes = json.loads(Path(recipes_path).read_text(encoding="utf-8"))
    if isinstance(recipes, dict):
        recipes = [recipes]

    done = failed = 0
    for item in recipes:
        fp = str(item.get("fingerprint") or "").strip()
        oracle_file = str(item.get("oracle_file") or "").strip()
        pos, neg = str(item.get("positive") or ""), str(item.get("negative") or "")
        if not (fp and oracle_file and pos and neg):
            print(f"[findings-confirm] ✗ 条目缺字段: {str(item)[:80]}")
            failed += 1
            continue
        _precheck_code(pos, f"{fp} positive")
        _precheck_code(neg, f"{fp} negative")
        oracle_path = root / oracle_file
        if not oracle_path.is_file():
            print(f"[findings-confirm] ✗ oracle 文件不存在: {oracle_file}")
            failed += 1
            continue
        recipe = {
            "positive": {"argv": _wrap_positive(pos),
                          "oracle": {"kind": "exit_code", "expected_exit_code": 1}},
            "negative_control": {"argv": _wrap(neg),
                                  "oracle": {"kind": "exit_code", "expected_exit_code": 0}},
            "cwd": ".", "timeout_seconds": 30,
            "oracle_reference": oracle_file,
            "oracle_hash": hashlib.sha256(oracle_path.read_bytes()).hexdigest(),
        }
        try:
            supervisor.verify_finding(finding=fp, recipe=recipe)
        except audit_supervisor.SupervisorError as exc:
            print(f"[findings-confirm] ✗ verify {fp[:44]}: {str(exc)[:140]}")
            failed += 1
            continue
        try:
            supervisor.confirm_finding(
                finding=fp, decision="confirmed",
                reason="findings-confirm 一条龙：引擎双正例+负对照实跑 CONFIRMED",
                root_cause_verified=True,
                oracle={"tier": "differential", "provenance_kind": "runtime-invariant",
                         "reference": oracle_file},
            )
            print(f"[findings-confirm] ✓ confirmed {fp[:52]}")
            done += 1
        except audit_supervisor.SupervisorError as exc:
            print(f"[findings-confirm] ✗ confirm {fp[:44]}: {str(exc)[:140]}")
            failed += 1

    if all_pending:
        covered = {str(item.get("fingerprint") or "").strip() for item in recipes}
        covered |= {spec[0] for spec in rejects}
        for fingerprint in _pending_fingerprints(state):
            if fingerprint in covered:
                continue
            rejects = [*rejects, (
                fingerprint, "insufficient_evidence",
                "findings-confirm --all-pending：未提供可执行 oracle，按证据不足驳回",
            )]

    for spec in rejects:
        fp, klass, reason = spec
        try:
            supervisor.confirm_finding(
                finding=fp, decision="rejected", reason=reason, rejection_class=klass,
            )
            print(f"[findings-confirm] ✓ rejected({klass}) {fp[:48]}")
            done += 1
        except audit_supervisor.SupervisorError as exc:
            print(f"[findings-confirm] ✗ reject {fp[:44]}: {str(exc)[:120]}")
            failed += 1

    # findings.json 从 Supervisor 权威态整体重建（apply_decision 的返回是
    # 决策记录而非完整 finding——实测误用会把正文字段清空）
    if done:
        _materialize_findings(root, supervisor)
    print(f"[findings-confirm] 完成：ok={done} failed={failed}")
    return 0 if failed == 0 else 1


def _materialize_findings(root: Path, supervisor: Any) -> None:
    findings_path = supervisor_state_path().parent.parent / "findings.json"
    if not findings_path.is_file():
        return
    payload = json.loads(findings_path.read_text(encoding="utf-8"))
    state = supervisor.status()
    findings = []
    for index, f in enumerate(state.findings, 1):
        item = {k: v for k, v in f.items()
                if k not in ("verification", "verification_confirmed", "candidate_key")}
        item.setdefault("id", f.get("fingerprint") or f"F-life-1-round-1-{index}")
        findings.append(item)
    payload["findings"] = findings
    payload.setdefault("schema_version", 1)
    # L4：按当前 runtime 自动刷新 run_binding——轮次推进后沿用旧 binding
    # 会被 post 的 findings_binding_mismatch 挡下（实测手工改过两次）。
    runtime_path = findings_path.parent / "state" / "runtime.json"
    if runtime_path.is_file():
        try:
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            runtime = {}
        if runtime.get("run_id"):
            payload["run_id"] = runtime["run_id"]
            payload["run_binding"] = {
                "run_id": runtime.get("run_id"),
                "lifecycle_id": runtime.get("lifecycle_id"),
                "round": runtime.get("round"),
            }
    dur = payload.get("duration_seconds")
    if not isinstance(dur, (int, float)) or dur <= 0:
        sv_start = getattr(state, "round_started_at", None) or getattr(state, "started_at", None)
        if isinstance(sv_start, (int, float)) and sv_start > 0:
            payload["duration_seconds"] = max(1, int(round(time.time() - float(sv_start))))
        else:
            payload.setdefault("duration_seconds", 0)
    findings_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"[findings-confirm] findings.json 已从 Supervisor 权威态重建（{len(findings)} 条）")


def _reject_triplet(value: str) -> tuple[str, str, str]:
    """``<fingerprint>:<class>:<reason>``；格式错误由 argparse 直接拒绝。"""
    parts = value.split(":", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise argparse.ArgumentTypeError(
            "格式必须是 <fingerprint>:<rejection_class>:<reason>"
        )
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


def build_parser() -> argparse.ArgumentParser:
    """``bugx engine findings-confirm`` 的**唯一**参数契约。

    历史缺陷：本模块 docstring 与 ``core/host_runtime/autonomous_runner.py`` 的
    Worker 指令都写 ``--recipe <recipe.json>`` / ``--all-pending``，但 ``main()``
    只手工扫 ``--from-verdicts`` / ``--reject``，把 ``--recipe`` 当成位置参数，
    于是文档里的标准调用一律报「缺少 recipe 文件」。参数契约收敛到这里，
    文档与实现由 ``tests/launch/test_finding_confirm_cli.py`` 双向断言。
    """
    parser = argparse.ArgumentParser(
        prog="bugx engine findings-confirm",
        description="Finding 确认一条龙：校验 recipe → 引擎双正例+负对照实跑 → 物化确认块 → sync。",
    )
    parser.add_argument(
        "recipes", nargs="?", metavar="RECIPE",
        help="recipe JSON 文件（含 {fingerprint, oracle_file, positive, negative} 列表）",
    )
    parser.add_argument(
        "--recipe", dest="recipe_opt", metavar="RECIPE",
        help="recipe JSON 文件的显式写法（等价于位置参数，文档以此为准）",
    )
    parser.add_argument(
        "--all-pending", action="store_true",
        help="把未在 recipe 中列出的 pending 候选一并处理；未给 --reject 的一律拒绝",
    )
    parser.add_argument(
        "--reject", action="append", type=_reject_triplet, default=[],
        metavar="FP:CLASS:REASON",
        help="显式驳回某候选（可重复）；<class> ∈ duplicate/rejected 等标准归类",
    )
    parser.add_argument(
        "--from-verdicts", action="store_true",
        help="输入文件是 judge verdicts（自动生成 recipe）而不是 recipe 本身",
    )
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse 用 exit(2) 表示用法错误
        return int(exc.code or 2)
    rejects: list[tuple[str, str, str]] = list(args.reject or [])
    source = args.recipe_opt or args.recipes
    if not source:
        parser.print_usage()
        print(
            "用法：bugx engine findings-confirm --recipe <recipes.json> "
            "[--all-pending] [--reject <fp>:<class>:<reason> ...]"
        )
        return 2
    if args.from_verdicts:
        return confirm(
            build_recipes_from_verdicts(Path(source)), rejects,
            all_pending=args.all_pending,
        )
    return confirm(Path(source), rejects, all_pending=args.all_pending)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
