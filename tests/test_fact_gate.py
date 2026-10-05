"""The numeric fact gate (tailoring/fact_gate.py): figures a tailored draft states that the
person's own material never did are FLAGGED for the review, never silently shipped."""

from __future__ import annotations

from tailoring.fact_gate import describe, figures, flag_new_figures, source_figures


def test_figures_are_bare_digits_without_years():
    assert figures("Cut close from 5 days to 1, grew revenue 40% to $1.2M in 2021") == {
        "5", "1", "40", "1.2"}
    assert figures("Managed 1,200 accounts (12+ reps) at 3x throughput") == {"1200", "12", "3"}
    assert figures("Jan 2019 - Dec 2021") == set()


def test_source_figures_walks_every_nested_string():
    profile = {"experience": [{"org": "Acme", "roles": [{"title": "Analyst",
                "bullets": ["Built 12 dashboards used by 300 staff"]}]}],
               "summary": "Six years, $4M budget"}
    assert source_figures(profile) == {"12", "300", "4"}


def test_new_figures_are_flagged_with_where_they_appear():
    source = {"experience": [{"org": "Acme", "title": "Analyst",
                              "bullets": ["Built 12 dashboards used across the bank"]}]}
    tailored = {"experience": [{"org": "Acme", "title": "Analyst",
                                "bullets": ["Built 12 dashboards adopted by 300 analysts",
                                            "Cut reporting time 40%"]}]}
    flags = flag_new_figures(tailored, source)
    assert [f["figures"] for f in flags] == [["300"], ["40"]]
    assert flags[0]["label"] == "Acme · Analyst" and flags[0]["section"] == "experience"
    note = describe(flags)
    assert "300" in note and "40" in note and "Acme" in note


def test_rephrased_figures_with_suffixes_are_not_flagged():
    source = {"experience": [{"org": "Acme", "title": "PM",
                              "bullets": ["Exports of 60M and a fleet of 250 drivers"]}]}
    tailored = {"experience": [{"org": "Acme", "title": "PM",
                                "bullets": ["Grew exports to $60M+ with a 250-driver fleet"]}]}
    assert flag_new_figures(tailored, source) == []


def test_extra_sources_such_as_the_transcript_count_as_origin():
    source = {"experience": [{"org": "Acme", "title": "PM", "bullets": ["Led the team"]}]}
    tailored = {"experience": [{"org": "Acme", "title": "PM", "bullets": ["Led a team of 8"]}]}
    assert flag_new_figures(tailored, source) and not flag_new_figures(
        tailored, source, ["I managed 8 people at Acme"])


def test_nested_roles_and_projects_and_summary_are_all_checked():
    source = {"projects": [{"org": "Beacon", "bullets": ["Shipped a beacon app"]}],
              "summary": "Analyst."}
    tailored = {"experience": [], "summary": "Analyst with 6 years in finance.",
                "projects": [{"org": "Beacon", "bullets": ["Shipped a beacon app to 2 clients"]}]}
    flagged = flag_new_figures(tailored, source)
    assert {f["section"] for f in flagged} == {"projects", "summary"}
