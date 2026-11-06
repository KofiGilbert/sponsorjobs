r"""Pour a user's PROFILE into the template's shape (CLAUDE.md §2).

The template `.tex` is *shape only*: its preamble defines the fonts, colours,
geometry, hyperlink rendering, and the section/heading/bullet macros that make it
one beautiful page. Its body text is placeholder clay and is discarded. This
module reuses the template's **preamble verbatim** and re-emits the body from the
person's own content using the same macro vocabulary — so the result is
identical in form (font, colour, links, structure, length budget) but entirely
the user's material.

Flow: fill slots from PROFILE -> tailor bullet language to the JOB (LLM, truthful
to the profile) -> compile -> shrink to one page if needed -> coverage report.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from llm.base import LLMBackend
from .compiler import CompileResult, compile_tex
from .keywords import CoverageReport, build_coverage_report, supported_skills
from .fact_gate import flag_new_figures
from .latex_text import escape_latex
from .latex_template import LatexTemplate

try:                                     # the live activity feed (ui/activity.py): a no-op off a run
    from ui.activity import step as _say
except Exception:                        # noqa: BLE001 - the engine builds without the app too
    def _say(*_a, **_k):
        return None

DOC_START = r"\begin{document}"
DOC_END = r"\end{document}"

# Tailoring re-emphasises rather than inflates an ALREADY-full bullet (±10%,
# CLAUDE.md §8). But a CV built from a person's terse notes ("Built SQL pipelines")
# would otherwise render as stubby half-line fragments — so a bullet is also allowed
# to grow up to a full template line, giving every bullet a complete, page-width
# sentence. The one-page self-heal loop remains the guardrail against overflow.
LENGTH_BUDGET = 1.10
# The Skills block never shrinks below this many lines to fit the page (linefit.SKILL_LINES_MIN).
SKILL_LINES_FLOOR = 5
FULL_LINE_CHARS = 165   # ~ one full template bullet line

def _bullet_budget(original: str) -> int:
    """Max length for a reworded bullet: at least a full line (so terse notes
    become complete sentences), or ±10% of an already-substantial bullet."""
    return max(int(len(original) * LENGTH_BUDGET), FULL_LINE_CHARS)


def extract_preamble(template_source: str) -> str:
    """Return the template's preamble, verbatim, up to and including \\begin{document}."""
    idx = template_source.find(DOC_START)
    if idx == -1:
        raise ValueError("template has no \\begin{document}")
    return template_source[: idx + len(DOC_START)]


# --------------------------------------------------------------------------- #
# Rendering the body from a PROFILE, in the template's exact macro vocabulary.
# --------------------------------------------------------------------------- #

def _esc(s: str) -> str:
    return escape_latex(str(s).strip())


def _safe_url(url: str) -> str:
    """Neutralize LaTeX-active bytes in a hyperlink TARGET before it goes inside \\href{...}.

    A raw ``}`` closes the href group early and lets whatever follows execute as LaTeX
    (e.g. a smuggled ``\\twocolumn`` breaking the single-column layout, §8), and a stray
    ``\\`` / ``^`` / ``~`` injects a control sequence or an active char. We percent-encode
    exactly those bytes — which is URL-equivalent, so a legitimate link is never dropped.
    ``#`` ``%`` ``&`` ``_`` are left intact: hyperref already parses them correctly inside
    \\href, and encoding them would corrupt real fragment/query URLs."""
    url = str(url).strip()
    for ch, enc in (("\\", "%5C"), ("{", "%7B"), ("}", "%7D"),
                    ("^", "%5E"), ("~", "%7E")):
        url = url.replace(ch, enc)
    return url


_HAS_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _link_url(url: str) -> str:
    """Absolutize a CLICKABLE link target before it goes inside \\href{...}.

    Profiles store links the way people type them ("github.com/user/repo"), with no
    scheme. hyperref treats a scheme-less target as RELATIVE, so the arrow renders
    and highlights but resolves to nothing when clicked -- the link looks wired up
    and silently isn't. Defaulting to https makes it actually open.

    Kept separate from ``_safe_url`` because that one is also used for mailto
    targets, which are passed in bare and must not gain a scheme here.
    """
    url = str(url).strip()
    if url and not _HAS_SCHEME.match(url):
        url = "https://" + url.lstrip("/")
    return _safe_url(url)


# Per-template section headings. A template's manifest may rename a section ("experience"
# -> "Research Experience", "skills" -> "Technical Skills", "extracurricular" -> "Leadership")
# because the SAME single-column body serves every field and the heading is what tells a
# nursing recruiter and a PhD committee they are looking at their kind of resume. Set for
# the duration of one render_cv call; renderers read it through _label().
import contextvars as _cv
_HEADINGS: _cv.ContextVar[dict] = _cv.ContextVar("cv_headings", default={})


def _label(key: str, default: str) -> str:
    v = (_HEADINGS.get() or {}).get(key)
    return str(v).strip() if v and str(v).strip() else default


# Headings sit exactly where the template puts them: no stretch glue between sections, ever.
# A short page ends earlier and leaves white space at the bottom (Kofi, 2026-11-06).
def _heading(title: str) -> str:
    return (
        f"\n\\noindent \\textbf{{\\textsc{{\\large {title}}}}}\n\n"
        f"\\noindent \\rule[3pt]{{\\textwidth}}{{1pt}}\n"
    )


# Auto-bold metrics (opt-in via profile["bold_metrics"]): the numbers are what a
# recruiter scans for, so money, counts, percentages, month-year dates, and the
# zero-loss claim render bold in the summary and bullets. Order matters: month-year
# and $-amounts must win before the bare-number alternative claims their digits.
_MONTHS = (r"(?:January|February|March|April|May|June|July|August|September|"
           r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)")
_METRIC_RE = re.compile(
    r"(\b" + _MONTHS + r"\s+\d{4}\b"
    r"|\$\s?\d[\d,.]*(?:\s?(?:million|billion|[MKB]))?\+?"
    r"|\bzero (?:in-)?transit losses\b"
    r"|\b\d[\d,.]*\+?%?)", re.I)


def _esc_bold(s: str, bold: bool = False) -> str:
    """Escape for LaTeX; when ``bold``, wrap metric tokens in \\textbf. Marking
    happens BEFORE escaping (sentinels survive _esc; raw backslashes would not)."""
    if not bold:
        return _esc(s)
    marked = _METRIC_RE.sub(lambda m: "\x01" + m.group(1) + "\x02", str(s))
    return _esc(marked).replace("\x01", r"\textbf{").replace("\x02", "}")


def _bullets(items: list[str], bold: bool = False) -> str:
    if not items:
        return ""
    lines = "\n".join(f"\t\\item {_esc_bold(it, bold)}" for it in items if str(it).strip())
    # Natural justification (like the template): every line except the last fills to
    # both margins; the last line stays ragged. Bullets are SIZED (see the fit loop in
    # ui/session.py) so their last line lands naturally near the right margin — no
    # forced \parfillskip stretch, which spreads unnatural gaps across sparse lines.
    return f"\\begin{{itemize}}\n{lines}\n\\end{{itemize}}\n"


def _header(profile: dict) -> str:
    ident = profile.get("identity", {})
    name = _esc(ident.get("name", ""))
    contact_bits: list[str] = []
    if ident.get("address"):
        contact_bits.append(_esc(ident["address"]))
    if ident.get("phone"):
        contact_bits.append(_esc(ident["phone"]))
    if ident.get("email"):
        e = ident["email"]
        contact_bits.append(f"\\href{{mailto:{_safe_url(e)}}}{{{_esc(e)}}}")
    # "Website" carries a personal venture/portfolio site (e.g. a founder's own
    # product) for people who have one instead of (or besides) a blog.
    for label, key in (("LinkedIn", "linkedin"), ("GitHub", "github"),
                       ("Website", "website"), ("Blog", "blog")):
        if ident.get(key):
            contact_bits.append(f"\\href{{{_link_url(ident[key])}}}{{{label}}}")
    contact = " | ".join(contact_bits)
    return (
        "\n\\begin{center}\n"
        f"\t\\textbf{{\\LARGE {name}}}\n\n"
        f"\t{contact}\n"
        "\\end{center}\n"
    )


def _education(profile: dict) -> str:
    blocks = profile.get("education", [])
    if not blocks:
        return ""
    out = [_heading(_label("education", "Education"))]
    for b in blocks:
        # School stays BOLD small caps (the scannable anchor); the PROGRAM is REGULAR
        # weight so several degrees don't stack into an overwhelming wall of bold. The
        # right-aligned location/date stay bold, like every other section.
        out.append(
            f"\n\\noindent \\textbf{{\\textsc{{{_esc(b.get('school',''))}}}}} "
            f"\\hfill \\textbf{{{_esc(b.get('location',''))}}}\n\n"
            f"\\noindent {_esc(b.get('degree',''))} "
            f"\\hfill \\textbf{{{_esc(b.get('date',''))}}}\n"
        )
        if b.get("courses"):
            out.append(_bullets([f"Courses: {b['courses']}"]))
    out.append("\\vs\n")   # single section-end spacer, like the template
    return "".join(out)


def _skills(profile: dict) -> str:
    lines = profile.get("skills", {})
    if not lines:
        return ""
    # NO stretching, ever. The packer (ui/session.py _fill_skills) selects real
    # supported terms to fill each line as close to the right margin as natural
    # word-play allows, and microtype (preamble) absorbs a small residual via glyph
    # protrusion/expansion. Whatever is left simply stays ragged -- forced
    # \makebox[s] inter-word stretching reads as unnatural, so we don't do it.
    out = [_heading(_label("skills", "Skills")), "\n"]
    for label, value in lines.items():
        body = f"\\textbf{{{_esc(label)}:}} {_esc(value)}"
        out.append(f"\\noindent {body}\n\n")
    out.append("\\vs\n")
    return "".join(out)


def _entry_blocks(entries: list[dict], allow_link: bool = False,
                  bold: bool = False) -> str:
    """Render project/experience entries: org, then one-or-more role+bullets.
    When ``allow_link`` (projects), an entry's ``link``/``url`` is attached to a SMALL
    inline marker after the name — NOT the whole title. The heading stays black; only the
    tiny marker is blue, matching the template's minimal inline-word hyperlink style (no
    "blue takeover" of project headings)."""
    out: list[str] = []
    for e in entries:
        # Projects carry their heading in `name`, experience in `org`; accept either so a project
        # isn't rendered as a bare link with no title (the heading was blank whenever only `name`
        # was set). `title` is intentionally NOT a fallback here: it's the role line below.
        org = _esc(e.get("org") or e.get("name") or "")
        url = str(e.get("link") or e.get("url") or "").strip()
        # Minimal blue: a small linked arrow after the (black) title, never the title itself.
        link_marker = f"~\\href{{{_link_url(url)}}}{{$\\nearrow$}}" if (allow_link and url) else ""
        # A flagged placeholder is marked in the .tex too, so even a preview PDF
        # cannot present an invented project as real.
        suggested = (" \\textit{[SUGGESTED: replace before finalizing]}"
                     if (allow_link and e.get("suggested")) else "")
        out.append(
            f"\n\\noindent \\textbf{{\\textsc{{{org}}}}}{link_marker}{suggested} "
            f"\\hfill \\textbf{{{_esc(e.get('location',''))}}}\n"
        )
        roles = e.get("roles") or [
            {"title": e.get("title", ""), "dates": e.get("dates", ""),
             "bullets": e.get("bullets", [])}
        ]
        for r in roles:
            dates = _esc(r.get("dates", ""))
            title = _esc(r.get("title", ""))
            # Placeholder months (inserted under a build-override for a year-only date)
            # render in red so the person can see exactly which dates to replace.
            if r.get("dates_placeholder") and dates:
                dates = f"\\textcolor{{red}}{{{dates}}}"
            # A personal/portfolio project has no role title or work dates -- emit the
            # bullet directly under the linked name rather than a blank bold line (which
            # left an ugly gap between the project name and its bullet).
            if title or dates:
                out.append(
                    f"\n\\noindent \\textbf{{{title}}} \\hfill \\textbf{{{dates}}}\n"
                )
            out.append(_bullets(r.get("bullets", []), bold=bold))
    return "".join(out)


def _projects(profile: dict) -> str:
    entries = profile.get("projects", [])
    if not entries:
        return ""
    return (_heading(_label("projects", "Projects"))
            + _entry_blocks(entries, allow_link=True,
                            bold=bool(profile.get("bold_metrics")))
            + "\\vs\n")


def _experience(profile: dict) -> str:
    entries = profile.get("experience", [])
    if not entries:
        return ""
    return (_heading(_label("experience", "Experience"))
            + _entry_blocks(entries, bold=bool(profile.get("bold_metrics")))
            + "\\vs\n")


def _extracurricular(profile: dict) -> str:
    items = profile.get("extracurricular", [])
    if not items:
        return ""
    out = [_heading(_label("extracurricular", "Extracurricular")), "\n"]
    for it in items:
        date = _esc(it.get("date", ""))
        if it.get("dates_placeholder") and date:
            date = f"\\textcolor{{red}}{{{date}}}"   # flagged placeholder, like Experience
        out.append(
            f"\\noindent \\textbf{{{_esc(it.get('title',''))}}} "
            f"\\hfill \\textbf{{{date}}}\n"
        )
        out.append(_bullets(it.get("bullets", [])))
    out.append("\\vs\n")
    return "".join(out)


def _additional(profile: dict) -> str:
    interests = profile.get("interests")
    if not interests:
        return ""
    return (
        _heading(_label("additional", "Additional Information"))
        + f"\n\\noindent \\textbf{{{_label('interests', 'Interests')}:}} {_esc(interests)}\n"
    )


def _certifications(profile: dict) -> str:
    certs = profile.get("certifications") or []
    if isinstance(certs, str):
        certs = [c.strip() for c in re.split(r"[;\n]|,\s(?=[A-Z])", certs) if c.strip()]
    certs = [str(c).strip() for c in certs if str(c).strip()]
    if not certs:
        return ""
    return (_heading(_label("certifications", "Licenses \\& Certifications")) + "\n"
            + _bullets(certs) + "\\vs\n")


def _summary(profile: dict) -> str:
    """A leading professional-summary paragraph (for templates that open with one)."""
    text = str(profile.get("summary") or "").strip()
    if not text:
        return ""
    body = _esc_bold(text, bool(profile.get("bold_metrics")))
    return _heading(_label("summary", "Summary")) + f"\n\\noindent {body}\n\\vs\n"


# Maps a manifest section name -> its renderer. A template's `sections` list (from its
# manifest) drives which of these render and in what order, so a template's shape is data.
_SECTION_RENDERERS = {
    "summary": _summary,
    "education": _education,
    "skills": _skills,
    "projects": _projects,
    "experience": _experience,
    "extracurricular": _extracurricular,
    "interests": _additional,
    "certifications": _certifications,
}
_DEFAULT_SECTION_ORDER = ("education", "skills", "projects", "experience",
                          "extracurricular", "interests")


def _as_bullets(v) -> list[str]:
    """Coerce a bullets value (str | list[str] | list[dict]) into list[str]."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, list):
        out: list[str] = []
        for x in v:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
            elif isinstance(x, dict):
                t = x.get("text") or x.get("bullet") or x.get("description") or ""
                if str(t).strip():
                    out.append(str(t).strip())
        return out
    return [str(v)]


def _norm_role(r) -> dict:
    r = dict(r) if isinstance(r, dict) else {"title": str(r)}
    r["bullets"] = _as_bullets(r.get("bullets"))
    return r


def _norm_entry(e) -> dict:
    e = dict(e) if isinstance(e, dict) else {"org": str(e)}
    if isinstance(e.get("roles"), list):
        e["roles"] = [_norm_role(r) for r in e["roles"]]
    else:
        e["bullets"] = _as_bullets(e.get("bullets"))
    return e


def _norm_extra(it) -> dict:
    if isinstance(it, str):
        return {"title": it.strip(), "date": "", "bullets": []}
    it = dict(it) if isinstance(it, dict) else {"title": str(it)}
    it["bullets"] = _as_bullets(it.get("bullets"))
    return it


def _norm_skills(s) -> dict:
    """Coerce skills (dict | list[dict] | list[str] | str) into {label: text}."""
    if isinstance(s, dict):
        out = {}
        for k, v in s.items():
            out[str(k)] = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
        return out
    if isinstance(s, list):
        out = {}
        loose: list[str] = []
        for item in s:
            if isinstance(item, dict):
                label = item.get("label") or item.get("category") or "Skills"
                val = item.get("items") or item.get("value") or item.get("skills") or ""
                out[str(label)] = ", ".join(map(str, val)) if isinstance(val, list) else str(val)
            elif str(item).strip():
                loose.append(str(item).strip())
        if loose:
            out.setdefault("Skills", ", ".join(loose))
        return out
    if isinstance(s, str) and s.strip():
        return {"Skills": s.strip()}
    return {}


MAX_ROLES_ON_PAGE = 4

# Bullets per role by recency (Kofi, 2026-11-06): the more recent the job, the more bullets.
# (fewest, most) for the most recent role, then the second; every older role takes
# OLDER_BUDGET. Selection starts each role at its fewest; the recency fill (``_recency_fill``)
# brings the most recent role up toward its most first while the page is short, and the
# one-page fit cuts from the oldest roles first (``_drop_trailing_bullet``).
RECENCY_BUDGETS: tuple[tuple[int, int], ...] = ((3, 5), (3, 4))
OLDER_BUDGET = (2, 3)


def bullet_budget(rank: int, lead: bool = False) -> tuple[int, int]:
    """(fewest, most) bullets for the role at recency ``rank`` (0 is the most recent). A role
    the person leads with (``_lead``) gets the most recent role's budget."""
    if lead:
        return RECENCY_BUDGETS[0]
    return RECENCY_BUDGETS[rank] if rank < len(RECENCY_BUDGETS) else OLDER_BUDGET


_PRESENT_RE = re.compile(r"\b(?:present|current|currently|now|today|ongoing)\b", re.I)
_DATE_TOKEN = re.compile(r"(?:\b([A-Za-z]{3,9})\.?\s+)?\b((?:19|20)\d\d)\b")
_MONTH3 = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def _end_ordinal(dates) -> float | None:
    """A comparable end date for a role's date range: infinity for a current role, else
    year * 12 + month of the LAST date in the range, or None when it states no year."""
    s = str(dates or "")
    if _PRESENT_RE.search(s):
        return math.inf
    hits = list(_DATE_TOKEN.finditer(s))
    if not hits:
        return None
    month, year = hits[-1].group(1), int(hits[-1].group(2))
    return year * 12 + _MONTH3.get((month or "")[:3].lower(), 0)


def recency_ranks(roles) -> list[int]:
    """The recency rank of each role in ``roles`` ([(role, entry)] in page order): 0 for the
    most recent. A current ("Present") role comes first, then by end date, latest first. An
    undated role keeps the place the page gives it (it ranks just after the role above it),
    and ties keep page order."""
    keys, prev = [], math.inf
    for i, (role, entry) in enumerate(roles):
        end = _end_ordinal((role or {}).get("dates") or (entry or {}).get("dates"))
        if end is None:
            end = prev
        keys.append((-end, i))
        prev = end
    ranks = [0] * len(keys)
    for rank, i in enumerate(sorted(range(len(keys)), key=keys.__getitem__)):
        ranks[i] = rank
    return ranks


def _experience_roles(profile: dict) -> list:
    """(entry, role) for every experience role, nested or flat, in page order."""
    out = []
    for entry in profile.get("experience") or []:
        if not isinstance(entry, dict):
            continue
        roles = entry["roles"] if isinstance(entry.get("roles"), list) else [entry]
        out += [(entry, r) for r in roles if isinstance(r, dict)]
    return out


def ranked_roles(profile: dict) -> list:
    """(rank, entry, role) for every experience role on ``profile``, in page order."""
    pairs = _experience_roles(profile)
    ranks = recency_ranks([(r, e) for e, r in pairs])
    return [(ranks[i], e, r) for i, (e, r) in enumerate(pairs)]


def _budget_trim(role: dict, fewest: int, jd_terms: set) -> dict:
    """``role`` showing its ``fewest`` best bullets for the job; the rest wait in ``_reserve``
    (best match first) for the recency fill. Ranked by job overlap but re-emitted in the
    author's order: the bullets a person writes first are usually the ones they want read
    first. ``_reserve`` is a private mark, stripped before the page leaves the assembler."""
    role = dict(role)
    role.pop("_reserve", None)
    bullets = list(role.get("bullets") or [])
    if len(bullets) > fewest:
        ranked = sorted(range(len(bullets)), key=lambda i: len(jd_terms & _terms(bullets[i])),
                        reverse=True)
        role["_reserve"] = [bullets[i] for i in ranked[fewest:]]
        bullets = [bullets[i] for i in sorted(ranked[:fewest])]
    role["bullets"] = bullets
    return role


# Words that carry no signal about whether a role matches a job.
_STOP = frozenset("""
a an and are as at be by for from has have in into is it its of on or that the their to
was were will with your you our we they this these those than then them us can may must
role work team teams year years experience including etc using use used across while
""".split())


def _terms(text: str) -> set:
    return {w for w in re.findall(r"[a-z][a-z0-9+#.]{2,}", str(text).lower())
            if w not in _STOP}


def select_roles_for_jd(profile: dict, jd_text: str, *,
                        max_roles: int = MAX_ROLES_ON_PAGE,
                        choices: dict | None = None,
                        report: dict | None = None) -> dict:
    """Keep the roles that argue for THIS job, and set how much each one says.

    A long career does not shrink gracefully. Rendering every role forces the fitter to
    trim all of them to one line each, which throws away the specific, quantified
    evidence that makes a resume persuasive and leaves a uniform grey list. Choosing a
    few relevant roles is what a person does when they tailor by hand.

    Selection is by term overlap with the job description, with the most recent role
    always kept (recruiters read the top of the page first and a missing current role
    reads as a gap). Output stays in the profile's original order, so chronology is never
    rearranged to flatter the score. Each kept role shows the fewest bullets its recency
    allows (``bullet_budget``: 3 for the two most recent roles, 2 for older ones), its best
    matches for the job; the rest of its bullets wait in ``_reserve`` for the recency fill.
    Nothing is invented or rewritten here -- this only decides what is shown, which is the
    person's own material either way.

    ``choices`` (applicant/rules.py ``plan``) are the person's own choices for this kind of
    job (docs/applicant-brain.md, section 6), applied BEFORE the scoring: hidden items go,
    shown and lead items stay (and count toward the limit), ``prefer`` keeps the first of two,
    ``apart`` keeps the better match of two. With no choices this is exactly the scorer.
    ``report`` (a dict) is filled with what was shown and which choices changed it.
    """
    if choices and any(choices.get(k) for k in ("hide", "show", "lead", "prefer", "apart")):
        return _select_with_choices(profile, jd_text, choices, max_roles, report)
    experience = profile.get("experience") or []
    if report is not None:
        report.update(_report_shown(profile))
    if not experience:
        return profile

    jd_terms = _terms(jd_text)

    # Entries come in two shapes: nested ({org, roles:[{title, bullets}]}) and flat
    # ({org, title, bullets}), and the drafting path produces the flat one. A flat entry
    # IS its own single role; treating it as "no roles" silently emptied the whole
    # Experience section on every chat-drafted resume.
    def _roles_of(entry: dict) -> list:
        return entry.get("roles") if isinstance(entry.get("roles"), list) else [entry]

    flat = []                                   # (employer_index, role_index, role, entry)
    for ei, entry in enumerate(experience):
        for ri, role in enumerate(_roles_of(entry)):
            flat.append((ei, ri, role, entry))
    selected = _scorer_pick([(ei, ri, role, entry, None) for ei, ri, role, entry in flat],
                            jd_terms, max_roles)
    shown = [(ei, ri, role, entry) for ei, ri, role, entry in flat if (ei, ri) in selected]
    rank_of = dict(zip([(ei, ri) for ei, ri, _r, _e in shown],
                       recency_ranks([(role, entry) for _ei, _ri, role, entry in shown])))

    kept = []
    for ei, entry in enumerate(experience):
        nested = isinstance(entry.get("roles"), list)
        roles = [_budget_trim(role, bullet_budget(rank_of[(ei, ri)])[0], jd_terms)
                 for ri, role in enumerate(_roles_of(entry)) if (ei, ri) in selected]
        if not roles:
            continue
        if nested:
            entry = dict(entry)
            entry["roles"] = roles
            kept.append(entry)
        else:
            kept.append(roles[0])               # a flat entry keeps its flat shape

    profile = dict(profile)
    profile["experience"] = kept
    if report is not None:
        report.update(_report_shown(profile))
    return profile


# --------------------------------------------------------------------------- #
# The person's choices (docs/applicant-brain.md, section 6). Item ids come from
# applicant/rules.py so the builder, the brain and this selection name a role alike.
# --------------------------------------------------------------------------- #

def _role_entries(profile: dict) -> list:
    """(employer_index, role_index, role, entry, item id) for every role, in page order."""
    from applicant.rules import role_id, role_title
    out = []
    for ei, entry in enumerate(profile.get("experience") or []):
        roles = entry.get("roles") if isinstance(entry.get("roles"), list) else [entry]
        for ri, role in enumerate(roles):
            out.append((ei, ri, role, entry,
                        role_id(entry.get("org") or entry.get("company"), role_title(role))))
    return out


def _report_shown(profile: dict) -> dict:
    """{shown: [item ids]} for a page with no choices applied."""
    from applicant.rules import project_id
    shown = [iid for *_x, iid in _role_entries(profile)]
    shown += [project_id(p) for p in profile.get("projects") or [] if isinstance(p, dict)]
    return {"shown": shown, "excluded": {}, "dropped_pins": [], "applied": [], "lead": []}


def _scorer_pick(flat: list, jd_terms: set, max_roles: int) -> set:
    """The plain scorer (no choices): the selection ``select_roles_for_jd`` always made."""
    if len(flat) <= max_roles:
        return {(ei, ri) for ei, ri, *_r in flat}
    scored = []
    for ei, ri, role, entry, _iid in flat:
        text = " ".join([str(role.get("title", "")), str(entry.get("org", "")),
                         " ".join(map(str, role.get("bullets") or []))])
        scored.append((len(jd_terms & _terms(text)), -ei, -ri, ei, ri))
    scored.sort(reverse=True)
    selected = {(ei, ri) for _, _, _, ei, ri in scored[:max_roles]}
    selected.add(_most_recent(flat))             # the most recent role always survives
    return selected


def _most_recent(flat: list) -> tuple:
    """(employer_index, role_index) of the most recent role among ``flat`` entries."""
    ranks = recency_ranks([(x[2], x[3]) for x in flat])
    x = flat[ranks.index(0)]
    return (x[0], x[1])


def _rank_beats(a, b) -> bool:
    return list(a or []) > list(b or [])


def choice_exclusions(ids: list, scores: dict, choices: dict, base: set) -> tuple[dict, dict]:
    """Which of these item ids the choices leave out, and which they pin.

    ``ids`` in page order, ``scores`` {id: (job overlap, recency)}, ``base`` the ids the plain
    scorer would show. Returns (excluded {id: {rule, how, by}}, pins {id: {rule, rank, how}}).
    A hidden item goes. ``prefer`` drops the second of two when both are the person's, and the
    first takes the second's place when the scorer would have shown it. ``apart`` drops the
    weaker match of two (a ``prefer`` or a pin settles which). A choice made for a narrower
    kind of job beats a broader one when they disagree."""
    present = set(ids)
    hide, show, lead = choices.get("hide") or {}, choices.get("show") or {}, choices.get("lead") or {}
    pins = {i: dict(src, how="lead" if i in lead else "show") for i, src in {**show, **lead}.items() if i in present}
    excluded: dict = {}
    for i, src in hide.items():
        if i in present:
            excluded[i] = {"rule": src.get("rule", 0), "how": "hide"}
    prefer = [p for p in choices.get("prefer") or [] if len(p) >= 3]
    for a, b, src in prefer:
        if a not in present or b not in present or a in excluded or b in excluded:
            continue
        if b in pins and _rank_beats(pins[b].get("rank"), src.get("rank")):
            continue                                   # a narrower choice keeps b on the page
        excluded[b] = {"rule": src.get("rule", 0), "how": "prefer", "by": a}
        pins.pop(b, None)
        if b in base and a not in pins:
            pins[a] = dict(src, how="prefer")
    for p in choices.get("apart") or []:
        if len(p) < 3:
            continue
        a, b, src = p[0], p[1], p[2]
        if a not in present or b not in present or a in excluded or b in excluded:
            continue
        winner = next((x for x, y, _s in prefer if {x, y} == {a, b}), None)
        if winner is None:
            if (a in pins) != (b in pins):
                winner = a if a in pins else b
            else:
                winner = a if scores.get(a, (0, 0)) >= scores.get(b, (0, 0)) else b
        loser = b if winner == a else a
        if loser in pins and _rank_beats(pins[loser].get("rank"), src.get("rank")):
            continue
        excluded[loser] = {"rule": src.get("rule", 0), "how": "apart", "by": winner}
        pins.pop(loser, None)
    for i in list(pins):
        if i in excluded:
            pins.pop(i)
    return excluded, pins


def _select_with_choices(profile: dict, jd_text: str, choices: dict, max_roles: int,
                         report: dict | None) -> dict:
    """``select_roles_for_jd`` with the person's choices (see its docstring). Chronological
    order is kept; a pinned role over the limit is reported, the most recent ones kept."""
    from applicant.rules import project_id
    jd_terms = _terms(jd_text)
    flat = _role_entries(profile)
    projects = [p for p in profile.get("projects") or [] if isinstance(p, dict)]
    pids = [project_id(p) for p in projects]
    base = _scorer_pick(flat, jd_terms, max_roles) if flat else set()
    base_ids = {iid for ei, ri, _r, _e, iid in flat if (ei, ri) in base} | set(pids)
    scores = {}
    for n, (ei, ri, role, entry, iid) in enumerate(flat):
        text = " ".join([str(role.get("title", "")), str(entry.get("org", "")),
                         " ".join(map(str, role.get("bullets") or []))])
        scores[iid] = (len(jd_terms & _terms(text)), -n)
    for n, p in enumerate(projects):
        scores[pids[n]] = (len(jd_terms & _terms(" ".join([str(p.get("org", "")), str(p.get("title", "")),
                                                              " ".join(map(str, p.get("bullets") or []))]))), -n)
    excluded, pins = choice_exclusions([x[4] for x in flat] + pids, scores, choices, base_ids)

    role_pins = [x for x in flat if x[4] in pins]          # page order: most recent first
    dropped_pins = [x[4] for x in role_pins[max_roles:]]
    role_pins = role_pins[:max_roles]
    keep = {(x[0], x[1]) for x in role_pins}
    candidates = [x for x in flat if x[4] not in excluded and (x[0], x[1]) not in keep]
    room = max_roles - len(keep)
    if len(candidates) <= room:
        keep |= {(x[0], x[1]) for x in candidates}
    elif room > 0:
        ranked = sorted(candidates, key=lambda x: (scores[x[4]][0], -x[0], -x[1]), reverse=True)
        keep |= {(x[0], x[1]) for x in ranked[:room]}
        open_roles = [x for x in flat if x[4] not in excluded]
        if open_roles:
            keep.add(_most_recent(open_roles))           # the most recent role still survives
    lead_ids = {i for i, src in pins.items() if src.get("how") == "lead"}
    shown_flat = [x for x in flat if (x[0], x[1]) in keep]
    rank_of = dict(zip([(x[0], x[1]) for x in shown_flat],
                       recency_ranks([(x[2], x[3]) for x in shown_flat])))

    def _trim(role: dict, iid: str, key: tuple) -> dict:
        role = _budget_trim(role, bullet_budget(rank_of[key], lead=iid in lead_ids)[0], jd_terms)
        if iid in lead_ids:
            role["_lead"] = True
        return role

    kept = []
    for ei, entry in enumerate(profile.get("experience") or []):
        nested = isinstance(entry.get("roles"), list)
        roles = [_trim(role, iid, (e2, ri)) for (e2, ri, role, _en, iid) in flat
                 if e2 == ei and (e2, ri) in keep]
        if not roles:
            continue
        if nested:
            entry = dict(entry)
            entry["roles"] = roles
            kept.append(entry)
        else:
            kept.append(roles[0])
    kept_projects = []
    for n, p in enumerate(projects):
        if pids[n] in excluded:
            continue
        if pids[n] in lead_ids:
            p = dict(p, _lead=True)
        kept_projects.append(p)
    out = dict(profile)
    out["experience"] = kept
    if "projects" in profile:
        out["projects"] = kept_projects
    shown_roles = [x[4] for x in flat if (x[0], x[1]) in keep]
    shown = shown_roles + [pids[n] for n in range(len(projects)) if pids[n] not in excluded]
    lead_names = [str(x[3].get("org") or "") for x in flat if x[4] in lead_ids and (x[0], x[1]) in keep]
    lead_names += [str(p.get("org") or p.get("title") or "") for n, p in enumerate(projects)
                   if pids[n] in lead_ids and pids[n] not in excluded]
    if lead_names and out.get("summary"):
        out["summary"] = lead_summary(str(out["summary"]), lead_names)
    if report is not None:
        applied = set()
        for i, why in excluded.items():
            if i in base_ids and why.get("rule"):
                applied.add(int(why["rule"]))
        for i, src in pins.items():
            if i in shown and (i not in base_ids or i in lead_ids) and src.get("rule"):
                applied.add(int(src["rule"]))
        report.update({"shown": shown, "excluded": excluded, "dropped_pins": dropped_pins,
                       "applied": sorted(applied), "lead": [i for i in shown if i in lead_ids],
                       "pins": {i: {"rule": s.get("rule", 0), "how": s.get("how")} for i, s in pins.items()}})
    return out


def choice_pool(profile: dict, report: dict | None) -> dict:
    """The person's whole material minus what their choices left out (hidden, or the loser of
    a ``prefer`` or ``apart``), so the grow pass never brings a hidden role back."""
    ex = (report or {}).get("excluded") or {}
    if not ex:
        return profile
    from applicant.rules import project_id
    out = dict(profile)
    exp = []
    for entry in profile.get("experience") or []:
        nested = isinstance(entry.get("roles"), list)
        roles = [r for (_e, _r, r, en, iid) in _role_entries({"experience": [entry]}) if iid not in ex]
        if not roles:
            continue
        if nested:
            exp.append(dict(entry, roles=roles))
        else:
            exp.append(roles[0])
    out["experience"] = exp
    if "projects" in profile:
        out["projects"] = [p for p in profile.get("projects") or [] if not isinstance(p, dict) or project_id(p) not in ex]
    return out


def lead_summary(summary: str, names: list) -> str:
    """The summary names the lead item first: the sentence that names it moves to the front.
    Only the person's own sentences are reordered; no word is added or changed."""
    parts = _split_sentences(summary)
    if len(parts) < 2:
        return summary
    low = [p.lower() for p in parts]
    for name in names:
        n = str(name or "").strip().lower()
        if not n:
            continue
        hit = next((i for i, p in enumerate(low) if n in p), None)
        if hit:
            return " ".join([parts[hit]] + parts[:hit] + parts[hit + 1:])
        if hit == 0:
            return summary
    return summary


def strip_choice_marks(profile: dict) -> dict:
    """Remove the selection's private marks (``_lead``, ``_reserve``) before the page leaves
    the assembler."""
    for e in (profile.get("projects") or []) + (profile.get("experience") or []):
        if isinstance(e, dict):
            e.pop("_lead", None)
            e.pop("_reserve", None)
            for r in e.get("roles") or []:
                if isinstance(r, dict):
                    r.pop("_lead", None)
                    r.pop("_reserve", None)
    return profile


def _merge_same_employer(entries: list) -> list:
    """Collapse adjacent entries for the SAME employer into one block of roles.

    A promotion is one employer and two titles, and that is how a resume must show it:
    the company name once, with the roles stacked beneath. Carrying them as two separate
    entries renders the employer's name twice in a row -- which reads as a formatting
    bug to anyone looking at the page, and buries the promotion it should be advertising.

    Only ADJACENT entries merge, so a genuine boomerang (Bank -> Startup -> Bank) keeps
    its real chronology instead of being silently stitched into one continuous stint.
    Matching ignores case and surrounding punctuation; a differing location is kept from
    the first entry, since that is the one whose header is rendered.
    """
    merged: list = []
    for entry in entries:
        key = re.sub(r"[^a-z0-9]+", " ", str(entry.get("org", "")).lower()).strip()
        if merged and key and merged[-1][0] == key:
            previous = merged[-1][1]
            previous.setdefault("roles", []).extend(entry.get("roles") or [])
            continue
        merged.append((key, entry))
    return [entry for _, entry in merged]


def normalize_profile(profile: dict) -> dict:
    """Coerce a possibly-loose profile (e.g. real LLM JSON) into the shape the
    renderer expects. Idempotent and total — never raises on odd input."""
    p = dict(profile)

    edu = []
    for e in p.get("education", []) or []:
        e = dict(e) if isinstance(e, dict) else {"school": str(e)}
        if isinstance(e.get("courses"), list):
            e["courses"] = ", ".join(map(str, e["courses"]))
        edu.append(e)
    p["education"] = edu

    p["skills"] = _norm_skills(p.get("skills"))
    p["projects"] = [_norm_entry(e) for e in (p.get("projects", []) or [])]
    p["experience"] = _merge_same_employer(
        [_norm_entry(e) for e in (p.get("experience", []) or [])])
    p["extracurricular"] = [_norm_extra(it) for it in (p.get("extracurricular", []) or [])]

    interests = p.get("interests")
    if isinstance(interests, list):
        p["interests"] = ", ".join(map(str, interests))
    ident = p.get("identity", {})
    p["identity"] = ident if isinstance(ident, dict) else {}

    # Reformat dates/degrees and scrub AI-tell dashes so every render matches the
    # template's conventions (December 2022, full degree names, plain prose).
    from tailoring.conform import conform_profile
    conform_profile(p)
    return p


# Make the document report its own page fill into the log, so the fit loop can tell
# "fits on one page" from "fills one page". Registered from the BODY, never the preamble,
# so no template's formatting is touched (CLAUDE.md §8). It contributes no vertical
# material (\typeout is log-only; \par in vertical mode is a no-op), so a render with the
# probe and one without lay out identically — which is what lets us measure with it and
# ship without it.
_FILL_PROBE = r"\AtEndDocument{\par\typeout{TAILORFILL=\the\pagetotal:\the\textheight}}"


def render_cv(preamble: str, profile: dict, sections=None, probe: bool = False,
              headings: dict | None = None) -> str:
    """Render a full CV `.tex` from ``profile`` using the template's preamble.

    ``sections`` is the selected template's ordered section list (from its manifest); each
    name maps to a renderer in ``_SECTION_RENDERERS``. Defaults to the classic order so
    existing callers are unchanged. This is what lets a summary-first / no-extracurricular
    template render differently without touching this function.

    ``probe`` adds the fill measurement hook, for the fit loop. The shipped `.tex` is
    rendered without it so the person never sees a debug line in their own source.
    """
    profile = normalize_profile(profile)
    order = list(sections) if sections else list(_DEFAULT_SECTION_ORDER)
    body = [_header(profile)]
    token = _HEADINGS.set(dict(headings or {}))
    try:
        for name in order:
            renderer = _SECTION_RENDERERS.get(name)
            if renderer:
                body.append(renderer(profile))
    finally:
        _HEADINGS.reset(token)
    head = f"{preamble}\n{_FILL_PROBE}" if probe else preamble
    # The template's own type size, margins and spacing, always: nothing in the body changes
    # them (CLAUDE.md sec 8; the page-enlarging layout steps were removed on 2026-11-06).
    return f"{head}\n{''.join(body)}\n{DOC_END}\n"


# --------------------------------------------------------------------------- #
# Tailoring the profile's bullet language toward the JD, and one-page fitting.
# --------------------------------------------------------------------------- #

def _iter_bullet_slots(profile: dict):
    """Yield (container_list, index) for every editable bullet in the profile.

    A thin view over ``_iter_bullet_slots_with_grounding`` so there is exactly one walk
    order to keep correct: the tailoring and fit/shrink stages visit the same slots."""
    for bl, i, _ in _iter_bullet_slots_with_grounding(profile):
        yield bl, i


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(text.strip()) if s]


def _iter_bullet_slots_with_grounding(profile: dict, with_label: bool = False):
    """Yield (container_list, index, grounding_text) for every editable bullet.

    ``grounding_text`` is everything the bullet's OWN entry says: the employer/project
    with its title(s), tech line, and sibling bullets. It is the only material a reword of
    that bullet may draw skills from (see ``keywords.introduced_skills``). With
    ``with_label`` each item also carries (section, "Employer, Title") for the review."""
    from .keywords import profile_text

    projects = profile.get("projects", [])
    for n, e in enumerate(projects + profile.get("experience", [])):
        section = "projects" if n < len(projects) else "experience"
        grounding = profile_text(e)
        roles = e.get("roles") or [e]
        for r in roles:
            bl = r.get("bullets")
            if isinstance(bl, list):
                for i in range(len(bl)):
                    if with_label:
                        yield bl, i, grounding, section, entry_label(e, r)
                    else:
                        yield bl, i, grounding
    for it in profile.get("extracurricular", []):
        bl = it.get("bullets")
        if isinstance(bl, list):
            grounding = profile_text(it)
            for i in range(len(bl)):
                if with_label:
                    yield bl, i, grounding, "extracurricular", entry_label(it)
                else:
                    yield bl, i, grounding


def _dedupe_entry_bullets(profile: dict) -> int:
    """Drop, in place, any bullet that repeats an earlier bullet of the SAME entry (role,
    project, or activity), so one fact never prints twice (tailoring/filler.same_fact).
    Returns how many were dropped."""
    from .filler import dedupe_bullets

    dropped = 0
    lists = []
    for e in (profile.get("projects") or []) + (profile.get("experience") or []):
        for r in (e.get("roles") or [e]) if isinstance(e, dict) else ():
            if isinstance(r, dict) and isinstance(r.get("bullets"), list):
                lists.append(r)
    lists += [it for it in (profile.get("extracurricular") or [])
              if isinstance(it, dict) and isinstance(it.get("bullets"), list)]
    for holder in lists:
        kept = dedupe_bullets(holder["bullets"])
        dropped += len(holder["bullets"]) - len(kept)
        holder["bullets"][:] = kept
    return dropped


def entry_label(entry: dict, role: dict | None = None) -> str:
    """"Acme Analytics, Data Engineer": the employer or project, then the role title."""
    org = str(entry.get("org") or entry.get("name") or entry.get("company") or "").strip()
    title = str((role if role is not None else entry).get("title") or "").strip()
    if not org:
        org, title = title, ""
    if title == org:
        title = ""
    return ", ".join(x for x in (org, title) if x) or "Entry"


def _reword_smuggled_unsupported(original: str, new: str, jd_text: str,
                                 grounding: str) -> bool:
    """True if a reword claims a skill that the bullet's own role/project never shows.

    ``grounding`` is that entry's text, NOT the whole profile: a skill that is real in one
    part of the person's history (an AI side project) must not migrate onto another (a
    banking role) where it never happened (issue #279). We never surgically strip a word
    (that mangles grammar); the caller discards the whole reword and keeps the real bullet,
    so the CV stays honest and the gap surfaces in the coverage report instead."""
    from .filler import is_padded
    from .keywords import introduced_skills

    return bool(introduced_skills(original, new, grounding, jd_text)) or is_padded(original, new)


def _tailor_bullets(profile: dict, jd_text: str, llm: LLMBackend, *,
                    material: tuple = (), reverted: list | None = None,
                    origins: dict | None = None) -> dict:
    """Return a copy of ``profile`` with bullets reworded toward the JD.

    Bullets are plain text here — escaping happens once, at render time (`_esc`),
    so the LLM output must NOT be pre-escaped. Three safety nets apply per bullet:
    (1) a reword that claims a skill its own role/project doesn't show is discarded in
    favor of the person's real bullet (anti-fabrication, §8); (2) a reword that blew past
    the length budget is rewritten shorter via ``shorten_bullet`` (re-emphasis, not
    expansion, §8) — a rewrite, never a mid-sentence cut, so bullets stay complete
    sentences and the fit/shrink stage still handles page-level reduction; (3) the fact gate
    (tailoring/fact_gate.py): a reword that states a figure, a name, a tool, an acronym or a
    credential found nowhere in ``material`` (the person's profile, brain facts) or the source
    bullet is discarded too, and recorded in ``reverted`` so the review can say why.

    ``origins`` (a dict) is filled with {tailored text: source text}, so a later cut can be
    traced back to the person's own bullet.
    """
    import copy
    from .fact_gate import known_material, source_figures

    tailored = copy.deepcopy(profile)
    # Weave only real supported SKILLS into bullets — never company/location terms.
    supported = supported_skills(jd_text, profile)
    sources = (profile,) + tuple(material or ())
    known = known_material(*sources)
    known_figs = source_figures(*sources)
    for bl, i, grounding, section, label in _iter_bullet_slots_with_grounding(tailored, with_label=True):
        bl[i] = _tailor_one(str(bl[i]), [bl[k] for k in range(len(bl)) if k != i], grounding,
                            jd_text, llm, supported=supported, known=known, known_figs=known_figs,
                            section=section, label=label, reverted=reverted, origins=origins)
    return tailored


def _tailor_one(original: str, siblings: list, grounding: str, jd_text: str, llm: LLMBackend, *,
                supported: list, known: dict, known_figs: set, section: str = "", label: str = "",
                reverted: list | None = None, origins: dict | None = None) -> str:
    """ONE bullet reworded toward the JD, or ``original`` when any safety net refuses the
    reword (see ``_tailor_bullets``). ``siblings`` are the other bullets of its entry and
    ``grounding`` everything that entry says."""
    from .fact_gate import gate_rewrite
    from .filler import repeats_sibling
    from .keywords import term_present

    budget = _bullet_budget(original)
    # Offer the model only the JD skills THIS role/project demonstrates, so it is never
    # invited to carry a skill from one part of the person's history onto another.
    role_terms = [t for t in supported if term_present(t, grounding)]
    new = llm.reword_bullet(
        original_text=original,
        supported_jd_terms=role_terms,
        target_len_chars=budget,
        jd_text=jd_text,
    ).strip()
    if not new:
        return original
    # (1) Discard a reword that fabricated a skill for this role — keep the real bullet.
    if _reword_smuggled_unsupported(original, new, jd_text, grounding):
        return original
    # (1b) Discard a reword that restates a sibling bullet's clause (the sibling is in
    # its grounding, so a reword can copy it): one fact, one bullet per entry.
    if repeats_sibling(original, new, siblings):
        return original
    # (2) Enforce the per-bullet length ceiling. Only fires on a wild over-expansion
    # past the budget (floor 165 chars); a normal one-line bullet never trips it.
    if len(new) > budget:
        shorter = str(llm.shorten_bullet(new, budget) or "").strip()
        if shorter and (_reword_smuggled_unsupported(original, shorter, jd_text, grounding)
                        or repeats_sibling(original, shorter, siblings)):
            return original              # the shortening fabricated: keep the real bullet
        if shorter:
            new = shorter
    # (3) The fact gate: nothing on the page the person's material does not state.
    bad = gate_rewrite(original, new, known=known, known_figures=known_figs)
    if bad:
        if reverted is not None:
            reverted.append({"section": section, "label": label, "bullet": original,
                             "attempted": new, "claims": bad, "action": "reverted"})
        return original
    if origins is not None and new != original:
        origins[new] = original
    return new


# --------------------------------------------------------------------------- #
# Cuts made to fit one page, kept so the person can put them back (never lost silently).
# --------------------------------------------------------------------------- #

def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def trim_id(kind: str, label: str, text: str) -> str:
    """A stable id for one cut, the same across rebuilds of the same material."""
    import hashlib
    raw = f"{kind}|{_norm_text(label)}|{_norm_text(text)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


class _Cuts:
    """Records what the fit loop cuts, and refuses to cut what the person put back."""

    def __init__(self, trims: list | None, keep, origins: dict | None):
        self.trims = trims
        self.keep = set(keep or ())
        self.origins = origins or {}

    def kept(self, kind: str, label: str, text: str) -> bool:
        if not self.keep:
            return False
        ids = {trim_id(kind, label, text)}
        src = self.origins.get(text)
        if src:
            ids.add(trim_id(kind, label, src))
        return bool(ids & self.keep)

    def add(self, kind: str, section: str, label: str, text: str, **extra) -> None:
        if self.trims is None:
            return
        src = self.origins.get(text, text) if isinstance(text, str) else text
        self.trims.append({"id": trim_id(kind, label, text), "kind": kind, "section": section,
                           "label": label, "text": text, "source": src, **extra})


def _role_lists(profile: dict):
    """(bullets list, section, label, is_lead) for every role/project/extracurricular entry."""
    projects = profile.get("projects", [])
    for n, e in enumerate(projects + profile.get("experience", [])):
        section = "projects" if n < len(projects) else "experience"
        for r in (e.get("roles") or [e]):
            if isinstance(r.get("bullets"), list):
                yield r["bullets"], section, entry_label(e, r), bool(r.get("_lead") or e.get("_lead"))
    for it in profile.get("extracurricular", []):
        if isinstance(it.get("bullets"), list):
            yield it["bullets"], "extracurricular", entry_label(it), False


def _drop_trailing_bullet(profile: dict, cuts: "_Cuts | None" = None) -> bool:
    """Drop the last bullet of one entry that still has more than one, by recency.

    Order (Kofi, 2026-11-06: the more recent the job, the more bullets): first the extra
    bullets of experience roles above their fewest (``bullet_budget``), oldest role first, so
    the most recent role keeps its extras longest; then a project or activity, the fullest
    first; only then a role below its fewest, again oldest first, so neither of the two most
    recent roles goes below three while an older role still has more than two.

    Never truncates text — a dropped bullet is removed whole, so every surviving
    bullet stays a complete sentence. Returns True if something was dropped. A bullet the
    person put back is skipped: the next one up goes instead. A role the person leads with
    keeps its bullets while any other entry can give one up.
    """
    cuts = cuts or _Cuts(None, (), None)
    lists = list(_role_lists(profile))
    rank_of = {id(r.get("bullets")): rank for rank, _e, r in ranked_roles(profile)}

    def droppable(bl, label):
        return [i for i in range(len(bl) - 1, 0, -1) if not cuts.kept("bullet", label, str(bl[i]))]
    candidates = [x for x in lists if len(x[0]) > 1 and droppable(x[0], x[2])]
    others = [x for x in candidates if not x[3]]
    if others:
        candidates = others
    if not candidates:
        return False
    roles = [x for x in candidates if x[1] == "experience" and id(x[0]) in rank_of]
    rest = [x for x in candidates if not (x[1] == "experience" and id(x[0]) in rank_of)]
    extra = [x for x in roles if len(x[0]) > bullet_budget(rank_of[id(x[0])], x[3])[0]]
    if extra:
        pick = max(extra, key=lambda x: rank_of[id(x[0])])
    elif rest:
        pick = max(rest, key=lambda x: sum(len(str(b)) for b in x[0]))
    else:
        pick = max(roles, key=lambda x: rank_of[id(x[0])])
    bl, section, label, _lead = pick
    i = droppable(bl, label)[0]
    cuts.add("bullet", section, label, str(bl[i]), index=i)
    bl.pop(i)
    return True


def _all_roles(profile: dict):
    """(role_dict) for every experience/project role, in document order."""
    for e in profile.get("projects", []) + profile.get("experience", []):
        for r in (e.get("roles") or [e]):
            yield r


def _shrink_once(profile: dict, trims: list | None = None, keep=None,
                 origins: dict | None = None) -> bool:
    """Reduce content by ONE step so the CV fits one page, keeping every bullet a
    complete sentence and never character-truncating.

    Least-destructive first. Each call performs exactly one reduction and returns
    True; when nothing remains to reduce it returns False.

    ``trims`` (a list) receives one record per cut (what, where, the text), so nothing cut to
    fit is lost: the builder offers "Put back" for each. ``keep`` holds the ids
    (``trim_id``) the person put back; those are never cut again, the next thing goes instead.
    """
    import copy
    cuts = _Cuts(trims, keep, origins)
    # 1. A multi-sentence bullet: drop its last sentence.
    slots = sorted(_iter_bullet_slots_with_grounding(profile, with_label=True),
                   key=lambda s: len(str(s[0][s[1]])), reverse=True)
    for bl, i, _g, section, label in slots:
        sentences = _split_sentences(str(bl[i]))
        if len(sentences) > 1 and not cuts.kept("sentence", label, str(bl[i])):
            before = str(bl[i])
            bl[i] = " ".join(sentences[:-1]).strip()
            cuts.add("sentence", section, label, before, cut=sentences[-1], after=bl[i])
            return True

    # 2. Drop a trailing bullet: the oldest role's extras first (``_drop_trailing_bullet``).
    if _drop_trailing_bullet(profile, cuts):
        return True

    # 3. One-shot: cap every "Courses:" line to at most 6 courses.
    changed = False
    for e in profile.get("education", []):
        items = [c.strip() for c in str(e.get("courses", "")).split(",") if c.strip()]
        label = str(e.get("school") or "Education")
        if len(items) > 6 and not cuts.kept("courses", label, str(e.get("courses", ""))):
            cuts.add("courses", "education", label, str(e.get("courses", "")),
                     cut=", ".join(items[6:]))
            e["courses"] = ", ".join(items[:6])
            changed = True
    if changed:
        return True

    # 4. One-shot: cap every skills line to at most 8 items.
    skills = profile.get("skills", {})
    changed = False
    for k, v in list(skills.items()):
        items = [x.strip() for x in str(v).split(",") if x.strip()]
        if len(items) > 8 and not cuts.kept("skills", str(k), str(v)):
            cuts.add("skills", "skills", str(k), str(v), cut=", ".join(items[8:]))
            skills[k] = ", ".join(items[:8])
            changed = True
    if changed:
        return True

    # 5. Extracurricular is a MANDATORY page-filling section (hard rule) — NEVER drop the
    # whole section to fit. Only shorten it, by dropping a trailing bullet from its last
    # multi-bullet entry; the heading + at least one entry always stays.
    for x in reversed(profile.get("extracurricular", []) or []):
        bl = x.get("bullets")
        if isinstance(bl, list) and len(bl) > 1:
            label = entry_label(x)
            idx = next((i for i in range(len(bl) - 1, 0, -1)
                        if not cuts.kept("bullet", label, str(bl[i]))), None)
            if idx is None:
                continue
            cuts.add("bullet", "extracurricular", label, str(bl[idx]), index=idx)
            bl.pop(idx)
            return True

    # 6. Drop a trailing project entry (keep at least one).
    projects = profile.get("projects", [])
    if len(projects) > 1:
        idx = next((i for i in range(len(projects) - 1, 0, -1)
                    if not cuts.kept("project", entry_label(projects[i]), entry_label(projects[i]))), None)
        if idx is not None:
            label = entry_label(projects[idx])
            cuts.add("project", "projects", label, label, index=idx, entry=copy.deepcopy(projects[idx]))
            projects.pop(idx)
            return True

    # 7. Skills keep 5 to 6 full-width categories (the owner's layout rule, linefit.py):
    # drop a line ONLY above the 5-category floor, so it can go 6 -> 5 but never below 5.
    if len(profile.get("skills", {})) > SKILL_LINES_FLOOR:
        last = next((k for k in reversed(list(profile["skills"]))
                     if not cuts.kept("skills_line", str(k), str(k))), None)
        if last is not None:
            cuts.add("skills_line", "skills", str(last), str(last), value=profile["skills"][last])
            del profile["skills"][last]
            return True

    # 8. Drop a trailing education entry if there is more than one.
    edu = profile.get("education", [])
    if len(edu) > 1:
        idx = next((i for i in range(len(edu) - 1, 0, -1)
                    if not cuts.kept("education", str(edu[i].get("school") or ""),
                                     str(edu[i].get("school") or ""))), None)
        if idx is not None:
            school = str(edu[idx].get("school") or "Education")
            cuts.add("education", "education", school, school, index=idx, entry=copy.deepcopy(edu[idx]))
            edu.pop(idx)
            return True

    # 9. Drop a remaining single bullet from the last role that has one.
    for bl, section, label, _lead in reversed([x for x in _role_lists(profile)
                                                if x[1] != "extracurricular" and x[0]]):
        idx = next((i for i in range(len(bl) - 1, -1, -1)
                    if not cuts.kept("bullet", label, str(bl[i]))), None)
        if idx is None:
            continue
        cuts.add("bullet", section, label, str(bl[idx]), index=idx)
        bl.pop(idx)
        return True

    # 10. Last resort: drop a trailing experience entry (keep at least one).
    exp = profile.get("experience", [])
    if len(exp) > 1:
        idx = next((i for i in range(len(exp) - 1, 0, -1)
                    if not cuts.kept("role", entry_label(exp[i]), entry_label(exp[i]))), None)
        if idx is not None:
            label = entry_label(exp[idx])
            cuts.add("role", "experience", label, label, index=idx, entry=copy.deepcopy(exp[idx]))
            exp.pop(idx)
            return True

    return False


def restore_trim(profile: dict, trim: dict) -> bool:
    """Put one cut back into ``profile`` (the person's material), in its own place. Returns
    True when the content is back. The caller then rebuilds with ``trim["id"]`` kept, which
    re-runs the one-page fit check."""
    kind, label = trim.get("kind"), str(trim.get("label") or "")
    text, source = str(trim.get("text") or ""), str(trim.get("source") or "")

    def _lists(section):
        for bl, sec, lab, _lead in _role_lists(profile):
            if sec == section and _norm_text(lab) == _norm_text(label):
                yield bl
    if kind == "bullet":
        for bl in _lists(trim.get("section")):
            for i, b in enumerate(bl):
                if _norm_text(b) in (_norm_text(text), _norm_text(source)):
                    bl[i] = text                 # the wording the person saw cut
                    return True
            bl.insert(min(int(trim.get("index") or len(bl)), len(bl)), text)
            return True
        return False
    if kind == "sentence":
        after = _norm_text(trim.get("after") or "")
        for bl in _lists(trim.get("section")):
            for i, b in enumerate(bl):
                if _norm_text(b) in (after, _norm_text(text), _norm_text(source)):
                    bl[i] = text
                    return True
        return False
    if kind in ("project", "role", "education"):
        key = {"project": "projects", "role": "experience", "education": "education"}[kind]
        items = profile.setdefault(key, [])
        name = (lambda e: str(e.get("school") or "")) if kind == "education" else entry_label
        if any(_norm_text(name(e)) == _norm_text(label) for e in items if isinstance(e, dict)):
            return True                          # still in the material: keeping it is enough
        entry = trim.get("entry")
        if not isinstance(entry, dict):
            return False
        items.insert(min(int(trim.get("index") or len(items)), len(items)), entry)
        return True
    if kind == "courses":
        for e in profile.get("education") or []:
            if _norm_text(e.get("school") or "Education") == _norm_text(label):
                e["courses"] = text
                return True
        return False
    if kind == "skills":
        sk = profile.setdefault("skills", {})
        if isinstance(sk, dict):
            sk[label] = text
            return True
        return False
    if kind == "skills_line":
        sk = profile.setdefault("skills", {})
        if isinstance(sk, dict):
            sk.setdefault(label, str(trim.get("value") or ""))
            return True
        return False
    return False


# What "fills the page" means, as a number. The template's own reference sample
# (config/resume_shetty.tex) occupies 98.1% of its text block — that is the shape the
# person is promised. 0.90 is the floor: it still passes a CV whose last bullet can't be
# split without ragging the page, while rejecting the half pages this rule exists to stop
# (memory complete-cv-fills-page). Underfull is NOT fixed by padding prose — §8 caps every
# bullet at ±10% of its original length, so a thin page means thin MATERIAL, and the honest
# repair is to ask the person for more (the enrichment interview), never to invent.
TARGET_FILL = 0.90
# Upper bound: the template's geometry has NO bottom margin (margin=0cm, only
# left/right/top overridden), so a page can compile "clean" (1 page, no overfull
# hbox) while its last lines sit AT — or measurably past — the paper's bottom
# edge. Found live: an accepted CV whose lowest text was 1.05cm BELOW the page.
# fill > MAX_FILL is therefore an overflow the healer must shrink, not ship;
# 0.97 of \textheight leaves ~0.8cm of real clearance above the edge.
MAX_FILL = 0.97
# The recency fill (``_recency_fill``) adds bullets while the page is short of THIS, a stricter
# bar than TARGET_FILL: a page at 91 percent is not "underfull" enough to ask the person for
# more material, but it still shows a band of empty paper that one more bullet on a recent
# role can use. The template's type size, margins and spacing never change to fill a page
# (the layout steps that did were removed on 2026-11-06).
FILL_GOAL = 0.95


@dataclass
class AssembleResult:
    ok: bool
    status: str                      # "clean" | "overflow" | "underfull"
    tex_source: str
    pdf_path: Path | None
    coverage: CoverageReport
    compile: CompileResult
    attempts: int
    profile_used: dict
    empty_template_source: str = ""  # the original template (shape/placeholder)
    fill_ratio: float | None = None  # share of the text block used; None if unmeasured
    # Figures in the tailored bullets with no origin in the person's material. Flagged
    # for the review, never a reason to fail the build (tailoring/fact_gate.py).
    fact_flags: list = field(default_factory=list)
    # The selected, pre-tailoring material the page was built from (same roles, same bullet
    # order as ``profile_used``), so a later review can show each bullet before and after.
    source_profile: dict | None = None
    # What the person's choices did on this page (select_roles_for_jd ``report``): the item
    # ids shown, the ones left out and why, and which saved choices changed the page.
    choices_report: dict | None = None
    # What the one-page fit cut, one record per cut (``_shrink_once``), so the builder can
    # offer "Put back" and nothing cut to fit is ever lost silently.
    trims: list = field(default_factory=list)
    # Rewrites the fact gate refused (a name, tool, acronym, credential or figure the person's
    # material never states): the bullet kept the person's own wording.
    reverted: list = field(default_factory=list)
    # The full-width line check (tailoring/linefit.py), measured on the compiled PDF: every
    # skills line and every bullet, before and after the fix loop, and what real material is
    # still missing for every line to run full (the builder asks the person about it).
    line_fit: dict | None = None
    line_fit_before: dict | None = None
    line_gaps: dict | None = None
    # The page as it stood before the line fit, so a review can render the same build without
    # the fit and compare.
    pre_fit_profile: dict | None = None
    # What the recency fill added to a short page, one record per bullet: {section, label,
    # bullet, kind, source}. ``kind`` is "saved" (the person's own bullet that selection left
    # out), "split" (one of the parts of a bullet that merged several accomplishments) or
    # "written" (a new bullet written from that role's own material; the person checks these,
    # see ``written``).
    added: list = field(default_factory=list)
    # Plain notes about bullet counts, e.g. a role left below its fewest bullets because the
    # person gave no more material for it.
    bullet_notes: list = field(default_factory=list)

    @property
    def written(self) -> list:
        """The bullets the recency fill WROTE (not the person's own wording), for review."""
        return [a for a in self.added if a.get("kind") == "written"]

    @property
    def underfull(self) -> bool:
        """One page, compiles, no overfull — and still visibly short of a full page.

        The signal that the PROFILE is thin, not that the layout is wrong. Callers use it
        to ask the person for more material rather than ship a half page.
        """
        return self.fill_ratio is not None and self.fill_ratio < TARGET_FILL

    def summary(self) -> str:
        fill = "unmeasured" if self.fill_ratio is None else f"{self.fill_ratio:.0%}"
        return "\n".join(
            [
                f"Status: {self.status}",
                f"Compile: pages={self.compile.pages}, "
                f"overfull={self.compile.overfull_count}, attempts={self.attempts}, "
                f"page filled={fill}",
                "",
                self.coverage.render(),
            ]
        )


def _count_material(profile: dict) -> tuple[int, int]:
    """(roles, bullets) across the experience section, nested or flat."""
    roles = bullets = 0
    for entry in profile.get("experience") or []:
        nested = entry.get("roles") if isinstance(entry.get("roles"), list) else [entry]
        for role in nested:
            roles += 1
            bullets += len(role.get("bullets") or [])
    return roles, bullets


def _dropped_roles(full: dict, selected: dict) -> bool:
    """Did role selection leave out any of the person's roles?"""
    return _count_material(full)[0] != _count_material(selected)[0]


# --------------------------------------------------------------------------- #
# The recency fill: a short page gets more bullets on its most recent roles (Kofi, 2026-11-06).
# --------------------------------------------------------------------------- #

def _role_key(entry: dict, role: dict) -> tuple:
    return (_norm_text(entry.get("org") or entry.get("name") or entry.get("company") or ""),
            _norm_text(role.get("title") or ""))


def _role_material(entry: dict, role: dict, source_role: dict | None, profile: dict,
                   material=()) -> str:
    """Everything the person's own material says about THIS role, and nothing about another:
    the employer line, the role's title and dates, all of its bullets (shown, waiting, and as
    first written), the sentences of their summary and saved answers that name the employer,
    and the skills they listed that this text shows. A written bullet may draw only on this."""
    from .fact_gate import _strings
    from .keywords import term_present

    head = {k: v for k, v in entry.items() if k not in ("roles", "bullets", "_reserve", "_lead")}
    parts = _strings(head) + _strings({k: v for k, v in role.items() if k != "_lead"})
    if source_role:
        parts += _strings(source_role.get("bullets") or [])
    org = str(entry.get("org") or entry.get("name") or entry.get("company") or "").strip().lower()
    if len(org) >= 3:
        said = _split_sentences(str(profile.get("summary") or ""))
        said += [s for m in material or () for t in _strings(m) for s in _split_sentences(t)]
        parts += [s for s in said if org in s.lower()]
    text = "\n".join(dict.fromkeys(p for p in parts if str(p).strip()))
    tied = [x for x in _listed_skills(profile) if term_present(x, text)]
    if tied:
        text += "\nSkills shown in this role: " + ", ".join(tied)
    return text


def _listed_skills(profile: dict) -> list[str]:
    """Every skill the person listed, once, in their order."""
    lines = (_norm_skills(profile.get("skills")) or {}).values()
    return list(dict.fromkeys(x.strip() for v in lines for x in str(v).split(",") if x.strip()))


def written_problems(new: str, role_text: str, siblings, jd_text: str = "",
                     listed_skills=(), max_chars: int = 0) -> list[str]:
    """Why a bullet WRITTEN for one role may not go on the page; empty means it may.

    The fact gate (tailoring/fact_gate.py) scoped to that role's own material: no figure, name,
    acronym, tool or credential the role's material does not state, no skill the person listed
    that this role never shows, no filler, no keyword list, and nothing a sibling bullet
    already says (tailoring/filler.py: one fact, one bullet)."""
    from .fact_gate import figures, known_material, new_claims, source_figures
    from .filler import (_FACT_STOP, _fact_tokens, filler_added, repeats_sibling, same_fact,
                         tacked_on_terms)
    from .keywords import introduced_skills, term_present

    new = str(new or "").strip()
    if not new:
        return ["empty"]
    bad = sorted(figures(new) - source_figures(role_text), key=lambda s: (len(s), s))
    bad += new_claims(new, known=known_material(role_text))
    bad += introduced_skills("", new, role_text, jd_text)
    bad += [s for s in listed_skills or ()
            if term_present(s, new) and not term_present(s, role_text)]
    bad += filler_added(role_text, new) + tacked_on_terms(role_text, new)
    sibs = [str(s) for s in siblings or () if str(s).strip()]
    if repeats_sibling("", new, sibs) or any(same_fact(new, s) for s in sibs):
        bad.append("repeats a bullet of this role")
    else:
        # Says nothing new: most of its content words are already on the role's other bullets.
        # A real build (2026-11-06) wrote "Gathered enterprise CRM requirements ... process
        # improvement ... two roles" under a bullet that already said exactly that.
        def stems(text):
            return [t.rstrip("s") for t in _fact_tokens(text) if t not in _FACT_STOP]
        said = {t for x in sibs for t in stems(x)}
        mine = stems(new)
        if mine and sum(t in said for t in mine) / len(mine) >= WRITTEN_SAID_SHARE:
            bad.append("restates the role's other bullets")
    if max_chars and len(new) > max_chars:
        bad.append("too long")
    return list(dict.fromkeys(bad))


# A written bullet whose content words are this share or more already on the role's other
# bullets restates them, and is refused (``written_problems``).
WRITTEN_SAID_SHARE = 0.5
# Two parts of a split may not share this many words in a row.
SPLIT_SHARED_RUN = 5


def _looks_merged(text: str) -> bool:
    """A bullet that may hold several accomplishments (worth asking to split)."""
    t = str(text or "")
    return len(t) >= 80 and (";" in t or t.count(",") >= 2 or ", and " in t
                             or (len(t) >= 120 and " and " in t))


def _split_call(llm, title: str, src: str, jd_text: str, room: int) -> list:
    """One bullet split for the recency fill: ``llm.split_bullet`` into exactly two parts (one
    more bullet, the least room it takes), or, for a backend without it, ``llm.expand_bullets``
    (the caller drops a split with more parts than ``room``)."""
    if callable(getattr(llm, "split_bullet", None)):
        return llm.split_bullet(title, src, 2, jd_text)
    return llm.expand_bullets(title, [src], jd_text)


def _split_ok(parts: list, src: str, role_text: str, siblings, jd_text: str) -> bool:
    """Every part of a split restates only its source bullet: no new figure, name or skill,
    no filler, no repeat of another bullet or of another part."""
    from .fact_gate import figures, known_material, new_claims
    from .filler import _fact_tokens, _longest_shared_run, filler_added, shares_a_clause
    from .keywords import introduced_skills

    known = known_material(src, role_text)
    for n, part in enumerate(parts):
        if (figures(part) - figures(src) or new_claims(part, known=known)
                or introduced_skills(src, part, role_text, jd_text) or filler_added(src, part)
                or len(part) > _bullet_budget(src)):
            return False
        if any(shares_a_clause(part, str(s)) for s in siblings or ()):
            return False
        # Parts of one sentence each say their own thing: a phrase repeated across parts
        # ("growth, usage, and unit costs" three times, a real split on 2026-11-06) reads as
        # padding on the page.
        if any(_longest_shared_run(_fact_tokens(part), _fact_tokens(other)) >= SPLIT_SHARED_RUN
               for other in parts[n + 1:]):
            return False
    return True


def _recency_fill(preamble: str, used: dict, result, *, sections, headings, workdir: Path,
                  jobname: str, llm, jd_text: str, tailor: bool, write: bool, source: dict,
                  material=(), gate_material=(), origins: dict | None = None,
                  reverted: list | None = None, trims: list | None = None, keep=()):
    """Give the most recent roles their bullets, then fill a short page with more, re-measuring
    the real page after every bullet. Returns (result, added, notes).

    Recency comes first (Kofi, 2026-11-06). The two most recent roles (and a role the person
    leads with) reach their fewest bullets (three) even on a page that is already full: when a
    bullet does not fit, room is made from OLDER material, one cut at a time and re-measured
    after each (``_make_room``), stopping as soon as the page is one clean page again. If even
    that cannot fit it, the bullet and the cuts are undone (the best clean page stays) and a
    note says so. Every cut is recorded in ``trims`` (Put back), and a cut the person put back
    (``keep``) is never made again.

    Then, while the page is short of ``FILL_GOAL``, two passes over the roles in recency order
    (``ranked_roles``): every older role up to its fewest bullets, then every role up to its
    most, so the most recent role is brought toward five first, then the second toward four,
    then older ones toward three. This part never cuts anything: it stops when the page
    reaches ``FILL_GOAL`` or the next bullet would overflow (two pages, a new overfull line, or
    past ``MAX_FILL``); that bullet is taken back off.

    Each bullet comes from, in order: (a) the person's own bullet for that role that selection
    left out (``_reserve``), put back in the author's order and, with ``tailor``, reworded
    toward the job like any other; (b) a split of one of the role's bullets that merges several
    accomplishments (``llm.split_bullet``, each part checked against its source bullet);
    (c) a NEW bullet written from that role's own material only (``llm.write_role_bullet``),
    refused by ``written_problems`` if it states anything the material does not or repeats a
    sibling. (b) and (c) need ``write`` and a model (a fresh build, never a rebuild after the
    person's own edit). A role whose (c) is refused gets no more written bullets in this build.
    """
    from .fact_gate import known_material, source_figures
    from .filler import same_fact
    from .keywords import profile_text, term_present

    added: list = []
    notes: list = []
    origins = origins if origins is not None else {}
    if not (result and result.ok and result.pages == 1 and result.fill_ratio is not None):
        return result, added, notes
    reword = bool(tailor and llm is not None)
    use_llm = bool(write and llm is not None)
    can_write = use_llm and callable(getattr(llm, "write_role_bullet", None))
    supported = supported_skills(jd_text, source) if ((reword or use_llm) and jd_text) else []
    known = known_material(*gate_material) if reword else None
    known_figs = source_figures(*gate_material) if reword else None
    source_roles = {_role_key(e, r): r for e, r in _experience_roles(source)}
    listed = _listed_skills(source)
    base_overfull = result.overfull_count
    state: dict = {}

    def clean(r) -> bool:
        return bool(r and r.ok and r.pages == 1 and r.overfull_count <= base_overfull
                    and r.fill_ratio is not None and r.fill_ratio <= MAX_FILL)

    def next_step(entry: dict, role: dict, most: int):
        # ``most`` caps how many bullets the role may reach with this step: a split that would
        # carry it past the bullet the caller asked for is not taken.
        # A role whose bullets were drafted as placeholders (not the person's words) is never
        # split or written from: only the person's own material may seed a new bullet.
        own = not (role.get("drafted_bullets") or entry.get("drafted_bullets"))
        st = state.setdefault(id(role), {"tried": set(), "write": can_write and own, "dry": False})
        bl = role["bullets"]
        label = entry_label(entry, role)
        rec = {"section": "experience", "label": label}
        src_role = source_roles.get(_role_key(entry, role))
        reserve = role.get("_reserve") or []
        while reserve:                                         # (a) their own, left out
            src = str(reserve.pop(0))
            if any(same_fact(src, str(origins.get(b, b))) for b in bl):
                continue
            text = src
            if reword:
                text = _tailor_one(src, list(bl), profile_text(entry), jd_text, llm,
                                   supported=supported, known=known, known_figs=known_figs,
                                   section="experience", label=label, reverted=reverted,
                                   origins=origins)
            # Back in the author's order, so the page reads as they wrote it and a rebuild
            # does not shuffle it.
            order = [_norm_text(x) for x in (src_role or {}).get("bullets") or []]
            at = order.index(_norm_text(src)) if _norm_text(src) in order else len(order)
            pos = sum(1 for b in bl if _norm_text(origins.get(b, b)) in order
                      and order.index(_norm_text(origins.get(b, b))) < at)
            return bl[:pos] + [text] + bl[pos:], [dict(rec, bullet=text, kind="saved", source=src)]
        role_text = _role_material(entry, role, src_role, source, material)
        room = most - len(bl) + 1                             # parts one bullet may become
        if use_llm and own and room >= 2:                      # (b) split a merged bullet
            for i, b in enumerate(bl):
                src = str(origins.get(b, b))
                if src in st["tried"] or not _looks_merged(src):
                    continue
                st["tried"].add(src)
                try:
                    parts = _split_call(llm, str(role.get("title") or ""), src, jd_text, room)
                except Exception:                              # noqa: BLE001 - a split is optional
                    continue
                parts = [str(x).strip() for x in parts or [] if str(x).strip()]
                if len(parts) < 2 or len(bl) - 1 + len(parts) > most:
                    continue
                if not _split_ok(parts, src, role_text, [x for k, x in enumerate(bl) if k != i],
                                 jd_text):
                    continue
                for x in parts:
                    origins[x] = src
                return (bl[:i] + parts + bl[i + 1:],
                        [dict(rec, bullet=x, kind="split", source=src) for x in parts])
        if st["write"]:                                        # (c) written from the role
            budget = _bullet_budget(max(map(str, bl), key=len) if bl else "")
            terms = [t for t in supported if term_present(t, role_text)]
            try:
                new = str(llm.write_role_bullet(label, role_text, list(bl), terms, budget,
                                                jd_text=jd_text) or "").strip()
            except Exception:                                  # noqa: BLE001
                new = ""
            if new.strip(". ").upper() == "NONE":
                new = ""
            if new and not written_problems(new, role_text, bl, jd_text, listed, budget):
                origins[new] = new                             # traced through the line fit
                return bl + [new], [dict(rec, bullet=new, kind="written", source="")]
            st["write"] = False
        st["dry"] = True
        return None

    order = sorted(ranked_roles(used), key=lambda x: x[0])
    cuts = _Cuts(trims, keep, origins)

    def lead(entry: dict, role: dict) -> bool:
        return bool(role.get("_lead") or entry.get("_lead"))

    def measure():
        _say("fill", "Filling the page", "with more of your most recent work")
        return compile_tex(render_cv(preamble, used, sections, probe=True, headings=headings),
                           workdir, jobname=f"{jobname}-fill")

    # 1. The two most recent roles reach their fewest bullets, making room from older material.
    blocked: set = set()
    for rank, entry, role in order:
        if rank >= len(RECENCY_BUDGETS) and not lead(entry, role):
            continue
        fewest, most = bullet_budget(rank, lead(entry, role))
        role.setdefault("bullets", [])
        while len(role["bullets"]) < fewest:
            step = next_step(entry, role, fewest)
            if step is None:
                break
            before = list(role["bullets"])
            role["bullets"][:] = step[0]
            undo: list = []
            r = measure()
            while not clean(r):
                u = _make_room(used, cuts, order)
                if u is None:
                    break
                undo.append(u)
                r = measure()
            if not clean(r):                   # no room even after every allowed cut
                role["bullets"][:] = before
                for u in reversed(undo):
                    u()
                blocked.add(id(role))
                n = len(role["bullets"])
                notes.append(f"{entry_label(entry, role)} shows {n} bullet{'' if n == 1 else 's'}: "
                             f"a {'third' if n == 2 else 'further'} one would not fit on one page, "
                             "even after trimming older material.")
                break
            result = r
            added += step[1]
        if blocked:
            break

    # 2. A short page gets more, most recent first, without cutting anything.
    stop = bool(blocked) or result.fill_ratio >= FILL_GOAL
    for phase in (0, 1):                     # 0: up to the fewest, 1: up to the most
        for rank, entry, role in order:
            if id(role) in blocked:
                continue
            fewest, most = bullet_budget(rank, lead(entry, role))
            role.setdefault("bullets", [])
            while not stop and len(role["bullets"]) < (fewest if phase == 0 else most):
                step = next_step(entry, role, fewest if phase == 0 else most)
                if step is None:
                    break
                before = list(role["bullets"])
                role["bullets"][:] = step[0]
                r = measure()
                if not clean(r):
                    role["bullets"][:] = before        # this bullet would overflow: stop here
                    stop = True
                    break
                result = r
                added += step[1]
                stop = r.fill_ratio >= FILL_GOAL
            if stop:
                break
        if stop:
            break
    for rank, entry, role in order:
        fewest, _most = bullet_budget(rank, lead(entry, role))
        n = len(role.get("bullets") or [])
        drafted = role.get("drafted_bullets") or entry.get("drafted_bullets")
        if n < fewest and not drafted and state.get(id(role), {}).get("dry"):
            notes.append(f"{entry_label(entry, role)} shows {n} bullet{'' if n == 1 else 's'}: "
                         "there is no more of your material for that role yet. Tell me more "
                         "about it and I will add it.")
    return result, added, notes


def _make_room(profile: dict, cuts: "_Cuts", order: list):
    """Make room for the most recent roles' fewest bullets with ONE cut of older material, in
    the order Kofi set (2026-11-06): (1) an older role's bullets beyond two, oldest role first;
    (2) a project's bullets beyond one, last project first; (3) the oldest role down to one
    bullet; (4) a skills line beyond the template's minimum (``SKILL_LINES_FLOOR``). Never a
    whole role, never the summary, never type or margins. The cut is recorded in ``cuts`` (Put
    back) and a cut the person put back is skipped. Returns a function that undoes the cut, or
    None when nothing is left to cut."""
    trims = cuts.trims

    def forget(rec):
        if trims is not None and rec in trims:
            trims.remove(rec)

    def pop(bl: list, section: str, label: str, keep_n: int):
        idx = next((i for i in range(len(bl) - 1, keep_n - 1, -1)
                    if not cuts.kept("bullet", label, str(bl[i]))), None)
        if idx is None:
            return None
        text = bl.pop(idx)
        cuts.add("bullet", section, label, str(text), index=idx)
        rec = trims[-1] if trims else None

        def undo():
            bl.insert(idx, text)
            forget(rec)
        return undo

    older = sorted((x for x in order if x[0] >= len(RECENCY_BUDGETS)
                    and not (x[2].get("_lead") or x[1].get("_lead"))), key=lambda x: -x[0])
    for _rank, e, r in older:                                         # (1)
        u = pop(r["bullets"], "experience", entry_label(e, r), OLDER_BUDGET[0])
        if u:
            return u
    for p in reversed(profile.get("projects") or []):                # (2)
        if isinstance(p, dict) and isinstance(p.get("bullets"), list):
            u = pop(p["bullets"], "projects", entry_label(p), 1)
            if u:
                return u
    if older:                                                         # (3)
        _rank, e, r = older[0]
        u = pop(r["bullets"], "experience", entry_label(e, r), 1)
        if u:
            return u
    skills = profile.get("skills")                                    # (4)
    if isinstance(skills, dict) and len(skills) > SKILL_LINES_FLOOR:
        last = next((k for k in reversed(list(skills))
                     if not cuts.kept("skills_line", str(k), str(k))), None)
        if last is not None:
            before = dict(skills)
            cuts.add("skills_line", "skills", str(last), str(last), value=skills[last])
            rec = trims[-1] if trims else None
            del skills[last]

            def undo():
                skills.clear()
                skills.update(before)
                forget(rec)
            return undo
    return None


def _resolve_added(used: dict, added: list, origins: dict) -> list:
    """Each added bullet as it reads on the final page (the line fit may have resized it,
    and ``origins`` traces every rewrite back), dropping any that is no longer there."""
    page: dict = {}
    for _rank, e, r in ranked_roles(used):
        page.setdefault(entry_label(e, r), []).extend(str(b) for b in r.get("bullets") or [])
    taken: set = set()
    out = []
    for rec in added:
        want = {rec.get("bullet"), rec.get("source")} - {"", None}
        for n, b in enumerate(page.get(rec.get("label"), [])):
            key = (rec.get("label"), n)
            if key not in taken and (b in want or origins.get(b) in want):
                taken.add(key)
                out.append(dict(rec, bullet=b))
                break
    return out


def _fit_to_page(preamble, used: dict, sections, workdir: Path, jobname: str,
                 max_shrink: int, headings=None, trims: list | None = None, keep=None,
                 origins: dict | None = None) -> tuple[int, "CompileResult | None"]:
    """Render, compile and shrink ``used`` in place until the page is clean (one page, no
    overfull line, not flush to the bottom edge). Returns (attempts, last compile result).
    Every cut is appended to ``trims``; ids in ``keep`` are never cut (``_shrink_once``)."""
    attempts = 0
    result: CompileResult | None = None
    said_over = False
    for attempts in range(1, max_shrink + 2):
        # The first compile is "fit"; when it comes out over a page that same line turns into
        # the retry, and the shortening rounds after it are one "fit2" line.
        _say("fit" if attempts == 1 else "fit2", "Fitting the page", "so every line stays inside the margins",
             detail=f"{attempts} compiles" if attempts > 1 else "")
        tex = render_cv(preamble, used, sections, probe=True, headings=headings)
        result = compile_tex(tex, workdir, jobname=jobname)
        # A fill past MAX_FILL is a real overflow even at "1 page": with no bottom
        # margin the content is flush to (or off) the paper edge, so keep shrinking.
        over_bottom = bool(result.fill_ratio and result.fill_ratio > MAX_FILL)
        clean = (result.ok and result.pages == 1
                 and result.overfull_count == 0 and not over_bottom)
        if clean:
            break
        # Keep shrinking while it's over one page; a lone overfull hbox on an
        # otherwise one-page CV is acceptable to ship.
        if result.ok and result.pages == 1 and attempts > 1 and not over_bottom:
            break
        if not said_over:
            said_over = True
            _say("fit", "Shortening", "the longest bullets, it ran over one page", state="retry")
        if not _shrink_once(used, trims, keep, origins):
            break
    return attempts, result


def assemble_cv(
    template_source: str,
    profile: dict,
    jd_text: str,
    llm: LLMBackend,
    workdir: str | Path,
    max_shrink: int = 60,
    jobname: str = "cv",
    tailor: bool = True,
    sections=None,
    headings: dict | None = None,
    choices: dict | None = None,
    keep=None,
    material: tuple | list = (),
    fit_lines: bool = False,
    skill_pool=(),
    fit_rounds: int | None = None,
    fill_writes: bool | None = None,
) -> AssembleResult:
    """Assemble a one-page CV from ``profile`` in the template's shape.

    Reuses the template's preamble verbatim, fills slots from the profile, tailors
    bullet language to the JD (truthful to the profile), and shrinks content until
    it compiles to a single page. ``sections`` (from the template's manifest) drives
    which sections render and in what order — e.g. summary-first with no extracurricular.
    ``choices`` are the person's choices for this kind of job (``select_roles_for_jd``); what
    they changed is in ``choices_report``. ``keep`` holds the ids of cuts the person put back
    (``trims``), never cut again; ``material`` is more of the person's own words (brain facts)
    the fact gate may trace a rewrite to.

    ``fit_lines`` runs the full-width line loop (tailoring/linefit.py) on the finished page:
    5 to 6 one-line Skills categories that reach the right margin, and every bullet's last
    line close to the edge, measured on the PDF, never worse than before. ``skill_pool`` is
    more of the person's own skills it may place (the caller vets them; the fact gate checks
    them again). Its rounds show in the live activity feed (ui/activity.py) as "Fitting the
    page", a rejected round as a retry.

    The page always keeps the template's own type size, margins and spacing. A page still
    short after the fit gets more bullets, the most recent roles first (``_recency_fill``):
    the person's own bullets first, then, when ``fill_writes`` (default: ``tailor``) and a
    model are given, splits of merged bullets and bullets written from each role's own
    material, listed in ``added`` (``written``) so the person can check them.
    """

    workdir = Path(workdir)
    preamble = extract_preamble(template_source)
    full_profile = normalize_profile(profile)  # tolerate loose LLM-drafted shapes
    _dedupe_entry_bullets(full_profile)      # one fact, one bullet, per entry
    source_material = full_profile           # every figure the person actually supplied
    # Pick the roles this JD actually cares about BEFORE tailoring. A full career on one
    # page is not a resume, it is an index: every role gets squeezed to a single line and
    # the strongest evidence ($60M exports, a 250-driver fleet) is the first thing cut.
    # Selecting first means the roles that survive get room to argue.
    report: dict = {}
    _say("match", "Matching your experience", "to the roles this job cares about")
    profile = select_roles_for_jd(full_profile, jd_text, choices=choices, report=report)
    # What the person's choices left out never comes back through the grow pass below.
    full_profile = choice_pool(full_profile, report)
    import copy as _copy
    selected_source = _copy.deepcopy(profile)

    # Escaping happens once, at render time (`_esc`); the LLM returns plain prose.
    reverted: list = []
    origins: dict = {}
    gate_material = (source_material,) + tuple(material or ())
    if tailor:
        _say("bullets", "Crafting bullets", "in the job's own words")
    used = (_tailor_bullets(profile, jd_text, llm, material=gate_material, reverted=reverted,
                            origins=origins) if tailor else profile)

    trims: list = []
    attempts, result = _fit_to_page(preamble, used, sections, workdir, jobname, max_shrink,
                                    headings=headings, trims=trims, keep=keep, origins=origins)

    # THE GROW PASS. Selection keeps a career to 4 roles so a long one is not squeezed into
    # an index. For a THIN profile that is backwards: Kofi's own CV (5 roles, 9 bullets)
    # rendered at about half a page with a role cut while the bottom half sat empty
    # (2026-10-07). When the page comes out short and selection left roles out, rebuild with
    # every role the person gave (each at its recency budget) and keep whichever version fills
    # the page better without overflowing. Only their own material comes back.
    if (result and result.ok and result.pages == 1 and result.fill_ratio is not None
            and result.fill_ratio < TARGET_FILL and _dropped_roles(full_profile, profile)):
        _say("grow", "Trying again", "with more of your roles, the page came out short", state="retry")
        g_reverted: list = []
        g_origins: dict = {}
        g_source = select_roles_for_jd(full_profile, jd_text, max_roles=10 ** 6, choices=choices)
        if tailor:
            _say("bullets", "Crafting bullets", "for the roles that came back")
        grown = (_tailor_bullets(g_source, jd_text, llm, material=gate_material,
                                 reverted=g_reverted, origins=g_origins)
                 if tailor else _copy.deepcopy(g_source))
        g_trims: list = []
        g_attempts, g_result = _fit_to_page(preamble, grown, sections, workdir,
                                            jobname + "-full", max_shrink, headings=headings,
                                            trims=g_trims, keep=keep, origins=g_origins)
        if (g_result and g_result.ok and g_result.pages == 1
                and (g_result.fill_ratio or 0) > result.fill_ratio
                and (g_result.fill_ratio or 0) <= MAX_FILL):
            used, result, attempts = grown, g_result, attempts + g_attempts
            selected_source = _copy.deepcopy(g_source)
            trims, reverted, origins = g_trims, g_reverted, g_origins

    # THE RECENCY FILL. The page keeps the template's own type size, margins and spacing; a page
    # still short of FILL_GOAL gets more bullets, the most recent roles first (3 to 5 on the
    # latest, 3 to 4 on the second, 2 to 3 on older ones), re-measured after every bullet.
    # ``attempts`` stays the one-page fit's count: a caller reads attempts > 1 as "the builder
    # had to cut", which a fill never does.
    added: list = []
    bullet_notes: list = []
    if result and result.ok and result.pages == 1:
        result, added, bullet_notes = _recency_fill(
            preamble, used, result, sections=sections, headings=headings, workdir=workdir,
            jobname=jobname, llm=llm, jd_text=jd_text, tailor=tailor,
            write=tailor if fill_writes is None else fill_writes, source=source_material,
            material=material, gate_material=gate_material, origins=origins, reverted=reverted,
            trims=trims, keep=keep)
        # The review's "before" side gets the person's own bullets the fill brought back.
        keys = {entry_label(e, r): _role_key(e, r) for _rank, e, r in ranked_roles(used)}
        sources = {_role_key(e, r): r for e, r in _experience_roles(selected_source)}
        for rec in added:
            src_role = sources.get(keys.get(rec["label"]))
            if rec.get("kind") == "saved" and src_role is not None:
                src_role.setdefault("bullets", []).append(rec["source"])
    _say("compile", "Compiling", "the PDF")

    # FULL-WIDTH LINES (tailoring/linefit.py). Measured on the real page: Skills packed into 5
    # to 6 one-line categories that reach the right margin, each bullet's last line filled or a
    # one-word spill trimmed. Each round must keep the page clean and score better, so the
    # result is never worse than the page above.
    line_fit = line_before = line_gaps = pre_fit = None
    if fit_lines and result and result.ok and result.pages == 1:
        pre_fit = _copy.deepcopy(used)
        from . import linefit as _lf
        from .fact_gate import known_material, source_figures

        def _clean(r):
            return bool(r and r.ok and r.pages == 1 and r.overfull_count == 0
                        and not (r.fill_ratio and r.fill_ratio > MAX_FILL))
        fo = _lf.fit_lines(
            used, result,
            render=lambda p: render_cv(preamble, p, sections, probe=True, headings=headings),
            compile_fn=lambda tex, name: compile_tex(tex, workdir, jobname=name),
            is_clean=_clean, llm=llm, jd_text=jd_text, skill_pool=skill_pool,
            # A written bullet stays exactly as the gate passed it and the person reviews it.
            frozen={a["bullet"] for a in added if a.get("kind") == "written"},
            known=known_material(*gate_material), known_figures=source_figures(*gate_material),
            origins=origins, rounds=_lf.FIT_ROUNDS if fit_rounds is None else fit_rounds,
            fit_bullets=llm is not None, jobname=jobname)
        if fo.result is not result:
            used, result = fo.profile, fo.result
        # A fit rewrite the guards refused never reached the page, so there is nothing for
        # the person to confirm: the bullet simply kept its wording.
        line_fit, line_before, line_gaps = fo.after.as_dict(), fo.before.as_dict(), fo.gaps()

    # The grow and fill passes compile under side jobnames (-full, -fill). Whatever won must also
    # exist under the REAL jobname, because that is the file the app serves (/api/cv.pdf?job=...);
    # without this the chat reported the grown page's fill while the viewer showed the first,
    # small compile (seen 2026-10-07).
    if result and result.pdf_path and Path(result.pdf_path).stem != jobname:
        result = compile_tex(render_cv(preamble, used, sections, probe=True, headings=headings),
                             workdir, jobname=jobname)

    # Ship without the probe: it has no layout effect, so the measurement above still
    # describes this render exactly (see _FILL_PROBE).
    added = _resolve_added(used, added, origins)
    strip_choice_marks(used)
    strip_choice_marks(selected_source)
    tex = render_cv(preamble, used, sections, headings=headings)
    cv_text = _rendered_cv_text(used, sections)
    if line_fit:
        _say("check", "Checking every line",
             f"{line_fit['skills_full']} of {line_fit['skills_lines']} skills lines full, "
             f"{line_fit['bullets_ok']} of {line_fit['bullet_count']} bullets reach the edge")
    else:
        _say("check", "Checking every line", "and the job's terms")
    # One model read of the ad gives the skill list the report is measured against; the curated
    # vocabulary stands in when there is no model (llm is None in a few offline callers).
    try:
        jd_skills = llm.extract_jd_skills(jd_text) if (llm is not None and jd_text) else None
    except Exception:                                    # noqa: BLE001 - the report must not fail the build
        jd_skills = None
    coverage = build_coverage_report(jd_text, cv_text, used, terms=jd_skills)
    one_page = bool(result and result.ok and result.pages == 1)
    # Fill is only meaningful on a one-page compile (\pagetotal reports the LAST page).
    fill = result.fill_ratio if (result and one_page) else None
    underfull = fill is not None and fill < TARGET_FILL

    return AssembleResult(
        ok=one_page,
        # "Fits" was never the same as "done". A half page compiles cleanly and is still
        # the thing we promised never to ship, so it gets its own status rather than
        # being waved through as clean.
        status=("overflow" if not one_page
                else "underfull" if underfull
                else "clean"),
        tex_source=tex,
        pdf_path=result.pdf_path if result else None,
        coverage=coverage,
        compile=result,
        attempts=attempts,
        profile_used=used,
        empty_template_source=template_source,
        fill_ratio=fill,
        # Rewrites the gate refused come first (already reverted, so nothing to strike), then
        # any figure still on the page with no origin (the person's own edits are material).
        fact_flags=(list(reverted) + flag_new_figures(used, source_material, *(material or ()))
                    if tailor else []),
        source_profile=selected_source,
        choices_report=report,
        trims=trims,
        reverted=reverted,
        line_fit=line_fit,
        line_fit_before=line_before,
        line_gaps=line_gaps,
        pre_fit_profile=pre_fit,
        added=added,
        bullet_notes=bullet_notes,
    )


def _rendered_cv_text(profile: dict, sections=None) -> str:
    """Flatten ONLY the profile content that actually renders into the CV, for the coverage
    report. The 'present' column must reflect what is on the page, not a term sitting in a
    section this template omits (e.g. a skill only in Extracurricular under a summary-first
    template that doesn't render it). Section names ARE the profile keys; the header always
    renders, so identity is always included."""
    from .keywords import profile_text

    order = list(sections) if sections else list(_DEFAULT_SECTION_ORDER)
    visible = {"identity": profile.get("identity", {})}
    for name in order:
        if name in _SECTION_RENDERERS and name in profile:
            visible[name] = profile[name]
    return profile_text(visible)
