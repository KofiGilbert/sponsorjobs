"""What the apply agent knows about the person, as one compact block for the brain
(docs/apply-agent.md section 29, "The agent decides").

The brain used to see no fact about the person at all, so every question a rule did not cover was
asked ("What is the highest level of education you have completed?" with the degree on the resume
the agent itself built). ``build`` puts the person's own material in front of it on every step that
has open questions, so it can answer from that material under the answer protocol
(autoapply/protocol.py), and code checks each answer against this same block.

The facts are autoapply/grounded.py's (``build_facts``: where they live, each job, each school,
their skills, their saved answers), which already leaves out every self-identification, work
authorization, legal, drafted and secret answer, plus three lines grounded.py had no use for:
years of experience, the links the person saved (as placeholder names, never the address) and the
languages on the profile. Contact details (name, email, phone, street) stay placeholders, and a
password or a code is never here.

The block is stable for one application (the same text on every step), so it sits in the cached
part of the prompt (llm ``agent_step_blocks``).
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

from autoapply import grounded as G

MAX_BLOCK_CHARS = 4200       # about 1,050 tokens: the cap, not the usual size
MAX_SKILLS = 30
MAX_LANGUAGES = 8
LINK_KEYS = ("linkedin", "github", "website")


@dataclass
class Material:
    """The person's material for one application: the block the model reads and what the checks
    read (local only)."""
    block: str = ""
    facts: list = field(default_factory=list)          # grounded.Fact, plus "years", "links", "languages"
    company: str = ""
    employers: list = field(default_factory=list)      # the organisations in the work history

    def __bool__(self) -> bool:
        return bool(self.block)


def _short(s, n: int = 90) -> str:
    return " ".join(str(s or "").split())[:n]


def years_of_experience(facts, today: _dt.date | None = None) -> tuple[int, int] | None:
    """(whole years since the first job started, that year) from the job facts, else None."""
    today = today or _dt.date.today()
    years = [min(f.data["years"]) for f in facts if f.kind == "job" and f.data.get("years")]
    if not years:
        return None
    first = min(years)
    return max(0, today.year - first), first


def languages(profile: dict) -> list:
    """The languages the saved profile lists (a list, a comma line, or {name, level} rows)."""
    raw = (profile or {}).get("languages") or []
    items = raw if isinstance(raw, list) else re.split(r"[,;\n]", str(raw))
    out = []
    for it in items:
        if isinstance(it, dict):
            name, level = _short(it.get("name") or it.get("language"), 30), _short(it.get("level") or it.get("proficiency"), 30)
            it = f"{name} ({level})" if name and level else name
        t = _short(it, 50)
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)
        if len(out) >= MAX_LANGUAGES:
            break
    return out


def _line(f) -> str:
    s = f.show
    if f.kind == "location":
        place = ", ".join(str(s[k]) for k in ("city", "state", "postal_code", "country") if s.get(k))
        return f"{f.id} (lives in): {place}"
    if f.kind == "job":
        when = " to ".join(x for x in (s.get("start", ""), s.get("end", "")) if x)
        cur = " (current job)" if f.data.get("current") else ""
        return f"{f.id}: {s.get('title', '')}, {s.get('organization', '')}" + (f", {when}" if when else "") + cur
    if f.kind == "school":
        deg, fld = s.get("degree", ""), s.get("field", "")
        what = ", ".join(x for x in (deg, "" if fld and fld.lower() in deg.lower() else fld) if x)
        when = " to ".join(x for x in (s.get("start", ""), s.get("end", "")) if x)
        done = " (in progress)" if f.data.get("current") else ""
        return f"{f.id}: " + ", ".join(x for x in (what, s.get("school", ""), when) if x) + done
    if f.kind == "skills":
        return "skills: " + ", ".join(str(x) for x in (s.get("skills") or [])[:MAX_SKILLS])
    if f.kind == "answer":
        return f'{f.id} (saved answer): "{_short(s.get("question"), 110)}" = "{_short(s.get("answer"), 140)}"'
    return f"{f.id}: " + _short(s.get("text"), 160)


def build(applicant: dict, profile: dict, library=None, *, company: str = "", questions=(), skip_keys=frozenset(),
          today: _dt.date | None = None) -> Material:
    """The person's material for the brain. ``applicant``: the fill map (city, state, links);
    ``profile``: this application's tailored resume profile, else the saved profile; ``library``:
    the answer library (its self-identification, legal and secret rows never get here);
    ``questions``: the page's open questions, used only when there are more saved answers than
    fit; ``skip_keys``: answer keys held or withheld for this job."""
    facts = G.build_facts(applicant or {}, profile or {}, library, questions=(), skip_keys=skip_keys)
    if questions and sum(1 for f in facts if f.kind == "answer") >= G.MAX_ANSWERS:
        # more saved answers than fit: the ones nearest the page's questions (the block then
        # changes with the page; with fewer it is the same text on every step)
        facts = G.build_facts(applicant or {}, profile or {}, library, questions=questions, skip_keys=skip_keys)
    # A saved answer to a legal or attestation question never shows here: it fills its own
    # question as a placeholder (hard limit 5), so the brain has no use for its words.
    from autoapply import qclass
    facts = [f for f in facts if f.kind != "answer" or not qclass.is_legal(f.data.get("question") or "", company=company)]
    for n, f in enumerate([f for f in facts if f.kind == "answer"], start=1):
        f.id = f.show["id"] = f"ans{n}"
    yrs = years_of_experience(facts, today)
    if yrs:
        text = f"about {yrs[0]} years since the first job listed ({yrs[1]})"
        facts.append(G.Fact("years", "years", {"id": "years", "text": text}, {"years": yrs[0], "first": yrs[1]}))
    links = [k for k in LINK_KEYS if str((applicant or {}).get(k) or "").strip()]
    if links:
        text = "saved: " + ", ".join("{{" + k + "}}" for k in links) + " (type the placeholder)"
        facts.append(G.Fact("links", "links", {"id": "links", "text": text}, {"links": links}))
    langs = languages(profile or {})
    if langs:
        facts.append(G.Fact("languages", "languages", {"id": "languages", "text": ", ".join(langs)}, {"languages": langs}))
    if not facts:
        return Material(company=str(company or ""))
    order = {"location": 0, "job": 1, "school": 2, "years": 3, "skills": 4, "languages": 5, "links": 6, "answer": 7}
    facts.sort(key=lambda f: order.get(f.kind, 9))             # stable: jobs and schools keep their order
    lines, size = [], 0
    for f in facts:
        ln = _line(f)
        if size + len(ln) > MAX_BLOCK_CHARS:
            break
        lines.append(ln)
        size += len(ln) + 1
    employers = [str(f.data.get("org") or "") for f in facts if f.kind == "job" and f.data.get("org")]
    if not any(f.kind == "job" for f in facts):
        lines.append("work history: none saved")
    block = "<person>\n" + "\n".join(lines) + "\n</person>"
    return Material(block=block, facts=facts, company=str(company or ""), employers=employers)


def tokens(text: str) -> int:
    """About how many tokens a text is (four characters a token, the estimate the budget uses)."""
    return max(0, (len(str(text or "")) + 3) // 4)
