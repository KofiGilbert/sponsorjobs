"""International-student accessibility filter (2026-08-08).

A sponsor badge is employer-level; these tests pin the layer that keeps the FEED to roles a
student can actually take — dropping clearance/citizen/no-sponsorship roles and the
defense-contractor flood — WITHOUT hiding roles that genuinely sponsor (CLAUDE.md §8).
"""

from __future__ import annotations

import pytest

from sourcing.quality import (employer_is_clearance_heavy, is_entry_level,
                              is_intl_student_accessible, role_excludes_international)
from sourcing.sponsors import SponsorRecord


@pytest.mark.parametrize("jd", [
    "Applicants must be a U.S. citizen.",
    "Requires an active security clearance (TS/SCI).",
    "Ability to obtain a clearance is required.",
    "This position does not offer visa sponsorship.",
    "We are unable to sponsor visas at this time.",
    "No visa sponsorship available for this role.",
    "US citizenship is required.",
])
def test_hard_bars_in_the_jd_are_excluded(jd):
    assert role_excludes_international("Some Startup", jd)


@pytest.mark.parametrize("jd", [
    "We are happy to sponsor visas for the right candidate.",
    "Visa sponsorship available. H-1B and green-card support offered.",
    "We welcome international applicants and provide sponsorship.",
    "Build distributed systems in Go. Remote-friendly.",
])
def test_positive_or_neutral_jds_are_kept(jd):
    # The whole point of §8: never hide a role that actually sponsors.
    assert not role_excludes_international("Some Startup", jd)


def test_defense_primes_and_subsidiaries_are_dropped():
    assert employer_is_clearance_heavy("Lockheed Martin")
    assert employer_is_clearance_heavy("Lockheed Martin Corporation")
    assert employer_is_clearance_heavy("General Dynamics Information Technology")  # subsidiary
    assert employer_is_clearance_heavy("L3Harris Technologies")
    # Even with a clean JD, a clearance-heavy employer is dropped (postings skew clearance).
    assert role_excludes_international("Lockheed Martin", "Great software role, apply now!")


def test_normal_tech_employers_are_kept():
    assert not employer_is_clearance_heavy("Stripe")
    assert not employer_is_clearance_heavy("Databricks")
    # A distinct company that merely shares a NON-prime word must not be swept in
    # ("general dynamics" is the prime, "general" alone is not, so General Mills is safe).
    assert not employer_is_clearance_heavy("General Mills")
    assert not employer_is_clearance_heavy("Northern Trust")   # "northern" != a prime


def test_is_intl_student_accessible_on_job_dicts():
    assert is_intl_student_accessible({"company": "Stripe", "jd_text": "sponsorship available"})
    assert not is_intl_student_accessible({"company": "Lockheed Martin", "jd_text": ""})
    assert not is_intl_student_accessible(
        {"company": "Acme", "jd_text": "Must be a US citizen with a secret clearance."})


# -- entry-level / new-grad detection ------------------------------------- #

@pytest.mark.parametrize("title", [
    "New Grad Software Engineer", "Junior Data Analyst", "Entry-Level Accountant",
    "Software Engineer, Early Career", "Graduate Engineer", "Associate Product Manager",
    "Financial Analyst (New Graduate)", "Software Engineering Intern",
])
def test_entry_level_titles_are_detected(title):
    assert is_entry_level(title)


@pytest.mark.parametrize("title", [
    "Senior Software Engineer", "Staff Data Scientist", "Principal Engineer",
    "Engineering Manager", "Software Engineer III", "Lead Developer",
    "Director of Product", "Sr. Analyst",
])
def test_senior_titles_are_not_entry_level(title):
    assert not is_entry_level(title)


def test_entry_level_falls_back_to_the_jd_when_the_title_is_neutral():
    assert is_entry_level("Software Engineer", "0-2 years of experience. New grads welcome.")
    assert not is_entry_level("Software Engineer", "8+ years of experience required.")


# -- nationality-based visas (E-3 / H-1B1 / TN) --------------------------- #

def test_nationality_visas_offered_only_for_an_h1b_sponsor():
    h1b = SponsorRecord("Acme", h1b_approvals=50, h1b_last_fy=2023, naics="", state="",
                        cap_exempt=False)
    codes = {v["code"] for v in h1b.nationality_visas()}
    assert codes == {"E-3", "H-1B1", "TN"}
    assert all(v.get("basis") for v in h1b.nationality_visas())      # honest caveat present
    none = SponsorRecord("Small Co", h1b_approvals=0, h1b_last_fy=0, naics="", state="",
                         cap_exempt=False, perm_certs=5)             # green-card only, no H-1B
    assert none.nationality_visas() == []
