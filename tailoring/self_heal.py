"""The self-healing compile loop (CLAUDE.md §9).

Given a template, a set of proposed bullet edits, and a baseline compile of the
untouched template, iterate:

  splice -> compile -> check (exit 0, page count unchanged, no NEW overfull hbox)
  -> if failing, identify the offending bullet(s), shorten them, recompile
  -> cap retries; on exhaustion roll back to the last known-good source.

We never ship an output that hasn't passed all three checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from llm.base import LLMBackend
from .compiler import CompileResult, compile_tex
from .latex_template import Bullet, LatexTemplate


@dataclass
class HealStep:
    attempt: int
    action: str            # "initial", "shorten", "rollback"
    detail: str
    pages: int | None
    overfull_new: int
    ok: bool


@dataclass
class HealResult:
    ok: bool
    status: str                      # "clean", "rolled_back"
    final_source: str
    final_edits: dict[str, str]
    compile: CompileResult
    steps: list[HealStep] = field(default_factory=list)
    baseline_pages: int | None = None

    @property
    def pdf_path(self) -> Path | None:
        return self.compile.pdf_path if self.compile else None


def _new_overfulls(baseline: CompileResult, candidate: CompileResult) -> list:
    """Overfull hboxes present in the candidate but not the baseline."""
    base_keys = baseline.overfull_keys
    fresh = [o for o in candidate.overfulls if o.key() not in base_keys]
    # If the candidate reports more overfulls than the baseline but some lack a
    # line range (so they're not in `overfulls`), treat the surplus as new too.
    surplus = candidate.overfull_count - baseline.overfull_count - len(
        [o for o in candidate.overfulls if o.key() not in base_keys]
    )
    return fresh, max(surplus, 0)


def _checks(baseline: CompileResult, candidate: CompileResult):
    """Return (passed, reasons, fresh_overfulls, extra_overfull_count)."""
    reasons: list[str] = []
    if not candidate.ok:
        reasons.append(f"compile failed (exit {candidate.returncode})")
    if candidate.pages is None:
        reasons.append("could not determine page count")
    elif candidate.pages != 1:
        # Absolute one-page floor (§8: "the page count is unchanged (still one page)").
        # Enforced independently of the baseline, so a 2-page candidate can never slip
        # through when the baseline log was degraded and its own page count is unknown.
        was = f" (was {baseline.pages})" if baseline.pages is not None else " (baseline unknown)"
        reasons.append(f"not one page: {candidate.pages}{was}")
    fresh, extra = _new_overfulls(baseline, candidate)
    if fresh or extra:
        reasons.append(f"{len(fresh) + extra} new overfull hbox warning(s)")
    return (not reasons), reasons, fresh, extra


def _offending_bullets(
    template: LatexTemplate,
    current_source: str,
    edits: dict[str, str],
    fresh_overfulls: list,
) -> list[str]:
    """Pick which edited bullets to shorten this round.

    Prefer bullets whose *current* line span overlaps a fresh overfull warning.
    If the failure is a page-count overflow with no attributable overfull, fall
    back to the longest edited bullets (they cost the most vertical space).
    """
    edited_ids = list(edits)
    if fresh_overfulls:
        # Map current line spans of edited bullets, using the current source.
        line_map: list[tuple[str, int, int]] = []
        for bid in edited_ids:
            b = template.get(bid)
            # Recompute span against the CURRENT (edited) source, since offsets
            # shift after edits.
            first, last = _current_line_span(current_source, edits[bid], b)
            line_map.append((bid, first, last))
        hit: list[str] = []
        for o in fresh_overfulls:
            for bid, first, last in line_map:
                if first <= o.line_end and o.line_start <= last:
                    if bid not in hit:
                        hit.append(bid)
        if hit:
            return hit
    # Fallback: the longest current edits first.
    return sorted(edited_ids, key=lambda b: len(edits[b]), reverse=True)


def _current_line_span(current_source: str, edit_text: str, b: Bullet) -> tuple[int, int]:
    """Best-effort line span of an edited bullet within the current source."""
    needle = edit_text.strip()[:40]
    idx = current_source.find(needle) if needle else -1
    if idx == -1:
        return (0, 0)
    first = current_source.count("\n", 0, idx) + 1
    last = current_source.count("\n", 0, idx + len(edit_text)) + 1
    return first, last


def self_heal_compile(
    template: LatexTemplate,
    edits: dict[str, str],
    baseline: CompileResult,
    workdir: str | Path,
    llm: LLMBackend,
    max_retries: int = 4,
    shrink_factor: float = 0.85,
    jobname: str = "resume",
) -> HealResult:
    """Iterate until the tailored résumé is clean, else roll back.

    ``baseline`` is a prior successful compile of the *untouched* template — it
    defines the target page count and the set of pre-existing overfulls.
    """
    workdir = Path(workdir)
    steps: list[HealStep] = []
    current_edits = dict(edits)

    for attempt in range(max_retries + 1):
        source = template.render(current_edits)
        result = compile_tex(source, workdir, jobname=jobname)
        passed, reasons, fresh, extra = _checks(baseline, result)
        steps.append(
            HealStep(
                attempt=attempt,
                action="initial" if attempt == 0 else "shorten",
                detail="; ".join(reasons) if reasons else "clean",
                pages=result.pages,
                overfull_new=len(fresh) + extra,
                ok=passed,
            )
        )
        if passed:
            return HealResult(
                ok=True,
                status="clean",
                final_source=source,
                final_edits=current_edits,
                compile=result,
                steps=steps,
                baseline_pages=baseline.pages,
            )
        if attempt == max_retries:
            break

        # Diagnose and shorten the offending bullet(s).
        targets = _offending_bullets(template, source, current_edits, fresh)
        if not targets:
            break  # nothing we can adjust — give up and roll back
        changed_any = False
        for bid in targets:
            cur = current_edits[bid]
            new_len = max(int(len(cur) * shrink_factor), 20)
            if new_len >= len(cur):
                new_len = len(cur) - 5
            if new_len <= 0:
                continue
            shorter = llm.shorten_bullet(cur, new_len)
            if shorter and shorter != cur:
                current_edits[bid] = shorter
                changed_any = True
        if not changed_any:
            break  # can't shrink further (e.g. a LaTeX syntax error) -> roll back

    # Exhausted retries or unfixable: roll back to the known-good template.
    rollback_source = template.source
    rollback = compile_tex(rollback_source, workdir, jobname=jobname)
    steps.append(
        HealStep(
            attempt=len(steps),
            action="rollback",
            detail="rolled back to last known-good template",
            pages=rollback.pages,
            overfull_new=0,
            ok=rollback.ok,
        )
    )
    return HealResult(
        ok=False,
        status="rolled_back",
        final_source=rollback_source,
        final_edits={},
        compile=rollback,
        steps=steps,
        baseline_pages=baseline.pages,
    )
