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

import re
from dataclasses import dataclass, field
from pathlib import Path

from llm.base import LLMBackend
from .compiler import CompileResult, compile_tex
from .keywords import CoverageReport, build_coverage_report, supported_skills
from .fact_gate import flag_new_figures
from .latex_text import escape_latex
from .latex_template import LatexTemplate

DOC_START = r"\begin{document}"
DOC_END = r"\end{document}"

# Tailoring re-emphasises rather than inflates an ALREADY-full bullet (±10%,
# CLAUDE.md §8). But a CV built from a person's terse notes ("Built SQL pipelines")
# would otherwise render as stubby half-line fragments — so a bullet is also allowed
# to grow up to a full template line, giving every bullet a complete, page-width
# sentence. The one-page self-heal loop remains the guardrail against overflow.
LENGTH_BUDGET = 1.10
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
    out = [_heading("Education")]
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
    out = [_heading("Skills"), "\n"]
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
    return (_heading("Projects")
            + _entry_blocks(entries, allow_link=True,
                            bold=bool(profile.get("bold_metrics")))
            + "\\vs\n")


def _experience(profile: dict) -> str:
    entries = profile.get("experience", [])
    if not entries:
        return ""
    return (_heading("Experience")
            + _entry_blocks(entries, bold=bool(profile.get("bold_metrics")))
            + "\\vs\n")


def _extracurricular(profile: dict) -> str:
    items = profile.get("extracurricular", [])
    if not items:
        return ""
    out = [_heading("Extracurricular"), "\n"]
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
        _heading("Additional Information")
        + f"\n\\noindent \\textbf{{Interests:}} {_esc(interests)}\n"
    )


def _summary(profile: dict) -> str:
    """A leading professional-summary paragraph (for templates that open with one)."""
    text = str(profile.get("summary") or "").strip()
    if not text:
        return ""
    body = _esc_bold(text, bool(profile.get("bold_metrics")))
    return _heading("Summary") + f"\n\\noindent {body}\n\\vs\n"


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
MAX_BULLETS_PER_ROLE = 3

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
                        max_bullets: int = MAX_BULLETS_PER_ROLE) -> dict:
    """Keep the roles that argue for THIS job, and cap how much each one says.

    A long career does not shrink gracefully. Rendering every role forces the fitter to
    trim all of them to one line each, which throws away the specific, quantified
    evidence that makes a resume persuasive and leaves a uniform grey list. Choosing a
    few relevant roles and letting them keep up to three bullets is what a person does
    when they tailor by hand.

    Selection is by term overlap with the job description, with the most recent role
    always kept (recruiters read the top of the page first and a missing current role
    reads as a gap). Output stays in the profile's original order, so chronology is never
    rearranged to flatter the score. Nothing is invented or rewritten here -- this only
    decides what is shown, which is the person's own material either way.
    """
    experience = profile.get("experience") or []
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
    if len(flat) <= max_roles:
        selected = {(ei, ri) for ei, ri, _, _ in flat}
    else:
        scored = []
        for ei, ri, role, entry in flat:
            text = " ".join([str(role.get("title", "")), str(entry.get("org", "")),
                             " ".join(map(str, role.get("bullets") or []))])
            overlap = len(jd_terms & _terms(text))
            scored.append((overlap, -ei, -ri, ei, ri))
        scored.sort(reverse=True)
        selected = {(ei, ri) for _, _, _, ei, ri in scored[:max_roles]}
        selected.add((flat[0][0], flat[0][1]))   # the most recent role always survives

    def _trim(role: dict) -> dict:
        role = dict(role)
        bullets = list(role.get("bullets") or [])
        if len(bullets) > max_bullets:
            # Rank by JD overlap but re-emit in the author's order: the bullets a
            # person writes first are usually the ones they want read first.
            ranked = sorted(range(len(bullets)),
                            key=lambda i: len(jd_terms & _terms(bullets[i])),
                            reverse=True)[:max_bullets]
            bullets = [bullets[i] for i in sorted(ranked)]
        role["bullets"] = bullets
        return role

    kept = []
    for ei, entry in enumerate(experience):
        nested = isinstance(entry.get("roles"), list)
        roles = [_trim(role) for ri, role in enumerate(_roles_of(entry)) if (ei, ri) in selected]
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


def render_cv(preamble: str, profile: dict, sections=None, probe: bool = False) -> str:
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
    for name in order:
        renderer = _SECTION_RENDERERS.get(name)
        if renderer:
            body.append(renderer(profile))
    head = f"{preamble}\n{_FILL_PROBE}" if probe else preamble
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


def _iter_bullet_slots_with_grounding(profile: dict):
    """Yield (container_list, index, grounding_text) for every editable bullet.

    ``grounding_text`` is everything the bullet's OWN entry says: the employer/project
    with its title(s), tech line, and sibling bullets. It is the only material a reword of
    that bullet may draw skills from (see ``keywords.introduced_skills``)."""
    from .keywords import profile_text

    for e in profile.get("projects", []) + profile.get("experience", []):
        grounding = profile_text(e)
        roles = e.get("roles") or [e]
        for r in roles:
            bl = r.get("bullets")
            if isinstance(bl, list):
                for i in range(len(bl)):
                    yield bl, i, grounding
    for it in profile.get("extracurricular", []):
        bl = it.get("bullets")
        if isinstance(bl, list):
            grounding = profile_text(it)
            for i in range(len(bl)):
                yield bl, i, grounding


def _reword_smuggled_unsupported(original: str, new: str, jd_text: str,
                                 grounding: str) -> bool:
    """True if a reword claims a skill that the bullet's own role/project never shows.

    ``grounding`` is that entry's text, NOT the whole profile: a skill that is real in one
    part of the person's history (an AI side project) must not migrate onto another (a
    banking role) where it never happened (issue #279). We never surgically strip a word
    (that mangles grammar); the caller discards the whole reword and keeps the real bullet,
    so the CV stays honest and the gap surfaces in the coverage report instead."""
    from .keywords import introduced_skills

    return bool(introduced_skills(original, new, grounding, jd_text))


def _tailor_bullets(profile: dict, jd_text: str, llm: LLMBackend) -> dict:
    """Return a copy of ``profile`` with bullets reworded toward the JD.

    Bullets are plain text here — escaping happens once, at render time (`_esc`),
    so the LLM output must NOT be pre-escaped. Two safety nets apply per bullet:
    (1) a reword that claims a skill its own role/project doesn't show is discarded in
    favor of the person's real bullet (anti-fabrication, §8); (2) a reword that blew past
    the length budget is rewritten shorter via ``shorten_bullet`` (re-emphasis, not
    expansion, §8) — a rewrite, never a mid-sentence cut, so bullets stay complete
    sentences and the fit/shrink stage still handles page-level reduction.
    """
    import copy
    from .keywords import term_present

    tailored = copy.deepcopy(profile)
    # Weave only real supported SKILLS into bullets — never company/location terms.
    supported = supported_skills(jd_text, profile)
    for bl, i, grounding in _iter_bullet_slots_with_grounding(tailored):
        original = str(bl[i])
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
            continue
        # (1) Discard a reword that fabricated a skill for this role — keep the real bullet.
        if _reword_smuggled_unsupported(original, new, jd_text, grounding):
            continue
        # (2) Enforce the per-bullet length ceiling. Only fires on a wild over-expansion
        # past the budget (floor 165 chars); a normal one-line bullet never trips it.
        if len(new) > budget:
            shorter = str(llm.shorten_bullet(new, budget) or "").strip()
            if shorter and _reword_smuggled_unsupported(original, shorter, jd_text,
                                                        grounding):
                continue                 # the shortening fabricated: keep the real bullet
            if shorter:
                new = shorter
        bl[i] = new
    return tailored


def _drop_trailing_bullet(profile: dict) -> bool:
    """Drop the last bullet of the entry that has the most bullets (>1).

    Never truncates text — a dropped bullet is removed whole, so every surviving
    bullet stays a complete sentence. Returns True if something was dropped.
    """
    lists: list[list] = []
    for e in profile.get("projects", []) + profile.get("experience", []):
        for r in (e.get("roles") or [e]):
            if isinstance(r.get("bullets"), list):
                lists.append(r["bullets"])
    for it in profile.get("extracurricular", []):
        if isinstance(it.get("bullets"), list):
            lists.append(it["bullets"])
    # Prefer trimming an entry that still has more than one bullet.
    candidates = [bl for bl in lists if len(bl) > 1]
    if candidates:
        target = max(candidates, key=lambda bl: sum(len(str(b)) for b in bl))
        target.pop()
        return True
    return False


def _all_roles(profile: dict):
    """(role_dict) for every experience/project role, in document order."""
    for e in profile.get("projects", []) + profile.get("experience", []):
        for r in (e.get("roles") or [e]):
            yield r


def _shrink_once(profile: dict) -> bool:
    """Reduce content by ONE step so the CV fits one page, keeping every bullet a
    complete sentence and never character-truncating.

    Least-destructive first. Each call performs exactly one reduction and returns
    True; when nothing remains to reduce it returns False.
    """
    # 1. A multi-sentence bullet: drop its last sentence.
    slots = sorted(_iter_bullet_slots(profile),
                   key=lambda s: len(str(s[0][s[1]])), reverse=True)
    for bl, i in slots:
        sentences = _split_sentences(str(bl[i]))
        if len(sentences) > 1:
            bl[i] = " ".join(sentences[:-1]).strip()
            return True

    # 2. Drop the last bullet of the fullest role that still has more than one.
    if _drop_trailing_bullet(profile):
        return True

    # 3. One-shot: cap every "Courses:" line to at most 6 courses.
    changed = False
    for e in profile.get("education", []):
        items = [c.strip() for c in str(e.get("courses", "")).split(",") if c.strip()]
        if len(items) > 6:
            e["courses"] = ", ".join(items[:6])
            changed = True
    if changed:
        return True

    # 4. One-shot: cap every skills line to at most 8 items.
    skills = profile.get("skills", {})
    changed = False
    for k, v in list(skills.items()):
        items = [x.strip() for x in str(v).split(",") if x.strip()]
        if len(items) > 8:
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
            bl.pop()
            return True

    # 6. Drop a trailing project entry (keep at least one).
    if len(profile.get("projects", [])) > 1:
        profile["projects"].pop()
        return True

    # 7. Skills must keep 4-5 dense categories (hard rule) — drop a line ONLY when there
    # are more than the 4-category floor, so it can go 5 -> 4 but never below 4.
    if len(profile.get("skills", {})) > 4:
        last = list(profile["skills"])[-1]
        del profile["skills"][last]
        return True

    # 8. Drop a trailing education entry if there is more than one.
    if len(profile.get("education", [])) > 1:
        profile["education"].pop()
        return True

    # 9. Drop a remaining single bullet from the last role that has one.
    roles = [r for r in _all_roles(profile)
             if isinstance(r.get("bullets"), list) and r["bullets"]]
    if roles:
        roles[-1]["bullets"].pop()
        return True

    # 10. Last resort: drop a trailing experience entry (keep at least one).
    if len(profile.get("experience", [])) > 1:
        profile["experience"].pop()
        return True

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
) -> AssembleResult:
    """Assemble a one-page CV from ``profile`` in the template's shape.

    Reuses the template's preamble verbatim, fills slots from the profile, tailors
    bullet language to the JD (truthful to the profile), and shrinks content until
    it compiles to a single page. ``sections`` (from the template's manifest) drives
    which sections render and in what order — e.g. summary-first with no extracurricular.
    """
    workdir = Path(workdir)
    preamble = extract_preamble(template_source)
    profile = normalize_profile(profile)  # tolerate loose LLM-drafted shapes
    source_material = profile                # every figure the person actually supplied
    # Pick the roles this JD actually cares about BEFORE tailoring. A full career on one
    # page is not a resume, it is an index: every role gets squeezed to a single line and
    # the strongest evidence ($60M exports, a 250-driver fleet) is the first thing cut.
    # Selecting first means the roles that survive get room to argue.
    profile = select_roles_for_jd(profile, jd_text)
    import copy as _copy
    selected_source = _copy.deepcopy(profile)

    # Escaping happens once, at render time (`_esc`); the LLM returns plain prose.
    used = _tailor_bullets(profile, jd_text, llm) if tailor else profile

    attempts = 0
    result: CompileResult | None = None
    for attempts in range(1, max_shrink + 2):
        tex = render_cv(preamble, used, sections, probe=True)
        result = compile_tex(tex, workdir, jobname=jobname)
        # A fill past MAX_FILL is a real overflow even at "1 page": with no bottom
        # margin the content is flush to (or off) the paper edge — keep shrinking.
        over_bottom = bool(result.fill_ratio and result.fill_ratio > MAX_FILL)
        clean = (result.ok and result.pages == 1
                 and result.overfull_count == 0 and not over_bottom)
        if clean:
            break
        # Keep shrinking while it's over one page; a lone overfull hbox on an
        # otherwise one-page CV is acceptable to ship.
        if result.ok and result.pages == 1 and attempts > 1 and not over_bottom:
            break
        if not _shrink_once(used):
            break

    # Ship without the probe: it has no layout effect, so the measurement above still
    # describes this render exactly (see _FILL_PROBE).
    tex = render_cv(preamble, used, sections)
    cv_text = _rendered_cv_text(used, sections)
    coverage = build_coverage_report(jd_text, cv_text, used)
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
        fact_flags=flag_new_figures(used, source_material) if tailor else [],
        source_profile=selected_source,
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
