"""Profile links and project entries read from an uploaded resume, faithfully.

A resume PDF shows link TEXT ("In", "GitHub", a small arrow) while the real address lives in
the PDF's link annotation. Text extraction only sees the anchor text, so a parser that reads
the text alone stores "In" as the person's LinkedIn. This module:

* decides whether a stored value is a real link at all (``link_ok``), so anchor text such as
  "In" or "GitHub" is never kept as a link anywhere (session, brain, letterhead);
* sorts the links a PDF carries (``intake.cv_import.pdf_links``) into the person's own profile
  links (LinkedIn, GitHub, a website) and the links that belong to a project row;
* reads the Projects section of a resume's text into project entries (name, tools, bullets),
  the fallback when the model returns none.

Pure functions, no network, never raises on odd input.
"""

from __future__ import annotations

import re

LINK_FIELDS = ("linkedin", "github", "blog", "website")

_URLISH = re.compile(r"^(?:(?:https?|ftp)://)?(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]\S*)?$",
                     re.I)
_ARROWS = "↗➚⇗→»↑⬈"
_BULLET = re.compile(r"^\s*[•●▪◦‣·\-*–]\s*")
_SECTION = re.compile(
    r"^(?:professional\s+|work\s+|relevant\s+|technical\s+|core\s+|key\s+)?"
    r"(experience|employment|work\s+history|education|skills|certifications?|interests|summary|"
    r"profile|references|awards|publications|languages|volunteer(?:ing)?|leadership|"
    r"extracurricular|activities|honou?rs|courses|coursework)\b.{0,25}$", re.I)
_PROJECTS_HEAD = re.compile(r"^(?:selected\s+|personal\s+|key\s+|academic\s+|side\s+|technical\s+)?"
                            r"projects?\s*:?\s*$", re.I)
_DATES = re.compile(r"((?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d\d\s*(?:[-–]|to)\s*(?:present|current|"
                    r"(?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d\d)|(?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d\d)\s*$",
                    re.I)


def _host_path(url: str) -> tuple[str, list[str]]:
    u = re.sub(r"^[a-z]+://", "", str(url or "").strip(), flags=re.I)
    u = re.sub(r"^www\.", "", u, flags=re.I)
    host, _, rest = u.partition("/")
    rest = re.split(r"[?#]", rest, maxsplit=1)[0]
    return host.lower(), [p for p in rest.split("/") if p]


def link_ok(field: str, value) -> bool:
    """True when ``value`` is a real web address for the profile field ``field``. Anchor text
    ("In", "GitHub", "LinkedIn", "Portfolio") is not; nor is a bare linkedin.com or github.com
    with no profile path, which points at nobody."""
    v = str(value or "").strip()
    if not v or " " in v or not _URLISH.match(v):
        return False
    host, path = _host_path(v)
    if host.endswith("linkedin.com") or host.endswith("github.com"):
        return bool(path)
    return True


def clean_identity_links(ident: dict) -> dict:
    """Blank any link field holding something that is not a real link (in place)."""
    if isinstance(ident, dict):
        for f in LINK_FIELDS:
            if f in ident and str(ident.get(f) or "").strip() and not link_ok(f, ident.get(f)):
                ident[f] = ""
    return ident


def _clean_title(s: str) -> str:
    s = str(s or "")
    for a in _ARROWS:
        s = s.replace(a, " ")
    s = re.split(r"\s\|\s|\s•\s", s)[-1]          # the last cell of a "a | b | Title" line
    return " ".join(s.split()).strip(" -–|,:")


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).split())


def same_title(a: str, b: str) -> bool:
    """A project named on the page and the same project as a parser wrote it down."""
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    if x == y or x in y or y in x or x.replace(" ", "") == y.replace(" ", ""):
        return True
    tx, ty = set(x.split()), set(y.split())
    return len(tx & ty) / max(1, len(tx | ty)) >= 0.5


def resume_links(links: list[dict] | None) -> dict:
    """Sort a PDF's links (``pdf_links``) into {"identity": {linkedin, github, blog},
    "projects": [{"title", "link", "tools"}]}. A header link (the top band of page one) is a
    profile link; a link on a body row is that row's project link, named by the text to its
    left (the project title) with the text to its right (the tools) kept too."""
    ident: dict = {}
    projects: list[dict] = []
    for ln in links or []:
        url = str(ln.get("url") or "").strip()
        if not url or url.lower().startswith(("mailto:", "tel:")):
            continue
        host, path = _host_path(url)
        header = bool(ln.get("header"))
        if host.endswith("linkedin.com"):
            if path and path[0] in ("in", "pub") and len(path) >= 2 and not ident.get("linkedin"):
                ident["linkedin"] = url
            continue
        if host.endswith("github.com") and len(path) == 1:
            if not ident.get("github"):
                ident["github"] = url
            continue
        if header:
            if not ident.get("blog"):
                ident["blog"] = url
            continue
        title = _clean_title(ln.get("before") or "")
        if not title:
            continue
        tools = " ".join(str(ln.get("after") or "").replace("|", " ").split()).strip(" ,;")
        for a in _ARROWS:
            tools = tools.replace(a, "").strip()
        if _DATES.fullmatch(tools or "x"):
            tools = ""
        projects.append({"title": title, "link": url, "tools": tools})
    return {"identity": ident, "projects": projects}


def projects_from_text(text: str) -> list[dict]:
    """The Projects section of a resume's text as entries {org, tools, dates, bullets}: each
    project header line (often "Name ↗ tools") and the bullets under it, wrapped lines joined.
    Empty when the text has no Projects section. Copies the words; invents nothing."""
    lines = [ln.rstrip() for ln in str(text or "").replace("\r", "\n").split("\n")]
    start = next((i for i, ln in enumerate(lines) if _PROJECTS_HEAD.match(ln.strip())), -1)
    if start < 0:
        return []
    out: list[dict] = []
    cur: dict | None = None
    for raw in lines[start + 1:]:
        ln = raw.strip()
        if not ln:
            continue
        if _SECTION.match(ln) and len(ln) < 40 and not _BULLET.match(ln):
            break
        if _BULLET.match(ln):
            if cur is None:
                continue
            cur["bullets"].append(_BULLET.sub("", ln).strip())
            continue
        last = cur["bullets"][-1] if cur and cur["bullets"] else ""
        has_arrow = any(a in ln for a in _ARROWS)
        if cur is not None and last and not has_arrow and (
                ln[:1].islower() or not last.rstrip().endswith((".", "!", "?"))):
            cur["bullets"][-1] = f"{last} {ln}"          # a wrapped bullet line
            continue
        if cur is not None and not cur["bullets"] and not has_arrow:
            continue                                     # a stray second header line
        name, tools, dates = ln, "", ""
        for a in _ARROWS:
            if a in name:
                name, _, tools = name.partition(a)
                break
        else:
            m = _DATES.search(name)
            if m:
                dates, name = m.group(1).strip(), name[:m.start()]
            if " | " in name:
                name, _, tools = name.partition(" | ")
        m = _DATES.search(tools)
        if m and not dates:
            dates, tools = m.group(1).strip(), tools[:m.start()]
        name = " ".join(name.split()).strip(" -–|,:")
        if not name:
            continue
        cur = {"org": name, "tools": " ".join(tools.split()).strip(" -–|,:"), "dates": dates,
               "bullets": []}
        out.append(cur)
    for p in out:
        p["bullets"] = [b if b.endswith((".", "!", "?")) else b + "." for b in p["bullets"] if b]
    return out


def attach_project_links(projects: list[dict], found: list[dict]) -> None:
    """Give each project its link and tools from the PDF's project rows, matched by name
    (in place). A project that already has a real link keeps it."""
    for p in projects or []:
        if not isinstance(p, dict):
            continue
        name = p.get("org") or p.get("name") or p.get("title") or ""
        hit = next((f for f in found or [] if same_title(f.get("title"), name)), None)
        if not hit:
            continue
        if not link_ok("link", p.get("link") or p.get("url")):
            p["link"] = hit["link"]
        if hit.get("tools") and not str(p.get("tools") or "").strip():
            p["tools"] = hit["tools"]


def links_note(links: list[dict] | None) -> str:
    """The links a file carries, as plain lines a reader of its text can use ("Anchor In ->
    https://..."), appended to the text the model reads so it never mistakes anchor text for
    an address."""
    rows = []
    for ln in links or []:
        url = str(ln.get("url") or "").strip()
        if not url or url.lower().startswith("mailto:"):
            continue
        where = _clean_title(ln.get("before") or "") if not ln.get("header") else "header"
        anchor = " ".join(str(ln.get("anchor") or "").split()) or "link"
        rows.append(f"- {anchor} ({where or 'body'}): {url}")
    if not rows:
        return ""
    return ("\n\nLINKS IN THIS FILE (the text above shows only each link's anchor text; these "
            "are the real addresses):\n" + "\n".join(rows))


# --------------------------------------------------------------------------- #
# A project and its GitHub repository, matched by name (never a guess)
# --------------------------------------------------------------------------- #

# Short forms a repository name uses for a word the project title spells out.
_ABBREV = {"ops": "operations", "op": "operations", "mgmt": "management", "mgr": "manager",
           "sim": "simulator", "db": "database", "viz": "visualization", "vis": "visualization",
           "app": "app", "apps": "apps", "analytics": "analytics", "eng": "engine",
           "gen": "generator", "auto": "automation", "ml": "machine learning",
           "nlp": "natural language processing", "dash": "dashboard", "cli": "cli"}
_NAME_STOP = frozenset({"the", "a", "an", "of", "for", "and", "my", "app"})


def name_words(name: str) -> list[str]:
    """A project or repository name as plain words: the bracketed tech list a resume adds is
    dropped, "-", "_", "." and camelCase split ("last-mile-ops-simulator" -> last mile
    operations simulator, "GateCheck" -> gate check), short forms spelled out."""
    s = re.sub(r"\([^)]*\)", " ", str(name or ""))
    for a in _ARROWS:
        s = s.replace(a, " ")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    words = re.findall(r"[a-z0-9]+", s.lower())
    out: list[str] = []
    for w in words:
        out.extend(_ABBREV.get(w, w).split())
    return out


def repo_matches(project_name: str, repo_name: str) -> bool:
    """True only when a resume project and a repository are clearly the same thing by name:
    the same letters once spaces and punctuation go ("Gate Check" and gatecheck), or the same
    words once short forms are spelled out ("Last-Mile Operations Simulator" and
    last-mile-ops-simulator). A shared word or two is not enough: no guessing."""
    a, b = name_words(project_name), name_words(repo_name)
    if not a or not b:
        return False
    if "".join(a) == "".join(b):
        return True
    ka = [w for w in a if w not in _NAME_STOP]
    kb = [w for w in b if w not in _NAME_STOP]
    return bool(ka) and ka == kb


def match_repo_links(projects: list[dict], repos: list[dict]) -> list[tuple[dict, str]]:
    """(project, repository URL) for each project with no real link whose name matches exactly
    ONE public repository (``repo_matches``). Two candidate repositories means no link: the
    person picks, the app does not guess. ``repos`` are github_repos cards {name, url, private}."""
    out = []
    for p in projects or []:
        if not isinstance(p, dict) or link_ok("link", p.get("link") or p.get("url")):
            continue
        name = p.get("org") or p.get("name") or p.get("title") or ""
        hits = [r for r in repos or [] if isinstance(r, dict) and not r.get("private")
                and str(r.get("url") or "").strip() and repo_matches(name, str(r.get("name") or ""))]
        if len(hits) == 1:
            out.append((p, str(hits[0]["url"]).strip()))
    return out
