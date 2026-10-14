"""Submission drivers for AUTO-lane sites (CLAUDE.md §7).

A driver performs the real, unattended submission for a verified auto-eligible site, using
the person's OWN reviewed material, identifying honestly, and respecting the site's rate
limits. Drivers exist ONLY for sites in ``submit.allowlist`` with ``auto_eligible`` True.

Hard safety rule: a driver NEVER evades bot detection. If it meets a captcha, a login
wall, or any anti-bot challenge, it aborts and signals ``needs_assist`` so the caller
drops the application to ASSISTED — the challenge is the site telling us it doesn't want a
bot here. The HTTP callable is injected so drivers are fully testable offline.

A driver returns ``{"ok": bool, "detail": str, "needs_assist": bool, "posted": bool,
"reason": str}`` where ``reason`` is a submit.stages.REASONS key on failure.
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

from submit.filenames import export_filename


def _post_json(url: str, payload: dict) -> tuple[int, str]:  # pragma: no cover - network
    """POST JSON, return (status, body). Kept for callers that send plain JSON."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    return _send(req)


def _send(req) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return int(getattr(resp, "status", 0) or resp.getcode()), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read().decode("utf-8", "replace")


def encode_multipart(fields: list, files: list) -> tuple[bytes, str]:
    """multipart/form-data body for ``fields`` [(name, value)] and ``files``
    [(name, filename, bytes, content_type)]. Returns (body, content_type header)."""
    boundary = "----sponsorjobs" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                ).encode("utf-8")
        out += str(value).encode("utf-8") + b"\r\n"
    for name, filename, blob, ctype in files:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode("utf-8")
        out += bytes(blob) + b"\r\n"
    out += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _post_multipart(url: str, form: dict) -> tuple[int, str]:  # pragma: no cover - network
    """Default transport: POST ``form`` ({"fields": [...], "files": [...]}) as multipart."""
    body, ctype = encode_multipart(form.get("fields") or [], form.get("files") or [])
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
    return _send(req)


def _get_json(url: str) -> tuple[int, str]:  # pragma: no cover - network
    """Default offer lookup: GET the public offer JSON, return (status, body)."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    return _send(req)


def _applicant_fields(record_data: dict) -> dict:
    """Pull the person's honest contact identity + materials from the record/profile."""
    ident = (record_data.get("profile") or {}).get("identity") or record_data.get("identity") or {}
    return {
        "name": ident.get("name") or "",
        "email": ident.get("email") or "",
        "phone": ident.get("phone") or "",
        "cover_letter": record_data.get("cover_letter") or "",
        "screening": [qa for qa in (record_data.get("screening") or []) if isinstance(qa, dict)],
    }


def _norm(text: str) -> str:
    """Question text for matching: no HTML, no punctuation, lower case, single spaces."""
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(text or "")))
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _yes_no(answer: str) -> bool | None:
    a = _norm(answer)
    if re.match(r"^(yes|y|true|i do|i am|i can|i have|i will)\b", a):
        return True
    if re.match(r"^(no|n|false|i do not|i don t|i am not|i cannot|i can t|i have not)\b", a):
        return False
    return None


def _answer_for(question: dict, answer: str) -> dict | None:
    """One ``open_question_answers_attributes`` item for a Recruitee question, or None when the
    drafted answer can't be expressed in that question's kind (we never guess a choice)."""
    kind = str(question.get("kind") or "")
    qid = question.get("id")
    answer = (answer or "").strip()
    if qid is None or not answer:
        return None
    if kind in ("string", "text"):
        return {"open_question_id": qid, "content": answer}
    if kind in ("boolean", "legal"):
        flag = _yes_no(answer)
        return None if flag is None else {"open_question_id": qid, "flag": flag}
    opts = [str(o.get("body") or "").strip() for o in (question.get("open_question_options") or [])]
    if kind == "single_choice":
        a = _norm(answer)
        hit = [o for o in opts if _norm(o) == a] or [o for o in opts if _norm(o) and a.startswith(_norm(o))]
        return {"open_question_id": qid, "content": hit[0]} if hit else None
    if kind == "multi_choice":
        a = f" {_norm(answer)} "
        hits = [o for o in opts if _norm(o) and f" {_norm(o)} " in a]
        return {"open_question_id": qid, "multi_content": hits} if hits else None
    if kind == "number" and re.fullmatch(r"-?\d+(\.\d+)?", answer):
        return {"open_question_id": qid, "content": answer}
    if kind == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", answer):
        return {"open_question_id": qid, "content": answer}
    return None        # salary, file, video, infobox, or an answer in the wrong shape


def map_open_questions(questions: list, screening: list) -> tuple[list, list]:
    """Pair the posting's open questions with the drafted screening answers, by question text.

    Returns (answers, unanswered_required): ``answers`` are ready-to-send attribute items;
    ``unanswered_required`` the bodies of required questions we have no usable answer for."""
    drafted = {_norm(qa.get("question")): str(qa.get("answer") or "") for qa in screening}
    answers, missing = [], []
    for q in questions or []:
        if not isinstance(q, dict) or q.get("kind") == "infobox":
            continue
        item = _answer_for(q, drafted.get(_norm(q.get("body")), ""))
        if item:
            answers.append(item)
        elif q.get("required"):
            missing.append(re.sub(r"<[^>]+>", " ", str(q.get("body") or "")).strip())
    return answers, missing


def _offer_questions(api_base: str, slug: str, get) -> tuple[str, list]:
    """The posting's open questions from its public offer JSON.

    Returns ("", questions) on success, or (reason, []) where reason is a stages.REASONS key:
    "posting_closed" when the offer is gone, "site_error" when it can't be read."""
    try:
        status, body = get(f"{api_base}/offers/{slug}")
    except Exception:
        return "site_error", []
    if status in (404, 410):
        return "posting_closed", []
    if not 200 <= status < 300:
        return "site_error", []
    try:
        doc = json.loads(body or "{}")
    except ValueError:
        return "site_error", []
    offer = doc.get("offer") if isinstance(doc, dict) and isinstance(doc.get("offer"), dict) else doc
    if isinstance(doc, dict) and isinstance(doc.get("offers"), list):   # the list form
        offer = next((o for o in doc["offers"] if str(o.get("slug")) == slug), None)
        if offer is None:
            return "posting_closed", []
    if not isinstance(offer, dict):
        return "site_error", []
    if str(offer.get("status") or "published") not in ("published", ""):
        return "posting_closed", []
    return "", [q for q in (offer.get("open_questions") or []) if isinstance(q, dict)]


def _fail(reason: str, detail: str, posted: bool) -> dict:
    return {"ok": False, "needs_assist": reason != "posting_closed", "posted": posted,
            "reason": reason, "detail": detail}


def recruitee_submit(url: str, record_data: dict, http=_post_multipart, get=_get_json) -> dict:
    """Submit an application to a Recruitee offer via its keyless Careers Site API.

    Endpoint: ``POST https://{company}.recruitee.com/api/offers/{offer_slug}/candidates``
    (verified keyless, candidate-facing, see submit.allowlist), sent as multipart/form-data
    so the tailored resume PDF is attached as ``candidate[cv]``. Screening answers go as
    ``candidate[open_question_answers_attributes][i][open_question_id|content|flag|
    multi_content]``, matched to the question ids in the offer's public JSON.

    Aborts to assisted (with a stages.REASONS reason) rather than guessing: missing identity
    or resume, a required question we can't answer, an auth/captcha wall, or any error.
    """
    # ``posted`` tells the caller whether a real request hit the site's submit API, it advances
    # the min-gap spacing clock even on failure, so a run of failing items can't hammer the API.
    f = _applicant_fields(record_data)
    if not (f["name"] and f["email"]):
        return _fail("form_blocker", "Your name and email aren't in your profile yet.", False)

    api = _to_candidates_endpoint(url)
    if not api:
        return _fail("form_blocker", "Couldn't find this posting's application form.", False)

    cv_path = str(record_data.get("cv_pdf_path") or "")
    try:
        cv = Path(cv_path).read_bytes() if cv_path else b""
    except OSError:
        cv = b""
    if not cv:
        return _fail("form_blocker", "The tailored resume PDF is missing, so nothing was sent.", False)

    api_base, slug = api.rsplit("/offers/", 1)[0], api.rsplit("/", 2)[-2]
    reason, questions = _offer_questions(api_base, slug, get)
    if reason:
        return _fail(reason, "The posting is closed." if reason == "posting_closed"
                     else "Couldn't read the posting's questions, so nothing was sent.", False)
    answers, missing = map_open_questions(questions, f["screening"])
    if missing:
        return _fail("form_blocker", "The form has a required question without an answer: "
                     + missing[0][:120], False)

    fields = [("candidate[name]", f["name"]), ("candidate[email]", f["email"])]
    if f["phone"]:
        fields.append(("candidate[phone]", f["phone"]))
    if f["cover_letter"]:
        fields.append(("candidate[cover_letter]", f["cover_letter"]))
    for i, a in enumerate(answers):
        key = f"candidate[open_question_answers_attributes][{i}]"
        fields.append((f"{key}[open_question_id]", a["open_question_id"]))
        if "content" in a:
            fields.append((f"{key}[content]", a["content"]))
        if "flag" in a:
            fields.append((f"{key}[flag]", "true" if a["flag"] else "false"))
        for v in a.get("multi_content") or []:
            fields.append((f"{key}[multi_content][]", v))
    filename = export_filename(f["name"], "resume")   # "<Name> Resume.pdf", not cv-<id>.pdf
    form = {"fields": fields, "files": [("candidate[cv]", filename, cv, "application/pdf")]}

    try:
        status, body = http(api, form)
    except Exception:
        # A connection was attempted (the request left the machine), so space the next one.
        return _fail("site_error", "Couldn't reach Recruitee, so it wasn't sent.", True)

    if status in (401, 403) or _looks_like_challenge(body):
        # An auth wall / captcha means the site doesn't want a bot here, never evade.
        return _fail("needs_signin", "Recruitee asked for a human check, so it wasn't sent.", True)
    if 200 <= status < 300:
        return {"ok": True, "needs_assist": False, "posted": True, "reason": "",
                "detail": "Submitted to Recruitee with your resume."}
    if status in (404, 410):
        return _fail("posting_closed", "The posting is closed.", True)
    if status == 422:
        return _fail("form_blocker", "Recruitee said the form needs more: " + _first_error(body), True)
    return _fail("site_error", f"Recruitee answered with an error (HTTP {status}), so it wasn't sent.", True)


def _first_error(body: str) -> str:
    try:
        errs = (json.loads(body or "{}") or {}).get("error") or []
    except (ValueError, AttributeError):
        errs = []
    return str(errs[0])[:120] if errs else "check it on the site."


def _to_candidates_endpoint(url: str) -> str:
    """Turn a Recruitee offer/careers URL into its Careers Site candidates endpoint.

    Recruitee careers URLs look like ``https://{company}.recruitee.com/o/{offer_slug}``;
    the sanctioned submit endpoint is ``/api/offers/{offer_slug}/candidates`` on the same host.
    """
    try:
        p = urlparse(url if "//" in (url or "") else "//" + (url or ""))
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    # Dot-boundary match (same rule as policy._matches / driver_for) so a lookalike like
    # "evilrecruitee.com" can never derive an endpoint — defense-in-depth, since policy
    # already gates this driver to genuine recruitee hosts before it's ever reached.
    if not (host == "recruitee.com" or host.endswith(".recruitee.com")):
        return ""
    # Offer slug is the last non-empty path segment (…/o/{slug} or …/{slug}).
    segs = [s for s in (p.path or "").split("/") if s and s not in ("o", "offers")]
    if not segs:
        return ""
    return f"https://{host}/api/offers/{segs[-1]}/candidates"


def _looks_like_challenge(body: str) -> bool:
    low = (body or "").lower()
    return any(w in low for w in ("captcha", "recaptcha", "hcaptcha", "are you human",
                                  "verify you are", "challenge", "cloudflare"))


# Per-host driver registry — a host gets a driver ONLY if it's auto-eligible in the
# allowlist. This dict never grows at runtime by inference; adding one is a code change.
_DRIVERS = {
    "recruitee.com": recruitee_submit,
}


def driver_for(host: str):
    """The submission driver for a host (exact or sub-domain), or None if none is wired."""
    host = (host or "").lower()
    for suffix, drv in _DRIVERS.items():
        if host == suffix or host.endswith("." + suffix):
            return drv
    return None
