#!/usr/bin/env python3
"""BugX WebUI Host Adapters - Extensible Plugin Registry (REQ-01)."""

from __future__ import annotations

import shutil
from typing import Any

from .base import BaseHostAdapter
from .bugx import BugxHostAdapter
from .claude import ClaudeHostAdapter
from .cline import ClineHostAdapter
from .codex import CodexHostAdapter
from .dsh import DshHostAdapter
from .opencode import OpenCodeHostAdapter
from .pi import PiHostAdapter
from .zcode import ZCodeHostAdapter


class HostAdapterRegistry:
    """Registry maintaining all AI Host Adapter plugins."""

    def __init__(self) -> None:
        self._adapters: dict[str, BaseHostAdapter] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        defaults: list[BaseHostAdapter] = [
            CodexHostAdapter(),
            ClaudeHostAdapter(),
            OpenCodeHostAdapter(),
            ClineHostAdapter(),
            PiHostAdapter(),
            BugxHostAdapter(),
            ZCodeHostAdapter(),
            DshHostAdapter(),
        ]
        for adapter in defaults:
            self.register(adapter)

    def register(self, adapter: BaseHostAdapter) -> None:
        """Register a new host adapter plugin."""
        self._adapters[adapter.host_id.lower()] = adapter

    def get_adapter(self, host_id: str) -> BaseHostAdapter:
        """Retrieve the adapter for host_id, defaulting to CodexHostAdapter if not found."""
        return self._adapters.get(host_id.lower(), self._adapters.get("codex", CodexHostAdapter()))

    def list_adapters(self) -> list[BaseHostAdapter]:
        """List all registered adapters."""
        return list(self._adapters.values())

    def discover_all_models(self) -> list[dict[str, Any]]:
        """Probe real local configurations across all registered adapters for GET /models."""
        catalog: list[dict[str, Any]] = []

        # Maintain canonical display ordering
        preferred_order = ["cline", "codex", "claude", "opencode", "pi", "zcode", "dsh", "bugx"]
        seen: set[str] = set()

        for hid in preferred_order:
            if hid in self._adapters:
                adapter = self._adapters[hid]
                seen.add(hid)
                def_model, options = adapter.discover_models()
                bin_installed = bool(shutil.which(adapter.host_id))
                catalog.append({
                    "provider": adapter.host_id,
                    "host_name": adapter.display_name,
                    "model_id": def_model,
                    "model_options": options,
                    "is_default": adapter.host_id == "cline",
                    "status": "ready" if bin_installed else "standby",
                })

        for hid, adapter in self._adapters.items():
            if hid not in seen:
                def_model, options = adapter.discover_models()
                bin_installed = bool(shutil.which(adapter.host_id))
                catalog.append({
                    "provider": adapter.host_id,
                    "host_name": adapter.display_name,
                    "model_id": def_model,
                    "model_options": options,
                    "is_default": False,
                    "status": "ready" if bin_installed else "standby",
                })

        return catalog


_GLOBAL_REGISTRY: HostAdapterRegistry | None = None


def get_adapter_registry() -> HostAdapterRegistry:
    """Return the global HostAdapterRegistry singleton."""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = HostAdapterRegistry()
    return _GLOBAL_REGISTRY
