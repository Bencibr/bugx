"""Data models for Autonomous CVE Hunting and Auto-Patch Synthesis Pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, List, Optional


class VulnerabilityCategory(str, Enum):
    """Recognized high-risk vulnerability patterns for autonomous hunting."""
    PATH_TRAVERSAL = "path_traversal"
    STACK_OVERFLOW = "stack_overflow"
    REDOS = "redos"
    BUFFER_OVERFLOW = "buffer_overflow"
    ROUTE_CONFUSION = "route_confusion"
    DESERIALIZATION_DOS = "deserialization_dos"
    GENERIC_LOGIC = "generic_logic"


@dataclass
class VulnerableSurface:
    """An identified attack surface in target code with vulnerability risk."""
    surface_id: str
    file_path: str
    language: str
    line_start: int
    line_end: int
    symbol_name: str
    category: VulnerabilityCategory
    risk_score: float  # 0.0 to 1.0
    reason: str
    matched_snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["category"] = self.category.value
        return data


@dataclass
class ExploitProbe:
    """A generated exploit probe with positive attack vector and negative control."""
    probe_id: str
    surface_id: str
    category: VulnerabilityCategory
    payload: str
    description: str
    expected_before_crashed: bool = True
    expected_after_crashed: bool = False
    negative_controls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["category"] = self.category.value
        return data


@dataclass
class DefensePatch:
    """A synthesized minimal fail-closed defensive guard patch."""
    patch_id: str
    surface_id: str
    category: VulnerabilityCategory
    target_file: str
    guard_code: str
    insertion_point: str  # "function_entry", "pre_loop", "wrapper"
    description: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["category"] = self.category.value
        return data


@dataclass
class HuntReceipt:
    """Structured, verifiable audit receipt for an autonomous hunting run."""
    schema_version: str
    target_root: str
    status: str  # "PASSED", "FAILED", "BLOCKED"
    surfaces_scouted: int
    exploits_tested: int
    patches_verified: int
    negative_control_pass_rate: float
    sha256_hash: str
    timestamp: float
    iso_time: str
    surfaces: list[dict[str, Any]] = field(default_factory=list)
    cases: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
