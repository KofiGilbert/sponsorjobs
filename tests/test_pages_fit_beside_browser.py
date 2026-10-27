"""Pages size themselves by their OWN width, not the window's, so they stay readable when the
in-app browser takes half the window (Kofi, 2026-10-27: Auto-apply clipped role names and the
Fill button, and the Tracker squeezed six columns)."""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "ui" / "static" / "styles.css").read_text()


def _blocks(at):
    return [m.group(0) for m in re.finditer(at + r"[^{]*\{(?:[^{}]*\{[^{}]*\})+\s*\}", CSS)]


def test_autoapply_layout_rules_follow_the_page_width():
    media = " ".join(_blocks(r"@media"))
    container = " ".join(_blocks(r"@container"))
    assert ".view-autoapply .aa-body" not in media
    assert ".view-autoapply .aa-body { grid-template-columns: minmax(0, 1fr); }" in container


def test_tracker_columns_keep_a_readable_width_and_scroll():
    container = " ".join(_blocks(r"@container"))
    assert "minmax(200px, 1fr)" in container and "overflow-x: auto" in container
