"""Home's Today strip ranks by fit to the person (Kofi, 2026-11-10: a BI and analytics profile was
shown MongoDB Site Reliability Engineer roles first, because the strip ranked only on sponsorship,
a US location and newness). ui/home_fit.py; with no profile the strip says "new sponsor roles"."""
from __future__ import annotations

from pathlib import Path

from ui.home_fit import fit_targets, rank

ROOT = Path(__file__).resolve().parents[1]
BI = {"experience": [
    {"org": "BlackOrigin", "roles": [{"title": "AI Senior Business Intelligence Manager"}]},
    {"org": "Stanbic", "roles": [{"title": "Senior Business Analyst"}]},
    {"org": "Bolt", "roles": [{"title": "Product and Operations Analyst"}]}],
    "skills": {"Tools": "Power BI, SQL, Azure Data Factory, Excel, Python"}}
H1B = [{"code": "H-1B", "label": "H-1B sponsor"}]


def _row(title, company, tags, **kw):
    return {"title": title, "company": company, "tags": tags, "location": "New York, NY", "visa": H1B, **kw}


FEED = [  # newest first, as the feed comes; the SRE roles are the freshest and best "reachable"
    _row("Site Reliability Engineer (Senior or Staff), Storage Layer Services", "MongoDB",
         ["Cloud & Infrastructure", "Software Engineering"], is_new=True),
    _row("Site Reliability Engineer 3", "MongoDB", ["Cloud & Infrastructure"], is_new=True),
    _row("Staff Product Manager - Internal AI", "MongoDB", ["Machine Learning & AI", "Product Management"], is_new=True),
    _row("Operations Manager", "CVS Pharmacy", ["Operations", "Hospitality & Retail"]),
    _row("Business Intelligence Analyst", "KLA", ["Data Science & Analytics"]),
    _row("Senior Business Analyst - Credit Reporting", "Capital One", ["Business Analysis", "Data Science & Analytics"]),
    _row("Power BI Developer", "ePace", ["Data Science & Analytics"]),
]


def test_bi_and_analytics_roles_rank_above_sre_for_a_bi_profile():
    out = [r["title"] for r in rank(FEED, fit_targets(BI))]
    top3 = out[:3]
    assert "Business Intelligence Analyst" in top3 and "Senior Business Analyst - Credit Reporting" in top3
    assert "Power BI Developer" in top3
    sre = [out.index(t) for t in out if "Reliability" in t]
    bi = out.index("Business Intelligence Analyst")
    assert all(i > bi for i in sre)
    assert out.index("Power BI Developer") < out.index("Operations Manager")   # generic title words weigh little


def test_one_employer_does_not_fill_the_first_screen():
    rows = [_row(f"Business Intelligence Analyst {i}", "Acme", ["Data Science & Analytics"]) for i in range(5)]
    rows.append(_row("Business Analyst", "Other", ["Business Analysis"]))
    out = rank(rows, fit_targets(BI))
    assert [r["company"] for r in out[:3]].count("Acme") == 2


def test_no_profile_means_no_fit_ranking_and_an_honest_label():
    assert fit_targets({}) is None
    assert rank(FEED, None) == FEED
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "r.fit_ranked ? \"home.today\" : \"home.todayNew\"" in js
    en = (ROOT / "ui" / "static" / "locales" / "en.json").read_text(encoding="utf-8")
    assert '"home.todayNew": "Today · new sponsor roles"' in en
