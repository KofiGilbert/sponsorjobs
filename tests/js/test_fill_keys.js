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
const SECRET = "Marine Biology", LABEL = "Field of Study";
const OPS = [{ ref: "f1", kind: "field", want: ["marine"], queries: [SECRET, "marine", "biology", "x", "y", "z"], label: LABEL },
             { ref: "f1", kind: "field", want: ["marine"], queries: [SECRET], label: LABEL },   // the same field again
             { ref: "f2", kind: "skills", items: ["SQL"], queries: ["SQL"], max: 3, label: "Type to Add Skills" }];
const KEYS = [{ ref: "k1", text: "3" }, { ref: "k2", text: "2026" }, { ref: "k1", text: "3" }];
// a page that behaves: an inline list (no popup), nothing open after the first close step
const PAGE = { searchTarget: { x: 5, y: 5, w: 40 }, searchInPage: { popup: false }, searchSeen: { options: 0, changed: false, none: false }, searchFocus: { popup: false, focused: true, added: ["div[aid=somePopup]"] },
               searchChoice: { ok: false }, searchTook: { ok: false, pending: false },
               searchClose: { open: false, real_escape: false }, closeSearch: { ok: true, picked: 0 } };
const FAST = { resultsMs: 1, claimMs: 300, inPage: "no" };          // real keys only, unless a case says otherwise

(async () => {
  // 1. nothing fits: each field once, at most MAX_QUERIES queries, exactly one end per field
  {
    const d = fake(PAGE);
    const lines = [];
    const r = await K.searchPicks(d, OPS, Object.assign({ log: (l) => lines.push(l) }, FAST));
    assert.deepStrictEqual([r.picked, r.fields, r.left_open, r.timed_out, r.dead, r.trouble], [0, 2, 0, false, false, []]);
    assert.strictEqual(count(d, "searchChoice"), K.MAX_QUERIES + 1);
    assert.strictEqual(count(d, "closeSearch"), 2);
    // the debug lines name positions and steps, never a query, a label or a ref
    assert.ok(lines.length > 10 && lines.every((l) => !l.includes(SECRET) && !/marine|biology|SQL|f1|f2|Field of Study|Skills/i.test(l)), lines.join("\n"));
    assert.ok(lines.some((l) => /field 1 query 2 choice \d+ms/.test(l)) && /picked 0 in 2 of 2 fields/.test(lines[lines.length - 1]));
    // no popup was claimed after the click: the log says what the page added (tags and ids only)
    assert.ok(lines.some((l) => /field 1 query 1 no popup claimed; the page added: div\[aid=somePopup\]/.test(l)), lines.join("\n"));
    // the popup is waited for on a field's first query only (about a second), not on the later ones
    assert.ok(count(d, ", false)") >= 2 && count(d, ", false)") <= 2 * 8, "looks: " + count(d, ", false)"));
  }
  // 2. a pick: what the end says reached the page is what counts; a list still open is reported by its label
  {
    const d = fake(Object.assign({}, PAGE, { searchChoice: { ok: true },
      searchClose: (e) => later(e.includes('"f1"') ? { open: false, real_escape: false } : { open: true, real_escape: true }),
      closeSearch: (e) => later(e.includes('"f1"') ? { ok: true, picked: 1 } : { ok: false, picked: 0 }) }));
    const r = await K.searchPicks(d, OPS, FAST);
    assert.deepStrictEqual([r.picked, r.left_open, r.dead, r.trouble], [1, 1, false, ["Type to Add Skills"]]);
    assert.strictEqual(count(d, "searchChoice"), 1 + 1);            // f1 stops at its first fit; f2 has one query
    // the field whose list stayed open got its three close steps and ONE real Escape key, f1 got one step and none
    const escapes = d.calls.send.filter(([m, p]) => m === "Input.dispatchKeyEvent" && p.key === "Escape" && p.type === "keyDown");
    assert.strictEqual(escapes.length, 1);
    assert.strictEqual(d.calls.evalJs.filter((e) => /searchClose\("f2", [012]\)/.test(e)).length, 3);
    assert.strictEqual(d.calls.evalJs.filter((e) => /searchClose\("f1", [012]\)/.test(e)).length, 1);
  }
  // 3. a popup that shows late: looked for until it is there, then the query is typed
  {
    let looks = 0;
    const d = fake(Object.assign({}, PAGE, { searchFocus: (e) => later(e.includes("false") ? { popup: ++looks >= 3 } : { popup: looks >= 3, focused: true }) }));
    const lines = [];
    await K.searchPicks(d, [OPS[2]], { resultsMs: 1, claimMs: 900, inPage: "no", log: (l) => lines.push(l) });
    assert.strictEqual(looks, 3);
    assert.ok(!lines.some((l) => /no popup claimed/.test(l)));
  }
  // 4. a pressed choice that shows a moment later is waited for, and counted
  {
    let asked = 0;
    const d = fake(Object.assign({}, PAGE, { searchChoice: { ok: false, pending: true }, searchTook: () => later(++asked >= 2 ? { ok: true } : { ok: false, pending: true }),
                                             closeSearch: { ok: true, picked: 1 } }));
    const r = await K.searchPicks(d, [OPS[2]], FAST);
    assert.deepStrictEqual([r.picked, asked], [1, 2]);
  }
  // 5. a field the engine will not search (searchTarget null: gone, or a popup is stuck): nothing clicked, no end
  {
    const d = fake({ searchTarget: null });
    const r = await K.searchPicks(d, OPS, FAST);
    assert.deepStrictEqual([r.picked, r.fields, d.calls.send.length, count(d, "closeSearch")], [0, 2, 0, 0]);
  }
  // 6. a page call that never answers (the real page's ``focus``, 2026-11-18): the job ends at
  //    that call, inside one call's cap, asks nothing more, and names the field for ``left``
  for (const stuckAt of ["searchTarget", "searchInPage", "searchSeen", "searchFocus", "searchChoice", "searchClose", "closeSearch"]) {
    const d = fake(Object.assign({}, PAGE, { [stuckAt]: never }));
    const t = Date.now();
    const lines = [];
    const r = await K.searchPicks(d, OPS, { resultsMs: 50, claimMs: 100, callMs: 120, log: (l) => lines.push(l) });
    assert.ok(r.dead && r.timed_out, stuckAt);
    assert.ok(Date.now() - t < 2000, stuckAt + " took " + (Date.now() - t));
    assert.strictEqual(count(d, stuckAt), 1, stuckAt + ": asked once, never again");
    if (stuckAt !== "searchTarget") assert.deepStrictEqual(r.trouble, [LABEL], stuckAt);
    assert.ok(lines.every((l) => !l.includes(LABEL)), "the label is never logged");
  }
  // 7. a debugger command that never answers ends it the same way
  {
    const d = fake(Object.assign({}, PAGE, { send: never }));
    const t = Date.now();
    const r = await K.searchPicks(d, OPS, { resultsMs: 1, claimMs: 100, callMs: 120, inPage: "no" });
    assert.ok(r.dead && Date.now() - t < 1500 && d.calls.send.length === 1);
  }
  // 8. a slow page: the job's own cap ends it; the field in hand still gets its close
  {
    const d = fake(PAGE, 25);
    const t = Date.now();
    const r = await K.searchPicks(d, OPS, { resultsMs: 1, claimMs: 100, searchMs: 300, closeMs: 200, inPage: "no" });
    assert.ok(r.timed_out && !r.dead, JSON.stringify(r));
    assert.ok(Date.now() - t < 2500, "took " + (Date.now() - t));
    assert.strictEqual(count(d, "closeSearch"), 1);
  }
  // 8b. the search from inside the page: when the list reacts, no click and no key goes through
  //     the debugger at all; when it does not, real keys take over, for this field and the rest
  {
    const d = fake(Object.assign({}, PAGE, { searchSeen: { options: 3, changed: true, none: false }, searchChoice: { ok: true }, closeSearch: { ok: true, picked: 1 } }));
    const r = await K.searchPicks(d, OPS, { resultsMs: 30, claimMs: 60 });
    assert.deepStrictEqual([r.picked, r.left_open, r.dead], [2, 0, false]);
    assert.strictEqual(d.calls.send.length, 0, "no debugger input when the page takes the in-page search");
    assert.strictEqual(count(d, "searchFocus(\"f1\", true)") + count(d, 'searchFocus("f1", true)'), 0);
    const d2 = fake(PAGE);                                           // the list never reacts
    const lines = [];
    await K.searchPicks(d2, OPS, { resultsMs: 30, claimMs: 60, log: (l) => lines.push(l) });
    assert.strictEqual(count(d2, "searchInPage"), 1, "tried once, then real keys only");
    assert.ok(d2.calls.send.length > 0 && lines.some((l) => /in-page search did not register/.test(l)));
  }
  // 9. typing: each box once, then the read-back; a box that does not take the focus is skipped
  {
    const d = fake({ "k2": false, keysLanded: { landed: 1, missed: 1 } });
    const lines = [];
    const r = await K.typeKeys(d, KEYS, { log: (l) => lines.push(l) });
    assert.deepStrictEqual([r.typed, r.of, r.landed, r.missed, r.timed_out, r.dead], [1, 2, 1, 1, false, false]);
    assert.strictEqual(d.calls.send.length, 2);                      // "3": one key down, one key up
    assert.ok(lines.every((l) => !/2026|k1|k2/.test(l)), lines.join("\n"));
  }
  // 10. typing on a page that never answers, or is slow: returns, and says so
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
