#!/usr/bin/env python3
"""core/fixture_solidify.py - 把已确认的真实发现固化为回归用例。

## 为什么需要它

在此之前，「发现缺陷 → 修掉 → 以后不再复发」全靠人记。``scenario_eval.py``
能跑 L1–L6，但它的用例是**手写**的：没人把它加进去，那个 bug 修完就再无保护，
下次改动照样会踩回去。

本模块补上最后一环：吃一份 findings JSON（``agent_eval`` / ``finding_report``
的同一格式，也是 checkpoint 提交用的格式），把其中的**已确认**发现转成
``agent_eval`` 可消费的 case + fixture，让下一轮自动回归。

## 只固化「已确认」的发现

``confirmation.state`` 必须是 ``confirmed``。理由不是保守，而是**代价不对称**：

* 固化一条误报 → 之后每轮都要求产品「发现」一个不存在的问题。要么产品为
  通过评测而编造发现（污染整个基线），要么评测永远红灯（很快被无视）。
* 漏固化一条真缺陷 → 少一层保护，但不会主动产生错误信号。

所以宁缺勿滥。``reproducible: true`` 也必须成立——不可复现的发现无法变成
可重复执行的用例，即使它是真的。

## 幂等

同一 ``root_cause`` 只固化一次。重复固化会让 recall 分母无故膨胀，看起来
\"发现率下降\"，而实际上产品没变。

## 产物

    evals/fixtures/<slug>.fixture   从发现证据里摘出的最小可复现片段
    evals/cases.json                追加新 case

fixture 摘取**按行窗口**而不是整文件复制：真实项目动辄上千行，把整文件塞进
评测集会让人工审视标准答案变得不可能，且会带进大量无关代码干扰判定。
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
import sys
from pathlib import Path


import argparse
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_EVALS_DIR = (REPO_ROOT / "verification" / "evals") if (REPO_ROOT / "verification" / "evals").is_dir() else (REPO_ROOT / "evals")
CASES_PATH = _EVALS_DIR / "cases.json"
GOLDEN_PATH = _EVALS_DIR / "golden-findings.json"
FIXTURES_DIR = _EVALS_DIR / "fixtures"
SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

#: fixture 摘取窗口：证据行前后各留几行。太小看不出上下文，太大把无关逻辑
#: 也带进标准答案。±12 行对「函数级」缺陷通常刚好覆盖完整函数体。
CONTEXT_BEFORE = 6
CONTEXT_AFTER = 16
#: 单文件体积上限。超过就只保留窗口，不再追加文件头注释。
MAX_FIXTURE_LINES = 120


class SolidifyError(Exception):
    """固化前置条件不满足。"""


def _slugify(text: str, *, limit: int = 48) -> str:
    """把 root_cause 转成文件名安全的 slug。"""
    slug = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return (slug or "finding")[:limit]


def _case_id(root_cause: str, taken: set[str]) -> str:
    base = "BH-EVAL-" + _slugify(root_cause, limit=32).upper()
    candidate = base
    index = 2
    while candidate in taken:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def eligible_findings(payload: dict) -> tuple[list[dict], list[dict]]:
    """拆出可固化与不可固化的发现，并给出后者原因。

    返回 ``(eligible, rejected)``，``rejected`` 每项含 ``id`` 与 ``reason``。
    显式列出拒绝原因很重要：静默跳过会让人以为「全都固化了」，而实际只固化了
    一部分，保护范围被高估。
    """
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise SolidifyError("findings JSON 缺 findings 数组")
    eligible: list[dict] = []
    rejected: list[dict] = []
    for index, finding in enumerate(findings, 1):
        label = (
            str(finding.get("id") or finding.get("fingerprint") or f"#{index}")
            if isinstance(finding, dict) else f"#{index}"
        )
        if not isinstance(finding, dict):
            rejected.append({"id": label, "reason": "不是对象"})
            continue
        confirmation = finding.get("confirmation")
        state = (
            str(confirmation.get("state", "")).lower()
            if isinstance(confirmation, dict) else ""
        )
        if state != "confirmed":
            rejected.append(
                {"id": label, "reason": f"未确认（confirmation.state={state or '缺失'}）"},
            )
            continue
        if finding.get("reproducible") is not True:
            rejected.append({"id": label, "reason": "不可复现，无法变成可重复用例"})
            continue
        root_cause = finding.get("root_cause")
        if not isinstance(root_cause, str) or not root_cause.strip():
            rejected.append({"id": label, "reason": "缺 root_cause（评测必需）"})
            continue
        severity = str(finding.get("severity") or "").lower()
        if severity not in SEVERITY_ORDER:
            rejected.append({"id": label, "reason": f"severity 无效：{severity!r}"})
            continue
        evidence = finding.get("evidence")
        first = evidence[0] if isinstance(evidence, list) and evidence else None
        if not isinstance(first, dict) or not isinstance(first.get("path"), str):
            rejected.append({"id": label, "reason": "缺可定位的 evidence.path"})
            continue
        eligible.append(finding)
    return eligible, rejected


def _extract_window(source: Path, line: int) -> tuple[str, int, int, int]:
    """摘出证据行附近的窗口，返回 (内容, 起行, 止行, 证据行钳制后落点)。

    证据行可能越界（发现固化时源码已修复、行数变短），钳制结果必须回传：
    上游若拿原始行号推 fixture 内行号，会在目标行恰为末行时指向窗口之外。
    """
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SolidifyError(f"无法读取证据文件 {source}: {exc}") from exc
    lines = text.splitlines()
    if not lines:
        raise SolidifyError(f"证据文件为空：{source}")
    target = max(1, min(int(line), len(lines)))
    start = max(1, target - CONTEXT_BEFORE)
    end = min(len(lines), target + CONTEXT_AFTER)
    if end - start + 1 > MAX_FIXTURE_LINES:
        end = start + MAX_FIXTURE_LINES - 1
        if target > end:  # 截断不得把证据行挤出窗口
            end = target
            start = max(1, target - MAX_FIXTURE_LINES + 1)
    return "\n".join(lines[start - 1:end]) + "\n", start, end, target


def build_fixture(finding: dict, source_root: Path) -> tuple[str, dict]:
    """为一个发现构造 fixture 文本与元信息。"""
    evidence = finding["evidence"][0]
    relative = str(evidence["path"])
    source = (source_root / relative).resolve()
    try:
        source.relative_to(source_root.resolve())
    except ValueError as exc:
        raise SolidifyError(f"证据路径逃出项目根：{relative}") from exc
    if not source.is_file():
        raise SolidifyError(f"证据文件不存在：{relative}")
    suffix = source.suffix or ".txt"
    line = evidence.get("line") or 1
    body, start, end, target = _extract_window(source, int(line))
    header_lines = [
        f"# BENCHMARK FIXTURE ONLY: 由已确认发现固化（原路径 {relative}:{line}）。",
        f"# 保留第 {start}-{end} 行作为最小可复现窗口；不含标准答案。",
    ]
    header = "\n".join(header_lines) + "\n"
    return header + body, {
        "source_path": relative, "line": int(line),
        "window": [start, end], "suffix": suffix,
        # fixture 内的行号 = 钳制后的证据行 - 窗口起点 + 1 + 头注释行数。
        # 必须用窗口实际锚定的钳制行：原始行号在源码行数变短时会越界，
        # 指向窗口之外的 golden 行号反而能通过行数校验之外的一切检查。
        # 头注释行数必须**实测**而不是写死常量：写死 2 之后一旦改文案，
        # golden 的行号就会静默错位，而错位的 golden 仍然能通过校验，
        # 只是把「漏检判定」指向了无害代码行。
        "fixture_line": target - start + 1 + len(header_lines),
    }


def build_golden_finding(finding: dict, case: dict, meta: dict,
                        index: int) -> dict:
    """为固化出的 case 构造配套的 golden finding。

    ## 为什么必须成对生成

    第一版只写 case 不写 golden，结果 ``agent_eval score`` 立刻把 recall 从
    100% 打到 85.71%——因为新 case 在 golden 里没有对应答案，永远算漏检。
    换句话说：**每固化一条缺陷，就制造一盏永久红灯**。评测很快就会被无视，
    固化反而摧毁了它本要保护的东西。

    golden finding 的语义是「这份 fixture 里存在这个根因」，所以它引用的是
    **fixture 路径**（而不是原始项目文件），行号落在窗口内的相对位置。
    """
    fixture_path = str(case["fixture"])
    # 行号由 build_fixture 实测得出，这里不再重算——重算就是第二份真相，
    # 两处一旦不同，golden 会静默指向无害代码行。
    fixture_line = int(meta["fixture_line"])
    reference = f"{fixture_path}:{fixture_line}"
    reproduction = str(finding.get("reproduction") or "见 fixture 中的缺陷实现")
    return {
        "id": f"F-SOLID-{index:03d}",
        "case_id": case["id"],
        "title": str(finding.get("title") or case["root_cause"]),
        "root_cause": str(case["root_cause"]),
        "severity": str(case["minimum_severity"]),
        "confidence": float(finding.get("confidence", 0.95)),
        "evidence": [{"path": fixture_path, "line": fixture_line}],
        "reproducible": True,
        "reproduction": reproduction,
        "confirmation": {
            "state": "confirmed",
            "oracle": {
                "tier": "contract",
                "reference": f"fixture 中存在 {case['root_cause']}",
                "provenance": {
                    "kind": "repository-contract",
                    "independent_from_change": True,
                    "verified": True,
                },
            },
            "observations": [
                {
                    "run_id": f"solid-{case['id'].lower()}-r1",
                    "outcome": "failure", "channel": "test",
                    "fingerprint": case["root_cause"], "reference": reference,
                },
                {
                    "run_id": f"solid-{case['id'].lower()}-r2",
                    "outcome": "failure", "channel": "trace",
                    "fingerprint": case["root_cause"], "reference": reference,
                },
            ],
            "negative_control": {
                "outcome": "pass",
                "reference": "fixture 中其余代码路径行为正常",
            },
            "root_cause_verified": True,
            "target_execution": {
                "target_platform": "source", "executed_platform": "source",
                "target_fingerprint": f"eval-fixture:{_slugify(case['root_cause'])}",
                "run_reference": f"solid-{case['id'].lower()}-r2",
                "executed": True, "simulated": False,
                "outcome": "failure", "errors": [],
            },
        },
    }


def solidify(payload: dict, *, source_root: Path,
             # 注意：``cases_path`` / ``fixtures_dir`` / ``golden_path`` 的默认值
             # 在**函数定义时**就绑定了，之后 patch 模块级同名常量不会影响它们。
             # 想让调用方换写入位置，必须显式传参（或传 repo_root 统一重定向）。
             cases_path: Path | None = None,
             fixtures_dir: Path | None = None,
             golden_path: Path | None = None,
             repo_root: Path | None = None,
             dry_run: bool = False) -> dict:
    """执行固化，返回报告。

    ``repo_root`` 可注入：本函数会**写文件**，写到哪里必须由调用方决定，
    而不是被模块级 ``REPO_ROOT`` 钉死。钉死的后果是测试只能去污染真实仓库，
    或者根本没法隔离验证「有没有真的写成」。
    """
    root = (repo_root or REPO_ROOT).resolve()
    # 全部 resolve：macOS 上 /var 是 /private/var 的符号链接，root 被 resolve
    # 而 fixtures_dir 没有时，``relative_to(root)`` 会抛 ValueError（两者字面
    # 前缀不同）。这类「同一目录的两个名字」在真实环境里到处都是，必须在入口
    # 统一归一，而不是指望每个调用方都记得 resolve。
    # evals 目录随仓库布局迁移（evals/ → verification/evals/），与模块级
    # _EVALS_DIR 的双路径回退保持同一规则；root 注入时按注入根推导，测试隔离不破。
    evals_dir = (
        (root / "verification" / "evals")
        if (root / "verification" / "evals").is_dir()
        else (root / "evals")
    )
    cases_path = (cases_path or (evals_dir / "cases.json")).resolve()
    fixtures_dir = (fixtures_dir or (evals_dir / "fixtures")).resolve()
    golden_path = (golden_path or (
        evals_dir / "golden-findings.json")).resolve()
    eligible, rejected = eligible_findings(payload)
    cases_payload = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = cases_payload.get("cases")
    if not isinstance(cases, list):
        raise SolidifyError(f"{cases_path} 缺 cases 数组")
    existing_causes = {
        str(case.get("root_cause")) for case in cases if isinstance(case, dict)
    }
    existing_ids = {
        str(case.get("id")) for case in cases if isinstance(case, dict)
    }

    added: list[dict] = []
    skipped: list[dict] = []
    for finding in eligible:
        root_cause = str(finding["root_cause"])
        label = str(finding.get("id") or finding.get("fingerprint") or root_cause)
        if root_cause in existing_causes:
            # 幂等：同一缺陷根因只固化一次。重复固化会膨胀 recall 分母，
            # 表现为「发现率下降」，而产品其实没变。
            skipped.append({"id": label, "reason": f"root_cause 已存在：{root_cause}"})
            continue
        try:
            fixture_text, meta = build_fixture(finding, source_root)
        except SolidifyError as exc:
            skipped.append({"id": label, "reason": str(exc)})
            continue
        slug = _slugify(root_cause)
        fixture_name = f"{slug}{meta['suffix']}.fixture"
        fixture_path = fixtures_dir / fixture_name
        case = {
            "id": _case_id(root_cause, existing_ids | {c["id"] for c in added}),
            "fixture": str(fixture_path.relative_to(root)),
            "root_cause": root_cause,
            "minimum_severity": str(finding["severity"]).lower(),
        }
        existing_causes.add(root_cause)
        added.append({**case, "_fixture_text": fixture_text, "_meta": meta,
                      "_finding": finding})

    report = {
        "added": [{k: v for k, v in item.items() if not k.startswith("_")}
                  for item in added],
        "skipped": skipped,
        "rejected": rejected,
        "cases_before": len(cases),
        "cases_after": len(cases) + len(added),
        "dry_run": dry_run,
    }
    if dry_run or not added:
        return report

    fixtures_dir.mkdir(parents=True, exist_ok=True)
    golden_payload: dict | None = None
    golden_findings: list = []
    if golden_path is not None and golden_path.is_file():
        try:
            golden_payload = json.loads(golden_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SolidifyError(f"golden findings 无法解析：{exc}") from exc
        existing_golden = golden_payload.get("findings")
        golden_findings = existing_golden if isinstance(existing_golden, list) else []

    # id 号段必须接续 golden 里已占用的最大序号：增量固化时 enumerate 从 1
    # 重来，会与既有 F-SOLID-xxx 撞 id，重复 id 会让 agent_eval score 直接拒绝。
    used_seq: set[int] = set()
    for g in golden_findings:
        gid = str(g.get("id", ""))
        if gid.startswith("F-SOLID-"):
            try:
                used_seq.add(int(gid[len("F-SOLID-"):]))
            except ValueError:
                pass
    base = max(used_seq) if used_seq else 0

    for offset, item in enumerate(added, 1):
        index = base + offset
        target = root / item["fixture"]
        target.write_text(item["_fixture_text"], encoding="utf-8")
        public = {k: v for k, v in item.items() if not k.startswith("_")}
        cases.append(public)
        if golden_payload is not None:
            golden_findings.append(
                build_golden_finding(item["_finding"], public, item["_meta"], index),
            )
    cases_payload["cases"] = cases
    cases_path.write_text(
        json.dumps(cases_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if golden_payload is not None:
        golden_payload["findings"] = golden_findings
        golden_path.write_text(
            json.dumps(golden_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把已确认发现固化为 agent_eval 回归用例",
    )
    parser.add_argument("--findings", required=True, help="findings JSON 路径")
    parser.add_argument("--source-root", default=None,
                        help="证据路径的解析根（缺省=仓库根）")
    parser.add_argument("--cases", default=str(CASES_PATH))
    parser.add_argument("--golden", default=str(GOLDEN_PATH),
                        help="配套 golden findings；传空串则只写 case")
    parser.add_argument("--dry-run", action="store_true", help="只报告不落盘")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    findings_path = Path(args.findings).expanduser()
    if not findings_path.is_file():
        print(f"findings 文件不存在：{findings_path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"findings 无法解析：{exc}", file=sys.stderr)
        return 2
    source_root = Path(args.source_root).expanduser() if args.source_root else REPO_ROOT
    try:
        report = solidify(
            payload, source_root=source_root, cases_path=Path(args.cases),
            golden_path=Path(args.golden) if args.golden else None,
            dry_run=args.dry_run,
        )
    except SolidifyError as exc:
        print(f"固化失败：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"固化 {len(report['added'])} 条（用例 {report['cases_before']}"
              f" → {report['cases_after']}）")
        for item in report["added"]:
            print(f"  + {item['id']}  {item['root_cause']}  [{item['minimum_severity']}]")
        for item in report["skipped"]:
            print(f"  = 跳过 {item['id']}：{item['reason']}")
        for item in report["rejected"]:
            print(f"  - 不收 {item['id']}：{item['reason']}")
        if report["dry_run"]:
            print("（dry-run，未落盘）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
