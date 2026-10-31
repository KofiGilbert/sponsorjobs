"""The applicant brain, Phase 5 (docs/applicant-brain.md, sections 6 and 8): the person's choices
decide which of their own roles and projects a resume shows, "one of each depending on where I am
applying".

* hide, show, lead, prefer and apart on two roles whose dates overlap; pinned roles over the
  limit keep the most recent; a choice never adds a word; with no choices the page is exactly
  the scorer's (zero behaviour loss);
* a renamed role keeps its choices, a removed role pauses them (and they come back with it);
* the builder: the switch, Swap for ..., the ONE line that asks once ("Hide Viva for other bank
  and finance jobs too?"), "Just this time", the overlapping-roles question, the application row;
* chat and Telegram: a choice is proposed with Save and kept only after the tap;
* the list on Your answers: see, edit, pause and delete every choice;
* every rule combination still compiles to one clean page.
Offline: FakeLLM only."""
from __future__ import annotations

import importlib
import json
import re

import pytest

from applicant import migrate as M
from applicant import rules as R
from applicant import scope as S
from conftest import requires_latex
from tailoring.assembler import assemble_cv, normalize_profile, select_roles_for_jd

STANBIC = "role:stanbic bank|credit analyst"
VIVA = "role:viva|data analyst"
BLACK = "role:blackorigin|founder"
RADAR = "project:job radar"


def _profile(extra_roles=()):
    exp = [
        {"org": "BlackOrigin", "location": "Remote", "roles": [{"title": "Founder", "dates": "Jan 2022 - Present", "bullets": [
            "Built a job search product in Python and SQL used by 300 people.",
            "Ran product and growth for the startup from idea to paying users.",
            "Hired and led a team of three engineers."]}]},
        {"org": "Stanbic Bank", "location": "Accra, Ghana", "roles": [{"title": "Credit Analyst", "dates": "Jan 2020 - Dec 2021", "bullets": [
            "Underwrote commercial loans worth $12M a year for the corporate bank.",
            "Built credit risk models in Excel and SQL for the lending team."]}]},
        {"org": "Viva", "location": "Accra, Ghana", "roles": [{"title": "Data Analyst", "dates": "Mar 2020 - Nov 2021", "bullets": [
            "Built SQL dashboards for the sales team every week.",
            "Cleaned customer data with Python and pandas."]}]},
    ] + list(extra_roles)
    return {"identity": {"name": "Kofi Test", "email": "k@example.com", "phone": "312-555-0100", "location": "Chicago, IL"},
            "summary": "Analyst with banking and data experience. Founder of BlackOrigin, a job search startup.",
            "experience": exp,
            "projects": [{"org": "Job Radar", "title": "Builder", "dates": "Jan 2023 - Mar 2023",
                          "bullets": ["Built a SQL and Python job tracker with dashboards."]}],
            "education": [{"school": "University of Ghana", "degree": "BSc Economics", "date": "May 2019", "location": "Accra, Ghana"}],
            "skills": {"Data": "SQL, Python, Excel", "Finance": "Credit analysis, underwriting"},
            "extracurricular": [{"title": "Volunteer tutor", "date": "Jan 2018 - May 2019", "bullets": ["Taught math to 40 students."]}],
            "interests": "Chess, running"}


BANK_JD = "Credit Analyst at Acme Bank\nCommercial banking team. Underwriting, lending, credit analysis, loan portfolio, SQL."
SRC = {"rule": 1, "rank": [2, 2, 1.0, 1]}


def _orgs(p):
    return [e["org"] for e in p["experience"]]


def _sel(profile, jd, choices, **kw):
    rep = {}
    out = select_roles_for_jd(normalize_profile(profile), jd, choices=choices, report=rep, **kw)
    return out, rep


@pytest.fixture()
def st(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    d = tmp_path / "data"
    d.mkdir()
    s = M.ensure(d)
    assert s is not None
    yield s
    M.forget_process_state(d)


# ---------------------------------------------------------------------------- items and dates
def test_items_and_overlap():
    its = R.items(_profile())
    assert [i["id"] for i in its] == [BLACK, STANBIC, VIVA, RADAR]
    assert [i["pos"] for i in its] == ["exp-0-0", "exp-1-0", "exp-2-0", "proj-0"]
    pairs = R.overlapping_pairs(its)
    assert [(a["id"], b["id"]) for a, b, _n in pairs] == [(STANBIC, VIVA)]
    assert R.years(*pairs[0][:2]) == "2020 to 2021"
    # a job change (one shared month) is not two day jobs
    a = {"kind": "role", "org": "A", "span": R.span("Jan 2019 - Mar 2020")}
    b = {"kind": "role", "org": "B", "span": R.span("Mar 2020 - Present")}
    assert R.overlapping_pairs([dict(a, id="a"), dict(b, id="b")]) == []


def test_an_aligned_title_keeps_the_item_id():
    p = _profile()
    p["experience"][2]["roles"][0].update(title="Business Analyst", title_original="Data Analyst", title_suggested=True)
    assert VIVA in [i["id"] for i in R.items(p)]


# ---------------------------------------------------------------------------- selection
def test_no_choices_is_exactly_the_scorer():
    roles = [{"org": f"Co{i}", "roles": [{"title": "Analyst", "dates": f"{2010 + i} - {2011 + i}",
                                         "bullets": ["SQL work." if i % 2 else "Other work."]}]} for i in range(6)]
    p = normalize_profile(dict(_profile(), experience=roles))
    plain = select_roles_for_jd(p, "SQL analyst")
    assert select_roles_for_jd(p, "SQL analyst", choices=None) == plain
    assert select_roles_for_jd(p, "SQL analyst", choices={"hide": {}, "prefer": []}) == plain


def test_hide_removes_the_item_and_reports_it():
    out, rep = _sel(_profile(), BANK_JD, {"hide": {VIVA: SRC}})
    assert _orgs(out) == ["BlackOrigin", "Stanbic Bank"]
    assert rep["excluded"][VIVA]["how"] == "hide" and rep["applied"] == [1]
    out, rep = _sel(_profile(), BANK_JD, {"hide": {RADAR: SRC}})
    assert out["projects"] == [] and RADAR not in rep["shown"]


def test_show_pins_a_role_the_scorer_would_drop():
    extra = [{"org": f"Shop{i}", "roles": [{"title": "Clerk", "dates": f"{2012 + i} - {2013 + i}", "bullets": ["Sold shoes."]}]}
             for i in range(3)]
    p = _profile(extra)
    plain, rep0 = _sel(p, BANK_JD, None)
    assert "Shop2" not in _orgs(plain)
    out, rep = _sel(p, BANK_JD, {"show": {"role:shop2|clerk": SRC}})
    assert "Shop2" in _orgs(out) and len(rep["shown"]) - 1 <= 4 + 1
    assert rep["applied"] == [1]
    # page order is kept: the pinned role sits where its dates put it
    assert _orgs(out).index("Shop2") > _orgs(out).index("Stanbic Bank")


def test_lead_keeps_its_bullets_and_leads_the_summary():
    out, rep = _sel(_profile(), BANK_JD, {"lead": {BLACK: SRC}})
    assert out["summary"].startswith("Founder of BlackOrigin")
    assert sorted(out["summary"].split()) == sorted(_profile()["summary"].split())     # no word added
    role = out["experience"][0]["roles"][0]
    assert role.get("_lead") is True and rep["lead"] == [BLACK]
    # the shrinker takes a bullet from another role first
    from tailoring.assembler import _drop_trailing_bullet
    page = {"experience": [{"org": "A", "roles": [dict(role, bullets=["a. b.", "c.", "d."])]},
                           {"org": "B", "roles": [{"title": "x", "bullets": ["e.", "f."]}]}], "projects": []}
    _drop_trailing_bullet(page)
    assert len(page["experience"][0]["roles"][0]["bullets"]) == 3 and len(page["experience"][1]["roles"][0]["bullets"]) == 1


def test_prefer_keeps_the_first_of_two_overlapping_roles():
    out, rep = _sel(_profile(), BANK_JD, {"prefer": [[STANBIC, VIVA, SRC]]})
    assert "Viva" not in _orgs(out) and "Stanbic Bank" in _orgs(out)
    assert rep["excluded"][VIVA] == {"rule": 1, "how": "prefer", "by": STANBIC}


def test_prefer_puts_the_first_in_the_seconds_place():
    extra = [{"org": f"Shop{i}", "roles": [{"title": "Clerk", "dates": f"{2012 + i} - {2013 + i}",
                                           "bullets": ["Data analyst work in SQL dashboards and Python."]}]} for i in range(3)]
    p = _profile(extra)
    p["experience"][1]["roles"][0]["bullets"] = ["Filed papers."]           # Stanbic scores low
    plain, _ = _sel(p, "Data analyst SQL dashboards Python pandas sales", None)
    assert "Viva" in _orgs(plain) and "Stanbic Bank" not in _orgs(plain)
    out, rep = _sel(p, "Data analyst SQL dashboards Python pandas sales", {"prefer": [[STANBIC, VIVA, SRC]]})
    assert "Stanbic Bank" in _orgs(out) and "Viva" not in _orgs(out)


def test_apart_keeps_the_better_match_unless_prefer_says():
    out, rep = _sel(_profile(), BANK_JD, {"apart": [[STANBIC, VIVA, SRC]]})
    assert "Stanbic Bank" in _orgs(out) and "Viva" not in _orgs(out)
    out, _ = _sel(_profile(), "Data analyst SQL dashboards Python pandas sales", {"apart": [[STANBIC, VIVA, SRC]]})
    assert "Viva" in _orgs(out) and "Stanbic Bank" not in _orgs(out)
    out, _ = _sel(_profile(), "Data analyst SQL dashboards Python pandas sales",
                  {"apart": [[STANBIC, VIVA, SRC]], "prefer": [[STANBIC, VIVA, SRC]]})
    assert "Stanbic Bank" in _orgs(out) and "Viva" not in _orgs(out)


def test_a_narrower_pin_beats_a_broader_apart():
    narrow = {"rule": 2, "rank": [4, 0, 1.0, 2]}                        # a company choice
    # a pin on one of the pair decides which one stays
    out, rep = _sel(_profile(), BANK_JD, {"apart": [[STANBIC, VIVA, SRC]], "show": {VIVA: narrow}})
    assert "Viva" in _orgs(out) and "Stanbic Bank" not in _orgs(out)
    # both pinned for this company: the narrower choices keep both
    out, rep = _sel(_profile(), BANK_JD, {"apart": [[STANBIC, VIVA, SRC]], "show": {VIVA: narrow, STANBIC: narrow}})
    assert "Viva" in _orgs(out) and "Stanbic Bank" in _orgs(out)


def test_pinned_roles_over_the_limit_keep_the_most_recent():
    extra = [{"org": f"Shop{i}", "roles": [{"title": "Clerk", "dates": f"201{5 - i} - 201{6 - i}", "bullets": ["Sold."]}]}
             for i in range(3)]
    p = _profile(extra)
    pins = {i["id"]: SRC for i in R.items(p) if i["kind"] == "role"}
    out, rep = _sel(p, BANK_JD, {"show": pins}, max_roles=4)
    assert _orgs(out) == ["BlackOrigin", "Stanbic Bank", "Viva", "Shop0"]
    assert rep["dropped_pins"] == ["role:shop1|clerk", "role:shop2|clerk"]


def test_a_choice_never_adds_words():
    p = _profile()
    words_in = set(re.findall(r"\w+", json.dumps(normalize_profile(p))))
    for ch in ({"hide": {VIVA: SRC}}, {"lead": {BLACK: SRC}}, {"prefer": [[STANBIC, VIVA, SRC]]},
               {"apart": [[STANBIC, VIVA, SRC]]}, {"show": {VIVA: SRC}}):
        out, _ = _sel(p, BANK_JD, ch)
        out = json.loads(json.dumps(out))
        for e in out["experience"]:
            for r in e.get("roles") or []:
                r.pop("_lead", None)
        assert set(re.findall(r"\w+", json.dumps(out))) <= words_in, ch


# ---------------------------------------------------------------------------- storage
def test_add_dedupe_clash_edit_pause_delete(st):
    a = R.add_rule(st, "hide", [VIVA], S.INDUSTRY, "52", source="builder")
    assert R.add_rule(st, "hide", [VIVA], S.INDUSTRY, "52") == a          # the same choice is one row
    b = R.add_rule(st, "show", [VIVA], S.INDUSTRY, "52")                  # contradicts it: replaces it
    assert R.get_rule(st, a)["status"] == R.REPLACED and R.get_rule(st, b)["status"] == R.ACTIVE
    assert R.update_rule(st, b, scope_kind=S.COMPANY_TYPE, scope_value="startup")["scope_value"] == "startup"
    assert R.update_rule(st, b, status=R.PAUSED)["status"] == R.PAUSED
    with pytest.raises(ValueError):
        R.update_rule(st, b, action="prefer")
    with pytest.raises(ValueError):
        R.add_rule(st, "prefer", [VIVA])
    assert R.delete_rule(st, b) and R.get_rule(st, b) is None
    # the event log keeps ids and kinds, never the person's words
    R.add_rule(st, "hide", [VIVA], words="Hide Viva for other bank and finance jobs too? Yes")
    assert "Viva" not in json.dumps([dict(r) for r in st.q("SELECT * FROM event")])


def test_a_renamed_role_keeps_its_choices(st):
    rid = R.add_rule(st, "prefer", [STANBIC, VIVA], S.INDUSTRY, "52")
    assert R.rename_item(st, VIVA, "role:viva group|data analyst") == 1
    assert R.get_rule(st, rid)["items"] == [STANBIC, "role:viva group|data analyst"]


def test_a_removed_role_pauses_its_choices_and_they_come_back(st):
    rid = R.add_rule(st, "hide", [VIVA], S.INDUSTRY, "52")
    gone = _profile()
    gone["experience"] = gone["experience"][:2]
    assert R.sync_items(st, R.items(gone)) == {"paused": 1, "resumed": 0}
    assert R.get_rule(st, rid)["status"] == R.PAUSED
    assert R.sync_items(st, R.items(_profile())) == {"paused": 0, "resumed": 1}
    assert R.get_rule(st, rid)["status"] == R.ACTIVE
    # one the person paused stays paused
    R.update_rule(st, rid, status=R.PAUSED)
    R.sync_items(st, R.items(_profile()))
    assert R.get_rule(st, rid)["status"] == R.PAUSED


def test_a_title_edited_since_still_finds_its_role(st):
    its = R.items(_profile())
    assert R.resolve("role:viva|junior analyst", its)["id"] == VIVA


def test_plan_most_specific_wins_and_names_what_it_beat(st):
    R.add_rule(st, "show", [VIVA], S.FAMILY, "Data & AI")
    hid = R.add_rule(st, "hide", [VIVA], S.INDUSTRY, "52")
    lab = {"family": "Data & AI", "industry": "5221"}
    p = R.plan(R.list_rules(st, status=R.ACTIVE), lab, R.items(_profile()))
    assert VIVA in p["hide"] and VIVA not in p["show"] and p["hide"][VIVA]["rule"] == hid
    assert p["beat"][VIVA]
    p2 = R.plan(R.list_rules(st, status=R.ACTIVE), lab, R.items(_profile()), {VIVA: "show"})
    assert VIVA in p2["show"] and p2["show"][VIVA]["rule"] == 0


def test_sentences():
    its = R.items(_profile())
    assert R.sentence({"action": "prefer", "items": [STANBIC, VIVA], "scope_kind": "industry", "scope_value": "52"}, its) \
        == "Bank and finance jobs: show Stanbic Bank, not Viva."
    assert R.sentence({"action": "apart", "items": [STANBIC, VIVA], "scope_kind": "all"}, its) \
        == "Every job: never show Stanbic Bank and Viva together."
    assert R.sentence({"action": "lead", "items": [BLACK], "scope_kind": "company_type", "scope_value": "startup"}, its) \
        == "Startup jobs: lead with BlackOrigin."


# ---------------------------------------------------------------------------- proposals
def test_a_chat_choice_is_proposed_and_saved_only_on_the_tap(st):
    its = R.items(_profile())
    rows = [{"action": "prefer", "items": ["Stanbic", "Viva"], "scope": "bank"}]
    props = R.propose_from_chat(st, rows, its, "app")
    assert len(props) == 1 and props[0]["text"] == "For bank and finance jobs: show Stanbic Bank, not Viva."
    assert R.list_rules(st) == []                                      # nothing saved yet
    assert R.propose_from_chat(st, rows, its, "app") == []             # never offered twice
    res = R.answer_proposal(st, props[0]["token"], True)
    assert res["saved"] and R.get_rule(st, res["rule_id"])["source"] == "chat"
    assert R.answer_proposal(st, props[0]["token"], True)["gone"]      # answered once


def test_not_quite_saves_nothing_and_unknown_names_or_scopes_propose_nothing(st):
    its = R.items(_profile())
    props = R.propose_from_chat(st, [{"action": "hide", "items": ["Viva"], "scope": "startups"}], its, "telegram")
    assert R.answer_proposal(st, props[0]["token"], False)["saved"] is False and R.list_rules(st) == []
    assert R.propose_from_chat(st, [{"action": "hide", "items": ["Google"], "scope": ""}], its, "app") == []
    assert R.propose_from_chat(st, [{"action": "hide", "items": ["Viva"], "scope": "the moon"}], its, "app") == []


def test_one_name_that_fits_two_roles_asks_which(st):
    p = _profile()
    p["experience"][2]["roles"].append({"title": "Intern", "dates": "Jun 2019 - Aug 2019", "bullets": ["Helped."]})
    props = R.propose_from_chat(st, [{"action": "hide", "items": ["Viva"], "scope": ""}], R.items(p), "app")
    assert props[0]["options"] == ["Viva (Data Analyst)", "Viva (Intern)"]
    res = R.answer_proposal(st, props[0]["token"], True, 1)
    assert R.get_rule(st, res["rule_id"])["items"] == ["role:viva|intern"]


def test_the_code_check_runs_before_any_model_call():
    its = R.items(_profile())
    assert R.looks_like_choice("For banks, show Stanbic, not Viva", its)
    assert not R.looks_like_choice("What jobs should I apply to?", its)
    assert not R.looks_like_choice("Show me remote jobs", its)


def test_a_do_not_mention_line_is_offered_as_a_hide_choice(st):
    its = R.items(_profile())
    notes = [{"id": 7, "type": "constraint", "value": "do not mention Viva"},
             {"id": 8, "type": "constraint", "value": "needs visa sponsorship"}]
    offers = R.constraint_offers(st, notes, its)
    assert [o["item"] for o in offers] == [VIVA]
    rid = R.settle_constraint(st, 7, VIVA, True)
    assert R.get_rule(st, rid)["action"] == "hide" and R.constraint_offers(st, notes, its) == []
    R.settle_constraint(st, 8, None, False)
    assert R.constraint_offers(st, notes, its) == []


# ---------------------------------------------------------------------------- one clean page
@requires_latex
@pytest.mark.parametrize("ch", [
    {"hide": {VIVA: SRC}}, {"lead": {BLACK: SRC}}, {"prefer": [[STANBIC, VIVA, SRC]]},
    {"apart": [[STANBIC, VIVA, SRC]]}, {"show": {VIVA: SRC}, "hide": {RADAR: SRC}},
])
def test_every_rule_combination_is_one_clean_page(ch, template_source, tmp_path):
    from llm.base import FakeLLM
    res = assemble_cv(template_source, _profile(), BANK_JD, FakeLLM(), tmp_path, tailor=False, choices=ch)
    assert res.ok and res.compile.pages == 1 and res.compile.overfull_count == 0, res.summary()
    assert "_lead" not in json.dumps(res.profile_used)
    shown = res.choices_report["shown"]
    for iid in (res.choices_report.get("excluded") or {}):
        assert iid not in shown
    if "hide" in ch and VIVA in ch["hide"]:
        assert "Viva" not in res.tex_source                       # the grow pass never brings it back


# ---------------------------------------------------------------------------- the builder
@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.delenv(M.ENV, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(data))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as A
    importlib.reload(A)
    S.set_sponsor_source(None)
    p = _profile()
    A._memory().save("default", p, {"identity": p["identity"]}, [])
    assert A._brain() is not None
    yield A, A.app.test_client()
    A._SESSION.pop("s", None)
    M.forget_process_state(data)


def _build(c, jd):
    assert c.post("/api/session/start", json={"jd": jd}).status_code == 200
    st = c.post("/api/session/answer", json={"text": "Nothing new. Everything is in my saved profile. Build it."}).get_json()
    assert st["phase"] == "review", st.get("messages")
    return st


def _items(st):
    return {i["id"]: i for i in st["choices"]["items"]}


@requires_latex
def test_the_builder_learns_a_choice_from_one_tap(app):
    A, c = app
    st = _build(c, BANK_JD)
    assert _items(st)[VIVA]["shown"] and st["choices"]["ask"] is None
    st = c.post("/api/session/shown", json={"id": VIVA, "show": False}).get_json()
    assert not _items(st)[VIVA]["shown"] and _items(st)[VIVA]["why"] == "Hidden on this resume."
    ask = st["choices"]["ask"]
    assert ask["text"] == "Hide Viva for other bank and finance jobs too?"
    assert [b["label"] for b in ask["buttons"]] == ["Yes, bank and finance jobs", "Only Acme Bank", "Just this time"]
    st = c.post("/api/session/choice", json={"answer": "scope"}).get_json()
    assert st["choice_saved"] == "Saved. Bank and finance jobs: hide Viva." and st["choices"]["ask"] is None
    assert c.post("/api/session/accept").get_json()["ok"]
    rules = c.get("/api/brain/rules").get_json()["rules"]
    assert [r["text"] for r in rules] == ["Bank and finance jobs: hide Viva."] and rules[0]["used"] == "Used on 1 resume"
    app_row = dict(A._brain().q("SELECT * FROM application")[0])
    assert app_row["industry"] == "52" and VIVA not in json.loads(app_row["shown"])

    # a second bank job: Viva is hidden, and nothing is asked
    st = _build(c, "Loan Officer at First Lending Bank\nRetail bank lending role. Mortgage, loan origination, underwriting.")
    assert not _items(st)[VIVA]["shown"] and _items(st)[VIVA]["why"] == "Hidden for bank and finance jobs."
    assert st["choices"]["ask"] is None
    # a startup job: Viva can show
    st = _build(c, "Data Analyst at Rocketly\nWe are a seed-stage startup. SQL, Python, dashboards for our sales team.")
    assert _items(st)[VIVA]["shown"]
    # delete the choice on the list: the next bank job shows the scorer's pick
    rid = rules[0]["id"]
    assert c.post(f"/api/brain/rules/{rid}", json={"delete": True}).get_json()["rules"] == []
    st = _build(c, BANK_JD)
    assert _items(st)[VIVA]["shown"]


@requires_latex
def test_just_this_time_keeps_the_line_away(app):
    A, c = app
    _build(c, BANK_JD)
    c.post("/api/session/shown", json={"id": VIVA, "show": False})
    st = c.post("/api/session/choice", json={"answer": "once"}).get_json()
    assert st["choices"]["ask"] is None and R.list_rules(A._brain()) == []
    _build(c, "Credit Analyst at Third Bank\nCommercial banking team. Underwriting, lending, credit analysis.")
    st = c.post("/api/session/shown", json={"id": VIVA, "show": False}).get_json()
    assert st["choices"]["ask"] is None                              # no ask for 30 days
    # switching back undoes it on this resume
    st = c.post("/api/session/shown", json={"id": VIVA, "show": True}).get_json()
    assert _items(st)[VIVA]["shown"]


@requires_latex
def test_overlapping_roles_are_asked_once_and_one_per_job_saves_apart(app):
    A, c = app
    st = _build(c, BANK_JD)
    ov = st["choices"]["overlap"]
    assert ov["text"] == "You were at Stanbic Bank and Viva at the same time (2020 to 2021). Show both, or one per job?"
    st = c.post("/api/session/overlap", json={"answer": "one"}).get_json()
    assert st["choice_saved"] == "Saved. Every job: never show Stanbic Bank and Viva together."
    shown = [i for i in (STANBIC, VIVA) if _items(st)[i]["shown"]]
    assert shown == [STANBIC]                                        # the better match for a bank
    assert "never show" in _items(st)[VIVA]["why"]
    st = _build(c, "Data Analyst at Rocketly\nWe are a seed-stage startup. SQL, Python, dashboards for our sales team.")
    assert st["choices"]["overlap"] is None                          # asked once
    assert [i for i in (STANBIC, VIVA) if _items(st)[i]["shown"]] == [VIVA]


@requires_latex
def test_swap_shows_the_other_role_and_offers_prefer(app):
    A, c = app
    c.post("/api/brain/rules", json={"action": "apart", "items": [STANBIC, VIVA], "scope_kind": "all"})
    st = _build(c, BANK_JD)
    assert _items(st)[STANBIC]["swap"] == [{"id": VIVA, "label": "Viva"}]
    st = c.post("/api/session/swap", json={"from": STANBIC, "to": VIVA}).get_json()
    assert _items(st)[VIVA]["shown"] and not _items(st)[STANBIC]["shown"]
    assert st["choices"]["ask"]["text"] == "Use Viva instead of Stanbic Bank for other bank and finance jobs?"
    c.post("/api/session/choice", json={"answer": "company"})
    rules = c.get("/api/brain/rules").get_json()["rules"]
    assert "Acme Bank: show Viva, not Stanbic Bank." in [r["text"] for r in rules]


@requires_latex
def test_a_renamed_role_on_the_page_keeps_its_choice(app):
    A, c = app
    rid = R.add_rule(A._brain(), "hide", [STANBIC], S.INDUSTRY, "52")
    st = _build(c, BANK_JD)
    st = c.post("/api/session/field", json={"ref": "ent-2-org", "text": "Stanbic Bank Ghana"}).get_json()
    assert st["edit_ok"], st.get("edit_error")
    assert R.get_rule(A._brain(), rid)["items"] == ["role:stanbic bank ghana|credit analyst"]
    assert R.get_rule(A._brain(), rid)["status"] == R.ACTIVE


@requires_latex
def test_a_choice_said_in_the_builder_chat_waits_for_save(app):
    A, c = app
    _build(c, BANK_JD)
    st = c.post("/api/session/answer", json={"text": "For bank jobs, show Stanbic, not Viva."}).get_json()
    props = st["rule_proposals"]
    assert props[0]["text"] == "For bank and finance jobs: show Stanbic Bank, not Viva."
    assert R.list_rules(A._brain()) == []
    st = c.post("/api/session/proposal", json={"token": props[0]["token"], "save": True}).get_json()
    assert st["messages"] == ["Saved. Bank and finance jobs: show Stanbic Bank, not Viva."]
    assert not _items(st)[VIVA]["shown"] and "rule_proposals" not in st


def test_telegram_proposes_with_buttons_and_saves_on_the_tap(app):
    A, c = app
    sent = []

    class Ch:
        def send(self, text, buttons=None):
            sent.append((text, buttons))
            return {"ok": True}

        def ack(self, ev, text=""):
            pass
    hub = A._notify_hub()
    out = hub.handle_event(Ch(), {"type": "text", "text": "On startup jobs, lead with BlackOrigin"})
    assert out == "For startup jobs: lead with BlackOrigin."
    text, buttons = sent[-1]
    assert [b["label"] for row in buttons for b in row] == ["Save", "Not quite"]
    assert R.list_rules(A._brain()) == []
    reply = hub.handle_event(Ch(), {"type": "button", "data": buttons[0][0]["id"]})
    assert reply.startswith("Saved. Startup jobs: lead with BlackOrigin.")
    assert [r["action"] for r in R.list_rules(A._brain())] == ["lead"]
    assert hub.handle_event(Ch(), {"type": "button", "data": buttons[0][0]["id"]}) == "That one was answered already."
    # a message that names no role of theirs goes to the assistant, not the proposer
    assert A._rule_proposer("What should I apply to this week?") is None


def test_the_list_edits_pauses_and_deletes(app):
    A, c = app
    d = c.post("/api/brain/rules", json={"action": "hide", "items": [VIVA], "scope_kind": "industry",
                                         "scope_value": "52"}).get_json()
    rid = d["saved"]
    assert d["rules"][0]["text"] == "Bank and finance jobs: hide Viva." and d["available"]
    assert any(o["kind"] == "company_type" for o in d["scopes"])
    d = c.post(f"/api/brain/rules/{rid}", json={"scope_kind": "company_type", "scope_value": "startup"}).get_json()
    assert d["rules"][0]["text"] == "Startup jobs: hide Viva."
    d = c.post(f"/api/brain/rules/{rid}", json={"status": "paused"}).get_json()
    assert d["rules"][0]["status"] == "paused"
    assert c.post("/api/brain/rules", json={"action": "hide", "items": ["role:google|ceo"]}).status_code == 400
    d = c.post(f"/api/brain/rules/{rid}", json={"delete": True}).get_json()
    assert d["rules"] == []
    assert c.post(f"/api/brain/rules/{rid}", json={"delete": True}).status_code == 404
