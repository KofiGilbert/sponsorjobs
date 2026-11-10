"""Support contact: support@sponsorjobs.ai is the one public contact address.

The website links only the support inbox (never a personal or company mailbox), the app's
account menu has a "Help and support" item that opens a small in-app panel (copy the address,
Gmail's compose screen in the person's own browser, or their mail app), and the desktop shell
hands exactly those two links to the operating system and nothing else (shell-electron/links.js).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
SUPPORT = "support@sponsorjobs.ai"
PAGES = sorted(SITE.glob("*.html")) + [ROOT / "scripts" / "site_partials" / "footer.html"]
PERSONAL = re.compile(r"kofig@blackorigin\.org|[A-Za-z0-9._%+-]+@gmail\.com", re.I)


@pytest.mark.parametrize("page", PAGES, ids=[p.name for p in PAGES])
def test_site_links_no_personal_address(page: Path):
    html = page.read_text(encoding="utf-8")
    hit = PERSONAL.search(html)
    assert not hit, f"{page.name} shows {hit.group(0)}; use {SUPPORT}"
    for addr in re.findall(r'href="mailto:([^"?]+)', html):
        assert addr == SUPPORT, f"{page.name} links mailto:{addr}"


def test_footer_and_legal_pages_name_support():
    assert f'href="mailto:{SUPPORT}"' in (ROOT / "scripts" / "site_partials" / "footer.html").read_text("utf-8")
    for name in ("privacy.html", "terms.html"):
        html = (SITE / name).read_text("utf-8")
        assert f'href="mailto:{SUPPORT}"' in html and "BlackOrigin, Inc." in html, name


def test_app_menu_has_help_item_in_every_locale():
    html = (ROOT / "ui" / "templates" / "index.html").read_text("utf-8")
    menu = html[html.index('id="acctMenu"'):]
    assert 'id="menuHelp"' in menu[:menu.index('id="menuLanguage"')], "Help sits next to Settings"
    js = (ROOT / "ui" / "static" / "app.js").read_text("utf-8")
    assert "mailto:" in js and "openSupportMail" in js
    for loc in sorted((ROOT / "ui" / "static" / "locales").glob("*.json")):
        val = json.loads(loc.read_text("utf-8")).get("acct.help")
        assert val and not re.search(r"[—–-]", val) and not re.search(r"\bCV\b", val), loc.name


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_shell_allows_only_the_support_mailto():
    cases = {
        f"mailto:{SUPPORT}": True,
        f"mailto:{SUPPORT}?subject=SponsorJobs%20help&body=%0A%0AApp%20version%3A%200.1.0": True,
        f"mailto:{SUPPORT}?subject=hi": True,
        f"mailto:{SUPPORT}?cc=someone@example.com": False,
        f"mailto:{SUPPORT}?subject=a&subject=b": False,
        f"mailto:{SUPPORT},other@example.com": False,
        f"mailto:{SUPPORT}.evil.com": False,
        "mailto:someone@example.com": False,
        "file:///etc/passwd": False,
        "https://sponsorjobs.ai": False,
        "": False,
    }
    script = (
        "const { isSupportMailto } = require(process.argv[1]);"
        "const cases = JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify(Object.keys(cases).map(u => [u, isSupportMailto(u)])));"
    )
    out = subprocess.run(["node", "-e", script, str(ROOT / "shell-electron" / "links.js"), json.dumps(cases)],
                         capture_output=True, text=True, check=True).stdout
    for url, got in json.loads(out):
        assert got is cases[url], url
    main = (ROOT / "shell-electron" / "main.js").read_text("utf-8")
    assert 'ipcMain.on("support:mail"' in main and "isSupportMailto(url)) shell.openExternal(url)" in main
    # links.js must ship in the installer (packed by build.files; see tests/test_shell_packaging.py).
    import fnmatch
    files = json.loads((ROOT / "shell-electron" / "package.json").read_text("utf-8"))["build"]["files"]
    assert any(fnmatch.fnmatch("links.js", f) for f in files if not f.startswith("!"))


def _menu_help_handler(js: str) -> str:
    i = js.index('$("#menuHelp")')
    return js[i:js.index("\n", i)]


def test_help_opens_a_panel_not_the_mail_handler():
    html = (ROOT / "ui" / "templates" / "index.html").read_text("utf-8")
    panel = html[html.index('id="helpModal"'):]
    panel = panel[:panel.index("<!-- Settings.")]
    for el in ('id="helpClose"', 'id="helpEmail"', 'id="helpCopy"', 'id="helpGmail"', 'id="helpMailApp"',
               'role="dialog"', 'aria-modal="true"'):
        assert el in panel, el
    assert f'<span class="help-email" id="helpEmail">{SUPPORT}</span>' in panel
    assert panel.count("btn-primary") == 1, "one primary action: Open in Gmail"
    assert not re.search(r"\bCV\b", panel)
    js = (ROOT / "ui" / "static" / "app.js").read_text("utf-8")
    handler = _menu_help_handler(js)
    assert "openHelp" in handler and "openSupportMail" not in handler and "mailto" not in handler
    body = js[js.index("function openHelp()"):js.index('$("#helpMailApp")')]
    assert "openSupportMail" not in body and "location.href" not in body
    # Gmail goes through the shell's own-browser route; the mail app stays a second choice.
    assert "openSupportGmail" in js and "https://mail.google.com/mail/?view=cm&fs=1&to=" in js
    assert "navigator.clipboard.writeText(SUPPORT_EMAIL)" in js and 'execCommand("copy")' in js
    assert "closeHelp()" in js[js.index('if (e.key !== "Escape") return;'):]
    for loc in sorted((ROOT / "ui" / "static" / "locales").glob("*.json")):
        d = json.loads(loc.read_text("utf-8"))
        for k in ("help.sub", "help.copy", "help.copied", "help.gmail", "help.mailApp"):
            assert d.get(k) and not re.search(r"[\u2014\u2013]", d[k]), (loc.name, k)


GMAIL = "https://mail.google.com/mail/?"


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_shell_allows_only_the_support_gmail_compose():
    to = "to=support%40sponsorjobs.ai"
    cases = {
        f"{GMAIL}view=cm&fs=1&{to}&su=SponsorJobs%20help&body=%0A%0AApp%20version%3A%200.1.0": True,
        f"{GMAIL}view=cm&fs=1&to={SUPPORT}": True,
        f"{GMAIL}view=cm&{to}": True,
        f"{GMAIL}{to}&view=cm&su=hi": True,
        f"{GMAIL}{to}": False,                                     # not the compose view
        f"{GMAIL}view=cm&fs=2&{to}": False,
        f"{GMAIL}view=cm&to=someone%40example.com": False,         # other recipient
        f"{GMAIL}view=cm&to=support%40sponsorjobs.ai%2Cother%40example.com": False,
        f"{GMAIL}view=cm&{to}&{to}": False,                         # repeated
        f"{GMAIL}view=cm&{to}&cc=someone%40example.com": False,     # extra params
        f"{GMAIL}view=cm&{to}&bcc=someone%40example.com": False,
        f"{GMAIL}view=cm&{to}&tf=1": False,
        f"{GMAIL}view=cm&{to}#inbox": False,
        f"https://mail.google.com/mail/u/0/?view=cm&{to}": False,   # other path
        f"https://mail.google.com.evil.com/mail/?view=cm&{to}": False,
        f"https://evil.com/mail/?view=cm&{to}": False,
        f"https://mail.google.com@evil.com/mail/?view=cm&{to}": False,
        f"https://mail.google.com:8443/mail/?view=cm&{to}": False,
        f"http://mail.google.com/mail/?view=cm&{to}": False,
        f"https://accounts.google.com/mail/?view=cm&{to}": False,
        f"mailto:{SUPPORT}": False,
        "": False,
    }
    script = (
        "const { isSupportGmailCompose } = require(process.argv[1]);"
        "const cases = JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify(Object.keys(cases).map(u => [u, isSupportGmailCompose(u)])));"
    )
    out = subprocess.run(["node", "-e", script, str(ROOT / "shell-electron" / "links.js"), json.dumps(cases)],
                         capture_output=True, text=True, check=True).stdout
    for url, got in json.loads(out):
        assert got is cases[url], url
    main = (ROOT / "shell-electron" / "main.js").read_text("utf-8")
    assert 'ipcMain.on("support:gmail"' in main and "isSupportGmailCompose(url)) shell.openExternal(url)" in main
    # Window-open and navigation also send it to the own browser, ahead of the in-app pane.
    assert main.count("isSupportMailto(url) || isSupportGmailCompose(url)") == 2
    pre = (ROOT / "shell-electron" / "preload.js").read_text("utf-8")
    assert 'openSupportGmail: (url) => ipcRenderer.send("support:gmail", url)' in pre
