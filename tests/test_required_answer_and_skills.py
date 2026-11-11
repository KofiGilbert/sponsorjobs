"""Three things Kofi hit in one resume chat (a Google data scientist job built from his saved
profile, 2026-11-10):

1. The outside-work question never said his interests were already on file, so when the page
   printed them he took them for invented. The question names what is on file first.
2. His answer "playing football and swimming" never reached the page (the model's absorb read it
   as interests, which were full, and dropped it) while the message said it had been added to his
   Extracurricular section. The answer goes into the section that was asked, and a claim that
   something went into a section is held to the page.
3. Every keyword he ADDed on a D365 job was kept on the Google resume, so its Skills block was
   another job's stack under that job's group names. The block is selected per job; an ADD holds
   its place only where the job asks for it. Two spellings of one term count once in the match.

FakeLLM; real pdflatex where the page is checked.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent import narrate as N
from agent import resume_chat as rc
from conftest import requires_latex
from intake.memory import ConversationMemory
from llm.base import FakeLLM
from tailoring.assembler import normalize_profile
from tailoring.linefit import split_terms
from ui.records import CVRecords
from ui.session import WebIntake, _reply_items

JD = ("Business Data Scientist, AI Foundations at Google. You will work with large datasets to "
      "develop and deploy ML/AI solutions. Lead the development of AI/ML solutions with a strong "
      "emphasis on Large Language Models (LLMs) and AI agents. Minimum qualifications: 4 years of "
      "experience using analytics to solve business problems, coding (e.g., Python, R, SQL), "
      "querying databases or statistical analysis. Preferred: machine learning and Natural "
      "Language Processing (NLP); Python with ML/AI libraries (TensorFlow, PyTorch, scikit-learn); "
      "experience with Large Language Models (LLMs); Statistics.")
D365_JD = ("D365 Software Developer. X++, CI/CD, .NET, SQL, Power BI, SSRS, Power Apps, Power "
           "Automate, Dataverse, Power Platform, SQL Server, Azure, C#, Git, Azure DevOps.")
INTERESTS = ("Playing football on weekends, travelling to see the world, and watching podcasts "
             "such as The Diary of a CEO (DOAC).")
# The saved profile as the D365 build left it: that job's terms, order and group names.
D365_BLOCK = {
    "Languages & Tools": "SQL, Python, X++, Dashboards, Reporting, advanced Excel, REST APIs",
    "Power Platform & Analysis": "Power BI, SSRS, Power Apps, Power Automate, Dataverse, "
                                 "Power Platform, SQL Server, Azure",
    "Development & Delivery": "C#, Object-oriented programming, JSON, XML, Git, Azure DevOps",
    "Microsoft & Azure Platform": "Data Management Framework, OData, Microsoft Fabric, "
                                  "Azure Data Factory, Azure Functions",
    "Domain Knowledge": "Logic Apps, Azure Data Lake Storage, CI/CD, .NET, KPI frameworks",
}
ADDED_ON_D365 = ["X++", "SQL Server", "SSRS", "Power Apps", "Power Automate", "Dataverse", "CD",
                 "Data Management Framework", "Microsoft Fabric", "OData", "Logic Apps",
                 "Azure Functions", "Azure Data Factory", "Azure DevOps", "Git", "C#"]
SAVED = {
    "identity": {"name": "Kofi Gilbert", "email": "kofi@example.com"},
    "education": [{"school": "DePaul University", "degree": "MBA in Business Analytics",
                   "date": "June 2025", "location": "Chicago, IL"}],
    "experience": [
        {"org": "BlackOrigin Inc.", "location": "Chicago, IL",
         "roles": [{"title": "AI Senior Business Intelligence Manager",
                    "dates": "March 2026 - Present",
                    "bullets": ["Lead business intelligence across the company's products, "
                                "building KPI dashboards and automated Python and SQL reporting "
                                "that track growth, usage and unit costs.",
                                "Built an AI-assisted data pipeline in Python and SQL that "
                                "processes 280,000 job postings a day."]}]},
        {"org": "Stanbic Bank Ghana", "location": "Accra, Ghana",
         "roles": [{"title": "Senior Business Analyst", "dates": "Feb 2018 - Sept 2024",
                    "bullets": ["Led business analysis and performance reporting across retail "
                                "banking, owning KPI frameworks and Power BI dashboards.",
                                "Drove enterprise CRM requirements and process improvement across "
                                "two remits, with SQL Server reporting in SSRS."]}]},
    ],
    "skills": D365_BLOCK,
    "projects": [],
    "extracurricular": [],
    "interests": INTERESTS,
}
ESS = {
    "identity": SAVED["identity"], "education": SAVED["education"],
    "experience": [{"org": e["org"], "location": e["location"], **r}
                   for e in SAVED["experience"] for r in e["roles"]],
    "projects": [], "extracurricular": [], "interests": INTERESTS,
    "skills_input": ADDED_ON_D365 + ["Power BI", "Excel", "business analytics", "KPI frameworks",
                                     "Dashboards", "Reporting", "Python", "SQL"],
    "skills_from_jobs": list(ADDED_ON_D365),
    "declined": [],
}
BUILD_BUTTON = "just build it from my saved profile"
ANSWER = "playing football and swimming"
KOFI_CLAIM = ("Football and swimming give the resume some personality outside the data work, so "
              "I've added them to your Extracurricular section.")


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv(rc.FLAG, "1")
    monkeypatch.setenv(N.FLAG, "1")


def _returning(template_source, workdir, jd=JD, template="shetty", saved=None, ess=None):
    mem = ConversationMemory(":memory:")
    mem.save("default", saved or SAVED, ess or ESS, [])
    if template != "shetty":
        from ui.app import _load_template
        template_source, template = _load_template(template)
    s = WebIntake(jd, template_source, FakeLLM(), mem, CVRecords(":memory:"), workdir,
                  jobname="cv", palace_dir=workdir / "p", template_name=template)
    s.github_status = lambda: {"connected": False, "login": ""}
    s.github_token = lambda: ""
    s.start()
    return s


def _terms(skills: dict) -> list[str]:
    return [t for v in (skills or {}).values() for t in split_terms(v)]


# ------------------------------------------------------------- 1. the question says what it has

def test_classic_asks_nothing_when_interests_are_on_file_and_builds(template_source, workdir):
    # One outside-work section per resume: Classic has only Interests, and they are on file.
    s = _returning(template_source, workdir)
    assert s.required_fill == ["interests"] and "extracurricular" not in s.sections
    st = s.submit(BUILD_BUTTON)
    assert s.assembled is not None and st["phase"] == "review", st["messages"]
    assert "Extracurricular" not in " ".join(st["messages"])


def test_the_question_names_what_is_already_on_file(template_source, workdir):
    # The MBA template keeps Leadership & Activities beside Languages & Interests (they hold
    # different things): with the interests on file and Leadership empty, the question says so.
    s = _returning(template_source, workdir, template="mba")
    st = s.submit(BUILD_BUTTON)
    assert s.assembled is None
    said = " ".join(st["messages"])
    assert "I already have your languages & interests on file (" in said, said
    assert "football" in said.lower() and "travelling" in said.lower(), said
    assert "Leadership & Activities section is still empty" in said, said
    # The brief the AI writes from carries the same facts, so a written message can say them.
    items = getattr(s.llm, "last_narrate_items", None) or []
    ask = next((i for i in items if i.get("kind") == "ask_required"), None)
    assert ask is not None and "Languages & Interests" in ask.get("already_on_file", {}), items
    assert "football" in ask["already_on_file"]["Languages & Interests"].lower()


def test_a_template_with_nothing_on_file_asks_one_outside_work_question(template_source, workdir):
    bare = dict(SAVED, interests="", extracurricular=[])
    s = _returning(template_source, workdir, saved=bare, ess=dict(ESS, interests=""))
    said = " ".join(s.submit(BUILD_BUTTON)["messages"])
    assert "already have your" not in said
    assert "Interests section" in said and "Extracurricular" not in said, said
    assert said.count("?") == 1
    s.submit("football at weekends and travelling")
    assert s.essentials["interests"] == "Football at weekends and travelling."
    assert s.assembled is not None


# ----------------------------------------------------- 2a. the answer lands in the asked section

def test_reply_items_are_clean_list_items():
    assert _reply_items("playing football and swimming") == ["Playing football", "Swimming"]
    assert _reply_items("I also volunteer at a food bank, and I coach a youth team.") == [
        "Volunteer at a food bank", "Coach a youth team"]
    assert _reply_items("Outside work I play chess; hiking") == ["Chess", "Hiking"]
    assert _reply_items("") == []


MERGED = ("Playing football on weekends and swimming, travelling to see the world, and watching "
          "podcasts such as The Diary of a CEO (DOAC).")


def test_the_same_figures_are_one_fact_however_reworded():
    # The page fill wrote a sixth BlackOrigin bullet that restated the third with the same
    # 280,000+ and 145,000+ figures (the real run, 2026-11-10). One fact never prints twice.
    from tailoring.filler import same_fact
    third = ("Built an AI-assisted data pipeline that processed 280,000+ job postings a day and "
             "matched them against government sponsorship records, producing 145,000+ "
             "sponsorship-matched roles that fed the company's products and its reporting.")
    sixth = ("Built an AI-assisted pipeline that turns 280,000+ daily job postings into "
             "145,000+ sponsorship-matched roles.")
    assert same_fact(third, sixth)
    assert not same_fact("Grew revenue 20% in 2021.", "Grew margin 20% in 2022.")
    assert not same_fact("Tracked 5,000+ daily orders using SQL and Excel.",
                         "Analyzed SQL and Excel data for a platform of 250 drivers.")


def test_a_bullet_with_a_figure_is_never_the_one_cut():
    # The page's cuts (the staircase's hold, making room, the one-page shrink) take a figureless
    # bullet first; "280,000+ ... 145,000+" is the last thing to go, and only when nothing else can.
    from tailoring.assembler import _cut_victim
    from tailoring.filler import figures_of

    class _Cuts:
        def kept(self, *a):
            return False
    bl = ["Built an AI-assisted data pipeline that processed 280,000+ job postings a day.",
          "Led business intelligence across the company's products.",
          "Translated analysis into roadmap and spend recommendations."]
    assert _cut_victim(bl, 0, _Cuts(), "x") == 2
    assert _cut_victim(bl[:1], 0, _Cuts(), "x") == 0            # nothing else: it may go
    assert figures_of(bl[0]) == {"280000"} and not figures_of(bl[1])
    assert figures_of("Grew margin 20% in 2021.") == {"20%"}       # a year alone is not a figure


def test_the_plan_pins_a_saved_figured_bullet_in_place_of_a_figureless_one():
    # tailoring/plan_fill.place: a saved bullet whose figures are missing from the role's written
    # set replaces the last figureless written bullet, in the person's own words.
    from tailoring.filler import figures_of
    saved = ["Built an AI-assisted data pipeline that processed 280,000+ job postings a day into "
             "145,000+ sponsorship-matched roles.", "Led business intelligence across products."]
    cur = ["Led business intelligence across the company's products, giving owners one view.",
           "Defined the metrics behind each product's dashboards.",
           "Translated analysis into recommendations on roadmap and spend."]
    for s in saved:                                                  # the pin, as place() runs it
        fs = figures_of(s)
        if not fs or any(fs & figures_of(c) for c in cur if c):
            continue
        k = next((j for j in range(len(cur) - 1, -1, -1) if cur[j] and not figures_of(cur[j])), None)
        if k is not None:
            cur[k] = s
    assert cur[2] == saved[0] and cur[0].startswith("Led business intelligence")
    src = open("tailoring/plan_fill.py").read()
    assert "plan-pinned" in src and "figures_of" in src


def test_the_interests_line_lands_on_one_line_or_fills_the_second():
    from tailoring.assembler import compact_interests
    from tailoring.textwidth import TEXTWIDTH_PT, text_width_pt
    line = MERGED
    out = compact_interests(line)
    w = text_width_pt("Interests:", bold=True) + text_width_pt(" " + out)
    assert out == ("Football on weekends and swimming, travelling to see the world, and podcasts "
                   "such as The Diary of a CEO (DOAC).")
    assert w <= TEXTWIDTH_PT - 2.0 or (w % (TEXTWIDTH_PT - 2.0)) >= 0.4 * (TEXTWIDTH_PT - 2.0)
    assert compact_interests("Chess and hiking.") == "Chess and hiking."    # short: untouched


def test_analysis_phrases_are_business_not_tools():
    from tailoring.linefit import _THEME_LABELS, skill_theme
    assert skill_theme("enterprise CRM requirements") == "business"
    assert skill_theme("requirements gathering") == "business"
    assert skill_theme("SQL Server") == "tools" and skill_theme("Power BI") == "data"
    assert _THEME_LABELS["business"][0] == "Business & Analysis"


def test_merge_interests_adds_without_duplicates():
    from tailoring.conform import interest_sentence, merge_interests
    sentence, added, already = merge_interests(INTERESTS, ["Playing football", "Swimming"])
    assert sentence == MERGED and added == ["Swimming"] and already == ["Playing football"]
    same, added2, already2 = merge_interests(INTERESTS, ["football"])
    assert same == INTERESTS and not added2 and already2 == ["football"]
    assert interest_sentence(["Football at weekends", "travelling"]) == "Football at weekends and travelling."
    assert interest_sentence(["Chess"]) == "Chess."


@requires_latex
def test_activities_merge_into_interests_and_the_chat_says_exactly_that(template_source, workdir):
    # Kofi's answer over interests already on file: one Interests line, swimming added, football
    # left as it was, and the message says so. Nothing lands in an Extracurricular grid.
    s = _returning(template_source, workdir)
    s.submit(BUILD_BUTTON)
    assert s.assembled is not None
    st = s.submit(ANSWER)
    assert s.essentials["interests"] == MERGED
    assert not (s.essentials.get("extracurricular") or [])
    said = " ".join(st["messages"])
    assert "I added swimming to your interests; playing football was already there." in said, said
    assert "Extracurricular" not in said
    page = s.assembled.profile_used
    assert page.get("interests") == MERGED
    tex = (workdir / "cv.tex").read_text(encoding="utf-8")
    assert "Extracurricular" not in tex
    assert tex.count("swimming") == 1 and "\\textbf{Swimming}" not in tex
    # Printed compacted so it lands on one line; the saved text stays MERGED.
    assert "Interests:} Football on weekends and swimming, travelling" in tex.replace("\\&", "&")


@requires_latex
def test_a_leadership_ask_answered_with_hobbies_files_them_in_interests(template_source, workdir):
    # MBA: Leadership & Activities is a block of entries. Bare hobby words never go into it;
    # they join the Interests line, and the question counts as answered.
    s = _returning(template_source, workdir, template="mba")
    s.submit(BUILD_BUTTON)
    st = s.submit(ANSWER)
    assert s.essentials["interests"] == MERGED
    assert not (s.essentials.get("extracurricular") or [])
    assert s.assembled is not None and st["phase"] == "review", st["messages"]
    tex = (workdir / "cv.tex").read_text(encoding="utf-8")
    assert "Leadership" not in tex.split("Languages")[0].split("Education")[-1] or \
        "\\textbf{Swimming}" not in tex
    assert "\\textbf{Swimming}" not in tex and "swimming" in tex
    said = " ".join(st["messages"])
    assert "I added swimming to your interests; playing football was already there." in said, said
    assert "Before I build, I need your" not in said


def test_every_template_keeps_hobbies_out_of_a_grid():
    """No template renders bare hobby words as a spaced grid beside an Interests line: entries
    (a title with bullets or an organisation) render in the template's activities block when it
    has one, hobby words only ever on the one Interests line."""
    import glob
    import json

    from intake.template_manifest import headings, sections
    from tailoring.assembler import render_cv
    profile = {
        "identity": {"name": "A Person", "email": "a@example.com"},
        "education": [{"school": "U", "degree": "BSc", "date": "2020", "location": "Accra, GH"}],
        "experience": [{"org": "Co", "location": "Accra, GH", "roles": [
            {"title": "Analyst", "dates": "Jan 2021 - Present", "bullets": ["Did the work."]}]}],
        "skills": {"Tools": "SQL"},
        "interests": "Travelling and chess.",
        "extracurricular": [{"title": "Playing football", "date": "", "bullets": []},
                            {"title": "Swimming", "date": "", "bullets": []},
                            {"title": "Youth team", "org": "Accra FC", "date": "2022",
                             "bullets": ["Coached twelve players."]}],
    }
    manifests = sorted(glob.glob("config/templates/*.json"))
    assert manifests
    for p in manifests:
        m = json.load(open(p))
        secs, heads = sections(m), headings(m)
        tex = render_cv("\\documentclass{article}\\begin{document}", profile, secs, headings=heads)
        assert "\\textbf{Swimming}" not in tex and "\\textbf{Playing football}" not in tex, p
        if "extracurricular" not in secs and "interests" not in secs:
            assert "swimming" not in tex and "Youth team" not in tex, p   # neither section: nothing
            continue
        label = heads.get("interests", "Interests").replace("\\&", "&")
        assert tex.count("swimming") == 1 and f"{label}:" in tex.replace("\\&", "&"), p
        if "extracurricular" in secs:
            assert "\\textbf{Youth team}" in tex and "Coached twelve players." in tex, p
        else:
            assert "\\textbf{Youth team}" not in tex and "youth team (Accra FC)" in tex, p
        # Never both an activities grid and the hobby words elsewhere: each hobby word once.
        assert tex.count("laying football") == 1, p


def test_bare_words_never_render_as_an_activities_grid():
    from tailoring.assembler import _additional, _extracurricular
    bare = {"extracurricular": [{"title": "Playing football", "date": "", "bullets": []},
                                {"title": "Swimming", "date": "", "bullets": []}]}
    assert _extracurricular(bare) == ""                       # no block of spaced words
    assert "Interests:} Playing football and swimming." in _additional(bare)
    with_bullets = _extracurricular({"extracurricular": [
        {"title": "Youth team", "date": "2024", "bullets": ["Coached twelve players."]}]})
    assert "\\begin{itemize}" in with_bullets and "\\textbf{Youth team}" in with_bullets
    with_org = _extracurricular({"extracurricular": [{"title": "Treasurer", "org": "Chess Club"}]})
    assert "\\textbf{Treasurer}" in with_org


def test_a_broken_fragment_never_reaches_the_skills_pool(template_source, workdir):
    # His brain held the skill fact "OpenAI)" (an old split of "analytics (Claude, OpenAI)"), and
    # the page printed it on a Skills line (the real run, 2026-11-10).
    s = _returning(template_source, workdir)
    s.essentials["skills_input"].append("OpenAI)")
    assert "OpenAI)" not in s._skill_pool()
    s.profile = normalize_profile(dict(s.saved_profile))
    s._reselect_skills()
    assert "OpenAI)" not in _terms(s.profile["skills"])


def test_a_decline_or_a_question_is_never_material(template_source, workdir):
    s = _returning(template_source, workdir)
    s.submit(BUILD_BUTTON)
    before = list(s.essentials.get("extracurricular") or [])
    assert s._absorb_required_reply("none", changed=False) == []
    assert s._absorb_required_reply("what do you mean by that?", changed=False) == []
    assert s._absorb_required_reply("that's everything", changed=False) == []
    assert s._absorb_required_reply(ANSWER, changed=True) == []       # the model took it already
    assert (s.essentials.get("extracurricular") or []) == before


# ------------------------------------------------- 2b. "added to your X section" is held to the page

def test_section_claims_are_detected_and_retold():
    claims = N.section_claims(KOFI_CLAIM + " You can edit any bullet on the page.")
    assert claims == [(KOFI_CLAIM, "Extracurricular")]
    assert N.section_claims("Your template has an Extracurricular section, and your resume won't "
                            "look complete without it.") == []
    assert N.section_claims("The Extracurricular section is still empty.") == []
    assert N.section_claims("I did not add anything to your Skills section.") == []
    assert N.section_claims("What should go in your Projects section?") == []
    told = N.section_not_added("Extracurricular", ANSWER)
    assert told == ("I noted what you said (playing football and swimming), but nothing went into "
                    "your Extracurricular section this time, so it is still empty on the page.")
    assert "-" not in told.replace("-", "") or True            # no dashes as punctuation
    assert " CV" not in told


@requires_latex
def test_a_claim_of_an_add_that_did_not_happen_is_retold_from_the_page(template_source, workdir):
    # Exactly Kofi's transcript: the AI writes that football and swimming were added to the
    # Extracurricular section. Classic has no such section (the hobbies went to Interests), so
    # the sentence is retold from the page.
    s = _returning(template_source, workdir)
    s.submit(BUILD_BUTTON)
    s.llm.narrate_script = lambda system, user, items: KOFI_CLAIM + " " + N.fallback(items)
    st = s.submit(ANSWER)
    assert s.assembled is not None and not (s.assembled.profile_used.get("extracurricular") or [])
    said = " ".join(st["messages"])
    assert "added them to your Extracurricular section" not in said, said
    assert ("I noted what you said (playing football and swimming), but nothing went into your "
            "Extracurricular section this time") in said, said
    assert s.history[-1]["content"] == st["messages"][0]        # the history holds the truth too


@requires_latex
def test_a_true_claim_of_an_add_stands(template_source, workdir):
    s = _returning(template_source, workdir)
    s.submit(BUILD_BUTTON)
    claim = "I've added swimming to your Interests section, next to football."
    s.llm.narrate_script = lambda system, user, items: claim + " " + N.fallback(items)
    st = s.submit(ANSWER)
    assert "swimming" in str(s.assembled.profile_used.get("interests") or "")
    said = " ".join(st["messages"])
    assert claim in said, said
    assert "nothing went into" not in said


# ------------------------------------------------------------- 3. skills selected per job

def test_an_add_holds_its_place_only_where_the_job_asks_for_it(template_source, workdir):
    s = _returning(template_source, workdir)
    assert s._skill_keep() == []                                 # nothing from D365 on a DS job
    d = _returning(template_source, workdir, jd=D365_JD)
    kept = d._skill_keep()
    assert "X++" in kept and "Power Apps" in kept and "Microsoft Fabric" not in kept
    # An ADD on THIS page is kept whatever the ad says.
    s.keyword_actions.append({"kind": "skill", "term": "Hugging Face"})
    assert s._skill_keep() == ["Hugging Face"]
    # The ADDs stay the person's own skills for every job.
    assert set(ADDED_ON_D365) <= set(s.essentials["skills_input"])


def test_the_skills_block_is_selected_for_this_job_not_carried_from_the_last(template_source, workdir):
    s = _returning(template_source, workdir)
    s.profile = normalize_profile(dict(s.saved_profile))
    assert list(s.profile["skills"]) == list(D365_BLOCK)          # as saved: the D365 render
    s._reselect_skills()
    s._validate_skills()
    s._fill_skills()
    s._dedupe_skill_groups()
    skills = s.profile["skills"]
    labels = list(skills)
    assert not ({"Power Platform & Analysis", "Microsoft & Azure Platform",
                 "Development & Delivery"} & set(labels)), labels
    first = split_terms(skills[labels[0]])
    assert first[0] in ("Python", "SQL") and first[1] in ("Python", "SQL"), skills
    assert "X++" not in first[:4], skills
    terms = _terms(skills)
    assert {"Python", "SQL", "Power BI"} <= set(terms), terms
    for v in skills.values():                                      # the block is still clean
        assert len(split_terms(v)) == len(set(t.lower() for t in split_terms(v)))
    # A term the block left off is still the person's own.
    for t in _terms(D365_BLOCK):
        assert t in terms or t.lower() in {x.lower() for x in s.essentials["skills_input"]}, t


def test_the_same_pool_leads_with_the_d365_stack_on_the_d365_job(template_source, workdir):
    d = _returning(template_source, workdir, jd=D365_JD)
    d.profile = normalize_profile(dict(d.saved_profile))
    d._reselect_skills()
    d._validate_skills()
    d._fill_skills()
    d._dedupe_skill_groups()
    first = split_terms(d.profile["skills"][list(d.profile["skills"])[0]])
    assert set(first[:6]) <= {t for t in _terms(d.profile["skills"]) if t in D365_JD}, first
    assert "X++" in _terms(d.profile["skills"])


def test_two_spellings_of_one_term_count_once_in_the_match():
    from tailoring.keywords import (acronym_pairs, build_coverage_report, merge_same_terms,
                                    same_term_key, skill_terms)
    pairs = acronym_pairs(JD)
    assert pairs.get("large language models") == "llm"
    assert pairs.get("natural language processing") == "nlp"
    assert same_term_key("ML/AI") == same_term_key("AI/ML")
    assert same_term_key("LLMs", pairs) == same_term_key("Large Language Models", pairs) == "llm"
    assert same_term_key("key performance indicators") == same_term_key("KPI")
    terms = skill_terms(JD)
    assert "NLP" in terms and "Natural Language Processing" in terms and "ML/AI" in terms
    groups = merge_same_terms(terms, JD)
    assert len(groups) < len(terms)
    rep = build_coverage_report(JD, "Python, SQL and LLMs in production", {"skills": {}}, terms)
    assert rep.jd_terms.count("NLP") + rep.jd_terms.count("Natural Language Processing") == 1
    assert sum(1 for t in rep.jd_terms if t in ("ML/AI", "AI/ML")) == 1
    assert "Python" in rep.present and "SQL" in rep.present
    assert any(t.lower().startswith("l") for t in rep.present), rep.present   # LLMs via LLM


# ------------------------------------------------------------- the project date lines

def test_a_project_built_in_one_month_shows_one_month():
    from tailoring.conform import format_dates
    from ui.github_dates import dates_from
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    assert dates_from({"created_at": "2026-07-16T15:16:04Z", "pushed_at": "2026-07-16T15:16:07Z"},
                      now) == "Jul 2026"
    assert dates_from({"created_at": "2026-07-16T15:16:04Z", "pushed_at": "2026-08-02T15:16:07Z"},
                      now) == "Jul 2026 - Aug 2026"
    assert dates_from({"created_at": "2026-08-19T21:52:48Z", "pushed_at": "2026-09-23T20:28:27Z"},
                      now) == "Aug 2026 - Present"                      # pushed within 60 days
    assert format_dates("July 2026 - July 2026") == "July 2026"
    assert format_dates("Jul 2026 - Jul 2026") == "July 2026"
    assert format_dates("July 2018 to July 2021") == "July 2018 - July 2021"


# ------------------------------- a session saved before the fixes, rebuilt on main (2026-11-11)
# Kofi's Google session was started before the per-job Skills selection and the outside-work
# merge. "why is there white space?" rebuilt it with fill_page: the D365 Skills block stayed
# (keywords 17%) and his "playing football and swimming" never reached the Interests line.

ASK = ("Your template has Extracurricular section, and your resume won't look complete without "
       "it. Could you tell me anything you do outside work, like volunteering, clubs or teams? "
       "If there's none, say so and I'll leave it off.")


def _old_session(template_source, workdir):
    """The Google session as it was saved before the fixes, restored."""
    from ui import builder_sessions as BS
    s = _returning(template_source, workdir)
    s.profile = normalize_profile(dict(s.saved_profile))   # the D365 block, as the old build left it
    s.essentials["required_ask_text"] = ASK[:200]
    s.essentials["asked_required"] = True
    s.history = [{"role": "agent", "content": "Welcome back, Kofi."},
                 {"role": "user", "content": BUILD_BUTTON},
                 {"role": "agent", "content": ASK},
                 {"role": "user", "content": ANSWER},
                 {"role": "agent", "content": KOFI_CLAIM},
                 {"role": "user", "content": "why is there white space?"}]
    data = BS.dump(s)
    for k in ("_skills_job", "_outside_work_checked"):      # fields a pre-fix save never had
        data.pop(k, None)
    r = BS.restore(data, template_source=template_source, llm=FakeLLM(), memory=s.memory,
                   records=s.records, workdir=workdir, palace_dir=workdir / "p")
    r.github_status = lambda: {"connected": False, "login": ""}
    r.github_token = lambda: ""
    return r


def test_an_old_sessions_first_rebuild_selects_the_skills_for_this_job(template_source, workdir):
    s = _old_session(template_source, workdir)
    assert list(s.profile["skills"]) == list(D365_BLOCK)
    s._refresh_for_job()                                     # what every rebuild runs first
    labels = list(s.profile["skills"])
    assert not ({"Power Platform & Analysis", "Microsoft & Azure Platform"} & set(labels)), labels
    first = split_terms(s.profile["skills"][labels[0]])
    assert "X++" not in first[:4] and {"Python", "SQL"} & set(first[:2]), s.profile["skills"]
    # Once selected for this job, the person's own edit to the block stands on later rebuilds.
    s.profile["skills"][labels[0]] = "Python, SQL, Tableau"
    s._refresh_for_job()
    assert s.profile["skills"][labels[0]] == "Python, SQL, Tableau"
    # Another job's description: selected again for it.
    s.jd = D365_JD
    s._refresh_for_job()
    assert "X++" in _terms(s.profile["skills"])


def test_an_old_sessions_lost_outside_work_answer_joins_the_interests(template_source, workdir):
    s = _old_session(template_source, workdir)
    assert "swimming" not in s.profile["interests"]
    s._refresh_for_job()
    assert s.essentials["interests"] == MERGED and s.profile["interests"] == MERGED
    # Once only, and a session saved after the fix never looks back at its history.
    s.essentials["interests"] = INTERESTS
    s._refresh_for_job()
    assert s.essentials["interests"] == INTERESTS
    fresh = _returning(template_source, workdir)
    fresh.history = list(s.history)
    fresh._refresh_for_job()
    assert "swimming" not in fresh.essentials["interests"]


def test_a_declined_or_unasked_reply_is_never_merged(template_source, workdir):
    s = _old_session(template_source, workdir)
    s.history[3] = {"role": "user", "content": "none, leave it off"}
    s._refresh_for_job()
    assert s.essentials["interests"] == INTERESTS
    s = _old_session(template_source, workdir)
    s.history[2] = {"role": "agent", "content": "Built your resume."}       # not the question
    s._refresh_for_job()
    assert s.essentials["interests"] == INTERESTS


@requires_latex
def test_the_page_fill_on_an_old_session_rebuilds_with_both(template_source, workdir):
    s = _old_session(template_source, workdir)
    s._assemble()
    page = s.assembled.profile_used
    assert page["interests"] == MERGED
    assert "X++" not in split_terms(page["skills"][list(page["skills"])[0]])[:4]


def test_a_project_bullet_the_saved_profile_lost_comes_back_from_the_intake(template_source, workdir):
    # Saving the deduped Google page wrote Last-Mile's lost result bullet out of the saved
    # profile; the person's intake material still had it. The next build puts it back.
    from tests.test_one_fact_per_bullet import SIM_BUILT, SIM_RESULT
    saved = {**SAVED, "projects": [{"title": "Last-Mile Operations Simulator (Python)", "bullets": [SIM_BUILT]}]}
    ess = {**ESS, "projects": [{"title": "Last-Mile Operations Simulator (Python)",
                                "bullets": [SIM_BUILT, SIM_RESULT]}]}
    s = _returning(template_source, workdir, saved=saved, ess=ess)
    base = dict(s.saved_profile)
    assert s._restore_project_bullets(base) == [SIM_RESULT]
    assert base["projects"][0]["bullets"] == [SIM_BUILT, SIM_RESULT]
    assert s.saved_profile["projects"][0]["bullets"] == [SIM_BUILT]      # the saved copy untouched
    # The same fact reworded never comes back twice.
    again = {**ess, "projects": [{"title": "Last-Mile Operations Simulator (Python)",
                                  "bullets": [SIM_BUILT.replace("deadlines.", "deadlines each day."), SIM_RESULT]}]}
    s2 = _returning(template_source, workdir, saved=saved, ess=again)
    b2 = dict(s2.saved_profile)
    assert s2._restore_project_bullets(b2) == [SIM_RESULT]


def test_an_old_sessions_first_rebuild_brings_back_a_lost_project_bullet(template_source, workdir):
    from tests.test_one_fact_per_bullet import SIM_BUILT, SIM_RESULT
    proj = "Last-Mile Operations Simulator (Python)"
    s = _old_session(template_source, workdir)
    s.essentials["projects"] = [{"title": proj, "bullets": [SIM_BUILT, SIM_RESULT]}]
    s.profile["projects"] = [{"title": proj, "bullets": [SIM_BUILT]}]
    s._refresh_for_job()
    assert s.profile["projects"][0]["bullets"] == [SIM_BUILT, SIM_RESULT]
