"""The shared Tavus CVI client (backend/tavus_client.py) and the broker's TavusProvider adapter on
top of it: verify the real request/response handling WITHOUT a live key, using an injected fake
transport (same pattern as TelegramBot/Gmail).

The one thing this cannot check is that Tavus accepts the request for real; that's the live smoke
test with a key. Everything both callers depend on (URLs, the x-api-key header, body shapes for
conversations and PALs, parsing conversation_url/meeting_token/conversation_id, the transcript
events) is covered here.
"""

from __future__ import annotations

import pytest

from backend.provider_tavus import TavusProvider
from backend.tavus_client import (TavusClient, TavusError, parse_transcript_events,
                                  parse_transcript_text)


class FakeHTTP:
    """Records the outbound request and returns canned (status, json) per (method, path suffix)."""
    def __init__(self, response=None, status=200, routes=None):
        self.response, self.status, self.routes = response, status, routes or {}
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        for (m, suffix), (st, data) in self.routes.items():
            if m == method and url.endswith(suffix):
                return st, data
        return self.status, self.response


_OK = {
    "conversation_id": "c123",
    "conversation_url": "https://tavus.daily.co/c123",
    "status": "active",
    "meeting_token": "jwt-abc",
}


def test_create_conversation_hits_the_right_endpoint_with_auth_and_body():
    http = FakeHTTP(_OK)
    out = TavusClient("sk-tavus", http=http).create_conversation(
        face_id="face_x", context="Interview for a data analyst role", max_call_seconds=600,
        greeting="Hello there")
    call = http.calls[0]
    assert call["method"] == "POST" and call["url"] == "https://tavusapi.com/v2/conversations"
    assert call["headers"]["x-api-key"] == "sk-tavus"
    b = call["body"]
    assert b["face_id"] == "face_x" and "pal_id" not in b
    assert b["require_auth"] is True
    assert b["properties"]["max_call_duration"] == 600
    # a generous join window so the room does not end before the person joins
    assert b["properties"]["participant_absent_timeout"] == 300
    assert b["properties"]["participant_left_timeout"] == 90
    assert b["conversational_context"] == "Interview for a data analyst role"
    assert b["custom_greeting"] == "Hello there"
    # response parsed, token appended to the join URL
    assert out == {"conversation_id": "c123", "conversation_url": "https://tavus.daily.co/c123?t=jwt-abc",
                   "meeting_token": "jwt-abc"}


def test_pal_takes_precedence_over_a_bare_face():
    http = FakeHTTP(_OK)
    TavusClient("k", http=http).create_conversation(face_id="face_x", pal_id="p1")
    b = http.calls[0]["body"]
    assert b["pal_id"] == "p1" and "face_id" not in b     # the interviewer brain carries its own face
    assert "conversational_context" not in b and "custom_greeting" not in b


def test_create_pal_end_and_get_conversation_shapes():
    http = FakeHTTP(routes={
        ("POST", "/v2/pals"): (200, {"pal_id": "pal_9"}),
        ("POST", "/v2/conversations/c1/end"): (200, {}),
        ("GET", "/v2/conversations/c1?verbose=true"): (200, {"status": "ended", "events": []}),
    })
    c = TavusClient("k", http=http)
    assert c.create_pal(name="Interviewer", system_prompt="You interview.", face_id="f", greeting="Hi") == "pal_9"
    pal_body = http.calls[0]["body"]
    assert pal_body == {"pal_name": "Interviewer", "system_prompt": "You interview.",
                        "default_face_id": "f", "pipeline_mode": "full", "greeting": "Hi"}
    c.end_conversation("c1")
    assert http.calls[1]["method"] == "POST" and http.calls[1]["body"] is None
    assert c.get_conversation("c1")["status"] == "ended"
    assert c.transcript("c1") == []


def test_errors_surface_as_tavus_error_with_the_status():
    http = FakeHTTP({"message": "Invalid access token"}, status=401)
    with pytest.raises(TavusError) as exc:
        TavusClient("bad", http=http).create_conversation(face_id="f")
    assert exc.value.status == 401 and "Invalid access token" in str(exc.value)
    with pytest.raises(ValueError):
        TavusClient("")
    with pytest.raises(ValueError):
        TavusClient("k", http=http).create_conversation()


def test_transcript_events_are_parsed_into_interviewer_and_candidate_turns():
    events = [
        {"event_type": "system.pal_joined", "timestamp": "t"},
        {"event_type": "application.transcription_ready", "properties": {"transcript": [
            {"role": "system", "content": "ignored"},
            {"role": "assistant", "content": "Tell me about yourself.", "seconds_from_start": 0, "duration": 2},
            {"role": "user", "content": "I led a team of six.", "seconds_from_start": 5, "duration": 4},
            {"role": "user", "content": ""},
        ]}},
    ]
    turns = parse_transcript_events(events)
    assert [(t["role"], t["content"]) for t in turns] == [
        ("interviewer", "Tell me about yourself."), ("candidate", "I led a team of six.")]
    assert turns[1]["seconds_from_start"] == 5


def test_typed_transcript_text_is_parsed_with_or_without_prefixes():
    turns = parse_transcript_text("Interviewer: Why this role?\nMe: Because I like data.\n"
                                  "Me: And the team.\nWhat else?\nI also built dashboards.")
    assert turns == [
        {"role": "interviewer", "content": "Why this role?"},
        {"role": "candidate", "content": "Because I like data. And the team."},
        {"role": "interviewer", "content": "What else?"},
        {"role": "candidate", "content": "I also built dashboards."},
    ]


# ---- the broker adapter --------------------------------------------------------------------
def test_provider_start_session_maps_onto_the_broker_contract():
    http = FakeHTTP(_OK)
    p = TavusProvider("sk-tavus", face_id="face_x", max_call_seconds=600, http=http)
    out = p.start_avatar_session("u1", {"prompt": "Interview for a data analyst role"})
    b = http.calls[0]["body"]
    assert b["face_id"] == "face_x" and b["properties"]["max_call_duration"] == 600
    assert b["conversational_context"] == "Interview for a data analyst role"
    assert b["conversation_name"] == "Tailor mock interview (u1)"
    assert out == {"session_url": "https://tavus.daily.co/c123?t=jwt-abc", "token": "jwt-abc",
                   "provider_session_id": "c123"}


def test_provider_no_context_and_no_token_leaves_url_untouched():
    http = FakeHTTP({"conversation_id": "c9", "conversation_url": "https://t.co/c9"})
    out = TavusProvider("k", face_id="f", http=http).start_avatar_session("u")
    assert "conversational_context" not in http.calls[0]["body"]
    assert out == {"session_url": "https://t.co/c9", "token": "", "provider_session_id": "c9"}


def test_provider_pal_takes_precedence_and_validates_inputs():
    http = FakeHTTP(_OK)
    TavusProvider("k", face_id="face_x", pal_id="pbd76", http=http).start_avatar_session("u")
    b = http.calls[0]["body"]
    assert b["pal_id"] == "pbd76" and "face_id" not in b
    with pytest.raises(ValueError):
        TavusProvider("", face_id="f")
    with pytest.raises(ValueError):
        TavusProvider("k", face_id="")
    with pytest.raises(NotImplementedError):
        TavusProvider("k", face_id="f", http=http).complete("m", "p")
