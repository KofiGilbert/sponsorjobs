"""Choices: how the person wants to come across for kinds of jobs (docs/applicant-brain.md,
sections 5.3, 6 and 8). "For banks and finance, show the Stanbic role, not Viva." "For startups,
lead with BlackOrigin." "Never show both 2020 day jobs together."

A choice is a ``rule`` row: an action, the items it names, and a scope (applicant/scope.py).

* ``hide``: never on the resume; ``show``: always on it; ``lead``: always on it, with the full
  bullet budget, and the summary names it first;
* ``prefer`` (two items): when both are the person's, show the first, not the second;
* ``apart`` (two items): never both on one resume (the better match for the job stays).

Choices are learned only from a tap: the builder's one-line ask, the overlapping-roles ask, a
chat or Telegram proposal the person saved, or the list on Your answers. Nothing here guesses a
choice. A choice only ever picks among the person's own roles and projects; it never adds words.

Item ids: ``role:<org>|<title>`` (the title the person gave, not a title aligned to a job) and
``project:<name>``, normalized like the brain's open facts. A renamed role keeps its choices
(``rename_item``); a role the person removed pauses them (``sync_items``) until it is back.

Personal data: rules hold item ids and the person's own words. Nothing here leaves the machine.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import secrets
import time

from applicant import scope as S
from applicant.store import PERSON, Store

ACTIONS = ("show", "hide", "lead", "prefer", "apart")
PAIR = ("prefer", "apart")
ACTIVE, PAUSED, REPLACED = "active", "paused", "replaced"
SOURCES = ("builder", "chat", "telegram", "memory_page", "migration")

ONCE_DAYS = 30                      # "Just this time" keeps the line away this long
OVERLAP_MONTHS = 3                  # two roles share at least this many months to count as overlapping
PROPOSAL_DAYS = 7


# ---------------------------------------------------------------------------- items
def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def role_title(role: dict) -> str:
    """The title the person gave (a title aligned to a job keeps the original beside it)."""
    return str(role.get("title_original") or role.get("title") or "")


def role_id(org, title) -> str:
    return f"role:{_norm(org)}|{_norm(title)}"


def project_name(entry: dict) -> str:
    return str(entry.get("org") or entry.get("name") or entry.get("title") or "")


def project_id(entry: dict) -> str:
    return f"project:{_norm(project_name(entry))}"


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                        "nov", "dec"), start=1)}


def _month(tok: str, end: bool) -> int | None:
    tok = str(tok or "").strip().lower()
    if not tok:
        return None
    if re.search(r"present|current|now|ongoing|today", tok):
        d = _dt.date.today()
        return d.year * 12 + d.month
    y = re.search(r"(19|20)\d\d", tok)
    if not y:
        return None
    m = re.search(r"[a-z]{3,}", tok)
    mon = _MONTHS.get(m.group(0)[:3]) if m else None
    return int(y.group(0)) * 12 + (mon or (12 if end else 1))


def span(dates) -> tuple[int, int] | None:
    """(first month, last month) of a date range ("Jan 2020 - Present", "2019 - 2021"), or None."""
    text = str(dates or "").replace(chr(0x2013), "-").replace(chr(0x2014), "-")     # en and em dashes
    parts = [p for p in re.split(r"\s*(?:-|\bto\b|\buntil\b|\bthrough\b)\s*", text) if p.strip()]
    if not parts:
        return None
    a = _month(parts[0], end=False)
    b = _month(parts[1], end=True) if len(parts) > 1 else _month(parts[0], end=True)
    if a is None or b is None:
        return None
    return (a, b) if a <= b else (b, a)


def overlap(a: dict, b: dict) -> int:
    """How many months two items share (0 when their dates are unknown or apart)."""
    x, y = a.get("span"), b.get("span")
    if not x or not y:
        return 0
    return max(0, min(x[1], y[1]) - max(x[0], y[0]) + 1)


def items(profile: dict | None) -> list[dict]:
    """The person's roles and projects, in page order: {id, kind, org, title, label, dates, span,
    pos}. ``pos`` names the item in the builder (``exp-<entry>-<role>``, ``proj-<n>``)."""
    p = profile if isinstance(profile, dict) else {}
    out: list[dict] = []
    for ei, entry in enumerate(p.get("experience") or []):
        if not isinstance(entry, dict):
            continue
        org = str(entry.get("org") or entry.get("company") or "")
        roles = entry["roles"] if isinstance(entry.get("roles"), list) else [entry]
        for ri, role in enumerate(roles):
            if not isinstance(role, dict):
                continue
            title = role_title(role)
            if not (_norm(org) or _norm(title)):
                continue
            dates = str(role.get("dates") or entry.get("dates") or "")
            out.append({"id": role_id(org, title), "kind": "role", "org": org, "title": title,
                        "dates": dates, "span": span(dates), "pos": f"exp-{ei}-{ri}"})
    for gi, entry in enumerate(p.get("projects") or []):
        if not isinstance(entry, dict) or not _norm(project_name(entry)):
            continue
        dates = str(entry.get("dates") or "")
        out.append({"id": project_id(entry), "kind": "project", "org": project_name(entry),
                    "title": str(entry.get("title") or ""), "dates": dates, "span": span(dates),
                    "pos": f"proj-{gi}"})
    per_org: dict = {}
    for it in out:
        if it["kind"] == "role":
            per_org.setdefault(_norm(it["org"]), []).append(it)
    for it in out:
        many = it["kind"] == "role" and len(per_org.get(_norm(it["org"]), [])) > 1
        it["label"] = f"{it['org']} ({it['title']})" if many and it["title"] else (it["org"] or it["title"])
    return out


def resolve(item: str, its: list[dict]) -> dict | None:
    """The person's item a choice names: the exact id, else (a title edited or aligned since) the
    one role at that employer when there is exactly one."""
    for it in its:
        if it["id"] == item:
            return it
    m = re.match(r"role:([^|]*)\|", str(item or ""))
    if m:
        same = [it for it in its if it["kind"] == "role" and _norm(it["org"]) == m.group(1)]
        if len(same) == 1:
            return same[0]
    return None


def label_of(item: str, its: list[dict]) -> str:
    it = resolve(item, its)
    if it:
        return it["label"]
    m = re.match(r"(?:role|project):([^|]*)", str(item or ""))
    return (m.group(1) if m else str(item)).title()


def overlapping_pairs(its: list[dict], months: int = OVERLAP_MONTHS) -> list[tuple[dict, dict, int]]:
    """Roles at different employers whose dates overlap by ``months`` or more (two day jobs at
    once, not a job change)."""
    roles = [it for it in its if it["kind"] == "role"]
    out = []
    for i, a in enumerate(roles):
        for b in roles[i + 1:]:
            if _norm(a["org"]) == _norm(b["org"]):
                continue
            n = overlap(a, b)
            if n >= months:
                out.append((a, b, n))
    return out


def pair_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


# ---------------------------------------------------------------------------- storage
def _row(r) -> dict:
    d = dict(r)
    try:
        d["items"] = json.loads(d.get("items") or "[]")
    except ValueError:
        d["items"] = []
    return d


def list_rules(st: Store, status=None) -> list[dict]:
    sql, args = "SELECT * FROM rule WHERE person=?", [PERSON]
    if status is not None:
        sts = (status,) if isinstance(status, str) else tuple(status)
        sql += f" AND status IN ({','.join('?' * len(sts))})"
        args.extend(sts)
    return [_row(r) for r in st.q(sql + " ORDER BY id", args)]


def get_rule(st: Store, rid) -> dict | None:
    rows = st.q("SELECT * FROM rule WHERE person=? AND id=?", (PERSON, int(rid)))
    return _row(rows[0]) if rows else None


def _same_scope(r: dict, kind: str, value: str) -> bool:
    return r["scope_kind"] == kind and str(r["scope_value"] or "") == str(value or "")


def add_rule(st: Store, action: str, its, scope_kind: str = S.ALL, scope_value: str = "", *,
             words: str = "", source: str = "builder", source_ref: str = "") -> int:
    """Save one choice and return its id. The same choice twice is one row (a paused one comes
    back); a choice that contradicts an older one for the same item and scope replaces it (kept
    as history)."""
    action = str(action or "")
    its = [str(i) for i in (its or []) if str(i or "").strip()]
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if (action in PAIR) != (len(its) == 2) or not its or len(its) > 2 or len(set(its)) != len(its):
        raise ValueError("show, hide and lead name one item; prefer and apart name two")
    if scope_kind not in S.SCOPE_KINDS:
        raise ValueError(f"unknown scope {scope_kind!r}")
    scope_value = "" if scope_kind == S.ALL else str(scope_value or "")
    if source not in SOURCES:
        source = "builder"
    now = time.time()
    with st.tx() as c:
        for r in list_rules(st, status=(ACTIVE, PAUSED)):
            if not _same_scope(r, scope_kind, scope_value):
                continue
            same_items = (r["items"] == its) if action == "prefer" else (sorted(r["items"]) == sorted(its))
            if r["action"] == action and same_items:
                c.execute("UPDATE rule SET status=?, updated_at=? WHERE id=?", (ACTIVE, now, r["id"]))
                st.log("rule_added", ref=f"rule:{r['id']}", outcome=action, source=source, n=0)
                return int(r["id"])
            clash = False
            if action in ("show", "hide", "lead") and r["action"] in ("show", "hide", "lead"):
                clash = r["items"] == its
            elif action == "prefer" and r["action"] == "prefer":
                clash = r["items"] == list(reversed(its))
            if clash:
                c.execute("UPDATE rule SET status=?, updated_at=? WHERE id=?", (REPLACED, now, r["id"]))
        cur = c.execute("INSERT INTO rule (person, action, items, scope_kind, scope_value, words, source, source_ref,"
                        " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (PERSON, action, json.dumps(its), scope_kind, scope_value, str(words or "")[:300], source,
                         str(source_ref or "")[:200], ACTIVE, now, now))
        rid = int(cur.lastrowid)
    st.log("rule_added", ref=f"rule:{rid}", outcome=action, source=source, n=1)
    return rid


def update_rule(st: Store, rid, *, action: str | None = None, scope_kind: str | None = None,
                scope_value: str | None = None, status: str | None = None) -> dict | None:
    """Change one choice in place (Edit and Pause on the list). Returns it, or None."""
    r = get_rule(st, rid)
    if r is None:
        return None
    sets: dict = {}
    if action is not None:
        if action not in ACTIONS or (action in PAIR) != (len(r["items"]) == 2):
            raise ValueError("that action does not fit this choice")
        sets["action"] = action
    if scope_kind is not None:
        if scope_kind not in S.SCOPE_KINDS:
            raise ValueError(f"unknown scope {scope_kind!r}")
        sets["scope_kind"] = scope_kind
        sets["scope_value"] = "" if scope_kind == S.ALL else str(scope_value or "")
    if status is not None:
        if status not in (ACTIVE, PAUSED):
            raise ValueError("a choice is active or paused")
        sets["status"] = status
        _set_auto_paused(st, [i for i in _auto_paused(st) if i != int(rid)])
    if not sets:
        return r
    sets["updated_at"] = time.time()
    with st.tx() as c:
        c.execute(f"UPDATE rule SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), int(rid)))
    st.log("rule_changed", ref=f"rule:{int(rid)}", n=len(sets) - 1)
    return get_rule(st, rid)


def delete_rule(st: Store, rid) -> bool:
    """The person deleted this choice: it is gone (the event log keeps only its id)."""
    with st.tx() as c:
        n = c.execute("DELETE FROM rule WHERE person=? AND id=?", (PERSON, int(rid))).rowcount
    if n:
        _set_auto_paused(st, [i for i in _auto_paused(st) if i != int(rid)])
        st.log("rule_deleted", ref=f"rule:{int(rid)}", n=int(n))
    return bool(n)


def rename_item(st: Store, old: str, new: str) -> int:
    """A role or project was renamed: every choice that names it, and the fact about it, follow.
    Returns how many choices changed."""
    if not old or not new or old == new:
        return 0
    n = 0
    with st.tx() as c:
        for r in list_rules(st):
            if old in r["items"]:
                its = [new if i == old else i for i in r["items"]]
                c.execute("UPDATE rule SET items=?, updated_at=? WHERE id=?", (json.dumps(its), time.time(), r["id"]))
                n += 1
        c.execute("UPDATE fact SET subject=? WHERE person=? AND subject=? AND store=''", (new, PERSON, old))
    if n:
        st.log("rule_renamed", n=n)
    return n


def _meta_json(st: Store, k: str, default):
    try:
        v = json.loads(st.meta(k) or "null")
    except ValueError:
        v = None
    return default if v is None else v


def _auto_paused(st: Store) -> list[int]:
    return [int(x) for x in _meta_json(st, "rules_auto_paused", []) if str(x).isdigit()]


def _set_auto_paused(st: Store, ids) -> None:
    st.set_meta("rules_auto_paused", json.dumps(sorted(set(int(i) for i in ids))))


def sync_items(st: Store, its: list[dict]) -> dict:
    """A choice that names an item the person no longer has is paused; it comes back when the
    item does. A choice the person paused stays paused. Returns {paused, resumed}."""
    auto = set(_auto_paused(st))
    before = set(auto)
    out = {"paused": 0, "resumed": 0}
    if not its:
        return out                          # no profile to judge against: change nothing
    for r in list_rules(st, status=(ACTIVE, PAUSED)):
        whole = all(resolve(i, its) for i in r["items"])
        if r["status"] == ACTIVE and not whole:
            update_rule(st, r["id"], status=PAUSED)
            auto.add(int(r["id"]))
            out["paused"] += 1
        elif r["status"] == PAUSED and whole and int(r["id"]) in auto:
            update_rule(st, r["id"], status=ACTIVE)
            auto.discard(int(r["id"]))
            out["resumed"] += 1
    if auto != before or out["paused"] or out["resumed"]:
        _set_auto_paused(st, auto)
    return out


def mark_applied(st: Store, rule_ids) -> None:
    ids = sorted({int(i) for i in rule_ids or [] if str(i).isdigit() and int(i) > 0})
    if not ids:
        return
    now = time.time()
    with st.tx() as c:
        c.execute(f"UPDATE rule SET times_applied=times_applied+1, last_applied_at=? WHERE id IN"
                  f" ({','.join('?' * len(ids))})", (now, *ids))


# ---------------------------------------------------------------------------- the plan for one job
def rank(rule: dict | None) -> list:
    """How strong a choice is when two disagree: a choice made for this one resume beats every
    saved one; then the scope's specificity; then the newer."""
    if rule is None:
        return [99, 0, time.time(), 0]
    lvl, depth, created, rid = S.specificity(rule)
    return [lvl, depth, created, rid]


def plan(rules: list[dict], lab: dict, its: list[dict], session: dict | None = None) -> dict:
    """What the job's choices say about this person's items, for ``select_roles_for_jd``:
    {hide, show, lead: {item id: {rule, rank}}, prefer, apart: [[a, b, {rule, rank}]], why}.
    ``session`` holds this resume's own switches ({item id: "show" | "hide"}): they beat any
    saved choice. Items are the person's actual ids (a choice naming an edited title still finds
    its role)."""
    out: dict = {"hide": {}, "show": {}, "lead": {}, "prefer": [], "apart": [], "beat": {}}
    vis: dict = {}                      # item id -> (action, src, rule)
    for r in S.applicable(rules, lab):
        ids = [resolve(i, its) for i in r["items"]]
        if any(x is None for x in ids):
            continue
        ids = [x["id"] for x in ids]
        src = {"rule": int(r["id"]), "rank": rank(r)}
        if r["action"] in PAIR:
            out[r["action"]].append([ids[0], ids[1], src])
            continue
        iid = ids[0]
        if iid in vis:
            won = vis[iid]
            if won[0] != r["action"] and iid not in out["beat"]:
                out["beat"][iid] = int(r["id"])        # the winner overruled this one
            continue
        vis[iid] = (r["action"], src, r)
    for iid, want in (session or {}).items():
        if want in ("show", "hide") and any(it["id"] == iid for it in its):
            if iid in vis and vis[iid][0] != want and vis[iid][2] is not None:
                out["beat"][iid] = int(vis[iid][2]["id"])
            vis[iid] = (want, {"rule": 0, "rank": rank(None)}, None)
    for iid, (action, src, _r) in vis.items():
        out[action][iid] = src
    return out


def empty(p: dict | None) -> bool:
    return not p or not any(p.get(k) for k in ("hide", "show", "lead", "prefer", "apart"))


# ---------------------------------------------------------------------------- words
def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def what(rule: dict, its: list[dict]) -> str:
    """The action of a choice in words: "show Stanbic, not Viva"."""
    names = [label_of(i, its) for i in rule.get("items") or []]
    a = rule.get("action")
    if a == "hide" and names:
        return f"hide {names[0]}"
    if a == "show" and names:
        return f"always show {names[0]}"
    if a == "lead" and names:
        return f"lead with {names[0]}"
    if a == "prefer" and len(names) == 2:
        return f"show {names[0]}, not {names[1]}"
    if a == "apart" and len(names) == 2:
        return f"never show {names[0]} and {names[1]} together"
    return "a choice"


def sentence(rule: dict, its: list[dict]) -> str:
    """A choice in plain words: "Bank and finance jobs: show Stanbic, not Viva.\""""
    where = _cap(S.words(rule.get("scope_kind") or S.ALL, rule.get("scope_value") or ""))
    return f"{where}: {what(rule, its)}."


def used_line(rule: dict) -> str:
    n = int(rule.get("times_applied") or 0)
    if not n:
        return "Not used yet"
    return "Used on 1 resume" if n == 1 else f"Used on {n} resumes"


# ---------------------------------------------------------------------------- "just this time" and asked-once
def once_sig(action: str, its, kind: str, value: str) -> str:
    return f"{action}|{','.join(its)}|{kind}:{value}"


def remember_once(st: Store, sig: str) -> None:
    d = {k: v for k, v in _meta_json(st, "choice_once", {}).items()
         if time.time() - float(v or 0) < ONCE_DAYS * 86400}
    d[sig] = time.time()
    st.set_meta("choice_once", json.dumps(d))


def said_once(st: Store, sig: str) -> bool:
    t = _meta_json(st, "choice_once", {}).get(sig)
    return bool(t) and time.time() - float(t) < ONCE_DAYS * 86400


def overlap_answer(st: Store, a: str, b: str) -> str:
    return str((_meta_json(st, "overlap_asked", {}).get(pair_key(a, b)) or {}).get("answer") or "")


def save_overlap_answer(st: Store, a: str, b: str, answer: str) -> None:
    d = _meta_json(st, "overlap_asked", {})
    d[pair_key(a, b)] = {"answer": answer, "at": time.time()}
    st.set_meta("overlap_asked", json.dumps(d))


def years(a: dict, b: dict) -> str:
    """The shared years of two overlapping roles ("2020", "2019 to 2021")."""
    x, y = a.get("span"), b.get("span")
    if not x or not y:
        return ""
    lo, hi = max(x[0], y[0]), min(x[1], y[1])
    y0, y1 = (lo - 1) // 12, (hi - 1) // 12
    return str(y0) if y0 == y1 else f"{y0} to {y1}"


# ---------------------------------------------------------------------------- chat and Telegram proposals
_CHOICE_WORDS = re.compile(r"\b(show|hide|lead with|instead of|rather than|not both|together|leave (?:off|out)|"
                           r"don'?t (?:show|use|include)|do not (?:show|use|include)|never (?:show|use|include)|"
                           r"use|keep|put|drop)\b", re.I)


def looks_like_choice(text: str, its: list[dict]) -> bool:
    """Cheap code check before any model call: the words of a choice AND one of the person's own
    roles or projects named."""
    t = str(text or "")
    if not _CHOICE_WORDS.search(t):
        return False
    low = _norm(t)
    return any(_norm(it["org"]) and re.search(r"\b" + re.escape(_norm(it["org"]).split()[0]) + r"\b", low)
               for it in its)


def _find(name: str, its: list[dict]) -> list[dict]:
    """The person's items a name in chat means: exact employer or project name, else the first
    words of one ("Stanbic" for "Stanbic Bank"). Several roles at one employer all fit."""
    n = _norm(name)
    if not n:
        return []
    exact = [it for it in its if _norm(it["org"]) == n or _norm(it["label"]) == n]
    if exact:
        return exact
    return [it for it in its if _norm(it["org"]).startswith(n + " ") or n.startswith(_norm(it["org"]) + " ")]


def resolve_proposal(raw: dict, its: list[dict], companies=()) -> dict | None:
    """A rule the model read in chat, made concrete by code: {action, scope_kind, scope_value,
    options: [[item id, ...], ...], words}. ``options`` has one entry when the names are clear and
    several when one name fits two roles (the person picks). None when it names nothing of theirs
    or a scope we do not know."""
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip().lower()
    names = [str(x) for x in (raw.get("items") or []) if str(x or "").strip()]
    if action not in ACTIONS or not names:
        return None
    need = 2 if action in PAIR else 1
    names = names[:need]
    if len(names) != need:
        return None
    found = [_find(n, its) for n in names]
    if any(not f for f in found):
        return None
    sc = S.from_words(str(raw.get("scope") or ""), companies)
    if sc is None:
        return None
    options: list[list[str]] = [[]]
    for f in found:
        options = [o + [it["id"]] for o in options for it in f if it["id"] not in o]
    options = [o for o in options if len(o) == need][:4]
    if not options:
        return None
    return {"action": action, "scope_kind": sc[0], "scope_value": sc[1], "options": options,
            "words": str(raw.get("words") or raw.get("text") or "")[:300]}


def proposal_text(prop: dict, its: list[dict]) -> str:
    """ "For bank and finance jobs: show Stanbic, not Viva." ("On every resume: ..." for all jobs)."""
    rule = {"action": prop["action"], "items": prop["options"][0], "scope_kind": prop["scope_kind"],
            "scope_value": prop["scope_value"]}
    where = ("On every resume" if prop["scope_kind"] == S.ALL
             else "For " + S.words(prop["scope_kind"], prop["scope_value"]))
    return f"{where}: {what(rule, its)}."


def option_label(prop: dict, i: int, its: list[dict]) -> str:
    opt = prop["options"][i]
    return ", ".join(label_of(x, its) for x in opt)


def _proposals(st: Store) -> dict:
    d = _meta_json(st, "rule_proposals", {})
    return {k: v for k, v in d.items() if time.time() - float((v or {}).get("at") or 0) < PROPOSAL_DAYS * 86400}


def proposal_sig(prop: dict) -> str:
    return json.dumps([prop["action"], prop["scope_kind"], prop["scope_value"], prop["options"]], sort_keys=True)


def already_known(st: Store, prop: dict) -> bool:
    """The same proposal was saved, declined or is waiting already; or a saved choice says it."""
    sig = proposal_sig(prop)
    if sig in _meta_json(st, "rule_proposals_seen", []):
        return True
    for r in list_rules(st, status=ACTIVE):
        if (r["action"] == prop["action"] and _same_scope(r, prop["scope_kind"], prop["scope_value"])
                and r["items"] in prop["options"]):
            return True
    return False


def stash_proposal(st: Store, prop: dict, channel: str) -> str:
    """Keep a proposal until the person taps Save or Not quite; returns its token."""
    d = _proposals(st)
    token = secrets.token_hex(4)
    d[token] = dict(prop, at=time.time(), channel=channel)
    if len(d) > 20:
        for k in sorted(d, key=lambda k: d[k].get("at") or 0)[:-20]:
            d.pop(k, None)
    st.set_meta("rule_proposals", json.dumps(d))
    seen = _meta_json(st, "rule_proposals_seen", [])
    seen = (seen + [proposal_sig(prop)])[-60:]
    st.set_meta("rule_proposals_seen", json.dumps(seen))
    return token


def pending_proposal(st: Store, token: str) -> dict | None:
    return _proposals(st).get(str(token or ""))


def answer_proposal(st: Store, token: str, save: bool, option: int = 0) -> dict:
    """The person tapped Save (with the option they picked) or Not quite. Returns
    {ok, rule_id?, proposal?}. A proposal is answered once."""
    d = _proposals(st)
    prop = d.pop(str(token or ""), None)
    st.set_meta("rule_proposals", json.dumps(d))
    if prop is None:
        return {"ok": False, "gone": True}
    if not save:
        return {"ok": True, "saved": False, "proposal": prop}
    opts = prop.get("options") or []
    i = option if 0 <= int(option or 0) < len(opts) else 0
    src = "telegram" if prop.get("channel") == "telegram" else "chat"
    rid = add_rule(st, prop["action"], opts[i], prop["scope_kind"], prop["scope_value"],
                   words=prop.get("words") or "", source=src, source_ref=prop.get("ref") or "")
    return {"ok": True, "saved": True, "rule_id": rid, "proposal": prop}


def propose_from_chat(st: Store, rows: list[dict], its: list[dict], channel: str, companies=(), ref: str = "") -> list[dict]:
    """The rules ``distill_memory`` read in chat, resolved and kept as proposals (never saved):
    [{token, text, options: [label, ...]}]. A rule already saved or already offered is skipped."""
    out = []
    for raw in rows or []:
        prop = resolve_proposal(raw, its, companies)
        if prop is None or already_known(st, prop):
            continue
        prop["ref"] = ref
        token = stash_proposal(st, prop, channel)
        out.append({"token": token, "text": proposal_text(prop, its),
                    "options": [option_label(prop, i, its) for i in range(len(prop["options"]))]})
    return out


# ---------------------------------------------------------------------------- chat constraints as hide choices
def constraint_offers(st: Store, notes: list[dict], its: list[dict]) -> list[dict]:
    """A chat constraint "do not mention Viva" that names exactly one of the person's items is
    offered as a hide choice for every job, for the person to confirm: [{fact, item, text}]."""
    dismissed = set(str(x) for x in _meta_json(st, "constraint_offers_done", []))
    have = {(r["action"], tuple(r["items"])) for r in list_rules(st, status=(ACTIVE, PAUSED))}
    out = []
    for n in notes or []:
        if str(n.get("type")) != "constraint" or str(n.get("id")) in dismissed:
            continue
        val = str(n.get("value") or "")
        m = re.search(r"(?:mention|avoid|omit|off|out|show|include)\s+(?:the\s+)?(.+)", val, re.I)
        if not m or not re.search(r"do ?n['’]?t|do not|avoid|leave|omit|never", val, re.I):
            continue
        hits = _find(re.sub(r"\s+(?:on|in|from)\s+(?:my|the)\b.*$", "", m.group(1)).strip(" ."), its)
        if len(hits) != 1:
            continue
        it = hits[0]
        if ("hide", (it["id"],)) in have:
            continue
        out.append({"fact": n.get("id"), "item": it["id"], "label": it["label"],
                    "text": f"From your chat: \"{val}\". Hide {it['label']} on every resume?"})
    return out


def settle_constraint(st: Store, fact_id, item: str | None, save: bool, words: str = "") -> int | None:
    done = _meta_json(st, "constraint_offers_done", [])
    done = (done + [str(fact_id)])[-200:]
    st.set_meta("constraint_offers_done", json.dumps(done))
    if save and item:
        return add_rule(st, "hide", [item], S.ALL, "", words=words, source="chat", source_ref=f"memory_fact:{fact_id}")
    return None


# ---------------------------------------------------------------------------- history
def record_application(st: Store, app_id, *, company: str = "", role: str = "", url: str = "",
                       labels: dict | None = None, shown=(), applied=()) -> None:
    """One ``application`` row (id = the resume record): the job's labels, the item ids the page
    showed and the choices that changed it. Each such choice counts one more use."""
    lab = labels or {}
    with st.tx() as c:
        c.execute("INSERT OR REPLACE INTO application (id, person, company, role, url, job_family, industry,"
                  " company_type, shown, rules, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (int(app_id), PERSON, str(company)[:200], str(role)[:200], str(url)[:500],
                   str(lab.get("family") or ""), str(lab.get("industry") or ""),
                   str(lab.get("company_type") or ""), json.dumps(list(shown)), json.dumps(sorted(set(applied))),
                   time.time()))
    mark_applied(st, applied)
    st.log("application_added", ref=f"application:{int(app_id)}", n=len(list(applied)))
