"""All jobs, sponsors first (2026-11-20).

The feed carries general (non-sponsor) rows next to the sponsor rows, capped and marked; the
manifest reports both counts; the sign-up question "Do you need visa sponsorship to work in the
US?" is saved into the work-authorization profile; and the board's scope (Sponsors only / All jobs)
defaults from that answer while the person's own pick wins. Fixtures only, no network."""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autoapply import workauth as W
from sourcing.filters import apply_facets, default_scope, effective_scope, scope_rows
from sourcing.quality import looks_english

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- the feed
def _feed_db(tmp_path, monkeypatch, rows):
    import backend.feed as feed
    from sourcing.watchlist import Watchlist
    jobs_db = tmp_path / "jobs.db"
    monkeypatch.setattr(feed, "_paths", lambda: (str(jobs_db), str(tmp_path / "s.db")))

    class _Tag:
        def tag_jobs(self, rs):
            for j in rs:
                j["us"] = True
                j["visa"] = ["H-1B"] if j.get("company") == "Stripe" else []
            return rs
    monkeypatch.setattr(feed, "_sponsors", lambda _p: _Tag())
    w = Watchlist(str(jobs_db))
    w.upsert_jobs(rows)
    w.close()
    return feed


def _row(sid, source, company, title, posted, jd="We are hiring."):
    return {"source_id": sid, "source": source, "company": company, "title": title,
            "location": "Austin, TX", "url": "https://example.com/" + sid, "jd_text": jd, "remote": "",
            "posted_at": posted}


ROWS = [
    _row("greenhouse::1", "greenhouse", "Acme Board Co", "Software Engineer", "2026-11-01"),
    _row("freehire::stripe", "freehire", "Stripe", "Data Engineer", "2026-11-02"),
    _row("freehire::new", "freehire", "Half Brothers Brewing", "Event Server", "2026-11-19"),
    _row("freehire::mid", "freehire", "Corner Bakery", "Shift Lead", "2026-11-10"),
    _row("freehire::old", "freehire", "Main Street Dental", "Front Desk Coordinator", "2026-11-03"),
    _row("freehire::es", "freehire", "Ciudad Foods", "Gerente de Tienda", "2026-11-18"),
    _row("freehire::no", "freehire", "Lakeside Clinic", "Medical Assistant", "2026-11-17",
         jd="We are unable to sponsor visas for this role."),
]


def test_general_rows_are_off_until_the_cap_is_set(tmp_path, monkeypatch):
    monkeypatch.delenv("FEED_GENERAL_MAX", raising=False)
    feed = _feed_db(tmp_path, monkeypatch, ROWS)
    got = {j["source_id"]: j["sponsor_relevant"] for j in feed.feed_jobs()}
    assert got == {"greenhouse::1": True, "freehire::stripe": True}     # the feed exactly as before


def test_general_rows_are_marked_capped_newest_first_and_english(tmp_path, monkeypatch):
    monkeypatch.setenv("FEED_GENERAL_MAX", "2")
    feed = _feed_db(tmp_path, monkeypatch, ROWS)
    got = {j["source_id"]: j["sponsor_relevant"] for j in feed.feed_jobs()}
    # Sponsor rows are never capped; the two newest English general rows (one of them an ad that
    # says it will not sponsor) join them; the Spanish title and the older rows stay out.
    assert got == {"greenhouse::1": True, "freehire::stripe": True,
                   "freehire::new": False, "freehire::no": False}
    assert feed.FUNNEL["kept_general"] == 2 and feed.FUNNEL["kept_sponsor"] == 2
    monkeypatch.setenv("FEED_GENERAL_MAX", "50")
    got = {j["source_id"] for j in feed.feed_jobs()}
    assert "freehire::es" not in got and {"freehire::mid", "freehire::old"} <= got


def test_general_max_reads_the_env_and_survives_junk():
    from backend.feed import general_max
    assert general_max({}) == 0
    assert general_max({"FEED_GENERAL_MAX": "50000"}) == 50000
    assert general_max({"FEED_GENERAL_MAX": "-3"}) == 0
    assert general_max({"FEED_GENERAL_MAX": "lots"}) == 0


def test_looks_english():
    for t in ("Software Engineer", "Barista", "Director of Sales", "Cafe Manager (Part Time)"):
        assert looks_english(t), t
    for t in ("Gerente de Tienda", "Entwickler (m/w/d)", "データエンジニア", ""):
        assert not looks_english(t), t


def test_manifest_counts_every_job_and_the_sponsor_ones(tmp_path):
    from sourcing.feedfile import load_feed_file, write_feed
    rows = [
        {"source_id": "greenhouse::1", "source": "greenhouse", "company": "Acme", "title": "Engineer"},
        {"source_id": "freehire::2", "source": "freehire", "company": "Stripe", "title": "Analyst",
         "visa": [{"code": "H-1B"}]},
        {"source_id": "freehire::3", "source": "freehire", "company": "Bakery", "title": "Baker", "visa": []},
        {"source_id": "greenhouse::4", "source": "greenhouse", "company": "Acme", "title": "Designer"},
    ]
    jd = {"greenhouse::4": "We will not sponsor a visa for this position."}
    man = write_feed(tmp_path, rows, jd, generated_at="2026-11-20T00:00:00+00:00")
    assert man["count"] == 4 and man["sponsor_count"] == 2
    on_disk = json.loads((tmp_path / "manifest.json").read_text())
    assert on_disk["count"] == 4 and on_disk["sponsor_count"] == 2
    data = load_feed_file(tmp_path / "jobs.json.gz")
    flags = {r["source_id"]: r["sponsor_relevant"] for r in data["jobs"]}
    # The board row whose own ad refuses sponsorship is a general row, like the bakery.
    assert flags == {"greenhouse::1": True, "freehire::2": True, "freehire::3": False, "greenhouse::4": False}
    assert data["sponsor_count"] == 2


def test_workflow_turns_the_general_rows_on_and_caps_them():
    wf = (ROOT / ".github" / "workflows" / "feed.yml").read_text(encoding="utf-8")
    assert "FREEHIRE_GENERAL_BULK: ${{ vars.FREEHIRE_GENERAL_BULK || 1 }}" in wf
    assert "FEED_GENERAL_MAX: ${{ vars.FEED_GENERAL_MAX || 50000 }}" in wf


def test_site_reads_sponsor_count_with_a_fallback():
    js = (ROOT / "site" / "script.js").read_text(encoding="utf-8")
    assert "m.sponsor_count != null ? m.sponsor_count : m.count" in js


# --------------------------------------------------------------------------- the default scope
@pytest.mark.parametrize("answer, scope", [("yes", "sponsors"), ("unsure", "sponsors"),
                                           ("", "sponsors"), ("no", "all"), ("No", "all")])
def test_default_scope_follows_the_answer(answer, scope):
    assert default_scope(answer) == scope
    assert effective_scope("", answer) == scope


@pytest.mark.parametrize("answer", ["yes", "no", "unsure", ""])
def test_a_manual_choice_wins(answer):
    assert effective_scope("all", answer) == "all"
    assert effective_scope("sponsors", answer) == "sponsors"
    assert effective_scope("bogus", answer) == default_scope(answer)


def test_scope_filters_and_old_rows_count_as_sponsor_rows():
    rows = [{"source_id": "a", "title": "A", "sponsor_relevant": True},
            {"source_id": "b", "title": "B", "sponsor_relevant": False},
            {"source_id": "c", "title": "C"}]                 # a feed from before the flag
    ids = lambda rs: [r["source_id"] for r in rs]
    assert ids(scope_rows(rows, "sponsors")) == ["a", "c"]
    assert ids(scope_rows(rows, "all")) == ["a", "b", "c"]
    assert ids(apply_facets(rows, scope="sponsors")) == ["a", "c"]
    assert ids(apply_facets(rows)) == ["a", "b", "c"]          # older callers filter nothing


# --------------------------------------------------------------------------- the question's store
def test_answers_round_trip_through_the_work_auth_profile():
    for ans, now, fut in (("yes", "Yes", "Yes"), ("no", "No", "No"), ("unsure", "", "Yes")):
        wa = W.set_sponsor_answer({}, ans)
        us = wa["countries"]["US"]
        assert (us["sponsor_now"], us["sponsor_future"]) == (now, fut), ans
        assert W.sponsor_answer(wa) == ans
        assert W.has_us_answers(wa)
    assert W.sponsor_answer({}) == "" and not W.has_us_answers({})
    with pytest.raises(ValueError):
        W.set_sponsor_answer({}, "maybe")


def test_saving_yes_keeps_an_older_future_answer_and_other_fields():
    wa = W.from_legacy({"needs_sponsorship": "Future", "work_auth_us": "Yes"})
    wa["countries"]["US"]["status"] = "f1_opt"
    assert W.sponsor_answer(wa) == "yes"
    same = W.set_sponsor_answer(wa, "yes")
    assert same["countries"]["US"]["sponsor_now"] == "No"          # not flattened to Yes/Yes
    no = W.set_sponsor_answer(wa, "no")["countries"]["US"]
    assert no["status"] == "f1_opt" and no["authorized"] == "Yes"


def test_a_citizen_reads_no_and_a_contradicting_answer_clears_the_status():
    wa = W.clean({"countries": {"US": {"status": "us_citizen"}}})
    assert W.sponsor_answer(wa) == "no"
    yes = W.set_sponsor_answer(wa, "yes")
    assert yes["countries"]["US"]["status"] == "" and W.sponsor_answer(yes) == "yes"


# --------------------------------------------------------------------------- the app
@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    return A


def _board(A, monkeypatch):
    rows = [{"source_id": "s1", "source": "greenhouse", "company": "Acme", "title": "Engineer",
             "location": "Austin, TX", "us": True, "sponsor_relevant": True, "visa": []},
            {"source_id": "g1", "source": "freehire", "company": "Bakery", "title": "Baker",
             "location": "Austin, TX", "us": True, "sponsor_relevant": False, "visa": []},
            {"source_id": "g2", "source": "freehire", "company": "Garage", "title": "Mechanic",
             "location": "Austin, TX", "us": True, "sponsor_relevant": False, "visa": []}]

    class _Feed:
        def jobs(self):
            return rows, {"generated_at": None}
    monkeypatch.setattr(A, "_feed_base_url", lambda: "https://feed.example")
    monkeypatch.setattr(A, "_static_feed", lambda: _Feed())
    return A.app.test_client()


def test_question_is_asked_once_and_saved_into_the_work_auth_profile(A, monkeypatch):
    c = _board(A, monkeypatch)
    st = c.get("/api/workauth/sponsorship").get_json()
    assert st["ask"] is True and st["answer"] == "" and st["scope"] == "sponsors"
    st = c.post("/api/workauth/sponsorship", json={"answer": "no"}).get_json()
    assert st["ask"] is False and st["answer"] == "no" and st["scope"] == "all"
    us = A._load_prefs()["work_auth"]["countries"]["US"]
    assert (us["sponsor_now"], us["sponsor_future"]) == ("No", "No")
    assert A._load_prefs()["needs_sponsorship"] == "No"            # the flat answer forms read
    assert c.post("/api/workauth/sponsorship", json={"answer": "maybe"}).status_code == 400


def test_a_person_with_a_work_auth_profile_is_not_asked(A, monkeypatch):
    c = _board(A, monkeypatch)
    A._merge_prefs({"work_auth": {"countries": {"US": {"status": "h1b"}}}})
    assert c.get("/api/workauth/sponsorship").get_json()["ask"] is False


def test_not_now_stops_asking_and_keeps_the_sponsor_board(A, monkeypatch):
    c = _board(A, monkeypatch)
    st = c.post("/api/workauth/sponsorship", json={"skip": True}).get_json()
    assert st["ask"] is False and st["answer"] == "" and st["scope"] == "sponsors"


def test_board_default_and_manual_override(A, monkeypatch):
    c = _board(A, monkeypatch)
    ids = lambda q="": sorted(j["source_id"] for j in c.get("/api/jobs" + q).get_json()["jobs"])
    # Not answered yet, Yes and Not sure: Sponsors only, with an honest "N of M".
    r = c.get("/api/jobs").get_json()
    assert r["scope"] == "sponsors" and r["count"] == 1 and r["total"] == 3
    for ans in ("yes", "unsure"):
        c.post("/api/workauth/sponsorship", json={"answer": ans})
        assert ids() == ["s1"] and ids("?home=1") == ["s1"]
    # No: All jobs, on Jobs and Home alike.
    c.post("/api/workauth/sponsorship", json={"answer": "no"})
    assert ids() == ["g1", "g2", "s1"] and ids("?home=1") == ["g1", "g2", "s1"]
    # The person's own pick wins over the answer, is remembered, and "" goes back to the default.
    st = c.post("/api/jobs/scope", json={"scope": "sponsors"}).get_json()
    assert st["scope"] == "sponsors" and st["manual"] == "sponsors" and st["default"] == "all"
    assert ids() == ["s1"]
    assert ids("?scope=all") == ["g1", "g2", "s1"]                 # the page says it outright
    c.post("/api/workauth/sponsorship", json={"answer": "yes"})
    c.post("/api/jobs/scope", json={"scope": "all"})
    assert ids() == ["g1", "g2", "s1"]
    assert c.post("/api/jobs/scope", json={"scope": ""}).get_json()["scope"] == "sponsors"
    assert c.post("/api/jobs/scope", json={"scope": "everything"}).status_code == 400


def test_auto_apply_picks_from_the_same_scope_as_jobs(A, monkeypatch):
    _board(A, monkeypatch)
    monkeypatch.setattr(A, "_board_rows", lambda: A._static_feed().jobs()[0])
    seen = {}
    import autoapply.picker as P

    def fake(board, saved, **kw):
        seen["ids"] = sorted(j["source_id"] for j in board)
        seen["sponsor_only"] = kw["sponsor_only"]
        return []
    monkeypatch.setattr(P, "candidates", fake)
    A._aa_candidates({})
    assert seen["ids"] == ["s1"] and seen["sponsor_only"] is False   # unanswered: the sponsor board
    A._merge_prefs({"work_auth": W.set_sponsor_answer({}, "no")})
    A._aa_candidates({})
    assert seen["ids"] == ["g1", "g2", "s1"]                          # No: every job
    A._set_prefs({"jobs_scope": "sponsors"})
    A._aa_candidates({"sponsor_only": True})
    assert seen["ids"] == ["s1"] and seen["sponsor_only"] is True     # the person's own picks win


# --------------------------------------------------------------------------- the page
@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_jobs_scope_js():
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_jobs_scope.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr


def test_page_wires_the_scope_and_the_question():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'p.set("scope", JF_SCOPE)' in js and "await loadScopeState();" in js
    assert 'id="sponsorAsk"' in html and 'data-scope="sponsors"' in html and 'data-scope="all"' in html
    assert 'id="memSponsorSeg"' in html
    assert html.index("jobs_scope.js") < html.index("filename='app.js'")
