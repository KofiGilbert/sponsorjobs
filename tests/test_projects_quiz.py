"""Defend it is a 20-question quiz (owner decision 2026-10-13), not a recorded interview.

Acceptance: the quiz is generated ONCE per build from the real files and the Learn it walkthrough
(one model call, inside the build's token budget, logged) and cached; the answer key never reaches
the client; grading is local (no model call); 14 of 20 passes and marks the project Verified, 13
fails and lists each wrong question with the right answer and a one-line explanation; a retake has
the same questions in a new order with the options reshuffled; publish and Add to resume refuse
until Verified; the recorded project-defence path is gone. Interview tie-in: a prep for a job whose
resume carries a Verified project passes that project's stored facts into the EXISTING question
generation, with no model call of its own. Offline: FakeLLM returns a deterministic quiz.
"""

import importlib
import json

import pytest

IDEA = {"title": "Spend stats CLI", "pitch": "Summarises spending numbers.", "tech": "Python, pytest",
        "skills": ["Python", "Testing"], "repo": "spend-stats-cli"}


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


def _built(A, c, learn=True):
    pid = c.post("/api/projects/start", json={"idea": IDEA, "local": True}).get_json()["project"]["id"]
    if learn:
        c.post(f"/api/projects/{pid}/learn")
        for i, s in enumerate(_store(A, pid)["learn"]["steps"]):
            ans = s["check"]["answer"] if s["check"]["kind"] == "choice" else s["check"]["accept"][0]
            assert c.post(f"/api/projects/{pid}/learn/{i}", json={"answer": ans}).get_json()["correct"]
    return pid


def _answers(A, pid, right):
    qz = _store(A, pid)["quiz"]
    att, qs = qz["attempt"], qz["questions"]
    out = []
    for k, qi in enumerate(att["order"]):
        good = att["opts"][k].index(qs[qi]["answer"])
        out.append(good if k < right else (good + 1) % 4)
    return att["id"], out


def _submit(A, c, pid, right):
    c.post(f"/api/projects/{pid}/quiz")
    aid, ans = _answers(A, pid, right)
    return c.post(f"/api/projects/{pid}/quiz/submit", json={"attempt": aid, "answers": ans}).get_json()


def test_quiz_is_made_once_in_the_build_and_cached(client, monkeypatch, capsys):
    A, c = client
    from llm.base import FakeLLM
    calls = []
    real = FakeLLM.generate_project_quiz
    monkeypatch.setattr(FakeLLM, "generate_project_quiz", lambda self, *a, **k: calls.append(1) or real(self, *a, **k))
    pid = _built(A, c)
    assert calls == [1]                                              # one call, in the build
    qz = _store(A, pid)["quiz"]
    assert len(qz["questions"]) == 20 and qz["tokens"] > 0
    assert "[projects] quiz" in capsys.readouterr().out             # its tokens are logged
    tokens = _store(A, pid)["build"]["tokens"]
    assert tokens["calls"] == 5 and tokens["total"] <= tokens["budget"] == 150_000   # plan, 2 file batches, Learn it, quiz
    # every code question's excerpt is cut from the real file
    for q in qz["questions"]:
        if q.get("code"):
            real_lines = (A._build_dir(pid) / "files" / q["path"]).read_text().splitlines()
            assert q["code"] == "\n".join(real_lines[q["start"] - 1:q["end"]])
    # opening, failing, retaking: never regenerated
    monkeypatch.setattr(FakeLLM, "generate_project_quiz", lambda *a, **k: pytest.fail("regenerated"))
    _submit(A, c, pid, 10)
    _submit(A, c, pid, 10)
    assert _store(A, pid)["quiz"]["questions"] == qz["questions"]


def test_quiz_is_made_on_open_when_the_build_missed_it(client, monkeypatch):
    A, c = client
    pid = _built(A, c)
    A._update_project(pid, lambda x: x.pop("quiz"))
    before = _store(A, pid)["build"]["tokens"]["total"]
    d = c.post(f"/api/projects/{pid}/quiz").get_json()
    assert len(d["questions"]) == 20
    assert _store(A, pid)["build"]["tokens"]["total"] > before        # counted in the same budget


def test_answer_key_never_reaches_the_client(client):
    A, c = client
    pid = _built(A, c)
    r = c.post(f"/api/projects/{pid}/quiz")
    d = r.get_json()
    assert d["total"] == 20 and d["pass_mark"] == 14 and len(d["questions"]) == 20
    assert [q["n"] for q in d["questions"]] == list(range(1, 21))
    for q in d["questions"]:
        assert set(q) <= {"n", "q", "options", "path", "start", "end", "code"} and len(q["options"]) == 4
    raw = r.get_data(as_text=True)
    for q in _store(A, pid)["quiz"]["questions"]:
        assert q["explain"] not in raw
    assert '"answer"' not in raw and '"explain"' not in raw and '"opts"' not in raw and '"order"' not in raw
    # reopening mid-quiz returns the same attempt, so local answers still line up
    again = c.post(f"/api/projects/{pid}/quiz").get_json()
    assert again["attempt"] == d["attempt"] and again["questions"] == d["questions"]


def test_quiz_waits_for_learn_it(client):
    A, c = client
    pid = _built(A, c, learn=False)
    assert c.post(f"/api/projects/{pid}/quiz").status_code == 409


def test_13_fails_with_explanations_and_14_verifies_graded_locally(client, monkeypatch):
    A, c = client
    pid = _built(A, c)
    assert c.post(f"/api/projects/{pid}/publish").status_code == 400
    assert c.post(f"/api/projects/{pid}/tocv").status_code == 400
    monkeypatch.setattr(A, "_make_llm", lambda *a, **k: pytest.fail("grading called a model"))
    miss = _submit(A, c, pid, 13)
    assert miss["score"] == 13 and miss["passed"] is False and miss["pass_mark"] == 14
    assert len(miss["wrong"]) == 7
    stored = {q["q"]: q for q in _store(A, pid)["quiz"]["questions"]}
    for w in miss["wrong"]:
        q = stored[w["q"]]
        assert w["correct"] == q["options"][q["answer"]] and w["explain"] == q["explain"]
        assert w["yours"] and w["yours"] != w["correct"]
    assert _store(A, pid)["status"] == "draft"
    assert c.post(f"/api/projects/{pid}/publish").status_code == 400
    assert c.post(f"/api/projects/{pid}/tocv").status_code == 400
    # a submitted attempt cannot be submitted again
    aid = _store(A, pid)["quiz"]["attempt"]["id"]
    assert c.post(f"/api/projects/{pid}/quiz/submit", json={"attempt": aid, "answers": []}).status_code == 409

    ok = _submit(A, c, pid, 14)
    assert ok["score"] == 14 and ok["passed"] is True
    p = _store(A, pid)
    assert p["status"] == "verified" and p["result"]["via"] == "quiz"
    pub = next(x for x in c.get("/api/projects").get_json()["projects"] if x["id"] == pid)
    assert pub["stage"] == "verified" and pub["defense"]["score"] == 14
    assert c.post(f"/api/projects/{pid}/tocv").status_code == 200
    assert c.post(f"/api/projects/{pid}/quiz").status_code == 409   # nothing left to defend


def test_retake_reshuffles_order_and_options_but_keeps_the_questions(client):
    A, c = client
    pid = _built(A, c)
    first = c.post(f"/api/projects/{pid}/quiz").get_json()
    aid, ans = _answers(A, pid, 5)
    c.post(f"/api/projects/{pid}/quiz/submit", json={"attempt": aid, "answers": ans})
    second = c.post(f"/api/projects/{pid}/quiz").get_json()
    assert second["attempt"] != first["attempt"]
    q1 = [q["q"] for q in first["questions"]]
    q2 = [q["q"] for q in second["questions"]]
    assert sorted(q1) == sorted(q2) and q1 != q2                      # same questions, new order
    opts1 = {q["q"]: q["options"] for q in first["questions"]}
    assert all(sorted(opts1[q["q"]]) == sorted(q["options"]) for q in second["questions"])
    assert any(opts1[q["q"]] != q["options"] for q in second["questions"])   # options reshuffled


def test_the_recorded_project_defence_is_gone(client):
    A, c = client
    pid = _built(A, c)
    assert c.post(f"/api/projects/{pid}/defend").status_code in (404, 405)
    assert c.post("/api/screens", json={"project_id": pid}).status_code == 404     # needs a prep now
    assert c.post(f"/api/projects/{pid}/test").status_code == 400                 # the quiz is the one path
    assert c.post(f"/api/projects/{pid}/grade", json={"answers": []}).status_code == 400
    assert not hasattr(A, "_project_screen_create") and not hasattr(A, "_project_defense_result")
    from llm.base import FakeLLM
    assert not hasattr(FakeLLM, "generate_project_interview")


# ---- Interview tie-in ------------------------------------------------------------------------
def _verified(A, c):
    pid = _built(A, c)
    assert _submit(A, c, pid, 20)["passed"]
    return pid


def _spy_llm(monkeypatch):
    """Every FakeLLM method call by name, and the kwargs of the screen question call."""
    from llm.base import FakeLLM
    names, seen = [], {}
    for name in [n for n in dir(FakeLLM) if not n.startswith("_") and callable(getattr(FakeLLM, n))]:
        real = getattr(FakeLLM, name)

        def wrap(self, *a, _n=name, _r=real, **k):
            names.append(_n)
            if _n == "generate_screen_questions":
                seen.update(k)
            return _r(self, *a, **k)
        monkeypatch.setattr(FakeLLM, name, wrap)
    return names, seen


def test_screen_for_a_job_with_a_verified_project_asks_about_it_without_a_new_call(client, monkeypatch):
    A, c = client
    _verified(A, c)
    # on the resume: Add to resume puts it among the saved profile's projects
    pid = _store(A, next(p["id"] for p in A._load_projects()))["id"]
    assert c.post(f"/api/projects/{pid}/tocv").status_code == 200
    prep = c.post("/api/preps", json={"role": "Data Analyst", "jd": "Python, testing, data."}).get_json()["prep"]
    names, seen = _spy_llm(monkeypatch)
    s = c.post("/api/screens", json={"prep_id": prep["id"], "questions": 5}).get_json()
    assert names == ["generate_screen_questions"]                    # no model call of its own
    facts = seen["projects"]
    assert [f["title"] for f in facts] == [IDEA["title"]]
    quiz = _store(A, pid)["quiz"]["questions"]
    assert any(q["q"] in " ".join(facts[0]["facts"]) for q in quiz)         # quiz content as context
    assert any(st["why"] in " ".join(facts[0]["facts"]) for st in _store(A, pid)["learn"]["steps"])
    texts = [q["text"] for q in s["questions"]]
    assert len(texts) == 5 and sum(IDEA["title"] in t for t in texts) == 1
    proj_q = next(q for q in s["questions"] if IDEA["title"] in q["text"])
    assert proj_q["competency"] == "Project"


def test_screen_without_a_verified_project_on_the_resume_is_unchanged(client, monkeypatch):
    A, c = client
    _verified(A, c)                                                  # verified, but not on the resume
    prep = c.post("/api/preps", json={"role": "Data Analyst", "jd": "SQL",
                                      "cv": "Jane Doe. Projects: Weather bot."}).get_json()["prep"]
    names, seen = _spy_llm(monkeypatch)
    s = c.post("/api/screens", json={"prep_id": prep["id"], "questions": 5}).get_json()
    assert names == ["generate_screen_questions"] and "projects" not in seen
    assert not any(IDEA["title"] in q["text"] for q in s["questions"])


def test_live_round_and_model_prompt_carry_the_project_facts(client, monkeypatch):
    A, c = client
    pid = _verified(A, c)
    # a tailored resume whose projects list the verified one by its title
    recs = A._records()
    rid = recs.add("Data Analyst", "Acme", 80.0, "", "Data Analyst",
                   data={"render_profile": {"projects": [{"org": IDEA["title"], "bullets": ["x"]}]},
                         "jd": "Python and testing."})
    recs.close()
    prep = {"id": "p1", "role": "Data Analyst", "company": "Acme", "jd": "Python and testing.",
            "record_id": str(rid), "questions": [{"q": f"Q{i}", "competency": "General"} for i in range(6)]}
    facts = A._verified_project_facts(prep)
    assert [f["title"] for f in facts] == [IDEA["title"]]
    plan = A._round2_plan(prep, facts)
    assert len(plan) == 6 and plan[2]["competency"] == "Project" and IDEA["title"] in plan[2]["q"]
    brief = A._round2_briefing(prep, {}, facts)
    assert "PROJECTS THEY BUILT" in brief and facts[0]["facts"][0] in brief
    # a resume without it: nothing added
    assert A._verified_project_facts(dict(prep, record_id=None, cv="Projects: Weather bot")) == []
    assert A._round2_plan(dict(prep), []) == A._round2_plan(prep)

    # the real model's prompt: the facts ride in the same screen-question call
    from llm.anthropic_client import AnthropicLLM
    llm = object.__new__(AnthropicLLM)
    sent = {}
    llm._complete = lambda system, user, **k: sent.update(system=system, user=user) or json.dumps({"questions": []})
    llm.generate_screen_questions({}, "Data Analyst", "Acme", "jd", kinds=["motivation", "behavioural",
                                  "behavioural", "behavioural", "situational"], projects=facts)
    assert "PROJECTS THEY BUILT" in sent["user"] and IDEA["title"] in sent["user"]
    assert facts[0]["facts"][0] in sent["user"] and "exactly 1 of the behavioural" in sent["system"]
