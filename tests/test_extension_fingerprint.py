"""The extension must not be fingerprintable by the sites it runs on (2026-10-11).

A page can probe chrome-extension://<id>/<file> for every file an extension lists under
web_accessible_resources, and that is one of the ways a site learns which extensions a
visitor runs. LinkedIn does this. So the manifest lists NO web-accessible resources: the only
thing the content script ever needed from the package was our logo, and that is now an inline
SVG data URI in content.js. Permissions stay minimal: "storage" and nothing else, with host
permissions only for the feed and the local app.
"""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST = Path("extension/manifest.json")
CONTENT = Path("extension/content.js")


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_no_web_accessible_resources_at_all():
    m = _manifest()
    assert "web_accessible_resources" not in m, \
        "a web-accessible resource lets any page probe for the extension by URL"


def test_permissions_are_exactly_storage():
    m = _manifest()
    assert m["permissions"] == ["storage"]
    assert "optional_permissions" not in m
    assert set(m["host_permissions"]) == {
        "http://127.0.0.1:57000/*", "http://localhost:57000/*", "https://feed.sponsorjobs.ai/*",
    }


def test_nothing_in_the_extension_loads_a_packaged_file_into_the_page():
    """chrome.runtime.getURL is how a content script points a page at a packaged file, and it
    only works for web-accessible files, so its absence is the proof the list can stay empty."""
    for f in sorted(Path("extension").glob("*.js")):
        src = f.read_text(encoding="utf-8")
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))
        assert "chrome.runtime.getURL" not in code, f"{f.name} still loads a packaged file into the page"
    src = CONTENT.read_text(encoding="utf-8")
    assert "data:image/svg+xml" in src and "<svg xmlns" in src     # the logo is inlined


def test_the_version_was_bumped_for_this_change():
    assert _manifest()["version"] == "0.4.0"
