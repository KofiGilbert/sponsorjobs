"""The product lists sponsor-relevant rows only (decided 2026-10-01), and a time-capped crawl
covers the cheap boards before spending minutes on Workday / SmartRecruiters tenants."""
from __future__ import annotations

from sourcing.quality import is_sponsor_relevant
from sourcing.service import rank_boards


def test_board_rows_always_fit_the_promise():
    assert is_sponsor_relevant({"source": "greenhouse", "company": "Stripe"})
    assert is_sponsor_relevant({"source": "workday", "company": "CVS Health"})


def test_aggregator_rows_need_a_badge_or_a_stated_sponsorship():
    assert not is_sponsor_relevant({"source": "freehire", "title": "Event Server", "visa": []})
    assert is_sponsor_relevant({"source": "freehire", "title": "Data Engineer", "visa": ["H-1B"]})
    assert is_sponsor_relevant({"source": "jsearch", "sponsorship_stated": True, "visa": []})
    assert not is_sponsor_relevant({"source": "remotive", "sponsorship_stated": None, "visa": []})


def test_feed_jobs_drops_unrelated_aggregator_rows(tmp_path, monkeypatch):
    import backend.feed as feed
    from sourcing.watchlist import Watchlist
    jobs_db = tmp_path / "jobs.db"
    monkeypatch.setattr(feed, "_paths", lambda: (str(jobs_db), str(tmp_path / "s.db")))

    class _NoSponsors:
        def tag_jobs(self, rows):
            for j in rows:
                j["us"] = True
                j["visa"] = ["H-1B"] if j.get("company") == "Stripe" else []
            return rows
    monkeypatch.setattr(feed, "_sponsors", lambda _p: _NoSponsors())
    w = Watchlist(str(jobs_db))
    w.upsert_jobs([
        {"source_id": "greenhouse::1", "source": "greenhouse", "company": "Acme Board Co",
         "title": "Software Engineer", "location": "Austin, TX", "url": "https://a", "jd_text": "x", "remote": "",
         "posted_at": "2026-10-01"},
        {"source_id": "freehire::2", "source": "freehire", "company": "Half Brothers Brewing",
         "title": "Event Server", "location": "Tampa, FL", "url": "https://b", "jd_text": "x", "remote": "",
         "posted_at": "2026-10-01"},
        {"source_id": "freehire::3", "source": "freehire", "company": "Stripe",
         "title": "Data Engineer", "location": "NYC", "url": "https://c", "jd_text": "x", "remote": "",
         "posted_at": "2026-10-01"},
    ])
    w.close()
    got = {j["source_id"] for j in feed.feed_jobs()}
    assert got == {"greenhouse::1", "freehire::3"}


def test_cheap_boards_come_before_list_only_tenants():
    boards = [
        {"company": "CVS Health", "ats": "workday", "board_id": "cvs.wd1/External"},
        {"company": "AbbVie", "ats": "smartrecruiters", "board_id": "AbbVie"},
        {"company": "Dropbox", "ats": "greenhouse", "board_id": "dropbox"},
        {"company": "Spotify", "ats": "lever", "board_id": "spotify"},
    ]
    approvals = {"CVS Health": 5000, "AbbVie": 900, "Dropbox": 200, "Spotify": 100}.get
    order = [b["ats"] for b in rank_boards(boards, lambda c: approvals(c, 0), 10)]
    assert order == ["greenhouse", "lever", "workday", "smartrecruiters"]


def test_general_bulk_dump_is_off_by_default(monkeypatch, tmp_path):
    import sourcing.service as svc
    from sourcing.watchlist import Watchlist
    calls = []

    def fake_bulk(fetch=None, cap=None, visa_sponsorship=False):
        calls.append(visa_sponsorship)
        return []
    monkeypatch.setattr(svc, "freehire_bulk", fake_bulk)
    monkeypatch.delenv("FREEHIRE_GENERAL_BULK", raising=False)
    w = Watchlist(str(tmp_path / "j.db"))
    svc.refresh_watchlist(w, fetch=lambda *a, **k: {}, boards=[])
    assert calls == [True]                       # only the sponsor slice ran
    monkeypatch.setenv("FREEHIRE_GENERAL_BULK", "1")
    calls.clear()
    svc.refresh_watchlist(w, fetch=lambda *a, **k: {}, boards=[])
    assert calls == [False, True]
    w.close()
