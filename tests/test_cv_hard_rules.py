"""Regression tests for the user's CV hard rules (2026-07):

  1. Extracurricular + Interests are MANDATORY and collected BEFORE the first build.
  2. Skills show 4-5 dense categories (never 2 skinny ones), packed to the page.
  3. Project links are a minimal inline marker, never a blue-title takeover.

See memories complete-cv-fills-page, skills-five-categories-fill-page, project-links-no-blue-title.
"""

from __future__ import annotations

from intake.memory import ConversationMemory
from tailoring.assembler import TARGET_FILL, _shrink_once, assemble_cv
from ui.records import CVRecords
from ui.session import WebIntake
from conftest import requires_latex

JD = ("Data Analyst. Required skills: SQL, Python, dashboards, statistics, data pipelines, "
      "machine learning, Tableau, Power BI, Excel, forecasting, ETL, data modeling.")
SKELETON = (
    "I'm Kwame Asante, kwame@example.com. B.S. in Statistics at University of Ghana, "
    "graduated June 2019. Data Analyst at BlackOrigin in Accra, Ghana since Feb 2021. "
    "Analyst at Stanbic Bank in Accra, Ghana from Jan 2018 to Dec 2020. "
    "Skills: SQL, Python, Tableau, Power BI, statistics, data pipelines, machine learning. "
    "No github, no blog, no linkedin. No address. Skip courses. No projects."
)
EXTRAS = "Outside work I volunteer coaching a youth team, and I enjoy chess and hiking."


def _s(template_source, fake_llm, workdir, memory=None):
    return WebIntake(JD, template_source, fake_llm, memory or ConversationMemory(":memory:"),
                     CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")


# A returning person whose SAVED profile has real work history but no extracurricular /
# interests — the shape that produced the shipped half page.
SAVED_NO_EXTRAS = {
    "identity": {"name": "Kwame Asante", "email": "kwame@example.com"},
    "education": [{"school": "University of Ghana", "degree": "B.S. Statistics",
                   "date": "June 2019", "location": "Accra, Ghana"}],
    "experience": [{"org": "BlackOrigin", "location": "Accra, Ghana",
                    "roles": [{"title": "Data Analyst", "dates": "Feb 2021 - Present",
                               "bullets": ["Built Tableau dashboards for the operations team."]}]}],
    "skills": {"Data & Analytics": "SQL, Python, Tableau", "Domain": "statistics, forecasting"},
}


def _returning(template_source, fake_llm, workdir):
    mem = ConversationMemory(":memory:")
    mem.save("default", SAVED_NO_EXTRAS, {}, [])
    return _s(template_source, fake_llm, workdir, memory=mem)


# -- Rule 1: extracurricular + interests gate the build ---------------------- #

@requires_latex
def test_extracurricular_and_interests_are_collected_before_the_build(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    st = s.submit(SKELETON)
    assert st["phase"] == "chatting"                     # NOT built while sections are open
    assert s.assembled is None
    msg = " ".join(st["messages"]).lower()
    assert any(w in msg for w in ("outside", "volunteer", "club", "team", "interest", "hobb"))
    st2 = s.submit(EXTRAS)                                # provided -> builds
    assert st2["phase"] == "review"
    assert s.profile.get("extracurricular") and str(s.profile.get("interests") or "").strip()


@requires_latex
def test_explicit_decline_lets_the_build_proceed(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    st = s.submit("I don't have any extracurriculars and no hobbies to add.")
    assert st["phase"] == "review"                       # explicit decline is the escape hatch


@requires_latex
def test_build_from_saved_asks_for_extras_before_building_not_after(template_source, fake_llm, workdir):
    """The half page that shipped: a returning person's saved profile had no extracurricular
    or interests, and build_from_saved built FIRST and asked afterwards. The sections are
    collected BEFORE the first build, so the person never sees the half page at all."""
    s = _returning(template_source, fake_llm, workdir)
    s.start()
    st = s.build_from_saved()
    assert s.assembled is None, "built a CV before collecting the page-filling sections"
    assert st["phase"] == "chatting"
    msg = " ".join(st["messages"]).lower()
    assert any(w in msg for w in ("outside", "volunteer", "club", "team", "interest", "hobb"))


@requires_latex
def test_just_build_it_still_collects_the_page_filling_sections(template_source, fake_llm, workdir):
    """"Just build it" opts out of the OPTIONAL extras, not into a half page. The required
    sections are still asked for once; an explicit decline remains the escape hatch."""
    s = _returning(template_source, fake_llm, workdir)
    s.start()
    st = s.submit("just build it now")
    assert s.assembled is None, "override skipped the page-filling gate and built a half page"
    assert st["phase"] == "chatting"


@requires_latex
def test_unattended_autoapply_is_the_only_path_past_the_gate(template_source, fake_llm, workdir):
    """Auto-apply runs with nobody there to answer, so it alone may build without asking.
    It must stay the ONLY exemption, or the gate is decorative."""
    s = _returning(template_source, fake_llm, workdir)
    s.start()
    s.essentials["override"] = True      # exactly what autoapply_run sets, and why
    s.essentials["unattended"] = True
    s.build_from_saved()
    assert s.assembled is not None, "unattended auto-apply must still produce a CV"


# -- Rule 2: 4-5 packed skill categories, and stated skills are captured ----- #

def test_stated_skills_are_captured_into_skills_input(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s._apply_side_channels("Skills: Python, SQL, Tableau, requirements analysis, machine learning.")
    si = [x.lower() for x in (s.essentials.get("skills_input") or [])]
    for term in ("python", "sql", "tableau", "requirements analysis", "machine learning"):
        assert term in si
    # A normal sentence is NOT scraped as skills.
    s2 = _s(template_source, fake_llm, workdir)
    s2._apply_side_channels("I worked with stakeholders across many teams at the bank.")
    assert not (s2.essentials.get("skills_input") or [])


@requires_latex
def test_built_cv_has_four_to_five_skill_categories(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    s.submit(EXTRAS)
    n = len(s.profile.get("skills") or {})
    assert 4 <= n <= 5, f"expected 4-5 skill categories, got {n}: {list((s.profile.get('skills') or {}).keys())}"


# -- Rule 1, measured: "fills the page" is a number, not a proxy ------------- #

@requires_latex
def test_a_thin_profile_is_reported_underfull_not_clean(template_source, workdir):
    """Every page-filling rule used to be tested through a PROXY (is extracurricular
    present? are there 4-5 skill categories?) and never against the rendered page. That is
    how a half page passed every check: it compiles, it is one page, it has no overfull, so
    "clean" was true of it. Fill is now measured, so the output itself is the assertion."""
    res = assemble_cv(template_source, dict(SAVED_NO_EXTRAS), JD, None, workdir,
                      jobname="thin", tailor=False)
    assert res.compile.pages == 1 and res.compile.overfull_count == 0   # passes the OLD checks
    assert res.fill_ratio is not None, "page fill was not measured"
    assert res.fill_ratio < TARGET_FILL
    assert res.underfull and res.status == "underfull", \
        f"a {res.fill_ratio:.0%} page was reported {res.status!r}"


@requires_latex
def test_every_shipped_template_fills_its_own_page(workdir):
    """A template we ship IS the promise: whatever its John Doe sample looks like is what
    the person expects their CV to look like. A template whose own sample stops halfway
    breaks the rule before a user profile is even involved, and its picker preview shows
    them a half page as the standard."""
    from pathlib import Path
    from tailoring.compiler import compile_tex, parse_fill
    short = []
    for tex in sorted(Path("config").glob("resume_*.tex")):
        src = tex.read_text(encoding="utf-8").replace(
            r"\begin{document}",
            r"\begin{document}" "\n"
            r"\AtEndDocument{\par\typeout{TAILORFILL=\the\pagetotal:\the\textheight}}")
        res = compile_tex(src, workdir, jobname=tex.stem)
        fill = parse_fill(res.log)
        if res.pages != 1 or fill is None or fill < TARGET_FILL:
            short.append(f"{tex.name}: pages={res.pages} fill="
                         f"{'n/a' if fill is None else format(fill, '.0%')}")
    assert not short, "templates that don't fill their own page: " + "; ".join(short)


@requires_latex
def test_the_reference_template_fills_its_page(template_source, workdir):
    """The template's own John Doe sample is the standard the person is promised: it packs
    ~98% of the text block. If this drops below the floor, the floor is wrong or the
    template regressed — either way the promise broke."""
    from tailoring.compiler import compile_tex, parse_fill
    res = compile_tex(template_source.replace(
        r"\begin{document}",
        r"\begin{document}" "\n"
        r"\AtEndDocument{\par\typeout{TAILORFILL=\the\pagetotal:\the\textheight}}"),
        workdir, jobname="reference")
    fill = parse_fill(res.log)
    assert res.pages == 1 and fill is not None
    assert fill >= TARGET_FILL, f"the reference template only fills {fill:.0%} of its page"


def test_fill_is_never_trusted_on_a_multi_page_compile():
    r"""\pagetotal reports the LAST page's height, so on a 2-page run it describes the
    spill, not the document. Reading it as the document's fill would make an overflowing
    CV look underfull and send the engine asking for MORE material."""
    from tailoring.compiler import parse_fill
    two_page_log = ("Output written on cv.pdf (2 pages, 1234 bytes).\n"
                    "TAILORFILL=100.0pt:775.0pt\n")
    assert parse_fill(two_page_log) is not None      # the probe still parses
    # ...but assemble_cv must discard it, because pages != 1.
    from tailoring.assembler import AssembleResult
    assert AssembleResult(ok=False, status="overflow", tex_source="", pdf_path=None,
                          coverage=None, compile=None, attempts=1, profile_used={},
                          fill_ratio=None).underfull is False


# -- a short page is met with the CHEAPEST true material, not the dearest ---- #

def test_the_interviewer_is_told_who_it_is_talking_to():
    """ask_enrich knew only the TARGET role, so it could only ask generic questions ("tell
    me an accomplishment"), which ask a person to search their whole life and get nothing
    back. That is why the page stayed at 50.6% while the person had plenty to say.

    This is deliberately the alternative to a bullet GENERATOR: a generator writes the claim
    and invites a tired applicant to accept it, and our user is applying for visa-sponsored
    roles where a misrepresentation attaches to an immigration petition. A question cannot
    fabricate. It just has to be worth answering, which needs context."""
    from intake.memory import ConversationMemory
    from llm.base import FakeLLM
    from ui.records import CVRecords
    s = WebIntake(JD, "x", FakeLLM(), ConversationMemory(":memory:"), CVRecords(":memory:"),
                  ".", jobname="cv", palace_dir=None)
    s.profile = {
        "identity": {"name": "Kofi Gilbert"},
        "education": [{"school": "DePaul University", "degree": "MBA Business Analytics"}],
        "experience": [{"org": "Stanbic Bank Ghana",
                        "roles": [{"title": "Product Manager", "dates": "Feb 2018 - Sept 2024",
                                   "bullets": ["Reviewed credit relationships."]}]}],
    }
    who = s._who_we_are_asking()
    assert who["name"] == "Kofi"
    assert who["roles"][0]["org"] == "Stanbic Bank Ghana"
    assert who["roles"][0]["dates"] == "Feb 2018 - Sept 2024"   # lets it say "over six years"
    # What's already on the CV goes too, so it doesn't ask for something already there.
    assert who["roles"][0]["already_said"] == ["Reviewed credit relationships."]
    assert who["education"][0]["school"] == "DePaul University"


def test_a_short_page_asks_for_cheap_certain_material_before_accomplishments():
    """A short page used to be met with "another accomplishment", the most EXPENSIVE thing
    a person can produce: they must reconstruct a metric from years ago, and it is the one
    question where a tired applicant is most tempted to round up. Meanwhile coursework,
    certifications and honors sat unasked: trivially recallable, impossible to fabricate by
    accident, and dense on the page. That is exactly how resume_summary went 57% -> 90%.

    Research: padding a thin page with fluff "achieves nothing but a recruiter's eye-roll",
    so the goal is more KINDS of true things, not more words about the same thing."""
    from intake.memory import ConversationMemory
    from llm.base import FakeLLM
    from ui.records import CVRecords
    s = WebIntake(JD, "x", FakeLLM(), ConversationMemory(":memory:"), CVRecords(":memory:"),
                  ".", jobname="cv", palace_dir=None)
    s.profile = {"education": [{"school": "S", "degree": "B.S.", "date": "2019"}],  # no courses
                 "experience": [{"org": "BlackOrigin", "roles": [{"title": "T", "bullets": ["one"]}]}]}
    gaps = s._cheapest_gaps_first()
    assert "course" in gaps[0].lower(), f"the first ask is not the cheapest: {gaps}"
    assert "accomplishment" in gaps[-1].lower(), f"the dearest ask is not last: {gaps}"
    # ...and the expensive one is still THERE. It's the strongest thing a reader sees; it
    # just isn't the opening question.
    assert any("accomplishment" in g.lower() for g in gaps)


def test_saying_thats_everything_stops_us_calling_the_cv_light():
    """They've said there is nothing left. Insisting past that is nagging, and a padded page
    reads worse than a short honest one, so there is nothing to win by pushing."""
    from intake.memory import ConversationMemory
    from llm.base import FakeLLM
    from ui.records import CVRecords
    s = WebIntake(JD, "x", FakeLLM(), ConversationMemory(":memory:"), CVRecords(":memory:"),
                  ".", jobname="cv", palace_dir=None)
    s.profile = {"experience": [{"org": "X", "roles": [{"title": "T", "drafted_bullets": True}]}]}
    assert s._light_extra() is not None          # light while there's more to give
    s.essentials["enrich_done"] = True
    assert s._light_extra() is None, "still calling the CV light after they said they're done"


# -- shrink-to-fit protects the mandatory sections --------------------------- #

def test_shrink_never_drops_the_extracurricular_section():
    """Overflow shrinking must never remove Extracurricular (a mandatory section) — it
    only shortens it; the heading + at least one entry always survive."""
    profile = {
        "skills": {"A": "x", "B": "y", "C": "z", "D": "w"},
        "experience": [{"org": "X", "roles": [{"title": "T", "dates": "2020", "bullets": ["one"]}]}],
        "projects": [{"org": "P", "roles": [{"title": "R", "dates": "2021", "bullets": ["one"]}]}],
        "education": [{"school": "S", "degree": "B.S.", "date": "2019"}],
        "extracurricular": [{"title": "Volunteer", "bullets": ["Did a real thing once."]}],
    }
    for _ in range(40):
        if not _shrink_once(profile):
            break
    assert profile.get("extracurricular"), "extracurricular section was dropped by shrink"


# -- Rule 2, at the source: two labels can't satisfy a 4-5 category rule ----- #

def test_timezones_and_boilerplate_are_not_skills_the_person_lacks():
    """A real Clipster posting produced "UFC" (a client), "UTC+2" and "PM" (from "10am to
    2pm UTC+2") as "JD terms your profile doesn't show yet". Uppercase-and-short is not the
    same as a skill, and the advice that follows ("your résumé is short of UTC+2") is
    nonsense. The CV engine also treats a JD term as something to align to."""
    from tailoring.keywords import _is_acronym_or_symbol
    for junk in ("UTC+2", "PM", "AM", "EMEA", "401K", "VP", "ROI", "EST"):
        assert not _is_acronym_or_symbol(junk), f"{junk} was treated as a skill"
    # The cure must not kill the patient.
    for real in ("SQL", "AWS", "GCP", "C++", "C#", "ETL", "API", "NLP"):
        assert _is_acronym_or_symbol(real), f"{real} stopped being a skill"


def test_the_vocabulary_can_read_a_non_tech_jd():
    """Skills lines shipped three words wide, and the packer was not at fault: it had
    nothing to pack. supported_skills() can only offer terms the vocabulary RECOGNISES,
    and the vocabulary was ~83 almost entirely technical terms, so a banking JD naming
    "credit analysis, underwriting, loan portfolio, covenant compliance, credit memos"
    matched exactly two. §8 wants the JD's own phrasing on the page, which a vocabulary
    that cannot see the JD's words can never do."""
    from tailoring.keywords import skill_terms
    banking = ("Credit Risk Analyst. Commercial lending, credit analysis, underwriting, "
               "loan portfolio management, covenant compliance, credit memos, credit scoring.")
    consulting = ("Consultant. Market sizing, operating model design, business case, "
                  "stakeholder management, process improvement, change management.")
    assert len(skill_terms(banking)) >= 6, skill_terms(banking)
    assert len(skill_terms(consulting)) >= 5, skill_terms(consulting)


def test_new_vocabulary_terms_spread_across_categories():
    """A term is only useful if it lands somewhere a reader expects. Dumping every new
    finance word into Domain would rebuild the two-skinny-lines problem under new labels."""
    from tailoring.keywords import classify_skill
    assert classify_skill("Underwriting") == "Methods & Frameworks"
    assert classify_skill("Commercial Lending") == "Domain Knowledge"
    assert classify_skill("Market Sizing") == "Methods & Frameworks"


def test_classify_skill_spans_all_four_categories():
    """classify_skill only ever answered "Computing" or "Knowledge", so every
    deterministic path was capped at TWO skinny lines however much real material the
    person had. A 4-5 category rule is unsatisfiable with a 2-value classifier."""
    from tailoring.keywords import SKILL_CATEGORIES, classify_skill
    assert len(SKILL_CATEGORIES) >= 4
    got = {classify_skill(t) for t in
           ("Python", "AWS", "Machine Learning", "Risk Management")}
    assert got == set(SKILL_CATEGORIES), f"terms collapsed into {got}"


def test_classify_skill_agrees_with_the_labels_we_ask_the_model_for():
    """The real model is told to group skills under these exact labels. When the
    deterministic path used a rival vocabulary, its rows appeared BESIDE the model's
    saying the same thing ("Computing" next to "Languages & Tools") instead of merging."""
    import re
    from pathlib import Path
    from tailoring.keywords import SKILL_CATEGORIES
    src = Path("llm/anthropic_client.py").read_text(encoding="utf-8")
    # Join adjacent string literals the way Python does, so a label wrapped across two
    # source lines still reads as the one phrase the model actually receives.
    prompt = re.sub(r'"\s*\n\s*"', "", src)
    for label in SKILL_CATEGORIES:
        assert label in prompt, f"{label!r} is not a label the model is asked for"


def test_supported_material_yields_more_than_two_skill_rows():
    """The end of the bug: enough real, varied material must produce more than the two
    rows the classifier used to allow."""
    from tailoring.keywords import classify_skill
    material = ["Python", "SQL", "AWS", "Airflow", "Machine Learning",
                "Forecasting", "Credit Risk", "Derivatives"]
    rows = {}
    for t in material:
        rows.setdefault(classify_skill(t), []).append(t)
    assert len(rows) >= 4, f"only {len(rows)} rows: {list(rows)}"


def test_shrink_keeps_at_least_four_skill_categories():
    """Skills shrink from 6 down to the 4-category floor, never below it."""
    profile = {"skills": {f"C{i}": "term one, term two" for i in range(6)},
               "experience": [{"org": "X", "roles": [{"title": "T", "dates": "2020", "bullets": []}]}]}
    for _ in range(40):
        if not _shrink_once(profile):
            break
    assert len(profile["skills"]) == 4, f"skills floor breached: {list(profile['skills'])}"
