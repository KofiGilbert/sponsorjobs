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
        q, perm = qs[qi], att["opts"][k]
        if q.get("type") == "multi":
            good = sorted(perm.index(i) for i in q["answers"])
            out.append(good if k < right else good[:-1])            # one right option short: wrong
        else:
            good = perm.index(q["answer"])
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
        assert set(q) <= {"n", "q", "type", "options", "path", "start", "end", "code"} and len(q["options"]) == 4
        assert q["type"] in ("single", "multi")
    assert {q["type"] for q in d["questions"]} == {"single", "multi"}
    raw = r.get_data(as_text=True)
    for q in _store(A, pid)["quiz"]["questions"]:
        assert q["explain"] not in raw
    assert '"answer"' not in raw and '"answers"' not in raw and '"explain"' not in raw and '"opts"' not in raw and '"order"' not in raw
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
        from ui import project_builder as PB
        right = [q["options"][i] for i in PB.quiz_correct(q)]
        if q.get("type") == "multi":
            assert w["type"] == "multi" and sorted(w["correct"]) == sorted(right)
            assert w["yours"] and set(w["yours"]) < set(w["correct"])
        else:
            assert w["type"] == "single" and w["correct"] == right[0]
            assert w["yours"] and w["yours"] != w["correct"]
        assert w["explain"] == q["explain"]
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


# ---- Single and "Select all that apply" questions (2026-10-13) -------------------------------
def _pb_quiz():
    from llm.base import FakeLLM
    from ui import project_builder as PB
    return PB, PB.quiz_questions(FakeLLM().generate_project_quiz({"title": "T"}, [], [])["questions"], [])


def test_fake_quiz_mixes_single_and_multi_with_a_server_side_key():
    PB, qs = _pb_quiz()
    assert len(qs) == 20
    kinds = [q["type"] for q in qs]
    assert kinds.count("multi") == 5 and kinds.count("single") == 15
    for q in qs:
        if q["type"] == "multi":
            assert 2 <= len(q["answers"]) <= 4 and "answer" not in q
        else:
            assert 0 <= q["answer"] < 4 and "answers" not in q
    assert any(PB._ABOVE.match(o) for q in qs for o in q["options"])      # "None of the above" is used


def test_normaliser_downgrades_a_one_answer_multi_and_drops_an_ambiguous_one():
    from ui import project_builder as PB
    raw = [{"q": "One right", "type": "multi", "answers": [2], "options": ["a", "b", "c", "d"]},
           {"q": "Above in multi", "type": "multi", "answers": [0, 3], "options": ["a", "b", "c", "All of the above"]},
           {"q": "Bad index", "type": "multi", "answers": [0, 7], "options": ["a", "b", "c", "d"]},
           {"q": "Old style", "answer": 1, "options": ["a", "b", "c", "d"]}]
    qs = PB.quiz_questions(raw, [])
    assert [(q["q"], q["type"]) for q in qs] == [("One right", "single"), ("Old style", "single")]
    assert qs[0]["answer"] == 2


def _attempt_for(PB, qs, seed):
    import random
    return PB.quiz_attempt(qs, random.Random(seed))


def _right_answers(PB, qs, att):
    out = []
    for k, qi in enumerate(att["order"]):
        perm, right = att["opts"][k], PB.quiz_correct(qs[qi])
        shown = [perm.index(i) for i in right]
        out.append(shown[::-1] if qs[qi].get("type") == "multi" else shown[0])   # pick order is irrelevant
    return out


def test_multi_is_right_only_for_the_exact_set():
    PB, qs = _pb_quiz()
    att = _attempt_for(PB, qs, 1)
    right = _right_answers(PB, qs, att)
    assert PB.grade_quiz(qs, att, right)["score"] == 20
    k = next(k for k, qi in enumerate(att["order"]) if qs[qi]["type"] == "multi")
    perm = att["opts"][k]
    good = sorted(right[k])
    extra = next(j for j in range(4) if j not in good)
    for bad in (good[:-1], good + [extra], [], [extra], None, 2, good + [9], [True, False]):
        ans = list(right)
        ans[k] = bad
        res = PB.grade_quiz(qs, att, ans)
        assert res["score"] == 19, bad
        (w,) = res["wrong"]
        q = qs[att["order"][k]]
        assert w["type"] == "multi" and sorted(w["correct"]) == sorted(q["options"][i] for i in q["answers"])
        assert isinstance(w["yours"], list)
    ans = list(right)
    ans[k] = good + good                                                  # duplicates collapse to the set
    assert PB.grade_quiz(qs, att, ans)["score"] == 20
    assert perm  # the shown order is the attempt's


def test_reshuffle_keeps_multi_and_single_correctness_and_above_stays_last():
    PB, qs = _pb_quiz()
    import random
    prev = None
    for seed in range(25):
        att = PB.quiz_attempt(qs, random.Random(seed), prev)
        assert PB.grade_quiz(qs, att, _right_answers(PB, qs, att))["score"] == 20
        shown = PB.public_quiz(qs, att)
        for k, qi in enumerate(att["order"]):
            assert shown[k]["type"] == qs[qi]["type"]
            if any(PB._ABOVE.match(o) for o in qs[qi]["options"]):
                assert PB._ABOVE.match(shown[k]["options"][-1])
        prev = att
    perms = {tuple(PB.quiz_attempt(qs, random.Random(s))["opts"][0]) for s in range(25)}
    assert len(perms) > 1


def test_an_old_single_only_quiz_still_opens_and_grades():
    from ui import project_builder as PB
    import random
    old = [{"q": f"Q{i}", "options": ["a", "b", "c", "d"], "answer": i % 4, "explain": "x", "topic": "parts"}
           for i in range(20)]                                            # cached before types existed
    att = PB.quiz_attempt(old, random.Random(3))
    assert {q["type"] for q in PB.public_quiz(old, att)} == {"single"}
    ans = [att["opts"][k].index(old[qi]["answer"]) for k, qi in enumerate(att["order"])]
    assert PB.grade_quiz(old, att, ans)["score"] == 20
    ans[0] = (ans[0] + 1) % 4
    res = PB.grade_quiz(old, att, ans)
    assert res["score"] == 19 and res["wrong"][0]["type"] == "single" and isinstance(res["wrong"][0]["correct"], str)


def test_old_cached_quiz_works_through_the_endpoints(client):
    A, c = client
    pid = _built(A, c)
    A._update_project(pid, lambda x: x["quiz"].update(
        questions=[{k: v for k, v in q.items() if k not in ("type", "answers")} | (
            {"answer": q["answers"][0]} if q.get("type") == "multi" else {})
            for q in x["quiz"]["questions"]], attempt=None))
    d = c.post(f"/api/projects/{pid}/quiz").get_json()
    assert {q["type"] for q in d["questions"]} == {"single"}
    assert _submit(A, c, pid, 20)["score"] == 20


def test_multi_answers_submit_as_lists_through_the_endpoint(client):
    A, c = client
    pid = _built(A, c)
    c.post(f"/api/projects/{pid}/quiz")
    aid, ans = _answers(A, pid, 20)
    assert any(isinstance(a, list) for a in ans)
    r = c.post(f"/api/projects/{pid}/quiz/submit", json={"attempt": aid, "answers": ans}).get_json()
    assert r["score"] == 20 and r["passed"] is True


def test_learn_it_check_is_unchanged():
    from ui import project_builder as PB
    step = {"check": {"kind": "choice", "q": "?", "options": ["a", "b", "c"], "answer": 1, "explain": "e"}}
    assert PB.check_answer(step, 1) and PB.check_answer(step, "1")
    assert not PB.check_answer(step, 0) and not PB.check_answer(step, None) and not PB.check_answer(step, [1])
    pub = PB.public_step({**step, "title": "t", "path": "a.py", "start": 1, "end": 2, "code": "x"})
    assert "answer" not in pub["check"] and pub["check"]["options"] == ["a", "b", "c"]
