"""Portal passwords (2026-11-18): a real Workday create form refused a saved password with
"Password must include: - A numeric character", and the agent then asked for a reset mail that
could never come.

Pins: the generator always holds an uppercase letter, a lowercase letter, a digit and a symbol;
a password is rotated ONLY for a password-rule error on a host where no sign-in or creation ever
worked; a weak saved password on such a host is replaced at next use; the JS ladder decision
(tests/js/test_agent_portal_password.js) runs below. Made-up values only, no network.
"""
from __future__ import annotations

import importlib
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST = "acme.wd1.myworkdayjobs.com"
NAME = "PORTAL_ACME_WD1_MYWORKDAYJOBS_COM"
RULE = "Password must include: - A numeric character"       # the site's own words, 2026-11-18
URL = "/api/autoapply/agent/portal-account"


@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    mod._memory().save("default", {"identity": {"name": "Test Person", "email": "person@example.com"}}, {}, [])
    c = mod.app.test_client()
    c.post("/api/autoapply/consent", json={"accounts": True})
    return mod, c, tmp_path


def _saved(tmp) -> str:
    raw = (tmp / "credentials.env").read_text(encoding="utf-8")
    return next(ln for ln in raw.splitlines() if ln.startswith(NAME + "=")).split("=", 1)[1].partition("|")[2]


def test_generator_always_has_every_character_class(A):
    mod, _, _ = A
    seen = set()
    for _ in range(5000):
        pw = mod._portal_password()
        assert len(pw) == 16
        assert re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw) and re.search(r"\d", pw), "a class is missing"
        symbols = [ch for ch in pw if not ch.isalnum()]
        assert len(symbols) == 2 and all(ch in "!@#$%&*?" for ch in symbols)
        assert pw[0].isalpha() and all(a != b for a, b in zip(pw, pw[1:]))
        assert not mod._password_weak(pw)
        seen.add(pw)
    assert len(seen) == 5000


def test_generator_honours_a_shorter_length_and_no_symbols(A):
    mod, _, _ = A
    for _ in range(500):
        pw = mod._portal_password(12, "")
        assert len(pw) == 12 and pw.isalnum() and not mod._password_weak(pw)
        pw = mod._portal_password(10, "!")
        assert len(pw) == 10 and pw.count("!") == 1 and not mod._password_weak(pw)


def test_weak_means_no_upper_lower_or_digit(A):
    mod, _, _ = A
    assert mod._password_weak("abcdefghIJKLMN!@")          # no digit: the real failure
    assert mod._password_weak("abcdefgh1234567!") and mod._password_weak("ABCDEFGH1234567!")
    assert not mod._password_weak("Abcdefgh1234567!") and not mod._password_weak("Abcdefgh1234")


def test_what_counts_as_a_password_rule_error(A):
    mod, _, _ = A
    for e in (RULE, "Your password does not meet the requirements.", "Password is too short",
              "Password must be at least 8 characters", "A special character", "An uppercase character",
              "Password cannot contain your email"):
        assert mod._is_password_rule_error(e), e
    for e in ("An account with this email address already exists.", "Passwords do not match",
              "Password is required", "Email must include @", "Please accept the terms", "",
              "This email is already registered. Password must be reset."):
        assert not mod._is_password_rule_error(e), e


def test_limits_read_from_the_sites_error(A):
    mod, _, _ = A
    assert mod._password_limits(RULE) == (16, "!@#$%&*?")
    assert mod._password_limits("Password must be 8 to 12 characters") == (12, "!@#$%&*?")
    assert mod._password_limits("Password must be no more than 14 characters long")[0] == 14
    assert mod._password_limits("Password must be between 8 and 20 characters")[0] == 16
    assert mod._password_limits("Password may only contain letters and numbers") == (16, "")
    assert mod._password_limits("Password must not contain special characters") == (16, "")
    assert "&" not in mod._password_limits("Password cannot contain & or %")[1]
    assert mod._password_limits("Password must include a special character (!@$)")[1] == "!@$"
    assert mod._password_limits("Password must include a symbol: - _ +")[1] == "-_+"
    assert mod._password_limits("Password has a maximum of 6 characters") is None


def test_rotate_only_for_a_rule_error_and_only_before_anything_worked(A):
    mod, c, tmp = A
    assert c.post(URL, json={"host": HOST, "create": True}).get_json()["created"] is True
    first = _saved(tmp)
    # not a password rule: nothing changes
    d = c.post(URL, json={"host": HOST, "rotate": True, "error": "An account with this email already exists."}).get_json()
    assert d["ok"] is False and d["reason"] == "not-a-password-rule" and _saved(tmp) == first
    d = c.post(URL, json={"host": HOST, "rotate": True, "error": "Passwords do not match"}).get_json()
    assert d["ok"] is False and _saved(tmp) == first
    # the site's real error: a fresh compliant password, the same email, the password never returned
    d = c.post(URL, json={"host": HOST, "rotate": True, "error": RULE}).get_json()
    second = _saved(tmp)
    assert d == {"ok": True, "rotated": True, "host": HOST, "email": "person@example.com"}
    assert second != first and not mod._password_weak(second) and len(second) == 16
    # the site's stated limit is honoured
    c.post(URL, json={"host": HOST, "rotate": True, "error": "Password must be no more than 12 characters, letters and numbers only"})
    third = _saved(tmp)
    assert len(third) == 12 and third.isalnum() and not mod._password_weak(third)
    d = c.post(URL, json={"host": HOST, "rotate": True, "error": "Password has a maximum of 6 characters"}).get_json()
    assert d["ok"] is False and d["reason"] == "password-rule" and _saved(tmp) == third
    # a sign-in or creation worked: the marker is set, and the password never rotates again
    assert c.get(URL + "?host=" + HOST).get_json()["confirmed"] is False
    assert c.post(URL, json={"host": HOST, "confirm": True}).get_json() == {"ok": True, "host": HOST, "confirmed": True}
    assert c.get(URL + "?host=" + HOST).get_json()["confirmed"] is True
    d = c.post(URL, json={"host": HOST, "rotate": True, "error": RULE}).get_json()
    assert d["ok"] is False and d["reason"] == "account-in-use" and _saved(tmp) == third
    for body in (d, c.get(URL + "?host=" + HOST).get_json()):
        assert third not in str(body) and second not in str(body)
    # the marker is not a portal account of its own and holds no secret
    raw = (tmp / "credentials.env").read_text(encoding="utf-8")
    assert "PORTALOK_ACME_WD1_MYWORKDAYJOBS_COM=1" in raw.splitlines()


def test_rotate_and_confirm_need_a_saved_account_and_the_setting(A):
    mod, c, tmp = A
    assert c.post(URL, json={"host": HOST, "rotate": True, "error": RULE}).get_json()["reason"] == "no-account"
    assert c.post(URL, json={"host": HOST, "confirm": True}).get_json()["reason"] == "no-account"
    assert c.post(URL, json={"host": "linkedin.com", "rotate": True, "error": RULE}).get_json()["reason"] == "fill-by-hand"
    c.post(URL, json={"host": HOST, "create": True})
    first = _saved(tmp)
    c.post("/api/submit/settings", json={"portal_accounts": False})
    d = c.post(URL, json={"host": HOST, "rotate": True, "error": RULE}).get_json()
    assert d["ok"] is False and d["reason"] == "portal_accounts_off" and _saved(tmp) == first


def test_a_weak_saved_password_is_replaced_at_next_use_unless_it_ever_worked(A):
    mod, c, tmp = A
    weak = "abcdefghIJKLMN!@"                               # what the old generator could save
    mod._save_cred(NAME, "person@example.com|" + weak)
    d = c.post(URL, json={"host": HOST, "create": True}).get_json()
    assert d["ok"] is True and d["created"] is True and d["regenerated"] is True and d["email"] == "person@example.com"
    fresh = _saved(tmp)
    assert fresh != weak and not mod._password_weak(fresh)
    again = c.post(URL, json={"host": HOST, "create": True}).get_json()
    assert again["created"] is False and _saved(tmp) == fresh          # a good one is left alone
    # the same weak password on a host where sign-in worked stays: the site knows it
    other, other_name = "beta.wd5.myworkdayjobs.com", "PORTAL_BETA_WD5_MYWORKDAYJOBS_COM"
    mod._save_cred(other_name, "person@example.com|" + weak)
    c.post(URL, json={"host": other, "confirm": True})
    d = c.post(URL, json={"host": other, "create": True}).get_json()
    assert d["created"] is False and "regenerated" not in d
    assert mod._portal_account(other)["password"] == weak


def test_the_ladder_decision_in_the_shell():
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_agent_portal_password.js")],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout[-2000:] + out.stderr[-2000:]
