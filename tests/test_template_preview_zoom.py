"""The template preview window shows the sample at full size: the PDF is loaded only after the
window is on screen, never into a hidden (0 x 0) frame, which left the page at a tiny zoom."""
import pathlib
import re


def test_preview_window_is_shown_before_the_pdf_is_loaded():
    js = (pathlib.Path(__file__).resolve().parents[1] / "ui/static/app.js").read_text()
    body = js[js.index("function openTplPreview"):js.index("function tplStep")]
    assert body.index('$("#tplModal").hidden = false;') < body.index("renderTplModal();")
    assert "requestAnimationFrame" in body and '#tplModalFrame").src = src' in body
    assert not re.search(r'renderTplModal\(\);\s*\n\s*\$\("#tplModal"\)\.hidden = false', body)
