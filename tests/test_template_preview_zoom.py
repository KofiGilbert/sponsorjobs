"""The template preview shows the sample at full size. It is an image the engine renders (at a
width the window asks for), never the PDF inside Chromium's viewer, which ignored every fit
option inside a frame and drew the page at three quarters of the frame (2026-10-26)."""
import pathlib

from ui.app import _thumb_width

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_thumb_width_defaults_and_clamps():
    assert _thumb_width(None) == 440 and _thumb_width("abc") == 440
    assert _thumb_width("900") == 900
    assert _thumb_width("50") == 200 and _thumb_width("99999") == 2000


def test_preview_window_and_strip_use_the_rendered_image_not_the_pdf_viewer():
    html = (ROOT / "ui/templates/index.html").read_text()
    assert '<img class="tpl-modal-frame" id="tplModalFrame"' in html
    assert '<iframe class="tpl-modal-frame"' not in html
    js = (ROOT / "ui/static/app.js").read_text()
    body = js[js.index("function openTplPreview"):js.index("function tplStep")]
    assert body.index('$("#tplModal").hidden = false;') < body.index("renderTplModal();")
    assert "/thumb.png?w=" in body and "view=FitH" not in body
    assert '<iframe class="tpl-preview"' not in js and "/thumb.png?w=600" in js


def test_the_preview_page_fits_any_window_and_clears_the_rail():
    # (2026-11-25) The page floats with no box. Its width is the smaller of what the row and the
    # height allow, so it is never cropped or stretched, and in the desktop app the arrows and
    # the close button stay clear of the 42px panel rail.
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / "ui" / "static" / "styles.css").read_text()
    rule = css[css.index(".tpl-modal-paper {"):]
    rule = rule[:rule.index("}")]
    assert "aspect-ratio: 8.5 / 11" in rule and "width: min(calc(100% -" in rule and "100vh" in rule
    assert "height: auto" in rule and "height: 100%;" not in rule.replace("max-height: 100%;", "")
    assert ".in-shell .tpl-modal { padding-right: 66px; }" in css
    assert ".tpl-modal-card { position: relative; flex: 1 1 auto;" in css   # no white box
