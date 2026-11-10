"""Full-width lines: measure every written line on the compiled resume, and fix the ones
that stop short (the owner's layout rule, 2026-11).

The rule. Every written line on the resume runs to the right edge of the page:

  * SKILLS: 4 to 5 category lines, at most 5 counting a Certifications line ("Analytics & BI: SQL, Python (pandas, NumPy), Power BI,
    Tableau, advanced Excel, data visualization, dashboard design"), each ONE line whose last
    item ends in the final ~10% of the text width. Never wrapping to a second line, never
    stopping halfway.
  * BULLETS: the number of bullets per role is untouched (the assembler's recency budget
    sets it before this runs). Each bullet's LAST line runs close to the right edge: a short
    last line is filled, a bullet that spills a word or two onto a new line is trimmed back.
  * Only the person's own material (CLAUDE.md section 8). Nothing here invents a skill or a
    detail: every rewrite passes the fact gate (tailoring/fact_gate.py), the skill guard
    (keywords.introduced_skills) and the padding guard (tailoring/filler.py). When there is
    not enough real material, the caller ASKS the person (``FitOutcome.gaps``), it never pads.
  * One page, no overfull box, and never worse than before: every round is compiled and
    measured, and only a round that keeps the page clean AND scores better is kept.

Measured, not estimated. Character-width estimates (tailoring/textwidth.py) are only
approximate (the templates differ in font and margins), so a line sized by estimate can wrap
on the real page. ``measure`` reads the glyph boxes from the compiled PDF with pypdfium2 (a
core dependency, the same renderer the previews use) and reports, for each skills line and
each bullet, how many lines it takes and how far its last glyph reaches toward the right
margin.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:                                     # the live activity feed (ui/activity.py): a no-op off a run
    from ui.activity import step as _say
except Exception:                        # noqa: BLE001 - the engine builds without the app too
    def _say(*_a, **_k):
        return None

# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #

# A skills line is FULL when its last item ends within the final 10% of the text width.
SKILL_LINE_TARGET = 0.90
# Lines merged to the cap before the page is measured use this share of the estimated width.
CAP_ESTIMATE_MARGIN = 0.96
# A bullet's last line is "close to the right edge" from here on. It replaces the sizer's old
# 45% (one-line) and 58% (last line) acceptance bars (ui/session.py _fit_bullets_to_lines).
BULLET_LAST_LINE_TARGET = 0.85
# A last line holding less than this is a word or two that spilled over: trim it back.
SPILL_TRIM = 0.30
# A line that reaches this is flush with the margin, whatever the target.
FLUSH = 0.985
# The Skills block: 4 to 5 lines (Kofi, 2026-11-10: "at most 5, the space goes to experience
# bullets"). SKILL_LINES_MAX is the whole block, a Certifications line included; a template's
# manifest may set its own cap (intake.template_manifest.max_skill_lines). SKILL_LINES_MIN is
# the category lines the packer builds toward when the person's material allows.
SKILL_LINES_MIN = 4
SKILL_LINES_MAX = 5
# Rounds of the fix loop (the owner asked for 3 to 4).
FIT_ROUNDS = 3
# One text line as a share of the text block (10pt type, 12pt leading, a letter page): how
# much a trimmed spill takes off the page's fill.
LINE_SHARE = 12.0 / 775.0

# Labels whose line is the person's list as given (credentials, spoken languages): the packer
# never adds to, splits, merges or drops them.
_FIXED_LABEL = re.compile(r"certif|licen|clearance|spoken|award", re.I)

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

    def skill_count_ok(self, fixed: int = 0, cap: int = SKILL_LINES_MAX) -> bool:
        n = len(self.skills)
        return min(SKILL_LINES_MIN, max(1, cap - fixed)) <= n - fixed and n <= cap

    def all_ok(self, fixed: int = 0, cap: int = SKILL_LINES_MAX) -> bool:
        return (not self.short_skills() and not self.short_bullets()
                and self.skill_count_ok(fixed, cap))

    def score(self, fixed: int = 0, cap: int = SKILL_LINES_MAX) -> float:
        """Higher is better. A wrapped skills line is the worst defect (it breaks the
        one-line-per-category look); then full skills lines, then bullets that land."""
        s = 2.0 * self.skills_full - 3.0 * self.skills_wrapped + 1.0 * self.bullets_ok
        if self.skill_count_ok(fixed, cap):
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


# A resize that lengthens a bullet with this many content words its entry's material never says
# (and that make up more than ``filler.PAD_SHARE`` of the bullet) pads it out: "owning the
# function that covers every product line and the data the company uses to track them" on a
# real build (2026-11-10).
PAD_NEW_WORDS = 4


def _pads_out(before: str, after: str, grounding: str, source: str = "") -> bool:
    from .filler import PAD_SHARE, fact_stems
    mine = set(fact_stems(after))
    if not mine:
        return False
    known = set(fact_stems(before)) | set(fact_stems(grounding)) | set(fact_stems(source))
    invented = mine - known
    return len(invented) >= PAD_NEW_WORDS and len(invented) / len(mine) > PAD_SHARE


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
# The Skills packer: 4 to 5 one-line lines, each reaching the right margin
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
               "docker", "linux", "windows", "api", "apis", "rest", "json", "xml", "graphql",
               "soap", "odata", "azure", "aws", "cloud", "devops", "c#", ".net", "x++")),
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
        """Not enough real material for full lines: the caller should ask the person."""
        return bool(self.short) or self.lines < SKILL_LINES_MIN


# A catch-all heading names nothing: two lines merged into it keep the other line's name.
_CATCH_ALL = re.compile(r"^(?:domain knowledge|knowledge|other(?: skills)?|additional skills|"
                        r"skills|general|misc(?:ellaneous)?)$", re.I)


def _merged_label(a: str, b: str) -> str:
    if _CATCH_ALL.match(str(b).strip()):
        return a
    if _CATCH_ALL.match(str(a).strip()):
        return b
    head = lambda s: s.split(" & ")[0].strip()
    lab = f"{head(a)} & {head(b)}"
    return lab if len(lab) <= 34 else a


# A VAGUE skills phrase: several plain words ending in a generic noun, naming no tool, product or
# acronym ("evidence-based recommendations", "revenue and cost analysis"). On a crowded Skills
# block these go before concrete tools (Kofi, 2026-11-10).
_VAGUE_HEADS = frozenset("""analysis analyses analytics recommendations insights insight decisions
    decision-making improvement improvements management planning thinking solutions strategy
    strategies skills communication initiatives optimization excellence alignment
    understanding awareness mindset approach approaches best-practices practices outcomes
    storytelling problem-solving""".split())


def is_vague_skill(term: str) -> bool:
    """True for a generic multi-word phrase on a skills line ("revenue and cost analysis"),
    False for a tool, product, acronym or a single word ("Power BI", "KPI frameworks", "SQL")."""
    raw = re.sub(r"\s*\(.*\)$", "", str(term or "")).strip()
    words = re.findall(r"[A-Za-z][A-Za-z+#.'&/-]*", raw)
    if len(words) < 2 or re.search(r"[0-9+#]", raw):
        return False
    if any(len(w) >= 2 and w.isupper() for w in words):          # KPI, SQL, CRM, ERP
        return False
    if any(any(ch.isupper() for ch in w) for w in words[1:]):     # Power BI, Azure Data Factory
        return False
    if term_families(raw) - {"data"}:
        return False
    return words[-1].lower() in _VAGUE_HEADS


def term_priority(term: str, rel: Callable[[str], bool], keep: set) -> int:
    """How strongly a term holds its place on a crowded Skills block, lower holds longer: a job
    term the person supports (0), a term they ADDed themselves (1, their own assertion), a
    concrete tool or method (2), a vague phrase (3). Ties keep the block's own order."""
    if rel(term):
        return 0
    if _tkey(term) in keep:
        return 1
    return 3 if is_vague_skill(term) else 2


def _theme_gap(a: set, b: set) -> int:
    if a & b:
        return 0
    if any(_NEIGHBORS.get(t, set()) & b for t in a):
        return 1
    return 2


def _merge_two(la: "_Line", lb: "_Line", width_fn, avail: float, prio) -> tuple:
    """Merge two lines into one: the terms in priority order, the lowest shed until it fits."""
    lab = _merged_label(la.label, lb.label)
    terms = sorted(la.terms + lb.terms, key=prio)
    shed: list[str] = []
    while len(terms) > 1 and width_fn(lab, terms) > avail:
        shed.insert(0, terms.pop())
    return lab, terms, shed


def _cap_lines(lines: list, cap: int, width_fn, avail: float, prio) -> list[str]:
    """Merge category lines until at most ``cap`` remain. Each merge is tried in full first (the
    two lines' terms in priority order, the lowest shed to fit, then every job term and ADDed term
    it shed put back on some line, ``_rescue``) and the pair that LOSES the fewest job terms,
    then ADDed terms, then is closest in meaning, then loses the fewest concrete terms is taken;
    vague phrases go first. A catch-all heading ("Domain Knowledge") or a nearly empty line is
    close to anything. Returns the terms the page lost."""
    import copy as _copy
    out: list[str] = []
    while True:
        cat = [k for k, ln in enumerate(lines) if not ln.fixed]
        if len(cat) <= max(1, cap):
            return out
        best = None
        for x in range(len(cat)):
            for y in range(x + 1, len(cat)):
                trial = _copy.deepcopy(lines)
                la, lb = trial[cat[x]], trial[cat[y]]
                lab, terms, shed = _merge_two(la, lb, width_fn, avail, prio)
                gap = _theme_gap(la.themes, lb.themes)
                if (_CATCH_ALL.match(la.label.strip()) or _CATCH_ALL.match(lb.label.strip())
                        or min(width_fn(la.label, la.terms), width_fn(lb.label, lb.terms))
                        < 0.5 * avail):
                    gap = 0
                la.label, la.terms, la.themes = lab, terms, la.themes | lb.themes
                trial.remove(lb)
                must = [t for t in shed if prio(t) <= 1]
                lost = [t for t in shed if prio(t) > 1]
                lost += _rescue(trial, must, width_fn, avail, prio) if must else []
                n = lambda k: sum(1 for t in lost if prio(t) == k)
                cost = (n(0), n(1), gap, n(2), n(3), -width_fn(lab, terms))
                if best is None or cost < best[0]:
                    best = (cost, trial, lost)
        _c, trial, lost = best
        lines[:] = trial
        out += lost


def _rescue(lines: list, terms: list[str], width_fn, avail: float, prio) -> list[str]:
    """Put each must-keep term (a job term or one the person ADDed) back on a line: the line of
    its own kind where it fits, else the line where shedding the fewest lower-priority terms
    makes room. Returns the terms shed to make room, and any term that found no place."""
    shed_all: list[str] = []
    for term in terms:
        cat = [ln for ln in lines if not ln.fixed]
        if not cat:
            shed_all.append(term)
            continue
        theme = skill_theme(term)

        def kin(ln):
            return (-_family_affinity(term, ln.label, ln.terms),
                    0 if theme in ln.themes else (1 if ln.themes & _NEIGHBORS.get(theme, set()) else 2))
        best = None
        for ln in cat:
            new = sorted(ln.terms + [term], key=prio)
            shed: list[str] = []
            while width_fn(ln.label, new) > avail:
                low = [t for t in new if t != term and prio(t) > prio(term)]
                if not low:
                    break
                drop = low[-1]
                new.remove(drop)
                shed.insert(0, drop)
            if width_fn(ln.label, new) > avail:
                continue
            cost = ((sum(1 for t in shed if prio(t) < 3),) + kin(ln)
                    + (len(shed), width_fn(ln.label, ln.terms)))
            if best is None or cost < best[0]:
                best = (cost, ln, new, shed)
        if best is None:
            shed_all.append(term)
            continue
        _c, ln, new, shed = best
        ln.terms = new
        shed_all += shed
    return shed_all


def cap_skill_block(skills: dict, max_lines: int = SKILL_LINES_MAX, *, jd_text: str = "",
                    keep=(), width_fn: Callable[[str, list[str]], float] = estimate_width,
                    avail: float | None = None) -> dict:
    """The Skills block at most ``max_lines`` lines (a Certifications line counts), with nothing
    else changed when it already is. Over the cap, lines closest in meaning merge into one
    heading; what no longer fits goes in priority order (``term_priority``): vague phrases and
    terms the job does not ask for first, never a job term or a term the person ADDed while
    another can go. What goes stays in the person's saved material (the caller keeps it)."""
    if not isinstance(skills, dict) or len(skills) <= max(1, int(max_lines)):
        return skills
    # Merged lines are sized by estimate before the page is measured: a small margin keeps one
    # from wrapping on the real page (the fit loop later packs them to the edge).
    return pack_skills(skills, (), width_fn=width_fn, avail=avail, jd_text=jd_text,
                       min_lines=0, max_lines=max_lines, allow_new_lines=False, keep=keep,
                       tidy=False, merge_margin=CAP_ESTIMATE_MARGIN).skills


def pack_skills(skills: dict, pool=(), *, width_fn: Callable[[str, list[str]], float] = estimate_width,
                avail: float | None = None, jd_text: str = "", target: float = SKILL_LINE_TARGET,
                min_lines: int = SKILL_LINES_MIN, max_lines: int = SKILL_LINES_MAX,
                allow_new_lines: bool = True, keep=(), tidy: bool = True,
                merge_margin: float = 1.0) -> SkillPack:
    """Arrange the person's skills into at least ``min_lines`` one-line categories and at most
    ``max_lines`` lines in all (a Certifications line counts), each packed as close to the right
    margin as their real terms allow.

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

    Over ``max_lines``, the lines closest in meaning merge (``_cap_lines``) and what no longer
    fits goes in ``term_priority`` order: vague phrases and terms the job does not ask for first.
    A job term or a term in ``keep`` (the person ADDed it) that was on the block is put back on
    some line (``_rescue``), so the page's keyword match never falls. ``tidy`` False only caps:
    no filling from the pool and no merging of short lines (a rebuild after the person's edit).
    """
    from .keywords import term_present
    from .textwidth import TEXTWIDTH_PT
    avail = float(avail if avail is not None else TEXTWIDTH_PT - 2.0)
    goal = target * avail
    jd = str(jd_text or "")
    rel = (lambda t: term_present(_tkey(t) or t, jd)) if jd else (lambda t: False)
    keep_keys = {_tkey(k) for k in keep or () if _tkey(k)}
    prio = lambda t: term_priority(t, rel, keep_keys)

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
    spare.sort(key=lambda x: prio(x[0]))                 # JD terms first (stable)

    w = lambda ln, extra=(): width_fn(ln.label, ln.terms + list(extra))
    cat_lines = lambda: [ln for ln in lines if not ln.fixed]
    cap = max(1, int(max_lines) - sum(1 for ln in lines if ln.fixed))     # category lines
    min_lines = min(int(min_lines), cap)
    must = {_tkey(t) for ln in cat_lines() for t in ln.terms if prio(t) <= 1}
    was_on = {_tkey(t) for ln in cat_lines() for t in ln.terms}

    # 1. JD terms first on every line; a line that wraps sheds its least relevant terms.
    for ln in cat_lines():
        ln.terms.sort(key=prio)
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

    def rescue():
        nonlocal spare
        back = [t for t, _th in spare
                if prio(t) <= 1 and (_tkey(t) in must or _tkey(t) in keep_keys)]
        if back:
            spare = [(t, th) for t, th in spare if t not in back]
            spare.extend((t, skill_theme(t)) for t in _rescue(lines, back, width_fn, avail, prio))

    # 1b. Over the cap: the closest lines in meaning merge, shedding vague and non-job terms
    # first (they stay in the person's material); then every job term and ADDed term that was
    # on the block goes back on a line before anything from the pool, so the keyword match never
    # falls. From here on a line is sized with ``merge_margin`` (an estimate before the page is
    # measured): a merged or refilled line never runs to the estimated edge.
    if len(cat_lines()) > cap:
        avail = avail * merge_margin
        spare.extend((t, skill_theme(t)) for t in _cap_lines(lines, cap, width_fn, avail, prio))
        spare.sort(key=lambda x: prio(x[0]))
        rescue()

    # 2. Fill every line from its own themes.
    if tidy:
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
    if tidy:
        fill(cat_lines(), neighbors=True)

    # 4. Merge two short lines that make one full line (or when there are too many lines).
    while tidy:
        short = [ln for ln in cat_lines() if w(ln) < goal]
        best = None
        for i in range(len(short)):
            for j in range(i + 1, len(short)):
                la, lb = short[i], short[j]
                lab = _merged_label(la.label, lb.label)
                ww = width_fn(lab, la.terms + lb.terms)
                if ww <= avail and (ww >= goal or len(cat_lines()) > cap):
                    if best is None or ww > best[0]:
                        best = (ww, la, lb, lab)
        if best is None:
            break
        _ww, la, lb, lab = best
        la.label, la.terms, la.themes = lab, la.terms + lb.terms, la.themes | lb.themes
        lines.remove(lb)
        fill([la])

    # 5. Still too many lines (a merge above can only lower the count): cap again. Then every
    # job term and ADDed term that was on the block goes back on a line.
    if len(cat_lines()) > cap:
        spare.extend((t, skill_theme(t)) for t in _cap_lines(lines, cap, width_fn, avail, prio))
        spare.sort(key=lambda x: prio(x[0]))
        fill(cat_lines())
    rescue()
    # A concrete term the block lost goes back in place of vague phrases, never of anything else.
    if any(prio(t) == 3 for ln in cat_lines() for t in ln.terms):
        swap = [t for t, _th in spare if prio(t) == 2 and _tkey(t) in was_on]
        if swap:
            spare = [(t, th) for t, th in spare if t not in swap]
            spare.extend((t, skill_theme(t)) for t in _rescue(lines, swap, width_fn, avail, prio))

    # 6. Order: the lines the job asks most about first, fixed lines (certifications) last.
    order = sorted(range(len(lines)), key=lambda i: (lines[i].fixed,
                                                     -sum(1 for t in lines[i].terms if rel(t)), i))
    lines = [lines[i] for i in order]
    out = {ln.label: ", ".join(ln.terms) for ln in lines if ln.terms}
    short = [{"label": ln.label, "themes": sorted(ln.themes), "fill": round(w(ln) / avail, 3)}
             for ln in cat_lines() if w(ln) < goal]
    return SkillPack(skills=out, short=short, lines=len(cat_lines()), spare=[t for t, _c in spare])


@dataclass
class TermPlacement:
    skills: dict                 # the block with the term in it
    label: str                   # the line it went on
    shed: list[str]              # terms moved off that line to keep it one line
    already: bool = False        # the block listed it already


# A skills line holds at most this many terms (the assembler's one-page fit cuts past it).
SKILL_LINE_TERMS = 8

# Product families: a term goes with its own kind ("Logic Apps" beside the Azure services, not
# under "Domain Knowledge"; "Power Apps" beside Power BI and Power Automate). A family is named
# by a word the terms share ("azure", "power", "aws"), and these names carry the family word
# they leave out of their own name.
_FAMILY_WORDS = frozenset({
    "azure", "power", "microsoft", "dynamics", "d365", "aws", "amazon", "google", "gcp", "sql",
    "oracle", "sap", "salesforce", "adobe", "atlassian", "python", "java", "javascript",
    "react", "excel", "tableau", "snowflake", "databricks", "apache", "spark", "hadoop",
    "kubernetes", "docker", "git", "linux", "data", "cloud", "fabric", "net"})
_FAMILY_OF = {
    "logic apps": "azure", "dataverse": "power", "copilot studio": "power", "ssrs": "sql",
    "ssis": "sql", "ssas": "sql", "t-sql": "sql", "tsql": "sql", "x++": "dynamics",
    "data management framework": "dynamics", "dmf": "dynamics", "odata": "dynamics",
    "onelake": "fabric", "synapse": "azure", "cosmos db": "azure", "devops": "azure",
    "github": "git", "gitlab": "git", "bitbucket": "git", "c#": "net", "asp.net": "net",
    "vb.net": "net", "entity framework": "net", "pandas": "python", "numpy": "python",
    "django": "python", "flask": "python", "node.js": "javascript", "typescript": "javascript",
    "lambda": "aws", "s3": "aws", "redshift": "aws", "bigquery": "google", "looker": "google",
    "vba": "excel", "power query": "power", "dax": "power", "sharepoint": "microsoft",
    "outlook": "microsoft", "teams": "microsoft"}


def term_families(term: str) -> set[str]:
    """The product families a skill term belongs to ("Azure Data Factory" -> azure, data;
    "Logic Apps" -> azure)."""
    low = _tkey(term)
    words = set(re.findall(r"[a-z0-9+#]+", low.replace(".net", " net ")))
    out = {w for w in words if w in _FAMILY_WORDS}
    for name, fam in _FAMILY_OF.items():
        if re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", low):
            out.add(fam)
    return out


def _family_affinity(term: str, label: str, terms: list[str]) -> int:
    """How strongly a skills line is ``term``'s own kind: its label names one of the term's
    families (3), plus one for each term on it from the same family. "data" alone is too broad
    to decide a line, so it never counts by itself."""
    fam = term_families(term) - {"data"}
    if not fam:
        return 0
    score = 3 if fam & set(re.findall(r"[a-z0-9]+", str(label).lower())) else 0
    return score + sum(1 for t in terms if fam & term_families(t))


def place_term(skills: dict, term: str, *, width_fn: Callable[[str, list[str]], float] = estimate_width,
               avail: float | None = None, jd_text: str = "", keep=()) -> TermPlacement:
    """Put ONE term the person added on the best-fitting Skills line, in its own wording.

    The line is chosen by theme (``skill_theme`` against ``line_themes``: its own theme first,
    then a neighbouring one, then any line), preferring a line where it fits without wrapping.
    The term goes after the line's other job terms (JD terms first, as the packer orders them).
    When the chosen line would wrap or pass ``SKILL_LINE_TERMS``, it sheds its least relevant
    terms from the end (never a job term while another can go); the caller keeps those in the
    person's material. One visible entry per term; certification and language lines are never
    touched. With no category line at all, the term starts a line named for its theme. Terms in
    ``keep`` (skills the person added earlier) are shed last, after every other term."""
    from .keywords import term_present
    from .textwidth import TEXTWIDTH_PT
    from .keywords import is_requirement
    term = " ".join(str(term or "").split())
    if is_requirement(term):
        # A degree, certification or years of experience is never a skill (clean_skill_block).
        return TermPlacement(skills=dict(skills or {}), label="", shed=[], already=False)
    avail = float(avail if avail is not None else TEXTWIDTH_PT - 2.0)
    jd = str(jd_text or "")
    rel = (lambda t: term_present(_tkey(t) or t, jd)) if jd else (lambda t: False)
    skills = {str(k): str(v) for k, v in (skills or {}).items()}
    key = _tkey(term)
    for label, value in skills.items():
        if any(_tkey(t) == key for t in split_terms(value)):
            return TermPlacement(skills=skills, label=label, shed=[], already=True)

    theme = skill_theme(term)
    near = _NEIGHBORS.get(theme, set())
    lines = [(lab, split_terms(v)) for lab, v in skills.items()
             if not _FIXED_LABEL.search(lab) and split_terms(v)]
    if not lines:
        label = _THEME_LABELS.get(theme, ("Skills",))[0]
        out = {label: term}
        out.update(skills)
        return TermPlacement(skills=out, label=label, shed=[])

    def insert(terms: list[str]) -> list[str]:
        at = 0
        for i, t in enumerate(terms):
            if rel(t):
                at = i + 1
        return terms[:at] + [term] + terms[at:]

    def fits(label: str, terms: list[str]) -> bool:
        return len(terms) <= SKILL_LINE_TERMS and width_fn(label, terms) <= avail

    def rank(item):
        label, terms = item
        th = line_themes(label, terms)
        tier = 0 if theme in th else (1 if th & near else 2)
        ok = fits(label, insert(terms))
        # A line of the term's own product family first (Azure with Azure), then its own or a
        # neighbouring theme, then any other line; a line it fits on before one that must shed;
        # emptiest first.
        aff = _family_affinity(term, label, terms)
        kin = aff > 0 or tier < 2
        group = (0 if ok else 2) if kin else (4 if ok else 5)
        return (group, -aff, tier, width_fn(label, terms))

    kept = {_tkey(k) for k in keep or ()}

    def shed_for(label: str, terms: list[str]) -> tuple[list[str], list[str]]:
        new = insert(terms)
        shed: list[str] = []
        while not fits(label, new) and len(new) > 1:
            movable = [t for t in new if t != term]
            free = [t for t in movable if _tkey(t) not in kept] or movable
            drop = next((t for t in reversed(free) if not rel(t)), free[-1])
            new.remove(drop)
            shed.insert(0, drop)
        return new, shed

    # The best line by meaning, unless making room there would push a term the job asks for
    # off the page: then the next line that takes it without losing a job term.
    ranked = sorted(lines, key=rank)
    label, terms = ranked[0]
    new, shed = shed_for(label, terms)
    if any(rel(t) for t in shed):
        for lab, ts in ranked[1:]:
            n2, s2 = shed_for(lab, ts)
            if not any(rel(t) for t in s2):
                label, new, shed = lab, n2, s2
                break
    skills[label] = ", ".join(new)
    return TermPlacement(skills=skills, label=label, shed=shed)


def remove_term(skills: dict, term: str, label: str = "", restore=()) -> dict:
    """Undo ``place_term``: take ``term`` off its line and put the shed terms back on it."""
    key = _tkey(term)
    out = {}
    for lab, value in (skills or {}).items():
        before = split_terms(value)
        terms = [t for t in before if _tkey(t) != key]
        if lab == label or (not label and len(terms) != len(before)):
            have = {_tkey(t) for t in terms}
            terms += [t for t in restore or () if _tkey(t) not in have]
        if terms:
            out[str(lab)] = ", ".join(terms)
    return out


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


# Saved terms a split broke apart (an older extraction cut "CI/CD" in two and ".NET" to
# "NET"): put back together when shown.
_REJOIN = (("ci", "cd", "CI/CD"), ("ui", "ux", "UI/UX"))
_BARE_FIX = {"net": ".NET"}


def clean_skill_block(skills: dict) -> dict:
    """The Skills block as it may show on the page: no requirement on a skills line (a degree,
    certification, licence, clearance or years of experience is not a skill,
    ``keywords.is_requirement``), no near-duplicate ("BI" beside "Power BI", "Excel" beside
    "advanced Excel", ``keywords.dedupe_near_terms``), and terms an old split broke put back
    ("CI" and "CD" as "CI/CD", "NET" as ".NET"). A credentials line (Certifications, Licences)
    keeps its certifications but never a degree. A line left empty goes."""
    from .keywords import dedupe_near_terms, is_requirement
    if not isinstance(skills, dict):
        return skills
    lines = [(str(k), split_terms(v)) for k, v in skills.items()]
    every = {_tkey(t) for _k, terms in lines for t in terms}
    rejoin = {(a, b): whole for a, b, whole in _REJOIN
              if (a in every and b in every) or whole.lower() in every}
    placed: set[str] = set()
    cleaned: list[tuple[str, list[str]]] = []
    for label, terms in lines:
        fixed = bool(_FIXED_LABEL.search(label))
        keep = []
        for t in terms:
            k = _tkey(t)
            if fixed:
                if not re.search(r"(?i)\b(?:bachelor|master|degree|diploma|doctorate|ph\.?d|mba)\b", t):
                    keep.append(t)
                continue
            if is_requirement(t):
                continue
            pair = next(((a, b) for (a, b) in rejoin if k in (a, b)), None)
            if pair:
                if rejoin[pair].lower() not in placed:
                    placed.add(rejoin[pair].lower())
                    keep.append(rejoin[pair])
                continue
            # Only the bare upper-case form is a broken term ("NET"); a word is left alone.
            keep.append(_BARE_FIX[k] if k in _BARE_FIX and t.strip().isupper() else t)
        cleaned.append((label, keep))
    flat = dedupe_near_terms([t for _l, terms in cleaned for t in terms])
    allowed = {}
    for t in flat:
        allowed.setdefault(t.lower(), 0)
        allowed[t.lower()] += 1
    out: dict = {}
    for label, terms in cleaned:
        mine = []
        for t in terms:
            if allowed.get(t.lower(), 0) > 0:
                allowed[t.lower()] -= 1
                mine.append(t)
        if mine:
            out[label] = ", ".join(mine)
    return out


def clean_pool(pool, skills: dict) -> list[str]:
    """The packer's extra skills, as ``clean_skill_block`` would let them show beside the
    block already on the page: no requirement, no broken split ("NET", "CD"), nothing that
    repeats or is the tail of a term already there ("Excel" beside "advanced Excel")."""
    have = [t for v in (skills or {}).values() for t in split_terms(v)]
    pool = [str(t).strip() for t in pool or () if str(t).strip()]
    shown = clean_skill_block({"have": ", ".join(have), "pool": ", ".join(pool)})
    survivors = {_tkey(t) for t in split_terms(shown.get("pool", ""))}
    have_keys = {_tkey(t) for t in have}
    out, seen = [], set()
    for t in pool:
        k = _tkey(t)
        if k in survivors and k not in have_keys and k not in seen:
            seen.add(k)
            out.append(t)
    return out


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
              jobname: str = "cv", trimmed: dict | None = None,
              fill_goal: float | None = None, skill_cap: int = SKILL_LINES_MAX,
              skill_keep=()) -> FitOutcome:
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
    ``trimmed`` ({entry label: [bullet text]}) holds what the one-page fit cut from each entry:
    a resize may not take a cut bullet's point either, so putting it back never prints the
    same thing twice. ``fill_goal``: while the page is shorter than this, a bullet that spills
    a word onto a new line is FILLED to run that line out, never trimmed back (a trim takes a
    line off a page that is already short; white space is never acceptable).
    ``skill_cap``: the most lines the Skills block may hold (a Certifications line counts);
    ``skill_keep``: terms the person ADDed, which a repack never sheds while another can go.
    """
    from .fact_gate import gate_rewrite
    from .filler import borrows_from_sibling, is_padded, repeats_sibling, tacked_on_terms
    from .keywords import introduced_skills, supported_skills, term_present

    before = measure(getattr(result, "pdf_path", None), profile)
    out = FitOutcome(profile=profile, result=result, before=before, after=before)
    if not before.items or not before.width:
        return out
    pool = clean_pool([t for t in (skill_pool or ()) if supported_term(str(t), known)],
                      profile.get("skills") or {})
    supported = supported_skills(jd_text, profile) if jd_text else []

    best_p, best_r, best_rep = profile, result, before
    fixed_n = lambda rep: sum(1 for i in rep.skills if _FIXED_LABEL.search(i.label))

    def short_page() -> bool:
        fill = getattr(best_r, "fill_ratio", None)
        return fill_goal is not None and fill is not None and fill < fill_goal
    tried: dict[int, set] = {}

    for rnd in range(max(0, int(rounds))):
        if best_rep.all_ok(fixed_n(best_rep), skill_cap):
            break
        cand = copy.deepcopy(best_p)
        grow: list[int] = []             # slots whose rewrite is longer (may cost height)
        # A trim takes a line off the page: only as many as keep the page at ``fill_goal``.
        fill_now = getattr(best_r, "fill_ratio", None)
        trims_left = [10 ** 6 if fill_goal is None or fill_now is None
                      else int((fill_now - fill_goal) / LINE_SHARE + 1e-9)]
        round_changes: list[dict] = []

        # Skills: repack with calibrated widths.
        pack = None
        if (best_rep.short_skills() or best_rep.skills_wrapped
                or not best_rep.skill_count_ok(fixed_n(best_rep), skill_cap)) and cand.get("skills"):
            k = calibrate(best_rep)
            pack = pack_skills(cand["skills"], pool,
                               width_fn=lambda lab, terms: k * estimate_width(lab, terms),
                               avail=best_rep.width - 1.5, jd_text=jd_text,
                               max_lines=skill_cap, keep=skill_keep)
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
                if short_page() or (spill and trims_left[0] <= 0):
                    order = ["fill"]             # a short page keeps every line it has
                mode = next((m for m in order if (text, m) not in done), None)
                if mode is None:
                    continue
                done.add((text, mode))
                if mode == "trim":
                    trims_left[0] -= 1
                _goal, lo, hi = _bullet_targets(it, mode)
                role_terms = [t for t in supported if term_present(t, grounding)]
                new = _propose(llm, text, lo, hi, jd_text, grounding, role_terms)
                if (not new or new == text or len(new) > hi * 1.12 or len(new) < lo * 0.8
                        or _broken(text, new)):
                    continue
                siblings = [bl[k] for k in range(len(bl)) if k != idx]
                bad = (introduced_skills(text, new, grounding, jd_text)
                       or (["padding"] if is_padded(text, new) else [])
                       or (["repeats another bullet"] if repeats_sibling(text, new, siblings)
                           else [])
                       or (["takes another bullet's point"]
                           if borrows_from_sibling(text, new, siblings) else [])
                       or (["takes the point of a bullet cut to fit"]
                           if borrows_from_sibling(text, new, (trimmed or {}).get(label) or ())
                           else [])
                       or tacked_on_terms(text, new)
                       or (["padding"] if _pads_out(text, new, grounding,
                                                    (origins or {}).get(text, "")) else [])
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
        n_sk = sum(1 for c in round_changes if c["kind"] == "skills")
        n_b = sum(1 for c in round_changes if c["kind"] == "bullet")
        what = ", ".join(x for x in (("the skills lines" if n_sk else ""),
                                     (f"{n_b} bullet{'s' if n_b != 1 else ''}" if n_b else "")) if x)
        _say("linefit", "Fitting the page", f"filling {what} to the right edge",
             detail=f"round {rnd + 1} of {rounds}")

        def trial(p, tag):
            r = compile_fn(render(p), f"{jobname}-fit{rnd}{tag}")
            if not is_clean(r):
                return r, None
            return r, measure(r.pdf_path, p)

        r, rep = trial(cand, "")
        if rep is None:
            _say("linefit", "Fitting the page", "that ran over one page, trying a smaller change",
                 state="retry", detail=f"round {rnd + 1} of {rounds}")
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
                                 allow_new_lines=False, keep=skill_keep,
                                 min_lines=0,
                                 max_lines=min(skill_cap, max(1, len(best_p["skills"]))))
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
        if rep.score(fixed_n(rep), skill_cap) <= best_rep.score(fixed_n(best_rep), skill_cap):
            _say("linefit", "Fitting the page", "no better than before, keeping the last version",
                 state="retry", detail=f"round {rnd + 1} of {rounds}")
        else:
            _say("linefit", "Fitting the page", "kept: more lines reach the right edge",
                 detail=f"round {rnd + 1} of {rounds}")
            best_p, best_r, best_rep = cand, r, rep
            out.changes.extend(round_changes)
            if origins is not None:
                for ch in round_changes:
                    if ch["kind"] == "bullet":
                        origins[ch["after"]] = origins.get(ch["before"], ch["before"])

    out.profile, out.result, out.after = best_p, best_r, best_rep
    return out
