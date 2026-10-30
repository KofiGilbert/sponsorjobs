"""Saved self-identification answers on Lam Research's Phenom form (live failure, 2026-10-30).

The person's "Answers every form asks" card was complete (Male; not Hispanic or Latino; Black or
African American; not a protected veteran; no disability), yet the agent asked "Gender" and
"Ethnicity" on the bar. Root cause: Phenom's dropdown ignores Escape and blur, so after the shell
read one list it stayed open over the next field, and the next read took THAT list as the next
question's choices (Ethnicity read as "Choose not to disclose, Male, Female"). The saved answer
fitted none of them, so the person was asked. The engine had the same stale-list read (Disability
got Ethnicity's choices, Gender and Veteran none), so it never filled them either.

Pinned here with the real option texts (tests/fixtures/forms/phenom_lam.html has the markup; the
browser side runs in tests/test_agent_view.js): the saved answers map, in code, to Lam's own
choices; the known_only check tells the shell which choice to pick itself (never the model); the
page's note above "Veteran" (about protected veterans) lets the saved "not a protected veteran"
answer Lam's plain No; and the shell's pick path never logs or sends the choice.
"""
from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from autoapply import asks as AK
from autoapply import basics
from autoapply.answers import AnswerLibrary

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}
# careers.lamresearch.com/careers/apply, read 2026-10-30 (the page's own order)
LAM_GENDER = ["Choose not to disclose", "Male", "Female"]
LAM_ETHNICITY = ["American Indian/Alaskan Native", "Asian", "Black or African American", "Choose not to disclose",
                 "Hispanic or Latino", "Native Hawaiian or Other Pacific Island", "Two or More Races", "White"]
LAM_DISABILITY = ["Choose not to disclose", "No", "Yes"]
LAM_VETERAN = ["Choose not to disclose", "No", "Yes"]
LAM_VETERAN_NOTE = ("Veteran To meet U.S. Department of Labor requirements, your voluntary cooperation is appreciated. "
                    "Title 38, United States Code, Section 4212(d), PL 105-339 and PL, 107-228, require that federal "
                    "contractors report annually the number of employees who are: 1) special disabled veterans 2) "
                    "veterans of the Vietnam era and 3) other protected veterans (that is, who served on active duty "
                    "during ...")
CARD = {"gender": "male", "hispanic": "no", "race": "black", "veteran": "not", "disability": "no"}


@pytest.fixture()
def lib(tmp_path):
    lib = AnswerLibrary(tmp_path / "answers.db")
    basics.write(lib, CARD)
    yield lib
    lib.close()


def test_the_saved_card_maps_to_lams_own_choices(lib):
    assert AK.known_answer("Gender", LAM_GENDER, library=lib) == ("Male", "library")
    # Lam's "Ethnicity" is ONE list of races plus "Hispanic or Latino": both halves decide it
    assert AK.known_answer("Ethnicity", LAM_ETHNICITY, library=lib) == ("Black or African American", "library")
    assert AK.known_answer("Disability", LAM_DISABILITY, library=lib) == ("No", "library")


def test_veteran_takes_the_pages_own_note_about_protected_veterans(lib):
    # A bare "Veteran: No / Yes" is not answered from "I am not a protected veteran" (a veteran
    # who is not protected served) ...
    assert AK.known_answer("Veteran", LAM_VETERAN, library=lib) is None
    # ... but Lam's note right above the field says the question is about protected veterans.
    assert AK.known_answer("Veteran", LAM_VETERAN, library=lib, hint=LAM_VETERAN_NOTE) == ("No", "library")
    # a note about something else never turns the plain No into an answer
    assert AK.known_answer("Veteran", LAM_VETERAN, library=lib, hint="Diversity Information. Completion is voluntary.") is None


def test_the_wrong_list_is_why_it_asked(lib):
    """The live failure: the next question's read took the previous list. No saved answer fits
    Gender's choices on the Ethnicity question, so it was asked; a decline still never guesses."""
    assert AK.known_answer("Ethnicity", LAM_GENDER, library=lib) is None


def test_a_standing_decline_takes_lams_choose_not_to_disclose(tmp_path):
    lib = AnswerLibrary(tmp_path / "a.db")
    try:
        for q, opts in (("Gender", LAM_GENDER), ("Ethnicity", LAM_ETHNICITY), ("Veteran", LAM_VETERAN),
                        ("Disability", LAM_DISABILITY)):
            assert AK.known_answer(q, opts, library=lib, eeo_decline=True) == ("Choose not to disclose", "eeo_decline")
    finally:
        lib.close()


# --------------------------------------------------------------------------- the known_only check
@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    sent = []

    class Chan:
        def send(self, text, buttons=None):
            sent.append((text, buttons))
    monkeypatch.setattr(mod, "_active_channel", lambda: Chan())
    with mod._ASKS_LOCK:
        mod._ASKS.clear()
    lib = mod._answer_lib()
    try:
        basics.write(lib, CARD)
    finally:
        lib.close()
    return mod, sent


def test_known_only_names_the_choice_the_shell_picks_itself(A):
    mod, sent = A
    qs = [{"question": "Gender", "options": LAM_GENDER},
          {"question": "Ethnicity", "options": LAM_ETHNICITY},
          {"question": "Veteran", "options": LAM_VETERAN, "hint": LAM_VETERAN_NOTE},
          {"question": "Disability", "options": LAM_DISABILITY}]
    r = mod.app.test_client().post("/api/autoapply/agent/ask", json={"known_only": True, "questions": qs},
                                   headers=H).get_json()
    want = {"Gender": "Male", "Ethnicity": "Black or African American", "Veteran": "No", "Disability": "No"}
    assert r["ok"] is True and r["open"] == []
    assert r["answers"] == want and r["select"] == want
    assert sent == []                                  # nothing went to Telegram


def test_known_only_select_is_only_an_exact_page_choice_on_an_eeo_question(A):
    mod, _sent = A
    lib = mod._answer_lib()
    try:
        lib.save("How did you hear about us?", "LinkedIn", "you")
    finally:
        lib.close()
    r = mod.app.test_client().post("/api/autoapply/agent/ask", json={"known_only": True, "questions": [
        {"question": "How did you hear about us?", "options": ["LinkedIn", "Indeed"]},
        # a cut list: the saved words go to the page as they are, never as a pick
        {"question": "Gender", "options": ["Choose not to disclose"], "partial": True}]}, headers=H).get_json()
    assert r["answers"] == {"How did you hear about us?": "LinkedIn", "Gender": "Male"}
    assert r["select"] == {}


# --------------------------------------------------------------------------- the shell side
def _node(js: str) -> str:
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_the_shell_reads_the_note_above_a_field_from_the_view():
    view = "\n".join([
        "[29]<combobox label=\"Gender\" value=\"\" expanded=false required>",
        "[30]<combobox label=\"Ethnicity\" value=\"\" expanded=false required>",
        "Disability A Disabled Person is an individual who has a mental or physical impairment.",
        "[31]<combobox label=\"Disability\" value=\"\" expanded=false required>",
        LAM_VETERAN_NOTE,
        "[32]<button>Read more</button>",
        "[33]<combobox label=\"Veteran\" value=\"\" expanded=false required>",
    ])
    js = "const A = require(%r); const v = %s; console.log(JSON.stringify([A.noteAbove(v, 'Veteran'), " \
         "A.noteAbove(v, 'Ethnicity'), A.noteAbove(v, 'Disability').slice(0, 10)]));" % (
             str(ROOT / "shell-electron" / "agent.js"), json.dumps(view))
    got = json.loads(_node(js))
    assert "protected veterans" in got[0] and got[1] == "" and got[2] == "Disability"


def test_the_shells_pick_path_never_logs_or_sends_the_choice():
    """pickSaved and fillSavedEeo hand the option only to the page: no dlog, no progress, no
    post carrying it (the run logs "N filled from saved answers")."""
    src = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    body = src[src.index("async function pickSaved("):src.index("// ---- terms a site requires")]
    assert "dlog(" not in body and "progress(" not in body
    posts = re.findall(r"deps\.postApp\(([^)]*)\)", body)
    assert posts and all("known_only: true" in p for p in posts), posts
    assert re.search(r'dlog\("step " \+ step \+ " eeo:", got\.length \+ " filled from saved answers"\)', src)
    # the choice the shell picked never travels on to the brain as a placeholder
    assert "if (pick[q] && await pickSaved(wc, v, q, pick[q])) continue;" in src


def test_the_shell_closes_a_dropdown_that_ignores_escape_and_blur():
    src = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    body = src[src.index("async function closeDropdown("):src.index("async function pressEscape(")]
    assert body.index("pressEscape(wc)") < body.index('close("outside")')
    assert "closeOthers" in src[src.index("async function dropdownOptions("):src.index("async function closeDropdown(")]
