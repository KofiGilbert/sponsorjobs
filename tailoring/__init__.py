"""The tailoring engine — the spine of the product (CLAUDE.md §8, Phase 1).

Public surface:

* :class:`LatexTemplate` / :class:`Bullet` — parse a template into addressable
  bullets.
* :func:`apply_edits` / :func:`verify_confined` — surgical, confinement-checked
  edits.
* :func:`compile_tex` — compile and extract page count + overfull warnings.
* :func:`self_heal_compile` — iterate-until-clean, else roll back.
* :func:`tailor_resume` — the full orchestrator with a coverage report.
"""

from .latex_template import Bullet, LatexTemplate
from .surgical_editor import apply_edits, verify_confined, unified_diff
from .compiler import compile_tex, CompileResult, Overfull
from .self_heal import self_heal_compile, HealResult
from .keywords import (
    build_coverage_report,
    extract_jd_terms,
    supported_terms,
    CoverageReport,
)
from .tailor import tailor_resume, TailorResult

__all__ = [
    "Bullet",
    "LatexTemplate",
    "apply_edits",
    "verify_confined",
    "unified_diff",
    "compile_tex",
    "CompileResult",
    "Overfull",
    "self_heal_compile",
    "HealResult",
    "build_coverage_report",
    "extract_jd_terms",
    "supported_terms",
    "CoverageReport",
    "tailor_resume",
    "TailorResult",
]
