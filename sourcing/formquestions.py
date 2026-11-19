"""The questions an application form asks, known BEFORE the form is opened, where a public feed
says so (docs/apply-agent.md section 31).

Auto prefers applications it can finish without a guess. For one system the questions are public:
Greenhouse's job board API (the same keyless, documented feed sourcing/ats.py and
sourcing/liveness.py already read: ``boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}``)
returns a job's application questions when asked with ``?questions=true``. Lever's and Ashby's
public postings feeds do not list a form's custom questions (Ashby's form endpoint needs the
employer's key), so those, and every other system, fall back to what earlier runs met
(ui/app.py ``form_history``). No page is scraped and no key is used.

``unknown_count`` then says how many REQUIRED questions nothing the person saved answers: not a
contact field or a file, not a self-identification question (it has its decline), not an essay
(it is drafted), not one common sense or a default settles, and not one of ``answered``.
"""
from __future__ import annotations

import re
import urllib.parse

API = "https://boards-api.greenhouse.io/v1/boards/"
_CONTACT = re.compile(r"^\W*(first|last|full|preferred|legal)? ?name\W*$|e-?mail|phone|resume|\bcv\b|cover letter|linkedin|github"
                      r"|website|portfolio|^\W*(city|state|country|address|zip|postal)", re.I)


def greenhouse_ref(url: str, source_id: str = "") -> tuple[str, str] | None:
    """(board token, job id) for a Greenhouse posting link or source id, else None."""
    try:
        p = urllib.parse.urlparse(str(url or ""))
    except ValueError:
        p = urllib.parse.urlparse("")
    host = (p.hostname or "").lower()
    segs = [s for s in (p.path or "").split("/") if s]
    token = jid = ""
    if re.match(r"^(job-)?boards(\.eu)?\.greenhouse\.io$", host) and len(segs) >= 3 and segs[1] == "jobs":
        token, jid = segs[0], segs[2]
    parts = str(source_id or "").split(":")
    if len(parts) >= 3 and parts[0] == "greenhouse":
        token, jid = token or parts[1], jid or parts[2]
    jid = jid or (urllib.parse.parse_qs(p.query or "").get("gh_jid") or [""])[0]
    if token and re.match(r"^\d+$", jid or "") and re.match(r"^[A-Za-z0-9_-]+$", token):
        return token, jid
    return None


def greenhouse_questions(token: str, job_id: str, fetch=None, timeout: int = 6) -> list[dict] | None:
    """[{label, required, type, options}] for one Greenhouse job, or None when the feed did not
    answer (the caller then knows nothing, which is not the same as "no questions")."""
    if fetch is None:
        from sourcing.ats import fetch_json
        fetch = lambda u: fetch_json(u, timeout=timeout)          # noqa: E731
    try:
        d = fetch(f"{API}{token}/jobs/{job_id}?questions=true")
    except Exception:          # noqa: BLE001 - the feed is a bonus, never a blocker
        return None
    if not isinstance(d, dict) or not isinstance(d.get("questions"), list):
        return None
    out = []
    for q in d["questions"]:
        if not isinstance(q, dict):
            continue
        label = " ".join(str(q.get("label") or "").split())[:300]
        fields = [f for f in (q.get("fields") or []) if isinstance(f, dict)]
        f0 = fields[0] if fields else {}
        opts = [" ".join(str(v.get("label") or "").split())[:120] for v in (f0.get("values") or []) if isinstance(v, dict)]
        if label:
            out.append({"label": label, "required": bool(q.get("required")), "type": str(f0.get("type") or ""),
                        "options": [o for o in opts if o][:40]})
    return out


def unknown_count(questions: list[dict], answered=(), *, company: str = "") -> int:
    """How many REQUIRED questions of a form nothing answers yet (see the module's words).
    ``answered``: the labels the person's own material already answers (the planner's verdict)."""
    from autoapply import qclass
    have = {str(a).strip().lower() for a in (answered or [])}
    n = 0
    for q in questions or []:
        label = str(q.get("label") or "")
        if not q.get("required") or not label or label.strip().lower() in have:
            continue
        opts = q.get("options") or []
        if _CONTACT.search(label) or str(q.get("type") or "").startswith("input_file"):
            continue
        if qclass.is_self_id(label, opts) or qclass.is_essay(label, opts, company=company) or qclass.is_consent(label):
            continue
        if qclass.default_kind(label) or qclass.question_class(label, opts, company=company) == qclass.COMMON:
            continue
        n += 1
    return n
