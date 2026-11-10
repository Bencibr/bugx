"""Reciprocal Rank Fusion for lexical and vector retrieval results.

Rankings are sequences of candidate identifiers in best-first order. Duplicate
identifiers within a ranking are removed before ranks are assigned. Candidates
missing from a ranking receive no contribution from that ranking.
"""

from __future__ import annotations

import math
import sys
from typing import Hashable, Iterable, List, Optional, Tuple, TypeVar


Candidate = TypeVar("Candidate", bound=Hashable)
FusedCandidate = Tuple[Candidate, float]


class RRFEngine:
    """Fuse two ranked candidate lists with deterministic exact-symbol priority.

    ``fuse`` returns ``(candidate, score)`` pairs in descending score order.
    Score ties preserve first-seen order (lexical results before vector
    results). Identifiers in ``exact_symbol_ids`` are placed in a separate
    priority tier, ahead of all other ranked candidates; this explicit tier is
    necessary because a lexical weight alone cannot guarantee exact matches
    rank first.

    The lexical weight is intentionally constrained to at least 1.2. This is a
    weighting floor, not a substitute for the exact-symbol priority tier.
    """

    DEFAULT_K = 60
    MIN_LEXICAL_WEIGHT = 1.2

    def __init__(self, k: int = DEFAULT_K, w_lex: float = MIN_LEXICAL_WEIGHT,
                 w_vec: float = 1.0) -> None:
        if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
            raise ValueError("k must be a positive integer")
        if k > sys.float_info.max:
            raise ValueError("k is too large for finite RRF scores")
        self.k = k
        self.w_lex = self._validate_weight("w_lex", w_lex)
        if self.w_lex < self.MIN_LEXICAL_WEIGHT:
            raise ValueError("w_lex must be at least 1.2")
        self.w_vec = self._validate_weight("w_vec", w_vec)

    @staticmethod
    def _validate_weight(name: str, weight: float) -> float:
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise ValueError("%s must be a finite positive number" % name)
        normalized = float(weight)
        if not math.isfinite(normalized) or normalized <= 0.0:
            raise ValueError("%s must be a finite positive number" % name)
        return normalized

    @staticmethod
    def _unique_ranked(values: Iterable[Candidate], name: str) -> List[Candidate]:
        if isinstance(values, (str, bytes, bytearray)):
            raise TypeError("%s must be a sequence of candidate identifiers" % name)
        result: List[Candidate] = []
        seen = set()
        try:
            iterator = iter(values)
        except TypeError as exc:
            raise TypeError("%s must be iterable" % name) from exc

        for candidate in iterator:
            try:
                duplicate = candidate in seen
            except TypeError as exc:
                raise TypeError("candidate identifiers must be hashable") from exc
            if not duplicate:
                seen.add(candidate)
                result.append(candidate)
        return result

    @staticmethod
    def _exact_id_set(values: Iterable[Candidate]) -> set:
        # A single string identifier is a common exact-symbol value; do not
        # accidentally interpret it as a sequence of one-character IDs.
        if isinstance(values, (str, bytes, bytearray)):
            values = (values,)  # type: ignore[assignment]
        try:
            result = set(values)
        except TypeError as exc:
            raise TypeError("exact_symbol_ids must contain hashable identifiers") from exc
        return result

    def fuse(
        self,
        lexical_ranked: Iterable[Candidate],
        vector_ranked: Iterable[Candidate] = (),
        *,
        exact_symbol_ids: Iterable[Candidate] = (),
        top_k: Optional[int] = None,
    ) -> List[FusedCandidate]:
        """Return candidates fused by weighted reciprocal rank.

        Ranks start at 1 and use the formula ``weight / (k + rank)``. Exact
        symbol IDs are prioritized only when they also occur in one of the two
        supplied rankings, so this method never fabricates candidates. If
        ``top_k`` is given, it must be a non-negative integer and truncates the
        final priority-sorted result.
        """
        if top_k is not None and (
            isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 0
        ):
            raise ValueError("top_k must be a non-negative integer")

        lexical = self._unique_ranked(lexical_ranked, "lexical_ranked")
        vector = self._unique_ranked(vector_ranked, "vector_ranked")
        exact_ids = self._exact_id_set(exact_symbol_ids)

        scores = {}
        first_seen = {}
        for ranked, weight in ((lexical, self.w_lex), (vector, self.w_vec)):
            for rank, candidate in enumerate(ranked, start=1):
                if candidate not in first_seen:
                    first_seen[candidate] = len(first_seen)
                    scores[candidate] = 0.0
                scores[candidate] += weight / (self.k + rank)

        ordered = sorted(
            scores,
            key=lambda candidate: (
                candidate not in exact_ids,
                -scores[candidate],
                first_seen[candidate],
            ),
        )
        if top_k is not None:
            ordered = ordered[:top_k]
        return [(candidate, scores[candidate]) for candidate in ordered]


__all__ = ["RRFEngine"]
