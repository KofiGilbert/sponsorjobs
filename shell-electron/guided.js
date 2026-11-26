// Guided fill (2026-11-26, the owner: "a human watching must be able to follow it with their
// eyes"). Kept apart from main.js so it is tested in a real page without Electron
// (tests/js/test_guided_walk.js).
//
// The decisions come first: the form's empty fields are read with their choices and the plan
// decides every one (the engine's own /api/autoapply/plan). Then the walk, strictly in page order,
// top to bottom, one field after another, with the drawn pointer (extension/pointer_overlay.js):
//   the page scrolls to the field ("Scrolling"), the pointer moves to it ("Clicking: <field>"),
//   text is typed a few characters at a time, a choice is picked and its list closed ("Filling:
//   <field>"), a file goes in ("Uploading resume"); a field the person must answer gets a short
//   pause and an outline ("Needs you: <field>"). About 0.6 to 1.2 s a field. Reduced motion: no
//   glide (the overlay and the scroll jump), still in order. Nothing is ever submitted here.
"use strict";

const num = (v, d) => (Number(v) >= 0 && v !== "" && v != null ? Number(v) : d);
const TIMING = {
  min: num(process.env.SJ_WALK_MIN_MS, 650),        // a field takes at least this long
  move: num(process.env.SJ_WALK_MOVE_MS, 200),      // the pointer's move to it
  scroll: num(process.env.SJ_WALK_SCROLL_MS, 380),  // the page's scroll to it
  needs: num(process.env.SJ_WALK_NEEDS_MS, 550),    // the pause on a field that needs the person
  perChar: num(process.env.SJ_WALK_CHAR_MS, 40),    // typing, a character
  burst: 14,                                        // typed one by one, then the rest at once
};

const q = (v) => JSON.stringify(v);

// deps: { js(expr, ms), plan(fields) -> {ok, ops, left, cost}, upload(refs, pathOf) -> {n, seen},
//         say?(info), alive?(), seenBefore?(fields), notePlanned?(fields), wait?(ms), timing? }
async function walk(deps) {
  const t = Object.assign({}, TIMING, deps.timing || {});
  const wait = deps.wait || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const js = deps.js;
  const out = { filled: 0, uploaded: 0, needs: [], cost: 0, files: [], walked: 0, order: [] };
  const fields = await js(`window.TailorAutofill.extractFormAsync(${q({ onlyEmpty: true, budgetMs: 15000 })})`, 20000);
  if (!Array.isArray(fields) || !fields.length) return out;
  // a page walked before (the agent's next step on it): what is left is the person's
  if (deps.seenBefore && deps.seenBefore(fields)) return out;
  const plan = await deps.plan(fields);
  if (deps.notePlanned) deps.notePlanned(fields);
  out.cost = (plan && Number(plan.cost)) || 0;
  const ops = plan && plan.ok ? (plan.ops || []) : [];
  const left = ((plan && plan.left) || []).map((l) => l.ref).filter(Boolean);
  const uploadPath = new Map(ops.filter((o) => o.op === "upload" && o.path).map((o) => [o.ref, o.path]));
  const byRef = new Map();
  for (const o of ops) if (!byRef.has(o.ref)) byRef.set(o.ref, o);
  const order = await js(`window.TailorAutofill.orderRefs(${q([...byRef.keys(), ...left.filter((r) => !byRef.has(r))])})`);
  const refs = Array.isArray(order) ? order : [];
  let i = 0;
  for (const ref of refs) {
    if (deps.alive && !deps.alive()) break;
    const t0 = Date.now();
    const w = await js(`window.TailorAutofill.walkTo(${q(ref)})`, 3000);
    if (!w || !w.ok) continue;
    const name = w.label || "this field";
    const pt = (label) => js(`window.TailorAutofill.pointAt(${q(ref)}, ${q(label)})`, 3000);
    if (w.scrolled) { await pt("Scrolling"); await wait(t.scroll); }
    const op = byRef.get(ref);
    out.order.push(ref);
    if (!op) {                                               // the person's: a pause and an outline
      await pt("Needs you: " + name);
      await js(`window.TailorAutofill.markNeeds(${q(ref)})`, 3000);
      out.needs.push(name);
      await wait(t.needs);
      continue;
    }
    i++;
    if (deps.say) deps.say({ guide: { i, n: byRef.size }, message: "Filling " + i + " of " + byRef.size });
    if (op.op === "upload") {
      await pt(op.from === "letter" ? "Uploading cover letter" : "Uploading resume");
      const rep = await js(`window.TailorAutofill.applyOpsAsync(${q([op])}, { budgetMs: 8000 })`, 10000);
      if (rep && (rep.uploads || []).length && deps.upload) {
        const u = await deps.upload(rep.uploads, (r) => uploadPath.get(r) || "");
        out.uploaded += (u && u.n) || 0;
        for (const x of (u && u.seen) || []) out.files.push({ kind: op.from === "letter" ? "cover_letter" : "resume", path: x.path, how: x.how, earlier: !!x.earlier });
      }
    } else {
      await pt("Clicking: " + name);
      await wait(t.move);
      await pt("Filling: " + name);
      let typed = false;
      if (op.op === "fill" && op.text) {
        typed = (await js(`window.TailorAutofill.typeVisibly(${q(ref)}, ${q(String(op.text))}, ${q({ perChar: t.perChar, burst: t.burst })})`, 8000)) === true;
      }
      if (typed) out.filled++;
      else {
        const rep = await js(`window.TailorAutofill.applyOpsAsync(${q([op])}, { budgetMs: 8000 })`, 10000);
        out.filled += (rep && rep.filled) || 0;
      }
      await js("window.TailorAutofill.tidyMenus()", 4000);    // its list closed before the next field
    }
    out.walked++;
    const spent = Date.now() - t0;
    if (spent < t.min) await wait(t.min - spent);
  }
  // the walk is over: the pointer goes (a pointer left behind sits over the wrong field once the
  // page moves); the run's end puts it where the person acts (guideEnd)
  await js("window.TailorPointer && window.TailorPointer.hide()", 2000);
  return out;
}

module.exports = { walk, TIMING };
