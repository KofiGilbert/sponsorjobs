// The REPLAY SUITE (2026-11-26): the guided Autofill (shell-electron/guided.js over the real fill
// engine, extension/autofill_core.js) run in real headless Chromium against offline copies of
// the day's application forms, with a made-up test profile. Never a real site: two local static
// servers (the second is the "other origin" a framed form comes from). Run by
// tests/test_replay_forms.py; SJ_REPLAY_ONLY=<name> runs one.
//
// For every form, after the walk and the shell's own end (outline what is still required, tidy
// the lists, guideEnd):
//   1. every REQUIRED question (read from the page as a person sees it: "*", a required mark,
//      required / aria-required) is filled or outlined as needs-you;
//   2. nothing was filled out of sight (the pointer was on the field, in view);
//   3. nothing was submitted (the form's submit count stays 0, no Next pressed);
//   4. no list is left open;
//   5. the end matches the final check: "check and submit" only when nothing required is empty,
//      else it names the needs-you fields (the Instacart Canada bug: a required box left empty
//      while the bubble said "check and submit").
// And for the confirmation cases the sent detector (shell-electron/sent_detect.js) fires once.
"use strict";
const path = require("path"), fs = require("fs"), http = require("http");
const ROOT = path.join(__dirname, "..", "..");
function findPlaywright() {
  for (const c of [process.env.SJ_PLAYWRIGHT_DIR, path.join(ROOT, "shell-electron", "node_modules", "playwright"),
                   path.join(process.env.HOME || "", "sponsorjobs", "shell-electron", "node_modules", "playwright")]) {
    if (c && fs.existsSync(path.join(c, "package.json"))) return require(c);
  }
  throw new Error("playwright not found");
}
const { chromium } = findPlaywright();
const CORE = fs.readFileSync(path.join(ROOT, "extension", "autofill_core.js"), "utf8");
const POINTER = fs.readFileSync(path.join(ROOT, "extension", "pointer_overlay.js"), "utf8");
const guided = require(path.join(ROOT, "shell-electron", "guided.js"));
const sent = require(path.join(ROOT, "shell-electron", "sent_detect.js"));
const FORMS = path.join(ROOT, "tests", "fixtures", "forms");
const RESUME = path.join(FORMS, "replay", "Avery_Example_Resume.pdf");

// ------------------------------------------------------------------------------ test profile
// Made up. What a saved profile answers; anything else is the person's (left, outlined if required).
const PROFILE = [
  [/^first name|^legal first name/i, "Avery"], [/^last name|^legal last name|surname/i, "Example"],
  [/^(full )?name\b/i, "Avery Example"], [/e-?mail/i, "avery.example@example.com"],
  [/phone/i, "5551002000"], [/linkedin/i, "https://www.linkedin.com/in/avery-example"],
  [/location \(city\)|current location|what city|city/i, "Chicago"],
  [/how did you hear/i, "LinkedIn"], [/at least 18|18 years/i, "Yes"],
  [/authori[sz]ed to work in the united states|legally authori[sz]ed.*united states/i, "Yes"],
  [/sponsorship|immigration/i, "Yes"],
  [/(current or former|previously|worked for|worked here|employee of)/i, "No"],
  [/veteran/i, "I am not a protected veteran"], [/gender|race|hispanic|disability/i, "Decline To Self Identify"],
];
function answerFor(label) {
  const lab = String(label || "").replace(/[*✱]\s*$/, "").trim();
  for (const [re, v] of PROFILE) if (re.test(lab)) return v;
  return null;
}
// The plan as the engine route makes it, from the profile only: ops for what it answers, the rest left.
function planFor(fields) {
  const ops = [], left = [];
  for (const f of fields) {
    if (f.type === "file") {
      if (/resume|cv/i.test(f.label || "") || /resume/i.test(f.name || "")) { ops.push({ ref: f.ref, op: "upload", path: RESUME, from: "resume" }); continue; }
      left.push({ ref: f.ref, label: f.label }); continue;
    }
    const v = answerFor(f.label);
    if (v == null) { left.push({ ref: f.ref, label: f.label }); continue; }
    const text = /^(text|email|tel|textarea|url|number)$/.test(f.type || "") ? "fill" : "select";
    if (text === "select" && Array.isArray(f.options) && f.options.length) {
      const o = f.options.find((x) => String(x).toLowerCase() === v.toLowerCase()) || f.options.find((x) => String(x).toLowerCase().startsWith(v.toLowerCase()));
      if (!o) { left.push({ ref: f.ref, label: f.label }); continue; }
      ops.push({ ref: f.ref, op: "select", text: o });
    } else ops.push({ ref: f.ref, op: text, text: v });
  }
  return { ok: true, ops, left, cost: 0 };
}

// ------------------------------------------------------------------------- in-page checks
// Read in the page as a person sees it, independent of the engine: each question, whether it
// is required, whether it holds an answer, and whether the engine outlined it as needs-you.
const TRUTH_JS = `(() => {
  const T = window.TailorAutofill;
  const needs = new Set(T && T.needsList ? T.needsList() : []);
  const shown = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return (r.width > 0 && r.height > 0) || e.type === "file" || s.position === "absolute"; };
  const boxes = [...document.querySelectorAll(".field, fieldset, .fieldEntry, .application-question, .fx-q, [class*='field-3'], [data-automation-id^='formField'], .iCIMS_TableRow")]
    .filter((b) => !b.parentElement || !b.parentElement.closest(".field, fieldset, .fieldEntry, .application-question, .fx-q, [class*='field-3'], [data-automation-id^='formField'], .iCIMS_TableRow"));
  const out = [];
  for (const b of boxes) {
    const ctrls = [...b.querySelectorAll("input:not([type=hidden]):not([type=submit]):not([type=button]):not([aria-hidden=true]), textarea, select, [role=combobox], [role=radio], [role=checkbox], button[aria-haspopup=listbox]")].filter(shown);
    if (!ctrls.length) continue;
    const head = b.querySelector("legend, label, .application-label, .label, .heading");
    const words = String((head && head.textContent) || "").replace(/\\s+/g, " ").trim();
    const req = /[*✱]/.test(words) || !!b.querySelector(".required, [class*='required']") || ctrls.some((c) => c.required || c.getAttribute("aria-required") === "true");
    if (!req) continue;
    let filled = false;
    for (const c of ctrls) {
      const t = (c.type || "").toLowerCase();
      if (t === "checkbox" || t === "radio") { if (c.checked) filled = true; }
      else if (t === "file") { if (c.files && c.files.length) filled = true; }
      else if (c.getAttribute("role") === "combobox") { const box = c.closest(".select__control, [class*='select-dropdown'], .select-shell") || b; if (box.querySelector(".select__single-value") || String(c.value || "").trim()) filled = true; }
      else if (c.getAttribute("role") === "radio" || c.getAttribute("role") === "checkbox") { if (c.getAttribute("aria-checked") === "true") filled = true; }
      else if (c.tagName === "SELECT") { if (c.selectedIndex > 0) filled = true; }
      else if (c.tagName === "BUTTON") { if (!/^select one$/i.test(String(c.textContent || "").trim())) filled = true; }
      else if (String(c.value || "").trim()) filled = true;
    }
    if (!filled && b.querySelector(".select__single-value")) filled = true;
    const outlined = ctrls.some((c) => needs.has(c.getAttribute("data-tailor-ref"))) || [...b.querySelectorAll("[data-tailor-ref]")].some((c) => needs.has(c.getAttribute("data-tailor-ref")));
    out.push({ q: words.slice(0, 90), filled, outlined });
  }
  const open = [...document.querySelectorAll("[role=listbox], .select__menu, [aria-expanded=true]")].filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== "hidden"; });
  return { required: out, open: open.length, submits: Number(window.fxSubmits || 0) };
})()`;

// The shell's own end of an Autofill (main.js runAutoApplyOnPane after the walk, then guideEnd).
const END_JS = `(async () => { const T = window.TailorAutofill;
  for (const f of T.extractForm({ onlyEmpty: true })) if (f.required) T.markNeeds(f.ref);
  await T.tidyMenus(); return T.guideEnd(); })()`;

function serve(dir) {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      const u = new URL(req.url, "http://x");
      const f = path.join(dir, path.normalize(decodeURIComponent(u.pathname)).replace(/^[/\\]+/, ""));
      if (!f.startsWith(dir) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); res.end(); return; }
      const type = f.endsWith(".html") ? "text/html" : f.endsWith(".js") ? "text/javascript" : f.endsWith(".css") ? "text/css"
                 : f.endsWith(".json") ? "application/json" : "application/octet-stream";
      res.writeHead(200, { "Content-Type": type + "; charset=utf-8", "Access-Control-Allow-Origin": "*" });
      fs.createReadStream(f).pipe(res);
    });
    srv.listen(0, "127.0.0.1", () => resolve(srv));
  });
}

// --------------------------------------------------------------------------------- scenarios
const GH = (q, more) => Object.assign({ page: "replay/greenhouse.html?q=" + q }, more || {});
const SCENARIOS = [
  Object.assign({ name: "greenhouse_stripe" }, GH("stripe")),
  Object.assign({ name: "greenhouse_axon" }, GH("axon")),
  Object.assign({ name: "greenhouse_meriton" }, GH("meriton")),
  Object.assign({ name: "greenhouse_vestmark" }, GH("vestmark")),
  { name: "greenhouse_instacart_frame", page: "replay/instacart_outer.html?b={B}", frame: true, embedConfirm: true },
  { name: "ashby_applied_compute", page: "ashby_application.html" },
  { name: "lever_crosscountry", page: "lever_apply.html" },
  { name: "phenom_lam_privacy", page: "phenom_lam.html", ackModal: "#confirmUploadResume" },
  { name: "workday_uline", page: "workday_experience.html" },
  { name: "icims_email_step", page: "replay/icims_email.html", signin: true },
  Object.assign({ name: "greenhouse_confirmation" }, GH("stripe", { confirm: true, page: "replay/greenhouse.html?q=stripe&confirm=1" })),
  // The checks themselves, proved on a planted bug: the Instacart Canada miss (a required box left
  // empty while the bubble said "check and submit") must be caught, never pass quietly.
  { name: "planted_check_and_submit_bug", page: "replay/greenhouse.html?q=instacart",
    sabotage: "window.TailorAutofill.markNeeds = () => false", expectFail: /says "check and submit" with \d+ required question/ },
];

async function runOne(browser, A, B, sc) {
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  const net = [];
  // at the response's headers, as the shell's Network.responseReceived (a reload right after may
  // cancel the body, as Instacart's frame did)
  page.on("response", (r) => {
    try { const req = r.request(); net.push({ method: req.method(), url: r.url(), status: r.status(), type: req.resourceType() === "fetch" ? "Fetch" : req.resourceType() }); } catch (e) { /* gone */ }
  });
  try {
    await page.goto(A + "/" + sc.page.replace("{B}", String(B)));
    await page.waitForLoadState("load");
    let frame = page.mainFrame();
    if (sc.frame) {
      await page.waitForFunction(() => document.querySelector("iframe"));
      for (let i = 0; i < 50 && page.frames().length < 2; i++) await page.waitForTimeout(100);
      frame = page.frames().find((f) => f !== page.mainFrame());
    }
    await frame.waitForFunction("document.readyState === 'complete' && (window.__fxReady || !document.querySelector('script[src*=\"widgets\"]') || !location.pathname.includes('replay/greenhouse'))", null, { timeout: 10000 });
    await page.waitForTimeout(200);
    await frame.evaluate(CORE); await frame.evaluate(POINTER);
    if (sc.sabotage) await frame.evaluate(sc.sabotage);
    await frame.evaluate(() => {
      window.__moves = [];
      const at = window.TailorPointer.at;
      window.TailorPointer.at = (el, label, opt) => { const r = el.getBoundingClientRect();
        window.__moves.push({ label, inView: r.bottom > 0 && r.top < innerHeight }); return at(el, label, opt); };
    });
    const js = (e) => frame.evaluate(`(async () => (${e}))()`);
    const upload = async (refs, pathOf) => {
      let n = 0;
      for (const ref of refs) {
        const h = await frame.$(`[data-tailor-ref="${ref}"]`);
        if (h && pathOf(ref)) { await h.setInputFiles(pathOf(ref)); n++; }
      }
      return { n, seen: [] };
    };
    const quick = { min: 0, move: 0, scroll: 0, needs: 0, perChar: 0 };
    const wait = (ms) => new Promise((r) => setTimeout(r, Math.min(ms, 20)));
    const done = new Set(), asked = new Set();
    let out = await guided.walk({ js, plan: async (f) => planFor(f), upload, wait, timing: quick, done, asked });
    let modal = out.modal || "";
    if (modal && sc.ackModal) {
      await frame.click(sc.ackModal);               // the person reads the site's notice and acknowledges it
      await page.waitForTimeout(300);
      const more = await guided.walk({ js, plan: async (f) => planFor(f), upload, wait, timing: quick, done, asked });
      out = { filled: out.filled + more.filled, needs: out.needs.concat(more.needs), modal: more.modal };
    }
    const end = await js(END_JS);
    await page.waitForTimeout(300);
    const truth = await frame.evaluate(TRUTH_JS);
    const moves = await frame.evaluate("window.__moves");
    const needsOn = await frame.evaluate(`window.TailorAutofill.needsList().map((r) => { const e = document.querySelector('[data-tailor-ref="' + r + '"]');
      return e ? (e.id || e.name || e.getAttribute("aria-label") || e.tagName) : r; })`);
    const res = { name: sc.name, out: { filled: out.filled, needs: out.needs.length, modal }, end, truth,
                  needsOn, outOfSight: moves.filter((m) => /^Filling/.test(m.label) && !m.inView).map((m) => m.label) };
    if (sc.embedConfirm || sc.confirm) res.detector = await confirmRun(page, frame, A, sc, js, net);
    return res;
  } finally {
    await page.close();
  }
}

// The person answers what was left and presses Submit (a real, trusted click); the detector is fed
// exactly what the shell feeds it: the click probe before and after, the network, the page marker.
async function confirmRun(page, frame, A, sc, js, net) {
  const fired = [];
  const d = sent.createDetector({ onFire: (r) => fired.push(r) });
  const startUrl = page.url();
  d.arm({ recordId: 1, startUrl, fillUrl: frame.url() });
  // the person's own answers to the rest (any option), as their clicks would
  await js(`(async () => { const T = window.TailorAutofill; const f = T.extractForm({ onlyEmpty: true });
    const ops = f.filter((x) => x.required && x.type !== "file").map((x) => ({ ref: x.ref, op: /^(text|email|tel|textarea)$/.test(x.type) ? "fill" : "select",
      text: /^(text|email|tel|textarea)$/.test(x.type) ? "An answer" : ((x.options || [])[0] || "Yes") }));
    return T.applyOpsAsync(ops); })()`);
  const probe = async (f) => { try { const p = await f.evaluate(sent.CLICK_PROBE_JS); d.signal(Object.assign({ kind: "probe", frame: f !== page.mainFrame(), frameUrl: f.url() }, p)); } catch (e) { /* navigating */ } };
  await probe(frame);
  net.length = 0;
  await frame.click("#submit");                     // the PERSON's click
  await page.waitForTimeout(1200);
  for (const r of net) d.signal(Object.assign({ kind: "network" }, r));
  const now = sc.frame ? page.frames().find((f) => f !== page.mainFrame()) : page.mainFrame();
  await now.waitForLoadState("load").catch(() => {});
  await page.waitForTimeout(300);
  await probe(now);
  if (!sc.frame) {
    await now.evaluate(CORE);
    const hit = await now.evaluate("window.TailorAutofill.detectSuccess()");
    if (hit) { d.signal({ kind: "page", how: "marker", pageUrl: now.url() }); d.signal({ kind: "page", how: "marker", pageUrl: now.url() }); }
  }
  return { fired: fired.length, how: fired[0] && fired[0].how, kinds: fired[0] ? fired[0].evidence.map((e) => e.kind) : [],
           posts: net.filter((r) => r.method === "POST").length,
           debug: process.env.SJ_REPLAY_DEBUG ? { net, state: d.state(), url: now.url(), subs: await now.evaluate("window.fxSubmits").catch(() => "x") } : undefined };
}

(async () => {
  const a = await serve(FORMS), b = await serve(FORMS);
  const A = "http://127.0.0.1:" + a.address().port, B = b.address().port;
  const browser = await chromium.launch({ args: ["--site-per-process"] });
  const only = process.env.SJ_REPLAY_ONLY || "";
  const results = [];
  try {
    for (const sc of SCENARIOS) {
      if (only && sc.name !== only) continue;
      try { results.push(await runOne(browser, A, B, sc)); }
      catch (e) { results.push({ name: sc.name, error: String(e && e.stack || e).slice(0, 600) }); }
    }
  } finally {
    await browser.close(); a.close(); b.close();
  }
  // ---- the checks ------------------------------------------------------------------------
  const fails = [];
  for (const r of results) {
    const sc = SCENARIOS.find((x) => x.name === r.name) || {};
    const mine = [];
    const bad = (m) => mine.push(r.name + ": " + m);
    check(r, bad);
    if (sc.expectFail) {
      if (!mine.some((m) => sc.expectFail.test(m))) fails.push(r.name + ": the planted bug was NOT caught: " + JSON.stringify(mine));
    } else fails.push(...mine);
  }
  process.stdout.write(JSON.stringify({ results, fails }, null, 1) + "\n");
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(2); });

function check(r, bad) {
  {
    if (r.error) { bad(r.error); return; }
    const reqs = r.truth.required;
    const missed = reqs.filter((q) => !q.filled && !q.outlined).map((q) => q.q);
    if (missed.length && !r.out.modal) bad("required, empty and not outlined as needs-you: " + JSON.stringify(missed));
    if (r.outOfSight.length) bad("filled out of sight: " + JSON.stringify(r.outOfSight));
    if (r.truth.submits) bad("submitted " + r.truth.submits + " time(s)");
    if (r.truth.open) bad(r.truth.open + " list(s) left open");
    const empty = reqs.filter((q) => !q.filled).length;
    if (r.end && r.end.where === "submit" && empty) bad(`the end says "check and submit" with ${empty} required question(s) empty`);
    if (r.end && r.end.where === "needs" && !empty) bad("the end says needs-you with nothing required empty");
    // the bar's "N need you" counts questions, one per question (never one per checkbox)
    if (r.end && r.end.where === "needs" && !r.out.modal && r.end.n !== reqs.filter((q) => !q.filled && q.outlined).length + reqs.filter((q) => q.filled && q.outlined).length)
      bad(`the end says ${r.end.n} need you for ${reqs.filter((q) => q.outlined).length} outlined question(s)`);
    if (r.detector) {
      if (r.detector.fired !== 1) bad("the sent detector fired " + r.detector.fired + " times: " + JSON.stringify(r.detector));
    }
  }
}
