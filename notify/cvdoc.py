"""A tailored CV as addressable spans, and the scoped edits a chat can make to it.

The Telegram review (``notify/cvreview.py``) lets the person change their CV by typing an
instruction ("rewrite E1.2 shorter", "change my title at Stanbic to Product Lead", "move
Projects above Experience"). Everything here is pure data work, no network, no model:

* ``span_map`` numbers every editable piece of the page with a STABLE id:
  ``S`` summary, ``E1.2`` employer 1 bullet 2, ``E1.title`` / ``E1.dates`` /
  ``E1.company`` / ``E1.location``, ``E1.R2.title`` for a second role at the same employer,
  ``P3.1`` project 3 bullet 1, ``P3.name``, ``ED1.school`` / ``ED1.degree`` / ``ED1.date``,
  ``K2`` skills line 2, ``X1.1`` extracurricular, ``I`` interests.
* ``apply_text_edits`` writes new text into exactly the targeted spans and then compares
  every span before and after: anything outside the targets that changed is an
  over-edit and the whole edit is rejected (scoped edits, InkSync UIST 2024; LLMs
  over-edit, FineEdit 2025).
* structural ops (move a section, drop a bullet or an entry, add a bullet) are
  deterministic code, so they need no guard.
* ``FACT_FIELDS`` are the spans that state a fact about the person (dates, titles,
  employer names, education). Those always go through an explicit confirm first.

The page itself is ``{"profile": <the rendered profile>, "sections": [ordered names]}``.
"""

from __future__ import annotations

import copy
import re

# Field spans that state a FACT (CLAUDE.md §8: the person owns their content, so a fact
# changes only when they confirm it, and only they decide whether the profile changes too).
FACT_FIELDS = {"company", "title", "dates", "location", "school", "degree", "date"}

FIELD_LABEL = {
    "company": "employer name", "title": "title", "dates": "dates", "location": "location",
    "school": "school", "degree": "degree", "date": "graduation date", "name": "name",
    "courses": "courses",
}

SECTION_LABEL = {
    "summary": "Summary", "education": "Education", "skills": "Skills",
    "projects": "Projects", "experience": "Experience",
    "extracurricular": "Extracurricular", "interests": "Interests",
}

SPAN_ID_RE = re.compile(r"\b(?:S|I|(?:E|P|ED|K|X)\d+(?:\.R\d+)?(?:\.(?:\d+|[a-z]+))?)\b")


def _roles_of(entry: dict) -> list:
    return entry["roles"] if isinstance(entry.get("roles"), list) else [entry]


def _org_key(entry: dict) -> str:
    for k in ("org", "company", "name"):
        if k in entry:
            return k
    return "org"


def _get(root, path):
    cur = root
    for p in path:
        cur = cur[p]
    return cur


def _set(root, path, value):
    cur = root
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = value


def span_map(doc: dict) -> dict:
    """Ordered ``{id: {"id", "kind", "text", "path", "section", "entry", "field"}}``.

    ``kind`` is bullet | summary | field | skill | interests. ``path`` points into
    ``doc["profile"]``. ``entry`` is the entry id (E1, P3, ...) a span belongs to."""
    prof = doc.get("profile") or {}
    out: dict = {}

    def add(sid, kind, path, section, entry="", field=""):
        try:
            text = _get(prof, path)
        except (KeyError, IndexError, TypeError):
            return
        out[sid] = {"id": sid, "kind": kind, "text": "" if text is None else str(text),
                    "path": tuple(path), "section": section, "entry": entry, "field": field}

    if "summary" in prof and str(prof.get("summary") or "").strip():
        add("S", "summary", ("summary",), "summary")

    for prefix, section in (("E", "experience"), ("P", "projects")):
        for i, entry in enumerate(prof.get(section) or []):
            eid = f"{prefix}{i + 1}"
            ok = _org_key(entry)
            add(f"{eid}.{'company' if section == 'experience' else 'name'}", "field",
                (section, i, ok), section, eid, "company" if section == "experience" else "name")
            if entry.get("location"):
                add(f"{eid}.location", "field", (section, i, "location"), section, eid, "location")
            nested = isinstance(entry.get("roles"), list)
            k = 0
            for j, role in enumerate(_roles_of(entry)):
                base = (section, i, "roles", j) if nested else (section, i)
                rid = eid if j == 0 else f"{eid}.R{j + 1}"
                for fld in ("title", "dates"):
                    if role.get(fld) or (section == "experience" and fld == "title"):
                        add(f"{rid}.{fld}", "field", base + (fld,), section, eid, fld)
                for b in range(len(role.get("bullets") or [])):
                    k += 1
                    add(f"{eid}.{k}", "bullet", base + ("bullets", b), section, eid)

    for i, ed in enumerate(prof.get("education") or []):
        eid = f"ED{i + 1}"
        for fld in ("school", "degree", "date", "location", "courses"):
            if ed.get(fld) or fld in ("school", "degree", "date"):
                if fld in ed:
                    add(f"{eid}.{fld}", "field", ("education", i, fld), "education", eid, fld)

    skills = prof.get("skills") or {}
    if isinstance(skills, dict):
        for i, label in enumerate(skills):
            add(f"K{i + 1}", "skill", ("skills", label), "skills", f"K{i + 1}")

    for i, it in enumerate(prof.get("extracurricular") or []):
        eid = f"X{i + 1}"
        if isinstance(it, dict):
            add(f"{eid}.title", "field", ("extracurricular", i, "title"), "extracurricular", eid, "title")
            if it.get("date"):
                add(f"{eid}.date", "field", ("extracurricular", i, "date"), "extracurricular", eid, "date")
            for b in range(len(it.get("bullets") or [])):
                add(f"{eid}.{b + 1}", "bullet", ("extracurricular", i, "bullets", b),
                    "extracurricular", eid)

    if str(prof.get("interests") or "").strip():
        add("I", "interests", ("interests",), "interests")
    return out


def span_texts(doc: dict) -> dict:
    return {sid: s["text"] for sid, s in span_map(doc).items()}


def is_fact(span: dict) -> bool:
    if span.get("kind") != "field":
        return False
    fld = span.get("field")
    if fld in ("title", "date") and span.get("section") == "extracurricular":
        return False                 # an activity name is not a career fact
    return fld in FACT_FIELDS


def entry_label(doc: dict, entry_id: str) -> str:
    """'Stanbic Bank' for E1, the project name for P2, the school for ED1."""
    spans = span_map(doc)
    for key in (f"{entry_id}.company", f"{entry_id}.name", f"{entry_id}.school",
                f"{entry_id}.title"):
        if key in spans and spans[key]["text"].strip():
            return spans[key]["text"].strip()
    return entry_id


def entry_ids(doc: dict) -> list:
    out = []
    for s in span_map(doc).values():
        if s["entry"] and s["entry"] not in out:
            out.append(s["entry"])
    return out


def grounding_text(doc: dict, entry_id: str, *more: dict) -> str:
    """Everything one entry says (its fields and bullets), across ``doc`` and any earlier
    versions in ``more``: the material a reworded bullet of that entry may draw skills from."""
    parts = []
    for d in (doc, *more):
        if not d:
            continue
        for s in span_map(d).values():
            if s["entry"] == entry_id:
                parts.append(s["text"])
    return " ".join(parts)


def outline(doc: dict, limit: int = 140) -> str:
    """One line per span (``E1.2: text``) plus the section order, for the edit planner."""
    lines = ["SECTIONS (in order): " + ", ".join(doc.get("sections") or [])]
    for sid, s in span_map(doc).items():
        t = s["text"].replace("\n", " ")
        lines.append(f"{sid}: {t[:limit]}{'…' if len(t) > limit else ''}")
    return "\n".join(lines)


def closest_span(doc: dict, wanted: str) -> str:
    """A best guess for an id that doesn't exist (E1.5 when E1 has 3 bullets -> E1.3)."""
    ids = list(span_map(doc))
    if not ids or not wanted:
        return ""
    m = re.match(r"^([A-Z]+)(\d+)(?:\.(.+))?$", wanted)
    if not m:
        return ""
    pre, num, rest = m.group(1), m.group(2), m.group(3) or ""
    same_entry = [i for i in ids if re.match(rf"^{pre}{num}\.", i)]
    if rest.isdigit() and same_entry:
        bullets = [i for i in same_entry if i.split(".")[-1].isdigit()]
        if bullets:
            return min(bullets, key=lambda i: abs(int(i.split(".")[-1]) - int(rest)))
    if rest and not rest.isdigit():
        for i in ids:
            if i.startswith(pre) and i.endswith("." + rest):
                return i
    same_kind = [i for i in ids if re.match(rf"^{pre}\d+", i)]
    return same_kind[0] if same_kind else ""


# --------------------------------------------------------------------------- edits
class EditRejected(Exception):
    """An edit that broke a rule. ``reason`` is the plain-language message for the person."""

    def __init__(self, reason: str, code: str = "rejected"):
        super().__init__(reason)
        self.reason = reason
        self.code = code


TEXT_OPS = ("rewrite", "set")
STRUCT_OPS = ("move_section", "drop", "add_bullet")


def normalize_plan(plan: dict, doc: dict) -> dict:
    """Clean a planner's output into ``{"edits": [...], "clarify": str, "suggest": [...]}``.

    Every edit is ``{"op", "target"?, "value"?, "instruction"?, "section"?, "before"?}``.
    Unknown targets become a clarifying question with the closest real id."""
    spans = span_map(doc)
    entries = set(entry_ids(doc))
    secs = [s for s in (doc.get("sections") or [])]
    edits, unknown, needs_value = [], [], []
    for e in (plan or {}).get("edits") or []:
        if not isinstance(e, dict):
            continue
        op = str(e.get("op") or "").strip().lower()
        tgt = str(e.get("target") or "").strip()
        if op in ("rewrite", "set"):
            if tgt not in spans:
                unknown.append(tgt)
                continue
            if op == "set" and not str(e.get("value") or "").strip():
                op = "rewrite"
            if op == "rewrite" and is_fact(spans[tgt]):
                needs_value.append(tgt)       # a fact is never reworded by the model
                continue
            edits.append({"op": op, "target": tgt, "value": str(e.get("value") or "").strip(),
                          "instruction": str(e.get("instruction") or "").strip()})
        elif op == "drop":
            if tgt not in spans and tgt not in entries:
                unknown.append(tgt)
                continue
            if tgt in spans and spans[tgt]["kind"] not in ("bullet", "skill"):
                unknown.append(tgt)
                continue
            edits.append({"op": "drop", "target": tgt})
        elif op == "add_bullet":
            base = tgt.split(".")[0] if tgt else ""
            role = tgt if re.match(r"^[EP]\d+\.R\d+$", tgt or "") else ""
            if base not in entries or not re.match(r"^(E|P|X)\d+$", base):
                unknown.append(tgt)
                continue
            val = str(e.get("value") or "").strip()
            if not val:
                continue
            edits.append({"op": "add_bullet", "target": role or base, "value": val})
        elif op == "move_section":
            sec = str(e.get("section") or "").strip().lower()
            before = str(e.get("before") or "").strip().lower()
            after = str(e.get("after") or "").strip().lower()
            if sec in secs and ((before in secs and before != sec) or (after in secs and after != sec)):
                edits.append({"op": "move_section", "section": sec,
                              "before": before if before in secs else "",
                              "after": after if after in secs and before not in secs else ""})
    out = {"edits": edits, "clarify": str((plan or {}).get("clarify") or "").strip(),
           "suggest": [], "unknown": [u for u in unknown if u]}
    if needs_value and not edits:
        sid = needs_value[0]
        label = FIELD_LABEL.get(spans[sid]["field"], spans[sid]["field"])
        out["clarify"] = (f"Tell me the exact new {label} for {sid}, for example: change "
                          f"{spans[sid]['entry']} {spans[sid]['field']} to ...")
    if unknown and not edits:
        guesses = [g for g in (closest_span(doc, u) for u in unknown) if g]
        out["suggest"] = guesses
        if guesses:
            out["clarify"] = f"Did you mean {guesses[0]}?"
        elif not out["clarify"]:
            out["clarify"] = ("I couldn't tell which part you mean. Tap Show changes to see the "
                              "ids, then say something like: rewrite E1.2 shorter.")
    return out


def retarget(plan: dict, old: str, new: str) -> dict:
    """The person said yes to 'Did you mean E1.2?': the same plan aimed at the real id."""
    p = copy.deepcopy(plan)
    for e in p.get("edits") or []:
        if e.get("target") == old:
            e["target"] = new
    return p


def fact_changes(plan: dict, doc: dict) -> list:
    """The ``set`` edits that change a fact (dates, titles, employer, education)."""
    spans = span_map(doc)
    out = []
    for e in plan.get("edits") or []:
        if e["op"] in TEXT_OPS and e.get("target") in spans and is_fact(spans[e["target"]]):
            s = spans[e["target"]]
            out.append({"target": e["target"], "field": s["field"], "section": s["section"],
                        "entry": s["entry"], "old": s["text"], "new": e.get("value") or "",
                        "label": entry_label(doc, s["entry"])})
    return out


def apply_text_edits(doc: dict, new_texts: dict, allowed: set) -> dict:
    """Write ``new_texts`` ({span id: text}) into a COPY of ``doc``.

    The over-editing guard: a key outside ``allowed`` rejects the edit, and after writing,
    every span outside ``allowed`` must read exactly as before."""
    stray = [k for k in new_texts if k not in allowed]
    if stray:
        raise EditRejected(
            f"I only change the part you point at, and that rewrite also touched "
            f"{', '.join(sorted(stray))}. I left your CV as it was. Try again, or name the "
            f"exact id.", code="over_edit")
    before = span_map(doc)
    out = copy.deepcopy(doc)
    for sid, text in new_texts.items():
        _set(out["profile"], before[sid]["path"], str(text).strip())
    after = span_texts(out)
    changed = [k for k in set(after) | set(before)
               if k not in allowed and after.get(k) != before.get(k, {}).get("text")]
    if changed:
        raise EditRejected(
            f"That edit would also have changed {', '.join(sorted(changed))}, so I left your "
            f"CV as it was.", code="over_edit")
    return out


def apply_struct_edit(doc: dict, edit: dict) -> dict:
    """One deterministic structural op on a COPY of ``doc``."""
    out = copy.deepcopy(doc)
    prof = out["profile"]
    op = edit["op"]
    if op == "move_section":
        secs = list(out.get("sections") or [])
        sec = edit["section"]
        secs.remove(sec)
        if edit.get("before"):
            secs.insert(secs.index(edit["before"]), sec)
        else:
            secs.insert(secs.index(edit["after"]) + 1, sec)
        out["sections"] = secs
        return out
    spans = span_map(out)
    tgt = edit["target"]
    if op == "drop":
        if tgt in spans:
            path = spans[tgt]["path"]
            container = _get(prof, path[:-1])
            if isinstance(container, list):
                container.pop(path[-1])
            else:
                container.pop(path[-1], None)
            return out
        m = re.match(r"^(E|P|X|ED)(\d+)$", tgt)
        section = {"E": "experience", "P": "projects", "X": "extracurricular",
                   "ED": "education"}[m.group(1)]
        prof[section].pop(int(m.group(2)) - 1)
        return out
    if op == "add_bullet":
        m = re.match(r"^(E|P|X)(\d+)(?:\.R(\d+))?$", tgt)
        section = {"E": "experience", "P": "projects", "X": "extracurricular"}[m.group(1)]
        entry = prof[section][int(m.group(2)) - 1]
        roles = _roles_of(entry)
        role = roles[int(m.group(3)) - 1] if m.group(3) else roles[0]
        role.setdefault("bullets", []).append(edit["value"])
        return out
    raise EditRejected("I don't know how to make that change yet.")


def describe_struct(edit: dict, doc: dict) -> str:
    op = edit["op"]
    if op == "move_section":
        sec = SECTION_LABEL.get(edit["section"], edit["section"].title())
        if edit.get("before"):
            return f"Moved {sec} above {SECTION_LABEL.get(edit['before'], edit['before'].title())}."
        return f"Moved {sec} below {SECTION_LABEL.get(edit['after'], edit['after'].title())}."
    if op == "drop":
        spans = span_map(doc)
        if edit["target"] in spans:
            return f"Removed {edit['target']}: {spans[edit['target']]['text']}"
        return f"Removed {edit['target']} ({entry_label(doc, edit['target'])})."
    if op == "add_bullet":
        return f"Added a bullet to {edit['target']}: {edit['value']}"
    return ""


def changed_spans(before: dict, after: dict) -> list:
    """Ids whose text differs between two docs (same numbering), in page order."""
    a, b = span_texts(before), span_texts(after)
    order = list(b) + [k for k in a if k not in b]
    return [k for k in order if a.get(k) != b.get(k)]


# --------------------------------------------------------------------------- profile facts
def apply_fact_to_profile(profile: dict, change: dict, doc: dict) -> bool:
    """Write one confirmed fact change into the person's SAVED profile ("Also update my
    profile"). Matches the entry by its current employer/school and title, never by
    position, since the saved profile holds more roles than one page shows."""
    spans = span_map(doc)
    entry = change["entry"]
    field = change["field"]
    old = change["old"]

    def norm(s):
        return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()

    if change["section"] == "education":
        school = spans.get(f"{entry}.school", {}).get("text", "")
        degree = spans.get(f"{entry}.degree", {}).get("text", "")
        for ed in profile.get("education") or []:
            if norm(ed.get("school")) == norm(school) and (
                    field == "school" or not degree or norm(ed.get("degree")) == norm(degree)
                    or norm(ed.get(field)) == norm(old)):
                ed[field] = change["new"]
                return True
        return False

    section = change["section"]
    org = spans.get(f"{entry}.company", spans.get(f"{entry}.name", {})).get("text", "")
    title = spans.get(f"{entry}.title", {}).get("text", "")
    target = change["target"]
    role_title = spans.get(target.rsplit(".", 1)[0] + ".title", {}).get("text", title)
    hit = False
    for e in profile.get(section) or []:
        if norm(e.get(_org_key(e))) != norm(org):
            continue
        if field in ("company", "name", "location"):
            e[_org_key(e) if field in ("company", "name") else "location"] = change["new"]
            hit = True
            continue
        for r in _roles_of(e):
            if norm(r.get("title")) == norm(role_title) and norm(r.get(field)) == norm(old):
                r[field] = change["new"]
                return True
        for r in _roles_of(e):
            if norm(r.get(field)) == norm(old):
                r[field] = change["new"]
                return True
    return hit


# --------------------------------------------------------------------------- messages
def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def change_summary(role: str, company: str, base: dict, page: dict, coverage: dict) -> str:
    """The preview caption: 'Tailored for {Role} at {Company}: {n} bullets reworded,
    summary updated. Added the job's terms: PnL, ETL. Missing: Kafka.'"""
    from tailoring.keywords import profile_text, term_present
    who = f"{role} at {company}" if company else (role or "this role")
    bits = []
    if base and page:
        b, p = span_map({"profile": base}), span_map({"profile": page})
        n = sum(1 for k, s in p.items() if s["kind"] == "bullet"
                and k in b and b[k]["text"].strip() != s["text"].strip())
        if n:
            bits.append(_plural(n, "bullet reworded", "bullets reworded"))
        if "S" in p and p["S"]["text"].strip() != b.get("S", {}).get("text", "").strip():
            bits.append("summary updated")
    present = list((coverage or {}).get("present") or [])
    base_text = profile_text(base or {})
    added = [t for t in present if not term_present(t, base_text)] if base else present
    missing = list((coverage or {}).get("missing") or [])
    text = f"Tailored for {who}" + (": " + ", ".join(bits) if bits else "") + "."
    if added:
        text += " Added the job's terms: " + ", ".join(added[:8]) + "."
    if missing:
        text += " Missing: " + ", ".join(missing[:8]) + "."
    return text[:1024]


def before_after(sid: str, before: str, after: str) -> str:
    if before == after:
        return f"{sid} (unchanged)\n{after}"
    if not before:
        return f"{sid} (new)\n{after}"
    if not after:
        return f"{sid} (removed)\n{before}"
    return f"{sid}\nBefore: {before}\nAfter: {after}"


def show_changes(role: str, company: str, base: dict, page: dict) -> list:
    """Per-span before/after for the whole page, grouped by entry, with every id shown so the
    person can name it in an edit. Returns the blocks (callers chunk them)."""
    who = f"{role} at {company}" if company else (role or "this role")
    b = span_map({"profile": base or {}})
    p = span_map(page)
    blocks = [f"Changes for {who}. Every part has an id you can use in an edit, "
              "for example: rewrite E1.2 shorter."]
    order = page.get("sections") or []
    if order:
        blocks.append("Section order: " + ", ".join(SECTION_LABEL.get(s, s) for s in order))
    groups: dict = {}
    for sid, s in p.items():
        groups.setdefault(s["entry"] or sid, []).append(sid)
    for key, sids in groups.items():
        lines = []
        fields = [sid for sid in sids if p[sid]["kind"] == "field"]
        if fields:
            lines.append(" | ".join(f"{sid}: {p[sid]['text']}" for sid in fields))
        for sid in sids:
            if p[sid]["kind"] == "field":
                continue
            old = b.get(sid, {}).get("text", "") if base else p[sid]["text"]
            lines.append(before_after(sid, old.strip(), p[sid]["text"].strip()))
        blocks.append("\n".join(lines))
    return blocks


def chunk(blocks: list, limit: int = 4000) -> list:
    """Pack blocks into messages under ``limit`` chars (Telegram's cap is 4096); a block that
    is itself too long is split on lines, then hard-split."""
    msgs, cur = [], ""
    pieces = []
    for blk in blocks:
        if len(blk) <= limit:
            pieces.append(blk)
            continue
        sub = ""
        for line in blk.split("\n"):
            while len(line) > limit:
                if sub:
                    pieces.append(sub)
                    sub = ""
                pieces.append(line[:limit])
                line = line[limit:]
            if sub and len(sub) + 1 + len(line) > limit:
                pieces.append(sub)
                sub = line
            else:
                sub = sub + "\n" + line if sub else line
        if sub:
            pieces.append(sub)
    for piece in pieces:
        add = piece if not cur else cur + "\n\n" + piece
        if len(add) <= limit:
            cur = add
        else:
            if cur:
                msgs.append(cur)
            cur = piece
    if cur:
        msgs.append(cur)
    return msgs
