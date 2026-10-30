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
import secrets
import threading
import time
import traceback
import uuid
from collections import OrderedDict
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
from submit.filenames import export_filename, upload_copy  # noqa: E402

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
                    # The Resumes page strip shows the FEATURED few (the fields that get most
                    # US sponsorship: software, data, engineering, then finance); "More
                    # templates" shows all of them grouped by category, in manifest order.
                    "featured": bool(m.get("featured")),
                    "order": int(m.get("order") or 999),
                    "preview_url": f"/api/templates/{name}/preview.pdf"})
    out.sort(key=lambda t: (t["order"], t["name"]))
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
        if seniority >= 5 and (m.get("category") or "") == "Experienced Professionals":
            score += 2
        if score > best_score:
            best, best_score, best_hits = m, score, hits
        elif best is not None and score == best_score and score > 0 \
                and _prefer_on_tie(m, hits, best, best_hits):
            best, best_hits = m, hits
    if not best or not best_score:
        return {"name": _DEFAULT_TEMPLATE, "reason": "", "matched": []}
    reason = best.get("best_for") or ""
    if seniority >= 5 and (best.get("category") or "") == "Experienced Professionals":
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
        "certifications": ["AWS Certified Solutions Architect, Associate, 20XX",
                           "Project Management Professional (PMP), 20XX"],
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


def _start_feed_download() -> None:
    """Begin the static feed's download (when one is due) on its own background thread, so the
    list is on its way before the first screen asks for it. Never blocks; never raises."""
    if not _feed_base_url():
        return
    try:
        sf = _static_feed()
        sf.start_refresh()
        sf.warm()            # and the cached copy is parsed now, so the first search is instant
    except Exception:                                # noqa: BLE001 - the board still asks later
        traceback.print_exc()


def _feed_status() -> dict:
    """The static feed's download state for the board ("downloading 4.2 MB", or a failure with a
    retry): cheap, no network. {"configured": False} when no feed is set."""
    if not _feed_base_url():
        return {"configured": False}
    return _static_feed().status()


def _feed_loading() -> bool:
    """Is the FIRST download of the job list still running (nothing cached to show yet)?"""
    st = _feed_status()
    return bool(st.get("configured") and st.get("downloading") and not st.get("has_cache"))


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


def _dismissed_ids() -> set:
    """The source_ids the person dismissed, to drop from a kitchen-sourced feed."""
    w = _watchlist()
    try:
        return w.dismissed_ids()
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
        # The store runs in WAL mode: drop the empty DB's journal siblings so SQLite never
        # replays a stale -wal over the freshly copied snapshot.
        for side in ("-wal", "-shm"):
            Path(str(target) + side).unlink(missing_ok=True)
        shutil.copyfile(seed, target)
    except Exception:
        # A failed seed must never block startup: the app still works, the person can
        # still click Update visa data, and the badges just start empty as before.
        traceback.print_exc()


_SPONSORS_OPEN_LOCK = threading.Lock()


def _sponsors() -> SponsorDB:
    """The process-wide visa-sponsor store. Safe to share across request threads: SponsorDB
    gives each thread its own SQLite connection. Opened (and seeded) once per data dir, under a
    lock so two first requests arriving together never both seed and load the bundles."""
    key = str(_SPONSORS_DB) if _SPONSORS_DB is not None else str(_DATA / "sponsors.db")
    db = _SPONSORS.get(key)
    if db is not None:
        return db
    with _SPONSORS_OPEN_LOCK:
        return _open_sponsors(key)


def _open_sponsors(key: str) -> SponsorDB:
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
        _load_bundled_depth(db)
        return
    with _H1B_SEED_LOCK:
        if _h1b_seed_running():
            return

        def run():
            try:
                n = _load_bundled_h1b(db)
                if n:
                    print(f"[sponsors] bundled H-1B seed loaded: {n} employers")
                # The per-employer depth (year rows, PERM by year, LCA wages) rides the same
                # background thread: it is detail behind the badge, never worth a slow start.
                _load_bundled_depth(db)
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
        tok = _account_token()
        if not tok and os.environ.get("TAILOR_EDITION") == "official":
            # The hosted broker accepts accounts only, never the dev header. Without a token
            # every call would 401 and the CV import would quietly fall back to a dumb parse
            # (seen 2026-10-07 when the sign-up limit was hit). Say what happened instead.
            raise Unavailable(
                "no_account",
                "SponsorJobs could not set up your free account just now (too many new "
                "sign-ups from this network today, or the service is busy). Try again in a "
                "while, or add your own AI key under Settings, Advanced.")
        return BrokerLLM(model=os.environ.get("RESUME_AGENT_MODEL"), auth_token=tok)
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
            from submit.stages import submitted_by
            recs.merge_data(rid, {"status": "applied", "applied_at": _now_iso(),
                                  **submitted_by("you")})
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
            "coverage": round(c["coverage"] or 0), "when": _local_day(c["created_at"]),
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
        data = _json.loads(rec.get("data") or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Letters drafted while records were titled "Role \u00b7 Classic" quote the template name
    # ("the Role \u00b7 Classic position"). Cut it on every read so it is never shown or sent.
    if isinstance(data.get("cover_letter"), str):
        from intake.template_manifest import strip_template_label
        data["cover_letter"] = strip_template_label(data["cover_letter"])
    return data


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
        # Notices the apply agent accepted for the person, with their consent (company, title, url, at).
        "terms_accepted": [x for x in (data.get("terms_accepted") or []) if isinstance(x, dict)],
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
            "match": _record_fit(r, data),
            "when": _local_day(r.get("created_at")),
            "status": data.get("status") or "ready",
            "stage": effective_stage(data),               # the funnel: saved..rejected
            "follow_up_due": (fu or {}).get("due"),        # when to nudge yourself (if applied)
            "follow_up_is_due": bool(fu and fu["is_due"]),
            "has_cover_letter": bool((data.get("cover_letter") or "").strip()),
            "screening_count": len(data.get("screening") or []),
            "folder": str(data.get("folder") or ""),
            "sort": data.get("sort"),
            # The layout, as its own field (never in the title), so two variants of one
            # application are still told apart on Resumes.
            "template_label": _template_label(data.get("template")),
        })
    return jsonify({"records": out})


def _local_stamp(ts) -> str:
    """A SQLite CURRENT_TIMESTAMP (stored in UTC, no offset) as local wall-clock ISO with its
    offset, the form the tracker board stores (sourcing.board.now_iso). "" when empty; an
    unparseable value comes back as it was."""
    from datetime import datetime, timezone
    raw = str(ts or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().isoformat(timespec="seconds")


def _local_day(ts) -> str:
    """The person's own calendar day for a SQLite CURRENT_TIMESTAMP (stored in UTC). Slicing the
    UTC string showed an evening's resume as made "tomorrow" (QA 2026-10-14)."""
    return _local_stamp(ts)[:10]


def _template_label(name) -> str:
    if not name:
        return ""
    from intake.template_manifest import load_manifest
    return str(load_manifest(str(name)).get("display_name") or "")


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
        cur = _record_data(rec)
        patch = stage_patch(cur, "applied" if status == "applied" else "saved", _now_iso())
        if status == "applied" and cur.get("status") != "applied":
            from submit.stages import submitted_by
            patch.update(submitted_by("you", (cur.get("submission") or {}).get("tier") or ""))
        elif status == "ready":
            patch.update({"submission": None, "queued": False})   # back to review, clean slate
        data = recs.merge_data(rid, patch)
    finally:
        recs.close()
    _board_follow_record(rec, "applied" if status == "applied" else "saved")
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
    _board_follow_record(rec, stage)
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
    """Ready to apply: applications the person APPROVED that are not submitted yet (waiting
    for their click on the site, queued for an auto-submit, or an auto attempt that came back
    as "Needs you"), each with its destination URL and submission lane. Approving an item
    adds it here; submitting or skipping it takes it off."""
    from submit import classify_record
    from submit.stages import app_stage, is_waiting_to_submit
    recs = _records()
    try:
        rows = recs.list()
    finally:
        recs.close()
    out = []
    for r in rows:
        data = _record_data(r)
        if not is_waiting_to_submit(data):
            continue        # still to review, submitted, or skipped: not on this list
        st = app_stage(data)
        out.append({
            "id": r["id"], "role": r.get("role") or "", "company": r.get("company") or "",
            "apply_url": (data.get("source_job") or {}).get("url") or "",
            "lane": classify_record(data)["tier"],     # 'auto' | 'assisted'
            "stage": st["stage"], "stage_label": st["label"], "stage_detail": st["detail"],
            "reason": st["reason"],
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
                  "inbox_auto_verify": False, "folders": [], "tracker_weekly_goal": 10,
                  "aa_min_fit": 60,   # Auto-apply's fit floor (autoapply/picker.py)
                  "aa_skip": [],      # roles the person took out of the next Auto-apply run
                  "aa_focus": {},     # the page's focus fields (titles, locations, sponsor_only)
                  "autopilot": False, # tailor new matches on a schedule while the person is away
                  "autopilot_per_day": 2,   # at most this many packages a day (1..10)
                  "portal_accounts": True,  # the apply agent may create employer portal accounts
                  "mail_alias": "",         # the person's SponsorJobs address (docs/mail-alias.md)
                  "apply_with_alias": True} # apply with it, so codes and links reach the agent


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
    if "tracker_weekly_goal" in patch:       # the Tracker page's applications-per-week target
        cur["tracker_weekly_goal"] = max(0, min(500, int(patch["tracker_weekly_goal"])))
    if "aa_skip" in patch:                   # source_ids skipped from Auto-apply's Up next
        cur["aa_skip"] = [str(x)[:300] for x in (patch["aa_skip"] or []) if str(x).strip()][-500:]
    if "aa_min_fit" in patch:                # Auto-apply's fit floor, a match percent
        from autoapply.picker import clamp_min_fit
        cur["aa_min_fit"] = clamp_min_fit(patch["aa_min_fit"], cur.get("aa_min_fit", 60))
    if "aa_focus" in patch:                  # the focus fields, so Autopilot tailors the same list
        f = patch["aa_focus"] if isinstance(patch["aa_focus"], dict) else {}
        cur["aa_focus"] = {"titles": _split(f.get("titles"))[:20],
                           "locations": _split(f.get("locations"))[:20],
                           "sponsor_only": bool(f.get("sponsor_only"))}
    if "autopilot" in patch:                 # OFF by default; the person turns it on
        cur["autopilot"] = bool(patch["autopilot"])
    if "autopilot_per_day" in patch:
        from autoapply.picker import clamp_per_day
        cur["autopilot_per_day"] = clamp_per_day(patch["autopilot_per_day"],
                                                 cur.get("autopilot_per_day", 2))
    if "portal_accounts" in patch:           # "Create portal accounts for me" (docs/apply-agent.md 4)
        cur["portal_accounts"] = bool(patch["portal_accounts"])
    if "mail_alias" in patch:
        cur["mail_alias"] = str(patch["mail_alias"] or "").strip()[:120]
    if "apply_with_alias" in patch:
        cur["apply_with_alias"] = bool(patch["apply_with_alias"])
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
    from autoapply.picker import clamp_per_day
    p = _prefs()
    ts, today = _now_ts_today()
    return jsonify({
        "autonomous": _autonomous_on(),
        "approve_all": p["approve_all"],
        "daily_cap": p["daily_cap"],
        "min_gap": p["min_gap"],
        "inbox_auto_verify": bool(p.get("inbox_auto_verify")),
        "autopilot": bool(p.get("autopilot")),
        "autopilot_per_day": clamp_per_day(p.get("autopilot_per_day")),
        "portal_accounts": bool(p.get("portal_accounts", True)),
        "telegram_configured": _active_channel() is not None,
        "rate": _rate_limiter().status(ts, today),
    })


@app.get("/api/submit/decision")
@_guard
def submit_decision():
    """May the desktop shell fill this page, and may it click the site's submit? Asked by
    shell-electron/main.js before every in-app Apply, so the rule lives in ONE tested place
    (submit.policy.apply_decision): submit is clicked only on the verified AUTO allowlist."""
    from submit.policy import apply_decision
    return jsonify(apply_decision(request.args.get("url", ""), request.args.get("page_url", "")))


@app.post("/api/submit/settings")
@_guard
def submit_settings_set():
    """Update settings. Body may set {autonomous, approve_all, daily_cap, min_gap, autopilot,
    autopilot_per_day}. autonomous, approve_all and autopilot default OFF and must be turned on
    deliberately."""
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


def _auto_result_patch(data: dict, result: dict) -> dict:
    """The record patch for one auto-lane submit attempt (submit.stages): sent -> submitted
    by the auto lane (and on the funnel as applied, follow-up armed); any other answer from
    the site -> back to the person as "Needs you" with a reason; not attempted -> unchanged
    stage with the honest message."""
    from submit.stages import failed_patch, submitted_by
    from submit.tracker import stage_patch
    st = result.get("status")
    if st == "auto_submitted":
        at = result.get("submitted_at") or _now_iso()
        return {**stage_patch({**data, "applied_at": at}, "applied", at), "queued": False,
                **submitted_by("auto", "auto", result.get("message") or "", at)}
    if st == "auto_failed" or (st == "assisted" and result.get("reason")):
        return failed_patch(result)
    return {"submission": {"tier": result.get("tier"), "status": st,
                           "message": result.get("message") or "", "at": result.get("submitted_at")}}


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
                data = {**data, "profile": _saved_full_profile(),   # honest identity to submit
                        "cv_pdf_path": str(WORKDIR / f"cv-{rid}.pdf")}
                result = submit_record(data, driver=driver, now=_now_iso())
                if result["status"] == "auto_submitted":
                    _rate_limiter().record(ts, today)   # count it against the cap
                elif result.get("posted"):
                    _rate_limiter().record_attempt(ts, today)   # a real POST fired -> space the next
        else:
            result = submit_record(data, driver=None, now=_now_iso())
        # Only a real auto-submit marks applied; a site's "no" goes back to the person.
        recs.merge_data(rid, _auto_result_patch(_record_data(rec), result))
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
    from submit.stages import app_stage
    plan = classify_record(data)
    st = app_stage(data)
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
        # Where it stands (submit/stages.py): to review / approved / needs you, in true words.
        # Nothing is "filled" at review time; what we can say is whether the profile has the
        # contact details a form will ask for.
        "stage": st["stage"], "stage_label": st["label"], "stage_detail": st["detail"],
        "reason": st["reason"],
        "missing_contact": [k for k in ("name", "email") if not ident.get(k)],
        "screening_count": len(data.get("screening") or []),
        "has_cover_letter": bool((data.get("cover_letter") or "").strip()),
        "coverage": cov.get("ratio") if cov.get("ratio") is not None else round(rec.get("coverage") or 0),
        "cv_path": _resume_upload_path(rec) if cv.exists() else "",
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
        from submit.stages import app_stage, approved_patch, queued_patch
        recs = _records()
        try:
            rec = recs.get(rid)
            if not rec:
                return {"ok": False, "message": f"#{rid} not found."}
            data = _record_data(rec)
            who = rec.get("company") or "application"
            if (data.get("status") or "ready") == "applied":   # never re-submit (idempotent)
                return {"ok": True, "stage": "submitted", "message": f"Already submitted: {who}."}
            url = (data.get("source_job") or {}).get("url") or ""
            driver = driver_for(_host(url))     # approval IS the consent for THIS item
            if driver is not None:
                # Auto-lane (verified), this really submits over the sanctioned API, so it
                # is rate-limited (daily cap + spacing). Over the limit -> queue for later.
                ts, today = _now_ts_today()
                allowed, reason = _rate_limiter().allow(ts, today)
                if not allowed:
                    recs.merge_data(rid, {**queued_patch(reason), "approved_at": _now_iso()})
                    return {"ok": True, "stage": "approved",
                            "message": f"Approved: {who}, queued to submit for you ({reason})."}
                result = submit_record({**data, "profile": _saved_full_profile(),
                                        "cv_pdf_path": str(WORKDIR / f"cv-{rid}.pdf")},
                                       driver=driver, now=_now_iso())
                if result["status"] == "auto_submitted":
                    _rate_limiter().record(ts, today)
                elif result.get("posted"):
                    _rate_limiter().record_attempt(ts, today)   # a real POST fired -> space the next
                # Un-queue on any non-success (failure / captcha-auth-wall drop-to-assisted): the
                # site already answered, so drain() must NOT keep re-POSTing this item every tick.
                merged = recs.merge_data(rid, _auto_result_patch(data, result))
                st = app_stage(merged)
                if st["stage"] == "submitted":
                    return {"ok": True, "stage": "submitted", "message": f"Submitted for you: {who}."}
                return {"ok": False, "stage": st["stage"], "reason": st["reason"],
                        "message": f"Not sent: {who}. {result['message']}"}
            # Assisted lane: never auto-submit, and nothing has been filled yet. Approval only
            # means "reviewed"; the person submits on the site.
            recs.merge_data(rid, {**approved_patch(), "approved_at": _now_iso()})
            return {"ok": True, "stage": "approved",
                    "message": f"Approved: {who}. Open the application and submit it."}
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
                     download_name=_export_name(rid, "cover"), max_age=0)
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
    resp = send_file(out, download_name=export_filename(_person_name(rec), "cover", "docx"),
                     max_age=0,
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
            z.write(pdf, arcname=export_filename(_person_name(rec), "resume"))
        cover = (data.get("cover_letter") or "").strip()
        if cover:
            cover_pdf = WORKDIR / f"cover-{rid}.pdf"
            if not cover_pdf.exists():
                _render_cover_pdf(rid, cover)   # build it for the bundle if we haven't yet
            if cover_pdf.exists():
                z.write(cover_pdf, arcname=export_filename(_person_name(rec), "cover"))
            z.writestr(export_filename(_person_name(rec), "cover", "txt"), cover)
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
    import shutil
    shutil.rmtree(WORKDIR / "uploads" / str(int(rid)), ignore_errors=True)   # the named copy
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

    from intake.template_manifest import headings, load_manifest, sections
    from tailoring.assembler import assemble_cv
    tex, tname = _load_template(name)
    profile = _placeholder_profile()
    man = load_manifest(tname)
    # A template may override parts of the sample (a nursing preview shows clinical
    # rotations and licenses, not a coding mentor). Still John Doe, still generic.
    if isinstance(man.get("preview"), dict):
        profile.update(man["preview"])
    # The header shows only the contact slots this template has (a nursing resume has no
    # GitHub or blog line), exactly as a real build of it would.
    from intake.template_manifest import identity_fields
    keep = set(identity_fields(man))
    profile["identity"] = {k: v for k, v in profile["identity"].items() if k in keep}
    secs = sections(man)
    heads = headings(man)
    # The key covers the BUILDER too (its source file), not only the template and the sample
    # data: an improved page-fitting pass otherwise never reached the gallery, and three
    # previews kept showing half-empty pages rendered before it (Kofi, 2026-10-08).
    from tailoring import assembler as _asm
    builder = Path(_asm.__file__).read_bytes()
    key = hashlib.sha1((tex + _json.dumps(profile, sort_keys=True)
                        + repr(secs) + repr(sorted(heads.items()))).encode("utf-8") + builder).hexdigest()[:10]
    out_pdf = WORKDIR / f"preview-{tname}-{key}.pdf"
    if not out_pdf.exists():
        WORKDIR.mkdir(parents=True, exist_ok=True)
        assemble_cv(tex, profile, "", None, WORKDIR,
                    jobname=out_pdf.stem, tailor=False, sections=secs, headings=heads)
    return out_pdf if out_pdf.exists() else None


@app.get("/api/templates/<name>/preview.pdf")
@_guard
def template_preview(name):
    """The rendered one-page preview PDF (placeholder data), for the "Open preview" link."""
    out_pdf = _template_preview_pdf(name)
    if not out_pdf:
        return jsonify({"error": "Couldn't render this template's preview."}), 500
    return _nostore(send_file(out_pdf, mimetype="application/pdf"))


def _thumb_width(raw) -> int:
    """The `w` query of a template thumbnail: 440 (the picker card) unless asked, 200 to 2000.
    The preview window asks for about twice its paper width, so it is sharp on a Retina screen."""
    try:
        w = int(raw)
    except (TypeError, ValueError):
        return 440
    return max(200, min(2000, w))


@app.get("/api/templates/<name>/thumb.png")
@_guard
def template_thumb(name):
    """First-page PNG of the template preview, so the picker cards, the Resumes strip and the
    preview window fill edge-to-edge (an image has no PDF-viewer grey gutter, and Chromium's
    viewer ignored every fit option inside a frame, 2026-10-26). `?w=` picks the width; each
    width is cached alongside the preview PDF."""
    out_pdf = _template_preview_pdf(name)
    if not out_pdf:
        return ("", 404)
    w = _thumb_width(request.args.get("w"))
    png = out_pdf.with_suffix(".png") if w == 440 else out_pdf.with_name(f"{out_pdf.stem}-{w}.png")
    try:
        if not (png.exists() and png.stat().st_mtime >= out_pdf.stat().st_mtime):
            from tailoring.preview import render_thumbnail_png
            render_thumbnail_png(out_pdf, png, width=w, dpi=220 if w <= 1600 else 300)
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


@app.post("/api/session/field")
@_guard
def edit_field():
    body = request.json or {}
    return jsonify(_session().edit_field(body.get("ref", ""), body.get("text", "")))


@app.post("/api/session/date")
@_guard
def edit_date():
    body = request.json or {}
    return jsonify(_session().edit_date(body.get("ref", ""), body.get("text", "")))


@app.post("/api/session/accept")
@_guard
def accept():
    return jsonify(_session().accept())


# ------------------------------------------------------------------ "Add this keyword"
# One missing JD term from the coverage report -> one honest, reviewable edit of one bullet
# (tailoring/keyword_action.py). The resume id is the live builder ("live"), the session's
# jobname, or the record id of the resume this session last saved; a resume that is not open
# in the builder has no page to edit surgically, so it answers 404.
def _keyword_session(cv_id: str):
    s = _SESSION.get("s")
    if s is None:
        return None
    cv_id = str(cv_id or "").strip()
    if cv_id in ("live", "current", getattr(s, "jobname", "")):
        return s
    if cv_id.isdigit() and str(getattr(s, "_last_record_id", "") or "") == cv_id:
        return s
    return None


def _keyword_term():
    return " ".join(str((request.json or {}).get("term") or "").split())


@app.post("/api/cv/<cv_id>/keyword/preview")
@_guard
def keyword_preview(cv_id):
    """Body {term}. Does the person's profile support this JD term, and if so what is the
    one-bullet change? -> {supported, ok, evidence, draft, target_bullet, before, after, ...}"""
    s = _keyword_session(cv_id)
    if s is None:
        return jsonify({"error": "Open this resume in the builder first."}), 404
    term = _keyword_term()
    if not term:
        return jsonify({"error": "No keyword given."}), 400
    return jsonify(s.keyword_preview(term))


@app.post("/api/cv/<cv_id>/keyword/apply")
@_guard
def keyword_apply(cv_id):
    """Body {term, draft}. Splices the draft surgically, recompiles, and returns the session
    state with the new coverage report, the page check and a unified diff of the .tex."""
    s = _keyword_session(cv_id)
    if s is None:
        return jsonify({"error": "Open this resume in the builder first."}), 404
    term = _keyword_term()
    if not term:
        return jsonify({"error": "No keyword given."}), 400
    draft = str((request.json or {}).get("draft") or "")
    return jsonify(s.keyword_apply(term, draft))


@app.post("/api/cv/<cv_id>/keyword/undo")
@_guard
def keyword_undo(cv_id):
    """Body {term}. Puts the previous bullet text back and recompiles."""
    s = _keyword_session(cv_id)
    if s is None:
        return jsonify({"error": "Open this resume in the builder first."}), 404
    term = _keyword_term()
    if not term:
        return jsonify({"error": "No keyword given."}), 400
    return jsonify(s.keyword_undo(term))


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
                     download_name=_export_name(_record_id_of(name[:-4]), "cover", "txt"),
                     max_age=0)
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
    # Saved as "<Name> Resume.pdf", never the internal cv-<id>.pdf storage name.
    nice = _export_name(_record_id_of(name[:-4]), "resume")
    if request.args.get("preview"):
        resp = send_file(_preview_safe_pdf(path), mimetype="application/pdf",
                         download_name=nice, max_age=0)
    else:
        resp = send_file(str(path), mimetype="application/pdf",
                         download_name=nice, max_age=0)
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
    resp = send_file(out, download_name=_export_name(int(rid) if rid.isdigit() else None,
                                                     "resume", "docx"), max_age=0,
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
_LOGO_CACHE: "OrderedDict[str, tuple]" = OrderedDict()   # domain -> (bytes, mimetype) | (None, None), LRU
_LOGO_CACHE_MAX = 400          # the page caches each logo for a week, so this only spares refetches
_LOGO_LOCK = threading.Lock()
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
    with _LOGO_LOCK:
        hit = _LOGO_CACHE.get(domain)
        if hit is not None:
            _LOGO_CACHE.move_to_end(domain)
    if hit is None:
        hit = _fetch_logo(domain)                     # (bytes, mimetype) or (None, None)
        with _LOGO_LOCK:
            _LOGO_CACHE[domain] = hit
            while len(_LOGO_CACHE) > _LOGO_CACHE_MAX:
                _LOGO_CACHE.popitem(last=False)
    data, mime = hit or (None, None)
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
    from sourcing.ats import ats_disabled
    if ats_disabled(ats):
        # e.g. SmartRecruiters: its API robots.txt disallows us, so we never read its boards.
        return jsonify({"error": f"We don't read {ats} boards: its robots.txt asks automated "
                                 "tools not to. Open the company's job page in your browser instead."}), 400
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


def _local_board_rows(w) -> list[dict]:
    """The LOCAL board: every stored role, sponsor-tagged, ad-stance + role-tagged, and reduced to
    the accessible set (US, not clearance-heavy, JD-checked, sponsor-relevant) with entry_level
    filled. Unfiltered and unpaginated: the feed route and the saved-search digest both start here."""
    from sourcing.adstance import UNKNOWN, ad_stance
    from sourcing.jobtags import job_tags
    from sourcing.quality import (employer_is_clearance_heavy, is_entry_level, is_jd_checked,
                                  is_sponsor_relevant)
    jobs = _sponsors().tag_jobs(w.list_jobs(order="recent", limit=15000))
    texts = w.get_jd_texts([j["source_id"] for j in jobs])
    for j in jobs:
        jd = texts.get(j["source_id"]) or ""
        st = ad_stance(jd)
        if st["stance"] != UNKNOWN:
            j["ad_stance"], j["ad_sentence"] = st["stance"], st["sentence"]
        j["tags"] = job_tags(j.get("title") or "", jd)
    jobs = [j for j in jobs
            if j.get("us") and not employer_is_clearance_heavy(j.get("company", ""))
            and is_jd_checked(j) and is_sponsor_relevant(j)]
    for j in jobs:
        j["entry_level"] = is_entry_level(j.get("title", ""))
    return jobs


def _board_rows() -> list[dict]:
    """The whole board as the person sees it, unfiltered: the static feed's cached list when a
    feed is configured and holds rows, else the local store. What the saved-search digest counts
    new arrivals from. Never raises: a broken feed just means the local rows."""
    if _feed_base_url():
        try:
            jobs, _header = _static_feed().jobs()
            if jobs:
                return jobs
        except Exception:                            # noqa: BLE001 - fall back to the local rows
            pass
    w = _watchlist()
    try:
        return _local_board_rows(w)
    finally:
        w.close()


def _newest_arrival(jobs) -> str | None:
    """When the newest job on the board first ARRIVED (its first_seen), or None. This is what the
    board's freshness label shows. refreshed_at only says when the list was last rebuilt, and that
    keeps ticking while no new job comes in: in August 2026 the board read "updated 2m ago" over a
    month of no new postings."""
    return max((str(j.get("first_seen") or "") for j in jobs), default="") or None


@app.get("/api/jobs/feed-status")
@_guard
def jobs_feed_status():
    """What the board polls while the first job list downloads: {downloading, bytes, total,
    has_cache, error, ...}. Light: reads memory only."""
    return jsonify(_feed_status())


@app.post("/api/jobs/feed-retry")
@_guard
def jobs_feed_retry():
    """The Retry button after a failed download: start a fresh background download now, past
    the back-off. Returns the new status at once."""
    if not _feed_base_url():
        return jsonify({"configured": False})
    _static_feed().start_refresh(force=True)
    return jsonify(_feed_status())


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
        from sourcing.feedclient import FeedLoading
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
                filtered = _jobs_personal(apply_facets(jobs, **facets), home=a.get("home") == "1")
                hidden = _dismissed_ids()                    # the person's own Dismiss, kept locally
                if hidden:
                    filtered = [j for j in filtered if j.get("source_id") not in hidden]
                page_jobs, count, page, per_page = paginate(filtered, a.get("page", 1),
                                                            a.get("per_page", 30))
                page_jobs = _jobs_mark([dict(j) for j in page_jobs])   # never mutate the cached rows
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
        except FeedLoading:
            # The first download of the list is still running (a slow link can take minutes).
            # Say so at once instead of holding the request or flashing an empty local board;
            # the client shows the progress and polls /api/jobs/feed-status until it lands.
            a = request.args
            return jsonify({"jobs": [], "count": 0, "total": 0, "page": 1,
                            "per_page": int(a.get("per_page", 30) or 30), "refreshed_at": None,
                            "newest_at": None, "stale": False, "source": "central",
                            "loading": True, "feed": _feed_status()})
        except Exception:                            # noqa: BLE001 - fall back to the local feed
            traceback.print_exc()
    w = _watchlist()
    try:
        from sourcing.filters import apply_facets, paginate
        from sourcing.quality import employer_is_clearance_heavy, is_entry_level
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
        jobs = _local_board_rows(w)
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
        filtered = _jobs_personal(filtered, home=a.get("home") == "1")
        page_jobs, count, page, per_page = paginate(filtered, a.get("page", 1), a.get("per_page", 30))
        page_jobs = _jobs_mark([dict(j) for j in page_jobs])
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
                        "crawl_error": crawl_error or None,
                        "feed": _feed_status() if feed_url else None})
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
                    "terms": _jd_terms(jd), "jd_preview": preview,
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


def _jd_terms(jd: str) -> list[str]:
    """The skills this ad asks for, profile or no profile: the description is underlined from
    the first launch so it reads as a skills list, not a wall of text. Deterministic, no model.
    Once a profile exists the same terms split into have / lack (see _job_match)."""
    if not jd:
        return []
    import re as _re
    from tailoring.keywords import vocab_skills
    out = []
    for t in vocab_skills(jd):
        # The curated vocabulary only (no capitalised-word heuristic, which underlined NASDAQ,
        # I-9 and U.S). A one- or two-letter skill (R, Go, C) must stand as its own token in
        # the ad; "Go-to-market" and "R&D" are not the languages.
        if len(t) <= 2 and not _re.search(r"(?<![\w&/+#-])" + _re.escape(t) + r"(?![\w&/+#-])", jd):
            continue
        out.append(t)
    return out[:80]


def _match_profile(snap: dict | None = None) -> dict:
    """The saved profile every job MATCH reads (Jobs fit, Auto-apply fit, alerts, the extension):
    the built profile (else the intake essentials) plus the skills typed under "Stated skills"."""
    from tailoring.keywords import with_stated_skills
    snap = snap if snap is not None else (_memory().load("default") or {})
    ess = snap.get("essentials") or {}
    return with_stated_skills(snap.get("profile") or ess or {}, ess)


def _job_match(jd: str, title: str = "") -> dict | None:
    """A deterministic (no-model) preview of how the SAVED PROFILE fits this role. Beyond a raw
    keyword %, it separates MUST-HAVE gaps (skills named in the required section) from nice-to-have
    ones, gives an honest verdict weighted toward the must-haves, and flags the role's seniority --
    so the read is 'strong on the essentials, but it's a senior role', not a flat number.
    None when there's no JD or no profile yet (the UI then invites setup)."""
    if not jd:
        return None
    profile = _match_profile()
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
    # One score for every surface (Jobs, Auto-apply, alerts): coverage, steadied by how close the
    # title is to the person's own roles, so an ad that names two skills can't read as a 100% fit.
    from notify.alerts import (THIN_JD_SKILLS, match_percent, profile_targets, profile_titles,
                               title_fit)
    tfit = title_fit(title, profile_targets(profile), profile_titles(profile))
    m = {"total": len(terms), "ratio": overall, "required_total": req_total,
         "required_covered": req_covered, "title_fit": tfit}
    score = match_percent(m) if terms else None
    if score is not None and len(terms) < THIN_JD_SKILLS:
        # A thin ad: the verdict follows the steadied score, never the raw keyword hit rate.
        order = ["weak", "moderate", "good", "strong"]
        cap = "weak" if score < 40 else "moderate" if score < 60 else "good" if score < 80 else "strong"
        verdict = order[min(order.index(verdict), order.index(cap))]
    return {
        "score": score,
        "title_fit": tfit,
        "total": len(terms),
        "covered": len(supported),
        "ratio": round(overall, 3),
        "present": supported[:80],      # every term: these drive the inline JD underlines
        "missing": missing[:80],
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


# ---- Profile photo: one image in the data dir, shown at the bottom of the sidebar. Local only.
_PHOTO = _DATA / "profile_photo"
_PHOTO_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def _photo_path():
    for ext in _PHOTO_TYPES.values():
        p = _PHOTO.with_suffix(ext)
        if p.exists():
            return p
    return None


@app.get("/api/profile/photo")
@_guard
def profile_photo():
    p = _photo_path()
    if not p:
        return ("", 404)
    return _nostore(send_file(p, max_age=0))


@app.post("/api/profile/photo")
@_guard
def profile_photo_set():
    f = request.files.get("file")
    if f is None or f.mimetype not in _PHOTO_TYPES:
        return jsonify({"error": "Choose a PNG, JPEG, WebP or GIF image."}), 400
    data = f.read(6 * 1024 * 1024 + 1)
    if len(data) > 6 * 1024 * 1024:
        return jsonify({"error": "That image is over 6 MB. Pick a smaller one."}), 400
    for ext in _PHOTO_TYPES.values():
        _PHOTO.with_suffix(ext).unlink(missing_ok=True)
    _DATA.mkdir(parents=True, exist_ok=True)
    _PHOTO.with_suffix(_PHOTO_TYPES[f.mimetype]).write_bytes(data)
    return jsonify({"ok": True})


@app.delete("/api/profile/photo")
@_guard
def profile_photo_clear():
    for ext in _PHOTO_TYPES.values():
        _PHOTO.with_suffix(ext).unlink(missing_ok=True)
    return jsonify({"ok": True})


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
    # Per-employer depth: which fiscal years the year-by-year history covers and what the
    # LCA wage bundle holds, so the UI can say "wages from FY2024 to 2026 filings".
    s["history_fiscal_years"] = db.h1b_year_fiscal_years()
    s["lca"] = db.lca_stats()
    s["lca_updated_at"] = db.get_meta("lca_updated_at")
    s["lca_bundle_version"] = db.get_meta("lca_bundle_version")
    return jsonify(s)


def _split_location(location: str) -> tuple[str, str]:
    """'Chicago, IL' / 'Austin, Texas, United States' -> ('Chicago', 'IL'); a location
    without a readable US city and state gives ('', '')."""
    import re as _re
    text = (location or "").strip()
    if not text:
        return "", ""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    state = ""
    for p in parts[1:]:
        m = _re.match(r"^([A-Za-z]{2})\b", p)
        if m and m.group(1).upper() in _US_STATE_CODES:
            state = m.group(1).upper()
            break
        full = p.lower()
        if full in _US_STATE_BY_NAME:
            state = _US_STATE_BY_NAME[full]
            break
    city = parts[0] if parts and state else ""
    if city.lower() in ("remote", "united states", "us", "usa", "anywhere"):
        city = ""
    return city, state


_US_STATE_CODES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split())
_US_STATE_BY_NAME = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "puerto rico": "PR",
}


@app.get("/api/sponsors/<path:name>/history")
@_guard
def sponsors_history(name):
    """Year-by-year record for one employer: H-1B approvals and denials per fiscal year
    (USCIS Data Hub), certified PERM cases per fiscal year (DOL), E-Verify enrollment, and
    the three flags (H-1B dependent and willful violator from the DOL LCA attestations;
    cap-exempt as the name/industry heuristic). Employer-level public history, never a promise about a role."""
    company = (name or "").strip()
    h = _sponsors().history(company) if company else None
    if not h:
        return _nostore(jsonify({"company": company, "matched": False, "years": [],
                                 "flags": {"h1b_dependent": False, "willful_violator": False,
                                           "cap_exempt": False, "e_verify": False}}))
    return _nostore(jsonify({"company": company, "matched": True, **h}))


def _wage_args(args) -> tuple[str, str, str]:
    title = (args.get("title") or "").strip()
    city, state = (args.get("city") or "").strip(), (args.get("state") or "").strip()
    if not (city and state) and args.get("location"):
        c2, s2 = _split_location(args.get("location") or "")
        city, state = city or c2, state or s2
    return title, city, state


@app.get("/api/sponsors/<path:name>/wages")
@_guard
def sponsors_wages(name):
    """What this employer paid for a title at a worksite, from DOL LCA disclosure data
    (certified H-1B filings): count, min / median / max annual wage, the prevailing-wage
    level split, narrowing from the exact title and city to the employer as a whole
    (`scope` says which). ?title=&city=&state= or ?location="Austin, TX"."""
    company = (name or "").strip()
    title, city, state = _wage_args(request.args)
    w = _sponsors().wages(company, title, city, state) if company else None
    return _nostore(jsonify({"company": company, "title": title, "city": city, "state": state,
                             "found": bool(w), "wages": w}))


@app.post("/api/sponsors/offer-check")
@_guard
def sponsors_offer_check():
    """Compare an offer with the employer's LCA wages for the title and place, state the
    likely prevailing-wage level and the weighted-lottery odds at it, and give three
    negotiation talking points. Pure arithmetic on public filings; the result carries its
    own "estimate, not legal advice" note. Nothing is stored."""
    from sourcing.lottery import offer_check
    body = request.json or {}
    company = str(body.get("company") or "").strip()
    title = str(body.get("title") or "").strip()
    city, state = str(body.get("city") or "").strip(), str(body.get("state") or "").strip()
    if not (city and state) and body.get("location"):
        c2, s2 = _split_location(str(body.get("location") or ""))
        city, state = city or c2, state or s2
    raw = str(body.get("offer") or "").replace(",", "").replace("$", "").strip().lower()
    try:
        offer = int(float(raw[:-1]) * 1000) if raw.endswith("k") else int(float(raw or 0))
    except ValueError:
        offer = 0
    if offer <= 0:
        return jsonify({"error": "Enter the annual salary in dollars, for example 125000."}), 400
    w = _sponsors().wages(company, title, city, state) if company else None
    out = offer_check(offer, w, title=title, city=city, state=state)
    out.update({"company": company, "title": title, "city": city, "state": state, "wages": w})
    return _nostore(jsonify(out))


# ---- Extension (P6, FrogHire parity): a profile-vs-JD keyword MATCH estimate and a read-only
# TRACKING mirror. Both extension-guarded (like the sponsor lookup) so no arbitrary site can call
# them. The match is an honest keyword-COVERAGE estimate (reuses the ATS coverage logic), never a
# real ATS/employer score (§8); with no profile it says so instead of inventing a number. ----
_MATCH_NOTE = "A profile-vs-JD keyword coverage estimate, not a real ATS or employer score."


@app.post("/api/match/score")
@_guard
@_extension_only
def match_score():
    from tailoring.keywords import (profile_text, skill_terms, split_required_optional,
                                    term_present)
    jd = str((request.json or {}).get("jd") or "").strip()
    snap = _memory().load("default") or {}
    profile = _match_profile(snap) if snap.get("profile") else {}
    has_profile = bool(profile.get("experience") or (profile.get("identity") or {}).get("name"))
    if not has_profile:
        return jsonify({"has_profile": False, "score": None, "matched": [], "missing": [],
                        "skills": [], "skills_covered": 0, "skills_total": 0,
                        "note": "Add your profile in SponsorJobs to see your match."})
    if not jd:
        return jsonify({"has_profile": True, "score": None, "matched": [], "missing": [],
                        "skills": [], "skills_covered": 0, "skills_total": 0, "note": _MATCH_NOTE})
    prof = profile_text(profile)
    # Skills only (curated vocabulary + acronyms): the broad term walk scored sentence words and
    # company names, which made the percentage meaningless. Deterministic here; the extension's
    # popup must not spend AI on every job page.
    terms = skill_terms(jd)
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


def _person_name(rec=None) -> str:
    """The person's name for an outgoing file's name: the name on THIS application's tailored
    CV when there is one, else the saved profile's. '' when none is known."""
    if rec:
        ident = (_record_data(rec).get("render_profile") or {}).get("identity") or {}
        if str(ident.get("name") or "").strip():
            return str(ident["name"])
    try:
        return str(_saved_identity().get("name") or "")
    except Exception:   # noqa: BLE001 - a filename never blocks a download
        return ""


def _record_id_of(job: str):
    """The record id in an internal file stem ('cv-12' / 'cover-12'), or None."""
    m = re.fullmatch(r"(?:cv|cover)-(\d+)", job or "")
    return int(m.group(1)) if m else None


def _export_name(rid, kind: str = "resume", ext: str = "pdf") -> str:
    """'<Name> Resume.pdf' / '<Name> Cover Letter.pdf' for application ``rid`` (or the live
    session when ``rid`` is None), never the internal cv-<id>.pdf storage name."""
    rec = None
    if rid is not None:
        try:
            rec = _get_record(rid)
        except Exception:   # noqa: BLE001
            rec = None
    return export_filename(_person_name(rec), kind, ext)


def _resume_upload_path(rec: dict) -> str:
    """The tailored resume as a file an upload can take its name from. A browser file input
    set through CDP sends the path's basename, so the in-app fill uploads a copy named after
    the person (WORKDIR/uploads/<id>/<Name> Resume.pdf), not cv-<id>.pdf. '' when no CV."""
    rid = int(rec["id"])
    cv = WORKDIR / f"cv-{rid}.pdf"
    if not cv.exists():
        return ""
    try:
        return str(upload_copy(cv, WORKDIR / "uploads" / str(rid),
                               export_filename(_person_name(rec), "resume")))
    except OSError:
        return str(cv)


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
        # Application answers the fill engine reuses on every form (work authorization,
        # sponsorship, EEO decline); edited on this screen, saved with /api/profile/answers.
        "answers": _profile_answers(),
    })


_DECLINABLE = ("github", "blog", "linkedin", "projects", "courses", "address",
               "extracurricular", "interests")
_SOFT_SKIPS = ("project_dates:", "project_location:")   # per-project Skips from the details step


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
    # The editor shows only the section-level skips; keep the details step's per-project ones.
    declined += [d for d in (ess.get("declined") or []) if str(d).startswith(_SOFT_SKIPS)]

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


def _template_shows_projects(name: str | None = None) -> bool:
    """Whether the template a build uses renders a Projects section (and so each project's
    dates): its manifest's ``sections``. Unreadable manifest: assume it does."""
    import json as _json
    name = (name or "").strip() or _DEFAULT_TEMPLATE
    try:
        m = _json.loads((ROOT / "config" / "templates" / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return "projects" in (m.get("sections") or ["projects"])


def _profile_missing_slots(include_soft: bool = True) -> list[dict]:
    """The slots the saved profile still lacks (ui/session._missing_slots), loaded exactly as a
    WebIntake session loads "default" (normalize_profile, _flatten_profile, the declined list):
    the HARD ones, plus the SOFT ones (a project's dates and location) when ``include_soft``
    and the template shows projects. One source for the onboarding step, "Add the details" on
    Auto-apply, and the run's pre-flight (HARD only). Empty without a profile: that is "add
    your resume"'s job."""
    from tailoring.assembler import normalize_profile
    from ui.session import _essentials_from, _flatten_profile, _missing_slots
    saved = _memory().load("default") or {}
    profile = saved.get("profile") or {}
    if not profile.get("experience"):
        return []
    ess = _essentials_from(saved.get("essentials") or {}, profile)
    soft = include_soft and _template_shows_projects(_load_template(None)[1])
    return [x for x in _missing_slots(*_flatten_profile(normalize_profile(profile)),
                                      declined=ess.get("declined") or [])
            if x["crit"] or (soft and x["soft"])]


def _autofill_saved_project_dates() -> list[str]:
    """Fill the saved profile's undated projects from their GitHub repos (ui/github_dates.py),
    before anyone is asked for them. Returns the projects filled; never raises (offline-safe)."""
    try:
        from ui.github_dates import fill_project_dates
        mem = _memory()
        saved = mem.load("default") or {}
        profile = saved.get("profile") or {}
        ess = saved.get("essentials") or {}
        gh = str((profile.get("identity") or {}).get("github") or "")
        cache = _DATA / "github_repo_dates.json"
        filled = fill_project_dates(profile.get("projects") or [], gh, cache, _cred("GITHUB_TOKEN"))
        if ess.get("projects"):
            filled += fill_project_dates(ess["projects"], gh, cache, _cred("GITHUB_TOKEN"))
        if filled:
            mem.save("default", profile, ess, saved.get("history") or [])
        return filled
    except Exception:   # noqa: BLE001 - a lookup never stops the person
        traceback.print_exc()
        return []


@app.get("/api/profile/missing")
@_guard
def profile_missing():
    """The details step's questions, in the gate's words: one slot per missing HARD field, and
    per SOFT one (a project's dates, after GitHub has filled what it can) the template shows."""
    _autofill_saved_project_dates()
    return jsonify({"ok": True, "missing": _profile_missing_slots()})


def _same(a, b) -> bool:
    return str(a or "").strip().lower() == str(b or "").strip().lower()


def _detail_targets(bag: dict, sec: str, match: dict) -> list[dict]:
    """The saved entries (and the dict the key lives on) a slot's ``match`` names, in both
    profile shapes: a grouped experience entry yields its matching roles, with the entry's
    own dict for the location; flat essentials rows stand for themselves."""
    out = []
    if sec == "experience":
        for e in bag.get("experience") or []:
            if not isinstance(e, dict) or not _same(e.get("org"), match.get("org")):
                continue
            for r in (e.get("roles") or [e]):
                if isinstance(r, dict) and _same(r.get("title"), match.get("title")):
                    out.append({"dates": r, "title": r, "org": e, "location": e})
    elif sec == "education":
        for d in bag.get("education") or []:
            if isinstance(d, dict) and _same(d.get("school"), match.get("school")) \
                    and _same(d.get("degree"), match.get("degree")):
                out.append({k: d for k in ("school", "degree", "date", "courses")})
    elif sec == "projects":
        for p in bag.get("projects") or []:
            # The gate names a project by its org or its title (ui/session._flatten_profile
            # keeps one of them), so either name finds it here.
            if isinstance(p, dict) and any(_same(p.get(k), match.get("name")) for k in ("title", "org")):
                out.append({k: p for k in ("location", "dates", "personal")})
    return out


def _apply_profile_details(profile: dict, ess: dict, answers: list, declines: list) -> None:
    """Land each answer on the entry its slot names, in BOTH saved bags, filling the first
    matching entry whose field is still empty (the one the gate asked about). A Skip on a
    SOFT slot records its key ("project_dates:<name>") in the declined list, so it is never
    asked again; a HARD slot has no Skip."""
    for a in answers:
        if not isinstance(a, dict):
            continue
        sec, key, match = str(a.get("sec") or ""), str(a.get("key") or ""), a.get("match") or {}
        val = str(a.get("value") or "").strip()
        if not val:
            continue
        if sec == "identity":
            if key == "contact":         # "an email or phone number": file it where it belongs
                key = "email" if "@" in val else "phone"
            if key not in IDENTITY_KEYS:
                continue
            for bag in (profile, ess):
                bag.setdefault("identity", {})[key] = val
            continue
        for bag in (profile, ess):
            for t in _detail_targets(bag, sec, match):
                if key in t and not str(t[key].get(key) or "").strip():
                    t[key][key] = val
                    break
    skips = [str(d.get("decline") or "") for d in declines if isinstance(d, dict)]
    skips = [k for k in skips if k.startswith(_SOFT_SKIPS)]
    if skips:
        ess["declined"] = sorted(set(ess.get("declined") or []) | set(skips))


@app.post("/api/profile/details")
@_guard
def profile_details_save():
    """Save the details step: {answers: [{sec, key, match, value}], declines: [{sec, match}]},
    each from a slot /api/profile/missing (or the import) handed out. Returns what is still
    missing, so the step re-asks only that. Refused while a resume session is open, like
    /api/profile/sections: its autosave would overwrite the edit."""
    if _SESSION.get("s") is not None:
        return jsonify({"error": "Finish or close the resume you're building first, "
                                 "its autosave would overwrite these edits."}), 409
    b = request.json or {}
    mem = _memory()
    saved = mem.load("default") or {}
    profile = saved.get("profile") or {}
    ess = saved.get("essentials") or {}
    _apply_profile_details(profile, ess, list(b.get("answers") or []), list(b.get("declines") or []))
    mem.save("default", profile, ess, saved.get("history") or [])
    return jsonify({"ok": True, "missing": _profile_missing_slots()})


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
    _autofill_saved_project_dates()     # a linked repo's dates, before asking for them
    profile = (_memory().load("default") or {}).get("profile") or profile
    ident = profile.get("identity") or {}
    # What the gate would still ask for: the wizard turns it into the details step right
    # away, so an unattended build never stalls later on a date the import could not read.
    return jsonify({"ok": True, "profile": profile, "missing": _profile_missing_slots(), "summary": {
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
    from tailoring.keywords import PROFILE_SECTIONS
    # Keep the person's sections only. A model that adds advice fields ("notes_for_candidate")
    # would otherwise have them saved, rendered into the evidence text, and counted as skills.
    out = {k: v for k, v in profile.items() if k in PROFILE_SECTIONS}
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
# Sponsorship has three honest answers: Yes (now), No, Future (not now, e.g. on OPT, but later).
_SPONSOR = {"", "Yes", "No", "Future"}


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

    # work_auth_us is the profile screen's name for the same answer the extension popup calls
    # work_authorized; both spellings are accepted and both are stored, so neither carrier breaks.
    work_auth = prefs.get("work_auth_us") or prefs.get("work_authorized")
    clean = {
        "work_authorized": work_auth if work_auth in _YN else "",
        "work_auth_us": work_auth if work_auth in _YN else "",
        "needs_sponsorship": prefs.get("needs_sponsorship") if prefs.get("needs_sponsorship") in _SPONSOR else "",
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
        # Terms a site requires to apply: "ask" (default) or "always" (accept without asking).
        # A write that does not carry it (the extension's whole-store save) keeps the saved one.
        "accept_required_terms": "always" if (prefs.get("accept_required_terms") if "accept_required_terms" in prefs
                                              else _load_prefs().get("accept_required_terms")) == "always" else "ask",
    }
    # The structured work-authorization profile (autoapply/workauth.py) is the truth; the flat
    # Yes/No answers above are derived from it so the extension and older code fill the same
    # answer. A write from an older surface (no work_auth in it) is carried into the US entry.
    from autoapply import workauth as _wa
    if isinstance(prefs.get("work_auth"), dict):
        wa = _wa.clean(prefs["work_auth"])
    else:
        prev = _load_prefs().get("work_auth")
        wa = _wa.apply_legacy(prev, clean) if isinstance(prev, dict) else None
    if wa is not None:
        clean["work_auth"] = wa
        for k, v in _wa.legacy_prefs(wa).items():
            clean[k] = v
    _DATA.mkdir(parents=True, exist_ok=True)
    _PREFS_PATH.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    return clean


def _merge_prefs(patch: dict) -> dict:
    """Update only the answers in ``patch``, keeping every other saved answer."""
    cur = _load_prefs()
    cur.update({k: v for k, v in (patch or {}).items() if k in _PREF_KEYS})
    if "work_auth_us" in (patch or {}):          # the profile screen's spelling wins when given
        cur["work_authorized"] = patch["work_auth_us"]
    legacy = ("work_auth_us", "work_authorized", "needs_sponsorship", "willing_to_relocate")
    if "work_auth" not in (patch or {}) and isinstance(cur.get("work_auth"), dict) \
            and any(k in (patch or {}) for k in legacy):
        from autoapply import workauth as _wa     # carry the flat answer into the structured profile
        cur["work_auth"] = _wa.apply_legacy(cur["work_auth"], patch)
    return _save_prefs(cur)


_PREF_KEYS = ("work_authorized", "work_auth_us", "needs_sponsorship", "willing_to_relocate",
              "over_18", "years_experience", "desired_salary", "earliest_start", "hear_about_us",
              "eeo_decline", "work_auth", "accept_required_terms")


def _profile_answers(prefs: dict | None = None) -> dict:
    """The three first-class application answers the profile screen shows."""
    p = prefs if prefs is not None else _load_prefs()
    wa = p.get("work_auth_us") or p.get("work_authorized") or ""
    return {"work_auth_us": wa if wa in _YN else "",
            "needs_sponsorship": p.get("needs_sponsorship") if p.get("needs_sponsorship") in _SPONSOR else "",
            "eeo_decline": bool(p.get("eeo_decline"))}


_RECIPES_PATH = ROOT / "autoapply" / "recipes.json"
_RECIPES_CACHE: dict = {}


def _recipes() -> dict:
    """The fill-recipe table (autoapply/recipes.json): one DATA entry per application system
    (URL patterns, selectors per profile field, continue/submit buttons to HIGHLIGHT, success
    markers). Read once per file change. Never personal data, never a click."""
    try:
        mtime = _RECIPES_PATH.stat().st_mtime
    except OSError:
        return {"version": 0, "systems": []}
    if _RECIPES_CACHE.get("mtime") != mtime:
        try:
            _RECIPES_CACHE["data"] = _json_prefs.loads(_RECIPES_PATH.read_text(encoding="utf-8"))
            _RECIPES_CACHE["mtime"] = mtime
        except (OSError, ValueError):
            return {"version": 0, "systems": []}
    return _RECIPES_CACHE.get("data") or {"version": 0, "systems": []}


@app.get("/api/autofill/recipes")
@_guard
def autofill_recipes():
    """The fill-recipe table for the fill engine (extension + desktop shell). Public data about
    public forms, so no extension lock; no-store so an edited table is picked up at once."""
    return _nostore(jsonify(_recipes()))


_LEARN_IDENTITY = {"name": "name", "email": "email", "phone": "phone", "address": "address",
                   "linkedin": "linkedin", "github": "github", "website": "blog"}


def _learn_from_corrections(body: dict) -> dict:
    """'Learn from corrections': the person changed a value the engine filled, and said yes to
    saving it. Contact fields land in the saved identity (both bags, like /api/profile/identity),
    screening answers in the reusable prefs. Only known keys, only non-empty values, length-capped."""
    fields = body.get("fields") or {}
    screening = body.get("screening") or {}
    patch: dict = {}
    first = str(fields.get("first_name") or "").strip()
    last = str(fields.get("last_name") or "").strip()
    if first or last:
        cur = str(_saved_identity().get("name") or "").strip()
        cf, cl = _split_name(cur)
        patch["name"] = " ".join(x for x in (first or cf, last or cl) if x).strip()
    for src, dst in _LEARN_IDENTITY.items():
        v = str(fields.get(src) or "").strip()[:200]
        if v:
            patch[dst] = v
    if "street" in fields or "city" in fields or "state" in fields or "zip" in fields:
        cur_addr = _parse_address(str(_saved_identity().get("address") or ""))
        parts = {k: str(fields.get(k) or cur_addr.get(k) or "").strip() for k in ("street", "city", "state", "zip")}
        tail = " ".join(x for x in (parts["state"], parts["zip"]) if x)
        addr = ", ".join(x for x in (parts["street"], parts["city"], tail) if x)
        if addr:
            patch["address"] = addr[:200]
    saved_identity = {}
    if patch:
        mem = _memory()
        try:
            saved = mem.load("default") or {}
            profile = saved.get("profile") or {}
            essentials = saved.get("essentials") or {"identity": {}, "education": [], "experience": []}
            profile.setdefault("identity", {}).update(patch)
            essentials.setdefault("identity", {}).update(patch)
            mem.save("default", profile, essentials, saved.get("history") or [])
            saved_identity = dict(essentials.get("identity") or {})
        finally:
            mem.close()
        sess = _SESSION.get("s")
        if sess is not None:
            try:
                sess.essentials.setdefault("identity", {}).update(patch)
                if isinstance(getattr(sess, "profile", None), dict):
                    sess.profile.setdefault("identity", {}).update(patch)
            except Exception:
                pass
    pref_patch = {k: v for k, v in screening.items()
                  if k in ("work_auth_us", "work_authorized", "needs_sponsorship",
                           "willing_to_relocate", "over_18")}
    if "eeo_decline" in body:
        pref_patch["eeo_decline"] = bool(body.get("eeo_decline"))
    prefs = _merge_prefs(pref_patch) if pref_patch else _load_prefs()
    learned_q = 0
    qa_list = [x for x in (body.get("answers") or []) if isinstance(x, dict)][:40]
    if qa_list:                                   # a typed answer the person fixed on a form
        lib = _answer_lib()
        try:
            for x in qa_list:
                if lib.save(str(x.get("question") or ""), str(x.get("answer") or ""), "you",
                            company=str(body.get("company") or "")):
                    learned_q += 1
        finally:
            lib.close()
    return {"ok": True, "learned": sorted(list(patch) + list(pref_patch)), "learned_answers": learned_q,
            "identity": {k: str(saved_identity.get(k) or "") for k in IDENTITY_KEYS} if patch else None,
            "answers": _profile_answers(prefs)}


def _url_key(url: str) -> str:
    """host + path, lowercased, without query/fragment: enough to say two URLs are one job page."""
    from urllib.parse import urlparse
    try:
        u = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    host = (u.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return (host + u.path.rstrip("/")).lower() if host else ""


def _mark_applied_from_page(body: dict) -> dict:
    """The fill engine saw the site's 'application received' marker AFTER THE PERSON submitted.
    Find the application record this page belongs to (by record id, else by the job URL the
    package was built from) and move it to 'applied' through the tracker, which also arms the
    follow-up nudge. No record for this page -> nothing is marked, and we say so."""
    from submit.tracker import stage_patch
    rid = body.get("record_id")
    keys = {k for k in (_url_key(body.get("url")), _url_key(body.get("page_url"))) if k}
    recs = _records()
    try:
        rec = None
        if rid not in (None, ""):
            try:
                rec = recs.get(int(rid))
            except (TypeError, ValueError):
                rec = None
        if rec is None and keys:
            for r in recs.list(limit=200):
                d = _record_data(r)
                cand = {_url_key((d.get("source_job") or {}).get("url")), _url_key(d.get("apply_url"))}
                if cand & keys:
                    rec = r
                    break
        if rec is None:
            return {"ok": True, "matched": False, "record_id": None}
        cur = _record_data(rec)
        patch = stage_patch(cur, "applied", _now_iso())
        if cur.get("status") != "applied":
            from submit.stages import submitted_by
            patch.update(submitted_by("you", (cur.get("submission") or {}).get("tier") or ""))
        data = recs.merge_data(rec["id"], patch)
    finally:
        recs.close()
    return {"ok": True, "matched": True, "record_id": rec["id"], "stage": data.get("stage"),
            "applied_at": data.get("applied_at"), "follow_up_due": data.get("follow_up_due")}


@app.post("/api/profile/autofill")
@_guard
@_extension_only
def profile_autofill_learn():
    """Learn from corrections: body {fields:{first_name,last_name,email,phone,street,city,state,
    zip,linkedin,github,website}, screening:{work_auth_us,needs_sponsorship,...}, eeo_decline}.
    Saves what the person corrected on a form into the profile so the next fill is right."""
    return _nostore(jsonify(_learn_from_corrections(request.json or {})))


@app.post("/api/autofill/applied")
@_guard
@_extension_only
def autofill_applied():
    """The engine detected the application-received page: mark that job applied.
    Body {url, page_url?, record_id?, title?, system?}."""
    return _nostore(jsonify(_mark_applied_from_page(request.json or {})))


@app.post("/api/autoapply/fill-report")
@_guard
@_extension_only
def autoapply_fill_report():
    """What the in-app fill did on one page of an application form: how many fields it filled
    and which it left for the person (by label). The desktop shell posts this after every page
    it fills, including the later pages of a multi-page form. Kept on the record as
    ``fill_report`` = {pages: [{page, url, filled, left, system, at}], updated}, one entry per
    page number (a re-fill of the same page replaces it), for the Auto-apply drawer.
    Body {record_id, page, url, filled, left:[labels], system?}."""
    b = request.json or {}
    try:
        rid = int(b.get("record_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "record_id is required."}), 400
    try:
        page = max(1, min(50, int(b.get("page") or 1)))
        filled = max(0, min(500, int(b.get("filled") or 0)))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "page and filled must be numbers."}), 400
    left = []
    for lab in (b.get("left") or [])[:60]:
        t = " ".join(str(lab or "").split())[:200]
        if t and t not in left:
            left.append(t)
    entry = {"page": page, "url": str(b.get("url") or "")[:500], "filled": filled, "left": left,
             "system": str(b.get("system") or "")[:40], "at": _now_iso()}
    recs = _records()
    try:
        rec = recs.get(rid)
        if not rec:
            return jsonify({"ok": False, "error": "That application was not found."}), 404
        cur = _record_data(rec).get("fill_report")
        pages = [p for p in ((cur or {}).get("pages") or []) if isinstance(p, dict)
                 and p.get("page") != page]
        pages = sorted(pages + [entry], key=lambda p: p.get("page") or 0)[-12:]
        report = {"pages": pages, "updated": entry["at"]}
        recs.merge_data(rid, {"fill_report": report})
    finally:
        recs.close()
    return _nostore(jsonify({"ok": True, "fill_report": report}))


def _phone_digits(phone) -> str:
    from autoapply.fields import phone_digits
    return phone_digits(phone)


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
        "email": _application_email(),
        "phone": _phone_digits(ident.get("phone")),
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
    # The first-class profile answers: work_auth_us (Yes/No) alongside its older spelling, and
    # needs_sponsorship with its third honest value, Future (the engine answers a "now or in the
    # future" question Yes for it and leaves a present-tense one to the person).
    answers_now = _profile_answers(prefs)
    if answers_now["work_auth_us"]:
        screening["work_auth_us"] = answers_now["work_auth_us"]
        screening.setdefault("work_authorized", answers_now["work_auth_us"])
    if answers_now["needs_sponsorship"]:
        screening["needs_sponsorship"] = answers_now["needs_sponsorship"]
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
        # The per-system fill recipes ride along so the extension (whose background worker only
        # knows this route) always fills from the freshest table; also at /api/autofill/recipes.
        "recipes": _recipes(),
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
    form is never filled with a free-text guess for a screening question.

    Also the extension's EVENT channel: its background worker (owned with the manifest, and
    not changed here) only knows this route, so a body of the form {event: {type: ...}} is
    routed to the matching endpoint instead of being saved as answers:
      {type: "learn", fields, screening, eeo_decline} -> POST /api/profile/autofill
      {type: "applied", url, page_url, record_id}     -> POST /api/autofill/applied
      {type: "linkedin_review", profile}              -> POST /api/profile/linkedin-review"""
    body = request.json or {}
    ev = body.get("event")
    if isinstance(ev, dict):
        kind = str(ev.get("type") or "")
        if kind == "learn":
            return _nostore(jsonify(_learn_from_corrections(ev)))
        if kind == "applied":
            return _nostore(jsonify(_mark_applied_from_page(ev)))
        if kind == "linkedin_review":
            # The extension read the person's OWN LinkedIn profile page (read-only, no
            # scrolling or clicking) and sends the extraction here, to the local app only.
            return _nostore(jsonify(_linkedin_review_run(ev.get("profile"), draft=False)))
        return jsonify({"ok": False, "error": f"Unknown event {kind!r}."}), 400
    return _nostore(jsonify({"ok": True, "prefs": _save_prefs(body)}))


@app.post("/api/profile/answers")
@_guard
def profile_answers_save():
    """The profile screen's first-class application answers: work_auth_us (Yes/No),
    needs_sponsorship (Yes/No/Future) and eeo_decline (bool). Same store as the extension
    popup's answers, so both surfaces show one truth."""
    b = request.json or {}
    patch: dict = {}
    if "work_auth_us" in b:
        v = str(b.get("work_auth_us") or "")
        if v not in _YN:
            return jsonify({"error": "work_auth_us must be Yes, No, or blank"}), 400
        patch["work_auth_us"] = v
    if "needs_sponsorship" in b:
        v = str(b.get("needs_sponsorship") or "")
        if v not in _SPONSOR:
            return jsonify({"error": "needs_sponsorship must be Yes, No, Future, or blank"}), 400
        patch["needs_sponsorship"] = v
    if "eeo_decline" in b:
        patch["eeo_decline"] = bool(b.get("eeo_decline"))
    prefs = _merge_prefs(patch) if patch else _load_prefs()
    return jsonify({"ok": True, "answers": _profile_answers(prefs)})


# --------------------------------------------------------------------------- Your answers
# The answer library (autoapply/answers.py) + the structured work-authorization profile
# (autoapply/workauth.py): fix an answer once, reused on every later form. Local SQLite in the
# data dir; nothing here goes to the model.
_ANSWERS_DB = _DATA / "answers.db"


def _answer_lib():
    """Open the answer library, seeding it once from the older saved answers."""
    from autoapply.answers import AnswerLibrary, seed_once
    lib = AnswerLibrary(_ANSWERS_DB)
    try:
        seed_once(lib, _load_prefs())
    except Exception:
        pass
    return lib


def _work_auth() -> dict:
    """The person's work-authorization profile (structured, or built from the older answers)."""
    from autoapply import workauth as _wa
    return _wa.load(_load_prefs())


def _answers_payload(query: str = "") -> dict:
    from autoapply import workauth as _wa
    lib = _answer_lib()
    try:
        rows = lib.list(query)
        total = lib.count()
    finally:
        lib.close()
    return {"ok": True, "work_auth": _work_auth(),
            "countries": [{"code": c, "name": _wa.COUNTRY_NAMES[c],
                           "statuses": [{"code": k, "label": v} for k, v in _wa.STATUSES[c]]}
                          for c in _wa.COUNTRIES],
            "implied": sorted(_wa.CITIZEN | _wa.PERMANENT), "dated": sorted(_wa.DATED),
            "eeo_decline": bool(_load_prefs().get("eeo_decline")),
            "answers": rows, "total": total}


@app.get("/api/answers")
@_guard
def answers_list():
    """Your answers: the work-authorization profile and the saved answer library (?q= filters)."""
    return _nostore(jsonify(_answers_payload(str(request.args.get("q") or ""))))


@app.post("/api/answers/workauth")
@_guard
def answers_workauth_save():
    """Save the work-authorization profile {work_auth:{countries:{US:{...}}, relocate}} and,
    optionally, eeo_decline. The flat Yes/No answers are derived from it."""
    from autoapply import workauth as _wa
    b = request.json or {}
    patch: dict = {}
    if isinstance(b.get("work_auth"), dict):
        patch["work_auth"] = _wa.clean(b["work_auth"])
    if "eeo_decline" in b:
        patch["eeo_decline"] = bool(b.get("eeo_decline"))
    prefs = _merge_prefs(patch) if patch else _load_prefs()
    return jsonify({"ok": True, "work_auth": _wa.load(prefs),
                    "eeo_decline": bool(prefs.get("eeo_decline")),
                    "answers": _profile_answers(prefs)})


@app.post("/api/answers")
@_guard
def answers_save():
    """Add or replace one saved answer {question, answer, key?}. With ``key`` it edits that
    entry (re-keyed if the question changed). Saved as the person's own."""
    b = request.json or {}
    q = str(b.get("question") or "").strip()
    a = str(b.get("answer") or "").strip()
    if not a:
        return jsonify({"error": "Write an answer first."}), 400
    lib = _answer_lib()
    try:
        key = str(b.get("key") or "").strip()
        row = lib.update(key, question=q or None, answer=a) if key else lib.save(q, a, "you")
    finally:
        lib.close()
    if row is None:
        return jsonify({"error": "That question can't be saved for reuse."}), 400
    return jsonify({"ok": True, "answer": row})


@app.post("/api/answers/delete")
@_guard
def answers_delete():
    """Delete one saved answer {key}."""
    key = str((request.json or {}).get("key") or "").strip()
    lib = _answer_lib()
    try:
        ok = lib.delete(key) if key else False
    finally:
        lib.close()
    return jsonify({"ok": ok})


def _basics_payload(prefs: dict | None = None) -> dict:
    """The "Answers every form asks" card: each answer as the card shows it, the countries it
    offers, and how many of the seven are answered (the five self-identification ones count as
    answered while the person declines them all)."""
    from autoapply import basics, countries
    p = prefs if prefs is not None else _load_prefs()
    lib = _answer_lib()
    try:
        d = basics.read(lib, eeo_decline=bool(p.get("eeo_decline")), salary_fallback=p.get("desired_salary") or "")
    finally:
        lib.close()
    return {"ok": True, **d, "eeo_decline": bool(p.get("eeo_decline")),
            "accept_required_terms": "always" if p.get("accept_required_terms") == "always" else "ask",
            "countries": list(countries.NAMES),
            "choices": {f: [{"code": c, "label": w} for c, w in ch] for f, ch in basics.CHOICES.items()}}


@app.get("/api/answers/basics")
@_guard
def answers_basics():
    """The answers every form asks (country of citizenship, desired salary, gender, Hispanic or
    Latino, race, veteran status, disability) as Your answers shows them. Local only."""
    return _nostore(jsonify(_basics_payload()))


@app.post("/api/answers/basics")
@_guard
def answers_basics_save():
    """Save the card {fields: {field: value}, eeo_decline?, accept_required_terms?}: only the fields the person changed,
    "" clearing one. Saved in the answer library under the canonical keys as the person's own,
    the salary in the prefs store too (desired_salary) so the browser extension fills the same
    number. Nothing here reaches a model, the broker, Telegram or a log."""
    from autoapply import basics
    b = request.json or {}
    fields = b.get("fields") if isinstance(b.get("fields"), dict) else {}
    pref_salary = str(_load_prefs().get("desired_salary") or "").strip()
    lib = _answer_lib()
    try:
        # A salary only the prefs store holds (the extension's) is the card's salary too: saved
        # in the library with the rest, unless the person cleared or changed it here.
        if "salary" not in fields and not basics.read(lib)["values"].get("salary") and basics.parse_salary(pref_salary):
            fields = dict(fields, salary=pref_salary)
        got = basics.write(lib, fields)
        now = basics.parse_salary(basics.read(lib)["values"].get("salary") or "")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        lib.close()
    patch: dict = {}
    if got["salary"] is not None:
        patch["desired_salary"] = got["salary"]
    elif now and str(now) != pref_salary:
        # The card's salary (the newest saved one) is what the extension fills too, even when
        # this save changed another field: the two never disagree after a Save.
        patch["desired_salary"] = str(now)
    if "eeo_decline" in b:
        patch["eeo_decline"] = bool(b.get("eeo_decline"))
    if b.get("accept_required_terms") in ("ask", "always"):     # terms a site requires to apply
        patch["accept_required_terms"] = b["accept_required_terms"]
    prefs = _merge_prefs(patch) if patch else _load_prefs()
    return jsonify(_basics_payload(prefs))


@app.post("/api/record/<int:rid>/screening")
@_guard
def record_screening_save(rid: int):
    """The person edited one screening answer on an application {index, answer}: saved on the
    record AND in the answer library, so every later form reuses it (unless the question is
    about this one company or role)."""
    b = request.json or {}
    try:
        idx = int(b.get("index"))
    except (TypeError, ValueError):
        return jsonify({"error": "Which answer?"}), 400
    ans = str(b.get("answer") or "").strip()[:4000]
    recs = _records()
    try:
        rec = recs.get(rid)
        if not rec:
            return jsonify({"error": "That application was not found."}), 404
        data = _record_data(rec)
        scr = [dict(x) for x in (data.get("screening") or []) if isinstance(x, dict)]
        if not 0 <= idx < len(scr):
            return jsonify({"error": "Which answer?"}), 400
        scr[idx]["answer"] = ans
        scr[idx]["edited"] = True
        recs.merge_data(rid, {"screening": scr})
        company = rec.get("company") or ""
    finally:
        recs.close()
    saved = None
    if ans:
        lib = _answer_lib()
        try:
            saved = lib.save(scr[idx].get("question") or "", ans, "you", company=company)
        finally:
            lib.close()
    return jsonify({"ok": True, "screening": scr, "reused": bool(saved)})


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


# ---- LinkedIn profile review (read-only; the person edits LinkedIn by hand) ----------
# The extension reads the person's OWN profile page while they have it open and posts the
# extraction here. We score it (linkedin_review.py), draft a headline + About from the saved
# profile with one model call, and keep the last review in a local JSON file so the Profile
# page can show it and "Re-score" without the browser. Nothing goes anywhere but this app.
_LI_REVIEW_PATH = _DATA / "linkedin_review.json"
_LI_LIST_KEYS = {"experience": ("title", "company", "dates", "description", "has_description"),
                 "education": ("school", "degree", "dates"),
                 "licenses": ("name", "issuer")}
_LI_STR_KEYS = ("url", "slug", "name", "headline", "about", "location", "current_title",
                "current_company")
_LI_BOOL_KEYS = ("photo", "banner", "open_to_work", "custom_url", "featured", "activity",
                 "activity_recent", "recommendations")
_LI_INT_KEYS = ("featured_count", "projects_count", "recommendations_count", "followers")


def _li_clean(raw) -> dict:
    """Keep only the fields the scorer reads, with caps, so an odd page can't bloat the
    local file or the UI."""
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {}
    for k in _LI_STR_KEYS:
        out[k] = str(raw.get(k) or "")[:3000]
    for k in _LI_BOOL_KEYS:
        out[k] = bool(raw.get(k))
    for k in _LI_INT_KEYS:
        try:
            out[k] = max(0, int(raw.get(k) or 0))
        except (TypeError, ValueError):
            out[k] = 0
    for k, keys in _LI_LIST_KEYS.items():
        rows = []
        for e in (raw.get(k) or [])[:40]:
            if not isinstance(e, dict):
                continue
            row = {kk: (bool(e.get(kk)) if kk == "has_description" else str(e.get(kk) or "")[:1500])
                   for kk in keys}
            if "has_description" in keys and row.get("description"):
                row["has_description"] = True
            rows.append(row)
        out[k] = rows
    out["skills"] = [str(x)[:80] for x in (raw.get("skills") or [])[:100] if str(x).strip()]
    return out


def _li_load() -> dict:
    import json as _json
    try:
        return _json.loads(_LI_REVIEW_PATH.read_text(encoding="utf-8")) if _LI_REVIEW_PATH.exists() else {}
    except Exception:
        return {}


def _li_save(doc: dict) -> None:
    import json as _json
    _LI_REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LI_REVIEW_PATH.write_text(_json.dumps(doc), encoding="utf-8")


def _li_target() -> tuple[str, list[str]]:
    """The role the review aims at (last tailored job, else the saved profile's most recent
    title) and the JD skill terms the app already extracted for it."""
    from linkedin_review import target_role_from
    from tailoring.keywords import skill_terms
    recs = _records()
    try:
        rows = recs.list(limit=1)
    finally:
        recs.close()
    saved = _memory().load("default") or {}
    profile = saved.get("profile") or saved.get("essentials") or {}
    role = target_role_from(profile, rows)
    terms: list[str] = []
    if rows:
        jd = str(_record_data(rows[0]).get("jd") or "")
        if jd:
            terms = skill_terms(jd)[:12]
    return role, terms


def _linkedin_review_run(extracted=None, draft: bool = False) -> dict:
    """Score the extraction (a fresh one from the extension, else the stored one), optionally
    draft the headline + About with one model call, store, and return the review."""
    from linkedin_review import draft_headline_about, score_profile
    stored = _li_load()
    if isinstance(extracted, dict) and extracted:
        x = _li_clean(extracted)
    else:
        x = stored.get("extracted") or {}
    if not x:
        return {"ok": True, "has_review": False, "app_url": "http://127.0.0.1:57000/"}
    role, terms = _li_target()
    review = score_profile(x, role, terms)
    doc = {"extracted": x, "review": review, "drafts": stored.get("drafts") or None,
           "at": time.time(), "draft_error": ""}
    if draft:
        profile = _saved_full_profile()
        loaded = bool((profile.get("identity") or {}).get("name") or profile.get("experience"))
        if not loaded:
            doc["draft_error"] = "Add your profile in SponsorJobs first, the drafts are written from it."
        else:
            try:
                doc["drafts"] = draft_headline_about(_make_llm(), profile, role, x)
            except Exception as exc:   # the score still stands without the drafts
                doc["draft_error"] = str(exc)[:300] or "The drafts could not be written right now."
    _li_save(doc)
    return {"ok": True, "has_review": True, "score": review["score"], "review": review,
            "drafts": doc["drafts"], "draft_error": doc["draft_error"], "at": doc["at"],
            "name": x.get("name") or "", "url": x.get("url") or "",
            "app_url": "http://127.0.0.1:57000/"}


@app.get("/api/profile/linkedin-review")
@_guard
def linkedin_review_get():
    """The last stored LinkedIn review for the Profile page. Nothing is computed here."""
    doc = _li_load()
    if not doc.get("review"):
        return _nostore(jsonify({"ok": True, "has_review": False}))
    x = doc.get("extracted") or {}
    return _nostore(jsonify({"ok": True, "has_review": True, "score": doc["review"]["score"],
                             "review": doc["review"], "drafts": doc.get("drafts"),
                             "draft_error": doc.get("draft_error") or "", "at": doc.get("at"),
                             "name": x.get("name") or "", "url": x.get("url") or ""}))


@app.post("/api/profile/linkedin-review")
@_guard
def linkedin_review_post():
    """Score a LinkedIn profile extraction. Body: {profile: {...}} with a fresh extraction
    (the extension's path, via the prefs event channel, lands here too), or {} to re-score
    the stored one. ``draft`` (default true from the app) also writes the headline + About
    with one model call; the extension's call never spends the model."""
    body = request.json or {}
    draft = body.get("draft", True) is not False
    return _nostore(jsonify(_linkedin_review_run(body.get("profile"), draft=draft)))


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
    # What the apply agent will read from: imap (app password), gmail (OAuth), fake (dev).
    from inbox.mailbox import mask_email
    box = _current_mailbox()
    method = getattr(box, "method", "") if box is not None else ""
    address = _cred("IMAP_EMAIL") if method == "imap" else getattr(box, "email", "")
    if box is not None and getattr(box, "name", "") == "alias":
        method, address = "alias", getattr(box, "alias", "")
    return jsonify({
        "libs": libs, "creds": creds, "token": token,
        "configured": (libs and token) or method in ("imap", "fake", "alias"),
        "creds_path": str(g._CREDS_PATH),
        "auto_verify": bool(_prefs().get("inbox_auto_verify")),
        "query": g.DEFAULT_QUERY,
        "method": method,
        "email": address if method == "alias" else mask_email(address or ""),
        "alias": _mail_alias() or None,
        "imap_host": _cred("IMAP_HOST") if method == "imap" else "",
    })


def _fake_mailbox_on() -> bool:
    """The in-memory mailbox, for tests and the shell e2e only; never in a shipped build."""
    return os.environ.get("RESUME_AGENT_FAKE_MAILBOX") == "1" and _under_test_or_dev()


def _mail_alias() -> str:
    """The person's SponsorJobs address (first.last@sponsorjobs.ai), cached locally once taken."""
    return str(_prefs().get("mail_alias") or "").strip()


def _application_email() -> str:
    """The e-mail the agent applies WITH: the SponsorJobs alias when the person took one and left
    "apply with it" on (docs/mail-alias.md: the site's codes and links reach the agent, recruiters'
    replies reach the real inbox), else the profile's own address."""
    alias = _mail_alias()
    if alias and _prefs().get("apply_with_alias", True):
        return alias
    return str((_saved_identity() or {}).get("email") or "").strip()


_RELAY = {"box": None, "alias": ""}      # one RelayMailbox per alias, so its cursor persists


def _relay_mailbox():
    """The alias read through the broker, or None when no alias is taken (or no account yet)."""
    alias = _mail_alias()
    if not alias:
        return None
    if _RELAY["box"] is not None and _RELAY["alias"] == alias:
        return _RELAY["box"]
    from inbox.relay import RelayMailbox, urllib_fetch
    tok = _account_token()
    if not tok:
        return None
    _RELAY["box"], _RELAY["alias"] = RelayMailbox(urllib_fetch(BROKER_URL, tok), alias), alias
    return _RELAY["box"]


def _current_mailbox():
    """The mailbox the apply agent reads (inbox/mailbox.pick_mailbox), or None."""
    from inbox.mailbox import pick_mailbox
    return pick_mailbox(_cred, fake=_fake_mailbox_on(), relay=_relay_mailbox())


# ---- The SponsorJobs address: taken, shown, released through the broker (inbox/relay.py). ----
def _relay_fetch():
    from inbox.relay import urllib_fetch
    tok = _account_token()
    return urllib_fetch(BROKER_URL, tok) if tok else None


@app.get("/api/inbox/alias")
@_guard
def inbox_alias_get():
    return jsonify({"alias": _mail_alias() or None, "apply_with_alias": bool(_prefs().get("apply_with_alias", True))})


@app.post("/api/inbox/alias")
@_guard
def inbox_alias_take():
    """Take the address (idempotent). The broker needs a signed-in account whose e-mail is
    verified: that is where the forwarded mail goes, and it is never typed here."""
    from inbox.relay import claim_alias
    fetch = _relay_fetch()
    if fetch is None:
        return jsonify({"ok": False, "error": "The SponsorJobs service is not reachable right now."}), 503
    name = str((_saved_identity() or {}).get("name") or "").strip()
    r = claim_alias(fetch, name)
    if not r.get("ok"):
        why = str(r.get("error") or "")
        msg = ("Sign in to SponsorJobs first (Settings > Account), so replies have somewhere to go."
               if "sign_in" in why else "Could not create the address. Try again in a moment.")
        return jsonify({"ok": False, "error": msg, "reason": why}), 400
    _set_prefs({"mail_alias": r["alias"], "apply_with_alias": True})
    _RELAY["box"] = None
    return jsonify({"ok": True, "alias": r["alias"], "forward_to": r.get("forward_to")})


@app.delete("/api/inbox/alias")
@_guard
def inbox_alias_release():
    from inbox.relay import release_alias
    fetch = _relay_fetch()
    if fetch is not None:
        release_alias(fetch)
    _set_prefs({"mail_alias": "", "apply_with_alias": True})
    _RELAY["box"] = None
    return jsonify({"ok": True})


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


_PROJ_LOCK = threading.RLock()


def _save_projects(projects: list, _from_build: bool = False) -> None:
    """Write the list. An AI build runs in its own thread and owns its build fields: when the
    file holds a NEWER build revision than the list being saved (a request that loaded before the
    build moved on), those fields are taken from the file, so a request never rolls a build back."""
    with _PROJ_LOCK:
        if not _from_build:
            disk = {x.get("id"): x for x in _load_projects()}
            for p in projects:
                d = disk.get(p.get("id"))
                if d and int((d.get("build") or {}).get("rev") or 0) > int((p.get("build") or {}).get("rev") or 0):
                    for k in _THREAD_KEYS:
                        if k in d:
                            p[k] = d[k]
        _PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PROJECTS_FILE.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"projects": projects}, indent=2), encoding="utf-8")
        tmp.replace(_PROJECTS_FILE)


def _project_public(p: dict) -> dict:
    return {"id": p.get("id"), "title": p.get("title", ""), "summary": p.get("summary", ""),
            "tech": p.get("tech", ""), "status": p.get("status", "draft"), "result": p.get("result"),
            "plan": p.get("plan"), "done": p.get("done") or [], "repo_url": p.get("repo_url") or "",
            "repo_private": bool(p.get("repo_private")), "repo": p.get("repo") or "",
            "skills": p.get("skills") or [], **_ai_public(p)}


def _ai_public(p: dict) -> dict:
    """The AI-built fields a card needs: stage, build progress, Learn it progress, defence."""
    if p.get("origin") != "ai":
        return {"origin": p.get("origin") or "manual"}
    b = p.get("build") or {}
    lr = p.get("learn") or {}
    last = (p.get("quiz") or {}).get("last") or {}
    return {"origin": "ai", "stage": _ai_stage(p), "target": p.get("target") or "local",
            "ai_credit": p.get("ai_credit", True) is not False, "has_files": bool(p.get("files")),
            "build": {k: b.get(k) for k in ("state", "done", "total", "error", "stage")},
            "learn": {"total": len(lr.get("steps") or []), "learned": len(set(lr.get("learned") or []))},
            "defense": {"score": last.get("score"), "total": last.get("total"), "pass_mark": last.get("pass_mark"),
                        "passed": bool(last.get("passed")), "tried": last.get("score") is not None}}


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


# ---- GitHub project ideas: one click, no typing. The target role comes from what the app
# already knows (the last tailored job, else the saved profile's latest title), the model drafts
# GitHub-shaped ideas aimed at it, and the app adds REAL example repos from GitHub's own search.
_IDEAS_FILE = _DATA / "project_ideas.json"


def _ideas_cache() -> dict:
    try:
        d = json.loads(_IDEAS_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _ideas_cache_put(key: str, entry: dict) -> None:
    cache = _ideas_cache()
    cache[key] = entry
    if len(cache) > 24:   # keep the two dozen most recent (role, search) sets
        for k in sorted(cache, key=lambda k: cache[k].get("at", 0))[:len(cache) - 24]:
            cache.pop(k, None)
    _IDEAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _IDEAS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    tmp.replace(_IDEAS_FILE)


def _profile_skill_text(profile: dict) -> str:
    """Everything the saved profile says, lower-cased, for 'does the profile show this skill'."""
    try:
        return json.dumps(profile or {}, ensure_ascii=False).lower()
    except Exception:
        return ""


def _has_term(text: str, term: str) -> bool:
    t = (term or "").strip().lower()
    return bool(t) and re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text) is not None


def _project_target(role_override: str = "") -> tuple[str, list[str], str]:
    """(role, JD skill terms, source) for project ideas. A role the person typed wins; the JD
    terms only apply when it is the same role as the last tailored job. source is 'job',
    'profile', 'typed', or '' when nothing is known yet."""
    derived, terms = _li_target()
    recs = _records()
    try:
        has_job = bool(recs.list(limit=1))
    finally:
        recs.close()
    typed = re.sub(r"\s+", " ", role_override or "").strip()[:120]
    if typed:
        same = typed.lower() == (derived or "").lower()
        return typed, (terms if same else []), ("job" if same and has_job else "typed")
    if derived:
        return derived, terms, ("job" if has_job else "profile")
    return "", [], ""


def _clean_idea(raw: dict, terms: list[str], prof_text: str) -> dict | None:
    from ui.github_publish import slugify
    if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
        return None
    title = str(raw["title"]).strip()[:80]
    skills, seen = [], set()
    for k in raw.get("skills") or []:
        k = str(k).strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            skills.append(k[:40])
    skills = skills[:5]
    terms_l = {t.lower() for t in terms}
    gaps = [k for k in skills if k.lower() in terms_l and not _has_term(prof_text, k)]
    # No build-time size: AI build time is not human build time, so the label misled. Ideas
    # cached before this still carry one; the UI ignores it and nothing new is written.
    return {"title": title, "pitch": str(raw.get("pitch") or "").strip()[:160],
            "skills": skills, "gaps": gaps, "tech": str(raw.get("tech") or "").strip()[:160],
            "repo": slugify(raw.get("repo") or title),
            "search": str(raw.get("search") or title).strip()[:120], "examples": []}


def _add_examples(ideas: list[dict]) -> bool:
    """Fill each idea's examples from GitHub search, in parallel. Returns whether search
    answered at all (False means GitHub was unreachable, and every idea shows none)."""
    from concurrent.futures import ThreadPoolExecutor
    from ui import github_publish as G
    token = _cred("GITHUB_TOKEN")
    if not ideas:
        return True
    try:
        with ThreadPoolExecutor(max_workers=min(6, len(ideas))) as ex:
            found = list(ex.map(lambda i: G.search_examples(i.get("search") or i["title"], token), ideas))
    except Exception:
        found = [[] for _ in ideas]
    for idea, ex_list in zip(ideas, found):
        idea["examples"] = ex_list or []
    return any(found)


# Ideas cached before example repos carried a ``language`` show a hollow ring on every repo. Such
# a set is stale: refetch the examples for the ideas that need it and keep the rest. GitHub allows
# 10 unauthenticated searches a minute and one idea can cost two (the short-phrase retry), so
# without a token a pass heals at most 4 ideas, and passes are at least a minute apart; the rest
# heal on a later open. A search that comes back empty keeps the old examples rather than none.
_HEAL_GAP = 60
_HEAL_MAX_ANON = 4


def _stale_examples(idea: dict) -> bool:
    return any(isinstance(e, dict) and "language" not in e for e in idea.get("examples") or [])


def _heal_examples(key: str, hit: dict) -> list[dict]:
    ideas = hit.get("ideas") or []
    stale = [i for i in ideas if isinstance(i, dict) and _stale_examples(i)]
    if not stale or time.time() - float(hit.get("healed_at") or 0) < _HEAL_GAP:
        return ideas
    from concurrent.futures import ThreadPoolExecutor
    from ui import github_publish as G
    token = _cred("GITHUB_TOKEN")
    batch = stale if token else stale[:_HEAL_MAX_ANON]
    try:
        with ThreadPoolExecutor(max_workers=min(6, len(batch))) as ex:
            found = list(ex.map(lambda i: G.search_examples(i.get("search") or i.get("title") or "", token), batch))
    except Exception:
        found = [[] for _ in batch]
    for idea, ex_list in zip(batch, found):
        if ex_list:
            idea["examples"] = ex_list
    _ideas_cache_put(key, {**hit, "ideas": ideas, "healed_at": time.time()})
    return ideas


# Every new batch of ideas is one model call, so they are capped per local day (cache hits are
# free). The count lives in a small local file; a new local day starts from zero.
_IDEAS_USAGE_FILE = _DATA / "project_ideas_usage.json"
_SAVED_IDEAS_FILE = _DATA / "saved_ideas.json"
_IDEAS_BATCH = 6


def _ideas_day() -> str:
    """Today's local date (a seam the tests pin)."""
    from datetime import date
    return date.today().isoformat()


def _ideas_cap() -> int:
    try:
        return max(1, int(os.environ.get("PROJECT_IDEAS_DAILY_CAP") or 10))
    except ValueError:
        return 10


def _ideas_used() -> int:
    try:
        d = json.loads(_IDEAS_USAGE_FILE.read_text(encoding="utf-8"))
        return int(d.get("count") or 0) if d.get("day") == _ideas_day() else 0
    except Exception:
        return 0


def _ideas_count_batch() -> None:
    used = _ideas_used() + 1
    _IDEAS_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _IDEAS_USAGE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"day": _ideas_day(), "count": used}), encoding="utf-8")
    tmp.replace(_IDEAS_USAGE_FILE)


def _ideas_cap_state() -> dict:
    cap, used = _ideas_cap(), _ideas_used()
    return {"limit": cap, "used": used, "left": max(0, cap - used)}


def _ideas_key(role: str, query: str) -> str:
    """Cache key for a (role, search) set. No search keeps the original per-role key."""
    k = role.lower() or "_"
    return f"{k}|q:{query.lower()}" if query else k


@app.post("/api/projects/ideas")
@_guard
def projects_ideas():
    """GitHub project ideas for the target role, with no typing needed. Body (all optional):
    {role: a different target, refresh: true to skip the cache, query: a topic to search ideas
    for (still aimed at the role), exclude: titles already shown, which asks for ANOTHER batch to
    append}. Ideas are cached per (role, query), so reopening is instant and costs nothing. New
    batches count against a local daily cap; a cache hit never does."""
    b = request.json or {}
    role, terms, source = _project_target(str(b.get("role") or ""))
    query = re.sub(r"\s+", " ", str(b.get("query") or "")).strip()[:80]
    exclude = [str(t).strip()[:80] for t in (b.get("exclude") or []) if str(t).strip()][:60] \
        if isinstance(b.get("exclude"), list) else []
    more = bool(exclude)
    key = _ideas_key(role, query)
    gh = {"connected": bool(_cred("GITHUB_TOKEN")), "login": _cred("GITHUB_LOGIN") or ""}
    base = {"role": role, "source": source, "query": query, "github": gh}
    hit = _ideas_cache().get(key)
    if not b.get("refresh") and not more and hit and hit.get("ideas"):
        ideas = _heal_examples(key, hit)
        return jsonify({**base, "ideas": ideas, "batches": hit.get("batches") or [len(ideas)],
                        "cached": True, "cap": _ideas_cap_state()})
    cap = _ideas_cap_state()
    if cap["left"] <= 0:
        return jsonify({**base, "error": f"You've reached today's {cap['limit']} idea batches. More tomorrow.",
                        "code": "ideas_cap", "cap": cap}), 429
    saved = _memory().load("default") or {}
    profile = saved.get("profile") or saved.get("essentials") or {}
    prof_text = _profile_skill_text(profile)
    missing = [t for t in terms if not _has_term(prof_text, t)]
    # Already shown: what the client sent plus, when appending, what this set already holds.
    from ui.github_publish import slugify
    prior = (hit.get("ideas") or []) if (more and hit) else []
    seen_titles = {t.lower() for t in exclude} | {str(i.get("title") or "").lower() for i in prior}
    seen_repos = {slugify(t) for t in exclude} | {str(i.get("repo") or "") for i in prior}
    raw = _make_llm().suggest_github_ideas(profile, role, terms, missing, _IDEAS_BATCH,
                                           query=query, exclude=list(dict.fromkeys(
                                               exclude + [str(i.get("title") or "") for i in prior if i.get("title")])))
    _ideas_count_batch()
    ideas = []
    for x in raw.get("ideas") or []:
        i = _clean_idea(x, terms, prof_text)
        if not i or i["title"].lower() in seen_titles or i["repo"] in seen_repos:
            continue
        seen_titles.add(i["title"].lower())
        seen_repos.add(i["repo"])
        ideas.append(i)
        if len(ideas) >= _IDEAS_BATCH:
            break
    _add_examples(ideas)
    if ideas:
        if more and prior:
            batches = list(hit.get("batches") or [len(prior)]) + [len(ideas)]
            _ideas_cache_put(key, {**hit, "ideas": prior + ideas, "batches": batches, "at": time.time()})
        else:
            _ideas_cache_put(key, {"role": role, "query": query, "ideas": ideas, "batches": [len(ideas)],
                                   "at": time.time()})
    return jsonify({**base, "ideas": ideas, "batches": [len(ideas)], "append": more, "cached": False,
                    "cap": _ideas_cap_state()})


# Saved ideas: a small local bookmark list, kept across searches, refreshes and restarts.
_SAVED_IDEAS_MAX = 60


def _saved_ideas() -> list[dict]:
    try:
        d = json.loads(_SAVED_IDEAS_FILE.read_text(encoding="utf-8"))
        return [i for i in d if isinstance(i, dict) and i.get("title")] if isinstance(d, list) else []
    except Exception:
        return []


def _save_saved_ideas(items: list[dict]) -> None:
    _SAVED_IDEAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SAVED_IDEAS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items[:_SAVED_IDEAS_MAX]), encoding="utf-8")
    tmp.replace(_SAVED_IDEAS_FILE)


def _saved_idea_id(idea: dict) -> str:
    from ui.github_publish import slugify
    return slugify(idea.get("repo") or idea.get("title") or "")


@app.get("/api/projects/ideas/saved")
@_guard
def projects_ideas_saved():
    return jsonify({"saved": _saved_ideas()})


@app.post("/api/projects/ideas/saved")
@_guard
def projects_ideas_save():
    """Bookmark an idea: {idea}. Saving the same idea again replaces it (newest first)."""
    idea = (request.json or {}).get("idea") or {}
    if not isinstance(idea, dict) or not str(idea.get("title") or "").strip():
        return jsonify({"error": "Pick an idea to save."}), 400
    clean = {"title": str(idea["title"]).strip()[:80], "pitch": str(idea.get("pitch") or "").strip()[:160],
             "skills": [str(k).strip()[:40] for k in (idea.get("skills") or []) if str(k).strip()][:5],
             "gaps": [str(k).strip()[:40] for k in (idea.get("gaps") or []) if str(k).strip()][:5],
             "tech": str(idea.get("tech") or "").strip()[:160],
             "search": str(idea.get("search") or "").strip()[:120],
             "examples": [e for e in (idea.get("examples") or [])
                          if isinstance(e, dict) and str(e.get("url") or "").startswith("https://github.com/")][:3],
             "saved_at": time.time()}
    clean["repo"] = _saved_idea_id(idea)
    items = [i for i in _saved_ideas() if _saved_idea_id(i) != clean["repo"]]
    _save_saved_ideas([clean] + items)
    return jsonify({"saved": _saved_ideas()})


@app.delete("/api/projects/ideas/saved")
@_guard
def projects_ideas_unsave():
    """Remove a bookmark by its repo name (or title): {repo} in the body or ?repo= in the URL."""
    b = request.get_json(silent=True) or {}
    ref = {"repo": str(b.get("repo") or request.args.get("repo") or "").strip(),
           "title": str(b.get("title") or request.args.get("title") or "").strip()}
    if not (ref["repo"] or ref["title"]):
        return jsonify({"error": "Pick an idea to remove."}), 400
    rid = _saved_idea_id(ref)
    _save_saved_ideas([i for i in _saved_ideas() if _saved_idea_id(i) != rid])
    return jsonify({"saved": _saved_ideas()})


@app.post("/api/projects/start")
@_guard
def projects_start():
    """Start an idea. By default the AI BUILDS it (see _start_ai_build): one package, a small
    working project generated in the background, pushed to a PRIVATE repo when GitHub is
    connected, else kept here ({local: true}). {build: false} keeps the older hand-built start: a
    draft plus, with GitHub, a private repo with a starter README and the guide's milestones as
    issues, no code. Either way the defend gate is untouched: nothing goes public or onto the
    resume until the project is verified."""
    import uuid
    from ui.github_publish import slugify
    body = request.json or {}
    idea = body.get("idea") or {}
    title = str(idea.get("title") or "").strip()[:80]
    if not title:
        return jsonify({"error": "Pick an idea to start."}), 400
    if body.get("build", True) is not False:
        # The AI builds it (one package). {local: true} builds without GitHub: files stay here.
        p = _start_ai_build(idea, bool(body.get("local")))
        p = next((x for x in _load_projects() if x.get("id") == p["id"]), p)
        return jsonify({"project": _project_public(p),
                        "github": {"connected": bool(_cred("GITHUB_TOKEN")), "target": p.get("target")}})
    skills = [str(k).strip()[:40] for k in (idea.get("skills") or []) if str(k).strip()][:5]
    p = {"id": uuid.uuid4().hex[:12], "title": title,
         "summary": str(idea.get("pitch") or idea.get("summary") or "").strip()[:280],
         "tech": str(idea.get("tech") or "").strip()[:160], "skills": skills,
         "repo": slugify(idea.get("repo") or title),
         "status": "draft"}
    gh = {"connected": False, "ok": False, "url": "", "error": "", "issues": 0}
    token = _cred("GITHUB_TOKEN")
    if token:
        gh["connected"] = True
        from ui import github_publish as G
        try:
            p["plan"] = _make_llm().generate_build_plan(p)   # the issues ARE the build guide
            p["done"] = []
        except Exception:
            p.pop("plan", None)
        res = G.create_starter_repo(token, p)
        if res.get("ok"):
            p["repo_url"], p["repo_full_name"] = res.get("url", ""), res.get("full_name", "")
            p["repo_private"] = True
            gh.update(ok=True, url=p["repo_url"], issues=int(res.get("issues") or 0))
        else:
            gh["error"] = res.get("error") or "GitHub wouldn't create the repo."
    projects = _load_projects()
    projects.append(p)
    _save_projects(projects)
    return jsonify({"project": _project_public(p), "github": gh})


@app.delete("/api/projects/<pid>")
@_guard
def projects_delete(pid):
    with _PROJ_LOCK:
        _save_projects([p for p in _load_projects() if p.get("id") != pid], _from_build=True)
    import shutil
    shutil.rmtree(_build_dir(pid), ignore_errors=True)
    return jsonify({"ok": True})


@app.post("/api/projects/<pid>/test")
@_guard
def projects_test(pid):
    """Generate the written defence test for a project the person built by hand, grounded in
    what they built. An AI-built project is defended by its quiz instead (/quiz)."""
    projects = _load_projects()
    p = next((x for x in projects if x.get("id") == pid), None)
    if not p:
        return jsonify({"error": "Project not found."}), 404
    if p.get("origin") == "ai":
        return _quiz_only()
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
    """Grade the written defence of a hand-built project. Passing marks the project verified,
    the CV-inclusion gate. An AI-built project is graded by its quiz (/quiz/submit)."""
    projects = _load_projects()
    p = next((x for x in projects if x.get("id") == pid), None)
    if not p:
        return jsonify({"error": "Project not found."}), 404
    if p.get("origin") == "ai":
        return _quiz_only()
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
    from ui import github_publish as G
    if p.get("origin") == "ai":
        prep = _publish_ai_prepare(token, p)
        if prep:
            return prep
    # A project started on GitHub already has a private repo: publishing flips it to public.
    res = G.publish_project(token, p)
    if not res.get("ok"):
        return jsonify({"error": res.get("error", "Couldn't publish to GitHub.")}), 400
    p["repo_url"] = res.get("url", "") or p.get("repo_url", "")
    if res.get("full_name"):
        p["repo_full_name"] = res["full_name"]
    p["repo_private"] = False
    _save_projects(projects)
    # Added to the resume while the repo was still private? Give that entry its link now.
    mem = _memory()
    saved = mem.load("default") or {}
    prof = saved.get("profile") or {}
    entry = next((pr for pr in prof.get("projects") or [] if pr.get("org") == p.get("title")), None)
    if entry is not None and not entry.get("link") and p["repo_url"]:
        entry["link"] = p["repo_url"]
        mem.save("default", prof, saved.get("essentials") or {}, saved.get("history") or [])
    return jsonify({"ok": True, "url": p["repo_url"]})


def _publish_ai_prepare(token: str, p: dict):
    """Before an AI-built project goes public: a project built without GitHub gets its private
    repo and its one commit now; and the README on GitHub is brought in line with the
    "Built with an AI pair-programmer" toggle, in this same step. Returns an error response, or
    None to carry on (``p`` is updated in place)."""
    from ui import github_publish as G
    from ui import project_builder as PB
    files = PB.load_files(_build_dir(p["id"]))
    if not files:
        return jsonify({"error": "This project has no files to publish yet."}), 400
    want = p.get("ai_credit", True) is not False
    files = [dict(f, content=PB.with_ai_line(f["content"], want)) if f["path"] == "README.md" else f for f in files]
    if not p.get("repo_full_name"):
        made = G.create_private_repo(token, p)
        if not made.get("ok"):
            return jsonify({"error": made.get("error") or "GitHub wouldn't create the repo."}), 400
        fields = {"repo_url": made.get("url", ""), "repo_full_name": made["full_name"], "repo_private": True}
        p.update(fields)
        _update_project(p["id"], lambda x: x.update(fields))   # kept even if the push below fails
    if "readme_credit_pushed" not in p:   # the files never reached GitHub: one commit now
        pushed = G.push_files(token, p["repo_full_name"], files, "Initial version, built with an AI pair-programmer")
        if not pushed.get("ok"):
            return jsonify({"error": pushed.get("error") or "GitHub wouldn't take the files."}), 400
        p["readme_credit_pushed"] = want
    elif bool(p.get("readme_credit_pushed")) != want:
        readme = next((f["content"] for f in files if f["path"] == "README.md"), "")
        if not G.update_file(token, p["repo_full_name"], "README.md", readme,
                             "Update README" if want else "README: drop the AI pair-programmer line"):
            return jsonify({"error": "GitHub wouldn't update the README. Try again."}), 400
        p["readme_credit_pushed"] = want
    return None


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
                         # a private repo would 404 for a recruiter, so only a public one is linked
                         "link": "" if p.get("repo_private") else (p.get("repo_url") or ""),
                         "bullets": bullets, "suggested": False})
    mem.save("default", prof, saved.get("essentials") or {}, saved.get("history") or [])
    return jsonify({"ok": True})


# ---- AI-built projects (approved 2026-10-13): the AI builds a SMALL working project, the person
# learns it ("Learn it", a walkthrough of the real files), then defends it in a 20-question quiz
# ("Defend it"). Only a defended project is Verified, and only Verified goes public or
# onto the resume (the gate above, enforced server-side in /publish and /tocv).
#
# Packages: a build is one "package" (the same unit as a tailoring run), counted through the
# broker BEFORE any model call by `_count_package`, which also raises the upgrade prompt when none
# are left. The broker has NO refund route (backend/broker.py: /llm/package only decrements), and a
# client-callable refund would hand out free packages, so a failed build is made good locally
# instead: the project keeps the package it paid for and "Try again" rebuilds without counting
# another, up to _BUILD_ATTEMPTS generations per package. A push that fails after generation keeps
# the files, and retrying only pushes (no model call, no package).
_PROJECT_BUILDS = _DATA / "project_builds"
_BUILD_ATTEMPTS = 3
_BUILD_ACTIVE = ("queued", "planning", "writing", "pushing", "preparing")
_BUILD_INLINE = False          # tests run the build in the request thread
_THREAD_KEYS = ("build", "files", "learn", "repo_url", "repo_full_name", "repo_private",
                "readme_credit_pushed")


def _build_dir(pid: str) -> Path:
    return _PROJECT_BUILDS / re.sub(r"[^a-z0-9]", "", str(pid).lower())[:24]


def _update_project(pid: str, fn) -> dict | None:
    """Load, change, and save one project under the projects lock. A change to the build bumps
    its revision so a request that loaded the list earlier never writes an older build back."""
    with _PROJ_LOCK:
        projects = _load_projects()
        p = next((x for x in projects if x.get("id") == pid), None)
        if p is None:
            return None
        fn(p)
        if isinstance(p.get("build"), dict):
            p["build"]["rev"] = int(p["build"].get("rev") or 0) + 1
        _save_projects(projects, _from_build=True)
        return p


def _personal_markers() -> list[str]:
    """The person's own identifying details, so a generated file that somehow carries one is
    refused. The model never sees them; this is the belt to that braces."""
    saved = _memory().load("default") or {}
    out = []
    for src in (saved.get("essentials") or {}, saved.get("profile") or {}):
        for d in (src, src.get("identity") or {}, src.get("contact") or {}):
            if not isinstance(d, dict):
                continue
            for k in ("name", "full_name", "email", "phone"):
                v = str(d.get(k) or "").strip()
                if (k in ("name", "full_name") and " " in v and len(v) >= 6) or (k == "email" and "@" in v) \
                        or (k == "phone" and sum(ch.isdigit() for ch in v) >= 7):
                    out.append(v)
    return list(dict.fromkeys(out))


def _ai_stage(p: dict) -> str:
    """Where an AI-built project is: building, failed, learn, defend, or verified."""
    if p.get("status") == "verified":
        return "verified"
    b = p.get("build") or {}
    if b.get("state") in _BUILD_ACTIVE:
        return "building"
    if b.get("state") == "failed":
        return "failed"
    lr = p.get("learn") or {}
    steps = lr.get("steps") or []
    if not steps or len(set(lr.get("learned") or [])) < len(steps):
        return "learn"
    return "defend"


def _learn_done(p: dict) -> bool:
    lr = p.get("learn") or {}
    steps = lr.get("steps") or []
    return bool(steps) and len(set(lr.get("learned") or [])) >= len(steps)


def _run_build(pid: str, llm, generate: bool = True) -> None:
    """The build job: generate and validate (unless the files are already here), save them
    locally, push them to the private repo when the project targets GitHub, prepare Learn it."""
    from ui import github_publish as G
    from ui import project_builder as PB
    p = next((x for x in _load_projects() if x.get("id") == pid), None)
    if not p:
        return

    def progress(**kw):
        _update_project(pid, lambda x: x.setdefault("build", {}).update(kw))

    meter = PB.Meter()
    files = PB.load_files(_build_dir(pid)) if not generate else []
    try:
        if generate or not files:
            res = PB.generate(llm, p, progress, personal=_personal_markers(), meter=meter)
            files = res["files"]
            if not p.get("ai_credit", True):
                files = [dict(f, content=PB.with_ai_line(f["content"], False)) if f["path"] == "README.md" else f
                         for f in files]
            PB.save_files(_build_dir(pid), files)
            # New files: any quiz made from older ones no longer matches them.
            _update_project(pid, lambda x: (x.pop("quiz", None),
                                            x.update(files=True, run=res["plan"].get("run", ""),
                                                     test_cmd=res["plan"].get("test", ""))))
    except PB.BuildError as exc:
        print(f"[projects] build {pid} failed at {exc.stage}: {exc}; tokens {meter.as_dict()}")
        progress(state="failed", error=str(exc), stage=exc.stage, tokens=meter.as_dict(), finished=time.time())
        return
    except Exception as exc:   # the model or the network: say so, keep the package for a retry
        traceback.print_exc()
        reason = _classify_outage(exc)
        msg = ("Your AI allowance ran out partway through. Top it up, then build again."
               if reason in ("upgrade_required", "no_credit") else
               "The AI could not finish this build." if not reason else
               "SponsorJobs could not reach the AI. Check your connection.")
        progress(state="failed", error=msg, stage="write", tokens=meter.as_dict(), finished=time.time())
        return
    if p.get("target") == "github":
        token = _cred("GITHUB_TOKEN")
        if not token:
            progress(state="failed", stage="push", error="Connect GitHub in Settings first. "
                     "Your files are saved here.", tokens=meter.as_dict(), finished=time.time())
            return
        progress(state="pushing", done=len(files), total=len(files))
        cur = next((x for x in _load_projects() if x.get("id") == pid), {}) or {}
        full = cur.get("repo_full_name") or ""
        if not full:
            made = G.create_private_repo(token, cur)
            if not made.get("ok"):
                progress(state="failed", stage="push", error=made.get("error") or "GitHub wouldn't create the repo.",
                         tokens=meter.as_dict(), finished=time.time())
                return
            full = made["full_name"]
            _update_project(pid, lambda x: x.update(repo_url=made.get("url", ""), repo_full_name=full,
                                                    repo_private=True))
        pushed = G.push_files(token, full, files, "Initial version, built with an AI pair-programmer")
        if not pushed.get("ok"):
            progress(state="failed", stage="push", error=pushed.get("error") or "GitHub wouldn't take the files.",
                     tokens=meter.as_dict(), finished=time.time())
            return
        credit = any(f["path"] == "README.md" and PB.AI_LINE in f["content"] for f in files)
        _update_project(pid, lambda x: x.update(readme_credit_pushed=credit))
    # Learn it is generated once, here, so it opens instantly. A miss is fine: /learn retries.
    if not ((next((x for x in _load_projects() if x.get("id") == pid), {}) or {}).get("learn") or {}).get("steps"):
        progress(state="preparing")
        try:
            raw = meter.call(llm, llm.generate_learn_steps, PB._idea(p), files,
                             prompt_chars=sum(len(f["content"]) for f in files) + 2000)
            steps = PB.learn_steps((raw or {}).get("steps"), files)
            if len(steps) >= 3:
                _update_project(pid, lambda x: x.update(learn={"steps": steps, "learned": [], "answers": {}}))
        except Exception:
            traceback.print_exc()
    # The Defend it quiz, also once, here, inside the same token budget. A miss is fine: /quiz
    # makes it on first open from what is left of this build's budget.
    cur = next((x for x in _load_projects() if x.get("id") == pid), {}) or {}
    if not (cur.get("quiz") or {}).get("questions") and (cur.get("learn") or {}).get("steps"):
        _make_project_quiz(pid, llm, files, cur["learn"]["steps"], meter)
    tokens = meter.as_dict()
    print(f"[projects] build {pid} ready: {len(files)} files, tokens in={tokens['input']} "
          f"out={tokens['output']} calls={tokens['calls']}{' (estimated)' if tokens['estimated'] else ''}")
    progress(state="ready", error="", stage="", done=len(files), total=len(files), tokens=tokens,
             finished=time.time())


def _spawn_build(pid: str, llm, generate: bool = True) -> None:
    if _BUILD_INLINE:
        _run_build(pid, llm, generate)
        return
    threading.Thread(target=_run_build, args=(pid, llm, generate), daemon=True,
                     name=f"project-build-{pid}").start()


def _start_ai_build(idea: dict, local: bool) -> dict:
    """Create the AI-built project, count its package, and start the build job."""
    from ui.github_publish import slugify
    title = str(idea.get("title") or "").strip()[:80]
    target = "local" if (local or not _cred("GITHUB_TOKEN")) else "github"
    llm = _count_package(_make_llm())     # one build = one package; raises the upgrade prompt
    p = {"id": uuid.uuid4().hex[:12], "title": title,
         "summary": str(idea.get("pitch") or idea.get("summary") or "").strip()[:280],
         "tech": str(idea.get("tech") or "").strip()[:160],
         "skills": [str(k).strip()[:40] for k in (idea.get("skills") or []) if str(k).strip()][:5],
         "repo": slugify(idea.get("repo") or title), "status": "draft", "origin": "ai",
         "target": target, "ai_credit": True,
         "package": {"counted_at": time.time(), "generations": 1},
         "build": {"state": "queued", "done": 0, "total": 0, "error": "", "started": time.time(), "rev": 1}}
    with _PROJ_LOCK:
        projects = _load_projects()
        projects.append(p)
        _save_projects(projects)
    _spawn_build(p["id"], llm)
    return p


def _project_or_404(pid: str):
    p = next((x for x in _load_projects() if x.get("id") == pid), None)
    return p, (None if p else (jsonify({"error": "Project not found."}), 404))


@app.get("/api/projects/<pid>/build")
@_guard
def projects_build_status(pid):
    """The build's progress, for the card to poll: state, done/total, error, token counts."""
    p, err = _project_or_404(pid)
    if err:
        return err
    return jsonify({"project": _project_public(p)})


@app.post("/api/projects/<pid>/build")
@_guard
def projects_build_retry(pid):
    """Try a failed build again. Files already generated: push only, no model call. Otherwise
    rebuild on the package this project already paid for (no second charge), up to
    _BUILD_ATTEMPTS generations; past that, a new build counts a new package."""
    p, err = _project_or_404(pid)
    if err:
        return err
    b = p.get("build") or {}
    if p.get("origin") != "ai":
        return jsonify({"error": "Only an AI-built project can be rebuilt."}), 400
    if b.get("state") in _BUILD_ACTIVE:
        return jsonify({"project": _project_public(p)})
    if b.get("state") != "failed":
        return jsonify({"error": "This project is already built."}), 400
    from ui import project_builder as PB
    have_files = bool(p.get("files")) and bool(PB.load_files(_build_dir(pid)))
    gens = int((p.get("package") or {}).get("generations") or 1)
    if have_files:
        llm, generate = _make_llm(), False
    elif gens < _BUILD_ATTEMPTS:
        llm, generate = _make_llm(), True
    else:
        llm, generate = _count_package(_make_llm()), True
        gens = 0

    def reset(x):
        x["build"] = {**(x.get("build") or {}), "state": "queued", "error": "", "stage": "", "done": 0,
                      "total": 0, "started": time.time()}
        if generate:
            x.setdefault("package", {})["generations"] = gens + 1
    p = _update_project(pid, reset)
    _spawn_build(pid, llm, generate)
    return jsonify({"project": _project_public(p)})


@app.get("/api/projects/<pid>/download.zip")
@_guard
def projects_download(pid):
    """The generated project as a zip, for a build without GitHub (or a local copy of one)."""
    import io
    from ui import project_builder as PB
    p, err = _project_or_404(pid)
    if err:
        return err
    files = PB.load_files(_build_dir(pid))
    if not files:
        return jsonify({"error": "This project has no files yet."}), 404
    slug = p.get("repo") or "project"
    return send_file(io.BytesIO(PB.zip_bytes(files, slug)), mimetype="application/zip", as_attachment=True,
                     download_name=f"{slug}.zip", max_age=0)


@app.post("/api/projects/<pid>/settings")
@_guard
def projects_settings(pid):
    """Per-project settings: {ai_credit: bool}, the README's "Built with an AI pair-programmer"
    line (on by default). The local copy changes now; a GitHub repo's README is updated in the
    same step that makes it public."""
    from ui import project_builder as PB
    b = request.json or {}
    if "ai_credit" not in b:
        return jsonify({"error": "Nothing to change."}), 400
    on = bool(b.get("ai_credit"))
    p = _update_project(pid, lambda x: x.update(ai_credit=on))
    if not p:
        return jsonify({"error": "Project not found."}), 404
    files = PB.load_files(_build_dir(pid))
    if files:
        PB.save_files(_build_dir(pid), [dict(f, content=PB.with_ai_line(f["content"], on))
                                        if f["path"] == "README.md" else f for f in files])
    return jsonify({"project": _project_public(p)})


@app.post("/api/projects/<pid>/learn")
@_guard
def projects_learn(pid):
    """The Learn it walkthrough: 5 to 8 steps from the real files, generated once and cached with
    the project. Answer keys stay on the server."""
    from ui import project_builder as PB
    p, err = _project_or_404(pid)
    if err:
        return err
    if p.get("origin") != "ai" or _ai_stage(p) in ("building", "failed"):
        return jsonify({"error": "Learn it opens once the project is built."}), 400
    lr = p.get("learn") or {}
    if not lr.get("steps"):
        files = PB.load_files(_build_dir(pid))
        if not files:
            return jsonify({"error": "This project has no files yet."}), 400
        raw = _make_llm().generate_learn_steps(PB._idea(p), files)
        steps = PB.learn_steps((raw or {}).get("steps"), files)
        if len(steps) < 3:
            return jsonify({"error": "Couldn't prepare the walkthrough right now. Try again."}), 502
        lr = {"steps": steps, "learned": [], "answers": {}}
        p = _update_project(pid, lambda x: x.update(learn=lr))
    # Checks are always multiple choice (owner, 2026-10-13). A walkthrough cached before that rule may
    # still hold typed one-line checks: convert them once with one small call and cache the result.
    typed = [i for i, s in enumerate(lr["steps"]) if (s.get("check") or {}).get("kind") == "line"]
    if typed:
        try:
            items = [{"i": i, "q": lr["steps"][i]["check"].get("q", ""), "accept": lr["steps"][i]["check"].get("accept") or [],
                      "explain": lr["steps"][i]["check"].get("explain", ""), "code": lr["steps"][i].get("code", "")[:1500]}
                     for i in typed]
            got = {c.get("i"): c for c in (_make_llm().choice_checks_for(items) or {}).get("checks") or []}
            fixed = {}
            for i in typed:
                c = PB._clean_check(dict(got.get(i) or {}, kind="choice"))
                if c and c.get("kind") == "choice":
                    fixed[i] = c
            if fixed:
                def apply(x):
                    for i, c in fixed.items():
                        x["learn"]["steps"][i]["check"] = c
                        (x["learn"].get("answers") or {}).pop(str(i), None)
                p = _update_project(pid, apply)
                lr = p.get("learn") or lr
        except Exception as exc:                       # noqa: BLE001 - a typed check still works
            print(f"[projects] learn {pid}: could not convert typed checks: {exc}")
    return jsonify({"steps": [PB.public_step(s) for s in lr["steps"]], "learned": sorted(set(lr.get("learned") or [])),
                    "project": _project_public(p)})


@app.post("/api/projects/<pid>/learn/<int:idx>")
@_guard
def projects_learn_answer(pid, idx):
    """Check one step's question: {answer} (an option index, or one line of text). A right
    answer marks the step learned; either way the explanation comes back for instant feedback."""
    from ui import project_builder as PB
    p, err = _project_or_404(pid)
    if err:
        return err
    steps = (p.get("learn") or {}).get("steps") or []
    if not (0 <= idx < len(steps)):
        return jsonify({"error": "No such step."}), 404
    ans = (request.json or {}).get("answer")
    ok = PB.check_answer(steps[idx], ans)

    def save(x):
        lr = x.setdefault("learn", {})
        learned = set(lr.get("learned") or [])
        if ok:
            learned.add(idx)
        lr["learned"] = sorted(learned)
        lr.setdefault("answers", {})[str(idx)] = {"answer": ans if isinstance(ans, (int, str)) else "", "ok": ok}
    p = _update_project(pid, save)
    c = steps[idx].get("check") or {}
    right = c["options"][c["answer"]] if c.get("kind") == "choice" else ""
    return jsonify({"correct": ok, "explain": c.get("explain", ""), "answer": right if ok else "",
                    "why": PB.option_why(steps[idx], ans),
                    "learned": (p.get("learn") or {}).get("learned") or [], "total": len(steps),
                    "project": _project_public(p)})


def _defense_gate(p: dict):
    """An AI-built project is defended only once it is built and Learn it is complete."""
    if p.get("origin") == "ai" and p.get("status") != "verified" and not _learn_done(p):
        return jsonify({"error": "Finish Learn it first, then defend your project."}), 409
    return None


# ---- Defend it: a 20-question multiple-choice quiz on THIS project (owner decision 2026-10-13,
# replacing the recorded interview). Generated ONCE per build from the real files and the Learn it
# walkthrough (one model call, inside the build's token budget, no package charged), cached on the
# project and never regenerated on a retake. The answer key stays here; grading is local. Each
# attempt shows the same questions in a fresh order with the options reshuffled. 14 of 20 passes and
# marks the project Verified, which unlocks Publish to GitHub and Add to resume (the gate above).
def _quiz_only():
    return jsonify({"error": "This project is defended with its quiz. Open Defend it."}), 400


def _make_project_quiz(pid: str, llm, files: list, steps: list, meter) -> list:
    """One model call for the quiz, metered against the build's budget. Returns the questions
    ([] on a short or failed reply) and stores them with the tokens it used."""
    from ui import project_builder as PB
    p = next((x for x in _load_projects() if x.get("id") == pid), None)
    if not p:
        return []
    before = meter.used
    try:
        raw = meter.call(llm, llm.generate_project_quiz, PB._idea(p), files, steps,
                         prompt_chars=sum(len(f["content"]) for f in files) + 4000)
        qs = PB.quiz_questions((raw or {}).get("questions"), files)
    except PB.BuildError as exc:
        print(f"[projects] quiz {pid} not made: {exc}")
        return []
    except Exception:
        traceback.print_exc()
        return []
    spent = meter.used - before
    print(f"[projects] quiz {pid}: {len(qs)} questions, tokens {spent} "
          f"(build total {meter.used} of {meter.budget}){' (estimated)' if meter.estimated else ''}")
    if len(qs) < PB.QUIZ_SIZE:
        return []
    _update_project(pid, lambda x: x.update(quiz={"questions": qs, "tokens": spent, "made": time.time(),
                                                  "attempt": None, "last": None, "tries": 0}))
    return qs


def _quiz_open(p: dict):
    """The quiz's questions, made now if the build could not (from what is left of its budget).
    Returns (questions, error response)."""
    from ui import project_builder as PB
    qs = (p.get("quiz") or {}).get("questions") or []
    if len(qs) >= PB.QUIZ_SIZE:
        return qs, None
    files = PB.load_files(_build_dir(p["id"]))
    steps = (p.get("learn") or {}).get("steps") or []
    if not files:
        return [], (jsonify({"error": "This project has no files yet."}), 400)
    spent = int(((p.get("build") or {}).get("tokens") or {}).get("total") or 0)
    meter = PB.Meter(budget=max(0, PB.TOKEN_BUDGET - spent))
    qs = _make_project_quiz(p["id"], _make_llm(), files, steps, meter)
    if not qs:
        return [], (jsonify({"error": "Couldn't prepare the quiz right now. Try again."}), 502)
    _update_project(p["id"], lambda x: x.setdefault("build", {}).update(
        tokens={**((x.get("build") or {}).get("tokens") or {}), "total": spent + meter.used}))
    return qs, None


@app.post("/api/projects/<pid>/quiz")
@_guard
def projects_quiz(pid):
    """Open Defend it: the current attempt (a fresh shuffled one when none is open), questions in
    their shown order with shuffled options, no answer key. Built and Learn it complete first."""
    import random
    from ui import project_builder as PB
    p, err = _project_or_404(pid)
    if err:
        return err
    if p.get("origin") != "ai":
        return jsonify({"error": "Only an AI-built project has a quiz."}), 400
    if p.get("status") == "verified":
        return jsonify({"error": "This project is already verified."}), 409
    gate = _defense_gate(p)
    if gate:
        return gate
    qs, err = _quiz_open(p)
    if err:
        return err
    holder = {}

    def ensure(x):
        qz = x.setdefault("quiz", {})
        att = qz.get("attempt")
        if not att or att.get("done") or len(att.get("order") or []) != len(qs):
            prev = att or qz.get("prev")
            att = {"id": uuid.uuid4().hex[:10], "done": False, "started": time.time(),
                   **PB.quiz_attempt(qs, random.SystemRandom(), prev)}
            qz["attempt"] = att
        holder["att"] = att
    p = _update_project(pid, ensure)
    att = holder["att"]
    last = (p.get("quiz") or {}).get("last")
    return jsonify({"attempt": att["id"], "questions": PB.public_quiz(qs, att), "total": len(qs),
                    "pass_mark": PB.quiz_pass_mark(len(qs)), "last": last, "project": _project_public(p)})


@app.post("/api/projects/<pid>/quiz/submit")
@_guard
def projects_quiz_submit(pid):
    """Grade an attempt locally: {attempt, answers: [shown option index or null, in shown order]}.
    14 of 20 marks the project Verified. A miss returns the wrong questions with the right answer
    and a one-line explanation; the next open is a retake (same questions, new order)."""
    from ui import project_builder as PB
    p, err = _project_or_404(pid)
    if err:
        return err
    if p.get("origin") != "ai":
        return jsonify({"error": "Only an AI-built project has a quiz."}), 400
    qz = p.get("quiz") or {}
    qs, att = qz.get("questions") or [], qz.get("attempt") or {}
    b = request.json or {}
    if not qs or not att or att.get("done") or str(b.get("attempt") or "") != att.get("id"):
        return jsonify({"error": "This quiz has changed. Open Defend it again."}), 409
    answers = b.get("answers") if isinstance(b.get("answers"), list) else []
    res = PB.grade_quiz(qs, att, answers)

    def save(x):
        q = x.setdefault("quiz", {})
        q["prev"] = {"order": att["order"]}
        q["attempt"] = dict(att, done=True)
        q["tries"] = int(q.get("tries") or 0) + 1
        q["last"] = {k: res[k] for k in ("score", "total", "pass_mark", "passed")} | {"at": time.time()}
        if res["passed"]:
            x["status"] = "verified"
            x["result"] = {"pass": True, "via": "quiz",
                           "summary": f"Passed the Defend it quiz, {res['score']} of {res['total']}."}
    p = _update_project(pid, save)
    return jsonify({**res, "project": _project_public(p)})


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
         "focus": focus, "difficulty": difficulty, "questions": questions, "record_id": rid,
         "cv": cv[:20000]}
    preps = _load_preps()
    preps.append(p)
    _save_preps(preps)
    return jsonify({"prep": _prep_public(p)})


def _when(stamp) -> float:
    """A sortable epoch for a stored timestamp: record rows are naive UTC ("2026-10-13 09:00:00"),
    board stage stamps carry their offset. Unreadable or missing sorts oldest."""
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(str(stamp or "").strip().replace(" ", "T", 1))
    except ValueError:
        return 0.0
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


@app.get("/api/preps/prefill")
@_guard
def preps_prefill():
    """What the interview setup page starts with, so most people just click Start: the role and
    job description of the job they touched most recently, the newest tailored resume or the
    newest job moved on the Tracker, whichever is later, plus that job's tailored resume as text
    when one exists. Empty fields when there is nothing yet. Read-only."""
    best = None                       # (when, payload)
    recs = _records()
    try:
        rows = recs.list(limit=1)
    finally:
        recs.close()
    if rows:
        r = rows[0]
        data = _record_data(r)
        role = (r.get("jd_label") or r.get("role") or "").split(" · ")[0].strip()
        best = (_when(r.get("created_at")), {"role": role, "company": r.get("company") or "",
                "jd": str(data.get("jd_text") or data.get("jd") or "").strip(), "record_id": r["id"]})
    w = _watchlist()
    try:
        by_job = _board_reconcile(w)
        live = [j for j in w.board_jobs() if j.get("stage") not in ("rejected", "archived")]
        if live:
            j = live[0]
            when = _when(j.get("stage_at"))
            if best is None or when > best[0]:
                full = w.get_job(j["source_id"]) or {}
                mine = sorted(by_job.get(j["source_id"], []), key=lambda r: -r["id"])
                rid = mine[0]["id"] if mine else None
                jd = str(full.get("jd_text") or "").strip()
                if not jd and mine:
                    jd = str(_record_data(mine[0]).get("jd_text") or "").strip()
                if not jd and _feed_serving():
                    # A role saved from the job feed keeps only its slim row locally; its
                    # description lives in the feed's shards (the setup showed the role over an
                    # empty description box, QA 2026-10-14).
                    try:
                        jd = str((_feed_job_with_jd(j["source_id"]) or {}).get("jd_text") or "").strip()
                    except Exception:                    # noqa: BLE001 - the person can paste it
                        jd = ""
                best = (when, {"role": (j.get("title") or "").strip(), "company": j.get("company") or "",
                               "jd": jd, "record_id": rid})
    finally:
        w.close()
    if best is None:
        return _nostore(jsonify({"role": "", "company": "", "jd": "", "record_id": None,
                                 "cv": "", "cv_name": ""}))
    out = best[1]
    cv = _record_cv_text(out["record_id"])[:20000] if out["record_id"] else ""
    name = ""
    if cv:
        name = out["role"] + (f" at {out['company']}" if out["company"] else "")
    return _nostore(jsonify({**out, "cv": cv, "cv_name": name}))


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
    try:
        question = qs[int(b.get("index"))].get("q", "")
    except (TypeError, ValueError, IndexError):
        question = str(b.get("question") or "").strip()
    profile = (_memory().load("default") or {}).get("profile") or {}
    result = _make_llm().coach_interview_answer(profile, question, answer)
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


# ---- Interview goals per job: the prep checklist shown on the interview start screen (the 5 most
# likely questions, the 3 things to make sure to mention, the company facts the JD reveals). Built
# from the JD and the tailored CV ONCE per job and cached locally; both rounds show the same one. ----
_GOALS_FILE = _DATA / "interview_goals.json"


def _load_goals() -> dict:
    try:
        d = json.loads(_GOALS_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_goals(goals: dict) -> None:
    _GOALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _GOALS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(goals, indent=2), encoding="utf-8")
    tmp.replace(_GOALS_FILE)


def _job_key(prep: dict) -> str:
    """What makes two preps 'the same job': the saved application when there is one, else the
    role and company typed in. The goals cache and the report-card history both key on this."""
    rid = prep.get("record_id")
    if rid not in (None, ""):
        return f"record:{rid}"
    role = str(prep.get("role") or "").strip().lower()
    company = str(prep.get("company") or "").strip().lower()
    return f"job:{role}|{company}" if role else f"prep:{prep.get('id')}"


def _record_cv_text(rid) -> str:
    """The tailored CV behind a saved application, as plain text (what the interviewer holds)."""
    if rid in (None, ""):
        return ""
    rec = _get_record(rid)
    if not rec:
        return ""
    data = _record_data(rec)
    prof = data.get("page_profile") or data.get("profile") or {}
    try:
        from tailoring.keywords import profile_text
        return profile_text(prof) if prof else ""
    except Exception:
        return ""


def _goals_for(prep: dict, refresh: bool = False) -> dict:
    key = _job_key(prep)
    goals = _load_goals()
    hit = goals.get(key)
    if isinstance(hit, dict) and hit.get("questions") and not refresh:
        return {**hit, "cached": True}
    profile = (_memory().load("default") or {}).get("profile") or {}
    cv = _record_cv_text(prep.get("record_id")) or str(prep.get("cv") or "")
    out = _make_llm().interview_goals(profile, prep.get("role", ""), prep.get("company", ""),
                                      prep.get("jd", ""), cv=cv)
    entry = {"questions": [str(x) for x in (out.get("questions") or [])][:5],
             "mentions": [str(x) for x in (out.get("mentions") or [])][:3],
             "company_facts": [str(x) for x in (out.get("company_facts") or [])][:5],
             "role": prep.get("role", ""), "company": prep.get("company", ""),
             "generated": int(time.time()), "job_key": key}
    goals[key] = entry
    _save_goals(goals)
    return {**entry, "cached": False}


@app.get("/api/preps/<pid>/goals")
@_guard
def preps_goals(pid):
    """The prep checklist for this prep's job. Generated on the first call and cached per job
    (`?refresh=1` rebuilds it). Shown on the start screen of both rounds."""
    prep = next((p for p in _load_preps() if p.get("id") == pid), None)
    if not prep:
        return jsonify({"error": "Prep not found."}), 404
    return jsonify({"goals": _goals_for(prep, refresh=request.args.get("refresh") == "1")})


def _history_for(prep: dict, screens: list, interviews: list, preps: list) -> dict:
    """Every scored attempt for the SAME job as this prep (Round 1 screens, re-drills and Round 2
    live interviews), oldest first, with the overall-score trend. Pure, so it is testable."""
    from interview.report import score_trend
    key = _job_key(prep)
    by_id = {p.get("id"): p for p in preps}
    same = {p.get("id") for p in preps if _job_key(p) == key}
    cards = []
    seq = 0                       # file order breaks ties inside one second (screens, then live)
    for s in screens:
        res = s.get("result")
        if s.get("prep_id") in same and isinstance(res, dict) and res.get("score") is not None:
            seq += 1
            cards.append({"id": s.get("id"), "kind": "screen", "round": 1, "_seq": seq,
                          "created": int(s.get("created") or 0), "score": int(res.get("score") or 0),
                          "passed": bool(res.get("passed")), "drill": bool(s.get("drill")),
                          "has_card": isinstance(res.get("report_card"), dict),
                          "questions": len([a for a in (s.get("answers") or []) if a.get("transcript")])})
    for iv in interviews:
        rep = iv.get("report")
        if iv.get("prep_id") in same and isinstance(rep, dict) and rep.get("score") is not None:
            seq += 1
            cards.append({"id": iv.get("id"), "kind": "interview", "round": 2, "_seq": seq,
                          "created": int(iv.get("ended") or iv.get("started") or 0),
                          "score": int(rep.get("score") or 0), "passed": bool(rep.get("passed")),
                          "drill": False, "has_card": isinstance(rep.get("report_card"), dict),
                          "questions": len(rep.get("answers") or [])})
    cards.sort(key=lambda c: (c["created"], c["_seq"]))
    for c in cards:
        c.pop("_seq", None)
    p0 = by_id.get(prep.get("id")) or prep
    return {"job_key": key, "role": p0.get("role", ""), "company": p0.get("company", ""),
            "cards": cards, "trend": score_trend([c["score"] for c in cards])}


def _all_reports(screens: list, interviews: list, preps: list) -> list:
    """Every scored interview across all jobs (Round 1 screens and Round 2 live interviews), newest
    first, for the Past interviews list on the prep landing. Pure, so it is testable."""
    by_id = {p.get("id"): p for p in preps}
    cards = []
    for s in screens:
        res = s.get("result")
        if isinstance(res, dict) and res.get("score") is not None:
            p = by_id.get(s.get("prep_id")) or {}
            cards.append({"id": s.get("id"), "round": 1, "prep_id": s.get("prep_id"),
                          "created": int(s.get("created") or 0), "score": int(res.get("score") or 0),
                          "passed": bool(res.get("passed")), "drill": bool(s.get("drill")),
                          "role": p.get("role", ""), "company": p.get("company", "")})
    for iv in interviews:
        rep = iv.get("report")
        if isinstance(rep, dict) and rep.get("score") is not None:
            p = by_id.get(iv.get("prep_id")) or {}
            cards.append({"id": iv.get("id"), "round": 2, "prep_id": iv.get("prep_id"),
                          "created": int(iv.get("ended") or iv.get("started") or 0),
                          "score": int(rep.get("score") or 0), "passed": bool(rep.get("passed")),
                          "drill": False, "role": iv.get("role") or p.get("role", ""),
                          "company": iv.get("company") or p.get("company", "")})
    cards.sort(key=lambda c: c["created"], reverse=True)
    return cards


@app.get("/api/interview/reports")
@_guard
def interview_reports():
    """The Past interviews list: every scored report, newest first."""
    return jsonify({"cards": _all_reports(_load_screens(), _load_interviews(), _load_preps())})


@app.get("/api/interview/history")
@_guard
def interview_history():
    """Past report cards for one job (`?prep_id=`), both rounds, with the overall-score trend."""
    pid = str(request.args.get("prep_id") or "").strip()
    preps = _load_preps()
    prep = next((p for p in preps if p.get("id") == pid), None)
    if not prep:
        return jsonify({"error": "Prep not found."}), 404
    return jsonify(_history_for(prep, _load_screens(), _load_interviews(), preps))


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


# ---- Interview tie-in: a Verified project on the job's resume gets 1 or 2 interview questions.
# Found by the project's title or repo URL among the projects on the tailored resume this prep was
# built from (else the resume text attached to the prep, else the saved profile's projects). Its
# stored facts (Learn it and the Defend it quiz, already on disk) ride as context in the EXISTING
# question generation, so this costs no model call of its own.
def _norm_url(u) -> str:
    return re.sub(r"^https?://(www\.)?", "", str(u or "").strip().lower()).rstrip("/").removesuffix(".git")


def _resume_project_entries(prep: dict) -> tuple[list[dict], str]:
    """(project entries on the resume, the resume's free text) for a prep: the tailored resume's
    projects when the prep came from a built resume, the attached resume text, or failing both
    the saved profile's projects."""
    entries, text = [], str(prep.get("cv") or "")
    rec = _get_record(prep["record_id"]) if prep.get("record_id") not in (None, "") else None
    if rec:
        data = _record_data(rec)
        for key in ("render_profile", "page_profile"):
            prof = data.get(key) or {}
            if isinstance(prof, dict) and prof.get("projects"):
                entries = [e for e in prof["projects"] if isinstance(e, dict)]
                break
        if not entries:
            text += "\n" + json.dumps(data)
    elif not text.strip():
        prof = (_memory().load("default") or {}).get("profile") or {}
        entries = [e for e in prof.get("projects") or [] if isinstance(e, dict)]
    return entries, text


def _project_interview_questions(p: dict) -> list[str]:
    """Up to two spoken interview questions on a project, from what is stored: the quiz's why,
    trade-off, scale and extend questions first, else a plain walk-through question."""
    t = p.get("title") or "your project"
    qs = (p.get("quiz") or {}).get("questions") or []
    pick = [q["q"] for topic in ("why", "tradeoffs", "scale", "extend")
            for q in qs if q.get("topic") == topic][:2]
    out = [f"On {t}, which you built: {q}" for q in pick]
    return out or [f"Walk me through {t}: why you built it the way you did, and what you would change."]


def _verified_project_facts(prep: dict) -> list[dict]:
    """The Verified projects on this prep's resume (at most two), with their stored facts."""
    from ui import project_builder as PB
    verified = [p for p in _load_projects() if p.get("status") == "verified"]
    if not verified:
        return []
    entries, text = _resume_project_entries(prep)
    low = text.lower()
    names = {str(e.get(k) or "").strip().lower() for e in entries for k in ("org", "title", "name")} - {""}
    links = {_norm_url(e.get(k)) for e in entries for k in ("link", "url", "repo")} - {""}
    out = []
    for p in verified:
        title, url = str(p.get("title") or "").strip(), _norm_url(p.get("repo_url"))
        hit = (title.lower() in names or (url and url in links)
               or (len(title) >= 4 and title.lower() in low) or (url and url in low))
        if not hit:
            continue
        steps = (p.get("learn") or {}).get("steps") or []
        facts = [f"{s.get('title', '')}: {s.get('why', '')}" for s in steps if s.get("why")][:3]
        facts += [f"{q['q']} {'; '.join(q['options'][i] for i in PB.quiz_correct(q))}."
                  for q in (p.get("quiz") or {}).get("questions") or []
                  if q.get("topic") in ("why", "tradeoffs", "scale")][:3]
        out.append({"title": title, "tech": p.get("tech", ""), "summary": p.get("summary", ""),
                    "repo_url": "" if p.get("repo_private") else (p.get("repo_url") or ""),
                    "facts": facts, "questions": _project_interview_questions(p)})
        if len(out) >= 2:
            break
    return out


def _with_project_questions(qs: list[dict], facts: list[dict], generated: list | None = None) -> list[dict]:
    """Make sure the screen asks about each Verified project on the resume (1, or 2 for two
    projects on a 5-question screen): when the model's set does not name a project, its stored
    question takes the last behavioural slot. Deterministic, no model call."""
    if not facts:
        return qs
    want = facts[:2] if len(qs) >= 5 else facts[:1]
    out = list(qs)
    for pr in want:
        t = pr["title"].lower()
        if any(t and t in q["q"].lower() for q in out):
            continue
        slots = [i for i, q in enumerate(out) if q["kind"] == "behavioural" and q.get("competency") != "Project"]
        if not slots:
            break
        out[slots[-1]] = {"q": pr["questions"][0], "kind": "behavioural", "competency": "Project",
                          "why": f"Probes how well you know {pr['title']}, which you built."}
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


def _drill_questions(raw) -> list[dict]:
    """The questions a re-drill hands over, normalised to the screen's shape: 1 to 5 items with a
    non-empty q; competency and kind kept when given ('drill' otherwise). [] when unusable."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, str):
            item = {"q": item}
        if not isinstance(item, dict):
            continue
        q = str(item.get("q") or item.get("text") or "").strip()
        if not q:
            continue
        out.append({"q": q[:600], "kind": _norm_kind(item.get("kind")) or "drill",
                    "competency": str(item.get("competency") or "").strip()[:60] or "General",
                    "why": "Practice this one again."})
        if len(out) >= _SCREEN_MAX_Q:
            break
    return out


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


def _measured_seconds(v) -> float | None:
    """A measured answer length from the client, or None when it sent nothing usable."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 1) if 0 < f < 3600 else None


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
            "drill": bool(s.get("drill")),
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
    # Re-drill (from a report card): the SAME recorded flow, with just the questions handed over
    # (1 to 5), no generation. The screen is flagged so the history shows it as a drill.
    drill = _drill_questions(b.get("drill"))
    if b.get("drill") is not None and not drill:
        return jsonify({"error": "Pick 1 to 5 questions to drill."}), 400
    extra = bool(b.get("extra_time"))
    if drill:
        qs = drill
    else:
        try:
            n = int(b.get("questions") or _SCREEN_DEFAULT_Q)
        except (TypeError, ValueError):
            n = _SCREEN_DEFAULT_Q
        if not (_SCREEN_MIN_Q <= n <= _SCREEN_MAX_Q):
            return jsonify({"error": f"Pick between {_SCREEN_MIN_Q} and {_SCREEN_MAX_Q} questions."}), 400
        kinds = _screen_mix(n)
        profile = (_memory().load("default") or {}).get("profile") or {}
        llm = _make_llm()       # no model -> fails loudly here (CLAUDE.md §13), never a silent fallback
        # A Verified project on this job's resume rides in the SAME question call as context.
        facts = _verified_project_facts(prep)
        try:
            gen = llm.generate_screen_questions(profile, prep.get("role", ""), prep.get("company", ""),
                                                prep.get("jd", ""), kinds=kinds,
                                                **({"projects": facts} if facts else {}))
            generated = gen.get("questions") or []
        except Exception:
            generated = []      # a bad model reply: the fit falls back to the prep's questions / defaults
        qs = _with_project_questions(_fit_screen_questions(generated, kinds, prep), facts, generated)
    answers = [{"q": q["q"], "kind": q["kind"], "competency": q["competency"], "why": q["why"],
                "video_path": "", "transcript": "", "per_answer_feedback": None,
                "delivery_metrics": None, "takes": 0} for q in qs]
    mult = 2 if extra else 1
    s = {"id": uuid.uuid4().hex[:12], "prep_id": prep_id, "role": prep.get("role", ""),
         "company": prep.get("company", ""), "answers": answers, "result": None, "passed": False,
         "created": int(time.time()), "extra_time": extra, "drill": bool(drill),
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
    # The report card's words-per-minute wants the real length of the take: the UI measures it
    # on the recorder and sends it along (an older UI sends nothing, and the card estimates).
    ans["seconds"] = _measured_seconds(request.form.get("seconds"))
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
    ans["delivery_metrics"] = delivery_metrics(text, ans.get("seconds"))
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
    body = request.json or {}
    text = str(body.get("transcript") or "").strip()
    ans = s["answers"][idx]
    ans["transcript"] = text
    if body.get("seconds") is not None:              # the UI may still know how long the take ran
        ans["seconds"] = _measured_seconds(body.get("seconds"))
    ans["delivery_metrics"] = delivery_metrics(text, ans.get("seconds"))
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
    # A project defence recorded before Defend it became a quiz is not job interview practice:
    # any such old screen on disk stays out of the readiness read.
    scored = [dict(s, _round=1) for s in (screens or []) if isinstance(s.get("result"), dict)
              and s["result"].get("score") is not None and not s.get("project_id")]
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


def _score_answer_set(role: str, company: str, answers: list, profile: dict,
                      round_no: int = 1) -> dict:
    """Shared scoring for a Round-1 recorded screen AND the Round-2 live dialogue: per-answer
    feedback (mutated onto each answered item) plus an aggregate PRACTICE result. Only answers
    that have a transcript are scored; the result is flagged is_practice (never a hiring outcome).
    Content, STAR structure, specificity, JD-term use, fillers and pace; never facial analysis.
    The structured report card (interview/report.py) rides along ADDITIVELY as
    ``result["report_card"]``: the overall score above is untouched, older stored results simply
    have no card."""
    from interview.report import build_report_card
    from interview.transcribe import delivery_metrics
    llm = _make_llm()
    scored = []
    for a in answers:
        if not str(a.get("transcript") or "").strip():
            continue
        d = a.get("delivery_metrics") or delivery_metrics(a.get("transcript", ""), a.get("seconds"))
        fb = llm.score_interview_answer(profile, a.get("q", ""), a.get("transcript", ""), d)
        a["per_answer_feedback"] = fb
        a["delivery_metrics"] = d
        scored.append({"q": a.get("q", ""), "transcript": a.get("transcript", ""),
                       "score": fb.get("score", 0)})
    result = llm.score_interview_screen(role, company, scored)
    result["is_practice"] = True          # honesty: a practice assessment, not a hiring outcome
    result["competencies"] = _competency_breakdown(answers)   # where they're strong vs weak
    try:
        result["report_card"] = build_report_card(answers, llm, overall_score=result.get("score"),
                                                  round_no=round_no)
    except Exception:
        result["report_card"] = None      # the card never takes the score down with it
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


# ---- Round 2: ONE live Tavus CVI interview (20 min) with an interviewer briefed on the JD and the
# person's resume, then a scored report (decision of 2026-10-02). PAID ONLY (Kofi, 2026-10-14): the
# minutes come from a pass or a prepaid pack, run on the COMPANY Tavus key, which lives only on the
# managed broker. Nobody adds their own Tavus key anywhere; an old AVATAR_API_KEY left on disk from
# an earlier version is ignored and never sent. Gated on passing Round 1 unless the person
# deliberately skips. Practice ONLY,
# never a hiring decision; never facial analysis. The transcript is scored, the video is never
# stored by us. ----
_INTERVIEWS_FILE = _DATA / "interviews.json"
SIMULATION_DISCLAIMER = "This is a practice simulation, not a real interview or hiring decision."
ROUND2_MAX_MINUTES = 20
# The interviewer wraps up at 15:00 and never cuts someone off mid-answer; Tavus's own hard stop
# sits a minute later, only as a safety net for a call left running by mistake.
ROUND2_SAFETY_SECONDS = 1260


def _cvi_timing() -> dict:
    """When the app's timekeeper passes notes to the interviewer (seconds). TAILOR_CVI_TEST_SCALE
    (e.g. 0.3) shrinks every mark so a short test call, such as Tavus's 5-minute free tier,
    walks through the whole pattern: a nudge, a step-in, the last question and the close."""
    try:
        k = float(os.environ.get("TAILOR_CVI_TEST_SCALE") or 1)
    except ValueError:
        k = 1.0
    k = min(1.0, max(0.1, k))
    # 20 minutes (Kofi, 2026-10-10; Mercor, micro1 and Alex run about 20): intro, 4 main questions
    # of about 4 minutes, the candidate's question, a close. The last main question is asked by
    # 15:30 so it has time to be answered properly.
    base = {"total": 1200, "answer_nudge": 180, "answer_step_in": 240, "pause_wait": 15,
            "last_question": 930, "candidate_questions": 1110, "close_wait": 20, "hard_end": 1230,
            "think_grace": 10, "answer_grace": 60}
    out = {name: max(5, int(v * k)) for name, v in base.items()}
    # The human waits never shrink with the test scale: a person needs the same few seconds to
    # think or finish a sentence however short the test call (at 0.3 the 10 s thinking time
    # became 3 s and an answer was skipped, 2026-10-10).
    for name in ("pause_wait", "close_wait", "think_grace", "answer_grace"):
        out[name] = base[name] if k >= 1 else max(out[name], {"pause_wait": 8, "close_wait": 10,
                                                              "think_grace": 10, "answer_grace": 30}[name])
    out["minutes"] = max(1, round(out["total"] / 60))
    return out
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
ROUND2_INTERVIEW_SECONDS = 1200
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
    """Whether Round 2 can run right now. Live interviews are PAID ONLY: they run through the
    managed broker on the company's Tavus key (which never leaves the broker), drawing on the
    person's pass + purchased extra interviews, and only when those cover a full interview.
    Otherwise unavailable with the reason: 'no_pass' (no active pass; `offer: "passes"`),
    'no_minutes' (an active pass whose interviews are used up; `offer: "packs"`, buyable while
    the pass lasts) or 'unreachable' (the broker can't be reached; try again). Also reports
    `tier`, `pass_until` and whether extra interviews can be bought (`can_buy`, `packs`)."""
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
    extra = {"can_buy": bool(packs), "packs": packs, "tier": tier or None,
             "pass_until": pass_until if on_pass else None,
             "offer": "packs" if on_pass else ("passes" if st == 200 else None)}
    if secs >= ROUND2_INTERVIEW_SECONDS:
        return {"available": True, "reason": "ok", "source": "plan", "minutes_left": secs // 60, **extra}
    reason = "no_minutes" if on_pass else ("no_pass" if st == 200 else "unreachable")
    return {"available": False, "reason": reason, "source": None,
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
    if st == 401 and (data or {}).get("reason") == "sign_in_required":
        return jsonify({"error": "Sign in to buy extra interviews.", "reason": "sign_in_required"}), 401
    if st == 402:
        return jsonify({"error": "Extra interviews are sold while a pass is active. Get a pass "
                                 "first.", "offer": "passes"}), 402
    if st != 200 or not data or not data.get("url"):
        return jsonify({"error": (data or {}).get("error") or "Couldn't start checkout."}), 502
    _PACKS_CACHE.update(at=0.0, packs=None)
    return jsonify({"url": data["url"]})


# ---- The managed broker: the ONLY source of Round-2 interviews (the passes + prepaid packs) and the account /
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
    from llm.broker_client import BROKER_USER_AGENT
    # Two tries: this is the first call a fresh install ever makes, and one slow response
    # (a cold edge, a flaky network) must not turn into "could not set up your account".
    for attempt, timeout in enumerate((10, 20)):
        try:
            r = requests.post(f"{BROKER_URL}/account/register", timeout=timeout,
                              headers={"User-Agent": BROKER_USER_AGENT})
            if r.status_code == 200:
                tok = (r.json() or {}).get("token")
                if tok:
                    _save_cred("TAILOR_ACCOUNT_TOKEN", tok)
                    return tok
            if r.status_code == 429:
                break                                  # the network's daily limit: retrying won't help
        except requests.RequestException:
            if attempt == 1:
                break
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
    """This install's account: its email when signed in (the account menu and foot show it, and
    "Get the pass" goes straight to Stripe only when there is one) and its tier with what is left (so the profile badge and the
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
    """Where to send the person for 'Continue with Google': the broker's OAuth start, told to come
    back to this app's own callback page (/account/google/done), which reads the token from the
    URL fragment and adopts it. ?via=browser (the desktop app: the flow runs in the person's own
    browser, since Google refuses sign-in inside embedded browsers) or ?via=tab (the web build:
    same tab, and the callback page returns to the app)."""
    from urllib.parse import quote
    via = "tab" if request.args.get("via") == "tab" else "browser"
    callback = f"{request.host_url.rstrip('/')}/account/google/done?via={via}"
    return jsonify({"url": f"{BROKER_URL}/account/google/start?redirect={quote(callback, safe='')}"})


@app.post("/api/account/google/open")
@_guard
def account_google_open():
    """Desktop app: open the Google sign-in in the person's OWN default browser (RFC 8252: native
    apps must not host sign-in in an embedded view). The callback page lands back on this engine,
    adopts the token, and the app sees the email on its next status check."""
    import webbrowser
    from urllib.parse import quote
    callback = f"{request.host_url.rstrip('/')}/account/google/done?via=browser"
    url = f"{BROKER_URL}/account/google/start?redirect={quote(callback, safe='')}"
    try:
        webbrowser.open(url)
    except Exception:
        return jsonify({"error": "Couldn't open your browser.", "url": url}), 500
    return jsonify({"ok": True, "url": url})


_GOOGLE_DONE_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>SponsorJobs</title>
<style>
:root{color-scheme:light dark;--bg:#131316;--ink:#e7e7ea;--muted:#82828c}
@media (prefers-color-scheme: light){:root{--bg:#f7f7f8;--ink:#18181b;--muted:#71717a}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
font:14px/1.5 "Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;text-align:center}
h1{font-size:19px;font-weight:600;margin:0}
p{margin:6px 0 0;color:var(--muted)}
</style></head><body><main>
<h1 id="h">Signing you in...</h1><p id="p"></p></main>
<script>
(function () {
  var q = new URLSearchParams(location.search), h = new URLSearchParams(location.hash.slice(1));
  var via = q.get("via") === "tab" ? "tab" : "browser";
  var token = h.get("token") || q.get("account") || "";
  var err = h.get("error") === "cancelled" ? "cancelled" : "error";
  history.replaceState(null, "", location.pathname + "?via=" + via);   // keep the token out of history
  function done(ok, msg) {
    if (via === "tab") { location.replace("/?signin=" + (ok ? "ok" : err)); return; }
    if (ok) setTimeout(function () { location.href = "sponsorjobs://signed-in"; }, 400);   // back to the app
    document.getElementById("h").textContent = ok ? "You're signed in"
      : (err === "cancelled" ? "Sign-in cancelled" : "Sign-in didn't finish");
    document.getElementById("p").textContent = msg;
  }
  if (!token) {
    done(false, err === "cancelled" ? "Nothing changed. Go back to SponsorJobs to try again."
                                    : "Google couldn't sign you in. Go back to SponsorJobs and try again.");
    return;
  }
  fetch("/api/account/adopt", {method: "POST", headers: {"Content-Type": "application/json"},
                               body: JSON.stringify({token: token})})
    .then(function (r) { return r.json().then(function (d) { return {ok: r.ok && !d.error, d: d}; }); })
    .then(function (x) { done(x.ok, x.ok ? "You can close this tab and go back to SponsorJobs."
                                         : (x.d.error || "Go back to SponsorJobs and try again.")); })
    .catch(function () { done(false, "Go back to SponsorJobs and try again."); });
})();
</script></body></html>"""


@app.get("/account/google/done")
def account_google_done():
    """The Google sign-in callback page. The broker redirects here with #token=... in the URL
    FRAGMENT (never sent to any server, so it stays out of logs); the page posts it to
    /api/account/adopt on this same engine."""
    return _GOOGLE_DONE_PAGE, 200, {"Content-Type": "text/html; charset=utf-8",
                                    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


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


_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


@app.post("/api/account/email/start")
@_guard
def account_email_start():
    """Email sign-in, step 1: {email} -> the broker emails a 6-digit code. No passwords anywhere.
    Errors carry an `error_code` the sign-in screen shows inline: rate_limited, bad_email, off."""
    email = str((request.json or {}).get("email", "")).strip()
    if not _EMAIL_RE.fullmatch(email) or len(email) > 254:
        return jsonify({"error": "Enter a valid email address.", "error_code": "bad_email"}), 400
    st, data = _broker_post("/account/email/start", {"email": email})
    if st == 200:
        return jsonify({"sent": True, "email": email})
    if st == 0:
        return jsonify({"error": "The account service isn't reachable. Try again in a moment.",
                        "error_code": "unreachable"}), 503
    if st == 429:
        return jsonify({"error": "Too many codes asked for. Wait a few minutes and try again.",
                        "error_code": "rate_limited"}), 429
    if st == 400:
        return jsonify({"error": "Enter a valid email address.", "error_code": "bad_email"}), 400
    if st == 503:
        return jsonify({"error": "Email sign-in isn't available yet.", "error_code": "off"}), 503
    return jsonify({"error": "Couldn't send the code. Try again in a moment.", "error_code": "failed"}), 502


@app.post("/api/account/email/verify")
@_guard
def account_email_verify():
    """Email sign-in, step 2: {email, code} -> the broker checks the code (with this device's
    bearer token, so an anonymous account can be merged into the email account) and returns the
    email account's token, which this install adopts as its device token."""
    b = request.json or {}
    email = str(b.get("email", "")).strip()
    code = re.sub(r"\D", "", str(b.get("code", "")))
    if not email or len(code) != 6:
        return jsonify({"error": "Enter the 6-digit code from the email.", "error_code": "bad_code"}), 400
    st, data = _broker_post("/account/email/verify", {"email": email, "code": code})
    if st == 0:
        return jsonify({"error": "The account service isn't reachable. Try again in a moment.",
                        "error_code": "unreachable"}), 503
    if st == 200 and data and data.get("token"):
        _save_cred("TAILOR_ACCOUNT_TOKEN", data["token"])   # this device is now that account
        return jsonify({"ok": True, "email": data.get("email") or email,
                        "account_id": data.get("account_id")})
    reason = (data or {}).get("reason")
    if st == 503:
        return jsonify({"error": "Email sign-in isn't available yet.", "error_code": "off"}), 503
    if st == 400 and reason == "expired":
        return jsonify({"error": "That code has expired. Send a new one.", "error_code": "expired"}), 400
    if st == 400:
        return jsonify({"error": "That code isn't right. Check the email and try again.",
                        "error_code": "bad_code"}), 400
    if st == 429:
        return jsonify({"error": "Too many tries. Wait a few minutes and try again.",
                        "error_code": "rate_limited"}), 429
    return jsonify({"error": "Couldn't sign you in. Try again.", "error_code": "failed"}), 502


@app.post("/api/account/signout")
@_guard
def account_signout():
    """Sign out on this device: forget the email account's token and start a fresh anonymous one
    (the free tier), exactly like a new install. The pass stays on the email account and comes
    back on the next sign-in."""
    _revoke_cred("TAILOR_ACCOUNT_TOKEN")
    _account_token()          # registers a fresh anonymous account; None if offline (retried later)
    return jsonify({"ok": True, "email": None})


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
    if st == 401 and (data or {}).get("reason") == "sign_in_required":
        return jsonify({"error": "Sign in to get your pass.", "reason": "sign_in_required"}), 401
    if st != 200 or not data or not data.get("url"):
        return jsonify({"error": (data or {}).get("error") or "Couldn't start checkout."}), 502
    _PACKS_CACHE.update(at=0.0, packs=None)
    return jsonify({"url": data["url"]})


# The interviewer's standing instructions. The broker's company account has no PAL of ours, so they
# ride in the conversation context with everything specific to the interview (company, role, JD,
# resume, questions).
_PAL_SYSTEM_PROMPT = (
    "You are a professional hiring manager running a live mock job interview; the context gives its "
    "length in minutes, and you say that length, never another. The "
    "conversational context you receive names the company, the role, the job description, the "
    "candidate's resume and the questions to cover; stay in that character throughout.\n"
    "You keep the time, like a real interviewer with a full day of candidates. Structure: "
    "(1) introduce yourself in one or two sentences, say how many minutes the interview lasts, and ask the "
    "candidate to introduce themselves briefly; (2) ask exactly 4 main questions, ONE AT A TIME, "
    "wait for the answer, and ask at most ONE short follow-up per question; (3) ask whether they "
    "have a question for you and answer it briefly from the job description; (4) close warmly: "
    "thank them and say their feedback report is ready.\n"
    "TIME NOTES: during the call you will receive notes that start with TIME NOTE. They come from "
    "the interview organiser, never from the candidate, and you must follow them on your very next "
    "turn without mentioning them. When a note says to move on, briefly acknowledge the answer "
    "('That's helpful, thank you') and ask the next question; when it says to step in, say "
    "'Sorry to jump in, that's really helpful, and I want to make sure we cover everything' and ask "
    "the next question.\n"
    "Rules: keep each of your turns under 40 words; never answer for the candidate; never ask what "
    "they want to talk about; use the job description's own vocabulary; do not give scores or "
    "feedback during the interview. Never thank, praise or comment on an answer the candidate did "
    "not give: if they have not answered, ask whether they would like a moment or to move on. "
    "Let the candidate finish; never start speaking while they are mid-sentence. "
    "If the candidate is silent for a few seconds after a question, "
    "say 'Take your time' once; if the silence goes on, rephrase the question in one short sentence."
)
_PAL_GREETING = ("Hi, thanks for joining. I'll be interviewing you today. To start, could you tell "
                 "me a little about yourself and what drew you to this role?")


def _round2_plan(prep: dict, facts: list[dict] | None = None) -> list[dict]:
    """The 4 to 6 questions the interviewer should cover, with the competency each probes, drawn
    from the prep (JD-grounded). A Verified project on the job's resume (``facts``) adds one
    question per project (at most two), third in line so the interviewer reaches it. Used in the
    briefing and to tag competencies in the report."""
    out = []
    for q in (prep.get("questions") or []):
        text = str(q.get("q") or "").strip()
        if text:
            out.append({"q": text, "competency": str(q.get("competency") or "").strip() or "General"})
        if len(out) >= 6:
            break
    proj = [{"q": pr["questions"][0], "competency": "Project"} for pr in (facts or [])[:2] if pr.get("questions")]
    if proj:
        out = (out[:2] + proj + out[2:])[:6]
    return out


def _round2_briefing(prep: dict, profile: dict, facts: list[dict] | None = None) -> str:
    """The per-interview briefing (Tavus `conversational_context`): interviewer persona for this
    company/role, the interview's length and structure, the JD and the person's résumé/profile, and the
    questions to cover. Sent to Tavus to steer the interviewer; not stored by us."""
    role = (prep.get("role") or "the role").strip()
    company = (prep.get("company") or "").strip()
    jd = str(prep.get("jd") or "").strip()
    who = f"the {role} role" + (f" at {company}" if company else "")
    lines = [
        f"You are the hiring manager for {who}, interviewing this candidate in a "
        f"{_cvi_timing()['minutes']}-minute practice "
        "interview. Introduce yourself as the hiring manager for this team.",
        "Ask one question at a time, listen to the whole answer, probe with short follow-ups, and "
        "keep to: intro, 4 to 6 questions, the candidate's questions, close.",
    ]
    plan = _round2_plan(prep, facts)
    if plan:
        lines.append("QUESTIONS TO COVER (adapt the wording, ask in a natural order):")
        lines += [f"- {q['q']} (probes: {q['competency']})" for q in plan]
    if facts:
        from llm.base import project_facts_text
        lines.append("Be sure to ask the Project question(s): the candidate built this and lists it on "
                     "their resume.\n" + project_facts_text(facts))
    if jd:
        lines.append("JOB DESCRIPTION:\n" + jd[:2500])
    if profile:
        lines.append("CANDIDATE RESUME / PROFILE (JSON):\n" + json.dumps(profile)[:2500])
    return "\n\n".join(lines)


def _interview_public(iv: dict) -> dict:
    return {"id": iv.get("id"), "prep_id": iv.get("prep_id"), "screen_id": iv.get("screen_id"),
            "skipped_screen": bool(iv.get("skipped_screen")),
            "role": iv.get("role", ""), "company": iv.get("company", ""),
            "source": iv.get("source"), "conversation_id": iv.get("conversation_id", ""),
            "started": int(iv.get("started") or 0), "ended": int(iv.get("ended") or 0),
            "max_minutes": int(iv.get("max_minutes") or ROUND2_MAX_MINUTES),
            "report": iv.get("report"), "is_practice": True, "disclaimer": SIMULATION_DISCLAIMER}


@app.post("/api/interviews/cvi/start")
@_guard
def interviews_cvi_start():
    """Start the ONE live interview for a prep: {prep_id, screen_id?, skip_screen?}.
    403 screen_not_passed unless the given screen passed (or skip_screen with no screen_id);
    402 round2_unavailable {reason} when the pass + packs don't cover a full interview. The
    managed broker mints the session on the company key (paid only, never a person's own key).
    Returns the join URL for the app's video pane."""
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
    facts = _verified_project_facts(prep)
    briefing = _round2_briefing(prep, profile, facts)
    role = prep.get("role", "")
    source = "plan"
    # The company account has no PAL of ours, so the interviewer's standing instructions ride in
    # the context with the briefing, and the opening line goes as the custom greeting.
    st, data = _broker_post("/avatar/session/start", {"context": {
        "prompt": f"{_PAL_SYSTEM_PROMPT}\n\n{briefing}", "greeting": _PAL_GREETING}})
    if st == 0:
        return jsonify({"error": "service_unavailable",
                        "message": "The interview service isn't reachable right now."}), 503
    if st == 402:
        on_pass = (data or {}).get("reason") == "no_interviews" or status.get("tier") in _PASS_TIERS
        return jsonify({"error": "round2_unavailable", "reason": "no_minutes" if on_pass else "no_pass",
                        "source": None, "can_buy": status.get("can_buy", False),
                        "packs": status.get("packs", []), "offer": "packs" if on_pass else "passes",
                        "message": (data or {}).get("error") or "You're out of live interviews."}), 402
    if st != 200 or not data or not data.get("session_url"):
        return jsonify({"error": "service_error", "message": "Couldn't start the live interview."}), 502
    join_url, conversation_id = data["session_url"], str(data.get("provider_session_id") or "")
    iv = {"id": uuid.uuid4().hex[:12], "prep_id": prep.get("id"), "screen_id": screen_id or None,
          "skipped_screen": not screen_id, "role": role, "company": prep.get("company", ""),
          "source": source, "conversation_id": conversation_id,
          "started": int(time.time()), "max_minutes": ROUND2_MAX_MINUTES,
          "plan": _round2_plan(prep, facts), "report": None, "transcript": []}
    interviews = _load_interviews()
    interviews.append(iv)
    _save_interviews(interviews)
    return jsonify({"interview_id": iv["id"], "join_url": join_url, "conversation_id": conversation_id,
                    "max_minutes": ROUND2_MAX_MINUTES, "source": source, "timing": _cvi_timing(),
                    "role": role, "company": iv["company"], "disclaimer": SIMULATION_DISCLAIMER})


@app.post("/api/interviews/cvi/heartbeat")
@_guard
def interviews_cvi_heartbeat():
    """While the live conversation runs, report elapsed seconds; the broker meters them against
    the pass and says when to stop at the limit."""
    b = request.json or {}
    try:
        seconds = int(b.get("seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0
    iid = str(b.get("interview_id") or "").strip()
    iv = next((x for x in _load_interviews() if x.get("id") == iid), None) if iid else None
    st, data = _broker_post("/avatar/heartbeat", {"seconds": max(0, seconds),
                                                  "conversation_id": (iv or {}).get("conversation_id") or ""})
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


_REPROMPT = re.compile(r"^\s*(take your time|no problem|no worries|sorry|let me (rephrase|repeat|put it)|"
                       r"(i was|i'm|i am) (just )?asking|to (clarify|rephrase)|in other words|"
                       r"could you walk me through a specific)", re.I)
_ASKS_FOR_THEIR_QUESTION = re.compile(r"(any|one|a) questions? for me|questions? (do|would) you have for me", re.I)


def _pair_dialogue(turns: list, plan: list, min_words: int = 8) -> list[dict]:
    """Turn a dialogue into scorable {q, transcript, competency} items: each interviewer turn (or run
    of turns) is a question, the candidate turns that follow are its answer. Answers shorter than
    `min_words` (yes / thank you / goodbye) are not scored. Pure."""
    items: list[dict] = []
    cur: dict | None = None
    turns = [t for t in (turns or []) if str(t.get("content") or "").strip()]
    for k, t in enumerate(turns):
        content = str(t.get("content") or "").strip()
        if t.get("role") == "interviewer":
            # "Take your time", "No problem, I was asking...", "Let me rephrase": the SAME question
            # again, not a new one. The answer is everything said to it, so a false start ("U.S.
            # companies have, you know...") is not scored on its own (Kofi's test, 2026-10-10).
            # "That's", "Right", "Mm-hm": a few words cut off by the person talking over them are the
            # interviewer reacting, not a new question. The answer carries on under the same one.
            if cur is not None and cur["transcript"] and len(content.split()) < 4 and "?" not in content:
                cur["transcript"] += " "
                continue
            if cur is not None and cur["transcript"] and _REPROMPT.match(content):
                cur["transcript"] += " "
                continue
            if cur is not None and cur["transcript"]:
                items.append(cur)
                cur = None
            if cur is None:
                cur = {"q": content, "transcript": "", "seconds": None}
            else:
                cur["q"] = (cur["q"] + " " + content).strip()
        else:
            if cur is None:
                cur = {"q": "Tell me about yourself.", "transcript": "", "seconds": None}
            cur["transcript"] = (cur["transcript"] + " " + content).strip()
            # The report card's pace wants how long the person spoke: the turn's own duration when
            # the transcript carries timestamps, else the gap to the next turn; else unknown.
            secs = _turn_seconds(t, turns[k + 1] if k + 1 < len(turns) else None)
            if secs is not None:
                cur["seconds"] = round((cur["seconds"] or 0) + secs, 1)
    if cur is not None and cur["transcript"]:
        items.append(cur)
    out = []
    for it in items:
        if len(it["transcript"].split()) < min_words:
            continue
        if _ASKS_FOR_THEIR_QUESTION.search(it["q"]):
            continue                     # "Do you have a question for me?": theirs, not an answer
        it["competency"] = _closest_competency(it["q"], plan)
        it["per_answer_feedback"], it["delivery_metrics"] = None, None
        out.append(it)
    return out


def _turn_seconds(t: dict, nxt: dict | None) -> float | None:
    """How long one transcript turn ran: its `duration`, else the gap to the next turn's start.
    None when the transcript has no timestamps (a pasted transcript). Pure."""
    try:
        if t.get("duration") is not None:
            d = float(t.get("duration"))
            return d if d > 0 else None
        if t.get("seconds_from_start") is not None and nxt and nxt.get("seconds_from_start") is not None:
            d = float(nxt["seconds_from_start"]) - float(t["seconds_from_start"])
            return d if d > 0 else None
    except (TypeError, ValueError):
        return None
    return None


def _score_live_dialogue(iv: dict, turns: list, profile: dict, duration_s: int) -> dict:
    """Score the live interview's transcript with the same per-answer + aggregate scorers as Round 1,
    adapted for a dialogue (questions paired with the answers that followed). Content, STAR,
    specificity, JD terms, fillers, pace; never the video."""
    answers = _pair_dialogue(turns, iv.get("plan") or [])
    if answers:
        result = _score_answer_set(iv.get("role", ""), iv.get("company", ""), answers, profile,
                                   round_no=2)
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
    """End the live interview {interview_id, transcript?, duration_s?}: the broker ends the
    conversation on the company key and hands its transcript back, which is scored here, stored
    with the report, and returned. If no transcript comes back (yet), a client-supplied
    `transcript` (text, or [{role, content}]) is scored instead; with neither it's a 400 so the
    UI can ask for one. An interview recorded on an own key by an older version is scored only
    from a supplied transcript."""
    b = request.json or {}
    iid = str(b.get("interview_id") or "").strip()
    interviews = _load_interviews()
    iv = next((x for x in interviews if x.get("id") == iid), None)
    if not iv:
        return jsonify({"error": "Interview not found."}), 404
    from backend.tavus_client import parse_transcript_text
    turns: list = []
    cid = iv.get("conversation_id") or ""
    if iv.get("source") == "plan" and cid:
        # Plan path: the company key lives on the broker, which ends the conversation and hands
        # the transcript straight back (it keeps nothing). It can lag the end by a few seconds.
        for attempt in range(3):
            st, data = _broker_post("/avatar/session/end", {"conversation_id": cid})
            turns = [t for t in ((data or {}).get("transcript") or []) if isinstance(t, dict)] \
                if st == 200 else []
            if turns or st not in (200,):
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
    # The timekeeper's notes reach the interviewer as if the candidate had spoken them (Tavus's
    # conversation.respond), so they come back in the transcript under the candidate's name.
    # They are the organiser's words, never the person's: drop them before anything is scored.
    turns = [t for t in turns if not str(t.get("content") or "").lstrip().upper().startswith("TIME NOTE")]
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
    # A recruiter reply SUGGESTS a tracker move (Interviewing, or Rejected); the person clicks.
    _board_suggestions(result.get("recruiters") or [])
    # The finer triage (interview / rejection / offer / assessment / confirmation) for mail
    # that names a TRACKED employer lands in the "From your inbox" strip. Stored by message id
    # so a rescan never re-asks about mail the person already settled.
    result["suggestions"] = _store_inbox_suggestions(messages, llm)
    return _nostore(jsonify({"ok": True, **result}))


# ---- the apply agent's mailbox (docs/apply-agent.md milestone 3) -----------------------------
# A portal that just got a new account sends a verification link, a one-time code, or a
# password reset. These routes let the run wait for THAT mail in the person's own inbox,
# read-only, and nothing else: every search result is gated by detect.about_site before it
# is read any further (CLAUDE.md section 5). Nothing here logs a subject or a body.

_AWAIT_POLL_S = 4


@app.post("/api/inbox/connect-imap")
@_guard
def inbox_connect_imap():
    """Body {email, app_password, host?}. Signs in once to prove the credentials, then keeps
    them locally (App Lock custody when on). Returns {ok, email, host}; the password never
    comes back. On failure {ok: false, error: <one plain sentence>}."""
    from inbox.mailbox import ImapMailbox, guess_imap_host
    b = request.json or {}
    address = str(b.get("email") or "").strip()
    # Google shows an app password as four groups of four; the spaces are not part of it.
    password = str(b.get("app_password") or b.get("password") or "").replace(" ", "")
    host = str(b.get("host") or "").strip().lower() or guess_imap_host(address)
    if "@" not in address or "." not in address.rpartition("@")[2]:
        return jsonify({"ok": False, "error": "Enter the email address of the mailbox."}), 400
    if len(password) < 8:
        return jsonify({"ok": False, "error": "Enter the app password (16 characters for a "
                                              "Google account)."}), 400
    check = ImapMailbox(address, password, host=host).test_login()
    if not check.get("ok"):
        return jsonify({"ok": False, "error": check.get("error") or "Sign-in failed."}), 400
    _save_cred("IMAP_EMAIL", address)
    _save_cred("IMAP_PASSWORD", password)
    _save_cred("IMAP_HOST", host)
    return _nostore(jsonify({"ok": True, "email": address, "host": host}))


@app.post("/api/inbox/disconnect-imap")
@_guard
def inbox_disconnect_imap():
    """Forget the IMAP credentials entirely (lock copy and plaintext line)."""
    for name in ("IMAP_EMAIL", "IMAP_PASSWORD", "IMAP_HOST"):
        _revoke_cred(name)
    return jsonify({"ok": True})


def _await_match(messages: list[dict], kind: str, host: str, company: str) -> dict | None:
    """The first message about this site that is the kind the run waits for, as the reply."""
    from inbox import detect
    for msg in messages:
        if not detect.about_site(msg, host, company):
            continue            # not this employer's mail: never read any further
        found = detect.kind_of(msg)
        if not found or (kind != "any" and found != kind):
            continue
        out = {"ok": True, "kind": found, "subject": msg.get("subject", ""),
               "message_id": msg.get("id", "")}
        if found == "verify":
            out["link"] = detect.verification_link(msg)
        elif found == "reset":
            out["link"] = detect.password_reset_link(msg)
        elif found == "code":
            out["code"] = detect.otp_code(msg)
        return out
    return None


@app.post("/api/inbox/await")
@_guard
def inbox_await():
    """Long-poll the person's mailbox for the mail an unattended apply run is waiting on.
    Body {kind: verify|code|reset|any, host, company?, since (epoch seconds), timeout_s}.
    Returns {ok: true, kind, link?, code?, subject, message_id}; {ok: false, reason:
    "not-connected"} at once when no mailbox is set up; {ok: false, reason: "timeout"} after
    timeout_s (default 120, at most 240). Blocks the request; the server is threaded."""
    b = request.json or {}
    kind = str(b.get("kind") or "any").strip().lower()
    if kind not in ("verify", "code", "reset", "any"):
        return jsonify({"ok": False, "error": "kind must be verify, code, reset or any."}), 400
    from submit.policy import _host
    host = _host(str(b.get("host") or b.get("url") or ""))
    company = str(b.get("company") or "").strip()
    if not host and not company:
        return jsonify({"ok": False, "error": "Say which site (host) the mail is from."}), 400
    try:
        since = float(b.get("since") or time.time())
    except (TypeError, ValueError):
        since = time.time()
    try:
        timeout_s = int(b.get("timeout_s") or 120)
    except (TypeError, ValueError):
        timeout_s = 120
    timeout_s = max(1, min(timeout_s, 240))
    mailbox = _current_mailbox()
    if mailbox is None:
        return jsonify({"ok": False, "reason": "not-connected"})
    poll = 3 if _under_test_or_dev() else _AWAIT_POLL_S
    deadline = time.time() + timeout_s
    while True:
        try:
            messages = mailbox.search(since)
        except Exception as exc:     # the mailbox itself failed (server down, token stale)
            return jsonify({"ok": False, "reason": "mailbox-error", "error": str(exc)})
        hit = _await_match(messages, kind, host, company)
        if hit:
            return _nostore(jsonify(hit))
        if time.time() + poll > deadline:
            return jsonify({"ok": False, "reason": "timeout"})
        time.sleep(poll)


@app.post("/api/inbox/dev/deliver")
def inbox_dev_deliver():
    """Put a message in the FakeMailbox (shell e2e). 404 unless RESUME_AGENT_FAKE_MAILBOX=1 in
    a dev or test session: this does not exist in a shipped build."""
    if not _fake_mailbox_on():
        return jsonify({"error": "Not found."}), 404
    from inbox.mailbox import fake_mailbox
    b = request.json or {}
    stored = fake_mailbox().deliver({"from": b.get("from", ""), "subject": b.get("subject", ""),
                                     "body": b.get("body", ""), "id": b.get("id", "")})
    return jsonify({"ok": True, "id": stored["id"]})


_SUGGESTIONS_FILE = _DATA / "inbox_suggestions.json"


def _suggestion_store():
    from inbox.suggestions import SuggestionStore
    return SuggestionStore(_SUGGESTIONS_FILE)


def _store_inbox_suggestions(messages: list[dict], llm=None) -> list[dict]:
    """Triage ``messages`` against the board, keep the new suggestions, return the pending
    ones. The model pass (``llm``) only ever sees mail the rules left as "other" AND that
    names a tracked employer (CLAUDE.md §5: nothing unrelated is read by anyone)."""
    from inbox.suggestions import build_suggestions
    store = _suggestion_store()
    known = {s["id"] for s in store.all()}
    from inbox.suggestions import suggestion_id
    fresh = [m for m in (messages or []) if suggestion_id(m) not in known]
    if fresh:
        w = _watchlist()
        try:
            _board_reconcile(w)
            jobs = w.board_jobs()
        finally:
            w.close()
        store.add(build_suggestions(fresh, jobs, llm=llm))
    return store.pending()


@app.get("/api/inbox/suggestions")
@_guard
def inbox_suggestions():
    """The pending "From your inbox" suggestions (Tracker strip, Review page). Read-only."""
    return _nostore(jsonify({"ok": True, "suggestions": _suggestion_store().pending()}))


@app.post("/api/inbox/suggestions/<sid>/accept")
@_guard
def inbox_suggestion_accept(sid):
    """The person's click: move the card to the suggested stage, log the mail subject in the
    timeline, and settle the suggestion (plus any other pending one for the same job and
    stage). This is the ONLY path from a suggestion to a move."""
    from datetime import date
    store = _suggestion_store()
    s = store.get(sid)
    if s is None:
        return jsonify({"error": "No such suggestion."}), 404
    if s.get("status", "pending") != "pending":
        return jsonify({"error": "That suggestion was already settled."}), 409
    w = _watchlist()
    try:
        by_job = _board_reconcile(w)
        job_id = s["source_id"]
        if not w.stage_of(job_id):
            store.set_status(sid, "dismissed")
            return jsonify({"error": "That job is no longer on your board."}), 404
        note = f'From your inbox: "{(s.get("subject") or "").strip()[:120]}"'
        row = w.move_stage(job_id, s["stage"], note=note)
        _board_patch_records(by_job.get(job_id, []), s["stage"])
        job = _sponsors().tag_jobs([row])[0]
        due = len(w.due_followups(date.today()))
    finally:
        w.close()
    store.set_status(sid, "accepted")
    for other in store.pending():
        if other.get("source_id") == job_id and other.get("stage") == s["stage"]:
            store.set_status(other["id"], "dismissed")
    return jsonify({"ok": True, "suggestion": store.get(sid),
                    "job": _board_card(job, by_job.get(job_id, []), date.today()),
                    "due_count": due, "suggestions": store.pending()})


@app.post("/api/inbox/suggestions/<sid>/dismiss")
@_guard
def inbox_suggestion_dismiss(sid):
    """"Not this": the suggestion is kept as dismissed so the same mail never asks again."""
    store = _suggestion_store()
    s = store.get(sid)
    if s is None:
        return jsonify({"error": "No such suggestion."}), 404
    store.set_status(sid, "dismissed")
    return jsonify({"ok": True, "suggestion": store.get(sid), "suggestions": store.pending()})


@app.post("/api/tracker/<path:sid>/draft-email")
@_guard
def tracker_draft_email(sid):
    """Draft one email for a tracked job, for the PERSON to send (CLAUDE.md §5). Body:
    {type: inbox.drafts.DRAFT_TYPES, contact_id?}. Returns the editable subject and body, the
    recipient (the contact's email when there is one), and the Gmail compose link. Nothing is
    sent and no Gmail draft is created."""
    from inbox.drafts import DRAFT_TYPES, draft_email, gmail_compose_url
    body = request.json or {}
    kind = str(body.get("type") or "").strip()
    if kind not in DRAFT_TYPES:
        return jsonify({"error": f"type must be one of {', '.join(DRAFT_TYPES)}"}), 400
    w = _watchlist()
    try:
        job = w.board_job(sid)
        if job is None or not job.get("stage"):
            return jsonify({"error": "That job is not on your board."}), 404
        contact = None
        if body.get("contact_id") not in (None, ""):
            try:
                cid = int(body.get("contact_id"))
            except (TypeError, ValueError):
                return jsonify({"error": "contact_id must be a number"}), 400
            contact = next((c for c in w.contacts(sid) if c["id"] == cid), None)
            if contact is None:
                return jsonify({"error": "No such contact on that job."}), 404
    finally:
        w.close()
    try:
        out = draft_email(_make_llm(), kind, job, _saved_full_profile(), contact=contact,
                          stage=job.get("stage") or "")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    out["gmail_url"] = gmail_compose_url(out["to"], out["subject"], out["body"])
    out["source_id"] = sid
    return _nostore(jsonify({"ok": True, **out}))


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
        missing = _aa_missing_details()      # the same wall the run pre-flights: say what is missing
        if missing:
            return {"ok": False, "reason": _aa_missing_reason(missing)}
        _autofill_saved_project_dates()
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

    def act(data: str) -> dict:
        # The Autopilot's batch buttons (ap:...) and the batch review's (approve:/skip:/approveall).
        return _autopilot_act(data) if str(data or "").startswith("ap:") else batch.act(data)
    return NotifyHub(state, AlertEngine(state, match_fn=_alert_match_score),
                     NotifyService(None, _RecordActions()),
                     batch_callback=act, job_actions=_JobAlertActions(),
                     review=_cv_review(state), ask_callback=_agent_ask_event)


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
    profile = _match_profile()
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
            if n % NOTIFY_SCAN_EVERY == 0:
                try:
                    _autopilot_kick()     # whether or not Telegram is connected
                except Exception:   # noqa: BLE001
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
    _load_bundled_depth(db)     # year rows, PERM by year, LCA wages (bundled)
    lca = _sync_lca_bundle(db)  # and the hosted LCA bundle, when a data host is set
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
                    "errors": errors, "snapshot": snapshot, "lca": lca, "note": note, **stats})


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


# Per-employer depth bundles (2026-10). All optional: a missing file is a quiet no-op and the
# badges keep working on the totals. None means "resolve under ROOT at call time" so the
# packaged app's relocated ROOT and a test's monkeypatch both work.
BUNDLED_H1B_HISTORY = None     # seed/h1b_history.csv.gz (scripts/build_h1b_history_bundle.py)
BUNDLED_PERM_YEARS = None      # config/perm_by_year.csv.gz (scripts/build_perm_bundle.py)
BUNDLED_LCA_DIR = None         # dist-data/lca/ (scripts/import_lca_disclosure.py)


def _load_bundled_depth(db) -> dict:
    """Load the year-by-year H-1B history, the PERM-by-year list and the LCA wage bundle
    when the store has none of each. Each is idempotent (SET / MAX per key, the LCA bundle
    by version), prints a failure and never raises."""
    from sourcing.lca import load_lca_bundle
    from sourcing.sponsors import read_seed_rows
    out = {"history": 0, "perm_years": 0, "lca": "absent"}
    hist = Path(BUNDLED_H1B_HISTORY) if BUNDLED_H1B_HISTORY else ROOT / "seed" / "h1b_history.csv.gz"
    try:
        if hist.exists() and not db.h1b_year_fiscal_years():
            out["history"] = db.ingest_h1b_year_rows(read_seed_rows(hist))
            if out["history"]:
                db.set_meta("history_updated_at", _now_iso())
    except Exception:
        traceback.print_exc()
    perm_years = Path(BUNDLED_PERM_YEARS) if BUNDLED_PERM_YEARS else ROOT / "config" / "perm_by_year.csv.gz"
    try:
        if perm_years.exists() and not db.perm_year_fiscal_years():
            out["perm_years"] = db.ingest_perm_year_counts(read_seed_rows(perm_years))
    except Exception:
        traceback.print_exc()
    lca_dir = Path(BUNDLED_LCA_DIR) if BUNDLED_LCA_DIR else ROOT / "dist-data" / "lca"
    try:
        out["lca"] = load_lca_bundle(db, lca_dir).get("status", "absent")
    except Exception:
        traceback.print_exc()
    return out


def _sync_lca_bundle(db) -> dict:
    """Fetch SponsorJobs' LCA wage bundle from TAILOR_DATA_URL ({base}/lca/latest.json +
    wages.csv.gz + flags.csv.gz) when set; applied once per version. Quiet when unset or
    unreachable, like the H-1B snapshot."""
    base = os.environ.get("TAILOR_DATA_URL", "").strip()
    if not base:
        return {"status": "disabled"}
    try:
        from sourcing.lca import sync_lca_bundle
        return sync_lca_bundle(db, base)
    except Exception as exc:    # noqa: BLE001
        traceback.print_exc()
        return {"status": "unreachable", "reason": str(exc)[:200]}


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


# ---------------------------------------------------------------- saved search strategies
# A named set of Jobs-page filters, kept in the local db. Three jobs: apply it in one click, open
# the same search on the other boards (plain URLs the browser opens; nothing is driven or read
# there, see CLAUDE.md section 7), and the digest: what arrived on OUR feed since the person last
# looked at this strategy. Pure logic lives in sourcing/strategies.py; these routes only glue it
# to the store and the board.
def _strategies():
    from sourcing.strategies import StrategyStore
    return StrategyStore(DB_PATH)


def _strategy_with_counts(store, strategies: list[dict], rows: list[dict] | None = None) -> list[dict]:
    """Attach new_count + newest_at (the digest's headline numbers) to each strategy row."""
    from sourcing.strategies import new_since
    if rows is None:
        rows = _board_rows()
    out = []
    for st in strategies:
        fresh = new_since(rows, st)
        st = dict(st)
        st["new_count"] = len(fresh)
        st["newest_at"] = str(fresh[0].get("first_seen")) if fresh else None
        out.append(st)
    return out


@app.get("/api/strategies")
@_guard
def strategies_list():
    """Every saved strategy (default first, then by name), each with its digest count unless
    counts=0 (a cheap list for the editor)."""
    store = _strategies()
    try:
        items = store.list()
        if request.args.get("counts", "1") != "0" and items:
            items = _strategy_with_counts(store, items)
        return jsonify({"strategies": items})
    finally:
        store.close()


@app.post("/api/strategies")
@_guard
def strategies_create():
    body = request.json or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "a strategy needs a name"}), 400
    store = _strategies()
    try:
        st = store.create(name, body.get("filters") or {}, bool(body.get("is_default")))
        return jsonify({"ok": True, "strategy": st})
    finally:
        store.close()


@app.get("/api/strategies/<int:sid>")
@_guard
def strategies_get(sid: int):
    store = _strategies()
    try:
        st = store.get(sid)
        if st is None:
            return jsonify({"error": "no such strategy"}), 404
        return jsonify({"strategy": st})
    finally:
        store.close()


@app.route("/api/strategies/<int:sid>", methods=["POST", "PUT", "PATCH"])
@_guard
def strategies_update(sid: int):
    """Rename, replace the filters, or flip default. Only the keys sent are changed."""
    body = request.json or {}
    store = _strategies()
    try:
        st = store.update(sid, name=body.get("name"),
                          filters=body.get("filters") if "filters" in body else None,
                          is_default=(bool(body["is_default"]) if "is_default" in body else None))
        if st is None:
            return jsonify({"error": "no such strategy"}), 404
        return jsonify({"ok": True, "strategy": st})
    finally:
        store.close()


@app.route("/api/strategies/<int:sid>", methods=["DELETE"])
@_guard
def strategies_delete(sid: int):
    store = _strategies()
    try:
        if not store.delete(sid):
            return jsonify({"error": "no such strategy"}), 404
        return jsonify({"ok": True})
    finally:
        store.close()


@app.post("/api/strategies/<int:sid>/default")
@_guard
def strategies_default(sid: int):
    store = _strategies()
    try:
        st = store.set_default(sid)
        if st is None:
            return jsonify({"error": "no such strategy"}), 404
        return jsonify({"ok": True, "strategy": st})
    finally:
        store.close()


@app.get("/api/strategies/<int:sid>/new")
@_guard
def strategies_new(sid: int):
    """The digest: roles on our feed first seen after this strategy's last_seen that match its
    filters (newest first, first 30 rows + the full count). Reading it does NOT mark the strategy
    seen; POST /seen does, once the person has actually looked."""
    from sourcing.strategies import digest
    store = _strategies()
    try:
        st = store.get(sid)
        if st is None:
            return jsonify({"error": "no such strategy"}), 404
        d = digest(_board_rows(), st, limit=30)
        saved = _saved_ids()
        d["jobs"] = [dict(j, saved=(j.get("source_id") in saved)) for j in d["jobs"]]
        return jsonify(d)
    finally:
        store.close()


@app.post("/api/strategies/<int:sid>/seen")
@_guard
def strategies_seen(sid: int):
    """The person has looked at this strategy's results: new arrivals are counted from now on."""
    store = _strategies()
    try:
        st = store.mark_seen(sid)
        if st is None:
            return jsonify({"error": "no such strategy"}), 404
        return jsonify({"ok": True, "strategy": st})
    finally:
        store.close()


@app.get("/api/strategies/<int:sid>/open-urls")
@_guard
def strategies_open_urls(sid: int):
    """The same search as plain URLs on LinkedIn, Indeed, Glassdoor, Handshake and Google Jobs.
    The browser opens them in tabs; this app never loads, clicks or reads those pages."""
    from ui.boardlinks import strategy_open_urls
    store = _strategies()
    try:
        st = store.get(sid)
        if st is None:
            return jsonify({"error": "no such strategy"}), 404
        return jsonify({"strategy_id": sid, "urls": strategy_open_urls(st)})
    finally:
        store.close()


# TODO(email digest): a daily email of each strategy's new arrivals would hook in here, reusing
# _strategy_with_counts over the board rows. Not built: v1 is local-first with no mail sender of
# our own (the inbox module reads the person's own mailbox; it never sends bulk mail).


def _feed_detail_response(job: dict):
    """The /api/jobs/detail payload for a static-feed row (same shape as the local-store path).
    The MATCH is computed locally from the user's private profile."""
    from sourcing.ats import salary_from_text
    from sourcing.filters import salary_insight
    jd = job.get("jd_text") or ""
    if not (job.get("salary") or "").strip():
        job["salary"] = salary_from_text(jd)
    return jsonify({"job": job, "jd": jd, "match": _job_match(jd, job.get("title", "")),
                    "terms": _jd_terms(jd),
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
    from autoapply import KnownContext, applicant_from_record, build_fill_plan, plan_known, resolve_plan
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
    prof = data.get("render_profile") or data.get("profile") or {}
    if not (prof.get("identity") or {}).get("name"):
        prof = _saved_full_profile() or prof             # fall back to the saved profile's identity
    applicant = applicant_from_record({"profile": prof})
    if _application_email():
        applicant["email"] = _application_email()       # the alias, when the person took one
    resume = _resume_upload_path(rec) if rec else ""   # named after the person, not cv-<id>.pdf
    # The person's own answers first (work authorization, the answer library, this record's
    # drafted screening answers, the cover letter, contact fields); the model only for what is
    # left, behind the fact gate (autoapply/planner.py). None of these answers reach the model.
    lib = _answer_lib()
    try:
        ctx = KnownContext(work_auth=_work_auth(), library=lib,
                           drafted=[x for x in (data.get("screening") or []) if isinstance(x, dict)],
                           cover_letter=str(data.get("cover_letter") or ""), applicant=applicant,
                           resume_path=resume, job_country=_job_country(data), company=company,
                           eeo_decline=bool(_load_prefs().get("eeo_decline")))
        res = plan_known(fields, ctx)
        if res.rest:
            try:
                llm = _make_llm()
            except Exception:                            # no model: what we know still fills
                llm = None
            if llm is not None:
                ai = build_fill_plan(res.rest, llm, role=role, company=company, jd=jd)
                res.actions.extend(ai)
        for k in res.used_library:
            lib.mark_used(k)
        for q, a in res.drafted_used:                    # a generic drafted answer, kept to confirm
            lib.save(q, a, "drafted", company=company)
    finally:
        lib.close()
    plan = res.actions
    if not plan:
        return jsonify({"ok": False, "needs_assist": True,
                        "reason": "Could not plan this form confidently. Fill it in and I'll help.",
                        "left": [{"ref": f.ref, "label": f.label} for f in res.left]})
    ops = resolve_plan(plan, applicant, resume_path=resume)
    return jsonify({"ok": True, "filled": len(ops),
                    "ops": [{"ref": o.ref, "op": o.op, "text": o.text, "path": o.path,
                             "sensitive": o.is_sensitive, "from": o.origin} for o in ops],
                    "left": [{"ref": f.ref, "label": f.label} for f in res.left]})


def _job_country(data: dict) -> str:
    """The country of the job, from its location (US when it does not say)."""
    from autoapply import workauth as _wa
    sj = data.get("source_job") if isinstance(data.get("source_job"), dict) else {}
    loc = " ".join(str(x or "") for x in (data.get("location"), sj.get("location"), data.get("job_location")))
    if not loc.strip():
        return "US"
    cc = _wa.question_country(loc, "")
    return cc or "US"


# ---- the apply agent (docs/apply-agent.md): the brain, portal accounts, the host fence --------
# The shell drives the pane; these routes decide the next click, keep the per-employer portal
# accounts (password never leaves this process except into the pane), and name the hosts a run
# may visit. Same honest gate as /api/autoapply/plan: never on a bot-prohibited board.

def _portal_cred_name(host: str) -> str:
    """PORTAL_<host with dots as underscores, uppercased>: one credential per employer portal."""
    return "PORTAL_" + re.sub(r"[^a-z0-9]", "_", str(host or "").lower()).upper()


def _portal_account(host: str) -> dict | None:
    """{host, email, password} for a portal account the agent created, or None."""
    host = str(host or "").strip().lower()
    if not host:
        return None
    raw = _cred(_portal_cred_name(host))
    if not raw or "|" not in raw:
        return None
    email, _, password = raw.partition("|")
    if not (email.strip() and password):
        return None
    return {"host": host, "email": email.strip(), "password": password}


def _portal_password() -> str:
    """16 characters: letters and digits plus two symbols from a set every portal accepts."""
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    chars = [secrets.choice(alphabet) for _ in range(14)] + [secrets.choice("!@#$%&*?") for _ in range(2)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _agent_placeholders(applicant: dict, prof: dict, portal: dict | None) -> tuple[list[str], dict]:
    """The placeholder NAMES the model may type, and the values they resolve to (local only):
    the contact keys the profile has, the school for a typeahead, the portal account if any."""
    from autoapply.fields import APPLICANT_KEYS
    values = {k: v for k, v in (applicant or {}).items() if k in APPLICANT_KEYS and str(v).strip()}
    edu = prof.get("education") if isinstance(prof.get("education"), list) else []
    school = next((str(e.get("school") or "").strip() for e in edu
                   if isinstance(e, dict) and str(e.get("school") or "").strip()), "")
    if school:
        values["school"] = school
    names = [k for k in APPLICANT_KEYS if k in values] + (["school"] if school else [])
    if portal:
        names += ["portal_email", "portal_password"]
    # The "please explain" box after a sponsorship question: one sentence from the saved profile.
    from autoapply import workauth as _wa
    wnote = _wa.note(_work_auth())
    if wnote:
        values["work_auth_note"] = wnote
        names.append("work_auth_note")
    return names, values


def _known_answers_for(labels: list, rec, data: dict, applicant: dict) -> dict:
    """{label: answer} for the open fields the person's own material already answers, through
    the same planner the fill route uses (work authorization, the answer library, this record's
    drafted answers, the profile). Nothing is invented: a field nothing answers stays out.

    Never a self-identification field (gender, race, Hispanic or Latino, veteran, disability):
    these fields reach the brain as text with no choices, where a saved "No" or "Asian" is only
    half an answer and the standing decline cannot be honored. They go to the agent's ask
    instead, which reads the page's own choices and picks the matching one, or the decline."""
    from autoapply import KnownContext, plan_known, resolve_plan
    from autoapply.asks import is_eeo
    from autoapply.fields import FormField
    from autoapply.planner import residence_withheld
    labels = [lab for lab in labels if not is_eeo(lab)]
    if not labels:
        return {}
    fields = [FormField(ref=f"l{i}", label=lab, type="text") for i, lab in enumerate(labels)]
    lib = _answer_lib()
    try:
        ctx = KnownContext(work_auth=_work_auth(), library=lib,
                           drafted=[x for x in ((data or {}).get("screening") or []) if isinstance(x, dict)],
                           cover_letter="", applicant=applicant, resume_path="",
                           job_country=_job_country(data or {}), company=(rec.get("company") if rec else "") or "",
                           eeo_decline=bool(_load_prefs().get("eeo_decline")))
        res = plan_known(fields, ctx)
        ops = resolve_plan([a for a in res.actions if a.op in ("fill", "select", "check")], applicant)
    except Exception:          # noqa: BLE001 - a planner hiccup never blocks the step
        return {}
    finally:
        lib.close()
    by_ref = {f.ref: f.label for f in fields}
    out = {}
    for o in ops:
        if o.text and o.ref in by_ref:
            out[by_ref[o.ref]] = str(o.text)
    # The person's own answer to this exact question (an earlier "ask") wins for anything the
    # planner left: a work-authorization question answered on the bar is answered, even when
    # the structured work-auth profile is still empty.
    lib = _answer_lib()
    wa = _work_auth()
    try:
        for lab in labels:
            if lab in out or residence_withheld(lab, wa):
                continue
            hit = lib.lookup(lab)
            if hit and str(hit.get("answer") or "").strip():
                out[lab] = str(hit["answer"]).strip()
    finally:
        lib.close()
    return out


# ---- "Ask the person": a question the agent cannot answer goes to the bar and to Telegram, the
# run waits for the answer (up to the caller's timeout), the answer is saved for every later
# application, and the run goes on. One registry, answered from either side first. Several open
# questions on one page go out as ONE numbered message, and each keeps its number until all are
# answered. A typed reply is read by one small model call (autoapply/asks.py) that keeps only the
# part answering each question, and answers a question back ("what are the options?") instead
# of saving it. Nothing is saved raw, and a slash command is never an answer.
_ASKS: dict = {}
_ASKS_LOCK = threading.Lock()


def _ask_items(ask: dict) -> list:
    """The ask's questions as [{question, options, all_options, partial, answer}] (the older
    single-question shape too)."""
    if "items" not in ask:
        ask["items"] = [{"question": ask.get("question", ""), "options": list(ask.get("options") or []),
                         "answer": ask.get("answer")}]
    return ask["items"]


def _ask_done(ask: dict) -> bool:
    return all(it.get("answer") for it in _ask_items(ask))


def _ask_got_it(items: list) -> str:
    if len(items) == 1:
        return f"Got it: {items[0]['answer']}."
    return "Got it: " + "; ".join(f"{n}. {it['answer']}" for n, it in enumerate(items, start=1)) + "."


def _ask_set(ask: dict, i: int, value: str):
    items = _ask_items(ask)
    items[i]["answer"] = value
    if len(items) == 1:
        ask["answer"] = value                      # the older single-question shape


def _ask_open_items(items: list) -> list:
    """The questions still open, each with the number the person saw (kept, never renumbered)."""
    return [{"n": i + 1, "question": it["question"], "options": list(it.get("options") or []),
             "all_options": list(it.get("all_options") or it.get("options") or []),
             "partial": bool(it.get("partial"))}
            for i, it in enumerate(items) if not it.get("answer")]


def _ask_reader():
    """The model that reads a reply, or None when there is none (the plain reading then
    answers). autoapply/asks.py bounds the call; the broker client also gets one short try
    instead of two of 120 seconds, so a stalled read never holds the person's reply."""
    try:
        llm = _make_llm()
    except Exception:          # noqa: BLE001 - no model: the deterministic reading
        return None
    if isinstance(getattr(llm, "timeout", None), (int, float)):
        llm.timeout = min(llm.timeout, 20)
    return llm


def _ask_take_reply(aid: str, text: str) -> tuple[str, bool]:
    """Read a typed reply to an open ask. Returns (the message to send back, done). Only the
    part of the reply that answers each question is kept; a question back gets the options."""
    from autoapply import asks as AK
    text = str(text or "").strip()[:1500]
    with _ASKS_LOCK:
        ask = _ASKS.get(aid)
        if not ask or not ask.get("open", True):
            return "That question is no longer open.", False
        pending = _ask_open_items(_ask_items(ask))
        if pending and text and ask.get("kind") == "terms":
            # Consent is read strictly, never by a model: a choice's words or number, yes or no.
            choice = AK.read_terms_reply(text)
            if not choice:
                return "Tap or type one: " + ", ".join(AK.TERMS_CHOICES) + ".", False
            _ask_set(ask, 0, choice)
            return f"Got it: {choice}.", True
    if not pending or not text:
        return ("", not pending)
    got = AK.read_reply(_ask_reader(), pending, text)
    with _ASKS_LOCK:
        ask = _ASKS.get(aid)
        if not ask or not ask.get("open", True):
            return "That question is no longer open.", False
        items = _ask_items(ask)
        tag = (lambda n: f"{n}. ") if len(items) > 1 else (lambda n: "")
        took = []
        for j, val in got["answers"].items():
            i = pending[j]["n"] - 1
            if not items[i].get("answer"):
                _ask_set(ask, i, val)
                took.append(i)
        if _ask_done(ask):
            return _ask_got_it(items), True
        left = _ask_open_items(items)
        if took:
            msg = ("Got it: " + "; ".join(tag(i + 1) + items[i]["answer"] for i in sorted(took))
                   + ". Still open: " + "; ".join(tag(x["n"]) + x["question"] for x in left) + ".")
            if got["question"]:                    # they also asked for the choices
                msg += "\n" + AK.options_reply(left)
            return AK.fit_text(msg), False
        if got["question"]:
            return AK.options_reply(left), False
        if got.get("error"):
            # The reader failed or stalled, not the person: it does not count as unclear.
            return "I could not read that just now. Tap a choice, or send just the answer.", False
        ask["unclear"] = int(ask.get("unclear") or 0) + 1
        if ask["unclear"] == 1:
            return AK.unclear_reply(left), False
        return "I still could not tell. Type just the answer, or answer on the page yourself.", False


def _agent_ask_event(kind: str, payload: str):
    """The notify hub's hook: an "ask:<id>:<n>" button tap, or a text reply while a question is
    open. Returns the reply text when the event answered a question, else None. A slash command
    is never an answer: it goes on to the usual routing (None), and /stop, /pause or /skip also
    end the wait, so the run stops instead of sitting out the question. Text is taken only by a
    question whose Telegram message was actually sent."""
    text = str(payload or "").strip()
    with _ASKS_LOCK:
        if kind == "button":
            parts = text.split(":")
            if parts[0] != "ask" or len(parts) < 3:
                return None
            ask = _ASKS.get(parts[1])
            if not ask or _ask_done(ask) or not ask.get("open", True):
                return "That question was already answered."
            items = _ask_items(ask)
            try:
                _ask_set(ask, 0, str(items[0]["options"][int(parts[2])]))
            except (ValueError, IndexError):
                return None
            return f"Got it: {items[0]['answer']}."
        if text.startswith("/"):
            words = text.split()
            cmd = words[0].lstrip("/").split("@", 1)[0].lower()
            arg = words[1] if len(words) > 1 else ""
            if cmd in ("stop", "pause", "skip"):
                for a in _ASKS.values():
                    if a.get("open", True) and not _ask_done(a) and (
                            cmd != "skip" or not arg or str(a.get("record_id") or "") == arg):
                        a["open"] = False
                        a["closed"] = cmd
            return None
        open_asks = [a for a in _ASKS.values()
                     if a.get("open", True) and not _ask_done(a) and a.get("sent", True)]
        if not open_asks:
            return None
        aid = open_asks[-1]["id"]
    msg, _done = _ask_take_reply(aid, text)
    return msg


@app.post("/api/autoapply/agent/ask")
@_guard
def autoapply_agent_ask():
    """Body {question, options?, all_options?, partial?, context?} or {questions: [{...}]}, plus
    {id?, timeout_s?: <=600, record_id?, force_questions?: [question], force?, known_only?}.
    ``options`` are the choices to offer, ``all_options`` every choice the page has (up to 40,
    ``options`` when absent), and ``partial`` says the page has more than were read. First the
    person's own material (autoapply/asks.known_answer: work authorization, the answer library,
    the standing decline choice on EEO questions), except for a question in ``force_questions``
    (the run took a known answer for it and it did not stick; ``force`` means every question).
    What is still open goes to Telegram when a channel is connected (ONE message; buttons only
    for a single question with choices, inside the broker's limits; when the channel refuses
    them, the same question without buttons) and to the bar, and the route waits for the answers
    (/api/autoapply/agent/answer, or a Telegram reply). Answers are saved to the answer library.
    Reply {ok, id, answer, answers: {question: answer}}, or {ok:false, id, reason:"timeout"}, or
    reason "stopped" when /stop or /skip on Telegram ended the wait."""
    from autoapply import asks as AK
    b = request.json or {}
    raw = b.get("questions") if isinstance(b.get("questions"), list) else [b]
    force_all = bool(b.get("force"))
    forced = [" ".join(str(q or "").split())[:300] for q in (b.get("force_questions") or []) if isinstance(q, str)]
    items = []
    for x in raw[:6]:
        if not isinstance(x, dict):
            continue
        q = " ".join(str(x.get("question") or "").split())[:300]
        if not q:
            continue
        given = x.get("all_options") if isinstance(x.get("all_options"), list) else (x.get("options") or [])
        cleaned = AK.clean_options(given, limit=1000)
        every = cleaned[:AK.MAX_ALL]
        it = {"question": q, "options": AK.order_options(q, cleaned), "all_options": every,
              "partial": bool(x.get("partial")) or len(cleaned) > len(every),
              "hint": " ".join(str(x.get("hint") or "").split())[:400],
              "context": " ".join(str(x.get("context") or b.get("context") or "").split())[:120],
              "answer": None, "origin": "", "force": force_all or any(AK.same_question(q, f) for f in forced)}
        dup = next((o for o in items if AK.same_question(o["question"], q)), None)
        if dup is None:
            items.append(it)
            continue
        # The same question twice (a Workday "Error-" link cuts it short): asked once, in full.
        if len(q) > len(dup["question"]):
            dup["question"] = q
        if not dup["all_options"] and every:
            dup.update(options=it["options"], all_options=every, partial=it["partial"])
        dup["force"] = dup["force"] or it["force"]
    if not items:
        return jsonify({"ok": False, "reason": "no-question"}), 400
    timeout = max(5, min(600, int(b.get("timeout_s") or 300)))
    aid = re.sub(r"[^a-z0-9]", "", str(b.get("id") or "").lower())[:16] or secrets.token_hex(4)
    rec = _get_record(b.get("record_id")) if b.get("record_id") not in (None, "") else None
    data = _record_data(rec) if rec else {}
    company = (rec.get("company") if rec else "") or ""
    # The person's own material first: never ask what the profile or the library already says.
    unforced = [it for it in items if not it["force"]]
    if unforced:
        lib = _answer_lib()
        try:
            eeo_decline = bool(_load_prefs().get("eeo_decline"))
            wa, country = _work_auth(), _job_country(data)
            for it in unforced:
                k = AK.known_answer(it["question"], it["all_options"], partial=it["partial"], library=lib,
                                    work_auth=wa, eeo_decline=eeo_decline, job_country=country, hint=it["hint"])
                if k:
                    it["answer"], it["origin"] = k
        finally:
            lib.close()
    answers = lambda: {it["question"]: it["answer"] for it in items if it["answer"]}   # noqa: E731
    if b.get("known_only"):                        # the shell checks first, then shows the bar
        # A self-identification answer that IS one of the page's choices: the shell picks it
        # itself with real clicks (shell-electron/agent.js pickSaved), never through the model.
        select = {it["question"]: it["answer"] for it in items
                  if it["answer"] and AK.is_eeo(it["question"]) and it["answer"] in it["all_options"]}
        return jsonify({"ok": all(it["answer"] for it in items), "id": aid, "known": True,
                        "answers": answers(), "select": select,
                        "open": [it["question"] for it in items if not it["answer"]]})
    if all(it["answer"] for it in items):
        return jsonify({"ok": True, "id": aid, "known": True, "answer": items[0]["answer"], "answers": answers()})
    pending = [it for it in items if not it["answer"]]
    _ask_open(aid, pending, b.get("record_id"))
    _ask_telegram(aid, pending)

    def save_all():
        lib = _answer_lib()
        try:
            for it in items:
                a = str(it["answer"] or "").strip()
                # A question back is never an answer (the reply reader drops it too).
                if a and not it["origin"] and not a.endswith("?"):
                    lib.save(it["question"], a, "you", company=company)
        finally:
            lib.close()

    how = _ask_wait(aid, pending, timeout)
    if how == "answered":
        save_all()
        return jsonify({"ok": True, "id": aid, "answer": items[0]["answer"], "answers": answers()})
    if any(it["answer"] and not it["origin"] for it in items):
        save_all()                                 # what the person did answer is kept
    return jsonify({"ok": False, "id": aid, "reason": how})


def _ask_open(aid: str, pending: list, record_id=None, kind: str = ""):
    """Register an ask in the registry the bar, Telegram and /answer share."""
    with _ASKS_LOCK:
        _ASKS[aid] = {"id": aid, "items": [{"question": it["question"], "options": it["options"],
                                            "all_options": it["all_options"], "partial": it["partial"],
                                            "answer": None} for it in pending],
                      "open": True, "unclear": 0, "at": time.time(), "sent": False, "kind": kind,
                      "record_id": "" if record_id in (None, "") else str(record_id)}
        if len(pending) == 1:
            _ASKS[aid]["question"], _ASKS[aid]["options"], _ASKS[aid]["answer"] = \
                pending[0]["question"], pending[0]["options"], None
        for k in [k for k, a in _ASKS.items() if time.time() - a["at"] > 3600]:
            _ASKS.pop(k, None)


def _ask_telegram(aid: str, pending: list, kind: str = ""):
    """Telegram: one message, and for a single question its choices as buttons (asks.telegram_ask
    keeps them inside docs/notify.md). A channel that refuses the buttons still gets the
    question, its choices numbered in the text, so a person away from the desk always sees it."""
    from autoapply import asks as AK
    sent = False
    try:
        ch = _active_channel()
    except Exception:          # noqa: BLE001 - no Telegram is fine; the bar still asks
        ch = None
    if ch is not None:
        text, buttons = AK.telegram_ask(aid, pending)
        if kind == "terms":                        # the site's own notice, asked as it is
            text = pending[0]["question"] + "\nTap a choice below, or reply 1, 2 or 3."
        try:
            ch.send(text, buttons)
            sent = True
        except Exception:      # noqa: BLE001 - retried without buttons below
            if buttons:
                try:
                    ch.send(AK.prompt_text(pending, list_choices=True, buttons=False), None)
                    sent = True
                except Exception:  # noqa: BLE001 - no Telegram is fine; the bar still asks
                    pass
    with _ASKS_LOCK:
        if aid in _ASKS:
            _ASKS[aid]["sent"] = sent


def _ask_wait(aid: str, pending: list, timeout: float) -> str:
    """Wait for every pending question of the ask to be answered (the bar, a Telegram reply or
    button). Fills each item's "answer" and returns "answered", "stopped" (/stop or /skip on
    Telegram) or "timeout". The ask is closed either way, so a stale question never captures
    a later reply."""
    deadline = time.time() + timeout
    stopped = False
    while time.time() < deadline:
        with _ASKS_LOCK:
            ask = _ASKS.get(aid)
            got = [x.get("answer") for x in _ask_items(ask)] if ask else []
            closed = bool(ask) and ask.get("open", True) is False
        if got and all(got):
            for it, a in zip(pending, got):
                it["answer"] = a
            with _ASKS_LOCK:
                if aid in _ASKS:
                    _ASKS[aid]["open"] = False
            return "answered"
        if closed:                                 # /stop or /skip on Telegram
            stopped = True
            break
        time.sleep(1.0)
    with _ASKS_LOCK:
        ask = _ASKS.get(aid)
        if ask:
            ask["open"] = False
            for it, x in zip(pending, _ask_items(ask)):
                it["answer"] = x.get("answer") or None
    return "stopped" if stopped else "timeout"


@app.post("/api/autoapply/agent/answer")
@_guard
def autoapply_agent_answer():
    """The bar's answer to an open question: {id, answer, exact?, index?}. A tapped option
    (exact) is taken as is; typed text is read like a Telegram reply (only the part answering
    the question is kept; a question back gets the options). Reply {ok, id, done, reply}."""
    from autoapply import asks as AK
    b = request.json or {}
    aid, ans = str(b.get("id") or ""), str(b.get("answer") or "").strip()[:1500]
    q = " ".join(str(b.get("question") or "").split())[:300]
    if not ans:
        return jsonify({"ok": False, "error": "Write an answer first."}), 400
    with _ASKS_LOCK:
        ask = _ASKS.get(aid)
        if not ask and q:                          # the bar knows the question, not the id
            ask = next((a for a in _ASKS.values() if a.get("open", True) and not _ask_done(a)
                        and any(AK.same_question(it["question"], q) for it in _ask_items(a))), None)
        if not ask or not ask.get("open", True):
            return jsonify({"ok": False, "error": "That question is no longer open."}), 404
        aid = ask["id"]
        items = _ask_items(ask)
        if b.get("exact"):
            try:
                i = int(b.get("index") or 0)
            except (TypeError, ValueError):
                i = 0
            if not 0 <= i < len(items):
                return jsonify({"ok": False, "error": "No such question."}), 400
            if ask.get("kind") == "terms" and ans not in AK.TERMS_CHOICES:
                return jsonify({"ok": False, "error": "Pick one of the choices."}), 400
            _ask_set(ask, i, ans[:300])
            done = _ask_done(ask)
            return jsonify({"ok": True, "id": aid, "done": done,
                            "reply": _ask_got_it(items) if done else f"Got it: {ans[:300]}."})
    reply, done = _ask_take_reply(aid, ans)
    return jsonify({"ok": True, "id": aid, "done": done, "reply": reply})


# ---- Terms a site requires to apply (docs/apply-agent.md): a privacy notice, a data privacy
# agreement, terms, a consent to process the application data, an E-Verify acknowledgement. The
# agent accepts one only with the person's consent: their standing choice in Your answers
# (accept_required_terms "always"), else asked on the bar and on Telegram, once per site and
# run. Every acceptance is recorded on the application (terms_accepted) and named in the Ready
# to send summary. Marketing opt-ins are never terms (extension/agent_view.js termsItems).
def _terms_pref() -> str:
    v = str(_load_prefs().get("accept_required_terms") or "ask")
    return v if v in ("ask", "always") else "ask"


def _terms_company(rec, b: dict) -> str:
    from submit.policy import _host
    company = " ".join(str((rec.get("company") if rec else "") or b.get("company") or "").split())[:80]
    return company or _host(str(b.get("url") or ""))


@app.post("/api/autoapply/agent/terms")
@_guard
def autoapply_agent_terms():
    """Body {record_id?, title, url, kind?, company?, check_only?, id?, timeout_s?}. The person's
    decision on accepting a notice the site requires. With the standing choice "always": at once
    {ok, decision:"accept", via:"always"}. Else check_only returns {ok, decision:"ask", question,
    options}; without it the question goes to Telegram (three buttons) and the bar, and the
    route waits: {ok, decision:"accept", via:"all"|"once"} ("all" also sets the standing choice
    to "always") or {ok, decision:"stop"}; {ok:false, reason:"timeout"|"stopped"} when nobody
    answered or /stop ended the wait. Nothing is accepted here: the shell clicks, then records
    the acceptance with /api/autoapply/agent/terms/accepted."""
    from autoapply import asks as AK
    b = request.json or {}
    title = " ".join(str(b.get("title") or "").split())[:80] or "terms"
    rec = _get_record(b.get("record_id")) if b.get("record_id") not in (None, "") else None
    company = _terms_company(rec, b)
    if _terms_pref() == "always":
        return jsonify({"ok": True, "decision": "accept", "via": "always"})
    question = AK.terms_question(company, title)
    if b.get("check_only"):
        return jsonify({"ok": True, "decision": "ask", "question": question, "options": list(AK.TERMS_CHOICES)})
    aid = re.sub(r"[^a-z0-9]", "", str(b.get("id") or "").lower())[:16] or secrets.token_hex(4)
    timeout = max(5, min(600, int(b.get("timeout_s") or 300)))
    pending = [{"question": question, "options": list(AK.TERMS_CHOICES), "all_options": list(AK.TERMS_CHOICES),
                "partial": False, "answer": None}]
    _ask_open(aid, pending, b.get("record_id"), kind="terms")
    _ask_telegram(aid, pending, kind="terms")
    how = _ask_wait(aid, pending, timeout)
    if how != "answered":
        return jsonify({"ok": False, "id": aid, "reason": how})
    decision, via = AK.terms_decision(pending[0]["answer"])
    if via == "all":
        _merge_prefs({"accept_required_terms": "always"})
    return jsonify({"ok": True, "id": aid, "decision": decision, "via": via})


@app.post("/api/autoapply/agent/terms/accepted")
@_guard
def autoapply_agent_terms_accepted():
    """Record that the agent accepted a notice for the person: body {record_id, title, url, kind?,
    via?, company?}. Appended to the application's terms_accepted list (company, title, url,
    host, kind, via, at), at most 20, one entry per title and host. Reply {ok, accepted}."""
    from submit.policy import _host
    b = request.json or {}
    if b.get("record_id") in (None, ""):
        return jsonify({"ok": False, "reason": "no-record"})
    recs = _records()
    try:
        rec = recs.get(int(b.get("record_id")))
        if rec is None:
            return jsonify({"ok": False, "reason": "no-record"}), 404
        url = str(b.get("url") or "")[:500]
        entry = {"company": _terms_company(rec, b), "title": " ".join(str(b.get("title") or "").split())[:80] or "terms",
                 "url": url, "host": _host(url), "kind": str(b.get("kind") or "")[:20],
                 "via": str(b.get("via") or "")[:10], "at": _now_iso()}
        have = [x for x in (_record_data(rec).get("terms_accepted") or []) if isinstance(x, dict)]
        have = [x for x in have if not (x.get("title") == entry["title"] and x.get("host") == entry["host"])]
        data = recs.merge_data(int(rec["id"]), {"terms_accepted": (have + [entry])[-20:]})
    finally:
        recs.close()
    return jsonify({"ok": True, "accepted": data.get("terms_accepted") if isinstance(data, dict) else [entry]})


@app.post("/api/autoapply/agent/step")
@_guard
def autoapply_agent_step():
    """One step of the apply agent's brain (autoapply/brain.py). Body: {record_id, step,
    max_steps, url, lane, view, filled_this_step, left, history}. Reply: {ok, memory, evaluation,
    actions, done, notes}. The actions carry real values for the pane (resolved here, marked
    sensitive, never logged); the model only ever saw placeholder names and a redacted view."""
    from autoapply import applicant_from_record, brain
    from submit.policy import _host, fill_permitted
    b = request.json or {}
    url = str(b.get("url") or "").strip()
    if not fill_permitted(url):
        return jsonify({"ok": False, "reason": "fill-by-hand",
                        "message": "This site is fill-by-hand only. Open it and apply yourself."})
    view = str(b.get("view") or "")
    if not view.strip():
        return jsonify({"ok": False, "reason": "no-view", "message": "The page view is empty."})
    rec = _get_record(b.get("record_id")) if b.get("record_id") not in (None, "") else None
    data = _record_data(rec) if rec else {}
    prof = data.get("render_profile") or data.get("profile") or {}
    if not (prof.get("identity") or {}).get("name"):
        prof = _saved_full_profile() or prof             # fall back to the saved profile's identity
    applicant = applicant_from_record({"profile": prof})
    if _application_email():
        applicant["email"] = _application_email()       # the alias, when the person took one
    portal = _portal_account(_host(url))
    placeholders, values = _agent_placeholders(applicant, prof, portal)
    # A one-time code the shell took from the person's own mailbox (inbox/await): typed by the
    # brain as {{email_code}} on this step only, never seen by the model.
    code = re.sub(r"\D", "", str(b.get("email_code") or ""))[:8]
    if code:
        values["email_code"] = code
        placeholders = placeholders + ["email_code"]
    left = []
    for x in (b.get("left") or [])[:40]:
        left.append(str(x.get("label") or x.get("ref") or "") if isinstance(x, dict) else str(x))
    # A field the engine left open but the person HAS answered (the answer library, work
    # authorization, a drafted screening answer): the brain gets the value as a placeholder
    # next to the field and fills it itself. Workday's "How did you hear about us" is a widget
    # the engine cannot drive but the model can: type, wait, click the suggestion.
    known = _known_answers_for([x for x in left if x.strip()], rec, data, applicant)
    # What the person answered on the bar or Telegram during THIS run (or the decline choice the
    # ask route picked), by the question's words: theirs, so it wins. The engine's label can
    # still carry the page's error round the question ("Error: The field ... is required"),
    # the asked question never does, so both are compared without it.
    from autoapply.answers import clean_label
    given = b.get("answers") if isinstance(b.get("answers"), dict) else {}
    by_clean = {clean_label(k).lower(): v for k, v in given.items() if clean_label(k)}
    for label in [x for x in left if x.strip()]:
        v = " ".join(str(given.get(label) or by_clean.get(clean_label(label).lower()) or "").split())[:300]
        if v:
            known[label] = v
    for n, (label, value) in enumerate(known.items(), start=1):
        key = f"answer_{n}"
        values[key] = value
        placeholders = placeholders + [key]
        left = [f"{x} (type {{{{{key}}}}})" if x == label else x for x in left]
    req = {
        "step": b.get("step") or 1, "max_steps": b.get("max_steps") or 40,
        "url": url, "lane": b.get("lane") or "assisted", "view": view[:14000],
        "left": [x for x in left if x.strip()],
        "history": [h for h in (b.get("history") or [])[-brain.HISTORY_STEPS:] if isinstance(h, dict)],
        "job": {"role": (rec.get("role") if rec else "") or "", "company": (rec.get("company") if rec else "") or ""},
    }
    resume = _resume_upload_path(rec) if rec else ""
    out = brain.step(req, _make_llm(), values, portal, placeholders=placeholders, resume_path=resume)
    out["placeholders"] = placeholders
    return jsonify(out)


@app.post("/api/autoapply/agent/sign")
@_guard
def autoapply_agent_sign():
    """Web Bot Auth headers for one page request the agent makes (docs/apply-agent.md 7): the
    broker holds the signing key and signs {method, authority, path} for this account; the shell
    adds the three headers. {ok:false} when there is no account or the broker declines, and the
    request goes out unsigned, never blocked."""
    from inbox.relay import urllib_fetch
    b = request.json or {}
    authority = str(b.get("authority") or "").strip().lower()[:253]
    if not authority or "/" in authority:
        return jsonify({"ok": False, "reason": "bad-authority"})
    tok = _account_token()
    if not tok:
        return jsonify({"ok": False, "reason": "no-account"})
    out = urllib_fetch(BROKER_URL, tok, timeout=3.0)("POST", "/agent/sign", {
        "method": str(b.get("method") or "GET")[:10], "authority": authority,
        "path": str(b.get("path") or "/")[:2048]}) or {}
    if out.get("error") or not out.get("signature"):
        return jsonify({"ok": False, "reason": str(out.get("error") or "unsigned")})
    return jsonify({"ok": True, "signature_input": out.get("signature_input"), "signature": out.get("signature"),
                    "signature_agent": out.get("signature_agent") or '"https://sponsorjobs.ai"'})


@app.get("/api/autoapply/agent/portal-account")
@_guard
def autoapply_agent_portal_account():
    """Does a portal account exist for this host? {exists, email}. The password never comes
    out of this route: the step route resolves {{portal_password}} itself."""
    from submit.policy import _host
    if request.args.get("include_secret"):
        return jsonify({"error": "The password is never returned. The agent types it itself."}), 400
    host = _host(request.args.get("host") or request.args.get("url") or "")
    acct = _portal_account(host)
    return jsonify({"ok": True, "host": host, "exists": acct is not None,
                    "email": acct["email"] if acct else ""})


@app.post("/api/autoapply/agent/portal-account")
@_guard
def autoapply_agent_portal_account_create():
    """Create (or reuse) the portal account for a host: body {host, email?, create: true}. The
    email is the person's own (recruiters reply to it); the password is generated here and
    stored under PORTAL_<host> in the credential store (App Lock custody when on). Refused when
    "Create portal accounts for me" is off. Returns {ok, email, created}; never the password."""
    from submit.policy import _host, fill_permitted
    b = request.json or {}
    host = _host(str(b.get("host") or b.get("url") or ""))
    if not host or not fill_permitted(host):
        return jsonify({"ok": False, "reason": "fill-by-hand",
                        "message": "No portal account on this site."})
    if not b.get("create"):
        return jsonify({"ok": False, "reason": "no-create", "message": "Send create: true."})
    if not _prefs().get("portal_accounts", True):
        return jsonify({"ok": False, "reason": "portal_accounts_off",
                        "message": "Creating portal accounts is turned off in Auto-apply settings."})
    existing = _portal_account(host)
    if existing:
        return jsonify({"ok": True, "host": host, "email": existing["email"], "created": False})
    email = str(b.get("email") or "").strip() or _application_email()
    if not email:
        prof = _saved_full_profile() or {}
        email = str((prof.get("identity") or {}).get("email") or "").strip()
    if not email or "@" not in email:
        return jsonify({"ok": False, "reason": "no-email",
                        "message": "Add your email to your profile first."})
    _save_cred(_portal_cred_name(host), f"{email}|{_portal_password()}")
    return jsonify({"ok": True, "host": host, "email": email, "created": True})


@app.get("/api/autoapply/agent/allowed-hosts")
@_guard
def autoapply_agent_allowed_hosts():
    """The hosts an apply-agent run may navigate to for this apply URL (submit.policy
    .run_allowed_hosts); the shell stops the run anywhere else. Empty on a prohibited site."""
    from submit.policy import run_allowed_hosts
    return jsonify({"ok": True, "hosts": sorted(run_allowed_hosts(request.args.get("url") or ""))})


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
                          "source_id": job.get("source_id") or "",
                          "location": job.get("location") or ""}   # Auto-apply's dedup key
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


def _aa_records() -> list[dict]:
    """Every tailored record as the picker reads it: its data bag plus company and role."""
    recs = _records()
    try:
        rows = recs.list(limit=10000)
    finally:
        recs.close()
    return [{**_record_data(r), "company": r.get("company") or "",
             "role": r.get("jd_label") or r.get("role") or ""} for r in rows]


def _aa_saved_search() -> dict | None:
    """The person's default saved Jobs search (a strategy), or None."""
    try:
        st = _strategies()
        try:
            return st.default()
        finally:
            st.close()
    except Exception:   # noqa: BLE001 - no saved search is the normal case
        return None


def _aa_candidates(focus: dict) -> tuple[list[dict], dict]:
    """The roles an Auto-apply run may tailor: the jobs the person sees on Jobs (the central
    feed, or the local board) plus their saved jobs, saved first, narrowed by their default
    saved search and today's focus, ordered by fit to the profile. See autoapply/picker.py."""
    from autoapply.picker import candidates
    from notify.alerts import profile_targets
    from sourcing.quality import is_entry_level
    titles = [str(t).strip() for t in (focus.get("titles") or []) if str(t).strip()]
    locations = [str(l).strip() for l in (focus.get("locations") or []) if str(l).strip()]
    sponsor_only = bool(focus.get("sponsor_only"))
    w = _watchlist()
    try:
        saved = _sponsors().tag_jobs(w.list_jobs(order="recent", saved_only=True, limit=5000))
    finally:
        w.close()
    for j in saved:
        j["entry_level"] = is_entry_level(j.get("title", ""))
    board = _board_rows()
    strat = _aa_saved_search()
    profile = _match_profile()
    from autoapply.learning import needs_sponsorship
    needs = needs_sponsorship(_load_prefs())
    skip = tuple(_prefs().get("aa_skip") or [])
    # The prefilter reads every row of the board (~0.7 s on 100k rows), so its order is kept
    # until something it depends on changes: the board itself, the focus, the saved search,
    # the saved jobs, the profile, the skips and company lists, the visa answer.
    key = (id(board), len(board), str(DB_PATH), tuple(titles), tuple(locations), sponsor_only,
           json.dumps((strat or {}).get("filters") or {}, sort_keys=True, default=str),
           tuple(str(j.get("source_id") or "") for j in saved), _aa_profile_sig(profile),
           skip, _LEARN_VER[0], needs)
    hit = _AA_CAND_CACHE.get("v")
    if hit and hit[0] == key and hit[2] is board:
        rows = hit[1]
    else:
        rows = candidates(board, saved, facets=(strat or {}).get("filters") or {},
                          titles=titles, locations=locations, sponsor_only=sponsor_only,
                          targets=profile_targets(profile), learn=_learn_state(),
                          needs_sponsor=needs)
        if skip:
            skipped = set(skip)
            rows = [j for j in rows if str(j.get("source_id") or "") not in skipped]
        _AA_CAND_CACHE["v"] = (key, rows, board)
    meta = {"titles": titles, "locations": locations, "sponsor_only": sponsor_only,
            "search": (strat or {}).get("name") or None, "board": len(board), "saved": len(saved)}
    return rows, meta


_AA_CAND_CACHE: dict = {}
_AA_FIT_CACHE: dict = {}         # (db, profile, source_id) -> fit | None, so a role is scored once
_AA_FIT_MAX = 60000
_AA_SHARDS: "OrderedDict[Path, tuple]" = OrderedDict()   # JD shards on disk, parsed (LRU)
# A parsed shard is ~6 MB of text and a fit is scored once then cached (_AA_FIT_CACHE), so a few
# shards are plenty: 32 of them held ~200 MB (2026-10-21, 8 GB laptops).
_AA_SHARDS_MAX = 4
_AA_SHARDS_LOCK = threading.Lock()
_AA_WARM = {"thread": None, "queue": [], "ver": 0}
_AA_WARM_LOCK = threading.Lock()
UPNEXT_BUDGET_S = 0.8            # Up next answers inside ~1 s; roles left are scored after


def _aa_profile_sig(profile: dict | None = None) -> str:
    """A short fingerprint of the saved profile: a cached fit is only reused for the profile it
    was scored against."""
    import hashlib
    if profile is None:
        profile = _match_profile()
    blob = json.dumps(profile or {}, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:16]


def _aa_jd_on_disk(sid: str) -> str | None:
    """A feed role's description when its JD shard is already on this machine (any age: a
    day-old description scores the same), read without touching the network. None otherwise."""
    if not sid or not _feed_serving():
        return None
    from sourcing.ats import html_to_text
    from sourcing.feedfile import JD_DIR, loads_maybe_gz, shard_for
    path = _static_feed().cache_dir / JD_DIR / f"{shard_for(sid)}.json.gz"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _AA_SHARDS_LOCK:
        hit = _AA_SHARDS.get(path)
        if hit and hit[0] == mtime:
            _AA_SHARDS.move_to_end(path)
    if not hit or hit[0] != mtime:
        try:
            data = loads_maybe_gz(path.read_bytes()) or {}
        except Exception:                            # noqa: BLE001 - a corrupt shard = no JD
            data = {}
        hit = (mtime, data if isinstance(data, dict) else {})
        with _AA_SHARDS_LOCK:
            _AA_SHARDS[path] = hit
            while len(_AA_SHARDS) > _AA_SHARDS_MAX:
                _AA_SHARDS.popitem(last=False)
    jd = hit[1].get(sid)
    return html_to_text(jd).strip() if jd else None


def _aa_scorer(rows: list[dict], *, deadline: float | None = None, network: bool = False):
    """The fit scorer the picker calls (autoapply.picker.rank): a role scored before (for this
    profile) answers from memory; one whose description is on this machine (on the row, in the
    local store, or in a JD shard on disk) is scored now with the same fit the Jobs detail shows
    (_aa_fit); one that would need a download answers PENDING unless ``network`` is on (a run).
    Past ``deadline`` every unscored role is PENDING, so Up next stays quick."""
    from autoapply.picker import POOL, PENDING
    sig = _aa_profile_sig()
    feed = _feed_serving()
    need = [str(j.get("source_id") or "") for j in rows[:POOL * 2]
            if j.get("source_id") and not (j.get("jd_text") or "").strip()
            and (str(DB_PATH), sig, str(j.get("source_id"))) not in _AA_FIT_CACHE]
    local: dict = {}
    if need:
        w = _watchlist()
        try:
            local = w.get_jd_texts(need) or {}
        except Exception:                            # noqa: BLE001 - scored from the feed instead
            local = {}
        finally:
            w.close()

    def score(job: dict):
        sid = str(job.get("source_id") or "")
        key = (str(DB_PATH), sig, sid)
        if sid and key in _AA_FIT_CACHE:
            return _AA_FIT_CACHE[key]
        if deadline is not None and time.monotonic() > deadline:
            return PENDING
        if not (job.get("jd_text") or "").strip():
            jd = (local.get(sid) or "").strip() or _aa_jd_on_disk(sid)
            if jd:
                job["jd_text"] = jd
            elif feed and not network:
                return PENDING
        pct = _aa_fit(job)
        if sid:
            if len(_AA_FIT_CACHE) > _AA_FIT_MAX:
                _AA_FIT_CACHE.clear()
            _AA_FIT_CACHE[key] = pct
        return pct
    return score


def _aa_warm(jobs: list[dict]) -> None:
    """Score Up next's waiting roles in the background (downloading their JD shards), then let
    the next Up next request see them. One worker; a new request just adds to its list."""
    with _AA_WARM_LOCK:
        queued = {str(j.get("source_id") or "") for j in _AA_WARM["queue"]}
        _AA_WARM["queue"].extend(dict(j) for j in jobs
                                 if str(j.get("source_id") or "") not in queued)
        th = _AA_WARM["thread"]
        if th is not None and th.is_alive():
            return
        th = threading.Thread(target=_aa_warm_run, name="aa-upnext-warm", daemon=True)
        _AA_WARM["thread"] = th
    th.start()


def _aa_warm_bump() -> None:
    with _AA_WARM_LOCK:
        _AA_WARM["ver"] += 1
    _AA_NEXT_CACHE.pop("v", None)


def _aa_warm_run() -> None:
    """Drain the waiting list: download each missing JD shard once (a few at a time), score
    every role whose description is then on disk, and only then ask companies' own boards for
    the few descriptions the shards lack (one request each, the slow part)."""
    from concurrent.futures import ThreadPoolExecutor
    from autoapply.picker import PENDING
    try:
        local = _aa_scorer([], network=False)
        net = _aa_scorer([], network=True)
    except Exception:                                # noqa: BLE001 - Up next just stays partial
        traceback.print_exc()
        return
    while True:
        with _AA_WARM_LOCK:
            batch, _AA_WARM["queue"] = _AA_WARM["queue"], []
        if not batch:
            break
        if _feed_serving():
            from sourcing.feedfile import JD_DIR, shard_for
            sf = _static_feed()
            first: dict = {}
            for j in batch:
                sid = str(j.get("source_id") or "")
                if sid:
                    first.setdefault(shard_for(sid), sid)
            missing = [sid for sh, sid in first.items()
                       if not (sf.cache_dir / JD_DIR / f"{sh}.json.gz").exists()]
            if missing:
                with ThreadPoolExecutor(max_workers=4) as ex:
                    list(ex.map(sf.jd, missing))
        later = []
        for j in batch:
            try:
                if local(j) == PENDING:
                    later.append(j)
            except Exception:                        # noqa: BLE001 - one bad posting, keep going
                pass
        _aa_warm_bump()
        for n, j in enumerate(later, 1):
            try:
                net(j)
            except Exception:                        # noqa: BLE001
                pass
            if n % 20 == 0:
                _aa_warm_bump()
        _aa_warm_bump()


def _aa_fit(job: dict) -> int | None:
    """The same fit the Jobs detail shows for this role (0..100), or None when there is no
    description or it names no skills to match. Fills job["jd_text"] for the tailoring."""
    from notify.alerts import match_percent
    sid = job.get("source_id") or ""
    jd = (job.get("jd_text") or "").strip()
    if not jd and sid:
        w = _watchlist()
        try:
            jd = ((w.get_job(sid) or {}).get("jd_text") or "").strip()
        finally:
            w.close()
    if not jd and sid and _feed_serving():
        fj = _feed_job_with_jd(sid)
        jd = ((fj or {}).get("jd_text") or "").strip()
    if not jd:
        return None
    job["jd_text"] = jd
    m = _job_match(jd, job.get("title") or "")
    if not m or not m.get("total"):
        return None
    return match_percent(m)


def _aa_min_fit(body: dict | None = None) -> int:
    from autoapply.picker import clamp_min_fit
    p = _prefs()
    if body and body.get("min_fit") is not None:
        v = clamp_min_fit(body.get("min_fit"), p["aa_min_fit"])
        if v != p["aa_min_fit"]:
            _set_prefs({"aa_min_fit": v})
        return v
    return clamp_min_fit(p["aa_min_fit"])


def _aa_has_profile() -> bool:
    snap = _memory().load("default") or {}
    return bool((snap.get("profile") or {}).get("experience"))


_AA_NO_PROFILE = {"error": "Add your resume first, then SponsorJobs can build these for you.",
                  "action": "add_resume"}


def _aa_missing_details() -> list[str]:
    """The HARD details the saved profile still lacks, worded as the questions
    build_from_saved would ask (ui/session._missing_skeleton, critical tier). A project's
    dates and location are SOFT and never stop a run: they build as undated. An unattended
    build has no one to answer them, so without this check every role came back "needs a
    detail we don't have" and the run queued nothing while the page blamed the fit floor
    (measured 2026-10-26: two undated projects, 0 queued, 284 "below your fit floor").
    Checked BEFORE any role is counted."""
    return [x["q"] for x in _profile_missing_slots(include_soft=False)]


def _aa_missing_error(missing: list[str]) -> dict:
    """The 400 body for a run or preview the saved profile cannot support yet: the plain
    questions, and the action the page turns into an "Add the details" button."""
    n = len(missing)
    return {"error": f"Auto-apply needs {n} detail{'' if n == 1 else 's'} first.",
            "missing": list(missing), "action": "complete_profile"}


def _aa_missing_reason(missing: list[str]) -> str:
    """The same cause in one Telegram-sized line, for the alert buttons (and a paused
    autopilot): "your profile is missing 2 details (the dates for ...). Add them in SponsorJobs"."""
    n = len(missing)
    return (f"your profile is missing {n} detail{'' if n == 1 else 's'} ({', '.join(missing)}). "
            "Add them in SponsorJobs")


@app.get("/api/autoapply/preview")
@_guard
def autoapply_preview():
    """What a run would draw from, before it runs: how many roles match (not already queued
    or applied), how many of those the shipped submission policy can submit automatically,
    the saved search in use, and the fit floor. No JD is fetched and nothing is tailored."""
    from autoapply.picker import reach, taken_index, taken_status
    from submit.policy import submission_policy
    a = request.args
    focus = {"titles": _split(a.get("titles")), "locations": _split(a.get("locations")),
             "sponsor_only": a.get("sponsor_only") in ("1", "true", "on")}
    missing = _aa_missing_details()         # a run would stall on these, say so now
    if missing:
        return jsonify(_aa_missing_error(missing)), 400
    rows, meta = _aa_candidates(focus)
    idx = taken_index(_aa_records())
    fresh = [j for j in rows if taken_status(j, idx) is None]
    return jsonify({"ok": True, "has_profile": _aa_has_profile(), "matches": len(fresh),
                    "taken": len(rows) - len(fresh), "reach": reach(fresh, submission_policy),
                    "search": meta["search"], "board": meta["board"], "min_fit": _aa_min_fit()})


@app.post("/api/autoapply/run")
@_guard
def autoapply_run():
    """Auto-apply (the paid autopilot, CLAUDE.md §4c): tailor the best-fit roles from the jobs
    the person sees on Jobs (saved jobs first), from the SAVED profile, no per-role input, and
    queue each package for review. Skips roles that already have a package and roles below the
    person's fit floor. Submission stays on the existing guarded path (verified allowlist +
    opt-in), so this run never submits on its own; it fills the review queue."""
    body = request.json or {}
    count = max(1, min(int(body.get("count") or 5), 10))
    if not _aa_has_profile():
        return jsonify(_AA_NO_PROFILE), 400
    # Pre-flight the saved profile before ranking, counting, or tailoring a single role: an
    # unattended build cannot ask for a missing date, so the run would queue nothing.
    missing = _aa_missing_details()
    if missing:
        return jsonify(_aa_missing_error(missing)), 400
    _autofill_saved_project_dates()     # soft gaps: fill what GitHub knows, build the rest undated
    min_fit = _aa_min_fit(body)
    if not _AA_RUN_LOCK.acquire(blocking=False):
        return jsonify({"error": "A run is already tailoring. Wait for it to finish, then try "
                                 "again.", "busy": True}), 409
    try:
        payload, status = _aa_run(count, min_fit, body.get("focus") or {})
    finally:
        _AA_RUN_LOCK.release()
    return jsonify(payload), status


# One run at a time: a manual Tailor & queue and the Autopilot never tailor side by side (they
# would pick the same Up next roles and double the packages). Held for the whole run.
_AA_RUN_LOCK = threading.Lock()


def _aa_run(count: int, min_fit: int, focus: dict, *, ctrl=None) -> tuple[dict, int]:
    """The Auto-apply run itself, shared by the route and the Autopilot scheduler: the same
    ranking Up next shows, the same fit floor, focus and skips, never a role that already has a
    package. Returns (payload, http_status); an error payload carries "error". The caller holds
    _AA_RUN_LOCK."""
    from autoapply.picker import TIME_BUDGET_S, rank, reach, skip_summary
    from submit.policy import submission_policy
    rows, meta = _aa_candidates(focus)
    focus_meta = {"titles": meta["titles"], "locations": meta["locations"],
                  "sponsor_only": meta["sponsor_only"]}
    if not meta["board"] and not meta["saved"]:
        return {"error": "The job board is still loading. Open Jobs, wait for roles to "
                         "appear, then try again.", "action": "open_jobs"}, 400
    if not rows:
        return {"error": "No roles match today's focus or your saved search. Broaden "
                         "or clear it.", "focus": focus_meta}, 400
    # The same ranking Up next shows (picker.rank), so the run tailors the roles listed there;
    # a role Up next could not score yet is fetched and scored now, within the time budget.
    score = _aa_scorer(rows, deadline=time.monotonic() + TIME_BUDGET_S, network=True)
    res = rank(rows, records=_aa_records(), score_fn=score, count=count, min_fit=min_fit)
    for job in res["jobs"]:
        if not (job.get("jd_text") or "").strip():
            _aa_fit(job)                         # fills jd_text: the fit came from memory
    summary = skip_summary(res, min_fit)
    passed = {k: res[k] for k in ("below", "unscored", "queued", "applied", "scored")}
    jobs = res["jobs"]
    if not jobs:
        if not res["available"]:
            err = "Every matching role already has a package. Broaden your focus for new ones."
        else:
            err = (f"None of the {res['scored']} best-fit roles we checked reach your fit floor "
                   f"of {min_fit}%. Lower the floor or broaden your focus.")
        return {"error": err, "summary": summary, "passed": passed, "min_fit": min_fit,
                "focus": focus_meta, "nothing": True}, 400

    llm = _make_llm()   # real model; clear error if the key/connectivity is missing
    tex, tname = _load_template(None)
    # Tailor into a throwaway scratch profile, never the shared "default" row: each session
    # LOADS the pristine "default" (so every role tailors from the real base) but PERSISTS to
    # the scratch identity. A concurrent request reading "default" can't observe a half-built,
    # wrong-role profile. The scratch row (and its palace turns) are deleted at the end.
    scratch = "_autoapply_" + uuid.uuid4().hex[:8]
    tailored, skipped = [], []
    ctrl = ctrl or _control()  # P3: the person can stop/skip this run from Telegram while it runs
    halted = upgrade = False
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
            item = {k: out[k] for k in ("id", "role", "company", "coverage")}
            item["fit"] = job.get("fit")
            item["source_id"] = str(job.get("source_id") or "")
            item["url"] = job.get("url") or ""
            _aa_keep_fit(out["id"], job.get("fit"))
            item["lane"] = submission_policy(job.get("url") or "")
            tailored.append(item)
        else:
            skipped.append({"role": out.get("role") or job.get("title", "a role"),
                            "reason": out.get("reason") or "held for your review"})
            if out.get("upgrade"):          # the pass is used up: every next role would be too
                upgrade = True
                break
    try:
        _memory().delete(scratch)   # drop the throwaway row; "default" was never written
    except Exception:
        pass
    # A role with a package never needs a job-match alert as well (the same role twice).
    _alerts_mark_tailored([it["source_id"] for it in tailored if it.get("source_id")])
    return {"ok": True, "queued": len(tailored), "tailored": tailored,
            "skipped": skipped, "considered": len(jobs), "halted": halted, "upgrade": upgrade,
            "matched": res["available"], "min_fit": min_fit,
            "passed": passed, "summary": summary,
            "reach": reach([j for j in rows], submission_policy),
            "search": meta["search"], "focus": focus_meta}, 200


def _alerts_mark_tailored(sids: list[str]) -> None:
    """Keep a tailored role out of the job-match alerts (notify/alerts.py): it has a package
    now, so an alert asking to tailor it would be a duplicate. Best-effort."""
    if not sids:
        return
    try:
        from notify.alerts import AlertEngine
        AlertEngine(_notify_state()).mark_tailored(sids)
    except Exception:   # noqa: BLE001 - the packages are saved; the alert list is a nicety
        pass


# ----------------------------------------------------------------------------- Autopilot
# A scheduled, capped, hands-off Auto-apply run (CLAUDE.md §4c): while the app runs, every scan
# tick of the notify loop (5 minutes) tailors up to N new matches a day into To review, exactly
# as a manual Tailor & queue does, and never submits anything by itself. OFF by default. One
# Telegram message per run with [Approve all] [Review] [Skip]; Approve all goes through the
# same guarded batch approve (verified allowlist + autonomous on submits; the rest is marked
# approved for the person's click). Day state lives in notify_state.json under "autopilot":
# {date, count, ids, failures, paused: {code, text} | None, batches: {id: {ids, at}}}.
AUTOPILOT_PAUSE_TEXT = {
    "no_packages": "Paused: no packages left on your pass.",
    "no_ai": "Paused: the AI is not reachable. Check your connection or key.",
    "failures": "Paused after 3 failed runs. It tries again tomorrow.",
}
AUTOPILOT_MAX_FAILURES = 3
_AUTOPILOT_THREAD: dict = {"thread": None}


def _autopilot_state(today: str) -> dict:
    """Today's Autopilot state, started fresh on a new day (the count, the ids and any pause
    are per day: a backoff lasts until tomorrow)."""
    st = _notify_state().get("autopilot") or {}
    if st.get("date") != today:
        st = {"date": today, "count": 0, "ids": [], "failures": 0, "paused": None,
              "batches": {}}
    st.setdefault("batches", {})
    return st


def _autopilot_save(st: dict) -> None:
    _notify_state().set("autopilot", st)


def _autopilot_packages_left():
    """Tailored packages left on the pass, from the broker, or None when unknown: the broker
    is unreachable or not signed in, or the person runs on their own key (never counted)."""
    if _ai_provider() == "openai":
        return None
    if os.environ.get("TAILOR_OWN_KEY") == "1" and _cred("ANTHROPIC_API_KEY"):
        return None
    if not _broker_reachable():
        return None
    st, usage = _broker_get("/me/usage")
    if st != 200 or not isinstance(usage, dict) or usage.get("packages_left") is None:
        return None
    try:
        return max(0, int(usage.get("packages_left")))
    except (TypeError, ValueError):
        return None


def _autopilot_status() -> dict:
    """What the Auto-apply page shows for the Autopilot row: on/off, the per-day number,
    today's count and the pause reason (code + text) when it backed off."""
    from autoapply.picker import clamp_per_day
    p = _prefs()
    _ts, today = _now_ts_today()
    st = _autopilot_state(today)
    return {"on": bool(p.get("autopilot")), "per_day": clamp_per_day(p.get("autopilot_per_day")),
            "done": int(st.get("count") or 0), "paused": st.get("paused")}


def _autopilot_kick() -> None:
    """The notify loop's scan tick: run the Autopilot on its own thread so a multi-minute
    tailoring never holds up taps and alerts. One thread at a time."""
    th = _AUTOPILOT_THREAD["thread"]
    if th is not None and th.is_alive():
        return
    if not _prefs().get("autopilot"):
        return
    th = threading.Thread(target=_autopilot_tick, name="autopilot", daemon=True)
    _AUTOPILOT_THREAD["thread"] = th
    th.start()


def _autopilot_tell_missing(missing: list[str]) -> bool:
    """One Telegram line per missing-details pause (not one per tick): sent when a channel is
    active and this pause has not been told yet. The flag lives outside the day state, so a new
    day does not repeat it; it clears when the tick finds the profile complete."""
    ns = _notify_state()
    if ns.get("autopilot_missing_told"):
        return False
    ch = _active_channel()
    if ch is None:
        return False
    try:
        ch.send(f"Autopilot is paused: {_aa_missing_reason(missing)} and it resumes on its own.")
    except Exception:   # noqa: BLE001 - the pause shows on the page either way
        traceback.print_exc()
        return False
    ns.set("autopilot_missing_told", True)
    return True


def _autopilot_tick() -> dict:
    """One scheduled check. Runs only when ALL hold: Autopilot is on, a profile exists, today's
    pause is not set, today's count is under the per-day number, the pass has packages left
    (when the broker knows), and no other run holds the lock. Then it tailors up to the
    remaining allowance through _aa_run (the manual run's code) and messages Telegram when a
    channel is active. Backs off until tomorrow on: no AI (key or connectivity), no packages
    left, or AUTOPILOT_MAX_FAILURES runs in a row that tailored nothing because of errors."""
    from autoapply.picker import clamp_per_day
    p = _prefs()
    if not p.get("autopilot"):
        return {"ran": False, "why": "off"}
    if not _aa_has_profile():
        return {"ran": False, "why": "no_profile"}
    _ts, today = _now_ts_today()
    st = _autopilot_state(today)
    if st.get("paused") and st["paused"].get("code") != "missing_details":
        return {"ran": False, "why": "paused", "paused": st["paused"]}
    # The profile lacks a detail an unattended build cannot ask for: pause (never a failure,
    # never a retry), say so once on Telegram, and resume on the first tick after it is added.
    missing = _aa_missing_details()
    if missing:
        n = len(missing)
        st["paused"] = {"code": "missing_details", "n": n,
                        "text": f"Paused: {n} detail{'' if n == 1 else 's'} missing in your profile."}
        _autopilot_save(st)
        return {"ran": False, "why": "paused", "paused": st["paused"],
                "told": _autopilot_tell_missing(missing)}
    if st.get("paused"):                  # the details are in: resume now
        st["paused"] = None
        _autopilot_save(st)
    _notify_state().set("autopilot_missing_told", False)
    _autofill_saved_project_dates()
    per_day = clamp_per_day(p.get("autopilot_per_day"))
    remaining = per_day - int(st.get("count") or 0)
    if remaining <= 0:
        return {"ran": False, "why": "done"}
    left = _autopilot_packages_left()
    if left == 0 and not _cred("ANTHROPIC_API_KEY"):
        st["paused"] = {"code": "no_packages", "text": AUTOPILOT_PAUSE_TEXT["no_packages"]}
        _autopilot_save(st)
        return {"ran": False, "why": "paused", "paused": st["paused"]}
    if not _AA_RUN_LOCK.acquire(blocking=False):
        return {"ran": False, "why": "busy"}
    try:
        try:
            payload, status = _aa_run(remaining, _aa_min_fit(), p.get("aa_focus") or {})
        except Unavailable as exc:
            payload, status = {"error": str(exc), "reason": exc.reason}, 503
        except Exception as exc:   # noqa: BLE001 - say why, never crash the loop
            traceback.print_exc()
            payload, status = {"error": str(exc)[:200], "crashed": True}, 500
    finally:
        _AA_RUN_LOCK.release()
    st = _autopilot_state(today)          # re-read: a tap may have changed it meanwhile
    out = {"ran": True, "tailored": 0, "status": status}
    if status != 200:
        if payload.get("nothing") or payload.get("action") == "open_jobs":
            return {**out, "why": "nothing"}       # no new matches: not a failure, try later
        if payload.get("reason") in ("no_key", "offline", "no_account", "no_credit"):
            st["paused"] = {"code": "no_ai", "text": AUTOPILOT_PAUSE_TEXT["no_ai"]}
        else:
            st["failures"] = int(st.get("failures") or 0) + 1
            if st["failures"] >= AUTOPILOT_MAX_FAILURES:
                st["paused"] = {"code": "failures", "text": AUTOPILOT_PAUSE_TEXT["failures"]}
        _autopilot_save(st)
        return {**out, "why": "error", "error": payload.get("error"), "paused": st["paused"]}
    tailored = payload.get("tailored") or []
    st["count"] = int(st.get("count") or 0) + len(tailored)
    st["ids"] = (st.get("ids") or []) + [it["id"] for it in tailored]
    if tailored:
        st["failures"] = 0
    elif payload.get("upgrade"):
        st["paused"] = {"code": "no_packages", "text": AUTOPILOT_PAUSE_TEXT["no_packages"]}
    elif payload.get("skipped"):
        st["failures"] = int(st.get("failures") or 0) + 1
        if st["failures"] >= AUTOPILOT_MAX_FAILURES:
            st["paused"] = {"code": "failures", "text": AUTOPILOT_PAUSE_TEXT["failures"]}
    out.update(tailored=len(tailored), done=st["count"], paused=st["paused"])
    if tailored:
        bid = uuid.uuid4().hex[:6]
        st["batches"][bid] = {"ids": [it["id"] for it in tailored], "at": _now_iso()}
        if len(st["batches"]) > 20:
            st["batches"] = dict(list(st["batches"].items())[-20:])
        _autopilot_save(st)
        out["batch"] = bid
        ch = _active_channel()
        if ch is not None:
            try:
                ch.send(autopilot_message(tailored, _can_auto_apply, st["count"], per_day,
                                          _autopilot_packages_left()),
                        autopilot_buttons(bid))
                out["sent"] = True
            except Exception:   # noqa: BLE001 - the packages are queued either way
                traceback.print_exc()
                out["sent"] = False
    else:
        _autopilot_save(st)
    return out


def _companies(items: list[dict]) -> str:
    seen, names = set(), []
    for it in items:
        c = (it.get("company") or "").strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            names.append(c)
    return ", ".join(names)


def autopilot_message(tailored: list[dict], can_send_fn, done: int, per_day: int,
                      packages_left=None) -> str:
    """The one Telegram message after an Autopilot run. ``can_send_fn(item)`` is the app's own
    gate (verified allowlist AND autonomous on), so "can be sent for you" is honest: with
    today's allowlist that is usually none, and the line is left out when it is. The packages
    clause is left out when the broker does not know."""
    n = len(tailored)
    auto = [it for it in tailored if can_send_fn({"url": it.get("url") or ""})]
    rest = [it for it in tailored if it not in auto]
    parts = [f"I found {n} role{'s' if n != 1 else ''} you are qualified for and tailored your "
             f"resume for each." if n != 1 else
             "I found 1 role you are qualified for and tailored your resume for it."]
    if auto:
        parts.append(f"{len(auto)} can be sent for you ({_companies(auto)}).")
    if rest:
        verb = "are" if len(rest) != 1 else "is"
        parts.append(f"{len(rest)} {verb} ready and waiting for your click ({_companies(rest)}).")
    parts.append(f"{done} of {per_day} for today.")
    if packages_left is not None:
        parts.append(f"{packages_left} package{'s' if packages_left != 1 else ''} left on your "
                     "pass.")
    return " ".join(parts)


def autopilot_buttons(bid: str) -> list:
    return [[{"id": f"ap:approveall:{bid}", "label": "Approve all"},
             {"id": f"ap:review:{bid}", "label": "Review"},
             {"id": f"ap:skip:{bid}", "label": "Skip"}]]


def _autopilot_act(data: str) -> dict:
    """An Autopilot batch button: ``ap:<approveall|review|skip>:<batch>``. Approve all sends
    ONLY what the app's own gate allows (verified allowlist AND autonomous on), through the same
    rate-limited batch approve; everything else is marked approved for the person's click.
    Review sends the batch's items one by one with the usual Approve / Skip buttons. Skip marks
    them skipped, as the in-app Skip does. A batch is handled once."""
    from submit.stages import approved_patch
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "ap":
        return {"message": "Unrecognized action."}
    _, cmd, bid = parts
    _ts, today = _now_ts_today()
    state = _notify_state()
    st = state.get("autopilot") or {}
    batch = (st.get("batches") or {}).get(bid)
    if not batch:
        return {"message": "That batch was handled already, or has expired. Open SponsorJobs "
                           "to see To review."}
    ids = [int(x) for x in batch.get("ids") or []]
    pending = {r["id"]: r for r in _pending_records() if r["id"] in ids}
    actions = _BatchActions()
    if cmd == "approveall":
        sent, waiting, failed = [], [], []
        recs = _records()
        try:
            for rid in ids:
                rec = pending.get(rid)
                if not rec:
                    continue
                if _record_can_auto_apply(rid):
                    res = actions.approve(rid)
                    (sent if res.get("stage") == "submitted" else
                     waiting if res.get("ok") else failed).append(rec)
                else:
                    recs.merge_data(rid, {**approved_patch(), "approved_at": _now_iso()})
                    waiting.append(rec)
        finally:
            recs.close()
        bits = []
        if sent:
            bits.append(f"Sent {len(sent)} for you ({_companies(sent)}).")
        if waiting:
            bits.append(f"{len(waiting)} approved and waiting for your click "
                        f"({_companies(waiting)}). Open SponsorJobs to finish them.")
        if failed:
            bits.append(f"{len(failed)} could not be sent ({_companies(failed)}); it is under "
                        "Needs you.")
        msg = " ".join(bits) or "Nothing left to approve in that batch."
    elif cmd == "review":
        items = [_review_item(pending[rid]) for rid in ids if rid in pending]
        if not items:
            msg = "Nothing left to review in that batch."
        else:
            _batch_review().send(items)
            msg = f"Here are the {len(items)}, one by one." if len(items) != 1 else "Here it is."
    elif cmd == "skip":
        n = sum(1 for rid in ids if rid in pending and actions.skip(rid).get("ok"))
        msg = f"Skipped {n}. They stay in SponsorJobs, marked skipped."
    else:
        return {"message": "Unrecognized action."}

    def fn(d):
        ap = d.get("autopilot") or {}
        (ap.get("batches") or {}).pop(bid, None)
        d["autopilot"] = ap
    state.update(fn)
    return {"ok": True, "message": msg}


# ----------------------------------------------------------------------------- Home agent
# The Home box takes a pasted job description or a link (today's path: a tailoring session) or
# a plain request ("Find 5 data analytics roles and apply for me", "What is waiting on me?").
# A request is parsed into one intent (agent/home.py) and drives the features that already
# exist: the plan ranks with the same functions Up next and /api/autoapply/run use, Go ahead is
# one _aa_run under the same lock as a manual run or the Autopilot, and anything sent goes
# through the same guarded approve as Approve all (verified allowlist AND autonomous on). The
# plan is stated first and spends no package; the "can be sent" number is computed, never
# assumed. The pending plan lives here, server side, so Go ahead runs what was shown.
_AGENT: dict = {"plan": None, "intent": None}


def _agent_parse(text: str) -> dict:
    """The intent of a short request: the model when it answers, the rules otherwise (no key,
    offline). A tiny prompt on the app's usual backend; never counted as a package."""
    from agent.home import parse_request, parse_request_rules
    try:
        llm = _make_llm()
    except Exception:                 # noqa: BLE001 - no model: the rules still answer
        return parse_request_rules(text)
    try:
        return parse_request(llm, text)
    except Exception:                 # noqa: BLE001
        return parse_request_rules(text)


def _agent_focus(intent: dict) -> dict:
    return {"titles": list(intent.get("titles") or []),
            "locations": [intent["location"]] if intent.get("location") else [],
            "sponsor_only": bool(intent.get("sponsors_only"))}


def _agent_missing_reply(missing: list[str]) -> dict:
    """The plan's answer when the saved profile lacks a HARD detail an unattended build cannot
    ask for: the questions, and "Add the details", which opens the same details step as the
    Auto-apply page. No plan is kept, so Go ahead cannot run into the same wall."""
    n = len(missing)
    _AGENT["plan"] = None
    return {"text": f"Before I can tailor, your profile needs {n} detail{'' if n == 1 else 's'}:\n"
                    + "\n".join(f"- {q}" for q in missing),
            "buttons": [{"id": "add_details", "label": "Add the details"}], "missing": list(missing)}


def _agent_plan(intent: dict) -> dict:
    """find_and_apply, step one: the plan, from the same ranking Up next shows, with nothing
    tailored and no package spent. Keeps the plan for /api/agent/go."""
    from agent.home import cap_count, no_match_text, plan_text
    from autoapply.picker import rank
    if not _aa_has_profile():
        return {"text": _AA_NO_PROFILE["error"], "buttons": [{"id": "add_resume",
                                                             "label": "Add my resume"}]}
    missing = _aa_missing_details()     # HARD only: an undated project never stops the plan
    if missing:
        return _agent_missing_reply(missing)
    focus = _agent_focus(intent)
    rows, meta = _aa_candidates(focus)
    titles, location = focus["titles"], intent.get("location")
    if (not meta["board"] and not meta["saved"]) or not rows:
        return {"text": no_match_text(titles, location), "buttons": []}
    left = _autopilot_packages_left()
    n, note = cap_count(intent.get("count"), left)
    if n <= 0:
        return {"text": "No packages left on your pass, so I cannot tailor right now. Add a "
                        "pass, or your own AI key, under Auto-apply.", "buttons": []}
    min_fit = _aa_min_fit()
    score = _aa_scorer(rows, deadline=time.monotonic() + UPNEXT_BUDGET_S)
    res = rank(rows, records=_aa_records(), score_fn=score, count=n, min_fit=min_fit)
    jobs = res["jobs"]
    if not jobs and not res["pending"]:
        if not res["available"]:
            return {"text": "Every matching role already has a package. Try broader titles or "
                            "another location.", "buttons": []}
        return {"text": f"None of the {res['scored']} best-fit roles reach your fit floor of "
                        f"{min_fit}%. Lower the floor in Auto-apply, or broaden the titles.",
                "buttons": []}
    if not res["pending"] and len(jobs) < n:
        # Every candidate is scored and fewer than asked pass: say the true number, never
        # promise five when four exist. With roles still to score, the run may find more.
        n = len(jobs)
        note = f"Only {n} {'matches' if n == 1 else 'match'} right now, so I will do {n}."
    auto = [j for j in jobs if _can_auto_apply(j)]
    reason = ("sending for you is off in Auto-apply" if not _autonomous_on()
              else "none of these sites allows it")
    telegram = bool(intent.get("telegram"))
    connected = _active_channel() is not None
    text = plan_text(n=n, titles=titles, min_fit=min_fit, auto=len(auto),
                     auto_companies=_companies(auto), auto_reason=reason, telegram=telegram,
                     telegram_connected=connected, packages_left=left, note=note)
    _AGENT["plan"] = {"focus": focus, "count": n, "min_fit": min_fit, "telegram": telegram,
                      "titles": titles}
    return {"text": text, "buttons": [{"id": "go", "label": "Go ahead"},
                                      {"id": "change", "label": "Change"}],
            "plan": {"count": n, "focus": focus, "auto": len(auto), "min_fit": min_fit}}


def _agent_status() -> dict:
    """status: one bubble from the records, the same tiles the Auto-apply page counts."""
    from agent.home import status_text
    from submit.stages import tile_of
    _ts, today = _now_ts_today()
    recs = _records()
    try:
        rows = recs.list(limit=10000)
    finally:
        recs.close()
    counts = {"review": 0, "waiting": 0, "needs": 0, "submitted_today": 0}
    newest: list[tuple[str, str]] = []
    for r in rows:
        data = _record_data(r)
        tile = tile_of(data)
        if tile in ("review", "waiting", "needs"):
            counts[tile] += 1
            role = (r.get("jd_label") or r.get("role") or "a role").split(" · ")[0]
            name = f"{role} at {r['company']}" if r.get("company") else role
            newest.append((_aa_when(data, r), name))
        elif tile == "submitted":
            sub = data.get("submission") if isinstance(data.get("submission"), dict) else {}
            if str(data.get("applied_at") or sub.get("at") or "")[:10] == today:
                counts["submitted_today"] += 1
    newest.sort(key=lambda x: x[0], reverse=True)
    return {"text": status_text(counts, [n for _w, n in newest]),
            "buttons": [{"id": "open_autoapply", "label": "Open Auto-apply"}],
            "counts": counts}


@app.post("/api/agent/ask")
@_guard
def agent_ask():
    """One turn of the Home box. {text, change?}. Answers {kind: "jd" | "url"} when the text is
    a posting or a link (the client opens a tailoring session as before), else {kind: "agent",
    intent, text, buttons}. ``change`` merges the text into the last plan's intent and re-plans."""
    from agent.home import empty_intent, other_text, route, tailor_one_text
    body = request.json or {}
    text = str(body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Tell me what to do, or paste a job description."}), 400
    kind = route(text)
    if kind in ("jd", "url"):
        return jsonify({"ok": True, "kind": kind})
    intent = _agent_parse(text)
    if body.get("change") and _AGENT.get("intent"):
        prev = _AGENT["intent"]
        merged = dict(prev)
        for key in ("titles", "count", "location", "sponsors_only"):
            if intent.get(key):
                merged[key] = intent[key]
        merged["telegram"] = prev.get("telegram") or intent.get("telegram")
        intent = {**merged, "intent": "find_and_apply"}
    if intent["intent"] == "find_and_apply":
        _AGENT["intent"] = intent
        out = _agent_plan(intent)
    elif intent["intent"] == "status":
        out = _agent_status()
    elif intent["intent"] == "tailor_one":
        _AGENT["intent"] = {**empty_intent("find_and_apply"), "titles": intent.get("titles") or []}
        out = {"text": tailor_one_text(intent),
               "buttons": [{"id": "find", "label": "Find matching roles"}]}
    else:
        out = {"text": other_text(), "buttons": []}
    return jsonify({"ok": True, "kind": "agent", "intent": intent, **out})


@app.post("/api/agent/go")
@_guard
def agent_go():
    """Go ahead: run the plan that was shown, exactly as Tailor & queue does, under the one
    run lock. Then send only what the app's own gate allows (verified allowlist AND autonomous
    on) through the same approve as Approve all; everything else waits in To review. With
    Telegram asked for and connected, one message with the Autopilot batch buttons."""
    from agent.home import batch_message, result_text
    plan = _AGENT.get("plan")
    if not plan:
        return jsonify({"error": "There is no plan to run. Tell me what to find first."}), 400
    if not _AA_RUN_LOCK.acquire(blocking=False):
        return jsonify({"error": "A run is already tailoring. Wait for it to finish, then try "
                                 "again.", "busy": True}), 409
    try:
        payload, status = _aa_run(plan["count"], plan["min_fit"], plan["focus"])
    finally:
        _AA_RUN_LOCK.release()
    if status != 200:
        return jsonify(payload), status
    _AGENT["plan"] = None
    tailored = payload.get("tailored") or []
    sent, failed = [], []
    actions = _BatchActions()
    for it in tailored:
        if not _record_can_auto_apply(it["id"]):
            continue
        try:
            res = actions.approve(it["id"])
        except Exception:               # noqa: BLE001 - it stays in To review, under Needs you
            res = {}
        (sent if res.get("stage") == "submitted" else failed).append(it)
    text = result_text(len(tailored), len(sent))
    out = {"ok": True, "text": text, "buttons": [{"id": "open_autoapply",
                                                 "label": "Open Auto-apply"}],
           "tailored": len(tailored), "sent": len(sent), "skipped": payload.get("skipped") or [],
           "telegram": False}
    ch = _active_channel() if plan.get("telegram") else None
    if ch is not None and tailored:
        _ts, today = _now_ts_today()
        st = _autopilot_state(today)
        bid = uuid.uuid4().hex[:6]
        rest = [it["id"] for it in tailored if it not in sent]
        st["batches"][bid] = {"ids": rest or [it["id"] for it in tailored], "at": _now_iso()}
        if len(st["batches"]) > 20:
            st["batches"] = dict(list(st["batches"].items())[-20:])
        _autopilot_save(st)
        try:
            ch.send(batch_message(tailored, sent, _companies), autopilot_buttons(bid))
            out["telegram"] = True
        except Exception:               # noqa: BLE001 - the packages are queued either way
            traceback.print_exc()
    return jsonify(out)


def _record_fit(rec: dict, data: dict) -> int | None:
    """The ONE match number a saved application shows everywhere: how well the person fits the
    job (the Jobs page and Auto-apply number), not how many of the ad's phrases made it onto the
    one-page resume (that is "Keywords", in the editor). Kept on the record once worked out, so
    the list doesn't re-read every ad on each load and every page agrees."""
    fit = data.get("fit")
    if isinstance(fit, (int, float)):
        return int(round(fit))
    jd = (data.get("jd_text") or "").strip()
    if not jd:
        return None
    from notify.alerts import match_percent
    try:
        m = _job_match(jd, rec.get("role") or "")
    except Exception:   # noqa: BLE001 - a missing number, never a broken list
        return None
    if not m or not m.get("total"):
        return None
    pct = match_percent(m)
    if pct is not None:
        _aa_keep_fit(rec["id"], pct)
    return pct


def _aa_keep_fit(rid, fit) -> None:
    """Keep the run's fit percent on the record, so the page shows the same match the run used."""
    if fit is None:
        return
    recs = _records()
    try:
        recs.merge_data(int(rid), {"fit": int(fit)})
    except Exception:   # noqa: BLE001 - the package is saved; the number is a nicety
        pass
    finally:
        recs.close()


_ATS_NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "ashbyhq": "Ashby", "ashby": "Ashby",
              "workable": "Workable", "smartrecruiters": "SmartRecruiters", "recruitee": "Recruitee",
              "myworkdayjobs": "Workday", "workday": "Workday", "linkedin": "LinkedIn",
              "icims": "iCIMS", "jobvite": "Jobvite", "bamboohr": "BambooHR", "taleo": "Taleo"}


def _aa_site(url: str) -> str:
    """The site an application opens on, by name ("Greenhouse"), else its host."""
    from submit.policy import _host
    host = _host(url or "")
    if not host:
        return ""
    for key, name in _ATS_NAMES.items():
        if key in host:
            return name
    return host[4:] if host.startswith("www.") else host


def _aa_job_index(rows: list[dict]) -> dict[str, dict]:
    """{source_id: stored job} for the records' jobs, read once per request."""
    sids = []
    for r in rows:
        sid = str((_record_data(r).get("source_job") or {}).get("source_id") or "").strip()
        if sid:
            sids.append(sid)
    if not sids:
        return {}
    out: dict[str, dict] = {}
    w = _watchlist()
    try:
        for sid in dict.fromkeys(sids):
            j = w.get_job(sid)
            if j:
                out[sid] = j
    except Exception:   # noqa: BLE001 - the row still has its record's own fields
        pass
    finally:
        w.close()
    return out


def _aa_when(data: dict, rec: dict) -> str:
    """When an application last moved, for ordering a tile (newest first)."""
    sub = data.get("submission") if isinstance(data.get("submission"), dict) else {}
    return str(data.get("applied_at") or sub.get("at") or data.get("approved_at")
               or rec.get("created_at") or "")


def _aa_row(rec: dict, data: dict, jobs: dict[str, dict]) -> dict:
    """One application as the Auto-apply page lists it. Only facts the record or its stored
    job hold; anything unknown is left empty, never guessed."""
    from submit import classify_record
    from submit.stages import app_stage, row_action, tile_of
    st = app_stage(data)
    tile = tile_of(data) or ""
    sj = data.get("source_job") or {}
    job = jobs.get(str(sj.get("source_id") or "").strip()) or {}
    company = rec.get("company") or job.get("company") or ""
    location = str(sj.get("location") or job.get("location") or "")
    tagged = _sponsors().tag_jobs([{"company": company, "location": location or "United States"}])[0]
    stance = {}
    jd = job.get("jd_text") or ""
    if jd:
        from sourcing.adstance import UNKNOWN, ad_stance
        a = ad_stance(jd)
        if a["stance"] != UNKNOWN:
            stance = {"ad_stance": a["stance"], "ad_sentence": a["sentence"]}
    sub = data.get("submission") if isinstance(data.get("submission"), dict) else {}
    match = _record_fit(rec, data)
    url = (sj.get("url") or job.get("url") or "")
    plan = classify_record(data)
    return {
        "id": rec["id"], "role": (rec.get("jd_label") or rec.get("role") or "").split(" · ")[0],
        "company": company, "location": location,
        "visa": tagged.get("visa") or [], "sponsorship_stated": job.get("sponsorship_stated"),
        **stance,
        "match": round(match) if match is not None else None,
        "tile": tile, "stage": st["stage"], "stage_label": st["label"], "stage_detail": st["detail"],
        "reason": st["reason"], "how": st["how"], "queued": bool(data.get("queued")),
        "action": row_action(tile, st["reason"]),
        "lane": plan["tier"],
        "apply_url": _application_url(url, company) if url else "",
        "posting_url": url,
        "site": _aa_site(url),
        "saved": bool(job.get("saved")),
        "cv_path": _resume_upload_path(rec),
        "created_at": rec.get("created_at") or "",
        "approved_at": data.get("approved_at") or "",
        "submitted_at": data.get("applied_at") or sub.get("at") or "",
        "when": _aa_when(data, rec),
    }


def _aa_week_start():
    from datetime import date, timedelta
    today = date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


@app.get("/api/autoapply/board")
@_guard
def autoapply_board():
    """Everything the Auto-apply page shows except Up next: the four tiles with their counts
    and rows, the Needs you reasons, this week's submissions, today's auto-submit count against
    the daily cap, recent submissions, and which connections exist. Read-only."""
    from submit.stages import TILES, default_tile, needs_summary
    recs = _records()
    try:
        rows = recs.list(limit=10000)
    finally:
        recs.close()
    jobs = _aa_job_index(rows)
    tiles: dict[str, list[dict]] = {t: [] for t in TILES}
    for r in rows:
        data = _record_data(r)
        item = _aa_row(r, data, jobs)
        if item["tile"] in tiles:
            tiles[item["tile"]].append(item)
    for t in TILES:
        tiles[t].sort(key=lambda x: x["when"], reverse=True)
    week = _aa_week_start()
    sent_week = [x for x in tiles["submitted"] if str(x["submitted_at"])[:10] >= week]
    counts = {t: len(tiles[t]) for t in TILES}
    ident = (_saved_full_profile().get("identity") or {})
    p = _prefs()
    ts, today = _now_ts_today()
    return jsonify({
        "ok": True,
        "has_profile": _aa_has_profile(),
        "counts": counts,
        "default": default_tile(counts),
        "tiles": tiles,
        "week": {"sent": len(sent_week), "for_you": sum(1 for x in sent_week if x["how"] == "auto")},
        "needs_summary": needs_summary([x["reason"] for x in tiles["needs"]]),
        "missing_contact": [k for k in ("name", "email") if not ident.get(k)],
        "today": {**_rate_limiter().status(ts, today), "autonomous": _autonomous_on()},
        "recent": tiles["submitted"][:5],
        "telegram": _active_channel() is not None,
        "approve_all": bool(p.get("approve_all")),
        "min_fit": _aa_min_fit(),
        "autopilot": _autopilot_status(),
    })


def _pdf_pages(path) -> int | None:
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(path))
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:   # noqa: BLE001 - unknown page count is left out, not guessed
        return None


@app.get("/api/autoapply/item/<int:rid>")
@_guard
def autoapply_item(rid: int):
    """One application for the Auto-apply drawer: the row, plus what will be sent (the resume
    with its matched job terms, the cover letter, the screening answers), the job's type, pay
    and posting date when the stored job has them, and the timeline from the record's own
    timestamps. Nothing here is estimated."""
    rec = _get_record(rid)
    if not rec:
        return jsonify({"error": "That application was not found."}), 404
    data = _record_data(rec)
    jobs = _aa_job_index([rec])
    row = _aa_row(rec, data, jobs)
    job = jobs.get(str((data.get("source_job") or {}).get("source_id") or "").strip()) or {}
    cov = data.get("coverage") or {}
    present = [str(t) for t in (cov.get("present") or [])]
    missing = [str(t) for t in (cov.get("missing") or [])] + \
              [str(t) for t in (cov.get("missing_supported") or [])]
    pdf = WORKDIR / f"cv-{rid}.pdf"
    letter = (data.get("cover_letter") or "").strip()
    remote = (job.get("remote") or "").lower()
    sub = data.get("submission") if isinstance(data.get("submission"), dict) else {}
    return jsonify({
        **row,
        "type": {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}.get(remote, ""),
        "pay": (job.get("salary") or "").strip(),
        "posted_at": str(job.get("posted_at") or "")[:10],
        "resume": {"exists": pdf.exists(), "pages": _pdf_pages(pdf) if pdf.exists() else None,
                   "url": f"/api/cv.pdf?job=cv-{rid}" if pdf.exists() else "",
                   "path": str(pdf) if pdf.exists() else "",
                   "terms": present, "missing": missing,
                   "terms_total": len(present) + len(missing)},
        "letter": {"words": len(letter.split()), "text": letter[:700],
                   "url": f"/api/record/{rid}/cover_letter.pdf" if letter else ""},
        "screening": [{"q": str(qa.get("question") or ""), "a": str(qa.get("answer") or ""),
                       "knockout": bool(qa.get("knockout"))}
                      for qa in (data.get("screening") or []) if isinstance(qa, dict)],
        "message": sub.get("message") or "",
        "picked": bool((data.get("source_job") or {}).get("source_id")),
    })


_AA_NEXT_CACHE: dict = {}


@app.get("/api/autoapply/upnext")
@_guard
def autoapply_upnext():
    """The roles the next run would tailor, in the order it would take them: saved jobs first,
    then prioritized companies, then the best fits at or above the fit floor, skipping roles that
    already have a package and roles the person skipped here. The same picker as
    /api/autoapply/run (autoapply.picker.rank): a cheap prefilter over the whole board, then the
    real fit on the top few hundred. Roles whose description is not on this machine yet are
    scored in the background; ``pending`` says how many, and the page asks again. Nothing is
    tailored."""
    from autoapply.picker import rank
    t0 = time.monotonic()
    a = request.args
    count = max(1, min(int(a.get("count") or 5), 10))
    focus = {"titles": _split(a.get("titles")), "locations": _split(a.get("locations")),
             "sponsor_only": a.get("sponsor_only") in ("1", "true", "on")}
    min_fit = _aa_min_fit()
    records = _aa_records()
    key = (count, tuple(focus["titles"]), tuple(focus["locations"]), focus["sponsor_only"],
           min_fit, len(records), tuple(_prefs().get("aa_skip") or []),
           _LEARN_VER[0], _load_prefs().get("needs_sponsorship"), _AA_WARM["ver"])
    hit = _AA_NEXT_CACHE.get("v")
    if hit and hit[0] == key and time.time() - hit[1] < 120:
        return jsonify(hit[2])
    rows, meta = _aa_candidates(focus)
    score = _aa_scorer(rows, deadline=t0 + UPNEXT_BUDGET_S)
    res = rank(rows, records=records, score_fn=score, count=count, min_fit=min_fit)
    rows_out = []
    for j in res["jobs"]:
        rows_out.append({
            "source_id": j.get("source_id") or "", "title": j.get("title") or "",
            "company": j.get("company") or "", "location": j.get("location") or "",
            "visa": j.get("visa") or [], "ad_stance": j.get("ad_stance"),
            "ad_sentence": j.get("ad_sentence"), "sponsorship_stated": j.get("sponsorship_stated"),
            "fit": j.get("fit"), "saved": bool(j.get("saved")), "url": j.get("url") or "",
            "salary": j.get("salary") or "", "priority": bool(j.get("priority")),
        })
    out = {"ok": True, "jobs": rows_out, "count": count, "min_fit": min_fit,
           "matches": res["available"], "scored": res["scored"], "pool": res["pool"],
           "pending": res["pending"], "has_profile": _aa_has_profile(),
           "board": meta["board"], "search": meta["search"],
           "feed_loading": _feed_loading()}
    if res["pending"]:
        from autoapply.picker import taken_index, taken_status
        idx = taken_index(records)
        _aa_warm([j for j in rows[:res["pool"] + res["applied"] + res["queued"]]
                  if taken_status(j, idx) is None][:res["pool"]])
        return jsonify(out)                  # not cached: the rest of the list is on its way
    if out["feed_loading"]:
        return jsonify(out)                  # not cached: the list lands in a few minutes
    _AA_NEXT_CACHE["v"] = (key, time.time(), out)
    return jsonify(out)


@app.post("/api/autoapply/skip")
@_guard
def autoapply_skip():
    """Take one role out of the next Auto-apply run (Up next's x). Body {source_id, undo?}.
    The role stays on Jobs; only the run passes it over."""
    b = request.json or {}
    sid = str(b.get("source_id") or "").strip()
    if not sid:
        return jsonify({"error": "Which role? source_id is required."}), 400
    cur = [x for x in (_prefs().get("aa_skip") or []) if x != sid]
    if not b.get("undo"):
        cur.append(sid)
    _set_prefs({"aa_skip": cur})
    _AA_NEXT_CACHE.pop("v", None)
    return jsonify({"ok": True, "skipped": len(cur)})


# ---------------------------------------------------------------- skip reasons + company lists
# Why the person skipped a role (one tap, optional), what that taught the picker, and their
# "Never apply to" / "Prioritize" company lists. Local JSON next to the other prefs; the rules
# are re-derived from the skips every time (autoapply/learning.py), so undo is exact.
_LEARN_FILE = _DATA / "aa_learn.json"
_LEARN_VER = [0]       # bumped on every write, so Up next's cache never serves an old pick


def _learn_state() -> dict:
    from autoapply.learning import clean_state
    try:
        return clean_state(_json_prefs.loads(_LEARN_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return clean_state(None)


def _save_learn(state: dict) -> None:
    _LEARN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LEARN_FILE.write_text(_json_prefs.dumps(state), encoding="utf-8")
    _LEARN_VER[0] += 1
    _AA_NEXT_CACHE.pop("v", None)


def _learn_view(state: dict | None = None) -> dict:
    from autoapply.learning import learned, needs_sponsorship
    st = state if state is not None else _learn_state()
    names = lambda k: [c.get("name") for c in st.get(k, []) if c.get("name")]  # noqa: E731
    rules = [{k: r[k] for k in ("id", "kind", "value", "count")} for r in learned(st)]
    return {"ok": True, "exclude": names("exclude"), "prioritize": names("prioritize"),
            "learned": rules, "skips": sum(1 for s in st.get("skips", []) if s.get("reason")),
            "needs_sponsorship": needs_sponsorship(_load_prefs()),
            "portal_accounts": bool(_prefs().get("portal_accounts", True))}


@app.get("/api/autoapply/prefs")
@_guard
def autoapply_prefs():
    """The company lists and what Auto-apply learned from skip reasons, for the Preferences
    panel. Every learned line names its rule id so it can be undone."""
    return jsonify(_learn_view())


@app.post("/api/autoapply/reason")
@_guard
def autoapply_reason():
    """Say why a role was skipped. Body {source_id | record_id, title, company, location,
    salary, reason, note}. The skip itself already happened (Up next x, the drawer's Skip, Jobs
    Dismiss); this only records the reason, and may be sent with no reason at all. A "company"
    reason returns ask_company so the page can ask once "Skip all roles at X?"."""
    from autoapply.learning import REASONS, record_skip
    b = request.json or {}
    reason = str(b.get("reason") or "")
    if reason and reason not in REASONS:
        return jsonify({"error": "Unknown reason."}), 400
    key = str(b.get("source_id") or "").strip()
    if not key and b.get("record_id") not in (None, ""):
        key = f"rec:{b.get('record_id')}"
    if not key:
        return jsonify({"error": "Which role? source_id or record_id is required."}), 400
    st = _learn_state()
    before = {r["id"] for r in _learn_view(st)["learned"]}
    record_skip(st, {**b, "key": key}, reason, b.get("note") or "")
    _save_learn(st)
    view = _learn_view(st)
    company = str(b.get("company") or "").strip()
    from autoapply.learning import company_key
    already = company_key(company) in {company_key(n) for n in view["exclude"]}
    view["new"] = [r for r in view["learned"] if r["id"] not in before]
    view["ask_company"] = company if reason == "company" and company and not already else ""
    return jsonify(view)


@app.post("/api/autoapply/company")
@_guard
def autoapply_company():
    """Add or remove a company on "Never apply to" (list=exclude) or "Prioritize"
    (list=prioritize). Body {list, name, remove?}. A company sits on one list at a time."""
    from autoapply.learning import set_company
    b = request.json or {}
    st = _learn_state()
    if not set_company(st, str(b.get("list") or ""), b.get("name") or "",
                       on=not b.get("remove"), why="skip" if b.get("from_skip") else "you"):
        return jsonify({"error": "Name a company and a list (exclude or prioritize)."}), 400
    _save_learn(st)
    return jsonify(_learn_view(st))


@app.post("/api/autoapply/forget")
@_guard
def autoapply_forget():
    """Undo one thing Auto-apply learned. Body {id} (a rule id from /api/autoapply/prefs)."""
    from autoapply.learning import forget
    st = _learn_state()
    if not forget(st, str((request.json or {}).get("id") or "")):
        return jsonify({"error": "That rule is already gone."}), 404
    _save_learn(st)
    return jsonify(_learn_view(st))


def _jobs_personal(rows: list[dict], *, home: bool = False) -> list[dict]:
    """The Jobs board through the person's own settings: a role whose ad or TITLE requires
    citizenship or a clearance ("USC & GC only", "Top Secret"), or says it will not sponsor, is
    hidden while they need sponsorship. Home, the Jobs page and Auto-apply all use this one rule
    (autoapply.learning.closed_to_visa, title=True; Kofi, 2026-10-27). Nothing is deleted: the
    board count is unchanged and a citizen sees every role. Measured on 215,733 roles: the title
    check hides 23. Company lists only mark rows (see _jobs_mark), they never hide them.
    ``home`` is kept for the Home request's URL and is no longer different."""
    from autoapply.learning import closed_to_visa, needs_sponsorship
    if not needs_sponsorship(_load_prefs()):
        return rows
    return [j for j in rows if not closed_to_visa(j, title=True)]


def _jobs_mark(page_rows: list[dict]) -> list[dict]:
    """One page of Jobs rows with ``excluded`` / ``priority`` set from the company lists."""
    from autoapply.learning import mark_board
    st = _learn_state()
    if not st.get("exclude") and not st.get("prioritize"):
        return page_rows
    return mark_board(page_rows, st)


# ============================================================================ tracker board
# The job tracker (Huntr / Teal style, local-first): one stage per job the person saved,
# tailored, or applied to, on the SAME sourced_job rows the Jobs page uses (sourcing/board.py).
# The review queue's per-record stage (submit/tracker.py) and the board are kept in step:
# a record that is applied / interviewing pulls its job forward; a board move patches its
# records so the apply to-do and the Saved CVs follow-ups agree with the board.

def _board_key(rec: dict) -> tuple[str, dict | None]:
    """The board row a tailored record belongs to: its sourced job, or a board-only row keyed
    ``tailored:<id>`` for a resume built from a pasted JD (never shown on the Jobs feed)."""
    from sourcing.board import TAILORED_SOURCE
    data = _record_data(rec)
    sj = data.get("source_job") or {}
    sid = str(sj.get("source_id") or "").strip()
    role = (rec.get("jd_label") or rec.get("role") or "").split(" · ")[0].strip()
    if sid:
        snap = {"source_id": sid, "source": sj.get("source") or "", "company": rec.get("company") or "",
                "title": role, "url": sj.get("url") or "", "posted_at": (rec.get("created_at") or "")[:10]}
        return sid, snap
    sid = f"{TAILORED_SOURCE}:{rec['id']}"
    return sid, {"source_id": sid, "source": TAILORED_SOURCE, "company": rec.get("company") or "",
                 "title": role, "url": sj.get("url") or "", "posted_at": (rec.get("created_at") or "")[:10]}


def _record_board_stage(data: dict) -> str:
    """The board column one record belongs in: its funnel stage, except that a tailored
    record the person approved (not submitted yet, or handed back after an auto attempt)
    sits in Approved rather than Saved."""
    from submit.stages import app_stage
    from submit.tracker import effective_stage
    rstage = effective_stage(data)
    if rstage == "saved" and app_stage(data)["stage"] in ("approved", "failed"):
        return "approved"
    return rstage


def _board_reconcile(w) -> dict[str, list[dict]]:
    """Pull every tailored record onto the board and return {source_id: [records]}.

    A record not on the board yet lands in its own stage (Saved when just tailored, Applied once
    the assisted-apply engine, the auto lane, or the person marked it applied). A record that is
    further along than its job (applied while the board still says Saved) moves the job forward
    and logs why. The board is never moved backwards from here: once the person drags a card, the
    board is the source of truth and the move patches the records instead."""
    from sourcing.board import RANK, STAGES
    recs = _records()
    try:
        rows = recs.list(limit=10000)
    finally:
        recs.close()
    by_job: dict[str, list[dict]] = {}
    for r in rows:
        sid, snap = _board_key(r)
        data = _record_data(r)
        rstage = _record_board_stage(data)
        if rstage not in STAGES:
            rstage = "saved"
        applied_at = data.get("applied_at") or None
        cur = w.stage_of(sid)
        note = {"applied": "Application submitted", "approved": "Approved"}.get(rstage, "Tailored a resume")
        if cur is None:
            existing = w.get_job(sid)
            w.track_job(sid, rstage, note=note, now=(_local_stamp(r.get("created_at")) or None) if rstage == "saved" else None,
                        job=None if existing else snap, applied_at=applied_at)
        elif cur not in ("rejected", "archived") and RANK[rstage] > RANK[cur]:
            w.move_stage(sid, rstage, note=note, applied_at=applied_at)
        by_job.setdefault(sid, []).append(r)
    return by_job


def _board_follow_record(rec: dict, stage: str) -> None:
    """A stage set on one tailored record from the review queue (its stage picker, or Mark as
    applied) moves that record's board job too, in EITHER direction: the person chose, so the
    board follows. Best-effort: the record is already saved, and the board catches up on its
    next read anyway."""
    try:
        from sourcing.board import STAGES
        if stage not in STAGES:
            return
        sid, snap = _board_key(rec)
        w = _watchlist()
        try:
            if w.stage_of(sid) is None:
                w.track_job(sid, stage, note="From the review queue",
                            job=None if w.get_job(sid) else snap)
            else:
                w.move_stage(sid, stage, note="From the review queue")
        finally:
            w.close()
    except Exception:
        pass


def _board_patch_records(records: list[dict], stage: str) -> None:
    """Keep the review queue's own funnel in step with a board move (Archived has no record
    stage, so those records are left alone)."""
    from submit.tracker import STAGES as REC_STAGES
    from submit.tracker import stage_patch
    if stage == "approved" and records:
        # Dragged to Approved: the same as approving it in Review, minus any submit. A record
        # already approved or past it is left alone (the board never un-submits a record).
        from submit.stages import app_stage, approved_patch
        recs = _records()
        try:
            for r in records:
                data = _record_data(r)
                if app_stage(data)["stage"] == "queued":
                    recs.merge_data(r["id"], approved_patch())
                elif data.get("status") == "applied":
                    recs.merge_data(r["id"], {**stage_patch(data, "saved", _now_iso()),
                                              **approved_patch()})
        finally:
            recs.close()
        return
    if stage not in REC_STAGES or not records:
        return
    recs = _records()
    try:
        for r in records:
            data = _record_data(r)
            if stage == "saved":
                # Already back before review: nothing approved or sent to undo.
                if (data.get("status") or "ready") == "ready" and data.get("stage") in (None, "saved"):
                    continue
            elif data.get("stage") == stage or (stage == "applied" and data.get("status") == "applied"):
                continue
            patch = stage_patch(data, stage, _now_iso())
            if stage == "saved":
                patch.update({"submission": None, "queued": False})   # back before review
            recs.merge_data(r["id"], patch)
    finally:
        recs.close()


def _board_card(j: dict, records: list[dict], today) -> dict:
    """One card: the job, its sponsorship badges, the latest tailored resume, and the dates."""
    from sourcing.board import date_of
    latest = max(records, key=lambda r: r["id"]) if records else None
    rec = None
    if latest:
        data = _record_data(latest)
        job = f"cv-{latest['id']}"
        from submit.stages import app_stage
        st = app_stage(data)
        rec = {"id": latest["id"], "role": latest.get("role") or "",
               # How it was sent, or why it came back: "Submitted for you", "Needs you".
               "state": st["stage"], "state_label": st["label"], "state_detail": st["detail"],
               "how": st["how"],
               "pdf": f"/api/cv.pdf?job={job}" if (WORKDIR / f"{job}.pdf").exists() else None,
               "cover_letter": (f"/api/record/{latest['id']}/cover_letter.pdf"
                                if (data.get("cover_letter") or "").strip() else None),
               "count": len(records)}
    fu = date_of(j.get("follow_up_at"))
    return {
        "source_id": j["source_id"], "company": j.get("company") or "", "title": j.get("title") or "",
        "location": j.get("location") or "", "url": j.get("url") or "", "source": j.get("source") or "",
        "stage": j.get("stage"), "stage_at": (j.get("stage_at") or "")[:10],
        "applied_at": (j.get("applied_at") or "")[:10] or None,
        "follow_up_at": j.get("follow_up_at") or None,
        "follow_up_due": bool(fu and fu <= today and j.get("stage") in ("applied", "interviewing")),
        "visa": j.get("visa") or [], "sponsorship_stated": j.get("sponsorship_stated"),
        "has_notes": bool((j.get("notes") or "").strip()),
        "record": rec,
    }


def _board_goal() -> int:
    return int(_prefs().get("tracker_weekly_goal") or 0)


@app.get("/api/tracker/board")
@_guard
def tracker_board():
    """The whole board: every stage with its cards, plus how many follow-ups are due today.
    ?stage=applied narrows to one column."""
    from datetime import date

    from sourcing.board import STAGES
    only = (request.args.get("stage") or "").strip()
    if only and only not in STAGES:
        return jsonify({"error": f"stage must be one of {', '.join(STAGES)}"}), 400
    today = date.today()
    w = _watchlist()
    try:
        by_job = _board_reconcile(w)
        jobs = _sponsors().tag_jobs(w.board_jobs(only or None))
        due = len(w.due_followups(today))
    finally:
        w.close()
    columns = {s: [] for s in ([only] if only else STAGES)}
    for j in jobs:
        columns[j["stage"]].append(_board_card(j, by_job.get(j["source_id"], []), today))
    return _nostore(jsonify({"stages": list(STAGES), "columns": columns, "due_count": due,
                             "goal": _board_goal(), "total": len(jobs)}))


@app.post("/api/tracker/move")
@_guard
def tracker_move():
    """Move one job to a stage (a drag on the board, or the stage picker). Body: {source_id,
    stage, note?, job?}. ``job`` is a snapshot so a role straight from the feed can be put on
    the board even before it is in the local DB (same contract as /api/jobs/save)."""
    from datetime import date

    from sourcing.board import STAGES
    body = request.json or {}
    sid = str(body.get("source_id") or "").strip()
    stage = str(body.get("stage") or "").strip()
    if not sid:
        return jsonify({"error": "missing source_id"}), 400
    if stage not in STAGES:
        return jsonify({"error": f"stage must be one of {', '.join(STAGES)}"}), 400
    w = _watchlist()
    try:
        by_job = _board_reconcile(w)
        if w.get_job(sid) is None:
            snap = body.get("job") if isinstance(body.get("job"), dict) else None
            if not snap or snap.get("source_id") != sid:
                return jsonify({"error": "That job is not in your local list; send its snapshot."}), 404
            w.track_job(sid, stage, note=str(body.get("note") or ""), job=snap)
        row = w.move_stage(sid, stage, note=str(body.get("note") or ""))
        _board_patch_records(by_job.get(sid, []), stage)
        job = _sponsors().tag_jobs([row])[0]
        due = len(w.due_followups(date.today()))
    finally:
        w.close()
    return jsonify({"ok": True, "job": _board_card(job, by_job.get(sid, []), date.today()),
                    "due_count": due})


@app.get("/api/tracker/job")
@_guard
def tracker_job():
    """The side panel for one card: notes, contacts, follow-up, the timeline, and every tailored
    resume built for it. ?source_id=..."""
    from datetime import date
    sid = (request.args.get("source_id") or "").strip()
    w = _watchlist()
    try:
        by_job = _board_reconcile(w)
        row = w.board_job(sid)
        if row is None or not row.get("stage"):
            return jsonify({"error": "That job is not on your board."}), 404
        job = _sponsors().tag_jobs([row])[0]
        contacts = w.contacts(sid)
        activity = w.activity(sid)
    finally:
        w.close()
    recs = []
    for r in sorted(by_job.get(sid, []), key=lambda r: -r["id"]):
        data = _record_data(r)
        jobname = f"cv-{r['id']}"
        recs.append({"id": r["id"], "role": r.get("role") or "", "when": _local_day(r.get("created_at")),
                     "pdf": f"/api/cv.pdf?job={jobname}" if (WORKDIR / f"{jobname}.pdf").exists() else None,
                     "cover_letter": (f"/api/record/{r['id']}/cover_letter.pdf"
                                      if (data.get("cover_letter") or "").strip() else None)})
    card = _board_card(job, by_job.get(sid, []), date.today())
    return _nostore(jsonify({**card, "notes": job.get("notes") or "", "contacts": contacts,
                             "activity": activity, "records": recs, "salary": job.get("salary") or ""}))


@app.post("/api/tracker/note")
@_guard
def tracker_note():
    """Free-text notes for one job, saved whole on every blur. Body: {source_id, notes}."""
    body = request.json or {}
    sid = str(body.get("source_id") or "").strip()
    w = _watchlist()
    try:
        if not w.stage_of(sid):
            return jsonify({"error": "That job is not on your board."}), 404
        w.set_notes(sid, str(body.get("notes") or ""))
        notes = (w.board_job(sid) or {}).get("notes") or ""
    finally:
        w.close()
    return jsonify({"ok": True, "source_id": sid, "notes": notes})


@app.post("/api/tracker/followup")
@_guard
def tracker_followup():
    """Set or clear the follow-up date. Body: {source_id, date} (date "" clears it)."""
    from datetime import date
    body = request.json or {}
    sid = str(body.get("source_id") or "").strip()
    w = _watchlist()
    try:
        if not w.stage_of(sid):
            return jsonify({"error": "That job is not on your board."}), 404
        try:
            stored = w.set_follow_up(sid, str(body.get("date") or "").strip() or None)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        due = len(w.due_followups(date.today()))
    finally:
        w.close()
    return jsonify({"ok": True, "source_id": sid, "follow_up_at": stored, "due_count": due})


@app.post("/api/tracker/contact")
@_guard
def tracker_contact_add():
    """Add a contact to a job. Body: {source_id, name, role?, email?, linkedin?}. Stored locally."""
    body = request.json or {}
    sid = str(body.get("source_id") or "").strip()
    w = _watchlist()
    try:
        if not w.stage_of(sid):
            return jsonify({"error": "That job is not on your board."}), 404
        try:
            cid = w.add_contact(sid, str(body.get("name") or ""), str(body.get("role") or ""),
                                str(body.get("email") or ""), str(body.get("linkedin") or ""))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        contacts = w.contacts(sid)
    finally:
        w.close()
    return jsonify({"ok": True, "id": cid, "contacts": contacts})


@app.post("/api/tracker/contact/remove")
@_guard
def tracker_contact_remove():
    """Body: {source_id, id}."""
    body = request.json or {}
    sid = str(body.get("source_id") or "").strip()
    w = _watchlist()
    try:
        try:
            cid = int(body.get("id"))
        except (TypeError, ValueError):
            return jsonify({"error": "missing contact id"}), 400
        if not any(c["id"] == cid for c in w.contacts(sid)):
            return jsonify({"error": "No such contact on that job."}), 404
        w.remove_contact(cid)
        contacts = w.contacts(sid)
    finally:
        w.close()
    return jsonify({"ok": True, "contacts": contacts})


@app.get("/api/tracker/activity")
@_guard
def tracker_activity():
    """The stage timeline for one job (?source_id=), newest first."""
    sid = (request.args.get("source_id") or "").strip()
    w = _watchlist()
    try:
        if w.get_job(sid) is None:
            return jsonify({"error": "That job is not in your local list."}), 404
        rows = w.activity(sid)
    finally:
        w.close()
    return _nostore(jsonify({"source_id": sid, "activity": rows}))


@app.get("/api/tracker/metrics")
@_guard
def tracker_metrics():
    """The funnel at the top of the Tracker page: this week and total, applied to interview to
    offer with conversion, median days to a first interview, 12 weekly bars, the weekly goal,
    and how many follow-ups are due today (the sidebar dot)."""
    from datetime import date

    from sourcing.board import funnel_metrics
    today = date.today()
    w = _watchlist()
    try:
        _board_reconcile(w)
        m = funnel_metrics(w.funnel_rows(), today, _board_goal())
        due = w.due_followups(today)
    finally:
        w.close()
    m["due_count"] = len(due)
    m["due"] = [{"source_id": d["source_id"], "company": d.get("company") or "",
                 "title": d.get("title") or "", "follow_up_at": d.get("follow_up_at")} for d in due]
    return _nostore(jsonify(m))


@app.post("/api/tracker/goal")
@_guard
def tracker_goal():
    """The weekly applications target. Body: {target} (0 turns the goal off)."""
    body = request.json or {}
    try:
        target = int(body.get("target"))
    except (TypeError, ValueError):
        return jsonify({"error": "target must be a whole number"}), 400
    _set_prefs({"tracker_weekly_goal": target})
    return jsonify({"ok": True, "target": _board_goal()})


@app.get("/api/tracker/export.csv")
@_guard
def tracker_export():
    """Every job applied to, with dates, as a CSV for an unemployment-insurance work-search
    log (date, employer, position, contact method, link, result)."""
    from datetime import date

    from flask import Response

    from sourcing.board import export_csv
    w = _watchlist()
    try:
        _board_reconcile(w)
        body = export_csv(w.board_jobs())
    finally:
        w.close()
    resp = Response(body, mimetype="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="applications-{date.today().isoformat()}.csv"'
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _board_suggestions(recruiters: list[dict]) -> None:
    """Attach a SUGGESTED stage move to each recruiter reply the inbox surfaced (board job
    matched by company). In place; never moves anything, the person clicks."""
    from sourcing.board import suggest_stage
    if not recruiters:
        return
    w = _watchlist()
    try:
        _board_reconcile(w)
        jobs = w.board_jobs()
    finally:
        w.close()
    for entry in recruiters:
        entry["suggest"] = suggest_stage(entry, jobs)


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
    _start_feed_download()  # the job list downloads in the background from launch, never in a request
    server_only = "--server-only" in sys.argv or os.environ.get("TAILOR_SERVER_ONLY") == "1"
    # TAILOR_PORT lets a test engine run beside the installed app (which owns 57000). Without
    # it, a second engine fails to start and its caller's requests land on the installed app's
    # engine instead (that happened once, mid-test, 2026-10-07).
    port = int(os.environ.get("TAILOR_PORT") or 57000)
    url = f"http://127.0.0.1:{port}"
    if server_only:
        print(f"SponsorJobs: serving at {url} (server only)")
        app.run(host="127.0.0.1", port=port, debug=False)
        return

    from ui.shell import open_app_window

    # Serve on a daemon thread so closing the window ends the process rather than orphaning
    # a server on the port, which is what would strand the next launch.
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False,
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
