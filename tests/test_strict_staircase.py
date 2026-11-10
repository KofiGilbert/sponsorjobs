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
RICH = ("Analyzed SQL and Excel operational data for a high-volume delivery platform serving the "
        "Accra metro area, tracking 250 drivers and 5,000+ daily orders through shift reports, and "
        "turned the findings into capacity and service-level decisions for the operations team and "
        "the product group.")
# Each part a full line of its own (a split is taken only when its parts stand as full lines).
RICH_PARTS = [
    "Analyzed SQL and Excel operational data for a high-volume delivery platform serving the Accra metro area.",
    "Tracked 250 drivers and 5,000+ daily orders through shift reports on the delivery platform in SQL and Excel.",
    "Turned the operational findings into capacity and service-level decisions for the operations team and the product group.",
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


def test_only_empty_filler_is_refused_never_a_professionally_written_bullet():
    """Kofi, 2026-11-10: a bullet written the way a professional would (its words need not be
    the person's own) goes through; a line with no concrete action or object does not."""
    from tailoring.filler import hollow
    written = ("Led business intelligence across the company's products, owning the function "
               "that covers every product line and the data the company uses to track them.")
    assert new_bullet_problems(written, BO[1:], BO_MATERIAL) == []
    assert hollow("Drove strategic alignment and operational excellence across teams.")
    assert "empty filler" in new_bullet_problems(
        "Delivered key results and business value for stakeholders.", BO, BO_MATERIAL)
    # each part of a split still says something the role's other bullets do not
    good = ["Built an AI-assisted pipeline that processes 280,000+ daily job postings for the company.",
            "Matched 145,000+ sponsorship roles from the daily job postings the pipeline turns out."]
    assert _split_ok(good, BO[2], BO_MATERIAL, BO[:2], "")
    assert not _split_ok([BO[0], good[1]], BO[2], BO_MATERIAL, BO[:2], "")


def test_a_written_bullet_that_restates_or_invents_a_figure_is_refused():
    sibs = list(BO)
    # a typical duty of the title is fine now (Kofi, 2026-11-10); an invented figure is not
    assert written_problems("Set the reporting direction for the product, growth, and finance "
                            "teams that depend on product data to plan their work.",
                            BO_MATERIAL, sibs) == []
    assert written_problems("Cut reporting time by 35 percent for the product and finance teams.",
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


def test_a_split_part_that_takes_a_sibling_s_point_is_refused():
    """Word for word from the first real build after the staircase change (2026-11-10)."""
    from tailoring.assembler import _split_problem
    v0 = ("Analyzed SQL and Excel operational data for a digital fulfillment platform, tracking 250 "
          "drivers and 5,000+ daily orders.")
    v1 = ("Turned operational findings into product and operations decisions on capacity and service "
          "levels for the fulfillment platform.")
    borrowed = ["Analyzed SQL and Excel operational data for a high-velocity digital fulfillment platform.",
                "Tracked 250 drivers and more than 5,000 daily orders on the digital fulfillment platform, "
                "turning findings into decisions on capacity and service levels."]
    assert "point" in _split_problem(borrowed, v0, v0 + "\nhigh-velocity\n" + v1, [v1], "", "past")
    honest = ["Analyzed SQL and Excel operational data for a high-velocity digital fulfillment platform "
              "and its daily operations.",
              "Tracked 250 drivers and 5,000+ daily orders through shift reports, measuring the workload "
              "behind each delivery."]
    notes = v0 + "\nhigh-velocity\n" + v1 + "\nshift reports workload measuring delivery daily operations"
    assert _split_problem(honest, v0, notes, [v1], "", "past") == ""
    # a part too short to stand as a full line keeps the bullet whole (the role gets a written one)
    assert "too thin" in _split_problem(
        ["Analyzed SQL and Excel operational data for a digital fulfillment platform.", honest[1]],
        v0, notes, [v1], "", "past")
    # two parts that open the same way read as one fact twice
    same = ["Turned operational findings into product decisions on what the fulfillment platform built next.",
            "Turned operational findings into operations decisions on driver capacity and service levels."]
    assert "opens like" in _split_problem(same, v1, v1, [v0], "", "past")


# --------------------------------------------------------------------------- refused splits
def test_a_padded_tail_clause_comes_off_a_split_part():
    from tailoring.assembler import _without_padded_tail
    src = "Built an AI-assisted pipeline that turns 280,000+ daily job postings into 145,000+ sponsorship-matched roles."
    assert _without_padded_tail(
        "Built an AI-assisted pipeline that processes 280,000+ daily job postings, giving the "
        "company's job seekers a structured daily feed to work from.", src) == \
        "Built an AI-assisted pipeline that processes 280,000+ daily job postings."
    # a clause with a figure, or of the bullet's own words, stays
    keep = "Matched postings to sponsorship records, producing 145,000+ sponsorship-matched roles."
    assert _without_padded_tail(keep, src) == keep
    own = "Led business intelligence across the company's products, building KPI dashboards."
    assert _without_padded_tail(own, own) == own


class _SecondTry(FakeLLM):
    """Pads its first split; told why, splits cleanly."""

    def __init__(self):
        super().__init__()
        self.feedback: list = []

    def split_bullet(self, role_title, text, parts, jd_text="", tense="past", context="", feedback=""):
        self.feedback.append(feedback)
        if text != RICH or parts != 3:
            return [text]
        if not feedback:
            return [p.rstrip(".") + " to support long-term strategic planning cycles."
                    for p in RICH_PARTS]
        return list(RICH_PARTS)

    def write_role_bullet(self, *a, **k):
        return ""


@requires_latex
def test_a_refused_split_is_asked_for_once_more_with_the_reason(template_source, workdir):
    llm = _SecondTry()
    res = assemble_cv(template_source, _ladder_profile(), "Data Analyst. SQL, Excel, Tableau.", llm,
                      workdir, tailor=False, fill_writes=True)
    viva = next(r for _k, e, r in ranked_roles(res.profile_used) if e["org"] == "Viva Delivery")
    assert viva["bullets"] == RICH_PARTS
    told = [f for f in llm.feedback if f]
    assert told and "was refused (part" in told[0]          # the reason goes with the second ask


def test_a_possessive_names_the_same_employer():
    from tailoring.fact_gate import known_material, new_claims
    src = "BlackOrigin Inc.\nBuilt automated Python and SQL reporting for stakeholders."
    assert new_claims("Built automated Python and SQL reporting for BlackOrigin's products.",
                      known=known_material(src)) == []
    assert new_claims("Built reporting for Acme's products.", known=known_material(src))


# --------------------------------------------------------------------------- the saved profile
def test_a_page_that_leaves_out_a_middle_bullet_never_loses_it_from_the_profile(workdir):
    """Stanbic's page showed bullets 1, 2 and 4 of 4; the saved profile came back with bullet 4
    twice and bullet 3 gone (the merge went by position)."""
    from types import SimpleNamespace
    mem = ConversationMemory(":memory:")
    own = ["Led business analysis across retail and digital banking.",
           "Turned complex data into recommendations for senior leadership.",
           "Earned promotion to Senior Business Analyst within 6.5 years.",
           "Led enterprise CRM requirements and process improvement."]
    prof = {"identity": {"name": "K G"}, "experience": [
        {"org": "Stanbic Bank Ghana", "roles": [{"title": "Senior Business Analyst",
                                                 "dates": "2018 - 2024", "bullets": list(own)}]}]}
    mem.save("default", copy.deepcopy(prof), {}, [])
    s = WebIntake("Analyst.", "", FakeLLM(), mem, CVRecords(":memory:"), workdir, jobname="cv",
                  palace_dir=workdir / "p")
    s.profile = normalize_profile(copy.deepcopy(prof))
    page = copy.deepcopy(s.profile)
    tailored = ["Led business analysis and reporting across retail and digital banking.",
                "Translated complex data into recommendations for senior leadership.",
                own[3]]
    page["experience"][0]["roles"][0]["bullets"] = list(tailored)
    s.assembled = SimpleNamespace(origins={tailored[0]: own[0], tailored[1]: own[1]})
    whole = s._whole_profile_after_render(page)
    got = whole["experience"][0]["roles"][0]["bullets"]
    assert got == tailored + [own[2]]


# --------------------------------------------------------------------------- the four page rules
@requires_latex
def test_the_page_rule_report_measures_the_compiled_pdf(template_source, workdir):
    """Edges, one-line skills, fill and the strict staircase, read off the real PDF."""
    res = assemble_cv(template_source, _ladder_profile(), "Data Analyst. SQL, Excel, Tableau.",
                      _Splits(), workdir, tailor=False, fill_writes=True)
    rep = A.page_rule_report(res)
    assert set(rep) == {"edges", "skills_one_line", "fill", "staircase", "details"}
    assert rep["staircase"] == A.staircase_ok(res.profile_used)
    assert rep["fill"] == (res.fill_ratio >= A.FILL_GOAL)
    assert rep["edges"] == (not rep["details"]["short_bullets"])
    assert rep["details"]["counts"] == _counts(res.profile_used)
    # a tie at the bottom is not a staircase
    tied = copy.deepcopy(res)
    roles = [r for _k, _e, r in sorted(ranked_roles(tied.profile_used), key=lambda x: x[0])]
    roles[-2]["bullets"] = roles[-2]["bullets"][:len(roles[-1]["bullets"])]
    assert not A.page_rule_report(tied)["staircase"]


# --------------------------------------------------------------------------- padding with no fact
def test_padding_that_adds_no_fact_is_refused():
    """Word for word from the confirming real run (2026-11-10)."""
    from tailoring.filler import role_padding
    bo = "BlackOrigin Inc."
    sibs = ["Led business intelligence across the company's products for stakeholders across the business."]
    assert any(x.startswith("repeats a phrase of another bullet") for x in role_padding(
        "Built KPI dashboards that track growth, usage, and unit costs for stakeholders across the business.",
        sibs, bo))
    assert any(x.startswith("stock tail") for x in role_padding(
        "Built automated Python and SQL reporting for stakeholders across the business.", [], bo))
    assert "names its own employer" in role_padding(
        "Built automated Python and SQL reporting for BlackOrigin Inc.'s products.", [], bo)
    csir = "Council for Scientific and Industrial Research (CSIR)"
    assert "names its own employer" in role_padding(
        "Analyzed research datasets at the Council for Scientific and Industrial Research (CSIR).", [], csir)
    assert "names its own employer" in role_padding("Built SQL pipelines for CSIR research.", [], csir)
    # the written bullet the run got right passes
    assert role_padding("Built SQL reports and dashboards that gave operations staff a daily view of "
                        "order volumes, driver workload and service levels on the fulfillment platform.",
                        ["Analyzed SQL and Excel operational data for a digital fulfillment platform."],
                        "Viva Technologies Ghana") == []
    # what the person's own bullet already says is theirs
    own = "Led enterprise CRM requirements for stakeholders across the business."
    assert role_padding(own, [], bo, source=own) == []


class _Polisher(FakeLLM):
    """Polishes every bullet by naming the employer: each rewrite must be refused."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def polish_role(self, role_label, bullets, budgets, context="", jd_text="", only=None):
        self.calls += 1
        org = role_label.split(", ")[0]
        return [f"{b.rstrip('.')} at {org}, working across every team and function there." for b in bullets]

    def split_bullet(self, *a, **k):
        return [a[1]]

    def write_role_bullet(self, *a, **k):
        return ""


@requires_latex
def test_the_edge_polish_runs_per_role_and_refuses_padding(template_source, workdir):
    llm = _Polisher()
    prof = _ladder_profile()
    res = assemble_cv(template_source, prof, "Data Analyst. SQL, Excel, Tableau.", llm, workdir,
                      tailor=False, fill_writes=True, fit_lines=True, fit_rounds=0)
    assert res.ok and res.compile.pages == 1
    assert llm.calls >= 1
    for _k, e, r in ranked_roles(res.profile_used):
        for b in r["bullets"]:
            assert e["org"] not in b, b


def test_a_split_never_carries_its_source_s_phrase_into_two_bullets():
    """Run 6 (2026-11-10): "for stakeholders across the business" on bullets 1 and 2 of one role,
    both taken from the person's own bullet."""
    from tailoring.assembler import _split_problem
    src = ("Led business intelligence across the company's products, building KPI dashboards that "
           "track growth, usage, and unit costs for stakeholders across the business.")
    parts = ["Led business intelligence across the company's products for stakeholders across the "
             "business, owning the reporting side.",
             "Built KPI dashboards in Power BI that track growth, usage, and unit costs for stakeholders "
             "across the business."]
    assert _split_problem(parts, src, src, [], "", "past", "BlackOrigin Inc.")


# --------------------------------------------------------------------------- plan, then write
_WORDS = ("ledger invoice freight pallet roster audit queue ticket vendor budget forecast inventory "
          "shipment schedule warehouse route carrier depot clinic patient record survey sample "
          "batch query report review checklist template workflow handbook training session cohort "
          "pipeline extract schema table metric threshold alert variance trend sprint backlog "
          "release staging archive catalog index tracker calendar register summary briefing memo "
          "dataset column filter rollup chart panel notebook script macro import export upload "
          "invoice2 margin quote tender contract renewal intake triage escalation handoff").split()


class _PlanWriter(FakeLLM):
    """Writes each planned bullet to its window from words no other bullet uses."""

    def __init__(self):
        super().__init__()
        self.calls = 0
        self.n = 0

    def _sentence(self, lo, hi):
        verbs = ["Reviewed", "Prepared", "Organized", "Checked", "Updated", "Mapped", "Sorted"]
        out = verbs[self.n % len(verbs)]
        while len(out) < (lo + hi) // 2 - 12:
            out += " " + _WORDS[self.n % len(_WORDS)] + ("s" if self.n // len(_WORDS) % 2 else "")
            self.n += 1
        return out + " each week."

    def write_experience(self, roles, jd_text="", current=None, only=None, feedback=None):
        self.calls += 1
        res = []
        for i, ro in enumerate(roles):
            row = []
            for k, (lo, hi, _n) in enumerate(ro["windows"]):
                if only is not None and k not in (only.get(i) or []) and current and k < len(current[i]):
                    row.append(current[i][k])
                else:
                    row.append(self._sentence(lo, hi))
            res.append(row)
        return res


def test_the_plan_is_a_strict_staircase_that_adds_up_to_the_lines():
    from tailoring.plan_fill import plan_counts, plan_lines, windows
    c = plan_counts(4, 20)
    assert c == [5, 4, 3, 2]
    t = plan_lines(c, 20)
    assert sum(map(sum, t)) == 20 and t[0] == [2, 2, 2, 2, 2] and t[-1] == [1, 1]
    assert plan_counts(4, 27)[0] == 6                         # the lines need more bullets
    w = windows([[1, 2]], 100.0)[0]
    assert w[0] == (93, 100, 1) and w[1] == (193, 200, 2)


def test_an_invented_outcome_claim_is_refused():
    from tailoring.plan_fill import invented_claims
    assert invented_claims("Processed every one of the 280,000+ daily postings, with none skipped "
                           "or lost.", "Built a pipeline for 280,000+ daily postings.")
    assert invented_claims("Cut errors to zero errors in reporting.", "") 
    assert not invented_claims("Built SQL reports on order volumes for daily reviews.", "")


@requires_latex
def test_plan_then_write_fills_the_experience_section_in_one_call(template_source, workdir):
    llm = _PlanWriter()
    res = assemble_cv(template_source, _ladder_profile(), "Data Analyst. SQL, Excel, Tableau.", llm,
                      workdir, tailor=False, fit_lines=True, fit_rounds=0)
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0
    c = _counts(res.profile_used)
    assert all(b < a for a, b in zip(c, c[1:])) and c[-1] >= 2, c
    assert 1 <= llm.calls <= 3
    assert any(a["kind"] == "written" for a in res.added)
