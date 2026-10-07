"""Local web UI for the resume agent, Stage 2 (wired to the real engine).

Local-first: binds to 127.0.0.1 only, no external requests. Wraps the existing
engine, intake questions, `draft_profile`, `assemble_cv`, the coverage report,
the flagged-title flow, and the SQLite profile store, behind a small JSON API.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intake.memory import ConversationMemory  # noqa: E402
from sourcing.service import (SUPPORTED_ATS, first_open_refresh,  # noqa: E402
                              refresh_watchlist, seed_if_empty)
from sourcing.sponsors import (SponsorDB, download_h1b_rows,  # noqa: E402
                               H1BYearUnavailable, H1B_REFRESH_NOTE,
                               h1b_default_fiscal_years, sync_h1b_snapshot,
                               looks_us, naics_industry, parse_perm_xlsx, parse_tabular_file)
from sourcing.watchlist import Watchlist  # noqa: E402
from intake.palace_memory import PalaceMemory  # noqa: E402
from ui.lock import Lock  # noqa: E402
from ui.records import CVRecords  # noqa: E402
from ui.session import WebIntake  # noqa: E402

app = Flask(__name__, static_folder="static", template_folder="templates")
# Reject oversized uploads before they're read (a résumé/transcript is small); this bounds
# any single request body and stops a huge file tying up extraction/OCR (413 instead).
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024   # 32 MB
# The app is local and served fresh from disk every launch, so nothing should be cached: the
# Electron shell was serving stale app.js/styles.css from its HTTP cache after a restart, so
# UI changes only appeared after a forced reload. Flask's default static handler sets a cache
# max-age; override it to 0 and stamp no-store on every response so a plain restart always
# shows the latest UI. (API routes already set no-store individually; this covers static/HTML.)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.after_request
def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp

# Data locations (env-overridable so an isolated instance can run without
# touching the user's real data, used for end-to-end testing).
# Resolve to an ABSOLUTE path: send_file() resolves a relative path against the app's
# root (ui/), not the process cwd, so a relative data dir would 404 on every download
# even though the file exists. Absolute keeps every derived path unambiguous.
_DATA = Path(os.environ.get("RESUME_AGENT_DATA_DIR") or (ROOT / "data")).resolve()
DB_PATH = str(_DATA / "resume_agent.db")
WORKDIR = _DATA / "cv_build"
PALACE_DIR = _DATA / "palace"   # durable verbatim + semantic memory (git-ignored)
UPLOADS_DIR = _DATA / "uploads"  # raw uploaded CVs, kept as a backup, not indexed
TEMPLATE = (ROOT / "config" / "resume_shetty.tex").read_text(encoding="utf-8")
_DEFAULT_TEMPLATE = "shetty"


def _templates() -> list[dict]:
    """Every installed CV template, from its manifest (config/templates/*.json). The
    selected template drives the CV's shape AND the intake questions."""
    import json as _json
    out = []
    tdir = ROOT / "config" / "templates"
    for p in sorted(tdir.glob("*.json")):
        try:
            m = _json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = m.get("name") or p.stem
        out.append({"name": name,
                    "display_name": m.get("display_name") or name,
                    # Grouped by WHO the template is for, not by how it looks. Every
                    # ATS-safe CV converges on the same single-column shape, so the real
                    # difference between them is section order and section set, i.e. the
                    # target. A "General" label is the honest answer for an uncategorised
                    # template; guessing a category from its filename would not be.
                    "category": m.get("category") or "General",
                    "best_for": m.get("best_for") or "",
                    "sections": m.get("sections") or [],
                    "required_sections": m.get("required_sections", None),
                    "preview_url": f"/api/templates/{name}/preview.pdf"})
    return out


def _candidate_seniority() -> int:
    """Rough years of experience from the SAVED profile: the span of the earliest
    to the latest year across the person's roles. 0 when there's no profile."""
    saved = ConversationMemory(DB_PATH).load("default") or {}
    ess = saved.get("essentials") or {}
    prof = saved.get("profile") or {}
    years: list[int] = []
    roles = 0
    for e in (ess.get("experience") or prof.get("experience") or []):
        for r in (e.get("roles") or [e]):
            roles += 1
            years += [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b",
                                                 str(r.get("dates") or ""))]
    span = (max(years) - min(years)) if years else 0
    return max(span, 3 if roles >= 3 else 0)


def _prefer_on_tie(cand: dict, cand_hits: list, incumbent: dict, incumbent_hits: list) -> bool:
    """Deterministic tie-break for :func:`_suggest_template` when two manifests score equally.

    Several senior-IC templates (``summary`` and ``experienced``) share the SAME generic
    seniority keywords (senior, lead, principal, staff, manager, director, years of experience,
    experienced, mid level), so a plain "Senior Principal Engineer" JD hits both equally and
    ties. Prefer the manifest that actually matched a keyword DISTINGUISHING it from the other
    (e.g. ``experienced``'s operations / supervisor / area-manager terms). When neither did, the
    match is solely due to shared generic keywords, so fall back to the more generic manifest,
    the one with the shorter ``suits`` list, which is ``summary`` (``experienced`` is the
    specialized ops/supervisor variant). Returns True if ``cand`` should replace ``incumbent``.
    """
    shared = ({s.lower() for s in (cand.get("suits") or [])}
              & {s.lower() for s in (incumbent.get("suits") or [])})
    cand_distinct = sum(1 for h in cand_hits if h.lower() not in shared)
    inc_distinct = sum(1 for h in incumbent_hits if h.lower() not in shared)
    if cand_distinct != inc_distinct:
        return cand_distinct > inc_distinct
    return len(cand.get("suits") or []) < len(incumbent.get("suits") or [])


def _suggest_template(jd_text: str) -> dict:
    """Pick the template whose target the JD reads like, with the reason shown.

    Deterministic keyword overlap against each manifest's ``suits``, NOT a model call:
    the person is one click from the picker and every preview is right there, so a slow,
    costly, non-reproducible answer would buy nothing. Returns the default with an empty
    reason when the JD matches nothing, because "I couldn't tell" is a real answer and
    better than a confident guess.

    The CANDIDATE weighs in too (obs #28): a "Recent Graduates" posting chose the
    education-first layout for an 8-year operations leader, underselling every year
    of it. When the saved profile shows real seniority, experienced-professional
    templates get a boost, the JD says what the job is, the profile says who's
    applying, and the layout must fit both.
    """
    import json as _json
    low = (jd_text or "").lower()
    try:
        seniority = _candidate_seniority()
    except Exception:
        seniority = 0
    best, best_score, best_hits = None, 0, []
    tdir = ROOT / "config" / "templates"
    for p in sorted(tdir.glob("*.json")):
        try:
            m = _json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        hits = [s for s in (m.get("suits") or []) if s.lower() in low]
        score = len(hits)
        if seniority >= 5 and (m.get("category") or "") == "Experienced professionals":
            score += 2
        if score > best_score:
            best, best_score, best_hits = m, score, hits
        elif best is not None and score == best_score and score > 0 \
                and _prefer_on_tie(m, hits, best, best_hits):
            best, best_hits = m, hits
    if not best or not best_score:
        return {"name": _DEFAULT_TEMPLATE, "reason": "", "matched": []}
    reason = best.get("best_for") or ""
    if seniority >= 5 and (best.get("category") or "") == "Experienced professionals":
        reason = (reason + f" Your profile shows ~{seniority} years of experience, "
                           "which should lead.").strip()
    return {"name": best.get("name") or _DEFAULT_TEMPLATE,
            "display_name": best.get("display_name") or "",
            "reason": reason,
            "matched": best_hits[:4]}


def _placeholder_profile() -> dict:
    """A NEUTRAL 'John Doe' profile used ONLY to render template PREVIEWS. It is a COMPLETE,
    full-page CV (never a half page) so the picker shows what a finished CV in this template
    actually looks like, with obviously-fake, generic content and no real person's details
    (no real-person data anywhere user-facing)."""
    return {
        "identity": {"name": "John Doe", "address": "123 Main Street, Anytown, ST 00000",
                     "phone": "(000) 000-0000", "email": "john.doe@example.com",
                     "linkedin": "https://www.linkedin.com/in/johndoe",
                     "github": "https://github.com/johndoe", "blog": "https://johndoe.example.com"},
        "summary": ("Senior software engineer with 8+ years building reliable, data-intensive "
                    "platforms across fintech and healthcare. Turns ambiguous requirements into "
                    "well-tested systems and partners closely with product and stakeholders to ship "
                    "outcomes that move the business. Deep in Python, cloud infrastructure, and API design."),
        "education": [
            {"school": "State University", "degree": "Master of Science in Computer Science",
             "date": "May 20XX", "location": "Anytown, ST",
             "courses": "Distributed Systems, Machine Learning, Databases, Algorithms, Cloud Computing, Software Architecture"},
            {"school": "State College", "degree": "Bachelor of Science in Information Systems",
             "date": "May 20XX", "location": "Anytown, ST",
             "courses": "Data Structures, Operating Systems, Web Development, Statistics, Computer Networks"},
        ],
        "skills": {
            "Languages & Tools": "Python, Go, JavaScript, TypeScript, SQL, Bash, Git, Docker, Kubernetes, Terraform",
            "Data & Cloud": "AWS, GCP, PostgreSQL, Redis, Kafka, Airflow, Spark, Snowflake, BigQuery",
            "Practices": "REST & gRPC API design, CI/CD, automated testing, observability, code review, agile delivery",
            "Domain": "Payments, risk, HIPAA-regulated data, data pipelines, platform reliability, cost optimization",
        },
        "projects": [
            {"org": "Open-Source Reconciliation Library", "location": "Anytown, ST", "title": "Maintainer",
             "dates": "20XX - Present", "link": "https://github.com/johndoe/reconcile",
             "bullets": ["Built an open-source library that matches millions of daily transactions and cut manual review time by roughly 80 percent."]},
            {"org": "Self-Serve Deployment Platform", "location": "Anytown, ST", "title": "Lead",
             "dates": "20XX - 20XX",
             "bullets": ["Designed an internal deployment tool adopted by 12 teams, reducing release lead time from days to under an hour."]},
            {"org": "Personal Finance Dashboard", "location": "Anytown, ST", "title": "Creator",
             "dates": "20XX - 20XX",
             "bullets": ["Built a full-stack dashboard with a Python backend and a React frontend to track spending and forecast monthly cash flow."]},
        ],
        "experience": [
            {"org": "Northwind Systems", "location": "Anytown, ST",
             "roles": [{"title": "Senior Software Engineer", "dates": "Jan 20XX - Present",
                        "bullets": ["Built and scaled the core ledger service handling billions in annual transaction volume with 99.98 percent uptime.",
                                    "Led the migration to an event-driven architecture, cutting settlement latency from hours to minutes across the platform.",
                                    "Partnered with product to define service-level objectives and instrument dashboards, improving incident response time by 40 percent.",
                                    "Mentored four engineers and set the team's testing, code-review, and on-call standards."]}]},
            {"org": "Cobalt Labs", "location": "Anytown, ST",
             "roles": [{"title": "Software Engineer", "dates": "Jun 20XX - Dec 20XX",
                        "bullets": ["Developed HIPAA-compliant data pipelines ingesting clinical records from more than 30 provider systems.",
                                    "Shipped a patient-facing API used by three mobile apps with 200,000 monthly active users.",
                                    "Cut nightly batch runtime in half by rewriting the aggregation layer and adding incremental processing."]}]},
            {"org": "Initech", "location": "Anytown, ST",
             "roles": [{"title": "Junior Developer", "dates": "Jul 20XX - May 20XX",
                        "bullets": ["Automated a manual reporting process, saving the operations team an estimated ten hours each week.",
                                    "Contributed features and fixes across the billing service as part of a five-person agile team.",
                                    "Wrote unit and integration tests that lifted coverage on the reporting module from 40 to 85 percent."]}]},
            {"org": "Datawave Analytics", "location": "Anytown, ST",
             "roles": [{"title": "Software Engineering Intern", "dates": "Jun 20XX - Aug 20XX",
                        "bullets": ["Prototyped a data-quality checker that flagged anomalies in ingestion pipelines before they reached production.",
                                    "Presented the results to the engineering team and shipped the checker as an internal service."]}]},
        ],
        "extracurricular": [
            {"title": "Volunteer Coding Mentor, Anytown Nonprofit", "date": "20XX - Present",
             "bullets": ["Run weekly workshops teaching programming fundamentals to students from underserved backgrounds."]},
        ],
        "interests": "Chess, distance running, open-source software, photography, jazz",
    }


def _load_template(name: str | None):
    """Return (tex_source, template_name) for a selected template, falling back to the
    default. A template with no readable .tex falls back too, so selection never breaks."""
    import json as _json
    name = (name or "").strip() or _DEFAULT_TEMPLATE
    manifest_path = ROOT / "config" / "templates" / f"{name}.json"
    try:
        m = _json.loads(manifest_path.read_text(encoding="utf-8"))
        tex_path = ROOT / (m.get("tex") or f"config/resume_{name}.tex")
        return tex_path.read_text(encoding="utf-8"), name
    except (OSError, ValueError):
        return TEMPLATE, _DEFAULT_TEMPLATE


# Single active builder session (a local, single-user app).
_SESSION: dict[str, WebIntake] = {}


def _memory() -> ConversationMemory:
    return ConversationMemory(DB_PATH)


def _watchlist() -> Watchlist:
    w = Watchlist(DB_PATH)
    seed_if_empty(w)   # first run gets a few real example companies
    return w


# One cached sponsor DB per data dir, it holds an in-memory index, so we reuse it.
_SPONSORS: dict[str, SponsorDB] = {}
# Optional override for the visa-sponsor store path. Default None -> derive from `_DATA` at call
# time (so `monkeypatch.setattr(app, "_DATA", ...)` still redirects the store, as before). A test
# can set this to an isolated, self-seeded DB and clear the `_SPONSORS` cache, so it never depends
# on whatever the ambient data/ dir happens to hold.
_SPONSORS_DB = None
# The bundled H-1B seed (~105k employers out of a 340k-row gzip) takes tens of seconds to merge.
# It used to run inside _sponsors() on whatever request thread opened the DB first, while every
# other request queued behind it. It now runs on ONE background thread per process (like the
# first-open crawl); the DB serves at once and tag_jobs simply shows no H-1B badge until the
# rows land. JOBS_SYNC_SEED=1 keeps the old synchronous load (tests, CLI). The installer ships
# a DB that already has the rows, so this mostly affects dev checkouts.
_H1B_SEED: dict = {"thread": None, "done": 0.0}
_H1B_SEED_LOCK = threading.Lock()
_SALARY_BACKFILLED = False       # one-time-per-process JD->salary backfill guard (see the feed route)

# First-open crawl. A fresh install whose shared feed is unreachable (or not configured yet) has
# an EMPTY local store and nothing scheduled to fill it, so the Jobs page sat on "0 live roles"
# forever. When the local path finds no jobs AND the feed is genuinely absent (_first_crawl_allowed),
# it starts ONE background crawl -- the small, polite first_open_refresh (top boards by H-1B volume,
# per-host spacing, a wall-clock cap), not the full watchlist -- and tells the client, which
# re-polls until rows arrive. One crawl per process; never while another is running. A crawl
# that RAISED (the boards unreachable, a bug) is not "done": it is retried after a back-off
# (CRAWL_RETRY_AFTER, doubling per failure, capped at CRAWL_RETRY_CAP), and the Jobs route
# reports the failure as `crawl_error` while the store is still empty, so a fresh install never
# sits on a silent empty board for the life of the process (seen in review 2026-10-01).
_LOCAL_CRAWL: dict = {"thread": None, "started": 0.0, "done": 0.0,
                      "failed": 0.0, "failures": 0, "error": ""}
_LOCAL_CRAWL_LOCK = threading.Lock()
CRAWL_RETRY_AFTER = 10 * 60
CRAWL_RETRY_CAP = 60 * 60
# A configured feed must have been failing this long, with nothing cached, before a fresh install
# crawls the boards itself. One failed download is a blip, not a reason for every new install to
# hit the ATS hosts at once.
FIRST_CRAWL_AFTER_FAILING = 30 * 60


def _local_crawl_running() -> bool:
    t = _LOCAL_CRAWL.get("thread")
    return bool(t is not None and t.is_alive())


def _crawl_retry_after(failures: int) -> float:
    """Seconds to wait before retrying the first-open crawl after `failures` consecutive
    failures: 10 minutes, doubling, never more than an hour."""
    return float(min(CRAWL_RETRY_AFTER * 2 ** max(int(failures) - 1, 0), CRAWL_RETRY_CAP))


def _crawl_retry_due() -> bool:
    """Has the last failed first-open crawl's back-off elapsed? False when it never failed
    (a crawl that finished cleanly is not run again this process; the hourly refresh and the
    next launch take over) or when the wait is still running."""
    failed = _LOCAL_CRAWL.get("failed") or 0.0
    if not failed:
        return False
    return time.time() - failed >= _crawl_retry_after(_LOCAL_CRAWL.get("failures") or 1)


def _crawl_error() -> str:
    """The last first-open crawl's failure, in one short line, or "" when it did not fail (or
    has not run). Surfaced by the Jobs route while the store is still empty."""
    if not _LOCAL_CRAWL.get("failed"):
        return ""
    return _LOCAL_CRAWL.get("error") or "the first job-board crawl failed"


# A configured feed that downloads fine but holds ZERO jobs (an empty jobs.json.gz published by
# a bad central crawl, a freshly created bucket) is a "cache" in name only: nothing to show and,
# because the download succeeds, no failure streak either. The Jobs route records when it first
# saw the live list empty (cleared the moment rows come back) and the first-open crawl treats
# that streak like a download-failure streak, so the install is not left on an empty board.
_FEED_EMPTY: dict = {"since": 0.0}


def _note_feed_empty(empty: bool) -> None:
    if not empty:
        _FEED_EMPTY["since"] = 0.0
    elif not _FEED_EMPTY.get("since"):
        _FEED_EMPTY["since"] = time.time()
        print("[feed] the configured feed holds zero jobs; serving the local board instead")


def _feed_empty_for() -> float:
    since = _FEED_EMPTY.get("since") or 0.0
    return (time.time() - since) if since else 0.0


def _first_crawl_allowed(feed_url: str | None) -> bool:
    """May this install crawl the boards itself? Only when no feed will fill the board: no feed
    configured (unset, or the unbought-domain placeholder); or a configured feed whose downloads
    have been failing for FIRST_CRAWL_AFTER_FAILING with nothing cached at all; or one that has
    been serving an EMPTY list for that long (see _FEED_EMPTY). While a configured feed's first
    download is in flight the board shows "fetching jobs" instead of crawling."""
    if feed_url is None:
        return True
    sf = _static_feed()
    if not sf.has_cache() and sf.failing_for() >= FIRST_CRAWL_AFTER_FAILING:
        return True
    return _feed_empty_for() >= FIRST_CRAWL_AFTER_FAILING


def _kick_local_crawl() -> bool:
    """Start the first-open crawl unless one is running or already ran this process.

    JOBS_FIRST_CRAWL=0 disables it (the test suite sets this; a maintainer running the engine
    on a laptop that should never crawl the boards itself can too)."""
    if os.environ.get("JOBS_FIRST_CRAWL", "1") == "0":
        return False
    with _LOCAL_CRAWL_LOCK:
        if _local_crawl_running():
            return True
        if _LOCAL_CRAWL.get("started") and not _crawl_retry_due():
            return False

        def run():
            w = _watchlist()
            try:
                summary = first_open_refresh(w)
                print(f"[first-open crawl] boards={summary.get('boards_crawled')}/"
                      f"{summary.get('boards_total')} new={summary.get('new')} "
                      f"requests={summary.get('requests')} rate_limited={summary.get('rate_limited')} "
                      f"stopped_early={summary.get('stopped_early')}")
                _sponsors().set_meta("jobs_refreshed_at", _now_iso())
                _LOCAL_CRAWL.update({"failed": 0.0, "failures": 0, "error": ""})
            except Exception as exc:                 # noqa: BLE001 - a failed crawl must not kill the app
                traceback.print_exc()
                # Remember the failure so the route can say so and retry after the back-off,
                # instead of treating a crawl that blew up as one that ran.
                failures = (_LOCAL_CRAWL.get("failures") or 0) + 1
                _LOCAL_CRAWL.update({"failed": time.time(), "failures": failures,
                                     "error": f"{type(exc).__name__}: {exc}"[:200].strip()})
                print(f"[first-open crawl] failed ({failures}); retry in "
                      f"{int(_crawl_retry_after(failures) // 60)} min")
            finally:
                w.close()
                _LOCAL_CRAWL["done"] = time.time()

        t = threading.Thread(target=run, name="first-open-crawl", daemon=True)
        _LOCAL_CRAWL["thread"] = t
        _LOCAL_CRAWL["started"] = time.time()
        t.start()
        return True
_ENRICH_MISS: set = set()        # source_ids whose full JD freehire doesn't carry -> don't re-search
_LOCAL_BENCH: tuple[float, dict] | None = None   # (built_at, {family: (median, count)}) for pay insight


_STATIC_FEEDS: dict = {}          # (base_url, data dir) -> StaticFeed, one per process (holds the list)
_STATIC_FEEDS_LOCK = threading.Lock()   # Flask is threaded: never build two readers for one key


def _feed_base_url() -> str | None:
    """The static feed's base URL (JOBS_FEED_URL), or None when unset or still the unbought-domain
    placeholder (`tailor.example`): a `.example` host can never resolve, so a fresh install runs on
    its local crawl instead of waiting on a lookup that cannot succeed."""
    from sourcing.feedclient import is_placeholder
    u = (os.environ.get("JOBS_FEED_URL") or "").strip()
    return None if (not u or is_placeholder(u)) else u


def _feed_serving() -> bool:
    """Is the static feed where the board's rows come from (a feed is configured)? The detail /
    tailor paths then resolve a clicked row from the feed's cached list, its JD from the shard or,
    failing that, from the company's own public board endpoint (fetch_board_jd)."""
    return bool(_feed_base_url())


def _static_feed():
    """This install's reader for the static feed, cached under the data dir (docs/feed.md). It
    downloads jobs.json.gz at most hourly (ETag-conditional, per-install minute offset) and the JD
    shards on demand; all filtering happens here, locally. With no feed configured it never
    downloads anything."""
    from sourcing.feedclient import StaticFeed
    key = (_feed_base_url(), str(_DATA))
    with _STATIC_FEEDS_LOCK:
        sf = _STATIC_FEEDS.get(key)
        if sf is None:
            sf = StaticFeed(key[0], _DATA / "feed_cache")
            _STATIC_FEEDS[key] = sf
    return sf


def _facets(a) -> dict:
    """The board's facet query params in the shape sourcing.filters.apply_facets takes. Shared by
    the static-feed and local paths so both filter identically."""
    return dict(q=a.get("q", ""), loc=a.get("loc", ""), days=a.get("days", 0),
                remote=a.get("remote", ""),
                visa=[v for v in a.get("visa", "").split(",") if v], level=a.get("level", ""),
                pay=a.get("pay", 0), sort=a.get("sort", ""), sponsored=a.get("sponsored", ""))


def _local_benchmarks() -> dict:
    """Board-wide pay medians per role family for the LOCAL (no-kitchen) path, cached on a slow TTL
    so a job-open never triggers a full-board scan more than once every few minutes."""
    global _LOCAL_BENCH
    now = time.time()
    if _LOCAL_BENCH is None or (now - _LOCAL_BENCH[0]) >= 600:
        from sourcing.filters import salary_benchmarks
        w = _watchlist()
        try:
            jobs = w.list_jobs(order="recent", limit=15000)
        finally:
            w.close()
        _LOCAL_BENCH = (now, salary_benchmarks(jobs))
    return _LOCAL_BENCH[1]


def _saved_ids() -> set:
    """The set of bookmarked source_ids, to overlay `saved` onto a kitchen-sourced feed."""
    w = _watchlist()
    try:
        return w.saved_ids()
    finally:
        w.close()


def _saved_jobs_view():
    """The Saved list: the user's bookmarked roles, from the LOCAL snapshot store, with the same
    facets + pagination the main feed uses (so search/filter work inside Saved too)."""
    from sourcing.filters import apply_facets, paginate
    from sourcing.quality import is_entry_level
    a = request.args
    w = _watchlist()
    try:
        jobs = _sponsors().tag_jobs(w.list_jobs(order="recent", saved_only=True, limit=5000))
    finally:
        w.close()
    for j in jobs:
        j["entry_level"] = is_entry_level(j.get("title", ""))
        j["saved"] = True
    facets = dict(q=a.get("q", ""), loc=a.get("loc", ""), days=a.get("days", 0),
                  remote=a.get("remote", ""),
                  visa=[v for v in a.get("visa", "").split(",") if v], level=a.get("level", ""),
                  pay=a.get("pay", 0), sort=a.get("sort", ""))
    filtered = apply_facets(jobs, **facets)
    page_jobs, count, page, per_page = paginate(filtered, a.get("page", 1), a.get("per_page", 30))
    return jsonify({"jobs": page_jobs, "count": count, "total": len(jobs), "page": page,
                    "per_page": per_page, "source": "saved", "saved_view": True})


def _seed_sponsors(target: Path) -> None:
    """First run: copy the shipped visa-sponsor snapshot into the person's data dir.

    Without this a fresh install had ZERO sponsor data (H-1B arrives only via the Update
    button, PERM/E-Verify only via manual file import), so a buyer's first LinkedIn session
    showed "No sponsor record" on every job. Found the hard way: the dev data dir held
    582,145 employers while the packaged app's own data dir held 0.

    Replaces an EMPTY database too, not just a missing one: the packaged app had already
    created a zero-row sponsors.db on its first launch, so "seed only if the file is
    absent" would have skipped exactly the machines that need it. A populated database is
    never touched; the person's newer imports always win over our snapshot.
    """
    seed = ROOT / "seed" / "sponsors.db"
    if not seed.exists():
        return                                   # dev checkout: no snapshot staged
    try:
        if target.exists():
            existing = SponsorDB(target)
            try:
                if existing.count() > 0:
                    return                       # real data present: never overwrite
            finally:
                existing.close()
        import shutil
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed, target)
    except Exception:
        # A failed seed must never block startup: the app still works, the person can
        # still click Update visa data, and the badges just start empty as before.
        traceback.print_exc()


def _sponsors() -> SponsorDB:
    key = str(_SPONSORS_DB) if _SPONSORS_DB is not None else str(_DATA / "sponsors.db")
    db = _SPONSORS.get(key)
    if db is None:
        _seed_sponsors(Path(key))
        db = SponsorDB(key)
        # Load the PERM / E-Verify snapshots that SHIP WITH THE APP the first time the
        # database is opened empty. They were only ever loaded inside the "Update visa
        # data" button, so out of the box every job read "no sponsor record" -- worst for
        # exactly the person the badges exist for, who has no way to know the data is
        # sitting unread in config/. Both files ingest in under three seconds
        # (~509k employers), so there is no reason to make it a manual step.
        try:
            if db.is_empty():
                _load_bundled_perm(db)
                _load_bundled_everify(db)
            # H-1B is the headline badge, and a packaged install once shipped with ZERO
            # H-1B rows (the staged dev DB had none). The committed seed carries ~105k
            # H-1B employers, so a database without any gets them here, the same way PERM
            # and E-Verify load from their bundles -- but in the background (see _H1B_SEED):
            # the merge takes tens of seconds and must not hold a request thread.
            _start_bundled_h1b(db)
        except Exception:
            traceback.print_exc()   # badges degrade to empty; never block the app
        _SPONSORS[key] = db
    return db


def _h1b_seed_running() -> bool:
    t = _H1B_SEED.get("thread")
    return bool(t is not None and t.is_alive())


def _join_h1b_seed(timeout: float | None = None) -> None:
    """Wait for the background bundled-H-1B load, if one is running (tests; and the visa-data
    refresh, which must not merge the seed a second time alongside it)."""
    t = _H1B_SEED.get("thread")
    if t is not None and t.is_alive():
        t.join(timeout)


def _start_bundled_h1b(db) -> None:
    """Merge the bundled H-1B seed into `db` if it has no H-1B rows: synchronously under
    JOBS_SYNC_SEED=1, otherwise on one background thread per process. A second call while a
    load is running does nothing (the load is a MAX-merge, so even a repeat would not double
    counts, but there is no reason to read the 340k-row gzip twice)."""
    if os.environ.get("JOBS_SYNC_SEED", "") == "1":
        _load_bundled_h1b(db)
        return
    with _H1B_SEED_LOCK:
        if _h1b_seed_running():
            return

        def run():
            try:
                n = _load_bundled_h1b(db)
                if n:
                    print(f"[sponsors] bundled H-1B seed loaded: {n} employers")
            except Exception:                        # noqa: BLE001 - badges stay empty; app lives
                traceback.print_exc()
            finally:
                _H1B_SEED["done"] = time.time()

        t = threading.Thread(target=run, name="bundled-h1b-seed", daemon=True)
        _H1B_SEED["thread"] = t
        t.start()


def _records() -> CVRecords:
    return CVRecords(DB_PATH)


def _under_test_or_dev() -> bool:
    """True only inside the pytest suite or an explicit developer session. The
    offline FakeLLM is a TEST DOUBLE, it must never back a real (shipped) run, so
    it's honored only here, never in production."""
    return ("PYTEST_CURRENT_TEST" in os.environ
            or "pytest" in sys.modules
            or os.environ.get("RESUME_AGENT_DEV") == "1")


_AUTO_UPDATER = None


def start_auto_updater():
    """Launch the background auto-updater so the job feed stays fresh on its own (refresh +
    prune, plus periodic board discovery when available). OFF unless RESUME_AGENT_AUTOUPDATE=1,
    and never during tests/dev, so it only runs where it's meant to (a real local launch, or a
    central service that many users pull from). Safe to call more than once. See
    sourcing/scheduler.py."""
    global _AUTO_UPDATER
    if _AUTO_UPDATER is not None or os.environ.get("RESUME_AGENT_AUTOUPDATE") != "1" \
            or _under_test_or_dev():
        return None
    from sourcing.scheduler import AutoUpdater
    from sourcing.service import refresh_freehire_only as _fast_refresh
    from sourcing.service import refresh_watchlist as _refresh

    # Weekly discovery runs the SAME ranked-sponsor crawl the committed seed was grown with
    # (sourcing/growth.py): a bounded slice of the next-best un-probed H-1B sponsors per pass,
    # remembered in a checkpoint beside the DB so the watchlist keeps growing after launch
    # instead of re-probing the top of the list forever. Wired in only if present, so the
    # updater gracefully runs refresh+prune even where the crawler hasn't shipped yet.
    discover_fn = None
    try:
        from sourcing.growth import discover_sponsors

        def discover_fn():                       # noqa: E306 - small closure over app helpers
            w = Watchlist(DB_PATH)
            try:
                return discover_sponsors(
                    w, _DATA / "discover_checkpoint.jsonl",
                    budget=int(os.environ.get("JOBS_DISCOVER_BUDGET", "300")))
            finally:
                w.close()
    except Exception:                            # noqa: BLE001 - discovery is optional
        discover_fn = None

    _AUTO_UPDATER = AutoUpdater(_watchlist, lambda w: _refresh(w), discover_fn=discover_fn,
                               fast_fn=lambda w: _fast_refresh(w))
    _AUTO_UPDATER.start()
    return _AUTO_UPDATER


def _require_connectivity(host: str = "api.anthropic.com", port: int = 443,
                          timeout: float = 4.0) -> None:
    """Fail loudly if there's no internet. SponsorJobs is an online tool, it uses the
    real model to tailor CVs and to find/apply to jobs, so it must not run offline."""
    import socket
    try:
        socket.create_connection((host, port), timeout=timeout).close()
    except OSError as exc:
        raise Unavailable(
            "offline",
            "SponsorJobs needs an internet connection to run. It uses the real AI model to "
            "tailor your resume and to find and apply to jobs, so it can't work offline. "
            "Reconnect and try again."
        ) from exc


_PROVIDERS = ("anthropic", "openai")
_PROVIDER_LABEL = {"anthropic": "Anthropic", "openai": "OpenAI"}
_PROVIDER_KEY = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def _ai_provider() -> str:
    """Which model provider the user chose. Claude is the recommended default; OpenAI is the
    bring-your-own-OpenAI-key alternative. Stored as AI_PROVIDER in the local creds file."""
    p = (_cred("AI_PROVIDER") or "anthropic").strip().lower()
    return p if p in _PROVIDERS else "anthropic"


def _make_llm():
    """The backend for the app's AI.

    By default Claude is BUNDLED: calls route through SponsorJobs' managed broker (the company key,
    metered per plan), so the user needs NO key of their own. The 'local-first / no upload' promise
    still holds -- the broker is a thin, stateless pass-through for the paid AI only; nothing is
    stored server-side (see docs/bundled-api-backend.md). Two ways to run on your OWN key instead
    (bring-your-own): choose OpenAI as the provider, or set TAILOR_OWN_KEY=1 with an Anthropic key
    saved. If the managed broker is unreachable we fall back to a saved key when there is one.
    FakeLLM is a test double, allowed ONLY under pytest/dev, never shipped."""
    if os.environ.get("RESUME_AGENT_FAKE_LLM") == "1" and _under_test_or_dev():
        from llm.base import FakeLLM
        return FakeLLM()
    provider = _ai_provider()
    if provider == "openai":
        return _make_byo_llm("openai")   # OpenAI stays BYO; bundling is Anthropic-only for now
    # Claude: bundled through the broker unless the user opted into their own key. Fall back to a
    # saved key if they asked for it, or if the broker is down and they have one to run on.
    own_key = _cred("ANTHROPIC_API_KEY")
    prefer_own = os.environ.get("TAILOR_OWN_KEY") == "1" and bool(own_key)
    if not prefer_own and _broker_reachable():
        from llm.broker_client import BrokerLLM
        return BrokerLLM(model=os.environ.get("RESUME_AGENT_MODEL"), auth_token=_account_token())
    if not own_key:
        raise Unavailable(
            "no_key",
            # The managed AI is not live yet, so a new user simply has no key: say that plainly
            # (the UI opens the key box on this reason), not that a service is down.
            "Connect your AI key to write. It stays on this computer.")
    return _make_byo_llm("anthropic")


def _make_byo_llm(provider: str):
    """Bring-your-own-key path: call the provider directly with the user's saved key. Requires a
    connection and the key; fails loudly if either is missing. Reads the key through `_cred`, so a
    key held by App Lock (encrypted at rest) is found when unlocked."""
    key = _cred(_PROVIDER_KEY[provider])
    if not key:
        raise Unavailable(
            "no_key",
            f"Connect your {_PROVIDER_LABEL[provider]} key to write. It stays on this computer.")
    _require_connectivity()
    if provider == "openai":
        from llm.openai_client import OpenAILLM
        return OpenAILLM(model=os.environ.get("RESUME_AGENT_OPENAI_MODEL"), api_key=key)
    from llm.anthropic_client import AnthropicLLM
    return AnthropicLLM(model=os.environ.get("RESUME_AGENT_MODEL"), api_key=key)


def _count_package(llm):
    """One tailoring run is one "package" (docs/bundled-api-backend.md, Pricing). On the bundled
    AI the broker counts it BEFORE any model call: the free plan's 3 a month, or the active
    pass's balance. Returns the LLM to run this package on:

      * own key (BYOK) or OpenAI: never counted, unlimited -> the same llm;
      * bundled and allowed -> the same llm;
      * bundled and used up, with an Anthropic key saved -> that key (BYOK keeps tailoring
        unlimited on free);
      * bundled and used up, no key -> BrokerUnavailable('upgrade_required'), which the UI turns
        into the passes offer.

    A broker that is too old to know the route (404) or briefly unreachable never blocks a run;
    the per-call token cap on the broker is the safety net either way."""
    from llm.broker_client import BrokerLLM, BrokerUnavailable
    if not isinstance(llm, BrokerLLM):
        return llm
    st, data = _broker_post("/llm/package", {})
    if st != 402:
        return llm
    if _cred("ANTHROPIC_API_KEY"):
        return _make_byo_llm("anthropic")
    tier = str((data or {}).get("tier") or "free")
    msg = ("You've used the 3 free tailored packages this month. A Job Hunt Pass or a Season Pass "
           "gives you many more, or add your own AI key for unlimited tailoring."
           if tier == "free" else
           "Your pass's tailored packages are used up. Add another pass, or your own AI key for "
           "unlimited tailoring.")
    raise BrokerUnavailable(msg, "upgrade_required")


_BROKER_UP: dict = {}            # base url -> (checked_at, up): one probe a minute, not one per call


def _broker_reachable() -> bool:
    """Is the managed broker up, so a saved-key BYO fallback is only used when it is not? Any
    error (refused, timeout, non-200) means 'not available'. The answer is remembered for a minute:
    in the official edition the broker is on the internet, and probing it before EVERY AI call added
    up to two seconds to each. The official edition also requires the broker to report a real
    model, never a test double serving placeholder text."""
    import json as _json
    import urllib.request
    base = os.environ.get("TAILOR_BROKER_URL", "http://127.0.0.1:57001").rstrip("/")
    hit = _BROKER_UP.get(base)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    up = False
    try:
        from llm.broker_client import BROKER_USER_AGENT
        req = urllib.request.Request(base + "/health", headers={"User-Agent": BROKER_USER_AGENT})
        with urllib.request.urlopen(req, timeout=2) as r:   # nosec - our own broker
            up = getattr(r, "status", 200) == 200
            if up and os.environ.get("TAILOR_EDITION") == "official":
                up = bool((_json.loads(r.read() or b"{}") or {}).get("real_providers"))
    except Exception:   # noqa: BLE001 - unreachable / any error => treat as not available
        up = False
    _BROKER_UP[base] = (time.time(), up)
    return up


# Local-only, git-ignored credential store. Overridable for tests/isolation.
_CRED_FILE = Path(os.environ.get("RESUME_AGENT_CRED_FILE") or (ROOT / "config" / "credentials.env"))

# App Lock (optional passcode). When ON it takes custody of the API key (encrypted at rest,
# only in memory while unlocked) and the engine refuses data/AI routes until unlocked. When
# OFF (the default), nothing below changes and creds come from credentials.env as before.
_LOCK = Lock(_DATA / "lock.json")


def _file_cred(name: str) -> str:
    """Read a credential ONLY from the git-ignored config/credentials.env (never the lock
    or env). Used by migration to find plaintext secrets that still need sealing."""
    if _CRED_FILE.exists():
        for line in _CRED_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, val = line.partition("=")
            if k.strip() == name:
                return val.strip().strip('"').strip("'")
    return ""


def _cred(name: str) -> str:
    """Read a credential from the environment or the git-ignored config/credentials.env.

    When App Lock is enabled it holds custodied secrets encrypted, so read from the lock
    (available only while unlocked). If the lock doesn't hold it yet (an existing setup on
    the first run after custody expanded, or a value not migrated), fall through to the
    plaintext file so nothing breaks; migration then sweeps it into the lock on unlock."""
    v = os.environ.get(name)
    if v:
        return v
    if _LOCK.custodies(name) and _LOCK.enabled():
        sealed = _LOCK.cred(name)
        if sealed:
            return sealed
        # not sealed yet (or locked) -> fall through to the plaintext file below
    return _file_cred(name)


def _save_cred(name: str, value: str) -> None:
    """Upsert a credential into the git-ignored credentials file, never the repo.

    When App Lock owns this credential (the API key) and is unlocked, re-seal it INTO the
    encrypted lock instead of writing plaintext, so a changed key stays protected at rest."""
    if _LOCK.custodies(name) and _LOCK.enabled() and _LOCK.unlocked():
        _LOCK.update_cred(name, value)
        return
    _CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    new_line, out, found = f"{name}={value}", [], False
    if _CRED_FILE.exists():
        for line in _CRED_FILE.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key == name and not line.strip().startswith("#"):
                out.append(new_line); found = True
            else:
                out.append(line)
    if not found:
        out.append(new_line)
    _CRED_FILE.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")


def _remove_cred(name: str) -> None:
    """Delete a credential line from the plaintext creds file. Used when App Lock takes the
    API key into encrypted custody, so no plaintext copy lingers on disk."""
    if not _CRED_FILE.exists():
        return
    out = [line for line in _CRED_FILE.read_text(encoding="utf-8").splitlines()
           if not (("=" in line) and line.split("=", 1)[0].strip() == name
                   and not line.strip().startswith("#"))]
    _CRED_FILE.write_text(("\n".join(out).rstrip("\n") + "\n") if any(out) else "",
                          encoding="utf-8")


def _revoke_cred(name: str) -> None:
    """FULLY remove a credential (a Disconnect action): drop the sealed copy from App Lock
    if it holds it, AND remove any plaintext line. Distinct from _remove_cred (plaintext
    only, used by migration AFTER sealing). Without the lock drop, disconnecting a secret
    while App Lock is on was a no-op: the sealed copy stayed and _cred() still returned it."""
    if _LOCK.custodies(name) and _LOCK.enabled() and _LOCK.unlocked():
        _LOCK.update_cred(name, "")   # delete the sealed copy from the lock
    _remove_cred(name)                # and any plaintext line


def _migrate_plaintext_creds_into_lock() -> None:
    """Sweep any lock-custodied secret still sitting in the plaintext creds file into the
    encrypted lock, then scrub the plaintext copy. Runs after a successful unlock, so an
    existing setup (App Lock enabled before custody expanded to Telegram/GitHub/etc.) gets
    those secrets protected with no passcode re-prompt and no plaintext left behind."""
    if not (_LOCK.enabled() and _LOCK.unlocked()):
        return
    for name in _LOCK.custody_names():
        if _LOCK.cred(name):           # already sealed in the lock
            if _file_cred(name):       # ...but a stale plaintext copy lingers -> scrub it
                _remove_cred(name)
            continue
        val = _file_cred(name)         # a plaintext copy still on disk?
        if val:
            _LOCK.update_cred(name, val)   # seal it into the encrypted lock
            _remove_cred(name)             # remove the plaintext line


# ---- Gmail OAuth token custody --------------------------------------------------------
# The token grants mailbox read access and lives in its own JSON file (not the creds file),
# refreshed by the OAuth flow. When App Lock owns it, keep it ENCRYPTED in the lock and hold
# no plaintext file. We inject this into inbox/gmail via set_token_store so that module stays
# unaware of the lock.
_GMAIL_TOKEN = "GMAIL_TOKEN"


def _gmail_load_token() -> str | None:
    """The Gmail token JSON: from the lock when it holds it (unlocked), else the plaintext
    file (an existing setup not migrated yet). None when there is no token at all."""
    from inbox.gmail import _TOKEN_PATH
    if _LOCK.custodies(_GMAIL_TOKEN) and _LOCK.enabled():
        sealed = _LOCK.cred(_GMAIL_TOKEN)
        if sealed:
            return sealed
        # not migrated yet (or locked) -> fall through to the file
    return _TOKEN_PATH.read_text(encoding="utf-8") if _TOKEN_PATH.exists() else None


def _gmail_save_token(token_json: str) -> None:
    """Persist a new/refreshed Gmail token: sealed into the lock when it has custody and is
    unlocked (no plaintext copy), otherwise the plaintext file (default behavior)."""
    from inbox.gmail import _TOKEN_PATH
    if _LOCK.custodies(_GMAIL_TOKEN) and _LOCK.enabled() and _LOCK.unlocked():
        _LOCK.update_cred(_GMAIL_TOKEN, token_json)
        _TOKEN_PATH.unlink(missing_ok=True)      # never leave a plaintext copy
        return
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_PATH.write_text(token_json, encoding="utf-8")


def _migrate_gmail_token_into_lock() -> None:
    """Seal a plaintext gmail_token.json into the lock on unlock, then delete the file (the
    same one-time, no-reprompt migration the creds file gets)."""
    if not (_LOCK.enabled() and _LOCK.unlocked()):
        return
    from inbox.gmail import _TOKEN_PATH
    if _LOCK.cred(_GMAIL_TOKEN):                  # already sealed
        _TOKEN_PATH.unlink(missing_ok=True)       # scrub any stale plaintext copy
        return
    if _TOKEN_PATH.exists():
        _LOCK.update_cred(_GMAIL_TOKEN, _TOKEN_PATH.read_text(encoding="utf-8"))
        _TOKEN_PATH.unlink(missing_ok=True)


# Install the lock-aware token store into the Gmail module (once, at import).
try:
    from inbox import gmail as _gmail_mod
    _gmail_mod.set_token_store(_gmail_load_token, _gmail_save_token)
except Exception:   # inbox module optional at import time; never block app startup
    pass


def _validate_anthropic_key(key: str) -> tuple[bool, str]:
    """A cheap 1-token round-trip that confirms the key authenticates AND has credit.
    Returns (ok, human_message), the message names the exact fix on failure."""
    key = (key or "").strip()
    if not key.startswith("sk-"):
        return False, "That doesn't look like an Anthropic key, they start with “sk-”."
    try:
        import anthropic
    except ImportError:
        return False, "The 'anthropic' package isn't installed on this machine."
    try:
        anthropic.Anthropic(api_key=key).messages.create(
            model=os.environ.get("RESUME_AGENT_MODEL", "claude-sonnet-5-5"),
            max_tokens=1, messages=[{"role": "user", "content": "hi"}])
        return True, ""
    except Exception as exc:  # map auth / billing / rate-limit / connectivity to a clear message
        status = getattr(exc, "status_code", None)   # anthropic SDK errors carry this
        name, low = type(exc).__name__.lower(), str(exc).lower()
        is_auth = status == 401 or "authentication" in name or "401" in low or "invalid x-api-key" in low
        is_billing = (status in (402, 403) or "credit" in low or "billing" in low
                      or "quota" in low or "402" in low)
        # A momentary 429/5xx/overload is NOT an auth failure, a bad key returns 401. The key
        # reached far enough to be throttled, so don't block a valid key behind a transient blip;
        # any real billing problem still surfaces on the first tailoring call.
        is_transient = (status in (429, 500, 502, 503, 504, 529)
                        or "overloaded" in low or "rate limit" in low or "rate_limit" in low)
        if is_auth:
            return False, "That key was rejected, double-check you copied the whole thing."
        if is_billing and not is_transient:
            return False, ("The key works, but your Anthropic account needs credit. Add $5 at "
                           "console.anthropic.com → Billing, then verify again.")
        if is_transient:
            return True, ""   # accept optimistically; the key authenticated, Anthropic was just busy
        return False, "Couldn't reach Anthropic to verify the key. Check your connection and retry."


def _validate_openai_key(key: str) -> tuple[bool, str]:
    """The OpenAI equivalent: a cheap 1-token round-trip confirming the key authenticates and
    has quota. Returns (ok, human_message)."""
    key = (key or "").strip()
    if not key.startswith("sk-"):
        return False, "That doesn't look like an OpenAI key, they start with “sk-”."
    try:
        import openai
    except ImportError:
        return False, "The 'openai' package isn't installed on this machine."
    try:
        openai.OpenAI(api_key=key).chat.completions.create(
            model=os.environ.get("RESUME_AGENT_OPENAI_MODEL", "gpt-4o"),
            max_tokens=1, messages=[{"role": "user", "content": "hi"}])
        return True, ""
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        low = str(exc).lower()
        is_auth = status == 401 or "incorrect api key" in low or "invalid_api_key" in low \
            or "authentication" in low
        is_billing = "insufficient_quota" in low or "exceeded your current quota" in low \
            or (status == 429 and "quota" in low) or status == 402
        is_transient = status in (429, 500, 502, 503, 504) or "rate limit" in low or "overloaded" in low
        if is_auth:
            return False, "That key was rejected, double-check you copied the whole thing."
        if is_billing and not is_transient:
            return False, ("The key works, but your OpenAI account needs credit. Add billing at "
                           "platform.openai.com → Billing, then verify again.")
        if is_transient:
            return True, ""
        return False, "Couldn't reach OpenAI to verify the key. Check your connection and retry."


def _validate_key(provider: str, key: str) -> tuple[bool, str]:
    return _validate_openai_key(key) if provider == "openai" else _validate_anthropic_key(key)


def _make_bot():
    """Build the Telegram bot from local credentials, or fail with a clear message."""
    from notify.telegram import TelegramBot
    token = _cred("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Telegram isn't set up. Create a bot with @BotFather, then add "
            "TELEGRAM_BOT_TOKEN (and your TELEGRAM_CHAT_ID) to config/credentials.env.")
    return TelegramBot(token, chat_id=_cred("TELEGRAM_CHAT_ID"))


class _RecordActions:
    """Adapter the Telegram command loop calls into, reads/writes the application queue.

    ``approve`` marks a package applied (like the review screen's 'Mark as applied'); it
    does NOT submit, submission still follows the per-site policy where the person acts."""

    def pending(self) -> list[dict]:
        recs = _records()
        try:
            rows = recs.list()
        finally:
            recs.close()
        out = []
        for r in rows:
            if (_record_data(r).get("status") or "ready") == "ready":
                out.append({"id": r["id"], "role": r.get("role") or "",
                            "company": r.get("company") or ""})
        return out

    def approve(self, rid: int) -> dict | None:
        recs = _records()
        try:
            rec = recs.get(rid)
            if not rec:
                return None
            recs.merge_data(rid, {"status": "applied", "applied_at": _now_iso()})
        finally:
            recs.close()
        return {"id": rid, "role": rec.get("role") or "", "company": rec.get("company") or ""}

    def status(self, rid: int) -> dict | None:
        recs = _records()
        try:
            rec = recs.get(rid)
        finally:
            recs.close()
        if not rec:
            return None
        return {"id": rid, "role": rec.get("role") or "", "company": rec.get("company") or "",
                "status": _record_data(rec).get("status") or "ready"}

    # -- P3: run control from Telegram (pause/skip/handle + free-text chat) -- #
    def pause(self) -> None:
        _control().set_paused(True)

    def resume(self) -> None:
        _control().set_paused(False)

    def skip(self, rid=None) -> dict | None:
        """No id -> skip the current in-progress item (a one-shot flag the loop consumes). An id ->
        mark that queued application skipped so the engine and the review/submit path leave it."""
        _control().request_skip(rid)
        if rid is None:
            return {"current": True}
        recs = _records()
        try:
            rec = recs.get(rid)
            if not rec:
                return None
            recs.merge_data(rid, {"status": "skipped"})
        finally:
            recs.close()
        return {"id": rid, "role": rec.get("role") or "", "company": rec.get("company") or ""}

    def handle(self, rid, note: str) -> dict | None:
        """Attach a free-text handling note to an application; the (re)build reads it (see
        `_handling_note`). Persisted ON the record, so it travels with it."""
        recs = _records()
        try:
            rec = recs.get(rid)
            if not rec:
                return None
            recs.merge_data(rid, {"handling_note": str(note or "").strip()})
        finally:
            recs.close()
        return {"id": rid, "role": rec.get("role") or "", "company": rec.get("company") or ""}

    def chat(self, text: str) -> str:
        return _annalisa_reply(text)


def _control():
    """The persisted run-control store the auto-apply loop checks (pause / skip)."""
    from notify.control import ControlState
    return ControlState(_DATA / "control.json")


def _handling_note(rec: dict) -> str:
    """The free-text handling note attached to an application via `/handle` (Telegram), read by
    the rebuild path so an instruction like 'use my analyst profile' is applied to that record."""
    return str(_record_data(rec).get("handling_note") or "").strip()


def _annalisa_reply(text: str) -> str:
    """Free text from Telegram, answered by Annalisa with the shared P1 memory and remembered in
    the diary. Best-effort: returns "" (so the caller falls back to help) if the model or memory
    isn't available, e.g. no API key configured, so a message is never met with a stack trace."""
    try:
        palace = _palace()
        palace.remember_turn("user", text)
        mem = _memory()
        facts = mem.list_facts("default", status="active")
        history = palace.load_history()
        recalled = palace.recall(text)
        reply = str(_make_llm().converse("", history, recalled, facts) or "").strip()
        if reply:
            palace.remember_turn("note", reply)
        return reply
    except Exception:
        return ""


def _session() -> WebIntake:
    s = _SESSION.get("s")
    if s is None:
        raise RuntimeError("No active session. Start a new resume first.")
    return s


class Unavailable(RuntimeError):
    """SponsorJobs can't reach its model, and it is honest about WHY.

    SponsorJobs is an online, real-model tool: without a connection and the person's own key it
    cannot write anything, so it must stop and say so plainly rather than half-work. `reason`
    is a machine-readable code the UI turns into a specific screen (offline / no_key /
    no_credit / rate_limited / busy) instead of a generic error.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _classify_outage(exc: Exception) -> str:
    """Map a failure to WHY SponsorJobs is unavailable, so the UI can say the true thing:
    'you're offline' vs 'add your key' vs 'your credit ran out' are different problems
    with different fixes, and lumping them into one error message helps nobody."""
    if isinstance(exc, Unavailable):
        return exc.reason
    # The bundled broker path tags its own failures: 'upgrade_required' (out of plan allowance ->
    # show the paywall) vs 'service_down'. Trust that tag over the generic sniffing below.
    from llm.broker_client import BrokerUnavailable
    if isinstance(exc, BrokerUnavailable) and exc.reason:
        return exc.reason
    status = getattr(exc, "status_code", None)
    low = str(exc).lower()
    if status == 401 or "authentication" in low or "invalid x-api-key" in low:
        return "no_key"
    if status in (402, 403) or "credit balance" in low or "billing" in low or "quota" in low:
        return "no_credit"
    if status == 429 or "rate limit" in low or "rate_limit" in low:
        return "rate_limited"
    if status in (500, 502, 503, 529) or "overloaded" in low:
        return "busy"
    if isinstance(exc, (OSError, ConnectionError)) or "getaddrinfo" in low \
            or "connection" in low or "timed out" in low or "network" in low:
        return "offline"
    return ""


_FAILURE_TEXT = {
    "no_credit": ("your AI provider account is out of credit. Top it up (for Anthropic: "
                  "console.anthropic.com, Billing), then tap the button again."),
    "no_key": "SponsorJobs has no working AI key. Add one in Settings, AI key, then tap again.",
    "rate_limited": "your AI provider is rate limiting right now. Try again in a few minutes.",
    "busy": "your AI provider is overloaded right now. Try again in a few minutes.",
    "offline": "this computer looks offline. I'll be able to do it once it's back online.",
    "upgrade_required": "you've used the AI allowance on your plan. Open SponsorJobs to see the passes, or add your own AI key.",
}


def _friendly_failure(who: str, exc: Exception) -> str:
    """A message a person can act on, never a raw provider error (those can carry request ids,
    JSON, or internal detail). The full error still goes to the log."""
    reason = _classify_outage(exc)
    detail = _FAILURE_TEXT.get(reason, "something went wrong on this computer. Open SponsorJobs "
                                       "to try it from the Jobs page.")
    return f"I couldn't tailor {who}: {detail}"


def _guard(fn):
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as exc:  # surface a helpful message to the UI
            traceback.print_exc()
            reason = _classify_outage(exc)
            body = {"error": str(exc)}
            if reason:
                body["reason"] = reason
            # 503: SponsorJobs is temporarily unable to work, which is not the caller's fault.
            return jsonify(body), (503 if reason else 400)
    wrapper.__name__ = fn.__name__
    return wrapper


# Header the browser extension sends on every request to the endpoints below.
EXTENSION_HEADER = "X-Tailor-Extension"


def _extension_only(fn):
    """Guard for endpoints ONLY the SponsorJobs browser extension should reach, profile
    autofill / prefs / draft (personal data + spends the user's API credits) and sponsor
    lookup. They must not be callable by an arbitrary website the user happens to visit.

    Two locks, together closing PII exfiltration and API-key/credit abuse:
      1. No CORS headers anywhere, so a web page can never READ the JSON response (and a
         cross-origin preflight, triggered by JSON/custom-header requests, fails).
      2. A required custom header the extension sends via its background service worker,
         whose host-permission fetch bypasses CORS. A web page cannot set a custom header
         on a cross-origin request without a preflight this server refuses, so even a
         'simple' fire-and-forget POST is blocked.
    The app's own same-origin SPA never calls these endpoints, so the header requirement
    doesn't affect it."""
    def wrapper(*a, **k):
        if not request.headers.get(EXTENSION_HEADER):
            return jsonify({"error": "Local endpoint, only the SponsorJobs browser extension "
                                     "on this machine may call it."}), 403
        # The extension just spoke, so we know it is installed and reaching us. This is the
        # ONLY honest way to know: the app cannot inspect the person's browser, and asking
        # them "have you installed it?" is a question the software should answer itself.
        _EXT_SEEN["at"] = time.time()
        return fn(*a, **k)
    wrapper.__name__ = fn.__name__
    return wrapper


# When the extension last called us. In memory on purpose: "is it connected NOW" is a fact
# about this session, and a stale timestamp from last week would claim a connection that
# may no longer exist.
_EXT_SEEN: dict = {}
# LinkedIn and Indeed forbid bots (§7), so the extension is the ONLY way we help there.
# Shipped but never loaded, it is worth nothing, so the app has to notice and say so.
_EXT_FRESH_SECONDS = 15 * 60


@app.get("/api/extension/status")
def extension_status():
    """Is the browser extension installed and talking to us?

    Chrome cannot be made to install an unpacked extension from an installer (its own
    security rule), so there is always a manual step until the Web Store listing exists.
    That makes it the app's job to notice it is missing and say so, rather than the
    person's job to wonder why LinkedIn shows no badges.
    """
    seen = _EXT_SEEN.get("at")
    connected = bool(seen and (time.time() - seen) < _EXT_FRESH_SECONDS)
    folder = ROOT / "extension"
    return jsonify({
        "connected": connected,
        "last_seen": seen,
        # Where the person can point "Load unpacked" at. Shipped beside the app by the
        # installer, so this path exists on a real install, not only in a dev checkout.
        "folder": str(folder) if folder.exists() else "",
    })


def _nostore(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ pages
@app.before_request
def _guard_local_and_lock():
    """Two local-safety gates on every request:

    1. Host allowlist. The engine only serves the local app. Rejecting any Host that is not
       127.0.0.1/localhost defeats DNS-rebinding (a website the user visits cannot re-point
       its own domain at our port and drive the API, since the rebound request carries the
       attacker's Host). Per the loopback-security research.
    2. App Lock. When a passcode is set and the app is locked, refuse every data/AI route so
       nobody reads the profile/CVs or spends tokens through SponsorJobs. Only the shell page, its
       static assets, and the lock endpoints (needed to unlock) get through. Enforced here in
       the ENGINE, not just a UI overlay a local process could bypass."""
    host = (request.host or "").rsplit(":", 1)[0].lower().strip("[]")
    if host and host not in ("127.0.0.1", "localhost", "::1"):
        return jsonify({"error": "SponsorJobs only serves the local app on this machine."}), 403
    if _LOCK.enabled() and not _LOCK.unlocked():
        p = request.path or "/"
        if p == "/" or p.startswith("/static/") or p.startswith("/api/lock/"):
            return None
        return jsonify({"error": "SponsorJobs is locked.", "reason": "locked"}), 423
    return None


# ---- App Lock endpoints (a passcode for a shared machine). Reachable while locked ON PURPOSE
# (you must be able to unlock); the sensitive ones verify the passcode inside. ----
@app.get("/api/lock/status")
def lock_status():
    return jsonify(_LOCK.status())


@app.post("/api/lock/unlock")
def lock_unlock():
    pc = (request.get_json(silent=True) or {}).get("passcode", "")
    if _LOCK.unlock(pc):
        # Sweep any custodied secret still in plaintext (e.g. an older setup from before
        # custody covered Telegram/GitHub) into the encrypted lock, now that we're unlocked.
        _migrate_plaintext_creds_into_lock()
        _migrate_gmail_token_into_lock()
        return jsonify(_LOCK.status())
    return jsonify({"error": "That passcode is not correct."}), 401


@app.post("/api/lock/set")
def lock_set():
    """Turn App Lock ON: seal the current API key under a new passcode, then wipe the
    plaintext copy from the creds file."""
    if _LOCK.enabled():
        return jsonify({"error": "App Lock is already on. Change or remove it instead."}), 400
    pc = (request.get_json(silent=True) or {}).get("passcode", "")
    # Seal EVERY custodied secret the person has set (provider keys, Telegram, GitHub,
    # Tavus, Adzuna), not just the API key — so none is left in plaintext once locked.
    creds = {name: _cred(name) for name in _LOCK.custody_names() if _cred(name)}
    try:
        _LOCK.set_passcode(pc, creds)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    for name in creds:
        _remove_cred(name)   # plaintext gone; only the sealed copy remains
    # The Gmail token lives in its own file (not readable via _cred), so seal it separately.
    _migrate_gmail_token_into_lock()
    return jsonify(_LOCK.status())


@app.post("/api/lock/change")
def lock_change():
    b = request.get_json(silent=True) or {}
    try:
        if _LOCK.change(b.get("current", ""), b.get("passcode", "")):
            return jsonify(_LOCK.status())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"error": "Current passcode is wrong."}), 400


@app.post("/api/lock/remove")
def lock_remove():
    """Turn App Lock OFF: verify, then put the API key back into ordinary storage."""
    pc = (request.get_json(silent=True) or {}).get("passcode", "")
    try:
        creds = _LOCK.remove(pc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 401
    for k, v in creds.items():
        # The Gmail token belongs in its own file, not the creds file — route it back there
        # (lock is now disabled, so _gmail_save_token writes the plaintext token file).
        if k == _GMAIL_TOKEN:
            _gmail_save_token(v)
        else:
            _save_cred(k, v)
    return jsonify(_LOCK.status())


@app.post("/api/lock/lock")
def lock_now():
    _LOCK.lock()
    return jsonify(_LOCK.status())


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "stage": 2, "local_only": True})


# ------------------------------------------------------------------ dashboard
@app.get("/api/dashboard")
@_guard
def dashboard():
    recs = _records()
    try:
        cvs = recs.list()
    finally:
        recs.close()
    mem = _memory()
    try:
        has_profile = mem.exists("default")
    finally:
        mem.close()
    avg = round(sum(c["coverage"] or 0 for c in cvs) / len(cvs)) if cvs else 0
    jobs = []
    seen = set()
    for c in cvs:
        key = (c["role"], c["company"])
        if key not in seen:
            seen.add(key)
            jobs.append({"role": c["jd_label"] or c["role"], "company": c["company"] or "",
                         "when": c["created_at"]})
    return jsonify({
        "cvs": [{
            "id": c["id"], "role": c["role"], "company": c["company"] or "",
            "coverage": round(c["coverage"] or 0), "when": (c["created_at"] or "")[:10],
        } for c in cvs],
        "jobs": jobs,
        "stats": {"cvs": len(cvs), "jobs": len(jobs), "avg": avg},
        "has_profile": has_profile,
    })


# ------------------------------------------------------------------ application packages
def _safe_slug(text: str) -> str:
    s = "".join(ch if ch.isalnum() else "_" for ch in (text or "")).strip("_")
    while "__" in s:
        s = s.replace("__", "_")
    return s or "application"


def _record_data(rec: dict) -> dict:
    import json as _json
    try:
        return _json.loads(rec.get("data") or "{}")
    except (TypeError, ValueError):
        return {}


def _get_record(rid):
    """Fetch one record and CLOSE the connection. _records() opens a fresh sqlite handle per
    call, so a bare `_records().get(...)` leaks a handle every request; this closes it."""
    recs = _records()
    try:
        return recs.get(int(rid))
    finally:
        recs.close()


def _package_view(rec: dict) -> dict:
    """The full application package for the review screen: CV + cover letter + screening
    answers + coverage detail + visa badges, assembled from the record's persisted data."""
    data = _record_data(rec)
    rid = rec["id"]
    job = f"cv-{rid}"
    company = rec.get("company") or ""
    badges = []
    if company:
        srec = _sponsors().lookup(company)
        badges = srec.badges() if srec else []
    coverage = data.get("coverage") or {"ratio": round(rec.get("coverage") or 0),
                                         "present": [], "missing": [], "missing_supported": []}
    from submit import classify_record
    submission = classify_record(data)
    submission["result"] = data.get("submission")   # last submit attempt's status, if any
    return {
        "id": rid,
        "role": rec.get("role") or "",
        "company": company,
        "jd_label": rec.get("jd_label") or "",
        "coverage": coverage,
        "cover_letter": data.get("cover_letter") or "",
        "screening": data.get("screening") or [],
        "status": data.get("status") or "ready",
        "applied_at": data.get("applied_at"),
        "visa": badges,
        "created_at": (rec.get("created_at") or "")[:16],
        "pdf": f"/api/cv.pdf?job={job}" if (WORKDIR / f"{job}.pdf").exists() else None,
        "submission": submission,
        # The original posting, assisted apply opens it so the person can click Apply there.
        "apply_url": (data.get("source_job") or {}).get("url") or "",
    }


@app.get("/api/records")
@_guard
def records_list():
    """The application queue, one row per finished, tailored application."""
    recs = _records()
    try:
        rows = recs.list()
    finally:
        recs.close()
    from datetime import date

    from submit.tracker import effective_stage, follow_up
    today = date.today()
    out = []
    for r in rows:
        data = _record_data(r)
        fu = follow_up(data, today)
        out.append({
            "id": r["id"], "role": r.get("role") or "", "company": r.get("company") or "",
            "coverage": round(r.get("coverage") or 0),
            "when": (r.get("created_at") or "")[:10],
            "status": data.get("status") or "ready",
            "stage": effective_stage(data),               # the funnel: saved..rejected
            "follow_up_due": (fu or {}).get("due"),        # when to nudge yourself (if applied)
            "follow_up_is_due": bool(fu and fu["is_due"]),
            "has_cover_letter": bool((data.get("cover_letter") or "").strip()),
            "screening_count": len(data.get("screening") or []),
            "folder": str(data.get("folder") or ""),
            "sort": data.get("sort"),
        })
    return jsonify({"records": out})


@app.post("/api/record/<int:rid>/meta")
@_guard
def record_meta(rid: int):
    """Rename / refile / reorder one saved application (the Saved CVs manager).
    Body: {name?, folder?, sort?}. Folder and sort ride the record's data bag."""
    b = request.json or {}
    recs = _records()
    try:
        if recs.get(rid) is None:
            return jsonify({"error": f"No application {rid}."}), 404
        if "name" in b:
            name = str(b.get("name") or "").strip()
            if not name:
                return jsonify({"error": "A resume needs a name."}), 400
            recs.rename(rid, name[:120])
        patch = {}
        if "folder" in b:
            patch["folder"] = str(b.get("folder") or "").strip()[:60]
        if "sort" in b:
            try:
                patch["sort"] = float(b["sort"])
            except (TypeError, ValueError):
                pass
        if patch:
            recs.merge_data(rid, patch)
        row = recs.get(rid)
        data = _record_data(row)
        return jsonify({"ok": True, "id": rid, "role": row.get("role") or "",
                        "folder": str(data.get("folder") or ""),
                        "sort": data.get("sort")})
    finally:
        recs.close()


@app.get("/api/record/<int:rid>/thumb.png")
def record_thumb(rid: int):
    """First-page PNG of a saved CV, so the Saved CVs grid shows real document
    thumbnails (a CV looks like a PDF in a folder). Cached; 404 when unrenderable."""
    png = _cv_preview_png(rid)
    if not png or not Path(png).exists():
        return ("", 404)
    resp = send_file(png, mimetype="image/png", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/folders")
@_guard
def folders_get():
    return jsonify({"folders": _prefs().get("folders") or []})


@app.post("/api/folders")
@_guard
def folders_set():
    """Create or remove a folder name. Removing a folder never touches records:
    their cards simply regroup under Unfiled (labels, not cages)."""
    b = request.json or {}
    cur = list(_prefs().get("folders") or [])
    add = str(b.get("add") or "").strip()[:60]
    if add and add.lower() not in {f.lower() for f in cur}:
        cur.append(add)
    rn_from = str(b.get("rename_from") or "").strip()
    rn_to = str(b.get("rename_to") or "").strip()[:60]
    if rn_from and rn_to and rn_from != rn_to:
        cur = [rn_to if f == rn_from else f for f in cur]
        seen, deduped = set(), []
        for f in cur:
            if f.lower() not in seen:
                seen.add(f.lower()); deduped.append(f)
        cur = deduped
        recs = _records()
        try:
            for r in recs.list(limit=10000):
                if str(_record_data(r).get("folder") or "") == rn_from:
                    recs.merge_data(r["id"], {"folder": rn_to})
        finally:
            recs.close()
    if b.get("remove"):
        cur = [f for f in cur if f != b.get("remove")]
    _set_prefs({"folders": cur})
    return jsonify({"folders": _prefs().get("folders") or []})


@app.get("/api/record/<int:rid>")
@_guard
def record_detail(rid):
    recs = _records()
    try:
        rec = recs.get(rid)
    finally:
        recs.close()
    if not rec:
        return jsonify({"error": "That application was not found."}), 404
    return jsonify(_package_view(rec))


@app.post("/api/record/<int:rid>/status")
@_guard
def record_status(rid):
    status = (request.json or {}).get("status", "ready")
    if status not in ("ready", "applied"):
        return jsonify({"error": "status must be 'ready' or 'applied'"}), 400
    from submit.tracker import stage_patch
    recs = _records()
    try:
        rec = recs.get(rid)
        if rec is None:                # clean 404, not a leaky 400 from merge_data's KeyError
            return jsonify({"error": f"No application {rid}."}), 404
        # Route through the tracker so marking "applied" also arms the follow-up nudge (and
        # "ready" clears it) -- one code path shared with the funnel's /stage endpoint.
        patch = stage_patch(_record_data(rec), "applied" if status == "applied" else "saved",
                            _now_iso())
        data = recs.merge_data(rid, patch)
    finally:
        recs.close()
    return jsonify({"ok": True, "status": data.get("status"), "applied_at": data.get("applied_at"),
                    "stage": data.get("stage"), "follow_up_due": data.get("follow_up_due")})


@app.post("/api/record/<int:rid>/stage")
@_guard
def record_stage(rid):
    """Move one application along the honest funnel (saved / applied / interviewing / offer /
    rejected). Marking it applied arms an in-app follow-up nudge; progressing past that clears it."""
    from submit.tracker import STAGES, stage_patch
    stage = (request.json or {}).get("stage", "")
    if stage not in STAGES:
        return jsonify({"error": f"stage must be one of {', '.join(STAGES)}"}), 400
    recs = _records()
    try:
        rec = recs.get(rid)
        if rec is None:
            return jsonify({"error": f"No application {rid}."}), 404
        data = recs.merge_data(rid, stage_patch(_record_data(rec), stage, _now_iso()))
    finally:
        recs.close()
    return jsonify({"ok": True, "stage": data.get("stage"), "status": data.get("status"),
                    "applied_at": data.get("applied_at"), "follow_up_due": data.get("follow_up_due")})


@app.get("/api/followups")
@_guard
def followups():
    """The follow-up nudge list: applications you have applied to that are due (or coming up) for a
    check-in. Purely in-app and honest -- it never messages an employer, it just reminds YOU to."""
    from datetime import date

    from submit.tracker import follow_up
    today = date.today()
    recs = _records()
    try:
        rows = recs.list(limit=200)
    finally:
        recs.close()
    due, upcoming = [], []
    for r in rows:
        fu = follow_up(_record_data(r), today)
        if not fu:
            continue
        item = {"id": r["id"], "role": r.get("role") or "", "company": r.get("company") or "",
                "due": fu["due"]}
        (due if fu["is_due"] else upcoming).append(item)
    due.sort(key=lambda x: x["due"])
    upcoming.sort(key=lambda x: x["due"])
    return jsonify({"due": due, "upcoming": upcoming, "due_count": len(due)})


@app.get("/api/records/ready")
@_guard
def records_ready():
    """The apply TO-DO: tailored applications that are built but NOT yet applied or skipped,
    each with its destination URL and submission lane, so the person can work through the
    ones still waiting for their on-site submit. Closes the loop find -> tailor -> fill ->
    submit: nothing built falls through the cracks."""
    from submit import classify_record
    recs = _records()
    try:
        rows = recs.list()
    finally:
        recs.close()
    out = []
    for r in rows:
        data = _record_data(r)
        if (data.get("status") or "ready") != "ready":
            continue                       # applied / skipped are done, not on the to-do
        out.append({
            "id": r["id"], "role": r.get("role") or "", "company": r.get("company") or "",
            "apply_url": (data.get("source_job") or {}).get("url") or "",
            "lane": classify_record(data)["tier"],     # 'auto' | 'assisted'
        })
    return jsonify({"ready": out, "count": len(out)})


# Autonomous submission is OPT-IN and OFF by default (CLAUDE.md §4c/§7): even for a
# verified auto-eligible site, SponsorJobs won't submit unattended until the person turns this
# on. Persisted in a small local file so the choice survives restarts.
_AUTONOMOUS_FILE = _DATA / "autonomous_submit"


def _autonomous_on() -> bool:
    try:
        return _AUTONOMOUS_FILE.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return False


def _set_autonomous(on: bool) -> None:
    _AUTONOMOUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AUTONOMOUS_FILE.write_text("1" if on else "0", encoding="utf-8")


def _submit_driver_for(url: str):
    """The submission driver for a URL's host, but ONLY when the person has opted into
    autonomous submission. Off by default, so nothing auto-submits without consent."""
    if not _autonomous_on():
        return None
    from submit import driver_for
    from submit.policy import _host
    return driver_for(_host(url))


# Review preferences (volume cap, spacing, and the OPT-IN one-tap approve-all). Stored in
# a small local JSON; approve_all defaults OFF, one-tap approve-all must be deliberately
# turned on by the person (CLAUDE.md §7 review-first default).
import json as _json_prefs  # noqa: E402

_PREFS_FILE = _DATA / "review_prefs.json"
_RATE_FILE = _DATA / "submit_rate.json"
_PREFS_DEFAULT = {"approve_all": False, "daily_cap": 40, "min_gap": 45.0,
                  "inbox_auto_verify": False, "folders": []}


def _prefs() -> dict:
    try:
        return {**_PREFS_DEFAULT, **_json_prefs.loads(_PREFS_FILE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(_PREFS_DEFAULT)


def _set_prefs(patch: dict) -> dict:
    cur = _prefs()
    if "approve_all" in patch:
        cur["approve_all"] = bool(patch["approve_all"])
    if "daily_cap" in patch:
        cur["daily_cap"] = max(1, int(patch["daily_cap"]))
    if "min_gap" in patch:
        cur["min_gap"] = max(0.0, float(patch["min_gap"]))
    if "inbox_auto_verify" in patch:
        cur["inbox_auto_verify"] = bool(patch["inbox_auto_verify"])
    if "folders" in patch:
        cur["folders"] = [str(f).strip()[:60] for f in (patch["folders"] or [])
                          if str(f).strip()][:50]
    _PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PREFS_FILE.write_text(_json_prefs.dumps(cur), encoding="utf-8")
    return cur


def _rate_limiter():
    from submit.rate_limit import RateLimiter
    p = _prefs()
    return RateLimiter(_RATE_FILE, cap=p["daily_cap"], min_gap=p["min_gap"])


def _now_ts_today():
    from datetime import datetime
    now = datetime.now()
    return now.timestamp(), now.date().isoformat()


@app.get("/api/submit/settings")
@_guard
def submit_settings():
    """Autonomous-submit flag, review prefs (approve-all opt-in, daily cap, spacing), and
    whether Telegram is configured. Also the current day's submission count vs the cap."""
    p = _prefs()
    ts, today = _now_ts_today()
    return jsonify({
        "autonomous": _autonomous_on(),
        "approve_all": p["approve_all"],
        "daily_cap": p["daily_cap"],
        "min_gap": p["min_gap"],
        "inbox_auto_verify": bool(p.get("inbox_auto_verify")),
        "telegram_configured": _active_channel() is not None,
        "rate": _rate_limiter().status(ts, today),
    })


@app.post("/api/submit/settings")
@_guard
def submit_settings_set():
    """Update settings. Body may set {autonomous, approve_all, daily_cap, min_gap}. Both
    autonomous and approve_all default OFF and must be turned on deliberately."""
    body = request.json or {}
    if "autonomous" in body:
        _set_autonomous(bool(body["autonomous"]))
    _set_prefs(body)
    return jsonify({"ok": True, "autonomous": _autonomous_on(), **_prefs()})


def _notify_assisted(rec: dict, result: dict) -> None:
    """When an application routes to assisted, nudge the person on Telegram to review and
    click submit (if Telegram is set up). Best-effort, never breaks the submit response."""
    if result.get("tier") != "assisted":
        return
    try:
        if not _notify_state().prefs().get("app_updates", True):
            return
        ch = _active_channel()
        if ch is None:
            return
        who = (rec.get("role") or "An application") + (
            f" at {rec['company']}" if rec.get("company") else "")
        ch.send(f"📝 Ready to submit: {who}. This site needs your click, open SponsorJobs, review, "
                f"and submit. (Reply /status {rec['id']} for details.)")
    except Exception:
        pass


@app.post("/api/record/<int:rid>/submit")
@_guard
def record_submit(rid):
    """Submit an application by the per-site policy (submit/): auto-submit where the site is
    a VERIFIED auto-eligible portal and autonomous submission is on, else assisted (fill +
    you click, with a Telegram nudge). The outcome is recorded on the package; only a real
    auto-submit flips the status to applied."""
    result = _submit_record_id(rid)
    if result is None:
        return jsonify({"error": "That application was not found."}), 404
    return jsonify(result)


def _submit_record_id(rid: int) -> dict | None:
    """The per-site submit for one queued application (shared by the route and the Telegram
    [Apply for me] button). None when the record doesn't exist."""
    from submit import submit_record
    recs = _records()
    try:
        rec = recs.get(rid)
        if not rec:
            return None
        data = _record_data(rec)
        # Idempotent: never re-submit an application that already went through. A retry, a
        # double-click after a slow response, or a second manual hit must NOT fire a second
        # live submission or burn another daily-cap slot.
        if (data.get("status") or "ready") == "applied":
            return {"ok": True, "status": "already_applied",
                    "tier": (data.get("submission") or {}).get("tier", "auto"),
                    "message": "This application was already submitted."}
        url = (data.get("source_job") or {}).get("url") or ""
        driver = _submit_driver_for(url)
        if driver is not None:
            # A real auto-lane submission, enforce the SAME daily cap + spacing as the batch
            # path (otherwise this endpoint bypasses the cap). Over the limit → don't submit.
            ts, today = _now_ts_today()
            allowed, reason = _rate_limiter().allow(ts, today)
            if not allowed:
                result = {"ok": False, "tier": "auto", "status": "auto_pending",
                          "message": f"Auto-submit eligible but held back ({reason}), the "
                                     "daily volume cap/spacing applies. It'll submit on the "
                                     "next cycle.", "submitted_at": None}
            else:
                data = {**data, "profile": _saved_full_profile()}  # honest identity to submit
                result = submit_record(data, driver=driver, now=_now_iso())
                if result["status"] == "auto_submitted":
                    _rate_limiter().record(ts, today)   # count it against the cap
                elif result.get("posted"):
                    _rate_limiter().record_attempt(ts, today)   # a real POST fired -> space the next
        else:
            result = submit_record(data, driver=None, now=_now_iso())
        patch = {"submission": {"tier": result["tier"], "status": result["status"],
                                "message": result["message"], "at": result.get("submitted_at")}}
        if result["status"] == "auto_submitted":   # only a real auto-submit marks applied
            patch["status"] = "applied"
            patch["applied_at"] = result.get("submitted_at")
        recs.merge_data(rid, patch)
    finally:
        recs.close()
    if result["status"] == "assisted":
        _notify_assisted({**rec, "id": rid}, result)
    return result


# ------------------------------------------------------------- batch review
def _cv_preview_png(rid: int) -> str:
    """Path to the CV's first-page PNG (rendered from the PDF, cached). "" if it can't be
    rendered, the batch send then falls back to the PDF alone. Best-effort, never raises."""
    pdf = WORKDIR / f"cv-{rid}.pdf"
    png = WORKDIR / f"cv-{rid}.png"
    if not pdf.exists():
        return ""
    if png.exists() and png.stat().st_mtime >= pdf.stat().st_mtime:
        return str(png)          # cached and up to date
    try:
        from tailoring.preview import render_first_page_png
        return render_first_page_png(pdf, png, dpi=200)
    except Exception:
        return ""


def _cv_preview_redacted(rid: int, data: dict) -> str:
    """The Telegram preview of a saved CV: page 1 with the contact line covered unless the
    person turned on "Show my contact details in Telegram previews". "" when unrenderable."""
    pdf = WORKDIR / f"cv-{rid}.pdf"
    if not pdf.exists():
        return ""
    try:
        from notify.redact import render_preview
        ident = ((data.get("page_profile") or data.get("render_profile") or {}).get("identity")
                 or (_saved_full_profile().get("identity") or {}))
        show = bool(_notify_state().prefs().get("show_contact"))
        return render_preview(pdf, WORKDIR / f"tg-{rid}-preview.png", ident,
                              redact=not show).get("path") or ""
    except Exception:   # noqa: BLE001
        return ""


def _review_item(rec: dict) -> dict:
    """One pending application summarized for review (Telegram caption + in-app queue)."""
    data = _record_data(rec)
    rid = rec["id"]
    from submit import classify_record
    plan = classify_record(data)
    ident = (_saved_full_profile().get("identity") or {})
    cv = WORKDIR / f"cv-{rid}.pdf"
    cov = data.get("coverage") or {}
    return {
        "image_path": _cv_preview_redacted(rid, data) if cv.exists() else "",
        "id": rid,
        "role": rec.get("role") or "",
        "company": rec.get("company") or "",
        "lane": plan["tier"],
        "lane_label": plan["label"],
        # The APPLICATION form where one is derivable, not the advert (see _application_url).
        "url": _application_url(plan["url"], rec.get("company") or ""),
        "filled": {"name": bool(ident.get("name")), "email": bool(ident.get("email")),
                   "phone": bool(ident.get("phone"))},
        "screening_count": len(data.get("screening") or []),
        "has_cover_letter": bool((data.get("cover_letter") or "").strip()),
        "coverage": cov.get("ratio") if cov.get("ratio") is not None else round(rec.get("coverage") or 0),
        "cv_path": str(cv) if cv.exists() else "",
        "cv_url": f"/api/cv.pdf?job=cv-{rid}" if cv.exists() else "",
    }


def _greenhouse_board(company: str) -> str:
    """The Greenhouse board slug we source this company from, if any."""
    if not company:
        return ""
    w = _watchlist()
    try:
        for c in w.companies(active_only=False):
            if (c.get("ats") == "greenhouse"
                    and str(c.get("company", "")).strip().lower() == company.strip().lower()):
                return str(c.get("board_id") or "")
    except Exception:
        return ""
    finally:
        w.close()
    return ""


_GH_JID = re.compile(r"[?&]gh_jid=(\d+)")


def _application_url(job_url: str, company: str) -> str:
    """Upgrade a job ADVERT link to the actual APPLICATION FORM where we can.

    Feeds hand back the marketing listing (stripe.com/jobs/search?gh_jid=...), which is a
    page with zero form fields. Opening that for "assisted apply" gives the person a job
    advert and nothing to fill -- the assist has nowhere to put anything, so the promise
    of a prepared application is empty. Greenhouse publishes a public application form per
    posting, keyed by the same gh_jid the listing already carries, so the form is
    derivable rather than guessed. Anything we can't upgrade is returned unchanged.
    """
    match = _GH_JID.search(job_url or "")
    if not match:
        return job_url
    board = _greenhouse_board(company)
    if not board:
        return job_url
    return ("https://job-boards.greenhouse.io/embed/job_app"
            f"?for={board}&token={match.group(1)}")


def _pending_records() -> list[dict]:
    recs = _records()
    try:
        rows = recs.list(limit=200)
    finally:
        recs.close()
    # "approved" is NOT done. Outside the verified auto-lane, approving only marks a
    # package reviewed -- the application still has to be submitted. Filtering the queue to
    # "ready" alone meant one Approve click emptied the whole screen while submitting
    # nothing: the work looked finished and had not started. Only "applied" (or a skip)
    # leaves the queue.
    return [r for r in rows
            if (_record_data(r).get("status") or "ready") in ("ready", "approved")]


class _BatchActions:
    """Sanctioned approve/skip for the batch review. Approval submits ONLY through the
    verified channel: the auto-lane (Recruitee) submits via its API (approval = consent);
    everything else is marked reviewed/approved for the person to submit on-site, we NEVER
    auto-submit outside the verified auto-lane, and never touch LinkedIn's session."""

    def pending(self) -> list[dict]:
        return [_review_item(r) for r in _pending_records()]

    def approve(self, rid) -> dict:
        from submit import driver_for, submit_record
        from submit.policy import _host
        recs = _records()
        try:
            rec = recs.get(rid)
            if not rec:
                return {"ok": False, "message": f"#{rid} not found."}
            data = _record_data(rec)
            if (data.get("status") or "ready") == "applied":   # never re-submit (idempotent)
                return {"ok": True, "message": f"Already submitted: {rec.get('company') or 'application'}."}
            url = (data.get("source_job") or {}).get("url") or ""
            driver = driver_for(_host(url))     # approval IS the consent for THIS item
            if driver is not None:
                # Auto-lane (verified), this really submits over the sanctioned API, so it
                # is rate-limited (daily cap + spacing). Over the limit → queue for later.
                ts, today = _now_ts_today()
                allowed, reason = _rate_limiter().allow(ts, today)
                if not allowed:
                    recs.merge_data(rid, {"status": "approved", "queued": True,
                                          "submission": {"tier": "auto", "status": "queued",
                                                         "message": f"Approved, queued ({reason})."}})
                    return {"ok": True, "message": f"Approved, queued ({reason}); will submit shortly."}
                result = submit_record({**data, "profile": _saved_full_profile()},
                                       driver=driver, now=_now_iso())
                if result["status"] == "auto_submitted":
                    _rate_limiter().record(ts, today)
                    recs.merge_data(rid, {"status": "applied", "applied_at": result.get("submitted_at"),
                                          "queued": False,
                                          "submission": {"tier": result["tier"], "status": result["status"],
                                                         "message": result["message"]}})
                    return {"ok": True, "message": f"Approved & submitted: {rec.get('company') or 'application'} ✓"}
                if result.get("posted"):
                    _rate_limiter().record_attempt(ts, today)   # a real POST fired -> space the next
                # Un-queue on any non-success (failure / captcha-auth-wall drop-to-assisted): the
                # site already answered, so drain() must NOT keep re-POSTing this item every tick.
                recs.merge_data(rid, {"status": "approved", "queued": False,
                                      "submission": {"tier": result["tier"], "status": result["status"],
                                                     "message": result["message"]}})
                return {"ok": False, "message": result["message"]}
            # Assisted-lane, never auto-submit; mark approved/reviewed for on-site submit.
            recs.merge_data(rid, {"status": "approved", "queued": False,
                                  "submission": {"tier": "assisted", "status": "approved",
                                                 "message": "Approved, open the site and submit (it's filled)."}})
            return {"ok": True, "message": f"Approved: {rec.get('company') or 'application'}, "
                                           "open it and submit (assisted site)."}
        finally:
            recs.close()

    def skip(self, rid) -> dict:
        recs = _records()
        try:
            if not recs.get(rid):
                return {"ok": False, "message": f"#{rid} not found."}
            recs.merge_data(rid, {"status": "skipped"})
        finally:
            recs.close()
        return {"ok": True, "message": f"Skipped #{rid}."}

    def approve_all(self) -> dict:
        results = [self.approve(it["id"]) for it in self.pending()]
        ok = sum(1 for r in results if r.get("ok"))
        return {"ok": True, "message": f"Approve-all: {ok}/{len(results)} handled."}

    def drain(self, limit: int = 5) -> int:
        """Submit rate-limit-queued auto-lane approvals whose slot has come up. Called on
        each poll tick so a burst gets spaced out across ticks."""
        recs = _records()
        try:
            queued = [r for r in recs.list(limit=200) if _record_data(r).get("queued")]
        finally:
            recs.close()
        done = 0
        for r in queued[:limit]:
            ts, today = _now_ts_today()
            if not _rate_limiter().allow(ts, today)[0]:
                break
            if self.approve(r["id"]).get("ok"):
                done += 1
        return done


def _batch_review():
    """The batch review over the ACTIVE channel: the official bot (text + buttons) when linked,
    else the person's own bot (CV preview photo + PDF), else a clear error."""
    from notify.batch import BatchReview
    from notify.channel import TextOnlyBot
    ch = _active_channel()
    if ch is not None and getattr(ch, "name", "") == "official":
        bot = TextOnlyBot(ch)
    else:
        bot = _make_bot()
    return BatchReview(bot, _BatchActions(), approve_all_enabled=_prefs()["approve_all"])


@app.get("/api/review/queue")
@_guard
def review_queue():
    """The in-app review queue: every filled application waiting for approval, each with its
    tailored-CV link and a summary of what was filled. The person deselects/edits/approves
    on a big screen (review-first by nature)."""
    items = _BatchActions().pending()
    return jsonify({"pending": items, "count": len(items)})


@app.post("/api/review/approve")
@_guard
def review_approve():
    """In-app batch approve. Body {approve:[ids], skip:[ids]}. Submits approved items through
    the sanctioned channel (auto-lane via API; assisted marked for on-site submit)."""
    body = request.json or {}
    actions = _BatchActions()
    out = {"approved": [], "skipped": []}
    for rid in body.get("approve", []) or []:
        out["approved"].append({"id": rid, **actions.approve(rid)})
    for rid in body.get("skip", []) or []:
        out["skipped"].append({"id": rid, **actions.skip(rid)})
    return jsonify({"ok": True, **out})


@app.post("/api/review/telegram/send")
@_guard
def review_telegram_send():
    """SEND the pending batch to the person's Telegram (outbound only): a batch summary,
    then per application the CV preview + what-was-filled + Approve/Skip buttons. The opt-in
    'Approve all' button appears only if enabled."""
    result = _batch_review().send()
    return jsonify({"ok": True, **result})


@app.post("/api/review/telegram/poll")
@_guard
def review_telegram_poll():
    """POLL Telegram for the person's taps/replies and act on them (one notification tick:
    Approve/Skip and job-alert buttons, text commands, due alerts and the away digest), then
    drain any rate-limit-queued auto submissions. Outbound only; owner-only (fail-closed)."""
    out = _notify_tick()
    if out.get("channel") == "none":
        return jsonify({"ok": False, "handled": 0, "error": "Telegram isn't connected. "
                        "Connect it in Settings, Notifications."}), 400
    if out.get("error") == "no_owner":
        return jsonify({"ok": False, "error": "Set TELEGRAM_CHAT_ID (your own chat id) so "
                        "the bot only obeys you, refusing to act on taps until then.",
                        "handled": 0}), 400
    drained = _BatchActions().drain()
    return jsonify({**out, "ok": True, "offset": _tg_offset(), "drained": drained})


@app.post("/api/record/<int:rid>/cover_letter")
@_guard
def record_cover_letter(rid):
    """Save an edited cover letter (body {text}) or, with no text, draft a fresh one from
    the saved profile + the record's stored JD (real model). Persists to the package."""
    body = request.json or {}
    recs = _records()
    try:
        rec = recs.get(rid)
        if not rec:
            return jsonify({"error": "That application was not found."}), 404
        data = _record_data(rec)
        jd = data.get("jd_text") or rec.get("jd_label") or rec.get("role") or ""
        company = rec.get("company") or ""
        if "text" in body:
            text = str(body.get("text") or "")
        else:
            from drafting import cover_letter as _cover_letter
            out = _cover_letter(jd, _saved_full_profile(), _make_llm(),
                                role=rec.get("role") or "", company=company)
            text = out.get("cover_letter", "")
        (WORKDIR / f"cover-{rid}.txt").write_text(text, encoding="utf-8")
        recs.merge_data(rid, {"cover_letter": text})
        _render_cover_pdf(rid, text)
    finally:
        recs.close()
    # The honest craft check + grounding, recomputed for BOTH paths (drafted or hand-edited), so the
    # panel under the letter always reflects what is actually there now.
    from drafting.cover_review import review_cover_letter
    from tailoring.keywords import supported_skills
    review = review_cover_letter(text, company=company, profile=_saved_full_profile(), jd_text=jd)
    skills_used = supported_skills(jd, _saved_full_profile()) if jd else []
    has_pdf = (WORKDIR / f"cover-{rid}.pdf").exists()
    return jsonify({"ok": True, "cover_letter": text, "pdf": has_pdf,
                    "review": review, "skills_used": skills_used})


def _durable_profile() -> dict:
    """The person's DURABLE saved profile (from memory), independent of any live builder
    session. Used for a past record's cover-letter letterhead so it's never stamped with
    a different, in-progress application's identity."""
    mem = _memory().load("default") or {}
    return mem.get("profile") or mem.get("essentials") or {}


def _render_cover_pdf(rid: int, text: str) -> bool:
    """Render (or refresh) the matching one-page cover-letter PDF for a record from the
    person's durable saved letterhead. Best-effort, a missing LaTeX toolchain just means
    no PDF (the .txt cover letter still works)."""
    from drafting.cover_pdf import render_cover_letter_pdf
    pdf = WORKDIR / f"cover-{rid}.pdf"
    try:
        if not (text or "").strip():
            if pdf.exists():
                pdf.unlink()
            return False
        return bool(render_cover_letter_pdf(TEMPLATE, _durable_profile(), text,
                                            WORKDIR, f"cover-{rid}"))
    except Exception:
        return False


@app.get("/api/record/<int:rid>/cover_letter.pdf")
@_guard
def record_cover_pdf(rid):
    """The cover letter as a one-page PDF that matches the CV. Built on demand from the
    saved letter text if not already compiled."""
    pdf = WORKDIR / f"cover-{rid}.pdf"
    if not pdf.exists():
        recs = _records()
        try:
            rec = recs.get(rid)
        finally:
            recs.close()
        if rec:
            _render_cover_pdf(rid, _record_data(rec).get("cover_letter") or "")
    if not pdf.exists():
        return jsonify({"error": "No cover letter PDF yet, draft or save a cover letter first."}), 404
    resp = send_file(str(pdf), mimetype="application/pdf",
                     download_name="cover_letter.pdf", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/record/<int:rid>/cover_letter.docx")
@_guard
def record_cover_docx(rid):
    """The cover letter as an ATS-friendly .docx (parses more reliably than a PDF), built on
    demand from the saved letter text plus the person's contact header."""
    recs = _records()
    try:
        rec = recs.get(rid)
    finally:
        recs.close()
    text = (_record_data(rec).get("cover_letter") if rec else "") or ""
    if not text.strip():
        return jsonify({"error": "No cover letter yet, draft or save one first."}), 404
    from tailoring.docx_export import build_cover_docx
    out = build_cover_docx(_saved_identity(), text, WORKDIR / f"cover-{rid}.docx")
    resp = send_file(out, download_name="cover_letter.docx", max_age=0,
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/record/<int:rid>/export")
@_guard
def record_export(rid):
    """A submission-ready ZIP bundle: the tailored CV PDF, the cover letter, and a plain
    summary (role/company, coverage, screening answers), everything to submit by hand."""
    import io
    import zipfile
    recs = _records()
    try:
        rec = recs.get(rid)
    finally:
        recs.close()
    if not rec:
        return jsonify({"error": "That application was not found."}), 404
    data = _record_data(rec)
    role, company = rec.get("role") or "", rec.get("company") or ""
    slug = _safe_slug(f"{company}_{role}") or f"application_{rid}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        pdf = WORKDIR / f"cv-{rid}.pdf"
        if pdf.exists():
            z.write(pdf, arcname=f"{slug}_CV.pdf")
        cover = (data.get("cover_letter") or "").strip()
        if cover:
            cover_pdf = WORKDIR / f"cover-{rid}.pdf"
            if not cover_pdf.exists():
                _render_cover_pdf(rid, cover)   # build it for the bundle if we haven't yet
            if cover_pdf.exists():
                z.write(cover_pdf, arcname=f"{slug}_Cover_Letter.pdf")
            z.writestr(f"{slug}_Cover_Letter.txt", cover)
        cov = data.get("coverage") or {}
        lines = [f"Application summary, {role}" + (f" at {company}" if company else ""), ""]
        if cov.get("ratio") is not None:
            lines.append(f"JD keyword coverage: {cov.get('ratio')}%")
            if cov.get("present"):
                lines.append("Present: " + ", ".join(cov["present"]))
            if cov.get("missing"):
                lines.append("Not covered by your profile: " + ", ".join(cov["missing"]))
        for qa in (data.get("screening") or []):
            lines += ["", f"Q: {qa.get('question', '')}", f"A: {qa.get('answer', '')}"]
        z.writestr(f"{slug}_Summary.txt", "\n".join(lines))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{slug}_application.zip", max_age=0)


@app.post("/api/record/<int:rid>/delete")
@_guard
def record_delete(rid):
    """Delete an application: its record row and every artifact file it produced
    (compiled CV, cover-letter PDF, cover-letter text)."""
    recs = _records()
    try:
        deleted = recs.delete(rid)
    finally:
        recs.close()
    if not deleted:
        return jsonify({"error": "That application was not found."}), 404
    for name in (f"cv-{rid}.pdf", f"cover-{rid}.pdf", f"cover-{rid}.txt"):
        try:
            (WORKDIR / name).unlink(missing_ok=True)
        except OSError:
            pass
    return jsonify({"ok": True, "deleted": rid})


# ------------------------------------------------------------------ session
@app.get("/api/templates")
@_guard
def templates():
    """The CV templates the person can pick from, selecting one determines the CV's
    shape and the questions the intake asks."""
    return jsonify({"templates": _templates(), "default": _DEFAULT_TEMPLATE})


@app.post("/api/templates/suggest")
@_guard
def templates_suggest():
    """Pick a template from the JD, so the person doesn't have to judge templates at all.

    The JD is already pasted by this point, so the answer costs them nothing.
    """
    jd = ((request.json or {}).get("jd") or "").strip()
    return jsonify(_suggest_template(jd))


def _template_preview_pdf(name):
    """Render (or reuse the cached) preview PDF of a template filled with NEUTRAL
    placeholder data (John Doe), through the SAME one-page-fitting path the real app
    uses. Deterministic, cached by content hash. Returns the path or None."""
    import hashlib
    import json as _json

    from intake.template_manifest import load_manifest, sections
    from tailoring.assembler import assemble_cv
    tex, tname = _load_template(name)
    profile = _placeholder_profile()
    secs = sections(load_manifest(tname))
    key = hashlib.sha1((tex + _json.dumps(profile, sort_keys=True)
                        + repr(secs)).encode("utf-8")).hexdigest()[:10]
    out_pdf = WORKDIR / f"preview-{tname}-{key}.pdf"
    if not out_pdf.exists():
        WORKDIR.mkdir(parents=True, exist_ok=True)
        assemble_cv(tex, profile, "", None, WORKDIR,
                    jobname=out_pdf.stem, tailor=False, sections=secs)
    return out_pdf if out_pdf.exists() else None


@app.get("/api/templates/<name>/preview.pdf")
@_guard
def template_preview(name):
    """The rendered one-page preview PDF (placeholder data), for the "Open preview" link."""
    out_pdf = _template_preview_pdf(name)
    if not out_pdf:
        return jsonify({"error": "Couldn't render this template's preview."}), 500
    return _nostore(send_file(out_pdf, mimetype="application/pdf"))


@app.get("/api/templates/<name>/thumb.png")
@_guard
def template_thumb(name):
    """First-page PNG of the template preview, so the picker cards fill edge-to-edge
    (an image has no PDF-viewer grey gutter). Cached alongside the preview PDF."""
    out_pdf = _template_preview_pdf(name)
    if not out_pdf:
        return ("", 404)
    png = out_pdf.with_suffix(".png")
    try:
        if not (png.exists() and png.stat().st_mtime >= out_pdf.stat().st_mtime):
            from tailoring.preview import render_thumbnail_png
            render_thumbnail_png(out_pdf, png, width=440, dpi=220)
    except Exception:
        return ("", 404)
    if not png.exists():
        return ("", 404)
    resp = send_file(png, mimetype="image/png", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/session/start")
@_guard
def start():
    body = request.json or {}
    jd = body.get("jd", "").strip()
    if not jd:
        return jsonify({"error": "Paste a job description to begin."}), 400
    llm = _count_package(_make_llm())  # one tailoring run = one package; clear error if no key
    # Pick the template FROM the JD unless the person deliberately chose one in the picker.
    # Choosing is work, and someone who just pasted a job wants a CV, not a decision about
    # layout: we've already read the JD, so we already know the answer. The builder names
    # what it used and offers Change, so this is a default they can overrule, never a
    # silent one (silent-and-sticky was the original bug).
    chosen = body.get("template") or _suggest_template(jd)["name"]
    tex, tname = _load_template(chosen)
    # Unique jobname per session so a locked/leftover PDF can never be served.
    jobname = "cv" + uuid.uuid4().hex[:8]
    session = WebIntake(jd, tex, llm, _memory(), _records(), WORKDIR,
                        jobname=jobname, palace_dir=PALACE_DIR, template_name=tname)
    _SESSION["s"] = session
    return jsonify(session.start())


@app.post("/api/record/<int:rid>/variant")
@_guard
def record_variant(rid: int):
    """One-button variant: rebuild a finished application's CV on a different template
    and/or with a per-variant role selection, WITHOUT touching the durable profile
    (obs #36: the founder's A/B comparison took eight manual steps, including SQL
    surgery to bench a role). The variant session runs on a SCRATCH profile row
    seeded from 'default', the same isolation trick the auto-apply batch uses, so
    its autosaves can never mutate the real profile. The new record links back to
    its sibling via data.variant_of."""
    import json

    b = request.json or {}
    rec = _get_record(rid)
    if rec is None:
        return jsonify({"error": f"No application {rid}."}), 404
    try:
        jd = str(json.loads(rec.get("data") or "{}").get("jd_text") or "").strip()
    except (TypeError, ValueError):
        jd = ""
    if not jd:
        return jsonify({"error": "This application has no saved job description, "
                                 "so a variant can't be rebuilt from it."}), 400

    exclude = {str(o).strip().lower() for o in (b.get("exclude_orgs") or []) if str(o).strip()}
    mem = _memory()
    saved = mem.load("default") or {}
    prof = json.loads(json.dumps(saved.get("profile") or {}))
    ess = json.loads(json.dumps(saved.get("essentials") or {}))
    if exclude:
        for bag in (prof, ess):
            bag["experience"] = [e for e in bag.get("experience", [])
                                 if str(e.get("org") or "").strip().lower() not in exclude]
    mem.save("variant", prof, ess, [])

    llm = _count_package(_make_llm())   # a variant is a new tailoring run: one package
    chosen = b.get("template") or _suggest_template(jd)["name"]
    tex, tname = _load_template(chosen)
    jobname = "cv" + uuid.uuid4().hex[:8]
    session = WebIntake(jd, tex, llm, mem, _records(), WORKDIR, jobname=jobname,
                        palace_dir=PALACE_DIR, template_name=tname,
                        profile_name="variant")
    session.variant_of = rid
    _SESSION["s"] = session
    session.start()
    # P3: apply any Telegram handling note attached to this record ("use my analyst profile",
    # "don't mention the gap"). Feeding it as a turn lets P1 distill it into a preference/
    # constraint the tailoring then honors, on the scratch "variant" profile only.
    note = _handling_note(rec)
    if note:
        session._append("user", note)
        try:
            session._distill_facts()
        except Exception:
            pass
    # The scratch profile is complete by construction, so the saved-build fast path
    # tailors and compiles in this one call; the response is the review state.
    return jsonify(session.submit(
        "Nothing new. Everything is in my saved profile, including my summary. Build it."))


@app.get("/api/session/state")
def session_state():
    """Read the live session, so the person can leave a CV and come back to it.

    Every other session route is a POST that MUTATES. Nothing could READ the session, so
    the client held the whole CV in browser memory and any navigation destroyed work the
    server still had sitting right here in _SESSION ("I click away to other pages and I
    come back to the CV that we just created and it disappears").

    Never raises for "no session": an empty builder is a normal state, not an error.
    """
    s = _SESSION.get("s")
    if s is None:
        return jsonify({"active": False})
    state = s._state()
    state["active"] = True
    # The transcript lives server-side in session.history; the chat bubbles were only ever
    # in the browser. Hand it back or the CV returns to an empty conversation, which still
    # reads as "we weren't working on anything".
    state["history"] = [{"role": t.get("role"), "content": t.get("content")}
                        for t in (s.history or [])]
    return jsonify(state)


@app.post("/api/session/answer")
@_guard
def answer():
    text = (request.json or {}).get("text", "")
    return jsonify(_session().submit(text))


def _receive_upload():
    """Validate an uploaded file, save the raw copy under data/uploads (kept, never
    deleted, not indexed), and return (session, extracted_text, filename)."""
    from datetime import datetime

    from werkzeug.utils import secure_filename

    from intake.cv_import import SUPPORTED_EXTS, extract_text

    f = request.files.get("file")
    if f is None or not f.filename:
        return None, None, ("No file uploaded.", 400)
    name = secure_filename(f.filename) or "upload"
    if Path(name).suffix.lower() not in SUPPORTED_EXTS:
        return None, None, (f"Unsupported file type '{Path(name).suffix.lower()}'. "
                            "Use Word, PDF, PowerPoint, or an image.", 400)
    session = _session()   # a JD must already be loaded (raises a clear error otherwise)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}"
    f.save(str(dest))       # raw backup, kept, never deleted
    return session, extract_text(dest), name


@app.post("/api/session/upload")
@_guard
def upload():
    """ONE upload endpoint. Read any document (Word/PDF/PowerPoint/image), then let
    the session decide what it is, CV, transcript, or certificate, and route it.
    The person never has to say which; every upload is just an upload."""
    session, text, name = _receive_upload()
    if session is None:
        msg, code = name
        return jsonify({"error": msg}), code
    return jsonify(session.ingest_document(text, name))


@app.post("/api/session/upload_cv")
@_guard
def upload_cv():
    """Prefill the profile from an uploaded CV (.docx/.pdf/.pptx/image), fully local."""
    session, text, name = _receive_upload()
    if session is None:
        msg, code = name
        return jsonify({"error": msg}), code
    return jsonify(session.ingest_resume(text, name))


@app.post("/api/session/upload_transcript")
@_guard
def upload_transcript():
    """Read the real courses off an academic transcript and populate the most-recent
    degree's courses with the JD-relevant ones. Same local extraction/OCR + storage."""
    session, text, name = _receive_upload()
    if session is None:
        msg, code = name
        return jsonify({"error": msg}), code
    return jsonify(session.ingest_transcript(text, name))


@app.post("/api/session/remove_suggested")
@_guard
def remove_suggested():
    """Drop flagged placeholder projects so the CV can be finalized."""
    return jsonify(_session().remove_suggested_projects())


@app.post("/api/session/title")
@_guard
def title():
    body = request.json or {}
    return jsonify(_session().set_title(body.get("id", ""), body.get("decision", "keep"),
                                        body.get("text", "")))


@app.post("/api/session/bullet")
@_guard
def bullet():
    body = request.json or {}
    return jsonify(_session().edit_bullet(body.get("ref", ""), body.get("text", "")))


@app.post("/api/session/date")
@_guard
def edit_date():
    body = request.json or {}
    return jsonify(_session().edit_date(body.get("ref", ""), body.get("text", "")))


@app.post("/api/session/accept")
@_guard
def accept():
    return jsonify(_session().accept())


# ------------------------------------------------------------------ drafting (§5)
@app.post("/api/session/cover_letter")
@_guard
def cover_letter():
    """Draft a cover letter for the active session's JD from the person's profile.
    Body: {tone?}. Returns the letter text + a grounding report (JD-supported skills)."""
    tone = (request.json or {}).get("tone") or "professional"
    return jsonify(_session().cover_letter(tone))


@app.post("/api/session/screening")
@_guard
def screening():
    """Answer a form's free-text screening questions from the profile. Body:
    {questions: [str, ...]}. Returns one grounded answer per question, in order."""
    questions = (request.json or {}).get("questions") or []
    return jsonify(_session().screening_answers(questions))


@app.get("/api/cover_letter.txt")
def cover_letter_txt():
    job = request.args.get("job", "")
    name = "".join(ch for ch in job if ch.isalnum() or ch in "-_") + ".txt"
    path = WORKDIR / name
    if not path.exists():
        return jsonify({"error": "not built yet"}), 404
    resp = send_file(str(path), mimetype="text/plain; charset=utf-8",
                     download_name="cover_letter.txt", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ pdf
def _preview_safe_pdf(path):
    """Reroute every external link in a PDF through /api/open, so a click inside
    the embedded preview can never carry the app window off to GitHub with no way
    back (the reader's own machine opens the link in their default browser). The
    Download copy keeps its real links; only the preview is rewritten."""
    import io
    from urllib.parse import quote

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import TextStringObject

    reader = PdfReader(str(path))
    writer = PdfWriter()
    writer.append(reader)
    for page in writer.pages:
        for annot in (page.get("/Annots") or []):
            obj = annot.get_object()
            action = obj.get("/A")
            uri = action.get("/URI") if action else None
            if uri and str(uri).lower().startswith(("http://", "https://")):
                action[TextStringObject("/URI")] = TextStringObject(
                    f"/api/open?u={quote(str(uri), safe='')}")
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    return buf


@app.get("/api/cv.pdf")
def cv_pdf():
    job = request.args.get("job", "cv")
    # constrain to a safe filename in the workdir
    name = "".join(ch for ch in job if ch.isalnum() or ch in "-_") + ".pdf"
    path = WORKDIR / name
    if not path.exists():
        return jsonify({"error": "not built yet"}), 404
    if request.args.get("preview"):
        resp = send_file(_preview_safe_pdf(path), mimetype="application/pdf",
                         download_name="resume.pdf", max_age=0)
    else:
        resp = send_file(str(path), mimetype="application/pdf",
                         download_name="resume.pdf", max_age=0)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/cv.docx")
@_guard
def cv_docx():
    """Download the tailored resume as an ATS-friendly Word (.docx). Parsers read a
    .docx's XML text ordering more reliably than a PDF, and older Taleo/iCIMS prefer Word.
    Built on the fly from the SAME tailored profile the PDF was rendered from (persisted on
    the record as `render_profile`), so the two match. Falls back to the saved profile for
    older records built before render_profile was persisted."""
    from tailoring.docx_export import build_docx
    rid = request.args.get("rid", "")
    profile = None
    if rid.isdigit():
        rec = _get_record(rid)
        if rec:
            profile = _record_data(rec).get("render_profile")
    if not profile:
        profile = _saved_full_profile()
    if not profile:
        return jsonify({"error": "no profile to export yet"}), 404
    name = "cv-" + ("".join(ch for ch in rid if ch.isalnum()) or "resume")
    out = build_docx(profile, WORKDIR / f"{name}.docx")
    resp = send_file(out, download_name="resume.docx", max_age=0,
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/cv.parse_preview")
@_guard
def cv_parse_preview():
    """A 'how a resume parser sees your CV' plaintext readback of the tailored profile, so
    the person can sanity-check nothing important got dropped or scrambled for an ATS."""
    from tailoring.docx_export import profile_to_plaintext
    rid = request.args.get("rid", "")
    profile = None
    if rid.isdigit():
        rec = _get_record(rid)
        if rec:
            profile = _record_data(rec).get("render_profile")
    if not profile:
        profile = _saved_full_profile()
    return jsonify({"text": profile_to_plaintext(profile or {})})


@app.get("/api/open")
def open_external():
    """Open an external link from the CV preview in the person's DEFAULT browser,
    then send the app window straight back to where it was. This is the answer to
    'I clicked a link and could not get back into SponsorJobs'."""
    import webbrowser

    url = (request.args.get("u") or "").strip()
    if not url.lower().startswith(("http://", "https://")) or len(url) > 2000:
        return jsonify({"error": "only http(s) links can be opened"}), 400
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return (
        "<!doctype html><html><head><title>Opening link</title></head>"
        "<body style='font-family:system-ui;background:#111;color:#ddd;"
        "display:flex;align-items:center;justify-content:center;height:95vh'>"
        "<div style='text-align:center'><p>Opened in your browser.</p>"
        "<p><a href='/' target='_top' style='color:#9db4ff'>Back to SponsorJobs</a></p>"
        "<script>setTimeout(function(){history.back();},1200);</script>"
        "</div></body></html>")


# ------------------------------------------------------------------ company logos
# Real company logos for the job board, fetched ONLY when the person opts in (the "Company
# logos" toggle). We proxy through here rather than hitting a logo host straight from the
# browser for three reasons: (1) we can guess the employer's domain server-side and improve it
# without shipping new JS, (2) the logo service returns a small generated placeholder instead
# of a 404 on a miss, so we detect that by size and send a real 404 -> the board falls back to
# our own clean monogram instead of a foreign placeholder, and (3) an in-process cache means we
# fetch each domain once. Uses apistemic's keyless logo API (no key, no signup, permissive).
_LOGO_CACHE: dict[str, tuple] = {}   # domain -> (logo_bytes, mimetype) | (None, None) on a miss
_LOGO_MISS_MAX = 1500          # a miss is a ~650B generated avatar; real logos are multi-kB
_CO_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|group|holdings?|"
    r"plc|pbc|gmbh|lp|llp|pllc|ag|s\.?a|labs?|technolog(?:y|ies)|solutions?|systems?|the)\b", re.I)
# A school's name usually ENDS in the academic word ("DePaul University"); that's the reliable
# signal for a .edu domain AND the piece to drop from the slug (depaul.edu, not
# depauluniversity.edu). A leading/middle one ("University Health") is a normal company word.
_UNI_TAIL_RE = re.compile(r"\b(univers\w*|college|school|institut\w*|polytechnic|academy)\s*$", re.I)


def _guess_company_domain(company: str) -> str:
    """Best-effort employer domain from a display name: strip legal suffixes, collapse to a
    slug, and pick .edu (dropping the trailing 'University'/'College') for schools, .com
    otherwise. Wrong guesses simply miss (the size check turns them into a 404), so an
    over-eager guess never shows the wrong logo -- the board just keeps its monogram."""
    name = _CO_SUFFIX_RE.sub(" ", (company or "").strip().lower())
    if _UNI_TAIL_RE.search(name):
        tld = ".edu"
        name = _UNI_TAIL_RE.sub(" ", name)             # "depaul university" -> "depaul"
    else:
        tld = ".com"
    slug = re.sub(r"[^a-z0-9]", "", name)
    return slug + tld if slug else ""


def _fetch_logo(domain: str):
    """A real logo for a domain, or (None, None) on a miss. Two keyless sources, best first:
    apistemic (clean multi-kB WebP logos, but returns a small generated placeholder on a miss, so
    we size-gate it), then DuckDuckGo's favicon service (broad coverage, smaller icons, a true 404
    on a miss so no gate needed). Chaining the two lifts coverage on real domains apistemic lacks
    (e.g. freshclinics.com) without ever risking a WRONG logo -- a miss just falls through to our
    own monogram."""
    import urllib.parse
    import urllib.request
    ua = {"User-Agent": "resume-agent/1.0 (job board logos)"}
    try:
        req = urllib.request.Request(
            "https://logos-api.apistemic.com/domain:" + urllib.parse.quote(domain), headers=ua)
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = r.read()
        if raw and len(raw) >= _LOGO_MISS_MAX:        # big enough to be a real logo, not a placeholder
            return raw, "image/webp"
    except Exception:                                 # noqa: BLE001 - network blip / unknown domain
        pass
    try:                                              # DuckDuckGo 404s on a genuine miss (raises here)
        req = urllib.request.Request(
            "https://icons.duckduckgo.com/ip3/" + domain + ".ico", headers=ua)
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = r.read()
        if raw and len(raw) > 100:                    # a real favicon, not an empty body
            return raw, "image/x-icon"
    except Exception:                                 # noqa: BLE001 - no favicon -> monogram
        pass
    return None, None


@app.get("/api/logo")
def company_logo():
    domain = _guess_company_domain(request.args.get("company", ""))
    if not domain:
        return ("", 404)
    if domain not in _LOGO_CACHE:
        _LOGO_CACHE[domain] = _fetch_logo(domain)     # (bytes, mimetype) or (None, None)
    data, mime = _LOGO_CACHE[domain] or (None, None)
    if not data:
        return ("", 404)
    resp = app.response_class(data, mimetype=mime or "image/webp")
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


# ------------------------------------------------------------------ sourcing (watchlist)
def _split(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v or "").split(",") if s.strip()]


@app.get("/api/watchlist")
@_guard
def watchlist_get():
    w = _watchlist()
    try:
        return jsonify({"companies": w.companies(active_only=False),
                        "criteria": w.get_criteria(),
                        "supported_ats": list(SUPPORTED_ATS)})
    finally:
        w.close()


@app.post("/api/watchlist")
@_guard
def watchlist_add():
    b = request.json or {}
    ats = (b.get("ats") or "").strip().lower()
    board = (b.get("board_id") or "").strip()
    if ats not in SUPPORTED_ATS:
        return jsonify({"error": f"ATS must be one of {', '.join(SUPPORTED_ATS)}"}), 400
    if not board:
        return jsonify({"error": "Enter the company's public board id."}), 400
    from sourcing.ats import valid_board_id_for
    if not valid_board_id_for(ats, board):
        hint = ("use 'tenant.wdN/Site' from the careers URL, e.g. nvidia.wd5/NVIDIAExternalCareerSite"
                if ats == "workday" else
                "use the plain board slug (letters, digits, '.', '_', '-'), not a full URL or host")
        return jsonify({"error": f"That board id isn't valid, {hint}."}), 400
    w = _watchlist()
    try:
        w.add_company(b.get("company") or board.title(), ats, board)
        return jsonify({"ok": True, "companies": w.companies(active_only=False)})
    finally:
        w.close()


@app.post("/api/watchlist/remove")
@_guard
def watchlist_remove():
    wid = (request.json or {}).get("id")
    w = _watchlist()
    try:
        w.remove_company(int(wid))
        return jsonify({"ok": True, "companies": w.companies(active_only=False)})
    finally:
        w.close()


@app.post("/api/criteria")
@_guard
def criteria_set():
    b = request.json or {}
    w = _watchlist()
    try:
        w.set_criteria({"titles": _split(b.get("titles")),
                        "locations": _split(b.get("locations")),
                        "remote": (b.get("remote") or "any")})
        return jsonify({"ok": True, "criteria": w.get_criteria()})
    finally:
        w.close()


@app.post("/api/jobs/refresh")
@_guard
def jobs_refresh():
    w = _watchlist()
    try:
        summary = refresh_watchlist(w)     # hits official public feeds only
        jobs = _sponsors().tag_jobs(w.list_jobs(order="recent", limit=1500))   # freshest first
        jobs = [j for j in jobs if j.get("us")]      # US-only: this product is US visa sponsorship
        _sponsors().set_meta("jobs_refreshed_at", _now_iso())
        return jsonify({"summary": summary, "jobs": jobs})
    finally:
        w.close()


def _newest_arrival(jobs) -> str | None:
    """When the newest job on the board first ARRIVED (its first_seen), or None. This is what the
    board's freshness label shows. refreshed_at only says when the list was last rebuilt, and that
    keeps ticking while no new job comes in: in August 2026 the board read "updated 2m ago" over a
    month of no new postings."""
    return max((str(j.get("first_seen") or "") for j in jobs), default="") or None


@app.get("/api/jobs")
@_guard
def jobs_list():
    # Central "kitchen": if JOBS_FEED_URL is set, pull the already-fresh, sponsor-tagged feed
    # from the shared hosted service instead of crawling locally (so the paid aggregator keys
    # are used ONCE, centrally, not once per user). Falls back to the local feed on any error,
    # so the app always works. Personal data never leaves the machine either way.
    # Facets + pagination are applied SERVER-side (see sourcing/filters) so a single page is
    # small no matter how deep the board goes. We forward the same params to the kitchen, or
    # apply them locally on the fallback path.
    # Saved is PERSONAL, so the Saved view is always served from the local snapshot store, never
    # the shared feed. Returned early so it bypasses the feed entirely.
    if request.args.get("saved") == "1":
        return _saved_jobs_view()
    feed_url = _feed_base_url()
    if feed_url:
        # STATIC feed (docs/feed.md): the slim list is downloaded at most hourly into the data dir
        # and filtered + paginated HERE with the same sourcing/filters the kitchen used, so one
        # download serves every facet change. Any failure with nothing cached falls back to the
        # local crawl below (flagged `degraded`, so the client keeps its last good board).
        try:
            from sourcing.filters import apply_facets, paginate
            sf = _static_feed()
            jobs, header = sf.jobs()
            # A feed that downloaded fine but holds no jobs at all is NOT an authoritative empty
            # board (an empty jobs.json.gz from a bad central crawl, a fresh bucket): it is served
            # like an unreachable one, so the local path below runs with `degraded` set and the
            # first-open crawl can engage once the emptiness has lasted (see _FEED_EMPTY).
            _note_feed_empty(not jobs)
            if jobs:
                a = request.args
                facets = _facets(a)
                filtered = apply_facets(jobs, **facets)
                page_jobs, count, page, per_page = paginate(filtered, a.get("page", 1),
                                                            a.get("per_page", 30))
                page_jobs = [dict(j) for j in page_jobs]     # never mutate the shared cached rows
                saved = _saved_ids()                         # overlay the user's bookmarks onto feed rows
                if saved:
                    for j in page_jobs:
                        j["saved"] = j.get("source_id") in saved
                refreshed = header.get("generated_at")
                return jsonify({"jobs": page_jobs, "count": count, "total": len(jobs),
                                "page": page, "per_page": per_page, "refreshed_at": refreshed,
                                "newest_at": _newest_arrival(jobs),
                                "stale": _is_stale(refreshed, hours=12) if refreshed else False,
                                "source": "central"})
        except Exception:                            # noqa: BLE001 - fall back to the local feed
            traceback.print_exc()
    w = _watchlist()
    try:
        from sourcing.filters import apply_facets, paginate
        from sourcing.quality import (employer_is_clearance_heavy, is_entry_level, is_jd_checked,
                                      is_sponsor_relevant)
        # One-time-per-process: fill pay from the JD for rows a feed left blank, so the min-pay
        # filter + top-paid sort work over the whole board immediately (not only after a refresh).
        global _SALARY_BACKFILLED
        if not _SALARY_BACKFILLED:
            from sourcing.ats import salary_from_text
            try:
                w.backfill_salaries(salary_from_text)
            except Exception:                            # noqa: BLE001 - best-effort, never blocks the feed
                traceback.print_exc()
            _SALARY_BACKFILLED = True
        # RECENT-sorted so the freshest postings lead; US-only + accessible. A list-only row
        # (Workday) whose description was never fetched has not had the citizenship / clearance /
        # no-sponsorship check run on it, so it is not in the accessible set until it has.
        jobs = _sponsors().tag_jobs(w.list_jobs(order="recent", limit=15000))
        jobs = [j for j in jobs
                if j.get("us") and not employer_is_clearance_heavy(j.get("company", ""))
                and is_jd_checked(j) and is_sponsor_relevant(j)]
        for j in jobs:
            j["entry_level"] = is_entry_level(j.get("title", ""))
        # Same facets + pagination the static-feed path uses.
        a = request.args
        facets = _facets(a)
        filtered = apply_facets(jobs, **facets)
        # On-demand search: a keyword thin in what we hold triggers a live query into freehire's
        # full 1M+ index (persisted so it sticks), so search reaches everything, not just the slice.
        qterm = facets["q"].strip()
        if qterm and len(filtered) < 25:
            try:
                from sourcing.ats import freehire_jobs
                extra = _sponsors().tag_jobs(freehire_jobs(qterm))
                w.upsert_jobs(extra)
                extra = [j for j in extra if j.get("us")
                         and not employer_is_clearance_heavy(j.get("company", ""))]
                for j in extra:
                    j["entry_level"] = is_entry_level(j.get("title", ""))
                seen = {j.get("source_id") for j in filtered}
                filtered = filtered + [j for j in apply_facets(extra, **facets)
                                       if j.get("source_id") not in seen]
            except Exception:                            # noqa: BLE001 - search still returns the local matches
                pass
        page_jobs, count, page, per_page = paginate(filtered, a.get("page", 1), a.get("per_page", 30))
        refreshed = _sponsors().get_meta("jobs_refreshed_at")
        # An empty local store on a fresh install: start the first-open crawl (only when the feed is
        # genuinely absent, see _first_crawl_allowed) and say so, so the client shows "fetching
        # jobs" and re-polls instead of an empty board.
        store_empty = not _sponsors().get_meta("jobs_refreshed_at") and not _local_crawl_running()
        crawling = (_kick_local_crawl() if store_empty and _first_crawl_allowed(feed_url)
                    else _local_crawl_running())
        # The last crawl blew up and nothing has been stored since: say so (the client otherwise
        # sees crawling=False over an empty board with no reason), and the back-off above retries.
        crawl_error = _crawl_error() if (store_empty and not crawling) else ""
        # `degraded` = we WANTED the static feed but couldn't read it (feed_url set, no cache and the
        # download failed), so this small local copy is a stand-in, not the real board. The client
        # uses this to keep the last good board on screen and retry, instead of flashing a stale
        # 168-role fallback during a network blip. When feed_url is unset (or still the placeholder)
        # this is a genuine local-only install, so it is NOT degraded.
        return jsonify({"jobs": page_jobs, "count": count, "total": len(jobs),
                        "page": page, "per_page": per_page, "refreshed_at": refreshed,
                        "newest_at": _newest_arrival(jobs),
                        "stale": _is_stale(refreshed, hours=12),
                        "source": "local",
                        "degraded": bool(feed_url), "crawling": crawling,
                        "crawl_error": crawl_error or None})
    finally:
        w.close()


@app.get("/api/jobs/detail")
@_guard
def job_detail():
    """The full JD text + a pre-tailor MATCH for one role, fetched lazily when the
    person selects it (list_jobs stays light and omits the JD body)."""
    sid = (request.args.get("source_id") or "").strip()
    if not sid:
        return jsonify({"error": "missing source_id"}), 400
    # Static feed: a role from the shared list isn't in the local DB, so its JD comes from the
    # feed's JD shard (cached locally for a day), or failing that from the company's own public
    # board endpoint. The MATCH is still computed LOCALLY below (it reads the user's private
    # profile), so nothing personal leaves the machine. A role the feed doesn't list (an on-demand
    # search result, a saved snapshot) falls through to the local store -- and so does a listed
    # role whose description neither the shard nor the board yielded: the local store may hold the
    # full JD for a saved role, so it gets a look before we answer with an empty JD.
    feed_job = None
    if _feed_serving():
        try:
            feed_job = _feed_job_with_jd(sid)
            if feed_job is not None and (feed_job.get("jd_text") or "").strip():
                return _feed_detail_response(feed_job)
        except Exception:                            # noqa: BLE001 - fall back to local
            feed_job = None
            traceback.print_exc()
    w = _watchlist()
    try:
        job = w.get_job(sid)
    finally:
        w.close()
    if not job:
        if feed_job is not None:                     # the feed lists it; nobody has its JD text
            return _feed_detail_response(feed_job)
        return jsonify({"error": "That role is no longer in the feed."}), 404
    job = _sponsors().tag_jobs([job])[0]
    # A list-only row (Workday, SmartRecruiters) stored without its JD (before the refresh started
    # checking every kept row's description, or a bookmarked snapshot): fetch this one's JD from
    # the board's public detail endpoint and run the accessibility check it has not had yet. A
    # role that states a citizenship, clearance or no-sponsorship bar is dismissed, not shown;
    # otherwise the JD (and the posting's real public page, when the detail names one) is
    # persisted so the next open is instant and the row counts as checked from now on.
    from sourcing.quality import is_list_only
    if not (job.get("jd_text") or "").strip() and is_list_only(job):
        from sourcing.feedclient import fill_list_only_detail
        from sourcing.quality import role_excludes_international
        det = fill_list_only_detail(job)
        jd_body = det.get("jd_text") or ""
        if jd_body and role_excludes_international(job.get("company", ""), jd_body):
            try:
                w2 = _watchlist()
                try:
                    w2.dismiss(sid)
                finally:
                    w2.close()
            except Exception:                        # noqa: BLE001 - hiding it is what matters
                pass
            return jsonify({"error": "This posting states a citizenship, security-clearance or "
                                     "no-sponsorship requirement, so it is not open to international "
                                     "candidates and has been removed from your board."}), 404
        if jd_body:
            job.update(det)
            try:
                w2 = _watchlist()
                try:
                    w2.upsert_jobs([job])
                finally:
                    w2.close()
            except Exception:                        # noqa: BLE001 - persisting is a nicety
                pass
    # Clean on read too: jobs stored before the html_to_text fix hold literal HTML tags in jd_text,
    # so run them through the cleaner here. It's idempotent on already-clean text, so this is a safe
    # net that fixes existing rows without forcing a full re-fetch.
    from sourcing.ats import (freehire_fulltext, html_to_text, looks_truncated,
                              salary_from_text)
    jd = html_to_text((job.get("jd_text") or "")).strip()
    # Adzuna's API returns only a ~500-char PREVIEW of the posting (their terms forbid the full
    # text), which read as an abrupt mid-word cutoff. Pull the full JD from freehire when it has
    # the same role (matched by company + title), and persist it so it sticks; otherwise flag it a
    # preview so the UI can point to the posting instead of showing a broken cutoff.
    preview = False
    if looks_truncated(job, jd):
        # Only hit freehire ONCE per role per process: a role freehire doesn't carry would otherwise
        # re-search on every open (2-4s each). A miss is remembered so repeat opens are instant.
        if sid in _ENRICH_MISS:
            preview = True
        else:
            full = freehire_fulltext(job.get("title", ""), job.get("company", ""))
            if full and len(full) > len(jd) + 200:
                jd = html_to_text(full).strip()
                job["jd_text"] = jd
                w2 = _watchlist()
                try:
                    w2.upsert_jobs([{**job, "jd_text": jd}])   # persist the fuller text for next time
                finally:
                    w2.close()
            else:
                preview = True
                _ENRICH_MISS.add(sid)
    job["jd_text"] = jd
    # Fill pay from the posting's prose when the feed didn't carry a figure. Employers often
    # state it in the body ("The base pay for this position is $78,000 - $156,000"); this is the
    # read-time net that surfaces it on rows stored before salary extraction ran at ingest.
    if not (job.get("salary") or "").strip():
        job["salary"] = salary_from_text(jd)
    from sourcing.filters import salary_insight
    return jsonify({"job": job, "jd": jd, "match": _job_match(jd, job.get("title", "")),
                    "jd_preview": preview,
                    "pay_insight": salary_insight(job, _local_benchmarks())})


_SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|director|vp|vice[\s-]?president|head\s+of|distinguished)\b",
    re.I)
_ENTRY_RE = re.compile(
    r"\b(intern(?:ship)?|new[\s-]?grad(?:uate)?|entry[\s-]?level|junior|jr\.?|apprentice|trainee|"
    r"early[\s-]?career|recent\s+graduate|no\s+experience\s+(?:required|necessary))\b", re.I)
_YEARS_RE = re.compile("(\\d{1,2})\\s*\\+?\\s*(?:to|[-\\u2013\\u2014])?\\s*\\d{0,2}\\s*years?", re.I)


def _jd_seniority(title: str, jd: str) -> dict | None:
    """An HONEST heads-up about the role's level, the fit signal a keyword % misses -- most of our
    users are international STUDENTS / new grads, so 'this wants 7+ years' is decision-shaping.
    Entry cues win (they're the encouraging, load-bearing signal); else a senior title or a high
    years-of-experience bar flags senior. Mid-level roles get no note (nothing useful to say)."""
    title = title or ""
    if _ENTRY_RE.search(title) or _ENTRY_RE.search((jd or "")[:800]):
        return {"level": "entry", "note": "Entry-level / new-grad friendly."}
    years = [int(m.group(1)) for m in _YEARS_RE.finditer(jd or "") if int(m.group(1)) <= 25]
    max_years = max(years) if years else 0
    if _SENIOR_RE.search(title) or max_years >= 6:
        extra = f" (asks for ~{max_years}+ years)" if max_years >= 6 else ""
        return {"level": "senior", "note": f"Senior-level role{extra}."}
    return None


def _job_match(jd: str, title: str = "") -> dict | None:
    """A deterministic (no-model) preview of how the SAVED PROFILE fits this role. Beyond a raw
    keyword %, it separates MUST-HAVE gaps (skills named in the required section) from nice-to-have
    ones, gives an honest verdict weighted toward the must-haves, and flags the role's seniority --
    so the read is 'strong on the essentials, but it's a senior role', not a flat number.
    None when there's no JD or no profile yet (the UI then invites setup)."""
    if not jd:
        return None
    saved = _memory().load("default") or {}
    profile = saved.get("profile") or saved.get("essentials") or {}
    if not (profile.get("skills") or profile.get("experience") or profile.get("projects")):
        return None
    # Match on real SKILLS only, skill_terms excludes company names, locations, and
    # generic role/prose words, so the score and gaps read as credible skills, not noise.
    from tailoring.keywords import skill_terms, split_required_optional, supported_skills
    terms = skill_terms(jd)
    supported = supported_skills(jd, profile)
    supp = {t.lower() for t in supported}
    missing = [t for t in terms if t.lower() not in supp]
    # Must-have vs nice-to-have: skills named in the JD's required context matter more than ones
    # that only appear under "nice to have / preferred / bonus". Missing a must-have is the honest
    # signal; missing a nice-to-have rarely should.
    req_text, _opt = split_required_optional(jd)
    req_terms = {t.lower() for t in skill_terms(req_text)}
    required_missing = [t for t in missing if t.lower() in req_terms]
    optional_missing = [t for t in missing if t.lower() not in req_terms]
    req_total = sum(1 for t in terms if t.lower() in req_terms)
    req_covered = sum(1 for t in terms if t.lower() in req_terms and t.lower() in supp)
    overall = len(supported) / len(terms) if terms else 0.0
    req_ratio = (req_covered / req_total) if req_total else overall
    # Verdict driven mainly by the MUST-HAVES (what a screener actually gates on), with overall
    # coverage as a floor. Honest bands: a poor fit stays a poor fit even if it looks prestigious.
    verdict = ("strong" if req_ratio >= 0.8 and overall >= 0.55 else
               "good" if req_ratio >= 0.6 else
               "moderate" if (req_ratio >= 0.35 or overall >= 0.5) else "weak")
    return {
        "total": len(terms),
        "covered": len(supported),
        "ratio": round(overall, 3),
        "present": supported[:14],
        "missing": missing[:14],
        "required_missing": required_missing[:10],
        "optional_missing": optional_missing[:10],
        "required_total": req_total,
        "required_covered": req_covered,
        "verdict": verdict,
        "seniority": _jd_seniority(title, jd),
    }


@app.post("/api/jobs/upskill")
@_guard
def jobs_upskill():
    """Turn a role's skill GAPS into a short, concrete learning plan: how to close each gap fast
    (with proof) and the kind of resource to use. Gaps come from the detail's match; the saved
    profile grounds the advice. On-demand (a button), so the model runs only when asked."""
    b = request.json or {}
    gaps = [str(g) for g in (b.get("gaps") or []) if str(g).strip()][:8]
    # The must-have subset (from the detail's fit score): a screener gates on these, so the plan
    # prioritizes and labels them. Optional -- an older client that omits it still gets a plan.
    required = [str(g) for g in (b.get("required") or []) if str(g).strip()][:8]
    req = {g.lower() for g in required}
    role = str(b.get("role") or "").strip()[:140]
    if not gaps:
        return jsonify({"plan": []})
    profile = (_memory().load("default") or {}).get("profile") or {}
    try:
        result = _make_llm().upskill_plan(role, gaps, profile, required=required)
        if result.get("plan"):
            return jsonify(result)
    except Exception:                                    # noqa: BLE001 - fall through to the plan below
        traceback.print_exc()
    # Never dead-end the user: if the model is empty or the AI service is briefly down, still return
    # an honest, generic plan per gap (build something demonstrable + the kind of resource to use),
    # still with the must-haves labelled and sorted to the front.
    plan = [
        {"skill": g, "priority": "must-have" if g.lower() in req else "nice-to-have",
         "effort": "a weekend",
         "how": f"Build a small project that clearly uses {g} and put it on GitHub, so it is "
                "demonstrable on your resume.",
         "resource": "a free online course plus a portfolio project", "leverage": ""} for g in gaps]
    plan.sort(key=lambda p: 0 if p["priority"] == "must-have" else 1)
    return jsonify({"plan": plan})


@app.get("/api/apikey")
@_guard
def apikey_status():
    """Whether the CHOSEN provider's key is configured (masked). Drives the Connect-your-AI
    UI, which is provider-aware (Claude by default, OpenAI if selected)."""
    provider = _ai_provider()
    key = _cred(_PROVIDER_KEY[provider])
    masked = (key[:7] + "…" + key[-4:]) if len(key) > 14 else ("set" if key else "")
    # Editions (decided 2026-10-07): the official installer runs on SponsorJobs' own AI, so a
    # person never has to see a key; the open-source build from GitHub is bring-your-own. The
    # official UI hides the key ONLY while the managed AI is actually reachable, so an outage
    # still leaves the own-key path open instead of a dead end.
    edition = "official" if os.environ.get("TAILOR_EDITION") == "official" else "community"
    return jsonify({"configured": bool(key), "masked": masked, "provider": provider,
                    "provider_label": _PROVIDER_LABEL[provider], "edition": edition,
                    "managed": edition == "official" and _broker_reachable()})


@app.get("/api/whereami")
@_guard
def whereami():
    """The folder this install keeps the person's data in. Surfaced in Settings → Data &
    privacy so 'everything stays on your machine' is something they can CHECK, not just a
    claim we make. Local-first is the product's promise; this is the receipt."""
    return jsonify({"data_dir": str(_DATA)})


@app.post("/api/apikey")
@_guard
def apikey_save():
    """Verify a pasted key against the chosen PROVIDER with a 1-token test call, then persist
    it locally (git-ignored) and remember the provider choice."""
    body = request.json or {}
    key = (body.get("key") or "").strip()
    provider = (body.get("provider") or _ai_provider()).strip().lower()
    provider = provider if provider in _PROVIDERS else "anthropic"
    if not key:
        return jsonify({"ok": False, "error": f"Paste your {_PROVIDER_LABEL[provider]} API key first."}), 400
    ok, msg = _validate_key(provider, key)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    _save_cred(_PROVIDER_KEY[provider], key)
    _save_cred("AI_PROVIDER", provider)     # remember which provider this key is for
    return jsonify({"ok": True, "provider": provider})


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_stale(iso: str, hours: float) -> bool:
    """True if a stored ISO timestamp is missing or older than `hours`, drives the
    dashboard's automatic (no-click) refresh."""
    if not iso:
        return True
    from datetime import datetime, timezone
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return True
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() > hours * 3600


@app.get("/api/sponsors/status")
@_guard
def sponsors_status():
    from sourcing.sponsors import SPONSOR_DISCLAIMER
    db = _sponsors()
    s = db.stats()
    s["h1b_updated_at"] = db.get_meta("h1b_updated_at")
    s["perm_updated_at"] = db.get_meta("perm_updated_at")
    s["everify_updated_at"] = db.get_meta("everify_updated_at")
    s["jobs_refreshed_at"] = db.get_meta("jobs_refreshed_at")
    s["h1b_seeding"] = _h1b_seed_running()      # the bundled seed is still merging in the background
    # Which H-1B fiscal years the data actually spans, so the UI can show the vintage
    # ("H-1B: FY2020 to 2024") rather than just a download timestamp. Honest freshness.
    try:
        import json as _json
        fys = sorted(_json.loads(db.get_meta("h1b_fys_ingested") or "[]"))
    except (ValueError, TypeError):
        fys = []
    s["h1b_fiscal_years"] = fys
    s["h1b_fy_span"] = (f"FY{fys[0]}" if len(fys) == 1 else
                        f"FY{fys[0]} to {fys[-1]}") if fys else ""
    # Where the H-1B rows came from and which quarterly snapshot (if any) is in, so the UI
    # can say "bundled seed, FY2020 to 2023" or "snapshot 2026-09" instead of guessing.
    s["h1b_source"] = db.get_meta("h1b_source")
    s["h1b_snapshot_version"] = db.get_meta("h1b_snapshot_version")
    s["h1b_note"] = H1B_REFRESH_NOTE
    # The canonical "historical signal, not a guarantee" caveat, so the UI shows the same
    # honest framing next to the badges/filters rather than inventing its own copy.
    s["disclaimer"] = SPONSOR_DISCLAIMER
    return jsonify(s)


# ---- Extension (P6, FrogHire parity): a profile-vs-JD keyword MATCH estimate and a read-only
# TRACKING mirror. Both extension-guarded (like the sponsor lookup) so no arbitrary site can call
# them. The match is an honest keyword-COVERAGE estimate (reuses the ATS coverage logic), never a
# real ATS/employer score (§8); with no profile it says so instead of inventing a number. ----
_MATCH_NOTE = "A profile-vs-JD keyword coverage estimate, not a real ATS or employer score."


@app.post("/api/match/score")
@_guard
@_extension_only
def match_score():
    from tailoring.keywords import (extract_jd_terms, profile_text, skill_terms,
                                    split_required_optional, term_present)
    jd = str((request.json or {}).get("jd") or "").strip()
    profile = (_memory().load("default") or {}).get("profile") or {}
    has_profile = bool(profile.get("experience") or (profile.get("identity") or {}).get("name"))
    if not has_profile:
        return jsonify({"has_profile": False, "score": None, "matched": [], "missing": [],
                        "skills": [], "skills_covered": 0, "skills_total": 0,
                        "note": "Add your profile in SponsorJobs to see your match."})
    if not jd:
        return jsonify({"has_profile": True, "score": None, "matched": [], "missing": [],
                        "skills": [], "skills_covered": 0, "skills_total": 0, "note": _MATCH_NOTE})
    prof = profile_text(profile)
    terms = extract_jd_terms(jd)
    matched = [t for t in terms if term_present(t, prof)]
    matched_set = set(matched)
    missing = [t for t in terms if t not in matched_set]
    score = round(len(matched) / len(terms) * 100) if terms else None
    req_text, opt_text = split_required_optional(jd)
    skills = []
    for s in skill_terms(jd):
        # A skill is "optional" only if it appears in the optional context and NOT the required
        # one; otherwise treat it as required (conservative, so we never under-state a requirement).
        optional = term_present(s, opt_text) and not term_present(s, req_text)
        skills.append({"term": s, "required": not optional, "covered": term_present(s, prof)})
    skills_covered = sum(1 for s in skills if s["covered"])
    return jsonify({"has_profile": True, "score": score, "matched": matched, "missing": missing,
                    "skills": skills, "skills_covered": skills_covered, "skills_total": len(skills),
                    "note": _MATCH_NOTE})


@app.get("/api/tracking/summary")
@_guard
@_extension_only
def tracking_summary():
    """A read-only mirror of the app's application tracking for the extension popup: how many CVs
    the person has built, applied, and still has in review. The app stays the source of truth; this
    only counts. (SponsorJobs' real stages, honestly, rather than borrowing labels it doesn't track.)"""
    recs = _records()
    try:
        rows = recs.list(limit=1000)
    finally:
        recs.close()
    by: dict = {}
    applied = in_review = 0
    for r in rows:
        st = _record_data(r).get("status") or "ready"
        by[st] = by.get(st, 0) + 1
        if st == "applied":
            applied += 1
        elif st == "ready":
            in_review += 1
    return jsonify({"total": len(rows), "applied": applied, "in_review": in_review,
                    "by_status": by, "app_url": "http://127.0.0.1:57000/"})


@app.get("/api/sponsors/lookup")
@_guard
@_extension_only
def sponsors_lookup():
    """Visa-sponsor flags for ONE employer by name, the endpoint the browser extension
    calls to badge a job on LinkedIn/Indeed as you browse. Read-only public data.
    Extension-only (see _extension_only): reachable by the extension's background worker,
    not by an arbitrary web page.

    ``location`` is the ROLE's location and gates the badge, exactly as it does in the feed
    (sourcing.sponsors.tag_jobs). Sponsor data is per EMPLOYER, so without it a Spotify
    listing in London badged as "H-1B sponsor" on LinkedIn: true of Spotify, meaningless
    for that job, and an invitation to spend an application on an impossibility. When no
    location is passed we cannot judge, so the badge is withheld and ``sponsor_employer``
    still reports the honest employer-level fact.
    """
    company = (request.args.get("company") or "").strip()
    location = (request.args.get("location") or "").strip()
    rec = _sponsors().lookup(company) if company else None
    # "I couldn't read a location" is NOT "this job isn't in the US". looks_us() returns
    # False for both, which is right for gating a badge (withhold unless sure) and WRONG as
    # something to tell a person: it put "Sponsors in the US, not this role" on a job in
    # Dallas, Texas. That is worse than silence. It would talk someone out of a job they
    # could actually be sponsored for, which is the exact opposite of this product's point.
    # None means unknown, and the badge must say nothing rather than claim.
    in_us = looks_us(location) if location else None
    return _nostore(jsonify({
        "company": company,
        "matched": bool(rec),
        "matched_name": rec.display_name if rec else "",
        "visa": rec.badges() if (rec and in_us) else [],
        # The employer-level truth, separate from the per-role claim: Spotify does sponsor,
        # which is worth knowing even while looking at their London listing.
        "sponsor_employer": bool(rec),
        # The full sponsorship PROFILE (employer-level, always present when matched): the depth that
        # beats a yes/no checker -- H-1B volume + how recent, green-card (PERM) volume, E-Verify, and
        # cap-exempt. All from the public USCIS/DOL data we already hold. `visa` stays for back-compat.
        "profile": _sponsor_profile(rec) if rec else None,
        "role_in_us": in_us,               # True | False | None (couldn't read one)
        "employers_loaded": _sponsors().count(),
    }))


def _sponsor_profile(rec) -> dict:
    """Structured sponsorship profile for the rich extension/app panel (all fields honest, from the
    sponsor record). Kept employer-level: it reports what the company has done, not a role promise."""
    return {
        "h1b_approvals": rec.h1b_approvals,
        "h1b_first_fy": rec.h1b_first_fy or rec.h1b_last_fy,
        "h1b_last_fy": rec.h1b_last_fy,
        "fy_range": rec._fy_range() if rec.h1b_approvals > 0 else "",
        "perm_certs": rec.perm_certs,
        "e_verify": bool(rec.e_verify),
        "cap_exempt": bool(rec.cap_exempt),
        "state": rec.state or "",
        "industry": naics_industry(rec.naics),
    }


def _split_name(full: str) -> tuple[str, str]:
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _parse_address(addr: str) -> dict:
    """Best-effort split of a one-line address into street/city/state/zip so an ATS
    form's separate fields can be filled. Conservative: leaves a part blank rather than
    guessing wrong. e.g. '123 Lakeshore Ave, Chicago, IL 60615'."""
    import re
    out = {"street": "", "city": "", "state": "", "zip": ""}
    addr = (addr or "").strip()
    if not addr:
        return out
    m = re.search(r"\b([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b", addr)
    if m:
        out["state"], out["zip"] = m.group(1), m.group(2)
    parts = [p.strip() for p in addr.split(",") if p.strip()]
    if parts:
        out["street"] = parts[0]
    if len(parts) >= 2:
        out["city"] = parts[1]
    return out


def _saved_identity() -> dict:
    """The person's saved contact identity, for form autofill. Prefers a live builder
    session, else the durable saved profile ('default'). Read-only."""
    ident: dict = {}
    sess = _SESSION.get("s")
    if sess is not None:
        try:
            ident = dict((sess.essentials.get("identity") or {}))
        except Exception:
            ident = {}
    if not str(ident.get("name") or "").strip():
        mem = _memory().load("default") or {}
        ident = dict((mem.get("essentials") or {}).get("identity")
                     or (mem.get("profile") or {}).get("identity") or ident)
    return ident


def _saved_full_profile() -> dict:
    """The person's full saved PROFILE (identity, skills, experience, ...) for drafting
    cover letters / screening answers. Prefers a live builder session's working profile,
    else the durable built profile, else the intake essentials. Read-only."""
    sess = _SESSION.get("s")
    if sess is not None:
        try:
            p = sess._drafting_profile()
            if p:
                return p
        except Exception:
            pass
    mem = _memory().load("default") or {}
    prof = mem.get("profile") or {}
    if prof.get("experience") or (prof.get("identity") or {}).get("name"):
        return prof
    return mem.get("essentials") or prof


IDENTITY_KEYS = ("name", "email", "phone", "address", "linkedin", "github", "blog")


@app.get("/api/profile")
@_guard
def profile_view():
    """The person's whole saved PROFILE, for the 'My Profile' screen: editable contact
    identity + the CV sections it holds (education, experience, skills, projects,
    extracurricular, interests). The identity here is what powers autofill, every CV
    letterhead, and cover letters."""
    saved = _memory().load("default") or {}
    profile = saved.get("profile") or {}
    essentials = saved.get("essentials") or {}
    identity = dict(profile.get("identity") or essentials.get("identity") or {})
    return jsonify({
        "has_profile": bool(identity.get("name") or profile.get("experience")
                            or essentials.get("experience")),
        "identity": {k: str(identity.get(k) or "") for k in IDENTITY_KEYS},
        "education": profile.get("education") or essentials.get("education") or [],
        "experience": profile.get("experience") or essentials.get("experience") or [],
        "skills": profile.get("skills") or {},
        "projects": profile.get("projects") or [],
        "extracurricular": profile.get("extracurricular") or [],
        "interests": profile.get("interests") or "",
        "summary": str(profile.get("summary") or essentials.get("summary") or ""),
        "skills_input": essentials.get("skills_input") or [],
        "declined": essentials.get("declined") or [],
    })


_DECLINABLE = ("github", "blog", "linkedin", "projects", "courses", "address",
               "extracurricular", "interests")


def _prof_rows(rows, keys, bullet_key="bullets"):
    """Clean editor rows: keep dicts, strip strings, drop rows with no content."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        e = {k: str(r.get(k) or "").strip() for k in keys}
        bl = [str(b).strip() for b in (r.get(bullet_key) or []) if str(b).strip()]
        if bl:
            e[bullet_key] = bl
        if any(e.get(k) for k in keys):
            out.append(e)
    return out


@app.post("/api/profile/sections")
@_guard
def profile_sections_save():
    """Save the My Profile section edits into BOTH saved bags (profile + essentials)
    coherently, the productized form of the SQL surgery the Amazon case study needed
    three times (obs #15: 'no profile editor'). Refused while a CV session is open:
    its per-turn autosave would silently overwrite these edits (the same overwrite
    class as the extractor-echo bug)."""
    if _SESSION.get("s") is not None:
        return jsonify({"error": "Finish or close the resume you're building first, "
                                 "its autosave would overwrite these edits."}), 409
    b = request.json or {}
    mem = _memory()
    saved = mem.load("default") or {}
    profile = saved.get("profile") or {}
    ess = saved.get("essentials") or {}

    edu = _prof_rows(b.get("education"), ("school", "degree", "date", "location", "courses"))
    flat = _prof_rows(b.get("experience"), ("org", "title", "dates", "location"))
    projects = _prof_rows(b.get("projects"), ("org", "link", "location", "dates"))
    for p in projects:                     # editor projects are personal/portfolio work
        p.setdefault("bullets", [])
        p["personal"] = True
    extra = _prof_rows(b.get("extracurricular"), ("title", "date"))
    for x in extra:
        x.setdefault("bullets", [])
    summary = str(b.get("summary") or "").strip()
    interests = str(b.get("interests") or "").strip()
    skills_input = [str(s).strip() for s in (b.get("skills_input") or []) if str(s).strip()]
    declined = [d for d in (b.get("declined") or []) if d in _DECLINABLE]

    for bag in (profile, ess):
        bag["education"] = [dict(e) for e in edu]
        bag["projects"] = [dict(p) for p in projects]
        bag["extracurricular"] = [dict(x) for x in extra]
        bag["summary"] = summary
        bag["interests"] = interests
    # The two bags keep their own experience shapes: essentials flat, profile grouped.
    ess["experience"] = [dict(r) for r in flat]
    profile["experience"] = [
        {"org": r.get("org", ""), "location": r.get("location", ""),
         "roles": [{"title": r.get("title", ""), "dates": r.get("dates", ""),
                    "bullets": list(r.get("bullets") or [])}]}
        for r in flat
    ]
    ess["skills_input"] = skills_input
    ess["declined"] = declined

    mem.save("default", profile, ess, saved.get("history") or [])
    return profile_view()


@app.post("/api/profile/from_cv")
@_guard
def profile_from_cv():
    """Profile-first: build the whole PROFILE from an uploaded resume, no JD needed.
    Parse one artifact, prefill everything. Uses the real model (needs the key)."""
    from datetime import datetime

    from werkzeug.utils import secure_filename

    from intake.cv_import import SUPPORTED_EXTS, extract_text

    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "Choose a resume file (Word, PDF, PowerPoint, or an image)."}), 400
    name = secure_filename(f.filename) or "resume"
    if Path(name).suffix.lower() not in SUPPORTED_EXTS:
        return jsonify({"error": f"Unsupported file type '{Path(name).suffix.lower()}'. "
                        "Use Word, PDF, PowerPoint, or an image."}), 400
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}"
    f.save(str(dest))       # raw backup, kept, never deleted
    text = extract_text(dest)
    if not (text or "").strip():
        return jsonify({"error": "Couldn't read any text from that file. Try a Word or PDF export."}), 400
    llm = _make_llm()       # real model; raises a clear error if the key/connectivity is missing
    profile = _coerce_profile(llm.extract_profile(text))
    if not (profile.get("identity") or profile.get("experience")):
        return jsonify({"error": "Couldn't find a profile in that file, try another resume."}), 400
    # Projects conventionally carry a "Personal Project" location (matches the rest of the
    # pipeline) so a one-click autonomous build isn't stopped by a missing project location.
    for pr in profile.get("projects") or []:
        if isinstance(pr, dict) and not str(pr.get("location") or "").strip():
            pr["location"] = "Personal Project"
    # Non-destructive: never wipe a richer existing profile or its history, fold the résumé
    # into what's saved (fill gaps, add new entries), keeping the conversation history.
    saved = _memory().load("default") or {}
    existing = saved.get("profile") or {}
    if existing.get("experience") or (existing.get("identity") or {}).get("name"):
        profile = _merge_profiles(existing, profile)
    _memory().save("default", profile, _essentials_from_profile(profile), saved.get("history") or [])
    ident = profile.get("identity") or {}
    return jsonify({"ok": True, "profile": profile, "summary": {
        "name": ident.get("name", ""),
        "roles": sum(len(e.get("roles") or [e]) for e in profile.get("experience") or []),
        "skills": len(profile.get("skills") or {}),
        "education": len(profile.get("education") or []),
    }})


def _coerce_profile(profile) -> dict:
    """Defensively normalize a model-parsed profile into the canonical shape, so odd LLM
    output (a role as a dict, experience/identity as a string, a skills list) never crashes
    the flatten/merge/build path. Anything unsalvageable is dropped, not raised on."""
    if not isinstance(profile, dict):
        return {}
    out = dict(profile)
    ident = profile.get("identity")
    out["identity"] = ident if isinstance(ident, dict) else {}
    exp = []
    for e in profile.get("experience") or []:
        if not isinstance(e, dict):
            continue
        roles = e.get("roles")
        if isinstance(roles, dict):
            roles = [roles]
        if isinstance(roles, list):
            e = {**e, "roles": [r for r in roles if isinstance(r, dict)]}
        exp.append(e)
    out["experience"] = exp
    for sec in ("education", "projects", "extracurricular"):
        out[sec] = [x for x in (profile.get(sec) or []) if isinstance(x, dict)]
    sk = profile.get("skills")
    out["skills"] = sk if isinstance(sk, dict) else {}
    for k in ("summary", "interests"):
        v = profile.get(k)
        out[k] = v if isinstance(v, str) else ""
    return out


def _merge_profiles(base: dict, new: dict) -> dict:
    """Fold a freshly-parsed resume into an existing profile WITHOUT losing anything: add
    new entries, fill identity gaps (a fresh résumé's non-empty contact wins), union skills,
    and keep existing summary/interests when present."""
    out = dict(base)
    ident = dict(base.get("identity") or {})
    for k, v in (new.get("identity") or {}).items():
        if str(v or "").strip():
            ident[k] = v
    out["identity"] = ident
    for k in ("summary", "interests"):
        out[k] = base.get(k) or new.get(k) or ""

    def _key(d):
        d = d if isinstance(d, dict) else {}
        return (str(d.get("org") or d.get("company") or d.get("school") or d.get("title") or "").strip().lower(),
                str(d.get("title") or d.get("degree") or d.get("dates") or "").strip().lower())
    for sec in ("experience", "education", "projects", "extracurricular"):
        merged = [x for x in (base.get(sec) or []) if isinstance(x, dict)]
        seen = {_key(x) for x in merged}
        for x in (new.get(sec) or []):
            if isinstance(x, dict) and _key(x) not in seen:
                merged.append(x)
                seen.add(_key(x))
        out[sec] = merged
    skills = dict(new.get("skills") or {})
    skills.update(base.get("skills") or {})   # existing wins on a shared label
    out["skills"] = skills
    return out


def _essentials_from_profile(profile: dict) -> dict:
    """Flatten a built profile back to the intake 'essentials' skeleton (identity,
    education, one flat entry per role) so a later tailoring session can reuse it.
    Defensive against odd shapes, a bad entry is skipped, never crashes."""
    flat = []
    for e in profile.get("experience") or []:
        if not isinstance(e, dict):
            continue
        roles = e.get("roles")
        roles = roles if isinstance(roles, list) else [e]
        for r in roles:
            if not isinstance(r, dict):
                continue
            flat.append({"org": e.get("org") or e.get("company") or "",
                         "location": e.get("location", ""), "title": r.get("title", ""),
                         "dates": r.get("dates", ""), "bullets": list(r.get("bullets") or [])})
    ident = profile.get("identity")
    edu = profile.get("education")
    return {"identity": dict(ident) if isinstance(ident, dict) else {},
            "education": [x for x in edu if isinstance(x, dict)] if isinstance(edu, list) else [],
            "experience": flat}


@app.post("/api/profile/identity")
@_guard
def profile_identity_save():
    """Save edited contact identity. Writes BOTH the essentials.identity (read by
    autofill) and profile.identity (read by the CV letterhead + cover letters), via a
    read-modify-write that preserves history and the CV sections, so the change is seen
    everywhere at once."""
    body = request.json or {}
    ident = {k: str(body.get(k) or "").strip() for k in IDENTITY_KEYS}
    mem = _memory()
    try:
        saved = mem.load("default") or {}
        profile = saved.get("profile") or {}
        essentials = saved.get("essentials") or {"identity": {}, "education": [], "experience": []}
        history = saved.get("history") or []
        profile.setdefault("identity", {}).update(ident)
        essentials.setdefault("identity", {}).update(ident)
        mem.save("default", profile, essentials, history)
    finally:
        mem.close()
    # Keep a live builder session (if any) consistent so it doesn't overwrite on autosave.
    sess = _SESSION.get("s")
    if sess is not None:
        try:
            sess.essentials.setdefault("identity", {}).update(ident)
            if isinstance(getattr(sess, "profile", None), dict):
                sess.profile.setdefault("identity", {}).update(ident)
        except Exception:
            pass
    return jsonify({"ok": True, "identity": ident})


_PREFS_PATH = _DATA / "app_prefs.json"
_YN = {"", "Yes", "No"}


def _load_prefs() -> dict:
    """Application answers the person saved once and reuses on every form (work
    authorization, sponsorship need, EEO decline). Local JSON, git-ignored."""
    import json
    try:
        return json.loads(_PREFS_PATH.read_text(encoding="utf-8")) if _PREFS_PATH.exists() else {}
    except Exception:
        return {}


def _save_prefs(prefs: dict) -> dict:
    import json

    def _txt(v, n):
        return str(v or "").strip()[:n]

    clean = {
        "work_authorized": prefs.get("work_authorized") if prefs.get("work_authorized") in _YN else "",
        "needs_sponsorship": prefs.get("needs_sponsorship") if prefs.get("needs_sponsorship") in _YN else "",
        # More answers the person gives on nearly every form, saved once and reused so the
        # autofill fills the whole thing and they just review + submit (Path A). Yes/No and
        # short free-text only; length-capped so a stray paste can't bloat the store.
        "willing_to_relocate": prefs.get("willing_to_relocate") if prefs.get("willing_to_relocate") in _YN else "",
        "over_18": prefs.get("over_18") if prefs.get("over_18") in _YN else "",
        "years_experience": _txt(prefs.get("years_experience"), 20),
        "desired_salary": _txt(prefs.get("desired_salary"), 40),
        "earliest_start": _txt(prefs.get("earliest_start"), 60),
        "hear_about_us": _txt(prefs.get("hear_about_us"), 60),
        "eeo_decline": bool(prefs.get("eeo_decline")),
    }
    _DATA.mkdir(parents=True, exist_ok=True)
    _PREFS_PATH.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    return clean


@app.get("/api/profile/autofill")
@_guard
@_extension_only
def profile_autofill():
    """The person's saved contact identity + reusable application answers, normalized
    into what an application form asks for: contact fields (first/last name, email,
    phone, address parts, links), the work-authorization / sponsorship screening
    answers, and an EEO 'decline to self-identify' preference. The browser extension
    fetches this to PRE-FILL a job application form; the PERSON reviews and submits it
    themselves (CLAUDE.md §7, no unattended auto-submission). CORS-open so the
    extension, running on an ATS site, can reach this local server."""
    ident = _saved_identity()
    name = str(ident.get("name") or "").strip()
    first, last = _split_name(name)
    addr = _parse_address(str(ident.get("address") or ""))
    fields = {
        "name": name, "first_name": first, "last_name": last,
        "email": str(ident.get("email") or "").strip(),
        "phone": str(ident.get("phone") or "").strip(),
        "address": str(ident.get("address") or "").strip(),
        "street": addr["street"], "city": addr["city"],
        "state": addr["state"], "zip": addr["zip"],
        "linkedin": str(ident.get("linkedin") or "").strip(),
        "github": str(ident.get("github") or "").strip(),
        "website": str(ident.get("blog") or ident.get("website") or "").strip(),
    }
    prefs = _load_prefs()
    screening = {k: prefs[k] for k in ("work_authorized", "needs_sponsorship",
                                       "willing_to_relocate", "over_18")
                 if prefs.get(k) in ("Yes", "No")}
    # Short free-text answers the extension can type into a matching field (years of
    # experience, desired salary, earliest start, how they heard) so the form is filled,
    # not just contact info.
    answers = {k: prefs[k] for k in ("years_experience", "desired_salary", "earliest_start",
                                     "hear_about_us")
               if str(prefs.get(k) or "").strip()}
    # The person's tailored CVs, so the extension can offer a résumé/variant selector (P6): the
    # contact fields are shared, but this points them at the right PDF to attach for THIS role.
    resumes = []
    try:
        recs = _records()
        try:
            for r in recs.list(limit=12):
                resumes.append({"id": r["id"], "role": r.get("role") or "",
                                "company": r.get("company") or ""})
        finally:
            recs.close()
    except Exception:
        pass
    return _nostore(jsonify({
        "ok": True,
        "loaded": bool(name or fields["email"]),
        "fields": {k: v for k, v in fields.items() if v},
        "screening": screening,
        "answers": answers,
        "eeo": {"decline": bool(prefs.get("eeo_decline"))},
        "resumes": resumes,
        "app_url": "http://127.0.0.1:57000/",
    }))


@app.get("/api/profile/prefs")
@_guard
@_extension_only
def profile_prefs_get():
    """The saved reusable application answers (work auth / sponsorship / EEO decline).
    Set from the extension popup; read here to pre-fill forms. Read-only."""
    return _nostore(jsonify({"ok": True, "prefs": _load_prefs()}))


@app.post("/api/profile/prefs")
@_guard
@_extension_only
def profile_prefs_set():
    """Save the reusable application answers. Values are validated to Yes/No/blank so a
    form is never filled with a free-text guess for a screening question."""
    return _nostore(jsonify({"ok": True, "prefs": _save_prefs(request.json or {})}))


@app.post("/api/profile/draft")
@_guard
@_extension_only
def profile_draft():
    """Draft, from the person's SAVED profile, answers to an application form's free-text
    screening questions (and optionally a cover letter) for the extension's assisted
    apply. Body: {jd, questions:[str], cover_letter:bool}. Grounded in real profile
    material only (CLAUDE.md §5/§8); the person reviews and submits. Uses the real model,
    so it needs the internet + the user's key, like every other tailoring call."""
    body = request.json or {}
    jd = str(body.get("jd") or "").strip()
    questions = [str(q).strip() for q in (body.get("questions") or []) if str(q).strip()]
    want_cover = bool(body.get("cover_letter"))
    profile = _saved_full_profile()
    loaded = bool((profile.get("identity") or {}).get("name") or profile.get("experience"))
    out = {"ok": True, "loaded": loaded, "answers": [], "cover_letter": None}
    if loaded and (questions or want_cover):
        from drafting import cover_letter as _cover_letter
        from drafting import screening_answers as _screening_answers
        llm = _make_llm()   # real model; raises a clear error if key/connectivity missing
        if questions:
            out["answers"] = _screening_answers(jd, profile, llm, questions)["answers"]
        if want_cover:
            out["cover_letter"] = _cover_letter(jd, profile, llm)["cover_letter"]
    return _nostore(jsonify(out))


@app.post("/api/profile/referral")
@_guard
@_extension_only
def profile_referral():
    """Draft, from the person's SAVED profile, a short referral-request message they can
    send to a real person at the target company. Body: {role, company, jd, recipient_name,
    relationship}. The extension also builds a LinkedIn people-search link so the person
    finds the recipient themselves and sends it themselves: nothing is scraped, nothing is
    auto-sent (CLAUDE.md privacy line). Grounded in real profile material only; uses the
    real model, so it needs connectivity like every other tailoring call."""
    body = request.json or {}
    role = str(body.get("role") or "").strip()
    company = str(body.get("company") or "").strip()
    jd = str(body.get("jd") or "").strip()
    recipient = str(body.get("recipient_name") or "").strip()
    relationship = str(body.get("relationship") or "").strip()
    profile = _saved_full_profile()
    loaded = bool((profile.get("identity") or {}).get("name") or profile.get("experience"))
    out = {"ok": True, "loaded": loaded, "message": None}
    if loaded and (role or company):
        from drafting import referral_message as _referral_message
        llm = _make_llm()   # real model; raises a clear error if key/connectivity missing
        out["message"] = _referral_message(role, company, profile, llm, jd_text=jd,
                                            recipient_name=recipient,
                                            relationship=relationship)["message"]
    return _nostore(jsonify(out))


@app.get("/api/inbox/status")
@_guard
def inbox_status():
    """Is the person's Gmail wired up? Read-only diagnosis for the Inbox panel: the
    optional Google libraries, the OAuth client file, and the local token. Nothing here
    touches the mailbox."""
    from inbox import gmail as g
    try:
        g._require_google()
        libs = True
    except RuntimeError:
        libs = False
    creds = g._CREDS_PATH.exists()
    token = g.token_present()   # sees a lock-held token too, not just the plaintext file
    return jsonify({
        "libs": libs, "creds": creds, "token": token,
        "configured": libs and token,
        "creds_path": str(g._CREDS_PATH),
        "auto_verify": bool(_prefs().get("inbox_auto_verify")),
        "query": g.DEFAULT_QUERY,
    })


# ---- Telegram: connect from the UI (Connections settings), not by hand-editing creds. ----
@app.get("/api/telegram/status")
@_guard
def telegram_status():
    token, chat = _cred("TELEGRAM_BOT_TOKEN"), _cred("TELEGRAM_CHAT_ID")
    return jsonify({"configured": bool(token and chat), "chat": (chat[-4:] if chat else "")})


@app.post("/api/telegram")
@_guard
def telegram_save():
    """Verify a bot token with Telegram's getMe, then save token + chat id locally."""
    b = request.json or {}
    token, chat = (b.get("token") or "").strip(), (b.get("chat_id") or "").strip()
    if not token or not chat:
        return jsonify({"ok": False, "error": "Enter both the bot token and your chat ID."}), 400
    try:
        import requests
        data = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=8).json()
    except Exception:
        return jsonify({"ok": False, "error": "Couldn't reach Telegram to verify the token. Check your connection."}), 400
    if not data.get("ok"):
        return jsonify({"ok": False, "error": "That bot token was rejected by Telegram. Copy it again from @BotFather."}), 400
    _save_cred("TELEGRAM_BOT_TOKEN", token)
    _save_cred("TELEGRAM_CHAT_ID", chat)
    return jsonify({"ok": True, "bot": (data.get("result") or {}).get("username", "")})


@app.post("/api/telegram/disconnect")
@_guard
def telegram_disconnect():
    _revoke_cred("TELEGRAM_BOT_TOKEN")
    _revoke_cred("TELEGRAM_CHAT_ID")
    return jsonify({"ok": True})


# ---- GitHub (Connections): connect the person's OWN account with a Personal Access Token so
# they can publish a verified project. Token validated with GitHub and stored locally, like Telegram. ----
@app.get("/api/github/status")
@_guard
def github_status():
    token = _cred("GITHUB_TOKEN")
    return jsonify({"configured": bool(token), "login": _cred("GITHUB_LOGIN") or ""})


@app.post("/api/github")
@_guard
def github_save():
    """Verify a Personal Access Token with GitHub, then save it (and the login) locally."""
    from ui.github_publish import validate_token
    token = str((request.json or {}).get("token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "Paste a GitHub personal access token."}), 400
    res = validate_token(token)
    if not res.get("ok"):
        return jsonify({"ok": False, "error": res.get("error", "Token rejected.")}), 400
    _save_cred("GITHUB_TOKEN", token)
    _save_cred("GITHUB_LOGIN", res.get("login", ""))
    return jsonify({"ok": True, "login": res.get("login", "")})


@app.post("/api/github/disconnect")
@_guard
def github_disconnect():
    _revoke_cred("GITHUB_TOKEN")
    _revoke_cred("GITHUB_LOGIN")
    return jsonify({"ok": True})


# ---- Memory (P1): the person owns their local memory, view / export / erase it (CLAUDE.md §5).
# Three layers: the SQLite conversation row + distilled facts, and the palace transcript. ----
def _palace(person: str = "default") -> PalaceMemory:
    return PalaceMemory(PALACE_DIR, person=person)


def _career_highlights_for_job(role: str, company: str, jd: str,
                               min_similarity: float = 0.3, limit: int = 4) -> list:
    """The diary memories most relevant to THIS job, kept only when the match is STRONG and on-point,
    so tailoring can autonomously draw on the person's real past accomplishments without asking them
    anything, and without dredging up noise. Grounded: these are their own captured statements, never
    invented. Returns [{text, similarity}]; empty when nothing is relevant or the semantic layer is
    off (verified 2026-08-03: on-point hits score ~0.46-0.57, off-topic ~0.1, so 0.3 separates them
    cleanly). This is the shared retrieval engine for the autonomous CV path + conversational surfacing."""
    query = " ".join(x for x in (role or "", company or "", (jd or "")[:200]) if x).strip()
    if not query:
        return []
    hits = _palace().recall(query, n=max(limit * 2, 8)) or []
    strong = [{"text": str(h.get("text", "")).strip(),
               "similarity": round(float(h.get("similarity", 0)), 3)}
              for h in hits if float(h.get("similarity", 0)) >= min_similarity and str(h.get("text", "")).strip()]
    return strong[:limit]


@app.get("/api/memory")
@_guard
def memory_view():
    """A readable summary of everything remembered: the distilled facts/preferences and how many
    conversation turns are on record."""
    mem = _memory()
    saved = mem.load("default") or {}
    return jsonify({
        "facts": mem.list_facts("default", status="active"),
        "retracted": len(mem.list_facts("default", status="retracted")),
        "turns": len(_palace().load_history()),
        "has_profile": bool((saved.get("profile") or {}).get("experience")),
    })


@app.get("/api/memory/export")
@_guard
def memory_export():
    """Export the full local memory as JSON (facts + transcript + saved profile), so the person
    can keep or move their own data. Everything stays local until they choose to save the file."""
    from datetime import datetime
    mem = _memory()
    saved = mem.load("default") or {}
    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "facts": mem.list_facts("default", status=None),
        "transcript": _palace().load_history(),
        "profile": saved.get("profile") or {},
        "essentials": saved.get("essentials") or {},
    }
    resp = _nostore(jsonify(payload))
    resp.headers["Content-Disposition"] = "attachment; filename=tailor-memory.json"
    return resp


@app.delete("/api/memory/fact/<int:fact_id>")
@_guard
def memory_delete_fact(fact_id):
    """Delete one distilled fact/preference (granular user control)."""
    _memory().delete_fact("default", fact_id)
    return jsonify({"ok": True})


@app.post("/api/memory/ingest")
@_guard
def memory_ingest():
    """Add something to the person's durable CAREER MEMORY (the lifelong diary): a note or event they
    type, or a document they upload. Stored locally + indexed so it can be recalled later for CVs,
    cover letters, and 'tell me about a time' answers. Typed text is stored as-is; an uploaded
    document's text is extracted LOCALLY (best-effort; the file never leaves the machine and is not
    kept). Body: JSON {text, source} OR a multipart file."""
    text, source = "", "note"
    f = request.files.get("file")
    if f is not None and f.filename:
        if request.content_length and request.content_length > _RECORDING_MAX:
            return jsonify({"error": "That file is too large."}), 413
        source = f.filename
        import os as _os
        import tempfile
        from intake.cv_import import extract_text   # local: PDF/DOCX/PPTX/image+OCR, base deps
        tmp = tempfile.mktemp(suffix=(_os.path.splitext(f.filename)[1] or ".bin"))
        try:
            f.save(tmp)
            text = (extract_text(tmp) or "").strip()
        finally:
            try:
                _os.unlink(tmp)
            except OSError:
                pass
        if not text:
            return jsonify({"error": "Couldn't read any text from that file. Try a PDF, Word, or "
                                     "PowerPoint file, or paste the text instead."}), 422
    else:
        b = request.json or {}
        text = str(b.get("text") or "").strip()
        source = (str(b.get("source") or "note").strip() or "note")
    if not text:
        return jsonify({"error": "Add a note or a document to remember."}), 400
    chunks = _palace().ingest_note(text, source=source)
    return jsonify({"ok": True, "stored_chunks": chunks, "chars": len(text), "source": source})


@app.post("/api/memory/assess")
@_guard
def memory_assess():
    """On demand (never automatic, so the AI cost only fires when the person asks), judge whether a
    note describes a CV-worthy accomplishment and, if so, frame it. The 'we notice what matters'
    nudge for the career diary. Body: {text}. Uses the real model; grounds framing only in the note."""
    text = str((request.json or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "Write something to check first."}), 400
    return jsonify({"ok": True, "assessment": _make_llm().assess_cv_worthiness(text)})


def _derive_essentials_from_memory(person: str = "default", max_turns: int = 80) -> dict:
    """Compute the resume's STRUCTURED facts (identity, education, experience) FROM MEMORY, by
    replaying the person's captured statements through the same grounded extractor the live intake
    uses. This is the 'profile is computed, not a form you fill' path: the structure is DERIVED from
    what they told us, held internally, never a page. Invents nothing (extract_intake keeps names,
    companies, dates, schools exactly as stated). Bounded to the most recent statements to cap cost."""
    history = _palace(person).load_history()
    turns = [h for h in history if h.get("role") in ("user", "note") and str(h.get("content") or "").strip()]
    if not turns:
        return {"identity": {}, "education": [], "experience": []}
    llm = _make_llm()
    known = {"identity": {}, "education": [], "experience": []}
    for t in turns[-max_turns:]:
        try:
            res = llm.extract_intake("", known, [{"role": "user", "content": t["content"]}])
            if isinstance(res, dict) and isinstance(res.get("essentials"), dict):
                known = res["essentials"]
        except Exception:
            continue   # one bad turn never breaks the derivation
    return known


def _memory_first_profile(role: str = "", company: str = "", jd: str = "") -> dict:
    """Build the CV's input FROM MEMORY instead of the stored profile form: derive the structured
    essentials from what the person told us (identity/roles/dates/education), then fold in the
    STRONGEST recalled accomplishments for THIS job (grounded, attached to the right role). This is
    the 'memory-first' tailoring input, built ALONGSIDE the form path so we can prove quality before
    switching the default. Returns {essentials, drew_on}: drew_on is what it pulled from the career
    memory (for the 'from your career memory' transparency, and never invented)."""
    essentials = _derive_essentials_from_memory()
    highlights = _career_highlights_for_job(role, company, jd)
    if highlights:
        try:
            merged = _make_llm().absorb_enrichment(
                jd or "", essentials, ". ".join(h.get("text", "") for h in highlights if h.get("text")))
            if isinstance(merged, dict):
                essentials = merged
        except Exception:
            pass   # fall back to the derived essentials; a bad enrich never loses the facts
    return {"essentials": essentials, "drew_on": highlights}


@app.post("/api/memory/tailoring-input")
@_guard
def memory_tailoring_input():
    """The tailoring INPUT built entirely from memory (derived facts + recalled wins for THIS job),
    exposed so we can prove memory-first CVs hold up BEFORE retiring the profile form. Body:
    {role, company, jd}. Grounded; nothing invented."""
    b = request.json or {}
    return jsonify({"ok": True, **_memory_first_profile(
        str(b.get("role") or ""), str(b.get("company") or ""), str(b.get("jd") or ""))})


@app.get("/api/memory/derived-profile")
@_guard
def memory_derived_profile():
    """The resume's structured facts COMPUTED from memory (identity / roles+dates / education), not a
    form the person maintains. Behind the scenes this is what tailoring will read once the profile
    page is retired; exposed here so we can verify the derivation holds up BEFORE removing the page.
    Grounded; nothing invented."""
    return jsonify({"ok": True, "essentials": _derive_essentials_from_memory()})


@app.post("/api/memory/highlights")
@_guard
def memory_highlights():
    """The career-diary memories relevant to a specific job, so the app can autonomously draw on the
    person's real past accomplishments when tailoring (no decisions asked of them) and, in
    conversational mode, mention what it remembered. Body: {role, company, jd}. Grounded; strong
    matches only."""
    b = request.json or {}
    hi = _career_highlights_for_job(str(b.get("role") or ""), str(b.get("company") or ""),
                                    str(b.get("jd") or ""))
    return jsonify({"ok": True, "highlights": hi})


@app.post("/api/memory/erase")
@_guard
def memory_erase():
    """Erase everything remembered: distilled facts, the SQLite conversation row, and the palace
    transcript + semantic index. Local and irreversible; the person chose it."""
    mem = _memory()
    mem.erase_facts("default")
    mem.delete("default")
    _palace().erase()
    _SESSION.pop("s", None)   # drop any live in-memory session so it can't re-save the old state
    return jsonify({"ok": True})


# ---- Guided project builder: the defend-your-work gate. A project cannot be marked
# CV-ready until the person passes a test showing they GENUINELY understand what they built.
# This is the honesty keystone (not AI-built filler they can't explain in an interview). ----
_PROJECTS_FILE = _DATA / "projects.json"


def _load_projects() -> list:
    try:
        return json.loads(_PROJECTS_FILE.read_text(encoding="utf-8")).get("projects", [])
    except Exception:
        return []


def _save_projects(projects: list) -> None:
    _PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PROJECTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"projects": projects}, indent=2), encoding="utf-8")
    tmp.replace(_PROJECTS_FILE)


def _project_public(p: dict) -> dict:
    return {"id": p.get("id"), "title": p.get("title", ""), "summary": p.get("summary", ""),
            "tech": p.get("tech", ""), "status": p.get("status", "draft"), "result": p.get("result"),
            "plan": p.get("plan"), "done": p.get("done") or [], "repo_url": p.get("repo_url") or ""}


@app.get("/api/projects")
@_guard
def projects_list():
    return jsonify({"projects": [_project_public(p) for p in _load_projects()]})


@app.post("/api/projects")
@_guard
def projects_create():
    import uuid
    b = request.json or {}
    title = (b.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Give the project a title."}), 400
    p = {"id": uuid.uuid4().hex[:12], "title": title,
         "summary": (b.get("summary") or "").strip(), "tech": (b.get("tech") or "").strip(),
         "status": "draft"}
    projects = _load_projects()
    projects.append(p)
    _save_projects(projects)
    return jsonify({"project": _project_public(p)})


@app.post("/api/projects/suggest")
@_guard
def projects_suggest():
    """Suggest concrete projects to BUILD that close the gap between the saved profile and a
    target role/JD, so the person isn't inventing a project from scratch. Suggestions are ideas
    only; the person picks one, which creates a draft that still has to pass the defend gate."""
    target = str((request.json or {}).get("target") or "").strip()
    saved = _memory().load("default") or {}
    profile = saved.get("profile") or {}
    return jsonify(_make_llm().suggest_buildable_projects(profile, target))


@app.delete("/api/projects/<pid>")
@_guard
def projects_delete(pid):
    _save_projects([p for p in _load_projects() if p.get("id") != pid])
    return jsonify({"ok": True})


@app.post("/api/projects/<pid>/test")
@_guard
def projects_test(pid):
    """Generate the defense test for a project, grounded in what they built."""
    projects = _load_projects()
    p = next((x for x in projects if x.get("id") == pid), None)
    if not p:
        return jsonify({"error": "Project not found."}), 404
    test = _make_llm().generate_project_test(p)
    p["test"] = test
    _save_projects(projects)
    return jsonify(test)


# ---- Guided build-and-teach loop: help the person BUILD the project and TEACH them as they go,
# so their understanding is real by the time they reach the defend gate. Guidance, not a solution. ----
@app.post("/api/projects/<pid>/plan")
@_guard
def projects_plan(pid):
    """Generate (and store) an ordered build plan, each milestone teaching the concept behind it."""
    projects = _load_projects()
    p = next((x for x in projects if x.get("id") == pid), None)
    if not p:
        return jsonify({"error": "Project not found."}), 404
    plan = _make_llm().generate_build_plan(p)
    p["plan"] = plan
    p.setdefault("done", [])
    _save_projects(projects)
    return jsonify({"plan": plan, "done": p["done"]})


@app.post("/api/projects/<pid>/step")
@_guard
def projects_step(pid):
    """Mark a build milestone done or not done. Progress only; the defend gate is unaffected."""
    projects = _load_projects()
    p = next((x for x in projects if x.get("id") == pid), None)
    if not p:
        return jsonify({"error": "Project not found."}), 404
    b = request.json or {}
    try:
        idx = int(b.get("index"))
    except (TypeError, ValueError):
        return jsonify({"error": "Bad step index."}), 400
    done = set(p.get("done") or [])
    done.add(idx) if b.get("done") else done.discard(idx)
    p["done"] = sorted(done)
    _save_projects(projects)
    return jsonify({"done": p["done"]})


@app.post("/api/projects/<pid>/coach")
@_guard
def projects_coach(pid):
    """Teach the person through a step they're stuck on. Explains and guides, never hands over a
    finished copy-paste solution, so the learning (and the later defense) stays theirs."""
    p = next((x for x in _load_projects() if x.get("id") == pid), None)
    if not p:
        return jsonify({"error": "Project not found."}), 404
    b = request.json or {}
    milestones = ((p.get("plan") or {}).get("milestones")) or []
    try:
        milestone = milestones[int(b.get("index"))]
    except (TypeError, ValueError, IndexError):
        milestone = {}
    question = str(b.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Ask a question about this step."}), 400
    return jsonify(_make_llm().coach_project_step(p, milestone, question))


@app.post("/api/projects/<pid>/grade")
@_guard
def projects_grade(pid):
    """Grade the defense. Passing marks the project verified, the CV-inclusion gate."""
    projects = _load_projects()
    p = next((x for x in projects if x.get("id") == pid), None)
    if not p:
        return jsonify({"error": "Project not found."}), 404
    qa = (request.json or {}).get("answers") or []
    result = _make_llm().grade_project_defense(p, qa)
    p["result"] = result
    p["status"] = "verified" if result.get("pass") else "draft"
    _save_projects(projects)
    return jsonify(result)


@app.post("/api/projects/<pid>/publish")
@_guard
def projects_publish(pid):
    """Publish a VERIFIED project to the person's OWN GitHub, on their click. THE SAME GATE as
    the CV: only a defended project goes public. Creates a repo + README and stores the URL."""
    projects = _load_projects()
    p = next((x for x in projects if x.get("id") == pid), None)
    if not p:
        return jsonify({"error": "Project not found."}), 404
    if p.get("status") != "verified":
        return jsonify({"error": "Pass the understanding test before publishing this."}), 400
    token = _cred("GITHUB_TOKEN")
    if not token:
        return jsonify({"error": "Connect GitHub in Settings, Connections first."}), 400
    from ui.github_publish import publish_project
    res = publish_project(token, p)
    if not res.get("ok"):
        return jsonify({"error": res.get("error", "Couldn't publish to GitHub.")}), 400
    p["repo_url"] = res.get("url", "")
    _save_projects(projects)
    return jsonify({"ok": True, "url": p["repo_url"]})


@app.post("/api/projects/<pid>/tocv")
@_guard
def projects_to_cv(pid):
    """Add a VERIFIED project to the saved profile's projects. THE GATE: refuses unless the
    person passed the defense test, so nothing reaches the CV they can't defend."""
    p = next((x for x in _load_projects() if x.get("id") == pid), None)
    if not p:
        return jsonify({"error": "Project not found."}), 404
    if p.get("status") != "verified":
        return jsonify({"error": "Pass the understanding test before adding this to your resume."}), 400
    mem = _memory()
    saved = mem.load("default") or {}
    prof = saved.get("profile") or {}
    projects = prof.setdefault("projects", [])
    if not any(pr.get("org") == p["title"] for pr in projects):
        # Build the CV entry from what they defended: the summary bullet, plus the tech
        # stack (recruiters and ATS look for it). If published, carry the repo link too.
        bullets = []
        if str(p.get("summary") or "").strip():
            bullets.append(p["summary"].strip())
        if str(p.get("tech") or "").strip():
            bullets.append(f"Built with {p['tech'].strip()}.")
        projects.append({"org": p["title"], "location": "Personal Project", "dates": "",
                         "link": p.get("repo_url") or "", "bullets": bullets, "suggested": False})
    mem.save("default", prof, saved.get("essentials") or {}, saved.get("history") or [])
    return jsonify({"ok": True})


# ---- Interview prep (Feature 3): help someone who has LANDED an interview practice BEFOREHAND,
# grounded in their real experience and the specific role. Mock questions + coaching on their own
# answers (STAR structuring, honest gaps). Pre-interview ONLY: never a live in-interview copilot. ----
_PREPS_FILE = _DATA / "preps.json"


def _load_preps() -> list:
    try:
        return json.loads(_PREPS_FILE.read_text(encoding="utf-8")).get("preps", [])
    except Exception:
        return []


def _save_preps(preps: list) -> None:
    _PREPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PREPS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"preps": preps}, indent=2), encoding="utf-8")
    tmp.replace(_PREPS_FILE)


_STORIES_FILE = _DATA / "stories.json"


def _load_stories() -> list:
    try:
        return json.loads(_STORIES_FILE.read_text(encoding="utf-8")).get("stories", [])
    except Exception:
        return []


def _save_stories(stories: list) -> None:
    _STORIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STORIES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"stories": stories}, indent=2), encoding="utf-8")
    tmp.replace(_STORIES_FILE)


def _story_public(s: dict) -> dict:
    return {"id": s.get("id"), "title": s.get("title", ""), "situation": s.get("situation", ""),
            "task": s.get("task", ""), "action": s.get("action", ""), "result": s.get("result", ""),
            "competencies": s.get("competencies") or []}


def _match_stories_to_question(question: str, competency: str, stories: list) -> list:
    """Rank the person's saved STAR stories by fit for a question: a strong boost when the story is
    tagged with the question's competency, plus keyword overlap between the question and the story
    text. Deterministic, so the suggestion is testable. Top 3, real matches only."""
    qwords = set(re.findall(r"[a-z]{4,}", (question or "").lower()))
    comp = (competency or "").strip().lower()
    ranked = []
    for s in (stories or []):
        score = 0.0
        if comp and comp in [str(c).lower() for c in (s.get("competencies") or [])]:
            score += 5                      # a competency match is the strongest signal
        text = " ".join(str(s.get(k, "")) for k in ("title", "situation", "task", "action", "result"))
        score += len(qwords & set(re.findall(r"[a-z]{4,}", text.lower())))
        if score > 0:
            ranked.append({"id": s.get("id"), "title": s.get("title", ""), "score": round(score, 1),
                           "competencies": s.get("competencies") or []})
    ranked.sort(key=lambda x: -x["score"])
    return ranked[:3]


def _prep_public(p: dict) -> dict:
    return {"id": p.get("id"), "role": p.get("role", ""), "company": p.get("company", ""),
            "focus": p.get("focus", ""), "difficulty": p.get("difficulty", "standard"),
            "questions": p.get("questions") or []}


@app.get("/api/preps")
@_guard
def preps_list():
    return jsonify({"preps": [_prep_public(p) for p in _load_preps()]})


@app.post("/api/preps")
@_guard
def preps_create():
    """Start a prep for a landed interview: generate likely questions grounded in the saved profile
    and the specific role/company/JD, to PRACTICE before the interview.

    Memory-first: pass ``record_id`` (a resume SponsorJobs already built) and we seed the role, company,
    and job description straight from that application, no re-typing, and reuse an existing prep for
    it so tapping a round twice does not pile up duplicates. Or pass {role, company, jd} directly for
    a role you have no saved resume for. With ``focus`` (a competency) it builds a FOCUSED drill set."""
    import uuid
    b = request.json or {}
    role = (b.get("role") or "").strip()
    company, jd = (b.get("company") or "").strip(), (b.get("jd") or "").strip()
    cv = (b.get("cv") or "").strip()   # open option: the exact resume the interviewer has
    focus = (b.get("focus") or "").strip()
    rid = None
    if b.get("record_id") not in (None, ""):
        rec = _get_record(b.get("record_id"))
        if not rec:
            return jsonify({"error": "That application could not be found."}), 404
        rid = str(rec.get("id"))
        data = _record_data(rec)
        role = role or (rec.get("role") or "").strip()
        company = company or (rec.get("company") or "").strip()
        jd = jd or (data.get("jd") or data.get("jd_text") or rec.get("jd_label") or "").strip()
        # Reuse the prep already built for this application (unless drilling one competency).
        if not focus:
            existing = next((p for p in _load_preps() if str(p.get("record_id") or "") == rid), None)
            if existing:
                return jsonify({"prep": _prep_public(existing)})
    if not role:
        return jsonify({"error": "Add the role you're interviewing for."}), 400
    difficulty = "hard" if str(b.get("difficulty") or "").lower() == "hard" else "standard"
    profile = (_memory().load("default") or {}).get("profile") or {}
    # When they attached the CV they used (open option), fold it into the JD context so the
    # questions are grounded in exactly what the interviewer is holding.
    jd_ctx = (jd + "\n\nResume the interviewer has:\n" + cv).strip() if cv else jd
    qs = _make_llm().generate_interview_questions(profile, role, company, jd_ctx, focus=focus,
                                                  difficulty=difficulty)
    questions = qs.get("questions") or []
    p = {"id": uuid.uuid4().hex[:12], "role": role, "company": company, "jd": jd,
         "focus": focus, "difficulty": difficulty, "questions": questions, "record_id": rid}
    preps = _load_preps()
    preps.append(p)
    _save_preps(preps)
    return jsonify({"prep": _prep_public(p)})


@app.post("/api/preps/attach-cv")
@_guard
def preps_attach_cv():
    """Extract the text of an uploaded resume (PDF/DOCX/image/...) locally, so a prep for a role
    you applied to ELSEWHERE can be grounded in the exact CV you used. The file is read and then
    deleted, never stored or uploaded (local-first). Body: multipart form field 'file'."""
    from intake.cv_import import SUPPORTED_EXTS, extract_text
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file."}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in SUPPORTED_EXTS:
        return jsonify({"error": "Use a PDF, Word doc, text file, or image."}), 400
    dest = WORKDIR / f"prepcv{ext}"
    f.save(dest)
    try:
        text = extract_text(dest)
    finally:
        try:
            dest.unlink()
        except OSError:
            pass
    return jsonify({"name": f.filename, "text": (text or "").strip()[:20000]})


@app.get("/api/preps/<pid>/cheatsheet.docx")
@_guard
def preps_cheatsheet_docx(pid):
    """Download a one-page game-day cheat sheet (.docx) for a prep: the questions to have ready plus
    STAR and sponsorship reminders, to review right before the interview. Built with python-docx (no
    LaTeX), served no-store."""
    from tailoring.docx_export import build_cheatsheet_docx
    p = next((x for x in _load_preps() if x.get("id") == pid), None)
    if not p:
        return jsonify({"error": "Prep not found."}), 404
    out = build_cheatsheet_docx(p, WORKDIR / f"cheatsheet-{pid}.docx")
    resp = send_file(out, download_name="interview-cheat-sheet.docx", max_age=0,
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.delete("/api/preps/<pid>")
@_guard
def preps_delete(pid):
    _save_preps([p for p in _load_preps() if p.get("id") != pid])
    return jsonify({"ok": True})


@app.get("/api/stories")
@_guard
def stories_list():
    """The person's STAR story bank: reusable stories built once from their real experience, then
    mapped to whatever question comes up. Nothing here is generated; it is their own material."""
    return jsonify({"stories": [_story_public(s) for s in _load_stories()]})


@app.post("/api/stories")
@_guard
def stories_create():
    """Save one STAR story (Situation, Task, Action, Result) tagged with the competencies it shows.
    Grounded in the person's own experience; the app never invents a story."""
    import uuid
    b = request.json or {}
    title = str(b.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Give the story a short title."}), 400
    comps = [str(x).strip() for x in (b.get("competencies") or []) if str(x).strip()][:6]
    s = {"id": uuid.uuid4().hex[:12], "title": title,
         "situation": str(b.get("situation") or "").strip(),
         "task": str(b.get("task") or "").strip(),
         "action": str(b.get("action") or "").strip(),
         "result": str(b.get("result") or "").strip(),
         "competencies": comps, "created": int(time.time())}
    stories = _load_stories()
    stories.append(s)
    _save_stories(stories)
    return jsonify({"story": _story_public(s)})


@app.delete("/api/stories/<sid>")
@_guard
def stories_delete(sid):
    _save_stories([s for s in _load_stories() if s.get("id") != sid])
    return jsonify({"ok": True})


@app.post("/api/stories/structure")
@_guard
def stories_structure():
    """Turn a rough paragraph about a real experience into a STAR structure to seed a story bank
    entry, so building the bank is low-friction. Grounded in what they wrote; invents nothing. Uses
    the real model. Body: {text}."""
    text = str((request.json or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "Write a few sentences about the experience first."}), 400
    profile = (_memory().load("default") or {}).get("profile") or {}
    star = (_make_llm().coach_interview_answer(profile, "Tell me about this experience.", text)
            or {}).get("star") or {}
    return jsonify({"ok": True, "star": {k: str(star.get(k, "")).strip()
                                         for k in ("situation", "task", "action", "result")}})


@app.post("/api/stories/match")
@_guard
def stories_match():
    """Suggest which saved STAR stories best fit a question, so the bank is deploy-ready instead of
    just storage. Body: {question, competency}. Deterministic ranking, no model call."""
    b = request.json or {}
    matches = _match_stories_to_question(str(b.get("question") or ""),
                                         str(b.get("competency") or ""), _load_stories())
    return jsonify({"matches": matches})


@app.post("/api/stories/suggest")
@_guard
def stories_suggest():
    """Beat the blank page: mine the person's OWN profile for candidate STAR stories they can review,
    edit, and save. Strictly grounded in their real experience/projects; the app invents nothing, and
    they choose what to keep. Skips titles already in the bank so it only offers NEW stories."""
    profile = (_memory().load("default") or {}).get("profile") or {}
    if not (profile.get("experience") or profile.get("projects")):
        return jsonify({"suggestions": [],
                        "note": "Add your experience to your profile first, then I can draft stories from it."})
    try:
        out = _make_llm().suggest_star_stories(profile) or {}
    except Exception:                                    # noqa: BLE001 - never dead-end the button
        traceback.print_exc()
        out = {}
    have = {str(s.get("title", "")).strip().lower() for s in _load_stories()}
    sug = []
    for s in (out.get("stories") or []):
        title = str(s.get("title") or "").strip()
        if not title or title.lower() in have:
            continue
        sug.append({"title": title[:80],
                    "situation": str(s.get("situation") or "").strip(),
                    "task": str(s.get("task") or "").strip(),
                    "action": str(s.get("action") or "").strip(),
                    "result": str(s.get("result") or "").strip(),
                    "competencies": [str(c).strip() for c in (s.get("competencies") or []) if str(c).strip()][:3]})
        if len(sug) >= 6:
            break
    return jsonify({"suggestions": sug})


@app.post("/api/preps/<pid>/coach")
@_guard
def preps_coach(pid):
    """Coach the person's OWN practice answer: structure their real story, flag gaps, offer a
    tightened version built from their material. Stateless, and strictly pre-interview practice."""
    p = next((x for x in _load_preps() if x.get("id") == pid), None)
    if not p:
        return jsonify({"error": "Prep not found."}), 404
    b = request.json or {}
    answer = str(b.get("answer") or "").strip()
    if not answer:
        return jsonify({"error": "Write out your answer to get coaching."}), 400
    qs = p.get("questions") or []
    competency = ""
    try:
        q_obj = qs[int(b.get("index"))]
        question, competency = q_obj.get("q", ""), q_obj.get("competency", "")
    except (TypeError, ValueError, IndexError):
        question = str(b.get("question") or "").strip()
    profile = (_memory().load("default") or {}).get("profile") or {}
    result = _make_llm().coach_interview_answer(profile, question, answer)
    # Connect the STORY BANK to practice: surface the person's saved stories that fit THIS question,
    # so they can deploy one they have already built instead of starting cold. Deterministic, no model.
    stories = _match_stories_to_question(question, competency, _load_stories())
    if stories:
        result["stories"] = stories
    # The visa/sponsorship question is the one international students most need help with, so
    # attach a concrete, deterministic tip regardless of what the model returns.
    if re.search(r"sponsor|\bvisa\b|work authoriz", question, re.I):
        result["tip"] = ("State your status plainly (for example F-1 OPT or STEM-OPT) and "
                         "whether you will need sponsorship later, then pivot straight to the "
                         "value you bring. Honesty, brevity, and a confident pivot beat "
                         "over-explaining or apologizing.")
    return jsonify(result)


@app.post("/api/preps/<pid>/readiness")
@_guard
def preps_readiness(pid):
    """End of a mock session: an honest overall readiness read across all their answers."""
    p = next((x for x in _load_preps() if x.get("id") == pid), None)
    if not p:
        return jsonify({"error": "Prep not found."}), 404
    answers = [{"q": str(a.get("q") or ""), "a": str(a.get("a") or "")}
               for a in ((request.json or {}).get("answers") or []) if str(a.get("a") or "").strip()]
    if not answers:
        return jsonify({"error": "Answer at least one question first."}), 400
    profile = (_memory().load("default") or {}).get("profile") or {}
    return jsonify(_make_llm().assess_interview_readiness(profile, p.get("role", ""), p.get("company", ""), answers))


# ---- Spaced practice: after a mock, resurface the questions you want to nail a few days later.
# Retrieval practice spaced over days is what makes answers stick. Local, in-app nudges only. ----
_REVIEWS_FILE = _DATA / "reviews.json"
# Expanding intervals (days) as you keep nailing a question; a shaky rep resets to the first.
_REVIEW_INTERVALS = (2, 5, 12, 30, 60)


def _load_reviews() -> list:
    try:
        return json.loads(_REVIEWS_FILE.read_text(encoding="utf-8")).get("reviews", [])
    except Exception:
        return []


def _save_reviews(reviews: list) -> None:
    _REVIEWS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _REVIEWS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"reviews": reviews}, indent=2), encoding="utf-8")
    tmp.replace(_REVIEWS_FILE)


def _interval_days(reps: int) -> int:
    """Days until the next review after `reps` successful recalls (0-based, clamped)."""
    return _REVIEW_INTERVALS[min(max(reps, 0), len(_REVIEW_INTERVALS) - 1)]


def _due_in(days: int) -> str:
    from datetime import date, timedelta
    return (date.today() + timedelta(days=days)).isoformat()


def _review_public(r: dict) -> dict:
    from datetime import date
    return {"id": r.get("id"), "prep_id": r.get("prep_id"), "role": r.get("role", ""),
            "q": r.get("q", ""), "due": r.get("due", ""), "reps": r.get("reps", 0),
            "is_due": bool(r.get("due", "") <= date.today().isoformat())}


@app.post("/api/preps/<pid>/schedule")
@_guard
def preps_schedule(pid):
    """Schedule questions from a finished mock for spaced review, first one due in a couple of days.
    Skips questions already scheduled for this prep, so re-running a mock doesn't pile up duplicates."""
    p = next((x for x in _load_preps() if x.get("id") == pid), None)
    if not p:
        return jsonify({"error": "Prep not found."}), 404
    wanted = [str(q).strip() for q in ((request.json or {}).get("questions") or []) if str(q).strip()]
    if not wanted:
        return jsonify({"error": "No questions to schedule."}), 400
    reviews = _load_reviews()
    have = {r.get("q") for r in reviews if r.get("prep_id") == pid}
    added = 0
    for q in wanted:
        if q in have:
            continue
        reviews.append({"id": uuid.uuid4().hex[:12], "prep_id": pid, "role": p.get("role", ""),
                        "q": q, "due": _due_in(_interval_days(0)), "reps": 0})
        added += 1
    _save_reviews(reviews)
    return jsonify({"ok": True, "added": added})


@app.get("/api/reviews")
@_guard
def reviews_list():
    """All scheduled reviews, each flagged is_due, soonest first, so the UI can nudge on due ones."""
    reviews = sorted(_load_reviews(), key=lambda r: r.get("due", ""))
    return jsonify({"reviews": [_review_public(r) for r in reviews]})


@app.post("/api/reviews/<rid>/done")
@_guard
def reviews_done(rid):
    """Log a rehearsal: nailed it grows the interval, still shaky resets it to a couple of days."""
    reviews = _load_reviews()
    r = next((x for x in reviews if x.get("id") == rid), None)
    if not r:
        return jsonify({"error": "Review not found."}), 404
    good = bool((request.json or {}).get("good"))
    r["reps"] = (r.get("reps", 0) + 1) if good else 0
    r["due"] = _due_in(_interval_days(r["reps"]))
    _save_reviews(reviews)
    return jsonify({"ok": True, "due": r["due"], "reps": r["reps"]})


@app.delete("/api/reviews/<rid>")
@_guard
def reviews_delete(rid):
    _save_reviews([r for r in _load_reviews() if r.get("id") != rid])
    return jsonify({"ok": True})


# ---- Round 1: a HireVue-style ONE-WAY recorded video screen on top of a prep (decision of
# 2026-10-02). No interviewer: N questions (3 to 5, default 5), 30s to prepare, 120s to answer,
# one retake per question, auto-submit at time-out (client), unlimited unscored practice prompts
# first. Each answer is transcribed LOCALLY (never cloud), then the set is scored on content, STAR
# structure, specificity, JD-term coverage, fillers and pace; pass at 70. Never facial analysis.
# PRACTICE feedback only, never a hiring decision; not affiliated with HireVue, Inc. Recordings
# stay on disk until the person deletes them. Round 2 (the live interview) is gated on `passed`. ----
_SCREENS_FILE = _DATA / "screens.json"
_RECORDINGS_DIR = _DATA / "recordings"       # under data/ (git-ignored); raw video, local only
_RECORDING_MAX = 32 * 1024 * 1024            # per-recording cap (matches the app-wide upload cap)

ROUND1_DISCLAIMER = ("Practice for a HireVue-style one-way video interview. Not affiliated with "
                     "HireVue, Inc. This score is ours, not theirs.")
# The HireVue-style clocks. `extra_time` (an accessibility accommodation, the way HireVue grants
# extended time) doubles both; nothing else about the screen changes.
_SCREEN_PREP_S, _SCREEN_ANSWER_S, _SCREEN_RETAKES = 30, 120, 1
_SCREEN_MIN_Q, _SCREEN_MAX_Q, _SCREEN_DEFAULT_Q = 3, 5, 5
_SCREEN_KINDS = ("motivation", "behavioural", "situational", "technical")
_KIND_ALIASES = {"behavioral": "behavioural", "motivational": "motivation", "star": "behavioural",
                 "role": "motivation", "opener": "motivation", "practical": "technical",
                 "hypothetical": "situational"}


def _norm_kind(kind) -> str:
    k = str(kind or "").strip().lower()
    return _KIND_ALIASES.get(k, k)


def _screen_mix(n: int) -> list[str]:
    """The question mix, in order. 5: 1 motivation, 3 behavioural, 1 situational (or JD-specific
    light technical). 4 and 3 drop behavioural first. Pure."""
    n = max(_SCREEN_MIN_Q, min(_SCREEN_MAX_Q, int(n)))
    return ["motivation"] + ["behavioural"] * (n - 2) + ["situational"]


def _fit_screen_questions(generated: list, kinds: list[str], prep: dict) -> list[dict]:
    """Exactly one question per slot, in slot order, whatever the model returned. Each slot takes
    the first unused generated question of its kind (the last slot also accepts 'technical'), else
    the prep's first unused question of that type, else a generic default. Pure and deterministic,
    so the screen is never short a question and never has an extra."""
    role = (prep.get("role") or "this role").strip()
    company = (prep.get("company") or "").strip() or "this company"
    defaults = {
        "motivation": (f"Why do you want to work at {company}, and why this {role} role?", "Motivation"),
        "behavioural": ("Tell me about a time you delivered a result you are proud of. What was "
                        "the situation, what did you do, and what happened?", "Ownership"),
        "situational": (f"What would you do if a key {role} deliverable was slipping and the "
                        "deadline could not move?", "Judgment"),
        "technical": (f"Walk me through how you would approach the core technical work this "
                      f"{role} role describes.", "Technical depth"),
    }
    pool = [dict(q, kind=_norm_kind(q.get("kind") or q.get("type"))) for q in (generated or [])
            if isinstance(q, dict) and str(q.get("q") or "").strip()]
    fallback = [dict(q, kind=_norm_kind(q.get("type") or q.get("kind")))
                for q in (prep.get("questions") or [])
                if isinstance(q, dict) and str(q.get("q") or "").strip()]
    used_pool: set = set()
    used_fb: set = set()
    out = []
    last = len(kinds) - 1
    for i, kind in enumerate(kinds):
        accept = {kind} | ({"technical"} if (i == last and kind == "situational") else set())

        def _take(items, used):
            for j, q in enumerate(items):
                if j not in used and q["kind"] in accept:
                    used.add(j)
                    return q
            return None
        q = _take(pool, used_pool) or _take(fallback, used_fb)
        if q is None:
            text, comp = defaults[kind]
            q = {"q": text, "kind": kind, "competency": comp, "why": ""}
        out.append({"q": str(q.get("q", "")).strip(), "kind": q["kind"] if q["kind"] in _SCREEN_KINDS else kind,
                    "competency": str(q.get("competency") or defaults[kind][1]).strip(),
                    "why": str(q.get("why") or "").strip()})
    return out


# Unscored practice prompts: the person warms up on as many as they like before the real set.
# Generic by design (never a leak of the scored questions), never stored, never scored.
_PRACTICE_POOL = (
    "Tell me a bit about yourself and what you are looking for in your next role.",
    "What is one thing you are proud of from the last year, and why?",
    "Describe your ideal working day.",
    "How would a close colleague describe the way you work?",
    "What drew you to {role} work in the first place?",
    "Walk me through how you prepare for something important.",
    "What is a skill you have improved recently, and how did you do it?",
    "If you could change one thing about how teams you have worked on operate, what would it be?",
    "What does a good manager do that makes you do your best work?",
    "Tell me about something you learned the hard way.",
)


def _practice_question(role: str = "", seen: int | None = None) -> dict:
    """One unscored practice prompt. Random each time (or the `seen`-th one when given, so a client
    can page through them); never persisted anywhere."""
    import random
    pool = [p.format(role=(role or "this kind of").strip()) for p in _PRACTICE_POOL]
    text = pool[int(seen) % len(pool)] if seen is not None else random.choice(pool)
    return {"text": text, "kind": "practice"}


def _load_screens() -> list:
    try:
        return json.loads(_SCREENS_FILE.read_text(encoding="utf-8")).get("screens", [])
    except Exception:
        return []


def _save_screens(screens: list) -> None:
    _SCREENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SCREENS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"screens": screens}, indent=2), encoding="utf-8")
    tmp.replace(_SCREENS_FILE)


def _stt_available() -> bool:
    try:
        from interview.transcribe import available
        return available()
    except Exception:
        return False


def _screen_config(s: dict) -> dict:
    return {"prep_s": int(s.get("prep_s") or _SCREEN_PREP_S),
            "answer_s": int(s.get("answer_s") or _SCREEN_ANSWER_S),
            "retakes": int(s.get("retakes") if s.get("retakes") is not None else _SCREEN_RETAKES),
            "questions": len(s.get("answers") or []),
            "extra_time": bool(s.get("extra_time"))}


def _screen_public(s: dict, include_video: bool = False) -> dict:
    """Config + questions + per-answer transcripts / feedback / delivery + the report. Raw video
    paths are omitted by default: the recordings stay on disk, the report doesn't hand back the
    file. A stored result is returned with `answers` folded in (the contract's report shape)."""
    cfg = _screen_config(s)
    max_uploads = cfg["retakes"] + 1
    questions, ans = [], []
    for i, a in enumerate(s.get("answers") or []):
        takes = int(a.get("takes") or 0)
        questions.append({"i": i, "text": a.get("q", ""), "kind": a.get("kind", ""),
                          "competency": a.get("competency", "")})
        item = {"i": i, "q": a.get("q", ""), "kind": a.get("kind", ""),
                "competency": a.get("competency", ""),
                "transcript": a.get("transcript", ""),
                "per_answer_feedback": a.get("per_answer_feedback"),
                "delivery_metrics": a.get("delivery_metrics"),
                "recorded": bool(a.get("video_path")), "takes": takes,
                "takes_left": max(0, max_uploads - takes)}
        if include_video:
            item["video_path"] = a.get("video_path", "")
        ans.append(item)
    result = s.get("result")
    if isinstance(result, dict):
        result = dict(result)
        result.setdefault("disclaimer", ROUND1_DISCLAIMER)
        result["answers"] = [{"i": a["i"], "q": a["q"], "kind": a["kind"], "competency": a["competency"],
                              "transcript": a["transcript"],
                              "score": (a["per_answer_feedback"] or {}).get("score"),
                              "feedback": (a["per_answer_feedback"] or {}).get("feedback", ""),
                              "delivery_note": (a["per_answer_feedback"] or {}).get("delivery_note", ""),
                              "delivery_metrics": a["delivery_metrics"]}
                             for a in ans if a["per_answer_feedback"]]
    return {"id": s.get("id"), "prep_id": s.get("prep_id"), "role": s.get("role", ""),
            "company": s.get("company", ""), "config": cfg, "questions": questions,
            "answers": ans, "result": result, "passed": bool(s.get("passed")),
            "created": int(s.get("created") or 0), "stt_available": _stt_available(),
            "disclaimer": ROUND1_DISCLAIMER}


@app.get("/api/screens")
@_guard
def screens_list():
    return jsonify({"screens": [_screen_public(s) for s in _load_screens()]})


@app.post("/api/screens")
@_guard
def screens_create():
    """Start a Round-1 recorded screen from a prep: {prep_id, extra_time?, questions? (3..5,
    default 5)}. The model is asked for EXACTLY N questions with kinds (1 motivation, N-2
    behavioural, 1 situational or light technical), grounded in the JD and the saved profile; the
    mix is enforced server-side. Returns the screen (flat, plus under `screen`) and one unscored
    practice prompt; GET /api/screens/<id>/practice hands out more, never stored."""
    b = request.json or {}
    prep_id = str(b.get("prep_id") or "").strip()
    prep = next((p for p in _load_preps() if p.get("id") == prep_id), None)
    if not prep:
        return jsonify({"error": "Prep not found. Start a prep first."}), 404
    try:
        n = int(b.get("questions") or _SCREEN_DEFAULT_Q)
    except (TypeError, ValueError):
        n = _SCREEN_DEFAULT_Q
    if not (_SCREEN_MIN_Q <= n <= _SCREEN_MAX_Q):
        return jsonify({"error": f"Pick between {_SCREEN_MIN_Q} and {_SCREEN_MAX_Q} questions."}), 400
    extra = bool(b.get("extra_time"))
    kinds = _screen_mix(n)
    profile = (_memory().load("default") or {}).get("profile") or {}
    llm = _make_llm()           # no model -> fails loudly here (CLAUDE.md §13), never a silent fallback
    try:
        gen = llm.generate_screen_questions(profile, prep.get("role", ""), prep.get("company", ""),
                                            prep.get("jd", ""), kinds=kinds)
        generated = gen.get("questions") or []
    except Exception:
        generated = []          # a bad model reply: the fit falls back to the prep's questions / defaults
    qs = _fit_screen_questions(generated, kinds, prep)
    answers = [{"q": q["q"], "kind": q["kind"], "competency": q["competency"], "why": q["why"],
                "video_path": "", "transcript": "", "per_answer_feedback": None,
                "delivery_metrics": None, "takes": 0} for q in qs]
    mult = 2 if extra else 1
    s = {"id": uuid.uuid4().hex[:12], "prep_id": prep_id, "role": prep.get("role", ""),
         "company": prep.get("company", ""), "answers": answers, "result": None, "passed": False,
         "created": int(time.time()), "extra_time": extra,
         "prep_s": _SCREEN_PREP_S * mult, "answer_s": _SCREEN_ANSWER_S * mult,
         "retakes": _SCREEN_RETAKES}
    screens = _load_screens()
    screens.append(s)
    _save_screens(screens)
    pub = _screen_public(s)
    practice = _practice_question(s["role"])
    return jsonify({**pub, "practice": practice, "screen": {**pub, "practice": practice}})


@app.get("/api/screens/<sid>")
@_guard
def screens_get(sid):
    s = next((x for x in _load_screens() if x.get("id") == sid), None)
    if not s:
        return jsonify({"error": "Screen not found."}), 404
    pub = _screen_public(s)
    return jsonify({**pub, "screen": pub})


@app.get("/api/screens/<sid>/practice")
@_guard
def screens_practice(sid):
    """Another unscored practice prompt (unlimited). Nothing is stored. `?seen=N` pages through
    the pool deterministically; without it the prompt is random."""
    s = next((x for x in _load_screens() if x.get("id") == sid), None)
    if not s:
        return jsonify({"error": "Screen not found."}), 404
    seen = request.args.get("seen")
    try:
        seen_i = int(seen) if seen not in (None, "") else None
    except ValueError:
        seen_i = None
    return jsonify(_practice_question(s.get("role", ""), seen_i))


@app.post("/api/screens/<sid>/answers/<int:idx>")
@_guard
def screens_upload_answer(sid, idx):
    """Upload the recorded audio+video blob for one question. Saved under the local recordings dir;
    the file never leaves the machine. Rejects non-media and oversized input. Enforces the retake
    limit server-side (retakes=1 -> two uploads; the third is a 409), even if the UI is bypassed."""
    screens = _load_screens()
    s = next((x for x in screens if x.get("id") == sid), None)
    if not s:
        return jsonify({"error": "Screen not found."}), 404
    if not (0 <= idx < len(s.get("answers") or [])):
        return jsonify({"error": "No such question."}), 404
    if request.content_length and request.content_length > _RECORDING_MAX:
        return jsonify({"error": "That recording is too large."}), 413
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "No recording uploaded."}), 400
    ctype = (f.mimetype or "").split(";")[0].strip().lower()
    if not (ctype.startswith("video/") or ctype.startswith("audio/")):
        return jsonify({"error": "That isn't an audio or video recording."}), 400
    ans = s["answers"][idx]
    retakes = _screen_config(s)["retakes"]
    max_uploads = retakes + 1
    if int(ans.get("takes") or 0) >= max_uploads:
        return jsonify({"error": f"No retakes left on this question ({retakes} allowed).",
                        "no_takes_left": True, "retakes": retakes}), 409
    ext = ".mp4" if "mp4" in ctype else ".ogg" if "ogg" in ctype else \
          ".mp3" if ctype == "audio/mpeg" else ".webm"
    rec_dir = _RECORDINGS_DIR / sid            # sid is a validated existing screen id (uuid hex)
    rec_dir.mkdir(parents=True, exist_ok=True)
    dest = rec_dir / f"answer_{idx}{ext}"
    f.save(str(dest))
    ans["video_path"] = str(dest)
    ans["takes"] = int(ans.get("takes") or 0) + 1
    # A retake replaces the earlier take: its transcript and feedback no longer apply.
    ans["transcript"], ans["per_answer_feedback"], ans["delivery_metrics"] = "", None, None
    _save_screens(screens)
    return jsonify({"ok": True, "recorded": True, "takes": ans["takes"],
                    "takes_left": max(0, max_uploads - ans["takes"])})


@app.post("/api/screens/<sid>/answers/<int:idx>/transcribe")
@_guard
def screens_transcribe(sid, idx):
    """Transcribe the recorded answer LOCALLY (never cloud). If the local engine isn't installed,
    returns available:false so the UI shows the type/paste fallback instead."""
    screens = _load_screens()
    s = next((x for x in screens if x.get("id") == sid), None)
    if not s or not (0 <= idx < len(s.get("answers") or [])):
        return jsonify({"error": "Screen or question not found."}), 404
    from interview.transcribe import available, delivery_metrics, transcribe
    if not available():
        return jsonify({"available": False,
                        "message": "Local transcription isn't installed. Type or paste your answer instead."})
    ans = s["answers"][idx]
    path = ans.get("video_path")
    if not path or not Path(path).exists():
        return jsonify({"error": "No recording to transcribe."}), 400
    text = transcribe(path)
    ans["transcript"] = text
    ans["delivery_metrics"] = delivery_metrics(text)
    _save_screens(screens)
    return jsonify({"available": True, "transcript": text, "delivery_metrics": ans["delivery_metrics"]})


@app.post("/api/screens/<sid>/answers/<int:idx>/transcript")
@_guard
def screens_set_transcript(sid, idx):
    """Fallback path: set the transcript by typing / pasting (used when local STT isn't installed)."""
    screens = _load_screens()
    s = next((x for x in screens if x.get("id") == sid), None)
    if not s or not (0 <= idx < len(s.get("answers") or [])):
        return jsonify({"error": "Screen or question not found."}), 404
    from interview.transcribe import delivery_metrics
    text = str((request.json or {}).get("transcript") or "").strip()
    ans = s["answers"][idx]
    ans["transcript"] = text
    ans["delivery_metrics"] = delivery_metrics(text)
    _save_screens(screens)
    return jsonify({"ok": True, "delivery_metrics": ans["delivery_metrics"]})


@app.post("/api/screens/<sid>/answers/<int:idx>/coach")
@_guard
def screens_coach_answer(sid, idx):
    """On demand, rewrite ONE recorded answer into a stronger STAR version, grounded ONLY in the
    person's real profile. This is the coaching a one-way screen never gives back: what a strong
    answer to THIS question would have sounded like, from their own material. Uses the real model."""
    s = next((x for x in _load_screens() if x.get("id") == sid), None)
    if not s or not (0 <= idx < len(s.get("answers") or [])):
        return jsonify({"error": "Screen or question not found."}), 404
    ans = s["answers"][idx]
    transcript = str(ans.get("transcript") or "").strip()
    if not transcript:
        return jsonify({"error": "Record or type an answer first, then it can be strengthened."}), 400
    profile = (_memory().load("default") or {}).get("profile") or {}
    coaching = _make_llm().coach_interview_answer(profile, ans.get("q", ""), transcript)
    return jsonify({"ok": True, "coaching": coaching})


def _competency_breakdown(answers: list) -> list:
    """Average the per-answer scores by the question's competency. A one-way screen frames results
    by competency but tells the candidate nothing; we show them exactly where they are strong and
    weak, so they know what to practice. Weakest first (the top of the list is the next thing to
    work on). Competencies with no scored answer are skipped."""
    agg: dict = {}
    for a in (answers or []):
        fb = a.get("per_answer_feedback") or {}
        comp = str(a.get("competency") or "").strip()
        if not comp or "score" not in fb:
            continue
        agg.setdefault(comp, []).append(int(fb.get("score", 0)))
    out = [{"name": k, "score": round(sum(v) / len(v)), "count": len(v)} for k, v in agg.items()]
    out.sort(key=lambda x: (x["score"], x["name"]))
    return out


def _interview_readiness_overview(screens: list, interviews: list | None = None) -> dict:
    """Synthesize ALL of a person's scored attempts, Round-1 screens AND Round-2 live reports, into
    one honest 'am I ready?' read: how many attempts, best and latest score, whether the trend is
    up, which competencies are consistently weakest across attempts, and a plain verdict.
    Deterministic, so it is testable and the number never disagrees with itself. Practice only,
    never a hiring prediction."""
    scored = [dict(s, _round=1) for s in (screens or []) if isinstance(s.get("result"), dict)
              and s["result"].get("score") is not None]
    live = [{"created": iv.get("ended") or iv.get("started") or 0, "role": iv.get("role", ""),
             "company": iv.get("company", ""), "result": iv["report"], "_round": 2}
            for iv in (interviews or []) if isinstance(iv.get("report"), dict)
            and iv["report"].get("score") is not None]
    scored += live
    if not scored:
        return {"attempts": 0, "screens": 0, "live": 0, "verdict": None,
                "message": "Do a recorded screen to see where you stand."}
    ordered = sorted(scored, key=lambda s: s.get("created") or 0)
    scores = [int(s["result"].get("score", 0)) for s in ordered]
    best, latest, first = max(scores), scores[-1], scores[0]
    # Weakest competencies averaged ACROSS every attempt, so a one-off bad answer doesn't dominate.
    agg: dict = {}
    for s in scored:
        for cb in (s["result"].get("competencies") or []):
            agg.setdefault(cb.get("name", ""), []).append(int(cb.get("score", 0)))
    comps = [{"name": k, "score": round(sum(v) / len(v)), "count": len(v)}
             for k, v in agg.items() if k]
    comps.sort(key=lambda x: (x["score"], x["name"]))
    if latest >= 70 and best >= 70:
        verdict, headline = "ready", "You are hitting the practice bar. Keep sharp."
    elif best >= 55:
        verdict, headline = "getting there", "Close. Tighten your weakest areas below."
    else:
        verdict, headline = "not yet", "Keep practicing. Start with your weakest area below."
    return {
        "attempts": len(scored), "screens": len(scored) - len(live), "live": len(live),
        "best": best, "latest": latest,
        "improved": latest > first, "trend_from": first,
        "passed_any": any(s["result"].get("passed") for s in scored),
        "verdict": verdict, "headline": headline,
        "competencies": comps, "weakest": comps[:3],
        "role": ordered[-1].get("role", ""), "company": ordered[-1].get("company", ""),
        "is_practice": True,
    }


def _score_answer_set(role: str, company: str, answers: list, profile: dict) -> dict:
    """Shared scoring for a Round-1 recorded screen AND the Round-2 live dialogue: per-answer
    feedback (mutated onto each answered item) plus an aggregate PRACTICE result. Only answers
    that have a transcript are scored; the result is flagged is_practice (never a hiring outcome).
    Content, STAR structure, specificity, JD-term use, fillers and pace; never facial analysis."""
    from interview.transcribe import delivery_metrics
    llm = _make_llm()
    scored = []
    for a in answers:
        if not str(a.get("transcript") or "").strip():
            continue
        d = a.get("delivery_metrics") or delivery_metrics(a.get("transcript", ""))
        fb = llm.score_interview_answer(profile, a.get("q", ""), a.get("transcript", ""), d)
        a["per_answer_feedback"] = fb
        a["delivery_metrics"] = d
        scored.append({"q": a.get("q", ""), "transcript": a.get("transcript", ""),
                       "score": fb.get("score", 0)})
    result = llm.score_interview_screen(role, company, scored)
    result["is_practice"] = True          # honesty: a practice assessment, not a hiring outcome
    result["competencies"] = _competency_breakdown(answers)   # where they're strong vs weak
    return result


@app.post("/api/screens/<sid>/score")
@_guard
def screens_score(sid):
    """Score the whole screen: per-answer feedback + an overall PRACTICE result (score, pass/fail
    at 70, why, ranked improvements, competencies, answers, disclaimer). Never a real hiring
    decision. Persists the report and the Round-2 `passed` gate flag."""
    screens = _load_screens()
    s = next((x for x in screens if x.get("id") == sid), None)
    if not s:
        return jsonify({"error": "Screen not found."}), 404
    answered = [a for a in (s.get("answers") or []) if str(a.get("transcript") or "").strip()]
    if not answered:
        return jsonify({"error": "Record or type at least one answer first."}), 400
    profile = (_memory().load("default") or {}).get("profile") or {}
    result = _score_answer_set(s.get("role", ""), s.get("company", ""), s["answers"], profile)
    result["disclaimer"] = ROUND1_DISCLAIMER
    s["result"] = result
    s["passed"] = bool(result.get("passed"))   # the Round-2 gate marker
    _save_screens(screens)
    try:   # optional: a short note in the Annalisa diary (P1 memory), best-effort
        _palace().remember_summary(
            f"Recorded practice interview screen for {s.get('role', 'a role')}: scored "
            f"{result.get('score')}/100, {'passed' if s['passed'] else 'below'} the practice bar.")
    except Exception:
        pass
    pub = _screen_public(s)
    return jsonify({"screen": pub, "result": pub["result"]})


@app.get("/api/screens/<sid>/report.docx")
@_guard
def screens_report_docx(sid):
    """Download this screen's PRACTICE report as a Word (.docx), so the person can keep their
    feedback and track progress between sessions. Only after it has been scored. Built on disk and
    served no-store; the recordings themselves never leave the machine and are not included."""
    from tailoring.docx_export import build_screen_report_docx
    s = next((x for x in _load_screens() if x.get("id") == sid), None)
    if not s:
        return jsonify({"error": "Screen not found."}), 404
    if not s.get("result"):
        return jsonify({"error": "Score the screen first, then you can download the report."}), 400
    out = build_screen_report_docx(s, WORKDIR / f"interview-report-{sid}.docx")
    resp = send_file(out, download_name="interview-practice-report.docx", max_age=0,
                     mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/interview/readiness")
@_guard
def interview_readiness_overview():
    """One honest 'am I ready?' read across ALL the person's scored attempts (Round-1 screens and
    Round-2 live reports): attempts, best/latest score, whether they're improving, the competencies
    that are consistently weakest, and a plain verdict with what to fix. Practice only."""
    return jsonify(_interview_readiness_overview(_load_screens(), _load_interviews()))


@app.post("/api/screens/<sid>/delete")
@_guard
def screens_delete(sid):
    """Remove the screen's JSON record AND its recordings directory (local user control)."""
    import shutil
    screens = _load_screens()
    if not any(x.get("id") == sid for x in screens):
        return jsonify({"error": "Screen not found."}), 404
    _save_screens([x for x in screens if x.get("id") != sid])
    try:
        rec_dir = _RECORDINGS_DIR / sid
        if rec_dir.exists():
            shutil.rmtree(rec_dir, ignore_errors=True)
    except Exception:
        pass
    return jsonify({"ok": True})


# ---- Round 2: ONE live Tavus CVI interview (15 min) with an interviewer briefed on the JD and the
# person's résumé, then a scored report (decision of 2026-10-02). Two sources of minutes: the
# PERSON'S OWN Tavus API key (free tier: 25 min/month) saved in Settings and called DIRECTLY from
# this engine, or the paid plan through the managed broker (secondary; the company key stays
# server-side there). Gated on passing Round 1 unless the person deliberately skips. Practice ONLY,
# never a hiring decision; never facial analysis. The transcript is scored, the video is never
# stored by us. ----
_INTERVIEWS_FILE = _DATA / "interviews.json"
SIMULATION_DISCLAIMER = "This is a practice simulation, not a real interview or hiring decision."
ROUND2_MAX_MINUTES = 15
# Test seam for the direct Tavus path: a (method, url, headers, body) -> (status, json) transport.
# None = the real HTTPS transport in backend.tavus_client.
_TAVUS_HTTP = None
# The transcript lands a few seconds after a conversation ends; how long to wait between the
# (at most three) fetch attempts. Tests set it to 0.
_TRANSCRIPT_WAIT_S = 2.0


def _load_interviews() -> list:
    try:
        return json.loads(_INTERVIEWS_FILE.read_text(encoding="utf-8")).get("interviews", [])
    except Exception:
        return []


def _save_interviews(interviews: list) -> None:
    _INTERVIEWS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _INTERVIEWS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"interviews": interviews}, indent=2), encoding="utf-8")
    tmp.replace(_INTERVIEWS_FILE)


# Round 2 is ONE 15-minute interview; a plan source is only offered when it can cover all of it
# (mirrors backend.metering.INTERVIEW_SECONDS, so an interview never starts that cannot finish).
ROUND2_INTERVIEW_SECONDS = 900
_PACKS_TTL_S = 300                       # the broker's pack list changes rarely; cache it briefly
_PACKS_CACHE: dict = {"at": 0.0, "packs": None}


def _round2_packs() -> list:
    """The extra-interview packs the broker sells ([{id, interviews, seconds, price_label}]),
    cached a few minutes. [] when the broker is unreachable or billing is off (never cached, so
    it recovers). Buying one still needs an active pass."""
    now = time.time()
    if _PACKS_CACHE["packs"] is not None and now - _PACKS_CACHE["at"] < _PACKS_TTL_S:
        return list(_PACKS_CACHE["packs"])
    st, data = _broker_get("/billing/packs")
    if st != 200 or not isinstance(data, dict) or not isinstance(data.get("packs"), list):
        return []
    packs = []
    for p in data["packs"]:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        try:
            packs.append({"id": str(p["id"]), "interviews": int(p.get("interviews") or 0),
                          "seconds": int(p.get("seconds") or 0),
                          "price_label": str(p.get("price_label") or "")})
        except (TypeError, ValueError):
            continue
    _PACKS_CACHE.update(at=now, packs=packs)
    return list(packs)


_PASS_TIERS = ("pass30", "pass90")


def _round2_status() -> dict:
    """Whether Round 2 can run right now and from which source. PASS FIRST: when the managed
    broker is reachable and the person's pass + purchased extra interviews cover a full 15-minute
    interview ('plan', on the company's Tavus key, which never leaves the broker); otherwise the
    person's OWN Tavus key ('own_key'); otherwise unavailable with the reason: 'no_key' (a free
    account with nothing set up; `offer: "passes"`) or 'no_minutes' (an active pass whose
    interviews are used up; `offer: "packs"`, buyable while the pass lasts). Also reports `tier`,
    `pass_until` and whether extra interviews can be bought (`can_buy`, `packs`). Never returns
    the key itself."""
    key_set = bool(_cred("AVATAR_API_KEY"))
    # The cheap 2 s loopback health check first, so a down broker never stalls this status call
    # behind the longer account/usage timeouts.
    reachable = _broker_reachable()
    st, usage = _broker_get("/me/usage") if reachable else (0, None)
    tier, secs, pass_until = None, 0, None
    if st == 200 and isinstance(usage, dict):
        tier = str(usage.get("tier") or usage.get("plan") or "")
        pass_until = usage.get("pass_until")
        try:
            secs = max(0, int(usage.get("avatar_seconds_left") or 0))
        except (TypeError, ValueError):
            secs = 0
    on_pass = tier in _PASS_TIERS
    packs = _round2_packs() if reachable and st == 200 and on_pass else []
    extra = {"key_set": key_set, "can_buy": bool(packs), "packs": packs, "tier": tier or None,
             "pass_until": pass_until if on_pass else None,
             "offer": "packs" if on_pass else ("passes" if st == 200 else None)}
    if secs >= ROUND2_INTERVIEW_SECONDS:
        return {"available": True, "reason": "ok", "source": "plan", "minutes_left": secs // 60, **extra}
    if key_set:
        return {"available": True, "reason": "ok", "source": "own_key", "minutes_left": None, **extra}
    return {"available": False, "reason": "no_minutes" if on_pass else "no_key", "source": None,
            "minutes_left": secs // 60 if on_pass else None, **extra}


@app.get("/api/interview/round2/status")
@_guard
def interview_round2_status():
    return jsonify(_round2_status())


@app.post("/api/interview/round2/buy")
@_guard
def interview_round2_buy():
    """Buy more live interviews: {pack} -> the broker mints a one-time Stripe Checkout Session for
    that prepaid pack (never expires) and its URL comes back for the app to open. The Stripe key
    stays on the broker; the credits land on the account when Stripe confirms the payment."""
    pack = str((request.json or {}).get("pack") or "").strip()
    if not re.fullmatch(r"[a-z0-9_]{1,32}", pack):
        return jsonify({"error": "Pick a pack first."}), 400
    st, data = _broker_post(f"/billing/packs/{pack}/checkout", {})
    if st == 0:
        return jsonify({"error": "Billing isn't reachable right now. Try again in a moment."}), 503
    if st == 503:
        return jsonify({"error": "Billing isn't switched on yet."}), 503
    if st == 400:
        return jsonify({"error": "That pack isn't on sale."}), 400
    if st == 402:
        return jsonify({"error": "Extra interviews are sold while a pass is active. Get a pass "
                                 "first.", "offer": "passes"}), 402
    if st != 200 or not data or not data.get("url"):
        return jsonify({"error": (data or {}).get("error") or "Couldn't start checkout."}), 502
    _PACKS_CACHE.update(at=0.0, packs=None)
    return jsonify({"url": data["url"]})


@app.get("/api/avatar/settings")
@_guard
def avatar_settings_get():
    """Whether the person's own Tavus key is saved (never the key) plus the Round-2 status."""
    st = _round2_status()
    return jsonify({**st, "configured": st["key_set"]})


@app.post("/api/avatar/settings")
@_guard
def avatar_settings_set():
    """Save ({key: "..."}) or revoke ({key: ""}) the person's OWN Tavus API key, locally and
    git-ignored. It leaves the machine only as the x-api-key header on their own Tavus calls.
    A new key drops the interviewer PAL created under the old one. Returns the Round-2 status."""
    b = request.json or {}
    if "key" in b:
        key = str(b.get("key") or "").strip()
        if key != _cred("AVATAR_API_KEY"):
            _revoke_cred("AVATAR_PAL_ID")     # a PAL belongs to the account that created it
        _save_cred("AVATAR_API_KEY", key) if key else _revoke_cred("AVATAR_API_KEY")
    st = _round2_status()
    return jsonify({"ok": True, **st, "configured": st["key_set"]})


# ---- The managed broker: the FIRST source of Round-2 interviews (the paid plan + prepaid packs) and the account /
# billing routes. The broker holds the company Tavus/LLM keys SERVER-SIDE and meters minutes per
# plan (docs/bundled-api-backend.md); this app never sees a company key. In dev the broker runs
# locally (TAILOR_BROKER_URL, default 127.0.0.1:57001); in production it is hosted. ----
BROKER_URL = os.environ.get("TAILOR_BROKER_URL", "http://127.0.0.1:57001").rstrip("/")
_BROKER_USER = "local"   # fallback identity when there's no account token yet (dev/offline)


def _account_token() -> str | None:
    """The app's managed-broker account token (bearer auth). Stored git-ignored; registered once on
    first need as an ANONYMOUS account (the free tier, so try-before-signup has no wall). Buying a
    pass upgrades that account, and a signup attaches an email so it follows the person to a
    new device. Returns None if the broker can't be reached, so calls fall back to the dev identity."""
    tok = _cred("TAILOR_ACCOUNT_TOKEN")
    if tok:
        return tok
    import requests
    try:
        from llm.broker_client import BROKER_USER_AGENT
        r = requests.post(f"{BROKER_URL}/account/register", timeout=10,
                          headers={"User-Agent": BROKER_USER_AGENT})
        if r.status_code == 200:
            tok = (r.json() or {}).get("token")
            if tok:
                _save_cred("TAILOR_ACCOUNT_TOKEN", tok)
                return tok
    except requests.RequestException:
        pass
    return None


def _broker_headers() -> dict:
    """Auth for a broker call: the account's bearer token when we have one, else the dev header."""
    from llm.broker_client import BROKER_USER_AGENT
    tok = _account_token()
    auth = {"Authorization": f"Bearer {tok}"} if tok else {"X-Tailor-User": _BROKER_USER}
    return {**auth, "User-Agent": BROKER_USER_AGENT}


def _token_is_stale() -> bool:
    """A 401 on some route is only evidence the token is unknown to the broker when the plain
    identity route ALSO rejects it. Only then is it safe to register a fresh account; re-registering
    on any 401 could orphan a paid account over an unrelated auth error."""
    import requests
    tok = _cred("TAILOR_ACCOUNT_TOKEN")
    if not tok:
        return False
    try:
        r = requests.get(f"{BROKER_URL}/me/usage", headers={"Authorization": f"Bearer {tok}"},
                         timeout=10)
    except requests.RequestException:
        return False
    return r.status_code == 401


def _recover_from_401() -> bool:
    """Drop a token the broker no longer knows, so the next call registers a fresh one. True when
    a retry is worth making."""
    if not _token_is_stale():
        return False
    print("[broker] the saved account token is unknown to the broker; registering a new one")
    _revoke_cred("TAILOR_ACCOUNT_TOKEN")
    return _account_token() is not None


def _broker_post(path: str, body: dict):
    """Server-to-server call to the managed broker. Returns (status_code, json_or_None); status 0
    means the broker was unreachable."""
    import requests
    try:
        r = requests.post(f"{BROKER_URL}{path}", json=body,
                          headers=_broker_headers(), timeout=25)
        if r.status_code == 401 and _recover_from_401():
            r = requests.post(f"{BROKER_URL}{path}", json=body,
                              headers=_broker_headers(), timeout=25)
    except requests.RequestException:
        return 0, None
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def _broker_get(path: str):
    """GET against the broker with the account's auth. Returns (status_code, json_or_None)."""
    import requests
    try:
        r = requests.get(f"{BROKER_URL}{path}", headers=_broker_headers(), timeout=15)
        if r.status_code == 401 and _recover_from_401():
            r = requests.get(f"{BROKER_URL}{path}", headers=_broker_headers(), timeout=15)
    except requests.RequestException:
        return 0, None
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


@app.get("/api/account/status")
@_guard
def account_status():
    """This install's account: whether it has an email yet (so the UI knows to show the
    create-account step at checkout) and its tier with what is left (so the profile badge and the
    Upgrade panel show the pass, its end date, interviews and packages). Anonymous ->
    {email: null}."""
    st, data = _broker_get("/account/me")
    st2, usage = _broker_get("/me/usage")
    u = usage if st2 == 200 and isinstance(usage, dict) else {}
    extra = {"plan": u.get("tier") or u.get("plan"), "pass_until": u.get("pass_until"),
             "interviews_left": u.get("interviews_left"), "packages_left": u.get("packages_left")}
    if st != 200 or not data:
        return jsonify({"email": None, "reachable": st != 0, **extra})
    return jsonify({"email": data.get("email"), "reachable": True, **extra})


@app.get("/api/account/google/url")
@_guard
def account_google_url():
    """Where the browser should go to start 'Continue with Google' (the broker's OAuth start)."""
    return jsonify({"url": f"{BROKER_URL}/account/google/start"})


@app.post("/api/account/adopt")
@_guard
def account_adopt():
    """Adopt a bearer token handed back by Google sign-in: verify it works against the broker, then
    store it as this install's account so every later call uses it."""
    token = str((request.json or {}).get("token", "")).strip()
    if not token:
        return jsonify({"error": "no token"}), 400
    import requests
    try:
        r = requests.get(f"{BROKER_URL}/me/usage",
                         headers={"Authorization": f"Bearer {token}"}, timeout=15)
    except requests.RequestException:
        return jsonify({"error": "The account service isn't reachable. Try again in a moment."}), 503
    if r.status_code != 200:
        return jsonify({"error": "That sign-in didn't work. Please try again."}), 400
    _save_cred("TAILOR_ACCOUNT_TOKEN", token)   # this device is now that account
    return jsonify({"ok": True})


@app.post("/api/account/claim")
@_guard
def account_claim():
    """Attach an email + password to this install's anonymous account (try-first signup at checkout)."""
    b = request.json or {}
    st, data = _broker_post("/account/claim",
                            {"email": b.get("email", ""), "password": b.get("password", "")})
    if st == 0:
        return jsonify({"error": "The account service isn't reachable. Try again in a moment."}), 503
    if st == 200:
        return jsonify({"ok": True, "email": (data or {}).get("email")})
    return jsonify({"error": (data or {}).get("error") or "Couldn't create your account."}), \
        (st if st in (400, 409) else 502)


@app.post("/api/account/login")
@_guard
def account_login():
    """Log in on this device: swap the app's stored token for the logged-in account's, so an
    existing pass is restored here."""
    b = request.json or {}
    st, data = _broker_post("/account/login",
                            {"email": b.get("email", ""), "password": b.get("password", "")})
    if st == 0:
        return jsonify({"error": "The account service isn't reachable. Try again in a moment."}), 503
    if st == 200 and data and data.get("token"):
        _save_cred("TAILOR_ACCOUNT_TOKEN", data["token"])   # this device is now that account
        return jsonify({"ok": True})
    return jsonify({"error": (data or {}).get("error") or "Couldn't log in."}), \
        (st if st in (400, 401) else 502)


_PASS_IDS = ("pass30", "pass90")


@app.get("/api/billing/offers")
@_guard
def billing_offers():
    """The passes on sale, the extra-interview packs (only while a pass is active) and where this
    account stands: {passes, packs, current:{tier, pass_until, interviews_left, packages_left},
    reachable}. Nothing auto-renews. When the broker is unreachable the lists are empty."""
    st, data = _broker_get("/billing/offers")
    if st != 200 or not isinstance(data, dict):
        return jsonify({"passes": [], "packs": [], "current": None, "reachable": st != 0})
    return jsonify({"passes": data.get("passes") or [], "packs": data.get("packs") or [],
                    "current": data.get("current"), "reachable": True})


@app.post("/api/billing/checkout")
@_guard
def billing_checkout():
    """Buy a pass: {pass: "pass30" | "pass90"} -> the broker mints a ONE-TIME Stripe Checkout
    Session and its URL comes back for the app to open. No subscription, nothing auto-renews; the
    pass lands on the account when Stripe confirms the payment. The Stripe key stays on the
    broker."""
    pass_id = str((request.json or {}).get("pass", "")).strip()
    if pass_id not in _PASS_IDS:
        return jsonify({"error": "Pick a pass first."}), 400
    st, data = _broker_post(f"/billing/passes/{pass_id}/checkout", {})
    if st == 0:
        return jsonify({"error": "Billing isn't reachable right now. Try again in a moment."}), 503
    if st == 503:
        return jsonify({"error": "Billing isn't switched on yet."}), 503
    if st == 400:
        return jsonify({"error": "That pass isn't on sale."}), 400
    if st != 200 or not data or not data.get("url"):
        return jsonify({"error": (data or {}).get("error") or "Couldn't start checkout."}), 502
    _PACKS_CACHE.update(at=0.0, packs=None)
    return jsonify({"url": data["url"]})


# The interviewer PAL's standing instructions (created ONCE per Tavus account, id stored locally
# as AVATAR_PAL_ID). Everything specific to an interview (company, role, JD, résumé, questions)
# arrives per conversation as `conversational_context`, so one PAL serves every mock.
_PAL_SYSTEM_PROMPT = (
    "You are a professional hiring manager running a 15-minute live mock job interview. The "
    "conversational context you receive names the company, the role, the job description, the "
    "candidate's resume and the questions to cover; stay in that character throughout.\n"
    "Structure: (1) introduce yourself in two sentences and ask the candidate to introduce "
    "themselves; (2) ask 4 to 6 questions, ONE AT A TIME, waiting for the full answer, and probe "
    "each with one or two short follow-ups (what was your part, what was the result, what would "
    "you do differently); (3) around minute 12 ask whether they have questions for you and answer "
    "briefly from the job description; (4) close warmly and say they will hear back.\n"
    "Rules: keep each of your turns under 40 words; never answer for the candidate; never ask what "
    "they want to talk about; use the job description's own vocabulary; do not give scores or "
    "feedback during the interview; if the candidate goes quiet, gently re-ask or move on."
)
_PAL_GREETING = ("Hi, thanks for joining. I'll be interviewing you today. To start, could you tell "
                 "me a little about yourself and what drew you to this role?")


def _round2_plan(prep: dict) -> list[dict]:
    """The 4 to 6 questions the interviewer should cover, with the competency each probes, drawn
    from the prep (JD-grounded). Used in the briefing and to tag competencies in the report."""
    out = []
    for q in (prep.get("questions") or []):
        text = str(q.get("q") or "").strip()
        if text:
            out.append({"q": text, "competency": str(q.get("competency") or "").strip() or "General"})
        if len(out) >= 6:
            break
    return out


def _round2_briefing(prep: dict, profile: dict) -> str:
    """The per-interview briefing (Tavus `conversational_context`): interviewer persona for this
    company/role, the 15-minute structure, the JD and the person's résumé/profile, and the
    questions to cover. Sent to Tavus to steer the interviewer; not stored by us."""
    role = (prep.get("role") or "the role").strip()
    company = (prep.get("company") or "").strip()
    jd = str(prep.get("jd") or "").strip()
    who = f"the {role} role" + (f" at {company}" if company else "")
    lines = [
        f"You are the hiring manager for {who}, interviewing this candidate in a 15-minute practice "
        "interview. Introduce yourself as the hiring manager for this team.",
        "Ask one question at a time, listen to the whole answer, probe with short follow-ups, and "
        "keep to: intro, 4 to 6 questions, the candidate's questions, close.",
    ]
    plan = _round2_plan(prep)
    if plan:
        lines.append("QUESTIONS TO COVER (adapt the wording, ask in a natural order):")
        lines += [f"- {q['q']} (probes: {q['competency']})" for q in plan]
    if jd:
        lines.append("JOB DESCRIPTION:\n" + jd[:2500])
    if profile:
        lines.append("CANDIDATE RESUME / PROFILE (JSON):\n" + json.dumps(profile)[:2500])
    return "\n\n".join(lines)


def _tavus_client(key: str):
    from backend.tavus_client import TavusClient
    return TavusClient(key, http=_TAVUS_HTTP)


def _ensure_pal(client) -> str:
    """The interviewer PAL for the person's own Tavus account, created once and remembered locally.
    Returns '' when it can't be created; the caller then passes the briefing on a bare face."""
    pid = _cred("AVATAR_PAL_ID")
    if pid:
        return pid
    from backend.tavus_client import DEFAULT_FACE_ID
    try:
        pid = client.create_pal(name="Tailor mock interviewer", system_prompt=_PAL_SYSTEM_PROMPT,
                                face_id=_cred("AVATAR_FACE_ID") or DEFAULT_FACE_ID,
                                greeting=_PAL_GREETING)
    except Exception:
        return ""
    if pid:
        _save_cred("AVATAR_PAL_ID", pid)
    return pid


def _interview_public(iv: dict) -> dict:
    return {"id": iv.get("id"), "prep_id": iv.get("prep_id"), "screen_id": iv.get("screen_id"),
            "skipped_screen": bool(iv.get("skipped_screen")),
            "role": iv.get("role", ""), "company": iv.get("company", ""),
            "source": iv.get("source"), "conversation_id": iv.get("conversation_id", ""),
            "started": int(iv.get("started") or 0), "ended": int(iv.get("ended") or 0),
            "max_minutes": int(iv.get("max_minutes") or ROUND2_MAX_MINUTES),
            "report": iv.get("report"), "is_practice": True, "disclaimer": SIMULATION_DISCLAIMER}


def _start_own_key_conversation(briefing: str, role: str):
    """Mint the conversation on the person's OWN Tavus key, directly from this app. Returns
    (join_url, conversation_id), or a Flask (response, status) to hand straight back."""
    from backend.tavus_client import DEFAULT_FACE_ID, TavusError
    client = _tavus_client(_cred("AVATAR_API_KEY"))
    pal = _ensure_pal(client)
    try:
        conv = client.create_conversation(
            face_id=_cred("AVATAR_FACE_ID") or DEFAULT_FACE_ID, pal_id=pal, context=briefing,
            name=f"Mock interview: {role}"[:80], max_call_seconds=ROUND2_MAX_MINUTES * 60,
            greeting="" if pal else _PAL_GREETING, require_auth=True)
    except TavusError as exc:
        if exc.status in (401, 403):
            return jsonify({"error": "round2_unavailable", "reason": "no_key", "source": "own_key",
                            "message": "Tavus rejected that API key. Check it in Settings."}), 402
        if exc.status in (402, 429):
            return jsonify({"error": "round2_unavailable", "reason": "no_minutes",
                            "source": "own_key",
                            "message": "Your Tavus account is out of conversation minutes."}), 402
        return jsonify({"error": "tavus_error", "message": str(exc)[:200]}), 502
    except Exception:
        return jsonify({"error": "service_unavailable",
                        "message": "Couldn't reach Tavus right now."}), 503
    if not conv.get("conversation_url"):
        return jsonify({"error": "tavus_error", "message": "Tavus returned no join URL."}), 502
    return conv["conversation_url"], conv["conversation_id"]


@app.post("/api/interviews/cvi/start")
@_guard
def interviews_cvi_start():
    """Start the ONE live interview for a prep: {prep_id, screen_id?, skip_screen?}.
    403 screen_not_passed unless the given screen passed (or skip_screen with no screen_id);
    402 round2_unavailable {reason} when neither plan minutes (a full interview's worth) nor the
    person's own Tavus key are available. PLAN FIRST: the managed broker mints the session on the
    company key; otherwise, with their own key, Tavus CVI is called DIRECTLY from here (their key,
    their minutes). Returns the join URL for the app's
    video pane."""
    b = request.json or {}
    prep = next((p for p in _load_preps() if p.get("id") == str(b.get("prep_id") or "").strip()), None)
    if not prep:
        return jsonify({"error": "Prep not found. Start a prep first."}), 404
    screen_id = str(b.get("screen_id") or "").strip()
    if screen_id:
        screen = next((s for s in _load_screens() if s.get("id") == screen_id), None)
        if not screen or not bool(screen.get("passed")):
            return jsonify({"error": "screen_not_passed", "gated": True,
                            "message": "Pass the recorded screen first, or start Round 2 on its own."}), 403
    elif not bool(b.get("skip_screen")):
        return jsonify({"error": "screen_not_passed", "gated": True,
                        "message": "Do the recorded screen first, or start Round 2 on its own."}), 403
    status = _round2_status()
    if not status["available"]:
        return jsonify({"error": "round2_unavailable", **status}), 402
    profile = (_memory().load("default") or {}).get("profile") or {}
    briefing = _round2_briefing(prep, profile)
    role = prep.get("role", "")
    source = status["source"]
    if source == "plan":
        st, data = _broker_post("/avatar/session/start", {"context": {"prompt": briefing}})
        if st == 402 and status.get("key_set"):
            source = "own_key"               # the plan ran out between status and start: own key
        elif st == 0:
            return jsonify({"error": "service_unavailable",
                            "message": "The interview service isn't reachable right now."}), 503
        elif st == 402:
            return jsonify({"error": "round2_unavailable", "reason": "no_minutes", "source": "plan",
                            "can_buy": status.get("can_buy", False), "packs": status.get("packs", []),
                            "message": (data or {}).get("error") or "You're out of interview minutes."}), 402
        elif st != 200 or not data or not data.get("session_url"):
            return jsonify({"error": "service_error", "message": "Couldn't start the live interview."}), 502
        else:
            join_url, conversation_id = data["session_url"], str(data.get("provider_session_id") or "")
    if source == "own_key":
        started = _start_own_key_conversation(briefing, role)
        if not isinstance(started[0], str):
            return started                   # an error (response, status) to hand straight back
        join_url, conversation_id = started
    iv = {"id": uuid.uuid4().hex[:12], "prep_id": prep.get("id"), "screen_id": screen_id or None,
          "skipped_screen": not screen_id, "role": role, "company": prep.get("company", ""),
          "source": source, "conversation_id": conversation_id,
          "started": int(time.time()), "max_minutes": ROUND2_MAX_MINUTES,
          "plan": _round2_plan(prep), "report": None, "transcript": []}
    interviews = _load_interviews()
    interviews.append(iv)
    _save_interviews(interviews)
    return jsonify({"interview_id": iv["id"], "join_url": join_url, "conversation_id": conversation_id,
                    "max_minutes": ROUND2_MAX_MINUTES, "source": source,
                    "role": role, "company": iv["company"], "disclaimer": SIMULATION_DISCLAIMER})


@app.post("/api/interviews/cvi/heartbeat")
@_guard
def interviews_cvi_heartbeat():
    """Plan source only: while the live conversation runs, report elapsed seconds; the broker
    meters and says when to stop at the plan limit. With the person's own key there is no meter
    (Tavus enforces its own), so it just answers stop:false."""
    b = request.json or {}
    try:
        seconds = int(b.get("seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0
    iid = str(b.get("interview_id") or "").strip()
    iv = next((x for x in _load_interviews() if x.get("id") == iid), None) if iid else None
    # The interview remembers which source it started on (plan first now), so meter by that; a
    # call without an interview id keeps the old rule (an own key means no meter).
    source = (iv or {}).get("source") or ("own_key" if _cred("AVATAR_API_KEY") else "plan")
    if source == "own_key":
        return jsonify({"remaining": None, "stop": False, "source": "own_key"})
    st, data = _broker_post("/avatar/heartbeat", {"seconds": max(0, seconds)})
    if st != 200 or data is None:
        return jsonify({"remaining": 0, "stop": True, "error": "meter unavailable"}), 200
    return jsonify({"remaining": data.get("remaining", 0), "stop": bool(data.get("stop")), "source": "plan"})


def _closest_competency(question: str, plan: list) -> str:
    """Tag a live question with the planned question it most resembles (word overlap), so the
    report breaks down by competency like Round 1. 'General' when nothing is close. Pure."""
    def words(s):
        return {w for w in re.findall(r"[a-z]{4,}", str(s or "").lower())}
    qw = words(question)
    best, best_score = "General", 0.0
    for p in plan or []:
        pw = words(p.get("q"))
        if not qw or not pw:
            continue
        score = len(qw & pw) / len(qw | pw)
        if score > best_score:
            best, best_score = (p.get("competency") or "General"), score
    return best if best_score >= 0.15 else "General"


def _pair_dialogue(turns: list, plan: list, min_words: int = 8) -> list[dict]:
    """Turn a dialogue into scorable {q, transcript, competency} items: each interviewer turn (or run
    of turns) is a question, the candidate turns that follow are its answer. Answers shorter than
    `min_words` (yes / thank you / goodbye) are not scored. Pure."""
    items: list[dict] = []
    cur: dict | None = None
    for t in turns or []:
        content = str(t.get("content") or "").strip()
        if not content:
            continue
        if t.get("role") == "interviewer":
            if cur is not None and cur["transcript"]:
                items.append(cur)
                cur = None
            if cur is None:
                cur = {"q": content, "transcript": ""}
            else:
                cur["q"] = (cur["q"] + " " + content).strip()
        else:
            if cur is None:
                cur = {"q": "Tell me about yourself.", "transcript": ""}
            cur["transcript"] = (cur["transcript"] + " " + content).strip()
    if cur is not None and cur["transcript"]:
        items.append(cur)
    out = []
    for it in items:
        if len(it["transcript"].split()) < min_words:
            continue
        it["competency"] = _closest_competency(it["q"], plan)
        it["per_answer_feedback"], it["delivery_metrics"] = None, None
        out.append(it)
    return out


def _score_live_dialogue(iv: dict, turns: list, profile: dict, duration_s: int) -> dict:
    """Score the live interview's transcript with the same per-answer + aggregate scorers as Round 1,
    adapted for a dialogue (questions paired with the answers that followed). Content, STAR,
    specificity, JD terms, fillers, pace; never the video."""
    answers = _pair_dialogue(turns, iv.get("plan") or [])
    if answers:
        result = _score_answer_set(iv.get("role", ""), iv.get("company", ""), answers, profile)
    else:
        result = {"score": 0, "passed": False, "threshold": 70, "is_practice": True,
                  "why": "There was too little said to score. Answer each question in a few full "
                         "sentences next time.",
                  "improvements": ["Answer in full sentences with a situation, your action, and the result.",
                                   "Aim for 60 to 120 seconds per answer."],
                  "competencies": []}
    result["answers"] = [{"q": a["q"], "transcript": a["transcript"], "competency": a["competency"],
                          "score": (a.get("per_answer_feedback") or {}).get("score"),
                          "feedback": (a.get("per_answer_feedback") or {}).get("feedback", ""),
                          "delivery_note": (a.get("per_answer_feedback") or {}).get("delivery_note", ""),
                          "delivery_metrics": a.get("delivery_metrics")} for a in answers]
    result["transcript"] = [{"role": t.get("role"), "content": t.get("content")} for t in turns]
    result["duration_s"] = int(duration_s)
    result["disclaimer"] = SIMULATION_DISCLAIMER
    result["round"] = 2
    result["source"] = iv.get("source")
    return result


@app.post("/api/interviews/cvi/end")
@_guard
def interviews_cvi_end():
    """End the live interview {interview_id, transcript?, duration_s?}: tells Tavus to end the
    conversation (own key), fetches its transcript, scores the dialogue, stores the report, and
    returns it. If Tavus has no transcript (yet, or the plan path where this app holds no key), a
    client-supplied `transcript` (text, or [{role, content}]) is scored instead; with neither it's
    a 400 so the UI can ask for one."""
    b = request.json or {}
    iid = str(b.get("interview_id") or "").strip()
    interviews = _load_interviews()
    iv = next((x for x in interviews if x.get("id") == iid), None)
    if not iv:
        return jsonify({"error": "Interview not found."}), 404
    from backend.tavus_client import parse_transcript_text
    turns: list = []
    key = _cred("AVATAR_API_KEY")
    cid = iv.get("conversation_id") or ""
    if iv.get("source") == "own_key" and key and cid:
        client = _tavus_client(key)
        try:
            client.end_conversation(cid)
        except Exception:
            pass                         # already ended / timed out: still fetch the transcript
        for attempt in range(3):
            try:
                turns = client.transcript(cid)
            except Exception:
                turns = []
            if turns:
                break
            if attempt < 2 and _TRANSCRIPT_WAIT_S:
                time.sleep(_TRANSCRIPT_WAIT_S)
    supplied = b.get("transcript")
    if not turns and supplied:
        if isinstance(supplied, list):
            turns = [{"role": "interviewer" if str(t.get("role", "")).lower() in
                      ("interviewer", "assistant", "ai") else "candidate",
                      "content": str(t.get("content") or "").strip()}
                     for t in supplied if isinstance(t, dict) and str(t.get("content") or "").strip()]
        else:
            turns = parse_transcript_text(str(supplied))
    if not turns:
        return jsonify({"error": "no_transcript",
                        "message": "No transcript came back from the interview. Paste what was said "
                                   "to get it scored."}), 400
    now = int(time.time())
    try:
        duration_s = int(b.get("duration_s") or 0)
    except (TypeError, ValueError):
        duration_s = 0
    if not duration_s:
        ends = [float(t.get("seconds_from_start") or 0) + float(t.get("duration") or 0)
                for t in turns if t.get("seconds_from_start") is not None]
        duration_s = int(max(ends)) if ends else max(0, now - int(iv.get("started") or now))
    duration_s = min(duration_s, ROUND2_MAX_MINUTES * 60)
    profile = (_memory().load("default") or {}).get("profile") or {}
    report = _score_live_dialogue(iv, turns, profile, duration_s)
    iv["report"], iv["ended"] = report, now
    iv["transcript"] = report["transcript"]
    _save_interviews(interviews)
    try:
        _palace().remember_summary(
            f"Live practice interview for {iv.get('role', 'a role')}: scored {report.get('score')}/100, "
            f"{'passed' if report.get('passed') else 'below'} the practice bar.")
    except Exception:
        pass
    return jsonify({"report": report, "interview_id": iid, "interview": _interview_public(iv)})


@app.get("/api/interviews/<iid>")
@_guard
def interviews_get(iid):
    """The stored Round-2 record and its report (null until the interview has been ended/scored)."""
    iv = next((x for x in _load_interviews() if x.get("id") == iid), None)
    if not iv:
        return jsonify({"error": "Interview not found."}), 404
    return jsonify({"interview": _interview_public(iv), "report": iv.get("report")})


@app.post("/api/interviews/<iid>/delete")
@_guard
def interviews_delete(iid):
    """Remove the live interview's record and report (local user control)."""
    interviews = _load_interviews()
    if not any(x.get("id") == iid for x in interviews):
        return jsonify({"error": "Interview not found."}), 404
    _save_interviews([x for x in interviews if x.get("id") != iid])
    return jsonify({"ok": True})


@app.post("/api/inbox/verify")
@_guard
def inbox_verify():
    """Visit ONE application-verification link on the person's behalf (their click on
    the button IS the consent; in autonomous mode the scan can do it for opted-in
    accounts). The opener refuses anything that isn't a safe public http(s) URL."""
    from inbox.gmail import complete_verification
    url = str((request.json or {}).get("url") or "").strip()
    if not url:
        return jsonify({"error": "No verification link given."}), 400
    out = complete_verification(url)
    status = 200 if out.get("ok") else 400
    return _nostore(jsonify(out)), status


@app.post("/api/inbox/scan")
@_guard
def inbox_scan():
    """Scan the person's OWN mailbox for application-related mail (CLAUDE.md §5) and
    return verification links to visit plus recruiter replies drafted from their profile
    for review. Read-first, send-on-consent, nothing is sent here.

    Body: optionally {messages:[...]} to scan an explicit batch (used by tests / import);
    otherwise the live Gmail adapter is used, which needs the local OAuth token. Drafting
    uses the real model, so it needs the internet + the user's key like every other
    tailoring call."""
    from inbox.service import scan_inbox
    body = request.json or {}
    messages = body.get("messages")
    if messages is None:
        from inbox.gmail import build_service, fetch_messages
        messages = fetch_messages(build_service())
    profile = _saved_full_profile()
    llm = _make_llm() if body.get("draft", True) else None
    result = scan_inbox(messages, profile=profile, llm=llm,
                        draft_replies=bool(body.get("draft", True)))
    # Autonomous verification is DOUBLY opted in (CLAUDE.md §5): the global autonomous
    # switch AND the inbox_auto_verify preference, both OFF by default. Otherwise every
    # verification is surfaced with a button; the person's click is the consent.
    if _autonomous_on() and _prefs().get("inbox_auto_verify"):
        from inbox.gmail import complete_verification
        for v in result.get("verifications", []):
            if v.get("link"):
                v["auto"] = complete_verification(v["link"])
    return _nostore(jsonify({"ok": True, **result}))


@app.post("/api/inbox/draft_reply")
@_guard
def inbox_draft_reply():
    """Draft (or re-draft) a reply to one recruiter message on demand, grounded in the
    person's saved profile. Body: {from, subject, body}. The person reviews and sends."""
    from inbox.service import draft_reply
    body = request.json or {}
    msg = {"from": str(body.get("from") or ""), "subject": str(body.get("subject") or ""),
           "body": str(body.get("body") or "")}
    reply = draft_reply(msg, _saved_full_profile(), _make_llm())
    return _nostore(jsonify({"ok": True, "reply": reply}))


# Where the Telegram long-poll offset persists between /api/notify/poll calls, so the
# same command isn't handled twice. Small local file, created on demand.
_TG_OFFSET_FILE = _DATA / "telegram_offset"


def _tg_offset() -> int:
    try:
        return int(_TG_OFFSET_FILE.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _set_tg_offset(offset: int) -> None:
    _TG_OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TG_OFFSET_FILE.write_text(str(int(offset)), encoding="utf-8")


@app.post("/api/notify/announce/<int:rid>")
@_guard
def notify_announce(rid):
    """Push a 'package ready' alert to the person's Telegram so they can approve while
    away (CLAUDE.md §5). Sends only to the owner's own chat."""
    from notify.service import NotifyService
    recs = _records()
    try:
        rec = recs.get(rid)
    finally:
        recs.close()
    if not rec:
        return jsonify({"error": "That application was not found."}), 404
    svc = NotifyService(_make_bot(), _RecordActions())
    svc.announce_ready({"id": rid, "role": rec.get("role") or "",
                        "company": rec.get("company") or "",
                        "coverage": rec.get("coverage")})
    return jsonify({"ok": True})


@app.post("/api/notify/poll")
@_guard
def notify_poll():
    """One notification tick on demand (the background loop runs the same every 30 s): read
    the person's Telegram replies and taps, act, send due alerts. Owner-only (fail-closed)."""
    out = _notify_tick()
    if out.get("channel") == "none":
        return jsonify({"ok": False, "handled": 0, "error": "Telegram isn't connected. "
                        "Connect it in Settings, Notifications."}), 400
    if out.get("error") == "no_owner":
        return jsonify({"ok": False, "handled": 0, "error": "No owner chat configured (set "
                        "TELEGRAM_CHAT_ID); ignoring all messages."}), 400
    return jsonify({**out, "ok": True, "offset": _tg_offset()})


# ---- Notifications hub (notify/): one channel (official SponsorJobs bot via the broker, or the
# person's own bot), job-match alerts, the away digest, and button/command dispatch. A
# background loop ticks every 30 s while the app runs; /api/notify/poll runs the same tick. ----
_NOTIFY_STATE_FILE = None    # None -> notify_state.json beside DB_PATH (follows a test's tmp DB)
_OFFICIAL_CACHE: dict = {"at": 0.0, "linked": False, "data": None}
_OFFICIAL_TTL = 60.0
_NOTIFY_TICK_LOCK = threading.Lock()
_NOTIFY_LOOP: dict = {"thread": None}
NOTIFY_INTERVAL = 30.0
NOTIFY_SCAN_EVERY = 10        # look for new job matches every 5 minutes


def _notify_state():
    from notify.alerts import NotifyState
    return NotifyState(_NOTIFY_STATE_FILE or (Path(DB_PATH).parent / "notify_state.json"))


def _official_channel():
    from notify.channel import OfficialBotChannel
    st = _notify_state()
    return OfficialBotChannel(_broker_get, _broker_post,
                              lambda: st.get("official_cursor", ""),
                              lambda c: st.set("official_cursor", c),
                              on_unlinked=_official_unlinked,
                              post_media=_broker_post_media)


def _broker_post_media(path: str, fields: dict, file_path: str, content_type: str):
    """Multipart upload of a CV preview picture or PDF to the broker relay, which streams it
    straight to Telegram and keeps nothing (docs/notify.md). (status, json_or_None)."""
    import requests
    try:
        with open(file_path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return 400, {"error": "file is missing"}
    name = "cv.png" if content_type == "image/png" else "cv.pdf"

    def go():
        return requests.post(f"{BROKER_URL}{path}", data=fields,
                             files={"file": (name, blob, content_type)},
                             headers=_broker_headers(), timeout=60)
    try:
        r = go()
        if r.status_code == 401 and _recover_from_401():
            r = go()
    except requests.RequestException:
        return 0, None
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def _official_unlinked() -> None:
    """The broker says we're no longer linked (the person blocked the bot, or unlinked from
    another device): forget it now so the next tick falls back to the own bot or none."""
    _OFFICIAL_CACHE.update({"at": time.time(), "linked": False, "data": {"linked": False}})


def _official_status(force: bool = False) -> tuple[bool, object]:
    """(linked, channel). Cached for a minute so the 30 s loop doesn't ask the broker every tick.
    NOTIFY_OFFICIAL=0 turns the official bot off (the test suite sets it)."""
    if os.environ.get("NOTIFY_OFFICIAL", "1") == "0":
        return False, None
    now = time.time()
    if force or now - _OFFICIAL_CACHE["at"] >= _OFFICIAL_TTL:
        ch = _official_channel()
        try:
            data = ch.status()
        except Exception:   # noqa: BLE001 - unreachable / not configured: not linked
            data = None
        _OFFICIAL_CACHE.update({"at": now, "linked": bool((data or {}).get("linked")),
                                "data": data})
    return _OFFICIAL_CACHE["linked"], (_official_channel() if _OFFICIAL_CACHE["linked"] else None)


def _own_channel():
    """The person's own bot as a channel, or None when it isn't set up."""
    from notify.channel import OwnBotChannel
    try:
        bot = _make_bot()
    except RuntimeError:
        return None
    return OwnBotChannel(bot, _tg_offset, _set_tg_offset)


def _active_channel():
    from notify.channel import select_channel
    return select_channel(_official_status, _own_channel)


def _site_name(url: str) -> str:
    from submit.allowlist import evidence_for
    from submit.policy import _host
    e = evidence_for(_host(url))
    return e.ats if e else ""


def _can_auto_apply(job: dict) -> bool:
    """Honest gate for [Apply for me]: the site is on the VERIFIED auto allowlist AND the
    person turned autonomous submission on. Everything else is tailor-and-queue."""
    from submit.policy import submission_policy
    return _autonomous_on() and submission_policy(job.get("url") or "") == "auto"


class _JobAlertActions:
    """What the job-alert buttons do. Tailoring runs here, on this machine, from the saved
    profile; submission only through the existing per-site path. ``take_ready`` hands the
    hub the record a tap just tailored, so the chat gets its preview (notify/cvreview.py)."""

    def __init__(self):
        self._ready = None

    def take_ready(self):
        rid, self._ready = self._ready, None
        return rid

    def _job(self, alert: dict) -> dict:
        sid = alert.get("sid") or ""
        w = _watchlist()
        try:
            job = w.get_job(sid)
        finally:
            w.close()
        if not job:
            job = _job_from_feed(sid)
        return job or {"source_id": sid, "title": alert.get("title") or "",
                       "company": alert.get("company") or "", "location": alert.get("location") or "",
                       "url": alert.get("url") or "", "source": alert.get("source") or ""}

    def _tailor(self, alert: dict) -> dict:
        snap = _memory().load("default") or {}
        if not (snap.get("profile") or {}).get("experience"):
            return {"ok": False, "reason": "Add your profile in SponsorJobs first, then I can build these."}
        tex, tname = _load_template(None)
        scratch = "_tgalert_" + uuid.uuid4().hex[:8]
        try:
            return _tailor_job_unattended(self._job(alert), _make_llm(), tex, tname, scratch)
        finally:
            try:
                _memory().delete(scratch)
            except Exception:   # noqa: BLE001
                pass

    def queue(self, alert: dict) -> str:
        who = f"{alert.get('title') or 'the role'} at {alert.get('company') or 'the company'}"
        try:
            out = self._tailor(alert)
        except Exception as exc:   # noqa: BLE001 - say so instead of going silent
            traceback.print_exc()
            return _friendly_failure(who, exc)
        if not out.get("ok"):
            return f"I couldn't finish {who}: {out.get('reason') or 'it needs your review'}."
        self._ready = out["id"]
        cov = out.get("coverage")
        cov_bit = f", {round(cov)}% JD match" if isinstance(cov, (int, float)) else ""
        return (f"Ready: {who} is in your review queue (#{out['id']}{cov_bit}). "
                "Open SponsorJobs to review it and click submit.")

    def apply(self, alert: dict) -> str:
        who = f"{alert.get('title') or 'the role'} at {alert.get('company') or 'the company'}"
        try:
            out = self._tailor(alert)
        except Exception as exc:   # noqa: BLE001
            traceback.print_exc()
            return _friendly_failure(who, exc)
        if not out.get("ok"):
            return f"I couldn't finish {who}: {out.get('reason') or 'it needs your review'}."
        self._ready = out["id"]
        res = _submit_record_id(out["id"]) or {}
        if res.get("status") == "auto_submitted":
            return f"Applied: {who} was submitted for you (#{out['id']})."
        return (f"Tailored {who} (#{out['id']}) but did not submit: "
                f"{res.get('message') or 'it is waiting in your review queue'}")

    def dismiss(self, sid: str) -> None:
        w = _watchlist()
        try:
            w.dismiss(sid)
        finally:
            w.close()

    def autonomous_ok(self, alert: dict) -> bool:
        return _can_auto_apply(alert)


def _notify_hub():
    from notify.alerts import AlertEngine
    from notify.batch import BatchReview
    from notify.hub import NotifyHub
    from notify.service import NotifyService
    state = _notify_state()
    batch = BatchReview(None, _BatchActions(), approve_all_enabled=_prefs()["approve_all"])
    return NotifyHub(state, AlertEngine(state, match_fn=_alert_match_score),
                     NotifyService(None, _RecordActions()),
                     batch_callback=batch.act, job_actions=_JobAlertActions(),
                     review=_cv_review(state))


def _cv_review(state=None):
    """The Telegram CV review (preview, Show changes, Send PDF, chat edits with Accept/Undo and
    versions). Works on this machine's records and PDFs; the model only plans and rewords."""
    from notify.cvreview import CVReview
    return CVReview(state or _notify_state(), _records, _make_llm, WORKDIR,
                    lambda name: _load_template(name)[0],
                    can_apply=_record_can_auto_apply, apply=_record_apply_text,
                    update_profile=_update_profile_fact)


def _record_can_auto_apply(rid) -> bool:
    rec = _get_record(rid)
    if not rec:
        return False
    data = _record_data(rec)
    if (data.get("status") or "ready") == "applied":
        return False
    return _can_auto_apply({"url": (data.get("source_job") or {}).get("url") or ""})


def _record_apply_text(rid) -> str:
    """[Apply for me] after a review: re-checked at tap time, then the per-site submit path."""
    rec = _get_record(rid)
    who = f"#{rid}"
    if rec:
        who = f"{rec.get('jd_label') or rec.get('role') or 'the role'}" + (
            f" at {rec['company']}" if rec.get("company") else "")
    if not _record_can_auto_apply(rid):
        return f"{who} is waiting in your review queue. Open SponsorJobs to submit."
    res = _submit_record_id(int(rid)) or {}
    if res.get("status") == "auto_submitted":
        return f"Applied: {who} was submitted for you (#{rid})."
    return f"I didn't submit {who}: {res.get('message') or 'it is waiting in your review queue'}"


def _update_profile_fact(change: dict, doc: dict) -> bool:
    """"Also update my profile": one confirmed fact into the SAVED profile ('default')."""
    from notify.cvdoc import apply_fact_to_profile
    mem = _memory()
    saved = mem.load("default") or {}
    prof = saved.get("profile") or {}
    if not apply_fact_to_profile(prof, change, doc):
        return False
    mem.save("default", prof, saved.get("essentials") or {}, saved.get("history") or [])
    return True


def _alert_match_score(row: dict):
    """The real fit for a shortlisted role: fetch its JD (feed shard / board / local store) and
    run the same _job_match the detail pane shows. None when there is no JD or no profile."""
    from notify.alerts import match_percent
    sid = row.get("source_id") or ""
    jd = (row.get("jd_text") or "").strip()
    if not jd:
        w = _watchlist()
        try:
            job = w.get_job(sid)
        finally:
            w.close()
        jd = ((job or {}).get("jd_text") or "").strip()
    if not jd:
        job = _job_from_feed(sid)
        jd = ((job or {}).get("jd_text") or "").strip()
    return match_percent(_job_match(jd, row.get("title") or "")) if jd else None


def _alert_rows() -> tuple[list[dict], str]:
    """(rows, marker): the board's current rows and a marker that changes when the feed
    refreshes (static feed generated_at, or the local crawl's jobs_refreshed_at)."""
    rows, marker = [], ""
    if _feed_serving():
        try:
            jobs, header = _static_feed().jobs()
            rows = [j for j in jobs if j.get("us") is not False]
            marker = "feed:" + str(header.get("generated_at") or "")
        except Exception:   # noqa: BLE001 - fall through to the local store
            rows = []
    w = _watchlist()
    try:
        local = _sponsors().tag_jobs(w.list_jobs(order="recent", limit=3000))
    finally:
        w.close()
    have = {r.get("source_id") for r in rows}
    rows = rows + [j for j in local if j.get("us") and j.get("source_id") not in have]
    marker += "|local:" + str(_sponsors().get_meta("jobs_refreshed_at") or "")
    return rows, marker


def _scan_job_alerts(force: bool = False) -> int:
    """After a feed refresh (marker changed), pick new strong matches into the alert queue."""
    from datetime import datetime

    from notify.alerts import AlertEngine, profile_targets
    state = _notify_state()
    if not state.prefs().get("job_alerts"):
        return 0
    saved = _memory().load("default") or {}
    profile = saved.get("profile") or saved.get("essentials") or {}
    if not (profile.get("skills") or profile.get("experience")):
        return 0
    rows, marker = _alert_rows()
    if not force and marker == state.get("feed_marker"):
        return 0
    picked = AlertEngine(state, match_fn=_alert_match_score).pick(
        rows, profile_targets(profile), datetime.now().astimezone())
    state.set("feed_marker", marker)
    return len(picked)


def _notify_records() -> list[dict]:
    recs = _records()
    try:
        rows = recs.list(limit=200)
    finally:
        recs.close()
    return [{**r, "data": _record_data(r)} for r in rows]


def _notify_tick(scan: bool = False) -> dict:
    """One tick: poll the active channel, dispatch, send due alerts and the away digest.
    Serialised so the loop and an on-demand poll never handle the same tap twice."""
    from datetime import datetime
    with _NOTIFY_TICK_LOCK:
        ch = _active_channel()
        if ch is None:
            return {"ok": False, "channel": "none", "handled": 0}
        if scan:
            try:
                _scan_job_alerts()
            except Exception:   # noqa: BLE001 - alerts are best-effort
                traceback.print_exc()
        return _notify_hub().tick(ch, datetime.now().astimezone(), _can_auto_apply,
                                  records_fn=_notify_records, site_name_fn=_site_name)


def start_notify_loop():
    """Tick every NOTIFY_INTERVAL seconds while the app runs. The first tick handles anything
    the person tapped while the app was closed. Never under tests/dev."""
    if _NOTIFY_LOOP["thread"] is not None or _under_test_or_dev():
        return None

    def run():
        n = 0
        while True:
            try:
                # Taps every tick; the feed check (cheap marker compare, then scoring) every
                # NOTIFY_SCAN_EVERY ticks, so reading the board doesn't run twice a minute.
                _notify_tick(scan=(n % NOTIFY_SCAN_EVERY == 0))
            except Exception:   # noqa: BLE001 - the loop must outlive any one failure
                traceback.print_exc()
            n += 1
            time.sleep(NOTIFY_INTERVAL)

    t = threading.Thread(target=run, name="notify-loop", daemon=True)
    _NOTIFY_LOOP["thread"] = t
    t.start()
    return t


@app.post("/api/notify/presence")
@_guard
def notify_presence():
    """The person is using the app (throttled ping from the UI). The away digest only goes out
    for work done while they were NOT here."""
    _notify_state().set("presence_at", time.time())
    return jsonify({"ok": True})


@app.get("/api/notify/settings")
@_guard
def notify_settings():
    linked, _ch = _official_status(force=True)
    data = _OFFICIAL_CACHE.get("data") or {}
    own = bool(_cred("TELEGRAM_BOT_TOKEN") and _cred("TELEGRAM_CHAT_ID"))
    return jsonify({"ok": True, "prefs": _notify_state().prefs(),
                    "channel": "official" if linked else ("own" if own else "none"),
                    "official": {"linked": linked, "username": data.get("username") or "",
                                 "linked_at": data.get("linked_at") or "",
                                 "paused": bool(linked and data.get("paused"))},
                    "own_configured": own})


@app.post("/api/notify/settings")
@_guard
def notify_settings_set():
    return jsonify({"ok": True, "prefs": _notify_state().set_prefs(request.json or {})})


@app.post("/api/notify/telegram/link")
@_guard
def notify_telegram_link():
    """Start linking the official bot: the broker returns a one-time code and a t.me link."""
    from notify.channel import ChannelError
    try:
        d = _official_channel().link()
    except ChannelError as exc:
        return jsonify({"ok": False, "error": exc.code}), 200
    return jsonify({"ok": True, "url": d.get("url") or "", "code": d.get("code") or "",
                    "expires_in": d.get("expires_in") or 600})


@app.get("/api/notify/telegram/status")
@_guard
def notify_telegram_status():
    linked, _ch = _official_status(force=True)
    data = _OFFICIAL_CACHE.get("data") or {}
    return jsonify({"ok": True, "linked": linked, "username": data.get("username") or "",
                    "paused": bool(linked and data.get("paused"))})


@app.post("/api/notify/telegram/unlink")
@_guard
def notify_telegram_unlink():
    from notify.channel import ChannelError
    try:
        _official_channel().unlink()
    except ChannelError as exc:
        return jsonify({"ok": False, "error": exc.code}), 200
    _OFFICIAL_CACHE.update({"at": 0.0, "linked": False, "data": None})
    _notify_state().set("official_cursor", "")
    return jsonify({"ok": True})


@app.post("/api/sponsors/refresh")
@_guard
def sponsors_refresh():
    """Bring the H-1B sponsor data up to date from public USCIS files (GREEN lane) and
    SponsorJobs' own quarterly snapshot, then make sure the bundled PERM / E-Verify lists are
    loaded. Fiscal years are optional in the body.

    What "up to date" honestly means: USCIS published one CSV per fiscal year for
    FY2009..FY2023 and then moved the data into a Tableau dashboard with no file URL. So a
    404 for FY2024+ is the normal state of the world, reported as `unavailable_fys`, never
    as an error, and the newest years arrive via the snapshot (TAILOR_DATA_URL) instead.
    The default year list probes the newer years quietly, then the last five published."""
    body = request.json or {}
    fys = body.get("fiscal_years") or h1b_default_fiscal_years()
    force = bool(body.get("force"))
    db = _sponsors()
    # Never leave the person with zero H-1B rows when the seed we ship has ~105k of them:
    # if the per-year downloads all 404 (they do, from FY2024 on) this is the data they get.
    # (The startup load may still be running in the background: wait for it rather than
    # merging the same seed twice.)
    _join_h1b_seed()
    _load_bundled_h1b(db)
    # H-1B ingest is ADDITIVE per employer (sums approvals across years), so ingesting a
    # fiscal year that's ALREADY counted doubles that year's approvals. Track which FYs
    # are ingested and skip them, otherwise every 'Update visa data' click corrupts the
    # counts. `force` re-ingests (only meaningful after a manual DB reset).
    done = db.h1b_fys_ingested()
    ingested, loaded, errors, skipped, unavailable = 0, [], [], [], []
    for fy in fys:
        fy = int(fy)
        if fy in done and not force:
            skipped.append(fy)
            continue
        try:
            rows = download_h1b_rows(fy)
        except H1BYearUnavailable:
            unavailable.append(fy)              # USCIS publishes no file for this year
            continue
        except Exception as exc:                # noqa: BLE001 - a real failure (blocked, offline)
            errors.append({"fy": fy, "error": str(exc)})
            continue
        if not rows:
            unavailable.append(fy)
            continue
        ingested += db.ingest_h1b_rows(rows)
        done.add(fy)
        loaded.append(fy)
    db.set_h1b_fys_ingested(done)
    if loaded:
        db.set_meta("h1b_updated_at", _now_iso())
    snapshot = _sync_h1b_snapshot(db)
    _load_bundled_perm(db)      # green-card data ships with the app; loads instantly
    _load_bundled_everify(db)   # STEM-OPT list IF a bundle has been added (see scripts/)
    stats = db.stats()
    years = sorted(db.h1b_fys_ingested())
    if snapshot.get("status") == "merged":
        note = (f"Loaded SponsorJobs' {snapshot.get('version')} H-1B snapshot "
                f"({snapshot.get('rows', 0):,} employers).")
    elif loaded:
        note = "Loaded H-1B fiscal year" + ("s " if len(loaded) > 1 else " ") + \
               ", ".join(str(y) for y in sorted(loaded)) + " from USCIS."
    else:
        note = H1B_REFRESH_NOTE
    if years:
        span = f"FY{years[0]}" if len(years) == 1 else f"FY{years[0]} to {years[-1]}"
        note += f" Your H-1B data covers {span}."
    return jsonify({"ok": not errors, "ingested": ingested,
                    "loaded_fys": sorted(loaded), "skipped_fys": skipped,
                    "unavailable_fys": sorted(unavailable), "fiscal_years": years,
                    "errors": errors, "snapshot": snapshot, "note": note, **stats})


# The committed sponsor seed (seed/sponsors_seed.csv.gz, ~340k employers with a real
# signal, ~105k of them H-1B). PERM and E-Verify have their own bundles below; this is the
# only bundled source of H-1B rows, so a database that has none gets them from here.
# None means "ROOT/seed/sponsors_seed.csv.gz, resolved at call time" (ROOT is relocated in
# the packaged app and patched in tests); a test may point it at a tiny seed of its own.
BUNDLED_H1B_SEED = None


def _load_bundled_h1b(db) -> int:
    """If the sponsor DB holds no H-1B rows, merge the H-1B columns of the bundled seed and
    record the fiscal years they cover (so a later per-year download never double counts
    them). Mirrors _load_bundled_perm: a no-op when data is present or the seed is absent,
    and a failure is printed, never raised."""
    from sourcing.sponsors import read_seed_rows
    seed = Path(BUNDLED_H1B_SEED) if BUNDLED_H1B_SEED else ROOT / "seed" / "sponsors_seed.csv.gz"
    if db.stats().get("h1b") or not seed.exists():
        return 0
    try:
        n = db.merge_aggregated_rows(read_seed_rows(seed), h1b_only=True)
        if n:
            db.set_h1b_fys_ingested(db.h1b_fys_ingested() | set(db.h1b_fiscal_years()))
            db.set_meta("h1b_updated_at", _now_iso())
            db.set_meta("h1b_source", "bundled seed")
        return n
    except Exception:   # don't crash startup, but don't hide a corrupt bundle either
        traceback.print_exc()
        return 0


def _sync_h1b_snapshot(db) -> dict:
    """Merge SponsorJobs' quarterly H-1B snapshot when TAILOR_DATA_URL is set (a base URL such as
    https://tailor.example/data). Unset, unreachable, or already ingested: a quiet no-op."""
    base = os.environ.get("TAILOR_DATA_URL", "").strip()
    if not base:
        return {"status": "disabled"}
    try:
        return sync_h1b_snapshot(db, base)
    except Exception as exc:    # noqa: BLE001 - never let the optional extra break a refresh
        traceback.print_exc()
        return {"status": "unreachable", "reason": str(exc)[:200]}


# Green-card sponsors distilled from DOL PERM and BUNDLED with the app (scripts/
# build_perm_bundle.py), so end-users get green-card badges automatically -- no 76MB
# download (DOL blocks automated download) and no manual import.
BUNDLED_PERM = ROOT / "config" / "perm_sponsors.csv"


def _load_bundled_perm(db) -> None:
    import csv as _csv
    if db.stats().get("perm") or not BUNDLED_PERM.exists():
        return
    try:
        with open(BUNDLED_PERM, encoding="utf-8-sig") as f:
            db.ingest_perm_counts(_csv.DictReader(f))
        db.set_meta("perm_updated_at", _now_iso())
    except Exception:   # don't crash startup, but don't hide a corrupt bundle either
        traceback.print_exc()


# STEM-OPT / E-Verify list, bundled with the app. Built from an official participating-
# employers file (the 2018 public baseline, or a FOIA release) via
# scripts/build_everify_bundle.py. USCIS publishes no live download, so the distilled list
# ships gzipped in the repo (~3.6 MB vs ~13 MB raw) and is decompressed on load; the user
# can swap in a fresher file via the Import button anytime. A plain .csv (e.g. produced by
# the Import path) is honored too.
BUNDLED_EVERIFY_GZ = ROOT / "config" / "everify_employers.csv.gz"
BUNDLED_EVERIFY = ROOT / "config" / "everify_employers.csv"


def _load_bundled_everify(db) -> None:
    import csv as _csv
    import gzip as _gzip
    if db.stats().get("e_verify"):
        return
    try:
        if BUNDLED_EVERIFY_GZ.exists():
            with _gzip.open(BUNDLED_EVERIFY_GZ, "rt", encoding="utf-8-sig") as f:
                db.ingest_everify_rows(_csv.DictReader(f))
        elif BUNDLED_EVERIFY.exists():
            with open(BUNDLED_EVERIFY, encoding="utf-8-sig") as f:
                db.ingest_everify_rows(_csv.DictReader(f))
        else:
            return
        db.set_meta("everify_updated_at", _now_iso())
    except Exception:   # surface a corrupt/truncated bundle instead of silently loading none
        traceback.print_exc()


@app.post("/api/sponsors/import")
@_guard
def sponsors_import():
    """Ingest an OFFICIAL disclosure file the person downloaded themselves, a USCIS H-1B
    Data Hub .csv, a DOL PERM .xlsx (green-card sponsors), or an E-Verify employer export
    (.csv/.xlsx, STEM-OPT eligible). USCIS/DOL block automated bulk download, so importing
    the official file is the clean, GREEN-lane way to get these flags, no scraping, no
    browser masquerade, no bypass (the honest downloader falls back here when blocked)."""
    from werkzeug.utils import secure_filename
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "Choose a USCIS H-1B .csv, a PERM .xlsx, or an "
                                 "E-Verify .csv/.xlsx file."}), 400
    kind = (request.form.get("kind") or "").lower()
    name = secure_filename(f.filename) or "import"
    tmp = _DATA / "imports"
    tmp.mkdir(parents=True, exist_ok=True)
    dest = tmp / f"{_now_iso().replace(':', '')}_{name}"
    f.save(dest)
    db = _sponsors()
    try:
        rows = parse_perm_xlsx(dest) if str(dest).lower().endswith(".xlsx") \
            else parse_tabular_file(dest)
        if not rows:
            return jsonify({"error": "No rows found in that file."}), 400
        headers = {str(h).strip().lower() for h in rows[0].keys()}
        # Auto-detect by columns: H-1B Data Hub (approval columns) vs PERM (case-status) vs
        # an E-Verify employer list. An explicit `kind` always wins.
        is_h1b = kind == "h1b" or (kind == "" and
                 any("initial approval" in h or "continuing approval" in h for h in headers))
        is_perm = kind == "perm" or (kind == "" and not is_h1b and
                  any("case_status" in h or "case status" in h for h in headers))
        if is_h1b:
            n = db.ingest_h1b_rows(rows)
            db.set_meta("h1b_updated_at", _now_iso())
            flag = "H-1B"
        elif is_perm:
            n = db.ingest_perm_rows(rows)
            db.set_meta("perm_updated_at", _now_iso())
            flag = "green-card (PERM)"
        else:
            n = db.ingest_everify_rows(rows)
            db.set_meta("everify_updated_at", _now_iso())
            flag = "E-Verify / STEM-OPT"
        return jsonify({"ok": True, "kind": flag, "employers_in_file": n, **db.stats()})
    except Exception as exc:
        return jsonify({"error": f"Could not read that file: {exc}"}), 400


@app.post("/api/jobs/dismiss")
@_guard
def jobs_dismiss():
    sid = (request.json or {}).get("source_id", "")
    w = _watchlist()
    try:
        w.dismiss(sid)
        return jsonify({"ok": True})
    finally:
        w.close()


@app.post("/api/jobs/save")
@_guard
def jobs_save():
    """Bookmark (or un-bookmark) a role. Personal, so it lives in the LOCAL db, never the shared
    kitchen. The client sends the job snapshot so a kitchen-sourced role can be saved even though
    it isn't in the local DB -- the Saved list then survives it ageing out of the live feed."""
    body = request.json or {}
    sid = (body.get("source_id") or "").strip()
    if not sid:
        return jsonify({"error": "missing source_id"}), 400
    saved = bool(body.get("saved", True))
    w = _watchlist()
    try:
        w.set_saved(sid, saved, body.get("job") if saved else None)
        return jsonify({"ok": True, "saved": saved})
    finally:
        w.close()


def _feed_detail_response(job: dict):
    """The /api/jobs/detail payload for a static-feed row (same shape as the local-store path).
    The MATCH is computed locally from the user's private profile."""
    from sourcing.ats import salary_from_text
    from sourcing.filters import salary_insight
    jd = job.get("jd_text") or ""
    if not (job.get("salary") or "").strip():
        job["salary"] = salary_from_text(jd)
    return jsonify({"job": job, "jd": jd, "match": _job_match(jd, job.get("title", "")),
                    "pay_insight": salary_insight(job, _static_feed().benchmarks()),
                    "jd_preview": False})


def _feed_job_with_jd(sid: str) -> dict | None:
    """One role from the static feed's cached list, with `jd_text` filled from its JD shard or,
    when the shard lacks it (e.g. SmartRecruiters rows are crawled list-only), from the company's
    own public board endpoint. None when the feed doesn't list this source_id."""
    from sourcing.ats import html_to_text
    from sourcing.feedclient import fetch_board_jd
    sf = _static_feed()
    row = sf.get(sid)
    if row is None:
        return None
    job = dict(row)
    jd = (sf.jd(sid) or "").strip()
    if not jd:
        # The shard has no description, so the crawl never checked this row's JD for a
        # citizenship / clearance / no-sponsorship bar: check it now that we have it. A role that
        # excludes international candidates is treated as not in the feed.
        from sourcing.quality import role_excludes_international
        jd = fetch_board_jd(job)
        if jd and role_excludes_international(job.get("company", ""), jd):
            return None
    job["jd_text"] = html_to_text(jd).strip()
    return job


def _job_from_feed(sid: str) -> dict | None:
    """Resolve a job by source_id from the static feed (JOBS_FEED_URL) when it isn't in the local
    store: feed rows are never upserted locally, so the local lookup misses them. Same source the
    detail pane uses to show the JD. Returns a job dict with jd_text filled, or None."""
    if not sid or not _feed_serving():
        return None
    try:
        return _feed_job_with_jd(sid)
    except Exception:  # noqa: BLE001 - a feed blip just means we couldn't resolve it
        return None


@app.post("/api/session/start_job")
@_guard
def start_from_job():
    """Tailor a specific watchlist role: load its JD into the CV builder.

    Explicit, per-role action, we never auto-tailor every match.
    """
    body = request.json or {}
    sid = body.get("source_id", "")
    w = _watchlist()
    try:
        job = w.get_job(sid)
        if job:
            w.mark_seen(sid)
    finally:
        w.close()
    # Feed rows are served straight from the static feed and never upserted into the local store,
    # so w.get_job misses them. Fall back to the feed (same source the detail pane already used to
    # show this JD) instead of dead-ending with "job not found".
    if not job:
        job = _job_from_feed(sid)
    if not job:
        return jsonify({"error": "job not found, try refreshing"}), 404
    jd = (job.get("jd_text") or "").strip() or (
        f"{job.get('title', '')} at {job.get('company', '')}. {job.get('location', '')}.")
    llm = _count_package(_make_llm())   # one tailoring run = one package
    tex, tname = _load_template(body.get("template"))
    jobname = "cv" + uuid.uuid4().hex[:8]
    session = WebIntake(jd, tex, llm, _memory(), _records(), WORKDIR,
                        jobname=jobname, palace_dir=PALACE_DIR, template_name=tname)
    # carry the real company/title from the posting into the header/record
    session.company = (job.get("company") or "") or session.company
    session.role = (job.get("title") or "") or session.role
    # remember where this application goes, so the submission policy (submit/) can decide
    # auto vs. assisted for it later.
    session.source_job = {"url": job.get("url") or "", "source": job.get("source") or "",
                          "source_id": sid}
    _SESSION["s"] = session
    return jsonify(session.start())


def _select_focus_jobs(all_jobs: list, titles: list, locations: list,
                       sponsor_only: bool, tag_fn=None) -> list:
    """Narrow the watchlist to the person's FOCUS for today before the autopilot tailors
    (Kofi's "tell SponsorJobs what to focus on applying to that day"). Empty focus = all jobs,
    the prior behavior. `tag_fn` (SponsorDB.tag_jobs) is injected so this stays unit-testable.
    Order is preserved, so the caller still takes the freshest matches."""
    picked = list(all_jobs)
    if titles or locations:
        from sourcing.service import matches_criteria
        crit = {"titles": titles, "locations": locations, "remote": "any"}
        picked = [j for j in picked if matches_criteria(j, crit)]
    if sponsor_only and tag_fn is not None:
        picked = [j for j in tag_fn(picked) if (j.get("visa") or [])]
    return picked


@app.post("/api/autoapply/plan")
@_guard
def autoapply_plan():
    """The BRAIN of SponsorJobs' IN-APP autonomous applier. The Electron shell extracts the fields of the
    form open in the in-app browser pane and posts them here with the application record. We ask the
    model (through the broker) which data goes in which field -- it sees ONLY the field structure and
    placeholder KEYS, never the person's real data -- then resolve that plan to real values LOCALLY
    and return concrete ops the pane fills in (the person watches, approves, and submits).

    Honest gate: refuses bot-prohibited sites (LinkedIn etc.) via fill_permitted, so those stay
    fill-by-hand; returns needs_assist (never a guess) whenever it can't plan confidently."""
    from autoapply import applicant_from_record, build_fill_plan, resolve_plan
    from autoapply.fields import FormField
    from submit.policy import fill_permitted
    b = request.json or {}
    url = str(b.get("url") or "").strip()
    if not fill_permitted(url):
        return jsonify({"ok": False, "needs_assist": True,
                        "reason": "This site is fill-by-hand only. Open it and apply yourself."})
    rec = _get_record(b.get("record_id")) if b.get("record_id") not in (None, "") else None
    data = _record_data(rec) if rec else {}
    fields = []
    for f in (b.get("fields") or [])[:120]:
        if isinstance(f, dict) and str(f.get("ref") or "").strip():
            fields.append(FormField(
                ref=str(f["ref"]), label=str(f.get("label") or ""),
                type=str(f.get("type") or "text"),
                options=[str(o) for o in (f.get("options") or [])][:40],
                required=bool(f.get("required"))))
    if not fields:
        return jsonify({"ok": False, "needs_assist": True, "reason": "No form fields found."})
    role = (rec.get("role") if rec else "") or ""
    company = (rec.get("company") if rec else "") or ""
    jd = data.get("jd_text") or data.get("jd") or ""
    plan = build_fill_plan(fields, _make_llm(), role=role, company=company, jd=jd)
    if not plan:
        return jsonify({"ok": False, "needs_assist": True,
                        "reason": "Could not plan this form confidently. Fill it in and I'll help."})
    prof = data.get("render_profile") or data.get("profile") or {}
    if not (prof.get("identity") or {}).get("name"):
        prof = _saved_full_profile() or prof             # fall back to the saved profile's identity
    applicant = applicant_from_record({"profile": prof})
    resume = ""
    if rec:
        cv = WORKDIR / f"cv-{rec['id']}.pdf"
        resume = str(cv) if cv.exists() else ""
    ops = resolve_plan(plan, applicant, resume_path=resume)
    return jsonify({"ok": True, "filled": len(ops),
                    "ops": [{"ref": o.ref, "op": o.op, "text": o.text, "path": o.path,
                             "sensitive": o.is_sensitive} for o in ops]})


def _tailor_job_unattended(job: dict, llm, tex: str, tname: str, scratch: str) -> dict:
    """Tailor ONE role from the SAVED profile with no one present, into the review queue.
    Shared by the autopilot (/api/autoapply/run) and the Telegram [Tailor and queue] button.
    Persists to the throwaway ``scratch`` identity; "default" is loaded but never written.
    Returns {ok, id, role, company, coverage} or {ok: False, role, reason}. Never submits.
    Each role is one package on the bundled AI (counted here, before any model call)."""
    from llm.broker_client import BrokerUnavailable
    try:
        llm = _count_package(llm)
    except BrokerUnavailable as exc:
        if exc.reason != "upgrade_required":
            raise
        return {"ok": False, "role": job.get("title") or "a role", "reason": str(exc),
                "upgrade": True}
    jd = job.get("jd_text") or (f"{job.get('title', '')} at {job.get('company', '')}. "
                                f"{job.get('location', '')}.")
    session = WebIntake(jd, tex, llm, _memory(), _records(), WORKDIR,
                        jobname="cv" + uuid.uuid4().hex[:8], palace_dir=PALACE_DIR,
                        template_name=tname)
    # Redirect ALL persistence (SQLite row via _persist/accept, and verbatim palace
    # turns via _append) to the scratch identity, "default" is loaded but untouched.
    session.profile_name = scratch
    session.palace = session.palace.__class__(
        PALACE_DIR or (WORKDIR / "palace"), person=scratch)
    session.company = job.get("company") or session.company
    session.role = job.get("title") or session.role
    session.source_job = {"url": job.get("url") or "", "source": job.get("source") or "",
                          "source_id": job.get("source_id") or ""}
    session.essentials["override"] = True   # autonomous, build with what's saved
    # Truly unattended: there is no one to answer a question, so this is the one
    # path allowed past the page-filling gate (build_from_saved). Interactive
    # builds must still collect those sections first.
    session.essentials["unattended"] = True
    session.build_from_saved()
    if session.stage != "review":           # needs a critical field it can't infer
        return {"ok": False, "role": session.role or job.get("title", "a role"),
                "reason": "needs a detail we don't have"}
    res = session.accept()
    if res.get("ok"):
        return {"ok": True, "id": res["record_id"], "role": session.role,
                "company": session.company, "coverage": res.get("coverage")}
    return {"ok": False, "role": session.role, "reason": "held for your review"}


@app.post("/api/autoapply/run")
@_guard
def autoapply_run():
    """Auto-apply (the paid autopilot, CLAUDE.md §4c): tailor the person's FOCUS roles from
    the SAVED profile, no per-role input, and queue each package for review. Submission stays
    on the existing guarded path (verified allowlist + opt-in), so this run never submits on
    its own; it fills the review queue for the person to approve."""
    body = request.json or {}
    count = max(1, min(int(body.get("count") or 5), 10))
    snap = _memory().load("default") or {}
    base_profile = snap.get("profile") or {}
    if not base_profile.get("experience"):
        return jsonify({"error": "Add your profile first (drop your resume), then SponsorJobs can "
                                 "build these for you."}), 400

    # What KINDS of roles to tailor today (all optional; empty = the freshest roles).
    focus = body.get("focus") or {}
    titles = [str(t).strip() for t in (focus.get("titles") or []) if str(t).strip()]
    locations = [str(l).strip() for l in (focus.get("locations") or []) if str(l).strip()]
    sponsor_only = bool(focus.get("sponsor_only"))

    w = _watchlist()
    try:
        all_jobs = [w.get_job(r["source_id"]) or r for r in w.list_jobs()]
    finally:
        w.close()
    if not all_jobs:
        return jsonify({"error": "No roles in your feed yet, open Jobs to add sources."}), 400
    picked = _select_focus_jobs(all_jobs, titles, locations, sponsor_only,
                                tag_fn=_sponsors().tag_jobs)
    if not picked:
        return jsonify({"error": "No fresh roles match today's focus. Broaden or clear it, "
                                 "or open Jobs to add more sources."}), 400
    jobs = picked[:count]

    llm = _make_llm()   # real model; clear error if the key/connectivity is missing
    tex, tname = _load_template(None)
    # Tailor into a throwaway scratch profile, never the shared "default" row: each session
    # LOADS the pristine "default" (so every role tailors from the real base) but PERSISTS to
    # the scratch identity. A concurrent request reading "default" can't observe a half-built,
    # wrong-role profile. The scratch row (and its palace turns) are deleted at the end.
    scratch = "_autoapply_" + uuid.uuid4().hex[:8]
    tailored, skipped = [], []
    ctrl = _control()          # P3: the person can stop/skip this run from Telegram while it runs
    halted = False
    for job in jobs:
        # Cooperate with Telegram control between items: /stop halts the run gracefully after the
        # current one; /skip (no id) drops just the next item. Checked fresh each iteration.
        if ctrl.paused():
            halted = True
            break
        if ctrl.take_skip_current():
            skipped.append({"role": job.get("title", "a role"), "reason": "skipped by you"})
            continue
        try:
            out = _tailor_job_unattended(job, llm, tex, tname, scratch)
        except Exception as exc:
            skipped.append({"role": job.get("title", "a role"), "reason": str(exc)[:80]})
            continue
        if out.get("ok"):
            tailored.append({k: out[k] for k in ("id", "role", "company", "coverage")})
        else:
            skipped.append({"role": out.get("role") or job.get("title", "a role"),
                            "reason": out.get("reason") or "held for your review"})
    try:
        _memory().delete(scratch)   # drop the throwaway row; "default" was never written
    except Exception:
        pass
    return jsonify({"ok": True, "queued": len(tailored), "tailored": tailored,
                    "skipped": skipped, "considered": len(jobs), "halted": halted,
                    "matched": len(picked),
                    "focus": {"titles": titles, "locations": locations,
                              "sponsor_only": sponsor_only}})


def main() -> None:
    """Run SponsorJobs as a desktop app: serve locally, then open its own window.

    This is a local-first desktop app (§5) that renders with web tech; it is not a website.
    Starting it as a bare server and leaving a person to type 127.0.0.1:57000 into a tab
    showed the product through a URL bar and a bookmarks strip, which is the wrong
    reference for judging every screen in it.

    --server-only keeps the old behaviour for tests, headless runs, and anyone who wants to
    point their own browser at it.
    """
    import threading

    start_auto_updater()   # keep the job feed fresh on its own (only if RESUME_AGENT_AUTOUPDATE=1)
    start_notify_loop()    # Telegram: taps, job-match alerts, the away digest (every 30 s)
    server_only = "--server-only" in sys.argv or os.environ.get("TAILOR_SERVER_ONLY") == "1"
    url = "http://127.0.0.1:57000"
    if server_only:
        print(f"SponsorJobs: serving at {url} (server only)")
        app.run(host="127.0.0.1", port=57000, debug=False)
        return

    from ui.shell import open_app_window

    # Serve on a daemon thread so closing the window ends the process rather than orphaning
    # a server on the port, which is what would strand the next launch.
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=57000, debug=False,
                               use_reloader=False),
        daemon=True).start()
    # Wait for the port rather than sleeping a guessed interval: the first request must not
    # land on a socket that isn't listening yet.
    import socket
    import time
    for _ in range(100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", 57000)) == 0:
                break
        time.sleep(0.05)
    proc = open_app_window(url, _DATA / "shell-profile")
    if proc is None:
        print(f"SponsorJobs: no Chrome or Edge found, opened {url} in your browser instead.")
        proc_wait = None
    else:
        print(f"SponsorJobs is running. Close the window to quit.")
        proc_wait = proc
    try:
        if proc_wait is not None:
            proc_wait.wait()          # the window IS the app: when it closes, we're done
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
