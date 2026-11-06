"""Template previews must render in the installed app, which ships compiled code but no .py files.

The preview cache key hashed tailoring/assembler.py's source; in the installed app that file does
not exist, so every template card in the gallery came up blank (2026-11-06, test build 8)."""
from __future__ import annotations

import types

from ui import app as A


def test_fingerprint_uses_the_source_in_a_dev_run():
    from pathlib import Path
    from tailoring import assembler
    assert A._module_fingerprint(assembler) == Path(assembler.__file__).read_bytes()


def test_fingerprint_falls_back_to_compiled_code_when_the_source_is_missing():
    from tailoring import assembler
    code = compile("x = 1", "assembler.py", "exec")
    frozen = types.SimpleNamespace(__file__="/nowhere/_internal/tailoring/assembler.py",
                                   __name__="tailoring.assembler",
                                   __loader__=types.SimpleNamespace(get_code=lambda name: code))
    fp = A._module_fingerprint(frozen)
    assert fp and fp != A._module_fingerprint(assembler)
    other = types.SimpleNamespace(__file__=None, __name__="tailoring.assembler",
                                  __loader__=types.SimpleNamespace(get_code=lambda name: compile("x = 2", "a", "exec")))
    assert A._module_fingerprint(other) != fp          # different code, different key


def test_preview_renders_when_the_builder_source_is_unreadable(monkeypatch, tmp_path):
    import tailoring.assembler as asm
    monkeypatch.setattr(asm, "__file__", str(tmp_path / "missing" / "assembler.py"))
    monkeypatch.setattr(A, "WORKDIR", tmp_path / "work")
    out = A._template_preview_pdf("classic")
    assert out is not None and out.exists()
