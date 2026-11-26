"""Autofill: forms inside frames follow the same visible walk (2026-11-26).

iCIMS (and similar) draw the application inside an iframe. The guided walk (top to bottom, in sight,
with the violet pointer, the glow ring, the needs-you Enter flow and the submit guard) reached only
the top page, so a frame's form was filled by the engine's frame pass out of sight. Now a frame's
fields join the same walk: same-origin frames through the top page's own engine, frames another
origin serves through their own engine (CDP isolated world; a frame in its own process through its
own target), with the ONE pointer drawn on the top page where the person sees the field. The walk
itself runs in a real Chromium with site isolation: tests/js/test_frame_walk.js (below).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "shell-electron" / "main.js"
CORE = ROOT / "extension" / "autofill_core.js"

try:
    from tests.test_form_widgets import _playwright_dir
except ImportError:                                   # run from the tests folder
    from test_form_widgets import _playwright_dir  # type: ignore

browser = pytest.mark.skipif(not shutil.which("node") or _playwright_dir() is None,
                             reason="needs node + playwright (shell-electron devDependency)")


@browser
def test_the_walk_inside_cross_origin_and_same_origin_frames_in_a_real_page():
    env = dict(os.environ, SJ_PLAYWRIGHT_DIR=str(_playwright_dir()))
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_frame_walk.js")], capture_output=True,
                         text=True, timeout=300, env=env)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stdout[-2000:] + out.stderr[-2000:]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def test_a_walked_page_fills_nothing_in_its_frames_out_of_sight():
    main = MAIN.read_text(encoding="utf-8")
    run = _between(main, "async function runAutoApplyOnPane", "// ------------------------------------------------------------- multi-page forms")
    # the frames are opened and prepared BEFORE the walk, and handed to it
    walk_at = run.index("walked = await guidedWalk(")
    assert run.index("fr = await openFrameSession(wc);") < walk_at
    assert run.index("walkFrames = await prepareWalkFrames(fr);") < walk_at
    assert "walkFrames, fr)" in run[walk_at:walk_at + 260]
    # after a walk: no engine pass, no form read, no plan in the frames (the out-of-sight pass)
    assert "if (havePayload && !walked) {" in run
    assert "if (!walked) for (const ctx of liveFrames) fields = fields.concat(await readForm(" in run
    # the frames' required empties are outlined and their Submit guarded
    assert "for (const ctx of liveFrames) await fr.eval(ctx, outline);" in run
    assert 'for (const ctx of liveFrames) await fr.eval(ctx, "window.TailorAutofill.armSubmitGuard()");' in run
    # the person's turn keeps the frames (Enter flow, pointer on Submit), let go on Done
    assert "holdGuideFrames(wc, fr, held, { url, recordId })" in run
    step = _between(main, 'ipcMain.handle("assist:guide-step"', "\n});")
    assert "c.js(`window.TailorAutofill.needsStep(${d})`" in step
    assert "guided.bringIntoSight(top, c, r.ref)" in step      # Next / Previous scroll the top page too


def test_frames_only_report_signals_to_the_one_sent_detector():
    """A frame's Submit, its confirmation and its form coming back empty are SIGNALS for
    shell-electron/sent_detect.js; the frame code never announces a submission itself."""
    main = MAIN.read_text(encoding="utf-8")
    held = _between(main, "function holdGuideFrames(", "function releaseGuideFrames()")
    assert "announceSubmitted" not in held and "/api/autofill/applied" not in held
    assert "frameSignals.createFrameSignalReader" in held
    sig = (ROOT / "shell-electron" / "frame_signals.js").read_text(encoding="utf-8")
    assert 'require("./sent_detect").signal' in sig and "announceSubmitted" not in sig
    for kind in ('kind: "submit-click"', 'kind: "page", how: "marker"', 'kind: "form-reset"'):
        assert kind in sig


def test_a_frame_never_draws_its_own_pointer_and_never_submits():
    core = CORE.read_text(encoding="utf-8")
    relay = _between(core, "function relayPointer()", "function relayTake(")
    assert "P.at = " in relay and "P.point = " in relay and "P.hide = " in relay   # kept, not drawn
    guard = _between(core, "function armSubmitGuard()", "function guardDoc(")
    assert "searchRoots()" in guard and "nodeType === 9" in guard                  # every same-origin document
    frames_js = (ROOT / "shell-electron" / "frames.js").read_text(encoding="utf-8")
    assert "captcha" in frames_js.lower()                                         # captcha frames are never entered
    guided = (ROOT / "shell-electron" / "guided.js").read_text(encoding="utf-8")
    assert "click()" not in guided and "requestSubmit" not in guided
