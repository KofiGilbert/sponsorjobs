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
    # (2026-11-24) A flex board: each column keeps a readable width and the board scrolls sideways
    # inside its own box when the page is narrow (the browser open, a small window).
    assert ".trk-col { flex: 1 1 0; min-width: 200px;" in CSS
    assert "#view-tracker > .trk-board { flex: 1 1 auto; min-height: 320px; display: flex; gap: 12px; align-items: stretch;\n  overflow-x: auto;" in CSS
