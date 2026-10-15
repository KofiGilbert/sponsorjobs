"""Which roles the Auto-apply run tailors: the same jobs the person sees on Jobs.

The run used to pick only from the LOCAL watchlist, so a fresh install (whose Jobs page shows
the central feed) dead-ended on "No roles in your feed yet". This module picks from the board
the person actually sees (the central feed, or the local board when no feed is configured)
plus their saved jobs, saved first, narrowed by their saved Jobs search and today's focus, and:

* never queues a role twice: a job already in the review queue, approved or applied is
  skipped by its source_id AND by the cross-feed dedup keys (sourcing/dedup.py), so the
  same opening reached through a second feed is caught too;
* never queues a poor match: each role is scored with the same fit the Jobs detail shows,
  and one below the person's fit floor is skipped;
* says how many of the matches can really be submitted automatically, from the shipped
  submission policy on each role's apply URL (CLAUDE.md section 7), never from the feed name.

Pure: the board, the records, the scorer and the policy are all passed in, so this runs
offline in tests.
"""

from __future__ import annotations

import re
import time
from functools import lru_cache

from sourcing.dedup import role_key, url_key
from sourcing.filters import apply_facets

DEFAULT_MIN_FIT = 60
MIN_FIT_RANGE = (0, 95)
MAX_SCORED = 60           # JD fetches per run, so a run on a 70k board stays bounded
TIME_BUDGET_S = 60.0
POOL = 300                # roles the fit is scored on, after the cheap prefilter over the board
SPONSOR_BONUS = 0.25      # how far a known sponsor moves up the cheap prefilter (title fit is 0..1)
PENDING = "pending"       # a score_fn answer: this role's description is not here yet

_TOKEN = re.compile(r"[a-z0-9+#]+")


def clamp_min_fit(value, default: int = DEFAULT_MIN_FIT) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(MIN_FIT_RANGE[0], min(MIN_FIT_RANGE[1], v))


# --------------------------------------------------------------------------- what is taken
_LEGAL = frozenset("inc incorporated llc ltd plc co corp corporation company group holdings "
                   "systems technologies technology the".split())
_REQ = re.compile(r"(\d{5,})")


def req_key(company: str, title: str, source_id: str) -> str:
    """The employer's requisition number, for the one opening the shared dedup keys miss: the
    same posting listed on the company's career site AND its Workday board, under two company
    spellings ("Cisco" / "Cisco Systems Inc") and two location spellings. Found on the real
    feed in Oct 2026. Keyed on the company's core name + title + the posting's number, so two
    different openings never merge."""
    tail = str(source_id or "").rsplit(":", 1)[-1]
    m = _REQ.findall(tail)
    words = [w for w in _TOKEN.findall((company or "").lower()) if w not in _LEGAL]
    title_k = " ".join(_TOKEN.findall((title or "").lower()))
    if not (m and words and title_k):
        return ""
    return f"req:{words[0]}::{title_k}::{m[-1]}"


def _record_keys(rec: dict) -> list[str]:
    """Every identity a tailored record carries: its job's source_id, the apply URL's key, and
    the company + role + location key. An older record that stored no location and no
    source_id falls back to company + role alone, the only identity it has."""
    sj = rec.get("source_job") or {}
    keys = []
    sid = str(sj.get("source_id") or "").strip()
    if sid:
        keys.append("sid:" + sid)
    uk = url_key(sj.get("url") or "")
    if uk:
        keys.append(uk)
    role = str(rec.get("role") or "").split(" · ")[0].strip()
    rq = req_key(rec.get("company") or "", role, sid)
    if rq:
        keys.append(rq)
    loc = str(sj.get("location") or "").strip()
    if loc or not sid:
        rk = role_key(rec.get("company") or "", role, loc)
        if rk:
            keys.append(rk)
    return keys


def _job_keys(job: dict, loose: bool = True) -> list[str]:
    """A job's identities. ``loose`` adds the location-free company + role key, which only
    an older record without a location can hold."""
    keys = []
    sid = str(job.get("source_id") or "").strip()
    if sid:
        keys.append("sid:" + sid)
    uk = url_key(job.get("url") or "")
    if uk:
        keys.append(uk)
    rq = req_key(job.get("company") or "", job.get("title") or "", sid)
    if rq:
        keys.append(rq)
    for loc in ((job.get("location") or ""), "") if loose else ((job.get("location") or ""),):
        rk = role_key(job.get("company") or "", job.get("title") or "", loc)
        if rk and rk not in keys:
            keys.append(rk)
    return keys


_URL_HEAD = re.compile(r"^https?://(?:www\.)?")


def _cheap_url(url: str) -> str:
    """Host + path, lowercased, without query or fragment: equal url_key()s always give equal
    cheap keys, so this is a safe first gate before the full key is built."""
    u = _URL_HEAD.sub("", str(url or "").strip().lower())
    return u.split("#", 1)[0].split("?", 1)[0].rstrip("/")


def _company_words(company: str) -> set[str]:
    return {w for w in _TOKEN.findall((company or "").lower()) if w not in _LEGAL}


class TakenIndex(dict):
    """{key: "applied" | "queued"} plus a cheap gate. Building every key for every row of a
    100k-row board took ~2.5 s; a row can only share a key with a record when it shares the
    source_id, the apply URL's host + path, or a company word, so only those rows get keys."""

    def __init__(self):
        super().__init__()
        self.sids: set[str] = set()
        self.urls: set[str] = set()
        self.words: set[str] = set()

    def may_hold(self, job: dict) -> bool:
        if not self:
            return False
        if str(job.get("source_id") or "").strip() in self.sids:
            return True
        cu = _cheap_url(job.get("url") or "")
        if cu and cu in self.urls:
            return True
        words = _company_words(job.get("company") or "")
        return not words or not words.isdisjoint(self.words)


def taken_index(records: list[dict]) -> TakenIndex:
    """{key: "applied" | "queued"} over every record the person already has. "applied" wins
    when two records share a key. Any record counts: a package that exists for a job is
    either waiting in the queue or already sent, and a second one is a duplicate."""
    idx = TakenIndex()
    for rec in records or []:
        status = "applied" if _is_applied(rec) else "queued"
        for k in _record_keys(rec):
            if idx.get(k) != "applied":
                idx[k] = status
        sj = rec.get("source_job") or {}
        sid = str(sj.get("source_id") or "").strip()
        if sid:
            idx.sids.add(sid)
        cu = _cheap_url(sj.get("url") or "")
        if cu:
            idx.urls.add(cu)
        idx.words |= _company_words(rec.get("company") or "")
    return idx


def _is_applied(rec: dict) -> bool:
    if rec.get("status") == "applied":
        return True
    return rec.get("stage") in ("applied", "interviewing", "offer", "rejected")


def taken_status(job: dict, idx: dict[str, str]) -> str | None:
    if isinstance(idx, TakenIndex) and not idx.may_hold(job):
        return None
    hit = None
    for k in _job_keys(job):
        st = idx.get(k)
        if st == "applied":
            return "applied"
        hit = hit or st
    return hit


# --------------------------------------------------------------------------- narrowing
def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if len(t) > 1}


def _matches_focus(job: dict, titles: list[str], locations: list[str]) -> bool:
    title = (job.get("title") or "").lower()
    if titles and not any(re.search(r"(?<![a-z0-9])" + re.escape(t.lower()) + r"(?![a-z0-9])", title)
                          for t in titles):
        return False
    if locations:
        loc = (job.get("location") or "").lower()
        remote = (job.get("remote") or "").lower() == "remote" or "remote" in loc
        if not any(l.lower() in loc or (l.lower() == "remote" and remote) for l in locations):
            return False
    return True


def is_sponsor(job: dict) -> bool:
    """A role the person sees as sponsoring: an employer with a public visa record, or an ad that
    says sponsorship is available. Exactly the chip the row shows: the aggregator's own
    ``sponsorship_stated`` flag alone is shown as "No sponsorship record" (it was wrong for most
    flagged ads, sourcing/adstance.py), so it does not count here either. An ad that refuses
    sponsorship or requires citizenship never counts."""
    stance = job.get("ad_stance")
    if stance in ("not_offered", "restricted"):
        return False
    return bool(job.get("visa")) or stance == "offered"


@lru_cache(maxsize=1 << 18)
def _title_words(title: str) -> frozenset[str]:
    """A title's words, cached: the board repeats titles ("Software Engineer" thousands of
    times), and the prefilter reads every title on every Up next request."""
    return frozenset(_tokens(title))


def profile_rank(job: dict, title_words: set[str], skill_words: set[str]) -> float:
    """0..1 from the job TITLE alone vs the profile's role titles and skills (no JD needed):
    orders tens of thousands of roles cheaply so the JD scoring is spent on likely fits."""
    words = _title_words(job.get("title") or "")
    if not words:
        return 0.0
    hit = len(words & title_words) + 0.5 * len((words - title_words) & skill_words)
    return min(1.0, hit / len(words))


def candidates(board: list[dict], saved: list[dict], *, facets: dict | None = None,
               titles=(), locations=(), sponsor_only: bool = False,
               targets: dict | None = None, learn: dict | None = None,
               needs_sponsor: bool = False) -> list[dict]:
    """The roles a run may tailor, best first: the person's saved jobs (in their order), then
    the board ranked by how well each title fits the profile (stable, so the board's recency
    order breaks ties). Each is narrowed by the saved Jobs search (``facets``) and today's
    focus. Rows are copied, so the caller can annotate them freely.

    ``learn`` is the person's skip reasons and company lists (autoapply/learning.py): when given,
    "Never apply to" companies and roles closed to a visa holder (when ``needs_sponsor``) are
    left out, prioritized companies move up and roles like the ones skipped move to the back."""
    facets = {k: v for k, v in (facets or {}).items() if v not in (None, "", 0, [], False)}
    titles = [t for t in (titles or []) if str(t).strip()]
    locations = [l for l in (locations or []) if str(l).strip()]
    targets = targets or {}
    tw, sw = set(targets.get("title_words") or ()), set(targets.get("skill_words") or ())

    def narrow(rows):
        rows = apply_facets(rows, **facets) if facets else list(rows)
        rows = [j for j in rows if _matches_focus(j, titles, locations)]
        if sponsor_only:
            rows = [j for j in rows if is_sponsor(j)]
        return rows

    seen: set[str] = set()
    out: list[dict] = []
    for j in narrow(saved or []):
        sid = str(j.get("source_id") or "")
        if sid and sid in seen:
            continue
        seen.add(sid)
        out.append({**j, "saved": True})
    rest = []
    for j in narrow(board or []):
        sid = str(j.get("source_id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        rest.append(j if learn is not None else dict(j))   # shape() copies the rows it keeps
    # The cheap prefilter over the whole board: title fit to the profile, with known sponsors
    # moved up (a person who left "Visa sponsors only" off still sees sponsors first among
    # equals). Stable, so the board's recency order breaks ties.
    if tw or sw:
        ranked = [(profile_rank(j, tw, sw) + (SPONSOR_BONUS if is_sponsor(j) else 0.0), i, j)
                  for i, j in enumerate(rest)]
    else:
        ranked = [(SPONSOR_BONUS if is_sponsor(j) else 0.0, i, j) for i, j in enumerate(rest)]
    ranked.sort(key=lambda x: (-x[0], x[1]))
    rest = [j for _r, _i, j in ranked]
    if learn is not None:
        from autoapply.learning import shape
        return shape(out + rest, learn, needs_sponsor=needs_sponsor)
    return out + rest


# --------------------------------------------------------------------------- the pick
def reach(rows: list[dict], policy_fn) -> dict:
    """How many of ``rows`` the shipped submission policy would auto-submit, by apply URL."""
    auto = sum(1 for j in rows if policy_fn(j.get("url") or "") == "auto")
    return {"auto": auto, "total": len(rows)}


def pick(rows: list[dict], *, records: list[dict], score_fn, count: int, min_fit: int,
         max_scored: int = MAX_SCORED, time_budget: float = TIME_BUDGET_S,
         clock=time.monotonic) -> dict:
    """Walk ``rows`` (already ordered by ``candidates``) and take up to ``count`` roles that
    are not already taken and score at or above ``min_fit``. ``score_fn(job)`` returns the
    fit percent (0..100) or None when the role cannot be scored (no description, or one that
    names no skills); it may also fill ``job["jd_text"]`` for the tailoring that follows.

    Returns {"jobs", "applied", "queued", "below", "unscored", "scored", "available"}:
    ``applied`` / ``queued`` count candidates skipped because a package already exists,
    ``below`` / ``unscored`` count roles scored this run and passed over, and ``available``
    is how many candidates were not taken at all (what reach is measured on)."""
    idx = taken_index(records)
    fresh, applied, queued = [], 0, 0
    for j in rows:
        st = taken_status(j, idx)
        if st == "applied":
            applied += 1
        elif st == "queued":
            queued += 1
        else:
            fresh.append(j)
    picked, below, unscored, scored = [], 0, 0, 0
    run_keys: set[str] = set()      # the same opening twice in one board (two feeds) counts once
    start = clock()
    for j in fresh:
        if len(picked) >= count or scored >= max_scored or (clock() - start) > time_budget:
            break
        keys = _job_keys(j, loose=False)
        if any(k in run_keys for k in keys):
            continue
        scored += 1
        try:
            pct = score_fn(j)
        except Exception:   # noqa: BLE001 - one unreadable posting never stops the run
            pct = None
        if pct is None:
            unscored += 1
            continue
        j["fit"] = int(pct)
        if pct < min_fit:
            below += 1
            continue
        run_keys.update(keys)
        picked.append(j)
    return {"jobs": picked, "applied": applied, "queued": queued, "below": below,
            "unscored": unscored, "scored": scored, "available": len(fresh)}


def rank(rows: list[dict], *, records: list[dict], score_fn, count: int, min_fit: int,
         pool: int = POOL) -> dict:
    """Up next and the run: score the fit on the first ``pool`` untaken roles of ``rows``
    (already ordered by ``candidates``, the cheap prefilter over the whole board), keep those at
    or above ``min_fit`` and return the best ``count``: saved roles first, then prioritized
    companies, then by fit (highest first), a known sponsor ahead of an equal fit without one.

    ``score_fn(job)`` returns the fit (0..100), None when the role cannot be scored, or PENDING
    when its description is not on this machine yet (the caller scores those later). The
    same opening listed twice (two feeds) is scored once.

    Returns {"jobs", "applied", "queued", "below", "unscored", "pending", "scored",
    "available", "pool"}."""
    idx = taken_index(records)
    fresh, applied, queued = [], 0, 0
    for j in rows:
        st = taken_status(j, idx)
        if st == "applied":
            applied += 1
        elif st == "queued":
            queued += 1
        else:
            fresh.append(j)
    kept, below, unscored, pending, scored = [], 0, 0, 0, 0
    run_keys: set[str] = set()
    head = fresh[:max(pool, 0)]
    for pos, j in enumerate(head):
        j = dict(j)                 # the fit is written on a copy: ``rows`` may be a cached list
        keys = _job_keys(j, loose=False)
        if any(k in run_keys for k in keys):
            continue
        run_keys.update(keys)
        try:
            pct = score_fn(j)
        except Exception:   # noqa: BLE001 - one unreadable posting never stops the list
            pct = None
        if pct == PENDING:
            pending += 1
            continue
        scored += 1
        if pct is None:
            unscored += 1
            continue
        j["fit"] = int(pct)
        if pct < min_fit:
            below += 1
            continue
        kept.append((pos, j))
    kept.sort(key=lambda x: (not x[1].get("saved"), not x[1].get("priority"), -x[1]["fit"],
                             not is_sponsor(x[1]), x[0]))
    return {"jobs": [j for _p, j in kept[:count]], "applied": applied, "queued": queued,
            "below": below, "unscored": unscored, "pending": pending, "scored": scored,
            "available": len(fresh), "pool": len(head)}


def skip_summary(result: dict, min_fit: int) -> str:
    """One plain line: "12 below your fit floor of 60%, 3 already applied"."""
    parts = []
    if result.get("below"):
        parts.append(f"{result['below']} below your fit floor of {min_fit}%")
    if result.get("unscored"):
        parts.append(f"{result['unscored']} with no description to score")
    if result.get("queued"):
        parts.append(f"{result['queued']} already in your queue")
    if result.get("applied"):
        parts.append(f"{result['applied']} already applied")
    return ", ".join(parts)
