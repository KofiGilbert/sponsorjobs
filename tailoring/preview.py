"""Render a CV PDF's first page to a PNG for at-a-glance review (e.g. inline in Telegram).

Best-effort and dependency-light: the app already requires a TeX toolchain to compile the
CV, and both MiKTeX and TeX Live ship a PDF→image tool right next to ``pdflatex``
(``pdftocairo`` / Ghostscript), so we reuse that. First choice, though, is ``pypdfium2``:
it's a core dependency (already installed everywhere the app runs) and renders in-process
with no subprocess and no system binary, so thumbnails work out of the box even on a bare
install. PyMuPDF is tried only if it happens to be present (it's AGPL, so never a declared
dependency). If nothing can render, we return "" and the caller simply sends the PDF without
an inline image — the feature degrades, it never breaks.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .compiler import no_window_kwargs


def render_first_page_png(pdf_path, png_path, dpi: int = 150) -> str:
    """Render page 1 of ``pdf_path`` to ``png_path``. Returns the PNG path, or "" on failure."""
    pdf_path, png_path = Path(pdf_path), Path(png_path)
    if not pdf_path.exists():
        return ""
    png_path.parent.mkdir(parents=True, exist_ok=True)

    if _render_pdfium(pdf_path, png_path, dpi):
        return str(png_path)
    if _render_pymupdf(pdf_path, png_path, dpi):
        return str(png_path)
    if _render_pdftocairo(pdf_path, png_path, dpi):
        return str(png_path)
    if _render_ghostscript(pdf_path, png_path, dpi):
        return str(png_path)
    return ""


def render_thumbnail_png(pdf_path, png_path, width: int = 440, dpi: int = 220) -> str:
    """Render page 1 as a CRISP small thumbnail. We rasterize the page at high
    resolution, then Lanczos-downscale to ``width`` px. That collapses dense body
    text into clean grey lines like a real document miniature, instead of the
    speckly "sand on paper" you get when the browser shrinks a full-size page image
    (browser downscaling of razor-sharp black text aliases into noise; Lanczos
    pre-smooths it so nothing is left to alias). Falls back to the high-res PNG if
    Pillow isn't available. Returns the PNG path, or "" on failure."""
    pdf_path, png_path = Path(pdf_path), Path(png_path)
    hi = png_path.with_name(png_path.stem + "-hi.png")
    if not render_first_page_png(pdf_path, hi, dpi=dpi):
        return ""
    try:
        from PIL import Image
        with Image.open(hi) as im:
            im = im.convert("RGB")
            h = max(1, round(width * im.height / im.width))
            im.resize((width, h), Image.LANCZOS).save(png_path, optimize=True)
        hi.unlink(missing_ok=True)
    except Exception:
        # Pillow missing/failed: keep the high-res PNG as the thumbnail (still fine).
        if hi.exists() and hi != png_path:
            hi.replace(png_path)
    return str(png_path) if png_path.exists() else ""


def _render_pdfium(pdf_path: Path, png_path: Path, dpi: int) -> bool:
    """Render page 1 with pypdfium2 (a core dependency; Google's PDFium under a permissive
    BSD/Apache license). In-process, no subprocess, no system tool — so this is the path that
    makes thumbnails work on a plain install."""
    try:
        import pypdfium2 as pdfium
    except Exception:
        return False
    doc = None
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
        doc[0].render(scale=dpi / 72.0).to_pil().save(str(png_path))
        return png_path.exists() and png_path.stat().st_size > 0
    except Exception:
        return False
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


def _render_pymupdf(pdf_path: Path, png_path: Path, dpi: int) -> bool:
    try:
        import fitz  # PyMuPDF, optional
    except Exception:
        return False
    try:
        doc = fitz.open(str(pdf_path))
        try:
            doc.load_page(0).get_pixmap(dpi=dpi).save(str(png_path))
        finally:
            doc.close()
        return png_path.exists() and png_path.stat().st_size > 0
    except Exception:
        return False


def _render_pdftocairo(pdf_path: Path, png_path: Path, dpi: int) -> bool:
    exe = _find_exe(("miktex-pdftocairo", "pdftocairo"))
    if not exe:
        return False
    # pdftocairo appends ".png" to the out-base with -singlefile, so pass the stem.
    base = png_path.with_suffix("")
    try:
        subprocess.run([exe, "-png", "-singlefile", "-r", str(dpi), "-f", "1", "-l", "1",
                        str(pdf_path), str(base)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                       check=False, **no_window_kwargs())
    except (OSError, subprocess.SubprocessError):
        return False
    produced = base.with_suffix(".png")
    if produced != png_path and produced.exists():
        produced.replace(png_path)
    return png_path.exists() and png_path.stat().st_size > 0


def _render_ghostscript(pdf_path: Path, png_path: Path, dpi: int) -> bool:
    exe = _find_exe(("gswin64c", "gswin32c", "gs", "mgs"))
    if not exe:
        return False
    try:
        subprocess.run([exe, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=png16m",
                        "-r" + str(dpi), "-dFirstPage=1", "-dLastPage=1",
                        "-sOutputFile=" + str(png_path), str(pdf_path)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                       check=False, **no_window_kwargs())
    except (OSError, subprocess.SubprocessError):
        return False
    return png_path.exists() and png_path.stat().st_size > 0


def _find_exe(names) -> str:
    """Locate a renderer on PATH or next to the TeX engine (MiKTeX/TeX Live bin dir)."""
    tex_bin = None
    try:
        from tailoring.compiler import find_pdflatex
        tex_bin = Path(find_pdflatex()).parent
    except Exception:
        tex_bin = None
    for name in names:
        hit = shutil.which(name)
        if hit:
            return hit
        if tex_bin:
            for cand in (tex_bin / name, tex_bin / (name + ".exe")):
                if cand.exists():
                    return str(cand)
    return ""
