"""ADD on a job keyword adds it (Kofi, 2026-11-09).

"what difference does it make if i add it on my profile or i add it here, it is still the same
person telling you i have the skill so add it". The coverage panel's ADD puts the term, in the
job's own wording, on the best-fitting Skills line, rebuilds through the normal one-page fit,
and saves it as the person's own skill. The old dead end ("Not supported by your profile. Add
it to a role or project in your profile, then try again.") is gone.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

import ui.app as app
from conftest import requires_latex
from intake.memory import ConversationMemory
from llm.base import FakeLLM
from tailoring.keywords import term_present
from tailoring.linefit import place_term, remove_term, split_terms
from ui.records import CVRecords
from ui.session import WebIntake

ROOT = Path(__file__).resolve().parents[1]
DEAD_END = "Not supported by your profile"

JD = ("Operations Analyst. Required: SQL, Python, Tableau, purchase orders, Kubernetes, "
      "ETL, forecasting, statistics.")

PROFILE = {
    "identity": {"name": "Kwame Asante", "email": "kwame@example.com", "city": "Accra"},
    "education": [{"school": "University of Ghana", "degree": "B.S. Statistics",
                   "date": "June 2019", "location": "Accra, Ghana"}],
    "experience": [
        {"org": "BlackOrigin", "location": "Accra, Ghana",
         "roles": [{"title": "Data Analyst", "dates": "Feb 2021 - Present",
                    "bullets": ["Built Tableau dashboards for the operations team that cut weekly reporting time.",
                                "Wrote SQL models for finance reporting across three regional teams."]}]},
        {"org": "Stanbic Bank", "location": "Accra, Ghana",
         "roles": [{"title": "Analyst", "dates": "Jan 2018 - Dec 2020",
                    "bullets": ["Prepared monthly forecasting packs for the treasury desk."]}]},
    ],
    "skills": {"Data & Analytics": "SQL, Python, Tableau",
               "Business & Finance": "forecasting, budgeting",
               "Certifications": "CFA Level I"},
    "interests": "chess, hiking",
}


# -- placing one term on a Skills line (pure) --------------------------------------------- #

def test_term_goes_on_the_line_of_its_theme_in_the_jobs_wording():
    pl = place_term({"Data & Analytics": "SQL, Tableau", "Leadership": "mentoring, hiring"},
                    "ETL", jd_text="We need SQL and ETL.")
    assert pl.label == "Data & Analytics" and not pl.shed and not pl.already
    # After the line's job terms (SQL), before the rest (Tableau is not in this JD).
    assert split_terms(pl.skills["Data & Analytics"]) == ["SQL", "ETL", "Tableau"]
    assert pl.skills["Leadership"] == "mentoring, hiring"


def test_a_business_term_lands_on_the_business_line():
    pl = place_term({"Data & Analytics": "SQL, Tableau", "Business & Finance": "budgeting"},
                    "purchase orders")
    assert pl.label == "Business & Finance"
    assert "purchase orders" in split_terms(pl.skills["Business & Finance"])


def test_a_listed_term_is_not_listed_twice():
    pl = place_term({"Data & Analytics": "SQL, ETL (dbt)"}, "etl")
    assert pl.already and pl.skills == {"Data & Analytics": "SQL, ETL (dbt)"}


def test_a_full_line_sheds_its_least_relevant_term_never_the_new_one():
    full = ", ".join(f"Tool{i}" for i in range(8))
    pl = place_term({"Languages & Tools": "SQL, " + full}, "Docker",
                    width_fn=lambda lab, terms: 10.0 * len(terms), avail=95.0,
                    jd_text="SQL and Docker")
    terms = split_terms(pl.skills["Languages & Tools"])
    assert "Docker" in terms and "SQL" in terms and len(terms) <= 8
    assert pl.shed and all(t.startswith("Tool") for t in pl.shed)


def test_certification_lines_are_never_touched():
    pl = place_term({"Certifications": "PMP"}, "Kubernetes")
    assert pl.skills["Certifications"] == "PMP"
    assert pl.label != "Certifications" and pl.skills[pl.label] == "Kubernetes"
    assert list(pl.skills)[0] == pl.label           # a category line sits above certifications


def test_remove_term_puts_the_shed_terms_back():
    pl = place_term({"Languages & Tools": "A1, B2, C3"}, "Docker",
                    width_fn=lambda lab, terms: 10.0 * len(terms), avail=35.0)
    assert pl.shed
    back = remove_term(pl.skills, "Docker", pl.label, pl.shed)
    assert split_terms(back["Languages & Tools"]) == ["A1", "B2", "C3"]


# -- the live session ------------------------------------------------------------------- #

def _session(template_source, workdir, memory=None):
    return WebIntake(JD, template_source, FakeLLM(), memory or ConversationMemory(":memory:"),
                     CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")


def _built(template_source, workdir, memory=None):
    from tailoring.assembler import normalize_profile
    s = _session(template_source, workdir, memory)
    s.profile = normalize_profile(copy.deepcopy(PROFILE))
    s.stage = "review"
    s._assemble()
    assert s.assembled is not None and s.assembled.ok
    return s


def _skill_lines(profile):
    return {k: split_terms(v) for k, v in (profile.get("skills") or {}).items()}


def _on_a_skills_line(profile, term):
    return [k for k, terms in _skill_lines(profile).items() if term in terms]


@requires_latex
def test_add_puts_the_term_on_a_skills_line_and_keeps_one_clean_page(template_source, workdir):
    s = _built(template_source, workdir)
    base = s.assembled.compile
    assert "Kubernetes" in s.coverage_dict()["missing"]
    v0 = s._pdf_v

    out = s.keyword_add("Kubernetes")
    assert out["ok"], out.get("reason")
    assert DEAD_END not in str(out)
    # On exactly one Skills line, in the job's wording, on the page and in the profile.
    lines = _on_a_skills_line(s.assembled.profile_used, "Kubernetes")
    assert len(lines) == 1 and lines == _on_a_skills_line(s.profile, "Kubernetes")
    assert lines != ["Certifications"]
    assert "Kubernetes" in out["coverage"]["present"]
    assert "Kubernetes" not in out["coverage"]["missing"]
    # One page, no new overfull box, a fresh PDF.
    c = s.assembled.compile
    assert c.pages == 1 and c.overfull_count <= base.overfull_count
    assert s._pdf_v > v0
    assert out["keyword_actions"] == [{"term": "Kubernetes", "kind": "skill", "label": lines[0]}]
    # The honesty check does not flag the person's own claim.
    honesty = next(ch for ch in out["review"]["checks"] if ch["id"] == "honesty")
    assert honesty["level"] == "pass"


@requires_latex
def test_a_business_term_lands_on_the_business_line_of_the_page(template_source, workdir):
    s = _built(template_source, workdir)
    out = s.keyword_add("purchase orders")
    assert out["ok"], out.get("reason")
    assert _on_a_skills_line(s.assembled.profile_used, "purchase orders") == ["Business & Finance"]
    assert s.assembled.compile.pages == 1


@requires_latex
def test_added_term_is_saved_to_the_profile_for_later_resumes(template_source, workdir, tmp_path):
    mem = ConversationMemory(str(tmp_path / "mem.db"))
    s = _built(template_source, workdir, mem)
    assert s.keyword_add("Kubernetes")["ok"]
    saved = mem.load("default")
    assert "Kubernetes" in saved["essentials"]["skills_input"]
    assert "Kubernetes" in saved["essentials"]["skills_from_jobs"]
    assert any("Kubernetes" in terms for terms in _skill_lines(saved["profile"]).values())
    # A later session treats it as theirs: it is in the material skills are vetted against.
    later = _session(template_source, workdir, mem)
    assert "Kubernetes" in later.essentials["skills_input"]
    assert "Kubernetes" in later._skill_pool()


@requires_latex
def test_several_adds_in_a_row_each_work(template_source, workdir):
    s = _built(template_source, workdir)
    for term in ("purchase orders", "Kubernetes", "ETL"):
        out = s.keyword_add(term)
        assert out["ok"], (term, out.get("reason"))
        assert s.assembled.compile.pages == 1
    assert {"Kubernetes", "ETL"} <= set(out["coverage"]["present"])
    on_page = [t for terms in _skill_lines(s.assembled.profile_used).values() for t in terms]
    for term in ("purchase orders", "Kubernetes", "ETL"):
        assert on_page.count(term) == 1                  # one visible entry per term
    assert [a["term"] for a in s.keyword_actions] == ["purchase orders", "Kubernetes", "ETL"]


@requires_latex
def test_undo_takes_the_term_off_the_page_and_out_of_the_profile(template_source, workdir):
    s = _built(template_source, workdir)
    skills0 = dict(s.profile["skills"])
    assert s.keyword_add("Kubernetes")["ok"]
    back = s.keyword_undo("Kubernetes")
    assert back["ok"] and back.get("undone") and back["keyword_actions"] == []
    assert s.profile["skills"] == skills0
    assert "Kubernetes" not in s.essentials.get("skills_input", [])
    assert not term_present("Kubernetes", str(s.assembled.profile_used.get("skills")))


@requires_latex
def test_no_room_keeps_the_page_and_the_saved_skill(template_source, workdir, monkeypatch):
    s = _built(template_source, workdir)
    skills0 = dict(s.profile["skills"])
    monkeypatch.setattr(WebIntake, "_term_on_page", lambda self, term: False)
    out = s.keyword_add("Kubernetes")
    assert out["ok"] is False
    assert out["reason"] == "No room on the page for Kubernetes. It is saved to your profile."
    assert DEAD_END not in out["reason"]
    assert s.profile["skills"] == skills0 and s.assembled.ok
    assert "Kubernetes" in s.essentials["skills_input"]
    assert s.keyword_actions == []


def test_add_needs_a_built_page(tmp_path):
    s = WebIntake(JD, "x", FakeLLM(), ConversationMemory(":memory:"), CVRecords(":memory:"),
                  tmp_path, jobname="cv", palace_dir=tmp_path / "p")
    out = s.keyword_add("Kubernetes")
    assert out.get("ok") is False and "Build the resume first" in out["reason"]


# -- the route and the panel ------------------------------------------------------------ #

def test_add_route_needs_an_open_resume_and_a_term(monkeypatch, tmp_path):
    app._SESSION.pop("s", None)
    client = app.app.test_client()
    assert client.post("/api/cv/live/keyword/add", json={"term": "Go"}).status_code == 404
    s = WebIntake(JD, "x", FakeLLM(), ConversationMemory(":memory:"), CVRecords(":memory:"),
                  tmp_path, jobname="cv", palace_dir=tmp_path / "p")
    monkeypatch.setitem(app._SESSION, "s", s)
    assert client.post("/api/cv/live/keyword/add", json={}).status_code == 400


@requires_latex
def test_add_route_adds_and_never_returns_the_dead_end(template_source, workdir, monkeypatch):
    s = _built(template_source, workdir)
    monkeypatch.setitem(app._SESSION, "s", s)
    client = app.app.test_client()
    r = client.post("/api/cv/live/keyword/add", json={"term": "ETL"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] and "ETL" in body["coverage"]["present"]
    assert DEAD_END not in r.get_data(as_text=True)
    # The preview of a term the profile lacks no longer dead-ends either: the panel adds it.
    p = client.post("/api/cv/live/keyword/preview", json={"term": "Kubernetes"}).get_json()
    assert DEAD_END not in str(p)


def test_the_panel_has_no_dead_end_and_queues_adds():
    js = (ROOT / "ui/static/app.js").read_text(encoding="utf-8")
    assert DEAD_END not in js and "Add it to a role or project" not in js
    assert "/api/cv/live/keyword/add" in js and "KW_QUEUE" in js and "KW_BUSY" in js
    for lang in ("en", "de", "es", "fr", "pt"):
        text = (ROOT / f"ui/static/locales/{lang}.json").read_text(encoding="utf-8")
        assert "kw.unsupported" not in text and '"kw.adding"' in text


def test_brain_records_the_skill_as_the_persons_own(tmp_path, monkeypatch):
    from applicant import migrate as M
    from applicant.store import ACTIVE
    monkeypatch.delenv(M.ENV, raising=False)
    d = tmp_path / "data"
    mem = ConversationMemory(str(d / "resume_agent.db"))
    st = M.ensure(d)
    s = WebIntake(JD, "x", FakeLLM(), mem, None, tmp_path / "w", palace_dir=d / "palace")
    saved = s._save_added_skill("purchase orders")
    assert saved == {"input": True, "fact": True}
    rows = [r for r in st.facts(store="", key="skill", status=ACTIVE)
            if r["value"] == "purchase orders"]
    assert rows and rows[0]["source"] == "you" and rows[0]["source_ref"] == "job_keywords"
    assert "purchase orders" in s._brain_skills()
    s._unsave_added_skill("purchase orders", saved)
    assert not [r for r in st.facts(store="", key="skill", status=ACTIVE)
                if r["value"] == "purchase orders"]
    M.forget_process_state(d)
