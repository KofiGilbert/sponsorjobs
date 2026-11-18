// The shell's real-input jobs (shell-electron/fill_keys.js) always return: a call that never
// answers, a page that is slow, a list with nothing fitting, the same field named twice.
// On 2026-11-18 a fill on a real Workday page never returned (two minutes at a time) because
// nothing in this part had a limit. Run: node tests/js/test_fill_keys.js
"use strict";
const assert = require("assert");
const path = require("path");
const K = require(path.resolve(__dirname, "..", "..", "shell-electron", "fill_keys.js"));

const never = () => new Promise(() => {});
const later = (v, ms) => new Promise((r) => setTimeout(() => r(v), ms || 0));
// A fake page: counts what it is asked, answers by the name of the engine call in the expression.
function fake(answers, delayMs) {
  const calls = { evalJs: [], send: [] };
  const hit = (expr) => Object.keys(answers).find((k) => expr.includes(k));
  return { calls,
    evalJs: (expr) => { calls.evalJs.push(expr); const k = hit(expr); const a = k ? answers[k] : true;
                        return typeof a === "function" ? a(expr) : later(a, delayMs); },
    send: (m, p) => { calls.send.push([m, p]); return answers.send ? answers.send(m, p) : later({}, delayMs); } };
}
const count = (d, name) => d.calls.evalJs.filter((e) => e.includes(name)).length;
const SECRET = "Marine Biology";
const OPS = [{ ref: "f1", kind: "field", want: ["marine"], queries: [SECRET, "marine", "biology", "x", "y", "z"] },
             { ref: "f1", kind: "field", want: ["marine"], queries: [SECRET] },                 // the same field again
             { ref: "f2", kind: "skills", items: ["SQL"], queries: ["SQL"], max: 3 }];
const KEYS = [{ ref: "k1", text: "3" }, { ref: "k2", text: "2026" }, { ref: "k1", text: "3" }];

(async () => {
  // 1. nothing fits: each field once, at most MAX_QUERIES queries, exactly one close per field
  {
    const d = fake({ searchTarget: { x: 5, y: 5, w: 40 }, searchChoice: { ok: false }, closeSearch: { ok: true, picked: 0 } });
    const lines = [];
    const r = await K.searchPicks(d, OPS, { resultsMs: 1, log: (l) => lines.push(l) });
    assert.deepStrictEqual([r.picked, r.fields, r.left_open, r.timed_out, r.dead], [0, 2, 0, false, false]);
    assert.strictEqual(count(d, "searchChoice"), K.MAX_QUERIES + 1);
    assert.strictEqual(count(d, "closeSearch"), 2);
    // the debug lines name positions and steps, never a query, a label or a ref
    assert.ok(lines.length > 10 && lines.every((l) => !l.includes(SECRET) && !/marine|biology|SQL|f1|f2/i.test(l)), lines.join("\n"));
    assert.ok(lines.some((l) => /field 1 query 2 choice \d+ms/.test(l)) && /picked 0 in 2 of 2 fields/.test(lines[lines.length - 1]));
  }
  // 2. a pick: what the close says reached the page is what counts; a close that failed is reported
  {
    const d = fake({ searchTarget: { x: 5, y: 5, w: 40 }, searchChoice: { ok: true },
                     closeSearch: (e) => later(e.includes('"f1"') ? { ok: true, picked: 1 } : { ok: false, picked: 0 }) });
    const r = await K.searchPicks(d, OPS, { resultsMs: 1 });
    assert.deepStrictEqual([r.picked, r.left_open, r.dead], [1, 1, false]);
    assert.strictEqual(count(d, "searchChoice"), 1 + 1);            // f1 stops at its first fit; f2 has one query
  }
  // 3. a field the engine will not search (searchTarget null: gone, or its popup is stuck): nothing clicked, no close
  {
    const d = fake({ searchTarget: null });
    const r = await K.searchPicks(d, OPS, { resultsMs: 1 });
    assert.deepStrictEqual([r.picked, r.fields, d.calls.send.length, count(d, "closeSearch")], [0, 2, 0, 0]);
  }
  // 4. the page never answers: the job ends at the first call, inside one call's cap
  for (const stuckAt of ["searchTarget", "searchFocus", "searchChoice", "closeSearch"]) {
    const d = fake({ searchTarget: { x: 5, y: 5, w: 40 }, searchChoice: { ok: false }, closeSearch: { ok: true, picked: 0 }, [stuckAt]: never });
    const t = Date.now();
    const r = await K.searchPicks(d, OPS, { resultsMs: 1, callMs: 120 });
    assert.ok(r.dead && r.timed_out, stuckAt);
    assert.ok(Date.now() - t < 1000, stuckAt + " took " + (Date.now() - t));
    assert.strictEqual(count(d, stuckAt), 1, stuckAt + ": asked once, never again");
  }
  // 5. a debugger command that never answers ends it the same way
  {
    const d = fake({ searchTarget: { x: 5, y: 5, w: 40 }, send: never });
    const t = Date.now();
    const r = await K.searchPicks(d, OPS, { resultsMs: 1, callMs: 120 });
    assert.ok(r.dead && Date.now() - t < 1000 && d.calls.send.length === 1);
  }
  // 6. a slow page: the job's own cap ends it; the field in hand still gets its one close
  {
    const d = fake({ searchTarget: { x: 5, y: 5, w: 40 }, searchChoice: { ok: false }, closeSearch: { ok: true, picked: 0 } }, 25);
    const t = Date.now();
    const r = await K.searchPicks(d, OPS, { resultsMs: 1, searchMs: 300, closeMs: 200 });
    assert.ok(r.timed_out && !r.dead, JSON.stringify(r));
    assert.ok(Date.now() - t < 1500, "took " + (Date.now() - t));
    assert.strictEqual(count(d, "closeSearch"), 1);
  }
  // 7. typing: each box once, then the read-back; a box that does not take the focus is skipped
  {
    const d = fake({ "k2": false, keysLanded: { landed: 1, missed: 1 } });
    const lines = [];
    const r = await K.typeKeys(d, KEYS, { log: (l) => lines.push(l) });
    assert.deepStrictEqual([r.typed, r.of, r.landed, r.missed, r.timed_out, r.dead], [1, 2, 1, 1, false, false]);
    assert.strictEqual(d.calls.send.length, 2);                      // "3": one key down, one key up
    assert.ok(lines.every((l) => !/2026|k1|k2/.test(l)), lines.join("\n"));
  }
  // 8. typing on a page that never answers, or is slow: returns, and says so
  {
    const d = fake({ "k1": never });
    const t = Date.now();
    const r = await K.typeKeys(d, KEYS, { callMs: 120 });
    assert.ok(r.dead && r.typed === 0 && Date.now() - t < 1000);
    assert.strictEqual(count(d, "keysLanded"), 0);                  // a dead page is asked nothing more
    const slow = fake({ keysLanded: { landed: 0, missed: 0 } }, 40);
    const many = Array.from({ length: 40 }, (_, i) => ({ ref: "k" + i, text: "2026" }));
    const t2 = Date.now();
    const r2 = await K.typeKeys(slow, many, { keysMs: 300 });
    assert.ok(r2.timed_out && !r2.dead && r2.typed < 40 && Date.now() - t2 < 2500, JSON.stringify(r2));
  }
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
