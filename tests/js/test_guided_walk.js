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
  // the glow: rings in their own overlay, the current one brighter, gone once a field is answered
  const glow = await page.evaluate(() => { const h = document.querySelector("[data-sj-glow]"); const rs = h ? [...h.shadowRoot.querySelectorAll(".r")] : [];
    return { n: rs.length, cur: rs.filter((r) => r.classList.contains("cur")).length, layout: getComputedStyle(h || document.body).position }; });
  // the person answers it (here through the engine's own pick, as a person's click would)
  await js("(async () => { const T = window.TailorAutofill; const ref = T.needsList()[0]; return T.applyOpsAsync([{ ref, op: 'select', text: 'No' }]); })()");
  await page.waitForTimeout(800);
  const after = await page.evaluate(() => { const h = document.querySelector("[data-sj-glow]"); return h ? h.shadowRoot.querySelectorAll(".r").length : 0; });
  const outlined = glow.n;
  const submitted = await page.evaluate("document.getElementById('submit').dataset.clicked || ''");
  await b.close();
  return { out, v, moves, end, rest, steps, outlined, submitted, waits, glow, after };
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
  assert(r.glow.n === 1 && r.glow.cur === 1 && r.glow.layout === "fixed", "a glow ring, the current one bright, in an overlay " + JSON.stringify(r.glow));
  assert(r.after === 0, "the ring goes once the field is answered: " + r.after);
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
    const tip = await page.evaluate(() => { const r = document.getElementById("submit").getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; });
    const outlined = await page.evaluate("!!document.querySelector('#submit.tailor-submit-hint')");
    const clicked = await page.evaluate("document.getElementById('submit').dataset.clicked || ''");
    await b.close();
    assert(end.where === "submit" && st && st.label === "Your turn: check and submit", "rests on Submit " + JSON.stringify([end, st]));
    assert(st.side === "on" && st.x >= tip.left && st.x <= tip.right && st.y >= tip.top && st.y <= tip.bottom, "the tip ON the button " + JSON.stringify([st, tip]));
    assert(outlined && !clicked, "Submit outlined, never pressed");
  }

  // ---- (Lam, 2026-11-26, the owner) a site window over the form: NOTHING is filled under it. The
  // walk stops before touching the page and says so; the window is read in sight ("Reading:
  // <title>", its text scrolled) and acknowledged; then the walk starts from the top. A window that
  // opens in the middle stops the walk at once, and the next walk carries on where it stopped.
  {
    const b = await chromium.launch();
    const page = await b.newPage({ viewport: { width: 1000, height: 700 } });
    await page.goto(FIX);
    await page.evaluate(CORE); await page.evaluate(POINTER);
    const openDpa = () => page.evaluate(() => {
      const back = document.createElement("div");
      back.id = "dpa";
      back.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1000;display:flex;align-items:center;justify-content:center";
      back.innerHTML = '<div role="dialog" aria-modal="true" aria-labelledby="dpaH" style="background:#fff;width:520px;padding:20px">'
        + '<h2 id="dpaH">Data Privacy Agreement</h2><div id="dpaBody" style="height:120px;overflow-y:auto"><p style="height:900px">Made-up notice text.</p></div>'
        + '<button id="ack" type="button">I Acknowledge</button></div>';
      document.body.appendChild(back);
      document.getElementById("ack").addEventListener("click", () => back.remove());
    });
    await openDpa();
    await page.evaluate(() => { window.__labels = []; const show = window.TailorPointer.at;
      window.TailorPointer.at = (el, label, opt) => { const ok = show(el, label, opt); const st = window.TailorPointer.state(); window.__labels.push(st ? st.label : ""); return ok; }; });
    const js = (e) => page.evaluate(`(async () => (${e}))()`);
    const done = new Set();
    const walkIt = () => guided.walk({ js, done, plan: async (f) => planFor(f, {}), upload: async () => ({ n: 0, seen: [] }), wait: () => new Promise((res) => setTimeout(res, 10)) });
    const first = await walkIt();
    const v1 = await page.evaluate("window.fxValues()");
    assert(first.modal === "Data Privacy Agreement" && first.walked === 0 && first.filled === 0, "stopped before touching the page " + JSON.stringify(first));
    assert(!v1.first && !v1.email && !v1.adult, "nothing filled under the window " + JSON.stringify(v1));
    // the window read in sight: the pointer on its title, the text scrolled
    const read = await js("window.TailorAutofill.readModal(1500)");
    const scrolled = await page.evaluate("document.getElementById('dpaBody').scrollTop");
    const label = await page.evaluate("window.TailorPointer.state() && window.TailorPointer.state().label");
    assert(read && read.title === "Data Privacy Agreement" && label === "Reading: Data Privacy Agreement" && scrolled > 50, "read: " + JSON.stringify([read, label, scrolled]));
    const ackFree = await page.evaluate(() => !window.TailorPointer.coverOf(document.getElementById("ack")));
    assert(ackFree, "the window's own button is not covered: the click lands there");
    await page.click("#ack");
    // a window that opens after the second field: the walk stops there
    await page.evaluate(() => { const em = document.getElementById("email"); em.addEventListener("focus", () => window.__openAgain && window.__openAgain(), { once: true }); });
    await page.exposeFunction("__openAgainHost", () => true);
    await page.evaluate(() => { window.__openAgain = () => { const back = document.createElement("div"); back.id = "dpa2";
      back.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1000";
      back.innerHTML = '<div role="dialog" aria-modal="true" style="background:#fff;width:400px;margin:100px auto;padding:20px"><h2>Before you go on</h2><button id="ok2">OK</button></div>';
      document.body.appendChild(back); document.getElementById("ok2").addEventListener("click", () => back.remove()); }; });
    const second = await walkIt();
    const v2 = await page.evaluate("window.fxValues()");
    assert(second.modal === "Before you go on", "a window opening mid-walk stops it " + JSON.stringify(second));
    assert(v2.first === "Avery" && !v2.adult, "the fields above were filled, nothing after " + JSON.stringify(v2));
    await page.click("#ok2");
    const third = await walkIt();
    const v3 = await page.evaluate("window.fxValues()");
    const labels = await page.evaluate("window.__labels");
    await b.close();
    assert(!third.modal && v3.adult === "Yes" && v3.veteran === "I am not a protected veteran", "carried on to the end " + JSON.stringify([third, v3]));
    assert(third.order[0] !== second.order[0], "never the same box twice");
    const fillLabels = labels.filter((l) => /^(Filling|Clicking)/.test(l));
    assert(fillLabels.length >= 4 && !labels.some((l) => /^Checking/.test(l)), "every box filled in sight " + JSON.stringify(labels));
  }

  // ---- (2026-11-26, the owner) a box the site already filled is filled the same way: the pointer
  // goes there, it is cleared in sight and our value typed; the site's "Autofill from resume" box
  // is never offered for the resume
  {
    const b = await chromium.launch();
    const page = await b.newPage({ viewport: { width: 1000, height: 700 } });
    await page.goto(FIX);
    await page.evaluate(CORE); await page.evaluate(POINTER);
    await page.evaluate(() => {
      document.getElementById("first_name").value = "Wrongname";
      const pane = document.createElement("div");
      pane.innerHTML = '<div class="autofill-pane"><p>Autofill from resume</p><p>Upload your resume here to autofill key application fields.</p><input type="file" id="parse"></div>';
      document.body.prepend(pane);
      const f = document.createElement("div"); f.className = "field";
      f.innerHTML = '<label for="resume">Resume *</label><input type="file" id="resume" name="resume">';
      document.getElementById("application-form").appendChild(f);
      window.__seen = [];
      const el = document.getElementById("first_name");
      el.addEventListener("input", () => window.__seen.push(el.value));
    });
    const js = (e) => page.evaluate(`(async () => (${e}))()`);
    await guided.walk({ js, plan: async (f) => planFor(f, {}), upload: async () => ({ n: 0, seen: [] }), wait: () => new Promise((res) => setTimeout(res, 10)) });
    const seen = await page.evaluate("window.__seen");
    const v = await page.evaluate("window.fxValues()");
    const boxes = await page.evaluate(() => ({ parse: window.TailorAutofill.siteParseBox(document.getElementById("parse")),
      plain: window.TailorAutofill.siteParseBox(document.getElementById("resume")),
      listed: window.TailorAutofill.extractForm({}).filter((f) => f.type === "file").map((f) => f.label) }));
    await b.close();
    assert(v.first === "Avery", "our value in the end " + JSON.stringify(v));
    assert(seen.some((x) => x && "Wrongname".startsWith(x) && x.length < 9) && seen.includes(""), "cleared in sight first " + JSON.stringify(seen.slice(0, 12)));
    assert(boxes.parse === true && boxes.plain === false && boxes.listed.length === 1 && /Resume/.test(boxes.listed[0]), "the parse box is never used " + JSON.stringify(boxes));
  }

  // ---- (Lam, 2026-11-26) the site's confirmation in a window over its profile page counts as
  // submitted (the run and its bar end); the same words in the open form's text never do
  {
    const b = await chromium.launch();
    const page = await b.newPage({ viewport: { width: 1000, height: 700 } });
    await page.goto(FIX);
    await page.evaluate(CORE);
    const seen = () => page.evaluate("window.TailorAutofill.detectSuccess(null, 'https://careers.example.com/careers/apply?pid=1')");
    const before = await seen();
    await page.evaluate(() => { const p = document.createElement("p"); p.textContent = "Thank you for your application in advance."; document.body.prepend(p);
      const f = document.createElement("input"); f.type = "file"; document.getElementById("application-form").appendChild(f); });
    const formWords = await seen();
    await page.evaluate(() => { const d = document.createElement("div"); d.setAttribute("role", "dialog"); d.style.cssText = "position:fixed;inset:20%;background:#fff";
      d.innerHTML = "<h2>Thank you for your interest</h2>"; document.body.appendChild(d); });
    const interest = await seen();
    await page.evaluate(() => { const d = document.createElement("div"); d.setAttribute("role", "dialog"); d.style.cssText = "position:fixed;inset:20%;background:#fff";
      d.innerHTML = "<h2>Thank you for your application, Avery!</h2><button>Continue</button>"; document.body.appendChild(d); });
    const thanks = await seen();
    await b.close();
    assert(before === false && formWords === false && interest === false, "not before the submit " + JSON.stringify([before, formWords, interest]));
    assert(thanks === true, "the confirmation window counts as submitted");
  }

  // ---- reduced motion: no glide, still in order ---------------------------------------------
  const rr = await run({ reduced: true });
  const fm = rr.moves.filter((m) => /^(Clicking|Needs you)/.test(m.label));
  for (let i = 1; i < fm.length; i++) assert(fm[i].top >= fm[i - 1].top, "reduced motion keeps the order");
  assert(rr.v.veteran === "I am not a protected veteran" && rr.v.open === 0, "reduced motion fills the same");
  console.log("ok");
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
