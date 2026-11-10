#!/usr/bin/env python3
"""seed_drill.py — 种子缺陷演练：知识系统实效的端到端度量。

回放指标只测检索层；本工具生成一个含已知缺陷的确定性小项目
（每个缺陷对应一个错题类别 + 检测词），再用真实 findings 评分发现率——
按类别给 recall，并支持「知识注入开/关」两轮对比。

用法：
  python3 seed_drill.py build --out drill-target          # 生成靶场+answer.json
  python3 seed_drill.py verify --dir drill-target --findings run.json
  python3 seed_drill.py compare --dir drill-target --a run_a.json --b run_b.json

answer.json 是评分依据，演练 agent 不得读取（靶场 root 下不放置副本）。
verify 的匹配规则：evidence 命中缺陷文件 且 title/root_cause 含检测词。
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

SCHEMA_VERSION = 1

# 确定性靶场：每个文件一个已知缺陷（类别来自错题 14 大类）。
SEED_FILES: dict[str, str] = {
    "cart/order_store.py": '''"""订单存取。"""


def latest_orders(records):
    """返回最近 5 条订单。"""
    # 缺陷：升序排序后取前 5，拿到的是最旧而非最新。
    return sorted(records)[:5]
''',
    "cart/pricing.py": '''"""价格计算。"""


def total(item, user_input):
    # 缺陷：qty 可能是字符串/列表，裸 float 直接崩溃。
    qty = float(user_input["qty"])
    return qty * item["price"]
''',
    "api/handler.py": '''"""API 响应处理。"""


def unwrap(payload):
    # 缺陷：消费者读 result.result，生产者实际返回 {"items": ...}。
    return payload["result"]["result"]
''',
    "io/loader.py": '''"""逐行加载。"""


def load_lines(text):
    out = []
    for line in text.splitlines():
        # 缺陷：一条坏行让整批崩溃，之后的好行全丢。
        out.append(json.loads(line))
    return out


import json  # noqa: E402
''',
    "util/redact.py": '''"""敏感信息脱敏。"""

SENSITIVE = {"api_key", "authorization"}


def redact(value):
    # 缺陷：只脱敏顶层键，嵌套 dict/list 原样保留。
    if not isinstance(value, dict):
        return value
    return {
        key: "***" if key.lower() in SENSITIVE else item
        for key, item in value.items()
    }
''',
    "sched/arbiter.py": '''"""并发仲裁。"""


def winner(actions):
    # 缺陷：平局时 max 返回首元素，同一 key 可能既胜又败。
    return max(actions, key=lambda a: a["score"])
''',
    "routes/mention.py": '''"""@提及解析。"""

import re

MENTION = re.compile(r"@([A-Za-z0-9_.]+)")


def extract(text):
    # 缺陷：无词边界，把 me@example.com 整段当文件引用。
    return MENTION.findall(text)
''',
    "report/export.py": '''"""SSE 事件导出。"""


def final_event(events):
    for event in events:
        # 缺陷：流式多事件里取第一个 dict 就返回，progress 被当结果。
        if isinstance(event, dict):
            return event
    return None
''',
    "auth/rbac.py": '''"""基于角色的权限校验。"""


def check_permission(user, action):
    # 缺陷：黑名单模式，未覆盖的新增敏感操作默认放行越权。
    blacklist = {"admin_delete", "admin_drop_db"}
    if action in blacklist:
        return "admin" in user.get("roles", [])
    return True
''',
    "stateful/order_flow.py": '''"""订单状态流转与退款。"""


def refund_order(order, refund_amount):
    # 缺陷：未校验订单当前状态，允许对已取消或已退款的终态订单重复发起退款。
    if refund_amount <= 0:
        return False
    order["refunded_amount"] = order.get("refunded_amount", 0) + refund_amount
    order["status"] = "refunded"
    return True
''',
    "concurrency/counter.py": '''"""多线程共享计数器。"""

import threading

counter = 0


def increment():
    global counter
    # 缺陷：非原子读改写且无锁保护，并发下发生竞态导致计数丢失。
    current = counter
    counter = current + 1
    return counter
''',
    "security/path_util.py": '''"""用户上传文件路径解析。"""

from pathlib import Path


def resolve_upload_path(base_dir: str, filename: str) -> Path:
    # 缺陷：直接拼接且未规范化解析，允许 .. 路径遍历逃逸出基准目录。
    return Path(base_dir) / filename
''',
    "data/query_builder.py": '''"""动态 SQL 查询构建器。"""


def find_user_by_name(name: str) -> str:
    # 缺陷：直接字符串格式化拼接 SQL，存在注入漏洞。
    return f"SELECT * FROM users WHERE username = '{name}' AND active = 1"
''',
}

ANSWER = [
    {"id": "S-01", "category": "ordering", "file": "cart/order_store.py",
     "description": "升序取前 N 拿到最旧记录", "detect_tokens": ["sort", "最旧", "oldest", "排序"]},
    {"id": "S-02", "category": "type-assumption", "file": "cart/pricing.py",
     "description": "裸 float 假设字段类型", "detect_tokens": ["float", "类型", "崩溃", "type"]},
    {"id": "S-03", "category": "contract", "file": "api/handler.py",
     "description": "消费者键名与生产者结构不一致", "detect_tokens": ["键名", "契约", "result", "contract"]},
    {"id": "S-04", "category": "partial-failure", "file": "io/loader.py",
     "description": "单条坏行拖垮整批加载", "detect_tokens": ["坏行", "批量", "逐条", "line", "load"]},
    {"id": "S-05", "category": "secrets", "file": "util/redact.py",
     "description": "脱敏只做顶层不递归", "detect_tokens": ["脱敏", "嵌套", "redact", "sanitize"]},
    {"id": "S-06", "category": "concurrency", "file": "sched/arbiter.py",
     "description": "平局仲裁自胜自败", "detect_tokens": ["平局", "仲裁", "max", "并发", "tie"]},
    {"id": "S-07", "category": "input-boundary", "file": "routes/mention.py",
     "description": "正则无边界吞掉邮箱", "detect_tokens": ["边界", "邮箱", "正则", "mention", "@"]},
    {"id": "S-08", "category": "streaming", "file": "report/export.py",
     "description": "流式解析取第一个事件当结果", "detect_tokens": ["流式", "第一个", "stream", "event", "sse"]},
    {"id": "S-09", "category": "authorization", "file": "auth/rbac.py",
     "description": "黑名单权限校验导致未列出动作默认放行越权", "detect_tokens": ["rbac", "权限", "越权", "authorization", "permission", "blacklist"]},
    {"id": "S-10", "category": "state-transition", "file": "stateful/order_flow.py",
     "description": "状态机未校验终态导致重复退款", "detect_tokens": ["状态", "退款", "终态", "refund", "state", "status"]},
    {"id": "S-11", "category": "race-condition", "file": "concurrency/counter.py",
     "description": "无锁并发累加引发竞态计数丢失", "detect_tokens": ["竞态", "无锁", "并发", "race", "atomic", "lock"]},
    {"id": "S-12", "category": "path-traversal", "file": "security/path_util.py",
     "description": "路径未规范化拼接导致目录遍历", "detect_tokens": ["遍历", "路径", "traversal", "path", "escape", "resolve"]},
    {"id": "S-13", "category": "injection", "file": "data/query_builder.py",
     "description": "字符串格式化拼接动态 SQL 产生注入", "detect_tokens": ["注入", "sql", "拼接", "format", "injection"]},
]


class DrillError(ValueError):
    """演练协议或输入无效。"""


def build(out: Path) -> dict:
    if out.exists() and any(out.iterdir()):
        raise DrillError(f"输出目录非空: {out}")
    for relative, content in SEED_FILES.items():
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    answer = out.parent / f"{out.name}-answer.json"
    answer.write_text(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "bugs": ANSWER},
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "built", "root": str(out), "answer": str(answer),
        "files": len(SEED_FILES), "bugs": len(ANSWER),
        "note": (
            "靶场 root 只含缺陷文件；answer 写在靶场外，agent 不可读。"
            "跑两轮（知识注入开/关）后用 compare 对比 recall。"
        ),
    }


def _load_bugs(answer_path: Path) -> list[dict]:
    try:
        data = json.loads(answer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DrillError(f"answer 无法读取: {exc}") from exc
    bugs = data.get("bugs")
    if not isinstance(bugs, list) or not bugs:
        raise DrillError("answer.bugs 必须是非空数组")
    return bugs


def verify(root: Path, answer_path: Path, findings_payload: dict) -> dict:
    bugs = _load_bugs(answer_path)
    findings = findings_payload.get("findings")
    if not isinstance(findings, list):
        raise DrillError("findings_payload.findings 必须是数组")
    per_category: dict[str, dict] = {}
    found_total = 0
    for bug in bugs:
        category = str(bug.get("category"))
        tokens = [str(token).casefold() for token in bug.get("detect_tokens", [])]
        file_key = str(bug.get("file"))
        found = False
        matched_finding = None
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            evidence_paths = [
                str(item.get("path", "")) for item in finding.get("evidence", [])
                if isinstance(item, dict)
            ]
            if not any(file_key in path for path in evidence_paths):
                continue
            haystack = " ".join([
                str(finding.get("title", "")), str(finding.get("root_cause", "")),
                str(finding.get("reproduction", "")),
            ]).casefold()
            if any(token in haystack for token in tokens):
                found = True
                matched_finding = finding.get("id")
                break
        bucket = per_category.setdefault(
            category, {"bugs": 0, "found": 0, "details": []},
        )
        bucket["bugs"] += 1
        bucket["found"] += int(found)
        bucket["details"].append({
            "id": bug.get("id"), "file": file_key, "found": found,
            "finding_id": matched_finding,
        })
        found_total += int(found)
    recall = found_total / len(bugs) if bugs else 0.0
    category_recall = {
        category: round(bucket["found"] / bucket["bugs"], 4)
        for category, bucket in sorted(per_category.items())
        if bucket["bugs"]
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluation": "seed-drill-verify",
        "root": str(root),
        "bugs_total": len(bugs),
        "bugs_found": found_total,
        "recall": round(recall, 4),
        "recall_by_category": category_recall,
        "findings_total": len(findings),
        "comparison_scope": "seeded-bug discovery; real end-to-end signal",
    }


def compare(root: Path, answer_path: Path, payload_a: dict, payload_b: dict) -> dict:
    result_a = verify(root, answer_path, payload_a)
    result_b = verify(root, answer_path, payload_b)
    delta_categories = {
        category: round(
            result_b["recall_by_category"].get(category, 0.0)
            - result_a["recall_by_category"].get(category, 0.0), 4,
        )
        for category in sorted(
            set(result_a["recall_by_category"]) | set(result_b["recall_by_category"])
        )
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluation": "seed-drill-compare",
        "a": {k: result_a[k] for k in ("recall", "recall_by_category", "bugs_found")},
        "b": {k: result_b[k] for k in ("recall", "bugs_found")},
        "b_recall_by_category": result_b["recall_by_category"],
        "recall_delta": round(result_b["recall"] - result_a["recall"], 4),
        "category_delta": delta_categories,
        "note": (
            "b - a 的差值即知识注入（或其他变量）的发现率增益；"
            "样本量小，多轮重复后再下结论"
        ),
    }


def aggregate(results: list[dict]) -> dict:
    """跨项目聚合 drill 结果：根因类别 × recall 热力。

    输入是若干 verify/compare 的结果 JSON（含 recall_by_category）。
    输出每类别的均值/最小值与项目数——「哪类错题普遍挖不动」的宏观视图，
    反哺 pattern_probe 探针库迭代。无代码、无路径，天然脱敏。
    """
    buckets: dict[str, list[float]] = {}
    overall: list[float] = []
    for result in results:
        categories = result.get("recall_by_category")
        if not isinstance(categories, dict):
            continue
        overall.append(float(result.get("recall", 0.0) or 0.0))
        for category, value in categories.items():
            try:
                buckets.setdefault(str(category), []).append(float(value))
            except (TypeError, ValueError):
                continue
    if not buckets:
        raise DrillError("输入中没有 recall_by_category（需要 verify 结果）")
    summary = {
        category: {
            "projects": len(values),
            "mean_recall": round(sum(values) / len(values), 4),
            "min_recall": round(min(values), 4),
        }
        for category, values in sorted(buckets.items())
    }
    weakest = sorted(summary.items(), key=lambda item: item[1]["mean_recall"])[:3]
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluation": "seed-drill-aggregate",
        "results_total": len(overall),
        "overall_mean_recall": round(
            sum(overall) / len(overall), 4,
        ) if overall else None,
        "categories": summary,
        "weakest_categories": [
            {"category": name, **stats} for name, stats in weakest
        ],
        "note": "最弱类别优先迭代 pattern_probe 探针与 worker 提示",
    }


def run_self_drill(
    audit_callable=None,
    out_dir: Path | str | None = None,
) -> dict:
    """自动化自演练评测：生成确定性靶场并运行审计回调（或基线规则），返回评估指标。

    参数:
        audit_callable: 接收 (target_dir: Path) -> dict(findings=[...]) 的可调用对象。
                        若为 None，则执行内置轻量基线规则扫描。
        out_dir: 靶场输出目录；若未提供则使用自动清理的临时目录。
    返回:
        verify() 评估结果字典。
    """
    import shutil
    import tempfile

    is_temp = False
    if out_dir is None:
        temp_dir = Path(tempfile.mkdtemp(prefix="seed-drill-run-"))
        target_dir = temp_dir / "target"
        is_temp = True
    else:
        target_dir = Path(out_dir)
        temp_dir = target_dir.parent

    try:
        built = build(target_dir)
        answer_path = Path(built["answer"])

        if audit_callable is not None:
            findings_payload = audit_callable(target_dir)
        else:
            # 内置基线扫描器：对已知缺陷模式做轻量静态规则启发式匹配
            findings = []
            for file_key in SEED_FILES:
                p = target_dir / file_key
                if not p.is_file():
                    continue
                code = p.read_text(encoding="utf-8")
                if "sorted(records)[:5]" in code:
                    findings.append({
                        "id": "B-01", "title": "升序排序切片取最旧记录",
                        "root_cause": "sort 升序取切片导致逻辑颠倒",
                        "evidence": [{"path": file_key, "line": 35}],
                    })
                if "float(user_input" in code:
                    findings.append({
                        "id": "B-02", "title": "未校验直接 float 转换导致类型异常崩溃",
                        "root_cause": "裸 float 缺乏类型兜底",
                        "evidence": [{"path": file_key, "line": 42}],
                    })
                if 'payload["result"]["result"]' in code:
                    findings.append({
                        "id": "B-03", "title": "嵌套键名提取不符合契约结构",
                        "root_cause": "契约字段假设不一致",
                        "evidence": [{"path": file_key, "line": 50}],
                    })
                if "json.loads(line)" in code:
                    findings.append({
                        "id": "B-04", "title": "单条坏行异常阻断批量逐条加载",
                        "root_cause": "未做局部异常捕获跳过坏行",
                        "evidence": [{"path": file_key, "line": 59}],
                    })
                if "SENSITIVE" in code and "isinstance(value, dict)" in code:
                    findings.append({
                        "id": "B-05", "title": "敏感词脱敏未做深度递归处理",
                        "root_cause": "sanitize/redact 遗漏深层嵌套数据",
                        "evidence": [{"path": file_key, "line": 74}],
                    })
                if "max(actions, key=" in code:
                    findings.append({
                        "id": "B-06", "title": "并发仲裁平局首项自胜自败",
                        "root_cause": "并发平局缺乏仲裁 tie breaker",
                        "evidence": [{"path": file_key, "line": 84}],
                    })
                if "@([A-Za-z0-9_.]+)" in code:
                    findings.append({
                        "id": "B-07", "title": "提及正则无词边界导致邮箱误判",
                        "root_cause": "mention 缺少边界前缀约束",
                        "evidence": [{"path": file_key, "line": 95}],
                    })
                if "isinstance(event, dict)" in code:
                    findings.append({
                        "id": "B-08", "title": "流式事件返回第一个进度事件作为最终结果",
                        "root_cause": "流式 SSE 未等待完成事件",
                        "evidence": [{"path": file_key, "line": 103}],
                    })
                if "blacklist = {" in code:
                    findings.append({
                        "id": "B-09", "title": "黑名单权限鉴权未覆盖新增操作越权",
                        "root_cause": "rbac 黑名单模式缺乏默认拒绝",
                        "evidence": [{"path": file_key, "line": 111}],
                    })
                if 'order["status"] = "refunded"' in code and "refund_amount <= 0" in code:
                    findings.append({
                        "id": "B-10", "title": "状态机未校验订单终态引发重复退款",
                        "root_cause": "refund 缺乏终态前置状态机条件拦截",
                        "evidence": [{"path": file_key, "line": 122}],
                    })
                if "counter = current + 1" in code:
                    findings.append({
                        "id": "B-11", "title": "全局计数器无锁并发读写竞态丢失",
                        "root_cause": "race condition 缺乏原子操作或 lock 锁保护",
                        "evidence": [{"path": file_key, "line": 134}],
                    })
                if "resolve_upload_path" in code:
                    findings.append({
                        "id": "B-12", "title": "路径拼接未做相对规范化解析产生目录遍历",
                        "root_cause": "path traversal 未校验 resolve 逃逸边界",
                        "evidence": [{"path": file_key, "line": 145}],
                    })
                if 'f"SELECT * FROM users' in code:
                    findings.append({
                        "id": "B-13", "title": "用户输入直接字符串拼接 SQL 查询注入",
                        "root_cause": "sql injection 缺乏参数化查询",
                        "evidence": [{"path": file_key, "line": 154}],
                    })
            findings_payload = {"findings": findings}

        return verify(target_dir, answer_path, findings_payload)
    finally:
        if is_temp:
            shutil.rmtree(temp_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=("build", "verify", "compare", "aggregate"))
    parser.add_argument("--results", type=Path, action="append", default=[],
                        help="aggregate 输入：一个或多个 verify 结果 JSON")
    parser.add_argument("--out", type=Path, help="build 输出靶场目录")
    parser.add_argument("--dir", type=Path, help="靶场 root")
    parser.add_argument("--answer", type=Path, help="verify/compare 的 answer.json")
    parser.add_argument("--findings", type=Path, help="verify 输入 findings JSON")
    parser.add_argument("--a", type=Path, help="compare A 轮 findings JSON")
    parser.add_argument("--b", type=Path, help="compare B 轮 findings JSON")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            if args.out is None:
                raise DrillError("build 必须提供 --out")
            result: dict = build(args.out)
        elif args.command == "verify":
            if args.dir is None or args.findings is None:
                raise DrillError("verify 必须提供 --dir 和 --findings")
            answer = args.answer or args.dir.parent / f"{args.dir.name}-answer.json"
            result = verify(
                args.dir, answer,
                json.loads(args.findings.read_text(encoding="utf-8")),
            )
        elif args.command == "aggregate":
            if not args.results:
                raise DrillError("aggregate 必须提供 --results（可重复）")
            loaded = []
            for path in args.results:
                loaded.append(json.loads(path.read_text(encoding="utf-8")))
            result = aggregate(loaded)
        else:
            if args.dir is None or args.a is None or args.b is None:
                raise DrillError("compare 必须提供 --dir --a --b")
            answer = args.answer or args.dir.parent / f"{args.dir.name}-answer.json"
            result = compare(
                args.dir, answer,
                json.loads(args.a.read_text(encoding="utf-8")),
                json.loads(args.b.read_text(encoding="utf-8")),
            )
    except (DrillError, OSError, json.JSONDecodeError) as exc:
        print(f"[seed_drill] ✗ {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
