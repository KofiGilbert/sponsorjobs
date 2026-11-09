"""Build a project from the resume chat (one-brain plan PRs 7, 8, 9) and the one-line project row.

PR 7: project ideas in the chat are aimed at the job open in the chat (its role and the skills its
JD names), not the last saved application; the daily ideas cap holds; with the agent on, an ideas
turn puts no placeholder project on the page. PR 8: an ideas card's "Build this for this job"
click starts the Projects page's own build (one package, once) marked ``for_resume``. PR 9: a
passed quiz adds the project to that resume exactly once (and always to the saved profile); a
fail adds nothing; another job's session is untouched; a private repo carries no link until
/publish back-fills it; the add from the quiz thread waits for the session's lock. And every
project prints as ONE line: name (and link arrow) left, tools right, never "Personal Project".
FakeLLM only: the build is the offline fake build, no model and no GitHub.
"""

from __future__ import annotations

import importlib
import threading
import time

import pytest

from agent import resume_chat as rc
from conftest import requires_latex
from tailoring.assembler import normalize_profile, project_row, render_cv
from tailoring.keywords import skill_terms

JD = ("Business Intelligence Analyst at Northwind. Required: SQL, Power BI, Tableau, Python, "
      "KPI dashboards, stakeholder management, data modeling.")
ROLE, COMPANY = "Business Intelligence Analyst", "Northwind"


@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv(rc.FLAG, "1")
    import ui.app as app
    importlib.reload(app)
    app._BUILD_INLINE = True
    monkeypatch.setattr(app, "_add_examples", lambda ideas: True)      # no GitHub search
    yield app
    app._SESSION.pop("s", None)


def _live(A, template_source, workdir, jobname="cvlive"):
    from llm.base import FakeLLM
    from ui.records import CVRecords
    from ui.session import WebIntake
    s = WebIntake(JD, template_source, FakeLLM(), A._memory(), CVRecords(":memory:"), workdir,
                  jobname=jobname, palace_dir=workdir / "p")
    s.start()
    s.role, s.company = ROLE, COMPANY
    s.essentials["identity"].update({"name": "Jane Doe"})
    s.github_status = lambda: {"connected": False, "login": ""}
    s.github_token = lambda: ""
    A._SESSION["s"] = s
    A._session()                       # wires the app's hooks (ideas_for, start_build)
    return s


def _ideas_card(st):
    return next(c for c in st["cards"] if c.get("type") == "ideas")


# -- PR 7: ideas for THIS job ------------------------------------------------------------- #

def test_chat_ideas_use_the_open_jobs_role_and_jd_terms_not_the_last_record(A, template_source, workdir, monkeypatch):
    from llm.base import FakeLLM
    seen = []
    real = FakeLLM.suggest_github_ideas
    monkeypatch.setattr(FakeLLM, "suggest_github_ideas",
                        lambda self, profile, role="", terms=None, *a, **k: seen.append((role, list(terms or [])))
                        or real(self, profile, role, terms, *a, **k))
    monkeypatch.setattr(A, "_project_target", lambda *a, **k: pytest.fail("used the last saved record"))
    s = _live(A, template_source, workdir)
    st = s.submit("suggest some projects I could build for this job")
    assert seen == [(ROLE, skill_terms(JD)[:12])]
    card = _ideas_card(st)
    assert card["token"] and len(card["ideas"]) >= 3 and card["role"] == ROLE
    # no placeholder project on the page, and none in the chat's own material
    assert not any(p.get("suggested") for p in s.essentials.get("projects") or [])
    assert not any(p.get("suggested") for p in (s.profile or {}).get("projects") or [])
    # the card is on the history turn, so a restored chat shows it
    assert s.history[-1]["card"]["type"] == "ideas"
    # asking again is a cache hit for this job: no new batch
    s.submit("suggest some projects I could build for this job")
    assert len(seen) == 1


def test_the_daily_ideas_cap_holds_in_the_chat(A, template_source, workdir, monkeypatch):
    monkeypatch.setenv("PROJECT_IDEAS_DAILY_CAP", "1")
    A._ideas_count_batch()
    s = _live(A, template_source, workdir)
    st = s.submit("suggest some projects I could build for this job")
    assert not [c for c in st["cards"] if c.get("type") == "ideas"]
    assert "idea batches" in st["messages"][0] or "today" in st["messages"][0].lower()
    assert A._ideas_used() == 1


def test_flag_off_keeps_the_old_flow(A, template_source, workdir, monkeypatch):
    monkeypatch.delenv(rc.FLAG)
    s = _live(A, template_source, workdir)
    s.submit("suggest some projects I could build for this job")
    assert any(p.get("suggested") for p in s.essentials.get("projects") or [])


# -- PR 8: build from the chat ------------------------------------------------------------ #

def _build_from_chat(A, s, monkeypatch, counted=None):
    counted = [] if counted is None else counted
    monkeypatch.setattr(A, "_count_package", lambda llm: counted.append(1) or llm)
    card = _ideas_card(s.submit("suggest some projects I could build for this job"))
    key = card["ideas"][0]["key"]
    st = s.apply_action(card["token"], "build", {"key": key})
    return card, key, st, counted


def test_build_click_stores_for_resume_and_counts_one_package(A, template_source, workdir, monkeypatch):
    s = _live(A, template_source, workdir)
    card, key, st, counted = _build_from_chat(A, s, monkeypatch)
    assert st["action_ok"] and st["project"]["id"]
    p = next(x for x in A._load_projects() if x["id"] == st["project"]["id"])
    assert p["for_resume"]["jobname"] == "cvlive" and p["for_resume"]["role"] == ROLE
    assert p["for_resume"]["company"] == COMPANY and len(p["for_resume"]["jd_sha"]) == 12
    assert p["title"] == card["ideas"][0]["title"]
    assert st["cards"][0] == {"type": "project_progress", "project_id": p["id"], "title": p["title"]}
    assert "for this job" in st["messages"][0]
    again = s.apply_action(card["token"], "build", {"key": key})
    assert again["action_ok"] is False and counted == [1]
    assert len(A._load_projects()) == 1
    used = [t["card"] for t in s.history if (t.get("card") or {}).get("token") == card["token"]]
    assert used[0]["used"] and used[0]["built"] == key


def test_build_through_the_route_and_a_failed_start_leaves_the_card_usable(A, template_source, workdir, monkeypatch):
    s = _live(A, template_source, workdir)
    card = _ideas_card(s.submit("suggest some projects I could build for this job"))

    def broke(llm):
        raise RuntimeError("You've used the 3 free tailored packages this month.")
    monkeypatch.setattr(A, "_count_package", broke)
    c = A.app.test_client()
    st = c.post("/api/session/action", json={"token": card["token"], "choice": "build",
                                             "args": {"key": card["ideas"][1]["key"]}}).get_json()
    assert st["action_ok"] is False and "packages" in st["action_error"] and not A._load_projects()
    monkeypatch.setattr(A, "_count_package", lambda llm: llm)
    st = c.post("/api/session/action", json={"token": card["token"], "choice": "build",
                                             "args": {"key": card["ideas"][1]["key"]}}).get_json()
    assert st["action_ok"] and len(A._load_projects()) == 1
    hist = c.get("/api/session/state").get_json()["history"]
    assert any((t.get("card") or {}).get("type") == "project_progress" for t in hist)


# -- PR 9: a passed quiz adds it ---------------------------------------------------------- #

def _learn(A, c, pid):
    c.post(f"/api/projects/{pid}/learn")
    p = next(x for x in A._load_projects() if x["id"] == pid)
    for i, st in enumerate(p["learn"]["steps"]):
        ans = st["check"]["answer"] if st["check"]["kind"] == "choice" else st["check"]["accept"][0]
        c.post(f"/api/projects/{pid}/learn/{i}", json={"answer": ans})


def _quiz(A, c, pid, right):
    c.post(f"/api/projects/{pid}/quiz")
    qz = next(x for x in A._load_projects() if x["id"] == pid)["quiz"]
    att, qs, out = qz["attempt"], qz["questions"], []
    for k, qi in enumerate(att["order"]):
        q, perm = qs[qi], att["opts"][k]
        if q.get("type") == "multi":
            good = sorted(perm.index(i) for i in q["answers"])
            out.append(good if k < right else good[:-1])
        else:
            good = perm.index(q["answer"])
            out.append(good if k < right else (good + 1) % 4)
    return c.post(f"/api/projects/{pid}/quiz/submit", json={"attempt": att["id"], "answers": out}).get_json()


def _on_page(s, title):
    return [p for p in (s.essentials.get("projects") or []) if p.get("org") == title]


def _saved(A, title):
    prof = (A._memory().load("default") or {}).get("profile") or {}
    return [p for p in prof.get("projects") or [] if p.get("org") == title]


def test_a_pass_adds_exactly_once_and_a_fail_adds_nothing(A, template_source, workdir, monkeypatch):
    s = _live(A, template_source, workdir)
    _, _, st, _ = _build_from_chat(A, s, monkeypatch)
    pid, title = st["project"]["id"], st["project"]["title"]
    c = A.app.test_client()
    _learn(A, c, pid)
    miss = _quiz(A, c, pid, 10)
    assert miss["passed"] is False and miss["added_to"] is None
    assert not _on_page(s, title) and not _saved(A, title)
    ok = _quiz(A, c, pid, 20)
    assert ok["passed"] and ok["added_to"] == {"role": ROLE, "company": COMPANY}
    entry = _on_page(s, title)
    assert len(entry) == 1 and entry[0]["source"] == "built" and entry[0]["project_id"] == pid
    assert entry[0]["tools"] and entry[0]["bullets"] and entry[0]["link"] == ""
    assert s.history[-1]["content"].startswith(
        f"You passed {title}, 20 of 20. I added it to your resume for {ROLE} at {COMPANY}.")
    assert s.history[-1]["card"]["type"] == "project_progress" and s.history[-1]["card"]["added"]
    assert len(_saved(A, title)) == 1
    p = next(x for x in A._load_projects() if x["id"] == pid)
    assert p["on_resume"] == ["cvlive"]
    # a second verification (e.g. the hand-built grade path) never adds it twice
    assert A._on_project_verified(p, 20, 20)["added_to"] is None
    assert len(_on_page(s, title)) == 1 and len(_saved(A, title)) == 1
    # /tocv still works and stays a no-op once it is there
    assert c.post(f"/api/projects/{pid}/tocv").get_json()["ok"] and len(_saved(A, title)) == 1


def _verified(A, title="Sales KPI Dashboard", **kw):
    p = {"id": "p" + title.lower().replace(" ", "")[:10], "title": title, "summary": "Built a KPI dashboard.",
         "tech": "Python, SQL, Power BI, pandas", "status": "verified", "origin": "ai", **kw}
    projects = A._load_projects()
    projects.append(p)
    A._save_projects(projects)
    return p


def test_a_different_jobs_session_is_untouched(A, template_source, workdir):
    s = _live(A, template_source, workdir)
    before = list(s.essentials.get("projects") or []), len(s.history)
    p = _verified(A, for_resume={"jobname": "cvother", "role": "Data Engineer", "company": "Acme"})
    assert A._on_project_verified(p, 18, 20)["added_to"] is None
    assert (list(s.essentials.get("projects") or []), len(s.history)) == before
    assert len(_saved(A, p["title"])) == 1


def test_no_live_session_writes_the_saved_profile_only(A):
    A._SESSION.pop("s", None)
    p = _verified(A, for_resume={"jobname": "cvgone", "role": ROLE, "company": COMPANY})
    assert A._on_project_verified(p, 20, 20)["added_to"] is None
    assert len(_saved(A, p["title"])) == 1


def test_a_private_repo_has_no_link_and_publish_back_fills_it(A, template_source, workdir, monkeypatch):
    s = _live(A, template_source, workdir)
    p = _verified(A, repo_url="https://github.com/janedoe/sales-kpi", repo_private=True,
                  repo_full_name="janedoe/sales-kpi",
                  for_resume={"jobname": "cvlive", "role": ROLE, "company": COMPANY})
    assert A._on_project_verified(p, 19, 20)["added_to"]
    assert _on_page(s, p["title"])[0]["link"] == "" and _saved(A, p["title"])[0]["link"] == ""
    from ui import github_publish as G
    monkeypatch.setattr(A, "_cred", lambda k: "tok" if k == "GITHUB_TOKEN" else "")
    monkeypatch.setattr(A, "_publish_ai_prepare", lambda token, p: None)
    monkeypatch.setattr(G, "publish_project", lambda token, p: {"ok": True, "url": "https://github.com/janedoe/sales-kpi",
                                                                "full_name": "janedoe/sales-kpi"})
    r = A.app.test_client().post(f"/api/projects/{p['id']}/publish").get_json()
    assert r["ok"]
    assert _on_page(s, p["title"])[0]["link"] == "https://github.com/janedoe/sales-kpi"
    assert _saved(A, p["title"])[0]["link"] == "https://github.com/janedoe/sales-kpi"


def test_the_quiz_thread_add_waits_for_the_session_lock(A, template_source, workdir):
    s = _live(A, template_source, workdir)
    p = _verified(A, for_resume={"jobname": "cvlive", "role": ROLE, "company": COMPANY})
    got = {}
    s.lock.acquire()                  # a chat turn is running on another request
    try:
        t = threading.Thread(target=lambda: got.update(A._on_project_verified(p, 20, 20)))
        t.start()
        time.sleep(0.3)
        assert t.is_alive() and not _on_page(s, p["title"])
    finally:
        s.lock.release()
    t.join(10)
    assert got["added_to"] and len(_on_page(s, p["title"])) == 1


def test_verified_cv_entry_tools_and_link():
    import ui.app as app
    e = app._verified_cv_entry({"id": "x1", "title": "T", "summary": "S.", "tech": "Python, SQL, Power BI, pandas",
                                "repo_url": "https://github.com/a/t", "repo_private": False})
    assert e["tools"] == "Python, SQL, Power BI" and e["link"] == "https://github.com/a/t"
    assert e["bullets"] == ["S.", "Built with Python, SQL, Power BI, pandas."]


# -- the chat cards ----------------------------------------------------------------------- #

def test_card_validation_keeps_only_known_fields():
    tok = "a" * 24
    ideas = rc.merge_cards([{"type": "ideas", "token": tok, "role": ROLE, "ideas": [
        {"key": "kpi", "title": "KPI", "pitch": "p", "skills": ["SQL"], "gaps": [], "tech": "Python",
         "repo": "kpi", "examples": [{"full_name": "x/y", "url": "javascript:alert(1)", "stars": 3},
                                     {"full_name": "o/r", "url": "https://github.com/o/r", "stars": 5}],
         "evil": "<script>"}]},
        {"type": "project_progress", "project_id": "abc123", "title": "KPI", "x": 1},
        {"type": "project_progress", "project_id": "../etc", "title": "bad"}])
    assert ideas[0]["type"] == "ideas" and "evil" not in ideas[0]["ideas"][0]
    assert [e["url"] for e in ideas[0]["ideas"][0]["examples"]] == ["https://github.com/o/r"]
    assert rc._valid_card({"type": "project_progress", "project_id": "abc123", "title": "KPI", "x": 1}) == \
        {"type": "project_progress", "project_id": "abc123", "title": "KPI"}
    assert rc._valid_card({"type": "project_progress", "project_id": "../etc"}) is None


# -- one line per project ----------------------------------------------------------------- #

def test_project_row_moves_bracketed_tools_and_drops_personal_project():
    assert project_row({"title": "Last-Mile Operations Simulator (Python, simulation, operations analytics)",
                        "location": "Personal Project", "dates": ""}) == \
        {"name": "Last-Mile Operations Simulator", "right": "Python, simulation, operations analytics",
         "role": "", "dates": ""}
    assert project_row({"org": "Gate Check", "tools": "JavaScript, OCR, Firebase",
                        "location": "Personal Project"})["right"] == "JavaScript, OCR, Firebase"
    assert project_row({"org": "Thesis (2023)", "location": "Personal Project"}) == \
        {"name": "Thesis (2023)", "right": "", "role": "", "dates": ""}
    assert project_row({"org": "Capstone", "location": "DePaul University", "dates": "2024"})["right"] == \
        "DePaul University | 2024"
    assert project_row({"org": "App", "title": "Lead Developer", "dates": "2022"}) == \
        {"name": "App", "right": "", "role": "Lead Developer", "dates": "2022"}


def test_project_rows_render_as_one_line(template_source):
    prof = normalize_profile({"identity": {"name": "Jane Doe"}, "projects": [
        {"title": "Last-Mile Operations Simulator (Python, simulation, operations analytics)",
         "location": "Personal Project", "dates": "", "link": "https://github.com/j/lm",
         "bullets": ["Built a discrete-event simulation."]},
        {"org": "Gate Check", "tools": "JavaScript, OCR, Firebase", "location": "Personal Project",
         "bullets": ["Built a yard app."]}]})
    from tailoring.assembler import _projects
    tex = _projects(prof)
    assert "Personal Project" not in tex and "(Python" not in tex
    assert ("\\noindent \\textbf{\\textsc{Last-Mile Operations Simulator}}~\\href{https://github.com/j/lm}"
            "{$\\nearrow$} \\hfill \\textbf{Python, simulation, operations analytics}\n") in tex
    assert "\\noindent \\textbf{\\textsc{Gate Check}} \\hfill \\textbf{JavaScript, OCR, Firebase}\n" in tex
    assert "\\textbf{} \\hfill" not in tex and "\\textsc{}" not in tex


@requires_latex
def test_the_rows_compile_on_one_page(template_source, workdir):
    import subprocess

    from tailoring.assembler import extract_preamble
    from tailoring.compiler import compile_tex
    prof = normalize_profile({
        "identity": {"name": "Jane Doe", "email": "jane@example.com"},
        "education": [{"school": "DePaul University", "location": "Chicago, IL",
                       "degree": "MBA, Business Analytics", "date": "Dec 2025"}],
        "skills": {"Analytics": "SQL, Python, Power BI, Tableau"},
        "projects": [{"title": "Last-Mile Operations Simulator (Python, simulation, operations analytics)",
                      "location": "Personal Project", "link": "https://github.com/j/lm",
                      "bullets": ["Built a discrete-event simulation of a delivery operation."]}],
        "experience": []})
    tex = render_cv(extract_preamble(template_source), prof, sections=["education", "skills", "projects"])
    out = compile_tex(tex, workdir, jobname="rows")
    assert out.ok and out.pages == 1
    text = subprocess.run(["pdftotext", "-layout", str(out.pdf_path), "-"], capture_output=True, text=True).stdout
    line = next(ln for ln in text.splitlines() if "Simulator" in ln or "SIMULATOR" in ln)
    assert "Python, simulation, operations analytics" in line and "Personal Project" not in text
