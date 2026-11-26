"""The Orb's pure JavaScript, run under node: where its view sits (shell-electron/orb_view.js),
and the bubble's copy of the Browser bar plus the ball's state (ui/static/orb_core.js)."""
import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def _node(script, *args):
    out = subprocess.run(["node", "-e", script, *map(str, args)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _rect(**kw):
    script = ("const {orbRect}=require(process.argv[1]);"
              "console.log(JSON.stringify(orbRect(JSON.parse(process.argv[2]))))")
    return _node(script, ROOT / "shell-electron" / "orb_view.js", json.dumps(kw))


WIN = {"width": 1893, "height": 1183}


@pytest.mark.parametrize("zoom", [0.76, 1.0])
def test_bottom_right_of_the_window_by_default(zoom):
    r = _rect(anchor=None, offset=None, content={"w": 72, "h": 72}, win=WIN, zoom=zoom)
    s = 72 * zoom
    assert r["side"] == "right" and r["vert"] == "up"
    assert abs(r["rect"]["x"] + r["rect"]["width"] - (WIN["width"] - 16 * zoom)) <= 1
    assert abs(r["rect"]["y"] + r["rect"]["height"] - (WIN["height"] - 16 * zoom)) <= 1
    assert abs(r["rect"]["width"] - s) <= 1


def test_docks_to_the_pane_and_grows_up_and_left_for_the_bubble():
    pane = {"x": 1000, "y": 90, "width": 850, "height": 1000}
    r = _rect(anchor=pane, offset=None, content={"w": 360, "h": 150}, win=WIN, zoom=1)
    assert r["rect"]["x"] + r["rect"]["width"] == 1850 - 16
    assert r["rect"]["y"] + r["rect"]["height"] == 1090 - 16
    assert r["rect"]["width"] == 360 and r["rect"]["height"] == 150


def test_dragged_to_the_top_left_the_bubble_opens_down_and_right():
    r = _rect(anchor=None, offset={"r": 1700, "b": 1050}, content={"w": 300, "h": 120}, win=WIN, zoom=1)
    assert r["side"] == "left" and r["vert"] == "down"
    assert r["rect"]["x"] >= 0 and r["rect"]["y"] >= 0


def test_never_leaves_the_window():
    r = _rect(anchor=None, offset={"r": -500, "b": -500}, content={"w": 72, "h": 72}, win=WIN, zoom=1)
    assert r["rect"]["x"] + r["rect"]["width"] <= WIN["width"]
    assert r["rect"]["y"] + r["rect"]["height"] <= WIN["height"]
    r = _rect(anchor=None, offset={"r": 99999, "b": 99999}, content={"w": 72, "h": 72}, win=WIN, zoom=1)
    assert r["rect"]["x"] >= 0 and r["rect"]["y"] >= 0


CORE = ROOT / "ui" / "static" / "orb_core.js"
# A tiny stand-in for the DOM: just what snapshot/resolve read.
FAKE = """
const C=require(process.argv[1]);
function t(v){return{nodeType:3,nodeValue:v}}
function e(tag,cls,kids,extra){const n=Object.assign({nodeType:1,tagName:tag.toUpperCase(),className:cls||'',
  childNodes:kids||[],attrs:{},getAttribute(k){return k in this.attrs?this.attrs[k]:null}},extra||{});return n}
const btn=e('button','btn btn-sm btn-primary',[t('Next')]);
const inp=e('input','fld bb-answer',[],{value:'typed'});inp.attrs.placeholder='Your answer';
const svg=e('svg','',[]);
const scr=e('script','',[t('alert(1)')]);
const bar=e('div','bb-fill',[e('span','bb-fill-text is-one',[t('1 field needs you. 1 of 3')]),svg,scr,btn,inp],{hidden:false});
bar.attrs.role='status';
const snap=C.snapshot(bar);
console.log(JSON.stringify({snap,
  hit:C.resolve(bar,[3])===btn, deep:C.resolve(bar,[0,0]).nodeValue, bad:C.resolve(bar,[9])===null,
  neg:C.resolve(bar,[-1])===null}));
"""


def test_snapshot_copies_only_plain_safe_nodes_and_keeps_paths():
    out = _node(FAKE, CORE)
    snap = out["snap"]
    assert snap["tag"] == "div" and snap["cls"] == "bb-fill" and "a" not in snap   # no role copied
    kids = snap["c"]
    assert kids[0]["c"][0] == {"t": "1 field needs you. 1 of 3"}
    assert kids[1] == {"t": ""} and kids[2] == {"t": ""}        # svg and script dropped, paths kept
    assert kids[3]["tag"] == "button" and kids[3]["c"][0]["t"] == "Next"
    assert kids[4] == {"tag": "input", "cls": "fld bb-answer", "a": {"placeholder": "Your answer"}, "value": "typed"}
    assert out["hit"] and out["deep"] == "1 field needs you. 1 of 3" and out["bad"] and out["neg"]


@pytest.mark.parametrize("bar,want", [
    ({"text": "Filling page 2 of 4"}, "working"),
    ({"text": "Page 2: filled 9", "spin": True}, "working"),
    ({"text": "1 field needs you. 1 of 3", "primary": True}, "needs"),
    ({"text": "Sign in to the site, then press Autofill"}, "needs"),
    ({"text": "Checking", "wall": True}, "needs"),
    ({"text": "Submitted. Marked as applied"}, "done"),
    ({"text": "Nothing new to fill here"}, "info"),
    ({"text": ""}, ""),
    ({"text": "x", "hidden": True}, ""),
])
def test_mood_reads_the_bar(bar, want):
    script = "const C=require(process.argv[1]);console.log(JSON.stringify(C.mood(JSON.parse(process.argv[2]))))"
    assert _node(script, CORE, json.dumps(bar)) == want


@pytest.mark.parametrize("s,want", [
    ({}, "idle"), ({"offline": True, "mood": "needs"}, "offline"), ({"celebrating": True, "thinking": True}, "celebrating"),
    ({"thinking": True, "mood": "needs"}, "thinking"), ({"mood": "needs", "attention": True}, "needs"),
    ({"mood": "working"}, "working"), ({"attention": True, "sleeping": True}, "attention"), ({"sleeping": True}, "sleeping"),
])
def test_one_state_most_important_first(s, want):
    script = "const C=require(process.argv[1]);console.log(JSON.stringify(C.state(JSON.parse(process.argv[2]))))"
    assert _node(script, CORE, json.dumps(s)) == want


def test_the_orb_page_builds_with_text_only_and_the_shell_guards_its_channels():
    page = (ROOT / "ui" / "static" / "orb.js").read_text()
    assert "innerHTML" not in page and "insertAdjacentHTML" not in page
    main = (ROOT / "shell-electron" / "main.js").read_text()
    for ch in ("orb:size", "orb:drag", "orb:drag-end", "orb:move", "orb:reset", "orb:event"):
        assert f'ipcMain.on("{ch}", (e' in main and "fromOrb(e)" in main
    assert 'ipcMain.on("orb:set", (e, payload) => { if (fromApp(e)' in main
    assert "if (orb) orb.raise();" in main            # above the pane, every time it is made
