"""The resume material, assembled from memory (docs/applicant-brain.md, sections 7.4 and 15,
Phase 6: "Tailoring reads the profile assembled from memory by default").

The August plan built this by replaying up to 80 chat turns through the model on every call. It
was never wired in, and it would have cost a model call per turn per resume. The brain now holds
the facts themselves, so the profile is assembled by code, with no model call:

* the saved resume material (roles with their bullets, schools, projects, skills, summary) is
  the base, because only it holds the bullets;
* the contact details come from the brain: a value the person gave or confirmed (confidence
  1.0, confirmed) is the one every resume, letter and form uses, so a phone number corrected
  anywhere is corrected everywhere; any other fact at 0.9 or more fills only what is empty;
* a role's dates or location, a school's date and a project's dates the brain holds fill what
  the material leaves empty (the details step, the extension, a resume import);
* what the person forgot on the memory page is gone from both, so nothing comes back.

Invents nothing: every value is one the person or their own resume gave.
"""
from __future__ import annotations

import copy

from applicant.store import ACTIVE, PERSON, Store

# identity field -> brain key
_CONTACT = (("name", "name"), ("email", "email"), ("phone", "phone"), ("address", "address"),
            ("linkedin", "@linkedin"), ("github", "github"), ("blog", "@portfolio"))


def _theirs(st: Store, key: str):
    """The person's own current contact value (said or confirmed by them), else None."""
    rows = st.q("SELECT value FROM fact WHERE person=? AND key=? AND subject='' AND store='' AND status=?"
                " AND confirmed=1 AND confidence>=1.0 ORDER BY updated_at DESC, id DESC LIMIT 1",
                (PERSON, key, ACTIVE))
    return str(rows[0]["value"]).strip() if rows and str(rows[0]["value"] or "").strip() else None


def assemble(profile: dict | None, st: Store | None) -> dict:
    """A copy of the saved resume material with the brain's facts laid over it (see above)."""
    p = copy.deepcopy(profile) if isinstance(profile, dict) else {}
    if st is None:
        return p
    has_material = bool(p.get("experience") or p.get("education") or (p.get("identity") or {}).get("name"))
    try:
        ident = p.get("identity") if isinstance(p.get("identity"), dict) else {}
        for fld, key in _CONTACT:
            mine = _theirs(st, key)
            if mine:
                ident[fld] = mine
        from ui.session import brain_backfill
        brain_backfill(ident, [], [], [], st)          # contact facts at 0.9 or more, empties only
        if ident:
            p["identity"] = ident
        if has_material:
            _fill_entries(p, st)
    except Exception:          # noqa: BLE001 - the saved material always stands on its own
        return copy.deepcopy(profile) if isinstance(profile, dict) else {}
    return p


def _fill_entries(p: dict, st: Store) -> None:
    """Fill a role's dates and location, a school's degree, date and location and a project's
    dates from the brain where the material leaves them empty (both profile shapes)."""
    from ui.session import _brain_items
    import re
    items = _brain_items(st)
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()   # noqa: E731
    roles = {(norm(x["org"]), norm(x["title"])): x for x in items["experience"]}
    for e in p.get("experience") or []:
        if not isinstance(e, dict):
            continue
        org = e.get("org") or e.get("company") or ""
        for r in (e.get("roles") if isinstance(e.get("roles"), list) else [e]):
            if not isinstance(r, dict):
                continue
            x = roles.get((norm(org), norm(r.get("title"))))
            if not x:
                continue
            if not str(r.get("dates") or e.get("dates") or "").strip() and x.get("dates"):
                r["dates"] = x["dates"]
            if not str(e.get("location") or r.get("location") or "").strip() and x.get("location"):
                e["location"] = x["location"]
    schools = {norm(x["school"]): x for x in items["education"]}
    for d in p.get("education") or []:
        x = schools.get(norm(d.get("school"))) if isinstance(d, dict) else None
        for f in ("degree", "date", "location"):
            if x and not str(d.get(f) or "").strip() and str(x.get(f) or "").strip():
                d[f] = x[f]
    projs = {norm(x["org"]): x for x in items["projects"]}
    for d in p.get("projects") or []:
        if not isinstance(d, dict):
            continue
        x = projs.get(norm(d.get("title") or d.get("name") or d.get("org")))
        if x and not str(d.get("dates") or "").strip() and str(x.get("dates") or "").strip():
            d["dates"] = x["dates"]


def essentials(profile: dict) -> dict:
    """The assembled profile in the essentials shape the resume assembler reads:
    {identity, education, experience} with flat experience rows."""
    p = profile if isinstance(profile, dict) else {}
    exp = []
    for e in p.get("experience") or []:
        if not isinstance(e, dict):
            continue
        org = e.get("org") or e.get("company") or ""
        for r in (e.get("roles") if isinstance(e.get("roles"), list) else [e]):
            if isinstance(r, dict):
                exp.append({"org": org, "title": r.get("title") or "", "dates": r.get("dates") or e.get("dates") or "",
                            "location": e.get("location") or r.get("location") or ""})
    return {"identity": {k: v for k, v in (p.get("identity") or {}).items() if str(v or "").strip()},
            "education": [dict(x) for x in (p.get("education") or []) if isinstance(x, dict)],
            "experience": exp}
