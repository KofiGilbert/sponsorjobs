"""The person's OWN GitHub, on their click: example repos for ideas, a private starter repo
when they start a project, and making it public once they have defended it.

Rules, consistent with the rest of the app's Connections model:
- Only ever the person's OWN account, and only on their explicit click (creating a repo,
  opening issues, or changing visibility is an outward action, never done unattended).
- Auth is a Personal Access Token stored LOCALLY and git-ignored, like the other
  Connections credentials. No cloud, no server.
- We never push code for the person. A started repo holds a README to fill in and the
  build guide's milestones as issues; the code is theirs to write.
- Example repos come only from GitHub's own search results. A URL is never invented: if
  search fails or returns nothing usable, there are simply no examples.

Every HTTP call goes through ``FETCH(method, url, token, body, params) -> (status, json)``,
a module-level seam tests replace with fixtures so nothing touches the network.
"""

from __future__ import annotations

import base64
import re

API = "https://api.github.com"


def _headers(token: str = "") -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "SponsorJobs-desktop"}
    if token:
        h["Authorization"] = f"token {token}"
    return h


def _default_fetch(method: str, url: str, token: str = "", body: dict | None = None,
                   params: dict | None = None, timeout: float = 10) -> tuple[int, object]:
    """One GitHub API call. Returns (status, parsed JSON or None). A network failure is
    status 0, so callers handle "unreachable" and "refused" the same simple way."""
    import requests
    try:
        r = requests.request(method, url, headers=_headers(token), json=body, params=params,
                             timeout=timeout)
    except Exception:
        return 0, None
    try:
        data = r.json()
    except Exception:
        data = None
    return r.status_code, data


FETCH = _default_fetch


def _fetch(method, url, token="", body=None, params=None):
    return FETCH(method, url, token, body, params)


def slugify(title: str) -> str:
    """A GitHub-safe repo name from a project title: lower-kebab, alnum + hyphens."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (s or "project")[:80]


def readme_for(project: dict) -> str:
    """A clean, honest README from the verified project: what it is, the tech, the build
    log from the plan, and how to push the code. About their project, no branding."""
    title = project.get("title") or "Project"
    parts = [f"# {title}", ""]
    if project.get("summary"):
        parts += [project["summary"], ""]
    if project.get("tech"):
        parts += ["## Tech", "", project["tech"], ""]
    milestones = ((project.get("plan") or {}).get("milestones")) or []
    if milestones:
        parts += ["## What I built", ""]
        for m in milestones:
            t = str(m.get("title") or "").strip()
            if t:
                parts.append(f"- {t}")
        parts.append("")
    parts += ["## Run it", "",
              "Clone the repo and add your project files, then push:", "",
              "```",
              "git init",
              "git add .",
              'git commit -m "Initial commit"',
              "git branch -M main",
              "git remote add origin <this repo's URL>",
              "git push -u origin main",
              "```", ""]
    return "\n".join(parts)


# The sections recruiters read on a pinned repo, each with one line telling the person what
# to write. Prompts, not content: the README is theirs to fill in as they build.
STARTER_SECTIONS = (
    ("Problem", "What problem does this solve, and for whom? One or two sentences."),
    ("Approach", "How you solved it, and one decision you made along the way."),
    ("Tech stack", "The languages, libraries, and services you used."),
    ("How to run", "The exact commands to install and run it locally."),
    ("Screenshots", "A screenshot or GIF of it working. Add a live demo link if you have one."),
    ("What I learned", "One thing that was hard, and how you worked through it."),
)


def starter_readme(project: dict) -> str:
    """The README a started repo begins with: the title, the one-line pitch, and the six
    sections with a fill-in prompt each. The tech line is pre-filled from the idea."""
    title = project.get("title") or "Project"
    parts = [f"# {title}", ""]
    if project.get("summary"):
        parts += [str(project["summary"]).strip(), ""]
    for head, prompt in STARTER_SECTIONS:
        parts += [f"## {head}", "", f"_{prompt}_", ""]
        if head == "Tech stack" and project.get("tech"):
            parts += [f"Planned: {project['tech']}", ""]
    return "\n".join(parts)


def validate_token(token: str) -> dict:
    """Confirm the token works and return the login. {'ok', 'login'} or {'ok': False, 'error'}."""
    status, data = _fetch("GET", f"{API}/user", token)
    if status == 0:
        return {"ok": False, "error": "Couldn't reach GitHub to verify the token. Check your connection."}
    if status == 200:
        return {"ok": True, "login": (data or {}).get("login", "") if isinstance(data, dict) else ""}
    return {"ok": False, "error": "That token was rejected by GitHub. Create a token with 'repo' scope and paste it again."}


def _create_repo(token: str, name: str, desc: str, private: bool) -> dict:
    """POST /user/repos, stepping to name-2, name-3 when the person already has that name."""
    base = slugify(name)
    for n in range(1, 4):
        cand = base if n == 1 else f"{base[:76]}-{n}"
        status, data = _fetch("POST", f"{API}/user/repos", token,
                              {"name": cand, "description": desc[:280], "private": bool(private),
                               "auto_init": False, "has_issues": True})
        if status == 0:
            return {"ok": False, "error": "Couldn't reach GitHub to create the repo. Check your connection."}
        if status == 422:
            continue
        if status not in (200, 201) or not isinstance(data, dict):
            return {"ok": False, "error": "GitHub wouldn't create the repo. Check the token has 'repo' scope."}
        return {"ok": True, "url": data.get("html_url", ""), "full_name": data.get("full_name", ""),
                "private": bool(data.get("private", private))}
    return {"ok": False, "error": f"You already have repos named '{base}'. Rename the project and try again."}


def _put_readme(token: str, full_name: str, text: str) -> bool:
    status, _ = _fetch("PUT", f"{API}/repos/{full_name}/contents/README.md", token,
                       {"message": "Add README", "content": base64.b64encode(text.encode()).decode()})
    return status in (200, 201)


def create_starter_repo(token: str, project: dict) -> dict:
    """Start a project on the person's GitHub: a PRIVATE repo, the starter README, and one
    issue per build-guide milestone. No code is pushed. Returns {'ok', 'url', 'full_name',
    'private', 'issues'} or {'ok': False, 'error'}. README or issue failures are non-fatal:
    the repo exists and the person can add them by hand."""
    name = project.get("repo") or project.get("title", "")
    res = _create_repo(token, name, project.get("summary") or "", private=True)
    if not res.get("ok"):
        return res
    full = res["full_name"]
    res["readme"] = _put_readme(token, full, starter_readme(project))
    issues = 0
    for m in ((project.get("plan") or {}).get("milestones")) or []:
        title = str((m or {}).get("title") or "").strip()
        if not title:
            continue
        lines = []
        for key, label in (("build", "Build"), ("learn", "Learn"), ("check", "Done when")):
            if str(m.get(key) or "").strip():
                lines.append(f"**{label}:** {str(m[key]).strip()}")
        status, _ = _fetch("POST", f"{API}/repos/{full}/issues", token,
                           {"title": title, "body": "\n\n".join(lines)})
        if status in (200, 201):
            issues += 1
    res["issues"] = issues
    return res


def make_public(token: str, full_name: str) -> dict:
    """Flip an existing repo to public (PATCH /repos/{owner}/{repo}). {'ok', 'url'} or error."""
    status, data = _fetch("PATCH", f"{API}/repos/{full_name}", token, {"private": False})
    if status == 0:
        return {"ok": False, "error": "Couldn't reach GitHub. Check your connection."}
    if status == 404:
        return {"ok": False, "error": "GitHub couldn't find that repo. It may have been renamed or deleted.",
                "missing": True}
    if status != 200 or not isinstance(data, dict):
        return {"ok": False, "error": "GitHub wouldn't make the repo public. Check the token has 'repo' scope."}
    return {"ok": True, "url": data.get("html_url", ""), "full_name": data.get("full_name", full_name)}


def publish_project(token: str, project: dict, private: bool = False) -> dict:
    """Make a verified project public. A project started on GitHub already has a private
    repo, so that repo is flipped to public; otherwise a public repo is created with a README
    seeded from what they built. Returns {'ok', 'url', 'full_name'} or {'ok': False, 'error'}."""
    full = project.get("repo_full_name") or ""
    if full:
        res = make_public(token, full)
        if res.get("ok") or not res.get("missing"):
            return res
        # The started repo is gone: fall through and create a fresh public one.
    res = _create_repo(token, project.get("repo") or project.get("title", ""),
                       project.get("summary") or "", private=private)
    if not res.get("ok"):
        return res
    _put_readme(token, res["full_name"], readme_for(project))
    return {"ok": True, "url": res["url"], "full_name": res["full_name"]}


# ---- Example repos: real public repos from GitHub's search, for inspiration only. ----
_REPO_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def search_examples(query: str, token: str = "", limit: int = 3) -> list[dict]:
    """Up to ``limit`` public example repos for an idea, straight from GitHub's search API.
    Only repos the search returned, each checked for a well-formed github.com URL that matches
    its full_name; forks and archived repos are skipped. Any failure means no examples."""
    q = re.sub(r"\s+", " ", str(query or "")).strip()[:120]
    if not q:
        return []
    def _search(words: str):
        return _fetch("GET", f"{API}/search/repositories", token, None,
                      {"q": f"{words} fork:false archived:false stars:>10", "sort": "stars",
                       "order": "desc", "per_page": 10})
    status, data = _search(q)
    if status != 200 or not isinstance(data, dict):
        return []
    if not data.get("items") and len(q.split()) > 2:
        # Search ANDs every word, so a long phrase often finds nothing: retry with the first two.
        status, data = _search(" ".join(q.split()[:2]))
        if status != 200 or not isinstance(data, dict):
            return []
    out = []
    for it in data.get("items") or []:
        if not isinstance(it, dict) or it.get("fork") or it.get("archived") or it.get("private"):
            continue
        full, url = str(it.get("full_name") or ""), str(it.get("html_url") or "")
        if not _REPO_URL.match(url) or url.lower() != f"https://github.com/{full}".lower():
            continue
        try:
            stars = int(it.get("stargazers_count") or 0)
        except (TypeError, ValueError):
            stars = 0
        out.append({"full_name": full, "url": url, "stars": stars,
                    "description": str(it.get("description") or "")[:160]})
        if len(out) >= limit:
            break
    return out
