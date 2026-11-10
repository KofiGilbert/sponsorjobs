"""Accept & Save gives feedback wherever it is clicked (Kofi, test build 13, 2026-11-09: clicked
on Home with the resume pane open, nothing visibly changed). The route returns the saved
resume's id, which the page uses to open Resumes with it highlighted, and the session reads as
done, so the rail's Building item goes."""

from __future__ import annotations

from pathlib import Path

import ui.app as app
from conftest import requires_latex
from intake.memory import ConversationMemory
from ui.records import CVRecords
from ui.session import WebIntake

ROOT = Path(__file__).resolve().parents[1]
JD = ("Data Analyst. Required skills: SQL, Python, dashboards, statistics, Tableau, Excel.")
SKELETON = (
    "I'm Kwame Asante, kwame@example.com. B.S. in Statistics at University of Ghana, "
    "graduated June 2019. Data Analyst at BlackOrigin in Accra, Ghana since Feb 2021. "
    "Analyst at Stanbic Bank in Accra, Ghana from Jan 2018 to Dec 2020. "
    "Skills: SQL, Python, Tableau. "
    "No github, no blog, no linkedin. No address. Skip courses. No projects."
)
EXTRAS = "Outside work I volunteer coaching a youth team, and I enjoy chess and hiking."


@requires_latex
def test_accept_returns_the_saved_id_and_ends_the_in_progress_item(template_source, fake_llm, workdir):
    recs = CVRecords(":memory:")
    s = WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"), recs, workdir,
                  jobname="cv", palace_dir=workdir / "p")
    s.start()
    s.submit(SKELETON)
    if s.assembled is None:
        s.submit(EXTRAS)
    assert s.assembled is not None
    app._SESSION["s"] = s
    try:
        client = app.app.test_client()
        assert client.get("/api/session/state").get_json()["phase"] != "done"
        r = client.post("/api/session/accept", json={}).get_json()
        assert r["ok"] is True and isinstance(r["record_id"], int)
        assert recs.get(r["record_id"])                      # the record the page highlights
        # the page hides the rail's Building item for a saved (done) session
        assert client.get("/api/session/state").get_json()["phase"] == "done"
        # a retry returns the same id (never a second record)
        assert client.post("/api/session/accept", json={}).get_json()["record_id"] == r["record_id"]
    finally:
        app._SESSION.pop("s", None)


def test_the_page_says_saving_then_toasts_and_opens_resumes():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    h = js.split('$("#acceptBtn").addEventListener("click"', 1)[1].split("\n  });\n", 1)[0]
    assert 'btn.textContent = "Saving…"' in h and "btn.disabled = true" in h
    assert 'toast("Saved to Resumes", { label: "Open"' in h
    assert 'Panel.close()' in h and "refreshLiveNav()" in h and "showSavedResume(r.record_id)" in h
    # a blocked save is a toast, never only a chat bubble
    blocked = h.split("r.ok === false", 1)[1].split("return;", 1)[0]
    assert "toast(why" in blocked
    assert 'cv-card[data-id=' in js and 'classList.add("is-new")' in js
    paint = js.split("function paintLiveNav(st)", 1)[1].split("\n  }\n", 1)[0]
    assert 'st.phase === "done"' in paint
