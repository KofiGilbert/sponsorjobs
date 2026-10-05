"""Publish a verified project to the person's OWN GitHub account, on their click.

Feature 2, phase 4 of the guided project builder: once a project has passed the
defend-your-work gate, the person can push it to GitHub (a repo + a README seeded
from what they built) and link it on their CV.

Rules, consistent with the rest of the app's Connections model:
- Only ever the person's OWN account, and only on their explicit click (creating a
  repo is an outward action, never done autonomously without that click).
- Auth is a Personal Access Token stored LOCALLY and git-ignored, like the other
  Connections credentials. No cloud, no server.
- Identifies honestly. No stealth, no automation of anyone else's account.
"""

from __future__ import annotations

import re

API = "https://api.github.com"


def _headers(token: str) -> dict:
    return {"Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


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


def validate_token(token: str) -> dict:
    """Confirm the token works and return the login. {'ok', 'login'} or {'ok': False, 'error'}."""
    import requests
    try:
        r = requests.get(f"{API}/user", headers=_headers(token), timeout=8)
    except Exception:
        return {"ok": False, "error": "Couldn't reach GitHub to verify the token. Check your connection."}
    if r.status_code == 200:
        return {"ok": True, "login": (r.json() or {}).get("login", "")}
    return {"ok": False, "error": "That token was rejected by GitHub. Create a token with 'repo' scope and paste it again."}


def publish_project(token: str, project: dict, private: bool = False) -> dict:
    """Create a repo on the user's account and seed a README from the project.
    Returns {'ok', 'url', 'full_name'} or {'ok': False, 'error'}.
    Creates without auto_init, then writes README.md as the first commit."""
    import requests
    name = slugify(project.get("title", ""))
    desc = (project.get("summary") or "")[:280]
    try:
        r = requests.post(f"{API}/user/repos", headers=_headers(token), timeout=12,
                          json={"name": name, "description": desc, "private": bool(private),
                                "auto_init": False})
    except Exception:
        return {"ok": False, "error": "Couldn't reach GitHub to create the repo. Check your connection."}
    if r.status_code == 422:
        return {"ok": False, "error": f"You already have a repo named '{name}'. Rename the project or delete that repo first."}
    if r.status_code not in (200, 201):
        return {"ok": False, "error": "GitHub wouldn't create the repo. Check the token has 'repo' scope."}
    repo = r.json() or {}
    full_name, url = repo.get("full_name", ""), repo.get("html_url", "")
    # Seed the README (first commit). A failure here is non-fatal: the repo exists.
    import base64
    try:
        requests.put(f"{API}/repos/{full_name}/contents/README.md", headers=_headers(token), timeout=12,
                     json={"message": "Add README", "content": base64.b64encode(readme_for(project).encode()).decode()})
    except Exception:
        pass
    return {"ok": True, "url": url, "full_name": full_name}
