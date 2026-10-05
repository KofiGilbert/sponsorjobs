"""Salary context (2026-08-09): how a role's pay compares to typical pay for its family on the
board -- a LinkedIn-Premium-style read, built from our own data.

Two pieces are pinned here: the role-family classifier (whose regexes have a nasty edge -- several
tokens are PREFIXES like "data scien", so a trailing word boundary would wrongly reject
"Data Scientist"), and the benchmark/insight math (median per family, +/-15% bands, thin families
and unpriced roles yield no claim).
"""

from __future__ import annotations

from sourcing.filters import role_family, salary_benchmarks, salary_insight


def test_family_prefix_tokens_match_whole_words():
    # The bug that motivated the rewrite: "data scien" must match "Data Scientist".
    assert role_family("Senior Data Scientist") == "Data & AI"
    assert role_family("Data Engineer") == "Data & AI"
    assert role_family("Financial Analyst") == "Finance & Accounting"


def test_family_ordering_and_no_false_matches():
    assert role_family("Salesforce Developer") == "Engineering"   # Engineering wins before Sales
    assert role_family("Career Coach") == ""                      # "care" must NOT match Healthcare
    assert role_family("Zookeeper") == ""


def test_benchmarks_need_min_samples():
    jobs = [{"title": "Data Analyst", "salary": "$90k/yr"},
            {"title": "Data Scientist", "salary": "$150k/yr"},
            {"title": "ML Engineer", "salary": "$200k/yr"},
            {"title": "Nurse", "salary": "$80k/yr"}]           # only 1 Healthcare -> no benchmark
    b = salary_benchmarks(jobs, min_samples=3)
    assert b == {"Data & AI": (150000, 3)}                     # median of 90/150/200k


def test_insight_verdict_bands():
    b = {"Data & AI": (150000, 40)}
    assert salary_insight({"title": "Data Scientist", "salary": "$220k/yr"}, b)["verdict"] == "above"
    assert salary_insight({"title": "Data Scientist", "salary": "$150k/yr"}, b)["verdict"] == "around"
    assert salary_insight({"title": "Data Scientist", "salary": "$110k/yr"}, b)["verdict"] == "below"


def test_insight_none_when_no_pay_or_no_family_benchmark():
    b = {"Data & AI": (150000, 40)}
    assert salary_insight({"title": "Data Scientist", "salary": ""}, b) is None          # no pay
    assert salary_insight({"title": "Registered Nurse", "salary": "$80k/yr"}, b) is None  # no benchmark
