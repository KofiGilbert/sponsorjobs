"""LinkedIn profile review: a read-only score of the person's OWN profile page.

The browser extension reads the person's own profile page while they have it open (it
never scrolls, clicks, or edits, and does nothing on anyone else's profile; CLAUDE.md §7),
sends the extraction to the local app, and this module turns it into the kind of report
Careerflow and Teal sell: 14 weighted sections, each with a 0 to 100 score, a one-line
reason and one concrete fix, plus a total out of 100. Everything here is deterministic
and pure so it is unit-testable on fixed inputs.

The one model call (``draft_headline_about``) writes a headline and an About aligned to
the person's target sponsor roles, grounded ONLY in their saved profile. The numeric fact
gate (tailoring/fact_gate.py) then flags any figure the person's material never stated.
The person applies every change on LinkedIn by hand.
"""

from __future__ import annotations

import re
from datetime import date

from tailoring.fact_gate import figures, source_figures

HEADLINE_MAX = 220       # LinkedIn's own headline limit
ABOUT_MAX = 2000         # our cap for the drafted About (LinkedIn allows 2,600)

# The 14 sections, in display order, with their share of the 100 points. The weights lean
# toward what a recruiter (and LinkedIn's own search) reads first: headline, About,
# experience and skills carry half the score between them.
SECTIONS: list[tuple[str, str, int]] = [
    ("photo", "Profile photo", 8),
    ("banner", "Banner image", 4),
    ("headline", "Headline", 12),
    ("about", "About", 14),
    ("experience", "Experience", 14),
    ("education", "Education", 5),
    ("skills", "Skills", 10),
    ("licenses", "Licenses and certifications", 4),
    ("custom_url", "Custom URL", 4),
    ("location", "Location", 3),
    ("open_to_work", "Open to work", 4),
    ("featured", "Featured and projects", 6),
    ("recommendations", "Recommendations", 6),
    ("activity", "Activity", 6),
]
assert sum(w for _, _, w in SECTIONS) == 100

_ROLE_STOP = {
    "a", "an", "and", "the", "of", "for", "to", "in", "at", "or", "with", "senior", "junior",
    "lead", "staff", "principal", "associate", "ii", "iii", "iv", "i", "sr", "jr", "intern",
    "remote", "hybrid", "onsite", "new", "grad", "entry", "level", "full", "time", "part",
}
_CTA = re.compile(r"\b(reach out|get in touch|contact me|connect|message me|email me|let'?s talk|"
                  r"open to|happy to chat|drop me|say hello|dm me)\b", re.I)
_FIRST_PERSON = re.compile(r"\b(i|i'm|i am|my|me|i've|i'd)\b", re.I)
_WEAK_HEADLINE = re.compile(r"\b(unemployed|seeking (new )?opportunit\w*|looking for (a )?job|"
                            r"open to work|job seeker|student at)\b", re.I)
_NUMBER = re.compile(r"\d")
_RANDOM_SLUG = re.compile(r"-[0-9a-f]{5,}$|-\d{3,}$|^[a-z]+-[a-z]+-[0-9a-z]{8,}$", re.I)
_BULLETISH = re.compile(r"(^|\n)\s*[-*•●▪‣⁃]|\n\s*\d+[.)]\s", re.M)


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def role_terms(target_role: str, extra_terms=None) -> list[str]:
    """The words a recruiter would search for this role: the role's own meaningful words
    plus any JD skill terms the app already extracted for it. Lowercase, de-duplicated."""
    seen: list[str] = []
    for tok in re.findall(r"[A-Za-z][A-Za-z+#.]*", _norm(target_role)):
        low = tok.lower().strip(".")
        if len(low) < 2 or low in _ROLE_STOP or low in seen:
            continue
        seen.append(low)
    for t in extra_terms or []:
        low = _norm(t).lower()
        if low and low not in seen:
            seen.append(low)
    return seen


def _present(term: str, text: str) -> bool:
    pat = r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])"
    return re.search(pat, text.lower()) is not None


def _terms_hit(terms: list[str], text: str) -> list[str]:
    return [t for t in terms if _present(t, text)]


def _clamp(n: float) -> int:
    return max(0, min(100, int(round(n))))


def _is_recent(dates: str, today: date | None = None) -> bool:
    """A date string like 'Jan 2023 - Present' or '2021 - 2024' counts as recent when it
    is current or ended within the last two years."""
    d = _norm(dates)
    if re.search(r"\bpresent\b|\bcurrent\b|\bnow\b", d, re.I):
        return True
    years = [int(y) for y in re.findall(r"\b(19\d\d|20\d\d)\b", d)]
    if not years:
        return False
    y = (today or date.today()).year
    return max(years) >= y - 2


# --------------------------------------------------------------------------- sections

def _sec_photo(x, _t):
    if x.get("photo"):
        return 100, "A photo is on the page.", "Keep it: a clear head-and-shoulders shot, plain background."
    return 0, "No profile photo.", "Add a clear head-and-shoulders photo. Profiles with a photo get many times more views."


def _sec_banner(x, _t):
    if x.get("banner"):
        return 100, "A custom banner is set.", "Keep it tied to your field (your work, your city, a clean graphic)."
    return 0, "The default grey banner.", "Upload a banner (1584 x 396) that says what you do: your field, a product, your city."


def _sec_headline(x, terms):
    h = _norm(x.get("headline"))
    if not h:
        return 0, "No headline.", "Write a headline: target role, two or three key skills, and what you bring."
    n = len(h)
    score = 25
    reasons = []
    if n >= 40:
        score += 25
    else:
        reasons.append("short")
    hits = _terms_hit(terms, h)
    if hits:
        score += 25
    else:
        reasons.append("no target-role keywords")
    richer = bool(re.search(r"[|•,]", h)) or (n >= 60 and len(h.split()) >= 8)
    if richer:
        score += 25
    else:
        reasons.append("reads as a job title only")
    if _WEAK_HEADLINE.search(h):
        score -= 30
        reasons.append("says you are looking rather than what you do")
    if n > HEADLINE_MAX:
        score -= 20
        reasons.append("over 220 characters")
    reason = ("Headline looks strong." if not reasons
              else "Headline is " + ", ".join(reasons) + ".")
    fix = ("Use the drafted headline below: role, two or three searchable skills, one outcome, under 220 characters."
           if reasons else "Keep it current with the roles you are targeting.")
    return _clamp(score), reason, fix


def _sec_about(x, terms):
    a = _norm(x.get("about"))
    if not a:
        return 0, "No About section.", "Add an About: who you are, what you do, proof, and how to reach you. Paste the draft below."
    n = len(a)
    score = 20
    reasons = []
    if n >= 600:
        score += 30
    elif n >= 250:
        score += 18
        reasons.append("a bit short")
    else:
        reasons.append("too short")
    if _FIRST_PERSON.search(a):
        score += 15
    else:
        reasons.append("not in the first person")
    hits = _terms_hit(terms, a)
    if len(hits) >= 3:
        score += 20
    elif hits:
        score += 10
        reasons.append("few target-role keywords")
    else:
        reasons.append("no target-role keywords")
    if _CTA.search(a):
        score += 15
    else:
        reasons.append("no call to action")
    reason = "About covers the basics." if not reasons else "About is " + ", ".join(reasons) + "."
    fix = ("Use the drafted About below: first person, the roles you want, three to five skills in the JD's words, one proof point, a way to reach you."
           if reasons else "Refresh it when your target roles change.")
    return _clamp(score), reason, fix


def _sec_experience(x, _t):
    rows = [e for e in (x.get("experience") or []) if isinstance(e, dict)]
    if not rows:
        return 0, "No experience entries.", "Add every relevant role with two to four lines on what you did and the result."
    score = 30
    reasons = []
    with_desc = [e for e in rows if e.get("has_description") or _norm(e.get("description"))]
    frac = len(with_desc) / len(rows)
    if frac >= 0.75:
        score += 25
    elif frac > 0:
        score += 12
        reasons.append("some roles have no description")
    else:
        reasons.append("no role has a description")
    descs = "\n".join(_norm(e.get("description")) for e in rows)
    if _NUMBER.search(descs):
        score += 20
    else:
        reasons.append("no numbers in the descriptions")
    if _BULLETISH.search("\n".join(str(e.get("description") or "") for e in rows)):
        score += 10
    else:
        reasons.append("paragraphs instead of bullets")
    if any(_is_recent(e.get("dates")) for e in rows):
        score += 15
    else:
        reasons.append("nothing recent")
    reason = "Experience is well filled in." if not reasons else "Experience: " + ", ".join(reasons) + "."
    fix = ("For each role add two to four bullets that start with a verb and carry a number (size, speed, money, people)."
           if reasons else "Keep the top role current.")
    return _clamp(score), reason, fix


def _sec_education(x, _t):
    rows = [e for e in (x.get("education") or []) if isinstance(e, dict)]
    if not rows:
        return 0, "No education listed.", "Add your degree, school and dates. Many sponsor roles filter on degree."
    full = [e for e in rows if _norm(e.get("school")) and _norm(e.get("degree"))]
    if full:
        return 100, "Education is listed with a degree.", "Keep it. Add relevant coursework or honors if space allows."
    return 60, "Education is listed without a degree name.", "Add the degree and field of study on each school."


def _sec_skills(x, terms):
    skills = [_norm(s) for s in (x.get("skills") or []) if _norm(s)]
    if not skills:
        return 0, "No skills listed.", "Add 15 to 30 skills, with the ones your target JDs name first."
    score = 20
    reasons = []
    n = len(skills)
    if n >= 15:
        score += 40
    elif n >= 5:
        score += 25
        reasons.append(f"only {n} listed")
    else:
        score += 10
        reasons.append(f"only {n} listed")
    blob = "\n".join(skills)
    hits = _terms_hit(terms, blob) if terms else []
    if terms:
        cov = len(hits) / len(terms)
        if cov >= 0.5:
            score += 40
        elif hits:
            score += 20
            reasons.append("partly matches the target role")
        else:
            reasons.append("none match the target role")
    else:
        score += 30
    reason = "Skills are plentiful and on target." if not reasons else "Skills: " + ", ".join(reasons) + "."
    missing = [t for t in terms if t not in hits][:6]
    fix = ("Add, in LinkedIn's wording, the skills your target JDs name that you have: " + ", ".join(missing) + "."
           if missing else "Pin your top three skills to the ones the target role names.")
    return _clamp(score), reason, fix


def _sec_licenses(x, _t):
    rows = [e for e in (x.get("licenses") or []) if isinstance(e, dict) and _norm(e.get("name"))]
    if rows:
        return 100, f"{len(rows)} license{'s' if len(rows) != 1 else ''} or certification listed.", "Keep them current and drop expired ones."
    return 0, "No licenses or certifications.", "Add any certification you hold (cloud, PMP, CFA, language). Leave it empty rather than invent one."


def _sec_custom_url(x, _t):
    slug = _norm(x.get("slug"))
    if x.get("custom_url") is True or (slug and not _RANDOM_SLUG.search(slug)):
        return 100, "Custom URL is set.", "Put the same URL on your resume header."
    return 0, "URL still has LinkedIn's random suffix.", "Edit your public profile URL to your name (Settings > Edit public profile and URL)."


def _sec_location(x, _t):
    loc = _norm(x.get("location"))
    if not loc:
        return 0, "No location.", "Set your city and country. Recruiters filter by location before anything else."
    if "," in loc:
        return 100, "City and region are set.", "Keep it to where you want to work, not where you grew up."
    return 70, "Location is set but not specific.", "Use city plus region or country so location filters find you."


def _sec_open_to_work(x, _t):
    if x.get("open_to_work"):
        return 100, "Open to work is on.", "Keep the roles and locations in it matched to what you tailor for here."
    return 40, "Open to work is off.", "Turn it on, visible to recruiters only, with the job titles you target."


def _sec_featured(x, _t):
    feat = int(x.get("featured_count") or 0)
    proj = int(x.get("projects_count") or 0)
    if feat or x.get("featured"):
        return 100, "Featured section is filled.", "Keep one link that proves your best work on top."
    if proj:
        return 60, "Projects listed, nothing featured.", "Feature your strongest project or a link to your work."
    return 0, "No featured items or projects.", "Feature one thing: a project, a post, a talk, or your portfolio link."


def _sec_recommendations(x, _t):
    n = int(x.get("recommendations_count") or 0)
    if n >= 2:
        return 100, f"{n} recommendations received.", "Ask for one from your most recent manager every year or so."
    if n == 1 or x.get("recommendations"):
        return 60, "One recommendation.", "Ask a second person, ideally a recent manager or client."
    return 0, "No recommendations.", "Ask two people you worked with for a short recommendation. Offer to write one back."


def _sec_activity(x, _t):
    if x.get("activity_recent"):
        return 100, "Recent activity on the page.", "A post or a thoughtful comment every week or two is plenty."
    if x.get("activity") or int(x.get("followers") or 0) > 0:
        return 50, "Activity section exists but nothing recent.", "Post or comment on something in your field this week."
    return 0, "No activity.", "Comment on a post in your field this week. Activity shows up in recruiter feeds."


_SCORERS = {
    "photo": _sec_photo, "banner": _sec_banner, "headline": _sec_headline, "about": _sec_about,
    "experience": _sec_experience, "education": _sec_education, "skills": _sec_skills,
    "licenses": _sec_licenses, "custom_url": _sec_custom_url, "location": _sec_location,
    "open_to_work": _sec_open_to_work, "featured": _sec_featured,
    "recommendations": _sec_recommendations, "activity": _sec_activity,
}


def score_profile(extracted: dict, target_role: str = "", target_terms=None) -> dict:
    """Score an extracted LinkedIn profile against the person's target role.

    Returns ``{"score": int, "target_role": str, "sections": [...]}`` where each section
    is ``{key, label, weight, score, points, reason, fix}``. Sections are in display order
    and total points equal ``score``.
    """
    x = extracted if isinstance(extracted, dict) else {}
    terms = role_terms(target_role, target_terms)
    sections = []
    total = 0.0
    for key, label, weight in SECTIONS:
        s, reason, fix = _SCORERS[key](x, terms)
        pts = weight * s / 100.0
        total += pts
        sections.append({"key": key, "label": label, "weight": weight, "score": int(s),
                         "points": round(pts, 1), "reason": reason, "fix": fix})
    return {"score": _clamp(total), "target_role": _norm(target_role), "sections": sections,
            "target_terms": terms}


# --------------------------------------------------------------------------- drafting

def _trim(text: str, limit: int) -> str:
    t = _norm(text)
    if len(t) <= limit:
        return t
    cut = t[:limit]
    for sep in (". ", "; ", " | ", ", ", " "):
        i = cut.rfind(sep)
        if i > limit * 0.6:
            return cut[:i].rstrip(" ,;|") + ("." if sep == ". " else "")
    return cut.rstrip()


def _about_trim(text: str, limit: int) -> str:
    t = str(text or "").strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    for sep in ("\n\n", ". ", "\n", " "):
        i = cut.rfind(sep)
        if i > limit * 0.6:
            return cut[:i].rstrip() + ("." if sep == ". " else "")
    return cut.rstrip()


def draft_headline_about(llm, saved_profile: dict, target_role: str, extracted: dict | None = None) -> dict:
    """One model call: a headline (under 220 characters) and an About (under 2,000) aimed
    at the person's target sponsor roles, grounded only in their saved profile. Figures the
    saved material (and the person's current page text) never stated are flagged, not
    silently kept. Returns ``{headline, about, flags}``."""
    x = extracted or {}
    out = llm.draft_linkedin_profile(saved_profile or {}, target_role or "",
                                     _norm(x.get("headline")), str(x.get("about") or ""))
    if not isinstance(out, dict):
        out = {}
    headline = _trim(out.get("headline") or "", HEADLINE_MAX)
    about = _about_trim(out.get("about") or "", ABOUT_MAX)
    known = source_figures(saved_profile or {}, x.get("headline") or "", x.get("about") or "",
                           [e.get("description") or "" for e in (x.get("experience") or [])
                            if isinstance(e, dict)])
    flags = []
    for label, text in (("headline", headline), ("about", about)):
        new = sorted(figures(text) - known, key=lambda s: (len(s), s))
        if new:
            flags.append({"field": label, "figures": new})
    return {"headline": headline, "about": about, "flags": flags}


def target_role_from(saved_profile: dict | None, records: list[dict] | None) -> str:
    """The role the review aims at: the last tailored job's role when there is one, else the
    person's most recent title from their saved profile."""
    for r in records or []:
        role = _norm((r or {}).get("jd_label") or (r or {}).get("role"))
        role = re.sub(r",\s*[A-Z]:\s.*$", "", role)   # strip an internal variant label
        if role:
            return role
    prof = saved_profile or {}
    for e in prof.get("experience") or []:
        if not isinstance(e, dict):
            continue
        roles = e.get("roles") if isinstance(e.get("roles"), list) else [e]
        for r in roles:
            t = _norm((r or {}).get("title") or (r or {}).get("role"))
            if t:
                return t
    return ""
