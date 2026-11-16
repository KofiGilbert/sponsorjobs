"""Inferred support: the job's terms the person's own material clearly implies, though it never
says them word for word (Kofi's Google page, 2026-11-11).

The coverage report counts a job term as supported only when the person's material carries it
literally. "Statistical analysis" is not in Kofi's profile, yet an MBA in Business Analytics,
Data Visualization coursework and years of analytics work clearly show it, and the chat asked
him "if you've really used it, tell me which and where". Tailoring means the job's own wording
goes on the page wherever the person's material backs it (CLAUDE.md section 8), so a term the
material clearly implies is put on the page, in the job's phrasing, without asking.

The model judges (``llm.infer_term_support``); code checks (``vet``):

- a named tool, language, library, framework or product (R, TensorFlow, scikit-learn, Hugging
  Face) is never inferred: the person either named it or did not (``is_named_tool``). Those go
  to the person as ADD chips, where a tap is their own assertion;
- the model's evidence must be a verbatim quote from the person's own material (the same check
  the keyword ADD action makes, tailoring/keyword_action.quote_in);
- a degree, certification or years of experience is never a skill (keywords.is_requirement).
"""

from __future__ import annotations

import re

from .keyword_action import quote_in
from .keywords import is_requirement, profile_text, term_present

MAX_TERMS = 24            # job terms sent to the model in one call
MAX_INFERRED = 6          # terms put on the page from one inference

# Named tools, languages, libraries and platforms a job ad commonly lists in lower or title case,
# so the casing tests below cannot tell them from a method. Each is a thing a person used or did
# not; material can never "imply" one.
_TOOLS = frozenset("""
r python sql java scala julia matlab sas spss stata go rust ruby perl php swift kotlin c c++ c#
excel tableau looker qlik alteryx dbt airflow spark hadoop hive kafka snowflake databricks
bigquery redshift postgres postgresql mysql mongodb oracle pandas numpy scipy keras tensorflow
pytorch scikit-learn sklearn xgboost lightgbm hugging face huggingface langchain llamaindex
openai jupyter git docker kubernetes terraform aws azure gcp vertex sagemaker salesforce sap
jira confluence figma power bi powerbi
""".split()) | {"hugging face", "power bi", "google cloud", "vertex ai", "c++", "c#"}

# Abbreviations that name a field or a method, not a product: these may be inferred like any
# other method term ("ML" from machine learning work, "ETL" from pipeline work).
_METHOD_ACRONYMS = frozenset({"ai", "ml", "nlp", "etl", "elt", "kpi", "kpis", "bi", "llm", "llms",
                              "a/b", "roi", "ux", "ui", "qa", "ci/cd", "api", "apis", "crm"})


def is_named_tool(term: str) -> bool:
    """A tool, language, library or product name, never a method: inferring one would invent a
    tool the person never named. Casing tells most of them (TensorFlow, PyTorch, a short
    all-capitals name like SAS); a list covers the lower-case ones (R, pandas, scikit-learn)."""
    t = " ".join(str(term or "").split())
    if not t:
        return False
    low = t.lower()
    if low in _TOOLS:
        return True
    words = t.split()
    for w in words:
        core = w.strip("()[],.;:")
        if not core:
            continue
        if core.lower() in _METHOD_ACRONYMS:
            continue
        if core.lower() in _TOOLS and len(words) <= 2:
            return True
        if re.search(r"[a-z][A-Z]", core):          # TensorFlow, PyTorch, LangChain, iOS
            return True
        if re.fullmatch(r"[A-Z][A-Z0-9+#.]{1,5}", core) and len(words) == 1:   # SAS, SPSS, AWS
            return True
        if re.search(r"\d", core) and len(words) == 1:                         # GPT4, S3
            return True
    if len(words) == 1 and "-" in t and t == low:     # scikit-learn, sentence-transformers
        return True
    return False


def candidates(terms) -> tuple[list[str], list[str]]:
    """(terms the model may judge, named tools that only the person can add)."""
    ask, tools = [], []
    for t in terms or []:
        t = " ".join(str(t or "").split())
        if not t or is_requirement(t):
            continue
        (tools if is_named_tool(t) else ask).append(t)
    return list(dict.fromkeys(ask))[:MAX_TERMS], list(dict.fromkeys(tools))


def vet(answer, asked: list[str], material: dict, roles=None) -> list[dict]:
    """The model's answer, checked: [{"term", "evidence"}] for each asked term it marked
    supported with a verbatim quote from ``material``, in the order asked. Anything else
    (a term not asked, a named tool, a quote not in the material, a requirement) is dropped."""
    text = profile_text(material or {})
    by_term = {}
    for row in answer if isinstance(answer, list) else []:
        if not isinstance(row, dict) or not row.get("supported"):
            continue
        term = " ".join(str(row.get("term") or "").split())
        quote = " ".join(str(row.get("evidence") or "").split())
        where = [str(x) for x in row.get("roles") or [] if isinstance(x, str)]
        if term and quote:
            by_term.setdefault(term.lower(), (term, quote, where))
    out = []
    for t in asked or []:
        got = by_term.get(t.lower())
        if not got:
            continue
        _term, quote, where = got
        if is_named_tool(t) or is_requirement(t):
            continue
        if not quote_in(quote, text):
            continue
        # The roles where the person did that work: only labels that were offered.
        offered = {str(r).lower(): str(r) for r in roles or []}
        out.append({"term": t, "evidence": quote[:200],
                    "roles": [offered[w.lower()] for w in where if w.lower() in offered][:2]})
        if len(out) >= MAX_INFERRED:
            break
    return out


def literal(terms, material: dict) -> list[str]:
    """The terms the material already says word for word (the coverage report's own test)."""
    text = profile_text(material or {})
    return [t for t in terms or [] if term_present(str(t), text)]
