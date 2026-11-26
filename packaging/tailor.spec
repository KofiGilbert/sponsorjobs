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
# LaTeX is not collected BY PyInstaller, but it does ship in this folder: after this build,
# packaging/install_tinytex.py's pinned TinyTeX (TeX Live's pdflatex, ~115MB) is copied to
# dist/SponsorJobs/tinytex (.github/workflows/release.yml). PyInstaller is the wrong tool to
# relocate a TeX tree, and it empties dist/SponsorJobs on every run, hence the copy after.
# compiler.find_pdflatex() looks there (beside the executable) first, then for a system TeX.

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

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

# Speech to text for the recorded practice interview (interview/transcribe.py): faster-whisper on
# CTranslate2, with PyAV (FFmpeg) decoding the recording. faster-whisper's voice filter is an
# .onnx file next to its module (faster_whisper/assets), so it ships as data; CTranslate2's and
# PyAV's native libraries are collected explicitly. The Whisper model itself (~140 MB) is NOT in
# the installer: it downloads once on first use into the data dir. Without this block every
# answer came back unheard in the installed app.
import importlib.util as _ilu
_STT = ("faster_whisper", "ctranslate2", "av", "tokenizers", "huggingface_hub")
_stt_missing = [m for m in _STT if _ilu.find_spec(m) is None]
if _stt_missing:
    print("WARNING: speech to text is missing from the build venv (" + ", ".join(_stt_missing) +
          "); the practice interview cannot hear answers. Install: pip install -e \".[interview]\"")
datas += collect_data_files("faster_whisper", excludes=["**/__pycache__/*"])
binaries = collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("av")

hiddenimports = [
    # Flask/Werkzeug reach for these dynamically, so static analysis misses them.
    "flask", "jinja2", "werkzeug", "click", "itsdangerous", "markupsafe",
    "anthropic", "httpx", "certifi",
    "sqlite3",
    # Our own late/optional imports (inside functions), which PyInstaller can't see.
    "ui.shell", "tailoring.compiler", "tailoring.assembler", "tailoring.keywords",
    "sourcing.sponsors", "sourcing.ats", "sourcing.service", "sourcing.watchlist",
    "submit.allowlist", "intake.memory", "intake.template_manifest", "llm.anthropic_client",
    # Sign in with GitHub: the device flow and its public Client ID (config/ is also a data dir).
    "ui.github_device", "config", "config.github_oauth",
    # The applicant brain (docs/applicant-brain.md), imported inside functions in ui/app.py.
    "applicant", "applicant.keys", "applicant.store", "applicant.library", "applicant.migrate",
    "applicant.redact", "applicant.facts", "applicant.mapper", "applicant.expiry", "applicant.rules",
    "applicant.scope", "applicant.page", "applicant.profile",
    # The Orb (orb/rules.py), imported inside functions in ui/app.py.
    "orb", "orb.rules", "orb.store",
    # The OCR stack, imported lazily inside intake/cv_import.py.
    "intake.cv_import", "numpy", "cv2", "onnxruntime", "shapely", "shapely.geometry", "pyclipper", "yaml",
    "pypdfium2", *collect_submodules("rapidocr_onnxruntime"),
    # Speech to text, imported lazily inside interview/transcribe.py. PyAV is a set of Cython
    # modules that import each other from C, which static analysis cannot follow.
    "interview", "interview.transcribe", "interview.report",
    "faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub",
    *collect_submodules("faster_whisper"), *collect_submodules("av"),
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here is used by the app and each drags in a large dependency tree. numpy is NOT
    # here: OCR needs it. MemPalace and ChromaDB are no longer dependencies and nothing imports
    # them (the chat diary, intake/diary.py, is our own code; tests/test_diary_recall.py checks
    # the source tree). They stay listed as a guard for a build venv that still has them
    # installed, so neither they nor their trees (grpc, opentelemetry, kubernetes) can ever be
    # pulled in. tokenizers and huggingface_hub DO ship now, for speech to text (above).
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
