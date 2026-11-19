// The shell turns focus emulation on whenever it takes the pane's debugger (focus_emulation.js,
// acquireDebugger in main.js): real keys do not reach a page that is out of sight without it.
// Whether the page then takes real keys can only be confirmed in the real app.
// Run: node tests/js/test_focus_emulation.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const SHELL = path.resolve(__dirname, "..", "..", "shell-electron");
const focus = require(path.join(SHELL, "focus_emulation.js"));

(async () => {
  // 1. the command, with enabled: true; one debug line the first time, none after
  const sent = [];
  const dbg = { sendCommand: async (m, p) => { sent.push([m, p]); return {}; } };
  const lines = [];
  assert.strictEqual(await focus.enable(dbg, (l) => lines.push(l)), true);
  assert.strictEqual(await focus.enable(dbg, (l) => lines.push(l)), true);
  assert.deepStrictEqual(sent, [["Emulation.setFocusEmulationEnabled", { enabled: true }], ["Emulation.setFocusEmulationEnabled", { enabled: true }]]);
  assert.strictEqual(lines.length, 1);
  assert.ok(/focus emulation: on/.test(lines[0]));
  // 2. a debugger that refuses: false, said so, never thrown
  const bad = { sendCommand: async () => { throw new Error("not attached"); } };
  const lines2 = [];
  assert.strictEqual(await focus.enable(bad, (l) => lines2.push(l)), false);
  assert.ok(/not taken: not attached/.test(lines2[0]));
  assert.strictEqual(await focus.enable(bad), false);
  // 3. main.js sends it from acquireDebugger: after attaching, and on every later acquire; that is
  //    the only place the pane's debugger is attached, in main.js and in agent.js
  const main = fs.readFileSync(path.join(SHELL, "main.js"), "utf8");
  const agent = fs.readFileSync(path.join(SHELL, "agent.js"), "utf8");
  const acquire = main.slice(main.indexOf("function acquireDebugger(wc)"), main.indexOf("// The \"application received\" marker"));
  assert.ok(acquire.indexOf("dbg.attach(") > 0 && acquire.indexOf("focusEmulation.enable(dbg") > acquire.indexOf("dbg.attach("), "sent after attaching");
  assert.strictEqual(main.split(".attach(").length - 1, 1, "one attach in main.js, inside acquireDebugger");
  assert.strictEqual(agent.split("debugger.attach(").length - 1, 0, "agent.js attaches only through acquireDebugger");
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
