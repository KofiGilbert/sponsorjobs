# PyInstaller recipe: turn SponsorJobs into SponsorJobs.exe.
#
# Run from the repo root:   pyinstaller packaging/tailor.spec
# Output:                   dist/SponsorJobs/SponsorJobs.exe
#
# ONE FOLDER, not one file. A --onefile exe unpacks itself to a temp dir on every launch,
# which for a ~60MB app is a visible pause each time and puts the LaTeX toolchain somewhere
# different on every run. The installer hides the folder from the person anyway, so the
# only thing --onefile would buy is a slower start.
#
# console=False: no black terminal window behind the app. This is the difference between
# "I run a Python command" and "I installed an app".
#
# LaTeX is NOT bundled here. It is a separate 600MB toolchain with its own installer, and
# PyInstaller is the wrong tool for relocating it. The Inno Setup script
# (packaging/tailor.iss) handles it as an install-time prerequisite, so the person still
# only ever runs ONE installer. compiler.find_pdflatex() already searches PATH and the
# standard MiKTeX location, so a bundled or system TeX both just work.

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(os.getcwd())

datas = [
    (str(ROOT / "ui" / "templates"), "ui/templates"),      # index.html
    (str(ROOT / "ui" / "static"), "ui/static"),            # css, js, icons
    (str(ROOT / "config" / "templates"), "config/templates"),  # CV manifests
    # The CV .tex templates themselves: without these the app has nothing to render into.
    *[(str(p), "config") for p in ROOT.glob("config/resume_*.tex")],
    # The AUTO-lane evidence log ships WITH the product (§7): it must never be something a
    # runtime path or a user config can edit.
    (str(ROOT / "submit"), "submit"),
]

# The visa-sponsor snapshot (public government data, vetted and staged by
# packaging/bundle_sponsor_db.py). Without it a fresh install has ZERO sponsor data and
# every LinkedIn badge reads "No sponsor record" until the person does homework, on the
# product whose differentiator is this data. Optional so a dev build still works, but say
# so loudly: an installer built without it recreates the empty-first-run bug.
# The committed seeds the engine reads at runtime: the watchlist (the local crawl's starting
# boards when the shared feed is unreachable) and the sponsor CSV (the H-1B fallback when the
# person's visa DB is empty). Without these an installed app falls back to three hardcoded
# boards and no H-1B badges. Both resolve as ROOT/seed/... in ui/ and sourcing/.
datas.append((str(ROOT / "seed"), "seed"))
# The per-employer depth (2026-10): the year-by-year H-1B history rides in seed/ above; the
# PERM-by-year list and the LCA wage bundle live in config/ and dist-data/lca/ and resolve as
# ROOT/config/... and ROOT/dist-data/lca in ui/app.py (_load_bundled_depth). The staged
# sponsors.db already holds all three tables; the files let a refresh re-check them by version.
if (ROOT / "config" / "perm_by_year.csv.gz").exists():
    datas.append((str(ROOT / "config" / "perm_by_year.csv.gz"), "config"))
if (ROOT / "dist-data" / "lca").is_dir():
    datas.append((str(ROOT / "dist-data" / "lca"), "dist-data/lca"))

_seed = ROOT / "packaging" / "seed" / "sponsors.db"
if _seed.exists():
    datas.append((str(_seed), "seed"))
else:
    print("WARNING: packaging/seed/sponsors.db missing; this build ships NO visa data. "
          "Run: python packaging/bundle_sponsor_db.py")

# Photo and scanned-PDF reading (intake/cv_import.py): RapidOCR on onnxruntime and OpenCV, all
# of which need numpy. RapidOCR finds its config.yaml and its three PP-OCR .onnx models (~15 MB)
# next to its own module (Path(__file__).parent), so they ship as data under
# rapidocr_onnxruntime/. Without them, or without numpy, OCR returned "" in the installed app
# and a photographed resume read as empty.
datas += collect_data_files("rapidocr_onnxruntime", excludes=["**/__pycache__/*"])

hiddenimports = [
    # Flask/Werkzeug reach for these dynamically, so static analysis misses them.
    "flask", "jinja2", "werkzeug", "click", "itsdangerous", "markupsafe",
    "anthropic", "httpx", "certifi",
    "sqlite3",
    # Our own late/optional imports (inside functions), which PyInstaller can't see.
    "ui.shell", "tailoring.compiler", "tailoring.assembler", "tailoring.keywords",
    "sourcing.sponsors", "sourcing.ats", "sourcing.service", "sourcing.watchlist",
    "submit.allowlist", "intake.memory", "intake.template_manifest", "llm.anthropic_client",
    # The applicant brain (docs/applicant-brain.md), imported inside functions in ui/app.py.
    "applicant", "applicant.keys", "applicant.store", "applicant.library", "applicant.migrate",
    "applicant.redact",
    # The OCR stack, imported lazily inside intake/cv_import.py.
    "intake.cv_import", "numpy", "cv2", "onnxruntime", "shapely", "shapely.geometry", "pyclipper", "yaml",
    "pypdfium2", *collect_submodules("rapidocr_onnxruntime"),
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here is used by the app and each drags in a large dependency tree. numpy is NOT
    # here: OCR needs it. MemPalace and ChromaDB are, so the semantic memory layer (being phased
    # out) cannot start in the installed app: it would add RAM and an 80 MB model download now
    # that numpy imports. The verbatim transcript layer needs neither (intake/palace_memory.py),
    # and packaging/launcher.py also sets RESUME_AGENT_PALACE_INDEX=0. Leaving them out drops
    # their own trees too (grpc, opentelemetry, kubernetes, tokenizers, huggingface_hub).
    # numpy's build and test tools are left out; OCR uses none of them.
    excludes=["tkinter", "matplotlib", "pandas", "PyQt5", "PySide6", "pytest",
              "chromadb", "mempalace",
              "numpy.f2py", "numpy.distutils", "numpy.testing", "numpy.tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="SponsorJobs",
    debug=False,
    strip=False,
    upx=False,              # UPX-packed exes are a classic false positive for AV scanners
    console=False,          # no terminal window: this is an app, not a script
    # The .ico only matters for the Windows exe. On macOS the engine is a sidecar inside
    # the Electron .app, which carries its own icon, and PyInstaller would need Pillow to
    # convert an .ico there.
    icon=str(ROOT / "ui" / "static" / "icon.ico") if sys.platform == "win32" else None,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="SponsorJobs",
)
