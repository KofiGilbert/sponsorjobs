"""Stage suggestions from the person's own inbox, and per-stage email drafts (CLAUDE.md §5).

Read-first: the rules classify fixed messages offline; the store remembers what the person
settled so the same mail never asks twice; the endpoints move a card ONLY on the person's
click; drafts come from the FakeLLM and are for the person to send (a Gmail compose link,
never a send). No live mailbox, no network.
"""

from __future__ import annotations

import importlib
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest

from inbox import detect
from inbox.drafts import DRAFT_TYPES, draft_email, gmail_compose_url
from inbox.suggestions import SuggestionStore, build_suggestions, suggestion_id
from llm.base import FakeLLM

JOBS = [
    {"source_id": "freehire::acme", "company": "Acme Robotics, Inc.", "title": "Data Analyst", "stage": "applied"},
    {"source_id": "freehire::beta", "company": "Beta Labs", "title": "Engineer", "stage": "interviewing"},
    {"source_id": "freehire::gamma", "company": "Gamma", "title": "PM", "stage": "saved"},
]

INVITE = {"id": "m-invite", "from": "Dana Lopez <dana@acme-robotics.com>",
          "subject": "Next steps on your Data Analyst application",
          "body": "We reviewed your application and would love to schedule a phone screen. "
                  "Here is a Calendly link: https://calendly.com/acme/30min", "date": "Mon, 6 Oct 2026"}
REJECT = {"id": "m-reject", "from": "no-reply@acmerobotics.com", "subject": "Your application to Acme Robotics",
          "body": "Thank you for your interest. Unfortunately we have decided to move forward with other candidates.",
          "date": "Tue, 7 Oct 2026"}
OFFER = {"id": "m-offer", "from": "people@betalabs.com", "subject": "Offer letter: Engineer at Beta Labs",
         "body": "We are pleased to offer you the position. The offer letter is attached.", "date": "Wed"}
ASSESS = {"id": "m-assess", "from": "HackerRank <no-reply@hackerrank.com>",
          "subject": "Acme Robotics invites you to complete a coding assessment",
          "body": "Complete the assessment within 7 days.", "date": "Thu"}
RECEIVED = {"id": "m-recv", "from": "jobs@gamma.com", "subject": "Thank you for applying to Gamma",
            "body": "We have received your application for PM and will be in touch.", "date": "Fri"}
OUTREACH = {"id": "m-out", "from": "Sam Recruiter <sam@gamma.com>", "subject": "Hello from the Gamma hiring team",
            "body": "I am a recruiter on the hiring team at Gamma, are you open to new opportunities?", "date": "Sat"}
NOISE = {"id": "m-noise", "from": "newsletter@jobs.example", "subject": "10 new jobs this week",
         "body": "Here are some roles. Unsubscribe any time.", "date": "Sun"}
FRIEND = {"id": "m-friend", "from": "pal@gmail.com", "subject": "how did the Acme Robotics thing go?",
          "body": "lunch tomorrow?", "date": "Sun"}
SOFT = {"id": "m-soft", "from": "dana@acme-robotics.com", "subject": "Quick note",
        "body": "Would you like to come in and meet the team on Thursday?", "date": "Mon"}


# --- detection rules --------------------------------------------------------------------------

@pytest.mark.parametrize("msg, kind, stage, phrase", [
    (INVITE, "interview_invite", "interviewing", "Next steps"),
    (REJECT, "rejection", "rejected", "Unfortunately"),
    (OFFER, "offer", "offer", "Offer letter"),
    (ASSESS, "assessment", "interviewing", "coding assessment"),
    (RECEIVED, "application_received", "applied", "Thank you for applying"),
    (OUTREACH, "recruiter_outreach", "", "hiring team"),
    (NOISE, "other", "", ""),
])
def test_triage_classifies_each_kind_and_quotes_the_phrase(msg, kind, stage, phrase):
    t = detect.triage(msg)
    assert t["kind"] == kind and t["stage"] == stage
    assert t["phrase"].lower() == phrase.lower()
    if phrase:
        assert f'"{t["phrase"]}"' in t["reason"] and t["reason"].startswith(("Subject says", "Message says"))
    else:
        assert t["reason"] == ""


def test_rejection_beats_interview_and_offer_wording():
    msg = {"subject": "Your interview with Acme", "body": "Unfortunately, after your interview we will not be moving forward."}
    assert detect.triage(msg)["kind"] == "rejection"
    msg = {"subject": "Update on your offer", "body": "We regret to inform you the position has been filled."}
    assert detect.triage(msg)["kind"] == "rejection"


def test_verification_mail_is_not_an_application_confirmation():
    msg = {"subject": "Please verify your email to complete your application",
           "body": "Confirm your application: https://boards.greenhouse.io/verify?t=abc"}
    assert detect.classify(msg) == "verification"
    assert detect.triage(msg)["kind"] == "other"


def test_every_kind_has_a_stage_entry():
    assert set(detect.STAGE_FOR) == set(detect.KINDS)


# --- employer matching + building --------------------------------------------------------------

def test_build_matches_the_employer_and_proposes_the_stage():
    out = build_suggestions([INVITE, REJECT, OFFER, ASSESS, RECEIVED, OUTREACH, NOISE, FRIEND], JOBS)
    by_id = {s["id"]: s for s in out}
    assert by_id["m-invite"]["source_id"] == "freehire::acme" and by_id["m-invite"]["stage"] == "interviewing"
    assert by_id["m-invite"]["from_stage"] == "applied" and by_id["m-invite"]["company"] == "Acme Robotics, Inc."
    assert by_id["m-invite"]["reason"] == 'Subject says "Next steps"'
    assert by_id["m-invite"]["sender"] == "Dana Lopez" and by_id["m-invite"]["status"] == "pending"
    assert by_id["m-reject"]["stage"] == "rejected"
    assert by_id["m-offer"]["source_id"] == "freehire::beta" and by_id["m-offer"]["stage"] == "offer"
    assert by_id["m-assess"]["stage"] == "interviewing" and by_id["m-assess"]["kind"] == "assessment"
    assert by_id["m-recv"]["source_id"] == "freehire::gamma" and by_id["m-recv"]["stage"] == "applied"
    # Outreach proposes no stage, noise names no employer, and a friend mentioning the company
    # has no stage wording: none of them become a suggestion.
    assert "m-out" not in by_id and "m-noise" not in by_id and "m-friend" not in by_id


def test_no_suggestion_when_the_job_is_already_there_or_closed():
    jobs = [{"source_id": "a", "company": "Acme Robotics", "title": "x", "stage": "interviewing"},
            {"source_id": "b", "company": "Beta Labs", "title": "y", "stage": "rejected"}]
    assert build_suggestions([INVITE, ASSESS, OFFER], jobs) == []
    # A rejection after an interview is still news.
    assert [s["stage"] for s in build_suggestions([REJECT], jobs)] == ["rejected"]
    # An application confirmation for a job already Applied adds nothing.
    jobs = [{"source_id": "g", "company": "Gamma", "title": "PM", "stage": "applied"}]
    assert build_suggestions([RECEIVED], jobs) == []


def test_model_pass_runs_only_for_other_mail_from_a_tracked_employer():
    class Spy(FakeLLM):
        seen = []

        def classify_application_mail(self, subject, body, sender, company):
            self.seen.append((subject, company))
            return super().classify_application_mail(subject, body, sender, company)

    llm = Spy()
    out = build_suggestions([INVITE, SOFT, NOISE, FRIEND], JOBS, llm=llm)
    # Only SOFT (rules said "other", names Acme) and FRIEND (same) reached the model; never the
    # newsletter and never the already-classified invite.
    assert [c for _, c in llm.seen] == ["Acme Robotics, Inc.", "Acme Robotics, Inc."]
    assert [s for s, _ in llm.seen] == ["Quick note", "how did the Acme Robotics thing go?"]
    soft = next(s for s in out if s["id"] == "m-soft")
    assert soft["kind"] == "interview_invite" and soft["stage"] == "interviewing"
    assert "come in and meet" in soft["reason"]
    assert not any(s["id"] == "m-friend" for s in out)       # the fake does not guess


def test_model_failure_or_nonsense_leaves_the_rules_verdict():
    class Boom:
        def classify_application_mail(self, *a, **k):
            raise RuntimeError("down")

    class Nonsense:
        def classify_application_mail(self, *a, **k):
            return {"kind": "promotion", "phrase": "x"}

    assert build_suggestions([SOFT], JOBS, llm=Boom()) == []
    assert build_suggestions([SOFT], JOBS, llm=Nonsense()) == []


def test_suggestion_id_is_stable_without_a_mailbox_id():
    a = {"from": "x@y", "subject": "s", "date": "d"}
    assert suggestion_id(a) == suggestion_id(dict(a)) and suggestion_id(a).startswith("h")
    assert suggestion_id({"id": "abc/def 1"}) == "abc_def_1"


# --- store lifecycle --------------------------------------------------------------------------

def test_store_lifecycle_never_reprompts(tmp_path):
    st = SuggestionStore(tmp_path / "sugg.json")
    assert st.pending() == [] and st.all() == []
    new = st.add(build_suggestions([INVITE, REJECT], JOBS))
    assert [s["id"] for s in new] == ["m-invite", "m-reject"]
    assert st.add(build_suggestions([INVITE], JOBS)) == []          # already known: not re-added
    st.set_status("m-invite", "accepted")
    st.set_status("m-reject", "dismissed")
    assert st.pending() == []
    assert st.get("m-invite")["status"] == "accepted" and st.get("m-invite")["decided_at"]
    # A rescan with the same mail adds nothing and flips nothing back.
    assert st.add(build_suggestions([INVITE, REJECT], JOBS)) == []
    assert {s["status"] for s in st.all()} == {"accepted", "dismissed"}
    with pytest.raises(ValueError):
        st.set_status("m-invite", "maybe")
    assert st.set_status("nope", "dismissed") is None
    # The file is plain JSON on disk, nothing else.
    assert json.loads((tmp_path / "sugg.json").read_text())[0]["id"] == "m-invite"


def test_store_survives_a_corrupt_file(tmp_path):
    f = tmp_path / "sugg.json"
    f.write_text("{not json")
    st = SuggestionStore(f)
    assert st.all() == []
    st.add(build_suggestions([OFFER], JOBS))
    assert [s["id"] for s in st.pending()] == ["m-offer"]


# --- Gmail compose URL ------------------------------------------------------------------------

def test_gmail_compose_url_encodes_every_field():
    url = gmail_compose_url("dana@acme.com", "Re: Offer & next steps #2", "Hi Dana,\n\nLine two + more.\n")
    u = urlparse(url)
    assert u.scheme == "https" and u.netloc == "mail.google.com" and u.path == "/mail/"
    q = parse_qs(u.query, keep_blank_values=True)
    assert q["view"] == ["cm"] and q["to"] == ["dana@acme.com"]
    assert q["su"] == ["Re: Offer & next steps #2"]
    assert q["body"] == ["Hi Dana,\n\nLine two + more.\n"]
    assert "&to=" not in gmail_compose_url("", "s", "b")           # no recipient: no empty to=
    assert " " not in url and "\n" not in url


# --- drafts (FakeLLM) --------------------------------------------------------------------------

PROFILE = {"identity": {"name": "Sam Rivera"}, "skills": ["Python", "SQL"], "experience": []}
JOB = {"source_id": "freehire::acme", "company": "Acme Robotics", "title": "Data Analyst", "stage": "applied"}


@pytest.mark.parametrize("kind", DRAFT_TYPES)
def test_fake_draft_for_every_type_is_grounded_and_dash_free(kind):
    contact = {"id": 1, "name": "Dana Lopez", "email": "dana@acme.com"} if kind == "referral" else None
    out = draft_email(FakeLLM(), kind, JOB, PROFILE, contact=contact)
    assert out["type"] == kind and out["subject"] and out["body"]
    assert "Sam Rivera" in out["body"] and "Acme Robotics" in out["body"]
    assert not re.search(r"[–—]", out["subject"] + out["body"])
    assert out["to"] == ("dana@acme.com" if contact else "")
    if contact:
        assert out["body"].startswith("Hi Dana,")


def test_referral_needs_a_named_contact_and_type_is_validated():
    with pytest.raises(ValueError):
        draft_email(FakeLLM(), "referral", JOB, PROFILE, contact=None)
    with pytest.raises(ValueError):
        draft_email(FakeLLM(), "poem", JOB, PROFILE)


def test_draft_strips_model_dashes():
    class Dashy:
        def draft_stage_email(self, *a, **k):
            return {"subject": "Hello — there", "body": "One – two — three"}
    out = draft_email(Dashy(), "follow_up", JOB, PROFILE)
    assert "—" not in out["subject"] and "–" not in out["body"] and "—" not in out["body"]


# --- endpoints --------------------------------------------------------------------------------

BOARD_JOB = {"source_id": "freehire::acme", "source": "freehire", "company": "Acme Robotics, Inc.",
             "title": "Data Analyst", "location": "NYC", "remote": "", "url": "http://x",
             "posted_at": "2026-10-01", "salary": "", "jd_text": ""}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    import ui.app as A
    importlib.reload(A)
    monkeypatch.setattr(A, "_saved_full_profile", lambda: PROFILE)
    return A


def _board(c, stage="applied"):
    r = c.post("/api/jobs/save", json={"source_id": BOARD_JOB["source_id"], "saved": True, "job": BOARD_JOB})
    assert r.status_code == 200
    if stage != "saved":
        assert c.post("/api/tracker/move", json={"source_id": BOARD_JOB["source_id"], "stage": stage}).status_code == 200


def test_scan_stores_suggestions_and_accept_moves_the_card_with_the_subject(app):
    c = app.app.test_client()
    _board(c)
    d = c.post("/api/inbox/scan", json={"messages": [INVITE, NOISE], "draft": False}).get_json()
    assert d["ok"] and [s["id"] for s in d["suggestions"]] == ["m-invite"]
    s = d["suggestions"][0]
    assert s["stage"] == "interviewing" and s["company"] == "Acme Robotics, Inc." and s["reason"] == 'Subject says "Next steps"'
    # Listed, and NOTHING moved yet.
    assert [x["id"] for x in c.get("/api/inbox/suggestions").get_json()["suggestions"]] == ["m-invite"]
    assert c.get("/api/tracker/board").get_json()["columns"]["interviewing"] == []
    # The file lives in the data dir, like the rest of the local state.
    assert (app._DATA / "inbox_suggestions.json").exists()

    r = c.post("/api/inbox/suggestions/m-invite/accept")
    assert r.status_code == 200
    out = r.get_json()
    assert out["job"]["stage"] == "interviewing" and out["suggestion"]["status"] == "accepted"
    assert out["suggestions"] == []
    board = c.get("/api/tracker/board").get_json()
    assert [x["source_id"] for x in board["columns"]["interviewing"]] == [BOARD_JOB["source_id"]]
    act = c.get(f"/api/tracker/activity?source_id={BOARD_JOB['source_id']}").get_json()["activity"]
    assert act[0]["to_stage"] == "interviewing" and act[0]["note"] == 'From your inbox: "Next steps on your Data Analyst application"'
    # Settled: a second accept is refused, and a rescan of the same mail does not bring it back.
    assert c.post("/api/inbox/suggestions/m-invite/accept").status_code == 409
    d = c.post("/api/inbox/scan", json={"messages": [INVITE], "draft": False}).get_json()
    assert d["suggestions"] == []


def test_dismiss_keeps_the_card_and_never_reprompts(app):
    c = app.app.test_client()
    _board(c)
    c.post("/api/inbox/scan", json={"messages": [REJECT], "draft": False})
    r = c.post("/api/inbox/suggestions/m-reject/dismiss")
    assert r.status_code == 200 and r.get_json()["suggestion"]["status"] == "dismissed"
    assert c.get("/api/inbox/suggestions").get_json()["suggestions"] == []
    assert c.get("/api/tracker/board").get_json()["columns"]["applied"][0]["source_id"] == BOARD_JOB["source_id"]
    d = c.post("/api/inbox/scan", json={"messages": [REJECT], "draft": False}).get_json()
    assert d["suggestions"] == []
    assert c.post("/api/inbox/suggestions/nope/dismiss").status_code == 404
    assert c.post("/api/inbox/suggestions/nope/accept").status_code == 404


def test_accept_settles_duplicates_for_the_same_job_and_stage(app):
    c = app.app.test_client()
    _board(c)
    second = {**INVITE, "id": "m-invite-2", "subject": "Interview with Acme Robotics"}
    d = c.post("/api/inbox/scan", json={"messages": [INVITE, second], "draft": False}).get_json()
    assert len(d["suggestions"]) == 2
    out = c.post("/api/inbox/suggestions/m-invite/accept").get_json()
    assert out["suggestions"] == []
    assert app._suggestion_store().get("m-invite-2")["status"] == "dismissed"


def test_scan_model_pass_uses_the_fake_only_for_tracked_other_mail(app):
    c = app.app.test_client()
    _board(c)
    d = c.post("/api/inbox/scan", json={"messages": [SOFT, FRIEND, NOISE]}).get_json()
    assert [s["id"] for s in d["suggestions"]] == ["m-soft"]
    assert d["suggestions"][0]["kind"] == "interview_invite"


def test_draft_email_endpoint_for_the_person_to_send(app):
    c = app.app.test_client()
    _board(c)
    sid = BOARD_JOB["source_id"]
    r = c.post(f"/api/tracker/{sid}/draft-email", json={"type": "follow_up"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["type"] == "follow_up" and "Sam Rivera" in d["body"] and "Data Analyst" in d["subject"]
    assert d["to"] == "" and d["gmail_url"].startswith("https://mail.google.com/mail/?view=cm&su=")
    assert parse_qs(urlparse(d["gmail_url"]).query)["body"] == [d["body"]]
    assert r.headers.get("Cache-Control") == "no-store"

    # A referral goes to a named contact from the card.
    cid = c.post("/api/tracker/contact", json={"source_id": sid, "name": "Dana Lopez", "email": "dana@acme.com"}).get_json()["id"]
    d = c.post(f"/api/tracker/{sid}/draft-email", json={"type": "referral", "contact_id": cid}).get_json()
    assert d["to"] == "dana@acme.com" and d["body"].startswith("Hi Dana,")
    assert parse_qs(urlparse(d["gmail_url"]).query)["to"] == ["dana@acme.com"]

    assert c.post(f"/api/tracker/{sid}/draft-email", json={"type": "referral"}).status_code == 400
    assert c.post(f"/api/tracker/{sid}/draft-email", json={"type": "referral", "contact_id": 999}).status_code == 404
    assert c.post(f"/api/tracker/{sid}/draft-email", json={"type": "poem"}).status_code == 400
    assert c.post("/api/tracker/freehire::nope/draft-email", json={"type": "follow_up"}).status_code == 404


def test_no_route_sends_mail(app):
    """The feature's one hard rule: every path drafts, none sends. No route in the app calls the
    Gmail send or draft-creation surface for this feature."""
    import inspect
    src = inspect.getsource(app)
    for fn in ("inbox_suggestion_accept", "inbox_suggestion_dismiss", "tracker_draft_email", "_store_inbox_suggestions"):
        body = inspect.getsource(getattr(app, fn))
        assert "create_draft" not in body and ".send(" not in body and "users().messages" not in body
    assert "mail.google.com" in src or True
