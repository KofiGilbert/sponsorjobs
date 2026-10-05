"""Sourcing service: refresh the watchlist and filter roles by the person's
criteria. GREEN lane only — see :mod:`sourcing`.
"""

from __future__ import annotations

import csv
import os
import re
import time
from pathlib import Path

from .ats import ADAPTERS, AGGREGATORS, fetch_json, freehire_bulk, normalize_jobs
from .dedup import DedupIndex
from .quality import LIST_ONLY_SOURCES, is_intl_student_accessible
from .watchlist import Watchlist

# The list-only boards (Workday, SmartRecruiters) deliver rows WITHOUT a description; the
# accessibility check reads the description, so a refresh fetches each kept row's detail first
# (select_checked_rows) under a per-board budget. Two budgets per ATS: a desktop refresh
# (WORKDAY_MAX_DETAIL / SMARTRECRUITERS_MAX_DETAIL, default 200) and the central feed build
# (scripts/build_feed.py on GitHub Actions, backend/feed.py's robot), which has time to spare and
# may read many more per board per run (*_MAX_DETAIL_FEED, default 2000).
DEFAULT_MAX_DETAIL = 200
FEED_MAX_DETAIL = 2000
FEED_WORKDAY_MAX_DETAIL = FEED_MAX_DETAIL
DETAIL_BUDGET_ENV = {"workday": "WORKDAY_MAX_DETAIL",
                     "smartrecruiters": "SMARTRECRUITERS_MAX_DETAIL"}
assert set(DETAIL_BUDGET_ENV) == set(LIST_ONLY_SOURCES)

# The FIRST-OPEN crawl (first_open_refresh): what a fresh install with no feed to download runs,
# once, from the person's laptop. Deliberately small and polite -- the full 1,105-board watchlist
# at full speed got one IP barred by Workable for 19 hours (2026-10-01), and during a feed outage
# every new install would have done the same at once. Top N boards by the company's H-1B volume
# (a few of every ATS kept), one request at a time with per-host spacing, a wall-clock cap, and a
# small description budget on the list-only boards. All env-tunable; JOBS_FIRST_CRAWL=0 in ui/app.py
# switches the whole thing off.
FIRST_CRAWL_BOARDS = 300            # env JOBS_FIRST_CRAWL_BOARDS (0 = crawl no boards)
FIRST_CRAWL_MINUTES = 10.0          # env JOBS_FIRST_CRAWL_MINUTES
FIRST_CRAWL_DETAIL = 20             # env JOBS_FIRST_CRAWL_DETAIL (per list-only board)
FIRST_CRAWL_TIMEOUT = 30         # seconds per request; generous because this runs on laptops
FIRST_CRAWL_PER_ATS = 10            # boards of each ATS guaranteed a place in the top N

# A committed list of the sponsor boards we've already discovered, so a fresh install (or the
# hosted feed on an empty disk) starts with the full watchlist and fills jobs with a light
# refresh — no heavy discovery crawl needed just to have a board.
SEED_WATCHLIST = Path(__file__).resolve().parents[1] / "seed" / "watchlist_seed.csv"

SUPPORTED_ATS = tuple(ADAPTERS.keys())
SUPPORTED_AGGREGATORS = tuple(AGGREGATORS.keys())
# Aggregators are keyword feeds, so they run only when the person has title criteria
# (otherwise it's the whole firehose). Enabled by default; the person can opt out via
# criteria["aggregators"]. remotive / remoteok are KEYLESS (run out of the box); adzuna/jsearch
# no-op until their keys are set. freehire is handled SEPARATELY as a bulk pull (see step 3 in
# refresh_watchlist), not per-term, because it's a free 1M+ minute-fresh source best grabbed in
# bulk. This product is US visa sponsorship for international students, so arbeitnow (a GERMAN
# board, it was surfacing Munich/EU roles) is deliberately NOT a default; the board is also
# filtered to US roles server-side. remoteok is US-remote heavy.
#
# ADZUNA is deliberately NOT a default: its API licenses only a ~500-char PREVIEW of each posting
# (never the full text), so every Adzuna row's JD ended mid-word. At one point it was ~40% of the
# hosted board -- that's what made the JDs look "scanty". freehire carries the same roles with the
# FULL text for free, so we lean on it for volume (see FREEHIRE_BULK_MAX) and drop Adzuna.
DEFAULT_AGGREGATORS = ("remotive", "remoteok", "jsearch")

# Seed a few real, official public boards so the watchlist shows live jobs
# immediately. The person edits this list freely.
SEED_COMPANIES = [
    ("Dropbox", "greenhouse", "dropbox"),
    ("Spotify", "lever", "spotify"),
    ("Ramp", "ashby", "ramp"),
    # The seed shipped greenhouse + lever + ashby only, and submission on all three is
    # gated behind the EMPLOYER's key (§7), so they are ASSISTED forever. Recruitee is the
    # only ATS on the AUTO allowlist, which meant the paid autopilot had zero possible
    # targets out of the box: not few, zero, by construction, from this list.
    # Verified live 2026-07-15: channable.recruitee.com/api/offers/ returns 15 offers.
    # Sober about what this buys: Recruitee is a mostly-European ATS, and exactly 1 of
    # those 15 is in the US, so this does not make autopilot meaningful. It makes the AUTO
    # path real and exercised end to end instead of theoretical. The honest fix for the
    # paid tier is what it CLAIMS, not more boards.
    ("Channable", "recruitee", "channable"),
]
# A broad-but-relevant default so the first list isn't hundreds of unrelated roles.
SEED_CRITERIA = {
    "titles": ["engineer", "developer", "architect", "machine learning", "ml",
               "ai", "data scientist", "software"],
    "locations": [],
    "remote": "any",
}

# Cap on stored matches per board per refresh. 60 suits a laptop crawling for one person; the
# central feed crawl (scripts/build_feed.py, run by the feed workflow) raises it through the
# env so a 2,000-posting Workday tenant is not cut to 60 rows for everyone.
MAX_PER_COMPANY = int(os.environ.get("JOBS_MAX_PER_COMPANY", "60"))


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


def max_detail_for(ats: str) -> int:
    """The per-board detail budget of a list-only ATS for a DESKTOP refresh (env
    WORKDAY_MAX_DETAIL / SMARTRECRUITERS_MAX_DETAIL, default 200)."""
    return _env_int(DETAIL_BUDGET_ENV[ats], DEFAULT_MAX_DETAIL)


def feed_max_detail_for(ats: str) -> int:
    """The per-board detail budget of a list-only ATS for the CENTRAL crawl (env
    WORKDAY_MAX_DETAIL_FEED / SMARTRECRUITERS_MAX_DETAIL_FEED, default 2000)."""
    return _env_int(DETAIL_BUDGET_ENV[ats] + "_FEED", FEED_MAX_DETAIL)


def feed_detail_budgets() -> dict[str, int]:
    """{ats: budget} for every list-only ATS, the central crawl's `detail_budgets`."""
    return {ats: feed_max_detail_for(ats) for ats in DETAIL_BUDGET_ENV}


def feed_workday_max_detail() -> int:
    """The per-board Workday detail budget for the CENTRAL crawl (env WORKDAY_MAX_DETAIL_FEED)."""
    return feed_max_detail_for("workday")


def _fill_detail(ats: str, job: dict, fetch, sleep) -> bool:
    """Read one list-only row's description from its board's public per-posting endpoint (each
    ATS's own adapter does the request, with its pacing and 429 / 5xx back-off). True once the
    row carries a non-blank jd_text."""
    if ats == "workday":
        from .workday import fill_details
        fill_details([job], fetch=fetch, max_detail=1, sleep=sleep)
    elif ats == "smartrecruiters":
        from .ats import SMARTRECRUITERS_REQUEST_GAP, smartrecruiters_fill
        smartrecruiters_fill(job, fetch, sleep)
        sleep(SMARTRECRUITERS_REQUEST_GAP)
    else:                                              # pragma: no cover - guarded by the caller
        raise ValueError(f"not a list-only ATS: {ats!r}")
    return bool((job.get("jd_text") or "").strip())


def select_checked_rows(ats: str, jobs: list[dict], criteria: dict, fetch=fetch_json, known_ids=(),
                        max_detail: int | None = None, cap: int = MAX_PER_COMPANY,
                        sleep=time.sleep) -> tuple[list[dict], int, int]:
    """The rows of a list-only board (Workday, SmartRecruiters) a refresh may keep:
    criteria-matching AND accessibility-CHECKED.

    These boards' rows arrive list-only and the accessibility check (is_intl_student_accessible)
    reads the description, so with no JD it can only judge the employer -- a posting saying "US
    citizenship required" or "active TS/SCI clearance" would pass. A row therefore counts as
    checked only once its JD has been fetched here, or is already stored with one (it passed
    the check when it was stored; `known_ids`). Details are fetched NEWEST FIRST (then board
    order) so the budget goes to fresh roles, until `cap` accessible rows are confirmed or the
    per-board detail budget (`max_detail`, env {ATS}_MAX_DETAIL / {ATS}_MAX_DETAIL_FEED) is spent.
    Rows the budget did not reach, and rows whose detail could not be read, are NOT returned and
    so never stored: an unchecked row never reaches the board or the public feed (it is simply
    picked up by a later refresh). Returns (kept, details_fetched, left_unchecked)."""
    if ats not in DETAIL_BUDGET_ENV:
        raise ValueError(f"not a list-only ATS: {ats!r}")
    if max_detail is None:
        max_detail = max_detail_for(ats)
    known = set(known_ids or ())
    kept: list[dict] = []
    fetched = unchecked = 0
    # Stable sort: newest posting date first, undated last, board order within a date.
    ordered = sorted(jobs, key=lambda j: j.get("posted_at") or "", reverse=True)
    for j in ordered:
        if not matches_criteria(j, criteria):
            continue
        if len(kept) >= cap:
            break
        if j.get("source_id") in known or (j.get("jd_text") or "").strip():
            pass                                   # stored with a checked JD, or carries one
        elif fetched >= max_detail:
            unchecked += 1                         # budget spent: left for a later refresh
            continue
        else:
            fetched += 1
            if not _fill_detail(ats, j, fetch, sleep):
                unchecked += 1                     # detail unreadable / blank: cannot be checked
                continue
        if is_intl_student_accessible(j):
            kept.append(j)
    return kept, fetched, unchecked


def select_workday_rows(jobs: list[dict], criteria: dict, fetch=fetch_json, known_ids=(),
                        max_detail: int | None = None, cap: int = MAX_PER_COMPANY,
                        sleep=time.sleep) -> tuple[list[dict], int, int]:
    """select_checked_rows for a Workday board (the original, kept for its callers)."""
    return select_checked_rows("workday", jobs, criteria, fetch, known_ids, max_detail, cap, sleep)


def seed_if_empty(store: Watchlist) -> None:
    """First run: fill the watchlist from the committed discovered-boards seed (hundreds of
    pre-vetted sponsors) so the feed has real jobs after a light refresh. Falls back to the
    tiny built-in example set if the seed file is missing."""
    if not store.is_empty():
        return
    added = 0
    if SEED_WATCHLIST.exists():
        try:
            with open(SEED_WATCHLIST, encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    try:
                        store.add_company(row["company"], row["ats"], row["board_id"])
                        added += 1
                    except (KeyError, ValueError):
                        continue                       # skip a malformed / invalid-slug row
        except OSError:
            added = 0
    if not added:
        for company, ats, board in SEED_COMPANIES:
            store.add_company(company, ats, board)
    store.set_criteria(SEED_CRITERIA)


def matches_criteria(job: dict, criteria: dict) -> bool:
    titles = [t.lower() for t in criteria.get("titles", []) if t.strip()]
    locs = [l.lower() for l in criteria.get("locations", []) if l.strip()]
    remote_pref = (criteria.get("remote") or "any").lower()

    title = (job.get("title") or "").lower()
    loc = (job.get("location") or "").lower()
    jr = (job.get("remote") or "").lower()
    is_remote = jr == "remote" or "remote" in loc

    # Whole-keyword match so "ai" hits "AI" but not "Affairs", "ml" not "html". Uses
    # alphanumeric look-arounds (not \b) so keywords with symbol edges — "c++", "c#",
    # ".net" — still match; \b would silently never match those.
    if titles and not any(
        re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", title) for k in titles):
        return False
    if remote_pref == "remote" and not is_remote:
        return False
    if remote_pref in ("hybrid", "onsite") and jr != remote_pref:
        return False
    if locs and not (is_remote or any(k in loc for k in locs)):
        return False
    return True


def refresh_watchlist(store: Watchlist, fetch=fetch_json, *, workday_max_detail: int | None = None,
                      detail_budgets: dict[str, int] | None = None, sleep=time.sleep,
                      boards: list[dict] | None = None, time_budget: float | None = None,
                      clock=time.monotonic) -> dict:
    """Fetch every watched company's official feed, filter by criteria, and
    upsert matches. Returns a summary the UI can show. `detail_budgets` is the per-board
    description budget of each list-only ATS ({"workday": n, "smartrecruiters": n}; default: env
    WORKDAY_MAX_DETAIL / SMARTRECRUITERS_MAX_DETAIL; the central feed build passes
    feed_detail_budgets()); `workday_max_detail` is the older single-ATS form of the same knob.
    `sleep` paces the detail reads (injectable for tests). `boards` restricts the board pass to
    these watchlist rows (default: every active board); `time_budget` (seconds, by `clock`) stops
    the board pass once it is spent -- the aggregator pulls that follow are a handful of requests
    and still run. The summary's `boards_crawled` / `boards_total` / `stopped_early` say how far
    it got."""
    criteria = store.get_criteria()
    fetched = matched = new_total = duplicates = 0
    budgets = dict(detail_budgets or {})
    if workday_max_detail is not None:
        budgets["workday"] = workday_max_detail
    unchecked = {ats: 0 for ats in DETAIL_BUDGET_ENV}
    errors: list[dict] = []
    started = clock()
    boards = list(store.companies(active_only=True) if boards is None else boards)
    crawled = 0
    stopped_early = False
    # The same opening reached through two feeds (a watched board and an aggregator)
    # is shown once. Seeded from what is already saved, so a re-listing of a known
    # posting under a new id is caught too.
    index = DedupIndex(store.list_jobs(include_dismissed=True))
    # 1) Watched company boards (Greenhouse/Lever/Ashby/Workable/SmartRecruiters/Recruitee/
    # Workday). Workday and SmartRecruiters rows arrive LIST-ONLY (a 2,000-job tenant or a
    # 25,000-posting franchise board would otherwise cost one detail request per posting): the
    # description is fetched here, newest first, for the rows that match the criteria and are not
    # already stored with one, until MAX_PER_COMPANY rows have been CHECKED accessible or the
    # per-board budget is spent. A row whose JD was not read is not stored -- the accessibility
    # check reads the JD, so an unchecked row must never be shown or published as accessible
    # (select_checked_rows).
    known: dict[str, set[str]] = {}
    for w in boards:
        if time_budget is not None and clock() - started >= time_budget:
            stopped_early = True                      # the cap is the whole point: stop, say so
            break
        crawled += 1
        try:
            jobs = normalize_jobs(w["ats"], w["board_id"], w["company"], fetch=fetch)
            fetched += len(jobs)
            if w["ats"] in DETAIL_BUDGET_ENV:
                if w["ats"] not in known:
                    known[w["ats"]] = store.source_ids_with_jd(w["ats"])
                jobs, _, left = select_checked_rows(w["ats"], jobs, criteria, fetch,
                                                    known[w["ats"]], budgets.get(w["ats"]),
                                                    sleep=sleep)
                unchecked[w["ats"]] += left
        except Exception as exc:  # a bad board id / network blip shouldn't kill the run
            errors.append({"company": w["company"], "error": str(exc)})
            continue
        keep = [j for j in jobs if matches_criteria(j, criteria)
                and is_intl_student_accessible(j)][:MAX_PER_COMPANY]
        keep, dup = index.filter(keep)
        duplicates += dup
        matched += len(keep)
        n, _ = store.upsert_jobs(keep)
        new_total += n

    # 2) Aggregator keyword feeds (Remotive, ...). Keyless, remote-only, GREEN lane.
    # Run one search per title term (bounded) so we never pull the whole firehose, and
    # skip entirely when the person wants onsite-only or has set no title criteria.
    remote_pref = (criteria.get("remote") or "any").lower()
    titles = [t for t in criteria.get("titles", []) if t.strip()][:4]
    if remote_pref != "onsite" and titles:
        for agg in (criteria.get("aggregators") or DEFAULT_AGGREGATORS):
            fn = AGGREGATORS.get(agg)
            if fn is None:
                continue
            seen: set[str] = set()
            for term in titles:
                try:
                    jobs = fn(term, fetch=fetch)
                except Exception as exc:
                    errors.append({"company": agg, "error": str(exc)})
                    continue
                jobs = [j for j in jobs if j["source_id"] not in seen]
                seen.update(j["source_id"] for j in jobs)
                fetched += len(jobs)
                keep = [j for j in jobs if matches_criteria(j, criteria)
                        and is_intl_student_accessible(j)][:MAX_PER_COMPANY]
                keep, dup = index.filter(keep)
                duplicates += dup
                matched += len(keep)
                n, _ = store.upsert_jobs(keep)
                new_total += n

    # 3) freehire BULK: the free high-volume feed. Pull the FRESHEST US roles across ALL fields
    # (not per title term), so the board carries large, minute-fresh volume at no cost. Only the
    # accessibility filter is applied (no title narrowing) because a job BOARD wants breadth --
    # the UI's date/visa/work-type/search facets narrow from there. Keyless; degrades gracefully.
    aggs = criteria.get("aggregators")
    # The general dump pulls recent US roles in EVERY field (restaurant, factory, retail). Since
    # 2026-10-01 the product only lists sponsor-relevant rows (quality.is_sponsor_relevant), so
    # that dump is off by default: FREEHIRE_GENERAL_BULK=1 turns it back on for a deployment
    # that wants the breadth and filters at serve time. The sponsor slice (3b) is the default.
    general_bulk = os.environ.get("FREEHIRE_GENERAL_BULK", "0") == "1"
    if (aggs is None or "freehire" in aggs) and general_bulk:
        try:
            fjobs = freehire_bulk(fetch=fetch)
            fetched += len(fjobs)
            keep = [j for j in fjobs if is_intl_student_accessible(j)]
            keep, dup = index.filter(keep)
            duplicates += dup
            matched += len(keep)
            n, _ = store.upsert_jobs(keep)
            new_total += n
        except Exception as exc:                         # never let one source kill the refresh
            errors.append({"company": "freehire", "error": str(exc)})

    if aggs is None or "freehire" in aggs:
        # 3b) freehire SPONSOR slice: a second, smaller pull using freehire's own verified
        # visa_sponsorship filter, so the board reliably carries a healthy depth of roles whose
        # POSTING states sponsorship -- the ones our international-student users most want. The
        # general bulk above already tags these, but leans to raw freshness; this guarantees depth.
        # Bounded + env-tunable (FREEHIRE_SPONSOR_ROWS, 0 to disable) so refresh load stays modest.
        try:
            scap = int(os.environ.get("FREEHIRE_SPONSOR_ROWS", "2000"))
        except ValueError:
            scap = 2000
        if scap > 0:
            try:
                sjobs = freehire_bulk(fetch=fetch, cap=scap, visa_sponsorship=True)
                fetched += len(sjobs)
                keep = [j for j in sjobs if is_intl_student_accessible(j)]
                keep, dup = index.filter(keep)
                duplicates += dup
                matched += len(keep)
                n, _ = store.upsert_jobs(keep)
                new_total += n
            except Exception as exc:                     # best-effort: never let it kill the refresh
                errors.append({"company": "freehire-sponsor", "error": str(exc)})

    # Retire Adzuna: its API licensed only a ~500-char preview of each posting, so its rows had
    # truncated JDs. We no longer ingest it (see DEFAULT_AGGREGATORS); clear any rows it left so
    # the board is full-text (freehire) only, rather than waiting out the 21-day stale window.
    try:
        pruned_adzuna = store.prune_source("adzuna")
    except Exception as exc:                             # noqa: BLE001 - best-effort cleanup
        pruned_adzuna, _ = 0, errors.append({"company": "adzuna-prune", "error": str(exc)})

    # Fill pay from the JD for any row a feed left blank, so the min-pay filter + top-paid sort
    # work across the whole board (not just rows a paid feed happened to price). Network-free.
    try:
        from sourcing.ats import salary_from_text
        backfilled = store.backfill_salaries(salary_from_text)
    except Exception as exc:                             # noqa: BLE001 - backfill is best-effort
        backfilled, _ = 0, errors.append({"company": "salary-backfill", "error": str(exc)})

    return {"fetched": fetched, "matched": matched, "new": new_total,
            "duplicates": duplicates, "workday_unchecked": unchecked["workday"],
            "unchecked": unchecked,
            "salary_backfilled": backfilled, "pruned_adzuna": pruned_adzuna, "errors": errors,
            "companies": len(store.companies()),
            "boards_crawled": crawled, "boards_total": len(boards), "stopped_early": stopped_early}


# -- the first-open crawl ------------------------------------------------------------------ #

def _seed_approvals() -> dict[str, int]:
    """{normalized employer: H-1B approvals} from the committed sponsor seed (the same public
    USCIS data the badges use), for ranking boards. Empty when the seed is missing."""
    try:
        from .growth import load_sponsor_seed
        out: dict[str, int] = {}
        for r in load_sponsor_seed():
            out[r["norm_name"]] = out.get(r["norm_name"], 0) + int(r["h1b_approvals"])
        return out
    except Exception:                                  # noqa: BLE001 - rank by name instead
        return {}


def rank_boards(boards: list[dict], approvals, limit: int | None,
                per_ats: int = FIRST_CRAWL_PER_ATS) -> list[dict]:
    """The `limit` boards most worth a fresh install's first requests: the companies with the
    most H-1B approvals first (`approvals(company) -> int`), with the top `per_ats` boards of
    EVERY ATS guaranteed a place so the pass is not all Greenhouse. Deterministic: ties by name.
    The result is interleaved by rank, never grouped by ATS, so one host is not hit in a run.
    `limit` None means every board; 0 (or less) means NO boards -- the one value a cautious
    maintainer sets to shrink a crawl must never widen it to the whole watchlist."""
    def key(b):
        # Cost tier first: a Greenhouse/Lever/Ashby board is ONE request, while a Workday or
        # SmartRecruiters tenant costs minutes of description fetches. A time-capped pass must
        # cover all the cheap boards before it spends its budget on a handful of tenants
        # (2026-10-01: a 23-minute runner pass reached 21 of 300 boards the other way round).
        tier = 1 if (b.get("ats") or "") in LIST_ONLY_SOURCES else 0
        return (tier, -int(approvals(b.get("company") or "") or 0), (b.get("company") or "").lower(),
                b.get("ats", ""), b.get("board_id", ""))
    ranked = sorted(boards, key=key)
    if limit is None or len(ranked) <= limit:
        return ranked
    if limit <= 0:
        return []
    chosen: list[dict] = []
    seen: set[int] = set()
    by_ats: dict[str, int] = {}
    for b in ranked:                                   # reserve a few seats per ATS first
        if by_ats.get(b.get("ats"), 0) < per_ats and len(chosen) < limit:
            chosen.append(b)
            seen.add(id(b))
            by_ats[b.get("ats")] = by_ats.get(b.get("ats"), 0) + 1
    for b in ranked:                                   # then the best of the rest
        if len(chosen) >= limit:
            break
        if id(b) not in seen:
            chosen.append(b)
            seen.add(id(b))
    chosen.sort(key=key)
    return chosen


def first_open_refresh(store: Watchlist, fetch=None, *, max_boards: int | None = None,
                       max_minutes: float | None = None, detail_budget: int | None = None,
                       approvals=None, sleep=time.sleep, clock=time.monotonic) -> dict:
    """The ONE crawl a fresh install runs when it has no feed to download (ui/app.py
    _kick_local_crawl): the top `max_boards` boards (env JOBS_FIRST_CRAWL_BOARDS, default 300;
    0 crawls NO boards and returns an empty summary at once; only None / an unset limit means
    every board) ranked by the company's H-1B approvals with a few of every ATS, each request paced per host
    by sourcing.growth.PoliteFetch (Workable at its slow lane, every Workday tenant on one lane,
    a 429 backs the host off and a cooling host is skipped, never hammered), under a wall-clock
    cap (env JOBS_FIRST_CRAWL_MINUTES, default 10) and with the Workday / SmartRecruiters
    description budget cut to `detail_budget` per board (env JOBS_FIRST_CRAWL_DETAIL, default
    20). Everything else is refresh_watchlist. `fetch`, `approvals(company)`, `sleep` and `clock`
    are injectable so the pass is testable offline. The regular auto-updater is untouched."""
    from .growth import PoliteFetch
    if max_boards is None:
        max_boards = _env_int("JOBS_FIRST_CRAWL_BOARDS", FIRST_CRAWL_BOARDS)
    if max_minutes is None:
        try:
            max_minutes = float(os.environ.get("JOBS_FIRST_CRAWL_MINUTES", "") or FIRST_CRAWL_MINUTES)
        except ValueError:
            max_minutes = FIRST_CRAWL_MINUTES
    if detail_budget is None:
        detail_budget = _env_int("JOBS_FIRST_CRAWL_DETAIL", FIRST_CRAWL_DETAIL)
    if approvals is None:
        from .sponsors import normalize_employer
        table = _seed_approvals()
        approvals = lambda company: table.get(normalize_employer(company or ""), 0)   # noqa: E731
    if max_boards is not None and max_boards <= 0:
        # "Crawl 0 boards" is a request to crawl nothing: no board, no aggregator, no request.
        return {"fetched": 0, "matched": 0, "new": 0, "duplicates": 0, "workday_unchecked": 0,
                "unchecked": {ats: 0 for ats in DETAIL_BUDGET_ENV}, "salary_backfilled": 0,
                "pruned_adzuna": 0, "errors": [], "companies": len(store.companies()),
                "boards_crawled": 0, "boards_total": 0, "stopped_early": False,
                "first_open": True, "requests": 0, "rate_limited": 0, "server_errors": 0,
                "cooling": []}
    boards = rank_boards(store.companies(active_only=True), approvals, max_boards)
    # A student's connection can be slow (a single Greenhouse board took 50s on one tested
    # link); the discovery probe's 8s timeout would turn every board into an error here.
    timeout = _env_int("JOBS_FIRST_CRAWL_TIMEOUT", FIRST_CRAWL_TIMEOUT)
    polite = PoliteFetch(fetch or fetch_json, timeout=timeout, sleep=sleep, clock=clock)
    summary = refresh_watchlist(store, polite, detail_budgets={ats: detail_budget for ats in DETAIL_BUDGET_ENV},
                                sleep=sleep, boards=boards, time_budget=max_minutes * 60, clock=clock)
    summary.update({"first_open": True, "requests": polite.requests,
                    "rate_limited": polite.rate_limited, "server_errors": polite.server_errors,
                    "cooling": polite.cooling()})
    return summary


def refresh_freehire_only(store: Watchlist, fetch=fetch_json, cap=None) -> dict:
    """A LIGHT refresh: pull ONLY the freehire bulk feed -- the board's free, keyless, high-volume,
    minute-fresh source -- and upsert it. This runs on a TIGHT cadence between the full refreshes so
    first_seen stays current and the board honestly reads "New Xm ago", WITHOUT re-hitting the dozens
    of third-party company ATS boards on every tick (those move slowly and stay on the 6h loop, and
    hammering them would be a bad-citizen move on GREEN-lane sources). freehire is keyless and free
    with no published rate limit, so there is no quota to burn -- but to stay a polite client we pull
    only the FRESHEST slice each tick (JOBS_FAST_ROWS, default 1500 -> ~15 requests at 100/page, not
    the full ~60). Newest-first, so a small cap still catches every brand-new posting. Never raises."""
    if cap is None:
        try:
            cap = max(100, int(os.environ.get("JOBS_FAST_ROWS", "1500")))
        except ValueError:
            cap = 1500
    fetched = matched = new_total = 0
    errors: list[dict] = []
    try:
        fjobs = freehire_bulk(fetch=fetch, cap=cap)
        fetched = len(fjobs)
        keep = [j for j in fjobs if is_intl_student_accessible(j)]
        matched = len(keep)
        new_total, _ = store.upsert_jobs(keep)
    except Exception as exc:                             # noqa: BLE001 - one blip mustn't kill the loop
        errors.append({"company": "freehire", "error": str(exc)})
    # Price the fresh rows so the min-pay filter + top-paid sort cover them immediately. Network-free.
    backfilled = 0
    try:
        from sourcing.ats import salary_from_text
        backfilled = store.backfill_salaries(salary_from_text)
    except Exception as exc:                             # noqa: BLE001 - backfill is best-effort
        errors.append({"company": "salary-backfill", "error": str(exc)})
    return {"fetched": fetched, "matched": matched, "new": new_total,
            "salary_backfilled": backfilled, "errors": errors}
