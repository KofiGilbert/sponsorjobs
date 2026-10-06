"""PDFium is not thread-safe. The Resumes page asks for every template thumbnail at once, and in
the 0.1.0 installer five parallel renders crashed the whole engine (blank templates, then a dead
app). Every PDFium call now holds tailoring.preview.PDFIUM_LOCK. A real crash needs heavy,
text-rich PDFs and luck, so this test measures the thing that matters instead: it slows each
document open down and asserts no two renders are ever inside PDFium at the same time."""

import threading
import time

import pytest

pdfium = pytest.importorskip("pypdfium2")

from tailoring import preview  # noqa: E402


def _pdf(path):
    doc = pdfium.PdfDocument.new()
    doc.new_page(612, 792)
    doc.save(str(path))
    doc.close()
    return path


def test_thumbnails_requested_at_once_never_overlap_inside_pdfium(tmp_path, monkeypatch):
    real = pdfium.PdfDocument
    state = {"now": 0, "most": 0}
    guard = threading.Lock()

    class Tracked:
        def __init__(self, *a, **kw):
            with guard:
                state["now"] += 1
                state["most"] = max(state["most"], state["now"])
            time.sleep(0.05)                     # widen the window a race would need
            self._doc = real(*a, **kw)

        def __getitem__(self, i):
            return self._doc[i]

        def close(self):
            self._doc.close()
            with guard:
                state["now"] -= 1

    pdfs = [_pdf(tmp_path / f"t{i}.pdf") for i in range(6)]
    monkeypatch.setattr(pdfium, "PdfDocument", Tracked)
    results = {}

    def render(i, pdf):
        results[i] = preview.render_thumbnail_png(pdf, tmp_path / f"t{i}.png", width=120, dpi=72)

    threads = [threading.Thread(target=render, args=(i, p)) for i, p in enumerate(pdfs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 6 and all(results.values())
    assert state["most"] == 1                    # one render at a time, always
