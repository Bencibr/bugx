#!/usr/bin/env python3
"""BugX 的最小、只读、宿主无关国际化边界。

首版只承诺 ``zh-CN`` 和 ``en-US``。语言选择不会调用 ``setlocale``、不会
修改进程全局状态，也不会把环境变量原文写入交互协议。交互层只把这里返回的
稳定 locale 和 catalog 文案用于展示；业务 value/action 永远保持不变。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

SUPPORTED_LOCALES = ("zh-CN", "en-US")
DEFAULT_LOCALE = "en-US"
_ENV_KEYS = ("LC_ALL", "LC_MESSAGES", "LANG")


@dataclass(frozen=True)
class LocaleResolution:
    """一次语言解析的可审计结果。"""

    requested_locale: str | None
    resolved_locale: str
    source: str
    fallback_reason: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "requested": self.requested_locale,
            "resolved": self.resolved_locale,
            "source": self.source,
            "fallback_reason": self.fallback_reason,
        }


def normalize_locale(value: object) -> str | None:
    """Normalize a BCP-47/POSIX-ish locale to a supported locale.

    ``None`` means the input is not supported.  ``C``/``POSIX`` are treated as
    the conventional English process locale, while arbitrary languages are not
    silently mistaken for Chinese.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    # Strip encoding and POSIX modifier, e.g. zh_CN.UTF-8 or en_US@posix.
    language = raw.split(".", 1)[0].split("@", 1)[0].replace("_", "-")
    parts = [part for part in language.split("-") if part]
    if not parts:
        return None
    primary = parts[0].casefold()
    if primary in {"c", "posix", "en", "eng"}:
        return "en-US"
    if primary in {"zh", "chi", "zho"}:
        # The first release intentionally has one Chinese regional catalog.
        return "zh-CN"
    if len(parts) >= 2 and primary == "en" and parts[1].casefold() in {"us", "gb"}:
        return "en-US"
    return None


def _read_preference(path: Path) -> str | None:
    """Read a user preference without following a symlink as the file itself."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, str):
        return payload.strip() or None
    if isinstance(payload, Mapping):
        value = payload.get("locale")
        return value.strip() if isinstance(value, str) and value.strip() else None
    return None


def resolve_locale(
    *,
    environ: Mapping[str, str] | None = None,
    preference_path: Path | None = None,
) -> LocaleResolution:
    """Resolve the display locale using explicit, persisted, then system hints.

    When a test or caller supplies an explicit environment mapping, the default
    home preference is intentionally not consulted unless ``BUGX_LOCALE_FILE``
    (or ``preference_path``) is present.  This keeps isolated probes deterministic.
    """
    env = dict(os.environ if environ is None else environ)
    explicit = env.get("BUGX_LOCALE", "").strip()
    if explicit:
        resolved = normalize_locale(explicit)
        if resolved:
            return LocaleResolution(explicit, resolved, "BUGX_LOCALE")
        return LocaleResolution(
            explicit, DEFAULT_LOCALE, "BUGX_LOCALE",
            f"unsupported locale: {explicit}",
        )

    configured_path = preference_path
    if configured_path is None:
        raw_path = env.get("BUGX_LOCALE_FILE", "").strip()
        if raw_path:
            configured_path = Path(raw_path).expanduser()
        elif environ is None:
            configured_path = Path.home() / ".bugx" / "locale.json"
    if configured_path is not None:
        requested = _read_preference(configured_path)
        if requested:
            resolved = normalize_locale(requested)
            if resolved:
                return LocaleResolution(requested, resolved, "preference-file")
            return LocaleResolution(
                requested, DEFAULT_LOCALE, "preference-file",
                f"unsupported locale: {requested}",
            )

    for key in _ENV_KEYS:
        raw = env.get(key, "").strip()
        if not raw:
            continue
        resolved = normalize_locale(raw)
        if resolved:
            return LocaleResolution(raw, resolved, key)
        return LocaleResolution(
            raw, DEFAULT_LOCALE, key,
            f"unsupported locale: {raw}",
        )
    return LocaleResolution(None, DEFAULT_LOCALE, "default")


def _catalog_path(locale: str) -> Path:
    return Path(__file__).resolve().parent / "locales" / f"{locale}.json"


@lru_cache(maxsize=len(SUPPORTED_LOCALES))
def _load_catalog(locale: str) -> dict[str, Any]:
    path = _catalog_path(locale)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    messages = payload.get("messages") if isinstance(payload, dict) else None
    return messages if isinstance(messages, dict) else {}


def _message(messages: Mapping[str, Any], key: str) -> str | None:
    value = messages.get(key)
    return value if isinstance(value, str) else None


def _lookup(locale: str, key: str) -> str | None:
    if locale not in SUPPORTED_LOCALES:
        locale = DEFAULT_LOCALE
    val = _message(_load_catalog(locale), key)
    if val is None and locale != DEFAULT_LOCALE:
        val = _message(_load_catalog(DEFAULT_LOCALE), key)
    return val


def question_text(
    question_id: str,
    field: str,
    locale: str,
    fallback: str,
) -> str:
    """Translate a question field, with a generic tooling fallback."""
    value = _lookup(locale, f"interaction.{question_id}.{field}")
    if value is None and question_id.startswith("tooling:"):
        value = _lookup(locale, f"interaction.tooling.{field}")
    return value if value is not None else fallback


def option_text(
    question_id: str,
    option_value: str,
    field: str,
    locale: str,
    fallback: str,
) -> str:
    """Translate one option by stable value; never translates the value itself."""
    key = f"interaction.{question_id}.options.{option_value}.{field}"
    value = _lookup(locale, key)
    return value if value is not None else fallback


def common_text(key: str, locale: str, fallback: str) -> str:
    """Translate interaction component chrome (prompts/errors), with fallback."""
    value = _lookup(locale, f"interaction.common.{key}")
    return value if value is not None else fallback


def entry_text(key: str, locale: str | None = None, fallback: str = "") -> str:
    """Lookup host-entry body text (protocol prompts rendered into host entries).

    DOC-230 语言策略：宿主入口**正文**与 description 一样按 locale 生成——
    locale 由 ``resolve_locale()`` 决定（``BUGX_LOCALE`` → 用户偏好 → 系统提示），
    签入镜像因此不绑定某一种语言。缺键回退到 ``fallback``（调用方提供，
    通常是同语言的常量兜底），保证入口永不渲染成空正文。
    """
    target_locale = locale or resolve_locale().resolved_locale
    full_key = key if key.startswith("host.entry.") else f"host.entry.{key}"
    value = _lookup(target_locale, full_key)
    return value if value is not None else fallback


def prompt_text(
    key: str,
    locale: str | None = None,
    fallback: str = "",
    **kwargs: Any,
) -> str:
    """Lookup a prompt message key, format it with kwargs safely, and fallback if missing."""
    target_locale = locale or resolve_locale().resolved_locale
    full_key = key if key.startswith("prompt.") else f"prompt.{key}"
    template = _lookup(target_locale, full_key)
    if template is None:
        template = fallback
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


def exception_text(
    key: str,
    locale: str | None = None,
    fallback: str = "",
    **kwargs: Any,
) -> str:
    """Lookup an exception/error message key, format it with kwargs safely, and fallback if missing."""
    target_locale = locale or resolve_locale().resolved_locale
    full_key = key if key.startswith("exception.") else f"exception.{key}"
    template = _lookup(target_locale, full_key)
    if template is None:
        template = fallback
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template

