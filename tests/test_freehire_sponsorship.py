"""Freehire visa-sponsorship signal (feature #5).

freehire's job enrichment states whether a POSTING offers visa sponsorship (verified: a real,
server-side ``visa_sponsorship=true`` filter, and a per-row ``enrichment.visa_sponsorship`` bool).
We capture that per row -- the strongest, most current sponsorship signal for an international
student -- persist it as a tri-state, and let the refresh pull a guaranteed slice of sponsor roles.
No network: an injected fetch feeds canned JSON.
"""
from __future__ import annotations

from sourcing.ats import _freehire_row, freehire_bulk
from sourcing.watchlist import Watchlist


def _row(slug, sponsorship):
    j = {"public_slug": slug, "company": "Acme", "title": "Engineer", "location": "Austin, TX",
         "url": "https://x/1", "description": "Build systems.", "posted_at": "2026-08-10"}
    if sponsorship is not None:
        j["enrichment"] = {"visa_sponsorship": sponsorship}
    return j


def test_freehire_row_captures_the_stated_flag():
    assert _freehire_row(_row("a", True))["sponsorship_stated"] is True
    assert _freehire_row(_row("b", False))["sponsorship_stated"] is False
    # Silent posting (no enrichment / no key) -> None, we never infer "no" from silence.
    assert _freehire_row(_row("c", None))["sponsorship_stated"] is None
    assert _freehire_row({"public_slug": "d", "company": "C", "title": "T",
                          "enrichment": {}})["sponsorship_stated"] is None


def test_bulk_sends_the_verified_visa_param_only_when_asked():
    seen = []
    def fetch(url, **k):
        seen.append(url)
        return {"data": []}
    freehire_bulk(fetch=fetch, cap=100)
    assert not any("visa_sponsorship" in u for u in seen)     # default: no filter (full volume)
    seen.clear()
    freehire_bulk(fetch=fetch, cap=100, visa_sponsorship=True)
    assert all("visa_sponsorship=true" in u for u in seen)    # opt-in: freehire's own filter


def test_watchlist_persists_and_reads_the_tri_state():
    w = Watchlist(":memory:")
    w.upsert_jobs([
        {**_base("yes"), "sponsorship_stated": True},
        {**_base("no"), "sponsorship_stated": False},
        {**_base("unknown"), "sponsorship_stated": None},
    ])
    by = {j["source_id"]: j for j in w.list_jobs()}
    assert by["freehire::yes"]["sponsorship_stated"] is True
    assert by["freehire::no"]["sponsorship_stated"] is False
    assert by["freehire::unknown"]["sponsorship_stated"] is None
    # get_job returns the same normalized tri-state.
    assert w.get_job("freehire::yes")["sponsorship_stated"] is True


def test_a_silent_reingest_never_erases_a_known_flag():
    # A role stated sponsorship once; a later general pull is silent about it (None). The badge must
    # survive (COALESCE keeps the known flag) rather than flicker off.
    w = Watchlist(":memory:")
    w.upsert_jobs([{**_base("keep"), "sponsorship_stated": True}])
    w.upsert_jobs([{**_base("keep"), "sponsorship_stated": None}])   # silent re-ingest
    assert w.get_job("freehire::keep")["sponsorship_stated"] is True


def _base(jid):
    return {"source_id": f"freehire::{jid}", "source": "freehire", "company": "Acme",
            "title": "Engineer", "location": "Austin, TX", "remote": "", "url": "https://x/1",
            "jd_text": "Build systems.", "posted_at": "2026-08-10", "salary": ""}
