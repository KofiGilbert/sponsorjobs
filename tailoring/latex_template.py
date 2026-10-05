r"""Parse a LaTeX résumé template into addressable bullets (CLAUDE.md §8).

The surgical-edit guarantee lives here. We locate every ``\item`` interior
inside an ``itemize`` environment, record its exact byte offsets, and expose a
``render`` that splices new bullet text back in while leaving *every other byte*
of the source — preamble, packages, geometry, headings, whitespace — untouched.

Nothing in this module treats the sample template's content as facts about any
person; it only manipulates structural slots.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Section headings look like: \textbf{\textsc{\large Experience}}
_HEADING = re.compile(r"\\textbf\{\\textsc\{\\large\s+([^}]*)\}\}")
# A \item token that is not part of a longer command name.
_ITEM = re.compile(r"\\item(?![A-Za-z])")
_END_ITEMIZE = re.compile(r"\\end\{itemize\}")
# A block label line, e.g. \noindent \textbf{\textsc{CME Group}} ... or a role.
_BLOCK_LABEL = re.compile(r"\\textbf\{(?:\\textsc\{)?([^}]*)\}")

# Sections whose bullets we are allowed to reword. Everything is tailorable in
# principle (CLAUDE.md §8), but identity/education facts are left alone by
# default; callers may widen this.
DEFAULT_TAILORABLE_SECTIONS = {
    "experience", "projects", "project", "skills", "summary",
    "extracurricular",
}


@dataclass
class Bullet:
    r"""One addressable ``\item`` interior."""

    id: str
    section: str          # nearest preceding \large heading (e.g. "Experience")
    block: str            # nearest preceding block label (e.g. "CME Group")
    text: str             # the stripped interior text (what we may reword)
    interior_start: int   # offset of the interior within the raw source
    interior_end: int     # offset just past the interior
    lead_ws: str          # whitespace between \item and the text (preserved)
    trail_ws: str         # whitespace after the text up to the boundary

    @property
    def length(self) -> int:
        return len(self.text)

    def line_span(self, source: str) -> tuple[int, int]:
        """1-based (first_line, last_line) this bullet's interior occupies."""
        first = source.count("\n", 0, self.interior_start) + 1
        last = source.count("\n", 0, self.interior_end) + 1
        return first, last


class LatexTemplate:
    """An immutable parse of a template that can render surgical edits."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.bullets: list[Bullet] = self._parse()
        self._by_id = {b.id: b for b in self.bullets}

    # -- parsing --------------------------------------------------------- #
    def _parse(self) -> list[Bullet]:
        src = self.source
        # Precompute section-heading positions for nearest-preceding lookup.
        headings = [(m.start(), m.group(1).strip()) for m in _HEADING.finditer(src)]
        # Block labels: any \textbf{...} occurrence, used for the nearest label
        # that is NOT itself a section heading.
        labels = [
            (m.start(), m.group(1).strip())
            for m in _BLOCK_LABEL.finditer(src)
        ]
        heading_starts = {pos for pos, _ in headings}

        item_positions = [m for m in _ITEM.finditer(src)]
        end_positions = [m.start() for m in _END_ITEMIZE.finditer(src)]

        bullets: list[Bullet] = []
        counter: dict[str, int] = {}
        for i, m in enumerate(item_positions):
            interior_start = m.end()
            # Boundary = the next \item or the next \end{itemize}, whichever first.
            next_item = (
                item_positions[i + 1].start()
                if i + 1 < len(item_positions)
                else len(src)
            )
            next_end = next((e for e in end_positions if e > interior_start), len(src))
            interior_end = min(next_item, next_end)

            raw = src[interior_start:interior_end]
            core = raw.strip()
            if not core:
                continue  # empty \item, nothing to address
            lead_len = len(raw) - len(raw.lstrip())
            trail_len = len(raw) - len(raw.rstrip())
            lead_ws = raw[:lead_len]
            trail_ws = raw[len(raw) - trail_len:] if trail_len else ""

            section = self._nearest_before(headings, interior_start) or ""
            block = self._nearest_before(
                [(p, t) for p, t in labels
                 if p not in heading_starts and p < interior_start],
                interior_start,
            ) or ""

            key = re.sub(r"[^a-z0-9]+", "-", section.lower()).strip("-") or "sec"
            counter[key] = counter.get(key, 0) + 1
            bid = f"{key}-{counter[key]}"

            bullets.append(
                Bullet(
                    id=bid,
                    section=section,
                    block=block,
                    text=core,
                    interior_start=interior_start,
                    interior_end=interior_end,
                    lead_ws=lead_ws,
                    trail_ws=trail_ws,
                )
            )
        return bullets

    @staticmethod
    def _nearest_before(items: list[tuple[int, str]], pos: int) -> str | None:
        best = None
        for start, text in items:
            if start < pos:
                best = text
            else:
                break
        return best

    # -- access ---------------------------------------------------------- #
    def get(self, bullet_id: str) -> Bullet:
        return self._by_id[bullet_id]

    def tailorable_bullets(
        self, sections: set[str] | None = None
    ) -> list[Bullet]:
        sections = sections or DEFAULT_TAILORABLE_SECTIONS
        wanted = {s.lower() for s in sections}
        return [b for b in self.bullets if b.section.lower() in wanted]

    # -- rendering ------------------------------------------------------- #
    def render(self, edits: dict[str, str]) -> str:
        """Return the source with only the given bullet interiors replaced.

        ``edits`` maps ``bullet.id`` -> new interior text. Bytes outside the
        targeted interiors are guaranteed byte-identical to the original.
        """
        unknown = set(edits) - set(self._by_id)
        if unknown:
            raise KeyError(f"unknown bullet id(s): {sorted(unknown)}")

        # Apply from the last interior to the first so earlier offsets stay valid.
        out = self.source
        for b in sorted(self.bullets, key=lambda x: x.interior_start, reverse=True):
            if b.id not in edits:
                continue
            new_text = edits[b.id].strip()
            replacement = f"{b.lead_ws}{new_text}{b.trail_ws}"
            out = out[: b.interior_start] + replacement + out[b.interior_end :]
        return out

    def text_of_bullets(self, edits: dict[str, str] | None = None) -> str:
        """Concatenated bullet text (optionally after edits) for coverage."""
        edits = edits or {}
        return "\n".join(edits.get(b.id, b.text) for b in self.bullets)
