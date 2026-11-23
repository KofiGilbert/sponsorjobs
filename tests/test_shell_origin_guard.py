"""The desktop shell's origin checks (shell-electron/origin_guard.js, under node) and how
main.js uses them: exact origin comparison wherever the app's own address is checked, no child
windows, the IPC sender check on every handler, the session cookie set before the app loads, and
no debugging port in a release build unless asked for."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "shell-electron" / "main.js").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_origin_guard_under_node():
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "test_origin_guard.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "origin_guard ok" in out.stdout


def test_main_js_never_compares_the_app_address_by_prefix():
    assert not re.search(r"startsWith\(\s*SERVER", MAIN)
    assert not re.search(r"startsWith\(`\$\{SERVER", MAIN)


def test_main_js_never_allows_a_child_window():
    assert 'action: "allow"' not in MAIN and "action: 'allow'" not in MAIN


def test_main_js_guards_ipc_before_any_handler():
    guard = MAIN.index("originGuard.guardIpc(ipcMain, SERVER")
    first = min(MAIN.index("ipcMain.handle("), MAIN.index("ipcMain.on("))
    assert guard < first


def test_main_js_debugging_port_only_when_the_decision_says_so():
    assert "originGuard.debugPort({ isPackaged: app.isPackaged, env: process.env })" in MAIN
    lines = [ln for ln in MAIN.splitlines() if "remote-debugging-port" in ln and "appendSwitch" in ln]
    assert lines and all(ln.strip().startswith("if (CDP_PORT)") for ln in lines)


def test_main_js_sets_the_session_cookie_before_loading_the_app():
    body = MAIN[MAIN.index("async function createWindow()"):]
    body = body[:body.index("\n}\n")]
    assert body.index("cookies.set(originGuard.sessionCookie(") < body.index("await win.loadURL(SERVER")
    assert 'crypto.randomBytes(32).toString("hex")' in MAIN
    assert "TAILOR_SESSION_TOKEN: SESSION_TOKEN" in MAIN          # handed to the spawned engine


def test_main_js_sends_the_session_header_on_its_own_engine_calls():
    for call in re.findall(r"net\.fetch\(SERVER[^;]*", MAIN, flags=re.S):
        assert "SESSION_HEADER" in call, call[:160]


def test_main_js_never_passes_a_sign_in_token_from_the_pane():
    assert '"signin:return", { token' not in MAIN
    assert "decodeURIComponent(acc[1])" not in MAIN
