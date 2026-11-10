"""Immutable, validated code-slice records for bounded retrieval."""

from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping

_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_KINDS = frozenset({"function", "method", "class", "type", "module", "fragment"})
_QUALITIES = frozenset({"ast", "recovered", "heuristic"})
_PARSE_STATUSES = frozenset({"ok", "partial", "failed"})


def _require_text(name: str, value: object, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")


def _require_digest(name: str, value: object) -> None:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{name} must be a 64-character hexadecimal digest")


def _require_int(name: str, value: object, *, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


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


def _validate_relative_file(value: object) -> None:
    _require_text("file", value)
    assert isinstance(value, str)
    if len(value.encode("utf-8")) > 4096:
        raise ValueError("file path exceeds the v1 limit of 4096 UTF-8 bytes")
    if "\\" in value or PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise ValueError("file must be a canonical relative POSIX path")
    if PureWindowsPath(value).drive or ":" in value:
        raise ValueError("file must not contain a drive or URI scheme")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("file must not contain empty, dot, or parent path segments")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("file must not contain control characters")
    if str(PurePosixPath(value)) != value:
        raise ValueError("file must be normalized")


def slice_id_for(
    snapshot_digest: str,
    file_digest: str,
    start_byte: int,
    end_byte: int,
    parser_version: str,
    kind: str,
) -> str:
    """Return the deterministic v1 ID for a source range."""
    _require_digest("snapshot_digest", snapshot_digest)
    _require_digest("file_digest", file_digest)
    _require_int("start_byte", start_byte)
    _require_int("end_byte", end_byte, minimum=1)
    if start_byte >= end_byte:
        raise ValueError("slice byte range must be non-empty")
    _require_text("parser_version", parser_version)
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError(f"unsupported slice kind: {kind!r}")
    payload = {
        "end_byte": end_byte,
        "file_digest": file_digest.lower(),
        "kind": kind,
        "parser_version": parser_version,
        "snapshot_digest": snapshot_digest.lower(),
        "start_byte": start_byte,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ErrorRange:
    """A bounded parser/recovery error location in original source bytes."""

    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    message: str

    def __post_init__(self) -> None:
        _require_int("error.start_byte", self.start_byte)
        _require_int("error.end_byte", self.end_byte)
        _require_int("error.start_line", self.start_line, minimum=1)
        _require_int("error.end_line", self.end_line, minimum=1)
        if self.start_byte >= self.end_byte:
            raise ValueError("error byte range must be non-empty")
        if self.start_line > self.end_line:
            raise ValueError("error lines must be ordered")
        _require_text("error.message", self.message)
        if len(self.message) > 500:
            raise ValueError("error.message exceeds the v1 limit of 500 characters")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ErrorRange":
        expected = {field.name for field in fields(cls)}
        actual = set(value)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            raise ValueError(f"invalid error range keys; missing={missing}, unknown={unknown}")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class CodeBlockSpec:
    """Complete immutable CodeBlockSpec v1 record.

    Nullable values are required constructor and mapping fields; this keeps
    serialization complete and prevents omitted fields from gaining defaults.
    """

    schema_version: int
    project_id: str
    snapshot_digest: str
    generation: int
    slice_id: str
    parent_slice_id: str | None
    file: str
    file_digest: str
    slice_digest: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    language: str
    symbol_name: str | None
    qualified_name: str | None
    kind: str
    signature: str | None
    feature_tags: tuple[str, ...]
    parser: str
    parser_version: str
    grammar_version: str | None
    quality: str
    parse_status: str
    error_ranges: tuple[ErrorRange, ...]
    encoding: str
    display_text: str
    display_truncated: bool
    display_start_byte: int
    display_end_byte: int

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        _require_text("project_id", self.project_id)
        _require_digest("snapshot_digest", self.snapshot_digest)
        _require_int("generation", self.generation, minimum=1)
        _require_digest("slice_id", self.slice_id)
        _require_text("parent_slice_id", self.parent_slice_id, nullable=True)
        if self.parent_slice_id is not None:
            _require_digest("parent_slice_id", self.parent_slice_id)
        _validate_relative_file(self.file)
        _require_digest("file_digest", self.file_digest)
        _require_digest("slice_digest", self.slice_digest)
        _require_int("start_byte", self.start_byte)
        _require_int("end_byte", self.end_byte, minimum=1)
        if self.start_byte >= self.end_byte:
            raise ValueError("slice byte range must be non-empty")
        _require_int("start_line", self.start_line, minimum=1)
        _require_int("end_line", self.end_line, minimum=1)
        if self.start_line > self.end_line:
            raise ValueError("slice lines must be ordered")
        _require_text("language", self.language)
        _require_text("symbol_name", self.symbol_name, nullable=True)
        _require_text("qualified_name", self.qualified_name, nullable=True)
        if not isinstance(self.kind, str) or self.kind not in _KINDS:
            raise ValueError(f"unsupported slice kind: {self.kind!r}")
        _require_text("signature", self.signature, nullable=True)
        if not isinstance(self.feature_tags, (tuple, list)):
            raise ValueError("feature_tags must be an array")
        if len(self.feature_tags) > 64:
            raise ValueError("feature_tags exceeds the v1 limit of 64")
        tags: list[str] = []
        for tag in self.feature_tags:
            _require_text("feature_tag", tag)
            if len(tag.encode("utf-8")) > 128:
                raise ValueError("feature_tag exceeds the v1 limit of 128 UTF-8 bytes")
            tags.append(tag)
        if len(tags) != len(set(tags)):
            raise ValueError("feature_tags must not contain duplicates")
        object.__setattr__(self, "feature_tags", tuple(sorted(tags)))
        _require_text("parser", self.parser)
        _require_text("parser_version", self.parser_version)
        _require_text("grammar_version", self.grammar_version, nullable=True)
        if not isinstance(self.quality, str) or self.quality not in _QUALITIES:
            raise ValueError(f"unsupported slice quality: {self.quality!r}")
        if not isinstance(self.parse_status, str) or self.parse_status not in _PARSE_STATUSES:
            raise ValueError(f"unsupported parse status: {self.parse_status!r}")
        if self.parse_status == "ok" and self.quality == "heuristic":
            raise ValueError("heuristic slices cannot claim parse_status='ok'")
        if not isinstance(self.error_ranges, (tuple, list)):
            raise ValueError("error_ranges must be a sequence")
        if len(self.error_ranges) > 256:
            raise ValueError("error_ranges exceeds the v1 limit of 256")
        normalized_errors: list[ErrorRange] = []
        for item in self.error_ranges:
            if isinstance(item, ErrorRange):
                normalized_errors.append(item)
            elif isinstance(item, Mapping):
                normalized_errors.append(ErrorRange.from_mapping(item))
            else:
                raise ValueError("error_ranges entries must be ErrorRange objects or mappings")
        normalized_errors.sort(key=lambda item: (item.start_byte, item.end_byte))
        object.__setattr__(self, "error_ranges", tuple(normalized_errors))
        _require_text("encoding", self.encoding)
        if not isinstance(self.display_text, str):
            raise ValueError("display_text must be a string")
        if len(self.display_text) > 2 * 1024 * 1024:
            raise ValueError("display_text exceeds the v1 input limit")
        if len(self.display_text.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("display_text exceeds the v1 input limit")
        if type(self.display_truncated) is not bool:
            raise ValueError("display_truncated must be a boolean")
        _require_int("display_start_byte", self.display_start_byte)
        _require_int("display_end_byte", self.display_end_byte)
        if not (
            self.start_byte <= self.display_start_byte
            < self.display_end_byte <= self.end_byte
        ):
            raise ValueError("display byte range must be a non-empty subrange of the slice")
        if not self.display_truncated and (
            self.display_start_byte != self.start_byte
            or self.display_end_byte != self.end_byte
        ):
            raise ValueError("non-truncated display must cover the full slice range")
        if self.display_truncated and (
            self.display_start_byte == self.start_byte
            and self.display_end_byte == self.end_byte
        ):
            raise ValueError("truncated display must be a strict subrange of the slice")
        expected_id = slice_id_for(
            self.snapshot_digest,
            self.file_digest,
            self.start_byte,
            self.end_byte,
            self.parser_version,
            self.kind,
        )
        if self.slice_id.lower() != expected_id:
            raise ValueError("slice_id does not match the deterministic v1 identity")

    def validate_source(self, source: bytes) -> None:
        """Raise ValueError unless all byte-derived fields match the source."""
        if not isinstance(source, bytes):
            raise ValueError("source must be bytes")
        if hashlib.sha256(source).hexdigest() != self.file_digest.lower():
            raise ValueError("file_digest does not match source bytes")
        if self.end_byte > len(source):
            raise ValueError("slice range extends beyond source bytes")
        raw_slice = source[self.start_byte:self.end_byte]
        if hashlib.sha256(raw_slice).hexdigest() != self.slice_digest.lower():
            raise ValueError("slice_digest does not match source byte range")
        start_line = _line_for_byte(source, self.start_byte)
        end_line = _inclusive_end_line(source, self.end_byte)
        if (start_line, end_line) != (self.start_line, self.end_line):
            raise ValueError("line range does not match source byte range")
        for error in self.error_ranges:
            if error.end_byte > len(source):
                raise ValueError("error range extends beyond source bytes")
            if (
                _line_for_byte(source, error.start_byte) != error.start_line
                or _inclusive_end_line(source, error.end_byte) != error.end_line
            ):
                raise ValueError("error line range does not match source byte range")
        shown = source[self.display_start_byte:self.display_end_byte]
        try:
            decoded = shown.decode(self.encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            raise ValueError("display byte range cannot be decoded with the declared encoding") from exc
        if decoded != self.display_text:
            raise ValueError("display_text does not match the declared display byte range")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready object containing every schema field."""
        result: dict[str, Any] = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "error_ranges":
                result[field.name] = [item.to_dict() for item in self.error_ranges]
            elif field.name == "feature_tags":
                result[field.name] = list(self.feature_tags)
            else:
                result[field.name] = value
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CodeBlockSpec":
        if not isinstance(value, Mapping):
            raise ValueError("CodeBlockSpec input must be a mapping")
        expected = {field.name for field in fields(cls)}
        actual = set(value)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            raise ValueError(f"invalid CodeBlockSpec keys; missing={missing}, unknown={unknown}")
        data = dict(value)
        errors = data["error_ranges"]
        if not isinstance(errors, (list, tuple)):
            raise ValueError("error_ranges must be an array")
        if len(errors) > 256:
            raise ValueError("error_ranges exceeds the v1 limit of 256")
        data["error_ranges"] = tuple(
            ErrorRange.from_mapping(item) if isinstance(item, Mapping) else item
            for item in errors
        )
        return cls(**data)


__all__ = ["CodeBlockSpec", "ErrorRange", "slice_id_for"]
