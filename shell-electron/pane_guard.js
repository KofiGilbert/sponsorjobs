// When the closed browser pane may be torn down (main.js destroyPane), and when an error means
// the pane is gone (agent.js). Pure, so tests/js/test_pane_guard.js runs it under node.
//
// A closed pane is destroyed a minute later to free its renderer (main.js PANE_REAP_MS). That
// reaper only looked at ``paneOpen``. A pane the apply agent is using is not always "open": a
// quiet (Auto) run tells the page it is closed, a run resumed on the same page never re-opens
// it, and the page's own Close leaves a running agent on a hidden pane. In each case a reaper
// armed by an earlier close destroyed the pane under the run, which then died on
// "TypeError: Object has been destroyed" (2026-11-18, two jobs of a quiet batch).
"use strict";

// {hasPane, open, quiet, busy}: destroy only a pane nobody is using.
function mayReap(s) {
  s = s || {};
  return !!s.hasPane && !s.open && !s.quiet && !s.busy;
}

// An error thrown by Electron for a call on a destroyed webContents / view / window.
function paneGoneError(e) {
  return /object has been destroyed|webcontents (was|has been|is) destroyed|render frame was disposed/i
    .test(String((e && e.message) || e || ""));
}

module.exports = { mayReap, paneGoneError };
