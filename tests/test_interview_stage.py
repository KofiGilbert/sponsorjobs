"""The recorded interview runs on one full-window stage.

What the stage promises, and where each promise is pinned:

* focus mode: the app's own chrome (side menu, page title, footer line) is hidden only while a
  session runs, and Escape never ends a recording;
* the question is the largest text, set on balanced lines, stepped down when long, never cut;
* the recording status and the clock each keep one fixed place;
* a person can choose the camera and the microphone, the choice is remembered, and an unplugged
  device falls back to the default;
* every text earns its place (no duplicated practice label, a good connection says nothing,
  no emoji icons).

The rules that need no page run in Node (tests/js/test_interview_stage.js). The layout and the
flow run in a real Chromium with a fake camera and microphone
(tests/js/interview_stage_harness.js), skipped where Playwright is not installed. The source
checks below run everywhere.
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
STAGE_JS = ROOT / "ui" / "static" / "interview_stage.js"
CSS = ROOT / "ui" / "static" / "styles.css"
INDEX = ROOT / "ui" / "templates" / "index.html"
LOCALES = ROOT / "ui" / "static" / "locales"
MAIN_JS = ROOT / "shell-electron" / "main.js"
UNIT = ROOT / "tests" / "js" / "test_interview_stage.js"
HARNESS = ROOT / "tests" / "js" / "interview_stage_harness.js"


def _stage_src() -> str:
    """The Round 1 section of app.js."""
    src = APP_JS.read_text(encoding="utf-8")
    return src.split("Round 1: one-way recorded interview", 1)[1].split("Round 2: live interview (Tavus)", 1)[0]


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


# ------------------------------------------------------------------ source-level (always runs)

def test_the_stage_script_loads_before_the_app():
    html = INDEX.read_text(encoding="utf-8")
    assert "filename='interview_stage.js'" in html
    assert html.index("filename='interview_stage.js'") < html.index("filename='app.js'")


def test_focus_mode_is_tied_to_a_live_stage_in_the_open_interview_view():
    """Pure CSS: the chrome is hidden only while a .r1-stage sits in the visible Interview view,
    so it cannot be left hidden by a flag nobody cleared."""
    css = CSS.read_text(encoding="utf-8")
    live = "#view-prep.is-active #prepScreen:not([hidden]) .r1-stage"
    assert f".app:has({live}) > .sidebar {{ display: none; }}" in css
    assert f".main:has({live}) > .privacy-line {{ display: none; }}" in css
    assert "#view-prep:has(#prepScreen:not([hidden]) .r1-stage) > .page-head" in css
    # Leaving empties the stage, which is what brings the chrome back.
    quit_body = _stage_src().split("function quitScreen()", 1)[1].split("\n  }\n", 1)[0]
    assert '$("#scrStage").innerHTML = ""' in quit_body


def test_escape_asks_and_never_ends_a_recording():
    src = _stage_src()
    esc_handler = src.split("It never ends a recording by itself.", 1)[1].split("\n  });\n", 1)[0]
    assert "r1LeaveAsk(true)" in esc_handler
    for forbidden in ("quitScreen(", "stopRecording(", ".stop()"):
        assert forbidden not in esc_handler, f"Escape must not call {forbidden}"


def test_devices_cannot_be_switched_while_recording():
    src = _stage_src()
    switch = src.split("async function r1DevSwitch", 1)[1].split("\n  }\n", 1)[0]
    assert switch.lstrip().split("\n", 2)[1].strip().startswith("if (r1Busy()"), "switching is refused first thing while busy"
    assert 'SCR_PHASE === "countdown" || SCR_PHASE === "recording"' in src
    assert "gear.disabled = busy" in src


def test_the_choice_is_remembered_and_storage_failures_are_harmless():
    src = _stage_src()
    for line in (l for l in src.splitlines() if "localStorage" in l):
        assert "try {" in line and "catch" in line, f"unguarded storage access: {line.strip()}"
    assert '"sj.r1." + kind' in src
    assert 'addEventListener("devicechange", r1DevChanged)' in src
    assert 'removeEventListener("devicechange", r1DevChanged)' in src


def test_every_text_earns_its_place():
    src = _stage_src()
    stage = src.split("// ---- The stage (screens 3 to 6)", 1)[1].split("function uploadTake", 1)[0]
    assert "&#127908;" not in src and "\U0001F3A4" not in src, "no emoji icons: the microphone is an inline SVG"
    for gone in ("r1.practiceTag", "r1.tip3", "r1.devicesSub", "r1.connOk", "r1.getReady", "r1.answerHelp", "r1.retakesNote"):
        assert gone not in src, f"{gone} was cut and must not come back"
    assert stage.count("r1.practiceLabel") == 1, "practice is said once"
    assert len(re.findall(r'T\("r1\.tip\d"', stage)) == 2, "at most two short tips on the device check"
    # A good connection is silent: the only connection string is the offline one.
    conn = stage.split("function r1ConnPaint()", 1)[1].split("\n  }\n", 1)[0]
    assert "navigator.onLine === false ?" in conn and conn.rstrip().endswith(': "";')
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    assert en["r1.notRecording"] == "Not recording" and en["r1.startRecording"] == "Start recording"   # sentence case
    assert en["r1.practiceLabel"] == "Practice · not saved"
    assert en["r1.micSilent"] == "No sound from this microphone. Pick another one or check it is not muted."
    for key in ("r1.tip1", "r1.tip2", "r1.qHelp", "r1.practiceDone", "r1.doneSub"):
        assert len(en[key].split()) <= 9, f"{key} is one short line: {en[key]!r}"


def test_the_question_size_rule_lives_in_code_and_css():
    src = _stage_src()
    assert 'data-size="${SJStage.questionTier(text)}"' in src
    css = CSS.read_text(encoding="utf-8")
    sizes = {}
    for tier in ("l", "m", "s", "xs"):
        m = re.search(r'\.r1-q\[data-size="%s"\] \{ font-size: clamp\((\d+)px, [\d.]+cqi, (\d+)px\);[^}]*max-inline-size: (\d+)ch' % tier, css)
        assert m, f"no size rule for tier {tier}"
        sizes[tier] = tuple(int(x) for x in m.groups())
    order = [sizes[t] for t in ("l", "m", "s", "xs")]
    assert all(a[0] > b[0] and a[1] > b[1] for a, b in zip(order, order[1:])), "each tier is smaller than the one before"
    assert all(a[2] <= b[2] for a, b in zip(order, order[1:])), "smaller text may run longer lines, never shorter"
    rule = css.split(".r1-q {", 1)[1].split("}", 1)[0]
    assert "text-wrap: balance" in rule and "font-weight: 600" in rule
    assert "text-overflow" not in rule and "line-clamp" not in rule, "a question is never truncated"


def test_the_status_and_the_clock_have_one_place_each():
    stage = _stage_src().split("function r1Stage(step, o)", 1)[1].split("\n  }\n", 1)[0]
    assert stage.count('id="r1Status"') == 1 and stage.count('id="r1Clock"') == 1
    assert stage.index('id="r1Exit"') < stage.index('id="r1Clock"') < stage.index('id="r1Status"'), "Exit, then the clock, then the status"
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.r1-status \{[^}]*min-width: \d+px", css), "the pill keeps its width when the word changes"
    assert re.search(r"\.r1-clock \{[^}]*tabular-nums", css)
    # No second clock on the video any more.
    assert "r1-answer-clock" not in css and "r1Answer" not in _stage_src()


def test_media_permission_is_still_limited_to_the_apps_own_origin():
    """Device menus need no new permission: the hardening stays exactly as it was."""
    main = MAIN_JS.read_text(encoding="utf-8")
    assert 'const allowMedia = (host) => host === "127.0.0.1" || host === "localhost"' in main
    assert "(daily\\.co|tavus\\.io|tavusapi\\.com)$" in main
    assert "callback(MEDIA.includes(permission) && allowMedia(" in main
    assert "setDevicePermissionHandler" not in main and "selectDevice" not in STAGE_JS.read_text(encoding="utf-8")


def test_new_strings_exist_in_every_language():
    keys = sorted(set(re.findall(r'T\("(r1\.[A-Za-z0-9]+)"', _stage_src())))
    for lang in ("en", "de", "es", "fr", "pt"):
        d = json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
        missing = [k for k in keys if k not in d]
        assert not missing, f"{lang} is missing {missing}"


# ------------------------------------------------------------------ in Node

@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_device_menu_and_question_rules():
    out = subprocess.run(["node", str(UNIT)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr


# ------------------------------------------------------------------ in a real browser

@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
@pytest.mark.skipif(_playwright_dir() is None,
                    reason="needs playwright (shell-electron devDependency)")
def test_the_stage_in_a_real_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_AGENT_DEV", "1")
    monkeypatch.setenv("RESUME_AGENT_FAKE_LLM", "1")
    monkeypatch.setenv("RESUME_AGENT_DATA_DIR", str(tmp_path / "data"))
    import importlib

    import ui.app as app
    importlib.reload(app)
    app.WORKDIR.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    server = threading.Thread(
        target=lambda: app.app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True)
    server.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):                       # wait for the port to answer
        try:
            import urllib.request
            urllib.request.urlopen(base, timeout=1).read(1)
            break
        except Exception:
            time.sleep(0.1)

    out = subprocess.run(["node", str(HARNESS), base, str(_playwright_dir())],
                         capture_output=True, text=True, timeout=420)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout + out.stderr
