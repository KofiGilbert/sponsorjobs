"""The SponsorJobs assistant in the person's Telegram chat (free text that is not a command).

It is an assistant for the app, not the resume intake: it gets a short, LOCAL summary of what
the app already knows about the person (name, latest roles, schools, skills, the resumes built,
which form answers are saved, the work authorization status) and answers briefly from it.

Two promises it keeps, both enforced here in code and not only asked of the model:

* It never claims it saved, remembered, uploaded or changed anything. This chat saves nothing;
  only code that really saved something may say so (the late answers path in ui/app.py). A
  sentence in the model's reply that claims otherwise is dropped (``honest``), and the reply then
  says where those details go in the app.
* Self-identification (EEO) values never reach the model or the broker: the summary says only
  "saved" or "not saved" for those questions, chat history that carries such values is left out
  (``scrub_history``), and a message that carries them is not sent to the model at all
  (``carries_eeo``).

Pure: no I/O and no model here. ui/app.py gathers the data and makes the one call.
"""

from __future__ import annotations

import re

from autoapply import eeo as EEO
from autoapply.answers import normalize

WHERE_ANSWERS = "Auto-apply, Your answers"
WHERE_RESUME = "Resumes"
# What the reply says in place of a claim the chat cannot keep.
POINTER = ("I can't keep details from this chat. Form answers go in SponsorJobs under "
           f"{WHERE_ANSWERS}, and resume details go under {WHERE_RESUME}.")
EEO_POINTER = ("I can't keep form answers from this chat. To use these on applications, open "
               f"SponsorJobs and go to {WHERE_ANSWERS}.")
FALLBACK = ("I can help with your applications here. Send /list for what is waiting, or /help "
            "for the commands.")

SYSTEM = (
    "You are the SponsorJobs assistant in the person's Telegram chat. SponsorJobs is a desktop "
    "app on their computer: it finds jobs at companies with a visa sponsorship record, tailors "
    "their resume to each job, drafts cover letters, and applies or helps them apply.\n"
    "Answer in at most three short sentences, plainly, using WHAT THE APP KNOWS below.\n"
    "Rules:\n"
    "1. This chat cannot save, remember, upload, change or send anything. Never say or suggest "
    "that you did or will (no 'noted', 'saved', 'I will remember', 'on file', 'updated'). To say "
    "what the app already has, write 'SponsorJobs has your ...' or 'SponsorJobs does not have ...'.\n"
    "2. When the person gives resume details (roles, schools, skills, dates), tell them to add "
    f"them in SponsorJobs under {WHERE_RESUME}. When they give form answers (salary, start date, "
    f"work authorization, demographic questions), tell them to add them under {WHERE_ANSWERS}.\n"
    "3. Never start a resume interview and never ask for their background: the app already has "
    "what is listed below. If something is missing there, say where in the app to add it.\n"
    "4. Never invent facts about the person, their applications or the app. If you do not know, "
    "say so and point to the app.\n"
    "5. You cannot open files or pictures sent in this chat. To add a resume they open "
    f"SponsorJobs and go to {WHERE_RESUME}, then Upload.\n"
    "6. Commands they can send: /list, /approve <id>, /status <id>, /stop, /resume, /skip, /help.\n"
    "7. Use plain words and commas. No em or en dashes. Say 'resume', never 'CV'."
)


# --------------------------------------------------------------------------- the summary
def _s(v) -> str:
    return " ".join(str(v or "").split())


def _roles(profile: dict) -> list[str]:
    out = []
    for e in profile.get("experience") or []:
        if not isinstance(e, dict):
            continue
        org = _s(e.get("org") or e.get("company") or e.get("organization"))
        roles = e.get("roles") if isinstance(e.get("roles"), list) else [e]
        for r in roles:
            if not isinstance(r, dict):
                continue
            title = _s(r.get("title") or r.get("role"))
            dates = _s(r.get("dates") or r.get("date"))
            if title or org:
                line = " at ".join(x for x in (title, org) if x)
                out.append(line + (f" ({dates})" if dates else ""))
    return out


def _schools(profile: dict) -> list[str]:
    out = []
    for e in profile.get("education") or []:
        if not isinstance(e, dict):
            continue
        school = _s(e.get("school") or e.get("institution") or e.get("name"))
        degree = _s(e.get("degree"))
        if school or degree:
            out.append(school + (f" ({degree})" if degree and school else degree if not school else ""))
    return out


def _skills(profile: dict) -> list[str]:
    raw = profile.get("skills")
    items: list[str] = []
    if isinstance(raw, dict):
        for v in raw.values():
            items += [x for x in re.split(r"[,;\n]", str(v or ""))]
    elif isinstance(raw, list):
        for v in raw:
            items += re.split(r"[,;\n]", str(v.get("items") if isinstance(v, dict) else v or ""))
    elif isinstance(raw, str):
        items = re.split(r"[,;\n]", raw)
    out: list[str] = []
    for x in items:
        x = _s(x)
        if x and x.lower() not in {o.lower() for o in out}:
            out.append(x)
    return out


def _work_auth_line(work_auth: dict, labels: dict) -> str:
    parts = []
    for cc, ent in ((work_auth or {}).get("countries") or {}).items():
        if not isinstance(ent, dict):
            continue
        bits = []
        status = labels.get((cc, str(ent.get("status") or "")), "")
        if status:
            bits.append(status)
        for key, name in (("authorized", "authorized to work"), ("sponsor_now", "needs sponsorship now"),
                          ("sponsor_future", "needs sponsorship later")):
            if ent.get(key):
                bits.append(f"{name}: {ent[key]}")
        if ent.get("auth_end"):
            bits.append(f"permit ends {ent['auth_end']}")
        if bits:
            parts.append(f"{cc}: " + ", ".join(bits))
    if (work_auth or {}).get("relocate"):
        parts.append(f"open to relocating: {work_auth['relocate']}")
    return "; ".join(parts)


def is_eeo_row(row: dict) -> bool:
    """A saved answer to a self-identification question (by its key, or by its wording)."""
    from autoapply.asks import is_eeo
    key = str((row or {}).get("key") or "")
    return key in EEO.KEYS or is_eeo((row or {}).get("question") or "")


def summary(profile: dict | None, resumes: list[str] | None = None, answers: list[dict] | None = None,
            work_auth: dict | None = None, status_labels: dict | None = None) -> str:
    """What the app knows, in a few short lines. Answer VALUES are never included: saved form
    answers are listed by question, and the self-identification ones only as saved or not."""
    p = profile or {}
    lines = []
    name = _s((p.get("identity") or {}).get("name"))
    lines.append(f"Name: {name or 'not saved'}")
    roles = _roles(p)
    lines.append("Latest roles: " + ("; ".join(roles[:4]) if roles else "none saved"))
    schools = _schools(p)
    lines.append("Schools: " + ("; ".join(schools[:3]) if schools else "none saved"))
    skills = _skills(p)
    lines.append("Skills: " + (", ".join(skills[:25]) if skills else "none saved"))
    rs = [_s(r) for r in (resumes or []) if _s(r)]
    lines.append("Resumes built: " + ("; ".join(rs[:8]) if rs else "none yet")
                 + (f" (and {len(rs) - 8} more)" if len(rs) > 8 else ""))
    rows = [r for r in (answers or []) if isinstance(r, dict)]
    plain = []
    for r in rows:
        if is_eeo_row(r):
            continue
        q = _s(r.get("question"))[:70]
        if q and q not in plain:
            plain.append(q)
    lines.append("Saved form answers: " + (", ".join(plain[:20]) if plain else "none")
                 + (f" (and {len(plain) - 20} more)" if len(plain) > 20 else ""))
    have = set()
    for r in rows:
        if is_eeo_row(r):
            k = str(r.get("key") or "")
            have.add(k if k in EEO.KEYS else normalize(r.get("question") or ""))
    eeo_bits = [f"{EEO.QUESTIONS[k]}: {'saved' if k in have else 'not saved'}"
                for k in ("@gender", "@race", "@ethnicity", "@veteran", "@disability")]
    lines.append("Self-identification answers (values are private): " + ", ".join(eeo_bits))
    wa = _work_auth_line(work_auth or {}, status_labels or {})
    lines.append("Work authorization: " + (wa or "not set"))
    return "\n".join(lines)


# --------------------------------------------------------------------------- EEO privacy
_SPLIT = re.compile(r"[\n,;/]|\b\d{1,2}\s*[.):]|\.\s+")


def carries_eeo(text: str) -> bool:
    """Does this text state a self-identification answer ("Male", "Black or African American",
    "No disability", "Not a veteran")? Such a message never goes to the model."""
    t = str(text or "")
    if not t.strip():
        return False
    for seg in _SPLIT.split(t):
        seg = seg.strip(" .:-")
        if not seg or len(seg) > 80:
            continue
        if (EEO.gender_class(seg) not in (None, "decline") or EEO.race_class(seg)
                or (re.search(r"disab", seg, re.I)
                    and EEO.value_class("@disability", seg) not in (None, "decline"))
                or (re.search(r"veteran", seg, re.I)
                    and EEO.value_class("@veteran", seg) not in (None, "decline"))):
            return True
    return False


def scrub_history(history: list[dict] | None, limit: int = 6) -> list[dict]:
    """The last turns of the chat, without any turn that states a self-identification answer."""
    out = [t for t in (history or []) if isinstance(t, dict) and not carries_eeo(t.get("content"))]
    return out[-limit:]


def build_user(summary_text: str, history: list[dict] | None, text: str) -> str:
    convo = "\n".join(f"[{t.get('role')}] {_s(t.get('content'))[:400]}" for t in (history or []))
    return (f"WHAT THE APP KNOWS:\n{summary_text}\n\n"
            f"RECENT CHAT:\n{convo or '(none)'}\n\n"
            f"THE PERSON SAYS:\n{str(text or '')[:1500]}")


# --------------------------------------------------------------------------- honesty
_NEG = re.compile(r"\b(can['’]?t|cannot|can not|don['’]?t|do not|won['’]?t|will not|unable|not able|"
                  r"isn['’]?t|aren['’]?t|wasn['’]?t|weren['’]?t|haven['’]?t|have not|hasn['’]?t|nothing|"
                  r"never|no way|not)\b", re.I)
_PAST = (r"saved|noted|remembered|stored|recorded|logged|uploaded|updated|changed|added|filed|kept|"
         r"captured|registered|written down|jotted")
_BASE = r"save|note|remember|store|record|log|upload|update|change|add|keep|file|capture|register|jot"
_ING = r"saving|noting|remembering|storing|recording|logging|uploading|updating|changing|adding|keeping|filing"
_GAP = r"(?:(?!(?:help|show|tell|let|walk|guide|explain|can|could|would|need)\b)\w+\s+){0,2}?"
_CLAIM = re.compile(
    rf"\b(?:i['’]?ve|i have|we['’]?ve|we have|i|we)\s+(?:just\s+|already\s+|also\s+|now\s+)?(?:{_PAST})\b"
    rf"|\b(?:i['’]?ll|i will|i['’]?m going to|i am going to|we['’]?ll|we will|let me)\s+{_GAP}(?:{_BASE})\b"
    rf"|\b(?:i['’]?m|i am|we['’]?re|we are)\s+(?:now\s+|also\s+|just\s+)?(?:{_ING})\b"
    r"|\bon (?:file|record)\b|\b(?:been|is|are|now|all|got)\s+(?:saved|noted|stored|recorded|added|updated|uploaded)\b"
    r"|\bgot (?:it|them|that|those|this|these) (?:down|noted|saved)\b|\bnoted\b|\bsaved\b",
    re.I)
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def claims_a_save(text: str) -> bool:
    """A sentence that says or suggests the chat saved, remembered or changed something."""
    return any(_CLAIM.search(s) and not _NEG.search(s) for s in _SENT.split(str(text or "")) if s.strip())


def honest(reply: str) -> str:
    """The model's reply without any sentence that claims a save the chat never made; when one
    was dropped, the reply says where those details really go. Never empty."""
    kept, dropped = [], False
    for s in _SENT.split(str(reply or "").strip()):
        if not s.strip():
            continue
        if _CLAIM.search(s) and not _NEG.search(s):
            dropped = True
            continue
        kept.append(s.strip())
    out = " ".join(kept).strip()
    if dropped:
        out = (out + " " + POINTER).strip()
    return out or FALLBACK
