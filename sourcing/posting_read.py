"""Read a job posting from its link (2026-11-26): title, company, location and the description.

Found on a real application: pasting a job LINK on Home started a resume chat from the bare link,
so the resume was "tailored" to a URL, not to the job. Now a link is read first, the same GREEN-lane
way the feed reads jobs (CLAUDE.md §6), and a link that cannot be read is said so plainly, never
tailored silently:

  * Greenhouse      boards-api.greenhouse.io/v1/boards/{token}/jobs/{id} (public JSON).
  * Lever           api.lever.co/v0/postings/{site}/{id}?mode=json (public JSON), the company's
                    name from the hosted page's <title> ("Company - Title").
  * Ashby           api.ashbyhq.com/posting-api/job-board/{org} (public JSON), the posting by id.
  * Workable        apply.workable.com/api/v2/accounts/{account}/jobs/{code} (public JSON).
  * Recruitee       {company}.recruitee.com/api/offers/{slug} (public JSON).
  * Workday         the cxs detail JSON the posting page itself loads.
  * SmartRecruiters its API is never asked (robots.txt disallows us, sourcing.ats.DISABLED_ATS):
                    the posting page, like any other site.
  * anything else   the page itself, only when the host's robots.txt allows it, with the app's
                    honest User-Agent: the schema.org JobPosting block the page embeds for search
                    engines, else the page's visible words when they read like a posting.
  * LinkedIn        never fetched (no automation of LinkedIn, CLAUDE.md §7): the person is asked to
                    paste the description. Every other board whose terms ban automation
                    (submit.allowlist.PROHIBITED_HOSTS) is asked the same way.

Plain HTTP, no AI, no login, no cookies; a bot wall or a captcha page is a failure, never a retry
with another identity. ``fetch(url, accept, follow) -> Resp`` is liveness.http_get, swapped for
saved fixtures in the tests.
"""

from __future__ import annotations

import html as _html
import re
import urllib.parse

from .ats import html_to_text
from .careersite import Robots, extract_jobposting, looks_challenged
from .liveness import Resp, closed_phrase, http_get, page_text

# A link, inside any text.
URL_IN_TEXT = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+")
# Text around the link that is still "just a link" ("tailor my resume for this: <link>").
MAX_WORDS_AROUND = 300
# The least a description must hold to be worth tailoring from.
MIN_DESCRIPTION = 200

# Sites whose own terms forbid automation: never fetched, the person pastes the text. The one
# list is the submission policy's (submit/allowlist.py), plus LinkedIn's short links.
from submit.allowlist import PROHIBITED_HOSTS  # noqa: E402

_ASK_TO_PASTE = tuple(PROHIBITED_HOSTS) + ("lnkd.in",)
_SITE_NAMES = {"linkedin": "LinkedIn", "lnkd": "LinkedIn", "ziprecruiter": "ZipRecruiter"}
# Links that are never a job posting (a request may carry one).
_NOT_JOB_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "t.me", "telegram.me",
                  "sponsorjobs.ai", "google.com", "docs.google.com", "drive.google.com")
_JD_WORDS = re.compile(
    r"\b(responsibilities|requirements|qualifications|what you.ll do|what you will do|"
    r"about the role|you will|experience with|we are looking for|we.re looking for|skills)\b", re.I)
_OG_TITLE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I)
_TITLE_TAG = re.compile(r"<title[^>]*>([^<]+)</title>", re.I)


# --------------------------------------------------------------------------- the link

def clean_url(url: str) -> str:
    """The link with a scheme, without tracking parameters or a trailing full stop."""
    u = str(url or "").strip().rstrip(".,;:!?")
    if u.lower().startswith("www."):
        u = "https://" + u
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith(("utm_", "gh_src", "lever-source"))]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def link_in(text: str) -> str:
    """The job link when the text IS a link (or a link with a short line around it, "tailor my
    resume for this: <link>"), else "". A pasted description that ends with its apply link is a
    description, not a link: the words around the link decide."""
    t = str(text or "").strip()
    m = URL_IN_TEXT.search(t)
    if not m:
        return ""
    rest = URL_IN_TEXT.sub(" ", t)
    if len(" ".join(rest.split())) > MAX_WORDS_AROUND:
        return ""
    url = clean_url(m.group(0))
    host = _host(urllib.parse.urlsplit(url)) if url else ""
    if any(host == h or host.endswith("." + h) for h in _NOT_JOB_HOSTS):
        return ""                        # "connect my GitHub <link>" is a request, not a posting
    return url


def _host(p) -> str:
    return (p.hostname or "").lower()


def ask_to_paste_site(url: str) -> str:
    """"LinkedIn" (or another board whose terms ban automation) when the link is on a site the
    app never reads, else ""."""
    host = _host(urllib.parse.urlsplit(url))
    for dom in _ASK_TO_PASTE:
        if host == dom or host.endswith("." + dom):
            label = dom.split(".")[0]
            return _SITE_NAMES.get(label, label.title())
    return ""


# --------------------------------------------------------------------------- answers

def _ok(source, board, jid, url, title, company, location, description) -> dict:
    sid = f"{source}:{board}:{jid}" if source and board and jid else ""
    return {"ok": True, "source": source or "page", "source_id": sid, "url": url,
            "title": " ".join(str(title or "").split())[:200],
            "company": " ".join(str(company or "").split())[:120],
            "location": " ".join(str(location or "").split())[:160],
            "description": str(description or "").strip()}


def _fail(reason: str, why: str, url: str = "") -> dict:
    return {"ok": False, "reason": reason, "why": why, "url": url}


def _slug_name(slug: str) -> str:
    slug = urllib.parse.unquote(str(slug or "")).strip()
    if " " in slug and slug != slug.lower():
        return slug                      # Ashby writes the name itself: "Applied Compute"
    return " ".join(w[:1].upper() + w[1:] for w in re.split(r"[-_ ]+", slug) if w)


def _segs(p) -> list[str]:
    return [s for s in (p.path or "").split("/") if s]


def _gone(r: Resp, what: str) -> dict | None:
    if r.status in (404, 410):
        return _fail("closed", f"{what} says this posting is gone")
    return None


def _no_answer(r: Resp, what: str) -> dict:
    if r.status == 0:
        return _fail("network", f"{what} did not answer")
    return _fail("http", f"{what} answered {r.status}")


# --------------------------------------------------------------------------- the readers
# Each takes the split link and fetch, and returns an answer dict, or None when the link is not
# its kind.

def greenhouse(p, fetch):
    host, segs = _host(p), _segs(p)
    q = urllib.parse.parse_qs(p.query or "")
    token = jid = ""
    if re.match(r"^(job-)?boards(\.eu)?\.greenhouse\.io$", host):
        if segs[:1] == ["embed"]:
            token = (q.get("for") or [""])[0]
            jid = (q.get("token") or [""])[0]
        elif len(segs) >= 3 and segs[1] == "jobs":
            token, jid = segs[0], segs[2]
        if not (token and re.match(r"^\d+$", jid or "")):
            return _fail("unknown_link", "the Greenhouse link has no job in it")
    else:
        return None
    api = f"https://boards-api.greenhouse.io/v1/boards/{token}"
    r = fetch(f"{api}/jobs/{jid}", "application/json", True)
    d = r.json()
    if r.status != 200 or not isinstance(d, dict):
        return _gone(r, "Greenhouse") or _no_answer(r, "Greenhouse")
    company = d.get("company_name") or ""
    if not company:
        b = fetch(api, "application/json", True).json()
        company = (b or {}).get("name") if isinstance(b, dict) else ""
    return _ok("greenhouse", token, jid, d.get("absolute_url") or urllib.parse.urlunsplit(p),
               d.get("title"), company or _slug_name(token),
               (d.get("location") or {}).get("name", ""), html_to_text(d.get("content", "")))


def lever(p, fetch):
    m = re.match(r"^jobs(\.eu)?\.lever\.co$", _host(p))
    if not m:
        return None
    segs = _segs(p)
    if len(segs) < 2:
        return _fail("unknown_link", "the Lever link has no posting in it")
    site, pid, eu = segs[0], segs[1], ".eu" if m.group(1) else ""
    r = fetch(f"https://api{eu}.lever.co/v0/postings/{site}/{pid}?mode=json", "application/json", True)
    d = r.json()
    if r.status != 200 or not isinstance(d, dict) or not d.get("text"):
        return _gone(r, "Lever") or _no_answer(r, "Lever")
    parts = [d.get("descriptionPlain") or html_to_text(d.get("description", ""))]
    for lst in d.get("lists") or []:
        if isinstance(lst, dict):
            body = html_to_text("<ul>" + str(lst.get("content") or "") + "</ul>")
            parts.append(f"{str(lst.get('text') or '').strip()}\n{body}".strip())
    parts.append(d.get("additionalPlain") or html_to_text(d.get("additional", "")))
    title = d.get("text") or ""
    hosted = d.get("hostedUrl") or f"https://jobs{eu}.lever.co/{site}/{pid}"
    # Lever's JSON names no employer; its hosted page's title does ("Company - Title").
    company = ""
    page = fetch(hosted, "text/html", True)
    if page.status == 200:
        tm = _TITLE_TAG.search(page.body or "")
        head = " ".join(_html.unescape(tm.group(1)).split()) if tm else ""
        if head and " - " in head:
            left, _, right = head.partition(" - ")
            if right.strip().lower() == title.strip().lower() or title.lower() in head.lower():
                company = left.strip()
    cat = d.get("categories") or {}
    return _ok("lever", site, pid, hosted, title, company or _slug_name(site),
               cat.get("location", ""), "\n\n".join(x.strip() for x in parts if x and x.strip()))


def ashby(p, fetch):
    if _host(p) != "jobs.ashbyhq.com":
        return None
    segs = _segs(p)
    if len(segs) < 2:
        return _fail("unknown_link", "the Ashby link has no posting in it")
    org, pid = segs[0], segs[1]
    r = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{org}", "application/json", True)
    d = r.json()
    if r.status != 200 or not isinstance(d, dict):
        return _gone(r, "Ashby") or _no_answer(r, "Ashby")
    for j in d.get("jobs") or []:
        if str(j.get("id")) == pid:
            return _ok("ashby", org, pid, j.get("jobUrl") or urllib.parse.urlunsplit(p),
                       j.get("title"), _slug_name(org), j.get("location", ""),
                       j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")))
    return _fail("closed", "Ashby no longer lists this posting")


def workable(p, fetch):
    host, segs = _host(p), _segs(p)
    acct = code = ""
    if host == "apply.workable.com" and len(segs) >= 3 and segs[1] == "j":
        acct, code = segs[0], segs[2]
    elif host.endswith(".workable.com") and host != "apply.workable.com" and segs[:1] == ["j"] and len(segs) >= 2:
        acct, code = host.split(".")[0], segs[1]
    elif "workable.com" in host:
        return _fail("unknown_link", "the Workable link has no job in it")
    else:
        return None
    r = fetch(f"https://apply.workable.com/api/v2/accounts/{acct}/jobs/{code}", "application/json", True)
    d = r.json()
    if r.status != 200 or not isinstance(d, dict) or not d.get("title"):
        return _gone(r, "Workable") or _no_answer(r, "Workable")
    a = fetch(f"https://apply.workable.com/api/v1/accounts/{acct}", "application/json", True).json()
    company = a.get("name") if isinstance(a, dict) else ""
    loc = d.get("location") or {}
    where = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x) \
        if isinstance(loc, dict) else str(loc)
    text = "\n\n".join(html_to_text(d.get(k) or "") for k in ("description", "requirements", "benefits"))
    return _ok("workable", acct, code, f"https://apply.workable.com/{acct}/j/{code}/",
               d.get("title"), company or _slug_name(acct), where, text)


def recruitee(p, fetch):
    host, segs = _host(p), _segs(p)
    if not host.endswith(".recruitee.com"):
        return None
    if len(segs) < 2 or segs[0] != "o":
        return _fail("unknown_link", "the Recruitee link has no offer in it")
    r = fetch(f"https://{host}/api/offers/{segs[1]}", "application/json", True)
    d = r.json()
    o = d.get("offer") if isinstance(d, dict) else None
    if r.status != 200 or not isinstance(o, dict):
        return _gone(r, "Recruitee") or _no_answer(r, "Recruitee")
    text = "\n\n".join(html_to_text(o.get(k) or "") for k in ("description", "requirements"))
    return _ok("recruitee", host.split(".")[0], o.get("id") or segs[1],
               o.get("careers_url") or urllib.parse.urlunsplit(p), o.get("title"),
               o.get("company_name") or _slug_name(host.split(".")[0]),
               o.get("location") or ", ".join(x for x in (o.get("city"), o.get("country")) if x), text)


def workday(p, fetch):
    host = _host(p)
    m = re.match(r"^([a-z0-9][a-z0-9-]*)\.wd\d{1,3}\.myworkdayjobs\.com$", host)
    if not m:
        return None
    segs = _segs(p)
    if segs and re.match(r"^[a-z]{2}(-[A-Za-z]{2})?$", segs[0]):
        segs = segs[1:]
    if len(segs) < 3 or "job" not in segs[1:]:
        return _fail("unknown_link", "the Workday link has no job in it")
    i = segs.index("job", 1)
    site = segs[i - 1]
    r = fetch(f"https://{host}/wday/cxs/{m.group(1)}/{site}/" + "/".join(segs[i:]), "application/json", True)
    d = r.json()
    info = d.get("jobPostingInfo") if isinstance(d, dict) else None
    if r.status != 200 or not isinstance(info, dict):
        return _gone(r, "Workday") or _no_answer(r, "Workday")
    org = (d.get("hiringOrganization") or {}).get("name") or _slug_name(m.group(1))
    board = f"{m.group(1)}.{host.split('.')[1]}/{site}"         # the feed's "{tenant}.wdN/{site}"
    return _ok("workday", board, info.get("jobReqId") or segs[-1],
               info.get("externalUrl") or urllib.parse.urlunsplit(p), info.get("title"), org,
               info.get("location", ""), html_to_text(str(info.get("jobDescription") or "")))


READERS = (greenhouse, lever, ashby, workable, recruitee, workday)


# --------------------------------------------------------------------------- any other page

def robots_allows(url: str, fetch) -> bool | None:
    """True / False from the host's robots.txt (a missing file allows); None when it cannot be
    read (a 429, a 5xx, no answer): nothing is fetched blind."""
    p = urllib.parse.urlsplit(url)
    r = fetch(f"{p.scheme}://{p.netloc}/robots.txt", "text/plain", True)
    if r.status == 200:
        return Robots(r.body or "").can_fetch(url)
    if 400 <= r.status < 500 and r.status != 429:
        return True
    return None


def _posting_text(posting: dict) -> tuple[str, str, str]:
    org = posting.get("hiringOrganization")
    company = org.get("name") if isinstance(org, dict) else (org if isinstance(org, str) else "")
    locs = posting.get("jobLocation")
    locs = locs if isinstance(locs, list) else [locs] if locs else []
    where = []
    for loc in locs:
        a = (loc or {}).get("address") if isinstance(loc, dict) else None
        if isinstance(a, dict):
            where.append(", ".join(str(a.get(k)) for k in ("addressLocality", "addressRegion") if a.get(k)))
    if not where and str(posting.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        where = ["Remote"]
    return str(company or ""), "; ".join(w for w in where if w), html_to_text(str(posting.get("description") or ""))


def _split_title(head: str) -> tuple[str, str]:
    """"Title - Company" / "Title at Company" / "Title | Company" -> (title, company)."""
    for sep in (" at ", " | ", " - ", " @ "):
        if sep in head:
            a, _, b = head.partition(sep)
            return a.strip(), b.strip()
    return head.strip(), ""


def page(p, fetch):
    url = urllib.parse.urlunsplit(p)
    allowed = robots_allows(url, fetch)
    if allowed is None:
        return _fail("robots", "the site did not let the app check its rules for reading pages")
    if not allowed:
        return _fail("robots", "the site's rules do not allow apps to read this page")
    r = fetch(url, "text/html,application/xhtml+xml", True)
    if r.status in (401, 403, 429) or (r.status == 200 and looks_challenged(r.body or "")):
        return _fail("blocked", "the site asked for a human check")
    gone = _gone(r, "the page")
    if gone:
        return gone
    if r.status != 200:
        return _no_answer(r, "the page")
    return from_html(r.url or url, r.body or "") or _fail(
        "no_text", "the page does not show the job's description to apps "
                   "(it may load it only in a browser)")


def from_html(url: str, body: str, text: str = "") -> dict | None:
    """The posting a page's own HTML carries, read with no request: its schema.org JobPosting
    block, else its visible words when they read like a posting (``text``: the words a browser
    shows, when the caller has them). A closed page answers closed; anything else None."""
    body = body or ""
    posting = extract_jobposting(body)
    if posting and posting.get("title"):
        company, where, desc = _posting_text(posting)
        return _ok("page", "", "", url, posting.get("title"), company, where, desc)
    words = (text or "").strip() or page_text(body)
    if closed_phrase(words):
        return _fail("closed", "the page says this posting is closed")
    m = _OG_TITLE.search(body) or _TITLE_TAG.search(body)
    head = " ".join(_html.unescape(m.group(1)).split()) if m else ""
    if len(words) >= MIN_DESCRIPTION * 3 and len(_JD_WORDS.findall(words)) >= 2 and head:
        title, company = _split_title(head)
        return _ok("page", "", "", url, title, company, "", words)
    return None


_ATS_HOST = re.compile(r"(^|\.)(greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|recruitee\.com|"
                       r"myworkdayjobs\.com)$", re.I)
_FORM_TAIL = re.compile(r"/(apply|application|applications|apply-now|applynow)/?$", re.I)


def read_open_page(url: str, html: str = "", text: str = "", canonical: str = "", *, fetch=None) -> dict:
    """The job behind a page the person has open in the app's Browser (2026-11-26): the page's
    own HTML first (no request), then the posting its link names, read the compliant way
    (read_posting), then the posting page an application form sits under (".../apply") or the
    page's canonical link. LinkedIn and the other paste-only sites are never read."""
    url = clean_url(url)
    if not url:
        return _fail("unknown_link", "no page is open")
    site = ask_to_paste_site(url)
    if site:
        out = _fail("paste_site", f"the app does not read {site} pages", url)
        out["site"] = site
        return out
    p = urllib.parse.urlsplit(url)
    tried, first = set(), None
    if _ATS_HOST.search(_host(p)):
        # an ATS's public JSON first: the feed's own id comes with it (a job on the board is that job)
        first = read_posting(url, fetch=fetch)
        if first.get("ok"):
            return first
        tried.add(url)
    own = from_html(url, html, text) if (html or text) else None
    if own and own.get("ok") and len(own.get("description") or "") >= MIN_DESCRIPTION:
        return own
    posting_page = urllib.parse.urlunsplit(p._replace(path=_FORM_TAIL.sub("", p.path) or "/", query="")) \
        if _FORM_TAIL.search(p.path or "") else ""
    last = own if own and not own.get("ok") else first
    for cand in (url, posting_page, clean_url(canonical)):
        if not cand or cand in tried:
            continue
        tried.add(cand)
        res = read_posting(cand, fetch=fetch)
        if res.get("ok"):
            return res
        if not (last and last.get("reason") == "closed"):
            last = res
    return last or _fail("no_text", "the page does not show the job's description")


# --------------------------------------------------------------------------- the read

def read_posting(url: str, *, fetch=None) -> dict:
    """{ok: True, title, company, location, description, url, source, source_id} for a job link,
    or {ok: False, reason, why, url, site?}. Never raises."""
    fetch = fetch or http_get
    url = clean_url(url)
    if not url:
        return _fail("unknown_link", "that is not a web link")
    site = ask_to_paste_site(url)
    if site:
        out = _fail("paste_site", f"the app does not read {site} pages", url)
        out["site"] = site
        return out
    p = urllib.parse.urlsplit(url)
    try:
        out = None
        for fn in READERS:
            out = fn(p, fetch)
            if out is not None:
                break
        if out is None:           # SmartRecruiters too: its API stays unasked (DISABLED_ATS)
            out = page(p, fetch)
    except Exception as exc:       # noqa: BLE001 - a broken answer is a failed read, said plainly
        out = _fail("error", f"reading it failed ({type(exc).__name__})")
    out.setdefault("url", url)
    if not out.get("url"):
        out["url"] = url
    if out.get("ok") and len(out.get("description") or "") < MIN_DESCRIPTION:
        return _fail("no_text", "the posting has almost no description in it", url)
    return out


def failure_message(res: dict) -> str:
    """The plain line the person reads when a link could not be read: what happened, then what
    to do. No dashes."""
    if res.get("reason") == "paste_site":
        site = res.get("site") or "that site"
        return (f"I do not read {site} pages. Open the job on {site}, copy its description, "
                f"and paste it here. I will tailor your resume from that.")
    why = str(res.get("why") or "it did not answer").rstrip(".")
    return (f"I could not read that posting: {why}. Paste the job description text here "
            f"and I will tailor your resume from it.")


def confirmation(res: dict) -> str:
    """"Read the posting: Business Intelligence Analyst at Example Co." """
    title, company = res.get("title") or "", res.get("company") or ""
    what = f"{title} at {company}" if title and company else (title or company or "the job")
    return f"Read the posting: {what}."


def as_jd(res: dict) -> str:
    """The text the resume chat tailors from: the posting's own header lines, then its words."""
    head = [x for x in (res.get("title"), res.get("company"), res.get("location")) if x]
    return ("\n".join(head) + "\n\n" + (res.get("description") or "")).strip()

