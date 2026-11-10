"""The plan writer's one call must come back with an answer (Kofi, 2026-11-10).

Twice a real build's write_experience call came back with no text: a reasoning model spent its
whole token budget and stopped at max_tokens before writing. An empty answer, or one cut off at
the limit, is asked once more with no effort setting and more room; why each try stopped is kept
for the fill log.
"""

from __future__ import annotations

import json

import pytest

from llm.anthropic_client import AnthropicLLM

ROLES = [{"title": "Analyst", "company": "Acme", "dates": "2020 - 2024", "tense": "past",
          "saved": ["Built SQL reports."], "notes": "", "windows": [(90, 100, 1)]}]


class _Scripted(AnthropicLLM):
    def __init__(self, answers):
        self.model, self.effort = "claude-sonnet-5-5", "low"
        self.answers = list(answers)
        self.calls = []

    def _complete(self, system, user, max_tokens=1024, effort=None):
        text, stop = self.answers.pop(0)
        self.calls.append((effort, max_tokens))
        self.last_meta = {"stop_reason": stop, "usage": {"output_tokens": max_tokens}}
        return text


def test_an_empty_answer_is_asked_again_without_effort_and_with_more_room():
    llm = _Scripted([("", "max_tokens"), (json.dumps([["Built SQL reports on order volumes."]]), "end_turn")])
    assert llm.write_experience(ROLES, jd_text="") == [["Built SQL reports on order volumes."]]
    assert llm.calls == [("low", 16000), ("none", 24000)]
    assert [t["stop_reason"] for t in llm.last_meta["tries"]] == ["max_tokens", "end_turn"]


def test_a_readable_first_answer_is_one_call():
    llm = _Scripted([(json.dumps([["Built SQL reports on order volumes."]]), "end_turn")])
    assert llm.write_experience(ROLES)
    assert len(llm.calls) == 1


def test_two_empty_answers_raise_with_why():
    llm = _Scripted([("", "max_tokens"), ("", "max_tokens")])
    with pytest.raises(ValueError, match="max_tokens"):
        llm.write_experience(ROLES)


def test_the_broker_client_sends_no_effort_for_none():
    from llm.broker_client import BrokerLLM
    b = BrokerLLM(broker_url="http://127.0.0.1:9", auth_token="t")
    sent = {}
    b._send = lambda payload: sent.update(payload) or {"text": "ok", "stop_reason": "end_turn"}
    assert b._complete("s", "u", max_tokens=12000, effort="none") == "ok"
    assert sent["effort"] is None and sent["max_tokens"] == 12000
    assert b.last_meta["stop_reason"] == "end_turn"


def test_a_polish_never_takes_the_page_under_the_goal_or_drops_a_figure(tmp_path):
    """Run 12: the project polish tightened "...matching about 10 drivers." to one line without
    the figure or the full stop, and the page fell from 95.5% to 93.9%."""
    import inspect
    from tailoring import assembler as A
    src = inspect.getsource(A._polish_edges)
    assert "LINE_SHARE >= FILL_GOAL" in src                    # tighten only with a line to spare
    assert "lost a figure or the full stop" in src
    assert ">= FILL_GOAL > (r.fill_ratio or 0)" in src          # never under the goal


# --------------------------------------------------------------------------- a slot left empty
def test_rebalance_keeps_the_staircase_strict_or_says_it_cannot():
    from tailoring.plan_fill import rebalance, strict
    assert rebalance([6, 5, 5, 3], 2) == [6, 5, 4, 3]
    assert rebalance([5, 5, 3, 2], 2) == [5, 4, 3, 2]
    assert rebalance([6, 6, 4, 2], 2) == [6, 5, 4, 2]
    assert rebalance([4, 4, 3, 2], 2) is None                  # 4/3/2/1 is under the floor
    assert strict([5, 4, 3, 2], 2) and not strict([4, 4, 3, 2], 2) and not strict([4, 3, 2, 1], 2)


class _LeavesTheTopSlotEmpty:
    """A plan writer (sized to each window, as test_strict_staircase._PlanWriter) that never
    writes the most recent role's last bullet."""

    def __new__(cls):
        import sys
        sys.path.insert(0, "tests")
        from test_strict_staircase import _PlanWriter

        class _W(_PlanWriter):
            def write_experience(self, roles, jd_text="", current=None, only=None, feedback=None):
                out = super().write_experience(roles, jd_text, current, only, feedback)
                if only is None:
                    out[0][0] = roles[0]["saved"][0]      # its one saved bullet is on the page
                out[0][-1] = ""
                return out
        return _W()


@pytest.mark.skipif(__import__("shutil").which("pdflatex") is None, reason="needs LaTeX")
def test_a_slot_the_model_never_fills_still_leaves_a_strict_staircase(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from pathlib import Path
    from test_strict_staircase import _ladder_profile
    from tailoring import assembler as A
    tpl = Path("config/resume_shetty.tex").read_text()
    prof = _ladder_profile()
    # The most recent role has no spare bullet of its own to put in the hole.
    prof["experience"][0]["roles"][0]["bullets"] = prof["experience"][0]["roles"][0]["bullets"][:1]
    res = A.assemble_cv(tpl, prof, "Data Analyst. SQL, Excel, Tableau.",
                        _LeavesTheTopSlotEmpty(), tmp_path, tailor=False, fit_lines=True,
                        fit_rounds=0)
    assert res.ok and res.compile.pages == 1
    c = [len(r["bullets"]) for _k, _e, r in sorted(A.ranked_roles(res.profile_used),
                                                    key=lambda x: x[0])]
    assert all(b < a for a, b in zip(c, c[1:])) and c[-1] >= 2, c
    assert c[0] < 6                                            # the hole was never filled


# --------------------------------------------------------------------------- full stops, measure
@pytest.mark.skipif(__import__("shutil").which("pdflatex") is None, reason="needs LaTeX")
def test_every_planned_bullet_ends_with_a_full_stop(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from pathlib import Path
    from test_strict_staircase import _PlanWriter, _ladder_profile
    from tailoring import assembler as A

    class _NoStops(_PlanWriter):
        def write_experience(self, roles, jd_text="", current=None, only=None, feedback=None):
            out = super().write_experience(roles, jd_text, current, only, feedback)
            return [[b.rstrip(".") for b in row] for row in out]
    res = A.assemble_cv(Path("config/resume_shetty.tex").read_text(), _ladder_profile(), "Analyst.",
                        _NoStops(), tmp_path, tailor=False, fit_lines=True, fit_rounds=0)
    for _k, _e, r in A.ranked_roles(res.profile_used):
        assert all(b.rstrip().endswith(".") for b in r["bullets"]), r["bullets"]


@pytest.mark.skipif(__import__("shutil").which("pdflatex") is None, reason="needs LaTeX")
def test_an_apostrophe_never_splits_a_printed_line(tmp_path):
    """Run 16: "company's" (a raised glyph) split its line in two, so the measure read the first
    half as the line's end and every full second line looked short, and short ones looked full."""
    from pathlib import Path
    from tailoring.assembler import extract_preamble, render_cv
    from tailoring.compiler import compile_tex
    from tailoring.linefit import pdf_lines
    b = ("Led business intelligence across the company's products, building KPI dashboards that "
         "track growth, usage, and unit costs so product and finance teams review one view.")
    prof = {"identity": {"name": "A B"}, "experience": [{"org": "Acme", "roles": [
        {"title": "Analyst", "dates": "2020 - 2024", "bullets": [b]}]}]}
    tex = render_cv(extract_preamble(Path("config/resume_shetty.tex").read_text()), prof)
    r = compile_tex(tex, tmp_path, jobname="apos")
    rows = [ln for ln in pdf_lines(r.pdf_path) if "intelligence" in ln.text or "products," in ln.text]
    assert len(rows) == 1 and "products" in rows[0].text, [x.text for x in rows]


# --------------------------------------------------------------------------- versions, not lengths
def test_the_version_that_fills_its_lines_is_chosen():
    from tailoring.plan_fill import choose_version, lines_needed
    from tailoring.textwidth import text_width_pt
    word = "metric "
    cap = text_width_pt(word * 20)                           # 20 words fill one line
    v = {n: ("Built " + word * (n - 1)).strip() + "." for n in (28, 33, 39, 43)}
    # a 2-line slot: 39 words land at about 1.94 lines; 43 spill onto a third
    text, landed, way = choose_version(list(v.values()), 2, cap)
    assert landed and text == v[39], (lines_needed(text, cap))
    # none lands: the longest that stays on its lines, and the retry is told to go longer
    text, landed, way = choose_version([v[28], v[33]], 2, cap)
    assert not landed and text == v[33] and way == "longer"
    # every version spills: the shortest, and shorter next time
    text, landed, way = choose_version([v[43]], 2, cap)
    assert not landed and way == "shorter"


@pytest.mark.skipif(__import__("shutil").which("pdflatex") is None, reason="needs LaTeX")
def test_the_plan_writer_chooses_among_four_versions(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from pathlib import Path
    from test_strict_staircase import _PlanWriter, _ladder_profile
    from tailoring import assembler as A
    from tailoring.linefit import measure

    class _Versions(_PlanWriter):
        """Four versions per bullet, from far too short to far too long."""
        def write_experience(self, roles, jd_text="", current=None, only=None, feedback=None):
            out = []
            for i, ro in enumerate(roles):
                row = []
                for k, (lo, hi, n) in enumerate(ro["windows"]):
                    if only is not None and k not in (only.get(i) or []) and current:
                        row.append([current[i][k]])
                        continue
                    mid = self._sentence(lo, hi)
                    words = mid.split()
                    row.append([" ".join(words[:max(4, len(words) * 6 // 10)]) + ".",
                                " ".join(words[:max(5, len(words) * 8 // 10)]) + ".", mid,
                                mid[:-1] + " " + " ".join(words[1:]) + "."])
                out.append(row)
            return out
    res = A.assemble_cv(Path("config/resume_shetty.tex").read_text(), _ladder_profile(), "Analyst.",
                        _Versions(), tmp_path, tailor=False, fit_lines=True, fit_rounds=0)
    assert res.ok and res.compile.pages == 1
    rep = measure(res.pdf_path, res.profile_used)
    exp = {b for _k, _e, r in A.ranked_roles(res.profile_used) for b in r["bullets"]}
    fits = [i for i in rep.bullets if i.text in exp and i.found]
    assert fits and sum(i.ok() for i in fits) >= len(fits) * 0.75, [round(i.last_fill, 2) for i in fits]


@pytest.mark.skipif(__import__("shutil").which("pdflatex") is None, reason="needs LaTeX")
def test_a_project_polish_that_falls_short_asks_for_longer_versions_next(tmp_path):
    """Run 20: the Last-Mile project's versions all stopped near 85% of two lines and the same
    word targets were asked again. The next try's targets move by how far the last one missed."""
    import sys
    sys.path.insert(0, "tests")
    from pathlib import Path
    from test_strict_staircase import _PlanWriter, _ladder_profile
    from tailoring import assembler as A

    class _ShortProjects(_PlanWriter):
        def __init__(self):
            super().__init__()
            self.words = []

        def polish_role(self, role_label, bullets, budgets, context="", jd_text="", only=None,
                        words=None):
            self.words.append([list(w) for w in (words or [])])
            out = list(bullets)
            for k in (only or []):
                base = bullets[k].rstrip(".")
                # every version reaches only about 80% of what was asked
                out[k] = [base + " " + " ".join(["for the depot team"] * max(0, int(w * 0.8 - len(base.split())) // 4)) + "."
                          for w in (words[k] if words else [20])]
            return out
    prof = _ladder_profile()
    prof["projects"] = [{"title": "Route Simulator (Python)", "bullets": [
        "Built a discrete-event simulation of a 5,000-order-a-day delivery operation."]}]
    llm = _ShortProjects()
    A.assemble_cv(Path("config/resume_shetty.tex").read_text(), prof, "Analyst.", llm, tmp_path,
                  tailor=False, fit_lines=True, fit_rounds=0)
    asked = [w[0] for w in llm.words if w]
    assert len(asked) >= 2, llm.words
    assert sum(asked[1]) > sum(asked[0]), asked             # the second try asks for more words
