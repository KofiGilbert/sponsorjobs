"""The SponsorJobs Telegram chat (2026-10-30, @SponsorJobAIBot): what the owner met, pinned.

A. A reply that came after the apply agent's ask stopped waiting ("1.Male 2.African 3.No
   Disability 4.Not a veteran", after the 5 minute wait and an app restart) is read with the same
   reader and saved to the answer library as the person's own; the reply says what was saved and
   that the application already stopped. Nothing answered: it goes on to the chat.
B. The free chat is an assistant for the app that knows a short local summary of the person
   (never the self-identification values), and never claims it saved anything.
C. A resume sent as a file or photo travels to the app and goes through the same import as the
   in-app upload; the reply says what the import really did, or plainly what failed. Voice notes,
   other file types and files over 20 MB get an honest reply. (The official bot's webhook and
   pass-through route: worker/test/telegram.test.ts and tests/test_telegram_relay.py.)

Offline: FakeLLM and stub models are test doubles for the plumbing; the rules that protect the
person (what is saved, what reaches the model, what the reply may claim) are checked directly.
"""
from __future__ import annotations

import importlib
import json
import threading
import time

import pytest

from autoapply import asks as AK
from llm.base import FakeLLM
from notify import assistant as AS
from notify import channel as CH
from notify.channel import ChannelError, OfficialBotChannel, OwnBotChannel, parse_update
from notify.hub import NotifyHub

H = {"X-Tailor-Extension": "1"}
GENDER = ["Male", "Female", "I don't wish to answer"]
ETHNICITY = ["Asian", "Black or African American", "White", "Hispanic or Latino", "I don't wish to answer"]
DISABILITY = ["Yes, I have a disability, or have had one in the past",
              "No, I do not have a disability and have not had one in the past", "I do not want to answer"]
VETERAN = ["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran",
           "I don't wish to answer"]
EEO_QS = [{"question": "Gender", "options": GENDER}, {"question": "Ethnicity", "options": ETHNICITY},
          {"question": "Disability status", "options": DISABILITY},
          {"question": "Veteran status", "options": VETERAN}]
OWNER_REPLY = "1.Male 2.African 3.No Disability 4.Not a veteran"


class Reader:
    """A stub reply reader standing in for the real model: it maps the owner's reply to the
    exact choices, as the reader's prompt tells the model to."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = 0

    def ask_reply(self, system, user):
        self.calls += 1
        return json.dumps({"answers": [{"n": n, "answer": a} for n, a in self.answers], "question": "",
                           "unclear": not self.answers})


@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    mod.DB_PATH = str(tmp_path / "m.db")
    mod.PALACE_DIR = tmp_path / "palace"
    sent = []

    class Chan:
        def send(self, text, buttons=None):
            sent.append((text, buttons))
    monkeypatch.setattr(mod, "_active_channel", lambda: Chan())
    with mod._ASKS_LOCK:
        mod._ASKS.clear()
    return mod, sent


def _lib(mod, q):
    lib = mod._answer_lib()
    try:
        return lib.lookup(q)
    finally:
        lib.close()


def _expired_ask(mod, aid, questions, *, role="Data Engineer", company="Databricks", ended_ago=60):
    """An ask Telegram received that already stopped waiting, as the registry keeps it."""
    mod._recent_ask_add(aid, [dict(q, all_options=q.get("options") or []) for q in questions],
                        role=role, company=company, timeout=300)
    mod._recent_ask_end(aid, [])
    rows = mod._recent_asks_load()
    for r in rows:
        if r["id"] == aid:
            r["ended_at"] = time.time() - ended_ago
    mod._recent_asks_save(rows)


# --------------------------------------------------------------------------- A: late answers
def test_a_late_reply_after_the_wait_ran_out_is_saved_as_the_persons_own(A, monkeypatch):
    """The whole path: the ask goes out, nobody answers in time, the reply comes later."""
    mod, sent = A
    monkeypatch.setattr(mod, "_make_llm", lambda: Reader([(1, "Male"), (2, "Black or African American"),
                                                          (3, DISABILITY[1]), (4, VETERAN[0])]))
    recs = mod.CVRecords(mod.DB_PATH)
    rid = recs.add("Data Engineer", "Databricks", 70.0, "", data={"status": "ready"})
    recs.close()
    r = mod.app.test_client().post("/api/autoapply/agent/ask", json={
        "id": "eeo1", "questions": EEO_QS, "timeout_s": 5, "record_id": rid}, headers=H).get_json()
    assert r["reason"] == "timeout"
    assert "Reply in one message" in sent[0][0]

    reply = mod._agent_ask_event("text", OWNER_REPLY)
    assert reply.startswith("Saved for next time: Gender; Ethnicity; Disability status; Veteran status.")
    assert "Data Engineer at Databricks already stopped" in reply
    assert "Your answers" in reply
    for value in ("Male", "African", "disability and", "protected veteran"):   # EEO values never echoed
        assert value not in reply
    assert _lib(mod, "What is your gender?")["answer"] == "Male"
    assert _lib(mod, "What is your gender?")["source"] == "you"
    assert _lib(mod, "Ethnicity")["answer"] == "Black or African American"
    assert _lib(mod, "Veteran status")["answer"] == VETERAN[0]
    # Saved once: the same message again is no longer a late answer (it goes on to the chat).
    assert mod._agent_ask_event("text", OWNER_REPLY) is None


def test_a_the_record_survives_an_app_restart(A, monkeypatch):
    mod, _sent = A
    _expired_ask(mod, "r1", EEO_QS[:1])
    importlib.reload(mod)                          # the app restarted: _ASKS is empty again
    monkeypatch.setattr(mod, "_make_llm", lambda: FakeLLM())
    reply = mod._agent_ask_event("text", "1. Male")
    assert reply and reply.startswith("Saved for next time: Gender.")
    assert _lib(mod, "Gender")["answer"] == "Male"


def test_a_only_what_was_answered_is_saved_and_the_rest_is_named(A, monkeypatch):
    mod, _sent = A
    _expired_ask(mod, "p1", EEO_QS + [{"question": "Desired salary", "options": []}])
    monkeypatch.setattr(mod, "_make_llm", lambda: Reader([(1, "Female"), (5, "120000")]))
    reply = mod._agent_ask_event("text", "1. Female\n5. 120000")
    assert reply.startswith("Saved for next time: Gender; Desired salary: 120000.")
    assert "Not saved yet: Ethnicity; Disability status; Veteran status." in reply
    assert _lib(mod, "Desired salary")["answer"] == "120000"


def test_a_a_message_that_answers_nothing_goes_on_to_the_chat(A, monkeypatch):
    mod, _sent = A
    _expired_ask(mod, "n1", EEO_QS)
    reader = Reader([])
    monkeypatch.setattr(mod, "_make_llm", lambda: reader)
    assert mod._agent_ask_event("text", "what jobs did you find today") is None
    assert reader.calls == 1                       # read with the same reader first
    assert _lib(mod, "Gender") is None


def test_a_a_late_reply_needs_more_than_a_live_one():
    """A late message may be about anything: a free text answer to a non-EEO question is never
    taken unless the person numbered it, and a choice only from a short reply."""
    items = [{"n": 1, "question": "Years of experience?", "options": []}]
    model = Reader([(1, "hello there")])
    assert AK.read_late(model, items, "hello there")["answers"] == {}
    assert AK.read_late(Reader([(1, "5")]), items, "1. 5")["answers"] == {0: "5"}
    g = [{"n": 1, "question": "Gender", "options": GENDER, "all_options": GENDER}]
    assert AK.read_late(Reader([(1, "Male")]), g, "Male")["answers"] == {0: "Male"}
    long = "my male friend asked me about the job, " * 8
    assert AK.read_late(Reader([(1, "Male")]), g, long)["answers"] == {}
    free = [{"n": 1, "question": "Gender", "options": []}]
    assert AK.read_late(Reader([(1, "Male")]), free, "male")["answers"] == {0: "Male"}
    assert AK.read_late(Reader([(1, "sure thing")]), free, "sure thing")["answers"] == {}


def test_a_the_window_is_a_day(A, monkeypatch):
    mod, _sent = A
    _expired_ask(mod, "old", EEO_QS[:1], ended_ago=25 * 3600)
    monkeypatch.setattr(mod, "_make_llm", lambda: FakeLLM())
    assert mod._agent_ask_event("text", "1. Male") is None
    assert _lib(mod, "Gender") is None


def test_a_a_late_tap_on_the_asks_button_is_saved_too(A):
    mod, _sent = A
    _expired_ask(mod, "b1", EEO_QS[:1])
    reply = mod._agent_ask_event("button", "ask:b1:1")
    assert reply.startswith("Saved for next time: Gender.") and "Female" not in reply
    assert _lib(mod, "Gender")["answer"] == "Female"
    assert mod._agent_ask_event("button", "ask:zz:0") == "That question is no longer open."


def test_a_an_open_ask_still_takes_the_reply_live(A):
    mod, _sent = A
    _expired_ask(mod, "x1", EEO_QS[:1])
    out = {}
    th = threading.Thread(target=lambda: out.update(r=mod.app.test_client().post(
        "/api/autoapply/agent/ask", json={"id": "live", "question": "Ethnicity", "options": ETHNICITY,
                                          "timeout_s": 15}, headers=H).get_json()))
    th.start()
    for _ in range(50):
        with mod._ASKS_LOCK:
            if mod._ASKS.get("live", {}).get("sent"):
                break
        time.sleep(0.1)
    assert mod._agent_ask_event("text", "Asian") == "Got it: Asian."
    th.join(20)
    assert out["r"]["answer"] == "Asian"
    assert _lib(mod, "Gender") is None             # the older, expired ask was not touched
    assert [r["id"] for r in mod._recent_asks_load()] == ["x1"]   # an answered ask is not kept


# --------------------------------------------------------------------------- B: the assistant
class Recorder(FakeLLM):
    """FakeLLM with a reply that lies the way the old chat did, recording the prompt."""

    def __init__(self, reply="Thanks, I've noted those four answers. I'll keep them on file for the forms."):
        super().__init__()
        self.reply, self.prompts = reply, []

    def app_assistant(self, system, user):
        self.prompts.append((system, user))
        return self.reply


PROFILE = {"identity": {"name": "Kofi Gilbert"},
           "experience": [{"org": "Black Origin", "roles": [{"title": "Data Engineer", "dates": "2024 - Present"}]}],
           "education": [{"school": "Howard University", "degree": "B.S. Computer Science"}],
           "skills": {"Data": "Python, SQL, Spark"}}


@pytest.fixture()
def known(A, monkeypatch):
    mod, _sent = A
    monkeypatch.setattr(mod, "_saved_full_profile", lambda: PROFILE)
    monkeypatch.setattr(mod, "_work_auth", lambda: {"countries": {"US": {
        "status": "f1_opt", "authorized": "Yes", "sponsor_now": "No", "sponsor_future": "Yes", "auth_end": ""}},
        "relocate": "Yes"})
    lib = mod._answer_lib()
    lib.save("Gender", "Male", "you")
    lib.save("Are you a protected veteran?", "I am not a protected veteran", "you")
    lib.save("Desired salary", "120000", "you")
    lib.close()
    recs = mod.CVRecords(mod.DB_PATH)
    recs.add("Senior Data Engineer", "Databricks", 80.0, "", data={})
    recs.close()
    rec = Recorder()
    monkeypatch.setattr(mod, "_make_llm", lambda: rec)
    return mod, rec


def test_b_the_prompt_knows_the_person_and_never_an_eeo_value(known):
    mod, rec = known
    mod._palace().remember_turn("user", "1.Male 2.African 3.No Disability 4.Not a veteran")
    mod._annalisa_reply("what do you know about me?")
    system, user = rec.prompts[0]
    for fact in ("Kofi Gilbert", "Data Engineer at Black Origin", "Howard University", "Spark",
                 "Senior Data Engineer at Databricks", "Desired salary", "Gender: saved", "Veteran status: saved",
                 "Ethnicity: not saved", "F-1 OPT", "needs sponsorship later: Yes"):
        assert fact in user, fact
    assert "Male" not in user and "protected veteran" not in user and "African" not in user
    assert "120000" not in user                    # answer values are not sent, only which exist
    assert "cannot save" in system and "Never start a resume interview" in system


def test_b_the_reply_never_claims_a_save_the_chat_did_not_make(known):
    mod, rec = known
    reply = mod._annalisa_reply("I worked at Stripe from 2021 to 2023 as an analyst")
    assert not AS.claims_a_save(reply)
    low = reply.lower()
    assert "saved" not in low and "noted" not in low and "on file" not in low
    assert "Your answers" in reply and "My profile" in reply and "Send your resume here" in reply
    # The turn is still kept in the diary for the history.
    joined = " ".join(t["content"] for t in mod._palace().load_history())
    assert "Stripe from 2021" in joined


def test_b_a_message_with_eeo_answers_never_reaches_the_model(known):
    mod, rec = known
    reply = mod._annalisa_reply(OWNER_REPLY)
    assert rec.prompts == []
    assert reply == AS.EEO_POINTER and "saved" not in reply.lower()


def test_b_the_chat_through_the_service_says_where_things_go(known):
    from notify.service import NotifyService
    mod, rec = known
    svc = NotifyService(bot=None, actions=mod._RecordActions())
    reply = svc.handle_command("Here are my answers, please keep them")
    assert not AS.claims_a_save(reply) and "Your answers" in reply


@pytest.mark.parametrize("text, claims", [
    ("Thanks, I've noted those four answers.", True),
    ("I'll keep them on file for the application forms.", True),
    ("Got it, I'll remember that.", True),
    ("Your answers are saved.", True),
    ("I have updated your resume.", True),
    ("I can't save anything from this chat.", False),
    ("SponsorJobs has your gender answer.", False),
    ("I can help you add it under Resumes.", False),
    ("You have 3 resumes: Data Engineer at Databricks.", False),
])
def test_b_claims(text, claims):
    assert AS.claims_a_save(text) is claims
    assert not AS.claims_a_save(AS.honest(text))
    assert AS.honest(text)


def test_b_fake_llm_greets_by_name():
    user = AS.build_user(AS.summary(PROFILE, ["Data Engineer at Databricks"]), [], "my resumes?")
    out = FakeLLM().app_assistant(AS.SYSTEM, user)
    assert out.startswith("Hi Kofi.") and "Databricks" in out


# --------------------------------------------------------------------------- C: files
DOC = {"file_id": "D1", "name": "Kofi Gilbert_Resume.pdf", "mime": "application/pdf", "size": 800, "photo": False}


class FileChan:
    """A channel that hands over the bytes of the file the person sent."""
    name = "own"

    def __init__(self, blob=b"%PDF bytes", error=None):
        self.blob, self.error, self.sent, self.downloads = blob, error, [], []

    def send(self, text, buttons=None):
        self.sent.append(text)

    def ack(self, ev, text=""):
        pass

    def download(self, file_id):
        self.downloads.append(file_id)
        if self.error:
            raise ChannelError(self.error)
        return self.blob


class Cmds:
    def __init__(self):
        self.said = []

    def handle_command(self, text):
        self.said.append(text)
        return "ok"


def test_c_own_bot_files_become_file_events_with_the_file_and_caption():
    photo = {"update_id": 5, "message": {"chat": {"id": 42}, "date": 1, "photo": [
        {"file_id": "S", "width": 90, "height": 90}, {"file_id": "B", "width": 1200, "height": 1600},
        {"file_id": "M", "width": 320, "height": 400}]}}
    doc = {"update_id": 6, "message": {"chat": {"id": 42}, "caption": "here", "document": {
        "file_id": "D", "file_name": "cv.docx", "mime_type": "application/x", "file_size": 5}}}
    ev = parse_update(photo, "42")
    assert ev["type"] == "file" and ev["file"]["file_id"] == "B" and ev["file"]["photo"] and ev["text"] == ""
    ev = parse_update(doc, "42")
    assert ev["file"] == {"file_id": "D", "name": "cv.docx", "mime": "application/x", "size": 5, "photo": False}
    assert ev["text"] == "here"
    assert parse_update(doc, "7") is None                 # never from anyone but the owner
    voice = parse_update({"update_id": 7, "message": {"chat": {"id": 42}, "voice": {"file_id": "x"}}}, "42")
    assert voice["type"] == "file" and voice["file"] is None


def test_c_the_hub_imports_a_resume_and_replies_with_what_the_import_did():
    got = []
    hub = NotifyHub(state=None, engine=None, commands=Cmds(),
                    file_import=lambda meta, blob: got.append((meta, blob)) or "Got it: 2 roles read.")
    ch = FileChan()
    assert hub.handle_event(ch, {"type": "file", "text": "", "file": DOC}) == "Got it: 2 roles read."
    assert ch.downloads == ["D1"] and got == [(DOC, b"%PDF bytes")] and ch.sent == ["Got it: 2 roles read."]


def test_c_a_caption_is_handled_as_a_message_after_the_import():
    cmds = Cmds()
    hub = NotifyHub(state=None, engine=None, commands=cmds, file_import=lambda m, b: "Imported.")
    ch = FileChan()
    hub.handle_event(ch, {"type": "file", "text": "tailor this for Stripe", "file": DOC})
    assert ch.sent == ["Imported.", "ok"] and cmds.said == ["tailor this for Stripe"]


@pytest.mark.parametrize("f, reply", [
    (None, CH.MSG_NO_MEDIA),
    ({"file_id": "Z", "name": "x.zip", "mime": "application/zip", "size": 9}, CH.MSG_FILE_TYPE),
    ({"file_id": "H", "name": "cv.pdf", "mime": "application/pdf", "size": 21 * 1024 * 1024}, CH.MSG_TOO_BIG),
])
def test_c_what_is_not_a_resume_gets_an_honest_reply_and_is_never_fetched(f, reply):
    imported = []
    hub = NotifyHub(state=None, engine=None, commands=Cmds(), file_import=lambda m, b: imported.append(m))
    ch = FileChan()
    assert hub.handle_event(ch, {"type": "file", "text": "", "file": f}) == reply
    assert ch.downloads == [] and imported == [] and ch.sent == [reply]


def test_c_a_failed_download_or_import_says_so():
    hub = NotifyHub(state=None, engine=None, commands=Cmds(), file_import=lambda m, b: "never")
    assert hub.handle_event(FileChan(error="telegram_unavailable"), {"type": "file", "file": DOC}) == CH.MSG_DOWNLOAD_FAILED
    assert hub.handle_event(FileChan(error="too_big"), {"type": "file", "file": DOC}) == CH.MSG_TOO_BIG

    def boom(m, b):
        raise RuntimeError("disk full")
    hub = NotifyHub(state=None, engine=None, commands=Cmds(), file_import=boom)
    assert hub.handle_event(FileChan(), {"type": "file", "file": DOC}) == CH.MSG_IMPORT_FAILED
    for m in (CH.MSG_NO_MEDIA, CH.MSG_FILE_TYPE, CH.MSG_TOO_BIG, CH.MSG_DOWNLOAD_FAILED, CH.MSG_IMPORT_FAILED):
        assert not AS.claims_a_save(m) and "\u2014" not in m and "\u2013" not in m


def test_c_official_channel_reads_file_events_and_downloads_through_the_broker():
    calls = []

    def get(path):
        return 200, {"events": [{"cursor": 9, "type": "file", "data": "FID", "at": "x", "text": json.dumps(
            {"name": "cv.pdf", "mime": "application/pdf", "size": 77, "caption": "for Stripe"})}], "cursor": 9}

    def fetch(path, body):
        calls.append((path, body))
        return (200, b"bytes") if body["file_id"] == "FID" else (404, None)
    store = {}
    ch = OfficialBotChannel(get, lambda p, b: (200, {}), lambda: store.get("c", ""), lambda c: store.update(c=c),
                            fetch_file=fetch)
    ev = ch.poll()[0]
    assert ev["type"] == "file" and ev["text"] == "for Stripe"
    assert ev["file"] == {"file_id": "FID", "name": "cv.pdf", "mime": "application/pdf", "size": 77, "photo": False}
    assert ch.download("FID") == b"bytes" and calls == [("/notify/telegram/file", {"file_id": "FID"})]
    with pytest.raises(ChannelError) as e:
        ch.download("OTHER")
    assert e.value.code == "not_found"


def test_c_own_bot_channel_downloads_with_getfile():
    from notify.telegram import TelegramBot
    seen = []

    def http(url, payload=None):
        seen.append((url.rsplit("/", 1)[-1], payload))
        return {"ok": True, "result": {"file_path": "documents/file_3.pdf", "file_size": 5}}
    bot = TelegramBot("TOKEN", chat_id="42", http=http, fetch=lambda url: seen.append(url) or b"hello")
    ch = OwnBotChannel(bot, lambda: 0, lambda o: None)
    assert ch.download("F9") == b"hello"
    assert seen == [("getFile", {"file_id": "F9"}), "https://api.telegram.org/file/botTOKEN/documents/file_3.pdf"]
    big = TelegramBot("T", chat_id="42", http=lambda u, p=None: {"ok": True, "result": {
        "file_path": "x", "file_size": 30 * 1024 * 1024}}, fetch=lambda url: b"")
    with pytest.raises(ChannelError) as e:
        OwnBotChannel(big, lambda: 0, lambda o: None).download("F")
    assert e.value.code == "too_big"


# --- the app's import: the same one as the in-app upload, and an honest reply from code
RESUME_TXT = "Kofi Gilbert\nkofi@example.com\nData Engineer, Black Origin, 2024 to now\n"


def _txt(mod, monkeypatch, llm=None):
    monkeypatch.setattr(mod, "_make_llm", lambda: llm or FakeLLM())
    monkeypatch.setattr(mod, "_profile_missing_slots", lambda include_soft=True: [])
    monkeypatch.setattr(mod, "_autofill_saved_project_dates", lambda: [])


def test_c_app_import_saves_the_profile_and_says_what_it_read(A, monkeypatch):
    mod, _sent = A
    _txt(mod, monkeypatch)
    calls = []
    real = mod._import_resume_file
    monkeypatch.setattr(mod, "_import_resume_file", lambda p, **k: calls.append(p) or real(p, **k))
    meta = {"file_id": "F", "name": "Kofi Gilbert_Resume.txt", "mime": "text/plain", "size": 60}
    reply = mod._telegram_file_import(meta, RESUME_TXT.encode())
    assert reply == ("Got Kofi Gilbert_Resume.txt. I read 1 role, 1 school and 7 skills and added them "
                     "to your profile. Open SponsorJobs, My profile to see it.")
    assert len(calls) == 1 and calls[0].read_text() == RESUME_TXT     # kept with the uploads
    assert calls[0].parent == mod.UPLOADS_DIR
    saved = mod._memory().load("default")["profile"]
    assert saved["identity"]["name"] == "Kofi Gilbert" and saved["experience"]


def test_c_app_import_names_what_is_still_missing(A, monkeypatch):
    mod, _sent = A
    _txt(mod, monkeypatch)
    monkeypatch.setattr(mod, "_profile_missing_slots", lambda include_soft=True: [
        {"q": "the dates for Portfolio Site"}, {"q": "your phone"}])
    reply = mod._telegram_file_import({"file_id": "F", "name": "cv.txt", "size": 9}, RESUME_TXT.encode())
    assert reply.endswith("It still needs 2 details, like the dates for Portfolio Site: open SponsorJobs, "
                          "Auto-apply, Add the details to finish.")


def test_c_app_import_failures_are_said_plainly(A, monkeypatch):
    import intake.cv_import as CI
    mod, _sent = A
    _txt(mod, monkeypatch)
    assert mod._telegram_file_import({"name": "empty.txt"}, b"   ") == \
        "I could not read any text from empty.txt. A PDF or Word file works best."
    monkeypatch.setattr(CI, "ocr_available", lambda: True)
    monkeypatch.setattr(CI, "extract_text", lambda p: "")
    assert mod._telegram_file_import({"name": "photo.jpg", "mime": "image/jpeg", "photo": True}, b"jpg") == \
        "I could not read text from that photo. A PDF or Word file works best."
    monkeypatch.setattr(CI, "ocr_available", lambda: False)
    assert "can't read text from pictures yet" in mod._telegram_file_import(
        {"name": "photo.jpg", "mime": "image/jpeg", "photo": True}, b"jpg")
    assert mod._telegram_file_import({"name": "old.doc"}, b"x").startswith("I can't read Word 97 (.doc) files yet.")

    def down():
        raise mod.Unavailable("no_key", "no key")
    monkeypatch.setattr(mod, "_make_llm", down)
    monkeypatch.setattr(CI, "extract_text", lambda p: RESUME_TXT)
    reply = mod._telegram_file_import({"name": "cv.txt"}, RESUME_TXT.encode())
    assert reply.startswith("I got cv.txt but could not read it: SponsorJobs has no working AI key.")
    assert mod._memory().load("default") in (None, {}) or not (mod._memory().load("default") or {}).get("profile")


def test_c_the_whole_path_from_a_telegram_file_to_the_profile(A, monkeypatch):
    mod, _sent = A
    _txt(mod, monkeypatch)
    cmds = Cmds()
    hub = NotifyHub(state=None, engine=None, commands=cmds, file_import=mod._telegram_file_import)
    ch = FileChan(blob=RESUME_TXT.encode())
    hub.handle_event(ch, {"type": "file", "text": "is this enough?", "file": {
        "file_id": "D1", "name": "resume.txt", "mime": "text/plain", "size": 60, "photo": False}})
    assert ch.sent[0].startswith("Got resume.txt. I read 1 role") and ch.sent[1] == "ok"
    assert cmds.said == ["is this enough?"]
    assert (mod._memory().load("default") or {}).get("profile", {}).get("identity", {}).get("name") == "Kofi Gilbert"


def test_c_the_assistant_knows_files_can_be_sent_here():
    assert "Files and photos CAN be sent in this chat" in AS.SYSTEM
    assert "Never tell them to upload it somewhere else" in AS.SYSTEM
