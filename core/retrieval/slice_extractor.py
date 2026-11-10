"""Bounded source slicing using Python AST, supplied Tree-sitter trees, or fallback lexing.

Tree-sitter stays an optional integration point: callers may pass a parsed
tree or parser object, while this module itself depends only on Python's
standard library.
"""

from __future__ import annotations

import ast
import bisect
import codecs
import hashlib
import importlib.metadata
import io
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Sequence

from .slice_spec import CodeBlockSpec, ErrorRange, slice_id_for


class SliceExtractionError(ValueError):
    """Raised when an input exceeds a declared extraction boundary."""


@dataclass(slots=True)
class _Draft:
    start_byte: int
    end_byte: int
    symbol_name: str | None
    qualified_name: str | None
    kind: str
    signature: str | None = None
    parent_qname: str | None = None


class _SourceText:
    """Decoded source plus exact character-to-original-byte position mapping."""

    def __init__(self, raw: bytes, encoding: str) -> None:
        self.raw = raw
        self.encoding = encoding
        try:
            self.text = raw.decode(encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            raise SliceExtractionError(f"source cannot be decoded as {encoding}") from exc
        codec = codecs.lookup(encoding).name
        if codec == "utf-8-sig":
            codec = "utf-8"
            bom = b"\xef\xbb\xbf"
            initial = len(bom) if raw.startswith(bom) else 0
        else:
            initial = 0
        encoded = self.text.encode(codec)
        if raw[initial:] != encoded:
            raise SliceExtractionError(
                f"encoding {encoding} does not provide stable source byte positions"
            )
        self.char_to_byte: list[int] = [initial]
        offset = initial
        for char in self.text:
            offset += len(char.encode(codec))
            self.char_to_byte.append(offset)
        if offset != len(raw):
            raise SliceExtractionError("decoded source does not map exactly to original bytes")
        self.lines = _physical_text_lines(self.text)
        self.line_char_starts: list[int] = []
        char_offset = 0
        for line in self.lines:
            self.line_char_starts.append(char_offset)
            char_offset += len(line)

    def byte_for_char(self, char_offset: int) -> int:
        if char_offset < 0 or char_offset >= len(self.char_to_byte):
            raise SliceExtractionError("character position lies outside source")
        return self.char_to_byte[char_offset]

    def byte_for_ast(self, line_number: int, utf8_column: int) -> int:
        if line_number < 1 or line_number > len(self.lines):
            raise SliceExtractionError("AST line position lies outside source")
        if utf8_column < 0:
            raise SliceExtractionError("AST column cannot be negative")
        line = self.lines[line_number - 1]
        utf8_offset = 0
        char_column = 0
        for char in line:
            if utf8_offset == utf8_column:
                break
            next_offset = utf8_offset + len(char.encode("utf-8"))
            if next_offset > utf8_column:
                raise SliceExtractionError("AST column splits a UTF-8 character")
            utf8_offset = next_offset
            char_column += 1
        if utf8_offset != utf8_column:
            raise SliceExtractionError("AST column lies outside source line")
        return self.byte_for_char(self.line_char_starts[line_number - 1] + char_column)

    def line_for_byte(self, byte_offset: int) -> int:
        return _line_for_byte(self.raw, byte_offset)


_EXTENSION_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".rs": "rust",
    ".go": "go",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".php": "php",
    ".rb": "ruby",
    ".sh": "shell",
    ".bash": "shell",
}

_TREE_FUNCTION_NODES = frozenset({
    "function_definition", "function_declaration", "function_item",
    "method_definition", "method_declaration", "function_expression",
    "arrow_function", "lambda_expression", "local_function_statement",
    "constructor_declaration", "function",
})
_TREE_CLASS_NODES = frozenset({
    "class_definition", "class_declaration", "class_specifier",
    "class_definition_item", "object_declaration", "class",
})
_TREE_TYPE_NODES = frozenset({
    "interface_declaration", "enum_declaration", "type_alias_declaration",
    "type_declaration", "type_definition", "struct_item", "trait_item",
    "union_specifier",
})
_TREE_MODULE_NODES = frozenset({
    "namespace_definition", "module_declaration", "module_definition",
})
_NAME_NODE_TYPES = frozenset({
    "identifier", "type_identifier", "field_identifier", "property_identifier",
    "namespace_identifier", "constant",
})
_DECLARATION_NAME_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def _canonical_file(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("file must be a non-empty trimmed path")
    if len(value.encode("utf-8")) > 4096:
        raise ValueError("file path exceeds the v1 limit of 4096 UTF-8 bytes")
    if (
        "\\" in value
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or PureWindowsPath(value).drive
        or ":" in value
    ):
        raise ValueError("file must be a canonical relative POSIX path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("file must not contain empty, dot, or parent path segments")
    if str(PurePosixPath(value)) != value:
        raise ValueError("file must be normalized")
    return value


def _language_for(file: str, supplied: str | None) -> str:
    if supplied is not None:
        if not isinstance(supplied, str) or not supplied.strip():
            raise ValueError("language must be a non-empty string")
        return supplied.strip().lower().replace("_", "-")
    suffix = PurePosixPath(file).suffix.lower()
    return _EXTENSION_LANGUAGES.get(suffix, "unknown")


def _python_encoding(raw: bytes, explicit: str | None) -> str:
    if explicit:
        return explicit
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        return encoding
    except (SyntaxError, LookupError):
        return "utf-8"


def _line_end_without_newline(text: str, end: int) -> int:
    while end > 0 and text[end - 1] in "\r\n":
        end -= 1
    return end


def _physical_text_lines(text: str) -> list[str]:
    lines: list[str] = []
    start = 0
    for match in re.finditer(r"\r\n|\r|\n", text):
        lines.append(text[start:match.end()])
        start = match.end()
    if start < len(text):
        lines.append(text[start:])
    return lines


def _physical_byte_lines(raw: bytes) -> list[bytes]:
    lines: list[bytes] = []
    start = 0
    for match in re.finditer(b"\r\n|\r|\n", raw):
        lines.append(raw[start:match.end()])
        start = match.end()
    if start < len(raw):
        lines.append(raw[start:])
    return lines


def _line_number(raw: bytes, byte_offset: int) -> int:
    prefix = raw[:byte_offset]
    return prefix.count(b"\n") + prefix.count(b"\r") - prefix.count(b"\r\n") + 1


def _line_for_byte(raw: bytes, byte_offset: int) -> int:
    if (
        0 < byte_offset < len(raw)
        and raw[byte_offset:byte_offset + 1] == b"\n"
        and raw[byte_offset - 1:byte_offset] == b"\r"
    ):
        byte_offset -= 1
    return _line_number(raw, byte_offset)


def _inclusive_end_line(raw: bytes, end_byte: int) -> int:
    last = end_byte - 1
    if (
        last > 0
        and raw[last:last + 1] == b"\n"
        and raw[last - 1:last] == b"\r"
    ):
        last -= 1
    return _line_number(raw, last)


def _error_range(raw: bytes, start: int, end: int, message: str) -> ErrorRange | None:
    if not raw:
        return None
    start = max(0, min(start, len(raw) - 1))
    end = max(start + 1, min(end, len(raw)))
    return ErrorRange(
        start_byte=start,
        end_byte=end,
        start_line=_line_for_byte(raw, start),
        end_line=_inclusive_end_line(raw, end),
        message=message[:500] or "parser error",
    )


def _installed_tree_sitter_version() -> str:
    try:
        return importlib.metadata.version("tree-sitter")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


class SliceExtractor:
    """Extract immutable, byte-verified code slices from one source file."""

    def __init__(
        self,
        project_id: str,
        snapshot_digest: str,
        generation: int,
        *,
        display_max_bytes: int = 8192,
        max_fragment_bytes: int | None = None,
        max_source_bytes: int = 2 * 1024 * 1024,
        max_blocks: int = 10000,
        max_tree_depth: int = 128,
        max_tree_nodes: int = 50000,
    ) -> None:
        if not isinstance(project_id, str) or not project_id or project_id != project_id.strip():
            raise ValueError("project_id must be a non-empty trimmed string")
        if not isinstance(snapshot_digest, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", snapshot_digest
        ):
            raise ValueError("snapshot_digest must be a 64-character hexadecimal digest")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ValueError("generation must be an integer >= 1")
        if max_fragment_bytes is None:
            max_fragment_bytes = display_max_bytes
        for name, limit, minimum in (
            ("display_max_bytes", display_max_bytes, 1),
            ("max_fragment_bytes", max_fragment_bytes, 1),
            ("max_source_bytes", max_source_bytes, 1),
            ("max_blocks", max_blocks, 1),
            ("max_tree_depth", max_tree_depth, 1),
            ("max_tree_nodes", max_tree_nodes, 1),
        ):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        self.project_id = project_id
        self.snapshot_digest = snapshot_digest.lower()
        self.generation = generation
        self.display_max_bytes = display_max_bytes
        self.max_fragment_bytes = max_fragment_bytes
        self.max_source_bytes = max_source_bytes
        self.max_blocks = max_blocks
        self.max_tree_depth = max_tree_depth
        self.max_tree_nodes = max_tree_nodes

    def extract(
        self,
        source: bytes | str,
        *,
        file: str,
        language: str | None = None,
        encoding: str | None = None,
        tree_sitter_tree: object | None = None,
        tree_sitter_parser: object | None = None,
        parser_version: str | None = None,
        grammar_version: str | None = None,
    ) -> tuple[CodeBlockSpec, ...]:
        """Extract structured blocks while preserving source offsets and digests.

        For Tree-sitter, pass either a parsed tree or a parser with a
        parse(bytes) method. This avoids making the optional package a runtime
        dependency.
        """
        file = _canonical_file(file)
        language = _language_for(file, language)
        if tree_sitter_tree is not None and tree_sitter_parser is not None:
            raise ValueError("pass a Tree-sitter tree or parser, not both")
        if isinstance(source, str):
            actual_encoding = encoding or "utf-8"
            if len(source) > self.max_source_bytes:
                raise SliceExtractionError("source text exceeds configured byte limit")
            try:
                raw = source.encode(actual_encoding)
            except (LookupError, UnicodeEncodeError) as exc:
                raise SliceExtractionError(f"source cannot be encoded as {actual_encoding}") from exc
        elif isinstance(source, bytes):
            raw = source
            actual_encoding = _python_encoding(raw, encoding) if language == "python" else (
                encoding or "utf-8"
            )
        else:
            raise TypeError("source must be bytes or str")
        if len(raw) > self.max_source_bytes:
            raise SliceExtractionError(
                f"source is {len(raw)} bytes; limit is {self.max_source_bytes}"
            )
        if not raw:
            return ()
        text_source = _SourceText(raw, actual_encoding)

        if language == "python":
            try:
                module = ast.parse(text_source.text, filename=file, type_comments=True)
                drafts = self._python_ast_drafts(module, text_source)
                if not drafts:
                    drafts = [_Draft(0, len(raw), None, None, "module")]
                version = parser_version or (
                    f"cpython-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
                )
                return self._emit(
                    drafts,
                    raw,
                    text_source,
                    file=file,
                    language=language,
                    parser="python-ast",
                    parser_version=version,
                    grammar_version=None,
                    quality="ast",
                    parse_status="ok",
                    error_ranges=(),
                )
            except SliceExtractionError:
                raise
            except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
                syntax_errors = self._python_syntax_errors(exc, raw)
                drafts, fallback_errors = self._python_indented_drafts(text_source)
                errors = self._merge_errors(syntax_errors, fallback_errors)
                if not drafts:
                    drafts = [_Draft(0, len(raw), None, None, "module")]
                return self._emit(
                    drafts,
                    raw,
                    text_source,
                    file=file,
                    language=language,
                    parser="delimited-fallback",
                    parser_version=parser_version or "stdlib-indentation-1",
                    grammar_version=None,
                    quality="heuristic",
                    parse_status="failed",
                    error_ranges=errors,
                )

        tree = tree_sitter_tree
        tree_parser = tree_sitter_parser
        tree_error: ErrorRange | None = None
        if tree is None and tree_parser is not None:
            try:
                parse_method = getattr(tree_parser, "parse", None)
                if not callable(parse_method):
                    raise TypeError("Tree-sitter parser must expose parse(bytes)")
                tree = parse_method(raw)
            except Exception as exc:
                tree_error = _error_range(raw, 0, len(raw), f"Tree-sitter parse failed: {exc}")
        if tree is not None:
            try:
                drafts, errors, has_error = self._tree_sitter_drafts(
                    tree, raw, text_source
                )
                if not drafts:
                    drafts = [_Draft(0, len(raw), None, None, "module")]
                errors = self._merge_errors(errors, (tree_error,) if tree_error else ())
                return self._emit(
                    drafts,
                    raw,
                    text_source,
                    file=file,
                    language=language,
                    parser="tree-sitter",
                    parser_version=parser_version or self._tree_sitter_version(tree_parser, tree),
                    grammar_version=grammar_version,
                    quality="recovered" if has_error or errors else "ast",
                    parse_status="partial" if has_error or errors else "ok",
                    error_ranges=errors,
                )
            except SliceExtractionError:
                raise
            except (AttributeError, TypeError, ValueError, RecursionError) as exc:
                tree_error = _error_range(raw, 0, len(raw), f"Tree-sitter inspection failed: {exc}")

        drafts, fallback_errors = self._delimited_drafts(text_source, language)
        if not drafts:
            drafts = [_Draft(0, len(raw), None, None, "module")]
        errors = self._merge_errors(fallback_errors, (tree_error,) if tree_error else ())
        return self._emit(
            drafts,
            raw,
            text_source,
            file=file,
            language=language,
            parser="delimited-fallback",
            parser_version=parser_version or "stdlib-delimited-1",
            grammar_version=None,
            quality="heuristic",
            parse_status="failed" if tree_error else "partial",
            error_ranges=errors,
        )

    @staticmethod
    def _tree_sitter_version(parser: object | None, tree: object) -> str:
        version = getattr(parser, "version", None) if parser is not None else None
        if isinstance(version, str) and version.strip():
            return version.strip()
        version = getattr(tree, "version", None)
        if isinstance(version, str) and version.strip():
            return version.strip()
        return _installed_tree_sitter_version()

    @staticmethod
    def _merge_errors(*groups: Iterable[ErrorRange | None]) -> tuple[ErrorRange, ...]:
        seen: set[tuple[int, int, str]] = set()
        merged: list[ErrorRange] = []
        for group in groups:
            for error in group:
                if error is None:
                    continue
                key = (error.start_byte, error.end_byte, error.message)
                if key not in seen:
                    seen.add(key)
                    merged.append(error)
        return tuple(sorted(merged, key=lambda item: (item.start_byte, item.end_byte))[:256])

    def _python_ast_drafts(self, module: ast.Module, source: _SourceText) -> list[_Draft]:
        drafts: list[_Draft] = []
        stack: list[tuple[ast.AST, tuple[str, ...], tuple[str, ...], int]] = [
            (node, (), (), 0) for node in reversed(module.body)
        ]
        visited = 0
        while stack:
            node, scopes, class_scopes, depth = stack.pop()
            visited += 1
            if visited > self.max_tree_nodes:
                raise SliceExtractionError("Python AST node count exceeds configured limit")
            if depth > self.max_tree_depth:
                raise SliceExtractionError("Python AST nesting exceeds configured limit")

            is_class = isinstance(node, ast.ClassDef)
            is_function = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            is_type_alias = type(node).__name__ == "TypeAlias"
            if is_class or is_function or is_type_alias:
                if is_type_alias:
                    raw_name = getattr(node, "name", None)
                    name = getattr(raw_name, "id", None) or getattr(raw_name, "name", None)
                else:
                    name = getattr(node, "name", None)
                if not isinstance(name, str) or not name:
                    name = None
                lineno = int(getattr(node, "lineno", 1))
                col = int(getattr(node, "col_offset", 0))
                decorators = getattr(node, "decorator_list", ())
                if decorators:
                    lineno = min(int(getattr(item, "lineno", lineno)) for item in decorators)
                    col = 0
                end_lineno = int(getattr(node, "end_lineno", lineno))
                end_col = getattr(node, "end_col_offset", None)
                if end_col is None:
                    line_text = source.lines[end_lineno - 1]
                    end_col = len(line_text.rstrip("\r\n").encode("utf-8"))
                start_byte = source.byte_for_ast(lineno, col)
                end_byte = source.byte_for_ast(end_lineno, int(end_col))
                if end_byte > start_byte:
                    parent_qname = ".".join(scopes) or None
                    qname = ".".join((*scopes, name)) if name else None
                    kind = (
                        "class" if is_class else
                        "type" if is_type_alias else
                        "method" if class_scopes else
                        "function"
                    )
                    signature = self._python_signature(source.text, node)
                    drafts.append(_Draft(
                        start_byte,
                        end_byte,
                        name,
                        qname,
                        kind,
                        signature,
                        parent_qname,
                    ))
                if is_class or is_function:
                    child_scopes = (*scopes, name) if name else scopes
                    child_classes = (*class_scopes, name) if is_class and name else class_scopes
                    for child in reversed(getattr(node, "body", ())):
                        stack.append((child, child_scopes, child_classes, depth + 1))
                continue

            for child in reversed(list(ast.iter_child_nodes(node))):
                stack.append((child, scopes, class_scopes, depth + 1))
        return drafts

    @staticmethod
    def _python_signature(source_text: str, node: ast.AST) -> str | None:
        segment = ast.get_source_segment(source_text, node, padded=False)
        if not segment:
            return None
        try:
            tokens = tokenize.generate_tokens(io.StringIO(segment).readline)
            depth = 0
            for token in tokens:
                if token.type != tokenize.OP:
                    continue
                if token.string in "([{":
                    depth += 1
                elif token.string in ")]}":
                    depth = max(0, depth - 1)
                elif token.string == ":" and depth == 0:
                    lines = segment.splitlines(keepends=True)
                    end = sum(len(line) for line in lines[:token.end[0] - 1]) + token.end[1]
                    return segment[:end].strip()
        except (tokenize.TokenError, IndentationError):
            return None
        return None

    @staticmethod
    def _python_syntax_errors(error: BaseException, raw: bytes) -> tuple[ErrorRange, ...]:
        if not isinstance(error, SyntaxError):
            item = _error_range(raw, 0, len(raw), f"Python AST failed: {error}")
            return (item,) if item else ()
        line_number = max(1, int(error.lineno or 1))
        lines = _physical_byte_lines(raw)
        start = sum(len(line) for line in lines[:line_number - 1])
        if start >= len(raw):
            start = max(0, len(raw) - 1)
        line_end = start
        while line_end < len(raw) and raw[line_end:line_end + 1] not in {b"\n", b"\r"}:
            line_end += 1
        if line_end == start:
            line_end = min(len(raw), start + 1)
        item = _error_range(raw, start, line_end, error.msg or "Python syntax error")
        return (item,) if item else ()

    def _tree_sitter_drafts(
        self, tree: object, raw: bytes, source: _SourceText
    ) -> tuple[list[_Draft], tuple[ErrorRange, ...], bool]:
        root = getattr(tree, "root_node", tree)
        if root is None:
            raise TypeError("Tree-sitter result has no root node")
        root_error = bool(getattr(root, "has_error", False))
        drafts: list[_Draft] = []
        errors: list[ErrorRange] = []
        stack: list[tuple[object, tuple[str, ...], tuple[str, ...], int]] = [
            (root, (), (), 0)
        ]
        visited = 0
        while stack:
            node, scopes, class_scopes, depth = stack.pop()
            visited += 1
            if visited > self.max_tree_nodes:
                raise SliceExtractionError("Tree-sitter node count exceeds configured limit")
            if depth > self.max_tree_depth:
                raise SliceExtractionError("Tree-sitter nesting exceeds configured limit")
            node_type = str(getattr(node, "type", ""))
            start = int(getattr(node, "start_byte", 0))
            end = int(getattr(node, "end_byte", start))
            is_error = node_type == "ERROR" or bool(getattr(node, "is_error", False))
            is_missing = bool(getattr(node, "is_missing", False))
            if is_error or is_missing:
                error = _error_range(
                    raw,
                    start,
                    end,
                    "Tree-sitter missing syntax node" if is_missing else "Tree-sitter ERROR node",
                )
                if error:
                    errors.append(error)

            mapped_kind: str | None = None
            if node_type in _TREE_FUNCTION_NODES:
                mapped_kind = "function"
            elif node_type in _TREE_CLASS_NODES:
                mapped_kind = "class"
            elif node_type in _TREE_TYPE_NODES:
                mapped_kind = "type"
            elif node_type in _TREE_MODULE_NODES:
                mapped_kind = "module"
            name = self._tree_node_name(node, raw) if mapped_kind else None
            parent_qname = ".".join(scopes) or None
            qualified = ".".join((*scopes, name)) if name else None
            actual_kind = "method" if mapped_kind == "function" and class_scopes else mapped_kind
            if mapped_kind and 0 <= start < end <= len(raw):
                signature = self._tree_signature(raw[start:end], source.encoding)
                drafts.append(_Draft(
                    start,
                    end,
                    name,
                    qualified,
                    actual_kind or mapped_kind,
                    signature,
                    parent_qname,
                ))
            next_scopes = (*scopes, name) if mapped_kind in {"class", "module"} and name else scopes
            next_classes = (*class_scopes, name) if mapped_kind == "class" and name else class_scopes
            children = getattr(node, "named_children", None)
            if children is None:
                children = getattr(node, "children", ())
            if not isinstance(children, Sequence):
                children = tuple(children)
            for child in reversed(children):
                stack.append((child, next_scopes, next_classes, depth + 1))

        if root_error and not errors:
            whole = _error_range(raw, 0, len(raw), "Tree-sitter tree reports syntax errors")
            if whole:
                errors.append(whole)
        return drafts, tuple(errors[:256]), root_error or bool(errors)

    @staticmethod
    def _tree_node_name(node: object, raw: bytes) -> str | None:
        field_method = getattr(node, "child_by_field_name", None)
        candidate = None
        if callable(field_method):
            for field_name in ("name", "declarator", "identifier"):
                try:
                    candidate = field_method(field_name)
                except Exception:
                    candidate = None
                if candidate is not None:
                    break
        if candidate is None:
            pending = list(reversed(getattr(node, "named_children", ()) or ()))
            depth = 0
            while pending and depth < 256:
                child = pending.pop()
                depth += 1
                if str(getattr(child, "type", "")) in _NAME_NODE_TYPES:
                    candidate = child
                    break
                pending.extend(reversed(getattr(child, "named_children", ()) or ()))
        if candidate is None:
            return None
        text = getattr(candidate, "text", None)
        if isinstance(text, bytes):
            name = text.decode("utf-8", errors="replace")
        elif isinstance(text, str):
            name = text
        else:
            start = int(getattr(candidate, "start_byte", 0))
            end = int(getattr(candidate, "end_byte", start))
            name = raw[start:end].decode("utf-8", errors="replace")
        match = _DECLARATION_NAME_RE.search(name)
        return match.group(0) if match else None

    @staticmethod
    def _tree_signature(raw: bytes, encoding: str) -> str | None:
        text = raw.decode(encoding, errors="replace").strip()
        if not text:
            return None
        brace = text.find("{")
        if brace >= 0:
            text = text[:brace + 1]
        return text[:512].strip() or None

    def _python_indented_drafts(
        self, source: _SourceText
    ) -> tuple[list[_Draft], tuple[ErrorRange, ...]]:
        pattern = re.compile(
            r"^(?P<indent>[ \t]*)(?:(?:async[ \t]+)?def[ \t]+(?P<function>[A-Za-z_]\w*)"
            r"|class[ \t]+(?P<class>[A-Za-z_]\w*))\b"
        )
        lines = source.lines
        char_starts = source.line_char_starts
        drafts: list[_Draft] = []
        for index, line in enumerate(lines):
            match = pattern.match(line)
            if not match:
                continue
            indent_text = match.group("indent")
            indent = len(indent_text.expandtabs(8))
            start_line = index
            while start_line > 0:
                previous = lines[start_line - 1]
                if not previous.lstrip().startswith("@"):
                    break
                previous_indent = len(previous) - len(previous.lstrip(" \t"))
                if len(previous[:previous_indent].expandtabs(8)) != indent:
                    break
                start_line -= 1
            last_content_line = index
            for next_index in range(index + 1, len(lines)):
                candidate = lines[next_index]
                stripped = candidate.strip()
                if not stripped:
                    continue
                candidate_indent_text = candidate[:len(candidate) - len(candidate.lstrip(" \t"))]
                candidate_indent = len(candidate_indent_text.expandtabs(8))
                if candidate_indent <= indent:
                    break
                last_content_line = next_index
            start_char = char_starts[start_line]
            end_line = lines[last_content_line]
            end_char = char_starts[last_content_line] + _line_end_without_newline(
                end_line, len(end_line)
            )
            if end_char <= start_char:
                continue
            name = match.group("function") or match.group("class")
            kind = "class" if match.group("class") else "function"
            signature = line[match.start():_line_end_without_newline(line, len(line))].strip()
            drafts.append(_Draft(
                source.byte_for_char(start_char),
                source.byte_for_char(end_char),
                name,
                name,
                kind,
                signature or None,
            ))
        return self._qualify_fallback_drafts(drafts), ()

    def _delimited_drafts(
        self, source: _SourceText, language: str
    ) -> tuple[list[_Draft], tuple[ErrorRange, ...]]:
        if language == "python":
            return self._python_indented_drafts(source)
        text = source.text
        pairs, unmatched = self._brace_pairs(text, language)
        drafts: list[_Draft] = []
        for open_char, close_char in sorted(pairs.items()):
            boundary = max(
                text.rfind(";", 0, open_char),
                text.rfind("{", 0, open_char),
                text.rfind("}", 0, open_char),
            ) + 1
            prefix = text[boundary:open_char]
            leading = len(prefix) - len(prefix.lstrip())
            header = prefix.strip()
            if not header:
                continue
            declaration = self._class_header(header)
            kind: str | None = None
            name: str | None = None
            signature: str | None = None
            start_char = boundary + leading
            if declaration:
                kind, name, relative_start = declaration
                start_char = boundary + leading + relative_start
                signature = text[start_char:open_char + 1].strip()[:512] or None
            else:
                function = self._function_header(header)
                if function:
                    name, relative_start = function
                    start_char = boundary + leading + relative_start
                    kind = "function"
                    signature = text[start_char:open_char + 1].strip()[:512] or None
                else:
                    method = self._method_header(header)
                    if method:
                        name, relative_start = method
                        start_char = boundary + leading + relative_start
                        kind = "function"
                        signature = text[start_char:open_char + 1].strip()[:512] or None
            if kind is None:
                continue
            end_char = close_char + 1
            if kind == "type":
                after = end_char
                while after < len(text) and text[after].isspace() and text[after] != "\n":
                    after += 1
                if after < len(text) and text[after] == ";":
                    end_char = after + 1
            start_byte = source.byte_for_char(start_char)
            end_byte = source.byte_for_char(end_char)
            if end_byte > start_byte:
                drafts.append(_Draft(
                    start_byte,
                    end_byte,
                    name,
                    name,
                    kind,
                    signature,
                ))
        errors = [
            item
            for start, end, message in unmatched
            if (item := _error_range(
                source.raw,
                source.byte_for_char(start),
                source.byte_for_char(end),
                message,
            )) is not None
        ]
        return self._qualify_fallback_drafts(drafts), tuple(errors[:256])

    @staticmethod
    def _class_header(header: str) -> tuple[str, str | None, int] | None:
        pattern = re.compile(
            r"\b(?P<keyword>class|struct|interface|enum|trait|impl|namespace|module)\b"
            r"(?:\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*))?"
        )
        match = pattern.search(header)
        if not match:
            return None
        keyword = match.group("keyword")
        kind = "module" if keyword in {"namespace", "module"} else (
            "class" if keyword in {"class", "impl"} else "type"
        )
        return kind, match.group("name"), match.start("keyword")

    @staticmethod
    def _function_header(header: str) -> tuple[str | None, int] | None:
        explicit = re.search(
            r"\b(?P<keyword>function|fn|func|fun)\s*\*?\s*(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)?",
            header,
        )
        if explicit:
            return explicit.group("name"), explicit.start("keyword")
        arrow = re.search(
            r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*="
            r"\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>",
            header,
        )
        if arrow:
            return arrow.group("name"), arrow.start()
        return None

    @staticmethod
    def _method_header(header: str) -> tuple[str, int] | None:
        pattern = re.compile(
            r"(?P<name>[A-Za-z_$~][A-Za-z0-9_$~]*)\s*\([^{};]*\)\s*"
            r"(?:const\s*)?(?:noexcept\s*)?(?:->\s*[^{}]+)?$"
        )
        match = pattern.search(header)
        if not match:
            return None
        name = match.group("name")
        if name in {
            "if", "for", "while", "switch", "catch", "with", "synchronized",
            "return", "new", "throw", "sizeof", "typeof", "do",
        }:
            return None
        return name, match.start("name")

    @staticmethod
    def _qualify_fallback_drafts(drafts: list[_Draft]) -> list[_Draft]:
        ordered = sorted(drafts, key=lambda item: (item.start_byte, -item.end_byte))
        qualified: list[_Draft] = []
        for draft in ordered:
            containers = [
                item for item in qualified
                if item.kind != "fragment"
                and item.start_byte <= draft.start_byte
                and item.end_byte >= draft.end_byte
                and (item.start_byte, item.end_byte) != (draft.start_byte, draft.end_byte)
            ]
            parent = min(
                containers,
                key=lambda item: (item.end_byte - item.start_byte, -item.start_byte),
                default=None,
            )
            if parent is not None and parent.qualified_name:
                draft.parent_qname = parent.qualified_name
                if draft.symbol_name:
                    draft.qualified_name = f"{parent.qualified_name}.{draft.symbol_name}"
                if draft.kind == "function" and parent.kind == "class":
                    draft.kind = "method"
            else:
                draft.parent_qname = None
                draft.qualified_name = draft.symbol_name
            qualified.append(draft)
        return qualified

    @staticmethod
    def _brace_pairs(
        text: str, language: str
    ) -> tuple[dict[int, int], list[tuple[int, int, str]]]:
        slash_comments = language not in {"shell", "ruby", "unknown"}
        hash_comments = language in {"shell", "ruby", "python", "unknown", "php"}
        backticks_are_strings = language in {
            "javascript", "typescript", "java", "c", "cpp", "go", "rust",
            "csharp", "kotlin", "swift", "php",
        }
        stack: list[int] = []
        pairs: dict[int, int] = {}
        errors: list[tuple[int, int, str]] = []
        index = 0
        state = "normal"
        state_start = 0
        quote = ""
        escaped = False
        while index < len(text):
            char = text[index]
            next_char = text[index + 1] if index + 1 < len(text) else ""
            if state == "line_comment":
                if char in "\r\n":
                    state = "normal"
                index += 1
                continue
            if state == "block_comment":
                if char == "*" and next_char == "/":
                    state = "normal"
                    index += 2
                else:
                    index += 1
                continue
            if state == "string":
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    state = "normal"
                index += 1
                continue
            if slash_comments and char == "/" and next_char == "/":
                state = "line_comment"
                state_start = index
                index += 2
                continue
            if slash_comments and char == "/" and next_char == "*":
                state = "block_comment"
                state_start = index
                index += 2
                continue
            if hash_comments and char == "#":
                state = "line_comment"
                state_start = index
                index += 1
                continue
            if char in {"'", '"'} or (char == chr(96) and backticks_are_strings):
                quote = char
                state = "string"
                state_start = index
                escaped = False
                index += 1
                continue
            if char == "{":
                stack.append(index)
            elif char == "}":
                if stack:
                    pairs[stack.pop()] = index
                else:
                    errors.append((index, index + 1, "unmatched closing brace"))
            index += 1
        if state == "string":
            errors.append((state_start, len(text), "unclosed string literal"))
        elif state == "block_comment":
            errors.append((state_start, len(text), "unclosed block comment"))
        for start in stack:
            errors.append((start, len(text), "unclosed brace"))
        return pairs, errors

    def _emit(
        self,
        drafts: Iterable[_Draft],
        raw: bytes,
        source: _SourceText,
        *,
        file: str,
        language: str,
        parser: str,
        parser_version: str,
        grammar_version: str | None,
        quality: str,
        parse_status: str,
        error_ranges: Iterable[ErrorRange],
    ) -> tuple[CodeBlockSpec, ...]:
        if not isinstance(parser_version, str) or not parser_version.strip():
            raise ValueError("parser_version must be a non-empty string")
        unique: dict[tuple[int, int, str, str | None], _Draft] = {}
        for draft in drafts:
            if not (0 <= draft.start_byte < draft.end_byte <= len(raw)):
                continue
            key = (
                draft.start_byte,
                draft.end_byte,
                draft.kind,
                draft.qualified_name,
            )
            unique.setdefault(key, draft)
        ordered = sorted(
            unique.values(),
            key=lambda item: (item.start_byte, -item.end_byte, item.kind, item.qualified_name or ""),
        )
        errors = tuple(error_ranges)
        specs: list[CodeBlockSpec] = []
        for draft in ordered:
            base = self._make_spec(
                raw,
                source,
                file=file,
                language=language,
                start_byte=draft.start_byte,
                end_byte=draft.end_byte,
                symbol_name=draft.symbol_name,
                qualified_name=draft.qualified_name,
                kind=draft.kind,
                signature=draft.signature,
                parser=parser,
                parser_version=parser_version,
                grammar_version=grammar_version,
                quality=quality,
                parse_status=parse_status,
                error_ranges=errors,
                parent_slice_id=None,
            )
            specs.append(base)
            if draft.end_byte - draft.start_byte > self.max_fragment_bytes:
                for start, end in self._fragment_ranges(
                    raw, source, draft.start_byte, draft.end_byte
                ):
                    specs.append(self._make_spec(
                        raw,
                        source,
                        file=file,
                        language=language,
                        start_byte=start,
                        end_byte=end,
                        symbol_name=draft.symbol_name,
                        qualified_name=draft.qualified_name,
                        kind="fragment",
                        signature=None,
                        parser=parser,
                        parser_version=parser_version,
                        grammar_version=grammar_version,
                        quality=quality,
                        parse_status=parse_status,
                        error_ranges=errors,
                        parent_slice_id=base.slice_id,
                    ))
                    if len(specs) > self.max_blocks:
                        raise SliceExtractionError(
                            f"extracted slices exceed configured limit {self.max_blocks}"
                        )
            if len(specs) > self.max_blocks:
                raise SliceExtractionError(
                    f"extracted slices exceed configured limit {self.max_blocks}"
                )
        return tuple(specs)

    def _make_spec(
        self,
        raw: bytes,
        source: _SourceText,
        *,
        file: str,
        language: str,
        start_byte: int,
        end_byte: int,
        symbol_name: str | None,
        qualified_name: str | None,
        kind: str,
        signature: str | None,
        parser: str,
        parser_version: str,
        grammar_version: str | None,
        quality: str,
        parse_status: str,
        error_ranges: tuple[ErrorRange, ...],
        parent_slice_id: str | None,
    ) -> CodeBlockSpec:
        display_start, display_end = self._display_window(
            source, start_byte, end_byte
        )
        display_text = raw[display_start:display_end].decode(source.encoding)
        file_digest = hashlib.sha256(raw).hexdigest()
        slice_digest = hashlib.sha256(raw[start_byte:end_byte]).hexdigest()
        slice_id = slice_id_for(
            self.snapshot_digest,
            file_digest,
            start_byte,
            end_byte,
            parser_version,
            kind,
        )
        result = CodeBlockSpec(
            schema_version=1,
            project_id=self.project_id,
            snapshot_digest=self.snapshot_digest,
            generation=self.generation,
            slice_id=slice_id,
            parent_slice_id=parent_slice_id,
            file=file,
            file_digest=file_digest,
            slice_digest=slice_digest,
            start_byte=start_byte,
            end_byte=end_byte,
            start_line=_line_for_byte(raw, start_byte),
            end_line=_inclusive_end_line(raw, end_byte),
            language=language,
            symbol_name=symbol_name,
            qualified_name=qualified_name,
            kind=kind,
            signature=signature,
            feature_tags=(),
            parser=parser,
            parser_version=parser_version,
            grammar_version=grammar_version,
            quality=quality,
            parse_status=parse_status,
            error_ranges=error_ranges,
            encoding=source.encoding,
            display_text=display_text,
            display_truncated=(display_start != start_byte or display_end != end_byte),
            display_start_byte=display_start,
            display_end_byte=display_end,
        )
        result.validate_source(raw)
        return result

    def _display_window(
        self, source: _SourceText, start_byte: int, end_byte: int
    ) -> tuple[int, int]:
        if end_byte - start_byte <= self.display_max_bytes:
            return start_byte, end_byte
        target = min(end_byte, start_byte + self.display_max_bytes)
        index = bisect.bisect_right(source.char_to_byte, target) - 1
        display_end = source.char_to_byte[max(0, index)]
        if display_end <= start_byte:
            next_index = bisect.bisect_right(source.char_to_byte, start_byte)
            if next_index >= len(source.char_to_byte):
                return start_byte, end_byte
            display_end = source.char_to_byte[next_index]
        return start_byte, min(display_end, end_byte)

    def _fragment_ranges(
        self, raw: bytes, source: _SourceText, start_byte: int, end_byte: int
    ) -> tuple[tuple[int, int], ...]:
        ranges: list[tuple[int, int]] = []
        cursor = start_byte
        while cursor < end_byte:
            target = min(end_byte, cursor + self.max_fragment_bytes)
            cut = target
            if target < end_byte:
                newline = raw.rfind(b"\n", cursor + 1, target + 1)
                if newline >= cursor + 1:
                    cut = newline + 1
                else:
                    index = bisect.bisect_right(source.char_to_byte, target) - 1
                    cut = source.char_to_byte[max(0, index)]
                    if cut <= cursor:
                        next_index = bisect.bisect_right(source.char_to_byte, cursor)
                        if next_index >= len(source.char_to_byte):
                            cut = end_byte
                        else:
                            cut = source.char_to_byte[next_index]
            cut = min(cut, end_byte)
            if cut <= cursor:
                raise SliceExtractionError("could not find a safe byte boundary for fragment")
            ranges.append((cursor, cut))
            cursor = cut
            if len(ranges) > self.max_blocks:
                raise SliceExtractionError("fragment count exceeds configured slice limit")
        return tuple(ranges)


__all__ = ["SliceExtractionError", "SliceExtractor"]
