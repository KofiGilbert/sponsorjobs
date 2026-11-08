"""The resume chat agent ("one brain", first piece): the resume chat answers with tools, so what
it says about the person's resume, their upload and their GitHub comes from what the app really
holds, read in that turn. Behind the flag TAILOR_CHAT_AGENT (off by default; off means the chat
behaves exactly as before).

One turn (``run_turn``) is at most MAX_ROUNDS rounds of tool calls, then the answer. Tools only
READ in this piece: the resume being built, the last upload, the GitHub connection and the
person's public repositories. Nothing here changes the resume, so ``did`` is always empty and
``honest`` strips any sentence that claims a save or an addition. Mutating tools come later,
behind a single-use action token the person's own click spends.

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
        "the button you were given.",
        "- If a tool says something failed or was rate limited, say so in one plain line.",
        "- Be short and warm: a few sentences, or a short list (one item per line) when listing "
        "things. Plain text, no markdown headings or tables. Use plain words, no dashes as "
        "punctuation, and say resume, never CV.",
        "- Never describe a capability that is not in the list above.",
    ]
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

def _valid_card(card) -> dict | None:
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
    """One buttons card per turn: the tools' buttons together, each label once."""
    seen, buttons = set(), []
    for c in cards:
        for b in (_valid_card(c) or {}).get("buttons", []):
            if b["label"].lower() not in seen:
                seen.add(b["label"].lower())
                buttons.append(b)
    return [{"type": "buttons", "buttons": buttons[:MAX_BUTTONS]}] if buttons else []


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


def run_turn(session, ctx: AppContext, message: str, tools: dict[str, Tool] | None = None) -> dict:
    """One chat turn: {"text", "cards", "did", "tools"}. ``tools`` lists the tool names called,
    for logs and tests; it never carries their results."""
    tools = dict(TOOLS if tools is None else tools)
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


def wants_agent(message: str) -> bool:
    """A turn the agent should answer (cheap, no model): GitHub, repositories, portfolio,
    projects talked about without an explicit list, relevance, the quiz, connecting, and "what
    can you do". An explicit list of projects or a pasted link is material for the existing
    capture, so it stays there."""
    m = str(message or "")
    if not m.strip() or _LISTED.search(m):
        return False
    if _URL.search(m) and not re.search(r"\?", m):
        return False
    return bool(_AGENT_WORDS.search(m) or _PROJECTS.search(m))
