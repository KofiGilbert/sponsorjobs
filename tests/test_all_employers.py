"""Every employer, not only sponsors (2026-11-25).

The board serves every job seeker, so the crawl and the watchlist no longer leave an employer or
a role out for having no sponsorship record: the feed build stores roles a visa holder cannot
take and publishes them as GENERAL rows (never as sponsor roles), boards of employers with no
record join the watchlist through a list that marks their rows honestly, and discovery has a lane
for employers the H-1B / PERM data does not know. The desktop app's own crawl is unchanged.
Fixtures only, no network."""
from __future__ import annotations

import csv
import gzip
from pathlib import Path

import pytest

from sourcing import growth, quality
from sourcing.service import crawl_keeps, refresh_watchlist, select_checked_rows
from sourcing.watchlist import Watchlist

ROOT = Path(__file__).resolve().parents[1]
NO = "This position is not eligible for visa sponsorship. We will not sponsor."
CITIZEN = "US citizenship required. Active TS/SCI security clearance required."


# ------------------------------------------------------------------ what a crawl may store
def test_the_desktop_crawl_still_keeps_only_accessible_roles():
    assert crawl_keeps({"company": "Acme", "jd_text": "Great role."})
    assert not crawl_keeps({"company": "Acme", "jd_text": NO})
    assert not crawl_keeps({"company": "Acme", "jd_text": CITIZEN})
    assert not crawl_keeps({"company": "Lockheed Martin", "jd_text": "Great role."})


def test_the_feed_build_keeps_every_role():
    for job in ({"company": "Acme", "jd_text": NO}, {"company": "Acme", "jd_text": CITIZEN},
                {"company": "Lockheed Martin", "jd_text": "Great role."}):
        assert crawl_keeps(job, all_roles=True)


def _gh(jobs):
    """A canned Greenhouse board answer."""
    return {"jobs": [{"id": i, "title": t, "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{i}",
                      "location": {"name": "Austin, TX"}, "content": jd, "updated_at": "2026-11-20T00:00:00Z"}
                     for i, (t, jd) in enumerate(jobs, 1)]}


def _store(tmp_path):
    w = Watchlist(str(tmp_path / "jobs.db"))
    w.add_company("Acme Inc", "greenhouse", "acme")
    w.set_criteria({"titles": [], "locations": [], "remote": "any", "aggregators": []})
    return w


BOARD = [("Software Engineer", "Build things."), ("Account Manager", NO), ("Systems Engineer", CITIZEN)]


def test_refresh_drops_closed_roles_by_default_and_keeps_them_for_the_feed(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEHIRE_SPONSOR_ROWS", "0")
    fetch = lambda url, *a, **k: _gh(BOARD)                      # noqa: E731
    w = _store(tmp_path)
    try:
        s = refresh_watchlist(w, fetch=fetch, include_aggregators=False)
        assert s["matched"] == 1
        assert {j["title"] for j in w.list_jobs()} == {"Software Engineer"}
        s = refresh_watchlist(w, fetch=fetch, include_aggregators=False, all_roles=True)
        assert s["matched"] == 3
        assert {j["title"] for j in w.list_jobs()} == {"Software Engineer", "Account Manager", "Systems Engineer"}
    finally:
        w.close()


def test_a_list_only_row_is_still_read_before_it_is_kept(monkeypatch):
    """all_roles lifts the verdict, never the reading: a Workday row with no description fetched
    is not kept, so every published row carries what its ad says."""
    import sourcing.service as service
    rows = [{"source": "workday", "source_id": f"workday:t.wd1/s:{i}", "company": "Acme", "title": "Engineer",
             "location": "Austin, TX", "jd_text": "", "posted_at": "2026-11-20"} for i in range(3)]
    bodies = {"workday:t.wd1/s:0": "Build things.", "workday:t.wd1/s:1": NO}

    def fill(ats, job, fetch, sleep):
        job["jd_text"] = bodies.get(job["source_id"], "")
        return bool(job["jd_text"])
    monkeypatch.setattr(service, "_fill_detail", fill)
    kept, fetched, unchecked = select_checked_rows("workday", [dict(r) for r in rows], {}, max_detail=10)
    assert [j["source_id"][-1] for j in kept] == ["0"] and (fetched, unchecked) == (3, 1)
    kept, fetched, unchecked = select_checked_rows("workday", [dict(r) for r in rows], {}, max_detail=10,
                                                   all_roles=True)
    assert sorted(j["source_id"][-1] for j in kept) == ["0", "1"] and unchecked == 1   # 2 had no text


def test_build_feed_all_roles_switch():
    from scripts.build_feed import all_roles_enabled
    assert all_roles_enabled({}) is True
    assert all_roles_enabled({"FEED_ALL_ROLES": "1"}) is True
    for off in ("0", "off", "false", "no"):
        assert all_roles_enabled({"FEED_ALL_ROLES": off}) is False


def test_build_passes_all_roles_to_the_crawl(tmp_path, monkeypatch):
    import scripts.build_feed as bf
    import sourcing.service as service
    seen = {}

    def fake_refresh(w, **kw):
        seen.update(kw)
        return {"boards_crawled": 0}
    monkeypatch.setattr(service, "refresh_watchlist", fake_refresh)
    monkeypatch.setenv("CAREERSITE_LANE", "0")
    monkeypatch.setenv("FREEHIRE_SPONSOR_PASS_REQUESTS", "0")
    monkeypatch.setattr(bf, "freehire_sponsor_pass", lambda *a, **k: {})
    bf.build(tmp_path / "out", tmp_path / "data", delta_out=tmp_path / "d.db", log=lambda *_: None)
    assert seen["all_roles"] is True
    monkeypatch.setenv("FEED_ALL_ROLES", "0")
    bf.build(tmp_path / "out", tmp_path / "data2", delta_out=tmp_path / "d2.db", log=lambda *_: None)
    assert seen["all_roles"] is False


def test_career_site_lane_takes_the_switch_and_defaults_to_accessible_only():
    """The behaviour is pinned in tests/test_careersite.py (excluded roles stored with all_roles)."""
    import inspect
    from sourcing import careersite
    assert inspect.signature(careersite.crawl_site).parameters["all_roles"].default is False
    assert inspect.signature(careersite.crawl_sites).parameters["all_roles"].default is False


# ------------------------------------------------------------------ honest marking
def test_a_clearance_heavy_employer_is_never_a_sponsor_row():
    assert not quality.is_sponsor_relevant({"source": "workday", "company": "Lockheed Martin",
                                            "visa": ["H-1B"]})
    assert not quality.is_sponsor_relevant({"source": "freehire", "company": "Leidos", "visa": ["H-1B"],
                                            "sponsorship_stated": True})
    assert quality.is_sponsor_relevant({"source": "workday", "company": "General Mills"})


@pytest.fixture()
def general_list(tmp_path, monkeypatch):
    path = tmp_path / "watchlist_general.csv"
    growth.write_general_boards(path, [
        {"ats": "greenhouse", "board_id": "Calendly", "company": "Calendly", "jobs": 30, "us_jobs": 21,
         "verified": "2026-11-25", "source": "test"},
        {"ats": "workday", "board_id": "acme.wd5/External", "company": "Acme", "jobs": 9, "us_jobs": 9,
         "verified": "2026-11-25", "source": "test"}])
    monkeypatch.setattr(quality, "GENERAL_BOARDS_SEED", path)
    quality.general_boards.cache_clear()
    yield path
    quality.general_boards.cache_clear()


def test_a_general_board_row_needs_a_badge_or_an_offer(general_list):
    row = {"source": "greenhouse", "source_id": "greenhouse:calendly:77", "company": "Calendly"}
    assert quality.is_general_board_row(row)
    assert not quality.is_sponsor_relevant(row)                                  # no record: general
    assert quality.is_sponsor_relevant({**row, "visa": ["H-1B"]})                # the employer has one
    assert quality.is_sponsor_relevant({**row, "ad_stance": "offered"})          # the ad offers it
    assert not quality.is_sponsor_relevant({**row, "visa": ["H-1B"], "ad_stance": "not_offered"})
    wd = {"source": "workday", "source_id": "workday:acme.wd5/External:R-1:x", "company": "Acme"}
    assert quality.is_general_board_row(wd) and not quality.is_sponsor_relevant(wd)


def test_a_board_not_on_the_general_list_keeps_the_old_promise(general_list):
    row = {"source": "greenhouse", "source_id": "greenhouse:stripe:1", "company": "Stripe"}
    assert not quality.is_general_board_row(row) and quality.is_sponsor_relevant(row)
    assert not quality.is_general_board_row({"source": "greenhouse", "source_id": "greenhouse::1"})
    assert not quality.is_general_board_row({"source": "freehire"})


def test_a_missing_general_list_means_no_general_boards(tmp_path, monkeypatch):
    monkeypatch.setattr(quality, "GENERAL_BOARDS_SEED", tmp_path / "nope.csv")
    quality.general_boards.cache_clear()
    try:
        assert quality.general_boards() == frozenset()
    finally:
        quality.general_boards.cache_clear()


def test_every_committed_general_board_is_on_the_watchlist_seed():
    """A board listed as general but missing from the seed would never be crawled; a general board
    must be written to both files in the same change."""
    seed = {(r["ats"], r["board_id"].lower()) for r in growth.read_watchlist_seed()}
    rows = growth.read_general_boards()
    for r in rows:
        assert (r["ats"], r["board_id"].lower()) in seed, r
        assert int(r["jobs"]) >= 1 and r["verified"] and r["source"]
    keys = [(r["ats"], r["board_id"]) for r in rows]
    assert len(keys) == len(set(keys)) and rows == sorted(rows, key=lambda r: (r["company"], r["ats"], r["board_id"]))


# ------------------------------------------------------------------ the feed
def _feed(tmp_path, monkeypatch, rows):
    import backend.feed as feed
    jobs_db = tmp_path / "jobs.db"
    monkeypatch.setattr(feed, "_paths", lambda: (str(jobs_db), str(tmp_path / "s.db")))

    class _Tag:
        def tag_jobs(self, rs):
            for j in rs:
                j["us"] = True
                j["visa"] = ["H-1B"] if j.get("company") in ("Stripe", "Lockheed Martin") else []
            return rs
    monkeypatch.setattr(feed, "_sponsors", lambda _p: _Tag())
    w = Watchlist(str(jobs_db))
    w.upsert_jobs(rows)
    w.close()
    return feed


def _row(sid, source, company, title, jd="We are hiring.", posted="2026-11-20"):
    return {"source_id": sid, "source": source, "company": company, "title": title, "location": "Austin, TX",
            "url": "https://example.com/" + sid, "jd_text": jd, "remote": "", "posted_at": posted}


FEED_ROWS = [
    _row("greenhouse:stripe:1", "greenhouse", "Stripe", "Data Engineer"),
    _row("greenhouse:stripe:2", "greenhouse", "Stripe", "Account Manager", jd=NO),
    _row("workday:lm.wd1/x:3", "workday", "Lockheed Martin", "Systems Engineer"),
    _row("greenhouse:calendly:4", "greenhouse", "Calendly", "Product Designer"),
]


def test_a_sponsor_only_feed_is_exactly_as_before(tmp_path, monkeypatch, general_list):
    monkeypatch.delenv("FEED_GENERAL_MAX", raising=False)
    feed = _feed(tmp_path, monkeypatch, FEED_ROWS)
    assert {j["source_id"] for j in feed.feed_jobs()} == {"greenhouse:stripe:1"}
    assert feed.FUNNEL["clearance_employer"] == 1 and feed.FUNNEL["general_over_cap"] == 0


def test_closed_clearance_and_no_record_rows_are_published_as_general(tmp_path, monkeypatch, general_list):
    monkeypatch.setenv("FEED_GENERAL_MAX", "50")
    feed = _feed(tmp_path, monkeypatch, FEED_ROWS)
    got = {j["source_id"]: j["sponsor_relevant"] for j in feed.feed_jobs()}
    assert got == {"greenhouse:stripe:1": True, "greenhouse:stripe:2": False,
                   "workday:lm.wd1/x:3": False, "greenhouse:calendly:4": False}
    assert feed.FUNNEL["kept_sponsor"] == 1 and feed.FUNNEL["kept_general"] == 3
    stance = {j["source_id"]: j.get("ad_stance") for j in feed.feed_jobs()}
    assert stance["greenhouse:stripe:2"] == "not_offered"            # the chip's fact travels with the row


def test_a_role_the_old_crawl_hid_is_labelled_not_left_as_a_sponsor_row(tmp_path, monkeypatch, general_list):
    """The crawl's blunt no-sponsorship phrasings catch ads the careful ad reader misses. Those
    roles used to be dropped; stored now, they must be general rows with the sentence shown."""
    from sourcing.adstance import ad_stance
    jd = "Join us in Nashville. All applicants must be authorized to work in the US without sponsorship. Apply."
    assert ad_stance(jd)["stance"] == "unknown"                       # the careful reader misses it
    assert not quality.is_intl_student_accessible({"company": "Acme", "jd_text": jd})   # the crawl hid it
    st = quality.feed_ad_stance(jd)
    assert st == {"stance": "not_offered",
                  "sentence": "All applicants must be authorized to work in the US without sponsorship."}
    for text in ("", "We sponsor the local robotics league.", "Visa sponsorship is available for this role."):
        assert quality.feed_ad_stance(text) == ad_stance(text)        # nothing else changes
    assert quality.feed_ad_stance(CITIZEN)["stance"] == "restricted"
    monkeypatch.setenv("FEED_GENERAL_MAX", "50")
    feed = _feed(tmp_path, monkeypatch, [_row("greenhouse:stripe:9", "greenhouse", "Stripe", "Engineer", jd=jd)])
    row = feed.feed_jobs()[0]
    assert row["sponsor_relevant"] is False and row["ad_stance"] == "not_offered"
    from sourcing.feedfile import write_feed
    import json
    write_feed(tmp_path / "o", [row], {row["source_id"]: jd})
    out = json.loads(gzip.open(tmp_path / "o" / "jobs.json.gz").read())["jobs"][0]
    assert out["sponsor_relevant"] is False and out["ad_stance"] == "not_offered"
    assert out["ad_sentence"].endswith("without sponsorship.")


def test_the_general_cap_is_never_silent(tmp_path, monkeypatch, general_list):
    monkeypatch.setenv("FEED_GENERAL_MAX", "1")
    feed = _feed(tmp_path, monkeypatch, FEED_ROWS)
    rows = feed.feed_jobs()
    assert sum(1 for j in rows if not j["sponsor_relevant"]) == 1
    assert feed.FUNNEL["kept_general"] == 1 and feed.FUNNEL["general_over_cap"] == 2


def test_the_written_feed_marks_the_same_rows(tmp_path, monkeypatch, general_list):
    """write_feed re-derives sponsor_relevant; it must agree with feed_jobs for the new rows."""
    import json
    from sourcing.feedfile import write_feed
    monkeypatch.setenv("FEED_GENERAL_MAX", "50")
    feed = _feed(tmp_path, monkeypatch, FEED_ROWS)
    rows = feed.feed_jobs()
    man = write_feed(tmp_path / "out", rows, {r["source_id"]: "We are hiring." for r in rows})
    assert man["count"] == 4 and man["sponsor_count"] == 1
    data = json.loads(gzip.open(tmp_path / "out" / "jobs.json.gz").read())
    flags = {j["source_id"]: j["sponsor_relevant"] for j in data["jobs"]}
    assert flags == {j["source_id"]: j["sponsor_relevant"] for j in rows}


# ------------------------------------------------------------------ discovery
def _seed_csv(tmp_path):
    p = tmp_path / "sponsors.csv"
    rows = [("acme", "Acme Inc", 12, 2025, 0, 0), ("permco", "PermCo LLC", 0, 0, 3, 0),
            ("calendly", "Calendly LLC", 0, 0, 0, 1), ("zesty ai", "Zesty Ai", 0, 2024, 0, 1),
            ("old filer", "Old Filer Inc", 0, 2020, 0, 1)]
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["norm_name", "display_name", "h1b_approvals", "h1b_last_fy", "perm_certs", "e_verify"])
        w.writerows(rows)
    return p


def test_the_sponsor_lane_and_the_general_lane_split_the_seed(tmp_path):
    p = _seed_csv(tmp_path)
    assert {r["norm_name"] for r in growth.load_sponsor_seed(p)} == {"acme", "permco"}
    assert {r["norm_name"] for r in growth.load_sponsor_seed(p, no_record=True)} == {
        "calendly", "zesty ai", "old filer"}


def test_the_general_lane_asks_recent_filers_first(tmp_path):
    rows = growth.load_sponsor_seed(_seed_csv(tmp_path), no_record=True)
    assert [r["norm_name"] for r in growth.rank_sponsors(rows, no_record=True)] == [
        "zesty ai", "old filer", "calendly"]
    assert growth.rank_sponsors(rows) == []                      # the sponsor lane never ranks them
    assert growth.rank_sponsors(growth.load_sponsor_seed(_seed_csv(tmp_path)), no_record=True) == []


def test_a_clearance_heavy_employer_is_probed_now():
    assert growth.should_skip("Lockheed Martin Corporation") is None
    assert growth.should_skip("Inc") is not None                 # still nothing to verify against


def test_general_boards_file_round_trips_and_merges(tmp_path):
    p = tmp_path / "g.csv"
    growth.write_general_boards(p, [{"ats": "lever", "board_id": "zed", "company": "Zed", "jobs": 3}])
    out = growth.write_general_boards(p, [{"ats": "greenhouse", "board_id": "abc", "company": "Abc", "jobs": 5},
                                          {"ats": "lever", "board_id": "zed", "company": "Zed", "jobs": 8},
                                          {"ats": "workday", "board_id": "not a board", "company": "Bad"}])
    assert [(r["company"], r["jobs"]) for r in out] == [("Abc", "5"), ("Zed", "8")]
    assert growth.read_general_boards(p) == out
    assert quality.general_boards(str(p)) == {("greenhouse", "abc"), ("lever", "zed")}


# ------------------------------------------------------------------ scripts/add_general_boards.py
def test_parse_board_url():
    from scripts.add_general_boards import parse_board_url as p
    assert p("https://boards.greenhouse.io/calendly") == ("greenhouse", "calendly")
    assert p("https://job-boards.greenhouse.io/zapier/jobs/123") == ("greenhouse", "zapier")
    assert p("https://jobs.lever.co/acme/abc-123") == ("lever", "acme")
    assert p("https://jobs.ashbyhq.com/openrouter") == ("ashby", "openrouter")
    assert p("https://apply.workable.com/acme-co/") == ("workable", "acme-co")
    assert p("https://acme.recruitee.com/o/role") == ("recruitee", "acme")
    assert p("https://acme.wd5.myworkdayjobs.com/en-US/External/job/x") == ("workday", "acme.wd5/External")
    for bad in ("https://acme.com/careers", "https://boards.greenhouse.io/embed/job_app?token=1",
                "https://www.linkedin.com/jobs/view/1", "", None):
        assert p(bad) is None


def _files(tmp_path):
    seed, ev, gen = tmp_path / "watchlist_seed.csv", tmp_path / "watchlist_evidence.csv", tmp_path / "g.csv"
    growth.write_watchlist_seed(seed, [{"company": "Acme Inc", "ats": "greenhouse", "board_id": "acme"}])
    return seed, ev, gen


def test_add_general_boards_writes_seed_evidence_and_the_general_list(tmp_path):
    from scripts.add_general_boards import run
    seed, ev, gen = _files(tmp_path)
    found = {"Calendly": {"ats": "greenhouse", "board_id": "calendly"},
             "PermCo LLC": {"ats": "lever", "board_id": "permco"},
             "Tiny Shop": {"ats": "ashby", "board_id": "tinyshop"}}
    counts = {"calendly": (30, 21), "permco": (8, 8), "tinyshop": (4, 0), "zed": (5, 5)}
    s = run(["Calendly", "Acme Inc", "PermCo LLC", "Tiny Shop", "Nobody Here"],
            [{"ats": "lever", "board_id": "zed", "company": "Zed Labs"},
             {"ats": "greenhouse", "board_id": "acme", "company": "Acme Inc"},
             {"ats": "smartrecruiters", "board_id": "off", "company": "Off Co"}],
            seed=seed, evidence=ev, general=gen, sponsors=_seed_csv(tmp_path), fetch=None,
            discover=lambda name, atss=(): found.get(name),
            count=lambda ats, board, company: counts[board], source="a-list",
            log=lambda *_: None, today="2026-11-25")
    assert s["already_watched"] == 2 and s["no_board"] == 1 and s["too_few_us"] == 1
    assert s["boards"] == 2                                       # the disabled ATS is never asked
    added = {a["board_id"]: a["general"] for a in s["added"]}
    assert added == {"zed": True, "calendly": True, "permco": False}   # PermCo has a PERM record
    assert {(r["ats"], r["board_id"]) for r in growth.read_watchlist_seed(seed)} == {
        ("greenhouse", "acme"), ("greenhouse", "calendly"), ("lever", "permco"), ("lever", "zed")}
    assert {r["board_id"] for r in growth.read_general_boards(gen)} == {"calendly", "zed"}
    evidence = {r["board_id"]: r for r in growth.read_evidence(ev)}
    assert evidence["calendly"]["source"] == "a-list" and evidence["calendly"]["h1b_approvals"] == "0"
    assert evidence["permco"]["perm_certs"] == "3"
    cal = next(r for r in growth.read_general_boards(gen) if r["board_id"] == "calendly")
    assert (cal["jobs"], cal["us_jobs"], cal["verified"], cal["source"]) == ("30", "21", "2026-11-25", "a-list")


def test_add_general_boards_dry_run_and_time_cap_write_nothing(tmp_path):
    from scripts.add_general_boards import run
    seed, ev, gen = _files(tmp_path)
    before = seed.read_text()
    kw = dict(seed=seed, evidence=ev, general=gen, sponsors=_seed_csv(tmp_path), fetch=None,
              discover=lambda name, atss=(): {"ats": "lever", "board_id": name.lower()},
              count=lambda *a: (5, 5), log=lambda *_: None)
    s = run(["Zed"], dry_run=True, **kw)
    assert len(s["added"]) == 1 and seed.read_text() == before and not gen.exists() and not ev.exists()
    ticks = iter(range(0, 1000, 10))
    s = run(["Zed", "Yak"], max_seconds=5, clock=lambda: next(ticks), **kw)
    assert s["stopped"] == "time cap" and s["added"] == [] and seed.read_text() == before


def test_a_board_found_before_the_time_cap_is_still_read_and_added(tmp_path):
    from scripts.add_general_boards import run
    seed, ev, gen = _files(tmp_path)
    now = [0]

    def discover(name, atss=()):
        now[0] = 100                                             # the probe itself ran past the cap
        return {"ats": "lever", "board_id": name.lower()}
    s = run(["Zed"], [{"ats": "lever", "board_id": "late", "company": "Late"}], seed=seed, evidence=ev,
            general=gen, sponsors=_seed_csv(tmp_path), fetch=None, discover=discover,
            count=lambda *a: (5, 5), log=lambda *_: None, max_seconds=50, clock=lambda: now[0])
    assert [a["board_id"] for a in s["added"]] == ["zed"] and s["stopped"] == "time cap"


def test_grow_watchlist_has_a_general_lane_flag():
    src = (ROOT / "scripts" / "grow_watchlist.py").read_text()
    assert '"--no-record"' in src and "write_general_boards" in src
    assert "load_sponsor_seed(args.sponsors, no_record=args.no_record)" in src


# ------------------------------------------------------------------ the workflow and the lanes
def test_workflow_keeps_the_switch_and_the_caps():
    wf = (ROOT / ".github" / "workflows" / "feed.yml").read_text()
    assert "FEED_ALL_ROLES: ${{ vars.FEED_ALL_ROLES || 1 }}" in wf
    assert "FEED_GENERAL_MAX: ${{ vars.FEED_GENERAL_MAX || 50000 }}" in wf     # raised by variable, in stages


def test_smartrecruiters_stays_off_and_no_new_source_was_switched_on():
    from scripts.build_feed import PUBLIC_OK_AGGREGATORS, aggregators_for_feed
    from sourcing.ats import ats_disabled
    assert ats_disabled("smartrecruiters")
    assert PUBLIC_OK_AGGREGATORS == ("remotive", "remoteok", "freehire")
    logs = []
    assert "adzuna" not in aggregators_for_feed({"FEED_INCLUDE_AGGREGATORS": "adzuna,jsearch"}, log=logs.append)
    assert any("refusing to include adzuna" in m for m in logs)
