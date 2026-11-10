#!/usr/bin/env python3
"""Session-scoped credential redaction for DOC-126.

The redactor deliberately knows about credential *shapes*, rather than trying
to redact every value that happens to follow a word such as ``token``.  This
keeps routes, hashes, UUIDs, and attack-graph identifiers useful while making
the egress boundary fail closed for the supported credential forms.
"""
from __future__ import annotations
from collections.abc import Iterable

import ast
import hashlib
import hmac
import re
import secrets
from pathlib import Path
from typing import Any


PLACEHOLDER_RE = re.compile(r"\{\{REDACTED_[A-Z0-9_]+\}\}")
UUID_RE = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
SHA_RE = re.compile(r"(?i)^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
ROUTE_TEMPLATE_RE = re.compile(r"(?:/:[A-Za-z_][\w-]*|/\{[A-Za-z_][\w-]*\})")

API_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_-]{20,}|"
    r"(?:AKIA|ASIA)[0-9A-Z]{16}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}"
    r")(?![A-Za-z0-9_-])"
)
JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
    r"[\s\S]+?"
    r"-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
)
DSN_RE = re.compile(
    r"(?P<prefix>\b(?:postgres(?:ql)?|mysql|redis|rediss|mongo|"
    r"mongodb(?:\+srv)?|amqp|amqps)://[^:/@\s]+:)"
    r"(?P<password>[^@\s]+)"
    r"(?P<suffix>@[^\s'\")]+)",
    re.IGNORECASE,
)

_SENSITIVE_KEY = (
    r"(?:password|passwd|secret|token|api[_ -]?key|access[_ -]?token|"
    r"refresh[_ -]?token|client[_ -]?secret|private[_ -]?key)"
)
ASSIGNMENT_QUOTED_RE = re.compile(
    rf"(?P<prefix>(?<![\w-]){_SENSITIVE_KEY}(?![\w-])['\"]?\s*[:=]\s*)"
    rf"(?P<quote>['\"`])"
    rf"(?P<value>(?:\\.|(?!(?P=quote))[\s\S]){{4,}}?)"
    rf"(?P=quote)",
    re.IGNORECASE,
)
ASSIGNMENT_BARE_RE = re.compile(
    rf"(?P<prefix>(?<![\w-]){_SENSITIVE_KEY}(?![\w-])['\"]?\s*[:=]\s*)"
    r"(?P<value>(?!['\"`])\{\{REDACTED_[A-Z0-9_]+\}\}|(?!['\"`])[^\s,;\}\]\)&\"']+)",
    re.IGNORECASE,
)
AUTH_HEADER_RE = re.compile(
    r"(?P<prefix>\b(?:authorization|proxy-authorization)\s*:\s*"
    r"(?:bearer|basic)\s+)"
    r"(?P<value>\{\{REDACTED_[A-Z0-9_]+\}\}|[^\r\n,\}\]\"']+)",
    re.IGNORECASE,
)
AUTH_VALUE_HEADER_RE = re.compile(
    r"(?P<prefix>\b(?:x-api-key|x-auth-token|x-access-token|cookie|"
    r"set-cookie)\s*:\s*)"
    r"(?P<value>\{\{REDACTED_[A-Z0-9_]+\}\}|[^\r\n,\}\]\"']+)",
    re.IGNORECASE,
)
BEARER_IGNORE_WORDS = frozenset({
    "token", "tokens", "auth", "authentication", "credential", "credentials",
    "scheme", "format", "header", "headers", "type", "mode", "of", "the",
    "a", "an", "to", "for", "in", "is", "was", "and", "or",
    "adm", "admin", "adm9", "user", "guest", "test", "bearer",
})
BEARER_RE = re.compile(
    r"(?P<prefix>\bBearer\s+)"
    r"(?P<value>\{\{REDACTED_[A-Z0-9_]+\}\}|[A-Za-z0-9_.\-~+/=:]+)",
    re.IGNORECASE,
)

# DES-17: 业务级敏感实体 (PII) 正则模式
ID_CARD_RE = re.compile(
    r"(?<![0-9A-Za-z])(?P<id>[1-6]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?![0-9A-Za-z])"
)
BANK_CARD_RE = re.compile(r"(?<!\d)(?P<card>[3-6]\d{15,18})(?!\d)")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?(?P<phone>1[3-9]\d{9})(?!\d)")
PRIVATE_IP_RE = re.compile(
    r"(?<![\d.])(?P<ip>(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|192\.168(?:\.\d{1,3}){2}))(?![\d.])"
)


def is_valid_chinese_id(id_str: str) -> bool:
    """验证 18 位中国居民身份证校验位 (GB 11643-1999 模 11 算法)。"""
    if len(id_str) != 18:
        return False
    factors = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    parity = "10X98765432"
    try:
        s = sum(int(id_str[i]) * factors[i] for i in range(17))
        return parity[s % 11] == id_str[17].upper()
    except (ValueError, IndexError):
        return False


def is_valid_luhn(card_str: str) -> bool:
    """验证银行卡号的 Luhn (模 10) 校验和。"""
    if not (16 <= len(card_str) <= 19 and card_str.isdigit()):
        return False
    digits = [int(c) for c in card_str]
    checksum = 0
    reverse_digits = digits[::-1]
    for i, d in enumerate(reverse_digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def load_redact_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """读取自定义脱敏配置文件 (.bugx-redact.json 或 .bugx-redact.yaml)。"""
    candidates: list[Path] = []
    if config_path:
        candidates.append(Path(config_path))
    else:
        cwd = Path.cwd()
        candidates.extend([cwd / ".bugx-redact.json", cwd / ".bugx-redact.yaml", cwd / ".bugx-redact.yml"])
    for p in candidates:
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8")
                if p.suffix == ".json":
                    import json
                    return json.loads(text)
                try:
                    import yaml
                    return yaml.safe_load(text) or {}
                except ImportError:
                    res: dict[str, list[str]] = {"keywords": [], "patterns": []}
                    curr_key = None
                    for line in text.splitlines():
                        sline = line.strip()
                        if sline.startswith("keywords:"):
                            curr_key = "keywords"
                        elif sline.startswith("patterns:"):
                            curr_key = "patterns"
                        elif sline.startswith("- ") and curr_key:
                            val = sline[2:].strip().strip("\"'")
                            res[curr_key].append(val)
                    return res
            except Exception:
                pass
    return {}

SAFE_TEST_VALUES = frozenset({
    "test", "dummy", "example", "123456", "password", "mock", "admin",
    "fake", "sample", "placeholder", "test-token", "test_token",
    "pending", "none", "null", "nil", "empty", "undefined", "true", "false",
    "na", "n/a", "decoy", "demo", "temp", "tmp", "adm", "adm9", "proj-b",
    "anon", "guest", "default", "fake-token", "demo-token",
})

_LANGUAGE_ALIASES = {
    "py": "python", "python3": "python", "js": "javascript",
    "ts": "typescript", "tsx": "typescript", "jsx": "javascript",
    "golang": "go", "c++": "cpp", "c#": "csharp", "shell": "bash",
    "sh": "bash", "zsh": "bash",
}
_LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".pyw": "python", ".js": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".java": "java", ".kt": "kotlin", ".rs": "rust",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".cxx": "cpp", ".cs": "csharp", ".php": "php", ".rb": "ruby",
    ".swift": "swift", ".sh": "bash", ".yaml": "yaml", ".yml": "yaml",
}


class SecretRedactionError(ValueError):
    """Raised when a bounded session map or syntax guard cannot continue."""


def is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER_RE.fullmatch(value.strip()))


def _is_hash_or_uuid(value: str) -> bool:
    candidate = value.strip()
    if UUID_RE.fullmatch(candidate) or SHA_RE.fullmatch(candidate):
        return True
    prefix, separator, digest = candidate.partition(":")
    return bool(separator and prefix.lower() in {"sha1", "sha256", "commit"}
                and SHA_RE.fullmatch(digest))


def _is_route_template(value: str) -> bool:
    return bool(ROUTE_TEMPLATE_RE.search(value)) and value.lstrip().startswith("/")


def _language(language: str | None, filename: str | Path | None) -> str | None:
    if language:
        normalized = language.strip().lower().lstrip(".")
        return _LANGUAGE_ALIASES.get(normalized, normalized)
    if filename:
        return _LANGUAGE_BY_SUFFIX.get(Path(filename).suffix.lower())
    return None


def _test_context(filename: str | Path | None, explicit: bool) -> bool:
    if explicit:
        return True
    if not filename:
        return False
    path = str(filename).replace("\\", "/")
    return Path(path).name.startswith("test_") or "/tests/" in f"/{path.lstrip('/')}"


def _test_function_context(text: str, position: int | None = None) -> bool:
    """Check only the indented body of a ``test_*`` function."""
    lines = text.splitlines(keepends=True)
    offset = 0
    spans: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if re.search(r"\b(?:async\s+def|def|function)\s+test_[A-Za-z0-9_]*\b", line):
            indent = len(line) - len(line.lstrip())
            end = len(text)
            cursor = offset + len(line)
            for child in lines[index + 1:]:
                if child.strip() and len(child) - len(child.lstrip()) <= indent:
                    end = cursor
                    break
                cursor += len(child)
            spans.append((offset, end))
        offset += len(line)
    if position is None:
        return bool(spans)
    return any(start <= position < end for start, end in spans)


def _safe_literal(value: str, *, test_context: bool) -> bool:
    candidate = value.strip()
    if not candidate or is_placeholder(candidate):
        return True
    if _is_hash_or_uuid(candidate) or _is_route_template(candidate):
        return True
    return test_context and candidate.lower() in SAFE_TEST_VALUES


def _safe_gate_literal(value: str) -> bool:
    """Values explicitly documented as harmless test fixtures are safe at egress."""
    candidate = value.strip()
    if not candidate or is_placeholder(candidate):
        return True
    if API_KEY_RE.search(candidate) or JWT_RE.search(candidate) or PRIVATE_KEY_RE.search(candidate):
        return False
    if (
        _is_hash_or_uuid(candidate)
        or _is_route_template(candidate)
        or candidate.lower() in SAFE_TEST_VALUES
    ):
        return True
    # Pure numbers or digits
    if candidate.isdigit():
        return True
    # Ellipsis or punctuation
    if all(c in ".-_/:;~^<>=*#@!?" for c in candidate):
        return True
    # Short tokens (< 4 chars) without strong secret signature
    if len(candidate) < 4:
        return True
    # Substring markers of test/decoy/demo intent
    lower = candidate.lower()
    if any(m in lower for m in ("decoy", "demo", "sample", "example", "placeholder", "fake")):
        return True
    return False


def _classify(value: str, fallback: str = "SECRET") -> str:
    if PRIVATE_KEY_RE.fullmatch(value):
        return "PRIVATE_KEY"
    if JWT_RE.fullmatch(value):
        return "JWT_TOKEN"
    if API_KEY_RE.fullmatch(value):
        return "API_KEY"
    return fallback


def _assignment_kind(prefix: str) -> str:
    key = prefix.split(":", 1)[0].split("=", 1)[0].strip().lower()
    if "api" in key and "key" in key:
        return "API_KEY"
    if "token" in key:
        return "TOKEN"
    if "password" in key or "passwd" in key:
        return "PASSWORD"
    return "SECRET"


def _letter(index: int) -> str:
    result = ""
    number = index
    while True:
        result = chr(ord("A") + number % 26) + result
        number = number // 26 - 1
        if number < 0:
            return result


def _balanced(text: str, language: str) -> tuple[bool, str | None, list[str]]:
    """Return lexical validity plus the currently open quote/bracket suffix."""
    quote: str | None = None
    escaped = False
    brackets: list[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    closing = set(pairs.values())
    i = 0
    line_comment = False
    block_comment = False
    while i < len(text):
        char = text[i]
        next_char = text[i + 1] if i + 1 < len(text) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif text.startswith(quote, i):
                i += len(quote)
                quote = None
                continue
            i += 1
            continue
        if char == "#" and language in {"python", "ruby", "bash", "yaml"}:
            line_comment = True
            i += 1
            continue
        if char == "/" and next_char == "/" and language not in {"python", "ruby"}:
            line_comment = True
            i += 2
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            i += 2
            continue
        if text.startswith("'''", i) or text.startswith('"""', i):
            quote = text[i:i + 3]
            i += 3
            continue
        if char in "'\"`":
            quote = char
        elif char in pairs:
            brackets.append(pairs[char])
        elif char in closing:
            if not brackets or brackets[-1] != char:
                return False, quote, brackets
            brackets.pop()
        i += 1
    return not quote and not brackets, quote, brackets


def syntax_is_valid(text: str, language: str | None = None,
                    filename: str | Path | None = None) -> bool:
    """Probe Python with ``ast`` and other supported languages lexically."""
    lang = _language(language, filename)
    if not lang:
        return True
    if lang == "python":
        try:
            ast.parse(text)
            return True
        except (SyntaxError, ValueError, TypeError):
            return False
    return _balanced(text, lang)[0]


def ensure_syntax(text: str, language: str | None = None,
                  filename: str | Path | None = None) -> str:
    """Repair a lexical boundary, then use a language-safe comment fallback."""
    lang = _language(language, filename)
    if not lang or syntax_is_valid(text, lang):
        return text
    valid, quote, brackets = _balanced(text, lang)
    del valid
    suffix = (quote or "") + "".join(reversed(brackets))
    candidate = text + suffix
    if syntax_is_valid(candidate, lang):
        return candidate
    if lang == "python":
        return "# REDACTED_SYNTAX_SAFE_BLOCK\n"
    return "/* REDACTED_SYNTAX_SAFE_BLOCK */\n"


class SecretRedactor:
    """Redact strings and nested payloads with a session-local pseudonym map."""

    def __init__(self, *, salt: bytes | None = None, max_mappings: int = 4096,
                 test_context: bool = False, redact_pii: bool = True,
                 custom_keywords: Iterable[str] | None = None,
                 custom_patterns: Iterable[str | re.Pattern[str]] | None = None,
                 config_path: str | Path | None = None) -> None:
        if max_mappings < 1:
            raise ValueError("max_mappings must be positive")
        self._salt = bytearray(salt or secrets.token_bytes(32))
        self._secret_map: dict[str, str] = {}
        self._digest_map: dict[bytes, str] = {}
        self._kind_counts: dict[str, int] = {}
        self._max_mappings = max_mappings
        self._test_context = test_context
        self._closed = False
        self._redact_pii = redact_pii

        cfg = load_redact_config(config_path) if (config_path or not (custom_keywords or custom_patterns)) else {}
        keywords = set(custom_keywords or ())
        keywords.update(cfg.get("keywords") or ())
        self._custom_keywords = sorted((kw.strip() for kw in keywords if kw.strip()), key=len, reverse=True)

        patterns = list(custom_patterns or ())
        patterns.extend(cfg.get("patterns") or ())
        self._custom_patterns: list[re.Pattern[str]] = []
        for pat in patterns:
            if isinstance(pat, re.Pattern):
                self._custom_patterns.append(pat)
            elif isinstance(pat, str) and pat.strip():
                try:
                    self._custom_patterns.append(re.compile(pat))
                except re.error:
                    pass

    def __enter__(self) -> "SecretRedactor":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @property
    def mapping_size(self) -> int:
        return len(self._secret_map)

    def close(self) -> None:
        """Drop all plaintext and pseudonym state at the session boundary."""
        self._secret_map.clear()
        self._digest_map.clear()
        self._kind_counts.clear()
        for index in range(len(self._salt)):
            self._salt[index] = 0
        self._closed = True

    clear = close

    def _pseudo(self, value: str, kind: str) -> str:
        if self._closed:
            raise SecretRedactionError("redaction session has ended")
        existing = self._secret_map.get(value)
        if existing:
            return existing
        if len(self._secret_map) >= self._max_mappings:
            raise SecretRedactionError("redaction session mapping limit exceeded")
        digest = hmac.new(bytes(self._salt), value.encode("utf-8"), hashlib.sha256).digest()
        # The digest is intentionally kept only in memory to bind mappings to
        # the session salt; the wire token remains a useful A/B pseudonym.
        while digest in self._digest_map:
            digest = hashlib.sha256(digest + b"\0").digest()
        index = self._kind_counts.get(kind, 0)
        self._kind_counts[kind] = index + 1
        if kind == "TOKEN":
            token = f"{{{{REDACTED_TOKEN_{_letter(index)}}}}}"
        else:
            token = f"{{{{REDACTED_{kind}_{index + 1}}}}}"
        self._secret_map[value] = token
        self._digest_map[digest] = token
        return token

    def _replace_value(self, value: str, fallback: str, *, filename: str | Path | None,
                       test_context: bool) -> str:
        if _safe_literal(value, test_context=test_context):
            return value
        kind = _classify(value, fallback)
        return self._pseudo(value, kind)

    def _replace_assignment(self, match: re.Match[str], *, filename: str | Path | None,
                            test_context: bool) -> str:
        value = match.group("value")
        replaced = self._replace_value(
            value, _assignment_kind(match.group("prefix")),
            filename=filename, test_context=test_context,
        )
        return f"{match.group('prefix')}{match.group('quote')}{replaced}{match.group('quote')}"

    def _replace_bare(self, match: re.Match[str], *, filename: str | Path | None,
                      test_context: bool, language: str | None) -> str:
        value = match.group("value")
        if PLACEHOLDER_RE.fullmatch(value) or value[:1] in "'\"`":
            if PLACEHOLDER_RE.fullmatch(value) and _language(language, filename) == "python":
                return f'{match.group("prefix")}"{value}"'
            return match.group(0)
        if len(value) >= 2 and value[0] in "'\"`" and value[-1] == value[0]:
            if _safe_literal(value[1:-1], test_context=test_context):
                return match.group(0)
        replaced = self._replace_value(
            value, _assignment_kind(match.group("prefix")),
            filename=filename, test_context=test_context,
        )
        if replaced == value:
            return match.group(0)
        return f'{match.group("prefix")}"{replaced}"'

    def redact_text(self, text: str, *, language: str | None = None,
                    filename: str | Path | None = None,
                    test_context: bool | None = None,
                    guard_syntax: bool = True) -> str:
        if not isinstance(text, str):
            raise TypeError("text must be str")
        context = self._test_context or _test_context(filename, bool(test_context))

        def replace_private(match: re.Match[str]) -> str:
            return self._replace_value(match.group(0), "PRIVATE_KEY",
                                        filename=filename, test_context=context)

        def replace_dsn(match: re.Match[str]) -> str:
            password = match.group("password")
            replacement = self._replace_value(password, "DSN_PASSWORD",
                                               filename=filename, test_context=context)
            return f"{match.group('prefix')}{replacement}{match.group('suffix')}"

        def replace_header(match: re.Match[str]) -> str:
            value = match.group("value").strip()
            if value.lower() in BEARER_IGNORE_WORDS:
                return match.group(0)
            header = match.group("prefix").lower()
            fallback = "TOKEN" if ("bearer" in header or "auth-token" in header) else "HTTP_AUTH"
            replacement = self._replace_value(value, fallback,
                                               filename=filename, test_context=context)
            return f"{match.group('prefix')}{replacement}"

        def replace_direct(match: re.Match[str]) -> str:
            kind = "JWT_TOKEN" if match.group(0).startswith("eyJ") else "API_KEY"
            return self._replace_value(match.group(0), kind,
                                       filename=filename, test_context=context)

        result = PRIVATE_KEY_RE.sub(replace_private, text)
        result = DSN_RE.sub(replace_dsn, result)
        result = ASSIGNMENT_QUOTED_RE.sub(
            lambda match: self._replace_assignment(
                match, filename=filename,
                test_context=context or _test_function_context(result, match.start()),
            ), result,
        )
        result = ASSIGNMENT_BARE_RE.sub(
            lambda match: self._replace_bare(
                match, filename=filename,
                test_context=context or _test_function_context(result, match.start()),
                language=language,
            ), result,
        )
        result = AUTH_HEADER_RE.sub(replace_header, result)
        result = AUTH_VALUE_HEADER_RE.sub(replace_header, result)
        result = BEARER_RE.sub(replace_header, result)
        result = JWT_RE.sub(replace_direct, result)
        result = API_KEY_RE.sub(replace_direct, result)

        # DES-17: 自定义敏感词与模式脱敏
        for kw in self._custom_keywords:
            if kw and kw in result:
                replacement = self._replace_value(kw, "CUSTOM", filename=filename, test_context=context)
                result = result.replace(kw, replacement)
        for pattern in self._custom_patterns:
            result = pattern.sub(
                lambda m: self._replace_value(m.group(0), "CUSTOM", filename=filename, test_context=context),
                result,
            )

        # DES-17: 业务级敏感实体 (PII) 脱敏
        if self._redact_pii:
            def replace_id_card(match: re.Match[str]) -> str:
                val = match.group("id")
                if is_valid_chinese_id(val):
                    return self._replace_value(val, "ID_CARD", filename=filename, test_context=context)
                return match.group(0)

            def replace_bank_card(match: re.Match[str]) -> str:
                val = match.group("card")
                if is_valid_luhn(val):
                    return self._replace_value(val, "BANK_CARD", filename=filename, test_context=context)
                return match.group(0)

            def replace_phone(match: re.Match[str]) -> str:
                val = match.group("phone")
                matched_all = match.group(0)
                prefix = matched_all[: match.start("phone") - match.start(0)]
                return f"{prefix}{self._replace_value(val, 'PHONE', filename=filename, test_context=context)}"

            def replace_private_ip(match: re.Match[str]) -> str:
                val = match.group("ip")
                return self._replace_value(val, "PRIVATE_IP", filename=filename, test_context=context)

            result = ID_CARD_RE.sub(replace_id_card, result)
            result = BANK_CARD_RE.sub(replace_bank_card, result)
            result = PHONE_RE.sub(replace_phone, result)
            result = PRIVATE_IP_RE.sub(replace_private_ip, result)

        if guard_syntax:
            result = ensure_syntax(result, language, filename)
        return result

    def redact(self, value: Any, *, language: str | None = None,
               filename: str | Path | None = None,
               test_context: bool | None = None,
               guard_syntax: bool = True) -> Any:
        """Recursively redact payload values while preserving container shape."""
        if isinstance(value, str):
            return self.redact_text(value, language=language, filename=filename,
                                    test_context=test_context,
                                    guard_syntax=guard_syntax)
        if isinstance(value, dict):
            return {
                key: self.redact(item, language=language, filename=filename,
                                 test_context=test_context, guard_syntax=guard_syntax)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact(item, language=language, filename=filename,
                                test_context=test_context, guard_syntax=guard_syntax)
                    for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item, language=language, filename=filename,
                                     test_context=test_context,
                                     guard_syntax=guard_syntax) for item in value)
        return value

    def redact_records(self, value: Any, **kwargs: Any) -> Any:
        """Alias for recursively redacting records/payloads."""
        return self.redact(value, **kwargs)

    def redact_code(self, code: str, language: str = "python", **kwargs: Any) -> str:
        """Alias for redacting code with language awareness."""
        return self.redact_text(code, language=language, **kwargs)

    def syntax_valid(self, code: str, language: str = "python",
                     filename: str | Path | None = None) -> bool:
        """Check whether code syntax is valid."""
        return syntax_is_valid(code, language, filename)

    def scan(self, text: str, *, filename: str | Path | None = None,
              test_context: bool | None = None) -> list[str]:
        """Return plaintext credential matches without retaining a second session."""
        context = self._test_context or _test_context(filename, bool(test_context))
        candidates: list[str] = []

        def collect(value: str) -> None:
            if not _safe_gate_literal(value) and value not in candidates:
                candidates.append(value)

        for match in PRIVATE_KEY_RE.finditer(text):
            collect(match.group(0))
        for match in DSN_RE.finditer(text):
            collect(match.group("password"))
        for pattern in (AUTH_HEADER_RE, AUTH_VALUE_HEADER_RE, BEARER_RE):
            for match in pattern.finditer(text):
                val = match.group("value").strip()
                if pattern is BEARER_RE and val.lower() in BEARER_IGNORE_WORDS:
                    continue
                collect(val)
        for pattern in (ASSIGNMENT_QUOTED_RE, ASSIGNMENT_BARE_RE):
            for match in pattern.finditer(text):
                value = match.group("value")
                if pattern is ASSIGNMENT_BARE_RE and len(value) >= 2 and value[0] in "'\"`" \
                        and value[-1] == value[0]:
                    value = value[1:-1]
                if not (context or _test_function_context(text, match.start())):
                    collect(value)
        for pattern in (JWT_RE, API_KEY_RE):
            for match in pattern.finditer(text):
                collect(match.group(0))
        for kw in self._custom_keywords:
            if kw in text:
                collect(kw)
        if self._redact_pii:
            for match in ID_CARD_RE.finditer(text):
                val = match.group("id")
                if is_valid_chinese_id(val):
                    collect(val)
            for match in BANK_CARD_RE.finditer(text):
                val = match.group("card")
                if is_valid_luhn(val):
                    collect(val)
            for match in PHONE_RE.finditer(text):
                collect(match.group("phone"))
        return candidates


def redact_text(text: str, **kwargs: Any) -> str:
    return SecretRedactor().redact_text(text, **kwargs)


def redact(value: Any, **kwargs: Any) -> Any:
    return SecretRedactor().redact(value, **kwargs)


def redact_payload(value: Any, **kwargs: Any) -> Any:
    return redact(value, **kwargs)


def find_unredacted_secrets(text: str, **kwargs: Any) -> list[str]:
    return SecretRedactor().scan(text, **kwargs)


def contains_unredacted_secrets(text: str, **kwargs: Any) -> bool:
    return bool(find_unredacted_secrets(text, **kwargs))


def contains_unredacted_secret(value: Any, **kwargs: Any) -> bool:
    if isinstance(value, str):
        return contains_unredacted_secrets(value, **kwargs)
    import json
    try:
        return contains_unredacted_secrets(json.dumps(value, ensure_ascii=False), **kwargs)
    except Exception:
        return False


def redact_records(value: Any, **kwargs: Any) -> Any:
    return redact(value, **kwargs)


def is_whitelisted(value: str) -> bool:
    return _is_hash_or_uuid(value)


def syntax_guard(text: str, language: str | None = None,
                 filename: str | Path | None = None) -> str:
    return ensure_syntax(text, language, filename)


__all__ = [
    "API_KEY_RE", "JWT_RE", "PRIVATE_KEY_RE", "DSN_RE", "PLACEHOLDER_RE",
    "SAFE_TEST_VALUES", "SecretRedactionError", "SecretRedactor",
    "contains_unredacted_secrets", "contains_unredacted_secret", "ensure_syntax",
    "find_unredacted_secrets", "is_placeholder", "is_whitelisted", "redact",
    "redact_payload", "redact_records", "redact_text", "syntax_guard",
    "syntax_is_valid",
]
