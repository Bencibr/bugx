"""Discover rendered Storybook stories through the deployed index and canvas."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

STORY_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,191}")
STORY_ROOT = "#storybook-root"


class StorybookError(ValueError):
    """The requested Storybook index or story could not be verified."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class StorybookIndex:
    base_url: str
    index_url: str
    version: int
    sha256: str
    stories: dict[str, dict[str, str]]

    def story(self, story_id: str) -> dict:
        if story_id not in self.stories:
            raise StorybookError(f"Storybook story ID not found: {story_id}")
        preview = urljoin(self.base_url, "iframe.html") + "?" + urlencode({
            "id": story_id, "viewMode": "story",
        })
        return {**self.stories[story_id], "preview_url": preview, "index_url": self.index_url,
                "index_version": self.version, "index_sha256": self.sha256}


def read_storybook(url: str) -> StorybookIndex:
    """Read a deployed v4/v5 index and keep only renderable entries."""
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username is not None or parsed.query or parsed.fragment):
        raise StorybookError("--storybook requires an HTTP(S) base URL without credentials or query")
    base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/", "", ""))
    index_url = urljoin(base, "index.json")
    request = Request(index_url, headers={"Accept": "application/json"})
    with build_opener(_NoRedirect()).open(request, timeout=6) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise StorybookError("Storybook index exceeds 2 MiB")
    try:
        index = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StorybookError("Storybook index is not JSON") from exc
    if (not isinstance(index, dict) or type(index.get("v")) is not int or
            index["v"] not in (4, 5) or not isinstance(index.get("entries"), dict)):
        raise StorybookError("expected a Storybook v4 or v5 index.json with entries")
    stories = {}
    for key, row in index["entries"].items():
        if not isinstance(row, dict) or row.get("type") != "story":
            continue
        if not STORY_ID.fullmatch(key) or row.get("id") != key:
            raise StorybookError(f"invalid Storybook story ID: {key!r}")
        stories[key] = {"id": key, "title": str(row.get("title", ""))[:240],
                        "name": str(row.get("name", ""))[:160]}
    return StorybookIndex(base, index_url, index["v"], hashlib.sha256(raw).hexdigest(), stories)


def verify_story_url(url: str, story: dict) -> None:
    actual, expected = urlsplit(url), urlsplit(story["preview_url"])
    query = parse_qs(actual.query)
    def port(parsed):
        return parsed.port or (443 if parsed.scheme == "https" else 80)

    if ((actual.scheme, actual.hostname, port(actual)) !=
            (expected.scheme, expected.hostname, port(expected)) or actual.username is not None or
            actual.path != expected.path or query.get("id") != [story["id"]] or
            query.get("viewMode") != ["story"]):
        raise StorybookError(f"browser did not stay on the selected Storybook story: {story['id']}")


def wait_for_story(page: Any, story: dict, timeout: int) -> None:
    verify_story_url(page.url, story)
    ready = page.wait_for_function(
        """({id, selector}) => {
          const body = document.body;
          if (!body) return false;
          if (body.matches('.sb-show-errordisplay,.sb-show-nopreview')) return 'error';
          const render = window.__STORYBOOK_PREVIEW__?.currentRender;
          if (render?.phase === 'errored') return 'error';
          if (typeof render?.phase === 'string' && (render.id !== id ||
              !['completed', 'finished'].includes(render.phase))) return false;
          return body.matches('.sb-show-main') && document.querySelector(selector) ? 'ready' : false;
        }""",
        arg={"id": story["id"], "selector": STORY_ROOT}, timeout=timeout,
    )
    try:
        if ready.json_value() != "ready":
            raise StorybookError(f"Storybook reported a render error for {story['id']}")
    finally:
        ready.dispose()
