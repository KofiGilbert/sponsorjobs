"""Files that leave the app are named after the person, never the internal storage name.

Tailored files live as WORKDIR/cv-<id>.pdf and cover-<id>.pdf. An employer must instead
receive "<Name> Resume.pdf" / "<Name> Cover Letter.pdf" on every path out: the Recruitee
API submit, the in-app fill's CDP upload (which sends the path's basename), and downloads.
"""
from __future__ import annotations

import json
import os
import time
import types
from pathlib import Path

from submit.drivers import recruitee_submit
from submit.filenames import export_filename, safe_person_name, upload_copy
from ui.records import CVRecords


# --- helper ------------------------------------------------------------------ #

def test_export_filename_from_name():
    assert export_filename("Kofi Gilbert") == "Kofi Gilbert Resume.pdf"
    assert export_filename("Kofi Gilbert", "cover") == "Kofi Gilbert Cover Letter.pdf"
    assert export_filename("Kofi Gilbert", "resume", "docx") == "Kofi Gilbert Resume.docx"
    assert export_filename("José Núñez", "cover", ".txt") == "José Núñez Cover Letter.txt"


def test_export_filename_fallback_when_no_name():
    assert export_filename("") == "Resume.pdf"
    assert export_filename(None) == "Resume.pdf"
    assert export_filename("  / \\ \n ") == "Resume.pdf"
    assert export_filename("", "cover") == "Cover Letter.pdf"


def test_safe_person_name_strips_separators_and_control_chars():
    assert safe_person_name("../../etc/passwd") == "etc passwd"
    assert safe_person_name('Ann\x00 "Q" <Lee>\t\tSmith') == "Ann Q Lee Smith"
    assert safe_person_name("  Kofi    Gilbert  ") == "Kofi Gilbert"
    assert safe_person_name("C:\\Users\\x") == "C Users x"
    assert len(safe_person_name("A" * 500)) <= 80
    name = export_filename("a/b\\c:d")
    assert "/" not in name and "\\" not in name and ":" not in name


def test_upload_copy_names_refreshes_and_keeps_one(tmp_path):
    src = tmp_path / "cv-7.pdf"
    src.write_bytes(b"%PDF-1 v1")
    d = tmp_path / "uploads" / "7"
    p = upload_copy(src, d, "Kofi Gilbert Resume.pdf")
    assert p.name == "Kofi Gilbert Resume.pdf" and p.read_bytes() == b"%PDF-1 v1"
    assert src.exists()                                     # storage name untouched
    later = time.time() + 5
    src.write_bytes(b"%PDF-1 v2 longer")
    os.utime(src, (later, later))
    assert upload_copy(src, d, "Kofi Gilbert Resume.pdf").read_bytes() == b"%PDF-1 v2 longer"
    upload_copy(src, d, "Kofi A Gilbert Resume.pdf")        # renamed -> old copy removed
    assert [x.name for x in d.iterdir()] == ["Kofi A Gilbert Resume.pdf"]


# --- Recruitee API submit ---------------------------------------------------- #

def test_recruitee_multipart_filename_is_the_persons_name(tmp_path):
    cv = tmp_path / "cv-41.pdf"
    cv.write_bytes(b"%PDF-1.4 resume")
    sent = {}

    def http(url, form):
        sent["files"] = form["files"]
        return 201, '{"candidate":{"id":1}}'

    def offer(url):
        return 200, json.dumps({"offer": {"slug": "eng", "status": "published",
                                          "open_questions": []}})

    rec = {"profile": {"identity": {"name": "Kofi Gilbert", "email": "k@x.com"}},
           "cv_pdf_path": str(cv)}
    r = recruitee_submit("https://acme.recruitee.com/o/eng", rec, http=http, get=offer)
    assert r["ok"] is True
    field, filename, data, ctype = sent["files"][0]
    assert field == "candidate[cv]" and filename == "Kofi Gilbert Resume.pdf"
    assert data == b"%PDF-1.4 resume" and ctype == "application/pdf"


# --- engine endpoints -------------------------------------------------------- #

def _app(tmp_path, monkeypatch, name="Kofi Gilbert"):
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv_build")
    (tmp_path / "cv_build").mkdir()
    monkeypatch.setattr(app, "_sponsors", lambda: types.SimpleNamespace(lookup=lambda c: None))
    monkeypatch.setattr(app, "_saved_identity", lambda: {"name": name} if name else {})
    recs = CVRecords(app.DB_PATH)
    rid = recs.add("Engineer", "Acme", 70, "", data={"status": "ready",
                                                     "cover_letter": "Dear team"})
    recs.close()
    (app.WORKDIR / f"cv-{rid}.pdf").write_bytes(b"%PDF-1.4 fake")
    return app, rid


def test_cv_download_content_disposition_uses_name(tmp_path, monkeypatch):
    app, rid = _app(tmp_path, monkeypatch)
    r = app.app.test_client().get(f"/api/cv.pdf?job=cv-{rid}")
    assert r.status_code == 200
    cd = r.headers["Content-Disposition"]
    assert "Kofi Gilbert Resume.pdf" in cd and f"cv-{rid}" not in cd


def test_cv_download_prefers_the_name_on_the_tailored_cv(tmp_path, monkeypatch):
    app, rid = _app(tmp_path, monkeypatch, name="Saved Name")
    recs = CVRecords(app.DB_PATH)
    recs.merge_data(rid, {"render_profile": {"identity": {"name": "Kofi Gilbert"}}})
    recs.close()
    cd = app.app.test_client().get(f"/api/cv.pdf?job=cv-{rid}").headers["Content-Disposition"]
    assert "Kofi Gilbert Resume.pdf" in cd


def test_cv_download_falls_back_to_resume_pdf(tmp_path, monkeypatch):
    app, rid = _app(tmp_path, monkeypatch, name="")
    cd = app.app.test_client().get(f"/api/cv.pdf?job=cv-{rid}").headers["Content-Disposition"]
    assert 'filename=Resume.pdf' in cd or 'filename="Resume.pdf"' in cd


def test_cover_letter_txt_download_uses_name(tmp_path, monkeypatch):
    app, rid = _app(tmp_path, monkeypatch)
    (app.WORKDIR / f"cover-{rid}.txt").write_text("Dear team", encoding="utf-8")
    cd = app.app.test_client().get(
        f"/api/cover_letter.txt?job=cover-{rid}").headers["Content-Disposition"]
    assert "Kofi Gilbert Cover Letter.txt" in cd


def test_cover_pdf_download_uses_name(tmp_path, monkeypatch):
    app, rid = _app(tmp_path, monkeypatch)
    (app.WORKDIR / f"cover-{rid}.pdf").write_bytes(b"%PDF-1.4 cover")
    r = app.app.test_client().get(f"/api/record/{rid}/cover_letter.pdf")
    assert r.status_code == 200
    assert "Kofi Gilbert Cover Letter.pdf" in r.headers["Content-Disposition"]


def test_in_app_upload_path_is_a_named_copy(tmp_path, monkeypatch):
    """main.js uploads the path it is handed (DOM.setFileInputFiles sends its basename), so
    the engine hands it a copy named after the person; the stored cv-<id>.pdf stays."""
    app, rid = _app(tmp_path, monkeypatch)
    rec = app._get_record(rid)
    p = Path(app._resume_upload_path(rec))
    assert p.name == "Kofi Gilbert Resume.pdf"
    assert p.parent == app.WORKDIR / "uploads" / str(rid)
    assert p.read_bytes() == b"%PDF-1.4 fake"
    assert (app.WORKDIR / f"cv-{rid}.pdf").exists()


def test_autoapply_plan_upload_op_carries_named_path(tmp_path, monkeypatch):
    app, rid = _app(tmp_path, monkeypatch)
    import autoapply
    from autoapply.fields import FormField  # noqa: F401
    monkeypatch.setattr(app, "_make_llm", lambda: None)
    monkeypatch.setattr(autoapply, "build_fill_plan",
                        lambda fields, llm, **k: {"f1": "resume"})
    seen = {}

    def fake_resolve(plan, applicant, resume_path=""):
        seen["path"] = resume_path
        return []
    monkeypatch.setattr(autoapply, "resolve_plan", fake_resolve)
    r = app.app.test_client().post("/api/autoapply/plan", json={
        "url": "https://jobs.example.com/apply", "record_id": rid,
        "fields": [{"ref": "f1", "label": "Resume", "type": "file"}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert Path(seen["path"]).name == "Kofi Gilbert Resume.pdf"


def test_record_delete_removes_the_named_copy(tmp_path, monkeypatch):
    app, rid = _app(tmp_path, monkeypatch)
    app._resume_upload_path(app._get_record(rid))
    assert (app.WORKDIR / "uploads" / str(rid)).exists()
    assert app.app.test_client().post(f"/api/record/{rid}/delete").status_code == 200
    assert not (app.WORKDIR / "uploads" / str(rid)).exists()
