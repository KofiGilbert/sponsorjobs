"""Extract profile facts from an uploaded CV — fully local (CLAUDE.md §4a, §11).

Supports Word (.docx), PowerPoint (.pptx), PDF (text-based AND scanned/image-only),
and image files (.png/.jpg/…). Text formats are read directly; scanned PDFs and
images are read with OCR so a photographed or exported CV is still usable — the
user uploads and does nothing else.

Everything runs on the machine:
  * python-docx (MIT) / python-pptx (MIT) / pypdf (BSD) — text formats,
  * pypdfium2 (Apache-2.0, bundles the PDFium binary) — rasterize scanned PDFs,
  * rapidocr-onnxruntime (Apache-2.0, Apache-2.0 PP-OCR models bundled in the
    wheel) on the onnxruntime we already ship — OCR, no system binary, no download.

Extraction never raises: if a file can't be read or OCR comes back garbled, it
returns the best text it got (possibly empty) and the intake gate then asks
follow-up questions instead of failing.
"""

from __future__ import annotations

from pathlib import Path

TEXT_EXTS = {".docx", ".pptx", ".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
SUPPORTED_EXTS = TEXT_EXTS | IMAGE_EXTS

# Below this many non-whitespace characters, a PDF is treated as scanned and
# sent through OCR rather than trusted as extractable text.
_SCANNED_TEXT_THRESHOLD = 30
_MAX_OCR_PAGES = 15          # a résumé is 1-2 pages; cap OCR work on a huge/hostile scan

_OCR = None          # lazy RapidOCR singleton
_OCR_TRIED = False


def ocr_available() -> bool:
    return _get_ocr() is not None


def _get_ocr():
    global _OCR, _OCR_TRIED
    if not _OCR_TRIED:
        _OCR_TRIED = True
        try:
            from rapidocr_onnxruntime import RapidOCR
            _OCR = RapidOCR()
        except Exception:
            _OCR = None
    return _OCR


def _ocr_ndarray(arr) -> str:
    ocr = _get_ocr()
    if ocr is None:
        return ""
    try:
        result, _ = ocr(arr)
    except Exception:
        return ""
    if not result:
        return ""
    # result rows are [box, text, score]; keep readable order, drop empties.
    return "\n".join(str(r[1]).strip() for r in result if len(r) > 1 and str(r[1]).strip())


# ------------------------------------------------------------------ per-format
def _from_docx(path: Path) -> str:
    import docx
    doc = docx.Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _from_pptx(path: Path) -> str:
    from pptx import Presentation
    prs = Presentation(str(path))
    parts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs).strip()
                    if line:
                        parts.append(line)
    return "\n".join(parts)


def _from_image(path: Path) -> str:
    """OCR an image file directly (RapidOCR reads the path)."""
    ocr = _get_ocr()
    if ocr is None:
        return ""
    try:
        result, _ = ocr(str(path))
    except Exception:
        return ""
    if not result:
        return ""
    return "\n".join(str(r[1]).strip() for r in result if len(r) > 1 and str(r[1]).strip())


def _ocr_pdf(path: Path) -> str:
    """Rasterize each PDF page with pypdfium2 and OCR it (for scanned PDFs)."""
    if _get_ocr() is None:
        return ""
    try:
        import numpy as np
        import pypdfium2 as pdfium
    except Exception:
        return ""
    from tailoring.preview import PDFIUM_LOCK          # PDFium is not thread-safe
    out = []
    with PDFIUM_LOCK:
        try:
            pdf = pdfium.PdfDocument(str(path))
        except Exception:
            return ""
        try:
            # Cap the OCR work: a résumé is a page or two, so a huge/many-page scan (or a hostile
            # upload) can't tie the machine up rasterizing+OCR-ing hundreds of pages.
            for i in range(min(len(pdf), _MAX_OCR_PAGES)):
                try:
                    page = pdf[i]
                    pil = page.render(scale=2.5).to_pil().convert("RGB")
                    text = _ocr_ndarray(np.asarray(pil))
                    if text:
                        out.append(text)
                except Exception:
                    continue
        finally:
            try:
                pdf.close()
            except Exception:
                pass
    return "\n".join(out)


def _from_pdf(path: Path) -> str:
    text = ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        text = ""
    # Image-only / scanned PDF → OCR.
    if len(text.replace(" ", "").strip()) < _SCANNED_TEXT_THRESHOLD:
        ocr_text = _ocr_pdf(path)
        if len(ocr_text.strip()) > len(text.strip()):
            return ocr_text
    return text


# ------------------------------------------------------------------ public API
def extract_text(path) -> str:
    """Best-effort local text extraction; never raises."""
    path = Path(path)
    ext = path.suffix.lower()
    try:
        if ext == ".docx":
            return _from_docx(path)
        if ext == ".pptx":
            return _from_pptx(path)
        if ext == ".pdf":
            return _from_pdf(path)
        if ext in IMAGE_EXTS:
            return _from_image(path)
    except Exception:
        return ""
    return ""


def extract_profile(path, llm, known: dict | None = None) -> dict:
    """Extract text from the file, then let the intake extractor turn it into the
    structured essentials (identity/education/experience). Returns the essentials
    dict — the caller merges it into the profile/memory. Never raises."""
    known = known or {"identity": {}, "education": [], "experience": []}
    text = extract_text(path)
    if not text.strip():
        return {"identity": dict(known.get("identity", {})),
                "education": list(known.get("education", [])),
                "experience": list(known.get("experience", []))}
    try:
        out = llm.extract_intake("", known, [{"role": "user", "content": text}])
        ess = out.get("essentials") or known
    except Exception:
        ess = known
    for k in ("identity", "education", "experience"):
        ess.setdefault(k, {} if k == "identity" else [])
    return ess


def summarize_import(essentials: dict, filename: str, when: str) -> str:
    """A CLEAN, tool-authored prose summary of the extracted facts — this is what
    goes into MemPalace. Never the raw/OCR'd text (which can be garbled)."""
    ident = essentials.get("identity", {}) or {}
    bits = [f"User uploaded a resume ({filename}) on {when}."]
    if ident.get("name"):
        bits.append(f"Name: {ident['name']}.")
    roles = []
    for j in essentials.get("experience", []):
        title, org = (j.get("title") or "").strip(), (j.get("org") or "").strip()
        dates = (j.get("dates") or "").strip()
        if title or org:
            roles.append(" ".join(p for p in [title, f"at {org}" if org else "",
                                              f"({dates})" if dates else ""] if p).strip())
    if roles:
        bits.append("Roles: " + "; ".join(roles) + ".")
    schools = []
    for d in essentials.get("education", []):
        deg, sch = (d.get("degree") or "").strip(), (d.get("school") or "").strip()
        date = (d.get("date") or "").strip()
        if deg or sch:
            schools.append(" ".join(p for p in [deg, f"from {sch}" if sch else "",
                                                f"({date})" if date else ""] if p).strip())
    if schools:
        bits.append("Education: " + "; ".join(schools) + ".")
    return " ".join(bits)
