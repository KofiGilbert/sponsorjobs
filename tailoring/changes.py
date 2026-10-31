"""What changed: a tailored resume against the person's base resume, worked out in code.

Every place the app or Telegram tells the person what tailoring did (the builder's "What
changed" list, the Telegram preview caption, the record kept with an application) reads it
from here. No model writes it: a model asked to summarise its own edits can describe an edit
it did not make, and this summary is the person's check on the edits it did make. The
pieces are all facts the build already holds:

* bullets reworded: each bullet on the page paired with the base bullet it came from (same
  role, closest wording), so "4 bullets reworded" counts real differences, nothing else;
* roles and projects the person's own choices hid or showed (assembler ``choices_report``);
* skills reordered or added: the base skills lines against the page's;
* terms added: job terms the page shows that the base resume did not (the coverage report),
  plus the keywords the person added one by one;
* what the one-page fit cut (assembler ``trims``) and what the fact gate refused to publish
  (``reverted``).

Deterministic and offline: the same inputs always give the same words.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9+#]+")


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _key(entry: dict, role: dict) -> tuple:
    org = entry.get("org") or entry.get("name") or entry.get("company") or ""
    return (re.sub(r"[^a-z0-9]+", " ", str(org).lower()).strip(),
            re.sub(r"[^a-z0-9]+", " ", str(role.get("title") or "").lower()).strip())


def _label(entry: dict, role: dict) -> str:
    org = str(entry.get("org") or entry.get("name") or entry.get("company") or "").strip()
    title = str(role.get("title") or "").strip()
    if not org:
        org, title = title, ""
    if title == org:
        title = ""
    return ", ".join(x for x in (org, title) if x) or "Entry"


def _roles(profile: dict):
    """(key, label, bullets) for every role / project / activity with bullets, in page order."""
    for section in ("projects", "experience"):
        for e in (profile or {}).get(section) or []:
            if not isinstance(e, dict):
                continue
            for r in (e.get("roles") if isinstance(e.get("roles"), list) else [e]):
                if isinstance(r, dict):
                    yield (section,) + _key(e, r), _label(e, r), [str(b) for b in r.get("bullets") or []]
    for it in (profile or {}).get("extracurricular") or []:
        if isinstance(it, dict):
            yield ("extracurricular",) + _key(it, it), _label(it, it), [str(b) for b in it.get("bullets") or []]


def _overlap(a: str, b: str) -> float:
    wa, wb = set(_WORD.findall(_norm(a))), set(_WORD.findall(_norm(b)))
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def reworded_bullets(base: dict, page: dict) -> list[dict]:
    """[{label, before, after}] for every page bullet whose words differ from the base bullet
    it came from. Pairing is per role, exact matches first, then the closest unused base
    bullet; a page bullet with no plausible source (an added keyword line) is ``before: ""``."""
    base_roles = {k: list(bl) for k, _l, bl in _roles(base)}
    by_org: dict = {}
    for k, bl in base_roles.items():
        by_org.setdefault(k[:2], []).extend(bl)      # a retitled role still pairs by employer
    out: list[dict] = []
    for key, label, bullets in _roles(page):
        pool = list(base_roles.get(key) or by_org.get(key[:2]) or [])
        used = [False] * len(pool)
        for b in bullets:
            hit = next((i for i, x in enumerate(pool) if not used[i] and _norm(x) == _norm(b)), None)
            if hit is not None:
                used[hit] = True
                continue
            best, score = None, 0.0
            for i, x in enumerate(pool):
                if not used[i]:
                    sc = _overlap(x, b)
                    if sc > score:
                        best, score = i, sc
            if best is not None and score >= 0.2:
                used[best] = True
                out.append({"label": label, "before": pool[best], "after": b})
            elif base_roles:
                out.append({"label": label, "before": "", "after": b})
    return out


def _skill_items(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


def _skills_dict(profile: dict) -> dict:
    sk = (profile or {}).get("skills") or {}
    if isinstance(sk, dict):
        return {str(k): _skill_items(v) for k, v in sk.items()}
    if isinstance(sk, list):
        out: dict = {}
        for item in sk:
            if isinstance(item, dict):
                label = str(item.get("label") or item.get("category") or "Skills")
                out[label] = _skill_items(item.get("items") or item.get("value") or item.get("skills"))
            elif str(item).strip():
                out.setdefault("Skills", []).append(str(item).strip())
        return out
    return {"Skills": _skill_items(sk)} if str(sk).strip() else {}


def skills_change(base: dict, page: dict) -> dict:
    """{reordered: [line labels], added: [terms], lines_moved: bool} between two skills blocks."""
    b, p = _skills_dict(base), _skills_dict(page)
    if not b or not p:
        return {"reordered": [], "added": [], "lines_moved": False}
    every_base = {_norm(x) for items in b.values() for x in items}
    reordered, added = [], []
    for label, items in p.items():
        old = b.get(label)
        if old is None:
            continue
        common_new = [x for x in items if _norm(x) in {_norm(y) for y in old}]
        common_old = [y for y in old if _norm(y) in {_norm(x) for x in items}]
        if [_norm(x) for x in common_new] != [_norm(y) for y in common_old]:
            reordered.append(label)
    for items in p.values():
        for x in items:
            if _norm(x) not in every_base and x not in added:
                added.append(x)
    shared = [k for k in p if k in b]
    lines_moved = shared != [k for k in b if k in p]
    return {"reordered": reordered, "added": added, "lines_moved": lines_moved}


def terms_added(base: dict, coverage: dict | None, keyword_terms=()) -> list[str]:
    """Job terms the page shows that the base resume did not, then keywords added by hand."""
    from tailoring.keywords import profile_text, term_present
    present = list((coverage or {}).get("present") or [])
    base_text = profile_text(base or {})
    out = [t for t in present if not term_present(t, base_text)] if base else []
    for t in keyword_terms or ():
        if t and all(_norm(t) != _norm(x) for x in out):
            out.append(str(t))
    return out


def what_changed(base: dict | None, page: dict | None, *, coverage: dict | None = None,
                 hidden=(), shown=(), trims=(), reverted=(), keyword_terms=()) -> dict:
    """The structured diff. ``hidden`` / ``shown`` are the labels of the roles and projects the
    person's choices left off or put on this page; ``trims`` and ``reverted`` come straight
    from the build (assembler.AssembleResult)."""
    base = base or {}
    page = page or {}
    summary_changed = bool(base and page and _norm(page.get("summary")) != _norm(base.get("summary"))
                           and str(page.get("summary") or "").strip())
    rew = reworded_bullets(base, page) if base else []
    return {
        "reworded": [r for r in rew if r["before"]],
        "added_bullets": [r for r in rew if not r["before"]],
        "summary_updated": summary_changed,
        "skills": skills_change(base, page) if base else {"reordered": [], "added": [], "lines_moved": False},
        "hidden": [str(x) for x in hidden or () if str(x).strip()],
        "shown": [str(x) for x in shown or () if str(x).strip()],
        "terms_added": terms_added(base, coverage, keyword_terms),
        "missing": list((coverage or {}).get("missing") or []),
        "trimmed": [public_trim(t) for t in trims or ()],
        "reverted": [{"label": r.get("label", ""), "claims": list(r.get("claims") or [])}
                     for r in reverted or ()],
    }


def public_trim(t: dict) -> dict:
    """One cut as the builder and Telegram show it: what it was, and where."""
    kind = t.get("kind", "")
    shown = {"sentence": t.get("cut"), "courses": t.get("cut"), "skills": t.get("cut"),
             "skills_line": f"{t.get('label', '')}: {t.get('value', '')}"}.get(kind) or t.get("text", "")
    what = {"bullet": "Bullet", "sentence": "Sentence", "role": "Role", "project": "Project",
            "education": "School", "courses": "Courses", "skills": "Skills",
            "skills_line": "Skills line"}.get(kind, "Item")
    return {"id": t.get("id", ""), "kind": kind, "what": what, "label": t.get("label", ""),
            "text": str(shown or "")}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _names(items: list, n: int = 3) -> str:
    items = [str(x) for x in items if str(x).strip()]
    if len(items) <= n:
        return ", ".join(items)
    return ", ".join(items[:n]) + f" and {len(items) - n} more"


def points(ch: dict) -> list[str]:
    """The change list, one plain sentence per kind of change, for the builder."""
    out: list[str] = []
    if ch.get("reworded"):
        out.append(_plural(len(ch["reworded"]), "bullet reworded", "bullets reworded") + " toward the job.")
    if ch.get("summary_updated"):
        out.append("Summary updated.")
    sk = ch.get("skills") or {}
    if sk.get("reordered") or sk.get("lines_moved"):
        out.append("Skills reordered to lead with what the job asks for.")
    if sk.get("added"):
        out.append("Skills added from your material: " + _names(sk["added"], 5) + ".")
    if ch.get("shown"):
        out.append("Shown by your choice: " + _names(ch["shown"]) + ".")
    if ch.get("hidden"):
        out.append("Hidden by your choice: " + _names(ch["hidden"]) + ".")
    if ch.get("terms_added"):
        out.append("Job terms now on the page: " + _names(ch["terms_added"], 6) + ".")
    if ch.get("added_bullets"):
        out.append(_plural(len(ch["added_bullets"]), "new bullet", "new bullets") + " from your material.")
    if ch.get("trimmed"):
        n = len(ch["trimmed"])
        out.append(_plural(n, "item", "items") + " cut to fit one page. You can put "
                   + ("it" if n == 1 else "them") + " back.")
    if ch.get("reverted"):
        out.append(_plural(len(ch["reverted"]), "rewrite", "rewrites")
                   + " kept in your own words, because it named something your material doesn't show.")
    return out


def summary_line(ch: dict, role: str = "", company: str = "", limit: int = 1024) -> str:
    """The one-paragraph caption: 'Tailored for {Role} at {Company}: 2 bullets reworded,
    summary updated. Added the job's terms: ETL. Missing: Kafka.'"""
    who = f"{role} at {company}" if company else (role or "this role")
    bits = []
    if ch.get("reworded"):
        bits.append(_plural(len(ch["reworded"]), "bullet reworded", "bullets reworded"))
    if ch.get("summary_updated"):
        bits.append("summary updated")
    sk = ch.get("skills") or {}
    if sk.get("reordered") or sk.get("lines_moved"):
        bits.append("skills reordered")
    if ch.get("hidden"):
        bits.append(_names(ch["hidden"], 2) + " hidden by your choice")
    if ch.get("trimmed"):
        bits.append(_plural(len(ch["trimmed"]), "item", "items") + " cut to fit")
    text = f"Tailored for {who}" + (": " + ", ".join(bits) if bits else "") + "."
    if ch.get("terms_added"):
        text += " Added the job's terms: " + ", ".join(ch["terms_added"][:8]) + "."
    if ch.get("missing"):
        text += " Missing: " + ", ".join(ch["missing"][:8]) + "."
    return text[:limit]
