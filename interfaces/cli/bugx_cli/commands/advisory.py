from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..common import _ensure_core_importable


def command_advisory(args: argparse.Namespace) -> int:
    """bugx advisory：生成、查看与导出 GitHub Security Advisory (GHSA) 标准安全公告与修复 PR 草案。"""
    subcommand = getattr(args, "advisory_command", None)
    if not subcommand:
        print("[bugx advisory] 错误: 请指定子命令 (export, list)")
        return 1

    try:
        _ensure_core_importable()
        from core.autonomous_cve.ghsa_advisories import GHSAAdvisoryGenerator, VulnerabilityCategory
    except Exception as exc:
        print(f"[bugx advisory] ✗ 无法加载 GHSA 模块: {exc}")
        return 1

    if subcommand == "list":
        # Look for existing advisories in standard locations
        candidates = [
            Path.cwd() / "evals" / "oss" / "ghsa_advisories.json",
            Path.cwd() / ".bugx" / "advisories.json",
        ]
        found = False
        for c in candidates:
            if c.exists():
                try:
                    data = json.loads(c.read_text(encoding="utf-8"))
                    advisories = data.get("advisories", [])
                    if getattr(args, "json", False):
                        print(json.dumps({"status": "ok", "count": len(advisories), "advisories": advisories}, indent=2, ensure_ascii=False))
                        return 0
                    print("=" * 80)
                    print(f"BugX Security Advisory Registry: {c.relative_to(Path.cwd()) if c.is_relative_to(Path.cwd()) else c}")
                    print("=" * 80)
                    for adv in advisories:
                        print(f"  [{adv['severity']}] {adv['ghsa_id']} ({adv['cve_id']}) -> {adv['package_name']} ({adv['ecosystem']}): {adv['summary']}")
                    print("=" * 80)
                    print(f"Total: {len(advisories)} advisories registered.")
                    found = True
                    break
                except Exception as e:
                    print(f"[bugx advisory] 警告: 读取 {c} 失败: {e}")
        if not found:
            if getattr(args, "json", False):
                print(json.dumps({"status": "ok", "count": 0, "advisories": []}))
            else:
                print("[bugx advisory] 当前项目未发现已注册的 GHSA 安全公告 (可使用 `bugx advisory export` 生成)")
        return 0

    if subcommand == "export":
        cve_id = getattr(args, "cve", "CVE-2026-9001") or "CVE-2026-9001"
        package_name = getattr(args, "package", "target-package") or "target-package"
        ecosystem = getattr(args, "ecosystem", "generic") or "generic"
        raw_cat = getattr(args, "category", "generic_logic") or "generic_logic"
        affected_symbol = getattr(args, "symbol", "processInput") or "processInput"
        target_file = getattr(args, "target", "src/core.py") or "src/core.py"
        fmt = getattr(args, "format", "markdown") or "markdown"
        out_path = getattr(args, "out", None)

        try:
            category = VulnerabilityCategory(raw_cat)
        except ValueError:
            category = VulnerabilityCategory.GENERIC_LOGIC

        advisory = GHSAAdvisoryGenerator.generate_advisory(
            cve_id=cve_id,
            package_name=package_name,
            ecosystem=ecosystem,
            category=category,
            affected_symbol=affected_symbol,
            target_file=str(target_file),
            receipt_sha256="0" * 64,
        )

        content = json.dumps(advisory.to_dict(), indent=2, ensure_ascii=False) if fmt == "json" else advisory.render_markdown()

        if out_path:
            p = Path(out_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            print(f"[bugx advisory] ✓ 成功导出 GHSA 安全公告至: {p}")
        else:
            print(content)
        return 0

    return 0
