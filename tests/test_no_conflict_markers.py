"""A merge must never ship its conflict markers: one stray "=======" in index.html once
rendered at the top left of every page (2026-10-14)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_MARK = re.compile(r"^(<<<<<<< |=======$|>>>>>>> )", re.M)
_SKIP = {"node_modules", ".git", "dist", "build", ".venv"}


def test_no_merge_conflict_markers():
    bad = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in {".py", ".js", ".html", ".css", ".json", ".md"}:
            continue
        if _SKIP & set(p.relative_to(ROOT).parts):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if _MARK.search(text):
            bad.append(str(p.relative_to(ROOT)))
    assert not bad, f"merge conflict markers left in: {bad}"
