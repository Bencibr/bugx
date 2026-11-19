#!/usr/bin/env python3
"""core/probes/sast_fixture_corpus.py — SAST 规则回归语料运行器。

规则改动最容易出的两类事故，都只有「跑一遍真语料」才能发现：

* **假阳性**：新加的判据把 ``RegExp.exec``、列表式 ``subprocess.run`` 一起报了；
* **假阴性**：判据收得过紧，把 ``urlopen(base + "/health")`` 这类真阳性误杀了。

后者更危险——它让规则"看起来更干净"，实际是漏报。所以语料里正向用例的数量
不少于负向用例。

语料本身是**长期证据资产**，存放在 ``verification/sast-fixtures/<rule_id>/``，
不受 ``AGENTS.md`` 中"测试过程文件必须清理"的约束（``verification/`` 是证据域）。
用例文件用 ``.fixture`` 后缀而不是 ``.py``/``.js``：语料里是故意写成有漏洞的代码，
不能让它们被仓库全量 SAST 扫成告警，也不能被当成单元测试源码收集。

每个用例是一个独立文件，头部声明契约（``case`` 可省略，默认取文件名）::

    # bugx-sast-fixture: rule=SEC-SSRF-001 lang=python expect=hit
    # bugx-sast-fixture-xfail: <仅当期望与当前规则实现不符时出现，写明缺陷原因>

``expect=hit`` 要求该 rule_id 命中，``expect=miss`` 要求不命中。``xfail`` 表示
"这是规则的已知缺陷，用例保持红状态作为待修目标"——**不要**为了让套件变绿而把
期望改成当前行为。

用法::

    python3 -m core.probes.sast_fixture_corpus --root .
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

FIXTURE_DIR = Path("verification") / "sast-fixtures"
HEADER_PREFIX = "# bugx-sast-fixture:"
XFAIL_PREFIX = "# bugx-sast-fixture-xfail:"

# 语料里声明的语言 -> 临时文件后缀（解析器按后缀判语言）
LANG_SUFFIX = {"python": ".py", "javascript": ".js", "typescript": ".ts"}


@dataclass
class FixtureCase:
    path: Path
    case_id: str
    rule_id: str
    language: str
    expect_hit: bool
    xfail_reason: str
    source: str


@dataclass
class CaseOutcome:
    case: FixtureCase
    hit: bool
    matched_rule_ids: tuple[str, ...]

    @property
    def is_xfail(self) -> bool:
        return bool(self.case.xfail_reason)

    @property
    def ok(self) -> bool:
        """结果符合契约。xfail 用例"仍然失败"才算符合。"""
        return self.hit == self.case.expect_hit

    @property
    def xpass(self) -> bool:
        """xfail 用例意外变绿：说明缺陷已修，标记该摘掉了。"""
        return self.is_xfail and self.ok


def _resolve_root(root: Path | str | None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve()
    # 默认取本模块所在仓库根，保证从任意 cwd 调用都能找到语料
    return Path(__file__).resolve().parents[2]


def _parse_header(text: str) -> tuple[dict[str, str], str]:
    """解析头部契约，返回 (字段字典, xfail 原因)。"""
    fields: dict[str, str] = {}
    xfail_reason = ""
    for line in text.splitlines()[:6]:
        stripped = line.strip()
        if stripped.startswith(XFAIL_PREFIX):
            xfail_reason = stripped[len(XFAIL_PREFIX):].strip()
            continue
        if not stripped.startswith(HEADER_PREFIX):
            continue
        for token in stripped[len(HEADER_PREFIX):].split():
            if "=" in token:
                key, value = token.split("=", 1)
                fields[key.strip()] = value.strip()
    return fields, xfail_reason


def load_cases(root: Path | str | None = None) -> tuple[list[FixtureCase], list[str]]:
    """读取全部用例。返回 (用例列表, 结构错误明细)。"""
    base = _resolve_root(root) / FIXTURE_DIR
    if not base.is_dir():
        return [], [f"语料目录不存在: {base}"]

    cases: list[FixtureCase] = []
    errors: list[str] = []
    for path in sorted(base.rglob("*.fixture")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{path}: 读取失败 ({exc})")
            continue
        fields, xfail_reason = _parse_header(text)
        missing = [k for k in ("rule", "lang", "expect") if k not in fields]
        if missing:
            errors.append(f"{path}: 头部缺少字段 {','.join(missing)}")
            continue
        language = fields["lang"]
        if language not in LANG_SUFFIX:
            errors.append(f"{path}: 不支持的语言 {language!r}")
            continue
        expect = fields["expect"]
        if expect not in {"hit", "miss"}:
            errors.append(f"{path}: expect 只能是 hit/miss，实际 {expect!r}")
            continue
        cases.append(
            FixtureCase(
                path=path,
                case_id=fields.get("case", path.stem),
                rule_id=fields["rule"],
                language=language,
                expect_hit=expect == "hit",
                xfail_reason=xfail_reason,
                source=text,
            )
        )
    if not cases and not errors:
        errors.append(f"语料目录为空: {base}")
    return cases, errors


def _strip_header(text: str) -> str:
    """剥掉头部契约行，只把纯源码交给引擎。

    头部用 ``#`` 注释（Python 合法、JS 不合法），且不应参与匹配：否则 JS 用例
    会把 ``# bugx-sast-fixture: ...`` 当成代码文本一起扫。
    """
    lines = text.splitlines()
    index = 0
    while index < len(lines) and (
        not lines[index].strip()
        or lines[index].lstrip().startswith((HEADER_PREFIX, XFAIL_PREFIX))
    ):
        index += 1
    return "\n".join(lines[index:]) + "\n"


def _run_case(case: FixtureCase, workdir: Path, matcher) -> CaseOutcome:
    """把用例源码写成临时源文件后交给规则引擎，返回命中的 rule_id 集合。"""
    from core.retrieval.universal_code_matcher import UniversalCodeMatcher

    target = workdir / f"{case.case_id}{LANG_SUFFIX[case.language]}"
    target.write_text(_strip_header(case.source), encoding="utf-8")
    parsed = UniversalCodeMatcher.parse_file(target, rel_path=target.name)
    findings = matcher.match_file(parsed)
    rule_ids = tuple(sorted({f["rule_id"] for f in findings}))
    return CaseOutcome(case=case, hit=case.rule_id in rule_ids, matched_rule_ids=rule_ids)


def run_cases(root: Path | str | None = None) -> tuple[list[CaseOutcome], list[str]]:
    """执行全部用例，返回 (逐例结果, 结构错误明细)。"""
    from core.probes.sast_rules import BUILTIN_RULES, SASTRuleMatcher

    cases, errors = load_cases(root)
    known_rules = {rule.id for rule in BUILTIN_RULES}
    runnable: list[FixtureCase] = []
    for case in cases:
        if case.rule_id not in known_rules:
            errors.append(f"{case.path}: 未知规则 ID {case.rule_id}")
            continue
        runnable.append(case)

    matcher = SASTRuleMatcher(BUILTIN_RULES)
    workdir = Path(tempfile.mkdtemp(prefix="bugx-sast-fixture-"))
    outcomes: list[CaseOutcome] = []
    try:
        for case in runnable:
            outcomes.append(_run_case(case, workdir, matcher))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return outcomes, errors


def _summarize(outcomes: list[CaseOutcome], errors: list[str]) -> tuple[int, int, list[str]]:
    passed = 0
    failed = len(errors)
    details: list[str] = list(errors)

    for outcome in outcomes:
        case = outcome.case
        actual = "hit" if outcome.hit else "miss"
        expected = "hit" if case.expect_hit else "miss"
        if outcome.xpass:
            failed += 1
            details.append(
                f"XPASS {case.case_id} [{case.rule_id}] 结果已符合期望，缺陷可能已修复，"
                f"请复核并删除 xfail 标记（{case.path}）"
            )
        elif outcome.ok or outcome.is_xfail:
            # 符合契约，或已知缺陷被稳定复现（xfail）
            passed += 1
        else:
            failed += 1
            details.append(
                f"FAIL {case.case_id} [{case.rule_id}] expect={expected} actual={actual}"
                f" matched={','.join(outcome.matched_rule_ids) or '-'}"
            )
    return passed, failed, details


def known_defects(outcomes: list[CaseOutcome]) -> list[str]:
    """已知缺陷（xfail 且仍未达期望）的逐条说明，用于交付报告与套件输出。"""
    lines: list[str] = []
    for outcome in outcomes:
        case = outcome.case
        if not case.xfail_reason or outcome.ok:
            continue
        actual = "hit" if outcome.hit else "miss"
        expected = "hit" if case.expect_hit else "miss"
        lines.append(
            f"{case.case_id} [{case.rule_id}] expect={expected} actual={actual}：{case.xfail_reason}"
        )
    return lines


def verify(root: Path | str | None = None) -> tuple[int, int, list[str]]:
    """跑全部用例，返回 (通过数, 失败数, 失败明细)。

    ``xfail`` 用例"仍然不达期望"计入通过——它是被稳定复现的已知缺陷，不该让套件
    常红（常红等于没人看）。真正的回归才算失败；xfail 用例若意外变绿（XPASS）同样
    记为失败，提示把标记摘掉，否则缺陷修好了也没人知道。
    """
    outcomes, errors = run_cases(root)
    return _summarize(outcomes, errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sast_fixture_corpus",
        description=(
            "SAST 规则回归语料运行器："
            "用 verification/sast-fixtures 下的语料校验内置规则行为"
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        help="仓库根目录（含 verification/sast-fixtures），默认取模块所在仓库",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--verbose", action="store_true", help="逐条打印用例结果")
    args = parser.parse_args(argv)

    outcomes, errors = run_cases(args.root)
    passed, failed, details = _summarize(outcomes, errors)

    xfail_total = sum(1 for o in outcomes if o.is_xfail)
    hits = sum(1 for o in outcomes if o.case.expect_hit)
    misses = len(outcomes) - hits
    defects = known_defects(outcomes)

    if args.json:
        import json

        print(
            json.dumps(
                {
                    "total": len(outcomes),
                    "expected_hit": hits,
                    "expected_miss": misses,
                    "xfail": xfail_total,
                    "passed": passed,
                    "failed": failed,
                    "details": details,
                    "known_defects": defects,
                    "cases": [
                        {
                            "case": o.case.case_id,
                            "rule_id": o.case.rule_id,
                            "language": o.case.language,
                            "expect": "hit" if o.case.expect_hit else "miss",
                            "actual": "hit" if o.hit else "miss",
                            "xfail": bool(o.case.xfail_reason),
                            "ok": o.ok and not o.xpass,
                        }
                        for o in outcomes
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        if args.verbose:
            for o in outcomes:
                if o.xpass:
                    mark = "XPASS"
                elif o.is_xfail:
                    mark = "XFAIL"
                elif o.ok:
                    mark = "PASS"
                else:
                    mark = "FAIL"
                print(
                    f"{mark:5} {o.case.case_id:38} {o.case.rule_id:14} "
                    f"{'hit' if o.hit else 'miss':4} (expect {'hit' if o.case.expect_hit else 'miss'})"
                )
        print(
            f"SAST 规则回归语料: 共 {len(outcomes)} 例 "
            f"(应命中 {hits} / 应不命中 {misses})，xfail {xfail_total} 例"
        )
        print(f"通过 {passed}，失败 {failed}")
        for line in details:
            print(f"  - {line}")
        if defects:
            print("已知缺陷（xfail，待修；修好后套件会报 XPASS）：")
            for line in defects:
                print(f"  - {line}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
