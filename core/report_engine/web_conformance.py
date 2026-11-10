#!/usr/bin/env python3
"""BugX Web 标准合规基线。

这是 Web 项目的最小、无第三方依赖的静态基线和动态检查清单生成器。
它不声称替代 W3C/WHATWG 验证器或辅助技术测试：静态问题可直接定位，
键盘、最终样式、ARIA 树和缩放等运行时结论必须由浏览器证据确认。
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
import sys
from pathlib import Path


import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import os
from typing import Any


SCHEMA_VERSION = 1
STANDARD_ORACLES = (
    "WCAG 2.2 AA",
    "WAI-ARIA 1.2",
    "HTML/WHATWG semantic conformance",
)
WEB_FRONTEND_FRAMEWORK_DEPS = {
    "react", "react-dom", "vue", "svelte", "@angular/core", "next", "nuxt",
    "astro", "solid-js", "lit", "preact", "alpinejs",
}
WEB_MARKERS = {
    "vite.config.js", "vite.config.ts", "next.config.js",
    "next.config.mjs", "nuxt.config.ts", "angular.json", "astro.config.mjs",
    "svelte.config.js", "index.html",
}
WEB_EXTENSIONS = {".html", ".htm", ".xhtml", ".vue", ".svelte", ".jsx", ".tsx"}
HTML_DOCUMENT_EXTENSIONS = {".html", ".htm", ".xhtml"}
SKIP_DIRS = {
    ".git", ".bugx", ".opencode", ".codex", ".claude", ".pi", "node_modules",
    ".venv", "venv", "env", "__pycache__", "dist", "build", ".next", ".nuxt",
}
MAX_FILES = 5000
MAX_BYTES = 2 * 1024 * 1024
VALID_DYNAMIC_STATUSES = {"PASS", "FAIL", "BLOCKED", "UNVERIFIED", "DEFERRED"}

DYNAMIC_RULES: tuple[tuple[str, str], ...] = (
    ("keyboard-focus", "键盘可操作、焦点可见且不被遮挡，无焦点陷阱"),
    ("aria-tree", "最终 Accessibility Tree 的角色、名称、状态和关系正确"),
    ("contrast", "最终渲染样式满足文字、非文本和焦点指示器对比度要求"),
    ("reflow-zoom", "200% 放大、400% 重排/320 CSS px 和方向变化后仍可操作"),
    ("state-feedback", "loading/error/empty/disabled 状态有可感知且语义正确的反馈"),
)


class WebConformanceError(ValueError):
    """Web 合规输入或证据无效。"""


@dataclass
class Element:
    tag: str
    attrs: dict[str, str]
    line: int
    column: int
    text: str = ""
    enclosed_by_label: bool = False


class _MarkupParser(HTMLParser):
    """保留足够的 DOM 事实，避免把容错解析误报成完整标准验证。"""

    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self._stack: list[Element] = []

    @staticmethod
    def _attrs(raw: list[tuple[str, str | None]]) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in raw:
            result[key.lower()] = "" if value is None else value
        return result

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        element = Element(
            tag=normalized,
            attrs=self._attrs(attrs),
            line=self.getpos()[0],
            column=self.getpos()[1],
            enclosed_by_label=any(parent.tag == "label" for parent in self._stack),
        )
        self.elements.append(element)
        if normalized not in self.VOID_TAGS:
            self._stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self._stack and self._stack[-1].tag == tag.lower():
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].tag != normalized:
                continue
            self._stack[index].text = self._stack[index].text.strip()
            del self._stack[index:]
            return

    def handle_data(self, data: str) -> None:
        if not data.strip():
            return
        for element in self._stack:
            element.text += data


def _walk_files(root: Path, *, limit: int = MAX_FILES) -> tuple[list[Path], bool]:
    files: list[Path] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS and not name.startswith(".")]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            files.append(path)
            if len(files) >= limit:
                truncated = True
                return files, truncated
    return files, truncated


def detect_web_project(root: Path, *, files: list[Path] | None = None) -> bool:
    """只按可观察项目事实判断 Web，不把宿主配置或纯后端 package.json 当作前端 Web 项目。"""
    root = Path(root).resolve()
    paths = files if files is not None else _walk_files(root)[0]
    names = {path.name.lower() for path in paths}
    if names & {name.lower() for name in WEB_MARKERS}:
        return True
    if any(path.suffix.lower() in WEB_EXTENSIONS for path in paths):
        return True
    if "package.json" in names:
        for path in paths:
            if path.name.lower() == "package.json":
                try:
                    data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
                    if isinstance(data, dict):
                        deps = set((data.get("dependencies") or {}).keys()) | set((data.get("devDependencies") or {}).keys())
                        if deps & WEB_FRONTEND_FRAMEWORK_DEPS:
                            return True
                except Exception:
                    pass
    return False


def plan_metadata(root: Path) -> dict[str, Any]:
    files, truncated = _walk_files(Path(root).resolve())
    applicable = detect_web_project(root, files=files)
    return {
        "required": applicable,
        "status": "REQUIRED" if applicable else "NOT_APPLICABLE",
        "oracles": list(STANDARD_ORACLES) if applicable else [],
        "static_rules": [
            "html-lang", "img-alt", "control-name", "interactive-name",
            "duplicate-id", "aria-reference",
        ] if applicable else [],
        "dynamic_rules": [rule_id for rule_id, _ in DYNAMIC_RULES] if applicable else [],
        "file_scan_truncated": truncated,
    }


def _check(rule_id: str, oracle: str, status: str, *, path: str = "", line: int = 0,
           message: str, evidence: str = "") -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "oracle": oracle,
        "status": status,
        "path": path,
        "line": line,
        "message": message,
        "evidence": evidence or (f"{path}:{line}" if path and line else ""),
    }


def _has_name(element: Element, ids: set[str], labelled_ids: set[str]) -> bool:
    attrs = element.attrs
    if attrs.get("aria-label", "").strip():
        return True
    labelled_by = attrs.get("aria-labelledby", "").split()
    if labelled_by and all(item in ids for item in labelled_by):
        return True
    if attrs.get("title", "").strip():
        return True
    if element.text.strip():
        return True
    return bool(element.attrs.get("id") and element.attrs["id"] in labelled_ids)


def _check_document(path: Path, root: Path) -> list[dict[str, Any]]:
    relative = path.relative_to(root).as_posix()
    try:
        if path.stat().st_size > MAX_BYTES:
            return [_check(
                "static-size-limit", "HTML/WHATWG semantic conformance", "BLOCKED",
                path=relative, line=1, message="文档超过静态检查大小上限，不能声称已检查",
            )]
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [_check(
            "static-read", "HTML/WHATWG semantic conformance", "BLOCKED",
            path=relative, line=1, message=f"无法读取 Web 文档：{exc}",
        )]
    parser = _MarkupParser()
    try:
        parser.feed(source)
        parser.close()
    except (RuntimeError, ValueError) as exc:
        return [_check(
            "static-parse", "HTML/WHATWG semantic conformance", "BLOCKED",
            path=relative, line=1, message=f"静态标记解析失败：{exc}",
        )]

    elements = parser.elements
    ids = {element.attrs["id"] for element in elements if element.attrs.get("id", "").strip()}
    occurrences: defaultdict[str, list[Element]] = defaultdict(list)
    for element in elements:
        value = element.attrs.get("id", "").strip()
        if value:
            occurrences[value].append(element)
    labelled_ids = {
        element.attrs["for"].strip()
        for element in elements
        if element.tag == "label" and element.attrs.get("for", "").strip()
    }
    results: list[dict[str, Any]] = []
    html_elements = [element for element in elements if element.tag == "html"]
    if path.suffix.lower() in HTML_DOCUMENT_EXTENSIONS:
        if not html_elements:
            results.append(_check(
                "html-lang", "HTML/WHATWG semantic conformance", "FAIL",
                path=relative, line=1, message="HTML 文档缺少根 html 元素，无法确认 lang",
            ))
        elif not html_elements[0].attrs.get("lang", "").strip():
            results.append(_check(
                "html-lang", "WCAG 2.2 AA", "FAIL", path=relative,
                line=html_elements[0].line, message="文档必须声明非空 lang 属性",
            ))

    for value, same_ids in occurrences.items():
        if len(same_ids) > 1:
            results.append(_check(
                "duplicate-id", "WAI-ARIA 1.2", "FAIL", path=relative,
                line=same_ids[1].line, message=f"id={value!r} 在同一文档中重复",
            ))

    for element in elements:
        line = element.line
        if element.tag == "img" and "alt" not in element.attrs:
            results.append(_check(
                "img-alt", "WCAG 2.2 AA", "FAIL", path=relative, line=line,
                message="img 必须提供 alt（装饰图也应明确使用 alt=\"\"）",
            ))
        if element.tag in {"input", "select", "textarea"}:
            if element.tag == "input" and element.attrs.get("type", "").lower() == "hidden":
                continue
            if not _has_name(element, ids, labelled_ids) and not element.enclosed_by_label:
                results.append(_check(
                    "control-name", "WCAG 2.2 AA / WAI-ARIA 1.2", "FAIL", path=relative,
                    line=line, message="表单控件缺少可访问名称（label、aria-label 或 aria-labelledby）",
                ))
        if element.tag == "button" and not _has_name(element, ids, labelled_ids):
            results.append(_check(
                "interactive-name", "WCAG 2.2 AA / WAI-ARIA 1.2", "FAIL", path=relative,
                line=line, message="button 缺少可访问名称",
            ))
        if element.tag == "a" and "href" in element.attrs and not _has_name(element, ids, labelled_ids):
            results.append(_check(
                "interactive-name", "WCAG 2.2 AA / WAI-ARIA 1.2", "FAIL", path=relative,
                line=line, message="带 href 的链接缺少可访问名称",
            ))
        for attr in ("aria-labelledby", "aria-describedby"):
            refs = element.attrs.get(attr, "").split()
            missing = [ref for ref in refs if ref not in ids]
            if missing:
                results.append(_check(
                    "aria-reference", "WAI-ARIA 1.2", "FAIL", path=relative, line=line,
                    message=f"{attr} 引用了不存在的 ID：{', '.join(missing)}",
                ))
    return results


def _dynamic_checks(
    evidence_path: Path | None, *, preflight: bool = False
) -> list[dict[str, Any]]:
    if evidence_path is None and preflight:
        return [
            _check(
                f"dynamic.{rule_id}",
                "WCAG 2.2 AA / WAI-ARIA 1.2",
                "DEFERRED",
                message=f"动态规则在运行期通过浏览器测试检验（{description}），预检阶段免检",
            )
            for rule_id, description in DYNAMIC_RULES
        ]
    payload: dict[str, Any] = {}
    if evidence_path is not None:
        try:
            loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [_check(
                "dynamic-evidence", "WCAG 2.2 AA / WAI-ARIA 1.2", "UNVERIFIED",
                path=str(evidence_path), line=1, message=f"动态证据不可读取或不是 JSON：{exc}",
            )]
        if not isinstance(loaded, dict) or not isinstance(loaded.get("checks"), dict):
            return [_check(
                "dynamic-evidence", "WCAG 2.2 AA / WAI-ARIA 1.2", "UNVERIFIED",
                path=str(evidence_path), line=1, message="动态证据必须是 {checks: {rule_id: {status,evidence}}}",
            )]
        payload = loaded["checks"]
    results: list[dict[str, Any]] = []
    for rule_id, description in DYNAMIC_RULES:
        item = payload.get(rule_id)
        if not isinstance(item, dict):
            results.append(_check(
                f"dynamic.{rule_id}", "WCAG 2.2 AA / WAI-ARIA 1.2", "BLOCKED",
                message=f"缺少浏览器动态证据：{description}",
            ))
            continue
        status = str(item.get("status", "")).upper()
        evidence = str(item.get("evidence", "")).strip()
        if status not in VALID_DYNAMIC_STATUSES:
            status = "UNVERIFIED"
            message = f"动态证据状态无效：{item.get('status')!r}"
        elif status in {"PASS", "FAIL"} and not evidence:
            status = "UNVERIFIED"
            message = "PASS/FAIL 必须携带可定位的浏览器证据"
        else:
            message = str(item.get("message") or description)
        results.append(_check(
            f"dynamic.{rule_id}", "WCAG 2.2 AA / WAI-ARIA 1.2", status,
            message=message, evidence=evidence,
        ))
    return results


def check_project(
    root: Path,
    *,
    dynamic_evidence: Path | None = None,
    preflight: bool = False,
) -> dict[str, Any]:
    root = Path(root).resolve()
    files, truncated = _walk_files(root)
    applicable = detect_web_project(root, files=files)
    if not applicable:
        return {
            "schema_version": SCHEMA_VERSION, "root": ".", "applicable": False,
            "status": "NOT_APPLICABLE", "oracles": [], "checks": [],
            "summary": {"PASS": 0, "FAIL": 0, "BLOCKED": 0, "UNVERIFIED": 0, "DEFERRED": 0},
        }
    documents = [path for path in files if path.suffix.lower() in HTML_DOCUMENT_EXTENSIONS | {".vue", ".svelte"}]
    checks: list[dict[str, Any]] = []
    if not documents:
        checks.append(_check(
            "static-documents", "HTML/WHATWG semantic conformance", "BLOCKED",
            message="Web 项目未发现可静态检查的 HTML/模板文档，不能声称完整合规",
        ))
    else:
        for document in documents:
            checks.extend(_check_document(document, root))
    if truncated:
        checks.append(_check(
            "static-scan-limit", "HTML/WHATWG semantic conformance", "BLOCKED",
            message=f"源码文件超过扫描上限 {MAX_FILES}，静态结果不完整",
        ))
    checks.extend(_dynamic_checks(dynamic_evidence, preflight=preflight))
    summary = Counter(item["status"] for item in checks)
    status = "FAIL" if summary["FAIL"] else "BLOCKED" if summary["BLOCKED"] or summary["UNVERIFIED"] else "PASS"
    return {
        "schema_version": SCHEMA_VERSION,
        "root": ".",
        "applicable": True,
        "status": status,
        "oracles": list(STANDARD_ORACLES),
        "checks": checks,
        "summary": {name: summary[name] for name in ("PASS", "FAIL", "BLOCKED", "UNVERIFIED", "DEFERRED")},
    }


def audit_web_conformance(
    root: Path | str = ".",
    *,
    files: list[str] | None = None,
    dynamic_evidence: Path | str | None = None,
) -> dict[str, Any]:
    """供 Python 模块或 MCP 工具调用的 Web 标准与可访问性合规基线审计引擎。"""
    project_root = Path(root).resolve()
    dyn = Path(dynamic_evidence).resolve() if dynamic_evidence else None
    report = check_project(project_root, dynamic_evidence=dyn)
    if files:
        file_set = {str(Path(f).as_posix()) for f in files}
        filtered_checks = [
            c for c in report.get("checks", [])
            if c.get("path") in file_set or any(f in c.get("path", "") for f in files)
        ]
        report["checks"] = filtered_checks
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "check"), nargs="?", default="check")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--dynamic-evidence", type=Path)
    parser.add_argument("--preflight", action="store_true", help="启动预检模式：动态规则免检并推迟至运行期")
    parser.add_argument("--strict", action="store_true", help="FAIL/BLOCKED/UNVERIFIED 均返回 2")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = plan_metadata(args.root) if args.command == "plan" else check_project(
            args.root, dynamic_evidence=args.dynamic_evidence, preflight=args.preflight,
        )
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            if args.command == "plan":
                print(f"[web_conformance] {payload['status']}：{', '.join(payload['oracles']) or '无'}")
            else:
                print(f"[web_conformance] {payload['status']}：{payload['summary']}")
                for item in payload["checks"]:
                    location = f" {item['path']}:{item['line']}" if item.get("path") else ""
                    print(f"  [{item['status']}] {item['rule_id']}{location}：{item['message']}")
        if args.command == "check" and args.strict and payload.get("status") not in {"PASS", "NOT_APPLICABLE"}:
            return 2
        return 0
    except (OSError, WebConformanceError) as exc:
        print(f"[web_conformance] ✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
