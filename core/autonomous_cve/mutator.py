"""Exploit probe and negative-control mutation generator."""

from __future__ import annotations

from typing import Sequence

from .models import ExploitProbe, VulnerabilityCategory, VulnerableSurface


class ExploitProbeGenerator:
    """Generate targeted exploit mutation payloads and negative controls for vulnerable surfaces."""

    def generate(self, surface: VulnerableSurface) -> ExploitProbe:
        cat = surface.category
        probe_id = f"probe-{surface.surface_id}"

        if cat == VulnerabilityCategory.PATH_TRAVERSAL:
            return ExploitProbe(
                probe_id=probe_id,
                surface_id=surface.surface_id,
                category=cat,
                payload="../../etc/passwd",
                description="Path traversal boundary bypass via relative dot-dot sequences",
                expected_before_crashed=True,
                expected_after_crashed=False,
                negative_controls=["docs/guide.txt", "assets/logo.png", "src/index.html"],
            )

        if cat == VulnerabilityCategory.STACK_OVERFLOW or cat == VulnerabilityCategory.DESERIALIZATION_DOS:
            return ExploitProbe(
                probe_id=probe_id,
                surface_id=surface.surface_id,
                category=cat,
                payload="STACK_OVERFLOW",
                description="Catastrophic nested depth payload triggering JVM or OS stack exhaustion",
                expected_before_crashed=True,
                expected_after_crashed=False,
                negative_controls=['{"name": "Alice", "score": 98}', '{"items": [1, 2, 3, 4]}'],
            )

        if cat == VulnerabilityCategory.REDOS:
            return ExploitProbe(
                probe_id=probe_id,
                surface_id=surface.surface_id,
                category=cat,
                payload="REDOS_TRIGGER",
                description="Catastrophic exponential backtracking trigger for nested quantifiers",
                expected_before_crashed=True,
                expected_after_crashed=False,
                negative_controls=["user/123/profile", "status/ok", "api/v1/ping"],
            )

        if cat == VulnerabilityCategory.BUFFER_OVERFLOW:
            return ExploitProbe(
                probe_id=probe_id,
                surface_id=surface.surface_id,
                category=cat,
                payload="BUFFER_OVERFLOW",
                description="Malformed boundary payload triggering out-of-bounds read or memory corruption",
                expected_before_crashed=True,
                expected_after_crashed=False,
                negative_controls=['{"clean": "data"}', '{"status": 200}'],
            )

        if cat == VulnerabilityCategory.ROUTE_CONFUSION:
            return ExploitProbe(
                probe_id=probe_id,
                surface_id=surface.surface_id,
                category=cat,
                payload="/protected%2fbypass",
                description="URL-encoded path delimiter injection causing unexpected route bypass",
                expected_before_crashed=True,
                expected_after_crashed=False,
                negative_controls=["/api/v1/users", "/health", "/login"],
            )

        # Default fallback logic probe
        return ExploitProbe(
            probe_id=probe_id,
            surface_id=surface.surface_id,
            category=VulnerabilityCategory.GENERIC_LOGIC,
            payload="INVALID_PAYLOAD",
            description="Generic boundary edge-case input",
            expected_before_crashed=True,
            expected_after_crashed=False,
            negative_controls=["VALID_INPUT_A", "VALID_INPUT_B"],
        )
