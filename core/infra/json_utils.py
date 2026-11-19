"""Small JSON container helpers shared by protocol validators."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


def unique_json_object(
    pairs: Iterable[tuple[str, Any]],
    *,
    error_factory: Callable[[str], Exception] | None = None,
) -> dict[str, Any]:
    """Build an object while rejecting duplicate JSON member names."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            if error_factory is None:
                raise ValueError(f"duplicate JSON object key: {key}")
            raise error_factory(key)
        result[key] = value
    return result
