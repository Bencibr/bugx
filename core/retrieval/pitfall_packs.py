#!/usr/bin/env python3
"""core/retrieval/pitfall_packs.py — 按技术栈按需挂载的冷启动漏洞先验包检索引擎（P1 / T-005）。

零第三方依赖实现：
1. 加载并校验 ``core/audit_packs/pitfalls/*.json``；
2. 基于 ``project_profile``（``languages`` / ``frameworks`` / ``surfaces``）及可选文件扩展名
   执行技术栈硬过滤，杜绝跨语言噪声污染；
3. 双路倒数排名融合（RRF）：通道 A（技术栈与严重度先验）+ 通道 B（源码反模式信号命中度）；
4. 严格按 ``char_budget`` 渲染紧凑引导块供 ``worker_prior`` 注入。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_PITFALLS_DIR = Path(__file__).resolve().parents[1] / "audit_packs" / "pitfalls"
RRF_K = 60.0

_REQUIRED_FIELDS = (
    "pitfall_id",
    "cwe",
    "severity",
    "languages",
    "frameworks",
    "surfaces",
    "title",
    "anti_pattern_signals",
    "false_positive_guards",
    "poc_skeleton",
)

_SEVERITY_WEIGHT = {
    "critical": 4.0,
    "high": 3.0,
    "medium": 2.0,
    "low": 1.0,
}

_EXT_TO_LANGUAGE = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
}


class PitfallPackError(ValueError):
    """Raised when a pitfall pack JSON violates schema requirements."""


@dataclass(frozen=True)
class PitfallEntry:
    pack_id: str
    pitfall_id: str
    cwe: str
    severity: str
    languages: tuple[str, ...]
    frameworks: tuple[str, ...]
    surfaces: tuple[str, ...]
    title: str
    anti_pattern_signals: tuple[str, ...]
    false_positive_guards: tuple[str, ...]
    poc_skeleton: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "pitfall_id": self.pitfall_id,
            "cwe": self.cwe,
            "severity": self.severity,
            "languages": list(self.languages),
            "frameworks": list(self.frameworks),
            "surfaces": list(self.surfaces),
            "title": self.title,
            "anti_pattern_signals": list(self.anti_pattern_signals),
            "false_positive_guards": list(self.false_positive_guards),
            "poc_skeleton": self.poc_skeleton,
        }


def _norm_tokens(values: object) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    out: list[str] = []
    for item in values:
        if isinstance(item, str) and item.strip():
            norm = item.strip().lower()
            if norm not in out:
                out.append(norm)
    return tuple(out)


def load_pitfall_packs(packs_dir: Path | None = None) -> tuple[PitfallEntry, ...]:
    """Load and strictly validate all cold-start pitfall JSON packs."""
    root = Path(packs_dir) if packs_dir is not None else DEFAULT_PITFALLS_DIR
    if not root.is_dir():
        raise PitfallPackError(f"pitfall packs directory not found: {root}")
    entries: list[PitfallEntry] = []
    seen_ids: set[str] = set()
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PitfallPackError(f"invalid pitfall pack JSON {path.name}: {exc}") from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("pitfalls"), list):
            raise PitfallPackError(f"{path.name} must contain a 'pitfalls' list")
        pack_id = str(payload.get("pack_id") or path.stem).strip()
        for raw in payload["pitfalls"]:
            if not isinstance(raw, Mapping):
                raise PitfallPackError(f"{path.name} contains non-object pitfall entry")
            missing = [f for f in _REQUIRED_FIELDS if not raw.get(f)]
            if missing:
                raise PitfallPackError(f"{path.name} entry missing required fields: {missing}")
            pid = str(raw["pitfall_id"]).strip()
            if pid in seen_ids:
                raise PitfallPackError(f"duplicate pitfall_id detected: {pid}")
            seen_ids.add(pid)
            entries.append(
                PitfallEntry(
                    pack_id=pack_id,
                    pitfall_id=pid,
                    cwe=str(raw["cwe"]).strip(),
                    severity=str(raw["severity"]).strip().lower(),
                    languages=_norm_tokens(raw["languages"]),
                    frameworks=_norm_tokens(raw["frameworks"]),
                    surfaces=_norm_tokens(raw["surfaces"]),
                    title=str(raw["title"]).strip(),
                    anti_pattern_signals=tuple(
                        str(s) for s in raw["anti_pattern_signals"] if isinstance(s, str) and s.strip()
                    ),
                    false_positive_guards=tuple(
                        str(g).strip() for g in raw["false_positive_guards"] if isinstance(g, str) and g.strip()
                    ),
                    poc_skeleton=str(raw["poc_skeleton"]).strip(),
                )
            )
    return tuple(entries)


def _infer_languages_from_path(file_path: str) -> set[str]:
    if not file_path:
        return set()
    ext = Path(file_path).suffix.lower()
    lang = _EXT_TO_LANGUAGE.get(ext)
    return {lang} if lang else set()


def match_pitfalls(
    profile: Mapping[str, Any] | None = None,
    *,
    code_text: str = "",
    file_path: str = "",
    limit: int = 6,
    packs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Retrieve Top-K stack-matched cold-start pitfalls via dual-channel RRF."""
    if limit <= 0:
        return []
    entries = load_pitfall_packs(packs_dir=packs_dir)
    prof = profile if isinstance(profile, Mapping) else {}
    langs = set(_norm_tokens(prof.get("languages"))) | _infer_languages_from_path(file_path)
    frameworks = set(_norm_tokens(prof.get("frameworks")))
    surfaces = set(_norm_tokens(prof.get("surfaces")))
    code_lower = (code_text or "").lower()

    # 1. Hard stack filter (prevent cross-language contamination)
    eligible: list[tuple[PitfallEntry, float, int, list[str]]] = []
    for entry in entries:
        is_generic = "*" in entry.languages
        if not is_generic:
            if langs and not (langs & set(entry.languages)):
                continue
            if not langs and frameworks and not (frameworks & set(entry.frameworks)):
                continue

        matched_signals = [
            sig for sig in entry.anti_pattern_signals
            if sig.lower() in code_lower
        ]
        # Generic cross-language packs require surface or signal match to avoid noise
        if is_generic and not matched_signals and not (surfaces & set(entry.surfaces)):
            continue

        fw_overlap = len(frameworks & set(entry.frameworks)) if "*" not in entry.frameworks else 0
        surf_overlap = len(surfaces & set(entry.surfaces))
        lang_bonus = 2.0 if (not is_generic and (langs & set(entry.languages))) else 0.5
        prior_score = (
            _SEVERITY_WEIGHT.get(entry.severity, 1.0)
            + lang_bonus
            + (fw_overlap * 2.5)
            + (surf_overlap * 1.0)
        )
        eligible.append((entry, prior_score, len(matched_signals), matched_signals))

    if not eligible:
        return []

    # 2. Dual-channel RRF fusion (Channel A: stack prior, Channel B: code signal hits)
    rank_prior = {
        item[0].pitfall_id: idx
        for idx, item in enumerate(
            sorted(eligible, key=lambda x: (-x[1], x[0].pitfall_id)), 1
        )
    }
    rank_signal = {
        item[0].pitfall_id: idx
        for idx, item in enumerate(
            sorted(eligible, key=lambda x: (-x[2], -x[1], x[0].pitfall_id)), 1
        )
    }

    fused: list[tuple[float, PitfallEntry, list[str]]] = []
    for entry, _prior, sig_hits, matched_signals in eligible:
        pid = entry.pitfall_id
        # Give signal hits higher fusion weight when code_text is supplied
        w_sig = 1.6 if sig_hits > 0 else 0.6
        rrf_score = (1.0 / (RRF_K + rank_prior[pid])) + (w_sig / (RRF_K + rank_signal[pid]))
        fused.append((rrf_score, entry, matched_signals))

    fused.sort(key=lambda x: (-x[0], x[1].pitfall_id))
    results: list[dict[str, Any]] = []
    for score, entry, matched_signals in fused[:limit]:
        item = entry.to_dict()
        item["rrf_score"] = round(score, 6)
        item["matched_signals"] = matched_signals
        results.append(item)
    return results


def render_pitfall_guidance(
    matches: Sequence[Mapping[str, Any]],
    *,
    char_budget: int = 1800,
) -> str:
    """Render compact, budget-bounded cold-start pitfall guidance for Worker prompts."""
    if not matches or char_budget <= 0:
        return ""
    lines: list[str] = ["[冷启动漏洞先验雷达 (Cold-Start Pitfalls)]"]
    used = len(lines[0]) + 1
    for item in matches:
        signals = ", ".join(item.get("matched_signals") or ())
        sig_note = f" | 命中信号: {signals}" if signals else ""
        guard = (item.get("false_positive_guards") or [""])[0]
        block = (
            f"- [{item.get('pitfall_id')} / {item.get('cwe')} / {str(item.get('severity', '')).upper()}] "
            f"{item.get('title')}{sig_note}\n"
            f"  排除条件: {guard}\n"
            f"  PoC思路: {item.get('poc_skeleton')}"
        )
        if used + len(block) + 1 > char_budget:
            break
        lines.append(block)
        used += len(block) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


__all__ = [
    "DEFAULT_PITFALLS_DIR",
    "PitfallPackError",
    "PitfallEntry",
    "load_pitfall_packs",
    "match_pitfalls",
    "render_pitfall_guidance",
]
