"""The strict staircase, one fact per bullet, and Interests on every template (Kofi, 2026-11-10).

His D365 Software Developer build came out 7/5/2/2: the most recent role overloaded with thin
splits that restate each other, Viva tied with CSIR although Viva's own material is one rich
bullet that splits honestly in three, and no Interests although he had answered the hobbies
question. The rules:

* The bullets step down strictly by recency, built from the BOTTOM UP: the oldest role keeps at
  least 2, and each more recent role reaches one more than the role below it before any role
  above it grows. A lower role climbs by splitting its own long bullets.
* A bullet the fill adds must carry a fact (a tool, a number, an outcome) no other bullet of the
  role says, in the words of the person's material, not words added to reach a length.
* Interests the person gave print on every template, as "Additional Information".
"""

from __future__ import annotations

import copy

from conftest import requires_latex
from intake.memory import ConversationMemory
from llm.base import FakeLLM
from tailoring import assembler as A
from tailoring.assembler import (_split_ok, assemble_cv, normalize_profile, ranked_roles,
                                 render_cv, extract_preamble, role_notes, written_problems)
from tailoring.filler import new_bullet_problems
from ui.records import CVRecords
from ui.session import WebIntake


def _counts(profile: dict) -> list[int]:
    return [len(r.get("bullets") or []) for _rank, _e, r in
            sorted(ranked_roles(profile), key=lambda x: x[0])]


# --------------------------------------------------------------------------- the staircase
RICH = ("Analyzed SQL and Excel operational data for a high-volume delivery platform, tracking 250 "
        "drivers and 5,000+ daily orders, and turned the findings into capacity and service-level "
        "decisions for the operations team.")
RICH_PARTS = [
    "Analyzed SQL and Excel operational data for a high-volume delivery platform's operations team.",
    "Tracked 250 drivers and 5,000+ daily orders on the delivery platform in SQL and Excel.",
    "Turned the operational findings into capacity and service-level decisions for the operations team.",
]


def _ladder_profile() -> dict:
    return normalize_profile({
        "identity": {"name": "Ama Mensah", "email": "ama@example.com", "phone": "(312) 555-0100",
                     "address": "Chicago, IL"},
        "education": [{"school": "University of Illinois Chicago", "location": "Chicago, IL",
                       "degree": "Bachelor of Science in Statistics", "date": "May 2015"}],
        "skills": {"Data": "SQL, Python, pandas, Excel", "Visualization": "Tableau, Power BI",
                   "Methods": "A/B testing, forecasting, data cleaning"},
        "experience": [
            {"org": "Lakeshore Logistics", "location": "Chicago, IL", "roles": [{
                "title": "Senior Data Analyst", "dates": "March 2023 - Present", "bullets": [
                    "Built weekly SQL and Tableau dashboards that track on-time delivery for 14 regional depots.",
                    "Cleaned shipment data in Python and pandas, cutting report preparation from two days to four hours.",
                    "Automated the depot scorecard refresh with a scheduled Python job before the morning stand-up.",
                    "Documented the delivery metrics so new analysts could rebuild each dashboard without help.",
                    "Moved the monthly carrier report from Excel to SQL views, so finance could refresh it in minutes.",
                    "Joined route planners each week to explain late-delivery trends and agree on depots to review."]}]},
            {"org": "Northside Health", "location": "Chicago, IL", "roles": [{
                "title": "Reporting Analyst", "dates": "June 2019 - February 2023", "bullets": [
                    "Produced monthly Excel and SQL reports on clinic wait times for the operations director.",
                    "Ran an A/B test of appointment reminder texts that lowered no-shows by 9 percent.",
                    "Built a wait-time dashboard in Tableau that clinic managers checked at the start of every shift.",
                    "Cleaned three years of appointment records in SQL for the yearly staffing review."]}]},
            {"org": "Viva Delivery", "location": "Accra, GH", "roles": [{
                "title": "Operations Analyst", "dates": "Sept 2016 - May 2019", "bullets": [RICH]}]},
            {"org": "Campus Library", "location": "Chicago, IL", "roles": [{
                "title": "Student Assistant", "dates": "September 2013 - May 2015", "bullets": [
                    "Answered research questions and kept the circulation database up to date.",
                    "Trained new student staff on the checkout system."]}]},
        ]})


class _Splits(FakeLLM):
    """Splits the one rich bullet in three, honestly, and writes nothing."""

    def __init__(self):
        super().__init__()
        self.calls: list = []

    def split_bullet(self, role_title, text, parts, jd_text="", tense="past", context=""):
        self.calls.append((text[:30], parts))
        return list(RICH_PARTS) if (text == RICH and parts == 3) else [text]

    def write_role_bullet(self, *a, **k):
        return ""


@requires_latex
def test_a_lower_role_with_one_long_bullet_climbs_its_step_before_the_top_grows(
        template_source, workdir):
    llm = _Splits()
    res = assemble_cv(template_source, _ladder_profile(), "Data Analyst. SQL, Excel, Tableau.", llm,
                      workdir, tailor=False, fill_writes=True)
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0
    c = _counts(res.profile_used)
    assert all(b < a for a, b in zip(c, c[1:])), c            # strictly fewer each step down
    assert c[-1] >= 2 and c[2] >= 3 and c[0] <= A.STAIR_SOFT_TOP, c
    viva = next(r for _k, e, r in ranked_roles(res.profile_used) if e["org"] == "Viva Delivery")
    assert viva["bullets"] == RICH_PARTS                        # the rich bullet, in its 3 parts
    assert (RICH[:30], 3) in llm.calls
    # Bottom up: Viva's step was made before the most recent role took any more bullets.
    kinds = [(a["label"].split(",")[0], a["kind"]) for a in res.added]
    first_viva = kinds.index(("Viva Delivery", "split"))
    top = [i for i, k in enumerate(kinds) if k[0] == "Lakeshore Logistics"]
    assert top and first_viva < min(top), kinds
    assert A.staircase_ok(res.profile_used)


@requires_latex
def test_the_oldest_role_keeps_two_and_a_dry_middle_role_is_named(template_source, workdir):
    """Viva has no more material (no split offered): CSIR keeps its 2, nothing is cut to fake a
    step, and the note asks for more about the role that stands too low."""
    prof = _ladder_profile()
    res = assemble_cv(template_source, prof, "analyst", FakeLLM(), workdir, tailor=False,
                      fill_writes=False)
    c = _counts(res.profile_used)
    assert c[-1] == 2, c
    assert not any(t.get("staircase") and "Campus Library" in t.get("label", "") for t in res.trims)


def test_make_room_never_takes_the_oldest_role_below_two():
    from tailoring.assembler import _Cuts, _make_room
    page = normalize_profile({"experience": [
        {"org": "A", "roles": [{"title": "X", "dates": "2020 - Present", "bullets": ["a1", "a2", "a3"]}]},
        {"org": "B", "roles": [{"title": "X", "dates": "2018 - 2020", "bullets": ["b1", "b2"]}]},
        {"org": "C", "roles": [{"title": "X", "dates": "2016 - 2018", "bullets": ["c1", "c2", "c3"]}]}]})
    cuts = _Cuts([], (), {})
    while _make_room(page, cuts, sorted(ranked_roles(page), key=lambda x: x[0])):
        pass
    assert _counts(page)[-1] == 2


# --------------------------------------------------------------------------- one fact per bullet
BO = [
    "Led business intelligence across the company's products, building KPI dashboards that track "
    "growth, usage, and unit costs for stakeholders across the business.",
    "Built automated Python and SQL reporting and turned the results into product roadmap and "
    "spend decisions for stakeholders.",
    "Built an AI-assisted pipeline that turns 280,000+ daily job postings into 145,000+ "
    "sponsorship-matched roles.",
]
BO_MATERIAL = "BlackOrigin Inc.\nAI Senior Business Intelligence Manager\n" + "\n".join(BO)


def test_the_real_builds_restating_bullets_are_refused():
    """Word for word from the 7/5/2/2 page (2026-11-10)."""
    padded = {
        "Led business intelligence across the company's products, owning the function that covers "
        "every product line and the data the company uses to track them.": 0,
        "Built an AI-assisted pipeline that processes 280,000+ daily job postings, turning a very "
        "large raw feed into a structured set of listings.": 2,
        "Delivered 145,000+ sponsorship-matched roles from that feed, giving job seekers a filtered "
        "set of openings to apply to.": 2,
    }
    for text, src in padded.items():
        sibs = [b for i, b in enumerate(BO) if i != src]
        assert "padded with words the material does not say" in new_bullet_problems(
            text, sibs, BO_MATERIAL), text
    # The same split, made of the bullet's own words, goes through.
    good = ["Built an AI-assisted pipeline that processes 280,000+ daily job postings for the company.",
            "Matched 145,000+ sponsorship roles from the daily job postings the pipeline turns out."]
    assert _split_ok(good, BO[2], BO_MATERIAL, BO[:2], "")
    bad = list(padded)[1:]
    assert not _split_ok(bad, BO[2], BO_MATERIAL, BO[:2], "")


def test_a_written_bullet_that_restates_or_pads_is_refused():
    sibs = list(BO)
    # an invented direction-setting clause the material never states
    assert written_problems("Set the reporting direction for the product, growth, and finance "
                            "teams that depend on product data to plan their work.",
                            BO_MATERIAL, sibs)
    # nothing the other bullets do not already say
    assert "adds no fact of its own" in new_bullet_problems(
        "Built KPI dashboards and automated reporting for stakeholders across the business.",
        sibs, BO_MATERIAL)
    # a detail the uploaded resume adds is a fact of its own
    viva = ["Analyzed SQL and Excel operational data for a digital fulfillment platform, tracking "
            "250 drivers and 5,000+ daily orders."]
    upload = ("Viva Technologies Ghana\nProduct and Operations Analyst\n" + viva[0] + "\nAnalyzed "
              "operational data in SQL and Excel for a high-velocity digital fulfillment platform, "
              "and turned the findings into product and operations decisions on capacity and "
              "service levels.")
    assert new_bullet_problems("Turned the operational findings into product and operations "
                               "decisions on capacity and service levels.", viva, upload) == []


def test_role_notes_take_each_employer_s_lines_from_an_uploaded_resume():
    text = """Experience
BlackOrigin Inc.
Chicago, IL
AI Senior Business Intelligence Manager
March 2026 - Present
• Built an AI-assisted data pipeline that processes 280,000+ job postings a day into 145,000+
roles matched to government sponsorship records.
Viva Technologies Ghana
Accra, GH
Product and Operations Analyst
• Analyzed operational data in SQL and Excel for a high-velocity digital fulfillment platform,
tracking 250 drivers and 5,000+ daily orders.
Interests
Football, travel
"""
    notes = role_notes(text, ["BlackOrigin Inc.", "Viva Technologies Ghana"])
    assert "government sponsorship records" in notes["blackorigin inc."]
    assert "250 drivers" not in notes["blackorigin inc."]
    assert "high-velocity" in notes["viva technologies ghana"]
    assert "Football" not in notes["viva technologies ghana"]
    # the notes are this role's material, for a split or a written bullet
    entry = {"org": "Viva Technologies Ghana", "roles": [{"title": "Product and Operations Analyst"}]}
    got = A._role_material(entry, entry["roles"][0], None, {}, [{"role_notes": notes}])
    assert "high-velocity" in got and "government" not in got


# --------------------------------------------------------------------------- interests
def test_interests_print_on_a_template_without_an_interests_section(template_source, workdir):
    from ui.app import _load_template
    mem = ConversationMemory(":memory:")
    prof = _ladder_profile()
    prof["interests"] = "Playing football on weekends, travelling, and the DOAC podcast."
    mem.save("default", copy.deepcopy(prof), {}, [])
    tex, name = _load_template("software")
    s = WebIntake("Software Developer, D365.", tex, FakeLLM(), mem, CVRecords(":memory:"), workdir,
                  jobname="cv", palace_dir=workdir / "p", template_name=name)
    assert "interests" not in s._template_sections            # the template has no place for them
    assert s.sections[-1] == "interests"                       # ...so they go last
    src = render_cv(extract_preamble(tex), s.profile, s.sections, headings=s.headings)
    assert "Interests:" in src and "DOAC podcast" in src
    # a template with its own Interests section keeps it once, where it puts it
    tex2, name2 = _load_template("shetty")
    s.set_template(name2, tex2)
    assert s.sections.count("interests") == 1
    # and nobody without interests gets an empty heading
    s.profile["interests"] = ""
    s.saved_profile["interests"] = ""
    s.essentials["interests"] = ""
    s.set_template(name, tex)
    assert "interests" not in s.sections
