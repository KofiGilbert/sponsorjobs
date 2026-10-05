"""Turn a form's fields into a fill plan, via our broker LLM, WITHOUT ever sending personal data.

The model is shown only the form's structure (labels, types, options) plus the list of placeholder
KEYS it may reference (APPLICANT_KEYS) and the role/company/JD context. It returns a plan mapping each
field to a key (for personal data) or a literal (a non-personal choice). No real name, email, phone,
or answer is in the prompt, so nothing personal reaches the model. ``parse_plan`` then hard-validates
the model's output against the ACTUAL fields and allowed keys, so a hallucinated field or key is
dropped rather than acted on.
"""
from __future__ import annotations

from autoapply.fields import APPLICANT_KEYS, FillAction, FormField

_VALID_OPS = {"fill", "select", "check", "upload", "skip"}


def parse_plan(raw: dict, fields: list[FormField]) -> list[FillAction]:
    """Validate the model's raw plan against the real fields. Keeps only actions that target a known
    field ref and, when they reference personal data, a real APPLICANT_KEY. Everything else is
    dropped, so a hallucinated field/key can never drive the browser."""
    by_ref = {f.ref: f for f in fields}
    keys = set(APPLICANT_KEYS)
    out: list[FillAction] = []
    seen: set[str] = set()
    for item in (raw or {}).get("actions") or []:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        op = str(item.get("op") or "fill").strip().lower()
        if ref not in by_ref or op not in _VALID_OPS or ref in seen:
            continue
        source = str(item.get("source") or "").strip()
        value = str(item.get("value") or "").strip()
        if source and source not in keys:        # a made-up personal key -> ignore the source
            source = ""
        if op in ("fill", "select", "check") and not source and not value:
            continue                             # nothing to put in the field -> skip it
        seen.add(ref)
        out.append(FillAction(ref=ref, op=op, source=source, value=value))
    return out


def build_fill_plan(fields: list[FormField], llm, *, role: str = "", company: str = "",
                    jd: str = "", available_keys: tuple[str, ...] = APPLICANT_KEYS) -> list[FillAction]:
    """Ask the broker LLM for a fill plan for these fields, then validate it. Returns [] on any model
    error (the caller then drops to assisted apply, never guesses). The prompt carries NO personal
    data -- only field structure + the placeholder keys the plan may reference."""
    if not fields:
        return []
    public = [f.public() for f in fields]
    try:
        raw = llm.plan_form_fill(public, list(available_keys), role=role, company=company, jd=jd)
    except Exception:                            # noqa: BLE001 - model/broker blip -> no plan
        return []
    return parse_plan(raw or {}, fields)
