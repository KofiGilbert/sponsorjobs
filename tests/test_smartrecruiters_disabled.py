"""SmartRecruiters is switched off: https://api.smartrecruiters.com/robots.txt reads
"User-agent: LinkedInBot / Allow: /v1/companies/ / User-agent: * / Disallow: /" (checked
2026-10-19), so every automated request we would make to that API is disallowed (CLAUDE.md: obey
robots.txt, never evade). sourcing.ats.DISABLED_ATS records the decision; these tests pin that no
code path -- the feed's board crawl, the first-open crawl, the per-posting description, the detail
views' fill, discovery, the watchlist add endpoint -- sends a request to api.smartrecruiters.com,
and that the boards stay on the watchlist (inactive), so the decision is reversible.
"""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pytest

from sourcing import ats, discover, feedclient, service
from sourcing.watchlist import Watchlist

ROOT = Path(__file__).resolve().parents[1]
SR_HOST = "api.smartrecruiters.com"


class Recorder:
    """A fetch that records every URL and answers nothing (a 404 for everything)."""

    def __init__(self):
        self.urls: list[str] = []

    def __call__(self, url, *a, **k):
        self.urls.append(url)
        raise Exception("404")

    def sr(self) -> list[str]:
        return [u for u in self.urls if urlparse(u).hostname == SR_HOST]


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Any request that slips past an injected fetch (a default-arg fetch_json) lands here."""
    seen: list[str] = []

    def urlopen(req, *a, **k):
        seen.append(getattr(req, "full_url", req))
        raise OSError("network disabled in this test")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    yield seen
    assert not [u for u in seen if SR_HOST in str(u)], seen


def test_smartrecruiters_is_recorded_as_disabled_with_reason_and_date():
    d = ats.DISABLED_ATS["smartrecruiters"]
    assert d["host"] == SR_HOST and d["checked"] == "2026-10-19"
    assert "robots.txt" in d["reason"] and "LinkedInBot" in d["reason"]
    assert ats.ats_disabled("smartrecruiters") and ats.ats_disabled("SmartRecruiters ")
    assert not ats.ats_disabled("greenhouse") and not ats.ats_disabled("workday")


def test_the_adapter_and_detail_paths_make_no_request():
    rec = Recorder()
    assert ats.smartrecruiters_jobs("acme", "Acme", fetch=rec) == []
    assert ats.smartrecruiters_jobs("acme", "Acme", fetch=rec, details=True) == []
    assert ats.normalize_jobs("smartrecruiters", "acme", "Acme", fetch=rec) == []
    assert ats.smartrecruiters_detail("acme", "p1", fetch=rec, sleep=lambda s: None) == ("", "")
    row = {"source_id": "smartrecruiters:acme:p1", "url": "https://jobs.smartrecruiters.com/acme/p1"}
    assert ats.smartrecruiters_fill(row, fetch=rec, sleep=lambda s: None) is False
    assert rec.urls == []


def test_the_app_detail_views_make_no_request():
    rec = Recorder()
    job = {"source_id": "smartrecruiters:acme:p1", "source": "smartrecruiters",
           "url": "https://jobs.smartrecruiters.com/acme/p1", "jd_text": "",
           "title": "Data Engineer", "company": "Acme"}
    assert feedclient.fetch_board_jd(job, fetch=rec) == ""
    assert feedclient.fill_list_only_detail(job, fetch=rec) == {}
    assert rec.urls == []                       # not even freehire's lookup: the row is skipped


def _watchlist_with_a_smartrecruiters_board() -> Watchlist:
    w = Watchlist(":memory:")
    w.add_company("Acme", "smartrecruiters", "acme")
    w.add_company("Beta", "greenhouse", "beta")
    w.set_criteria({"titles": [], "locations": [], "remote": "any", "aggregators": []})
    return w


def test_a_smartrecruiters_board_is_kept_on_the_watchlist_but_inactive():
    w = _watchlist_with_a_smartrecruiters_board()
    every = {(c["ats"], c["board_id"]): c["active"] for c in w.companies(active_only=False)}
    assert every == {("smartrecruiters", "acme"): 0, ("greenhouse", "beta"): 1}
    assert [c["ats"] for c in w.companies(active_only=True)] == ["greenhouse"]
    w.add_company("Acme", "smartrecruiters", "acme")       # a seed re-sync does not revive it
    assert [c["ats"] for c in w.companies(active_only=True)] == ["greenhouse"]


def test_an_existing_active_smartrecruiters_board_is_switched_off_on_open(tmp_path):
    db = tmp_path / "jobs.db"
    w = Watchlist(db)
    w.add_company("Beta", "greenhouse", "beta")
    w._conn.execute("INSERT INTO watchlist(company, ats, board_id, active) "
                    "VALUES('Acme','smartrecruiters','acme',1)")      # stored before the switch
    w._conn.commit()
    w.close()
    w = Watchlist(db)
    assert [c["ats"] for c in w.companies(active_only=True)] == ["greenhouse"]
    assert len(w.companies(active_only=False)) == 2                    # kept, not deleted
    w.close()


def test_switching_it_back_on_revives_the_kept_boards(monkeypatch):
    w = _watchlist_with_a_smartrecruiters_board()
    monkeypatch.setattr(ats, "DISABLED_ATS", {})
    w.add_company("Acme", "smartrecruiters", "acme")       # what the feed build's sync_seed does
    assert {c["ats"] for c in w.companies(active_only=True)} == {"greenhouse", "smartrecruiters"}


def test_the_board_crawl_skips_smartrecruiters_even_when_handed_the_board():
    w = _watchlist_with_a_smartrecruiters_board()
    rec = Recorder()
    boards = w.companies(active_only=False)                # hand it the inactive row too
    s = service.refresh_watchlist(w, fetch=rec, boards=boards, sleep=lambda s: None,
                                  include_aggregators=False)
    assert rec.sr() == [] and rec.urls                     # greenhouse was still crawled
    assert s["boards_disabled"] == 1 and s["boards_crawled"] == 1
    s = service.refresh_watchlist(w, fetch=rec, sleep=lambda s: None)   # the default board list
    assert rec.sr() == []


def test_the_first_open_crawl_makes_no_smartrecruiters_request():
    w = _watchlist_with_a_smartrecruiters_board()
    rec = Recorder()
    service.first_open_refresh(w, fetch=rec, max_boards=50, max_minutes=1, approvals=lambda c: 0,
                               sleep=lambda s: None)
    assert rec.sr() == []


def test_discovery_never_probes_smartrecruiters():
    assert "smartrecruiters" not in discover.DISCOVER_ATS
    assert "smartrecruiters" in discover.NAME_LANE        # its dormant identity check is kept
    rec = Recorder()
    assert discover.probe("smartrecruiters", "experian", fetch=rec) == 0
    assert discover.board_name("smartrecruiters", "experian", fetch=rec) == ""
    assert discover.discover_for_company("Experian PLC", atss=("smartrecruiters",), fetch=rec) is None
    assert rec.urls == []


def test_the_watchlist_endpoint_refuses_a_smartrecruiters_board(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    r = app.app.test_client().post("/api/watchlist", json={"ats": "smartrecruiters", "board_id": "acme"})
    assert r.status_code == 400 and "robots.txt" in r.get_json()["error"]
    assert "smartrecruiters" not in service.SUPPORTED_ATS


def test_the_seed_keeps_its_smartrecruiters_boards():
    text = (ROOT / "seed" / "watchlist_seed.csv").read_text(encoding="utf-8")
    assert text.count(",smartrecruiters,") >= 100        # kept on purpose: reversible


# Every file that names the API host, and why it may. A new file that builds an
# api.smartrecruiters.com URL fails here until it goes through ats.DISABLED_ATS.
ALLOWED = {
    "sourcing/ats.py": "the dormant adapter, gated by ats_disabled + the DISABLED_ATS record",
    "sourcing/discover.py": "the dormant probe / board_name, gated by ats_disabled",
    "sourcing/growth.py": "robots.txt read + evidence URL + host->ATS map; never a crawl",
    "submit/allowlist.py": "submission evidence text (ASSISTED); never requested",
}
SCANNED = ("sourcing", "ui", "backend", "scripts", "extension", "shell-electron", "submit",
           "autoapply", "inbox", ".github")


def test_no_other_file_talks_to_the_smartrecruiters_api():
    hits = set()
    for top in SCANNED:
        for p in (ROOT / top).rglob("*"):
            if not p.is_file() or "node_modules" in p.parts or p.suffix not in (
                    ".py", ".js", ".mjs", ".json", ".html", ".yml", ".yaml", ".sh", ".ts"):
                continue
            if re.search(r"api\.smartrecruiters\.com", p.read_text(encoding="utf-8", errors="ignore")):
                hits.add(p.relative_to(ROOT).as_posix())
    assert hits <= set(ALLOWED), sorted(hits - set(ALLOWED))
