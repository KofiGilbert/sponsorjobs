"""The freehire search-by-sponsor pass (sourcing/freehire_sponsors.py).

Runs against a stub freehire on 127.0.0.1 (a real HTTP server, so headers, status codes and
query strings go through urllib exactly as in production). No call leaves the machine.
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sourcing import freehire_sponsors as fs
from sourcing.sponsors import looks_us
from sourcing.watchlist import Watchlist


# -- the stub API ----------------------------------------------------------------------- #

class Stub:
    """Companies and jobs the stub serves, plus a log of every request it answered."""

    def __init__(self):
        self.companies: list[dict] = []                 # {"slug", "name", "job_count"}
        self.jobs: dict[str, list[dict]] = {}           # slug -> raw freehire rows
        self.requests: list[tuple[str, dict]] = []
        self.status_queue: list[tuple[int, dict]] = []  # forced (status, headers) for next requests
        self.headers = {"X-RateLimit-Limit": "300", "X-RateLimit-Remaining": "250",
                        "X-RateLimit-Reset": "1"}
        self.ignore_params = False

    def handle(self, path, qs):
        self.requests.append((path, qs))
        if self.status_queue:
            status, headers = self.status_queue.pop(0)
            if status != 200:
                return status, headers, {"error": "stub"}
        meta = {"ignored_params": [{"param": "company_slug"}]} if self.ignore_params else {}
        if path == "/api/v1/companies":
            q = (qs.get("q") or [""])[0].lower()
            data = [c for c in self.companies if q in c["name"].lower()]
            return 200, self.headers, {"data": data, "meta": meta}
        if path == "/api/v1/agent/jobs/search":
            rows = [r for s in qs.get("company_slug", []) for r in self.jobs.get(s, [])]
            off, lim = int(qs["offset"][0]), int(qs["limit"][0])
            return 200, self.headers, {"data": rows[off:off + lim], "meta": meta}
        return 404, {}, {"error": "nope"}


@pytest.fixture
def api():
    stub = Stub()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            status, headers, body = stub.handle(u.path, urllib.parse.parse_qs(u.query))
            raw = json.dumps(body).encode()
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    stub.base = f"http://127.0.0.1:{srv.server_address[1]}/api/v1"
    yield stub
    srv.shutdown()
    srv.server_close()


def _seed(*names_approvals):
    return [{"norm_name": "", "display_name": n, "h1b_approvals": a, "h1b_last_fy": 2023}
            for n, a in names_approvals]


def _raw(slug, company, title="Data Engineer", loc="Austin, TX, United States", jd="Build data pipelines.",
         url=None):
    return {"public_slug": slug, "company": company, "title": title, "location": loc,
            "url": url or f"https://jobs.example/{slug}", "description": jd,
            "posted_at": "2026-10-17T00:00:00Z"}


SPONSORS = {"zentrix analytics", "korvel labs", "plumeria health"}


def tag(jobs):
    """A stand-in for SponsorDB.tag_jobs: a badge for the sponsor names above, US roles only."""
    from sourcing.freehire_sponsors import name_tokens
    for j in jobs:
        j["us"] = looks_us(j.get("location", ""))
        hit = " ".join(name_tokens(j.get("company", ""))) in SPONSORS
        j["visa"] = [{"type": "H-1B"}] if (hit and j["us"]) else []
    return jobs


def _run(api, tmp_path, store, seed, **kw):
    kw.setdefault("budget", 50)
    return fs.run_pass(store, tmp_path, tag, seed_rows=seed, base=api.base, sleep=lambda s: None,
                       log=lambda *a: None, **kw)


# -- company-name matching ------------------------------------------------------------- #

@pytest.mark.parametrize("a,b", [
    ("Zentrix Analytics Inc", "Zentrix Analytics, Inc."),
    ("Korvel Labs LLC", "KORVEL LABS"),
    ("Plumeria Health Corp.", "Plumeria Health Corporation"),
    ("JPMorgan Chase & Co", "JP Morgan Chase"),
    ("Ernst Young Us Llp", "Ernst & Young"),
    ("Tata Consultancy Svcs Ltd", "Tata Consultancy Services"),
    ("Wells Fargo Bank NA", "Wells Fargo"),
    ("Amazon Com Services Llc", "Amazon"),                 # a curated brand spans its entities
    ("The Korvel Labs", "Korvel Labs"),
])
def test_names_that_are_the_same_employer_match(a, b):
    assert fs.names_match(a, b)


@pytest.mark.parametrize("a,b", [
    ("Nowhere Corp", "Nowhere Partners"),                  # a lone generic word never stretches
    ("Korvel Labs", "Korvel Biotech"),
    ("Zentrix Analytics", "Zentrix"),                      # the shorter side needs two words
    ("Acme", ""),
    ("", "Acme"),
])
def test_different_employers_do_not_match(a, b):
    assert not fs.names_match(a, b)


def test_search_term_is_the_distinctive_part_of_the_name():
    assert fs.search_term("Amazon Com Services LLC") == "amazon"
    assert fs.search_term("Zentrix Analytics Inc") == "zentrix"
    assert fs.search_term("American Express Travel") == "american express"
    assert fs.search_term("Tata Consultancy Svcs Ltd") == "tata consultancy"


def test_ranking_skips_covered_and_inactive_employers():
    seed = _seed(("Zentrix Analytics Inc", 900), ("Korvel Labs LLC", 500), ("Plumeria Health", 700))
    seed += [{"norm_name": "", "display_name": "Old Filer Inc", "h1b_approvals": 5000, "h1b_last_fy": 2015}]
    seed += _seed(("Zentrix Analytics LLC", 50))           # a second filing entity is summed
    got = fs.ranked_employers(seed, covered_names=["Plumeria Health, Inc."])
    assert [(e["name"], e["approvals"]) for e in got] == [("Zentrix Analytics Inc", 950),
                                                         ("Korvel Labs LLC", 500)]


# -- the pass --------------------------------------------------------------------------- #

def test_pass_adds_sponsor_rows_linked_to_the_employer(api, tmp_path):
    api.companies = [{"slug": "zentrix", "name": "Zentrix Analytics", "job_count": 3},
                     {"slug": "zentrix-gmbh-other", "name": "Zentrixx Media", "job_count": 4}]
    api.jobs = {"zentrix": [_raw("z1", "Zentrix Analytics"), _raw("z2", "Zentrix Analytics", title="ML Engineer"),
                            _raw("z3", "Zentrix Analytics", loc="Berlin, Germany")],
                "zentrix-gmbh-other": [_raw("x1", "Zentrixx Media")]}
    w = Watchlist(":memory:")
    s = _run(api, tmp_path, w, _seed(("Zentrix Analytics Inc", 900)))
    got = {j["source_id"]: j for j in w.list_jobs()}
    assert set(got) == {"freehire::z1", "freehire::z2"}          # Berlin dropped, wrong co never asked
    assert all(j["source"] == "freehire" for j in got.values())  # internal only, never shown
    assert got["freehire::z1"]["url"] == "https://jobs.example/z1"  # the employer's own posting
    assert s["new"] == 2 and s["not_sponsor"] == 1
    searched = [qs for p, qs in api.requests if p.endswith("/agent/jobs/search")]
    assert searched and searched[0]["company_slug"] == ["zentrix"]
    assert searched[0]["countries"] == ["US"] and searched[0]["posted_within_days"] == ["14"]


def test_rows_failing_the_feed_rules_are_not_stored(api, tmp_path):
    api.companies = [{"slug": "korvel", "name": "Korvel Labs", "job_count": 3},
                     {"slug": "nobadge", "name": "Unbadged Works", "job_count": 1}]
    api.jobs = {"korvel": [
        _raw("k-ok", "Korvel Labs"),
        _raw("k-no", "Korvel Labs", jd="We are unable to sponsor visas for this role."),
        _raw("k-clr", "Korvel Labs", jd="Must be a U.S. citizen with an active Secret clearance."),
    ], "nobadge": [_raw("n1", "Unbadged Works")]}
    w = Watchlist(":memory:")
    _run(api, tmp_path, w, _seed(("Korvel Labs", 500), ("Unbadged Works", 400)))
    assert {j["source_id"] for j in w.list_jobs()} == {"freehire::k-ok"}


def test_dedup_against_rows_we_already_hold(api, tmp_path):
    w = Watchlist(":memory:")
    board = {"source_id": "greenhouse:korvel:9", "source": "greenhouse", "company": "Korvel Labs",
             "title": "Data Engineer", "location": "Austin, TX, United States", "remote": "",
             "url": "https://jobs.example/k-dup", "jd_text": "x", "posted_at": "2026-10-17"}
    held = {"source_id": "freehire::k-held", "source": "freehire", "company": "Korvel Labs",
            "title": "ML Engineer", "location": "Austin, TX", "remote": "",
            "url": "https://jobs.example/k-held", "jd_text": "x", "posted_at": "2026-10-17"}
    w.upsert_jobs([board, held])
    api.companies = [{"slug": "korvel", "name": "Korvel Labs", "job_count": 3}]
    api.jobs = {"korvel": [_raw("k-dup", "Korvel Labs", url="https://jobs.example/k-dup?utm_source=fh"),
                           _raw("k-held", "Korvel Labs", title="ML Engineer"),
                           _raw("k-new", "Korvel Labs", title="Analyst")]}
    s = _run(api, tmp_path, w, _seed(("Korvel Labs", 500)))
    ids = {j["source_id"] for j in w.list_jobs()}
    assert "freehire::k-dup" not in ids                     # the board's own copy wins
    assert {"freehire::k-new", "freehire::k-held", "greenhouse:korvel:9"} <= ids
    assert s["duplicates"] == 1 and s["already_held"] == 1 and s["new"] == 1


def test_request_cap_and_cursor_rotation(api, tmp_path):
    names = [f"Employer{i:02d} Holdings" for i in range(12)]
    api.companies = [{"slug": f"e{i}", "name": n, "job_count": 1} for i, n in enumerate(names)]
    seed = _seed(*[(n, 1000 - i) for i, n in enumerate(names)])
    w = Watchlist(":memory:")
    s1 = _run(api, tmp_path, w, seed, budget=5)
    assert s1["requests"] == 5 == len(api.requests)          # never one request over the cap
    # 5 company look-ups, no search yet: the next run starts again at the first unsearched one,
    # and the cached slugs mean it does not ask for them twice.
    assert s1["start"] == 0
    asked = [qs["q"][0] for p, qs in api.requests if p.endswith("/companies")]
    api.requests.clear()
    s2 = _run(api, tmp_path, w, seed, budget=8)
    asked2 = [qs["q"][0] for p, qs in api.requests if p.endswith("/companies")]
    assert not set(asked) & set(asked2)                      # cached: not asked again
    assert any(p.endswith("/agent/jobs/search") for p, _ in api.requests)
    cur = json.loads((tmp_path / fs.CURSOR_FILE).read_text())
    assert cur["next"] != "employer00 holdings"              # the cursor moved past searched ones
    s3 = _run(api, tmp_path, w, seed, budget=8)
    assert s3["start"] > s2["start"] or s3["start"] == 0


def test_cursor_wraps_after_the_last_employer(api, tmp_path):
    api.companies = [{"slug": "zentrix", "name": "Zentrix Analytics", "job_count": 1}]
    api.jobs = {"zentrix": [_raw("z1", "Zentrix Analytics")]}
    w = Watchlist(":memory:")
    seed = _seed(("Zentrix Analytics", 900), ("Korvel Labs", 800))
    s = _run(api, tmp_path, w, seed)
    assert s["employers"] == 2
    assert json.loads((tmp_path / fs.CURSOR_FILE).read_text())["next"] == "zentrix analytics"


def test_backs_off_on_429_then_retries(api, tmp_path):
    api.companies = [{"slug": "zentrix", "name": "Zentrix Analytics", "job_count": 1}]
    api.jobs = {"zentrix": [_raw("z1", "Zentrix Analytics")]}
    api.status_queue = [(429, {"Retry-After": "7"})]
    slept = []
    w = Watchlist(":memory:")
    s = fs.run_pass(w, tmp_path, tag, seed_rows=_seed(("Zentrix Analytics", 900)), base=api.base,
                    sleep=slept.append, log=lambda *a: None, budget=10)
    assert 7.0 in slept and s["rate_limited"] == 1 and s["new"] == 1
    assert s["requests"] == 3                                 # the refused one counts too


def test_stops_for_the_run_on_a_second_429(api, tmp_path):
    api.companies = [{"slug": "zentrix", "name": "Zentrix Analytics", "job_count": 1}]
    api.status_queue = [(429, {"Retry-After": "1"}), (429, {"Retry-After": "1"})]
    w = Watchlist(":memory:")
    s = _run(api, tmp_path, w, _seed(("Zentrix Analytics", 900), ("Korvel Labs", 800)), budget=50)
    assert s["stopped"] == "rate limited twice" and s["requests"] == 2
    assert not (tmp_path / fs.SLUG_CACHE_FILE).exists() or "zentrix analytics" not in \
        json.loads((tmp_path / fs.SLUG_CACHE_FILE).read_text())


def test_slows_down_when_remaining_budget_is_low(api, tmp_path):
    api.headers = {"X-RateLimit-Remaining": "2", "X-RateLimit-Reset": "4"}
    api.companies = [{"slug": "zentrix", "name": "Zentrix Analytics", "job_count": 1}]
    slept = []
    fs.run_pass(Watchlist(":memory:"), tmp_path, tag, seed_rows=_seed(("Zentrix Analytics", 900)),
                base=api.base, sleep=slept.append, log=lambda *a: None, budget=3)
    assert slept and all(s == 4.0 for s in slept)


def test_an_ignored_company_filter_stops_the_pass(api, tmp_path):
    """freehire drops an unknown parameter instead of refusing it; a renamed company_slug would
    return the whole catalogue. The pass must stop, not ingest that."""
    api.companies = [{"slug": "zentrix", "name": "Zentrix Analytics", "job_count": 1}]
    api.jobs = {"zentrix": [_raw("z1", "Zentrix Analytics")]}
    api.ignore_params = True
    w = Watchlist(":memory:")
    s = _run(api, tmp_path, w, _seed(("Zentrix Analytics", 900)))
    assert s["stopped"].startswith("the API ignored") and w.list_jobs() == []


def test_zero_budget_switches_the_pass_off(api, tmp_path):
    s = _run(api, tmp_path, Watchlist(":memory:"), _seed(("Zentrix Analytics", 900)), budget=0)
    assert s["stopped"] == "off" and api.requests == []


def test_budget_comes_from_the_env(api, tmp_path, monkeypatch):
    monkeypatch.setenv("FREEHIRE_SPONSOR_PASS_REQUESTS", "2")
    names = [f"Employer{i:02d} Holdings" for i in range(5)]
    api.companies = [{"slug": f"e{i}", "name": n, "job_count": 1} for i, n in enumerate(names)]
    s = fs.run_pass(Watchlist(":memory:"), tmp_path, tag,
                    seed_rows=_seed(*[(n, 100 - i) for i, n in enumerate(names)]),
                    base=api.base, sleep=lambda s: None, log=lambda *a: None)
    assert s["requests"] == 2


def test_build_runs_the_pass_in_shard_zero_only(monkeypatch, tmp_path):
    import scripts.build_feed as bf
    assert bf.owns_lanes(0) and not bf.owns_lanes(1)
    # The pass's state files travel with shard 0's delta, so the merge job keeps them.
    assert set(bf.FREEHIRE_SPONSOR_FILES) == {fs.CURSOR_FILE, fs.SLUG_CACHE_FILE}


def test_rows_without_an_employer_url_are_dropped(api, tmp_path):
    api.companies = [{"slug": "zentrix", "name": "Zentrix Analytics", "job_count": 3}]
    api.jobs = {"zentrix": [_raw("z1", "Zentrix Analytics"),
                            _raw("z2", "Zentrix Analytics", title="ML Engineer",
                                 url="https://freehire.me/jobs/z2"),
                            {**_raw("z3", "Zentrix Analytics", title="Analyst"), "url": ""}]}
    w = Watchlist(":memory:")
    s = _run(api, tmp_path, w, _seed(("Zentrix Analytics", 900)))
    assert {j["source_id"] for j in w.list_jobs()} == {"freehire::z1"}
    assert s["no_employer_url"] == 2


@pytest.mark.parametrize("url,ok", [
    ("https://boards.greenhouse.io/acme/jobs/1", True),
    ("http://careers.acme.com/job/2", True),
    ("https://freehire.me/jobs/abc", False),
    ("https://www.freehire.me/jobs/abc", False),
    ("", False),
    ("/jobs/abc", False),
])
def test_only_employer_urls_count(url, ok):
    from sourcing.ats import freehire_employer_url_ok
    assert freehire_employer_url_ok({"url": url}) is ok


def test_bulk_and_search_drop_rows_without_an_employer_url():
    from sourcing.ats import freehire_bulk, freehire_jobs
    data = {"data": [_raw("a", "Acme"), _raw("b", "Acme", url="https://freehire.me/jobs/b")]}
    assert [r["source_id"] for r in freehire_bulk(fetch=lambda u, **k: data, cap=100)] == ["freehire::a"]
    assert [r["source_id"] for r in freehire_jobs("x", fetch=lambda u, **k: data)] == ["freehire::a"]
