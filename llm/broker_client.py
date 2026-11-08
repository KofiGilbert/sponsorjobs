"""BrokerLLM: the bundled-AI path for the app's language model.

Instead of calling Anthropic directly with the user's OWN key (that is what AnthropicLLM does),
this routes every completion through Tailor's managed broker (backend/broker.py) over loopback.
The broker holds the company Anthropic key server-side and meters usage per plan, so the default
experience needs NO key from the user. Power users who add their own key fall back to AnthropicLLM
(bring-your-own) in ui.app._make_llm.

Why a subclass: AnthropicLLM already carries ~30 specialised prompt/parse methods
(generate_intake_questions, draft_cover_letter, coach_interview_answer, ...). Every one of them
funnels through exactly two low-level calls -- _complete (a single system+user turn) and
_complete_history (a multi-turn conversation). Overriding just those two reroutes the entire
surface to the broker with zero duplication. This class never imports the anthropic SDK and never
needs a key locally: the broker owns both the key and the per-plan model policy.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from llm.anthropic_client import DEFAULT_MODEL, AnthropicLLM

# Say who is calling. The hosted broker sits behind Cloudflare, whose bot protection refuses
# Python's default "Python-urllib" agent with a 403 (found 2026-10-07 on the first live deploy);
# an honest product name gets through, and it is what the requests should say anyway.
BROKER_USER_AGENT = "SponsorJobs/0.1 (+https://github.com/KofiGilbert/sponsorjobs)"


# What the person reads when the failure is OURS (the company key has no credit, is disabled or is
# invalid: the broker answers {error: "ai_unavailable", reason: "company"}). Never an upgrade, never
# a key prompt: nothing they can buy fixes it, and the owner has already been alerted by the broker.
AI_COMPANY_TEXT = ("SponsorJobs' AI is temporarily unavailable. Your pass and packages are not "
                   "affected. We have been alerted; try again in a little while.")
# A passing failure (rate limit, overload): worth a retry here, then a plain "busy".
AI_TRANSIENT_TEXT = "The AI is busy right now. Try again in a moment."
TRANSIENT_TRIES = 3            # attempts on a transient answer, with a short backoff between
TRANSIENT_MAX_WAIT = 8         # seconds: the broker's retry_after is honoured up to this


class BrokerUnavailable(RuntimeError):
    """The managed broker could not be reached, or refused the call (service down, or quota hit).

    Carries a user-facing message AND a machine-readable ``reason`` the app turns into the right
    screen: ``upgrade_required`` (out of plan allowance -> show the upgrade prompt, NOT the
    'your Anthropic credit ran out' one, which is a different problem with a different fix),
    ``ai_company`` (OUR AI is down: the company key failed; the person is told plainly and never
    asked to buy anything), ``ai_transient`` (the model is busy; retried here first) or
    ``service_down`` (the broker itself is unreachable)."""

    def __init__(self, message: str, reason: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.reason = reason
        self.retry_after = retry_after


class BrokerLLM(AnthropicLLM):
    def __init__(self, model: str | None = None, effort: str = "low", *,
                 broker_url: str | None = None, user: str | None = None, timeout: int = 120,
                 auth_token: str | None = None) -> None:
        # Deliberately does NOT call AnthropicLLM.__init__: no anthropic SDK import, no local key.
        # `model` is only a hint here -- the broker picks the real model per the user's plan, so a
        # free user runs on Haiku and a paid user on Sonnet without this side deciding policy.
        self.model = model or os.environ.get("RESUME_AGENT_MODEL") or DEFAULT_MODEL
        self.effort = effort
        base = broker_url or os.environ.get("TAILOR_BROKER_URL", "http://127.0.0.1:57001")
        self.broker_url = base.rstrip("/")
        self.user = user or os.environ.get("TAILOR_BROKER_USER", "local")
        self.auth_token = auth_token   # bearer token -> the app's own account; else the dev header
        self.timeout = timeout

    # -- the two low-level calls every specialised method funnels through -------------------- #
    def _complete(self, system: str, user: str, max_tokens: int = 1024,
                  effort: str | None = None) -> str:
        return self._post({"system": system, "prompt": user,
                           "max_tokens": max_tokens, "effort": effort or self.effort})

    def agent_step_image(self, system: str, user: str, image_b64: str) -> str:
        # The broker takes the screenshot as one base64 PNG next to the prompt; a broker that
        # does not know the field answers from the text alone, which is still a valid step.
        return self._post({"system": system, "prompt": user, "max_tokens": 1100, "effort": "medium",
                           "image": {"media_type": "image/png", "data": image_b64}})

    def _complete_history(self, system, history, max_tokens: int = 1500,
                          effort: str | None = None) -> str:
        # Map the app's turn history (role 'agent' == the assistant) into the Anthropic message
        # shape, ensuring the first turn is a user turn -- exactly as the local AnthropicLLM does,
        # so the broker sees an identical conversation.
        messages = []
        for h in history:
            role = "assistant" if h.get("role") == "agent" else "user"
            content = h.get("content", "")
            if content:
                messages.append({"role": role, "content": content})
        if not messages or messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": "(start)"})
        return self._post({"system": system, "messages": messages,
                           "max_tokens": max_tokens, "effort": effort or self.effort})

    def agent_turn(self, system: str, messages: list[dict], tools: list[dict],
                   max_tokens: int = 900) -> dict:
        # The broker (worker/src/anthropic.ts complete) returns text only and drops tool use
        # blocks, so the agent speaks the JSON protocol through it until the broker passes tools
        # through (a later piece). Then this becomes native like AnthropicLLM.agent_turn.
        from .agent_protocol import json_agent_turn
        return json_agent_turn(self._complete_history, system, messages, tools, max_tokens)

    # -- transport -------------------------------------------------------------------------- #
    def _post(self, payload: dict) -> str:
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "User-Agent": BROKER_USER_AGENT}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"   # the app's own account
        else:
            headers["X-Tailor-User"] = self.user                     # dev/offline fallback
        req = urllib.request.Request(
            f"{self.broker_url}/llm/complete", data=data, method="POST", headers=headers)
        # One retry on a dropped or stalled connection. A slow home connection stalls a long
        # answer (the whole-profile draft) now and then, and without this the person lost a
        # build that had otherwise succeeded (2026-10-08). A quota answer is never re-sent, and
        # neither is a company outage (the key is dead until the owner acts); only the broker's
        # "transient" answer (the model is rate limited or overloaded, after the broker's own
        # retries) is tried again here, a few times with a short backoff, so a passing blip never
        # reaches the person as a failure.
        last_exc = None
        out = None
        attempt = 0
        transient = 0
        while attempt < 2:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:   # nosec - our broker
                    out = json.loads(resp.read().decode() or "{}")
                break
            except urllib.error.HTTPError as exc:
                message, reason, retry_after = self._error(exc)
                transient += 1
                if reason == "ai_transient" and transient < TRANSIENT_TRIES:
                    wait = retry_after if retry_after is not None else 2 * transient
                    time.sleep(min(max(wait, 0), TRANSIENT_MAX_WAIT))
                    continue
                raise BrokerUnavailable(message, reason, retry_after) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                attempt += 1
                if attempt < 2:
                    time.sleep(2)
                    continue
        if out is None:
            raise BrokerUnavailable(
                "SponsorJobs' AI took too long to answer. Check your connection and try again.",
                "service_down") from last_exc
        text = (out.get("text") or "").strip()
        # Last line of defence against shipping a test double as product. FakeProvider
        # answers with "[fake:<model>] tailored from: ..." -- text that compiles into a
        # perfectly normal-looking resume made entirely of placeholder. Whatever
        # misconfiguration produced it (a missing key, a broker wired to the fake), the
        # honest outcome is to stop and say so, not to hand the person a CV they might
        # send to an employer. CLAUDE.md §13.
        if text.startswith("[fake:"):
            raise BrokerUnavailable(
                "Tailor's AI is not configured with a real model, so it cannot write your "
                "resume. Check your API key in Settings, then try again.",
                "no_key")
        return text

    @staticmethod
    def _error(exc: "urllib.error.HTTPError") -> "tuple[str, str, float | None]":
        """(message for the person, reason, retry_after seconds or None) for a broker answer."""
        body: dict = {}
        try:
            body = json.loads(exc.read().decode() or "{}") or {}
            if not isinstance(body, dict):
                body = {}
        except Exception:   # noqa: BLE001 - best-effort read of an error body
            body = {}
        detail = str(body.get("error") or "")
        if detail == "ai_unavailable":
            # The broker's structured outage answer (worker/src/outage.ts). The person never sees
            # Anthropic's words, and a company failure never reads as something to buy.
            why = str(body.get("reason") or "")
            retry_after = body.get("retry_after")
            retry_after = float(retry_after) if isinstance(retry_after, (int, float)) else None
            if why == "company":
                return AI_COMPANY_TEXT, "ai_company", None
            return AI_TRANSIENT_TEXT, "ai_transient", retry_after
        if exc.code == 402:   # out of plan allowance -> a paywall moment, not a hard error
            return (detail or "You have used up the AI included in your plan for this period.",
                    "upgrade_required", None)
        return (detail or "Tailor's AI service is temporarily unavailable. Please try again.",
                "service_down", None)
