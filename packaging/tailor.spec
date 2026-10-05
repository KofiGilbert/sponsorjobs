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

_seed = ROOT / "packaging" / "seed" / "sponsors.db"
if _seed.exists():
    datas.append((str(_seed), "seed"))
else:
    print("WARNING: packaging/seed/sponsors.db missing; this build ships NO visa data. "
          "Run: python packaging/bundle_sponsor_db.py")

# The job-list snapshot (public postings only, staged by scripts/bundle_feed_snapshot.py from the
# published feed or a bounded polite crawl). A fresh install shows this board at once instead of
# crawling 1,100 boards from the person's laptop; the hourly feed download replaces it. Optional,
# so a dev build still works -- but an installer without it makes every new install crawl.
_feed_snapshot = ROOT / "packaging" / "seed" / "feed"
if (_feed_snapshot / "jobs.json.gz").exists():
    datas.append((str(_feed_snapshot), "seed/feed"))
else:
    print("WARNING: packaging/seed/feed/jobs.json.gz missing; this build ships NO job snapshot "
          "(a fresh install will run the first-open crawl). Run: python scripts/bundle_feed_snapshot.py")

hiddenimports = [
    # Flask/Werkzeug reach for these dynamically, so static analysis misses them.
    "flask", "jinja2", "werkzeug", "click", "itsdangerous", "markupsafe",
    "anthropic", "httpx", "certifi",
    "sqlite3",
    # Our own late/optional imports (inside functions), which PyInstaller can't see.
    "ui.shell", "tailoring.compiler", "tailoring.assembler", "tailoring.keywords",
    "sourcing.sponsors", "sourcing.ats", "sourcing.service", "sourcing.watchlist",
    "submit.allowlist", "intake.memory", "intake.template_manifest", "llm.anthropic_client",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here is used by the app and each drags in a large dependency tree.
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PyQt5", "PySide6", "pytest"],
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
