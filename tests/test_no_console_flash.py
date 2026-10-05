"""No black console windows flashing on screen (2026-07-15).

The user, clicking Templates on the packaged app: "4 windows opened quickly outside tailor
and quickly closed before the templates appeared, what is that? certainly doesn't look good
for a commercial app."

They were pdflatex: one per template preview, four templates, four windows. pdflatex,
pdftocairo and ghostscript are all CONSOLE programs. In a dev run the parent HAS a console
and they quietly borrow it, which is why this was invisible until the app was packaged with
console=False. With no console to borrow, Windows gives each child its own.

This is only observable on a packaged app, so a test is the only thing that can hold it:
nobody is going to rebuild an installer to check for a flash.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tailoring.compiler import compile_tex, no_window_kwargs

MIN_TEX = r"\documentclass{article}\begin{document}hi\end{document}"


@pytest.mark.skipif(sys.platform != "win32", reason="CREATE_NO_WINDOW is Windows-only")
def test_pdflatex_runs_without_a_console_window(tmp_path):
    """The one that bit: four previews, four black boxes."""
    seen = {}
    real = subprocess.run

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    with patch("subprocess.run", spy):
        compile_tex(MIN_TEX, tmp_path, jobname="nw")
    assert seen.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW, \
        "pdflatex was launched without CREATE_NO_WINDOW: it will flash a console window"


@pytest.mark.skipif(sys.platform != "win32", reason="CREATE_NO_WINDOW is Windows-only")
def test_the_flag_is_the_real_windows_constant():
    assert no_window_kwargs()["creationflags"] & subprocess.CREATE_NO_WINDOW


def test_the_flag_is_never_passed_off_windows(monkeypatch):
    """creationflags is Windows-only: passing it on macOS/Linux is a TypeError, so the
    helper has to return nothing there rather than every call site remembering."""
    monkeypatch.setattr(sys, "platform", "darwin")
    assert no_window_kwargs() == {}
    monkeypatch.setattr(sys, "platform", "linux")
    assert no_window_kwargs() == {}


def test_every_external_program_we_launch_hides_its_window():
    """Every console program we spawn, not just the one that was reported. pdftocairo and
    ghostscript render the PDF preview and would flash exactly the same way."""
    for rel in ("tailoring/compiler.py", "tailoring/preview.py"):
        src = Path(rel).read_text(encoding="utf-8")
        runs = src.count("subprocess.run(")
        guarded = src.count("no_window_kwargs()")
        assert guarded >= runs, (
            f"{rel}: {runs} subprocess.run call(s) but only {guarded} guarded by "
            "no_window_kwargs(); an unguarded one flashes a console on Windows")
