"""The hidden Auto run of 2026-11-19: eight applications, five that did not finish.

* left-site (three of eight): where the run went is kept on the record and shown; an
  aggregator's ad link adopts the employer site it lands on; a careers site's hand-off to its
  hiring system (SAP SuccessFactors on sapsf.com, Oracle Recruiting Cloud, Phenom, Jibe ...)
  stays on the run; a third-party survey is a person step, not left-site;
* the shell under node (tests/js/test_queue_run_2026_11_19.js).

Everyone and every site here is made up.
"""
from __future__ import annotations

import importlib
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
