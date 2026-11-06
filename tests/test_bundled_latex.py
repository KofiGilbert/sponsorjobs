"""The installers carry their own LaTeX (a pinned TinyTeX, packaging/install_tinytex.py).

1. An installed (frozen) engine finds that bundled pdflatex FIRST, on macOS and Windows,
   with no PATH assumptions, and falls back to the system's TeX when there is none.
2. The bundle renders the same resumes as the reference TeX: when a TinyTeX is present
   (TINYTEX_PDFLATEX, or the local copy at .scratch/tinytex), every template is built with it
   and with the system pdflatex and compared page count, text, pixels and the PDF streams.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import tailoring.compiler as compiler

ROOT = Path(__file__).resolve().parents[1]


def _fake_install(tmp_path, monkeypatch, platform: str, plat_dir: str, exe_name: str,
                  under_meipass: bool = False):
    """A frozen engine at tmp/engine/SponsorJobs with a bundled TinyTeX beside it."""
    engine = tmp_path / "engine"
    internal = engine / "_internal"
    internal.mkdir(parents=True)
    root = (internal if under_meipass else engine) / "tinytex"
    exe = root / "bin" / plat_dir / exe_name
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(engine / ("SponsorJobs.exe" if platform == "win32"
                                                          else "SponsorJobs")))
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delenv("PDFLATEX", raising=False)
    return exe


def test_installed_mac_app_uses_the_bundled_pdflatex_first(tmp_path, monkeypatch):
    exe = _fake_install(tmp_path, monkeypatch, "darwin", "universal-darwin", "pdflatex")
    # Even with a system TeX on PATH, the installed app renders with the TeX it shipped with.
    monkeypatch.setattr(compiler.shutil, "which", lambda name: "/opt/homebrew/bin/pdflatex")
    assert compiler.find_pdflatex() == str(exe)
    assert compiler.is_bundled(str(exe))


def test_installed_windows_app_uses_the_bundled_pdflatex_first(tmp_path, monkeypatch):
    exe = _fake_install(tmp_path, monkeypatch, "win32", "windows", "pdflatex.exe")
    monkeypatch.setattr(compiler.shutil, "which", lambda name: r"C:\texlive\bin\pdflatex.exe")
    assert compiler.find_pdflatex() == str(exe)


def test_bundle_in_pyinstaller_data_folder_is_found_too(tmp_path, monkeypatch):
    exe = _fake_install(tmp_path, monkeypatch, "darwin", "universal-darwin", "pdflatex",
                        under_meipass=True)
    monkeypatch.setattr(compiler.shutil, "which", lambda name: None)
    assert compiler.find_pdflatex() == str(exe)


def test_pdflatex_env_var_still_overrides_the_bundle(tmp_path, monkeypatch):
    _fake_install(tmp_path, monkeypatch, "darwin", "universal-darwin", "pdflatex")
    mine = tmp_path / "my-pdflatex"
    mine.write_text("")
    monkeypatch.setenv("PDFLATEX", str(mine))
    assert compiler.find_pdflatex() == str(mine)


def test_installed_app_without_a_bundle_falls_back_to_the_system(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "engine" / "SponsorJobs"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "engine" / "_internal"), raising=False)
    monkeypatch.delenv("PDFLATEX", raising=False)
    monkeypatch.setattr(compiler.shutil, "which", lambda name: "/usr/bin/pdflatex")
    assert compiler.find_pdflatex() == "/usr/bin/pdflatex"


def test_dev_checkout_never_looks_for_a_bundle(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert compiler.bundled_tex_roots() == []
    assert compiler.bundled_pdflatex() is None


def test_bundled_tex_runs_with_its_own_bin_first_and_no_foreign_tree(tmp_path, monkeypatch):
    exe = _fake_install(tmp_path, monkeypatch, "darwin", "universal-darwin", "pdflatex")
    monkeypatch.setattr(compiler.shutil, "which", lambda name: "/opt/homebrew/bin/pdflatex")
    monkeypatch.setenv("TEXMFCNF", "/opt/homebrew/share/texmf-dist/web2c")
    monkeypatch.setenv("TEXMFHOME", "/Users/someone/Library/texmf")
    env = compiler.tex_env(str(exe))
    assert env["PATH"].split(os.pathsep)[0] == str(exe.parent)
    assert "TEXMFCNF" not in env and "TEXMFHOME" not in env


def test_system_tex_on_path_keeps_the_environment_untouched(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(compiler.shutil, "which", lambda name: "/usr/bin/pdflatex")
    assert compiler.tex_env("/usr/bin/pdflatex") is None


def test_package_list_is_the_one_the_installer_reads():
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        import install_tinytex
    finally:
        sys.path.pop(0)
    pkgs = install_tinytex.read_packages()
    # The template preambles' own packages are all there.
    for p in ("microtype", "hyperref", "geometry", "enumitem", "cm-super", "amsmath", "xcolor"):
        assert p in pkgs
    assert len(pkgs) == len(set(pkgs))
    assert set(install_tinytex.PINS) >= {"darwin", "windows"}


# ---------------------------------------------------------------- same output as today
def _tinytex_pdflatex() -> str | None:
    env = os.environ.get("TINYTEX_PDFLATEX")
    if env and Path(env).exists():
        return env
    name = "pdflatex.exe" if os.name == "nt" else "pdflatex"
    for cand in sorted((ROOT / ".scratch" / "tinytex" / "bin").glob(f"*/{name}")):
        return str(cand)
    return None


def _reference_pdflatex() -> str | None:
    try:
        ref = compiler.find_pdflatex()
    except FileNotFoundError:
        return None
    tt = _tinytex_pdflatex()
    return None if tt and Path(ref).resolve() == Path(tt).resolve() else ref


@pytest.mark.latex
@pytest.mark.skipif(_tinytex_pdflatex() is None or _reference_pdflatex() is None,
                    reason="needs a TinyTeX (packaging/install_tinytex.py) and a system pdflatex")
def test_bundled_tinytex_makes_the_same_resumes_as_the_system_tex(tmp_path):
    out = subprocess.run(
        [sys.executable, str(ROOT / "packaging" / "latex_check.py"),
         "--pdflatex", _tinytex_pdflatex(), "--reference", _reference_pdflatex(),
         "--out", str(tmp_path / "check")],
        capture_output=True, text=True, timeout=1800,
        env={k: v for k, v in os.environ.items() if k != "PDFLATEX"})
    assert out.returncode == 0, out.stdout + out.stderr
