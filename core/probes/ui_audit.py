#!/usr/bin/env python3
"""Audit a rendered Web UI against browser measurements and DESIGN.md."""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from importlib.util import find_spec
from multiprocessing import get_context
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.probes.ui_accessibility import keyboard_journey, load_axe, scan_accessibility
from core.probes.ui_contract import IDENTIFIER, UIContract, UIContractError, load_ui_contract
from core.probes.ui_evidence import combine_status, make_manifest, write_ui_evidence
from core.probes.ui_measurements import CHECK_JOURNEY, MEASURE_UI
from core.probes.ui_repair import repair_tokens
from core.probes.ui_review import review_ui, validate_model_tag
from core.probes.ui_storybook import STORY_ROOT, StorybookError, StorybookIndex, read_storybook, wait_for_story
from core.probes.ui_visual_diff import compare_visual
from core.probes.web_probe import WebDynamicProbe
from core.secure_atomic import atomic_write_json

DEFAULT_VIEWPORTS = (
    ("desktop", 1440, 900), ("tablet", 768, 1024),
    ("mobile", 390, 844), ("landscape", 844, 390),
)
ZOOM_BASE = {"width": 1280, "height": 1024}
ZOOMS = (2, 4)
TIMEOUT_MS = 5000
# 浏览器冷启动（尤其带扩展的 persistent context 首次注册 service worker）
# 与本机的页面等待不是一个量级；共用 5s 会把启动慢误报成 zoom 用例 BLOCKED。
BROWSER_START_TIMEOUT_MS = 20000


def origin(url: str) -> tuple[str, str, int]:
    """Return the canonical origin of an HTTP URL without embedded credentials."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
        raise UIContractError("expected an absolute HTTP(S) URL without credentials")
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def _viewport(raw: str) -> tuple[str, int, int]:
    try:
        name, size = raw.split("=", 1)
        width, height = map(int, size.lower().split("x", 1))
        if IDENTIFIER.fullmatch(name) and not name.startswith(("zoom-", "journey-", "keyboard-")):
            if 240 <= width <= 2560 and 240 <= height <= 1600:
                return name, width, height
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("viewport must be NAME=WIDTHxHEIGHT, e.g. mobile=390x844")


def _capture(
    page: Any, name: str, url: str, directory: Path, browser_version: str,
    *, axe: str, actions: list[dict] | None = None, after_navigation: Any = None,
    inspect_page: Any = None, expect_origin: bool = False, masks: tuple[str, ...] = (),
    story: dict | None = None,
) -> dict:
    from playwright.sync_api import Error

    def ready(current: Any) -> None:
        if (expect_origin or story) and origin(current.url) != origin(url):
            raise UIContractError("audit page navigated outside the configured origin")
        if story:
            wait_for_story(current, story, TIMEOUT_MS)
        else:
            current.locator("body").wait_for(state="visible", timeout=TIMEOUT_MS)
        if after_navigation is not None:
            after_navigation(current)

    def inspect(current: Any) -> dict:
        if (expect_origin or story) and origin(current.url) != origin(url):
            raise UIContractError("audit action navigated outside the configured origin")
        inspected = inspect_page(current) if inspect_page is not None else {}
        if (expect_origin or story) and origin(current.url) != origin(url):
            raise UIContractError("audit action navigated outside the configured origin")
        if story:
            wait_for_story(current, story, TIMEOUT_MS)
        return {**inspected, "axe": scan_accessibility(current, axe, STORY_ROOT if story else None)}

    try:
        raw = WebDynamicProbe.capture_browser_page(
            page, url, actions=actions, timeout=TIMEOUT_MS / 1000,
            screenshot_path=directory / f"{name}.png", screenshot_full_page=False,
            screenshot_masks=masks,
            browser_version=browser_version, after_navigation=ready, inspect_page=inspect,
        )
    except (Error, UIContractError, StorybookError) as exc:
        return {"name": name, "status": "BLOCKED", "reason": str(exc).splitlines()[0][:400],
                **({"story": story} if story else {})}
    evidence = raw["evidence"]
    # Keep browser diagnostics useful without retaining console messages or request URLs.
    for key in ("console", "page_errors", "request_failures", "http_errors"):
        evidence[f"{key}_count"] = len(evidence.pop(key))
    status = raw["http_status"]
    return {"name": name, "status": "PASS" if status and 200 <= status < 300 else "BLOCKED",
            **raw, **({"story": story} if story else {})}


def _measure(page: Any, name: str, contract: UIContract) -> dict:
    from playwright.sync_api import TimeoutError

    page.evaluate("() => Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, 1000))])"
                  ".then(() => undefined)")
    details = page.evaluate(MEASURE_UI, {
        "tokens": contract.tokens or {},
        "components": [row for row in contract.components if not row.get("profiles") or name in row["profiles"]],
        "contrast": list(contract.contrast), "reflow": list(contract.reflow),
    })
    actionable = [(rule, observed) for rule, observed in zip(contract.reflow, details["anchors"], strict=True)
                  if rule.get("interactive") and observed["status"] == "PASS"]
    if not actionable:
        return details
    scroll = page.evaluate_handle("""selectors => {
      const positions = new Map();
      for (const selector of selectors) {
        for (let node = document.querySelector(selector); node; node = node.parentElement) {
          positions.set(node, [node.scrollLeft, node.scrollTop]);
        }
      }
      return {x: scrollX, y: scrollY, positions};
    }""", [rule["selector"] for rule, _ in actionable])
    try:
        for rule, observed in actionable:
            try:
                page.locator(rule["selector"]).click(trial=True, timeout=1200)
            except TimeoutError:
                observed.update(status="FAIL", reason="control failed Playwright actionability checks")
    finally:
        scroll.evaluate("""({x, y, positions}) => {
          for (const [node, [left, top]] of positions)
            node.scrollTo({left, top, behavior: 'instant'});
          window.scrollTo({left: x, top: y, behavior: 'instant'});
        }""")
        scroll.dispose()
    return details


def _zoom_extension(directory: Path) -> Path:
    extension = directory / "extension"
    extension.mkdir()
    (extension / "manifest.json").write_text(json.dumps({
        "manifest_version": 3, "name": "BugX audit page zoom", "version": "1.0",
        "background": {"service_worker": "zoom.js"},
    }), encoding="utf-8")
    (extension / "zoom.js").write_text("chrome.runtime.onInstalled.addListener(() => {});\n",
                                      encoding="utf-8")
    return extension


def _zoom_capture(chromium: Any, extension: Path, profile: Path, factor: int, url: str,
                  directory: Path, contract: UIContract, theme: str, confined: bool,
                  axe: str, masks: tuple[str, ...], story: dict | None = None) -> dict:
    from playwright.sync_api import Error

    name = f"zoom-{factor * 100}"
    try:
        with chromium.launch_persistent_context(
            str(profile), channel="chromium", headless=True, viewport=ZOOM_BASE,
            color_scheme=theme, service_workers="block", timeout=BROWSER_START_TIMEOUT_MS,
            args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
        ) as context:
            worker = (context.service_workers[0] if context.service_workers else
                      context.wait_for_event("serviceworker", timeout=BROWSER_START_TIMEOUT_MS))
            tab_id = worker.evaluate("""async () => {
              const tabs = await chrome.tabs.query({});
              if (tabs.length !== 1) throw new Error('Expected one isolated audit tab');
              return tabs[0].id;
            }""")
            page = context.pages[0]
            version = context.new_cdp_session(page).send("Browser.getVersion")["product"]
            observed_factor = None

            def zoom(current: Any) -> None:
                nonlocal observed_factor
                observed_factor = worker.evaluate("""async ({id, factor}) => {
                  await chrome.tabs.setZoomSettings(id, {mode: 'automatic', scope: 'per-tab'});
                  await chrome.tabs.setZoom(id, factor);
                  return chrome.tabs.getZoom(id);
                }""", {"id": tab_id, "factor": factor})
                if not math.isclose(observed_factor, factor):
                    raise UIContractError("Chromium did not apply the requested native zoom")
                current.wait_for_function(
                    """({width, height, factor}) =>
                      innerWidth === width && innerHeight === height &&
                      Math.abs(devicePixelRatio - factor) < 0.01""",
                    arg={"width": ZOOM_BASE["width"] // factor,
                         "height": ZOOM_BASE["height"] // factor, "factor": factor},
                    timeout=TIMEOUT_MS,
                )

            capture = _capture(
                page, name, url, directory, version, axe=axe, actions=[{"type": "press", "key": "Tab"}],
                after_navigation=zoom, inspect_page=lambda current: _measure(current, name, contract),
                expect_origin=confined, masks=masks, story=story,
            )
            measured = capture.get("inspection", {}).get("viewport", {})
            if capture["status"] == "PASS" and (
                measured.get("width") != ZOOM_BASE["width"] // factor or
                not math.isclose(measured.get("dpr", 0), factor, abs_tol=0.01)
            ):
                capture.update(status="BLOCKED", reason="native zoom changed before capture")
            return {**capture, "zoom": {"method": "chromium-tabs", "requested": factor,
                                        "observed": observed_factor, "base_viewport": ZOOM_BASE}}
    except Error as exc:
        return {"name": name, "status": "BLOCKED", "reason": str(exc).splitlines()[0][:400]}


def _journey(context: Any, url: str, directory: Path, version: str, rule: dict,
             axe: str, masks: tuple[str, ...], *, keyboard: bool = False,
             storybook: StorybookIndex | None = None, base_story: dict | None = None) -> dict:
    from playwright.sync_api import TimeoutError

    story = base_story
    if "story" in rule:
        if storybook is None:
            raise UIContractError("story journeys require --storybook")
        story = storybook.story(rule["story"])
    elif "path" in rule:
        story = None
    target = story["preview_url"] if story else urljoin(url, rule.get("path", ""))
    prefix = "keyboard" if keyboard else "journey"
    name = f"{prefix}-{rule['id']}"
    expected_origin = origin(target)
    page = context.new_page()

    def guard(route: Any) -> None:
        request = route.request
        if request.is_navigation_request() and request.frame == page.main_frame:
            try:
                same_origin = origin(request.url) == expected_origin
            except ValueError:
                same_origin = False
            if not same_origin:
                route.abort()
                return
        route.continue_()

    page.route("**/*", guard)

    def inspect(current: Any) -> dict:
        if keyboard:
            return keyboard_journey(current, rule["steps"])
        checks = rule["expect"]
        try:
            result = current.wait_for_function(
                f"(rules) => {{ const rows = ({CHECK_JOURNEY})(rules);"
                " return rows.every(row => row.passed) ? rows : false; }",
                arg=checks, timeout=2500,
            )
            return {"status": "PASS", "assertions": result.json_value()}
        except TimeoutError:
            return {"status": "FAIL", "assertions": current.evaluate(CHECK_JOURNEY, checks)}

    actions = [{**action, "timeout": 2.5} for action in rule.get("actions", [])]
    capture = _capture(page, name, target, directory, version, axe=axe, actions=actions, inspect_page=inspect,
                       expect_origin=True, masks=masks, story=story)
    if capture["status"] == "PASS":
        capture["status"] = capture["inspection"]["status"]
    if not keyboard:
        capture["state"] = rule["state"]
    return capture


def run_ui_audit(
    url: str, directory: Path, viewports: tuple[tuple[str, int, int], ...],
    contract: UIContract, *, root: Path, axe: str, theme: str,
    run_journeys: bool, confined: bool = False, masks: tuple[str, ...] = (),
    browser_temp: Path | None = None,
    storybook: StorybookIndex | None = None, story_id: str | None = None,
) -> dict:
    """Render each viewport and journey in an isolated browser context."""
    from playwright.sync_api import sync_playwright

    captures: list[dict] = []
    journeys: list[dict] = []
    keyboard: list[dict] = []
    state_plan = contract.journeys if run_journeys else ()
    keyboard_plan = contract.keyboard if run_journeys else ()
    story = storybook.story(story_id) if storybook is not None and story_id else None
    with WebDynamicProbe.browser_slot(), sync_playwright() as playwright:
        with playwright.chromium.launch(headless=True, timeout=BROWSER_START_TIMEOUT_MS) as browser:
            for name, width, height in viewports:
                with browser.new_context(
                    viewport={"width": width, "height": height}, color_scheme=theme, service_workers="block",
                ) as context:
                    captures.append(_capture(
                        context.new_page(), name, url, directory, browser.version, axe=axe,
                        actions=[{"type": "press", "key": "Tab"}], expect_origin=confined,
                        inspect_page=lambda page: _measure(page, name, contract), masks=masks, story=story,
                    ))
            for is_keyboard, plan, cases in (
                (False, state_plan, journeys), (True, keyboard_plan, keyboard),
            ):
                for rule in plan:
                    with browser.new_context(viewport=ZOOM_BASE, color_scheme=theme, service_workers="block") as context:
                        cases.append(_journey(context, url, directory, browser.version, rule,
                                              axe, masks, keyboard=is_keyboard,
                                              storybook=storybook, base_story=story))
        with tempfile.TemporaryDirectory(prefix="bugx-ui-chromium-", dir=browser_temp) as temp:
            zoom_root = Path(temp)
            extension = _zoom_extension(zoom_root)
            for factor in ZOOMS:
                captures.append(_zoom_capture(playwright.chromium, extension, zoom_root / str(factor),
                                              factor, url, directory, contract, theme, confined, axe, masks, story))
    manifest = make_manifest(
        root=root, url=url, theme=theme,
        rules={"path": contract.source, "sha256": contract.sha256,
               "contrast": bool(contract.contrast), "reflow": bool(contract.reflow),
               "token_source": contract.token_source},
        planned={
            "captures": [name for name, _, _ in viewports] + [f"zoom-{factor * 100}" for factor in ZOOMS],
            "journeys": [f"journey-{rule['id']}" for rule in state_plan],
            "keyboard_journeys": [f"keyboard-{rule['id']}" for rule in keyboard_plan],
        },
        captures=captures, journeys=journeys, keyboard=keyboard,
    )
    if story:
        manifest["storybook"] = story
    return manifest


def _recheck_worker(receipt: Path, render_args: tuple, options: dict) -> None:
    try:
        result = {"manifest": run_ui_audit(*render_args, **options)}
    except Exception as exc:
        result = {"error": (str(exc) or type(exc).__name__).splitlines()[0][:400]}
    atomic_write_json(receipt, result)


def _bounded_recheck(render_args: tuple, options: dict, *, timeout: float = 35) -> dict:
    """Keep a browser hang in the read-only worker from holding a CSS transaction."""
    with tempfile.TemporaryDirectory(prefix="bugx-ui-recheck-") as temporary:
        directory = Path(temporary).resolve()
        receipt = directory / "result.json"
        process = get_context("spawn").Process(
            target=_recheck_worker,
            args=(receipt, render_args, {**options, "browser_temp": directory}),
        )
        process.start()
        try:
            process.join(timeout)
            if process.is_alive():
                raise TimeoutError(f"browser recheck exceeded {timeout:g} seconds")
            if process.exitcode != 0 or not receipt.is_file():
                raise RuntimeError(f"browser recheck exited without a receipt ({process.exitcode})")
            result = json.loads(receipt.read_bytes())
            if "error" in result:
                raise RuntimeError(result["error"])
            return result["manifest"]
        finally:
            if process.is_alive():
                process.terminate()
                process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join(2)
                if process.is_alive():
                    raise RuntimeError("read-only browser recheck process could not be reaped")
            process.close()


def _output_directory(path: Path | None) -> Path:
    if path is None:
        return Path(tempfile.mkdtemp(prefix="bugx-visual-audit-")).resolve()
    result = path.expanduser().resolve()
    result.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(result.iterdir()):
        raise UIContractError("--out-dir must be empty to preserve earlier evidence")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--url", help="target page URL")
    target.add_argument("--storybook", help="running or published Storybook base URL")
    story_mode = parser.add_mutually_exclusive_group()
    story_mode.add_argument("--story", help="exact Storybook story ID to audit")
    story_mode.add_argument("--list-stories", action="store_true", help="list renderable Storybook story IDs")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="target project root")
    parser.add_argument("--design", type=Path, help="design specification (default: root/DESIGN.md)")
    parser.add_argument("--out-dir", type=Path, help="empty output directory (default: system temp)")
    parser.add_argument("--viewport", action="append", type=_viewport, metavar="NAME=WIDTHxHEIGHT")
    parser.add_argument("--color-scheme", choices=("light", "dark"), default="light")
    parser.add_argument("--journeys", action="store_true", help="execute declared UI actions and assertions")
    parser.add_argument("--vision-model", help="installed local Ollama vision model tag")
    parser.add_argument("--vision-port", type=int, default=11434, help="local Ollama port")
    parser.add_argument("--repair-css", type=Path, help="preview token edits to this project stylesheet")
    parser.add_argument("--apply", action="store_true", help="apply and reverify the preview on a loopback page")
    parser.add_argument("--reference-url", help="approved reference page or Storybook base URL")
    parser.add_argument("--visual-mask", action="append", default=[], metavar="CSS_SELECTOR",
                        help="ignore the visible area of a dynamic element on both pages")
    parser.add_argument("--visual-max-diff", type=float, metavar="FRACTION",
                        help="maximum fraction of changed pixels (default 0.001)")
    parser.add_argument("--strict", action="store_true", help="return 2 for incomplete or failing rules")
    args = parser.parse_args(argv)
    try:
        if bool(args.story or args.list_stories) != bool(args.storybook):
            raise UIContractError("--storybook requires --story or --list-stories")
        book = read_storybook(args.storybook) if args.storybook else None
        if args.list_stories:
            print(json.dumps({"status": "PASS", "index": book.index_url,
                              "index_version": book.version, "index_sha256": book.sha256,
                              "stories": list(book.stories.values())}, ensure_ascii=False, indent=2))
            return 0
        url = book.story(args.story)["preview_url"] if book else args.url
        _, hostname, _ = origin(url)
        if args.apply and (args.repair_css is None or hostname not in {"127.0.0.1", "::1", "localhost"}):
            raise UIContractError("--apply requires --repair-css and a loopback URL")
        if not 1 <= args.vision_port <= 65535:
            raise UIContractError("--vision-port must be within 1–65535")
        if args.vision_model is not None:
            validate_model_tag(args.vision_model)
        if args.reference_url is not None:
            origin(args.reference_url)
            if find_spec("PIL") is None:
                raise UIContractError("visual comparison requires Pillow from the bugx-cli[ui] extra")
        elif args.visual_mask or args.visual_max_diff is not None:
            raise UIContractError("visual options require --reference-url")
        masks = tuple(args.visual_mask)
        if len(set(masks)) != len(masks) or len(masks) > 12 or any(
            not selector.strip() or len(selector) > 256 for selector in masks
        ):
            raise UIContractError("provide at most 12 unique, nonempty visual mask selectors")
        limit = args.visual_max_diff if args.visual_max_diff is not None else 0.001
        if not 0 <= limit <= 1:
            raise UIContractError("--visual-max-diff must be within 0–1")
        root = args.root.resolve(strict=True)
        contract = load_ui_contract(root, args.design)
        story_ids = {item["story"] for item in contract.journeys + contract.keyboard if "story" in item
                     and args.journeys}
        if story_ids and book is None:
            raise UIContractError("declared story journeys require --storybook")
        for story_id in story_ids:
            book.story(story_id)
        reference_book = read_storybook(args.reference_url) if book and args.reference_url else None
        reference_url = args.reference_url
        if reference_book:
            reference_url = reference_book.story(args.story)["preview_url"]
            for story_id in story_ids:
                reference_book.story(story_id)
        axe = load_axe()
        viewports = tuple(args.viewport or DEFAULT_VIEWPORTS)
        names = {name for name, _, _ in viewports}
        if len(names) != len(viewports) or len(names) > 8:
            raise UIContractError("provide at most eight uniquely named viewports")
        known = names | {f"zoom-{factor * 100}" for factor in ZOOMS}
        for item in contract.components:
            if set(item.get("profiles", [])) - known:
                raise UIContractError(f"component {item['id']} references an unknown viewport")
        directory = _output_directory(args.out_dir)

        render_options = dict(root=root, axe=axe, theme=args.color_scheme, run_journeys=args.journeys,
                              confined=args.apply or reference_url is not None, masks=masks,
                              storybook=book, story_id=args.story)

        reference = None
        reference_dir = directory / "reference"
        if reference_url is not None:
            reference_dir.mkdir(mode=0o700)
            reference = run_ui_audit(reference_url, reference_dir, viewports, contract,
                                     **{**render_options, "storybook": reference_book})
            write_ui_evidence(reference_dir, reference)

        def scan(output: Path, *, recheck: bool = False) -> dict:
            render_args = (url, output, viewports, contract)
            result = (_bounded_recheck(render_args, render_options) if recheck
                      else run_ui_audit(*render_args, **render_options))
            if reference is not None:
                visual = compare_visual(reference, result, reference_dir, output, limit)
                result.update(visual_regression=visual,
                              status=combine_status([result["status"], visual["status"]]))
            return result

        manifest = scan(directory)
        if args.repair_css is not None:
            atomic_write_json(directory / "before.json", manifest)
            repair, verified = repair_tokens(root, args.repair_css, directory, contract, manifest,
                                              apply=args.apply, verify=lambda output: scan(output, recheck=True))
            manifest = {**(verified if verified is not None else manifest), "repair": repair}
        manifest["visual_ai_review"] = review_ui(manifest["captures"], contract.markdown,
                                                args.vision_model, args.vision_port)
        write_ui_evidence(directory, manifest)
        print(json.dumps({
            "status": manifest["status"], "manifest": str(directory / "manifest.json"),
            "dynamic_evidence": str(directory / "dynamic-evidence.json"),
            "checks": manifest["dynamic_evidence"]["checks"], "design": manifest["design_system"],
            "visual_review": manifest["visual_ai_review"]["status"], "repair": manifest.get("repair"),
            "visual_regression": manifest.get("visual_regression", {}).get("status"),
            "storybook": manifest.get("storybook"),
        }, ensure_ascii=False, indent=2))
        if manifest.get("repair", {}).get("status") in {"BLOCKED", "ROLLED_BACK", "RECOVERY_REQUIRED"}:
            return 2
        return 2 if args.strict and manifest["status"] != "PASS" else 0
    except Exception as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc).splitlines()[0][:500]},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
