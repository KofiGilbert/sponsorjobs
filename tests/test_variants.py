"""Variant CVs and record naming (case-study obs #35/#36).

The founder's A/B comparison took eight manual steps including SQL surgery to bench
a role; and his four Amazon records were all named "Founder (Side Project)". Variants
are now one call, run on a scratch profile row, and records are named for the job."""

from __future__ import annotations

import json

import ui.app as app
from conftest import requires_latex
from intake.memory import ConversationMemory
from ui.records import CVRecords
from ui.session import WebIntake

JD = ("AI Architect - Synechron - Chicago. Generative AI, RAG, MLOps, Azure, "
      "AWS, governance, machine learning.")

COMPLETE = ("I'm Maya Rodriguez, maya@example.com, https://github.com/maya, "
            "https://www.linkedin.com/in/maya, https://mayablog.com . I live at 88 Oak St, Chicago, IL 60614. "
            "I did an M.S. in AI at Northwestern in 2018 and a B.S. at UIUC in 2015. "
            "My courses were Machine Learning, Optimization, and Statistics. "
            "I work as Principal Engineer at Continental Trust Bank in Chicago, IL since Feb 2021. "
            "I don't have projects to add.")
EXTRAS = ("Outside work I volunteer mentoring at a coding bootcamp, and I enjoy chess, "
          "climbing, and jazz.")


@requires_latex
def test_records_are_named_for_the_job_not_the_first_role(template_source, fake_llm, workdir):
    mem, recs = ConversationMemory(":memory:"), CVRecords(":memory:")
    s = WebIntake(JD, template_source, fake_llm, mem, recs, workdir, jobname="cv")
    s.start()
    s.submit(COMPLETE)
    s.submit(EXTRAS)
    out = s.accept()
    assert out["ok"] is True
    rec = recs.get(out["record_id"])
    # Named for the job applied to, not "Principal Engineer" (the CV's first role).
    assert rec["role"].startswith("AI Architect")
    assert "Principal Engineer" not in rec["role"]


@requires_latex
def test_variant_builds_on_scratch_profile_and_links_back(monkeypatch, template_source,
                                                          fake_llm, workdir, tmp_path):
    # File-backed + FRESH handles per call, mirroring production: the real _memory()/_records()
    # open a new sqlite handle each call and the route handlers close it. A shared :memory: store
    # would be destroyed the first time a handler closed it, breaking the next request.
    mem_db, rec_db = tmp_path / "memory.db", tmp_path / "records.db"
    mem, recs = ConversationMemory(mem_db), CVRecords(rec_db)
    monkeypatch.setattr(app, "_memory", lambda: ConversationMemory(mem_db))
    monkeypatch.setattr(app, "_records", lambda: CVRecords(rec_db))
    monkeypatch.setattr(app, "_make_llm", lambda: fake_llm)
    monkeypatch.setattr(app, "WORKDIR", tmp_path)
    monkeypatch.setattr(app, "PALACE_DIR", tmp_path / "palace")
    app._SESSION.pop("s", None)

    # A finished application (record with its JD) + a saved default profile of 2 roles.
    s = WebIntake(JD, template_source, fake_llm, mem, recs, workdir, jobname="cv")
    s.start()
    s.submit(COMPLETE)
    s.submit(EXTRAS)
    rid = s.accept()["record_id"]
    saved = mem.load("default")
    prof, ess = saved["profile"], saved["essentials"]
    for bag in (prof, ess):
        bag["experience"] = list(bag["experience"]) + [
            {"org": "Acme Corp", "title": "Analyst", "dates": "2019 - 2021",
             "location": "Chicago, IL", "bullets": ["Analyzed things."]}]
    mem.save("default", prof, ess, [])

    client = app.app.test_client()
    r = client.post(f"/api/record/{rid}/variant",
                    json={"exclude_orgs": ["Acme Corp"]})
    assert r.status_code == 200, r.get_json()
    st = r.get_json()
    assert st["phase"] == "review"

    # The variant session excluded the role on its SCRATCH row…
    scratch = mem.load("variant")
    orgs = [e.get("org") for e in scratch["essentials"]["experience"]]
    assert "Acme Corp" not in orgs
    # …while the durable profile still has it.
    durable = mem.load("default")
    assert any(e.get("org") == "Acme Corp"
               for e in durable["essentials"]["experience"])

    # Accepting the variant records the sibling link.
    r2 = client.post("/api/session/accept", json={})
    assert r2.status_code == 200 and r2.get_json()["ok"] is True
    rec2 = recs.get(r2.get_json()["record_id"])
    assert json.loads(rec2["data"])["variant_of"] == rid
