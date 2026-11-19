// What the pane's page is told about itself through the debugger: that it is focused and
// visible, and, while an agent run keeps the pane out of sight, that it has a real size.
//
// Measured on a real form (2026-11-19), with the pane of a quiet (Auto) run parked off the
// window:
//   * the page reports document.visibilityState "hidden" and hasFocus() false, and a real key
//     press through the debugger (Input.dispatchKeyEvent) does not reach a focused text box.
//     After Emulation.setFocusEmulationEnabled {enabled: true} it reports "visible", focused,
//     and the same key press lands;
//   * the page reports innerWidth 0 and innerHeight 0. A page with no size lays out as the
//     smallest phone: Workday swapped its prompt lists for a full-page picker (a title, Search,
//     "Options Expanded", Done) that the agent could neither read nor close, screenshots failed,
//     and every "is it on screen" check misfired. After Emulation.setDeviceMetricsOverride
//     {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false} it reports 1280 x 900 at
//     once, still out of sight.
// Both last only while the debugger stays attached, so main.js keeps it attached for the whole
// quiet run and sends both again every time the debugger is taken and after every navigation.
// When the pane is shown to the person the size override is cleared (the visible pane lays out
// to its own size). Nothing here hides anything from a site: it lets a page that is open in the
// app behave as an open page of an ordinary size.
//
// Pure (no Electron), so tests/js/test_focus_emulation.js runs it under node.
"use strict";

const QUIET_SIZE = { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false };
const MIN_WIDTH = 600;                 // below this a page is not laid out as a desktop page

const _said = new WeakSet();           // debuggers whose first outcome was logged
const _sized = new WeakSet();          // debuggers that carry the size override now

async function send(dbg, method, params) {
  try { await dbg.sendCommand(method, params || {}); return ""; }
  catch (e) { return String((e && e.message) || e) || "failed"; }
}

// What the page says about itself now: {w, h, vis, focus} or null.
async function probe(dbg) {
  try {
    const r = await dbg.sendCommand("Runtime.evaluate", { returnByValue: true,
      expression: "JSON.stringify({w: window.innerWidth, h: window.innerHeight, vis: document.visibilityState, focus: document.hasFocus()})" });
    return JSON.parse(r && r.result && r.result.value);
  } catch (e) { return null; }
}

// apply(dbg, {quiet, log}) -> Promise<{focus, size}>. Never throws.
//   focus emulation: always.  size: set while ``quiet`` (the pane is out of sight), cleared when
//   it is not and this debugger still carries it.
// ``log``: one line per debugger the first time (what the page reports after applying), and a
// line whenever a command is refused.
async function apply(dbg, o) {
  const quiet = !!(o && o.quiet), log = o && o.log;
  const out = { focus: false, size: "" };
  const f = await send(dbg, "Emulation.setFocusEmulationEnabled", { enabled: true });
  out.focus = !f;
  let s = "";
  if (quiet) {
    s = await send(dbg, "Emulation.setDeviceMetricsOverride", QUIET_SIZE);
    if (!s) { _sized.add(dbg); out.size = "set"; }
  } else if (_sized.has(dbg)) {
    s = await send(dbg, "Emulation.clearDeviceMetricsOverride");
    if (!s) { _sized.delete(dbg); out.size = "cleared"; }
  }
  if (log && (f || s)) log("page emulation: refused (" + (f ? "focus: " + f : "") + (f && s ? "; " : "") + (s ? "size: " + s : "") + ")");
  if (log && !_said.has(dbg) && !f) {
    _said.add(dbg);
    const p = await probe(dbg);
    log("page emulation: focus on" + (quiet ? ", size " + QUIET_SIZE.width + "x" + QUIET_SIZE.height : "")
        + (p ? "; the page reports " + p.w + "x" + p.h + ", " + p.vis + (p.focus ? ", focused" : ", not focused") : "; the page did not say"));
  }
  return out;
}

// The pane is shown to the person: the visible pane lays out to its own size again.
async function show(dbg) {
  if (!_sized.has(dbg)) return false;
  const s = await send(dbg, "Emulation.clearDeviceMetricsOverride");
  if (!s) _sized.delete(dbg);
  return !s;
}

// Before a fill or a step of a quiet run: a page narrower than MIN_WIDTH (0 x 0 for a parked
// pane) is given its size and read again. {ok, width, applied}: ok false = still no real size,
// and nothing may be filled on it.
//   driver: {width() -> Promise<number>, dbg}  (width: the page's innerWidth)
async function ensureViewport(driver, o) {
  const log = o && o.log;
  const read = async () => { try { return Number(await driver.width()) || 0; } catch (e) { return 0; } };
  let width = await read();
  if (width >= MIN_WIDTH) return { ok: true, width, applied: false };
  if (!driver.dbg) { if (log) log("viewport: " + width + " wide and no debugger to size it"); return { ok: false, width, applied: false }; }
  await apply(driver.dbg, { quiet: true, log });
  width = await read();
  if (log) log("viewport: was under " + MIN_WIDTH + " wide; after the size override the page reports " + width);
  return { ok: width >= MIN_WIDTH, width, applied: true };
}

// kept for callers of the first version
const enable = (dbg, log) => apply(dbg, { quiet: false, log }).then((r) => r.focus);

module.exports = { apply, show, ensureViewport, enable, probe, QUIET_SIZE, MIN_WIDTH };
