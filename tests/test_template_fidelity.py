"""Acceptance tests for template fidelity (CLAUDE.md §8): the expanded gate, date
and degree reformatting, the em/en-dash scrub, and full education rendering.
"""

from __future__ import annotations

from intake.memory import ConversationMemory
from tailoring.assembler import extract_preamble, normalize_profile, render_cv
from ui.records import CVRecords
from ui.session import WebIntake
from conftest import requires_latex

JD = "Data Analyst - Beacon - Chicago. SQL, Python, dashboards, statistics."


def _session(template_source, fake_llm, workdir):
    return WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"),
                     CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")


# -- expanded gate ------------------------------------------------------ #

@requires_latex
def test_gate_asks_for_template_elements_and_skips_only_on_decline(template_source, fake_llm, workdir):
    s = _session(template_source, fake_llm, workdir)
    s.start()
    # Name, email, a dated degree, and a role with a location — but no personal
    # address, no links, no courses, no projects.
    st = s.submit("I'm Ana Cruz, ana@example.com. B.S. in Economics at UCLA in 2016. "
                  "I work as Analyst at Beacon Capital (New York, NY) since Jan 2020.")
    assert st["phase"] == "chatting"                     # does not build with gaps
    ask = " ".join(st["messages"]).lower()
    # Asks conversationally for a couple of the optional elements — not the whole
    # list dumped at once.
    assert sum(k in ask for k in ("address", "github", "linkedin", "blog", "courses")) <= 3

    # Provide the address and explicitly decline the rest — the skeleton is now complete,
    # so it moves on to the mandatory outside-work sections (still not a build yet).
    st2 = s.submit("I live at 12 Maple Ave, Chicago, IL 60601. I don't have a GitHub. No blog. "
                   "No LinkedIn. Skip the courses. No projects.")
    assert st2["phase"] == "chatting"                    # now asks Extracurricular / Interests
    assert set(s.essentials["declined"]) >= {"github", "blog", "linkedin", "courses", "projects"}
    # Provide (or decline) those, and the CV builds — skipping only what was declined.
    st3 = s.submit("I volunteer at a food bank and I enjoy chess and running.")
    assert st3["phase"] == "review"
    assert s.assembled.compile.pages == 1


def test_gate_is_conversational_a_couple_at_a_time_and_never_reasks(template_source, fake_llm, workdir):
    """The gate asks a couple of fields at a time, acknowledges what was answered,
    and never re-asks a field the person already filled — while still collecting
    every template-critical field."""
    s = _session(template_source, fake_llm, workdir)
    s.start()
    st1 = s.submit("I'm Dana Lee, dana@example.com. Analyst at BlackOrigin since Feb 2020. "
                   "Engineer at Stanbic Bank since Jan 2018.")
    ask1 = " ".join(st1["messages"])
    assert st1["phase"] == "chatting"
    assert ask1.lower().count("location") <= 2 and "BlackOrigin" in ask1   # a couple, not all

    st2 = s.submit("BlackOrigin is in Accra, Ghana")
    ask2 = " ".join(st2["messages"])
    assert "BlackOrigin" not in ask2                     # never re-asks the answered one
    assert "Stanbic Bank" in ask2                        # keeps collecting the rest
    assert any(w in ask2.lower() for w in ("got", "thanks", "great", "perfect"))   # acknowledges


# -- reformatting ------------------------------------------------------- #

def test_dates_and_degree_reformatted_to_template_style():
    p = normalize_profile({
        "identity": {"name": "A"},
        "education": [{"school": "X", "degree": "M.S. in Statistics", "date": "graduated Dec 2025"}],
        "experience": [{"org": "Y", "location": "NYC",
                        "roles": [{"title": "Eng", "dates": "feb 2023 - present", "bullets": []}]}],
    })
    assert p["education"][0]["date"] == "Dec 2025"               # abbreviated, prefix stripped
    assert p["education"][0]["degree"] == "Master of Science in Statistics"
    assert p["experience"][0]["roles"][0]["dates"] == "Feb 2023 - Present"


def test_ongoing_role_dates_render_as_range_to_present():
    """An ongoing role given as 'since X' / 'from X' (no end) must render as a
    range ending in 'Present' — the template never shows a lone start date for a
    current job (CLAUDE.md §8). Education graduation dates stay single."""
    from tailoring.conform import format_dates
    assert format_dates("since Feb 2021") == "Feb 2021 - Present"
    assert format_dates("from June 2022") == "June 2022 - Present"    # short month kept full
    assert format_dates("July 2018 to July 2021") == "July 2018 - July 2021"
    assert format_dates("Feb 2021 - present") == "Feb 2021 - Present"
    assert format_dates("graduated May 2018") == "May 2018"          # education: single
    assert format_dates("January 2021 - September 2024") == "Jan 2021 - Sept 2024"
    assert format_dates("still there, started March 2020") == "March 2020 - Present"
    # A provably impossible range (start after end) is a mis-attributed date -> rejected,
    # so nothing nonsensical renders and the gate re-asks for the real dates.
    assert format_dates("December 2025 - February 2020") == ""
    assert format_dates("2020 - 2019") == ""
    assert format_dates("Dec 2020 - Jan 2020") == ""
    assert format_dates("Feb 2020 - Feb 2020") == "Feb 2020 - Feb 2020"   # equal is fine


def test_education_program_regular_weight_school_bold(template_source):
    """Many degrees shouldn't stack into a wall of bold: the SCHOOL stays bold small
    caps (the anchor), the PROGRAM is regular weight."""
    profile = {"identity": {"name": "A"}, "education": [
        {"school": "MIT", "degree": "Master of Science in CS", "date": "Dec 2025",
         "location": "Cambridge, MA"}]}
    tex = render_cv(extract_preamble(template_source), profile)
    assert "\\textbf{\\textsc{MIT}}" in tex                 # school bold small caps
    assert "\\textbf{Master of Science in CS}" not in tex   # program NOT bold
    assert "Master of Science in CS" in tex                 # but present


def test_skills_never_stretched(template_source):
    """Skills lines are NEVER inter-word stretched, full or short. Natural word packing
    fills a line where the real terms allow; whatever is left stays ragged (the user
    rejected the makebox[s] justify as looking unnatural)."""
    from tailoring.textwidth import text_width_pt, TEXTWIDTH_PT
    terms = ["SQL", "Python", "pandas", "numpy", "Excel", "Git", "dbt", "Airflow", "R",
             "Bash", "Scala", "Java", "Golang", "Rust", "Kotlin", "Swift", "Perl", "Ruby"]
    long_val = ""
    for t in terms:
        cand = (long_val + ", " + t) if long_val else t
        if (text_width_pt("Tools: ", bold=True) + text_width_pt(cand)) / TEXTWIDTH_PT > 0.98:
            break
        long_val = cand
    assert (text_width_pt("Tools: ", bold=True) + text_width_pt(long_val)) / TEXTWIDTH_PT >= 0.92
    tex = render_cv(extract_preamble(template_source), {"identity": {"name": "A"},
                                                        "skills": {"Tools": long_val}})
    assert "makebox[\\linewidth][s]" not in tex             # even a near-full line: no stretch
    assert "\\noindent \\textbf{Tools:}" in tex             # plain ragged line
    tex2 = render_cv(extract_preamble(template_source), {"identity": {"name": "A"},
                                                         "skills": {"Tools": "one, two"}})
    assert "makebox[\\linewidth][s]" not in tex2            # short -> ragged, no stretch


def test_no_ai_dashes_in_generated_prose(template_source):
    profile = {
        "identity": {"name": "Ana Cruz"},
        "experience": [{"org": "Beacon", "location": "Chicago, IL",
                        "roles": [{"title": "Analyst", "dates": "2020",
                                   "bullets": ["Built a model — improved accuracy by 30%"]}]}],
        "skills": {"Computing": "Python — SQL — AWS"},
    }
    tex = render_cv(extract_preamble(template_source), profile)
    assert "—" not in tex and "–" not in tex                    # em/en dashes scrubbed
    assert "Built a model, improved accuracy by 30" in tex      # replaced with plain phrasing
    assert "Python, SQL, AWS" in tex


def test_courses_are_capped_to_fit_two_lines():
    """The template's Courses line is at most 2 lines; long course lists must drop
    WHOLE trailing (least-relevant) courses rather than overflow to a 3rd line."""
    from tailoring.assembler import normalize_profile
    from tailoring.conform import fit_courses
    long = ("Business Analytics Tools, Managerial Economics, Financial Accounting for "
            "Managerial Decision Making, Financial Management, Business Innovation & Design, "
            "Human Capital Strategy & Science, Leading Effective Ethical Organizations, "
            "Marketing Management")   # ~8 long courses -> 3 lines raw
    fitted = fit_courses(long)
    assert len(fitted) <= 180                       # within a ~2-line budget
    assert fitted.startswith("Business Analytics Tools")   # keeps the most-relevant (first)
    assert "Marketing Management" not in fitted      # dropped a whole trailing course
    assert not fitted.endswith(",") and ", ," not in fitted   # only whole courses kept
    # And it flows through normalize/conform onto the profile.
    p = normalize_profile({"education": [{"school": "X", "degree": "B.S.", "date": "2020",
                                          "courses": long}]})
    assert p["education"][0]["courses"] == fitted


def test_education_renders_full_school_program_location_date(template_source):
    profile = {
        "identity": {"name": "Ana Cruz"},
        "education": [{"school": "University of Chicago",
                       "degree": "M.S. in Financial Mathematics", "location": "Chicago, IL",
                       "date": "Dec 2022", "courses": "Option Pricing, Risk Management"}],
    }
    tex = render_cv(extract_preamble(template_source), profile)
    assert "University of Chicago" in tex
    assert "Master of Science in Financial Mathematics" in tex   # full program, not 'M.S.'
    assert "Chicago, IL" in tex
    assert "Dec 2022" in tex                                     # abbreviated graduation date


def test_essentials_dates_conformed_to_template_style_in_every_view(template_source, fake_llm, workdir):
    """Bug fix: date/degree reformatting must reach the education block and the
    intake preview, not just the compiled PDF — in the template's abbreviated form."""
    s = _session(template_source, fake_llm, workdir)
    s.start()
    s.essentials["education"] = [{"school": "MIT", "degree": "B.S. in CS",
                                  "date": "graduated Dec 2025"}]
    s.essentials["experience"] = [{"org": "Acme", "title": "Eng",
                                   "dates": "September 2018 – May 2021", "location": "NYC"}]
    s._conform_essentials()
    assert s.essentials["education"][0]["date"] == "Dec 2025"          # not "graduated Dec 2025"
    assert s.essentials["education"][0]["degree"] == "Bachelor of Science in CS"
    assert s.essentials["experience"][0]["dates"] == "Sept 2018 - May 2021"
    # the live intake preview (not just the PDF) shows the formatted dates
    assert s._preview_from_essentials()["education"][0]["date"] == "Dec 2025"


@requires_latex
def test_explicit_override_skips_optional_and_builds(template_source, fake_llm, workdir):
    """Explicit opt-out builds at once and skips the OPTIONAL extras (address,
    GitHub, blog, courses) — as long as the template-critical fields are present."""
    s = _session(template_source, fake_llm, workdir)
    s.start()
    # name + a role WITH its critical fields (location + dates), nothing optional.
    st = s.submit("I'm Dana Lee, dana@example.com. I work as an Engineer at Globex "
                  "in Boston, MA since 2020.")
    assert st["phase"] == "chatting"                 # gate still asks for the optional extras

    st2 = s.submit("just continue with what you already know about me")
    assert st2["phase"] == "review"                  # override -> builds now
    assert s.assembled.compile.pages == 1
    assert not any("github" in m.lower() or "address" in m.lower() for m in st2["messages"])


@requires_latex
def test_override_still_requires_template_critical_location(template_source, fake_llm, workdir):
    """Even under the override, a missing experience LOCATION (template-critical)
    must be asked for — then, and only then, it builds; optional fields stay skipped."""
    s = _session(template_source, fake_llm, workdir)
    s.start()
    # roles have dates but NO location.
    s.submit("I'm Rae Kim, rae@example.com. Analyst at BlackOrigin since Feb 2020. "
             "Engineer at Stanbic Bank since Jan 2018.")
    st = s.submit("continue with what you already know and build it")
    assert st["phase"] == "chatting"                 # does NOT ship without locations
    ask = " ".join(st["messages"]).lower()
    assert "location" in ask and "blackorigin" in ask
    assert "github" not in ask and "address" not in ask   # only the critical field, not extras

    st2 = s.submit("BlackOrigin is in Accra, Ghana and Stanbic Bank is in Lagos, Nigeria")
    assert st2["phase"] == "review"                  # now it builds
    locs = [e.get("location") for e in s.profile["experience"]]
    # every experience entry has a location, formatted "City, XX" (2-letter code)
    assert all(x.strip() for x in locs)
    assert "Accra, GH" in locs and "Lagos, NG" in locs


def test_job_titles_are_cleaned_not_conversational_fragments(template_source, fake_llm, workdir):
    """A run-on answer must never put 'Before that I was an Analyst' on the CV — the
    title is cleaned to the real role, never inferred from chatter."""
    s = _session(template_source, fake_llm, workdir)
    s.start()
    s.essentials["override"] = True   # build straight through for the assertion
    s.submit("I'm Kofi Mensah, kofi@example.com. Product Manager at BlackOrigin in Accra, Ghana "
             "since Feb 2021. Before that I was an Analyst at Stanbic Bank in Accra, Ghana since "
             "Jan 2018, and a Developer at Viva in Lagos, Nigeria since 2015.")
    titles = [r["title"] for e in s.profile["experience"] for r in (e.get("roles") or [e])]
    assert not any("before that" in t.lower() or t.lower().startswith(("a ", "an ", "i ")) for t in titles)
    assert "Analyst" in titles and "Developer" in titles


def test_locations_formatted_city_comma_two_letter_code():
    from tailoring.assembler import normalize_profile
    p = normalize_profile({
        "experience": [{"org": "A", "location": "Accra, Ghana",
                        "roles": [{"title": "PM", "dates": "2020", "bullets": []}]},
                       {"org": "B", "location": "Chicago, Illinois",
                        "roles": [{"title": "Eng", "dates": "2019", "bullets": []}]}],
        "education": [{"school": "X", "degree": "B.S.", "date": "2015", "location": "Lagos, Nigeria"}],
    })
    assert p["experience"][0]["location"] == "Accra, GH"      # non-US -> country code
    assert p["experience"][1]["location"] == "Chicago, IL"    # US state name -> USPS code
    assert p["education"][0]["location"] == "Lagos, NG"


def test_project_link_is_a_minimal_marker_not_a_blue_title(template_source):
    """A project link attaches to a SMALL inline marker after the (black) title — never
    wraps the whole heading in \\href (no 'blue takeover'; matches the template)."""
    profile = {
        "identity": {"name": "Ana Cruz"},
        "projects": [{"org": "Credit Risk Model", "location": "Chicago, IL", "dates": "2023",
                      "link": "https://github.com/ana/credit-risk", "bullets": ["Modeled defaults"]}],
    }
    tex = render_cv(extract_preamble(template_source), profile)
    # The title stays black (rendered in \textbf{\textsc{...}}), NOT wrapped in a link.
    assert "\\textbf{\\textsc{Credit Risk Model}}" in tex
    assert "\\href{https://github.com/ana/credit-risk}{Credit Risk Model}" not in tex
    # The link is present as a minimal marker (the arrow), keeping blue minimal.
    assert "\\href{https://github.com/ana/credit-risk}{$\\nearrow$}" in tex
