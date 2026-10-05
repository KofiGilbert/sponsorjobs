"""Hardening of the newer core-loop endpoints (from_cv, auto-apply, apikey) against the
edge cases an adversarial review surfaced: a résumé drop must never wipe a richer saved
profile or its history; odd model shapes must not crash the flatten/merge path; the key
validator must not reject a valid key on a transient Anthropic blip; and the real backend
must read the SAME credentials file the UI saved to."""
from __future__ import annotations

from docx import Document

import llm.anthropic_client as ac
import ui.app as app


# --------------------------------------------------------------------------- helpers
def _cv_docx(tmp_path, text):
    doc = Document()
    for line in text.splitlines():
        doc.add_paragraph(line)
    p = tmp_path / "resume.docx"
    doc.save(str(p))
    return p


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")   # + pytest → FakeLLM stand-in
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(app, "UPLOADS_DIR", tmp_path / "uploads")


# ---------------------------------------------------------- from_cv is non-destructive
def test_from_cv_preserves_existing_profile_and_history(tmp_path, monkeypatch):
    """Dropping a résumé folds INTO the saved profile — it never wipes a role the person
    already curated, nor the conversation history a guided intake built up."""
    _isolate(tmp_path, monkeypatch)
    rich = {"identity": {"name": "Existing Person", "email": "ep@example.com"},
            "experience": [{"org": "Curated Bank", "location": "London",
                            "roles": [{"title": "Analyst", "dates": "2019-2021", "bullets": ["kept"]}]}],
            "summary": "A carefully written summary."}
    history = [{"role": "user", "content": "earlier turn"}]
    app._memory().save("default", rich, app._essentials_from_profile(rich), history)

    cv = _cv_docx(tmp_path, "Sam Rivera\nsam.rivera@example.com\nSoftware Engineer at Acme")
    with open(cv, "rb") as f:
        r = app.app.test_client().post("/api/profile/from_cv", data={"file": (f, "resume.docx")},
                                       content_type="multipart/form-data").get_json()
    assert r["ok"]

    saved = app._memory().load("default")
    orgs = [e.get("org") for e in saved["profile"].get("experience") or []]
    assert "Curated Bank" in orgs                       # the curated role survived the drop
    assert saved["profile"].get("summary") == "A carefully written summary."  # summary kept
    assert saved["history"] == history                  # history was NOT wiped to []


def test_from_cv_first_time_still_populates(tmp_path, monkeypatch):
    """With nothing saved yet, a résumé drop populates the profile as before (no regression)."""
    _isolate(tmp_path, monkeypatch)
    cv = _cv_docx(tmp_path, "Sam Rivera\nsam.rivera@example.com\nSoftware Engineer at Acme")
    with open(cv, "rb") as f:
        r = app.app.test_client().post("/api/profile/from_cv", data={"file": (f, "resume.docx")},
                                       content_type="multipart/form-data").get_json()
    assert r["ok"] and r["summary"]["roles"] >= 1
    assert (app._memory().load("default")["profile"].get("experience") or [])


# ------------------------------------------------------ defensive coercion of odd shapes
def test_coerce_profile_normalizes_bad_shapes():
    weird = {"identity": "just a name string",          # identity as a string
             "experience": [{"org": "Acme", "roles": {"title": "Eng", "dates": "2020"}},  # roles as a dict
                            "not-a-dict",                 # a stray string entry
                            {"org": "Beta", "roles": ["bad", {"title": "Dev"}]}],
             "skills": ["Python", "SQL"],                # skills as a list
             "education": [{"school": "S"}, 7],          # a stray int
             "summary": {"oops": "dict"}}                # summary as a dict
    out = app._coerce_profile(weird)
    assert out["identity"] == {}                          # non-dict identity dropped to {}
    assert out["skills"] == {}                            # non-dict skills dropped to {}
    assert out["summary"] == ""                           # non-str summary dropped to ""
    assert [e["org"] for e in out["experience"]] == ["Acme", "Beta"]   # string entry dropped
    assert out["experience"][0]["roles"] == [{"title": "Eng", "dates": "2020"}]  # dict→[dict]
    assert out["experience"][1]["roles"] == [{"title": "Dev"}]          # bad role filtered
    assert out["education"] == [{"school": "S"}]           # stray int filtered


def test_coerce_profile_on_non_dict_returns_empty():
    assert app._coerce_profile("nonsense") == {}
    assert app._coerce_profile(None) == {}


def test_essentials_from_profile_survives_odd_shapes():
    """The flatten used to crash when roles came back as a dict or an entry as a string."""
    prof = {"identity": {"name": "A"},
            "experience": [{"org": "Acme", "roles": {"title": "Eng", "dates": "2020"}},
                           "garbage",
                           {"org": "Beta"}]}              # no roles → falls back to the entry
    ess = app._essentials_from_profile(app._coerce_profile(prof))
    titles = [e["title"] for e in ess["experience"]]
    assert "Eng" in titles                                # dict-role recovered, no crash


# ------------------------------------------------------------- merge is loss-free
def test_merge_profiles_unions_without_loss():
    base = {"identity": {"name": "Base", "phone": "111"},
            "experience": [{"org": "Acme", "roles": [{"title": "Eng"}]}],
            "skills": {"Computing": "Python"}, "summary": "keep me"}
    new = {"identity": {"name": "New", "email": "new@example.com"},   # fresh résumé identity
           "experience": [{"org": "Acme", "roles": [{"title": "Eng"}]},   # dup — not re-added
                          {"org": "Beta", "roles": [{"title": "Dev"}]}],  # new — added
           "skills": {"Knowledge": "Finance"}, "summary": "discard"}
    out = app._merge_profiles(base, new)
    assert out["identity"]["name"] == "New"               # fresh résumé's non-empty value wins
    assert out["identity"]["email"] == "new@example.com"  # new field added
    assert out["identity"]["phone"] == "111"              # base field kept where new omits it
    assert [e["org"] for e in out["experience"]] == ["Acme", "Beta"]   # union, deduped
    assert out["skills"] == {"Computing": "Python", "Knowledge": "Finance"}  # skills unioned
    assert out["summary"] == "keep me"                    # existing summary preserved


# ---------------------------------------------------- key validator: transient vs auth
class _Boom(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


def _patch_anthropic(monkeypatch, exc):
    """Make _validate_anthropic_key's test call raise `exc`."""
    class _Msgs:
        def create(self, **_):
            raise exc

    class _Client:
        def __init__(self, **_):
            self.messages = _Msgs()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _Client)


def test_validate_accepts_key_on_transient_overload(monkeypatch):
    _patch_anthropic(monkeypatch, _Boom("overloaded_error", status_code=529))
    ok, msg = app._validate_anthropic_key("sk-ant-valid")
    assert ok is True and msg == ""          # a busy service must not reject a valid key


def test_validate_accepts_key_on_rate_limit(monkeypatch):
    _patch_anthropic(monkeypatch, _Boom("rate limit exceeded", status_code=429))
    assert app._validate_anthropic_key("sk-ant-valid")[0] is True


def test_validate_rejects_bad_key_on_401(monkeypatch):
    _patch_anthropic(monkeypatch, _Boom("authentication_error: invalid x-api-key", status_code=401))
    ok, msg = app._validate_anthropic_key("sk-ant-bad")
    assert ok is False and "rejected" in msg


def test_validate_flags_billing_on_402(monkeypatch):
    _patch_anthropic(monkeypatch, _Boom("credit balance too low", status_code=402))
    ok, msg = app._validate_anthropic_key("sk-ant-nocredit")
    assert ok is False and "credit" in msg.lower()


def test_validate_rejects_obvious_non_key():
    assert app._validate_anthropic_key("nope")[0] is False   # no network — fails the sk- check


# ------------------------------------------------ real backend reads the UI's cred file
def test_load_key_honors_cred_file_env(tmp_path, monkeypatch):
    """The regression: the UI saved the key to RESUME_AGENT_CRED_FILE, but the real backend
    read a CWD-relative path and never saw it. Both must resolve to the same file now."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cred = tmp_path / "credentials.env"
    cred.write_text('ANTHROPIC_API_KEY="sk-ant-fromfile"\nTELEGRAM_BOT_TOKEN=x\n', encoding="utf-8")
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(cred))
    assert ac._load_key() == "sk-ant-fromfile"
    assert ac._cred_file() == cred                          # same resolution the UI uses


def test_load_key_prefers_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fromenv")
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "nope.env"))
    assert ac._load_key() == "sk-ant-fromenv"               # env wins, file not required
