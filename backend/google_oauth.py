"""'Sign in with Google' for the broker (P6). The Google client secret lives ONLY here on the
server, never in the downloaded app.

Flow (loopback via the broker, which has a real HTTPS domain Google can redirect to):
  1. GET /account/google/start   -> redirect the browser to Google's consent screen, carrying a
                                     signed `state` (CSRF).
  2. Google -> GET /account/google/callback?code&state
     -> verify state, exchange the code for tokens over TLS with Google, read the VERIFIED email
        from the returned id_token, find-or-create the account, then redirect to the LOCAL app
        carrying the new bearer token.
The desktop shell intercepts that final redirect (exactly like the Stripe return) and stores the
token, so it never lingers in the app.

The token exchange is injected (``http=``) so the whole flow is unit-tested without hitting Google.
Reading the email claim from the id_token is safe here because the token came DIRECTLY from Google
over TLS in the server-side code exchange (no third party could substitute it).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import urllib.parse
import urllib.request

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"   # nosec - fixed Google endpoint


def _form_post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as resp:   # nosec - fixed Google host, server-side
        return json.loads(resp.read().decode() or "{}")


def email_from_id_token(id_token: str) -> str | None:
    """Read the verified email from a Google id_token (a JWT). Signature check is unnecessary: the
    token was returned directly by Google over TLS in the code exchange."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:   # noqa: BLE001 - any malformed token -> no email
        return None
    if claims.get("email") and claims.get("email_verified", True):
        return str(claims["email"]).strip().lower()
    return None


class GoogleOAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str,
                 state_secret: str, *, http=_form_post) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.state_secret = state_secret
        self._http = http

    def sign_state(self) -> str:
        nonce = secrets.token_urlsafe(12)
        sig = hmac.new(self.state_secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{nonce}.{sig}"

    def valid_state(self, state: str) -> bool:
        try:
            nonce, sig = (state or "").rsplit(".", 1)
        except ValueError:
            return False
        expect = hmac.new(self.state_secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(sig, expect)

    def consent_url(self, state: str) -> str:
        q = urllib.parse.urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        })
        return f"{GOOGLE_AUTH}?{q}"

    def exchange_code(self, code: str) -> str | None:
        """Exchange the auth code for tokens; return the verified email (or None)."""
        resp = self._http(GOOGLE_TOKEN, {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }) or {}
        id_token = resp.get("id_token")
        return email_from_id_token(id_token) if id_token else None
