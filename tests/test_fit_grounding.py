"""The two anti-fabrication gates agree, and the fit stage never burns identical calls.

The assembler's reword gate grounds a bullet in its WHOLE entry (title, org, tech line,
sibling bullets: `profile_text(entry)`). The line-fitting resize in `WebIntake` used to
ground in the sibling bullets alone, so a skill the role's tech line lists ("SQL" in
"Python, SQL, Airflow") passed the assembler and was then rejected by the fit stage, which
re-issued the identical LLM call and got the identical rejection, three times over.
Also: `skill_terms` is cached per JD and the vocabulary is sorted once at import.
"""

from __future__ import annotations

from intake.memory import ConversationMemory
from llm.base import FakeLLM
from tailoring.keywords import introduced_skills, profile_text
from tailoring.textwidth import line_fraction
from ui.records import CVRecords
from ui.session import WebIntake

JD = "Data Engineer. Required: SQL, Python, Airflow, data pipelines, Kafka, ETL."

ENTRY = {
    "org": "Acme Analytics", "location": "Accra, Ghana",
    "roles": [{"title": "Data Engineer", "dates": "Feb 2021 - Present",
               "tech": "Python, SQL, Airflow",
               "bullets": ["Built batch reporting pipelines for finance."]}],
}


class _Spy(FakeLLM):
    """Returns a scripted candidate for every resize and records each request."""

    def __init__(self, candidate):
        super().__init__()
        self.candidate = candidate
        self.calls: list[tuple[str, str, int]] = []

    def reword_bullet(self, original_text, supported_jd_terms, target_len_chars, jd_text):
        self.calls.append(("reword", original_text, target_len_chars))
        return self.candidate

    def shorten_bullet(self, text, max_len_chars):
        self.calls.append(("shorten", text, max_len_chars))
        return self.candidate


def _session(tmp_path, llm):
    return WebIntake(JD, "x", llm, ConversationMemory(":memory:"), CVRecords(":memory:"),
                     tmp_path, jobname="cv", palace_dir=tmp_path / "p")


def _one_full_line(text: str) -> str:
    """Pad ``text`` with ordinary words until it lands just under one measured line, the
    landing the fit stage scores as a clean fill (so an accepted candidate is kept)."""
    fillers = ["for", "the", "finance", "and", "operations", "teams", "each", "week"]
    i = 0
    while line_fraction(text + " " + fillers[i % len(fillers)]) <= 0.995:
        text = text + " " + fillers[i % len(fillers)]
        i += 1
    assert line_fraction(text) >= 0.90
    return text


def test_tech_line_skill_is_accepted_by_the_fit_stage(tmp_path):
    # "SQL" is on the role's tech line but in none of its bullets.
    cand = _one_full_line("Built batch reporting pipelines in SQL and Airflow for finance")
    bullets = list(ENTRY["roles"][0]["bullets"])
    # The assembler's gate accepts this reword...
    assert not introduced_skills(bullets[0], cand, profile_text(ENTRY), JD)
    # ...and so does the fit stage, now that both ground in the same entry text.
    spy = _Spy(cand)
    out = _session(tmp_path, spy)._fit_bullets_to_lines(bullets, ENTRY)
    assert out == [cand]
    assert len(spy.calls) == 1


def test_truly_new_skill_is_still_rejected_and_the_call_is_not_repeated(tmp_path):
    # "Kafka" is in the JD but nowhere in the role: fabrication, discarded.
    cand = _one_full_line("Built batch reporting pipelines on Kafka and Airflow for finance")
    bullets = list(ENTRY["roles"][0]["bullets"])
    assert introduced_skills(bullets[0], cand, profile_text(ENTRY), JD) == ["Kafka"]
    spy = _Spy(cand)
    out = _session(tmp_path, spy)._fit_bullets_to_lines(bullets, ENTRY)
    assert out == bullets                                   # the real bullet is kept
    # A rejection never re-issues the identical request: at most one ALTERED retry.
    assert 1 <= len(spy.calls) <= 2
    assert len(set(spy.calls)) == len(spy.calls), spy.calls
    if len(spy.calls) == 2:
        assert spy.calls[0][2] != spy.calls[1][2]           # the target changed


def test_fit_stage_without_an_entry_still_grounds_in_the_sibling_bullets(tmp_path):
    cand = _one_full_line("Built batch reporting pipelines in SQL and Airflow for finance")
    bullets = list(ENTRY["roles"][0]["bullets"])
    spy = _Spy(cand)
    out = _session(tmp_path, spy)._fit_bullets_to_lines(bullets)
    assert out == bullets                                   # no entry: "SQL" is ungrounded


def test_expand_terse_bullets_threads_the_employer_entry_through(tmp_path):
    cand = _one_full_line("Built batch reporting pipelines in SQL and Airflow for finance")
    spy = _Spy(cand)
    s = _session(tmp_path, spy)
    import copy
    e = copy.deepcopy(ENTRY)
    e["roles"][0]["drafted_bullets"] = False
    s.profile["experience"] = [e]
    s.profile["extracurricular"] = []
    s._expand_terse_bullets()
    assert s.profile["experience"][0]["roles"][0]["bullets"] == [cand]


# ---------------------------------------------------------------- keyword cache

def test_skill_terms_cache_matches_the_uncached_walk_and_hands_out_copies():
    from tailoring import keywords as kw
    jd = ("Senior Analyst. SQL, Python, Power BI, machine learning, ETL, Tableau, AWS, "
          "stakeholder management, forecasting.")
    kw._skill_terms_cached.cache_clear()
    first = kw.skill_terms(jd)
    assert first == list(kw._skill_terms_cached.__wrapped__(jd))   # cached == uncached
    assert kw._skill_terms_cached.cache_info().misses == 1
    first.append("Mutated")
    again = kw.skill_terms(jd)
    assert "Mutated" not in again and again == first[:-1]           # a fresh copy each call
    assert kw._skill_terms_cached.cache_info().hits >= 1


def test_vocabulary_is_sorted_once_longest_first():
    import re
    from tailoring import keywords as kw
    names = [s for s, _ in kw._SKILL_PATTERNS]
    assert names == sorted(kw._SKILL_DISPLAY, key=len, reverse=True)
    # vocab_skills behaves exactly like the former per-call sort + search.
    text = "Shipped a machine learning service on AWS with Kafka, Python and SQL reporting."
    low = text.lower()
    naive = []
    for s in sorted(kw._SKILL_DISPLAY, key=len, reverse=True):
        # Same lookahead as keywords._SKILL_PATTERNS: a skill followed by sentence punctuation
        # still counts ("SQL reporting." matches Reporting); only a longer token is excluded.
        if re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9+#]|\.[a-z])", low):
            if kw._SKILL_DISPLAY[s] not in naive:
                naive.append(kw._SKILL_DISPLAY[s])
    assert kw.vocab_skills(text) == naive
