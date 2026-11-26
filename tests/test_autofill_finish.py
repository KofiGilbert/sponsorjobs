"""Autofill, then the agent finishes (2026-11-26), the engine's side and the drawn pointer.

The Autofill button fills fast first; the shell hands the page to the agent only when the fill left
required fields, more pages or a job page (tests/js/test_autofill_finish.js, run below). Pinned here:

  * POST /api/autoapply/finish/start counts that application ONCE a month with the broker, where
    the plan's allowance is kept (Free 3, Student 40, Standard 60): a second Autofill on the same
    application counts no more; used up, the fast Autofill stands with one line saying when it
    resets; the person's own AI key, and a broker that is down or too old, never block it;
  * with the apply agent switched off (the launch), the agent's page routes answer only while a
    finish run is open, never Auto mode's queue routes, the parked list stays empty (nothing is
    carried on by itself) and the decision route never allows a click on Submit, whatever the
    saved mode says;
  * the plan and the cover letter report what they cost, for the run's one cost figure;
  * the drawn pointer sits beside the field, never on it, labels what it does, jumps under
    reduced motion, and is gone when hidden.
Offline: FakeLLM, the broker is monkeypatched, the pointer runs in a headless Chromium.
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

try:
    from tests.test_form_widgets import _playwright_dir, run
except ImportError:                                   # run from the tests folder
    from test_form_widgets import _playwright_dir, run  # type: ignore

browser = pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None,
                             reason="needs node + playwright (shell-electron devDependency)")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RESUME_AGENT_CRED_FILE", str(tmp_path / "credentials.env"))
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_DISABLE_STT", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SJ_APPLY_AGENT", raising=False)
    import ui.app as A
    importlib.reload(A)
    return A, A.app.test_client()


class Broker:
    """The Worker's /apply/finish: `left` this month, 402 finish_limit when none is left."""

    def __init__(self, left=3, limit=3, status=200):
        self.left, self.limit, self.status, self.posts = left, limit, status, []

    def post(self, path, body):
        self.posts.append(path)
        if self.status != 200:
            return self.status, None
        if path != "/apply/finish":
            return 200, {}
        if self.left <= 0:
            return 402, {"reason": "finish_limit", "finishes_left": 0, "finishes_limit": self.limit,
                         "finishes_reset": "2026-12-01"}
        self.left -= 1
        return 200, {"finishes_left": self.left, "finishes_limit": self.limit, "finishes_reset": "2026-12-01", "tier": "free"}


def _wire(A, monkeypatch, broker, bundled=True):
    monkeypatch.setattr(A, "_broker_post", broker.post)
    monkeypatch.setattr(A, "_on_bundled_ai", lambda: bundled)
    return broker


def _end(c):
    assert c.post("/api/autoapply/finish/end", json={}).get_json()["ok"] is True


def test_one_count_per_application_a_month(client, monkeypatch):
    A, c = client
    b = _wire(A, monkeypatch, Broker(left=3))
    r = c.post("/api/autoapply/finish/start", json={"record_id": 5}).get_json()
    assert r == {"ok": True, "counted": True, "left": 2, "limit": 3, "resets": "2026-12-01"}
    _end(c)
    again = c.post("/api/autoapply/finish/start", json={"record_id": 5}).get_json()
    assert again["ok"] is True and again["counted"] is False          # the same application: not again
    _end(c)
    assert b.posts == ["/apply/finish"]
    other = c.post("/api/autoapply/finish/start", json={"record_id": 6}).get_json()
    assert other["counted"] is True and other["left"] == 1
    _end(c)


def test_used_up_keeps_the_fast_autofill_with_one_line(client, monkeypatch):
    A, c = client
    _wire(A, monkeypatch, Broker(left=0, limit=3))
    r = c.post("/api/autoapply/finish/start", json={"record_id": 9}).get_json()
    assert r["ok"] is False and r["reason"] == "used_up"
    assert r["note"] == "The agent finish for this month is used up, so I filled what I could. It resets on December 1."
    assert "—" not in r["note"] and "–" not in r["note"]
    # nothing was opened for the agent
    assert c.post("/api/autoapply/agent/step", json={"record_id": 9}).status_code == 403


@pytest.mark.parametrize("bundled,status", [(False, 200), (True, 0), (True, 404)])
def test_own_key_or_a_broker_down_or_old_never_blocks(client, monkeypatch, bundled, status):
    A, c = client
    b = _wire(A, monkeypatch, Broker(left=0, status=status), bundled=bundled)
    r = c.post("/api/autoapply/finish/start", json={"record_id": 3}).get_json()
    assert r["ok"] is True and r["counted"] is False
    if not bundled:
        assert b.posts == []                                          # the person's own key: never counted
    _end(c)


def test_agent_routes_open_only_for_the_finish_run_and_never_the_queue(client, monkeypatch):
    A, c = client
    _wire(A, monkeypatch, Broker(left=3))
    assert A._apply_agent_enabled() is False                         # the launch: the agent is off
    # the owner's own saved choice is Auto: it must change nothing here
    A._AUTONOMOUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    A._apply_mode_file().write_text("auto", encoding="utf-8")
    assert A._apply_mode() == "assisted" and A._autonomous_on() is False
    assert c.post("/api/autoapply/agent/precheck", json={"record_id": 1}).status_code == 403
    c.post("/api/autoapply/finish/start", json={"record_id": 1})
    assert c.post("/api/autoapply/agent/precheck", json={"record_id": 1}).status_code != 403
    # Auto mode's queue stays shut, and no parked job is ever carried on by itself
    assert c.post("/api/autoapply/auto/plan", json={}).status_code == 403
    assert c.post("/api/autoapply/auto/release", json={}).status_code == 403
    assert c.get("/api/autoapply/agent/parked").get_json() == {"ok": True, "parked": [], "agent": False}
    # and Submit is the person's click on every site, the verified allowlist included
    d = c.get("/api/submit/decision?url=https://acme.recruitee.com/o/analyst").get_json()
    assert d["click_submit"] is False
    _end(c)
    assert c.post("/api/autoapply/agent/precheck", json={"record_id": 1}).status_code == 403


def test_the_finish_window_closes_by_itself(client, monkeypatch):
    A, c = client
    _wire(A, monkeypatch, Broker(left=3))
    c.post("/api/autoapply/finish/start", json={"record_id": 2})
    assert A._finish_active() is True
    A._FINISH["until"] = 0.0                                          # 20 minutes later
    assert A._finish_active() is False
    assert c.post("/api/autoapply/agent/precheck", json={"record_id": 2}).status_code == 403


def test_the_plan_reports_its_cost(client):
    A, c = client
    fields = [{"ref": "t1", "label": "What interests you about this role?", "type": "textarea", "required": True}]
    r = c.post("/api/autoapply/plan", json={"url": "https://job-boards.greenhouse.io/acme/jobs/1", "fields": fields},
               headers={"X-Tailor-Extension": "1"}).get_json()
    assert "cost" in r and isinstance(r["cost"], (int, float)) and r["cost"] >= 0


# ------------------------------------------------------------------ the drawn pointer
POINTER = str(ROOT / "extension" / "pointer_overlay.js")
AT = ("(async () => { const el = document.querySelectorAll('input')[0]; const ok = window.TailorPointer.at(el, 'Filling: Email Address');"
      " await new Promise((r) => setTimeout(r, 500)); const s = window.TailorPointer.state(); const b = el.getBoundingClientRect();"
      " const host = document.querySelector('[data-sj-pointer]'); const p = host.shadowRoot.querySelector('.p');"
      " return { ok, s, field: { left: b.left, right: b.right, top: b.top, bottom: b.bottom },"
      " still: p.classList.contains('still'), events: getComputedStyle(host).pointerEvents, label: host.shadowRoot.querySelector('.t').textContent }; })()")


@browser
def test_the_pointer_sits_beside_the_field_and_goes_away(tmp_path):
    r = run(tmp_path, "workday_prefilled.html", [
        {"do": "script", "file": POINTER},
        {"do": "eval", "as": "at", "js": AT},
        {"do": "eval", "as": "hidden", "js": "(() => { window.TailorPointer.hide(); return { host: !!document.querySelector('[data-sj-pointer]'), s: window.TailorPointer.state() }; })()"},
    ])
    at = r["at"]
    assert at["ok"] is True and at["label"] == "Filling: Email Address"
    s, f = at["s"], at["field"]
    # never on the field: right of it, or under it
    beside = s["x"] >= f["right"] or s["y"] >= f["bottom"]
    assert beside, (s, f)
    assert at["events"] == "none" and at["still"] is False          # a picture, gliding
    assert r["hidden"] == {"host": False, "s": None}


@browser
def test_the_pointer_jumps_under_reduced_motion(tmp_path):
    r = run(tmp_path, "workday_prefilled.html", [
        {"do": "media", "reduced": True},
        {"do": "script", "file": POINTER},
        {"do": "eval", "as": "at", "js": AT},
    ])
    assert r["at"]["still"] is True and r["at"]["s"]["reduced"] is True


@browser
def test_the_engine_names_each_field_it_fills_beside_the_pointer(tmp_path):
    import json as _json
    payload = {"fields": {"email": "apply.person@example.com", "city": "Springfield"}, "screening": {}, "eeo": {}}
    js = (f"(async () => {{ const seen = []; const at = window.TailorPointer.at; window.TailorPointer.at = (el, l) => {{ seen.push(l); return at(el, l); }};"
          f" await window.TailorAutofill.fillAsync({_json.dumps(payload)}, {{ pointer: true }}); return seen; }})()")
    off = (f"(async () => {{ const seen = []; window.TailorPointer.at = (el, l) => {{ seen.push(l); return true; }};"
           f" await window.TailorAutofill.fillAsync({_json.dumps(payload)}, {{}}); return seen; }})()")
    r = run(tmp_path, "workday_prefilled.html", [
        {"do": "inject"}, {"do": "script", "file": POINTER},
        {"do": "eval", "as": "on", "js": js},
        {"do": "eval", "as": "off", "js": off},
    ])
    assert any(x.startswith("Filling: Email Address") for x in r["on"]), r["on"]
    assert r["off"] == []                                              # only when the shell asks


@browser
def test_the_fast_fill_fills_each_box_as_the_pointer_arrives_within_the_time_cap(tmp_path):
    import json as _json
    payload = {"fields": {"email": "apply.person@example.com", "city": "Springfield", "zip": "60601",
                          "street": "12 Sample Street"}, "screening": {}, "eeo": {}}
    # empty the site's own values first, then fill with the pointer on
    js = (f"(async () => {{ for (const i of document.querySelectorAll('input')) i.value = '';"
          f" const seen = []; const at = window.TailorPointer.at;"
          f" window.TailorPointer.at = (el, l, o) => {{ seen.push({{ l, empty: !String(el.value || '').trim(), now: !!(o && o.now) }}); return at(el, l, o); }};"
          f" const t0 = Date.now(); await window.TailorAutofill.fillAsync({_json.dumps(payload)}, {{ pointer: true }});"
          f" const s = getComputedStyle(document.querySelector('[data-sj-pointer]').shadowRoot.querySelector('.t'));"
          f" return {{ seen, ms: Date.now() - t0, bg: s.backgroundColor, color: window.TailorPointer.COLOR }}; }})()")
    r = run(tmp_path, "workday_prefilled.html", [{"do": "inject"}, {"do": "script", "file": POINTER}, {"do": "eval", "as": "r", "js": js}])["r"]
    paced = [x for x in r["seen"] if x["now"]]
    assert len(paced) >= 3, r["seen"]
    assert all(x["empty"] for x in paced), paced              # the pointer arrives, then the box fills
    assert r["ms"] < 6500, r["ms"]                            # the whole pointer time is capped
    # ONE colour token: the label's background is it
    assert r["color"] == "#7957FC" and r["bg"] == "rgb(121, 87, 252)", r


# ------------------------------------------------------------------ the shell's rules (no Electron)
def test_autofill_finish_js():
    if not shutil.which("node"):
        pytest.skip("needs node")
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_autofill_finish.js")],
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout[-2000:] + out.stderr[-2000:]
