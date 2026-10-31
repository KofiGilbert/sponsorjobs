"""The memory page, "What SponsorJobs knows about you" (docs/applicant-brain.md, section 13,
Phase 6). It replaced the profile form and Your answers, so it keeps every route they used:

* the saved identity powers autofill, every resume letterhead and cover letters, so an edit lands
  in BOTH essentials.identity and profile.identity, keeping the history and the resume sections
  (from tests/test_profile_screen.py);
* the section edits write both saved bags and refuse to race a live session (from
  tests/test_profile_editor.py);
* the page itself: every fact grouped with where it came from and when; self-identification
  values never in the page until Show; Forget clears every copy (and the chat lines, when asked);
  the chat's "I noted" proposals saved only on a tap; a career entry forgotten everywhere; the
  history; Export as one zip.

Offline: FakeLLM and a throwaway data folder.
"""

from __future__ import annotations

import importlib
import io
import json
import zipfile

import pytest

import ui.app as app
from applicant import migrate as M
from intake.memory import ConversationMemory


def _seed(tmp_path, monkeypatch):
    db = str(tmp_path / "m.db")
    monkeypatch.setattr(app, "DB_PATH", db)
    app._SESSION.pop("s", None)   # no active builder session
    ident = {"name": "Old Name", "email": "old@example.com"}
    m = ConversationMemory(db)
    m.save("default",
           {"identity": dict(ident), "skills": {"Computing": "Python, SQL"},
            "experience": [{"org": "Acme", "location": "Chicago", "dates": "2021",
                            "roles": [{"title": "Engineer", "dates": "2021",
                                       "bullets": ["Built things."]}]}],
            "education": [{"school": "Northwestern", "degree": "M.S.", "date": "2018",
                           "location": "", "courses": ""}],
            "interests": "hiking, chess"},
           {"identity": dict(ident), "education": [], "experience": []},
           [{"role": "user", "content": "hi"}])
    m.close()
    return db


def test_profile_view_returns_identity_and_sections(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    d = app.app.test_client().get("/api/profile").get_json()
    assert d["has_profile"] is True
    assert d["identity"]["name"] == "Old Name" and d["identity"]["email"] == "old@example.com"
    assert d["identity"]["blog"] == ""                       # missing keys default to ""
    assert d["skills"] == {"Computing": "Python, SQL"}
    assert d["experience"][0]["org"] == "Acme"
    assert d["education"][0]["school"] == "Northwestern"
    assert d["interests"] == "hiking, chess"


def test_identity_save_updates_both_blobs_and_autofill(tmp_path, monkeypatch):
    db = _seed(tmp_path, monkeypatch)
    client = app.app.test_client()
    r = client.post("/api/profile/identity", json={
        "name": "New Name", "email": "new@example.com", "phone": "(312) 555-0001",
        "address": "1 Main St, Chicago, IL 60601",
        "linkedin": "https://linkedin.com/in/new", "github": "https://github.com/new",
        "blog": ""})
    assert r.get_json()["ok"] is True

    saved = ConversationMemory(db).load("default")
    # Written to BOTH blobs so autofill AND the letterhead see it.
    assert saved["profile"]["identity"]["name"] == "New Name"
    assert saved["essentials"]["identity"]["name"] == "New Name"
    # Read-modify-write preserved history and the CV sections.
    assert saved["history"] == [{"role": "user", "content": "hi"}]
    assert saved["profile"]["experience"][0]["org"] == "Acme"
    assert saved["profile"]["skills"] == {"Computing": "Python, SQL"}

    # Autofill (reads essentials.identity) reflects the edit and derives its parts.
    # (autofill is extension-only, so send the extension header.)
    f = client.get("/api/profile/autofill",
                   headers={"X-Tailor-Extension": "1"}).get_json()["fields"]
    assert f["first_name"] == "New" and f["last_name"] == "Name"
    assert f["email"] == "new@example.com"
    assert f["city"] == "Chicago" and f["state"] == "IL" and f["zip"] == "60601"


def test_profile_view_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "empty.db"))
    app._SESSION.pop("s", None)
    d = app.app.test_client().get("/api/profile").get_json()
    assert d["has_profile"] is False
    assert d["identity"]["name"] == "" and d["experience"] == []


def test_identity_save_creates_profile_when_none(tmp_path, monkeypatch):
    db = str(tmp_path / "fresh.db")
    monkeypatch.setattr(app, "DB_PATH", db)
    app._SESSION.pop("s", None)
    app.app.test_client().post("/api/profile/identity",
                               json={"name": "First Timer", "email": "ft@example.com"})
    saved = ConversationMemory(db).load("default")
    assert saved["profile"]["identity"]["name"] == "First Timer"
    assert saved["essentials"]["identity"]["name"] == "First Timer"


# ---------------------------------------------------------------- the section edits (was test_profile_editor.py)
def _mem(monkeypatch, tmp_path=None):
    mem = ConversationMemory(":memory:")
    monkeypatch.setattr(app, "_memory", lambda: mem)
    if tmp_path is not None:                     # the section edits also land in the brain beside DB_PATH
        monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "resume_agent.db"))
    return mem


EDIT = {
    "summary": "Operations leader with real scale numbers.",
    "experience": [
        {"org": "Ampsel Commodities Limited", "title": "Supply Chain Operations Manager",
         "dates": "June 2020 - Sept 2024", "location": "Accra, GH",
         "bullets": ["Exported over $60 million in six months."]},
        {"org": "Viva Technologies", "title": "Operations Lead",
         "dates": "Sept 2016 - Jan 2018", "location": "Accra, GH",
         "bullets": ["Led a fleet of 250 drivers."]},
    ],
    "education": [{"school": "DePaul University", "degree": "MBA - Business Analytics (STEM)",
                   "date": "Sept 2024 - Dec 2025", "location": "Chicago, IL",
                   "courses": "Business Analytics Tools, Data Visualization"}],
    "projects": [{"org": "Warehouse Safety Benchmark",
                  "link": "https://github.com/x/warehouse", "location": "Python, pandas",
                  "dates": "", "bullets": ["Benchmarked 30,000+ warehouses."]}],
    "extracurricular": [{"title": "Team Captain, Intramural Soccer", "date": "",
                         "bullets": []}],
    "interests": "Competitive soccer and basketball.",
    "skills_input": ["team leadership", "dispatch operations"],
    "declined": ["blog", "not-a-real-key"],
}


def test_sections_roundtrip_writes_both_bags(monkeypatch, tmp_path):
    mem = _mem(monkeypatch, tmp_path)
    monkeypatch.setitem(app._SESSION, "s", None)
    client = app.app.test_client()

    r = client.post("/api/profile/sections", json=EDIT)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["summary"] == EDIT["summary"]
    assert body["skills_input"] == EDIT["skills_input"]
    assert body["declined"] == ["blog"]                     # unknown keys dropped

    saved = mem.load("default")
    ess, prof = saved["essentials"], saved["profile"]
    # essentials: flat rows
    assert ess["experience"][0]["title"] == "Supply Chain Operations Manager"
    assert ess["experience"][0]["bullets"] == ["Exported over $60 million in six months."]
    # profile: grouped org -> roles (the renderer's shape)
    assert prof["experience"][0]["org"] == "Ampsel Commodities Limited"
    assert prof["experience"][0]["roles"][0]["dates"] == "June 2020 - Sept 2024"
    # projects carry the personal flag and the link verbatim (obs #8)
    assert prof["projects"][0]["personal"] is True
    assert prof["projects"][0]["link"] == "https://github.com/x/warehouse"
    # both bags agree on the shared sections
    for bag in (ess, prof):
        assert bag["summary"] == EDIT["summary"]
        assert bag["interests"] == EDIT["interests"]
        assert bag["extracurricular"][0]["title"] == "Team Captain, Intramural Soccer"

    # GET reflects the same state
    g = client.get("/api/profile").get_json()
    assert g["summary"] == EDIT["summary"]
    assert g["experience"][0]["org"] == "Ampsel Commodities Limited"


def test_sections_save_refuses_while_a_session_is_open(monkeypatch):
    _mem(monkeypatch)
    monkeypatch.setitem(app._SESSION, "s", object())        # a live CV session
    client = app.app.test_client()
    r = client.post("/api/profile/sections", json=EDIT)
    assert r.status_code == 409
    assert "autosave" in r.get_json()["error"]


def test_empty_rows_are_dropped_not_saved(monkeypatch, tmp_path):
    mem = _mem(monkeypatch, tmp_path)
    monkeypatch.setitem(app._SESSION, "s", None)
    client = app.app.test_client()
    r = client.post("/api/profile/sections", json={
        "experience": [{"org": "", "title": "", "dates": "", "location": "",
                        "bullets": []}],
        "education": [], "projects": [], "extracurricular": [],
        "summary": "", "interests": "", "skills_input": [], "declined": [],
    })
    assert r.status_code == 200
    saved = mem.load("default")
    assert saved["essentials"]["experience"] == []
    assert saved["profile"]["experience"] == []


# ---------------------------------------------------------------- the memory page itself
PROFILE = {"identity": {"name": "Ama Mensah", "email": "ama@example.com", "phone": "312-555-0101"},
           "summary": "Bank data analyst.",
           "experience": [{"org": "Stanbic Bank", "location": "Accra, GH",
                           "roles": [{"title": "Credit Analyst", "dates": "Jan 2020 - Dec 2021", "bullets": ["Built a dashboard."]}]},
                          {"org": "Viva Technologies", "location": "Accra, GH",
                           "roles": [{"title": "Operations Lead", "dates": "Mar 2020 - Aug 2021", "bullets": ["Led drivers."]}]}],
           "education": [{"school": "DePaul University", "degree": "MBA", "date": "2025"}],
           "skills": {"Data": "SQL, Python"}}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """The engine on a throwaway data folder with a saved profile, the answers every form asks
    (gender and race among them), work authorization and a chat that said a salary."""
    monkeypatch.delenv(M.ENV, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(data))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._SESSION.pop("s", None)
    mem = A._memory()
    mem.save("default", json.loads(json.dumps(PROFILE)), {"identity": dict(PROFILE["identity"])}, [])
    c = A.app.test_client()
    assert c.post("/api/answers/basics", json={"fields": {"salary": "120000", "citizenship": "Ghana",
                                                          "gender": "female", "race": "asian"}}).status_code == 200
    c.post("/api/answers/workauth", json={"work_auth": {"countries": {"US": {"status": "f1_opt", "authorized": "Yes",
                                                                            "sponsor_now": "No", "sponsor_future": "Yes"}}}})
    d = A._palace()
    d.remember_turn("agent", "What salary are you aiming for?")
    ref = d.remember_turn("user", "I am aiming for 135000 dollars a year.")
    d.remember_turn("user", "Resolved a production outage in 20 minutes on-call.")
    mem.upsert_fact("default", "fact", "notice_period", "Two weeks", provenance=f"turn 2 · {ref}", confidence=0.7)
    from applicant import facts as F
    F.sync_chat(A._brain(), mem.list_facts("default", status=None))
    yield A, c
    A._SESSION.pop("s", None)
    M.forget_process_state(data)


def _page(c):
    r = c.get("/api/memory/page")
    assert r.status_code == 200
    return r.get_json()


def test_every_fact_is_grouped_with_where_and_when(env):
    A, c = env
    d = _page(c)
    about = {r["field"]: r for r in d["about"]}
    assert about["email"]["value"] == "ama@example.com" and about["email"]["source"] and about["email"]["when"]
    work = {r["id"]: r for r in d["work"]}
    assert "F-1 OPT" in work["wa:US"]["value"] and work["wa:US"]["source"].startswith("You")
    assert work["key:@citizenship"]["value"] == "Ghana"
    answers = {r["key"]: r for r in d["answers"]}
    assert answers["@salary"]["value"] == "$120,000" and answers["@salary"]["source"] == "You, on the form answers card"
    assert [r["org"] for r in d["career"]["roles"]] == ["Stanbic Bank", "Viva Technologies"]
    assert d["career"]["schools"][0]["school"] == "DePaul University"
    assert d["diary"]["lines"] == 3
    assert d["basics"]["total"] == 7


def test_self_identification_values_stay_out_of_the_page_until_show(env):
    A, c = env
    d = _page(c)
    eeo = {r["field"]: r for r in d["eeo"]}
    assert eeo["gender"]["state"] == "saved" and eeo["race"]["state"] == "saved" and eeo["veteran"]["state"] == ""
    d.pop("eeo_choices")                            # the card's own choices, not the person's answers
    blob = json.dumps(d).lower()
    assert "female" not in blob and "asian" not in blob
    # nor in Other answers, which list the answer library
    lib = json.dumps(c.get("/api/answers?view=memory").get_json()["answers"]).lower()
    assert "female" not in lib and "asian" not in lib
    shown = c.get("/api/memory/reveal").get_json()["values"]
    assert shown == {"gender": "Female", "race": "Asian"}


def test_forget_the_salary_clears_every_copy(env):
    A, c = env
    out = c.post("/api/memory/forget", json={"id": "key:@salary"}).get_json()
    assert out["forgotten"] >= 2
    assert not [r for r in out["answers"] if r["key"] == "@salary"]
    assert not str(A._load_prefs().get("desired_salary") or "")
    lib = A._answer_lib()
    try:
        assert lib.best("@salary") is None                      # the next application asks for it
    finally:
        lib.close()
    st = A._brain()
    assert not st.q("SELECT id FROM fact WHERE key='@salary'")    # nothing left in the brain, history too


def test_forget_with_the_chat_lines(env):
    A, c = env
    st = A._brain()
    noted = _page(c)["noted"]
    n = [x for x in noted if x["key"] == "@notice_period"][0]
    c.post("/api/memory/forget", json={"id": f"noted:{n['fact']}", "chat": True})
    path = A.PALACE_DIR / "transcripts" / "default.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and lines[1] == ""                   # blanked in place: other refs still point right
    assert "135000" not in path.read_text(encoding="utf-8")
    assert "outage" in lines[2]
    assert not st.q("SELECT id FROM fact WHERE key='@notice_period'")
    assert not A._memory().list_facts("default", status=None)  # the chat fact is gone, so it never comes back


def test_i_noted_is_saved_only_on_a_tap(env):
    A, c = env
    lib = A._answer_lib()
    try:
        assert lib.best("@notice_period") is None
    finally:
        lib.close()
    n = [x for x in _page(c)["noted"] if x["key"] == "@notice_period"][0]
    assert n["value"] == "Two weeks" and n["source"] == "From your chat"
    d = c.post("/api/memory/noted", json={"fact": n["fact"], "save": True}).get_json()
    assert not d["noted"]
    assert {r["key"]: r["value"] for r in d["answers"]}["@notice_period"] == "Two weeks"
    lib = A._answer_lib()
    try:
        assert lib.best("@notice_period")["answer"] == "Two weeks"
    finally:
        lib.close()
    # a later chat sync keeps it settled
    from applicant import facts as F
    F.sync_chat(A._brain(), A._memory().list_facts("default", status=None))
    assert not _page(c)["noted"]


def test_not_quite_forgets_the_proposal(env):
    A, c = env
    n = [x for x in _page(c)["noted"] if x["key"] == "@notice_period"][0]
    d = c.post("/api/memory/noted", json={"fact": n["fact"], "save": False}).get_json()
    assert not d["noted"]
    from applicant import facts as F
    F.sync_chat(A._brain(), A._memory().list_facts("default", status=None))
    assert not _page(c)["noted"]


def test_a_chat_salary_that_disagrees_is_one_question(env):
    A, c = env
    A._memory().upsert_fact("default", "fact", "desired_salary", "$135,000", provenance="turn 3", confidence=0.7)
    from applicant import facts as F
    F.sync_chat(A._brain(), A._memory().list_facts("default", status=None))
    d = _page(c)
    assert [x["kind"] for x in d["needs"]] == ["conflict"]
    assert not [x for x in d["noted"] if x["key"] == "@salary"]       # never also a chip


def test_edit_a_form_answer_lands_where_forms_read_it(env):
    A, c = env
    d = c.post("/api/memory/answer", json={"key": "@start_date", "value": "June 1, 2027"}).get_json()
    assert {r["key"]: r["value"] for r in d["answers"]}["@start_date"] == "June 1, 2027"
    assert A._load_prefs().get("earliest_start") == "June 1, 2027"
    c.post("/api/memory/answer", json={"key": "contact:phone", "value": "312-555-0199"})
    saved = A._memory().load("default")
    assert saved["profile"]["identity"]["phone"] == "312-555-0199" == saved["essentials"]["identity"]["phone"]
    assert c.post("/api/memory/answer", json={"key": "@salary", "value": "lots"}).status_code == 400


def test_forget_a_contact_detail(env):
    A, c = env
    c.post("/api/memory/forget", json={"id": "contact:phone"})
    saved = A._memory().load("default")
    assert not saved["profile"]["identity"].get("phone") and not saved["essentials"]["identity"].get("phone")
    assert not A._brain().q("SELECT id FROM fact WHERE key='phone'")
    assert "phone" not in {r["field"] for r in _page(c)["about"]}


def test_forget_a_role_everywhere_and_pause_its_choice(env):
    A, c = env
    from applicant import rules as R
    st = A._brain()
    rid = R.add_rule(st, "hide", ["role:viva technologies|operations lead"], "industry", "52", source="builder")
    st.q("SELECT 1")
    c.post("/api/memory/career", json={"sec": "experience", "i": 1})
    saved = A._memory().load("default")
    assert [e["org"] for e in saved["profile"]["experience"]] == ["Stanbic Bank"]
    assert not st.q("SELECT id FROM fact WHERE key='role' AND subject LIKE 'role:viva%'")
    assert R.get_rule(st, rid)["status"] == "paused"
    # an edit through the section save makes the entry the person's own fact
    st2 = A.app.test_client().post("/api/profile/sections", json={
        "experience": [{"org": "Stanbic Bank", "title": "Credit Analyst", "dates": "Jan 2020 - Jan 2022",
                        "location": "Accra, GH", "bullets": ["Built a dashboard."]}],
        "education": [], "projects": [], "extracurricular": [], "summary": "", "interests": "",
        "skills_input": [], "declined": []})
    assert st2.status_code == 200
    role = st.q("SELECT * FROM fact WHERE key='role' AND status='active'")
    assert len(role) == 1 and role[0]["source"] == "you" and role[0]["source_ref"] == "memory_page"


def test_history_names_what_was_filled_never_a_self_identification_value(env):
    A, c = env
    from applicant import page as P
    from applicant import rules as R
    st = A._brain()
    R.record_application(st, 9, company="Acme Bank", role="Analyst", shown=["role:stanbic bank|credit analyst"])
    P.record_fills(st, 9, [("Desired annual salary", "library"), ("Gender", "eeo"), ("Gender", "eeo")])
    app_ = _page(c)["history"]["applications"][0]
    assert app_["company"] == "Acme Bank" and app_["shown"] == ["Stanbic Bank"]
    assert app_["filled"] == ["salary"] and app_["eeo"] == ["gender: your saved answer"]
    assert len(st.q("SELECT id FROM fill WHERE application_id=9")) == 2     # the same label once


def test_export_is_one_zip_with_the_salary_and_the_diary(env):
    A, c = env
    r = c.get("/api/memory/export.zip")
    assert r.status_code == 200 and r.mimetype == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = z.namelist()
    assert "memory.json" in names and "diary/default.jsonl" in names
    mem = z.read("memory.json").decode()
    assert "120000" in mem and "Ama Mensah" in mem
    assert "outage" in z.read("diary/default.jsonl").decode()


def test_rules_offer_the_persons_own_items_for_add_a_choice(env):
    A, c = env
    d = c.get("/api/brain/rules").get_json()
    assert {i["id"] for i in d["items"]} >= {"role:stanbic bank|credit analyst", "role:viva technologies|operations lead"}
    out = c.post("/api/brain/rules", json={"items": ["role:viva technologies|operations lead"], "action": "hide",
                                           "scope_kind": "industry", "scope_value": "52"}).get_json()
    assert out["saved"] and out["rules"][0]["text"].endswith("hide Viva Technologies.")
