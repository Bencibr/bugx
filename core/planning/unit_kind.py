#!/usr/bin/env python3
"""core/unit_kind.py - coverage unit 类型前缀的单一来源（DES-05）。

审计发现 ``"内部:"`` / ``"业务:"`` 两个字面量散落在 6+ 模块的十余处
``startswith`` 判断中，而阶段名的唯一常量 ``VALID_PHASES`` 只在
``module_coverage`` 内定义。新增单元类型时必须同步改动所有位置，**漏掉任何
一处都会导致静默误分类**——例如新类型被当作 business 处理，从而绕过
「内部优先」这一阶段级硬边界（``task_scheduler`` 里价值 1000 分的绝对优先级）。

本模块把前缀定义、解析与格式化收敛为单点 API：

* 判断类型一律走 :func:`is_internal` / :func:`is_business`；
* 剥离前缀走 :func:`parse_unit_id` / :func:`strip_unit_prefix`；
* 构造 ID 走 :func:`format_unit_id`；
* 需要遍历全部前缀时用 :data:`PREFIXES`，不要重新写字面量元组。

``VALID_PHASES`` 在此重新导出，供 ``module_coverage`` 复用，避免阶段名集合
与前缀定义再次漂移。
"""

from __future__ import annotations

from enum import Enum


class UnitKind(str, Enum):
    """coverage unit 的阶段类型。

    继承 ``str`` 以便与既有字符串比较、序列化和 JSON 落盘保持兼容
    （``UnitKind.INTERNAL == "内部"`` 成立）。
    """

    INTERNAL = "内部"
    BUSINESS = "业务"


#: 单元 ID 中类型前缀与正文之间的分隔符。
SEPARATOR = ":"

#: 全部已知前缀（含分隔符），顺序即「内部优先」的优先级顺序。
PREFIXES: tuple[str, ...] = tuple(f"{kind.value}{SEPARATOR}" for kind in UnitKind)

#: 阶段名集合。与 :data:`PREFIXES` 同源，供 ``module_coverage`` 复用。
VALID_PHASES = frozenset(kind.value for kind in UnitKind)


def _as_text(unit_id: object) -> str:
    """把入参归一化为 ``str``；``None`` 视为空串，避免调用方到处判空。"""
    return "" if unit_id is None else str(unit_id)


def unit_kind(unit_id: object) -> UnitKind | None:
    """返回单元 ID 的类型；前缀未知或缺失时返回 ``None``。

    ``None`` 表示「未知类型」而非「业务类型」——调用方若需要旧代码
    ``not unit_id.startswith("业务:")`` 的「非业务即内部」语义，应显式写成
    ``not is_business(unit_id)``，以免未知前缀被静默归入内部。
    """
    text = _as_text(unit_id)
    for kind in UnitKind:
        if text.startswith(f"{kind.value}{SEPARATOR}"):
            return kind
    return None


def is_internal(unit_id: object) -> bool:
    """单元是否带 ``内部:`` 前缀。"""
    return _as_text(unit_id).startswith(PREFIXES[0])


def is_business(unit_id: object) -> bool:
    """单元是否带 ``业务:`` 前缀。"""
    return _as_text(unit_id).startswith(PREFIXES[1])


def strip_unit_prefix(unit_id: object) -> str:
    """剥离已知类型前缀；无已知前缀时原样返回（仅去首尾空白）。"""
    text = _as_text(unit_id).strip()
    for prefix in PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def parse_unit_id(unit_id: object) -> tuple[UnitKind | None, str]:
    """拆出 ``(类型, 去前缀正文)``。

    未知前缀时类型为 ``None`` 且正文为原文本，调用方据此决定是拒绝还是
    按「未知类型」处理，不再依赖隐式的 ``startswith`` 兜底。
    """
    text = _as_text(unit_id).strip()
    kind = unit_kind(text)
    if kind is None:
        return None, text
    return kind, strip_unit_prefix(text)


def format_unit_id(kind: UnitKind | str, body: object) -> str:
    """按统一格式构造单元 ID。

    ``kind`` 可传 :class:`UnitKind` 或等价字符串；非法类型直接报错，避免
    拼出 ``"未知:xxx"`` 这类会被下游静默误分类的 ID。
    """
    resolved = kind if isinstance(kind, UnitKind) else UnitKind(str(kind))
    text = _as_text(body).strip()
    if not text:
        raise ValueError("单元正文不能为空")
    return f"{resolved.value}{SEPARATOR}{text}"


__all__ = [
    "UnitKind",
    "SEPARATOR",
    "PREFIXES",
    "VALID_PHASES",
    "unit_kind",
    "is_internal",
    "is_business",
    "strip_unit_prefix",
    "parse_unit_id",
    "format_unit_id",
]
