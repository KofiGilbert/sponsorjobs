"""The in-app Apply may click a site's submit ONLY on the verified AUTO allowlist (CLAUDE.md 7).

The desktop shell (shell-electron/main.js, assist:apply) asks the engine's
/api/submit/decision before it touches a site's submit button, and that endpoint is a thin
wrapper over submit.policy.apply_decision. These tests pin the decision, the endpoint, and
the wiring in main.js, so a refactor cannot quietly go back to "click submit everywhere but
LinkedIn".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from submit.policy import apply_decision

ROOT = Path(__file__).resolve().parents[1]
MAIN_JS = (ROOT / "shell-electron" / "main.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")


# Every site we fill but must NOT submit: employer-key ATSs, company career sites, unknown hosts.
ASSISTED = [
    "https://boards.greenhouse.io/acme/jobs/123",
    "https://job-boards.greenhouse.io/acme/jobs/123",
    "https://jobs.lever.co/acme/abc",
    "https://jobs.ashbyhq.com/acme/abc/application",
    "https://apply.workable.com/acme/j/ABC/",
    "https://jobs.smartrecruiters.com/Acme/123",
    "https://www.amazon.jobs/en/jobs/123",
    "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/123",
    "https://careers.example.com/apply/123",
    # look-alikes of the one AUTO host must not ride on its name
    "https://recruitee.com.evil.example/o/eng",
    "https://evilrecruitee.com/o/eng",
    "https://acme.recruitee.co/o/eng",
]

PROHIBITED = [
    "https://www.linkedin.com/jobs/view/123",
    "https://uk.linkedin.com/jobs/view/123",
    "https://www.indeed.com/viewjob?jk=1",
    "https://www.glassdoor.com/job-listing/x",
    "https://www.ziprecruiter.com/c/x",
]


@pytest.mark.parametrize("url", ASSISTED)
def test_non_allowlisted_site_is_filled_but_never_submitted(url):
    d = apply_decision(url, url)
    assert d["click_submit"] is False
    assert d["lane"] == "assisted"
    assert d["fill"] is True


@pytest.mark.parametrize("url", PROHIBITED)
def test_prohibited_site_is_neither_filled_nor_submitted(url):
    d = apply_decision(url, url)
    assert d == {"fill": False, "click_submit": False, "lane": "assisted"}


def test_only_the_verified_allowlist_is_submitted():
    url = "https://acme.recruitee.com/o/backend-engineer"
    assert apply_decision(url, url) == {"fill": True, "click_submit": True, "lane": "auto"}
    assert apply_decision(url)["click_submit"] is True


@pytest.mark.parametrize("landed", [
    "https://boards.greenhouse.io/acme/jobs/1",      # handed off to another ATS
    "https://www.linkedin.com/jobs/view/1",          # redirected to a prohibited board
    "https://careers.example.com/apply",
])
def test_redirect_off_the_allowlist_drops_to_assisted(landed):
    d = apply_decision("https://acme.recruitee.com/o/eng", landed)
    assert d["click_submit"] is False


def test_allowlisted_page_reached_from_an_unlisted_link_is_still_assisted():
    d = apply_decision("https://careers.example.com/jobs/1", "https://acme.recruitee.com/o/eng")
    assert d["click_submit"] is False


@pytest.mark.parametrize("url", ["", "   ", "not a url", "javascript:alert(1)", "ftp://x"])
def test_empty_or_junk_url_is_never_submitted(url):
    assert apply_decision(url, url)["click_submit"] is False


def test_decision_endpoint_matches_the_policy():
    import ui.app as app
    c = app.app.test_client()
    gh = c.get("/api/submit/decision", query_string={
        "url": "https://boards.greenhouse.io/a/jobs/1",
        "page_url": "https://boards.greenhouse.io/a/jobs/1"}).get_json()
    assert gh == {"fill": True, "click_submit": False, "lane": "assisted"}
    rc = c.get("/api/submit/decision", query_string={
        "url": "https://acme.recruitee.com/o/eng",
        "page_url": "https://acme.recruitee.com/o/eng"}).get_json()
    assert rc["click_submit"] is True
    li = c.get("/api/submit/decision", query_string={"url": "https://www.linkedin.com/jobs/1"}).get_json()
    assert li["fill"] is False and li["click_submit"] is False
    assert c.get("/api/submit/decision").get_json()["click_submit"] is False


# ---------------------------------------------------------------- the shell's wiring (main.js)
# Electron can't run under pytest, so pin the shape that keeps the click behind the policy.

def _apply_handler() -> str:
    start = MAIN_JS.index('ipcMain.handle("assist:apply"')
    end = MAIN_JS.index("\n});", start)
    return MAIN_JS[start:end]


def test_main_js_clicks_submit_in_exactly_one_place():
    assert MAIN_JS.count("btn.click()") == 1
    # ...and that place is guarded by the doClick flag inside submitScript.
    script = MAIN_JS[MAIN_JS.index("function submitScript(click)"):]
    script = script[:script.index("\n}\n")]
    assert "btn.click()" in script
    assert script.index("if (!doClick)") < script.index("btn.click()")
    assert "SUBMIT_JS" not in MAIN_JS


def test_submit_script_is_only_ever_run_with_the_engine_decision():
    calls = re.findall(r"submitScript\(([^)]*)\)", MAIN_JS)
    # the definition plus one call, and that call passes the policy-derived flag
    assert calls == ["click", "mayClick"]
    h = _apply_handler()
    assert re.search(r"const mayClick = !!submit && now\.click_submit === true;", h)
    assert "applyDecision(startUrl, wc.getURL()" in h


def test_unreachable_engine_means_no_click_and_no_fill():
    fn = MAIN_JS[MAIN_JS.index("async function applyDecision"):]
    fn = fn[:fn.index("\n}\n")]
    assert "/api/submit/decision" in fn
    # every failure path returns a closed decision
    assert fn.count("{ fill: false, click_submit: false }") == 2
    assert "d.click_submit === true" in fn


def test_old_fill_handlers_are_gone():
    assert 'ipcMain.handle("assist:fill"' not in MAIN_JS
    assert 'ipcMain.handle("assist:fill-current"' not in MAIN_JS


# ---------------------------------------------------------------- one button, one path (app.js)

def test_every_apply_button_uses_the_one_path():
    # The package page and the Auto-apply page (its rows and drawer share aaFill) call applyInPane.
    assert APP_JS.count("applyInPane({") == 2
    assert "assistFill(" not in APP_JS and "assistFillCurrent(" not in APP_JS
    for gone in ("pkgAutoBtn", "pkgAutoHereBtn", "pkgAssistBtn", "pkgAssistHereBtn"):
        assert gone not in APP_JS
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    for gone in ("pkgAutoBtn", "pkgAutoHereBtn", "pkgAssistBtn", "pkgAssistHereBtn", "pkgAssistCard"):
        assert gone not in html
    assert 'id="pkgFillBtn"' in html and 'id="pkgFillHereBtn"' in html


def test_fill_box_reads_apply_url_and_resets_per_package():
    assert "PKG_FILL_URL = p.apply_url ||" in APP_JS
    assert '$("#pkgAssistUrl").value = PKG_FILL_URL;' in APP_JS
    assert "p.job_url || p.url || \"\";\n    }" not in APP_JS
