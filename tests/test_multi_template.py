"""Multi-template UX: the SELECTED template drives the CV's shape AND the intake questions.

Two templates ship: `shetty` (ends with Extracurricular + Interests — both required) and
`summary` (leads with a Summary, ends at Experience — no extracurricular/interests). The
manifest (config/templates/*.json) is the source of truth; the gate, the questions, and the
assembler section order all read from it.
"""

from __future__ import annotations

from pathlib import Path

from intake.memory import ConversationMemory
from intake.template_manifest import (identity_fields, load_manifest,
                                      required_sections, sections)
from tailoring.assembler import extract_preamble, render_cv
from ui.records import CVRecords
from ui.session import WebIntake
from conftest import requires_latex

ROOT = Path(__file__).resolve().parents[1]
SUMMARY_TEX = (ROOT / "config" / "resume_summary.tex").read_text(encoding="utf-8")

JD = "Backend Engineer. Python, SQL, AWS, REST APIs, data pipelines."
SKELETON = (
    "I'm Sam Rivera, sam@example.com. B.S. in CS at UC Berkeley, May 2016. "
    "Senior Engineer at Northwind in San Francisco, CA since Feb 2020. "
    "Skills: Python, SQL, AWS, Docker, REST APIs, data pipelines. "
    "No LinkedIn, GitHub, or blog. No address. Skip courses. No projects."
)


# --- manifests are the source of truth -------------------------------------- #

def test_manifests_declare_distinct_shapes_and_questions():
    shetty, summ = load_manifest("shetty"), load_manifest("summary")
    assert sections(shetty)[-2:] == ["extracurricular", "interests"]
    assert required_sections(shetty) == ["extracurricular", "interests"]
    # The summary template leads with Summary and now also carries Extracurricular + Interests so
    # the CV fills a full page (it used to end at Experience, leaving the bottom third empty).
    assert sections(summ) == ["summary", "education", "skills", "projects", "experience",
                              "extracurricular", "interests"]
    assert required_sections(summ) == []    # still optional, so they never gate a build
    assert "blog" not in identity_fields(summ)                 # summary template has no blog slot
    assert "blog" in identity_fields(shetty)


# --- assembler renders the manifest's sections, in order -------------------- #

def test_assembler_renders_only_the_templates_sections(template_source):
    profile = {"identity": {"name": "Sam Rivera"}, "summary": "A backend engineer who ships.",
               "education": [{"school": "UC Berkeley", "degree": "B.S.", "date": "2016"}],
               "experience": [{"org": "Acme", "roles": [{"title": "Eng", "dates": "2020",
                                                         "bullets": ["Built things."]}]}],
               "extracurricular": [{"title": "Volunteer", "bullets": ["Helped out."]}],
               "interests": "chess"}
    pre = extract_preamble(template_source)
    # Summary template: Summary present; Extracurricular/Additional absent; ends at Experience.
    summ = render_cv(pre, profile, sections=["summary", "education", "skills", "projects", "experience"])
    assert "Summary" in summ and "A backend engineer who ships" in summ
    assert "Extracurricular" not in summ and "Additional Information" not in summ
    # Shetty-style: extracurricular present; no Summary heading.
    classic = render_cv(pre, profile, sections=["education", "skills", "projects",
                                                "experience", "extracurricular", "interests"])
    assert "Extracurricular" in classic and "Additional Information" in classic
    assert "\\large Summary" not in classic


# --- the gate is template-driven -------------------------------------------- #

def _s(tex, fake_llm, workdir, template):
    return WebIntake(JD, tex, fake_llm, ConversationMemory(":memory:"), CVRecords(":memory:"),
                     workdir, jobname="cv", palace_dir=workdir / "p", template_name=template)


@requires_latex
def test_summary_template_builds_after_skeleton_with_a_summary(fake_llm, workdir):
    s = _s(SUMMARY_TEX, fake_llm, workdir, "summary")
    assert s.required_fill == []                               # no extra gate
    s.start()
    st = s.submit(SKELETON)
    assert st["phase"] == "review"                            # builds right after the skeleton
    assert str(s.profile.get("summary") or "").strip()        # a summary was drafted
    tex = Path(s.assembled.pdf_path).with_suffix(".tex").read_text(encoding="utf-8")
    assert "Summary" in tex
    assert "Extracurricular" not in tex and "Additional Information" not in tex


@requires_latex
def test_shetty_template_still_requires_extracurricular_and_interests(template_source, fake_llm, workdir):
    s = _s(template_source, fake_llm, workdir, "shetty")
    assert s.required_fill == ["extracurricular", "interests"]
    s.start()
    st = s.submit(SKELETON)
    assert st["phase"] == "chatting"                          # NOT built — still asks
    msg = " ".join(st["messages"]).lower()
    assert any(w in msg for w in ("outside", "volunteer", "club", "team", "interest", "hobb"))


# --- endpoints: list + select ---------------------------------------------- #

def test_templates_endpoint_lists_both(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    d = app.app.test_client().get("/api/templates").get_json()
    names = {t["name"] for t in d["templates"]}
    assert {"shetty", "summary"} <= names and d["default"] == "shetty"


def test_start_selects_the_requested_template(tmp_path, monkeypatch):
    import ui.app as app
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setattr(app, "WORKDIR", tmp_path / "cv")
    monkeypatch.setattr(app, "PALACE_DIR", tmp_path / "p")
    (tmp_path / "cv").mkdir()
    app._SESSION.pop("s", None)
    app.app.test_client().post("/api/session/start", json={"jd": JD, "template": "summary"})
    assert app._SESSION["s"].template_name == "summary"
    assert app._SESSION["s"].required_fill == []
