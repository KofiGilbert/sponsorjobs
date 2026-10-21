"""The public website (site/) keeps the house copy rules and stays internally consistent.

Copy rules (docs: the website brief, 2026-10-18): no em or en dashes, no exclamation marks,
"resume" never "CV", no marketing filler words, and every number the page states is one we
can source (checked by hand; this test only guards the words).

Structure rules: the nav and footer are shared partials stitched in by scripts/build_site.py,
so the committed pages must match the partials; every local link must resolve to a file in
site/; the images referenced must exist; sponsor_core.js is a verbatim copy of the extension's.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
PAGES = sorted(SITE.glob("*.html"))

DASH = re.compile(r"[—–]")
HTML_DASH = re.compile(r"&(mdash|ndash|#8211|#8212|#x201[34]);", re.I)
BANNED = re.compile(
    r"\b(seamless(ly)?|supercharged?|unlock(s|ed|ing)?|effortless(ly)?|10x|revolutionary|"
    r"game.changing|cutting.edge|next.gen(eration)?|AI-powered)\b", re.I)
CV = re.compile(r"\bCVs?\b")


class _Text(HTMLParser):
    """Visible text plus alt/title/meta-description text: what a person reads."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        a = dict(attrs)
        for key in ("alt", "title", "aria-label", "placeholder", "content"):
            if a.get(key):
                self.parts.append(a[key])

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _visible(path: Path) -> str:
    p = _Text()
    p.feed(path.read_text(encoding="utf-8"))
    return "\n".join(p.parts)


@pytest.mark.parametrize("page", PAGES, ids=[p.name for p in PAGES])
def test_copy_rules(page: Path):
    raw = page.read_text(encoding="utf-8")
    text = _visible(page)
    assert not DASH.search(raw) and not HTML_DASH.search(raw), f"{page.name}: em/en dash"
    assert "!" not in text, f"{page.name}: exclamation mark in copy"
    assert not CV.search(text), f"{page.name}: say resume, not CV"
    hit = BANNED.search(text)
    assert not hit, f"{page.name}: banned word {hit.group(0)!r}"


def test_script_copy_rules():
    js = (SITE / "script.js").read_text(encoding="utf-8")
    strings = re.findall(r'"([^"\n]*)"', js)
    for s in strings:
        assert not DASH.search(s), f"script.js string has a dash: {s!r}"
        assert not CV.search(s), f"script.js string says CV: {s!r}"


def test_pages_match_partials():
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts" / "build_site.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["--check"]) == 0, "run scripts/build_site.py and commit the pages"


def _links(page: Path) -> list[str]:
    html = page.read_text(encoding="utf-8")
    return re.findall(r'(?:href|src|srcset)="([^"]+)"', html)


@pytest.mark.parametrize("page", PAGES, ids=[p.name for p in PAGES])
def test_local_links_resolve(page: Path):
    for raw in _links(page):
        for href in raw.split(","):
            href = href.strip().split(" ")[0]
            if not href or href.startswith(("http://", "https://", "mailto:", "#", "data:")):
                continue
            target = href.split("#")[0].split("?")[0]
            if not target:
                continue
            assert (SITE / target).exists(), f"{page.name} links to missing {href}"


def test_shared_head_requirements():
    for page in PAGES:
        html = page.read_text(encoding="utf-8")
        assert "<title>" in html and 'name="description"' in html, page.name
        assert 'rel="stylesheet" href="styles.css"' in html, page.name
        if page.name != "404.html":
            assert 'rel="canonical"' in html, page.name
            assert 'property="og:image"' in html, page.name


def test_sponsor_core_is_verbatim_copy():
    assert (SITE / "sponsor_core.js").read_bytes() == (ROOT / "extension" / "sponsor_core.js").read_bytes(), \
        "site/sponsor_core.js must stay a verbatim copy of extension/sponsor_core.js"


def test_csp_allows_the_live_hosts():
    headers = (SITE / "_headers").read_text(encoding="utf-8")
    csp = next(l for l in headers.splitlines() if "Content-Security-Policy" in l)
    assert "https://feed.sponsorjobs.ai" in csp and "https://api.github.com" in csp
    assert "font-src 'self'" in csp
    assert "/fonts/*" in headers and "immutable" in headers


def test_prices_agree_with_the_broker():
    """The pass and pack prices on the site are the ones the broker labels."""
    metering = (ROOT / "worker" / "src" / "metering.ts").read_text(encoding="utf-8")
    pricing = _visible(SITE / "pricing.html")
    for label in re.findall(r'priceLabel: "(\$\d+)"', metering):
        if label:
            assert label in pricing, f"pass price {label} missing from pricing.html"
    for label in re.findall(r'pack_\d: "(\$\d+)"', metering):
        assert label in pricing, f"pack price {label} missing from pricing.html"


# ---- the home page stays short and plain ---------------------------------------------------
HOME_WORD_BUDGET = 180


class _HomeWords(HTMLParser):
    """Words a visitor reads inside <main>, minus the employer panel's live data."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.words: list[str] = []
        self._main = 0
        self._skip = 0
        self._stack: list[bool] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            return
        a = dict(attrs)
        skip = tag in ("script", "style") or a.get("id") == "employer" or tag == "footer"
        self._stack.append(skip)
        if skip:
            self._skip += 1
        if tag == "main":
            self._main += 1

    def handle_endtag(self, tag):
        if tag in self.VOID or not self._stack:
            return
        if self._stack.pop():
            self._skip -= 1
        if tag == "main":
            self._main -= 1

    def handle_data(self, data):
        if self._main and not self._skip:
            self.words += re.findall(r"[A-Za-z0-9$][\w$.,'-]*", data)


def _home_word_count() -> int:
    p = _HomeWords()
    p.feed((SITE / "index.html").read_text(encoding="utf-8"))
    return len(p.words)


def test_home_page_word_budget():
    n = _home_word_count()
    assert n < HOME_WORD_BUDGET, f"home page has {n} words of copy; the budget is {HOME_WORD_BUDGET}"


def test_only_inter_is_loaded():
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*{[^}]*font-family:\s*\"([^\"]+)\"", css)
    assert set(faces) == {"Inter", "Inter Fallback"}, faces
    assert not re.search(r"mono", css, re.I), "no monospace face or mono styles on the site"
    for decl in re.findall(r"font(?:-family)?:\s*([^;]+);", css):
        if "var(--sans)" in decl or "Inter" in decl:
            continue
        assert re.fullmatch(r"\s*(inherit|normal|[\d.]+\w*)\s*", decl), f"unexpected font declaration: {decl}"
    fonts = sorted(p.name for p in (SITE / "fonts").iterdir())
    assert all(n.lower().startswith(("inter", "ofl-inter")) for n in fonts), fonts
    for page in PAGES:
        html = page.read_text(encoding="utf-8")
        for href in re.findall(r'rel="preload" href="([^"]+)"', html):
            assert "inter" in href, f"{page.name} preloads {href}"
        assert "fonts.googleapis" not in html and "mono" not in html.lower(), page.name


def test_no_kicker_labels_or_display_italics():
    for page in PAGES:
        html = page.read_text(encoding="utf-8")
        assert "<i>" not in html and "<em>" not in html, f"{page.name}: italic display text"
        assert not re.search(r'class="[^"]*\b(eyebrow|kicker|mono)\b', html), page.name
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    assert "text-transform: uppercase" not in css and "background-clip: text" not in css


def test_home_tabs_work_without_script():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    panels = re.findall(r'<div class="panel" role="tabpanel" id="(panel-[a-z]+)"[^>]*?( hidden)?>', html)
    assert [p for p, _ in panels] == ["panel-find", "panel-tailor", "panel-apply", "panel-practise"]
    assert panels[0][1] == "" and all(h == " hidden" for _, h in panels[1:]), "only the first tab shows without JS"


def test_employer_lookup_is_wired():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    for el in ("employer", "emp-form", "emp-q", "emp-verdict", "emp-for", "emp-since", "emp-h1b",
               "emp-perm", "emp-last", "emp-everify", "emp-status"):
        assert f'id="{el}"' in html, f"index.html lost #{el}, which script.js fills"
    assert '<script src="sponsor_core.js" defer></script>' in html and '<script src="script.js" defer></script>' in html


def test_theme_toggle_has_no_inline_script():
    for page in PAGES:
        html = page.read_text(encoding="utf-8")
        assert '<script src="theme.js"></script>' in html, page.name
        for body in re.findall(r"<script(?![^>]*\bsrc=)(?![^>]*application/ld\+json)[^>]*>(.*?)</script>", html, re.S):
            assert not body.strip(), f"{page.name}: inline script breaks the CSP"
        assert "style=" not in html, f"{page.name}: inline style breaks the CSP"
    js = (SITE / "theme.js").read_text(encoding="utf-8")
    assert "localStorage" in js and "try" in js
