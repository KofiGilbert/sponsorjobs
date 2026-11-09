"""Connect GitHub, the one-click way (the SponsorJobs GitHub App, ui/github_app.py).

Acceptance: Connect opens the person's own browser at the Worker with state =
base64url(sha256(verifier)) and this engine's loopback page, and the verifier never reaches a
page; the loopback page's one-time key claims the tokens with that verifier, they are checked
with GET /user and saved through the usual credentials (plus the refresh token and expiry); a
claim the Worker refuses (wrong or expired) saves nothing; Cancel on GitHub says so; when the
Worker is not set up the page falls back to the device flow (or the token box); tokens are
renewed before they expire and once on a 401; Disconnect forgets everything and says where to
remove the App on GitHub; no token is ever in a response or a log line. The Worker and GitHub
are mocked through the BROKER_FETCH and FETCH seams, so nothing touches the network.
"""

import hashlib
import base64
import importlib
import logging
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ACCESS = "ghu_ACCESSnotreal0123456789abcdef"
REFRESH = "ghr_REFRESHnotreal0123456789abcdef"
NEW_ACCESS = "ghu_RENEWEDnotreal0123456789abcdef"
NEW_REFRESH = "ghr_RENEWEDnotreal0123456789abcdef"
NONCE = "nonce_from_the_fragment_0123456789abcdefABCDEF"
BROKER = "https://broker.test"

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("TAILOR_BROKER_URL", BROKER)
    monkeypatch.setenv("GITHUB_APP_PROBE", "1")
    monkeypatch.delenv("GITHUB_OAUTH_CLIENT_ID", raising=False)
    for name in ("GITHUB_TOKEN", "GITHUB_LOGIN", "GITHUB_REFRESH_TOKEN", "GITHUB_TOKEN_EXPIRES"):
        monkeypatch.delenv(name, raising=False)
    import ui.app as A
    importlib.reload(A)
    import ui.github_app as H
    H.clear()
    opened = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    yield A, A.app.test_client(), H, opened
    H.clear()


class Worker:
    """A scripted api.sponsorjobs.ai: /github/available, /github/claim, /github/refresh."""

    def __init__(self, configured=True, claim=None, refresh=None):
        self.configured = configured
        self.claim = claim or (200, {"ok": True, "access_token": ACCESS, "refresh_token": REFRESH,
                                     "expires_in": 28800, "refresh_token_expires_in": 15897600})
        self.refresh = refresh or (200, {"ok": True, "access_token": NEW_ACCESS,
                                         "refresh_token": NEW_REFRESH, "expires_in": 28800})
        self.calls = []

    def __call__(self, method, url, body=None, timeout=10):
        self.calls.append((method, url, dict(body or {})))
        if url == f"{BROKER}/github/available":
            if not self.configured:
                return 503, {"error": "github connect is not configured", "reason": "not_configured"}
            return 200, {"configured": True, "slug": "sponsorjobs",
                         "manage_url": "https://github.com/settings/installations"}
        if url == f"{BROKER}/github/claim":
            return self.claim
        if url == f"{BROKER}/github/refresh":
            return self.refresh
        raise AssertionError(f"unexpected Worker call {url}")


class GitHub:
    """api.github.com: GET /user answers for the tokens it knows; anything else is recorded."""

    def __init__(self, good=(ACCESS, NEW_ACCESS)):
        self.good = set(good)
        self.calls = []

    def __call__(self, method, url, token="", body=None, params=None):
        self.calls.append((method, url, token))
        if token not in self.good:
            return 401, {"message": "Bad credentials"}
        if url.endswith("/user"):
            return 200, {"login": "octocat"}
        return 200, {"ok": True}


def _wire(monkeypatch, H, worker, gh=None):
    import ui.github_publish as G
    monkeypatch.setattr(H, "BROKER_FETCH", worker)
    monkeypatch.setattr(G, "FETCH", gh or GitHub())


def _text(*responses):
    return "".join(r.get_data(as_text=True) for r in responses)


def _start(c, opened):
    r = c.post("/api/github/app/start")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert len(opened) == 1
    url = urlparse(opened[0])
    return r, url, parse_qs(url.query)


# ---------------------------------------------------------------- not configured: the fallback

def test_not_configured_falls_back_to_the_device_flow(client, monkeypatch):
    A, c, H, opened = client
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "Iv1.publicclientid")
    _wire(monkeypatch, H, Worker(configured=False))
    st = c.get("/api/github/status").get_json()
    assert st["app_flow"] is False and st["device_flow"] is True
    r = c.post("/api/github/app/start").get_json()
    assert r == {"ok": False, "fallback": "device"}
    assert opened == []                   # no browser opened onto a 503


def test_not_configured_and_no_device_flow_falls_back_to_the_token_box(client, monkeypatch):
    A, c, H, opened = client
    _wire(monkeypatch, H, Worker(configured=False))
    assert c.post("/api/github/app/start").get_json() == {"ok": False, "fallback": "token"}
    assert opened == []


def test_the_suite_default_never_probes_the_network(monkeypatch):
    import ui.github_app as H
    H.clear()
    monkeypatch.setenv("GITHUB_APP_PROBE", "0")
    monkeypatch.setattr(H, "BROKER_FETCH", lambda *a, **k: pytest.fail("probed the network"))
    assert H.available(BROKER) is False


# ---------------------------------------------------------------- the connect

def test_start_opens_the_browser_with_the_hashed_verifier_and_the_loopback_page(client, monkeypatch):
    A, c, H, opened = client
    _wire(monkeypatch, H, Worker())
    assert c.get("/api/github/status").get_json()["app_flow"] is True
    r, url, q = _start(c, opened)
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{BROKER}/github/connect"
    assert q["redirect"] == ["http://localhost/github/app/done"]
    verifier = H._PENDING["verifier"]
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["state"] == [digest] and len(digest) == 43
    assert verifier not in r.get_data(as_text=True) and verifier not in opened[0]
    assert c.get("/api/github/app/poll").get_json() == {"state": "waiting"}


def test_the_key_from_the_browser_claims_checks_and_saves_the_tokens(client, monkeypatch, caplog, capsys):
    A, c, H, opened = client
    worker, gh = Worker(), GitHub()
    _wire(monkeypatch, H, worker, gh)
    caplog.set_level(logging.DEBUG)
    s, _, q = _start(c, opened)
    verifier = H._PENDING["verifier"]
    done = c.get("/github/app/done")
    fin = c.post("/api/github/app/finish", json={"nonce": NONCE})
    assert fin.status_code == 200 and fin.get_json() == {"ok": True, "login": "octocat"}
    # the claim carried the kept verifier and the key, and nothing else
    claim = [x for x in worker.calls if x[1].endswith("/github/claim")]
    assert claim == [("POST", f"{BROKER}/github/claim", {"verifier": verifier, "nonce": NONCE})]
    # checked with GitHub before saving
    assert ("GET", "https://api.github.com/user", ACCESS) in gh.calls
    assert A._cred("GITHUB_TOKEN") == ACCESS and A._cred("GITHUB_LOGIN") == "octocat"
    assert A._cred("GITHUB_REFRESH_TOKEN") == REFRESH
    assert abs(int(A._cred("GITHUB_TOKEN_EXPIRES")) - (time.time() + 28800)) < 60
    poll = c.get("/api/github/app/poll")
    assert poll.get_json() == {"state": "connected", "login": "octocat"}
    st = c.get("/api/github/status")
    assert st.get_json()["configured"] is True and st.get_json()["login"] == "octocat"
    assert st.get_json()["via_app"] is True
    # no token, refresh token or verifier in any response, and none in the log
    text = _text(s, done, fin, poll, st)
    out = capsys.readouterr()
    logged = caplog.text + out.out + out.err
    for secret in (ACCESS, REFRESH, verifier):
        assert secret not in text
        assert secret not in logged


def test_a_claim_the_worker_refuses_saves_nothing(client, monkeypatch):
    A, c, H, opened = client
    _wire(monkeypatch, H, Worker(claim=(404, {"error": "not found", "reason": "not_found"})))
    _start(c, opened)
    fin = c.post("/api/github/app/finish", json={"nonce": "wrong-key"})
    assert fin.status_code == 400 and fin.get_json()["error"] == H.EXPIRED
    assert A._cred("GITHUB_TOKEN") == "" and A._cred("GITHUB_REFRESH_TOKEN") == ""
    assert c.get("/api/github/app/poll").get_json() == {"state": "error", "error": H.EXPIRED}


def test_a_finish_without_a_connect_in_progress_claims_nothing(client, monkeypatch):
    A, c, H, opened = client
    worker = Worker()
    _wire(monkeypatch, H, worker)
    fin = c.post("/api/github/app/finish", json={"nonce": NONCE})
    assert fin.status_code == 400
    assert not [x for x in worker.calls if x[1].endswith("/claim")]
    assert A._cred("GITHUB_TOKEN") == ""


def test_a_token_github_rejects_is_not_saved(client, monkeypatch):
    A, c, H, opened = client
    _wire(monkeypatch, H, Worker(), GitHub(good=()))
    _start(c, opened)
    fin = c.post("/api/github/app/finish", json={"nonce": NONCE})
    assert fin.status_code == 400 and A._cred("GITHUB_TOKEN") == ""


def test_cancel_on_github_says_so(client, monkeypatch):
    A, c, H, opened = client
    _wire(monkeypatch, H, Worker())
    _start(c, opened)
    fin = c.post("/api/github/app/finish", json={"error": "cancelled"})
    assert fin.status_code == 400
    assert c.get("/api/github/app/poll").get_json() == {"state": "denied", "error": H.DENIED}


def test_cancel_in_the_app_stops_the_wait(client, monkeypatch):
    A, c, H, opened = client
    _wire(monkeypatch, H, Worker())
    _start(c, opened)
    c.post("/api/github/app/cancel")
    assert c.get("/api/github/app/poll").get_json() == {"state": "idle"}
    assert c.post("/api/github/app/finish", json={"nonce": NONCE}).status_code == 400


def test_a_wait_left_open_too_long_expires(client, monkeypatch):
    A, c, H, opened = client
    _wire(monkeypatch, H, Worker())
    _start(c, opened)
    H._set(started=time.time() - H.WAIT_LIMIT - 1)
    assert c.get("/api/github/app/poll").get_json() == {"state": "error", "error": H.EXPIRED}


def test_the_done_page_reads_the_key_from_the_fragment_and_returns_to_the_app(client):
    A, c, H, opened = client
    r = c.get("/github/app/done")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "location.hash" in html and "location.search" not in html
    assert "history.replaceState" in html
    assert "sponsorjobs://github-connected" in html
    assert "/api/github/app/finish" in html
    assert "You can go back to SponsorJobs." in html


# ---------------------------------------------------------------- refresh

def _connected(A, expires_in):
    A._save_github_tokens({"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": expires_in},
                          "octocat")


def test_a_token_about_to_expire_is_renewed_first(client, monkeypatch):
    A, c, H, opened = client
    worker = Worker()
    _wire(monkeypatch, H, worker)
    _connected(A, 60)                       # one minute left: inside the five-minute margin
    assert A._github_token() == NEW_ACCESS
    assert ("POST", f"{BROKER}/github/refresh", {"refresh_token": REFRESH}) in worker.calls
    assert A._cred("GITHUB_TOKEN") == NEW_ACCESS and A._cred("GITHUB_REFRESH_TOKEN") == NEW_REFRESH
    n = len(worker.calls)
    assert A._github_token() == NEW_ACCESS and len(worker.calls) == n   # fresh now: no second call


def test_a_fresh_token_is_used_as_it_is(client, monkeypatch):
    A, c, H, opened = client
    worker = Worker()
    _wire(monkeypatch, H, worker)
    _connected(A, 28800)
    assert A._github_token() == ACCESS
    assert not [x for x in worker.calls if x[1].endswith("/refresh")]


def test_a_401_renews_once_and_retries(client, monkeypatch):
    A, c, H, opened = client
    worker, gh = Worker(), GitHub(good=(NEW_ACCESS,))   # GitHub has already expired the old one
    _wire(monkeypatch, H, worker, gh)
    _connected(A, 28800)
    import ui.github_publish as G
    status, data = G._fetch("GET", f"{G.API}/repos/octocat/x", ACCESS)
    assert status == 200
    assert gh.calls[-1][2] == NEW_ACCESS
    assert len([x for x in worker.calls if x[1].endswith("/refresh")]) == 1
    # a second call still holding the old token gets the renewed one without refreshing again
    status, _ = G._fetch("GET", f"{G.API}/repos/octocat/y", ACCESS)
    assert status == 200 and len([x for x in worker.calls if x[1].endswith("/refresh")]) == 1


def test_a_refused_refresh_token_keeps_things_as_they_were(client, monkeypatch):
    A, c, H, opened = client
    worker = Worker(refresh=(400, {"error": "sign in again", "reason": "bad_refresh_token"}))
    _wire(monkeypatch, H, worker, GitHub(good=()))
    _connected(A, 28800)
    import ui.github_publish as G
    status, _ = G._fetch("GET", f"{G.API}/user", ACCESS)
    assert status == 401
    assert A._cred("GITHUB_TOKEN") == ACCESS
    assert H.refresh(BROKER, REFRESH) == {"ok": False, "reason": "expired"}


def test_a_pasted_token_never_refreshes(client, monkeypatch):
    A, c, H, opened = client
    worker = Worker()
    _wire(monkeypatch, H, worker, GitHub(good=("ghp_pasted",)))
    _connected(A, 60)
    r = c.post("/api/github", json={"token": "ghp_pasted"})
    assert r.status_code == 200
    assert A._cred("GITHUB_REFRESH_TOKEN") == "" and A._cred("GITHUB_TOKEN_EXPIRES") == ""
    assert A._github_token() == "ghp_pasted"
    assert not [x for x in worker.calls if x[1].endswith("/refresh")]


# ---------------------------------------------------------------- disconnect

def test_disconnect_forgets_everything_and_says_where_to_remove_the_app(client, monkeypatch):
    A, c, H, opened = client
    _wire(monkeypatch, H, Worker())
    _connected(A, 28800)
    c.get("/api/github/status")              # the probe learns the manage URL
    r = c.post("/api/github/disconnect").get_json()
    assert r == {"ok": True, "manage_url": "https://github.com/settings/installations"}
    for name in ("GITHUB_TOKEN", "GITHUB_LOGIN", "GITHUB_REFRESH_TOKEN", "GITHUB_TOKEN_EXPIRES"):
        assert A._cred(name) == ""
    assert c.get("/api/github/status").get_json()["configured"] is False


def test_app_lock_keeps_the_refresh_token_too():
    from ui.lock import _CRED_NAMES
    assert {"GITHUB_TOKEN", "GITHUB_REFRESH_TOKEN", "GITHUB_TOKEN_EXPIRES"} <= set(_CRED_NAMES)


# ---------------------------------------------------------------- the page

def test_the_settings_row_has_the_one_click_states():
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    for el in ("ghAppWait", "ghAppCancel", "ghAppMsg", "ghManageWrap", "ghManage", "ghToggle", "ghAdvanced"):
        assert f'id="{el}"' in html
    assert "Finish in your browser" in html
    assert '"Connect GitHub"' in js and "Choose repos on GitHub" in js
    assert "/api/github/app/start" in js and "/api/github/app/poll" in js
    # the fallback is wired: a start that says fallback goes on to the device flow / token box
    assert "d.fallback" in js


def test_no_secret_lives_in_the_desktop_app():
    for path in ("ui/github_app.py", "config/github_oauth.py"):
        text = (ROOT / path).read_text(encoding="utf-8").lower()
        assert "client_secret" not in text.replace("client_secret,", "")
