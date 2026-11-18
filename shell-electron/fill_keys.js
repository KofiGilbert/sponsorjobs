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
//   * both always return what they did: {typed | picked, ..., timed_out, dead}.
// ``log`` (optional): one line per sub-step, a field's position in the list and the step's name
// and time, never a label or a value. main.js passes it when SJ_AGENT_DEBUG=1.
"use strict";

const CALL_MS = 4000;          // one page or debugger call
const KEYS_MS = 15000;         // all the date typing of one fill
const SEARCH_MS = 20000;       // all the searches of one fill
const CLOSE_MS = 3000;         // the close of one search, allowed even after SEARCH_MS
const RESULTS_MS = 1500;       // a search's results, after Enter
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
           close: n(o.closeMs, CLOSE_MS), results: n(o.resultsMs, RESULTS_MS) };
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

// Search lists that filter only on a real Enter (Workday's Field of Study and Skills). For each
// one the engine names ({ref, kind, queries, want | items, max}): ask the engine where to click
// (the box, or the search box of its popup), click, type a query, Enter, let the engine pick the
// best REAL option (searchChoice); the next query only when nothing fit. Then ONE close
// (closeSearch), which also says how many choices really reached the page. Both lists are
// optional: nothing that fits leaves them empty.
// -> {picked, fields, left_open, timed_out, dead, ms}
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
  const out = { picked: 0, fields: 0, left_open: 0, timed_out: false, dead: false, ms: 0 };
  for (let i = 0; i < list.length; i++) {
    if (run.st.dead || run.left() <= 0) { run.st.timed_out = true; break; }
    const op = list[i], ref = JSON.stringify(op.ref), f = "field " + (i + 1);
    out.fields++;
    let got = 0, started = false;
    const queries = (op.queries || []).slice(0, MAX_QUERIES);
    for (let q = 0; q < queries.length; q++) {
      if (run.st.dead || run.left() <= 0 || got >= (op.max || 1)) break;
      const w = f + " query " + (q + 1) + " ";
      const box = await run.step(w + "target", () => driver.evalJs(`window.TailorAutofill.searchTarget(${ref})`));
      if (!box || !(box.w > 0)) break;                  // gone, or left alone (its popup would not close before)
      started = true;
      await run.step(w + "press", () => driver.send("Input.dispatchMouseEvent", { type: "mousePressed", x: box.x, y: box.y, button: "left", clickCount: 1 }));
      await run.step(w + "release", () => driver.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: box.x, y: box.y, button: "left", clickCount: 1 }));
      await run.step(w + "focus", () => driver.evalJs(`window.TailorAutofill.searchFocus(${ref})`));
      await key(w + "clear", "Backspace", 8);
      if (!(await typeText(run, w + "type", queries[q]))) break;
      await key(w + "enter", "Enter", 13);
      if (run.st.dead) break;
      await sleep(Math.max(0, Math.min(L.results, run.left())));
      const spec = JSON.stringify({ kind: op.kind, want: op.want || [], items: op.items || [] });
      const r = await run.step(w + "choice", () => driver.evalJs(`window.TailorAutofill.searchChoice(${ref}, ${spec})`));
      if (r && r.ok) got++;
    }
    if (!started) continue;
    // ONE close, also when the time is up (a list is never left open on purpose); never asked
    // of a page that stopped answering.
    const c = await run.step(f + " close", () => driver.evalJs(`(async () => { const el = document.querySelector(${refSel(op.ref)}); if (el && el.value) {
        const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set; set.call(el, "");
        el.dispatchEvent(new Event("input", { bubbles: true })); }
      return window.TailorAutofill.closeSearch(${ref}); })()`), L.close);
    // what really reached the page once the list (or its popup) was closed
    if (c && typeof c.picked === "number") out.picked += c.picked;
    if (!c || c.ok === false) out.left_open++;
  }
  out.timed_out = run.st.timed_out; out.dead = run.st.dead; out.ms = run.ms();
  if (log) log("searchPicks: picked " + out.picked + " in " + out.fields + " of " + list.length + " fields, left open " + out.left_open
               + (out.timed_out ? ", out of time" : "") + (out.dead ? ", the page did not answer" : "") + " " + out.ms + "ms");
  return out;
}

module.exports = { typeKeys, searchPicks, capped, TIMED_OUT, CALL_MS, KEYS_MS, SEARCH_MS, CLOSE_MS, MAX_QUERIES, MAX_FIELDS };
