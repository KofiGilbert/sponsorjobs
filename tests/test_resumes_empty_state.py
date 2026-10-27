"""The fresh-install Resumes list shows faint example rows under one card with one action
(Kofi chose this on 2026-10-27, so a new install doesn't look blank)."""
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "ui" / "static" / "app.js").read_text()
CSS = (Path(__file__).resolve().parents[1] / "ui" / "static" / "styles.css").read_text()


def test_empty_list_shows_examples_and_one_action():
    assert 'class="cvs-ex-rows" aria-hidden="true"' in JS
    assert "Your tailored resumes land here" in JS
    assert 'id="cvsFirstTailor"' in JS
    assert JS.count("cvsFirstTailor") >= 2  # rendered and wired


def test_examples_cannot_be_clicked_or_mistaken_for_real():
    assert ">Example</span>" in JS
    rule = CSS[CSS.index(".cvs-ex-rows {"):]
    assert "pointer-events: none" in rule.split("}")[0]
