"""PDFium is not thread-safe and Flask serves on several threads: every module that opens a PDF
with pypdfium2 must hold tailoring.preview.PDFIUM_LOCK. Two unlocked reads segfaulted the engine
(2026-10-06 thumbnails; 2026-11-16 the ATS text-layer check)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {"packaging"}          # build-time scripts, one thread


def test_every_pdfium_user_holds_the_lock():
    bad = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if rel.parts[0] in SKIP | {"tests", ".venv", "node_modules"} or ".claude" in rel.parts:
            continue
        src = p.read_text(encoding="utf-8", errors="ignore")
        if "PdfDocument(" in src and "PDFIUM_LOCK" not in src:
            bad.append(str(rel))
    assert not bad, f"pypdfium2 used without PDFIUM_LOCK in: {bad}"
