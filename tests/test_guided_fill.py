"""Guided fill (2026-11-26): top to bottom, visible, ends where the person acts.

The owner watched a live Autofill on a Greenhouse form and could not follow it: the pointer was
not seen (the fields it filled were off screen), Yes / No lists were left open, and Veteran Status
stayed "Select..." although it is saved (the form read stopped reading choices after 15 lists, and
that form has 23). Now the fill walks the form in page order with the pointer, closes every list,
reads every list's choices, and ends on the first field that needs the person, else on Submit.
The walk itself runs in a real Chromium: tests/js/test_guided_walk.js (below).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

try:
    from tests.test_form_widgets import _playwright_dir
except ImportError:                                   # run from the tests folder
    from test_form_widgets import _playwright_dir  # type: ignore

browser = pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None,
                             reason="needs node + playwright (shell-electron devDependency)")


@browser
def test_the_guided_walk_in_a_real_page():
    import os
    env = dict(os.environ, SJ_PLAYWRIGHT_DIR=str(_playwright_dir()))
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_guided_walk.js")], capture_output=True,
                         text=True, timeout=240, env=env)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout[-2000:] + out.stderr[-2000:]


def test_every_list_is_read_not_only_the_first_fifteen():
    core = (ROOT / "extension" / "autofill_core.js").read_text(encoding="utf-8")
    assert "n >= 40" in core and "n >= 15 " not in core
    assert re.search(r"EXTRACT_BUDGET_MS = 15000", core)


def test_the_shell_walks_every_autofill_and_ends_where_the_person_acts():
    main = (ROOT / "shell-electron" / "main.js").read_text(encoding="utf-8")
    assert "runAutoApplyOnPane(recordId, { pointer: true, guided: true })" in main
    assert "return guideEnd(r);" in main                      # every Autofill ends with guideEnd
    agent = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    assert "guided: !quiet" in agent                          # the agent's pages are walked the same way
    app = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function say(msg)" in app and "guideStep" in app   # one status line, Next / Previous / Done


def _bar_short(cases):
    """Run app.js's aaBarShort under node on ``cases`` (the function cut out of the file)."""
    import json
    app = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    i = app.index("  function aaBarShort(info) {")
    j = app.index("\n  }\n", i) + 4
    src = "const T = (k, d) => d;\n" + app[i:j] + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(aaBarShort)));"
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_bar_says_one_short_line_and_keeps_the_rest_under_details():
    # (2026-11-26, Lam) the long "Ready to send. Accepted ... for you. Check it, then click Submit
    # on the site." stayed on the bar, over two lines, even after the owner submitted
    long_ready = ("Ready to send. Accepted Example Corp's Data Privacy Agreement for you. "
                  "Check it, then click Submit on the site.")
    ready, sent, wall, ask, short = _bar_short([
        {"state": "ready", "record_id": 7, "finish": True, "message": long_ready},
        {"state": "submitted", "record_id": 7, "message": "Sent, and the site confirmed it. Marked as applied in your Tracker."},
        {"state": "stopped", "message": "This site wants you to sign in first. Sign in in the Browser tab, then press Autofill this page again."},
        {"state": "ask", "message": "Are you willing to relocate to a city you have never lived in before for this role?"},
        {"state": "running", "message": "Reading the job page"},
    ])
    assert ready == {"line": "Your turn: check and submit", "details": long_ready, "one": True}
    assert sent["line"] == "Submitted. Marked as applied" and not sent["details"]
    assert wall["line"] == "This site wants you to sign in first." and wall["details"].startswith("This site")
    assert ask["line"].startswith("Are you willing") and not ask["details"]          # a question is never cut
    assert short == {"line": "Reading the job page", "details": "", "one": True}


def test_the_bar_clears_after_the_site_confirms_the_application():
    app = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    i = app.index("window.tailorShell.onApplied((info) => {")
    block = app[i:i + 900]
    assert 'T("fill.submitted"' in block and "say(null)" in block and "5000" in block
    assert "const short = aaBarShort(info);" in app


def test_a_preferred_name_box_is_never_asked():
    """(Meriton, 2026-11-26) The run stopped on an optional "Preferred First Name" and asked the
    owner. An optional preferred / nick name is left empty and never asked; a required one is the
    profile's first name (made-up values)."""
    from autoapply.fields import FormField
    from autoapply.planner import KnownContext, plan_known
    ctx = KnownContext(applicant={"first_name": "Avery", "last_name": "Example"})
    opt = FormField("p", "Preferred First Name", "text")
    req = FormField("q", "Preferred first name *", "text", required=True)
    nick = FormField("n", "Nickname", "text")
    res = plan_known([opt, req, nick], ctx)
    left = {f.ref for f in res.left}
    acts = {a.ref: a for a in res.actions}
    assert "p" not in left and "n" not in left and "p" not in acts and "n" not in acts
    assert acts["q"].source == "first_name"
    # no first name on file: the required one is the person's, the optional one still is not
    res2 = plan_known([opt, req], KnownContext(applicant={}))
    assert {f.ref for f in res2.left} == {"q"}


def test_other_please_specify_gets_its_box_filled(tmp_path):
    """(Meriton, 2026-11-26) "How did you hear about us?" has no job board choice: "Other (please
    specify)" is picked for the saved "Job board" AND its "Please specify" box gets "Job board"
    (it was left empty). A list with a job board choice takes that one, nothing to specify."""
    from autoapply.answers import AnswerLibrary
    from autoapply.fields import FormField
    from autoapply.planner import KnownContext, plan_known
    meriton = ["College Recruiting/Job Fair", "Company Website", "LinkedIn", "Indeed", "Military Program/SkillBridge",
               "Referred by a Current Employee (please specify)", "Referred by a Non-Employee (please specify)",
               "Other (please specify)"]
    lib = AnswerLibrary(tmp_path / "a.db")
    try:
        lib.save("How did you hear about us?", "Job board", "you")
        heard = FormField("h", "How did you hear about us?", "select", options=meriton, required=True)
        spec = FormField("s", "Please specify", "text")
        cur = FormField("c", "Are you a current or former employee of Example Co?", "select", options=["Yes", "No"], required=True)
        res = plan_known([heard, spec, cur], KnownContext(library=lib))
        acts = {a.ref: a.value for a in res.actions}
        assert acts.get("h") == "Other (please specify)" and acts.get("s") == "Job board", acts
        # a list that has a job board choice: that one, the specify box untouched
        heard2 = FormField("h2", "How did you hear about us?", "select", options=["LinkedIn", "Online Job Board", "Other (please specify)"])
        res2 = plan_known([heard2, FormField("s2", "Please specify", "text")], KnownContext(library=lib))
        acts2 = {a.ref: a.value for a in res2.actions}
        assert acts2.get("h2") == "Online Job Board" and "s2" not in acts2, acts2
    finally:
        lib.close()


def test_the_status_line_stays_one_short_line_when_the_agent_stops():
    """(Vestmark, 2026-11-26) The agent stopped on a consent box and its whole message ("This site
    needs you to accept its terms to apply, and I could not find where. Have a look at the page.")
    filled the bar over two lines. The bar keeps the counts; the agent's words go under Details,
    and any line over 80 characters keeps its first sentence."""
    app = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    g = app[app.index("  function guideSay(r) {"):]
    g = g[:g.index("\n  }\n")]
    assert "const head = guideLine(r);" in g and "said" in g
    s = app[app.index("  function say(msg) {"):]
    s = s[:s.index("\n  }\n")]
    assert "line.length > 80" in s and '"bb-fill-text is-one"' in s


def test_the_state_question_in_its_longer_wordings():
    """(Vestmark, 2026-11-26) "Please provide the US state in which you currently reside" was not
    read as the state, so the walk left it for the person; "State your reason..." is no state."""
    from autoapply.planner import contact_key
    assert contact_key("Please provide the US state in which you currently reside:") == "state"
    assert contact_key("What state do you currently live in?") == "state"
    assert contact_key("State") == "state" and contact_key("State/Province") == "state"
    assert contact_key("State your reason for applying") == "" and contact_key("State why you want this role") == ""


CCPA = ("California law requires that we provide you this notice about the collection and use of your "
        "personal information. Our Privacy Policy describes the categories of personal information that "
        "Example, Inc. collects about California residents who apply. For purposes of this notice, personal "
        "information has the meaning given in the California Consumer Privacy Act of 2018 (the CCPA). This "
        "notice does not create or form part of any contract for employment or otherwise.")


def test_a_typed_acknowledgement_under_the_standing_consent():
    """(Vestmark, 2026-11-26) A required box under a privacy notice that only acknowledges it: with
    the person's standing "always accept" for required notices it is filled ("I acknowledge", or
    the exact words the notice asks for); without it, it stays the person's. Never a signature,
    initials or a name."""
    from autoapply import consents as C
    from autoapply.fields import FormField
    from autoapply.planner import KnownContext, plan_known
    assert C.standing_answer(CCPA, [], "text", True, accept_notices=True) == (C.PICK, "I acknowledge", "consent")
    assert C.standing_answer(CCPA, [], "text", True, accept_notices=False) is None
    assert C.standing_answer('I have read the Privacy Notice. Please type "I agree" to confirm.', [], "text", True,
                             accept_notices=True)[1] == "I agree"
    assert C.standing_answer("By typing your full name below you certify the information provided is true. Signature",
                             [], "text", True, accept_notices=True) is None
    box = FormField("k", CCPA, "text", required=True)
    on = plan_known([box], KnownContext(accept_notices=True))
    off = plan_known([box], KnownContext(accept_notices=False))
    assert [(a.ref, a.value, a.origin) for a in on.actions] == [("k", "I acknowledge", "consent")]
    assert not off.actions and [f.ref for f in off.left] == ["k"]


def test_the_agent_tries_the_boxes_left_for_the_person_before_a_needs_consent_stop():
    agent = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    i = agent.index('done.reason === "needs-consent"')
    block = agent[i:i + 2600]
    assert "retryAsked: true" in block and block.index("retryAsked: true") < block.index('stopped("needs-consent"')
    walk = (ROOT / "shell-electron" / "guided.js").read_text(encoding="utf-8")
    assert "asked.add(ref)" in walk and "!asked.has(f.ref)" in walk
    main = (ROOT / "shell-electron" / "main.js").read_text(encoding="utf-8")
    assert "!!opts.retryAsked" in main


def test_suggestions_only_for_acknowledgements_and_the_bar_names_the_same_item():
    """(Vestmark, 2026-11-26, the owner) A box left for the person gets a suggestion when it only
    acknowledges a notice ("I acknowledge", typed in and selected on the page, never committed);
    a background or legal question never gets one. The bar says "1 needs you: <the item>", the
    same item the pointer is on (the page tells the shell, __sj_needs__)."""
    from autoapply.fields import FormField
    from autoapply.planner import KnownContext, plan_known
    res = plan_known([FormField("k", CCPA, "text", required=True),
                      FormField("b", "Have you ever been convicted of a felony?", "select", options=["Yes", "No"], required=True),
                      FormField("o", "Are you subject to any court order?", "text", required=True)], KnownContext())
    assert res.suggest == {"k": "I acknowledge"}
    app = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "if (needs && g && g.label) return needsLine(needs, g.label);" in app and "onNeeds((d)" in app
    main = (ROOT / "shell-electron" / "main.js").read_text(encoding="utf-8")
    assert '"__sj_needs__"' in main and '"assist:needs"' in main
    agent = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    assert "marked ? NOTICE_IN_BOX : blockedMessage(\"needs-consent\")" in agent
