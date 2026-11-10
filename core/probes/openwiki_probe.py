#!/usr/bin/env python3
"""OpenWiki 探针：探测目标仓库的 wiki 叙述层可用性与新鲜度（DOC-032）。

定位（DOC-032 §接入设计）：OpenWiki 是第五层知识——AST 事实 / 关系图 /
语义索引 / 错题经验之上的"项目叙述层"，供勘察与假设生成参考。它是 LLM
归纳的**待验证假设**，不是符号级事实，也永不进入 Finding 证据链；缺失
不构成降级（状态 ABSENT = 维持现状，附安装建议）。

本探针永远 exit 0（纯建议性，不阻断、不交互），与 efficiency_probe 的
DEGRADED-决策流不同：OpenWiki 的"回退"就是不使用它，无需用户决策。
"""

from __future__ import annotations
# --- 引擎导入根（直接执行脚本时自举 core.* 命名空间）---
import sys as _sys
from pathlib import Path as _Path

for _p in _Path(__file__).resolve().parents:
    if (_p / "core" / "bugx.md").is_file():
        if str(_p) not in _sys.path:
            _sys.path.insert(0, str(_p))
        break
del _p

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

WIKI_DIR = "openwiki"
META_DIR = ".openwiki"
# 实测（A/B 0.6.83）：OpenWiki 把元数据写在 openwiki/ 目录内而非项目根
# .openwiki/；页面按 architecture/components/concepts/operations 子目录
# 分布，不存在根级 index.md。
MANIFEST = "openwiki/.page-manifest.json"
LAST_UPDATE = "openwiki/.last-update.json"
INSTALL_HINT = "npm install -g openwiki && openwiki --init（需 Node.js ≥ 22；模型经 OPENWIKI_PROVIDER 配置，或以 MCP 模式复用宿主 agent）"


def _git_last_commit_time(root: Path) -> int | None:
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            cwd=str(root), capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return int((result.stdout or "").strip())
    except ValueError:
        return None


def _wiki_freshness(root: Path, wiki_time: int | None, status: str) -> tuple[str, str]:
    """返回 (freshness, note)：fresh / stale / unknown / interrupted。"""
    if status == "interrupted":
        return "interrupted", "上次生成未完成（status=interrupted），建议重跑 openwiki --init/--update"
    if wiki_time is None:
        return "unknown", "无法读取 wiki 更新时间"
    head_time = _git_last_commit_time(root)
    if head_time is None:
        return "unknown", "仓库无 git 历史可比对"
    if wiki_time >= head_time:
        return "fresh", "wiki 晚于最近一次提交"
    return "stale", "wiki 落后于最近一次提交（建议 openwiki --update）"


def inspect(root: Path) -> dict:
    root = root.resolve()
    manifest_path = root / MANIFEST
    last_update_path = root / LAST_UPDATE

    cli_path = shutil.which("openwiki")
    cli_version = ""
    if cli_path:
        try:
            result = subprocess.run(
                [cli_path, "--version"], capture_output=True, text=True,
                timeout=10, check=False,
            )
            cli_version = (result.stdout or result.stderr or "").strip()[:80]
        except (OSError, subprocess.TimeoutExpired):
            cli_version = ""

    wiki_dir = root / WIKI_DIR
    wiki_present = wiki_dir.is_dir() and not wiki_dir.is_symlink() and any(
        path.is_file() and not path.is_symlink() and path.suffix == ".md"
        for path in wiki_dir.rglob("*.md"))
    pages = 0
    claims = 0
    last_update = ""
    generation_status = ""
    wiki_time: int | None = None
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(manifest, dict):
                manifest_pages = manifest.get("pages")
                if isinstance(manifest_pages, dict):
                    pages = len(manifest_pages)
        except (OSError, ValueError):
            pass
    if last_update_path.is_file() and not last_update_path.is_symlink():
        try:
            update_info = json.loads(last_update_path.read_text(encoding="utf-8"))
            if isinstance(update_info, dict):
                last_update = str(update_info.get("updatedAt") or "")
                generation_status = str(update_info.get("status") or "")
                updated_at = update_info.get("updatedAt")
                if isinstance(updated_at, str) and updated_at.endswith("Z"):
                    from datetime import datetime
                    try:
                        wiki_time = int(datetime.fromisoformat(
                            updated_at.replace("Z", "+00:00")).timestamp())
                    except ValueError:
                        wiki_time = None
        except (OSError, ValueError):
            pass
    claims_dir = wiki_dir / ".claims"
    if claims_dir.is_dir():
        claims = sum(1 for f in claims_dir.rglob("*.json") if f.is_file())

    status = "ABSENT"
    freshness, note = "none", "尚未生成 wiki"
    if wiki_present:
        freshness, note = _wiki_freshness(root, wiki_time, generation_status)
        status = "READY" if freshness in {"fresh", "unknown"} else "STALE"
    if not cli_path and not wiki_present:
        status = "ABSENT"

    recommendations = []
    if status == "ABSENT":
        recommendations.append(
            "未使用叙述层 wiki（可选增强，DOC-032）；不影响审计，如需自建见 "
            "docs/DOC-032_OPENWIKI_NARRATIVE_LAYER_PLAN.md")
    elif status == "STALE":
        if freshness == "interrupted":
            recommendations.append(
                "上次生成未完成（status=interrupted）：重跑 openwiki --init 或 "
                "openwiki --update 补齐 wiki 后再进入勘察")
        else:
            recommendations.append("wiki 落后于代码：运行 openwiki --update 刷新后再进入勘察")
    else:
        recommendations.append("wiki 可作为勘察参考与假设来源；其断言仍须以 "
                              "symbol_index/project_graph 事实核验后方可作为 Finding 依据")

    return {
        "probe": "openwiki",
        "status": status,
        "freshness": freshness,
        "note": note,
        "cli": {"found": bool(cli_path), "version": cli_version},
        "wiki": {
            "present": wiki_present, "pages": pages, "claims": claims,
            "last_update": last_update,
        },
        "recommendations": recommendations,
    }


def print_report(report: dict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    status = report["status"]
    print(f"[openwiki] {status}（{report['freshness']}）")
    wiki = report["wiki"]
    if report["status"] == "READY":
        print(f"  wiki 就绪：{wiki['pages']} 页 / {wiki['claims']} 条溯源声明，"
              f"最后更新 {wiki['last_update'] or '未知'}")
    for item in report["recommendations"]:
        print(f"  → {item}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check",), nargs="?", default="check")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = inspect(args.root.resolve())
    print_report(report, getattr(args, "json", False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
