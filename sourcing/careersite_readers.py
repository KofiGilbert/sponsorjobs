"""Page readers and the keep rule for the career-site lane (sourcing/careersite.py).

careersite.py reads each job page's schema.org JobPosting JSON-LD. Some large sponsors' sites
have a clean robots.txt and job sitemap but keep the job data in the page's framework JSON
instead (Walmart: the Next.js `__NEXT_DATA__` script, no JSON-LD). A site whose table entry
names a reader ("reader": "nextdata") gets that reader as the FALLBACK when a page has no
JSON-LD; the reader returns a JobPosting-shaped dict, so careersite.posting_to_job is unchanged.

The keep rule ("keep" in a site's entry) is for sites whose sitemap is mostly roles our users
(international students looking for sponsored professional jobs) cannot use: store, warehouse
and driver hourly roles. It is applied twice:

  * on the sitemap URL's slug, before any request, when the slug spells the title
    (careers.homedepot.com/job/23555314/senior-software-engineer-payroll-ai-remote/), so a
    dropped role costs nothing;
  * on the page's title (and the department the reader found) when the slug is just an id
    (careers.walmart.com/us/en/jobs/R-2646349). A page dropped this way is remembered for
    SKIP_DAYS (a small table in the same SQLite file) so the next run does not read it again.

Reading only; nothing here fetches. The politeness rules all live in careersite.Host.
"""

from __future__ import annotations

import html as _html
import json
import re
from datetime import date, timedelta
from urllib.parse import urlsplit

SKIP_DAYS = 30                  # a page the keep rule dropped is not read again for this long

# --------------------------------------------------------------------------------------------- #
# the keep rule
# --------------------------------------------------------------------------------------------- #

# Hourly store / warehouse / driver / service roles. Checked FIRST, so "Data Center Technician
# (Part Time)" or "Pharmacy Grad Intern (Hrly)" is dropped even though a word in it is professional.
HOURLY = re.compile(
    r"\b(cashiers?|stockers?|stocking|cart attendant|attendant|custodians?|custodial|janitor\w*|"
    r"drivers?|cdl|meat cutter|butcher|bakery|baker|cake decorator|deli|produce|cafe|barista|"
    r"cook|kitchen|dishwasher|housekeep\w*|personal shopper|shopper|carwash|greeter|crew member|"
    r"team member|part[- ]time|pt|hourly|hrly|warehouse|forklift|order filler|picker|packer|"
    r"loaders?|unloaders?|lot associate|freight|tire|lube|overnight|seasonal|"
    r"store (associate|manager|lead|supervisor|director)|department supervisor|"
    r"customer service|sales associate|sales floor|teller|security officer|guard|optician|"
    r"pharmacy tech\w*|technician|member (specialist|team lead|assist)|"
    r"fulfillment associate|merchandising execution|asset protection)\b", re.I)

# Professional roles worth a sponsored-job seeker's time: technology, data, engineering,
# product, design, research, finance, risk, legal, strategy, supply-chain management and
# similar corporate work, plus senior leadership.
PROFESSIONAL = re.compile(
    r"\b(software|engineer\w*|developer|programmer|data|analyst|analytics|scientist|science|"
    r"machine learning|ml|ai|artificial intelligence|deep learning|nlp|computer vision|"
    r"product (manager|management|owner|designer|marketing|analyst|lead)|"
    r"program manag\w*|project manag\w*|technical|technology|technologist|architect\w*|"
    r"cyber\w*|information security|security engineer|(?<!saint )(?<!st )cloud|devops|sre|site reliability|"
    r"infrastructure|network|systems? (engineer|administrator|analyst)|database|"
    r"ux|ui|user experience|designer|research\w*|finance|financial|fp&a|accounting|accountant|"
    r"audit\w*|tax|treasury|actuar\w*|economist|quantitative|quant|risk|compliance|legal|"
    r"counsel|attorney|strategy|strategist|consultant|consulting|supply chain|logistics "
    r"(analyst|manager|engineer)|procurement|sourcing|buyer|merchant|merchandise planner|"
    r"pricing|marketing manager|business intelligence|bi|operations research|"
    r"director|vice president|vp)\b", re.I)

_SLUG_STOP = {"en", "us", "es", "fr", "de", "gb", "uk", "ca", "in", "job", "jobs", "detail",
              "details", "jobdetail", "jobdetails", "careers", "career", "search", "apply", "posting"}


def slug_words(url: str) -> str:
    """The words a job URL's path spells, or "" when the path is only ids and locale parts
    ("/us/en/jobs/R-2646349" -> "", "/job/23555314/senior-software-engineer-remote/" ->
    "senior software engineer remote")."""
    words: list[str] = []
    for seg in urlsplit(url).path.split("/"):
        if not re.search(r"[A-Za-z]{2}", seg):
            continue
        if re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", seg, re.I):           # en, en-us, us-en
            continue
        for w in re.findall(r"[a-z0-9&]+", seg.lower()):
            if w not in _SLUG_STOP and re.search(r"[a-z]{2}", w) and not re.fullmatch(r"[a-z]?\d+", w):
                words.append(w)
    return " ".join(words) if len(words) >= 2 else ""


def decide(rule: dict | None, title: str, department: str = "") -> bool | None:
    """True keep / False drop / None undecided, for one title (and the department a reader
    found). Hourly words drop first, then professional words keep, then the site's optional
    `department` pattern keeps."""
    if rule is None:
        return True
    title = (title or "").replace("_", " ")
    extra_drop = rule.get("drop")
    if HOURLY.search(title) or (extra_drop and re.search(extra_drop, title, re.I)):
        return False
    extra_keep = rule.get("keep")
    if PROFESSIONAL.search(title) or (extra_keep and re.search(extra_keep, title, re.I)):
        return True
    dept = rule.get("department")
    if dept and department and re.search(dept, department, re.I):
        return True
    return None


def _fallback(rule: dict) -> bool:
    return str(rule.get("else", "drop")).lower() == "keep"


def keep_by_slug(cfg: dict, url: str) -> bool | None:
    """The keep rule on a sitemap URL, before any request: False = drop without fetching,
    True = keep (the page title is checked again), None = the slug says nothing; read the page."""
    rule = cfg.get("keep")
    if rule is None:
        return True
    words = slug_words(url)
    if not words:
        return None
    verdict = decide(rule, words)
    if verdict is None and not rule.get("department"):
        return _fallback(rule)          # the slug IS the title here; the page would add nothing
    return verdict


def keep_by_page(cfg: dict, posting: dict) -> bool:
    """The keep rule on a read posting (title, plus the department a reader recorded)."""
    rule = cfg.get("keep")
    if rule is None:
        return True
    dept = " ".join(_text(posting.get(k)) for k in ("_department", "occupationalCategory", "industry")
                    if _text(posting.get(k)))
    verdict = decide(rule, str(posting.get("title") or ""), dept)
    return _fallback(rule) if verdict is None else verdict


def order_urls(cfg: dict, found: dict[str, str]) -> list[tuple[str, str]]:
    """Sitemap rows newest first: by lastmod, then (when the site's entry names an `id_order`
    pattern and the sitemap has no lastmod, as on Walmart) by the requisition number, highest
    first, so the freshest postings are read first."""
    pat = re.compile(cfg["id_order"]) if cfg.get("id_order") else None

    def key(kv):
        num = 0
        if pat:
            m = pat.search(kv[0])
            num = int(m.group(1)) if m and m.group(1).isdigit() else 0
        return (kv[1], num)
    return sorted(found.items(), key=key, reverse=True)


class SkipMemory:
    """Job pages the keep rule dropped after reading them, remembered for SKIP_DAYS in the
    store's own SQLite file (table careersite_skip) so a site whose sitemap has no title in the
    URL does not spend its page budget on the same store roles every run. Without a SQLite
    connection (a test double store) it remembers for this run only."""

    def __init__(self, store, days: int = SKIP_DAYS):
        self.days = days
        self._conn = getattr(store, "_conn", None)
        self._mem: set[str] = set()
        if self._conn is not None:
            self._conn.execute("CREATE TABLE IF NOT EXISTS careersite_skip ("
                               "url TEXT PRIMARY KEY, host TEXT, reason TEXT, at TEXT)")
            self._conn.commit()

    def recent(self, urls) -> set[str]:
        urls = [u for u in urls if u]
        if self._conn is None:
            return {u for u in urls if u in self._mem}
        since = (date.today() - timedelta(days=self.days)).isoformat()
        out: set[str] = set()
        for i in range(0, len(urls), 500):
            chunk = urls[i:i + 500]
            q = f"SELECT url FROM careersite_skip WHERE at >= ? AND url IN ({','.join('?' * len(chunk))})"
            out.update(r[0] for r in self._conn.execute(q, (since, *chunk)).fetchall())
        return out

    def remember(self, host: str, url: str, reason: str) -> None:
        self._mem.add(url)
        if self._conn is not None:
            self._conn.execute("INSERT OR REPLACE INTO careersite_skip (url, host, reason, at) VALUES (?,?,?,?)",
                               (url, host, reason[:120], date.today().isoformat()))
            self._conn.commit()


# --------------------------------------------------------------------------------------------- #
# readers: page HTML -> JobPosting-shaped dict
# --------------------------------------------------------------------------------------------- #

_NEXT = re.compile(r"<script[^>]*\bid\s*=\s*[\"']?__NEXT_DATA__[\"']?[^>]*>(.*?)</script>", re.I | re.S)

_TITLE_KEYS = ("jobPostingTitle", "postingTitle", "jobTitle", "positionTitle", "title")
_DESC_KEYS = ("jobPostingDescription", "jobDescription", "descriptionHtml", "description",
              "externalDescription", "jobDescriptionHtml", "body")
_COMPANY_KEYS = ("hiringOrganization", "companyName", "company", "brand", "employer", "organization")
_DATE_KEYS = ("datePosted", "postedDate", "postingDate", "publishedDate", "publishDate",
              "jobPostingStartDate", "postedOn", "createdAt", "created")
_VALID_KEYS = ("validThrough", "expirationDate", "expiryDate", "closingDate", "applicationDeadline")
_EMP_KEYS = ("employmentType", "employmentTypes", "timeType", "workerType", "jobType")
_ID_KEYS = ("jobId", "requisitionId", "jobReqId", "reqId", "jobRequisitionId", "identifier", "id")
_LOC_KEYS = ("jobLocation", "primaryLocation", "locations", "location", "additionalLocations",
             "jobLocations", "workLocations")


def nextdata(page_html: str):
    """The parsed `__NEXT_DATA__` JSON of a Next.js page, or None."""
    m = _NEXT.search(page_html or "")
    if not m:
        return None
    text = m.group(1).strip()
    try:
        return json.loads(text, strict=False)
    except ValueError:
        try:
            return json.loads(_html.unescape(text), strict=False)
        except ValueError:
            return None


def _walk(obj, depth=0):
    if depth > 40:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, depth + 1)


def _text(v) -> str:
    """A scalar, or a {value|name|label: ...} object, as text."""
    if isinstance(v, dict):
        for k in ("value", "name", "label", "descriptor", "text"):
            if isinstance(v.get(k), (str, int, float)) and str(v[k]).strip():
                return str(v[k]).strip()
        return ""
    if isinstance(v, (str, int, float)) and not isinstance(v, bool):
        return str(v).strip()
    return ""


def _first(obj: dict, keys) -> str:
    for k in keys:
        t = _text(obj.get(k))
        if t:
            return t
    return ""


def _iso(s: str) -> str:
    s = (s or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    if re.fullmatch(r"\d{12,13}", s):                     # epoch milliseconds
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return ""
    return ""


def _place(loc) -> dict | None:
    """One location (a string, or an object with city / state / country under any of the usual
    names) as a schema.org Place."""
    if isinstance(loc, str):
        return {"address": {"name": loc.strip()}} if loc.strip() else None
    if not isinstance(loc, dict):
        return None
    if isinstance(loc.get("address"), dict) and any(k.startswith("address") for k in loc["address"]):
        return {"address": loc["address"]}
    city = _first(loc, ("city", "addressLocality", "cityName", "locality"))
    region = _first(loc, ("stateCode", "state", "addressRegion", "region", "stateProvince", "province"))
    country = _first(loc, ("countryCode", "country", "addressCountry", "countryName"))
    if city and city.isupper():
        city = city.title()
    if not (city or region or country):
        name = _first(loc, ("locationName", "name", "label", "displayName"))
        return {"address": {"name": name}} if name else None
    return {"address": {"addressLocality": city, "addressRegion": region, "addressCountry": country}}


def _places(obj: dict) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for k in _LOC_KEYS:
        v = obj.get(k)
        for loc in (v if isinstance(v, list) else [v]):
            p = _place(loc)
            if p:
                sig = json.dumps(p, sort_keys=True)
                if sig not in seen:
                    seen.add(sig)
                    out.append(p)
    return out


def _posting(title, desc, company, places, posted, valid, emp, ident, department="") -> dict:
    p = {"@type": "JobPosting", "title": title, "description": desc}
    if company:
        p["hiringOrganization"] = {"name": company}
    if places:
        p["jobLocation"] = places
    if posted:
        p["datePosted"] = posted
    if valid:
        p["validThrough"] = valid
    if emp:
        p["employmentType"] = emp
    if ident:
        p["identifier"] = {"value": ident}
    if department:
        p["_department"] = department
    return p


_NOT_THIS_SHAPE = object()


def _walmart(doc):
    """careers.walmart.com (verified 2026-10-12 on R-2646349): props.pageProps.jobDetails
    carries the Workday requisition: jobPostingTitle, jobPostingDescription (the full ad incl.
    qualifications), requisitionStatus, primaryLocation / additionalLocations, employmentTypes,
    jobPostingStartDate, businessSegment; pageProps.jobType is "corporate" or "field".
    Returns _NOT_THIS_SHAPE for any other page, None for a closed or untitled requisition."""
    try:
        pp = doc["props"]["pageProps"]
        jd = pp["jobDetails"]
    except (KeyError, TypeError):
        return _NOT_THIS_SHAPE
    if not isinstance(jd, dict) or not ("jobPostingTitle" in jd or "requisitionStatus" in jd):
        return _NOT_THIS_SHAPE
    status = _text(jd.get("requisitionStatus")).lower()
    if status and status not in ("open", "opened"):
        return None                                          # filled / closed requisition
    title = re.sub(r"^\(\s*[A-Z]{2,3}\s*\)\s*", "", _first(jd, ("jobPostingTitle", "title")))
    desc = jd.get("jobPostingDescription") or jd.get("description") or ""
    if not isinstance(desc, str):
        desc = ""
    if not title:
        return None
    places = [p for p in (_place(jd.get("primaryLocation")),
                          *[_place(x) for x in (jd.get("additionalLocations") or [])]) if p]
    places = list({json.dumps(p, sort_keys=True): p for p in places}.values())
    emp = ", ".join(_text(e) for e in (jd.get("employmentTypes") or []) if _text(e))
    dept = " / ".join(x for x in (_text(pp.get("jobType")), _text(jd.get("businessSegment"))) if x)
    # Sam's Club postings carry brand "Sam's Club"; the employer (and the H-1B filer) is Walmart.
    return _posting(title, desc, "Walmart", places,
                    _iso(_first(jd, ("jobPostingStartDate", "recruitingStartDate", "createdAt"))),
                    "", emp, _first(jd, ("jobId", "jobPostingId")), dept)


def _generic(doc) -> dict | None:
    """Any Next.js page: the object with a title-like string and the LONGEST description-like
    string (at least 200 characters, so a page's SEO blurb does not pass for an ad)."""
    best, best_len = None, 0
    for obj in _walk(doc):
        title = next((obj[k].strip() for k in _TITLE_KEYS
                      if isinstance(obj.get(k), str) and 0 < len(obj[k].strip()) <= 200), "")
        if not title:
            continue
        desc = next((obj[k] for k in _DESC_KEYS if isinstance(obj.get(k), str) and len(obj[k]) >= 200), "")
        if len(desc) > best_len:
            best, best_len = (obj, title, desc), len(desc)
    if not best:
        return None
    obj, title, desc = best
    company = _first(obj, _COMPANY_KEYS)
    emp_raw = next((obj[k] for k in _EMP_KEYS if obj.get(k)), None)
    emp = ", ".join(_text(e) for e in emp_raw if _text(e)) if isinstance(emp_raw, list) else _text(emp_raw)
    return _posting(title, desc, company, _places(obj), _iso(_first(obj, _DATE_KEYS)),
                    _iso(_first(obj, _VALID_KEYS)), emp, _first(obj, _ID_KEYS),
                    _first(obj, ("department", "category", "jobCategory", "jobFamily", "businessUnit")))


def read_nextdata(page_html: str, url: str = "") -> dict | None:
    """A JobPosting-shaped dict from a Next.js page's `__NEXT_DATA__`, or None: a known shape
    (Walmart) first, then the generic walk."""
    doc = nextdata(page_html)
    if doc is None:
        return None
    known = _walmart(doc)
    return _generic(doc) if known is _NOT_THIS_SHAPE else known


_PH_DDO = re.compile(r"phApp\.ddo\s*=\s*")


def read_phenom(page_html: str, url: str = "") -> dict | None:
    """A JobPosting-shaped dict from a Phenom page's `phApp.ddo` object, or None.

    Phenom sites (State Street, Merck, Thermo Fisher, GE HealthCare, Xoriant...) embed the
    posting as JSON-LD on most pages, but on many the JSON-LD script is EMPTY and the posting
    lives only in the page's own `phApp.ddo = {"jobDetail": {"status": 200, "data": {"job":
    {...}}}}` (checked 2026-10-23: 4 of 5 State Street pages; the feed build logged ~1,200
    "no posting" pages a run across the Phenom sites). Fields used: title, description,
    companyName, city / state / country (or cityStateCountry), postedDate, jobId / reqId,
    type / positionType, category. A status other than 200 or a job without a title is None."""
    m = _PH_DDO.search(page_html or "")
    if not m:
        return None
    start = page_html.find("{", m.end() - 1)
    try:
        doc, _ = json.JSONDecoder(strict=False).raw_decode(page_html, start)
    except ValueError:
        return None
    jd = doc.get("jobDetail") if isinstance(doc, dict) else None
    if not isinstance(jd, dict) or jd.get("status") not in (200, "200", None):
        return None
    job = (jd.get("data") or {}).get("job") if isinstance(jd.get("data"), dict) else None
    if not isinstance(job, dict):
        return None
    title = _text(job.get("title"))
    desc = job.get("description") or ""
    if not title or not isinstance(desc, str):
        return None
    city, state, country = _text(job.get("city")), _text(job.get("state")), _text(job.get("country"))
    if city or state or country:
        places = [{"address": {"addressLocality": city, "addressRegion": state, "addressCountry": country}}]
    else:
        loc = _first(job, ("cityStateCountry", "location", "locationName"))
        places = [{"address": {"name": loc}}] if loc else []
    return _posting(title, desc, _text(job.get("companyName")), places,
                    _iso(_first(job, ("postedDate", "dateCreated"))), "",
                    _first(job, ("positionType", "type")), _first(job, ("jobId", "reqId", "jobSeqNo")),
                    _first(job, ("category", "jobFamily")))


READERS = {"nextdata": read_nextdata, "phenom": read_phenom}


def reader_for(cfg: dict) -> str:
    """The reader a site entry gets: its own `reader`, else the platform's (every Phenom site
    gets `phenom`), else none."""
    return str(cfg.get("reader") or ("phenom" if cfg.get("platform") == "phenom" else ""))


def read_page(reader: str, page_html: str, url: str = "") -> dict | None:
    """Run the named reader; an unknown name reads nothing (the page is skipped as before)."""
    fn = READERS.get(reader)
    return fn(page_html, url) if fn else None
