"""The applicant brain, Phase 5 (docs/applicant-brain.md, section 6): which kind of job is this,
and which of the person's choices wins when two disagree.

A choice for NAICS 52 (Finance & Insurance) matches a bank (5221) and an insurer (524); one for
522 matches only banks. Company beats company type beats industry beats job family beats every
job; a longer NAICS prefix beats a shorter one; inside a level the newer choice wins. An unknown
company type never matches. The industry comes from the sponsor record, then the JD's tags, and
the one small model call runs only when a choice needs a label nothing else settled, once per
company. The person's own correction wins for good. Offline: FakeLLM only."""
from __future__ import annotations

import pytest

from applicant import migrate as M
from applicant import scope as S
from llm.base import FakeLLM


@pytest.fixture()
def st(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    d = tmp_path / "data"
    d.mkdir()
    s = M.ensure(d)
    assert s is not None
    yield s
    M.forget_process_state(d)


def _r(rid, kind, value="", created=0.0, action="hide"):
    return {"id": rid, "action": action, "items": ["role:viva|analyst"], "scope_kind": kind,
            "scope_value": value, "status": "active", "created_at": created}


def test_naics_prefixes_match_down_the_tree():
    bank = {"industry": "522110"}
    insurer = {"industry": "524126"}
    assert S.matches(_r(1, S.INDUSTRY, "52"), bank) and S.matches(_r(1, S.INDUSTRY, "52"), insurer)
    assert S.matches(_r(1, S.INDUSTRY, "522"), bank) and not S.matches(_r(1, S.INDUSTRY, "522"), insurer)
    assert not S.matches(_r(1, S.INDUSTRY, "52"), {"industry": ""})
    assert not S.matches(_r(1, S.INDUSTRY, "52"), {"industry": "541511"})


def test_company_beats_type_beats_industry_beats_family_beats_all():
    lab = {"family": "Data & AI", "industry": "5221", "company_type": "startup", "company": "Acme Bank"}
    rules = [_r(1, S.ALL, created=5), _r(2, S.FAMILY, "Data & AI", created=4), _r(3, S.INDUSTRY, "52", created=3),
             _r(4, S.COMPANY_TYPE, "startup", created=2), _r(5, S.COMPANY, "Acme Bank", created=1)]
    assert [r["id"] for r in S.applicable(rules, lab)] == [5, 4, 3, 2, 1]


def test_a_longer_naics_prefix_beats_a_shorter_one_and_newer_wins_in_a_level():
    lab = {"industry": "5221"}
    rules = [_r(1, S.INDUSTRY, "522", created=1), _r(2, S.INDUSTRY, "52", created=9)]
    assert [r["id"] for r in S.applicable(rules, lab)] == [1, 2]
    same = [_r(1, S.INDUSTRY, "52", created=1), _r(2, S.INDUSTRY, "52", created=2)]
    assert [r["id"] for r in S.applicable(same, lab)] == [2, 1]


def test_an_unknown_company_type_never_matches():
    rule = _r(1, S.COMPANY_TYPE, "startup")
    assert not S.matches(rule, {"company_type": "unknown"})
    assert not S.matches(rule, {})
    assert S.matches(rule, {"company_type": "startup"})


def test_a_company_scope_matches_the_same_employer_however_it_is_written():
    rule = _r(1, S.COMPANY, "Acme Bank")
    assert S.matches(rule, {"company": "ACME BANK"})
    assert S.matches(rule, {"company": "Acme Bank Ghana"})
    assert not S.matches(rule, {"company": "Acme Corp"})


def test_paused_choices_do_not_apply():
    r = _r(1, S.ALL)
    r["status"] = "paused"
    assert S.applicable([r], {}) == []


class _Rec:
    def __init__(self, naics):
        self.naics = naics


class _Sponsors:
    def __init__(self, table):
        self.table, self.calls = table, 0

    def lookup(self, company):
        self.calls += 1
        n = self.table.get(company)
        return _Rec(n) if n else None


def test_industry_comes_from_the_sponsor_record_then_the_jd_tags(st):
    sp = _Sponsors({"Acme Bank": "522110"})
    lab = S.labels(st, {"title": "Credit Analyst", "company": "Acme Bank", "jd": "A role."}, sponsors=sp)
    assert lab["industry"] == "522110" and lab["family"] == "Finance & Accounting"
    assert S.cached_class(st, "Acme Bank")["industry_source"] == "naics"
    tags = S.labels(st, {"title": "Analyst", "company": "Lendly",
                         "jd": "Lending and underwriting for our commercial bank clients. Loan origination, lending."})
    assert tags["industry"] == "52"


def test_the_model_is_called_only_when_a_choice_needs_it_and_once_per_company(st):
    FakeLLM.company_calls = 0
    llm = FakeLLM()
    job = {"title": "Data Analyst", "company": "Rocketly", "jd": "We are a seed-stage startup building payroll."}
    lab = S.labels(st, job, llm=llm)                         # no choice needs the type: no call
    assert lab["company_type"] == "unknown" and FakeLLM.company_calls == 0
    need = [_r(1, S.COMPANY_TYPE, "startup")]
    lab = S.labels(st, job, llm=llm, rules=need)
    assert lab["company_type"] == "startup" and FakeLLM.company_calls == 1
    lab = S.labels(st, job, llm=llm, rules=need)             # cached: no second call
    assert lab["company_type"] == "startup" and FakeLLM.company_calls == 1


def test_the_company_call_carries_the_ad_only(st):
    seen = {}

    class Spy:
        def classify_company(self, system, user):
            seen["user"] = user
            return '{"naics": "54", "type": "established"}'
    S.classify_company(st, Spy(), "Initech", "Initech is a Fortune 500 consultancy. " + "x" * 3000)
    assert "<company>Initech</company>" in seen["user"] and len(seen["user"]) < S.PROMPT_CHARS + 100
    row = S.cached_class(st, "Initech")
    assert row["industry"] == "54" and row["company_type"] == "established"


def test_the_persons_correction_wins_for_good(st):
    S.set_class(st, "Rocketly", company_type="established", source="person")
    S.set_class(st, "Rocketly", company_type="startup", source="model")
    assert S.cached_class(st, "Rocketly")["company_type"] == "established"
    S.set_class(st, "Rocketly", company_type="scaleup", source="person")
    assert S.cached_class(st, "Rocketly")["company_type"] == "scaleup"


def test_a_garbled_model_reply_is_unknown():
    assert S.parse_company("sure!") == ("", "unknown")
    assert S.parse_company('{"naics": "52-1", "type": "megacorp"}') == ("521", "unknown")


def test_scope_words_and_the_offered_scope():
    assert S.words(S.INDUSTRY, "52") == "bank and finance jobs"
    assert S.words(S.INDUSTRY, "522110") == "bank and lending jobs"
    assert S.words(S.COMPANY_TYPE, "startup") == "startup jobs"
    assert S.words(S.FAMILY, "Data & AI") == "data and AI jobs"
    assert S.words(S.ALL) == "every job"
    assert S.offer({"industry": "522110", "company_type": "startup", "family": "Data & AI"}) == (S.INDUSTRY, "52")
    assert S.offer({"industry": "", "company_type": "startup"}) == (S.COMPANY_TYPE, "startup")
    assert S.offer({"family": "Sales"}) == (S.FAMILY, "Sales")
    assert S.offer({}) is None


def test_the_persons_scope_words_by_the_fixed_table():
    assert S.from_words("bank and finance") == (S.INDUSTRY, "52")
    assert S.from_words("fintech jobs") == (S.INDUSTRY, "52")
    assert S.from_words("startups") == (S.COMPANY_TYPE, "startup")
    assert S.from_words("") == (S.ALL, "")
    assert S.from_words("at Acme Bank") == (S.COMPANY, "Acme Bank")
    assert S.from_words("the moon") is None
