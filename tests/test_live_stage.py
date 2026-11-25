"""The live interview (Round 2) runs on the recorded round's stage.

What it promises, and where each promise is pinned:

* focus mode from the intro to "All done", by the same pure-CSS rule as Round 1;
* the intro is Round 1's card (one-line title, a stat row of the engine's real numbers, points that
  are true for a live call) with ONE primary action;
* with no live interview left the screen lists no plans and no prices: one line, one primary that
  opens the plans panel (the one place prices live), and a quiet "Not now";
* the device check is Round 1's, and the camera and microphone chosen there (or in Round 1) are
  the ones handed to the call;
* the call: a large interviewer tile, a small self view, the control bar (microphone, camera,
  settings, end) under the video, a speaking outline, and an end button that asks first;
* every call behaviour stays: the start call, the token, the timekeeper, the heartbeat metering,
  the drop handling, and joining without a camera.

The flow runs in a real Chromium with a fake camera and a stand-in for the video SDK
(tests/js/live_stage_harness.js): no real call is ever made. The source checks run everywhere.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "ui" / "static" / "app.js"
CSS = ROOT / "ui" / "static" / "styles.css"
LOCALES = ROOT / "ui" / "static" / "locales"
HARNESS = ROOT / "tests" / "js" / "live_stage_harness.js"


def _r2_src() -> str:
    src = APP_JS.read_text(encoding="utf-8")
    return src.split("Round 2: live interview (Tavus)", 1)[1].split("Report card (both rounds)", 1)[0]


def _fn(name: str) -> str:
    return _r2_src().split(f"function {name}(", 1)[1].split("\n  }\n", 1)[0]


def _en() -> dict:
    return json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))


def test_focus_mode_covers_the_live_round():
    css = CSS.read_text(encoding="utf-8")
    live = "#view-prep.is-active :is(#prepScreen, #prepLive):not([hidden]) .r1-stage"
    assert f".app:has({live}) > .sidebar {{ display: none; }}" in css
    assert f".main:has({live}) > .privacy-line {{ display: none; }}" in css
    assert ":is(#prepScreen, #prepLive):has(.r1-stage) > .mock-top { display: none; }" in css
    # Every Round 2 step draws the shared stage, and leaving empties it (the chrome comes back).
    for fn in ("renderR2Intro", "r2GoDevices", "renderCviStage", "endCvi", "renderCviNoStart"):
        assert 'r1Stage("' in _fn(fn), f"{fn} draws the shared stage"
    assert '$("#liveStage").innerHTML = ""' in _fn("quitLive")
    assert '$("#liveStage").innerHTML = r1Stage("intro", { call: true, body: "" })' in _fn("startRound2"), "no chrome flash while it loads"


def test_one_design_system_not_two():
    """No second set of call classes: the old green room and boxed call are gone."""
    src, css = APP_JS.read_text(encoding="utf-8"), CSS.read_text(encoding="utf-8")
    assert "zoom-" not in _r2_src() and ".zoom-" not in css
    for gone in ("renderR2GreenRoom", "r2GateHtml", "wireR2Gate", "fillR2Passes", "buyR2Pass", "buyInterviews",
                 "r2PassLabel", "LIVE_STREAM", "fitCviCall"):
        assert gone not in src, f"{gone} was replaced by the shared stage"
    # The device check is Round 1's card, with Join where Round 1 has its practice question.
    assert "r1CheckBody(" in _fn("r2GoDevices") and 'id="liveBegin"' in _fn("r2GoDevices")


def test_the_intro_has_one_primary_action_and_true_points():
    intro, act = _fn("renderR2Intro"), _fn("renderR2Act")
    assert 'r1Stage("intro", { solo: true' in intro
    assert '<h2 class="r1-h">${esc(prepRole(p))}</h2>' in intro and 'class="r1-co"' in intro, "role is the title, company its own line"
    assert intro.count("${point(") == 3
    en = _en()
    assert "Tavus" in en["r2.notice1"] and "video" in en["r2.notice2"], "the points say where the call goes"
    for key in ("r2.notice1", "r2.notice2"):
        assert "stay on this computer" not in en[key].lower().replace("stays", "stay").split(".")[0], f"{key} must not claim the call stays here"
        assert len(en[key].split()) <= 12
    # Each branch renders exactly one primary.
    for branch in act.split("else"):
        assert branch.count('primary("') <= 1
    assert act.count('primary("r2Begin"') == 1 and 'T("r1.getStarted"' not in intro


def test_the_paywall_lists_no_plans_and_no_prices():
    """Prices live in ONE place, the plans panel. This screen sends the person there."""
    act = _fn("renderR2Act")
    assert '$("#r2Plans")?.addEventListener("click", () => openUpgrade())' in act
    assert 'primary("r2Plans", "r2.seePasses", "See plans")' in act
    assert 'primary("r2Plans", "r2.buyMore", "Buy more interviews")' in act
    assert 'id="r2NotNow"' in act and '$("#r2NotNow")?.addEventListener("click", quitLive)' in act
    for banned in ("price_label", "passes", ".packs", "PASS_NAMES", "$"):
        assert banned not in act.replace("${", "").replace("$(", ""), f"the action area must not read {banned!r}"
    en = _en()
    assert en["r2.planLine"] == "Live interviews are part of every plan."
    assert "{date}" in en["r2.usedUpOn"] and "renews" in en["r2.usedUp"]
    for gone in ("r2.finishHere", "r2.passH", "r2.passD", "r2.passNIv"):
        assert gone not in en, f"{gone} belonged to the old pill row"


def test_the_stat_row_is_the_engines_numbers():
    import ui.app as app
    d = app.app.test_client().get("/api/interview/round2/config").get_json()
    assert d == {"minutes": app._cvi_timing()["minutes"], "questions_min": app.ROUND2_Q_MIN, "questions_max": app.ROUND2_Q_MAX}
    assert d["minutes"] == 20
    stats = _fn("r2Stats")
    assert "c.minutes" in stats and "c.questions_min" in stats and not re.search(r'"\d+ min', stats)
    # The briefing and the plan use the same numbers the intro states.
    brief = app._round2_briefing({"role": "PM", "questions": [{"q": f"Q{i}?"} for i in range(9)]}, {}, None)
    assert f"{app.ROUND2_Q_MIN} to {app.ROUND2_Q_MAX} questions" in brief
    assert len(app._round2_plan({"questions": [{"q": f"Q{i}?"} for i in range(9)]})) == app.ROUND2_Q_MAX


def test_the_call_controls_and_behaviour_are_kept():
    call = _fn("renderCviStage")
    for ctl in ('"cviMic"', '"cviCam"', 'id="cviGear"', 'id="cviEnd"'):
        assert ctl in call
    assert call.index('"cviMic"') < call.index('"cviCam"') < call.index('id="cviGear"') < call.index('id="cviEnd"')
    assert 'id="cviConfirm"' in call and 'role="alertdialog"' in call, "ending asks first"
    # Kept as it was: the call object, the token split, the waits, the timekeeper, the metering.
    for kept in ("window.Daily.createCallObject(opts)", "subscribeToTracksAutomatically: true", 'u.searchParams.get("t")',
                 'setTimeout(() => cviDropped("no interviewer"), 90000)', 'CVI_CALL.on("left-meeting"',
                 "if (CVI_TK) CVI_TK.onEvent(e && e.data)", "/api/interviews/cvi/heartbeat", "++CVI_UNBILLED >= 30",
                 "window.tailorShell.openBrowser(s.join_url)"):
        assert kept in call, kept
    assert "CVI_UNBILLED > 0" in _fn("endCvi") and "/api/interviews/cvi/end" in _fn("r2Finish")
    # The devices chosen on the check go to the call; no camera joins with the voice only.
    assert "opts.videoSource = pick.cam" in call and "opts.videoSource = false" in call and "opts.audioSource = pick.mic" in call
    assert 'pick.hasCam ? r2Ctrl("cviCam"' in call, "no dead camera control without a camera"
    assert "SCR_DEV_SEL.cam" in _fn("beginLive") and "stopScrStream()" in _fn("beginLive")
    assert "r1DevSave(kind, d)" in _fn("r2DevPick") and "setInputDevicesAsync" in _fn("r2DevPick")


def test_escape_never_ends_the_call():
    src = _r2_src()
    handler = src.split("It never\n  // ends the interview by itself.", 1)[1].split("\n  });\n", 1)[0]
    assert "r2Confirm(true)" in handler and "endCvi(" not in handler


def test_after_the_call_no_typing():
    end = _fn("endCvi") + _fn("r2Finish")
    assert 'r1Label(T("r1.allDone", "All done"))' in end and "r2.doneSub" in end
    assert "<textarea" not in _r2_src() and "transcript:" not in end.split("/api/interviews/cvi/end", 1)[1].split(")", 1)[0]


def test_new_strings_exist_in_every_language_without_dashes():
    keys = set(re.findall(r'T\("(r[12]\.[A-Za-z0-9]+)"', _r2_src()))
    keys |= set(re.findall(r'(?:primary|point)\("[A-Za-z0-9]*",?\s*"(r[12]\.[A-Za-z0-9]+)"', _r2_src()))
    keys |= set(re.findall(r'point\("\w+", "(r[12]\.[A-Za-z0-9]+)"', _r2_src()))
    assert {"r2.notice1", "r2.notice2", "r2.seePasses", "r2.buyMore", "r2.checkAgain"} <= keys
    for lang in ("en", "de", "es", "fr", "pt"):
        d = json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
        missing = sorted(k for k in keys if k not in d)
        assert not missing, f"{lang} is missing {missing}"
        for k in keys:
            assert "—" not in d[k] and "–" not in d[k], f"{lang} {k} has a dash"


def _playwright_dir() -> Path | None:
    for c in (os.environ.get("SJ_PLAYWRIGHT_DIR"),
              ROOT / "shell-electron" / "node_modules" / "playwright",
              Path.home() / "sponsorjobs" / "shell-electron" / "node_modules" / "playwright"):
        if c and (Path(c) / "package.json").exists():
            return Path(c)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
@pytest.mark.skipif(_playwright_dir() is None, reason="needs playwright (shell-electron devDependency)")
def test_the_live_stage_in_a_real_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    import importlib

    import ui.app as app
    importlib.reload(app)
    app.WORKDIR.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    threading.Thread(target=lambda: app.app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
                     daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            import urllib.request
            urllib.request.urlopen(base, timeout=1).read(1)
            break
        except Exception:
            time.sleep(0.1)
    args = ["node", str(HARNESS), base, str(_playwright_dir())]
    if os.environ.get("SJ_LIVE_SHOTS"):
        args += ["--shots", os.environ["SJ_LIVE_SHOTS"]]
    out = subprocess.run(args, capture_output=True, text=True, timeout=600)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr
