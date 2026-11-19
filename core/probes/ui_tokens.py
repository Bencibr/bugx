"""Resolve mapped CSS audit values from project design token JSON."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import unquote

TOKEN_FORMATS = ("dtcg", "style-dictionary")
_ALIAS = re.compile(r"\{([^{}]+)\}")
_DIMENSION_UNITS = {"px", "rem", "em", "vh", "vw", "%"}
_DURATION_UNITS = {"ms", "s"}
_GENERIC_FONTS = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui"}


class TokenSourceError(ValueError):
    """A declared token source could not produce an auditable CSS value."""


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise TokenSourceError(f"duplicate key in token JSON: {name}")
        result[name] = value
    return result


def _parts(reference: str) -> tuple[str, ...]:
    path = tuple(reference.split("."))
    if len(path) > 32 or any(not part or "{" in part or "}" in part for part in path):
        raise TokenSourceError(f"invalid token path: {reference!r}")
    return path


def _pointer(reference: object) -> tuple[str, ...]:
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise TokenSourceError("token $ref must be a same-file JSON Pointer")
    keys = unquote(reference[2:]).split("/")
    if len(keys) > 48 or any(re.search(r"~(?![01])", key) for key in keys):
        raise TokenSourceError("invalid token JSON Pointer")
    return tuple(key.replace("~1", "/").replace("~0", "~") for key in keys)


def _at(data: object, keys: tuple[str, ...]) -> object:
    for key in keys:
        if isinstance(data, dict) and key in data:
            data = data[key]
        elif isinstance(data, list) and re.fullmatch(r"0|[1-9][0-9]{0,5}", key) and int(key) < len(data):
            data = data[int(key)]
        else:
            raise TokenSourceError(f"unresolved token reference: {'.'.join(keys)}")
    return data


def _number(value: object) -> str:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise TokenSourceError("expected a finite token number")
    return format(value, ".12g")


def _css(value: object, kind: str | None, token_format: str) -> str:
    if isinstance(value, str):
        result = value.strip()
    elif type(value) in (int, float):
        if kind in {"color", "dimension", "duration", "fontFamily"}:
            raise TokenSourceError(f"token type {kind} requires a non-numeric CSS value or explicit unit")
        result = _number(value)
    elif isinstance(value, dict) and {"value", "unit"} <= value.keys():
        unit = value["unit"]
        allowed = _DURATION_UNITS if kind == "duration" else (
            {"px", "rem"} if token_format == "dtcg" else _DIMENSION_UNITS
        )
        if kind not in ("duration", "dimension", None) or not isinstance(unit, str) or unit not in allowed:
            raise TokenSourceError("unsupported token dimension or duration unit")
        result = _number(value["value"]) + unit
    elif isinstance(value, dict) and value.get("colorSpace") == "srgb" and kind in (None, "color"):
        channels = value.get("components")
        alpha = value.get("alpha", 1)
        if (not isinstance(channels, list) or len(channels) != 3 or
                any(type(number) not in (int, float) or not 0 <= number <= 1
                    for number in [*channels, alpha])):
            raise TokenSourceError("invalid sRGB token components or alpha")
        rgb = ", ".join(_number(number * 255) for number in channels)
        result = f"rgb({rgb})" if alpha == 1 else f"rgba({rgb}, {_number(alpha)})"
    elif kind == "fontFamily" and isinstance(value, list) and value and all(
        isinstance(item, str) and item.strip() for item in value
    ):
        result = ", ".join(
            item if item in _GENERIC_FONTS else json.dumps(item, ensure_ascii=False)
            for item in value
        )
    else:
        raise TokenSourceError(f"unsupported CSS token value for type {kind or 'unspecified'}")
    if not result or len(result) > 256 or re.search(r"[;{}\x00-\x1f]", result):
        raise TokenSourceError("token did not resolve to a single bounded CSS value")
    return result


def _resolve(
    data: dict, path: tuple[str, ...], token_format: str, seen: tuple[tuple[str, ...], ...] = (),
) -> tuple[object, str | None]:
    if path in seen or len(seen) >= 32:
        raise TokenSourceError(f"cyclic or excessively deep token reference: {'.'.join(path)}")
    seen = (*seen, path)
    dtcg = token_format == "dtcg"
    type_key, value_key = ("$type", "$value") if dtcg else ("type", "value")
    node = _at(data, path)
    if not isinstance(node, dict):
        raise TokenSourceError(f"token is not an object: {'.'.join(path)}")
    inherited = None
    if dtcg:
        for depth in range(len(path)):
            group = _at(data, path[:depth])
            if not isinstance(group, dict) or "$value" in group or "$extends" in group:
                raise TokenSourceError("mapped tokens require concrete groups without $extends")
            if type_key in group:
                inherited = group[type_key]
    kind = node.get(type_key, inherited)
    if kind is not None and not isinstance(kind, str):
        raise TokenSourceError("token type must be text")
    if dtcg and "$ref" in node:
        if value_key in node:
            raise TokenSourceError("token declares both $value and $ref")
        pointer = _pointer(node["$ref"])
        if "$value" in pointer:
            position = pointer.index("$value")
            value, target_type = _resolve(data, pointer[:position], token_format, seen)
            tail = pointer[position + 1:]
            if not tail and kind and target_type and kind != target_type:
                raise TokenSourceError(f"token $ref type mismatch: {'.'.join(path)}")
            return _at(value, tail), kind or (target_type if not tail else None)
        value = _at(data, pointer)
        if isinstance(value, dict) and (value_key in value or "$ref" in value):
            value, target_type = _resolve(data, pointer, token_format, seen)
            if kind and target_type and kind != target_type:
                raise TokenSourceError(f"token $ref type mismatch: {'.'.join(path)}")
            return value, kind or target_type
        return value, kind
    if value_key not in node:
        raise TokenSourceError(f"missing {value_key} for token {'.'.join(path)}")
    value = node[value_key]
    if isinstance(value, str):
        alias = _ALIAS.fullmatch(value.strip())
        if alias:
            value, target_type = _resolve(data, _parts(alias[1]), token_format, seen)
            if dtcg and kind and target_type and kind != target_type:
                raise TokenSourceError(f"token alias type mismatch: {'.'.join(path)}")
            return value, kind or target_type
        if not dtcg:
            value = _ALIAS.sub(
                lambda match: _css(*_resolve(data, _parts(match[1]), token_format, seen), token_format),
                value,
            )
    return value, kind


def load_mapped_tokens(root: Path, spec: dict) -> tuple[dict[str, str], dict]:
    """Read one confined token JSON and resolve only the explicitly mapped values."""
    root = root.resolve()
    relative = Path(spec["file"])
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".json":
        raise TokenSourceError("token_source.file must be a project-relative JSON file")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise TokenSourceError("token_source.file must resolve to a file within the project")
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise TokenSourceError("token JSON exceeds 1 MiB")
    document = json.loads(raw, object_pairs_hook=_pairs)
    if not isinstance(document, dict):
        raise TokenSourceError("token JSON root must be an object")
    token_format = spec["format"]
    values = {}
    for name, reference in spec["map"].items():
        try:
            values[name] = _css(*_resolve(document, _parts(reference), token_format), token_format)
        except TokenSourceError as exc:
            raise TokenSourceError(f"{name} → {reference}: {exc}") from exc
    return values, {"path": str(path), "format": token_format,
                    "sha256": hashlib.sha256(raw).hexdigest(), "map": spec["map"]}
