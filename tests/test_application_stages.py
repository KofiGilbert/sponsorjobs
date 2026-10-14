"""Honest application stages (submit/stages.py) and the screens that read them.

Queued (needs your review) -> Approved (reviewed, not submitted) -> Submitted (and how) or
Failed ("Needs you", with a reason). Review keeps approved cards and shows the change; Ready to
apply lists approved-not-submitted; the Tracker has an Approved column; no status line claims a
form was filled when nothing was. Also: the Recruitee auto lane sends the real resume and maps
screening answers to the posting's question ids, and template names stay out of role titles.
Every HTTP call here is stubbed: no test ever reaches a real employer's endpoint."""

from __future__ import annotations

import importlib
import json

import pytest

from submit.drivers import encode_multipart, map_open_questions, recruitee_submit
from submit.stages import app_stage, is_waiting_to_submit
from ui.records import CVRecords

RECRUITEE = "https://acme.recruitee.com/o/eng"
LINKEDIN = "https://www.linkedin.com/jobs/view/1"


# --- pure stage logic ------------------------------------------------------------------------

def test_stage_for_every_state():
    assert app_stage({})["stage"] == "queued"
    assert app_stage({"status": "ready"})["label"] == "To review"
    a = app_stage({"status": "approved", "submission": {"tier": "assisted", "status": "approved"}})
    assert a["stage"] == "approved" and "submit" in a["detail"].lower()
    q = app_stage({"status": "approved", "queued": True, "submission": {"status": "queued"}})
    assert q["stage"] == "approved" and q["detail"] == "Queued to submit for you"
    s = app_stage({"status": "applied", "submission": {"status": "auto_submitted"}})
    assert s["stage"] == "submitted" and s["how"] == "auto" and s["detail"] == "Submitted for you"
    you = app_stage({"status": "applied"})
    assert you["how"] == "you" and you["detail"] == "You submitted it"
    f = app_stage({"status": "approved", "submission": {"status": "failed", "reason": "posting_closed"}})
    assert f["stage"] == "failed" and f["label"] == "Needs you" and f["reason"] == "posting_closed"
    assert app_stage({"status": "approved", "submission": {"status": "auto_failed"}})["reason"] == "site_error"
    assert app_stage({"status": "skipped"})["stage"] == "skipped"


def test_waiting_to_submit_is_approved_or_handed_back():
    assert not is_waiting_to_submit({"status": "ready"})
    assert is_waiting_to_submit({"status": "approved"})
    assert is_waiting_to_submit({"status": "approved", "submission": {"status": "failed"}})
    assert not is_waiting_to_submit({"status": "applied"})


def test_no_stage_text_has_dashes_or_says_filled():
    from submit import stages
    texts = list(stages.REASONS.values()) + list(stages.STAGE_LABEL.values()) + list(stages.NEXT.values())
    for d in ({}, {"status": "approved"}, {"status": "approved", "queued": True},
              {"status": "applied"}, {"status": "approved", "submission": {"status": "failed"}}):
        texts.append(app_stage(d)["detail"])
    for t in texts:
        assert "—" not in t and "–" not in t
        assert "filled" not in t.lower()


# --- the app: Review, Ready to apply, Tracker -------------------------------------------------

@pytest.fixture
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    import ui.app as mod
    importlib.reload(mod)
    mod.WORKDIR.mkdir(parents=True, exist_ok=True)
    return mod


def _add(A, role="Engineer", company="Acme", url=LINKEDIN, data=None):
    recs = CVRecords(A.DB_PATH)
    try:
        return recs.add(role, company, 70, "", jd_label=role,
                        data={"status": "ready", "source_job": {"url": url, "source_id": f"x::{role}"},
                              **(data or {})})
    finally:
        recs.close()


def _data(A, rid):
    recs = CVRecords(A.DB_PATH)
    try:
        return json.loads(recs.get(rid)["data"])
    finally:
        recs.close()


def test_assisted_approve_keeps_the_card_and_moves_it_to_ready(A):
    c = A.app.test_client()
    rid = _add(A)
    q = c.get("/api/review/queue").get_json()["pending"]
    assert q[0]["stage"] == "queued" and "filled" not in q[0]
    assert c.get("/api/records/ready").get_json()["count"] == 0     # not approved yet

    r = c.post("/api/review/approve", json={"approve": [rid]}).get_json()["approved"][0]
    assert r["ok"] and r["stage"] == "approved"
    assert "filled" not in r["message"].lower()
    sub = _data(A, rid)["submission"]
    assert "filled" not in sub["message"].lower()

    q = c.get("/api/review/queue").get_json()["pending"]
    assert [i["id"] for i in q] == [rid] and q[0]["stage"] == "approved"   # still there, changed
    assert q[0]["stage_label"] == "Approved"
    ready = c.get("/api/records/ready").get_json()["ready"]
    assert [i["id"] for i in ready] == [rid] and ready[0]["stage"] == "approved"

    c.post(f"/api/record/{rid}/status", json={"status": "applied"})
    d = _data(A, rid)
    assert app_stage(d)["stage"] == "submitted" and d["submission"]["by"] == "you"
    assert c.get("/api/records/ready").get_json()["count"] == 0
    assert c.get("/api/review/queue").get_json()["count"] == 0


def test_auto_submit_success_is_submitted_by_the_auto_lane(A, monkeypatch):
    import submit
    rid = _add(A, url=RECRUITEE)
    monkeypatch.setattr(submit, "driver_for", lambda host: (
        lambda u, d: {"ok": True, "posted": True, "detail": "Submitted to Recruitee with your resume."}))
    r = A.app.test_client().post("/api/review/approve", json={"approve": [rid]}).get_json()["approved"][0]
    assert r["stage"] == "submitted"
    d = _data(A, rid)
    assert d["status"] == "applied" and d["submission"]["by"] == "auto" and d["follow_up_due"]
    board = A.app.test_client().get("/api/tracker/board").get_json()
    card = board["columns"]["applied"][0]
    assert card["record"]["state"] == "submitted" and card["record"]["how"] == "auto"


def test_auto_submit_failure_comes_back_as_needs_you_with_a_reason(A, monkeypatch):
    import submit
    rid = _add(A, url=RECRUITEE)
    monkeypatch.setattr(submit, "driver_for", lambda host: (
        lambda u, d: {"ok": False, "needs_assist": False, "posted": True,
                      "reason": "posting_closed", "detail": "The posting is closed."}))
    c = A.app.test_client()
    r = c.post("/api/review/approve", json={"approve": [rid]}).get_json()["approved"][0]
    assert r["ok"] is False and r["stage"] == "failed" and r["reason"] == "posting_closed"
    ready = c.get("/api/records/ready").get_json()["ready"]
    assert ready[0]["stage"] == "failed" and ready[0]["stage_label"] == "Needs you"
    q = c.get("/api/review/queue").get_json()["pending"]
    assert q[0]["stage"] == "failed"
    board = c.get("/api/tracker/board").get_json()
    assert board["columns"]["approved"][0]["record"]["state"] == "failed"
    assert not _data(A, rid).get("queued")                          # drain() won't re-POST


def test_captcha_drop_is_a_sign_in_reason(A, monkeypatch):
    import submit
    rid = _add(A, url=RECRUITEE)
    monkeypatch.setattr(submit, "driver_for", lambda host: (
        lambda u, d: {"ok": False, "needs_assist": True, "posted": True,
                      "reason": "needs_signin", "detail": "Recruitee asked for a human check."}))
    A.app.test_client().post("/api/review/approve", json={"approve": [rid]})
    st = app_stage(_data(A, rid))
    assert st["stage"] == "failed" and st["reason"] == "needs_signin"


def test_tracker_has_an_approved_column_and_board_moves_follow(A):
    from sourcing.board import STAGES
    assert STAGES.index("saved") < STAGES.index("approved") < STAGES.index("applied")
    c = A.app.test_client()
    rid = _add(A)
    b = c.get("/api/tracker/board").get_json()
    assert b["stages"] == list(STAGES) and len(b["columns"]["saved"]) == 1
    c.post("/api/review/approve", json={"approve": [rid]})
    b = c.get("/api/tracker/board").get_json()
    assert len(b["columns"]["approved"]) == 1 and not b["columns"]["saved"]
    sid = b["columns"]["approved"][0]["source_id"]
    # Dragging back to Saved puts the record back to review, with a clean slate.
    c.post("/api/tracker/move", json={"source_id": sid, "stage": "saved"})
    assert app_stage(_data(A, rid))["stage"] == "queued"
    # Dragging to Approved approves it (no submit).
    c.post("/api/tracker/move", json={"source_id": sid, "stage": "approved"})
    assert app_stage(_data(A, rid))["stage"] == "approved"


# --- template names stay out of titles -----------------------------------------------------------

def test_template_label_is_stripped_from_stored_titles_and_letters(A):
    rid = _add(A, role="Director of Product Management · Classic", data={
        "cover_letter": "I am applying for the Director of Product Management · Classic position."})
    c = A.app.test_client()
    assert c.get("/api/review/queue").get_json()["pending"][0]["role"] == "Director of Product Management"
    pkg = c.get(f"/api/record/{rid}").get_json()
    assert pkg["role"] == "Director of Product Management"
    assert "Classic" not in pkg["cover_letter"]
    c.post("/api/review/approve", json={"approve": [rid]})
    assert c.get("/api/records/ready").get_json()["ready"][0]["role"] == "Director of Product Management"
    title = c.get("/api/tracker/board").get_json()["columns"]["approved"][0]["title"]
    assert "Classic" not in title


def test_strip_keeps_a_real_middle_dot_and_unknown_tags():
    from intake.template_manifest import strip_template_label
    assert strip_template_label("Engineer · Software Engineer") == "Engineer"
    assert strip_template_label("R&D · Platform") == "R&D · Platform"   # not a template
    assert strip_template_label("") == ""


def test_new_records_are_not_titled_with_the_template():
    import inspect

    import ui.session as session
    src = inspect.getsource(session)
    assert 'label += f" · {tlabel}"' not in src


def test_review_shows_no_developer_install_text():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert "pip install" not in (root / "ui" / "static" / "app.js").read_text(encoding="utf-8")


# --- Recruitee: resume attached, answers mapped to question ids ------------------------------------

def _cv(tmp_path):
    p = tmp_path / "cv-9.pdf"
    p.write_bytes(b"%PDF-1.4 the tailored resume")
    return str(p)


QUESTIONS = [
    {"id": 11, "kind": "string", "required": True, "body": "<p>Why do you want to join?</p>"},
    {"id": 12, "kind": "boolean", "required": True, "body": "Are you authorized to work in the US?"},
    {"id": 13, "kind": "single_choice", "required": False, "body": "Preferred office",
     "open_question_options": [{"id": 1, "body": "Berlin"}, {"id": 2, "body": "Remote"}]},
    {"id": 14, "kind": "multi_choice", "required": False, "body": "Languages",
     "open_question_options": [{"id": 3, "body": "Python"}, {"id": 4, "body": "Go"}, {"id": 5, "body": "Rust"}]},
    {"id": 15, "kind": "infobox", "required": False, "body": "Read our privacy notice."},
    {"id": 16, "kind": "file", "required": False, "body": "Portfolio"},
]
SCREENING = [
    {"question": "Why do you want to join?", "answer": "Your robotics work."},
    {"question": "Are you authorized to work in the US?", "answer": "Yes, I am."},
    {"question": "Preferred office", "answer": "Remote"},
    {"question": "Languages", "answer": "Python and Go"},
]


def _rec(tmp_path, screening=SCREENING):
    return {"profile": {"identity": {"name": "Sam Rivera", "email": "sam@x.com", "phone": "555"}},
            "cover_letter": "Hello", "screening": screening, "cv_pdf_path": _cv(tmp_path)}


def _offer(questions=QUESTIONS, status=200):
    def get(url):
        get.url = url
        return status, json.dumps({"offer": {"slug": "eng", "status": "published",
                                             "open_questions": questions}})
    return get


def test_map_open_questions_by_text_and_kind():
    answers, missing = map_open_questions(QUESTIONS, SCREENING)
    assert {"open_question_id": 11, "content": "Your robotics work."} in answers
    assert {"open_question_id": 12, "flag": True} in answers
    assert {"open_question_id": 13, "content": "Remote"} in answers
    assert {"open_question_id": 14, "multi_content": ["Python", "Go"]} in answers
    assert all(a["open_question_id"] not in (15, 16) for a in answers)
    assert missing == []


def test_recruitee_posts_multipart_with_resume_and_answer_ids(tmp_path):
    sent = {}

    def http(url, form):
        sent.update(url=url, form=form)
        return 201, '{"candidate":{"id":7}}'

    get = _offer()
    r = recruitee_submit(RECRUITEE, _rec(tmp_path), http=http, get=get)
    assert r["ok"] and r["posted"]
    assert get.url == "https://acme.recruitee.com/api/offers/eng"
    assert sent["url"] == "https://acme.recruitee.com/api/offers/eng/candidates"
    fields = sent["form"]["fields"]
    f = dict(fields)
    assert f["candidate[name]"] == "Sam Rivera" and f["candidate[email]"] == "sam@x.com"
    assert f["candidate[cover_letter]"] == "Hello"
    ids = [v for k, v in fields if k.endswith("[open_question_id]")]
    assert ids == [11, 12, 13, 14]
    assert ("candidate[open_question_answers_attributes][1][flag]", "true") in fields
    assert ("candidate[open_question_answers_attributes][3][multi_content][]", "Go") in fields
    assert "open_question_answers" not in f                         # never a bare list of strings
    (name, filename, blob, ctype), = sent["form"]["files"]
    assert name == "candidate[cv]" and filename.endswith(".pdf") and ctype == "application/pdf"
    assert blob == b"%PDF-1.4 the tailored resume"


def test_recruitee_required_question_without_answer_is_not_sent(tmp_path):
    posted = []
    r = recruitee_submit(RECRUITEE, _rec(tmp_path, screening=SCREENING[:1]),
                         http=lambda u, f: posted.append(u) or (201, "{}"), get=_offer())
    assert r["ok"] is False and r["reason"] == "form_blocker" and r["posted"] is False
    assert posted == []


def test_recruitee_missing_resume_is_not_sent(tmp_path):
    rec = {**_rec(tmp_path), "cv_pdf_path": str(tmp_path / "nope.pdf")}
    posted = []
    r = recruitee_submit(RECRUITEE, rec, http=lambda u, f: posted.append(u) or (201, "{}"), get=_offer())
    assert r["reason"] == "form_blocker" and posted == []


def test_recruitee_closed_posting_and_errors_have_reasons(tmp_path):
    r = recruitee_submit(RECRUITEE, _rec(tmp_path), http=lambda u, f: (201, "{}"), get=_offer(status=404))
    assert r["reason"] == "posting_closed" and r["posted"] is False
    r = recruitee_submit(RECRUITEE, _rec(tmp_path), http=lambda u, f: (404, ""), get=_offer())
    assert r["reason"] == "posting_closed"
    r = recruitee_submit(RECRUITEE, _rec(tmp_path), get=_offer(),
                         http=lambda u, f: (422, '{"error": ["Phone can\'t be blank"]}'))
    assert r["reason"] == "form_blocker" and "Phone" in r["detail"]
    r = recruitee_submit(RECRUITEE, _rec(tmp_path), http=lambda u, f: (403, "no"), get=_offer())
    assert r["reason"] == "needs_signin"
    r = recruitee_submit(RECRUITEE, _rec(tmp_path), http=lambda u, f: (500, "boom"), get=_offer())
    assert r["reason"] == "site_error"


def test_multipart_encoding_carries_fields_and_the_pdf():
    body, ctype = encode_multipart([("candidate[name]", "Sam")],
                                   [("candidate[cv]", "Sam_resume.pdf", b"%PDF-x", "application/pdf")])
    boundary = ctype.split("boundary=")[1]
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="candidate[name]"\r\n\r\nSam\r\n' in body
    assert b'filename="Sam_resume.pdf"' in body and b"%PDF-x" in body
    assert body.endswith(f"--{boundary}--\r\n".encode())
