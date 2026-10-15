"""Skip reasons, what they teach Auto-apply, the company lists, and the citizen-only filter
(autoapply/learning.py and its routes). Offline: boards are seeded in memory."""

from __future__ import annotations

import importlib

import pytest

from autoapply import learning as L
from autoapply.picker import candidates


def _job(sid, title="Data Engineer", company="Acme", location="New York, NY", **kw):
    return {"source_id": sid, "title": title, "company": company, "location": location, **kw}


def _skip(st, sid, reason, **kw):
    return L.record_skip(st, {"key": sid, **_job(sid, **kw)}, reason)


# --- storage -----------------------------------------------------------------------------------

def test_a_reason_can_follow_the_skip_and_replaces_it():
    st = L.empty_state()
    _skip(st, "gh:1", "")
    _skip(st, "gh:1", "location")
    assert len(st["skips"]) == 1 and st["skips"][0]["reason"] == "location"
    assert L.record_skip(st, {"key": "gh:2"}, "made up")["reason"] == ""


def test_company_sits_on_one_list_at_a_time():
    st = L.empty_state()
    assert L.set_company(st, "exclude", "Acme Inc")
    assert L.set_company(st, "prioritize", "ACME")
    assert st["exclude"] == [] and [c["name"] for c in st["prioritize"]] == ["ACME"]
    assert L.set_company(st, "prioritize", "acme", on=False)
    assert st["prioritize"] == []
    assert not L.set_company(st, "exclude", "  ")
    assert not L.set_company(st, "nope", "Acme")


# --- learning ----------------------------------------------------------------------------------

def test_wrong_role_needs_a_repeat_on_similar_titles():
    st = L.empty_state()
    _skip(st, "1", "wrong_role", title="Sales Engineer")
    assert L.learned(st) == []
    _skip(st, "2", "wrong_role", title="Senior Sales Engineer")
    rules = L.learned(st)
    assert [r["kind"] for r in rules] == ["role"] and rules[0]["count"] == 2


def test_seniority_location_pay_and_no_sponsorship_rules():
    st = L.empty_state()
    _skip(st, "1", "too_senior", title="Senior Data Engineer")
    _skip(st, "2", "too_junior", title="Junior Data Engineer")
    _skip(st, "3", "location", location="Austin, TX")
    _skip(st, "4", "pay_low", salary="$80k-$95k/yr")
    _skip(st, "5", "pay_low", salary="")            # unknown pay teaches nothing
    _skip(st, "6", "no_sponsorship", company="Globex")
    by = {r["kind"]: r for r in L.learned(st)}
    assert by["senior"]["value"] == "senior" and by["junior"]["value"] == "entry"
    assert by["location"]["value"] == "Austin, TX"
    assert by["pay"]["value"] == 95000 and by["pay"]["count"] == 1
    assert by["nospon"]["value"] == "Globex"


def test_forget_undoes_a_rule_and_keeps_the_skip():
    st = L.empty_state()
    _skip(st, "1", "location", location="Austin, TX")
    _skip(st, "2", "location", location="Boston, MA")
    assert L.forget(st, "loc:austin tx") == 1
    assert [r["id"] for r in L.learned(st)] == ["loc:boston ma"]
    assert len(st["skips"]) == 2
    assert L.forget(st, "loc:austin tx") == 0


# --- shaping a run -----------------------------------------------------------------------------

def test_shape_excludes_boosts_and_down_weights():
    st = L.empty_state()
    L.set_company(st, "exclude", "Initech")
    L.set_company(st, "prioritize", "Globex")
    _skip(st, "x", "location", location="Austin, TX")
    rows = [_job("a", company="Acme", location="Austin, TX"),
            _job("b", company="Initech LLC"),
            _job("c", company="Acme"),
            _job("d", company="Globex"),
            _job("s", company="Acme", location="Austin, TX", saved=True)]
    out = L.shape(rows, st, needs_sponsor=False)
    assert [j["source_id"] for j in out] == ["s", "d", "c", "a"]
    assert out[1]["priority"] and out[3]["down"]


def test_too_senior_moves_back_only_levels_above_the_skipped_role():
    st = L.empty_state()
    _skip(st, "x", "too_senior", title="Data Engineer")          # a plain, mid-level title
    rows = [_job("1", title="Staff Data Engineer"), _job("2", title="Senior Data Engineer"),
            _job("3", title="Data Analyst"), _job("4", title="Lead Data Engineer"),
            _job("5", title="Junior Data Engineer"), _job("6", title="Principal Engineer")]
    out = L.shape(rows, st)
    assert [j["source_id"] for j in out] == ["3", "5", "1", "2", "4", "6"]
    assert not any(j.get("down") for j in out[:2])               # other mid and junior roles stay


def test_too_junior_moves_back_only_levels_below_the_skipped_role():
    st = L.empty_state()
    _skip(st, "x", "too_junior", title="Data Engineer")
    rows = [_job("1", title="Junior Data Engineer"), _job("2", title="Data Analyst"),
            _job("3", title="Data Engineering Intern"), _job("4", title="Senior Data Engineer")]
    assert [j["source_id"] for j in L.shape(rows, st)] == ["2", "4", "1", "3"]


def test_pay_floor_drops_only_known_lower_pay():
    st = L.empty_state()
    _skip(st, "x", "pay_low", salary="$90k/yr")
    rows = [_job("low", salary="$70k-$90k/yr"), _job("high", salary="$120k/yr"), _job("unknown")]
    assert [j["source_id"] for j in L.shape(rows, st)] == ["high", "unknown"]


def test_citizen_only_and_clearance_roles_are_hidden_when_sponsorship_is_needed():
    rows = [_job("ok"), _job("cit", ad_stance="restricted"), _job("no", ad_stance="not_offered"),
            _job("def", company="Lockheed Martin"), _job("yes", ad_stance="offered")]
    st = L.empty_state()
    assert [j["source_id"] for j in L.shape(rows, st, needs_sponsor=True)] == ["ok", "yes"]
    assert len(L.shape(rows, st, needs_sponsor=False)) == 5


def test_no_sponsorship_reason_leaves_the_employer_out_only_while_needed():
    st = L.empty_state()
    _skip(st, "x", "no_sponsorship", company="Globex")
    rows = [_job("1", company="Globex Corp"), _job("2")]
    assert [j["source_id"] for j in L.shape(rows, st, needs_sponsor=True)] == ["2"]
    assert len(L.shape(rows, st, needs_sponsor=False)) == 2


def _no_workauth(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "autoapply.workauth", None)   # import raises ImportError


def test_needs_sponsorship_falls_back_to_the_saved_answer(monkeypatch):
    _no_workauth(monkeypatch)
    assert L.needs_sponsorship({"needs_sponsorship": "No"}) is False
    for v in ("Yes", "Future", "", None):
        assert L.needs_sponsorship({"needs_sponsorship": v}) is True
    assert L.needs_sponsorship(None) is True


def _stub_workauth(monkeypatch, *, citizen_or_pr=None, sponsor=None, boom=False):
    """A stand-in for autoapply/workauth.py with its real signatures (Builder A, PR #53)."""
    import sys
    import types
    calls = []
    mod = types.ModuleType("autoapply.workauth")

    def load(prefs):
        if boom:
            raise RuntimeError("bad profile")
        return {"loaded_from": prefs}

    def is_citizen_or_pr(wa, country="US"):
        calls.append(("citizen", wa, country))
        return citizen_or_pr

    def needs_sponsorship(wa, country="US", when="any"):
        calls.append(("sponsor", wa, country, when))
        return sponsor

    mod.load, mod.is_citizen_or_pr, mod.needs_sponsorship = load, is_citizen_or_pr, needs_sponsorship
    monkeypatch.setitem(sys.modules, "autoapply.workauth", mod)
    import autoapply
    monkeypatch.setattr(autoapply, "workauth", mod, raising=False)
    return calls


def test_needs_sponsorship_uses_the_work_authorization_profile(monkeypatch):
    calls = _stub_workauth(monkeypatch, sponsor=False)
    prefs = {"needs_sponsorship": "Yes"}                       # the profile wins over the flat answer
    assert L.needs_sponsorship(prefs) is False
    assert ("sponsor", {"loaded_from": prefs}, "US", "any") in calls
    _stub_workauth(monkeypatch, sponsor=True)
    assert L.needs_sponsorship({"needs_sponsorship": "No"}) is True
    _stub_workauth(monkeypatch, citizen_or_pr=True, sponsor=True)
    assert L.needs_sponsorship({}) is False                    # a citizen or PR never needs it


def test_needs_sponsorship_profile_silent_or_broken_falls_back(monkeypatch):
    _stub_workauth(monkeypatch, sponsor=None)
    assert L.needs_sponsorship({"needs_sponsorship": "No"}) is False
    assert L.needs_sponsorship({}) is True
    _stub_workauth(monkeypatch, boom=True)
    assert L.needs_sponsorship({"needs_sponsorship": "No"}) is False


def test_picker_candidates_uses_the_learned_state():
    st = L.empty_state()
    L.set_company(st, "exclude", "Acme")
    board = [_job("1"), _job("2", company="Hooli")]
    assert [j["source_id"] for j in candidates(board, [], learn=st)] == ["2"]
    assert len(candidates(board, [])) == 2          # no state given: unchanged behaviour


def test_mark_board_marks_never_hides():
    st = L.empty_state()
    L.set_company(st, "exclude", "Acme")
    out = L.mark_board([_job("1"), _job("2", company="Hooli")], st)
    assert out[0]["excluded"] and "excluded" not in out[1]


# --- the app -----------------------------------------------------------------------------------

@pytest.fixture
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    import ui.app as mod
    importlib.reload(mod)
    mod.WORKDIR.mkdir(parents=True, exist_ok=True)
    yield mod
    # The reloaded module stays imported for later test files: leave them no lists or answers.
    for f in (mod._LEARN_FILE, mod._PREFS_PATH):
        f.unlink(missing_ok=True)


def test_reason_routes_learn_ask_and_undo(A):
    c = A.app.test_client()
    assert c.post("/api/autoapply/reason", json={"reason": "location"}).status_code == 400
    assert c.post("/api/autoapply/reason", json={"source_id": "1", "reason": "bogus"}).status_code == 400
    r = c.post("/api/autoapply/reason", json={"source_id": "1", "title": "Data Engineer",
                                              "company": "Acme", "location": "Austin, TX",
                                              "reason": "location"}).get_json()
    assert [x["kind"] for x in r["new"]] == ["location"] and r["ask_company"] == ""
    r = c.post("/api/autoapply/reason", json={"record_id": 7, "company": "Hooli",
                                              "reason": "company"}).get_json()
    assert r["ask_company"] == "Hooli"
    r = c.post("/api/autoapply/company", json={"list": "exclude", "name": "Hooli",
                                               "from_skip": True}).get_json()
    assert r["exclude"] == ["Hooli"]
    r = c.post("/api/autoapply/reason", json={"source_id": "2", "company": "Hooli",
                                              "reason": "company"}).get_json()
    assert r["ask_company"] == ""                   # asked once: already on the list
    r = c.post("/api/autoapply/forget", json={"id": "loc:austin tx"}).get_json()
    assert r["learned"] == []
    assert c.post("/api/autoapply/forget", json={"id": "loc:austin tx"}).status_code == 404
    p = c.get("/api/autoapply/prefs").get_json()
    assert p["exclude"] == ["Hooli"] and p["needs_sponsorship"] is True
    assert c.post("/api/autoapply/company", json={"list": "x", "name": "A"}).status_code == 400


def test_candidates_honour_lists_and_the_visa_answer(A, monkeypatch):
    rows = [_job("gh:1", company="Acme"), _job("gh:2", company="Hooli"),
            _job("gh:3", company="Initech", ad_stance="restricted")]
    monkeypatch.setattr(A, "_board_rows", lambda: rows)
    A.app.test_client().post("/api/autoapply/company", json={"list": "exclude", "name": "Acme"})
    got, _ = A._aa_candidates({})
    assert [j["source_id"] for j in got] == ["gh:2"]
    A._merge_prefs({"needs_sponsorship": "No"})
    got, _ = A._aa_candidates({})
    assert [j["source_id"] for j in got] == ["gh:2", "gh:3"]


def test_jobs_board_hides_closed_roles_and_marks_excluded(A):
    rows = [_job("1", company="Acme"), _job("2", ad_stance="restricted", company="Hooli")]
    assert [j["source_id"] for j in A._jobs_personal(rows)] == ["1"]
    A._merge_prefs({"needs_sponsorship": "No"})
    assert len(A._jobs_personal(rows)) == 2
    A.app.test_client().post("/api/autoapply/company", json={"list": "exclude", "name": "Acme"})
    assert A._jobs_mark([dict(r) for r in rows])[0]["excluded"] is True


def test_page_has_preferences_and_the_reason_bar():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    html = (root / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    js = (root / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    for el in ('id="aaPrefs"', 'id="aaPfExclude"', 'id="aaPfPrio"', 'id="aaPfLearned"', 'id="skipWhy"'):
        assert el in html
    for reason in L.REASONS:
        assert f'"{reason}"' in js
    assert js.count("skipWhy({") >= 3              # Up next, the drawer, Jobs Dismiss


def test_priority_still_leads_among_down_weighted_roles():
    st = L.empty_state()
    L.set_company(st, "prioritize", "Globex")
    _skip(st, "x", "too_senior", title="Data Engineer")       # above mid goes to the back
    rows = [_job("1", title="Senior Data Engineer", company="Acme"),
            _job("2", title="Senior Data Engineer", company="Globex")]
    assert [j["source_id"] for j in L.shape(rows, st)] == ["2", "1"]
