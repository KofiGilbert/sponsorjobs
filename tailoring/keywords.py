"""JD keyword extraction and the coverage report (CLAUDE.md §8).

Extraction is rule-based and deterministic on purpose: the coverage report is an
acceptance-tested artifact, so it must not depend on an LLM's whims. We pull the
JD's *meaningful* terms — acronyms, tool/skill tokens, and known multi-word
phrases — then check which the profile genuinely supports and which made it into
the tailored CV, mirroring the JD's own phrasing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

# Generic words that look term-ish but carry no ATS signal.
_STOPWORDS = {
    "and", "the", "for", "with", "you", "your", "our", "will", "have", "has",
    "are", "is", "to", "of", "in", "on", "as", "at", "an", "be", "or", "we",
    "a", "this", "that", "role", "team", "work", "working", "years", "year",
    "experience", "strong", "ability", "including", "such", "using", "use",
    "new", "across", "all", "into", "from", "their", "who", "what", "which",
    "job", "candidate", "candidates", "responsibilities", "requirements",
    "plus", "etc", "e.g", "i.e", "must", "should", "help", "build", "built",
    "developed", "develop", "designed", "design", "led", "lead", "based",
    "various", "existing", "real", "time", "day", "used", "make", "made",
    # Generic role/title filler that isn't an ATS skill.
    "analyst", "engineer", "senior", "developer", "manager", "associate",
    "intern", "looking", "familiarity", "required", "exposure", "scalable",
    # Job-posting boilerplate: headings, logistics, and hype that read like terms
    # (capitalized) but carry no ATS signal -- these were inflating "Missing".
    "company", "remote", "hybrid", "onsite", "on-site", "apply", "about",
    "overview", "qualifications", "benefits", "salary", "compensation", "equal",
    "opportunity", "employer", "eeo", "hiring", "hire", "join", "mission",
    "values", "culture", "growth", "impact", "engineering", "full-time",
    "fulltime", "part-time", "contract", "position", "posting", "location",
    "own", "owns", "drive", "drives", "driving", "partner", "partners",
    "partnering", "deep", "fluency", "track", "deliver", "delivering",
    "translate", "set", "lead", "leading", "own", "ability", "proven",
    # Posting boilerplate that inflated the Amazon case study's coverage report
    # (obs #18: "Missing" listed Please, Site, LLC, Stand, Willing; "Present"
    # listed While, May): politeness, logistics, legal suffixes, physical-duty
    # sentence-starters, benefits vocabulary, months, and US state codes.
    "please", "site", "sites", "locations", "while", "key", "basic", "preferred",
    "recent", "upcoming", "graduates", "graduate", "eligible", "relocation",
    "support", "oversee", "stand", "willing", "lift", "climb", "descend",
    "note", "within", "before", "after", "during", "per", "via", "also",
    "id", "llc", "inc", "ltd", "corp", "co", "usd", "usa", "rsu", "rsus",
    "package", "comprehensive", "excellent", "currently", "master", "masters",
    "bachelor", "bachelors", "degree", "completed", "enrolled",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec",
    "al", "ak", "az", "ar", "ca", "ct", "de", "fl", "ga", "hi", "ia",
    "il", "ks", "ky", "la", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne",
    "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "pa", "ri", "sc",
    "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
}

# Curated multi-word / lowercase skills worth catching even without capitals.
# Kept small and generic; the JD, not this list, drives what gets asked/tailored.
_LEXICON = [
    "machine learning", "risk management", "portfolio theory", "asset pricing",
    "option pricing", "data pipelines", "feature engineering", "time series",
    "quantitative research", "statistical modeling", "predictive modeling",
    "profit and loss", "fixed income", "credit risk", "market risk",
    "deep learning", "natural language processing", "data science",
    "software development", "unit testing", "continuous integration",
    "rest api", "cloud infrastructure", "back testing", "backtesting",
]

# Acronyms / capitalized tool tokens: Python, SQL, ETL, PnL, AWS, C++, .NET
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]*")


def _norm(term: str) -> str:
    """Normalize a term for comparison: lowercase, collapse whitespace."""
    return re.sub(r"\s+", " ", term.strip().lower())


def extract_jd_terms(jd_text: str) -> list[str]:
    """Return an ordered, de-duplicated list of meaningful JD terms.

    Order is first-appearance in the JD so downstream output is stable.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        n = _norm(term)
        if n and n not in seen:
            seen.add(n)
            found.append(term.strip())

    lower = jd_text.lower()

    # 1) Curated multi-word phrases (record them in the JD's own casing region).
    phrase_words: set[str] = set()  # individual words of captured phrases
    for phrase in _LEXICON:
        idx = lower.find(phrase)
        if idx != -1:
            add(jd_text[idx : idx + len(phrase)])
            phrase_words.update(phrase.split())

    # 2) Single tokens that read like a skill/tool/acronym.
    for m in _TOKEN.finditer(jd_text):
        # Keep internal punctuation (Node.js, C++, .NET) but drop sentence
        # punctuation clinging to the ends (SQL. -> SQL, backtesting, -> ...).
        tok = m.group(0).strip(".,;:!?")
        if not tok:
            continue
        low = tok.lower()
        if low in _STOPWORDS or _norm(tok) in seen:
            continue
        # Skip a bare word already covered by a captured multi-word phrase
        # (e.g. "REST"/"API" when "REST API" was captured).
        if low in phrase_words:
            continue
        is_acronym = tok.isupper() and 2 <= len(tok) <= 6
        has_inner_caps = any(c.isupper() for c in tok[1:])  # PnL, JavaScript
        has_symbol = any(c in "+#." for c in tok)  # C++, C#, .NET, Node.js
        is_capitalized = tok[0].isupper() and len(tok) >= 3
        # Sentence-capital demotion: a capitalized word whose lowercase form ALSO
        # appears in this JD is an ordinary word wearing sentence punctuation
        # ("Support, mentor and motivate…", "…support your team"), not a proper
        # term. Real names and tools (Python, Amazon, Tableau) never show up
        # lowercase in the same posting. (obs #18)
        if (is_capitalized and not (is_acronym or has_inner_caps or has_symbol)
                and re.search(r"(?<![A-Za-z0-9])" + re.escape(low) + r"(?![A-Za-z0-9])",
                              jd_text)):
            continue
        if is_acronym or has_inner_caps or has_symbol or is_capitalized:
            add(tok)

    return found


# Curated, display-cased skill vocabularies so the Skills section carries real,
# specific, JD-relevant skills grouped like the template ("Computing:" /
# "Knowledge:") — and never company names, locations, or role-title filler.
_COMPUTING = [
    "Python", "SQL", "Java", "JavaScript", "TypeScript", "C++", "C#", "Scala",
    "Go", "Rust", "R", "MATLAB", "AWS", "Azure", "GCP", "Docker", "Kubernetes",
    "Spark", "Hadoop", "Kafka", "Airflow", "Snowflake", "Databricks", "Tableau",
    "Power BI", "Looker", "Excel", "Git", "GitHub", "GitLab", "Linux", "Unix",
    "Bash", "MongoDB", "PostgreSQL", "MySQL", "Redis", "Pandas", "NumPy",
    "PyTorch", "TensorFlow", "scikit-learn", "Hive", "REST API", "ETL",
    "Node.js", "React", "Django", "Flask", "FastAPI", "Alteryx", "SAS",
]
_KNOWLEDGE = [
    "Machine Learning", "Deep Learning", "Natural Language Processing",
    "Statistical Modeling", "Predictive Modeling", "Time Series", "Data Science",
    "Feature Engineering", "Data Pipelines", "Backtesting", "Statistics",
    "Econometrics", "Experimentation", "A/B Testing", "Regression",
    "Optimization", "Risk Management", "Portfolio Theory", "Asset Pricing",
    "Option Pricing", "Credit Risk", "Market Risk", "Financial Modeling",
    "Derivatives", "Quantitative Research", "Dashboards", "Data Visualization",
    "Forecasting", "Hypothesis Testing", "Clustering", "Classification",
    # Credit, lending and banking. The vocabulary was ~83 terms and almost entirely
    # tech, so a banking JD naming "credit analysis, underwriting, loan portfolio,
    # covenant compliance, credit memos" matched exactly TWO of them. _fill_skills then
    # had an empty pool, packed nothing, and shipped three-word skill lines: the packer
    # was never broken, it had nothing to pack with. §8 wants the JD's own phrasing on
    # the page, which is impossible for a vocabulary that can't see the JD's words.
    "Credit Analysis", "Underwriting", "Commercial Lending", "Loan Portfolio",
    "Portfolio Monitoring", "Covenant Compliance", "Credit Memos", "Credit Scoring",
    "Financial Statement Analysis", "Cash Flow Analysis", "Debt Structuring",
    "Loan Origination", "Loan Servicing", "Collateral Analysis", "Risk Assessment",
    "Risk Rating", "Regulatory Compliance", "Banking Regulations", "AML", "KYC",
    "Basel III", "Stress Testing", "Liquidity Risk", "Operational Risk",
    "Capital Markets", "Fixed Income", "Equity Research", "Valuation", "DCF",
    "LBO", "M&A", "Due Diligence", "Deal Structuring", "Treasury", "Securitization",
    "Wealth Management", "Asset Management", "Private Equity", "Venture Capital",
    "Investment Banking", "Corporate Finance", "Budgeting", "Variance Analysis",
    "Financial Reporting", "GAAP", "IFRS", "Auditing", "Internal Controls",
    "Accounts Receivable", "Accounts Payable", "Reconciliation", "PnL",
    # Consulting, strategy and general business. Same reason: our two newest templates
    # target these people and the vocabulary could not read their JDs at all.
    "Market Sizing", "Market Entry", "Operating Model", "Business Case",
    "Cost Reduction", "Process Improvement", "Change Management", "Stakeholder Management",
    "Requirements Gathering", "Business Analysis", "Strategy Development",
    "Competitive Analysis", "Go-to-Market", "Pricing Strategy", "Supply Chain",
    "Procurement", "Vendor Management", "Project Management", "Agile", "Scrum",
    "Kanban", "Lean", "Six Sigma", "PMP", "Roadmapping", "Product Strategy",
    "Product Management", "User Research", "Customer Segmentation", "KPI Development",
    "Business Intelligence", "Reporting", "Data Governance", "Data Quality",
    "Cross-functional Collaboration", "Executive Presentation", "Client Management",
    "Post-Merger Integration", "Transformation", "Benchmarking", "Root Cause Analysis",
]
_SKILL_DISPLAY = {s.lower(): s for s in (_COMPUTING + _KNOWLEDGE)}
# The vocabulary walked longest-first (so "Machine Learning" wins over "Learning"), each
# entry with its whole-token pattern precompiled. Built ONCE at import: skill_terms /
# vocab_skills run several times per bullet during a tailoring run, and re-sorting and
# re-compiling 170+ patterns on every call was a measurable cost on the request thread.
_SKILL_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    # A skill still counts when sentence punctuation follows it ("Tableau." / "SQL,"): the
    # lookahead only guards against a LONGER token (Node.js vs Node, C++ vs C), so a "." is
    # excluded only when a letter follows it.
    (s, re.compile(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9+#]|\.[a-z])"))
    for s in sorted(_SKILL_DISPLAY, key=len, reverse=True))
_COMPUTING_SET = {s.lower() for s in _COMPUTING}

# The four categories the CV actually shows. These are the SAME labels the real model is
# asked for in llm/anthropic_client.py, so a profile grouped here and one grouped by the
# model agree instead of producing rival rows ("Computing" next to "Languages & Tools").
#
# Two labels could never satisfy the 4-5 category rule (memory
# skills-five-categories-fill-page): classify_skill only answered Computing or Knowledge,
# so every deterministic path was capped at two skinny lines no matter how much the
# person's material supported.
LANGUAGES = "Languages & Tools"
DATA = "Data & Analytics"
METHODS = "Methods & Frameworks"
DOMAIN = "Domain Knowledge"
SKILL_CATEGORIES = (LANGUAGES, DATA, METHODS, DOMAIN)

_DATA_SET = {s.lower() for s in (
    "AWS", "Azure", "GCP", "Docker", "Kubernetes", "Spark", "Hadoop", "Kafka",
    "Airflow", "Snowflake", "Databricks", "Tableau", "Power BI", "Looker",
    "MongoDB", "PostgreSQL", "MySQL", "Redis", "Hive", "ETL", "REST API",
    "Alteryx", "Pandas", "NumPy", "Dashboards", "Data Visualization",
    "Data Pipelines",
)}
_METHODS_SET = {s.lower() for s in (
    "Machine Learning", "Deep Learning", "Natural Language Processing",
    "Statistical Modeling", "Predictive Modeling", "Time Series", "Data Science",
    "Feature Engineering", "Statistics", "Econometrics", "Experimentation",
    "A/B Testing", "Regression", "Optimization", "Forecasting",
    "Hypothesis Testing", "Clustering", "Classification", "Quantitative Research",
    "Backtesting", "PyTorch", "TensorFlow", "scikit-learn",
    # Ways of working and analysing, as opposed to the industry they're applied in.
    "Valuation", "DCF", "LBO", "Due Diligence", "Stress Testing", "Credit Analysis",
    "Financial Statement Analysis", "Cash Flow Analysis", "Financial Modeling",
    "Market Sizing", "Business Case", "Competitive Analysis", "Benchmarking",
    "Root Cause Analysis", "Variance Analysis", "Customer Segmentation",
    "User Research", "Agile", "Scrum", "Kanban", "Lean", "Six Sigma",
    "Process Improvement", "Change Management", "Project Management",
    "Requirements Gathering", "Business Analysis", "Reconciliation", "Auditing",
    "Risk Assessment", "Underwriting",
)}
_DOMAIN_SET = {s.lower() for s in (
    "Risk Management", "Portfolio Theory", "Asset Pricing", "Option Pricing",
    "Credit Risk", "Market Risk", "Financial Modeling", "Derivatives",
)}


# Uppercase-and-short is not the same as "a skill". The heuristic below was pulling
# timezones, clients and stray initialisms out of postings and calling them skills the
# person LACKS: a real Clipster posting produced "UFC" (a client they ran campaigns for),
# "UTC+2" and "PM" (from "10am to 2pm UTC+2"). Those then reached the coverage report as
# "JD terms your profile doesn't show yet", which is nonsense advice, and the CV engine
# treats a JD term as something to align to. Cheap to exclude, and the cost of a miss here
# is one absent term versus telling someone their résumé is short of "UFC".
_NOT_SKILLS = {
    # time and place
    "UTC", "GMT", "EST", "PST", "CST", "MST", "CET", "EDT", "PDT", "BST", "AM", "PM",
    "US", "USA", "UK", "EU", "UAE", "NYC", "SF", "LA", "DC", "APAC", "EMEA", "LATAM",
    # employment boilerplate
    "FTE", "PTO", "OTE", "EOE", "HR", "IT", "CEO", "CTO", "CFO", "COO", "VP", "SVP",
    "EVP", "MD", "PHD", "BS", "BA", "MS", "MBA", "FAQ", "TBD", "ASAP", "AKA", "ETC",
    "OK", "NO", "YES", "NEW", "TOP", "ALL", "AND", "THE", "FOR", "YOU", "WE", "OUR",
    # money and legal boilerplate that is not a skill
    "401K", "401", "PLC", "LLC", "INC", "LTD", "IPO", "NDA", "SLA", "ROI", "KPI",
}


def _is_acronym_or_symbol(tok: str) -> bool:
    """A token that reads as a genuine tool/skill acronym (SQL, AWS, C++, C#).

    Excludes the stoplist above: a timezone or a client name is uppercase and short too,
    and calling it a skill the person lacks is worse than missing a real one.
    """
    up = tok.upper().strip()
    if up in _NOT_SKILLS:
        return False
    # "UTC+2", "10AM+" and friends: a symbol only counts when it hangs off a real token
    # (C++, C#, .NET), never off digits.
    if any(c in "+#" for c in tok):
        return not any(ch.isdigit() for ch in tok) and up.rstrip("+#") not in _NOT_SKILLS
    return tok.isupper() and 2 <= len(tok) <= 6


def classify_skill(term: str) -> str:
    """Which of the CV's four skill categories ``term`` belongs to.

    Checked most-specific first: a term in several vocabularies (ETL is a tool AND a
    method) lands where a reader expects to find it.
    """
    low = term.lower()
    if low in _DOMAIN_SET:
        return DOMAIN
    if low in _DATA_SET:
        return DATA
    if low in _METHODS_SET:
        return METHODS
    if low in _COMPUTING_SET or _is_acronym_or_symbol(term):
        return LANGUAGES
    # An unrecognised phrase is domain knowledge far more often than a language: the
    # vocabularies above already name the tools we know of.
    return DOMAIN


def skill_terms(jd_text: str) -> list[str]:
    """Real, JD-relevant skills only — matched from the curated vocabularies plus
    genuine acronym/symbol tokens. Excludes company names, locations, and generic
    role words (the source of filler like 'Scientist, Acme, Chicago').

    The result is cached per JD text (the same JD is walked for every bullet of a
    tailoring run, twice in the assembler and up to three times in the line-fitting
    stage); callers get a fresh list each time, so mutating it cannot poison the cache."""
    return list(_skill_terms_cached(jd_text))


@lru_cache(maxsize=32)
def _skill_terms_cached(jd_text: str) -> tuple[str, ...]:
    low = jd_text.lower()
    out: list[str] = []
    seen: set[str] = set()
    for s, pat in _SKILL_PATTERNS:
        if pat.search(low):
            disp = _SKILL_DISPLAY[s]
            if disp.lower() not in seen:
                seen.add(disp.lower())
                out.append(disp)
    for t in extract_jd_terms(jd_text):
        if _is_acronym_or_symbol(t) and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return tuple(out)


# The sections a profile is made of. Everything else a model returns alongside them (one import
# came back with "notes_for_candidate": {"gaps_vs_jd": ["CBAP certification: Not currently held"]})
# is commentary, not the person's material: it must never be rendered, and never count as
# evidence that they have a skill. Shared by the import coercion and the evidence text below.
PROFILE_SECTIONS: frozenset = frozenset({
    "identity", "summary", "education", "experience", "projects", "skills",
    "extracurricular", "interests", "certifications", "languages", "links", "bold_metrics",
})


def profile_text(profile: dict) -> str:
    """Flatten the person's own material into one searchable text blob. Only the known
    profile sections count (PROFILE_SECTIONS); a model's side notes are not evidence."""
    parts: list[str] = []
    if isinstance(profile, dict):
        profile = {k: v for k, v in profile.items() if k in PROFILE_SECTIONS}

    def walk(v):
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif v is not None:
            parts.append(str(v))

    walk(profile)
    return "\n".join(parts)


def term_present(term: str, haystack: str) -> bool:
    """Whole-token, case-insensitive membership test for a term in text."""
    n = _norm(term)
    h = _norm(haystack)
    # Word-boundary match so "R" doesn't match "Research" and "ETL" doesn't
    # match "metals". Symbols like + and # are escaped literally.
    pattern = r"(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Za-z0-9])"
    return re.search(pattern, h) is not None


@dataclass
class CoverageReport:
    """Which JD terms are present in the CV, and which gaps remain."""

    present: list[str] = field(default_factory=list)      # in JD and in CV
    missing_supported: list[str] = field(default_factory=list)  # profile has it, CV doesn't
    missing_unsupported: list[str] = field(default_factory=list)  # JD wants, profile lacks

    @property
    def jd_terms(self) -> list[str]:
        return self.present + self.missing_supported + self.missing_unsupported

    @property
    def coverage_ratio(self) -> float:
        total = len(self.jd_terms)
        return (len(self.present) / total) if total else 1.0

    def to_dict(self) -> dict:
        return {
            "present": self.present,
            "missing_supported": self.missing_supported,
            "missing_unsupported": self.missing_unsupported,
            "coverage_ratio": round(self.coverage_ratio, 3),
        }

    def render(self) -> str:
        lines = ["=== JD Keyword Coverage Report ==="]
        lines.append(f"Present in resume ({len(self.present)}): "
                     + (", ".join(self.present) or "—"))
        lines.append(
            f"Supported by profile but not yet in resume "
            f"({len(self.missing_supported)}): "
            + (", ".join(self.missing_supported) or "—"))
        lines.append(
            f"Wanted by JD but not in your profile "
            f"({len(self.missing_unsupported)}): "
            + (", ".join(self.missing_unsupported) or "—"))
        lines.append(f"Coverage: {self.coverage_ratio:.0%}")
        return "\n".join(lines)


def build_coverage_report(
    jd_text: str, cv_text: str, profile: dict, terms: list[str] | None = None
) -> CoverageReport:
    """Compare JD skill terms against the tailored CV and the source profile. ``terms`` is the
    skill list to measure (the model-read list from llm.extract_jd_skills, or the curated
    skill_terms); the old capitalized-word walk is not used here any more, since it reported
    sentence words ("These", "Responsible") as skills the person lacks."""
    terms = list(terms) if terms is not None else skill_terms(jd_text)
    prof = profile_text(profile)
    report = CoverageReport()
    for t in terms:
        in_cv = term_present(t, cv_text)
        in_profile = term_present(t, prof)
        if in_cv:
            report.present.append(t)
        elif in_profile:
            report.missing_supported.append(t)
        else:
            report.missing_unsupported.append(t)
    return report


def split_required_optional(jd_text: str) -> tuple[str, str]:
    """Split a JD into (required_context, optional_context) at the first 'nice to have' /
    'preferred' / 'bonus' style heading. A rough, HONEST heuristic for the extension's
    Required-vs-Optional skill highlight (P6): text before the marker is treated as required,
    text from the marker on as optional. No marker means everything is required context. Not an
    exact parse of any one JD's structure, just a reasonable, labeled split."""
    m = re.search(
        r"\b(nice[\s-]?to[\s-]?haves?|preferred(?:\s+(?:qualifications|skills))?|bonus(?:\s+points)?"
        r"|(?:it'?s\s+)?a\s+plus|pluses|desirable|good\s+to\s+have|would\s+be\s+(?:a\s+plus|great)"
        r"|optional(?:\s+skills)?)\b",
        jd_text.lower())
    if not m:
        return jd_text, ""
    return jd_text[:m.start()], jd_text[m.start():]


def supported_terms(jd_text: str, profile: dict) -> list[str]:
    """JD terms the profile genuinely supports — safe to weave into the CV."""
    prof = profile_text(profile)
    return [t for t in extract_jd_terms(jd_text) if term_present(t, prof)]


def vocab_skills(text: str) -> list[str]:
    """Skills from the curated vocabularies that ``text`` mentions (display form).

    Unlike ``skill_terms`` this skips the acronym heuristic, so it is safe to run on a
    single résumé bullet: it only reports things that are unambiguously a skill, tool,
    or field (e.g. "AI", "Machine Learning", "Kafka"), never an arbitrary capitalised word.
    """
    low = text.lower()
    out: list[str] = []
    for s, pat in _SKILL_PATTERNS:
        if pat.search(low):
            disp = _SKILL_DISPLAY[s]
            if disp not in out:
                out.append(disp)
    return out


def introduced_skills(original: str, new: str, grounding: str,
                      jd_text: str = "") -> list[str]:
    """Skills a rewrite ``new`` claims that neither ``original`` nor ``grounding`` shows.

    ``grounding`` is the material the bullet may honestly draw on: the bullet's OWN role
    or project (title, org, sibling bullets, tech line), never the whole profile. A skill
    that is real somewhere else in the person's history (an AI side project) must not be
    moved onto a role where it never happened (a banking job): that is fabrication even
    though the person "has" the skill (CLAUDE.md §8; issue #279).

    Checks every curated-vocabulary skill in the rewrite, plus the JD's own skill terms
    (which include acronyms the vocabulary may not list).
    """
    candidates = list(dict.fromkeys(vocab_skills(new) + (skill_terms(jd_text) if jd_text else [])))
    return [t for t in candidates
            if term_present(t, new) and not _grounded(t, original)
            and not _grounded(t, grounding)]


_STEM_SUFFIXES = r"(?:ations?|ments?|ings?|ers?|ed|es|s)"
_STEM_SUFFIX = re.compile(_STEM_SUFFIXES + "$")


def _grounded(term: str, text: str) -> bool:
    """``term`` appears in ``text``, allowing ordinary word-form changes.

    "Reporting" is grounded by "reports", "Forecasting" by "forecast", "Optimization" by
    "optimized". The stem must make up a WHOLE word of ``text`` save for one of the
    inflection suffixes above: an unanchored prefix would let "excellent" ground "Excel",
    "accountable" ground "Accounting", or "testimony" ground "Testing", which is exactly
    the fabrication this check exists to catch. Short tokens and acronyms (AI, LLM, SQL)
    must match exactly: they are the claims that matter most and stemming them would only
    create false matches."""
    if term_present(term, text):
        return True
    # A tool or technology name never inflects: "Docker" is not evidenced by "docked", nor
    # "Spark" by "sparkling". Only activity words (Reporting, Forecasting, Optimization) may
    # be grounded through their ordinary word forms.
    if term.lower() in _COMPUTING_SET:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z0-9+#.]*", term)
    if not words or any(len(w) < 5 or w.isupper() for w in words):
        return False
    hay = text.lower()
    for w in words:
        stem = _STEM_SUFFIX.sub("", w.lower())
        if len(stem) < 4:
            return False
        pat = (r"(?<![a-z0-9])" + re.escape(stem) + _STEM_SUFFIXES + r"?(?![a-z0-9])")
        if not re.search(pat, hay):
            return False
    return True


def supported_skills(jd_text: str, profile: dict) -> list[str]:
    """Real, JD-relevant SKILLS the profile supports — for weaving into bullets
    without dragging in company names or locations."""
    prof = profile_text(profile)
    return [t for t in skill_terms(jd_text) if term_present(t, prof)]
