"""The country note before Autofill reads the page when the record does not say (2026-11-26).

On instacart.careers the owner pressed Autofill on the Canada posting of a role and no note came:
the bar's Autofill fills the page already open, which skipped the note, and the record's job had no
location (its posting says "Remote" and names Canada only in its JobPosting's
applicantLocationRequirements). Now Autofill on an open page asks too, once per application and
page, and the engine reads the country from the page's own HTML (no request).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import ui.app as app
from sourcing.posting_read import from_html

ROOT = Path(__file__).resolve().parents[1]
US_OPT = {"countries": {"US": {"status": "", "authorized": "Yes", "sponsor_now": "No", "sponsor_future": "Yes",
                               "auth_end": ""}}}
PAGE = """<!doctype html><html><head><title>Business Intelligence Manager II | Example Grocer</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "JobPosting",
 "title": "Business Intelligence Manager II", "hiringOrganization": {"@type": "Organization", "name": "Example Grocer"},
 "jobLocationType": "TELECOMMUTE", "applicantLocationRequirements": {"@type": "Country", "name": "CA"},
 "description": "<p>Lead analytics for our partners. Requirements: SQL, dashboards, stakeholder management.</p>"}</script>
</head><body><div id="app"></div></body></html>"""


def test_a_remote_posting_names_its_country_from_the_jobposting():
    res = from_html("https://careers.example-grocer.test/job/?gh_jid=1", PAGE)
    assert res["location"] == "Remote (Canada)"                    # CA is Canada, never California
    assert app._job_country({"location": res["location"]}) == "CA"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(app, "_work_auth", lambda: US_OPT)
    recs = app._records()
    try:
        rid = recs.add("Business Intelligence Manager II", "Example Grocer", 70, "",
                       data={"jd_text": "SQL.", "source_job": {"url": "https://careers.example-grocer.test/job/?gh_jid=1"}})
    finally:
        recs.close()
    c = app.app.test_client()
    c.rid = rid
    return c


def test_the_note_comes_from_the_open_page_when_the_record_does_not_say(client):
    H = {"X-Tailor-Extension": "1"}
    got = client.get(f"/api/autoapply/country-note?id={client.rid}", headers=H).get_json()
    assert got["note"] == "" and got["unknown"] is True              # open the job first, then ask again
    r = client.post("/api/autoapply/country-note", headers=H,
                    json={"id": client.rid, "url": "https://careers.example-grocer.test/job/?gh_jid=1", "html": PAGE})
    assert r.get_json()["note"] == "This job is in Canada; your saved work authorization is for the US"


def test_autofill_on_the_open_page_asks_too_once_per_application_and_page():
    src = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    body = src.split("async function applyInPaneRun(opts) {", 1)[1].split("\n  }\n", 1)[0]
    assert "countryGo(opts.recordId, !!opts.here" in body and "!opts.here &&" not in body
    go = src.split("async function countryGo(recordId, here, url) {", 1)[1].split("\n  }\n", 1)[0]
    assert "window.tailorShell.readPage" in go and "COUNTRY_ASKED" in go and '"/api/autoapply/country-note"' in go


def test_another_jobs_page_lets_the_last_application_go():
    """Instacart: the Canada posting was filled with the application of the US posting the owner had
    filled before (the same site, another gh_jid). The Browser going to another job lets the
    application go (shell-electron/job_identity.js decides, tests/js/test_job_identity.js); the
    next page of the same one keeps it."""
    src = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    ev = src.split('document.addEventListener("sj:pane-url"', 1)[1].split("\n  });", 1)[0]
    assert "window.tailorShell.sameJob" in ev and "dropCtx()" in ev and "FILL_BUSY" in ev
    assert 'document.dispatchEvent(new CustomEvent("sj:pane-url"' in src
