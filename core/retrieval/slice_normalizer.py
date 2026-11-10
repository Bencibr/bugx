"""Syntax healing and tiered context assembly for bounded code slices."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import re
import textwrap
from typing import Any, Mapping, Sequence

from .slice_spec import CodeBlockSpec


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IMPORT_LINE_RE = re.compile(r"^(?:from\s+[\w.]+\s+import\s+.+|import\s+.+)")


@dataclass(frozen=True, slots=True)
class NormalizedSliceView:
    """Executable or self-contained view of a code slice."""

    executable_code: str
    wrapper_type: str
    line_offset: int
    original_text: str
    is_valid_ast: bool


class SliceNormalizer:
    """Normalize extracted code slices into self-contained, executable AST forms."""

    @classmethod
    def to_executable(
        cls,
        spec_or_text: CodeBlockSpec | str,
        *,
        kind: str | None = None,
        language: str = "python",
        symbol_name: str | None = None,
        qualified_name: str | None = None,
    ) -> NormalizedSliceView:
        """Return a syntactically valid executable view of a slice if possible.

        Preserves original slice text without mutating the source CodeBlockSpec.
        """
        if isinstance(spec_or_text, CodeBlockSpec):
            text = spec_or_text.display_text
            kind = spec_or_text.kind if kind is None else kind
            language = spec_or_text.language
            symbol_name = spec_or_text.symbol_name if symbol_name is None else symbol_name
            qualified_name = (
                spec_or_text.qualified_name if qualified_name is None else qualified_name
            )
        elif isinstance(spec_or_text, str):
            text = spec_or_text
            kind = kind or "fragment"
        else:
            raise TypeError("spec_or_text must be a CodeBlockSpec or a string")

        lang = language.casefold()

        # Non-Python Language Wrappers (Go, Rust, JS/TS)
        if lang in ("go", "golang"):
            stripped = text.strip()
            if stripped.startswith("package "):
                return NormalizedSliceView(
                    executable_code=text,
                    wrapper_type="plain",
                    line_offset=0,
                    original_text=text,
                    is_valid_ast=True,
                )
            wrapped = f"package main\n\n{text}"
            return NormalizedSliceView(
                executable_code=wrapped,
                wrapper_type="package_wrap",
                line_offset=2,
                original_text=text,
                is_valid_ast=True,
            )

        if lang in ("rust", "rs"):
            stripped = text.strip()
            if kind == "method" or (
                not stripped.startswith((
                    "fn ", "pub fn ", "struct ", "pub struct ", "enum ", "pub enum ",
                    "trait ", "pub trait ", "impl ", "use ", "mod ", "type "
                ))
                and "fn " in stripped
            ):
                struct_name = "_StubHolder"
                if qualified_name and "::" in qualified_name:
                    struct_name = qualified_name.split("::")[0]
                elif symbol_name and not symbol_name.startswith("fn"):
                    struct_name = symbol_name.split("::")[0]
                indented = textwrap.indent(textwrap.dedent(text), "    ")
                wrapped = f"impl {struct_name} {{\n{indented}\n}}"
                return NormalizedSliceView(
                    executable_code=wrapped,
                    wrapper_type="impl_wrap",
                    line_offset=1,
                    original_text=text,
                    is_valid_ast=True,
                )
            return NormalizedSliceView(
                executable_code=text,
                wrapper_type="plain",
                line_offset=0,
                original_text=text,
                is_valid_ast=True,
            )

        if lang in ("javascript", "js", "typescript", "ts"):
            stripped = text.strip()
            if kind == "method":
                class_name = "_StubHolder"
                if qualified_name and "." in qualified_name:
                    candidate = qualified_name.split(".")[0]
                    if _IDENTIFIER_RE.match(candidate):
                        class_name = candidate
                indented = textwrap.indent(textwrap.dedent(text), "    ")
                wrapped = f"class {class_name} {{\n{indented}\n}}"
                return NormalizedSliceView(
                    executable_code=wrapped,
                    wrapper_type="class_stub",
                    line_offset=1,
                    original_text=text,
                    is_valid_ast=True,
                )
            if kind in ("function", "class", "type") and not stripped.startswith(("export ", "import ")):
                wrapped = f"export {stripped}"
                return NormalizedSliceView(
                    executable_code=wrapped,
                    wrapper_type="export_wrap",
                    line_offset=0,
                    original_text=text,
                    is_valid_ast=True,
                )
            return NormalizedSliceView(
                executable_code=text,
                wrapper_type="plain",
                line_offset=0,
                original_text=text,
                is_valid_ast=True,
            )

        if lang != "python":
            return NormalizedSliceView(
                executable_code=text,
                wrapper_type="plain",
                line_offset=0,
                original_text=text,
                is_valid_ast=True,
            )

        # 1. Method-specific Class Stub wrapper
        if kind == "method":
            class_name = "_StubHolder"
            if qualified_name and "." in qualified_name:
                candidate = qualified_name.split(".")[0]
                if _IDENTIFIER_RE.match(candidate):
                    class_name = candidate

            first_non_empty = next(
                (line for line in text.splitlines() if line.strip()), ""
            )
            if first_non_empty.startswith((" ", "\t")):
                wrapped = f"class {class_name}:\n{text}"
            else:
                wrapped = f"class {class_name}:\n{textwrap.indent(text, '    ')}"

            try:
                ast.parse(wrapped)
                return NormalizedSliceView(
                    executable_code=wrapped,
                    wrapper_type="class_stub",
                    line_offset=1,
                    original_text=text,
                    is_valid_ast=True,
                )
            except SyntaxError:
                pass

        # 2. Direct AST validation (for unindented statements/functions/modules)
        first_non_empty = next(
            (line for line in text.splitlines() if line.strip()), ""
        )
        if not first_non_empty.startswith((" ", "\t")):
            try:
                ast.parse(text)
                return NormalizedSliceView(
                    executable_code=text,
                    wrapper_type="plain",
                    line_offset=0,
                    original_text=text,
                    is_valid_ast=True,
                )
            except SyntaxError:
                pass

        # 3. Dedent normalization for indented blocks
        dedented = textwrap.dedent(text)
        try:
            ast.parse(dedented)
            return NormalizedSliceView(
                executable_code=dedented,
                wrapper_type="dedented",
                line_offset=0,
                original_text=text,
                is_valid_ast=True,
            )
        except SyntaxError:
            pass

        # 4. Function Stub wrapper (for function-scoped statements)
        func_wrapped = f"def _stub_exec():\n{textwrap.indent(dedented, '    ')}"
        try:
            ast.parse(func_wrapped)
            return NormalizedSliceView(
                executable_code=func_wrapped,
                wrapper_type="function_stub",
                line_offset=1,
                original_text=text,
                is_valid_ast=True,
            )
        except SyntaxError:
            pass

        # 5. Branch Stub wrapper (for elif / else fragments)
        if dedented.startswith(("elif ", "else:")):
            branch_wrapped = f"if True:\n    pass\n{dedented}"
            try:
                ast.parse(branch_wrapped)
                return NormalizedSliceView(
                    executable_code=branch_wrapped,
                    wrapper_type="branch_stub",
                    line_offset=2,
                    original_text=text,
                    is_valid_ast=True,
                )
            except SyntaxError:
                pass

        # 6. Failed fallback (remains unparsed)
        return NormalizedSliceView(
            executable_code=text,
            wrapper_type="raw",
            line_offset=0,
            original_text=text,
            is_valid_ast=False,
        )

    @classmethod
    def extract_imports(
        cls,
        source: bytes | str,
        *,
        language: str = "python",
        max_lines: int = 50,
    ) -> list[str]:
        """Extract top-level import statements from the beginning of a source file."""
        if isinstance(source, bytes):
            text = source.decode("utf-8", errors="replace")
        elif isinstance(source, str):
            text = source
        else:
            raise TypeError("source must be bytes or string")

        lang = language.casefold()
        imports: list[str] = []
        lines = text.splitlines()[:max_lines]

        if lang == "python":
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if _IMPORT_LINE_RE.match(stripped):
                    imports.append(stripped)
                elif stripped.startswith(("def ", "class ", "@", "if ", "for ", "while ")):
                    break
            return imports

        if lang in ("go", "golang"):
            in_import_block = False
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("//"):
                    continue
                if stripped.startswith("package "):
                    continue
                if stripped == "import (":
                    in_import_block = True
                    imports.append(stripped)
                    continue
                if in_import_block:
                    imports.append(line)
                    if stripped == ")":
                        in_import_block = False
                    continue
                if stripped.startswith("import "):
                    imports.append(stripped)
                elif stripped.startswith(("func ", "type ", "const ", "var ")):
                    break
            return imports

        if lang in ("rust", "rs"):
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
                    continue
                if stripped.startswith(("use ", "pub use ", "extern crate ")):
                    imports.append(stripped)
                elif stripped.startswith((
                    "fn ", "pub fn ", "struct ", "pub struct ", "enum ", "pub enum ",
                    "trait ", "pub trait ", "impl ", "mod ", "type "
                )):
                    break
            return imports

        if lang in ("javascript", "js", "typescript", "ts"):
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
                    continue
                if stripped.startswith("import "):
                    imports.append(stripped)
                elif stripped.startswith(("function ", "class ", "const ", "let ", "var ", "export ")):
                    if not stripped.startswith("export * from") and not stripped.startswith("export {"):
                        break
            return imports

        return []

    @classmethod
    def assemble_context(
        cls,
        spec: CodeBlockSpec,
        *,
        imports: Sequence[str] = (),
        ego_graph: Mapping[str, Any] | None = None,
        include_header: bool = True,
    ) -> str:
        """Assemble Tiered Progressive Context (imports + ego-subgraph + slice)."""
        sections: list[str] = []
        if include_header:
            loc = f"{spec.file}:{spec.start_line}-{spec.end_line}"
            sym = f" ({spec.kind}: {spec.qualified_name or spec.symbol_name or 'anonymous'})"
            sections.append(f"### Code Context: {loc}{sym}")

        if imports:
            lang_label = spec.language if spec.language else "text"
            sections.append("#### File Imports:")
            sections.append(f"```{lang_label}\n" + "\n".join(imports) + "\n```")

        if ego_graph:
            center = ego_graph.get("center_node", "target")
            neighbors = ego_graph.get("neighbors", {})
            callers = neighbors.get("callers") or neighbors.get("incoming") or []
            callees = neighbors.get("callees") or neighbors.get("outgoing") or []
            sections.append(f"#### Local Topology for `{center}`:")
            if callers:
                sections.append("- Callers: " + ", ".join(f"`{c}`" for c in callers))
            if callees:
                sections.append("- Callees: " + ", ".join(f"`{c}`" for c in callees))

        sections.append(f"#### Target Slice ({spec.language}):")
        sections.append(f"```{spec.language}\n{spec.display_text}\n```")

        return "\n\n".join(sections)


__all__ = ["NormalizedSliceView", "SliceNormalizer"]
