"""Restorable trims (tailoring/assembler.py): what the one-page fit cuts is kept with the build,
offered as "Put back" in the builder, and putting it back re-runs the fit check. Nothing cut to
fit is lost silently (CLAUDE.md §8-9: one page, no overfull line, self-healing compile)."""

from __future__ import annotations

import copy

from intake.memory import ConversationMemory
from llm.base import FakeLLM
from tailoring.assembler import (_shrink_once, assemble_cv, normalize_profile, restore_trim,
                                 trim_id)
from tailoring.changes import public_trim
from ui.records import CVRecords
from ui.session import WebIntake
from conftest import requires_latex

JD = "Senior Data Engineer. Python, SQL, AWS, Airflow, Kubernetes, data pipelines, dashboards."


def _role(org, title, dates, n):
    return {"org": org, "location": "Chicago, IL", "roles": [{"title": title, "dates": dates, "bullets": [
        f"{org} work item {i}: built and ran Python and SQL data pipelines on AWS with Airflow for the "
        f"analytics, finance and operations teams, with monitoring, alerting and clear runbooks." for i in range(1, n + 1)]}]}


LONG = {
    "identity": {"name": "Ama Owusu", "email": "ama@example.com", "address": "Chicago, IL"},
    "education": [{"school": "Ohio State University", "location": "Columbus, OH",
                   "degree": "Bachelor of Science in Computer Science", "date": "May 2016"}],
    "skills": {"Data": "Python, SQL, Airflow, dbt", "Cloud": "AWS, Docker, Kubernetes",
               "Tools": "Git, Tableau, Looker", "Methods": "Data modeling, testing, CI"},
    "experience": [_role("Acme Bank", "Senior Data Engineer", "June 2021 - Present", 3),
                   _role("Beacon Health", "Data Engineer", "June 2019 - May 2021", 3),
                   _role("Cobalt Retail", "Data Analyst", "June 2017 - May 2019", 3),
                   _role("Delta Freight", "Junior Analyst", "June 2016 - May 2017", 3)],
    "projects": [{"org": "Open Pipeline Kit", "title": "Creator", "dates": "2023",
                  "bullets": ["Built an open-source Python toolkit for testing SQL data pipelines."]},
                 {"org": "Budget Dashboard", "title": "Creator", "dates": "2022",
                  "bullets": ["Built a Tableau dashboard that tracks household spending by month."]}],
    "extracurricular": [{"title": "Meetup Organizer", "date": "2021 - Present",
                         "bullets": ["Run a monthly data engineering meetup with talks and demos."]}],
    "interests": "Cycling, chess",
}


def test_a_cut_is_recorded_with_where_it_came_from():
    p = normalize_profile(copy.deepcopy(LONG))
    trims: list = []
    assert _shrink_once(p, trims)
    t = trims[0]
    assert t["kind"] == "bullet" and t["section"] == "experience"
    assert t["label"] in {"Acme Bank, Senior Data Engineer", "Beacon Health, Data Engineer",
                          "Cobalt Retail, Data Analyst", "Delta Freight, Junior Analyst"}
    assert t["id"] == trim_id("bullet", t["label"], t["text"])
    assert t["text"] not in " ".join(b for e in p["experience"] for r in e["roles"] for b in r["bullets"])
    shown = public_trim(t)
    assert shown["what"] == "Bullet" and shown["text"] == t["text"] and shown["id"] == t["id"]


def test_a_kept_cut_is_never_cut_again_the_next_one_goes():
    first = normalize_profile(copy.deepcopy(LONG))
    trims: list = []
    _shrink_once(first, trims)
    kept = trims[0]
    again = normalize_profile(copy.deepcopy(LONG))
    trims2: list = []
    assert _shrink_once(again, trims2, keep={kept["id"]})
    assert trims2[0]["id"] != kept["id"]
    flat = [b for e in again["experience"] for r in e["roles"] for b in r["bullets"]]
    assert kept["text"] in flat                                   # the kept bullet stayed


def test_restore_puts_each_kind_back_in_its_place():
    p = normalize_profile(copy.deepcopy(LONG))
    trims: list = []
    for _ in range(40):                                           # cut deep: bullets, projects, skills
        if not _shrink_once(p, trims):
            break
    kinds = {t["kind"] for t in trims}
    assert {"bullet", "project"} <= kinds
    for t in reversed(trims):                                     # last cut first, like undo
        assert restore_trim(p, t), t["kind"]
    whole = normalize_profile(copy.deepcopy(LONG))
    def bullets(x):
        return sorted(b for sec in ("experience", "projects", "extracurricular")
                      for e in x.get(sec) or [] for r in (e.get("roles") or [e]) for b in r.get("bullets") or [])
    assert bullets(p) == bullets(whole)                           # nothing lost
    assert [e["org"] for e in p["projects"]] == [e["org"] for e in whole["projects"]]


@requires_latex
def test_the_fit_loop_keeps_every_cut_and_ships_one_clean_page(template_source, workdir):
    res = assemble_cv(template_source, LONG, JD, FakeLLM(), workdir, tailor=False)
    assert res.ok and res.compile.pages == 1
    assert res.trims, "this profile overflows, so the fit loop had to cut"
    page = res.profile_used
    on_page = {b for sec in ("experience", "projects", "extracurricular")
               for e in page.get(sec) or [] for r in (e.get("roles") or [e]) for b in r.get("bullets") or []}
    cut = {t["text"] for t in res.trims if t["kind"] == "bullet"}
    cut |= {b for t in res.trims if t["kind"] in ("project", "role")
            for r in (t["entry"].get("roles") or [t["entry"]]) for b in r.get("bullets") or []}
    every = {b for sec in ("experience", "projects", "extracurricular")
             for e in normalize_profile(LONG).get(sec) or [] for r in (e.get("roles") or [e])
             for b in r.get("bullets") or []}
    assert every <= on_page | cut                                  # each bullet: on the page or listed


def _session(template_source, workdir):
    s = WebIntake(JD, template_source, FakeLLM(), ConversationMemory(":memory:"),
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    s.profile = normalize_profile(copy.deepcopy(LONG))
    s.stage = "review"
    s._assemble()
    return s


@requires_latex
def test_put_back_reruns_the_fit_and_lists_what_went_instead(template_source, workdir):
    s = _session(template_source, workdir)
    st = s._state()
    assert st["trims"] and all({"id", "what", "label", "text"} <= set(t) for t in st["trims"])
    assert any("cut to fit" in p for p in st["changes"]["points"])
    target = next(t for t in s.assembled.trims if t["kind"] == "bullet")
    out = s.put_back(target["id"])
    assert out["put_back_ok"] is True, out.get("put_back_error")
    a = s.assembled
    assert a.ok and a.compile.pages == 1 and a.compile.overfull_count == 0
    page = [b for e in a.profile_used["experience"] for r in e["roles"] for b in r["bullets"]]
    assert target["text"] in page                                  # it is back on the page
    assert all(t["id"] != target["id"] for t in a.trims)           # and not cut again
    assert out["messages"] and out["messages"][0].startswith("Put back:")
    # Whatever had to go instead is listed, with its own Put back.
    assert {m["id"] for m in out.get("moved") or []} <= {t["id"] for t in a.trims}


@requires_latex
def test_put_back_that_cannot_fit_changes_nothing(template_source, workdir, monkeypatch):
    s = _session(template_source, workdir)
    target = s.assembled.trims[0]
    before = copy.deepcopy(s.profile)
    real = s._assemble

    def overflow():
        real()
        if target["id"] in s.kept_trims:                 # simulate a page that cannot hold it
            s.assembled.ok = False
    monkeypatch.setattr(s, "_assemble", overflow)
    out = s.put_back(target["id"])
    assert out["put_back_ok"] is False and "one page" in out["put_back_error"]
    assert s.profile == before and target["id"] not in s.kept_trims


def test_put_back_of_an_unknown_cut_is_refused_plainly(tmp_path):
    s = WebIntake(JD, "x", FakeLLM(), ConversationMemory(":memory:"), CVRecords(":memory:"),
                  tmp_path, jobname="cv", palace_dir=tmp_path / "p")
    out = s.put_back("nope")
    assert out["put_back_ok"] is False and "no longer on this page" in out["put_back_error"]


CUT = "Cutbullet Plumcrestwater built the nightly loader for the treasury desk."


def test_export_lists_and_erase_removes_the_cuts_and_change_lists(tmp_path, monkeypatch):
    """The cuts and the "What changed" list kept with a record are copies of the person's resume
    text: Export lists them, Erase removes them (secure_delete on, the file vacuumed) even when
    the resumes themselves stay."""
    import importlib
    import json
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(data))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    A._SESSION.pop("s", None)
    trim = {"id": "t1", "kind": "bullet", "label": "Acme Bank, Analyst", "text": CUT}
    recs = A._records()
    try:
        rid = recs.add("Analyst", "Acme Bank", 0.8, "", data={
            "trims": [trim], "changes": {"trimmed": [trim], "points": ["1 item cut to fit one page."]}})
    finally:
        recs.close()
    c = A.app.test_client()
    exported = c.get("/api/memory/export").get_json()
    assert exported["resume_changes"][0]["record_id"] == rid
    assert exported["resume_changes"][0]["trims"][0]["text"] == CUT
    assert c.post("/api/memory/erase").status_code == 200          # resumes stay
    recs = A._records()
    try:
        kept = recs.get(rid)
        assert kept is not None and "trims" not in json.loads(kept["data"])
        assert recs.tailoring_logs() == []
    finally:
        recs.close()
    assert CUT.encode() not in (data / "resume_agent.db").read_bytes()   # vacuumed, no free page keeps it
    A._SESSION.pop("s", None)
