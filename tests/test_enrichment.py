"""Acceptance tests for the page-filling enrichment interview (CLAUDE.md §8, §12).

After the skeleton is complete the tool builds a real one-page CV, then — if the
page is light — proactively draws out the person's REAL material (accomplishments
per role, projects, skills, certifications, interests) a couple of topics at a
time, acknowledging answers and never re-asking. Nothing is fabricated to fill
space; declined sections are skipped; and a returning user is not re-interviewed.
"""

from __future__ import annotations

from intake.memory import ConversationMemory
from tailoring.assembler import extract_preamble, render_cv
from ui.records import CVRecords
from ui.session import WebIntake
from conftest import requires_latex

JD = "Data Analyst - BlackOrigin - Accra. SQL, Python, dashboards, statistics, data pipelines."

# Enough to satisfy the SKELETON gate (declines the optional/critical-declinable bits).
# The mandatory outside-work sections (Extracurricular + Interests) are now collected
# BEFORE the CV is built, so the skeleton alone no longer triggers a build.
SKELETON = (
    "I'm Kwame Asante, kwame@example.com. B.S. in Statistics at University of Ghana, "
    "graduated June 2019. Data Analyst at BlackOrigin in Accra, Ghana since Feb 2021. "
    "Analyst at Stanbic Bank in Accra, Ghana from Jan 2018 to Dec 2020. No github, no "
    "blog, no linkedin. No address. Skip courses. No projects."
)

# The person's answer to the pre-build Extracurricular + Interests questions — providing
# these (or declining them) is what now lets the CV build.
EXTRAS = ("Outside work I volunteer coaching a youth football team on weekends, and I "
          "enjoy chess, hiking, and reading.")


def _s(template_source, fake_llm, workdir):
    return WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"),
                     CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")


@requires_latex
def test_builds_then_invites_real_material_when_light(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    st0 = s.submit(SKELETON)                             # first: asks the outside-work sections
    assert st0["phase"] == "chatting"                    # no half-page CV before they're addressed
    st = s.submit(EXTRAS)                                # provided -> the CV builds
    assert st["phase"] == "review"                       # a real CV exists once they're in
    assert st.get("light_cv", {}).get("light") is True   # still flagged light (placeholder bullets)
    # It invites REAL accomplishments, not a wall of questions, and offers an out.
    invite = " ".join(st["messages"]).lower()
    assert "blackorigin" in invite
    assert "invent" not in invite or "won't invent" in invite


@requires_latex
def test_real_accomplishments_become_the_bullets(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    s.submit(EXTRAS)                                     # clears the pre-build required sections
    s.submit("At BlackOrigin I built an automated reporting pipeline that cut monthly close "
             "from 5 days to 1, and I designed 12 dashboards used across the bank.")
    bo = next(j for j in s.essentials["experience"] if j["org"] == "BlackOrigin")
    assert any("automated reporting pipeline" in b for b in bo["bullets"])
    assert any("12 dashboards" in b for b in bo["bullets"])
    tex = render_cv(extract_preamble(template_source), s.profile)
    assert "automated reporting pipeline" in tex           # the person's real words on the CV
    assert "Delivered" not in tex.split("BlackOrigin")[1].split("Stanbic")[0]  # no filler for BO


@requires_latex
def test_enrichment_never_reasks_and_walks_topics(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    st1 = s.submit(EXTRAS)                                # builds, then invites real accomplishments
    asked1 = " ".join(st1["messages"])
    assert "BlackOrigin" in asked1
    # Answer BlackOrigin; the next invite must move on, never re-ask BlackOrigin.
    st2 = s.submit("At BlackOrigin I automated the monthly reporting pipeline and cut close time.")
    asked2 = " ".join(st2["messages"])
    assert "BlackOrigin" not in asked2                    # answered -> never re-asked
    assert any(w in asked2.lower() for w in ("added", "perfect", "nice", "great"))  # acknowledges


@requires_latex
def test_skills_and_certifications_from_the_person_render(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    s.submit(EXTRAS)                                     # clears the pre-build required sections
    s.submit("My skills are SQL, Python, Power BI, and Tableau. I am AWS Certified Data Analytics.")
    assert "Tableau" in (s.essentials.get("skills_input") or [])
    assert "AWS Certified Data Analytics" in (s.essentials.get("certifications") or [])
    tex = render_cv(extract_preamble(template_source), s.profile)
    assert "Tableau" in tex and "AWS Certified Data Analytics" in tex


@requires_latex
def test_that_is_everything_does_not_finalize_while_page_sections_open(
        template_source, fake_llm, workdir):
    """A half page is not a CV: the mandatory Extracurricular / Interests sections are
    collected BEFORE the build, so 'that's everything' can't even produce a CV while they
    are still blank and undeclined — the tool keeps insisting (never fabricating)."""
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)   # skeleton declines github/projects/... but NOT extras/interests
    st = s.submit("that's everything")
    assert st["phase"] == "chatting"                            # NO CV while the sections are open
    assert s.assembled is None                                  # nothing built to accept
    assert s.essentials.get("enrich_done") is not True          # NOT finalized
    invite = " ".join(st["messages"]).lower()
    assert any(w in invite for w in ("outside", "volunteer", "club", "team", "interest", "hobb"))


@requires_latex
def test_that_is_everything_finalizes_once_page_sections_addressed(
        template_source, fake_llm, workdir):
    """Finalize only when the page-filling sections are provided OR explicitly
    declined — then 'that's everything' truly stops and the CV can be accepted."""
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    # Explicitly opt out of the outside-work sections, then finalize.
    s.submit("I don't have any extracurriculars, and no hobbies to add.")
    st = s.submit("that's everything")
    assert st["phase"] == "review"
    assert s.essentials.get("enrich_done") is True
    assert s.accept().get("ok") is True
    assert "hiking" not in render_cv(extract_preamble(template_source), s.profile).lower()


@requires_latex
def test_override_still_draws_out_required_template_sections(template_source, fake_llm, workdir):
    """A build-override skips the OPTIONAL enrichment (extra accomplishments,
    projects, skills, certs) but must still draw out Extracurricular / Interests —
    those are their own template sections, and skipping them leaves a visible gap
    (the user's complaint). It builds immediately AND invites the required sections."""
    s = _s(template_source, fake_llm, workdir)
    s.start()
    s.submit(SKELETON)
    st = s.submit("just build it with what you have")
    assert st["phase"] == "review"                       # override builds immediately
    assert s.essentials.get("override") is True
    invite = " ".join(st["messages"]).lower()
    # The required outside-work / interests sections are drawn out even under override…
    assert any(w in invite for w in ("outside", "volunteer", "club", "team", "interest", "hobb"))
    # …but the OPTIONAL enrichment (per-role accomplishments) is NOT interrogated.
    assert "blackorigin" not in invite
    # And an answer to the required-section invite is absorbed even under override.
    s.submit("I volunteer coaching a youth football team on weekends.")
    assert s.essentials.get("extracurricular")


def test_experience_location_is_never_dropped(template_source, fake_llm, workdir):
    """Hard rule (CLAUDE.md): every company under Experience shows its city/country.
    The drafting model can silently drop the location, so it is restored
    deterministically from the person's essentials by matching the company name."""
    s = _s(template_source, fake_llm, workdir)
    s.essentials["experience"] = [{"org": "Viva Technologies", "title": "Data Analyst",
                                   "dates": "January 2021 - August 2023",
                                   "location": "Accra, Ghana"}]
    # A drafted profile that lost the location (as the real model sometimes returns).
    s.profile = {"experience": [{"org": "Viva Technologies", "location": "",
                 "roles": [{"title": "Data Analyst", "dates": "January 2021 - August 2023",
                            "bullets": []}]}]}
    s._apply_essentials_locations()
    assert s.profile["experience"][0]["location"] == "Accra, Ghana"
    tex = render_cv(extract_preamble(template_source), s.profile)
    assert "Accra" in tex   # and the location reaches the rendered CV (city always shown)


def test_year_only_dates_get_flagged_placeholder_months(template_source, fake_llm, workdir):
    """Hard rule: every experience role shows FULL months. A year-only date ('2019 -
    2020') breaks the template. Under a build override it's filled with placeholder
    months and rendered in a warning colour (never silently shipped)."""
    from tailoring.conform import date_lacks_month, dummy_months
    assert date_lacks_month("2019 - 2020") is True
    assert date_lacks_month("January 2021 - August 2023") is False
    assert date_lacks_month("Feb 2023 - Present") is False
    assert dummy_months("2019 - 2020") == "Jan 2019 - Dec 2020"

    s = _s(template_source, fake_llm, workdir)
    s.essentials["override"] = True
    s.profile = {"experience": [{"org": "BlackOrigin", "location": "Accra, Ghana",
                 "roles": [{"title": "Analyst", "dates": "2019 - 2020", "bullets": []}]}]}
    flagged = s._flag_placeholder_dates()
    assert flagged == ["BlackOrigin"]
    role = s.profile["experience"][0]["roles"][0]
    assert role["dates"] == "Jan 2019 - Dec 2020"
    assert role["dates_placeholder"] is True
    tex = render_cv(extract_preamble(template_source), s.profile)
    assert "\\textcolor{red}" in tex          # flagged in colour on the CV
    assert "Jan 2019 - Dec 2020" in tex


def test_months_gate_captures_answer_and_never_loops(template_source, fake_llm, workdir):
    """Regression: the 'insist on months' gate must not loop. It asks a year-only role
    for its months ONCE; a date answer ('feb 2026 to present') is captured onto that
    role (dates aren't named with the org), and it's never re-asked."""
    from ui.session import _jd_label, _wants_placeholder
    # A garbled JD (scraped 'Company logo …') never yields a junk role label.
    role, _c = _jd_label("Company logo for, KPMG US.\nKPMG US\nSenior Data Analyst")
    assert "logo" not in role.lower()
    assert _jd_label("Data Analyst at BlackOrigin") == ("Data Analyst", "BlackOrigin")
    assert _wants_placeholder("put some dummy data there") is True

    s = _s(template_source, fake_llm, workdir)
    s.essentials["experience"] = [{"org": "BlackOrigin", "title": "Analyst",
                                   "dates": "2019 - 2020", "location": "Accra, Ghana"}]
    asks = s._months_to_ask()
    assert asks and asks[0][0] == "BlackOrigin"          # asked for months
    s._capture_dates("feb 2026 to present")              # a bare date answer…
    assert s.essentials["experience"][0]["dates"] == "Feb 2026 - Present"  # …lands on the role
    assert s._months_to_ask() == []                      # resolved -> never re-asked

    # And if unanswered, marking it asked stops the loop (build fills a placeholder).
    s2 = _s(template_source, fake_llm, workdir)
    s2.essentials["experience"] = [{"org": "BlackOrigin", "title": "Analyst",
                                    "dates": "2019 - 2020", "location": "Accra, Ghana"}]
    s2.essentials["months_asked"] = ["blackorigin"]
    assert s2._months_to_ask() == []


def test_explicit_section_directive_moves_entry(template_source, fake_llm, workdir):
    """Control plane: an explicit 'X is an extracurricular activity' MOVES the entry
    from Experience to Extracurricular (overriding the LLM), pins it, and keeps it there
    even if a later re-extraction misplaces it — the person's placement is authoritative."""
    s = _s(template_source, fake_llm, workdir)
    s.essentials["experience"] = [
        {"org": "BlackOrigin", "title": "Analyst", "dates": "Jan 2020 - Present",
         "location": "Accra, GH", "bullets": ["Ran analysis."]},
        {"org": "Covid-19 Vaccination Drive (Volunteer)", "title": "Bot Developer",
         "dates": "May 2021 - July 2021", "location": "Accra, GH",
         "bullets": ["Built a bot on AWS to notify vaccine availability."]},
    ]
    s._apply_side_channels("The Covid-19 Vaccination Drive is an extracurricular activity, not a job.")
    exp = [e["org"] for e in s.essentials["experience"]]
    assert "Covid-19 Vaccination Drive (Volunteer)" not in exp          # gone from experience
    assert any("Covid" in x["title"] for x in s.essentials.get("extracurricular", []))  # now extracurricular
    assert s.essentials["section_pins"]["covid-19 vaccination drive (volunteer)"] == "extracurricular"
    # A re-extraction that puts it back under experience is corrected by the pin.
    s.essentials["experience"].append({"org": "Covid-19 Vaccination Drive (Volunteer)",
                                       "title": "Bot Developer", "dates": "May 2021 - July 2021",
                                       "bullets": []})
    s._enforce_section_pins()
    assert "Covid-19 Vaccination Drive (Volunteer)" not in [e["org"] for e in s.essentials["experience"]]
    assert len([x for x in s.essentials["extracurricular"]
                if "Covid" in x["title"]]) == 1                          # de-duped


def test_volunteer_entry_defaults_to_extracurricular_but_job_stays(template_source, fake_llm, workdir):
    """Conservative heuristic: a clearly-volunteer entry defaults to extracurricular; a
    formal job (no volunteer marker in the name) stays in experience — placement is
    genuinely ambiguous, so the heuristic only fires on explicit markers."""
    s = _s(template_source, fake_llm, workdir)
    s.essentials["experience"] = [
        {"org": "Meridian Bank", "title": "Data Analyst", "dates": "2020 - 2021", "bullets": []},
        {"org": "Habitat Build (Volunteer)", "title": "Helper", "dates": "2019", "bullets": []},
    ]
    s._classify_volunteer_extracurricular()
    exp = [e["org"] for e in s.essentials["experience"]]
    assert "Meridian Bank" in exp                                       # formal job stays
    assert "Habitat Build (Volunteer)" not in exp                       # volunteer moves
    assert any("Habitat" in x["title"] for x in s.essentials.get("extracurricular", []))


def test_extracurricular_dates_get_full_month_ranges_like_experience(template_source, fake_llm, workdir):
    """Consistency: EVERY dated section follows the template's date standard. A year-only
    Extracurricular date is filled with a flagged full-month RANGE and rendered in red —
    the same treatment Experience gets — not shipped as a bare '2021'."""
    from tailoring.conform import dummy_months
    assert dummy_months("2021") == "Jan 2021 - Dec 2021"            # lone year -> range
    s = _s(template_source, fake_llm, workdir)
    s.essentials["override"] = True
    s.profile = {"experience": [],
                 "extracurricular": [{"title": "Covid-19 Vaccination Drive (Volunteer)",
                                      "date": "2021", "bullets": ["Built a bot."]}]}
    flagged = s._flag_placeholder_dates()
    assert "Covid-19 Vaccination Drive (Volunteer)" in flagged
    x = s.profile["extracurricular"][0]
    assert x["date"] == "Jan 2021 - Dec 2021"                       # full-month range
    assert x["dates_placeholder"] is True
    tex = render_cv(extract_preamble(template_source), s.profile)
    assert "\\textcolor{red}" in tex and "Jan 2021 - Dec 2021" in tex   # flagged red on the CV


@requires_latex
def test_date_picker_edit_reformats_validates_and_clears_placeholder(template_source, fake_llm, workdir):
    """The edit-view month/year picker: a chosen date runs through the template pipeline
    (abbreviated + chronologically validated) and clears the red placeholder; an
    impossible range is ignored so the old value stays."""
    from tailoring.assembler import assemble_cv
    s = _s(template_source, fake_llm, workdir)
    s.stage = "review"
    s.profile = {"identity": {"name": "K"}, "education": [], "skills": {},
                 "experience": [], "projects": [],
                 "extracurricular": [{"title": "Covid Drive", "date": "Jan 2021 - Dec 2021",
                                      "dates_placeholder": True, "bullets": ["Built a bot."]}],
                 "interests": ""}
    s.assembled = assemble_cv(s.template, s.profile, s.jd, s.llm, s.workdir,
                              jobname="cv", tailor=False)
    s.edit_date("extra-0", "May 2021 to July 2021")
    x = s.profile["extracurricular"][0]
    assert x["date"] == "May 2021 - July 2021"      # reformatted to template style
    assert "dates_placeholder" not in x             # red flag cleared
    s.edit_date("extra-0", "December 2025 - February 2020")   # impossible -> ignored
    assert s.profile["extracurricular"][0]["date"] == "May 2021 - July 2021"


def test_gate_never_asks_for_fields_already_on_the_cv(template_source, fake_llm, workdir):
    """Regression: a returning user whose STORED essentials predate the location
    fields (but whose saved PROFILE has them) must not be asked for locations that
    are already rendered on the CV — the gate checks the built/saved profile."""
    mem = ConversationMemory(":memory:")
    recs = CVRecords(":memory:")
    saved_profile = {
        "identity": {"name": "Kofi Gilbert", "email": "kofi@example.com"},
        "education": [{"school": "DePaul University", "degree": "MBA - Business Analytics",
                       "date": "December 2025", "location": "Chicago, IL"}],
        "experience": [
            {"org": "BlackOrigin", "location": "Chicago, IL",
             "roles": [{"title": "AI Systems Architect", "dates": "February 2026 - Present",
                        "bullets": ["Built the platform"]}]},
            {"org": "Stanbic Bank Ghana", "location": "Accra, GH",
             "roles": [{"title": "Product Manager", "dates": "February 2018 - September 2024",
                        "bullets": ["Ran lending"]}]},
        ],
    }
    stored_essentials = {   # the pre-fix shape: no location fields
        "identity": {"name": "Kofi Gilbert", "email": "kofi@example.com"},
        "education": [{"school": "DePaul University", "degree": "MBA - Business Analytics",
                       "date": "December 2025"}],
        "experience": [
            {"org": "BlackOrigin", "title": "AI Systems Architect", "dates": "February 2026 - Present"},
            {"org": "Stanbic Bank Ghana", "title": "Product Manager",
             "dates": "February 2018 - September 2024"}],
        "declined": ["github", "linkedin", "blog", "courses", "projects", "address"],
    }
    mem.save("kofi", saved_profile, stored_essentials, [])

    s = WebIntake(JD, template_source, fake_llm, mem, recs, workdir, jobname="cv",
                  palace_dir=workdir / "p", profile_name="kofi")
    # Locations are backfilled from the saved profile, so nothing is missing.
    assert [j.get("location") for j in s.essentials["experience"]] == ["Chicago, IL", "Accra, GH"]
    assert s._gate() == []
    s.start()
    st = s.submit("complete with what you already know")
    st2 = s.submit("you already have all this on the cv")
    asked = " ".join(st["messages"] + st2["messages"]).lower()
    assert "location" not in asked and "located" not in asked   # never re-asks known locations
    assert "where was" not in asked and "where were" not in asked


def test_faculty_subunit_is_trimmed_from_school():
    """Template-strict: the template renders the institution name only, so an
    appended faculty/graduate-school clause is dropped (never a slot the template
    lacks). Real names like 'London School of Economics' are left intact."""
    from tailoring.conform import format_school
    assert format_school("DePaul University | Kellstadt Graduate School of Business") == "DePaul University"
    assert format_school("University of Professional Studies, Faculty of Law") == "University of Professional Studies"
    assert format_school("University of Cape Coast | Faculty of Social Sciences") == "University of Cape Coast"
    assert format_school("London School of Economics") == "London School of Economics"   # not trimmed
    assert format_school("University of Chicago") == "University of Chicago"


@requires_latex
def test_gate_does_not_interrogate_for_a_full_address(template_source, fake_llm, workdir):
    """The header's full address is OPTIONAL tier: a bare 'City, ST' never breaks the
    template, so the gate must not stall the build asking for a street address. It moves
    straight on to the mandatory outside-work sections, and a full address volunteered
    later is still stored."""
    s = _s(template_source, fake_llm, workdir)
    s.start()
    st = s.submit("I'm Nia Bello, nia@example.com. B.S. in Statistics at UCLA in 2016. "
                  "Analyst at Beacon in Chicago, IL since Jan 2020. I live in Chicago, IL. "
                  "No GitHub, LinkedIn, or blog. Skip courses. No projects.")
    assert st["phase"] == "chatting"                       # the outside-work sections come next
    assert "address" not in " ".join(st["messages"]).lower()
    st2 = s.submit("My full address is 42 State St, Chicago, IL 60603.")
    assert s.essentials["identity"]["address"] == "42 State St, Chicago, IL 60603"
    st3 = s.submit(EXTRAS)                                 # all mandatory fields in -> builds
    assert st3["phase"] == "review"
    assert s.profile["identity"]["address"] == "42 State St, Chicago, IL 60603"


def test_synonym_keys_are_canonicalized():
    """Robustness: the model sometimes returns fields under synonym keys; they must
    map to the canonical keys the gate/render use, so intake never re-asks for
    something already given."""
    from ui.session import _canonicalize_essentials
    e = _canonicalize_essentials({
        "identity": {"name": "Ada", "mail": "ada@x.com"},
        "education": [{"institution": "DePaul University", "program": "B.S. Statistics",
                       "graduated": "June 2019"}],
        "experience": [{"company": "Meridian", "role": "Analyst",
                        "start": "January 2021", "end": "Present", "location": "Chicago, IL"}],
    })
    assert e["identity"]["email"] == "ada@x.com"
    assert e["education"][0]["school"] == "DePaul University"
    assert e["education"][0]["degree"] == "B.S. Statistics"
    assert e["education"][0]["date"] == "June 2019"
    assert e["experience"][0]["org"] == "Meridian"
    assert e["experience"][0]["title"] == "Analyst"
    assert e["experience"][0]["dates"] == "January 2021 - Present"


def test_city_is_normalized_to_address(template_source, fake_llm, workdir):
    """#3: the extractor may store the location under 'city'; the gate/render use
    'address'. It must be copied so a typed address satisfies the gate."""
    s = _s(template_source, fake_llm, workdir)
    s.essentials["identity"] = {"name": "A", "email": "a@b.com", "city": "Chicago, IL"}
    s._conform_essentials()
    assert s.essentials["identity"]["address"] == "Chicago, IL"


def test_decline_phrase_is_never_captured_as_courses(template_source, fake_llm, workdir):
    """#4: 'Skip the courses, and I don't have any projects' must NOT put
    'I don't have any projects' onto the CV as a course."""
    s = _s(template_source, fake_llm, workdir)
    s.essentials["education"] = [{"school": "X", "degree": "B.S.", "date": "2020", "courses": ""}]
    s._apply_side_channels("Skip the courses, and I don't have any projects.")
    assert s.essentials["education"][0]["courses"] == ""
    # And a different decline phrasing must not be scraped either.
    s.essentials["education"][0]["courses"] = ""
    s._apply_side_channels("Please leave the courses off, and go ahead and tailor what you have.")
    assert s.essentials["education"][0]["courses"] == ""
    # A real course list still works (fresh session — the prior lines declined courses).
    s2 = _s(template_source, fake_llm, workdir)
    s2.essentials["education"] = [{"school": "X", "degree": "B.S.", "date": "2020", "courses": ""}]
    s2._apply_side_channels("My courses were Machine Learning and Statistics.")
    assert "Machine Learning" in s2.essentials["education"][0]["courses"]


def test_uploaded_cv_real_bullets_are_kept(template_source, fake_llm, workdir):
    """#2: a role's real bullet points from the uploaded CV are attached, not dropped."""
    s = _s(template_source, fake_llm, workdir)
    s.essentials["experience"] = [{"org": "Meridian Retail Group", "title": "Data Analyst",
                                   "dates": "March 2021 - Present", "location": "Chicago, IL"}]
    text = ("Experience\n"
            "Meridian Retail Group - Data Analyst - Chicago, IL - March 2021 to Present\n"
            "Built automated sales dashboards that cut monthly reporting time in half.\n"
            "Partnered with merchandising teams to guide inventory decisions.\n"
            "Skills\nSQL, Python\n")
    s._attach_cv_bullets(text)
    bl = s.essentials["experience"][0]["bullets"]
    assert any("automated sales dashboards" in b for b in bl)
    assert any("merchandising" in b for b in bl)


def test_drafted_bullets_flag_is_deterministic(template_source, fake_llm, workdir):
    """#1: whether a role's bullets are drafted is decided by us (did essentials have
    real bullets?), NOT by trusting the model — so the light-CV/enrichment trigger
    fires reliably. Real bullets also win over any draft."""
    s = _s(template_source, fake_llm, workdir)
    s.essentials["experience"] = [{"org": "Acme", "bullets": ["Shipped the real thing"]},
                                  {"org": "Beta", "bullets": []}]
    s.profile = {"experience": [
        {"org": "Acme", "roles": [{"title": "E", "bullets": ["a drafted placeholder"]}]},
        {"org": "Beta", "roles": [{"title": "E", "bullets": ["a drafted placeholder"]}]}]}
    s._apply_real_bullets()
    acme = s.profile["experience"][0]["roles"][0]
    beta = s.profile["experience"][1]["roles"][0]
    assert acme["bullets"] == ["Shipped the real thing"] and acme["drafted_bullets"] is False
    assert beta["drafted_bullets"] is True                 # no real bullets -> flagged light


def test_project_inherits_location_from_its_institution(template_source, fake_llm, workdir):
    """Regression (real-data bug): a project whose org is a school/company the person
    already listed inherits that place's location, so the gate never asks 'the
    location for DePaul University…' for a capstone that's really located in Chicago."""
    mem = ConversationMemory(":memory:")
    recs = CVRecords(":memory:")
    saved_ess = {
        "identity": {"name": "Kofi Gilbert", "email": "kofi@example.com"},
        "education": [{"school": "DePaul University", "degree": "MBA - Analytics",
                       "date": "December 2025", "location": "Chicago, IL"}],
        "experience": [{"org": "Stanbic Bank Ghana", "title": "Product Manager",
                        "dates": "February 2018 - September 2024", "location": "Accra, GH"}],
        "projects": [
            {"org": "DePaul University", "title": "Credit Risk Capstone", "dates": "2025",
             "bullets": ["Built a credit-scoring model"]},              # no location
            {"org": "Stanbic Bank Ghana", "title": "Portfolio Review", "dates": "2022",
             "bullets": ["Reviewed the lending book"]},                 # no location
        ],
        "declined": ["blog", "github", "linkedin", "courses", "address"],
    }
    mem.save("kofi", {}, saved_ess, [])
    s = WebIntake(JD, template_source, fake_llm, mem, recs, workdir, jobname="cv",
                  palace_dir=workdir / "p", profile_name="kofi")
    s._conform_essentials()
    locs = [p.get("location") for p in s.essentials["projects"]]
    assert locs == ["Chicago, IL", "Accra, GH"]        # inherited, not asked for
    # The gate (even the full tier) does not ask for any project location.
    assert not any("location for" in g for g in s._missing_required())


@requires_latex
def test_returning_user_is_not_reinterviewed(template_source, fake_llm, workdir):
    """A person whose enriched profile is saved should build straight from memory,
    not be walked back through the interview."""
    db = ConversationMemory(":memory:")
    recs = CVRecords(":memory:")
    s1 = WebIntake(JD, template_source, fake_llm, db, recs, workdir, jobname="cv",
                   palace_dir=workdir / "p", profile_name="kwame")
    s1.start()
    s1.submit(SKELETON)
    s1.submit("At BlackOrigin I built an automated pipeline that cut monthly close in half.")
    s1.submit("At Stanbic Bank I led a fraud-analytics model that flagged risky transactions.")
    s1.submit("that's everything")
    assert s1.accept().get("ok") is True

    # New session, same person, a NEW job — should recognize them and reuse.
    s2 = WebIntake("Business Analyst - Meridian - Remote. SQL, analytics, stakeholder reporting.",
                   template_source, fake_llm, db, recs, workdir, jobname="cv2",
                   palace_dir=workdir / "p", profile_name="kwame")
    opening = s2.opening().lower()
    assert s2.returning is True
    assert "welcome back" in opening and "blackorigin" in opening
