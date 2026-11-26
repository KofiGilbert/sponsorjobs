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
//         say?(info), alive?(), done?(Set of refs walked on this page), notePlanned?(fields),
//         wait?(ms), timing? }
//
// (2026-11-26, the owner, after Lam) Nothing is ever filled out of sight:
//   * a site window (a privacy notice) open over the page stops the walk before anything is
//     touched (out.modal = its title); the caller reads it and acknowledges it visibly, then walks
//     again, carrying on where it stopped (``done``);
//   * every box is filled the same way, top to bottom, whether or not the site already put
//     something in it (its parse, a saved draft): those boxes are emptied first in one quick
//     reset, then the pointer goes to each, the box glows, our value is typed or picked
//     ("Filling: <field>"), once;
//   * a window that opens while walking (Lam's notice comes up when the resume goes in) stops the
//     walk at once, the same way.
async function walk(deps) {
  const t = Object.assign({}, TIMING, deps.timing || {});
  const wait = deps.wait || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const js = deps.js;
  const done = deps.done || new Set();
  const out = { filled: 0, uploaded: 0, needs: [], cost: 0, files: [], walked: 0, order: [], modal: "" };
  const modalNow = async () => { const m = await js("window.TailorAutofill.siteModal()", 3000); return (m && m.title) || ""; };
  const stopFor = async (title) => { out.modal = title; return out; };
  { const m = await modalNow(); if (m) return stopFor(m); }
  const upload = async (op, uploadPath) => {
    const rep = await js(`window.TailorAutofill.applyOpsAsync(${q([op])}, { budgetMs: 8000 })`, 10000);
    if (rep && (rep.uploads || []).length && deps.upload) {
      const u = await deps.upload(rep.uploads, (r) => uploadPath.get(r) || "");
      out.uploaded += (u && u.n) || 0;
      for (const x of (u && u.seen) || []) out.files.push({ kind: op.from === "letter" ? "cover_letter" : "resume", path: x.path, how: x.how, earlier: !!x.earlier });
    }
  };
  let i = 0;
  // Round 1: every box on the page; then only boxes that came up empty since (a question that
  // opens on an answer), at most twice more.
  for (let round = 0; round < 3; round++) {
    if (deps.alive && !deps.alive()) break;
    const got = await js(`window.TailorAutofill.extractFormAsync(${q({ onlyEmpty: round > 0, budgetMs: 15000 })})`, 20000);
    const fields = (Array.isArray(got) ? got : []).filter((f) => f && f.ref && !done.has(f.ref));
    if (!fields.length) break;
    const plan = await deps.plan(fields);
    if (deps.notePlanned) deps.notePlanned(fields);
    out.cost += (plan && Number(plan.cost)) || 0;
    const ops = plan && plan.ok ? (plan.ops || []) : [];
    const left = ((plan && plan.left) || []).map((l) => l.ref).filter(Boolean);
    const uploadPath = new Map(ops.filter((o) => o.op === "upload" && o.path).map((o) => [o.ref, o.path]));
    const byRef = new Map();
    for (const o of ops) if (!byRef.has(o.ref) && !done.has(o.ref)) byRef.set(o.ref, o);
    const order = await js(`window.TailorAutofill.orderRefs(${q([...byRef.keys(), ...left.filter((r) => !byRef.has(r) && !done.has(r))])})`);
    const refs = Array.isArray(order) ? order : [];
    if (!refs.length) break;
    // what the site already put in the boxes we fill goes in one quick reset: the form looks empty,
    // then each box is filled once, in order (never a visible delete then type)
    if (round === 0) await js(`window.TailorAutofill.resetBoxes(${q([...byRef.keys()].filter((r) => byRef.get(r).op !== "upload"))})`, 4000);
    for (const ref of refs) {
      if (deps.alive && !deps.alive()) break;
      { const m = await modalNow(); if (m) return stopFor(m); }
      const t0 = Date.now();
      const w = await js(`window.TailorAutofill.walkTo(${q(ref)})`, 3000);
      if (!w || !w.ok) { done.add(ref); continue; }
      if (w.covered) return stopFor(w.covered);
      const name = w.label || "this field";
      const pt = (label) => js(`window.TailorAutofill.pointAt(${q(ref)}, ${q(label)})`, 3000);
      const op = byRef.get(ref);
      if (!op) {
        // the person's: only when it is still empty (a box the site filled is left as it is)
        done.add(ref);
        if (!(await js(`window.TailorAutofill.markNeeds(${q(ref)})`, 3000))) continue;
        if (w.scrolled) { await pt("Scrolling"); await wait(t.scroll); }
        await pt("Needs you: " + name);
        out.needs.push(name);
        out.order.push(ref);
        await wait(t.needs);
        continue;
      }
      if (w.scrolled) { await pt("Scrolling"); await wait(t.scroll); }
      out.order.push(ref);
      i++;
      if (deps.say) deps.say({ guide: { i, n: i + Math.max(0, refs.length - refs.indexOf(ref) - 1) }, message: "Filling " + i });
      await js(`window.TailorAutofill.flash(${q(ref)}, true)`, 2000);
      if (op.op === "upload") {
        await pt(op.from === "letter" ? "Uploading cover letter" : "Uploading resume");
        await upload(op, uploadPath);
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
      await js(`window.TailorAutofill.flash(${q(ref)}, false)`, 2000);
      done.add(ref);
      out.walked++;
      const spent = Date.now() - t0;
      if (spent < t.min) await wait(t.min - spent);
    }
  }
  // a window the last field opened (the resume going in): the caller deals with it
  { const m = await modalNow(); if (m) return stopFor(m); }
  // the walk is over: the pointer goes (a pointer left behind sits over the wrong field once the
  // page moves); the run's end puts it where the person acts (guideEnd)
  await js("window.TailorPointer && window.TailorPointer.hide()", 2000);
  return out;
}

module.exports = { walk, TIMING };
