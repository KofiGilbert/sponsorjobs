"""Three leftovers from Kofi's D365 chat (2026-11-10): the header read "OPEN DATE: November 9"
(the job title parsed from the posting's metadata line), the intake model asked "if you have a
LinkedIn or GitHub address, send it over", and the edit tip and page notes vanished when the
chat was reopened. FakeLLM and real pdflatex."""

from __future__ import annotations

import pytest

import ui.app as app
from agent import resume_chat as rc
from conftest import requires_latex
from intake.memory import ConversationMemory
from ui.records import CVRecords
from ui.session import WebIntake, _jd_label

SMITHSONIAN = """OPEN DATE: November 9, 2026
CLOSING DATE: November 23, 2026
POSITION TYPE: Trust Fund
APPOINTMENT TYPE: Indefinite
SCHEDULE: Full Time
*DUTY LOCATION: Washington, DC
*

Position sensitivity and risk: Low Risk

The D365 Software Developer plays a key role in advancing Smithsonian Enterprises technology
initiatives, with a primary focus on Microsoft Dynamics 365 Finance & Operations.
"""

JD = ("Data Analyst. Required skills: SQL, Python, dashboards, statistics, data pipelines, "
      "machine learning, Tableau, Power BI, Excel, forecasting, ETL, data modeling.")
SKELETON = (
    "I'm Kwame Asante, kwame@example.com. B.S. in Statistics at University of Ghana, "
    "graduated June 2019. Data Analyst at BlackOrigin in Accra, Ghana since Feb 2021. "
    "Analyst at Stanbic Bank in Accra, Ghana from Jan 2018 to Dec 2020. "
    "Skills: SQL, Python, Tableau. "
    "No github, no blog, no linkedin. No address. Skip courses. No projects."
)
EXTRAS = "Outside work I volunteer coaching a youth team, and I enjoy chess and hiking."
ASK = ("Your resume is ready to build, and if you have a LinkedIn or GitHub address, send it "
       "over and I'll add it to the header.")


# ------------------------------------------------------------------ the job title

def test_the_title_skips_the_postings_metadata_lines():
    assert _jd_label(SMITHSONIAN) == ("D365 Software Developer", "")
    assert _jd_label("Job Title: Data Analyst\nLocation: New York") == ("Data Analyst", "")
    assert _jd_label("Posted: Nov 3, 2026\nSalary: $90k\nProduct Manager, Growth")[0] == "Product Manager"
    assert _jd_label("OPEN DATE: November 9, 2026\nSCHEDULE: Full Time") == ("", "")
    # Unchanged for the ordinary first-line title.
    assert _jd_label("Senior Data Engineer at Acme in Chicago") == ("Senior Data Engineer", "Acme")
    assert _jd_label("Data Analyst at BlackOrigin") == ("Data Analyst", "BlackOrigin")


def test_the_greeting_names_the_real_role(template_source, fake_llm, workdir):
    s = WebIntake(SMITHSONIAN, template_source, fake_llm, ConversationMemory(":memory:"),
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    text = s.start()["messages"][0]
    assert "the D365 Software Developer role" in text and "OPEN DATE" not in text


# ------------------------------------------------------------------ never ask for GitHub

class _Gh(WebIntake):
    def __init__(self, gh):            # only what _no_github_ask reads
        self.gh = gh

    def _github_view(self):
        return self.gh


@pytest.mark.parametrize("connected", [False, True])
def test_a_github_ask_is_taken_out_of_a_model_reply(connected):
    s = _Gh({"connected": connected, "login": "kofi" if connected else ""})
    out = s._no_github_ask("Weekend football gives it personality. " + ASK)
    assert "GitHub" not in out and "LinkedIn" in out and out.startswith("Weekend football")
    assert bool(s._github_card_due) is (not connected)
    s._github_card_due = False
    assert s._no_github_ask("Great. What's your GitHub username? Then I'll build it.") \
        == "Great. Then I'll build it."
    assert s._no_github_ask("Please share your GitHub link and LinkedIn URL.") \
        == "Please share your LinkedIn URL."
    said = "Your GitHub shows strong Python work."
    assert s._no_github_ask(said) == said           # a mention is not an ask


def _session(template_source, fake_llm, workdir, connected):
    s = WebIntake(JD, template_source, fake_llm, ConversationMemory(":memory:"),
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    s.github_status = lambda: {"connected": connected, "login": "kofi" if connected else ""}
    s.github_token = lambda: "tok" if connected else ""
    return s


@pytest.mark.parametrize("connected", [False, True])
def test_the_intake_reply_never_asks_for_github(template_source, fake_llm, workdir, monkeypatch,
                                                connected):
    monkeypatch.delenv(rc.FLAG, raising=False)          # the intake path, agent off
    s = _session(template_source, fake_llm, workdir, connected)
    s.start()
    real = fake_llm.extract_intake

    def extract(*a, **kw):
        out = dict(real(*a, **kw))
        out["reply"] = "Thanks Kwame. " + ASK
        return out

    monkeypatch.setattr(fake_llm, "extract_intake", extract)
    st = s.submit("I'm Kwame Asante, kwame@example.com.")
    said = " ".join(st["messages"])
    assert "GitHub address" not in said and "send it over" in said
    gh_cards = [c for c in st["cards"] if s._connect_buttons(c)]
    if connected:
        assert not gh_cards and s.GITHUB_NOT_CONNECTED not in said
    else:
        assert len(gh_cards) == 1 and s.GITHUB_NOT_CONNECTED in said
        turn = next(t for t in s.history if t.get("content") == s.GITHUB_NOT_CONNECTED)
        assert s._connect_buttons(turn.get("card"))          # a reopened chat shows it too


# ------------------------------------------------------------------ the notes survive a reload

@requires_latex
def test_the_edit_tip_and_notes_come_back_once_on_reload(template_source, fake_llm, workdir):
    s = _session(template_source, fake_llm, workdir, connected=True)
    s.start()
    s.submit(SKELETON)
    st = s.submit(EXTRAS)
    assert s.assembled is not None
    live = [m for m in st["messages"] if m != s.history[0]["content"]]
    said_live = set(st["messages"])
    app._SESSION["s"] = s
    try:
        client = app.app.test_client()
        h1 = client.get("/api/session/state").get_json()["history"]
        h2 = client.get("/api/session/state").get_json()["history"]
    finally:
        app._SESSION.pop("s", None)
    contents = [t["content"] for t in h1]
    assert h1 == h2, "reading the chat back changed it"
    assert contents.count(WebIntake.EDIT_TIP) == 1
    for m in live:                                  # everything said live is in the history
        assert m in contents, m
    assert WebIntake.EDIT_TIP in said_live
    # The notes sit after the build's row, in the order they were said.
    i = next(k for k, t in enumerate(h1) if t.get("build"))
    assert contents.index(WebIntake.EDIT_TIP) > i
    # A later build adds no second tip.
    s.submit("At BlackOrigin I rebuilt the monthly reporting in SQL and cut close time from "
             "five days to two.")
    assert [t["content"] for t in s.history].count(WebIntake.EDIT_TIP) == 1


def test_a_thrown_away_reply_brings_no_github_card(template_source, fake_llm, workdir, monkeypatch):
    """The build button's reply is not said when the build first asks the outside-work
    question, so the GitHub ask taken out of it must not bring a button (seen in the real run)."""
    from test_chat_one_voice import SAVED
    monkeypatch.delenv(rc.FLAG, raising=False)
    mem = ConversationMemory(":memory:")
    mem.save("default", SAVED, {}, [])
    s = WebIntake(JD, template_source, fake_llm, mem, CVRecords(":memory:"), workdir,
                  jobname="cv", palace_dir=workdir / "p")
    s.github_status = lambda: {"connected": False, "login": ""}
    s.github_token = lambda: ""
    s.start()
    real = fake_llm.extract_intake
    monkeypatch.setattr(fake_llm, "extract_intake",
                        lambda *a, **kw: {**real(*a, **kw), "reply": ASK})
    st = s.submit("just build it from my saved profile")
    assert "won't look complete" in " ".join(st["messages"])
    assert s.GITHUB_NOT_CONNECTED not in st["messages"]
    assert not [c for c in st["cards"] if s._connect_buttons(c)]
