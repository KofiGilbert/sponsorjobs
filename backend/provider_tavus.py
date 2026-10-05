"""Real Tavus CVI provider for the broker (the company key, server-side only).

A thin adapter from the broker's ``Provider.start_avatar_session`` contract onto the shared
``backend.tavus_client.TavusClient`` (the same client the engine uses with a person's OWN key, so
the request shape is written and tested once). The company key lives ONLY here on the server,
never in the downloaded app. We create a private room (require_auth) and get back a
conversation_url + a short-lived meeting_token; the user's browser joins that URL directly, so the
interview VIDEO never flows through our broker (only the session mint + metering do).

The HTTP transport is injected (``http=``), matching the repo's TelegramBot/Gmail pattern, so the
request shape and response parsing are unit-tested with a fake transport without a key.

Note: avatar-only. The bundled LLM uses the Anthropic provider; a production broker composes the
two (avatar -> Tavus, llm -> Anthropic). ``complete`` here raises on purpose.
"""

from __future__ import annotations

from backend.providers import Provider
from backend.tavus_client import TAVUS_API, TavusClient  # noqa: F401  (TAVUS_API re-exported)


class TavusProvider(Provider):
    def __init__(self, api_key: str, face_id: str = "", pal_id: str | None = None,
                 *, max_call_seconds: int = 900, join_timeout: int = 300,
                 left_timeout: int = 90, http=None) -> None:
        if not api_key:
            raise ValueError("Tavus api_key is required")
        if not (face_id or pal_id):
            raise ValueError("a face_id or pal_id is required")
        self.face_id = face_id
        self.pal_id = pal_id or ""
        self.max_call_seconds = max_call_seconds
        # How long the conversation waits for the person to actually join before ending, and how
        # long it lingers if they briefly drop. Without a generous join window Tavus ends the
        # conversation almost immediately and the person hits "meeting no longer available".
        self.join_timeout = join_timeout
        self.left_timeout = left_timeout
        self.client = TavusClient(api_key, http=http)

    def start_avatar_session(self, user: str, context: dict | None = None) -> dict:
        # JD-driven interviewer steering (role, questions). Passed through to Tavus to shape the
        # interviewer, not stored by us.
        ctx = context.get("prompt") if isinstance(context, dict) else (context or "")
        out = self.client.create_conversation(
            face_id=self.face_id, pal_id=self.pal_id, context=str(ctx or ""),
            name=f"Tailor mock interview ({user})", max_call_seconds=self.max_call_seconds,
            join_timeout=self.join_timeout, left_timeout=self.left_timeout, require_auth=True)
        return {"session_url": out["conversation_url"], "token": out["meeting_token"],
                "provider_session_id": out["conversation_id"]}

    def complete(self, model, prompt, **kw):
        raise NotImplementedError("TavusProvider is avatar-only; the LLM uses the Anthropic provider")
