"""The resume being built stays reachable from the rail (Kofi, test build 13, 2026-11-09: he left
the resume chat and had no way back, because the rail hid the "In progress" entry)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "ui" / "static" / "styles.css").read_text(encoding="utf-8")
JS = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")


def _rules(selector_part: str) -> list[str]:
    """Every CSS rule whose selector list names ``selector_part``, as 'selector { body }'."""
    return [m.group(0) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS) if selector_part in m.group(1)]


def test_the_rail_does_not_hide_the_in_progress_entry():
    for rule in _rules(".live-nav") + _rules(".nav-live"):
        sel, body = rule.split("{", 1)
        if ".is-rail" in sel and ".nav-sec" not in sel:
            assert "display: none" not in body and "display:none" not in body, rule
    assert 'id="liveNav"' in HTML


def test_the_in_progress_entry_is_a_rail_item():
    paint = JS.split("function paintLiveNav(st)", 1)[1].split("\n  }\n", 1)[0]
    assert 'class="nav-item nav-live" id="liveNavBtn"' in paint
    assert 'class="nav-ic"' in paint and "live-dot" in paint          # the icon carries the dot
    assert 'data-rail="Building"' in paint                              # the rail's short label
    assert 'title="${esc(who)}"' in paint                               # role and company on hover
    assert "resumeBuilder()" in paint
    # highlighted while the builder is open
    view = JS.split("function showView(name)", 1)[1].split("\n  }\n", 1)[0]
    assert "liveNavBtn" in view and "is-active" in view
