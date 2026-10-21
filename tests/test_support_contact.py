"""Support contact: support@sponsorjobs.ai is the one public contact address.

The website links only the support inbox (never a personal or company mailbox), the app's
account menu has a "Help and support" item that drafts an email to it, and the desktop shell
hands exactly that mailto link to the operating system and nothing else (shell-electron/links.js).
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
    assert f"mailto:{SUPPORT}?subject=" in js and "openSupportMail" in js
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
    assert '"links.js"' in (ROOT / "shell-electron" / "package.json").read_text("utf-8")
