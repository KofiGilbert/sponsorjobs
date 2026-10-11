"""The "Add this keyword" action (tailoring/keyword_action.py, ui/session.py, ui/app.py).

One missing JD term from the coverage report becomes one honest edit of one bullet:
  * supported only when the person's OWN profile shows the term in the SAME role or project;
  * the draft is hard-validated (term present, length budget, numeric fact gate, no other new
    skill, no filler);
  * applied through the surgical editor and a self-healing compile (one page, diff confined to
    the bullet's interior), rolled back on failure;
  * undone one term at a time.
"""

from __future__ import annotations

import copy
import re

import pytest

import ui.app as app
from conftest import requires_latex
from intake.memory import ConversationMemory
from llm.base import FakeLLM
from tailoring import keyword_action as ka
from tailoring.assembler import assemble_cv, extract_preamble, render_cv
from tailoring.keywords import term_present
from tailoring.latex_template import LatexTemplate
from tailoring.surgical_editor import verify_confined
from ui.records import CVRecords
from ui.session import WebIntake

JD = ("Data Analyst. Required: SQL, Python, Tableau, ETL, forecasting, Kubernetes, "
      "Airflow, statistics.")

PROFILE = {
    "identity": {"name": "Kwame Asante", "email": "kwame@example.com", "city": "Accra"},
    "education": [{"school": "University of Ghana", "degree": "B.S. Statistics",
                   "date": "June 2019", "location": "Accra, Ghana"}],
    "experience": [
        {"org": "BlackOrigin", "location": "Accra, Ghana",
         "roles": [{"title": "Data Analyst", "dates": "Feb 2021 - Present",
                    "tech": "Python, SQL, Airflow",
                    "bullets": ["Built Tableau dashboards for the operations team that cut weekly reporting time.",
                                "Wrote SQL models for finance reporting across three regional teams."]}]},
        {"org": "Stanbic Bank", "location": "Accra, Ghana",
         "roles": [{"title": "Analyst", "dates": "Jan 2018 - Dec 2020",
                    "bullets": ["Prepared monthly forecasting packs for the treasury desk."]}]},
    ],
    "skills": {"Data & Analytics": "SQL, Python, Tableau", "Domain": "statistics, forecasting"},
    "extracurricular": [{"title": "Youth football coach", "date": "2019 - Present",
                         "bullets": ["Coach a youth team on weekends."]}],
    "interests": "chess, hiking",
}


class _Scripted(FakeLLM):
    """Answers keyword_action with a fixed dict, so each validation rule can be hit on purpose."""

    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def keyword_action(self, term, jd_text, profile, entries, allow_new):
        return copy.deepcopy(self.answer)


def _page(template_source):
    """A rendered page (no compile needed) and the profile it was rendered from."""
    from tailoring.assembler import normalize_profile
    prof = normalize_profile(copy.deepcopy(PROFILE))
    return render_cv(extract_preamble(template_source), prof), prof


def _target(tex, prof, label="BlackOrigin"):
    return next(c for c in ka.candidates(tex, prof) if c.label.startswith(label))


# -- candidates: the page's bullets map back to profile slots ------------------- #

def test_candidates_map_each_item_to_its_profile_slot(template_source):
    tex, prof = _page(template_source)
    cands = ka.candidates(tex, prof)
    refs = [c.ref for c in cands]
    assert refs == ["g0-r0-b0", "g0-r0-b1", "g1-r0-b0", "x0-b0"]
    assert cands[0].label == "BlackOrigin, Data Analyst"
    assert "Airflow" in cands[0].grounding          # the role's tech line is its own material
    assert "Airflow" not in cands[2].grounding      # another role's material is not


# -- preview: supported / unsupported / validation ------------------------------ #

def test_supported_term_gets_a_rewrite_with_verbatim_evidence(template_source):
    tex, prof = _page(template_source)
    pv = ka.preview_keyword_action("Python", tex, prof, JD, FakeLLM(), fill_ratio=0.9)
    assert pv.supported and pv.ok, pv.reason
    assert pv.kind == "rewrite" and pv.target_bullet == "experience-1"
    assert term_present("Python", pv.after)
    assert pv.evidence["quote"] and ka.quote_in(pv.evidence["quote"], ka.profile_text(prof))
    lo, hi = ka.rewrite_budget(pv.before, "Python")
    assert lo <= len(pv.after) <= hi
    d = pv.to_dict()
    assert {"supported", "evidence", "draft", "target_bullet", "before", "after"} <= set(d)


def test_unsupported_term_adds_nothing(template_source):
    tex, prof = _page(template_source)
    pv = ka.preview_keyword_action("Kubernetes", tex, prof, JD, FakeLLM(), fill_ratio=0.9)
    assert not pv.supported and not pv.ok
    assert pv.after == "" and pv.target_bullet == ""
    assert "Kubernetes" in pv.reason


def test_fabricated_evidence_is_not_support(template_source):
    tex, prof = _page(template_source)
    t = _target(tex, prof)
    llm = _Scripted({"supported": True, "kind": "rewrite", "target": t.id,
                     "evidence": {"label": t.label, "quote": "Ran Kubernetes clusters in production"},
                     "draft": f"Kubernetes: {t.text}"})
    pv = ka.preview_keyword_action("Kubernetes", tex, prof, JD, llm, fill_ratio=0.9)
    assert not pv.supported and "not in your profile" in pv.reason


def test_evidence_from_another_role_cannot_move_onto_this_bullet(template_source):
    """Issue #279: forecasting is real at Stanbic; it must not land on a BlackOrigin bullet."""
    tex, prof = _page(template_source)
    t = _target(tex, prof, "BlackOrigin")
    llm = _Scripted({"supported": True, "kind": "rewrite", "target": t.id,
                     "evidence": {"label": "Stanbic Bank, Analyst",
                                  "quote": "Prepared monthly forecasting packs for the treasury desk."},
                     "draft": f"Forecasting: {t.text}"})
    pv = ka.preview_keyword_action("forecasting", tex, prof, JD, llm, fill_ratio=0.9)
    assert not pv.ok and not pv.supported
    assert "Stanbic" in pv.reason


def test_over_budget_draft_is_rejected(template_source):
    tex, prof = _page(template_source)
    t = _target(tex, prof)
    llm = _Scripted({"supported": True, "kind": "rewrite", "target": t.id,
                     "evidence": {"label": t.label, "quote": "Python, SQL, Airflow"},
                     "draft": "Python: " + t.text + " " + t.text + " " + t.text})
    pv = ka.preview_keyword_action("Python", tex, prof, JD, llm, fill_ratio=0.9)
    assert pv.supported and not pv.ok
    assert "budget" in pv.reason


def test_draft_without_the_term_is_rejected(template_source):
    tex, prof = _page(template_source)
    t = _target(tex, prof)
    llm = _Scripted({"supported": True, "kind": "rewrite", "target": t.id,
                     "evidence": {"label": t.label, "quote": "Python, SQL, Airflow"},
                     "draft": t.text})
    pv = ka.preview_keyword_action("Python", tex, prof, JD, llm, fill_ratio=0.9)
    assert pv.supported and not pv.ok
    assert '"Python"' in pv.reason


def test_draft_with_an_invented_figure_fails_the_fact_gate(template_source):
    tex, prof = _page(template_source)
    t = _target(tex, prof)
    llm = _Scripted({"supported": True, "kind": "rewrite", "target": t.id,
                     "evidence": {"label": t.label, "quote": "Python, SQL, Airflow"},
                     "draft": "Built Python dashboards for the operations team, cutting reporting time 40%."})
    pv = ka.preview_keyword_action("Python", tex, prof, JD, llm, fill_ratio=0.9)
    assert pv.supported and not pv.ok
    assert "40" in pv.reason and "figure" in pv.reason


def test_draft_smuggling_another_skill_is_rejected(template_source):
    tex, prof = _page(template_source)
    t = _target(tex, prof)
    llm = _Scripted({"supported": True, "kind": "rewrite", "target": t.id,
                     "evidence": {"label": t.label, "quote": "Python, SQL, Airflow"},
                     "draft": "Built Python and Kubernetes dashboards for the operations team weekly."})
    pv = ka.preview_keyword_action("Python", tex, prof, JD, llm, fill_ratio=0.9)
    assert pv.supported and not pv.ok
    assert "Kubernetes" in pv.reason


def test_new_bullet_only_when_the_page_has_room(template_source):
    tex, prof = _page(template_source)
    t = _target(tex, prof)
    answer = {"supported": True, "kind": "new", "target": t.id,
              "evidence": {"label": t.label, "quote": "Python, SQL, Airflow"},
              "draft": "Automated the weekly operations reporting in Python for the whole team."}
    assert not ka.has_room(None) and not ka.has_room(0.96) and ka.has_room(0.90)
    full = ka.preview_keyword_action("Python", tex, prof, JD, _Scripted(answer), fill_ratio=0.96)
    assert full.supported and not full.ok and "room" in full.reason
    roomy = ka.preview_keyword_action("Python", tex, prof, JD, _Scripted(answer), fill_ratio=0.85)
    assert roomy.ok and roomy.kind == "new" and roomy.before == "" and roomy.anchor == t.text


def test_model_dashes_never_reach_the_page():
    assert ka.undash("Built dashboards \u2014 in Python \u2013 for ops, 2019\u20132021") == \
        "Built dashboards, in Python, for ops, 2019-2021"


# -- apply: surgical, one page, rolled back on failure --------------------------- #

@requires_latex
def test_apply_keeps_one_page_and_touches_only_that_bullet(template_source, workdir):
    llm = FakeLLM()
    built = assemble_cv(template_source, PROFILE, JD, llm, workdir, tailor=False, jobname="cv")
    assert built.ok and built.compile.pages == 1
    pv = ka.preview_keyword_action("Python", built.tex_source, built.profile_used, JD, llm,
                                   fill_ratio=built.fill_ratio)
    assert pv.ok, pv.reason
    res = ka.apply_keyword_action(built.tex_source, pv.kind, pv.target_bullet, pv.after, "Python",
                                  pv.before, workdir, llm, jobname="cv")
    assert res.ok, res.reason
    assert res.compile.ok and res.compile.pages == 1
    assert res.fill_ratio is not None and res.fill_ratio <= ka.MAX_FILL
    # The diff is confined to the one bullet's interior (the project's own assertion helper).
    tmpl = LatexTemplate(built.tex_source)
    confined = verify_confined(tmpl, res.tex_source, res.edits)
    assert confined.ok, confined.violations
    marker = "\\begin{document}"
    assert built.tex_source.split(marker)[0] == res.tex_source.split(marker)[0]
    for h in re.findall(r"\\textbf\{\\textsc\{\\large[^}]*\}\}", built.tex_source):
        assert h in res.tex_source
    changed = [ln for ln in res.diff.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    assert len(changed) == 2 and all("\\item" in ln for ln in changed)
    assert term_present("Python", res.final_text)


@requires_latex
def test_overflow_is_rolled_back_to_the_previous_page(template_source, workdir):
    llm = FakeLLM()
    built = assemble_cv(template_source, PROFILE, JD, llm, workdir, tailor=False, jobname="cv")
    t = _target(built.tex_source, built.profile_used)
    wall = " ".join(["Python reporting work for the operations and finance teams."] * 60)
    res = ka.apply_keyword_action(built.tex_source, "new", t.id, wall, "Python", "",
                                  workdir, llm, jobname="cv")
    assert not res.ok and "one page" in res.reason
    assert res.tex_source == built.tex_source and res.edits == {}
    assert res.compile.ok and res.compile.pages == 1     # the served PDF is the old page again


@requires_latex
def test_compile_failure_is_rolled_back(template_source, workdir, monkeypatch):
    llm = FakeLLM()
    built = assemble_cv(template_source, PROFILE, JD, llm, workdir, tailor=False, jobname="cv")
    t = _target(built.tex_source, built.profile_used)
    monkeypatch.setattr(ka, "render_interior", lambda text, bold=False: r"\begin{itemize")
    res = ka.apply_keyword_action(built.tex_source, "rewrite", t.id, "Python: " + t.text, "Python",
                                  t.text, workdir, llm, jobname="cv")
    assert not res.ok and res.tex_source == built.tex_source
    assert res.compile.ok and res.compile.pages == 1


# -- the live session: apply, mirror, undo ---------------------------------------- #

def _session(template_source, workdir, llm=None):
    s = WebIntake(JD, template_source, llm or FakeLLM(), ConversationMemory(":memory:"),
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    return s


def _built_session(template_source, workdir, llm=None):
    from tailoring.assembler import normalize_profile
    s = _session(template_source, workdir, llm)
    s.profile = normalize_profile(copy.deepcopy(PROFILE))
    s.stage = "review"
    s._assemble()
    assert s.assembled is not None and s.assembled.ok
    return s


def _bullet(profile, org, i=0):
    e = next(x for x in profile["experience"] if x["org"] == org)
    return (e.get("roles") or [e])[0]["bullets"][i]


@requires_latex
def test_session_apply_then_undo_restores_everything(template_source, workdir):
    s = _built_session(template_source, workdir)
    tex0, v0 = s.assembled.tex_source, s._pdf_v
    pv = s.keyword_preview("Python")
    assert pv["ok"], pv["reason"]
    out = s.keyword_apply("Python", pv["draft"])
    assert out["ok"], out["reason"]
    assert out["page_check"]["one_page"] and out["diff"].startswith("--- a/")
    assert "Python" in out["coverage"]["present"]
    assert out["keyword_actions"] == [{"term": "Python", "kind": "rewrite",
                                       "label": "BlackOrigin, Data Analyst"}]
    assert s._pdf_v == v0 + 1 and out["pdf"].endswith(f"v={s._pdf_v}")
    # Mirrored into both the durable profile and the page profile, so a rebuild keeps it.
    assert _bullet(s.profile, "BlackOrigin") == pv["draft"]
    assert _bullet(s.assembled.profile_used, "BlackOrigin") == pv["draft"]

    back = s.keyword_undo("Python")
    assert back["ok"] and back.get("undone") and back["keyword_actions"] == []
    assert s.assembled.tex_source == tex0
    assert _bullet(s.profile, "BlackOrigin") == pv["before"]
    assert _bullet(s.assembled.profile_used, "BlackOrigin") == pv["before"]
    assert s.keyword_undo("Python")["ok"] is False      # nothing left to undo


@requires_latex
def test_session_new_bullet_is_added_mirrored_and_removed(template_source, workdir):
    s = _built_session(template_source, workdir)
    t = _target(s.assembled.tex_source, s.assembled.profile_used)
    answer = {"supported": True, "kind": "new", "target": t.id,
              "evidence": {"label": t.label, "quote": "Python, SQL, Airflow"},
              "draft": "Scheduled the finance reporting models in Airflow for the operations team."}
    s.llm = _Scripted(answer)
    s.assembled.fill_ratio = 0.85                     # measured room for one more line
    pv = s.keyword_preview("Airflow")
    assert pv["ok"] and pv["kind"] == "new", pv["reason"]
    out = s.keyword_apply("Airflow")
    assert out["ok"], out["reason"]
    role = s.profile["experience"][0]["roles"][0]
    assert role["bullets"][1] == answer["draft"] and len(role["bullets"]) == 3
    assert "\\item " + answer["draft"].replace("\u2014", "") in s.assembled.tex_source
    back = s.keyword_undo("Airflow")
    assert back["ok"] and len(s.profile["experience"][0]["roles"][0]["bullets"]) == 2
    assert answer["draft"] not in s.assembled.tex_source


@requires_latex
def test_session_refuses_an_edited_draft_that_breaks_a_rule(template_source, workdir):
    s = _built_session(template_source, workdir)
    pv = s.keyword_preview("Python")
    out = s.keyword_apply("Python", pv["draft"] + " Raised accuracy by 25%.")
    assert not out["ok"] and "25" in out["reason"]
    assert s.keyword_actions == []


# -- endpoints ------------------------------------------------------------------- #

def test_keyword_routes_need_an_open_resume(monkeypatch):
    monkeypatch.setitem(app._SESSION, "s", None)
    app._SESSION.pop("s", None)
    client = app.app.test_client()
    for route in ("preview", "apply", "undo"):
        r = client.post(f"/api/cv/live/keyword/{route}", json={"term": "Python"})
        assert r.status_code == 404, route


def test_keyword_preview_needs_a_term_and_a_built_page(tmp_path, monkeypatch):
    s = WebIntake(JD, "x", FakeLLM(), ConversationMemory(":memory:"), CVRecords(":memory:"),
                  tmp_path, jobname="cv", palace_dir=tmp_path / "p")
    monkeypatch.setitem(app._SESSION, "s", s)
    client = app.app.test_client()
    assert client.post("/api/cv/live/keyword/preview", json={}).status_code == 400
    assert client.post("/api/cv/other/keyword/preview", json={"term": "Python"}).status_code == 404
    r = client.post("/api/cv/live/keyword/preview", json={"term": "Python"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["supported"] is False and "Build the resume first" in body["reason"]


@requires_latex
def test_keyword_endpoints_preview_apply_undo(template_source, workdir, monkeypatch):
    s = _built_session(template_source, workdir)
    monkeypatch.setitem(app._SESSION, "s", s)
    client = app.app.test_client()

    r = client.post("/api/cv/live/keyword/preview", json={"term": "Kubernetes"})
    assert r.status_code == 200 and r.get_json()["supported"] is False

    p = client.post("/api/cv/cv/keyword/preview", json={"term": "Python"}).get_json()
    assert p["supported"] and p["ok"]
    assert {"evidence", "draft", "target_bullet", "before", "after"} <= set(p)

    a = client.post("/api/cv/live/keyword/apply", json={"term": "Python", "draft": p["draft"]}).get_json()
    assert a["ok"], a.get("reason")
    assert "Python" in a["coverage"]["present"]
    assert a["page_check"]["one_page"] is True
    assert a["diff"] and all("\\item" in ln for ln in a["diff"].splitlines()
                             if ln[:1] in "+-" and not ln.startswith(("+++", "---")))
    assert a["keyword_actions"][0]["term"] == "Python"

    u = client.post("/api/cv/live/keyword/undo", json={"term": "Python"}).get_json()
    assert u["ok"] and u["keyword_actions"] == []
