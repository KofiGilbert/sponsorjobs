"""One small Tavus CVI client, shared by the broker (company key, server-side) and the engine
(the PERSON'S OWN key, local). Both paths need the same four calls, so the request shapes live
here once and are unit-tested with a fake transport (no key, no network).

Endpoints (docs.tavus.io, read 2026-10-02):
  POST https://tavusapi.com/v2/conversations               mint a conversation (join URL)
  POST https://tavusapi.com/v2/conversations/{id}/end      end it
  GET  https://tavusapi.com/v2/conversations/{id}?verbose=true   events incl. the transcript
  POST https://tavusapi.com/v2/pals                        create the interviewer "PAL"
Auth is the ``x-api-key`` header on every call. Tavus renamed persona -> PAL and replica -> face
in 2026; the body fields are ``pal_id`` / ``face_id``. The transcript arrives as the
``application.transcription_ready`` event: ``properties.transcript`` = [{role: assistant|user,
content, ...}].
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

TAVUS_API = "https://tavusapi.com/v2"

# A Tavus stock face ('Luna'); any account, including the free tier, can use the stock set.
DEFAULT_FACE_ID = "r9d30b0e55ac"


class TavusError(RuntimeError):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"Tavus {status}: {message}" if message else f"Tavus {status}")
        self.status = status


def _urllib_http(method: str, url: str, headers: dict, body: dict | None) -> tuple[int, dict]:
    """Default transport: (method, url, headers, body) -> (status, json). Never raises on a 4xx/5xx;
    the status is handed back so callers map it (402/401 are meaningful here)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={**headers, **({"Content-Type": "application/json"} if data is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:   # nosec - fixed Tavus host
            raw = resp.read().decode() or "{}"
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = (exc.read() or b"").decode(errors="replace") or "{}"
        status = exc.code
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = {"raw": raw}
    return status, parsed if isinstance(parsed, dict) else {"data": parsed}


class TavusClient:
    """The four CVI calls. ``http`` is the injected transport (test seam)."""

    def __init__(self, api_key: str, http=None) -> None:
        if not api_key:
            raise ValueError("Tavus api_key is required")
        self.api_key = api_key
        self._http = http or _urllib_http

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        status, out = self._http(method, f"{TAVUS_API}{path}", {"x-api-key": self.api_key}, body)
        if status >= 400:
            msg = ""
            if isinstance(out, dict):
                msg = str(out.get("message") or out.get("error") or out.get("raw") or "")[:200]
            raise TavusError(status, msg)
        return out or {}

    def create_conversation(self, *, face_id: str = "", pal_id: str = "", context: str = "",
                            name: str = "", greeting: str = "", max_call_seconds: int = 900,
                            join_timeout: int = 300, left_timeout: int = 90,
                            require_auth: bool = True) -> dict:
        """Mint a private conversation. Returns {conversation_id, conversation_url (token appended),
        meeting_token}. A PAL (the interviewer's brain) takes precedence over a bare face."""
        if not (face_id or pal_id):
            raise ValueError("a face_id or pal_id is required")
        body: dict = {
            "conversation_name": name or "Mock interview",
            "require_auth": bool(require_auth),   # private room -> a short-lived meeting_token
            "properties": {
                "max_call_duration": int(max_call_seconds),          # hard ceiling on the call
                "participant_absent_timeout": int(join_timeout),     # time to grant cam/mic + join
                "participant_left_timeout": int(left_timeout),       # linger if they briefly drop
            },
        }
        if pal_id:
            body["pal_id"] = pal_id
        else:
            body["face_id"] = face_id
        if context:
            body["conversational_context"] = str(context)
        if greeting:
            body["custom_greeting"] = str(greeting)
        out = self._call("POST", "/conversations", body)
        url = out.get("conversation_url", "") or ""
        token = out.get("meeting_token", "") or ""
        if token and url and "t=" not in url:
            url = f"{url}{'&' if '?' in url else '?'}t={token}"
        return {"conversation_id": out.get("conversation_id", "") or "",
                "conversation_url": url, "meeting_token": token}

    def create_pal(self, *, name: str, system_prompt: str, face_id: str,
                   greeting: str = "") -> str:
        """Create the interviewer PAL once; returns its pal_id (the caller stores it)."""
        body: dict = {"pal_name": name, "system_prompt": system_prompt,
                      "default_face_id": face_id, "pipeline_mode": "full"}
        if greeting:
            body["greeting"] = greeting
        out = self._call("POST", "/pals", body)
        return str(out.get("pal_id") or "")

    def end_conversation(self, conversation_id: str) -> None:
        self._call("POST", f"/conversations/{conversation_id}/end", None)

    def get_conversation(self, conversation_id: str, verbose: bool = True) -> dict:
        q = "?verbose=true" if verbose else ""
        return self._call("GET", f"/conversations/{conversation_id}{q}", None)

    def transcript(self, conversation_id: str) -> list[dict]:
        """The dialogue as [{role: 'interviewer'|'candidate', content}], from the
        application.transcription_ready event. Empty if Tavus has not produced it (yet)."""
        data = self.get_conversation(conversation_id, verbose=True)
        return parse_transcript_events(data.get("events") or [])


def parse_transcript_events(events: list) -> list[dict]:
    """Pure: pull the transcript out of a verbose conversation's events. Tavus roles are
    'assistant' (the PAL) and 'user' (the person); we rename them for the report."""
    turns: list[dict] = []
    for ev in events or []:
        if not isinstance(ev, dict) or ev.get("event_type") != "application.transcription_ready":
            continue
        props = ev.get("properties") or {}
        for t in (props.get("transcript") or []):
            if not isinstance(t, dict):
                continue
            role = str(t.get("role") or "").lower()
            if role == "system":
                continue
            content = str(t.get("content") or "").strip()
            if not content:
                continue
            turns.append({"role": "interviewer" if role == "assistant" else "candidate",
                          "content": content,
                          "seconds_from_start": t.get("seconds_from_start"),
                          "duration": t.get("duration")})
    return turns


def parse_transcript_text(text: str) -> list[dict]:
    """A typed/pasted transcript, one turn per line, 'Interviewer: ...' / 'Me: ...' prefixes
    optional. Lines without a prefix alternate, starting with the interviewer."""
    turns: list[dict] = []
    for line in str(text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        role = None
        for pre, r in (("interviewer:", "interviewer"), ("ai:", "interviewer"),
                       ("assistant:", "interviewer"), ("q:", "interviewer"),
                       ("me:", "candidate"), ("candidate:", "candidate"), ("user:", "candidate"),
                       ("you:", "candidate"), ("a:", "candidate")):
            if low.startswith(pre):
                role, s = r, s[len(pre):].strip()
                break
        if role is None:
            role = "interviewer" if not turns or turns[-1]["role"] == "candidate" else "candidate"
        if turns and turns[-1]["role"] == role and role == "candidate":
            turns[-1]["content"] += " " + s
        else:
            turns.append({"role": role, "content": s})
    return turns
