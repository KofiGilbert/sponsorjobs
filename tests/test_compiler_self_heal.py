"""§10: one-page preservation, self-heal, and broken-LaTeX rollback.

Covers:
  * "Tailored PDF is still one page with no new overfull warnings."
  * "Deliberately broken LaTeX is rejected and rolled back to a valid PDF."
  * The §9 invariant: never ship an output that hasn't passed all three checks.

These require a real pdflatex/MiKTeX toolchain and are skipped otherwise.
"""

from __future__ import annotations

import pytest

from tailoring.compiler import compile_tex
from tailoring.latex_template import LatexTemplate
from tailoring.self_heal import self_heal_compile
from tailoring.tailor import tailor_resume
from conftest import requires_latex


@requires_latex
def test_baseline_template_is_one_page(template_source, workdir):
    base = compile_tex(template_source, workdir, jobname="baseline")
    assert base.ok
    assert base.pages == 1
    assert base.pdf_path is not None and base.pdf_path.exists()


@requires_latex
def test_tailored_pdf_is_one_page_no_new_overfull(
    template_source, sample_profile, jd_quant, fake_llm, workdir
):
    baseline = compile_tex(template_source, workdir, jobname="resume_baseline")
    res = tailor_resume(
        template_source, sample_profile, jd_quant, fake_llm, workdir
    )
    assert res.ok, res.summary()
    assert res.status == "clean"
    assert res.heal.compile.pages == baseline.pages == 1
    # No overfull hbox present in the tailored output beyond the baseline's set.
    new_overfull = res.heal.compile.overfull_count - baseline.overfull_count
    assert new_overfull <= 0
    assert res.pdf_path is not None and res.pdf_path.exists()


@requires_latex
def test_self_heal_shortens_overflow_back_to_one_page(
    template_source, fake_llm, workdir
):
    """A too-long edit overflows to a second page; the loop shrinks it back."""
    tmpl = LatexTemplate(template_source)
    baseline = compile_tex(template_source, workdir, jobname="ov_baseline")
    assert baseline.pages == 1

    victim = max(tmpl.tailorable_bullets(), key=lambda b: b.length)
    # ~2500 chars of real words -> clearly spills onto a second page.
    huge = (victim.text + " ") * 12
    edits = {victim.id: huge.strip()}

    heal = self_heal_compile(
        template=tmpl,
        edits=edits,
        baseline=baseline,
        workdir=workdir,
        llm=fake_llm,
        max_retries=8,
        shrink_factor=0.6,
        jobname="ov",
    )

    # It must converge to a clean one-page result...
    assert heal.ok, [s.detail for s in heal.steps]
    assert heal.status == "clean"
    assert heal.compile.pages == 1
    # ...having actually shortened the offending bullet across >1 attempt.
    assert len(heal.steps) > 1
    assert len(heal.final_edits[victim.id]) < len(huge.strip())


@requires_latex
def test_never_ships_failing_output_invariant(
    template_source, sample_profile, jd_swe, fake_llm, workdir
):
    """Whenever a run reports ok, the PDF genuinely passes all three checks."""
    baseline = compile_tex(template_source, workdir, jobname="inv_baseline")
    res = tailor_resume(
        template_source, sample_profile, jd_swe, fake_llm, workdir,
        jobname="inv",
    )
    if res.ok:
        assert res.heal.compile.pages == baseline.pages
        new_overfull = res.heal.compile.overfull_count - baseline.overfull_count
        assert new_overfull <= 0
        assert res.pdf_path and res.pdf_path.exists()


@requires_latex
def test_broken_latex_is_rejected_and_rolled_back(
    template_source, fake_llm, workdir
):
    """§10: a candidate that can't compile is rejected and rolled back.

    Feed a genuinely broken bullet (an undefined control sequence) straight into
    the self-heal loop. Shortening can't repair a syntax error, so the loop must
    exhaust its retries and roll back to the valid template — never shipping a
    broken or two-page PDF.
    """
    tmpl = LatexTemplate(template_source)
    baseline = compile_tex(template_source, workdir, jobname="broken_baseline")
    assert baseline.pages == 1

    victim = tmpl.tailorable_bullets()[0]
    broken = {victim.id: r"\thiscommanddoesnotexistxyz cannot compile at all"}

    heal = self_heal_compile(
        template=tmpl,
        edits=broken,
        baseline=baseline,
        workdir=workdir,
        llm=fake_llm,          # its shorten can't fix a syntax error
        max_retries=4,
        jobname="broken",
    )

    assert heal.ok is False
    assert heal.status == "rolled_back"
    # Rolled back to the valid template: a real one-page PDF still exists.
    assert heal.compile.pages == 1
    assert heal.pdf_path is not None and heal.pdf_path.exists()
    assert heal.final_source == template_source
    assert heal.final_edits == {}


class _RogueCommandLLM:
    """Emits a stray LaTeX command in every bullet — the pipeline must neutralize
    it rather than ship a broken PDF."""

    def generate_intake_questions(self, jd_text, saved_profile):
        return []

    def reword_bullet(self, original_text, supported_jd_terms, target_len_chars, jd_text):
        return r"\thiscommanddoesnotexistxyz should become literal text"

    def shorten_bullet(self, text, max_len_chars):
        return text[: max(max_len_chars, 20)]


@requires_latex
def test_rogue_latex_from_llm_is_escaped_not_shipped_broken(
    template_source, sample_profile, jd_quant, workdir
):
    """Defense-in-depth: stray LaTeX from the model is escaped to literal text,
    so the tailored PDF is still valid and one page (no rollback needed)."""
    res = tailor_resume(
        template_source, sample_profile, jd_quant, _RogueCommandLLM(), workdir,
        jobname="rogue",
    )
    # Whatever happens, the shipped artifact is a valid one-page PDF.
    assert res.pdf_path is not None and res.pdf_path.exists()
    assert res.heal.compile.pages == 1
    # The rogue command was neutralized, not passed through raw.
    assert r"\thiscommanddoesnotexistxyz" not in res.tex_source


def test_find_pdflatex_checks_standard_dirs_when_path_is_minimal(monkeypatch, tmp_path):
    """A macOS app launched from Finder gets launchd's bare PATH, which excludes MacTeX's
    /Library/TeX/texbin and Homebrew. The finder must still locate a standard install."""
    import tailoring.compiler as compiler

    monkeypatch.delenv("PDFLATEX", raising=False)
    monkeypatch.setattr(compiler.shutil, "which", lambda name: None)
    monkeypatch.setattr(compiler.Path, "exists",
                        lambda self: str(self) == "/Library/TeX/texbin/pdflatex")
    assert compiler.find_pdflatex() == "/Library/TeX/texbin/pdflatex"
