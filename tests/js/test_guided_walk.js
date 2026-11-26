// Guided fill (shell-electron/guided.js) in a real headless Chromium, on a made-up form taller than
// the window (tests/fixtures/forms/guided_form.html). Run by tests/test_guided_fill.py.
//   * strictly top to bottom, one field after another; the page scrolls to each field and the
//     pointer is ON the field (in view) when it is filled;
//   * text is typed a few characters at a time; a choice is picked and its list closed: no menu is
//     left open at the end (the live Axon bug);
//   * a question the person must answer gets "Needs you" and an outline, never filled;
//   * the end rests on the first field that needs the person, else on Submit ("Your turn"), never
//     pressed; Next / Previous step through those fields; reduced motion still walks in order.
"use strict";
const path = require("path"), fs = require("fs");
const PW = process.env.SJ_PLAYWRIGHT_DIR || path.join(process.env.HOME, "sponsorjobs/shell-electron/node_modules/playwright");
const { chromium } = require(PW);
const ROOT = path.join(__dirname, "..", "..");
const CORE = fs.readFileSync(path.join(ROOT, "extension", "autofill_core.js"), "utf8");
const POINTER = fs.readFileSync(path.join(ROOT, "extension", "pointer_overlay.js"), "utf8");
const guided = require(path.join(ROOT, "shell-electron", "guided.js"));
const FIX = "file://" + path.join(ROOT, "tests", "fixtures", "forms", "guided_form.html");
const assert = (c, m) => { if (!c) throw new Error(m); };

// The plan as the engine route would make it: by label (made-up values only).
function planFor(fields, o) {
  const want = { "First Name": "Avery", "Last Name": "Example", "Email": "apply.person@example.com",
                 "Are you at least 18 years of age?": "Yes", "Veteran Status": o && o.noVeteran ? null : "I am not a protected veteran" };
  const ops = [], left = [];
  for (const f of fields) {
    const lab = String(f.label || "").replace(/\s*\*\s*$/, "");
    const v = want[lab];
    if (v) ops.push({ ref: f.ref, op: f.type === "text" || f.type === "email" ? "fill" : "select", text: v });
    else if (f.required) left.push({ ref: f.ref, label: f.label });
  }
  return { ok: true, ops, left, cost: 0.001 };
}

async function run(o) {
  const b = await chromium.launch();
  const page = await b.newPage({ viewport: { width: 1000, height: 700 } });
  if (o.reduced) await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(FIX);
  await page.evaluate(CORE); await page.evaluate(POINTER);
  // record every pointer move: its label, the field's words, and whether the field was in view
  await page.evaluate(() => {
    window.__moves = [];
    const at = window.TailorPointer.at;
    window.TailorPointer.at = (el, label, opt) => {
      const r = el.getBoundingClientRect();
      const f = el.closest(".field"); const words = f && f.querySelector("label") ? f.querySelector("label").textContent.replace(/\s*\*\s*$/, "").trim() : "";
      window.__moves.push({ label, top: r.top + window.scrollY, inView: r.top >= 0 && r.bottom <= innerHeight, words });
      return at(el, label, opt);
    };
  });
  const js = (e) => page.evaluate(`(async () => (${e}))()`);
  const waits = [];
  const out = await guided.walk({ js, plan: async (f) => planFor(f, o), upload: async () => ({ n: 0, seen: [] }),
    wait: (ms) => { waits.push(ms); return new Promise((r) => setTimeout(r, Math.min(ms, 30))); }, timing: o.timing });
  await page.waitForTimeout(500);
  const v = await page.evaluate("window.fxValues()");
  const moves = await page.evaluate("window.__moves");
  const end = await js("window.TailorAutofill.guideEnd()");
  await page.waitForTimeout(600);
  const rest = await page.evaluate("window.TailorPointer.state()");
  const steps = [await js("window.TailorAutofill.needsStep(1)"), await js("window.TailorAutofill.needsStep(-1)")];
  const outlined = await page.evaluate("document.querySelectorAll('.tailor-needs').length");
  const submitted = await page.evaluate("document.getElementById('submit').dataset.clicked || ''");
  await b.close();
  return { out, v, moves, end, rest, steps, outlined, submitted, waits };
}

(async () => {
  // ---- the walk ------------------------------------------------------------------------------
  const r = await run({});
  assert(r.v.first === "Avery" && r.v.last === "Example" && r.v.email === "apply.person@example.com", "text filled " + JSON.stringify(r.v));
  assert(r.v.adult === "Yes" && r.v.veteran === "I am not a protected veteran", "choices picked " + JSON.stringify(r.v));
  assert(r.v.fugitive == null, "the person's question is never filled");
  assert(r.v.open === 0, "no list left open: " + r.v.open);
  // top to bottom: the fields' page positions only ever grow
  const fieldMoves = r.moves.filter((m) => /^(Clicking|Needs you)/.test(m.label));
  for (let i = 1; i < fieldMoves.length; i++) assert(fieldMoves[i].top >= fieldMoves[i - 1].top, "in page order: " + JSON.stringify(fieldMoves));
  assert(fieldMoves.length === 6, "every field visited once: " + fieldMoves.map((m) => m.label).join(" | "));
  // the pointer is on the field, in view, when it fills it; the page scrolled to the far ones
  assert(r.moves.filter((m) => /^Filling/.test(m.label)).every((m) => m.inView), "in view when filled " + JSON.stringify(r.moves));
  assert(r.moves.some((m) => m.label === "Scrolling"), "scrolled to the far fields");
  // the label names the field the pointer is on (never another one)
  for (const m of r.moves.filter((x) => /^(Clicking|Filling|Needs you): /.test(x.label))) {
    assert(m.words && m.label.endsWith(": " + m.words), "the label names this field: " + JSON.stringify(m));
  }
  assert(r.moves.some((m) => m.label === "Needs you: Are you a fugitive from justice?"), "the person's question is shown: " + r.moves.map((m) => m.label).join(" | "));
  assert(r.out.needs.length === 1 && r.out.filled === 5, "counts " + JSON.stringify(r.out));
  // paced: every field took at least the minimum (the waits top each field up to it)
  assert(r.waits.filter((w) => w > 0).length >= 5, "paced " + JSON.stringify(r.waits));
  // the end: on the first field that needs the person, outlined, resting (never hidden)
  assert(r.end.where === "needs" && r.end.n === 1 && r.outlined === 1, "ends on the person's field " + JSON.stringify(r.end));
  assert(r.rest && /^Needs you/.test(r.rest.label), "the pointer rests there " + JSON.stringify(r.rest));
  assert(r.steps[0].n === 1 && r.steps[0].i === 0, "Next steps through them " + JSON.stringify(r.steps));
  assert(!r.submitted, "never submitted");

  // ---- nothing left for the person: the end rests on Submit, outlined, never pressed -----------
  {
    const b = await chromium.launch();
    const page = await b.newPage({ viewport: { width: 1000, height: 700 } });
    await page.goto(FIX);
    await page.evaluate(CORE); await page.evaluate(POINTER);
    await page.evaluate(() => { document.getElementById("w2").remove(); document.getElementById("submit").addEventListener("click", () => { document.getElementById("submit").dataset.clicked = "1"; }); });
    const js = (e) => page.evaluate(`(async () => (${e}))()`);
    await guided.walk({ js, plan: async (f) => planFor(f, {}), upload: async () => ({ n: 0, seen: [] }), wait: () => new Promise((res) => setTimeout(res, 20)) });
    const end = await js("window.TailorAutofill.guideEnd()");
    await page.waitForTimeout(700);
    const st = await page.evaluate("window.TailorPointer.state()");
    const outlined = await page.evaluate("!!document.querySelector('#submit.tailor-submit-hint')");
    const clicked = await page.evaluate("document.getElementById('submit').dataset.clicked || ''");
    await b.close();
    assert(end.where === "submit" && st && st.label === "Your turn: check and submit", "rests on Submit " + JSON.stringify([end, st]));
    assert(outlined && !clicked, "Submit outlined, never pressed");
  }

  // ---- reduced motion: no glide, still in order ---------------------------------------------
  const rr = await run({ reduced: true });
  const fm = rr.moves.filter((m) => /^(Clicking|Needs you)/.test(m.label));
  for (let i = 1; i < fm.length; i++) assert(fm[i].top >= fm[i - 1].top, "reduced motion keeps the order");
  assert(rr.v.veteran === "I am not a protected veteran" && rr.v.open === 0, "reduced motion fills the same");
  console.log("ok");
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
