// The two things a form fill needs REAL input for (shell-electron/main.js runs them after the
// page-side engine's pass): typing date segments, and searching lists that filter only on a real
// Enter. Both talk to the page through a small driver, so they run the same under Electron
// (webContents + its debugger) and under the tests (tests/js/test_fill_keys.js with a fake
// driver, tests/js/forms_harness.js with a headless browser):
//
//   driver.evalJs(expr) -> Promise<value>      evaluate in the page (may return a promise)
//   driver.send(method, params) -> Promise     one DevTools command (Input.dispatch...)
//
// The limits (2026-11-18: a fill on a real Workday page never returned, for two minutes at a
// time, because nothing here had one):
//   * every call to the page or the debugger is raced against a timer (CALL_MS). A call that
//     does not answer ends the whole job at once ("dead"): the page is not asked anything more;
//   * each job has a wall-clock cap (KEYS_MS, SEARCH_MS) checked before every step;
//   * each field is handled at most once per fill (a ref named twice is skipped), and a search
//     runs at most MAX_QUERIES queries per field;
//   * both always return what they did: {typed | picked, ..., timed_out, dead}. ``dead`` (a call
//     did not answer) only ends THIS job: both jobs are optional extras after the engine's fill,
//     so the fill itself is never reported as stalled for them (a fill of 21 fields was thrown
//     away four times over one search call, 2026-11-18). main.js reports the fields instead.
// ``log`` (optional): one line per sub-step, a field's position in the list and the step's name
// and time, never a label or a value. main.js passes it when SJ_AGENT_DEBUG=1.
"use strict";

const CALL_MS = 2500;          // one page or debugger call (every page call here is synchronous)
const KEYS_MS = 15000;         // all the date typing of one fill
const SEARCH_MS = 20000;       // all the searches of one fill
const CLOSE_MS = 3000;         // the close of one search, allowed even after SEARCH_MS
const RESULTS_MS = 4000;       // at most, for a search's results after Enter (it ends as soon as they show)
const NONE_MS = 2000;          // a list that says it has nothing is believed after this long
const MAX_QUERIES = 4;
const MAX_FIELDS = 8;

const TIMED_OUT = Symbol("timed out");
function capped(p, ms) {
  let t = null;
  const timer = new Promise((resolve) => { t = setTimeout(() => resolve(TIMED_OUT), Math.max(1, ms)); });
  return Promise.race([Promise.resolve(p).catch(() => null), timer]).finally(() => clearTimeout(t));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function limits(o) {
  o = o || {};
  const n = (v, d) => (Number(v) > 0 ? Number(v) : d);
  return { call: n(o.callMs, CALL_MS), keys: n(o.keysMs, KEYS_MS), search: n(o.searchMs, SEARCH_MS),
           close: n(o.closeMs, CLOSE_MS), results: n(o.resultsMs, RESULTS_MS), claim: n(o.claimMs, CLAIM_MS) };
}

// One runner per job: every step goes through ``step``, which stops everything when the job's
// time is up or a call did not answer.
function runner(driver, capMs, L, log, name) {
  const t0 = Date.now();
  const st = { dead: false, timed_out: false };
  const left = () => capMs - (Date.now() - t0);
  const note = (what, at) => { if (log) log(name + ": " + what + " " + (Date.now() - at) + "ms"); };
  // step(what, fn, graceMs): fn's value, or null when the job is over (dead / out of time)
  const step = async (what, fn, graceMs) => {
    if (st.dead) return null;
    const room = Math.max(left(), graceMs || 0);
    if (room <= 0) { st.timed_out = true; return null; }
    const at = Date.now();
    const r = await capped(fn(), Math.min(L.call, room));
    if (r === TIMED_OUT) {
      // a whole call's time with no answer: the page is gone. Cut short by the job's own cap: only out of time.
      st.timed_out = true;
      if (room >= L.call) st.dead = true;
      note(what + (st.dead ? " did not answer" : " ran out of time"), at);
      return null;
    }
    note(what, at);
    return r;
  };
  return { st, left, step, ms: () => Date.now() - t0 };
}

const refSel = (ref) => JSON.stringify('[data-tailor-ref="' + ref + '"]');
const typeText = async (run, what, text) => {
  for (const ch of String(text)) {
    if (run.st.dead || run.left() <= 0) return false;
    await run.step(what, () => run.driver.send("Input.dispatchKeyEvent", { type: "keyDown", key: ch, text: ch, unmodifiedText: ch }));
    await run.step(what, () => run.driver.send("Input.dispatchKeyEvent", { type: "keyUp", key: ch }));
  }
  return !run.st.dead;
};

// Type into boxes that take only real key presses (Workday's date segments: a value set from
// script shows but is not kept). keys: [{ref, text}], empty boxes only (the engine's rule), each
// focused, typed, and the last one left with a blur. Then the engine reads back what landed.
// -> {typed, of, landed, missed, timed_out, dead, ms}
async function typeKeys(driver, keys, opts) {
  const L = limits(opts);
  const log = opts && opts.log;
  const run = runner(driver, L.keys, L, log, "typeKeys");
  run.driver = driver;
  const seen = new Set();
  const list = (keys || []).filter((k) => k && k.ref && !seen.has(k.ref) && seen.add(k.ref)).slice(0, 60);
  const out = { typed: 0, of: list.length, landed: 0, missed: 0, timed_out: false, dead: false, ms: 0 };
  for (let i = 0; i < list.length; i++) {
    if (run.st.dead || run.left() <= 0) { run.st.timed_out = true; break; }
    const k = list[i];
    const ok = await run.step("key " + (i + 1) + " focus", () => driver.evalJs(`(() => { const el = document.querySelector(${refSel(k.ref)}); if (!el) return false;
      try { el.scrollIntoView({ block: "center", inline: "nearest" }); } catch (e) {}
      el.focus(); return document.activeElement === el; })()`));
    if (!ok) continue;
    // The engine only names EMPTY boxes, so nothing is cleared first: a Backspace in an empty
    // date segment steps back into the one before it and erases that.
    if (await typeText(run, "key " + (i + 1) + " type", k.text)) out.typed++;
  }
  await run.step("blur", () => driver.evalJs("(() => { const a = document.activeElement; if (a && a !== document.body) a.blur(); })()"), 500);
  const kl = await run.step("read back", () => driver.evalJs("window.TailorAutofill.keysLanded ? window.TailorAutofill.keysLanded() : null"), 1000);
  if (kl && typeof kl === "object") { out.landed = Number(kl.landed) || 0; out.missed = Number(kl.missed) || 0; }
  out.timed_out = run.st.timed_out; out.dead = run.st.dead; out.ms = run.ms();
  if (log) log("typeKeys: typed " + out.typed + " of " + out.of + ", landed " + out.landed + ", missed " + out.missed
               + (out.timed_out ? ", out of time" : "") + (out.dead ? ", the page did not answer" : "") + " " + out.ms + "ms");
  return out;
}

// Search a list with REAL input: the fallback of the engine's own in-page search (promptSearch
// in autofill_core.js), asked for only when the page did not take a script's Enter AND is
// visible (real keys and clicks do not reach a page that is out of sight, measured 2026-11-19).
// For each list the engine names ({ref, kind, queries, want | items, max, label}), per query:
// ask the engine where to click (the box, or the search box of its popup), click, give the page
// up to CLAIM_MS to bring up a popup (the engine claims it; none means an inline list, typed
// into the box itself), type the query, Enter, and let the engine press the best REAL option
// (searchChoice); the next query only when nothing fit. Then the close, step by step (Done,
// Escape, a press outside, and one real Escape key if the engine asks for it), and closeSearch
// for what really reached the page.
// Every page call here is synchronous on the page side; all the waiting is done HERE, with this
// process's timers (a page with a popup open did not run its own, 2026-11-18).
// A problem in this optional job is never the fill's problem: the fields it could not finish
// come back in ``trouble`` (their labels, for the fill's ``left`` list; never logged).
// -> {picked, fields, left_open, trouble: [label], timed_out, dead, ms}
const CLAIM_MS = 1000;         // how long a click's popup may take to show
const POLL_MS = 150;
const TOOK_MS = 1000;          // how long a pressed choice may take to show
const CLOSE_STEP_MS = 250;
async function searchPicks(driver, ops, opts) {
  const L = limits(opts);
  const log = opts && opts.log;
  const run = runner(driver, L.search, L, log, "searchPicks");
  run.driver = driver;
  const key = async (what, k, vk) => {
    await run.step(what, () => driver.send("Input.dispatchKeyEvent", { type: "keyDown", key: k, code: k, windowsVirtualKeyCode: vk }));
    await run.step(what, () => driver.send("Input.dispatchKeyEvent", { type: "keyUp", key: k, code: k, windowsVirtualKeyCode: vk }));
  };
  const seen = new Set();
  const list = (ops || []).filter((o) => o && o.ref && !seen.has(o.ref) && seen.add(o.ref)).slice(0, MAX_FIELDS);
  const out = { picked: 0, fields: 0, left_open: 0, trouble: [], timed_out: false, dead: false, ms: 0 };
  const troubled = (op) => { if (op.label && !out.trouble.includes(op.label)) out.trouble.push(op.label); };
  for (let i = 0; i < list.length; i++) {
    if (run.st.dead || run.left() <= 0) { run.st.timed_out = true; break; }
    const op = list[i], ref = JSON.stringify(op.ref), f = "field " + (i + 1);
    out.fields++;
    let got = 0, started = false, popup = false;
    const queries = (op.queries || []).slice(0, MAX_QUERIES);
    for (let q = 0; q < queries.length; q++) {
      if (run.st.dead || run.left() <= 0 || got >= (op.max || 1)) break;
      const w = f + " query " + (q + 1) + " ";
      const box = await run.step(w + "target", () => driver.evalJs(`window.TailorAutofill.searchTarget(${ref})`));
      if (!box || !(box.w > 0)) break;                  // gone, or left alone (a popup would not close before)
      started = true;
      popup = popup || !!box.popup;
      const query = JSON.stringify(String(queries[q]));
      // the popup may render late: look for it for up to L.claim, on a field's first query only
      const awaitPopup = async () => {
        const t0 = Date.now(), waitFor = popup || q > 0 ? 0 : Math.min(L.claim, Math.max(0, run.left()));
        while (!popup && !run.st.dead && Date.now() - t0 < waitFor) {
          await sleep(POLL_MS);
          const look = await run.step(w + "look", () => driver.evalJs(`window.TailorAutofill.searchFocus(${ref}, false)`));
          if (look && look.popup) popup = true;
        }
      };
      // The results of a query: polled until the list has changed and holds choices (twice the
      // same), or says it has none for long enough ("No Items." can flash before results come),
      // or L.results is up. -> the last look, or null.
      const awaitResults = async (needChange) => {
        const t0 = Date.now();
        let last = null, same = 0, n = -1;
        while (!run.st.dead && run.left() > 0) {
          await sleep(Math.min(POLL_MS, L.results));
          const seen = await run.step(w + "results", () => driver.evalJs(`window.TailorAutofill.searchSeen(${ref})`));
          if (!seen) break;
          last = seen;
          const age = Date.now() - t0;
          same = seen.options === n ? same + 1 : 0; n = seen.options;
          if ((seen.changed || !needChange) && seen.options > 0 && same >= 1) break;
          if (age >= L.results) break;
          if (seen.changed && seen.options === 0 && age >= Math.min(NONE_MS, L.results)) break;
        }
        return last;
      };
      // Real input through the debugger (the engine already tried the search in the page).
      {
        const at = await run.step(w + "target", () => driver.evalJs(`window.TailorAutofill.searchTarget(${ref})`));
        if (!at || !(at.w > 0)) break;
        await run.step(w + "press", () => driver.send("Input.dispatchMouseEvent", { type: "mousePressed", x: at.x, y: at.y, button: "left", clickCount: 1 }));
        await run.step(w + "release", () => driver.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: at.x, y: at.y, button: "left", clickCount: 1 }));
        popup = popup || !!at.popup;
        await awaitPopup();
        const foc = await run.step(w + "focus", () => driver.evalJs(`window.TailorAutofill.searchFocus(${ref}, true)`));
        if (foc && foc.popup) popup = true;
        if (log && foc && !foc.popup) log("searchPicks: " + w + "no popup claimed; the page added: " + ((foc.added || []).join(" ") || "nothing"));
        if (run.st.dead) break;
        await key(w + "clear", "Backspace", 8);
        if (!(await typeText(run, w + "type", queries[q]))) break;
        await key(w + "enter", "Enter", 13);
        if (run.st.dead) break;
        await awaitResults(false);
      }
      const spec = JSON.stringify({ kind: op.kind, want: op.want || [], items: op.items || [] });
      let r = await run.step(w + "choice", () => driver.evalJs(`window.TailorAutofill.searchChoice(${ref}, ${spec})`));
      const t1 = Date.now();
      let again = false;
      while (r && r.pending && !run.st.dead && Date.now() - t1 < TOOK_MS) {
        await sleep(POLL_MS);
        // half way with nothing to show: the row itself is pressed, once
        const retry = !again && Date.now() - t1 >= TOOK_MS / 2;
        if (retry) again = true;
        r = await run.step(w + "took", () => driver.evalJs(`window.TailorAutofill.searchTook(${ref}, ${retry})`));
      }
      if (r && r.ok) got++;
    }
    if (!started) continue;
    // The close: always tried, also when the time is up (a list is never left open on purpose),
    // unless the page stopped answering. Each step is one short page call; the waits are ours.
    let state = null;
    for (let stepNo = 0; stepNo < 3 && !run.st.dead; stepNo++) {
      await run.step(f + " close step " + stepNo, () => driver.evalJs(`window.TailorAutofill.searchClose(${ref}, ${stepNo})`), L.close);
      await sleep(CLOSE_STEP_MS);
      state = await run.step(f + " close look", () => driver.evalJs(`window.TailorAutofill.searchClose(${ref}, -1)`), L.close);
      if (state && !state.open) break;
    }
    if (state && state.open && state.real_escape && !run.st.dead) {
      await run.step(f + " close real escape", () => driver.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 }), L.close);
      await run.step(f + " close real escape", () => driver.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 }), L.close);
      await sleep(CLOSE_STEP_MS);
    }
    const c = await run.step(f + " end", () => driver.evalJs(`window.TailorAutofill.closeSearch(${ref})`), L.close);
    // what really reached the page once the list (or its popup) was closed
    if (c && typeof c.picked === "number") out.picked += c.picked;
    if (!c || c.ok === false) { out.left_open++; troubled(op); }
  }
  out.timed_out = run.st.timed_out; out.dead = run.st.dead; out.ms = run.ms();
  if (log) log("searchPicks: picked " + out.picked + " in " + out.fields + " of " + list.length + " fields, left open " + out.left_open
               + (out.timed_out ? ", out of time" : "") + (out.dead ? ", a page call did not answer" : "") + " " + out.ms + "ms");
  return out;
}

module.exports = { typeKeys, searchPicks, capped, TIMED_OUT, CALL_MS, KEYS_MS, SEARCH_MS, CLOSE_MS, MAX_QUERIES, MAX_FIELDS };
