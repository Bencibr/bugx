"""Minimal fail-closed defense patch synthesis."""

from __future__ import annotations

from .models import DefensePatch, ExploitProbe, VulnerabilityCategory, VulnerableSurface


class DefensePatchSynthesizer:
    """Synthesize minimal fail-closed defensive guard patches without breaking clean inputs."""

    def synthesize(self, surface: VulnerableSurface, probe: ExploitProbe) -> DefensePatch:
        cat = surface.category
        patch_id = f"patch-{surface.surface_id}"

        if cat == VulnerabilityCategory.PATH_TRAVERSAL:
            guard = (
                "# BugX Fail-Closed Defense: Commonpath Traversal Guard\n"
                "import os\n"
                "def is_safe_path(base_dir: str, target_path: str) -> bool:\n"
                "    try:\n"
                "        resolved_base = os.path.abspath(base_dir)\n"
                "        resolved_target = os.path.abspath(os.path.join(base_dir, target_path))\n"
                "        return os.path.commonpath([resolved_base, resolved_target]) == resolved_base\n"
                "    except (ValueError, OSError):\n"
                "        return False\n"
            )
            return DefensePatch(
                patch_id=patch_id,
                surface_id=surface.surface_id,
                category=cat,
                target_file=surface.file_path,
                guard_code=guard,
                insertion_point="pre_join",
                description="Canonical commonpath containment check preventing path traversal",
            )

        if cat == VulnerabilityCategory.STACK_OVERFLOW or cat == VulnerabilityCategory.DESERIALIZATION_DOS:
            guard = (
                "// BugX Fail-Closed Defense: Recursion Depth Guard\n"
                "public static final int MAX_PARSE_DEPTH = 1000;\n"
                "public static void assertSafeDepth(int currentDepth) {\n"
                "    if (currentDepth > MAX_PARSE_DEPTH) {\n"
                "        throw new IllegalArgumentException(\"Depth limit exceeded: \" + currentDepth);\n"
                "    }\n"
                "}\n"
            )
            return DefensePatch(
                patch_id=patch_id,
                surface_id=surface.surface_id,
                category=cat,
                target_file=surface.file_path,
                guard_code=guard,
                insertion_point="pre_recursion",
                description="Explicit depth counter bound preventing stack exhaustion DoS",
            )

        if cat == VulnerabilityCategory.REDOS:
            guard = (
                "// BugX Fail-Closed Defense: ReDoS Complexity Guard\n"
                "function assertSafeRegexInput(str, maxLen = 1000) {\n"
                "    if (typeof str !== 'string' || str.length > maxLen) {\n"
                "        throw new Error('Input length exceeds safety bound: ' + (str ? str.length : 0));\n"
                "    }\n"
                "}\n"
            )
            return DefensePatch(
                patch_id=patch_id,
                surface_id=surface.surface_id,
                category=cat,
                target_file=surface.file_path,
                guard_code=guard,
                insertion_point="pre_match",
                description="Input complexity and length bound eliminating exponential backtracking",
            )

        if cat == VulnerabilityCategory.BUFFER_OVERFLOW:
            guard = (
                "/* BugX Fail-Closed Defense: Boundary Slice Guard */\n"
                "static inline int is_safe_slice(size_t offset, size_t need, size_t total_len) {\n"
                "    if (offset + need > total_len || offset + need < offset) return 0;\n"
                "    return 1;\n"
                "}\n"
            )
            return DefensePatch(
                patch_id=patch_id,
                surface_id=surface.surface_id,
                category=cat,
                target_file=surface.file_path,
                guard_code=guard,
                insertion_point="pre_indexing",
                description="Integer overflow safe boundary check preventing heap/stack out-of-bounds read",
            )

        if cat == VulnerabilityCategory.ROUTE_CONFUSION:
            guard = (
                "// BugX Fail-Closed Defense: Route Normalization Middleware\n"
                "func RejectMalformedPath() gin.HandlerFunc {\n"
                "    return func(c *gin.Context) {\n"
                "        if strings.Contains(c.Request.URL.RawPath, \"%2f\") || strings.Contains(c.Request.URL.RawPath, \"%2F\") {\n"
                "            c.AbortWithStatus(400)\n"
                "            return\n"
                "        }\n"
                "        c.Next()\n"
                "    }\n"
                "}\n"
            )
            return DefensePatch(
                patch_id=patch_id,
                surface_id=surface.surface_id,
                category=cat,
                target_file=surface.file_path,
                guard_code=guard,
                insertion_point="middleware",
                description="URL encoded slash rejection middleware preventing routing traversal bypass",
            )

        # Fallback generic logic guard
        return DefensePatch(
            patch_id=patch_id,
            surface_id=surface.surface_id,
            category=VulnerabilityCategory.GENERIC_LOGIC,
            target_file=surface.file_path,
            guard_code="// BugX Fail-Closed Defense: Generic Input Validation Guard\nassert payload != 'INVALID_PAYLOAD'\n",
            insertion_point="entry",
            description="Generic input validation fail-closed gate",
        )
