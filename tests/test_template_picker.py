"""Template picker previews: each template renders with NEUTRAL placeholder data (John Doe)
so the picker shows the shape — never a real person's details."""

from __future__ import annotations

import ui.app as app
from intake.template_manifest import load_manifest, sections
from tailoring.assembler import extract_preamble, render_cv
from conftest import requires_latex


# A real person's first name must never appear in placeholder content. It is checked by hash so
# this public test file does not itself publish the name.
_REAL_FIRST_NAME_SHA = "07ae2d63f7b1b8af"


def _mentions_real_first_name(text: str) -> bool:
    import hashlib, re
    return any(hashlib.sha256(w.encode()).hexdigest()[:16] == _REAL_FIRST_NAME_SHA
               for w in re.findall(r"[a-z]+", text.lower()))


def test_placeholder_profile_is_neutral_john_doe():
    p = app._placeholder_profile()
    assert p["identity"]["name"] == "John Doe"
    # No real-person leakage anywhere in the placeholder content.
    blob = str(p).lower()
    assert "shetty" not in blob and not _mentions_real_first_name(blob)
    # Every section a template could render is present, so any template previews fully.
    for key in ("summary", "education", "skills", "projects", "experience",
                "extracurricular", "interests"):
        assert p.get(key)


def test_each_template_preview_renders_john_doe_not_shetty():
    for name in ("shetty", "summary"):
        tex, tname = app._load_template(name)
        secs = sections(load_manifest(tname))
        src = render_cv(extract_preamble(tex), app._placeholder_profile(), sections=secs)
        assert "John Doe" in src
        assert "Shetty" not in src and not _mentions_real_first_name(src)   # no real details in the preview
    # And the shape differs per template. The distinguishing section is the SUMMARY: only the
    # "summary" template leads with one. (Both carry Extracurricular by design, the summary
    # template deliberately includes it to help fill a full page, per its manifest interview_style.)
    shetty = render_cv(extract_preamble(app._load_template("shetty")[0]),
                       app._placeholder_profile(), sections=sections(load_manifest("shetty")))
    summ = render_cv(extract_preamble(app._load_template("summary")[0]),
                     app._placeholder_profile(), sections=sections(load_manifest("summary")))
    assert "large Summary" not in shetty                  # shetty has no professional-summary block
    assert "large Summary" in summ                        # the summary template leads with one


def test_templates_endpoint_exposes_preview_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    d = app.app.test_client().get("/api/templates").get_json()
    by = {t["name"]: t for t in d["templates"]}
    assert by["shetty"]["preview_url"] == "/api/templates/shetty/preview.pdf"
    assert by["summary"]["preview_url"] == "/api/templates/summary/preview.pdf"


@requires_latex
def test_preview_endpoint_returns_a_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    r = app.app.test_client().get("/api/templates/summary/preview.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert r.data[:5] == b"%PDF-"
    assert list((tmp_path / "cv").glob("preview-summary-*.pdf"))   # cached by content hash


def _fill_tools():
    """(pdftocairo path, ok) — the tools to measure how far a preview fills the page."""
    import shutil
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return None
    from tailoring.compiler import find_pdflatex
    try:
        binroot = find_pdflatex()
    except Exception:
        return None
    from pathlib import Path
    for cand in (Path(binroot).parent / "miktex-pdftocairo.exe",
                 Path(binroot).parent / "pdftocairo.exe"):
        if cand.exists():
            return str(cand)
    return shutil.which("pdftocairo")


@requires_latex
def test_previews_are_full_page_not_half(tmp_path, monkeypatch):
    """The user's hard rule: a CV must FILL the page. A preview whose content stops
    halfway is a half-page CV — regression guard. Measures how far down the page 1 the
    rendered content reaches; must be a nearly-full page for BOTH templates."""
    import subprocess

    import numpy as np
    from PIL import Image

    pdftocairo = _fill_tools()
    if not pdftocairo:
        import pytest
        pytest.skip("no pdftocairo/PIL/numpy to measure page fill")
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    for name in ("shetty", "summary"):
        r = app.app.test_client().get(f"/api/templates/{name}/preview.pdf")
        assert r.status_code == 200
        pdf = next((tmp_path / "cv").glob(f"preview-{name}-*.pdf"))
        png = tmp_path / f"{name}.png"
        subprocess.run([pdftocairo, "-png", "-singlefile", "-r", "150", "-f", "1", "-l", "1",
                        str(pdf), str(png.with_suffix(""))],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        im = np.asarray(Image.open(png).convert("L"))
        dark = np.where((im < 200).any(axis=1))[0]
        reaches = (dark.max() / im.shape[0]) if len(dark) else 0.0
        assert reaches >= 0.88, f"{name} preview only fills {reaches:.2f} of the page (half page)"
