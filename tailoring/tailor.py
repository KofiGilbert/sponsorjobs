"""Top-level tailoring orchestrator (CLAUDE.md §8/§9/§10).

Pull the tailorable bullets from the TEMPLATE, reword them toward the JOB using
only PROFILE-supported terms, run the self-healing compile loop, and emit the
tailored PDF alongside a JD keyword coverage report.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from llm.base import LLMBackend
from .compiler import CompileResult, compile_tex
from .keywords import CoverageReport, build_coverage_report, supported_terms
from .latex_template import LatexTemplate
from .latex_text import SanitizingLLM
from .self_heal import HealResult, self_heal_compile
from .surgical_editor import unified_diff, verify_confined

# Tailoring is re-emphasis, not expansion: keep each rewrite within ~±10% of the
# original bullet length (CLAUDE.md §8, "Per-bullet length budget").
LENGTH_BUDGET = 1.10


@dataclass
class TailorResult:
    ok: bool
    status: str                    # "clean" | "rolled_back"
    tex_source: str
    pdf_path: Path | None
    coverage: CoverageReport
    heal: HealResult
    diff: str
    edits: dict[str, str]

    def summary(self) -> str:
        lines = [
            f"Status: {self.status}",
            f"Bullets tailored: {len(self.edits)}",
            f"Compile: {'clean' if self.ok else 'ROLLED BACK'} "
            f"(pages={self.heal.compile.pages}, "
            f"retries={len(self.heal.steps) - 1})",
            "",
            self.coverage.render(),
        ]
        return "\n".join(lines)


def tailor_resume(
    template_source: str,
    profile: dict,
    jd_text: str,
    llm: LLMBackend,
    workdir: str | Path,
    sections: set[str] | None = None,
    max_retries: int = 4,
    jobname: str = "resume",
) -> TailorResult:
    """Tailor ``template_source`` to ``jd_text`` from ``profile``.

    Returns a :class:`TailorResult`. Guarantees (per §10): edits are confined to
    bullet interiors, the shipped PDF is one page with no new overfull warnings,
    and a coverage report is always attached. On unrecoverable failure the result
    is the rolled-back (valid) template rather than a broken PDF.
    """
    workdir = Path(workdir)
    template = LatexTemplate(template_source)

    # Escape every value the LLM returns so real prose (with %, &, $, ...) can't
    # break the compile; also feed the model clean, unescaped originals.
    llm = SanitizingLLM(llm)

    # Baseline compile of the untouched template defines page count + overfulls.
    baseline = compile_tex(template_source, workdir, jobname=f"{jobname}_baseline")

    # Which JD terms does the profile genuinely support? Only those get woven in.
    supported = supported_terms(jd_text, profile)

    # Reword each tailorable bullet within the per-bullet length budget.
    edits: dict[str, str] = {}
    for b in template.tailorable_bullets(sections):
        budget = int(len(b.text) * LENGTH_BUDGET)
        new_text = llm.reword_bullet(
            original_text=b.text,
            supported_jd_terms=supported,
            target_len_chars=budget,
            jd_text=jd_text,
        ).strip()
        # Enforce the length budget defensively regardless of the LLM.
        if len(new_text) > budget:
            new_text = llm.shorten_bullet(new_text, budget)
        if new_text and new_text != b.text:
            edits[b.id] = new_text

    # Prove confinement before we even compile (fail loud on a bad edit map).
    edited_source = template.render(edits)
    confined = verify_confined(template, edited_source, edits)
    if not confined:
        raise AssertionError(
            "surgical-edit invariant violated: " + "; ".join(confined.violations)
        )

    # Self-heal until clean, or roll back.
    heal = self_heal_compile(
        template=template,
        edits=edits,
        baseline=baseline,
        workdir=workdir,
        llm=llm,
        max_retries=max_retries,
        jobname=jobname,
    )

    final_edits = heal.final_edits
    cv_text = template.text_of_bullets(final_edits) if final_edits else \
        template.text_of_bullets()
    coverage = build_coverage_report(jd_text, cv_text, profile)
    diff = unified_diff(template_source, heal.final_source)

    return TailorResult(
        ok=heal.ok,
        status=heal.status,
        tex_source=heal.final_source,
        pdf_path=heal.pdf_path,
        coverage=coverage,
        heal=heal,
        diff=diff,
        edits=final_edits,
    )
