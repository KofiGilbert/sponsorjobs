// Drive the REAL fill engine (extension/autofill_core.js) and the shell's CDP frame path
// (shell-electron/frames.js) in headless Chromium against the local widget fixtures in
// tests/fixtures/forms/. Never touches a real site: two local static servers only (port A, and
// port B for the "other origin" an iCIMS-style iframe is served from).
//
//   node forms_harness.js <scenario.json>
//
// scenario: {core, page, query?, steps:[...]}; each step is one of
//   {do:"eval", js, as?}            evaluate js (may return a promise) in the top page
//   {do:"click", sel}               a PERSON's click (Next, Submit), via Playwright
//   {do:"frameClick", sel}          a person's click inside the first child frame
//   {do:"frames", as?}              discover cross-origin frames + inject the engine (frames.js)
//   {do:"framesEval", js, as}       evaluate js in every discovered frame -> [results]
//   {do:"framesUpload", refsFrom, file, as}  CDP file upload in each frame for refs in a prior result
//   {do:"frameValues", as}          window.fxValues() of the first child frame (main world)
//   {do:"wait", ms}
// Prints {results:{...}} as JSON.
"use strict";
const fs = require("fs");
const http = require("http");
const path = require("path");

function findPlaywright() {
  const cands = [process.env.SJ_PLAYWRIGHT_DIR,
                 path.join(__dirname, "..", "..", "shell-electron", "node_modules", "playwright"),
                 path.join(process.env.HOME || "", "sponsorjobs", "shell-electron", "node_modules", "playwright")];
  for (const c of cands) { if (c && fs.existsSync(path.join(c, "package.json"))) return require(c); }
  throw new Error("playwright not found");
}

function serve(dir) {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      const u = new URL(req.url, "http://x");
      const f = path.join(dir, path.normalize(decodeURIComponent(u.pathname)).replace(/^[/\\]+/, ""));
      if (!f.startsWith(dir) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); res.end(); return; }
      const type = f.endsWith(".html") ? "text/html" : f.endsWith(".js") ? "text/javascript"
                 : f.endsWith(".css") ? "text/css" : "application/octet-stream";
      res.writeHead(200, { "Content-Type": type + "; charset=utf-8" });
      fs.createReadStream(f).pipe(res);
    });
    srv.listen(0, "127.0.0.1", () => resolve(srv));
  });
}

// webContents.debugger stand-in: a raw DevTools websocket to the page target, flat sessions.
function wsCdp(url) {
  const ws = new WebSocket(url);
  let id = 0;
  const pending = new Map();
  const fns = new Set();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(typeof ev.data === "string" ? ev.data : ev.data.toString());
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id); pending.delete(msg.id);
      if (msg.error) reject(new Error(msg.error.message)); else resolve(msg.result);
    } else if (msg.method) { for (const fn of fns) fn(msg.method, msg.params, msg.sessionId); }
  };
  const ready = new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  return {
    ready,
    send: (method, params, sessionId) => new Promise((resolve, reject) => {
      const m = { id: ++id, method, params: params || {} };
      if (sessionId) m.sessionId = sessionId;
      pending.set(m.id, { resolve, reject });
      ws.send(JSON.stringify(m));
    }),
    on: (fn) => fns.add(fn), off: (fn) => fns.delete(fn), close: () => ws.close(),
  };
}

async function freePort() {
  const s = http.createServer();
  await new Promise((r) => s.listen(0, "127.0.0.1", r));
  const p = s.address().port; s.close(); return p;
}

(async () => {
  const sc = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const root = path.resolve(__dirname, "..", "fixtures", "forms");
  const frames = require(path.resolve(__dirname, "..", "..", "shell-electron", "frames.js"));
  const pagewatch = require(path.resolve(__dirname, "..", "..", "shell-electron", "pagewatch.js"));
  const core = fs.readFileSync(sc.core, "utf8");
  const a = await serve(root), b = await serve(root);
  const A = "http://127.0.0.1:" + a.address().port, B = b.address().port;
  const port = await freePort();
  const { chromium } = findPlaywright();
  const browser = await chromium.launch({ args: ["--remote-debugging-port=" + port, "--site-per-process"] });
  const results = {};
  let cdp = null, ctxs = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 1400 } });
    let q = sc.query || "";
    q = q.replace(/\{B\}/g, String(B)).replace(/\{A\}/g, A);
    await page.goto(A + "/" + sc.page + q);
    await page.waitForLoadState("load");
    await page.waitForTimeout(150);
    for (const st of sc.steps) {
      if (st.do === "inject") { await page.evaluate(core); continue; }
      if (st.do === "eval") {
        const v = await page.evaluate(st.js.trim().startsWith("(") || /^await|return/.test(st.js)
          ? `(async () => (${st.js}))()` : `(async () => (${st.js}))()`);
        if (st.as) results[st.as] = v;
      } else if (st.do === "click") {
        await page.click(st.sel);
      } else if (st.do === "frameClick") {
        await page.frames()[1].click(st.sel);
      } else if (st.do === "wait") {
        await page.waitForTimeout(st.ms);
      } else if (st.do === "frames") {
        const list = await (await fetch("http://127.0.0.1:" + port + "/json/list")).json();
        const target = list.find((t) => t.type === "page" && t.url.startsWith(A));
        cdp = wsCdp(target.webSocketDebuggerUrl);
        await cdp.ready;
        ctxs = await frames.crossOriginFrames(cdp);
        for (const c of ctxs) await frames.injectEngine(cdp, c, core);
        if (st.as) results[st.as] = ctxs.map((c) => ({ url: c.url, oopif: !!c.sessionId, prefix: c.prefix }));
      } else if (st.do === "framesEval") {
        const out = [];
        for (const c of ctxs) out.push(await frames.evalIn(cdp, c, st.js));
        results[st.as] = out;
      } else if (st.do === "framesUpload") {
        const out = [];
        for (let i = 0; i < ctxs.length; i++) {
          const refs = ((results[st.refsFrom] || [])[i] || {}).uploads || [];
          out.push(await frames.uploadInFrame(cdp, ctxs[i], refs, path.resolve(st.file)));
        }
        results[st.as] = out;
      } else if (st.do === "frameValues") {
        results[st.as] = await page.frames()[1].evaluate("window.fxValues()");
      } else if (st.do === "isNewPage") {
        results[st.as] = pagewatch.isNewPage(new Set(results[st.prev].labels), results[st.now]);
      } else if (st.do === "route") {
        const ops = st.ops.map((o) => ({ ...o, ref: String(o.ref).replace("PREFIX", ctxs.length ? ctxs[0].prefix : "") }));
        const r = frames.routeOps(ops, ctxs);
        results[st.as] = { main: r.main.map((o) => o.ref), frames: [...r.byFrame.values()].map((l) => l.map((o) => o.ref)) };
      }
    }
  } finally {
    if (cdp) cdp.close();
    await browser.close();
    a.close(); b.close();
  }
  process.stdout.write(JSON.stringify({ results }) + "\n");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
