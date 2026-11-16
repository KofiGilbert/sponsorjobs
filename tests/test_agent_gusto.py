"""The first real run on a live Greenhouse posting (Gusto, Lead CX Business Intelligence Analyst,
2026-11-16) and what it taught the apply agent.

The run uploaded the RESUME into the optional "Cover Letter" box, ticked EVERY box of "How did you
hear about this opportunity?", and stopped to ask about questions the person's own material
answers: where they live, whether they ever worked at Gusto, and seven voluntary
self-identification questions. The fields below are what the fill engine read from that page.
No model: the planner, the brain's guards and the routes run on the test double.
"""
from __future__ import annotations

import importlib
import json

import pytest

from autoapply import eeo
from autoapply.answers import AnswerLibrary
from autoapply.asks import known_answer
from autoapply.brain import step
from autoapply.fields import FormField, resolve_plan
from autoapply.planner import (KnownContext, generic_source, plan_known, residence_option,
                               worked_here_question)

HEARD = "How did you hear about this opportunity? (select all that apply) *"
SOURCES = ["LinkedIn", "Glassdoor", "Indeed", "Facebook", "Built In Colorado", "News Article", "Conference or Meetup",
           "Gusto Blog", "Gusto Employee", "Gusto Website", "Billboard/Outdoor Ads"]
RESIDE = "Do you currently reside in one of the following locations?"
RESIDE_OPTS = ["Yes - Denver, CO", "Yes - New York, NY", "No - None of the above"]
WORKED = ("Have you ever, or are you currently working at Gusto or Symmetry in any capacity "
          "(ie: employee or contingent worker)?")
DECLINE = "I don't wish to answer"
# The engine's labels: "select one" is cut out of "(please select one)".
GUSTO_EEO = [
    ("gender", "How would you describe your gender identity (please )?",
     ["Female", "Male", "Non-binary / third gender (Inclusive of bigender, agender)", "I prefer to self-describe", DECLINE]),
    ("trans", "I identify as transgender (please ):", ["Yes", "No", "I prefer to self-describe", DECLINE]),
    ("orient", "I identify my sexual orientation as (please ):",
     ["Asexual", "Bisexual, pansexual and/or queer", "Gay and/or lesbian", "Heterosexual", "I prefer to self-describe", DECLINE]),
    ("race", "I identify my race/ethnicity as (mark all that apply):",
     ["Alaskan Native / American Indian / Indigenous American / Native American",
      "Black / of African or African-American descent", "East Asian (inclusive of Chinese, Japanese, Korean)",
      "Latino/a/x/Hispanic (inclusive of the United States of America, South America)", "Middle Eastern",
      "White", "I prefer to self-describe", DECLINE]),
    ("vet", "Veteran Status (please ):", ["Yes, I am a veteran", "No, I am not a veteran", DECLINE]),
    ("dis", "I have a disability (please ):", ["Yes", "No", "I prefer to self-describe", DECLINE]),
    ("first", "I identify as a first-generation professional (please ):", ["Yes", "No", DECLINE]),
]


def _gusto_fields(required_letter=False):
    f = [FormField("resume", "Resume/CV*", "file", required=True),
         FormField("letter", "Cover Letter", "file", required=required_letter),
         FormField("reside", RESIDE, "select", RESIDE_OPTS, required=True),
         FormField("auth", "Are you legally authorized to work in the country where you are applying?", "select",
                   ["Yes", "No"], required=True),
         FormField("visa", "Will you now or in the future require visa sponsorship for employment?", "select",
                   ["Yes", "No"], required=True),
         FormField("worked", WORKED, "select", ["Yes", "No"], required=True)]
    f += [FormField(f"h{i}", HEARD + ": " + o, "checkbox", required=True) for i, o in enumerate(SOURCES)]
    f += [FormField(ref, label, "select", opts, required=True) for ref, label, opts in GUSTO_EEO]
    return f


WORK_AUTH = {"countries": {"US": {"status": "f1_opt", "authorized": "Yes", "sponsor_now": "No",
                                  "sponsor_future": "Yes", "auth_end": ""}}}


@pytest.fixture()
def lib(tmp_path):
    lib = AnswerLibrary(tmp_path / "a.db")
    # The owner's saved answers (Memory > Your answers and Self-identification).
    for q, a in (("How did you hear about us?", "Job board"), ("Gender", "Male"), ("Race", "Black or African American"),
                 ("Are you Hispanic or Latino?", "Not Hispanic or Latino"),
                 ("Veteran status", "I am not a protected veteran"), ("Disability status", "No")):
        lib.save(q, a, "you")
    yield lib
    lib.close()


def _ctx(lib, **kw):
    base = dict(work_auth=WORK_AUTH, library=lib, applicant={"city": "Chicago", "state": "IL"},
                resume_path="/r/Kofi Gilbert Resume.pdf", company="Gusto Inc", per_company=True,
                worked_here=("No", "derived"))
    base.update(kw)
    return KnownContext(**base)


def test_the_gusto_form_is_answered_from_the_persons_material_and_nothing_is_left(lib):
    res = plan_known(_gusto_fields(), _ctx(lib))
    by = {a.ref: (a.op, a.value, a.origin) for a in res.actions}
    assert res.left == []                                       # nothing for the person to answer
    assert by["resume"] == ("upload", "", "resume")
    assert "letter" not in by                                   # optional, no letter yet: left empty, never asked
    assert by["reside"] == ("select", "No - None of the above", "profile")
    assert by["auth"][1] == "Yes" and by["visa"][1] == "Yes"   # F-1 OPT: authorized, sponsorship later
    assert by["worked"][1:] == ("No", "derived")
    # the person's saved self-identification answers, on Greenhouse's own wording
    assert by["gender"][1:] == ("Male", "library")
    assert by["race"][1:] == ("Black / of African or African-American descent", "library")
    assert by["dis"][1:] == ("No", "library")
    # nothing saved fits: the page's decline, never a question
    for ref in ("trans", "orient", "vet", "first"):
        assert by[ref][1:] == (DECLINE, "eeo_decline"), ref


def test_a_checkbox_group_ticks_exactly_one_box_never_all(lib):
    """Every box of "How did you hear" was ticked from the saved answer's Yes/No polarity. Now the
    group gets its ONE closest choice: a job board the list names (no "Job board" or "Other")."""
    res = plan_known(_gusto_fields(), _ctx(lib))
    ticked = [a.ref for a in res.actions if a.ref.startswith("h")]
    assert ticked == [f"h{SOURCES.index('Indeed')}"]
    assert all(a.value == "Yes" and a.op == "check" for a in res.actions if a.ref.startswith("h"))
    # a generic choice still wins over a named board, and "Other" over a named board too
    assert generic_source("Job board", ["LinkedIn", "Indeed", "Online job board"]) == "Online job board"
    assert generic_source("Job board", ["LinkedIn", "Indeed", "Other"]) == "Other"
    assert generic_source("Job board", ["Built In Colorado", "Gusto Blog"]) is None
    assert generic_source("A friend told me", ["LinkedIn", "Indeed"]) is None


def test_a_group_nothing_answers_is_one_open_question_and_no_box_is_ticked(tmp_path):
    with AnswerLibrary(tmp_path / "e.db") as empty:
        res = plan_known(_gusto_fields(), _ctx(empty))
        assert not [a for a in res.actions if a.ref.startswith("h")]
        assert [f.ref for f in res.left if f.ref.startswith("h")] == ["h0"]   # one line for the group


def test_self_identification_topics_on_sentence_wordings():
    assert eeo.topic_key("How would you describe your gender identity (please select one)?") == "@gender"
    assert eeo.topic_key("I identify my race/ethnicity as (mark all that apply):") == "@race"
    assert eeo.topic_key("Veteran Status (please select one):") == "@veteran"
    assert eeo.topic_key("I have a disability (please select one):") == "@disability"
    assert eeo.topic_key("Are you of Hispanic or Latino origin?") == "@hispanic_latino"
    assert eeo.topic_key("I identify as transgender (please select one):").startswith("@") is False
    assert eeo.topic_key("Gender pronouns") != "@gender"
    assert eeo.topic_key("Do you need an accommodation for a disability?") != "@disability"


def test_the_shells_known_only_check_declines_and_a_plain_ask_still_asks(lib):
    q, opts = GUSTO_EEO[1][1], GUSTO_EEO[1][2]
    assert known_answer(q, opts, library=lib, eeo_fallback=True) == (DECLINE, "eeo_decline")
    assert known_answer(q, opts, library=lib) is None
    assert known_answer(GUSTO_EEO[0][1], GUSTO_EEO[0][2], library=lib) == ("Male", "library")
    # "working at Gusto" is the worked-here question for this company, even with the label
    # mapper's unconfirmed mapping waiting
    assert known_answer(WORKED, ["Yes", "No"], library=lib, per_company=True, worked_here=("No", "derived"),
                        confirm={WORKED: "@previously_worked"}, company="Gusto Inc") == ("No", "derived")


def test_residence_from_the_profile_city():
    assert residence_option(RESIDE, RESIDE_OPTS, {"city": "Chicago", "state": "IL"}) == "No - None of the above"
    assert residence_option(RESIDE, RESIDE_OPTS, {"city": "Denver", "state": "CO"}) == "Yes - Denver, CO"
    assert residence_option(RESIDE, RESIDE_OPTS, {"city": "Boulder", "state": "CO"}) is None   # their state: theirs
    assert residence_option(RESIDE, RESIDE_OPTS, {}) is None
    assert residence_option("Where do you live?", RESIDE_OPTS, {"city": "Chicago"}) is None


def test_worked_here_names_the_company_never_someone_else():
    assert worked_here_question(WORKED, "Gusto Inc")
    assert worked_here_question("Have you previously worked for Acme Corporation?", "Acme Corporation")
    assert not worked_here_question(WORKED, "Stripe")
    assert not worked_here_question("Do you have a relative working at Gusto?", "Gusto")
    assert not worked_here_question("Do you know anyone that currently works at Gusto?", "Gusto")


def test_the_cover_letter_box_gets_the_letter_never_the_resume(lib):
    ctx = _ctx(lib, cover_letter_path="/r/Kofi Gilbert Cover Letter.pdf")
    res = plan_known(_gusto_fields(), ctx)
    ops = resolve_plan(res.actions, {}, resume_path=ctx.resume_path, letter_path=ctx.cover_letter_path)
    files = {o.ref: o.path for o in ops if o.op == "upload"}
    assert files == {"resume": "/r/Kofi Gilbert Resume.pdf", "letter": "/r/Kofi Gilbert Cover Letter.pdf"}
    # a REQUIRED cover letter box with no letter is the person's; an optional one stays empty
    res = plan_known(_gusto_fields(required_letter=True), _ctx(lib))
    assert [f.ref for f in res.left] == ["letter"]


# --- the brain's upload, by the box it names --------------------------------------------------------
GH_VIEW = """URL: https://job-boards.greenhouse.io/gusto/jobs/8263050
[1]<file label="Resume/CV" required>
[2]<button>Attach</button>
[3]<file label="Cover Letter">
[4]<button>Attach</button>
[5]<button>Submit application</button>"""


class _Scripted:
    def __init__(self, reply):
        self.reply = reply

    def agent_step(self, system, user):
        return self.reply


def _brain(actions, **kw):
    llm = _Scripted(json.dumps({"memory": "", "evaluation": "", "actions": actions, "done": None}))
    req = {"step": 2, "max_steps": 40, "url": "https://job-boards.greenhouse.io/gusto/jobs/8263050",
           "lane": "assisted", "view": GH_VIEW, "history": [], "job": {"role": "Analyst", "company": "Gusto"}}
    return step(req, llm, {"email": "k@example.com"}, None, placeholders=["email"], **kw)


def test_the_brains_upload_on_the_cover_letter_box_never_carries_the_resume():
    out = _brain([{"op": "upload", "index": 3}], resume_path="/r/Resume.pdf")
    assert out["actions"] == [] and any("cover letter" in n for n in out["notes"])
    out = _brain([{"op": "upload", "index": 3}], resume_path="/r/Resume.pdf", letter_path="/r/Cover Letter.pdf")
    assert out["actions"][0]["path"] == "/r/Cover Letter.pdf" and out["actions"][0]["file"] == "cover_letter"
    out = _brain([{"op": "upload", "index": 1}], resume_path="/r/Resume.pdf", letter_path="/r/Cover Letter.pdf")
    assert out["actions"][0]["path"] == "/r/Resume.pdf" and out["actions"][0]["file"] == "resume"


# --- the routes: write the letter, then the plan attaches it ------------------------------------------
@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._memory().save("default", {"identity": {"name": "Kofi Gilbert", "email": "kofi@example.com",
                                              "phone": "555-100-2000"}, "experience": [{"org": "X"}]}, {}, [])
    from ui.records import CVRecords
    recs = CVRecords(A.DB_PATH)
    rid = recs.add("Analyst", "Gusto Inc", 70, "", data={"jd_text": "We need SQL and Tableau."})
    recs.close()
    A.WORKDIR.mkdir(parents=True, exist_ok=True)
    (A.WORKDIR / f"cv-{rid}.pdf").write_bytes(b"%PDF-1.4 resume")

    def fake_pdf(r, text):                         # no LaTeX in the test: a stand-in PDF
        (A.WORKDIR / f"cover-{r}.pdf").write_bytes(b"%PDF-1.4 letter")
        return True
    monkeypatch.setattr(A, "_render_cover_pdf", fake_pdf)
    return A, A.app.test_client(), rid


_PAGE = [{"ref": "t5", "label": "Resume/CV*", "type": "file", "required": True},
         {"ref": "t6", "label": "Cover Letter", "type": "file"}]


def test_the_plan_asks_for_a_letter_the_route_writes_it_and_the_next_plan_attaches_it(app):
    A, c, rid = app
    url = "https://job-boards.greenhouse.io/gusto/jobs/8263050"
    d = c.post("/api/autoapply/plan", json={"url": url, "record_id": rid, "fields": _PAGE}).get_json()
    assert d["wants_letter"] is True
    assert [(o["ref"], o["path"].rsplit("/", 1)[-1]) for o in d["ops"] if o["op"] == "upload"] == \
        [("t5", "Kofi Gilbert Resume.pdf")]                     # never the resume in the letter box
    assert not [x for x in d["left"] if x["ref"] == "t6"]       # optional: not a question
    r = c.post("/api/autoapply/agent/cover-letter", json={"record_id": rid}).get_json()
    assert r["ok"] and r["drafted"] and r["pdf"] and r["words"] > 20
    d = c.post("/api/autoapply/plan", json={"url": url, "record_id": rid, "fields": _PAGE}).get_json()
    assert d["wants_letter"] is False
    files = {o["ref"]: o["path"].rsplit("/", 1)[-1] for o in d["ops"] if o["op"] == "upload"}
    assert files == {"t5": "Kofi Gilbert Resume.pdf", "t6": "Kofi Gilbert Cover Letter.pdf"}
    assert [o["from"] for o in d["ops"] if o["ref"] == "t6"] == ["letter"]
    import os                                     # both copies exist side by side (one per folder)
    assert all(os.path.exists(o["path"]) for o in d["ops"] if o["op"] == "upload")
    # a second call keeps the saved letter
    r = c.post("/api/autoapply/agent/cover-letter", json={"record_id": rid}).get_json()
    assert r["ok"] and not r["drafted"]


def test_nothing_unrendered_is_ever_typed_picked_or_attached():
    """The Ready to send check said "{{city}}" and "{{full_name}}" were on the page: they were the
    critic's own redacted view (values hidden from the model), never the page. Belt and braces
    anyway: an action that still holds a placeholder after resolution is dropped."""
    out = _brain([{"op": "type", "index": 1, "text": "{{nope}}"},
                  {"op": "select", "index": 1, "option": "{{city}}"},
                  {"op": "type", "index": 1, "text": "Lives in {{city}}"}], resume_path="/r/Resume.pdf")
    assert out["actions"] == []
    from autoapply.fields import FillAction
    ops = resolve_plan([FillAction("x", "select", value="{{city}}"), FillAction("y", "select", value="Yes")], {})
    assert [o.ref for o in ops] == ["y"]
    from autoapply.brain import CRITIC_PROMPT
    assert "{{city}}" in CRITIC_PROMPT and "never a template left unrendered" in CRITIC_PROMPT
