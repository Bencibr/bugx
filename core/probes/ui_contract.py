"""Validated, declarative UI rules stored in a project's DESIGN.md."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.probes.ui_storybook import STORY_ID
from core.probes.ui_tokens import TOKEN_FORMATS, load_mapped_tokens

STATES = ("loading", "error", "empty", "disabled")
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
TOKEN = re.compile(r"--[a-zA-Z_][a-zA-Z0-9_-]{0,95}")
STYLE_PROPERTIES = frozenset({
    "color", "background-color", "border-top-color", "outline-color",
    "font-family", "font-size", "font-weight", "line-height", "letter-spacing",
    "row-gap", "column-gap", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "margin-top", "margin-right", "margin-bottom", "margin-left",
    "border-top-left-radius", "max-width", "min-height",
})
CONTRAST_PROPERTIES = frozenset({"background-color", "border-top-color", "outline-color"})
KEYBOARD_KEYS = frozenset({
    "Tab", "Shift+Tab", "Enter", "Escape", "Space", "ArrowUp", "ArrowDown",
    "ArrowLeft", "ArrowRight", "Home", "End",
})


class UIContractError(ValueError):
    """Malformed UI audit input."""


@dataclass(frozen=True)
class UIContract:
    source: str | None = None
    sha256: str | None = None
    markdown: str = ""
    tokens: dict[str, str | None] | None = None
    components: tuple[dict[str, Any], ...] = ()
    contrast: tuple[dict[str, Any], ...] = ()
    journeys: tuple[dict[str, Any], ...] = ()
    reflow: tuple[dict[str, Any], ...] = ()
    keyboard: tuple[dict[str, Any], ...] = ()
    token_source: dict[str, Any] | None = None


def _object(value: Any, fields: set[str], where: str) -> dict:
    if not isinstance(value, dict) or set(value) - fields:
        raise UIContractError(f"{where}: expected an object with fields {sorted(fields)}")
    return value


def _items(value: Any, limit: int, where: str) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise UIContractError(f"{where}: expected an array of at most {limit} entries")
    return value


def _string(value: Any, where: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise UIContractError(f"{where}: expected nonempty text of at most {limit} characters")
    return value


def _identifiers(value: Any, limit: int, where: str) -> list[str]:
    entries = _items(value, limit, where)
    if any(not isinstance(name, str) or not IDENTIFIER.fullmatch(name) for name in entries):
        raise UIContractError(f"{where}: invalid identifier")
    if len(entries) != len(set(entries)):
        raise UIContractError(f"{where}: duplicate identifier")
    return entries


def _action(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise UIContractError(f"{where}: action must be an object")
    kind = value.get("type")
    extras = {
        "click": set(), "fill": {"value"}, "press": {"key"},
        "select_option": {"value"}, "wait_for": {"state"},
    }
    if not isinstance(kind, str) or kind not in extras:
        raise UIContractError(f"{where}: unsupported action type")
    _object(value, {"type", "selector"} | extras[kind], where)
    if kind != "press" or "selector" in value:
        _string(value.get("selector"), f"{where}.selector")
    if kind in {"fill", "select_option"}:
        if not isinstance(value.get("value"), str) or len(value["value"]) > 512:
            raise UIContractError(f"{where}.value: expected text of at most 512 characters")
    if kind == "press":
        _string(value.get("key"), f"{where}.key", 48)
    if kind == "wait_for" and value.get("state", "visible") not in ("visible", "hidden"):
        raise UIContractError(f"{where}.state: expected visible or hidden")
    return value


def _assertion(value: Any, where: str) -> dict:
    item = _object(value, {"selector", "text", "attributes", "disabled", "visible"}, where)
    _string(item.get("selector"), f"{where}.selector")
    if "text" in item:
        _string(item["text"], f"{where}.text")
    for key in ("visible", "disabled"):
        if key in item and type(item[key]) is not bool:
            raise UIContractError(f"{where}.{key}: expected boolean")
    if item.get("visible") is False and {"text", "attributes", "disabled"} & item.keys():
        raise UIContractError(f"{where}: hidden assertions cannot also inspect content")
    if "attributes" in item:
        attrs = item["attributes"]
        if not isinstance(attrs, dict) or not attrs or len(attrs) > 8:
            raise UIContractError(f"{where}.attributes: expected 1–8 attribute checks")
        for name, expected in attrs.items():
            if not re.fullmatch(r"(?:aria-[a-z-]+|role|data-[a-z0-9-]+)", name):
                raise UIContractError(f"{where}.attributes: unsupported attribute {name}")
            _string(expected, f"{where}.attributes.{name}")
    return item


def _rows(value: Any, kind: str, limit: int, fields: set[str]) -> tuple[dict, ...]:
    rows = tuple(_object(item, fields | {"id"}, kind) for item in _items(value, limit, kind))
    _identifiers([item.get("id") for item in rows], limit, f"{kind}.id")
    return rows


def _journey_target(row: dict, where: str) -> None:
    if "story" in row:
        if "path" in row or not STORY_ID.fullmatch(_string(row["story"], f"{where}.story", 192)):
            raise UIContractError(f"{where}: story must be a Storybook ID and cannot be combined with path")
        return
    path = _string(row.get("path", "/"), f"{where}.path", 512)
    if not path.startswith("/") or path.startswith("//") or "\\" in path or any(
        ord(ch) < 33 for ch in path
    ):
        raise UIContractError(f"{where}.path: expected an origin-relative URL")


def _validate(data: Any) -> dict[str, Any]:
    spec = _object(
        data, {"version", "tokens", "token_source", "components", "contrast", "journeys", "reflow", "keyboard"},
        "bugx-ui",
    )
    if type(spec.get("version")) is not int or spec["version"] != 1:
        raise UIContractError("bugx-ui.version: expected 1")
    tokens = spec.get("tokens", {})
    if not isinstance(tokens, dict) or len(tokens) > 64:
        raise UIContractError("tokens: expected up to 64 CSS custom properties")
    for key, value in tokens.items():
        if not TOKEN.fullmatch(key):
            raise UIContractError(f"invalid CSS token name: {key}")
        if value is not None:
            _string(value, f"tokens.{key}")
    source = None
    if "token_source" in spec:
        source = _object(spec["token_source"], {"file", "format", "map"}, "token_source")
        _string(source.get("file"), "token_source.file", 512)
        if source.get("format") not in TOKEN_FORMATS:
            raise UIContractError(f"token_source.format: expected one of {TOKEN_FORMATS}")
        mapping = source.get("map")
        if not isinstance(mapping, dict) or not mapping or len(mapping) + len(tokens) > 64:
            raise UIContractError("token_source.map: expected mapped CSS tokens within the 64 token limit")
        if mapping.keys() & tokens.keys():
            raise UIContractError("token_source.map and inline tokens must have disjoint names")
        for key, reference in mapping.items():
            if not TOKEN.fullmatch(key):
                raise UIContractError(f"invalid mapped CSS token name: {key}")
            _string(reference, f"token_source.map.{key}", 192)

    components = _rows(spec.get("components", []), "components", 32,
                       {"selector", "styles", "align", "tolerance_px", "profiles"})
    for item in components:
        _string(item.get("selector"), "components.selector")
        styles = _object(item.get("styles", {}), STYLE_PROPERTIES, "components.styles")
        for prop, expected in styles.items():
            _string(expected, f"components.styles.{prop}")
            if expected.startswith("--") and not TOKEN.fullmatch(expected):
                raise UIContractError(f"invalid token reference: {expected}")
        if "align" in item and item["align"] not in ("left", "top", "right", "center-x"):
            raise UIContractError("components.align: expected left, top, right, or center-x")
        if not styles and "align" not in item:
            raise UIContractError("components: styles or align is required")
        tolerance = item.get("tolerance_px", 2)
        if type(tolerance) not in (int, float) or not 0 <= tolerance <= 20:
            raise UIContractError("components.tolerance_px: expected 0–20")
        _identifiers(item.get("profiles", []), 12, "components.profiles")

    contrast = _rows(spec.get("contrast", []), "contrast", 12,
                     {"selector", "property", "against", "focus"})
    for item in contrast:
        _string(item.get("selector"), "contrast.selector")
        if item.get("property") not in sorted(CONTRAST_PROPERTIES):
            raise UIContractError(f"contrast.property: expected one of {sorted(CONTRAST_PROPERTIES)}")
        if item.get("against", "parent") not in ("self", "parent"):
            raise UIContractError("contrast.against: expected self or parent")
        if item["property"] == "background-color" and item.get("against") == "self":
            raise UIContractError("contrast: background-color must be compared to the parent")
        if "focus" in item and type(item["focus"]) is not bool:
            raise UIContractError("contrast.focus: expected boolean")

    reflow = _rows(spec.get("reflow", []), "reflow", 20, {"selector", "interactive"})
    for item in reflow:
        _string(item.get("selector"), "reflow.selector")
        if "interactive" in item and type(item["interactive"]) is not bool:
            raise UIContractError("reflow.interactive: expected boolean")

    journeys = _rows(spec.get("journeys", []), "journeys", 12,
                     {"state", "path", "story", "actions", "expect"})
    for item in journeys:
        if item.get("state") not in STATES:
            raise UIContractError(f"journeys.state: expected one of {STATES}")
        _journey_target(item, "journeys")
        for step in _items(item.get("actions", []), 8, "journeys.actions"):
            _action(step, "journeys.actions")
        assertions = [_assertion(check, "journeys.expect")
                      for check in _items(item.get("expect"), 8, "journeys.expect")]
        if not any(check.keys() & {"text", "attributes", "disabled"} for check in assertions):
            raise UIContractError("journeys.expect: provide a text, attribute or disabled assertion")
        if item["state"] == "disabled" and not any(check.get("disabled") is True for check in assertions):
            raise UIContractError("disabled journeys must assert disabled: true")
    keyboard = _rows(spec.get("keyboard", []), "keyboard", 8, {"path", "story", "steps"})
    for item in keyboard:
        _journey_target(item, "keyboard")
        steps = _items(item.get("steps"), 20, "keyboard.steps")
        if not steps:
            raise UIContractError("keyboard.steps: at least one step is required")
        for step in steps:
            step = _object(step, {"key", "focus", "indicator"}, "keyboard.steps")
            if _string(step.get("key"), "keyboard.steps.key") not in KEYBOARD_KEYS:
                raise UIContractError("keyboard.steps.key: unsupported navigation key")
            _string(step.get("focus"), "keyboard.steps.focus")
            if "indicator" in step:
                _string(step["indicator"], "keyboard.steps.indicator")
    return dict(tokens=tokens, components=components, contrast=contrast, journeys=journeys,
                reflow=reflow, keyboard=keyboard, token_source=source)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise UIContractError(f"duplicate bugx-ui key: {key}")
        result[key] = value
    return result


def load_ui_contract(root: Path, design: Path | None = None) -> UIContract:
    """Load at most one bugx-ui JSON fence. Plain Markdown is a visual reference."""
    path = root / (design if design is not None else Path("DESIGN.md"))
    if design is None and not path.exists():
        return UIContract()
    with path.open("rb") as stream:
        raw = stream.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise UIContractError("DESIGN.md exceeds 128 KiB")
    markdown = raw.decode("utf-8")
    starts = list(re.finditer(r"(?m)^[ \t]{0,3}" + chr(96) * 3 + r"bugx-ui[ \t]*\r?$", markdown))
    if len(starts) > 1:
        raise UIContractError("DESIGN.md must contain at most one bugx-ui block")
    rules = {}
    if starts:
        rest = markdown[starts[0].end():]
        end = re.search(r"(?m)^[ \t]{0,3}" + chr(96) * 3 + r"[ \t]*\r?$", rest)
        if end is None:
            raise UIContractError("unclosed bugx-ui fence")
        try:
            rules = _validate(json.loads(rest[:end.start()], object_pairs_hook=_unique))
        except json.JSONDecodeError as exc:
            raise UIContractError(f"invalid bugx-ui JSON at line {exc.lineno}: {exc.msg}") from exc
    if rules.get("token_source"):
        imported, provenance = load_mapped_tokens(root, rules["token_source"])
        rules.update(tokens={**rules["tokens"], **imported}, token_source=provenance)
    return UIContract(str(path.resolve()), hashlib.sha256(raw).hexdigest(), markdown, **rules)
