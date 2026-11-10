#!/usr/bin/env python3
"""BugX 宿主无关交互组件。

上层业务只声明问题，组件根据已经探测完成的环境选择原生请求、TTY
方向键菜单、TTY 文本菜单或阻断。这里不执行任何业务动作；云端、MCP、
重置和生命周期逻辑仍由调用方负责。

问题与答案会经 :mod:`core.runtime.interaction_store` 落到项目运行态
``<root>/.bugx/state/interaction/``，这是非交互宿主（DSH / CI / 远程
Worker）唯一的答案回灌通道——没有它，``bugx engine pre`` 的启动门禁只能
靠提前写配置文件或环境变量满足，在无 TTY 宿主上必然死锁。

答案优先级（从高到低，取值一律 fail-closed）：

1. 调用方显式参数 / CLI 显式开关（``--report-policy``、``--polling`` 等），
   在进入本组件之前就已定值，本组件不覆盖；
2. ``[DEV_ONLY: INTERACTION_PRESET]`` 测试预设（``test-interaction-preset.json``）；
3. 信箱内**尚未消费**的答案（``question_id`` + ``phase`` 双匹配，取最新一条）；
4. TTY 方向键菜单 / 宿主原生提问；
5. 结构化 ``NEEDS_DECISION``，其 ``reason`` 携带精确的
   ``bugx interaction answer <id> --value <v>`` 命令，禁止裸「已停止」。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import getpass
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

try:
    import core.i18n as _i18n
except ImportError:  # pragma: no cover - malformed legacy snapshot
    _i18n = None

try:
    from core.runtime.interactive_menu import RADIO_UNAVAILABLE, select_one as _select_one_impl
except ImportError:  # pragma: no cover - malformed legacy snapshot
    RADIO_UNAVAILABLE = object()
    _select_one_impl = None  # type: ignore[assignment]

try:
    from core.runtime import interaction_store as _store
except ImportError:  # pragma: no cover - malformed legacy snapshot
    _store = None  # type: ignore[assignment]

REQUEST_PREFIX = "BUGX_INTERACTION_REQUEST"
SCHEMA = "bugx-interaction/v1"
ANSWERED = "answered"
NEEDS_DECISION = "needs_decision"
CANCELLED = "cancelled"
INVALID = "invalid"
FAILED = "failed"
#: 从答案信箱取回答案时使用的通道名（与 preset/tty-radio 并列，供调用方审计）。
INTAKE_CHANNEL = "intake"
_KINDS = frozenset({"select", "confirm", "text", "secret"})
_ACTION_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")

# [BEGIN DEV_ONLY: INTERACTION_PRESET]
try:
    from core.dev_harness.interaction_preset import (
        resolve_preset_answer,
        execute_preset_action,
        get_preset_runtime_config,
    )
except ImportError:
    resolve_preset_answer = None
    execute_preset_action = None
    get_preset_runtime_config = None


def try_resolve_startup_gate_preset(
    gate: str,
    options: Sequence[Any],
    root: Any = None,
) -> tuple[bool, Any | None]:
    """尝试匹配启动门禁预设；返回 (是否命中, 匹配选项)。"""
    if resolve_preset_answer is None:
        return False, None
    from pathlib import Path
    resolved_root = Path(root) if root is not None else None
    matched = resolve_preset_answer(gate, options, root=resolved_root)
    if matched is not None:
        if execute_preset_action is not None and resolved_root is not None:
            execute_preset_action(gate, matched, resolved_root)
        return True, matched
    return False, None


def try_get_preset_runtime_config(root: Any = None) -> dict[str, Any]:
    """尝试获取测试预设的运行时调度与环境参数；无预设或非测试通道返回空字典。"""
    if get_preset_runtime_config is None:
        return {}
    from pathlib import Path
    resolved_root = Path(root) if root is not None else None
    try:
        return get_preset_runtime_config(root=resolved_root)
    except Exception:
        return {}
# [END DEV_ONLY: INTERACTION_PRESET]



@dataclass(frozen=True)
class InteractionOption:
    """一个可提交的稳定选项。``action`` 只能是业务白名单标识。"""

    value: str
    label: str
    description: str = ""
    action: str | None = None
    retry: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "InteractionOption":
        value = raw.get("value")
        label = raw.get("label")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("交互选项 value 必须是非空字符串")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("交互选项 label 必须是非空字符串")
        description = raw.get("description", "")
        action = raw.get("action")
        retry = raw.get("retry")
        if not isinstance(description, str):
            raise ValueError("交互选项 description 必须是字符串")
        if action is not None and (not isinstance(action, str) or not action.strip()):
            raise ValueError("交互选项 action 必须是非空标识或省略")
        if action and not _ACTION_RE.fullmatch(action.strip()):
            raise ValueError("交互选项 action 只能是白名单标识，不能包含命令或路径")
        if retry is not None and (not isinstance(retry, str) or not retry.strip()):
            raise ValueError("交互选项 retry 必须是非空标识或省略")
        if retry and not _ACTION_RE.fullmatch(retry.strip()):
            raise ValueError("交互选项 retry 只能是白名单标识")
        return cls(
            value.strip(), label.strip(), description,
            action.strip() if action else None,
            retry.strip() if retry else None,
        )

    def localized(self, question_id: str, locale: str) -> "InteractionOption":
        if _i18n is None:
            return self
        if question_id == "cloud_sync" and self.value == "sync" and locale.startswith("zh"):
            label = self.label
        else:
            label = _i18n.option_text(
                question_id, self.value, "label", locale, self.label,
            )
        return InteractionOption(
            value=self.value,
            label=label,
            description=_i18n.option_text(
                question_id, self.value, "description", locale, self.description,
            ),
            action=self.action,
            retry=self.retry,
        )

    def as_dict(self) -> dict[str, str]:
        result = {
            "value": self.value,
            "label": self.label,
            "description": self.description,
        }
        if self.action:
            result["action"] = self.action
        if self.retry:
            result["retry"] = self.retry
        return result


@dataclass(frozen=True)
class InteractionQuestion:
    """一次串行问题定义。"""

    question_id: str
    phase: str
    kind: str = "select"
    title: str = ""
    question: str = ""
    options: tuple[InteractionOption, ...] = ()
    multiple: bool = False
    default: int = 0
    required: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        for name in ("question_id", "phase", "kind", "title", "question", "detail"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} 必须是字符串")
        if not isinstance(self.options, tuple):
            try:
                object.__setattr__(self, "options", tuple(self.options))
            except TypeError as exc:
                raise ValueError("options 必须是选项序列") from exc
        normalised: list[InteractionOption] = []
        for option in self.options:
            if isinstance(option, InteractionOption):
                normalised.append(option)
            elif isinstance(option, Mapping):
                normalised.append(InteractionOption.from_mapping(option))
            else:
                raise ValueError("options 中只能包含 InteractionOption 或对象映射")
        object.__setattr__(self, "options", tuple(normalised))
        if not self.question_id.strip():
            raise ValueError("question_id 不能为空")
        if not self.phase.strip():
            raise ValueError("phase 不能为空")
        if self.kind not in _KINDS:
            raise ValueError(f"不支持的交互类型: {self.kind}")
        if not self.question.strip():
            raise ValueError("question 不能为空")
        if self.kind in {"select", "confirm"} and not self.options:
            raise ValueError(f"{self.kind} 问题必须至少提供一个选项")
        if self.kind == "confirm" and len(self.options) != 2:
            raise ValueError("confirm 问题必须恰好提供两个选项")
        if self.kind in {"text", "secret"} and self.options:
            raise ValueError(f"{self.kind} 问题不得提供 options")
        if self.kind != "select" and self.multiple:
            raise ValueError("只有 select 问题允许 multiple=true")
        if self.kind in {"select", "confirm"} and not 0 <= self.default < len(self.options):
            raise ValueError("default 必须落在选项范围内")
        values = [option.value for option in self.options]
        if len(values) != len(set(values)):
            raise ValueError("交互选项 value 必须唯一")

    def localized(self, environment: Mapping[str, Any] | None = None) -> "InteractionQuestion":
        """Return display text localized for one environment snapshot.

        This deliberately returns a copy.  A question definition can therefore
        be reused for a second host or locale without mutating stable business
        values or leaking one user's language into another request.
        """
        env = _environment_dict(environment)
        if _i18n is not None:
            resolved = env.get("resolved_locale") or env.get("locale")
            if not isinstance(resolved, str) or not resolved:
                resolved = _i18n.resolve_locale().resolved_locale
            if self.question_id == "cloud_sync" and resolved.startswith("zh"):
                title = self.title
            else:
                title = _i18n.question_text(self.question_id, "title", resolved, self.title)
            question = self.question
            # Strategy plans carry a generated, project-specific summary.  A
            # static catalog sentence must never replace that evidence.
            if self.question_id != "strategy_plan":
                question = _i18n.question_text(
                    self.question_id, "question", resolved, self.question,
                )
            if self.question_id.startswith("tooling:") and "{name}" in question:
                question = question.replace("{name}", self.question_id.split(":", 1)[1])
            detail = _i18n.question_text(self.question_id, "detail", resolved, self.detail)
            options = tuple(
                option.localized(self.question_id, resolved) for option in self.options
            )
        else:  # pragma: no cover - only malformed installations
            title, question, detail, options = self.title, self.question, self.detail, self.options
        return InteractionQuestion(
            question_id=self.question_id,
            phase=self.phase,
            kind=self.kind,
            title=title,
            question=question,
            options=options,
            multiple=self.multiple,
            default=self.default,
            required=self.required,
            detail=detail,
        )

    def as_dict(self, environment: Mapping[str, Any] | None = None) -> dict[str, Any]:
        environment_dict = _environment_dict(environment)
        localized = self.localized(environment_dict)
        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "request": "question",
            "question_id": localized.question_id,
            "phase": localized.phase,
            "kind": localized.kind,
            "title": localized.title,
            "question": localized.question,
            "multiple": localized.multiple,
            "options": [option.as_dict() for option in localized.options],
        }
        if localized.kind in {"select", "confirm"}:
            payload["default"] = localized.default
        if localized.required:
            payload["required"] = True
        if localized.detail:
            payload["detail"] = localized.detail
        if _i18n is not None:
            locale = environment_dict.get("resolved_locale") or environment_dict.get("locale")
            if not isinstance(locale, str) or not locale:
                locale_info = _i18n.resolve_locale()
                locale = locale_info.resolved_locale
                locale_meta = locale_info.as_dict()
            else:
                locale_meta = {
                    "requested": environment_dict.get("requested_locale"),
                    "resolved": locale,
                    "source": environment_dict.get("locale_source", "environment"),
                    "fallback_reason": environment_dict.get("locale_fallback_reason"),
                }
            payload["locale"] = locale
            payload["locale_meta"] = locale_meta
        if environment is not None:
            payload["environment"] = environment_dict
        return payload


@dataclass(frozen=True)
class InteractionResult:
    status: str
    value: str | tuple[str, ...] | None = None
    channel: str = "blocked"
    reason: str = ""


def _environment_dict(environment: Any) -> dict[str, Any]:
    if environment is None:
        return {"host": "unknown", "interface": "unknown", "interaction": "blocked"}
    if isinstance(environment, Mapping):
        return {
            key: value for key, value in environment.items()
            if key not in {"secret", "token", "credentials", "command_line"}
        }
    try:
        result = asdict(environment)
    except (TypeError, ValueError):
        result = {
            key: getattr(environment, key)
            for key in ("host", "interface", "interaction")
            if hasattr(environment, key)
        }
    return {
        key: value for key, value in result.items()
        if key not in {"secret", "token", "credentials", "command_line"}
    }


def channel_for(environment: Any) -> str:
    channel = _environment_dict(environment).get("interaction")
    return channel if channel in {"native-question", "tty-radio", "text-menu", "blocked"} else "blocked"


def _emit(question: InteractionQuestion, environment: Any, output: Callable[[str], Any]) -> InteractionResult:
    payload = question.as_dict(_environment_dict(environment))
    try:
        output(f"{REQUEST_PREFIX} " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    except (OSError, UnicodeError) as exc:
        return InteractionResult(FAILED, channel=channel_for(environment), reason=str(exc))
    hint = ""
    if _store is not None:
        # 落盘必须先于返回：宿主在拿到 NEEDS_DECISION 之后才可能提交答案，
        # 若此时信箱里没有这条请求，answer 只会得到「未知问题 id」。
        _store.record_request(question)
        hint = _store.answer_hint(question.question_id, question.options)
    return InteractionResult(NEEDS_DECISION, channel=channel_for(environment), reason=hint)


def emit_request(
    question: InteractionQuestion,
    environment: Any = None,
    *,
    output: Callable[[str], Any] = print,
) -> InteractionResult:
    """只输出一次结构化问题，不尝试读取答案。"""
    return _emit(question, environment, output)


def _parse_indices(answer: str, options: Sequence[InteractionOption]) -> tuple[str, ...] | None:
    values = [part.strip() for part in answer.split(",") if part.strip()]
    if not values:
        return ()
    selected: list[str] = []
    for value in values:
        # isdecimal 而非 isdigit：isdigit 对 Unicode 上标数字（如 "²"）返回
        # True 但 int() 解析会崩；isdecimal 严格匹配十进制数字。
        if value.isdecimal():
            index = int(value) - 1
            if not 0 <= index < len(options):
                return None
            selected.append(options[index].value)
        elif value in {option.value for option in options}:
            selected.append(value)
        else:
            return None
    return tuple(dict.fromkeys(selected))


def _ask_menu(
    question: InteractionQuestion,
    *,
    input_fn: Callable[[str], str],
    output: Callable[[str], Any],
    locale: str = "en-US",
    max_attempts: int = 3,
) -> InteractionResult:
    attempts = 0
    while attempts < max_attempts:
        attempts += 1
        output("")
        if question.title:
            output(question.title)
        output(question.question)
        for index, option in enumerate(question.options, 1):
            separator = _i18n.common_text("description_separator", locale, "：") if _i18n else "："
            suffix = f"{separator}{option.description}" if option.description else ""
            output(f"  {index}) {option.label}{suffix}")
        if _i18n:
            prompt = _i18n.common_text(
                "multiple_prompt" if question.multiple else "single_prompt",
                locale,
                "请选择菜单编号（逗号分隔）：" if question.multiple else "请选择菜单编号：",
            )
        else:
            prompt = "请选择菜单编号（逗号分隔）：" if question.multiple else "请选择菜单编号："
        try:
            answer = input_fn(prompt).strip()
        except (EOFError, KeyboardInterrupt, OSError) as exc:
            return InteractionResult(CANCELLED, channel="text-menu", reason=str(exc))
        if question.multiple:
            selected = _parse_indices(answer, question.options)
            if selected is not None and (selected or not question.required):
                return InteractionResult(ANSWERED, selected, channel="text-menu")
        else:
            selected = _parse_indices(answer, question.options)
            if selected and len(selected) == 1:
                return InteractionResult(ANSWERED, selected[0], channel="text-menu")
        output(_i18n.common_text("invalid", locale, "输入无效，请选择当前菜单中的选项。") if _i18n else
               "输入无效，请选择当前菜单中的选项。")
    return InteractionResult(
        CANCELLED,
        channel="text-menu",
        reason=f"Exceeded maximum attempts ({max_attempts})",
    )


def ask(
    question: InteractionQuestion,
    environment: Any = None,
    *,
    output: Callable[[str], Any] = print,
    input_fn: Callable[[str], str] | None = None,
    password_fn: Callable[[str], str] | None = None,
    select_fn: Callable[..., Any] | None = None,
) -> InteractionResult:
    """获取一个答案；native/headless 只输出请求，不尝试读取 stdin。"""
    input_fn = input if input_fn is None else input_fn
    password_fn = getpass.getpass if password_fn is None else password_fn
    select_fn = _select_one_impl if select_fn is None else select_fn
    channel = channel_for(environment)
    environment_dict = _environment_dict(environment)
    question = question.localized(environment_dict)
    locale = environment_dict.get("resolved_locale") or environment_dict.get("locale")
    if not isinstance(locale, str) or not locale:
        locale = _i18n.resolve_locale().resolved_locale if _i18n else "en-US"

    # [BEGIN DEV_ONLY: INTERACTION_PRESET_ASK]
    if resolve_preset_answer is not None and question.options:
        preset_opt = resolve_preset_answer(question.question_id, question.options)
        if preset_opt is not None:
            return InteractionResult(ANSWERED, preset_opt.value, channel="preset")
    # [END DEV_ONLY: INTERACTION_PRESET_ASK]

    # 答案信箱：显式参数与测试预设之后、任何 TTY/原生通道之前。
    # 无 TTY 的宿主只能靠这条通道回灌答案，因此它必须在 blocked 短路上边。
    if _store is not None:
        lookup = _store.take_answer(question.question_id, question.phase)
        if lookup.status == _store.ANSWERED:
            return InteractionResult(ANSWERED, lookup.value, channel=INTAKE_CHANNEL)
        if lookup.status == _store.INVALID:
            # 非法答案不消费：用户可以重新提交正确取值，不会把错误答案静默记住。
            return InteractionResult(INVALID, channel=INTAKE_CHANNEL, reason=lookup.reason)
        if lookup.status == _store.CANCELLED:
            return InteractionResult(CANCELLED, channel=INTAKE_CHANNEL, reason=lookup.reason)

    if channel in {"native-question", "blocked"}:
        return _emit(question, environment, output)

    if question.kind in {"select", "confirm"} and not question.multiple and channel == "tty-radio":
        if select_fn is not None:
            try:
                selected = select_fn(
                    question.title or question.question,
                    [(option.label, option.description) for option in question.options],
                    default=question.default,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                selected = RADIO_UNAVAILABLE
                fallback_reason = str(exc)
            else:
                fallback_reason = ""
            if selected is not RADIO_UNAVAILABLE:
                if selected is None:
                    return InteractionResult(CANCELLED, channel="tty-radio")
                if isinstance(selected, int) and 0 <= selected < len(question.options):
                    return InteractionResult(ANSWERED, question.options[selected].value, channel="tty-radio")
                fallback_reason = "方向键控件返回了无效选项"
            if fallback_reason:
                output(fallback_reason)
        # curses 不可用但仍有 TTY 时，安全降级为同一组件的文本菜单。
        channel = "text-menu"

    if question.kind in {"select", "confirm"}:
        return _ask_menu(question, input_fn=input_fn, output=output, locale=locale)

    suffix = _i18n.common_text("prompt_suffix", locale, "：") if _i18n else "："
    prompt = question.question + (suffix if not question.question.endswith(("：", ":")) else "")
    try:
        value = password_fn(prompt) if question.kind == "secret" else input_fn(prompt)
    except (EOFError, KeyboardInterrupt, OSError) as exc:
        return InteractionResult(CANCELLED, channel=channel, reason=str(exc))
    value = value.strip()
    if question.required and not value:
        reason = _i18n.common_text("required", locale, "输入不能为空") if _i18n else "输入不能为空"
        return InteractionResult(INVALID, channel=channel, reason=reason)
    return InteractionResult(ANSWERED, value, channel=channel)


def select_one(*args: Any, **kwargs: Any) -> Any:
    """测试和旧调用的底层转发；实际路由仍由 :func:`ask` 负责。"""
    if _select_one_impl is None:
        return RADIO_UNAVAILABLE
    return _select_one_impl(*args, **kwargs)


class InteractionRouter:
    """DOC-025 统一交互路由器，提供静态与面向对象门面入口。"""

    @staticmethod
    def ask(
        question: InteractionQuestion,
        environment: Any = None,
        *,
        output: Callable[[str], Any] = print,
        input_fn: Callable[[str], str] | None = None,
        password_fn: Callable[[str], str] | None = None,
        select_fn: Callable[..., Any] | None = None,
    ) -> InteractionResult:
        return ask(
            question,
            environment,
            output=output,
            input_fn=input_fn,
            password_fn=password_fn,
            select_fn=select_fn,
        )


def ask_domain_gap_decision(
    gap: dict[str, Any],
    environment: Any = None,
    *,
    output: Callable[[str], Any] = print,
    input_fn: Callable[[str], str] | None = None,
) -> str:
    """针对 DOC-245 规约断环发起人机交互决策单选 (对齐 Cline/OpenCode/TTY).

    在无头/CI 模式下返回默认保守降级选项 ('spec_gap_continue')，绝不挂起。
    """
    domain = str(gap.get("domain", "business"))
    summary = str(gap.get("summary", "业务领域生命周期断环"))
    raw_options = gap.get("recommended_options", [])
    if not raw_options:
        raw_options = [
            {"value": "spec_gap_continue", "label": "确认遗漏需求，仅测已有代码并记录风险"},
            {"value": "preset_only", "label": "企业内部/外部授权模式（无需注册）"},
        ]

    options = tuple(
        InteractionOption(
            value=str(opt["value"]),
            label=str(opt["label"]),
            description=str(opt.get("description", "")),
        )
        for opt in raw_options
    )

    q = InteractionQuestion(
        question_id=f"spec_gap_{domain}",
        phase="spec_alignment",
        kind="select",
        title=f"⚠️ 检测到需求生命周期断环 [{domain}]",
        question=summary,
        options=options,
        default=0,
        required=True,
        detail=str(gap.get("detail", "")),
    )

    channel = channel_for(environment)
    if channel == "blocked":
        # 无头/CI/自治模式保守降级，零挂起
        return "spec_gap_continue"

    res = ask(q, environment=environment, output=output, input_fn=input_fn)
    if res.status == ANSWERED and res.value:
        return str(res.value)
    return "spec_gap_continue"
