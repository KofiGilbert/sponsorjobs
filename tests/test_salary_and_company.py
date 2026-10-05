"""Richer job detail (2026-08-09): pay pulled from the posting prose + a NAICS->industry name
for the "About the company" strip. Both are pure functions, pinned here.

The salary extractor is deliberately CONSERVATIVE: a missing range ("Salary TBD") is honest, a
wrong one is not (CLAUDE.md 8). So a dollar figure only counts as pay when a compensation cue
sits next to it -- funding rounds, referral bonuses and revenue never masquerade as salary.
"""

from __future__ import annotations

from sourcing.ats import salary_from_text
from sourcing.sponsors import naics_industry


def test_salary_range_from_body():
    assert salary_from_text("The base pay for this position is $78,000.00 - $156,000.00.") == "$78k-$156k/yr"
    assert salary_from_text("Salary range: $120k to $160k plus bonus.") == "$120k-$160k/yr"


def test_salary_en_dash_and_em_dash_ranges():
    assert salary_from_text("Base salary $90,000 – $110,000 per year.") == "$90k-$110k/yr"
    assert salary_from_text("Pay range $100,000 — $130,000 annually.") == "$100k-$130k/yr"


def test_salary_hourly_detected():
    assert salary_from_text("The hourly rate for this role is $45.00 - $60.00 per hour.") == "$45-$60/hr"
    assert salary_from_text("Pay range $28 - $34 / hour depending on experience.") == "$28-$34/hr"


def test_salary_single_cued_figure():
    assert salary_from_text("Base salary is $135,000 annually.") == "$135k/yr"


def test_salary_ignores_non_pay_dollars():
    # A missing figure is honest; these must NOT be read as pay.
    assert salary_from_text("Compensation will be commensurate with experience.") == ""
    assert salary_from_text("We raised $2 billion in funding and serve 160 countries.") == ""
    assert salary_from_text("Refer a friend and get a $500 referral bonus!") == ""
    assert salary_from_text("") == ""


def test_naics_industry_lookup():
    assert naics_industry("541511") == "Professional, Scientific & Technical Services"
    assert naics_industry("622110") == "Health Care & Social Assistance"
    assert naics_industry("336411") == "Manufacturing"          # 33x all -> Manufacturing


def test_naics_industry_unknown_is_blank():
    assert naics_industry("") == ""
    assert naics_industry("99") == ""                            # not a real sector
    assert naics_industry(None) == ""
