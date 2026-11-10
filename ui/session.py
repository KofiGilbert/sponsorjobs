"""Intelligent, LLM-driven intake session over the existing engine (CLAUDE.md §4).

The conversation is run by the model, not a script: the person talks naturally
(several degrees at once, a link on the wrong line, casual phrasing) and the
agent extracts what it needs and asks smart follow-ups only for real gaps. When
it has enough, it drafts a JD-matched, one-page CV with the existing assembler.

Persistent memory: on return, the person's PROFILE, extracted ESSENTIALS, and
CONVERSATION are loaded from local storage, so nothing is re-asked, the agent
already knows the background and asks only about what's new or changed.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import threading
from pathlib import Path

from intake.diary import Diary
from ui import activity as _activity
from tailoring.assembler import assemble_cv, normalize_profile
from tailoring.assembler import ranked_roles as _ranked
from tailoring.conform import date_lacks_month, is_full_address


# Boilerplate a job-board paste sweeps in, "Company logo", "Apply now", a URL, etc.
# A line (or field) containing any of these isn't a real role/company title.
_JD_JUNK = ("logo", "posted", "apply now", "easy apply", "save job", "http", "www.",
            "sign in", "share this", "report this")


# A line that is a posting's metadata, not its title: "OPEN DATE: November 9, 2026", "SCHEDULE:
# Full Time", "*DUTY LOCATION: Washington, DC" (the Smithsonian D365 posting, 2026-11-10, whose
# header read "OPEN DATE: November 9"). A label before a colon, a date, or a bare marker.
_JD_META = re.compile(
    r"^[*#>\s]*(?:[A-Za-z][A-Za-z /&()'-]{1,40}:\s|\S+\s*:\s*$)"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b|^[\W_]*$", re.I)
_JD_LABEL_WORDS = re.compile(
    r"^(?:open|closing|close|posted|post|start|end|position|appointment|schedule|duty|job|"
    r"req(?:uisition)?|salary|pay|grade|series|location|department|division|type|category|"
    r"reports? to|apply by|deadline|date)\b", re.I)
# "The D365 Software Developer plays a key role...", "As a Data Analyst, you will...", "We are
# hiring a Senior Analyst": the title a posting's prose names when its first lines are metadata.
_JD_TITLE_IN_PROSE = re.compile(
    r"(?:\bThe|\bAs (?:a|an|the)|\b(?:hiring|seeking|looking for) (?:a|an))\s+"
    r"((?:[A-Z0-9][\w+#&/.-]*\s+){0,5}?[A-Z][\w+#&/.-]*)"
    r"(?=,?\s+(?:plays|will|is (?:responsible|a|an)|role|position|works|leads|supports|"
    r"you will|to join|who)\b)")
_TITLE_WORDS = re.compile(
    r"\b(?:developer|engineer|analyst|manager|scientist|architect|designer|consultant|"
    r"specialist|director|lead|officer|administrator|associate|coordinator|technician|"
    r"intern|accountant|advisor|adviser|programmer|researcher|strategist|head|vp|president)\b",
    re.I)


def _voice_logger(row: dict) -> None:
    """One line per chat turn's message: whether the AI wrote it, why not, and its tokens. To the
    log, and to TAILOR_VOICE_LOG (a file) when set, for measuring what one voice costs."""
    import logging
    logging.getLogger("tailor.voice").info("voice %s", json.dumps(row, default=str))
    path = os.environ.get("TAILOR_VOICE_LOG")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError:
            pass


def _jd_label(jd_text: str) -> tuple[str, str]:
    """Best-effort (role, company) from the top of a JD. Skips scraped boilerplate
    ('Company logo for …') and the posting's metadata lines ('OPEN DATE: November 9, 2026')
    so the greeting never reads 'the Company logo for role' or 'the OPEN DATE: November 9
    role'; when the first lines are all metadata, the title the prose names ('The D365
    Software Developer plays a key role') is used. '' for either when nothing clean is found.
    A session started from a job record uses the record's title instead (ui/app.py)."""
    def _clean(seg: str, limit: int) -> str:
        seg = re.split(r"[.,;]", seg or "")[0].strip(" *#\t")[:limit]
        return "" if (not seg or any(j in seg.lower() for j in _JD_JUNK)) else seg
    lines = [ln.strip() for ln in (jd_text or "").splitlines() if ln.strip()]
    for ln in lines[:8]:
        named = re.match(r"^[*#\s]*(?:job |position |role )?(?:title|position|role)\s*:\s*(.+)$", ln, re.I)
        if named and _clean(named.group(1), 60):
            return _clean(named.group(1), 60), ""
        if _JD_META.search(ln) or _JD_LABEL_WORDS.match(ln.lstrip("*# ")) or len(ln) > 120:
            continue
        parts = re.split(r"\s+[, \-, |·]\s+|\s+at\s+", ln, maxsplit=1)
        role = _clean(parts[0], 60)
        if not role:
            continue
        company = ""
        if len(parts) > 1:
            company = _clean(re.split(r"\s+in\s+", parts[1])[0], 40)
        return role, company
    for m in _JD_TITLE_IN_PROSE.finditer(" ".join(lines[:60])):
        title = m.group(1).strip()
        if _TITLE_WORDS.search(title):
            return title[:60], ""
    return "", ""


def _essentials_from(saved_ess: dict, profile: dict) -> dict:
    """Reconstruct essentials, preferring stored essentials but falling back to
    the full saved profile, so a returning user is recognized even if only the
    profile was captured richly."""
    ident = dict(saved_ess.get("identity") or profile.get("identity") or {})
    edu = [dict(x) for x in (saved_ess.get("education") or [])]
    if not edu:
        for e in profile.get("education", []):
            edu.append({"school": e.get("school", ""), "degree": e.get("degree", ""),
                        "date": e.get("date", ""), "location": e.get("location", ""),
                        "courses": e.get("courses", "")})
    exp = [dict(x) for x in (saved_ess.get("experience") or [])]
    pexp = [{"org": e.get("org", ""), "title": r.get("title", ""), "dates": r.get("dates", ""),
             "location": e.get("location", ""), "bullets": r.get("bullets", [])}
            for e in profile.get("experience", []) for r in (e.get("roles") or [e])]
    if not exp:
        exp = [dict(x) for x in pexp]
    else:
        # Stored essentials can predate a field (e.g. locations added later), fill
        # any gaps from the richer saved profile so the gate doesn't re-ask.
        _backfill(exp, pexp, "org", ("title", "dates", "location"))
    _backfill(edu, list(profile.get("education", [])), "school",
              ("degree", "date", "location", "courses"))
    # Carry the gate state (what was declined, any projects) so a returning user
    # isn't re-asked for things they already resolved. Extracurricular and interests
    # must ride along too: draft() rebuilds the profile's copies FROM essentials, so
    # anything missing here is silently deleted from the CV of a returning user and
    # re-asked every session (the fills-the-page sections were the ones lost).
    return {"identity": ident, "education": edu, "experience": exp,
            "projects": list(saved_ess.get("projects") or profile.get("projects") or []),
            "extracurricular": [dict(x) for x in (saved_ess.get("extracurricular")
                                                  or profile.get("extracurricular") or [])
                                if isinstance(x, dict)],
            "interests": str(saved_ess.get("interests")
                             or profile.get("interests") or "").strip(),
            # The person's own stated skills/certs are DATA, not gate state: without
            # them _validate_skills has nothing to validate stated skills against on a
            # reloaded session, and the whole skills block starves to 1-2 terms.
            "skills_input": [str(s) for s in (saved_ess.get("skills_input") or [])],
            "certifications": [str(c) for c in (saved_ess.get("certifications") or [])],
            # JD skills already asked about (the full-width line rule, tailoring/linefit.py)
            # and the ones the person said they don't have: never asked twice.
            "skills_asked": [str(s) for s in (saved_ess.get("skills_asked") or [])],
            "skills_declined": [str(s) for s in (saved_ess.get("skills_declined") or [])],
            # Skills the person added with ADD on a job's keywords (their provenance).
            "skills_from_jobs": [str(s) for s in (saved_ess.get("skills_from_jobs") or [])],
            "declined": list(saved_ess.get("declined") or [])}


# A one-line bullet whose line stops before this much of the width is thin: worth asking about.
THIN_BULLET = 0.6


def _bullet_owner(label) -> str:
    """The project or employer a bullet sits under, read cleanly from its label ("Last-Mile
    Operations Simulator (Python, simulation), Analyst" -> "Last-Mile Operations Simulator"):
    any bracketed tools go first, then the role title after the comma."""
    t = re.sub(r"\s*[(\[][^)\]]*[)\]]?", "", str(label or ""))
    t = t.split(",")[0].strip(" -:")
    return t if t and t.lower() != "entry" else ""


# Roughly how many real skill terms fill 4 to 5 full-width Skills lines (tailoring/linefit.py).
# Below this the enrichment interview asks for more tools and skills per area.
SKILLS_FOR_FULL_LINES = 24


# Phrases that mean "just use my saved profile", handled deterministically so a
# request to build from memory never falls through to a parse error.
_SAVED_BUILD_KEYS = (
    "what you already know", "what you know about me", "use my saved",
    "my saved profile", "from my saved", "use my profile", "use my info",
    "use my details", "use my background", "use my resume", "use my cv",
    "you already have my", "you have my info", "you have my profile",
    "everything you know", "everything you have", "use what you have",
    "use what you know", "build from my", "build it from my", "just build it",
    "go ahead and build", "with what you have", "with what you know",
)


def _wants_saved_build(msg: str) -> bool:
    m = " " + (msg or "").lower() + " "
    return any(k in m for k in _SAVED_BUILD_KEYS)


# Explicit opt-out of the completeness gate: build now with whatever's available.
# Kept to unambiguous opt-out phrases so normal intake prose can't trip it.
_OVERRIDE_KEYS = _SAVED_BUILD_KEYS + (
    "build with what", "build it with what", "continue with what",
    "go ahead and build", "go ahead and make", "build it now", "build it anyway",
    "build anyway", "proceed with what", "proceed and build", "skip the questions",
    "stop asking", "no more questions",
    "continue with the cv", "build the cv now", "make the cv now", "that's all i have",
    "thats all i have", "just make it now",
    # NOTE: bare "continue building" / "skip the rest" / "don't ask" were REMOVED, they
    # collide with ordinary intake prose ("continue building pipelines", "users don't ask"),
    # which flipped a normal answer into a force-build. Keep only build-anchored opt-outs.
)


def _wants_override(msg: str) -> bool:
    m = " " + (msg or "").lower() + " "
    return any(k in m for k in _OVERRIDE_KEYS)


# Explicit "I don't have the real value, fill a placeholder and move on". Distinct,
# specific phrases so ordinary intake prose ("I built a dummy dashboard") can't trip it.
_PLACEHOLDER_KEYS = (
    "dummy data", "dummy date", "some dummy", "put dummy", "use dummy", "a placeholder",
    "use placeholder", "placeholder date", "make it up", "make one up", "made it up",
    "you decide", "just put something", "put something there", "fill it in for me",
    "guess the date", "fake date",
    # bare "made up" was REMOVED, it collides with prose ("the team was made up of ...");
    # the directive forms ("make it up" / "made it up") capture the real intent.
)


def _wants_placeholder(msg: str) -> bool:
    m = " " + (msg or "").lower() + " "
    return any(k in m for k in _PLACEHOLDER_KEYS)


# ---- Deterministic section router (control plane) -----------------------------------
# The template's structure is a FIXED schema; where an entry goes is a placement policy
# the person can state explicitly, not a judgement to leave to the LLM. These helpers
# move entries between the movable sections and keep them where the person put them.
_MOVABLE_SECTIONS = ("experience", "extracurricular", "projects")

# Which section a directive names (checked in this order; 'extracurricular' before
# 'experience' so "extracurricular ... experience" doesn't mis-route).
_SECTION_KEYWORDS = (
    ("extracurricular", ("extracurricular", "extra curricular", "extra-curricular",
                         "extracurriculars", "extra-curriculars", "co-curricular")),
    ("projects", ("project",)),
    ("experience", ("work experience", "professional experience", "work history",
                    "employment", "experience", "a real job", "a paid job", "a job",
                    "day job")),
)
# The message must read like a PLACEMENT directive, not just prose mentioning a section.
_MOVE_INTENT = re.compile(
    r"(?i)\b(is\s+an?|was\s+an?|should\s+(?:be|go)|belongs?|goes?\s+under|move[ds]?|"
    r"put|list(?:ed)?|categori[sz]e[ds]?|classif(?:y|ied)|not\s+an?|that'?s\s+an?|"
    r"under\s+the|reclassif|counts?\s+as|treat\s+.*\bas\b)\b")


def _entry_name(e: dict) -> str:
    return str((e or {}).get("org") or (e or {}).get("title") or "").strip()


def _entry_names_match(name: str, low_msg: str) -> bool:
    """Does the person's message refer to this entry? Whole-name substring, else a
    majority of its distinctive words (drop generic filler)."""
    n = name.lower().strip()
    if not n:
        return False
    if n in low_msg:
        return True
    words = [w for w in re.findall(r"[a-z0-9]{3,}", n)
             if w not in ("the", "and", "for", "inc", "ltd", "llc", "group", "team",
                          "project", "volunteer", "volunteering", "company", "co")]
    return bool(words) and sum(w in low_msg for w in words) >= max(1, (len(words) + 1) // 2)


def _to_section_shape(e: dict, target: str) -> dict:
    """Reshape a moved entry into the target section's fields (never invents data)."""
    name = _entry_name(e)
    bullets = list(e.get("bullets") or [])
    dates = str(e.get("dates") or e.get("date") or "").strip()
    loc = str(e.get("location") or "").strip()
    title = str(e.get("title") or e.get("role") or "").strip()
    if target == "extracurricular":
        return {"title": name, "date": dates, "bullets": bullets}
    if target == "projects":
        return {"org": name, "title": title, "location": loc or "Personal Project",
                "dates": dates, "bullets": bullets, "suggested": False}
    return {"org": name, "title": title, "dates": dates, "location": loc, "bullets": bullets}


# An entry whose NAME clearly marks it as volunteering defaults to Extracurricular
# (overridable), the one heuristic the research supports, keyed on explicit markers so
# it never touches a formal job.
_VOLUNTEER_SIGNAL = re.compile(
    r"(?i)\(?\bvolunteer(?:ing|ed)?\b\)?|\bvaccination drive\b|\bcharity\b|\bfundrais|"
    r"\bcommunity\s+service\b|\bnon[- ]?profit\s+volunteer\b")


_DONE_KEYS = (
    "that's everything", "thats everything", "that's all", "thats all", "that's it",
    "thats it", "nothing else", "nothing more", "i'm done", "im done",
    "looks good", "that's good", "thats good", "finalize", "finalise",
    "good to go", "ready to go", "that's fine", "thats fine", "no thanks", "no more to add",
    # bare "no more" / "all done" were REMOVED, they collide with prose ("no more budget",
    # "the migration was all done by Q3"); "no more to add" / "i'm done" capture the intent.
)


def _wants_done(msg: str) -> bool:
    """The person is finished adding real material, finalize, don't keep asking."""
    m = " " + (msg or "").lower() + " "
    return any(k in m for k in _DONE_KEYS)


_PROJECT_IDEA_KEYS = (
    "suggest project", "suggest a project", "suggest some", "suggest a few", "suggest me",
    "suggest projects", "project idea", "ideas for a project", "what project should i build",
    "help me build", "recommend a project", "recommend project", "sample project",
    "projects i could build", "projects to build", "what should i build",
    "what could i build", "come up with a project", "give me a project", "help me come up",
    # bare "i could build" / "which project" / "what project" were REMOVED/tightened, they
    # collide with a skill statement ("skills i could build on") or a placement question
    # ("which project should I list first"), which mis-routed to sample-project generation.
)
_REMOVE_SUGGESTED_KEYS = (
    "remove the suggested", "remove suggested", "delete the suggested", "delete suggested",
    "remove the placeholder", "remove placeholder", "get rid of the suggested",
    "drop the suggested", "remove the sample project", "remove the sample",
)


def _wants_project_ideas(msg: str) -> bool:
    m = " " + (msg or "").lower() + " "
    return any(k in m for k in _PROJECT_IDEA_KEYS)


def _wants_remove_suggested(msg: str) -> bool:
    m = " " + (msg or "").lower() + " "
    return any(k in m for k in _REMOVE_SUGGESTED_KEYS)


# --- Every element the template contains (CLAUDE.md §8). The tool asks for each
# and only omits it if the user explicitly declines; it never invents to fill a
# gap. Only bullet descriptions of real roles/projects are safe to draft. -------
def _year(s: str) -> int:
    m = re.search(r"(19|20)\d\d", str(s or ""))
    return int(m.group(0)) if m else -1


def _most_recent(education: list) -> dict:
    return max(education, key=lambda d: _year(d.get("date")), default={}) if education else {}


def _missing_slots(identity: dict, education: list, experience: list,
                   projects: list | None = None, declined=None, identity_fields=None) -> list[dict]:
    """Every template element still missing (or not yet declined), as a slot an editor can
    fill: ``{q, crit, soft, sec, key, match, decline}``. ``q`` is the question's wording (what
    _missing_skeleton returns), ``crit``/``soft`` its tier, ``sec``/``key`` where the answer
    lands, ``match`` the saved entry's own names (org and title, school and degree, a
    project's name) so the save finds it again, and ``decline`` the key a Skip records in the
    declined list (None where the template needs the value).

    Three tiers (owner decision 2026-10-26). HARD (``crit``, blocks a build and every
    unattended run): name, a contact, the org/title/dates/location of every experience entry,
    and each education entry's school/degree/date. SOFT (``soft``, never blocks anything, so
    _missing_skeleton leaves it out): a project's dates and location, asked only in the
    details step and only where the template renders projects, with Skip remembered per
    project. OPTIONAL (only in the full gate): address, links, courses, and the "do you have
    any …" prompts.
    """
    projects = projects or []
    dec = set(declined or [])
    crit: list[dict] = []    # template-critical (HARD), asked first
    soft: list[dict] = []    # never blocks: a project's dates and location
    opt: list[dict] = []     # optional, full gate only, asked last

    def slot(q, sec, key, match=None, decline=None):
        return {"q": q, "sec": sec, "key": key, "match": dict(match or {}), "decline": decline}

    if not str(identity.get("name") or "").strip():
        crit.append(slot("your full name", "identity", "name"))
    if not str(identity.get("email") or identity.get("phone") or "").strip():
        crit.append(slot("an email or phone number", "identity", "contact"))

    for j in experience:   # every EXISTING role needs its full skeleton (critical)
        org, title = str(j.get("org") or "").strip(), str(j.get("title") or "").strip()
        where = f"your role at {org}" if org else (f"your {title} role" if title else "a role you listed")
        m = {"org": org, "title": title}
        if not org:
            crit.append(slot("the company for " + (f"your {title} role" if title else "a role you listed"),
                             "experience", "org", m))
        if not title:
            crit.append(slot(f"your job title at {org or 'that company'}", "experience", "title", m))
        if not str(j.get("dates") or "").strip():
            crit.append(slot(f"the employment dates for {where}", "experience", "dates", m))
        if not str(j.get("location") or "").strip():
            crit.append(slot(f"the location (city, state) for {where}", "experience", "location", m))

    for d in education:    # every EXISTING degree needs school/degree/date (critical)
        sch, deg = str(d.get("school") or "").strip(), str(d.get("degree") or "").strip()
        whose = f"your {deg}" if deg else (f"your degree at {sch}" if sch else "your degree")
        m = {"school": sch, "degree": deg}
        if not sch:
            crit.append(slot("the school for " + (f"your {deg}" if deg else "your degree"),
                             "education", "school", m))
        if not deg:
            crit.append(slot(f"the degree you earned at {sch or 'your school'}", "education", "degree", m))
        if not str(d.get("date") or "").strip():
            crit.append(slot(f"the graduation month and year for {whose}", "education", "date", m))

    if "projects" not in dec:
        for p in projects:   # a project's location+dates are SOFT: asked, never required
            # A personal / portfolio project (has a repo link or is flagged personal) is
            # not employment, it legitimately has no company location or work dates, so
            # we never ask (a linked repo's dates come from GitHub instead, ui/github_dates.py).
            if p.get("link") or p.get("url") or p.get("personal"):
                continue
            nm = str(p.get("title") or p.get("org") or "").strip()
            label = f'your project "{nm}"' if nm else "your project"
            m = {"name": nm}
            for key, q in (("location", f"the location for {label}"), ("dates", f"the dates for {label}")):
                skip = f"project_{key}:{nm.lower()}"     # a Skip, remembered per project
                if not str(p.get(key) or "").strip() and skip not in dec:
                    soft.append(slot(q, "projects", key, m, skip))

    # Optional / structural prompts (full gate only, after the critical fields).
    if not experience:
        opt.append(slot("at least one company and job title", "experience", "org"))
    if not education:
        opt.append(slot("your school and degree", "education", "school"))
    # Only ask for the identity slots THIS template actually shows (manifest identity_fields),
    # so a template without a blog slot never asks for a blog, etc.
    id_fields = set(identity_fields or ("name", "address", "phone", "email",
                                        "linkedin", "github", "blog"))
    # The template header shows a FULL mailing address, so a bare "City, ST" doesn't
    # satisfy the slot, ask for the whole thing (street, city, state, ZIP).
    if "address" in id_fields and "address" not in dec and not is_full_address(identity.get("address")):
        opt.append(slot("your full mailing address, street, city, state, and ZIP (or tell "
                        "me you'd rather leave it off)", "identity", "address", decline="address"))
    if "github" in id_fields and "github" not in dec and not str(identity.get("github") or "").strip():
        opt.append(slot("your GitHub link (a GitHub with real projects makes a much stronger "
                        "resume, or tell me you don't have one)", "identity", "github", decline="github"))
    if "linkedin" in id_fields and "linkedin" not in dec and not str(identity.get("linkedin") or "").strip():
        opt.append(slot("your LinkedIn URL (or tell me you don't have one)", "identity", "linkedin",
                        decline="linkedin"))
    if "blog" in id_fields and "blog" not in dec and not str(identity.get("blog") or "").strip():
        opt.append(slot("a blog/portfolio link (or tell me you don't have one)", "identity", "blog",
                        decline="blog"))
    recent = _most_recent(education)
    if education and "courses" not in dec and not str(recent.get("courses") or "").strip():
        opt.append(slot("the courses for your most recent degree (list them, or upload your "
                        "transcript, or tell me you'd rather skip the courses line)", "education",
                        "courses", {"school": str(recent.get("school") or "").strip()}, "courses"))
    if "projects" not in dec and not projects:
        opt.append(slot("any projects you'd like to include (or tell me you don't have projects "
                        "to add)", "projects", "org", decline="projects"))

    for x in crit:
        x["crit"], x["soft"] = True, False
    for x in soft:
        x["crit"], x["soft"] = False, True
    for x in opt:
        x["crit"], x["soft"] = False, False
    seen, out = set(), []
    for x in crit + soft + opt:      # de-dupe on the wording, keep order
        if x["q"] not in seen:
            seen.add(x["q"])
            out.append(x)
    return out


def _missing_skeleton(identity: dict, education: list, experience: list,
                      projects: list | None = None, declined=None,
                      critical_only: bool = False, identity_fields=None) -> list[str]:
    """Human-readable list of template elements still missing (or not yet declined): the
    questions of _missing_slots, without the SOFT tier (a project's dates and location never
    block a build). ``critical_only=True`` (an explicit build override, and the unattended
    auto-apply pre-flight) keeps the HARD tier only."""
    return [s["q"] for s in _missing_slots(identity, education, experience, projects,
                                           declined, identity_fields)
            if not s["soft"] and (s["crit"] or not critical_only)]


_DECLINE_TARGETS = {
    "github": ("github", "git hub"), "blog": ("blog", "portfolio", "personal website", "website"),
    "linkedin": ("linkedin",), "projects": ("project", "projects"),
    "courses": ("course", "courses", "transcript"), "address": ("address",),
    "extracurricular": ("extracurricular", "extracurriculars", "extra curricular",
                        "volunteering", "activities"),
    "interests": ("interest", "interests", "hobby", "hobbies"),
}
_DECLINE_VERB = (r"(?:no\b|not\b|n['’]?t\b|do ?n['’]?t|dont|without|skip|omit|leave out|"
                 r"leave\b[\w\s]{0,12}?\boff\b|\boff\b|rather not|prefer not|"
                 r"can['’]?t provide|cannot provide|don['’]?t have|"
                 r"haven['’]?t got|have none|none\b)")


# A bare "none" with no section named. It only means something in context: while the
# required-sections question (Extracurricular / Interests) is open, it declines exactly
# those open sections, the same as the "None, leave it off" button. Kept to whole-message
# matches so an answer that merely contains "no" is never read as a decline.
_BARE_DECLINE = re.compile(
    r"^\s*(?:none|no|nope|nothing|n/?a|"
    r"(?:i\s+)?(?:have|got)\s+none|"
    r"(?:i\s+)?(?:do\s*n['’]?t|dont)\s+have\s+(?:any|one|those|them)(?:\s+of\s+(?:those|them))?|"
    r"nothing\s+to\s+add|none\s+of\s+(?:those|them)|"
    r"(?:none,?\s+)?leave\s+(?:it|them|both)\s+off|skip(?:\s+(?:it|them|both))?|"
    r"(?:none|no),?\s+(?:thanks|thank\s+you))\s*[.!]*\s*$", re.I)


def _bare_decline(message: str) -> bool:
    return bool(_BARE_DECLINE.match(message or ""))


def _note_declines(message: str, declined: set) -> set:
    """Detect 'I don't have a GitHub / no blog / skip courses / no projects' and
    record the declined element so the gate stops asking for it."""
    m = " " + (message or "").lower() + " "
    for key, words in _DECLINE_TARGETS.items():
        for w in words:
            if w not in m:
                continue
            near = rf"(?:{_DECLINE_VERB}[\w\s'’/,-]{{0,24}}{re.escape(w)}|{re.escape(w)}[\w\s'’/,-]{{0,16}}{_DECLINE_VERB})"
            if re.search(near, m):
                declined.add(key)
                break
    return declined


# Require a real list connector (a verb like 'were/are/took/include', or a colon/
# dash) between 'courses' and the list, so a decline like 'leave the courses off'
# is never scraped as course content. The verb may be FOLLOWED by a colon/dash:
# "my courses were: A, B" is the most natural phrasing of all and used to fail
# both branches (Amazon case study obs #17, three model calls burned on it).
_COURSE_CUE = re.compile(
    r"(?:relevant\s+)?(?:courses?|classes|coursework|subjects)\b\s*"
    r"(?:(?:i\s+took|were|was|are|include[ds]?|taken)\s*[:\-]?\s*|[:\-]\s*)([^.\n]+)", re.I)


def _capture_courses(message: str) -> str:
    """Pull an inline course list ('my courses were A, B, C') into a courses string.
    Returns '' if the message isn't listing courses."""
    msg = message or ""
    if not re.search(r"\b(course|class|coursework|subject)", msg, re.I):
        return ""
    m = _COURSE_CUE.search(msg)
    if not m:
        return ""
    # Drop anything that's actually a decline ("skip / I don't have / none / no …")
    # so it never lands on the CV as a course.
    _decline = re.compile(r"\b(?:skip|no|none|n['’]?t|not|without|omit|rather not|"
                          r"don['’]?t|do ?n['’]?t|haven['’]?t|have none)\b", re.I)
    items = [c.strip(" .") for c in re.split(r",|;|\band\b", m.group(1))
             if len(c.strip(" .")) > 2 and not _decline.search(c)]
    return ", ".join(dict.fromkeys(items[:8]))


def _bare_course_list(message: str) -> str:
    """A bare 'A, B, C' reply accepted ONLY when the courses question was just asked,
    the question supplies the context the cue word normally would (obs #17: answering
    'what courses did you take?' with the list itself looped forever). Conservative:
    several short comma-separated items, no URLs, nothing sentence-like."""
    msg = (message or "").strip()
    if not msg or "http" in msg.lower():
        return ""
    items = [c.strip(" .") for c in re.split(r"[,;]", msg) if c.strip(" .")]
    if not (2 <= len(items) <= 10):
        return ""
    for it in items:
        if not (1 <= len(it.split()) <= 6) or len(it) > 48:
            return ""
        if re.search(r"\b(i|we|my|you|no|not|skip|don'?t|have|was|were)\b", it, re.I):
            return ""
    return ", ".join(dict.fromkeys(items[:8]))


# "Confirm back what you captured" is a READ request, never an answer to the pending
# question. It once got consumed by the intake queue and shredded into phantom project
# records (Amazon case study obs #11/#12); now it's answered from state, deterministically.
_CAPTURE_SUMMARY_RE = re.compile(
    r"confirm (?:back )?(?:to me )?what you (?:have|captured|got)"
    r"|what (?:do|did) you (?:have|capture|get)\b"
    r"|read (?:it |that )?back|list back|confirm back", re.I)


def _wants_capture_summary(message: str) -> bool:
    return bool(_CAPTURE_SUMMARY_RE.search(message or ""))


def _dedup_entries(entries: list, keys: tuple) -> list:
    """Keep first occurrence of each entry, matched case-insensitively on ``keys``."""
    seen, out = set(), []
    for e in entries:
        sig = tuple(str(e.get(k) or "").strip().lower() for k in keys)
        if sig not in seen:
            seen.add(sig)
            out.append(e)
    return out


_LINK_NAMES = {"linkedin": "LinkedIn", "github": "GitHub", "blog": "website"}


def _and_list(items: list) -> str:
    items = [str(x) for x in items if str(x).strip()]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _upload_project(p: dict) -> dict:
    """One project read from an uploaded resume, in the essentials' project shape. Its name is
    the heading (``org``); the arrow a PDF shows after the name is dropped from it."""
    from intake.links import _clean_title
    name = _clean_title(p.get("org") or p.get("name") or p.get("title") or "")
    link = str(p.get("link") or p.get("url") or "").strip()
    from intake.links import link_ok
    return {"org": name, "title": "", "location": str(p.get("location") or "").strip(),
            "dates": str(p.get("dates") or p.get("date") or "").strip(),
            "link": link if link_ok("link", link) else "",
            "tools": str(p.get("tools") or "").strip(),
            "bullets": [str(b).strip() for b in (p.get("bullets") or []) if str(b).strip()],
            "suggested": False}


def _entry_match(old: list, e: dict, keys: tuple):
    """The saved entry that is the same as ``e`` (fuzzy on each key, so "Stanbic Bank" is
    "Stanbic Bank Ghana" and a re-spaced title still matches), or None. At least one key
    must agree and none may disagree; a key blank on either side neither agrees nor
    disagrees (so a degree read without its school still finds its saved school)."""
    from intake.links import same_title
    for o in old or []:
        if not isinstance(o, dict):
            continue
        agree = disagree = 0
        for k in keys:
            a, b = str(o.get(k) or "").strip(), str(e.get(k) or "").strip()
            if a and b:
                if same_title(a, b):
                    agree += 1
                else:
                    disagree += 1
        if agree and not disagree:
            return o
    return None


def _merge_upload(saved: dict, up: dict) -> dict:
    """Fold a freshly uploaded resume (``up``) over the session's essentials (``saved``): the
    upload is the newer truth for this application. Its contact details win where it has
    them; its roles, schools and projects are this application's set, each gap-filled from the
    same saved entry (a bullet, a location, transcript courses the upload leaves blank); a
    section the upload has nothing for keeps the saved one. Everything else the session holds
    (skills, declines, interests, extracurricular) is kept."""
    out = {k: v for k, v in (saved or {}).items()}
    ident = dict((saved or {}).get("identity") or {})
    for k, v in (up.get("identity") or {}).items():
        if str(v or "").strip():
            ident[k] = v
    out["identity"] = ident
    for section, keys in (("experience", ("org", "title")), ("education", ("school", "degree")),
                          ("projects", ("org",))):
        new = [dict(e) for e in (up.get(section) or []) if isinstance(e, dict)
               and any(str(e.get(k) or "").strip() for k in keys)]
        if not new:
            out.setdefault(section, list((saved or {}).get(section) or []))
            continue
        old = (saved or {}).get(section) or []
        for e in new:
            m = _entry_match(old, e, keys)
            if not m:
                continue
            for f, v in m.items():
                blank_new = e.get(f) in (None, "", [], {})
                if blank_new and v not in (None, "", [], {}):
                    e[f] = v
        out[section] = new
    return out


def _flatten_profile(profile: dict) -> tuple[dict, list, list, list]:
    exp = []
    for e in profile.get("experience", []):
        for r in (e.get("roles") or [e]):
            exp.append({"org": e.get("org", ""), "title": r.get("title", ""),
                        "dates": r.get("dates", ""), "location": e.get("location", "")})
    # Carry the personal/portfolio markers so the gate's exemption for a linked GitHub project
    # (no employment location/dates) survives the flatten, otherwise an autonomous build stalls
    # (and batch autoapply silently skips) re-asking for a location/dates it can never have.
    projs = [{"org": p.get("org", "") or p.get("title", ""), "location": p.get("location", ""),
              "dates": p.get("dates", ""), "link": p.get("link"), "url": p.get("url"),
              "personal": p.get("personal")} for p in profile.get("projects", [])
             if not p.get("suggested")]   # flagged placeholders handled in commit 2
    return (profile.get("identity", {}) or {}), profile.get("education", []), exp, projs


def _backfill(dst_entries: list, src_entries: list, key_field: str, fields: tuple):
    """Fill empty ``fields`` on each dst entry from the src entry with the same
    ``key_field`` (school/org, matched case-insensitively). Used so the gate never
    asks for a value that's already on the built or saved CV."""
    src_by: dict = {}
    for s in src_entries or []:
        k = str(s.get(key_field) or "").strip().lower()
        if k:
            src_by.setdefault(k, s)
    for d in dst_entries or []:
        s = src_by.get(str(d.get(key_field) or "").strip().lower())
        if not s:
            continue
        for f in fields:
            if not str(d.get(f) or "").strip() and str(s.get(f) or "").strip():
                d[f] = s.get(f)


def _brain_items(st) -> dict:
    """The applicant brain's open facts (roles, schools, projects) as the gate's flat entries:
    {"experience": [{org, title, dates, location}], "education": [...], "projects": [...]}."""
    import json as _json
    out = {"experience": [], "education": [], "projects": []}
    if st is None:
        return out
    try:
        rows = st.q("SELECT key, value FROM fact WHERE person='default' AND store='' AND status='active'"
                    " AND key IN ('role','school','project')")
    except Exception:          # noqa: BLE001 - the brain is a help here, never a blocker
        return out
    for r in rows:
        try:
            v = _json.loads(r["value"])
        except (ValueError, TypeError):
            continue
        if not isinstance(v, dict):
            continue
        if r["key"] == "role":
            out["experience"].append({"org": v.get("org", ""), "title": v.get("title", ""),
                                      "dates": v.get("dates", ""), "location": v.get("location", "")})
        elif r["key"] == "school":
            out["education"].append({k: v.get(k, "") for k in ("school", "degree", "date", "location")})
        else:
            out["projects"].append({"org": v.get("title") or v.get("name") or v.get("org") or "",
                                    "dates": v.get("dates", "")})
    return out


def brain_backfill(identity: dict, education: list, experience: list, projects: list, st) -> None:
    """(Phase 3) The gate checks the applicant brain before asking: an empty contact detail, a
    role's dates or location, a school's date, a project's dates the brain already holds (from
    the details step, the extension, a resume import) is filled in place, so it is never asked
    again. Matches a role on its org AND title, never on the org alone. Does nothing without a
    brain."""
    if st is None:
        return
    from applicant import facts as _F
    try:
        mem = _F.identity(st)
    except Exception:          # noqa: BLE001
        mem = {}
    for f in ("name", "email", "phone", "address", "github", "linkedin", "blog"):
        if not str(identity.get(f) or "").strip() and str(mem.get(f) or "").strip():
            identity[f] = mem[f]
    items = _brain_items(st)
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()   # noqa: E731
    roles = {(norm(x["org"]), norm(x["title"])): x for x in items["experience"]}
    for d in experience or []:
        x = roles.get((norm(d.get("org")), norm(d.get("title"))))
        for f in ("dates", "location"):
            if x and not str(d.get(f) or "").strip() and str(x.get(f) or "").strip():
                d[f] = x[f]
    schools = {norm(x["school"]): x for x in items["education"]}
    for d in education or []:
        x = schools.get(norm(d.get("school")))
        for f in ("degree", "date", "location"):
            if x and not str(d.get(f) or "").strip() and str(x.get(f) or "").strip():
                d[f] = x[f]
    projs = {norm(x["org"]): x for x in items["projects"]}
    for d in projects or []:
        x = projs.get(norm(d.get("org")))
        if x and not str(d.get("dates") or "").strip() and str(x.get("dates") or "").strip():
            d["dates"] = x["dates"]


_ACTION_RE = re.compile(
    r"\b(?:built|led|designed|developed|created|launched|shipped|managed|owned|drove|"
    r"delivered|automated|analy[sz]ed|improved|implemented|migrated|scaled|reduced|"
    r"increased|grew|cut|boosted|optimi[sz]ed|architected|engineered|deployed|"
    r"maintained|mentored|coordinated|streamlined|established|introduced|spearheaded|"
    r"orchestrated|generated|saved|wrote|ran|founded|organi[sz]ed|presented|published|"
    r"trained|prepared|produced|queried|partnered|collaborated|supported|conducted)\b",
    re.I)


def _canonicalize_essentials(ess: dict) -> dict:
    """The LLM sometimes returns fields under synonym keys ('institution' for
    'school', 'company' for 'org', 'start'/'end' for 'dates', 'location' for a
    personal 'address'). Map them to the canonical keys the gate/render use so
    key-variance never makes the tool re-ask for something the person already gave."""
    if not isinstance(ess, dict):
        return ess
    ident = ess.get("identity")
    if isinstance(ident, dict):
        for syn, canon in (("mobile", "phone"), ("tel", "phone"), ("telephone", "phone"),
                           ("e-mail", "email"), ("mail", "email")):
            if not str(ident.get(canon) or "").strip() and str(ident.get(syn) or "").strip():
                ident[canon] = ident[syn]
    for e in ess.get("education", []) or []:
        if not isinstance(e, dict):
            continue
        for syn, canon in (("institution", "school"), ("university", "school"),
                           ("college", "school"), ("school_name", "school"),
                           ("graduated", "date"), ("grad_date", "date"), ("graduation", "date"),
                           ("program", "degree"), ("qualification", "degree")):
            if not str(e.get(canon) or "").strip() and str(e.get(syn) or "").strip():
                e[canon] = e[syn]
    for j in ess.get("experience", []) or []:
        if not isinstance(j, dict):
            continue
        for syn, canon in (("company", "org"), ("employer", "org"), ("organization", "org"),
                           ("organisation", "org"), ("role", "title"), ("position", "title"),
                           ("job_title", "title")):
            if not str(j.get(canon) or "").strip() and str(j.get(syn) or "").strip():
                j[canon] = j[syn]
        if not str(j.get("dates") or "").strip():
            start, end = str(j.get("start") or "").strip(), str(j.get("end") or "").strip()
            if start:
                j["dates"] = f"{start} - {end}" if end else start
    return ess


def _normalize_identity(ident: dict) -> dict:
    """The extractor (esp. the real model) may put the person's location under
    'city' or 'location'; the gate and both renderers use 'address'. Copy it over
    so a typed address actually satisfies the gate and shows on the CV."""
    if isinstance(ident, dict) and not str(ident.get("address") or "").strip():
        for alt in ("city", "location", "town"):
            if str(ident.get(alt) or "").strip():
                ident["address"] = ident[alt]
                break
    # A link field holding a resume's anchor TEXT ("In", "GitHub") is not a link: an older
    # import stored those, and the letterhead turned them into dead links. Blank them so the
    # gate asks for (or the next upload fills) the real address.
    from intake.links import clean_identity_links
    clean_identity_links(ident)
    return ident


def _fill_project_locations(education: list, experience: list, projects: list):
    """A project whose org is a school/company the person already listed shares
    that place's location (a capstone 'at DePaul' is in Chicago). Fill it in so the
    gate doesn't ask for a location that's already on the CV under another section."""
    loc: dict = {}
    for j in experience or []:
        n = str(j.get("org") or "").strip().lower()
        if n and str(j.get("location") or "").strip():
            loc.setdefault(n, j["location"])
    for d in education or []:
        n = str(d.get("school") or "").strip().lower()
        if n and str(d.get("location") or "").strip():
            loc.setdefault(n, d["location"])
    for p in projects or []:
        if not str(p.get("location") or "").strip():
            hit = loc.get(str(p.get("org") or "").strip().lower())
            if hit:
                p["location"] = hit


def _merge_turns(*sequences) -> list[dict]:
    """Union of turn lists in order, de-duplicated by (role, content), so the
    SQLite-saved history and the diary combine without repeats."""
    seen, out = set(), []
    for seq in sequences:
        for t in seq or []:
            key = (t.get("role", ""), t.get("content", ""))
            if key not in seen:
                seen.add(key)
                out.append({"role": t.get("role", "user"), "content": t.get("content", "")})
    return out


def classify_document(text: str, filename: str = "") -> str:
    """Read an uploaded document and decide what it IS, a transcript, a certificate,
    or a CV/résumé, so ONE upload button can route it correctly (the person should
    never have to tell us). Content-first, with the filename as a strong hint."""
    fn = (filename or "").lower()
    low = (text or "").lower()
    head = low[:600]
    course_codes = len(re.findall(r"\b[A-Z]{2,4}\s?\d{3,4}\b", text or ""))
    tr_cues = sum(w in low for w in ("transcript", "gpa", "semester", "cumulative",
                                     "registrar", "credit hours", "grade point", "quarter units"))
    if re.search(r"transcript|tsrpt|tscript|acad(?:emic)?[_\- ]?record", fn):
        return "transcript"
    if "transcript" in head or (course_codes >= 4 and tr_cues >= 2):
        return "transcript"
    if ((re.search(r"cert(?:ificate|ification|ified)|credential", fn)
         or re.search(r"this (?:is to )?certif|certificate of|certificate in|has (?:successfully )?"
                      r"completed|awarded to|is hereby", head))
            and course_codes < 3 and tr_cues < 2):
        return "certificate"
    return "cv"


def _certificate_name(text: str, filename: str = "") -> str:
    """Pull a plausible certification name from a certificate's text (best effort), anchored near the credential keyword so it doesn't grab the whole sentence."""
    t = re.sub(r"\s+", " ", text or "")
    for p in (r"((?:[A-Z][\w&/.\-]*\s+){0,3}Certified(?:\s+[A-Z][\w&/.\-]*){0,4})",
              r"([A-Z][\w &/.\-]{2,40}?\s+Certification\b)",
              r"(Certificate\s+(?:in|of)\s+[A-Z][\w &/.\-]{2,40})"):
        m = re.search(p, t)
        if m:
            name = m.group(1).strip(" .,")
            if 4 < len(name) < 60:
                return name
    return ""


def _locked(fn):
    """Run a session-changing method under the session's lock (WebIntake.lock), so the chat, a
    card's click, an upload, the page's own edits and a passed quiz on another request thread
    never change the session at the same time."""
    @functools.wraps(fn)
    def run(self, *a, **kw):
        with self.lock:
            return fn(self, *a, **kw)
    run._session_locked = True
    return run


def _builds(kind: str):
    """One build or rebuild of the page, told in the chat (Claude and ChatGPT show their work in
    the conversation, the document beside it). Opens the live activity run (ui/activity.py) the
    chat polls while the request runs; when the page was rebuilt and kept, ONE agent turn goes
    into the history at the point the build started, carrying the steps and what changed
    (WebIntake._record_build), and the returned state carries it as ``build``. A build inside a
    build (draft from the build button) is one turn."""
    def deco(fn):
        @functools.wraps(fn)
        def run(self, *a, **kw):
            if self._bt is not None:
                return fn(self, *a, **kw)
            bt = {"kind": kind, "h0": len(self.history), "v0": self._pdf_v, "args": a}
            self._bt = bt
            try:
                with self._build_feed() as r:
                    out = fn(self, *a, **kw)
            finally:
                self._bt = None
            rec = self._record_build(bt, r, out)
            if rec is not None:
                if isinstance(out, dict) and "phase" in out:
                    out["build"] = rec
                    # The lines said before the build started ("Building your resume now")
                    # stay above its message; the rest follow it.
                    out["build_at"] = int(bt.get("lead") or 0)
                else:                       # a helper's result: the next state carries it
                    self._pending_build = rec
            return out
        return run
    return deco


def _one_voice(fn):
    """One chat turn, said in ONE message written by the AI (Kofi, 2026-11-10). The code inside
    the turn decides what happens and what must be said, and says it as before: a fixed line, or
    a model reply. Each fixed line is a brief item (WebIntake._fact) with its facts. When the turn
    ends, WebIntake._speak turns everything said in it into one message: kept as is when it is a
    single model reply already, else written by the narrator (agent/narrate.py) from the brief
    and the chat, else (the call failed) the lines joined. The history then holds that one
    message, with the turn's cards on it, after the build's summary row. A turn inside a turn
    (a build from an upload) is one turn."""
    @functools.wraps(fn)
    def run(self, *a, **kw):
        if self._turn is not None:
            return fn(self, *a, **kw)
        turn = {"h0": len(self.history), "v0": self._pdf_v, "sig": self._material_sig(),
                "items": {}, "model": set(), "message": ""}
        self._turn = turn
        try:
            out = fn(self, *a, **kw)
        finally:
            self._turn = None
        with self.lock:
            return self._speak(turn, out)
    return run


class WebIntake:
    # The body sections the page renders: the template's own (its manifest), plus Interests
    # whenever the person gave them and the template has no Interests section of its own. Kofi
    # answered the hobbies question and a D365 build in the Software Engineer template (no
    # Interests section) printed none of it (2026-11-10): what the person gives always prints.
    # It goes last, as "Additional Information", and renders nothing while they have none.
    @property
    def sections(self) -> list:
        secs = list(getattr(self, "_template_sections", None) or [])
        if secs and "interests" not in secs and self._has_interests():
            secs.append("interests")
        return secs

    @sections.setter
    def sections(self, value) -> None:
        self._template_sections = list(value or [])

    def _has_interests(self) -> bool:
        return self._section_filled("interests", getattr(self, "profile", None) or {},
                                    getattr(self, "essentials", None) or {},
                                    getattr(self, "saved_profile", None) or {})

    def __init__(self, jd_text, template_source, llm, memory, records, workdir,
                 profile_name="default", jobname="cv", palace_dir=None,
                 template_name=None):
        self.jd = jd_text
        self.template = template_source
        # The selected template's interview config, slot schema + question bank the
        # LLM draws on to converse naturally (one template today; future picker passes
        # the name). Product config, kept out of the person's diary.
        from intake.template_manifest import (headings, identity_fields, load_manifest,
                                               required_sections, sections)
        self.manifest = load_manifest(template_name)
        self.template_name = template_name
        self.identity_fields = identity_fields(self.manifest)
        # The selected template drives the CV's shape AND the questions: `sections` is the
        # body render order; `required_fill` is what the person MUST provide (or decline)
        # before we can build to this template's standard (e.g. extracurricular+interests
        # for shetty; nothing extra for the summary-first template).
        self.sections = sections(self.manifest)
        self.headings = headings(self.manifest)
        self.required_fill = required_sections(self.manifest)
        self.llm = llm
        self.memory = memory
        self.records = records
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.profile_name = profile_name
        self.jobname = jobname
        self.role, self.company = _jd_label(jd_text)

        # The chat diary (intake/diary.py): every turn kept verbatim, keyword recall. Defaults
        # under the workdir so tests stay isolated; the app passes a stable shared dir.
        self.palace = Diary(palace_dir or (self.workdir / "palace"), person=profile_name)
        self._last_user_ref = ""          # diary line of the person's latest turn (a fact's source_ref)

        self.essentials = {"identity": {}, "education": [], "experience": []}
        self.history: list[dict] = []      # this session's turns
        self.prior_history: list[dict] = []  # earlier sessions (kept, not replayed)
        self.recalled: list[dict] = []     # diary lines recalled for this JD (filled on start)
        self.cv_selection: dict = {}       # P1: recall->decide reasoning for this build (surfaced)
        self.last_missing: list[str] = []  # outstanding gate fields from the last ask
        self.last_declined: list[str] = []  # declines already acknowledged (don't re-ack)
        self.profile = self._empty()
        self.assembled = None
        self.stage = "chatting"            # chatting | review | done
        self.returning = False
        self.saved_profile = {}
        # The files the person uploaded in THIS session and what was read from them, so the
        # chat answers "did you see my projects?" from what it really parsed (never "I can't
        # read files"). And a hook the app sets to report its GitHub connection:
        # () -> {"connected": bool, "login": str}. None in tests and bare sessions.
        self.uploads: list[dict] = []
        self.github_status = None
        # The stored GitHub token, () -> str, set by the app beside github_status. Only the
        # agent's list_repos callable holds it (ui/github_repos.py); it is never shown or logged.
        self.github_token = None
        # The app's hook for the Telegram connection, () -> {"connected": bool}, so the chat
        # states it truly. None in tests and bare sessions.
        self.telegram_status = None
        # The repo ranking (ui/repo_rank.py): at most three model calls a session, cached by job,
        # repositories and resume projects. And the single-use tokens a repo_fit card's button
        # spends (POST /api/session/action); only the person's click uses one, never a tool.
        self.rank_state: dict = {"count": 0, "cache": {}}
        self.action_tokens: dict = {}
        # The app's hooks for project ideas aimed at THIS job (() -> ui/app._ideas_for's body) and
        # for starting an AI build from a chat card ((idea, for_resume) -> the project), set by
        # ui/app._session(). None in tests and bare sessions.
        self.ideas_for = None
        self.start_build = None
        # One person's resume is changed from more than one request thread: the chat, a card's
        # click, and a passed quiz on the Projects page (which adds the project here). Every
        # change to this session runs under this lock; an RLock, so a change that calls
        # another (a click that rebuilds) does not wait on itself.
        self.lock = threading.RLock()
        self._connect_due = False          # a reply said GitHub is not connected (_reply_with_connect)
        self._project_offered = False      # the one proactive projects offer (_project_offer)
        self.last_cover_letter = ""        # most recent drafted cover letter (bundled on accept)
        self.last_screening = []           # most recent drafted screening Q/A (bundled on accept)
        self.source_job = {}               # {url, source, source_id} when started from a sourced job
        self.keyword_actions: list[dict] = []   # "Add this keyword" edits on this page, undo one by one
        # The person's choices (applicant brain, Phase 5): this resume's own switches ("Show on
        # this resume", "Swap for ..."), the one line that asks once whether to keep a change for
        # similar jobs, the overlapping-roles question, and choices read in chat waiting for Save.
        self.session_choices: dict = {}          # item id -> "show" | "hide", this resume only
        self.choice_ask: dict | None = None
        self._choice_asked = False
        self.overlap_ask: dict | None = None
        self.rule_proposals: list[dict] = []
        self._new_proposals: list[dict] = []
        self._born_rules: set = set()            # choices saved from this resume (it used them)
        self._labels: dict | None = None
        self._keyword_previews: dict = {}        # term -> the last validated preview, for apply
        # What the one-page fit cut that the person put back (assembler.trim_id): never cut
        # again on this resume; the next thing goes instead (assembler._shrink_once).
        self.kept_trims: set = set()
        # The base resume "What changed" is measured against (tailoring/changes.py): the saved
        # profile a tailored build started from. None for a page drafted from the chat.
        self._change_base: dict | None = None
        self._last_reverted: list = []           # rewrites the fact gate kept in the person's words
        # Bullets the recency fill WROTE from a role's own material on the last fresh build
        # (assembler.AssembleResult.written), kept while they are on the page unedited, so
        # "What changed" lists them for the person to check.
        self._written: list = []

        saved = memory.load(profile_name)
        if saved:
            self.saved_profile = saved.get("profile") or {}
            # (Phase 6) The resume material assembled from memory: the person's own contact
            # details and the dates the brain holds, laid over the saved material
            # (applicant/profile.py). No model call; nothing invented.
            try:
                from applicant.profile import assemble
                self.saved_profile = assemble(self.saved_profile, self._brain())
            except Exception:      # noqa: BLE001 - the saved material always stands on its own
                pass
            # An older import stored a resume's anchor text ("In", "GitHub") as the links;
            # never carry those to the letterhead as dead links.
            from intake.links import clean_identity_links
            if isinstance(self.saved_profile.get("identity"), dict):
                clean_identity_links(self.saved_profile["identity"])
            # Seed the built profile FROM the saved one, so the first autosave of a returning
            # session writes the real profile back instead of clobbering the durable saved
            # profile (bullets, projects, extracurricular, skills) with the empty skeleton a
            # session starts with, the returning-user data-loss bug (§4b).
            if self.saved_profile.get("experience") or \
                    (self.saved_profile.get("identity") or {}).get("name"):
                self.profile = normalize_profile(self.saved_profile)
            ess = _essentials_from(saved.get("essentials") or {}, self.saved_profile)
            if ess["identity"].get("name") or ess["experience"] or ess["education"]:
                self.returning = True
                self.essentials = ess
                self.prior_history = list(saved.get("history", []))

        # Reload every turn ever recorded for this person from the diary, so a
        # restart mid-conversation loses nothing and no entry point forgets, the
        # transcript is the source of truth even when Accept never happened.
        self.prior_history = _merge_turns(self.prior_history, self.palace.load_history())

    # ------------------------------------------------------------------ helpers
    def _append(self, role: str, content: str):
        """Record one turn in the session AND persist it verbatim to the diary,
        as it happens, the fix for 'nothing persisted until Accept'. Inside a turn the app's own
        lines wait: the turn's one message reaches the diary when it is said (_speak)."""
        self.history.append({"role": role, "content": content})
        if role == "agent" and self._turn is not None:
            return
        try:
            ref = self.palace.remember_turn(role, content)
            if role == "user" and ref:
                self._last_user_ref = ref
        except Exception:
            pass

    def _persist(self):
        """Autosave the structured profile/essentials to the SQLite store on every
        turn, so a returning user is recognized even if they never clicked
        Accept & Save. (The diary holds the verbatim copy in parallel.)"""
        try:
            self.memory.save(self.profile_name, self.profile or {}, self.essentials,
                             self.prior_history + self.history)
        except Exception:
            pass
        self._brain_contact()

    def _brain(self):
        """(Phase 3) The applicant brain beside this session's memory file, for the person's own
        profile only ("default": never a variant or a batch's scratch copy). None when there is
        no brain there (a test's bare memory) or it is off."""
        if self.profile_name != "default":
            return None
        p = str(getattr(self.memory, "db_path", "") or "")
        if not p or p == ":memory:":
            return None
        d = Path(p).parent
        if not (d / "applicant.db").exists():
            return None
        try:
            from applicant import migrate as _bm
            return _bm.ensure(d)
        except Exception:      # noqa: BLE001 - the brain never breaks the chat
            return None

    def _brain_material(self) -> list:
        """The person's own material for the fact gate (tailoring/fact_gate.py), beyond the page
        being built: the Phase 6 profile assembled from memory (applicant/profile.py, already in
        self.saved_profile) and the brain's active facts as plain strings, so a rewrite may name
        what the person told the brain even when the resume material does not say it.
        Self-identification (EEO) answers are never read. Local only; never sent anywhere."""
        out: list = [self.saved_profile] if self.saved_profile else []
        st = self._brain()
        if st is None:
            return out
        try:
            from applicant import keys as _K
            from applicant.store import ACTIVE
            out += [" ".join(str(r[k] or "") for k in ("subject", "value"))
                    for r in st.facts(store="", status=ACTIVE)
                    if _K.sensitivity(r["key"]) != _K.EEO]
        except Exception:      # noqa: BLE001 - the gate works from the profile alone
            pass
        return out

    def _brain_contact(self):
        """(Phase 3) The contact details the person gave the builder are facts: their own words
        in answer to the builder's question (source "you"), with the diary line as the ref. An
        unchanged value writes nothing."""
        st = self._brain()
        if st is None:
            return
        try:
            from applicant import facts as _F
            ident = dict(self.essentials.get("identity") or {})
            _F.write_contact(st, ident, source="you", source_ref=self._last_user_ref or "builder")
        except Exception:      # noqa: BLE001
            pass

    # ------------------------------------------------------------------ P1 memory
    @staticmethod
    def _is_meaningful_turn(message: str) -> bool:
        """Worth distilling: real prose, not a one-word command or a bare 'yes'/'ok'."""
        m = (message or "").strip()
        return len(m) >= 15 and len(m.split()) >= 3

    _FREEFORM = re.compile(
        r"\b(do ?n['’]?t mention|do not mention|leave (?:off|out)|please omit|"
        r"targeting|i care about|i'?m interested in|interested in|i prefer|i'?d prefer|"
        r"focus(?:ed|ing)? on|needs? (?:visa )?sponsorship|require[sd]? sponsorship|"
        r"gap was|took time off|career break|caregiving|sabbatical|maternity|"
        r"what do you (?:remember|know|think)|do you remember|who am i|my goal|my goals)\b",
        re.I)

    def _wants_freeform_chat(self, message: str) -> bool:
        """A turn that states context / goals / preferences rather than answering the gate or
        asking to build. These get a conversational reply (and are remembered), instead of being
        funneled into a CV field question. Deliberately conservative so it never hijacks a normal
        CV-building answer ('my name is…', 'I worked at X 2019-2021', 'add these projects:…')."""
        return bool(self._FREEFORM.search(message or ""))

    def _distill_facts(self) -> list[dict]:
        """Distill NEW durable facts/preferences/constraints from the chat into the authoritative
        SQLite store (deduped, with provenance) and mirror genuinely-new ones into the diary so
        recall surfaces them too. The provenance names the diary line of the person's latest
        turn (``diary:default.jsonl:212``), which becomes the fact's ``source_ref`` in the
        applicant brain. Best-effort: never breaks the chat."""
        from datetime import date

        from notify.assistant import carries_eeo
        new: list[dict] = []
        try:
            existing = self.memory.list_facts(self.profile_name, status=None)
            known = {f["key"] for f in existing}
            # (Phase 3) A turn that states a self-identification answer never reaches the model:
            # chat never sets one (docs/applicant-brain.md, section 9).
            turns = [t for t in self.prior_history + self.history
                     if not (t.get("role") == "user" and carries_eeo(t.get("content")))]
            result = self.llm.distill_memory(turns, sorted(known))
        except Exception:
            return new
        turn_no = len(self.prior_history) + len(self.history)
        self._propose_rules(result.get("rules") or [])
        for f in (result.get("facts") or []):
            prov = f"turn {turn_no} · {date.today().isoformat()}"
            if self._last_user_ref:
                prov += f" · {self._last_user_ref}"
            try:
                row = self.memory.upsert_fact(self.profile_name, f.get("type"), f.get("key"),
                                              f.get("value"), provenance=prov,
                                              confidence=f.get("confidence", 0.8))
            except Exception:
                continue
            if not row:
                continue
            new.append(row)
            if row["key"] not in known:      # mirror only genuinely-new facts, no diary spam
                try:
                    self.palace.remember_summary(f"[{row['type']}] {row['key']}: {row['value']}")
                except Exception:
                    pass
        if new:
            self._brain_sync_chat()
        return new

    def _brain_sync_chat(self):
        """(Phase 3) The chat's distilled facts land in the applicant brain at their own confidence
        (0.7 by default): a fact naming a form answer (salary, start date) waits there as a
        proposal for the person to confirm, a legal one as unconfirmed, a self-identification one
        is refused. Returns the brain (or None)."""
        st = self._brain()
        if st is None:
            return None
        try:
            from applicant import facts as _F
            _F.sync_chat(st, self.memory.list_facts(self.profile_name, status=None))
        except Exception:      # noqa: BLE001
            return None
        return st

    def _preferences(self) -> list:
        """The active preferences and constraints tailoring honors: from the applicant brain when
        it is on (kept equal to the chat's distilled facts first), else the memory store."""
        st = self._brain_sync_chat()
        if st is not None:
            try:
                from applicant import facts as _F
                return _F.notes(st)
            except Exception:  # noqa: BLE001
                pass
        return self.memory.active_preferences(self.profile_name)

    def _converse(self, message: str):
        """Handle a free-form turn conversationally with the real LLM, grounded in the full
        history + recalled memories + known facts. The turn is already appended and distilled;
        here we just reply and persist. Never enters the build funnel."""
        facts = self.memory.list_facts(self.profile_name, status="active")
        try:
            reply = str(self.llm.converse(self.jd, self.prior_history + self.history,
                                          self.recalled, facts,
                                          app_context=self._app_context()) or "").strip()
        except Exception:
            reply = ""
        self._connect_due = False
        reply = self._no_false_limits(reply, message)
        if self._connect_due:
            return self._reply_with_connect(reply)
        reply = self._model_said(reply) or self._fact("ack", "Noted, I'll remember that.")
        self._append("agent", reply)
        self._persist()
        return self._state([reply])

    def _reply_with_connect(self, reply: str):
        """A reply that says GitHub is not connected, with the Connect GitHub button under it
        (the button is the way; the chat never lists menu steps)."""
        from agent.resume_chat import connect_card
        self._connect_due = False
        card = connect_card()
        self._model_said(reply)
        self._append("agent", reply)
        self.history[-1]["card"] = card
        self._persist()
        return self._state([reply], cards=[card])

    # -- the resume chat agent (TAILOR_CHAT_AGENT, agent/resume_chat.py) --------------- #
    def _agent_on(self) -> bool:
        from agent.resume_chat import enabled
        return enabled() and callable(getattr(self.llm, "agent_turn", None))

    def _chat_reply(self, message: str):
        """A conversational turn: the agent when its flag is on, else today's _converse."""
        return self._agent_reply(message) if self._agent_on() else self._converse(message)

    def _github_view(self) -> dict:
        """The GitHub connection as the chat states it. It agrees with the token the repo
        lookup uses: a saved token is connected unless GitHub itself rejected it."""
        from ui.github_repos import login_from
        got = self._github()
        gh = got or {}
        has_token = bool(self._token())
        connected = bool(gh.get("connected")) or (has_token and gh.get("reason") != "token_rejected")
        ident = dict((self.profile or {}).get("identity") or {})
        ident.update({k: v for k, v in (self.essentials.get("identity") or {}).items() if v})
        return {"connected": connected, "login": str(gh.get("login") or ""),
                "resume_login": login_from(ident.get("github") or "")}

    def _token(self) -> str:
        try:
            return str(self.github_token() or "") if callable(self.github_token) else ""
        except Exception:      # noqa: BLE001 - no token reads as not connected
            return ""

    def _snapshot(self) -> dict:
        """The resume being built, compact, for the agent's resume_snapshot tool."""
        used = getattr(self.assembled, "profile_used", None) or self.profile or {}
        ident = dict(used.get("identity") or {})
        ident.update({k: v for k, v in (self.essentials.get("identity") or {}).items() if v})

        def label(e):
            return str(e.get("org") or e.get("title") or e.get("name") or "").strip()

        def first_bullet(e):
            for b in e.get("bullets") or []:
                t = b.get("text") if isinstance(b, dict) else b
                if str(t or "").strip():
                    return str(t).strip()[:180]
            return ""

        exp = [{"title": str(e.get("title") or ""), "org": str(e.get("org") or ""),
                "dates": str(e.get("dates") or e.get("date") or "")}
               for e in (used.get("experience") or self.essentials.get("experience") or [])
               if isinstance(e, dict)][:8]
        projs = [e for e in (used.get("projects") or []) if isinstance(e, dict) and label(e)]
        names = {label(e).lower() for e in projs}
        projs += [e for e in (self.essentials.get("projects") or [])
                  if isinstance(e, dict) and label(e) and label(e).lower() not in names]
        skills = used.get("skills") or {}
        if isinstance(skills, dict):
            skills = {str(k): str(v)[:200] for k, v in list(skills.items())[:8]}
        state = self._agent_state()
        return {
            **{k: state[k] for k in ("page", "job_terms_on_page", "job_terms_not_on_page",
                                     "keywords_the_person_added", "empty_sections") if k in state},
            "role": self.role, "company": self.company,
            "draft_built": self.assembled is not None,
            "name": str(ident.get("name") or ""),
            "links": {k: str(ident[k]) for k in ("github", "linkedin", "website", "portfolio")
                      if ident.get(k)},
            "experience": exp,
            "projects": [{"name": label(e), "link": str(e.get("link") or e.get("url") or ""),
                          "first_bullet": first_bullet(e),
                          **({"placeholder": True} if e.get("suggested") else {})}
                         for e in projs][:12],
            "skills": skills,
            "education": [{"school": str(e.get("school") or ""), "degree": str(e.get("degree") or "")}
                          for e in (used.get("education") or self.essentials.get("education") or [])
                          if isinstance(e, dict)][:4],
        }

    def _page_fill_pct(self) -> int | None:
        """The measured fill of the page in percent: 100 once it is full, None with no page."""
        a = self.assembled
        if a is None:
            return None
        fill = getattr(a, "fill_ratio", None)
        # The measured number, also above the short-page bar: "100%" over a page with white
        # space at the bottom was the agent's answer to "why is there so much space?".
        return min(100, round(fill * 100)) if fill else 100

    def _empty_sections(self, used: dict) -> list[str]:
        heads = self.headings if isinstance(self.headings, dict) else {}
        out = []
        for sec in self.sections or []:
            if sec in ("projects", "extracurricular", "interests", "certifications", "summary") \
                    and not self._section_filled(sec, used):
                out.append(str(heads.get(sec) or sec).replace("\\&", "&").replace("\\", "").strip())
        return out

    def _agent_state(self) -> dict:
        """What the page and the app hold right now, for the agent's system prompt every turn, so
        it never says a job term is missing when it is on the page (Kofi had ADDed X++ and Azure
        Data Factory and was told the gap was still open, 2026-11-10), and knows the page's
        measured fill and the connections."""
        st: dict = {"resume_built": self.assembled is not None}
        if self.assembled is not None:
            a = self.assembled
            try:
                cov = self.coverage_dict() or {}
            except Exception:      # noqa: BLE001
                cov = {}
            used = getattr(a, "profile_used", None) or self.profile or {}
            pct = self._page_fill_pct()
            st["page"] = {"one_page": bool(a.ok and a.compile is not None and a.compile.pages == 1),
                          "fill_percent": pct,
                          "short": bool(getattr(a, "underfull", False)) or (pct or 100) < 97}
            added = [k["term"] for k in self.keyword_actions]
            low = {t.lower() for t in added}
            st["job_terms_on_page"] = list(dict.fromkeys(
                list(cov.get("present") or []) + [t for t in added if self._term_on_page(t)]))
            st["job_terms_not_on_page"] = [t for t in (list(cov.get("missing_supported") or [])
                                                       + list(cov.get("missing") or []))
                                           if t.lower() not in low]
            st["keywords_the_person_added"] = added
            st["projects_on_page"] = [
                {"name": str(p.get("org") or p.get("title") or p.get("name") or "").strip(),
                 "github_link": "github.com/" in str(p.get("link") or p.get("url") or "").lower()}
                for p in (used.get("projects") or []) if isinstance(p, dict)
                and str(p.get("org") or p.get("title") or p.get("name") or "").strip()]
            st["empty_sections"] = self._empty_sections(used)
            st["interests_on_page"] = str(used.get("interests") or "").strip()[:200]
        gh = self._github_view()
        st["github"] = {"connected": bool(gh.get("connected")), "login": gh.get("login") or "",
                        "username_on_resume": gh.get("resume_login") or ""}
        try:
            tg = self.telegram_status() if callable(self.telegram_status) else None
        except Exception:      # noqa: BLE001 - unknown is left out, never guessed
            tg = None
        if isinstance(tg, dict):
            st["telegram_connected"] = bool(tg.get("connected"))
        return st

    @_builds("fill")
    def _fill_page_now(self) -> dict:
        """The agent's fill_page tool, for "why is there so much white space?": act, never guess.
        Re-runs the page fit with the fill (tailoring/assembler.py: full-width lines, bullets
        written from the person's own material for a short page), keeps it only when it is
        still one clean page, and returns what changed and, when the page is still short, the
        real reasons and what real material would fill it."""
        import copy as _copy
        if self.assembled is None:
            return {"ok": False, "error": "There is no resume on the page yet."}
        before_pct = self._page_fill_pct()
        before = (self.assembled, _copy.deepcopy(self.profile), list(self._written), self._pdf_v)
        # The page's files, so a refit that is not kept leaves the PDF the person sees as it was.
        files = {}
        for ext in (".pdf", ".tex"):
            f = self.workdir / f"{self.jobname}{ext}"
            try:
                files[f] = f.read_bytes()
            except OSError:
                pass

        def roll_back():
            self.assembled, self.profile, self._written, self._pdf_v = before
            for f, data in files.items():
                try:
                    f.write_bytes(data)
                except OSError:
                    pass

        _activity.step("fill", "Filling the page", "from your own material")
        try:
            self._assemble(fit_lines=True)
        except Exception:      # noqa: BLE001 - the page stays as it was
            roll_back()
            return {"ok": False, "error": "The fill did not run just now. The page is unchanged."}
        a = self.assembled
        if not (a.ok and a.compile is not None and a.compile.pages == 1):
            roll_back()
            return {"ok": False, "changed": False, "fill_percent": before_pct,
                    "error": "Filling it would not fit one page, so the page is unchanged."}
        after_pct = self._page_fill_pct()
        if (after_pct or 0) <= (before_pct or 0) and not self._written:
            # No fuller: the page as it was stays (a refit that came out shorter was kept once,
            # 92% to 89%, 2026-11-10), and the tool says why it is still short.
            roll_back()
            a = self.assembled
            after_pct = before_pct
        self._persist()
        out = {"ok": True, "ran": True, "changed": after_pct != before_pct or bool(self._written),
               "fill_before_percent": before_pct, "fill_after_percent": after_pct,
               "bullets_written_from_their_material": [
                   {"where": str(w.get("label") or ""), "bullet": str(w.get("bullet") or w.get("text") or "")}
                   for w in self._written[:6] if isinstance(w, dict)],
               "cut_to_fit": len(getattr(a, "trims", None) or [])}
        if getattr(a, "underfull", False):
            used = getattr(a, "profile_used", None) or self.profile or {}
            why = [str(x) for x in (getattr(a, "bullet_notes", None) or []) if str(x).strip()]
            gaps = getattr(a, "line_gaps", None) or {}
            short = [str(x.get("label") or "") for x in gaps.get("short_skills") or [] if x.get("label")]
            if short:
                why.append("These skills lines stop short of the margin: " + ", ".join(short[:3]) + ".")
            empty = self._empty_sections(used)
            if empty:
                why.append("These sections are empty: " + ", ".join(empty) + ".")
            if not why:
                why.append("Every role and project already uses all the material on file.")
            out.update({"still_short": True, "why_still_short": why,
                        "real_material_that_would_fill_it": self._cheapest_gaps_first()[:3]})
        return out

    def _agent_ctx(self):
        from agent.resume_chat import AppContext
        from ui import github_repos
        return AppContext(
            state=self._agent_state, fill_page=self._fill_page_now,
            llm=self.llm, resume_snapshot=self._snapshot,
            upload_summary=lambda: (self.uploads[-1] if self.uploads else None),
            github_status=self._github_view,
            list_repos=lambda login: github_repos.list_repos(login, token=self._token()),
            history=lambda: self.prior_history + self.history,
            secrets=lambda: [t for t in (self._token(),) if t],
            rank_repos=self._rank_for_job, offer=self._offer,
            suggest_ideas=self._ideas if callable(self.ideas_for) else None,
            offer_build=self._offer_build if callable(self.start_build) else None,
            add_top=self._add_top,
            role=self.role or "", company=self.company or "")

    # -- repo ranking and adding (plan PRs 3 and 4) ------------------------------------- #
    def _resume_projects(self, github_too: bool = False) -> list[dict]:
        """The projects on this resume (the page, then the chat's own), each once. Placeholder
        suggestions are not the person's; projects added from GitHub are judged as repos."""
        from intake.links import same_title
        used = getattr(self.assembled, "profile_used", None) or self.profile or {}
        out: list[dict] = []
        for p in (used.get("projects") or []) + (self.essentials.get("projects") or []):
            if not isinstance(p, dict) or p.get("suggested"):
                continue
            if p.get("source") == "github" and not github_too:
                continue
            name = str(p.get("org") or p.get("title") or p.get("name") or "")
            if name and not any(same_title(name, str(q.get("org") or q.get("title") or q.get("name") or ""))
                                for q in out):
                out.append(p)
        return out

    def _on_resume(self, repo: dict) -> bool:
        from ui.repo_rank import same_project as same_title
        for p in self._resume_projects(github_too=True):
            name = str(p.get("org") or p.get("title") or p.get("name") or "")
            link = str(p.get("link") or p.get("url") or "").rstrip("/").lower()
            if (repo.get("full_name") and p.get("repo") == repo.get("full_name")) \
                    or (link and link == str(repo.get("url") or "").rstrip("/").lower()) \
                    or same_title(name, str(repo.get("name") or "")):
                return True
        return False

    def _rank_for_job(self, login: str) -> dict:
        """The person's repositories (and resume projects) judged for this job, through
        ui/repo_rank.rank with this session's cap and cache. Each repo says whether it is on the
        resume already. The token makes the GitHub calls and goes nowhere else."""
        from ui import github_repos, repo_rank
        repos, err = [], ""
        if login:
            got = github_repos.list_repos(login, token=self._token())
            if got.get("ok"):
                repos = list(got.get("repos") or [])
                login = got.get("login") or login
            else:
                err = str(got.get("error") or "")
        projects = self._resume_projects()
        if err and not projects:
            return {"ok": False, "error": err}
        out = repo_rank.rank(self.llm, self.jd, repos, projects, login=login, token=self._token(),
                             state=self.rank_state)
        out["repos"] = [{**r, "on_resume": self._on_resume(r)} for r in out.get("repos") or []]
        if err:
            out["github_error"] = err
        return out

    def _offer(self, repos: list) -> str:
        """A single-use action token for a card that offers these ranked repositories. The
        entries are built now, from what the card shows (checked bullets, real link, dates)."""
        import secrets as _secrets
        import time as _time
        from ui.repo_rank import entry_for
        entries = {r["key"]: entry_for(r) for r in repos or [] if isinstance(r, dict) and r.get("key")}
        if not entries:
            return ""
        tok = _secrets.token_urlsafe(18)
        self.action_tokens[tok] = {"kind": "add_projects", "entries": entries, "used": False,
                                   "at": _time.time()}
        for old in list(self.action_tokens)[:-20]:          # keep the last twenty cards
            self.action_tokens.pop(old, None)
        return tok

    def _ideas(self) -> dict:
        """Project ideas for this job, from the app (ui/app._session_ideas: this session's role
        and JD skills, the daily cap, the cache)."""
        return dict(self.ideas_for() or {})

    def _for_resume(self) -> dict:
        import hashlib
        return {"jobname": self.jobname, "role": self.role or "", "company": self.company or "",
                "jd_sha": hashlib.sha1(str(self.jd or "").encode("utf-8")).hexdigest()[:12]}

    def _offer_build(self, ideas: list) -> str:
        """A single-use token for an ideas card: one click on one idea's "Build this for this
        job" starts that build (one package) for this resume. The ideas are kept as shown."""
        import secrets as _secrets
        import time as _time
        keep = {}
        for i in ideas or []:
            if isinstance(i, dict) and str(i.get("title") or "").strip():
                k = str(i.get("repo") or i["title"]).strip()[:80]
                keep[k] = {f: i.get(f) for f in ("title", "pitch", "skills", "gaps", "tech", "repo")}
        if not keep:
            return ""
        tok = _secrets.token_urlsafe(18)
        self.action_tokens[tok] = {"kind": "build", "ideas": keep, "used": False, "at": _time.time()}
        for old in list(self.action_tokens)[:-20]:
            self.action_tokens.pop(old, None)
        return tok

    BUILD_STARTED = ("I started building {title} for this job. It takes a few minutes. When it is "
                     "built, learn it and pass its quiz, and I will add it to this resume.")

    def _apply_build(self, token: str, rec: dict, args: dict) -> dict:
        key = str(args.get("key") or "")
        idea = rec["ideas"].get(key)
        if idea is None or not callable(self.start_build):
            st = self._state([])
            st.update({"action_ok": False, "action_error": "Pick one of the ideas on the card."})
            return st
        rec["used"] = True          # before the start: a second click can never count a package
        try:
            proj = dict(self.start_build(dict(idea), self._for_resume()) or {})
        except Exception as exc:   # noqa: BLE001 - e.g. no packages left: nothing was built
            rec["used"] = False
            st = self._state([])
            st.update({"action_ok": False,
                       "action_error": str(exc) or "I could not start that build. Try again in a moment."})
            return st
        title = str(proj.get("title") or idea.get("title") or "")
        msg = self._fact("build_started", self.BUILD_STARTED.format(title=title), exact=[title],
                         project=title, takes="a few minutes",
                         then="when it is built they learn it and pass its quiz, then you add it "
                              "to this resume")
        self._append("agent", msg)
        card = {"type": "project_progress", "project_id": str(proj.get("id") or ""), "title": title}
        self.history[-1]["card"] = card
        for t in reversed(self.history):
            c = t.get("card")
            if isinstance(c, dict) and c.get("token") == token:
                c["used"] = True
                c["built"] = key
        self._persist()
        st = self._state([msg], cards=[card])
        st.update({"action_ok": True, "project": proj})
        return st

    @_one_voice
    def add_verified_project(self, entry: dict, title: str = "", score=None, total=None,
                             project_id: str = "") -> dict:
        """A project the person built for this resume passed its quiz (ui/app._on_project_verified,
        on the quiz request's thread, under ``self.lock``): add it (source "built"), rebuild the
        page, and say so in the chat with a card."""
        with self.lock:
            got = self.add_projects([entry], source="built")
            if not got.get("added"):
                return got
            title = title or entry.get("org") or ""
            where = f" for {self.role}" if self.role else ""
            where += f" at {self.company}" if self.company else ""
            score_s = f", {score} of {total}" if score is not None and total else ""
            msg = self._fact("passed_quiz", f"You passed {title}{score_s}. I added it to your resume{where}.",
                             exact=[title], project=title, score=score, out_of=total,
                             added_to_resume=True, rebuilt=self.assembled is not None)
            cut = self._trims_note() if self.assembled is not None else ""
            self._append("agent", msg + (f" {cut}" if cut else ""))
            self.history[-1]["card"] = {"type": "project_progress", "project_id": project_id,
                                        "title": title, "added": True}
            self._persist()
            return got

    def link_project(self, project_id: str, title: str, url: str) -> bool:
        """A project on this resume went public (/publish): its entry gets the link, and the page
        is rebuilt when it shows. Only an entry with no link yet."""
        from ui.repo_rank import same_project as same_title
        if not url:
            return False
        with self.lock:
            hit = False
            bags = [self.essentials.get("projects") or [], (self.profile or {}).get("projects") or []]
            used = getattr(self.assembled, "profile_used", None)
            if isinstance(used, dict):
                bags.append(used.get("projects") or [])
            for bag in bags:
                for e in bag:
                    if not isinstance(e, dict) or e.get("link") or e.get("url"):
                        continue
                    if (project_id and e.get("project_id") == project_id) or \
                            (e.get("source") == "built" and same_title(title, str(e.get("org") or ""))):
                        e["link"] = url
                        hit = True
            if hit:
                if self.assembled is not None:
                    self._assemble()
                self._persist()
            return hit

    ACTION_USED = "That card was already used. Ask me again to see your repositories now."
    ACTION_GONE = "That card is from an earlier chat. Ask me again to check your repositories."

    def apply_action(self, token: str, choice: str, args: dict | None = None) -> dict:
        with self.lock:
            return self._apply_action(token, choice, args)

    @_one_voice
    def _apply_action(self, token: str, choice: str, args: dict | None = None) -> dict:
        """The person's click on a card (POST /api/session/action): the only way a card changes
        the resume. ``choice`` "add" adds the ticked repositories (``args.keys``). A token works
        once; a used or unknown one changes nothing. Returns the session state with
        action_ok and, on success, what was added."""
        args = dict(args or {})
        rec = self.action_tokens.get(str(token or ""))
        if rec is None:
            st = self._state([])
            st.update({"action_ok": False, "action_error": self.ACTION_GONE})
            return st
        if rec.get("used"):
            st = self._state([])
            st.update({"action_ok": False, "action_error": self.ACTION_USED})
            return st
        if choice == "build" and rec.get("kind") == "build":
            return self._apply_build(token, rec, args)
        if choice != "add" or rec.get("kind") != "add_projects":
            st = self._state([])
            st.update({"action_ok": False, "action_error": "I can't do that from this card."})
            return st
        keys = [str(k) for k in args.get("keys") or [] if str(k) in rec["entries"]]
        if not keys:
            st = self._state([])
            st.update({"action_ok": False, "action_error": "Tick at least one repository first."})
            return st
        rec["used"] = True
        got = self.add_projects([rec["entries"][k] for k in keys], source="github")
        added, skipped = got.get("added") or [], got.get("skipped") or []
        msgs = []
        if added:
            msgs.append(self._fact("added_projects", f"I added {', '.join(added)} to this resume"
                                   + (" and your saved profile." if got.get("saved") else "."),
                                   exact=added, added=added, saved_to_profile=bool(got.get("saved")),
                                   rebuilt=self.assembled is not None))
            cut = self._trims_note() if self.assembled is not None else ""
            if cut:
                msgs.append(cut)
        if skipped:
            one = len(skipped) == 1
            msgs.append(self._fact("already_on_resume", f"{', '.join(skipped)} {'was' if one else 'were'} "
                                   f"already on this resume, so I left {'it as it was' if one else 'them as they were'}.",
                                   exact=skipped, already_on_resume=skipped))
        for m in msgs:
            self._append("agent", m)
        # The card in the transcript shows it was used, so a restored chat does not offer it again.
        for t in reversed(self.history):
            for c in [t.get("card")] + list(t.get("cards") or []):
                if isinstance(c, dict) and c.get("token") == token:
                    c["used"] = True
                    c["added"] = list(added)
        self._persist()
        st = self._state(msgs)
        st.update({"action_ok": True, "added": added, "skipped": skipped})
        return st

    @_builds("projects")
    def add_projects(self, entries: list, source: str = "github") -> dict:
        """Add project entries the person chose (from a repo_fit card) to this resume's
        essentials and page, the brain (source ``source``) and the saved profile (section 4b),
        then rebuild the page. An entry already there by name, link or repo is skipped, so
        adding twice changes nothing. Returns {"added": [names], "skipped": [names], "saved"}."""
        from ui.repo_rank import same_project as same_title

        def there(e, lst):
            for p in lst or []:
                if not isinstance(p, dict):
                    continue
                link = str(p.get("link") or p.get("url") or "").rstrip("/").lower()
                if same_title(e["org"], str(p.get("org") or p.get("title") or p.get("name") or "")) \
                        or (e.get("link") and link == e["link"].rstrip("/").lower()) \
                        or (e.get("repo") and p.get("repo") == e["repo"]):
                    return True
            return False

        cur = self.essentials.setdefault("projects", [])
        page = (self.profile or {}).get("projects") if isinstance(self.profile, dict) else None
        added, skipped = [], []
        for e in entries or []:
            if not isinstance(e, dict) or not str(e.get("org") or "").strip():
                continue
            e = dict(e, source=source)
            if there(e, cur) or there(e, page):
                skipped.append(e["org"])
                # The same project is already on the resume (from the saved profile) without its
                # link: it takes the repository's link, so the arrow shows.
                if e.get("link"):
                    self._give_link(e["org"], str(e["link"]))
                continue
            cur.append(dict(e))
            # The page's profile too (before a draft it is the saved row this session writes,
            # and _draft refills its projects from essentials anyway).
            if isinstance(self.profile, dict):
                self.profile.setdefault("projects", []).append(dict(e))
            added.append(e)
        if not added:
            if self._linked_now:
                self._linked_now = False
                if self.assembled is not None:
                    self._assemble()
                self._persist()
            return {"added": [], "skipped": skipped, "saved": False}
        dec = self.essentials.get("declined")
        if isinstance(dec, list) and "projects" in dec:
            self.essentials["declined"] = [d for d in dec if d != "projects"]
        self._brain_projects(added, source)
        if self.assembled is not None:
            self._assemble()
        self._persist()
        saved = self._save_projects_to_profile(added)
        return {"added": [e["org"] for e in added], "skipped": skipped, "saved": saved}

    _linked_now = False

    def _give_link(self, title: str, url: str) -> bool:
        """``url`` as the link of every entry named ``title`` that has none (the essentials, the
        profile and the page). True when one took it."""
        from intake.links import link_ok
        from ui.repo_rank import same_project
        hit = False
        bags = [self.essentials.get("projects") or [], (self.profile or {}).get("projects") or []]
        used = getattr(self.assembled, "profile_used", None)
        if isinstance(used, dict):
            bags.append(used.get("projects") or [])
        for bag in bags:
            for p in bag:
                if isinstance(p, dict) and not link_ok("link", p.get("link") or p.get("url")) \
                        and same_project(str(p.get("org") or p.get("name") or p.get("title") or ""), title):
                    p["link"] = url
                    hit = True
        self._linked_now = self._linked_now or hit
        return hit

    def _brain_projects(self, entries: list, source: str):
        st = self._brain()
        if st is None:
            return
        try:
            from applicant import facts as _F
            from applicant.migrate import _norm
            for e in entries:
                val = {k: e.get(k) for k in ("org", "dates", "link") if e.get(k)}
                _F.put(st, "project", val, source=source, subject=f"project:{_norm(e['org'])}",
                       source_ref=f"{source}:{e.get('repo') or e.get('project_id') or e['org']}")
        except Exception:      # noqa: BLE001 - the brain never breaks the chat
            pass

    def _save_projects_to_profile(self, entries: list) -> bool:
        """The added projects on the saved profile too (section 4b), so the next application
        can reuse them. Only adds; never rewrites what is there."""
        from intake.links import same_title
        try:
            saved = self.memory.load("default") or {}
            prof = dict(saved.get("profile") or {})
            projs = prof.setdefault("projects", [])
            new = [e for e in entries if not any(
                isinstance(p, dict) and same_title(e["org"], str(p.get("org") or p.get("title") or ""))
                for p in projs)]
            if new:
                projs.extend(dict(e) for e in new)
                self.memory.save("default", prof, saved.get("essentials") or {}, saved.get("history") or [])
            return True
        except Exception:      # noqa: BLE001
            return False

    # -- project links: each project's GitHub repository, from the person's own material ---- #
    _UPLOAD_LINKS: dict = {}          # (path, mtime) -> resume_links() of that uploaded file

    def _uploads_dir(self):
        p = str(getattr(self.memory, "db_path", "") or "")
        if not p or p == ":memory:":
            return None
        d = Path(p).parent / "uploads"
        return d if d.is_dir() else None

    def _upload_links(self) -> list[dict]:
        """The links the person's uploaded resumes carry (intake.links.resume_links), newest
        file first: the real GitHub profile behind a "GitHub" anchor and the repository behind a
        project's arrow. Read once per file."""
        d = self._uploads_dir()
        if d is None:
            return []
        from intake.cv_import import pdf_links
        from intake.links import resume_links
        out = []
        files = sorted(d.glob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)[:12]
        for f in files:
            key = (str(f), f.stat().st_mtime)
            got = self._UPLOAD_LINKS.get(key)
            if got is None:
                try:
                    got = resume_links(pdf_links(f))
                except Exception:      # noqa: BLE001 - an unreadable file carries no links
                    got = {"identity": {}, "projects": []}
                self._UPLOAD_LINKS[key] = got
            out.append(got)
        return out

    def _github_login_known(self, uploads=()) -> str:
        """The person's GitHub login from their own material: the link on their resume header
        (profile or what they told the builder), their connected GitHub, or the GitHub link on
        an uploaded resume. "" when none says it."""
        from ui.github_repos import login_from
        ident = dict((self.profile or {}).get("identity") or {})
        ident.update({k: v for k, v in (self.essentials.get("identity") or {}).items() if v})
        login = login_from(ident.get("github") or "")
        if not login:
            login = login_from(str((self._github() or {}).get("login") or ""))
        for got in uploads or ():
            if login:
                break
            login = login_from(str((got.get("identity") or {}).get("github") or ""))
        return login

    def _link_identity(self, uploads=()) -> None:
        """The contact line's profile links ("In | GitHub") when the person's own material has
        them and their profile does not: the LinkedIn and GitHub links on their uploaded resume's
        header, or their connected GitHub account. Saved with the profile. A link they already
        have is never replaced, and each link is filled once only (``links_filled``): one they
        later removed stays removed."""
        from intake.links import link_ok
        from ui.github_repos import login_from
        found: dict = {}
        for got in uploads or ():
            for k in ("linkedin", "github"):
                v = str((got.get("identity") or {}).get(k) or "")
                if v and k not in found and link_ok(k, v):
                    found[k] = v
        if "github" not in found:
            login = login_from(str((self._github() or {}).get("login") or ""))
            if login:
                found["github"] = f"https://github.com/{login}"
        if not found:
            return
        ident_e = self.essentials.setdefault("identity", {})
        ident_p = (self.profile or {}).setdefault("identity", {}) if isinstance(self.profile, dict) else {}
        done = self.essentials.setdefault("links_filled", [])
        for k, v in found.items():
            if k in done or any(link_ok(k, d.get(k)) for d in (ident_e, ident_p)
                                if isinstance(d, dict)):
                continue
            for d in (ident_e, ident_p):
                if isinstance(d, dict):
                    d[k] = v
            done.append(k)

    def _link_projects(self) -> list[str]:
        """Give each project on the resume the link to its GitHub repository (the small blue
        arrow after its name), from the person's own material, and save it so it sticks:
        first the link their uploaded resume carries on that project's row, then their public
        repositories (the login from ``_github_login_known``) matched by name
        (intake.links.repo_matches: the same name, never a guess; two candidates means no
        link). A project that has a link keeps it. Returns the names linked."""
        from intake.links import link_ok, match_repo_links, same_title
        try:
            uploads = self._upload_links()
        except Exception:      # noqa: BLE001
            uploads = []
        self._link_identity(uploads)
        bags = [b for b in (self.essentials.get("projects"), (self.profile or {}).get("projects"))
                if isinstance(b, list)]
        projects = [p for b in bags for p in b if isinstance(p, dict) and not p.get("suggested")]
        need = [p for p in projects if not link_ok("link", p.get("link") or p.get("url"))]
        if not need:
            return []
        linked: list[str] = []

        def name(p):
            return str(p.get("org") or p.get("name") or p.get("title") or "")

        def give(target: dict, url: str):
            for p in need:
                if p is target or same_title(name(p), name(target)):
                    if not link_ok("link", p.get("link") or p.get("url")):
                        p["link"] = url
                        if name(p) not in linked:
                            linked.append(name(p))

        from ui.repo_rank import same_project
        for got in uploads:
            for row in got.get("projects") or []:
                url = str(row.get("link") or "")
                if "github.com/" not in url.lower():
                    continue
                for p in list(need):
                    if not link_ok("link", p.get("link")) and same_project(name(p), row.get("title")):
                        give(p, url)
        need = [p for p in need if not link_ok("link", p.get("link") or p.get("url"))]
        if need and os.environ.get("GITHUB_LINK_FETCH", "1") != "0":
            login = self._github_login_known(uploads)
            if login:
                from ui import github_repos
                got = github_repos.list_repos(login, token=self._token())
                if got.get("ok"):
                    for p, url in match_repo_links(need, got.get("repos") or []):
                        give(p, url)
        return linked

    # -- checking GitHub from a button, and carrying on once it is connected -------------- #
    @staticmethod
    def _is_check_github(message: str) -> bool:
        from agent.resume_chat import CHECK_GITHUB_SEND
        norm = lambda t: re.sub(r"[^a-z ]", "", str(t or "").lower()).strip()   # noqa: E731
        return norm(message) == norm(CHECK_GITHUB_SEND)     # the button's own line only

    def _check_github(self, connected_as: str = ""):
        """Rank the person's repositories for this job and show the repo_fit card: the offer's
        "Check my GitHub" button, and the turn that carries on by itself once GitHub is
        connected (``connected_as``, the login). Not connected and no username: one line and
        the Connect GitHub button, never steps."""
        from agent.resume_chat import ADD_LABEL, _t_rank_repos, connect_card, merge_cards
        gh = self._github_view()
        login = (gh.get("login") if gh.get("connected") else "") or gh.get("resume_login") or ""
        msgs: list[str] = []
        if connected_as:
            where = f"this {self.role} role" if self.role else "this job"
            lead = self._fact("github_connected", f"Connected as {connected_as}. Checking your "
                              f"repos for {where}.", exact=[connected_as], login=connected_as,
                              doing="checking their repositories for this job")
            msgs.append(lead)
            self._append("agent", lead)
        if not login:
            self._fact("github_not_connected", self.GITHUB_NOT_CONNECTED,
                       github="not connected and no username known",
                       point_to="the Connect GitHub button below; once connected you check their "
                                "repos for this job by yourself")
            msgs.append(self.GITHUB_NOT_CONNECTED)
            self._append("agent", self.GITHUB_NOT_CONNECTED)
            card = connect_card()
            self.history[-1]["card"] = card
            self._persist()
            return self._state(msgs, cards=[card])
        try:
            out = _t_rank_repos(self._agent_ctx(), {"login": login})
        except Exception:      # noqa: BLE001 - one polite line, never a trace
            out = {"ok": False}
        cards = merge_cards([out.pop("card")]) if out.get("card") else []
        text = self._rank_takeaway(out)
        fits = [f for f in out.get("fits") or [] if isinstance(f, dict) and f.get("name")]
        self._fact("repo_fit", text, exact=self._rank_best(fits),
                   ok=bool(out.get("ok")), best_fits=self._rank_best(fits) or None,
                   fits=[{"name": f["name"], "fit": f.get("fit"), "shows": f.get("skills_it_shows")}
                         for f in fits[:5]] or None,
                   none_fit=bool(out.get("ok") and not fits),
                   how_judged="names, descriptions and READMEs, not the code",
                   card_below=("a card lists every repo; they tick the ones they want and click "
                               "the card's button" if cards else None),
                   button_on_card=ADD_LABEL if fits else None)
        msgs.append(text)
        self._append("agent", text)
        if cards:
            self.history[-1]["card"] = cards[0]
        self._agent_spoke = True       # a follow-up question stays with the agent
        self._persist()
        return self._state(msgs, cards=cards)

    @staticmethod
    def _rank_best(fits: list) -> list:
        best = [f["name"] for f in fits if f.get("fit") == "strong fit"] or [f["name"] for f in fits]
        return best[:3]

    @staticmethod
    def _rank_takeaway(out: dict) -> str:
        """One or two plain lines over the repo_fit card (the card lists every repo)."""
        from agent.resume_chat import ADD_LABEL
        if not out.get("ok"):
            return "I couldn't read your repos just now. Try again in a moment."
        fits = [f for f in out.get("fits") or [] if isinstance(f, dict) and f.get("name")]
        if not fits:
            return ("None of your repos shows this job's skills yet. I can help you build a project "
                    "for this job instead.")
        best = [f["name"] for f in fits if f.get("fit") == "strong fit"] or [f["name"] for f in fits]
        best = best[:3]
        verb = "fits" if len(best) == 1 else "fit"
        return (f"{_and_list(best)} {verb} this job best. Tick the ones you want on the card and "
                f"click {ADD_LABEL}.")

    # -- "add 2 github projects" (Kofi, 2026-11-09) ------------------------------------------ #
    _github_add_due = 0     # an add asked for while GitHub was not connected: N, carried on after
    _NUM_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}

    def _github_add_login(self, login: str = "") -> str:
        gh = self._github_view()
        return login or (gh.get("login") if gh.get("connected") else "") or gh.get("resume_login") or ""

    def _add_top(self, n: int, login: str = "") -> dict:
        """Rank the person's repositories for this job and add the top ``n`` that fit (strong
        or some fit, never none or unknown; not already on the resume), through add_projects:
        the same entries and the same add as ticking them on the repo_fit card and clicking its
        button, then the page is rebuilt (once built). Returns the agent tool's result: what was
        added and why, ``say`` (the reply), ``fewer``, and the repo_fit card with a fresh token
        for the fits still to add. No GitHub and no username: the connect card, and the add is
        remembered so connecting carries on with it."""
        from agent.resume_chat import _no_github, repo_fit_card
        from ui.repo_rank import entry_for
        login = self._github_add_login(login)
        if not login:
            self._github_add_due = n
            return {**_no_github(), "changed": False}
        try:
            got = self._rank_for_job(login)
        except Exception:      # noqa: BLE001 - one polite line, never a trace
            got = {"ok": False}
        if not got.get("ok"):
            return {"ok": False, "changed": False, "error": str(got.get("error") or ""),
                    "say": "I couldn't read your repos just now. Try again in a moment."}
        repos = [r for r in got.get("repos") or [] if isinstance(r, dict)]
        fits = [r for r in repos if r.get("fit") in ("strong", "some")]
        already = [r.get("name") for r in fits if r.get("on_resume")]
        pick = [r for r in fits if not r.get("on_resume")][:n]
        res = self.add_projects([entry_for(r) for r in pick], source="github") if pick else {}
        names = set(res.get("added") or [])
        chosen = [r for r in pick if r.get("name") in names]
        for r in repos:                 # the card shows the new ones as on the resume
            r["on_resume"] = self._on_resume(r)
        rest = [r for r in fits if not r.get("on_resume")]
        token = self._offer(rest) if rest else ""
        say = self._added_line(n, chosen, already)
        return {"ok": True, "changed": bool(chosen), "asked_for": n,
                "added": [{"name": r.get("name"), "fit": r.get("fit"), "skills_it_shows": list(r.get("terms") or [])[:4],
                           "why": r.get("why") or ""} for r in chosen],
                "fewer_fit_than_asked": len(chosen) < n, "already_on_resume": already,
                "say": say, "card": repo_fit_card(got, token)}

    def _added_line(self, n: int, chosen: list, already: list) -> str:
        """One or two plain lines: which repositories were added and why (the job skills they
        show), and honestly when fewer fit than were asked for."""
        names = [str(r.get("name")) for r in chosen]
        terms: list[str] = []
        for r in chosen:
            for t in r.get("terms") or []:
                if str(t).lower() not in {x.lower() for x in terms}:
                    terms.append(str(t))
        one = len(chosen) == 1
        why = (f" {'It shows' if one else 'They show'} this job's {_and_list(terms[:4])}." if terms else "")
        on = (f" {_and_list(already)} {'is' if len(already) == 1 else 'are'} already on it."
              if already else "")
        where = "and rebuilt your resume" if self.assembled is not None else "to this resume"
        if not chosen:
            if already:
                return (f"The repos that fit this job are already on your resume.{on} I can help you "
                        "build a project for this job for more.")
            return ("None of your repos shows this job's skills yet, so I didn't add any. I can help "
                    "you build a project for this job instead.")
        if len(chosen) < n:
            k = n - len(chosen)
            more = " more" if already else ""
            count = self._NUM_WORDS.get(len(chosen), str(len(chosen)))
            other = "the other one" if k == 1 else f"the other {self._NUM_WORDS.get(k, str(k))}"
            line = (f"Only {count}{more} of your repos {'fits' if one else 'fit'} this job, so I added "
                    f"{_and_list(names)} {where}.{why}{on} I can help you build a project for this "
                    f"job for {other}.")
        else:
            line = f"I added {_and_list(names)} from your GitHub {where}.{why}{on}"
        cut = self._trims_note() if self.assembled is not None else ""
        return line + (f" {cut}" if cut else "")

    BUILD_ONE_SEND = "Suggest a project I could build for this job"

    def _add_from_github(self, n: int, connected_as: str = ""):
        """The "add N github projects" turn, decided without a model: _add_top, then its line
        and the repo_fit card (so the person can add others). Fewer fit than asked: a button to
        get project ideas for this job. Not connected and no username: one line and the Connect
        GitHub button, and connecting carries on with this same add."""
        from agent.resume_chat import connect_card, merge_cards
        msgs: list[str] = []
        if connected_as:
            what = "repo" if n == 1 else f"{self._NUM_WORDS.get(n, str(n))} repos"
            where = f"this {self.role} role" if self.role else "this job"
            lead = self._fact("github_connected", f"Connected as {connected_as}. Adding the {what} "
                              f"that fit {where} best.", exact=[connected_as], login=connected_as,
                              doing=f"adding the {what} that fit this job best")
            msgs.append(lead)
            self._append("agent", lead)
        out = self._add_top(n)
        if out.get("github_connected") is False:
            line = ("GitHub isn't connected yet. Connect it with the button below and I'll add the "
                    + ("repo" if n == 1 else f"{self._NUM_WORDS.get(n, str(n))} repos")
                    + " that fit this job best.")
            card = dict(connect_card(), add=n)
            self._fact("github_not_connected", line, github="not connected", asked_to_add=n,
                       point_to="the Connect GitHub button below; once connected you add the "
                                "repos that fit this job best by yourself")
            msgs.append(line)
            self._append("agent", line)
            self.history[-1]["card"] = card
            self._persist()
            return self._state(msgs, cards=[card])
        raw = [out.pop("card")] if out.get("card") else []
        if out.get("ok") and (out.get("fewer_fit_than_asked") or not out.get("added")):
            raw.append({"type": "buttons", "buttons": [
                {"label": "Build a project for this job", "send": self.BUILD_ONE_SEND}]})
        cards = merge_cards(raw)
        text = str(out.get("say") or "")
        added = [a.get("name") for a in out.get("added") or [] if a.get("name")]
        self._fact("added_from_github", text, exact=added, asked_for=n, added=out.get("added") or None,
                   fewer_fit_than_asked=bool(out.get("fewer_fit_than_asked")),
                   already_on_resume=out.get("already_on_resume") or None,
                   rebuilt=bool(added and self.assembled is not None),
                   cut_to_fit=len(getattr(self.assembled, "trims", None) or []) if added else None,
                   offer=("help building a project for this job for the rest"
                          if out.get("fewer_fit_than_asked") or not added else None),
                   failed=None if out.get("ok") else "could not read their repos just now")
        msgs.append(text)
        self._append("agent", text)
        if cards:
            self.history[-1]["card"] = cards[0]
            if len(cards) > 1:
                self.history[-1]["cards"] = cards
        self._agent_spoke = True       # a follow-up question stays with the agent
        self._persist()
        st = self._state(msgs, cards=cards)
        st["added"] = [a["name"] for a in out.get("added") or []]
        return st

    def _connect_add_count(self) -> int:
        """N from the latest live Connect GitHub card that an "add N github projects" left."""
        for t in reversed(self.history):
            if not isinstance(t, dict):
                continue
            for c in [t.get("card")] + list(t.get("cards") or []):
                if self._connect_buttons(c):        # the latest one only
                    try:
                        return max(0, int(c.get("add") or 0))
                    except (TypeError, ValueError):
                        return 0
        return 0

    @_one_voice
    def github_connected(self, pending: bool = False) -> dict:
        """GitHub just became connected (the one-click flow, the device flow, a pasted token, in
        Settings or from the chat): when the chat's last word on GitHub was "not connected" (a
        Connect GitHub button still live), mark those buttons connected and carry on by itself,
        "Connected as <login>. Checking your repos for this <role> role." and the repo_fit card.
        No turn from the person is added. Once per connect: a second call finds no live button
        and changes nothing. ``pending``: the person clicked "Check my GitHub" on a card from
        before it was connected, and that click started the connect. Returns the state with
        ``resumed``."""
        with self.lock:
            gh = self._github_view()
            if not gh.get("connected"):
                st = self._state([])
                st["resumed"] = False
                return st
            login = str(gh.get("login") or gh.get("resume_login") or "")
            add_n = self._connect_add_count()
            due = self._mark_connect_cards(login or "GitHub")
            if pending:
                due = True
            if due is None:
                st = self._state([])
                st["resumed"] = False
                return st
            self._github_add_due = 0     # the card says it (a restored chat too)
            if not due or not self._agent_on():
                self._persist()
                st = self._state([])
                st.update({"resumed": False, "github_login": login})
                return st
            if add_n:       # "add 2 github projects" asked before it was connected: do just that
                st = self._add_from_github(add_n, connected_as=login or "GitHub")
                st.update({"resumed": True, "github_login": login})
                return st
            st = self._check_github(connected_as=login or "GitHub")
            st.update({"resumed": True, "github_login": login})
            return st

    @staticmethod
    def _connect_buttons(card) -> bool:
        return isinstance(card, dict) and card.get("type") == "buttons" and not card.get("connected") \
            and any(isinstance(b, dict) and b.get("nav") == "github" for b in card.get("buttons") or [])

    def _mark_connect_cards(self, login: str):
        """Mark every live Connect GitHub card in the transcript connected (a restored chat
        shows them done). None: there was none. True: the last one is the chat's latest word on
        GitHub (no repo_fit card after it), so the chat should carry on; False: it was already
        answered another way."""
        last = None
        for i, t in enumerate(self.history):
            if not isinstance(t, dict):
                continue
            for c in [t.get("card")] + list(t.get("cards") or []):
                if self._connect_buttons(c):
                    c["connected"] = login
                    last = i
        if last is None:
            return None
        later = [c for t in self.history[last + 1:] if isinstance(t, dict)
                 for c in [t.get("card")] + list(t.get("cards") or [])]
        return not any(isinstance(c, dict) and c.get("type") == "repo_fit" for c in later)

    def _agent_reply(self, message: str):
        from agent.resume_chat import run_turn
        self._github_add_due = 0       # the latest request decides what a connect carries on with
        try:
            out = run_turn(self, self._agent_ctx(), message)
        except Exception:      # noqa: BLE001 - the agent failing falls back to today's reply
            return self._converse(message)
        text = self._model_said(str(out.get("text") or "").strip()) or self._fact("ack", "Noted.")
        cards = list(out.get("cards") or [])
        self._agent_spoke = True       # a follow-up question stays with the agent (_submit)
        self._append("agent", text)
        if self._github_add_due:       # add_github_projects met no GitHub: the connect carries on
            for c in cards:
                if self._connect_buttons(c):
                    c["add"] = self._github_add_due
        if cards:
            self.history[-1]["card"] = cards[0]
            if len(cards) > 1:
                self.history[-1]["cards"] = cards
        self._persist()
        return self._state([text], cards=cards)

    # -- what the app can do (2026-11-06) ----------------------------------------------- #
    # Kofi uploaded his resume, then asked "didn't you see the projects that were on the resume
    # i uploaded? or can't you check my github", and the chat answered "I can't access files
    # or GitHub directly, only what you paste into our chat": false on both counts. The reply
    # model now gets the app's true facts, and a reply that still denies them is replaced by
    # one written from what the app really holds.
    # Not connected: one line, and a Connect GitHub button on the card (agent/resume_chat.py
    # connect_card). Never menu paths in the chat (Kofi, test build 13, 2026-11-09).
    GITHUB_NOT_CONNECTED = ("GitHub isn't connected yet. Connect it with the button below and I'll "
                            "check your repos for this job.")
    _ASKS_FILES = re.compile(r"\b(upload(?:ed|s|ing)?|attach(?:ed|ment)?|the file|my file|that file|"
                             r"pdf|github|repos?|repositor(?:y|ies))\b", re.I)
    _ASKS_SEEN = re.compile(r"\b(see|saw|seen|read|notice\w*|miss\w*|pick\w* up|pull\w*|includ\w*|"
                            r"happened|find|found|got|check\w*|look\w*)\b", re.I)
    _ASKS_ITEMS = re.compile(r"\b(projects?|links?|linkedin)\b", re.I)
    _QUESTION = re.compile(r"\?|\b(did(?:n['’]?t| not)?|can(?:['’]?t|not)?|could(?:n['’]?t)?|"
                           r"do(?:n['’]?t)?|does(?:n['’]?t)?|why|have(?:n['’]?t)?|were(?:n['’]?t)?|"
                           r"was(?:n['’]?t)?|where|what happened|are you able|is it possible)\b", re.I)
    _DENIES = re.compile(
        r"(?:can(?:no|['’])?t|cannot|can not|unable to|not able to|no (?:way|ability) to|"
        r"(?:do|does)(?: not|n['’]t) have (?:the ability|access))\b[^.!?\n]{0,60}?"
        r"\b(?:access|see|read|open|view|check|look at|browse|reach)\b[^.!?\n]{0,60}?"
        r"\b(?:files?|uploads?|attach\w*|pdfs?|documents?|resumes?|github|repos?|repositor\w*)"
        r"|only (?:see |read |work with |use )?what you (?:paste|type|share)", re.I)

    def _asks_about_upload(self, message: str) -> bool:
        """A question to the chat about a file it was given, its projects or links, or the
        person's GitHub ("didn't you see the projects on the resume I uploaded?", "can't you
        check my github?"). Answered in conversation from what the app holds, never fed to the
        build funnel. A message carrying a link is material to add, not a question."""
        m = message or ""
        if re.search(r"https?://|www\.|\b[\w-]+\.(?:com|io|dev|app|org)/", m, re.I):
            return False
        if not (re.search(r"\byou\b", m, re.I) or "?" in m) or not self._QUESTION.search(m):
            return False
        if self._ASKS_FILES.search(m):
            return True
        return bool(self.uploads and self._ASKS_SEEN.search(m) and self._ASKS_ITEMS.search(m))

    def _github(self) -> dict | None:
        """{"connected", "login"} from the app's hook, or None when this session has none."""
        try:
            got = self.github_status() if callable(self.github_status) else None
        except Exception:      # noqa: BLE001 - an unknown status is said as unknown
            got = None
        return got if isinstance(got, dict) else None

    def _shown_projects(self) -> list[str]:
        used = getattr(self.assembled, "profile_used", None) or self.profile or {}
        return [str(p.get("org") or p.get("name") or "").strip() for p in (used.get("projects") or [])
                if isinstance(p, dict) and str(p.get("org") or p.get("name") or "").strip()]

    def _app_context(self) -> str:
        """The app's true, short statement of what it can do and what it holds right now, for
        the reply model (``llm.converse``): this session's uploads and what was read from them,
        the projects on the resume, the GitHub connection and where to make it, and the
        Projects page."""
        lines = ["- This chat has an upload button. The app reads any file the person uploads "
                 "(PDF, Word, PowerPoint or a photo) and gives you what it read."]
        for u in self.uploads:
            bits = []
            if u.get("roles"):
                bits.append("roles: " + "; ".join(u["roles"][:6]))
            if u.get("projects"):
                bits.append("projects: " + "; ".join(
                    p + (f" ({u['project_links'][p]})" if u.get("project_links", {}).get(p) else "")
                    for p in u["projects"]))
            elif u.get("kind") == "resume":
                bits.append("projects: none found in the file")
            links = u.get("links") or {}
            if links:
                bits.append("links: " + ", ".join(
                    f"{_LINK_NAMES.get(k, k)} {v}" for k, v in links.items()))
            lines.append(f"- Uploaded {u.get('name')} ({u.get('kind')}) in this chat. Read from it: "
                         + ("; ".join(bits) if bits else "nothing usable") + ".")
        if not self.uploads:
            lines.append("- No file has been uploaded in this chat yet.")
        shown = self._shown_projects()
        if self.assembled is not None or self.profile.get("projects"):
            lines.append("- Projects on the resume being built now: "
                         + (_and_list(shown) if shown else "none") + ".")
        gh = self._github()
        mine = str((self.essentials.get("identity") or {}).get("github") or "").strip()
        if gh and gh.get("connected"):
            who = f" as {gh['login']}" if gh.get("login") else ""
            lines.append(f"- GitHub is connected{who} (Settings, Connections).")
        elif gh is not None:
            lines.append("- GitHub is NOT connected. The chat shows a Connect GitHub button for it. "
                         "Never describe menus, settings or steps to connect.")
        if mine:
            lines.append(f"- Their GitHub profile link: {mine}.")
        lines.append("- This chat cannot list or browse all of the person's GitHub repositories. "
                     "It adds a project when they paste the repository link or describe it.")
        lines.append("- The Projects page (Projects in the left sidebar) can build a project for "
                     "this job with them, then check they can explain it with a short quiz "
                     "(Defend it) before it goes on the resume.")
        return "\n".join(lines)

    def _capability_answer(self, message: str) -> str:
        """A plain answer written from what the app really holds, for when the model's reply
        denied a capability the app has. Never claims what did not happen."""
        m = message or ""
        out = []
        resumes = [u for u in self.uploads if u.get("kind") == "resume"]
        if re.search(r"\b(upload\w*|attach\w*|file|pdf|resume|projects?|links?|linkedin)\b", m, re.I):
            if resumes:
                u = resumes[-1]
                found = []
                if u.get("projects"):
                    found.append(("your projects " if len(u["projects"]) > 1 else "your project ")
                                 + _and_list(u["projects"]))
                names = [_LINK_NAMES.get(k, k) for k in (u.get("links") or {})]
                if names:
                    found.append(f"your {_and_list(names)} link" + ("s" if len(names) > 1 else ""))
                out.append(f"Yes, I read {u.get('name')}" + (f" and found {' and '.join(found)}."
                                                              if found else "."))
                shown = self._shown_projects()
                from intake.links import same_title
                on = [p for p in u.get("projects") or [] if any(same_title(p, x) for x in shown)]
                if on:
                    out.append(f"{_and_list(on)} {'is' if len(on) == 1 else 'are'} on this resume now.")
                elif u.get("projects"):
                    out.append("They are saved to your profile.")
            elif self.uploads:
                out.append(f"I read {self.uploads[-1].get('name')}.")
            else:
                out.append("I don't have a file from you in this chat yet. Use the upload button "
                           "here and I'll read it.")
        if re.search(r"\b(github|repos?|repositor\w*)\b", m, re.I):
            gh = self._github()
            if gh and gh.get("connected"):
                who = f" as {gh['login']}" if gh.get("login") else ""
                out.append(f"Your GitHub is connected{who}. I can't list all your repositories from "
                           "this chat yet, so paste a repository link or tell me about a project and "
                           "I'll add it.")
            else:
                out.append(self.GITHUB_NOT_CONNECTED + " Or paste a repository link and I'll add it.")
                self._connect_due = True
            out.append("The Projects page can also build a project for this job with you and check "
                       "you can explain it with a short quiz before it goes on your resume.")
        return " ".join(out)

    def _no_false_limits(self, reply: str, message: str) -> str:
        """Replace a reply that claims the app cannot read files or GitHub (when it can, or
        when the honest answer is "not connected yet, here is where") with one written from
        the app's own state. Then the GitHub rule (_no_github_ask)."""
        hit = self._DENIES.search(reply or "")
        if hit:
            fixed = self._capability_answer(f"{message} {hit.group(0)}")
            if fixed:
                return fixed
        return self._no_github_ask(reply)

    # The intake model asked "if you have a LinkedIn or GitHub address, send it over" (Kofi's
    # D365 chat, 2026-11-10). GitHub is never typed into the chat: connected, it is never asked
    # for; not connected, the Connect GitHub button does it.
    _GITHUB_WORD = re.compile(r"\bgit\s?hub\b", re.I)
    _ASKS_FOR = re.compile(
        r"\b(?:send|share|give|paste|provide|drop|post|add|include|tell me|let me know|"
        r"what(?:'s|’s| is) your|do you have|if you have|got an?|have an?)\b", re.I)
    _GITHUB_IN_LIST = re.compile(
        r"\s*(?:,|\bor\b|\band\b|/)\s*(?:your\s+)?git\s?hub(?:\s+(?:link|address|url|profile|"
        r"username|handle|page))?\b|\bgit\s?hub(?:\s+(?:link|address|url|profile|"
        r"username|handle|page))?\s*(?:,|\bor\b|\band\b|/)\s*", re.I)
    _github_card_due = False

    def _no_github_ask(self, reply: str) -> str:
        """Take any ask for the person's GitHub out of a model reply: GitHub alone, the
        sentence goes; GitHub beside LinkedIn or a website, just GitHub goes. When GitHub is
        not connected and no username is known, the turn gets the Connect GitHub button
        (_state). A sentence that only mentions GitHub stays."""
        if not reply or not self._GITHUB_WORD.search(reply):
            return reply
        kept, dropped = [], False
        for sent in re.split(r"(?<=[.!?])\s+", reply):
            if self._GITHUB_WORD.search(sent) and self._ASKS_FOR.search(sent):
                rest = self._GITHUB_IN_LIST.sub(" ", sent)
                rest = re.sub(r"\s+([,.!?])", r"\1", re.sub(r"\s{2,}", " ", rest)).strip()
                dropped = True
                if not self._GITHUB_WORD.search(rest) and re.search(
                        r"\b(?:linkedin|website|portfolio|blog)\b", rest, re.I):
                    kept.append(rest)
                continue
            kept.append(sent)
        if not dropped:
            return reply
        out = " ".join(kept).strip()
        gh = self._github_view()
        if not (gh.get("connected") or gh.get("resume_login")):
            # The button goes under this reply only if the reply is really said (_state): a
            # reply the turn throws away (a build that asks something else first) brings none.
            self._github_card_due = out or True
        return out

    def _apply_memory_selection(self):
        """Recall -> decide (P1, Task 3). Record which of the person's experiences/projects/skills
        earn a place on the CV for THIS job and briefly why, surfaced in state so the person sees
        the reasoning and can override. Constraints ('do not mention X') are NOT applied here:
        they are enforced non-destructively at RENDER time (see _render_profile), so the durable
        saved profile is NEVER mutated and an exclusion is reversible by retracting the fact."""
        try:
            prefs = self._preferences()
        except Exception:
            prefs = []
        prefs = list(prefs) + self._choice_notes()   # so its "why" agrees with what code shows
        try:
            self.cv_selection = self.llm.select_for_cv(self.jd, self.profile, self.recalled, prefs) or {}
        except Exception:
            self.cv_selection = {"selected": [], "excluded": [], "summary": ""}

    def _avoid_token_sets(self) -> list:
        """Distinctive-token sets for each ACTIVE 'do not mention' constraint (a year, or a word
        >= 4 chars). Read fresh every call, so retracting a constraint takes effect on the next
        build with no lingering state."""
        try:
            prefs = self._preferences()
        except Exception:
            return []
        avoid = []
        for p in prefs:
            if p.get("type") != "constraint":
                continue
            val = str(p.get("value", "")).lower()
            if not re.search(r"do ?n['’]?t mention|do not mention|avoid|leave (?:off|out)|omit", val):
                continue
            m = re.search(r"(?:mention|avoid|omit|off|out)\s+(?:the\s+)?(.+)", val)
            toks = {t for t in re.findall(r"[a-z0-9]+", m.group(1) if m else val)
                    if (t.isdigit() and len(t) == 4) or len(t) >= 4}
            if toks:
                avoid.append(toks)
        return avoid

    @staticmethod
    def _item_blocked(item: dict, avoid: list) -> bool:
        # Match against the whole item, including role-nested title/dates/bullets (experiences are
        # {org, location, roles:[{title, dates, bullets}]}) and flat project shapes alike.
        parts = [str(item.get(k) or "") for k in ("org", "company", "title", "dates")]
        parts += [str(b) for b in (item.get("bullets") or [])]
        for r in (item.get("roles") or []):
            parts += [str(r.get(k) or "") for k in ("title", "dates")]
            parts += [str(b) for b in (r.get("bullets") or [])]
        itoks = set(re.findall(r"[a-z0-9]+", " ".join(parts).lower()))
        return any(a & itoks for a in avoid)

    def _render_profile(self) -> dict:
        """A deep COPY of the profile with items an active 'do not mention' constraint forbids
        removed, for assembly / render ONLY. self.profile (the durable, persisted material) is
        never mutated, so an excluded item stays saved and reappears once the constraint is
        retracted. With no active constraints this is just a copy (behavior unchanged)."""
        import copy
        prof = copy.deepcopy(self.profile)
        avoid = self._avoid_token_sets()
        if not avoid:
            return prof
        for section in ("experience", "projects"):
            prof[section] = [it for it in (prof.get(section) or [])
                             if not self._item_blocked(it, avoid)]
        return prof

    def _restore_hidden(self, used: dict) -> dict:
        """After assembling from the render copy, re-attach the items a constraint hid (from the
        COMPLETE self.profile) so the profile we keep and PERSIST stays whole. The rendered CV in
        self.assembled already excludes them; only the durable profile is made complete again."""
        avoid = self._avoid_token_sets()
        if not avoid:
            return used
        used = dict(used or {})
        for section in ("experience", "projects"):
            hidden = [it for it in (self.profile.get(section) or [])
                      if self._item_blocked(it, avoid)]
            if hidden:
                used[section] = list(used.get(section) or []) + hidden
        return used

    @staticmethod
    def _role_key(org: str, title: str) -> tuple:
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()
        return (norm(org), norm(title))

    def _whole_profile_after_render(self, used: dict) -> dict:
        """Take the render's tailored WORDING without letting its TRIMMING stick.

        A render is a one-page artifact: it drops roles the job doesn't need and shortens
        bullets until the page fits. Assigning that result back to the durable profile
        made those cuts permanent -- every build quietly ate the person's history, so a
        role rendered with one bullet came back with one bullet forever, and the next
        job's tailoring had less to work with than the last. Across a batch of
        applications the profile erodes to nothing.

        So: keep every role and every bullet the profile already had, and adopt the
        tailored text only where the render actually produced a replacement.
        """
        rendered = self._restore_hidden(used)          # constraint-hidden items first
        master = self.profile or {}
        # A skill the page's capped Skills block left off stays in their saved skills.
        self._keep_dropped_skills(master.get("skills"), rendered.get("skills"))
        if not master.get("experience"):
            return rendered
        if not rendered.get("experience"):
            # A render that lost the whole section is a render bug, never a reason to
            # erase the person's history: keep the durable material as it was.
            whole = dict(rendered)
            whole["experience"] = master["experience"]
            return whole

        def _roles_of(entry: dict) -> list:
            return entry["roles"] if isinstance(entry.get("roles"), list) else [entry]

        tailored: dict[tuple, list] = {}
        for entry in rendered.get("experience") or []:
            for role in _roles_of(entry):
                key = self._role_key(entry.get("org"), role.get("title"))
                tailored[key] = list(role.get("bullets") or [])

        origins = getattr(self.assembled, "origins", None)

        def _norm(t) -> str:
            return re.sub(r"\s+", " ", str(t or "")).strip().lower()

        def _adopt(entry: dict, role: dict) -> dict:
            role = dict(role)
            original = list(role.get("bullets") or [])
            new = tailored.get(self._role_key(entry.get("org"), role.get("title")))
            if new and isinstance(origins, dict):
                # The page's bullets, as tailored (a split's parts and a bullet written for the
                # page included), then every bullet of theirs the page did not show. Matched by
                # where each page bullet came from, never by position: a page that left out the
                # third of four bullets used to save the fourth twice and lose the third
                # (Stanbic, 2026-11-10).
                shown = {_norm(origins.get(b, b)) for b in new}
                role["bullets"] = list(new) + [o for o in original if _norm(o) not in shown]
            elif new:
                # Tailored bullets replace the originals one for one; anything the
                # render trimmed off the end is carried over untouched.
                role["bullets"] = new + original[len(new):]
            return role

        merged = []
        for entry in master.get("experience") or []:
            if isinstance(entry.get("roles"), list):
                entry = dict(entry)
                entry["roles"] = [_adopt(entry, r) for r in entry["roles"]]
                merged.append(entry)
            else:
                merged.append(_adopt(entry, entry))   # flat entries stay flat

        whole = dict(rendered)
        whole["experience"] = merged
        return self._restore_choice_projects(whole)

    def _restore_choice_projects(self, whole: dict) -> dict:
        """A project the person's choices left off this page stays in the durable profile, in
        its own place. With no choices this changes nothing."""
        ex = ((getattr(self.assembled, "choices_report", None) or {}).get("excluded") or {})
        if not any(str(k).startswith("project:") for k in ex):
            return whole
        from applicant.rules import project_id
        used = {project_id(p): p for p in (whole.get("projects") or []) if isinstance(p, dict)}
        out = []
        for p in (self.profile or {}).get("projects") or []:
            if not isinstance(p, dict):
                continue
            pid = project_id(p)
            if pid in used:
                out.append(used[pid])
            elif pid in ex:
                out.append(p)
        whole = dict(whole)
        whole["projects"] = out
        return whole

    # ------------------------------------------------------------------ choices (brain, Phase 5)
    # "Show one of each depending on where I am applying" (docs/applicant-brain.md, sections 6
    # and 8). The person's saved choices for this kind of job decide which of their own roles
    # and projects this page shows; a switch on the builder's edit page changes this resume, and
    # ONE line then asks whether to keep the change for similar jobs. Nothing is learned without
    # a tap, and a choice never adds words. With no choices every path below is a no-op.
    def _rules_store(self):
        """The brain the person's choices live in: beside this session's memory file. Read for
        every build of this person (the unattended runs build under a scratch name for the same
        person); written only from the person's own taps."""
        p = str(getattr(self.memory, "db_path", "") or "")
        if not p or p == ":memory:":
            return None
        d = Path(p).parent
        if not (d / "applicant.db").exists():
            return None
        try:
            from applicant import migrate as _bm
            return _bm.ensure(d)
        except Exception:      # noqa: BLE001 - choices never break a build
            return None

    def _choice_notes(self) -> list:
        """The saved choices that fit this job, as plain sentences for ``select_for_cv`` (it
        explains the page; code decides it). Empty with no choices."""
        st = self._rules_store()
        if st is None:
            return []
        try:
            from applicant import rules as _R
            from applicant import scope as _Sc
            rules = _R.list_rules(st, status=_R.ACTIVE)
            if not rules:
                return []
            its = self._choice_items()
            return [{"type": "preference", "key": f"choice_{r['id']}", "value": _R.sentence(r, its)}
                    for r in _Sc.applicable(rules, self._job_labels(rules))]
        except Exception:      # noqa: BLE001
            return []

    def _new_build(self):
        """A fresh build of this resume: the one-line asks may come once more, and each project
        gets its GitHub link when the person's own material names the repository."""
        self._choice_asked = False
        self.choice_ask = None
        self.overlap_ask = None
        try:
            self._link_projects()
        except Exception:      # noqa: BLE001 - a link is a nicety, never a reason to fail a build
            pass

    def _choice_items(self) -> list:
        from applicant import rules as _R
        return _R.items(self.profile or {})

    def _job_labels(self, rules=None) -> dict:
        """This job's family, industry and company type (applicant/scope.py), worked out once per
        resume. The small model call runs only when a saved choice needs a label nothing else
        settled."""
        need = {r.get("scope_kind") for r in (rules or [])}
        cached = self._labels
        if cached is not None and not (("company_type" in need and cached.get("company_type") == "unknown"
                                        and not cached.get("_typed"))
                                       or ("industry" in need and not cached.get("industry")
                                           and not cached.get("_typed"))):
            return cached
        from applicant import scope as _Sc
        st = self._rules_store()
        lab = _Sc.labels(st, {"title": self.role, "company": self.company, "jd": self.jd},
                         sponsors=_Sc.sponsor_source(), llm=self.llm, rules=rules)
        if {"company_type", "industry"} & need:
            lab["_typed"] = True             # the model was asked (or need not be): ask once
        self._labels = lab
        return lab

    def _choices_plan(self):
        """The plan ``assemble_cv`` applies: the saved choices that fit this job, plus this
        resume's own switches. None when there are none (the page is exactly the scorer's)."""
        from applicant import rules as _R
        st = self._rules_store()
        its = self._choice_items()
        rules = []
        if st is not None:
            try:
                if its:
                    _R.sync_items(st, its)
                rules = _R.list_rules(st, status=_R.ACTIVE)
            except Exception:  # noqa: BLE001
                rules = []
        if not rules and not self.session_choices:
            return None
        lab = self._job_labels(rules) if rules else {}
        p = _R.plan(rules, lab, its, self.session_choices)
        return None if _R.empty(p) else p

    def _report(self) -> dict:
        return dict(getattr(self.assembled, "choices_report", None) or {})

    def _after_build(self):
        """After a build: the overlapping-roles question, asked once per pair (8.3)."""
        if self.overlap_ask is not None or self.choice_ask is not None:
            return
        st = self._rules_store()
        if st is None:
            return
        from applicant import rules as _R
        try:
            shown = set(self._report().get("shown") or [])
            active = _R.list_rules(st, status=_R.ACTIVE)
            for a, b, _n in _R.overlapping_pairs(self._choice_items()):
                if a["id"] not in shown or b["id"] not in shown:
                    continue
                if _R.overlap_answer(st, a["id"], b["id"]):
                    continue
                if any(r["action"] == "apart" and sorted(r["items"]) == sorted([a["id"], b["id"]]) for r in active):
                    continue
                when = _R.years(a, b)
                _R.save_overlap_answer(st, a["id"], b["id"], "asked")    # asked once, ever
                self.overlap_ask = {
                    "a": a["id"], "b": b["id"],
                    "text": (f"You were at {a['label']} and {b['label']} at the same time"
                             + (f" ({when})" if when else "") + ". Show both, or one per job?")}
                return
        except Exception:      # noqa: BLE001
            self.overlap_ask = None

    def _offer_scope(self):
        from applicant import scope as _Sc
        return _Sc.offer(self._job_labels())

    def _make_ask(self, kind: str, action: str, items: list, names: list):
        """The ONE line under the preview after the person changed what this resume shows: keep
        it for similar jobs? Offered at the job's most specific known label. Not asked when a
        saved choice already says it, after "Just this time" in the last 30 days, or twice in one
        build."""
        if self._choice_asked:
            return
        st = self._rules_store()
        if st is None:
            return
        from applicant import rules as _R
        from applicant import scope as _Sc
        sc = self._offer_scope()
        company = (self.company or "").strip()
        if sc is None and not company:
            return
        kind_, val = sc if sc else ("company", company)
        if _R.said_once(st, _R.once_sig(action, items, kind_, val)):
            return
        for r in _R.list_rules(st, status=_R.ACTIVE):
            if r["action"] == action and r["items"] == items and r["scope_kind"] == kind_ and r["scope_value"] == val:
                return
        where = _Sc.words(kind_, val)
        other = "other " + where if kind_ != "company" else f"other {val} jobs"
        if kind == "hide":
            text = f"Hide {names[0]} for {other} too?"
        elif kind == "show":
            text = f"Show {names[0]} on {other} too?"
        else:
            text = f"Use {names[0]} instead of {names[1]} for {other}?"
        buttons = []
        if sc:
            buttons.append({"id": "scope", "label": f"Yes, {where}"})
            if company:
                buttons.append({"id": "company", "label": f"Only {company}"})
        else:
            buttons.append({"id": "company", "label": f"Yes, {company} jobs"})
        buttons.append({"id": "once", "label": "Just this time"})
        self.choice_ask = {"kind": kind, "action": action, "items": items, "text": text,
                           "scope_kind": kind_, "scope_value": val, "buttons": buttons}
        self.overlap_ask = None
        self._choice_asked = True

    @_locked
    def set_shown(self, item_id: str, show: bool):
        """The builder's "Show on this resume" switch: this resume shows (or leaves off) one of
        the person's roles or projects, then rebuilds. One line may ask to keep it for similar
        jobs."""
        its = {it["id"]: it for it in self._choice_items()}
        if item_id not in its or self.assembled is None:
            st = self._state(); st["choice_error"] = "That part of your resume is not on this page."
            return st
        want = "show" if show else "hide"
        ask = self.choice_ask
        if ask and ask.get("kind") in ("show", "hide") and ask.get("items") == [item_id] and ask["action"] != want:
            self.choice_ask, self._choice_asked = None, False      # the person undid it
            self.session_choices.pop(item_id, None)
        else:
            self.session_choices[item_id] = want
            self._make_ask(want, want, [item_id], [its[item_id]["label"]])
        self.dirty = True
        self._assemble()
        return self._state()

    @_locked
    def swap_role(self, from_id: str, to_id: str):
        """"Swap for ...": this resume shows ``to_id`` instead of ``from_id`` (two roles whose
        dates overlap), then rebuilds; the line asks whether to do the same on similar jobs."""
        its = {it["id"]: it for it in self._choice_items()}
        if from_id not in its or to_id not in its or from_id == to_id or self.assembled is None:
            st = self._state(); st["choice_error"] = "Those roles are not on this resume."
            return st
        self.session_choices[from_id] = "hide"
        self.session_choices[to_id] = "show"
        self._make_ask("swap", "prefer", [to_id, from_id], [its[to_id]["label"], its[from_id]["label"]])
        self.dirty = True
        self._assemble()
        return self._state()

    @_locked
    def answer_choice(self, answer: str):
        """The person's tap on the one line: ``scope`` (Yes, bank and finance jobs), ``company``
        (Only Acme Bank) or ``once`` (Just this time)."""
        ask, st = self.choice_ask, self._rules_store()
        if not ask or st is None:
            return self._state()
        from applicant import rules as _R
        from applicant import scope as _Sc
        saved = ""
        if answer in ("scope", "company"):
            kind, val = ((ask["scope_kind"], ask["scope_value"]) if answer == "scope"
                         else (_Sc.COMPANY, (self.company or "").strip()))
            label = next((b["label"] for b in ask.get("buttons") or [] if b["id"] == answer), "")
            rid = _R.add_rule(st, ask["action"], ask["items"], kind, val,
                              words=f"{ask['text']} {label}".strip(), source="builder",
                              source_ref=f"builder:{self.jobname}")
            self._born_rules.add(int(rid))
            rule = _R.get_rule(st, rid)
            saved = "Saved. " + _R.sentence(rule, self._choice_items()) if rule else ""
        elif answer == "once":
            _R.remember_once(st, _R.once_sig(ask["action"], ask["items"], ask["scope_kind"], ask["scope_value"]))
        self.choice_ask = None
        out = self._state()
        if saved:
            out["choice_saved"] = saved
        return out

    @_locked
    def answer_overlap(self, answer: str):
        """"Show both, or one per job?" ``both`` keeps both (never asked again for the pair);
        ``one`` saves an apart choice for every job and rebuilds."""
        ask, st = self.overlap_ask, self._rules_store()
        if not ask or st is None:
            return self._state()
        from applicant import rules as _R
        _R.save_overlap_answer(st, ask["a"], ask["b"], "one" if answer == "one" else "both")
        self.overlap_ask = None
        saved = ""
        if answer == "one":
            rid = _R.add_rule(st, "apart", [ask["a"], ask["b"]], "all", "", words=f"{ask['text']} One per job",
                              source="builder", source_ref=f"builder:{self.jobname}")
            rule = _R.get_rule(st, rid)
            saved = "Saved. " + _R.sentence(rule, self._choice_items()) if rule else ""
            self.overlap_ask = {"done": True}             # do not ask about another pair this build
            self.dirty = True
            self._assemble()
            self.overlap_ask = None
        out = self._state()
        if saved:
            out["choice_saved"] = saved
        return out

    @_one_voice
    @_locked
    def answer_proposal(self, token: str, save: bool, option: int = 0):
        """Save (or not) a choice read in this chat. Saved, it applies to this page at once."""
        st = self._rules_store()
        self.rule_proposals = [p for p in self.rule_proposals if p.get("token") != token]
        if st is None:
            return self._state()
        from applicant import rules as _R
        res = _R.answer_proposal(st, token, bool(save), int(option or 0))
        msg = ""
        if res.get("saved"):
            rule = _R.get_rule(st, res["rule_id"])
            msg = "Saved. " + _R.sentence(rule, self._choice_items()) if rule else "Saved."
            if self.assembled is not None:
                self.dirty = True
                self._assemble()
        elif res.get("ok"):
            msg = "Not saved. Say it another way, or set it on Your answers, under Resume choices."
        if msg:
            self._append("agent", msg)
        return self._state([msg] if msg else None)

    def _propose_rules(self, rows):
        """Choices the chat read (distill_memory's ``rules``), kept as chips with Save."""
        self._new_proposals = []
        st = self._rules_store()
        if st is None or not rows:
            return
        from applicant import rules as _R
        its = _R.items(self.profile if (self.profile or {}).get("experience") else (self.saved_profile or {}))
        try:
            new = _R.propose_from_chat(st, rows, its, "app", companies=[self.company] if self.company else [],
                                       ref=self._last_user_ref or "")
        except Exception:      # noqa: BLE001
            new = []
        self.rule_proposals.extend(new)
        self._new_proposals = new

    def _why(self, it: dict, rep: dict, rules: dict, its: list) -> str:
        """"Shown because ..." in plain words, or "" when only the job match decided."""
        from applicant import rules as _R
        from applicant import scope as _Sc
        iid = it["id"]
        ex = (rep.get("excluded") or {}).get(iid)
        pin = (rep.get("pins") or {}).get(iid)

        def where(rid):
            r = rules.get(int(rid or 0))
            return _Sc.words(r["scope_kind"], r["scope_value"]) if r else ""
        line = ""
        if ex:
            rid = ex.get("rule") or 0
            other = _R.label_of(ex.get("by") or "", its) if ex.get("by") else ""
            if not rid:
                line = "Hidden on this resume."
            elif ex.get("how") == "hide":
                line = f"Hidden for {where(rid)}." if where(rid) != "every job" else "Hidden on every resume."
            elif ex.get("how") == "prefer":
                line = f"Hidden for {where(rid)}: you show {other} instead."
            else:
                line = f"Hidden: you never show {other} and {it['label']} together."
        elif pin and iid in (rep.get("shown") or []):
            rid = pin.get("rule") or 0
            if not rid:
                line = "Shown on this resume."
            elif pin.get("how") == "lead":
                line = f"Leads this resume for {where(rid)}."
            else:
                line = f"Shown because you always show it for {where(rid)}."
        elif iid in (rep.get("dropped_pins") or []):
            line = "Not shown: your choices keep more roles than fit, so the most recent stay."
        beat = ((self._last_beat or {}).get(iid))
        if line and beat and int(beat) in rules:
            line += f" That beat your {where(beat)} choice."
        return line

    _last_beat: dict | None = None

    def _rename_items(self, before, after, back=None) -> list:
        """A role or project renamed on the page: its saved choices (and the brain's fact about
        it) follow the new name, and so do this resume's switches. Returns the (old, new) pairs."""
        from applicant import rules as _R
        if back is not None:
            pairs = [(n, o) for o, n in back]
        else:
            old = {it["pos"]: it["id"] for it in _R.items(before or {})}
            pairs = [(old[it["pos"]], it["id"]) for it in _R.items(after or {})
                     if it["pos"] in old and old[it["pos"]] != it["id"]]
        if not pairs:
            return []
        st = self._rules_store()
        for o, n in pairs:
            if o in self.session_choices:
                self.session_choices[n] = self.session_choices.pop(o)
            if st is not None:
                try:
                    _R.rename_item(st, o, n)
                except Exception:  # noqa: BLE001
                    pass
        return pairs

    def _record_render_profile(self) -> dict:
        """The material saved with the record (the Word export reads it): the constraint-filtered
        copy, minus what the person's choices left off this page."""
        prof = self._render_profile()
        ex = self._report().get("excluded") or {}
        if not ex:
            return prof
        from tailoring.assembler import choice_pool
        return choice_pool(prof, {"excluded": ex})

    def _record_application(self, rec_id) -> None:
        """The brain's history row for this application (section 5.4): the job's labels, what
        the page showed and which choices changed it; each such choice counts one more use."""
        st = self._rules_store()
        if st is None:
            return
        try:
            from applicant import rules as _R
            rep = self._report()
            lab = self._labels or self._job_labels()
            _R.record_application(st, rec_id, company=self.company or "", role=self.role or "",
                                  url=(self.source_job or {}).get("url") or "", labels=lab,
                                  shown=rep.get("shown") or [],
                                  applied=sorted(set(rep.get("applied") or []) | set(self._born_rules)))
        except Exception:      # noqa: BLE001 - history never blocks a save
            pass

    def choices_dict(self) -> dict:
        """For the builder's edit page: each role and project with its switch, its swap and why
        it is shown or not; the one line to answer; the choices read in chat."""
        from applicant import rules as _R
        rep = self._report()
        its = self._choice_items()
        st = self._rules_store()
        rules = {}
        if st is not None:
            try:
                rules = {int(r["id"]): r for r in _R.list_rules(st)}
            except Exception:  # noqa: BLE001
                rules = {}
        shown = set(rep.get("shown") or [it["id"] for it in its])
        plan = self._choices_plan() or {}
        self._last_beat = plan.get("beat") or {}
        out_items = []
        for it in its:
            row = {"id": it["id"], "pos": it["pos"], "kind": it["kind"], "label": it["label"],
                   "shown": it["id"] in shown, "why": self._why(it, rep, rules, its)}
            ex = (rep.get("excluded") or {}).get(it["id"]) or (rep.get("pins") or {}).get(it["id"])
            if ex and ex.get("rule"):
                row["rule"] = int(ex["rule"])
            if it["kind"] == "role" and row["shown"]:
                row["swap"] = [{"id": b["id"], "label": b["label"]} for a, b, _n in
                               [(x, y, n) if x["id"] == it["id"] else (y, x, n)
                                for x, y, n in _R.overlapping_pairs(its) if it["id"] in (x["id"], y["id"])]
                               if b["id"] not in shown]
            out_items.append(row)
        return {"items": out_items, "ask": self.choice_ask,
                "overlap": self.overlap_ask if (self.overlap_ask and not self.choice_ask) else None,
                "can_save": st is not None}

    # ------------------------------------------------------------------ gate
    def _effective_essentials(self) -> dict:
        """Essentials with every skeleton field backfilled from what's already on
        the built CV (``self.profile``) and the saved profile, so the gate CHECKS
        THE CV before asking, and never re-requests a location/date/degree that's
        already shown (e.g. a returning user whose stored essentials predate the
        location fields).

        (Phase 3) The applicant brain comes first: a contact detail, a role's dates or location,
        a school's date the brain holds (the details step, the extension, a resume import) lands
        in the essentials themselves, so the gate never asks for it and the resume shows it."""
        st = self._brain()
        if st is not None:
            ess = self.essentials
            if not isinstance(ess.get("identity"), dict):
                ess["identity"] = {}
            brain_backfill(ess["identity"], ess.get("education") or [], ess.get("experience") or [],
                           ess.get("projects") or [], st)
        e = {
            "identity": dict(self.essentials.get("identity") or {}),
            "education": [dict(x) for x in (self.essentials.get("education") or [])],
            "experience": [dict(x) for x in (self.essentials.get("experience") or [])],
            "projects": [dict(x) for x in (self.essentials.get("projects") or [])],
            "declined": list(self.essentials.get("declined") or []),
        }
        for src in (self.profile, self.saved_profile):
            if not src:
                continue
            ident, sedu, sexp, sproj = _flatten_profile(src)
            for f in ("name", "email", "phone", "address", "github", "linkedin", "blog"):
                if not str(e["identity"].get(f) or "").strip() and str(ident.get(f) or "").strip():
                    e["identity"][f] = ident[f]
            _backfill(e["education"], sedu, "school", ("school", "degree", "date", "location", "courses"))
            _backfill(e["experience"], sexp, "org", ("org", "title", "dates", "location"))
            _backfill(e["projects"], sproj, "org", ("org", "location", "dates"))
        # (Phase 3) then the applicant brain again, for what only the built or saved CV named
        brain_backfill(e["identity"], e["education"], e["experience"], e["projects"], st)
        _normalize_identity(e["identity"])
        _fill_project_locations(e["education"], e["experience"], e["projects"])
        return e

    def _missing_required(self) -> list[str]:
        e = self._effective_essentials()
        return _missing_skeleton(e["identity"], e["education"], e["experience"],
                                 e["projects"], e["declined"],
                                 identity_fields=self.identity_fields)

    def _missing_critical(self) -> list[str]:
        """Template-critical fields only, required even under a build override,
        because their absence breaks the template's form."""
        e = self._effective_essentials()
        return _missing_skeleton(e["identity"], e["education"], e["experience"],
                                 e["projects"], e["declined"], critical_only=True,
                                 identity_fields=self.identity_fields)

    def _gate(self) -> list[str]:
        """What to still ask for: template-critical fields ONLY.

        Drafting a resume is a marketing exercise the candidate owns and defends, not a
        deposition. The optional tier (address, links, courses, "do you have any ...")
        never breaks the template -- it only turns one resume into a five-round interview
        before anything renders, which is unusable when the goal is twenty applications in
        front of recruiters. Those gaps are now simply left out of the render and can be
        added by chat afterwards.

        Critical fields stay: a name, one contact, and the org/title/dates/location and
        school/degree/date that the template's layout is literally built around. Without
        those the page renders broken, which helps nobody.
        """
        return self._missing_critical()

    def _months_to_ask(self) -> list[tuple[str, str]]:
        """Entries in ANY dated section (Experience, Projects, Extracurricular) whose date
        is YEAR-ONLY, missing the template's full months, that we haven't already asked
        about, as (name, ask) pairs. Asked ONCE each: if unanswered, the build fills a
        flagged placeholder, so the gate can never loop. Every section is treated the same
        so dates are consistent across the whole CV."""
        asked = {a.lower() for a in (self.essentials.get("months_asked") or [])}
        eff = self._effective_essentials()
        out, seen = [], set()
        # (entries, date field, how the entry is named in the question)
        dated = (
            (eff.get("experience") or [], "dates", "your role at {n}"),
            (eff.get("projects") or [], "dates", "your project {n}"),
            (self.essentials.get("extracurricular") or [], "date", "{n}"),
        )
        for entries, field, phrase in dated:
            for j in entries:
                if not isinstance(j, dict):
                    continue
                # A personal / portfolio project has no employment dates and the template
                # prints none for it, so demanding start and end months for a GitHub repo
                # is asking for something that will never appear on the page -- and it was
                # re-asked every turn, stalling the build behind an unanswerable question.
                if j.get("link") or j.get("url") or j.get("personal"):
                    continue
                name = str(j.get("org") or j.get("title") or "").strip()
                key = name.lower()
                if (name and key not in asked and key not in seen
                        and date_lacks_month(str(j.get(field) or ""))):
                    seen.add(key)
                    out.append((name, f'the start and end month for {phrase.format(n=name)} '
                                f'(the template uses full months, e.g. "January 2021 - '
                                f'August 2023", not just the year)'))
        return out

    def _capture_dates(self, message: str):
        """A date answer to the months question ("feb 2026 to present") names a DATE but
        not the role, so, like locations, bind it to the experience role still missing
        its months. Only a real month+year answer is captured; a year-only reply isn't."""
        from tailoring.conform import extract_date_span
        msg = str(message or "").strip()
        if not msg:
            return
        got = extract_date_span(msg)   # pulls a date span out of a conversational reply
        if not (re.search(r"(?:19|20)\d\d", got) and not date_lacks_month(got)):
            return   # no usable full-month date in the reply
        low = msg.lower()
        # Any dated entry (Experience, Projects, Extracurricular) still missing its months.
        needy = []
        for section, field in (("experience", "dates"), ("projects", "dates"),
                               ("extracurricular", "date")):
            for j in self.essentials.get(section) or []:
                if isinstance(j, dict) and date_lacks_month(str(j.get(field) or "")):
                    needy.append((j, field))
        if not needy:
            return
        # Prefer the entry the person named in the reply; else the first still-needy one.
        target = next(((j, f) for j, f in needy
                       if str(j.get("org") or j.get("title") or "").strip()
                       and str(j.get("org") or j.get("title")).strip().lower() in low), needy[0])
        target[0][target[1]] = got

    # ---- deterministic section router (control plane) ------------------------------ #
    def _capture_section_moves(self, message: str):
        """Honor an EXPLICIT placement directive, "X is an extracurricular activity",
        "put X under projects", "X is a project not a job", by MOVING that entry to the
        named section, overriding the LLM's placement. The person's word is the policy;
        the move is pinned so a later re-extraction can't quietly undo it."""
        msg = str(message or "")
        low = msg.lower()
        if not _MOVE_INTENT.search(low):
            return
        target = next((sec for sec, kws in _SECTION_KEYWORDS if any(k in low for k in kws)), None)
        if not target:
            return
        for src in _MOVABLE_SECTIONS:
            if src == target:
                continue
            for e in list(self.essentials.get(src) or []):
                if isinstance(e, dict) and _entry_names_match(_entry_name(e), low):
                    self.essentials[src].remove(e)
                    self.essentials.setdefault(target, []).append(_to_section_shape(e, target))
                    self.essentials.setdefault("section_pins", {})[_entry_name(e).lower()] = target
                    self._enforce_section_pins()   # also cleans up any duplicate
                    return   # one explicit move per message

    def _enforce_section_pins(self):
        """Keep every explicitly-placed entry in the section the person chose, the
        extractor re-reads the whole history each turn and may re-misplace it, then
        de-duplicate each movable section by entry name (keep the richest copy)."""
        pins = self.essentials.get("section_pins") or {}
        for src in _MOVABLE_SECTIONS:
            for e in list(self.essentials.get(src) or []):
                tgt = pins.get(_entry_name(e).lower()) if isinstance(e, dict) else None
                if tgt and tgt != src:
                    self.essentials[src].remove(e)
                    self.essentials.setdefault(tgt, []).append(_to_section_shape(e, tgt))
        for sec in _MOVABLE_SECTIONS:
            self._dedupe_section(sec)

    def _dedupe_section(self, section: str):
        seen: dict[str, dict] = {}
        out: list[dict] = []
        for e in self.essentials.get(section) or []:
            if not isinstance(e, dict):
                out.append(e); continue
            k = _entry_name(e).lower()
            if not k:
                out.append(e); continue
            if k in seen:
                if len(e.get("bullets") or []) > len(seen[k].get("bullets") or []):
                    out[out.index(seen[k])] = e
                    seen[k] = e
                continue
            seen[k] = e
            out.append(e)
        self.essentials[section] = out

    def _classify_volunteer_extracurricular(self):
        """Conservative default: an experience entry whose NAME clearly marks it as
        volunteering ('(Volunteer)', 'vaccination drive', 'charity') moves to
        Extracurricular, UNLESS the person pinned it to Experience. Resume guidance
        says this placement is genuinely ambiguous, so it's keyed on explicit markers
        (never a formal job) and is always overridable by a directive."""
        pins = self.essentials.get("section_pins") or {}
        for e in list(self.essentials.get("experience") or []):
            if not isinstance(e, dict):
                continue
            nm = _entry_name(e)
            if pins.get(nm.lower()) == "experience":
                continue
            if _VOLUNTEER_SIGNAL.search(nm):
                self.essentials["experience"].remove(e)
                self.essentials.setdefault("extracurricular", []).append(
                    _to_section_shape(e, "extracurricular"))

    def _asked_for(self, key: str) -> bool:
        """Did the LAST agent turn ask for this item? Pending-question context lets a
        bare answer land without the literal cue word (obs #17)."""
        return any(key in str(g).lower()
                   for g in getattr(self, "last_missing", []) or [])

    def _capture_summary(self) -> str:
        """What the session ACTUALLY holds, straight from state, never the model's
        claim of what it did (obs #25: the narrator once said extras were 'locked in'
        while the state held nothing)."""
        e = self.essentials
        lines = ["Here's exactly what I have on file:"]
        for j in e.get("experience", []) or []:
            for r in (j.get("roles") or [j]):
                lines.append(f"• <b>{j.get('org', '?')}</b>: {r.get('title', '?')} "
                             f"({r.get('dates') or 'no dates'}), "
                             f"{len(r.get('bullets') or [])} bullet(s)")
        projs = e.get("projects", []) or []
        lines.append("• Projects: " + (", ".join(
            f"{p.get('org') or p.get('title') or '?'}"
            f"{' (linked)' if (p.get('link') or p.get('url')) else ''}"
            for p in projs) if projs else "none yet"))
        extra = e.get("extracurricular", []) or []
        lines.append("• Extracurricular: " + (", ".join(
            str(x.get("title") or "?") for x in extra) if extra else "none yet"))
        lines.append("• Interests: " + (str(e.get("interests") or "").strip() or "none yet"))
        edu = e.get("education", []) or []
        recent = _most_recent(edu) if edu else {}
        lines.append("• Courses: " + (str(recent.get("courses") or "").strip() or "none yet"))
        si = e.get("skills_input") or []
        if si:
            lines.append(f"• Stated skills: {len(si)} terms")
        dec = sorted(e.get("declined") or [])
        if dec:
            lines.append("• Skipped by your choice: " + ", ".join(dec))
        lines.append("If anything in that list is wrong or missing, tell me and I'll fix it.")
        return "<br>".join(lines)

    def _ask_missing(self, missing: list[str], lead=None, critical: bool = False):
        """Ask conversationally for what's still missing, a couple of fields at a
        time, acknowledging what was just answered, instead of dumping the whole
        list. The LLM phrases it; the same fields are still required underneath."""
        prev = getattr(self, "last_missing", [])
        answered = [m for m in prev if m not in missing]   # resolved since the last ask
        self.last_missing = list(missing)
        # Only acknowledge what was declined THIS turn, never re-say "we'll skip the
        # blog" on every subsequent message.
        cur_declined = set(self.essentials.get("declined", []) or [])
        new_declined = sorted(cur_declined - set(getattr(self, "last_declined", [])))
        self.last_declined = sorted(cur_declined)
        ask = ""
        if not self._narrating():          # in a turn, the turn's one message words the ask
            try:
                ask = str(self.llm.ask_missing(missing, answered, self.role, new_declined) or "").strip()
            except Exception:
                ask = ""
        if not ask:                                        # deterministic fallback
            take = missing[:2]
            joined = take[0] if len(take) == 1 else f"{take[0]}, and {take[1]}"
            ack = "Got it. " if answered else ""
            ask = f"{ack}Could you tell me {joined}?"
        self._fact("ask_missing", ask, ask=list(missing[:2]),
                   just_answered=answered or None, just_declined=new_declined or None,
                   note="ask for these (at most two), naturally; acknowledge what was just answered "
                        "or declined without praising a decline")
        msgs = list(lead or [])
        msgs.append(ask)
        self._append("agent", ask)
        self._persist()
        return self._state(msgs)

    # ------------------------------------------------------------------ upload
    @_one_voice
    @_locked
    def ingest_document(self, text: str, filename: str = "", links: list | None = None):
        """ONE upload path: read the file, decide what it is (CV / transcript /
        certificate), and route it, the person never has to say which button. Every
        upload is just an upload; we read it and figure out the rest. ``links`` are the
        file's link annotations (intake.cv_import.pdf_links), the real addresses behind
        anchor text such as "In" or "GitHub"."""
        kind = classify_document(text or "", filename)
        if kind == "transcript":
            return self.ingest_transcript(text, filename)
        if kind == "certificate":
            return self.ingest_certificate(text, filename)
        return self.ingest_resume(text, filename, links=links)

    @_one_voice
    def ingest_certificate(self, text: str, filename: str = ""):
        """Record an uploaded certificate as a real credential (added to the skills
        block's certifications). Raw file kept; clean summary to the diary."""
        self._append("user", f"[Uploaded certificate: {filename}]")
        name = _certificate_name(text, filename)
        self.uploads.append({"name": filename or "a certificate", "kind": "certificate",
                             "roles": [], "projects": [], "links": {}, "project_links": {},
                             "read": name or ""})
        if name:
            certs = self.essentials.setdefault("certifications", [])
            if name.lower() not in {c.lower() for c in certs}:
                certs.append(name)
        self._conform_essentials()
        self._persist()
        try:
            self.palace.remember_summary(
                f"Uploaded a certificate ({filename}); recorded certification: {name or 'unnamed'}.")
        except Exception:
            pass
        lead = ([f"I read {filename}, looks like a certificate, so I added "
                 f"“{name}” to your credentials."] if name else
                [f"I read {filename}, it looks like a certificate. What's the exact "
                 "certification name you'd like on the resume?"])
        return self.draft(lead=lead)

    @_one_voice
    @_locked
    def ingest_resume(self, text: str, filename: str = "", links: list | None = None):
        """Pre-fill the profile from an uploaded CV's extracted text: merge the
        structured facts into memory (the real store), write a CLEAN summary into
        the diary (never the raw/OCR'd text), then gate on the required skeleton.

        The upload is the NEWER truth for this application (2026-11-06: a returning person's
        older saved titles and empty projects outranked the resume they had just uploaded).
        So the file is read on its own, not merged by the model into the saved profile, and
        then folded in by code: the upload's roles, schools and projects are this
        application's; the saved profile only fills what the upload leaves blank. Projects
        and the real link addresses (from the PDF's link annotations, never the anchor
        text) come along. The session autosaves, so it is the saved profile next time."""
        from intake.links import (attach_project_links, clean_identity_links, links_note,
                                  projects_from_text, resume_links)
        self._append("user", f"[Uploaded resume: {filename}]")
        found = resume_links(links)
        blank = {"identity": {}, "education": [], "experience": []}
        source = (text or "") + links_note(links)
        try:
            out = self.llm.extract_intake("", blank, [{"role": "user", "content": source}],
                                          with_projects=True)
            up = _canonicalize_essentials(out.get("essentials") or {})
        except Exception:
            up = {}
        up = up if isinstance(up, dict) else {}
        # Projects: the model's reading, else the file's own Projects section read by code.
        projects = [_upload_project(p) for p in (up.get("projects") or []) if isinstance(p, dict)]
        projects = [p for p in projects if p.get("org")] or \
            [_upload_project(p) for p in projects_from_text(text or "")]
        attach_project_links(projects, found["projects"])
        for p in projects:
            if str(p.get("tools") or "").strip() and not str(p.get("location") or "").strip():
                p["location"] = p["tools"]    # the template's right-hand slot, as on the page
        up["projects"] = projects
        ident = dict(up.get("identity") or {})
        clean_identity_links(ident)
        ident.update({k: v for k, v in found["identity"].items() if v})
        up["identity"] = ident
        merged = _merge_upload(self.essentials, up)
        if merged.get("projects") and "projects" in (merged.get("declined") or []):
            merged["declined"] = [d for d in merged["declined"] if d != "projects"]
        # Drop fully-empty entries that extraction noise can introduce (so the gate
        # never asks about a phantom role/degree), and de-dupe so re-uploading or
        # uploading-then-retyping the same facts can't double an entry.
        merged["experience"] = _dedup_entries(
            [j for j in merged.get("experience", [])
             if any(str(j.get(k) or "").strip() for k in ("org", "title", "dates"))],
            ("org", "title", "dates"))
        merged["education"] = _dedup_entries(
            [d for d in merged.get("education", [])
             if any(str(d.get(k) or "").strip() for k in ("school", "degree", "date"))],
            ("school", "degree", "date"))
        merged.setdefault("override", self.essentials.get("override", False))
        self.essentials = merged
        # Keep the person's REAL bullet points from the uploaded CV, never drop them
        # and re-draft over real accomplishments (CLAUDE.md §8).
        self._attach_cv_bullets(text or "")
        # Capture the CV's REAL skills as source-of-truth so the skills block can't drift
        # into JD-shaped fabrications (validated later by _validate_skills).
        self._attach_cv_skills(text or "")
        self._conform_essentials()
        self._persist()
        read = self._note_upload(filename or "your resume", "resume", up, found)

        # Clean, tool-authored summary -> the diary. Raw text is NOT kept there.
        from datetime import date
        from intake.cv_import import summarize_import
        try:
            self.palace.remember_summary(summarize_import(self.essentials, filename, date.today().isoformat()))
        except Exception:
            pass

        lead = [f"I read {filename} and pulled in your details{read}." if filename else
                f"I read your resume and pulled in your details{read}.",
                "I saved them to your profile, so the next resume starts from them too."]
        return self.draft(lead=lead)   # draft() applies the tiered gate

    def _note_upload(self, name: str, kind: str, up: dict, found: dict | None = None) -> str:
        """Remember what this session's upload held (the chat answers "did you see my
        projects?" from it), and return a short ", including ..." clause for the reply."""
        projects = [str(p.get("org") or "").strip() for p in (up.get("projects") or [])
                    if str(p.get("org") or "").strip()]
        ident = up.get("identity") or {}
        links = {k: ident.get(k) for k in ("linkedin", "github", "blog") if str(ident.get(k) or "").strip()}
        roles = [" at ".join(x for x in (str(j.get("title") or "").strip(), str(j.get("org") or "").strip()) if x)
                 for j in (up.get("experience") or []) if isinstance(j, dict)]
        self.uploads.append({"name": name, "kind": kind, "roles": [r for r in roles if r],
                             "projects": projects, "links": links,
                             "project_links": {str(p.get("org")): p.get("link")
                                               for p in (up.get("projects") or [])
                                               if p.get("link") and p.get("org")}})
        bits = []
        if projects:
            bits.append(("your projects " + _and_list(projects)) if len(projects) > 1
                        else f"your project {projects[0]}")
        names = [_LINK_NAMES.get(k, k) for k in links]
        if names:
            bits.append(f"your {_and_list(names)} link" + ("s" if len(names) > 1 else ""))
        return (", including " + " and ".join(bits)) if bits else ""

    def _attach_cv_bullets(self, text: str):
        """Pull each role's REAL bullet points out of the uploaded CV text and attach
        them to the matching experience entry, so real accomplishments are used
        rather than dropped and re-drafted. Engine-independent: works whether the
        extractor captured bullets or not, and only fills roles that have none.

        For every experience org, take the lines between that org and the next role/
        section heading, and keep the ones that read like accomplishments."""
        exp = self.essentials.get("experience", []) or []
        if not exp or not text:
            return
        lines = [ln.strip(" \t•*-, ●.") for ln in text.splitlines()]
        low_lines = [ln.lower() for ln in lines]
        orgs = [str(j.get("org") or "").strip() for j in exp]
        _HEAD = re.compile(r"^(education|experience|employment|skills|projects|"
                           r"certifications?|interests|summary|profile|references)\b", re.I)

        def _line_of(org: str) -> int:
            o = org.lower()
            return next((i for i, ll in enumerate(low_lines) if o and o in ll), -1)

        starts = sorted((i, k) for k, org in enumerate(orgs) if (i := _line_of(org)) != -1)
        for pos, (start, k) in enumerate(starts):
            j = exp[k]
            if [b for b in (j.get("bullets") or []) if str(b).strip()]:
                continue                                   # already has real bullets
            end = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
            bullets = []
            for i in range(start + 1, end):
                ln = lines[i]
                if not ln or _HEAD.match(ln) or len(ln) < 12:
                    continue
                # An accomplishment reads like a sentence (has an action verb) and
                # isn't just another header/title line.
                if _ACTION_RE.search(ln) or ln.endswith((".", ";")) or len(ln.split()) >= 6:
                    b = ln[0].upper() + ln[1:]
                    bullets.append(b if b.endswith(".") else b + ".")
            if bullets:
                j["bullets"] = bullets[:4]

    _SKILL_HEAD = re.compile(r"^\s*(technical\s+skills|core\s+skills|key\s+skills|"
                             r"skills(?:\s*(?:&|and)\s+\w+)?|competencies)\s*:?\s*$", re.I)
    _SECT_HEAD = re.compile(r"\b(education|experience|employment|work\s+history|projects|"
                            r"certifications?|interests|summary|profile|references|awards|"
                            r"publications|languages|volunteer)\b", re.I)

    def _attach_cv_skills(self, text: str):
        """Pull the person's REAL skills out of an uploaded CV's SKILLS section into
        essentials['skills_input'] -- the source of truth for what they actually have.
        Without this the CV's skills would live only in the LLM's free-form draft, where
        JD-shaped fabrications creep in; capturing the real ones here lets the
        deterministic validator (_validate_skills) keep the CV to skills the person
        genuinely lists (CLAUDE.md 8: no unsupported skills)."""
        if not text:
            return
        lines = text.splitlines()
        n = len(lines)
        collected: list[str] = []
        i = 0
        while i < n:
            if self._SKILL_HEAD.match(lines[i].strip()):
                i += 1
                while i < n:
                    ln = lines[i].strip()
                    if not ln:
                        i += 1
                        continue
                    # Stop at the next section heading: a short, heading-like line
                    # (few words, no comma list) naming another CV section.
                    if len(ln) < 40 and "," not in ln and self._SECT_HEAD.search(ln):
                        break
                    if self._SKILL_HEAD.match(ln):
                        break
                    # A skills line often reads "Group: a, b, c" -- drop the group label,
                    # keep the individual comma/pipe-separated terms.
                    body = ln.split(":", 1)[1] if ":" in ln else ln
                    for part in re.split(r"[,;|•·/]", body):
                        t = part.strip(" \t-, .()").strip()
                        if 1 < len(t) <= 40 and not t.endswith(".") and len(t.split()) <= 5:
                            collected.append(t)
                    i += 1
                continue
            i += 1
        if collected:
            existing = self.essentials.setdefault("skills_input", [])
            have = {s.lower() for s in existing}
            for t in collected:
                if t.lower() not in have:
                    existing.append(t)
                    have.add(t.lower())

    def _skill_source_text(self) -> str:
        """The person's GENUINE material, used to validate the skills block -- every
        real signal EXCEPT the drafted skills block itself, so a fabricated skill can
        never validate itself. Their stated skills/certs, real experience bullets,
        education + courses, projects, and interests."""
        e = self.essentials
        parts: list[str] = []
        parts += [str(s) for s in (e.get("skills_input") or [])]
        parts += [str(c) for c in (e.get("certifications") or [])]
        for j in (e.get("experience") or []):
            parts.append(str(j.get("org", "")))
            for r in (j.get("roles") or [j]):
                parts.append(str(r.get("title", "")))
                parts += [str(b) for b in (r.get("bullets") or [])]
        for d in (e.get("education") or []):
            parts += [str(d.get("school", "")), str(d.get("degree", "")),
                      str(d.get("courses", ""))]
        for p in (e.get("projects") or []):
            parts.append(str(p.get("org", "") or p.get("title", "")))
            parts += [str(b) for b in (p.get("bullets") or [])]
        parts.append(str(e.get("interests") or ""))
        # Real (non-drafted) bullets that only exist on the built profile also count.
        for x in (self.profile.get("experience") or []):
            for r in (x.get("roles") or [x]):
                if not r.get("drafted_bullets"):
                    parts += [str(b) for b in (r.get("bullets") or [])]
        return "\n".join(parts)

    def _validate_skills(self):
        """Drop any skill the person's own material doesn't support (anti-fabrication,
        CLAUDE.md 8). This is the safety net that keeps the CV honest no matter what the
        LLM drafts: only genuinely-held skills appear; JD skills the person lacks are
        surfaced in the coverage report, never invented onto the page."""
        skills = self.profile.get("skills") or {}
        if not skills:
            return
        from tailoring.keywords import term_present
        src = self._skill_source_text()
        cleaned: dict[str, str] = {}
        for label, value in skills.items():
            kept = [t.strip() for t in str(value).split(",")
                    if t.strip() and term_present(t.strip(), src)]
            if kept:
                cleaned[label] = ", ".join(dict.fromkeys(kept))
        self.profile["skills"] = cleaned

    @_one_voice
    @_locked
    def ingest_transcript(self, text: str, filename: str = ""):
        """Read the REAL courses from an academic transcript and populate the
        most-recent degree's courses with the JD-relevant ones. Same storage rules
        as any upload: facts -> profile, CLEAN summary -> diary, raw file kept."""
        from datetime import date

        self._append("user", f"[Uploaded transcript: {filename}]")
        self.uploads.append({"name": filename or "a transcript", "kind": "transcript",
                             "roles": [], "projects": [], "links": {}, "project_links": {}})
        try:
            courses = self.llm.select_courses(self.jd, text or "")
        except Exception:
            courses = []
        from tailoring.conform import fit_courses
        edu = self.essentials.get("education", [])
        recent = _most_recent(edu) if edu else {}
        if courses and recent:
            existing = [c.strip() for c in str(recent.get("courses") or "").split(",") if c.strip()]
            # Keep only the most-relevant courses that fit ~2 lines, so the chat
            # message and the CV show the SAME list and it never runs to a 3rd line.
            recent["courses"] = fit_courses(", ".join(dict.fromkeys(existing + courses)))
            self.essentials["declined"] = [d for d in self.essentials.get("declined", []) if d != "courses"]
        self._persist()

        stamp = date.today().isoformat()
        summary = (f"User uploaded a transcript ({filename}) on {stamp}; courses populated: "
                   f"{', '.join(courses[:8])}." if courses else
                   f"User uploaded a transcript ({filename}) on {stamp}; no clear courses extracted.")
        try:
            self.palace.remember_summary(summary)
        except Exception:
            pass

        if not edu:
            note = ("Thanks, tell me your school and degree first, then I'll map these courses "
                    "under your most recent degree.")
            self._append("agent", note)
            return self._state([note])
        if not courses:
            note = ("I couldn't read clear courses from that transcript, you can list your "
                    "courses here, or tell me you'd rather skip the courses line.")
            self._append("agent", note)
            return self._state([note])
        lead = [f"I read {filename} and pulled your most JD-relevant courses: {recent.get('courses', '')}."]
        return self.draft(lead=lead)   # draft() applies the tiered gate

    def suggest_projects_flow(self):
        """Draft JD-matching sample projects as FLAGGED suggestions (persona
        reference) and offer help to actually build one. Flagged projects block
        finalization until the person replaces, builds, or removes them.

        Flag-off only, and retired with the flag (plan PR 10): with TAILOR_CHAT_AGENT on, ideas
        for this job come as a card the person builds from, and no placeholder ("suggested")
        project is ever written onto the page."""
        if self._agent_on():
            last = next((t.get("content") for t in reversed(self.history)
                         if t.get("role") == "user"), "") or "Build me a project for this job"
            return self._agent_reply(str(last))
        try:
            projs = self.llm.suggest_projects(self.jd) or []
        except Exception:
            projs = []
        for p in projs:
            p["suggested"] = True
        keep = [p for p in self.essentials.get("projects", []) if not p.get("suggested")]
        self.essentials["projects"] = keep + projs
        self.essentials["declined"] = [d for d in self.essentials.get("declined", []) if d != "projects"]
        self._persist()

        lead = ["Here are projects you could actually build to qualify for this role:"]
        for p in projs:
            lead.append(f"• <b>{p.get('org') or p.get('title', 'Project')}</b>, {p.get('how', '')}".strip())
        lead.append("I've added them as <b>clearly-marked suggestions</b>. Build one (even a small "
                    "version on public data and GitHub) and I'll swap it in. You must replace or "
                    "<b>remove</b> a suggested project before the resume can be saved or exported.")
        missing = self._missing_required()
        if missing:
            return self._ask_missing(missing, lead=lead)
        return self.draft(lead=lead)

    @_one_voice
    @_locked
    def remove_suggested_projects(self):
        """Drop all flagged placeholder projects and unblock finalization."""
        self.essentials["projects"] = [p for p in self.essentials.get("projects", [])
                                       if not p.get("suggested")]
        if self.profile:
            self.profile["projects"] = [p for p in self.profile.get("projects", [])
                                        if not p.get("suggested")]
        dec = set(self.essentials.get("declined", []))
        dec.add("projects")   # the person chose not to include a project
        self.essentials["declined"] = sorted(dec)
        if self.assembled is not None:
            self._assemble()
        self._persist()
        return self._state(["Removed the suggested project(s). You can add a real one anytime, "
                            "just tell me the project, its location, and dates."])

    @staticmethod
    def _empty():
        return {"identity": {}, "education": [], "experience": [],
                "skills": {}, "projects": [], "extracurricular": [], "interests": ""}

    def _preview_from_essentials(self):
        e = self.essentials
        return {
            "identity": dict(e.get("identity", {})),
            "education": [dict(x) for x in e.get("education", [])],
            "experience": [
                {"org": j.get("org", ""), "location": j.get("location", ""),
                 "roles": [{"title": j.get("title", ""), "dates": j.get("dates", ""), "bullets": []}]}
                for j in e.get("experience", [])
            ],
            "skills": {}, "projects": [], "extracurricular": [], "interests": "",
        }

    def preview(self):
        return self.profile if self.stage in ("review", "done") else self._preview_from_essentials()

    def _known_summary(self) -> str:
        e = self.essentials
        bits = []
        edu = e.get("education", [])
        if edu:
            d = edu[0]
            bits.append(f"{d.get('degree','a degree')} from {d.get('school','')}".strip())
        orgs = [j.get("org", "") for j in e.get("experience", []) if j.get("org")]
        if orgs:
            bits.append("roles at " + (", ".join(orgs[:3])))
        return "; ".join(b for b in bits if b) or "your background"

    def opening(self) -> str:
        role_phrase = f"the {self.role} role" if self.role else "this role"
        where = f" at {self.company}" if self.company else ""
        if self.returning:
            name = self.essentials.get("identity", {}).get("name", "")
            first = (" " + name.split()[0]) if name else ""
            text = (f"Welcome back{first}. I still have your background, {self._known_summary()}. "
                    f"Anything new or changed since last time, or should I tailor what I have to "
                    f"{role_phrase}{where}?")
            return self._fact("opening", text, returning=True, first_name=first.strip(),
                              background_on_file=self._known_summary(), job_role=self.role,
                              company=self.company,
                              ask="whether anything is new or changed since last time, or to "
                                  "tailor what is on file to this job")
        text = (f"I've read {role_phrase}{where}. <b>Upload your existing resume</b> "
                "(Word, PDF, PowerPoint, or a photo/scan) and I'll pull in the details, or just "
                "tell me about yourself: your name, schools, and work history. Either way I'll only "
                "ask about anything that's missing.")
        return self._fact("opening", text, returning=False, job_role=self.role, company=self.company,
                          ask="upload their existing resume (Word, PDF, PowerPoint, or a photo), "
                              "or tell you their name, schools and work history; you only ask "
                              "about what is missing")

    # ------------------------------------------------------------------ client state
    def _state(self, messages=None, extra=None, cards=None):
        s = {
            "phase": self.stage,
            # Chat cards (agent/resume_chat.py) under this turn's messages; each is also kept
            # on its history turn ("card") so a restored chat shows it again.
            "cards": list(cards or []),
            "role": self.role, "company": self.company,
            # Which template this CV is in. The person is no longer asked to choose one up
            # front (we read the JD, so we already know), which only works if the answer is
            # then visible and changeable. An unnamed default is the silent sticky state
            # this replaced.
            "template": self.template_name,
            "template_label": str((self.manifest or {}).get("display_name")
                                  or self.template_name or ""),
            "messages": messages or [],
            "preview": self.preview(),
            # The edit view shows exactly the sections this template prints, no more: a summary
            # the page never renders must not be offered for editing (seen 2026-10-08).
            "template_sections": list(self.sections or []),
            "step": "review" if self.stage in ("review", "done") else "details",
            "returning": self.returning,
            # A returning user with a real saved profile can build in ONE click,
            # no conversation, the autonomous "just build it" path (CLAUDE.md §4c).
            "can_autobuild": bool(self.returning and (self.saved_profile or {}).get("experience")
                                  and self.assembled is None and self.stage == "chatting"),
        }
        # While the required-sections question is open, the build button can do nothing but
        # ask again, so it is not offered. The UI offers "None, leave it off" instead (a real
        # decline of exactly these sections) and keeps the text box for a real answer.
        req_open = self._required_question_open()
        if req_open:
            s["can_autobuild"] = False
            s["required_open"] = [{"key": k, "label": lbl} for k, lbl
                                  in zip(req_open, self._required_labels(req_open))]
        if self.recalled:
            s["recalled"] = self.recalled
        if self.rule_proposals:
            s["rule_proposals"] = list(self.rule_proposals)
        # P1: why these choices, so the person sees the reasoning and can override on the page.
        if self.cv_selection and (self.cv_selection.get("selected")
                                  or self.cv_selection.get("excluded")):
            s["selection"] = self.cv_selection
        if self.assembled is not None:
            s["coverage"] = self.coverage_dict()
            s["review"] = self.review_dict()   # honest pre-send check (fabrication guard + fit)
            s["flags"] = self.title_flags()
            # Figures on the draft with no origin in the person's material: confirm or
            # strike before sending. Empty when every number traces back to them.
            s["fact_flags"] = list(getattr(self.assembled, "fact_flags", None) or [])
            # Keywords added through the coverage report, so the panel can offer undo links.
            s["keyword_actions"] = [{"term": a["term"], "kind": a["kind"], "label": a["label"]}
                                    for a in self.keyword_actions]
            # What tailoring did, worked out in code (tailoring/changes.py), never by the model.
            try:
                s["changes"] = self.changes_dict()
            except Exception:  # noqa: BLE001 - the page works without its change list
                s["changes"] = None
            # What the one-page fit cut, each with "Put back" (re-runs the fit check).
            from tailoring.changes import public_trim
            s["trims"] = [public_trim(t) for t in (getattr(self.assembled, "trims", None) or [])]
            # One-click refine chips from the coverage gaps the person's material supports:
            # "Work in Kubernetes" runs the "Add this keyword" path. Made here from the report,
            # with no model call.
            s["refine"] = self.refine_chips(s.get("coverage"))
            s["pdf"] = f"/api/cv.pdf?job={self.jobname}&v={self._pdf_v}"
            try:
                s["choices"] = self.choices_dict()
            except Exception:  # noqa: BLE001 - the page works without its switches
                pass
            s["one_page"] = bool(self.assembled.ok and self.assembled.compile.pages == 1)
            # Flagged placeholder projects block finalization until resolved.
            sugg = self._suggested_projects()
            if sugg:
                s["suggested_projects"] = sugg
                s["blocked_finalize"] = True
        if extra:
            s.update(extra)
        # A model reply asked for the person's GitHub while it is not connected (_no_github_ask):
        # the Connect GitHub button instead, once, unless this turn already shows one.
        due = self._github_card_due
        if due and s["messages"]:
            self._github_card_due = False
            said = due is True or any(str(due) in str(m) for m in s["messages"])
            if said and not any(self._connect_buttons(c) for c in s["cards"]):
                from agent.resume_chat import connect_card
                card = connect_card()
                self._fact("github_not_connected", self.GITHUB_NOT_CONNECTED,
                           github="not connected and no username known",
                           point_to="the Connect GitHub button below; once connected you check "
                                    "their repos for this job by yourself")
                self._append("agent", self.GITHUB_NOT_CONNECTED)
                self.history[-1]["card"] = card
                s["messages"] = list(s["messages"]) + [self.GITHUB_NOT_CONNECTED]
                s["cards"].append(card)
        # A build told in the chat by a helper (add_projects inside a chat turn) rides on the
        # first state after it; a build's own state gets it from _builds.
        if self._pending_build is not None and self._bt is None:
            s["build"], self._pending_build = self._pending_build, None
        return s

    def _suggested_projects(self) -> list[str]:
        return [str(p.get("org") or p.get("title") or "Project")
                for p in (self.profile.get("projects") or []) if p.get("suggested")]

    _pdf_v = 0
    _bt = None              # the build being told in the chat (_builds)
    _pending_build = None   # a helper's build, carried by the next state
    _turn = None            # the chat turn being said in one message (_one_voice)

    # -- one voice: the code decides what is said, the AI says it (agent/narrate.py) ------ #
    def _voice_on(self) -> bool:
        """The turn's message is written by the model: narration on, a model that narrates,
        and someone reading (an unattended auto-apply build has nobody in the chat)."""
        from agent.narrate import enabled
        return (enabled() and callable(getattr(self.llm, "narrate", None))
                and not self.essentials.get("unattended"))

    def _narrating(self) -> bool:
        """Inside a turn whose message the model writes: a helper that would word a line with its
        own model call (a question for more material) leaves the wording to that one message."""
        return self._turn is not None and self._voice_on()

    def _fact(self, kind: str, text: str, exact=None, **facts) -> str:
        """A fixed line said in this turn, as a brief item: what it is (``kind``), the facts the
        written message must state (``exact`` verbatim), and the line itself as the fallback.
        Returns the line, so the caller says it exactly as before."""
        if text and self._turn is not None:
            from agent.narrate import item
            self._turn["items"][text] = item(kind, text, exact, **facts)
        return text

    def _model_said(self, text: str) -> str:
        """A line a model wrote for this turn (the intake's reply, the agent's answer)."""
        if text and self._turn is not None:
            self._turn["model"].add(text)
        return text

    _voice_log: list = []

    def _speak(self, turn: dict, out):
        """The end of a turn (_one_voice): everything the app said in it becomes ONE message.
        Returns ``out`` with that message, its cards, and the build's row before it."""
        from agent import narrate as _N
        st = out if isinstance(out, dict) and "phase" in out else None
        h0 = min(int(turn["h0"]), len(self.history))
        said_rows = [t for t in self.history[h0:] if isinstance(t, dict)
                     and t.get("role") == "agent" and not t.get("build")]
        said = [str(m) for m in ((st or {}).get("messages") or []) if str(m or "").strip()]
        for t in said_rows:
            c = str(t.get("content") or "")
            if c.strip() and c not in said:
                said.append(c)
        if not said:
            return out
        cards: list = []
        for t in said_rows:
            for c in (t.get("cards") or ([t["card"]] if t.get("card") else [])):
                if isinstance(c, dict) and not any(c is x for x in cards):
                    cards.append(c)
        for c in (st or {}).get("cards") or []:
            if isinstance(c, dict) and not any(c is x for x in cards):
                cards.append(c)
        model = turn["model"]
        rec = (st or {}).get("build") or next(
            (t["build"] for t in self.history[h0:] if isinstance(t, dict) and t.get("build")), None)
        log = {"turn": len(self.history), "lines": len(said), "narrated": False}
        if len(said) == 1 and said[0] in model:
            final = said[0]                   # one model reply already: the AI said it
        else:
            items = [turn["items"].get(m) or _N.item("reply_draft" if m in model else "note", m)
                     for m in said]
            if rec:
                items.insert(0, _N.item("build", "", summary=rec.get("summary"),
                                        what_changed=list(rec.get("items") or [])[:4]))
            if self._voice_on():
                context = (self.prior_history + [t for t in self.history if not any(
                    t is r for r in said_rows)])
                res = _N.narrate(self.llm, items, context, message=turn.get("message") or "",
                                 role=self.role, company=self.company)
            else:
                res = {"text": _N.fallback(items), "ok": False, "why": "off", "usage": None}
            final = res["text"]
            if res["ok"]:
                final = self._voice_checks(final, items, turn, cards)
                if not final:
                    res["ok"], res["why"], final = False, "checks", _N.fallback(items)
            log.update({"narrated": bool(res["ok"]), "why": res.get("why") or "",
                        "usage": res.get("usage"), "secs": res.get("secs"),
                        "kinds": [i.get("kind") for i in items]})
            final = final or _N.fallback(items) or said[-1]
        self._voice_log = (list(self._voice_log) + [log])[-50:]
        _voice_logger(log)
        entry = {"role": "agent", "content": final}
        if cards:
            entry["card"] = cards[0]
            if len(cards) > 1:
                entry["cards"] = cards
        keep = [t for t in self.history if not any(t is r for r in said_rows)]
        self.history[:] = keep + [entry]
        try:
            self.palace.remember_turn("agent", final)
        except Exception:  # noqa: BLE001
            pass
        self._persist()
        if st is not None:
            st["messages"] = [final]
            st["cards"] = cards
            if st.get("build"):
                st["build_at"] = 0
        return out

    def _voice_checks(self, text: str, items: list, turn: dict, cards: list) -> str:
        """The written message under the chat's own rules: "share your GitHub" becomes the Connect
        GitHub button while it is not connected (the button goes under the message), and a claim
        that something was saved, added, changed or filled when nothing changed in this turn
        throws the message away (empty: the fallback is said instead)."""
        from agent import narrate as _N
        from agent.resume_chat import connect_card
        if _N.claims_change(text) and self._pdf_v == turn["v0"] \
                and self._material_sig() == turn["sig"] and not self._said_done(items):
            return ""
        shared, hit = _N.share_to_connect(text)
        if hit:
            gh = self._github_view()
            if not (gh.get("connected") or gh.get("resume_login")):
                text = shared
                if not any(self._connect_buttons(c) for c in cards):
                    cards.append(connect_card())
        return text

    # Brief kinds that report something the app did in this turn, so the message may say so even
    # when the page itself did not change (a saved choice, a project build started).
    _DONE_KINDS = ("added_projects", "added_from_github", "passed_quiz", "build_started",
                   "keep_choice", "note", "github_connected")

    def _said_done(self, items: list) -> bool:
        return any(i.get("kind") in self._DONE_KINDS for i in items)

    @_one_voice
    def start(self):
        # Recall the most relevant prior statements for THIS job from the diary (keyword
        # overlap), on every entry point (New CV and Tailor-from-job both land here).
        self.recalled = self.palace.recall(f"{self.role} {self.company} {self.jd[:200]}")
        text = self.opening()
        self._append("agent", text)
        return self._state([text])

    # ------------------------------------------------------------------ conversation turn
    def submit(self, message: str):
        with self.lock:
            return self._submit(message)

    @_one_voice
    def _submit(self, message: str):
        message = (message or "").strip()
        if not message:
            return self._state()
        if self._turn is not None:
            self._turn["message"] = message
        # Did the agent answer the last turn? Then a follow-up question ("None of them is strong.
        # What do you suggest?") is part of that conversation, even without a GitHub or project
        # word in it, and must not fall back to the intake's next field question.
        followup, self._agent_spoke = bool(getattr(self, "_agent_spoke", False)), False
        self._github_card_due = False
        self._append("user", message)

        # "Check my GitHub" (the offer's button sends this exact line): the check itself, not a
        # model's choice of it. A typed "check my github" stays with the intake and the agent.
        if self._agent_on() and self._is_check_github(message):
            return self._check_github()
        # "add 2 github projects": the add itself, before the intake's extractor or enrichment
        # can take it as material ("send me the repo links", Kofi, 2026-11-09). Ranked for this
        # job, the top N that fit added like the card's button, even while an intake answer is
        # pending. A pasted list or link, or a described project, is not such a request.
        if self._agent_on():
            from agent.resume_chat import github_add_count
            n_add = github_add_count(message)
            if n_add:
                return self._add_from_github(n_add)

        # Flagged-placeholder project controls.
        if _wants_remove_suggested(message) and self._suggested_projects():
            return self.remove_suggested_projects()
        if _wants_project_ideas(message):
            # With the agent on, ideas for THIS job come as a card the person builds from
            # (suggest_projects_for_jd); no placeholder project goes on the page.
            if self._agent_on():
                return self._agent_reply(message)
            return self.suggest_projects_flow()

        # A read request, answered FROM STATE before anything can consume it as an
        # answer to the pending question (obs #11/#25: this exact request was once
        # swallowed by the queue while the narrator claimed things state didn't hold).
        if _wants_capture_summary(message):
            note = self._capture_summary()
            self._append("agent", note)
            self._persist()
            return self._state([note])

        # Always read the message first. A user often supplies their details AND
        # opts out in the same breath ("…just build with what you have"), extract
        # before acting on the override so the build uses what they just gave, not
        # an empty profile.
        declined_now: list[str] = []
        material_before = self._material_sig()
        prior_projects = [dict(p) for p in (self.essentials.get("projects") or [])]
        prior_education = [dict(d) for d in (self.essentials.get("education") or [])]
        out = self.llm.extract_intake(self.jd, self.essentials, self.history)
        merged = _canonicalize_essentials(out.get("essentials") or self.essentials)
        for k in ("identity", "education", "experience"):
            merged.setdefault(k, self.essentials.get(k, {} if k == "identity" else []))
        # Preserve everything the extractor doesn't manage, gate state (declined,
        # projects, override) AND enrichment state (enrich_asked/_done, skills_input,
        # certifications, interests, extracurricular), so it's never dropped and we
        # don't re-ask what's already been offered or answered. Session state is
        # AUTHORITATIVE for these keys: the extractor re-emits the whole bag and
        # often echoes them back empty, and a setdefault let that echo win, silently
        # wiping skills/interests/extracurricular on any later message. Keep the
        # extractor's copy only when the session genuinely has nothing.
        def _blank(x):
            return x is None or x == "" or x == [] or x == {}
        for k, v in self.essentials.items():
            if k not in ("identity", "education", "experience"):
                if not _blank(v) or _blank(merged.get(k)):
                    merged[k] = v
        self.essentials = merged
        # The extractor re-emits education/projects each turn but often WITHOUT fields it
        # doesn't manage (transcript courses, chat-captured projects), so carry those
        # forward, or they'd be silently wiped on the next message.
        self._preserve_projects(prior_projects)
        self._preserve_education_extras(prior_education)
        self._capture_projects(message)      # explicit "add these projects: …" lists
        self._apply_side_channels(message)   # declines ("no github") + inline courses
        self._conform_essentials()           # full-month dates + full degrees in every view
        # A pre-build answer to the required-section questions (Extracurricular / Interests,
        # asked before the first draft) is REAL material the skeleton extractor doesn't
        # capture, fold it in here so the required-sections gate below sees it.
        if self.assembled is None and self.essentials.get("asked_required"):
            # Absorb any real material in this message FIRST (absorb-then-finalize). A request
            # for the agent ("use my github") is not material, so enrichment never takes it.
            from agent.resume_chat import wants_agent as _wants_agent
            if not (self._agent_on() and _wants_agent(message, pending=True)):
                self._absorb_enrichment(message)
            # Then a bare "none" / "skip", or "that's everything" (which the question itself
            # invites), answers the open question: it declines exactly the required sections
            # STILL empty, recorded like any named decline (and reused next job). A section
            # that just got real content stays. Re-asking here was the same loop as the build
            # button in other words.
            if _bare_decline(message) or _wants_done(message):
                declined_now = self._decline_open_required()
            # Any reply to the question answers it (Kofi, 2026-11-10: ask ONCE, never again).
            # His "football and travelling" landed in Interests while only Extracurricular had
            # been asked, and the chat said "Before I build, I need your Extracurricular" right
            # after he answered. What he didn't give is covered for this chat (not declined), and
            # the page fill fills the room with more of his recent work.
            # Only a reply to THAT question: the last thing the chat said before this message
            # was the outside-work ask (a reply to a skeleton question is not an answer to it).
            e = self.essentials
            if self._last_agent_said(e.get("required_ask_text")):
                asked = list(e.get("required_open_at_ask") or self._open_required_sections())
                e["required_covered"] = sorted(set(e.get("required_covered") or []) | set(asked))
            if _wants_done(message) and not self._open_required_sections():
                self.essentials["enrich_done"] = True
        self._connect_due = False
        reply = self._model_said(self._no_false_limits(str(out.get("reply", "")).strip(), message))
        if self._connect_due:     # a GitHub question answered "not connected": the button, not steps
            return self._reply_with_connect(reply)
        self._persist()   # remember what was shared, without waiting for Accept

        # P1: distill durable facts/preferences/constraints from this turn into the authoritative
        # store (best-effort; the turn is already saved verbatim). Meaningful prose only.
        if self._is_meaningful_turn(message):
            self._new_proposals = []
            self._distill_facts()
            if self._new_proposals:
                # A choice for kinds of jobs ("for banks, show Stanbic, not Viva"): offered as a
                # chip with Save. Nothing is kept until the tap, and the turn is not material.
                note = self._fact("keep_choice", "Want me to keep this choice for next time?",
                                  ask="whether to keep this choice for next time (a Save chip "
                                      "is shown under the message)")
                self._append("agent", note)
                self._persist()
                return self._state([note])

        # EXPLICIT OVERRIDE: "build with what you have / continue with what you know /
        # just build it". The completeness gate only protects a user who is silently
        # omitting things, a clear opt-out must build immediately and never re-ask.
        if _wants_override(message) or _wants_placeholder(message):
            # "just build it" / "put dummy data / use a placeholder", build now and let
            # the placeholder-flag fill any year-only dates in red for the person to fix.
            self.essentials["override"] = True
            return self._build_now()

        # A bare "none" that just closed the required-sections question: carry on down the
        # path the person was on (their saved profile, or the chat draft), with no model
        # reply in front of it, since the extractor's line could thank them for material
        # they never gave.
        if declined_now and not self._open_required_sections():
            return self._resume_after_required()

        # P1 (Task 1): a free-form turn (context, goals, preferences, a question to the copilot)
        # gets a conversational reply grounded in full history + recalled + facts, instead of a
        # CV-field question, and is already remembered above. Pre-build only; the build/tailor
        # funnel below is unchanged for everything else.
        if self.assembled is None and self._wants_freeform_chat(message):
            return self._chat_reply(message)
        # A question about a file they uploaded, its projects or links, or their GitHub, before
        # or after the build: answered from what the app really read and holds (2026-11-06).
        if self._asks_about_upload(message):
            return self._chat_reply(message)
        # With the agent on, questions and requests about GitHub, repositories, portfolio,
        # projects, relevance, the quiz, connecting and "what can you do" go to it too. Field capture
        # above has already run; a turn whose projects it just captured stays on the funnel.
        if self._agent_on():
            from agent.resume_chat import wants_agent
            captured = ([p.get("org") or p.get("title") for p in prior_projects]
                        != [p.get("org") or p.get("title") for p in (self.essentials.get("projects") or [])
                            if isinstance(p, dict)])
            # Before the first build the intake's question is pending: only a clear question
            # leaves the funnel; an answer ("no github, no projects") stays on it.
            from agent.resume_chat import is_question
            if not captured and (wants_agent(message, pending=self.assembled is None)
                                 or (followup and is_question(message))):
                return self._agent_reply(message)
        else:
            captured = False

        # After the build, a question to the chat is answered, never taken as material for a
        # rebuild. Kofi's numbered "1. why have you already listed projects? 2. why is there
        # so much space?" read as a list of material, ran a whole rebuild ("Working on your
        # resume...", "29 job terms added") and got the intake model's guess as the answer,
        # with the question tail after it (2026-11-10).
        if self.assembled is not None and not captured:
            from agent.resume_chat import asks_something
            if asks_something(message):
                return self._chat_reply(message)

        # Enrichment: once a first draft exists, a substantive message is REAL
        # material to fold in (accomplishments/projects/skills/…), not a gate answer;
        # "that's everything" finalizes and stops the invitations.
        if self.stage in ("review", "done"):
            # Absorb any real material in this message FIRST, so content given in the
            # same breath as "that's everything" is never lost, THEN finalize.
            self._absorb_enrichment(message)
            self._settle_required_answer()
            if _wants_done(message):
                self.essentials["enrich_done"] = True
            # Nothing new for the page (a decline, "that's everything", a thank you): no
            # rebuild and no questions after it, just the reply.
            if self.assembled is not None and self._material_sig() == material_before:
                if _wants_done(message) and getattr(self.assembled, "underfull", False):
                    return self._finish_short_page()
                return self._say(self._no_build_reply(reply, message))

        return self._funnel_tail(reply)

    # What a turn can add to the page. A turn that changes none of it does not rebuild.
    _MATERIAL_KEYS = ("identity", "education", "experience", "projects", "extracurricular",
                      "interests", "skills_input", "certifications")

    def _material_sig(self) -> str:
        e = self.essentials
        try:
            return json.dumps({k: e.get(k) for k in self._MATERIAL_KEYS}, sort_keys=True,
                              default=str)
        except (TypeError, ValueError):
            return repr([e.get(k) for k in self._MATERIAL_KEYS])

    def _finish_short_page(self):
        """"That's everything" over a short page: never called done while it is half empty
        (Kofi, 2026-11-10). The page fill runs (more of their recent work, by recency, from
        their own material) and the chat says in one line what it did, and plainly when the
        page is still short."""
        out = self._fill_page_now()
        lines = []
        wrote = out.get("bullets_written_from_their_material") if out.get("ok") else None
        if wrote:
            lines.append("I filled the rest with more of your recent work. Check the new bullets "
                         "in the summary above before you send.")
        pct = self._page_fill_pct()
        short = bool(getattr(self.assembled, "underfull", False))
        if short:
            lines.append(f"The page is at {pct}%, so it isn't finished yet: "
                         "there is no more of your material on file to fill it. Send me more "
                         "any time and I'll work it in.")
        else:
            lines.append("Check the page, then <b>Accept & Save</b>.")
        text = " ".join(lines)
        if short:
            return self._say(self._fact(
                "page_still_short", text, exact=[f"{pct}%"], fill_percent=pct,
                bullets_just_written_from_their_material=len(wrote or []) or None,
                why="there is no more of their material on file to fill it",
                what_would_fill_it=list(out.get("real_material_that_would_fill_it") or [])[:3],
                next="they can send more material any time and you will work it in; the page is "
                     "not finished, so do not call it done or tell them to Accept & Save"))
        return self._say(self._fact(
            "done", text, exact=["Accept & Save"],
            bullets_just_written_from_their_material=len(wrote or []) or None,
            next="check the page (and the new bullets in the summary above, if any), then click "
                 "Accept & Save"))

    def _no_build_reply(self, reply: str, message: str) -> str:
        if _wants_done(message):
            return self._fact("done", "That's everything then. Check the page, then <b>Accept & Save</b>.",
                              exact=["Accept & Save"], next="check the page, then click Accept & Save")
        return reply or self._fact("ack", "Got it.")

    def _say(self, text: str):
        self._append("agent", text)
        self._persist()
        return self._state([text])

    def _funnel_tail(self, reply: str = ""):
        """The end of a conversation turn: ask for what the gate still needs, or build."""
        # The gate drives the flow: build when nothing's left to ask for; otherwise
        # ask conversationally for exactly what's missing (the extractor's readiness
        # reply is suppressed so it doesn't contradict the follow-up question). Under
        # an override that's the critical tier only, never the optional extras.
        gaps = self._gate()
        if gaps:
            return self._ask_missing(gaps, critical=bool(self.essentials.get("override")))
        # Skeleton complete, insist ONCE on full months for any year-only date (a hard
        # template rule). Mark them asked so we never loop: if the person doesn't give
        # real months, the build fills flagged placeholder months instead.
        if not self.essentials.get("override"):
            months = self._months_to_ask()
            if months:
                self.essentials["months_asked"] = sorted(
                    {a.lower() for a in (self.essentials.get("months_asked") or [])}
                    | {o.lower() for o, _ in months})
                self._persist()
                return self._ask_missing([ask for _o, ask in months])
        # HARD RULE (memory: complete-cv-fills-page): Extracurricular + Interests are
        # page-filling sections the CV must NOT be built without. Collect them, or an
        # explicit decline ("I have none"), BEFORE the first draft, so we never present a
        # half page. Post-build enrichment still keeps drawing out the rest.
        if self.assembled is None and not self.essentials.get("override"):
            open_req = self._open_required_sections()
            if open_req:
                # Asked already and not answered: one plain line with the two ways forward,
                # never the same question again (_nudge_required).
                if self.essentials.get("asked_required"):
                    return self._nudge_required(open_req)
                return self._ask_required_sections(open_req, lead=[reply] if reply else None)
        saved_path = (self.assembled is None and self.essentials.get("override")
                      and (self.saved_profile or {}).get("experience"))
        if saved_path and self.essentials.get("asked_required") and not self.essentials.get(
                "unattended"):
            open_req = self._open_required_sections()
            if open_req:
                return self._nudge_required(open_req)
        if reply:
            self._append("agent", reply)
        lead = [reply] if reply else None
        # The person asked to build from their saved profile ("Build it from my saved profile")
        # and was asked the outside-work question first: their answer carries on with exactly
        # that build, tailored from the saved profile. It used to fall through to a fresh chat
        # draft instead (Kofi, 2026-11-10: "1 of 25 job terms").
        if saved_path:
            return self.build_from_saved(lead=lead)
        return self.draft(lead=lead)

    # -- the required-sections question (Extracurricular / Interests) ------------------ #
    def _last_agent_said(self, text) -> bool:
        """True when the chat's last message before the person's latest one began with ``text``."""
        text = str(text or "").strip()
        if not text:
            return False
        seen_user = False
        for turn in reversed(self.history or []):
            if not isinstance(turn, dict):
                continue
            if turn.get("role") == "user" and not seen_user:
                seen_user = True
                continue
            if turn.get("role") == "agent" and seen_user:
                return str(turn.get("content") or "").strip().startswith(text[:120])
        return False

    def _required_labels(self, keys) -> list[str]:
        """The open required sections as the person reads them ("Extracurricular")."""
        heads = self.headings if isinstance(self.headings, dict) else {}
        out = []
        for k in keys:
            h = str(heads.get(k) or "").replace("\\&", "&").replace("\\", "").strip()
            out.append(h or k.replace("_", " ").capitalize())
        return out

    def _required_question_open(self) -> list[str]:
        """The required sections the person has been ASKED for and not yet answered or
        declined, pre-build. Empty when no such question is waiting on them."""
        if self.assembled is not None or not self.essentials.get("asked_required"):
            return []
        if self.essentials.get("unattended"):
            return []
        return self._open_required_sections()

    def _decline_open_required(self) -> list[str]:
        """Record a real decline of every required section still open, in the same
        ``declined`` list a typed "I have no extracurriculars" writes to (so it is saved
        and reused for the next job, §4b). Returns what was declined."""
        open_req = self._open_required_sections()
        if open_req:
            dec = set(self.essentials.get("declined", []) or [])
            dec.update(open_req)
            self.essentials["declined"] = sorted(dec)
        return open_req

    def _resume_after_required(self):
        """The required sections are settled: carry on down the path the person was on.
        A returning person who asked to build from their saved profile gets exactly that;
        everyone else continues the chat funnel (which builds once nothing is missing)."""
        if self.essentials.get("override") and (self.saved_profile or {}).get("experience"):
            return self.build_from_saved()
        return self._funnel_tail()

    @_one_voice
    @_locked
    def decline_required(self):
        """The "None, leave it off" button under the required-sections question: decline
        every open required section, then build. Deterministic, no model call."""
        self._append("user", "None, leave it off")
        if self._turn is not None:
            self._turn["message"] = "None, leave it off"
        self._decline_open_required()
        self._persist()
        if self.assembled is not None:     # a stale click after the build: nothing to do
            return self._state()
        return self._resume_after_required()

    def _nudge_required(self, open_req):
        """The build was asked for AGAIN while the required-sections question is already
        open. Re-asking the same question verbatim is the loop the person hit (2026-11-05),
        so this is one short, plain line pointing at the two ways forward. Deterministic:
        never a model line, so it can never thank them for material they did not give."""
        labels = self._required_labels(open_req)
        what = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
        them = "it" if len(labels) == 1 else "them"
        note = (f"Before I build, I need your {what}. Tell me below, or choose "
                f"<b>None, leave it off</b> and I'll build without {them}.")
        self._fact("nudge_required", note, exact=list(labels) + ["None, leave it off"],
                   sections_still_needed=labels, button="None, leave it off",
                   ask="tell you about them below, or click the button to build without them")
        self._append("agent", note)
        self._persist()
        return self._state([note])

    def _build_now(self):
        """Build immediately with whatever's on hand, per an explicit user override, leaving out any template elements they didn't provide, without re-asking."""
        self._conform_essentials()
        if self.saved_profile.get("experience"):
            return self.build_from_saved()
        if self.essentials.get("experience") or (self.essentials.get("identity") or {}).get("name"):
            # draft() builds now if nothing template-critical is missing; if a
            # location/date the template needs is still absent, it asks for just
            # that (concise) and then builds, skipping all the optional extras.
            return self.draft(lead=[self._fact("building", "On it, building with what you've given me.")])
        note = self._fact(
            "ask_missing", "Happy to build right away, but I don't have anything on file yet. Give me "
            "at least your name and one role and I'll build it immediately.",
            nothing_on_file=True, ask="their name and at least one role, then you build at once")
        self._append("agent", note)
        self._persist()
        return self._state([note])

    def _preserve_projects(self, prior: list):
        """Carry chat-captured projects across an extract_intake merge (the extractor
        re-emits an empty projects list on turns that aren't about projects, which would
        otherwise wipe them). Union by name; never drop what the person already gave."""
        cur = self.essentials.setdefault("projects", [])
        names = {str(p.get("org") or p.get("title") or "").strip().lower() for p in cur}
        for p in prior or []:
            nm = str(p.get("org") or p.get("title") or "").strip().lower()
            if nm and nm not in names:
                cur.append(p)
                names.add(nm)
        # A project the person added from GitHub keeps what came from the repository (its link,
        # dates, checked bullets) even when the extractor echoes it back thinner.
        by = {str(p.get("org") or p.get("title") or "").strip().lower(): p for p in prior or []
              if isinstance(p, dict) and p.get("source") == "github"}
        for i, p in enumerate(cur):
            old = by.get(str(p.get("org") or p.get("title") or "").strip().lower())
            if old is not None and p is not old:
                cur[i] = {**p, **{k: v for k, v in old.items() if v not in (None, "", [], {})}}

    def _preserve_education_extras(self, prior: list):
        """Carry transcript/typed courses (and location) forward when the extractor
        re-emits a degree without them -- the fix for courses vanishing on the next
        message after a transcript upload."""
        by_school = {str(d.get("school", "")).strip().lower(): d for d in prior or []}
        for d in self.essentials.get("education") or []:
            old = by_school.get(str(d.get("school", "")).strip().lower())
            if not old:
                continue
            for f in ("courses", "location"):
                if not str(d.get(f) or "").strip() and str(old.get(f) or "").strip():
                    d[f] = old[f]

    # An explicit project-list directive: a projects/repo cue plus an enumeration.
    _PROJ_ENUM = re.compile(r"\(\d+\)|(?<![\w.])\d+[.)]\s")
    _PROJ_NAME_STOP = re.compile(r",| - | using | that | which | built | to solve |\.",
                                 re.I)

    def _capture_projects(self, message: str):
        """Capture an EXPLICIT project list the person gives in chat ('add these
        projects: (1) X, (2) Y …'), with any GitHub/portfolio URL as the link. The LLM
        extractor misses these often; an explicit user directive is exactly what the
        control plane should honor deterministically. Linked personal projects are
        portfolio work, so the gate does NOT demand employment dates/locations for them."""
        msg = message or ""
        low = msg.lower()
        if "project" not in low and "repo" not in low:
            return
        # Only act on an explicit list (numbered items), not a passing mention.
        if not self._PROJ_ENUM.search(msg):
            return
        # And only when the message is ADDING projects. A removal ("drop the X
        # project"), or a numbered list about other things entirely ("confirm back:
        # 1) the Ampsel role, 2) both project links") must never be scraped into the
        # profile, the Amazon case study left phantom records like
        # org="Drop the DePaul University project from this CV" (obs #12).
        if re.search(r"\b(drop|remove|delete|confirm|verify|read back|list back)\b", low):
            return
        if not re.search(r"\b(add|include|use|put|list|here (are|is)|these are|"
                         r"i (built|made|have)|my)\b[^.\n]{0,50}\bprojects?\b"
                         r"|\bprojects?\s*(?:section)?\s*[:\-]", low):
            return
        url_m = re.search(r"https?://[^\s)]+", msg)
        url = url_m.group(0).rstrip(".,);") if url_m else ""
        if url:  # a GitHub link belongs in the header too
            self.essentials.setdefault("identity", {}).setdefault("github", url)
        # Work on the clause after the 'projects' cue, then split on the enumerators.
        tail = re.split(r"projects?\s*(?:section)?\s*[:,-]", msg, maxsplit=1, flags=re.I)
        body = tail[1] if len(tail) > 1 else msg
        chunks = [c.strip(" \t-, .;") for c in self._PROJ_ENUM.split(body)]
        existing = self.essentials.setdefault("projects", [])
        have = {str(p.get("org") or p.get("title") or "").strip().lower() for p in existing}
        for chunk in chunks:
            if not chunk or len(chunk) < 3:
                continue
            # Drop a trailing instruction sentence ("Keep the descriptions accurate…").
            chunk = re.split(r"\.\s+[A-Z]", chunk)[0].strip(" \t-, .;")
            # Strip a trailing conjunction the enumeration split leaves behind
            # ("…Equation Solver; and" -> "…Equation Solver").
            chunk = re.sub(r"[;,]?\s*and\s*$", "", chunk, flags=re.I).strip(" \t-, .;,")
            if re.search(r"\b(keep|do not|don't|accurate|overstate|please|thanks|that'?s)\b",
                         chunk, re.I) and len(chunk.split()) <= 8:
                continue
            m = self._PROJ_NAME_STOP.search(chunk)
            name = (chunk[:m.start()] if m else chunk).strip()
            name = re.sub(r"^(a set of|a|an|the)\s+", "", name, flags=re.I).strip()
            name = name.rstrip(" .,;-").strip()
            if not (2 < len(name) <= 60) or name.lower() in have:
                continue
            # The whole clause is the person's own accurate description -> one bullet.
            desc = re.sub(r"\s+", " ", chunk).strip()
            desc = desc[0].upper() + desc[1:]
            if not desc.endswith("."):
                desc += "."
            entry = {"org": name, "title": "", "dates": "", "location": "",
                     "bullets": [desc], "personal": True}
            if url:
                entry["link"] = url
            existing.append(entry)
            have.add(name.lower())

    # Explicit skills statement: "Skills: X, Y, Z", "my skills are …", "proficient/
    # skilled/experienced in/with …". Kept conservative so a normal sentence isn't scraped.
    _SKILL_CUE = re.compile(
        r"(?:my\s+|technical\s+|core\s+|key\s+)?skills?\s*(?:are|include|:)\s*([^.\n]+)"
        r"|(?:proficient|skilled|experienced)\s+(?:in|with)\s+([^.\n]+)", re.I)

    def _capture_skills(self, message: str):
        """Pull an explicitly-stated skills list into ``skills_input`` so the Skills block
        draws on the person's REAL skills (the extractor otherwise drops them, leaving the
        section thin and under the 4-category floor)."""
        m = self._SKILL_CUE.search(message or "")
        if not m:
            return
        raw = next((g for g in m.groups() if g), "")
        terms = [t.strip(" .") for t in re.split(r",|;|/|\band\b", raw) if t.strip(" .")]
        # Keep short, skill-like phrases (1-4 words), never a whole clause.
        terms = [t for t in terms if 1 <= len(t.split()) <= 4 and 2 <= len(t) <= 40]
        if not terms:
            return
        si = self.essentials.setdefault("skills_input", [])
        for t in terms:
            if t.lower() not in {x.lower() for x in si}:
                si.append(t)

    def _apply_side_channels(self, message: str):
        """Fold natural-language declines, inline course lists, stated skills, and per-entry
        locations ('BlackOrigin is in Accra, Ghana') into essentials."""
        declined = set(self.essentials.get("declined", []))
        # A question about the upload or GitHub is not a decline: "didn't you see the
        # projects ... or can't you check my github" declined both and wiped the projects
        # the upload had just added (2026-11-07 proof run).
        if not self._asks_about_upload(message):
            _note_declines(message, declined)
        self.essentials["declined"] = sorted(declined)
        # Declining projects ('I don't have projects' / 'skip this project') removes
        # any project entries so nothing incomplete renders and the gate won't re-ask
        #, the person is free to have no projects, never forced to detail one.
        if "projects" in declined and self.essentials.get("projects"):
            self.essentials["projects"] = []
            if self.profile:
                self.profile["projects"] = []
        # Only capture a real course list, never when the person is declining the
        # courses line (else "skip the courses, I have no projects" would be scraped
        # onto the CV as a course).
        if "courses" not in declined:
            courses = _capture_courses(message)
            # The pending question supplies the context: a bare list is a valid answer
            # to "what courses did you take?" without the literal cue word (obs #17).
            if not courses and self._asked_for("courses"):
                courses = _bare_course_list(message)
            if courses and self.essentials.get("education"):
                recent = _most_recent(self.essentials["education"])
                if not str(recent.get("courses") or "").strip():
                    recent["courses"] = courses
        self._capture_skills(message)   # stated skills -> skills_input (else the extractor drops them)
        self._absorb_skill_answers(message)   # "yes, I've used dbt" -> their own skill fact
        self._capture_locations(message)
        self._capture_dates(message)
        # Section router (control plane): the person's explicit placement wins over the
        # LLM, is kept there each turn, and clear volunteer work defaults to extracurricular.
        self._capture_section_moves(message)
        self._enforce_section_pins()
        self._classify_volunteer_extracurricular()

    # A "City, Region", City capitalized, Region a state/country (case-sensitive).
    _LOC_PAT = re.compile(r"[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,2},\s*[A-Z][A-Za-z]+")

    def _capture_locations(self, message: str):
        """Attach a 'City, Region' to a named experience/project entry that's still
        missing its location (e.g. 'Stanbic Bank is in Lagos, Nigeria'). Only real
        regions count, so a course list ('Machine Learning, Statistics') is ignored."""
        from tailoring.conform import is_region
        msg = message or ""
        locs = [(m.start(), m.end(), m.group(0)) for m in self._LOC_PAT.finditer(msg)
                if is_region(m.group(0).rsplit(",", 1)[1])]
        if not locs:
            return
        low = msg.lower()
        for entries in (self.essentials.get("experience", []) or [],
                        self.essentials.get("projects", []) or []):
            for j in entries:
                if not isinstance(j, dict) or str(j.get("location") or "").strip():
                    continue
                org = str(j.get("org") or j.get("title") or "").strip()
                if len(org) < 2:
                    continue
                oi = low.find(org.lower())
                if oi == -1:
                    continue
                oend = oi + len(org)
                # Nearest "City, Region" just after the org (else just before it).
                best = next((loc for s, _e, loc in locs if 0 <= s - oend <= 40), None) \
                    or next((loc for s, e, loc in locs if 0 <= oi - e <= 40), None)
                if best:
                    j["location"] = best

    def _conform_essentials(self):
        """Reformat stored dates/degrees/locations/titles to the template's
        conventions so every view (intake preview AND the built CV) matches:
        'December 2025', full degree names, 'City, XX' locations, clean job titles."""
        from tailoring.conform import (clean_title, fit_courses, format_dates,
                                        format_degree, format_location, format_school)
        _normalize_identity(self.essentials.get("identity") or {})
        for e in self.essentials.get("education", []) or []:
            if isinstance(e, dict):
                if e.get("date"):
                    e["date"] = format_dates(e["date"])
                if e.get("degree"):
                    e["degree"] = format_degree(e["degree"])
                if e.get("school"):
                    e["school"] = format_school(e["school"])   # institution only
                if e.get("courses"):
                    e["courses"] = fit_courses(e["courses"])   # ≤ 2 lines
                if e.get("location"):
                    e["location"] = format_location(e["location"])
        for j in self.essentials.get("experience", []) or []:
            if isinstance(j, dict):
                if j.get("dates"):
                    j["dates"] = format_dates(j["dates"])
                if j.get("location"):
                    j["location"] = format_location(j["location"])
                if j.get("title"):
                    j["title"] = clean_title(j["title"])
        for p in self.essentials.get("projects", []) or []:
            if isinstance(p, dict):
                if p.get("org"):
                    p["org"] = format_school(p["org"])   # institution only, no faculty
                if p.get("dates"):
                    p["dates"] = format_dates(p["dates"])
                if p.get("location"):
                    p["location"] = format_location(p["location"])
        for x in self.essentials.get("extracurricular", []) or []:
            if isinstance(x, dict) and x.get("date"):
                x["date"] = format_dates(x["date"])   # same abbreviated-month standard
        # A project tied to a school/company the person already has inherits that
        # location, so we never ask for something already shown elsewhere.
        _fill_project_locations(self.essentials.get("education", []),
                                self.essentials.get("experience", []),
                                self.essentials.get("projects", []))

    # ------------------------------------------------------------------ draft + assemble
    def _build_feed(self):
        """The live activity run for one build (ui/activity.py): the builder's own line, or the
        unattended auto-apply tailoring's, so the builder never shows a background build."""
        key = "unattended" if self.essentials.get("unattended") else "builder"
        role, company = str(self.role or "").strip(), str(self.company or "").strip()
        target = f"{role} at {company}" if role and company else (role or company)
        return _activity.run("resume", key=key, target=target)

    def _feed_read_job(self):
        _activity.step("read", "Reading the job")

    # One build told in the chat (``_builds``): the steps it took and what changed, worked out
    # in code from the same data as the change list (tailoring/changes.py), never by the model.
    BUILD_FIRST = "Built your resume"
    BUILD_AGAIN = "Updated your resume"

    def _build_failed(self, bt: dict, out) -> bool:
        """The rebuild was rolled back (no room for a keyword, a cut that cannot fit, a template
        the page does not fit): the page is as it was, and the reply already says why."""
        if not isinstance(out, dict):
            return False
        if out.get("template_ok") is False or out.get("put_back_ok") is False:
            return True
        return bt["kind"] in ("keyword", "keyword_undo") and out.get("ok") is False

    @staticmethod
    def _build_counts(ch: dict) -> dict:
        return {"reworded": len(ch.get("reworded") or []), "written": len(ch.get("written") or []),
                "cut": len(ch.get("trimmed") or [])}

    def _build_bits(self, bt: dict, out, ch: dict, cov: dict | None = None) -> list[str]:
        """The summary line's parts after the headline, at most three, in counts. A rebuild names
        what it did ("Excel added") and only the counts that moved since the last build, so the
        same "13 bullets reworded" is not said again for a keyword."""
        def n(k, one, many):
            return f"{k} {one if k == 1 else many}"
        kind, args = bt["kind"], bt.get("args") or ()
        cur = self._build_counts(ch)
        prev = next((t["build"].get("counts") for t in reversed(self.history)
                     if isinstance(t, dict) and t.get("build")), None) if kind != "draft" else None
        moved = (lambda k: cur[k] and (prev is None or cur[k] != (prev or {}).get(k)))
        bits = []
        if moved("reworded"):
            bits.append(n(cur["reworded"], "bullet reworded", "bullets reworded"))
        term = " ".join(str(args[0] if args else "").split())[:60]
        if kind == "keyword" and term:
            bits.append(f"{term} added")
        elif kind == "keyword_undo" and term:
            bits.append(f"{term} taken out")
        elif kind == "put_back":
            bits.append("1 item put back")
        elif kind == "projects" and isinstance(out, dict) and out.get("added"):
            bits.append(n(len(out["added"]), "project added", "projects added"))
        elif kind == "template":
            label = str((self.manifest or {}).get("display_name") or self.template_name or "").strip()
            if label:
                bits.append(f"{label} template")
        if moved("written"):
            bits.append(f"{cur['written']} written for you")
        if moved("cut"):
            bits.append(f"{cur['cut']} cut to fit")
        if kind == "draft":
            terms_total = int((cov or {}).get("terms") or 0)
            if ch.get("terms_added"):
                bits.append(n(len(ch["terms_added"]), "job term added", "job terms added"))
            elif not bits and terms_total:
                # A first draft from the chat has no base resume to compare with: how much of
                # the job's language the page carries instead (the Keywords bar's numbers).
                bits.append(f"{len((cov or {}).get('present') or [])} of {terms_total} job terms")
            sk = ch.get("skills") or {}
            if len(bits) < 2 and (sk.get("reordered") or sk.get("lines_moved")):
                bits.append("skills reordered")
        return bits[:3]

    def _record_build(self, bt: dict, run: dict | None, out) -> dict | None:
        """After a build that changed the page and kept it: ONE agent turn at the point the build
        started, {"role": "agent", "content": <summary>, "build": <record>}. The record holds
        the steps (verb, summary, state; no times) and the change list, so a restored chat shows
        the same collapsed summary. None when nothing was built or the build was rolled back."""
        a = self.assembled
        if a is None or self._pdf_v == bt["v0"] or self.essentials.get("unattended") \
                or self._build_failed(bt, out):
            return None
        try:
            ch = self.changes_dict()
        except Exception:  # noqa: BLE001 - the summary still says what it can
            ch = {}
        try:
            cov = self.coverage_dict() or {}
        except Exception:  # noqa: BLE001
            cov = {}
        terms = [str(t) for t in cov.get("present") or []]
        from tailoring.changes import readable
        one_page = bool(a.ok and a.compile is not None and a.compile.pages == 1)
        fill = getattr(a, "fill_ratio", None)
        pct = round(fill * 100) if (getattr(a, "underfull", False) and fill) else None
        first = not any(isinstance(t, dict) and t.get("build") for t in self.history)
        head = self.BUILD_FIRST if first and bt["kind"] == "draft" else self.BUILD_AGAIN
        page = (f"{pct}% of the page" if pct else "one page") if one_page else "over one page"
        summary = " · ".join([head] + self._build_bits(bt, out, ch, cov) + [page])
        items = readable(ch, terms)
        if not items and terms:
            items = ["Job terms on the page: " + ", ".join(terms[:8])
                     + (f" and {len(terms) - 8} more" if len(terms) > 8 else "") + "."]
        run = run or {}
        steps = [{k: ev[k] for k in ("verb", "summary", "state", "detail") if ev.get(k)}
                 for ev in run.get("events") or []]
        secs = None
        if run.get("started") and run.get("ended"):
            secs = max(1, round(run["ended"] - run["started"]))
        flag = None
        try:
            hon = next((c for c in (self.review_dict() or {}).get("checks") or []
                        if c.get("id") == "honesty" and c.get("level") == "flag"), None)
            if hon:
                flag = {"label": str(hon.get("label") or ""), "items": list(hon.get("items") or [])}
        except Exception:  # noqa: BLE001
            flag = None
        rec = {"id": str(run.get("id") or f"b{self._pdf_v}"), "kind": bt["kind"], "summary": summary,
               "steps": steps, "secs": secs, "items": items,
               "written": list(ch.get("written") or []), "cuts": list(ch.get("trimmed") or []),
               "fill": pct, "one_page": one_page, "flag": flag, "counts": self._build_counts(ch)}
        if bt["kind"] in self._KEYWORD_KINDS:
            term = " ".join(str((bt.get("args") or ("",))[0] or "").split())[:60]
            rec["added"] = [term] if term and bt["kind"] == "keyword" else []
            rec["removed"] = [term] if term and bt["kind"] == "keyword_undo" else []
            merged = self._merge_keyword_build(bt, rec, page)
            if merged is not None:
                return merged
        turn = {"role": "agent", "content": summary, "build": rec}
        self.history.insert(min(bt["h0"], len(self.history)), turn)
        try:
            self.palace.remember_turn("agent", summary)
        except Exception:  # noqa: BLE001
            pass
        self._persist()
        return rec

    # Keyword clicks (ADD on a job term, its undo) come in runs: twenty ADDs posted twenty rows
    # (Kofi, 2026-11-10). A run of them is ONE row that updates in place, its terms inside.
    _KEYWORD_KINDS = ("keyword", "keyword_undo")

    def _merge_keyword_build(self, bt: dict, rec: dict, page: str) -> dict | None:
        """Fold a keyword build into the keyword row right before it (nothing said since), and
        return that row's record, now carrying both. None when there is no such row."""
        if bt["h0"] != len(self.history) or not self.history:
            return None
        last = self.history[-1]
        prev = last.get("build") if isinstance(last, dict) else None
        if not prev or prev.get("kind") not in self._KEYWORD_KINDS:
            return None
        added = list(prev.get("added") or [])
        removed = list(prev.get("removed") or [])
        low = lambda xs: {str(x).lower() for x in xs}      # noqa: E731
        for t in rec.get("added") or []:
            if t.lower() in low(removed):
                removed = [x for x in removed if x.lower() != t.lower()]
            elif t.lower() not in low(added):
                added.append(t)
        for t in rec.get("removed") or []:
            if t.lower() in low(added):
                added = [x for x in added if x.lower() != t.lower()]
            elif t.lower() not in low(removed):
                removed.append(t)
        bits = []
        if added:
            bits.append(f"{added[0]} added" if len(added) == 1 else f"{len(added)} keywords added")
        if removed:
            bits.append(f"{removed[0]} taken out" if len(removed) == 1
                        else f"{len(removed)} keywords taken out")
        if not bits:
            bits.append("keywords put back as they were")
        summary = " · ".join([self.BUILD_AGAIN] + bits + [page])
        items = []
        if added:
            items.append("Added to your skills: " + ", ".join(added) + ".")
        if removed:
            items.append("Taken out: " + ", ".join(removed) + ".")
        items += [i for i in rec.get("items") or [] if not str(i).startswith("Job terms on the page")]
        secs = (prev.get("secs") or 0) + (rec.get("secs") or 0) or None
        merged = dict(rec, id=prev.get("id") or rec["id"], run=rec["id"], summary=summary,
                      items=items, added=added, removed=removed, secs=secs)
        last["build"] = merged
        last["content"] = summary
        self._persist()
        return merged

    @_locked
    @_builds("draft")
    def draft(self, lead=None):
        self._feed_read_job()
        return self._draft(lead)

    def _draft(self, lead=None):
        # Gate: never build while a template field is missing. Under an override we
        # still insist on the template-critical fields (locations/dates), because
        # their absence breaks the template, but skip the optional extras.
        self._conform_essentials()
        gaps = self._gate()
        if gaps:
            return self._ask_missing(gaps, lead=lead, critical=bool(self.essentials.get("override")))
        self.stage = "drafting"
        self._change_base = None           # a page drafted from the chat has no base resume
        self._last_reverted = []
        _activity.step("draft", "Drafting", "your resume from your experience")
        drafted = normalize_profile(self.llm.draft_profile(self.jd, self.essentials))
        self.profile = drafted
        self.profile["identity"] = self.essentials.get("identity", {}) or self.profile.get("identity", {})
        # The person's real education, verbatim, courses come only from them or
        # their transcript, never drafted.
        if self.essentials.get("education"):
            self.profile["education"] = [dict(e) for e in self.essentials["education"]]
        # Ask-not-invent: never fabricate projects or extracurricular. Only real
        # projects the person supplied survive (bullets for them stay inferable).
        self.profile["projects"] = [dict(p) for p in self.essentials.get("projects", []) or []]
        self.profile["extracurricular"] = [dict(x) for x in self.essentials.get("extracurricular", []) or []]
        self.profile["interests"] = str(self.essentials.get("interests") or "").strip()
        _activity.step("bullets", "Crafting bullets", "from what you told me, each sized to its line")
        self._apply_real_bullets()  # the person's REAL bullets win; flag drafted ones
        self._apply_essentials_locations()  # never drop an experience location (hard rule)
        date_flags = self._flag_placeholder_dates()  # year-only dates -> flagged placeholder months
        self._expand_terse_bullets()  # grow stubby fragments into full page-width sentences
        _activity.step("match", "Matching your experience", "to the skills this job asks for")
        self._apply_user_skills()   # the person's own skills + certifications
        self._validate_skills()     # drop any drafted skill the person's material can't support
        self._ensure_richness()
        self._fill_skills()         # foreground supported JD terms (no stretch, ragged if short)
        self._dedupe_skill_groups() # one term, one line -- no duplicate Computing/Knowledge rows
        self._apply_memory_selection()  # P1: record why these belong (reasoning only, surfaced)
        self._new_build()
        # renders a constraint-filtered copy; durable profile stays whole. fit_lines: every
        # skills line and bullet measured on the PDF and filled to the right edge (linefit.py).
        self._assemble(fit_lines=True)
        self.stage = "review"
        self._persist()   # the built profile is now remembered for next time
        msgs = list(lead or [])
        if self._bt is not None:
            self._bt["lead"] = len(msgs)
        # The build's own summary in the chat (_builds) says what the page is ("one page", or
        # "62% of the page" when short; never "a real, one-page resume" over a half page,
        # 2026-10-07), so no "Here's a first draft" line repeats it.
        return self._review_tail(msgs, date_flags)

    EDIT_TIP = "Edit any bullet right on the page, then <b>Accept & Save</b>."
    LIGHT_NOTE = ("Heads up: this resume is on the lighter side. Send me more accomplishments, a "
                  "project, or interests anytime and I'll work them in.")

    def _review_tail(self, msgs: list, date_flags, saved: bool = False):
        """What the chat says under a build's summary row, for both build paths (draft and
        build_from_saved). First the notes that are facts about this page (figures to confirm,
        what the fit cut, bullets written for the person to check, an aligned title, dates),
        and the edit tip once per chat. Then at most ONE follow-up: the projects offer at the
        first build when it applies, else one question for real material (_next_question).
        One voice per turn: Kofi got the same tip after every build and three questions in a
        row, two of them the same (2026-11-10)."""
        e = self.essentials

        def say(note):
            # Every line said here goes into the chat's history too, so a reopened chat shows
            # what was said live (the tip and the notes used to vanish on reload). The live
            # turn renders from the response and a reload from the history, never both.
            if note:
                msgs.append(note)
                self._append("agent", note)

        if saved and getattr(self.assembled, "fact_flags", None):
            from tailoring.fact_gate import describe as _describe_figures
            say(self._fact("figures_to_confirm", _describe_figures(self.assembled.fact_flags),
                           flagged=[f"{', '.join(list(f.get('figures') or f.get('claims') or [])[:3])}"
                                    f" ({f.get('label')}, {f.get('action') or 'flagged'})"
                                    for f in list(self.assembled.fact_flags)[:4] if isinstance(f, dict)],
                           how="flagged figures are not in their saved material: confirm or "
                               "strike each on the page before sending; reverted ones already "
                               "kept their own wording"))
        say(self._trims_note())
        say(self._fill_note())
        if self.title_flags():
            say(self._fact("title_aligned", "I aligned a job <b>title</b> to the role, it's "
                           "highlighted; click it to confirm or revert.",
                           how="the title is highlighted on the page; clicking it confirms or "
                               "reverts it"))
        if not e.get("edit_tip_said"):
            e["edit_tip_said"] = True
            say(self._fact("edit_tip", self.EDIT_TIP, exact=["Accept & Save"],
                           tip="they can edit any bullet right on the page, then click "
                               "Accept & Save"))
        if date_flags:
            say(self._fact("placeholder_dates", self._date_flag_note(date_flags), exact=date_flags,
                           entries=date_flags,
                           what="their start and end months were missing, so placeholder months "
                                "are filled in and shown in red on the resume",
                           next="replace them with the real months before submitting; the "
                                "template uses full months"))
        if saved:
            say(self.undated_projects_note())
        extra = self._light_extra()
        cards = self._project_offer(msgs)
        if not cards and not e.get("unattended"):
            q = self._next_question()
            if q:
                msgs.append(q)
                self._append("agent", q)
            elif extra and not e.get("light_note_said"):
                # Nothing left to ask and the page is still light: said once, never nagged.
                e["light_note_said"] = True
                note = self._fact("light", self.LIGHT_NOTE,
                                  could_add=list((extra.get("light_cv") or {}).get("suggestions") or [])[:3])
                msgs.append(note)
                self._append("agent", note)
        self._persist()
        return self._state(msgs, extra=extra, cards=cards)

    def _next_question(self) -> str:
        """The ONE question after a build, each topic asked once per chat: real bullets for a
        role that has only drafted ones, or a template section never asked about, first; then
        the job's skills the person may have, or a genuinely thin bullet (_line_question);
        then the optional extras (projects, skills, certifications). Empty when the person
        said that's everything."""
        if self.essentials.get("enrich_done"):
            return ""
        # A template section the person was never asked for (a build that skipped the question,
        # "just build it"): ONE message asking for all of them, said plainly.
        asked = set(self.essentials.get("required_asked") or [])
        never = [sec for sec in self._open_required_sections() if sec not in asked]
        if never:
            return self._required_ask_text(never)
        first = self._enrich_invite(lambda k: k.startswith("bullets:"))
        if first:
            return first
        line = self._line_question()
        if line:
            return line
        return self._enrich_invite()

    # -- one proactive offer after the first build (plan PR 10) ------------------------- #
    PROJECT_OFFER = "projects"
    PROJECT_OFFER_BUTTONS = (
        {"label": "Check my GitHub", "send": "Check my GitHub for projects that fit this job"},
        {"label": "Build a project for this job", "send": "Build me a project for this job"},
    )
    # Neither connected nor a username on the resume: nothing to check yet, so the first button
    # connects (one click), and the chat checks the repos by itself once it is connected.
    PROJECT_OFFER_CONNECT = {"label": "Connect GitHub", "nav": "github"}

    def _project_offer(self, msgs: list) -> list:
        """At the first build in this chat (agent flag on): when the job asks for skills the
        resume does not show and its projects are weak for this job (none of them names a skill
        the job asks for, or there are none), ONE message with two buttons, "Check my GitHub"
        and "Build a project for this job". Decided from what the session holds, so no model
        call is made until a click (a button sends its line as the person's next message). Never
        repeated in this session. Returns the cards for this turn's state."""
        if self._project_offered or not self._agent_on() or self.assembled is None \
                or self.essentials.get("unattended"):
            return []
        self._project_offered = True       # judged at the first build only, offered or not
        if any((c or {}).get("offer") == self.PROJECT_OFFER for t in self.history
               if isinstance(t, dict) for c in [t.get("card")] + list(t.get("cards") or [])
               if isinstance(c, dict)):
            return []
        try:
            cov = self.coverage_dict()
        except Exception:      # noqa: BLE001 - no coverage, no offer
            return []
        missing = [str(t) for t in cov.get("missing") or [] if str(t).strip()]
        if not missing or not self._projects_weak_for_job(
                list(cov.get("present") or []) + list(cov.get("missing_supported") or []) + missing):
            return []
        named = missing[:2]
        skills = named[0] if len(named) == 1 else f"{named[0]} and {named[1]}"
        gh = self._github_view()           # checked first: never offer a check it cannot do
        can_check = bool(gh.get("connected") or gh.get("resume_login"))
        lead = (f"This job asks for {skills}, and your resume does not show "
                f"{'it' if len(named) == 1 else 'them'} yet. A project can. ")
        if can_check:
            text = lead + "I can check your GitHub for one that fits, or help you build one for this job."
            buttons = [dict(b) for b in self.PROJECT_OFFER_BUTTONS]
        else:
            text = lead + ("Connect your GitHub and I'll check your repos for one that fits, or I can "
                           "help you build one for this job.")
            buttons = [dict(self.PROJECT_OFFER_CONNECT), dict(self.PROJECT_OFFER_BUTTONS[1])]
        card = {"type": "buttons", "offer": self.PROJECT_OFFER, "buttons": buttons}
        self._fact("project_offer", text, exact=named, job_skills_not_shown=named,
                   github_can_be_checked=can_check,
                   buttons_below=[b["label"] for b in buttons],
                   point_to="a project can show those skills; the buttons below check their "
                            "GitHub" + ("" if can_check else " once connected") + " or build one "
                            "for this job")
        msgs.append(text)
        self._append("agent", text)
        self.history[-1]["card"] = card
        self._persist()
        return [card]

    def _projects_weak_for_job(self, terms: list) -> bool:
        """True when this resume has no project, or none of its projects names any of the job's
        skills (``terms``) in its name, tools or bullets."""
        projs = self._resume_projects(github_too=True)
        if not projs:
            return True
        pats = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(str(t).strip()) + r"(?![A-Za-z0-9])", re.I)
                for t in terms if str(t).strip()]

        def text(p):
            bits = [p.get(k) for k in ("org", "title", "name", "tools", "description")]
            for b in p.get("bullets") or []:
                bits.append(b.get("text") if isinstance(b, dict) else b)
            return " ".join(str(x) for x in bits if x)

        return not any(pat.search(text(p)) for p in projs for pat in pats)

    def undated_projects_note(self) -> str:
        """One honest line when this template shows project dates and a rendered project has
        none (project dates are SOFT, so the build went ahead): "Without dates, X reads as
        undated; recruiters may weigh it less." Empty when every shown project is dated."""
        if self.sections and "projects" not in self.sections:
            return ""
        used = getattr(self.assembled, "profile_used", None) or self.profile or {}
        names = [str(p.get("title") or p.get("org") or "").strip()
                 for p in (used.get("projects") or []) if isinstance(p, dict)
                 and not str(p.get("dates") or "").strip()]
        names = [n for n in names if n]
        if not names:
            return ""
        who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        if len(names) == 1:
            text = f"Without dates, {who} reads as undated; recruiters may weigh it less."
        else:
            text = f"Without dates, {who} read as undated; recruiters may weigh them less."
        return self._fact("undated_projects", text, exact=names, projects_without_dates=names,
                          why="recruiters may weigh an undated project less")

    @_locked
    @_builds("draft")
    def build_from_saved(self, lead=None):
        """Build directly from the saved profile, tailored to this job, the
        response to "use what you already know about me"."""
        self._feed_read_job()
        return self._build_from_saved(lead)

    def _build_from_saved(self, lead=None):
        if self.saved_profile and self.saved_profile.get("experience"):
            # The required-section answers given in THIS chat (asked before the first build)
            # live in essentials; the saved profile may not have them yet. Without this, a
            # returning person's "I enjoy chess and hiking" was asked for and then dropped.
            base = dict(self.saved_profile)
            for k in ("extracurricular", "interests"):
                if not base.get(k) and self.essentials.get(k):
                    v = self.essentials[k]
                    base[k] = [dict(x) for x in v if isinstance(x, dict)] if isinstance(v, list) else v
            self.profile = normalize_profile(base)
            # HARD RULE (memory: complete-cv-fills-page): the template's page-filling
            # sections are collected BEFORE the first build and never deferred to post-build
            # enrichment. Deferring is exactly what shipped half pages: the CV appeared, and
            # only then did we ask about clubs and interests. This holds even under a "just
            # build it" override, that asks to skip the OPTIONAL extras, not to be handed
            # the half page this rule exists to prevent. An explicit "I have none" is still
            # the escape hatch, and §4b means it's asked once and reused for every later job.
            # Only the unattended auto-apply path skips it: nobody is there to answer.
            if not self.essentials.get("unattended"):
                open_req = self._open_required_sections()
                if open_req:
                    # Asked already and the build was requested again (the build button,
                    # "just build it"): one plain line with the two ways forward, never
                    # the same question again. That repeat was a silent loop (2026-11-05).
                    if self.essentials.get("asked_required"):
                        return self._nudge_required(open_req)
                    return self._ask_required_sections(open_req)
            # Gate the saved profile too, critical tier only under an override.
            override = bool(self.essentials.get("override"))
            missing = _missing_skeleton(*_flatten_profile(self.profile),
                                        declined=self.essentials.get("declined", []),
                                        critical_only=override)
            if missing:
                return self._ask_missing(missing, critical=override)
            self.stage = "drafting"
            self._apply_essentials_locations()  # never drop an experience location (hard rule)
            date_flags = self._flag_placeholder_dates()  # year-only -> flagged placeholder months
            _activity.step("match", "Matching your experience", "to the skills this job asks for")
            self._validate_skills()  # drop any saved skill the person's material can't support
            self._fill_skills()   # foreground supported JD terms (no stretch, ragged if short)
            self._dedupe_skill_groups()  # one term, one line
            self._apply_memory_selection()  # P1: record why these belong (reasoning only, surfaced)
            # reword the saved bullets toward THIS job, from a constraint-filtered COPY so a
            # 'do not mention X' never renders; the durable self.profile stays whole (restored below)
            self._new_build()
            import copy as _copy
            self._change_base = _copy.deepcopy(normalize_profile(self.saved_profile))
            self.assembled = assemble_cv(
                self.template, self._render_profile(), self.jd, self.llm, self.workdir,
                jobname=self.jobname, tailor=True, sections=self.sections,
                headings=self.headings, choices=self._choices_plan(),
                keep=self.kept_trims, material=self._fit_material(),
                fit_lines=True, skill_pool=self._skill_pool(),
                skill_cap=self._skill_cap(), skill_keep=self._skill_keep())
            self._last_reverted = list(getattr(self.assembled, "reverted", None) or [])
            self._written = list(getattr(self.assembled, "written", None) or [])
            self.profile = self._whole_profile_after_render(self.assembled.profile_used)
            self._pdf_v += 1
            self.dirty = False
            self._after_build()
            self.stage = "review"
            self._persist()
            # The build's summary in the chat (_builds) says what was built; no lead line
            # repeats it. What was said before the build (the reply to the answer that let it
            # start) stays above it.
            msgs = list(lead or [])
            if self._bt is not None:
                self._bt["lead"] = len(msgs)
            return self._review_tail(msgs, date_flags, saved=True)
        if self.essentials.get("experience"):
            return self.draft(lead=[self._fact("building", "Using what I already have on file.")])
        note = self._fact("ask_missing", "I don't have a saved profile yet, tell me your name and "
                          "work history and I'll build one.", no_saved_profile=True,
                          ask="their name and work history")
        self._append("agent", note)
        return self._state([note])

    def _ensure_richness(self):
        # Real, JD-relevant skills grouped like the template, no company/location
        # filler. (draft_profile may already provide skills; only fill if empty.)
        if not self.profile.get("skills"):
            from tailoring.keywords import (SKILL_CATEGORIES, classify_skill,
                                            supported_skills, term_present)
            # Group across ALL the CV's categories. This used to sort into exactly two
            # buckets and emit two skinny lines, which is the 4-5 category rule broken by
            # construction (memory skills-five-categories-fill-page): no amount of real
            # material could produce a third row.
            groups: dict[str, list[str]] = {c: [] for c in SKILL_CATEGORIES}
            # Only skills the person's OWN material supports -- never bare JD terms, and
            # never a term that lives only in a drafted placeholder bullet (validate against
            # the same genuine-material source _validate_skills uses), which would be
            # fabrication when the profile carries no skills of its own.
            genuine = self._skill_source_text()
            for t in supported_skills(self.jd, self.profile):
                if not term_present(t, genuine):
                    continue
                groups[classify_skill(t)].append(t)
            # Keep the categories' order stable, and drop the empties: a category with no
            # real terms behind it is a blank row, not a filled page.
            self.profile["skills"] = {
                cat: ", ".join(list(dict.fromkeys(terms))[:10])
                for cat in SKILL_CATEGORIES if (terms := groups[cat])
            }

    def _apply_real_bullets(self):
        """The person's REAL accomplishment bullets (from an uploaded CV or the
        enrichment interview) always win over drafted ones, and every role is flagged
        drafted_bullets True/False DETERMINISTICALLY here, never trusting the model
        to set it, so the 'light CV' detector reliably fires the enrichment interview
        for roles that still have only placeholder prose."""
        real = {}
        for j in self.essentials.get("experience", []) or []:
            org = str(j.get("org") or "").strip().lower()
            bl = [str(b).strip() for b in (j.get("bullets") or []) if str(b).strip()]
            if org and bl:
                real[org] = bl
        for e in self.profile.get("experience", []) or []:
            org = str(e.get("org") or "").strip().lower()
            for r in (e.get("roles") or [e]):
                if org in real:
                    r["bullets"] = list(real[org])   # real content replaces any draft
                    r["drafted_bullets"] = False
                else:
                    r["drafted_bullets"] = True       # placeholder -> light -> enrich

    def _apply_essentials_locations(self):
        """The org-line location under Experience is a HARD template requirement
        (every company shows its city/country), and the drafting model can't be
        trusted to preserve it. Deterministically copy each experience entry's
        location from the person's essentials or saved profile, matched by company
        name, onto the profile that gets rendered, so a location is never dropped."""
        loc: dict[str, str] = {}
        for src in (self.essentials.get("experience", []),
                    (self.saved_profile or {}).get("experience", [])):
            for j in src or []:
                if not isinstance(j, dict):
                    continue
                org = str(j.get("org") or j.get("company") or "").strip().lower()
                where = str(j.get("location") or "").strip()
                if org and where:
                    loc.setdefault(org, where)
        for e in self.profile.get("experience", []) or []:
            org = str(e.get("org") or "").strip().lower()
            if org in loc and not str(e.get("location") or "").strip():
                e["location"] = loc[org]

    def _flag_placeholder_dates(self) -> list[str]:
        """The template shows FULL-month date RANGES on EVERY dated section, Experience,
        Projects AND Extracurricular, so a year-only date ('2021') breaks consistency.
        The normal gate insists on the months; under a build override we don't block, instead fill placeholder months and MARK them so the assembler renders them in a
        warning colour and the person sees exactly which to replace. Never ships an
        invented date silently. Returns the flagged entry names."""
        from tailoring.conform import dummy_months
        flagged: list[str] = []

        def _flag(entry: dict, field: str, name: str):
            d = str(entry.get(field) or "").strip()
            if d and date_lacks_month(d):
                entry[field] = dummy_months(d)
                entry["dates_placeholder"] = True
                if name and name not in flagged:
                    flagged.append(name)

        for e in self.profile.get("experience", []) or []:
            org = str(e.get("org") or "").strip()
            for r in (e.get("roles") or [e]):
                _flag(r, "dates", org)
        for p in self.profile.get("projects", []) or []:
            _flag(p, "dates", str(p.get("title") or p.get("org") or "").strip())
        for x in self.profile.get("extracurricular", []) or []:
            _flag(x, "date", str(x.get("title") or "").strip())
        return flagged

    @staticmethod
    def _date_flag_note(orgs: list[str]) -> str:
        who = orgs[0] if len(orgs) == 1 else ", ".join(orgs[:-1]) + " and " + orgs[-1]
        return (f"I didn't have the months for {who}, so placeholder months show in <b>red</b>. "
                f"Replace them with the real months before you send.")

    def _expand_terse_bullets(self):
        """Typed notes arrive as stubby fragments ('Built SQL reporting pipelines') or
        a single comma-list that merges several distinct accomplishments, both render
        as weak, half-empty bullets. Rebuild each real-content role's (and each
        extracurricular entry's) bullets: split merged accomplishments into separate
        bullets and expand each into a full, page-width sentence, using only the
        person's own material (no invented facts). Then snap every bullet to a clean
        line count so none renders as an awkward one-and-a-half lines."""
        from tailoring.filler import is_padded

        def _looks_like_notes(bullets):
            # Typed notes are short fragments ("Built SQL reporting pipelines") or one long
            # comma-list. A bullet the person already wrote as a full sentence (an uploaded
            # CV) is finished writing; expanding it is how 120-character bullets became
            # 250-character AI prose (2026-10-07).
            if len(bullets) == 1 and bullets[0].count(",") >= 2 and len(bullets[0]) > 90:
                return True
            short = [b for b in bullets if len(b) < 70 or not b.rstrip().endswith((".", "!", "%", ")"))]
            return len(short) * 2 > len(bullets)

        def _rebuild(bullets, title, expand, entry):
            bullets = [str(b).strip() for b in (bullets or []) if str(b).strip()]
            if not bullets:
                return bullets
            if expand and _looks_like_notes(bullets):
                try:
                    grown = self.llm.expand_bullets(str(title or ""), bullets, self.jd)
                    grown = [str(b).strip() for b in (grown or []) if str(b).strip()]
                    # The expansion must say more because the notes said more, never because
                    # it reached for filler. One padded bullet discards the whole expansion.
                    joined = " ".join(bullets)
                    if grown and not any(is_padded(joined, g) for g in grown):
                        bullets = grown
                except Exception:
                    pass
            return self._fit_bullets_to_lines(bullets, entry)

        for e in self.profile.get("experience", []) or []:
            for r in (e.get("roles") or [e]):
                # Expand only roles the person gave REAL bullets for (drafted-placeholder
                # roles are handled by draft_profile + the enrichment interview); snap all.
                # The grounding entry is the EMPLOYER (all its roles, titles, tech line),
                # the same unit the assembler grounds a reword in.
                r["bullets"] = _rebuild(r.get("bullets"), r.get("title"),
                                        expand=(r.get("drafted_bullets") is False), entry=e)
        for x in self.profile.get("extracurricular", []) or []:
            x["bullets"] = _rebuild(x.get("bullets"), x.get("title"), expand=True, entry=x)

    def _fit_bullets_to_lines(self, bullets, entry=None):
        """Size each bullet so its LAST line lands NATURALLY near the right margin, the template's clean ragged-but-full look, with NO forced stretch. A bullet
        that lands in the ragged middle (a half-empty last line, or a lone word spilled
        onto a new line) is reworded, tighter or fuller, creative word choice, never
        padded with invented facts, to a measured character target, then re-measured.
        A few passes until it either fills a line to the edge or runs to a naturally
        near-full 1.5-2 lines. Width is measured, so it matches the rendered PDF.

        ``entry`` is the bullets' own employer / project / activity dict. A resize may
        reword, but never claim a skill that entry doesn't show (issue #279)."""
        from tailoring.textwidth import line_fraction, text_width_pt, BULLET_WIDTH_PT
        from tailoring.fact_gate import gate_rewrite, known_material
        from tailoring.linefit import BULLET_LAST_LINE_TARGET as TARGET
        from tailoring.filler import is_padded, tacked_on_terms
        from tailoring.keywords import introduced_skills, profile_text
        # The fact gate too (tailoring/fact_gate.py): a resize never names a tool, employer,
        # acronym or credential, nor states a figure, that the person's material does not.
        known = known_material(self.profile or {}, self.essentials or {}, entry or {},
                               list(bullets or []), *self._brain_material())
        # ONE anti-fabrication rule, shared with the assembler's reword gate
        # (assembler._iter_bullet_slots_with_grounding): the grounding is everything the
        # bullet's own entry says (title, org, tech line, sibling bullets), never the whole
        # profile. Grounding in the sibling bullets alone used to reject a skill the role's
        # tech line already lists, so a bullet the assembler accepted was refused here.
        grounding = "\n".join(str(x) for x in (bullets or []))
        if entry:
            grounding = profile_text(entry) + "\n" + grounding

        def score(frac):
            # How cleanly the last line lands (higher = better; >= TARGET is "good"). The
            # owner's rule (2026-11): every written line runs to the right edge, so a one-line
            # bullet must reach TARGET of the line and a longer bullet's LAST line too (the old
            # bars were 45% and 58%). Filling is only ever with the person's own detail: the
            # filler guard and the fact gate below still vet every rewrite, and the measured
            # loop on the real PDF (tailoring/linefit.py) settles what this estimate misses.
            if frac < 1.0 - 0.015:
                return 1.0 if frac >= TARGET else frac * 0.5
            part = frac - int(frac)
            if part <= 0.015:
                return 1.0                             # ends right at a line boundary
            if part >= TARGET:
                return part                            # last line runs close to the edge
            return part * 0.3                          # sparse last line / lone-word spill

        def chars_for(text, lines):
            avg = text_width_pt(text) / max(1, len(text))
            return max(1, int(0.985 * lines * BULLET_WIDTH_PT / avg))

        out = []
        for b in bullets:
            b = str(b).strip()
            if not b:
                continue
            best, best_score = b, score(line_fraction(b))
            asked: set[tuple[str, int]] = set()   # (text, target) requests already made
            nudge, rejected = 1.0, 0               # tighten the target after a rejection
            for _ in range(3):
                if best_score >= TARGET:
                    break
                frac = line_fraction(best)
                whole, part = int(frac), frac - int(frac)
                # Aim for the nearest full-line landing: trim a word-or-two spill back, or
                # fill out the current line otherwise.
                target = 1 if frac < 1.0 else (whole if part < 0.30 else whole + 1)
                tgt = max(1, int(chars_for(best, target) * nudge))
                if (best, tgt) in asked:
                    break                          # never re-issue an identical request
                asked.add((best, tgt))
                try:
                    cand = (self.llm.reword_bullet(best, [], tgt, self.jd)
                            if tgt >= len(best) else self.llm.shorten_bullet(best, tgt))
                    cand = str(cand or "").strip()
                except Exception:
                    cand = ""
                if not cand:
                    break
                if (introduced_skills(b, cand, grounding, self.jd) or is_padded(b, cand)
                        or tacked_on_terms(b, cand)
                        or gate_rewrite(b, cand, self.profile or {}, self.essentials or {},
                                        entry or {}, known=known)):
                    # Fabricated a skill, or reached the target with hollow phrases: discard
                    # it. best/target are unchanged, so an identical call would only get the
                    # identical rejection; allow ONE altered retry (a tighter target), then
                    # keep best. A short true bullet beats a long padded one.
                    rejected += 1
                    if rejected >= 2:
                        break
                    nudge *= 0.9
                    continue
                cs = score(line_fraction(cand))
                if cs > best_score:
                    best, best_score = cand, cs
            out.append(best)
        return out

    def _fill_skills(self):
        """Pack the Skills block into 4 to 5 one-line lines (at most ``_skill_cap``, a
        Certifications line included) that each reach the right margin (the owner's layout
        rule, tailoring/linefit.pack_skills), from the person's OWN skills only: the block
        itself plus ``_skill_pool`` (stated skills, brain facts, the saved profile, and JD terms
        their genuine material shows). JD terms go first (ATS alignment, CLAUDE.md section 8),
        then the terms they ADDed; vague phrases go before concrete tools when the lines are
        full; nothing is ever invented. A term that no longer fits stays in their saved skills.
        This is the estimate pass; the build then measures the real page and corrects it
        (assembler fit_lines)."""
        skills = self.profile.get("skills") or {}
        if not skills:
            return
        from tailoring.linefit import pack_skills
        packed = pack_skills(skills, self._skill_pool(), jd_text=self.jd,
                             max_lines=self._skill_cap(), keep=self._skill_keep()).skills
        self._keep_dropped_skills(skills, packed)
        self.profile["skills"] = packed

    def _skill_cap(self) -> int:
        """The most lines this template's Skills block may hold (5 unless its manifest says)."""
        from intake.template_manifest import max_skill_lines
        return max_skill_lines(self.manifest or {})

    def _skill_keep(self) -> list[str]:
        """Skills the person ADDed from a job's keywords: their own assertions, kept on the page
        over every term the job does not ask for."""
        e = self.essentials or {}
        out = [str(x) for x in (e.get("skills_from_jobs") or []) if str(x).strip()]
        out += [str(a.get("term")) for a in (getattr(self, "keyword_actions", None) or [])
                if a.get("kind") == "skill" and str(a.get("term") or "").strip()]
        return list(dict.fromkeys(out))

    def _keep_dropped_skills(self, before: dict, after: dict) -> None:
        """A skill the capped Skills block no longer shows stays the person's own: it goes to
        their stated skills (which every later build reads), never lost."""
        from tailoring.keywords import is_requirement
        from tailoring.linefit import split_terms, _tkey
        if not isinstance(before, dict) or not isinstance(after, dict):
            return
        shown = {_tkey(t) for v in after.values() for t in split_terms(v)}
        si = self.essentials.setdefault("skills_input", [])
        have = {str(x).lower() for x in si}
        for v in before.values():
            for t in split_terms(v):
                if _tkey(t) not in shown and t.lower() not in have and not is_requirement(t):
                    si.append(t)
                    have.add(t.lower())

    def _brain_skills(self) -> list[str]:
        """Skills the person told the brain they have (key ``skill``, active)."""
        st = self._brain()
        if st is None:
            return []
        try:
            from applicant.store import ACTIVE
            return [str(r["value"]) for r in st.facts(store="", key="skill", status=ACTIVE)
                    if str(r["value"] or "").strip()]
        except Exception:      # noqa: BLE001
            return []

    def _genuine_text(self) -> str:
        """Everything the person's own material says, for vetting a skill: their stated
        skills, real bullets, education, projects (``_skill_source_text``), plus the saved
        profile and the brain's facts. Never a drafted placeholder bullet."""
        from tailoring.keywords import profile_text
        parts = [self._skill_source_text()]
        for m in self._brain_material():
            parts.append(profile_text(m) if isinstance(m, dict) else str(m))
        parts += self._brain_skills()
        return "\n".join(parts)

    def _skill_pool(self) -> list[str]:
        """More of the person's OWN skills the Skills packer may place: JD terms their
        genuine material shows first, then their stated skills, brain skill facts and saved
        profile skills. Each must appear in their genuine material (no drafted text)."""
        from tailoring.keywords import supported_skills, term_present
        from tailoring.linefit import split_terms
        genuine = self._genuine_text()
        cands = list(supported_skills(self.jd, self.profile))
        cands += [str(x) for x in (self.essentials.get("skills_input") or [])]
        cands += self._brain_skills()
        sk = (self.saved_profile or {}).get("skills")
        if isinstance(sk, dict):
            for v in sk.values():
                cands += split_terms(v)
        elif isinstance(sk, list):
            cands += [str(x) for x in sk]
        from tailoring.keywords import is_requirement
        declined = {str(x).lower() for x in (self.essentials.get("skills_declined") or [])}
        avoid = self._avoid_token_sets()          # an active "do not mention X" wins
        out, seen = [], set()
        for t in cands:
            t = str(t).strip()
            if not t or t.lower() in seen or t.lower() in declined or is_requirement(t):
                continue
            if avoid and any(a & set(re.findall(r"[a-z0-9]+", t.lower())) for a in avoid):
                continue
            if term_present(t, genuine):
                seen.add(t.lower())
                out.append(t)
        return out

    def _fit_material(self) -> list:
        """The fact gate's material for a fit build: the brain material plus the person's
        stated skills and real bullets (``_skill_source_text``), so a skill they told us
        about may be placed on the page, and what their uploaded resumes say under each of
        their employers (``_upload_role_notes``), which the page fill may split or draw on."""
        out = list(self._brain_material()) + [self._skill_source_text()] + self._brain_skills()
        notes = self._upload_role_notes()
        return out + ([{"role_notes": notes}] if notes else [])

    _UPLOAD_TEXT: dict = {}          # (path, mtime) -> extracted text of that uploaded file

    def _upload_role_notes(self) -> dict:
        """{employer, lower case: what the person's uploaded resumes say under it}
        (tailoring/assembler.role_notes), newest file first, for the employers in their
        profile. Read once per file; local only."""
        d = self._uploads_dir()
        if d is None:
            return {}
        from intake.cv_import import extract_text
        from tailoring.assembler import role_notes
        prof = self.profile if (self.profile or {}).get("experience") else (self.saved_profile or {})
        orgs = [str(e.get("org") or e.get("company") or "").strip()
                for e in (prof or {}).get("experience") or [] if isinstance(e, dict)]
        orgs = [o for o in orgs if o]
        if not orgs:
            return {}
        out: dict = {}
        files = sorted((f for f in d.iterdir() if f.suffix.lower() in (".pdf", ".docx", ".txt")),
                       key=lambda f: f.stat().st_mtime, reverse=True)[:6]
        for f in files:
            key = (str(f), f.stat().st_mtime)
            text = self._UPLOAD_TEXT.get(key)
            if text is None:
                try:
                    text = extract_text(f)
                except Exception:      # noqa: BLE001 - an unreadable file adds nothing
                    text = ""
                self._UPLOAD_TEXT[key] = text
            for org, notes in role_notes(text, orgs).items():
                out.setdefault(org, notes)
        return out

    # ------------------------------------------------------- full-width line questions
    def _missing_jd_skills(self) -> list[str]:
        """JD skills the person's genuine material does not show, in the ad's order, not yet
        asked about or declined. These are what we ask about, never what we add."""
        from tailoring.keywords import skill_terms, term_present
        c = getattr(self.assembled, "coverage", None)
        terms = list(c.jd_terms) if c is not None else skill_terms(self.jd)
        role, company = _jd_label(self.jd)
        drop = {w.lower() for seg in (role, company) for w in re.findall(r"[A-Za-z]+", seg or "")}
        genuine = self._genuine_text()
        asked = {str(x).lower() for x in (self.essentials.get("skills_asked") or [])}
        asked |= {str(x).lower() for x in (self.essentials.get("skills_declined") or [])}
        out = []
        for t in terms:
            t = str(t).strip()
            words = set(re.findall(r"[a-z]+", t.lower()))
            if (t and not (words and words <= drop) and t.lower() not in asked
                    and not term_present(t, genuine) and t not in out):
                out.append(t)
        return out

    def _line_question(self) -> str:
        """One targeted question when the page lacks the real material for every line to run
        full (tailoring/linefit.py ``line_gaps``): first the JD skills the person may have
        ("This role asks for Looker and dbt. Have you used either?"). Asked once per
        item; the answer is saved as the person's own fact. A short skills line or a bullet that
        stops short is filled by the line fit's writing, never asked about (Kofi, 2026-11-10)."""
        gaps = getattr(self.assembled, "line_gaps", None) or {}
        if not gaps or self.essentials.get("unattended"):
            return ""
        e = self.essentials
        if gaps.get("skills_needed") or gaps.get("short_skills"):
            terms = self._missing_jd_skills()[:2]
            if terms:
                e["skills_pending"] = list(terms)
                e["skills_asked"] = list(dict.fromkeys(list(e.get("skills_asked") or []) + terms))
                if len(terms) == 1:
                    q = (f"This role asks for {terms[0]}. Have you used it? If you have, say so "
                         "and I'll add it to your skills, with any other tools you use.")
                else:
                    q = (f"This role asks for {terms[0]} and {terms[1]}. Have you used either? "
                         "Tell me which, plus any other tools you use, and I'll add them to "
                         "your skills.")
                return self._fact("ask_skills", q, exact=terms, job_skills=terms,
                                  ask="whether they have used these; any they have goes into "
                                      "their skills, with other tools they use")
        # No question about a short skills line or a bullet that stops short (Kofi, 2026-11-10):
        # the line fit fills those by writing, as a professional resume writer would.
        return ""

    _YES_ALL = re.compile(r"^\s*(yes|yeah|yep|yup|sure|both|all of them|i have|i've used (?:both|all)|"
                          r"i do|correct|definitely|absolutely)\b", re.I)
    _NO_ALL = re.compile(r"^\s*(no|nope|neither|none|not really|never|i haven'?t|i have not)\b", re.I)
    _NEG = re.compile(r"\b(not|never|no|haven'?t|hasn'?t|didn'?t|don'?t|neither|nor|without)\b", re.I)

    def _absorb_skill_answers(self, message: str):
        """The answer to "This role asks for Looker and dbt. Have you used either?": a skill
        the person says they have becomes THEIR fact (stated skills, the brain with source
        "you"), one they say they lack is never asked again. A message that is not about
        them leaves the question closed (it is not asked again either)."""
        e = self.essentials
        pending = [str(t) for t in (e.get("skills_pending") or []) if str(t).strip()]
        if not pending:
            return
        e["skills_pending"] = []
        from tailoring.keywords import term_present
        msg = str(message or "")
        low = msg.lower()
        got, no = [], []
        for t in pending:
            if term_present(t, msg):
                i = low.find(t.lower())
                before = low[max(0, i - 28):i] if i >= 0 else ""
                (no if self._NEG.search(before) else got).append(t)
        if not got and not no:
            if self._YES_ALL.search(msg) and not self._NO_ALL.search(msg):
                got = list(pending)
            elif self._NO_ALL.search(msg):
                no = list(pending)
        elif self._NO_ALL.search(msg):
            no += [t for t in pending if t not in got and t not in no]
        si = e.setdefault("skills_input", [])
        for t in got:
            if t.lower() not in {x.lower() for x in si}:
                si.append(t)
        if no:
            e["skills_declined"] = list(dict.fromkeys(list(e.get("skills_declined") or []) + no))
        if got:
            st = self._brain()
            if st is not None:
                try:
                    from applicant import facts as _F
                    for t in got:
                        _F.put(st, "skill", t, source="you", subject=f"skill:{t.lower()}",
                               source_ref=self._last_user_ref or "builder",
                               question=f"Have you used {t}?")
                except Exception:      # noqa: BLE001 - stated skills still carry it
                    pass

    def _dedupe_skill_groups(self):
        """Collapse duplicate skills across groups. _apply_user_skills adds
        Computing/Knowledge lines from the person's stated skills, but the drafted
        block often already lists those same terms under its own natural group names
        ('Languages & Tools', 'AI System Design') -- which rendered the skill twice on
        two lines. Keep a term in its FIRST group, drop later repeats, and drop any
        group left empty. Case-insensitive on the term text."""
        skills = self.profile.get("skills") or {}
        if not skills:
            return
        import re
        seen: set[str] = set()
        cleaned: dict[str, str] = {}
        for label, value in skills.items():
            kept = []
            # Split on commas OUTSIDE parentheses, so "JavaScript (Node.js, Express)" stays one
            # term instead of leaving "JavaScript (Node.js" and "Express)" fragments on the page
            # (seen on Kofi's CV, 2026-10-07). A term that is only a bare parenthetical remnant
            # or a dangling "(" is dropped.
            for t in re.split(r",(?![^()]*\))", str(value)):
                t = t.strip().strip(",")
                if t.count("(") != t.count(")"):
                    t = t.split("(")[0].strip()
                if not t:
                    continue
                key = re.sub(r"\s*\(.*\)$", "", t).lower()   # "Python (3.x)" and "Python" are one skill
                if key in seen:
                    continue
                seen.add(key)
                kept.append(t)
            if kept:
                cleaned[label] = ", ".join(kept)
        self.profile["skills"] = cleaned

    def _apply_user_skills(self):
        """Merge the person's OWN stated skills and certifications into the skills block, but only skills the drafted categories DON'T already list. Re-adding a term the
        draft already covers just creates a cross-category duplicate that _dedupe_skill_groups
        then strips, sometimes emptying a whole category and collapsing the block below the
        4-category floor. Their real material still takes priority (draft categories keep it)."""
        e = self.essentials
        user_sk = [str(s).strip() for s in (e.get("skills_input") or []) if str(s).strip()]
        certs = [str(c).strip() for c in (e.get("certifications") or []) if str(c).strip()]
        if not user_sk and not certs:
            return
        from tailoring.keywords import SKILL_CATEGORIES, classify_skill

        def _merge(existing: str, items: list[str]) -> str:
            have = [x.strip() for x in str(existing or "").split(",") if x.strip()]
            for it in items:
                if it and it.lower() not in {h.lower() for h in have}:
                    have.append(it)
            return ", ".join(have)

        skills = dict(self.profile.get("skills") or {})
        present = {t.strip().lower() for v in skills.values()
                   for t in str(v).split(",") if t.strip()}
        fresh = [s for s in user_sk if s.lower() not in present]   # not already on a draft line
        if fresh:
            # Merge each stated skill into its own category. Splitting into just two rows
            # here also fought the model: draft_profile labels its groups "Languages &
            # Tools" / "Data & Analytics", so a hard-coded "Computing" row didn't merge
            # with them, it appeared ALONGSIDE them as a rival line saying the same thing.
            for cat in SKILL_CATEGORIES:
                items = [s for s in fresh if classify_skill(s) == cat]
                if items:
                    skills[cat] = _merge(skills.get(cat, ""), items)
        if certs:
            skills["Certifications"] = _merge(skills.get("Certifications", ""), certs)
        self.profile["skills"] = skills

    # ------------------------------------------------------------------ enrichment
    def _enrich_topics(self) -> list[tuple[str, str]]:
        """The REAL material that would fill the page but isn't on file yet, as
        (key, phrase) pairs, never anything to fabricate. Roles still carrying
        drafted placeholder bullets come first (the biggest win)."""
        e = self.essentials
        declined = set(e.get("declined", []))
        override = bool(e.get("override"))
        topics: list[tuple[str, str]] = []
        # 1) Highest value: real accomplishments for roles that still carry only
        # drafted placeholder bullets. Skipped under a build-override.
        if not override:
            for j in e.get("experience", []) or []:
                org = str(j.get("org") or "").strip()
                has_real = bool([b for b in (j.get("bullets") or []) if str(b).strip()])
                if org and not has_real and f"bullets:{org.lower()}" not in declined:
                    topics.append((f"bullets:{org.lower()}",
                                   f"2-3 things you actually did or improved at {org}"))
        # 2) The template's REQUIRED page-filling sections (from its manifest). NOT optional
        # -- drawn out even under an override, and (via _enrich_invite) insisted upon until
        # provided or declined. They rank ahead of the optional extras below so we never let
        # the person "finish" on a half page. A template with no required sections (e.g. the
        # summary-first one) adds nothing here.
        from intake.template_manifest import elicit_goal
        saved = self.saved_profile or {}
        for sec in self.required_fill:
            if self._section_filled(sec, e, saved) or (declined & {sec, "extras"}):
                continue
            topics.append((sec, elicit_goal(self.manifest, sec) or f"your {sec}"))
        # 3) Optional extras (projects, skills, certs) -- offered once, skipped under an
        # override.
        if not override:
            if not (e.get("projects")) and "projects" not in declined:
                topics.append(("projects", "any real projects you'd like to feature"))
            if not (e.get("skills_input")) and "skills" not in declined:
                topics.append(("skills", "the key skills you'd want highlighted"))
            elif (len(e.get("skills_input") or []) < SKILLS_FOR_FULL_LINES
                  and not (declined & {"skills", "skills_more"})):
                # Thin: 4 to 5 full-width skills lines need roughly 24 real terms. Ask for
                # more tools and skills per area, naming what this job asks for.
                ask = self._missing_jd_skills()[:3] if self.jd else []
                named = (f" (this role mentions {', '.join(ask[:-1])} and {ask[-1]})"
                         if len(ask) > 1 else f" (this role mentions {ask[0]})" if ask else "")
                topics.append(("skills_more", "more of the tools, software and methods you use, "
                               "grouped by area, so every skills line runs full" + named))
            if not (e.get("certifications")) and "certifications" not in declined:
                topics.append(("certifications", "any certifications you hold"))
        return topics

    @staticmethod
    def _section_filled(sec: str, *sources: dict) -> bool:
        """True if any source (essentials, saved profile) already carries section ``sec``, a non-empty list (extracurricular) or non-empty string (interests, summary)."""
        for src in sources:
            v = (src or {}).get(sec)
            if isinstance(v, list) and v:
                return True
            if isinstance(v, str) and v.strip():
                return True
        return False

    def _open_required_sections(self) -> list[str]:
        """The template's REQUIRED sections still EMPTY and not explicitly declined, driven
        by the selected template's manifest (``required_fill``). The tool keeps demanding
        real material for these (never fabricated) until the person provides it or clearly
        opts the section out. A template with no required sections returns []."""
        e = self.essentials
        declined = set(e.get("declined", []))
        covered = set(e.get("required_covered") or [])
        saved = self.saved_profile or {}   # a returning user's saved profile counts as filled
        return [sec for sec in self.required_fill
                if not self._section_filled(sec, e, saved)
                and not (declined & {sec, "extras"}) and sec not in covered]

    def _settle_required_answer(self) -> None:
        """The outside-work question asks for Extracurricular and Interests in one breath. An
        answer that fills either of them answers that question: "football at weekends,
        travelling, podcasts" went into Interests and the chat then asked about clubs and
        volunteering twice more (Kofi, 2026-11-10). The rest of what that question asked is
        covered for this chat (not declined: a later answer still lands, and the next job may
        ask again)."""
        e = self.essentials
        asked = list(e.get("required_open_at_ask") or [])
        if not asked:
            return
        saved = self.saved_profile or {}
        if any(self._section_filled(sec, e, saved) for sec in asked):
            e["required_covered"] = sorted(set(e.get("required_covered") or []) | set(asked))

    def _ask_required_sections(self, open_req, lead=None):
        """Ask, BEFORE the first build, for the template's required sections: ONCE, all of
        them in one message, saying plainly that the resume won't look complete without them
        (Kofi, 2026-11-10: white space is never acceptable). An answer uses what they give;
        a skip, a refusal or "that's everything" is never asked again, and the page fill
        fills the rest with more of their recent work."""
        ask = self._required_ask_text(open_req)
        msgs = list(lead or [])
        msgs.append(ask)
        self._append("agent", ask)
        return self._state(msgs)

    def _required_ask_text(self, open_req) -> str:
        """The one message that asks for every open required section, and records it asked."""
        from intake.template_manifest import elicit_leads
        phrases = []
        for sec in open_req:
            leads = elicit_leads(self.manifest, sec)
            phrases.append(leads[0] if leads else f"your {sec}")
        guidance = self._enrich_guidance(list(open_req))
        # ANSWERED tells the model to thank the person for what they just added, so it holds
        # only sections that were really filled since the last ask. It used to be a constant
        # ["earlier"] on every re-ask, and the model duly said "Thanks for adding that" when
        # nothing had been added (2026-11-05).
        before = set(self.essentials.get("required_open_at_ask") or [])
        answered = sorted(sec for sec in before - set(open_req)
                          if sec not in set(self.essentials.get("declined") or []))
        ask = ""
        narrating = self._narrating()
        if not narrating:
            try:
                ask = str(self.llm.ask_enrich(phrases, answered,
                                              self.role, guidance,
                                              person=self._who_we_are_asking()) or "").strip()
            except Exception:
                ask = ""
        if not ask and narrating:         # the fallback of a written message: one question
            hints = {"extracurricular": "anything you do outside work, like volunteering, clubs "
                                        "or teams", "interests": "a few interests or hobbies",
                     "summary": "a line on your professional focus"}
            ask = (f"Could you tell me {_and_list([hints.get(k, f'your {k}') for k in open_req])}? "
                   "If there's none, say so and I'll leave it off.")
        if not ask:                       # deterministic, warm fallback
            ask = (" ".join(phrases) + " (If there's genuinely none, tell me and I'll leave "
                   "that off.)")
        labels = self._required_labels(open_req)
        what = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
        word = "section" if len(labels) == 1 else "sections"
        them = "it" if len(labels) == 1 else "them"
        ask = (f"Your template has {what} {word}, and your resume won't look complete without "
               f"{them}. {ask}")
        if narrating:
            self._fact("ask_required", ask, exact=labels, sections=labels,
                       why="the template has these sections and the resume won't look complete "
                           "without them",
                       ask_about=phrases, how_to_ask=self._ask_style(guidance),
                       about_the_person=self._who_brief(),
                       if_none="they can say there is none and you leave it off",
                       one_question="ask for all of these in ONE question")
        e = self.essentials
        e["asked_required"] = True
        e["required_open_at_ask"] = list(open_req)
        e["required_ask_text"] = ask[:200]
        # Asked once: never asked again, before or after the build (_enrich_invite).
        e["required_asked"] = sorted(set(e.get("required_asked") or []) | set(open_req))
        self._persist()
        return ask

    def _enrich_invite(self, only=None) -> str:
        """Draw out the next enrichment topics (at most two), each ONCE per chat. The
        page-filling sections (Extracurricular, Interests) used to be "insisted upon",
        re-offered after every build until filled or declined, and Kofi got the same
        outside-work question three times in one chat after he had answered it with his
        hobbies (2026-11-10). Now a topic asked before the build counts too, the light-resume
        banner keeps listing what is still open, and a typed decline still closes a section.
        ``only`` (key -> bool) limits the topics this call may ask."""
        e = self.essentials
        asked = set(e.get("enrich_asked", []))
        asked |= set(e.get("required_asked") or [])
        all_topics = self._enrich_topics()
        if not all_topics:
            e["enrich_done"] = True
            return ""
        topics = [(k, p) for k, p in all_topics if k not in asked and (only is None or only(k))]
        if not topics:
            return ""
        take = topics[:2]
        # ANSWERED: only what the person really added since the last ask (a topic asked then and
        # no longer open, not declined), so the model never thanks them for nothing ("Thanks for
        # rounding out the skills" after he had said nothing about skills).
        open_now = {k for k, _ in all_topics}
        declined = set(e.get("declined") or [])
        answered = [k.split(":", 1)[0] if k.startswith("bullets:") else k
                    for k in (e.get("enrich_last_ask") or [])
                    if k not in open_now and k not in declined]
        e["enrich_asked"] = sorted(set(e.get("enrich_asked", [])) | {k for k, _ in take})
        e["enrich_last_ask"] = [k for k, _ in take]
        phrases = [p for _k, p in take]
        guidance = self._enrich_guidance([k for k, _ in take])
        ask = ""
        narrating = self._narrating()
        if not narrating:
            try:
                ask = str(self.llm.ask_enrich(phrases, answered, self.role, guidance,
                                              person=self._who_we_are_asking()) or "").strip()
            except Exception:
                ask = ""
        if not ask:
            joined = phrases[0] if len(phrases) == 1 else f"{phrases[0]}, and {phrases[1]}"
            ack = "Nice, added. " if answered else ""
            ask = (f"{ack}To fill your resume out like the template, could you share "
                   f"{joined}? (Real details only, or say \u201cthat's everything\u201d and "
                   "I'll finalize.)")
        if narrating:
            self._fact("ask", ask, ask_about=phrases, how_to_ask=self._ask_style(guidance),
                       about_the_person=self._who_brief(),
                       just_added=answered or None,
                       real_details_only="they can also say that's everything and you finalize",
                       one_question="ask for these in ONE question")
        return ask

    @staticmethod
    def _ask_style(guidance) -> dict | None:
        """The template's interview style and each topic's goal and example opener, small, for
        the turn's brief (the same guidance ask_enrich gets)."""
        if not guidance:
            return None
        out = {}
        if guidance.get("style"):
            out["style"] = str(guidance["style"])[:300]
        for k, t in (guidance.get("topics") or {}).items():
            out[k] = {"goal": str(t.get("goal") or "")[:200],
                      "example_opener": (list(t.get("leads") or []) or [""])[0][:200]}
        return out or None

    def _enrich_guidance(self, keys):
        """Interview-style + per-topic goals/example openers from the template
        manifest, so the LLM phrases these naturally (especially the indirect
        extracurricular/interests prompts) instead of reading a canned question."""
        from intake.template_manifest import elicit_goal, elicit_leads, interview_style
        topics = {}
        for k in keys:
            goal, leads = elicit_goal(self.manifest, k), elicit_leads(self.manifest, k)
            if goal or leads:
                topics[k] = {"goal": goal, "leads": leads}
        style = interview_style(self.manifest)
        if not style and not topics:
            return None
        return {"style": style, "topics": topics}

    def _who_we_are_asking(self) -> dict:
        """What the interviewer knows about THIS person, so it can ask a question only they
        could be asked.

        The alternative to a bullet GENERATOR is a better question. A generator writes
        "Increased efficiency by 30%" and invites a tired applicant to accept it: the model
        authored the claim and the human rubber-stamped it. That risk is not symmetric for
        our user, who is applying for visa-sponsored roles, where a misrepresentation is
        attached to an immigration petition rather than merely an interview.

        A question cannot fabricate. But a GENERIC question ("tell me an accomplishment")
        doesn't unlock anything either, which is why the page stayed at 50.6%. ask_enrich
        received only the TARGET role, so it had no idea it was talking to an AI Systems
        Architect who spent two years at BlackOrigin. Handing it the person's own history
        lets it ask the one specific thing that jogs a real memory, which is the whole
        difference between an interview and a form.
        """
        roles = []
        for e in (self.profile.get("experience") or [])[:3]:
            org = str(e.get("org") or "").strip()
            for r in (e.get("roles") or [e]):
                title = str(r.get("title") or "").strip()
                if not (org or title):
                    continue
                roles.append({
                    "org": org, "title": title,
                    "dates": str(r.get("dates") or "").strip(),
                    # What they've ALREADY said about this role, so the question doesn't
                    # ask again for something on the page.
                    "already_said": [str(b) for b in (r.get("bullets") or [])][:4],
                })
        edu = [{"school": str(d.get("school") or ""), "degree": str(d.get("degree") or "")}
               for d in (self.profile.get("education") or [])[:2]]
        return {"name": str((self.profile.get("identity") or {}).get("name") or "").split(" ")[0],
                "roles": roles, "education": edu,
                "target_role": self.role, "target_company": self.company}

    def _who_brief(self) -> dict | None:
        """The person in a few words for the turn's brief: first name and their latest roles, so
        a question can be one only they could be asked (the slim form of _who_we_are_asking)."""
        who = self._who_we_are_asking()
        roles = [" at ".join(x for x in (r.get("title"), r.get("org")) if x)
                 for r in who.get("roles") or []][:3]
        out = {k: v for k, v in (("first_name", who.get("name")), ("roles", roles)) if v}
        return out or None

    def _cheapest_gaps_first(self) -> list[str]:
        """Real material that would fill this page, ordered by what it COSTS to answer.

        A short page was being met by asking for another accomplishment, which is the most
        expensive thing a person can produce: they must reconstruct a metric from years ago,
        and it is the one question where a tired applicant is most tempted to round up. So we
        asked for the hardest, riskiest material while ignoring easy certain material sitting
        right there.

        Coursework, certifications, languages and honors are all: trivially recallable (you
        either took the course or you didn't), impossible to fabricate by accident, and dense
        on the page (a courses line renders two lines; a project renders five). This is
        exactly how config/resume_summary.tex went 57% -> 90% and resume_banking 64% -> 90%:
        coursework, honors, a second project. The method was proven on our own templates and
        never offered to the person.

        The research also says the goal was never volume: padding a thin page with fluff
        "achieves nothing but a recruiter's eye-roll", and relevance beats length. So this
        adds more KINDS of true things, never more words about the same thing.
        """
        gaps: list[str] = []
        declined = set(self.essentials.get("declined", []))
        # 1. Coursework: on the CV already, costs nothing to recall, two dense lines.
        if "courses" not in declined and any(
                not str(e.get("courses") or "").strip()
                for e in (self.profile.get("education") or [])):
            gaps.append("the relevant courses from your degree")
        # 2. Certifications: a fact you either hold or don't.
        if not (self.essentials.get("certifications") or []) and "certifications" not in declined:
            gaps.append("any certifications or licences you hold")
        # 3. Honors and languages: one line each, zero recall cost.
        if "honors" not in declined:
            gaps.append("any honours, awards, or scholarships")
        # 4. Only THEN the expensive ask. It's the strongest thing a reader sees, so it's
        #    worth asking for, just not first and not instead of the easy wins above.
        recent = next((str(e.get("org") or "").strip()
                       for e in (self.profile.get("experience") or [])
                       if str(e.get("org") or "").strip()), "")
        gaps.append(f"another accomplishment from {recent}" if recent
                    else "another accomplishment from your most recent role")
        return gaps

    def _light_extra(self):
        """A structured 'this CV is light' flag for the UI, with concrete, real
        things the person could add. Present whenever placeholder bullets remain or
        a whole fill-the-page section is empty; absent once the CV stands on its own."""
        declined = set(self.essentials.get("declined", []))
        drafted_orgs, sugg = [], []
        for _rid, ent, r in self._roles():
            if r.get("drafted_bullets"):
                org = str(ent.get("org") or "").strip()
                drafted_orgs.append(org)
                if org:
                    sugg.append(f"real accomplishments for {org}")
        # Only nag about a section the person hasn't already opted out of.
        no_projects = not self.profile.get("projects") and "projects" not in declined
        if no_projects:
            sugg.append("a project (real, or I can suggest one to build)")
        # Suggest each of the template's still-open REQUIRED sections (manifest-driven), so
        # a template without extracurricular/interests never nags about them.
        _hints = {"extracurricular": "anything you do outside work (volunteering, clubs, teams)",
                  "interests": "a few interests or hobbies", "summary": "a line on your professional focus"}
        for sec in self._open_required_sections():
            sugg.append(_hints.get(sec, f"your {sec}"))
        # The RENDERED page is the honest signal. Every test above is a proxy, and a CV can
        # satisfy all of them (real bullets, a project, no open sections) and still land at
        # half a page -- which is exactly what shipped. Measured fill outranks the proxies.
        underfull = bool(self.assembled is not None and self.assembled.underfull)
        if underfull and not drafted_orgs:
            sugg[:0] = self._cheapest_gaps_first()
        # An empty required section leaves the page half-built, so the CV is "light" until
        # those are filled or explicitly declined -- not only for placeholder bullets/projects.
        light = (bool(drafted_orgs) or no_projects
                 or bool(self._open_required_sections()) or underfull)
        if not light:
            return None
        # They've said there is nothing left. Insisting past that is nagging, and the
        # research is blunt that a padded page reads WORSE than a short honest one ("nothing
        # but a recruiter's eye-roll"), so there is nothing to win by pushing. enrich_done
        # already silences the invite; it must silence this too, or the invite goes quiet
        # while the banner keeps saying the same thing.
        if self.essentials.get("enrich_done"):
            return None
        return {"light_cv": {"light": True, "suggestions": sugg[:4]}}

    def _absorb_enrichment(self, message: str):
        """Fold a REAL enrichment answer into essentials, then re-conform."""
        try:
            self.essentials = self.llm.absorb_enrichment(self.jd, self.essentials, message)
        except Exception:
            pass
        self._conform_essentials()

    # -- switching the template in a running chat (Kofi, test build 13, 2026-11-09) ------ #
    TEMPLATE_SWITCHED = "Switched to the {name} template."
    TEMPLATE_FAILED = ("Your resume didn't fit one clean page in the {name} template, so I kept "
                       "{old}.")

    @_one_voice
    @_locked
    @_builds("template")
    def set_template(self, name: str, template_source: str):
        """Switch this chat's template (the builder's "change", then "Use this template"). The
        job, the chat, the profile and every answer stay. Built already: rebuild the page in the
        new template through the normal assemble path, which keeps it one page with no new
        overfull lines (assemble_cv's self-healing); a page that cannot fit rolls back to the old
        template and says so. Not built yet: the next build uses it. One short chat line either
        way. Returns the session state, with ``template_ok``."""
        from intake.template_manifest import (headings, identity_fields, load_manifest,
                                               required_sections, sections)
        keep = {k: getattr(self, k, None) for k in (
            "template", "template_name", "manifest", "identity_fields", "sections", "headings",
            "required_fill", "assembled", "profile", "_written", "_pdf_v", "dirty")}
        old_label = str((self.manifest or {}).get("display_name") or self.template_name or "")
        man = load_manifest(name)
        label = str((man or {}).get("display_name") or name)
        self.template, self.template_name, self.manifest = template_source, name, man
        self.identity_fields = identity_fields(man)
        self.sections, self.headings = sections(man), headings(man)
        self.required_fill = required_sections(man)
        built = self.assembled is not None
        if built:
            try:
                self._assemble()
                ok = bool(self.assembled and self.assembled.ok)
            except Exception:  # noqa: BLE001 - a failed build keeps the old template
                ok = False
            if not ok:
                for k, v in keep.items():
                    setattr(self, k, v)
                try:                     # the failed try overwrote the PDF: build the old one again
                    self._assemble()
                except Exception:  # noqa: BLE001
                    for k, v in keep.items():
                        setattr(self, k, v)
                st = self._state([])
                st.update({"template_ok": False,
                           "template_error": self.TEMPLATE_FAILED.format(name=label, old=old_label)})
                return st
        if built:
            # The rebuild is told in the chat by its own summary (_builds), with the template
            # named in it, so no second line says the same.
            self._persist()
            st = self._state([])
            st["template_ok"] = True
            return st
        msg = self.TEMPLATE_SWITCHED.format(name=label)
        self._append("agent", msg)
        self._persist()
        st = self._state([msg])
        st["template_ok"] = True
        return st

    def _assemble(self, fit_lines: bool = False):
        # Render from a constraint-filtered COPY so a 'do not mention X' never reaches the CV,
        # while self.profile (persisted) stays whole; restore any hidden items into the durable
        # profile afterward. With no active constraints both are no-ops (behavior unchanged).
        # ``fit_lines`` (a fresh build only, never a rebuild after the person's own edit, which
        # must not be reworded behind their back) runs the full-width line loop.
        self.assembled = assemble_cv(
            self.template, self._render_profile(), self.jd, self.llm, self.workdir,
            jobname=self.jobname, tailor=False, sections=self.sections, headings=self.headings,
            choices=self._choices_plan(), keep=self.kept_trims,
            material=self._fit_material() if fit_lines else self._brain_material(),
            fit_lines=fit_lines, skill_pool=self._skill_pool() if fit_lines else (),
            # A fresh build may write bullets for a short page; a rebuild after the person's
            # own edit never writes behind their back.
            fill_writes=fit_lines,
            skill_cap=self._skill_cap(), skill_keep=self._skill_keep(),
        )
        if fit_lines:
            self._written = list(getattr(self.assembled, "written", None) or [])
        else:
            on_page = {str(b) for _r, _e, r in _ranked(self.assembled.profile_used)
                       for b in r.get("bullets") or []}
            self._written = [w for w in self._written if w.get("bullet") in on_page]
        self.profile = self._whole_profile_after_render(self.assembled.profile_used)
        self._pdf_v += 1
        self.dirty = False
        self._after_build()

    # ------------------------------------------------------------------ review actions
    def _roles(self):
        out = []
        for ei, e in enumerate(self.profile.get("experience", [])):
            for ri, r in enumerate(e.get("roles") or [e]):
                out.append((f"{ei}-{ri}", e, r))
        return out

    def title_flags(self):
        return [
            {"id": rid, "suggested": r.get("title", ""), "original": r.get("title_original", "")}
            for rid, _e, r in self._roles() if r.get("title_suggested")
        ]

    @_locked
    def set_title(self, role_id: str, decision: str, custom: str = ""):
        from intake.conversation import apply_title_decision
        for rid, _e, r in self._roles():
            if rid == role_id:
                apply_title_decision(r, decision, custom)
                break
        self._assemble()
        return self._state()

    @_locked
    def edit_bullet(self, ref: str, text: str):
        import copy as _copy
        slots = self._bullet_slots()
        if ref in slots:
            bl, i = slots[ref]
            before = _copy.deepcopy(self.profile)
            bl[i] = str(text or "").strip()[: self._FIELD_LIMIT]
            self.dirty = True
            self._assemble()   # recompile so the PDF preview (the source of truth) updates
            a = self.assembled
            if (a is None or not a.ok or (a.compile and a.compile.pages and a.compile.pages > 1)
                    or a.attempts > 1):   # the builder had to cut text to fit: not what was typed
                self.profile = before
                self._assemble()
                st = self._state(); st["edit_ok"] = False
                st["edit_error"] = "That change would push the resume onto a second page, so it was not kept. Shorten it, or trim something else first."
                return st
            self._persist()
        st = self._state(); st["edit_ok"] = True
        return st

    # -- whole-page editing ------------------------------------------------------------- #
    # Every text on the edit page has a field address ("field path"), so the person can change
    # anything, not only bullets (Kofi, 2026-10-08). The one rule is the one page: an edit that
    # pushes the resume past one page is refused and the page stays as it was, with a plain
    # message. Edits never touch anything but the person's own profile text.
    _FIELD_LIMIT = 5000      # a sanity bound only; the one-page check is the real limit

    def _field_slots(self):
        """Address -> (container dict, key). Covers identity, education, skills, summary,
        interests, and the org/title/location lines of every entry. Bullets keep their own
        addresses (edit_bullet) and dates their picker (edit_date)."""
        p = self.profile
        slots = {}
        ident = p.setdefault("identity", {}) if isinstance(p.get("identity"), dict) else {}
        for k in ("name", "address", "city", "phone", "email", "linkedin", "github", "blog", "website"):
            slots[f"id-{k}"] = (ident, k)
        # Only sections this template renders are editable; a field that never reaches the page
        # would accept text the person cannot see.
        shown = set(self.sections or ()) if self.sections else {"summary", "interests"}
        for k in ("summary", "interests"):
            if k in shown or not self.sections:
                slots[k] = (p, k)
        for i, e in enumerate(p.get("education") or []):
            if isinstance(e, dict):
                for k in ("school", "degree", "location", "courses"):
                    slots[f"edu-{i}-{k}"] = (e, k)
        skills = p.get("skills")
        if isinstance(skills, dict):
            for i, k in enumerate(list(skills.keys())):
                slots[f"skill-{i}"] = (skills, k)
        for gi, e in enumerate((p.get("projects") or []) + (p.get("experience") or [])):
            if not isinstance(e, dict):
                continue
            for k in ("org", "location"):
                slots[f"ent-{gi}-{k}"] = (e, k)
            for ri, r in enumerate(e.get("roles") or [e]):
                if isinstance(r, dict):
                    slots[f"ent-{gi}-r{ri}-title"] = (r, "title")
        for xi, it in enumerate(p.get("extracurricular") or []):
            if isinstance(it, dict):
                slots[f"extra-{xi}-title"] = (it, "title")
        return slots

    @_locked
    def edit_field(self, ref: str, text: str):
        """Set one field, rebuild, and keep the change only if the page still fits. Returns the
        state plus ``edit_ok`` and, when refused, ``edit_error``."""
        import copy as _copy
        slots = self._field_slots()
        text = str(text or "").strip()[: self._FIELD_LIMIT]
        if ref not in slots:
            st = self._state(); st["edit_ok"] = False; st["edit_error"] = "That part of the page cannot be edited."
            return st
        container, key = slots[ref]
        before = _copy.deepcopy(self.profile)
        if ref.startswith("skill-"):
            # A skills line is "Label: items". Editing the label renames the group.
            label, _, items = text.partition(":")
            if items.strip():
                new_skills = {}
                for k, v in container.items():
                    new_skills[label.strip() if k == key else k] = items.strip() if k == key else v
                self.profile["skills"] = new_skills
            else:
                container[key] = text
        elif ref == "id-name" and not text:
            st = self._state(); st["edit_ok"] = False; st["edit_error"] = "The name cannot be empty."
            return st
        else:
            container[key] = text
            if ref == "ent-" or ref.endswith("-title"):
                container.pop("title_suggested", None)      # the person's own title wins
        renamed = self._rename_items(before, self.profile)   # a renamed role keeps its choices
        self.dirty = True
        self._assemble()
        a = self.assembled
        if (a is None or not a.ok or (a.compile and a.compile.pages and a.compile.pages > 1)
                    or a.attempts > 1):   # the builder had to cut text to fit: not what was typed
            self.profile = before
            if renamed:
                self._rename_items(None, None, back=renamed)
            self._assemble()
            st = self._state(); st["edit_ok"] = False
            st["edit_error"] = "That change would push the resume onto a second page, so it was not kept. Shorten it, or trim something else first."
            return st
        self._persist()
        st = self._state(); st["edit_ok"] = True
        return st

    def _bullet_slots(self):
        slots = {}
        for gi, e in enumerate(self.profile.get("projects", []) + self.profile.get("experience", [])):
            for ri, r in enumerate(e.get("roles") or [e]):
                bl = r.get("bullets")
                if isinstance(bl, list):
                    for bi in range(len(bl)):
                        slots[f"g{gi}-r{ri}-b{bi}"] = (bl, bi)
        for xi, it in enumerate(self.profile.get("extracurricular", [])):
            bl = it.get("bullets")
            if isinstance(bl, list):
                for bi in range(len(bl)):
                    slots[f"x{xi}-b{bi}"] = (bl, bi)
        return slots

    def _date_slots(self):
        """Map a date field on the CV to (entry, field) so the edit-view date picker can
        update it. One namespace per dated section."""
        slots = {}
        for i, e in enumerate(self.profile.get("education", []) or []):
            if isinstance(e, dict):
                slots[f"edu-{i}"] = (e, "date")
        for gi, p in enumerate(self.profile.get("projects", []) or []):
            if isinstance(p, dict):
                slots[f"proj-{gi}"] = (p, "dates")
        for ei, e in enumerate(self.profile.get("experience", []) or []):
            if isinstance(e, dict):
                for ri, r in enumerate(e.get("roles") or [e]):
                    slots[f"exp-{ei}-{ri}"] = (r, "dates")
        for xi, it in enumerate(self.profile.get("extracurricular", []) or []):
            if isinstance(it, dict):
                slots[f"extra-{xi}"] = (it, "date")
        return slots

    @_locked
    def edit_date(self, ref: str, text: str):
        """Set a CV date from the edit-view picker: run it through the same template
        pipeline (abbreviated months + chronological validation), and if it's a real,
        valid date, clear the red placeholder flag. An impossible range is ignored."""
        from tailoring.conform import format_dates
        slots = self._date_slots()
        if ref in slots:
            entry, field = slots[ref]
            new = format_dates(str(text or ""))
            if new:                                   # empty == invalid/impossible -> keep old
                entry[field] = new
                entry.pop("dates_placeholder", None)  # a real date replaces the placeholder
                self.dirty = True
                self._assemble()   # recompile so the PDF preview updates
                self._persist()
        return self._state()

    def coverage_dict(self):
        c = self.assembled.coverage
        # Drop the JD's own company/role name (e.g. "Paradigm", "Principal") from the
        # coverage lists -- it's not a skill the person should "cover", so leaving it in
        # dragged the percentage down with noise. Boilerplate words are already filtered
        # upstream in keywords._STOPWORDS.
        role, company = _jd_label(self.jd)
        drop = {w.lower() for seg in (role, company) for w in re.findall(r"[A-Za-z]+", seg or "")}
        keep = lambda terms: [t for t in terms if t.lower() not in drop]
        present, missing = keep(c.present), keep(c.missing_unsupported)
        missing_sup = keep(c.missing_supported)
        total = len(present) + len(missing) + len(missing_sup)
        ratio = round(len(present) / total * 100) if total else 100
        # ``terms`` is the denominator. With no measurable terms the ratio means nothing, so
        # the Keywords bar is hidden (app.js) rather than claiming 0% or 100% of nothing.
        # Degrees, certifications and years of experience the job asks for are listed apart
        # ("Job asks for"), with no ADD and outside the percentage: they are requirements a
        # person holds or not, never a keyword to add.
        return {"ratio": ratio, "terms": total, "present": present,
                "missing": missing, "missing_supported": missing_sup,
                "requirements": keep(list(getattr(c, "requirements", None) or []))}

    def review_dict(self):
        """An honest pre-send check on the built CV: does every skill shown trace back to the
        person's own profile (no tailor-introduced claim they cannot defend), plus easy wins,
        honest gaps, and one-page fit. Deterministic, so it never changes run-to-run."""
        from tailoring.reviewer import review_resume
        return review_resume(self.profile, self.coverage_dict(), self.assembled.status,
                             fill=getattr(self.assembled, "fill_ratio", None))

    def changes_dict(self) -> dict:
        """What changed on this page against the base resume (tailoring/changes.py): bullets
        reworded, roles hidden or shown by choices, skills reordered, job terms added, what the
        fit cut and what the fact gate kept in the person's words. Deterministic."""
        from tailoring.changes import points, what_changed
        a = self.assembled
        rep = self._report()
        try:
            labels = {it["id"]: it["label"] for it in self._choice_items()}
        except Exception:  # noqa: BLE001
            labels = {}
        shown_ids = set(rep.get("shown") or [])
        hidden = [labels.get(i, i) for i in (rep.get("excluded") or {})]
        shown = [labels.get(i, i) for i, src in (rep.get("pins") or {}).items()
                 if i in shown_ids and (src or {}).get("how") in ("show", "lead")]
        ch = what_changed(self._change_base, getattr(a, "profile_used", None) or {},
                          coverage=self.coverage_dict(), hidden=hidden, shown=shown,
                          trims=getattr(a, "trims", None) or [], reverted=self._last_reverted,
                          keyword_terms=[k["term"] for k in self.keyword_actions],
                          written=self._written)
        ch["points"] = points(ch)
        return ch

    def _fill_note(self) -> str:
        """One line when the recency fill WROTE bullets to fill a short page (the person checks
        them: What changed lists each with its role), plus any role left with fewer bullets than
        its recency budget because there was no more of their material for it."""
        a = self.assembled
        parts = []
        n = len(self._written)
        if n:
            parts.append(f"I filled the rest with more of your recent work: {n} new "
                         f"bullet{'' if n == 1 else 's'} from your own material. Check "
                         f"{'it' if n == 1 else 'them'} in the summary above before you send.")
        notes = [str(x) for x in (getattr(a, "bullet_notes", None) or []) if str(x).strip()]
        if self.essentials.get("override"):
            # "Just build it": no per-role asks, only what the page did.
            notes = [x for x in notes if "Tell me more" not in x]
        parts += notes
        text = " ".join(parts)
        return self._fact("filled_page", text,
                          bullets_written_from_their_material=n or None,
                          where_to_check="the build summary above lists each new bullet" if n else None,
                          notes=notes or None)

    def _trims_note(self) -> str:
        """One line when the one-page fit cut something: nothing is cut silently."""
        n = len(getattr(self.assembled, "trims", None) or []) if self.assembled is not None else 0
        if not n:
            return ""
        if n == 1:
            text = "To fit one page I cut one item. It is in the summary above, with Put back."
        else:
            text = (f"To fit one page I cut {n} items. They are in the summary above, "
                    "each with Put back.")
        return self._fact("cut_to_fit", text, exact=["Put back"], items_cut=n,
                          where="the build summary above, each with a Put back button")

    REFINE_CHIPS = 4

    def refine_chips(self, coverage: dict | None = None) -> list:
        """Up to four "Work in X" chips: the job's terms the person's material supports but the
        page does not show yet, in the report's order. Each runs keyword_preview(term)."""
        cov = coverage if coverage is not None else self.coverage_dict()
        done = {str(a["term"]).lower() for a in self.keyword_actions}
        out = []
        for t in cov.get("missing_supported") or []:
            if str(t).lower() in done:
                continue
            out.append({"term": t, "label": f"Work in {t}"})
            if len(out) >= self.REFINE_CHIPS:
                break
        return out

    @_one_voice
    @_locked
    @_builds("put_back")
    def put_back(self, trim_id: str) -> dict:
        """Put one cut back on the page and re-run the one-page fit. When it fits (perhaps by
        cutting something else, which is then listed with its own Put back) it stays; when it
        cannot fit at all, nothing changes and the person is told why."""
        import copy as _copy
        from tailoring.assembler import restore_trim
        trim_id = str(trim_id or "").strip()
        a = self.assembled
        trim = next((t for t in (getattr(a, "trims", None) or []) if t.get("id") == trim_id), None)
        if a is None or trim is None:
            st = self._state()
            st.update({"put_back_ok": False,
                       "put_back_error": "That cut is no longer on this page. Rebuild to see the current list."})
            return st
        before_profile = _copy.deepcopy(self.profile)
        before_keep = set(self.kept_trims)
        before_ids = {t.get("id") for t in a.trims}
        if not restore_trim(self.profile, trim):
            st = self._state()
            st.update({"put_back_ok": False,
                       "put_back_error": "I could not find where that went. Add it again on the edit page."})
            return st
        self.kept_trims.add(trim_id)
        self._assemble()
        b = self.assembled
        back = bool(b and b.ok and b.compile and b.compile.pages == 1
                    and not any(t.get("id") == trim_id for t in b.trims))
        if not back:
            self.profile, self.kept_trims = before_profile, before_keep
            self._assemble()
            st = self._state()
            st.update({"put_back_ok": False,
                       "put_back_error": "That does not fit on one page, even with everything else that "
                                         "can go cut. Shorten or remove something on the edit page, then put it back."})
            return st
        self._persist()
        from tailoring.changes import public_trim
        moved = [public_trim(t) for t in b.trims if t.get("id") not in before_ids]
        def clip(text, words=10):
            w = str(text or "").split()
            return " ".join(w[:words]) + ("..." if len(w) > words else "")
        what = public_trim(trim)
        note = f"Put back: {clip(what['text'], 14)}"
        note += "" if note.endswith((".", "...")) else "."
        if moved:
            note += (" To keep one page, " + ("this" if len(moved) == 1 else f"these {len(moved)}")
                     + " went instead, and you can put "
                     + ("it" if len(moved) == 1 else "them") + " back too: "
                     + "; ".join(clip(m["text"]) for m in moved[:3]))
            note += "" if note.endswith((".", "...")) else "."
        st = self._state([note])
        st.update({"put_back_ok": True, "moved": moved})
        return st

    # ------------------------------------------------------------------ "Add this keyword"
    # One missing JD term from the coverage report becomes one reviewable edit of one bullet
    # (tailoring/keyword_action.py). The page is edited surgically and recompiled; the same
    # change is mirrored into the profile so a later rebuild keeps it; each action can be
    # undone on its own.
    def _page_profile(self) -> dict:
        return (getattr(self.assembled, "profile_used", None) or {}) if self.assembled else {}

    def keyword_preview(self, term: str) -> dict:
        from tailoring.keyword_action import preview_keyword_action
        term = " ".join(str(term or "").split())
        if self.assembled is None or not self.assembled.tex_source:
            return {"term": term, "supported": False, "ok": False,
                    "reason": "Build the resume first, then add keywords to it."}
        pv = preview_keyword_action(term, self.assembled.tex_source, self._page_profile(), self.jd,
                                    self.llm, fill_ratio=self.assembled.fill_ratio,
                                    evidence_profile=self.profile)
        if pv.ok:
            self._keyword_previews[term.lower()] = pv
        return pv.to_dict()

    @_locked
    @_builds("keyword")
    def keyword_apply(self, term: str, draft: str = "") -> dict:
        from tailoring.keyword_action import (apply_keyword_action, check_draft_for,
                                              render_interior)
        from tailoring.latex_template import LatexTemplate
        term = " ".join(str(term or "").split())
        draft = " ".join(str(draft or "").split())
        if self.assembled is None or not self.assembled.tex_source:
            return self._keyword_result(term, False, "Build the resume first, then add keywords to it.")
        pv = self._keyword_previews.get(term.lower())
        if pv is None:
            d = self.keyword_preview(term)
            pv = self._keyword_previews.get(term.lower())
            if pv is None:
                return self._keyword_result(term, False, d.get("reason") or "Nothing to add.")
        draft = draft or pv.after
        if draft != pv.after:
            problems = check_draft_for(pv, draft, self._page_profile(), self.jd, self.profile)
            if problems:
                return self._keyword_result(term, False, " ".join(problems))
        bold = bool(self._page_profile().get("bold_metrics"))
        tex_before = LatexTemplate(self.assembled.tex_source).get(pv.target_bullet).text
        res = apply_keyword_action(self.assembled.tex_source, pv.kind, pv.target_bullet, draft,
                                   pv.term, pv.before, self.workdir, self.llm, jobname=self.jobname,
                                   bold_metrics=bold)
        if not res.ok:
            return self._keyword_result(term, False, res.reason, res)
        self._mirror_keyword(pv, res.final_text)
        self._refresh_assembled(res)
        self.keyword_actions.append({
            "term": pv.term, "kind": pv.kind, "ref": pv.ref, "label": pv.label,
            "before": pv.before, "after": res.final_text, "anchor": pv.anchor,
            "tex_before": tex_before, "tex_after": render_interior(res.final_text, bold),
        })
        self._keyword_previews.pop(term.lower(), None)
        self._persist()
        return self._keyword_result(term, True, "", res)

    @_locked
    @_builds("keyword_undo")
    def keyword_undo(self, term: str) -> dict:
        from tailoring.keyword_action import remove_added_bullet, restore_bullet
        term = " ".join(str(term or "").split())
        idx = next((i for i in range(len(self.keyword_actions) - 1, -1, -1)
                    if self.keyword_actions[i]["term"].lower() == term.lower()), None)
        if idx is None or self.assembled is None:
            return self._keyword_result(term, False, f"Nothing to undo for {term}.")
        act = self.keyword_actions[idx]
        if act["kind"] == "skill":
            from tailoring.linefit import remove_term
            self.profile["skills"] = remove_term(self.profile.get("skills") or {}, act["term"],
                                                 act.get("label", ""), act.get("shed") or ())
            self._unsave_added_skill(act["term"], act.get("saved") or {})
            del self.keyword_actions[idx]
            self._assemble()
            self._persist()
            out = self._keyword_result(term, True, "")
            out["undone"] = True
            return out
        if act["kind"] == "new":
            res = remove_added_bullet(self.assembled.tex_source, act["tex_after"], self.workdir,
                                      jobname=self.jobname)
        else:
            res = restore_bullet(self.assembled.tex_source, act["tex_after"], act["tex_before"],
                                 self.workdir, jobname=self.jobname)
        self._unmirror_keyword(act)
        del self.keyword_actions[idx]
        if res.ok:
            self._refresh_assembled(res)
        else:
            self._assemble()          # the page moved on; rebuild it from the restored profile
        self._persist()
        out = self._keyword_result(term, True, "", res if res.ok else None)
        out["undone"] = True
        return out

    # ADD on a job keyword. The person clicking ADD is the person saying they have the skill,
    # the same as typing it into their profile (Kofi, 2026-11-09: "it is still the same person
    # telling you i have the skill so add it"), so it is added in one click, like Teal, Jobscan
    # and Rezi: on the best-fitting Skills line, in the job's own wording, and saved as their
    # own skill so every later resume treats it as theirs.
    @_locked
    @_builds("keyword")
    def keyword_add(self, term: str) -> dict:
        """Add ``term`` to the page's Skills block and to the saved profile. The page rebuilds
        through the normal one-page fit (compile, page count, overfull check, shrink). When
        even that cannot keep it on one page, the page stays as it was, the skill stays saved,
        and the person is told in one line."""
        import copy as _copy
        from tailoring.linefit import place_term
        from tailoring.keywords import is_requirement
        term = " ".join(str(term or "").split())[:80]
        if not term:
            return self._keyword_result(term, False, "No keyword given.")
        if self.assembled is None or not self.assembled.tex_source:
            return self._keyword_result(term, False, "Build the resume first, then add keywords to it.")
        if is_requirement(term):
            # A degree, certification or years of experience is something the job requires a
            # person to hold, not a skill: never added to Skills, never saved as a skill.
            return self._keyword_result(
                term, False, f"{term} is something the job requires, not a skill, so it is not "
                             "added to your Skills. If you hold it, add it to Education or "
                             "Certifications on the edit page.")
        saved = self._save_added_skill(term)
        before = _copy.deepcopy(self.profile)
        width_fn, avail = self._skill_widths()
        # A skill the person ADDed earlier is never pushed off the page by the next one: Kofi
        # ADDed Azure Data Factory three times because each new ADD shed the last (2026-11-10).
        pl = place_term(self.profile.get("skills") or {}, term, width_fn=width_fn, avail=avail,
                        jd_text=self.jd, keep=self._skill_keep())
        if pl.already and self._term_on_page(term):
            self._persist()
            return self._keyword_result(term, True, "")
        self.profile["skills"] = pl.skills
        self._assemble()
        a = self.assembled
        if not (a is not None and a.ok and a.compile is not None and a.compile.pages == 1
                and self._term_on_page(term)):
            self.profile = before
            self._assemble()
            self._persist()
            return self._keyword_result(
                term, False, f"No room on the page for {term}. It is saved to your profile.")
        # Terms the line gave up to stay one line remain the person's own skills.
        si = self.essentials.setdefault("skills_input", [])
        for t in pl.shed:
            if t.lower() not in {str(x).lower() for x in si}:
                si.append(t)
        self.keyword_actions.append({"term": term, "kind": "skill", "label": pl.label,
                                     "before": "", "after": term, "shed": list(pl.shed),
                                     "saved": saved})
        self._persist()
        return self._keyword_result(term, True, "")

    def _term_on_page(self, term: str) -> bool:
        from tailoring.assembler import _rendered_cv_text
        from tailoring.keywords import term_present
        a = self.assembled
        if a is None:
            return False
        return term_present(term, _rendered_cv_text(a.profile_used or {}, self.sections))

    def _skill_widths(self):
        """The Skills line width test calibrated to the real page (tailoring/linefit.py), or
        the template estimate when the page cannot be read."""
        from tailoring import linefit as _lf
        try:
            rep = _lf.measure(getattr(self.assembled, "pdf_path", None),
                              getattr(self.assembled, "profile_used", None) or {})
            if rep.items and rep.width:
                k = _lf.calibrate(rep)
                return (lambda lab, terms: k * _lf.estimate_width(lab, terms)), rep.width - 1.5
        except Exception:      # noqa: BLE001 - the estimate is good enough to place a term
            pass
        return _lf.estimate_width, None

    def _save_added_skill(self, term: str) -> dict:
        """Save an ADDed keyword as the person's own skill: their stated skills (which every
        build reads) with its provenance, and the brain with source "you". Returns what was
        new, so an undo takes back only that."""
        e = self.essentials
        low = term.lower()
        out = {"input": False, "fact": False}
        si = e.setdefault("skills_input", [])
        if low not in {str(x).lower() for x in si}:
            si.append(term)
            out["input"] = True
        src = e.setdefault("skills_from_jobs", [])
        if low not in {str(x).lower() for x in src}:
            src.append(term)
        for k in ("skills_declined", "skills_pending"):
            if e.get(k):
                e[k] = [x for x in e[k] if str(x).lower() != low]
        st = self._brain()
        if st is not None:
            try:
                from applicant import facts as _F
                from applicant.store import ACTIVE
                subject = f"skill:{low}"
                if not any(r["status"] == ACTIVE for r in st.current("skill", subject, "")):
                    _F.put(st, "skill", term, source="you", subject=subject,
                           source_ref="job_keywords",
                           question=f"Added {term} from a job's keywords")
                    out["fact"] = True
            except Exception:      # noqa: BLE001 - stated skills still carry it
                pass
        return out

    def _unsave_added_skill(self, term: str, saved: dict) -> None:
        e = self.essentials
        low = term.lower()
        if saved.get("input"):
            e["skills_input"] = [x for x in (e.get("skills_input") or []) if str(x).lower() != low]
            e["skills_from_jobs"] = [x for x in (e.get("skills_from_jobs") or [])
                                     if str(x).lower() != low]
        if saved.get("fact"):
            st = self._brain()
            if st is not None:
                try:
                    st.forget("skill", f"skill:{low}")
                except Exception:      # noqa: BLE001
                    pass

    def _keyword_result(self, term: str, ok: bool, reason: str, res=None) -> dict:
        st = self._state()
        st.update({"ok": ok, "term": term, "reason": reason,
                   "page_check": res.page_check() if res is not None else None,
                   "diff": (res.diff if res is not None and ok else "")})
        return st

    def _keyword_slots(self, label: str, text: str) -> list[dict]:
        """The matching bullet slot in BOTH the durable profile and the page profile."""
        from tailoring.keyword_action import profile_slots, _norm
        out = []
        for prof in (self.profile, self._page_profile()):
            for s in profile_slots(prof):
                if _norm(s["label"]) == _norm(label) and _norm(s["text"]) == _norm(text):
                    out.append(s)
                    break
        return out

    def _mirror_keyword(self, pv, text: str) -> None:
        if pv.kind == "new":
            for s in self._keyword_slots(pv.label, pv.anchor):
                s["bullets"].insert(s["index"] + 1, text)
        else:
            for s in self._keyword_slots(pv.label, pv.before):
                s["bullets"][s["index"]] = text

    def _unmirror_keyword(self, act: dict) -> None:
        for s in self._keyword_slots(act["label"], act["after"]):
            if act["kind"] == "new":
                del s["bullets"][s["index"]]
            else:
                s["bullets"][s["index"]] = act["before"]

    def _refresh_assembled(self, res) -> None:
        """Make the session's build describe the page after a surgical change."""
        from tailoring.assembler import TARGET_FILL, _rendered_cv_text
        from tailoring.keywords import build_coverage_report
        a = self.assembled
        a.tex_source = res.tex_source
        a.compile = res.compile
        a.pdf_path = res.compile.pdf_path if res.compile else a.pdf_path
        a.fill_ratio = res.fill_ratio
        a.ok = True
        a.status = "underfull" if (a.fill_ratio is not None and a.fill_ratio < TARGET_FILL) else "clean"
        a.coverage = build_coverage_report(self.jd, _rendered_cv_text(a.profile_used, self.sections),
                                           a.profile_used, terms=list(a.coverage.all_terms))
        self._pdf_v += 1

    # ------------------------------------------------------------------ accept + memory
    # ------------------------------------------------------------------ drafting
    def _drafting_profile(self) -> dict:
        """The best profile to draft prose from: the CV built this session if it has
        real content, else the saved profile, else the intake essentials."""
        built = self.profile if (self.profile and (self.profile.get("experience") or
                (self.profile.get("identity") or {}).get("name"))) else None
        return built or self.saved_profile or self.essentials

    def cover_letter(self, tone: str = "professional") -> dict:
        """Draft a cover letter for this JD from the person's profile (they review it)."""
        from drafting import cover_letter as _cover_letter
        out = _cover_letter(self.jd, self._drafting_profile(), self.llm,
                            role=self.role or "", company=self.company or "",
                            tone=tone or "professional")
        self.last_cover_letter = out.get("cover_letter", "")
        return out

    def screening_answers(self, questions: list[str]) -> dict:
        """Draft answers to a form's free-text screening questions from the profile."""
        from drafting import screening_answers as _screening_answers
        out = _screening_answers(self.jd, self._drafting_profile(), self.llm, questions)
        self.last_screening = out.get("answers", [])
        return out

    @_locked
    def accept(self):
        # Idempotent: once this session has produced a record, a second accept() (a
        # double-click, or a client retry on a slow first response) must return the SAME
        # record, not assemble again and add a duplicate row to the application queue.
        if getattr(self, "stage", None) == "done" and getattr(self, "_last_record_id", None):
            job = f"cv-{self._last_record_id}"
            return {"ok": True, "record_id": self._last_record_id,
                    "coverage": self.coverage_dict().get("ratio"),
                    "pdf": f"/api/cv.pdf?job={job}"}
        # A finished/exported CV must never contain an invented project presented
        # as real: block while any flagged placeholder project remains.
        flagged = self._suggested_projects()
        if flagged:
            noun = "a suggested placeholder project" if len(flagged) == 1 else \
                   f"{len(flagged)} suggested placeholder projects"
            return {"ok": False, "blocked": True, "flagged_projects": flagged,
                    "reason": (f"This resume still has {noun} ({', '.join(flagged)}). Replace it with "
                               "a real project, build one with the tips above, or remove it before "
                               "saving, a finished resume must not present an invented project as real.")}
        if getattr(self, "dirty", False) or self.assembled is None:
            self._assemble()
        # Never export a CV that didn't compile to a clean one page (§8 rollback / §9: never
        # ship an output that failed the checks). assemble_cv sets ok=False / status="overflow"
        # when even the shrink floor can't fit one page, hold it for review rather than
        # queueing a two-page or broken PDF (this also stops the autopilot queueing one).
        if not (self.assembled and self.assembled.ok):
            return {"ok": False, "blocked": True,
                    "reason": ("This resume didn't compile to a clean one page, so I've held it "
                               "for your review instead of saving a two-page or broken PDF. "
                               "Trim a little content and rebuild.")}
        cov_detail = self.coverage_dict()
        cov = cov_detail["ratio"]
        title = self._roles()[0][2].get("title", self.role) if self._roles() else self.role
        cover = (self.last_cover_letter or "").strip()
        # The application PACKAGE: everything the review screen needs to show this
        # finished application later, persisted on the record's data bag.
        data = {
            "jd_text": self.jd,
            "coverage": cov_detail,
            "cover_letter": cover,
            "screening": self.last_screening or [],
            "status": "ready",
            "source_job": self.source_job or {},   # drives the submission policy (submit/)
            # The exact tailored profile this CV was rendered from, so an ATS-friendly .docx
            # export reproduces the SAME content as the PDF (not a lossy PDF->Word convert).
            "render_profile": self._record_render_profile(),
            # The exact page (after role selection, tailoring and one-page fitting) and the
            # same material before tailoring, so a chat edit (notify/cvreview.py) starts from
            # what the person actually sees and "Show changes" can pair each bullet.
            "page_profile": getattr(self.assembled, "profile_used", None) or {},
            "base_profile": getattr(self.assembled, "source_profile", None) or {},
            "template": self.template_name or "",
            "sections": list(self.sections or []),
            "headings": dict(self.headings or {}),
        }
        # What the fit cut and what tailoring changed travel with the record, so nothing cut to
        # fit is lost and Telegram's caption says the same thing the builder did.
        data["trims"] = list(getattr(self.assembled, "trims", None) or [])
        try:
            ch = self.changes_dict()
            data["changes"] = {k: ch.get(k) for k in ("hidden", "shown", "trimmed", "points",
                                                        "terms_added", "reverted", "written")}
        except Exception:  # noqa: BLE001 - the record saves without its change list
            pass
        rep = self._report()
        if rep.get("excluded") or rep.get("applied") or self.session_choices:
            # What the person's choices did on this page (item ids and rule ids, no words).
            data["choices"] = {"shown": rep.get("shown") or [], "applied": rep.get("applied") or []}
        # Records are named for the JOB APPLIED TO, never the CV's first role title:
        # four Amazon builds once all read "Founder (Side Project)" and the person
        # couldn't tell which was which (obs #35). The template is NOT part of that name:
        # it leaked into Review, Ready to apply and cover letters ("the X \u00b7 Classic
        # position"). Variants are told apart by data.template (shown as its own meta on
        # Resumes); siblings link back via data.variant_of.
        label = (self.role or title or "Resume").strip() or "Resume"
        if getattr(self, "variant_of", None):
            data["variant_of"] = self.variant_of
        rec_id = self.records.add(role=label, company=self.company, coverage=cov,
                                  pdf_path="", jd_label=self.role, data=data)
        self._last_record_id = rec_id   # remember it so a repeat accept() is idempotent
        self._record_application(rec_id)
        job = f"cv-{rec_id}"
        if self.assembled.pdf_path and Path(self.assembled.pdf_path).exists():
            shutil.copyfile(self.assembled.pdf_path, self.workdir / f"{job}.pdf")
        # Persist everything so nothing is re-asked next time.
        self.memory.save(self.profile_name, self.profile, self.essentials,
                         self.prior_history + self.history)
        self.stage = "done"
        result = {"ok": True, "record_id": rec_id, "coverage": cov,
                  "pdf": f"/api/cv.pdf?job={job}"}
        # Bundle a drafted cover letter into the package if one was generated this session,
        # both as text and as a one-page PDF matching the CV (best-effort compile).
        if cover:
            (self.workdir / f"cover-{rec_id}.txt").write_text(cover, encoding="utf-8")
            try:
                from drafting.cover_pdf import render_cover_letter_pdf
                render_cover_letter_pdf(self.template, self._drafting_profile(), cover,
                                        self.workdir, f"cover-{rec_id}")
            except Exception:
                pass
            result["cover_letter"] = f"/api/cover_letter.txt?job=cover-{rec_id}"
        return result
