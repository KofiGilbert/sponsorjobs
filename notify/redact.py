"""The CV preview image sent to Telegram, with the contact line covered by default.

Telegram bot chats are cloud chats (stored on Telegram's servers, not end to end
encrypted), so the picture of a CV that goes there hides the address / phone / email
line behind a solid bar unless the person turns on "Show my contact details in Telegram
previews". The name stays visible so the person knows whose CV it is.

Locating the line: pypdfium2 (already a core dependency, see tailoring/preview.py) gives
the text boxes of the page. We search for the person's own address, phone and email
strings and cover the band they sit in across the full page width. If none of them can
be found but the person has contact details, we fall back to the first text line under
the name, and if even that fails, a fixed band near the top: the bar fails closed.
"""

from __future__ import annotations

from pathlib import Path

BAR_RGB = (17, 17, 17)
PAD_PT = 2.5


def _contact_strings(identity: dict) -> list:
    out = []
    for key in ("email", "phone", "address"):
        v = str((identity or {}).get(key) or "").strip()
        if v:
            out.append(v)
            if key == "phone":
                digits = "".join(ch for ch in v if ch.isdigit())
                if len(digits) >= 4:
                    out.append(digits[-4:])
            if key == "email" and "@" in v:
                out.append(v.split("@", 1)[0])
    for key in ("linkedin", "github", "website", "blog"):
        if (identity or {}).get(key):
            out.append({"linkedin": "In", "github": "GitHub", "website": "Website",
                        "blog": "Blog"}[key])
    return out


def _boxes_for(textpage, needle: str) -> list:
    boxes = []
    try:
        searcher = textpage.search(needle, match_case=False, match_whole_word=False)
    except TypeError:
        searcher = textpage.search(needle)
    try:
        for _ in range(4):
            hit = searcher.get_next()
            if not hit:
                break
            start, count = hit
            for i in range(start, start + count):
                try:
                    boxes.append(textpage.get_charbox(i))
                except Exception:   # noqa: BLE001
                    pass
    finally:
        try:
            searcher.close()
        except Exception:   # noqa: BLE001
            pass
    return boxes


def _lines(textpage) -> list:
    """Text rects grouped into lines, top of page first: [(bottom, top)]."""
    rects = []
    try:
        n = textpage.count_rects()
        rects = [textpage.get_rect(i) for i in range(n)]
    except Exception:   # noqa: BLE001
        return []
    lines: list = []
    for (_l, b, _r, t) in sorted(rects, key=lambda r: -r[3]):
        for ln in lines:
            if abs(ln[1] - t) < 2.0 or (b < ln[1] and t > ln[0] and min(t, ln[1]) - max(b, ln[0]) > 2):
                ln[0], ln[1] = min(ln[0], b), max(ln[1], t)
                break
        else:
            lines.append([b, t])
    return sorted(lines, key=lambda ln: -ln[1])


def contact_band(pdf_path, identity: dict):
    """(bottom, top) in PDF points of the contact line on page 1, and the page height.
    Returns (None, height) when the person has no contact details at all."""
    import pypdfium2 as pdfium

    from tailoring.preview import PDFIUM_LOCK          # PDFium is not thread-safe
    with PDFIUM_LOCK:
        return _contact_band(pdfium.PdfDocument(str(pdf_path)), identity)


def _contact_band(doc, identity: dict):
    try:
        page = doc[0]
        _w, h = page.get_size()
        needles = _contact_strings(identity)
        if not needles:
            return None, h
        tp = page.get_textpage()
        boxes = []
        for s in needles:
            boxes.extend(_boxes_for(tp, s))
        # Only boxes in the top third: a skill or a bullet that happens to share a string
        # with the email must not move the bar into the body.
        boxes = [bx for bx in boxes if bx[3] > h * 0.66]
        if boxes:
            return (min(b[1] for b in boxes) - PAD_PT, max(b[3] for b in boxes) + PAD_PT), h
        lines = _lines(tp)
        if len(lines) >= 2:
            b, t = lines[1]
            return (b - PAD_PT, t + PAD_PT), h
        return (h * 0.86, h * 0.94), h                    # fail closed: a fixed top band
    finally:
        doc.close()


def render_preview(pdf_path, png_path, identity: dict, redact: bool = True,
                   dpi: int = 150) -> dict:
    """Render page 1 of ``pdf_path`` to ``png_path`` and (by default) cover the contact line.
    Returns {"path", "redacted", "band": (y0, y1) in pixels or None}; path "" on failure."""
    import pypdfium2 as pdfium
    pdf_path, png_path = Path(pdf_path), Path(png_path)
    if not pdf_path.exists():
        return {"path": "", "redacted": False, "band": None}
    png_path.parent.mkdir(parents=True, exist_ok=True)
    scale = dpi / 72.0
    from tailoring.preview import PDFIUM_LOCK          # PDFium is not thread-safe
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(pdf_path))
        try:
            img = doc[0].render(scale=scale).to_pil().convert("RGB")
        finally:
            doc.close()
        band = h = None
        if redact:
            band, h = contact_band(pdf_path, identity)
    band_px = None
    if redact:
        if band is not None:
            from PIL import ImageDraw
            bottom, top = band
            y0 = max(0, int((h - top) * scale))
            y1 = min(img.height, int((h - bottom) * scale) + 1)
            ImageDraw.Draw(img).rectangle([0, y0, img.width, y1], fill=BAR_RGB)
            band_px = (y0, y1)
    img.save(str(png_path), optimize=True)
    return {"path": str(png_path), "redacted": band_px is not None, "band": band_px}
