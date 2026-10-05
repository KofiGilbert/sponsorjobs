r"""Compile a .tex to PDF and extract the three success signals (CLAUDE.md §8/§9).

Exit code 0 is NOT proof of success. After every compile we read the log for:

1. exit code,
2. page count (must stay 1),
3. Overfull \hbox warnings (no *new* ones versus the baseline).

The self-heal loop consumes these. This module only compiles and reports; it
makes no accept/reject decision itself.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# "Output written on resume.pdf (1 page, 12345 bytes)."
_PAGES = re.compile(r"Output written on .*?\((\d+)\s+pages?", re.IGNORECASE)
# "Overfull \hbox (12.3pt too wide) in paragraph at lines 84--86"
_OVERFULL = re.compile(
    r"Overfull \\hbox \(([\d.]+)pt too wide\).*?at lines (\d+)--(\d+)",
    re.DOTALL,
)
# Some overfull lines omit the line range (e.g. in \hbox alignment); catch them
# too so counts stay honest.
_OVERFULL_ANY = re.compile(r"Overfull \\hbox")
# "TAILORFILL=760.60461pt:775.05315pt" — the document reporting how much of the text
# block its content actually occupies (emitted from the body; see assembler._FILL_PROBE).
_FILL = re.compile(r"TAILORFILL=([\d.]+)pt:([\d.]+)pt")


def no_window_kwargs() -> dict:
    """subprocess kwargs that keep Windows from flashing a console window.

    pdflatex, pdftoppm and ghostscript are all CONSOLE programs. When the parent has a
    console of its own (a dev run in a terminal) they quietly borrow it. Once the app is
    packaged with console=False it has none, so Windows gives each child its OWN window:
    opening Templates renders four previews and four black boxes flash across the screen
    and vanish. Nothing is broken, and it looks completely broken, which for a commercial
    app is the same thing.

    CREATE_NO_WINDOW is Windows-only and must never be passed elsewhere, so this returns
    an empty dict off Windows rather than making every call site remember that.
    """
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def find_pdflatex() -> str:
    """Locate a pdflatex executable, honouring PDFLATEX / common install dirs."""
    env = os.environ.get("PDFLATEX")
    if env and Path(env).exists():
        return env
    found = shutil.which("pdflatex")
    if found:
        return found
    # Standard install locations that are often NOT on PATH. An app opened from the macOS
    # Finder/Dock inherits launchd's minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin), so a
    # perfectly good MacTeX or Homebrew TeX is invisible to shutil.which there.
    candidates = [
        # MiKTeX per-user install on Windows.
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64" / "pdflatex.exe",
        # MacTeX / BasicTeX (the texbin symlink always points at the active TeX year).
        Path("/Library/TeX/texbin/pdflatex"),
        # Homebrew TeX Live: Apple Silicon, then Intel.
        Path("/opt/homebrew/bin/pdflatex"),
        Path("/usr/local/bin/pdflatex"),
        # Linux distro packages.
        Path("/usr/bin/pdflatex"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    raise FileNotFoundError(
        "pdflatex not found. Install MiKTeX (Windows), MacTeX (macOS), or TeX Live, "
        "or set the PDFLATEX env var."
    )


@dataclass
class Overfull:
    points: float
    line_start: int
    line_end: int

    def key(self) -> tuple[int, int]:
        return (self.line_start, self.line_end)


@dataclass
class CompileResult:
    ok: bool                       # exit code 0 AND a PDF was produced
    returncode: int
    pages: int | None
    overfulls: list[Overfull] = field(default_factory=list)
    overfull_count: int = 0        # includes any that lacked a line range
    log: str = ""
    pdf_path: Path | None = None
    tex_path: Path | None = None
    fill_ratio: float | None = None  # ONLY meaningful when pages == 1; see parse_fill

    @property
    def overfull_keys(self) -> set[tuple[int, int]]:
        return {o.key() for o in self.overfulls}


def parse_log(log: str) -> tuple[int | None, list[Overfull], int]:
    """Return (pages, overfulls-with-line-ranges, total-overfull-count)."""
    pages_matches = _PAGES.findall(log)
    pages = int(pages_matches[-1]) if pages_matches else None
    overfulls = [
        Overfull(points=float(pt), line_start=int(a), line_end=int(b))
        for pt, a, b in _OVERFULL.findall(log)
    ]
    total = len(_OVERFULL_ANY.findall(log))
    return pages, overfulls, total


def parse_fill(log: str) -> float | None:
    """Return the fraction of the text block the content occupies, or None if unprobed.

    This is what separates "fits on one page" from "fills one page". Exit code 0, one
    page, and no overfull are all still true of a half-empty CV, which is how half pages
    shipped (CLAUDE.md §8, memory complete-cv-fills-page).

    CAUTION: \\pagetotal reports the height of the LAST page only, so on a multi-page
    run this returns that final page's fill, not the document's. Callers MUST check
    ``pages == 1`` before trusting it.
    """
    found = _FILL.findall(log)
    if not found:
        return None
    used, available = float(found[-1][0]), float(found[-1][1])
    if available <= 0:
        return None
    return used / available


def compile_tex(
    tex_source: str,
    workdir: str | os.PathLike,
    jobname: str = "resume",
    timeout: int = 120,
    pdflatex: str | None = None,
) -> CompileResult:
    """Write ``tex_source`` into ``workdir`` and run pdflatex once.

    A single pass is enough for this single-page template (no TOC / refs). The
    caller (self-heal loop) may recompile with adjusted content.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    tex_path = workdir / f"{jobname}.tex"
    tex_path.write_text(tex_source, encoding="utf-8")

    exe = pdflatex or find_pdflatex()
    cmd = [
        exe,
        "-interaction=nonstopmode",
        "-halt-on-error",
        f"-jobname={jobname}",
        # MiKTeX: install missing packages automatically, no prompt.
        "--enable-installer",
        str(tex_path.name),
    ]
    # pdflatex spawns helpers (kpsewhich, mktextfm, ...) by name. When we found it outside
    # PATH (a Finder-launched macOS app), put its own bin dir first so they resolve too.
    env = None
    exe_dir = os.path.dirname(exe)
    if exe_dir and shutil.which("pdflatex") is None:
        env = {**os.environ, "PATH": exe_dir + os.pathsep + os.environ.get("PATH", "")}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workdir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            **no_window_kwargs(),   # no black box flashing on screen; see the helper
        )
        returncode = proc.returncode
        stdout = proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        returncode = -1
        stdout = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(
            exc.stdout, bytes
        ) else (exc.stdout or "")

    # pdflatex writes the authoritative log to <jobname>.log; prefer it.
    log_path = workdir / f"{jobname}.log"
    log = ""
    if log_path.exists():
        log = log_path.read_text(encoding="utf-8", errors="replace")
    if not log:
        log = stdout

    pages, overfulls, total = parse_log(log)
    pdf_path = workdir / f"{jobname}.pdf"
    produced = pdf_path.exists() and pdf_path.stat().st_size > 0
    ok = returncode == 0 and produced

    return CompileResult(
        ok=ok,
        returncode=returncode,
        pages=pages,
        overfulls=overfulls,
        overfull_count=total,
        log=log,
        pdf_path=pdf_path if produced else None,
        tex_path=tex_path,
        fill_ratio=parse_fill(log),
    )
