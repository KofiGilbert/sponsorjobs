"""The free plan's stated limits and the upgrade moments, app side (owner decision 2026-11-25).

Free: 3 tailored resumes a month and 1 scored recorded practice interview a month. Both are counted
by the broker (worker/src/metering.ts); this pins what the app does with the broker's answers:

  * a recorded interview is counted ONCE, when it is first scored;
  * with this month's used, another one does not start, and the reply is the one decided line with
    `upgrade: "plans"` (the page puts a "See plans" button next to it);
  * paid plans, the person's own key, and a broker that is down or too old never block anything;
  * using the 3rd free tailored resume hands the page one upgrade moment, once.

Offline: FakeLLM, and the broker is two monkeypatched functions. No network, no camera.
"""

from __future__ import annotations

import importlib

import pytest

RECORDED_LINE = "You've used this month's practice interview. Plans include unlimited recorded practice."
RESUMES_LINE = "You've used your 3 free tailored resumes this month. Plans include 40 or 60 a month."


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_DISABLE_STT", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


class Broker:
    """A stand-in broker with the Worker's rules: Free has `allowance` scored recorded interviews
    a month; a paid tier is unlimited (recorded_left null) and never counted."""

    def __init__(self, tier="free", allowance=1, status=200):
        self.tier, self.left, self.status, self.posts = tier, allowance, status, []

    def get(self, path):
        if self.status != 200:
            return self.status, None
        left = None if self.tier != "free" else self.left
        return 200, {"tier": self.tier, "plan": self.tier, "recorded_left": left, "packages_left": 3}

    def post(self, path, body):
        self.posts.append(path)
        if self.status != 200:
            return self.status, None
        if path != "/interview/recorded":
            return 200, {}
        if self.tier != "free":
            return 200, {"recorded_left": None, "tier": self.tier}
        if self.left <= 0:
            return 402, {"reason": "recorded_limit", "tier": "free", "recorded_left": 0}
        self.left -= 1
        return 200, {"recorded_left": self.left, "tier": "free"}


def _wire(A, monkeypatch, broker, bundled=True):
    monkeypatch.setattr(A, "_broker_get", broker.get)
    monkeypatch.setattr(A, "_broker_post", broker.post)
    monkeypatch.setattr(A, "_on_bundled_ai", lambda: bundled)
    return broker


def _prep(c):
    return c.post("/api/preps", json={"role": "Data Analyst", "company": "Acme",
                                      "jd": "SQL, Tableau dashboards, stakeholder reporting."}).get_json()["prep"]


def _answered_screen(A, c, prep):
    """A started screen with one answer that has words, written straight to the store."""
    r = c.post("/api/screens", json={"prep_id": prep["id"]})
    assert r.status_code == 200, r.get_json()
    sid = r.get_json()["id"]
    screens = A._load_screens()
    cur = next(s for s in screens if s["id"] == sid)
    cur["answers"][0]["transcript"] = "At Acme I built a Tableau dashboard that cut reporting time in half."
    A._save_screens(screens)
    return sid


# ---- the recorded practice interview ------------------------------------------------------------
def test_free_scores_one_recorded_interview_then_the_next_does_not_start(client, monkeypatch):
    A, c = client
    b = _wire(A, monkeypatch, Broker())
    prep = _prep(c)
    sid = _answered_screen(A, c, prep)
    assert b.posts == []                                   # starting one counts nothing
    r = c.post(f"/api/screens/{sid}/score")
    assert r.status_code == 200 and r.get_json()["result"]["is_practice"] is True
    assert b.posts == ["/interview/recorded"] and b.left == 0
    # Another recorded interview does not start: the one line, and the plans.
    r = c.post("/api/screens", json={"prep_id": prep["id"]})
    assert r.status_code == 402
    assert r.get_json() == {"error": RECORDED_LINE, "error_code": "recorded_limit", "upgrade": "plans"}
    assert "reason" not in r.get_json()                    # a `reason` would open the outage screen
    assert len(A._load_screens()) == 1                     # nothing was created
    # A re-drill from the report is a recorded interview too.
    r = c.post("/api/screens", json={"prep_id": prep["id"], "drill": [{"q": "Tell me about a time you led."}]})
    assert r.status_code == 402


def test_scoring_the_same_interview_again_is_never_counted_twice(client, monkeypatch):
    A, c = client
    b = _wire(A, monkeypatch, Broker())
    sid = _answered_screen(A, c, _prep(c))
    assert c.post(f"/api/screens/{sid}/score").status_code == 200
    assert c.post(f"/api/screens/{sid}/score").status_code == 200      # a retry, a rescue
    assert c.post(f"/api/screens/{sid}/finish", json={"retry": True}).status_code == 200
    assert b.posts == ["/interview/recorded"]
    assert next(s for s in A._load_screens() if s["id"] == sid)["counted"] is True


def test_an_interview_scored_before_this_limit_existed_is_not_counted(client, monkeypatch):
    A, c = client
    b = _wire(A, monkeypatch, Broker())
    sid = _answered_screen(A, c, _prep(c))
    screens = A._load_screens()
    next(s for s in screens if s["id"] == sid)["result"] = {"score": 71, "passed": True}   # an old report
    A._save_screens(screens)
    assert c.post(f"/api/screens/{sid}/score").status_code == 200
    assert b.posts == [] and b.left == 1


def test_a_second_interview_started_before_the_first_was_scored_is_refused_at_scoring(client, monkeypatch):
    """The broker's count is the limit: two started while one was still unscored cannot both be scored."""
    A, c = client
    _wire(A, monkeypatch, Broker())
    prep = _prep(c)
    first, second = _answered_screen(A, c, prep), _answered_screen(A, c, prep)
    assert c.post(f"/api/screens/{first}/score").status_code == 200
    r = c.post(f"/api/screens/{second}/finish", json={})
    assert r.status_code == 402
    assert r.get_json() == {"error": RECORDED_LINE, "error_code": "recorded_limit", "upgrade": "plans"}
    assert next(s for s in A._load_screens() if s["id"] == second)["result"] is None   # recordings kept, not scored


def test_paid_plans_are_unlimited(client, monkeypatch):
    A, c = client
    b = _wire(A, monkeypatch, Broker(tier="standard_monthly"))
    prep = _prep(c)
    for _ in range(3):
        sid = _answered_screen(A, c, prep)
        assert c.post(f"/api/screens/{sid}/score").status_code == 200
    assert b.posts == ["/interview/recorded"] * 3          # asked each time, never refused


def test_own_key_is_never_counted_or_blocked(client, monkeypatch):
    A, c = client
    b = _wire(A, monkeypatch, Broker(allowance=0), bundled=False)
    sid = _answered_screen(A, c, _prep(c))
    assert c.post(f"/api/screens/{sid}/score").status_code == 200
    assert b.posts == []


@pytest.mark.parametrize("status", [0, 404, 500])
def test_a_broker_that_is_down_or_too_old_never_blocks(client, monkeypatch, status):
    A, c = client
    _wire(A, monkeypatch, Broker(allowance=0, status=status))
    sid = _answered_screen(A, c, _prep(c))                 # starts
    assert c.post(f"/api/screens/{sid}/score").status_code == 200      # and scores


def test_an_older_broker_that_does_not_report_the_count_never_blocks_a_start(client, monkeypatch):
    A, c = client
    monkeypatch.setattr(A, "_on_bundled_ai", lambda: True)
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"tier": "free", "packages_left": 3}))
    assert A._free_recorded_used_up() is False
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"tier": "free", "recorded_left": 0}))
    assert A._free_recorded_used_up() is True
    monkeypatch.setattr(A, "_broker_get", lambda path: (200, {"tier": "student_monthly", "recorded_left": None}))
    assert A._free_recorded_used_up() is False


# ---- the 3rd free tailored resume ---------------------------------------------------------------
def _broker_llm():
    from llm.broker_client import BrokerLLM
    return BrokerLLM(broker_url="http://broker.invalid")


def test_using_the_third_free_resume_hands_the_page_one_upgrade_moment(client, monkeypatch):
    A, c = client
    left = iter([2, 1, 0])
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (200, {"packages_left": next(left), "tier": "free"}))
    llm = _broker_llm()
    for _ in range(2):
        A._count_package(llm)
        assert "X-SJ-Moment" not in c.get("/api/screens/config").headers
    A._count_package(llm)                                   # the 3rd
    assert c.get("/").headers.get("X-SJ-Moment") is None    # only on an /api/ reply
    assert c.get("/api/screens/config").headers["X-SJ-Moment"] == "free_resumes_used"
    assert "X-SJ-Moment" not in c.get("/api/screens/config").headers       # once


def test_a_paid_plan_running_out_is_not_the_free_moment(client, monkeypatch):
    A, c = client
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (200, {"packages_left": 0, "tier": "standard_monthly"}))
    A._count_package(_broker_llm())
    assert "X-SJ-Moment" not in c.get("/api/screens/config").headers


def test_the_fourth_free_resume_is_refused_with_the_decided_line(client, monkeypatch):
    A, _ = client
    from llm.broker_client import BrokerUnavailable
    monkeypatch.setattr(A, "_broker_post", lambda path, body: (402, {"reason": "package_limit", "tier": "free"}))
    with pytest.raises(BrokerUnavailable) as e:
        A._count_package(_broker_llm())
    assert str(e.value) == RESUMES_LINE and e.value.reason == "upgrade_required"


# ---- only monthly plans are sold; the broker holds the switch -----------------------------------
def test_a_quarterly_checkout_the_broker_refuses_reads_as_not_on_sale(client, monkeypatch):
    A, c = client
    seen = []

    def post(path, body):
        seen.append((path, body))
        return 400, {"error": "that plan is not on sale", "reason": "not_on_sale"}
    monkeypatch.setattr(A, "_broker_post", post)
    r = c.post("/api/billing/subscribe", json={"interval": "quarterly", "consent": True})
    assert r.status_code == 400
    assert r.get_json() == {"error": "That plan isn't on sale.", "reason": "not_on_sale"}
    assert seen == [("/billing/subscribe", {"interval": "quarterly", "consent": True})]
