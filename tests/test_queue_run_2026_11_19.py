"""The hidden Auto run of 2026-11-19: eight applications, five that did not finish.

* left-site (three of eight): where the run went is kept on the record and shown; an
  aggregator's ad link adopts the employer site it lands on; a careers site's hand-off to its
  hiring system (SAP SuccessFactors on sapsf.com, Oracle Recruiting Cloud, Phenom, Jibe ...)
  stays on the run; a third-party survey is a person step, not left-site;
* Workday's Languages rows (record 23, 48 steps): filled from the profile's languages with the
  closest proficiency, an empty row removed with its own Delete, a language never invented;
* the export-control question (record 1, Lam on Phenom): answered from the saved citizenship (and
  a permanent residence when it asks), the list's own "Other" when the country is not on it;
* the shell under node (tests/js/test_queue_run_2026_11_19.js).

Everyone and every site here is made up.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from submit import policy

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}


# --------------------------------------------------------------------------- the fence
@pytest.mark.parametrize("host", [
    "career41.sapsf.com", "career2.successfactors.eu", "brightwater.wd5.myworkdayjobs.com",
    "wd3.myworkdaysite.com", "brightwater.jibeapply.com", "careers-brightwater.icims.com",
    "brightwater.avature.net", "cdn.phenompeople.com", "brightwater.eightfold.ai",
    "eeho.fa.us2.oraclecloud.com", "brightwater.taleo.net",
])
def test_a_careers_site_hands_off_to_its_hiring_system(host):
    assert policy.is_ats_host(host)


@pytest.mark.parametrize("host", ["objectstorage.us-ashburn-1.oraclecloud.com", "shop.unrelated.example", "linkedin.com"])
def test_not_a_hiring_system(host):
    assert not policy.is_ats_host(host)


def test_the_run_fence_says_whether_the_link_is_an_aggregators_ad():
    f = policy.run_fence("https://www.adzuna.com/land/ad/1?se=x")
    assert f["aggregator"] is True and "adzuna.com" in f["hosts"]
    assert "adzuna.com" in f["aggregators"] and "freehire.me" in f["aggregators"] and "remotive.com" in f["aggregators"]
    assert "wotcgs.ey.com" in f["surveys"] and "linkedin.com" in f["prohibited"]
    assert "sapsf.com" in f["hosts"] and f["patterns"]
    assert policy.run_fence("https://jobs.brightwater.example/job/1")["aggregator"] is False
    # a prohibited site has no hosts at all
    assert policy.run_fence("https://www.linkedin.com/jobs/view/1")["hosts"] == []
    assert policy.is_survey_host("https://wotcgs.ey.com/x") and not policy.is_survey_host("https://brightwater.example")


def test_the_shell_mirrors_the_engines_lists():
    js = (ROOT / "shell-electron" / "agent.js").read_text(encoding="utf-8")
    for h in policy.ATS_HOSTS + policy.AGGREGATOR_HOSTS + policy.SURVEY_HOSTS:
        assert f'"{h}"' in js, h


# --------------------------------------------------------------------------- the record
@pytest.fixture()
def A(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    import ui.app as mod
    importlib.reload(mod)
    return mod


def _post(mod, path, body=None):
    return mod.app.test_client().post(path, json=body or {}, headers=H).get_json()


def _get(mod, path):
    return mod.app.test_client().get(path, headers=H).get_json()


def _add(mod, url):
    recs = mod._records()
    try:
        return recs.add("Analyst", "Brightwater Freight", 80, "", data={"status": "approved", "fit": 80, "source_job": {"url": url}})
    finally:
        recs.close()


def test_where_the_run_went_is_kept_on_the_record_and_shown(A):
    mod = A
    rid = _add(mod, "https://jobs.brightwater.example/job/1")
    r = _post(mod, "/api/autoapply/agent/left-site", {"record_id": rid, "host": "Shop.Unrelated.example", "step": 3,
                                                      "from_host": "jobs.brightwater.example", "kind": "left"})
    assert r["ok"]
    data = mod._record_data(mod._get_record(rid))
    assert data["agent_left"]["host"] == "shop.unrelated.example" and data["agent_left"]["step"] == 3
    assert data["agent_left"]["from_host"] == "jobs.brightwater.example" and data["agent_left"]["at"]
    row = mod._aa_row(mod._get_record(rid), data, {})
    assert row["left_site"] == {"host": "shop.unrelated.example", "step": 3}
    # a new run clears it
    assert _post(mod, "/api/autoapply/agent/left-site", {"record_id": rid, "clear": True})["ok"]
    data = mod._record_data(mod._get_record(rid))
    assert not data.get("agent_left") and mod._aa_row(mod._get_record(rid), data, {})["left_site"] is None
    # a survey is kept, but it is not a left-site line on the row
    _post(mod, "/api/autoapply/agent/left-site", {"record_id": rid, "host": "wotcgs.ey.com", "step": 25, "kind": "survey"})
    data = mod._record_data(mod._get_record(rid))
    assert data["agent_left"]["kind"] == "survey" and mod._aa_row(mod._get_record(rid), data, {})["left_site"] is None
    assert _post(mod, "/api/autoapply/agent/left-site", {"record_id": 99999, "host": "x"})["reason"] == "no-record"


def test_the_allowed_hosts_route_carries_the_fence(A):
    r = _get(A, "/api/autoapply/agent/allowed-hosts?url=" + "https%3A%2F%2Fwww.adzuna.com%2Fland%2Fad%2F1")
    assert r["ok"] and r["aggregator"] is True and "wotcgs.ey.com" in r["surveys"] and "sapsf.com" in r["hosts"]


def test_the_row_line_is_in_five_locales():
    import json
    for loc in ("de", "en", "es", "fr", "pt"):
        t = json.loads((ROOT / "ui" / "static" / "locales" / f"{loc}.json").read_text(encoding="utf-8"))
        assert "{host}" in t["aa.st.left"] and "—" not in t["aa.st.left"] and "–" not in t["aa.st.left"]


# --------------------------------------------------------------------------- the shell
def test_the_shell_under_node():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_queue_run_2026_11_19.js")], capture_output=True, text=True, timeout=240)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr


# --------------------------------------------------------------------------- languages
def _playwright_dir():
    for c in (os.environ.get("SJ_PLAYWRIGHT_DIR"), ROOT / "shell-electron" / "node_modules" / "playwright",
              Path.home() / "sponsorjobs" / "shell-electron" / "node_modules" / "playwright"):
        if c and (Path(c) / "package.json").exists():
            return Path(c)
    return None


browser = pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None,
                             reason="needs node + playwright (shell-electron devDependency)")


def run_page(tmp_path, page, steps, query=""):
    sc = {"core": str(ROOT / "extension" / "autofill_core.js"), "page": page, "query": query, "steps": steps}
    p = tmp_path / "scenario.json"
    p.write_text(json.dumps(sc), encoding="utf-8")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "forms_harness.js"), str(p)], capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])["results"]


def test_the_profiles_languages_for_a_form():
    from autoapply.history import form_languages
    got = form_languages({"languages": ["English (Native)", "Spanish - conversational", {"name": "French", "level": "Fluent"}, "German basic"]})
    assert got == [{"name": "English", "level": "Native"}, {"name": "Spanish", "level": "conversational"},
                   {"name": "French", "level": "Fluent"}, {"name": "German", "level": "basic"}]
    assert form_languages({"languages": "English, Twi"}) == [{"name": "English", "level": ""}, {"name": "Twi", "level": ""}]
    assert form_languages({}) == [] and form_languages({"languages": []}) == []


def test_a_language_is_never_a_best_guess():
    from autoapply import guess
    langs = ["Akan", "Albanian", "English", "Spanish"]
    assert guess.choose("Language", langs) is None
    assert guess.choose("Read", ["Basic", "Fluent", "Intermediate"]) is None
    ok, why = guess.accept("Language", "English", langs)
    assert not ok and "never made up" in why
    assert not guess.accept("Speak", "Fluent", ["Basic", "Fluent", "Intermediate"])[0]
    assert guess.choose("Are you at least 18 years of age?", ["Yes", "No"])[0] == "Yes"      # unchanged


@browser
def test_workday_language_rows_are_filled_from_the_profile(tmp_path):
    payload = {"languages": [{"name": "English", "level": "Native"}, {"name": "Spanish", "level": "Intermediate"}]}
    r = run_page(tmp_path, "workday_languages.html", [
        {"do": "inject"},
        {"do": "eval", "as": "rep", "js": f"window.TailorAutofill.fillAsync({json.dumps(dict(payload, fields={}, screening={}, eeo={}))})"},
        {"do": "wait", "ms": 300},
        {"do": "eval", "as": "v", "js": "window.fxValues()"},
    ])
    v = r["v"]
    assert v["rows"] == 2                                              # "Add" for the second language
    en, es = v["values"]
    # native: the closest the list offers (Fluent), and "I am fluent in this language"
    assert en == {"language": "English", "native": True, "read": "Fluent", "speak": "Fluent", "write": "Fluent"}
    assert es == {"language": "Spanish", "native": False, "read": "Intermediate", "speak": "Intermediate", "write": "Intermediate"}
    assert v["siteLang"] == ""                                         # the site's own language picker: untouched
    assert r["rep"]["history"] >= 9


@browser
def test_an_empty_language_row_is_removed_never_filled_with_an_invented_language(tmp_path):
    r = run_page(tmp_path, "workday_languages.html", [
        {"do": "inject"},
        {"do": "eval", "as": "rep", "js": "window.TailorAutofill.fillLanguages({languages: []})"},
        {"do": "wait", "ms": 300},
        {"do": "eval", "as": "v", "js": "window.fxValues()"},
    ])
    assert r["rep"]["removed"] == 1 and r["v"]["rows"] == 0 and r["v"]["siteLang"] == ""
    # a section that requires its row (no Delete): the row stays for the person, nothing invented
    r = run_page(tmp_path, "workday_languages.html", [
        {"do": "inject"},
        {"do": "eval", "as": "rep", "js": "window.TailorAutofill.fillLanguages({languages: []})"},
        {"do": "eval", "as": "v", "js": "window.fxValues()"},
    ], query="?nodelete")
    assert r["rep"]["kept"] == 1 and r["v"]["rows"] == 1 and r["v"]["values"][0]["language"] is None


# --------------------------------------------------------------------------- export control
LAM = ("For the sole purpose of determining export licensing requirements, please provide your most recent "
       "country/region of citizenship or legal permanent residence. If your country/region is not listed please select Other.")
CONTROLLED = ["Armenia", "Azerbaijan", "Belarus", "Cambodia", "China", "Cuba", "Georgia", "Hong Kong",
              "Iran, Islamic Republic of", "Iraq", "Kazakhstan", "Kyrgyzstan", "Macao", "Other"]
H1B = {"countries": {"US": {"status": "h1b"}}}
GC = {"countries": {"US": {"status": "green_card"}}}


@pytest.fixture()
def lib(tmp_path):
    from autoapply import basics
    from autoapply.answers import AnswerLibrary
    with AnswerLibrary(tmp_path / "a.db") as L:
        basics.write(L, {"citizenship": "Ghana"})
        yield L


@pytest.mark.parametrize("q", [
    LAM, LAM[:200], LAM[:80] + " country/region of citizenship",
    "For export control purposes, what is your country of citizenship?",
    "Deemed export: country of citizenship",
    "ITAR: Country of citizenship",
    "EAR: please provide your country of citizenship",
    "Under the Export Administration Regulations (EAR), please state your citizenship",
    "What is your citizenship for export control purposes?",
])
def test_export_control_wordings_are_the_citizenship_question_and_legal(q):
    from autoapply import planner, qclass
    from autoapply.answers import normalize
    assert normalize(q) == "@citizenship" and planner.export_question(q), q
    assert qclass.question_class(q) == qclass.LEGAL


def test_not_export_questions():
    from autoapply import planner
    for q in ("Country of citizenship", "Citizenship Status (for export control purposes)",
              "Are you a citizen of Cuba, Iran, North Korea, Syria?", "Second country of citizenship"):
        assert not planner.export_question(q), q


def test_the_lam_list_takes_other_when_the_country_is_not_on_it(lib):
    from autoapply import asks as AK
    assert AK.known_answer(LAM, CONTROLLED, library=lib, work_auth=H1B) == ("Other", "library")
    # on the list: the country itself
    assert AK.known_answer(LAM, CONTROLLED[:-1] + ["Ghana", "Other"], library=lib, work_auth=H1B) == ("Ghana", "library")
    # a list read only in part: "Other" is never assumed
    assert AK.known_answer(LAM, CONTROLLED[:-1], library=lib, work_auth=H1B, partial=True) is None
    # a list with no Other and without the country: left
    assert AK.known_answer(LAM, CONTROLLED[:-1], library=lib, work_auth=H1B) is None
    # a text box: the saved country
    assert AK.known_answer(LAM, [], library=lib, work_auth=H1B) == ("Ghana", "library")


def test_a_permanent_residence_is_the_more_recent_answer(lib):
    from autoapply import asks as AK
    full = ["Ghana", "United States of America", "Other"]
    assert AK.known_answer(LAM, full, library=lib, work_auth=GC) == ("United States of America", "library")
    assert AK.known_answer(LAM, CONTROLLED, library=lib, work_auth=GC) == ("Other", "library")
    assert AK.known_answer(LAM, [], library=lib, work_auth=GC) == ("United States (permanent resident); citizen of Ghana", "library")
    # a question about citizenship alone never takes the residence
    q = "For export control purposes, what is your country of citizenship?"
    assert AK.known_answer(q, full, library=lib, work_auth=GC) == ("Ghana", "library")


def test_nothing_saved_leaves_it(tmp_path):
    from autoapply import asks as AK
    from autoapply.answers import AnswerLibrary
    with AnswerLibrary(tmp_path / "b.db") as L:
        assert AK.known_answer(LAM, CONTROLLED, library=L, work_auth=H1B) is None


def test_the_planner_fills_the_lam_list(lib):
    from autoapply import KnownContext, plan_known
    from autoapply.fields import FormField
    res = plan_known([FormField("s", LAM, "select", CONTROLLED, required=True)], KnownContext(library=lib, work_auth=H1B))
    assert [(a.ref, a.value, a.origin) for a in res.actions] == [("s", "Other", "library")] and not res.left
