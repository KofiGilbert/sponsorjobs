"""One fact, one bullet: an entry never prints the same fact twice.

Seen 2026-11-05 on the Phoenix Contact resume: the "Last-Mile Operations Simulator" project
came out with two bullets that both began "Built/Used a Python discrete-event simulation of a
5,000-order-a-day, 250-driver delivery operation". The person's own two bullets were distinct
(the build, then the result); a rewrite filled the second one to length by copying the first
one's clause from the entry's grounding. Rewrites that restate a sibling are now refused, and
near-identical bullets in the person's own material print once."""

from __future__ import annotations

import copy
from types import SimpleNamespace

from llm.base import FakeLLM
from tailoring import filler as F
from tailoring import linefit as LF
from tailoring.assembler import _dedupe_entry_bullets, _tailor_bullets

BUILT = ("Built a discrete-event simulation of a 5,000-order-a-day, 250-driver delivery "
         "operation to size the fleet, test shift designs at equal driver-hours, and "
         "stress-test demand surges.")
SHOWED = ("Showed a demand-shaped roster beat a flat one by about 5 points of on-time delivery "
          "at no extra cost, the work of about 10 drivers.")
# What the page printed (the second bullet restates the first one's clause).
COPIED = ("Used a Python discrete-event simulation of a 5,000-order-a-day, 250-driver delivery "
          "operation to show a demand-shaped roster beat a flat one by about 5 points of "
          "on-time delivery at no extra cost, the work of about 10 drivers.")
BUILT_PAGE = ("Built a Python discrete-event simulation of a 5,000-order-a-day, 250-driver "
              "delivery operation to size the fleet, test shift designs at equal driver-hours, "
              "and stress-test demand surges, as a personal project.")

PROFILE = {
    "identity": {"name": "Test Person"},
    "projects": [{"title": "Last-Mile Operations Simulator (Python, simulation)",
                  "bullets": [BUILT, SHOWED]}],
    "experience": [
        {"org": "Viva Technologies", "roles": [{"title": "Operations Analyst", "bullets": [
            "Analyzed SQL and Excel data for a fulfillment platform tracking 250 drivers and "
            "5,000+ daily orders.",
            "Turned analysis findings into product and operations decisions on driver capacity "
            "and service levels for a fulfillment platform."]}]},
        {"org": "CSIR", "roles": [{"title": "Research Assistant", "bullets": [
            "Built SQL-based data management pipelines to support structured querying and "
            "analytical reporting for data-intensive research projects, while reviewing complex "
            "datasets in relational databases to preserve data integrity.",
            "Analyzed complex research datasets in relational databases through systematic "
            "review while preserving data integrity."]}]},
    ],
}


def test_the_seen_pair_is_one_fact_said_twice():
    assert F.shares_a_clause(BUILT_PAGE, COPIED)
    assert F.repeats_sibling(SHOWED, COPIED, [BUILT_PAGE])
    # The person's own two bullets are distinct facts, and so are real sibling pairs that
    # merely share a few words ("for a fulfillment platform", "data integrity").
    assert not F.shares_a_clause(BUILT, SHOWED)
    for e in PROFILE["experience"]:
        a, b = e["roles"][0]["bullets"]
        assert not F.shares_a_clause(a, b), (a, b)
        assert not F.same_fact(a, b)


def test_near_identical_bullets_in_the_material_print_once():
    p = copy.deepcopy(PROFILE)
    again = BUILT.replace("stress-test demand surges.", "stress-test surges in demand.")
    p["projects"][0]["bullets"] = [BUILT, again, SHOWED]
    assert _dedupe_entry_bullets(p) == 1
    assert p["projects"][0]["bullets"] == [BUILT, SHOWED]
    # Nothing else in the profile is touched.
    assert p["experience"] == PROFILE["experience"]


class _CopyingLLM(FakeLLM):
    """Rewords the result bullet by restating its sibling's clause, as the model did."""

    def reword_bullet(self, original_text, supported_jd_terms, target_len_chars, jd_text):
        return COPIED if original_text.startswith("Showed") else original_text


def test_a_reword_that_restates_a_sibling_keeps_the_persons_bullet():
    out = _tailor_bullets(copy.deepcopy(PROFILE), "Operations analyst. Python, simulation.",
                          _CopyingLLM())
    bullets = out["projects"][0]["bullets"]
    assert bullets == [BUILT, SHOWED], bullets


def test_the_line_fit_refuses_a_fill_that_restates_a_sibling(monkeypatch):
    """The fit loop asks for a longer second bullet; the answer copies the first bullet's
    clause. It is refused, so the bullet keeps the person's wording."""
    p = {"identity": {"name": "Test Person"},
         "projects": [{"title": "Last-Mile Operations Simulator", "bullets": [BUILT_PAGE, SHOWED]}]}
    short = LF.LineReport(items=[
        LF.ItemFit("bullet", "Sim", BUILT_PAGE, lines=2, last_fill=0.95, slot=0),
        LF.ItemFit("bullet", "Sim", SHOWED, lines=1, last_fill=len(SHOWED) / len(COPIED), slot=1),
    ], left=0.0, right=400.0)
    full = LF.LineReport(items=[
        LF.ItemFit("bullet", "Sim", BUILT_PAGE, lines=2, last_fill=0.95, slot=0),
        LF.ItemFit("bullet", "Sim", COPIED, lines=2, last_fill=0.95, slot=1),
    ], left=0.0, right=400.0)
    calls = {"n": 0}

    def fake_measure(_pdf, _profile, lines=None):
        calls["n"] += 1
        return short if calls["n"] == 1 else full

    class _FillLLM(FakeLLM):
        def fit_bullet(self, text, min_chars, max_chars, jd_text="", context=""):
            return COPIED if text.startswith("Showed") else text

    from tailoring.fact_gate import known_material, source_figures

    def run():
        calls["n"] = 0
        # The fact gate sees the person's material, as in the assembler: every figure and
        # tool in the copy IS theirs (from the sibling), which is why it got through.
        return LF.fit_lines(copy.deepcopy(p), SimpleNamespace(pdf_path="x.pdf"),
                            render=lambda _p: "",
                            compile_fn=lambda _t, _n: SimpleNamespace(pdf_path="y.pdf"),
                            is_clean=lambda _r: True, llm=_FillLLM(), jd_text="", rounds=1,
                            known=known_material(p), known_figures=source_figures(p))

    monkeypatch.setattr(LF, "measure", fake_measure)
    out = run()
    assert out.profile["projects"][0]["bullets"][1] == SHOWED
    assert any("repeats another bullet" in r["claims"] for r in out.rejected)

    # The same run without the guard takes the copy: the test exercises the real path.
    monkeypatch.setattr(F, "repeats_sibling", lambda *_a, **_k: False)
    assert run().profile["projects"][0]["bullets"][1] == COPIED
