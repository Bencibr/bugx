"""Deterministic planning for the small, supported retrieval query language.

The planner deliberately uses bounded phrase and field patterns instead of an
LLM or a general-purpose query parser. It emits data only: callers must still
bind the plan to an authorized project scope and validate it at execution time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping, Optional


SUPPORTED_INTENTS = frozenset(
    {
        "find_symbol",
        "search_code",
        "callers",
        "callees",
        "neighbors",
        "recall_lesson",
    }
)
SUPPORTED_FILTERS = frozenset({"language", "path_prefix", "kind"})
SUPPORTED_KINDS = frozenset(
    {"function", "method", "class", "type", "module", "fragment"}
)
MAX_QUERY_BYTES = 512
MAX_TOP_K = 20
MAX_BUDGET = 800
MAX_DEPTH = 2

_GRAPH_INTENTS = frozenset({"callers", "callees", "neighbors"})
_SYMBOL_INTENTS = frozenset({"find_symbol", "callers", "callees"})
_SYMBOL_RE = re.compile(r"[\w$]+(?:(?:\.|::)[\w$]+)*\Z", re.UNICODE)
_NODE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_LANGUAGE_RE = re.compile(r"[a-z][a-z0-9_+#.-]{0,31}\Z")
_KIND_ALIASES = {
    "functions": "function",
    "methods": "method",
    "classes": "class",
    "types": "type",
    "modules": "module",
    "fragments": "fragment",
}
_LANGUAGE_ALIASES = {
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "golang": "go",
    "c++": "cpp",
    "c#": "csharp",
    "objective-c": "objc",
    "objective_c": "objc",
    "sh": "shell",
    "bash": "shell",
}
_KNOWN_LANGUAGE_PHRASES = tuple(
    sorted(
        {
            "python", "py", "javascript", "js", "typescript", "ts", "rust",
            "go", "golang", "java", "kotlin", "swift", "c", "c++", "cpp",
            "c#", "csharp", "objective-c", "objective_c", "objc", "ruby",
            "php", "scala", "shell", "sh", "bash", "powershell", "html",
            "css", "json", "yaml", "toml", "markdown", "lua", "elixir",
            "erlang", "haskell", "ocaml", "perl", "r", "dart", "groovy",
            "solidity", "terraform", "hcl", "protobuf", "sql",
        },
        key=len,
        reverse=True,
    )
)


class QueryPlannerError(ValueError):
    """A fail-fast planning error with a stable code and one correction hint."""

    CODES = frozenset({"invalid_request", "unsupported", "needs_context"})

    def __init__(
        self,
        code: str,
        message: str,
        hint: str,
        *,
        unsupported_clauses: tuple[str, ...] = (),
    ) -> None:
        if code not in self.CODES:
            raise ValueError("unknown query planner error code")
        self.code = code
        self.message = message
        self.hint = hint
        self.unsupported_clauses = tuple(unsupported_clauses)
        super().__init__("%s: %s Hint: %s" % (code, message, hint))


def _fail(code: str, message: str, hint: str, *, clauses: tuple[str, ...] = ()) -> None:
    raise QueryPlannerError(code, message, hint, unsupported_clauses=clauses)


def _unquote(value: str, *, field_name: str) -> str:
    value = value.strip()
    if not value:
        _fail(
            "invalid_request",
            "%s must have a value" % field_name,
            "supply a non-empty value for %s" % field_name,
        )
    if value[0] in {'"', "'", "`"}:
        if len(value) < 2 or value[-1] != value[0]:
            _fail(
                "invalid_request",
                "%s has an unmatched quote" % field_name,
                "close the quoted value or remove its quotes",
            )
        value = value[1:-1]
    return value.strip()


def _normalize_language(value: Any) -> str:
    if not isinstance(value, str):
        _fail(
            "invalid_request",
            "language filter must be text",
            "use a language name such as python, rust, or typescript",
        )
    language = _unquote(value, field_name="language").casefold()
    language = _LANGUAGE_ALIASES.get(language, language)
    if not _LANGUAGE_RE.fullmatch(language):
        _fail(
            "invalid_request",
            "language filter contains unsupported syntax",
            "use one language label, for example language=python",
        )
    return language


def _normalize_kind(value: Any) -> str:
    if not isinstance(value, str):
        _fail(
            "invalid_request",
            "kind filter must be text",
            "use function, method, class, type, module, or fragment",
        )
    kind = _unquote(value, field_name="kind").casefold()
    kind = _KIND_ALIASES.get(kind, kind)
    if kind not in SUPPORTED_KINDS:
        _fail(
            "invalid_request",
            "unknown kind filter %r" % kind,
            "use function, method, class, type, module, or fragment",
        )
    return kind


def _normalize_path_prefix(value: Any) -> str:
    if not isinstance(value, str):
        _fail(
            "invalid_request",
            "path_prefix filter must be text",
            "use a project-relative prefix such as core/retrieval",
        )
    path = _unquote(value, field_name="path_prefix")
    if (
        len(path.encode("utf-8")) > 256
        or path.startswith(("/", "~"))
        or re.match(r"^[A-Za-z]:", path)
        or "\\" in path
        or any(ord(char) < 32 for char in path)
        or any(char in path for char in "*?[]{};")
        or "://" in path
    ):
        _fail(
            "invalid_request",
            "path_prefix must be a bounded project-relative path",
            "remove absolute paths, wildcards, and shell or URL syntax",
        )
    path = path.rstrip("/")
    components = path.split("/")
    if not path or any(component in {"", ".", ".."} for component in components):
        _fail(
            "invalid_request",
            "path_prefix contains an empty or traversing component",
            "use a normalized project-relative prefix such as core/retrieval",
        )
    return path


def _normalize_filters(filters: Optional[Mapping[str, Any]]) -> Mapping[str, str]:
    if filters is None:
        return MappingProxyType({})
    if not isinstance(filters, Mapping):
        _fail(
            "invalid_request",
            "filters must be a mapping of supported names to text values",
            "use only language, path_prefix, and kind filters",
        )
    normalized: dict[str, str] = {}
    for raw_name, raw_value in filters.items():
        if not isinstance(raw_name, str):
            _fail(
                "invalid_request",
                "filter names must be text",
                "use only language, path_prefix, and kind filters",
            )
        name = raw_name.casefold()
        if name == "lang":
            name = "language"
        elif name == "path":
            name = "path_prefix"
        if name not in SUPPORTED_FILTERS:
            _fail(
                "invalid_request",
                "unknown filter %r" % raw_name,
                "use only language, path_prefix, and kind filters",
            )
        if name == "language":
            value = _normalize_language(raw_value)
        elif name == "path_prefix":
            value = _normalize_path_prefix(raw_value)
        else:
            value = _normalize_kind(raw_value)
        if name in normalized and normalized[name] != value:
            _fail(
                "invalid_request",
                "conflicting values were supplied for %s" % name,
                "keep one value for each filter",
            )
        normalized[name] = value
    return MappingProxyType(dict(sorted(normalized.items())))


def _validate_integer(name: str, value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail(
            "invalid_request",
            "%s must be an integer from %d through %d" % (name, minimum, maximum),
            "set %s within the supported range" % name,
        )
    return value


def _normalize_symbol(value: Any) -> str:
    if not isinstance(value, str):
        _fail(
            "invalid_request",
            "symbol must be text",
            "supply one qualified symbol such as package.module.function",
        )
    symbol = _unquote(value, field_name="symbol")
    if len(symbol.encode("utf-8")) > 256 or not _SYMBOL_RE.fullmatch(symbol):
        _fail(
            "invalid_request",
            "symbol contains unsupported syntax or more than one symbol",
            "supply one identifier, optionally qualified with dots or ::",
        )
    return symbol


def _normalize_node_id(value: Any) -> str:
    if not isinstance(value, str):
        _fail(
            "invalid_request",
            "node_id must be text",
            "supply a stable node_id from the current graph generation",
        )
    node_id = _unquote(value, field_name="node_id")
    if not _NODE_ID_RE.fullmatch(node_id):
        _fail(
            "invalid_request",
            "node_id contains unsupported syntax",
            "supply one stable node_id from the current graph generation",
        )
    return node_id


def _clean_query(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(
            "invalid_request",
            "query text must be a string",
            "supply plain text for the search terms",
        )
    query = " ".join(value.split()).strip(" .?!")
    if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
        _fail(
            "invalid_request",
            "query exceeds the %d-byte limit" % MAX_QUERY_BYTES,
            "shorten the query to at most %d UTF-8 bytes" % MAX_QUERY_BYTES,
        )
    if any(ord(char) < 32 and char not in "\t\r\n" for char in query):
        _fail(
            "invalid_request",
            "query contains a control character",
            "remove control characters and submit plain text",
        )
    return query or None


@dataclass(frozen=True)
class QueryPlanAST:
    """Immutable, validated query plan data; it never contains executable DSL."""

    intent: str
    query: Optional[str] = None
    symbol: Optional[str] = None
    node_id: Optional[str] = None
    filters: Mapping[str, str] = field(default_factory=dict, hash=False)
    depth: int = 1
    top_k: int = 10
    budget: int = MAX_BUDGET

    def __post_init__(self) -> None:
        if not isinstance(self.intent, str) or self.intent not in SUPPORTED_INTENTS:
            _fail(
                "invalid_request",
                "intent is not supported",
                "use find_symbol, search_code, callers, callees, neighbors, or recall_lesson",
            )
        normalized_query = _clean_query(self.query)
        normalized_filters = _normalize_filters(self.filters)
        normalized_symbol = None if self.symbol is None else _normalize_symbol(self.symbol)
        normalized_node_id = None if self.node_id is None else _normalize_node_id(self.node_id)
        depth = _validate_integer("depth", self.depth, 1, MAX_DEPTH)
        top_k = _validate_integer("top_k", self.top_k, 1, MAX_TOP_K)
        budget = _validate_integer("budget", self.budget, 1, MAX_BUDGET)

        if self.intent in _SYMBOL_INTENTS and normalized_symbol is None:
            _fail(
                "needs_context",
                "%s requires one symbol" % self.intent,
                "add a symbol, for example callers of package.module.function",
            )
        if self.intent == "neighbors" and normalized_node_id is None:
            _fail(
                "needs_context",
                "neighbors requires a stable node_id",
                "resolve the symbol in the current graph, then supply its node_id",
            )
        if self.intent not in _GRAPH_INTENTS and depth != 1:
            _fail(
                "invalid_request",
                "depth is only supported for graph intents",
                "use depth=1 or depth=2 with graph intents such as callers, callees, or neighbors",
            )
        if self.intent == "search_code" and normalized_query is None and not normalized_filters:
            _fail(
                "needs_context",
                "search_code needs search terms or at least one filter",
                "add terms such as search code for payment retry, or a supported filter",
            )
        if self.intent == "recall_lesson" and normalized_query is None:
            _fail(
                "needs_context",
                "recall_lesson needs a topic",
                "add a topic, for example recall lessons about retry handling",
            )
        if self.intent in _SYMBOL_INTENTS and normalized_node_id is not None:
            _fail(
                "invalid_request",
                "node_id is only valid for neighbors",
                "use a symbol for find_symbol, callers, or callees",
            )
        if self.intent != "neighbors" and normalized_node_id is not None:
            _fail(
                "invalid_request",
                "node_id is only valid for neighbors",
                "use a node_id with the neighbors intent",
            )
        if self.intent == "neighbors" and normalized_symbol is not None:
            _fail(
                "invalid_request",
                "neighbors requires a node_id rather than a symbol",
                "resolve the symbol in the current graph and pass its stable node_id",
            )

        object.__setattr__(self, "query", normalized_query)
        object.__setattr__(self, "filters", normalized_filters)
        object.__setattr__(self, "symbol", normalized_symbol)
        object.__setattr__(self, "node_id", normalized_node_id)
        object.__setattr__(self, "depth", depth)
        object.__setattr__(self, "top_k", top_k)
        object.__setattr__(self, "budget", budget)

    @property
    def capabilities(self) -> tuple[str, ...]:
        """Static planner features requested by this plan, not runtime claims."""
        features = ["intent:%s" % self.intent]
        features.extend("filter:%s" % name for name in self.filters)
        if self.intent in _GRAPH_INTENTS:
            features.append("graph_depth:%d" % self.depth)
        return tuple(features)

    def _canonical_payload(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "query": self.query,
            "symbol": self.symbol,
            "node_id": self.node_id,
            "filters": dict(self.filters),
            "depth": self.depth,
            "top_k": self.top_k,
            "budget": self.budget,
        }

    @property
    def plan_digest(self) -> str:
        encoded = json.dumps(
            self._canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QueryPlanAST":
        """Construct and validate a QueryPlanAST from a structured dictionary."""
        if not isinstance(data, Mapping):
            _fail(
                "invalid_request",
                "structured query must be a mapping",
                "provide a dictionary with 'intent' and optional filters/options",
            )
        allowed_keys = {
            "intent", "query", "symbol", "node_id", "filters",
            "depth", "top_k", "budget", "capabilities", "plan_digest"
        }
        unknown = set(data.keys()) - allowed_keys
        if unknown:
            _fail(
                "invalid_request",
                "unknown fields in structured query: %s" % sorted(unknown),
                "use only intent, query, symbol, node_id, filters, depth, top_k, budget",
            )
        intent = data.get("intent")
        if not intent:
            _fail(
                "invalid_request",
                "structured query requires 'intent'",
                "specify a supported intent like 'find_symbol', 'search_code', etc.",
            )
        raw_filters = data.get("filters")
        if raw_filters is not None and not isinstance(raw_filters, Mapping):
            _fail(
                "invalid_request",
                "filters must be a mapping",
                "provide a dictionary of filter name to value",
            )
        return cls(
            intent=str(intent),
            query=data.get("query"),
            symbol=data.get("symbol"),
            node_id=data.get("node_id"),
            filters=dict(raw_filters) if raw_filters else {},
            depth=data.get("depth", 1),
            top_k=data.get("top_k", 10),
            budget=data.get("budget", MAX_BUDGET),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh, JSON-ready plan envelope with a deterministic digest."""
        result = self._canonical_payload()
        result["capabilities"] = list(self.capabilities)
        result["plan_digest"] = self.plan_digest
        return result


_FIELD_VALUE_RE = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*(?:=|:(?!:))\s*(?P<value>`[^`]*`|'[^']*'|\"[^\"]*\"|[^\s,;]+)"
)
_MISSING_FIELD_RE = re.compile(
    r"\b(language|lang|path|path_prefix|file|kind|depth|top_k|budget|symbol|node_id)\s*(?:=|:(?!:))\s*(?=$|[,;])",
    re.IGNORECASE,
)
_UNDER_RE = re.compile(
    r"\b(?:under|within)\s+(?P<value>`[^`]*`|'[^']*'|\"[^\"]*\"|[^\s,;]+)",
    re.IGNORECASE,
)
_FILE_RE = re.compile(
    r"\b(?:in\s+file:|file:)\s*(?P<value>`[^`]*`|'[^']*'|\"[^\"]*\"|[^\s,;]+)",
    re.IGNORECASE,
)
_COMPOUND_INTENTS_RE = re.compile(
    r"^(?:(?:please\s+)?(?:find|inspect|show|get|search)\s+)?(?:the\s+)?"
    r"(?P<a1>callers?|callees?|definition|symbol|slice)\s+and\s+"
    r"(?P<a2>callers?|callees?|definition|symbol|slice)\s+of\s+(?P<target>.+)$",
    re.IGNORECASE,
)
_LANGUAGE_PHRASE_RE = re.compile(
    r"\bin\s+(?P<value>%s)(?=$|\s|[,;])" % "|".join(re.escape(x) for x in _KNOWN_LANGUAGE_PHRASES),
    re.IGNORECASE,
)
_ASSIGNMENT_KEYS = frozenset(
    {"language", "lang", "path", "path_prefix", "file", "kind", "depth", "top_k", "budget", "symbol", "node_id"}
)
_FILTER_ASSIGNMENT_KEYS = frozenset({"language", "lang", "path", "path_prefix", "file", "kind"})


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    characters = list(text)
    for start, end in spans:
        for index in range(start, end):
            characters[index] = " "
    return " ".join("".join(characters).split())


def _parse_integer_field(name: str, value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        _fail(
            "invalid_request",
            "%s must be a whole number" % name,
            "use a decimal integer, for example %s=2" % name,
        )
    return int(value)


class QueryPlanner:
    """Parse supported retrieval phrases into a bounded :class:`QueryPlanAST`.

    Parsing consists only of fixed regular expressions and one bounded pass over
    the input. Unsupported clauses raise once with a correction hint; the
    planner never retries, expands syntax, or makes network/LLM calls.
    """

    def plan(
        self,
        natural_language: str,
        *,
        filters: Optional[Mapping[str, Any]] = None,
        depth: Optional[int] = None,
        top_k: Optional[int] = None,
        budget: Optional[int] = None,
        symbol: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> QueryPlanAST:
        if not isinstance(natural_language, str):
            _fail(
                "invalid_request",
                "query text must be a string",
                "supply plain text, such as search code for payment retry",
            )
        text = _clean_query(natural_language)
        if text is None:
            _fail(
                "invalid_request",
                "query is empty",
                "try search code for payment retry or callers of package.module.function",
            )
        if len(text.encode("utf-8")) > MAX_QUERY_BYTES:
            _fail(
                "invalid_request",
                "query exceeds the %d-byte limit" % MAX_QUERY_BYTES,
                "shorten the query to at most %d UTF-8 bytes" % MAX_QUERY_BYTES,
            )
        self._reject_query_syntax(text)

        extracted_filters: dict[str, str] = {}
        extracted_values: dict[str, str] = {}
        spans: list[tuple[int, int]] = []

        if _MISSING_FIELD_RE.search(text):
            field_name = _MISSING_FIELD_RE.search(text).group(1)  # type: ignore[union-attr]
            _fail(
                "invalid_request",
                "%s field is missing its value" % field_name,
                "provide a value or remove that field",
            )

        for pattern, field_name in ((_FILE_RE, "path_prefix"),):
            for match in pattern.finditer(text):
                raw_value = _unquote(match.group("value"), field_name=field_name)
                canonical_value = self._normalize_filter(field_name, raw_value)
                self._merge_value(extracted_filters, field_name, canonical_value)
                spans.append(match.span())

        for match in _FIELD_VALUE_RE.finditer(text):
            if any(start < match.end() and match.start() < end for start, end in spans):
                continue
            key = match.group("key").casefold()
            if key not in _ASSIGNMENT_KEYS:
                _fail(
                    "invalid_request",
                    "unknown query field %r" % match.group("key"),
                    "remove the unknown query field or use language, path_prefix, kind, depth, top_k, budget, symbol, or node_id",
                )
            raw_value = _unquote(match.group("value"), field_name=key)
            if key in _FILTER_ASSIGNMENT_KEYS:
                canonical_key = {"lang": "language", "path": "path_prefix", "file": "path_prefix"}.get(key, key)
                canonical_value = self._normalize_filter(canonical_key, raw_value)
                self._merge_value(extracted_filters, canonical_key, canonical_value)
            else:
                self._merge_value(extracted_values, key, raw_value)
            spans.append(match.span())

        for pattern, field_name in ((_UNDER_RE, "path_prefix"), (_LANGUAGE_PHRASE_RE, "language")):
            for match in pattern.finditer(text):
                if any(start < match.end() and match.start() < end for start, end in spans):
                    continue
                raw_value = _unquote(match.group("value"), field_name=field_name)
                canonical_value = self._normalize_filter(field_name, raw_value)
                self._merge_value(extracted_filters, field_name, canonical_value)
                spans.append(match.span())

        working = _remove_spans(text, spans).strip(" ,;:")
        self._reject_unsupported_semantics(working)

        parsed_options: dict[str, Any] = {}
        for name, keyword_value in (
            ("depth", depth),
            ("top_k", top_k),
            ("budget", budget),
        ):
            text_value = extracted_values.get(name)
            if text_value is not None:
                parsed_options[name] = _parse_integer_field(name, text_value)
            if keyword_value is not None:
                if text_value is not None and parsed_options[name] != keyword_value:
                    _fail(
                        "invalid_request",
                        "conflicting values were supplied for %s" % name,
                        "keep one value for each option",
                    )
                parsed_options[name] = keyword_value

        explicit_symbol = extracted_values.get("symbol")
        explicit_node_id = extracted_values.get("node_id")
        if symbol is not None:
            if explicit_symbol is not None and _normalize_symbol(explicit_symbol) != _normalize_symbol(symbol):
                _fail(
                    "invalid_request",
                    "conflicting symbol values were supplied",
                    "keep one symbol for the query",
                )
            explicit_symbol = symbol
        if node_id is not None:
            if explicit_node_id is not None and _normalize_node_id(explicit_node_id) != _normalize_node_id(node_id):
                _fail(
                    "invalid_request",
                    "conflicting node_id values were supplied",
                    "keep one node_id for the query",
                )
            explicit_node_id = node_id

        provided_filters = _normalize_filters(filters)
        for name, value in provided_filters.items():
            self._merge_value(extracted_filters, name, value)

        intent, query_text, parsed_symbol, parsed_node_id, inferred_kind = self._classify(
            working,
            explicit_symbol=explicit_symbol,
            explicit_node_id=explicit_node_id,
        )
        if parsed_symbol is not None and explicit_symbol is not None:
            if _normalize_symbol(parsed_symbol) != _normalize_symbol(explicit_symbol):
                _fail(
                    "invalid_request",
                    "query names more than one symbol",
                    "keep a single symbol in the intent phrase or symbol field",
                )
        if parsed_node_id is not None and explicit_node_id is not None:
            if _normalize_node_id(parsed_node_id) != _normalize_node_id(explicit_node_id):
                _fail(
                    "invalid_request",
                    "query names more than one node_id",
                    "keep one node_id in the intent phrase or node_id field",
                )
        final_symbol = parsed_symbol or explicit_symbol
        final_node_id = parsed_node_id or explicit_node_id

        if inferred_kind is not None:
            self._merge_value(extracted_filters, "kind", inferred_kind)

        unused_values = set(extracted_values) - set(_FILTER_ASSIGNMENT_KEYS) - {"depth", "top_k", "budget", "symbol", "node_id"}
        if unused_values:
            _fail(
                "invalid_request",
                "query contains unsupported fields",
                "use only documented query filters and options",
            )
        if explicit_symbol is not None and intent not in _SYMBOL_INTENTS:
            _fail(
                "invalid_request",
                "symbol is not valid for this intent",
                "use symbol with find_symbol, callers, or callees",
            )
        if explicit_node_id is not None and intent != "neighbors":
            _fail(
                "invalid_request",
                "node_id is only valid for neighbors",
                "use node_id with the neighbors intent",
            )

        return QueryPlanAST(
            intent=intent,
            query=query_text,
            symbol=final_symbol,
            node_id=final_node_id,
            filters=extracted_filters,
            depth=parsed_options.get("depth", 1),
            top_k=parsed_options.get("top_k", 10),
            budget=parsed_options.get("budget", MAX_BUDGET),
        )

    def plan_structured(self, data: Mapping[str, Any]) -> QueryPlanAST:
        """Construct and validate a QueryPlanAST from a structured dictionary."""
        return QueryPlanAST.from_dict(data)

    def plan_composite(
        self,
        natural_language: str,
        *,
        filters: Optional[Mapping[str, Any]] = None,
        depth: Optional[int] = None,
        top_k: Optional[int] = None,
        budget: Optional[int] = None,
        symbol: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> tuple[QueryPlanAST, ...]:
        """Safely plan composite queries (e.g. 'callers of X and callees of Y' or 'callers and callees of X').

        Returns a tuple of 1 or 2 QueryPlanAST instances. If the query is not compound, returns (single_plan,).
        """
        if not isinstance(natural_language, str):
            _fail(
                "invalid_request",
                "query text must be a string",
                "supply plain text",
            )
        text = _clean_query(natural_language)
        if text is None:
            _fail(
                "invalid_request",
                "query is empty",
                "try search code for payment retry or callers of package.module.function",
            )
        self._reject_query_syntax(text)

        compound_match = _COMPOUND_INTENTS_RE.fullmatch(text)
        if compound_match:
            a1 = compound_match.group("a1").casefold()
            a2 = compound_match.group("a2").casefold()
            target = compound_match.group("target").strip()

            def _action_to_phrase(act: str, tgt: str) -> str:
                act = act.rstrip("s")
                if act in ("caller", "callee"):
                    return f"{act}s of {tgt}"
                elif act in ("definition", "symbol", "slice"):
                    return f"find symbol {tgt}"
                return f"search code for {tgt}"

            phrase1 = _action_to_phrase(a1, target)
            phrase2 = _action_to_phrase(a2, target)
            plan1 = self.plan(phrase1, filters=filters, depth=depth, top_k=top_k, budget=budget)
            plan2 = self.plan(phrase2, filters=filters, depth=depth, top_k=top_k, budget=budget)
            return (plan1, plan2)

        if " and " in text.lower():
            if re.search(r"\bor\b", text, re.IGNORECASE):
                _fail(
                    "unsupported",
                    "disjunctive (OR) queries are not supported",
                    "submit queries separately",
                    clauses=("or",),
                )
            parts = re.split(r"\s+and\s+", text, flags=re.IGNORECASE)
            if len(parts) == 2:
                p1, p2 = parts[0].strip(), parts[1].strip()
                if p1 and p2:
                    plan1 = self.plan(p1, filters=filters, depth=depth, top_k=top_k, budget=budget, symbol=symbol, node_id=node_id)
                    plan2 = self.plan(p2, filters=filters, depth=depth, top_k=top_k, budget=budget, symbol=symbol, node_id=node_id)
                    return (plan1, plan2)
            elif len(parts) > 2:
                _fail(
                    "unsupported",
                    "more than two conjoined queries are not supported",
                    "split into at most two queries joined by 'and'",
                    clauses=("multiple conjunctions",),
                )

        return (self.plan(text, filters=filters, depth=depth, top_k=top_k, budget=budget, symbol=symbol, node_id=node_id),)

    plan_composite_query = plan_composite

    @staticmethod
    def _normalize_filter(name: str, value: Any) -> str:
        if name == "language":
            return _normalize_language(value)
        if name == "path_prefix":
            return _normalize_path_prefix(value)
        if name == "kind":
            return _normalize_kind(value)
        _fail(
            "invalid_request",
            "unknown filter %r" % name,
            "use only language, path_prefix, and kind filters",
        )

    @staticmethod
    def _merge_value(target: dict[str, Any], name: str, value: Any) -> None:
        if name in target and target[name] != value:
            _fail(
                "invalid_request",
                "conflicting values were supplied for %s" % name,
                "keep one value for each field",
            )
        target[name] = value

    @staticmethod
    def _reject_query_syntax(text: str) -> None:
        if "```" in text or any(token in text for token in ("&&", "||", "!=", "<=", ">=", "<>")):
            _fail(
                "invalid_request",
                "query contains executable or compound query syntax",
                "use a supported natural-language phrase and named filters instead",
            )
        if ";" in text or "{" in text or "}" in text:
            _fail(
                "invalid_request",
                "query contains statement or block syntax",
                "submit one natural-language retrieval request",
            )
        if re.search(
            r"\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|MATCH\s*\(|UNWIND\s+|CREATE\s*\(|MERGE\s*\()",
            text,
            re.IGNORECASE,
        ):
            _fail(
                "invalid_request",
                "SQL or graph query syntax is not accepted",
                "describe a supported intent such as search code for retry or callers of module.function",
            )

    @staticmethod
    def _reject_unsupported_semantics(text: str) -> None:
        clauses = tuple(
            dict.fromkeys(
                match.group(0).casefold()
                for match in re.finditer(
                    r"\b(?:all|every|each|without|not|never|none|unauthenticated|unauthorized|unverified)\b",
                    text,
                    re.IGNORECASE,
                )
            )
        )
        if clauses:
            _fail(
                "unsupported",
                "negation, universal claims, and authorization conclusions are not searchable filters",
                "split this into a supported retrieval query, then review the relevant evidence manually",
                clauses=clauses,
            )

    @staticmethod
    def _classify(
        text: str,
        *,
        explicit_symbol: Optional[str],
        explicit_node_id: Optional[str],
    ) -> tuple[str, Optional[str], Optional[str], Optional[str], Optional[str]]:
        candidate = text.strip().strip(" .?!,")
        if not candidate:
            _fail(
                "needs_context",
                "an intent is required",
                "use find symbol, search code for, callers of, callees of, neighbors of, or recall lessons about",
            )

        if re.search(r"\b(?:and|or)\b", candidate, re.IGNORECASE):
            _fail(
                "unsupported",
                "multiple intents or boolean combinations are not supported",
                "submit one intent per query, using one symbol or search topic",
                clauses=("boolean combination",),
            )

        symbol_pattern = r"(?P<symbol>[\w$]+(?:(?:\.|::)[\w$]+)*)"
        exact_patterns = (
            ("find_symbol", r"^(?:please\s+)?(?:find|locate|show|inspect|get|fetch|lookup)\s+(?:the\s+)?(?:symbol|definition|declaration|signature)(?:\s+of)?\s+" + symbol_pattern + r"$"),
            ("find_symbol", r"^(?:symbol|definition|declaration|signature)\s+of\s+" + symbol_pattern + r"$"),
            ("find_symbol", r"^(?:where\s+is|where's)\s+(?:the\s+)?(?:symbol\s+|definition\s+of\s+)?" + symbol_pattern + r"(?:\s+defined)?$"),
            ("callers", r"^(?:(?:please\s+)?(?:find|show|list|inspect|get)\s+)?(?:the\s+)?(?:callers?|incoming\s+calls?)\s+(?:of|to)\s+" + symbol_pattern + r"$"),
            ("callers", r"^(?:who|what)\s+calls\s+" + symbol_pattern + r"$"),
            ("callers", r"^(?:(?:please\s+)?find\s+)?(?:the\s+)?(?:functions?|methods?)\s+(?:that|which)\s+call\s+" + symbol_pattern + r"$"),
            ("callees", r"^(?:(?:please\s+)?(?:find|show|list|inspect|get)\s+)?(?:the\s+)?(?:callees?|outgoing\s+calls?)\s+(?:of|from)\s+" + symbol_pattern + r"$"),
            ("callees", r"^(?:what|who)\s+does\s+" + symbol_pattern + r"\s+call$"),
            ("callees", r"^(?:(?:please\s+)?find\s+)?(?:the\s+)?(?:functions?|methods?)\s+called\s+by\s+" + symbol_pattern + r"$"),
        )
        for intent, pattern in exact_patterns:
            match = re.fullmatch(pattern, candidate, re.IGNORECASE)
            if match:
                return intent, None, match.group("symbol"), None, None

        neighbor_patterns = (
            r"^(?:(?:please\s+)?(?:find|show|list)\s+)?(?:the\s+)?neighbors?(?:\s+of)?$",
            r"^(?:(?:please\s+)?(?:find|show|list)\s+)?(?:the\s+)?neighbors?\s+of\s+(.+)$",
        )
        if any(re.fullmatch(pattern, candidate, re.IGNORECASE) for pattern in neighbor_patterns):
            if explicit_node_id:
                return "neighbors", None, None, explicit_node_id, None
            _fail(
                "needs_context",
                "neighbors needs a stable node_id, not a guessed symbol name",
                "resolve the symbol in the current graph and pass a stable node_id=<id>",
            )

        if re.fullmatch(
            r"^(?:(?:please\s+)?(?:find|show|list|inspect|get)\s+)?(?:callers?|incoming\s+calls?)$",
            candidate,
            re.IGNORECASE,
        ):
            if explicit_symbol:
                return "callers", None, explicit_symbol, None, None
            _fail("needs_context", "callers needs a symbol", "add 'of <qualified-symbol>'")
        if re.fullmatch(
            r"^(?:(?:please\s+)?(?:find|show|list|inspect|get)\s+)?(?:callees?|outgoing\s+calls?)$",
            candidate,
            re.IGNORECASE,
        ):
            if explicit_symbol:
                return "callees", None, explicit_symbol, None, None
            _fail("needs_context", "callees needs a symbol", "add 'of <qualified-symbol>'")
        if re.fullmatch(
            r"^(?:please\s+)?(?:find|locate|show|inspect|get|fetch|lookup)\s+(?:the\s+)?(?:symbol|definition|declaration|signature)$",
            candidate,
            re.IGNORECASE,
        ):
            if explicit_symbol:
                return "find_symbol", None, explicit_symbol, None, None
            _fail("needs_context", "find_symbol needs a symbol", "add one qualified symbol name")

        if re.fullmatch(
            r"(?:(?:please\s+)?(?:search(?:\s+code)?(?:\s+for)?|look\s+for|find(?:\s+code)?(?:\s+for)?|query(?:\s+code)?(?:\s+for)?|inspect(?:\s+code)?(?:\s+for)?|scan(?:\s+code)?(?:\s+for)?|grep(?:\s+code)?(?:\s+for)?))",
            candidate,
            re.IGNORECASE,
        ):
            _fail(
                "needs_context",
                "search_code needs search terms",
                "add search terms, for example search code for payment retry",
            )
        if re.fullmatch(
            r"(?:(?:please\s+)?(?:recall|search|find|show|retrieve|fetch|query|lookup)\s+(?:past\s+)?(?:lessons?|patterns?|learnings?|memories|memory)|lessons?|patterns?|learnings?|memories|memory)",
            candidate,
            re.IGNORECASE,
        ):
            _fail(
                "needs_context",
                "recall_lesson needs a topic",
                "add a topic after 'lessons about', for example retry handling",
            )

        lesson = re.fullmatch(
            r"(?:(?:please\s+)?(?:recall|search|find|show|retrieve|fetch|query|lookup)\s+(?:past\s+)?(?:lessons?|patterns?|learnings?|memories|memory)|lessons?|patterns?|learnings?|memories|memory)\s+(?:about|for|related\s+to|on)\s+(.+)",
            candidate,
            re.IGNORECASE,
        )
        if lesson:
            return "recall_lesson", _clean_query(lesson.group(1)), None, None, None

        search = re.fullmatch(
            r"(?:(?:please\s+)?(?:search(?:\s+code)?(?:\s+for)?|look\s+for|find(?:\s+code)?(?:\s+for)?|query(?:\s+code)?(?:\s+for)?|inspect(?:\s+code)?(?:\s+for)?|scan(?:\s+code)?(?:\s+for)?|grep(?:\s+code)?(?:\s+for)?))\s+(.+)",
            candidate,
            re.IGNORECASE,
        )
        if search:
            target = _clean_query(search.group(1)) or ""
            named = re.fullmatch(
                r"(function|functions|method|methods|class|classes|type|types|module|modules|fragment|fragments)\s+(?:named|called)\s+(" + symbol_pattern + r")",
                target,
                re.IGNORECASE,
            )
            if named:
                return "find_symbol", None, named.group(2), None, _normalize_kind(named.group(1))
            kind_phrase = re.fullmatch(
                r"(function|functions|method|methods|class|classes|type|types|module|modules|fragment|fragments)(?:\s+(?:about|for|related\s+to)\s+(.+)|\s+(.+))?",
                target,
                re.IGNORECASE,
            )
            inferred_kind = None
            if kind_phrase:
                inferred_kind = _normalize_kind(kind_phrase.group(1))
                target = _clean_query(kind_phrase.group(2) or kind_phrase.group(3)) or ""
            return "search_code", target or None, None, None, inferred_kind

        if re.fullmatch(r"^(?:neighbors?|neighbours?)(?:\s+of)?$", candidate, re.IGNORECASE):
            if explicit_node_id:
                return "neighbors", None, None, explicit_node_id, None
            _fail(
                "needs_context",
                "neighbors needs a stable node_id",
                "resolve the symbol in the current graph and pass a stable node_id=<id>",
            )

        _fail(
            "unsupported",
            "query does not match a supported retrieval intent",
            "use find symbol <name>, search code for <terms>, callers of <name>, callees of <name>, neighbors with node_id=<id>, or recall lessons about <topic>",
        )


__all__ = [
    "MAX_BUDGET",
    "MAX_DEPTH",
    "MAX_QUERY_BYTES",
    "MAX_TOP_K",
    "QueryPlanAST",
    "QueryPlanner",
    "QueryPlannerError",
    "SUPPORTED_FILTERS",
    "SUPPORTED_INTENTS",
    "SUPPORTED_KINDS",
]
