"""Learn it and Defend it on one split page (owner decision 2026-10-13): the generator gives each
Learn it step a focus range and a one-line why per option, and each quiz question a topic; old
cached steps and quizzes without them still work; the answer key and the per-option lines never
reach the client before a Check; marks for review persist with the saved answers; Submit stays off
until all 20 are answered. Offline: FakeLLM, and the pure client helpers run under node."""

import importlib
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "ui" / "static" / "app.js").read_text()
IDEA = {"title": "Spend stats CLI", "pitch": "Summarises spending numbers.", "tech": "Python, pytest",
        "skills": ["Python"], "repo": "spend-stats-cli"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._BUILD_INLINE = True
    return A, A.app.test_client()


def _store(A, pid):
    return next(p for p in A._load_projects() if p["id"] == pid)


def _files():
    from llm.base import FakeLLM
    return [{"path": p, "content": c} for p, c in FakeLLM._FAKE_PROJECT.items()]


def test_fake_steps_carry_focus_and_a_why_per_option():
    from llm.base import FakeLLM
    from ui import project_builder as PB
    steps = PB.learn_steps(FakeLLM().generate_learn_steps({"title": "t"}, _files())["steps"], _files())
    assert len(steps) >= 3
    for s in steps:
        a, b = s["focus"]
        assert s["start"] <= a <= b <= s["end"]
        c = s["check"]
        if c["kind"] == "choice":
            assert len(c["whys"]) == len(c["options"]) and all(c["whys"])
    assert any(s["focus"] != [s["start"], s["end"]] for s in steps)       # a real, narrower focus


def test_focus_is_clamped_and_falls_back_to_the_whole_excerpt():
    from ui import project_builder as PB
    files = [{"path": "big.py", "content": "\n".join(f"x{i} = {i}" for i in range(1, 301))}]
    chk = {"kind": "choice", "q": "?", "options": ["a", "b", "c"], "answer": 0, "explain": "e"}
    raw = [{"title": "t", "path": "big.py", "start": 10, "end": 200, "focus": [90, 999], "check": chk},
           {"title": "u", "path": "big.py", "start": 5, "end": 20, "focus": "nope", "check": chk},
           {"title": "v", "path": "big.py", "start": 5, "end": 20, "check": dict(chk, whys=["only one"])}]
    s1, s2, s3 = PB.learn_steps(raw, files)
    assert (s1["start"], s1["end"]) == (10, 129) and s1["focus"] == [90, 129]   # 120-line cap, focus clamped
    assert s2["focus"] == [5, 20] and s3["focus"] == [5, 20]
    assert "whys" not in s3["check"]                                           # not one per option: dropped
    old = {k: v for k, v in s2.items() if k != "focus"}                        # cached before focus existed
    assert PB.public_step(old)["focus"] == [5, 20]


def test_learn_payload_has_focus_but_no_key_and_check_returns_the_picked_why(client):
    A, c = client
    pid = c.post("/api/projects/start", json={"idea": IDEA, "local": True}).get_json()["project"]["id"]
    r = c.post(f"/api/projects/{pid}/learn")
    raw, d = r.get_data(as_text=True), r.get_json()
    assert all(len(s["focus"]) == 2 for s in d["steps"])
    assert '"answer"' not in raw and '"whys"' not in raw and '"accept"' not in raw
    step = _store(A, pid)["learn"]["steps"][0]
    right, wrong = step["check"]["answer"], (step["check"]["answer"] + 1) % len(step["check"]["options"])
    no = c.post(f"/api/projects/{pid}/learn/0", json={"answer": wrong}).get_json()
    assert no["correct"] is False and no["why"] == step["check"]["whys"][wrong] and no["answer"] == ""
    yes = c.post(f"/api/projects/{pid}/learn/0", json={"answer": right}).get_json()
    assert yes["correct"] is True and yes["why"] == step["check"]["whys"][right]


def test_old_cached_steps_without_focus_or_whys_still_work(client):
    A, c = client
    pid = c.post("/api/projects/start", json={"idea": IDEA, "local": True}).get_json()["project"]["id"]
    c.post(f"/api/projects/{pid}/learn")

    def strip(x):
        for s in x["learn"]["steps"]:
            s.pop("focus", None)
            s["check"].pop("whys", None)
    A._update_project(pid, strip)
    d = c.post(f"/api/projects/{pid}/learn").get_json()
    assert all(s["focus"] == [s["start"], s["end"]] for s in d["steps"])
    step = _store(A, pid)["learn"]["steps"][0]
    res = c.post(f"/api/projects/{pid}/learn/0", json={"answer": step["check"]["answer"]}).get_json()
    assert res["correct"] is True and res["why"] == "" and res["explain"]      # the client falls back to explain


def test_quiz_grade_carries_topics_and_tolerates_old_quizzes():
    from llm.base import FakeLLM
    from ui import project_builder as PB
    qs = PB.quiz_questions(FakeLLM().generate_project_quiz({"title": "t"}, _files(), [])["questions"], _files())
    assert len(qs) == 20 and all(q["topic"] in PB.QUIZ_TOPICS for q in qs)
    att = PB.quiz_attempt(qs, random.Random(1))
    ans = []
    for k, qi in enumerate(att["order"]):
        q, perm = qs[qi], att["opts"][k]
        ans.append(sorted(perm.index(i) for i in q["answers"]) if q["type"] == "multi" else perm.index(q["answer"]))
    ans[0] = None
    res = PB.grade_quiz(qs, att, ans)
    assert res["score"] == 19 and res["wrong"][0]["topic"] == qs[att["order"][0]]["topic"]
    assert [i["n"] for i in res["items"]] == list(range(1, 21)) and sum(i["ok"] for i in res["items"]) == 19
    assert sum(t["total"] for t in res["topics"]) == 20 and sum(t["right"] for t in res["topics"]) == 19
    assert [t["topic"] for t in res["topics"]] == [t for t in PB.QUIZ_TOPICS if any(q["topic"] == t for q in qs)]
    old = [{k: v for k, v in q.items() if k != "topic"} for q in qs]           # cached before topics
    res = PB.grade_quiz(old, att, ans)
    assert res["score"] == 19 and res["topics"] == [] and res["wrong"][0]["topic"] == ""


def test_answer_key_and_topics_stay_off_the_open_quiz(client):
    A, c = client
    pid = c.post("/api/projects/start", json={"idea": IDEA, "local": True}).get_json()["project"]["id"]
    c.post(f"/api/projects/{pid}/learn")
    for i, s in enumerate(_store(A, pid)["learn"]["steps"]):
        ans = s["check"]["answer"] if s["check"]["kind"] == "choice" else s["check"]["accept"][0]
        c.post(f"/api/projects/{pid}/learn/{i}", json={"answer": ans})
    raw = c.post(f"/api/projects/{pid}/quiz").get_data(as_text=True)
    for k in ('"answer"', '"answers"', '"explain"', '"order"', '"opts"', '"topic"', '"whys"'):
        assert k not in raw


def _node(block_start, block_end, script, tmp_path):
    block = APP[APP.index(block_start):APP.index(block_end)]
    js = tmp_path / "t.js"
    js.write_text(block + "\n" + script)
    return json.loads(subprocess.run(["node", str(js)], capture_output=True, text=True, check=True).stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_marks_persist_with_the_saved_answers_and_submit_waits_for_all(tmp_path):
    out = _node("// <quiz-state>", "// </quiz-state>", """
      const saved = JSON.parse(JSON.stringify(quizSnapshot({ attempt: "a1", answers: [1, [0, 2], null], flags: [false, true, true], i: 2 })));
      const same = quizRestore(saved, "a1", 4), other = quizRestore(saved, "a2", 4), none = quizRestore(null, "a1", 3);
      const full = quizTally([0, [1, 2], 3], [true]), part = quizTally([0, null, []], []);
      console.log(JSON.stringify({ same, other, none, full, part }));
    """, tmp_path)
    assert out["same"] == {"answers": [1, [0, 2], None, None], "flags": [False, True, True, False], "i": 2}
    assert out["other"] == {"answers": [None] * 4, "flags": [False] * 4, "i": 0}    # a retake starts clean
    assert out["none"]["flags"] == [False] * 3
    assert out["full"] == {"answered": 3, "left": 0, "marked": 1, "canSubmit": True}
    assert out["part"] == {"answered": 1, "left": 2, "marked": 0, "canSubmit": False}
    submit = APP[APP.index("async function submitQuiz("):APP.index("function resultRows(")]
    assert "canSubmit) return" in submit                                      # the button and the call both wait
    review = APP[APP.index("function renderQuizReview("):APP.index("async function submitQuiz(")]
    assert """t.canSubmit ? "" : 'disabled""" in review


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_folds_and_line_chips(tmp_path):
    out = _node("// <code-fold>", "// </code-fold>", """
      console.log(JSON.stringify({
        short: foldPlan(1, 40, [10, 12]), long: foldPlan(1, 100, [50, 55]),
        top: foldPlan(1, 100, [50, 55], { top: true }), edge: foldPlan(1, 100, [2, 4]),
        refs: lineRefs("See line 8, then lines 10 to 11, and line 99.", 1, 16).map(r => [r.a, r.b]) }));
    """, tmp_path)
    assert out["short"] == {"from": 1, "to": 40, "above": None, "below": None}
    assert out["long"] == {"from": 44, "to": 61, "above": [1, 43], "below": [62, 100]}
    assert out["top"]["from"] == 1 and out["top"]["above"] is None
    assert out["edge"]["above"] is None and out["edge"]["from"] == 1
    assert out["refs"] == [[8, 8], [10, 11]]                                   # line 99 is outside the excerpt


def test_split_markup_and_accessibility_hooks():
    html = (ROOT / "ui" / "templates" / "index.html").read_text()
    css = (ROOT / "ui" / "static" / "styles.css").read_text()
    assert 'id="projProg"' in html and "projLearnRail" not in html and "projQuizRail" not in html
    assert "minmax(520px, 1fr) minmax(400px, 520px)" in css and "@container sx (max-width: 699px)" in css
    assert "@media (max-height: 640px)" in css
    panel = APP[APP.index("function codePanel("):APP.index("function wireCode(")]
    assert 'role="region"' in panel and 'tabindex="0"' in panel
    row = APP[APP.index("function optRow("):APP.index("function syncOpts(")]
    assert 'type="${multi ? "checkbox" : "radio"}"' in row
    assert 'id="learnFb" aria-live="polite"' in APP


def test_learn_checks_are_always_multiple_choice(client):
    """Owner decision 2026-10-13: every Learn it check is A to D multiple choice, never typed."""
    A, c = client
    pid = c.post("/api/projects/start", json={"idea": IDEA, "local": True}).get_json()["project"]["id"]
    d = c.post(f"/api/projects/{pid}/learn").get_json()
    assert d["steps"] and all(s["check"]["kind"] == "choice" and len(s["check"]["options"]) >= 3 for s in d["steps"])


def test_typed_checks_cached_earlier_are_converted_once(client):
    A, c = client
    pid = c.post("/api/projects/start", json={"idea": IDEA, "local": True}).get_json()["project"]["id"]
    c.post(f"/api/projects/{pid}/learn")

    def make_typed(x):
        x["learn"]["steps"][1]["check"] = {"kind": "line", "q": "Which function is exported?",
                                           "accept": ["summarize"], "explain": "__all__ lists it."}
    A._update_project(pid, make_typed)
    d = c.post(f"/api/projects/{pid}/learn").get_json()
    chk = d["steps"][1]["check"]
    assert chk["kind"] == "choice" and len(chk["options"]) == 4 and "answer" not in chk
    stored = _store(A, pid)["learn"]["steps"][1]["check"]
    assert stored["kind"] == "choice" and stored["options"][stored["answer"]] == "summarize"
    # cached: a second open makes no further conversion
    calls = []
    import llm.base as B
    orig = B.FakeLLM.choice_checks_for
    B.FakeLLM.choice_checks_for = lambda self, items: (calls.append(items), orig(self, items))[1]
    try:
        c.post(f"/api/projects/{pid}/learn")
    finally:
        B.FakeLLM.choice_checks_for = orig
    assert calls == []
