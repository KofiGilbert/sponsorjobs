"""Cover letter → one-page PDF that matches the CV (Phase 4 package polish).

Reuses the CV template's exact preamble + letterhead and the shared pdflatex toolchain,
so the CV and cover letter are a matched set. Escaping mirrors CV content. Compile tests
are gated on a LaTeX toolchain (@requires_latex), like the assembler tests.
"""

from __future__ import annotations

import io
import types
import zipfile

from conftest import requires_latex
from ui.records import CVRecords

PROFILE = {"identity": {"name": "Maya Rodriguez", "email": "maya@example.com",
                        "phone": "(312) 555-0134",
                        "address": "123 Lakeshore Ave, Chicago, IL 60615",
                        "linkedin": "https://linkedin.com/in/maya"}}
LETTER = ("Dear Hiring Team,\n\nI delivered 40% gains & managed $2M budgets #impact.\n\n"
          "Sincerely,\nMaya Rodriguez")


def test_build_tex_reuses_preamble_and_escapes(template_source):
    from drafting.cover_pdf import build_cover_letter_tex
    tex = build_cover_letter_tex(template_source, PROFILE, LETTER)
    assert "\\documentclass" in tex and "geometry" in tex     # the CV preamble, verbatim
    assert "Maya Rodriguez" in tex                            # same letterhead name as the CV
    assert "\\href{mailto:maya@example.com}" in tex           # same contact rendering
    assert "\\&" in tex and "\\$" in tex and "\\#" in tex     # body special chars escaped
    assert tex.rstrip().endswith("\\end{document}")


def test_cover_letter_docx_endpoint(tmp_path, monkeypatch):
    """The cover letter downloads as an ATS-friendly .docx (no LaTeX needed)."""
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    monkeypatch.setattr(app, "_saved_identity",
                        lambda: {"name": "Maya Rodriguez", "email": "maya@example.com"})
    recs = CVRecords(app.DB_PATH)
    rid = recs.add("Analyst", "Acme", 70, "", data={"cover_letter": LETTER})
    empty = recs.add("PM", "Beta", 60, "", data={})     # no cover letter
    recs.close()
    client = app.app.test_client()

    r = client.get(f"/api/record/{rid}/cover_letter.docx")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml")
    assert r.data[:2] == b"PK"                            # a real .docx (zip)
    assert client.get(f"/api/record/{empty}/cover_letter.docx").status_code == 404


@requires_latex
def test_render_pdf_is_one_page(template_source, workdir):
    from drafting.cover_pdf import build_cover_letter_tex, render_cover_letter_pdf
    from tailoring.compiler import compile_tex
    tex = build_cover_letter_tex(template_source, PROFILE, LETTER)
    res = compile_tex(tex, workdir, jobname="cover-test")
    assert res.ok and res.pages == 1 and res.overfull_count == 0
    p = render_cover_letter_pdf(template_source, PROFILE, LETTER, workdir, "cover-test2")
    assert p is not None and p.exists() and p.stat().st_size > 0


def test_render_returns_none_on_empty_text(template_source, workdir):
    from drafting.cover_pdf import render_cover_letter_pdf
    assert render_cover_letter_pdf(template_source, PROFILE, "   ", workdir, "cover-none") is None


@requires_latex
def test_cover_pdf_endpoint_and_export_include_pdf(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv_build")
    (tmp_path / "cv_build").mkdir()
    monkeypatch.setattr(app, "_saved_full_profile", lambda: PROFILE)
    monkeypatch.setattr(app, "_sponsors", lambda: types.SimpleNamespace(lookup=lambda c: None))
    recs = CVRecords(app.DB_PATH)
    rid = recs.add("Machine Learning Engineer", "Synechron", 70, "",
                   data={"cover_letter": LETTER, "status": "ready", "coverage": {"ratio": 70}})
    recs.close()
    (tmp_path / "cv_build" / f"cv-{rid}.pdf").write_bytes(b"%PDF-1.4 fake")
    client = app.app.test_client()

    # PDF built on demand from the saved letter text.
    r = client.get(f"/api/record/{rid}/cover_letter.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data[:4] == b"%PDF"

    # The export bundle carries the cover letter as BOTH .pdf and .txt.
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/record/{rid}/export").data))
    names = z.namelist()
    assert any(n.endswith("_Cover_Letter.pdf") for n in names)
    assert any(n.endswith("_Cover_Letter.txt") for n in names)
