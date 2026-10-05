"""P1: intelligent persistent memory in the existing chat.

Free-form turns are conversational AND remembered; the LLM distills structured facts/preferences
(deduped, with provenance) into the authoritative SQLite store; a stated constraint ("don't
mention X") is honored by the tailoring selection; the person can export and erase everything;
and the runtime NEVER uses FakeLLM (it's a test double only).

All offline: FakeLLM (deterministic) + RESUME_AGENT_PALACE_INDEX=0 (transcript layer only, no
embedder/network). The real backend does the real work in production.
"""

from __future__ import annotations

import importlib

import pytest

from conftest import requires_latex
from intake.memory import ConversationMemory
from llm.base import FakeLLM
from ui.records import CVRecords
from ui.session import WebIntake

JD = "Data Analyst at a fintech. SQL, dashboards, forecasting."


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # Transcript layer only: no embedder, no network. The real backend is used in production.
    monkeypatch.setenv("RESUME_AGENT_PALACE_INDEX", "0")


def _session(tmp_path, db=None):
    return WebIntake(JD, "x", FakeLLM(),
                     ConversationMemory(db or str(tmp_path / "m.db")),
                     CVRecords(":memory:"), tmp_path / "cv",
                     palace_dir=tmp_path / "palace")


# ---- Task 1: free-form chat, remembered across a restart -----------------------------------
def test_freeform_turn_is_conversational_and_persists_across_restart(tmp_path):
    db = str(tmp_path / "m.db")
    s = _session(tmp_path, db)
    s.start()
    out = s.submit("I care about fintech and I'm targeting fintech analyst roles.")
    # A conversational reply, not a CV-field gate question.
    assert out["messages"] and out["phase"] == "chatting"
    assert s.assembled is None   # a free-form turn did not force a build

    # Simulate a full restart: a fresh session reloads the entire history from the palace.
    s2 = _session(tmp_path, db)
    joined = " ".join(t["content"] for t in s2.prior_history)
    assert "targeting fintech" in joined.lower(), "the free-form turn was not remembered"


# ---- Task 2: distilled, deduped facts with provenance ---------------------------------------
def test_facts_are_distilled_deduped_and_carry_provenance(tmp_path):
    db = str(tmp_path / "m.db")
    s = _session(tmp_path, db)
    s.start()
    s.submit("Please don't mention the 2021 gap on my CV. I'm targeting fintech.")

    mem = ConversationMemory(db)
    facts = mem.list_facts("default", status=None)
    kinds = {(f["type"], f["key"]) for f in facts}
    assert ("constraint", "avoid_2021_gap") in kinds
    assert ("preference", "target_industry") in kinds
    assert all(f["provenance"] for f in facts), "every fact must carry provenance"

    # Restating the same preference must NOT create a duplicate row (dedup by type+key).
    before = len(facts)
    s.submit("Just to repeat, I'm targeting fintech.")
    assert len(ConversationMemory(db).list_facts("default", status=None)) == before


# ---- Task 3: recall -> decide, honoring constraints, with a why -----------------------------
def test_a_dont_mention_constraint_is_respected_without_touching_the_profile(tmp_path):
    s = _session(tmp_path)
    s.memory.upsert_fact("default", "constraint", "avoid_2021_gap",
                         "do not mention the 2021 gap", provenance="turn 3")
    s.profile["experience"] = [
        {"org": "Acme Analytics", "title": "Analyst", "dates": "2022-2024"},
        {"org": "Career break", "title": "2021 gap year", "dates": "2021-2022"},
    ]
    s._apply_memory_selection()   # reasoning only; must NOT mutate the durable profile
    assert [e["org"] for e in s.profile["experience"]] == ["Acme Analytics", "Career break"]
    # The render copy (what the CV is actually built from) is the one that excludes it.
    assert [e["org"] for e in s._render_profile()["experience"]] == ["Acme Analytics"]
    # And the reasoning surfaces it, so the person sees why and can override.
    excluded = [x["item"] for x in s.cv_selection.get("excluded", [])]
    assert any("break" in x.lower() or "gap" in x.lower() for x in excluded)
    assert "selection" in s._state()


def test_a_constraint_never_mutates_or_persists_the_saved_profile(tmp_path):
    """The blocking #130 bug: constraint enforcement mutated self.profile and both build paths then
    persisted it, permanently deleting the item (retract couldn't restore it). The filter must
    apply to a RENDER copy only; the durable saved profile stays whole and the exclusion reverses."""
    db = str(tmp_path / "m.db")
    s = _session(tmp_path, db)
    s.profile["experience"] = [
        {"org": "Acme Analytics", "title": "Analyst", "dates": "2022-2024"},
        {"org": "BlackOrigin", "title": "Career break, 2021 gap year", "dates": "2021-2022"},
    ]
    fact = s.memory.upsert_fact("default", "constraint", "avoid_2021_gap",
                               "do not mention the 2021 gap", provenance="turn 3")

    # The CV renders from a copy that EXCLUDES the constrained item...
    assert [e["org"] for e in s._render_profile()["experience"]] == ["Acme Analytics"]
    # ...while a build persists the WHOLE profile (this is exactly what _assemble does).
    s.profile = s._restore_hidden(s._render_profile())
    s._persist()
    saved = ConversationMemory(db).load("default")["profile"]["experience"]
    assert sorted(e["org"] for e in saved) == ["Acme Analytics", "BlackOrigin"], "saved profile lost data"

    # Reversible: retract the constraint and the next render includes the item again.
    s.memory.retract_fact("default", fact["id"])
    assert "BlackOrigin" in [e["org"] for e in s._render_profile()["experience"]]


@requires_latex
def test_a_real_build_hides_the_item_yet_keeps_the_saved_profile_whole(template_source, fake_llm, workdir):
    """End to end through build_from_saved + _assemble + _persist: the rendered CV excludes the
    constrained item, but the durable saved profile keeps it, and retracting brings it back."""
    db = str(workdir / "m.db")
    mem = ConversationMemory(db)
    saved = {
        "identity": {"name": "Kwame Asante", "email": "kwame@example.com"},
        "education": [{"school": "University of Ghana", "degree": "B.S. Statistics",
                       "date": "June 2019", "location": "Accra, Ghana"}],
        "experience": [
            {"org": "BlackOrigin", "location": "Accra, Ghana",
             "roles": [{"title": "Data Analyst", "dates": "Feb 2022 - Present",
                        "bullets": ["Built Tableau dashboards for the operations team."]}]},
            {"org": "Sabbatical", "location": "Accra, Ghana",
             "roles": [{"title": "Career break, 2021 gap year", "dates": "2021 - 2022",
                        "bullets": ["Took time off for the 2021 gap."]}]},
        ],
        "skills": {"Data & Analytics": "SQL, Python, Tableau", "Domain": "statistics, forecasting"},
    }
    mem.save("default", saved, {}, [])
    fact = mem.upsert_fact("default", "constraint", "avoid_2021_gap",
                          "do not mention the 2021 gap", provenance="turn 3")

    def _build():
        s = WebIntake(JD, template_source, fake_llm, ConversationMemory(db), CVRecords(":memory:"),
                      workdir, jobname="cv", palace_dir=workdir / "p")
        s.essentials["override"] = True      # skip the optional-extras gate for the test
        s.essentials["unattended"] = True    # and the required-sections gate
        s.build_from_saved()
        return s

    s = _build()
    rendered = [e.get("org") for e in (s.assembled.profile_used.get("experience") or [])]
    assert "Sabbatical" not in rendered, "the constrained item rendered onto the CV"
    kept = [e.get("org") for e in ConversationMemory(db).load("default")["profile"]["experience"]]
    assert "Sabbatical" in kept, "the build persisted the filtered profile and lost the item"

    # Retract the constraint -> the next build includes it again (reversible).
    mem.retract_fact("default", fact["id"])
    s2 = _build()
    rendered2 = [e.get("org") for e in (s2.assembled.profile_used.get("experience") or [])]
    assert "Sabbatical" in rendered2, "retracting the constraint did not bring the item back"


def test_recall_to_decision_records_a_why_and_the_person_can_still_override(tmp_path):
    s = _session(tmp_path)
    s.profile["experience"] = [{"org": "Northwind Data", "title": "Analyst", "dates": "2020-2024"}]
    s._apply_memory_selection()
    sel = s.cv_selection
    assert sel["selected"] and all(item.get("why") for item in sel["selected"]), "no why recorded"
    # Overriding still works: the person edits their own profile after the selection ran.
    s.profile["experience"][0]["title"] = "Senior Data Analyst"
    assert s.profile["experience"][0]["title"] == "Senior Data Analyst"


# ---- Task 4: view / export / erase (endpoints) ----------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A.DB_PATH = str(tmp_path / "m.db")
    A.PALACE_DIR = tmp_path / "palace"
    return A, A.app.test_client()


def test_memory_export_returns_everything_then_erase_empties_it(client):
    A, c = client
    mem = A._memory()
    mem.upsert_fact("default", "preference", "target_industry", "fintech", provenance="turn 2")
    mem.upsert_fact("default", "constraint", "avoid_2021_gap", "do not mention 2021 gap", provenance="turn 3")
    A._palace().remember_turn("user", "here is my history")

    exp = c.get("/api/memory/export").get_json()
    assert len(exp["facts"]) == 2 and len(exp["transcript"]) == 1
    assert set(exp) >= {"facts", "transcript", "profile", "essentials"}

    assert c.post("/api/memory/erase").get_json()["ok"] is True
    view = c.get("/api/memory").get_json()
    assert view["facts"] == [] and view["turns"] == 0
    assert A._memory().list_facts("default", status=None) == []
    assert A._palace().load_history() == []


# ---- The runtime never uses the test double --------------------------------------------------
def test_runtime_uses_the_real_backend_when_a_key_is_set(monkeypatch):
    import ui.app as A
    import llm.anthropic_client as ac
    from llm.base import FakeLLM as _Fake
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "0")   # never the test double at runtime
    monkeypatch.setenv("TAILOR_OWN_KEY", "1")           # exercise the bring-your-own-key backend
    monkeypatch.setattr(A, "_ai_provider", lambda: "anthropic")
    monkeypatch.setattr(A, "_cred", lambda name: "sk-ant-test")
    monkeypatch.setattr(A, "_require_connectivity", lambda: None)
    made = {}

    class _RealSpy:
        def __init__(self, **kw):
            made["real"] = True
    monkeypatch.setattr(ac, "AnthropicLLM", _RealSpy)

    llm = A._make_llm()
    assert made.get("real") is True, "runtime did not construct the real backend"
    assert not isinstance(llm, _Fake), "runtime must never return FakeLLM"
