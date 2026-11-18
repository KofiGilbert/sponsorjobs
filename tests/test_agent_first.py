"""The agent decides (2026-11-18, docs/apply-agent.md section 29).

The brain gets the person's own material and may answer a question itself under the answer
protocol (source, evidence, class); code checks every such answer; a short list of hard limits
stays in code; the fast-path rules are advice.

* the classifier (autoapply/qclass.py): what is legal, and "when unsure, it is legal";
* the material block (autoapply/material.py): what the brain is shown, what never gets there, and
  how many tokens it adds;
* the validator (autoapply/protocol.py): a cited fact that exists passes, one that does not is
  refused, a legal answer needs the person's saved answer;
* the brain's step (autoapply/brain.py): a refusal comes back as an observation with its reason,
  a hard limit names itself, an accepted answer keeps its proof;
* the proof the owner asked for: with the fast-path rule switched off, the brain answers
  "highest level of education" and "have you worked here before" from the block, and the answer
  passes the check;
* the step route's advice lines, the application's record, and the shell's part under node
  (tests/js/test_agent_first.js).

Everyone here is made up.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from autoapply import brain, commonsense, material, protocol, qclass
from autoapply.fields import FormField
from autoapply.planner import KnownContext, plan_known
from llm.base import FakeLLM

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}
TODAY = dt.date(2026, 11, 18)
COMPANY = "Brightwater Freight"

PROFILE = {
    "identity": {"name": "Dana Whitfield"},
    "experience": [
        {"org": "Northwind Analytics", "location": "Chicago, IL",
         "roles": [{"title": "Senior Data Analyst", "dates": "March 2021 - Present",
                    "bullets": ["Built weekly revenue dashboards in SQL and Tableau for the sales team."]}]},
        {"org": "Larkspur Labs", "location": "Madison, WI",
         "roles": [{"title": "Data Analyst", "dates": "June 2018 - February 2021",
                    "bullets": ["Cleaned survey data in Python and reported findings to product managers."]}]},
        {"org": "Harlow County Library", "location": "Madison, WI",
         "roles": [{"title": "Research Assistant", "dates": "September 2016 - May 2018", "bullets": []}]},
    ],
    "education": [
        {"school": "Harlow State University", "degree": "Master of Science in Business Analytics", "date": "2016 - 2018"},
        {"school": "Pinecrest College", "degree": "Bachelor of Arts in Economics", "date": "2012 - 2016"},
    ],
    "skills": {"Tools": ["SQL", "Python", "Tableau", "Excel", "dbt", "Looker"],
               "Methods": ["A/B testing", "Forecasting", "Data modeling"]},
    "languages": ["English", "Spanish (conversational)"],
}
APPLICANT = {"first_name": "Dana", "last_name": "Whitfield", "full_name": "Dana Whitfield", "email": "dana@example.com",
             "phone": "+1 555 010 0199", "city": "Chicago", "state": "IL", "postal_code": "60601", "country": "United States",
             "linkedin": "https://www.linkedin.com/in/dana-whitfield-example", "current_company": "Northwind Analytics",
             "current_title": "Senior Data Analyst"}


class Lib:
    """An answer library with a few saved rows, one of each kind that must never reach the model."""

    ROWS = [
        {"key": "@heard_about", "question": "How did you hear about us?", "answer": "Job board", "source": "user"},
        {"key": "@notice_period", "question": "What is your notice period?", "answer": "Two weeks", "source": "user"},
        {"key": "@gender", "question": "Gender", "answer": "Zyxwv Selfid", "source": "user"},
        {"key": "@citizenship", "question": "Country of citizenship", "answer": "Freedonia", "source": "user"},
        {"key": "why us", "question": "Why do you want to work here?", "answer": "Drafted words", "source": "drafted"},
    ]

    def list(self):
        return [dict(r) for r in self.ROWS]

    def lookup(self, q):
        return None

    def best(self, key):
        return None


def _material(company=COMPANY, profile=PROFILE, lib=None):
    return material.build(APPLICANT, profile, lib if lib is not None else Lib(), company=company, today=TODAY)


# --------------------------------------------------------------------------- the classifier
@pytest.mark.parametrize("q", [
    "Are you legally authorized to work in the United States?",
    "Will you now or in the future require visa sponsorship?",
    "Country of citizenship",
    "Have you ever been convicted of a felony?",
    "Do you consent to a background check?",
    "Do you hold an active security clearance?",
    "Are you bound by a non-compete agreement with a current or former employer?",
    "Do you have a disability?", "Veteran status", "Race / Ethnicity", "Gender", "What are your pronouns?",
    "Date of birth", "Are you at least 18 years of age?", "Social Security Number",
    "Are any of your relatives employed by Brightwater Freight?",
    "Do you have a conflict of interest with our company?",
    "List any aliases or other name(s) you have used or currently use or indicate N/A.",
    "What are your salary expectations?", "When can you start?", "Earliest start date",
    "Reference 1: phone number", "Signature", "I certify that the information provided is true and complete",
    "Do you agree to the terms and conditions?", "Would you like to receive text messages about job alerts?",
])
def test_legal_and_attestation_questions_are_legal(q):
    assert qclass.is_legal(q, company=COMPANY) and qclass.question_class(q, company=COMPANY) == qclass.LEGAL


@pytest.mark.parametrize("q, cls", [
    ("How did you hear about us?", qclass.COMMON),
    ("Phone Device Type", qclass.COMMON),
    ("If yes, please explain", qclass.COMMON),
    ("What is the highest level of education you have completed?", qclass.FACTUAL),
    ("Current employer", qclass.FACTUAL),
    ("How many years of experience do you have with SQL?", qclass.FACTUAL),
    ("Which city do you currently live in?", qclass.FACTUAL),
    ("Are you comfortable working weekends?", qclass.PREFERENCE),
    ("Are you open to a hybrid schedule?", qclass.PREFERENCE),
    ("Favourite spreadsheet shortcut", ""),
])
def test_the_other_classes(q, cls):
    assert not qclass.is_legal(q, company=COMPANY)
    assert qclass.question_class(q, company=COMPANY) == cls


def test_a_work_history_date_is_not_the_availability_start_date():
    assert qclass.question_class("Start date", company=COMPANY) == qclass.LEGAL          # when unsure, legal
    assert qclass.question_class("What is your earliest start date?", company=COMPANY) == qclass.LEGAL
    assert qclass.question_class("Work Experience 1: Start Date", company=COMPANY) == qclass.FACTUAL
    m = _material()
    v = protocol.check({"op": "type", "index": 4, "text": "03/2021", "source": "resume", "evidence": "job1: 2021-03",
                        "class": "factual"}, "Work Experience 1: Start Date", [], m)
    assert v.ok, v.reason
    v = protocol.check({"op": "type", "index": 4, "text": "03/2015", "source": "resume", "evidence": "job1", "class": "factual"},
                       "Work Experience 1: Start Date", [], m)
    assert not v.ok                                                                       # a year the material does not hold


def test_relocation_is_the_work_authorization_profiles_question():
    """workauth.classify owns it (the saved work profile answers it), so it stays there: legal."""
    assert qclass.question_class("Are you willing to relocate?", company=COMPANY) == qclass.LEGAL


def test_worked_here_is_legal_only_when_the_employer_is_in_the_work_history():
    q = "Have you worked for Brightwater Freight before?"
    assert qclass.question_class(q, company=COMPANY, employers=["Northwind Analytics"]) == qclass.FACTUAL
    q2 = "Have you ever worked at Northwind Analytics?"
    assert qclass.question_class(q2, company="Northwind Analytics", employers=["Northwind Analytics", "Larkspur Labs"]) == qclass.LEGAL
    # about someone else: the relatives rule, legal either way
    assert qclass.is_legal("Are you related to a current Brightwater Freight employee?", company=COMPANY)


def test_a_self_identification_list_is_legal_by_its_choices():
    assert qclass.is_legal("Please check one of the boxes below:",
                           ["Yes, I have a disability, or have had one in the past", "No, I do not have a disability",
                            "I do not want to answer"])


# --------------------------------------------------------------------------- the material block
def test_the_block_holds_the_resume_facts_and_never_a_legal_or_contact_value():
    m = _material()
    b = m.block
    assert b.startswith("<person>") and b.endswith("</person>")
    for want in ("Northwind Analytics", "Senior Data Analyst", "(current job)", "Larkspur Labs", "Harlow State University",
                 "Master of Science in Business Analytics", "2018", "Chicago", "SQL", "Spanish", "{{linkedin}}",
                 "about 10 years", '"How did you hear about us?" = "Job board"'):
        assert want in b, want
    # never: a self-identification or citizenship answer, a drafted one, a saved answer to any
    # other legal-class question (the notice period: it fills its own question as a placeholder),
    # a contact detail or the link's own address
    for never in ("Zyxwv", "Freedonia", "Drafted words", "Two weeks", "dana@example.com", "555 010", "linkedin.com", "Whitfield"):
        assert never not in b, never
    assert m.employers[:2] == ["Northwind Analytics", "Larkspur Labs"] and m.company == COMPANY
    assert {f.id for f in m.facts} >= {"loc", "job1", "job2", "edu1", "edu2", "skills", "years", "links", "languages", "ans1"}


def test_the_block_is_the_same_text_on_every_step_and_small():
    """Stable text is what lets it sit in the cached part of the prompt; the size is what it adds
    to a step. With this realistic made-up profile: under 300 tokens for the block, and the two
    cached blocks together stay near 3,200."""
    a, b = _material().block, _material().block
    assert a == b
    blocks = brain.system_blocks(_material())
    assert blocks[0] == brain.SYSTEM_PROMPT and blocks[1].endswith(a) and len(blocks) == 2
    assert brain.system_blocks(None) == [brain.SYSTEM_PROMPT]            # no open question: rules only
    assert 150 < material.tokens(blocks[1]) < 300, material.tokens(blocks[1])
    assert material.tokens(brain.SYSTEM_PROMPT) < 3000, material.tokens(brain.SYSTEM_PROMPT)


def test_no_profile_means_no_block():
    m = material.build({}, {}, None, company=COMPANY)
    assert not m and m.block == "" and m.facts == []


# --------------------------------------------------------------------------- the validator
EDU_Q = "What is the highest level of education you have completed?"
WORKED_Q = "Have you worked for Brightwater Freight before?"


def _act(op="select", value="Master's Degree", **kw):
    a = {"op": op, "index": 5}
    a["text" if op == "type" else "option"] = value
    a.update(kw)
    return a


def test_a_cited_fact_that_exists_is_accepted():
    m = _material()
    v = protocol.check(_act(source="resume", evidence="edu1: Master of Science, 2018", **{"class": "factual"}), EDU_Q, [], m)
    assert v.ok and v.klass == qclass.FACTUAL
    # by the fact's own words, without an id
    v = protocol.check(_act("type", "Northwind Analytics", source="resume", evidence="current job at Northwind Analytics",
                            **{"class": "factual"}), "Current employer", [], m)
    assert v.ok, v.reason
    # where they live, from the profile
    v = protocol.check(_act("type", "Chicago, IL", source="profile", evidence="loc: Chicago, IL", **{"class": "factual"}),
                       "Which city do you currently live in?", [], m)
    assert v.ok, v.reason
    # a saved answer that is in the block
    v = protocol.check(_act("select", "Other Job Board", source="saved_answer", evidence="ans1: Job board",
                            **{"class": "common_sense"}), "How did you hear about us?", [], m)
    assert v.ok, v.reason


def test_a_cited_fact_that_does_not_exist_is_refused_with_the_reason():
    m = _material()
    v = protocol.check(_act(value="Doctorate", source="resume", evidence="edu7: PhD, 2020", **{"class": "factual"}), EDU_Q, [], m)
    assert not v.ok and "not in <person>" in v.reason and not v.hard
    v = protocol.check(_act("type", "Globex Dynamics", source="resume", evidence="worked at Globex Dynamics in 2015",
                            **{"class": "factual"}), "Current employer", [], m)
    assert not v.ok
    # a real fact cited, an invented value typed
    v = protocol.check(_act("type", "Globex Dynamics", source="resume", evidence="job1", **{"class": "factual"}),
                       "Current employer", [], m)
    assert not v.ok and "typed is not in <person>" in v.reason
    # no source, evidence or class at all
    v = protocol.check(_act(), EDU_Q, [], m)
    assert not v.ok and "source, evidence and class" in v.reason
    # common sense never answers a fact about the person
    v = protocol.check(_act(source="common_sense", evidence="most applicants hold one", **{"class": "common_sense"}), EDU_Q, [], m)
    assert not v.ok and "common sense does not answer it" in v.reason


def test_a_legal_answer_needs_the_persons_saved_answer():
    m = _material()
    legal = "Will you now or in the future require visa sponsorship?"
    v = protocol.check(_act(value="No", source="resume", evidence="job1: works in Chicago", **{"class": "factual"}), legal, ["Yes", "No"], m)
    assert not v.ok and v.hard == "5" and "hard limit 5" in v.reason and v.klass == qclass.LEGAL
    # whatever class the brain claims, the question decides
    v = protocol.check(_act(value="No", source="common_sense", evidence="most people do not", **{"class": "common_sense"}), legal, [], m)
    assert not v.ok and v.hard == "5"
    # and a question the patterns do not know, classed legal by the brain itself
    v = protocol.check(_act(value="No", source="resume", evidence="job1", **{"class": "legal"}), "Favourite spreadsheet shortcut", [], m)
    assert not v.ok and v.hard == "5"
    # the saved answer (a placeholder the brain cannot read) always passes
    assert protocol.check({"op": "type", "index": 5, "text": "(resolved)", "placeholder": "answer_1"}, legal, [], m, saved=True).ok
    # other names used is an attestation: "N/A" is never common sense there (PR #304's setup question stays)
    aliases = "List any aliases or other name(s) you have used or currently use or indicate N/A."
    v = protocol.check(_act("type", "N/A", source="common_sense", evidence="nothing in the material suggests another name",
                            **{"class": "common_sense"}), aliases, [], m)
    assert not v.ok and v.hard == "5"


def test_worked_here_no_comes_from_the_history_and_yes_is_the_persons():
    m = _material()
    no = _act(value="No", source="resume", evidence="no employer named Brightwater Freight in work history", **{"class": "factual"})
    assert protocol.check(no, WORKED_Q, ["Yes", "No"], m).ok
    yes = _act(value="Yes", source="resume", evidence="job1", **{"class": "factual"})
    assert not protocol.check(yes, WORKED_Q, ["Yes", "No"], m).ok
    # the employer IS in the history: a hard limit either way, the person confirms
    m2 = _material(company="Larkspur Labs")
    v = protocol.check(_act(value="Yes", source="resume", evidence="job2: Larkspur Labs", **{"class": "factual"}),
                       "Have you worked for Larkspur Labs before?", ["Yes", "No"], m2)
    assert not v.ok and v.hard == "5" and "the person confirms" in v.reason


def test_common_sense_and_stand_ins():
    m = _material()
    ok = protocol.check(_act(value="Mobile", source="common_sense", evidence="a personal phone is a mobile", **{"class": "common_sense"}),
                        "Phone Device Type", ["Landline", "Mobile"], m)
    assert ok.ok and ok.klass == qclass.COMMON
    # "N/A" where the question asks for it and no fact is at stake
    v = protocol.check(_act("type", "N/A", source="common_sense", evidence="the question above was answered No",
                            **{"class": "common_sense"}), "If yes, please explain", [], m)
    assert v.ok, v.reason
    # filler is never an answer, and "N/A" is not one on a box that did not ask for it
    v = protocol.check(_act("type", "TBD", source="common_sense", evidence="unknown", **{"class": "common_sense"}), "Team name", [], m)
    assert not v.ok and "filler" in v.reason
    v = protocol.check(_act("type", "N/A", source="common_sense", evidence="nothing to say", **{"class": "common_sense"}), "Team name", [], m)
    assert not v.ok and "stand-in" in v.reason
    # a certification the material does not hold: "No" on the evidence of its absence
    v = protocol.check(_act(value="No", source="resume", evidence="no PMP in skills or education", **{"class": "factual"}),
                       "Do you hold a PMP certification?", ["Yes", "No"], m)
    assert v.ok, v.reason
    # and the same claim about something the material DOES hold is refused
    v = protocol.check(_act(value="No", source="resume", evidence="no Tableau in skills", **{"class": "factual"}),
                       "Do you have experience with Tableau?", ["Yes", "No"], m)
    assert not v.ok and "is in <person>" in v.reason
    # a long number never goes in as free text
    v = protocol.check(_act("type", "5550100199", source="profile", evidence="loc", **{"class": "factual"}), "Team extension", [], m)
    assert not v.ok and v.hard == "6"


def test_the_record_carries_the_origin_label_review_answers_shows():
    m = _material()
    a = _act(source="resume", evidence="edu1: Master of Science, 2018", **{"class": "factual"})
    rec = protocol.record(a, protocol.check(a, EDU_Q, [], m))
    assert rec == {"source": "resume", "origin": "history", "evidence": "edu1: Master of Science, 2018", "class": "factual"}
    assert protocol.ORIGIN == {"profile": "profile", "resume": "history", "saved_answer": "library", "common_sense": "common_sense"}


# --------------------------------------------------------------------------- the brain's step
PAGE = """URL: https://jobs.brightwater.example/apply/123
Title: Application questions
[Start of page]
[3]<select label="What is the highest level of education you have completed?" value="" options=6 first="Select One|High School|Associate Degree|Bachelor's Degree" required>
[4]<radio label="Have you worked for Brightwater Freight before?" option="Yes" checked=false required>
[5]<radio label="Have you worked for Brightwater Freight before?" option="No" checked=false required>
[6]<select label="Will you now or in the future require visa sponsorship?" value="" options=3 first="Select One|Yes|No" required>
[7]<combobox label="How did you hear about us?" value="" expanded=false required>
[8]<input type=text label="Search jobs" value="" search=true>
[9]<button>Next</button>
[End of page]"""
LEFT = [EDU_Q, WORKED_Q, "Will you now or in the future require visa sponsorship?"]


class Scripted:
    def __init__(self, *replies):
        self.replies, self.calls, self.systems = list(replies), [], []

    def agent_step(self, system, user):
        self.systems.append(system)
        self.calls.append(user)
        return self.replies.pop(0) if self.replies else ""


def _reply(actions, done=None):
    return json.dumps({"memory": "m", "evaluation": "e", "summary": "Answering the questions", "actions": actions, "done": done})


def _step(llm, mat=None, view=PAGE, left=LEFT, **kw):
    req = {"view": view, "step": 3, "max_steps": 40, "left": left, "history": [], **kw}
    return brain.step(req, llm, APPLICANT, None, placeholders=["email"], material=mat)


def test_the_person_block_reaches_the_model_in_the_system_prompt_never_in_the_page_message():
    llm = Scripted(_reply([{"op": "click", "index": 9}]))
    _step(llm, _material())
    assert "<person>" in llm.systems[0] and "Northwind Analytics" in llm.systems[0]
    assert "<person>" not in llm.calls[0] and "Northwind Analytics" not in llm.calls[0]
    assert llm.systems[0].startswith(brain.SYSTEM_PROMPT)                 # the rules stay the stable prefix


def test_cached_blocks_go_to_a_client_that_marks_them():
    class Cached(Scripted):
        def agent_step_blocks(self, blocks, user):
            self.blocks = list(blocks)
            return self.replies.pop(0)
    llm = Cached(_reply([{"op": "click", "index": 9}]))
    _step(llm, _material())
    assert llm.blocks[0] == brain.SYSTEM_PROMPT and "<person>" in llm.blocks[1] and len(llm.blocks) == 2


def test_the_broker_client_sends_the_system_prompt_as_cached_blocks():
    """The rules and the person's material each end at a cache breakpoint: the steps of one
    application read them from the cache. (Before, the step call sent a plain system string.)"""
    from llm.broker_client import BrokerLLM
    llm, sent = BrokerLLM(model="m"), {}
    llm._post = lambda payload: sent.update(payload) or "{}"
    llm.agent_step_blocks(brain.system_blocks(_material()), "USER")
    assert sent["prompt"] == "USER" and len(sent["system"]) == 2
    assert all(b["type"] == "text" and b["cache_control"] == {"type": "ephemeral"} for b in sent["system"])
    assert sent["system"][0]["text"] == brain.SYSTEM_PROMPT and "<person>" in sent["system"][1]["text"]
    # the broker's own check accepts exactly this shape
    from backend.broker_tools import tool_args
    assert len(tool_args({"system": sent["system"]})["system"]) == 2
    # a cached read is priced at a tenth
    full = brain.estimate_cost({"input": 3000, "output": 100}, 0, 0, "claude-sonnet-4-6")
    cached = brain.estimate_cost({"input": 3000, "output": 100, "cache_read": 2900}, 0, 0, "claude-sonnet-4-6")
    assert 0 < cached < full / 3


def test_an_answer_from_the_block_runs_with_its_proof_and_a_refusal_comes_back_as_an_observation():
    llm = Scripted(_reply([
        {"op": "select", "index": 3, "option": "Master's Degree", "source": "resume", "evidence": "edu1: Master of Science, 2018",
         "class": "factual"},
        {"op": "click", "index": 5, "source": "resume", "evidence": "no employer named Brightwater Freight in <person>",
         "class": "factual"},
        {"op": "select", "index": 6, "option": "No", "source": "resume", "evidence": "job1", "class": "factual"},
        {"op": "type", "index": 8, "text": "analyst"},
        {"op": "click", "index": 9},
    ]))
    out = _step(llm, _material())
    ops = [(a["op"], a["index"]) for a in out["actions"]]
    assert ops == [("select", 3), ("click", 5), ("type", 8), ("click", 9)]            # the legal one did not run
    edu, worked = out["actions"][0], out["actions"][1]
    assert edu["proof"] == {"source": "resume", "origin": "history", "evidence": "edu1: Master of Science, 2018", "class": "factual"}
    assert "source" not in edu and worked["proof"]["origin"] == "history"
    assert "proof" not in out["actions"][2] and "proof" not in out["actions"][3]     # a search box and a button answer nothing
    # the refusal: said, with the limit named, and counted
    assert len(out["observations"]) == 1 and "hard limit 5" in out["observations"][0] and "[6]" in out["observations"][0]
    assert out["rejected"] == [{"index": 6, "label": "Will you now or in the future require visa sponsorship?", "hard": "5"}]
    # nothing the person's is in an observation
    assert "Northwind" not in " ".join(out["observations"])


def test_the_brain_picks_an_option_itself_when_no_choice_says_the_same_as_the_saved_answer():
    """The option-choice path: the page handed the list's options back (extension/agent_view.js);
    the brain picks one under the protocol, citing the saved answer it can see in the block."""
    llm = Scripted(_reply([{"op": "select", "index": 7, "option": "Other Job Board", "source": "saved_answer",
                            "evidence": "ans1: Job board", "class": "common_sense"}]))
    out = _step(llm, _material(), left=["How did you hear about us? (saved answer: type {{answer_1}})"],
                observations=["Your type on [7] did not work: no choice on this list says the same as the saved answer; "
                              "its options: Indeed, LinkedIn, Other Job Board, Employee Referral."])
    assert [a["op"] for a in out["actions"]] == ["select"] and out["actions"][0]["option"] == "Other Job Board"
    assert out["actions"][0]["proof"]["origin"] == "library" and not out["rejected"]
    assert "its options: Indeed" in llm.calls[0]


def test_a_saved_value_on_a_list_is_the_strict_pick_and_needs_no_proof():
    llm = Scripted(_reply([{"op": "select", "index": 6, "option": "{{answer_1}}"}]))
    req = {"view": PAGE, "step": 3, "left": LEFT, "history": []}
    out = brain.step(req, llm, {**APPLICANT, "answer_1": "No"}, None, placeholders=["answer_1"], material=_material())
    a = out["actions"][0]
    assert a["option"] == "No" and a["answer"] is True and a["sensitive"] is True and not out["rejected"]


def test_hard_limits_name_themselves_and_only_the_refused_action_is_dropped():
    review = PAGE.replace("[9]<button>Next</button>", "[9]<button>Submit Application</button>\n[10]<a>Apply with LinkedIn</a>")
    llm = Scripted(_reply([
        {"op": "select", "index": 3, "option": "Master's Degree", "source": "resume", "evidence": "edu1", "class": "factual"},
        {"op": "click", "index": 10},
        {"op": "click", "index": 9},
    ]))
    out = _step(llm, _material(), view=review)
    # the answer still runs; the Submit click and the LinkedIn click do not, and both say why
    assert [(a["op"], a["index"]) for a in out["actions"]] == [("select", 3)] and out["done"] is None
    said = " | ".join(out["observations"])
    assert "Hard limit 1" in said and "Hard limit 3" in said
    # a Submit click alone still means "ready" (as before)
    out = _step(Scripted(_reply([{"op": "click", "index": 9}])), _material(), view=review)
    assert out["actions"] == [] and out["done"]["outcome"] == "ready_to_submit"


def test_an_index_the_model_made_up_is_said_not_swallowed():
    out = _step(Scripted(_reply([{"op": "click", "index": 99}, {"op": "click", "index": 9}])), _material())
    assert [a["index"] for a in out["actions"]] == [9]
    assert any("no such index" in o for o in out["observations"])


def test_a_suggestion_click_on_a_legal_list_needs_a_saved_value_typed_first():
    view = PAGE.replace('[7]<combobox label="How did you hear about us?" value="" expanded=false required>',
                        '[7]<combobox label="Gender" value="" expanded=true required>\n*[71]<option>Male</option>')
    out = _step(Scripted(_reply([{"op": "click", "index": 71}])), _material(), view=view)
    assert out["actions"] == [] and out["rejected"][0]["hard"] == "5"
    hist = [{"step": 2, "evaluation": "typed", "actions": ["type 7 (secret)"], "results": ["ok"]}]
    out = _step(Scripted(_reply([{"op": "click", "index": 71}])), _material(), view=view, history=hist)
    assert [a["index"] for a in out["actions"]] == [71]


def test_the_prompt_states_the_principle():
    p = brain.SYSTEM_PROMPT
    assert len(p.splitlines()) < 60 and not re.search(r"[—–]", p)
    for want in ("You may answer a question about the person yourself", '"source"', '"evidence"', '"class"',
                 "Hard limits, checked in code", "The engine suggests", "<person>"):
        assert want in p, want
    assert "Never answer a question about the person" not in p            # the rule this replaces
    assert "You may type ONLY a placeholder" not in p


# --------------------------------------------------------------------------- the proof
class BlockBrain(FakeLLM):
    """The fake brain, taught only this: read <person> (the system prompt) and answer the two
    open questions from it under the protocol. No rule of the engine is consulted."""

    def agent_step(self, system, user):
        person = re.search(r"<person>\n(.*?)\n</person>", system, re.S)
        assert person, "the brain was not given the person's material"
        block = person.group(1)
        view = re.search(r"<view>\n(.*?)\n</view>", user, re.S).group(1)
        left = re.search(r"<left>\n(.*?)\n</left>", user, re.S).group(1)
        acts = []
        if EDU_Q in left:
            edu = re.search(r"^(edu\d): (Master[^,\n]*)", block, re.M)
            idx = int(re.search(r'\[(\d+)\]<select label="What is the highest level', view).group(1))
            acts.append({"op": "select", "index": idx, "option": "Master's Degree", "source": "resume",
                         "evidence": f"{edu.group(1)}: {edu.group(2)}", "class": "factual"})
        if WORKED_Q in left:
            assert "Brightwater" not in block
            idx = int(re.search(r'\[(\d+)\]<radio label="Have you worked for Brightwater Freight before\?" option="No"', view).group(1))
            acts.append({"op": "click", "index": idx, "source": "resume",
                         "evidence": "no employer named Brightwater Freight in <person>", "class": "factual"})
        return json.dumps({"memory": "", "evaluation": "", "summary": "", "actions": acts, "done": None})


def _fields():
    return [FormField("f1", EDU_Q, "select", ["High School", "Bachelor's Degree", "Master's Degree", "Doctorate"], True),
            FormField("f2", WORKED_Q, "radio", ["Yes", "No"], True)]


def _ctx():
    return KnownContext(applicant=APPLICANT, company=COMPANY, per_company=True, worked_here=("No", "history"),
                        education=commonsense.highest_education(PROFILE, TODAY), library=Lib())


def test_with_the_fast_path_rules_on_they_answer_without_a_model_step(monkeypatch):
    monkeypatch.delenv("SJ_FASTPATH_OFF", raising=False)
    res = plan_known(_fields(), _ctx())
    assert {a.ref: (a.value, a.origin) for a in res.actions} == {"f1": ("Master's Degree", "history"), "f2": ("No", "history")}
    assert not res.left


def test_with_the_fast_path_rules_off_the_brain_answers_from_the_block_and_passes_the_check(monkeypatch):
    """The owner's test: removing a per-question rule must not make the agent ask the person.
    Highest education and worked-here, rules off: the engine leaves both open, the brain answers
    both from <person>, both pass the answer protocol, nothing is refused and nothing is asked."""
    monkeypatch.setenv("SJ_FASTPATH_OFF", "education,worked_here")
    assert not commonsense.fast_path("education") and commonsense.fast_path("heard")
    res = plan_known(_fields(), _ctx())
    assert [f.label for f in res.left] == [EDU_Q, WORKED_Q] and not res.actions      # the rules stood back
    view = PAGE.replace('[6]<select label="Will you now or in the future require visa sponsorship?" value="" options=3 first="Select One|Yes|No" required>\n', "")
    out = brain.step({"view": view, "step": 4, "left": [f.label for f in res.left], "history": []},
                     BlockBrain(), APPLICANT, None, placeholders=[], material=_material())
    assert out["ok"] and out["done"] is None                                # never needs-answers
    assert [(a["op"], a["index"]) for a in out["actions"]] == [("select", 3), ("click", 5)]
    assert out["rejected"] == [] and out["observations"] == []
    assert out["actions"][0]["option"] == "Master's Degree" and out["actions"][0]["proof"]["origin"] == "history"
    assert out["actions"][1]["proof"]["evidence"].startswith("no employer named")


# --------------------------------------------------------------------------- the routes
@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    return mod


def test_the_step_route_sends_the_material_and_words_a_rules_answer_as_advice(A, monkeypatch):
    mod = A
    got = {}

    def fake_step(req, llm, applicant, portal=None, **kw):
        got["left"], got["material"], got["values"] = list(req["left"]), kw.get("material"), dict(applicant)
        return {"ok": True, "actions": [], "done": None, "memory": "", "evaluation": "", "notes": [], "cost": 0.0}
    monkeypatch.setattr(brain, "step", fake_step)
    monkeypatch.setattr(mod, "_saved_full_profile", lambda: PROFILE)
    monkeypatch.setattr(mod, "_applicant_for", lambda prof: dict(APPLICANT))
    heard = "How did you hear about us?"
    view = ('URL: https://jobs.brightwater.example/apply\n[3]<combobox label="' + heard + '" value="" expanded=false required>\n'
            '[4]<input type=text label="Team name" value="" required>\n[5]<button>Next</button>')
    body = {"record_id": None, "step": 2, "max_steps": 40, "url": "https://jobs.brightwater.example/apply", "view": view,
            "left": [heard, "Team name"], "history": []}
    mod.app.test_client().post("/api/autoapply/agent/step", headers=H, json=body)
    # the fast path's common-sense answer is a SUGGESTION in words, with its placeholder
    assert got["left"][0] == heard + " (the engine suggests Job board: type {{answer_1}})", got["left"]
    assert got["left"][1] == "Team name" and got["values"]["answer_1"] == "Job board"
    m = got["material"]
    assert m and "Northwind Analytics" in m.block and "Larkspur Labs" in m.employers
    # no open question: no block is sent
    mod.app.test_client().post("/api/autoapply/agent/step", headers=H, json={**body, "left": []})
    assert got["material"] is None
    # the person's own answer on the bar stays a placeholder, its words never shown
    mod.app.test_client().post("/api/autoapply/agent/step", headers=H, json={**body, "answers": {"Team name": "Blue team"}})
    assert got["left"][1] == "Team name (saved answer: type {{answer_2}})"


def test_the_answers_the_agent_chose_are_kept_on_the_application(A):
    mod = A
    rows = [{"page": 2, "label": EDU_Q, "value": "Master's Degree", "source": "history",
             "evidence": "edu1: Master of Science, 2018", "class": "factual"},
            {"page": 2, "label": "", "value": "x", "source": "history"},                 # no question: dropped
            {"page": 3, "label": "Phone Device Type", "value": "Mobile", "source": "made up", "evidence": "x" * 400}]
    clean = mod._agent_answers_clean(rows)
    assert len(clean) == 2 and clean[0]["source"] == "history" and clean[0]["evidence"].startswith("edu1")
    assert clean[1]["source"] == "" and len(clean[1]["evidence"]) == 160
    assert mod._agent_answers_clean(None) is None
    recs = mod._records()
    try:
        rid = recs.add("Data Analyst", COMPANY, 70, "", data={"status": "approved"})
    finally:
        recs.close()
    c = mod.app.test_client()
    r = c.post("/api/autoapply/agent/ready", headers=H, json={"record_id": rid, "answers_only": True, "agent_answers": rows}).get_json()
    assert r == {"ok": True, "agent_answers": 2}
    data = mod._record_data(mod._get_record(rid))
    assert data["agent_answers"][0]["label"] == EDU_Q and not data.get("agent_ready")


def test_the_review_answers_label_is_in_five_locales():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'T("aa.ans.flag.agent"' in js and 'code === "agent"' in js
    for code in ("de", "en", "es", "fr", "pt"):
        loc = json.loads((ROOT / "ui" / "static" / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        for k in ("aa.ans.flag.agent", "aa.ans.flag.agentPlain", "aa.ans.from.history", "aa.ans.from.commonSense"):
            assert loc.get(k), (code, k)
        assert "{e}" in loc["aa.ans.flag.agent"] and not re.search(r"[—–]", loc["aa.ans.flag.agent"])


# --------------------------------------------------------------------------- the shell
def test_the_shell_feeds_refusals_back_and_records_the_agents_answers():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_agent_first.js")], capture_output=True, text=True, timeout=180)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr
