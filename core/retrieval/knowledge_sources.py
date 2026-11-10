"""Bounded read-only queries across the project and global knowledge ledgers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


_READ_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error)


def source_paths(project_path: str | Path, global_path: str | Path) -> list[tuple[str, Path]]:
    """Return readable local scopes in project-first order.

    The project ledger is authoritative and callers already validate it.  A
    missing or damaged global projection is an optional degradation boundary.
    """
    project = Path(project_path)
    global_ = Path(global_path)
    result = [("project", project)]
    global_db = global_.with_suffix(".sqlite3")
    if global_db.is_file() and not global_db.is_symlink():
        result.append(("global", global_))
    return result


def path_for_source(
    source: str, project_path: str | Path, global_path: str | Path,
) -> Path | None:
    if source == "project":
        return Path(project_path)
    if source == "global":
        return Path(global_path)
    return None


def _scoped_rows(
    ledger: Any,
    project_path: str | Path,
    global_path: str | Path,
    operation: Callable[..., Iterable[Mapping[str, Any]]],
    *,
    key: str,
    project_only_kwargs: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for source, path in source_paths(project_path, global_path):
        try:
            call_kwargs = dict(kwargs)
            if source == "project" and project_only_kwargs:
                call_kwargs.update(project_only_kwargs)
            rows = operation(path, **call_kwargs)
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                value = {**row, "knowledge_source": source}
                item_key = str(value.get(key) or "")
                if not item_key:
                    continue
                previous = by_key.get(item_key)
                if previous is None or source == "project":
                    by_key[item_key] = value
        except _READ_ERRORS:
            if source == "project":
                raise
    return sorted(
        by_key.values(),
        key=lambda item: (
            -float(item.get("match_score", 0) or 0),
            str(item.get(key) or ""),
        ),
    )


def retrieve_patterns(
    ledger: Any,
    project_path: str | Path,
    global_path: str | Path,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    include_pattern_ids = kwargs.pop("include_pattern_ids", None)
    return _scoped_rows(
        ledger, project_path, global_path, ledger.retrieve,
        key="pattern_id",
        project_only_kwargs=(
            {"include_pattern_ids": include_pattern_ids}
            if include_pattern_ids is not None else None
        ),
        **kwargs,
    )


def lookup_incidents(
    ledger: Any,
    project_path: str | Path,
    global_path: str | Path,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    return _scoped_rows(
        ledger, project_path, global_path, ledger.lookup_incidents,
        key="incident_id", **kwargs,
    )


def lookup_tools(
    ledger: Any,
    project_path: str | Path,
    global_path: str | Path,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    return _scoped_rows(
        ledger, project_path, global_path, ledger.lookup_tools,
        key="tool_name", **kwargs,
    )


def tool_matrix(
    ledger: Any,
    project_path: str | Path,
    global_path: str | Path,
) -> dict[str, Any]:
    categories: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for source, path in source_paths(project_path, global_path):
        try:
            matrix = ledger.tool_matrix(path)
        except _READ_ERRORS:
            if source == "project":
                raise
            continue
        for category in matrix.get("categories", []):
            name = str(category.get("category") or "")
            if not name:
                continue
            bucket = categories.setdefault(name, {})
            for item in category.get("tools", []):
                if not isinstance(item, Mapping):
                    continue
                tool_name = str(item.get("tool_name") or "")
                if not tool_name:
                    continue
                bucket[(tool_name, source)] = {
                    **item,
                    "knowledge_source": source,
                }
    result = [
        {
            "category": name,
            "tools": sorted(
                rows.values(),
                key=lambda item: (
                    -int(item.get("hits", 0) or 0),
                    -int(item.get("feedback", 0) or 0),
                    str(item.get("tool_name") or ""),
                    str(item.get("knowledge_source") or ""),
                ),
            )[:4],
        }
        for name, rows in sorted(categories.items())
    ]
    return {
        "schema_version": getattr(ledger, "SCHEMA_VERSION", None),
        "status": "ok" if result else "empty",
        "reason": None if result else "no_tool_feedback",
        "categories_total": len(result),
        "categories": result,
    }
