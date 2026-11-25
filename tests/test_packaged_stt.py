"""Speech to text ships in the installers (the recorded practice interview).

The installers used to be built with ``pip install -e ".[llm]"``, so faster-whisper was never in
them and every recorded answer came back unheard. Now release.yml installs the ``[interview]``
extra and packaging/tailor.spec collects faster-whisper, CTranslate2, PyAV, tokenizers and the
Hugging Face client (for the one-time model download), plus faster-whisper's voice filter model.

No build here: the spec runs with stand-ins for PyInstaller's objects (tests/test_packaged_ocr.py).
"""
from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

from test_packaged_ocr import _run_spec

ROOT = Path(__file__).resolve().parents[1]
_has_stt = all(importlib.util.find_spec(m) for m in ("faster_whisper", "av", "ctranslate2"))
requires_stt = pytest.mark.skipif(not _has_stt, reason="the [interview] extra is not installed")


def test_the_release_build_installs_speech_to_text():
    wf = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert 'pip install -e ".[llm,interview]" pyinstaller' in wf
    assert 'pip install -e ".[llm]" pyinstaller' not in wf


def test_the_interview_extra_pins_versions_that_work_together():
    extra = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"]["optional-dependencies"]["interview"]
    names = {x.split(">")[0].split("<")[0].split("=")[0].strip(): x for x in extra}
    assert set(names) == {"faster-whisper", "av"}
    assert "<" in names["faster-whisper"] and "<" in names["av"]       # an upper bound on both


def test_the_spec_imports_the_speech_stack(monkeypatch):
    a = _run_spec(monkeypatch)
    hidden = set(a["hiddenimports"])
    assert {"interview.transcribe", "faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub"} <= hidden
    assert not ({"faster_whisper", "av", "ctranslate2", "tokenizers", "huggingface_hub"} & set(a["excludes"]))


@requires_stt
def test_the_spec_ships_the_voice_filter_and_the_native_libraries(monkeypatch):
    a = _run_spec(monkeypatch)
    landed = {(Path(src).name, dest) for src, dest in a["datas"]}
    assert any(name.endswith(".onnx") and dest.startswith("faster_whisper") for name, dest in landed), \
        "faster-whisper's voice filter (assets/*.onnx) must ship"
    assert any(m.startswith("av.") for m in a["hiddenimports"])          # PyAV's Cython modules
    assert any("ctranslate2" in dest for _src, dest in a["binaries"])    # CTranslate2's library


def test_the_model_weights_are_not_bundled(monkeypatch):
    """The ~140 MB model downloads once on first use; the installer stays its size."""
    a = _run_spec(monkeypatch)
    assert not any(Path(src).name == "model.bin" for src, _dest in a["datas"])
