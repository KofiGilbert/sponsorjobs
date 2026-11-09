"""Which of the person's GitHub repositories fit this job, judged honestly (plan PR 3).

The work is split so the model judges only what code has already gathered and checked:

1. Gather, in code: the repositories come from ``ui.github_repos.list_repos`` (owned, not forks,
   archived or empty). Each is scored on its name, description, topics and language against the
   job's skills (``tailoring.keywords.skill_terms``) plus how recently it was pushed. Only the
   top ``K`` get their README (the first ``README_CHARS`` characters, markdown stripped) and
   their languages read from GitHub, cached by (full_name, pushed_at) so an unchanged
   repository is never read twice.
2. Judge, in ONE tool-less model call (``llm.rank_repos_for_jd``): the README text goes only to
   this call, never into the chat agent's loop, so a README cannot steer the agent (a prompt
   injection guard). The person's own resume projects are judged in the same call, on the same
   scale.
3. Check, in code: every term the model claims must literally appear in that repository's own
   text and in the job (others are dropped); "strong" with no checked term becomes "some"; a
   repository with no README and no description is "couldn't judge", never a fit; a draft
   bullet must pass the fact gate (tailoring/fact_gate.py) with the README as the only material.

At most ``MAX_RANKINGS`` model calls per chat session, and a repeat for the same job, the same
repositories and the same resume projects is a cache hit with no model call. Every GitHub call
goes through ``github_publish.FETCH`` (the seam tests replace); the token never appears in a
result.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from datetime import datetime, timezone

from ui import github_publish

K = 8                         # repositories read closely (README + languages)
README_CHARS = 1500
MAX_RANKINGS = 3              # model calls per chat session
MAX_BULLETS = 2
WHY_CHARS = 160
DETAIL_TTL = 7 * 86400

FITS = ("strong", "some", "none", "unknown")
NOTE = "I read only names, descriptions and READMEs, not your code."
LIMIT = ("I have already checked your repositories for this job three times in this chat. "
         "Start a new chat to check again.")
NO_REPOS = "There are no repositories of yours to check."
PRIVATE_NOTE = "Private, so no link: a recruiter could not open it."
NOT_READ = "Its name, description and topics do not mention this job's skills."
NO_TEXT = "It has no README and no description, so I could not judge it."

# (full_name lower, pushed_at) -> (when, {"readme": str, "languages": [str]})
_DETAILS: dict[tuple[str, str], tuple[float, dict]] = {}


def clear_cache() -> None:
    _DETAILS.clear()


# ----------------------------------------------------------------------------- text

def strip_markdown(md: str) -> str:
    """README markdown as plain text: no code blocks, images, badges, HTML, link targets or
    heading and emphasis marks."""
    t = str(md or "")
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)                    # images and badges
    t = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)                  # links keep their words
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+", "", t)
    t = re.sub(r"(?m)^\s*>\s?", "", t)
    t = re.sub(r"(\*\*|__|\*|_)(\S[^*_]*?\S|\S)\1", r"\2", t)
    t = re.sub(r"(?m)^\s*[-=|:]{3,}\s*$", " ", t)
    t = re.sub(r"https?://\S+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _words_of(name: str) -> str:
    return re.sub(r"[-_.]+", " ", str(name or ""))


def same_project(a: str, b: str) -> bool:
    """A repository and a resume project are the same thing: "gatecheck" and "Gate Check
    (JavaScript, OCR, Firebase)", "last-mile-ops-simulator" and "Last-Mile Operations Simulator".
    The bracketed tech list a resume adds is ignored."""
    from intake.links import same_title

    def core(x):
        return _words_of(re.sub(r"\([^)]*\)", " ", str(x or ""))).strip()
    x, y = core(a), core(b)
    if not x or not y:
        return False
    if re.sub(r"[^a-z0-9]", "", x.lower()) == re.sub(r"[^a-z0-9]", "", y.lower()):
        return True
    return same_title(x, y)


def full_name(card: dict, login: str = "") -> str:
    m = re.search(r"github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)", str(card.get("url") or ""), re.I)
    if m:
        return f"{m.group(1)}/{re.sub(r'.git$', '', m.group(2))}"
    return f"{login}/{card.get('name')}" if login else str(card.get("name") or "")


def meta_text(card: dict) -> str:
    return " ".join([_words_of(card.get("name")), str(card.get("description") or ""),
                     " ".join(_words_of(t) for t in card.get("topics") or []),
                     str(card.get("language") or "")])


def repo_text(card: dict, details: dict | None) -> str:
    d = details or {}
    return " ".join([meta_text(card), " ".join(d.get("languages") or []), d.get("readme") or ""])


def _when(iso: str):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def pre_score(card: dict, terms: list[str], now: datetime | None = None) -> float:
    """How promising a repository looks from its name, description, topics and language alone:
    one point per job skill named there, plus up to one for a recent push."""
    from tailoring.keywords import term_present
    text = meta_text(card)
    score = float(sum(1 for t in terms if term_present(t, text)))
    pushed = _when(card.get("pushed_at"))
    if pushed is not None:
        now = now or datetime.now(timezone.utc)
        days = (now - pushed).days
        score += 1.0 if days <= 90 else 0.5 if days <= 365 else 0.0
    return score


# ----------------------------------------------------------------------------- GitHub reads

def fetch_details(full: str, pushed_at: str, token: str = "", now: float | None = None) -> dict:
    """{"readme": plain text (first README_CHARS), "languages": [by bytes], "ok": bool} for one
    repository, cached by (full_name, pushed_at). A README GitHub does not have is ""."""
    now = time.time() if now is None else now
    key = (full.lower(), str(pushed_at or ""))
    hit = _DETAILS.get(key)
    if hit and now - hit[0] < DETAIL_TTL:
        return dict(hit[1])
    out = {"readme": "", "languages": [], "ok": True}
    try:
        status, data = github_publish.FETCH("GET", f"{github_publish.API}/repos/{full}/readme",
                                            token or "", None, None)
    except Exception:      # noqa: BLE001
        status, data = 0, None
    if status == 200 and isinstance(data, dict) and data.get("content"):
        try:
            raw = base64.b64decode(str(data["content"])).decode("utf-8", "replace")
        except (ValueError, TypeError):
            raw = ""
        out["readme"] = strip_markdown(raw)[:README_CHARS]
    elif status not in (200, 404):
        out["ok"] = False                      # rate limited or offline: not cached
    try:
        status, data = github_publish.FETCH("GET", f"{github_publish.API}/repos/{full}/languages",
                                            token or "", None, None)
    except Exception:      # noqa: BLE001
        status, data = 0, None
    if status == 200 and isinstance(data, dict):
        out["languages"] = [str(k) for k, _v in sorted(data.items(), key=lambda kv: -int(kv[1] or 0))][:5]
    elif status != 404:
        out["ok"] = False
    if out["ok"]:
        _DETAILS[key] = (now, dict(out))
    return out


# ----------------------------------------------------------------------------- checks

def checked_terms(claimed, text: str, jd: str) -> list[str]:
    """The claimed terms that literally appear in the repository's own text AND in the job."""
    from tailoring.keywords import term_present
    out: list[str] = []
    for t in claimed or []:
        t = str(t or "").strip()
        if t and len(t) <= 40 and term_present(t, text) and term_present(t, jd) \
                and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out[:8]


def _line(text: str, limit: int = WHY_CHARS) -> str:
    from tailoring.conform import strip_ai_dashes
    t = re.sub(r"\s+", " ", strip_ai_dashes(str(text or ""))).strip()
    t = re.sub(r"\bCVs\b", "resumes", re.sub(r"\bCV\b", "resume", t))
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return t


def checked_bullets(bullets, material: str) -> list[str]:
    """Draft bullets that state nothing beyond the README (the fact gate, with the README and
    the description as the only material). At most MAX_BULLETS."""
    from tailoring.fact_gate import gate_rewrite
    out = []
    for b in bullets or []:
        b = _line(b, 240)
        if len(b) < 25 or not material.strip():
            continue
        if not b.endswith("."):
            b += "."
        if gate_rewrite("", b, material):
            continue
        out.append(b)
        if len(out) >= MAX_BULLETS:
            break
    return out


def _fit(value, terms: list[str]) -> str:
    v = str(value or "").strip().lower()
    v = v if v in ("strong", "some", "none") else "none"
    if v == "strong" and not terms:
        return "some"
    return v


# ----------------------------------------------------------------------------- the ranking

def signature(jd: str, repos: list[dict], projects: list[dict]) -> str:
    body = json.dumps({"jd": str(jd or ""), "repos": sorted((str(r.get("name")), str(r.get("pushed_at")))
                                                            for r in repos),
                       "projects": [[p.get("name"), p.get("text")] for p in projects]}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def project_cards(projects: list[dict]) -> list[dict]:
    """The resume's own projects as {name, text} (name and bullets), for the same call."""
    out = []
    for p in projects or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("org") or p.get("title") or p.get("name") or "").strip()
        if not name:
            continue
        bits = [str(b.get("text") if isinstance(b, dict) else b or "") for b in p.get("bullets") or []]
        out.append({"name": name, "text": " ".join(x for x in bits if x.strip())[:800],
                    "link": str(p.get("link") or p.get("url") or "")})
    return out[:10]


def rank(llm, jd: str, repos: list[dict], resume_projects: list[dict], *, login: str = "",
         token: str = "", state: dict | None = None, now: datetime | None = None) -> dict:
    """{"ok", "repos": [...], "projects": [...], "terms", "cached"?, "error"?}.

    Each repo: {key, name, full_name, url, private, fit (strong/some/none/unknown), terms, why,
    bullets, tools, dates, read (README read)}. Each resume project: {name, fit, terms, why}.
    ``state`` is the session's {"count", "cache"}; without one there is no cap or cache."""
    from tailoring.keywords import skill_terms
    from ui.github_dates import dates_from
    state = state if state is not None else {}
    state.setdefault("count", 0)
    cache = state.setdefault("cache", {})
    now = now or datetime.now(timezone.utc)
    terms = skill_terms(jd or "")
    projs = project_cards(resume_projects)
    repos = [r for r in repos or [] if isinstance(r, dict) and r.get("name")]
    sig = signature(jd, repos, projs)
    if sig in cache:
        return {**cache[sig], "cached": True}
    if not repos and not projs:
        return {"ok": False, "repos": [], "projects": [], "terms": terms, "error": NO_REPOS}
    if state["count"] >= MAX_RANKINGS:
        return {"ok": False, "repos": [], "projects": [], "terms": terms, "error": LIMIT}
    order = sorted(range(len(repos)), key=lambda i: (-pre_score(repos[i], terms, now), i))
    top = [repos[i] for i in order[:K]]
    rest = [repos[i] for i in order[K:]]
    details = {}
    for r in top:
        fn = full_name(r, login)
        details[fn] = fetch_details(fn, r.get("pushed_at"), token)
    to_model, unjudgeable = [], []
    for i, r in enumerate(top):
        fn = full_name(r, login)
        d = details[fn]
        if not d.get("readme") and not str(r.get("description") or "").strip():
            unjudgeable.append(r)
            continue
        to_model.append({"id": f"r{i + 1}", "name": r["name"], "description": str(r.get("description") or ""),
                         "topics": list(r.get("topics") or []), "languages": d.get("languages") or
                         ([r["language"]] if r.get("language") else []), "readme": d.get("readme") or "",
                         "_card": r, "_full": fn})
    raw = {"repos": [], "projects": []}
    if to_model or projs:
        state["count"] += 1
        got = llm.rank_repos_for_jd(
            jd, terms, [{k: v for k, v in c.items() if not k.startswith("_")} for c in to_model],
            [{"id": f"p{i + 1}", "name": p["name"], "text": p["text"]} for i, p in enumerate(projs)])
        raw = got if isinstance(got, dict) else raw
    by_id = {str(x.get("id")): x for x in raw.get("repos") or [] if isinstance(x, dict)}
    out_repos = []

    def base(r: dict, fn: str) -> dict:
        d = details.get(fn) or {}
        langs = d.get("languages") or ([r["language"]] if r.get("language") else [])
        return {"key": fn.lower(), "name": r["name"], "full_name": fn,
                "url": "" if r.get("private") else str(r.get("url") or ""),
                "private": bool(r.get("private")), "tools": langs[:3],
                "dates": dates_from({"created_at": r.get("created_at"), "pushed_at": r.get("pushed_at")}, now),
                "read": bool(d.get("readme"))}

    for c in to_model:
        r, fn = c["_card"], c["_full"]
        j = by_id.get(c["id"]) or {}
        text = repo_text(r, details.get(fn))
        got_terms = checked_terms(j.get("terms"), text, jd)
        fit = _fit(j.get("fit"), got_terms)
        material = " ".join([str(r.get("description") or ""), (details.get(fn) or {}).get("readme") or ""])
        bullets = checked_bullets(j.get("bullets"), material) if fit in ("strong", "some") else []
        why = _line(j.get("why")) or (NOT_READ if fit == "none" else "")
        out_repos.append({**base(r, fn), "fit": fit, "terms": got_terms, "why": why, "bullets": bullets})
    for r in unjudgeable:
        fn = full_name(r, login)
        out_repos.append({**base(r, fn), "fit": "unknown", "terms": [], "why": NO_TEXT, "bullets": []})
    for r in rest:
        fn = full_name(r, login)
        if not str(r.get("description") or "").strip():
            out_repos.append({**base(r, fn), "fit": "unknown", "terms": [], "why": NO_TEXT, "bullets": []})
        else:
            out_repos.append({**base(r, fn), "fit": "none", "terms": [], "why": NOT_READ, "bullets": []})
    rank_of = {"strong": 0, "some": 1, "none": 2, "unknown": 3}
    out_repos.sort(key=lambda x: (rank_of[x["fit"]], -len(x["terms"])))
    pj = {str(x.get("id")): x for x in raw.get("projects") or [] if isinstance(x, dict)}
    out_projs = []
    for i, p in enumerate(projs):
        j = pj.get(f"p{i + 1}") or {}
        text = p["name"] + " " + p["text"]
        got_terms = checked_terms(j.get("terms"), text, jd)
        fit = _fit(j.get("fit"), got_terms) if p["text"].strip() or got_terms else "unknown"
        out_projs.append({"name": p["name"], "fit": fit, "terms": got_terms,
                          "why": _line(j.get("why")) or ("Only its name is on the resume, so I could "
                                                         "not judge it." if fit == "unknown" else "")})
    result = {"ok": True, "repos": out_repos, "projects": out_projs, "terms": terms}
    cache[sig] = result
    return dict(result)


def entry_for(repo: dict) -> dict:
    """A resume project entry from one ranked repository: its name, the real link (none for a
    private one), the dates GitHub gives, the checked bullets, and a "Built with" line from the
    languages GitHub reports when there are fewer than two bullets."""
    bullets = list(repo.get("bullets") or [])[:MAX_BULLETS]
    tools = [t for t in repo.get("tools") or [] if t]
    if len(bullets) < 2 and tools:
        bullets.append(f"Built with {', '.join(tools[:3])}.")
    return {"org": str(repo["name"]),            # the name as it is on GitHub, so it matches the repo
            "title": "", "dates": str(repo.get("dates") or ""), "location": "Personal Project",
            "bullets": bullets, "personal": True,
            "link": "" if repo.get("private") else str(repo.get("url") or ""),
            "tools": ", ".join(tools[:3]), "source": "github", "repo": repo.get("full_name") or ""}
