#!/usr/bin/env python3
"""BugX WebUI Host Adapters - Base Protocol & Contracts (REQ-01 ~ REQ-03).

Provides abstract interfaces and normalized event representations for multi-host CLI execution.
Each AI host (Codex, Claude, OpenCode, Cline, Pi, ZCode, DSH, BugX) implements this adapter
interface to decouple command-line arguments, environment setup, model probing, and stream
parsing from the core dispatcher.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class HostEvent:
    """Normalized runtime event parsed from host-specific stdout/stderr streams."""
    event_type: str  # "text", "tool_start", "tool_output", "tool_result", "reasoning", "token_usage", "ignore"
    content: str = ""
    tool_id: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_chunk: str = ""
    tool_output: str = ""
    tool_success: bool = True
    tool_duration_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    raw_line: str = ""


class BaseHostAdapter(ABC):
    """Abstract Base Class for an AI CLI Host Adapter Plugin."""

    host_id: str
    display_name: str
    vendor: str
    default_model: str
    supports_reasoning: bool = False
    supported_effort_levels: list[str] = []
    default_effort: str = "medium"
    supported_modes: list[str] = ["build", "accept_edits", "bypass", "plan"]

    def get_effort_capabilities(self, model: str = "") -> tuple[bool, list[str], str]:
        """Return (effort_applicable, effort_levels, default_effort) for the host or specific model."""
        return self.supports_reasoning, list(self.supported_effort_levels), self.default_effort

    @abstractmethod
    def discover_models(self) -> tuple[str, list[dict[str, Any]]]:
        """Probe real local configuration files for available models (Zero Fictional Models)."""
        pass

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        cwd: Path,
        *,
        mode: str = "chat",
        model: str = "",
        reasoning_effort: str = "medium",
        auto_approve: bool = True,
        target_files: list[str] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Construct the CLI command arguments and environment variables for this host."""
        pass

    @abstractmethod
    def parse_stream_line(self, line: str, state: dict[str, Any]) -> list[HostEvent]:
        """Parse a single raw stdout/stderr line into zero or more normalized HostEvent instances.
        
        Lines that are CLI banners, prompt echoes, or internal debug logs should return []
        or a HostEvent with event_type="ignore", preventing UI clutter in chat bubbles.
        """
        pass

    @abstractmethod
    def format_final_reply(self, events: list[HostEvent], raw_lines: list[str]) -> str:
        """Format the final assistant message content (clean markdown without banners/echoes)."""
        pass

    def is_available(self) -> bool:
        """Check if the underlying CLI binary is available on PATH."""
        import shutil
        return bool(shutil.which(self.host_id))
