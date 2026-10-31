"""The memory page, "What SponsorJobs knows about you" (docs/applicant-brain.md, section 13,
Phase 6): every fact grouped, each with where it came from and when, and the pieces Forget,
Export and Erase need.

Pure helpers over the brain (``applicant/store.py``) and the inputs the app hands in (the saved
profile, the prefs view, the answer library). The routes live in ``ui/app.py``.

Three rules hold here:

* **Self-identification values never leave this module in the page.** ``eeo_rows`` says only
  "saved" or "declined" per question; the value itself is read by ``reveal`` when the person taps
  Show, for their own screen only. Nothing here reaches a model, the broker, Telegram or a log.
* **One row per thing the person knows about themselves**, not one per store: a salary the
  answer library, the prefs file and a chat all hold is ONE row, and Forget clears every copy.
* **Words, not codes.** "You, on Telegram, Oct 28"; "From your resume, Oct 5".
"""
from __future__ import annotations

import datetime as _dt
import json
import re

from applicant import keys as K
from applicant.store import ACTIVE, NEEDS_CHECK, PERSON, REPLACED, STORE_ANSWERS, STORE_PREFS, Store

CURRENT = (ACTIVE, NEEDS_CHECK)


# ---------------------------------------------------------------------------- dates and sources
def when(ts) -> str:
    """"Oct 28" this year, "Oct 28, 2025" before it; "" when unknown."""
    try:
        d = _dt.datetime.fromtimestamp(float(ts)).date()
    except (TypeError, ValueError, OSError):
        return ""
    s = f"{d.strftime('%b')} {d.day}"
    return s if d.year == _dt.date.today().year else f"{s}, {d.year}"


def iso_when(iso: str) -> str:
    try:
        d = _dt.date.fromisoformat(str(iso)[:10])
    except ValueError:
        return ""
    return when(_dt.datetime(d.year, d.month, d.day).timestamp())


# Where a fact came from, in the person's words. The source ref says more than the source for
# the answer library's rows (their source is only ever "you" or "drafted").
_VIA = (("ask_telegram", "You, on Telegram"), ("late_telegram", "You, on Telegram"),
        ("ask_bar", "You, on the apply bar"), ("card", "You, on the form answers card"),
        ("answers_page", "You, in your form answers"), ("memory_page", "You, on this page"),
        ("package", "You, on an application"), ("extension", "You, in the browser extension"),
        ("details", "You, in setup"), ("profile", "You, on this page"), ("onboarding", "You, in setup"),
        ("builder", "You, in the resume builder"), ("diary:", "You, in the resume builder"))
_SOURCE = {"resume": "From your resume", "chat": "From your chat", "telegram_chat": "From your Telegram chat",
           "drafted": "Drafted from your profile", "derived": "Worked out from your answers",
           "migration": "Saved by an earlier version", "card": "You, on the form answers card",
           "onboarding": "You, in setup", "ask_bar": "You, on the apply bar", "ask_telegram": "You, on Telegram",
           "late_telegram": "You, on Telegram", "extension": "You, in the browser extension"}


def source_words(source: str, ref: str = "", store: str = "") -> str:
    src, ref = str(source or ""), str(ref or "")
    if src in ("resume", "chat", "telegram_chat", "drafted", "derived", "migration"):
        return _SOURCE[src]
    for head, words in _VIA:
        if ref.startswith(head):
            return words
    if src in _SOURCE:
        return _SOURCE[src]
    if ref.startswith(STORE_ANSWERS) or ref.startswith(STORE_PREFS):
        return "You, on an application form" if ref.startswith(STORE_ANSWERS) else "You, in your form answers"
    return "You"


def meta(row) -> dict:
    """{source, when, at} for one fact row (``source`` is the line the page shows)."""
    if row is None:
        return {"source": "", "when": "", "at": 0}
    at = float(row["updated_at"] or row["created_at"] or 0)
    return {"source": source_words(row["source"], row["source_ref"], row["store"]), "when": when(at), "at": at}


def newest(st: Store | None, key: str, subject: str = "", stores=None, statuses=CURRENT):
    """The newest current row of a key and subject across the given stores (all by default)."""
    if st is None:
        return None
    sql = f"SELECT * FROM fact WHERE person=? AND key=? AND subject=? AND status IN ({','.join('?' * len(statuses))})"
    args = [PERSON, key, subject, *statuses]
    if stores is not None:
        sql += f" AND store IN ({','.join('?' * len(stores))})"
        args += list(stores)
    rows = st.q(sql + " ORDER BY updated_at DESC, id DESC LIMIT 1", args)
    return rows[0] if rows else None


def diary_refs(st: Store | None, key: str, subject: str | None = None) -> list[str]:
    """Every chat line (``diary:<file>:<line>``) a fact of this key came from, its history too."""
    if st is None:
        return []
    sql, args = "SELECT source_ref FROM fact WHERE person=? AND key=?", [PERSON, key]
    if subject is not None:
        sql += " AND subject=?"
        args.append(subject)
    out = []
    for r in st.q(sql, args):
        ref = str(r["source_ref"] or "")
        if ref.startswith("diary:") and ref not in out:
            out.append(ref)
    return out


# ---------------------------------------------------------------------------- About you
# (identity field, label, brain key)
CONTACT = (("name", "Name", "name"), ("email", "Email", "email"), ("phone", "Phone", "phone"),
           ("address", "Address", "address"), ("linkedin", "LinkedIn", "@linkedin"),
           ("github", "GitHub", "github"), ("blog", "Website", "@portfolio"))
CONTACT_KEY = {f: k for f, _l, k in CONTACT}


def contact_rows(st: Store | None, identity: dict) -> list[dict]:
    ident = identity if isinstance(identity, dict) else {}
    out = []
    for fld, label, key in CONTACT:
        v = " ".join(str(ident.get(fld) or "").split())
        if not v:
            continue
        m = meta(newest(st, key, "", stores=("",)))
        if not m["source"]:
            m = {"source": "From your profile", "when": "", "at": 0}
        out.append({"id": f"contact:{fld}", "field": fld, "label": label, "value": v, **m,
                    "chat": bool(diary_refs(st, key, ""))})
    return out


# ---------------------------------------------------------------------------- Work and visa
def workauth_text(cc: str, e: dict) -> str:
    """"F-1 OPT. Authorized to work. Needs sponsorship later. Permit ends Jun 30, 2026.\""""
    from autoapply import workauth as W
    bits = []
    lab = dict(W.STATUSES.get(cc, [])).get(str(e.get("status") or ""), "")
    if lab:
        bits.append(lab)
    implied = str(e.get("status") or "") in (W.CITIZEN | W.PERMANENT)
    if not implied:
        a = str(e.get("authorized") or "")
        if a:
            bits.append("Authorized to work" if a == "Yes" else "Not authorized to work")
        sn, sf = str(e.get("sponsor_now") or ""), str(e.get("sponsor_future") or "")
        if sn == "Yes":
            bits.append("Needs sponsorship now")
        elif sf == "Yes":
            bits.append("Needs sponsorship later")
        elif sn == "No" and sf == "No":
            bits.append("No sponsorship needed")
        end = str(e.get("auth_end") or "")
        if end:
            from applicant.expiry import fmt_date, parse_date
            d = parse_date(end)
            bits.append("Permit ends " + (fmt_date(d) if d else end))
    return ". ".join(bits) + ("." if bits else "")


def work_rows(st: Store | None, wa: dict, lib, prefs: dict, employers_now=()) -> list[dict]:
    from autoapply import workauth as W
    out = []
    for cc in W.COUNTRIES:
        e = ((wa or {}).get("countries") or {}).get(cc)
        if not isinstance(e, dict) or not any(str(v or "").strip() for v in e.values()):
            continue
        row = newest(st, "workauth", cc, stores=(STORE_PREFS,))
        ended = None
        try:
            from applicant.expiry import workauth_ended
            ended = workauth_ended(wa, cc)
        except Exception:      # noqa: BLE001
            ended = None
        out.append({"id": f"wa:{cc}", "kind": "workauth", "cc": cc, "label": W.COUNTRY_NAMES.get(cc, cc),
                    "value": workauth_text(cc, e), "ended": bool(ended), **meta(row)})
    rel = str((wa or {}).get("relocate") or "")
    if rel:
        out.append({"id": "key:relocate", "kind": "relocate", "label": "Willing to relocate", "value": rel,
                    **meta(newest(st, "relocate", "", stores=(STORE_PREFS,)))})
    for lkey, label in (("@citizenship", "Citizenship"), ("@citizenship2", "Second citizenship")):
        hit = lib.best(lkey) if lib is not None else None
        if hit and str(hit.get("answer") or "").strip():
            from autoapply import countries
            v = countries.canonical(hit["answer"]) or hit["answer"]
            out.append({"id": f"key:{lkey}", "kind": "citizenship", "field": lkey.lstrip("@"), "label": label,
                        "value": v, **meta(newest(st, lkey, "", stores=(STORE_ANSWERS,))),
                        "chat": bool(diary_refs(st, lkey))})
    o18 = str((prefs or {}).get("over_18") or "")
    if o18:
        out.append({"id": "key:@over_18", "kind": "yes_no", "key": "@over_18", "label": "At least 18 years old",
                    "value": o18, **meta(newest(st, "@over_18", ""))})
    if st is not None:
        for r in st.q("SELECT * FROM fact WHERE person=? AND key='@previously_worked' AND store='' AND status=?"
                      " AND subject<>'' ORDER BY updated_at DESC", (PERSON, ACTIVE)):
            out.append({"id": f"worked:{r['subject']}", "kind": "worked", "label": f"Worked at {r['subject'].title()} before",
                        "value": r["value"], **meta(r)})
    return out


# ---------------------------------------------------------------------------- Form answers
# (key, label, prefs field or "", editor kind)
ANSWERS = (("@salary", "Desired annual salary", "desired_salary", "salary"),
           ("@start_date", "Earliest start date", "earliest_start", "text"),
           ("@notice_period", "Notice period", "", "text"),
           ("@years_experience", "Years of experience", "years_experience", "text"),
           ("@heard_about", "How you heard about a job", "hear_about_us", "text"),
           ("accept_required_terms", "Terms a site requires to apply", "", "terms"))
ANSWER_KEYS = frozenset(k for k, *_ in ANSWERS) | {"@citizenship", "@citizenship2", "@over_18", "relocate"}
PREFS_FIELD = {k: f for k, _l, f, _e in ANSWERS if f} | {"@over_18": "over_18"}


def answer_rows(st: Store | None, lib, prefs: dict) -> list[dict]:
    out = []
    p = prefs or {}
    for key, label, fld, kind in ANSWERS:
        if key == "accept_required_terms":
            if p.get("accept_required_terms") == "always":
                out.append({"id": f"key:{key}", "key": key, "kind": kind, "label": label,
                            "value": "Accept without asking me", **meta(newest(st, key, ""))})
            continue
        hit = lib.best(key) if lib is not None else None
        v = str((hit or {}).get("answer") or "").strip() or str(p.get(fld) or "").strip() if fld else \
            str((hit or {}).get("answer") or "").strip()
        if not v:
            row = newest(st, key, "", stores=("",), statuses=(ACTIVE,))
            v = str(row["value"]) if row is not None else ""
        if not v:
            continue
        shown = v
        if key == "@salary":
            from autoapply.basics import parse_salary
            n = parse_salary(v)
            shown = f"${n:,}" if n else v
        out.append({"id": f"key:{key}", "key": key, "kind": kind, "label": label, "value": shown, "raw": v,
                    **meta(newest(st, key, "", statuses=(ACTIVE,))), "chat": bool(diary_refs(st, key, ""))})
    return out


def one_question(rows: list[dict]) -> list[dict]:
    """The answer library's rows the page lists under Other answers: never a self-identification
    answer (those sit behind Show) and never a key a row above already shows."""
    from autoapply import answers as A
    out = []
    for r in rows or []:
        k = str(r.get("key") or "")
        if k in ANSWER_KEYS or A.normalize(r.get("question") or "") in ANSWER_KEYS:
            continue
        if K.sensitivity(K.fact_key(k), str(r.get("question") or "")) == K.EEO:
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------------------- Self-identification
EEO = (("gender", "Gender"), ("hispanic", "Hispanic or Latino"), ("race", "Race"),
       ("veteran", "Veteran status"), ("disability", "Disability"))


def eeo_rows(st: Store | None, lib, decline: bool) -> list[dict]:
    """One row per self-identification question: ``state`` is "saved", "declined" or "" (never
    answered), with where and when. The value itself is NOT here (``reveal`` reads it)."""
    from autoapply import basics
    d = basics.read(lib, eeo_decline=False) if lib is not None else {"values": {}}
    out = []
    for fld, label in EEO:
        v = str((d.get("values") or {}).get(fld) or "")
        key = basics.KEY[fld]
        if v:
            state = "declined" if v == "decline" else "saved"
        else:
            state = "declined" if decline else ""
        row = newest(st, key, "", stores=(STORE_ANSWERS,)) if v else (
            newest(st, "@eeo_decline", "", stores=(STORE_PREFS,)) if decline else None)
        out.append({"id": f"eeo:{fld}", "field": fld, "label": label, "state": state, "own": bool(v), **meta(row)})
    return out


def reveal(lib) -> dict:
    """{field: words} for the self-identification answers, for the person's own screen when they
    tap Show. Local only: never logged, never sent anywhere."""
    from autoapply import basics
    d = basics.read(lib) if lib is not None else {"values": {}}
    out = {}
    for fld, _label in EEO:
        v = str((d.get("values") or {}).get(fld) or "")
        if v:
            out[fld] = basics._words(fld, v) or v
    return out


# ---------------------------------------------------------------------------- Career
def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def career(st: Store | None, profile: dict, essentials: dict) -> dict:
    """The saved resume material, section by section, each entry with where it came from (its
    open fact) and when. Edited and forgotten one entry at a time."""
    from applicant.migrate import _profile_items
    p = profile if isinstance(profile, dict) else {}
    ess = essentials if isinstance(essentials, dict) else {}
    facts = {}
    if st is not None:
        for r in st.q("SELECT * FROM fact WHERE person=? AND store='' AND status=? AND key IN"
                      " ('role','school','project','skill') ORDER BY id", (PERSON, ACTIVE)):
            facts[(r["key"], r["subject"])] = r
    items = {(k, sub): v for k, sub, v, part in _profile_items(p) if part != "contact"}

    def m(key, subject):
        r = facts.get((key, subject))
        return meta(r) if r is not None else {"source": "From your profile", "when": "", "at": 0}

    roles = []
    for e in p.get("experience") or ess.get("experience") or []:
        if not isinstance(e, dict):
            continue
        org = e.get("org") or e.get("company") or ""
        for r in (e.get("roles") if isinstance(e.get("roles"), list) else [e]):
            if not isinstance(r, dict):
                continue
            title = r.get("title") or ""
            sub = f"role:{_norm(org)}|{_norm(title)}"
            roles.append({"org": org, "title": title, "dates": r.get("dates") or e.get("dates") or "",
                          "location": e.get("location") or r.get("location") or "",
                          "bullets": [str(b) for b in (r.get("bullets") or []) if str(b).strip()], **m("role", sub)})
    schools = []
    for ed in p.get("education") or ess.get("education") or []:
        if isinstance(ed, dict):
            schools.append({k: ed.get(k) or "" for k in ("school", "degree", "date", "location")}
                           | {"courses": ed.get("courses") if isinstance(ed.get("courses"), str)
                              else ", ".join(ed.get("courses") or [])}
                           | m("school", f"school:{_norm(ed.get('school'))}"))
    projects = []
    for pr in p.get("projects") or []:
        if isinstance(pr, dict):
            name = pr.get("title") or pr.get("name") or pr.get("org") or ""
            projects.append({"org": pr.get("org") or pr.get("title") or "", "link": pr.get("link") or pr.get("url") or "",
                             "location": pr.get("location") or "", "dates": pr.get("dates") or "",
                             "bullets": [str(b) for b in (pr.get("bullets") or []) if str(b).strip()],
                             **m("project", f"project:{_norm(name)}")})
    skills = p.get("skills") if isinstance(p.get("skills"), dict) else {}
    del items
    return {"summary": str(p.get("summary") or ess.get("summary") or ""), "roles": roles, "schools": schools,
            "projects": projects, "skills": {str(k): str(v) for k, v in (skills or {}).items()},
            "skills_input": [str(s) for s in (ess.get("skills_input") or [])],
            "extracurricular": [{"title": x.get("title") or "", "date": x.get("date") or "",
                                 "bullets": [str(b) for b in (x.get("bullets") or []) if str(b).strip()]}
                                for x in (p.get("extracurricular") or []) if isinstance(x, dict)],
            "interests": str(p.get("interests") or ""),
            "declined": [d for d in (ess.get("declined") or []) if not str(d).startswith(("project_dates:", "project_location:"))]}


def notes(st: Store | None) -> list[dict]:
    """The chat's preferences, constraints and other notes, newest first."""
    if st is None:
        return []
    rows = st.q("SELECT * FROM fact WHERE person=? AND store='' AND status=? AND key IN ('preference','constraint','fact')"
                " ORDER BY updated_at DESC, id DESC", (PERSON, ACTIVE))
    words = {"preference": "Preference", "constraint": "Leave out", "fact": "Note"}
    return [{"id": f"note:{r['id']}", "label": words.get(r["key"], "Note"), "value": r["value"], **meta(r),
             "chat": str(r["source_ref"] or "").startswith("diary:")} for r in rows]


# ---------------------------------------------------------------------------- I noted, Needs a look
def _key_label(key: str) -> str:
    k = K.get(key)
    if k is not None and k.ask:
        return k.ask
    return key.lstrip("@").replace("_", " ").capitalize()


def noted(st: Store | None) -> list[dict]:
    """Facts the chat proposed for a form answer, waiting for the person's tap: "I noted your
    salary is $120,000. Save it?". Never a self-identification value (the store refuses those)."""
    if st is None:
        return []
    out = []
    for r in st.q("SELECT * FROM fact WHERE person=? AND store='' AND status=? AND source IN ('chat','telegram_chat')"
                  " AND check_reason IN ('from_chat','unconfirmed_legal') ORDER BY id DESC", (PERSON, NEEDS_CHECK)):
        if K.sensitivity(r["key"]) == K.EEO:
            continue
        out.append({"id": f"noted:{r['id']}", "fact": r["id"], "key": r["key"], "label": _key_label(r["key"]),
                    "value": r["value"], **meta(r)})
    return out


def needs(st: Store | None, *, conflict: dict | None, wa: dict, lib, prefs: dict) -> list[dict]:
    """The things to settle, each with the buttons that settle it (``acts``: [{act, label,
    value?}]). Conflicts, raw replies an older run saved, values that expired, and legal facts read
    from a resume that the person has not confirmed."""
    out: list[dict] = []
    if conflict:
        out.append({"id": "conflict:@salary", "kind": "conflict", "key": conflict.get("key"), "text": conflict.get("text"),
                    "acts": [{"act": "settle", "label": c["label"], "value": c["value"]} for c in conflict.get("choices") or []]})
    if st is None:
        return out
    from autoapply import workauth as W
    from applicant import expiry as E
    for cc in W.COUNTRIES:
        d = E.workauth_ended(wa, cc)
        if d is not None:
            out.append({"id": f"wa:{cc}", "kind": "expired", "cc": cc,
                        "text": f"Your work authorization for {W.COUNTRY_NAMES.get(cc, cc)} ended on {E.fmt_date(d)}.",
                        "acts": [{"act": "edit_wa", "label": "Update it"}]})
    sal = lib.best("@salary") if lib is not None else None
    if sal and E.salary_stale(sal) and not conflict:
        from autoapply.basics import parse_salary
        n = parse_salary(sal["answer"])
        if n:
            out.append({"id": "key:@salary", "kind": "stale", "key": "@salary",
                        "text": f"You saved ${n:,} as your salary on {when(sal.get('updated_at'))}. Still right?",
                        "acts": [{"act": "keep_salary", "label": "Still right", "value": str(n)},
                                 {"act": "edit", "label": "Change it", "value": "key:@salary"}]})
    start = (lib.best("@start_date") or {}).get("answer") if lib is not None else ""
    start = start or (prefs or {}).get("earliest_start") or ""
    d = E.start_date_passed(start)
    if d is not None:
        out.append({"id": "key:@start_date", "kind": "expired", "key": "@start_date",
                    "text": f"Your saved start date ({E.fmt_date(d)}) has passed.",
                    "acts": [{"act": "edit", "label": "Change it", "value": "key:@start_date"}]})
    for r in st.q("SELECT * FROM fact WHERE person=? AND status=? ORDER BY id", (PERSON, NEEDS_CHECK)):
        reason, src = r["check_reason"], r["source"]
        if reason == "raw_reply" and r["store"] == STORE_ANSWERS:
            q = str(r["question"] or "").strip() or _key_label(r["key"])
            out.append({"id": f"lib:{K.library_key(r['key'])}", "kind": "raw_reply",
                        "text": f"An older run saved a whole reply as your answer to \"{q[:90]}\". It is never used.",
                        "acts": [{"act": "forget", "label": "Forget it", "value": f"lib:{K.library_key(r['key'])}"}]})
        elif reason == "drafted_eeo":
            out.append({"id": f"lib:{K.library_key(r['key'])}", "kind": "drafted_eeo",
                        "text": f"A self-identification answer ({_key_label(r['key']).lower()}) was drafted for you by an"
                                " older version. Answer it yourself under Self-identification, or forget it.",
                        "acts": [{"act": "forget", "label": "Forget it", "value": f"lib:{K.library_key(r['key'])}"}]})
        elif reason == "unconfirmed_legal" and src not in ("chat", "telegram_chat"):
            out.append({"id": f"fact:{r['id']}", "kind": "unconfirmed", "fact": r["id"],
                        "text": f"Your resume says {_key_label(r['key']).lower().rstrip('?')}: {str(r['value'])[:80]}. Is that right?",
                        "acts": [{"act": "confirm", "label": "Yes, keep it", "value": str(r["id"])},
                                 {"act": "forget", "label": "Forget it", "value": f"fact:{r['id']}"}]})
        elif reason == "conflict" and r["key"] != "@salary" and r["store"] == "":
            out.append({"id": f"fact:{r['id']}", "kind": "conflict_one", "fact": r["id"],
                        "text": f"{_key_label(r['key'])}: you saved one value, and {source_words(src, r['source_ref']).lower()}"
                                f" gave {str(r['value'])[:80]}. Which one?",
                        "acts": [{"act": "confirm", "label": f"Use {str(r['value'])[:40]}", "value": str(r["id"])},
                                 {"act": "forget", "label": "Keep mine", "value": f"fact:{r['id']}"}]})
    return out


def confirm_fact(st: Store, fact_id: int) -> dict | None:
    """The person kept a proposed fact: it becomes their own (active, confirmed, 1.0), and an older
    current value of the same key and subject is kept as history."""
    rows = st.q("SELECT * FROM fact WHERE id=? AND person=?", (int(fact_id), PERSON))
    if not rows:
        return None
    r = rows[0]
    with st.tx() as c:
        if not K.is_multi(r["key"]):
            c.execute("UPDATE fact SET status=?, replaced_by=? WHERE person=? AND key=? AND subject=? AND store=?"
                      " AND id<>? AND status IN (?,?)", (REPLACED, r["id"], PERSON, r["key"], r["subject"], r["store"],
                                                       r["id"], ACTIVE, NEEDS_CHECK))
        c.execute("UPDATE fact SET status=?, check_reason='', confirmed=1, confidence=1.0 WHERE id=?",
                  (ACTIVE, r["id"]))
    st.log("fact_confirmed", key=r["key"] if K.sensitivity(r["key"]) != K.EEO else "eeo")
    return dict(r)


# ---------------------------------------------------------------------------- History
def _fill_words(key: str) -> str:
    short = {"@salary": "salary", "@citizenship": "citizenship", "@citizenship2": "second citizenship",
             "@start_date": "start date", "@years_experience": "years of experience", "@heard_about": "how you heard",
             "@over_18": "age", "workauth": "work authorization", "@previously_worked": "worked here before",
             "@notice_period": "notice period", "relocate": "relocation"}
    if key in short:
        return short[key]
    if key.startswith("q:"):
        return ""
    k = K.get(key)
    return (k.ask if k is not None and k.ask else key.lstrip("@")).lower()


def history(st: Store | None, its: list[dict], label_of, *, limit: int = 30) -> dict:
    """{applications, documents}: each application with what the resume showed and what the form
    got from memory ("filled salary and citizenship from memory; gender: Prefer not to say"), and
    each file the person shared. A self-identification fill names only "saved answer" or "Prefer
    not to say", never the value."""
    if st is None:
        return {"applications": [], "documents": []}
    from applicant import keys as _K
    apps = []
    for a in st.q("SELECT * FROM application WHERE person=? ORDER BY created_at DESC LIMIT ?", (PERSON, int(limit))):
        try:
            shown = [label_of(i, its) for i in json.loads(a["shown"] or "[]")]
        except ValueError:
            shown = []
        fills = st.q("SELECT DISTINCT key FROM fill WHERE person=? AND application_id=? ORDER BY id", (PERSON, a["id"]))
        normal, eeo_bits, other = [], [], 0
        for f in fills:
            k = str(f["key"] or "")
            if _K.sensitivity(k) == _K.EEO:
                cur = newest(st, k, "", stores=(STORE_ANSWERS,), statuses=(ACTIVE,))
                dec = cur is not None and str(cur["code"] or "") == "decline"
                eeo_bits.append(f"{_fill_words(k)}: {'Prefer not to say' if dec else 'your saved answer'}")
            else:
                w = _fill_words(k)
                if w and w not in normal:
                    normal.append(w)
                elif not w:
                    other += 1
        apps.append({"id": a["id"], "company": a["company"], "role": a["role"], "when": when(a["created_at"]),
                     "shown": [s for s in shown if s], "filled": normal, "other": other, "eeo": eeo_bits})
    docs = [{"id": f"doc:{d['id']}", "name": d["name"], "kind": d["kind"], "channel": d["channel"],
             "facts": d["facts"], "when": when(d["created_at"])}
            for d in st.q("SELECT * FROM document WHERE person=? ORDER BY created_at DESC LIMIT ?", (PERSON, int(limit)))]
    return {"applications": apps, "documents": docs}


def record_fills(st: Store, application_id, fills) -> int:
    """One ``fill`` row per form field filled from memory on this application (label, key,
    origin), never the value. The same label twice on one application is one row."""
    from autoapply import answers as A
    from autoapply import workauth as W
    n = 0
    try:
        aid = int(application_id)
    except (TypeError, ValueError):
        return 0
    have = {r["label"] for r in st.q("SELECT label FROM fill WHERE person=? AND application_id=?", (PERSON, aid))}
    rows = []
    for label, origin in fills or ():
        lab = " ".join(str(label or "").split())[:200]
        if not lab or lab in have:
            continue
        have.add(lab)
        key = A.normalize(lab)
        if not key.startswith("@"):
            key = "workauth" if W.classify(lab) else (K.fact_key(key) if key else "")
        if not key:
            continue
        rows.append((PERSON, aid, lab, key, str(origin or "")[:20]))
    if rows:
        import time
        with st.tx() as c:
            c.executemany("INSERT INTO fill (person, application_id, label, key, origin, at) VALUES (?,?,?,?,?,?)",
                          [r + (time.time(),) for r in rows])
        n = len(rows)
    return n
