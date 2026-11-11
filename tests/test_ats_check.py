"""The ATS text-layer check (tailoring/ats_check.py, issue #248).

The other three compile checks read the LaTeX log, so none of them can tell
whether the finished PDF still reads as text. These cover the fourth one:

  * every template in the library survives a read, so the check cannot start
    rolling back resumes that were always fine;
  * a date range joined by an en dash is caught, which is the failure a real
    Workday import turned into a dropped date field;
  * a ligature left in the layer is reported;
  * text that renders but never reaches the layer is reported;
  * the fold that makes the comparison forgiving does not hide a real miss.

Offline: a LaTeX compile where one is installed, placeholder profile data, and
no model (CLAUDE.md section 12).
"""

from __future__ import annotations

from pathlib import Path

from conftest import requires_latex
from tailoring.ats_check import (
    LIGATURES,
    AtsIssue,
    check_pdf,
    expected_strings,
    extract_text,
    fold,
    scan_text,
)
from tailoring.compiler import compile_tex

TEMPLATE_DIR = Path("config/templates")

# A minimal standalone document, so a case is one line of body text.
_DOC = r"""\documentclass[11pt]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\pagestyle{empty}
\begin{document}
%s
\end{document}
"""


def _kinds(issues: list[AtsIssue]) -> set[str]:
    return {i.kind for i in issues}


def _build_pdf(body: str, workdir: Path, name: str = "case"):
    result = compile_tex(_DOC % body, workdir, jobname=name)
    assert result.ok, f"fixture failed to compile: exit {result.returncode}"
    return result


def _hand_built_pdf(shown: str, mapping: dict[str, str], path: Path) -> Path:
    """A PDF whose ToUnicode map says exactly what we want the layer to carry.

    Needed because no LaTeX font we ship leaves a ligature in the text layer:
    the mapping is declared outright instead of coaxed out of a font.
    """
    bf = "".join(f"<{ord(c):02X}> <{ord(u):04X}>\n" for c, u in mapping.items())
    cmap = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            "/CMapType 2 def\n1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
            f"{len(mapping)} beginbfchar\n{bf}endbfchar\nendcmap\n"
            "CMapName currentdict /CMap defineresource pop\nend\nend\n").encode()
    content = f"BT /F1 24 Tf 72 700 Td ({shown}) Tj ET".encode()
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica/ToUnicode 6 0 R>>",
        b"<</Length %d>>stream\n" % len(content) + content + b"\nendstream",
        b"<</Length %d>>stream\n" % len(cmap) + cmap + b"\nendstream",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj" % i + body + b"endobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))
    return path


# --------------------------------------------------------------- the library

@requires_latex
def test_every_template_in_the_library_reads_back_as_text(tmp_path, monkeypatch):
    """All sixteen templates build and survive an ATS read.

    This is the check that keeps the new gate honest: if it flagged real output,
    every tailored resume would roll back to the untouched template.
    """
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as app

    monkeypatch.setattr(app, "WORKDIR", tmp_path)
    names = sorted(p.stem for p in TEMPLATE_DIR.glob("*.json"))
    assert names, "no template manifests found"

    failures = []
    for name in names:
        pdf = app._template_preview_pdf(name)
        assert pdf, f"{name}: no preview PDF was produced"
        report = check_pdf(pdf)
        if not report.ok:
            failures.append(f"{name}: {'; '.join(report.reasons())}")
    assert not failures, "templates an ATS could not read:\n" + "\n".join(failures)


# ------------------------------------------------------------ the three rules

@requires_latex
def test_an_en_dash_date_range_is_caught(tmp_path):
    """LaTeX turns "--" into U+2013; a Workday import dropped the whole date."""
    result = _build_pdf(
        r"\textbf{Risk Analyst} \hfill \textbf{April 2022 -- June 2022}", tmp_path
    )
    text = extract_text(result.pdf_path)
    assert chr(0x2013) in text, "fixture did not actually produce an en dash"

    report = check_pdf(result.pdf_path)
    assert not report.ok
    assert "unicode-dash" in _kinds(report.issues)
    # The reason names the code point rather than printing the dash itself.
    assert "U+2013" in report.reasons()[0]


@requires_latex
def test_a_plain_hyphen_date_range_passes(tmp_path):
    result = _build_pdf(
        r"\textbf{Risk Analyst} \hfill \textbf{April 2022 - June 2022}", tmp_path
    )
    assert check_pdf(result.pdf_path).ok


def test_a_ligature_in_the_layer_is_reported():
    issues = scan_text("The oﬃce ﬁle", source="")
    assert "ligature" in _kinds(issues)
    assert "U+FB01" in issues[0].detail or "U+FB03" in issues[0].detail


def test_every_ligature_we_name_is_reported():
    for ch in LIGATURES:
        assert "ligature" in _kinds(scan_text(f"a{ch}b")), f"{ch!r} not reported"


def test_pdfium_expands_ligatures_before_we_ever_see_them(tmp_path):
    """Documents why the ligature rule cannot fire through our current reader.

    The fixture's ToUnicode map declares U+FB03 outright. PDFium still hands back
    "ffi", so a PDF route can never reach the rule. If this test starts failing,
    PDFium stopped normalising and the ligature check became reachable: that is
    good news, and `test_a_ligature_in_the_layer_is_reported` already covers it.
    """
    pdf = _hand_built_pdf("A", {"A": "ﬃ"}, tmp_path / "lig.pdf")
    text = extract_text(pdf)
    assert text is not None
    assert "ffi" in text
    assert not any(c in LIGATURES for c in text)
    assert check_pdf(pdf).ok


@requires_latex
def test_text_that_never_reaches_the_layer_is_reported(tmp_path):
    """A bullet the source promises but the page does not carry."""
    result = _build_pdf(r"\begin{itemize}\item Built the pricing model\end{itemize}", tmp_path)
    source = _DOC % r"""\begin{itemize}
\item Built the pricing model
\item Led the quarterly risk review for the trading desk
\end{itemize}"""
    report = check_pdf(result.pdf_path, source)
    assert not report.ok
    assert "missing-text" in _kinds(report.issues)
    assert "quarterly risk review" in report.reasons()[0]


@requires_latex
def test_a_page_that_matches_its_source_passes(tmp_path):
    body = r"""\begin{itemize}
\item Built the pricing model for the structured products desk
\item Led the quarterly risk review across four trading teams
\end{itemize}"""
    result = _build_pdf(body, tmp_path)
    source = _DOC % body
    report = check_pdf(result.pdf_path, source)
    assert report.ok, "; ".join(report.reasons())


# ------------------------------------------------------------------- the fold

def test_the_fold_survives_hyphenation_and_ligatures_and_quotes():
    assert fold("trans-\nformation") == fold("transformation")
    assert fold("oﬃce") == fold("office")
    assert fold("don’t") == fold("don't")
    assert fold("A  B\tC") == fold("abc")


def test_the_fold_still_separates_genuinely_different_text():
    assert fold("risk modelling") != fold("credit modelling")


def test_a_missing_bullet_is_not_hidden_by_the_fold():
    source = r"\begin{itemize}\item Led the quarterly risk review\end{itemize}"
    assert "missing-text" in _kinds(scan_text("something else entirely", source))


# ---------------------------------------------------------- source extraction

def test_expected_strings_reads_bullets_headings_and_dates():
    source = r"""
\section{Experience}
\textbf{Quantitative Risk Associate} \hfill \textbf{Feb 2023 - Present}
\begin{itemize}
\item Designed a scenario tool for interest-rate shocks
\end{itemize}
"""
    found = " | ".join(expected_strings(source))
    assert "Experience" in found
    assert "Quantitative Risk Associate" in found
    assert "Feb 2023 - Present" in found
    assert "scenario tool" in found


def test_expected_strings_ignores_commented_out_source():
    source = "% \\item This bullet is commented out and never renders\n"
    assert expected_strings(source) == []


def test_expected_strings_keeps_the_label_of_a_link_not_its_target():
    source = r"\item See \href{https://example.com/very-long-target}{the project writeup}"
    found = " | ".join(expected_strings(source))
    assert "the project writeup" in found
    assert "example.com" not in found


# ----------------------------------------------------------------- the hook

@requires_latex
def test_the_compile_gate_fails_when_the_layer_is_unreadable(tmp_path):
    """A failing read is a failing compile: self-heal, else roll back (CLAUDE.md section 9).

    Both pages are one page with no overfull box, so the only thing separating
    them is the text layer.
    """
    from tailoring.self_heal import _checks

    good = _build_pdf(
        r"\textbf{Analyst} \hfill \textbf{April 2022 - June 2022}", tmp_path, "good"
    )
    bad = _build_pdf(
        r"\textbf{Analyst} \hfill \textbf{April 2022 -- June 2022}", tmp_path, "bad"
    )
    assert good.pages == bad.pages == 1

    passed, reasons, _fresh, _extra = _checks(good, good)
    assert passed, reasons

    passed, reasons, _fresh, _extra = _checks(good, bad)
    assert not passed
    assert any("ATS text layer" in r for r in reasons), reasons


@requires_latex
def test_the_gate_reads_the_source_back_from_the_compile(tmp_path):
    """The check needs no new argument: compile_tex already records its own tex_path."""
    from tailoring.self_heal import _ats_reasons

    result = _build_pdf(
        r"\begin{itemize}\item Led the quarterly risk review\end{itemize}", tmp_path, "src"
    )
    assert result.tex_path is not None and result.tex_path.exists()
    assert _ats_reasons(result) == []


def test_a_failed_compile_is_not_also_reported_as_an_ats_problem():
    """One failure, one reason: a compile that produced no PDF is already reported."""
    from tailoring.compiler import CompileResult
    from tailoring.self_heal import _ats_reasons

    assert _ats_reasons(CompileResult(ok=False, returncode=1, pages=None)) == []
    assert _ats_reasons(CompileResult(ok=True, returncode=0, pages=1, pdf_path=None)) == []


def test_an_unreadable_pdf_is_skipped_rather_than_failed(tmp_path):
    """A reader problem is ours, not the resume's: never roll back over it."""
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    report = check_pdf(broken, source="\\item anything")
    assert report.ok
    assert report.skipped
