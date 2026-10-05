"""Profile-first: drop a résumé (no JD) and get the whole PROFILE prefilled and saved,
so the My Profile screen reads it back. Runs on the FakeLLM stand-in offline."""
from __future__ import annotations

from docx import Document

import ui.app as app


def _cv_docx(tmp_path, text):
    doc = Document()
    for line in text.splitlines():
        doc.add_paragraph(line)
    p = tmp_path / "resume.docx"
    doc.save(str(p))
    return p


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")   # + pytest → FakeLLM stand-in
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(app, "UPLOADS_DIR", tmp_path / "uploads")


def test_from_cv_builds_and_persists_profile(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    cv = _cv_docx(tmp_path, "Sample Applicant\nsample@example.com\nSoftware Engineer at Acme")
    with open(cv, "rb") as f:
        r = app.app.test_client().post(
            "/api/profile/from_cv", data={"file": (f, "resume.docx")},
            content_type="multipart/form-data")
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] is True
    assert d["summary"]["roles"] >= 1 and d["summary"]["skills"] >= 1

    # Persisted → the My Profile screen reads it back.
    p = app.app.test_client().get("/api/profile").get_json()
    assert p["has_profile"] is True
    assert (p["experience"] or [])[0]["org"]     # experience populated
    assert p["skills"]                            # skills populated
    assert (p["education"] or [])[0]["school"]    # education populated


def test_from_cv_rejects_unsupported_file(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    bad = tmp_path / "notes.txt"
    bad.write_text("hello", encoding="utf-8")
    with open(bad, "rb") as f:
        r = app.app.test_client().post(
            "/api/profile/from_cv", data={"file": (f, "notes.txt")},
            content_type="multipart/form-data")
    assert r.status_code == 400 and "Unsupported" in r.get_json()["error"]


def test_from_cv_requires_a_file(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    r = app.app.test_client().post("/api/profile/from_cv", data={}, content_type="multipart/form-data")
    assert r.status_code == 400


def test_resume_drop_profile_is_reused_when_tailoring(tmp_path, monkeypatch):
    """The payoff: after a résumé drop, starting a New CV recognizes the returning
    user and tailors from the saved profile instead of asking from scratch."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    monkeypatch.setattr(app, "PALACE_DIR", tmp_path / "palace")
    client = app.app.test_client()

    cv = _cv_docx(tmp_path, "Jane Morgan\njane.morgan@example.com\nSoftware Engineer at Acme")
    with open(cv, "rb") as f:
        assert client.post("/api/profile/from_cv", data={"file": (f, "resume.docx")},
                           content_type="multipart/form-data").get_json()["ok"]

    st = client.post("/api/session/start",
                     json={"jd": "Senior Software Engineer. Python and AWS."}).get_json()
    opening = " ".join(st.get("messages") or [])
    assert "Welcome back" in opening          # reused — not re-asked from scratch
    assert "Jane" in opening                   # the name pulled from the résumé
    assert st.get("returning") is True
    assert st.get("can_autobuild") is True     # → the one-click "build it for me" button shows


def test_essentials_flatten_from_profile():
    prof = {"identity": {"name": "A"}, "education": [{"school": "S"}],
            "experience": [{"org": "Acme", "location": "NY",
                            "roles": [{"title": "Eng", "dates": "2020", "bullets": ["did x"]},
                                      {"title": "Sr Eng", "dates": "2022", "bullets": ["did y"]}]}]}
    ess = app._essentials_from_profile(prof)
    assert ess["identity"]["name"] == "A"
    assert len(ess["experience"]) == 2                       # one flat entry per role
    assert ess["experience"][0] == {"org": "Acme", "location": "NY", "title": "Eng",
                                    "dates": "2020", "bullets": ["did x"]}
