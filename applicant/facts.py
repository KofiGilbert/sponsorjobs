"""Phase 3 of the applicant brain (docs/applicant-brain.md, section 15): every writer saves facts
with a source ref, and the readers ask the brain before asking the person.

Writers (all brain-only facts, ``store = ''``, beside the legacy views of Phase 1):

* ``write_contact``: contact details from the details step, the resume import, My profile, the
  builder's chat and the extension's corrections, with the address split into its parts
  (street, city, state, ZIP, country) by code (``split_address``);
* ``write_profile``: the open facts of a resume (roles, schools, projects, skills) at 0.9, with a
  ``document`` row (``add_document``) when they came from a file;
* ``sync_chat``: the facts the intake chat distilled (``memory_facts``), at their own confidence.
  A fact naming a canonical key (salary, start date) waits as ``needs_check``; a legal one as
  ``unconfirmed_legal``; a self-identification one is refused by the store;
* ``save_worked_here``: "Have you worked here before?" per company (subject = company key).

Readers: ``contact`` (the values a form takes, at confidence 0.9 or more), ``notes`` (the chat's
preferences and constraints), ``employers`` and ``worked_here``.

Conflicts (``salary_conflict``): two current salaries that disagree become ONE question to the
person ("Forms say $90,000 (March); you said $120,000 (Oct 28). Which one?"), shown on Your answers
and asked once by the apply agent (``mark_asked``), never again for the same pair.

The rule "a lower-confidence value never replaces a confirmed one" lives in ``put`` here and in
``Store.add_fact``: the newer value waits as ``needs_check`` (``conflict``).

Personal data: nothing here leaves the machine, and no value is logged (the event log keeps keys,
sources and counts).
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import time

from applicant import keys as K
from applicant.store import ACTIVE, NEEDS_CHECK, PERSON, REPLACED, STORE_ANSWERS, STORE_PREFS, Store

# ---------------------------------------------------------------------------- companies
def company_key(name) -> str:
    """The key a company is remembered under ("Acme Corp." and "ACME Corporation" are one):
    the employer normal form the sponsor data and the learning store use."""
    s = " ".join(str(name or "").split())
    if not s:
        return ""
    from autoapply.learning import company_key as _ck
    return _ck(s)


def same_company(a, b) -> bool:
    """The same employer: equal keys, or one key the other's first whole words ("Stanbic" and
    "Stanbic Bank Ghana"). Two-letter keys only match exactly."""
    x, y = company_key(a), company_key(b)
    if not x or not y:
        return False
    if x == y:
        return True
    s, l = (x, y) if len(x) <= len(y) else (y, x)
    return len(s) >= 4 and l.startswith(s + " ")


# ---------------------------------------------------------------------------- addresses
def _states() -> dict:
    from tailoring.conform import _US_STATES
    return _US_STATES


_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_STATE_ZIP = re.compile(r"^([A-Za-z]{2})\.?\s+(\d{5}(?:-\d{4})?)$")
_STREET = re.compile(r"^\d{1,6}[A-Za-z]?\s+\S|\b(st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|"
                     r"way|ct|court|pl|place|pkwy|parkway|hwy|highway|suite|ste|apt|unit)\b\.?", re.I)
PARTS = ("street", "city", "state", "postal_code", "country")


def split_address(address="", location="") -> dict:
    """{street, city, state, postal_code, country} from a one-line address and a "City, ST"
    location, by code. Conservative: a part it cannot read is left out, never guessed. The
    country is the one the line names, else the United States when the state is a US state."""
    out: dict = {}
    states = _states()
    codes = set(states.values())

    def is_state(x: str) -> bool:
        x = str(x or "").strip(" .")
        m = _STATE_ZIP.match(x)
        return (len(x) == 2 and x.upper() in codes) or x.lower() in states or bool(m and m.group(1).upper() in codes)

    def take(line: str):
        parts = [p.strip(" .") for p in str(line or "").split(",") if p.strip(" .")]
        if not parts:
            return
        from autoapply import countries
        # The country, when the line ends with one (never a US state that shares a country's
        # name: "Atlanta, Georgia" is in the United States).
        if len(parts) > 1 and not is_state(parts[-1]):
            cc = countries.canonical(parts[-1])
            if cc:
                out.setdefault("country", cc)
                parts = parts[:-1]
        last = parts[-1]
        m = _STATE_ZIP.match(last)
        st = ""
        if m and m.group(1).upper() in codes:
            st = m.group(1).upper()
            out.setdefault("postal_code", m.group(2))
            parts = parts[:-1]
        elif len(last) == 2 and last.upper() in codes and len(parts) > 1:
            st = last.upper()
            parts = parts[:-1]
        elif last.lower() in states and len(parts) > 1:
            st = states[last.lower()]
            parts = parts[:-1]
        else:
            z = _ZIP.search(last)
            if z and len(parts) > 1:
                out.setdefault("postal_code", z.group(1))
                rest = _ZIP.sub("", last).strip(" ,.")
                if rest.lower() in states:              # "Illinois 62701"
                    st, rest = states[rest.lower()], ""
                parts = parts[:-1] + ([rest] if rest else [])
        if st:
            out.setdefault("state", st)
            out.setdefault("country", "United States")
        if not parts:
            return
        if len(parts) == 1:
            if _STREET.search(parts[0]) and re.match(r"\d", parts[0]):
                if st or out.get("postal_code"):
                    out.setdefault("street", parts[0])
            elif st or out.get("country"):
                out.setdefault("city", parts[0])
            return
        out.setdefault("city", parts[-1])
        street = ", ".join(parts[:-1])
        if _STREET.search(street):
            out.setdefault("street", street)

    take(address)
    take(location)
    return {k: v for k, v in out.items() if str(v or "").strip()}


# ---------------------------------------------------------------------------- one fact
def _val(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def put(st: Store, key: str, value, *, source: str, subject: str = "", source_ref: str = "",
        question: str = "", confidence: float | None = None, status: str = ACTIVE,
        check_reason: str = "", code: str = "", log: bool = True, confirm: bool = False) -> int | None:
    """One brain-only fact (``store = ''``), idempotent. The same value already current changes
    nothing (``confirm``: the person saw and kept it, which raises its confidence); a new value replaces the
    current one of the same key and subject, kept as history. A value from a source less sure
    than a confirmed current value never replaces it: it waits as ``needs_check`` (``conflict``)
    for the person to settle. Returns the new fact's id, or None when nothing was written."""
    val = _val(value).strip()
    if not val:
        return None
    conf = K.confidence_for(source) if confidence is None else float(confidence)
    cur = st.current(key, subject, "")
    active = [r for r in cur if r["status"] == ACTIVE]
    same = [r for r in cur if r["value"] == val]
    if same:
        r = same[0]
        if confirm and r["status"] == ACTIVE and conf > float(r["confidence"] or 0) and status == ACTIVE:
            st.update_fact(r["id"], confidence=conf, confirmed=1 if source not in K.UNCONFIRMED_SOURCES else r["confirmed"])
        return None
    top = active[0] if active else None
    with st.tx() as c:
        if top is not None and int(top["confirmed"] or 0) and float(top["confidence"] or 0) > conf and status == ACTIVE:
            # the person's confirmed value stays; the less sure one waits for them
            new_id = st.add_fact(key, val, source=source, subject=subject, source_ref=source_ref, question=question,
                                 confidence=conf, status=NEEDS_CHECK, check_reason="conflict", code=code,
                                 replace=False)
            old = [r["id"] for r in cur if r["status"] == NEEDS_CHECK and r["check_reason"] == "conflict"
                   and r["source"] == source]
        else:
            new_id = st.add_fact(key, val, source=source, subject=subject, source_ref=source_ref, question=question,
                                 confidence=conf, status=status, check_reason=check_reason, code=code, replace=False)
            old = [r["id"] for r in cur] if status == ACTIVE else \
                [r["id"] for r in cur if r["status"] == NEEDS_CHECK and r["source"] == source]
        if old:
            c.execute(f"UPDATE fact SET status=?, replaced_by=? WHERE id IN ({','.join('?' * len(old))})",
                      (REPLACED, new_id, *old))
    if log:
        st.log("fact_added", key=key if K.sensitivity(key, question) != K.EEO else "eeo", source=source)
    return new_id


def _current_value(st: Store, key: str, subject: str = "", min_conf: float = 0.0) -> str:
    for r in st.current(key, subject, ""):
        if r["status"] == ACTIVE and float(r["confidence"] or 0) >= min_conf:
            return str(r["value"] or "")
    return ""


# ---------------------------------------------------------------------------- contact
# (identity field, brain key). The address parts are written from the split.
CONTACT = (("name", "name"), ("email", "email"), ("phone", "phone"), ("address", "address"),
           ("location", "location"), ("linkedin", "@linkedin"), ("github", "github"),
           ("blog", "@portfolio"), ("website", "@portfolio"))
PART_KEYS = {p: "address." + p for p in PARTS}
# Field names a form or the extension may use for a part.
_PART_ALIASES = {"street": ("street", "address1", "address_line1"), "city": ("city", "town"),
                 "state": ("state", "province", "region"), "postal_code": ("postal_code", "zip", "zip_code", "postcode"),
                 "country": ("country",)}


def write_contact(st: Store, identity: dict | None, *, source: str, source_ref: str = "",
                  confirm: bool = False) -> int:
    """The person's contact details as facts, the address split into its parts. Only the fields
    given (a field left empty never clears a saved one). Returns how many facts changed."""
    ident = identity if isinstance(identity, dict) else {}
    n = 0
    done = set()
    for fld, key in CONTACT:
        v = " ".join(str(ident.get(fld) or "").split())[:300]
        if v and key not in done:
            done.add(key)
            if put(st, key, v, source=source, source_ref=source_ref, confirm=confirm) is not None:
                n += 1
    parts = split_address(ident.get("address"), ident.get("location"))
    given = {}
    for p, names in _PART_ALIASES.items():
        for nm in names:
            v = " ".join(str(ident.get(nm) or "").split())[:120]
            if v:
                given[p] = v
                break
    parts.update(given)
    if parts.get("country"):
        from autoapply import countries
        parts["country"] = countries.canonical(parts["country"]) or parts["country"]
    if not parts:
        return n
    # A new city or state is a new place: the parts this write does not give (an old ZIP) are
    # no longer current. They stay as history, never handed out.
    moved = any(parts.get(p) and _current_value(st, PART_KEYS[p]).lower() not in ("", parts[p].lower())
                for p in ("city", "state"))
    for p in PARTS:
        key = PART_KEYS[p]
        if parts.get(p):
            if put(st, key, parts[p], source=source, source_ref=source_ref, confirm=confirm) is not None:
                n += 1
        elif moved:
            for r in st.current(key, "", ""):
                st.update_fact(r["id"], status=REPLACED)
    return n


def contact(st: Store | None, min_conf: float = 0.9) -> dict:
    """The contact values a form takes, from facts at ``min_conf`` or more (APPLICANT_KEYS names):
    {full_name, email, phone, address, street, city, state, postal_code, country, linkedin,
    github, website}. Missing ones are absent."""
    if st is None:
        return {}
    out = {}
    for name, key in (("full_name", "name"), ("email", "email"), ("phone", "phone"), ("address", "address"),
                      ("linkedin", "@linkedin"), ("github", "github"), ("website", "@portfolio"),
                      ("location", "location")):
        v = _current_value(st, key, "", min_conf)
        if v:
            out[name] = v
    for p in PARTS:
        v = _current_value(st, PART_KEYS[p], "", min_conf)
        if v:
            out[p] = v
    return out


def identity(st: Store | None, min_conf: float = 0.9) -> dict:
    """The contact facts in the profile's identity shape (name, email, phone, address, linkedin,
    github, blog), for the gates that check memory before asking."""
    c = contact(st, min_conf)
    m = {"full_name": "name", "email": "email", "phone": "phone", "address": "address", "linkedin": "linkedin",
         "github": "github", "website": "blog", "location": "location"}
    out = {m[k]: v for k, v in c.items() if k in m}
    if not out.get("address"):
        line = ", ".join(x for x in (c.get("street"), c.get("city"),
                                     " ".join(y for y in (c.get("state"), c.get("postal_code")) if y)) if x)
        if c.get("city") and (c.get("state") or c.get("country")):
            out["address"] = line or out.get("location", "")
    return out


# ---------------------------------------------------------------------------- the resume
def write_profile(st: Store, profile: dict | None, *, source: str = "resume", source_ref: str = "",
                  contact_source: str | None = None) -> int:
    """A resume's open facts (roles, schools, projects, skills) and its contact details. A role
    whose dates changed is a new fact (the old one kept as history). Returns how many changed."""
    from applicant.migrate import _profile_items
    p = profile if isinstance(profile, dict) else {}
    n = write_contact(st, p.get("identity") or {}, source=contact_source or source, source_ref=source_ref)
    for key, subject, value, part in _profile_items(p):
        if part == "contact":
            continue
        if put(st, key, value, source=source, subject=subject, source_ref=source_ref, log=False) is not None:
            n += 1
    if n:
        st.log("profile_facts", source=source, n=n)
    return n


def add_document(st: Store, name: str, *, kind: str = "resume", path: str = "", channel: str = "app",
                 facts: int = 0) -> int:
    """One file the person shared. Returns its id (the facts it gave carry ``document:<id>``)."""
    with st.tx() as c:
        cur = c.execute("INSERT INTO document (person, name, kind, path, channel, facts, created_at)"
                        " VALUES (?,?,?,?,?,?,?)", (PERSON, str(name or "")[:200], str(kind or "")[:20],
                                                    str(path or "")[:500], str(channel or "app")[:20],
                                                    int(facts or 0), time.time()))
        did = int(cur.lastrowid)
    st.log("document_added", ref=f"document:{did}", n=int(facts or 0))
    return did


def set_document_facts(st: Store, did: int, n: int) -> None:
    with st.tx() as c:
        c.execute("UPDATE document SET facts=? WHERE id=?", (int(n), int(did)))


def employers(st: Store | None, profile: dict | None = None) -> list[str]:
    """Every employer the person has: the role facts and the profile's experience."""
    out: list[str] = []
    if st is not None:
        for r in st.q("SELECT value FROM fact WHERE person=? AND key='role' AND store='' AND status=?",
                      (PERSON, ACTIVE)):
            try:
                org = str(json.loads(r["value"]).get("org") or "").strip()
            except (ValueError, AttributeError):
                org = ""
            if org and org not in out:
                out.append(org)
    for e in (profile or {}).get("experience") or []:
        if isinstance(e, dict):
            org = str(e.get("org") or e.get("company") or "").strip()
            if org and org not in out:
                out.append(org)
    return out


# ---------------------------------------------------------------------------- chat facts
def sync_chat(st: Store, rows: list[dict]) -> dict:
    """Make the brain's chat facts (source ``chat``, brain-only) match the chat's distilled facts
    (``memory_facts`` rows, every status): a new or changed row is written (a canonical key as a
    proposal, ``needs_check``), a retracted one is marked replaced (kept as history), and one
    the person deleted is forgotten. Returns counts."""
    from applicant.migrate import _memory_fact_item, _memory_fact_ref
    n = {"added": 0, "retracted": 0, "forgotten": 0, "refused": 0}
    want: dict = {}
    retracted: set = set()
    for mf in sorted([r for r in rows or [] if isinstance(r, dict)], key=lambda r: int(r.get("id") or 0)):
        key, subject, outcome = _memory_fact_item(mf)
        if outcome == "skipped_retracted":
            k2, s2, _o = _memory_fact_item(dict(mf, status="active"))
            if k2:
                retracted.add((k2, s2))
            continue
        if outcome == "refused_eeo":
            n["refused"] += 1
            continue
        if key is None:
            continue
        want[(key, subject)] = (str(mf.get("value") or ""), outcome, _memory_fact_ref(mf),
                                float(mf.get("confidence") or 0.7))
    with st.lock:
        have, every = {}, set()
        for r in st.q("SELECT * FROM fact WHERE person=? AND store='' AND source='chat' ORDER BY id", (PERSON,)):
            every.add((r["key"], r["subject"]))
            if r["status"] in (ACTIVE, NEEDS_CHECK):
                have.setdefault((r["key"], r["subject"]), []).append(r)
        for (key, subject), (value, outcome, ref, conf) in want.items():
            mine = have.get((key, subject)) or []
            if any(r["value"] == value for r in mine):
                continue
            if outcome == NEEDS_CHECK:
                reason = "unconfirmed_legal" if K.sensitivity(key) == K.LEGAL else "from_chat"
                try:
                    got = put(st, key, value, source="chat", subject=subject, source_ref=ref, confidence=conf,
                              status=NEEDS_CHECK, check_reason=reason)
                except Exception:          # noqa: BLE001 - the store refused it (EEO)
                    n["refused"] += 1
                    continue
            else:
                got = put(st, key, value, source="chat", subject=subject, source_ref=ref, confidence=conf)
            if got is not None:
                n["added"] += 1
        for (key, subject) in sorted(every):
            if (key, subject) in want:
                continue
            mine = have.get((key, subject)) or []
            if (key, subject) in retracted:
                for r in mine:
                    st.update_fact(r["id"], status=REPLACED)
                n["retracted"] += 1 if mine else 0
            else:
                with st.tx() as c:
                    ids = [r["id"] for r in st.q("SELECT id FROM fact WHERE person=? AND key=? AND subject=?"
                                                  " AND store='' AND source='chat'", (PERSON, key, subject))]
                    marks = ",".join("?" * len(ids))
                    c.execute(f"DELETE FROM fill WHERE fact_id IN ({marks})", ids)
                    c.execute(f"DELETE FROM fact WHERE id IN ({marks})", ids)
                st.log("fact_forgotten", key=key if K.sensitivity(key) != K.EEO else "eeo", n=len(ids))
                n["forgotten"] += 1
    return n


def notes(st: Store, types=("preference", "constraint")) -> list[dict]:
    """The chat's active preferences and constraints in the memory_facts shape, newest first."""
    ts = tuple(types)
    rows = st.q(f"SELECT * FROM fact WHERE person=? AND store='' AND status=? AND key IN ({','.join('?' * len(ts))})"
                " ORDER BY updated_at DESC, id DESC", (PERSON, ACTIVE, *ts))
    return [{"id": r["id"], "type": r["key"], "key": r["subject"], "value": r["value"],
             "confidence": r["confidence"], "provenance": r["source_ref"], "status": "active"} for r in rows]


# ---------------------------------------------------------------------------- worked here before
WORKED = "@previously_worked"


def _yes_no(answer) -> str:
    from autoapply import workauth
    p = workauth.polarity(str(answer or ""))
    return "" if p is None else ("Yes" if p else "No")


def save_worked_here(st: Store, company: str, answer: str, *, question: str = "", source: str = "you",
                     source_ref: str = "") -> bool:
    """The person's answer to "Have you worked for <company> before?", kept for that company
    only (subject = company key), never for every employer. False when it is not a yes or no."""
    ck, yn = company_key(company), _yes_no(answer)
    if not ck or not yn:
        return False
    put(st, WORKED, yn, source=source, subject=ck, question=question, source_ref=source_ref)
    return True


def worked_here(st: Store | None, company: str, employers_: list | tuple = ()) -> tuple[str, str] | None:
    """("Yes" | "No", origin) for "Have you worked for <company> before?", else None (asked):

    1. the person's own answer for this company (an earlier application to it);
    2. Yes when the company is one of their employers (their resume's roles), with origin
       ``confirm``: the agent asks the person once for this company before it is used;
    3. No when it is not, and the person has answered No for a company outside their employer
       history before (or saved a standing No on Your answers): they answer from that history.
    Never the answer they gave for another company as such."""
    ck = company_key(company)
    if not ck:
        return None
    if st is not None:
        for r in st.current(WORKED, ck, ""):
            if r["status"] == ACTIVE and _yes_no(r["value"]):
                return _yes_no(r["value"]), "library"
    if any(same_company(e, company) for e in employers_ or ()):
        # (Phase 4) Read from the resume, not said by the person: a legal answer is never sent
        # unconfirmed, so this Yes is asked once per company as a one-tap confirm ("Your resume
        # lists Acme. Have you worked for Acme before?"); the answer is then theirs (1. above).
        return "Yes", "confirm"
    if st is None:
        return None
    for r in st.q("SELECT subject, value, store, source FROM fact WHERE person=? AND key=? AND status=?",
                  (PERSON, WORKED, ACTIVE)):
        if _yes_no(r["value"]) != "No":
            continue
        if r["store"] == "" and r["subject"] and not any(same_company(e, r["subject"]) for e in employers_ or ()):
            return "No", "derived"
        if r["store"] == STORE_ANSWERS and r["subject"] == "" and r["source"] == "you":
            return "No", "derived"
    return None


# ---------------------------------------------------------------------------- conflicts
SALARY = "@salary"


def _when(ts) -> str:
    try:
        d = _dt.datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError):
        return ""
    today = _dt.date.today()
    if d.date() == today:
        return "today"
    return d.strftime("%B") if (today - d.date()).days > 45 else d.strftime("%b %-d")


def money(n: int) -> str:
    return f"${int(n):,}"


def salary_values(st: Store) -> list[dict]:
    """Every current salary the person has given, one per amount, oldest first: the answer
    library's (every wording of the question), the extension's (app_prefs.json) and one said in
    chat. [{amount, label, when, at, where}]"""
    from autoapply import answers as A
    from autoapply.basics import parse_salary
    from applicant.library import BrainLibrary
    rows = []
    lib = BrainLibrary(st)
    for r in lib.candidates(SALARY):
        if not A.unusable(SALARY, r["answer"]):
            rows.append((r["answer"], float(r["updated_at"] or 0), "forms"))
    for f in st.facts(store=STORE_PREFS, key=SALARY, status=(ACTIVE, NEEDS_CHECK)):
        rows.append((f["value"], float(f["updated_at"] or 0), "extension"))
    for f in st.facts(store="", key=SALARY, status=(ACTIVE, NEEDS_CHECK)):
        if f["subject"]:
            continue                    # (Phase 4) a salary for jobs in one other country
        rows.append((f["value"], float(f["updated_at"] or 0), "chat" if f["source"] in ("chat", "telegram_chat") else "you"))
    by: dict = {}
    for v, at, where in rows:
        amt = parse_salary(v)
        if not amt:
            continue
        cur = by.get(amt)
        if cur is None or at > cur["at"]:
            by[amt] = {"amount": amt, "label": money(amt), "at": at, "where": where}
    out = sorted(by.values(), key=lambda x: x["at"])
    for x in out:
        x["when"] = _when(x["at"])
    return out


def _sig(values: list[dict]) -> str:
    return ",".join(str(x["amount"]) for x in sorted(values, key=lambda x: x["amount"]))


def salary_conflict(st: Store | None) -> dict | None:
    """The one question two disagreeing salaries become, or None when they agree:
    {key, field, text, choices: [{label, value}], asked}. ``asked`` says the apply agent already
    asked it once (it is never asked again for the same amounts)."""
    if st is None:
        return None
    vals = salary_values(st)
    if len(vals) < 2:
        return None
    vals = vals[-4:]

    def dated(x):
        return f" ({x['when']})" if x["when"] and x["when"] != "today" else ""
    if len(vals) == 2:
        old, new = vals
        if new["where"] == "chat":
            said = "you just said " + new["label"] if new["when"] == "today" else f"you said {new['label']}{dated(new)}"
        elif new["where"] == "extension":
            said = f"the browser extension fills {new['label']}{dated(new)}"
        else:
            said = f"you also saved {new['label']}{dated(new)}"
        text = f"Forms say {old['label']}{dated(old)}; {said}. Which one?"
    else:
        # the amounts are the choices right beside it: the line only says what to do
        text = f"You saved {len(vals)} different salaries. Which one is right?"
    sig = _sig(vals)
    return {"key": SALARY, "field": "salary", "text": text, "sig": sig,
            "choices": [{"label": x["label"], "value": str(x["amount"])} for x in reversed(vals)],
            "asked": st.meta("asked:" + SALARY) == sig}


def mark_asked(st: Store, conflict: dict) -> None:
    """The apply agent asked this question once: it fills the newest value from now on and the
    question stays on Your answers until the person settles it."""
    st.set_meta("asked:" + str(conflict.get("key") or SALARY), conflict.get("sig") or "")
    st.log("conflict_asked", key=str(conflict.get("key") or SALARY))


def settle_chat_salary(st: Store, amount: int) -> None:
    """After the person chose a salary: a chat proposal is confirmed when it is that amount, else
    it is no longer current (kept as history)."""
    from autoapply.basics import parse_salary
    for f in st.facts(store="", key=SALARY, status=(ACTIVE, NEEDS_CHECK)):
        if f["subject"]:
            continue
        if parse_salary(f["value"]) == amount:
            st.update_fact(f["id"], status=ACTIVE, check_reason="", confirmed=1, confidence=1.0)
        else:
            st.update_fact(f["id"], status=REPLACED)
    st.log("conflict_settled", key=SALARY)


# ---------------------------------------------------------------------------- the Phase 3 upgrade
def upgrade(st: Store, profile: dict | None) -> int:
    """Once per brain: the address parts of the contact facts a Phase 1 migration wrote whole
    (``address``, ``location``), from the saved profile. Returns how many facts it added."""
    ident = (profile or {}).get("identity") if isinstance((profile or {}).get("identity"), dict) else {}
    n = 0
    if ident:
        src = "resume"
        n = write_contact(st, ident, source=src, source_ref="resume_agent.db:conversation_memory")
    return n


# ---------------------------------------------------------------------------- replying to a recruiter
# What a recruiter's question may be answered from: the person's saved preferences (start date,
# notice period, relocation, remote, salary, years of experience) and the chat's preferences and
# constraints. Normal-sensitivity facts only: a legal value (work authorization, citizenship) or a
# self-identification one never goes into a model payload (applicant/redact.py), so a question
# about them is left for the person to answer (the draft lists it as unanswered).
REPLY_KEYS = (("preferred_name", "@preferred_name"), ("location", "location"),
              ("start_date", "@start_date"), ("notice_period", "@notice_period"),
              ("willing_to_relocate", "relocate"), ("open_to_remote", "remote"),
              ("desired_salary", "@salary"), ("years_experience", "@years_experience"))


def reply_facts(st: Store | None, min_conf: float = 0.8) -> dict:
    """{"answers": {name: value}, "notes": [one line each]} for drafting a reply to a recruiter's
    questions. Only facts at ``min_conf`` or more, and never a legal or self-identification one."""
    if st is None:
        return {"answers": {}, "notes": []}
    answers = {}
    for name, key in REPLY_KEYS:
        if K.sensitivity(key) != K.NORMAL:
            continue
        v = _current_value(st, key, "", min_conf)
        if v:
            answers[name] = v
    lines = []
    for n in notes(st):
        if float(n.get("confidence") or 0) < 0.6:
            continue
        val = " ".join(str(n.get("value") or "").split())[:200]
        if val:
            lines.append(val)
        if len(lines) >= 12:
            break
    # A chat note can repeat a legal or self-identification value in passing ("I am on an F-1");
    # those words are withheld the same way every model payload withholds them.
    from applicant import redact
    return redact.for_model({"answers": answers, "notes": lines}, redact.sensitive_values(st))
