"""Acceptance tests for the MemPalace-backed durable memory layer (CLAUDE.md §4b, §5).

Covers the guarantee that nothing the person told us is ever lost:
  * every conversation turn is persisted verbatim the moment it happens, inside
    submit() — NOT only on Accept,
  * a server restart mid-conversation loses nothing (the palace transcript is the
    source of truth, independent of the SQLite profile store and of Accept),
  * a returning user is recalled from the palace on BOTH entry points — New CV and
    Tailor-from-job.

The verbatim-layer tests are model-free and run everywhere. The semantic-recall
test is marked `requires_palace` and turns on real indexing itself.
"""

from __future__ import annotations

from intake.memory import ConversationMemory
from intake.palace_memory import PalaceMemory
from ui.records import CVRecords
from ui.session import WebIntake
from conftest import requires_palace

JD = ("AI Architect - Synechron - Chicago. Generative AI, RAG, MLOps, Azure, "
      "governance, machine learning.")


def _mk(template_source, fake_llm, workdir, palace_dir, jd=JD, jobname="cv"):
    return WebIntake(jd, template_source, fake_llm, ConversationMemory(":memory:"),
                     CVRecords(":memory:"), workdir, jobname=jobname, palace_dir=palace_dir)


# -- verbatim durability (model-free) ----------------------------------- #

def test_turn_persists_midconversation_before_accept(template_source, fake_llm, tmp_path):
    """A turn is durable the instant it's sent — before any Accept."""
    palace = tmp_path / "palace"
    s = _mk(template_source, fake_llm, tmp_path / "b", palace)
    s.start()
    s.submit("Hi, I'm Alex Kim, alex@example.com")   # identity-only: stays in chat, no draft
    assert s.stage == "chatting" and s.assembled is None   # nothing accepted/drafted

    assert (palace / "transcripts" / "default.jsonl").exists()   # already on disk
    turns = s.palace.load_history()
    joined = " ".join(t["content"] for t in turns)
    assert "Alex Kim" in joined
    assert any(t["role"] == "user" for t in turns)


def test_restart_midconversation_loses_nothing(template_source, fake_llm, tmp_path):
    """Simulate a crash: session 1 never accepts; a brand-new session (fresh
    in-memory SQLite, same palace dir) still sees every earlier turn — proving
    the palace transcript, not Accept, is the durable record."""
    palace = tmp_path / "palace"
    s1 = _mk(template_source, fake_llm, tmp_path / "b1", palace)
    s1.start()
    s1.submit("Hi, I'm Alex Kim, alex@example.com")
    del s1   # crash before Accept

    s2 = _mk(template_source, fake_llm, tmp_path / "b2", palace)
    prior = " ".join(t["content"] for t in s2.prior_history)
    assert "Alex Kim" in prior
    # Nothing was Accept-saved to SQLite (in-memory, empty): recall is purely
    # from the palace transcript.
    assert s2.returning is False


def test_palace_memory_verbatim_roundtrip(tmp_path, monkeypatch):
    """The wrapper stores turns verbatim and reads them back for the same person
    (name-slugged), and recall degrades to [] — never crashes — when disabled."""
    monkeypatch.setenv("RESUME_AGENT_PALACE_INDEX", "0")   # verbatim only, no model
    p = PalaceMemory(tmp_path / "palace", person="Maya Rodriguez")
    p.remember_turn("user", "I work at Continental Trust Bank.")
    p.remember_turn("agent", "Got it.")

    p2 = PalaceMemory(tmp_path / "palace", person="Maya Rodriguez")   # fresh instance, same person
    assert [(t["role"], t["content"]) for t in p2.load_history()] == [
        ("user", "I work at Continental Trust Bank."), ("agent", "Got it.")]
    assert p2.enabled is False
    assert p2.recall("where do they work") == []   # no semantic layer -> empty, no error


# -- semantic recall on both entry points (needs the embedder) ---------- #

@requires_palace
def test_returning_user_recalled_on_both_entry_points(template_source, fake_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_PALACE_INDEX", "1")   # turn on real indexing + recall
    palace = tmp_path / "palace"

    # The person told us where they work in an earlier session.
    seed = _mk(template_source, fake_llm, tmp_path / "seed", palace)
    seed.palace.remember_turn(
        "user", "I'm Maya Rodriguez, Principal Engineer at Continental Trust Bank since 2021.")

    def recalled(sess):
        return " ".join(h["text"] for h in sess.recalled)

    # Entry point 1 — New CV (plain JD).
    s_new = _mk(template_source, fake_llm, tmp_path / "new", palace)
    s_new.start()
    assert "Continental Trust Bank" in recalled(s_new)

    # Entry point 2 — Tailor-from-job (same construction, job company/role set).
    s_job = _mk(template_source, fake_llm, tmp_path / "job", palace,
                jd="Analytics Engineer at Spotify. SQL, Python, experimentation.")
    s_job.company, s_job.role = "Spotify", "Analytics Engineer"
    s_job.start()
    assert "Continental Trust Bank" in recalled(s_job)
