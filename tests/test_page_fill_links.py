"""The built resume page (Kofi, 2026-11-10, a D365 build at 83% of the page).

* The page fills, with the person's own bullets in a strict staircase by recency: the most
  recent role carries the most, every older role at least one fewer.
* Project links: a project gets its GitHub repository's link (the small blue arrow) when the
  person's own material names the repository, matched by name, never a guess; the contact line
  shows "In | GitHub".
* Job terms stay whole (.NET, CI/CD, C#), near-duplicates show once, and degrees,
  certifications and years of experience are what a job requires, never skills to add.
* An added skill goes on the line of its own kind (Azure with Azure), and never pushes a job
  term off the page.
"""

from __future__ import annotations

import copy

import pytest

from conftest import requires_latex
from intake.links import match_repo_links, name_words, repo_matches
from intake.memory import ConversationMemory
from llm.base import FakeLLM
from tailoring import assembler as A
from tailoring.assembler import assemble_cv, ranked_roles, render_cv, normalize_profile
from tailoring.keywords import (build_coverage_report, clean_term, dedupe_near_terms,
                                extract_jd_terms, is_requirement, skill_terms, split_jd_item)
from tailoring.linefit import clean_pool, clean_skill_block, place_term
from ui.records import CVRecords
from ui.session import WebIntake


def _counts(profile: dict) -> list[int]:
    return [len(r.get("bullets") or []) for _rank, _e, r in
            sorted(ranked_roles(profile), key=lambda x: x[0])]


# --------------------------------------------------------------------------- the staircase
def _short_with_own_material():
    from test_recency_bullets import _near_full
    p = _near_full()
    own = {
        0: ["Automated the depot scorecard refresh with a scheduled Python job that runs before the morning stand-up.",
            "Documented the delivery metrics so new analysts could rebuild each dashboard without help from the team.",
            "Joined route planners each week to explain late-delivery trends and agree on the depots to review next.",
            "Moved the monthly carrier report from Excel to SQL views, so finance could refresh it in minutes."],
        1: ["Built a wait-time dashboard in Tableau that clinic managers checked at the start of every shift.",
            "Cleaned three years of appointment records in SQL so the yearly staffing review used one trusted source."],
        2: ["Counted weekly stock at three stores and reconciled the counts against the point-of-sale exports."],
        3: ["Shelved returned books and kept the reading rooms in order during evening shifts."],
    }
    for i, extra in own.items():
        p["experience"][i]["roles"][0]["bullets"] += extra
    return p


@requires_latex
def test_a_short_page_fills_to_the_goal_in_a_strict_staircase_from_the_person_s_own_bullets(
        template_source, workdir):
    prof = _short_with_own_material()
    res = assemble_cv(template_source, prof, "Data Analyst. SQL, Python, Tableau, Excel.", None,
                      workdir, tailor=False)
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0
    assert A.FILL_GOAL <= res.fill_ratio <= A.MAX_FILL, res.fill_ratio
    c = _counts(res.profile_used)
    assert len(c) == 4 and c[0] == max(c) and c[0] >= 4
    assert all(b < a for a, b in zip(c, c[1:]) if a > 1), c          # strictly fewer each step
    assert A.staircase_ok(res.profile_used)
    # every bullet on the page is the person's own (nothing written, nothing invented)
    mine = {b for e in prof["experience"] for r in e["roles"] for b in r["bullets"]}
    page = {b for _k, _e, r in ranked_roles(res.profile_used) for b in r["bullets"]}
    assert page <= mine and {a["kind"] for a in res.added} == {"saved"}


@requires_latex
def test_tonight_s_wrong_way_round_page_comes_out_as_a_staircase(template_source, workdir):
    """BlackOrigin 3, Stanbic 4, Viva 2, CSIR 2 ran the wrong way: the most recent role grows
    from the person's own material first, so it carries the most."""
    from test_recency_bullets import _role
    acme = _role("Acme", "2025 - Present", 6, words="Shipped")
    prof = normalize_profile({
        "identity": {"name": "K G", "email": "k@example.com"},
        "experience": [acme, _role("Beta", "2018 - 2024", 4), _role("Cobalt", "2016 - 2018", 2),
                       _role("Delta", "2015 - 2016", 1)]})
    res = assemble_cv(template_source, prof, "analyst", None, workdir, tailor=False)
    c = _counts(res.profile_used)
    assert c[0] >= 5 and c[0] == max(c) and A.staircase_ok(res.profile_used), c


@requires_latex
def test_a_short_page_is_never_cut_further_to_make_the_staircase(template_source, workdir):
    """When the most recent role has no more material, cutting the older roles would only make
    white space: they keep their bullets and a note asks for more about the recent role."""
    from test_recency_bullets import _role
    prof = normalize_profile({
        "identity": {"name": "K G", "email": "k@example.com"},
        "experience": [_role("Acme", "2025 - Present", 2), _role("Beta", "2018 - 2024", 3),
                       _role("Cobalt", "2016 - 2018", 2)]})
    res = assemble_cv(template_source, prof, "analyst", None, workdir, tailor=False)
    assert _counts(res.profile_used) == [2, 3, 2] and res.trims == []
    assert any("Acme" in n and "Tell me more" in n for n in res.bullet_notes), res.bullet_notes


def test_staircase_ok():
    from test_recency_bullets import _role
    ok = normalize_profile({"experience": [_role("A", "2024 - Present", 5), _role("B", "2020 - 2024", 4),
                                           _role("C", "2018 - 2020", 3), _role("D", "2016 - 2018", 2)]})
    bad = normalize_profile({"experience": [_role("A", "2024 - Present", 3), _role("B", "2020 - 2024", 4),
                                            _role("C", "2018 - 2020", 2), _role("D", "2016 - 2018", 2)]})
    assert A.staircase_ok(ok) and not A.staircase_ok(bad)


# --------------------------------------------------------------------------- job terms
@pytest.mark.parametrize("item,want", [
    ("CI/CD", ["CI/CD"]), (".NET", [".NET"]), ("C#", ["C#"]), ("C++", ["C++"]),
    ("Node.js", ["Node.js"]), ("A/B Testing", ["A/B Testing"]), ("UI/UX", ["UI/UX"]),
    ("Tableau or Power BI", ["Tableau", "Power BI"]), ("HTML/CSS", ["HTML", "CSS"]),
    ("Git/source control", ["Git", "source control"]), ("Profit and Loss", ["Profit and Loss"]),
    ("Power Apps and Power Automate", ["Power Apps", "Power Automate"]), ("SQL Server.", ["SQL Server"]),
])
def test_a_model_read_item_splits_into_whole_terms(item, want):
    assert split_jd_item(item) == want


def test_term_cleaning_keeps_the_dot_of_net():
    assert clean_term(".NET") == ".NET" and clean_term("SQL.") == "SQL" and clean_term("C#,") == "C#"


def test_the_ad_s_own_terms_stay_whole():
    jd = "You will use .NET, C#, C++ and Node.js, own our CI/CD pipelines, and run A/B tests."
    terms = extract_jd_terms(jd)
    for t in (".NET", "C#", "C++", "Node.js", "CI/CD"):
        assert t in terms, terms
    assert "NET" not in terms and "CI" not in terms and "CD" not in terms
    found = skill_terms(jd)
    assert ".NET" in found and "CI/CD" in found and "CI" not in found


def test_the_model_reading_keeps_net_and_ci_cd_whole():
    from llm.anthropic_client import AnthropicLLM
    llm = object.__new__(AnthropicLLM)
    llm._complete = lambda *a, **k: ('[".NET", "CI/CD", "C#", "Tableau or Power BI", '
                                     '"Bachelor\'s degree Computer Science"]')
    got = llm.extract_jd_skills("A job ad that needs .NET and CI/CD.")
    assert got[:5] == [".NET", "CI/CD", "C#", "Tableau", "Power BI"]


@pytest.mark.parametrize("term,req", [
    ("Bachelor's degree Computer Science", True), ("Bachelor's in Computer Science", True),
    ("BS in Computer Science", True), ("Dynamics 365 certifications", True),
    ("Microsoft certifications", True), ("5+ years of experience", True),
    ("Security clearance", True), ("Power BI", False), ("PMP", False), ("Logic Apps", False),
    ("Certificate management", False), ("CI/CD", False),
])
def test_requirements_are_not_skills(term, req):
    assert is_requirement(term) is req


def test_requirements_are_listed_apart_and_kept_out_of_the_percentage():
    rep = build_coverage_report("ad", "SQL, Power BI", {"skills": {"a": "SQL, Power BI"}},
                                terms=["SQL", "Power BI", "Azure", "Bachelor's degree Computer Science",
                                       "Dynamics 365 certifications"])
    assert rep.present == ["SQL", "Power BI"] and rep.missing_unsupported == ["Azure"]
    assert rep.requirements == ["Bachelor's degree Computer Science", "Dynamics 365 certifications"]
    assert rep.coverage_ratio == pytest.approx(2 / 3)
    assert set(rep.all_terms) >= set(rep.requirements)


def test_near_duplicates_show_once():
    assert dedupe_near_terms(["BI", "Power BI", "Excel", "advanced Excel", "SQL", "SQL Server",
                              "Python (pandas)", "Python"]) == ["Power BI", "advanced Excel", "SQL",
                                                                "SQL Server", "Python (pandas)"]


def test_the_skills_block_shows_no_degree_no_duplicate_and_no_broken_term():
    sk = {"Languages & Tools": "X++, CD, NET, CI, SQL, BI, advanced Excel, Python, Excel",
          "Power Platform & Reporting": "Power BI, SSRS, Power Apps",
          "Domain Knowledge": "Microsoft certifications, Logic Apps, Bachelor's in Computer Science, "
                              "Bachelor's degree Computer Science",
          "Certifications": "PMP, MBA"}
    out = clean_skill_block(sk)
    assert out["Languages & Tools"] == "X++, CI/CD, .NET, SQL, advanced Excel, Python"
    assert out["Domain Knowledge"] == "Logic Apps"
    assert out["Certifications"] == "PMP"                 # a real certification stays; a degree never
    assert not any("Bachelor" in v for v in out.values())
    # the packer's pool gets the same treatment
    assert clean_pool(["NET", "CD", "Excel", "Tableau", "Bachelor's in CS"], out) == ["Tableau"]


@requires_latex
def test_a_page_never_shows_a_degree_on_a_skills_line(template_source, workdir):
    prof = copy.deepcopy(_short_with_own_material())
    prof["skills"]["Domain"] = "Logic Apps, Bachelor's degree Computer Science, Microsoft certifications"
    res = assemble_cv(template_source, prof, "analyst", None, workdir, tailor=False)
    body = res.tex_source.split("Skills", 1)[1].split("Projects", 1)[0]
    assert "Logic Apps" in body and "Bachelor" not in body and "certifications" not in body


# --------------------------------------------------------------------------- placing an added skill
SKILLS = {"Languages & Tools": "X++, SQL, Data Pipelines, Python",
          "Power Platform & Reporting": "Power BI, SSRS, Power Automate, Dataverse",
          "Development & Delivery": "C#, JSON, XML, Git, JavaScript",
          "Microsoft & Azure Platform": "OData, Microsoft Fabric, Azure Data Factory, Azure Functions",
          "Domain Knowledge": "KPI frameworks",
          "Strategy & Performance": "KPI frameworks, performance analysis"}


@pytest.mark.parametrize("term,line", [
    ("Logic Apps", "Microsoft & Azure Platform"), ("Azure DevOps", "Microsoft & Azure Platform"),
    ("Power Apps", "Power Platform & Reporting"), (".NET", "Development & Delivery"),
    ("REST APIs", "Languages & Tools"),
])
def test_an_added_skill_goes_on_the_line_of_its_own_kind(term, line):
    assert place_term(SKILLS, term).label == line


def test_an_added_skill_never_pushes_a_job_term_off_the_page():
    full = {"Microsoft & Azure Platform": "Azure Functions, Azure Data Factory, Azure Synapse",
            "Other Tools": "Jira"}
    jd = "Azure Functions, Azure Data Factory, Azure Synapse, Logic Apps"
    narrow = lambda label, terms: 10.0 * len(terms)          # room for three terms a line
    pl = place_term(full, "Logic Apps", width_fn=narrow, avail=30.0, jd_text=jd)
    # The Azure line is full of job terms: rather than shed one, the term goes on the next line.
    assert pl.label == "Other Tools" and pl.shed == []
    assert pl.skills["Microsoft & Azure Platform"] == full["Microsoft & Azure Platform"]


def test_a_requirement_is_never_placed_on_a_skills_line():
    pl = place_term(SKILLS, "Bachelor's degree Computer Science")
    assert pl.label == "" and pl.skills == SKILLS


# --------------------------------------------------------------------------- project links
REPOS = [{"name": "gatecheck", "html_url": "https://github.com/KofiGilbert/gatecheck", "size": 10},
         {"name": "last-mile-ops-simulator", "size": 10,
          "html_url": "https://github.com/KofiGilbert/last-mile-ops-simulator"},
         {"name": "sales-forecast-dashboard", "size": 10,
          "html_url": "https://github.com/KofiGilbert/sales-forecast-dashboard"}]


def test_a_repository_matches_a_project_by_name_never_by_a_guess():
    assert name_words("last-mile-ops-simulator") == ["last", "mile", "operations", "simulator"]
    assert repo_matches("Gate Check (JavaScript, OCR, Firebase)", "gatecheck")
    assert repo_matches("Last-Mile Operations Simulator", "last-mile-ops-simulator")
    assert repo_matches("GateCheck", "gate-check")
    assert not repo_matches("Sales Dashboard", "sales-forecast-dashboard")
    assert not repo_matches("Data Pipeline", "pipeline")
    assert not repo_matches("Resume Agent", "resume-agent-v2")


def test_two_candidate_repositories_mean_no_link():
    cards = [{"name": "gatecheck", "url": "https://github.com/a/gatecheck"},
             {"name": "gate-check", "url": "https://github.com/a/gate-check"},
             {"name": "secret-tool", "url": "https://github.com/a/secret-tool", "private": True}]
    projects = [{"title": "Gate Check"}, {"title": "Secret Tool"},
                {"title": "Linked", "link": "https://github.com/a/linked"}]
    assert match_repo_links(projects, cards) == []


@pytest.fixture
def gh(monkeypatch):
    from ui import github_publish, github_repos
    github_repos.clear_cache()
    calls = []

    def fetch(method, url, token="", body=None, params=None):
        calls.append(url)
        if url.endswith("/users/KofiGilbert/repos"):
            return 200, [dict(r) for r in REPOS]
        return 404, {}
    monkeypatch.setattr(github_publish, "FETCH", fetch)
    monkeypatch.setenv("GITHUB_LINK_FETCH", "1")
    yield calls
    github_repos.clear_cache()


def _session(template_source, workdir, profile):
    mem = ConversationMemory(":memory:")
    mem.save("default", copy.deepcopy(profile), {}, [])
    s = WebIntake("D365 developer. Azure, Power Platform.", template_source, FakeLLM(), mem,
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    s.start()
    return s


PROJECTS = [{"title": "Last-Mile Operations Simulator (Python, simulation, operations analytics)",
             "bullets": ["Built a discrete-event simulation of a delivery operation."]},
            {"title": "Gate Check (JavaScript, OCR, Firebase)",
             "bullets": ["Built and deployed a gate and yard app for a distribution center."]}]


def test_known_github_gives_each_project_its_repository_link(template_source, workdir, gh):
    s = _session(template_source, workdir, {"identity": {"name": "Kofi", "github": "github.com/KofiGilbert"},
                                            "projects": copy.deepcopy(PROJECTS)})
    s.profile["projects"] = copy.deepcopy(PROJECTS)
    linked = s._link_projects()
    assert sorted(linked) == sorted(p["title"] for p in PROJECTS)
    links = {p["title"].split(" (")[0]: p["link"] for p in s.profile["projects"]}
    assert links == {"Last-Mile Operations Simulator": "https://github.com/KofiGilbert/last-mile-ops-simulator",
                     "Gate Check": "https://github.com/KofiGilbert/gatecheck"}
    # the link renders as the small arrow after the name, linking the repository's home page
    tex = render_cv("\\begin{document}", normalize_profile(s.profile))
    assert "\\href{https://github.com/KofiGilbert/gatecheck}{$\\nearrow$}" in tex


def test_no_github_known_means_no_lookup_and_no_link(template_source, workdir, gh):
    s = _session(template_source, workdir, {"identity": {"name": "Kofi"}})
    s.profile["projects"] = copy.deepcopy(PROJECTS)
    assert s._link_projects() == [] and gh == []
    assert not any(p.get("link") for p in s.profile["projects"])


def test_a_connected_github_fills_the_contact_line_once(template_source, workdir, gh):
    s = _session(template_source, workdir, {"identity": {"name": "Kofi"}})
    s.github_status = lambda: {"connected": True, "login": "KofiGilbert"}
    s.profile["projects"] = copy.deepcopy(PROJECTS)
    s._link_projects()
    assert s.profile["identity"]["github"] == "https://github.com/KofiGilbert"
    assert all(p.get("link") for p in s.profile["projects"])
    # removed by the person later: it stays removed
    s.profile["identity"]["github"] = ""
    s.essentials["identity"]["github"] = ""
    s._link_projects()
    assert s.profile["identity"]["github"] == ""


def test_the_contact_line_reads_in_and_github():
    tex = render_cv("\\begin{document}", normalize_profile({"identity": {
        "name": "Kofi", "email": "k@example.com",
        "linkedin": "https://www.linkedin.com/in/kofi", "github": "https://github.com/KofiGilbert"}}))
    assert "\\href{https://www.linkedin.com/in/kofi}{In} | \\href{https://github.com/KofiGilbert}{GitHub}" in tex


def test_adding_a_repo_that_is_already_on_the_resume_gives_it_the_link(template_source, workdir):
    s = _session(template_source, workdir, {"identity": {"name": "Kofi"}})
    s.profile["projects"] = copy.deepcopy(PROJECTS)
    got = s.add_projects([{"org": "gatecheck", "link": "https://github.com/KofiGilbert/gatecheck",
                           "bullets": ["x."]}])
    assert got["added"] == [] and got["skipped"] == ["gatecheck"]
    gate = next(p for p in s.profile["projects"] if p["title"].startswith("Gate Check"))
    assert gate["link"] == "https://github.com/KofiGilbert/gatecheck"


# --------------------------------------------------------------------------- the line fit
@pytest.mark.parametrize("fill,want", [(0.87, ["fill"]), (0.99, ["trim"])])
def test_a_spill_on_a_short_page_is_filled_never_trimmed(monkeypatch, fill, want):
    """The line fit trimmed five spilling bullets and took a page from 95.5% to 87.7% (a real
    build, 2026-11-10). A trim takes a line off: only a page with lines to spare is trimmed."""
    from types import SimpleNamespace
    from tailoring import linefit as LF
    text = ("Built SQL data management pipelines at CSIR for structured querying and analytical "
            "reporting across the research projects of the lab.")
    p = {"identity": {"name": "T"}, "experience": [{"org": "CSIR", "roles": [
        {"title": "Research Assistant", "bullets": [text]}]}]}
    spill = LF.LineReport(items=[LF.ItemFit("bullet", "CSIR", text, lines=2, last_fill=0.1, slot=0)],
                          left=0.0, right=400.0)
    monkeypatch.setattr(LF, "measure", lambda *_a, **_k: spill)
    modes = []
    real = LF._bullet_targets
    monkeypatch.setattr(LF, "_bullet_targets", lambda it, mode: (modes.append(mode), real(it, mode))[1])
    LF.fit_lines(copy.deepcopy(p), SimpleNamespace(pdf_path="x.pdf", fill_ratio=fill),
                 render=lambda _p: "", compile_fn=lambda _t, _n: SimpleNamespace(pdf_path="y.pdf"),
                 is_clean=lambda _r: True, llm=FakeLLM(), jd_text="", rounds=1,
                 fill_goal=A.FILL_GOAL)
    assert modes == want
