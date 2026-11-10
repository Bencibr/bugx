"""``bugx interaction``：非交互宿主的启动门禁答案回灌入口。

BugX 在没有原生提问能力、也没有真实 TTY 时会把问题以
``BUGX_INTERACTION_REQUEST ...`` 结构化行抛出，并落到项目运行态
``<root>/.bugx/state/interaction/``。本命令组是这些问题的**唯一**回灌通道：
没有它，``bugx engine pre`` 的启动门禁只能靠提前写配置或环境变量满足。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..common import _ensure_core_importable


def register_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "interaction",
        help="查看/回答 BugX 抛出的结构化交互问题（非交互宿主的答案回灌通道）",
    )
    sub = parser.add_subparsers(dest="interaction_command")

    def _add_root(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--root", type=Path, default=None,
            help="项目根目录（默认取 BUGX_PROJECT_ROOT，其次当前目录）",
        )

    list_parser = sub.add_parser("list", help="列出尚未消费的交互问题")
    _add_root(list_parser)
    list_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    list_parser.add_argument(
        "--all", action="store_true", help="包含已消费/已回答的历史记录",
    )
    list_parser.set_defaults(handler=command_interaction)

    show_parser = sub.add_parser("show", help="查看单个交互问题的完整记录")
    show_parser.add_argument("question_id")
    _add_root(show_parser)
    show_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    show_parser.set_defaults(handler=command_interaction)

    answer_parser = sub.add_parser("answer", help="回答一个交互问题并写入答案信箱")
    answer_parser.add_argument("question_id")
    answer_parser.add_argument(
        "--value", required=True,
        help="选项取值（option value）或 1 起始的选项序号；文本/密钥题填原始文本",
    )
    _add_root(answer_parser)
    answer_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    answer_parser.set_defaults(handler=command_interaction)

    cancel_parser = sub.add_parser("cancel", help="取消一个交互问题（本次运行视为放弃）")
    cancel_parser.add_argument("question_id")
    _add_root(cancel_parser)
    cancel_parser.add_argument("--reason", default="", help="取消原因（写入审计记录）")
    cancel_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    cancel_parser.set_defaults(handler=command_interaction)

    return parser


def _load_store() -> Any:
    _ensure_core_importable()
    from core.runtime import interaction_store

    return interaction_store


def _question_summary(record: dict[str, Any]) -> dict[str, Any]:
    request = record.get("request") or {}
    answers = record.get("answers") or []
    pending = [answer for answer in answers if not answer.get("consumed_at")]
    cancel_marker = record.get("cancel") or {}
    return {
        "question_id": record.get("question_id"),
        "phase": record.get("phase"),
        # kind/title/question 都存在 ``record["request"]`` 里，顶层没有副本；
        # 直接读 ``record.get("kind")`` 会永远输出 null（实测踩到）。
        "kind": request.get("kind"),
        "title": request.get("title") or "",
        "question": request.get("question") or "",
        "multiple": bool(request.get("multiple")),
        "default": request.get("default"),
        "created_at": record.get("created_at"),
        "options": [
            {"value": option.get("value"), "label": option.get("label")}
            for option in request.get("options") or []
        ],
        "pending_answers": len(pending),
        "answered": bool(pending),
        "cancelled": bool(cancel_marker and not cancel_marker.get("consumed_at")),
        "consumed": bool(answers) and not pending,
    }


def _emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _print_pending(rows: list[dict[str, Any]], root: Path) -> None:
    if not rows:
        print(f"[bugx interaction] {root} 下没有待决策的交互问题。")
        return
    print(f"[bugx interaction] {root} 下有 {len(rows)} 个待决策问题：")
    for row in rows:
        options = ", ".join(
            f"{option['value']}={option['label']}" for option in row["options"]
        )
        print(f"  - {row['question_id']}（phase={row['phase']}）{row['question']}")
        if options:
            print(f"      可选项：{options}")
        print(
            "      回答：bugx interaction answer "
            f"{row['question_id']} --value <值>"
        )


def _command_list(args: argparse.Namespace, store: Any, root: Path) -> int:
    rows = [_question_summary(record) for record in store.list_records(root)]
    if not getattr(args, "all", False):
        rows = [row for row in rows if row["answered"] or row["cancelled"]]
    if getattr(args, "json", False):
        _emit_json({"status": "ok", "root": str(root), "count": len(rows), "questions": rows})
        return 0
    _print_pending(rows, root)
    return 0


def _command_show(args: argparse.Namespace, store: Any, root: Path) -> int:
    record = store.read_record(args.question_id, root)
    if record is None:
        if getattr(args, "json", False):
            _emit_json({
                "status": "error", "root": str(root),
                "error": f"未知的交互问题 id：{args.question_id}",
            })
        else:
            print(f"[bugx interaction] ✗ 未知的交互问题 id：{args.question_id}")
        return 2
    if getattr(args, "json", False):
        _emit_json({"status": "ok", "root": str(root), "question": _question_summary(record),
                    "record": record})
        return 0
    summary = _question_summary(record)
    print(f"问题 id：{summary['question_id']}（phase={summary['phase']}, kind={summary['kind']}）")
    print(f"问题：{summary['question']}")
    for option in summary["options"]:
        print(f"  - {option['value']}：{option['label']}")
    print(f"创建时间：{summary['created_at']}")
    print(f"待消费答案：{summary['pending_answers']}；已取消：{summary['cancelled']}")
    return 0


def _command_answer(args: argparse.Namespace, store: Any, root: Path) -> int:
    try:
        record = store.submit_answer(args.question_id, args.value, root=root)
    except store.UnknownQuestion as exc:
        if getattr(args, "json", False):
            _emit_json({"status": "error", "root": str(root), "error": str(exc)})
        else:
            print(f"[bugx interaction] ✗ {exc}")
            print("[bugx interaction] 提示：先用 bugx interaction list 查看待决策问题 id。")
        return 2
    except store.AnswerRejected as exc:
        if getattr(args, "json", False):
            _emit_json({
                "status": "rejected", "root": str(root),
                "question_id": args.question_id, "value": args.value, "error": str(exc),
            })
        else:
            print(f"[bugx interaction] ✗ 取值被拒绝：{exc}")
        return 1
    except store.StoreUnavailable as exc:
        if getattr(args, "json", False):
            _emit_json({"status": "error", "root": str(root), "error": str(exc)})
        else:
            print(f"[bugx interaction] ✗ {exc}")
        return 1
    if getattr(args, "json", False):
        _emit_json({
            "status": "ok", "root": str(root), "question_id": args.question_id,
            "value": args.value, "answers": len(record.get("answers") or []),
        })
    else:
        print(
            f"[bugx interaction] ✓ 已记录 {args.question_id} = {args.value}；"
            "重新运行 bugx engine pre 即可消费该答案。"
        )
    return 0


def _command_cancel(args: argparse.Namespace, store: Any, root: Path) -> int:
    try:
        marker = store.cancel(args.question_id, root=root, reason=args.reason)
    except store.UnknownQuestion as exc:
        if getattr(args, "json", False):
            _emit_json({"status": "error", "root": str(root), "error": str(exc)})
        else:
            print(f"[bugx interaction] ✗ {exc}")
        return 2
    except store.StoreUnavailable as exc:
        if getattr(args, "json", False):
            _emit_json({"status": "error", "root": str(root), "error": str(exc)})
        else:
            print(f"[bugx interaction] ✗ {exc}")
        return 1
    if getattr(args, "json", False):
        _emit_json({
            "status": "ok", "root": str(root),
            "question_id": args.question_id, "cancel": marker,
        })
    else:
        print(f"[bugx interaction] ✓ 已取消 {args.question_id}；下一次 pre 将按放弃处理。")
    return 0


def command_interaction(args: argparse.Namespace) -> int:
    """bugx interaction：列出、查看、回答或取消 BugX 的结构化交互问题。"""
    subcommand = getattr(args, "interaction_command", None)
    if not subcommand:
        print("[bugx interaction] 错误: 请指定子命令 (list, show, answer, cancel)")
        return 1
    try:
        store = _load_store()
    except Exception as exc:  # pragma: no cover - 安装损坏时才会触发
        print(f"[bugx interaction] ✗ 无法加载交互信箱模块: {exc}")
        return 1

    root = store.resolve_root(getattr(args, "root", None))
    handlers = {
        "list": _command_list,
        "show": _command_show,
        "answer": _command_answer,
        "cancel": _command_cancel,
    }
    handler = handlers.get(subcommand)
    if handler is None:  # pragma: no cover - argparse 已限定 choices
        print(f"[bugx interaction] 错误: 未知子命令 {subcommand}")
        return 1
    return handler(args, store, root)
