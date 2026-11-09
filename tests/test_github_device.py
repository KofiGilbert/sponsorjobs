"""Sign in with GitHub (OAuth device flow) replaces the token box as the default connect.

Acceptance: the page gets the code to show and never the device code; pending then approval
saves the token with the login; slow_down makes the wait longer; an expired or cancelled code
gives one plain line; the token is never in a response; status says whether the flow is
available (only with a Client ID); and no client secret exists in config/github_oauth.py.
GitHub is mocked through the FETCH seams, so nothing touches the network.
"""

import importlib
import re
from pathlib import Path

import pytest

DEVICE_CODE = "dc_secret_device_code_3584d83530557fdd1f46af8289938c8ef79f9dc5"
TOKEN = "gho_16C7e42F292c6912E7710c838347Ae178B4a"
CID = "Iv1.abc123publicclientid"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", CID)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_LOGIN", raising=False)
    import ui.app as A
    importlib.reload(A)
    import ui.github_device as D
    D.cancel()
    yield A, A.app.test_client(), D
    D.cancel()


class GitHub:
    """A scripted github.com: one device code, then the token answers in order."""

    def __init__(self, answers, user_status=200):
        self.answers = list(answers)
        self.calls = []
        self.user_status = user_status

    def device(self, method, url, token="", body=None, params=None):
        self.calls.append((url, dict(body or {})))
        if url.endswith("/login/device/code"):
            return 200, {"device_code": DEVICE_CODE, "user_code": "WDJB-MJHT",
                         "verification_uri": "https://github.com/login/device",
                         "expires_in": 900, "interval": 5}
        return 200, self.answers.pop(0)

    def api(self, method, url, token="", body=None, params=None):
        assert url.endswith("/user") and token == TOKEN
        return self.user_status, ({"login": "octocat"} if self.user_status == 200 else {})


def _wire(monkeypatch, D, gh):
    import ui.github_publish as G
    monkeypatch.setattr(D, "FETCH", gh.device)
    monkeypatch.setattr(G, "FETCH", gh.api)


def _due(monkeypatch, D):
    """Let the next poll through now, as if the interval had passed."""
    D._set(next_at=0)


def _all_text(*responses):
    return "".join(r.get_data(as_text=True) for r in responses)


def test_start_returns_the_code_but_never_the_device_code(client, monkeypatch):
    A, c, D = client
    gh = GitHub([])
    _wire(monkeypatch, D, gh)
    r = c.post("/api/github/device/start")
    body = r.get_json()
    assert r.status_code == 200
    assert body == {"ok": True, "user_code": "WDJB-MJHT", "expires_in": 900, "interval": 5,
                    "verification_uri": "https://github.com/login/device"}
    assert DEVICE_CODE not in r.get_data(as_text=True)
    # asked GitHub with the public client id and the repo scope, nothing secret
    url, sent = gh.calls[0]
    assert url == "https://github.com/login/device/code"
    assert sent == {"client_id": CID, "scope": "repo"}


def test_pending_then_success_saves_the_token_and_returns_the_login(client, monkeypatch):
    A, c, D = client
    gh = GitHub([{"error": "authorization_pending"}, {"access_token": TOKEN, "token_type": "bearer",
                                                      "scope": "repo"}])
    _wire(monkeypatch, D, gh)
    s = c.post("/api/github/device/start")
    _due(monkeypatch, D)
    r1 = c.post("/api/github/device/poll")
    assert r1.get_json() == {"ok": True, "connected": False, "state": "pending", "interval": 5}
    # asked again too soon (a second tab): no request spent, still pending
    n = len(gh.calls)
    r_soon = c.post("/api/github/device/poll")
    assert r_soon.get_json()["state"] == "pending" and len(gh.calls) == n
    _due(monkeypatch, D)
    r2 = c.post("/api/github/device/poll")
    assert r2.get_json() == {"ok": True, "connected": True, "state": "ok", "login": "octocat"}
    # the poll used the device-code grant
    url, sent = gh.calls[-1]
    assert url == "https://github.com/login/oauth/access_token"
    assert sent["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert sent["device_code"] == DEVICE_CODE and sent["client_id"] == CID
    # saved through the same credential the rest of the app reads
    assert A._cred("GITHUB_TOKEN") == TOKEN and A._cred("GITHUB_LOGIN") == "octocat"
    st = c.get("/api/github/status")
    assert st.get_json()["configured"] is True
    # the token and the device code never reach the page
    text = _all_text(s, r1, r_soon, r2, st)
    assert TOKEN not in text and DEVICE_CODE not in text
    # the creds file is private to the person's account
    assert (A._CRED_FILE.stat().st_mode & 0o777) == 0o600
    # the flow is finished: a stray poll doesn't reconnect or leak anything
    assert c.post("/api/github/device/poll").get_json()["connected"] is False


def test_slow_down_raises_the_interval(client, monkeypatch):
    A, c, D = client
    gh = GitHub([{"error": "slow_down", "interval": 10}, {"error": "slow_down"}])
    _wire(monkeypatch, D, gh)
    c.post("/api/github/device/start")
    _due(monkeypatch, D)
    assert c.post("/api/github/device/poll").get_json()["interval"] == 10
    _due(monkeypatch, D)
    assert c.post("/api/github/device/poll").get_json()["interval"] == 15


@pytest.mark.parametrize("err,state,line", [
    ("expired_token", "expired", "That code expired. Try again."),
    ("access_denied", "denied", "You cancelled on GitHub."),
    ("device_flow_disabled", "error", "Couldn't connect GitHub. Try again."),
])
def test_expired_and_denied_give_plain_lines(client, monkeypatch, err, state, line):
    A, c, D = client
    gh = GitHub([{"error": err, "error_description": "raw GitHub text"}])
    _wire(monkeypatch, D, gh)
    c.post("/api/github/device/start")
    _due(monkeypatch, D)
    body = c.post("/api/github/device/poll").get_json()
    assert body == {"ok": False, "connected": False, "state": state, "error": line}
    assert A._cred("GITHUB_TOKEN") == ""


def test_a_code_past_its_lifetime_expires_without_asking_github(client, monkeypatch):
    A, c, D = client
    gh = GitHub([])
    _wire(monkeypatch, D, gh)
    c.post("/api/github/device/start")
    D._set(expires_at=0)
    n = len(gh.calls)
    assert c.post("/api/github/device/poll").get_json()["error"] == "That code expired. Try again."
    assert len(gh.calls) == n


def test_a_token_github_will_not_confirm_is_not_saved(client, monkeypatch):
    A, c, D = client
    gh = GitHub([{"access_token": TOKEN}], user_status=401)
    _wire(monkeypatch, D, gh)
    c.post("/api/github/device/start")
    _due(monkeypatch, D)
    r = c.post("/api/github/device/poll")
    assert r.get_json()["connected"] is False and TOKEN not in r.get_data(as_text=True)
    assert A._cred("GITHUB_TOKEN") == ""


def test_status_reports_device_flow_only_with_a_client_id(client, monkeypatch):
    A, c, D = client
    assert c.get("/api/github/status").get_json()["device_flow"] is True
    monkeypatch.delenv("GITHUB_OAUTH_CLIENT_ID")
    import config.github_oauth as O
    monkeypatch.setattr(O, "CLIENT_ID", "")
    assert c.get("/api/github/status").get_json()["device_flow"] is False
    # and start refuses plainly instead of calling GitHub with no client id
    r = c.post("/api/github/device/start")
    assert r.status_code == 400 and r.get_json()["ok"] is False


def test_no_client_secret_in_the_oauth_config():
    src = Path("config/github_oauth.py").read_text(encoding="utf-8")
    assert not re.search(r"client[_ ]?secret\s*=", src, re.I)
    assert not re.search(r"CLIENT_SECRET|GITHUB_OAUTH_SECRET", src)
    # no 40-hex string (the shape of a GitHub OAuth client secret)
    assert not re.search(r"\b[0-9a-f]{40}\b", src)
    import config.github_oauth as O
    assert not [n for n in dir(O) if "secret" in n.lower()]


def test_the_chat_card_starts_the_sign_in():
    import agent.resume_chat as rc
    out = rc._t_github_status(type("Ctx", (), {"github_status": staticmethod(lambda: {})})(), {})
    assert out["card"]["buttons"] == [{"label": "Connect GitHub", "nav": "github"}]
    assert "github" in rc.NAV_TARGETS and "token" not in rc.GITHUB_CONNECT_STEPS.lower()
