"""The engine's per-launch session token and the sign-in nonce (ui/app.py _session_gate,
_new_signin_nonce / _take_signin_nonce).

The desktop shell starts the engine with TAILOR_SESSION_TOKEN and sets the same value as a cookie
on its own window; with the variable set, every engine request must carry it (cookie, or the
X-Tailor-Session header the shell's own code sends). Without it the engine behaves as before,
which the rest of the suite relies on.
"""

from __future__ import annotations

import importlib

import pytest

TOKEN = "t" * 64
HOST = "127.0.0.1:57000"
COOKIE = "sj_session_57000"


@pytest.fixture
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.delenv("TAILOR_SESSION_TOKEN", raising=False)
    import ui.app as A
    importlib.reload(A)
    A.app.config.update(TESTING=True)
    return A


@pytest.fixture
def S(A, monkeypatch):
    """The engine as the desktop shell runs it: a session token is set."""
    monkeypatch.setenv("TAILOR_SESSION_TOKEN", TOKEN)
    return A


def _c(A, cookie: str | None = None):
    c = A.app.test_client()
    if cookie is not None:
        c.set_cookie(COOKIE, cookie, domain="127.0.0.1")
    return c


def _get(c, path, **kw):
    return c.get(path, base_url=f"http://{HOST}", **kw)


def _post(c, path, **kw):
    return c.post(path, base_url=f"http://{HOST}", **kw)


# ---------------------------------------------------------------- the session token
def test_without_the_variable_nothing_changes(A):
    assert _get(_c(A), "/api/health").status_code == 200
    assert _get(_c(A), "/").status_code == 200


@pytest.mark.parametrize("path", ["/api/health", "/api/memory", "/api/account/status",
                                  "/api/extension/status", "/api/lock/status"])
def test_reads_without_the_token_are_refused(S, path):
    r = _get(_c(S), path)
    assert r.status_code == 403 and r.get_json()["reason"] == "no_session"


@pytest.mark.parametrize("path,kw", [
    ("/api/memory/erase", {"data": "x=1", "content_type": "application/x-www-form-urlencoded"}),
    ("/api/memory/erase", {"data": "x", "content_type": "text/plain"}),
    ("/api/lock/lock", {}),
    ("/api/open", {"json": {"u": "https://example.com/"}}),
    ("/api/account/signout", {"json": {}}),
])
def test_writes_without_the_token_are_refused(S, monkeypatch, path, kw):
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda u: pytest.fail("must not open"))
    r = _post(_c(S), path, **kw)
    assert r.status_code == 403


def test_a_wrong_token_is_refused(S):
    assert _get(_c(S, cookie="nope"), "/api/health").status_code == 403
    assert _get(_c(S), "/api/health", headers={"X-Tailor-Session": "nope"}).status_code == 403
    assert _get(_c(S, cookie=TOKEN[:-1]), "/api/health").status_code == 403


def test_the_cookie_or_the_header_lets_requests_through(S):
    assert _get(_c(S, cookie=TOKEN), "/api/health").status_code == 200
    assert _get(_c(S), "/api/health", headers={"X-Tailor-Session": TOKEN}).status_code == 200


def test_the_cookie_is_per_port(S):
    """A cookie for another engine port (the dev app beside the installed one) does not count."""
    c = S.app.test_client()
    c.set_cookie("sj_session_57010", TOKEN, domain="127.0.0.1")
    assert _get(c, "/api/health").status_code == 403
    assert c.get("/api/health", base_url="http://127.0.0.1:57010").status_code == 200


def test_the_open_route_works_with_the_token(S, monkeypatch):
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u) or True)
    r = _post(_c(S, cookie=TOKEN), "/api/open", json={"u": "https://example.com/"})
    assert r.status_code == 200 and opened == ["https://example.com/"]


def test_static_files_and_sign_in_pages_need_no_token(S):
    assert _get(_c(S), "/static/styles.css").status_code == 200
    assert _get(_c(S), "/account/google/done?via=browser").status_code == 200
    assert _get(_c(S), "/github/app/done").status_code == 200


def test_the_app_page_outside_the_app_window_says_where_to_go(S):
    body = _get(_c(S), "/").get_data(as_text=True)
    assert "sponsorjobs://open" in body and "own window" in body
    assert "Payment received" in _get(_c(S), "/?checkout=success").get_data(as_text=True)
    # the app window itself gets the app
    inside = _get(_c(S, cookie=TOKEN), "/").get_data(as_text=True)
    assert "sponsorjobs://open" not in inside


def test_the_extension_routes_keep_their_own_header(S):
    # profile autofill is an extension route: its header gets it past the session check ...
    assert _get(_c(S), "/api/profile/autofill", headers={"X-Tailor-Extension": "1"}).status_code == 200
    assert _get(_c(S), "/api/sponsors/status", headers={"X-Tailor-Extension": "1"}).status_code == 200
    # ... but the header opens nothing else
    assert _get(_c(S), "/api/memory", headers={"X-Tailor-Extension": "1"}).status_code == 403
    assert _post(_c(S), "/api/memory/erase", headers={"X-Tailor-Extension": "1"}).status_code == 403


# ---------------------------------------------------------------- where a request came from
@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_write_from_another_site_is_refused_even_with_the_token(S, site):
    r = _post(_c(S, cookie=TOKEN), "/api/lock/lock", headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_write_from_another_site_is_refused_without_the_shell_too(A, site):
    r = _post(_c(A), "/api/memory/erase", data="x", content_type="text/plain",
              headers={"Sec-Fetch-Site": site})
    assert r.status_code == 403


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_a_write_from_the_app_itself_goes_through(S, site):
    r = _post(_c(S, cookie=TOKEN), "/api/lock/lock", headers={"Sec-Fetch-Site": site})
    assert r.status_code == 200


# ---------------------------------------------------------------- the sign-in nonce
def _ok_broker(monkeypatch, saved):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: type("R", (), {"status_code": 200})())


def test_adopt_without_a_nonce_is_refused(A, monkeypatch):
    saved = {}
    monkeypatch.setattr(A, "_save_cred", lambda k, v: saved.__setitem__(k, v))
    _ok_broker(monkeypatch, saved)
    r = _post(_c(A), "/api/account/adopt", json={"token": "tok"})
    assert r.status_code == 400 and r.get_json()["reason"] == "stale_signin" and not saved
    r = _post(_c(A), "/api/account/adopt", json={"token": "tok", "nonce": "made-up"})
    assert r.status_code == 400 and not saved


def test_adopt_with_a_stale_nonce_is_refused(A, monkeypatch):
    saved = {}
    monkeypatch.setattr(A, "_save_cred", lambda k, v: saved.__setitem__(k, v))
    _ok_broker(monkeypatch, saved)
    old = A._new_signin_nonce(now=1000.0)              # long expired
    r = _post(_c(A), "/api/account/adopt", json={"token": "tok", "nonce": old})
    assert r.status_code == 400 and not saved


def test_adopt_with_a_fresh_nonce_works_once(A, monkeypatch):
    saved = {}
    monkeypatch.setattr(A, "_save_cred", lambda k, v: saved.__setitem__(k, v))
    _ok_broker(monkeypatch, saved)
    n = A._new_signin_nonce()
    assert _post(_c(A), "/api/account/adopt", json={"token": "tok", "nonce": n}).status_code == 200
    assert saved["TAILOR_ACCOUNT_TOKEN"] == "tok"
    saved.clear()
    r = _post(_c(A), "/api/account/adopt", json={"token": "tok2", "nonce": n})
    assert r.status_code == 400 and not saved          # single use


def test_adopt_works_from_the_persons_browser_with_the_shell_token_set(S, monkeypatch):
    """The callback page runs in the person's own browser, which has no session cookie: the
    nonce is what lets the sign-in finish."""
    saved = {}
    monkeypatch.setattr(S, "_save_cred", lambda k, v: saved.__setitem__(k, v))
    _ok_broker(monkeypatch, saved)
    n = S._new_signin_nonce()
    r = _post(_c(S), "/api/account/adopt", json={"token": "tok", "nonce": n},
              headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200 and saved["TAILOR_ACCOUNT_TOKEN"] == "tok"


def test_the_sign_in_url_carries_a_pending_nonce(A, monkeypatch):
    from urllib.parse import parse_qs, urlparse
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u) or True)
    assert _post(_c(A), "/api/account/google/open", json={}).status_code == 200
    redirect = parse_qs(urlparse(opened[0]).query)["redirect"][0]
    n = parse_qs(urlparse(redirect).query)["n"][0]
    assert n in A._SIGNIN_NONCES and "/account/google/done?via=browser&n=" in redirect


def test_only_a_few_nonces_are_kept(A):
    keys = [A._new_signin_nonce() for _ in range(A._SIGNIN_NONCE_MAX + 3)]
    assert len(A._SIGNIN_NONCES) == A._SIGNIN_NONCE_MAX
    assert keys[-1] in A._SIGNIN_NONCES and keys[0] not in A._SIGNIN_NONCES


def test_the_done_page_sends_the_nonce_and_never_reads_a_query_token(A):
    body = _get(_c(A), "/account/google/done?via=browser&n=abc").get_data(as_text=True)
    assert 'q.get("n")' in body and "nonce: nonce" in body
    assert 'q.get("account")' not in body


def test_every_extension_only_route_is_known_to_the_session_check(A):
    marked = {r.rule for r in A.app.url_map.iter_rules()
              if getattr(A.app.view_functions[r.endpoint], "_tailor_extension_route", False)}
    assert {"/api/profile/autofill", "/api/profile/prefs", "/api/profile/draft",
            "/api/profile/referral", "/api/match/score", "/api/tracking/summary",
            "/api/sponsors/lookup", "/api/autofill/applied", "/api/autoapply/fill-report"} <= marked
    assert "/api/memory/erase" not in marked and "/api/open" not in marked
