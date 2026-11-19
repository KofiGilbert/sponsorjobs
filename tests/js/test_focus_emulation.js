// What the shell tells the pane's page through the debugger (shell-electron/focus_emulation.js,
// acquireDebugger and the quiet-run hold in main.js): focused and visible always; 1280 x 900
// while a quiet run keeps the pane out of sight; its own size again when it is shown; and no
// fill on a page that still has no size. Whether the real page then behaves can only be
// confirmed in the real app. Run: node tests/js/test_focus_emulation.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const SHELL = path.resolve(__dirname, "..", "..", "shell-electron");
const focus = require(path.join(SHELL, "focus_emulation.js"));

// a fake debugger over a fake page: the size override is what the page then reports
function fakeDbg(page, refuse) {
  const sent = [];
  return { sent, sendCommand: async (m, p) => {
    sent.push([m, p]);
    if (refuse && refuse.includes(m)) throw new Error("refused");
    if (m === "Emulation.setFocusEmulationEnabled") { page.vis = "visible"; page.focus = true; }
    if (m === "Emulation.setDeviceMetricsOverride") { page.w = p.width; page.h = p.height; }
    if (m === "Emulation.clearDeviceMetricsOverride") { page.w = page.real; page.h = page.real; }
    if (m === "Runtime.evaluate") return { result: { value: JSON.stringify(page) } };
    return {};
  } };
}
const names = (d) => d.sent.map((x) => x[0]).filter((m) => m !== "Runtime.evaluate");

(async () => {
  // 1. taken in a quiet run: focus, then the size (1280 x 900, scale 1, not mobile); every time
  {
    const page = { w: 0, h: 0, vis: "hidden", focus: false, real: 0 };
    const d = fakeDbg(page), lines = [];
    const r = await focus.apply(d, { quiet: true, log: (l) => lines.push(l) });
    assert.deepStrictEqual(r, { focus: true, size: "set" });
    assert.deepStrictEqual(d.sent.slice(0, 2), [["Emulation.setFocusEmulationEnabled", { enabled: true }],
      ["Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false }]]);
    assert.deepStrictEqual([page.w, page.h, page.vis, page.focus], [1280, 900, "visible", true]);
    // one debug line, the first time: what the page reports after applying
    assert.strictEqual(lines.length, 1);
    assert.ok(/the page reports 1280x900, visible, focused/.test(lines[0]), lines[0]);
    await focus.apply(d, { quiet: true, log: (l) => lines.push(l) });                 // taken again: sent again
    assert.deepStrictEqual(names(d), ["Emulation.setFocusEmulationEnabled", "Emulation.setDeviceMetricsOverride",
                                      "Emulation.setFocusEmulationEnabled", "Emulation.setDeviceMetricsOverride"]);
    assert.strictEqual(lines.length, 1);
    // 2. shown to the person: the size override is cleared, once
    page.real = 900;
    assert.strictEqual(await focus.show(d), true);
    assert.strictEqual(names(d).pop(), "Emulation.clearDeviceMetricsOverride");
    assert.strictEqual(page.w, 900);
    assert.strictEqual(await focus.show(d), false);                                    // nothing to clear any more
  }
  // 3. taken while the pane is visible: focus only; a size left from a quiet run is cleared
  {
    const page = { w: 900, h: 700, vis: "hidden", focus: false, real: 900 };
    const d = fakeDbg(page);
    assert.deepStrictEqual(await focus.apply(d, { quiet: false }), { focus: true, size: "" });
    assert.deepStrictEqual(names(d), ["Emulation.setFocusEmulationEnabled"]);
    await focus.apply(d, { quiet: true });
    assert.deepStrictEqual(await focus.apply(d, { quiet: false }), { focus: true, size: "cleared" });
    assert.strictEqual(names(d).pop(), "Emulation.clearDeviceMetricsOverride");
  }
  // 4. a debugger that refuses: said so, never thrown
  {
    const d = fakeDbg({}, ["Emulation.setFocusEmulationEnabled", "Emulation.setDeviceMetricsOverride"]), lines = [];
    assert.deepStrictEqual(await focus.apply(d, { quiet: true, log: (l) => lines.push(l) }), { focus: false, size: "" });
    assert.ok(/refused/.test(lines[0]));
  }
  // 5. the guard before a fill: a page under 600 wide is sized and read again
  {
    const page = { w: 0, h: 0, vis: "hidden", focus: false, real: 0 };
    const d = fakeDbg(page), lines = [];
    const r = await focus.ensureViewport({ width: async () => page.w, dbg: d }, { log: (l) => lines.push(l) });
    assert.deepStrictEqual(r, { ok: true, width: 1280, applied: true });
    assert.ok(lines.some((l) => /after the size override the page reports 1280/.test(l)));
    // already a real size: nothing is sent
    const d2 = fakeDbg({ w: 1100 });
    assert.deepStrictEqual(await focus.ensureViewport({ width: async () => 1100, dbg: d2 }), { ok: true, width: 1100, applied: false });
    assert.strictEqual(d2.sent.length, 0);
    // the override does not take: not ok, so nothing may be filled
    const page3 = { w: 0, h: 0, real: 0 };
    const d3 = fakeDbg(page3, ["Emulation.setDeviceMetricsOverride"]);
    assert.deepStrictEqual(await focus.ensureViewport({ width: async () => page3.w, dbg: d3 }), { ok: false, width: 0, applied: true });
    // no debugger at all, or a page that does not answer
    assert.deepStrictEqual(await focus.ensureViewport({ width: async () => 0, dbg: null }), { ok: false, width: 0, applied: false });
    assert.strictEqual((await focus.ensureViewport({ width: async () => { throw new Error("gone"); }, dbg: null })).ok, false);
  }
  // 6. main.js: sent from acquireDebugger after attaching (the only attach, in main.js and agent.js);
  //    a quiet run holds the debugger; it is dropped (size cleared) when the pane is shown; the
  //    fill checks the viewport before anything else
  const main = fs.readFileSync(path.join(SHELL, "main.js"), "utf8");
  const agent = fs.readFileSync(path.join(SHELL, "agent.js"), "utf8");
  const part = (from, to) => main.slice(main.indexOf(from), main.indexOf(to, main.indexOf(from)));
  const acquire = part("function acquireDebugger(wc)", "// The \"application received\" marker");
  assert.ok(acquire.indexOf("dbg.attach(") > 0 && acquire.indexOf("focusEmulation.apply(dbg, { quiet: isQuietPane(wc)") > acquire.indexOf("dbg.attach("), "sent after attaching");
  assert.strictEqual(main.split(".attach(").length - 1, 1, "one attach in main.js, inside acquireDebugger");
  assert.strictEqual(agent.split("debugger.attach(").length - 1, 0, "agent.js attaches only through acquireDebugger");
  assert.ok(/if \(fromAgent\) holdQuietDebugger\(\);[\s\S]*?loadURL\(url\)/.test(part("function openInPane(url, fromAgent)", "function closePane()")), "held before the page loads");
  assert.ok(/dropQuietDebugger\(\);\s*\/\/[^\n]*\n\s*paneQuiet = false;/.test(part("function takePaneBack()", "ipcMain.on(\"pane:open\"")), "cleared when the person takes the pane");
  assert.ok(/\} else \{\s*dropQuietDebugger\(\);\s*paneQuiet = false;/.test(part("function setPaneQuiet(on)", "function paneState()")), "cleared when the quiet run ends");
  assert.ok(/wc\.on\("did-navigate", \(\) => \{[\s\S]*?focusEmulation\.apply\(/.test(part("function ensurePane()", "function openInPane(")), "sent again after a navigation");
  const fill = part("async function runAutoApplyOnPane(", "// ------------------------------------------------------------- multi-page forms");
  assert.ok(fill.indexOf("ensureQuietViewport(wc)") > 0 && fill.indexOf("ensureQuietViewport(wc)") < fill.indexOf("insertCSS(FILLED_CSS)"), "the viewport is checked before the fill");
  assert.ok(/if \(!vp\.ok\) return \{ ok: false, reason: "no-viewport"/.test(fill), "a page with no size is never filled");
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
