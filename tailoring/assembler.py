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

import inspect
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
SKILL_LINES_FLOOR = 4
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
    # product) for people who have one instead of (or besides) a blog. LinkedIn shows as the
    # linked word "In", beside "GitHub" ("... | In | GitHub"), the style Kofi confirmed from his
    # own resume (2026-11-10); hyperref colours both blue.
    for label, key in (("In", "linkedin"), ("GitHub", "github"),
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


# A project's right-hand slot never prints a bare "Personal Project": every project on the page is
# one, so it says nothing, and (before 2026-11-08) a project stored with only a ``title`` printed it
# on its own line ABOVE the name. Kofi's own resume is the reference: ONE line per project, the name
# (with the link arrow) on the left and its tools on the right, then the bullets.
_GENERIC_PROJECT_PLACES = frozenset({"personal project", "personal projects", "personal",
                                     "side project", "project", "projects", "personal work"})
_TRAILING_TOOLS = re.compile(r"^(?P<name>.*\S)\s*\((?P<tools>[^()]{2,120})\)\s*$")
_DATEY = re.compile(r"^\s*(?:(?:19|20)\d\d|present|current|ongoing|jan|feb|mar|apr|may|jun|jul|aug|"
                    r"sep|oct|nov|dec|spring|summer|fall|autumn|winter)\b", re.I)


def project_row(e: dict) -> dict:
    """How one project prints: {"name", "right", "role", "dates"}. The name is ``org`` or
    ``name``, else a title-only entry's ``title`` (then it is not a role line). Tools in brackets
    at the end of the name ("Gate Check (JavaScript, OCR, Firebase)") move to the right slot. The
    right slot is the tools, else a real place (never a generic "Personal Project"), else the
    dates. A role line prints only for a real role title."""
    name = str(e.get("org") or e.get("name") or "").strip()
    role = str(e.get("title") or "").strip()
    if not name:
        name, role = role, ""
    tools = str(e.get("tools") or e.get("tech") or "").strip()
    m = _TRAILING_TOOLS.match(name)
    if m and not _DATEY.match(m.group("tools")):
        name = m.group("name").strip()
        tools = tools or m.group("tools").strip()
    place = str(e.get("location") or "").strip()
    if place.lower() in _GENERIC_PROJECT_PLACES:
        place = ""
    dates = str(e.get("dates") or "").strip()
    right = tools or place
    if dates and not role:
        right = f"{right} | {dates}" if right else dates
        dates = ""
    return {"name": name, "right": right, "role": role, "dates": dates}


def _project_blocks(entries: list[dict], bold: bool = False) -> str:
    """Each project as ONE heading line (name and link arrow left, tools right), then its
    bullets; an entry with a real role title keeps that title's own line under it."""
    out: list[str] = []
    for e in entries:
        row = project_row(e)
        url = str(e.get("link") or e.get("url") or "").strip()
        # Minimal blue: a small linked arrow after the (black) name, never the name itself.
        link_marker = f"~\\href{{{_link_url(url)}}}{{$\\nearrow$}}" if url else ""
        # A flagged placeholder is marked in the .tex too, so even a preview PDF
        # cannot present an invented project as real.
        suggested = " \\textit{[SUGGESTED: replace before finalizing]}" if e.get("suggested") else ""
        right = f" \\hfill \\textbf{{{_esc(row['right'])}}}" if row["right"] else ""
        out.append(f"\n\\noindent \\textbf{{\\textsc{{{_esc(row['name'])}}}}}{link_marker}{suggested}{right}\n")
        roles = e.get("roles") if isinstance(e.get("roles"), list) and e.get("roles") else None
        if roles is None:
            if row["role"] or row["dates"]:
                out.append(f"\n\\noindent \\textbf{{{_esc(row['role'])}}} \\hfill \\textbf{{{_esc(row['dates'])}}}\n")
            out.append(_bullets(e.get("bullets", []), bold=bold))
            continue
        for r in roles:
            title, dates = _esc(r.get("title", "")), _esc(r.get("dates", ""))
            if title or dates:
                out.append(f"\n\\noindent \\textbf{{{title}}} \\hfill \\textbf{{{dates}}}\n")
            out.append(_bullets(r.get("bullets", []), bold=bold))
    return "".join(out)


def _projects(profile: dict) -> str:
    entries = profile.get("projects", [])
    if not entries:
        return ""
    return (_heading(_label("projects", "Projects"))
            + _project_blocks(entries, bold=bool(profile.get("bold_metrics")))
            + "\\vs\n")


def _experience(profile: dict) -> str:
    entries = profile.get("experience", [])
    if not entries:
        return ""
    return (_heading(_label("experience", "Experience"))
            + _entry_blocks(entries, bold=bool(profile.get("bold_metrics")))
            + "\\vs\n")


def _real_entry(it: dict) -> bool:
    """An Extracurricular ENTRY (a title with bullets or an organisation), as against a bare
    word ("Swimming") that reads as a grid cell."""
    title = str(it.get("title") or "").strip()
    bl = [b for b in (it.get("bullets") or []) if str(b).strip()]
    return bool(title and (bl or str(it.get("org") or it.get("organisation") or "").strip()))


def hobby_titles(profile: dict, entries_too: bool = False) -> list[str]:
    """The Extracurricular items that belong on the Interests line: bare words ("Swimming")
    always; with ``entries_too`` the real entries' titles as well (a template with no
    Extracurricular section of its own folds them into Interests, "Youth team (Accra FC)")."""
    out: list[str] = []
    for it in profile.get("extracurricular", []) or []:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        if _real_entry(it):
            if entries_too:
                org = str(it.get("org") or it.get("organisation") or "").strip()
                out.append(f"{title} ({org})" if org else title)
        else:
            out.append(title)
    return out


def _extracurricular(profile: dict) -> str:
    """The Extracurricular block renders ENTRIES only: a title with bullets or an organisation,
    as the template's title-and-date rows. A bare word ("Playing football", "Swimming") is a
    hobby: it goes to the Interests line (``_additional``), never into this block as a grid of
    words spaced across the line (Kofi, 2026-11-10). Template-independent: the same rule under
    "Leadership & Activities", "Clinical Rotations" or "Extracurricular"."""
    real = [it for it in (profile.get("extracurricular", []) or [])
            if isinstance(it, dict) and _real_entry(it)]
    if not real:
        return ""
    out = [_heading(_label("extracurricular", "Extracurricular")), "\n"]
    for it in real:
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


def interests_line(profile: dict, fold_entries: bool = False) -> str:
    """The one Interests sentence for the page: the Interests on file with the hobby words from
    Extracurricular folded in without duplicates (tailoring/conform.merge_interests), and the
    real entries' titles too when the template has no Extracurricular section
    (``fold_entries``). Empty when the person gave neither."""
    from tailoring.conform import interest_sentence, merge_interests
    interests = profile.get("interests")
    if isinstance(interests, list):
        interests = ", ".join(str(x) for x in interests)
    interests = str(interests or "").strip()
    extra = hobby_titles(profile, entries_too=fold_entries)
    if extra:
        interests = interest_sentence(extra) if not interests else merge_interests(interests, extra)[0]
    return compact_interests(interests)


def compact_interests(line: str, label: str = "Interests") -> str:
    """The Interests sentence as the page prints it: when it would run a few words onto a
    second line ("CEO (DOAC)." alone, Kofi's page 2026-11-10), the items are compacted, in
    steps, until it fits one line or fills most of the second: a leading gerund goes
    ("Playing football" to "football", "watching podcasts" to "podcasts"), then a purpose
    tail ("travelling to see the world" to "travelling"). The saved text is never changed;
    only what prints."""
    from .textwidth import TEXTWIDTH_PT, text_width_pt
    line = str(line or "").strip()
    if not line:
        return ""
    avail = TEXTWIDTH_PT - 2.0

    def width(s: str) -> float:
        return text_width_pt(f"{label}:", bold=True) + text_width_pt(" " + s)

    def fine(s: str) -> bool:
        w = width(s)
        return w <= avail or (w % avail) >= 0.4 * avail

    if fine(line):
        return line
    steps = (
        lambda s: re.sub(r"(?i)(^|, |,? and )(playing|watching|doing|going|reading|listening to|"
                         r"following)\s+", lambda m: m.group(1), s),
        lambda s: re.sub(r"(?i)\s+to\s+(?:see|explore|visit|watch|learn|meet|stay)\b[^,.]*", "", s),
    )
    for step in steps:
        new = step(line).strip()
        new = (new[0].upper() + new[1:]) if new else new
        if new != line:
            line = new
            if fine(line):
                return line
    return line


def _additional(profile: dict, fold_entries: bool = False) -> str:
    """Additional Information: ONE prose Interests line, the single home for hobbies on every
    template (Kofi, 2026-11-10: "I don't think we need both Extracurricular and Additional Info
    on one resume"). A template with no Extracurricular section (``fold_entries``) carries the
    person's activities in it too."""
    line = interests_line(profile, fold_entries)
    if not line:
        return ""
    return (
        _heading(_label("additional", "Additional Information"))
        + f"\n\\noindent \\textbf{{{_label('interests', 'Interests')}:}} {_esc(line)}\n"
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
        fold = "extracurricular" not in order
        for name in order:
            if name == "interests":
                # One outside-work section: a template without an Extracurricular section
                # of its own carries the person's activities inside Additional Information.
                body.append(_additional(profile, fold_entries=fold))
                continue
            renderer = _SECTION_RENDERERS.get(name)
            if renderer:
                body.append(renderer(profile))
        # A template with an activities block but no Interests section of its own still prints
        # the person's hobbies (the words that never render under "Clinical Rotations" or
        # "Activities & Honors"), as the one Interests line at the end. A template with neither
        # section stays as it is.
        if "interests" not in order and "extracurricular" in order and interests_line(profile):
            body.append(_additional(profile))
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
    from .filler import repeats_sibling, role_padding
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
    # (1c) No padding that states no fact (Kofi, 2026-11-10): the role's own employer name, a
    # stock tail ("for stakeholders across the business"), a phrase another bullet of the role
    # says. What the person's own bullet already says stays.
    employer = label.split(", ")[0] if section == "experience" else ""
    if role_padding(new, siblings, employer, original):
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


def _cut_victim(bl: list, lo: int, cuts: "_Cuts", label: str):
    """The index of the bullet a cut takes from ``bl`` (positions ``lo`` and up, latest first):
    a bullet WITHOUT a distinctive figure before one with, so "280,000+ job postings a day ...
    145,000+ sponsorship-matched roles" is never what goes to make room (Kofi, 2026-11-10); a
    bullet the person put back is never cut again. None when nothing may go."""
    from .filler import figures_of
    free = [i for i in range(len(bl) - 1, lo - 1, -1) if not cuts.kept("bullet", label, str(bl[i]))]
    for i in free:
        if not figures_of(str(bl[i])):
            return i
    return free[0] if free else None


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

    # 7. Skills keep 4 to 5 full-width lines (the owner's layout rule, linefit.py): drop a line
    # ONLY above the 4-line floor, so it can go 5 -> 4 but never below 4.
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
        idx = _cut_victim(bl, 0, cuts, label)
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
# The staircase (``_recency_fill``): the most recent role's bullet count starts at least at
# STAIR_FIRST (5, 4, 3, 2 for four roles) and rises no higher than STAIR_TOP.
STAIR_FIRST = 3
STAIR_TOP = 8
# The top role stays at this many while a role below it can still take a bullet; it goes past
# only when every role stands one step below the one above and the page is still short.
STAIR_SOFT_TOP = 6
# The oldest role on the page keeps at least this many bullets (Kofi, 2026-11-10: "CSIR must keep
# at least 2"), and the staircase never cuts a role below it.
OLDEST_FLOOR = 2


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
    # Each bullet on the page -> the person's own bullet it came from (a rewrite, a resize, a
    # part of a split); a bullet written for the page maps to itself.
    origins: dict = field(default_factory=dict)

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
    first written), what their uploaded resumes say under that employer (``role_notes``), the
    sentences of their summary and saved answers that name the employer, and the skills they
    listed that this text shows. A written bullet may draw only on this."""
    from .fact_gate import _strings
    from .keywords import term_present

    head = {k: v for k, v in entry.items() if k not in ("roles", "bullets", "_reserve", "_lead")}
    parts = _strings(head) + _strings({k: v for k, v in role.items() if k != "_lead"})
    if source_role:
        parts += _strings(source_role.get("bullets") or [])
    org = str(entry.get("org") or entry.get("name") or entry.get("company") or "").strip().lower()
    # What their uploaded resume says under this employer: often more than the saved bullets
    # ("high-velocity", "matched to government sponsorship records").
    parts += _role_notes_for(material, org)
    if len(org) >= 3:
        said = _split_sentences(str(profile.get("summary") or ""))
        said += [s for m in material or () for t in _strings(m) for s in _split_sentences(t)]
        parts += [s for s in said if org in s.lower()]
    text = "\n".join(dict.fromkeys(p for p in parts if str(p).strip()))
    tied = [x for x in _listed_skills(profile) if term_present(x, text)]
    if tied:
        text += "\nSkills shown in this role: " + ", ".join(tied)
    return text


# A heading line that ends the Experience part of an uploaded resume.
_DOC_HEAD = re.compile(
    r"^(?:education|skills|technical skills|projects|extracurricular|activities|interests|"
    r"additional information|certifications?|licenses|awards|honors|publications|volunteer\w*|"
    r"leadership|references|languages|summary|profile)\b", re.I)


def role_notes(text: str, orgs) -> dict:
    """What an uploaded resume (``text``, as extracted) says under each employer in ``orgs``:
    {employer, lower case: its lines up to the next employer or section heading, the bullets
    joined back from their wrapped lines}. A role's own words from the person's own document,
    so the page fill may split or draw on them (``_role_material``); nothing about another
    employer, and nothing from outside the Experience part."""
    lines = [ln.strip() for ln in str(text or "").splitlines()]
    keys = [(str(o), _norm_text(o)) for o in orgs or () if len(_norm_text(o)) >= 3]
    starts = []
    for i, ln in enumerate(lines):
        low = _norm_text(ln)
        hit = next((o for o, k in keys if low.startswith(k) and len(low) <= len(k) + 40), None)
        if hit is not None:
            starts.append((i, hit))
    out: dict = {}
    for n, (i, org) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        body = []
        for ln in lines[i + 1:end]:
            if _DOC_HEAD.match(ln) and len(ln) <= 40:
                break
            body.append(ln)
        joined = re.sub(r"\s+", " ", " ".join(body)).strip()
        parts = [b.strip() for b in re.split(r"\s*[\u2022\u25cf\u25aa\u2023\u2043]\s*", joined)
                 if b.strip()]
        if parts:
            key = _norm_text(org)
            out[key] = "\n".join(([out[key]] if key in out else []) + parts)
    return out


def _role_notes_for(material, org: str) -> list[str]:
    """The uploaded resumes' notes for employer ``org`` among ``material`` ({"role_notes": ...})."""
    key = _norm_text(org)
    return [str(m["role_notes"][key]) for m in material or ()
            if isinstance(m, dict) and isinstance(m.get("role_notes"), dict)
            and m["role_notes"].get(key)]


def _listed_skills(profile: dict) -> list[str]:
    """Every skill the person listed, once, in their order."""
    lines = (_norm_skills(profile.get("skills")) or {}).values()
    return list(dict.fromkeys(x.strip() for v in lines for x in str(v).split(",") if x.strip()))


def written_problems(new: str, role_text: str, siblings, jd_text: str = "",
                     listed_skills=(), max_chars: int = 0, material: str = "",
                     employer: str = "") -> list[str]:
    """Why a bullet WRITTEN for one role may not go on the page; empty means it may.

    Like a professional resume writer (Kofi, 2026-11-10), the writer may describe the work a
    role of that title does, from the role's title, company and bullets, the person's skills
    (``listed_skills``) and projects (``material``), and the job's duties (``jd_text``). What
    stays refused: a number or percentage none of the person's material states, a named
    employer, product or client, or a degree, certification or award that neither the person's
    material nor the job names (tailoring/fact_gate.py); filler and keyword lists; and a bullet
    that repeats or restates another bullet of the role (one fact, one bullet)."""
    from .fact_gate import figures, known_material, new_claims, source_figures
    from .filler import (_FACT_STOP, _fact_tokens, filler_added, new_bullet_problems,
                         repeats_sibling, role_padding, same_fact, tacked_on_terms)

    new = str(new or "").strip()
    if not new:
        return ["empty"]
    pool = "\n".join([str(role_text or ""), str(material or ""), ", ".join(listed_skills or ())])
    bad = sorted(figures(new) - source_figures(pool), key=lambda s: (len(s), s))
    bad += new_claims(new, known=known_material(pool, str(jd_text or "")))
    bad += filler_added(role_text, new) + tacked_on_terms(role_text, new, count=False)
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
        # A written bullet carries the role's context (its tools, its platform); it restates
        # only when it adds fewer than WRITTEN_FRESH words of its own.
        fresh = {t for t in mine if t not in said}
        if (mine and sum(t in said for t in mine) / len(mine) >= WRITTEN_SAID_SHARE
                and len(fresh) < WRITTEN_FRESH):
            bad.append("restates the role's other bullets")
    if max_chars and len(new) > max_chars:
        bad.append("too long")
    # One fact, one bullet: something no other bullet of the role says.
    bad += new_bullet_problems(new, sibs)
    bad += role_padding(new, sibs, employer)
    return list(dict.fromkeys(bad))


# A written bullet whose content words are this share or more already on the role's other
# bullets restates them, and is refused (``written_problems``).
WRITTEN_SAID_SHARE = 0.5
# ...unless it adds at least this many content words of its own.
WRITTEN_FRESH = 4
# Two parts of a split may not share this many content words in a row.
SPLIT_SHARED_RUN = 4


def _looks_merged(text: str) -> bool:
    """A bullet that may hold several accomplishments (worth asking to split)."""
    t = str(text or "")
    # A long bullet with a clause joined on ("..., owning the KPI frameworks for both") holds two
    # pieces of work too; every part is checked again (``_split_ok``), so asking costs nothing
    # but a call.
    return len(t) >= 80 and (";" in t or t.count(",") >= 2 or ", and " in t
                             or (len(t) >= 100 and (" and " in t or ", " in t)))


def _split_call(llm, title: str, src: str, jd_text: str, tense: str, parts: int = 2,
                context: str = "", feedback: str = "") -> list:
    """One bullet split for the recency fill: ``llm.split_bullet`` into exactly ``parts`` parts
    in ``tense``, with the role's own material as ``context`` and why a previous try was refused
    as ``feedback`` when the backend takes them, or, for a backend without it,
    ``llm.expand_bullets`` (the caller drops a split with the wrong number of parts)."""
    fn = getattr(llm, "split_bullet", None)
    if callable(fn):
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        kw = {}
        if "context" in params:
            kw["context"] = context
        if feedback and "feedback" in params:
            kw["feedback"] = feedback
        return fn(title, src, parts, jd_text, tense=tense, **kw)
    return llm.expand_bullets(title, [src], jd_text)


_IRREGULAR_PAST = frozenset("""
built led ran made drove wrote grew cut set won began took gave brought sought taught held kept
met spent sold bought found chose saw oversaw understood rebuilt rewrote overhauled shipped put
became sent told thought upheld undertook withdrew
""".split())


def _opens_in_past(text: str) -> bool:
    """Does the bullet open with a past-tense verb ("Built", "Analyzed")?"""
    m = re.match(r"\s*([A-Za-z]+)", str(text or ""))
    w = m.group(1).lower() if m else ""
    return w in _IRREGULAR_PAST or (len(w) > 3 and w.endswith("ed"))


def _role_tense(role: dict, entry: dict, others) -> str:
    """The tense a new bullet of this role takes: "past" for a role that has ended; for a
    current role, the tense most of its other bullets on the page open in (past on a tie or
    with none to go by, which is how a resume reads)."""
    if not _PRESENT_RE.search(str(role.get("dates") or entry.get("dates") or "")):
        return "past"
    others = [o for o in others or () if str(o).strip()]
    present = sum(1 for o in others if not _opens_in_past(o))
    return "present" if present > len(others) - present else "past"


# A part of a split that is shorter than this, or says fewer content words, is too thin to
# stand as its own achievement ("Turn the reporting results into roadmap and spend decisions.",
# a real build): the bullet is kept whole instead.
SPLIT_MIN_CHARS = 85
SPLIT_MIN_WORDS = 6
# A split the checks refuse is asked for once more, with the reason.
SPLIT_TRIES = 2
# A written bullet the checks refuse is asked for again, up to this many tries in all.
WRITE_TRIES = 3
# A written bullet is at most about one printed line.
WRITE_MAX_CHARS = 115
# What to tell the model about a refused split, by the end of the refusal's reason.
_SPLIT_HINTS = {
    "padded with words the material does not say": "Use only the bullet's own words and ROLE "
    "MATERIAL; end each bullet at its fact, with no clause added to reach a length.",
    "repeats a phrase of another part": "Each bullet takes a different piece; do not repeat a "
    "phrase or a list across bullets.",
    "opens like another part": "Start each bullet with a different verb and subject.",
    "takes another bullet's point": "Leave out what the role's other bullets already say.",
    "too thin": "Each bullet needs its own detail (a tool, a number, a scale, who it was for) so "
    "it is a full sentence of at least 70 characters, taken from the bullet or ROLE MATERIAL.",
    "states what the bullet does not": "Name nothing the bullet and ROLE MATERIAL do not state.",
    "repeats another bullet": "Leave out what the role's other bullets already say.",
}
# A bullet splits into at most this many parts, and into three only when it is long enough to
# hold them (about this many characters of its own words per part).
SPLIT_MAX_PARTS = 3
SPLIT_CHARS_PER_PART = 85


def _split_ok(parts: list, src: str, role_text: str, siblings, jd_text: str,
              tense: str = "", employer: str = "") -> bool:
    """Every part of a split restates only its source bullet: no new figure, name or skill,
    no filler, no repeat of another bullet or of another part. Each part is a complete
    achievement of its own (``SPLIT_MIN_CHARS``, ``SPLIT_MIN_WORDS``) and opens in ``tense``."""
    return not _split_problem(parts, src, role_text, siblings, jd_text, tense, employer)


def _split_problem(parts: list, src: str, role_text: str, siblings, jd_text: str,
                   tense: str = "", employer: str = "") -> str:
    """Why a split may not go on the page (``_split_ok``); "" when it may."""
    from .fact_gate import figures, known_material, new_claims
    from .filler import (_content_stems, _fact_tokens, _longest_shared_run, borrows_from_sibling,
                         fact_stems, filler_added, new_bullet_problems, role_padding,
                         shares_a_clause)
    from .keywords import introduced_skills

    known = known_material(src, role_text)
    for n, part in enumerate(parts):
        if len(part) < SPLIT_MIN_CHARS or len(_content_stems(part)) < SPLIT_MIN_WORDS:
            return f"part {n + 1} too thin"
        if tense and _opens_in_past(part) != (tense == "past"):
            return f"part {n + 1} not in the {tense} tense"
        if figures(part) - figures(src) or new_claims(part, known=known):
            return f"part {n + 1} states what the bullet does not"
        if introduced_skills(src, part, role_text, jd_text) or filler_added(src, part):
            return f"part {n + 1} adds a skill or filler"
        if len(part) > _bullet_budget(src):
            return f"part {n + 1} longer than the bullet"
        if any(shares_a_clause(part, str(s)) for s in siblings or ()):
            return f"part {n + 1} repeats another bullet"
        # A part that takes another bullet's point ("..., turning findings into decisions on
        # capacity and service levels" beside the bullet that says exactly that, a real split on
        # 2026-11-10) says it twice on the page.
        if borrows_from_sibling(src, part, siblings):
            return f"part {n + 1} takes another bullet's point"
        # Parts of one sentence each say their own thing: a phrase repeated across parts
        # ("growth, usage, and unit costs" three times, a real split on 2026-11-06) reads as
        # padding on the page. Counted in content words, so a part may carry its source's
        # context ("for stakeholders across the business") and still read on its own.
        if any(_longest_shared_run(fact_stems(part), fact_stems(other)) >= SPLIT_SHARED_RUN
               for other in parts[n + 1:]):
            return f"part {n + 1} repeats a phrase of another part"
        # Two bullets that open the same way ("Turned operational findings into product
        # decisions..." over "Turned operational findings into operations decisions...") read
        # as one fact said twice.
        if any(_fact_tokens(part)[:3] == _fact_tokens(other)[:3] for other in parts[n + 1:]):
            return f"part {n + 1} opens like another part"
        # Each part says a fact the role's other bullets do not, and is not empty filler.
        bad = new_bullet_problems(part, siblings, restate=False)
        # No padding that states no fact: its own employer's name, a stock tail, a phrase
        # another bullet of the role (or the other part) already says.
        bad += role_padding(part, list(siblings or ()) + [x for k, x in enumerate(parts) if k != n],
                            employer, src, repeats_from_source=False)
        if bad:
            return f"part {n + 1}: {bad[0]}"
    return ""


# A trailing clause a model adds to reach a length: ", giving stakeholders a shared view of ...",
# ", covering the volume ...", ", shaping how the platform was built" (real splits, 2026-11-10).
_TAIL_CLAUSE = re.compile(r"^(?P<head>.+?),\s+(?:and\s+)?(?P<tail>[a-z]+ing\b[^,;]*)\.?\s*$")


def _without_padded_tail(part: str, material: str, siblings=()) -> str:
    """``part`` without a trailing "-ing" clause that is padding (most of its content words
    appear nowhere in ``material``) or another bullet's point (``siblings``), when that clause
    states no figure; ``part`` unchanged otherwise. The person's own words are never taken off."""
    from .fact_gate import figures
    from .filler import fact_stems

    m = _TAIL_CLAUSE.match(str(part or "").strip())
    if not m or figures(m.group("tail")):
        return part
    tail = set(fact_stems(m.group("tail")))
    if not tail:
        return part
    known = set(fact_stems(material))
    invented = tail - known
    borrowed = any(len(tail & set(fact_stems(str(s)))) >= 3 for s in siblings or ())
    if len(invented) / len(tail) <= 0.5 and not borrowed:
        return part
    return m.group("head").rstrip(" ,") + "."


def _fill_log(**rec) -> None:
    """One line of what the page fill tried and why it was refused, to the file named by
    ``TAILOR_FILL_LOG`` when it is set (a developer's diagnosis; nothing is written otherwise)."""
    import json
    import os
    path = os.environ.get("TAILOR_FILL_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


def _recency_fill(preamble: str, used: dict, result, *, sections, headings, workdir: Path,
                  jobname: str, llm, jd_text: str, tailor: bool, write: bool, source: dict,
                  material=(), gate_material=(), origins: dict | None = None,
                  reverted: list | None = None, trims: list | None = None, keep=(),
                  state: dict | None = None):
    """Give the most recent roles their bullets, then fill a short page with more, re-measuring
    the real page after every bullet. Returns (result, added, notes).

    Recency comes first (Kofi, 2026-11-06). The most recent role (and a role the person
    leads with) reach their fewest bullets (three) even on a page that is already full: when a
    bullet does not fit, room is made from OLDER material, one cut at a time and re-measured
    after each (``_make_room``), stopping as soon as the page is one clean page again. If even
    that cannot fit it, the bullet and the cuts are undone (the best clean page stays) and a
    note says so. Every cut is recorded in ``trims`` (Put back), and a cut the person put back
    (``keep``) is never made again.

    Then THE STRICT STAIRCASE (Kofi, 2026-11-10), built from the bottom up: the oldest role
    keeps ``OLDEST_FLOOR`` (2), and each more recent role reaches one more than the role below it
    (2, 3, 4, 5 for four roles) before any role above it grows, room for a step made from a
    project's extra bullets or a skills line (never another role's bullets). Only then, while
    the page is short of ``FILL_GOAL``, does it grow one bullet at a time, the most recent role
    first and each role below the one above it, the top held to ``STAIR_SOFT_TOP`` until every
    role below stands one step under it (up to ``STAIR_TOP``). A bullet that would overflow (two
    pages, a new overfull line, or past ``MAX_FILL``) is taken back off. An older role that shows
    as many bullets as the role above it is cut to the step (``staircase`` in ``trims``, Put back
    offers it), never below ``OLDEST_FLOOR``, unless that leaves the page short: white space is
    never the answer, so the cut is undone and a note asks for more about the role that stands
    too low. Still short with every role dry: a project's own long bullet splits into its parts.

    Each bullet comes from, in order: (a) the person's own bullet for that role that selection
    left out (``_reserve``), put back in the author's order and, with ``tailor``, reworded
    toward the job like any other; (b) a split of one of the role's bullets that merges several
    accomplishments (``llm.split_bullet``, each part checked against its source bullet);
    (c) a NEW bullet written from that role's own material only (``llm.write_role_bullet``),
    refused by ``written_problems`` if it states anything the material does not, repeats a
    sibling, or adds no fact of its own (``filler.new_bullet_problems``). A split may make three
    parts of a long bullet, or split a bullet it split in two again in three; its parts are
    held to the same one-fact rule. The role's material includes what the person's uploaded
    resumes say under that employer (``role_notes``). (b) and (c) need ``write`` and a model (a fresh build, never a rebuild after the
    person's own edit). A role whose (c) is refused gets no more written bullets in this build.
    """
    from .fact_gate import _strings, known_material, source_figures
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
    # Per role: what was tried (shared by a second pass, so nothing is asked of the model twice).
    state = state if state is not None else {}

    def clean(r) -> bool:
        return bool(r and r.ok and r.pages == 1 and r.overfull_count <= base_overfull
                    and r.fill_ratio is not None and r.fill_ratio <= MAX_FILL)

    def next_step(entry: dict, role: dict, most: int, section: str = "experience"):
        # ``most`` caps how many bullets the role may reach with this step: a split that would
        # carry it past the bullet the caller asked for is not taken.
        # A role whose bullets were drafted as placeholders (not the person's words) is never
        # split or written from: only the person's own material may seed a new bullet. A
        # project is only ever split, never written for.
        own = not (role.get("drafted_bullets") or entry.get("drafted_bullets"))
        st = state.setdefault((section,) + _role_key(entry, role),
                              {"tried": set(), "write": can_write and own and section == "experience",
                               "dry": False})
        bl = role["bullets"]
        label = entry_label(entry, role)
        employer = str(entry.get("org") or entry.get("company") or "") if section == "experience" else ""
        rec = {"section": section, "label": label}
        src_role = source_roles.get(_role_key(entry, role))
        reserve = role.get("_reserve") or []
        if reserve:
            # Their own bullets with a distinctive figure come back first: the strongest fact in
            # a role is the first thing kept, never the last thing written for.
            from .filler import figures_of
            reserve.sort(key=lambda s: 0 if figures_of(str(s)) else 1)
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
        if use_llm and own and room >= 2:                      # (b) split a long bullet
            # The longest of the role's own bullets first: it holds the most to split. A bullet
            # already split in two may be split again in three (its parts replaced), so a role
            # whose material is one rich bullet still climbs its step (Viva, 2026-11-10).
            srcs = list(dict.fromkeys(str(origins.get(b, b)) for b in bl))
            for src in sorted(srcs, key=len, reverse=True):
                on = [k for k, x in enumerate(bl) if str(origins.get(x, x)) == src]
                have = len(on)
                most_parts = min(SPLIT_MAX_PARTS, max(2, len(src) // SPLIT_CHARS_PER_PART))
                if have == 1:
                    if not _looks_merged(src):
                        continue
                    sizes = range(min(room, most_parts), 1, -1)
                else:
                    sizes = [have + 1] if have + 1 <= most_parts else []
                others = [x for k, x in enumerate(bl) if k not in on]
                tense = _role_tense(role, entry, others)
                for n in sizes:
                    if (src, n) in st["tried"]:
                        continue
                    st["tried"].add((src, n))
                    parts, why, feedback = [], "", ""
                    # One try, and one more told why the first was refused (CLAUDE.md section 9:
                    # read what failed, fix it, check again).
                    for _attempt in range(SPLIT_TRIES):
                        try:
                            parts = _split_call(llm, str(role.get("title") or ""), src, jd_text,
                                                tense, n, role_text, feedback)
                        except Exception:                      # noqa: BLE001 - a split is optional
                            parts, why = [], "no answer"
                            break
                        parts = [str(x).strip() for x in parts or [] if str(x).strip()]
                        if len(parts) != n or len(others) + n > most:
                            why = "not that many parts"
                            _fill_log(step="split", label=label, src=src, n=n, parts=parts, why=why)
                            break                              # the model says it holds fewer
                        why = _split_problem(parts, src, role_text, others, jd_text, tense,
                                             employer)
                        if why:
                            # A model told to write whole sentences tends to end a part with a
                            # clause that only reaches a length: the part without it is tried.
                            trimmed = [_without_padded_tail(x, src + "\n" + role_text, others)
                                       for x in parts]
                            if trimmed != parts:
                                why2 = _split_problem(trimmed, src, role_text, others, jd_text,
                                                      tense, employer)
                                _fill_log(step="split-trim", label=label, src=src, n=n,
                                          parts=trimmed, why=why2)
                                if not why2:
                                    parts, why = trimmed, ""
                        _fill_log(step="split", label=label, src=src, n=n, parts=parts, why=why)
                        if not why:
                            break
                        hint = next((h for k, h in _SPLIT_HINTS.items() if k in why), "")
                        feedback = (f"Your last split was refused ({why}): "
                                    + " | ".join(parts).rstrip(".") + ". " + hint).strip()
                    if why:
                        continue                               # too thin or off: keep it whole
                    for x in parts:
                        origins[x] = src
                    at = on[0]
                    rest = [x for k, x in enumerate(bl) if k not in on]
                    return (rest[:at] + parts + rest[at:],
                            [dict(rec, bullet=x, kind="split", source=src) for x in parts])
        if st["write"]:                                        # (c) written for the role
            # One full line: it fits the room a short page has left, and the polish runs it to
            # the edge.
            budget = min(_bullet_budget(max(map(str, bl), key=len) if bl else ""), WRITE_MAX_CHARS)
            # A professional writer's sources (Kofi, 2026-11-10): the role, the company, the
            # person's skills and projects, and the job's duties for that kind of role.
            extra = "\n".join(
                (["Skills: " + ", ".join(listed)] if listed else [])
                + [" ".join(_strings(p)) for p in (source.get("projects") or [])
                   if isinstance(p, dict)])
            writer_text = role_text + ("\n\nTHE PERSON'S SKILLS AND PROJECTS:\n" + extra if extra else "")
            terms = list(supported)
            # Every bullet the role has, on the page or not (one the fill cut or the person's own
            # left out): a written bullet never restates one of them.
            mine = list(bl) + [str(x) for x in (src_role or {}).get("bullets") or []
                               if not any(same_fact(str(x), str(origins.get(b, b))) for b in bl)]
            for _attempt in range(WRITE_TRIES):
                try:
                    new = str(llm.write_role_bullet(label, writer_text, list(mine), terms, budget,
                                                    jd_text=jd_text) or "").strip()
                except Exception:                              # noqa: BLE001
                    new = ""
                if new.strip(". ").upper() == "NONE":
                    new = ""
                why = (written_problems(new, role_text, mine, jd_text, listed, budget,
                                        material=extra, employer=employer) if new else ["none"])
                _fill_log(step="write", label=label, bullet=new, why=why)
                if new and not why:
                    origins[new] = new                         # traced through the line fit
                    return bl + [new], [dict(rec, bullet=new, kind="written", source="")]
                if not new:
                    break
                mine = mine + [new]                            # never offered the same twice
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

    # 1. The most recent role reaches its fewest bullets, making room from older material.
    blocked: set = set()
    for rank, entry, role in order:
        # The most recent role (and one the person leads with): under the staircase every older
        # role shows fewer than the one above it, so room is made for the top of it only.
        if rank >= 1 and not lead(entry, role):
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

    # 2. THE STRICT STAIRCASE (Kofi, 2026-11-10): white space is never acceptable, and the
    # bullets step down by recency, strictly: the most recent role carries the most, each older
    # role fewer than the one above it, and the oldest keeps at least ``OLDEST_FLOOR``. It is
    # built from the BOTTOM UP: the oldest role reaches its floor, then each more recent role
    # reaches one more than the role below it (CSIR 2, Viva 3, Stanbic 4, BlackOrigin 5), each
    # from its own material (left-out bullets, splits of its own long bullets, bullets written
    # from its material). Only then, while the page is still short, do the roles grow one bullet
    # at a time, the most recent first, each staying below the role above it, with the top role
    # held to ``STAIR_SOFT_TOP`` until every role below stands one step under it. Re-measured
    # after every bullet; a bullet that would overflow comes back off.
    chain = [(rank, e, r) for rank, e, r in order if not lead(e, r)]
    for _rank, _e, r in chain:
        r.setdefault("bullets", [])
    full: set = set()                       # roles whose next bullet would overflow the page

    def grow(entry: dict, role: dict, cap: int, room: bool = False,
             section: str = "experience") -> bool:
        nonlocal result
        if id(role) in blocked or _role_key(entry, role) in full:
            return False
        step = next_step(entry, role, cap, section)
        if step is None:
            return False
        before = list(role["bullets"])
        role["bullets"][:] = step[0]
        r = measure()
        undo: list = []
        # A step of the strict staircase is worth a project's extra bullet or a skills line
        # beyond the minimum (``_make_room``, never another role's bullets), one cut at a time.
        while room and not clean(r):
            u = _make_room(used, cuts, order, older_roles=False)
            if u is None:
                break
            undo.append(u)
            r = measure()
        if not clean(r):
            # This bullet would overflow: it comes back off, and this role takes no more in this
            # pass. Another role's shorter bullet may still fit the lines that are left.
            role["bullets"][:] = before
            for u in reversed(undo):
                u()
            full.add(_role_key(entry, role))
            _fill_log(step="overflow", label=entry_label(entry, role), fill=getattr(r, "fill_ratio", None))
            return False
        result = r
        _fill_log(step="grew", label=entry_label(entry, role), n=len(role["bullets"]),
                  fill=r.fill_ratio)
        # A split made again in more parts replaces the parts it made before.
        label = entry_label(entry, role)
        now = {str(b) for b in role["bullets"]}
        added[:] = [a for a in added if a.get("label") != label or str(a.get("bullet")) in now]
        added.extend(step[1])
        return True

    def short() -> bool:
        return result.fill_ratio is not None and result.fill_ratio < FILL_GOAL

    def floor_of(i: int) -> int:
        if i == len(chain) - 1:
            return OLDEST_FLOOR if i else STAIR_FIRST
        below = len(chain[i + 1][2]["bullets"]) + 1
        return max(below, STAIR_FIRST) if i == 0 else below

    def dry(entry: dict, role: dict) -> bool:
        return bool(state.get(("experience",) + _role_key(entry, role), {}).get("dry"))

    def base():
        for i in range(len(chain) - 1, -1, -1):
            _rank, entry, role = chain[i]
            want = floor_of(i)
            if i and dry(chain[i - 1][1], chain[i - 1][2]):
                # The role above has no more to give: this one stays a step under it.
                want = min(want, max(len(chain[i - 1][2]["bullets"]) - 1,
                                     OLDEST_FLOOR if i == len(chain) - 1 else 1))
            while len(role["bullets"]) < want:
                if not grow(entry, role, want, room=True):
                    break

    def unwind() -> bool:
        # The bottom-up climb reached a role whose role above then ran dry: the bullets the fill
        # WROTE for it come back off until it stands a step under the role above again (the
        # person's own bullets are hold()'s to cut, with Put back).
        written = {str(a.get("bullet")) for a in added if a.get("kind") == "written"}
        took = False
        for i in range(1, len(chain)):
            _rank, entry, role = chain[i]
            floor = OLDEST_FLOOR if i == len(chain) - 1 else 1
            lim = max(floor, len(chain[i - 1][2]["bullets"]) - 1)
            bl = role["bullets"]
            while len(bl) > lim:
                k = next((j for j in range(len(bl) - 1, -1, -1) if str(bl[j]) in written), None)
                if k is None:
                    break
                bl.pop(k)
                took = True
        if took:
            now = {str(b) for _k, _e, r in chain for b in r["bullets"]}
            added[:] = [a for a in added if a.get("section") != "experience"
                        or str(a.get("bullet")) in now]
        return took

    def one_more(top_cap: int) -> bool:
        for i, (_rank, entry, role) in enumerate(chain):
            cap = top_cap if i == 0 else len(chain[i - 1][2]["bullets"]) - 1
            if len(role["bullets"]) < cap and grow(entry, role, len(role["bullets"]) + 1):
                return True
        return False

    def climb():
        nonlocal result
        if not chain or blocked:
            return
        base()
        top_cap = min(STAIR_TOP, max(STAIR_SOFT_TOP, len(chain[0][2]["bullets"])))
        while short():
            if one_more(top_cap):
                continue
            # The top goes past STAIR_SOFT_TOP only when every role below stands a step under the
            # one above it: a tie lower down is not fixed by piling more onto the top.
            counts = [len(r["bullets"]) for _k, _e, r in chain]
            if top_cap >= STAIR_TOP or any(a - b != 1 for a, b in zip(counts, counts[1:])):
                break
            top_cap += 1                     # every role stands a step below: the page needs more
        if unwind():
            r = measure()
            if r and r.ok and r.pages == 1 and r.fill_ratio is not None:
                result = r

    def hold() -> bool:
        # The staircase holds: an older role never shows as many bullets as the role above it.
        # What does not fit the step is cut (Put back offers it; a bullet the person put back is
        # never cut again), never below ``OLDEST_FLOOR``.
        cut = False
        for i in range(1, len(chain)):
            _rank, entry, role = chain[i]
            lim = max(OLDEST_FLOOR, len(chain[i - 1][2]["bullets"]) - 1)
            bl = role["bullets"]
            label = entry_label(entry, role)
            while len(bl) > lim:
                idx = _cut_victim(bl, 0, cuts, label)
                if idx is None:
                    break
                text = bl.pop(idx)
                cuts.add("bullet", "experience", label, str(text), index=idx, staircase=True)
                cut = True
        return cut

    # Grow, then hold the staircase. A cut the staircase makes lets the top grow again; when even
    # that leaves the page short of FILL_GOAL, the cuts are undone (white space is never the
    # answer) and a note asks the person for more about the role that stands too low.
    climb()
    snap = ([list(r["bullets"]) for _k, _e, r in chain], len(trims) if trims is not None else 0,
            list(added))
    if hold():
        r = measure()
        if r and r.ok and r.pages == 1 and r.fill_ratio is not None:
            result = r
            full.clear()                     # the cuts made room: every role may try again
            climb()
        if not (result and result.fill_ratio is not None and result.fill_ratio >= FILL_GOAL):
            for (_k, _e, r), bl in zip(chain, snap[0]):
                r["bullets"][:] = bl
            if trims is not None:
                del trims[snap[1]:]
            added[:] = snap[2]
            r = measure()
            if r and r.ok and r.pages == 1:
                result = r

    # 3. Still short: the person's own bullets that selection left out come back (Kofi,
    # 2026-10-07: what they gave is never held back from a short page), up to each role's recency
    # budget, most recent first, as long as that makes no NEW tie or inversion with the role
    # above (one that already shows fewer stays as it is: holding the bullet back would only
    # leave white space).
    if chain and short() and not blocked:
        for i, (rank, entry, role) in enumerate(chain):
            _fewest, most = bullet_budget(rank)
            while short() and role.get("_reserve") and len(role["bullets"]) < most:
                n, above = len(role["bullets"]), (len(chain[i - 1][2]["bullets"]) if i else 10 ** 6)
                if n < above <= n + 1:               # one more would tie the role above
                    break
                if not grow(entry, role, len(role["bullets"]) + 1):
                    break

    for i in range(1, len(chain)):
        (_a, e0, r0), (_b, e1, r1) = chain[i - 1], chain[i]
        n0, n1 = len(r0["bullets"]), len(r1["bullets"])
        if n1 >= n0 and n1 > 1:
            notes.append(f"{entry_label(e0, r0)} shows {n0} bullet{'' if n0 == 1 else 's'}, "
                         f"no more than the older {entry_label(e1, r1)}: there is no more "
                         "of your material for it yet. Tell me more about that role and I "
                         "will add it.")
            break
    for rank, entry, role in order:
        fewest, _most = bullet_budget(rank, lead(entry, role))
        n = len(role.get("bullets") or [])
        drafted = role.get("drafted_bullets") or entry.get("drafted_bullets")
        named = any(n_.startswith(entry_label(entry, role) + " shows") for n_ in notes)
        if n < fewest and not drafted and not named \
                and state.get(("experience",) + _role_key(entry, role), {}).get("dry"):
            notes.append(f"{entry_label(entry, role)} shows {n} bullet{'' if n == 1 else 's'}: "
                         "there is no more of your material for that role yet. Tell me more "
                         "about it and I will add it.")
    return result, added, notes


# A polished bullet aims at this share of its lines' width: one full line, or two.
POLISH_LO, POLISH_HI = 0.93, 0.99


def _chars_per_line(rep) -> float:
    """Characters one full bullet line holds on this page, from the measured bullets."""
    est = [len(i.text) / i.total for i in rep.bullets if i.found and i.total >= 0.5]
    est.sort()
    return est[len(est) // 2] if est else 0.0


def _polish_edges(preamble, used: dict, result, *, llm, jd_text: str, sections, headings,
                  workdir: Path, jobname: str, origins: dict, known: dict, known_figures: set,
                  tries: int = 2, projects_only: bool = False):
    """Rewrite each entry's bullets so every one runs to the right edge: one call per entry
    (``llm.polish_role``, every role and every project with a bullet short of the edge) with a
    character window per bullet from the measured width. A bullet that spills a stub line is
    tightened to one line fewer while the page is at the fill goal (the freed line goes to the
    staircase afterwards) and extended to a full last line while it is short; a short one-line
    bullet is extended to a full line. Each rewrite passes the fact gate and the filler and
    padding checks. An entry's rewrites are kept when the page stays one clean page and the
    entry has fewer short or stub bullets than before. ``tries`` passes, the later ones only
    for what is still short. Returns the result."""
    from . import linefit as _lf
    from .fact_gate import gate_rewrite
    from .filler import hollow, is_padded, role_padding

    base_overfull = result.overfull_count

    def clean(r) -> bool:
        return bool(r and r.ok and r.pages == 1 and r.overfull_count <= base_overfull
                    and r.fill_ratio is not None and r.fill_ratio <= MAX_FILL)

    def entries():
        for p in used.get("projects") or []:
            if isinstance(p, dict) and isinstance(p.get("bullets"), list):
                yield p, p, "", entry_label(p)
        for _rank, e, r in ([] if projects_only else ranked_roles(used)):
            if isinstance(r.get("bullets"), list):
                yield e, r, str(e.get("org") or e.get("company") or ""), entry_label(e, r)

    def short_in(rep, bl) -> int:
        fits = {i.text: i for i in rep.bullets}
        return sum(1 for b in bl if b in fits and not fits[b].ok())

    from . import plan_fill as _pf

    def _polish_ok(cand, k, new, lo, hi) -> bool:
        from .fact_gate import figures as _figs
        bl_k = bl_now[k]
        # A polish keeps every figure the bullet states and ends as a sentence (run 12 dropped
        # "about 10 drivers" and the full stop).
        if _figs(bl_k) - _figs(cand) or not cand.rstrip().endswith((".", "!", "?")):
            _fill_log(step="polish", label=label_now[0], bullet=cand, why="lost a figure or the full stop")
            return False
        if not (lo * 0.6 <= len(cand) <= hi * 1.25):
            return False
        others = [x for j, x in enumerate(new) if j != k]
        src = str(origins.get(bl_k, bl_k))
        why = (gate_rewrite(bl_k, cand, known=known, known_figures=known_figures)
               + (["filler"] if hollow(cand) or is_padded(bl_k, cand) else [])
               + role_padding(cand, others, employer_now[0], src + " " + bl_k))
        if why:
            _fill_log(step="polish", label=label_now[0], bullet=cand, why=why)
        return not why

    bl_now: list = []
    label_now, employer_now = [""], [""]
    boost: dict = {}                        # (entry, bullet) -> how much longer the next versions

    for attempt in range(tries):
        rep = _lf.measure(getattr(result, "pdf_path", None), used)
        cpl = _chars_per_line(rep)
        cap = _pf.line_capacity(rep)
        if not cpl:
            return result
        fits = {i.text: i for i in rep.bullets}
        for entry, role, employer, label in list(entries()):
            bl = role["bullets"]
            bl_now[:] = list(bl)
            label_now[0], employer_now[0] = label, employer
            todo = [k for k, b in enumerate(bl) if b in fits and not fits[b].ok()]
            if not todo:
                continue
            # Tighten a stub only when the page keeps the fill goal with a line fewer; otherwise
            # run it out to a full last line (run 12: a project bullet tightened to one line took
            # the page from 95.5% to 93.9%).
            at_goal = (result.fill_ratio or 0) - _lf.LINE_SHARE >= FILL_GOAL
            asks, lines_for = [], []
            for k in range(len(bl)):
                it = fits.get(bl[k])
                by_text = max(1, math.ceil(len(bl[k]) / cpl - 0.15))
                if it and it.found:
                    # As printed, unless the measure matched lines not its own; a stub is run
                    # out to a full last line, or tightened to a line fewer while the page keeps
                    # the goal without it.
                    n = it.lines if it.lines <= by_text + 1 else by_text
                    if n >= 2 and it.last_fill < _lf.SPILL_TRIM and at_goal:
                        n -= 1
                else:
                    n = by_text
                asks.append((int(cpl * n * POLISH_LO), int(cpl * n * POLISH_HI)))
                lines_for.append(n)
            try:
                out = llm.polish_role(label, list(bl), asks, context=profile_text_safe(entry),
                                      jd_text=jd_text, only=todo,
                                      words=[[max(6, round(w * boost.get((label, k), 1.0)))
                                              for w in _pf.word_targets(n, cpl)]
                                             for k, n in enumerate(lines_for)])
            except Exception as exc:                         # noqa: BLE001 - polish is optional
                _fill_log(step="polish-error", label=label, why=str(exc)[:200])
                continue
            if not isinstance(out, list) or len(out) != len(bl):
                _fill_log(step="polish-error", label=label, why="wrong number of bullets")
                continue
            new = list(bl)
            for k in todo:
                lo, hi = asks[k]
                versions = out[k] if isinstance(out[k], list) else [out[k]]
                good = []
                for cand in versions:
                    cand = str(cand or "").strip()
                    if cand and cand != bl[k] and _polish_ok(cand, k, new, lo, hi):
                        good.append(cand)
                if not good:
                    continue
                # Versions of different length: the one whose last line lands full on its lines
                # (tailoring/plan_fill.choose_version), measured against this page.
                cand, landed, _way = (_pf.choose_version(good, lines_for[k], cap) if cap
                                      else (good[0], True, ""))
                if cap and not landed:
                    # Every version fell short (or spilled): the next try's word targets move by
                    # how far this one missed (run 20: a project's versions all stopped near 85%
                    # of two lines, and the same targets were asked again).
                    got = _pf.lines_needed(cand, cap) or 1.0
                    want = lines_for[k] - 1 + 0.95
                    boost[(label, k)] = min(1.6, max(0.6, boost.get((label, k), 1.0) * want / got))
                _fill_log(step="polish-versions", label=label, n=k, why={
                    "lines": lines_for[k], "landed": landed,
                    "versions": [round(_pf.lines_needed(v, cap), 2) for v in good] if cap else []})
                new[k] = cand
            if new == bl:
                continue
            old = list(bl)
            role["bullets"][:] = new
            r = compile_tex(render_cv(preamble, used, sections, probe=True, headings=headings),
                            workdir, jobname=f"{jobname}-polish")
            ok = clean(r)
            if ok:
                after = _lf.measure(getattr(r, "pdf_path", None), used)
                ok = short_in(after, new) < short_in(rep, old)
            # Never the page under the fill goal when it was at it.
            if ok and (result.fill_ratio or 0) >= FILL_GOAL > (r.fill_ratio or 0):
                ok = False
            if ok:
                result = r
                for o, n_ in zip(old, new):
                    if o != n_:
                        origins[n_] = origins.get(o, o)
                _fill_log(step="polished", label=label, fill=r.fill_ratio)
            else:
                role["bullets"][:] = old
                _fill_log(step="polish-undone", label=label, fill=getattr(r, "fill_ratio", None),
                          why={"pages": getattr(r, "pages", None), "clean": clean(r),
                               "before": result.fill_ratio, "asks": asks, "new": new})
    return result


def profile_text_safe(entry: dict) -> str:
    from .keywords import profile_text
    try:
        return profile_text(entry)
    except Exception:                                        # noqa: BLE001
        return ""


def staircase_ok(profile: dict) -> bool:
    """Do the bullets step down by recency: every role (but one the person leads with) shows
    fewer bullets than the more recent role above it, down to one?"""
    counts = [len(r.get("bullets") or []) for _rank, e, r in
              sorted(ranked_roles(profile), key=lambda x: x[0])
              if not (r.get("_lead") or e.get("_lead"))]
    return all(b < a or b <= 1 for a, b in zip(counts, counts[1:]))


def _make_room(profile: dict, cuts: "_Cuts", order: list, older_roles: bool = True):
    """Make room for the most recent roles' fewest bullets with ONE cut of older material, in
    the order Kofi set (2026-11-06): (1) an older role's bullets beyond two, oldest role first
    (skipped with ``older_roles`` off, for the strict staircase's own steps); (2) a project's
    bullets beyond one, last project first; (3) a skills line beyond the template's minimum
    (``SKILL_LINES_FLOOR``). The oldest role keeps ``OLDEST_FLOOR`` bullets (Kofi, 2026-11-10:
    it used to go down to one). Never a whole role, never the summary, never type or margins. The cut is recorded in ``cuts`` (Put
    back, marked ``room`` so ``_restore_room`` can try it again at the end) and a cut the
    person put back is skipped. Returns a function that undoes the cut, or
    None when nothing is left to cut."""
    trims = cuts.trims

    def forget(rec):
        if trims is not None and rec in trims:
            trims.remove(rec)

    def pop(bl: list, section: str, label: str, keep_n: int):
        idx = _cut_victim(bl, keep_n, cuts, label)
        if idx is None:
            return None
        text = bl.pop(idx)
        cuts.add("bullet", section, label, str(text), index=idx, room=True)
        rec = trims[-1] if trims else None

        def undo():
            bl.insert(idx, text)
            forget(rec)
        return undo

    older = sorted((x for x in order if x[0] >= len(RECENCY_BUDGETS)
                    and not (x[2].get("_lead") or x[1].get("_lead"))), key=lambda x: -x[0])
    for _rank, e, r in (older if older_roles else ()):                # (1)
        u = pop(r["bullets"], "experience", entry_label(e, r), max(OLDER_BUDGET[0], OLDEST_FLOOR))
        if u:
            return u
    for p in reversed(profile.get("projects") or []):                # (2)
        if isinstance(p, dict) and isinstance(p.get("bullets"), list):
            u = pop(p["bullets"], "projects", entry_label(p), 1)
            if u:
                return u
    skills = profile.get("skills")                                    # (3)
    if isinstance(skills, dict) and len(skills) > SKILL_LINES_FLOOR:
        last = next((k for k in reversed(list(skills))
                     if not cuts.kept("skills_line", str(k), str(k))), None)
        if last is not None:
            before = dict(skills)
            cuts.add("skills_line", "skills", str(last), str(last), value=skills[last], room=True)
            rec = trims[-1] if trims else None
            del skills[last]

            def undo():
                skills.clear()
                skills.update(before)
                forget(rec)
            return undo
    return None


def _restore_room(preamble: str, used: dict, result, trims: list, *, sections, headings,
                  workdir: Path, jobname: str):
    """Put back what ``_make_room`` cut, in the reverse of the cut order (an older role's
    bullet before a project bullet before a skills line, the last cut first), one at a time,
    re-measuring the real page. A restore is kept only if the page stays one clean page: one
    page, no new overfull line, fill at most ``MAX_FILL``. A kept restore leaves ``trims``
    (nothing to put back any more). Only adds material, so the recency minimums stand, and a
    cut the person put back was never made. Returns (used, result, trims)."""
    import copy
    room = [t for t in trims or () if t.get("room")]
    if not room:
        return used, result, trims
    base_overfull = result.overfull_count
    trims = list(trims)
    for t in reversed(room):
        before = copy.deepcopy(used)
        if not restore_trim(used, t):
            continue
        _say("fill", "Filling the page", "putting back what fits after all")
        r = compile_tex(render_cv(preamble, used, sections, probe=True, headings=headings),
                        workdir, jobname=f"{jobname}-restore")
        if (r.ok and r.pages == 1 and r.overfull_count <= base_overfull
                and r.fill_ratio is not None and r.fill_ratio <= MAX_FILL):
            result = r
            trims.remove(t)
        else:
            used = before
    return used, result, trims


def _trimmed_by_label(trims) -> dict:
    """{entry label: [text]} of the bullets and sentences the one-page fit cut."""
    out: dict = {}
    for t in trims or ():
        if t.get("kind") in ("bullet", "sentence"):
            text = str(t.get("cut") or t.get("text") or "")
            out.setdefault(str(t.get("label") or ""), []).append(text)
    return out


def _resolve_added(used: dict, added: list, origins: dict) -> list:
    """Each added bullet as it reads on the final page (the line fit may have resized it,
    and ``origins`` traces every rewrite back), dropping any that is no longer there."""
    page: dict = {}
    for _rank, e, r in ranked_roles(used):
        page.setdefault(entry_label(e, r), []).extend(str(b) for b in r.get("bullets") or [])
    for p in used.get("projects") or []:
        if isinstance(p, dict):
            page.setdefault(entry_label(p, p), []).extend(str(b) for b in p.get("bullets") or [])
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
    skill_cap: int | None = None,
    skill_keep=(),
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
    4 to 5 one-line Skills lines that reach the right margin, and every bullet's last
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
    # A Skills line shows skills: no degree, certification or years of experience, no "BI"
    # beside "Power BI", no "CI" and "CD" for CI/CD (tailoring/linefit.clean_skill_block).
    # At most ``skill_cap`` Skills lines (linefit.SKILL_LINES_MAX unless the template's manifest
    # says otherwise): lines merge by meaning, vague and non-job terms go first, never a job term
    # or one the person ADDed (``skill_keep``) while another can go (linefit.cap_skill_block).
    from . import linefit as _lfm
    cap = int(skill_cap or _lfm.SKILL_LINES_MAX)
    if isinstance(full_profile.get("skills"), dict):
        full_profile["skills"] = _lfm.cap_skill_block(
            _lfm.clean_skill_block(full_profile["skills"]), cap, jd_text=jd_text, keep=skill_keep)
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
    fill_state: dict = {}

    def fill_page(res):
        return _recency_fill(
            preamble, used, res, sections=sections, headings=headings, workdir=workdir,
            jobname=jobname, llm=llm, jd_text=jd_text, tailor=tailor,
            write=tailor if fill_writes is None else fill_writes, source=source_material,
            material=material, gate_material=gate_material, origins=origins, reverted=reverted,
            trims=trims, keep=keep, state=fill_state)
    # PLAN, THEN WRITE (Kofi, 2026-11-10): a fresh build with a backend that writes to a plan
    # does the Experience section in one planned pass after the line fit
    # (tailoring/plan_fill.py), so the bullet-by-bullet fill does not run first.
    planning = bool(fit_lines and llm is not None
                    and callable(getattr(llm, "write_experience", None)))
    if result and result.ok and result.pages == 1 and not planning:
        result, added, bullet_notes = fill_page(result)
        # The review's "before" side gets the person's own bullets the fill brought back.
        keys = {entry_label(e, r): _role_key(e, r) for _rank, e, r in ranked_roles(used)}
        sources = {_role_key(e, r): r for e, r in _experience_roles(selected_source)}
        for rec in added:
            src_role = sources.get(keys.get(rec["label"]))
            if rec.get("kind") == "saved" and src_role is not None:
                src_role.setdefault("bullets", []).append(rec["source"])
    _say("compile", "Compiling", "the PDF")

    # FULL-WIDTH LINES (tailoring/linefit.py). Measured on the real page: Skills packed into 4
    # to 5 one-line lines that reach the right margin, each bullet's last line filled or a
    # one-word spill trimmed. Each round must keep the page clean and score better, so the
    # result is never worse than the page above.
    line_fit = line_before = line_gaps = pre_fit = None

    def fit_once(used, result):
        """One line-fit pass on the page; returns (used, result, outcome)."""
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
            # A resize never takes the point of a bullet cut to fit (Put back would print it twice).
            trimmed=_trimmed_by_label(trims),
            known=known_material(*gate_material), known_figures=source_figures(*gate_material),
            origins=origins, rounds=_lf.FIT_ROUNDS if fit_rounds is None else fit_rounds,
            fit_bullets=llm is not None, jobname=jobname, fill_goal=FILL_GOAL,
            skill_cap=cap, skill_keep=skill_keep)
        if fo.result is not result:
            used, result = fo.profile, fo.result
        # The packer may have drawn a near-duplicate from the pool ("Excel" next to "Microsoft
        # Excel"): the page shows each skill once. Taking terms off a one-line skills line never
        # adds height, so the page stays clean; it is recompiled to show it.
        if isinstance(used.get("skills"), dict):
            from .linefit import clean_skill_block
            tidy = clean_skill_block(used["skills"])
            if tidy != used["skills"]:
                used["skills"] = tidy
                r2 = compile_tex(render_cv(preamble, used, sections, probe=True, headings=headings),
                                 workdir, jobname=f"{jobname}-tidy")
                if r2.ok and r2.pages == 1:
                    result = r2
        return used, result, fo

    if fit_lines and result and result.ok and result.pages == 1:
        pre_fit = _copy.deepcopy(used)
        used, result, fo = fit_once(used, result)
        # A fit rewrite the guards refused never reached the page, so there is nothing for
        # the person to confirm: the bullet simply kept its wording.
        line_fit, line_before, line_gaps = fo.after.as_dict(), fo.before.as_dict(), fo.gaps()

    # RESTORE. The line fit and the resized splits change heights after the room was made, so a
    # cut made for the recency minimums may no longer be needed: put those back, the last cut
    # first, each kept only if the page stays one clean page.
    if result and result.ok and result.pages == 1:
        used, result, trims = _restore_room(preamble, used, result, trims, sections=sections,
                                            headings=headings, workdir=workdir, jobname=jobname)

    # FILL AGAIN. The line fit trims a bullet that spills a word onto a new line, so a page the
    # fill brought to the goal can come out of it short again (a real build went from 95.5% to
    # 87.7%, 2026-11-10). White space is never acceptable: the staircase fill runs once more on
    # the fitted page, from the same material and without asking the model anything twice.
    if (result and result.ok and result.pages == 1 and result.fill_ratio is not None
            and not planning
            and (result.fill_ratio < FILL_GOAL or not staircase_ok(used))):
        result, more, more_notes = fill_page(result)
        added += more
        bullet_notes = list(dict.fromkeys(list(bullet_notes) + list(more_notes)))
        # The bullets this pass added run to the right edge too (Kofi's rule: no stub last
        # line): one more fit pass on the page, then the fill once more if it came out short.
        if more and fit_lines and result and result.ok and result.pages == 1:
            used, result, fo = fit_once(used, result)
            line_fit, line_gaps = fo.after.as_dict(), fo.gaps()
            if result.fill_ratio is not None and result.fill_ratio < FILL_GOAL:
                result, more2, notes2 = fill_page(result)
                added += more2
                bullet_notes = list(dict.fromkeys(list(bullet_notes) + list(notes2)))
    if staircase_ok(used):                   # a step the second pass made needs no note
        bullet_notes = [n for n in bullet_notes if "no more than the older" not in n]

    # THE EDGE POLISH (Kofi, 2026-11-10): one model call per role, every bullet given a
    # character budget from the measured line width (one full line, or two), so no bullet
    # stops short or spills a stub; re-measured, with one targeted retry.
    if planning and result and result.ok and result.pages == 1:
        from . import plan_fill as _pf
        from .fact_gate import known_material, source_figures
        _say("fill", "Planning the page", "every role's bullets, sized to the lines")
        result, planned = _pf.run_plan(
            preamble, used, result, llm=llm, jd_text=jd_text, source=source_material,
            material=material, gate_material=gate_material, sections=sections,
            headings=headings, workdir=workdir, jobname=jobname, origins=origins,
            fill_goal=FILL_GOAL, max_fill=MAX_FILL)
        if planned:
            added = [a for a in added if a.get("section") != "experience"] + planned
        elif result and result.ok and result.pages == 1:
            # The plan could not be written: the bullet-by-bullet fill runs instead, so the page
            # is never left short for it.
            result, more, more_notes = fill_page(result)
            added += more
            bullet_notes = list(dict.fromkeys(list(bullet_notes) + list(more_notes)))
        if callable(getattr(llm, "polish_role", None)) and result and result.ok:
            # Projects keep their own bullets; one that stops short is run to the edge.
            result = _polish_edges(preamble, used, result, llm=llm, jd_text=jd_text,
                                   sections=sections, headings=headings, workdir=workdir,
                                   jobname=jobname, origins=origins,
                                   known=known_material(*gate_material, jd_text),
                                   known_figures=source_figures(*gate_material),
                                   projects_only=bool(planned), tries=3)
    elif (fit_lines and llm is not None and callable(getattr(llm, "polish_role", None))
            and result and result.ok and result.pages == 1):
        from .fact_gate import known_material, source_figures
        pol_known = known_material(*gate_material, jd_text)
        pol_figs = source_figures(*gate_material)

        def polish(res):
            return _polish_edges(preamble, used, res, llm=llm, jd_text=jd_text,
                                 sections=sections, headings=headings, workdir=workdir,
                                 jobname=jobname, origins=origins, known=pol_known,
                                 known_figures=pol_figs)
        result = polish(result)
        # A stub tightened to one line frees a line: it goes to the staircase, and what that
        # adds runs to the edge too.
        if result.fill_ratio is not None and result.fill_ratio < FILL_GOAL:
            result, more, notes3 = fill_page(result)
            added += more
            bullet_notes = list(dict.fromkeys(list(bullet_notes) + list(notes3)))
            if more:
                result = polish(result)

    # The grow, fill and restore passes compile under side jobnames (-full, -fill, -restore).
    # Whatever won must also exist under the REAL jobname, because that is the file the app serves (/api/cv.pdf?job=...);
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
        origins=dict(origins),
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


def page_rule_report(res) -> dict:
    """Kofi's four page rules (2026-11-10), measured on the compiled PDF of a build ``res``:
    every bullet's last line reaches the right-edge target (``linefit.BULLET_LAST_LINE_TARGET``,
    no stub line), every skills line is one line, the page is filled to ``FILL_GOAL``, and the
    bullets step down strictly by recency (``staircase_ok``, the oldest role keeping
    ``OLDEST_FLOOR``). Returns {rule: bool} with the offending items under "details"."""
    from . import linefit as _lf
    rep = _lf.measure(getattr(res, "pdf_path", None), res.profile_used)
    counts = [len(r.get("bullets") or []) for _k, e, r in
              sorted(ranked_roles(res.profile_used), key=lambda x: x[0])]
    short = [b.text for b in rep.short_bullets()]
    out = {
        "edges": bool(rep.items) and not short,
        "skills_one_line": bool(rep.items) and rep.skills_wrapped == 0,
        "fill": bool(res.fill_ratio is not None and res.fill_ratio >= FILL_GOAL),
        "staircase": (staircase_ok(res.profile_used)
                      and (not counts or counts[-1] >= min(OLDEST_FLOOR, counts[0]))),
    }
    out["details"] = {"short_bullets": short, "skills_wrapped": rep.skills_wrapped,
                      "fill": res.fill_ratio, "counts": counts}
    return out
