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
import urllib.error
import urllib.request

from llm.anthropic_client import DEFAULT_MODEL, AnthropicLLM

# Say who is calling. The hosted broker sits behind Cloudflare, whose bot protection refuses
# Python's default "Python-urllib" agent with a 403 (found 2026-10-07 on the first live deploy);
# an honest product name gets through, and it is what the requests should say anyway.
BROKER_USER_AGENT = "SponsorJobs/0.1 (+https://github.com/KofiGilbert/sponsorjobs)"


class BrokerUnavailable(RuntimeError):
    """The managed broker could not be reached, or refused the call (service down, or quota hit).

    Carries a user-facing message AND a machine-readable ``reason`` the app turns into the right
    screen: ``upgrade_required`` (out of plan allowance -> show the upgrade prompt, NOT the
    'your Anthropic credit ran out' one, which is a different problem with a different fix) or
    ``service_down`` (our managed AI is temporarily unreachable)."""

    def __init__(self, message: str, reason: str = ""):
        super().__init__(message)
        self.reason = reason


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
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:   # nosec - loopback broker
                out = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            message, reason = self._error(exc)
            raise BrokerUnavailable(message, reason) from exc
        except urllib.error.URLError as exc:
            raise BrokerUnavailable(
                "Tailor's AI service is briefly unavailable. Please try again in a moment.",
                "service_down") from exc
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
    def _error(exc: "urllib.error.HTTPError") -> "tuple[str, str]":
        detail = ""
        try:
            detail = (json.loads(exc.read().decode() or "{}") or {}).get("error", "")
        except Exception:   # noqa: BLE001 - best-effort read of an error body
            detail = ""
        if exc.code == 402:   # out of plan allowance -> a paywall moment, not a hard error
            return (detail or "You have used up the AI included in your plan for this period.",
                    "upgrade_required")
        return (detail or "Tailor's AI service is temporarily unavailable. Please try again.",
                "service_down")
