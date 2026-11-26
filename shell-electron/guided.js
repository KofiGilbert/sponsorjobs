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
//         say?(info), alive?(), done?(Set of refs filled on this page), asked?(Set of refs left for
//         the person), notePlanned?(fields),
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
//
// (2026-11-26) Forms inside frames another origin serves (iCIMS and similar): ``deps.frames`` is
// one context per such frame, { prefix, js(expr, ms) } (shell-electron/frames.js walkContexts),
// the engine already loaded in it and its refs carrying the frame's prefix. Their fields join the
// same walk: read and planned with the top page's, walked in ONE top-to-bottom order (a frame's
// fields sit where the frame sits on the page), each filled in its own frame with the same steps,
// glow and needs-you outline; the ONE pointer is drawn on the top page, placed through the frame's
// own place there (TailorAutofill.frameBox / pointFrom). A frame's field below the top page's fold
// is scrolled into the person's sight first. Same-origin frames are reached by the top page's own
// engine (its searchRoots). Nothing is ever submitted or filled out of sight.
async function walk(deps) {
  const t = Object.assign({}, TIMING, deps.timing || {});
  const wait = deps.wait || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const js = deps.js;
  const top = { prefix: "", js, top: true };
  const frames = (deps.frames || []).filter((f) => f && f.prefix && typeof f.js === "function");
  const ctxs = [top, ...frames];
  const ctxOf = (ref) => frames.find((f) => String(ref || "").startsWith(f.prefix)) || top;
  const done = deps.done || new Set();
  // the boxes left for the person: not walked again on this page, unless the caller asks for them
  // again (a rule may answer one now: the agent, before it stops on a notice, 2026-11-26)
  const asked = deps.asked || new Set();
  const out = { filled: 0, uploaded: 0, needs: [], cost: 0, files: [], walked: 0, order: [], modal: "" };
  const modalNow = async () => {
    for (const c of ctxs) { const m = await c.js("window.TailorAutofill.siteModal()", 3000); if (m && m.title) return m.title; }
    return "";
  };
  const stopFor = async (title) => { out.modal = title; return out; };
  { const m = await modalNow(); if (m) return stopFor(m); }
  for (const c of ctxs) await c.js("window.TailorAutofill.armSubmitGuard()", 2000);   // no key or script ever sends the form
  const upload = async (op, uploadPath, c) => {
    const rep = await c.js(`window.TailorAutofill.applyOpsAsync(${q([op])}, { budgetMs: 8000 })`, 10000);
    if (rep && (rep.uploads || []).length && deps.upload) {
      const u = await deps.upload(rep.uploads, (r) => uploadPath.get(r) || "", c.top ? null : c);
      out.uploaded += (u && u.n) || 0;
      for (const x of (u && u.seen) || []) out.files.push({ kind: op.from === "letter" ? "cover_letter" : "resume", path: x.path, how: x.how, earlier: !!x.earlier });
    }
  };
  let i = 0;
  // Round 1: every box on the page; then only boxes that came up empty since (a question that
  // opens on an answer), at most twice more.
  for (let round = 0; round < 3; round++) {
    if (deps.alive && !deps.alive()) break;
    let got = [];
    for (const c of ctxs) {
      const g = await c.js(`window.TailorAutofill.extractFormAsync(${q({ onlyEmpty: round > 0, budgetMs: 15000 })})`, 20000);
      if (Array.isArray(g)) got = got.concat(g);
    }
    const fields = got.filter((f) => f && f.ref && !done.has(f.ref) && !asked.has(f.ref));
    if (!fields.length) break;
    const plan = await deps.plan(fields);
    if (deps.notePlanned) deps.notePlanned(fields);
    out.cost += (plan && Number(plan.cost)) || 0;
    const ops = plan && plan.ok ? (plan.ops || []) : [];
    // only a REQUIRED box the plan could not answer is the person's (Meriton, 2026-11-26: an
    // optional "Preferred First Name" was flagged "Needs you"); an optional one is left empty
    const required = new Set(fields.filter((f) => f.required).map((f) => f.ref));
    const left = ((plan && plan.left) || []).map((l) => l.ref).filter((r) => r && required.has(r));
    const suggest = new Map(((plan && plan.left) || []).filter((l) => l && l.ref && l.suggest).map((l) => [l.ref, String(l.suggest)]));
    const uploadPath = new Map(ops.filter((o) => o.op === "upload" && o.path).map((o) => [o.ref, o.path]));
    const byRef = new Map();
    for (const o of ops) if (!byRef.has(o.ref) && !done.has(o.ref)) byRef.set(o.ref, o);
    const refs = await orderAll(js, ctxs, ctxOf, [...byRef.keys(), ...left.filter((r) => !byRef.has(r) && !done.has(r))]);
    if (!refs.length) break;
    // what the site already put in the boxes we fill goes in one quick reset: the form looks empty,
    // then each box is filled once, in order (never a visible delete then type)
    if (round === 0) {
      for (const c of ctxs) {
        const mine = [...byRef.keys()].filter((r) => byRef.get(r).op !== "upload" && ctxOf(r) === c);
        if (mine.length) await c.js(`window.TailorAutofill.resetBoxes(${q(mine)})`, 4000);
      }
    }
    for (const ref of refs) {
      if (deps.alive && !deps.alive()) break;
      { const m = await modalNow(); if (m) return stopFor(m); }
      const t0 = Date.now();
      const c = ctxOf(ref);
      const cjs = c.js;
      if (!c.top) await syncSight(js, c);               // what of the frame the person sees now
      let w = await cjs(`window.TailorAutofill.walkTo(${q(ref)})`, 3000);
      if (!w || !w.ok) { done.add(ref); continue; }
      if (w.covered) return stopFor(w.covered);
      if (!c.top) {
        // a frame cannot tell whether its field is in the person's sight (the top page may be
        // scrolled away from the frame): measured from the top page, scrolled there when it is not
        const sight = await frameSight(js, c, ref);
        if (sight === false) {
          const w2 = await cjs(`window.TailorAutofill.walkTo(${q(ref)}, { force: true })`, 3000);
          if (w2 && w2.ok) w = Object.assign({}, w, { scrolled: true });
          await settledFrame(js, c);
        }
      }
      const name = w.label || "this field";
      const pt = c.top ? (label) => js(`window.TailorAutofill.pointAt(${q(ref)}, ${q(label)})`, 3000)
                       : async (label) => {
                         const r = await cjs(`window.TailorAutofill.refRect(${q(ref)})`, 2000);
                         return js(`window.TailorAutofill.pointFrom(${q(c.prefix)}, ${q(r)}, ${q(label)}, { now: true })`, 3000);
                       };
      const op = byRef.get(ref);
      if (!op) {
        // the person's: only when it is still empty (a box the site filled is left as it is)
        asked.add(ref);
        if (!(await cjs(`window.TailorAutofill.markNeeds(${q(ref)}, ${q(suggest.get(ref) || "")})`, 3000))) continue;
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
      await cjs(`window.TailorAutofill.flash(${q(ref)}, true)`, 2000);
      if (op.op === "upload") {
        await pt(op.from === "letter" ? "Uploading cover letter" : "Uploading resume");
        await upload(op, uploadPath, c);
      } else {
        await pt("Clicking: " + name);
        await wait(t.move);
        await pt("Filling: " + name);
        let typed = false;
        if (op.op === "fill" && op.text) {
          typed = (await cjs(`window.TailorAutofill.typeVisibly(${q(ref)}, ${q(String(op.text))}, ${q({ perChar: t.perChar, burst: t.burst })})`, 8000)) === true;
        }
        if (typed) out.filled++;
        else {
          const rep = await cjs(`window.TailorAutofill.applyOpsAsync(${q([op])}, { budgetMs: 8000 })`, 10000);
          out.filled += (rep && rep.filled) || 0;
        }
        await cjs("window.TailorAutofill.tidyMenus()", 4000);    // its list closed before the next field
      }
      await cjs(`window.TailorAutofill.flash(${q(ref)}, false)`, 2000);
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
  out.frames = frames.length;
  return out;
}

// ONE top-to-bottom order over the top page and its frames: a top-page field by its place on the
// page, a frame's field by the frame's place on the page plus the field's place in the frame. A
// frame the top page cannot place (not tagged) comes after the page, in its own order.
async function orderAll(js, ctxs, ctxOf, refs) {
  const keyed = [];
  for (const c of ctxs) {
    const mine = refs.filter((r) => ctxOf(r) === c);
    if (!mine.length) continue;
    const places = await c.js(`window.TailorAutofill.refPlaces(${q(mine)})`, 4000);
    let base = 0;
    if (!c.top) {
      const b = await js(`window.TailorAutofill.frameBox(${q(c.prefix)})`, 2000);
      base = b && Number.isFinite(b.abs) ? b.abs : 1e7;
    }
    for (const p of Array.isArray(places) ? places : []) keyed.push({ ref: p.ref, y: base + p.top, x: p.left || 0 });
  }
  keyed.sort((a, b) => (Math.abs(a.y - b.y) > 4 ? a.y - b.y : a.x - b.x));
  return keyed.map((k) => k.ref);
}

// Is a frame's field in the person's sight on the top page? true / false, or null when it cannot
// be told (the frame is not placed on the top page).
async function frameSight(js, c, ref) {
  const b = await js(`window.TailorAutofill.frameBox(${q(c.prefix)})`, 2000);
  const r = await c.js(`window.TailorAutofill.refRect(${q(ref)})`, 2000);
  if (!b || !r) return null;
  const top = b.top + r.top, bottom = b.top + r.bottom;
  return top >= 60 && bottom <= b.vh - 60 && r.top >= 0 && r.bottom <= b.height;
}
// Tell a frame which part of it the person sees on the top page (in the frame's own view), so its
// own "in view" (the walk, the needs-you Enter flow, Next / Previous) scrolls to a field the top
// page has scrolled away from. false when the frame cannot be placed on the top page.
async function syncSight(js, c) {
  const b = await js(`window.TailorAutofill.frameBox(${q(c.prefix)})`, 2000);
  if (!b) { await c.js("window.TailorAutofill.setSight(null)", 1500); return false; }
  const sight = { top: Math.max(0, -b.top), bottom: Math.max(0, Math.min(b.height, b.vh - b.top)) };
  await c.js(`window.TailorAutofill.setSight(${q(sight)})`, 1500);
  return true;
}
// A frame's field the person must see now (Next / Previous, the end of a run): when the top page
// has it out of sight, the top page scrolls so the field sits in the middle of the window.
async function bringIntoSight(js, c, ref) {
  if (!ref) return false;
  if ((await frameSight(js, c, ref)) !== false) return false;
  const b = await js(`window.TailorAutofill.frameBox(${q(c.prefix)})`, 2000);
  const r = await c.js(`window.TailorAutofill.refRect(${q(ref)})`, 2000);
  if (!b || !r) return false;
  const dy = Math.round(b.top + (r.top + r.bottom) / 2 - b.vh / 2);
  await js(`(window.scrollBy({ top: ${dy}, behavior: "instant" }), true)`, 2000);
  await syncSight(js, c);
  return true;
}
// Wait until the top page stops moving under a frame (a smooth scroll), 1.5 s at most.
async function settledFrame(js, c) {
  let last = null;
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 50));
    const b = await js(`window.TailorAutofill.frameBox(${q(c.prefix)})`, 2000);
    const tp = b ? Math.round(b.top) : null;
    if (last !== null && tp === last) return;
    last = tp;
  }
}

module.exports = { walk, TIMING, orderAll, syncSight, bringIntoSight, frameSight };
