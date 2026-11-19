// Tell the pane's page it is focused and visible, through the debugger.
//
// Measured on a real form (2026-11-19): the pane an agent run keeps out of sight reports
// document.visibilityState "hidden" and hasFocus() false, and a real key press sent through the
// debugger (Input.dispatchKeyEvent) does not reach a focused text box there. After
// Emulation.setFocusEmulationEnabled {enabled: true} on that target the page reports "visible"
// and focused, and the same key press lands. So the command is sent every time the shell takes
// the debugger for the pane (acquireDebugger in main.js): right after attaching, and again on
// every later use, which also covers a navigation that moved the page to a new process. It is
// cheap and has no effect when already on. It imitates nothing a site could object to: it only
// lets a page that is open in the app behave as an open page.
//
// Pure (no Electron), so tests/js/test_focus_emulation.js runs it under node.
"use strict";

const _said = new WeakSet();          // debuggers whose first outcome was logged

// enable(dbg, log?) -> Promise<boolean>. Never throws; ``log`` gets one line per debugger, the
// first time (whether the page took it), and one more if it later starts failing.
async function enable(dbg, log) {
  let ok = false, why = "";
  try {
    await dbg.sendCommand("Emulation.setFocusEmulationEnabled", { enabled: true });
    ok = true;
  } catch (e) { why = String((e && e.message) || e); }
  if (log && dbg && typeof dbg === "object" && (!_said.has(dbg) || !ok)) {
    _said.add(dbg);
    log("focus emulation: " + (ok ? "on (the page is told it is focused and visible)" : "not taken: " + why));
  }
  return ok;
}

module.exports = { enable };
