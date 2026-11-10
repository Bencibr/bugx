#!/usr/bin/env python3
"""core/hosts/mcp_toml.py - TOML 宿主 MCP 表的最小编辑原语（仅标准库）。

为什么需要它：``codex-cli 0.163.0`` 的项目级 MCP 注册只认 ``.codex/config.toml``
的 ``[mcp_servers.<name>]`` 表；此前的 JSON 写入器在该宿主上产生的是**死配置**
（宿主永不加载 ``.codex/config.json``，2026-10-23 真机实测）。标准库 ``tomllib``
只读，这里用「表块文本编辑 + 写入后 ``tomllib`` 复核」的窄实现，不引入第三方
TOML 写库：

* 读：``tomllib.loads`` → ``mcp_servers`` 映射；
* 写：只在文件尾部追加或替换**受管表块**，用户注释/顺序/其它表逐字节保留；
* 验：每次改动后 ``tomllib.loads`` 复核，解析失败即抛错（调用方回滚备份）。

失败姿态：未知字段、缺 ``command``、非受管条目冲突、表块歧义一律 ``ValueError``，
不允许「大概写进去了」这种假通过。
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping
from typing import Any

__all__ = [
    "normalize_spec",
    "read_servers",
    "remove_servers",
    "remove_table",
    "render_block",
    "set_table_key",
    "upsert_servers",
    "validate",
]

#: 单括号表头 ``[a.b]``；``[[a]]`` 数组表另判（它同样终止上一块）。
_HEADER = re.compile(r"^[ \t]*\[(?!\[)[ \t]*([^\[\]]+?)[ \t]*\][ \t]*(?:#.*)?$")
_ARRAY_HEADER = re.compile(r"^[ \t]*\[\[")

#: 允许写入 ``[mcp_servers.<name>]`` 的字段（Codex 0.163 的 stdio/HTTP 口径）。
#: 宿主不认识的键会让整份配置被拒（``--strict-config`` 直接报错），因此新增字段
#: 必须显式加入白名单，而不是「写进去试试」。
_ALLOWED_FIELDS: tuple[str, ...] = (
    "command",
    "args",
    "env",
    "env_vars",
    "bearer_token_env_var",
    "url",
    "cwd",
)

#: ``type`` 字段是 JSON 宿主的方言；值为 stdio 时静默丢弃（TOML 侧由 ``command``
#: 隐含），其它传输形态必须显式失败，避免把 HTTP server 写成 stdio 表。
_TYPE_ALIASES = {"stdio": None}


def _split_key_path(raw: str) -> tuple[str, ...]:
    """把 ``a."b.c".d`` 拆成 ``("a", "b.c", "d")``（双引号内不切分）。"""

    parts: list[str] = []
    buffer = ""
    quoted = False
    for char in raw:
        if char == '"':
            quoted = not quoted
            buffer += char
        elif char == "." and not quoted:
            parts.append(buffer)
            buffer = ""
        else:
            buffer += char
    parts.append(buffer)
    return tuple(part.strip().strip('"') for part in parts if part.strip())


def _header_spans(lines: list[str]) -> list[tuple[int, tuple[str, ...] | None]]:
    """返回 ``[(行号, 键路径)]``；数组表记 ``None``（只作为块边界）。"""

    spans: list[tuple[int, tuple[str, ...] | None]] = []
    for index, line in enumerate(lines):
        if _ARRAY_HEADER.match(line):
            spans.append((index, None))
            continue
        match = _HEADER.match(line)
        if match:
            spans.append((index, _split_key_path(match.group(1))))
    return spans


def validate(text: str) -> dict[str, Any]:
    """解析 TOML 文本；语法错误原样抛出 ``ValueError`` 子类。"""

    return tomllib.loads(text)


def read_servers(text: str) -> dict[str, dict[str, Any]]:
    """读取 ``mcp_servers`` 表；结构非法时抛 ``ValueError``（不猜）。"""

    if not text.strip():
        return {}
    parsed = tomllib.loads(text)
    servers = parsed.get("mcp_servers")
    if servers is None:
        return {}
    if not isinstance(servers, dict):
        raise ValueError("mcp_servers 必须是表")
    result: dict[str, dict[str, Any]] = {}
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            raise ValueError(f"mcp_servers.{name} 必须是表")
        result[str(name)] = dict(spec)
    return result


def _toml_value(value: Any) -> str:
    """渲染标量/数组为 TOML 字面量（JSON 字符串转义是 TOML 基本字符串的子集）。"""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ValueError(f"不支持的 TOML 字段值类型: {type(value).__name__}")


def normalize_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """把宿主定义裁剪成 Codex 可接受的字段集；未知字段直接失败。"""

    normalized: dict[str, Any] = {}
    for key, value in spec.items():
        if key == "type":
            if _TYPE_ALIASES.get(str(value)) is None and str(value) != "stdio":
                raise ValueError(f"不支持的 MCP 传输类型: {value}")
            continue
        if key not in _ALLOWED_FIELDS:
            raise ValueError(f"不支持的 MCP 字段: {key}")
        normalized[key] = value
    if "url" not in normalized and "command" not in normalized:
        raise ValueError("stdio MCP server 缺少 command")
    return normalized


def render_block(name: str, spec: Mapping[str, Any]) -> str:
    """渲染一个 ``[mcp_servers.<name>]`` 表块（含 ``env`` 子表）。"""

    normalized = normalize_spec(spec)
    lines = [f"[mcp_servers.{name}]"]
    for key in _ALLOWED_FIELDS:
        if key not in normalized or key == "env":
            continue
        lines.append(f"{key} = {_toml_value(normalized[key])}")
    env = normalized.get("env")
    if env is not None:
        if not isinstance(env, Mapping) or not env:
            raise ValueError(f"mcp_servers.{name}.env 必须是非空表")
        lines.append("")
        lines.append(f"[mcp_servers.{name}.env]")
        for env_key, env_value in env.items():
            lines.append(f"{env_key} = {_toml_value(env_value)}")
    return "\n".join(lines) + "\n"


def _block_ranges(
    lines: list[str],
    spans: list[tuple[int, tuple[str, ...] | None]],
    predicate: Callable[[tuple[str, ...]], bool],
) -> list[tuple[int, int]]:
    """返回需要整体切除的行区间 ``[(start, end))``（块边界 = 下一个表头）。"""

    ranges: list[tuple[int, int]] = []
    for position, (start, path) in enumerate(spans):
        if path is None or not predicate(path):
            continue
        end = spans[position + 1][0] if position + 1 < len(spans) else len(lines)
        ranges.append((start, end))
    return ranges


def _drop_ranges(lines: list[str], ranges: list[tuple[int, int]]) -> list[str]:
    if not ranges:
        return list(lines)
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    kept: list[str] = []
    cursor = 0
    for start, end in merged:
        kept.extend(lines[cursor:start])
        cursor = end
    kept.extend(lines[cursor:])
    return kept


def _trailing_trim(lines: list[str]) -> list[str]:
    """切块后去掉尾部多余空行，让追加块紧贴内容。"""

    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def upsert_servers(
    text: str,
    specs: Mapping[str, Mapping[str, Any]],
    *,
    owned: Callable[[str, Mapping[str, Any]], bool],
    banner: str | None = None,
) -> tuple[str, list[str], list[str]]:
    """写入/更新 ``mcp_servers`` 条目，返回 ``(新文本, 新增, 更新)``。

    ``owned(name, existing)`` 判定冲突条目是否属于本工具：属于则原地替换（否则
    旧版 BugX 写的过期条目会永久卡住安装），不属于则抛 ``ValueError``——用户
    自有 server 绝不被静默覆盖。
    """

    existing = read_servers(text)
    normalized = {name: normalize_spec(spec) for name, spec in specs.items()}
    lines = text.splitlines(keepends=True)
    spans = _header_spans(lines)

    replace: list[tuple[int, int]] = []
    added: list[str] = []
    updated: list[str] = []
    for name, spec in normalized.items():
        current = existing.get(name)
        if current is None:
            added.append(name)
            continue
        if normalize_spec(current) == spec:
            continue
        if not owned(name, current):
            raise ValueError(f"mcp_servers.{name} 已存在且与 BugX 定义冲突（非 BugX 拥有），拒绝覆盖")
        updated.append(name)
        replace.extend(
            _block_ranges(
                lines,
                spans,
                lambda path, _name=name: path[0] == "mcp_servers" and path[1] == _name,
            )
        )

    if not added and not updated:
        return text, [], []

    kept = _trailing_trim(_drop_ranges(lines, replace))
    body = "".join(kept)
    if body and not body.endswith("\n"):
        body += "\n"
    blocks = [banner] if banner and body.strip() and banner not in body else []
    for name in [*added, *updated]:
        blocks.append(render_block(name, normalized[name]))
    new_text = body + ("\n" if body.strip() else "") + "\n".join(blocks)
    if not new_text.endswith("\n"):
        new_text += "\n"
    validate(new_text)
    return new_text, added, updated


def remove_servers(
    text: str,
    predicate: Callable[[str, Mapping[str, Any]], bool],
) -> tuple[str, list[str]]:
    """切除满足 ``predicate`` 的 ``[mcp_servers.*]`` 块，返回 ``(新文本, 名称)``。"""

    existing = read_servers(text)
    targets = [name for name, spec in existing.items() if predicate(name, spec)]
    if not targets:
        return text, []
    lines = text.splitlines(keepends=True)
    spans = _header_spans(lines)
    ranges = _block_ranges(
        lines,
        spans,
        lambda path: path[0] == "mcp_servers" and path[1] in targets,
    )
    new_text = "".join(_trailing_trim(_drop_ranges(lines, ranges)))
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    validate(new_text)
    return new_text, targets


def remove_table(text: str, path: Iterable[str]) -> tuple[str, bool]:
    """切除**精确匹配**的表块 ``[path]``，返回 ``(新文本, 是否切除)``。

    只用于「本工具写入且能证明归属」的整表回收（例如 Codex 用户级
    ``[projects."<root>"]`` 信任条目）。不动的部分逐字节保留，切口后仍走
    ``tomllib`` 复核：宁可失败，也不留下半截配置。
    """

    target = tuple(path)
    if not target or not text.strip():
        return text, False
    lines = text.splitlines(keepends=True)
    spans = _header_spans(lines)
    ranges = _block_ranges(lines, spans, lambda candidate: candidate == target)
    if not ranges:
        return text, False
    new_text = "".join(_trailing_trim(_drop_ranges(lines, ranges)))
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    validate(new_text)
    return new_text, True


def _toml_quoted(part: str) -> str:
    """裸键合法时原样输出，否则用基本字符串引号（路径等含特殊字符的键）。"""

    if part and re.fullmatch(r"[A-Za-z0-9_-]+", part):
        return part
    return json.dumps(part, ensure_ascii=False)


def set_table_key(
    text: str,
    path: Iterable[str],
    key: str,
    rendered_value: str,
) -> tuple[str, str]:
    """在 ``[path]`` 表中设置 ``key``，返回 ``(新文本, 状态)``。

    状态：``added``（新建表 + 键）、``updated``（表内补键）、``unchanged``（键已
    存在且值相同）。键已存在但值不同时**不覆盖**并抛 ``ValueError``——用户显式
    写了别的值就必须可见，不能由安装流程替他决定。
    """

    parts = tuple(path)
    lines = text.splitlines(keepends=True)
    spans = _header_spans(lines)
    for position, (start, header) in enumerate(spans):
        if header != parts:
            continue
        end = spans[position + 1][0] if position + 1 < len(spans) else len(lines)
        for offset in range(start + 1, end):
            stripped = lines[offset].strip()
            prefix = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
            if prefix != key:
                continue
            current = stripped.split("=", 1)[1].split("#", 1)[0].strip()
            if current == rendered_value:
                return text, "unchanged"
            raise ValueError(f"[{'.'.join(parts)}] 的 {key} 已是 {current}，拒绝覆盖")
        insert_at = end
        while insert_at - 1 > start and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, f"{key} = {rendered_value}\n")
        new_text = "".join(lines)
        validate(new_text)
        return new_text, "updated"

    body = "".join(_trailing_trim(list(lines)))
    if body and not body.endswith("\n"):
        body += "\n"
    header = "[" + ".".join(_toml_quoted(part) for part in parts) + "]"
    new_text = body + ("\n" if body.strip() else "") + f"{header}\n{key} = {rendered_value}\n"
    validate(new_text)
    return new_text, "added"
