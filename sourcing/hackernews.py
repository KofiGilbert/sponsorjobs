"""Hacker News "Ask HN: Who is hiring?" as a job source (GREEN lane: official public APIs).

Once a month the `whoishiring` account posts "Ask HN: Who is hiring? (Month Year)" and employers
answer with one top-level comment each, by convention a first line like

    Acme (YC S24) | Senior Backend Engineer | San Francisco, CA | ONSITE | VISA | https://acme.com

This module reads that thread through the HN search API that Hacker News itself links to
(Algolia, https://hn.algolia.com/api, keyless, robots.txt answers 404 = no rules):

  1. GET /api/v1/search_by_date?tags=story,author_whoishiring  -> the newest "Who is hiring?" story
  2. GET /api/v1/items/{story id}                              -> the whole thread in ONE request

Two requests a refresh, and only when the on-disk cache is older than CACHE_HOURS (6): the feed
builds every 3 hours, so the thread is asked for at most 4 times a day (HN API etiquette: cache,
low rate, no per-comment fan-out; the Firebase API would need one request per comment).

What a row carries (facts only). Y Combinator's site terms (ycombinator.com/legal) do not let
us republish HN content for a commercial purpose, so a row holds only facts read from the post's
first line (company, role, location, remote, a sponsorship signal, pay when stated) and a link
back to the comment. The description is a few sentences WE write from those facts, never the
poster's text; the person reads the full post on Hacker News (the row's `url`).

Sponsorship signal (`sponsorship_stated`): True when the post says it sponsors ("VISA" in the
header, "visa sponsorship", "we sponsor visas", "H-1B sponsorship"); False when it says it will
not ("no visa", "not able to sponsor", "no sponsorship"; a no beats a yes, so "H-1B transfers
only; no new visa sponsorship" is False); None when it is silent. The description states the
signal in words sourcing/adstance.py reads, so the feed's own sponsor gating (quality.
is_sponsor_relevant: an aggregator row needs a sponsor badge or a stated yes) applies unchanged.
A post that requires US citizenship or a clearance is dropped here.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import time
from pathlib import Path

from .ats import _fmt_salary, _job, fetch_json, html_to_text

SOURCE = "hackernews"
API = "https://hn.algolia.com/api/v1"
SEARCH_URL = f"{API}/search_by_date?tags=story,author_whoishiring&hitsPerPage=10"
ITEM_URL = API + "/items/{id}"
POST_URL = "https://news.ycombinator.com/item?id={id}"
THREAD_RE = re.compile(r"^\s*Ask HN:\s*Who is hiring\?", re.I)
CACHE_FILE = "hn_whoishiring.json"
CACHE_HOURS = 6.0               # env HN_CACHE_HOURS
MAX_THREAD_DAYS = 45            # an older thread is stale (the feed drops 45-day-old rows anyway)
REQUEST_GAP = 1.0               # seconds between the two requests
MAX_TITLE = 120

# -- the sponsorship signal ------------------------------------------------------------ #

_NEG = re.compile(
    r"(\bno\b|\bnot\b|\bwithout\b|\bcannot\b|\bcan'?t\b|\bunable to\b|\bwon'?t\b|\bdon'?t\b"
    r"|\bdoesn'?t\b|\bdo not\b|\bnor\b)[^.|;\n]{0,40}?\b(visas?|sponsor\w*|h-?1b)\b", re.I)
_NEG_AFTER = re.compile(r"\b(visa|sponsorship)\b[^.|;\n]{0,15}?\b(not (available|offered|possible)|"
                        r"unavailable)\b", re.I)
_POS = re.compile(
    r"(\bvisa sponsorship\b|\bsponsor(s|ing)?\s+(h-?1b|visas?|work visas?)\b|\bh-?1b\s+(sponsorship|"
    r"transfers?)\b|\b(we|will|can|do|happy to|able to)\s+(\w+\s+){0,2}sponsor\b|"
    r"\bvisa (support|assistance|possible|available)\b)", re.I)
_VISA_SEGMENT = re.compile(r"^\W*visas?\b", re.I)     # a header segment that is "VISA ..."


def sponsorship_signal(header_parts: list[str], text: str) -> bool | None:
    """True / False / None (silent) from the post's header segments and full text."""
    if _NEG.search(text) or _NEG_AFTER.search(text):
        return False
    if any(_VISA_SEGMENT.match(p) for p in header_parts) or _POS.search(text):
        return True
    return None


# -- parsing one comment ---------------------------------------------------------------- #

_HREF = re.compile(r'href="([^"]+)"', re.I)
_URL_SEG = re.compile(r"^(https?://\S+|www\.\S+|[\w-]+(\.[\w-]+)*\.(com|io|ai|co|dev|org|net|app|"
                      r"tech|xyz|so|sh|us|health|bio)(/\S*)?)$", re.I)
_REMOTE = re.compile(r"\b(remote|onsite|on-site|on site|hybrid|in[- ]office|in person)\b", re.I)
_TYPE = re.compile(r"^\W*(full[- ]?time|part[- ]?time|contract(or)?|freelance|permanent|ft|pt|"
                   r"intern(ship)?s?|full[- ]?time\W+contract|contract\W+full[- ]?time)\W*$", re.I)
_PAY = re.compile(r"[$£€]|\b\d{2,3}\s?k\b|\bequity\b|\bsalary\b|\busd\b", re.I)
_ROLE = re.compile(
    r"\b(engineers?|engineering|developers?|scientists?|designers?|managers?|leads?|architects?|"
    r"analysts?|researchers?|sre|devops|product|head of|directors?|founding|swe|cto|vp|writers?|"
    r"recruiters?|marketing|sales|operations|ops|support|specialists?|consultants?|"
    r"administrators?|technicians?|accountants?|counsel|roles?|positions?|hiring|programmers?|"
    r"interns?|full[- ]?stack|backend|frontend|mobile|ios|android|ml|ai|data|security|"
    r"infrastructure|platform|qa|test)\b", re.I)
_LOC = re.compile(
    r"(,\s*[A-Z]{2}\b|\b(usa?|united states|nyc|new york|san francisco|sf|bay area|seattle|boston|"
    r"austin|chicago|los angeles|la|denver|atlanta|miami|washington|dc|toronto|london|berlin|"
    r"paris|amsterdam|europe|emea|canada|uk|india|anywhere|worldwide|global|americas|remote)\b)",
    re.I)
_PAREN = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")
_URL_ANY = re.compile(r"\s*(https?://\S+|www\.\S+)")
# A role named in the post's body when the first line names none ("We're hiring Typescript
# Engineers"): up to three capitalised words before a role noun.
_BODY_ROLE = re.compile(
    r"\b((?:Senior|Staff|Principal|Lead|Founding|Junior|Sr\.?)\s+)?((?:[A-Z][\w+#./-]*\s+){0,3})"
    r"(Engineers?|Developers?|Scientists?|Designers?|Product Managers?|Architects?|Researchers?)\b")
_SEPS = (" | ", "|", " — ", " – ", " -- ", " - ")


def _header(raw_html: str) -> str:
    """The post's first line: text before the first paragraph break, tags stripped."""
    first = re.split(r"<p>|<br\s*/?>|\n", raw_html or "", maxsplit=1)[0]
    return _html.unescape(re.sub(r"<[^>]+>", " ", first)).strip()


def _split(header: str) -> list[str]:
    for sep in _SEPS:
        parts = [p.strip(" \t·•|") for p in header.split(sep)]
        parts = [p for p in parts if p]
        if len(parts) >= 2:
            return parts
    return [header.strip()] if header.strip() else []


def _company(seg: str) -> str:
    name = _PAREN.sub("", seg)
    name = re.sub(r"\s*(https?://\S+|www\.\S+)", "", name).strip(" -–—:,|")
    return re.sub(r"\s{2,}", " ", name)


def _remote_value(segs: list[str]) -> str:
    blob = " ".join(segs).lower()
    if "hybrid" in blob:
        return "hybrid"
    if re.search(r"\bremote\b", blob):
        return "remote"
    if re.search(r"\b(onsite|on-site|on site|in[- ]office|in person)\b", blob):
        return "onsite"
    return ""


def _location(seg: str) -> str:
    """A location segment with its work-mode words tidied: "REMOTE (US)" -> "Remote (US)"."""
    s = re.sub(r"\b(fully\s+)?remote\b", "Remote", seg, flags=re.I)
    s = re.sub(r"\b(onsite|on-site|on site)\b", "Onsite", s, flags=re.I)
    s = re.sub(r"\bhybrid\b", "Hybrid", s, flags=re.I)
    return re.sub(r"\s{2,}", " ", s).strip(" -–—:,")


def parse_comment(c: dict) -> dict | None:
    """One top-level comment -> a feed row (facts only), or None when it is not a job post
    (deleted, a reply, no role on its first line) or is closed to a visa holder."""
    raw = c.get("text") or ""
    cid = c.get("id")
    if not raw or not cid:
        return None
    header = _header(raw)
    parts = _split(header)
    if len(parts) < 2:
        return None
    company = _company(parts[0])
    if not company or len(company) > 80 or re.match(r"^(https?://|www\.)", company, re.I) \
            or _ROLE.fullmatch(company):
        return None
    paren = [m.strip(" ()[]") for m in _PAREN.findall(parts[0])]
    rest = parts[1:]
    links = [u for u in (_html.unescape(h) for h in _HREF.findall(raw))
             if "ycombinator.com" not in u]
    title = loc = ""
    mode_segs: list[str] = []
    header_flags: list[str] = []
    for seg in rest:
        if _URL_SEG.match(seg):
            continue
        if _VISA_SEGMENT.match(seg) or re.search(r"\b(no )?visa\b", seg, re.I) and len(seg) < 40 \
                and not _ROLE.search(seg):
            header_flags.append(seg)
            continue
        if _TYPE.match(seg) or (_PAY.search(seg) and not _ROLE.search(seg) and not _LOC.search(seg)):
            continue
        has_mode = bool(_REMOTE.search(seg))
        if has_mode:
            mode_segs.append(seg)
        if not title and _ROLE.search(seg) and not (has_mode and not re.search(
                r"\b(engineer|developer|scientist|designer|manager)\b", seg, re.I)):
            title = seg
            continue
        if not loc and (_LOC.search(seg) or has_mode):
            loc = _location(seg)
    for p in paren:                          # "PrairieLearn (Remote US) -- ..." names it here
        if _REMOTE.search(p):
            mode_segs.append(p)
        if not loc and (_LOC.search(p) or _REMOTE.search(p)):
            loc = _location(p)
    text = html_to_text(raw)
    if not title:
        m = _BODY_ROLE.search(text[:1500])
        if not m:
            return None
        title = "".join(g or "" for g in m.groups()).strip()
    title = re.sub(r"\s{2,}", " ", _URL_ANY.sub("", title))[:MAX_TITLE].strip(" -–—:,")
    if not title:
        return None
    from .adstance import RESTRICTED, ad_stance
    if ad_stance(text)["stance"] == RESTRICTED:
        return None                         # citizenship / clearance required: closed to a visa holder
    remote = _remote_value(mode_segs or [loc])
    if not loc:
        loc = "Remote" if remote == "remote" else ""
    signal = sponsorship_signal(header_flags + rest, text)
    posted = (c.get("created_at") or "")[:10]
    post = POST_URL.format(id=cid)
    site = next((u for u in links if u.startswith("http")), "")
    row = _job(SOURCE, "", cid, company, title, loc, remote, post,
               describe(company, title, loc, remote, signal, site, post), posted)
    row["sponsorship_stated"] = signal
    row["salary"] = next((v for v in map(header_pay, rest) if v), "")
    return row


_PAY_RANGE = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])?\s*(?:-|–|—|to)\s*\$?\s*"
                        r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?")
_PAY_ONE = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*([kK])\b")


def header_pay(seg: str) -> str:
    """A yearly USD pay range from a header segment ("$150k-$190k", "$130-240K + equity"), or "".
    Only plausible yearly figures (20k to 2M) count; an hourly rate or a project fee is left out
    (a blank "Salary TBD" is honest, a wrong figure is not)."""
    def num(v, k):
        x = float(v.replace(",", ""))
        return x * 1000 if (k and x < 1000) else x
    m = _PAY_RANGE.search(seg or "")
    if m:
        k = m.group(2) or m.group(4)
        lo, hi = num(m.group(1), k), num(m.group(3), k)
        if 20_000 <= lo <= hi <= 2_000_000:
            return _fmt_salary(lo, hi)
        return ""
    m = _PAY_ONE.search(seg or "")
    if m and re.search(r"\b(salary|base|comp|/\s*y(ea)?r|per year|annual)\b", seg, re.I):
        v = num(m.group(1), m.group(2))
        if 20_000 <= v <= 2_000_000:
            return _fmt_salary(v, None)
    return ""


def describe(company, title, location, remote, signal, site, post) -> str:
    """The row's description: our own sentences from the post's facts (never the post's text)."""
    lines = [f"{company} is hiring: {title}."]
    if location:
        lines.append(f"Location: {location}.")
    if remote:
        lines.append({"remote": "The post lists the role as remote.",
                      "hybrid": "The post lists the role as hybrid.",
                      "onsite": "The post lists the role as onsite."}[remote])
    if signal is True:
        lines.append("The post says visa sponsorship is available.")
    elif signal is False:
        lines.append("The post says the company will not sponsor visas.")
    lines.append("Posted in the Hacker News \"Who is hiring?\" thread. Read the full post and how "
                 f"to apply on Hacker News: {post}")
    if site:
        lines.append(f"Company link from the post: {site}")
    return "\n".join(lines)


# -- the thread ------------------------------------------------------------------------- #

def find_thread(search: dict, now: float | None = None) -> dict | None:
    """The newest "Ask HN: Who is hiring?" story by whoishiring in a search_by_date answer,
    or None when there is none younger than MAX_THREAD_DAYS."""
    now = time.time() if now is None else now
    hits = [h for h in (search or {}).get("hits") or []
            if THREAD_RE.match(h.get("title") or "") and (h.get("author") or "") == "whoishiring"]
    hits.sort(key=lambda h: int(h.get("created_at_i") or 0), reverse=True)
    if not hits:
        return None
    top = hits[0]
    if now - int(top.get("created_at_i") or 0) > MAX_THREAD_DAYS * 86400:
        return None
    return {"id": int(top.get("objectID") or top.get("story_id") or 0), "title": top.get("title", ""),
            "created_at_i": int(top.get("created_at_i") or 0)}


def top_level(item: dict) -> list[dict]:
    """The thread's top-level comments, slimmed to what the parser reads."""
    sid = item.get("id")
    out = []
    for c in item.get("children") or []:
        if c.get("type", "comment") != "comment" or not c.get("text"):
            continue                         # deleted / flagged comments come back without text
        if c.get("parent_id") not in (None, sid):
            continue
        out.append({"id": c.get("id"), "author": c.get("author") or "",
                    "created_at": c.get("created_at") or "", "text": c.get("text") or ""})
    return out


def _cache_hours() -> float:
    try:
        return float(os.environ.get("HN_CACHE_HOURS", CACHE_HOURS))
    except ValueError:
        return CACHE_HOURS


def load_thread(fetch=fetch_json, cache_dir: str | Path | None = None, now: float | None = None,
                sleep=time.sleep) -> dict:
    """{thread, comments, fetched_at, cached} for the current thread, from the cache when it is
    younger than CACHE_HOURS, else from the API (two requests). A failed fetch falls back to an
    older cache rather than to nothing."""
    now = time.time() if now is None else now
    path = Path(cache_dir) / CACHE_FILE if cache_dir else None
    cached = None
    if path and path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
    if cached and now - float(cached.get("fetched_at") or 0) < _cache_hours() * 3600:
        return {**cached, "cached": True}
    try:
        thread = find_thread(fetch(SEARCH_URL), now=now)
        if thread is None:
            data = {"thread": None, "comments": [], "fetched_at": now}
        else:
            sleep(REQUEST_GAP)
            data = {"thread": thread, "comments": top_level(fetch(ITEM_URL.format(id=thread["id"]))),
                    "fetched_at": now}
    except Exception:
        if cached:
            return {**cached, "cached": True}
        raise
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
    return {**data, "cached": False}


def hn_jobs(search="", company="", fetch=fetch_json, cache_dir=None, now=None, sleep=time.sleep):
    """Aggregator entry point (ats.AGGREGATORS['hackernews']): every job post in the current
    "Who is hiring?" thread as a row. `search` filters titles client-side (no server search)."""
    data = load_thread(fetch=fetch, cache_dir=cache_dir, now=now, sleep=sleep)
    term = (search or "").strip().lower()
    out = []
    for c in data.get("comments") or []:
        row = parse_comment(c)
        if row and (not term or term in row["title"].lower()):
            out.append(row)
    return out

