"""Privacy + robustness hardening for inbox/ (CLAUDE.md §5). Pins the invariants an
adversarial audit found unenforced or untested:

  * READ-FIRST — a personal email whose only match is a generic word (a friend's "how did
    your interview go?") is NEVER treated as a recruiter reply, so its private body is never
    shipped to the LLM; two distinct cues are required;
  * the Gmail grant stays READ-ONLY (no send/compose/modify) and the query stays scoped;
  * parsing hostile mail (bad base64, html-only, empty, deeply-nested MIME) never crashes;
  * the OAuth token/credentials stay git-ignored;
  * complete_verification refuses a non-public / non-http link (SSRF / local-file / scheme abuse).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from inbox import detect, gmail
from inbox.service import scan_inbox

ROOT = Path(__file__).resolve().parents[1]


# ============================ bug: classifier read-first (no lone generic word) ============================
class _SpyLLM:
    def __init__(self):
        self.bodies: list[str] = []

    def draft_email_reply(self, subject, body, frm, profile):
        self.bodies.append(body)
        return "draft"


FRIEND_MSG = {
    "id": "f1", "from": "Jamie Chen <jamie@gmail.com>",
    "subject": "how did your interview go?",
    "body": "Hey! Curious how the interview went — want to grab coffee this weekend?",
    "date": "Mon, 14 Jul 2026 12:00:00 -0500",
}


def test_personal_email_with_a_lone_generic_word_is_not_a_recruiter():
    assert detect.classify(FRIEND_MSG) == "other"


def test_a_friends_interview_email_body_never_reaches_the_llm():
    spy = _SpyLLM()
    out = scan_inbox([FRIEND_MSG], profile={}, llm=spy)
    assert out["recruiters"] == [] and out["other"] == 1
    assert spy.bodies == []            # the private body was NEVER sent off-device (§5 read-first)


def test_two_distinct_weak_cues_still_classify_as_recruiter():
    msg = {"from": "Pat Reyes <pat@acme.com>", "subject": "Availability for an interview?",
           "body": "Are you available for an interview with the team next week?"}
    assert detect.classify(msg) == "recruiter"   # interview + available = 2 distinct cues


def test_a_strong_recruiter_phrase_alone_classifies():
    msg = {"from": "Dana <dana@acme.com>", "subject": "Next steps",
           "body": "We reviewed your application and want to move forward."}
    assert detect.classify(msg) == "recruiter"


# ============================ read-only scope + scoped query ============================
def test_gmail_scope_is_read_only():
    assert gmail.SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]
    for s in gmail.SCOPES:                        # substring guard: any write scope trips it
        for writeish in ("send", "compose", "modify", "insert"):
            assert writeish not in s, s


def test_default_query_stays_scoped_read_first():
    q = gmail.DEFAULT_QUERY
    assert q and q.strip()                        # never empty — an empty q would sweep everything
    assert "newer_than" in q                       # time-bounded
    assert "subject:" in q and "from:" in q        # scoped to job-relevant senders/subjects


# ============================ hostile-mail parsing never crashes ============================
def _b64data(text: str) -> str:
    import base64
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_parse_message_tolerates_broken_base64():
    msg = gmail.parse_message({"id": "x", "snippet": "fallback",
                               "payload": {"mimeType": "text/plain", "body": {"data": "!!not b64!!"}}})
    assert msg["body"] == "fallback"               # bad base64 -> graceful snippet fallback


def test_parse_message_reads_an_html_only_message():
    msg = gmail.parse_message({"id": "x", "payload": {
        "mimeType": "text/html", "body": {"data": _b64data("<p>Hello there</p>")}}})
    assert "Hello there" in msg["body"]            # _any_body decodes html-only mail


def test_parse_message_empty_payload_uses_snippet():
    msg = gmail.parse_message({"id": "x", "snippet": "just the snippet", "payload": {}})
    assert msg["body"] == "just the snippet"


def test_parse_message_survives_deeply_nested_mime():
    node = {"mimeType": "text/plain", "body": {"data": _b64data("deep leaf")}}
    for _ in range(3000):                          # a hostile tree well past Python's recursion limit
        node = {"mimeType": "multipart/mixed", "parts": [node]}
    msg = gmail.parse_message({"id": "x", "snippet": "snip", "payload": node})
    assert msg["body"] == "snip"                   # capped -> snippet fallback, NOT a RecursionError/500


# ============================ token/creds stay git-ignored ============================
def test_gmail_token_and_creds_are_git_ignored():
    for f in ("config/gmail_token.json", "config/gmail_credentials.json"):
        r = subprocess.run(["git", "check-ignore", f], cwd=str(ROOT),
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{f} is not git-ignored"


# ============================ complete_verification refuses unsafe links (defense-in-depth) ============================
def test_complete_verification_refuses_non_public_or_non_http_links():
    for bad in ("file:///etc/passwd", "ftp://x/y", "gopher://x/",
                "http://169.254.169.254/latest/meta-data/", "http://localhost:9200/",
                "http://127.0.0.1/x", "http://10.0.0.5/", "http://192.168.1.1/",
                "http://printer.local/"):
        r = gmail.complete_verification(bad, opener=lambda u: 200)
        assert r["ok"] is False and r["status"] == 0, bad


def test_complete_verification_allows_a_public_https_link():
    r = gmail.complete_verification("https://acme.com/verify?t=1", opener=lambda u: 200)
    assert r["ok"] is True and r["status"] == 200
