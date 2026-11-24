"""The Inbox screen (ui/templates/index.html, ui/static/app.js, ui/static/inbox_view.js,
ui/static/styles.css): the sidebar entry and the view are on the page, every string it shows is
translated in all five languages, a mail body is only ever shown in a sandboxed frame that runs
nothing and loads nothing, and the layout follows the page's own width. The screen's pure parts
are driven under node (tests/js/test_inbox_view.js)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
VIEW_JS = (ROOT / "ui" / "static" / "inbox_view.js").read_text(encoding="utf-8")
CSS = (ROOT / "ui" / "static" / "styles.css").read_text(encoding="utf-8")
LOCALES = ROOT / "ui" / "static" / "locales"
LANGS = ("en", "de", "es", "fr", "pt")

# The Inbox's part of app.js: from its heading to the sidebar wiring that follows it.
INBOX_JS = APP_JS[APP_JS.index("Inbox (docs/mail-alias.md)"):APP_JS.index("  /* nav */")]
VIEW = HTML[HTML.index('id="view-inbox"'):HTML.index('id="view-autoapply"')]


def _locale(code):
    return json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_pure_parts_pass_under_node():
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_inbox_view.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr or out.stdout
    assert "all checks passed" in out.stdout


def test_the_served_page_has_the_inbox_view_and_its_sidebar_item():
    import ui.app as app
    page = app.app.test_client().get("/").get_data(as_text=True)
    assert 'id="view-inbox"' in page and 'data-nav="inbox"' in page and 'id="navInboxBadge"' in page
    assert "inbox_view.js" in page and page.index("inbox_view.js") < page.index("static/app.js")


def test_the_sidebar_item_sits_between_tracker_and_projects_with_a_count_badge():
    order = re.findall(r'class="nav-item[^"]*" data-nav="([a-z]+)"', HTML)
    assert order[order.index("tracker") + 1] == "inbox" and order[order.index("inbox") + 1] == "projects"
    item = HTML[HTML.index('data-nav="inbox"'):HTML.index('data-nav="projects"')]
    assert 'class="nav-ic"' in item and 'data-i18n="nav.inbox"' in item
    assert '<span class="nav-badge" id="navInboxBadge" hidden>' in item          # hidden until there is unread mail
    assert 'n.dataset.nav === "inbox"' in APP_JS and 'showView("inbox")' in APP_JS
    # The badge's ink is the one that belongs on the accent in both themes.
    badge = CSS[CSS.index(".nav-badge {"):CSS.index(".nav-badge[hidden]")]
    assert "color: var(--accent-ink" in badge


def test_the_view_has_its_controls_and_nothing_that_composes():
    for el in ('id="ibAddr"', 'id="ibCopy"', 'id="ibSearch"', 'id="ibUnread"', 'id="ibReadAll"', 'id="ibRefresh"',
               'id="ibTabs"', 'id="ibNote"', 'id="ibSplit"', 'id="ibList"', 'id="ibMoreBtn"', 'id="ibReader"', 'id="ibEmpty"'):
        assert el in VIEW, el
    # every control is a button; the sixth is the back link to Settings, shown with the agent off
    assert VIEW.count("<button") == 6 and '<a ' not in VIEW and 'id="ibPageBack"' in VIEW
    for word in ("reply", "compose", "forward"):                                 # read only in this version
        assert word not in VIEW.lower()
        assert not re.search(r'ibT\("[^"]*",\s*"[^"]*\b%s' % word, INBOX_JS, re.I), word
    for action in ("ibViewApp", "ibArchive", "ibDelete", "ibDelYes", "ibDelNo", "ibLabelSel", "ibBack"):
        assert f'id="{action}"' in INBOX_JS and f'$("#{action}")' in INBOX_JS, action   # each one is wired
    assert "This cannot be undone." in INBOX_JS


def test_a_mail_body_is_shown_only_in_a_frame_that_runs_and_loads_nothing():
    assert 'f.setAttribute("sandbox", "allow-same-origin")' in INBOX_JS
    assert INBOX_JS.count('"sandbox"') == 1
    for grant in ("allow-scripts", "allow-forms", "allow-popups", "allow-top-navigation", "allow-modals", "allow-downloads"):
        assert f'"{grant}' not in INBOX_JS and f" {grant}" not in INBOX_JS.split('setAttribute("sandbox"')[1].split("\n")[0], grant
    assert "f.srcdoc = IBV.frameDoc(html)" in INBOX_JS
    # The sanitised HTML goes to the frame and nowhere else; the text fallback is set as text.
    assert re.findall(r"[^\n]*html_safe[^\n]*", INBOX_JS) == ["    if (m.html_safe) body.appendChild(ibFrame(m.html_safe));"]
    assert "pre.textContent = m.text" in INBOX_JS
    assert not re.search(r"innerHTML\s*=\s*[^;]*\bm\.text\b", INBOX_JS)
    # A link never navigates the frame: the click is stopped and handed to the app's own opener.
    assert "e.preventDefault();" in INBOX_JS and "acOpen(act.url)" in INBOX_JS
    assert "default-src 'none'; style-src 'unsafe-inline'" in VIEW_JS and '<base target=\\"_blank\\">' in VIEW_JS


def test_every_string_the_screen_shows_is_in_all_five_languages():
    used = set(re.findall(r'["\'](mbox\.[A-Za-z0-9_.]+|nav\.inbox)["\']', HTML + APP_JS + VIEW_JS))
    assert len(used) > 50
    en = _locale("en")
    for code in LANGS:
        d = _locale(code)
        missing = sorted(k for k in used if not str(d.get(k) or "").strip())
        assert not missing, f"{code}.json: {missing}"
        for k in used:
            assert not re.search("[–—]", d[k]), (code, k)               # no en or em dashes
            assert set(re.findall(r"\{[a-z]+\}", d[k])) == set(re.findall(r"\{[a-z]+\}", en[k])), (code, k)
    unused = sorted(k for k in en if k.startswith("mbox.") and k not in used)
    assert not unused, unused
    assert en["mbox.label.rejected"] == "Not this time"
    assert not any(re.search(r"\bCV\b", en[k]) for k in used)
    assert not re.search("[–—]", VIEW_JS)
    # The English on the page and in the code is the English in the file.
    assert en["mbox.empty.noaddr"] in INBOX_JS and en["mbox.stale"] in INBOX_JS and en["mbox.deleteAsk"] in INBOX_JS


def test_the_privacy_text_says_how_long_mail_is_held_on_the_server():
    en = _locale("en")["set.privacyBody"]
    said = ("Mail sent to your SponsorJobs address is held on our server only until this app downloads it, "
            "30 days at most. After that it lives on this machine.")
    assert en.endswith(said) and "keeps only usage counts for billing" in en
    assert said in HTML
    for code in LANGS:
        body = _locale(code)["set.privacyBody"]
        assert "30" in body and "SponsorJobs" in body, code


def test_settings_offers_the_three_choices_for_copies_to_the_persons_own_email():
    row = HTML[HTML.index('id="mailNotifyRow"'):HTML.index("<!-- GitHub:")]
    assert re.findall(r'<option value="([a-z]+)"', row) == ["forward", "notice", "off"]
    assert 'for="mailNotify"' in row and 'data-i18n="mbox.notify.help"' in row and " hidden" in row.split("\n")[0]
    assert "data-agent-only>" in row.split("\n")[0]     # the SponsorJobs address is the agent's (six tabs)
    en = _locale("en")
    assert [en[f"mbox.notify.{k}"] for k in ("title", "forward", "notice", "off")] == \
        ["Copies to my own e-mail", "Forward every message", "Send a short notice", "Do not send"]
    assert 'ibSend("/api/inbox/notify", "POST", { mode: sel.value })' in INBOX_JS and "mailNotifyLoad(alias);" in APP_JS


def test_the_tracker_panel_links_to_that_applications_mail():
    assert 'id="trkPanelMsgs" hidden' in APP_JS and "trkPanelMessages(sid, j);" in APP_JS
    assert "IBV.listQuery({ job: sid, per: 1 })" in INBOX_JS and 'navTo("inbox")' in INBOX_JS


def test_the_layout_follows_the_pages_own_width_and_never_the_windows():
    blocks = lambda at: " ".join(m.group(0) for m in re.finditer(at + r"[^{]*\{(?:[^{}]*\{[^{}]*\})+\s*\}", CSS))
    assert ".view-inbox" not in blocks(r"@media") and ".ib-" not in blocks(r"@media")
    container = blocks(r"@container")
    assert ".view-inbox .ib-split { grid-template-columns: minmax(0, 1fr); }" in container
    assert ".view-inbox .ib-split.is-reading .ib-list-w { display: none; }" in container
    assert ".view-inbox .ib-back { display: inline-flex; }" in container        # a back link, one pane only
    inbox_css = CSS[CSS.index("Inbox (docs/mail-alias.md)"):]
    assert "grid-template-columns: minmax(340px, 2fr) minmax(0, 3fr)" in inbox_css
    assert ".view-inbox [hidden] { display: none !important; }" in inbox_css
    # No fixed pixel widths on the panes or the rows, and no colour of its own beyond the frame's white page.
    assert not re.search(r"(?<![-\w])width:\s*\d{3,}px", inbox_css)
    assert set(re.findall(r"#[0-9a-fA-F]{3,6}\b", inbox_css)) == {"#fff"}
