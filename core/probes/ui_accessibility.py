"""Runtime accessibility checks and declarative keyboard journey execution."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

AXE_VERSION = "4.14.0"
AXE_SHA256 = "20c09fe157a8a34a30e241aaa1fcdade657734f08ab379ecfbeb7d45cc46e878"
AXE_TAGS = ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa")


def load_axe() -> str:
    path = Path(__file__).resolve().parents[1] / "assets/axe-core/axe.min.js"
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != AXE_SHA256:
        raise ValueError("bundled axe-core hash mismatch")
    return source.decode("utf-8")


def scan_accessibility(page: Any, source: str, selector: str | None = None) -> dict:
    """Run the bundled engine and retain its automated and incomplete results."""
    from playwright.sync_api import Error

    metadata = {"version": AXE_VERSION, "sha256": AXE_SHA256, "tags": list(AXE_TAGS),
                "scope": selector or "document"}
    try:
        page.evaluate(source)
        result = page.evaluate(r"""
          async ({tags, version, selector}) => {
            if (window.axe?.version !== version) throw new Error('axe-core did not load');
            const context = selector ? document.querySelector(selector) : document;
            if (!context) throw new Error('accessibility scope is unavailable');
            let timer;
            try {
              const result = await Promise.race([
                axe.run(context, {runOnly: {type: 'tag', values: tags},
                                  resultTypes: ['violations', 'incomplete']}),
                new Promise((_, reject) => {
                  timer = setTimeout(() => reject(new Error('axe-core timed out')), 5000);
                }),
              ]);
              const summarize = (rules) => rules.map(rule => ({
                id: rule.id, impact: rule.impact, help: rule.help, url: rule.helpUrl,
                node_count: rule.nodes.length,
                targets: rule.nodes.slice(0, 16).map(node => node.target),
              }));
              return {violations: summarize(result.violations), incomplete: summarize(result.incomplete),
                passed_rules: result.passes.length};
            } finally {
              clearTimeout(timer);
            }
          }
        """, {"tags": list(AXE_TAGS), "version": AXE_VERSION, "selector": selector})
    except Error as exc:
        return {**metadata, "status": "BLOCKED", "reason": str(exc).splitlines()[0][:280]}
    status = "FAIL" if result["violations"] else (
        "UNVERIFIED" if result["incomplete"] or not result["passed_rules"] else "PASS"
    )
    return {**metadata, **result, "status": status}


_FOCUS_SAMPLE = r"""
({focus, indicator}) => {
  const active = document.activeElement;
  const identify = node => node && ({
    tag: node.localName, id: node.id || null, role: node.getAttribute('role')
  });
  const one = selector => {
    const rows = document.querySelectorAll(selector);
    return rows.length === 1 ? rows[0] : null;
  };
  const target = one(focus);
  const marker = one(indicator);
  const shown = node => {
    const box = node.getBoundingClientRect();
    if (!node.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) ||
        box.width <= 0 || box.height <= 0) return false;
    const left = Math.max(0, box.left), right = Math.min(innerWidth, box.right);
    const top = Math.max(0, box.top), bottom = Math.min(innerHeight, box.bottom);
    if (left >= right || top >= bottom) return false;
    const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
    return !!hit && (node.contains(hit) || hit.contains(node));
  };
  let paint = null;
  if (marker) {
    const s = getComputedStyle(marker);
    const outline = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0 &&
      s.outlineColor !== 'transparent' && !/,\s*0\)$/.test(s.outlineColor);
    const shadowColors = s.boxShadow.match(/rgba?\([^)]*\)/g) || [];
    const shadow = s.boxShadow !== 'none' &&
      shadowColors.some(color => !/,\s*0\)$/.test(color));
    paint = {
      outline: outline ? [s.outlineStyle, s.outlineWidth, s.outlineColor, s.outlineOffset] : null,
      shadow: shadow ? s.boxShadow : null,
      border: s.borderTopStyle !== 'none' && parseFloat(s.borderTopWidth) > 0 ?
        [s.borderTopWidth, s.borderTopStyle, s.borderTopColor] : null,
      background: s.backgroundColor,
    };
  }
  return {
    active: identify(active), matched: !!target && target === active,
    shown: !!target && shown(target), marker_shown: !!marker && shown(marker), paint,
  };
}
"""


def keyboard_journey(page: Any, steps: list[dict]) -> dict:
    """Press real browser keys and assert each declared focus transition."""
    from playwright.sync_api import TimeoutError

    results = []
    for step in steps:
        selector = step["focus"]
        targets = {"focus": selector, "indicator": step.get("indicator", selector)}
        before = page.evaluate(_FOCUS_SAMPLE, targets)
        page.keyboard.press(step["key"])
        try:
            page.wait_for_function(
                """selector => {
                  const nodes = document.querySelectorAll(selector);
                  return nodes.length === 1 && nodes[0] === document.activeElement;
                }""",
                arg=selector, timeout=900,
            ).dispose()
        except TimeoutError:
            pass
        observed = page.evaluate(_FOCUS_SAMPLE, targets)
        old, new = before["paint"], observed["paint"]
        if not observed["matched"] or not observed["shown"]:
            status, reason = "FAIL", "expected focus was not reached or was obscured"
        elif not observed["marker_shown"] or new is None:
            status, reason = "FAIL", "declared indicator target is missing or obscured"
        elif old is None or before["matched"]:
            status, reason = "UNVERIFIED", "no unfocused indicator sample for this step"
        elif any(new[field] and new[field] != old[field]
                 for field in ("outline", "shadow", "border", "background")):
            status, reason = "PASS", "focus target and indicator change observed"
        else:
            status, reason = "FAIL", "no visible change in the declared indicator styles"
        results.append({**step, "status": status, "reason": reason, "before": before, "after": observed})
        if status == "FAIL":
            break
    status = "FAIL" if any(row["status"] == "FAIL" for row in results) else (
        "UNVERIFIED" if not results or any(row["status"] == "UNVERIFIED" for row in results) else "PASS"
    )
    return {"status": status, "steps": results, "planned_steps": len(steps)}
