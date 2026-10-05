"""Apply bullet edits and *prove* they are confined to bullet interiors.

CLAUDE.md §8: "After any edit, a diff of the .tex must show changes confined to
targeted bullet interiors." This module both performs the splice (delegating to
``LatexTemplate.render``) and provides the verification used by the acceptance
tests: everything outside the edited interiors must be byte-identical.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from .latex_template import LatexTemplate


@dataclass
class EditVerification:
    ok: bool
    violations: list[str]  # human-readable descriptions of out-of-scope changes

    def __bool__(self) -> bool:
        return self.ok


def apply_edits(template: LatexTemplate, edits: dict[str, str]) -> str:
    """Splice ``edits`` into the template, touching only bullet interiors."""
    return template.render(edits)


def verify_confined(
    template: LatexTemplate, edited_source: str, edits: dict[str, str]
) -> EditVerification:
    """Confirm ``edited_source`` differs from the original ONLY inside the
    interiors of the bullets named in ``edits``.

    Fast path (source came from our renderer): walk the original's "outside"
    segments and the edited source's outside segments in lock-step and require
    them byte-identical. Interior lengths in the edited source are known exactly
    from the stored ``lead_ws``/``trail_ws`` plus the edit text, so no guessing.

    Slow path (source produced elsewhere): fall back to a line diff and flag any
    changed line that isn't inside an edited bullet.
    """
    expected = template.render(edits)
    if edited_source != expected:
        return _diff_report(template, edited_source, edits)

    original = template.source
    edited_bullets = sorted(
        (template.get(bid) for bid in edits), key=lambda b: b.interior_start
    )

    violations: list[str] = []
    orig_cursor = 0   # position in the original source
    exp_cursor = 0    # position in the edited (expected) source
    for b in edited_bullets:
        # The "outside" segment leading up to this bullet must match verbatim.
        seg_len = b.interior_start - orig_cursor
        orig_seg = original[orig_cursor : orig_cursor + seg_len]
        exp_seg = expected[exp_cursor : exp_cursor + seg_len]
        if orig_seg != exp_seg:
            violations.append(
                f"bytes before bullet {b.id!r} changed outside its interior"
            )
        orig_cursor = b.interior_end
        exp_cursor += seg_len
        # Skip past the edited interior in the edited source.
        interior_len = len(b.lead_ws) + len(edits[b.id].strip()) + len(b.trail_ws)
        exp_cursor += interior_len

    # Trailing segment after the last edited bullet.
    if original[orig_cursor:] != expected[exp_cursor:]:
        violations.append("trailing bytes changed outside any edited bullet")

    return EditVerification(ok=not violations, violations=violations)


def _diff_report(
    template: LatexTemplate, edited_source: str, edits: dict[str, str]
) -> EditVerification:
    """Slow path: line-diff and flag any changed line not inside an edited bullet."""
    original = template.source
    allowed_lines: set[int] = set()
    for bid in edits:
        b = template.get(bid)
        first, last = b.line_span(original)
        allowed_lines.update(range(first, last + 1))

    violations: list[str] = []
    sm = difflib.SequenceMatcher(
        a=original.splitlines(), b=edited_source.splitlines()
    )
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        changed = set(range(i1 + 1, i2 + 1))  # original line numbers, 1-based
        stray = changed - allowed_lines
        if stray:
            violations.append(f"{tag} at original lines {sorted(stray)}")
    return EditVerification(ok=not violations, violations=violations)


def unified_diff(original: str, edited: str, path: str = "resume.tex") -> str:
    """A readable unified diff, for logs and human review."""
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            edited.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
