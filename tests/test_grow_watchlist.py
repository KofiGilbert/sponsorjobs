"""Watchlist growth from the ranked sponsor seed (sourcing/growth.py, scripts/grow_watchlist.py).

The committed watchlist is what day one shows, and the weekly in-app discovery is what keeps
it growing, so both run through ONE module. These tests pin its promises offline (a fake of
the ATS endpoints sits under the real PoliteFetch; nothing touches the network): the ranking
prefers recent, high-volume sponsors and skips what is already watched; every outcome is
checkpointed so a stopped crawl resumes without re-probing; politeness (per-host spacing,
429 back-off and cooldown, a throttled ATS is owed to the next run rather than recorded as a
miss); a board is added only with a live job and never twice; the seed file comes out sorted
and deduped; and the in-app pass is bounded per run yet advances through the list.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import re
import urllib.error
from pathlib import Path

import pytest

from sourcing import growth
from sourcing.watchlist import Watchlist

ROOT = Path(__file__).resolve().parents[1]


# -- fakes ----------------------------------------------------------------- #

def _http_429(retry_after=None):
    hdrs = {"Retry-After": str(retry_after)} if retry_after else {}
    return urllib.error.HTTPError("https://x", 429, "Too Many Requests", hdrs, None)


def _http_404(url):
    return urllib.error.HTTPError(url, 404, "Not Found", {}, None)


class FakeClock:
    def __init__(self, t=0.0, step=0.0):
        self.t, self.step = t, step

    def __call__(self):
        self.t += self.step
        return self.t


class FakeATS:
    """A fake of the public ATS endpoints. Hosts in `throttle` answer 429 (optionally only
    for their first `throttle_n` requests). Wrap it in the real PoliteFetch (`polite`)."""

    def __init__(self, gh_jobs=None, gh_names=None, ashby=None, lever=None,
                 throttle=(), throttle_n=None):
        self.gh_jobs, self.gh_names = gh_jobs or {}, gh_names or {}
        self.ashby, self.lever = ashby or {}, lever or {}
        self.throttle, self.throttle_n = set(throttle), throttle_n
        self.urls: list[str] = []

    def __call__(self, url, timeout=20, **kw):
        self.urls.append(url)
        host = url.split("/")[2]
        # A lane name matches its sub-hosts too: every {tenant}.wd{n}.myworkdayjobs.com is the
        # one Workday lane (growth.host_lane).
        lane = growth.host_lane(host)
        if lane in self.throttle and (self.throttle_n is None or
                                      sum(1 for u in self.urls if lane in u) <= self.throttle_n):
            raise _http_429()
        m = re.search(r"boards-api\.greenhouse\.io/v1/boards/([^/?]+)(/jobs)?", url)
        if m:
            slug, is_jobs = m.group(1), m.group(2)
            if is_jobs:
                if slug not in self.gh_jobs:
                    raise _http_404(url)
                return {"jobs": self.gh_jobs[slug]}
            return {"name": self.gh_names.get(slug, slug)}
        m = re.search(r"api\.ashbyhq\.com/posting-api/job-board/([^/?]+)", url)
        if m and m.group(1) in self.ashby:
            return {"jobs": self.ashby[m.group(1)]}
        m = re.search(r"api\.lever\.co/v0/postings/([^/?]+)", url)
        if m and m.group(1) in self.lever:
            return self.lever[m.group(1)]
        raise _http_404(url)


def polite(ats, **kw):
    """The real PoliteFetch over the fake endpoints, with no real sleeping or waiting."""
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("clock", FakeClock(step=0.001))
    return growth.PoliteFetch(fetch=ats, **kw)


_JOB = [{"id": 1, "title": "Engineer", "location": {"name": "Remote - US"}}]
_ASHBY_JOB = [{"id": "a1", "title": "Engineer", "location": "New York"}]
ALL_HOSTS = tuple(growth.ATS_HOSTS)


def _row(name, approvals=100, last_fy=2023):
    return {"norm_name": growth.normalize_employer(name), "display_name": name,
            "h1b_approvals": approvals, "h1b_last_fy": last_fy}


# -- ranking --------------------------------------------------------------- #

def test_rank_prefers_recent_filers_and_skips_watched_names():
    rows = [_row("Old Giant Inc", approvals=300, last_fy=2019),
            _row("Fresh Hirer LLC", approvals=200, last_fy=2023),
            _row("Datadog Inc", approvals=150, last_fy=2023),
            _row("Zero Co", approvals=0)]
    watched = growth.watched_norms([{"company": "Datadog, Inc."}])
    ranked = growth.rank_sponsors(rows, skip_norms=watched)
    assert [r["display_name"] for r in ranked] == ["Fresh Hirer LLC", "Old Giant Inc"]
    assert ranked[0]["score"] == 200 and ranked[1]["score"] == pytest.approx(300 * 0.6 ** 4)
    assert growth.rank_sponsors(rows, limit=1)[0]["display_name"] == "Fresh Hirer LLC"


def test_load_sponsor_seed_keeps_only_employers_with_approvals(tmp_path):
    p = tmp_path / "s.csv.gz"
    with gzip.open(p, "wt", newline="") as f:
        f.write("norm_name,display_name,h1b_approvals,h1b_last_fy,h1b_first_fy,naics,state,"
                "cap_exempt,e_verify,perm_certs\n"
                "datadog,Datadog Inc,12,2023,2020,51,NY,0,0,0\n"
                "perm only,Perm Only LLC,0,0,0,54,CA,0,1,3\n")
    rows = growth.load_sponsor_seed(p)
    assert [r["display_name"] for r in rows] == ["Datadog Inc"]
    assert rows[0]["h1b_approvals"] == 12 and rows[0]["h1b_last_fy"] == 2023


def test_should_skip_uses_only_the_existing_filters():
    assert growth.should_skip("Lockheed Martin Corporation")      # curated clearance list
    assert growth.should_skip("First National Group")             # no brand word to verify
    assert growth.should_skip("Datadog Inc") is None
    assert growth.should_skip("Tata Consultancy Svcs Ltd") is None   # no invented body-shop filter


# -- the crawl: checkpoint, honesty, resumability ---------------------------- #

def test_grow_checkpoints_every_outcome_with_reasons(tmp_path):
    fetch = polite(FakeATS(gh_jobs={"datadog": _JOB, "charles": _JOB},
                           gh_names={"datadog": "Datadog", "charles": "charles"}))
    cp = growth.Checkpoint(tmp_path / "cp.jsonl")
    ranked = [_row("Datadog Inc"), _row("Charles Schwab & Company Inc"),
              _row("First National Group"), _row("Nobody Here Ltd")]
    s = growth.grow(ranked, cp, fetch, workers=2)
    assert s["hits"] == 1 and s["probed"] == 3 and s["skipped"] == 1 and s["stopped"] == "exhausted"
    assert [(b["ats"], b["board_id"], b["jobs"]) for b in s["new_boards"]] == [("greenhouse", "datadog", 1)]
    assert s["per_ats"] == {"greenhouse": 1}
    # The squatted "charles" board is reported, with the identity reason.
    assert len(s["rejected"]) == 1 and "charles" in s["rejected"][0]["board"]
    assert "named 'charles'" in s["rejected"][0]["board"]
    # Every employer is remembered, and the file is replayable.
    recs = growth.Checkpoint(tmp_path / "cp.jsonl").records
    assert {r["status"] for r in recs.values()} == {"hit", "miss", "skipped"}
    assert recs["datadog"]["board_id"] == "datadog" and recs["datadog"]["jobs"] == 1
    schwab = recs[growth.normalize_employer("Charles Schwab & Company Inc")]
    assert schwab["status"] == "miss" and "charles" in schwab["reason"]
    assert recs["first national group"]["status"] == "skipped"
    assert recs["nobody here"]["reason"] == "no live board at any slug"
    assert s["requests"] == fetch.requests > 0 and s["rate_limited"] == 0


def test_grow_counts_an_already_watched_board_as_a_duplicate_not_new(tmp_path):
    fetch = polite(FakeATS(gh_jobs={"datadog": _JOB}, gh_names={"datadog": "Datadog"}))
    cp = growth.Checkpoint(tmp_path / "cp.jsonl")
    # Another filing entity of a company whose board is already watched.
    s = growth.grow([_row("Datadog Inc")], cp, fetch, known_boards={("greenhouse", "datadog")})
    assert s["hits"] == 1 and s["duplicates"] == 1 and s["new_boards"] == []


def test_grow_leaves_a_fully_throttled_employer_for_the_next_run(tmp_path):
    fetch = polite(FakeATS(throttle=ALL_HOSTS))
    cp = growth.Checkpoint(tmp_path / "cp.jsonl")
    s = growth.grow([_row("Datadog Inc")], cp, fetch, workers=1)
    assert s["throttled"] == 1 and s["probed"] == 0 and s["hits"] == 0
    assert cp.records == {} and not (tmp_path / "cp.jsonl").exists()   # nothing written -> retried
    assert s["rate_limited"] > 0 and s["cooldowns"] >= 1


def test_a_throttled_ats_is_owed_to_the_next_run_not_recorded_as_a_miss(tmp_path):
    # Workable keeps answering 429; the other hosts are healthy and say "no board".
    ats = FakeATS(throttle={"apply.workable.com"})
    fetch = polite(ats)
    cp = growth.Checkpoint(tmp_path / "cp.jsonl")
    rows = [_row("Datadog Inc", 300), _row("Nobody Here Ltd", 200)]
    s = growth.grow(rows, cp, fetch, workers=1)
    assert s["probed"] == 2 and s["partial"] == 2 and s["throttled"] == 0
    assert all(r["status"] == "miss" and r["unprobed_ats"] == ["workable"] for r in cp.records.values())
    assert fetch.rate_limited > 0 and (fetch.cooling() or fetch.deferred)   # backed off, not hammered
    # The next run's candidates are the same two employers, owing ONLY workable.
    nxt = growth.candidates(rows, cp, watched=set(), limit=None)
    assert [(r["display_name"], r["atss"]) for r in nxt] == [("Datadog Inc", ["workable"]),
                                                             ("Nobody Here Ltd", ["workable"])]
    # Workable recovers; the rerun probes just that host and the records become final.
    ats.throttle = set()
    before = len(ats.urls)
    s2 = growth.grow(nxt, cp, polite(ats), workers=1)
    assert s2["probed"] == 2 and s2["partial"] == 0
    assert all("workable" in u for u in ats.urls[before:])
    assert cp.pending() == {} and cp.done_norms() == {"datadog", "nobody here"}
    assert growth.candidates(rows, cp, watched=set(), limit=None) == []


def test_grow_stops_submitting_at_the_time_cap(tmp_path):
    fetch = polite(FakeATS())
    cp = growth.Checkpoint(tmp_path / "cp.jsonl")
    ranked = [_row(f"Company{i} Widgets Inc") for i in range(20)]
    # Each clock read advances 10s; a 60s budget runs out long before the 20th employer.
    s = growth.grow(ranked, cp, fetch, workers=1, max_seconds=60, clock=FakeClock(step=10))
    assert s["stopped"] == "time cap"
    assert 0 < len(cp.records) < 20
    assert growth.grow(ranked, cp, fetch, workers=1)["stopped"] == "exhausted"   # no cap -> all


def test_checkpoint_ignores_a_torn_final_line_and_appends_cleanly(tmp_path):
    p = tmp_path / "cp.jsonl"
    p.write_text(json.dumps({"norm": "datadog", "status": "hit"}) + "\n{\"norm\": \"half")
    cp = growth.Checkpoint(p)
    assert set(cp.records) == {"datadog"} and "datadog" in cp
    cp.record("acme", status="miss")
    assert set(growth.Checkpoint(p).records) == {"datadog", "acme"}


# -- politeness ------------------------------------------------------------- #

def test_polite_fetch_spaces_requests_per_host_with_a_slower_lane_for_strict_hosts():
    sleeps: list[float] = []
    pf = growth.PoliteFetch(fetch=lambda url, timeout: {}, host_delay=0.25,
                            sleep=sleeps.append, clock=lambda: 100.0)
    pf("https://boards-api.greenhouse.io/v1/boards/a/jobs")
    pf("https://api.lever.co/v0/postings/b")           # a different host: no wait
    assert sleeps == []
    pf("https://boards-api.greenhouse.io/v1/boards/c/jobs")   # same host, too soon: wait 0.25s
    assert sleeps == [pytest.approx(0.25)]
    pf("https://apply.workable.com/api/v1/widget/accounts/a")
    pf("https://apply.workable.com/api/v1/widget/accounts/b")
    assert sleeps[-1] == pytest.approx(growth.HOST_DELAYS["apply.workable.com"])
    assert pf.requests == 5 and pf.rate_limited == 0


def test_polite_fetch_backs_off_on_429_honouring_retry_after_then_retries():
    sleeps: list[float] = []
    attempts = {"n": 0}

    def flaky(url, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_429(retry_after=7)
        return {"ok": True}
    pf = growth.PoliteFetch(fetch=flaky, sleep=sleeps.append, clock=lambda: 0.0)
    pf.begin()
    host = "boards-api.greenhouse.io"
    assert pf(f"https://{host}/v1/boards/a/jobs") == {"ok": True}
    assert pf.rate_limited == 1 and attempts["n"] == 2
    assert sleeps and sleeps[0] == pytest.approx(7.0)             # the host paused Retry-After
    assert pf.delay_for(host) == pytest.approx(growth.HOST_DELAY * 2)   # and slowed down
    assert not pf.throttled and pf.cooling() == []                 # recovered: nothing owed


def test_polite_fetch_cools_a_host_down_after_bounded_429_retries():
    def always_429(url, timeout):
        raise _http_429()
    pf = growth.PoliteFetch(fetch=always_429, max_retries=2, cooldown=600,
                            sleep=lambda s: None, clock=lambda: 0.0)
    pf.begin()
    with pytest.raises(urllib.error.HTTPError):
        pf("https://api.lever.co/v0/postings/x")
    assert pf.rate_limited == 3 and pf.cooldowns == 1              # 1 try + 2 retries
    assert pf.blocked_ats == {"lever"} and pf.throttled
    # While cooling, the host is not asked at all; other hosts are unaffected.
    n = pf.requests
    with pytest.raises(growth.HostCoolingDown):
        pf("https://api.lever.co/v0/postings/y")
    assert pf.requests == n and pf.cooling() == ["api.lever.co"]
    pf.begin()
    assert not pf.throttled                                        # reset per employer


def test_polite_fetch_relaxes_the_spacing_again_after_clean_responses():
    host = "boards-api.greenhouse.io"
    pf = growth.PoliteFetch(fetch=lambda url, timeout: {}, sleep=lambda s: None,
                            clock=FakeClock(step=2.0))
    pf._on_429(host, _http_429(), 0)
    pf._on_429(host, _http_429(), 1)
    assert pf.delay_for(host) == pytest.approx(growth.HOST_DELAY * 4)
    for _ in range(growth.RELAX_AFTER):
        pf(f"https://{host}/v1/boards/a/jobs")
    assert pf.delay_for(host) == pytest.approx(growth.HOST_DELAY * 2)
    assert pf.delay_for(host) >= growth.HOST_DELAY                  # never below the base


def _http_5xx(code=503, retry_after=None):
    hdrs = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError("https://x", code, "Service Unavailable", hdrs, None)


def test_a_5xx_backs_the_lane_off_and_never_counts_as_a_clean_answer():
    """A host answering 503 under load used to be scored as a clean response: the lane never
    slowed, and after RELAX_AFTER of them its spacing HALVED -- the crawl sped up against a
    struggling host (while ats.fetch_with_backoff retried the 503s on top). A 5xx now pauses the
    lane and doubles its spacing like a 429, and the error still propagates unchanged."""
    host = "boards-api.greenhouse.io"
    clock = FakeClock(t=1000.0)
    sleeps: list[float] = []
    answers = {"n": 0}

    def struggling(url, timeout):
        answers["n"] += 1
        raise _http_5xx(503, retry_after=9)
    pf = growth.PoliteFetch(fetch=struggling, sleep=sleeps.append, clock=clock)
    pf.begin()
    base = pf.delay_for(host)
    with pytest.raises(urllib.error.HTTPError) as ei:
        pf(f"https://{host}/v1/boards/a/jobs")
    assert ei.value.code == 503 and answers["n"] == 1          # re-raised at once, no in-loop retry
    assert pf.server_errors == 1 and pf.rate_limited == 0
    assert pf.delay_for(host) == pytest.approx(base * 2)        # spacing doubled
    assert pf._clean.get(host, 0) == 0
    assert pf._next_ok[host] >= clock.t + 9.0 - 1e-9            # the lane is paused Retry-After
    # The next request on that lane waits the pause out instead of hitting the host again.
    with pytest.raises(urllib.error.HTTPError):
        pf(f"https://{host}/v1/boards/b/jobs")
    assert sleeps and sleeps[0] == pytest.approx(9.0)
    assert pf.delay_for(host) == pytest.approx(base * 4)
    assert not pf.throttled and pf.cooling() == []             # not a cooldown: just slower


def test_a_run_of_5xx_never_relaxes_a_backed_off_lane():
    host = "boards-api.greenhouse.io"
    pf = growth.PoliteFetch(fetch=lambda url, timeout: (_ for _ in ()).throw(_http_5xx(502)),
                            sleep=lambda s: None, clock=FakeClock(step=200.0))
    pf._on_429(host, _http_429(), 0)
    slowed = pf.delay_for(host)
    assert slowed == pytest.approx(growth.HOST_DELAY * 2)
    for _ in range(growth.RELAX_AFTER + 5):
        with pytest.raises(urllib.error.HTTPError):
            pf(f"https://{host}/v1/boards/a/jobs")
    assert pf.delay_for(host) >= slowed                        # never halved by "clean" 5xx counts
    assert pf.delay_for(host) == growth.MAX_HOST_DELAY         # grew to the cap and stayed there
    assert pf.server_errors == growth.RELAX_AFTER + 5


def test_a_404_is_neutral_neither_clean_nor_a_back_off():
    host = "api.lever.co"
    pf = growth.PoliteFetch(fetch=lambda url, timeout: (_ for _ in ()).throw(_http_404(url)),
                            sleep=lambda s: None, clock=FakeClock(step=200.0))
    pf._on_429(host, _http_429(), 0)
    slowed = pf.delay_for(host)
    for _ in range(growth.RELAX_AFTER + 5):
        with pytest.raises(urllib.error.HTTPError):
            pf(f"https://{host}/v0/postings/x")
    assert pf.delay_for(host) == pytest.approx(slowed)          # a 404 streak does not relax the lane
    assert pf.server_errors == 0 and pf.rate_limited == 1       # ...and is not a back-off either
    assert pf._clean.get(host, 0) == 0


def test_polite_fetch_owes_a_host_instead_of_queueing_too_long():
    # Workable's lane is slow; eight threads piling onto it would otherwise wait minutes.
    clock = FakeClock(t=0.0)                       # time never advances
    pf = growth.PoliteFetch(fetch=lambda url, timeout: {}, max_wait=10.0,
                            host_delays={"apply.workable.com": 4.0},
                            sleep=lambda s: None, clock=clock)
    pf.begin()
    url = "https://apply.workable.com/api/v1/widget/accounts/a"
    for _ in range(3):                             # slots at 0, 4, 8s -> next would be 12s away
        pf(url)
    with pytest.raises(growth.HostCoolingDown):
        pf(url)
    assert pf.deferred == 1 and pf.blocked_ats == {"workable"} and pf.requests == 3
    pf("https://api.lever.co/v0/postings/x")       # other hosts unaffected
    assert pf.requests == 4


class SleepClock(FakeClock):
    """A clock that only moves when something sleeps on it -- so a test sees exactly the
    waiting the pacer asked for, and nothing else advances time."""

    def __init__(self):
        super().__init__(t=0.0, step=0.0)
        self._lock = __import__("threading").Lock()

    def sleep(self, seconds):
        with self._lock:
            self.t += seconds


def _single_brand_rows(n=10):
    # One distinctive brand word each -> exactly one slug guess -> one Workable request each.
    names = ["Acme", "Zorblax", "Quendor", "Vantexo", "Blipware", "Krandor", "Mobius", "Fennwick",
             "Oxbridge", "Tarnhelm"]
    return [_row(f"{nm} Inc") for nm in names[:n]]


def test_a_single_slow_lane_run_waits_its_turn_instead_of_deferring_every_employer(tmp_path):
    # Regression (2026-10-01): `--ats workable` with two workers deferred all 3,613 employers in
    # seconds ("every ATS host asked was throttled; cooling: []") without making a request,
    # because a slot further away than MAX_WAIT was "owed to the next run" even though the run
    # had no other lane to turn to. A slow lane must still serve requests at its spacing.
    clock = SleepClock()
    ats = FakeATS()
    fetch = growth.PoliteFetch(fetch=ats, sleep=clock.sleep, clock=clock)
    cp = growth.Checkpoint(tmp_path / "cp.jsonl")
    s = growth.grow(_single_brand_rows(10), cp, fetch, atss=("workable",), workers=2, clock=clock,
                    sleep=clock.sleep)
    assert fetch.requests == 10 and fetch.deferred == 0 and len(ats.urls) == 10
    assert all("apply.workable.com" in u for u in ats.urls)
    assert s["probed"] == 10 and s["throttled"] == 0 and s["requeued"] == 0
    assert s["stopped"] == "exhausted" and len(cp.records) == 10
    assert clock.t >= 9 * growth.HOST_DELAYS["apply.workable.com"]     # spaced, never bunched


def test_a_429_pause_longer_than_max_wait_pauses_a_single_lane_run_rather_than_draining_it(tmp_path):
    # The real run's FIRST answer was a 429. Its pause (Retry-After, up to MAX_BACKOFF) is
    # longer than MAX_WAIT, which under the old rule deferred everyone instantly.
    clock = SleepClock()
    ats = FakeATS()
    calls = {"n": 0}

    def first_429(url, timeout=20, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_429(retry_after=30)
        return ats(url, timeout, **kw)
    fetch = growth.PoliteFetch(fetch=first_429, sleep=clock.sleep, clock=clock)
    cp = growth.Checkpoint(tmp_path / "cp.jsonl")
    s = growth.grow(_single_brand_rows(10), cp, fetch, atss=("workable",), workers=2, clock=clock,
                    sleep=clock.sleep)
    assert fetch.rate_limited == 1 and fetch.deferred == 0 and fetch.requests == 11   # one retry
    assert s["probed"] == 10 and s["throttled"] == 0 and len(cp.records) == 10
    assert clock.t >= 30.0                                               # the pause was honoured


def test_a_retry_after_of_hours_cools_the_lane_and_stops_a_run_it_would_outlast(tmp_path):
    # Workable's edge answers 429 + Retry-After ~19h once an IP has used its daily budget. That
    # is not retried, and a run whose every lane is away for longer than its budget stops and
    # says so, leaving the whole list for the next run -- nothing is recorded as a miss.
    clock = SleepClock()
    waits: list = []

    def budget_gone(url, timeout=20, **kw):
        raise _http_429(retry_after=67794)
    fetch = growth.PoliteFetch(fetch=budget_gone, sleep=clock.sleep, clock=clock)
    cp = growth.Checkpoint(tmp_path / "cp.jsonl")
    s = growth.grow(_single_brand_rows(10), cp, fetch, atss=("workable",), workers=2, clock=clock,
                    max_seconds=6000, sleep=clock.sleep, on_wait=lambda g, c: waits.append((g, c)))
    assert fetch.requests == 1 and fetch.rate_limited == 1 and fetch.cooldowns == 1
    assert fetch.cooling() == ["apply.workable.com"]
    assert s["stopped"] == "hosts throttled" and s["probed"] == 0 and s["hits"] == 0
    assert s["throttled"] >= 1 and cp.records == {} and waits == []    # too long to wait for
    assert fetch.next_free(("workable",)) == pytest.approx(67794.0, abs=1.0)
    # Without a budget the cap is MAX_LANE_PAUSE, so the same host stops the run too.
    s2 = growth.grow(_single_brand_rows(2), cp, fetch, atss=("workable",), workers=1, clock=clock,
                     sleep=clock.sleep)
    assert s2["stopped"] == "hosts throttled" and fetch.requests == 1


def test_a_short_cooldown_is_waited_out_and_the_employer_asked_again(tmp_path):
    # Three 429s in a row put the lane in COOLDOWN; the run waits it out (the budget allows),
    # asks the same employer again, and finishes the list -- rather than dropping everyone.
    clock = SleepClock()
    ats = FakeATS()
    calls = {"n": 0}

    def three_429s(url, timeout=20, **kw):
        calls["n"] += 1
        if calls["n"] <= 4:                                             # 1 try + 3 retries
            raise _http_429()
        return ats(url, timeout, **kw)
    fetch = growth.PoliteFetch(fetch=three_429s, sleep=clock.sleep, clock=clock)
    cp = growth.Checkpoint(tmp_path / "cp.jsonl")
    waits: list = []
    s = growth.grow(_single_brand_rows(3), cp, fetch, atss=("workable",), workers=1, clock=clock,
                    max_seconds=3600, sleep=clock.sleep, on_wait=lambda g, c: waits.append(g))
    assert fetch.cooldowns == 1 and s["requeued"] == 2 and s["throttled"] == 0   # both in flight
    assert s["probed"] == 3 and len(cp.records) == 3 and s["stopped"] == "exhausted"
    assert waits and waits[0] == pytest.approx(growth.COOLDOWN, abs=1.0) and s["waited"] >= 500


def test_polite_fetch_defers_a_slow_lane_only_when_another_lane_of_the_probe_is_nearer():
    clock = FakeClock(t=0.0)
    pf = growth.PoliteFetch(fetch=lambda url, timeout: {}, max_wait=10.0,
                            host_delays={"apply.workable.com": 4.0}, sleep=lambda s: None, clock=clock)
    url = "https://apply.workable.com/api/v1/widget/accounts/a"
    pf.begin(("workable",))                        # a Workable-only probe: nowhere else to go
    for _ in range(5):                             # 0, 4, 8, 12, 16s: the 4th/5th exceed max_wait
        pf(url)
    assert pf.requests == 5 and pf.deferred == 0 and not pf.throttled
    pf.begin(("workable", "lever"))                # Lever is free: Workable is owed, Lever asked
    with pytest.raises(growth.HostCoolingDown):
        pf(url)
    assert pf.deferred == 1 and pf.blocked_ats == {"workable"}
    pf("https://api.lever.co/v0/postings/x")
    assert pf.requests == 6
    assert growth.lanes_for(("workable", "workday")) == {"apply.workable.com", "myworkdayjobs.com"}


def test_polite_fetch_passes_other_http_errors_straight_through():
    def nf(url, timeout):
        raise _http_404(url)
    pf = growth.PoliteFetch(fetch=nf, sleep=lambda s: None, clock=lambda: 0.0)
    with pytest.raises(urllib.error.HTTPError):
        pf("https://api.lever.co/v0/postings/x")
    assert pf.rate_limited == 0 and pf.cooling() == []


# -- the committed seed file ------------------------------------------------- #

def test_write_watchlist_seed_sorts_dedupes_and_round_trips_commas(tmp_path):
    p = tmp_path / "wl.csv"
    rows = [{"company": "Zeta", "ats": "lever", "board_id": "zeta"},
            {"company": "Apptronik, Inc.", "ats": "greenhouse", "board_id": "apptronik"},
            {"company": "Zeta Again", "ats": "lever", "board_id": "zeta"},      # same board
            {"company": "Bad", "ats": "greenhouse", "board_id": "evil.com/x"}]  # invalid slug
    written = growth.write_watchlist_seed(p, rows)
    assert [r["company"] for r in written] == ["Apptronik, Inc.", "Zeta"]
    assert p.read_text().splitlines()[0] == "company,ats,board_id"
    assert growth.read_watchlist_seed(p) == written
    assert growth.per_ats_counts(written) == {"greenhouse": 1, "lever": 1}


def test_committed_seed_is_sorted_deduped_and_valid():
    rows = growth.read_watchlist_seed(growth.WATCHLIST_SEED)
    assert len(rows) >= 800
    keys = [(r["company"], r["ats"], r["board_id"]) for r in rows]
    assert keys == sorted(keys)
    assert len({(r["ats"], r["board_id"]) for r in rows}) == len(rows)
    assert all(growth.valid_board_id_for(r["ats"], r["board_id"]) for r in rows)
    assert {r["ats"] for r in rows} <= set(growth.DISCOVER_ATS) | {"recruitee"}


# -- the in-app weekly pass --------------------------------------------------- #

def test_discover_sponsors_is_bounded_per_pass_and_advances_through_the_list(tmp_path):
    wl = Watchlist(tmp_path / "wl.db")
    wl.add_company("Already Watched Co", "greenhouse", "alreadywatched")
    ats = FakeATS(gh_jobs={"datadog": _JOB, "stripe": _JOB},
                  gh_names={"datadog": "Datadog", "stripe": "Stripe"},
                  ashby={"americanexpress": _ASHBY_JOB})
    rows = [_row("Already Watched Co", 900), _row("American Express Co", 500),
            _row("Datadog Inc", 400), _row("Nobody Here Ltd", 300), _row("Stripe Inc", 200)]
    cp = tmp_path / "discover.jsonl"
    kw = dict(sponsor_rows=rows, fetch=polite(ats), workers=1, max_minutes=0)
    s1 = growth.discover_sponsors(wl, cp, budget=2, **kw)
    assert s1["candidates"] == 2 and s1["added"] == 2
    assert {(c["ats"], c["board_id"]) for c in wl.companies()} == {
        ("greenhouse", "alreadywatched"), ("ashby", "americanexpress"), ("greenhouse", "datadog")}
    s2 = growth.discover_sponsors(wl, cp, budget=2, **kw)          # next slice, not the same two
    assert s2["candidates"] == 2 and s2["added"] == 1 and s2["probed"] == 2
    assert ("greenhouse", "stripe") in {(c["ats"], c["board_id"]) for c in wl.companies()}
    before = len(ats.urls)
    s3 = growth.discover_sponsors(wl, cp, budget=2, **kw)          # list exhausted: no requests
    assert s3["candidates"] == 0 and len(ats.urls) == before


# -- the maintainer script ----------------------------------------------------- #

def _load_script():
    spec = importlib.util.spec_from_file_location("grow_watchlist", ROOT / "scripts" / "grow_watchlist.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_appends_new_boards_sorted_and_writes_a_summary(tmp_path, monkeypatch, capsys):
    sponsors = tmp_path / "sponsors.csv.gz"
    with gzip.open(sponsors, "wt", newline="") as f:
        f.write("norm_name,display_name,h1b_approvals,h1b_last_fy,h1b_first_fy,naics,state,"
                "cap_exempt,e_verify,perm_certs\n"
                "datadog,Datadog Inc,120,2023,2020,51,NY,0,0,0\n"
                "stripe,Stripe Inc,90,2023,2020,52,CA,0,0,0\n"
                "charles schwab and company,Charles Schwab & Company Inc,500,2023,2020,52,TX,0,0,0\n")
    seed = tmp_path / "watchlist.csv"
    seed.write_text("company,ats,board_id\nStripe Inc,greenhouse,stripe\n")
    ats = FakeATS(gh_jobs={"datadog": _JOB, "charles": _JOB, "stripe": _JOB},
                  gh_names={"datadog": "Datadog", "charles": "charles", "stripe": "Stripe"})
    fetch = polite(ats)
    monkeypatch.setattr(growth, "PoliteFetch", lambda **kw: fetch)
    script = _load_script()
    cp = tmp_path / "cp.jsonl"
    rc = script.main(["--sponsors", str(sponsors), "--seed", str(seed), "--checkpoint", str(cp),
                      "--limit", "10", "--max-minutes", "5", "--workers", "1"])
    assert rc == 0
    rows = growth.read_watchlist_seed(seed)
    assert [(r["company"], r["ats"], r["board_id"]) for r in rows] == [
        ("Datadog Inc", "greenhouse", "datadog"), ("Stripe Inc", "greenhouse", "stripe")]
    # Stripe was already watched, so it was never probed; Schwab's squatted board was rejected.
    assert not any("stripe" in u for u in ats.urls)
    out = capsys.readouterr().out
    assert "watchlist: 1 -> 2 boards" in out and "rejected live boards (1)" in out
    summary = json.loads(cp.with_suffix(".summary.json").read_text())
    assert summary["before"] == 1 and summary["after"] == 2 and summary["per_ats"] == {"greenhouse": 1}
    # Rerunning is a no-op on the network: everything is checkpointed.
    n = len(ats.urls)
    assert script.main(["--sponsors", str(sponsors), "--seed", str(seed), "--checkpoint", str(cp),
                        "--limit", "10"]) == 0
    assert len(ats.urls) == n and len(growth.read_watchlist_seed(seed)) == 2
