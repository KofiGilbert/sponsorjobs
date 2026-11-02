"""The owner's full-width line rule (tailoring/linefit.py), measured on real compiled PDFs.

  * Every Skills line is ONE line that reaches the right margin; 5 to 6 of them when the
    person's own material allows.
  * Every bullet's last line runs close to the right edge; bullet counts never change.
  * Only the person's own material: the fact gate is never bypassed, and a thin profile is
    ASKED for more (JD keywords first), never padded.
  * One page, no overfull box, never worse than before.

The two people are made up (tests/linefit_people.py)."""

from __future__ import annotations

import copy

import pytest

from conftest import requires_latex
from linefit_people import JD, RICH, RICH_POOL, THIN, THIN_POOL
from llm.base import FakeLLM
from tailoring import linefit as LF
from tailoring.assembler import assemble_cv, extract_preamble, render_cv
from tailoring.compiler import compile_tex


def _bullet_counts(profile: dict) -> list[int]:
    return [len(r.get("bullets") or []) for e in profile.get("experience", [])
            for r in (e.get("roles") or [e])]


def _build(template_source, workdir, person, pool, llm=None, fit=True, name="cv", **kw):
    return assemble_cv(template_source, copy.deepcopy(person), JD, llm, workdir, jobname=name,
                       tailor=False, fit_lines=fit, skill_pool=pool,
                       material=[", ".join(pool)], **kw)


# --------------------------------------------------------------------------- measurement

@requires_latex
def test_measure_reads_every_line_from_the_compiled_pdf(template_source, workdir):
    prof = {
        "identity": {"name": "Pat Example", "email": "pat@example.com"},
        "skills": {
            "Analytics & BI": ("SQL, Python (pandas, NumPy), Power BI, Tableau, advanced Excel, "
                               "data visualization, dashboard design, Looker, dbt, Snowflake, "
                               "BigQuery, Google Analytics, Alteryx"),          # wraps
            "People": "Mentoring",                                               # stubby
        },
        "experience": [{"org": "Acme", "location": "Austin, TX", "roles": [{
            "title": "Analyst", "dates": "2020 - Present", "bullets": [
                "Built reports.",
                "Built weekly SQL reporting pipelines that cut manual reconciliation work for "
                "the finance team by half and gave regional leads a single trusted number for "
                "weekly sales, margin and stock.",
            ]}]}],
    }
    tex = render_cv(extract_preamble(template_source), prof, probe=True)
    res = compile_tex(tex, workdir, jobname="measure")
    assert res.ok and res.pages == 1
    rep = LF.measure(res.pdf_path, prof)
    sk = {i.label: i for i in rep.skills}
    assert sk["Analytics & BI"].lines == 2, "a wrapping skills line must measure as two lines"
    assert sk["People"].lines == 1 and sk["People"].last_fill < 0.3
    assert not sk["Analytics & BI"].ok() and not sk["People"].ok()
    short, long_ = rep.bullets
    assert short.lines == 1 and short.last_fill < 0.2
    assert long_.lines >= 2 and 0 < long_.last_fill <= 1.02
    assert rep.width > 400                       # the template's real text block, in points
    assert "Skills:" in rep.summary()


# --------------------------------------------------------------------------- the packer

def test_packer_makes_five_full_one_line_categories_from_a_rich_profile():
    pack = LF.pack_skills(RICH["skills"], RICH_POOL, jd_text=JD)
    assert LF.SKILL_LINES_MIN <= pack.lines <= LF.SKILL_LINES_MAX
    assert not pack.short, pack.short
    avail = 574.0 - 2.0
    for label, value in pack.skills.items():
        w = LF.estimate_width(label, LF.split_terms(value))
        assert LF.SKILL_LINE_TARGET * avail <= w <= avail, (label, w / avail)


def test_packer_only_uses_the_persons_own_terms_and_never_repeats_one():
    own = {t.lower() for v in RICH["skills"].values() for t in LF.split_terms(v)}
    own |= {t.lower() for t in RICH_POOL}
    pack = LF.pack_skills(RICH["skills"], RICH_POOL, jd_text=JD)
    placed = [t.lower() for v in pack.skills.values() for t in LF.split_terms(v)]
    assert set(placed) <= own, set(placed) - own
    assert len(placed) == len(set(placed)), "a skill appears on two lines"
    # Looker and dbt are in the job ad, not in this person's material: never placed.
    assert "looker" not in placed and "dbt" not in placed


def test_packer_puts_the_jobs_terms_first():
    pack = LF.pack_skills(RICH["skills"], RICH_POOL, jd_text=JD)
    from tailoring.keywords import term_present
    first = [LF.split_terms(v)[0] for v in pack.skills.values()]
    assert all(term_present(t, JD) for t in first), first


def test_a_thin_profile_is_reported_short_not_padded():
    pack = LF.pack_skills(THIN["skills"], THIN_POOL, jd_text=JD)
    assert pack.needs_more
    placed = {t.lower() for v in pack.skills.values() for t in LF.split_terms(v)}
    assert placed == {"excel", "sql", "customer service"}


def test_a_skill_the_material_does_not_show_is_refused_by_the_gate():
    from tailoring.fact_gate import known_material
    known = known_material(RICH, "SQL, Python, Tableau")
    assert LF.supported_term("Tableau", known)
    assert not LF.supported_term("Kubernetes", known)
    assert not LF.supported_term("AWS Certified Solutions Architect", known)


# --------------------------------------------------------------------------- the fix loop

@requires_latex
def test_loop_reaches_five_to_six_full_skill_lines_from_a_rich_profile(template_source, workdir):
    plain = _build(template_source, workdir, RICH, RICH_POOL, FakeLLM(), fit=False, name="plain")
    res = _build(template_source, workdir, RICH, RICH_POOL, FakeLLM(), name="rich")
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0
    fit = res.line_fit
    assert LF.SKILL_LINES_MIN <= fit["skills_lines"] <= LF.SKILL_LINES_MAX
    assert fit["skills_full"] == fit["skills_lines"], fit["skills"]
    assert all(s["lines"] == 1 for s in fit["skills"])
    # Re-measure the shipped PDF independently: the report describes the real page.
    rep = LF.measure(res.pdf_path, res.profile_used)
    assert rep.skills_full == fit["skills_full"]
    # Bullets: more land near the edge, and no role gained or lost a bullet.
    assert fit["bullets_ok"] >= res.line_fit_before["bullets_ok"]
    assert _bullet_counts(res.profile_used) == _bullet_counts(plain.profile_used)
    assert res.line_gaps["skills_needed"] == 0 and not res.line_gaps["short_skills"]


@requires_latex
def test_loop_never_ships_a_skill_outside_the_persons_material(template_source, workdir):
    # The pool says Kubernetes and Looker, but the material (RICH + RICH_POOL) never does.
    res = assemble_cv(template_source, copy.deepcopy(RICH), JD, FakeLLM(), workdir,
                       jobname="gate", tailor=False, fit_lines=True,
                       skill_pool=RICH_POOL + ["Kubernetes", "Looker"],
                       material=[", ".join(RICH_POOL)])
    placed = " ".join(res.profile_used["skills"].values()).lower()
    assert "kubernetes" not in placed and "looker" not in placed
    assert res.line_fit["skills_lines"] >= LF.SKILL_LINES_MIN     # the real terms still fill


class _InventingLLM(FakeLLM):
    """Every fit rewrite invents: a tool the role never shows and a figure nobody gave."""

    def fit_bullet(self, text, min_chars, max_chars, jd_text="", context=""):
        return text.rstrip(".") + ", migrating it to Kubernetes and cutting costs 40%."


@requires_latex
def test_the_fact_gate_refuses_every_invented_rewrite(template_source, workdir):
    res = _build(template_source, workdir, RICH, RICH_POOL, _InventingLLM(), name="invent")
    text = " ".join(b for e in res.profile_used["experience"] for r in e["roles"]
                    for b in r["bullets"])
    assert "Kubernetes" not in text and "40%" not in text
    assert res.ok and res.compile.pages == 1


class _WorseLLM(FakeLLM):
    """Answers a fill request with a much shorter bullet: the page would get worse."""

    def fit_bullet(self, text, min_chars, max_chars, jd_text="", context=""):
        words = text.split()
        return " ".join(words[: max(3, len(words) // 3)]).rstrip(",") + "."


@requires_latex
def test_never_worse_than_before(template_source, workdir):
    res = _build(template_source, workdir, RICH, RICH_POOL, _WorseLLM(), name="worse",
                 fit_rounds=2)
    before, after = res.line_fit_before, res.line_fit
    assert after["bullets_ok"] >= before["bullets_ok"]
    assert after["skills_full"] >= before["skills_full"]
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0


@requires_latex
def test_thin_profile_reports_the_gaps_instead_of_padding(template_source, workdir):
    res = _build(template_source, workdir, THIN, THIN_POOL, FakeLLM(), name="thin")
    gaps = res.line_gaps
    assert gaps["skills_needed"] > 0 or gaps["short_skills"]
    placed = {t.lower() for v in res.profile_used["skills"].values() for t in LF.split_terms(v)}
    assert placed <= {"excel", "sql", "customer service"}
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0
    assert _bullet_counts(res.profile_used) == _bullet_counts(THIN)


# --------------------------------------------------------------------------- the builder

def _returning(template_source, workdir, saved, monkeypatch=None, brain=None):
    from intake.memory import ConversationMemory
    from ui.records import CVRecords
    from ui.session import WebIntake
    mem = ConversationMemory(":memory:")
    mem.save("default", copy.deepcopy(saved), {}, [])
    s = WebIntake(JD, template_source, FakeLLM(), mem, CVRecords(":memory:"), workdir,
                  jobname="cv", palace_dir=workdir / "p")
    if brain is not None and monkeypatch is not None:
        monkeypatch.setattr(type(s), "_brain", lambda self: brain)
    # "Just build it": skip the optional contact questions (address, GitHub) this test is
    # not about; the page-filling and line rules still apply.
    s.essentials["override"] = True
    return s


@requires_latex
def test_builder_asks_about_the_jobs_keywords_and_saves_the_answer(template_source, workdir,
                                                                   tmp_path, monkeypatch):
    from applicant.store import ACTIVE, open_store
    brain = open_store(tmp_path / "applicant.db")
    s = _returning(template_source, workdir, THIN, monkeypatch, brain)
    s.start()
    s.essentials["override"] = True
    st = s.build_from_saved()
    assert st["phase"] == "review"
    msg = " ".join(st["messages"])
    assert "This role asks for" in msg and "Have you used either?" in msg, msg
    asked = list(s.essentials["skills_pending"])
    assert len(asked) == 2
    # Progress for a later live feed, in build order.
    steps = [a["step"] for a in st["activity"]]
    assert steps == [k for k, _l in LF.BUILD_STEPS if k in steps]
    assert {"read", "match", "draft", "fit", "check"} <= set(steps)

    st2 = s.submit(f"Yes, I've used {asked[0]} at the bank, not {asked[1]} though.")
    si = [x.lower() for x in s.essentials["skills_input"]]
    assert asked[0].lower() in si and asked[1].lower() not in si
    assert asked[1] in s.essentials["skills_declined"]
    facts = [r["value"] for r in brain.facts(store="", key="skill", status=ACTIVE)]
    assert asked[0] in facts, "the answer must be saved to the brain as the person's own fact"
    placed = " ".join((s.profile.get("skills") or {}).values()).lower()
    assert asked[0].lower() in placed, "a skill the person confirmed belongs on the page"
    assert asked[1].lower() not in placed
    # Never asked about the same keyword twice.
    assert asked[1] not in " ".join(st2["messages"])


@requires_latex
def test_builder_does_not_ask_when_every_line_is_full(template_source, workdir):
    saved = copy.deepcopy(RICH)
    s = _returning(template_source, workdir, saved)
    s.start()
    s.essentials["override"] = True
    s.essentials["skills_input"] = list(RICH_POOL)
    st = s.build_from_saved()
    gaps = s.assembled.line_gaps
    assert gaps["skills_needed"] == 0 and not gaps["short_skills"], gaps
    assert "Have you used" not in " ".join(st["messages"])
    assert all(x["lines"] == 1 and x["ok"] for x in s.assembled.line_fit["skills"])


def test_bullet_sizer_now_fills_a_half_line_it_used_to_accept(template_source, workdir):
    """ui/session.py _fit_bullets_to_lines accepted a one-line bullet at 45% of the line
    (and a last line at 58%). The new bar is linefit.BULLET_LAST_LINE_TARGET."""
    from intake.memory import ConversationMemory
    from tailoring.textwidth import line_fraction
    from ui.records import CVRecords
    from ui.session import WebIntake

    calls = []

    class Spy(FakeLLM):
        def reword_bullet(self, original_text, supported_jd_terms, target_len_chars, jd_text):
            calls.append(target_len_chars)
            return original_text

    s = WebIntake(JD, template_source, Spy(), ConversationMemory(":memory:"),
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    b = "Built weekly SQL reporting for the finance team across all regional stores."
    assert 0.45 <= line_fraction(b) < LF.BULLET_LAST_LINE_TARGET
    s._fit_bullets_to_lines([b])
    assert calls and calls[0] > len(b), "a half-full one-line bullet should be filled"


def test_enrichment_asks_for_more_skills_when_the_profile_is_thin(template_source, workdir):
    from intake.memory import ConversationMemory
    from ui.records import CVRecords
    from ui.session import WebIntake
    s = WebIntake(JD, template_source, FakeLLM(), ConversationMemory(":memory:"),
                  CVRecords(":memory:"), workdir, jobname="cv", palace_dir=workdir / "p")
    s.essentials["skills_input"] = ["Excel", "SQL"]
    topics = dict(s._enrich_topics())
    assert "skills_more" in topics
    assert "this role mentions" in topics["skills_more"]
    s.essentials["skills_input"] = list(RICH_POOL)
    assert "skills_more" not in dict(s._enrich_topics())


def test_activity_endpoint_reads_the_live_builds_steps():
    import ui.app as A

    class _S:
        stage = "drafting"
        activity = [{"step": "read", "label": "Reading the job", "at": 1.0},
                    {"step": "check", "label": "Checking every line", "at": 2.0}]

    old = A._SESSION.get("s")
    A._SESSION["s"] = _S()
    try:
        r = A.app.test_client().get("/api/session/activity")
        assert r.status_code == 200
        assert [a["label"] for a in r.get_json()["activity"]] == ["Reading the job",
                                                                  "Checking every line"]
    finally:
        if old is None:
            A._SESSION.pop("s", None)
        else:
            A._SESSION["s"] = old
