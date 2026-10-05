"""Approximate rendered text width for the template's Computer Modern font.

The skills packer and bullet sizer must fill lines to the *right margin*, not to a
guessed character count — this font is proportional, so 'm' is ~3x the width of 'i'
and a fixed char budget always leaves a ragged gap (or overflows). These widths are
the cmr10 metrics (points at 10pt), so summing them predicts the real line width
within a few percent — enough to pack each line right up to the margin without
wrapping. Bold (cmbx10) runs ~9% wider; ``bold=True`` applies that factor.
"""

from __future__ import annotations

# cmr10 character widths in points at 10pt (from the TFM). Unlisted chars fall back
# to the average. This is the roman face used for skills values and bullet text.
_W: dict[str, float] = {
    " ": 3.33, "!": 2.78, '"': 5.0, "#": 8.33, "$": 5.0, "%": 8.33, "&": 7.78,
    "'": 2.78, "(": 3.89, ")": 3.89, "*": 5.0, "+": 7.78, ",": 2.78, "-": 3.33,
    ".": 2.78, "/": 5.0, ":": 2.78, ";": 2.78, "=": 7.78, "?": 4.72, "@": 7.78,
    "[": 2.78, "]": 2.78,
    "0": 5.0, "1": 5.0, "2": 5.0, "3": 5.0, "4": 5.0, "5": 5.0, "6": 5.0,
    "7": 5.0, "8": 5.0, "9": 5.0,
    "A": 7.5, "B": 7.08, "C": 7.22, "D": 7.64, "E": 6.81, "F": 6.53, "G": 7.85,
    "H": 7.64, "I": 3.75, "J": 5.14, "K": 7.78, "L": 6.25, "M": 9.17, "N": 7.64,
    "O": 7.64, "P": 6.94, "Q": 7.64, "R": 7.44, "S": 5.56, "T": 7.22, "U": 7.64,
    "V": 7.5, "W": 10.28, "X": 7.5, "Y": 7.5, "Z": 6.11,
    "a": 5.0, "b": 5.56, "c": 4.44, "d": 5.56, "e": 4.44, "f": 3.06, "g": 5.0,
    "h": 5.56, "i": 2.78, "j": 3.06, "k": 5.28, "l": 2.78, "m": 8.33, "n": 5.56,
    "o": 5.0, "p": 5.56, "q": 5.28, "r": 3.92, "s": 3.94, "t": 3.89, "u": 5.56,
    "v": 5.28, "w": 7.22, "x": 5.28, "y": 5.28, "z": 4.44,
}
_AVG = 5.0          # fallback width for any char not in the table
_BOLD = 1.09        # cmbx10 runs ~9% wider than cmr10

# Text area of the template: US-letter (614.3pt) minus 0.7cm margins each side.
TEXTWIDTH_PT = 574.0
# A bullet sits in an itemize whose text block is ~25pt narrower than the full width.
BULLET_WIDTH_PT = 549.0


def text_width_pt(s: str, bold: bool = False) -> float:
    """Approximate the rendered width of ``s`` in points at the template's 10pt."""
    total = sum(_W.get(ch, _AVG) for ch in s)
    return total * (_BOLD if bold else 1.0)


def line_fraction(s: str, width_pt: float = BULLET_WIDTH_PT) -> float:
    """How many lines ``s`` occupies (2.0 = exactly two full lines)."""
    return text_width_pt(s) / width_pt
