"""Which application does a message belong to? (CLAUDE.md section 5, read-first.)

Three lanes, tried in order, and the answer always says which one decided:

  1. ``thread``: the message is in the same conversation as one the app already tied to an
     application (its mailbox thread id, or the Message-ID it replies to / references).
  2. ``domain``: the sender's domain is the employer's own domain, read from the job's careers
     or apply link; or the sender is the applicant tracking system the job is posted on and the
     employer's slug in that link appears in the sender address, or it is the only tracked job
     on that system.
  3. ``company``: the employer's name appears in the From, the subject or the body (the
     original rule, sourcing/board.match_job).

Pure functions over the message dict and the board rows, so they are exact under test. A
message that matches no lane is never read any further.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from sourcing.board import match_job

from . import detect

THREAD, DOMAIN, COMPANY = "thread", "domain", "company"
LANES = (THREAD, DOMAIN, COMPANY)

# Senders that say nothing about the employer: personal mail and the big job boards. A job
# whose link points at one of these has no employer domain to match on.
_FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
              "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com"}
_BOARDS = {"linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com",
           "dice.com", "wellfound.com", "angel.co", "remotive.com", "adzuna.com", "simplyhired.com",
           "builtin.com", "otta.com", "welcometothejungle.com", "freehire.dev", "sponsorjobs.ai"}

# Applicant tracking systems: the hosts a job link may use, the domains their mail comes from,
# and where the employer's slug sits in the link ("path" = first path segment, "sub" = the
# first host label). Detect's ATS families decide the sender side; this adds the slug.
_ATS = (
    (("greenhouse.io",), ("greenhouse.io", "greenhouse-mail.io"), "path"),
    (("lever.co",), ("lever.co", "hire.lever.co"), "path"),
    (("ashbyhq.com",), ("ashbyhq.com",), "path"),
    (("workable.com",), ("workable.com", "workablemail.com"), "path"),
    (("smartrecruiters.com",), ("smartrecruiters.com",), "path"),
    (("recruitee.com",), ("recruitee.com", "recruiteemail.com"), "sub"),
    (("myworkdayjobs.com", "myworkday.com", "workday.com"), ("myworkday.com", "myworkdayjobs.com", "workday.com"), "sub"),
    (("icims.com",), ("icims.com",), "sub"),
    (("jobvite.com",), ("jobvite.com",), "path"),
    (("taleo.net",), ("taleo.net",), "sub"),
    (("successfactors.com", "successfactors.eu"), ("successfactors.com", "successfactors.eu", "sap.com"), "sub"),
    (("bamboohr.com",), ("bamboohr.com",), "sub"),
    (("breezy.hr",), ("breezy.hr",), "sub"),
    (("teamtailor.com",), ("teamtailor.com", "teamtailor-mail.com"), "sub"),
)
_GENERIC_SLUGS = {"jobs", "careers", "boards", "job-boards", "apply", "www", "en", "us", "embed", "o", "j"}


def _under(domain: str, suffixes) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in suffixes)


def _host(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _ats_of(host: str):
    for hosts, senders, where in _ATS:
        if _under(host, hosts):
            return hosts, senders, where
    return None


def employer_domain(job: dict) -> str:
    """The employer's own registrable domain from the job's link, or "" when the link is an
    applicant tracking system or a job board (those say nothing about the employer)."""
    for field in ("url", "apply_url", "company_url"):
        host = _host(job.get(field) or "")
        if not host or _ats_of(host):
            continue
        dom = detect.registrable_domain(host)
        if dom and dom not in _BOARDS and dom not in _FREE_MAIL:
            return dom
    return ""


def ats_slug(job: dict) -> tuple[tuple, str]:
    """(the ATS sender domains, the employer slug in the link) for a job posted on an applicant
    tracking system, else ((), "")."""
    url = str(job.get("url") or "")
    host = _host(url)
    hit = _ats_of(host) if host else None
    if not hit:
        return (), ""
    _hosts, senders, where = hit
    slug = ""
    if where == "sub":
        first = host.split(".")[0]
        slug = "" if first in _GENERIC_SLUGS else first
    else:
        try:
            parts = [p for p in (urlparse(url if "://" in url else "https://" + url).path or "").split("/") if p]
        except ValueError:
            parts = []
        for p in parts:
            if p.lower() not in _GENERIC_SLUGS:
                slug = p.lower()
                break
    return senders, slug


def thread_keys(msg: dict) -> list[str]:
    """The keys that tie a message to a conversation: its mailbox thread id ("t:"), and the
    Message-IDs it answers or references ("m:"). Lowercased, angle brackets dropped."""
    keys = []
    thread = str(msg.get("thread_id") or "").strip().lower()
    if thread:
        keys.append(f"t:{thread}")
    refs = f"{msg.get('in_reply_to') or ''} {msg.get('references') or ''}"
    for mid in re.findall(r"<([^<>\s]+)>", refs) or refs.split():
        mid = mid.strip("<> ").lower()
        if mid and f"m:{mid}" not in keys:
            keys.append(f"m:{mid}")
    return keys


def _open(job: dict) -> bool:
    return (job.get("stage") or "") not in ("", "archived")


def match_application(msg: dict, jobs: list[dict], threads: dict[str, str] | None = None):
    """(job, lane) for the first lane that places the message, else (None, "").

    ``threads`` is the board's {thread key: source_id} (Watchlist.thread_index). Only jobs on
    the board are candidates."""
    by_id = {j.get("source_id"): j for j in jobs or [] if j.get("source_id")}
    for key in thread_keys(msg):
        sid = (threads or {}).get(key)
        if sid and sid in by_id:
            return by_id[sid], THREAD

    sender = detect.sender_domain(msg)
    if sender:
        reg = detect.registrable_domain(sender)
        if reg and reg not in _FREE_MAIL and reg not in _BOARDS:
            own = [j for j in jobs if employer_domain(j) == reg]
            if own:
                return _prefer_open(own), DOMAIN
            local = str(msg.get("from") or "").lower()
            on_ats = []
            for j in jobs:
                senders, slug = ats_slug(j)
                if senders and _under(sender, senders):
                    on_ats.append((j, slug))
            slugged = [j for j, slug in on_ats if len(slug) >= 3 and slug in local]
            if slugged:
                return _prefer_open(slugged), DOMAIN
            open_on_ats = [j for j, _ in on_ats if _open(j)]
            if len(open_on_ats) == 1:
                return open_on_ats[0], DOMAIN

    text = " ".join(str(msg.get(k) or "") for k in ("from", "subject", "body"))[:4000]
    job = match_job(text, jobs)
    if job is not None:
        return job, COMPANY
    return None, ""


def _prefer_open(jobs: list[dict]) -> dict:
    """Several tracked jobs at one employer: the one still in play (most recent stage change
    first, as the board lists them)."""
    for j in jobs:
        if _open(j):
            return j
    return jobs[0]


LANE_LABEL = {THREAD: "Same email thread", DOMAIN: "Sender's domain", COMPANY: "Company name"}
