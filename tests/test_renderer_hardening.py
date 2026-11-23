"""Renderer hardening (2026-11-23): links from outside data are http(s) only, esc() escapes the
single quote, no inline event handlers, and the app page carries a script policy."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")


def _helpers():
    start = APP.index("  const esc = (s) =>")
    end = APP.index("\n", APP.index("  const safeHref = (u) =>"))
    return APP[start:end]


def test_safe_href_and_esc():
    js = _helpers() + """
    console.log(JSON.stringify([safeHref("https://a.example/x?y=1&z=2"), safeHref("javascript:alert(1)"), safeHref(" JaVaScRiPt:x"),
      safeHref("data:text/html,x"), safeHref("//evil.example"), safeHref(null), esc("a'b\\"c<d>&")]));"""
    out = json.loads(subprocess.check_output(["node", "-e", js], text=True))
    assert out[0] == "https://a.example/x?y=1&amp;z=2"
    assert out[1:6] == ["#"] * 5
    assert out[6] == "a&#39;b&quot;c&lt;d&gt;&amp;"


def test_feed_and_profile_links_go_through_safe_href():
    for frag in ('href="${safeHref(j.url)}"', 'href="${safeHref(c.linkedin)}"', 'href="${safeHref(e.url)}"',
                 'href="${safeHref(p.repo_url)}"', 'href="${safeHref(j.url || a.url)}"'):
        assert frag in APP, frag


def test_no_inline_event_handlers_in_the_renderer():
    import re
    assert not re.search(r'\son(error|load|click|mouseover)="', APP.replace("<img onerror=…>", ""))


def test_company_name_is_escaped_in_the_sponsor_strip():
    assert "Public U.S. filings show ${esc(name)} with" in APP
    assert "public sponsorship record for ${esc(name)} yet" in APP


def test_the_app_page_has_a_script_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path))
    from ui import app as A
    c = A.app.test_client()
    r = c.get("/", headers={"Host": "127.0.0.1:57000"})
    if r.status_code == 200 and r.mimetype == "text/html":
        csp = r.headers.get("Content-Security-Policy", "")
        assert "script-src 'self'" in csp and "object-src 'none'" in csp and "unsafe-inline" not in csp
    assert "unsafe-inline" not in A.APP_CSP and "script-src 'self'" in A.APP_CSP
