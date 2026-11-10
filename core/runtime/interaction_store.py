#!/usr/bin/env python3
"""BugX 交互请求的持久化信箱：请求落盘 + 答案回灌。

缺陷背景：交互组件只会把结构化问题打印成 ``BUGX_INTERACTION_REQUEST``
行，宿主（DSH / CI / 远程 Worker）**没有任何回灌答案的通道**，于是
``bugx engine pre`` 的启动门禁只能靠提前写配置文件或环境变量才能满足，
非交互宿主上表现为门禁死锁。

本模块提供唯一答案通道，落盘位置固定为项目运行态
``<root>/.bugx/state/interaction/``（一个问题一个 JSON 文件），
与 ``provider-window.json`` / ``cloud-sync-startup.json`` 同级：
它必须早于任何 Lifecycle 存在，因此不使用 ``bugx_paths.scope()``
（``scope()`` 要求 ``current.json`` 指向已存在的 Session/Round）。
写盘统一走 :mod:`core.secure_atomic` 的原子替换（临时文件 + ``os.replace``），
并发控制使用同目录锁文件；``fcntl`` 在 Windows 上不可用，导入与加锁
双端降级为「无锁但仍是原子替换」，绝不因平台差异崩溃。

答案优先级（从高到低，与 :mod:`core.runtime.interaction` 的装配顺序一致）：

1. 调用方显式参数 / CLI 显式开关（如 ``--report-policy``、``--polling``）；
2. DEV_ONLY 交互预设文件（``test-interaction-preset.json``）；
3. **本信箱内尚未消费的答案**（与 ``question_id`` + ``phase`` 双匹配）；
4. TTY 方向键菜单 / 宿主原生提问；
5. 结构化 ``NEEDS_DECISION``（消息里携带精确的
   ``bugx interaction answer <id> --value <v>`` 命令）。

一旦答案被消费，``consumed_at`` 落盘保留（审计需要），但不再满足后续
启动——避免一个旧答案静默地把下一次运行也「答」过去。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator, Mapping, Sequence

from core.secure_atomic import AtomicWriteError, atomic_write_json

try:  # Windows 无 fcntl：加锁降级，原子替换仍然保证单文件一致性。
    import fcntl
except ImportError:  # pragma: no cover - 平台差异分支
    fcntl = None  # type: ignore[assignment]

STORE_SCHEMA = "bugx-interaction-store/v1"
STORE_DIRNAME = "interaction"
LOCK_FILENAME = ".lock"
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
MAX_ANSWER_HISTORY = 32

NONE = "none"
ANSWERED = "answered"
INVALID = "invalid"
CANCELLED = "cancelled"


class StoreUnavailable(RuntimeError):
    """项目未初始化 ``.bugx`` 布局，交互信箱没有可信落盘位置。"""


class AnswerRejected(ValueError):
    """提交的答案无法通过问题自身的取值校验。"""


class UnknownQuestion(KeyError):
    """提交的 ``question_id`` 在信箱里没有对应的请求记录。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_root(root: str | Path | None = None) -> Path:
    """解析项目根：显式参数 > ``BUGX_PROJECT_ROOT`` > 当前工作目录。"""
    if root is not None:
        return Path(root).expanduser().resolve()
    override = os.environ.get("BUGX_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path.cwd().resolve()


def store_directory(root: str | Path | None = None) -> Path:
    return resolve_root(root) / ".bugx" / "state" / STORE_DIRNAME


def is_available(root: str | Path | None = None) -> bool:
    """信箱只在已初始化 ``.bugx`` 布局的项目里生效，绝不凭空创建运行态。"""
    return (resolve_root(root) / ".bugx").is_dir()


def require_available(root: str | Path | None = None) -> Path:
    directory = store_directory(root)
    if not (resolve_root(root) / ".bugx").is_dir():
        raise StoreUnavailable(
            f"项目未初始化 .bugx 布局：{resolve_root(root)}；"
            "请先运行 bugx init 或 bugx engine pre，再使用 bugx interaction"
        )
    return directory


def record_path(question_id: str, root: str | Path | None = None) -> Path:
    if not _ID_RE.match(question_id or ""):
        raise ValueError(f"question_id 非法（仅允许 [A-Za-z0-9_.:-]）：{question_id!r}")
    return store_directory(root) / f"{question_id}.json"


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """同目录排他锁；``fcntl`` 缺失或不可加锁时静默降级。"""
    handle = None
    if fcntl is not None:
        try:
            handle = open(directory / LOCK_FILENAME, "a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            if handle is not None:
                handle.close()
            handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != STORE_SCHEMA:
        return None
    if not isinstance(payload.get("answers"), list):
        payload["answers"] = []
    return payload


def _write_record(path: Path, record: dict[str, Any]) -> None:
    record["schema"] = STORE_SCHEMA
    record["updated_at"] = _now()
    try:
        atomic_write_json(path, record)
    except (AtomicWriteError, OSError, ValueError) as exc:
        raise StoreUnavailable(f"交互信箱写入失败：{path}（{exc}）") from exc


def question_payload(question: Any) -> dict[str, Any]:
    """把 ``InteractionQuestion`` 压成可持久化的契约字典。"""
    as_dict = getattr(question, "as_dict", None)
    if callable(as_dict):
        payload = as_dict()
    else:  # pragma: no cover - 非组件问题（兼容映射输入）
        payload = dict(question)
    options = []
    for option in payload.get("options") or []:
        options.append({
            key: option.get(key)
            for key in ("value", "label", "description", "action", "retry")
            if option.get(key) not in (None, "")
        })
    return {
        "question_id": str(payload.get("question_id") or ""),
        "phase": str(payload.get("phase") or "runtime"),
        "kind": str(payload.get("kind") or "select"),
        "title": str(payload.get("title") or ""),
        "question": str(payload.get("question") or ""),
        "detail": str(payload.get("detail") or ""),
        "options": options,
        "default": payload.get("default"),
        "multiple": bool(payload.get("multiple")),
        "required": bool(payload.get("required")),
    }


def record_request(question: Any, root: str | Path | None = None) -> dict[str, Any] | None:
    """落盘一次新发出的请求；未初始化项目布局时返回 ``None``（不创建运行态）。"""
    payload = question_payload(question)
    question_id = payload["question_id"]
    if not question_id or not _ID_RE.match(question_id):
        return None
    if not is_available(root):
        return None
    directory = store_directory(root)
    path = directory / f"{question_id}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with _locked(directory):
            record = _read_record(path) or {}
            record.setdefault("created_at", _now())
            record["question_id"] = question_id
            record["phase"] = payload["phase"]
            record["request"] = payload
            # 新请求作废旧取消标记：取消只对当时那一轮有效。
            record["cancel"] = None
            record["answers"] = list(record.get("answers") or [])[-MAX_ANSWER_HISTORY:]
            _write_record(path, record)
    except (OSError, StoreUnavailable):
        return None
    return record


def read_record(question_id: str, root: str | Path | None = None) -> dict[str, Any] | None:
    if not is_available(root):
        return None
    try:
        return _read_record(record_path(question_id, root))
    except (OSError, ValueError):
        return None


def list_records(root: str | Path | None = None) -> list[dict[str, Any]]:
    """按问题 id 排序返回全部请求记录（损坏文件跳过，不抛异常）。"""
    if not is_available(root):
        return []
    directory = store_directory(root)
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return []
    records = []
    for path in paths:
        record = _read_record(path)
        if record is not None:
            records.append(record)
    return records


def normalize_value(record: Mapping[str, Any], value: str) -> str:
    """把 CLI 友好输入（序号、逗号分隔）规范化成问题契约里的真实取值。

    只有 ``validate_value`` 通过后才调用。序号必须在这里折成 ``option.value``：
    实测缺陷是校验放行 ``--value 2`` 却把原文 ``"2"`` 落盘，于是调用方拿到的
    答案既不是 ``local`` 也不是任何合法取值——校验通过了，值却是错的。
    """
    request = record.get("request") or {}
    kind = str(request.get("kind") or "select")
    raw = "" if value is None else str(value)
    if kind not in {"select", "confirm"}:
        return raw
    options = [str(option.get("value")) for option in request.get("options") or []]
    if not options:
        return raw
    normalized: list[str] = []
    for item in [part.strip() for part in raw.split(",") if part.strip()]:
        if item in options:
            normalized.append(item)
        elif item.isdecimal() and 1 <= int(item) <= len(options):
            normalized.append(options[int(item) - 1])
        else:  # pragma: no cover - validate_value 已拦下
            return raw
    return ",".join(normalized)


def validate_value(record: Mapping[str, Any], value: str) -> str | None:
    """按问题自身的 kind/options 校验取值；返回错误原因，``None`` 表示合法。"""
    request = record.get("request") or {}
    kind = str(request.get("kind") or "select")
    options = [str(option.get("value")) for option in request.get("options") or []]
    raw = "" if value is None else str(value)
    if kind in {"select", "confirm"}:
        if not options:
            return "该问题没有可选值，无法使用 answers 通道"
        requested = [part.strip() for part in raw.split(",") if part.strip()]
        if not requested:
            return "答案不能为空"
        if len(requested) > 1 and not request.get("multiple"):
            return f"该问题是单选，只接受一个值：{options}"
        allowed = []
        for item in requested:
            if item in options:
                allowed.append(item)
            elif item.isdecimal() and 1 <= int(item) <= len(options):
                allowed.append(options[int(item) - 1])
            else:
                return f"值 {item!r} 不在可选项 {options} 中"
        return None if allowed else "答案不能为空"
    if request.get("required") and not raw.strip():
        return "该问题为必填文本，答案不能为空"
    return None


def _append_answer(
    record: dict[str, Any], value: str, *, channel: str,
) -> dict[str, Any]:
    answer = {
        "value": str(value),
        "phase": record.get("phase"),
        "answered_at": _now(),
        "channel": channel,
        "consumed_at": None,
        "consumed_pid": None,
    }
    answers = list(record.get("answers") or [])
    answers.append(answer)
    record["answers"] = answers[-MAX_ANSWER_HISTORY:]
    return answer


def submit_answer(
    question_id: str,
    value: str,
    *,
    root: str | Path | None = None,
    channel: str = "cli",
) -> dict[str, Any]:
    """写入一条答案（先按问题契约校验）；未消费前可反复被同一 phase 的复跑使用。"""
    require_available(root)
    record = read_record(question_id, root)
    if record is None:
        raise UnknownQuestion(
            f"未知的交互问题 id：{question_id}；先用 bugx interaction list 查看已发出的请求"
        )
    reason = validate_value(record, value)
    if reason is not None:
        raise AnswerRejected(reason)
    directory = store_directory(root)
    path = record_path(question_id, root)
    with _locked(directory):
        latest = _read_record(path) or record
        reason = validate_value(latest, value)
        if reason is not None:
            raise AnswerRejected(reason)
        answer = _append_answer(
            latest, normalize_value(latest, value), channel=channel,
        )
        _write_record(path, latest)
    return answer


def cancel(
    question_id: str, *, root: str | Path | None = None, reason: str = "",
) -> dict[str, Any]:
    """写入取消标记；下一次同一 phase 的 ``ask`` 返回 ``CANCELLED`` 后即失效。"""
    require_available(root)
    record = read_record(question_id, root)
    if record is None:
        raise UnknownQuestion(f"未知的交互问题 id：{question_id}")
    path = record_path(question_id, root)
    marker = {"at": _now(), "reason": str(reason or ""), "consumed_at": None}
    with _locked(store_directory(root)):
        latest = _read_record(path) or record
        latest["cancel"] = marker
        _write_record(path, latest)
    return marker


@dataclass(frozen=True)
class AnswerLookup:
    """信箱查询结果：``status`` 属于 ``none/answered/invalid/cancelled``。"""

    status: str
    value: Any = None
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.status != NONE


def _consume(path: Path, record: dict[str, Any]) -> None:
    _write_record(path, record)


def take_answer(
    question_id: str, phase: str | None, *, root: str | Path | None = None,
) -> AnswerLookup:
    """取走一条可用答案（消费即落盘）；不合法时不消费，交由调用方报 INVALID。"""
    if not _ID_RE.match(question_id or ""):
        return AnswerLookup(NONE)
    record = read_record(question_id, root)
    if record is None:
        return AnswerLookup(NONE)
    path = record_path(question_id, root)
    with _locked(store_directory(root)):
        record = _read_record(path) or record
        cancel_marker = record.get("cancel")
        if isinstance(cancel_marker, dict) and not cancel_marker.get("consumed_at"):
            cancel_marker = dict(cancel_marker)
            cancel_marker["consumed_at"] = _now()
            record["cancel"] = cancel_marker
            _consume(path, record)
            return AnswerLookup(CANCELLED, reason=str(cancel_marker.get("reason") or ""))
        target_phase = str(phase or record.get("phase") or "")
        answers = record.get("answers") or []
        pending = [
            answer for answer in answers
            if not answer.get("consumed_at") and str(answer.get("phase") or "") == target_phase
        ]
        if not pending:
            return AnswerLookup(NONE)
        candidate = pending[-1]
        reason = validate_value(record, str(candidate.get("value")))
        if reason is not None:
            return AnswerLookup(INVALID, reason=reason)
        candidate["consumed_at"] = _now()
        candidate["consumed_pid"] = os.getpid()
        _consume(path, record)
        return AnswerLookup(ANSWERED, value=candidate.get("value"))


def answer_command(question_id: str, options: Sequence[Any] = (), *, root: str | Path | None = None) -> str:
    """生成可直接复制执行的答案提交命令（消息里必须携带的精确命令）。"""
    sample = ""
    for option in options:
        value = getattr(option, "value", None)
        if value is None and isinstance(option, Mapping):
            value = option.get("value")
        if value:
            sample = str(value)
            break
    record = read_record(question_id, root) if is_available(root) else None
    if not sample and record:
        for option in (record.get("request") or {}).get("options") or []:
            if option.get("value"):
                sample = str(option["value"])
                break
    value_hint = f"--value {sample}" if sample else "--value <值>"
    return f"bugx interaction answer {question_id} {value_hint}"


def answer_hint(question_id: str, options: Sequence[Any] = (), *, root: str | Path | None = None) -> str:
    """给结构化 ``NEEDS_DECISION`` 用的中文指引（含精确命令）。"""
    return (
        f"该问题需要人工决策；非交互宿主请先执行 "
        f"`{answer_command(question_id, options, root=root)}`，然后重新运行 pre。"
        "可用 bugx interaction list 查看全部待决策问题。"
    )
