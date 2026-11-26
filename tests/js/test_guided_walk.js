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
    else left.push({ ref: f.ref, label: f.label });              // the walk flags only the required ones
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
  assert(!r.out.needs.some((n) => /Preferred/.test(n)), "an optional box the plan left is never flagged " + JSON.stringify(r.out.needs));
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
  assert(r.rest && r.rest.label === "Choose an answer", "the pointer rests there, saying what to do " + JSON.stringify(r.rest));
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
  // goes there and our value is typed, after one quick reset of what the site put in; the site's "Autofill from resume" box
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
    // one quick reset before the walk, then typed once: never a visible delete, letter by letter
    assert(seen[0] === "" && !seen.some((x) => x && "Wrongname".startsWith(x)) && seen.slice(1).every((x) => "Avery".startsWith(x)), "reset once, then typed " + JSON.stringify(seen.slice(0, 12)));
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

  // ---- (Meriton, 2026-11-26) Enter in the site's own box never sends the form; a script never
  // clicks Submit; the person's own click on Submit still sends it
  {
    const b = await chromium.launch();
    const page = await b.newPage({ viewport: { width: 1000, height: 700 } });
    await page.goto(FIX);
    await page.evaluate(CORE); await page.evaluate(POINTER);
    await page.evaluate(() => { window.__sent = 0; window.__siteEnter = 0;
      document.getElementById("application-form").addEventListener("submit", () => { window.__sent++; });
      document.getElementById("first_name").addEventListener("keydown", (e) => { if (e.key === "Enter") window.__siteEnter++; }); });
    // without the guard: a real Enter in a one-line box sends the form (the browser's own rule)
    await page.click("#first_name"); await page.keyboard.type("Kofi"); await page.keyboard.press("Enter");
    const before = await page.evaluate("window.__sent");
    await page.evaluate("window.TailorAutofill.armSubmitGuard()");
    await page.click("#last_name"); await page.keyboard.type("Example"); await page.keyboard.press("Enter");
    await page.click("#first_name"); await page.keyboard.press("Enter");
    const guarded = await page.evaluate("[window.__sent, window.__siteEnter, window.__sjEnterStopped, document.getElementById('last_name').value]");
    const hint = await page.evaluate("window.TailorPointer.state() && window.TailorPointer.state().label");
    // a script's click on Submit: stopped; the person's own click: goes
    await page.evaluate(() => document.getElementById("submit").click());
    const scripted = await page.evaluate("window.__sent");
    await page.click("#submit");
    const person = await page.evaluate("window.__sent");
    await b.close();
    assert(before === 1, "the browser sends a form on Enter in a one-line box (the Meriton case): " + before);
    assert(guarded[0] === 1 && guarded[2] === 2 && guarded[3] === "Example", "Enter never sends it once guarded " + JSON.stringify(guarded));
    assert(guarded[1] === 2, "the site's own Enter handler still runs " + JSON.stringify(guarded));
    assert(hint === null || hint === "Press Submit yourself when you are ready", "the pointer, when Submit is in view: " + hint);
    assert(scripted === 1, "a script's click on Submit is stopped");
    assert(person === 2, "the person's own click on Submit sends it");
  }

  // ---- (2026-11-26, the owner) the fields that need the person, one after another: Enter moves
  // on from a box with a valid answer and stays on an empty one; a list moves on by itself; a
  // multi-line box keeps Enter for a new line and moves on with its Next chip or Ctrl+Enter; after
  // the last one the pointer is ON Submit. The form is never sent.
  {
    const b = await chromium.launch();
    const page = await b.newPage({ viewport: { width: 1000, height: 700 } });
    await page.setContent(`<form id="f" onsubmit="window.__sent=(window.__sent||0)+1; return false">
      <div><label for="a">Your city *</label><input id="a" type="text" required></div>
      <div><label for="m">Email for updates *</label><input id="m" type="email" required></div>
      <div><label for="c">Shift *</label><select id="c" required><option value="">Select...</option><option>Day</option><option>Night</option></select></div>
      <div><label for="t">Why this role? *</label><textarea id="t" required></textarea></div>
      <div><button type="submit" id="sub">Submit application</button></div></form>`);
    await page.evaluate(CORE); await page.evaluate(POINTER);
    const js = (e) => page.evaluate(`(async () => (${e}))()`);
    await js(`(() => { const T = window.TailorAutofill; T.armSubmitGuard(); for (const f of T.extractForm({ onlyEmpty: true })) T.markNeeds(f.ref); return true; })()`);
    await js("window.TailorAutofill.guideEnd()");
    const lab = () => page.evaluate("window.TailorPointer.state() && window.TailorPointer.state().label");
    const focused = () => page.evaluate("document.activeElement && document.activeElement.id");
    const s0 = [await focused(), await lab()];
    await page.keyboard.press("Enter");                          // empty: stays, says so
    const s1 = [await focused(), await lab()];
    await page.keyboard.type("Springfield"); await page.keyboard.press("Enter");
    await page.waitForTimeout(400);
    const s2 = [await focused(), await lab()];
    await page.keyboard.type("not-an-email"); await page.keyboard.press("Enter");   // invalid: stays
    const s3 = [await focused(), await lab()];
    await page.fill("#m", ""); await page.focus("#m"); await page.keyboard.type("a@example.com"); await page.keyboard.press("Enter");
    await page.waitForTimeout(400);
    const s4 = [await focused(), await lab()];
    await page.selectOption("#c", "Night");                      // a list moves on by itself
    await page.waitForTimeout(1200);
    const s5 = [await focused(), await lab()];
    const chip = await page.evaluate(() => { const h = document.querySelector("[data-sj-glow]"); const c = h && h.shadowRoot.querySelector(".chip"); return c ? !c.hidden : false; });
    await page.keyboard.type("Line one"); await page.keyboard.press("Enter"); await page.keyboard.type("Line two");
    const s6 = [await focused(), await page.evaluate("document.getElementById('t').value")];
    await page.keyboard.press("Control+Enter");
    await page.waitForTimeout(900);
    const end = await page.evaluate(() => { const st = window.TailorPointer.state(); const r = document.getElementById("sub").getBoundingClientRect();
      return { label: st && st.label, on: !!st && st.x >= r.left && st.x <= r.right && st.y >= r.top && st.y <= r.bottom, sent: window.__sent || 0 }; });
    await b.close();
    assert(s0[0] === "a" && s0[1] === "Fill this, then press Enter", "the caret in the first, the words " + JSON.stringify(s0));
    assert(s1[0] === "a" && s1[1] === "This one still needs an answer", "empty: stays and says so " + JSON.stringify(s1));
    assert(s2[0] === "m" && s2[1] === "Fill this, then press Enter", "Enter moves on " + JSON.stringify(s2));
    assert(s3[0] === "m" && s3[1] === "This one still needs an answer", "an invalid answer stays " + JSON.stringify(s3));
    assert(s4[0] === "c" && s4[1] === "Choose an answer", "on to the list " + JSON.stringify(s4));
    assert(s5[0] === "t" && s5[1] === "Fill this, then press Next" && chip, "a choice moves on by itself; the Next chip " + JSON.stringify([s5, chip]));
    assert(s6[0] === "t" && s6[1] === "Line one\nLine two", "Enter is a new line in a multi-line box " + JSON.stringify(s6));
    assert(end.label === "All done. Press Submit when you're happy" && end.on && end.sent === 0, "the end ON Submit, never sent " + JSON.stringify(end));
  }

  // ---- (Vestmark, 2026-11-26, the owner) a suggestion is typed in and left selected ("Suggested: I
  // acknowledge. Press Enter to accept, or type your own"); Enter accepts it; typing replaces it. A
  // background question gets no suggestion. The bar hears the same item the pointer is on.
  {
    const b = await chromium.launch();
    const page = await b.newPage({ viewport: { width: 1000, height: 700 } });
    const told = [];
    page.on("console", (m) => { const t = m.text(); if (t.startsWith("__sj_needs__")) told.push(JSON.parse(t.slice(12))); });
    await page.setContent(`<form id="f" onsubmit="window.__sent=(window.__sent||0)+1; return false">
      <div><label for="k">California law requires that we provide you this notice about the collection and use of your personal information under the California Consumer Privacy Act (the CCPA). *</label><input id="k" type="text" required></div>
      <div><label for="n">Nickname to print on your badge *</label><input id="n" type="text" required></div>
      <div><label for="c">Have you ever been convicted of a felony? *</label><select id="c" required><option value="">Select...</option><option>Yes</option><option>No</option></select></div>
      <div><button type="submit" id="sub">Submit application</button></div></form>`);
    await page.evaluate(CORE); await page.evaluate(POINTER);
    const js = (e) => page.evaluate(`(async () => (${e}))()`);
    await js(`(() => { const T = window.TailorAutofill; T.armSubmitGuard(); const fs = T.extractForm({ onlyEmpty: true });
      T.markNeeds(fs[0].ref, "I acknowledge"); T.markNeeds(fs[1].ref, "Pat"); T.markNeeds(fs[2].ref, ""); return true; })()`);
    const end = await js("window.TailorAutofill.guideEnd()");
    const lab = () => page.evaluate("window.TailorPointer.state() && window.TailorPointer.state().label");
    const sel = () => page.evaluate("(() => { const e = document.activeElement; return [e.id, e.value, e.selectionStart, e.selectionEnd]; })()");
    const a0 = [await lab(), await sel()];
    await page.keyboard.press("Enter");                                   // accepts the suggestion
    await page.waitForTimeout(900);
    const a1 = [await lab(), await sel()];
    await page.keyboard.type("Sam"); await page.keyboard.press("Enter");  // typing replaces it
    await page.waitForTimeout(500);
    const a2 = [await lab(), await page.evaluate("[document.getElementById('n').value, document.activeElement.id]")];
    const sent = await page.evaluate("window.__sent || 0");
    await b.close();
    assert(end.label === "the privacy notice", "the item's plain name " + JSON.stringify(end));
    assert(a0[0] === "Suggested: I acknowledge. Press Enter to accept, or type your own" && a0[1][0] === "k" && a0[1][1] === "I acknowledge"
           && a0[1][2] === 0 && a0[1][3] === "I acknowledge".length, "typed in and selected " + JSON.stringify(a0));
    assert(a1[1][0] === "n" && a1[1][1] === "Pat" && /^Suggested: Pat\./.test(a1[0]), "Enter accepted it, on to the next " + JSON.stringify(a1));
    assert(a2[1][0] === "Sam" && a2[1][1] === "c" && a2[0] === "Choose an answer", "typing replaced it; the felony question has no suggestion " + JSON.stringify(a2));
    assert(sent === 0, "the form was never sent");
    const names = told.filter((d) => d.n).map((d) => d.label);
    assert(JSON.stringify(names) === JSON.stringify(["the privacy notice", "Nickname to print on your badge", "Have you ever been convicted of a felony?"]),
           "the bar hears each item the pointer is on " + JSON.stringify(told));
  }

  // ---- (Vestmark, 2026-11-26) a box left for the person is not walked again on the same page,
  // unless the caller asks for those boxes again (a rule may answer one now)
  {
    const b = await chromium.launch();
    const page = await b.newPage({ viewport: { width: 1000, height: 700 } });
    await page.goto(FIX);
    await page.evaluate(CORE); await page.evaluate(POINTER);
    const js = (e) => page.evaluate(`(async () => (${e}))()`);
    const done = new Set(), asked = new Set();
    const quick = () => new Promise((res) => setTimeout(res, 5));
    const first = await guided.walk({ js, done, asked, plan: async (f) => planFor(f, {}), upload: async () => ({ n: 0, seen: [] }), wait: quick });
    const again = await guided.walk({ js, done, asked, plan: async (f) => planFor(f, {}), upload: async () => ({ n: 0, seen: [] }), wait: quick });
    // a rule now answers the fugitive question (here: the plan knows it), and the caller asks again
    const answer = async (f) => { const p = planFor(f, {}); for (const x of f) if (/fugitive/i.test(x.label)) p.ops.push({ ref: x.ref, op: "select", text: "No" }); p.left = []; return p; };
    const retry = await guided.walk({ js, done, asked: new Set(), plan: answer, upload: async () => ({ n: 0, seen: [] }), wait: quick });
    const v = await page.evaluate("window.fxValues()");
    await b.close();
    assert(first.needs.length === 1 && again.needs.length === 0 && again.walked === 0, "not walked again " + JSON.stringify([first.needs, again]));
    assert(retry.walked === 1 && v.fugitive === "No", "tried again when asked " + JSON.stringify([retry, v]));
  }

  // ---- reduced motion: no glide, still in order ---------------------------------------------
  const rr = await run({ reduced: true });
  const fm = rr.moves.filter((m) => /^(Clicking|Needs you)/.test(m.label));
  for (let i = 1; i < fm.length; i++) assert(fm[i].top >= fm[i - 1].top, "reduced motion keeps the order");
  assert(rr.v.veteran === "I am not a protected veteran" && rr.v.open === 0, "reduced motion fills the same");
  console.log("ok");
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
