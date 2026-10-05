"""The extension must actually find the employer on the page (2026-07-16).

The user, with the extension installed and LinkedIn open: "i don't see what you are
referring to, just frog.ai still showing".

It was reading LinkedIn's internal CSS class names
(".job-details-jobs-unified-top-card__company-name"), which change whenever LinkedIn
redeploys. When none matched, pickTarget() returned {} and the extension did NOTHING: no
badge, no error, no log. Silence, on the single most important site we support, written
once and quietly dead ever since. Zero calls ever reached the app.

The fix reads schema.org JobPosting instead: the same structured data the page gives a
search engine. It survives a redesign, because a redesign is the CSS.

These tests run content.js in a fake DOM, which is the thing nobody had ever done: the file
had no test of any kind, so "it works" was only ever an assumption.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

CONTENT = Path("extension/content.js")

# The harness: run content.js the way Chrome does, with just enough DOM for pickTarget().
HARNESS = r"""
// argv[0] is node, argv[1] is this harness; our arguments start at 2.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const jsonld = process.argv[3] === 'null' ? null : JSON.parse(process.argv[3]);
const scripts = jsonld ? [{ textContent: JSON.stringify(jsonld) }] : [];
const h1 = { tagName: 'H1', textContent: 'A role', insertAdjacentElement() {} };
let sent = null;
const document = {
  querySelectorAll: (s) => s.includes('ld+json') ? scripts : [],
  querySelector: (s) => (s === 'h1' || s === 'h1, h2') ? h1 : null,
  body: { firstElementChild: h1 },
  createElement: () => ({ style: {}, setAttribute() {} }),
};
const chrome = { runtime: { sendMessage: (m) => { sent = m; }, lastError: null } };
const location = { hostname: 'www.linkedin.com', pathname: '/jobs/view/123' };
const MutationObserver = function () { this.observe = () => {}; };
new Function('document','chrome','location','MutationObserver','setTimeout','clearTimeout',
  src)(document, chrome, location, MutationObserver, (f) => f(), () => {});
console.log(JSON.stringify(sent));
"""


def _lookup(jsonld) -> dict | None:
    """What the content script would ask the app for, given this page's structured data."""
    harness = Path("build/_ext_harness.js")
    harness.parent.mkdir(parents=True, exist_ok=True)
    harness.write_text(HARNESS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(harness), str(CONTENT),
         "null" if jsonld is None else json.dumps(jsonld)],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip() or "null")


def test_it_reads_the_employer_from_structured_data_not_css():
    """The exact page the user was looking at. LinkedIn's classes may change tomorrow; this
    is the same data they hand a search engine."""
    got = _lookup({
        "@type": "JobPosting",
        "hiringOrganization": {"@type": "Organization", "name": "East West Bank"},
        "jobLocation": {"address": {"addressLocality": "Santa Clara",
                                    "addressRegion": "CA", "addressCountry": "US"}},
    })
    assert got and got["company"] == "East West Bank"
    # The location gates the visa badge (a US instrument says nothing about a London job),
    # so it has to come from the same reliable source as the name.
    assert "Santa Clara" in got["location"] and "CA" in got["location"]


def test_it_handles_a_bare_organisation_string():
    """schema.org allows hiringOrganization to be a plain string, not only an object."""
    got = _lookup({"@type": "JobPosting", "hiringOrganization": "Spotify",
                   "jobLocation": {"address": {"addressLocality": "London",
                                               "addressCountry": "GB"}}})
    assert got and got["company"] == "Spotify"
    assert "London" in got["location"]


def test_it_finds_the_posting_inside_an_at_graph():
    """Many pages wrap several objects in @graph; the JobPosting is one of them."""
    got = _lookup({"@graph": [
        {"@type": "WebPage"},
        {"@type": ["JobPosting"], "hiringOrganization": {"name": "Ramp"},
         "jobLocation": {"address": {"addressLocality": "New York", "addressRegion": "NY"}}},
    ]})
    assert got and got["company"] == "Ramp"


def test_a_page_with_no_job_posting_asks_for_nothing():
    """No guessing: a page without a posting must not send a company to look up."""
    assert _lookup({"@type": "WebPage"}) is None
    assert _lookup(None) is None


def test_the_css_selectors_are_a_fallback_not_the_only_path():
    """The bug was that CSS classes were the ONLY way in on LinkedIn. They can stay (they
    place the badge well when they match) but must never again be the single point of
    failure."""
    src = CONTENT.read_text(encoding="utf-8")
    ld = src.index("companyFromJsonLd()")
    css = src.index("for (const s of sels)")
    assert ld < css, "structured data must be tried BEFORE the site's CSS classes"


# The LinkedIn search page: no structured data, and the class names had moved ------------

LINKEDIN_SEARCH = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function el(tag, attrs={}, kids=[]) {
  const n = { tagName: tag.toUpperCase(), children: kids, attrs, parentElement: null,
              textContent: attrs.text || kids.map(k => k.textContent || '').join(''),
              getAttribute: (k) => attrs[k], insertAdjacentElement() {} };
  kids.forEach(k => k.parentElement = n); return n;
}
const companyLink = el('a', { href: '/company/first-united-bank/', text: 'First United Bank' });
const h1 = el('h1', { text: 'Portfolio Manager' });
const card = el('div', {}, [companyLink, h1]);
// Decoys, FIRST in the DOM, so picking "the first /company/ link" would get this wrong.
const aside = el('aside', {}, [
  el('a', { href: '/company/linkedin/', text: 'LinkedIn Premium' }),
  el('a', { href: '/company/commerce-bank/', text: 'Commerce Bank' }),
]);
const body = el('body', {}, [aside, card]);
const all = []; (function walk(n) { all.push(n); n.children.forEach(walk); })(body);
const document = {
  querySelectorAll: (s) => s.includes('ld+json') ? []
    : s.includes('/company/') ? all.filter(n => n.tagName === 'A' && (n.attrs.href || '').includes('/company/')) : [],
  querySelector: (s) => (s === 'h1' || s === 'h1, h2') ? h1 : null,
  body, createElement: () => ({ style: {}, setAttribute() {} }),
};
let sent = null;
const chrome = { runtime: { sendMessage: (m) => { sent = m; }, lastError: null } };
const location = { hostname: 'www.linkedin.com', pathname: '/jobs/search-results/' };
const MutationObserver = function () { this.observe = () => {}; };
new Function('document','chrome','location','MutationObserver','setTimeout','clearTimeout',
  src)(document, chrome, location, MutationObserver, (f) => f(), () => {});
console.log(JSON.stringify(sent));
"""


def test_it_finds_the_employer_on_the_linkedin_search_page():
    """Where the user actually was: /jobs/search-results/. There is NO structured data on
    that page, and the class names we relied on had changed, so the extension found nothing
    and silently did nothing.

    A URL is a stabler contract than a class name: LinkedIn cannot stop linking a company
    to /company/<slug> without breaking their own product."""
    harness = Path("build/_ext_li.js")
    harness.parent.mkdir(parents=True, exist_ok=True)
    harness.write_text(LINKEDIN_SEARCH, encoding="utf-8")
    out = subprocess.run(["node", str(harness), str(CONTENT)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip() or "null")
    # The page holds DECOY /company/ links (an ad, the hiring team) BEFORE the real one, so
    # "take the first company link" would pick the wrong employer and badge the wrong job.
    assert got and got["company"] == "First United Bank", got


LINKEDIN_DALLAS = LINKEDIN_SEARCH.replace(
    "const companyLink = el('a', { href: '/company/first-united-bank/', text: 'First United Bank' });",
    "const companyLink = el('a', { href: '/company/first-united-bank/', text: 'First United Bank' });\n"
    "const meta = el('div', { text: 'Dallas, TX \u00b7 Reposted 2 weeks ago \u00b7 60 applicants' });"
).replace("const card = el('div', {}, [companyLink, h1]);",
          "const card = el('div', {}, [el('div', {}, [companyLink, h1, meta])]);")


def test_it_reads_the_location_so_it_cannot_lie_about_a_us_job():
    """The badge said "Sponsors in the US, not this role" about a job in DALLAS, TEXAS.

    The LinkedIn path returned the company and dropped the location. Empty then read as
    "not in the US", so the extension told the user a Texas job was out of reach. Worse than
    silence: it talks someone out of a job they could actually be sponsored for.
    """
    harness = Path("build/_ext_dallas.js")
    harness.parent.mkdir(parents=True, exist_ok=True)
    harness.write_text(LINKEDIN_DALLAS, encoding="utf-8")
    out = subprocess.run(["node", str(harness), str(CONTENT)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip() or "null")
    assert got and got["company"] == "First United Bank"
    assert got["location"], "no location sent: this is exactly what produced the lie"
    # It must read as US, or the badge claims a Texas job is abroad.
    from sourcing.sponsors import looks_us
    assert looks_us(got["location"]), f"{got['location']!r} did not read as US"


# Reading the ROLE's location off a real LinkedIn card ------------------------------------

LINKEDIN_CARD = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const META = process.argv[3];
function el(t, a = {}, k = []) {
  const n = { tagName: t.toUpperCase(), children: k, attrs: a, parentElement: null,
    get textContent() { return a.text || k.map(x => x.textContent || '').join(' '); },
    getAttribute: (x) => a[x], insertAdjacentElement() {} };
  k.forEach(x => x.parentElement = n); return n;
}
const link = el('a', { href: '/company/x/', text: 'First United Bank' });
const h1 = el('h1', { text: 'Portfolio Manager' });
// The real shape: the company is in a header row, the location is a SIBLING OF THE TITLE.
const pane = el('section', {}, [el('div', {}, [link]), el('div', {}, [h1, el('div', { text: META })])]);
// A long results list of OTHER jobs in OTHER cities, exactly like the real page.
const list = el('ul', {}, Array.from({ length: 20 }, (_, i) =>
  el('li', { text: 'Other Portfolio Manager at A Bank Fort Worth, TX early applicant 2 weeks ago ' + i })));
const body = el('body', {}, [list, pane]);
const all = []; (function w(n) { all.push(n); n.children.forEach(w); })(body);
const document = {
  querySelectorAll: (s) => s.includes('ld+json') ? []
    : s.includes('/company/') ? all.filter(n => n.tagName === 'A' && (n.attrs.href || '').includes('/company/')) : [],
  querySelector: (s) => (s === 'h1' || s === 'h1, h2') ? h1 : null,
  body, createElement: () => ({ style: {}, setAttribute() {} }),
};
let sent = null;
const chrome = { runtime: { sendMessage: (m) => { sent = m; }, lastError: null } };
const location = { hostname: 'www.linkedin.com', pathname: '/jobs/search-results/' };
const MutationObserver = function () { this.observe = () => {}; };
new Function('document','chrome','location','MutationObserver','setTimeout','clearTimeout',
  src)(document, chrome, location, MutationObserver, (f) => f(), () => {});
console.log(JSON.stringify(sent));
"""


def _card(meta: str) -> dict | None:
    harness = Path("build/_ext_card.js")
    harness.parent.mkdir(parents=True, exist_ok=True)
    harness.write_text(LINKEDIN_CARD, encoding="utf-8")
    out = subprocess.run(["node", str(harness), str(CONTENT), meta],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip() or "null")


def test_it_reads_the_location_beside_the_title():
    """The location is a sibling of the <h1>, not of the company link. Searching only from
    the company link found nothing, so every badge said "location unclear" about jobs whose
    location was in plain sight."""
    from sourcing.sponsors import looks_us
    for meta, expect_us in [
        ("Dallas, TX · Reposted 2 weeks ago · 60 applicants", True),
        ("New York, NY · $130K/yr · 1 week ago", True),
        ("San Francisco, CA · 2 days ago", True),
        ("Remote - US · 3 days ago", True),
        ("London, England · 2 weeks ago", False),
    ]:
        got = _card(meta)
        assert got and got["location"], f"read nothing from {meta!r}"
        assert looks_us(got["location"]) is expect_us, f"{meta!r} -> {got['location']!r}"


def test_it_never_borrows_another_jobs_location():
    """Caught in testing: a role with NO location was handed "Fort Worth, TX" from a
    different listing further up the page, because the search climbed out of the job card
    and into the results list. That is the Dallas lie again, better disguised: it would
    stamp one job's city onto another job's badge."""
    got = _card("Reposted 5 days ago")
    assert got and got["location"] == "",         f"borrowed another job's location: {got['location']!r}"
