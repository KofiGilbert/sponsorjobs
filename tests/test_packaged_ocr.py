"""Photo and scanned-PDF reading in the installed app.

packaging/tailor.spec used to exclude numpy, so in the frozen app rapidocr_onnxruntime,
onnxruntime and cv2 could not import and intake/cv_import.py's OCR silently returned "": a
photographed or scanned resume read as empty. Now the spec ships numpy and RapidOCR's config and
models. MemPalace and ChromaDB are no longer dependencies (the chat diary, intake/diary.py, is
our own code); the spec still lists them as excluded, so even a build venv that has them can never
pull them in once numpy imports.

No build here (PyInstaller is not run): the spec is executed with stand-ins for PyInstaller's
build objects, and the OCR stack is imported in a fresh interpreter that refuses every module the
spec excludes, which is what the frozen app sees."""
from __future__ import annotations

import importlib.util
import json
import os
import pkgutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from conftest import requires_ocr

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "tailor.spec"
OCR_TEXT = "Jane Doe Data Analyst"


def _launcher():
    spec = importlib.util.spec_from_file_location("sj_launcher_ocr", ROOT / "packaging" / "launcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- the launcher
def test_the_launcher_sets_the_data_dir_and_nothing_for_the_old_index(tmp_path):
    env: dict = {}
    _launcher()._engine_env(tmp_path, env)
    assert env == {"RESUME_AGENT_DATA_DIR": str(tmp_path),
                   "RESUME_AGENT_CRED_FILE": str(tmp_path / "credentials.env")}


def test_the_launcher_never_overrides_a_setting_already_made(tmp_path):
    env = {"RESUME_AGENT_DATA_DIR": "/elsewhere"}
    _launcher()._engine_env(tmp_path, env)
    assert env["RESUME_AGENT_DATA_DIR"] == "/elsewhere"


def test_main_uses_the_engine_env():
    src = (ROOT / "packaging" / "launcher.py").read_text(encoding="utf-8")
    main = src[src.index("def main()"):]
    assert "_engine_env(data)" in main and main.index("_engine_env(data)") < main.index("from ui.app import")


# --------------------------------------------------------------------------- the spec
def _collect_data_files(package, excludes=()):
    """What PyInstaller's collect_data_files returns for a package: every non-Python file, with
    the folder it lands in inside the bundle."""
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        return []
    base = Path(list(spec.submodule_search_locations)[0])
    out = []
    for f in base.rglob("*"):
        if f.is_file() and f.suffix not in (".py", ".pyc") and "__pycache__" not in f.parts:
            out.append((str(f), str(Path(package, *f.relative_to(base).parent.parts))))
    return out


def _collect_submodules(package):
    spec = importlib.util.find_spec(package)
    if spec is None:
        return []
    return [package] + [m.name for m in pkgutil.walk_packages(spec.submodule_search_locations, package + ".")]


def _run_spec(monkeypatch):
    got: dict = {}
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_data_files = _collect_data_files
    hooks.collect_submodules = _collect_submodules
    for name in ("PyInstaller", "PyInstaller.utils"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)

    class _Obj:
        def __init__(self, *a, **kw):
            self.args, self.kw = a, kw
            self.pure = self.scripts = self.binaries = self.datas = []

    def analysis(*a, **kw):
        got.update(kw)
        return _Obj(*a, **kw)

    monkeypatch.chdir(ROOT)
    ns = {"Analysis": analysis, "PYZ": _Obj, "EXE": _Obj, "COLLECT": _Obj, "__file__": str(SPEC)}
    exec(compile(SPEC.read_text(encoding="utf-8"), str(SPEC), "exec"), ns)
    return got


def test_the_spec_ships_numpy_and_leaves_the_memory_layer_out(monkeypatch):
    a = _run_spec(monkeypatch)
    ex = set(a["excludes"])
    assert "numpy" not in ex
    assert {"chromadb", "mempalace"} <= ex
    hidden = set(a["hiddenimports"])
    assert {"numpy", "cv2", "onnxruntime", "intake.cv_import", "pypdfium2", "shapely.geometry", "pyclipper",
            "yaml"} <= hidden


@requires_ocr
def test_the_spec_ships_rapidocrs_config_and_models(monkeypatch):
    a = _run_spec(monkeypatch)
    landed = {(Path(src).name, dest) for src, dest in a["datas"]}
    assert ("config.yaml", "rapidocr_onnxruntime") in landed
    models = {name for name, dest in landed if dest == str(Path("rapidocr_onnxruntime", "models"))}
    assert len([m for m in models if m.endswith(".onnx")]) == 3, models
    assert "rapidocr_onnxruntime.main" in a["hiddenimports"]


# --------------------------------------------------------------------------- the OCR itself
def _image(path: Path):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (900, 220), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=56)
    except TypeError:                       # Pillow before 10.1
        font = ImageFont.load_default()
    d.text((30, 70), OCR_TEXT, fill="black", font=font)
    img.save(path)
    return img


@requires_ocr
def test_ocr_reads_a_photo_and_a_scanned_pdf(tmp_path):
    from intake.cv_import import extract_text
    png = tmp_path / "resume.png"
    img = _image(png)
    assert "Jane" in extract_text(png) and "Analyst" in extract_text(png)
    pdf = tmp_path / "scan.pdf"             # an image-only PDF: no text layer, so OCR reads it
    img.save(pdf, "PDF", resolution=100)
    assert "Analyst" in extract_text(pdf)


_FROZEN = r"""
import importlib.abc, importlib.util, json, os, sys
from pathlib import Path
blocked = set(json.loads(sys.argv[1]))
class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name in blocked or any(name.startswith(b + ".") for b in blocked):
            raise ImportError("excluded from the build: " + name)
        return None
sys.meta_path.insert(0, Blocker())
spec = importlib.util.spec_from_file_location("launcher", sys.argv[3])
launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)
launcher._engine_env(Path(sys.argv[4]))
from intake.cv_import import extract_text, ocr_available
try:
    import numpy  # noqa: F401  (in the build now, for OCR)
    has_numpy = True
except ImportError:
    has_numpy = False
from intake.diary import Diary
pm = Diary(Path(sys.argv[4]) / "palace")
pm.remember_turn("user", "I led the migration to Postgres.")
try:
    import chromadb  # noqa: F401
    chroma_imports = True
except ImportError:
    chroma_imports = False
print(json.dumps({"ocr": ocr_available(), "text": extract_text(sys.argv[2]),
                  "recall": [h["text"] for h in pm.recall("Postgres migration")],
                  "numpy": has_numpy, "chroma_imports": chroma_imports,
                  "chromadb": "chromadb" in sys.modules, "mempalace": "mempalace" in sys.modules}))
"""


def _frozen_run(tmp_path, excludes):
    png = tmp_path / "resume.png"
    _image(png)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    out = subprocess.run([sys.executable, "-c", _FROZEN, json.dumps(sorted(excludes)), str(png),
                          str(ROOT / "packaging" / "launcher.py"), str(tmp_path)],
                         capture_output=True, text=True, timeout=180, cwd=str(ROOT), env=env)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@requires_ocr
def test_ocr_imports_the_way_the_frozen_app_does(tmp_path, monkeypatch):
    excludes = _run_spec(monkeypatch)["excludes"]
    r = _frozen_run(tmp_path, excludes)
    assert r["ocr"] is True and "Analyst" in r["text"]
    # ...numpy is there, ChromaDB cannot import and nothing loaded it or MemPalace...
    assert r["numpy"] is True and r["chroma_imports"] is False and not r["chromadb"] and not r["mempalace"]
    # ...and the chat diary keeps and recalls the turn with neither.
    assert (tmp_path / "palace" / "transcripts" / "default.jsonl").exists()
    assert r["recall"] == ["I led the migration to Postgres."]


@requires_ocr
def test_the_old_spec_is_what_broke_it(tmp_path):
    old = ["tkinter", "matplotlib", "numpy", "pandas", "PyQt5", "PySide6", "pytest"]
    r = _frozen_run(tmp_path, old)
    assert r["ocr"] is False and r["text"] == ""
