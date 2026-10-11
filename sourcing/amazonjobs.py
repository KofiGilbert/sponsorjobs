"""Amazon's own job search JSON (GREEN lane, CLAUDE.md §6). Amazon is the largest H-1B sponsor
and hires through amazon.jobs, not an ATS feed. The site has no sitemap and no JobPosting block,
but the public, keyless JSON its own search page reads is open to crawlers (robots.txt disallows
only /internal; verified 2026-10-11):

  GET https://www.amazon.jobs/en/search.json?base_query=&country=USA&result_limit=100
      &offset=0&sort=recent
  -> {"hits": N, "jobs": [{"id_icims", "title", "company_name", "city", "state",
      "normalized_location", "job_path", "posted_date": "October 10, 2026", "description",
      "basic_qualifications", "preferred_qualifications", "job_schedule_type", ...}]}

Each page carries the full description, so rows arrive checked (no detail fetch). Pages of 100,
newest first, one request at a time with a gap, up to AMAZON_MAX_JOBS rows per run (the API never
answers past 10,000). The board id is the country code the search is limited to ("USA").
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime

from .ats import _job, fetch_json, fetch_with_backoff, html_to_text

SOURCE = "amazonjobs"
BASE = "https://www.amazon.jobs"
PAGE = 100
REQUEST_GAP = 1.0
DEFAULT_MAX_JOBS = 1000           # env AMAZON_MAX_JOBS; the crawl stores at most JOBS_MAX_PER_COMPANY (500 in the feed) per run
API_CEILING = 10000               # the search never pages past this many hits
_COUNTRY = re.compile(r"^[A-Z]{2,3}$")


def search_url(country: str, offset: int, limit: int = PAGE) -> str:
    if not _COUNTRY.match(country or ""):
        raise ValueError(f"invalid country code {country!r}")
    return (f"{BASE}/en/search.json?base_query=&country={country}&result_limit={limit}"
            f"&offset={offset}&sort=recent")


def _posted(text: str) -> str:
    try:
        return datetime.strptime((text or "").strip(), "%B %d, %Y").date().isoformat()
    except ValueError:
        return ""


def _location(hit: dict) -> str:
    city, state = (hit.get("city") or "").strip(), (hit.get("state") or "").strip()
    if city and state:
        return f"{city}, {state}, US"
    return (hit.get("normalized_location") or hit.get("location") or "").strip()


def _text(hit: dict) -> str:
    parts = [html_to_text(hit.get("description") or "")]
    for label, key in (("Basic qualifications", "basic_qualifications"),
                       ("Preferred qualifications", "preferred_qualifications")):
        body = html_to_text(hit.get(key) or "")
        if body.strip():
            parts.append(f"{label}\n{body}")
    return "\n\n".join(p for p in parts if p.strip())


def row_from_hit(country: str, hit: dict, company: str = "") -> dict | None:
    jid = str(hit.get("id_icims") or "").strip()
    title = (hit.get("title") or "").strip()
    if not jid or not title:
        return None
    path = hit.get("job_path") or f"/en/jobs/{jid}"
    job = _job(SOURCE, country, jid, (hit.get("company_name") or company or "Amazon").strip(),
               title, _location(hit), "", BASE + path, _text(hit), _posted(hit.get("posted_date")))
    if hit.get("job_schedule_type"):
        job["employment_type"] = str(hit["job_schedule_type"])
    return job


def amazon_jobs(board_id: str = "USA", company: str = "", fetch=fetch_json, *, max_jobs: int | None = None,
                sleep=time.sleep, **_) -> list[dict]:
    """The newest `max_jobs` Amazon postings in `board_id` (a country code), full text included."""
    if max_jobs is None:
        try:
            max_jobs = int(os.environ.get("AMAZON_MAX_JOBS", DEFAULT_MAX_JOBS))
        except ValueError:
            max_jobs = DEFAULT_MAX_JOBS
    rows: list[dict] = []
    seen: set[str] = set()
    offset = 0
    total = None
    while len(rows) < max_jobs and offset < API_CEILING:
        if offset:
            sleep(REQUEST_GAP)
        data = fetch_with_backoff(fetch, search_url(board_id, offset), sleep)
        hits = data.get("jobs") or []
        if total is None:
            try:
                total = int(data.get("hits") or 0)
            except (TypeError, ValueError):
                total = 0
        for h in hits:
            job = row_from_hit(board_id, h, company)
            if job and job["source_id"] not in seen:
                seen.add(job["source_id"])
                rows.append(job)
        offset += PAGE
        if len(hits) < PAGE or offset >= total:
            break
    return rows[:max_jobs]
