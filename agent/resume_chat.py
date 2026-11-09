"""The resume chat agent ("one brain", first piece): the resume chat answers with tools, so what
it says about the person's resume, their upload and their GitHub comes from what the app really
holds, read in that turn. Behind the flag TAILOR_CHAT_AGENT (off by default; off means the chat
behaves exactly as before).

One turn (``run_turn``) is at most MAX_ROUNDS rounds of tool calls, then the answer. Tools only
READ: the resume being built, the last upload, the GitHub connection, the person's public
repositories, and how well those and the resume's own projects fit this job (ui/repo_rank.py,
whose README text never enters this loop). Nothing here changes the resume, so ``did`` is
always empty and ``honest`` strips any sentence that claims a save or an addition. A tool that
offers a change (rank_repos_for_jd, propose_add_projects) returns a card with a single-use
action token; only the person's click spends it (POST /api/session/action ->
WebIntake.apply_action), and the token never reaches the model.

The app's pieces reach this module only through ``AppContext`` callables, so it never imports
ui/app.py (the same split as agent/home.py: decide and word things here, act in the routes).
The texts the person reads are built here so tests can pin them: plain words, no dashes,
"resume" never "CV". The GitHub token never appears in a tool result, a card, a log line or a
model call: the list_repos callable holds it, and every tool result is scrubbed of the
context's secrets before the model sees it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

FLAG = "TAILOR_CHAT_AGENT"
MAX_ROUNDS = 4
MAX_REPOS_TO_MODEL = 40
MAX_BUTTONS = 3

# Where a card button may take the person (ui/static/app.js renderCard knows the same names).
NAV_TARGETS = ("projects", "connections", "cvs", "jobs")

POLITE_FAIL = "I could not finish that just now. Ask me again in a moment."
TOOL_FAIL = "This lookup did not work just now."
NOT_CHANGED = "I have not changed your resume."
GITHUB_CONNECT_STEPS = ("click your name at the bottom of the left sidebar, choose Settings, then "
                        "Connections, and click Connect next to GitHub")


def enabled() -> bool:
    return os.environ.get(FLAG, "").strip().lower() in ("1", "true", "on", "yes")


@dataclass
class AppContext:
    """What the agent can reach, as callables the session wires up."""
    llm: Any
    resume_snapshot: Callable[[], dict]
    upload_summary: Callable[[], dict | None]
    github_status: Callable[[], dict]              # {"connected", "login", "resume_login"}
    list_repos: Callable[[str], dict]              # login -> ui/github_repos.list_repos result
    history: Callable[[], list] = field(default=lambda: [])
    secrets: Callable[[], list] = field(default=lambda: [])
    # login -> ui/repo_rank.rank result (the session holds the cap, the cache and the token).
    # README text stays inside it: only fits, checked terms and one line "why" reach the model.
    rank_repos: Callable[[str], dict] | None = None
    # ranked repos the person may add -> a single-use action token for the card's button. Only
    # POST /api/session/action spends it (the person's click); no tool can.
    offer: Callable[[list], str] | None = None
    # () -> project ideas for THIS job (ui/app._session_ideas: the open job's role and JD skills,
    # the daily ideas cap, the cache), and ideas -> a single-use token for the ideas card's
    # "Build this for this job" buttons. Only the person's click starts a build (one package).
    suggest_ideas: Callable[[], dict] | None = None
    offer_build: Callable[[list], str] | None = None
    role: str = ""
    company: str = ""


@dataclass
class Tool:
    name: str
    description: str
    run: Callable[[AppContext, dict], dict]
    schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    mutates: bool = False

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


# ----------------------------------------------------------------------------- the tools

def _t_resume_snapshot(ctx: AppContext, inp: dict) -> dict:
    return dict(ctx.resume_snapshot() or {})


def _t_upload_summary(ctx: AppContext, inp: dict) -> dict:
    up = ctx.upload_summary()
    if not up:
        return {"uploaded": False, "note": "No file has been uploaded in this chat yet."}
    out = {"uploaded": True}
    for k in ("name", "kind", "roles", "projects", "project_links", "links"):
        if up.get(k):
            out[k] = up[k]
    if up.get("kind") == "resume" and not up.get("projects"):
        out["projects"] = []
        out["note"] = "No projects were found in the file."
    return out


def _t_github_status(ctx: AppContext, inp: dict) -> dict:
    gh = dict(ctx.github_status() or {})
    out = {"connected": bool(gh.get("connected")), "login": str(gh.get("login") or ""),
           "username_on_resume": str(gh.get("resume_login") or "")}
    if not out["connected"]:
        out["how_to_connect"] = GITHUB_CONNECT_STEPS
        out["card"] = {"type": "buttons", "buttons": [{"label": "Connect GitHub", "nav": "connections"}]}
    if out["connected"] or out["username_on_resume"]:
        out["public_repos_readable"] = True
    return out


def _t_list_repos(ctx: AppContext, inp: dict) -> dict:
    login = str((inp or {}).get("login") or "").strip()
    if not login:
        gh = dict(ctx.github_status() or {})
        login = str(gh.get("resume_login") or gh.get("login") or "")
    if not login:
        return {"ok": False, "repos": [],
                "error": "There is no GitHub username on the resume and GitHub is not connected. "
                         "Ask the person for their GitHub username or profile link."}
    got = dict(ctx.list_repos(login) or {})
    repos = list(got.get("repos") or [])
    out = {"ok": bool(got.get("ok")), "login": got.get("login") or login,
           "count": len(repos), "repos": repos[:MAX_REPOS_TO_MODEL]}
    if len(repos) > MAX_REPOS_TO_MODEL:
        out["note"] = f"Showing the {MAX_REPOS_TO_MODEL} most recently pushed of {len(repos)}."
    if got.get("dropped"):
        out["left_out"] = got["dropped"]
    if got.get("error"):
        out["error"] = got["error"]
    if out["ok"] and repos:
        out["card"] = {"type": "buttons", "buttons": [
            {"label": "Which fit this job?", "send": "Which of my GitHub repositories fit this job?"},
            {"label": "Open Projects", "nav": "projects"}]}
    return out


def _login(ctx: AppContext, inp: dict) -> str:
    login = str((inp or {}).get("login") or "").strip()
    if not login:
        gh = dict(ctx.github_status() or {})
        login = str(gh.get("resume_login") or gh.get("login") or "")
    return login


FIT_WORDS = {"strong": "strong fit", "some": "some fit", "none": "does not fit", "unknown": "could not judge"}
ADD_LABEL = "Add selected to this resume"
BUILD_LABEL = "Build a project for this job instead"
ADD_HINT = ("The person sees these in a card with checkboxes. To add any, they tick it and click "
            f"\"{ADD_LABEL}\". You cannot add projects yourself.")


def repo_fit_card(got: dict, token: str, picked: list | None = None, only: list | None = None) -> dict:
    """The repo_fit card: fits (a checkbox each), doesn't fit and couldn't judge (one line each,
    collapsed), the resume's own projects on the same scale, and the honest footnote. ``only``
    (keys) shows just those as the fits, for a proposal the person asked for by name."""
    repos = [r for r in got.get("repos") or [] if isinstance(r, dict)]
    if only is not None:
        keys = set(only)
        fits = [r for r in repos if r.get("key") in keys]
        rest = []
    else:
        fits = [r for r in repos if r.get("fit") in ("strong", "some")]
        rest = [r for r in repos if r.get("fit") not in ("strong", "some")]
    from ui.repo_rank import NOTE, PRIVATE_NOTE

    def row(r):
        return {"key": r.get("key"), "name": r.get("name"), "url": r.get("url") or "",
                "fit": r.get("fit"), "terms": list(r.get("terms") or [])[:6], "why": r.get("why") or "",
                "on_resume": bool(r.get("on_resume")),
                **({"note": PRIVATE_NOTE} if r.get("private") else {})}
    return {"type": "repo_fit", "token": token,
            "fits": [row(r) for r in fits],
            "no_fit": [{"name": r.get("name"), "why": r.get("why") or ""} for r in rest if r.get("fit") == "none"],
            "unknown": [{"name": r.get("name"), "why": r.get("why") or ""} for r in rest if r.get("fit") == "unknown"],
            "projects": [{"name": p.get("name"), "fit": p.get("fit"), "terms": list(p.get("terms") or [])[:6],
                          "why": p.get("why") or ""} for p in got.get("projects") or []],
            "picked": [k for k in (picked or []) if any(f.get("key") == k for f in fits)],
            "note": NOTE}


def _for_model(r: dict) -> dict:
    return {"name": r.get("name"), "fit": FIT_WORDS.get(r.get("fit"), r.get("fit")),
            "skills_it_shows": list(r.get("terms") or []), "why": r.get("why") or "",
            **({"already_on_resume": True} if r.get("on_resume") else {}),
            **({"private": True} if r.get("private") else {})}


def _ranked(ctx: AppContext, inp: dict) -> dict:
    if ctx.rank_repos is None:
        raise RuntimeError("ranking is not wired")
    return dict(ctx.rank_repos(_login(ctx, inp)) or {})


def _t_rank_repos(ctx: AppContext, inp: dict) -> dict:
    got = _ranked(ctx, inp)
    if not got.get("ok"):
        return {"ok": False, "error": got.get("error") or TOOL_FAIL}
    repos = [r for r in got.get("repos") or [] if isinstance(r, dict)]
    addable = [r for r in repos if r.get("fit") in ("strong", "some") and not r.get("on_resume")]
    token = ctx.offer(addable) if (ctx.offer and addable) else ""
    from ui.repo_rank import NOTE
    out = {"ok": True, "job_skills": list(got.get("terms") or [])[:15],
           "fits": [_for_model(r) for r in repos if r.get("fit") in ("strong", "some")],
           "doesnt_fit": [r.get("name") for r in repos if r.get("fit") == "none"],
           "couldnt_judge": [r.get("name") for r in repos if r.get("fit") == "unknown"],
           "resume_projects": [_for_model(p) for p in got.get("projects") or []],
           "how_judged": NOTE, "how_to_add": ADD_HINT}
    out["card"] = repo_fit_card(got, token)
    return out


def _t_assess_projects(ctx: AppContext, inp: dict) -> dict:
    got = _ranked(ctx, inp)
    if not got.get("ok"):
        return {"ok": False, "error": got.get("error") or TOOL_FAIL}
    return {"ok": True, "job_skills": list(got.get("terms") or [])[:15],
            "resume_projects": [_for_model(p) for p in got.get("projects") or []]}


def _t_propose_add(ctx: AppContext, inp: dict) -> dict:
    names = [str(n).strip().lower() for n in (inp or {}).get("names") or [] if str(n).strip()]
    if not names:
        return {"ok": False, "error": "Say which repositories to add, by name."}
    got = _ranked(ctx, inp)
    if not got.get("ok"):
        return {"ok": False, "error": got.get("error") or TOOL_FAIL}
    repos = [r for r in got.get("repos") or [] if isinstance(r, dict)]

    def hit(r, n):
        return n in (str(r.get("name") or "").lower(), str(r.get("full_name") or "").lower())
    found = [r for r in repos if any(hit(r, n) for n in names) and not r.get("on_resume")]
    missing = [n for n in names if not any(hit(r, n) for r in repos)]
    already = [r.get("name") for r in repos if any(hit(r, n) for n in names) and r.get("on_resume")]
    out = {"ok": bool(found), "offered": [r.get("name") for r in found], "not_found": missing,
           "already_on_resume": already, "how_to_add": ADD_HINT}
    if found and ctx.offer:
        keys = [r.get("key") for r in found]
        out["card"] = repo_fit_card(got, ctx.offer(found), picked=keys, only=keys)
    return out


BUILD_LABEL_IDEA = "Build this for this job"
IDEAS_HINT = ("The person sees these ideas in a card. Each has a \"" + BUILD_LABEL_IDEA + "\" button: "
              "one click has the AI build a small working project for this job (it uses one package "
              "and takes a few minutes). Then they learn it and pass its quiz, and it is added to "
              "this resume. You cannot start a build yourself, and nothing is on the resume yet.")


def _idea_key(i: dict) -> str:
    return str(i.get("repo") or i.get("title") or "").strip()[:80]


def ideas_card(ideas: list, token: str, role: str = "") -> dict:
    """The ideas card: each idea as the Projects page shows it (ui/static/app.js ideaCard,
    compact), with its own "Build this for this job" button spending the card's token."""
    rows = []
    for i in ideas or []:
        if not isinstance(i, dict) or not str(i.get("title") or "").strip():
            continue
        rows.append({"key": _idea_key(i), "title": i.get("title"), "pitch": i.get("pitch") or "",
                     "skills": list(i.get("skills") or [])[:5], "gaps": list(i.get("gaps") or [])[:5],
                     "tech": i.get("tech") or "", "repo": i.get("repo") or "",
                     "examples": list(i.get("examples") or [])[:3]})
    return {"type": "ideas", "token": token, "role": role, "ideas": rows}


def _t_suggest_ideas(ctx: AppContext, inp: dict) -> dict:
    if ctx.suggest_ideas is None:
        raise RuntimeError("ideas are not wired")
    got = dict(ctx.suggest_ideas() or {})
    ideas = [i for i in got.get("ideas") or [] if isinstance(i, dict) and i.get("title")][:6]
    if not ideas:
        return {"ok": False, "error": got.get("error") or "I could not come up with ideas just now.",
                **({"daily_limit_reached": True} if got.get("code") == "ideas_cap" else {})}
    token = ctx.offer_build(ideas) if ctx.offer_build else ""
    out = {"ok": True, "for_role": got.get("role") or ctx.role,
           "ideas": [{"title": i.get("title"), "pitch": i.get("pitch") or "",
                      "skills": list(i.get("skills") or [])[:5],
                      "job_skills_your_resume_lacks": list(i.get("gaps") or [])[:5]} for i in ideas],
           "how_to_build": IDEAS_HINT}
    out["card"] = ideas_card(ideas, token, str(got.get("role") or ctx.role or ""))
    return out


TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool("resume_snapshot",
         "See the resume being built now: the person's name and links (GitHub, LinkedIn), their "
         "roles, projects, skills and education, and whether a draft exists yet.",
         _t_resume_snapshot),
    Tool("read_upload_summary",
         "See what the app read from the last file the person uploaded in this chat: its name, "
         "kind, the roles, the projects with their links, and the profile links found in it.",
         _t_upload_summary),
    Tool("github_status",
         "Check whether the person's GitHub is connected, the connected login, and the GitHub "
         "username on their resume.",
         _t_github_status),
    Tool("list_repos",
         "List the person's own public GitHub repositories (no sign in needed): name, link, "
         "description, topics, language, stars and last push, newest first. Forks, archived and "
         "empty repositories are left out. Uses the username on the resume unless you pass one "
         "the person gave.",
         _t_list_repos,
         schema={"type": "object", "properties": {
             "login": {"type": "string",
                       "description": "A GitHub username the person gave, only if it differs "
                                      "from the one on their resume."}}}),
    Tool("rank_repos_for_jd",
         "Judge which of the person's own public GitHub repositories fit this job, and the "
         "projects already on their resume on the same scale: strong fit, some fit, does not fit "
         "or could not judge, with the job skills each one really shows and why. The person gets "
         "a card with checkboxes to add the fits to this resume. It reads names, descriptions and "
         "READMEs, not code.",
         _t_rank_repos,
         schema={"type": "object", "properties": {
             "login": {"type": "string",
                       "description": "A GitHub username the person gave, only if it differs "
                                      "from the one on their resume."}}}),
    Tool("assess_resume_projects",
         "Judge how well each project already on the resume fits this job, with the job skills "
         "each one really shows and why.",
         _t_assess_projects),
    Tool("propose_add_projects",
         "When the person asks to add named repositories to this resume, give them a card to "
         "confirm. This does not add anything: they confirm with the card's button.",
         _t_propose_add,
         schema={"type": "object", "properties": {
             "names": {"type": "array", "items": {"type": "string"},
                       "description": "The repository names the person asked to add."}},
             "required": ["names"]}),
    Tool("suggest_projects_for_jd",
         "When the person wants a project to build for this job (or has none that fits), suggest "
         "small GitHub projects aimed at this job's role and the skills its description asks for. "
         "They get a card where one click has the AI build the one they pick for this job.",
         _t_suggest_ideas),
)}


# ----------------------------------------------------------------------------- the prompt

def system_prompt(tools: dict[str, Tool], role: str = "", company: str = "") -> str:
    """The agent's instructions, built from the registered tools so the chat only describes
    what it can really do."""
    job = f" for the {role} role" + (f" at {company}" if company else "") if role else ""
    lines = [
        f"You are the resume chat inside the SponsorJobs desktop app, helping the person with "
        f"their resume{job}. You answer from what the app really holds, read through your tools.",
        "",
        "You can do only these things, each through a tool:",
    ]
    lines += [f"- {t.name}: {t.description}" for t in tools.values()]
    lines += [
        "",
        "Rules:",
        "- Call a tool before you say anything about the resume, an upload or GitHub. Never "
        "guess, and never invent a project, repository, link, date or fact.",
        "- You cannot change the resume, add projects, publish anything or connect GitHub "
        "yourself. Never say you saved, added, updated or changed anything. When the person "
        "wants a change, tell them to say it in this chat with the details, or point them to "
        "the button you were given. When a tool gave them a card with checkboxes, tell them to "
        "tick what they want and click its button.",
        "- When you judge repositories, say plainly that you read names, descriptions and "
        "READMEs, not the code, and name the job skills each fit really shows.",
        "- If a tool says something failed or was rate limited, say so in one plain line.",
        "- Be short and warm: a few sentences, or a short list (one item per line) when listing "
        "things. Plain text, no markdown headings or tables. Use plain words, no dashes as "
        "punctuation, and say resume, never CV.",
        "- Never describe a capability that is not in the list above.",
    ]
    if "suggest_projects_for_jd" in tools:
        lines.append(
            "- When the person wants a project to build for this job, call suggest_projects_for_jd. "
            "Say these are ideas for this job, that one click on an idea's button has the AI build "
            "it (one package), and that it reaches the resume only after they learn it and pass "
            "its quiz. Never present an idea as something the person has done.")
    return "\n".join(lines)


LAST_ROUND = ("\n\nThis is your last step for this message: answer the person now from what the "
              "tools returned, without calling any tool.")


# ----------------------------------------------------------------------------- honesty

def honest(text: str, did: list | None) -> str:
    """The reply without any sentence that claims a save or an addition, when nothing changed
    this turn (the precedent is notify/assistant.py). Never empty."""
    if did:
        return text
    from notify.assistant import _CLAIM, _NEG
    lines, dropped = [], False
    for line in str(text or "").strip().split("\n"):       # a list keeps its lines
        kept = []
        for s in re.split(r"(?<=[.!?])\s+", line):
            if s.strip() and _CLAIM.search(s) and not _NEG.search(s):
                dropped = True
                continue
            kept.append(s)
        joined = " ".join(kept).rstrip()
        if joined.strip() or not line.strip():
            lines.append(joined)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if dropped:
        out = (out + " " + NOT_CHANGED).strip()
    return out or POLITE_FAIL


def _clean(text: str) -> str:
    from tailoring.conform import strip_ai_dashes
    t = strip_ai_dashes(str(text or "").strip())
    t = re.sub(r"\bCVs\b", "resumes", re.sub(r"\bCV\b", "resume", t))
    # The chat shows plain text and re-allows only a bare <b> (ui/static/app.js chatText), so
    # markdown bold becomes that and a markdown heading becomes a plain line.
    t = re.sub(r"\*\*([^*\n]+?)\*\*", r"<b>\1</b>", t)
    return re.sub(r"(?m)^#{1,6}\s+", "", t)


# ----------------------------------------------------------------------------- cards

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _s(v, n: int = 160) -> str:
    return str(v or "").strip()[:n]


def _valid_repo_fit(card: dict) -> dict | None:
    """A repo_fit card with only the fields renderCard knows, every string clipped, links only
    to https://github.com, and a token of the shape the session issues (or none)."""
    tok = str(card.get("token") or "")

    def url(u):
        u = str(u or "")
        return u if u.startswith("https://github.com/") and len(u) < 200 else ""

    fits = [{"key": _s(f.get("key"), 140), "name": _s(f.get("name"), 100), "url": url(f.get("url")),
             "fit": f.get("fit") if f.get("fit") in ("strong", "some", "none", "unknown") else "unknown",
             "terms": [_s(t, 40) for t in (f.get("terms") or [])[:6]], "why": _s(f.get("why")),
             "on_resume": bool(f.get("on_resume")), **({"note": _s(f["note"])} if f.get("note") else {})}
            for f in card.get("fits") or [] if isinstance(f, dict) and f.get("name")]
    lines = lambda k: [{"name": _s(x.get("name"), 100), "why": _s(x.get("why"))}   # noqa: E731
                       for x in card.get(k) or [] if isinstance(x, dict) and x.get("name")][:40]
    projs = [{"name": _s(p.get("name"), 100), "fit": p.get("fit") if p.get("fit") in FIT_WORDS else "unknown",
              "terms": [_s(t, 40) for t in (p.get("terms") or [])[:6]], "why": _s(p.get("why"))}
             for p in card.get("projects") or [] if isinstance(p, dict) and p.get("name")][:10]
    if not (fits or card.get("no_fit") or card.get("unknown") or projs):
        return None
    keys = {f["key"] for f in fits}
    out = {"type": "repo_fit", "token": tok if _TOKEN.match(tok) else "", "fits": fits[:12],
           "no_fit": lines("no_fit"), "unknown": lines("unknown"), "projects": projs,
           "picked": [k for k in card.get("picked") or [] if k in keys], "note": _s(card.get("note"), 120)}
    for k in ("used", "added"):
        if card.get(k):
            out[k] = card[k] if k == "used" else [_s(x, 100) for x in card[k]][:12]
    return out


def _gh_url(u) -> str:
    u = str(u or "")
    return u if u.startswith("https://github.com/") and len(u) < 200 else ""


def _valid_ideas(card: dict) -> dict | None:
    tok = str(card.get("token") or "")
    rows = []
    for i in card.get("ideas") or []:
        if not isinstance(i, dict) or not str(i.get("title") or "").strip():
            continue
        ex = [{"full_name": _s(e.get("full_name"), 120), "url": _gh_url(e.get("url")),
               "stars": int(e.get("stars") or 0) if str(e.get("stars") or "0").isdigit() else 0,
               "description": _s(e.get("description"))}
              for e in i.get("examples") or [] if isinstance(e, dict) and _gh_url(e.get("url"))][:3]
        rows.append({"key": _s(i.get("key") or i.get("repo") or i.get("title"), 80),
                     "title": _s(i.get("title"), 80), "pitch": _s(i.get("pitch")),
                     "skills": [_s(k, 40) for k in (i.get("skills") or [])[:5]],
                     "gaps": [_s(k, 40) for k in (i.get("gaps") or [])[:5]],
                     "tech": _s(i.get("tech")), "repo": _s(i.get("repo"), 100), "examples": ex})
    if not rows:
        return None
    out = {"type": "ideas", "token": tok if _TOKEN.match(tok) else "", "role": _s(card.get("role"), 120),
           "ideas": rows[:6]}
    if card.get("used"):
        out["used"] = True
    if card.get("built"):
        out["built"] = _s(card["built"], 80)
    return out


def _valid_progress(card: dict) -> dict | None:
    pid = str(card.get("project_id") or "")
    if not re.match(r"^[A-Za-z0-9_-]{4,40}$", pid):
        return None
    return {"type": "project_progress", "project_id": pid, "title": _s(card.get("title"), 80),
            **({"added": True} if card.get("added") else {})}


def _valid_card(card) -> dict | None:
    if isinstance(card, dict) and card.get("type") == "repo_fit":
        return _valid_repo_fit(card)
    if isinstance(card, dict) and card.get("type") == "ideas":
        return _valid_ideas(card)
    if isinstance(card, dict) and card.get("type") == "project_progress":
        return _valid_progress(card)
    if not isinstance(card, dict) or card.get("type") != "buttons":
        return None
    buttons = []
    for b in card.get("buttons") or []:
        if not isinstance(b, dict) or not str(b.get("label") or "").strip():
            continue
        label = str(b["label"]).strip()[:40]
        if b.get("nav") in NAV_TARGETS:
            buttons.append({"label": label, "nav": b["nav"]})
        elif isinstance(b.get("send"), str) and b["send"].strip():
            buttons.append({"label": label, "send": b["send"].strip()[:200]})
    return {"type": "buttons", "buttons": buttons} if buttons else None


def merge_cards(cards: list[dict]) -> list[dict]:
    """At most one repo_fit card and one ideas card (the last of each a tool gave) and one
    buttons card per turn: the tools' buttons together, each label once."""
    seen, buttons, fit, ideas = set(), [], None, None
    for c in cards:
        v = _valid_card(c) or {}
        if v.get("type") == "repo_fit":
            fit = v
            continue
        if v.get("type") == "ideas":
            ideas = v
            continue
        for b in v.get("buttons", []):
            if b["label"].lower() not in seen:
                seen.add(b["label"].lower())
                buttons.append(b)
    if fit is not None:       # the card has its own Projects button; the list one repeats it
        buttons = [b for b in buttons if not b.get("send", "").lower().startswith("which of my github")]
    out = ([fit] if fit is not None else []) + ([ideas] if ideas is not None else [])
    return out + ([{"type": "buttons", "buttons": buttons[:MAX_BUTTONS]}] if buttons else [])


# ----------------------------------------------------------------------------- the loop

def _scrub(text: str, secrets: list) -> str:
    for s in secrets or []:
        if s and len(str(s)) >= 6:
            text = text.replace(str(s), "[hidden]")
    return text


def _conversation(ctx: AppContext, message: str, limit: int = 10) -> list[dict]:
    turns = [t for t in (ctx.history() or []) if isinstance(t, dict)
             and t.get("role") in ("user", "agent") and str(t.get("content") or "").strip()]
    if turns and turns[-1].get("role") == "user" and turns[-1].get("content") == message:
        turns = turns[:-1]
    out = [{"role": "assistant" if t["role"] == "agent" else "user", "content": str(t["content"])}
           for t in turns[-limit:]]
    out.append({"role": "user", "content": message})
    return out


def tools_for(ctx: AppContext) -> dict[str, Tool]:
    """The tools this context can really run: a tool whose app piece is not wired is not offered,
    so the chat never describes it."""
    out = dict(TOOLS)
    if ctx.suggest_ideas is None:
        out.pop("suggest_projects_for_jd", None)
    return out


def run_turn(session, ctx: AppContext, message: str, tools: dict[str, Tool] | None = None) -> dict:
    """One chat turn: {"text", "cards", "did", "tools"}. ``tools`` lists the tool names called,
    for logs and tests; it never carries their results."""
    tools = dict(tools_for(ctx) if tools is None else tools)
    role = ctx.role or str(getattr(session, "role", "") or "")
    company = ctx.company or str(getattr(session, "company", "") or "")
    system = system_prompt(tools, role, company)
    specs = [t.spec() for t in tools.values()]
    messages = _conversation(ctx, message)
    secrets = [s for s in (ctx.secrets() or []) if s]
    cards: list[dict] = []
    did: list = []
    called: list[str] = []
    text = ""
    for rnd in range(MAX_ROUNDS + 1):
        last = rnd == MAX_ROUNDS
        try:
            turn = ctx.llm.agent_turn(system + (LAST_ROUND if last else ""), messages, specs)
        except Exception:      # noqa: BLE001 - the chat answers politely, never a trace
            return {"text": POLITE_FAIL, "cards": merge_cards(cards), "did": did, "tools": called}
        calls = list((turn or {}).get("tool_calls") or [])
        text = str((turn or {}).get("text") or "")
        if not calls or last:
            break
        messages.append({"role": "assistant", "content": text, "tool_calls": calls})
        results = []
        for c in calls:
            name = str(c.get("name") or "")
            called.append(name)
            tool = tools.get(name)
            try:
                if tool is None:
                    raise KeyError(name)
                out = tool.run(ctx, dict(c.get("input") or {}))
                if tool.mutates:
                    did.append(name)
                card = out.pop("card", None) if isinstance(out, dict) else None
                if card:
                    cards.append(card)
                body, err = json.dumps(out, default=str)[:12000], False
            except Exception:  # noqa: BLE001 - a failed tool is one polite line to the model
                body, err = json.dumps({"error": TOOL_FAIL}), True
            results.append({"id": c.get("id"), "name": name, "content": _scrub(body, secrets),
                            "is_error": err})
        messages.append({"role": "tool", "results": results})
        text = ""
    text = _scrub(honest(_clean(text), did), secrets) if text.strip() else POLITE_FAIL
    return {"text": text, "cards": merge_cards(cards), "did": did, "tools": called}


# ----------------------------------------------------------------------------- routing

_AGENT_WORDS = re.compile(
    r"\b(github|repos?|repositor(?:y|ies)|portfolio|relevant|irrelevant|quiz|connect(?:ed|ion)?|"
    r"what can you do|what do you do|how can you help|what are you able)\b", re.I)
_PROJECTS = re.compile(r"\bprojects?\b", re.I)
_LISTED = re.compile(r"(^|\n)\s*(?:\d+[.)]|[-*•])\s+\S", re.M)
_URL = re.compile(r"https?://|www\.", re.I)


# A question: a question mark, or a sentence that opens like one.
_QUESTION_START = re.compile(
    r"^\s*(?:what|which|how|why|where|who|when|can|could|would|will|do|does|did|is|are|should|"
    r"have you|has|any)\b", re.I)
# An ask of the app: a verb aimed at GitHub, repositories or projects ("check my github",
# "build me a project", "which repos fit").
_ASK = re.compile(
    r"\b(?:check|look (?:at|through)|scan|review|go through|rank|show|list|find|pull|read|"
    r"open|connect|link|suggest|recommend|build me|help me build|make me|create|compare)\b"
    r"[^.?!\n]{0,40}\b(?:github|repos?|repositor(?:y|ies)|portfolio|projects?)\b", re.I)
# An answer: what a person says back to the intake's question ("no github, no projects",
# "I don't have projects", "skip", "yes").
_ANSWER_START = re.compile(
    r"^\s*(?:no|none|nope|nah|not|n/?a|skip|i (?:do not|don'?t|dont) have|i have(?:n'?t| no)|"
    r"there (?:are|is) no|yes|yeah|yep|sure|ok(?:ay)?|my)\b", re.I)


def is_question(message: str) -> bool:
    m = str(message or "")
    return "?" in m or bool(_QUESTION_START.match(m))


def wants_agent(message: str, pending: bool = False) -> bool:
    """A turn the agent should answer (cheap, no model): a question or a request about GitHub,
    repositories, portfolio, projects, relevance, the quiz, connecting, or "what can you do".

    A message that only answers (a decline such as "no github, no projects", "I don't have
    projects", a statement, an explicit list of projects, a pasted link) is material for the
    intake's capture, so it stays there. ``pending`` is True while a required intake answer is
    pending (before the first build): then only a clear question goes to the agent, never an
    imperative or a statement, so the funnel's question is always answered first."""
    m = str(message or "")
    if not m.strip() or _LISTED.search(m):
        return False
    if _URL.search(m) and not re.search(r"\?", m):
        return False
    if not (_AGENT_WORDS.search(m) or _PROJECTS.search(m)):
        return False
    if is_question(m):
        return True
    if pending:
        return False
    return bool(_ASK.search(m)) and not _ANSWER_START.match(m)
