"""Answers in the application's panel (Auto-apply, Waiting on you), 2026-11-19.

A quiet (Auto) run leaves an application at Ready to send with its answers, and the daily summary
says "6 answers need your check", but the only list of them was Review answers under the bar of
a VISIBLE run. Now:

* the shell sends its Review answers list with the ready post, self-identification values left
  out (they stay in the shell, as on the bar), and the engine keeps it on the record
  (agent_ready.answers);
* GET /api/autoapply/item/<id> returns the stored rows while the application waits at Ready to
  send, and the board's row says how many lines to check ("N to check");
* the panel paints them under "What will be sent" with the bar's own row and words, the lines to
  check first, a long list collapsed (tests/js/test_answers_panel.js);
* Send all's "Review them first" opens that panel at its Answers;
* every new string is in the five locales, with no dashes.
"""
from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# The apply agent is off by default (assisted apply only); these tests use its routes, so it is on here.
pytestmark = pytest.mark.usefixtures("apply_agent_on")

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}
GH_URL = "https://boards.greenhouse.io/brightwater/jobs/1"
SELF = "Zyxwv Selfid Value"


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as m
    importlib.reload(m)
    monkeypatch.setattr(m, "_active_channel", lambda: None)
    # the board's open-or-closed sweep would ask the (made-up) posting over the network in a
    # background thread and write to the same records while the test does
    monkeypatch.setattr(m, "_liveness_kick", lambda rows: False)
    return m


def _add(mod, role, company, **data):
    from ui.records import CVRecords
    recs = CVRecords(mod.DB_PATH)
    try:
        return recs.add(role, company, 80, "", data={"status": "approved", "fit": 80, "source_job": {"url": GH_URL}, **data})
    finally:
        recs.close()


ANSWERS = {
    "rows": [
        {"page": 1, "label": "Phone", "value": "555 0100", "from": "profile", "flag": ""},
        {"page": 1, "label": "Have you ever been convicted of a felony?", "value": "No", "from": "best_guess",
         "flag": "best_guess", "evidence": "nothing you saved mentions one", "klass": "legal"},
        # a self-identification value that reached the engine anyway is dropped here too
        {"page": 2, "label": "Gender", "value": SELF, "from": "eeo", "flag": "", "self_id": True},
        {"page": 2, "label": "Portfolio", "value": "", "from": "left_empty", "flag": "left_empty"},
        {"page": 2, "label": "Field of Study", "value": "Economics", "from": "closest", "flag": "closest"},
        {"page": 2, "label": "", "value": "x", "from": "ai", "flag": "ai"},                 # no question: dropped
        {"page": 3, "label": "Odd", "value": "y", "from": "<script>", "flag": "made up"},  # unknown codes: cleared
    ],
    "files": [{"kind": "resume", "name": "Sam Lee Resume.pdf", "record_id": 4, "flag": "other_resume"}],
    "flagged": 99,                                                                      # recounted, never trusted
}


def test_the_ready_post_keeps_the_answers_and_the_item_route_returns_them(mod):
    rid = _add(mod, "Data Analyst", "Brightwater Freight")
    c = mod.app.test_client()
    r = c.post("/api/autoapply/agent/ready", headers=H,
               json={"record_id": rid, "sig": "abc", "run": {"url": GH_URL}, "answers": ANSWERS}).get_json()
    assert r["ok"]
    kept = mod._record_data(mod._get_record(rid))["agent_ready"]["answers"]
    assert SELF not in json.dumps(kept), "a self-identification value is never kept on the record"

    it = c.get(f"/api/autoapply/item/{rid}", headers=H).get_json()
    assert it["action"] == "send"
    a = it["answers"]
    labels = [x["label"] for x in a["rows"]]
    assert labels == ["Phone", "Have you ever been convicted of a felony?", "Gender", "Portfolio", "Field of Study", "Odd"]
    felony = a["rows"][1]
    assert felony["from"] == "best_guess" and felony["flag"] == "best_guess"
    assert felony["evidence"] == "nothing you saved mentions one" and felony["value"] == "No"
    gender = a["rows"][2]
    assert gender["self_id"] and gender["held"] and gender["value"] == "" and gender["from"] == "eeo"
    assert a["rows"][5]["from"] == "" and a["rows"][5]["flag"] == ""
    assert a["files"] == [{"kind": "resume", "name": "Sam Lee Resume.pdf", "record_id": 4, "flag": "other_resume"}]
    # best guess + closest + the other application's file; an optional box left empty is not counted
    assert a["flagged"] == 3

    # the row on Waiting on you carries the count for its "N to check" marker
    board = c.get("/api/autoapply/board", headers=H).get_json()
    row = next(x for x in board["tiles"]["waiting"] if x["id"] == rid)
    assert row["answers_n"] == 7 and row["answers_check"] == 3


def test_no_answers_when_none_are_stored_or_the_application_is_not_waiting_at_ready(mod):
    c = mod.app.test_client()
    plain = _add(mod, "Engineer", "Larkspur Labs")
    it = c.get(f"/api/autoapply/item/{plain}", headers=H).get_json()
    assert it["answers"] is None and it["answers_check"] == 0
    old = _add(mod, "Writer", "Harlow Press", agent_ready={"sig": "x", "run": {}})     # stored before this change
    it = c.get(f"/api/autoapply/item/{old}", headers=H).get_json()
    assert it["action"] == "send" and it["answers"] is None and it["answers_check"] == 0
    # sent: the ready view (and its answers) is cleared
    rid = _add(mod, "Analyst", "Pinecrest Studio")
    c.post("/api/autoapply/agent/ready", headers=H, json={"record_id": rid, "sig": "s", "answers": ANSWERS})
    c.post("/api/autoapply/agent/ready", headers=H, json={"record_id": rid, "clear": True})
    assert c.get(f"/api/autoapply/item/{rid}", headers=H).get_json()["answers"] is None
    assert mod._ready_answers_clean(None) is None and mod._ready_answers_clean([1]) is None


def test_an_application_ready_before_this_change_falls_back_to_the_answers_the_agent_chose(mod):
    """Records that reached Ready to send before agent_ready.answers was kept still hold the
    answers the agent chose itself (agent_answers, #311): the panel lists those, a best guess
    and a legal answer flagged, the count the same rule."""
    chosen = [
        {"page": 1, "label": "Do you have full work rights in the country the position is located?", "value": "Yes",
         "source": "best_guess", "evidence": "you saved US work authorization", "class": "legal"},
        {"page": 1, "label": "How did you hear about us?", "value": "Job board", "source": "common_sense", "evidence": "", "class": ""},
        {"page": 2, "label": "Have you been convicted of a crime?", "value": "No", "source": "history",
         "evidence": "nothing in your history", "class": "legal"},
        {"page": 2, "label": "Highest degree", "value": "Master's Degree", "source": "history", "evidence": "edu1", "class": "factual"},
        {"page": 2, "label": "Are you a veteran?", "value": SELF, "source": "profile", "evidence": "", "class": ""},
    ]
    rid = _add(mod, "Data Analyst", "Brightwater Freight", agent_ready={"sig": "x", "run": {}}, agent_answers=chosen)
    c = mod.app.test_client()
    it = c.get(f"/api/autoapply/item/{rid}", headers=H).get_json()
    a = it["answers"]
    assert [x["label"] for x in a["rows"]] == [x["label"] for x in chosen]
    work, heard, crime, degree, vet = a["rows"]
    assert work["flag"] == "best_guess" and work["from"] == "best_guess" and work["evidence"] == "you saved US work authorization"
    assert heard["flag"] == "" and heard["from"] == "common_sense"
    assert crime["flag"] == "agent" and crime["evidence"] == "nothing in your history"
    assert degree["flag"] == "" and degree["from"] == "history" and degree["value"] == "Master's Degree"
    assert vet["self_id"] and vet["held"] and vet["value"] == ""
    assert SELF not in json.dumps(it)
    assert a["files"] == [] and a["flagged"] == 2
    row = next(x for x in c.get("/api/autoapply/board", headers=H).get_json()["tiles"]["waiting"] if x["id"] == rid)
    assert row["answers_n"] == 5 and row["answers_check"] == 2
    # the full list, once a run sends it, wins over the fallback
    c.post("/api/autoapply/agent/ready", headers=H, json={"record_id": rid, "sig": "y", "answers": ANSWERS})
    assert len(c.get(f"/api/autoapply/item/{rid}", headers=H).get_json()["answers"]["rows"]) == 6
    # not waiting at Ready to send: no fallback either
    other = _add(mod, "Engineer", "Larkspur Labs", agent_answers=chosen)
    assert c.get(f"/api/autoapply/item/{other}", headers=H).get_json()["answers"] is None


def test_the_panel_rendering_model_under_node():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_answers_panel.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr


def test_the_panel_paints_answers_under_what_will_be_sent_and_send_all_opens_it():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    # the module loads before app.js
    assert html.index("answers_panel.js") < html.index("filename='app.js'")
    # under "What will be sent", in the same column, above Progress
    d = js[js.index("function aaDrawerHtml(it, loading)"):]
    d = d[:d.index("aaConfirmation(it)")]
    assert d.index('"aa.willSend"') < d.index("aaDrAnsHTML(it)") < d.index('"aa.progress"')
    # the bar's own row, origin words and reasons
    panel = js[js.index("function aaDrAnsModel("):js.index("function aaAgentBar(info)")]
    for name in ("aaAnsFrom", "aaAnsFlag(r.flag, r.fact, r.value, r.evidence)", "aaAnsRowHTML(", "aa.ans.toCheck", "aa.ans.local"):
        assert name in panel, name
    bar = js[js.index("function aaAnsHTML(list, rid)"):js.index("function aaAnsWordsFor(rid)")]
    assert "aaAnsRowHTML" in bar, "the bar and the panel share one row"
    # self-identification values from the shell's own list while it holds the run
    assert "sh.agentReady(Number(id))" in panel
    # Send all's Review them first: the panel at its Answers
    sa = js[js.index("async function aaSendAll()"):]
    sa = sa[:sa.index('$("#aaSendAll")')]
    assert 'aaOpen(Number(d.review), { focus: "answers" })' in sa
    assert 'focus === "answers"' in js and '$("#aaDrAns")' in js
    # the row's marker
    assert "aaCheckMark(it)" in js and "it.answers_check" in js


def test_the_shell_sends_the_list_without_self_identification_values():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_ready_answers.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr
    main = (ROOT / "shell-electron" / "main.js").read_text(encoding="utf-8")
    assert "answers: agent.readyList(recordId)" in main


def test_every_new_string_is_in_five_locales_without_dashes():
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    panel = js[js.index("function aaDrAnsModel("):js.index("function aaAgentBar(info)")]
    keys = set(re.findall(r'T\("(aa\.ans\.[A-Za-z0-9_.]+)"', panel))
    assert {"aa.ans.panelTitle", "aa.ans.showAll", "aa.ans.showFewer", "aa.ans.held"} <= keys
    for code in ("en", "de", "es", "fr", "pt"):
        d = json.loads((ROOT / "ui" / "static" / "locales" / f"{code}.json").read_text(encoding="utf-8"))
        assert keys <= set(d), f"{code}.json misses {sorted(keys - set(d))}"
        for k in keys:
            assert not re.search("[–—]", d[k]), (code, k)
    en = json.loads((ROOT / "ui" / "static" / "locales" / "en.json").read_text(encoding="utf-8"))
    assert en["aa.ans.showAll"] == "Show all {n}" and not re.search(r"\bCV\b", " ".join(en[k] for k in keys))
