"""Bullets by recency, at the template's own size (Kofi, 2026-11-06).

A short page keeps the template's own type size, margins and spacing: the layout ladder that
enlarged it (12pt, one-inch margins, stretch glue between sections) is gone, so a short page
ends earlier. What fills it is bullets by recency: the most recent role shows 3 to 5, the second
3 to 4, older roles 2 to 3, filled most recent first and re-measured after every bullet. An
overflow cuts the oldest roles' bullets first. A bullet WRITTEN from a role's own material must
pass the fact gate scoped to that role (no new figure, tool, name or claim, no repeat of a
sibling) and is listed for the person to check (CLAUDE.md sections 8 and 9).
"""

from __future__ import annotations

import copy
import inspect

from intake.memory import ConversationMemory
from llm.base import FakeLLM
from tailoring import assembler as A
from tailoring.assembler import (MAX_FILL, _drop_trailing_bullet, assemble_cv, bullet_budget,
                                 normalize_profile, ranked_roles, recency_ranks, render_cv,
                                 select_roles_for_jd, written_problems)
from tailoring.changes import points, what_changed
from ui.records import CVRecords
from ui.session import WebIntake
from conftest import requires_latex

JD = ("Data Analyst. SQL, Python, Tableau, Excel, dashboards, reporting, data cleaning, "
      "A/B testing.")

# 4 roles, 2 bullets each, no projects: the thin shape the recency fill exists for.
THIN = {
    "identity": {"name": "Ama Mensah", "address": "Chicago, IL", "phone": "(312) 555-0100",
                 "email": "ama@example.com"},
    "education": [{"school": "University of Illinois Chicago", "location": "Chicago, IL",
                   "degree": "Bachelor of Science in Statistics", "date": "May 2018"}],
    "skills": {"Data": "SQL, Python, pandas, Excel", "Visualization": "Tableau, Power BI",
               "Methods": "A/B testing, forecasting, data cleaning", "Tools": "Git, Jira, Snowflake",
               "Communication": "Stakeholder reporting, documentation"},
    "experience": [
        {"org": "Lakeshore Logistics", "location": "Chicago, IL", "roles": [{
            "title": "Data Analyst", "dates": "March 2023 - Present", "bullets": [
                "Built weekly SQL and Tableau dashboards that track on-time delivery for 14 regional depots.",
                "Cleaned shipment data in Python and pandas, cutting report preparation from two days to four hours."]}]},
        {"org": "Northside Health", "location": "Chicago, IL", "roles": [{
            "title": "Reporting Analyst", "dates": "June 2021 - February 2023", "bullets": [
                "Produced monthly Excel and SQL reports on clinic wait times for the operations director.",
                "Ran an A/B test of appointment reminder texts that lowered no-shows by 9 percent."]}]},
        {"org": "Brightway Retail", "location": "Evanston, IL", "roles": [{
            "title": "Operations Associate", "dates": "July 2019 - May 2021", "bullets": [
                "Tracked store inventory in Excel and flagged stock gaps to the regional manager each week.",
                "Forecast holiday staffing from two years of sales data for three stores."]}]},
        {"org": "Campus Library", "location": "Chicago, IL", "roles": [{
            "title": "Student Assistant", "dates": "September 2016 - May 2018", "bullets": [
                "Answered research questions and kept the circulation database up to date.",
                "Trained new student staff on the checkout system."]}]},
    ],
    "extracurricular": [{"title": "Volunteer Tutor, Chicago Public Library", "date": "2019 - Present",
                         "bullets": ["Tutor adult learners in spreadsheet basics every other Saturday."]}],
    "interests": "Running, chess, cooking",
}

_LAYOUT_MARKS = ("\\newgeometry", "\\fontsize", "\\flushbottom", "plus 1fill", "\\setlist")


def _body(tex: str) -> str:
    return tex.split("\\begin{document}", 1)[1]


def _counts(profile: dict) -> list[int]:
    return [len(r.get("bullets") or []) for _rank, _e, r in ranked_roles(profile)]


def _role(org, dates, n, words="Delivered"):
    return {"org": org, "location": "Chicago, IL", "roles": [{"title": "Analyst", "dates": dates,
            "bullets": [f"{words} {org} result number {i} for the team." for i in range(n)]}]}


# --------------------------------------------------------------------------- no layout ladder
def test_the_layout_ladder_and_stretch_glue_are_gone():
    assert not hasattr(A, "LAYOUT_STEPS") and not hasattr(A, "_STRETCH")
    assert "layout" not in inspect.signature(render_cv).parameters
    tex = render_cv("\\documentclass{article}\\begin{document}", normalize_profile(THIN))
    assert not any(k in _body(tex) for k in _LAYOUT_MARKS)


@requires_latex
def test_no_render_carries_a_layout_step_or_stretch_glue(template_source, workdir):
    from ui.app import _placeholder_profile
    for name, prof, llm, tailor in (("doe", _placeholder_profile(), None, False),
                                    ("thin", THIN, FakeLLM(), True)):
        res = assemble_cv(template_source, prof, JD, llm, workdir, jobname=name, tailor=tailor,
                          fit_lines=True)
        assert res.ok, res.summary()
        assert not any(k in _body(res.tex_source) for k in _LAYOUT_MARKS), name
        assert not hasattr(res, "layout")


# --------------------------------------------------------------------------- recency
def test_recency_ranks_present_first_then_by_end_date_and_undated_keep_their_place():
    roles = [({"dates": "2014 - 2016"}, {}), ({"dates": "Jan 20XX - Dec 20XX"}, {}),
             ({"dates": "June 2021 - Present"}, {}), ({"dates": "March 2017 - May 2021"}, {})]
    assert recency_ranks(roles) == [2, 3, 0, 1]
    assert [bullet_budget(r) for r in range(4)] == [(3, 5), (3, 4), (2, 3), (2, 3)]
    assert bullet_budget(3, lead=True) == (3, 5)


def test_selection_starts_each_role_at_its_fewest_and_keeps_the_rest_waiting():
    p = normalize_profile({"experience": [_role("Acme", "2021 - Present", 6), _role("Beta", "2019 - 2021", 6),
                                          _role("Cobalt", "2017 - 2019", 6), _role("Delta", "2015 - 2017", 6)]})
    out = select_roles_for_jd(p, "analyst")
    assert _counts(out) == [3, 3, 2, 2]
    reserves = [len(r.get("_reserve") or []) for _rank, _e, r in ranked_roles(out)]
    assert reserves == [3, 3, 4, 4]
    # the person's material itself is untouched
    assert _counts(p) == [6, 6, 6, 6]


# --------------------------------------------------------------------------- overflow
def test_overflow_cuts_the_oldest_roles_first_and_protects_the_two_most_recent():
    page = {"projects": [], "experience": [_role("Acme", "2021 - Present", 5), _role("Beta", "2019 - 2021", 4),
                                           _role("Cobalt", "2017 - 2019", 3), _role("Delta", "2015 - 2017", 3)]}
    seen = []
    while _drop_trailing_bullet(page):
        seen.append(_counts(page))
    # extras above the fewest go first, oldest role first, the most recent role last
    assert seen[:4] == [[5, 4, 3, 2], [5, 4, 2, 2], [5, 3, 2, 2], [4, 3, 2, 2]]
    assert seen[4] == [3, 3, 2, 2]
    # then older roles give up bullets before either recent role goes below three
    assert seen[5:7] == [[3, 3, 2, 1], [3, 3, 1, 1]]
    for c in seen:
        if c[0] < 3 or c[1] < 3:
            assert all(x <= 2 for x in c[2:]), c


# --------------------------------------------------------------------------- the gate
def test_a_written_bullet_with_an_invented_number_or_tool_is_rejected():
    role_text = ("Lakeshore Logistics\nData Analyst\nBuilt weekly SQL and Tableau dashboards that "
                 "track on-time delivery for 14 regional depots.\nAt Lakeshore Logistics I also "
                 "trained the new analysts to maintain the SQL dashboards.")
    listed = ["SQL", "Tableau", "Kubernetes", "Power BI"]
    sib = ["Built weekly SQL and Tableau dashboards that track on-time delivery for 14 regional depots."]
    ok = "Trained the new analysts to maintain and extend the SQL dashboards."
    assert written_problems(ok, role_text, sib, JD, listed) == []
    # says nothing the sibling does not (a real build wrote one like this, 2026-11-06)
    assert "restates the role's other bullets" in written_problems(
        "Shared the weekly SQL delivery dashboards with each regional depot.", role_text, sib, JD, listed)
    assert "40" in " ".join(written_problems("Cut delivery delays by 40 percent with SQL dashboards.",
                                             role_text, sib, JD, listed))
    assert written_problems("Moved the depot dashboards onto Kubernetes for the team.",
                            role_text, sib, JD, listed)                      # tool from another part
    assert written_problems("Built dashboards in Power BI for the depots.", role_text, sib, JD, listed)
    assert written_problems("Partnered with Amazon on depot reporting.", role_text, sib, JD, listed)
    assert written_problems(sib[0], role_text, sib, JD, listed)            # repeats a sibling
    assert written_problems("Leveraging SQL, ensuring robust delivery tracking for depots.",
                            role_text, sib, JD, listed)                      # filler


class _Inventive(FakeLLM):
    """Writes bullets that state things the role's material does not."""

    def write_role_bullet(self, role_label, material, existing, jd_terms, max_chars, jd_text=""):
        return f"Grew revenue by 40 percent using Kubernetes at {role_label.split(', ')[0]}."


class _Splitter(FakeLLM):
    """Splits a bullet at its semicolon (FakeLLM.split_bullet) and writes nothing."""

    def write_role_bullet(self, *a, **k):
        return ""


def test_a_split_that_repeats_a_phrase_across_parts_is_refused():
    from tailoring.assembler import _split_ok
    src = _SRC
    good = [_PART1, _PART2]
    echo = ["Built KPI dashboards that track growth, usage, and unit costs.",
            "Automated Python and SQL reporting that tracks growth, usage, and unit costs."]
    assert _split_ok(good, src, src, [], JD)
    assert not _split_ok(echo, src, src, [], JD)
    assert not _split_ok(["Led BI for 40 products.", "Built KPI dashboards."], src, src, [], JD)


@requires_latex
def test_the_fill_never_publishes_an_invented_bullet(template_source, workdir):
    res = assemble_cv(template_source, THIN, JD, _Inventive(), workdir, tailor=True)
    assert res.ok and res.compile.pages == 1
    assert res.written == []
    assert "Kubernetes" not in res.tex_source and "40 percent" not in res.tex_source


@requires_latex
def test_a_merged_bullet_is_split_into_its_own_accomplishments(template_source, workdir):
    prof = copy.deepcopy(THIN)
    prof["experience"][0]["roles"][0]["bullets"] = [
        "Built weekly SQL and Tableau dashboards that track on-time delivery for 14 regional depots; "
        "cleaned shipment data in Python and pandas, cutting report preparation from two days to four hours."]
    res = assemble_cv(template_source, prof, JD, _Splitter(), workdir, tailor=False, fill_writes=True)
    split = [a for a in res.added if a["kind"] == "split"]
    assert len(split) == 2 and all(a["label"] == "Lakeshore Logistics, Data Analyst" for a in split)
    first = res.profile_used["experience"][0]["roles"][0]["bullets"]
    assert any(b.startswith("Built weekly SQL") for b in first)
    assert any(b.startswith("Cleaned shipment data") for b in first)


# --------------------------------------------------------------------------- the fill
@requires_latex
def test_a_thin_profile_gets_more_bullets_on_its_most_recent_roles(template_source, workdir):
    res = assemble_cv(template_source, THIN, JD, FakeLLM(), workdir, tailor=True, fit_lines=True)
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0
    assert res.fill_ratio is not None and res.fill_ratio <= MAX_FILL
    c = _counts(res.profile_used)
    # The staircase (Kofi, 2026-11-10): the most recent role carries the most, every older role
    # strictly fewer than the one above it, down to one.
    assert c[0] == max(c) and c[0] >= 3
    assert all(b <= a for a, b in zip(c, c[1:])), c
    # where a step could only be made by cutting a short page shorter, the note asks for more
    assert A.staircase_ok(res.profile_used) or any("no more than the older" in n
                                                   for n in res.bullet_notes)
    # The page keeps the template's own size: a thin page stays short (the invite asks for more)
    assert res.underfull
    # every written bullet is on the page and listed for the person to check
    assert res.written
    on_page = {b for _r, _e, r in ranked_roles(res.profile_used) for b in r["bullets"]}
    assert all(w["bullet"] in on_page for w in res.written)
    ch = what_changed(normalize_profile(THIN), res.profile_used, written=res.written)
    assert [w["text"] for w in ch["written"]] == [w["bullet"] for w in res.written]
    assert not ({r["after"] for r in ch["added_bullets"] + ch["reworded"]}
                & {w["bullet"] for w in res.written})
    assert any("written from your own material" in p for p in points(ch))


@requires_latex
def test_the_person_s_own_left_out_bullets_come_back_first_in_their_order(template_source, workdir):
    prof = copy.deepcopy(THIN)
    own = ["Automated the depot scorecard refresh with a scheduled Python job.",
           "Documented the delivery metrics so new analysts could rebuild each dashboard."]
    prof["experience"][0]["roles"][0]["bullets"] += own
    prof["experience"][0]["roles"][0]["bullets"].insert(1, "Answered depot questions about the SQL reports.")
    res = assemble_cv(template_source, prof, JD, None, workdir, tailor=False)
    first = res.profile_used["experience"][0]["roles"][0]["bullets"]
    assert len(first) == 5 and res.written == []
    assert first == prof["experience"][0]["roles"][0]["bullets"]      # author's order kept
    assert {a["kind"] for a in res.added} == {"saved"}


@requires_latex
def test_a_full_page_gets_nothing_added_and_stays_clean(template_source, workdir):
    from test_restorable_trims import JD as LONG_JD
    from test_restorable_trims import LONG
    res = assemble_cv(template_source, LONG, LONG_JD, FakeLLM(), workdir, tailor=True)
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0
    assert res.fill_ratio <= MAX_FILL
    # Only what fits is added (a page short of FILL_GOAL may take one more bullet), and the
    # bullets step down by recency.
    assert len(res.added) <= 2
    c = _counts(res.profile_used)
    assert c[0] >= 3 and c[0] == max(c)
    assert A.staircase_ok(res.profile_used) or any("no more than the older" in n
                                                   for n in res.bullet_notes)


# --------------------------------------------------------------------------- the builder
@requires_latex
def test_the_builder_says_what_it_wrote_and_still_invites_more_when_short(template_source, workdir):
    mem = ConversationMemory(":memory:")
    mem.save("default", copy.deepcopy(THIN), {}, [])
    s = WebIntake(JD, template_source, FakeLLM(), mem, CVRecords(":memory:"), workdir,
                  jobname="cv", palace_dir=workdir / "p")
    s.start()
    s.essentials["declined"] = ["address", "github", "linkedin", "blog", "projects", "courses"]
    st = s.build_from_saved()
    assert st["phase"] == "review", st.get("messages")
    said = " ".join(st["messages"])
    assert "I filled the rest with more of your recent work" in said and "summary above" in said
    assert st["build"]["written"] and "written for you" in st["build"]["summary"]
    assert st["changes"]["written"] and all(w["label"] and w["text"] for w in st["changes"]["written"])
    assert s.assembled.underfull and (st.get("light_cv") or {}).get("light") is True
    # A rebuild after the person's own edit writes nothing new, and keeps listing what is still there
    before = list(s._written)
    s._assemble()
    assert s._written == [w for w in before if w["bullet"] in
                          {b for _r, _e, r in ranked_roles(s.assembled.profile_used) for b in r["bullets"]}]
    assert s.assembled.written == []


# --------------------------------------------------------------------------- recency comes first
def test_make_room_cuts_older_material_in_kofis_order_and_can_undo():
    from tailoring.assembler import _Cuts, _make_room
    page = normalize_profile({
        "summary": "Analyst.",
        "skills": {f"Line {i}": "SQL, Excel" for i in range(6)},
        "projects": [{"org": "P1", "title": "Creator", "bullets": ["p1 a.", "p1 b.", "p1 c."]},
                     {"org": "P2", "title": "Creator", "bullets": ["p2 a.", "p2 b."]}],
        "experience": [_role("Acme", "2021 - Present", 2), _role("Beta", "2019 - 2021", 2),
                       _role("Cobalt", "2017 - 2019", 3), _role("Delta", "2015 - 2017", 3)]})
    whole = copy.deepcopy(page)
    trims: list = []
    cuts = _Cuts(trims, (), {})
    seen, undo = [], []

    def shape(p):
        return (_counts(p), [len(x["bullets"]) for x in p["projects"]], len(p["skills"]))
    while True:
        u = _make_room(page, cuts, sorted(ranked_roles(page), key=lambda x: x[0]))
        if u is None:
            break
        undo.append(u)
        seen.append(shape(page))
    assert seen == [
        ([2, 2, 3, 2], [3, 2], 6),      # (1) older roles beyond two, oldest first
        ([2, 2, 2, 2], [3, 2], 6),
        ([2, 2, 2, 2], [3, 1], 6),      # (2) project bullets beyond one, last project first
        ([2, 2, 2, 2], [2, 1], 6),
        ([2, 2, 2, 2], [1, 1], 6),
        # The oldest role keeps two (Kofi, 2026-11-10: it used to go down to one).
        ([2, 2, 2, 2], [1, 1], 5),      # (3) skills lines beyond the minimum (4)
        ([2, 2, 2, 2], [1, 1], 4),
    ]
    assert len(trims) == 7 and page["summary"] == "Analyst." and len(page["experience"]) == 4
    for u in reversed(undo):
        u()
    assert page == whole and trims == []


def _near_full():
    p = copy.deepcopy(THIN)
    p["education"][0]["courses"] = ("Regression, Probability, Data Mining, Database Systems, "
                                    "Survey Methods, Statistical Computing")
    p["extracurricular"].append({"title": "Treasurer, UIC Statistics Club", "date": "2016 - 2018", "bullets": [
        "Kept the club budget in a shared spreadsheet and reported spending to members each term.",
        "Organized a yearly data challenge with a local hospital that drew forty student teams.",
        # The Skills block holds at most 5 lines (linefit.SKILL_LINES_MAX), so the line a 6th
        # skills line used to take is taken here, keeping the page as full as before.
        "Ran weekly R workshops for first-year members."]})
    p["skills"]["Reporting"] = "Monthly reporting, KPI tracking, Excel pivot tables"
    p["projects"] = [
        {"org": "Depot Delay Tracker", "title": "Creator", "dates": "2024", "bullets": [
            "Built a Python and SQL tracker that pulls public freight delay data each night and charts late arrivals by depot and weekday.",
            "Wrote a short guide so other analysts could rerun the tracker against their own depots without changing the code.",
            "Shared weekly Tableau charts from the tracker with a local logistics meetup and took requests for new views."]},
        {"org": "Clinic Wait Time Study", "title": "Volunteer Analyst", "dates": "2022", "bullets": [
            "Cleaned two years of anonymized clinic visit logs in Excel and SQL and reported median waits by hour to the clinic board.",
            "Ran a simple A/B comparison of two check-in layouts and wrote up the result for the front desk team."]}]
    return p


@requires_latex
def test_on_a_near_full_page_the_recent_roles_still_get_three_by_trimming_older_material(
        template_source, workdir):
    prof = _near_full()
    plain = assemble_cv(template_source, prof, JD, None, workdir, jobname="plain", tailor=False)
    # No more material for the most recent role: stepping the older roles down would only
    # leave white space, so they keep their bullets and a note asks for more.
    assert _counts(plain.profile_used) == [2, 2, 2, 2] and plain.fill_ratio > 0.88   # nearly full
    assert any("no more than the older" in n for n in plain.bullet_notes), plain.bullet_notes
    res = assemble_cv(template_source, prof, JD, FakeLLM(), workdir, jobname="cv", tailor=True)
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0
    assert res.fill_ratio <= MAX_FILL
    c = _counts(res.profile_used)
    assert c[0] >= 3 and len(c) == 4 and c[0] == max(c)                      # no role dropped
    # room came from project bullets beyond one each, and the staircase from older roles
    assert res.trims and all(t["kind"] == "bullet" for t in res.trims)
    assert all(t["section"] == "projects" or t.get("staircase") for t in res.trims)
    assert all(len(p["bullets"]) >= 1 for p in res.profile_used["projects"])
    assert len(res.profile_used["projects"]) == 2


@requires_latex
def test_when_no_room_can_be_made_the_page_stays_clean_and_says_so(template_source, workdir,
                                                                   monkeypatch):
    monkeypatch.setattr(A, "_make_room", lambda *a, **k: None)
    res = assemble_cv(template_source, _near_full(), JD, FakeLLM(), workdir, tailor=True)
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0
    assert res.fill_ratio <= MAX_FILL
    assert any("would not fit on one page" in n for n in res.bullet_notes), res.bullet_notes


# --------------------------------------------------------------------------- a split stays split
# Real builds (2026-11-06): the most recent role's first bullet was split in two, then the line
# fit lengthened each part with the other's clause, so the page printed the whole original
# again beside its own second half; the second part alone was a thin present-tense half line.
_SRC = ("Lead business intelligence across the company's products, building KPI dashboards and "
        "automated Python and SQL reporting that track growth, usage, and unit costs, and turn "
        "the results into roadmap and spend decisions.")
_PART1 = ("Led business intelligence across the company's products, building KPI dashboards and "
          "automated Python and SQL reporting that track growth, usage, and unit costs.")
_PART2 = ("Turned the dashboard and reporting results into decisions on the product roadmap and "
          "company spend.")
_THIN2 = "Turn the reporting results into roadmap and spend decisions."
# what the line fit made of the parts on the real builds, word for word
_REAL4_1 = ("Lead business intelligence across the company's products, building KPI dashboards and "
            "automated Python and SQL reporting that track growth, usage, and unit costs, then turn "
            "those results into roadmap and spend decisions for leadership.")
_REAL4_2 = ("Turned KPI dashboard and automated reporting results on growth, usage and unit costs "
            "into roadmap and spend decisions.")
_REAL5_1 = ("Lead business intelligence across the company's products, building KPI dashboards and "
            "automated Python and SQL reporting that track growth, usage, and unit costs, so company "
            "leaders can make better roadmap and spend decisions.")
_REAL1 = ("Lead business intelligence across the company's products, building KPI dashboards and "
          "automated Python and SQL reporting that track growth, usage, and unit costs.")


def test_the_sibling_guards_catch_a_part_that_takes_back_its_partners_clause():
    from tailoring.filler import borrows_from_sibling, repeats_sibling
    assert repeats_sibling("", _REAL4_2, [_REAL4_1])         # the pair that printed
    assert borrows_from_sibling(_REAL1, _REAL5_1, [_THIN2])  # said again in other words
    assert borrows_from_sibling(_THIN2, _REAL4_2, [_REAL1])
    # an ordinary resize that adds its own detail still passes
    assert not borrows_from_sibling(_THIN2, "Turned the reporting results into roadmap and spend "
                                            "decisions for the leadership team.", [_REAL1])


def test_a_split_part_must_be_a_whole_achievement_in_the_roles_tense():
    from tailoring.assembler import _role_tense, _split_ok
    ended = {"dates": "Feb 2018 - Sept 2024"}
    current = {"dates": "March 2026 - Present"}
    assert _role_tense(ended, {}, ["Lead the team."]) == "past"
    assert _role_tense(current, {}, ["Built an AI-assisted pipeline."]) == "past"
    assert _role_tense(current, {}, ["Lead the BI team.", "Own the KPI framework."]) == "present"
    assert _split_ok([_PART1, _PART2], _SRC, _SRC, [], JD, "past")
    assert not _split_ok([_PART1, _THIN2], _SRC, _SRC, [], JD, "past")      # thin, present
    assert not _split_ok([_PART1, _PART2], _SRC, _SRC, [], JD, "present")   # wrong tense


class _SplitThenRegrow(FakeLLM):
    """Splits the long bullet in two, writes nothing, and on every resize hands back the part
    with its partner's clause glued on, as the real model did."""

    def __init__(self, parts=(_PART1, _PART2)):
        super().__init__()
        self.parts = list(parts)
        self.resized = []

    def write_role_bullet(self, *a, **k):
        return ""

    def split_bullet(self, role_title, text, parts, jd_text="", tense="past"):
        return list(self.parts) if "roadmap" in text else [text]

    def fit_bullet(self, text, min_chars, max_chars, jd_text="", context=""):
        self.resized.append(text)
        if text.startswith("Led business"):
            return text.rstrip(".") + ", then turned the dashboard and reporting results into decisions on the product roadmap."
        if text.startswith("Turned the dashboard"):
            return ("Turned KPI dashboard and automated reporting results on growth, usage and unit "
                    "costs into decisions on the product roadmap and company spend.")
        return text


def _first_role(res):
    return res.profile_used["experience"][0]["roles"][0]["bullets"]


def _src_profile():
    prof = copy.deepcopy(_near_full())
    prof["experience"][0]["roles"][0]["bullets"] = [
        _SRC, "Built weekly SQL and Tableau dashboards that track on-time delivery for 14 regional depots."]
    return prof


@requires_latex
def test_a_tailored_then_split_bullet_never_leaves_its_original_clause_on_the_page(
        template_source, workdir):
    from tailoring.filler import _fact_tokens, _longest_shared_run
    llm = _SplitThenRegrow()
    res = assemble_cv(template_source, _src_profile(), JD, llm, workdir, tailor=True, fit_lines=True)
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0
    first = _first_role(res)
    assert len([a for a in res.added if a["kind"] == "split"]) == 2 and len(first) >= 3
    assert not any("roadmap" in b and "unit costs" in b for b in first), first
    for i, a in enumerate(first):                      # no two bullets share a main clause
        for b in first[i + 1:]:
            assert _longest_shared_run(_fact_tokens(a), _fact_tokens(b)) < 5, (a, b)
    assert all(_opens(b) for b in first)                 # one tense: past


def _opens(b):
    from tailoring.assembler import _opens_in_past
    return _opens_in_past(b) or b.split(":")[0].count(",") >= 1   # FakeLLM's term prefix


@requires_latex
def test_a_thin_second_part_keeps_the_bullet_whole(template_source, workdir):
    res = assemble_cv(template_source, _src_profile(), JD, _SplitThenRegrow((_PART1, _THIN2)),
                      workdir, tailor=True)
    assert [a for a in res.added if a["kind"] == "split"] == []
    assert not any(b.endswith(_THIN2) for b in _first_role(res))
    assert any("Lead business intelligence" in b for b in _first_role(res))    # kept whole


def test_the_line_fit_never_takes_the_point_of_a_bullet_cut_to_fit(monkeypatch):
    """Putting a cut bullet back must not print its point twice: a resize that borrows it is
    refused, even though the fact gate knows the words (they are the person's own)."""
    from types import SimpleNamespace
    from tailoring import linefit as LF
    from tailoring.fact_gate import known_material, source_figures
    kept = "Built SQL data management pipelines at CSIR for structured querying and analytical reporting."
    cut = "Reviewed the lab's survey records each week and corrected entry errors at the source."
    grown = kept.rstrip(".") + ", and reviewed the lab's survey records each week."
    p = {"identity": {"name": "T"}, "experience": [{"org": "CSIR", "roles": [
        {"title": "Research Assistant", "bullets": [kept]}]}]}
    short = LF.LineReport(items=[LF.ItemFit("bullet", "CSIR", kept, lines=1, last_fill=0.5, slot=0)],
                          left=0.0, right=400.0)
    full = LF.LineReport(items=[LF.ItemFit("bullet", "CSIR", grown, lines=1, last_fill=0.97, slot=0)],
                         left=0.0, right=400.0)
    calls = {"n": 0}

    def fake_measure(*_a, **_k):
        calls["n"] += 1
        return short if calls["n"] == 1 else full

    class _Grow(FakeLLM):
        def fit_bullet(self, text, min_chars, max_chars, jd_text="", context=""):
            return grown

    def run(trimmed):
        calls["n"] = 0
        return LF.fit_lines(copy.deepcopy(p), SimpleNamespace(pdf_path="x.pdf"),
                            render=lambda _p: "", compile_fn=lambda _t, _n: SimpleNamespace(pdf_path="y.pdf"),
                            is_clean=lambda _r: True, llm=_Grow(), jd_text="", rounds=1,
                            known=known_material(p, cut), known_figures=source_figures(p),
                            trimmed=trimmed)
    monkeypatch.setattr(LF, "measure", fake_measure)
    out = run({"CSIR, Research Assistant": [cut]})
    assert out.profile["experience"][0]["roles"][0]["bullets"] == [kept]
    assert any("cut to fit" in str(r["claims"]) for r in out.rejected)
    # without the cut on record the resize is not refused for that reason: the test exercises
    # the real path. (It is refused all the same, as padding: the words it adds are not this
    # entry's, tailoring/linefit._pads_out.)
    again = run({})
    assert not any("cut to fit" in str(r["claims"]) for r in again.rejected)
    assert any("padding" in str(r["claims"]) for r in again.rejected)


# --------------------------------------------------------------------------- restore what fits
def _room_build(template_source, workdir):
    # The build without its own restore pass, so the room cuts are still on record.
    real = A._restore_room
    A._restore_room = lambda pre, used, result, trims, **k: (used, result, trims)
    try:
        res = assemble_cv(template_source, _near_full(), JD, FakeLLM(), workdir, jobname="cv",
                          tailor=True, fit_lines=True)
    finally:
        A._restore_room = real
    assert res.ok and any(t.get("room") for t in res.trims), res.trims
    return res


def _measure(template_source, workdir, page, name):
    from tailoring.assembler import extract_preamble
    from tailoring.compiler import compile_tex
    pre = extract_preamble(template_source)
    return pre, compile_tex(render_cv(pre, page, probe=True), workdir, jobname=name)


@requires_latex
def test_a_room_cut_that_a_later_height_change_made_unnecessary_is_put_back(template_source, workdir):
    from tailoring.assembler import _restore_room
    res = _room_build(template_source, workdir)
    cut = next(t for t in res.trims if t.get("room"))
    page = copy.deepcopy(res.profile_used)
    page["extracurricular"] = page["extracurricular"][:1]          # a later change frees lines
    pre, r0 = _measure(template_source, workdir, page, "shorter")
    page, r, trims = _restore_room(pre, page, r0, res.trims, sections=None, headings=None,
                                   workdir=workdir, jobname="t")
    assert r.ok and r.pages == 1 and r.overfull_count == 0 and r.fill_ratio <= MAX_FILL
    assert r.fill_ratio > r0.fill_ratio
    assert cut not in trims                                         # off the Put back list
    on_page = {b for p in page["projects"] for b in p["bullets"]}
    assert cut["text"] in on_page
    assert _counts(page)[:2] == _counts(res.profile_used)[:2]       # minimums untouched


@requires_latex
def test_a_restore_that_would_overflow_is_skipped(template_source, workdir):
    from tailoring.assembler import _restore_room
    res = _room_build(template_source, workdir)
    page = copy.deepcopy(res.profile_used)
    pre, r0 = _measure(template_source, workdir, page, "same")
    # One restore pass puts back what fits; what is left on the list would overflow.
    page, r1, left = _restore_room(pre, page, r0, res.trims, sections=None, headings=None,
                                   workdir=workdir, jobname="t")
    before = copy.deepcopy(page)
    after, r, trims = _restore_room(pre, page, r1, left, sections=None, headings=None,
                                    workdir=workdir, jobname="t2")
    assert trims == left and after == before                     # nothing fit, nothing changed
    assert r is r1
