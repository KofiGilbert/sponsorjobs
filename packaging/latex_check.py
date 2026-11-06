r"""Compile every resume template and prove the result, optionally against a reference LaTeX.

Two jobs, one script:

* ``--pdflatex PATH`` alone: build every template's preview (the same "John Doe" sample and
  the same one-page fitting path the app's Resumes gallery uses, ``ui.app._template_preview_pdf``)
  with that pdflatex, and fail unless every one compiles to exactly ONE page. This is the CI
  check for the bundled TinyTeX (.github/workflows/latex-bundle.yml).

* ``--pdflatex PATH --reference REF``: also build every template with the reference pdflatex
  (on the founder's Mac, Homebrew TeX Live) and compare the two PDFs: page count, extracted
  text, a pixel diff of the rendered page, and the final .tex the fitting loop settled on. Any
  difference fails. This is how "the bundle makes the same resumes as today" is proven, and
  tests/test_bundled_latex.py runs it when a TinyTeX is present.

Same compile flags as the app: it goes through tailoring.compiler.compile_tex, with the engine
chosen by the PDFLATEX env var (which find_pdflatex honours first).

    python packaging/latex_check.py --pdflatex .scratch/tinytex/TinyTeX/bin/universal-darwin/pdflatex \
        --reference /opt/homebrew/bin/pdflatex --out .scratch/latex-check
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _template_names() -> list[str]:
    names = []
    for p in sorted((ROOT / "config" / "templates").glob("*.json")):
        try:
            names.append(json.loads(p.read_text(encoding="utf-8")).get("name") or p.stem)
        except (OSError, ValueError):
            continue
    return names


def _build_all(app, pdflatex: str | None, outdir: Path, names: list[str]) -> dict[str, Path | None]:
    """Build every template's preview PDF with ``pdflatex`` (None: whatever the app's own
    find_pdflatex picks) into ``outdir``."""
    if pdflatex:
        # abspath, not resolve(): pdflatex may be a symlink to pdftex, which picks its
        # format by the name it was called with.
        os.environ["PDFLATEX"] = os.path.abspath(pdflatex)
    else:
        os.environ.pop("PDFLATEX", None)
    outdir.mkdir(parents=True, exist_ok=True)
    app.WORKDIR = outdir
    return {n: app._template_preview_pdf(n) for n in names}


def _pages(pdf: Path) -> int:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf))
    try:
        return len(doc)
    finally:
        doc.close()


def _text(pdf: Path) -> str:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf))
    try:
        return "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    finally:
        doc.close()


def _pixel_diff(a: Path, b: Path, dpi: int = 150) -> tuple[int, int]:
    """(differing pixels, total pixels) of page 1 rendered at ``dpi`` with PDFium."""
    import pypdfium2 as pdfium

    def render(p):
        doc = pdfium.PdfDocument(str(p))
        try:
            return doc[0].render(scale=dpi / 72.0).to_pil().convert("RGB")
        finally:
            doc.close()

    ia, ib = render(a), render(b)
    if ia.size != ib.size:
        return ia.size[0] * ia.size[1], ia.size[0] * ia.size[1]
    pa, pb = ia.tobytes(), ib.tobytes()
    if pa == pb:
        return 0, ia.size[0] * ia.size[1]
    diff = sum(1 for i in range(0, len(pa), 3) if pa[i:i + 3] != pb[i:i + 3])
    return diff, ia.size[0] * ia.size[1]


def _streams(pdf: Path) -> tuple[list[bytes], list[bytes]]:
    """(document streams, ToUnicode maps) of the PDF, decompressed.

    The whole files are never byte-identical across two TeX installs: the PDF carries the
    engine's banner ("TeX Live 2026/Homebrew" vs "TeX Live 2026"), which shifts every byte
    offset after it, so the cross-reference stream differs too. Everything else is the
    document itself (page content, embedded font subsets, the object stream), compared
    exactly. ToUnicode maps are returned apart: they only steer copy and paste, and a newer
    TeX Live glyph list may map one more unused glyph (seen: cmsy's "mapsto"), which changes
    neither the page nor any text the resume contains (the text check covers that).
    """
    import re
    import zlib
    doc, cmaps = [], []
    for head, body in re.findall(rb"<<((?:(?!>>\s*stream).){0,600}?)>>\s*stream\r?\n(.*?)endstream",
                                 pdf.read_bytes(), re.DOTALL):
        if b"/XRef" in head:
            continue
        if b"/FlateDecode" in head:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                pass
        (cmaps if b"begincmap" in body else doc).append(body)
    return doc, cmaps


def _deps(pdflatex: str, texdir: Path) -> list[str]:
    """The TeX Live packages owning every file pdflatex opens for the .tex files in ``texdir``.

    Each .tex is compiled again with -recorder; the .fls lists every file read, and the
    install's own package database (tlpkg/texlive.tlpdb) says which package owns each one.
    """
    import shutil
    import subprocess
    exe = os.path.abspath(pdflatex)
    kpse = str(Path(exe).parent / ("kpsewhich.exe" if os.name == "nt" else "kpsewhich"))
    root = subprocess.run([kpse, "-var-value=TEXMFROOT"], capture_output=True,
                          text=True, check=True).stdout.strip()
    dist = Path(subprocess.run([kpse, "-var-value=TEXMFDIST"], capture_output=True,
                               text=True, check=True).stdout.strip()).resolve()
    used: set[str] = set()
    with tempfile.TemporaryDirectory() as tmp:
        for tex in sorted(texdir.glob("*.tex")):
            shutil.copy(tex, tmp)
            subprocess.run([exe, "-recorder", "-interaction=nonstopmode", "-halt-on-error",
                            tex.name], cwd=tmp, capture_output=True)
            fls = Path(tmp) / tex.with_suffix(".fls").name
            for line in fls.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("INPUT "):
                    try:
                        rel = Path(line[6:]).resolve().relative_to(dist)
                    except ValueError:
                        continue                      # the .tex itself, the format, aux files
                    used.add("texmf-dist/" + rel.as_posix())
    owner, pkg, runfiles = {}, None, False
    db = Path(root) / "tlpkg" / "texlive.tlpdb"
    for line in db.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("name "):
            pkg, runfiles = line[5:], False
        elif not line.startswith(" "):
            runfiles = line.startswith("runfiles")
        elif runfiles:
            f = line.split()[0]
            if f in used:
                owner[f] = pkg
    missing = sorted(used - owner.keys())
    if missing:
        print("files with no owning package:", *missing, sep="\n  ")
    # 00texlive.image owns the generated pdftex.map; it is not an installable package.
    return sorted({p for p in owner.values() if not p.startswith("00texlive")})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pdflatex", help="the pdflatex under test (the bundle)")
    ap.add_argument("--as-installed", metavar="ENGINE_DIR",
                    help="instead of --pdflatex: act as the installed engine in ENGINE_DIR "
                         "(the folder holding SponsorJobs[.exe] and tinytex/), so the app's own "
                         "lookup has to find the bundled LaTeX, with no PATH help")
    ap.add_argument("--reference", help="a reference pdflatex to compare against")
    ap.add_argument("--out", help="where to write the PDFs (default: a temp dir)")
    ap.add_argument("--json", help="also write the results as JSON here")
    ap.add_argument("--deps", action="store_true",
                    help="print the TeX Live packages the templates need (use a FULL TeX Live)")
    args = ap.parse_args(argv)

    if bool(args.pdflatex) == bool(args.as_installed):
        ap.error("give exactly one of --pdflatex and --as-installed")
    out = Path(args.out or tempfile.mkdtemp(prefix="latex-check-")).resolve()
    # The app reads its data dir at import time; keep it away from anyone's real data.
    os.environ["RESUME_AGENT_DATA_DIR"] = str(out / "data")
    # Reproducible PDF dates and IDs, so a byte comparison is meaningful too.
    os.environ.setdefault("SOURCE_DATE_EPOCH", "1767225600")
    os.environ.setdefault("FORCE_SOURCE_DATE", "1")
    sys.path.insert(0, str(ROOT))
    from ui import app  # noqa: E402

    names = _template_names()
    if args.as_installed:
        from tailoring import compiler
        engine = Path(args.as_installed).resolve()
        sys.frozen = True
        sys.executable = str(engine / ("SponsorJobs.exe" if os.name == "nt" else "SponsorJobs"))
        sys._MEIPASS = str(engine / "_internal")
        os.environ.pop("PDFLATEX", None)
        found = compiler.find_pdflatex()
        print(f"the installed engine would use: {found}")
        if not compiler.is_bundled(found):
            print("FAIL: that is not the bundled LaTeX")
            return 1
    test = _build_all(app, args.pdflatex, out / "bundle", names)
    if args.deps:
        print("\n".join(_deps(args.pdflatex, out / "bundle")))
        return 0
    ref = _build_all(app, args.reference, out / "reference", names) if args.reference else {}

    results, failed = [], False
    for n in names:
        row = {"template": n}
        pdf = test.get(n)
        row["compiled"] = bool(pdf)
        row["pages"] = _pages(pdf) if pdf else None
        ok = bool(pdf) and row["pages"] == 1
        if args.reference:
            rpdf = ref.get(n)
            row["ref_pages"] = _pages(rpdf) if rpdf else None
            if pdf and rpdf:
                row["text_match"] = _text(pdf) == _text(rpdf)
                d, total = _pixel_diff(pdf, rpdf)
                row["pixel_diff"] = d
                row["pixels"] = total
                row["tex_match"] = (pdf.with_suffix(".tex").read_bytes()
                                    == rpdf.with_suffix(".tex").read_bytes())
                (sa, ca), (sb, cb) = _streams(pdf), _streams(rpdf)
                row["streams_match"] = sa == sb
                row["tounicode_match"] = ca == cb
                ok = ok and row["ref_pages"] == row["pages"] and row["text_match"] \
                    and d == 0 and row["tex_match"] and row["streams_match"]
            else:
                ok = False
        row["ok"] = ok
        failed |= not ok
        results.append(row)

    cols = ["template", "pages"] + (["ref_pages", "text_match", "pixel_diff", "tex_match",
                                     "streams_match", "tounicode_match"] if args.reference else []) + ["ok"]
    print("  ".join(f"{c:>15}" for c in cols))
    for r in results:
        print("  ".join(f"{str(r.get(c)):>15}" for c in cols))
    print(f"\n{sum(r['ok'] for r in results)}/{len(results)} templates OK  (PDFs in {out})")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
