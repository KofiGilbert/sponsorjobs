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

// The plan as the engine route would make it, by label (made-up values only); "Why" is the person's.
function planFor(fields) {
  const want = { "First Name": "Avery", "Last Name": "Example", "Email": "apply.person@example.com",
                 "Are you legally authorized to work in the United States?": "Yes" };
  const ops = [], left = [];
  for (const f of fields) {
    const lab = String(f.label || "").replace(/\s*\*\s*$/, "");
    const v = want[lab];
    if (v) ops.push({ ref: f.ref, op: f.type === "text" || f.type === "email" ? "fill" : "select", text: v });
    else left.push({ ref: f.ref, label: f.label });
  }
  return { ok: true, ops, left, cost: 0 };
}

async function open(kind, A, B, port, browser, after) {
  const q = after ? "?after=" + after : "";
  const inner = kind === "oopif" ? `http://localhost:${B}/frame_walk_inner.html${q}`
              : kind === "cross" ? `http://127.0.0.1:${B}/frame_walk_inner.html${q}` : "frame_walk_inner.html" + q;
  const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
  if (process.env.SJ_DEBUG) page.on("pageerror", (e) => console.error("pageerror", e.message));
  await page.goto(`${A}/frame_walk_outer.html?inner=${encodeURIComponent(inner)}`);
  await page.waitForLoadState("load");
  await page.waitForTimeout(300);
  await page.evaluate(CORE); await page.evaluate(POINTER);
  // every pointer move the person sees: its label and where it is on the top page
  await page.evaluate(() => {
    window.__moves = [];
    const P = window.TailorPointer, point = P.point, at = P.at;
    const vis = (r) => r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth;
    P.point = (r, label, o) => { window.__moves.push({ label, top: r.top, left: r.left, inView: vis(r) }); return point(r, label, o); };
    P.at = (el, label, o) => {
      const r = window.TailorAutofill.topRect(el);
      window.__moves.push({ label, top: r.top, left: r.left, inView: vis(r), at: true });
      return at(el, label, o);
    };
  });
  const js = (e) => page.evaluate(`(async () => (${e}))()`);
  let cdp = null, wctx = [];
  if (kind !== "same") {
    const list = await (await fetch("http://127.0.0.1:" + port + "/json/list")).json();
    const target = list.find((t) => t.type === "page" && t.url.includes("frame_walk_outer"));
    cdp = wsCdp(target.webSocketDebuggerUrl);
    await cdp.ready;
    const ctxs = await frames.crossOriginFrames(cdp);
    assert(ctxs.length === 1, kind + ": one cross-origin frame, got " + ctxs.length);
    assert(kind !== "oopif" || ctxs[0].sessionId, "the other site's frame runs in its own process");
    for (const c of ctxs) {
      await frames.injectEngine(cdp, c, CORE);
      assert(await frames.injectPointerRelay(cdp, c, POINTER), kind + ": the pointer relay is in the frame");
      assert(await frames.tagOwner(cdp, c), kind + ": the frame's element is tagged on the top page");
    }
    wctx = frames.walkContexts(cdp, ctxs);
  }
  const frame = page.frames().find((f) => f !== page.mainFrame() && f.url().includes("frame_walk_inner"));
  const inFrame = (e) => (wctx.length ? wctx[0].js(e) : frame.evaluate(e));
  const walk = () => guided.walk({ js, frames: wctx, plan: async (f) => planFor(f), upload: async () => ({ n: 0, seen: [] }),
    wait: (ms) => new Promise((r) => setTimeout(r, Math.min(ms, 40))), timing: { min: 0, move: 0, scroll: 0, needs: 0, perChar: 10 } });
  // a real click at a frame box's place on the top page
  const clickIn = async (id) => {
    await inFrame(`(document.getElementById(${JSON.stringify(id)}).scrollIntoView({ block: "center" }), 0)`);
    await page.waitForTimeout(150);
    const er = await inFrame(`(() => { const r = document.getElementById(${JSON.stringify(id)}).getBoundingClientRect(); return { x: r.left + 20, y: r.top + r.height / 2 }; })()`);
    const fb = await page.evaluate(() => { const f = document.getElementById("icims_content_iframe"), r = f.getBoundingClientRect(), g = getComputedStyle(f);
      return { x: r.left + f.clientLeft + parseFloat(g.paddingLeft), y: r.top + f.clientTop + parseFloat(g.paddingTop) }; });
    await page.mouse.click(fb.x + er.x, fb.y + er.y);
  };
  // the shell's poll (main.js holdGuideFrames), once: the frame's sight, its kept pointer move drawn on the top page
  const pollOnce = async (seq) => {
    await guided.syncSight(js, wctx[0]);
    const took = await wctx[0].js(`window.TailorAutofill.relayTake(${seq || 0})`);
    if (took && took.point && !took.point.hide) await js(`window.TailorAutofill.pointFrom(${JSON.stringify(wctx[0].prefix)}, ${JSON.stringify(took.point.rect)}, ${JSON.stringify(took.point.label)}, ${JSON.stringify(Object.assign({}, took.point.opts, { now: true }))})`);
    return took;
  };
  return { page, js, cdp, wctx, frame, inFrame, walk, clickIn, pollOnce };
}

async function run(kind, A, B, port, browser) {
  const { page, js, cdp, wctx, frame, inFrame, walk, clickIn, pollOnce } = await open(kind, A, B, port, browser);
  const out = await walk();
  await page.waitForTimeout(300);
  const read = () => (wctx.length ? wctx[0].js(READ) : frame.evaluate(READ));
  const v = await read();
  // a same-origin frame's ring is drawn by the top page's engine on the top page, over the field
  if (kind === "same") {
    v.glow = await page.evaluate(() => {
      const h = document.querySelector("[data-sj-glow]");
      const ring = h && h.shadowRoot.querySelector(".r:not(.off)");
      const f = document.getElementById("icims_content_iframe").contentDocument.getElementById("why");
      if (!ring || !f) return false;
      const a = ring.getBoundingClientRect(), b = window.TailorAutofill.topRect(f);
      return a.left <= b.left && a.right >= b.right && a.top <= b.top && a.bottom >= b.bottom;
    });
  }
  const moves = await page.evaluate("window.__moves");
  const box = await page.evaluate(() => { const r = document.getElementById("icims_content_iframe").getBoundingClientRect(); return { left: r.left, right: r.right }; });

  // the end of the run, with the top page scrolled away from the frame's question: the end (and
  // Next / Previous) bring it into the person's sight on the top page AND in the frame, and the
  // pointer rests on it, placed on the top page through the relay the shell polls
  await js("(window.scrollTo(0, 0), 0)");
  await page.waitForTimeout(100);
  await page.evaluate("window.__moves = []");
  let end, sight = true;
  if (wctx.length) {
    await guided.syncSight(js, wctx[0]);
    end = await wctx[0].js("window.TailorAutofill.guideEnd()");
    await guided.bringIntoSight(js, wctx[0], end && end.ref);
    await page.waitForTimeout(400);
    const took = await pollOnce(0);
    assert(took && took.point && took.point.rect, kind + ": the frame kept the pointer's move " + JSON.stringify(took));
    assert(took.needs && took.needs.d && took.needs.d.n === 1, kind + ": the needs line is kept for the bar " + JSON.stringify(took.needs));
    sight = await guided.frameSight(js, wctx[0], end.ref);
    // Previous / Next on the bar or the Orb: the same, from anywhere on the page
    await js("(window.scrollTo(0, 0), 0)");
    await guided.syncSight(js, wctx[0]);
    const st = await wctx[0].js("window.TailorAutofill.needsStep(1)");
    await guided.bringIntoSight(js, wctx[0], st && st.ref);
    await page.waitForTimeout(400);
    const s2 = await guided.frameSight(js, wctx[0], st.ref);
    assert(st && st.n === 1 && s2 === true, kind + ": Next takes the person to the frame's question " + JSON.stringify({ st, s2 }));
  } else {
    end = await js("window.TailorAutofill.guideEnd()");
    await page.waitForTimeout(400);
  }
  const rest = await page.evaluate("window.__moves");
  const restState = await page.evaluate("window.TailorPointer.state()");

  // the needs-you Enter flow inside the frame: the person types the answer and presses Enter; the
  // pointer moves on to the frame's Submit ("All done"), never pressing it
  await clickIn("why");
  await page.keyboard.type("Because the work fits");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(700);
  let allDone = "";
  if (wctx.length) { const t = await pollOnce(0); allDone = t && t.point ? t.point.label : ""; }
  else { const st2 = await page.evaluate("window.TailorPointer.state()"); allDone = st2 ? st2.label : ""; }

  // the person's Enter in a frame's box never sends the form; a script's click on Submit is stopped
  if (wctx.length) await wctx[0].js("window.TailorAutofill.clearNeeds()"); else await js("window.TailorAutofill.clearNeeds()");
  await clickIn("email");
  const focused = await inFrame("document.activeElement && document.activeElement.id");
  assert(focused === "email", kind + ": the click reached the frame's Email box, got " + focused);
  await page.keyboard.press("Enter");
  await inFrame("(document.getElementById('submit').click(), 0)");     // a script's click on Submit
  await page.waitForTimeout(200);
  const after = await read();
  if (cdp) cdp.close();
  await page.close();
  return { out, v, moves, box, end, rest, restState, after, sight, allDone };
}

// The person sends the application from inside the frame (a real click on Submit). The shell's poll
// (main.js holdGuideFrames) feeds the frame's answers to the signal reader (frame_signals.js); a
// frame whose page changed is read once more with a fresh engine, as the shell does (rereadFrame).
// The signals are what the app's one sent detector hears; this test only checks what they say.
async function runSubmit(kind, after, A, B, port, browser) {
  const { page, cdp, wctx, walk, clickIn } = await open(kind, A, B, port, browser, after);
  await walk();
  await wctx[0].js("window.TailorAutofill.armFrameSubmitWatch(true)");
  await clickIn("why");
  await page.keyboard.type("Because the work fits");
  await clickIn("submit");                                              // the PERSON's Submit
  const sigs = [];
  const reader = frameSignals.createFrameSignalReader({ signal: (x) => sigs.push(x) });
  let ctx = wctx[0];
  for (let i = 0; i < 16; i++) {
    await page.waitForTimeout(150);
    const got = await ctx.js("window.TailorAutofill && window.TailorAutofill.relayTake(0)");
    if (got) { reader.seen(ctx, got); continue; }
    await page.waitForTimeout(300);                                     // the new page loads
    const found = await frames.reopenFrame(cdp, ctx.raw);
    if (!found) continue;
    await frames.injectEngine(cdp, found, CORE);
    reader.seen(ctx, await frames.evalIn(cdp, found, "(() => { const T = window.TailorAutofill, r = T.relayTake(0); "
      + "return Object.assign(r, { form: r.form || T.formFill(), success: r.success || !!T.detectSuccess() }); })()"));
    ctx = Object.assign(frames.walkContexts(cdp, [found])[0], { prefix: ctx.prefix });
  }
  cdp.close();
  await page.close();
  return sigs;
}

(async () => {
  const a = await serve(FORMS), b = await serve(FORMS);
  const A = "http://127.0.0.1:" + a.address().port, B = b.address().port;
  const port = await freePort();
  const browser = await chromium.launch({ args: ["--remote-debugging-port=" + port, "--site-per-process"] });
  try {
    for (const kind of ["oopif", "cross", "same"]) {
      const r = await run(kind, A, B, port, browser);
      const tag = kind + ": ";
      assert(r.v.first === "Avery" && r.v.last === "Example" && r.v.email === "apply.person@example.com", tag + "text filled " + JSON.stringify(r.v));
      assert(r.v.auth === "Yes", tag + "the list picked " + JSON.stringify(r.v));
      assert(r.v.why === "", tag + "the person's question is never filled");
      assert(r.v.open === 0, tag + "no list left open " + JSON.stringify(r.v));
      assert(r.v.submitted === 0 && r.after.submitted === 0, tag + "never submitted " + JSON.stringify(r.after));
      assert(r.out.needs.length === 1 && /Why do you want/.test(r.out.needs[0]), tag + "needs you " + JSON.stringify(r.out.needs));
      assert(r.v.glow, tag + "the glow ring is drawn for the frame's question");
      // the pointer: on each field, in the person's sight, inside the frame's box, in page order
      const fills = r.moves.filter((m) => /^Filling: /.test(m.label));
      const names = fills.map((m) => m.label.replace(/^Filling: /, ""));
      const want = ["First Name", "Last Name", "Email", "Are you legally authorized to work in the United States?"];
      assert(names.length === want.length && names.every((n, i) => want[i].startsWith(n)),
             tag + "walked top to bottom " + JSON.stringify(names));
      assert(fills.every((m) => m.inView), tag + "every field filled in sight " + JSON.stringify(fills));
      assert(fills.every((m) => m.left >= r.box.left && m.left <= r.box.right + 400), tag + "the pointer is at the frame's fields " + JSON.stringify({ fills, box: r.box }));
      assert(r.moves.some((m) => /^Needs you: Why do you want/.test(m.label) && m.inView), tag + "the needs-you pause is in sight");
      // the end rests on that question, on the top page, in sight
      assert(r.end && r.end.where === "needs" && r.end.n === 1, tag + "the end rests on the question " + JSON.stringify(r.end));
      assert(r.rest.some((m) => m.inView && /Fill this, then press Enter/.test(m.label)), tag + "the pointer says what to do there " + JSON.stringify(r.rest));
      assert(r.restState && r.restState.label, tag + "the pointer is showing at the end");
      assert(kind === "same" ? r.out.frames === 0 : r.out.frames === 1, tag + "frames walked " + r.out.frames);
      assert(r.sight === true, tag + "the end brings the question into the person's sight");
      assert(/All done/.test(r.allDone), tag + "Enter on the answered question moves on to Submit " + JSON.stringify(r.allDone));
    }
    // what the frame says after the person's Submit (signals for the app's sent detector)
    for (const kind of ["oopif", "cross"]) {
      for (const after of ["reset", "reload", "thanks", "error"]) {
        const sigs = await runSubmit(kind, after, A, B, port, browser);
        const kinds = sigs.map((x) => x.kind);
        const tag = `${kind} ${after}: `;
        if (process.env.SJ_DEBUG) console.error(tag, JSON.stringify(sigs));
        assert(kinds[0] === "submit-click" && sigs[0].filled >= 4 && /frame_walk_inner/.test(sigs[0].frameUrl),
               tag + "the person's Submit is a signal " + JSON.stringify(sigs));
        assert(kinds.filter((k) => k === "submit-click").length === 1, tag + "said once " + JSON.stringify(kinds));
        const reset = sigs.find((x) => x.kind === "form-reset");
        if (after === "reset" || after === "reload") assert(reset && reset.filledBefore >= 4 && reset.filledAfter === 0, tag + "the form came back empty " + JSON.stringify(sigs));
        if (after === "thanks") assert(sigs.some((x) => x.kind === "page" && x.how === "marker" && x.frame === true), tag + JSON.stringify(sigs));
        if (after !== "thanks") assert(!kinds.includes("page"), tag + "no confirmation where there is none " + JSON.stringify(kinds));
        if (after === "error") assert(kinds.length === 1, tag + "an error keeps the answers: nothing more is said " + JSON.stringify(kinds));
      }
    }
  } finally {
    await browser.close();
    a.close(); b.close();
  }
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
