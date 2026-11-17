// The accessibility tree merged into the agent's page view, and the final form check before
// "Ready to send" (shell-electron/ax_view.js, docs/apply-agent.md section 20), in a real headless
// Chromium on the local fixtures tests/fixtures/forms/ax_*.html. Never touches a real site: two
// local static servers (the second is the "other origin" an embedded application is served from).
// Run directly (node tests/js/test_ax_view.js) or through tests/test_ax_view.py.
//
// What it pins, per fixture (modelled on the 2026-11-16 Gusto run and the Workday and Lever pages):
// every box of "How did you hear" ticked, the resume in the Cover Letter box, a dropdown left open,
// a field marked invalid, a required field left empty (also when only a "✱" says required), the
// resume listed three times, two answers ticked on a Yes/No question; the clean version of each
// form passes with "Checked N required fields: all filled"; a multi-answer question is never
// flagged; no file name and no typed value ever reaches a line; the check reads an embedded
// cross-origin frame; and the gate's order: the brain fixes once, the person is asked once, then
// the hand-over names what is left.
"use strict";
const fs = require("fs");
const http = require("http");
const path = require("path");
const assert = require("assert");

const ROOT = path.resolve(__dirname, "..", "..");
const AGENT = fs.readFileSync(path.join(ROOT, "extension", "agent_view.js"), "utf8");
const CORE = fs.readFileSync(path.join(ROOT, "extension", "autofill_core.js"), "utf8");
const FIXTURES = path.join(ROOT, "tests", "fixtures", "forms");
const ax = require(path.join(ROOT, "shell-electron", "ax_view.js"));
const frames = require(path.join(ROOT, "shell-electron", "frames.js"));

function findPlaywright() {
  const cands = [process.env.SJ_PLAYWRIGHT_DIR,
                 path.join(ROOT, "shell-electron", "node_modules", "playwright"),
                 path.join(process.env.HOME || "", "sponsorjobs", "shell-electron", "node_modules", "playwright")];
  for (const c of cands) { if (c && fs.existsSync(path.join(c, "package.json"))) return require(c); }
  throw new Error("playwright not found (shell-electron devDependency)");
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

async function freePort() {
  const s = http.createServer();
  await new Promise((r) => s.listen(0, "127.0.0.1", r));
  const p = s.address().port; s.close(); return p;
}

// webContents.debugger stand-in: a raw DevTools websocket to one page target, flat sessions.
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

const tests = [];
const test = (name, fn) => tests.push({ name, fn });
const line = (text, i) => (text.split("\n").find((l) => l.replace(/^\*/, "").startsWith(`[${i}]`)) || "");
const lineOf = (text, re) => text.split("\n").find((l) => /^\*?\[\d+\]</.test(l) && re.test(l)) || "";
const kinds = (res) => res.problems.map((p) => p.kind).sort();
const elementLines = (text) => text.split("\n").filter((l) => /^\*?\[\d+\]</.test(l));

// Open a fixture as the pane has it (engine + agent view), take the view, and collect.
async function viewed(ctx, fixture, o) {
  const page = await ctx.open(fixture);
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  if (o && o.before) await o.before(page);
  const v = await page.evaluate("window.TailorAgent.view()");
  const s = await page.context().newCDPSession(page);
  const cdp = { send: (m, p) => s.send(m, p || {}) };
  const entries = await ax.collect(cdp, [{ base: 1 }], v.text);
  return { page, v, cdp, entries, text: ax.annotate(v.text, entries, (o && o.opts) || {}),
           res: ax.verifyForm(entries, (o && o.opts) || {}) };
}

// ------------------------------------------------------------------ Greenhouse
test("Greenhouse (the Gusto page): every box ticked, resume in Cover Letter, open dropdown, invalid, empty", async (ctx) => {
  const { page, v, text, res, entries } = await viewed(ctx, "ax_greenhouse.html?state=bug");
  assert.deepStrictEqual(kinds(res), ["every-box", "invalid", "left-open", "required-empty", "wrong-file"].sort(), JSON.stringify(res, null, 1));
  const by = (k) => res.problems.find((p) => p.kind === k);
  assert.strictEqual(by("required-empty").label, "Last Name");
  assert.strictEqual(by("invalid").label, "LinkedIn Profile");
  assert.ok(/sponsorship/.test(by("left-open").label));
  assert.strictEqual(by("wrong-file").label, "Cover Letter");
  assert.strictEqual(by("every-box").label, "How did you hear about this job?", "the question, without its star");
  assert.ok(!res.ok && /^Checked 7 required fields: 5 problems$/.test(res.line), res.line);
  // Each problem points at the line the brain acts on.
  assert.ok(line(v.text, by("wrong-file").index).includes('label="Cover Letter"'));
  // react-select's chosen value stays (the DOM view's strength); the tree adds invalid and the file.
  assert.ok(/<combobox label="Are you legally authorized[^"]*" value="yes"/.test(text), text);
  assert.ok(/label="LinkedIn Profile" value="\(filled\)" invalid>/.test(text), text);
  const letter = lineOf(text, /label="Cover Letter"/);
  assert.ok(/file=chosen/.test(letter) && /holds="resume"/.test(letter), letter);
  assert.ok(/file=none/.test(lineOf(v.text, /label="Cover Letter"/)), "the DOM view alone said the box was empty");
  // A one-letter remove button reads by its accessible name.
  assert.ok(/<button name="Remove file">x<\/button>/.test(text), text);
  // "Which tools (select all that apply)": two of three ticked is fine.
  assert.ok(!res.problems.some((p) => /tools/i.test(p.label)));
  // Every line the AX merge touched kept its number and order.
  assert.deepStrictEqual(elementLines(text).map((l) => /\[(\d+)\]/.exec(l)[1]), elementLines(v.text).map((l) => /\[(\d+)\]/.exec(l)[1]));
  assert.ok(entries.filter((e) => /^(input|combobox|file|checkbox)$/.test(e.kind)).every((e) => e.ax), "every control met its accessibility node");
  await page.close();
});

test("Greenhouse done right: Checked 7 required fields: all filled", async (ctx) => {
  const { page, res } = await viewed(ctx, "ax_greenhouse.html?state=clean");
  assert.ok(res.ok, JSON.stringify(res.problems));
  assert.strictEqual(res.line, "Checked 7 required fields: all filled");
  await page.close();
});

test("no typed value and no file name ever reaches a line the model reads", async (ctx) => {
  const { page, text } = await viewed(ctx, "ax_greenhouse.html?state=bug");
  for (const l of elementLines(text)) {
    assert.ok(!/applicant@example\.com|not a url|"Test"/.test(l), l);
    assert.ok(!/\.pdf/i.test(l.replace(/ accept="[^"]*"/, "")), l);       // accept= is the site's own list of types
  }
  await page.close();
});

// ------------------------------------------------------------------ Lever
test("Lever: a ✱ alone makes a field required; two answers on a Yes/No question are caught", async (ctx) => {
  const { page, v, text, res } = await viewed(ctx, "ax_lever.html?state=bug");
  // Since #282 the DOM view reads Lever's ✱ itself; the merged view must still say required.
  assert.ok(/ required>$/.test(lineOf(text, /label="Email"/)), lineOf(text, /label="Email"/));
  assert.ok(!/ required/.test(lineOf(text, /label="Current company"/)), "no ✱, not required");
  assert.deepStrictEqual(kinds(res), ["many-ticked", "required-empty", "required-empty", "required-empty"], JSON.stringify(res, null, 1));
  const empty = res.problems.filter((p) => p.kind === "required-empty").map((p) => p.label);
  assert.deepStrictEqual(empty, ["Email", "Phone", "Are you legally authorized to work in the United States?"]);
  assert.strictEqual(res.problems.find((p) => p.kind === "many-ticked").label, "Have you worked here before?");
  assert.ok(!res.problems.some((p) => /teams/i.test(p.label)), "several teams may be ticked");
  // The resume Lever shows as a filename counts as attached.
  assert.ok(/<file label="Resume\/CV" file=chosen required holds="resume">/.test(text), lineOf(text, /<file/));
  await page.close();
});

test("Lever done right: Checked 6 required fields: all filled", async (ctx) => {
  const { page, res } = await viewed(ctx, "ax_lever.html?state=clean");
  assert.ok(res.ok, JSON.stringify(res.problems));
  assert.strictEqual(res.line, "Checked 6 required fields: all filled");
  await page.close();
});

// ------------------------------------------------------------------ Workday
test("Workday: the resume listed three times, a dropdown left open, an invalid and an empty field", async (ctx) => {
  const { page, text, res } = await viewed(ctx, "ax_workday.html?state=bug");
  assert.deepStrictEqual(kinds(res), ["duplicate-file", "invalid", "left-open", "required-empty"], JSON.stringify(res, null, 1));
  assert.ok(/3 times/.test(res.problems.find((p) => p.kind === "duplicate-file").text));
  assert.ok(/How Did You Hear About Us/.test(res.problems.find((p) => p.kind === "left-open").label));
  assert.strictEqual(res.problems.find((p) => p.kind === "invalid").label, "Postal Code");
  assert.strictEqual(res.problems.find((p) => p.kind === "required-empty").label, "Address Line 1");
  assert.ok(/<file label="Resume\/CV" file=chosen required holds="resume x3">/.test(text), lineOf(text, /<file/));
  await page.close();
});

test("Workday done right: Checked 4 required fields: all filled", async (ctx) => {
  const { page, res } = await viewed(ctx, "ax_workday.html?state=clean");
  assert.ok(res.ok, JSON.stringify(res.problems));
  assert.strictEqual(res.line, "Checked 4 required fields: all filled");
  await page.close();
});

// ------------------------------------------------------------------ a cover letter box
test("the resume in a cover letter box is caught by the run's own file name; the right files pass", async (ctx) => {
  const pdf = (name) => ({ name, mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\n%%EOF\n") });
  const opts = { resumeName: "Applicant 2026.pdf", letterName: "Applicant Cover Letter.pdf" };
  const bad = await viewed(ctx, "ax_cover_letter.html", { opts, before: (p) => p.setInputFiles("#letter", pdf("Applicant 2026.pdf")) });
  assert.deepStrictEqual(kinds(bad.res), ["required-empty", "wrong-file"], JSON.stringify(bad.res, null, 1));
  assert.ok(/holds="resume"/.test(lineOf(bad.text, /label="Cover letter"/)), lineOf(bad.text, /label="Cover letter"/));
  // An empty required box is ONE problem, not also "invalid" (Chrome marks it invalid as well).
  assert.ok(!bad.res.problems.some((p) => p.kind === "invalid"));
  // Without the run's file names an unnamed file is just "file": no guess.
  assert.ok(!ax.verifyForm(bad.entries, {}).problems.some((p) => p.kind === "wrong-file"));
  await bad.page.close();
  const good = await viewed(ctx, "ax_cover_letter.html", { opts, before: async (p) => {
    await p.setInputFiles("#resume", pdf("Applicant 2026.pdf"));
    await p.setInputFiles("#letter", pdf("Applicant Cover Letter.pdf"));
  } });
  assert.ok(good.res.ok, JSON.stringify(good.res.problems));
  assert.strictEqual(good.res.line, "Checked 1 required field: all filled");
  await good.page.close();
});

// ------------------------------------------------------------------ an embedded cross-origin frame
test("an application embedded from another site (its own process) is read through the frame's session", async (ctx) => {
  const page = await ctx.open("icims_outer.html?inner=" + encodeURIComponent("http://localhost:" + ctx.B + "/ax_lever.html?state=bug"));
  await page.waitForTimeout(400);
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  const list = await (await fetch("http://127.0.0.1:" + ctx.port + "/json/list")).json();
  const target = list.find((t) => t.type === "page" && t.url.includes("icims_outer.html"));
  const cdp = wsCdp(target.webSocketDebuggerUrl);
  await cdp.ready;
  try {
    const ctxs = await frames.crossOriginFrames(cdp);
    assert.strictEqual(ctxs.length, 1, JSON.stringify(ctxs.map((c) => c.url)));
    assert.ok(ctxs[0].sessionId, "the frame is out of process");
    await frames.injectEngine(cdp, ctxs[0], CORE);
    // The agent view straight into the frame's world (injectEngine loads only what is not there
    // yet, judged by TailorAutofill, so it would skip this second script).
    await frames.evalIn(cdp, ctxs[0], AGENT + "\n;0");
    const main = await page.evaluate("window.TailorAgent.view({ indexBase: 1 })");
    const inner = await frames.evalIn(cdp, ctxs[0], "window.TailorAgent.view({ indexBase: 1001 })");
    const text = main.text + "\n--- embedded frame 1 ---\n" + inner.text;
    const entries = await ax.collect(cdp, [{ base: 1 }, { base: 1001, sessionId: ctxs[0].sessionId, contextId: ctxs[0].contextId }], text);
    const framed = entries.filter((e) => e.index >= 1001);
    assert.ok(framed.length >= 10 && framed.filter((e) => /^(input|radio|checkbox)$/.test(e.kind)).every((e) => e.ax),
              JSON.stringify(framed.map((e) => [e.index, e.kind, e.ax])));
    const res = ax.verifyForm(entries, {});
    assert.deepStrictEqual(kinds(res), ["many-ticked", "required-empty", "required-empty", "required-empty"], JSON.stringify(res, null, 1));
    assert.ok(res.problems.every((p) => p.index >= 1001));
    assert.ok(/ required>$/.test(lineOf(ax.annotate(text, entries, {}), /\[100\d\]<input type=email/)));
  } finally { cdp.close(); await page.close(); }
});

// ------------------------------------------------------------------ the gate, through the shell's own path
// The shell's objects, faked over Playwright: wc.executeJavaScript is the page, the debugger is the
// page's CDP session (deps.acquireDebugger / deps.frames.electronCdp as main.js hands them in).
async function shellLike(ctx, fixture, o) {
  const page = await ctx.open(fixture);
  await page.evaluate(CORE);
  await page.evaluate(AGENT);
  const s = await page.context().newCDPSession(page);
  const wc = { debugger: {}, executeJavaScript: (expr) => page.evaluate(expr) };
  const deps = { acquireDebugger: () => ((o && o.noCdp) ? null : () => {}),
                 frames: { electronCdp: () => ({ send: (m, p) => s.send(m, p || {}), dispose: () => {} }) } };
  const feed = { lines: [], push(phase, verb, summary, x) { this.lines.push({ phase, verb, summary, state: x && x.state, detail: x && x.detail }); } };
  const v = await page.evaluate("window.TailorAgent.view()");
  return { page, wc, deps, feed, v };
}

test("the gate: the brain gets the problems once, then the person is asked once, then they are noted", async (ctx) => {
  const { page, wc, deps, feed, v } = await shellLike(ctx, "ax_greenhouse.html?state=bug");
  const seen = new Set();
  const o = { wc, fr: null, deps, feed, seen, recordId: 7, resumePath: "/x/uploads/7/resume.pdf", viewText: v.text };
  const g1 = await ax.readyGate(o);
  assert.ok(g1.nudge && !g1.ask && !g1.ok, JSON.stringify(g1));
  assert.ok(/^Before I hand over, the form check found: /.test(g1.nudge) && /never Submit/.test(g1.nudge), g1.nudge);
  assert.ok(/\[14\] the resume is in the cover letter box/.test(g1.nudge), "names the line to fix: " + g1.nudge);
  assert.ok(!/\.pdf/.test(g1.nudge), "no file name to the model");
  assert.ok(feed.lines.some((l) => l.verb === "Form check" && l.state === "retry" && /5 problems/.test(l.summary)), JSON.stringify(feed.lines));
  const g2 = await ax.readyGate(o);
  assert.ok(g2.ask && !g2.nudge, JSON.stringify(g2));
  assert.ok(/^Before I hand over: /.test(g2.ask) && /Fix it on the page, then press Continue\.$/.test(g2.ask), g2.ask);
  assert.ok(!/[–—]/.test(g2.ask), "no dashes in what the person reads");
  const g3 = await ax.readyGate(o);
  assert.ok(Array.isArray(g3.noted) && g3.noted.length === 3 && !g3.ask && !g3.nudge, JSON.stringify(g3));
  assert.ok(seen.has("ax-fix|7") && seen.has("ax-asked|7"), "kept on the run's checkedPages (it survives a park)");
  await page.close();
});

test("the gate on a clean form: one Steps line, Checked N required fields: all filled", async (ctx) => {
  const { page, wc, deps, feed, v } = await shellLike(ctx, "ax_workday.html?state=clean");
  const g = await ax.readyGate({ wc, fr: null, deps, feed, seen: new Set(), recordId: 1, viewText: v.text });
  assert.ok(g.ok, JSON.stringify(g));
  assert.deepStrictEqual(feed.lines, [{ phase: "formcheck", verb: "Checked", summary: "4 required fields: all filled", state: "done", detail: undefined }]);
  await page.close();
});

test("without the debugger (DevTools holds it) the page's own facts still catch the blockers", async (ctx) => {
  const { page, wc, deps, v } = await shellLike(ctx, "ax_workday.html?state=bug", { noCdp: true });
  const entries = await ax.collectLive({ wc, fr: null, deps, text: v.text });
  assert.ok(entries.every((e) => !e.ax));
  const res = ax.verifyForm(entries, {});
  assert.deepStrictEqual(kinds(res), ["duplicate-file", "invalid", "left-open", "required-empty"], JSON.stringify(res, null, 1));
  const text = await ax.enrich({ wc, fr: null, deps, text: v.text });
  assert.ok(/holds="resume x3"/.test(text), text);
  await page.close();
});

test("enrich over the shell's path equals collect + annotate", async (ctx) => {
  const { page, wc, deps, v } = await shellLike(ctx, "ax_lever.html?state=bug");
  const text = await ax.enrich({ wc, fr: null, deps, text: v.text });
  assert.ok(/<input type=email label="Email" value="" required>/.test(text), text);
  await page.close();
});

// ------------------------------------------------------------------ pure rules
test("which questions take one answer", async () => {
  assert.strictEqual(ax.allowsOne("How did you hear about us?", ["LinkedIn", "Indeed", "Referral"]), true);
  assert.strictEqual(ax.allowsOne("How did you hear about us? (select all that apply)", ["LinkedIn", "Indeed"]), false);
  assert.strictEqual(ax.allowsOne("Have you worked here before?", ["No", "Yes"]), true);
  assert.strictEqual(ax.allowsOne("Which teams interest you?", ["Data", "Finance", "Operations"]), false);
  assert.strictEqual(ax.allowsOne("Pick one location", ["Remote", "Austin"]), true);
});

test("verifyForm: a none answer ticked with others; two consent boxes both ticked are fine; self-identification stays hidden", async () => {
  const box = (i, g, q, option, checked, extra) => Object.assign({ index: i, kind: "checkbox", label: option, option, group: g, question: q,
    checked, required: false, disabled: false, files: [], line: `[${i}]<checkbox label="${option}" checked=${checked}>` }, extra || {});
  let r = ax.verifyForm([box(1, "a", "Which teams interest you?", "Data", true), box(2, "a", "Which teams interest you?", "Finance", false),
                         box(3, "a", "Which teams interest you?", "None of the above", true)], {});
  assert.deepStrictEqual(kinds(r), ["many-ticked"]);
  r = ax.verifyForm([box(1, "c", "", "I agree to the privacy notice", true), box(2, "c", "", "I agree to be contacted", true)], {});
  assert.ok(r.ok, JSON.stringify(r));
  // A self-identification line never gains a checked state, whatever the tree says.
  const e = Object.assign(box(4, "s", "Gender", "Woman", true), { selfId: true, mixed: true, line: '[4]<checkbox label="Woman" checked=hidden>' });
  const t = ax.annotate(e.line, [e], {});
  assert.strictEqual(t, '[4]<checkbox label="Woman" checked=hidden>');
});

test("the flag: SJ_AX=1 turns it on, SJ_AX=0 off, unset is the default", async () => {
  const was = process.env.SJ_AX;
  try {
    process.env.SJ_AX = "1"; assert.strictEqual(ax.on(), true);
    process.env.SJ_AX = "0"; assert.strictEqual(ax.on(), false);
    delete process.env.SJ_AX; assert.strictEqual(ax.on(), ax.DEFAULT_ON);
  } finally { if (was === undefined) delete process.env.SJ_AX; else process.env.SJ_AX = was; }
});

// ------------------------------------------------------------------ runner
(async () => {
  const { chromium } = findPlaywright();
  const a = await serve(FIXTURES), b = await serve(FIXTURES);
  const A = "http://127.0.0.1:" + a.address().port, B = b.address().port;
  const port = await freePort();
  const browser = await chromium.launch({ args: ["--remote-debugging-port=" + port, "--site-per-process"] });
  const ctx = {
    B, port,
    open: async (fixture) => {
      const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
      await page.goto(A + "/" + fixture);
      await page.waitForLoadState("load");
      await page.waitForTimeout(100);
      return page;
    },
  };
  let passed = 0, failed = 0;
  const only = process.argv[2];
  try {
    for (const t of tests) {
      if (only && !t.name.includes(only)) continue;
      try { await t.fn(ctx); passed++; console.log("ok   " + t.name); }
      catch (e) { failed++; console.log("FAIL " + t.name + "\n     " + String(e && e.stack || e).split("\n").slice(0, 8).join("\n     ")); }
    }
  } finally {
    await browser.close();
    a.close(); b.close();
  }
  console.log(`${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
