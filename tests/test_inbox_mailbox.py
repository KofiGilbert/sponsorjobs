"""The apply agent's mailbox (docs/apply-agent.md milestone 3, CLAUDE.md section 5).

An unattended run waits for the one mail a portal sends it: a verification link, a one-time
code, or a password reset. Pins: the pure detectors (otp_code, password_reset_link,
about_site, kind_of); the IMAP mailbox never writes (readonly select, no STORE) and honours
the since cut; the await route finds the right mail and ignores every other sender; IMAP
credentials are stored, reported masked, and never returned.
"""

from __future__ import annotations

import email.message
import imaplib
import importlib
import threading
import time

import pytest

from inbox import detect
from inbox import mailbox as mb


# ============================================================ otp_code
def test_otp_code_prefers_the_subject():
    msg = {"subject": "482913 is your Workday verification code",
           "body": "Enter the code 111111 if the subject is missing."}
    assert detect.otp_code(msg) == "482913"


def test_otp_code_from_the_body():
    msg = {"subject": "Verify your email",
           "body": "Hi Kofi,\n\nYour one-time code is:\n\n7731\n\nIt expires in 10 minutes."}
    assert detect.otp_code(msg) == "7731"


def test_otp_code_ignores_a_year_and_a_phone_number():
    msg = {"subject": "Thanks for applying",
           "body": "We got your application on 3 March 2026. Questions? Call (555) 123-4567 "
                   "or 555-987-6543 and quote the confirmation."}
    assert detect.otp_code(msg) == ""


def test_otp_code_ignores_a_zip_and_an_order_number():
    msg = {"subject": "Your order",
           "body": "Order #48213 ships to 12 Main St, Austin, TX 78701. Confirm by replying."}
    assert detect.otp_code(msg) == ""


def test_otp_code_four_and_eight_digits():
    assert detect.otp_code({"subject": "", "body": "Your PIN is 4821."}) == "4821"
    assert detect.otp_code({"subject": "", "body": "Security code: 12345678"}) == "12345678"
    # nine digits is not a code
    assert detect.otp_code({"subject": "", "body": "Security code: 123456789"}) == ""


def test_otp_code_needs_a_cue_word_nearby():
    assert detect.otp_code({"subject": "Hello", "body": "Room 4821 on the third floor."}) == ""


# ============================================================ password_reset_link
def test_password_reset_link_by_url():
    msg = {"body": "Forgot your password? https://acme.wd5.myworkdayjobs.com/wday/forgot?t=abc "
                   "or visit https://acme.com/help"}
    assert detect.password_reset_link(msg) == "https://acme.wd5.myworkdayjobs.com/wday/forgot?t=abc"


def test_password_reset_link_by_surrounding_words():
    msg = {"body": "To choose a new password, follow this link within an hour:\n"
                   "https://x.example/a/b?token=qq.\nThanks"}
    assert detect.password_reset_link(msg) == "https://x.example/a/b?token=qq"


def test_password_reset_link_absent():
    assert detect.password_reset_link({"body": "Welcome aboard https://x.example/home"}) == ""


# ============================================================ about_site
def test_about_site_workday_sender_for_a_myworkdayjobs_host():
    msg = {"from": "Acme Careers <acme@myworkday.com>", "subject": "Verify your account"}
    assert detect.about_site(msg, "acme.wd5.myworkdayjobs.com") is True


def test_about_site_same_registrable_domain():
    msg = {"from": "Jobs <noreply@mail.acme.co.uk>", "subject": "hi"}
    assert detect.about_site(msg, "careers.acme.co.uk") is True
    assert detect.about_site(msg, "https://www.careers.acme.co.uk/jobs/1") is True


def test_about_site_company_name_in_from_or_subject():
    msg = {"from": "no-reply@notify.example", "subject": "Your Acme application"}
    assert detect.about_site(msg, "jobs.other.com", company="Acme") is True
    assert detect.about_site(msg, "jobs.other.com", company="A") is False   # one letter is noise


def test_about_site_rejects_a_different_company():
    msg = {"from": "no-reply@beta.com", "subject": "Your Beta application"}
    assert detect.about_site(msg, "acme.wd5.myworkdayjobs.com", company="Acme") is False
    assert detect.about_site(msg, "careers.acme.com") is False


def test_about_site_ats_families_are_case_insensitive():
    msg = {"from": "No-Reply@Hire.Lever.co", "subject": "x"}
    assert detect.about_site(msg, "jobs.LEVER.co") is True
    assert detect.about_site({"from": "team@greenhouse-mail.io"}, "boards.greenhouse.io") is True


# ============================================================ kind_of
def test_kind_of():
    assert detect.kind_of({"subject": "Verify your email",
                           "body": "Click https://a.b/verify?t=1"}) == "verify"
    assert detect.kind_of({"subject": "Your code is 123456", "body": ""}) == "code"
    assert detect.kind_of({"subject": "Reset your password",
                           "body": "https://a.b/reset?t=1"}) == "reset"
    assert detect.kind_of({"subject": "Hello", "body": "nothing here"}) == ""


# ============================================================ ImapMailbox
NOW = time.time()


def _raw(subject: str, plain: str, html: str, frm: str = "Acme <acme@myworkday.com>") -> bytes:
    m = email.message.EmailMessage()
    m["From"] = frm
    m["To"] = "kofi@gmail.com"
    m["Subject"] = subject
    m["Date"] = email.utils.formatdate(NOW)
    m["Message-ID"] = f"<{abs(hash(subject))}@acme>"
    m.set_content(plain)
    m.add_alternative(html, subtype="html")
    return m.as_bytes()


RAW_NEW = _raw("Your verification code for Zoë", "Your verification code is 482913.",
               "<p>Your verification code is <b>482913</b>.</p>")
RAW_OLD = _raw("Old mail", "Last week's code 999999.", "<p>old</p>")


class _FakeImap:
    """Just enough of imaplib.IMAP4_SSL: records every call, refuses any write."""
    instances: list = []

    def __init__(self, host, port=993, timeout=None):
        self.host, self.port, self.calls = host, port, []
        _FakeImap.instances.append(self)

    def login(self, user, password):
        self.calls.append(("login", user))
        if password != "goodpassword":
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return ("OK", [b"ok"])

    def select(self, mailbox="INBOX", readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return ("OK", [b"2"])

    def uid(self, cmd, *args):
        self.calls.append(("uid", cmd) + args)
        if cmd == "search":
            return ("OK", [b"1 2"])
        if cmd == "fetch":
            new = imaplib.Time2Internaldate(NOW).encode()
            old = imaplib.Time2Internaldate(NOW - 2 * 86400).encode()
            return ("OK", [
                (b"2 (UID 2 INTERNALDATE " + new + b" BODY[] {1})", RAW_NEW), b")",
                (b"1 (UID 1 INTERNALDATE " + old + b" BODY[] {1})", RAW_OLD), b")",
            ])
        raise AssertionError(f"unexpected uid command {cmd}")

    def store(self, *a, **k):
        raise AssertionError("STORE must never be called: the mailbox is read-only")

    def logout(self):
        self.calls.append(("logout",))
        return ("BYE", [b"bye"])


@pytest.fixture
def fake_imap(monkeypatch):
    _FakeImap.instances.clear()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeImap)
    return _FakeImap


def test_imap_search_is_read_only_and_filters_by_since(fake_imap):
    box = mb.ImapMailbox("kofi@gmail.com", "goodpassword")
    out = box.search(NOW - 3600)
    conn = fake_imap.instances[-1]
    assert ("select", "INBOX", True) in conn.calls            # readonly, so the server refuses writes
    assert any(c[:2] == ("uid", "search") and "SINCE" in c[3] for c in conn.calls)
    assert any(c[:2] == ("uid", "fetch") and "BODY.PEEK[]" in c[3] for c in conn.calls)
    assert conn.calls[-1] == ("logout",)
    assert not any(c[0] == "store" for c in conn.calls)
    assert len(out) == 1                                       # the two-day-old mail is cut
    msg = out[0]
    assert msg["subject"] == "Your verification code for Zoë"  # RFC2047 decoded
    assert msg["from"] == "Acme <acme@myworkday.com>"
    assert "482913" in msg["body"] and "<b>" not in msg["body"]  # text/plain preferred
    assert msg["id"].startswith("<") and msg["date"]
    assert detect.kind_of(msg) == "code" and detect.otp_code(msg) == "482913"


def test_imap_search_returns_everything_when_since_is_old(fake_imap):
    out = mb.ImapMailbox("kofi@gmail.com", "goodpassword").search(NOW - 10 * 86400)
    assert [m["subject"] for m in out] == ["Your verification code for Zoë", "Old mail"]


def test_imap_test_login_reads_no_mail(fake_imap):
    assert mb.ImapMailbox("kofi@gmail.com", "goodpassword").test_login() == {"ok": True, "error": ""}
    conn = fake_imap.instances[-1]
    assert [c[0] for c in conn.calls] == ["login", "logout"]
    bad = mb.ImapMailbox("kofi@gmail.com", "wrong").test_login()
    assert bad["ok"] is False and "app password" in bad["error"]


def test_html_only_mail_is_stripped_to_text():
    m = email.message.EmailMessage()
    m["From"] = "a@b.c"
    m["Subject"] = "Reset"
    m.set_content('<p>Reset it <a href="https://a.b/reset?t=1">here</a>.</p>', subtype="html")
    msg = mb.parse_rfc822(m.as_bytes(), uid="7")
    assert "<p>" not in msg["body"] and "https://a.b/reset?t=1" in msg["body"]
    assert msg["id"] == "7"


def test_guess_imap_host_and_mask():
    assert mb.guess_imap_host("k@gmail.com") == "imap.gmail.com"
    assert mb.guess_imap_host("k@outlook.com") == "outlook.office365.com"
    assert mb.guess_imap_host("k@acme.org") == "imap.acme.org"
    assert mb.mask_email("kofi@gmail.com") == "k***@gmail.com"
    assert mb.mask_email("nope") == ""


# ============================================================ the routes
@pytest.fixture
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.delenv("RESUME_AGENT_FAKE_MAILBOX", raising=False)
    for name in ("IMAP_EMAIL", "IMAP_PASSWORD", "IMAP_HOST"):
        monkeypatch.delenv(name, raising=False)
    import ui.app as mod
    importlib.reload(mod)
    from inbox import gmail as g
    monkeypatch.setattr(g, "_TOKEN_PATH", tmp_path / "gmail_token.json")
    monkeypatch.setattr(g, "_CREDS_PATH", tmp_path / "gmail_credentials.json")
    monkeypatch.setattr(mb, "_FAKE", None)
    return mod, mod.app.test_client(), tmp_path


CODE_MAIL = {"from": "Acme Careers <acme@myworkday.com>",
             "subject": "Your Workday verification code",
             "body": "Enter this code to verify your email: 482913"}


def test_await_not_connected_without_a_mailbox(A):
    _, c, _ = A
    r = c.post("/api/inbox/await", json={"kind": "code", "host": "acme.wd5.myworkdayjobs.com",
                                         "since": time.time(), "timeout_s": 5}).get_json()
    assert r == {"ok": False, "reason": "not-connected"}


def test_await_returns_the_code_delivered_a_second_later(A, monkeypatch):
    _, c, _ = A
    monkeypatch.setenv("RESUME_AGENT_FAKE_MAILBOX", "1")
    since = time.time()

    def later():
        time.sleep(1.0)
        mb.fake_mailbox().deliver(CODE_MAIL)
    threading.Thread(target=later, daemon=True).start()

    r = c.post("/api/inbox/await", json={"kind": "code", "host": "acme.wd5.myworkdayjobs.com",
                                         "since": since, "timeout_s": 20}).get_json()
    assert r["ok"] is True and r["kind"] == "code" and r["code"] == "482913"
    assert r["subject"] == CODE_MAIL["subject"] and r["message_id"]


def test_await_any_returns_a_verification_link(A, monkeypatch):
    _, c, _ = A
    monkeypatch.setenv("RESUME_AGENT_FAKE_MAILBOX", "1")
    since = time.time()
    mb.fake_mailbox().deliver({"from": "no-reply@greenhouse.io",
                               "subject": "Please verify your email",
                               "body": "Confirm: https://boards.greenhouse.io/verify?t=abc"})
    r = c.post("/api/inbox/await", json={"kind": "any", "host": "boards.greenhouse.io",
                                         "since": since, "timeout_s": 5}).get_json()
    assert r["ok"] is True and r["kind"] == "verify"
    assert r["link"] == "https://boards.greenhouse.io/verify?t=abc"


def test_await_ignores_mail_from_another_site(A, monkeypatch):
    _, c, _ = A
    monkeypatch.setenv("RESUME_AGENT_FAKE_MAILBOX", "1")
    since = time.time()
    mb.fake_mailbox().deliver({"from": "Beta <no-reply@beta.com>",
                               "subject": "Your Beta code", "body": "Code: 111111"})
    r = c.post("/api/inbox/await", json={"kind": "code", "host": "acme.wd5.myworkdayjobs.com",
                                         "company": "Acme", "since": since,
                                         "timeout_s": 1}).get_json()
    assert r == {"ok": False, "reason": "timeout"}


def test_await_ignores_mail_received_before_since(A, monkeypatch):
    _, c, _ = A
    monkeypatch.setenv("RESUME_AGENT_FAKE_MAILBOX", "1")
    mb.fake_mailbox().deliver(CODE_MAIL, received=time.time() - 600)
    r = c.post("/api/inbox/await", json={"kind": "code", "host": "acme.wd5.myworkdayjobs.com",
                                         "since": time.time(), "timeout_s": 1}).get_json()
    assert r == {"ok": False, "reason": "timeout"}


def test_await_rejects_a_bad_kind_and_a_missing_host(A):
    _, c, _ = A
    assert c.post("/api/inbox/await", json={"kind": "spam", "host": "x.com"}).status_code == 400
    assert c.post("/api/inbox/await", json={"kind": "code"}).status_code == 400


def test_connect_imap_stores_creds_and_status_masks_the_email(A, monkeypatch):
    mod, c, tmp = A
    seen = []
    monkeypatch.setattr(mb.ImapMailbox, "test_login",
                        lambda self: (seen.append((self.email, self._password, self.host))
                                      or {"ok": True, "error": ""}))
    r = c.post("/api/inbox/connect-imap",
               json={"email": "kofi@gmail.com", "app_password": "abcd efgh ijkl mnop"})
    body = r.get_json()
    assert r.status_code == 200 and body == {"ok": True, "email": "kofi@gmail.com",
                                             "host": "imap.gmail.com"}
    assert "abcdefghijklmnop" not in r.get_data(as_text=True)
    assert seen == [("kofi@gmail.com", "abcdefghijklmnop", "imap.gmail.com")]  # spaces stripped
    creds = (tmp / "credentials.env").read_text()
    assert "IMAP_PASSWORD=abcdefghijklmnop" in creds and "IMAP_HOST=imap.gmail.com" in creds

    s = c.get("/api/inbox/status").get_json()
    assert s["method"] == "imap" and s["email"] == "k***@gmail.com" and s["configured"] is True
    assert "abcdefghijklmnop" not in str(s)
    assert s["token"] is False                       # the Gmail fields are still reported

    assert c.post("/api/inbox/disconnect-imap").get_json() == {"ok": True}
    assert "IMAP_PASSWORD" not in (tmp / "credentials.env").read_text()
    s = c.get("/api/inbox/status").get_json()
    assert s["method"] == "" and s["email"] == ""


def test_connect_imap_refused_sign_in_and_bad_shape(A, monkeypatch):
    mod, c, tmp = A
    monkeypatch.setattr(mb.ImapMailbox, "test_login",
                        lambda self: {"ok": False, "error": "Google refused the sign-in. Check "
                                                            "the app password and that IMAP is on."})
    r = c.post("/api/inbox/connect-imap", json={"email": "kofi@gmail.com", "app_password": "abcdefghijklmnop"})
    assert r.status_code == 400 and r.get_json()["ok"] is False
    assert "refused" in r.get_json()["error"]
    assert not (tmp / "credentials.env").exists()
    r = c.post("/api/inbox/connect-imap", json={"email": "kofi", "app_password": "abcdefghijklmnop"})
    assert r.status_code == 400 and "email" in r.get_json()["error"]
    r = c.post("/api/inbox/connect-imap", json={"email": "kofi@gmail.com", "app_password": "short"})
    assert r.status_code == 400 and "app password" in r.get_json()["error"]


def test_imap_creds_are_in_app_lock_custody():
    from ui.lock import Lock
    lk = Lock.__new__(Lock)
    assert lk.custodies("IMAP_PASSWORD") and lk.custodies("IMAP_EMAIL") and lk.custodies("IMAP_HOST")
    assert "IMAP_PASSWORD" in lk.custody_names()


def test_dev_deliver_is_404_outside_a_fake_mailbox_session(A, monkeypatch):
    _, c, _ = A
    r = c.post("/api/inbox/dev/deliver", json=CODE_MAIL)
    assert r.status_code == 404
    monkeypatch.setenv("RESUME_AGENT_FAKE_MAILBOX", "1")
    r = c.post("/api/inbox/dev/deliver", json=CODE_MAIL)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert c.get("/api/inbox/status").get_json()["method"] == "fake"
    assert mb.fake_mailbox().search(0)[0]["subject"] == CODE_MAIL["subject"]
