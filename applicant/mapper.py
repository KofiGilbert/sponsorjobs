"""New wordings of known questions: the label mapper (docs/applicant-brain.md, section 7.1 step 2).

A form asks "What base pay are you targeting for this position?". No rule in the code knows that
wording, so the saved salary would not be found and the person would be asked again. The mapper
works out, once per wording, which of the brain's keys a question asks for:

1. the rules first, for free: contact fields, work authorization (``workauth.classify``), the
   canonical patterns (``answers.normalize``), the self-identification classifiers, a sanctions
   screen; a wording the answer library already holds; and the ``label_map`` cache;
2. then ONE batched model call per page for every label still unknown. The call sees the form's
   labels, its options and our key names, never a value: the payload goes through
   ``applicant/redact.py for_model`` against every stored self-identification and legal value,
   with only the form's own options allowed through. The model may answer a key, ``none`` (the
   answer belongs to this one job, it is never saved) or ``ask`` (about the person, but no key
   fits: asked and saved under its own words, as today);
3. the answer is cached in ``label_map`` (label plus a hash of its options), so a wording is
   worked out once. ``how`` says who decided (``model`` or ``person``).

Code decides what is used. A wording mapped to a normal key (salary, start date) is answered
from that key. A wording mapped to a legal or self-identification key is NOT used until the
person confirms it once ("Use my saved answer"): "Are you a U.S. person?" is not "Are you a US
citizen?" until they say so. Their tap is cached too (``how = person``, ``confirmed = 1``).

Nothing here logs a label's answer; the event log keeps counts.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from applicant import keys as K
from applicant import redact
from applicant.store import Store

NONE, ASK = "none", "ask"
CONFIRM = "Use my saved answer"          # the one-tap confirm's first choice (no value in it)
MAX_LABELS = 20                          # labels in one call
MAX_LABEL_CHARS = 200
MAX_OPTIONS = 20
MAX_OPTION_CHARS = 60
TIMEOUT_S = 20

# What a key asks, for the model (our words, not the person's) and for the confirm line.
KEYS: dict[str, str] = {
    "@salary": "desired salary or pay for the job",
    "@start_date": "when the person can start",
    "@notice_period": "notice period at the current job",
    "@heard_about": "how the person heard about the job",
    "@years_experience": "total years of professional experience",
    "@over_18": "whether the person is at least 18 years old",
    "@previously_worked": "whether the person has worked for this company before",
    "@citizenship": "the person's country of citizenship",
    "@citizenship2": "a second country of citizenship",
    "@preferred_name": "the name the person prefers to be called",
    "@linkedin": "LinkedIn profile URL",
    "@portfolio": "personal website or portfolio URL",
    "workauth:authorized": "authorized to work in the job's country",
    "workauth:sponsor_now": "needs visa sponsorship now",
    "workauth:sponsor_future": "will need visa sponsorship in the future",
    "workauth:sponsor_any": "needs visa sponsorship now or in the future",
    "workauth:citizen": "a citizen of the job's country",
    "workauth:citizen_or_pr": "a citizen or permanent resident of the job's country",
    "workauth:pr": "a permanent resident of the job's country",
    "workauth:status": "current immigration or work status",
    "workauth:auth_end": "when the work authorization ends",
    "workauth:relocate": "willing to relocate",
    "@gender": "gender (voluntary self-identification)",
    "@sex": "sex (voluntary self-identification)",
    "@pronouns": "pronouns",
    "@hispanic_latino": "Hispanic or Latino (voluntary self-identification)",
    "@race": "race (voluntary self-identification)",
    "@ethnicity": "ethnicity (voluntary self-identification)",
    "@veteran": "protected veteran status (voluntary self-identification)",
    "@disability": "disability status (voluntary self-identification)",
}

# The confirm line names the question the person answered before, in plain words.
ASKED_AS = {
    "workauth:authorized": "Are you authorized to work here?",
    "workauth:sponsor_now": "Do you need visa sponsorship now?",
    "workauth:sponsor_future": "Will you need visa sponsorship in the future?",
    "workauth:sponsor_any": "Will you need visa sponsorship?",
    "workauth:citizen": "Are you a citizen of the job's country?",
    "workauth:citizen_or_pr": "Are you a citizen or permanent resident?",
    "workauth:pr": "Are you a permanent resident?",
    "workauth:status": "What is your work status?",
    "workauth:auth_end": "When does your work authorization end?",
    "workauth:relocate": "Are you willing to relocate?",
}


def sensitivity(key: str) -> str:
    """The sensitivity of a mapped key: work authorization is legal; the rest from the registry."""
    if str(key or "").startswith("workauth:"):
        return K.LEGAL
    if key in (NONE, ASK, ""):
        return K.NORMAL
    return K.sensitivity(key)


def asked_as(key: str) -> str:
    """How the confirm line names the question the person answered before."""
    if key in ASKED_AS:
        return ASKED_AS[key]
    k = K.get(key)
    return (k.ask if k and k.ask else KEYS.get(key, key)).rstrip("?")


def clean(label: str) -> str:
    from autoapply.answers import clean_label
    s = " ".join(str(label or "").split())
    return (clean_label(s) or s)[:MAX_LABEL_CHARS]


def cache_label(label: str) -> str:
    """The cache's form of a label: cleaned and lowercased, as the migration seeded it."""
    return " ".join(clean(label).lower().split())


def options_hash(options) -> str:
    opts = sorted({" ".join(str(o or "").split()).lower() for o in (options or []) if str(o or "").strip()})
    return hashlib.sha1("\n".join(opts).encode()).hexdigest()[:16] if opts else ""


# ---------------------------------------------------------------------------- the rules first
# Not a question about the person: the portal's own account and consent furniture (the terms
# flow decides agreements; the account fields are the portal's).
_FURNITURE = re.compile(r"password|passcode|verification code|one[- ]time|captcha|\bsearch\b|user ?name"
                        r"|\bagree|\bterms\b|privacy|consent|acknowledg|signature|\bsign (in|up|here)\b", re.I)


def rule_known(label: str, company: str = "") -> bool:
    """True when the code's own rules already recognize this question (they decide it; the
    mapper never sees it), or it is not a question about the person at all."""
    from autoapply import answers as A
    from autoapply import countries, workauth
    from autoapply.asks import is_eeo
    from autoapply.planner import _LETTER, _RESUME, contact_key
    lab = clean(label)
    if not lab or len(lab) < 3:
        return True
    key = A.normalize(lab)
    if not key or key.startswith("@"):
        return True
    if workauth.classify(lab) or contact_key(lab) or is_eeo(lab):
        return True
    if _LETTER.search(lab) or _RESUME.search(lab) or A.is_per_job(lab, company) or _FURNITURE.search(lab):
        return True
    if countries.named_in(lab) and re.search(r"citizen|national", lab, re.I):
        return True
    return False


# ---------------------------------------------------------------------------- the cache
def cached(st: Store, label: str, options=None):
    """The cached mapping of a wording: for these options when given, else the newest for the
    label (a field read without its choices still finds the mapping its select had)."""
    lab = cache_label(label)
    # (Phase 1 seeded "rule" rows: a wording the answer library already keys. Not the mapper's.)
    if options is not None:
        r = st.q("SELECT * FROM label_map WHERE label=? AND options_hash=? AND how<>'rule'",
                 (lab, options_hash(options)))
        if r:
            return r[0]
    r = st.q("SELECT * FROM label_map WHERE label=? AND how<>'rule' ORDER BY updated_at DESC LIMIT 1", (lab,))
    return r[0] if r else None


def remember(st: Store, label: str, options, key: str, *, how: str = "model", confirmed: int = 0) -> None:
    now = time.time()
    with st.tx() as c:
        c.execute("INSERT INTO label_map (label, options_hash, key, how, confirmed, created_at, updated_at)"
                  " VALUES (?,?,?,?,?,?,?) ON CONFLICT(label, options_hash) DO UPDATE SET key=excluded.key,"
                  " how=excluded.how, confirmed=excluded.confirmed, updated_at=excluded.updated_at",
                  (cache_label(label), options_hash(options), key, how, int(confirmed), now, now))


def confirm(st: Store, label: str, key: str) -> None:
    """The person said this wording asks the same as ``key``: used from now on, every row of it."""
    lab, now = cache_label(label), time.time()
    with st.tx() as c:
        n = c.execute("UPDATE label_map SET how='person', confirmed=1, updated_at=? WHERE label=? AND key=?"
                      " AND how<>'rule'", (now, lab, key)).rowcount
        if not n:
            c.execute("INSERT OR REPLACE INTO label_map (label, options_hash, key, how, confirmed, created_at,"
                      " updated_at) VALUES (?,?,?,?,?,?,?)", (lab, "", key, "person", 1, now, now))
    st.log("label_confirmed", key=key if sensitivity(key) != K.EEO else "eeo")


def usable(row) -> bool:
    """A mapping the agent answers from without asking: a real key, normal, or confirmed."""
    if row is None or row["key"] not in KEYS:
        return False
    return sensitivity(row["key"]) == K.NORMAL or bool(row["confirmed"])


def needs_confirm(row) -> bool:
    return row is not None and row["key"] in KEYS and not usable(row)


# ---------------------------------------------------------------------------- the model call
SYSTEM = (
    "You match job application form questions to the keys of a person's saved answers. Return ONLY JSON:\n"
    '{"map": [{"n": <question number>, "key": "<one key from the list, or none, or ask>"}]}\n'
    "Rules:\n"
    "1. Give a key only when the question asks exactly what the key describes, in other words. "
    "\"What base pay are you targeting?\" is @salary.\n"
    "2. none: the answer belongs to this one job or company (why this role, a job specific essay, "
    "an agreement to this employer's terms).\n"
    "3. ask: a question about the person that no key fits.\n"
    "4. When unsure between a key and ask, answer ask. Never guess a legal or self-identification key.\n"
    "5. One entry per question number."
)


def build_payload(items: list[dict]) -> dict:
    """The model's input: numbered labels with their options, and the key list. No values."""
    qs = []
    for n, it in enumerate(items, start=1):
        q = {"n": n, "question": clean(it.get("label"))}
        opts = [" ".join(str(o).split())[:MAX_OPTION_CHARS] for o in (it.get("options") or []) if str(o).strip()]
        if opts:
            q["options"] = opts[:MAX_OPTIONS]
        qs.append(q)
    return {"questions": qs, "keys": [{"key": k, "means": v} for k, v in KEYS.items()]}


def guarded(st: Store, payload: dict) -> dict:
    """The payload with every stored EEO and legal value withheld, the form's options kept."""
    allowed = [o for q in payload.get("questions") or [] for o in (q.get("options") or [])]
    allowed += [k["means"] for k in payload.get("keys") or []] + [k["key"] for k in payload.get("keys") or []]
    return redact.for_model(payload, redact.sensitive_values(st), allowed)


def user_text(payload: dict) -> str:
    return ("<questions>\n" + json.dumps(payload["questions"], ensure_ascii=False) + "\n</questions>\n"
            "<keys>\n" + json.dumps(payload["keys"], ensure_ascii=False) + "\n</keys>")


def parse(raw: str, n_items: int) -> dict[int, str]:
    """{question number: key} from the model's reply, validated: a key not on our list is ask."""
    text = str(raw or "").strip()
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        return {}
    try:
        obj = json.loads(text[s:e + 1])
    except ValueError:
        return {}
    out: dict[int, str] = {}
    maps = obj.get("map") if isinstance(obj, dict) else None
    for x in maps if isinstance(maps, list) else []:
        if not isinstance(x, dict):
            continue
        try:
            n = int(x.get("n"))
        except (TypeError, ValueError):
            continue
        k = str(x.get("key") or "").strip()
        if 1 <= n <= n_items and n not in out:
            out[n] = k if (k in KEYS or k in (NONE, ASK)) else ASK
    return out


def _call(llm, system: str, user: str, timeout: float) -> str:
    """The model call, bounded: a stalled call never holds the form."""
    import threading
    box: dict = {}

    def run():
        try:
            box["raw"] = llm.map_labels(system, user)
        except Exception as exc:       # noqa: BLE001 - no mapping then; asked as before
            box["err"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return "" if t.is_alive() or "err" in box else str(box.get("raw") or "")


def map_page(st: Store, llm, items: list[dict], *, library=None, company: str = "",
             timeout: float = TIMEOUT_S) -> dict:
    """{label: label_map row} for the page's labels the rules do not know, cached or mapped now
    with at most ONE model call. ``items`` = [{label, options}]. A label the library already
    holds under its own wording is skipped (its answer is found without a mapping). ``llm`` is a
    model, or a function that makes one (called only when a wording is new)."""
    out: dict = {}
    todo: list[dict] = []
    seen: set = set()
    for it in items or []:
        label = str((it or {}).get("label") or "")
        lab = clean(label)
        if not lab or lab in seen or rule_known(lab, company):
            continue
        seen.add(lab)
        opts = list((it or {}).get("options") or [])
        row = cached(st, lab, opts if opts else None)
        if row is not None:
            out[label] = row
            continue
        try:
            if library is not None and library.lookup(lab):
                continue
        except Exception:              # noqa: BLE001
            pass
        todo.append({"label": label, "options": opts})
    if todo and llm is not None and not hasattr(llm, "map_labels") and callable(llm):
        try:
            llm = llm()                # made only when a wording needs it (a factory)
        except Exception:              # noqa: BLE001 - no model: only the cache answers
            llm = None
    if not todo or llm is None or not hasattr(llm, "map_labels"):
        return out
    todo = todo[:MAX_LABELS]
    payload = guarded(st, build_payload(todo))
    raw = _call(llm, SYSTEM, user_text(payload), timeout)
    st.set_meta("mapper_calls", str(int(st.meta("mapper_calls") or 0) + 1))
    got = parse(raw, len(todo))
    if not got:
        st.log("labels_unmapped", n=len(todo))
        return out                     # nothing cached: tried again on the next page
    for n, it in enumerate(todo, start=1):
        k = got.get(n)
        if not k:
            continue
        remember(st, it["label"], it["options"], k, how="model", confirmed=0)
        out[it["label"]] = cached(st, it["label"], it["options"])
    st.log("labels_mapped", n=len(got), asked=len(todo))
    return out


def counts(st: Store) -> dict:
    """Counts only: calls made and cached wordings by who decided."""
    by = {r["how"]: r["n"] for r in st.q("SELECT how, COUNT(*) n FROM label_map GROUP BY how")}
    return {"calls": int(st.meta("mapper_calls") or 0), "by_how": by}
