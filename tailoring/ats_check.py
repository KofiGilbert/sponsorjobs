"""The ATS text-layer check: read the finished PDF the way an ATS reads it.

A resume can pass every check we already have (exit 0, still one page, no new
overfull box) and still arrive at the employer as noise, because all three of
those read the LaTeX log and none of them opens the PDF. The parser behind the
application form does not see the page; it sees the text layer. These three
checks each come from a real import that silently lost data:

  1. **Text that is missing from the layer.** It renders on the page, so the
     person and the recruiter both see a finished resume, while the parser gets
     a gap where a bullet or a job title should be.
  2. **A date range typeset with a Unicode dash** (U+2013, U+2014) rather than a
     plain hyphen. LaTeX turns ``--`` into an en dash; one Workday import read
     the result and dropped the entire date field.
  3. **Ligature characters** (U+FB00 to U+FB04) left in the layer. A font with no
     proper ToUnicode map writes "office" as "o\\ufb03ce", so a recruiter search
     for the word never matches it.

This module reads the text layer only. It says nothing about how the page looks:
layout stays the job of the page count and the overfull check.

**On check 3 and our current reader.** PDFium, which ``extract_text`` uses, expands
every ligature in U+FB00 to U+FB04 to its component letters while it extracts, and
it does so even when the PDF's own ToUnicode map explicitly declares the ligature
(verified against a hand-built PDF for U+FB00, U+FB01 and U+FB03: all three come
back as plain letters). So the ligature check cannot fire through this reader
today. It is implemented and tested at ``scan_text`` anyway, because the scan is
the part that encodes what is wrong, and a later reader, or a PDFium that stops
normalising, makes it live without another change here.

The comparison is deliberately forgiving. Both sides are folded to lowercase
letters and digits before matching, so hyphenation at a line break, a changed
space, a typographic quote or an expanded ligature can never raise a false
alarm. That bias is on purpose: the caller treats a failure here like a compile
failure and rolls the resume back, so missing a subtle problem is much cheaper
than discarding a page that was fine.

Pure except for reading the PDF: no model, no network.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# The five Latin ligatures a text layer can carry, and what each one should read as.
LIGATURES: dict[str, str] = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
}
# The two dashes LaTeX produces from "--" and "---". Named, never written literally,
# because user-facing text in this project does not use them (AGENTS.md).
DASHES: dict[str, str] = {"\u2013": "U+2013", "\u2014": "U+2014"}

# A fold shorter than this is not worth checking: a two-letter link label ("In")
# proves nothing, and anything that short is found by chance in any page of text.
MIN_FOLDED = 6
# At most this many missing fragments are named in one reason line.
_MAX_NAMED = 3

_YEAR = r"(?:19|20)\d{2}"
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
_DASH_CLASS = "[\u2013\u2014]"
# A date range: a year or a month name on either side of one of the two dashes.
_DASH_DATE = re.compile(
    rf"(?:{_YEAR}|{_MONTH})\s*{_DASH_CLASS}|{_DASH_CLASS}\s*(?:{_YEAR}|{_MONTH})",
    re.IGNORECASE,
)

# LaTeX source parsing. Comments are dropped first so a commented-out bullet is
# never expected in the output.
_COMMENT = re.compile(r"(?<!\\)%[^\n]*")
# Commands whose argument is content we expect to see rendered.
_HEADING_CMD = re.compile(r"\\([A-Za-z@]*(?:section|heading|chapter)[A-Za-z@]*)\*?\s*(?=\{)",
                          re.IGNORECASE)
_TEXT_CMD = re.compile(r"\\(textbf|textit|emph|underline|uline)\s*(?=\{)")
# A size group such as "{\Huge Jonathan Dobson}" carries the name on most templates.
_SIZE_GROUP = re.compile(r"\{\s*\\(?:Huge|huge|LARGE|Large|large)\b")
_ITEM = re.compile(r"\\item\b")
# Where a bullet interior ends: the next item, the end of its list, or a blank line.
_ITEM_END = re.compile(r"\\item\b|\\end\s*\{|\n\s*\n")
_HREF = re.compile(r"\\(?:href|hyperlink|hyperref)\s*\{")
_CMD = re.compile(r"\\[A-Za-z@]+\*?")
_OPT = re.compile(r"\[[^\]\n]*\]")
_UNESCAPE = (
    (r"\&", "&"), (r"\%", "%"), (r"\$", "$"), (r"\#", "#"),
    (r"\_", "_"), (r"\{", "{"), (r"\}", "}"),
)


@dataclass
class AtsIssue:
    """One reason this PDF would not survive an ATS import."""

    kind: str      # "missing-text" | "unicode-dash" | "ligature" | "no-text"
    detail: str


@dataclass
class AtsReport:
    ok: bool
    issues: list[AtsIssue] = field(default_factory=list)
    text: str = ""
    skipped: str = ""      # set when the check could not run at all

    def reasons(self) -> list[str]:
        """The issue details, phrased for the compile-check reason list."""
        return [f"ATS text layer: {i.detail}" for i in self.issues]


def extract_text(pdf: str | Path) -> str | None:
    """The PDF's text layer, or None when it cannot be read at all.

    None means "do not judge this PDF" (pypdfium2 missing, or the file will not
    open); an empty string means the file opened and carries no text, which is a
    finding, not an absence of one.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    try:
        doc = pdfium.PdfDocument(str(pdf))
    except Exception:
        return None
    try:
        return "\n".join(
            doc[i].get_textpage().get_text_range() for i in range(len(doc))
        )
    except Exception:
        return None
    finally:
        try:
            doc.close()
        except Exception:
            pass


def fold(text: str) -> str:
    """Lowercase letters and digits only, with ligatures expanded.

    Everything a renderer is free to change between the source and the page is
    thrown away here: spacing, line breaks, the hyphen LaTeX inserts when it
    breaks a word, quote style, and ligature glyphs.
    """
    s = unicodedata.normalize("NFKC", text or "")
    for lig, plain in LIGATURES.items():
        s = s.replace(lig, plain)
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _group(src: str, open_at: int) -> tuple[str, int]:
    """Contents of the brace group at ``open_at``, and the index just past it."""
    depth = 0
    i = open_at
    while i < len(src):
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[open_at + 1:i], i + 1
        i += 1
    return src[open_at + 1:], len(src)


def _plain(fragment: str) -> str:
    """A LaTeX fragment reduced to the words it renders."""
    s = _COMMENT.sub("", fragment or "")
    # A link renders its second argument, so keep that and drop the target.
    while True:
        m = _HREF.search(s)
        if not m:
            break
        _target, after = _group(s, m.end() - 1)
        if after < len(s) and s[after] == "{":
            label, end = _group(s, after)
            s = s[:m.start()] + label + s[end:]
        else:
            s = s[:m.start()] + s[after:]
    for esc, plain in _UNESCAPE:
        s = s.replace(esc, "\x00" + plain)       # park it so _CMD cannot eat it
    s = s.replace("\\\\", " ").replace("~", " ")
    s = _CMD.sub(" ", s)
    s = _OPT.sub(" ", s)
    s = s.replace("{", " ").replace("}", " ")
    s = s.replace("\x00", "")
    return " ".join(s.split())


def expected_strings(source: str) -> list[str]:
    """The bullets, headings, titles and dates a compile of ``source`` should render."""
    src = _COMMENT.sub("", source or "")
    out: list[str] = []

    for m in _ITEM.finditer(src):
        end = _ITEM_END.search(src, m.end())
        out.append(_plain(src[m.end():end.start() if end else len(src)]))

    for pattern in (_HEADING_CMD, _TEXT_CMD):
        for m in pattern.finditer(src):
            body, _ = _group(src, m.end())
            out.append(_plain(body))

    for m in _SIZE_GROUP.finditer(src):
        body, _ = _group(src, m.start())
        out.append(_plain(body))

    seen: set[str] = set()
    keep: list[str] = []
    for frag in out:
        key = fold(frag)
        if len(key) < MIN_FOLDED or key in seen:
            continue
        seen.add(key)
        keep.append(frag)
    return keep


def _quote(match: str) -> str:
    """A matched run, with each Unicode dash named instead of written."""
    for dash, name in DASHES.items():
        match = match.replace(dash, f"[{name}]")
    return " ".join(match.split())


def scan_text(text: str, source: str = "") -> list[AtsIssue]:
    """Everything wrong with an already-extracted text layer.

    Split out from :func:`check_pdf` so the rules can be exercised directly, with
    no PDF and no reader in the way.
    """
    issues: list[AtsIssue] = []

    ligs = sorted({c for c in text if c in LIGATURES})
    if ligs:
        named = ", ".join(f"U+{ord(c):04X} for {LIGATURES[c]}" for c in ligs)
        issues.append(AtsIssue(
            "ligature",
            f"{len(ligs)} ligature character(s) a parser cannot read as letters ({named})",
        ))

    dates = [_quote(m.group(0)) for m in _DASH_DATE.finditer(text)]
    if dates:
        shown = ", ".join(f'"{d}"' for d in dates[:_MAX_NAMED])
        more = f" and {len(dates) - _MAX_NAMED} more" if len(dates) > _MAX_NAMED else ""
        issues.append(AtsIssue(
            "unicode-dash",
            f"{len(dates)} date range(s) joined by a Unicode dash instead of a "
            f"plain hyphen ({shown}{more})",
        ))

    wanted = expected_strings(source) if source else []
    if wanted:
        haystack = fold(text)
        missing = [w for w in wanted if fold(w) not in haystack]
        if missing:
            shown = ", ".join(f'"{m[:60]}"' for m in missing[:_MAX_NAMED])
            more = f" and {len(missing) - _MAX_NAMED} more" if len(missing) > _MAX_NAMED else ""
            issues.append(AtsIssue(
                "missing-text",
                f"{len(missing)} of {len(wanted)} rendered fragment(s) missing from "
                f"the text layer ({shown}{more})",
            ))
    elif source and not fold(text):
        issues.append(AtsIssue("no-text", "the page carries no extractable text at all"))

    return issues


def check_pdf(pdf: str | Path, source: str = "") -> AtsReport:
    """Read ``pdf`` as an ATS would and report what would not survive the import.

    ``source`` is the LaTeX that produced it. Without it the missing-text check
    is skipped and only the dash and ligature checks run, which need no source.
    """
    text = extract_text(pdf)
    if text is None:
        return AtsReport(ok=True, skipped="the PDF text layer could not be read")
    issues = scan_text(text, source)
    return AtsReport(ok=not issues, issues=issues, text=text)
