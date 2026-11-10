"""The Skills block holds at most 5 lines (Kofi, 2026-11-10): the space goes to experience bullets.

* At most ``linefit.SKILL_LINES_MAX`` lines, a Certifications line included, across a build, an
  ADD and a template switch. A template's manifest may set its own cap.
* Over the cap, lines merge by meaning. What no longer fits goes in priority order: vague
  phrases and terms the job does not ask for first; a job term or a term the person ADDed stays
  on the page, so the keyword match never falls. What goes stays in their saved skills.
* The freed line feeds the staircase fill: the oldest role gets its second bullet.
"""

from __future__ import annotations

import copy

import pytest

from conftest import requires_latex
from intake.memory import ConversationMemory
from intake.template_manifest import load_manifest, max_skill_lines
from llm.base import FakeLLM
from tailoring import assembler as A
from tailoring import linefit as LF
from tailoring.assembler import assemble_cv, normalize_profile, ranked_roles
from tailoring.keywords import term_present
from ui.records import CVRecords
from ui.session import WebIntake

# The D365 build's six lines (2026-11-10), and the job's own terms.
JD = ("D365 Software Developer. X++, CI/CD, .NET, SQL, REST APIs, D365, GitHub, Python, Power BI, "
      "SSRS, Power Apps, Power Automate, Dataverse, Power Platform, SQL Server, Azure, C#, "
      "object-oriented programming, JSON, XML, Git, Azure DevOps, JavaScript, Data Management "
      "Framework, OData, Microsoft Fabric, Azure Data Factory, Azure Functions, Logic Apps, "
      "Dynamics 365 Finance, data pipelines, dashboards, reporting.")
D365 = {
    "Languages & Tools": "X++, CI/CD, .NET, SQL, Dashboards, Reporting, REST APIs, D365, "
                         "advanced Excel, Python, GitHub",
    "Power Platform & Reporting": "Power BI, SSRS, Power Apps, Power Automate, Dataverse, "
                                  "Power Platform, SQL Server, Azure",
    "Development & Delivery": "C#, Object-oriented programming, JSON, XML, Git, Azure DevOps, "
                              "JavaScript, Firebase, OCR",
    "Microsoft & Azure Platform": "Data Management Framework, OData, Microsoft Fabric, "
                                  "Azure Data Factory, Azure Functions",
    "Domain Knowledge": "Logic Apps, Dynamics 365 Finance, evidence-based recommendations, "
                        "revenue and cost analysis",
    "Analysis & Requirements": "Data Pipelines, Business analysis, enterprise CRM requirements, "
                               "business analytics, KPI frameworks",
}


def _terms(skills: dict) -> list[str]:
    return [t for v in (skills or {}).values() for t in LF.split_terms(v)]


def _jd_terms(skills: dict) -> set[str]:
    return {t for t in _terms(skills) if term_present(t, JD)}


# --------------------------------------------------------------------------- the cap itself
def test_the_default_cap_is_five_and_a_manifest_may_set_its_own():
    assert LF.SKILL_LINES_MAX == 5 and LF.SKILL_LINES_MIN == 4
    assert max_skill_lines({}) == 5 and max_skill_lines(load_manifest("shetty")) == 5
    assert max_skill_lines({"max_skill_lines": 6}) == 6
    from pathlib import Path
    import json
    for p in (Path(__file__).resolve().parent.parent / "config" / "templates").glob("*.json"):
        assert max_skill_lines(json.loads(p.read_text())) <= 5, p.name


def test_six_lines_merge_by_meaning_into_five_and_every_job_term_stays():
    out = LF.cap_skill_block(D365, 5, jd_text=JD)
    assert len(out) == 5
    assert _jd_terms(D365) <= set(_terms(out)), _jd_terms(D365) - set(_terms(out))
    assert len(_terms(out)) == len(set(_terms(out)))                  # one term, one line
    for label, value in out.items():                                   # each still one line
        assert LF.estimate_width(label, LF.split_terms(value)) <= 574.0 - 2.0, label


def test_vague_phrases_go_before_concrete_tools():
    out = _terms(LF.cap_skill_block(D365, 5, jd_text=JD))
    assert "evidence-based recommendations" not in out and "revenue and cost analysis" not in out
    assert "business analytics" not in out
    assert not any(LF.is_vague_skill(t) for t in out)          # no vague phrase kept over a tool
    assert "KPI frameworks" in out and "enterprise CRM requirements" in out


def test_a_nearly_empty_line_merges_rather_than_two_full_ones():
    """Kofi's saved block: "Domain Knowledge" held only Logic Apps once the degrees were taken
    off. It joins a line; it never stays as a one-term row while two full rows are squeezed."""
    skills = dict(D365, **{"Domain Knowledge": "Logic Apps"})
    out = LF.cap_skill_block(skills, 5, jd_text=JD)
    assert len(out) == 5 and "Logic Apps" in _terms(out)
    assert all(len(LF.split_terms(v)) >= 3 for v in out.values()), out


@pytest.mark.parametrize("term,vague", [
    ("evidence-based recommendations", True), ("revenue and cost analysis", True),
    ("business analysis", True), ("Power BI", False), ("KPI frameworks", False),
    ("advanced Excel", False), ("Data Pipelines", False), ("SQL", False),
    ("enterprise CRM requirements", False)])
def test_what_counts_as_a_vague_phrase(term, vague):
    assert LF.is_vague_skill(term) is vague


def test_terms_the_person_added_are_kept_over_their_other_skills():
    narrow = lambda label, terms: 10.0 * len(terms)            # room for four terms a line
    skills = {"Tools": "SQL, Firebase", "Data": "Python, OCR", "Web": "JavaScript, Jira"}
    jd = "SQL, Python, JavaScript"
    plain = LF.cap_skill_block(skills, 2, jd_text=jd, width_fn=narrow, avail=40.0)
    kept = LF.cap_skill_block(skills, 2, jd_text=jd, width_fn=narrow, avail=40.0,
                              keep=["Jira", "OCR"])
    assert len(plain) == len(kept) == 2
    assert {"SQL", "Python", "JavaScript"} <= set(_terms(plain)) & set(_terms(kept))
    assert {"Jira", "OCR"} <= set(_terms(kept))                # ADDed: never shed first
    assert "Firebase" not in _terms(kept)


def test_a_block_within_the_cap_is_left_exactly_as_it_is():
    five = dict(list(D365.items())[:5])
    assert LF.cap_skill_block(five, 5, jd_text=JD) is five


def test_the_packer_never_passes_the_cap_counting_a_certifications_line():
    skills = dict(D365, Certifications="PL-300 Power BI Data Analyst")
    pool = ["Jira", "Tableau", "Snowflake", "Kubernetes", "Terraform", "Jenkins", "Agile"]
    pack = LF.pack_skills(skills, pool, jd_text=JD)
    assert len(pack.skills) <= 5 and "Certifications" in pack.skills
    # Four lines cannot hold every job term here: one is lost only when no other term on the
    # page is as wide (nothing the job does not ask for holds a place it could take).
    w = lambda t: LF.estimate_width("", [t])
    lost = _jd_terms(D365) - set(_terms(pack.skills))
    others = [t for t in _terms(pack.skills) if not term_present(t, JD)
              and t not in LF.split_terms(skills["Certifications"])]
    assert all(w(o) < w(t) for t in lost for o in others), (lost, others)


def test_add_places_the_term_on_an_existing_line_never_a_sixth():
    five = LF.cap_skill_block(D365, 5, jd_text=JD)
    for term in ("Terraform", "Stakeholder management", "Kubernetes"):
        five = LF.place_term(five, term, jd_text=JD).skills
        assert len(five) == 5, five


# --------------------------------------------------------------------------- the page
@requires_latex
def test_a_six_line_profile_builds_with_five_lines_and_no_job_term_lost(template_source, workdir):
    prof = normalize_profile(_profile_with(D365))
    res = assemble_cv(template_source, prof, JD, None, workdir, tailor=False)
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0
    assert len(res.profile_used["skills"]) <= 5
    assert _jd_terms(D365) <= set(_terms(res.profile_used["skills"]))
    six = assemble_cv(template_source, normalize_profile(_profile_with(D365)), JD, None, workdir,
                      tailor=False, skill_cap=6, jobname="six")
    assert len(six.profile_used["skills"]) == 6                       # a manifest's own cap


def _profile_with(skills: dict) -> dict:
    from test_recency_bullets import _role
    return {"identity": {"name": "K G", "email": "k@example.com"},
            "skills": dict(skills),
            "experience": [_role("Acme", "2025 - Present", 3), _role("Beta", "2018 - 2024", 3),
                           _role("Cobalt", "2016 - 2018", 2), _role("Delta", "2015 - 2016", 2)]}


def _counts(profile: dict) -> list[int]:
    return [len(r.get("bullets") or []) for _rank, _e, r in
            sorted(ranked_roles(profile), key=lambda x: x[0])]


def _staircase_sample():
    """A page with the person's own material for 5, 4, 3 and 2 bullets, sized so that with six
    skills lines the oldest role's second bullet does not fit."""
    from test_page_fill_links import _short_with_own_material
    p = _short_with_own_material()
    for e, n in zip(p["experience"], (5, 4, 3, 2)):
        e["roles"][0]["bullets"] = e["roles"][0]["bullets"][:n]
    p["projects"][0]["bullets"] = p["projects"][0]["bullets"][:2]
    p["projects"][1]["bullets"] = p["projects"][1]["bullets"][:1]
    p["extracurricular"][1]["bullets"] = p["extracurricular"][1]["bullets"][:2]
    return p


@requires_latex
def test_the_freed_line_gives_the_oldest_role_its_second_bullet(template_source, workdir):
    jd = "Data Analyst. SQL, Python, Tableau, Excel."
    six = assemble_cv(template_source, _staircase_sample(), jd, None, workdir, tailor=False,
                      skill_cap=6, jobname="six")
    five = assemble_cv(template_source, _staircase_sample(), jd, None, workdir, tailor=False)
    assert len(six.profile_used["skills"]) == 6 and _counts(six.profile_used)[-1] == 1
    assert five.ok and five.compile.pages == 1 and five.compile.overfull_count == 0
    assert len(five.profile_used["skills"]) == 5
    c = _counts(five.profile_used)
    assert c == [5, 4, 3, 2] and A.staircase_ok(five.profile_used), c
    assert five.fill_ratio >= A.FILL_GOAL, five.fill_ratio
    # every bullet on the page is the person's own
    mine = {b for e in _staircase_sample()["experience"] for r in e["roles"] for b in r["bullets"]}
    page = {b for _k, _e, r in ranked_roles(five.profile_used) for b in r["bullets"]}
    assert page <= mine


# --------------------------------------------------------------------------- the session
def _session(template_source, workdir, mem=None):
    return WebIntake(JD, template_source, FakeLLM(), mem or ConversationMemory(":memory:"),
                     CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")


@requires_latex
def test_the_cap_holds_across_a_build_two_adds_and_a_template_switch(template_source, workdir):
    s = _session(template_source, workdir)
    s.essentials["skills_from_jobs"] = ["Firebase"]                   # an earlier ADD
    s.profile = normalize_profile(_profile_with(D365))
    s.stage = "review"
    s._assemble()
    shown = s.assembled.profile_used["skills"]
    assert s.assembled.ok and len(shown) <= 5
    assert _jd_terms(D365) <= set(_terms(shown))
    assert "Firebase" in _terms(shown)                                # the ADD stays on the page
    # What the page left off stays the person's own skill.
    dropped = set(_terms(D365)) - set(_terms(shown))
    assert dropped and dropped <= set(s.essentials.get("skills_input") or [])
    for term in ("Terraform", "Kubernetes"):
        out = s.keyword_add(term)
        assert out["ok"], out.get("reason")
        assert len(s.assembled.profile_used["skills"]) <= 5
        assert term in _terms(s.assembled.profile_used["skills"])
    assert _jd_terms(D365) <= set(_terms(s.assembled.profile_used["skills"]))
    from ui.app import _load_template
    tex, name = _load_template("software")
    st = s.set_template(name, tex)
    assert st.get("template_ok"), st.get("template_error")
    assert len(s.assembled.profile_used["skills"]) <= 5
    assert {"Terraform", "Kubernetes"} <= set(_terms(s.assembled.profile_used["skills"]))
