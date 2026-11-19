// The time limits of one form fill (runAutoApplyOnPane in main.js, the apply agent in agent.js).
//
// A fill has phases: the page-side engine ("filling the page"), the form read ("reading the
// form"), the plan (one model call) and the plan's ops ("filling the planned fields"). Until
// 2026-11-18 all of them ran under ONE 40 s stopwatch; long real forms took 30 to 45 s, so they
// landed on either side of it by chance, and a fill that ran over was thrown away whole: the
// agent reloaded the page, started from zero, ran over again, and parked after four tries.
//
// Now the limit is about progress:
//   * each phase has its own cap, and the page-side phases are told theirs (cap minus a grace),
//     so the engine stops starting new work and RETURNS what it filled (``timed_out``);
//   * while a page-side phase runs, the shell reads the engine's heartbeat (fillProgress: the
//     last time it started or finished something). A phase that is still moving is left alone
//     up to its cap; one that has been silent for STALL (the page stopped answering) is given up;
//   * a timeout keeps the finished work: the fill returns a normal report with ``timed_out`` and
//     the phase, and the agent carries on from the page as it stands. Only a stall is a hang
//     (the reload ladder), so slow fills that make progress never use up the ladder's tries.
//
// Pure (no Electron), so tests/js/test_fill_budget.js runs it under node.
"use strict";

const num = (v, d) => (Number(v) > 0 ? Number(v) : d);

// The caps, from the measurements of 2026-11-18 on real forms (engine 10 to 21 s, form read 10
// to 12 s, plan 2 to 15 s, ops 5 to 16 s): each about twice the slowest seen, and the overall cap
// a little over their sum, so a fill is only cut when something is really wrong.
// SJ_AGENT_FILL_MS still overrides the overall cap (a shorter one scales the phases down).
const DEFAULTS = { engine: 30000, extract: 15000, plan: 30000, ops: 40000, overall: 120000, stall: 15000,
                   grace: 2000, poll: 2000 };
function caps(env) {
  env = env || process.env || {};
  const c = {
    engine: num(env.SJ_FILL_ENGINE_MS, DEFAULTS.engine),
    extract: num(env.SJ_FILL_EXTRACT_MS, DEFAULTS.extract),
    plan: num(env.SJ_AGENT_PLAN_MS, DEFAULTS.plan),
    ops: num(env.SJ_FILL_OPS_MS, DEFAULTS.ops),
    overall: num(env.SJ_AGENT_FILL_MS, DEFAULTS.overall),
    stall: num(env.SJ_FILL_STALL_MS, DEFAULTS.stall),
    grace: DEFAULTS.grace, poll: DEFAULTS.poll,
  };
  for (const k of ["engine", "extract", "plan", "ops"]) c[k] = Math.min(c[k], c.overall);
  c.stall = Math.min(c.stall, c.overall);
  c.grace = Math.min(c.grace, Math.floor(c.overall / 10));
  c.poll = Math.min(c.poll, Math.max(50, Math.floor(c.stall / 4)));
  return c;
}

// What a phase may still use: its own cap, or what is left of the overall cap (less a reserve
// for reading what is left on the page). 0 = no time for it: the phase is skipped.
function room(c, phase, spentMs, reserveMs) {
  const left = c.overall - spentMs - (reserveMs == null ? 3000 : reserveMs);
  const ms = Math.min(c[phase], left);
  return ms >= 1000 ? ms : 0;
}

function withTimeout(p, ms) {
  let t = null;
  const cap = new Promise((_r, rej) => { t = setTimeout(() => rej(new Error("timed out")), ms); });
  return Promise.race([Promise.resolve(p), cap]).finally(() => clearTimeout(t));
}

// Run one phase.
//   start(budgetMs) -> Promise: the work. ``budgetMs`` is what the page side is told (the cap
//     less the grace), so it normally returns by itself, with its partial result.
//   beat() -> Promise<{at, done} | null>: the page's heartbeat (optional: the plan has none).
// Resolves {name, value, timedOut, stalled, ms, done}:
//   * the work finished: its value (which may itself say ``timed_out``);
//   * the cap passed while the heartbeat kept moving: timedOut, value null;
//   * the heartbeat did not move (or could not be read) for stallMs: timedOut AND stalled.
// The work's own rejection is thrown as is.
async function runPhase(o) {
  const t0 = Date.now();
  const capMs = Math.max(1, o.capMs), stallMs = Math.max(1, o.stallMs || DEFAULTS.stall);
  const pollMs = Math.max(10, o.pollMs || DEFAULTS.poll);
  const graceMs = o.graceMs == null ? DEFAULTS.grace : o.graceMs;
  let alive = true, timer = null, lastKey = "", lastMove = t0, done = 0;
  const work = Promise.resolve()
    .then(() => o.start(Math.max(500, capMs - graceMs)))
    .then((value) => ({ value }), (error) => ({ error }));
  const watch = new Promise((resolve) => {
    const loop = async () => {
      if (!alive) return;
      if (Date.now() - t0 >= capMs) return resolve({ timedOut: true, stalled: false });
      if (o.beat) {
        let b = null;
        try { b = await withTimeout(o.beat(), Math.min(3000, stallMs)); } catch (e) { b = null; }
        if (!alive) return;
        if (b && typeof b === "object") {
          const key = String(b.at) + ":" + String(b.done);
          if (key !== lastKey) { lastKey = key; lastMove = Date.now(); }
          if (Number(b.done) >= 0) done = Number(b.done);
        }
        if (Date.now() - lastMove >= stallMs) return resolve({ timedOut: true, stalled: true });
        if (Date.now() - t0 >= capMs) return resolve({ timedOut: true, stalled: false });
      }
      timer = setTimeout(loop, Math.min(pollMs, Math.max(10, capMs - (Date.now() - t0))));
    };
    timer = setTimeout(loop, Math.min(pollMs, capMs));
  });
  const r = await Promise.race([work, watch]);
  alive = false;
  clearTimeout(timer);
  const ms = Date.now() - t0;
  if (r.timedOut) return { name: o.name, value: null, timedOut: true, stalled: !!r.stalled, ms, done };
  if (r.error) throw r.error;
  return { name: o.name, value: r.value, timedOut: false, stalled: false, ms, done };
}

// What the agent does with a fill report:
//   "hang"  the page stopped answering during the fill (no heartbeat): the reload ladder;
//   "carry" the fill ran out of time but was moving: keep what it filled and go on from the
//           page as it stands (never a reload, never one of the ladder's tries);
//   "ok"    anything else.
function fillOutcome(rep) {
  if (rep && rep.stalled) return "hang";
  if (rep && rep.timed_out) return "carry";
  return "ok";
}

// One log line per fill: the phase timings and the counts (never a value, never a label).
function fillLogLine(rep) {
  const r = rep || {};
  const c = r.counts || {};
  const ms = r.ms || {};
  const total = Object.values(ms).reduce((a, b) => a + (Number(b) || 0), 0);
  const parts = ["filled=" + (r.filled || 0), "left=" + ((r.left || []).length),
                 "engine_filled=" + (c.engine_filled || 0), "ops_filled=" + (c.ops_filled || 0),
                 "planned=" + (c.planned || 0), "pending=" + (c.pending || 0),
                 "from_memory=" + (c.from_memory || 0), "opens=" + (c.opens || 0),
                 "total_ms=" + total];
  if (c.left_open) parts.push("left_open=" + c.left_open);      // popups of ours that would not close
  if (c.keys_cut) parts.push("keys_cut=1");                    // the date typing did not finish
  if (c.search_cut) parts.push("search_cut=1");                // a list search did not finish (its field is in ``left``)
  for (const k of ["payload", "engine", "keys", "search", "extract", "plan", "ops", "leftover"]) if (ms[k] != null) parts.push(k + "_ms=" + ms[k]);
  if (r.timed_out) parts.push("timed_out=" + (r.timed_out_phase || "yes") + (r.stalled ? " stalled" : ""));
  return parts.join(" ");
}

module.exports = { DEFAULTS, caps, room, runPhase, withTimeout, fillOutcome, fillLogLine };
