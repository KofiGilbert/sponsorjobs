"""The LinkedIn profile READER (extension/profile_core.js + profile.js) reads the person's OWN
profile page, read-only, and does nothing on anyone else's (CLAUDE.md §7).

profile_core.js is pure over a tiny DOM surface (tagName, getAttribute, childNodes, children,
parentElement), so these tests parse fixture HTML (modelled on LinkedIn's documented profile
markup: section anchor ids, aria-hidden / visually-hidden text pairs, data-field skill links,
the owner-only "Add profile section" and "Edit intro" controls) into a plain tree with
Python's html.parser and run the real file on it under node. A real https://www.linkedin.com/in/
fetch (2026-10-12) answers 404 without a session, so there is no live page to learn from; the
selectors are tolerant and layered for that reason.

The source guards hold the hard rule: the content script never clicks, scrolls, focuses,
dispatches events, or touches the page beyond appending its own card.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

CORE = Path("extension/profile_core.js").resolve()
CARRIER = Path("extension/profile.js").resolve()
FIX = Path("tests/fixtures/linkedin")

VOID = {"img", "br", "hr", "input", "meta", "link", "source"}


class _Tree(HTMLParser):
    """HTML -> {t, a, c} nodes (strings are text nodes)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"t": "html", "a": {}, "c": []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"t": tag, "a": {k: (v if v is not None else "") for k, v in attrs}, "c": []}
        self.stack[-1]["c"].append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["t"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if data:
            self.stack[-1]["c"].append(data)


def _tree(path: Path) -> dict:
    p = _Tree()
    p.feed(path.read_text(encoding="utf-8"))
    return p.root


HARNESS = r"""
const fs = require('fs'), path = require('path');
const Core = require(path.resolve(process.argv[2]));
const tree = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const pathname = process.argv[4];
function build(n, parent) {
  if (typeof n === 'string') return { nodeType: 3, nodeValue: n, parentElement: parent };
  const el = { nodeType: 1, tagName: n.t.toUpperCase(), _a: n.a, parentElement: parent,
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._a, k) ? this._a[k] : null; } };
  el.childNodes = n.c.map((c) => build(c, el));
  el.children = el.childNodes.filter((c) => c.nodeType === 1);
  return el;
}
const doc = build(tree, null);
const loc = { pathname, hostname: 'www.linkedin.com' };
const out = { own: Core.isOwnProfile(doc, loc), extracted: Core.extract(doc, loc) };
process.stdout.write(JSON.stringify(out));
"""


def _run(tmp_path: Path, fixture: str, pathname: str) -> dict:
    tree_file = tmp_path / "tree.json"
    tree_file.write_text(json.dumps(_tree(FIX / fixture)), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    r = subprocess.run(["node", str(harness), str(CORE), str(tree_file), pathname],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_own_profile_is_detected_and_extracted(tmp_path):
    out = _run(tmp_path, "own_profile.html", "/in/ada-lovelace/")
    assert out["own"] is True
    x = out["extracted"]
    assert x["own"] is True
    assert x["name"] == "Ada Lovelace"
    assert x["headline"] == "Data Engineer at Acme Analytics"
    assert x["about"].startswith("I build data pipelines in Python and SQL on AWS")
    assert "I build data pipelines" in x["about"] and x["about"].count("I build data pipelines") == 1
    assert x["location"] == "Chicago, Illinois, United States"
    assert x["slug"] == "ada-lovelace" and x["custom_url"] is True
    assert x["url"] == "https://www.linkedin.com/in/ada-lovelace/"
    assert x["photo"] is True and x["banner"] is True and x["open_to_work"] is True


@needs_node
def test_experience_entries_including_grouped_roles(tmp_path):
    x = _run(tmp_path, "own_profile.html", "/in/ada-lovelace/")["extracted"]
    exp = x["experience"]
    assert [e["title"] for e in exp] == ["Data Engineer", "Analytics Engineer", "Data Analyst"]
    assert exp[0]["company"] == "Acme Analytics"          # " · Full-time" stripped
    assert exp[0]["dates"].startswith("Mar 2022 - Present")
    assert exp[0]["has_description"] is True and "40%" in exp[0]["description"]
    assert exp[1]["company"] == "Globex" and exp[2]["company"] == "Globex"
    assert exp[1]["dates"].startswith("Jan 2020") and exp[1]["has_description"] is False
    assert x["current_title"] == "Data Engineer" and x["current_company"] == "Acme Analytics"


@needs_node
def test_education_skills_licenses_and_the_rest(tmp_path):
    x = _run(tmp_path, "own_profile.html", "/in/ada-lovelace/")["extracted"]
    assert x["education"] == [{"school": "University of Illinois Urbana-Champaign",
                               "degree": "Master of Science, Computer Science", "dates": "2017 - 2019"}]
    assert x["skills"] == ["Python", "SQL", "Apache Airflow", "Amazon Web Services (AWS)", "ETL"]
    assert x["licenses"] == [{"name": "AWS Certified Data Engineer - Associate",
                              "issuer": "Amazon Web Services (AWS)"}]
    assert x["recommendations_count"] == 1 and x["recommendations"] is True
    assert x["activity"] is True and x["activity_recent"] is True and x["followers"] == 1204
    assert x["featured_count"] == 0 and x["projects_count"] == 0


@needs_node
def test_someone_elses_profile_is_not_ours(tmp_path):
    """No owner-only controls on the page: not my profile. The carrier then does nothing."""
    out = _run(tmp_path, "other_profile.html", "/in/someone-else-1a2b3c4d5/")
    assert out["own"] is False
    assert out["extracted"]["own"] is False


@needs_node
def test_a_jobs_page_is_never_a_profile(tmp_path):
    out = _run(tmp_path, "own_profile.html", "/jobs/view/123/")
    assert out["own"] is False


@needs_node
def test_bare_profile_defaults_are_read_as_missing(tmp_path):
    """Default banner, ghost photo, LinkedIn's random URL suffix, no sections: all read as absent."""
    x = _run(tmp_path, "bare_own_profile.html", "/in/sam-student-1a2b3c4d5/")["extracted"]
    assert x["own"] is True
    assert x["photo"] is False and x["banner"] is False
    assert x["custom_url"] is False and x["slug"] == "sam-student-1a2b3c4d5"
    assert x["experience"] == [] and x["skills"] == [] and x["about"] == ""
    assert x["open_to_work"] is False and x["location"] == ""


# ---------------------------------------------------------------- source guards (§7)

def _code(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))


FORBIDDEN = [
    r"\.click\(", r"scroll", r"dispatchEvent", r"\.focus\(", r"\.submit\(", r"MouseEvent",
    r"KeyboardEvent", r"\.value\s*=", r"\.innerHTML", r"querySelector", r"MutationObserver",
    r"\.remove\(", r"XMLHttpRequest", r"\bfetch\(",
]


@pytest.mark.parametrize("path", [CARRIER, CORE])
def test_reader_never_clicks_scrolls_or_dispatches(path: Path):
    code = _code(path)
    for pat in FORBIDDEN:
        assert not re.search(pat, code), f"{path.name} contains {pat!r}; the reader is read-only"


def test_carrier_only_appends_its_own_card_to_the_page():
    """Every use of `document.` in the carrier is reading the tree or building our card."""
    code = _code(CARRIER)
    uses = set(re.findall(r"\bdocument\.(\w+)", code))
    assert uses <= {"documentElement", "createElement", "body"}, uses
    assert "document.body.appendChild(card)" in code
    # It talks ONLY to the extension's background worker (which reaches the local app).
    assert "chrome.runtime.sendMessage" in code
    assert "linkedin_review" in code
    assert "http://127.0.0.1:57000/" in code
    assert not re.search(r"https?://(?!127\.0\.0\.1)", code), "no other destination"


def test_carrier_does_nothing_off_the_own_profile_page():
    code = _code(CARRIER)
    assert "isOwnProfile" in code
    assert re.search(r"if \(!Core\.isOwnProfile\(root, location\)\)", code)
    assert "if (!profile.own) return;" in code


def test_manifest_loads_the_reader_only_on_profile_pages():
    m = json.loads(Path("extension/manifest.json").read_text(encoding="utf-8"))
    entries = [cs for cs in m["content_scripts"] if "https://www.linkedin.com/in/*" in cs.get("matches", [])]
    assert len(entries) == 1
    cs = entries[0]
    assert cs["matches"] == ["https://www.linkedin.com/in/*"]
    assert cs["js"] == ["profile_core.js", "profile.js"]
    assert m["permissions"] == ["storage"] and "web_accessible_resources" not in m
    for f in cs["js"]:
        assert (Path("extension") / f).exists()
