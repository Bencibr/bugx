"""AI 覆盖复核机制（DOC-AIREV）：覆盖清单污染由宿主 AI 逐目录排查定案。

设计约束（2026-11-04 复盘固化，替代"白名单简单排除"）：

- 引擎只做确定性枚举与证据打包；"这个目录是不是产品代码"的判定权属于
  宿主 AI（复核包内含内容摘录与 import 可达性信号，AI 必须基于证据判定）。
- AI 裁决只能**移出**单元（排除），不能添加——发现层的确定性下限不被
  AI 放大；裁决持久化到 ``.bugx/knowledge/coverage-review.json``，带
  裁决理由/时间/裁决者/证据包摘要，可审计可回滚（重审覆盖即可推翻）。
- 硬排除（依赖/生成物/引擎自产物）保持确定性，不经 AI。
- 换项目零迁移：候选组按当次发现结果动态生成，不携带任何项目名清单。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

_EXCERPT_BYTES = 2400
_SAMPLE_FILES = 8
_EXCERPT_FILES = 2

VALID_VERDICTS = ("exclude", "product")


def verdicts_path(root: Path) -> Path:
    return Path(root) / ".bugx" / "knowledge" / "coverage-review.json"


def load_verdicts(root: Path) -> dict[str, Any]:
    p = verdicts_path(root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def excluded_top_dirs(root: Path) -> dict[str, str]:
    """AI 复核定案的排除目录：{top_dir: reason}。"""
    data = load_verdicts(root)
    out: dict[str, str] = {}
    for top_dir, item in (data.get("verdicts") or {}).items():
        if (
            isinstance(item, dict)
            and item.get("verdict") == "exclude"
            and isinstance(item.get("reason"), str)
        ):
            out[str(top_dir)] = item["reason"]
    return out


def candidate_groups(
    root: Path,
    source_paths: list[Path] | None = None,
    asset_paths: list[Path] | None = None,
) -> list[dict[str, Any]]:
    """把发现结果按顶层目录聚组，产出待 AI 复核的候选组与证据包。

    已有 AI 裁决的目录不重复生成；硬排除目录本来就不会出现在发现结果里。
    """
    root = Path(root).resolve()
    if source_paths is None or asset_paths is None:
        # DES-02 禁双向对：symbol_index 在发现侧消费本模块（AI 排除裁决），
        # 本模块若反向 import symbol_index 即成运行期环路。调用方
        # （coverage_cli review-packages）负责传入发现结果；缺参即报错，
        # 不做隐式反向依赖。
        raise TypeError(
            "candidate_groups 需要 source_paths/asset_paths（由调用方从 "
            "symbol_index.discover_* 传入，禁止本模块反向 import 形成双向依赖对）"
        )
    existing = set((load_verdicts(root).get("verdicts") or {}).keys())
    groups: dict[str, dict[str, list[Path]]] = {}
    for kind, paths in (("source", source_paths or []), ("asset", asset_paths or [])):
        for p in paths:
            rel = Path(p).relative_to(root)
            if len(rel.parts) < 2:
                continue  # 根下散文件不构成目录组
            top = rel.parts[0]
            if top in existing:
                continue
            groups.setdefault(top, {"source": [], "asset": []})[kind].append(rel)
    packages: list[dict[str, Any]] = []
    for top in sorted(groups):
        files = sorted(set(groups[top]["source"]) | set(groups[top]["asset"]))
        exts = Counter(f.suffix for f in files)
        excerpts: list[dict[str, str]] = []
        for sample in files[:_EXCERPT_FILES]:
            try:
                raw = (root / sample).read_bytes()[:_EXCERPT_BYTES]
                excerpts.append({
                    "path": sample.as_posix(),
                    "head": raw.decode("utf-8", errors="replace"),
                })
            except OSError:
                continue
        packages.append({
            "top_dir": top,
            "file_count": len(files),
            "kinds": {k: len(v) for k, v in groups[top].items() if v},
            "extensions": dict(exts.most_common(8)),
            "samples": [f.as_posix() for f in files[:_SAMPLE_FILES]],
            "excerpts": excerpts,
        })
    return packages


def write_review_packages(root: Path, packages: list[dict[str, Any]]) -> Path:
    out = Path(root) / ".bugx" / "state" / "coverage-review"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"packages-{int(time.time())}.json"
    payload = {
        "schema_version": 1,
        "generated_at": time.time(),
        "instruction": (
            "宿主 AI 必须逐目录基于证据（文件名/扩展分布/内容摘录）判定 "
            "product（产品代码，保留审计）或 exclude（非产品：证据快照/练"
            "习靶场/CI 配置/文档资产等），并给出可审计理由。裁决只允许移出，"
            "不允许添加。写入 verdicts JSON 后执行 "
            "bugx coverage review-apply --verdicts <file>"
        ),
        "packages": packages,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def build_verdicts_template(root: Path, packages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "reviewed_by": "",
        "verdicts": {
            pkg["top_dir"]: {"verdict": "", "reason": "", "evidence_digest": pkg["extensions"]}
            for pkg in packages
        },
    }


def apply_verdicts(root: Path, verdicts_file: Path, reviewed_by: str) -> dict[str, Any]:
    """校验并持久化宿主 AI 的目录裁决（原子写，含审计字段）。"""
    raw = json.loads(Path(verdicts_file).read_text(encoding="utf-8"))
    incoming = raw.get("verdicts") if isinstance(raw, dict) else None
    if not isinstance(incoming, dict) or not incoming:
        raise ValueError("verdicts 文件缺少非空 verdicts 对象")
    reviewer = str(reviewed_by or raw.get("reviewed_by") or "").strip()
    if not reviewer:
        raise ValueError("必须声明 reviewed_by（宿主 AI 身份），拒绝匿名裁决")
    merged = load_verdicts(root)
    store = merged.get("verdicts") if isinstance(merged.get("verdicts"), dict) else {}
    applied = 0
    for top_dir, item in incoming.items():
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if verdict not in VALID_VERDICTS:
            raise ValueError(f"{top_dir}: verdict 必须是 {'/'.join(VALID_VERDICTS)}")
        if len(reason) < 8:
            raise ValueError(f"{top_dir}: reason 必须给出可审计理由（≥8 字符）")
        store[str(top_dir)] = {
            "verdict": verdict,
            "reason": reason,
            "reviewed_by": reviewer,
            "reviewed_at": time.time(),
        }
        applied += 1
    merged["schema_version"] = 1
    merged["verdicts"] = store
    merged["updated_at"] = time.time()
    out = verdicts_path(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    return {"applied": applied, "path": str(out), "reviewed_by": reviewer}


__all__ = [
    "candidate_groups", "write_review_packages", "build_verdicts_template",
    "apply_verdicts", "load_verdicts", "excluded_top_dirs", "verdicts_path",
    "VALID_VERDICTS",
]
