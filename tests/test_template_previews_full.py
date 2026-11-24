"""Every template preview in the Resumes gallery is a complete, full one-page resume.

Kofi, 2026-11-24: "make sure all resume templates are complete pages and none are half page,
3/4 or unfinished." Educator, MBA, PhD, Banking, Consulting and Nursing rendered at 65 to 75
percent of the page and the default template at 91 percent, because the shared John Doe sample
did not fit their sections. Each now carries its own sample in its manifest's "preview" dict.

Fill is measured the way the gallery shows it: the lowest text on page 1 as a share of the page
height (a full page reaches about 94 to 97 percent; the builder never lets it pass 97).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ui import app as A

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = sorted(p.stem for p in (ROOT / "config" / "templates").glob("*.json"))
FILL_FLOOR = 0.90

pytestmark = pytest.mark.skipif(shutil.which("pdflatex") is None, reason="needs pdflatex")


@pytest.fixture(scope="module")
def previews(tmp_path_factory):
    """Render every template's preview once, into a throwaway build folder."""
    import pypdfium2 as pdfium

    from tailoring.preview import PDFIUM_LOCK
    work = tmp_path_factory.mktemp("previews")
    old = A.WORKDIR
    A.WORKDIR = work
    try:
        out = {}
        for name in TEMPLATES:
            pdf = A._template_preview_pdf(name)
            assert pdf is not None and pdf.exists(), f"{name}: no preview PDF"
            with PDFIUM_LOCK:
                doc = pdfium.PdfDocument(str(pdf))
                try:
                    page = doc[0]
                    height = page.get_height()
                    text = page.get_textpage()
                    lowest = min(text.get_rect(i)[1] for i in range(text.count_rects()))
                    pages = len(doc)
                finally:
                    doc.close()
            log = pdf.with_suffix(".log")
            log_text = log.read_text(encoding="utf-8", errors="ignore") if log.exists() else None
            out[name] = {"pages": pages, "fill": (height - lowest) / height, "log": log_text}
        yield out
    finally:
        A.WORKDIR = old


def test_there_are_templates_to_check():
    assert len(TEMPLATES) >= 16


@pytest.mark.parametrize("name", TEMPLATES)
def test_preview_is_one_full_page(previews, name):
    p = previews[name]
    assert p["pages"] == 1, f"{name}: preview runs to {p['pages']} pages"
    assert p["fill"] >= FILL_FLOOR, (
        f"{name}: preview fills only {p['fill']:.1%} of the page; add sample content to its "
        f"manifest's \"preview\" (more bullets on recent roles first)")


@pytest.mark.parametrize("name", TEMPLATES)
def test_preview_has_no_overfull_lines(previews, name):
    log = previews[name]["log"]
    if log is not None:
        assert "Overfull \\hbox" not in log, f"{name}: preview has an Overfull hbox"


@pytest.mark.parametrize("name", TEMPLATES)
def test_preview_sample_is_generic(name):
    """The sample is John Doe with made-up places, and no dashes the person would read."""
    man = json.loads((ROOT / "config" / "templates" / f"{name}.json").read_text(encoding="utf-8"))
    blob = json.dumps(man.get("preview") or {}, ensure_ascii=False)
    assert "—" not in blob and "–" not in blob
    assert not any(w in blob for w in (" CV ", " CVs ", "CV.", "CV,"))
