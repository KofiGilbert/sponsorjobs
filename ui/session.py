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

import re
import shutil
from pathlib import Path

from intake.palace_memory import PalaceMemory
from tailoring.assembler import assemble_cv, normalize_profile
from tailoring.conform import date_lacks_month, is_full_address


# Boilerplate a job-board paste sweeps in, "Company logo", "Apply now", a URL, etc.
# A line (or field) containing any of these isn't a real role/company title.
_JD_JUNK = ("logo", "posted", "apply now", "easy apply", "save job", "http", "www.",
            "sign in", "share this", "report this")


def _jd_label(jd_text: str) -> tuple[str, str]:
    """Best-effort (role, company) from the top of a JD. Skips scraped boilerplate
    ('Company logo for …') so the greeting never reads 'the Company logo for role';
    returns '' for either when nothing clean is found."""
    def _clean(seg: str, limit: int) -> str:
        seg = re.split(r"[.,;]", seg or "")[0].strip()[:limit]
        return "" if (not seg or any(j in seg.lower() for j in _JD_JUNK)) else seg
    lines = [ln.strip() for ln in (jd_text or "").splitlines() if ln.strip()]
    for ln in lines[:8]:
        parts = re.split(r"\s+[, \-, |·]\s+|\s+at\s+", ln, maxsplit=1)
        role = _clean(parts[0], 60)
        if not role:
            continue
        company = ""
        if len(parts) > 1:
            company = _clean(re.split(r"\s+in\s+", parts[1])[0], 40)
        return role, company
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
            "declined": list(saved_ess.get("declined") or [])}


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


def _missing_skeleton(identity: dict, education: list, experience: list,
                      projects: list | None = None, declined=None,
                      critical_only: bool = False, identity_fields=None) -> list[str]:
    """Human-readable list of template elements still missing (or not yet declined).

    Two tiers. TEMPLATE-CRITICAL (returned even when ``critical_only``): fields whose
    absence breaks the template's form, name, a contact, and the location+dates of
    every experience/project entry, plus each education entry's school/degree/date.
    OPTIONAL (only in the full gate): address, links, courses, and the "do you have
    any …" prompts. ``critical_only=True`` is used under an explicit build override.
    """
    projects = projects or []
    dec = set(declined or [])
    crit: list[str] = []    # template-critical, asked first
    opt: list[str] = []     # optional, full gate only, asked last

    if not str(identity.get("name") or "").strip():
        crit.append("your full name")
    if not str(identity.get("email") or identity.get("phone") or "").strip():
        crit.append("an email or phone number")

    for j in experience:   # every EXISTING role needs its full skeleton (critical)
        org, title = str(j.get("org") or "").strip(), str(j.get("title") or "").strip()
        where = f"your role at {org}" if org else (f"your {title} role" if title else "a role you listed")
        if not org:
            crit.append("the company for " + (f"your {title} role" if title else "a role you listed"))
        if not title:
            crit.append(f"your job title at {org or 'that company'}")
        if not str(j.get("dates") or "").strip():
            crit.append(f"the employment dates for {where}")
        if not str(j.get("location") or "").strip():
            crit.append(f"the location (city, state) for {where}")

    for d in education:    # every EXISTING degree needs school/degree/date (critical)
        sch, deg = str(d.get("school") or "").strip(), str(d.get("degree") or "").strip()
        whose = f"your {deg}" if deg else (f"your degree at {sch}" if sch else "your degree")
        if not sch:
            crit.append("the school for " + (f"your {deg}" if deg else "your degree"))
        if not deg:
            crit.append(f"the degree you earned at {sch or 'your school'}")
        if not str(d.get("date") or "").strip():
            crit.append(f"the graduation month and year for {whose}")

    if "projects" not in dec:
        for p in projects:   # every EXISTING project needs location+dates (critical)
            # A personal / portfolio project (has a repo link or is flagged personal) is
            # not employment, it legitimately has no company location or work dates, so
            # we never demand them (that was forcing friction on real GitHub projects).
            if p.get("link") or p.get("url") or p.get("personal"):
                continue
            nm = str(p.get("title") or p.get("org") or "").strip()
            label = f'your project "{nm}"' if nm else "your project"
            if not str(p.get("location") or "").strip():
                crit.append(f"the location for {label}")
            if not str(p.get("dates") or "").strip():
                crit.append(f"the dates for {label}")

    # Optional / structural prompts (full gate only, after the critical fields).
    if not experience:
        opt.append("at least one company and job title")
    if not education:
        opt.append("your school and degree")
    # Only ask for the identity slots THIS template actually shows (manifest identity_fields),
    # so a template without a blog slot never asks for a blog, etc.
    id_fields = set(identity_fields or ("name", "address", "phone", "email",
                                        "linkedin", "github", "blog"))
    # The template header shows a FULL mailing address, so a bare "City, ST" doesn't
    # satisfy the slot, ask for the whole thing (street, city, state, ZIP).
    if "address" in id_fields and "address" not in dec and not is_full_address(identity.get("address")):
        opt.append("your full mailing address, street, city, state, and ZIP (or tell "
                   "me you'd rather leave it off)")
    if "github" in id_fields and "github" not in dec and not str(identity.get("github") or "").strip():
        opt.append("your GitHub link (a GitHub with real projects makes a much stronger "
                   "resume, or tell me you don't have one)")
    if "linkedin" in id_fields and "linkedin" not in dec and not str(identity.get("linkedin") or "").strip():
        opt.append("your LinkedIn URL (or tell me you don't have one)")
    if "blog" in id_fields and "blog" not in dec and not str(identity.get("blog") or "").strip():
        opt.append("a blog/portfolio link (or tell me you don't have one)")
    recent = _most_recent(education)
    if education and "courses" not in dec and not str(recent.get("courses") or "").strip():
        opt.append("the courses for your most recent degree (list them, or upload your "
                   "transcript, or tell me you'd rather skip the courses line)")
    if "projects" not in dec and not projects:
        opt.append("any projects you'd like to include (or tell me you don't have projects "
                   "to add)")

    result = crit if critical_only else (crit + opt)
    return list(dict.fromkeys(result))   # de-dupe, keep order


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
    SQLite-saved history and the palace transcript combine without repeats."""
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


class WebIntake:
    def __init__(self, jd_text, template_source, llm, memory, records, workdir,
                 profile_name="default", jobname="cv", palace_dir=None,
                 template_name=None):
        self.jd = jd_text
        self.template = template_source
        # The selected template's interview config, slot schema + question bank the
        # LLM draws on to converse naturally (one template today; future picker passes
        # the name). Product config, kept out of the user's MemPalace memory.
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

        # Durable verbatim + semantic memory (MemPalace). Defaults under the
        # workdir so tests stay isolated; the app passes a stable shared dir.
        self.palace = PalaceMemory(palace_dir or (self.workdir / "palace"), person=profile_name)

        self.essentials = {"identity": {}, "education": [], "experience": []}
        self.history: list[dict] = []      # this session's turns
        self.prior_history: list[dict] = []  # earlier sessions (kept, not replayed)
        self.recalled: list[dict] = []     # palace hits for this JD (filled on start)
        self.cv_selection: dict = {}       # P1: recall->decide reasoning for this build (surfaced)
        self.last_missing: list[str] = []  # outstanding gate fields from the last ask
        self.last_declined: list[str] = []  # declines already acknowledged (don't re-ack)
        self.profile = self._empty()
        self.assembled = None
        self.stage = "chatting"            # chatting | review | done
        self.returning = False
        self.saved_profile = {}
        self.last_cover_letter = ""        # most recent drafted cover letter (bundled on accept)
        self.last_screening = []           # most recent drafted screening Q/A (bundled on accept)
        self.source_job = {}               # {url, source, source_id} when started from a sourced job

        saved = memory.load(profile_name)
        if saved:
            self.saved_profile = saved.get("profile") or {}
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

        # Reload every turn ever recorded for this person from the palace, so a
        # restart mid-conversation loses nothing and no entry point forgets, the
        # transcript is the source of truth even when Accept never happened.
        self.prior_history = _merge_turns(self.prior_history, self.palace.load_history())

    # ------------------------------------------------------------------ helpers
    def _append(self, role: str, content: str):
        """Record one turn in the session AND persist it verbatim to the palace,
        as it happens, the fix for 'nothing persisted until Accept'."""
        self.history.append({"role": role, "content": content})
        try:
            self.palace.remember_turn(role, content)
        except Exception:
            pass

    def _persist(self):
        """Autosave the structured profile/essentials to the SQLite store on every
        turn, so a returning user is recognized even if they never clicked
        Accept & Save. (The palace holds the verbatim+semantic copy in parallel.)"""
        try:
            self.memory.save(self.profile_name, self.profile or {}, self.essentials,
                             self.prior_history + self.history)
        except Exception:
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
        SQLite store (deduped, with provenance) and mirror genuinely-new ones into the episodic
        palace so recall surfaces them too. Best-effort: never breaks the chat."""
        from datetime import date
        new: list[dict] = []
        try:
            existing = self.memory.list_facts(self.profile_name, status=None)
            known = {f["key"] for f in existing}
            result = self.llm.distill_memory(self.prior_history + self.history, sorted(known))
        except Exception:
            return new
        turn_no = len(self.prior_history) + len(self.history)
        for f in (result.get("facts") or []):
            prov = f"turn {turn_no} · {date.today().isoformat()}"
            try:
                row = self.memory.upsert_fact(self.profile_name, f.get("type"), f.get("key"),
                                              f.get("value"), provenance=prov,
                                              confidence=f.get("confidence", 0.8))
            except Exception:
                continue
            if not row:
                continue
            new.append(row)
            if row["key"] not in known:      # mirror only genuinely-new facts, no palace spam
                try:
                    self.palace.remember_summary(f"[{row['type']}] {row['key']}: {row['value']}")
                except Exception:
                    pass
        return new

    def _converse(self, message: str):
        """Handle a free-form turn conversationally with the real LLM, grounded in the full
        history + recalled memories + known facts. The turn is already appended and distilled;
        here we just reply and persist. Never enters the build funnel."""
        facts = self.memory.list_facts(self.profile_name, status="active")
        try:
            reply = str(self.llm.converse(self.jd, self.prior_history + self.history,
                                          self.recalled, facts) or "").strip()
        except Exception:
            reply = ""
        reply = reply or "Noted, I'll remember that."
        self._append("agent", reply)
        self._persist()
        return self._state([reply])

    def _apply_memory_selection(self):
        """Recall -> decide (P1, Task 3). Record which of the person's experiences/projects/skills
        earn a place on the CV for THIS job and briefly why, surfaced in state so the person sees
        the reasoning and can override. Constraints ('do not mention X') are NOT applied here:
        they are enforced non-destructively at RENDER time (see _render_profile), so the durable
        saved profile is NEVER mutated and an exclusion is reversible by retracting the fact."""
        try:
            prefs = self.memory.active_preferences(self.profile_name)
        except Exception:
            prefs = []
        try:
            self.cv_selection = self.llm.select_for_cv(self.jd, self.profile, self.recalled, prefs) or {}
        except Exception:
            self.cv_selection = {"selected": [], "excluded": [], "summary": ""}

    def _avoid_token_sets(self) -> list:
        """Distinctive-token sets for each ACTIVE 'do not mention' constraint (a year, or a word
        >= 4 chars). Read fresh every call, so retracting a constraint takes effect on the next
        build with no lingering state."""
        try:
            prefs = self.memory.active_preferences(self.profile_name)
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

        def _adopt(entry: dict, role: dict) -> dict:
            role = dict(role)
            original = list(role.get("bullets") or [])
            new = tailored.get(self._role_key(entry.get("org"), role.get("title")))
            if new:
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
        return whole

    # ------------------------------------------------------------------ gate
    def _effective_essentials(self) -> dict:
        """Essentials with every skeleton field backfilled from what's already on
        the built CV (``self.profile``) and the saved profile, so the gate CHECKS
        THE CV before asking, and never re-requests a location/date/degree that's
        already shown (e.g. a returning user whose stored essentials predate the
        location fields)."""
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
        try:
            ask = str(self.llm.ask_missing(missing, answered, self.role, new_declined) or "").strip()
        except Exception:
            ask = ""
        if not ask:                                        # deterministic fallback
            take = missing[:2]
            joined = take[0] if len(take) == 1 else f"{take[0]}, and {take[1]}"
            ack = "Got it. " if answered else ""
            ask = f"{ack}Could you tell me {joined}?"
        msgs = list(lead or [])
        msgs.append(ask)
        self._append("agent", ask)
        self._persist()
        return self._state(msgs)

    # ------------------------------------------------------------------ upload
    def ingest_document(self, text: str, filename: str = ""):
        """ONE upload path: read the file, decide what it is (CV / transcript /
        certificate), and route it, the person never has to say which button. Every
        upload is just an upload; we read it and figure out the rest."""
        kind = classify_document(text or "", filename)
        if kind == "transcript":
            return self.ingest_transcript(text, filename)
        if kind == "certificate":
            return self.ingest_certificate(text, filename)
        return self.ingest_resume(text, filename)

    def ingest_certificate(self, text: str, filename: str = ""):
        """Record an uploaded certificate as a real credential (added to the skills
        block's certifications). Raw file kept; clean summary to MemPalace."""
        self._append("user", f"[Uploaded certificate: {filename}]")
        name = _certificate_name(text, filename)
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

    def ingest_resume(self, text: str, filename: str = ""):
        """Pre-fill the profile from an uploaded CV's extracted text: merge the
        structured facts into memory (the real store), write a CLEAN summary into
        MemPalace (never the raw/OCR'd text), then gate on the required skeleton."""
        self._append("user", f"[Uploaded resume: {filename}]")
        try:
            out = self.llm.extract_intake("", self.essentials, [{"role": "user", "content": text or ""}])
            merged = _canonicalize_essentials(out.get("essentials") or self.essentials)
        except Exception:
            merged = self.essentials
        for k in ("identity", "education", "experience"):
            merged.setdefault(k, self.essentials.get(k, {} if k == "identity" else []))
        merged.setdefault("declined", self.essentials.get("declined", []))
        merged.setdefault("projects", self.essentials.get("projects", []))
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

        # Clean, tool-authored summary → semantic memory. Raw text is NOT indexed.
        from datetime import date
        from intake.cv_import import summarize_import
        try:
            self.palace.remember_summary(summarize_import(self.essentials, filename, date.today().isoformat()))
        except Exception:
            pass

        lead = [f"I read {filename} and pulled in your details."] if filename else \
               ["I read your resume and pulled in your details."]
        return self.draft(lead=lead)   # draft() applies the tiered gate

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

    def ingest_transcript(self, text: str, filename: str = ""):
        """Read the REAL courses from an academic transcript and populate the
        most-recent degree's courses with the JD-relevant ones. Same storage rules
        as any upload: facts -> profile, CLEAN summary -> palace, raw file kept."""
        from datetime import date

        self._append("user", f"[Uploaded transcript: {filename}]")
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
        finalization until the person replaces, builds, or removes them."""
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
            return (f"Welcome back{first}. I still have your background, {self._known_summary()}. "
                    f"Anything new or changed since last time, or should I tailor what I have to "
                    f"{role_phrase}{where}?")
        return (f"I've read {role_phrase}{where}. <b>Upload your existing resume</b> "
                "(Word, PDF, PowerPoint, or a photo/scan) and I'll pull in the details, or just "
                "tell me about yourself: your name, schools, and work history. Either way I'll only "
                "ask about anything that's missing.")

    # ------------------------------------------------------------------ client state
    def _state(self, messages=None, extra=None):
        s = {
            "phase": self.stage,
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
        if self.recalled:
            s["recalled"] = self.recalled
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
            s["pdf"] = f"/api/cv.pdf?job={self.jobname}&v={self._pdf_v}"
            s["one_page"] = bool(self.assembled.ok and self.assembled.compile.pages == 1)
            # Flagged placeholder projects block finalization until resolved.
            sugg = self._suggested_projects()
            if sugg:
                s["suggested_projects"] = sugg
                s["blocked_finalize"] = True
        if extra:
            s.update(extra)
        return s

    def _suggested_projects(self) -> list[str]:
        return [str(p.get("org") or p.get("title") or "Project")
                for p in (self.profile.get("projects") or []) if p.get("suggested")]

    _pdf_v = 0

    def start(self):
        # Retrieve the most relevant prior statements for THIS job from the palace
        #, on every entry point (New CV and Tailor-from-job both land here).
        self.recalled = self.palace.recall(f"{self.role} {self.company} {self.jd[:200]}")
        text = self.opening()
        self._append("agent", text)
        return self._state([text])

    # ------------------------------------------------------------------ conversation turn
    def submit(self, message: str):
        message = (message or "").strip()
        if not message:
            return self._state()
        self._append("user", message)

        # Flagged-placeholder project controls.
        if _wants_remove_suggested(message) and self._suggested_projects():
            return self.remove_suggested_projects()
        if _wants_project_ideas(message):
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
            self._absorb_enrichment(message)
            # Content given alongside "that's everything" is absorbed, THEN, once the
            # required sections are addressed, finalizes (absorb-then-finalize, pre-build).
            if _wants_done(message) and not self._open_required_sections():
                self.essentials["enrich_done"] = True
        reply = str(out.get("reply", "")).strip()
        self._persist()   # remember what was shared, without waiting for Accept

        # P1: distill durable facts/preferences/constraints from this turn into the authoritative
        # store (best-effort; the turn is already saved verbatim). Meaningful prose only.
        if self._is_meaningful_turn(message):
            self._distill_facts()

        # EXPLICIT OVERRIDE: "build with what you have / continue with what you know /
        # just build it". The completeness gate only protects a user who is silently
        # omitting things, a clear opt-out must build immediately and never re-ask.
        if _wants_override(message) or _wants_placeholder(message):
            # "just build it" / "put dummy data / use a placeholder", build now and let
            # the placeholder-flag fill any year-only dates in red for the person to fix.
            self.essentials["override"] = True
            return self._build_now()

        # P1 (Task 1): a free-form turn (context, goals, preferences, a question to the copilot)
        # gets a conversational reply grounded in full history + recalled + facts, instead of a
        # CV-field question, and is already remembered above. Pre-build only; the build/tailor
        # funnel below is unchanged for everything else.
        if self.assembled is None and self._wants_freeform_chat(message):
            return self._converse(message)

        # Enrichment: once a first draft exists, a substantive message is REAL
        # material to fold in (accomplishments/projects/skills/…), not a gate answer;
        # "that's everything" finalizes and stops the invitations.
        if self.stage in ("review", "done"):
            # Absorb any real material in this message FIRST, so content given in the
            # same breath as "that's everything" is never lost, THEN finalize. This
            # runs even under an override: the required Extracurricular / Interests
            # sections are still drawn out post-build, and their answers must land.
            self._absorb_enrichment(message)
            # "That's everything" only finalizes once the page-filling sections are
            # addressed. If Extracurricular / Interests are still blank and not declined,
            # a half page isn't finished, we keep insisting instead of accepting "done".
            if _wants_done(message) and not self._open_required_sections():
                self.essentials["enrich_done"] = True

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
                return self._ask_required_sections(open_req, lead=[reply] if reply else None)
        if reply:
            self._append("agent", reply)
        lead = [reply] if reply else None
        return self.draft(lead=lead)

    def _build_now(self):
        """Build immediately with whatever's on hand, per an explicit user override, leaving out any template elements they didn't provide, without re-asking."""
        self._conform_essentials()
        if self.saved_profile.get("experience"):
            return self.build_from_saved()
        if self.essentials.get("experience") or (self.essentials.get("identity") or {}).get("name"):
            # draft() builds now if nothing template-critical is missing; if a
            # location/date the template needs is still absent, it asks for just
            # that (concise) and then builds, skipping all the optional extras.
            return self.draft(lead=["On it, building with what you've given me."])
        note = ("Happy to build right away, but I don't have anything on file yet. Give me at least "
                "your name and one role and I'll build it immediately.")
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
    def draft(self, lead=None):
        # Gate: never build while a template field is missing. Under an override we
        # still insist on the template-critical fields (locations/dates), because
        # their absence breaks the template, but skip the optional extras.
        self._conform_essentials()
        gaps = self._gate()
        if gaps:
            return self._ask_missing(gaps, lead=lead, critical=bool(self.essentials.get("override")))
        self.stage = "drafting"
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
        self._apply_real_bullets()  # the person's REAL bullets win; flag drafted ones
        self._apply_essentials_locations()  # never drop an experience location (hard rule)
        date_flags = self._flag_placeholder_dates()  # year-only dates -> flagged placeholder months
        self._expand_terse_bullets()  # grow stubby fragments into full page-width sentences
        self._apply_user_skills()   # the person's own skills + certifications
        self._validate_skills()     # drop any drafted skill the person's material can't support
        self._ensure_richness()
        self._fill_skills()         # foreground supported JD terms (no stretch, ragged if short)
        self._dedupe_skill_groups() # one term, one line -- no duplicate Computing/Knowledge rows
        self._apply_memory_selection()  # P1: record why these belong (reasoning only, surfaced)
        self._assemble()                # renders a constraint-filtered copy; durable profile stays whole
        self.stage = "review"
        self._persist()   # the built profile is now remembered for next time
        msgs = list(lead or [])
        # Never call a short page "a real, one-page resume" (that line sat over a page that was
        # half empty, 2026-10-07). Say what the page actually is and what happens next.
        fill = getattr(self.assembled, "fill_ratio", None) if self.assembled is not None else None
        if self.assembled is not None and self.assembled.underfull and fill:
            msgs.append(f"Here's a first draft. It fills about {round(fill * 100)}% of the page, "
                        "so I'll ask you for a few more real things to fill it properly.")
        else:
            msgs.append("Here's your draft, a real, one-page resume.")
        if self.title_flags():
            msgs.append("I aligned a job <b>title</b> to the role, it's highlighted; click it to "
                        "confirm or revert.")
        msgs.append("Edit any bullet right on the page, then <b>Accept & Save</b>.")
        if date_flags:
            note = self._date_flag_note(date_flags)
            msgs.append(note)
            self._append("agent", note)
        # Thorough elicitation: keep drawing out REAL material for every template slot
        # still empty, accomplishments, projects, skills, certs, AND extracurricular /
        # interests, until each has been offered once (then _enrich_invite stops).
        # This runs whether or not the CV is "light"; opting out (override/done) skips it.
        extra = self._post_build_invite(msgs)
        return self._state(msgs, extra=extra)

    def _post_build_invite(self, msgs):
        """Shared review-message tail for both build paths (draft + build_from_saved):
        keep drawing out still-empty template slots, under an override, only the
        required Extracurricular / Interests sections, or flag a light CV once.
        Returns the `extra` flag for _state so the UI can surface it."""
        extra = self._light_extra()
        invite = "" if self.essentials.get("enrich_done") else self._enrich_invite()
        if invite:
            msgs.append(invite)
            self._append("agent", invite)
        elif extra:
            # Opted out but still light, flag it once, don't interrogate.
            note = ("Heads up: this resume is on the lighter side. Send me more "
                    "accomplishments, a project, or interests anytime and I'll work "
                    "them in to fill it out like the template.")
            msgs.append(note)
            self._append("agent", note)
        return extra

    def build_from_saved(self):
        """Build directly from the saved profile, tailored to this job, the
        response to "use what you already know about me"."""
        if self.saved_profile and self.saved_profile.get("experience"):
            self.profile = normalize_profile(self.saved_profile)
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
            self._validate_skills()  # drop any saved skill the person's material can't support
            self._fill_skills()   # foreground supported JD terms (no stretch, ragged if short)
            self._dedupe_skill_groups()  # one term, one line
            self._apply_memory_selection()  # P1: record why these belong (reasoning only, surfaced)
            # reword the saved bullets toward THIS job, from a constraint-filtered COPY so a
            # 'do not mention X' never renders; the durable self.profile stays whole (restored below)
            self.assembled = assemble_cv(
                self.template, self._render_profile(), self.jd, self.llm, self.workdir,
                jobname=self.jobname, tailor=True, sections=self.sections,
                headings=self.headings)
            self.profile = self._whole_profile_after_render(self.assembled.profile_used)
            self._pdf_v += 1
            self.dirty = False
            self.stage = "review"
            self._persist()
            lead = "Building from your saved profile, tailored to this role."
            self._append("agent", lead)
            msgs = [lead]
            if getattr(self.assembled, "fact_flags", None):
                from tailoring.fact_gate import describe as _describe_figures
                msgs.append(_describe_figures(self.assembled.fact_flags))
            if self.title_flags():
                msgs.append("I aligned a job <b>title</b> to the role, it's highlighted; "
                            "click it to confirm or revert.")
            msgs.append("Edit any bullet right on the page, then <b>Accept & Save</b>.")
            if date_flags:
                note = self._date_flag_note(date_flags)
                msgs.append(note)
                self._append("agent", note)
            extra = self._post_build_invite(msgs)
            return self._state(msgs, extra=extra)
        if self.essentials.get("experience"):
            return self.draft(lead=["Using what I already have on file."])
        note = ("I don't have a saved profile yet, tell me your name and work history "
                "and I'll build one.")
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
        return (f"Heads up: I didn't have the start/end <b>months</b> for {who}, so I "
                f"filled placeholder months (shown in <b>red</b> on the resume). Replace them "
                f"with the real months before you submit, the template uses full months.")

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
        from tailoring.filler import is_padded
        from tailoring.keywords import introduced_skills, profile_text
        # ONE anti-fabrication rule, shared with the assembler's reword gate
        # (assembler._iter_bullet_slots_with_grounding): the grounding is everything the
        # bullet's own entry says (title, org, tech line, sibling bullets), never the whole
        # profile. Grounding in the sibling bullets alone used to reject a skill the role's
        # tech line already lists, so a bullet the assembler accepted was refused here.
        grounding = "\n".join(str(x) for x in (bullets or []))
        if entry:
            grounding = profile_text(entry) + "\n" + grounding

        def score(frac):
            # How cleanly the last line lands (higher = better; >=0.58 is "good"). A one-line
            # bullet that is at least about half a line is a normal ragged line, not a defect:
            # rewriting those to "fill the line" is what produced the padded sentences. Only a
            # genuinely stubby fragment is reworked, and the filler guard still vets the result.
            if frac < 0.85:
                return 1.0 if frac >= 0.45 else frac * 0.5
            part = frac - int(frac)
            if part <= 0.015:
                return 1.0                             # ends right at a line boundary
            if part >= 0.58:
                return part                            # last line >=58% full (natural)
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
                if best_score >= 0.58:
                    break
                frac = line_fraction(best)
                whole, part = int(frac), frac - int(frac)
                # Aim for the nearest full-line landing: trim a small overrun back, or
                # fill out the current line when it's already well into it.
                target = 1 if frac < 0.85 else (whole if part < 0.30 else whole + 1)
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
                if introduced_skills(b, cand, grounding, self.jd) or is_padded(b, cand):
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
        """Fill each skills line toward the template's full-page-width density using
        JD keywords the person's material ACTUALLY supports (ATS alignment, CLAUDE.md
        §8), so the Skills block reaches the right margin like the template, without
        ever inventing a skill the person doesn't have."""
        from tailoring.keywords import supported_skills
        skills = self.profile.get("skills") or {}
        if not skills:
            return
        from tailoring.textwidth import text_width_pt, TEXTWIDTH_PT
        from tailoring.keywords import term_present
        present = {t.strip().lower() for v in skills.values()
                   for t in str(v).split(",") if t.strip()}
        # Draw the fill pool from the SAME genuine-material source _validate_skills uses
        # (not profile_text, which includes drafted placeholder bullets), otherwise a JD
        # skill the person doesn't have, echoed in a drafted bullet, would be packed onto
        # the Skills line, re-opening the fabrication hole validation just closed (§8).
        genuine = self._skill_source_text()
        pool = [t for t in supported_skills(self.jd, self.profile)
                if t.lower() not in present and term_present(t, genuine)]
        SAFE = 2.0   # tiny right-margin guard; microtype + justify close the last sliver
        for label in list(skills.keys()):
            # Budget = the real text width available for the VALUE, measured in points:
            # full text width minus the BOLD "Label:" minus a space minus safety.
            budget_pt = (TEXTWIDTH_PT - text_width_pt(f"{label}:", bold=True)
                         - text_width_pt(" ") - SAFE)
            items = [s.strip() for s in str(skills[label] or "").split(",") if s.strip()]
            val_w = lambda lst: text_width_pt(", ".join(lst))
            # Self-healing word choice: at each step add the supported term that packs the
            # line CLOSEST to the right margin without overflowing (best-fit), so the line
            # fills as tightly as the real terms allow, a small residual, if any, is then
            # taken up invisibly by microtype's glyph expansion at render time.
            added = True
            while added and pool:
                added = False
                best_k, best_w = -1, -1.0
                for k, term in enumerate(pool):
                    w = val_w(items + [term])
                    if w <= budget_pt and w > best_w:
                        best_k, best_w = k, w
                if best_k >= 0:
                    items.append(pool.pop(best_k))
                    added = True
            # Drop WHOLE trailing terms if the model over-supplied (never wrap).
            while len(items) > 1 and val_w(items) > budget_pt:
                items.pop()
            skills[label] = ", ".join(items)
        self.profile["skills"] = skills

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
        saved = self.saved_profile or {}   # a returning user's saved profile counts as filled
        return [sec for sec in self.required_fill
                if not self._section_filled(sec, e, saved)
                and not (declined & {sec, "extras"})]

    def _ask_required_sections(self, open_req, lead=None):
        """Ask, BEFORE the first build, for the template's required sections. Phrased from
        the manifest's elicit leads, insisted upon (re-asked until provided), honoring an
        explicit decline via the gate so it never loops."""
        from intake.template_manifest import elicit_leads
        phrases = []
        for sec in open_req:
            leads = elicit_leads(self.manifest, sec)
            phrases.append(leads[0] if leads else f"your {sec}")
        guidance = self._enrich_guidance(list(open_req))
        already_asked = bool(self.essentials.get("asked_required"))
        try:
            ask = str(self.llm.ask_enrich(phrases, ["earlier"] if already_asked else [],
                                          self.role, guidance,
                                          person=self._who_we_are_asking()) or "").strip()
        except Exception:
            ask = ""
        if not ask:                       # deterministic, warm fallback
            joined = phrases[0] if len(phrases) == 1 else f"{phrases[0]}  And {phrases[1]}"
            opener = "One more before I build it" if not already_asked else "Whenever you're ready"
            ask = (f"{opener}, the resume isn't complete without it: {joined}. "
                   "(If there's genuinely none, tell me and I'll leave that section off.)")
        self.essentials["asked_required"] = True
        self._persist()
        msgs = list(lead or [])
        msgs.append(ask)
        self._append("agent", ask)
        return self._state(msgs)

    def _enrich_invite(self) -> str:
        """Draw out the next enrichment topics. The page-filling sections
        (Extracurricular, Interests) are INSISTED upon: re-offered every turn until the
        person provides real material or explicitly declines the section -- never
        one-and-done, because a CV isn't finished while they're blank. Optional extras
        (extra bullets, certs) are offered once."""
        asked = set(self.essentials.get("enrich_asked", []))
        required = set(self.required_fill)
        open_required = set(self._open_required_sections())
        # Keep _enrich_topics' priority order (real accomplishments first, then the
        # page-filling sections). Required topics persist until resolved; optional
        # topics are offered once.
        topics = [(k, p) for k, p in self._enrich_topics()
                  if (k in required and k in open_required)
                  or (k not in required and k not in asked)]
        req_topics = [t for t in topics if t[0] in required]
        if not topics:
            self.essentials["enrich_done"] = True
            return ""
        take = topics[:2]
        answered = bool(asked)   # they've added something since we started asking
        # Only OPTIONAL topics get marked asked; required ones keep coming back.
        self.essentials["enrich_asked"] = sorted(
            asked | {k for k, _ in take if k not in required})
        phrases = [p for _k, p in take]
        guidance = self._enrich_guidance([k for k, _ in take])
        insist = bool(req_topics)   # a required section is still open -> push harder
        try:
            ask = str(self.llm.ask_enrich(phrases, list(asked) if answered else [],
                                          self.role, guidance,
                                          person=self._who_we_are_asking()) or "").strip()
        except Exception:
            ask = ""
        if not ask:
            joined = phrases[0] if len(phrases) == 1 else f"{phrases[0]}, and {phrases[1]}"
            ack = "Nice, added. " if answered else ""
            if insist:
                ask = (f"{ack}A complete resume fills the page, it can't stop at your "
                       f"experience with the rest blank. I still need {joined} to finish "
                       "it. Share real details and I'll add them, or tell me to leave a "
                       "section off if it truly doesn't apply.")
            else:
                ask = (f"{ack}To fill your resume out like the template, could you share "
                       f"{joined}? (Real details only, or say “that's everything” and "
                       "I'll finalize.)")
        return ask

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

    def _assemble(self):
        # Render from a constraint-filtered COPY so a 'do not mention X' never reaches the CV,
        # while self.profile (persisted) stays whole; restore any hidden items into the durable
        # profile afterward. With no active constraints both are no-ops (behavior unchanged).
        self.assembled = assemble_cv(
            self.template, self._render_profile(), self.jd, self.llm, self.workdir,
            jobname=self.jobname, tailor=False, sections=self.sections, headings=self.headings,
        )
        self.profile = self._whole_profile_after_render(self.assembled.profile_used)
        self._pdf_v += 1
        self.dirty = False

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

    def set_title(self, role_id: str, decision: str, custom: str = ""):
        from intake.conversation import apply_title_decision
        for rid, _e, r in self._roles():
            if rid == role_id:
                apply_title_decision(r, decision, custom)
                break
        self._assemble()
        return self._state()

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
        self.dirty = True
        self._assemble()
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
        return {"ratio": ratio, "present": present,
                "missing": missing, "missing_supported": missing_sup}

    def review_dict(self):
        """An honest pre-send check on the built CV: does every skill shown trace back to the
        person's own profile (no tailor-introduced claim they cannot defend), plus easy wins,
        honest gaps, and one-page fit. Deterministic, so it never changes run-to-run."""
        from tailoring.reviewer import review_resume
        return review_resume(self.profile, self.coverage_dict(), self.assembled.status,
                             fill=getattr(self.assembled, "fill_ratio", None))

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
            "render_profile": self._render_profile(),
            # The exact page (after role selection, tailoring and one-page fitting) and the
            # same material before tailoring, so a chat edit (notify/cvreview.py) starts from
            # what the person actually sees and "Show changes" can pair each bullet.
            "page_profile": getattr(self.assembled, "profile_used", None) or {},
            "base_profile": getattr(self.assembled, "source_profile", None) or {},
            "template": self.template_name or "",
            "sections": list(self.sections or []),
            "headings": dict(self.headings or {}),
        }
        # Records are named for the JOB APPLIED TO, never the CV's first role title:
        # four Amazon builds once all read "Founder (Side Project)" and the person
        # couldn't tell which was which (obs #35). The template label disambiguates
        # variants of the same application; siblings link back via data.variant_of.
        tlabel = str((self.manifest or {}).get("display_name") or "").strip()
        label = (self.role or title or "Resume").strip() or "Resume"
        if tlabel:
            label += f" · {tlabel}"
        if getattr(self, "variant_of", None):
            data["variant_of"] = self.variant_of
        rec_id = self.records.add(role=label, company=self.company, coverage=cov,
                                  pdf_path="", jd_label=self.role, data=data)
        self._last_record_id = rec_id   # remember it so a repeat accept() is idempotent
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
