"""Full-width lines: measure every written line on the compiled resume, and fix the ones
that stop short (the owner's layout rule, 2026-11).

The rule. Every written line on the resume runs to the right edge of the page:

  * SKILLS: 5 to 6 category lines ("Analytics & BI: SQL, Python (pandas, NumPy), Power BI,
    Tableau, advanced Excel, data visualization, dashboard design"), each ONE line whose last
    item ends in the final ~10% of the text width. Never wrapping to a second line, never
    stopping halfway.
  * BULLETS: the number of bullets per role is untouched. Each bullet's LAST line runs close
    to the right edge: a short last line is filled, a bullet that spills a word or two onto
    a new line is trimmed back.
  * Only the person's own material (CLAUDE.md section 8). Nothing here invents a skill or a
    detail: every rewrite passes the fact gate (tailoring/fact_gate.py), the skill guard
    (keywords.introduced_skills) and the padding guard (tailoring/filler.py). When there is
    not enough real material, the caller ASKS the person (``FitOutcome.gaps``), it never pads.
  * One page, no overfull box, and never worse than before: every round is compiled and
    measured, and only a round that keeps the page clean AND scores better is kept.

Measured, not estimated. Character-width estimates (tailoring/textwidth.py) are only good at
the template's own 10pt; the layout ladder (assembler.LAYOUT_STEPS) changes the font size and
margins, so a line sized by estimate can wrap on the real page. ``measure`` reads the glyph
boxes from the compiled PDF with pypdfium2 (a core dependency, the same renderer the previews
use) and reports, for each skills line and each bullet, how many lines it takes and how far
its last glyph reaches toward the right margin.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #

# A skills line is FULL when its last item ends within the final 10% of the text width.
SKILL_LINE_TARGET = 0.90
# A bullet's last line is "close to the right edge" from here on. It replaces the sizer's old
# 45% (one-line) and 58% (last line) acceptance bars (ui/session.py _fit_bullets_to_lines).
BULLET_LAST_LINE_TARGET = 0.85
# A last line holding less than this is a word or two that spilled over: trim it back.
SPILL_TRIM = 0.30
# A line that reaches this is flush with the margin, whatever the target.
FLUSH = 0.985
# The Skills block: 5 to 6 category lines.
SKILL_LINES_MIN = 5
SKILL_LINES_MAX = 6
# Rounds of the fix loop (the owner asked for 3 to 4).
FIT_ROUNDS = 3

# Labels whose line is the person's list as given (credentials, spoken languages): the packer
# never adds to, splits, merges or drops them.
_FIXED_LABEL = re.compile(r"certif|licen|clearance|spoken|award", re.I)

# Progress steps for the resume build, in order. ui/session.py emits these so a later live
# feed can show them; the keys are stable, the labels are what the person reads.
BUILD_STEPS: tuple[tuple[str, str], ...] = (
    ("read", "Reading the job"),
    ("match", "Matching your experience"),
    ("draft", "Drafting"),
    ("bullets", "Crafting bullets"),
    ("fit", "Fitting the page"),
    ("check", "Checking every line"),
)


# --------------------------------------------------------------------------- #
# Reading lines out of the compiled PDF
# --------------------------------------------------------------------------- #

@dataclass
class PdfLine:
    text: str
    x0: float           # left of the first visible glyph
    x1: float           # right of the last visible glyph
    text_left: float    # left of the first letter or digit (after a bullet glyph)
    y: float
    key: str            # letters and digits only, for matching


def _key(text: str) -> str:
    """Letters and digits only, lowercased and accent-folded, so the PDF's text (ligatures,
    hyphenation, LaTeX-escaped symbols) and the profile's text compare equal."""
    t = unicodedata.normalize("NFKD", str(text or "")).lower()
    return re.sub(r"[^a-z0-9]", "", t)


def pdf_lines(pdf_path) -> list[PdfLine]:
    """Every text line on page 1, top to bottom, with the x extent of its glyphs."""
    import pypdfium2 as pdfium

    from .preview import PDFIUM_LOCK

    out: list[PdfLine] = []
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            page = doc[0]
            tp = page.get_textpage()
            n = tp.count_chars()
            cur: list[tuple[str, tuple]] = []

            def flush():
                vis = [(c, b) for c, b in cur if b[2] > b[0] + 0.05 and not c.isspace()]
                if vis:
                    alnum = [b for c, b in vis if c.isalnum()]
                    text = "".join(c for c, _b in cur)
                    out.append(PdfLine(text=text.strip(), x0=vis[0][1][0], x1=vis[-1][1][2],
                                       text_left=(alnum[0][0] if alnum else vis[0][1][0]),
                                       y=vis[0][1][1], key=_key(text)))
                cur.clear()

            last_y = None
            for i in range(n):
                ch = tp.get_text_range(i, 1)
                box = tp.get_charbox(i)
                if ch in ("\r", "\n"):
                    flush()
                    last_y = None
                    continue
                # A new baseline without a line break (rare): start a new line.
                if last_y is not None and box[2] > box[0] and abs(box[1] - last_y) > 4.0 \
                        and not ch.isspace():
                    flush()
                if box[2] > box[0] and not ch.isspace():
                    last_y = box[1]
                cur.append((ch, box))
            flush()
        finally:
            doc.close()
    return out


def _edge(values: list[float], side: str) -> float:
    """The text block's left or right edge: the median of the lines that reach the extreme
    (justified lines and right-aligned dates all end on the margin; a hanging comma or a
    single protruding glyph does not move it)."""
    if not values:
        return 0.0
    ext = max(values) if side == "right" else min(values)
    near = sorted(v for v in values if abs(v - ext) <= 4.0)
    return near[len(near) // 2]


# --------------------------------------------------------------------------- #
# The measurement report
# --------------------------------------------------------------------------- #

@dataclass
class ItemFit:
    kind: str            # "skill" | "bullet"
    label: str           # the skills label, or "Employer, Title" for a bullet
    text: str            # the item's text as the profile holds it
    lines: int = 0       # rendered lines; 0 when the item was not found on the page
    last_fill: float = 0.0   # how far the last line reaches, 0..1 of the usable width
    last_text: str = ""      # the PDF's text of the last line
    last_width: float = 0.0  # its measured width, in PDF points
    slot: int = -1       # bullet slot index (assembler walk order); -1 for a skills line

    @property
    def found(self) -> bool:
        return self.lines > 0

    @property
    def total(self) -> float:
        """Lines as a fraction (2.4 = two full lines and 40% of a third)."""
        return max(0, self.lines - 1) + self.last_fill

    def ok(self) -> bool:
        if not self.found:
            return True
        if self.kind == "skill":
            if _FIXED_LABEL.search(self.label):
                return True          # the person's own list (certifications): left as given
            return self.lines == 1 and self.last_fill >= SKILL_LINE_TARGET
        return self.last_fill >= BULLET_LAST_LINE_TARGET or self.last_fill >= FLUSH

    def as_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "text": self.text, "lines": self.lines,
                "last_fill": round(self.last_fill, 3), "ok": self.ok()}


@dataclass
class LineReport:
    items: list[ItemFit] = field(default_factory=list)
    left: float = 0.0
    right: float = 0.0

    @property
    def width(self) -> float:
        return max(0.0, self.right - self.left)

    @property
    def skills(self) -> list[ItemFit]:
        return [i for i in self.items if i.kind == "skill" and i.found]

    @property
    def bullets(self) -> list[ItemFit]:
        return [i for i in self.items if i.kind == "bullet" and i.found]

    @property
    def skills_full(self) -> int:
        return sum(1 for i in self.skills if i.ok())

    @property
    def skills_wrapped(self) -> int:
        return sum(1 for i in self.skills if i.lines > 1)

    @property
    def bullets_ok(self) -> int:
        return sum(1 for i in self.bullets if i.ok())

    def short_skills(self) -> list[ItemFit]:
        return [i for i in self.skills if not i.ok()]

    def short_bullets(self) -> list[ItemFit]:
        return [i for i in self.bullets if not i.ok()]

    def skill_count_ok(self, fixed: int = 0) -> bool:
        n = len(self.skills) - fixed
        return SKILL_LINES_MIN <= n <= SKILL_LINES_MAX

    def all_ok(self, fixed: int = 0) -> bool:
        return (not self.short_skills() and not self.short_bullets()
                and self.skill_count_ok(fixed))

    def score(self, fixed: int = 0) -> float:
        """Higher is better. A wrapped skills line is the worst defect (it breaks the
        one-line-per-category look); then full skills lines, then bullets that land."""
        s = 2.0 * self.skills_full - 3.0 * self.skills_wrapped + 1.0 * self.bullets_ok
        if self.skill_count_ok(fixed):
            s += 2.0
        # Break ties toward fuller last lines.
        s += 0.01 * sum(min(1.0, i.last_fill) for i in self.items if i.found)
        return s

    def as_dict(self) -> dict:
        return {"skills": [i.as_dict() for i in self.skills],
                "bullets": [i.as_dict() for i in self.bullets],
                "skills_full": self.skills_full, "skills_lines": len(self.skills),
                "bullets_ok": self.bullets_ok, "bullet_count": len(self.bullets),
                "width_pt": round(self.width, 1)}

    def summary(self) -> str:
        """A short plain-text report, one line per skills line and per short bullet."""
        out = [f"Skills: {self.skills_full} of {len(self.skills)} lines full; "
               f"bullets: {self.bullets_ok} of {len(self.bullets)} land near the edge."]
        for i in self.skills:
            out.append(f"  skill  {i.label}: {i.lines} line(s), last at {i.last_fill:.0%}"
                       + ("" if i.ok() else "  <- short" if i.lines == 1 else "  <- wraps"))
        for i in self.short_bullets():
            out.append(f"  bullet {i.label}: {i.lines} line(s), last at {i.last_fill:.0%}"
                       f"  '{i.text[:50]}'")
        return "\n".join(out)


def _bullet_items(profile: dict) -> list[tuple[list, int, str, str, str]]:
    from .assembler import _iter_bullet_slots_with_grounding
    return list(_iter_bullet_slots_with_grounding(profile, with_label=True))


def _skill_items(profile: dict) -> list[tuple[str, str]]:
    sk = profile.get("skills") or {}
    if not isinstance(sk, dict):
        return []
    return [(str(k), str(v)) for k, v in sk.items() if str(v or "").strip()]


def measure(pdf_path, profile: dict, lines: list[PdfLine] | None = None) -> LineReport:
    """Measure every skills line and every bullet of ``profile`` on the compiled page."""
    lines = lines if lines is not None else (pdf_lines(pdf_path) if pdf_path and Path(pdf_path).exists() else [])
    if not lines:
        return LineReport()
    left = _edge([ln.x0 for ln in lines], "left")
    right = _edge([ln.x1 for ln in lines], "right")
    used: set[int] = set()

    def locate(text: str):
        k = _key(text)
        if not k:
            return None
        for i, ln in enumerate(lines):
            if i in used or not ln.key or not k.startswith(ln.key):
                continue
            if len(ln.key) < min(12, len(k)):
                continue
            acc, j = ln.key, i
            while len(acc) < len(k) and j + 1 < len(lines) and (j + 1) not in used \
                    and k.startswith(acc + lines[j + 1].key):
                j += 1
                acc += lines[j].key
            if acc == k:
                used.update(range(i, j + 1))
                return i, j
        return None

    items: list[ItemFit] = []
    for label, value in _skill_items(profile):
        it = ItemFit("skill", label, value)
        hit = locate(f"{label}: {value}")
        if hit:
            i, j = hit
            last = lines[j]
            it.lines = j - i + 1
            it.last_fill = (last.x1 - left) / max(1.0, right - left)
            it.last_text, it.last_width = last.text, last.x1 - (left if j > i else lines[i].x0)
        items.append(it)
    for n, (bl, idx, _g, _section, label) in enumerate(_bullet_items(profile)):
        it = ItemFit("bullet", label, str(bl[idx]), slot=n)
        hit = locate(str(bl[idx]))
        if hit:
            i, j = hit
            tl = lines[i].text_left
            last = lines[j]
            it.lines = j - i + 1
            it.last_fill = (last.x1 - tl) / max(1.0, right - tl)
            it.last_text = re.sub(r"^[^0-9A-Za-z]+", "", last.text)   # no bullet glyph
            it.last_width = last.x1 - (last.x0 if j > i else tl)
        items.append(it)
    return LineReport(items=items, left=left, right=right)


# --------------------------------------------------------------------------- #
# The Skills packer: 5 to 6 one-line categories, each reaching the right margin
# --------------------------------------------------------------------------- #

_TERM_SPLIT = re.compile(r",(?![^()]*\))")


def split_terms(value) -> list[str]:
    """"SQL, Python (pandas, NumPy), Tableau" -> ["SQL", "Python (pandas, NumPy)", "Tableau"]."""
    out = []
    for t in _TERM_SPLIT.split(str(value or "")):
        t = t.strip().strip(",").strip()
        if t:
            out.append(t)
    return out


def _tkey(term: str) -> str:
    return re.sub(r"\s*\(.*\)$", "", str(term)).strip().lower()


def estimate_width(label: str, terms: list[str]) -> float:
    """The template-10pt estimate of a rendered skills line ("**Label:** a, b, c")."""
    from .textwidth import text_width_pt
    return (text_width_pt(f"{label}:", bold=True) + text_width_pt(" ")
            + text_width_pt(", ".join(terms)))


# Skill THEMES. The CV's four broad categories (keywords.SKILL_CATEGORIES) are too coarse to
# keep a full-width line coherent: "Domain Knowledge" is the catch-all, so "Data Modeling"
# would land on a "Leadership & Delivery" line. A theme is decided from the term's own words
# first, then from the broad category. A line's themes come from its LABEL's words ("Analytics
# & BI" is data, "Leadership & Delivery" is people + delivery), else from its terms.
_THEME_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tools", ("microsoft", "google ", "excel", "word", "outlook", "powerpoint", "office",
               "sheets", "salesforce", "hubspot", "quickbooks", "sap", "oracle", "workday",
               "zendesk", "slack", "confluence", "notion", "crm", "erp", "vba", "git",
               "docker", "linux", "windows")),
    ("people", ("stakeholder", "mentor", "leadership", "leading", "coach", "training", "vendor",
                "communication", "presentation", "negotiat", "customer service", "customer success",
                "customer support", "customer onboarding", "customer experience",
                "customer relations", "client", "complaint",
                "people management", "team", "hiring", "recruit", "public speaking",
                "collaboration", "relationship", "facilitat", "teaching", "supervis",
                "coordinat", "onboarding", "event", "fundrais", "volunteer", "community")),
    ("delivery", ("agile", "scrum", "kanban", "project management", "program management",
                  "process improvement", "change management", "requirements", "lean",
                  "six sigma", "jira", "roadmap", "delivery", "prioriti", "sdlc", "waterfall",
                  "pmp", "prince2", "operations management", "quality assurance", "scheduling",
                  "planning", "documentation", "data entry", "report writing")),
    ("business", ("financial", "finance", "budget", "variance", "kpi", "strategy", "strategic",
                  "p&l", "pnl", "profit", "pricing", "valuation", "market", "revenue",
                  "accounting", "audit", "compliance", "risk", "procurement", "sales",
                  "business case", "performance management", "okr", "fraud", "kyc", "aml",
                  "reconciliation", "cash", "banking", "payments", "invoic", "payroll")),
    ("methods", ("statistic", "regression", "hypothesis", "a/b", "experiment", "machine learning",
                 "deep learning", "forecast", "time series", "clustering", "classification",
                 "optimization", "econometric", "modeling", "modelling", "nlp",
                 "natural language", "causal", "predictive", "quantitative", "research")),
    ("data", ("dashboard", "visualization", "visualisation", "etl", "elt", "warehouse",
              "reporting", "power bi", "tableau", "looker", "dbt", "snowflake", "bigquery",
              "redshift", "databricks", "data model", "data pipeline", "analytics", "bi",
              "pandas", "numpy", "spark", "hadoop", "kafka", "airflow", "alteryx", "qlik")),
)
_LABEL_THEMES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("data", ("analytics", "bi", "data", "visualization", "reporting", "insight")),
    ("tools", ("automation", "engineering", "programming", "languages", "tools", "software",
               "platforms", "technical", "cloud", "systems", "computing", "technology")),
    ("methods", ("methods", "statistics", "modeling", "modelling", "research", "quantitative",
                 "techniques", "machine", "ml", "ai", "science", "experimentation")),
    ("people", ("leadership", "people", "communication", "collaboration", "team", "management",
                "interpersonal", "client", "customer")),
    ("delivery", ("delivery", "project", "process", "agile", "operations", "execution")),
    ("business", ("strategy", "performance", "business", "finance", "financial", "domain",
                  "industry", "knowledge", "commercial", "planning")),
)
_CAT_THEME = {"Languages & Tools": "tools", "Data & Analytics": "data",
              "Methods & Frameworks": "methods", "Domain Knowledge": "business"}
# Fresh labels for a new line, per theme.
_THEME_LABELS = {
    "tools": ("Languages & Tools", "Platforms & Tools", "Software & Systems"),
    "data": ("Data & Analytics", "Analytics & BI", "Data Platforms"),
    "methods": ("Methods & Statistics", "Methods & Frameworks", "Techniques"),
    "people": ("Leadership & Collaboration", "People & Communication"),
    "delivery": ("Project Delivery", "Process & Delivery"),
    "business": ("Business & Finance", "Business Operations", "Domain Knowledge"),
}


# Themes close enough to share a line once a line's own theme has nothing left that fits
# ("Strategy & Performance" may take A/B Testing; "Leadership & Delivery" may take Training).
_NEIGHBORS = {"methods": {"business", "data"}, "business": {"methods", "delivery"},
              "data": {"tools", "methods"}, "tools": {"data"}, "people": {"delivery"},
              "delivery": {"people", "business"}}


def skill_theme(term: str) -> str:
    """The theme a skill term belongs to: people, delivery, business, methods, data or tools."""
    from .keywords import classify_skill
    low = " " + re.sub(r"[^a-z0-9/&+#. ]", " ", str(term).lower()) + " "
    for theme, words in _THEME_WORDS:
        for w in words:
            # Short words match whole ("bi", "sap"); longer ones match a word's start
            # ("negotiat" -> negotiation), never its middle.
            pat = (r"(?<![a-z0-9])" + re.escape(w.strip())
                   + (r"(?![a-z0-9])" if len(w.strip()) <= 3 else ""))
            if re.search(pat, low):
                return theme
    return _CAT_THEME.get(classify_skill(str(term)), "business")


def line_themes(label: str, terms: list[str]) -> set[str]:
    """The themes a skills line may draw terms from: its label's words, else its terms."""
    words = set(re.findall(r"[a-z]+", str(label).lower()))
    out = {theme for theme, ws in _LABEL_THEMES if words & set(ws)}
    if out:
        return out
    votes = Counter(skill_theme(t) for t in terms)
    if not votes:
        return {"business"}
    top = max(votes.values())
    return {t for t, n in votes.items() if n == top}


@dataclass
class _Line:
    label: str
    terms: list[str]
    themes: set[str]
    fixed: bool = False
    closed: bool = False


@dataclass
class SkillPack:
    skills: dict                 # {label: "a, b, c"}
    short: list[dict]            # lines still short of the target: {label, themes, fill}
    lines: int                   # non-fixed category lines
    spare: list[str]             # the person's terms that did not fit anywhere

    @property
    def needs_more(self) -> bool:
        """Not enough real material for 5 full lines: the caller should ask the person."""
        return bool(self.short) or self.lines < SKILL_LINES_MIN


def _merged_label(a: str, b: str) -> str:
    head = lambda s: s.split(" & ")[0].strip()
    lab = f"{head(a)} & {head(b)}"
    return lab if len(lab) <= 34 else a


def pack_skills(skills: dict, pool=(), *, width_fn: Callable[[str, list[str]], float] = estimate_width,
                avail: float | None = None, jd_text: str = "", target: float = SKILL_LINE_TARGET,
                min_lines: int = SKILL_LINES_MIN, max_lines: int = SKILL_LINES_MAX,
                allow_new_lines: bool = True) -> SkillPack:
    """Arrange the person's skills into ``min_lines``..``max_lines`` one-line categories, each
    packed as close to the right margin as their real terms allow.

    ``skills`` is the current block; ``pool`` is MORE of the person's own terms (already
    vetted by the caller: stated skills, brain facts, JD terms their material shows). Nothing
    else is ever added. JD terms go first on every line and lines are ordered by how much the
    job asks for them (ATS alignment). A line that would wrap sheds its least relevant terms;
    short lines are filled from terms of their own theme, the emptiest line choosing first
    (best fit: the term that lands closest to the margin); two short lines are merged when
    together they make one full line; a theme with enough spare terms gets a new line until
    there are ``min_lines``. Whatever is still short is reported, so the caller asks the
    person instead of padding. ``width_fn(label, terms)`` measures a line in the same units
    as ``avail`` (the estimate at the template's 10pt by default; tailoring/linefit.fit_lines
    passes one calibrated to the real page).
    """
    from .keywords import term_present
    from .textwidth import TEXTWIDTH_PT
    avail = float(avail if avail is not None else TEXTWIDTH_PT - 2.0)
    goal = target * avail
    jd = str(jd_text or "")
    rel = (lambda t: term_present(_tkey(t) or t, jd)) if jd else (lambda t: False)

    lines: list[_Line] = []
    seen: set[str] = set()
    for label, value in (skills or {}).items():
        terms = []
        for t in split_terms(value):
            if _tkey(t) and _tkey(t) not in seen:
                seen.add(_tkey(t))
                terms.append(t)
        if not terms:
            continue
        fixed = bool(_FIXED_LABEL.search(str(label)))
        lines.append(_Line(str(label), terms, line_themes(str(label), terms), fixed))
    spare: list[tuple[str, str]] = []
    for t in pool or ():
        t = str(t).strip()
        if t and _tkey(t) not in seen:
            seen.add(_tkey(t))
            spare.append((t, skill_theme(t)))
    spare.sort(key=lambda x: 0 if rel(x[0]) else 1)      # JD terms first (stable)

    w = lambda ln, extra=(): width_fn(ln.label, ln.terms + list(extra))
    cat_lines = lambda: [ln for ln in lines if not ln.fixed]

    # 1. JD terms first on every line; a line that wraps sheds its least relevant terms.
    for ln in cat_lines():
        ln.terms.sort(key=lambda t: 0 if rel(t) else 1)
        while len(ln.terms) > 1 and w(ln) > avail:
            t = ln.terms.pop()
            spare.append((t, skill_theme(t)))

    def fill(targets: list[_Line], neighbors: bool = False):
        """Round robin: the emptiest open line takes the best-fitting term of its themes
        (JD terms in a first pass, then the rest), until no line can take another. With
        ``neighbors`` only lines still short of the goal take from a neighbouring theme."""
        for jd_only in (True, False):
            for ln in targets:
                ln.closed = neighbors and w(ln) >= goal
            while True:
                open_ = [ln for ln in targets if not ln.closed]
                if not open_:
                    break
                ln = min(open_, key=lambda x: w(x) / avail)
                ok = set(ln.themes)
                if neighbors:
                    for th in ln.themes:
                        ok |= _NEIGHBORS.get(th, set())
                best_k, best_w = -1, -1.0
                for k, (t, th) in enumerate(spare):
                    if th not in ok or (jd_only and not rel(t)):
                        continue
                    ww = w(ln, [t])
                    if ww <= avail and ww > best_w:
                        best_k, best_w = k, ww
                if best_k < 0:
                    ln.closed = True
                    continue
                ln.terms.append(spare.pop(best_k)[0])

    # 2. Fill every line from its own themes.
    fill(cat_lines())

    # 3. New lines from spare material until there are min_lines, full ones only.
    if allow_new_lines:
        used_labels = {ln.label for ln in lines}
        while len(cat_lines()) < min_lines and spare:
            made = False
            themes = sorted({th for _t, th in spare},
                            key=lambda th: -sum(width_fn("", [t]) for t, x in spare if x == th))
            for th in themes:
                label = next((a for a in _THEME_LABELS.get(th, ()) if a not in used_labels), None)
                if label is None:
                    continue
                trial = _Line(label, [], {th})
                fill([trial])
                if trial.terms and w(trial) >= goal:
                    lines.append(trial)
                    used_labels.add(label)
                    made = True
                    break
                spare.extend((t, th) for t in trial.terms)       # not a full line: put back
            if not made:
                break

    # 3b. A line still short may take a neighbouring theme's terms.
    fill(cat_lines(), neighbors=True)

    # 4. Merge two short lines that make one full line (or when there are too many lines).
    while True:
        short = [ln for ln in cat_lines() if w(ln) < goal]
        best = None
        for i in range(len(short)):
            for j in range(i + 1, len(short)):
                la, lb = short[i], short[j]
                lab = _merged_label(la.label, lb.label)
                ww = width_fn(lab, la.terms + lb.terms)
                if ww <= avail and (ww >= goal or len(cat_lines()) > max_lines):
                    if best is None or ww > best[0]:
                        best = (ww, la, lb, lab)
        if best is None:
            break
        _ww, la, lb, lab = best
        la.label, la.terms, la.themes = lab, la.terms + lb.terms, la.themes | lb.themes
        lines.remove(lb)
        fill([la])

    # 5. Too many lines: the least relevant one goes (its terms stay in the person's material).
    while len(cat_lines()) > max_lines:
        drop = min(reversed(cat_lines()), key=lambda ln: sum(1 for t in ln.terms if rel(t)))
        lines.remove(drop)
        spare.extend((t, skill_theme(t)) for t in drop.terms)
        fill(cat_lines())

    # 6. Order: the lines the job asks most about first, fixed lines (certifications) last.
    order = sorted(range(len(lines)), key=lambda i: (lines[i].fixed,
                                                     -sum(1 for t in lines[i].terms if rel(t)), i))
    lines = [lines[i] for i in order]
    out = {ln.label: ", ".join(ln.terms) for ln in lines if ln.terms}
    short = [{"label": ln.label, "themes": sorted(ln.themes), "fill": round(w(ln) / avail, 3)}
             for ln in cat_lines() if w(ln) < goal]
    return SkillPack(skills=out, short=short, lines=len(cat_lines()), spare=[t for t, _c in spare])


def supported_term(term: str, known: dict | None) -> bool:
    """A skill term may go on the page only when every word of it is in the person's own
    material and it names nothing the fact gate would refuse (no invented tool, credential)."""
    if known is None:
        return True
    from .fact_gate import _SMALL, _words, new_claims
    words = {w for w in _words(term) if w not in _SMALL}
    if words and not words <= known.get("words", set()):
        return False
    return not new_claims(term, known=known)


# --------------------------------------------------------------------------- #
# The fix loop
# --------------------------------------------------------------------------- #

@dataclass
class FitOutcome:
    profile: dict
    result: object                     # CompileResult of the kept version
    before: LineReport
    after: LineReport
    rounds: int = 0
    changes: list = field(default_factory=list)    # {kind, label, before, after}
    rejected: list = field(default_factory=list)   # rewrites a guard refused
    skill_pack: SkillPack | None = None

    @property
    def improved(self) -> bool:
        return self.after.score() > self.before.score()

    def gaps(self) -> dict:
        """What real material is missing for every line to run full: the caller asks the
        person about these, never pads them."""
        fixed = sum(1 for i in self.after.skills if _FIXED_LABEL.search(i.label))
        n = len(self.after.skills) - fixed
        return {
            "skill_lines": n,
            "skills_needed": max(0, SKILL_LINES_MIN - n),
            "short_skills": [{"label": i.label, "fill": round(i.last_fill, 3), "lines": i.lines}
                             for i in self.after.short_skills()
                             if not _FIXED_LABEL.search(i.label)],
            "short_bullets": [{"label": i.label, "text": i.text, "fill": round(i.last_fill, 3),
                               "lines": i.lines} for i in self.after.short_bullets()],
        }


def calibrate(report: LineReport) -> float:
    """PDF points per estimated point (tailoring/textwidth.py at 10pt), from lines whose
    natural width the page shows: single-line skills, and every bullet's last line."""
    from .textwidth import text_width_pt
    ratios = []
    for it in report.items:
        if not it.found or it.last_fill >= FLUSH:
            continue
        if it.kind == "skill" and it.lines == 1:
            est = estimate_width(it.label, split_terms(it.text))
        else:
            est = text_width_pt(it.last_text)
        if est > 40 and it.last_width > 0:
            ratios.append(it.last_width / est)
    if not ratios:
        return report.width / 574.0 if report.width else 1.0
    ratios.sort()
    return ratios[len(ratios) // 2]


def _bullet_targets(it: ItemFit, mode: str) -> tuple[int, int, int]:
    """(goal lines, min chars, max chars) for a bullet, from its measured length."""
    cpl = len(it.text) / max(0.2, it.total)          # characters per full line, as rendered
    if mode == "trim":
        goal = max(1, it.lines - 1)
    else:
        goal = max(1, it.lines)
    lo = int(cpl * (goal - 1 + BULLET_LAST_LINE_TARGET + 0.03))
    hi = int(cpl * (goal - 1 + 0.98))
    return goal, lo, max(lo + 1, hi)


_DANGLING = frozenset("a an and as at by for from in into of on or the to via with "
                      "while including across".split())


def _broken(original: str, new: str) -> bool:
    """A rewrite that trails off ("saving the."), loses the sentence's end mark, or drops
    one of the original's figures."""
    words = re.findall(r"[A-Za-z']+", new)
    if not words or words[-1].lower() in _DANGLING:
        return True
    ends = (".", "!", "?", "%", ")")
    if original.rstrip().endswith(ends) and not new.rstrip().endswith(ends):
        return True
    # Resizing never drops one of the person's numbers ("10 hours a week" is the point).
    from .fact_gate import figures
    return bool(figures(original) - figures(new))


def _propose(llm, text: str, lo: int, hi: int, jd_text: str, grounding: str,
             role_terms: list[str]) -> str:
    fit = getattr(llm, "fit_bullet", None)
    try:
        if callable(fit):
            return str(fit(text, lo, hi, jd_text, grounding) or "").strip()
        if len(text) > hi:
            return str(llm.shorten_bullet(text, hi) or "").strip()
        return str(llm.reword_bullet(text, role_terms, hi, jd_text) or "").strip()
    except Exception:              # noqa: BLE001 - a failed call keeps the bullet as it is
        return ""


def fit_lines(profile: dict, result, *, render: Callable[[dict], str],
              compile_fn: Callable[[str, str], object], is_clean: Callable[[object], bool],
              llm=None, jd_text: str = "", skill_pool=(), known: dict | None = None,
              known_figures: set | None = None, origins: dict | None = None,
              rounds: int = FIT_ROUNDS, fit_bullets: bool = True,
              progress: Callable[[str], None] | None = None,
              jobname: str = "cv") -> FitOutcome:
    """Measure the compiled page and fix the lines that stop short, for up to ``rounds``
    rounds. Each round: repack the Skills block with widths calibrated to the real page, and
    ask the model to fill or trim each short bullet to a measured character window; compile;
    measure; keep the round only if the page is still clean (one page, no overfull box, not
    flush to the bottom edge) and the score went up. Bullets that did not improve go back to
    their previous wording before the round is judged. Never worse than before.

    ``render(profile) -> tex`` and ``compile_fn(tex, jobname) -> CompileResult`` come from the
    assembler (so this module needs no layout knowledge); ``is_clean(result)`` is its page
    check. ``skill_pool`` is more of the person's own skills; each is re-checked here against
    ``known`` (the fact gate's view of their material) before it can reach the page.
    """
    from .fact_gate import gate_rewrite
    from .filler import is_padded
    from .keywords import introduced_skills, supported_skills, term_present

    if progress:
        try:
            progress("check")
        except Exception:          # noqa: BLE001
            pass
    before = measure(getattr(result, "pdf_path", None), profile)
    out = FitOutcome(profile=profile, result=result, before=before, after=before)
    if not before.items or not before.width:
        return out
    pool = [t for t in (skill_pool or ()) if supported_term(str(t), known)]
    supported = supported_skills(jd_text, profile) if jd_text else []

    best_p, best_r, best_rep = profile, result, before
    fixed_n = lambda rep: sum(1 for i in rep.skills if _FIXED_LABEL.search(i.label))
    tried: dict[int, set] = {}

    for rnd in range(max(0, int(rounds))):
        if best_rep.all_ok(fixed_n(best_rep)):
            break
        cand = copy.deepcopy(best_p)
        grow: list[int] = []             # slots whose rewrite is longer (may cost height)
        round_changes: list[dict] = []

        # Skills: repack with calibrated widths.
        pack = None
        if (best_rep.short_skills() or best_rep.skills_wrapped
                or not best_rep.skill_count_ok(fixed_n(best_rep))) and cand.get("skills"):
            k = calibrate(best_rep)
            pack = pack_skills(cand["skills"], pool,
                               width_fn=lambda lab, terms: k * estimate_width(lab, terms),
                               avail=best_rep.width - 1.5, jd_text=jd_text)
            if pack.skills != cand["skills"]:
                round_changes.append({"kind": "skills", "label": "Skills",
                                      "before": dict(cand["skills"]), "after": dict(pack.skills)})
                cand["skills"] = pack.skills
            out.skill_pack = pack

        # Bullets: fill a short last line, trim a one- or two-word spill.
        if fit_bullets and llm is not None:
            slots = _bullet_items(cand)
            for it in best_rep.short_bullets():
                if it.slot < 0 or it.slot >= len(slots):
                    continue
                bl, idx, grounding, section, label = slots[it.slot]
                text = str(bl[idx])
                done = tried.setdefault(it.slot, set())
                spill = it.lines >= 2 and it.last_fill < SPILL_TRIM
                order = ["trim", "fill"] if spill else (["fill", "trim"] if it.lines >= 2 else ["fill"])
                mode = next((m for m in order if (text, m) not in done), None)
                if mode is None:
                    continue
                done.add((text, mode))
                _goal, lo, hi = _bullet_targets(it, mode)
                role_terms = [t for t in supported if term_present(t, grounding)]
                new = _propose(llm, text, lo, hi, jd_text, grounding, role_terms)
                if (not new or new == text or len(new) > hi * 1.12 or len(new) < lo * 0.8
                        or _broken(text, new)):
                    continue
                bad = (introduced_skills(text, new, grounding, jd_text)
                       or (["padding"] if is_padded(text, new) else [])
                       or gate_rewrite(text, new, known=known, known_figures=known_figures))
                if bad:
                    out.rejected.append({"label": label, "bullet": text, "attempted": new,
                                         "claims": list(bad)})
                    continue
                bl[idx] = new
                if len(new) > len(text):
                    grow.append(it.slot)
                round_changes.append({"kind": "bullet", "label": label, "slot": it.slot,
                                      "before": text, "after": new})

        if not round_changes:
            break
        out.rounds = rnd + 1

        def trial(p, tag):
            r = compile_fn(render(p), f"{jobname}-fit{rnd}{tag}")
            if not is_clean(r):
                return r, None
            return r, measure(r.pdf_path, p)

        r, rep = trial(cand, "")
        if rep is None and (grow or pack is not None):
            # The round cost the page (it overflowed): keep only the changes that take no
            # height, the trims, and a skills repack that adds no line.
            safe = copy.deepcopy(best_p)
            s_slots = _bullet_items(safe)
            for ch in round_changes:
                if ch["kind"] == "bullet" and ch["slot"] not in grow:
                    bl, idx = s_slots[ch["slot"]][0], s_slots[ch["slot"]][1]
                    bl[idx] = ch["after"]
            if pack is not None and best_p.get("skills"):
                k = calibrate(best_rep)
                p2 = pack_skills(best_p["skills"], pool,
                                 width_fn=lambda lab, terms: k * estimate_width(lab, terms),
                                 avail=best_rep.width - 1.5, jd_text=jd_text,
                                 allow_new_lines=False,
                                 min_lines=0, max_lines=max(1, len(best_p["skills"])))
                safe["skills"] = p2.skills
            cand = safe
            round_changes = [c for c in round_changes
                             if c["kind"] == "bullet" and c["slot"] not in grow]
            if best_p.get("skills") and safe.get("skills") != best_p.get("skills"):
                round_changes.append({"kind": "skills", "label": "Skills",
                                      "before": dict(best_p["skills"]),
                                      "after": dict(safe["skills"])})
            r, rep = trial(cand, "s")
        if rep is None:
            continue

        # Per bullet: a rewrite that did not land better goes back to its previous wording.
        prev = {i.slot: i for i in best_rep.bullets}
        now = {i.slot: i for i in rep.bullets}
        reverts = []
        for ch in round_changes:
            if ch["kind"] != "bullet":
                continue
            a, b = prev.get(ch["slot"]), now.get(ch["slot"])
            better = b is not None and (b.ok() or (a is not None and b.found and (
                (b.lines < a.lines and b.last_fill >= SPILL_TRIM)
                or (b.lines == a.lines and b.last_fill > a.last_fill + 0.02))))
            if not better:
                reverts.append(ch)
        if reverts:
            slots = _bullet_items(cand)
            for ch in reverts:
                bl, idx = slots[ch["slot"]][0], slots[ch["slot"]][1]
                bl[idx] = ch["before"]
            round_changes = [c for c in round_changes if c not in reverts]
            if not round_changes:
                continue
            r, rep = trial(cand, "m")
            if rep is None:
                continue
        if rep.score(fixed_n(rep)) > best_rep.score(fixed_n(best_rep)):
            best_p, best_r, best_rep = cand, r, rep
            out.changes.extend(round_changes)
            if origins is not None:
                for ch in round_changes:
                    if ch["kind"] == "bullet":
                        origins[ch["after"]] = origins.get(ch["before"], ch["before"])

    out.profile, out.result, out.after = best_p, best_r, best_rep
    return out
