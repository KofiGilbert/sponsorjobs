"""Cross-feed dedup (sourcing/dedup.py): one opening reached through two feeds is shown once,
and a refresh of an already-known posting is never mistaken for a duplicate."""

from __future__ import annotations

from sourcing.dedup import DedupIndex, role_key, url_key
from sourcing.service import refresh_watchlist
from sourcing.watchlist import Watchlist


def test_url_key_strips_tracking_but_keeps_functional_params():
    a = url_key("https://boards.greenhouse.io/acme/jobs/123?gh_src=abc&utm_source=x")
    b = url_key("https://BOARDS.greenhouse.io/acme/jobs/123/#apply")
    assert a == b == "https://boards.greenhouse.io/acme/jobs/123"
    # gh_jid selects the job: two ids stay two keys.
    assert url_key("https://stripe.com/jobs?gh_jid=1") != url_key("https://stripe.com/jobs?gh_jid=2")
    assert url_key("mailto:jobs@acme.com") == "" and url_key("") == ""


def test_role_key_ignores_trailing_location_tags_and_city_order():
    assert role_key("Acme", "Data Engineer (Remote)", "Chicago, IL; Remote") == \
        role_key("ACME", "Data Engineer", "Remote | Chicago, IL")
    # A comma is not a separator: "Chicago, IL" is one place, so it differs from Chicago alone.
    assert role_key("Acme", "Data Engineer", "Chicago, IL") != role_key("Acme", "Data Engineer", "Chicago")
    assert role_key("", "Engineer") == ""


def _job(sid, company, title, url, location="Remote"):
    return {"source_id": sid, "source": sid.split(":")[0], "company": company, "title": title,
            "location": location, "remote": "remote", "url": url, "jd_text": "x", "posted_at": ""}


def test_index_drops_the_same_posting_from_a_second_feed_but_not_a_refresh():
    gh = _job("greenhouse:acme:1", "Acme", "ML Engineer",
              "https://boards.greenhouse.io/acme/jobs/1?gh_src=feed")
    idx = DedupIndex([gh])
    again = _job("greenhouse:acme:1", "Acme", "ML Engineer", "https://boards.greenhouse.io/acme/jobs/1")
    assert not idx.is_duplicate(again)                     # same source_id: a refresh
    via_agg = _job("remotive:0:77", "Acme", "ML Engineer (Remote)",
                   "https://boards.greenhouse.io/acme/jobs/1#apply")
    assert idx.is_duplicate(via_agg)                        # same URL, different door
    other = _job("remotive:0:78", "Acme", "Data Engineer", "https://remotive.com/j/78")
    kept, dropped = idx.filter([via_agg, other])
    assert kept == [other] and dropped == 1


def test_refresh_reports_duplicates_and_stores_the_opening_once(tmp_path):
    store = Watchlist(str(tmp_path / "w.db"))
    store.add_company("Acme", "greenhouse", "acme")
    store.set_criteria({"titles": ["engineer"], "remote": "any", "aggregators": ["remotive"]})
    gh = {"jobs": [{"id": 1, "title": "ML Engineer", "location": {"name": "Remote"},
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "content": "Train models."}]}
    remotive = {"jobs": [{"id": 77, "title": "ML Engineer", "company_name": "Acme",
                          "candidate_required_location": "Remote",
                          "url": "https://boards.greenhouse.io/acme/jobs/1?utm_source=remotive",
                          "description": "Train models.", "publication_date": "2026-07-01"}]}

    def fetch(url, timeout=20):
        return gh if "greenhouse" in url else remotive

    out = refresh_watchlist(store, fetch=fetch)
    assert out["duplicates"] == 1 and out["new"] == 1
    assert len(store.list_jobs()) == 1
    store.close()
