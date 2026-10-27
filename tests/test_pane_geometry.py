"""The in-app browser pane sits exactly on its slot at ANY app zoom (2026-10-27: at 76% zoom it
landed too far right and too wide, cut off past the window edge). Runs the real panegeom.js."""
import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHELL = ROOT / "shell-electron"

SLOT = {"x": 573, "y": 89, "width": 1278, "height": 978}   # measured in the app, 2026-10-27


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
@pytest.mark.parametrize("zoom", [0.5, 0.67, 0.76, 0.9, 1.0, 1.1, 1.25, 1.5, 2.0])
def test_pane_rect_is_the_slot_scaled_by_the_app_zoom(zoom):
    script = ("const {paneRect}=require(process.argv[1]);"
              "console.log(JSON.stringify(paneRect(JSON.parse(process.argv[2]), +process.argv[3])))")
    out = subprocess.run(["node", "-e", script, str(SHELL / "panegeom.js"), json.dumps(SLOT), str(zoom)],
                         capture_output=True, text=True, check=True).stdout
    r = json.loads(out)
    for k in ("x", "y", "width", "height"):
        assert abs(r[k] - SLOT[k] * zoom) <= 0.5, (zoom, k, r)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_bad_input_never_throws_or_goes_negative():
    script = ("const {paneRect}=require(process.argv[1]);"
              "console.log(JSON.stringify([paneRect(null,1),paneRect({x:-5,y:1,width:2,height:3},0),"
              "paneRect({x:10,y:10,width:10,height:10},'nope')]))")
    out = json.loads(subprocess.run(["node", "-e", script, str(SHELL / "panegeom.js")],
                                    capture_output=True, text=True, check=True).stdout)
    assert out[0] == {"x": 0, "y": 0, "width": 0, "height": 0}
    assert out[1]["x"] == 0 and out[2] == {"x": 10, "y": 10, "width": 10, "height": 10}


def test_the_shell_uses_it_and_re_lays_out_on_every_zoom_change():
    main = (SHELL / "main.js").read_text()
    assert "pane.setBounds(paneRect(paneBounds, win.webContents.getZoomFactor()))" in main
    assert 'win.webContents.on("zoom-changed"' in main
    js = (ROOT / "ui/static/app.js").read_text()
    assert 'window.addEventListener("resize", () => requestAnimationFrame(sendBounds))' in js
