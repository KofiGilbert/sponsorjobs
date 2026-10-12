r"""The "Add this keyword" action: one missing JD term, one honest, surgical edit.

The coverage report lists the JD terms the resume does not show yet. This module turns one
of them into a concrete, reviewable change, the way Rezi's keyword targeting and Huntr's
"Improve" do, but under this project's rules (CLAUDE.md §8):

* A term may only be added when the person's own PROFILE genuinely supports it, and the
  support must sit in the SAME role or project as the bullet that changes (issue #279: a
  skill that is real in a side project must not migrate onto a banking job). The model
  names its evidence; we verify that evidence is a verbatim piece of the person's material
  and that it belongs to the target entry. No evidence, no edit.
* The draft is hard-validated before anyone sees it: the term must be present, the
  rewrite must stay within the bullet's length budget (about plus or minus ten percent),
  every figure must trace back to the person's material (tailoring/fact_gate.py), no other
  unsupported skill may ride along (keywords.introduced_skills), and no filler padding
  (tailoring/filler.py).
* Applying goes through the surgical editor and a self-healing compile: the edit touches
  only that bullet's interior, the page must still be one page with no new overfull line
  and not flush to the bottom edge, and a candidate that cannot be made to fit is rolled
  back so the person never receives a broken or two-page PDF (§9).
* Everything is deterministic except the single model call, so the offline suite can
  prove every rule with the FakeLLM test double.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .compiler import CompileResult, compile_tex
from .fact_gate import figures, source_figures
from .filler import is_padded
from .keywords import _grounded, introduced_skills, profile_text, term_present
from .latex_template import LatexTemplate
from .latex_text import unescape_latex
from .self_heal import _checks
from .surgical_editor import unified_diff, verify_confined

# Per-bullet length budget (CLAUDE.md §8): a rewrite stays within about ten percent of the
# original. A short bullet is allowed just enough extra room to hold the term itself, since
# "natural language processing" cannot fit inside ten percent of a forty-character line.
LENGTH_BUDGET = 1.10
LOWER_BUDGET = 0.90
# A NEW bullet is one full template line at most (assembler.FULL_LINE_CHARS) and never a stub.
NEW_BULLET_MAX = 165
NEW_BULLET_MIN = 40
# How much of the text block one extra bullet line needs (a 10pt line with its item
# spacing on a 775pt block is about two percent). The page has room for a new bullet only
# when the measured fill leaves at least this much below assembler.MAX_FILL.
ROOM_FOR_ONE_LINE = 0.025
MAX_FILL = 0.97

# The sections whose bullets the action may touch: the same content sections the tailoring
# pass rewords. Education "Courses:" lines and skills lines are not bullets of a role.
_SECTIONS = ("projects", "experience", "extracurricular")

# The fill probe the assembler uses, re-attached here so every compile in the loop measures
# how full the page is (a "one page" that runs off the bottom edge is still an overflow).
_FILL_PROBE = r"\AtEndDocument{\par\typeout{TAILORFILL=\the\pagetotal:\the\textheight}}"
_DOC_START = r"\begin{document}"

_WS = re.compile(r"\s+")
_BOLD = re.compile(r"\\textbf\{([^{}]*)\}")


# --------------------------------------------------------------------------- #
# Candidates: the bullets on the page, each mapped back to its profile slot.
# --------------------------------------------------------------------------- #

@dataclass
class Candidate:
    id: str            # LatexTemplate bullet id ("experience-3")
    ref: str           # profile slot ref, same scheme as ui.session.WebIntake._bullet_slots
    section: str       # "experience" | "projects" | "extracurricular"
    label: str         # "Acme Analytics, Data Engineer"
    text: str          # the bullet as plain prose (the profile's text)
    grounding: str     # everything the bullet's OWN entry says (title, org, tech, siblings)
    entry: dict = field(repr=False)
    bullets: list = field(repr=False)
    index: int = 0

    def public(self) -> dict:
        return {"id": self.id, "ref": self.ref, "section": self.section,
                "label": self.label, "text": self.text}


def _norm(text: str) -> str:
    return _WS.sub(" ", str(text or "")).strip().lower()


def _plain_interior(tex_text: str) -> str:
    """A rendered ``\\item`` interior back to the prose it was rendered from."""
    out = _BOLD.sub(r"\1", tex_text)
    return _norm(unescape_latex(out))


def _entry_label(entry: dict, role: dict | None = None) -> str:
    """"Acme Analytics, Data Engineer": the employer or project, then the role title."""
    org = str(entry.get("org") or entry.get("name") or "").strip()
    title = str((role if role is not None else entry).get("title") or "").strip()
    if not org:
        org, title = title, ""
    return ", ".join(x for x in (org, title) if x) or "Entry"


def profile_slots(profile: dict) -> list[dict]:
    """Every editable bullet in ``profile`` with its ref, label, grounding and container.

    The ref scheme is the one the live session uses for bullet edits (``g{gi}-r{ri}-b{bi}``
    for projects then experience, ``x{xi}-b{bi}`` for extracurricular), so an action
    recorded against a ref can be mirrored into the profile the page was rendered from."""
    out: list[dict] = []
    projects = profile.get("projects") or []
    experience = profile.get("experience") or []
    for gi, e in enumerate(list(projects) + list(experience)):
        if not isinstance(e, dict):
            continue
        section = "projects" if gi < len(projects) else "experience"
        grounding = profile_text(e)
        roles = e.get("roles") or [e]
        for ri, r in enumerate(roles):
            bl = r.get("bullets") if isinstance(r, dict) else None
            if not isinstance(bl, list):
                continue
            for bi in range(len(bl)):
                out.append({"ref": f"g{gi}-r{ri}-b{bi}", "section": section,
                            "label": _entry_label(e, r), "text": str(bl[bi]),
                            "grounding": grounding, "entry": e, "bullets": bl, "index": bi})
    for xi, it in enumerate(profile.get("extracurricular") or []):
        if not isinstance(it, dict):
            continue
        bl = it.get("bullets")
        if not isinstance(bl, list):
            continue
        grounding = profile_text(it)
        for bi in range(len(bl)):
            out.append({"ref": f"x{xi}-b{bi}", "section": "extracurricular",
                        "label": str(it.get("title") or "Activity"), "text": str(bl[bi]),
                        "grounding": grounding, "entry": it, "bullets": bl, "index": bi})
    return out


def candidates(tex_source: str, profile: dict) -> list[Candidate]:
    """The bullets on the page that the action may rewrite, in page order.

    A ``\\item`` interior is matched back to the profile bullet it was rendered from by its
    prose (escaping and metric bolding undone). An interior with no profile twin (a courses
    line, a bullet the person typed straight into the .tex) is not a candidate."""
    tmpl = LatexTemplate(tex_source)
    slots = profile_slots(profile)
    by_text: dict[str, list[dict]] = {}
    for s in slots:
        by_text.setdefault(_norm(s["text"]), []).append(s)
    out: list[Candidate] = []
    for b in tmpl.bullets:
        pool = by_text.get(_plain_interior(b.text))
        if not pool:
            continue
        s = pool.pop(0)
        out.append(Candidate(id=b.id, ref=s["ref"], section=s["section"], label=s["label"],
                             text=s["text"], grounding=s["grounding"], entry=s["entry"],
                             bullets=s["bullets"], index=s["index"]))
    return out


def entries_for_model(cands: list[Candidate]) -> list[dict]:
    """Candidates grouped by role/project, the shape the model is shown."""
    groups: list[dict] = []
    seen: dict[int, dict] = {}
    for c in cands:
        g = seen.get(id(c.entry))
        if g is None:
            g = {"label": c.label, "section": c.section, "context": c.grounding, "bullets": []}
            seen[id(c.entry)] = g
            groups.append(g)
        g["bullets"].append({"id": c.id, "text": c.text})
    return groups


def has_room(fill_ratio: float | None) -> bool:
    """Whether the measured page can take one more bullet line without overflowing."""
    return fill_ratio is not None and fill_ratio + ROOM_FOR_ONE_LINE <= MAX_FILL


# --------------------------------------------------------------------------- #
# Validation of what the model proposes.
# --------------------------------------------------------------------------- #

def undash(text: str) -> str:
    """Model prose sometimes carries em or en dashes; the product never shows them."""
    text = re.sub(r"\s*\u2014\s*", ", ", str(text or ""))
    text = re.sub(r"(\d)\s*\u2013\s*(\d)", r"\1-\2", text)
    text = re.sub(r"\s*\u2013\s*", ", ", text)
    return _WS.sub(" ", text).strip()


def rewrite_budget(before: str, term: str) -> tuple[int, int]:
    """(min, max) characters for a rewrite of ``before`` that works ``term`` in."""
    n = len(before)
    hi = max(int(n * LENGTH_BUDGET), n + len(term) + 2)
    return int(n * LOWER_BUDGET), hi


def quote_in(quote: str, haystack: str) -> bool:
    """Is ``quote`` a verbatim piece of ``haystack`` (whitespace and case aside)?"""
    q = _norm(quote).strip(" .;,:\"'")
    return bool(q) and q in _norm(haystack)


def validate_draft(term: str, draft: str, before: str, kind: str, profile: dict,
                   grounding: str, jd_text: str) -> list[str]:
    """Every rule the draft must pass, as plain reasons. Empty means it passes."""
    problems: list[str] = []
    draft = str(draft or "").strip()
    if not draft:
        return ["The model returned an empty draft."]
    if "\\" in draft or "{" in draft or "}" in draft:
        problems.append("The draft contains LaTeX markup; bullets are prose only.")
    if not term_present(term, draft):
        problems.append(f"The draft does not contain \"{term}\".")
    if kind == "new":
        lo, hi = NEW_BULLET_MIN, NEW_BULLET_MAX
    else:
        lo, hi = rewrite_budget(before, term)
    if len(draft) > hi:
        problems.append(f"The draft is {len(draft)} characters; the budget is {hi}.")
    elif len(draft) < lo:
        problems.append(f"The draft is {len(draft)} characters; it should keep at least {lo}.")
    # Numeric fact gate: no figure the person never supplied.
    new_figures = sorted(figures(draft) - source_figures(profile), key=lambda s: (len(s), s))
    if new_figures:
        problems.append("The draft adds a figure your profile never states: "
                        + ", ".join(new_figures) + ".")
    # Honesty gate shared with the tailoring pass: the term itself is vouched for by the
    # verified evidence; anything else that is new must already be in this entry.
    extra = [s for s in introduced_skills(before if kind != "new" else "", draft, grounding, jd_text)
             if _norm(s) != _norm(term)]
    if extra:
        problems.append("The draft claims a skill this role does not show: "
                        + ", ".join(extra) + ".")
    if kind != "new" and is_padded(before, draft):
        problems.append("The draft reaches its length with filler phrases.")
    return problems


# --------------------------------------------------------------------------- #
# Preview: ask the model, verify the evidence, validate the draft.
# --------------------------------------------------------------------------- #

@dataclass
class KeywordPreview:
    term: str
    supported: bool = False
    ok: bool = False            # supported AND a draft that passed every check
    reason: str = ""
    evidence: dict | None = None  # {"label", "quote", "section"}
    kind: str = ""              # "rewrite" | "new"
    target_bullet: str = ""     # LatexTemplate bullet id (the anchor bullet for a new one)
    ref: str = ""               # profile slot ref of the target / anchor
    label: str = ""             # role or project the change lands in
    before: str = ""            # current bullet prose ("" for a new bullet)
    after: str = ""             # the draft (plain prose)
    anchor: str = ""            # prose of the bullet a new one is added after
    budget: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["draft"] = self.after
        return d


def check_draft_for(pv: KeywordPreview, draft: str, profile: dict, jd_text: str,
                    evidence_profile: dict | None = None) -> list[str]:
    """Re-run every draft rule for a draft the person edited after the preview."""
    cands = [c for c in profile_slots(evidence_profile or profile) if _norm(c["label"]) == _norm(pv.label)]
    grounding = cands[0]["grounding"] if cands else ""
    return validate_draft(pv.term, undash(draft), pv.before, pv.kind, evidence_profile or profile,
                          grounding, jd_text)


def preview_keyword_action(term: str, tex_source: str, profile: dict, jd_text: str, llm,
                           *, fill_ratio: float | None = None,
                           evidence_profile: dict | None = None) -> KeywordPreview:
    """Ask the model whether ``profile`` supports ``term`` and, if so, for one draft.

    ``evidence_profile`` is the person's whole saved material when it is richer than the
    page profile (a role's bullets the one-page fit dropped still count as their material);
    it defaults to ``profile``. Nothing here changes the page: the result is a proposal."""
    term = _WS.sub(" ", str(term or "")).strip()
    pv = KeywordPreview(term=term)
    if not term:
        pv.reason = "No term given."
        return pv
    cands = candidates(tex_source, profile)
    if not cands:
        pv.reason = "This resume has no bullets the action could change."
        return pv
    room = has_room(fill_ratio)
    material = evidence_profile or profile
    try:
        resp = llm.keyword_action(term, jd_text, material, entries_for_model(cands), room) or {}
    except Exception as exc:  # noqa: BLE001 - surfaced as a plain reason, never a crash
        pv.reason = f"The model could not answer: {exc}"
        return pv
    if not isinstance(resp, dict) or not resp.get("supported"):
        pv.reason = str((resp or {}).get("reason") if isinstance(resp, dict) else "") \
            or f"Your profile does not show {term}."
        return pv

    ev = resp.get("evidence") if isinstance(resp.get("evidence"), dict) else {}
    quote = str(ev.get("quote") or "").strip()
    if not quote or not quote_in(quote, profile_text(material)):
        pv.reason = "The evidence the model cited is not in your profile, so nothing was added."
        return pv
    kind = "new" if str(resp.get("kind") or "rewrite").lower() == "new" else "rewrite"
    target = str(resp.get("target") or "").strip()
    cand = next((c for c in cands if c.id == target), None)
    if cand is None:
        pv.reason = "The model pointed at a bullet that is not on the page."
        return pv
    # The evidence must belong to the role or project that changes.
    full_grounding = cand.grounding
    if material is not profile:
        full_grounding = _grounding_from(material, cand) or cand.grounding
    if not (quote_in(quote, full_grounding) or _grounded(term, full_grounding)):
        pv.reason = (f"Your profile shows {term} under {_where(material, quote) or 'another entry'}, "
                     f"not under {cand.label}, so it cannot go on that bullet.")
        return pv
    pv.supported = True
    pv.evidence = {"label": _where(material, quote) or cand.label, "quote": quote,
                   "section": str(ev.get("section") or cand.section)}
    if kind == "new" and not room:
        pv.reason = "The page has no room for another line; only a rewrite would fit."
        return pv
    draft = undash(resp.get("draft") or "")
    before = "" if kind == "new" else cand.text
    problems = validate_draft(term, draft, before, kind, material, full_grounding, jd_text)
    pv.kind, pv.target_bullet, pv.ref, pv.label = kind, cand.id, cand.ref, cand.label
    pv.before, pv.after, pv.anchor = before, draft, cand.text
    pv.budget = NEW_BULLET_MAX if kind == "new" else rewrite_budget(before, term)[1]
    if problems:
        pv.reason = " ".join(problems)
        return pv
    pv.ok = True
    return pv


def _grounding_from(material: dict, cand: Candidate) -> str:
    """The text of the entry in ``material`` that matches the candidate's entry."""
    want = _norm(cand.label)
    for s in profile_slots(material):
        if _norm(s["label"]) == want:
            return s["grounding"]
    return ""


def _where(material: dict, quote: str) -> str:
    """The role/project (or 'skills', 'summary') the verbatim quote lives in."""
    for s in profile_slots(material):
        if quote_in(quote, s["grounding"]):
            return s["label"]
    for key in ("skills", "summary", "education", "certifications", "interests"):
        if key in material and quote_in(quote, profile_text(material[key])):
            return key
    return ""


# --------------------------------------------------------------------------- #
# Apply: surgical splice + self-healing compile, or roll back.
# --------------------------------------------------------------------------- #

@dataclass
class ApplyResult:
    ok: bool
    reason: str
    tex_source: str                      # the page after the action (the input on failure)
    compile: CompileResult | None
    edits: dict                          # {bullet id: new interior}, empty on failure
    diff: str = ""
    final_text: str = ""                 # the prose that ended up on the page
    fill_ratio: float | None = None
    attempts: int = 0
    steps: list = field(default_factory=list)

    def page_check(self) -> dict:
        c = self.compile
        return {"one_page": bool(c and c.ok and c.pages == 1), "pages": c.pages if c else None,
                "fill_ratio": round(self.fill_ratio, 3) if self.fill_ratio is not None else None,
                "attempts": self.attempts, "ok": self.ok}


def with_probe(tex_source: str) -> str:
    """Attach the fill probe right after ``\\begin{document}``; no layout effect."""
    if _FILL_PROBE in tex_source:
        return tex_source
    idx = tex_source.find(_DOC_START)
    if idx == -1:
        return tex_source
    end = idx + len(_DOC_START)
    return tex_source[:end] + "\n" + _FILL_PROBE + tex_source[end:]


def render_interior(text: str, bold_metrics: bool = False) -> str:
    """The exact ``\\item`` interior the assembler would render for ``text``."""
    from .assembler import _esc_bold
    return _esc_bold(str(text).strip(), bool(bold_metrics))


def apply_keyword_action(tex_source: str, kind: str, target_bullet: str, draft: str, term: str,
                         before: str, workdir: str | Path, llm, *, jobname: str = "cv",
                         bold_metrics: bool = False, max_retries: int = 2) -> ApplyResult:
    """Splice ``draft`` into the page surgically and prove it still fits; else roll back.

    ``kind`` "rewrite" replaces ``target_bullet``'s interior. ``kind`` "new" appends a new
    ``\\item`` after it (inside that bullet's interior, so the change is still confined to
    one addressable slot and the diff shows one bullet growing by one line). Every compile
    is checked for exit code, one page, no new overfull line and page fill; when the page
    overflows, the rewrite is shortened and tried again, and if the term or the budget is
    lost in the process, or the retries run out, the previous page is recompiled and kept."""
    workdir = Path(workdir)
    probed_src = with_probe(tex_source)
    probed = LatexTemplate(probed_src)
    plain = LatexTemplate(tex_source)
    if target_bullet not in {b.id for b in plain.bullets}:
        return ApplyResult(False, "That bullet is no longer on the page.", tex_source, None, {})
    anchor = probed.get(target_bullet)
    draft = undash(draft)
    lo, hi = (NEW_BULLET_MIN, NEW_BULLET_MAX) if kind == "new" else rewrite_budget(before, term)

    def interior(text: str) -> str:
        body = render_interior(text, bold_metrics)
        if kind == "new":
            return f"{anchor.text}\n\t\\item {body}"
        return body

    baseline = compile_tex(probed_src, workdir, jobname=f"{jobname}-kwbase")
    steps: list[dict] = []
    current = draft
    result: CompileResult | None = None
    for attempt in range(max_retries + 1):
        edits = {target_bullet: interior(current)}
        source = probed.render(edits)
        result = compile_tex(source, workdir, jobname=jobname)
        passed, reasons, _fresh, _extra = _checks(baseline, result)
        if result.pages == 1 and result.fill_ratio is not None and result.fill_ratio > MAX_FILL:
            passed = False
            reasons.append("the page is full to the bottom edge")
        steps.append({"attempt": attempt, "text": current, "pages": result.pages,
                      "fill": result.fill_ratio, "ok": passed, "detail": "; ".join(reasons)})
        if passed:
            final = plain.render(edits)
            confined = verify_confined(plain, final, edits)
            if not confined:
                break  # never ship an edit that leaked outside its bullet
            return ApplyResult(True, "", final, result, edits, unified_diff(tex_source, final),
                               current, result.fill_ratio, attempt + 1, steps)
        if attempt == max_retries or kind == "new":
            break
        # Shorten the rewrite and try again; the term and the budget must survive.
        target_len = max(lo, int(len(current) * 0.9))
        if target_len >= len(current):
            break
        try:
            shorter = undash(llm.shorten_bullet(current, target_len))
        except Exception:  # noqa: BLE001
            shorter = ""
        if not shorter or shorter == current or len(shorter) < lo \
                or not term_present(term, shorter):
            break
        current = shorter

    # Roll back: the served PDF must be the page the person already had.
    rollback = compile_tex(probed_src, workdir, jobname=jobname)
    why = (steps[-1]["detail"] if steps else "could not compile") or "could not fit"
    return ApplyResult(False, f"That change would not fit on one page ({why}), so it was not kept.",
                       tex_source, rollback, {}, "", "", rollback.fill_ratio, len(steps), steps)


def restore_bullet(tex_source: str, current_interior: str, previous_interior: str,
                   workdir: str | Path, *, jobname: str = "cv") -> ApplyResult:
    """Undo one rewrite: put ``previous_interior`` back where ``current_interior`` is."""
    plain = LatexTemplate(tex_source)
    hit = next((b for b in plain.bullets if b.text == current_interior.strip()), None)
    if hit is None:
        return ApplyResult(False, "That bullet is no longer on the page.", tex_source, None, {})
    edits = {hit.id: previous_interior}
    final = plain.render(edits)
    return _compile_restored(tex_source, final, edits, workdir, jobname)


def remove_added_bullet(tex_source: str, added_interior: str, workdir: str | Path, *,
                        jobname: str = "cv") -> ApplyResult:
    """Undo one added bullet: cut the exact ``\\item`` line the action inserted."""
    needle = f"\n\t\\item {added_interior.strip()}"
    idx = tex_source.find(needle)
    if idx == -1:
        return ApplyResult(False, "That bullet is no longer on the page.", tex_source, None, {})
    final = tex_source[:idx] + tex_source[idx + len(needle):]
    return _compile_restored(tex_source, final, {}, workdir, jobname)


def _compile_restored(tex_source: str, final: str, edits: dict, workdir, jobname) -> ApplyResult:
    probed = with_probe(final)
    result = compile_tex(probed, Path(workdir), jobname=jobname)
    over = result.fill_ratio is not None and result.fill_ratio > MAX_FILL
    if result.ok and result.pages == 1 and not over:
        return ApplyResult(True, "", final, result, edits, unified_diff(tex_source, final),
                           "", result.fill_ratio, 1)
    compile_tex(with_probe(tex_source), Path(workdir), jobname=jobname)   # keep the page as it was
    return ApplyResult(False, "Restoring that bullet would not fit on one page.", tex_source,
                       result, {}, "", "", result.fill_ratio, 1)
