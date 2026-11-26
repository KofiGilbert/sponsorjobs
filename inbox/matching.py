"""Which application does a message belong to? (CLAUDE.md section 5, read-first.)

Three lanes, tried in order, and the answer always says which one decided:

  1. ``thread``: the message is in the same conversation as one the app already tied to an
     application (its mailbox thread id, or the Message-ID it replies to / references).
  2. ``domain``: the sender's domain is the employer's own domain, read from the job's careers
     or apply link; or the sender is the applicant tracking system the job is posted on and the
     employer's slug in that link appears in the sender address, or it is the only tracked job
     on that system.
  3. ``role`` / ``company``: the employer's distinctive name ("Octane Lending Inc" -> "octane")
     stands as a whole word in the sender's display name, the subject, the sender's domain
     label or the first lines of the body. Most confirmations come from an applicant tracking
     system's own domain, which says nothing about the employer, so this is the lane that
     places them. ``role`` when the application's role title is in the message as well (as
     sure as a domain match); ``company`` when only the name is. A name of fewer than 4
     letters, or a single everyday word ("First", "American"), counts only with the role.

Pure functions over the message dict and the board rows, so they are exact under test. A
message that matches no lane is never read any further.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from . import detect

THREAD, DOMAIN, ROLE, COMPANY = "thread", "domain", "role", "company"
LANES = (THREAD, DOMAIN, ROLE, COMPANY)
# The lanes sure enough for a plain acknowledgement to move a card without a tap.
SURE_LANES = (THREAD, DOMAIN, ROLE)

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
    job, lane, _ambiguous = match_detail(msg, jobs, threads)
    return job, lane


def match_detail(msg: dict, jobs: list[dict], threads: dict[str, str] | None = None):
    """(job, lane, ambiguous): match_application, plus whether the pick was a guess among
    several applications, or rested on nothing that names the employer (2026-11-26, strict: a
    confirmation must never move a different company's card). ``ambiguous`` is True when
      * two or more tracked applications fit and the message names none of their roles, or
      * the only tie is that the sender is the hiring system the one open job is posted on (a
        nameless "Thank you for applying" from Greenhouse can be for a job applied to outside
        the app, which the board does not know about).
    An ambiguous match still becomes a suggestion; it is never applied without the person's tap."""
    jobs = jobs or []
    by_id = {j.get("source_id"): j for j in jobs if j.get("source_id")}
    for key in thread_keys(msg):
        sid = (threads or {}).get(key)
        if sid and sid in by_id:
            return by_id[sid], THREAD, False

    sender = detect.sender_domain(msg)
    lone = None
    if sender:
        reg = detect.registrable_domain(sender)
        if reg and reg not in _FREE_MAIL and reg not in _BOARDS:
            own = [j for j in jobs if employer_domain(j) == reg]
            if own:
                job, guess = _pick_detail(own, msg)
                return job, DOMAIN, guess
            local = str(msg.get("from") or "").lower()
            on_ats = []
            for j in jobs:
                senders, slug = ats_slug(j)
                if senders and _under(sender, senders):
                    on_ats.append((j, slug))
            slugged = [j for j, slug in on_ats if len(slug) >= 3 and slug in local]
            if slugged:
                job, guess = _pick_detail(slugged, msg)
                return job, DOMAIN, guess
            open_on_ats = [j for j, _ in on_ats if _open(j)]
            if len(open_on_ats) == 1:
                lone = open_on_ats[0]

    job, lane, guess = _match_by_name_detail(msg, jobs, sender)
    if job is not None:
        # the name (and the role) places it; the lone job on that system only if it is the same one
        return job, lane, guess
    if lone is not None:
        return lone, DOMAIN, True
    return None, "", False


# ---------------------------------------------------------------------- the employer's name

# Words that end a registered name and say nothing about which employer it is.
_NAME_TAIL = {
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp", "corporation", "co", "company",
    "plc", "gmbh", "sa", "ag", "nv", "bv", "pty", "pte", "group", "holdings", "holding", "services",
    "service", "solutions", "systems", "technologies", "technology", "tech", "labs", "lending",
    "financial", "finance", "capital", "partners", "associates", "consulting", "international",
    "global", "worldwide", "enterprises", "industries", "us", "usa", "america", "americas", "na",
    "north", "the", "and", "of",
}
# A single everyday word is not a name on its own: it places a message only with the role.
_EVERYDAY = {
    "first", "american", "national", "united", "general", "global", "international", "new", "one",
    "best", "great", "royal", "premier", "advanced", "allied", "pacific", "atlantic", "northern",
    "southern", "eastern", "western", "central", "federal", "city", "state", "home", "direct",
    "target", "match", "open", "next", "team",
    "talent", "careers", "jobs", "work", "people", "data", "health", "energy", "capital", "bank",
}
_WORDS_RE = re.compile(r"[a-z0-9]+")
_HEAD_LINES, _HEAD_CHARS = 8, 800        # "the first lines of the body"
MIN_NAME = 4                             # letters a name needs to count without the role


def _words(text: str) -> list[str]:
    return _WORDS_RE.findall(str(text or "").lower().replace("&", " and "))


def distinctive_name(company: str) -> str:
    """The part of a registered name that says who the employer is: "Octane Lending Inc" ->
    "octane", "Barclays Services Corp" -> "barclays", "First American Financial Corp" ->
    "first american". Trailing legal and generic words go, but never down to a single everyday
    word: "First Financial Inc" stays "first financial"."""
    words = [w for w in _words(company) if w not in ("the",)]
    kept = list(words)
    while len(kept) > 1 and kept[-1] in _NAME_TAIL:
        kept.pop()
    if len(kept) == 1 and kept[0] in _EVERYDAY and len(words) > 1:
        kept = words[:2]
    return " ".join(kept)


def _strong_name(name: str) -> bool:
    """Can this name place a message without the role also matching?"""
    letters = name.replace(" ", "")
    return len(letters) >= MIN_NAME and not (" " not in name and name in _EVERYDAY)


def _has_words(needle: list[str], hay: list[str]) -> bool:
    """Do the words of ``needle`` stand in ``hay`` in order, as whole words?"""
    n = len(needle)
    return bool(n) and any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def _names_role(job: dict, hay: list[str]) -> bool:
    """Is the application's role title in the message? A one-word title ("Analyst") is in every
    other job mail, so only a title of two words or more counts."""
    title = _words(job.get("title") or "")
    return len(title) >= 2 and len("".join(title)) >= 6 and _has_words(title, hay)


def _pick(jobs: list[dict], msg: dict) -> dict:
    """Several tracked applications at one employer: the one whose role is in the message, else
    the one still in play (most recent first)."""
    return _pick_detail(jobs, msg)[0]


def _pick_detail(jobs: list[dict], msg: dict) -> tuple[dict, bool]:
    """(_pick, ambiguous): a guess when more than one application is left after the role."""
    hay = _words(msg.get("subject") or "") + ["|"] + _words(str(msg.get("body") or "")[:4000])
    with_role = [j for j in jobs if _names_role(j, hay)]
    left = with_role or jobs
    return _prefer_open(left), len(left) > 1


def _body_head(body: str) -> str:
    lines = [ln for ln in str(body or "").splitlines() if ln.strip()][:_HEAD_LINES]
    return "\n".join(lines)[:_HEAD_CHARS]


def _match_by_name(msg: dict, jobs: list[dict], sender: str = ""):
    """(job, ROLE | COMPANY) for the tracked application whose employer this message names,
    else (None, ""). Several applications at one employer: the one whose role is in the
    message, else the one still in play (most recent first, as the board lists them)."""
    job, lane, _guess = _match_by_name_detail(msg, jobs, sender)
    return job, lane


def _match_by_name_detail(msg: dict, jobs: list[dict], sender: str = ""):
    """(job, lane, ambiguous) for _match_by_name."""
    display = detect.sender_name(msg) if "<" in str(msg.get("from") or "") else ""
    subject = str(msg.get("subject") or "")
    body = str(msg.get("body") or "")
    reg = detect.registrable_domain(sender) if sender else ""
    # The domain label (barclays.com -> barclays), unless the domain is a mail system's, a job
    # board's or a personal one: those name nobody.
    label = "" if (not reg or reg in _FREE_MAIL or reg in _BOARDS or _ats_of(reg)
                   or any(_under(sender, senders) for _h, senders, _w in _ATS)) else reg.split(".")[0]
    near = _words(display) + ["|"] + _words(subject) + ["|"] + _words(label) + ["|"] + _words(_body_head(body))
    squashed_label = "".join(_words(label))
    everywhere = _words(subject) + ["|"] + _words(body[:4000])

    named, with_role = [], []
    for j in jobs:
        name = distinctive_name(j.get("company") or "")
        if not name:
            continue
        words = name.split()
        if not (_has_words(words, near) or (squashed_label and squashed_label == "".join(words))):
            continue
        if _names_role(j, everywhere):
            with_role.append(j)
        elif _strong_name(name):
            named.append(j)
    if with_role:
        return _prefer_open(with_role), ROLE, len(with_role) > 1
    if named:
        return _prefer_open(named), COMPANY, len(named) > 1
    return None, "", False


def _prefer_open(jobs: list[dict]) -> dict:
    """Several tracked jobs at one employer: the one still in play (most recent stage change
    first, as the board lists them)."""
    for j in jobs:
        if _open(j):
            return j
    return jobs[0]


LANE_LABEL = {THREAD: "Same email thread", DOMAIN: "Sender's domain", ROLE: "Company and role",
              COMPANY: "Company name"}
