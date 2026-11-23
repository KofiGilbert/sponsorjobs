"""Two follow-ups from the hidden re-run of 2026-11-19.

* A required privacy notice behind a button (SAP SuccessFactors at Gainwell: "Read and accept
  the data privacy statement" opens a dialog with Accept): opened and accepted under the consent
  to required notices, once per site and notice, never marketing (tests/js/test_consent_dialog.js,
  headless);
* a job whose link lands on a board that forbids automated applications (an Adzuna ad that went
  to Dice): said plainly on the row with "Open and finish", set aside in Auto without counting as
  a failure, and the same ad skipped by Auto's plan from what a run recorded
  (tests/js/test_prohibited_board.js).

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

ROOT = Path(__file__).resolve().parents[1]
H = {"X-Tailor-Extension": "1"}
AD = "https://www.adzuna.com/land/ad/5551234?se=abc&utm_source=feed"


def _playwright_dir():
    for c in (os.environ.get("SJ_PLAYWRIGHT_DIR"), ROOT / "shell-electron" / "node_modules" / "playwright",
              Path.home() / "sponsorjobs" / "shell-electron" / "node_modules" / "playwright"):
        if c and (Path(c) / "package.json").exists():
            return Path(c)
    return None


def _node(name, timeout=240):
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / name)], capture_output=True, text=True, timeout=timeout)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr


@pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None, reason="needs node + playwright")
def test_a_required_notice_behind_a_button_is_opened_and_accepted_under_consent():
    _node("test_consent_dialog.js")


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_a_prohibited_board_is_said_plainly_and_set_aside():
    _node("test_prohibited_board.js")


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


def _add(mod, company, url):
    recs = mod._records()
    try:
        return recs.add("Analyst", company, 80, "", data={"status": "approved", "fit": 80, "source_job": {"url": url}})
    finally:
        recs.close()


def test_the_row_says_which_board_and_offers_open_and_finish(A):
    mod = A
    rid = _add(mod, "Brightwater", AD)
    assert _post(mod, "/api/autoapply/agent/left-site", {"record_id": rid, "host": "dice.com", "step": 2,
                                                         "from_host": "adzuna.com", "kind": "prohibited", "board": "Dice"})["ok"]
    data = mod._record_data(mod._get_record(rid))
    assert data["agent_left"]["kind"] == "prohibited" and data["agent_left"]["board"] == "Dice"
    row = mod._aa_row(mod._get_record(rid), data, {})
    assert row["board_stop"] == {"host": "dice.com", "board": "Dice"}
    assert row["action"] == "open_self" and row["left_site"] is None and row["tile"] == "waiting"
    for loc in ("de", "en", "es", "fr", "pt"):
        t = json.loads((ROOT / "ui" / "static" / "locales" / f"{loc}.json").read_text(encoding="utf-8"))["aa.st.board"]
        assert "{board}" in t and "—" not in t and "–" not in t
    js = (ROOT / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'open_self: ["aa.act.finish", "Open and finish"]' in js


def test_auto_skips_the_job_and_any_other_with_the_same_ad_from_what_a_run_recorded(A):
    mod = A
    mod._set_apply_mode("auto")
    first = _add(mod, "Brightwater", AD)
    same_ad = _add(mod, "Brightwater Freight", AD.replace("se=abc", "se=xyz"))     # the same ad, another query
    other = _add(mod, "Cove", "https://boards.greenhouse.io/cove/jobs/1")
    plan = _post(mod, "/api/autoapply/auto/plan", {"peek": True})
    assert {j["recordId"] for j in plan["jobs"]} == {first, same_ad, other}       # nothing recorded yet: nothing skipped
    _post(mod, "/api/autoapply/agent/left-site", {"record_id": first, "host": "dice.com", "step": 2, "kind": "prohibited", "board": "Dice"})
    plan = _post(mod, "/api/autoapply/auto/plan", {"peek": True})
    assert {j["recordId"] for j in plan["jobs"]} == {other}
    # an ordinary left-site never skips anything
    _post(mod, "/api/autoapply/agent/left-site", {"record_id": first, "host": "shop.unrelated.example", "step": 2, "kind": "left"})
    plan = _post(mod, "/api/autoapply/auto/plan", {"peek": True})
    assert {j["recordId"] for j in plan["jobs"]} == {first, same_ad, other}
    assert mod._ad_key(AD) == "adzuna.com/land/ad/5551234"


def test_country_or_region_of_residence_is_the_country_field():
    from autoapply.planner import contact_key
    assert contact_key("Country/Region of Residence:*") == "country"
    assert contact_key("Country or region") == "country" and contact_key("Country of residence") == "country"
    assert contact_key("Country of citizenship") == ""                  # never the residence
