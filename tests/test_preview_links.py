"""The CV preview must never carry the app window away: preview-served PDFs get
their external links rerouted through /api/open, which opens the link in the
person's default browser and sends the app window back where it was."""

from __future__ import annotations

import io

import pytest

from pypdf import PdfReader


@pytest.fixture()
def client(monkeypatch):
    from ui import app as webapp
    monkeypatch.setattr(webapp.app, "testing", True)
    return webapp.app.test_client()


def _pdf_with_link(url: str) -> bytes:
    from pypdf import PdfWriter
    from pypdf.annotations import Link
    from pypdf.generic import RectangleObject
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    w.add_annotation(0, Link(rect=RectangleObject((10, 10, 100, 30)), url=url))
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_preview_rewrites_external_links(tmp_path):
    from ui.app import _preview_safe_pdf
    src = tmp_path / "cv.pdf"
    src.write_bytes(_pdf_with_link("https://github.com/KofiGilbert"))
    out = PdfReader(_preview_safe_pdf(src))
    uris = [a.get_object()["/A"]["/URI"] for p in out.pages
            for a in (p.get("/Annots") or [])]
    assert uris and all(u.startswith("/api/open?u=https%3A%2F%2F") for u in uris)


def test_open_endpoint_opens_default_browser_and_returns(client, monkeypatch):
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u) or True)
    r = client.get("/api/open?u=https%3A%2F%2Fgithub.com%2FKofiGilbert")
    assert r.status_code == 200
    assert opened == ["https://github.com/KofiGilbert"]
    body = r.get_data(as_text=True)
    assert "Back to SponsorJobs" in body and "history.back()" in body


def test_open_endpoint_rejects_non_http(client, monkeypatch):
    import webbrowser
    monkeypatch.setattr(webbrowser, "open",
                        lambda u: pytest.fail("must not open non-http"))
    for bad in ("javascript:alert(1)", "file:///etc/passwd", ""):
        r = client.get("/api/open", query_string={"u": bad})
        assert r.status_code == 400
