"""Render a cover letter to a one-page PDF that visually MATCHES the tailored CV.

Reuses the CV template's exact preamble (packages, geometry, macros) and the same
name+contact letterhead, so the CV and the cover letter read as a matched set. The body
is the drafted letter text (from drafting.cover_letter), escaped for LaTeX exactly the
way CV content is. Compiled with the shared pdflatex toolchain (tailoring.compiler).
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from tailoring.assembler import _header, extract_preamble
from tailoring.compiler import compile_tex
from tailoring.conform import strip_ai_dashes
from tailoring.latex_text import escape_latex


def _today() -> str:
    d = datetime.date.today()
    return f"{d.strftime('%B')} {d.day}, {d.year}"   # no %-d: portable across OSes


def _body_to_latex(text: str) -> str:
    """Blank-line-separated paragraphs; single newlines within a block become LaTeX
    line breaks (so 'Sincerely,\\nName' signs on two lines). Every line escaped."""
    text = strip_ai_dashes((text or "").replace("\r\n", "\n").strip())
    blocks = re.split(r"\n\s*\n", text)
    rendered = []
    for blk in blocks:
        lines = [escape_latex(ln.strip()) for ln in blk.splitlines() if ln.strip()]
        if lines:
            rendered.append(" \\\\\n".join(lines))
    return "\n\n".join(rendered)


def build_cover_letter_tex(template_source: str, profile: dict, letter_text: str,
                           date_str: str | None = None) -> str:
    """The full .tex for a one-page cover letter: CV preamble + CV letterhead + date +
    the (escaped) letter body. Block-style paragraphs (no indent), like a business letter."""
    preamble = extract_preamble(template_source)
    header = _header(profile)                          # same name + contact as the CV
    body = _body_to_latex(letter_text)
    return (
        f"{preamble}\n"
        "\\setlength{\\parindent}{0pt}\n"
        "\\setlength{\\parskip}{0.7em}\n"
        f"{header}\n"
        "\\vspace{0.5cm}\n"
        f"{{\\raggedright {escape_latex(date_str or _today())}\\par}}\n"
        "\\vspace{0.35cm}\n"
        f"{body}\n"
        "\\end{document}\n"
    )


def render_cover_letter_pdf(template_source: str, profile: dict, letter_text: str,
                            workdir, jobname: str) -> Path | None:
    """Compile the cover letter to ``workdir/<jobname>.pdf`` and return its path, or None
    if there's no text or the compile fails (e.g. no LaTeX toolchain) — callers degrade
    to the plain-text cover letter gracefully."""
    if not (letter_text or "").strip():
        return None
    tex = build_cover_letter_tex(template_source, profile, letter_text)
    try:
        result = compile_tex(tex, workdir, jobname=jobname)
    except Exception:
        return None
    return result.pdf_path if (result.ok and result.pdf_path) else None
