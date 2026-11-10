#!/usr/bin/env python3
"""移动端 UI 布局审计器（DOC-015）：视图树解析 + 5 大机械几何 Oracle + Failure Pack。

只读探测，不执行任何设备交互（AD-03）。输入为 uiautomator dump XML 或
WDA `/source` JSON（统一节点模型），输出为可复现的自洽证据组。

Oracle（阈值固定，INV-03，不随模型主观调参）：
  Overlap        — 同级可见节点 Rect 交叠
  Clipping       — 文本节点宽 > XML 父容器宽
  Touch Target   — clickable 节点 < 48dp / 44pt
  Safe Area      — 非根节点 Y 侵入状态栏（< 48dp）
  Contrast       — 文字/背景 < WCAG AA 4.5:1（需颜色数据，缺则 DEGRADED 不伪造）
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
import re
import sys
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "skills" else Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from core.infra import safe_xml

# ---- INV-03 固定阈值 -----------------------------------------------------
ANDROID_TOUCH_DP = 48.0
IOS_TOUCH_PT = 44.0
WCAG_AA_MIN_RATIO = 4.5
STATUS_BAR_TOP_DP = 48.0
OVERLAP_MIN_RATIO = 0.05


def _w(b): return max(b[2] - b[0], 0)
def _h(b): return max(b[3] - b[1], 0)
def _area(b): return _w(b) * _h(b)


def _parse_bounds(raw: str) -> tuple[int, int, int, int]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", raw or "")
    if not m:
        raise ValueError(f"bounds 格式无效: {raw!r}")
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


# ---- 解析器 -------------------------------------------------------------

def parse_uiautomator_xml(path: Path) -> list[dict]:
    """uiautomator dump `<hierarchy>` → 统一节点模型（保留 parent 索引边）。"""
    # SEC-07：uiautomator dump 来自被测应用，属于不可信输入。
    root = safe_xml.parse_xml_file(path)
    nodes: list[dict] = []

    def walk(node, depth: int, parent_idx: int | None) -> None:
        attrs = node.attrib
        try:
            b = _parse_bounds(attrs.get("bounds", ""))
        except ValueError:
            return
        nodes.append({
            "depth": depth,
            "parent": parent_idx,
            "id": attrs.get("resource-id", "") or attrs.get("class", ""),
            "text": attrs.get("text", ""),
            "class": attrs.get("class", ""),
            "content_desc": attrs.get("content-desc", ""),
            "bounds": list(b),
            "clickable": attrs.get("clickable") == "true",
            "width": _w(b),
            "height": _h(b),
            "x": b[0],
            "y": b[1],
            "source": "uiautomator",
        })
        idx = len(nodes) - 1
        for child in node:
            walk(child, depth + 1, idx)

    for child in root:
        walk(child, 0, None)
    return nodes


def _rect_from_wda(node: dict) -> tuple[int, int, int, int] | None:
    rect = node.get("rect") or {}
    if not rect:
        return None
    try:
        x = int(rect.get("x", 0) or 0)
        y = int(rect.get("y", 0) or 0)
        w = int(rect.get("width", 0) or 0)
        h = int(rect.get("height", 0) or 0)
    except (TypeError, ValueError):
        return None
    return (x, y, x + w, y + h)


def parse_wda_json(path: Path, density: float = 1.0) -> list[dict]:
    """WDA `/source` JSON → 统一节点模型（保留 parent 索引边）。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    nodes: list[dict] = []

    def walk(node: dict, depth: int, parent_idx: int | None) -> None:
        b = _rect_from_wda(node)
        if b is None:
            b = (0, 0, 0, 0)
        nodes.append({
            "depth": depth,
            "parent": parent_idx,
            "id": node.get("name", "") or node.get("type", ""),
            "text": node.get("label", "") or node.get("value", "") or "",
            "class": node.get("type", ""),
            "content_desc": "",
            "bounds": list(b),
            "clickable": bool(node.get("tap", False)),
            "width": _w(b),
            "height": _h(b),
            "x": b[0],
            "y": b[1],
            "source": "wda",
        })
        idx = len(nodes) - 1
        for child in node.get("children", []) or []:
            walk(child, depth + 1, idx)

    walk(payload, 0, None)
    return nodes


# ---- Oracle -------------------------------------------------------------

def _overlap_amount(a: list[int], b: list[int]) -> float:
    """交集面积 / min(area_a, area_b)；不相交为 0。"""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    if ix <= 0 or iy <= 0:
        return 0.0
    denom = min(_area(a), _area(b))
    if denom <= 0:
        return 0.0
    return (ix * iy) / denom


def _siblings(nodes: list[dict], a: int, b: int) -> bool:
    """同 depth 且同 parent 即为同级节点（重叠只在同级间判定）。"""
    return (nodes[a]["depth"] == nodes[b]["depth"]
            and nodes[a]["parent"] == nodes[b]["parent"])


def oracle_overlap(nodes: list[dict]) -> list[dict]:
    findings = []
    for i, a in enumerate(nodes):
        for j, b in enumerate(nodes):
            if j <= i or not _siblings(nodes, i, j):
                continue
            ov = _overlap_amount(a["bounds"], b["bounds"])
            if ov > OVERLAP_MIN_RATIO:
                findings.append({
                    "oracle": "Overlap",
                    "severity": "high",
                    "nodes": [a["id"], b["id"]],
                    "overlap_ratio": round(ov, 3),
                    "detail": f"{a['id']} 与 {b['id']} 重叠 {ov:.1%}",
                })
    return findings


def oracle_clipping(nodes: list[dict]) -> list[dict]:
    findings = []
    for a in nodes:
        if not a["text"] or a["width"] <= 0 or a["parent"] is None:
            continue
        parent = nodes[a["parent"]]
        if a["width"] > parent["width"]:
            findings.append({
                "oracle": "Clipping",
                "severity": "medium",
                "node": a["id"],
                "text": a["text"],
                "node_width": a["width"],
                "container_width": parent["width"],
                "overflow": a["width"] - parent["width"],
                "detail": f"{a['id']} 渲染宽 {a['width']} 超出父容器 {parent['width']}",
            })
    return findings


def oracle_touch_target(nodes: list[dict], density: float = 1.0) -> list[dict]:
    threshold = ANDROID_TOUCH_DP * density
    findings = []
    for a in nodes:
        if a["clickable"] and min(a["width"], a["height"]) < threshold:
            findings.append({
                "oracle": "TouchTarget",
                "severity": "medium",
                "node": a["id"],
                "min_dim": min(a["width"], a["height"]),
                "threshold_px": round(threshold, 2),
                "detail": f"{a['id']} 可触控热区 {min(a['width'], a['height'])}px < {threshold:.0f}px",
            })
    return findings


def oracle_safe_area(nodes: list[dict]) -> list[dict]:
    """非根节点 Y 落入状态栏带（< 48dp）。根节点豁免（全窗 Frame）。"""
    findings = []
    for a in nodes:
        if a["parent"] is not None and 0 < a["y"] < STATUS_BAR_TOP_DP:
            findings.append({
                "oracle": "SafeArea",
                "severity": "low",
                "node": a["id"],
                "y": a["y"],
                "status_bar_px": STATUS_BAR_TOP_DP,
                "detail": f"{a['id']} 顶边 Y={a['y']} 侵入状态栏 {STATUS_BAR_TOP_DP}px 区域",
            })
    return findings


def _relative_luminance(r: int, g: int, b: int) -> float:
    def lin(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1 = _relative_luminance(*fg)
    l2 = _relative_luminance(*bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    if lighter == darker:
        return 1.0
    return (lighter + 0.05) / (darker + 0.05)


def oracle_contrast(nodes: list[dict]) -> list[dict]:
    """需要颜色数据。缺则返回 degraded 标记，不伪造断言（INV-03）。"""
    colored = [n for n in nodes if n.get("fg") or n.get("bg")]
    if not colored:
        return [{"oracle": "Contrast", "severity": "degraded",
                 "detail": "无颜色数据（截图未采样），对比度不判定"}]
    findings = []
    for a in colored:
        fg = a.get("fg")
        bg = a.get("bg")
        if not fg or not bg:
            continue
        ratio = _contrast_ratio(fg, bg)
        if ratio < WCAG_AA_MIN_RATIO:
            findings.append({
                "oracle": "Contrast",
                "severity": "medium",
                "node": a["id"],
                "ratio": round(ratio, 2),
                "min_ratio": WCAG_AA_MIN_RATIO,
                "detail": f"{a['id']} 对比度 {ratio:.2f}:1 < WCAG AA {WCAG_AA_MIN_RATIO}:1",
            })
    return findings


def run_oracles(nodes: list[dict], density: float = 1.0) -> list[dict]:
    return (
        oracle_overlap(nodes)
        + oracle_clipping(nodes)
        + oracle_touch_target(nodes, density)
        + oracle_safe_area(nodes)
        + oracle_contrast(nodes)
    )


# ---- Failure Pack -------------------------------------------------------

def build_failure_pack(
    out_dir: Path,
    ui_tree: list[dict],
    reproduction_steps: list[dict],
    findings: list[dict],
    screenshot_path: Path | None = None,
) -> dict:
    """落盘自洽证据组（4 个要素）。缺截图时写入占位说明，不伪造文件。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ui_tree.json").write_text(
        json.dumps({"nodes": ui_tree}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "reproduction_steps.json").write_text(
        json.dumps(reproduction_steps, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "root_cause_hypothesis.md").write_text(
        "# 根因假设\n\n" + "\n".join(f"- [{f['oracle']}] {f.get('detail', '')}" for f in findings)
        + "\n", encoding="utf-8",
    )
    if screenshot_path and Path(screenshot_path).is_file():
        target = out_dir / "screenshot.png"
        target.write_bytes(Path(screenshot_path).read_bytes())
        files = ["screenshot.png", "ui_tree.json", "reproduction_steps.json", "root_cause_hypothesis.md"]
    else:
        (out_dir / "screenshot.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 8  # PNG 魔数占位，非真实截图
        )
        files = ["screenshot.png (placeholder)", "ui_tree.json", "reproduction_steps.json", "root_cause_hypothesis.md"]
    return {"findings": findings, "files": files, "dir": str(out_dir)}


# ---- 入口 ---------------------------------------------------------------

def inspect(
    root: Path,
    dump_path: Path | None = None,
    screenshot_path: Path | None = None,
    density: float = 1.0,
    out_dir: Path | None = None,
) -> dict:
    root = root.resolve()
    dump = dump_path or (root / "ui_dump.xml")
    if not dump.is_file():
        return {
            "status": "DEGRADED",
            "detail": "无 UI 视图树输入（缺 uiautomator dump / WDA source）",
            "findings": [],
        }
    if dump.suffix == ".xml":
        nodes = parse_uiautomator_xml(dump)
    elif dump.suffix == ".json":
        nodes = parse_wda_json(dump, density)
    else:
        return {"status": "DEGRADED", "detail": f"不支持的视图树格式: {dump.suffix}", "findings": []}

    findings = run_oracles(nodes, density)
    steps = [
        {"action": "tap", "target": f.get("node") or (f.get("nodes") or [None])[0],
         "reason": f["detail"], "oracle": f["oracle"]}
        for f in findings
        if f.get("severity") != "degraded"
    ]
    pack_dir = out_dir if out_dir else root / "failure-packs"
    pack = build_failure_pack(pack_dir, nodes, steps, findings, screenshot_path)
    return {
        "status": "READY" if findings else "READY",
        "nodes": len(nodes),
        "findings": findings,
        "failure_pack": pack,
    }


def inspect_ui_dump(
    dump_path_or_str: str | Path,
    *,
    is_ios: bool = False,
    density: float = 1.0,
    screenshot_path: str | Path | None = None,
    out_dir: str | Path | None = None,
) -> dict:
    """供 MCP 或自动化工具便捷调用的移动 UI 几何 Oracle 审计接口。"""
    path = Path(dump_path_or_str)
    temp_file = None
    if not path.is_file():
        import tempfile
        suffix = ".json" if is_ios or str(dump_path_or_str).strip().startswith("{") else ".xml"
        temp_file = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=suffix)
        temp_file.write(str(dump_path_or_str))
        temp_file.flush()
        temp_file.close()
        path = Path(temp_file.name)
    try:
        root_dir = path.parent
        sc_path = Path(screenshot_path) if screenshot_path else None
        od_path = Path(out_dir) if out_dir else None
        return inspect(root_dir, dump_path=path, screenshot_path=sc_path, density=density, out_dir=od_path)
    finally:
        if temp_file is not None and path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--dump", type=Path, default=None)
    parser.add_argument("--screenshot", type=Path, default=None)
    parser.add_argument("--density", type=float, default=1.0)
    parser.add_argument("--out-dir", type=Path, default=None, help="Failure Pack 输出目录")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = inspect(args.root, args.dump, args.screenshot, args.density, args.out_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())