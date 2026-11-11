"""Strip text a person cannot see but a model can read, before the model reads it.

A job posting and a live application page are both third-party text that we put
in front of a model next to the person's own data. ``extension/agent_view.js``
already drops page elements a person cannot see (display none, visibility hidden,
aria-hidden, an empty box, parked off-screen), and that covers the ordinary case.

What it cannot catch is a payload hidden inside text that is visible by every one
of those tests. None of the following is hidden by CSS or by geometry:

  * **Unicode tag characters** (U+E0000 to U+E007F). A second alphabet that
    renders as nothing and reads as ASCII to a model, so a whole sentence written
    in it is invisible on the page and legible in the prompt.
  * **Zero-width characters inside a word** (ZWSP, ZWNJ, ZWJ, word joiner, BOM,
    soft hyphen). Between words they are ordinary typesetting; inside one they
    split a word so a filter reads something different from the person.
  * **Bidi overrides and isolates** (U+202A to U+202E, U+2066 to U+2069). They
    reorder what is displayed without changing the characters, so the line a
    person reads and the line a model reads can say opposite things.
  * **Text spelled in variation selectors.** U+FE00 to U+FE0F and U+E0100 to
    U+E01EF each carry one byte, so a run of them is a sentence that renders as
    nothing at all.
  * **Mixed-script look-alike words** (Latin spliced with Cyrillic or Greek).
    "Sеnior" with a Cyrillic "е" reads as a different word to anything matching
    on text, which is how a filter is walked around.

**Removal is the defense; the report is for the person.** Everything in the first
four classes is taken out, so it never reaches the prompt whatever the model would
have done with it. A look-alike word is reported and left alone, because there is
no way to repair one without guessing what it meant.

**This advises, it never blocks.** ``clean`` always returns usable text. A finding
is something to show the person with the offending line quoted, and nothing here
decides that a posting is unfit to read. That matters: real postings carry odd
characters for innocent reasons, and a guard that rejected them would quietly cost
somebody a job they could have applied for.

The three UK subdivision flag emoji (England, Scotland, Wales) are built out of
tag characters legitimately, so they are matched whole and kept.

Pure: no model, no network, no I/O.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# --------------------------------------------------------------------- classes
TAG_START, TAG_END = 0xE0000, 0xE007F
TAG_RANGE = "\U000E0000-\U000E007F"

# Zero width and friends. The soft hyphen and the BOM are here because both are
# invisible mid-word and both survive a copy and paste out of a job board.
ZERO_WIDTH = {
    "​": "zero-width space", "‌": "zero-width non-joiner",
    "‍": "zero-width joiner", "⁠": "word joiner",
    "﻿": "byte order mark", "­": "soft hyphen",
}
# Direction controls: the embeddings and overrides, then the isolates.
BIDI = {
    "‪": "left-to-right embedding", "‫": "right-to-left embedding",
    "‬": "pop directional formatting", "‭": "left-to-right override",
    "‮": "right-to-left override", "⁦": "left-to-right isolate",
    "⁧": "right-to-left isolate", "⁨": "first strong isolate",
    "⁩": "pop directional isolate", "‎": "left-to-right mark",
    "‏": "right-to-left mark",
}
VARIATION = "︀-️\U000E0100-\U000E01EF"

# A black flag plus tag letters plus the cancel tag: England, Scotland and Wales.
# Matched whole and put back, so the only legitimate use of tag characters we know
# of is not mistaken for an attack.
_FLAG = re.compile("\U0001F3F4[" + TAG_RANGE + "]+\U000E007F")
_TAGS = re.compile("[" + TAG_RANGE + "]+")
_VARIATION_RUN = re.compile("[" + VARIATION + "]{2,}")
_ZERO_WIDTH = re.compile("[" + "".join(ZERO_WIDTH) + "]")
_BIDI = re.compile("[" + "".join(BIDI) + "]")
# A zero-width character with a word character on BOTH sides is inside a word.
_ZW_IN_WORD = re.compile(r"(?<=\w)[" + "".join(ZERO_WIDTH) + r"](?=\w)")

_LATIN = re.compile(r"[A-Za-z]")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_GREEK = re.compile(r"[Ͱ-Ͽ]")
_WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)

_FLAG_SLOT = "\x00FLAG%d\x00"
_MAX_QUOTE = 160
_MAX_LISTED = 5


@dataclass
class GuardFinding:
    """One thing worth showing the person, with the line it came from."""

    kind: str          # "tag-characters" | "zero-width" | "bidi" | "variation-text" | "look-alike"
    detail: str
    quote: str = ""    # what it said, or the line it sat in, already made printable


@dataclass
class GuardResult:
    text: str
    findings: list[GuardFinding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        """One line for the person. Empty when there is nothing to say."""
        if not self.findings:
            return ""
        return "Hidden text was removed from this posting: " + "; ".join(
            f.detail for f in self.findings
        )


def _printable(text: str, limit: int = _MAX_QUOTE) -> str:
    """A quote safe to show: controls named, whitespace collapsed, length capped."""
    out = []
    for ch in text:
        if ch in ZERO_WIDTH or ch in BIDI:
            out.append(f"[{ZERO_WIDTH.get(ch) or BIDI.get(ch)}]")
        elif unicodedata.category(ch) in ("Cc", "Cf") or TAG_START <= ord(ch) <= TAG_END:
            out.append(f"[U+{ord(ch):04X}]")
        else:
            out.append(ch)
    flat = " ".join("".join(out).split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _line_of(text: str, index: int) -> str:
    """The line ``index`` falls on, for quoting back to the person."""
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return text[start:end if end != -1 else len(text)]


def _decode_tags(run: str) -> str:
    """Tag characters as the ASCII they stand for."""
    return "".join(chr(ord(c) - TAG_START) for c in run if TAG_START <= ord(c) <= TAG_END)


def _decode_variation(run: str) -> str:
    """A variation-selector run as the bytes it carries, where it decodes to text."""
    out = bytearray()
    for ch in run:
        cp = ord(ch)
        if 0xFE00 <= cp <= 0xFE0F:
            out.append(cp - 0xFE00)
        elif 0xE0100 <= cp <= 0xE01EF:
            out.append(cp - 0xE0100 + 16)
        else:
            return ""
    try:
        decoded = out.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return decoded if decoded.isprintable() else ""


def _look_alike_words(text: str) -> list[str]:
    """Words that splice Latin with Cyrillic or Greek letters."""
    found = []
    for m in _WORD.finditer(text):
        word = m.group(0)
        if _LATIN.search(word) and (_CYRILLIC.search(word) or _GREEK.search(word)):
            found.append(word)
    return found


def clean(text: str) -> GuardResult:
    """Remove what a person cannot see, and say what was there.

    The text comes back usable in every case; see the module docstring on why this
    advises rather than blocks.
    """
    src = str(text or "")
    if not src:
        return GuardResult(text="")

    findings: list[GuardFinding] = []

    # Park the legitimate flag emoji so the tag sweep cannot touch them.
    flags: list[str] = []

    def _park(m: re.Match) -> str:
        flags.append(m.group(0))
        return _FLAG_SLOT % (len(flags) - 1)

    work = _FLAG.sub(_park, src)

    # 1. Tag characters. Decode first so the message is quoted, not merely counted.
    tag_runs = [m.group(0) for m in _TAGS.finditer(work)]
    if tag_runs:
        spelled = [_decode_tags(r) for r in tag_runs]
        said = "; ".join(f'"{_printable(s)}"' for s in spelled if s.strip())
        findings.append(GuardFinding(
            "tag-characters",
            f"{sum(len(r) for r in tag_runs)} invisible tag character(s) "
            f"in {len(tag_runs)} run(s)",
            said or _printable("".join(tag_runs)),
        ))
        work = _TAGS.sub("", work)

    # 2. Text spelled in variation selectors.
    var_runs = [m for m in _VARIATION_RUN.finditer(work)]
    if var_runs:
        spelled = [_decode_variation(m.group(0)) for m in var_runs]
        said = "; ".join(f'"{_printable(s)}"' for s in spelled if s)
        findings.append(GuardFinding(
            "variation-text",
            f"{len(var_runs)} run(s) of variation selectors carrying hidden text",
            said or _line_of(work, var_runs[0].start()),
        ))
        work = _VARIATION_RUN.sub("", work)

    # 3. Zero width. Removed everywhere; reported only where it split a word.
    in_word = [m for m in _ZW_IN_WORD.finditer(work)]
    if in_word:
        quotes = [_printable(_line_of(work, m.start())) for m in in_word[:_MAX_LISTED]]
        findings.append(GuardFinding(
            "zero-width",
            f"{len(in_word)} zero-width character(s) inside a word",
            " | ".join(dict.fromkeys(quotes)),
        ))
    work = _ZERO_WIDTH.sub("", work)

    # 4. Direction controls.
    bidi_hits = [m for m in _BIDI.finditer(work)]
    if bidi_hits:
        names = sorted({BIDI[m.group(0)] for m in bidi_hits})
        findings.append(GuardFinding(
            "bidi",
            f"{len(bidi_hits)} direction control(s) that reorder what is shown "
            f"({', '.join(names)})",
            _printable(_line_of(work, bidi_hits[0].start())),
        ))
        work = _BIDI.sub("", work)

    # 5. Look-alike words. Reported, never rewritten: there is no safe repair.
    mixed = _look_alike_words(work)
    if mixed:
        shown = ", ".join(f'"{w}"' for w in list(dict.fromkeys(mixed))[:_MAX_LISTED])
        findings.append(GuardFinding(
            "look-alike",
            f"{len(mixed)} word(s) mixing Latin with Cyrillic or Greek letters",
            shown,
        ))

    for i, flag in enumerate(flags):
        work = work.replace(_FLAG_SLOT % i, flag)

    return GuardResult(text=work, findings=findings)


def clean_text(text: str) -> str:
    """Just the cleaned text, for a call site with nothing to report to."""
    return clean(text).text
