"""BugX WebUI Host Adapters Plugin Suite."""

from .base import BaseHostAdapter, HostEvent
from .bugx import BugxHostAdapter
from .claude import ClaudeHostAdapter
from .cline import ClineHostAdapter
from .codex import CodexHostAdapter
from .dsh import DshHostAdapter
from .opencode import OpenCodeHostAdapter
from .pi import PiHostAdapter
from .registry import HostAdapterRegistry, get_adapter_registry
from .zcode import ZCodeHostAdapter

__all__ = [
    "BaseHostAdapter",
    "HostEvent",
    "HostAdapterRegistry",
    "get_adapter_registry",
    "CodexHostAdapter",
    "ClaudeHostAdapter",
    "OpenCodeHostAdapter",
    "ClineHostAdapter",
    "PiHostAdapter",
    "BugxHostAdapter",
    "ZCodeHostAdapter",
    "DshHostAdapter",
]
