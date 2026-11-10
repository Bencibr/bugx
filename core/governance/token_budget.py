#!/usr/bin/env python3
"""模型上下文预算的统一计量。

优先使用可选的 ``tiktoken``；未安装时采用保守 UTF-8 估算，绝不把估算值
伪装成模型真实 token 数。运行时返回计量方法，方便回放报告区分两者。
"""

from __future__ import annotations

import math
import json
import re
from typing import Any

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def estimate_tokens(value: str | bytes, model: str | None = None) -> tuple[int, str]:
    """返回 ``(tokens, method)``；token 值是可安全用于硬预算的上界估算。"""
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    text = raw.decode("utf-8", errors="replace")
    try:
        import tiktoken  # type: ignore[import-not-found]
        encoding = tiktoken.encoding_for_model(model) if model else tiktoken.get_encoding("o200k_base")
        return len(encoding.encode(text)), "tiktoken"
    except (ImportError, KeyError, ValueError):
        cjk = len(_CJK_RE.findall(text))
        words = len(_WORD_RE.findall(text))
        # UTF-8/3 比旧的 bytes/4 更保守，CJK 和标识符再单独兜底。
        return max(1, math.ceil(len(raw) / 3), cjk + math.ceil(words * 1.4)), "conservative-utf8-v1"


def budget_summary(items: list[dict[str, Any]], model: str | None = None) -> dict[str, Any]:
    """为 JSON 卡片列表提供一致的预算摘要。"""
    raw = "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in items)
    tokens, method = estimate_tokens(raw, model)
    return {"tokens": tokens, "bytes": len(raw.encode("utf-8")), "token_method": method}
