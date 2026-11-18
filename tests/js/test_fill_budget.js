// The fill's time limits (shell-electron/fill_budget.js) and the pane guard (pane_guard.js),
// under node (tests/test_agent_engineer.py runs it). The agent's side of a slow or stalled fill
// and of a destroyed pane is in tests/js/test_agent_engineer.js; the page side (the engine's
// budget, heartbeat and memory) in tests/test_agent_view.js.
"use strict";
const fs = require("fs");
const path = require("path");
const SHELL = path.join(__dirname, "..", "..", "shell-electron");
const B = require(path.join(SHELL, "fill_budget.js"));
const G = require(path.join(SHELL, "pane_guard.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const never = () => new Promise(() => {});

(async () => {
  // ---- the caps ---------------------------------------------------------------------------
  const c = B.caps({});
  assert(c.engine === 30000 && c.extract === 15000 && c.plan === 30000 && c.ops === 40000 && c.overall === 120000 && c.stall === 15000,
         "the defaults " + JSON.stringify(c));
  assert(c.engine + c.extract + c.plan + c.ops <= c.overall, "the phases fit the overall cap");
  const o = B.caps({ SJ_AGENT_FILL_MS: "300" });                    // the tests' short override still works
  assert(o.overall === 300 && o.engine === 300 && o.ops === 300 && o.stall <= 300 && o.grace <= 30, "an override scales the phases " + JSON.stringify(o));
  assert(B.caps({ SJ_FILL_ENGINE_MS: "5000", SJ_AGENT_PLAN_MS: "9000" }).engine === 5000 && B.caps({ SJ_AGENT_PLAN_MS: "9000" }).plan === 9000, "per-phase overrides");
  // what a phase may use: its cap, or what is left of the overall cap; nothing when too little is left
  assert(B.room(c, "engine", 0) === 30000 && B.room(c, "ops", 100000) === 17000 && B.room(c, "ops", 118000) === 0, "room");

  // ---- runPhase ---------------------------------------------------------------------------
  // 1. the work finishes: its value, and the page side was told its budget (the cap less the grace)
  let told = 0;
  let r = await B.runPhase({ name: "engine", capMs: 5000, graceMs: 2000, stallMs: 1000, pollMs: 20,
                             start: async (b) => { told = b; await sleep(60); return { filled: 3 }; }, beat: async () => ({ at: Date.now(), done: 1 }) });
  assert(!r.timedOut && !r.stalled && r.value.filled === 3 && told === 3000, "finished " + JSON.stringify(r) + " told=" + told);
  // 2. a partial result the page returned by itself passes through as the value
  r = await B.runPhase({ name: "engine", capMs: 1200, graceMs: 500, stallMs: 300, pollMs: 20,
                         start: async (b) => { await sleep(b); return { filled: 2, timed_out: true }; }, beat: async () => ({ at: Date.now(), done: 2 }) });
  assert(!r.timedOut && r.value.timed_out === true && r.value.filled === 2, "the page's own partial result " + JSON.stringify(r));
  // 3. still moving (the heartbeat changes) but past the cap: cut, NOT stalled
  let n = 0, t0 = Date.now();
  r = await B.runPhase({ name: "ops", capMs: 300, stallMs: 150, pollMs: 20, start: never, beat: async () => ({ at: ++n, done: n }) });
  assert(r.timedOut && !r.stalled && r.value === null && r.done > 0, "cut at the cap while moving " + JSON.stringify(r));
  assert(Date.now() - t0 >= 280 && Date.now() - t0 < 1500, "at the cap, not before: " + (Date.now() - t0));
  // 4. a moving phase is NOT killed by the stall limit: it runs past it and finishes
  n = 0;
  r = await B.runPhase({ name: "ops", capMs: 2000, stallMs: 120, pollMs: 20, start: async () => { await sleep(500); return { filled: 9 }; },
                         beat: async () => ({ at: ++n, done: n }) });
  assert(!r.timedOut && r.value.filled === 9, "a slow but moving phase finishes " + JSON.stringify(r));
  // 5. the heartbeat stopped: stalled after the stall limit, long before the cap
  t0 = Date.now();
  r = await B.runPhase({ name: "engine", capMs: 5000, stallMs: 200, pollMs: 20, start: never, beat: async () => ({ at: 7, done: 4 }) });
  assert(r.timedOut && r.stalled && Date.now() - t0 < 1500, "stalled " + JSON.stringify(r) + " after " + (Date.now() - t0));
  // 6. the page does not answer the heartbeat at all (a hung renderer): stalled
  t0 = Date.now();
  r = await B.runPhase({ name: "engine", capMs: 5000, stallMs: 200, pollMs: 20, start: never, beat: never });
  assert(r.timedOut && r.stalled && Date.now() - t0 < 2000, "no heartbeat at all " + JSON.stringify(r));
  r = await B.runPhase({ name: "engine", capMs: 5000, stallMs: 200, pollMs: 20, start: never, beat: async () => { throw new Error("gone"); } });
  assert(r.timedOut && r.stalled, "a heartbeat that throws");
  // 7. no heartbeat to read (the plan, a model call): only the cap
  t0 = Date.now();
  r = await B.runPhase({ name: "plan", capMs: 200, stallMs: 50, pollMs: 20, start: never });
  assert(r.timedOut && !r.stalled && Date.now() - t0 >= 190, "the cap alone " + JSON.stringify(r));
  // 8. the work's own error is thrown as is
  let threw = "";
  try { await B.runPhase({ name: "x", capMs: 500, stallMs: 500, pollMs: 20, start: async () => { throw new Error("boom"); } }); } catch (e) { threw = e.message; }
  assert(threw === "boom", "the work's error");

  // ---- what the agent does with a report --------------------------------------------------
  assert(B.fillOutcome({ ok: true, filled: 3 }) === "ok" && B.fillOutcome(null) === "ok" && B.fillOutcome({ ok: false, reason: "no-fields" }) === "ok", "ok");
  assert(B.fillOutcome({ ok: true, filled: 3, timed_out: true, timed_out_phase: "filling the planned fields" }) === "carry", "out of time while moving: carry on");
  assert(B.fillOutcome({ ok: false, reason: "stalled", timed_out: true, stalled: true }) === "hang", "no heartbeat: a hang");
  const line = B.fillLogLine({ filled: 12, left: ["a", "b"], ms: { payload: 10, engine: 9000, extract: 800, plan: 2000, ops: 3000, leftover: 5 },
                               counts: { engine_filled: 8, ops_filled: 4, planned: 6, pending: 1, from_memory: 7, opens: 9 }, timed_out: true, timed_out_phase: "filling the planned fields" });
  assert(line === "filled=12 left=2 engine_filled=8 ops_filled=4 planned=6 pending=1 from_memory=7 opens=9 total_ms=14815 payload_ms=10 engine_ms=9000 extract_ms=800 plan_ms=2000 ops_ms=3000 leftover_ms=5 timed_out=filling the planned fields", line);
  assert(!/[a-z]@|"a"/.test(line), "counts and times only, never a label or a value");

  // ---- the pane guard ---------------------------------------------------------------------
  assert(G.mayReap({ hasPane: true, open: false, quiet: false, busy: false }), "a closed pane nobody uses is reaped");
  assert(!G.mayReap({ hasPane: true, open: false, quiet: true, busy: false }), "never a quiet run's pane (it is not 'open')");
  assert(!G.mayReap({ hasPane: true, open: false, quiet: false, busy: true }), "never while the agent runs (a run resumed on the same page)");
  assert(!G.mayReap({ hasPane: true, open: true, quiet: false, busy: false }) && !G.mayReap({ hasPane: false }), "open, or no pane");
  assert(G.paneGoneError(new TypeError("Object has been destroyed")) && G.paneGoneError(new Error("WebContents was destroyed")), "the destroyed errors");
  assert(!G.paneGoneError(new Error("fill timed out")) && !G.paneGoneError(null), "nothing else");

  // ---- main.js uses them (it needs Electron, so its wiring is checked in the source) --------
  const main = fs.readFileSync(path.join(SHELL, "main.js"), "utf8");
  const destroy = main.slice(main.indexOf("function destroyPane()"), main.indexOf("async function createWindow()"));
  assert(/paneGuard\.mayReap\(\{ hasPane: !!pane, open: paneOpen, quiet: paneQuiet, busy: paneInUse\(\) \}\)/.test(destroy), "destroyPane asks the guard");
  assert(/paneReap = setTimeout\(destroyPane, PANE_REAP_MS\)/.test(destroy), "a pane in use is asked about again later, not destroyed");
  assert(destroy.indexOf("paneGuard.mayReap") < destroy.indexOf("p.webContents.close()"), "the guard comes before the teardown");
  const quiet = main.slice(main.indexOf("function setPaneQuiet(on)"), main.indexOf("function paneState()"));
  assert(/if \(on\) \{[\s\S]*?clearTimeout\(paneReap\);[\s\S]*?paneQuiet = true;/.test(quiet), "a quiet run taking the pane calls the reaper off");
  const fill = main.slice(main.indexOf("async function runAutoApplyOnPane("), main.indexOf("// ------------------------------------------------------------- multi-page forms"));
  assert(/fillBudget\.runPhase\(/.test(fill) && /fillAsync\(\$\{JSON\.stringify\(payload\)\}, \$\{JSON\.stringify\(\{ budgetMs \}\)\}\)/.test(fill), "the engine is told its budget");
  assert(/applyOpsAsync\(\$\{JSON\.stringify\(list\)\}, \$\{JSON\.stringify\(\{ budgetMs \}\)\}\)/.test(fill), "the ops are told theirs");
  assert(/reason: "stalled", timed_out: true, stalled: true/.test(fill), "only a stall is reported as stalled");
  assert(/timed_out: !!cut, timed_out_phase: cut \? cut\.phase : ""/.test(fill), "a cut fill still returns its report");
  assert(/console\.log\("\[fill\] " \+ fillBudget\.fillLogLine\(/.test(fill), "one log line per fill");
  assert(!/40000/.test(fs.readFileSync(path.join(SHELL, "agent.js"), "utf8").split("\n").find((l) => /^const FILL_(OVERALL_)?MS/.test(l)) || ""), "no single 40 s stopwatch");
  console.log("ok");
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
