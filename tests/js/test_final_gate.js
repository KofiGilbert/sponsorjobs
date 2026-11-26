// The final check before "Check it, then submit" (2026-11-26, the owner), in a real headless
// Chromium with site isolation (tests/fixtures/forms/final_gate_*.html). Run by
// tests/test_frame_walk.py. After the walk, every field is read fresh from the page and Chrome's
// accessibility tree, on the top page and inside a frame another site serves:
//   * a required box the walk did not answer, a box the site cleared after the walk, a custom list
//     whose wrapper alone says it is required, and a required box inside the frame all become
//     needs-you items (a ring each); a one-answer group with two ticks is flagged;
//   * "Filled N" is the number of fields holding an answer NOW;
//   * once the person answers, the check finds nothing left.
// (The original header of test_frame_walk.js follows, unchanged:)
// The guided walk inside frames (2026-11-26), in a real headless Chromium with site isolation, on a
// careers page whose form lives in an iframe below the fold (tests/fixtures/forms/frame_walk_*.html).
// Run by tests/test_frame_walk.py. Three frames: another site (its own process), another port
// (cross-origin, same process) and the same origin (reached by the top page's own engine).
//   * the frame's fields join the walk in page order, filled in sight: the top page scrolls to each,
//     and the ONE pointer, drawn on the top page, is ON the field (inside the frame's box, in view);
//   * a question only the person can answer gets the glow ring and "Needs you", never filled;
//   * the needs-you flow: the end rests on that question, the pointer placed on the top page;
//   * Enter in a frame's box never sends the form, a script's click on its Submit is stopped, and
//     nothing is ever submitted.
"use strict";
const fs = require("fs"), http = require("http"), path = require("path");
const PW = process.env.SJ_PLAYWRIGHT_DIR || path.join(process.env.HOME, "sponsorjobs/shell-electron/node_modules/playwright");
const { chromium } = require(PW);
const ROOT = path.join(__dirname, "..", "..");
const CORE = fs.readFileSync(path.join(ROOT, "extension", "autofill_core.js"), "utf8");
const POINTER = fs.readFileSync(path.join(ROOT, "extension", "pointer_overlay.js"), "utf8");
const guided = require(path.join(ROOT, "shell-electron", "guided.js"));
const frames = require(path.join(ROOT, "shell-electron", "frames.js"));
const frameSignals = require(path.join(ROOT, "shell-electron", "frame_signals.js"));   // its signals: sent_detect.js
const FORMS = path.join(ROOT, "tests", "fixtures", "forms");
const assert = (c, m) => { if (!c) throw new Error(m); };
// What the frame's form holds, read from the page itself (any script world can run it).
const READ = `(() => { const d = document, v = (id) => d.getElementById(id).value;
  const sv = d.querySelector("#w1 .select__single-value");
  return { first: v("first"), last: v("last"), email: v("email"), why: v("why"), auth: sv ? sv.textContent.trim() : "",
           submitted: Number(d.body.dataset.submitted || 0), glow: !!d.querySelector("[data-sj-glow]"),
           open: [...d.querySelectorAll("[aria-expanded='true']")].length + d.querySelectorAll(".select__menu").length }; })()`;

function serve(dir) {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      const u = new URL(req.url, "http://x");
      const f = path.join(dir, path.normalize(decodeURIComponent(u.pathname)).replace(/^[/\\]+/, ""));
      if (!f.startsWith(dir) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); res.end(); return; }
      const type = f.endsWith(".html") ? "text/html" : f.endsWith(".js") ? "text/javascript" : f.endsWith(".css") ? "text/css" : "application/octet-stream";
      res.writeHead(200, { "Content-Type": type + "; charset=utf-8" });
      fs.createReadStream(f).pipe(res);
    });
    srv.listen(0, "127.0.0.1", () => resolve(srv));
  });
}
function wsCdp(url) {
  const ws = new WebSocket(url);
  let id = 0;
  const pending = new Map(), fns = new Set();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id); pending.delete(msg.id);
      if (msg.error) reject(new Error(msg.error.message)); else resolve(msg.result);
    } else if (msg.method) { for (const fn of fns) fn(msg.method, msg.params, msg.sessionId); }
  };
  const ready = new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  return { ready, on: (fn) => fns.add(fn), off: (fn) => fns.delete(fn), close: () => ws.close(),
    send: (method, params, sessionId) => new Promise((resolve, reject) => {
      const m = { id: ++id, method, params: params || {} };
      if (sessionId) m.sessionId = sessionId;
      pending.set(m.id, { resolve, reject });
      ws.send(JSON.stringify(m));
    }) };
}
async function freePort() {
  const s = http.createServer();
  await new Promise((r) => s.listen(0, "127.0.0.1", r));
  const p = s.address().port; s.close(); return p;
}


(async () => {
  const a = await serve(FORMS), b = await serve(FORMS);
  const A = "http://127.0.0.1:" + a.address().port, B = b.address().port;
  const port = await freePort();
  const browser = await chromium.launch({ args: ["--remote-debugging-port=" + port, "--site-per-process"] });
  let cdp = null;
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
    await page.goto(`${A}/final_gate_outer.html?inner=${encodeURIComponent(`http://localhost:${B}/final_gate_inner.html`)}`);
    await page.waitForLoadState("load");
    await page.waitForTimeout(300);
    await page.evaluate(CORE); await page.evaluate(POINTER);
    const js = (e) => page.evaluate(`(async () => (${e}))()`);
    // the walk answered First Name and City; the site then cleared City (its own re-render)
    await js("(window.TailorAutofill.setNativeValue(document.getElementById('first'), 'Avery'), window.TailorAutofill.setNativeValue(document.getElementById('city'), 'Chicago'), 0)");
    await js("(window.TailorAutofill.setNativeValue(document.getElementById('city'), ''), 0)");
    const list = await (await fetch("http://127.0.0.1:" + port + "/json/list")).json();
    const target = list.find((t) => t.type === "page" && t.url.includes("final_gate_outer"));
    cdp = wsCdp(target.webSocketDebuggerUrl);
    await cdp.ready;
    const ctxs = await frames.crossOriginFrames(cdp);
    assert(ctxs.length === 1 && ctxs[0].sessionId, "the frame runs in its own process");
    await frames.injectEngine(cdp, ctxs[0], CORE);
    const fjs = (e) => frames.evalIn(cdp, ctxs[0], e);
    // Chrome's accessibility tree, on the page and in the frame (main.js finalGate)
    const tagged = (await frames.axTagRequired(cdp, undefined)) + (await frames.axTagRequired(cdp, ctxs[0].sessionId));
    assert(tagged >= 1, "the accessibility tree marked the required fields, got " + tagged);
    const top = await js("window.TailorAutofill.verifyAndMark()");
    const inFrame = await fjs("window.TailorAutofill.verifyAndMark()");
    const names = top.items.join(" | ");
    assert(/Person of Colour/.test(names), "a required box the walk skipped: " + names);
    assert(/City/.test(names), "a box the site cleared after the walk: " + names);
    assert(/Work setting/.test(names), "a custom list required by its wrapper: " + names);
    assert(top.multi_ticked === 1, "a one-answer group with two ticks: " + JSON.stringify(top));
    assert(top.filled === 2, "Filled counts what holds an answer now (First Name and the shift group): " + JSON.stringify(top));
    assert(inFrame.needs === 1 && /Phone/.test(inFrame.items[0]) && inFrame.filled === 0, "a required box inside the frame: " + JSON.stringify(inFrame));
    assert(!/Referral/.test(names + inFrame.items.join(" ")), "an optional box is never asked for");
    const rings = await page.evaluate(() => { const h = document.querySelector("[data-sj-glow]"); return h ? h.shadowRoot.querySelectorAll(".r").length : 0; });
    assert(rings >= 3, "each needs-you item on the page has its ring, got " + rings);
    assert((await fjs("!!document.querySelector('[data-sj-glow]')")) === true, "the frame's needs-you item has its ring");
    assert((await js("window.TailorAutofill.needsList().length")) >= 3 && (await fjs("window.TailorAutofill.needsList().length")) === 1, "Next steps through them");
    // the person answers them: nothing is left (the two ticks are the site's own control, still flagged)
    await js("(window.TailorAutofill.setNativeValue(document.getElementById('colour'), 'Prefer not to say'), window.TailorAutofill.setNativeValue(document.getElementById('city'), 'Chicago'), 0)");
    await js("window.TailorAutofill.applyOpsAsync([{ ref: window.TailorAutofill.fieldRef(document.getElementById('ws-q')), op: 'select', text: 'Remote' }])");
    await fjs("(window.TailorAutofill.setNativeValue(document.getElementById('phone'), '5550100'), 0)");
    const top2 = await js("window.TailorAutofill.verifyForm()");
    const in2 = await fjs("window.TailorAutofill.verifyForm()");
    assert(top2.needs.length === 0 && in2.needs.length === 0, "answered: nothing left " + JSON.stringify({ top2, in2 }));
    assert(top2.filled === 5 && in2.filled === 1, "Filled counts them now " + JSON.stringify({ top2, in2 }));
  } finally {
    if (cdp) cdp.close();
    await browser.close();
    a.close(); b.close();
  }
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
