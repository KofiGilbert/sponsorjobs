// After the Submit, on a real Workday tenant (2026-11-19), under node with fake panes
// (tests/test_after_submit.py runs it). Two things went wrong there:
//  1. The person pressed the site's own Submit while the bar said Ready to send. Workday went to
//     .../jobTasks/completed/application with an "Application Submitted" modal over Candidate
//     Home, and nothing noticed: the record stayed approved and the bar kept its Send button.
//     Now the pane is watched while a job waits at Ready to send, the same way as after the
//     shell's own Send (shell-electron/sent_watch.js, agent.js armReadyWatch), and the Workday
//     recipe knows the page (autoapply/recipes.json).
//  2. Right after, the Browser panel stayed open but showed solid black: the shell had closed its
//     pane (the live-interview teardown on every sidebar click) and the page ignored the
//     pane:state that said so. Now the two always agree (shell-electron/pane_view.js,
//     ui/static/pane_sync.js), and a pane coming into view always has its size override cleared.
// Everyone and every site here is made up.
"use strict";
const os = require("os");
const fs = require("fs");
const path = require("path");
process.env.TMPDIR = fs.mkdtempSync(path.join(os.tmpdir(), "sj-after-submit-"));
process.env.SJ_AGENT_PANE_MS = "300";
process.env.SJ_AGENT_FILL_MS = "300";
process.env.SJ_AGENT_RETRY_MS = "20";
const ROOT = path.join(__dirname, "..", "..");
const SHELL = path.join(ROOT, "shell-electron");
const { createAgent } = require(path.join(SHELL, "agent.js"));
const { sentVerdict, SENT_PROBE_JS, HOME_AFTER_SUBMIT_MS } = require(path.join(SHELL, "sent_watch.js"));
const paneView = require(path.join(SHELL, "pane_view.js"));
const focus = require(path.join(SHELL, "focus_emulation.js"));
const { panelOnState } = require(path.join(ROOT, "ui", "static", "pane_sync.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const eq = (a, b, m) => assert(JSON.stringify(a) === JSON.stringify(b), `${m}: got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

const HOST = "brightwater.wd5.myworkdayjobs.com";
const APPLY = "https://" + HOST + "/en-US/External/job/Analyst_R-101/apply/applyManually";
const DONE = "https://" + HOST + "/en-US/External/jobTasks/completed/application";
const HOME = "https://" + HOST + "/en-US/External/userHome";

// ---- 1a. the verdict, pure ------------------------------------------------------------------
{
  const now = 1_000_000_000, since = now - 60_000;
  const o = { host: HOST, since, now };
  eq(sentVerdict({ url: DONE, phrase: "Application Submitted" }, o), { how: "url", phrase: "Application Submitted" }, "Workday's completed-application page");
  eq(sentVerdict({ url: "https://brightwater.myworkdaysite.com/recruiting/x/jobTasks/completed/application" }, o), { how: "url", phrase: "" }, "myworkdaysite too");
  assert(sentVerdict({ url: "https://evil.example/jobTasks/completed/application" }, o) === null, "only on a Workday host");
  assert(sentVerdict({ url: "https://" + HOST + "/x/jobTasks/completed/applications-list" }, o) === null, "only that path");
  eq(sentVerdict({ url: APPLY, success: true, phrase: "" }, o), { how: "marker", phrase: "" }, "the recipe's marker");
  // Candidate Home right after a Submit press on this site
  const home = { url: HOME, home: true, submitShown: false, clickedAt: now - 3000 };
  eq(sentVerdict(home, o), { how: "home", phrase: "" }, "Candidate Home after the person's Submit");
  assert(sentVerdict(Object.assign({}, home, { clickedAt: 0 }), o) === null, "Candidate Home with no Submit press: the person may just have looked");
  assert(sentVerdict(Object.assign({}, home, { clickedAt: since - 5000 }), o) === null, "a press from before this watch (an older application)");
  assert(sentVerdict(Object.assign({}, home, { clickedAt: now - HOME_AFTER_SUBMIT_MS - 1 }), o) === null, "too long ago");
  assert(sentVerdict(Object.assign({}, home, { submitShown: true }), o) === null, "a Submit still on the page: not sent");
  assert(sentVerdict(Object.assign({}, home, { url: "https://other.wd1.myworkdayjobs.com/x/userHome" }), o) === null, "another site");
  assert(sentVerdict({ url: APPLY, submitShown: true }, o) === null && sentVerdict(null, o) === null && sentVerdict(true, o) === null, "nothing else");
  // the probe is one expression the page can run
  new Function("return " + SENT_PROBE_JS);
  assert(/__sjSubmitAt/.test(SENT_PROBE_JS) && /sessionStorage/.test(SENT_PROBE_JS), "the Submit press survives a navigation on the site");
}

// ---- 1b. the Workday recipe's markers, in the engine ----------------------------------------
{
  const page = { text: "", dialogs: [], submit: false };
  const body = { get textContent() { return page.text; }, querySelectorAll: () => [], querySelector: () => null };
  const dialogEl = (text) => ({ textContent: text, getAttribute: () => null, ownerDocument: null,
                                getBoundingClientRect: () => ({ width: 400, height: 200 }) });
  const btn = { textContent: "Submit", getAttribute: () => null, disabled: false, readOnly: false,
                getBoundingClientRect: () => ({ width: 80, height: 30, left: 10, top: 10, right: 90, bottom: 40 }) };
  global.window = { getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }), addEventListener() {},
                    innerWidth: 1280, innerHeight: 900, scrollX: 0, scrollY: 0 };
  global.document = {
    body, documentElement: body, readyState: "complete", addEventListener() {}, getElementById: () => null, querySelector: () => null,
    querySelectorAll: (sel) => (/role='dialog'/.test(sel) ? page.dialogs.map(dialogEl)
      : /pageFooterNextButton|bottom-navigation-next-button/.test(sel) && page.submit ? [btn] : []),
  };
  global.location = { hostname: HOST, href: APPLY };
  global.CSS = { escape: (s) => String(s) };
  global.HTMLInputElement = class {}; global.HTMLSelectElement = class {}; global.HTMLTextAreaElement = class {};
  require(path.join(ROOT, "extension", "autofill_core.js"));
  const F = window.TailorAutofill;
  const seen = (url) => F.detectSuccess(F.recipeFor(url), url);
  assert(F.recipeFor(DONE) && F.recipeFor(DONE).id === "workday", "the Workday recipe");
  assert(seen(DONE) === true, "the completed-application URL, before any words");
  assert(seen(APPLY) === false, "the application itself is not sent");
  page.dialogs = ["Application Submitted Thank you for your interest in joining our team. You have no more tasks. Return to Home"];
  assert(seen(HOME) === true, "the Application Submitted dialog over Candidate Home");
  page.dialogs = ["Your session is about to expire"];
  assert(seen(HOME) === false, "another dialog is not a confirmation");
  page.dialogs = [];
  page.text = "Welcome, Dana My Tasks You have no tasks. My Applications";
  assert(seen(HOME) === false, "Candidate Home's words alone are not a confirmation (the shell's watch decides that one)");
  delete global.window; delete global.document; delete global.location;
}

// ---- 1c. the shell: Ready to send, then the person's own Submit -----------------------------
function fakePane(P) {
  const handlers = {};
  const wc = {
    getURL: () => P.url, isDestroyed: () => false, session: null, reload: () => {}, getZoomFactor: () => 1,
    on: (ev, fn) => { (handlers[ev] = handlers[ev] || []).push(fn); },
    removeListener: (ev, fn) => { handlers[ev] = (handlers[ev] || []).filter((f) => f !== fn); },
    emit: (ev) => { for (const f of (handlers[ev] || []).slice()) f(); },
    listeners: (ev) => (handlers[ev] || []).length,
    navigationHistory: { canGoBack: () => false, goBack: () => {}, getActiveIndex: () => 0, getEntryAtIndex: () => ({ url: P.url }) },
    sendInputEvent: (ev) => { if (ev.type === "mouseUp") { P.clicks.push(P.url); P.url = DONE; P.page = "done"; } },
    executeJavaScript: async (expr) => {
      if (expr.includes("__sjSubmitWatch")) {
        P.probes++;
        if (P.page === "done") return { url: P.url, success: false, phrase: "Application Submitted Thank you for your interest", home: true, submitShown: false, clickedAt: P.clickedAt };
        if (P.page === "home") return { url: P.url, success: false, phrase: "", home: true, submitShown: false, clickedAt: P.clickedAt };
        if (P.page === "dialog") return { url: P.url, success: true, phrase: "Application Submitted", home: false, submitShown: false, clickedAt: 0 };
        return { url: P.url, success: false, phrase: "", home: false, submitShown: true, clickedAt: 0 };
      }
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet.at")) return "5000|complete";
      if (expr.includes("pageSignature")) return P.page === "review" ? { labels: ["review"] } : { success: true };
      if (expr.includes("submitButton()")) return { x: 100, y: 100, label: "Submit" };
      if (expr.includes("termsItems")) return [];
      if (expr.includes("crossOriginFrames")) return 0;
      if (expr.includes("resumeInputOpen") || expr.includes("hasResumeUpload")) return false;
      if (expr.includes("pageShape")) return { fields: 3, buttons: 1, skeleton: false, words: 60 };
      if (expr.includes("TailorAgent.view(")) return { text: "URL: " + P.url + "\nReview\n[40]<button>Submit</button>" };
      if (expr.includes("TailorAutofill.leftover()")) return [];
      if (expr.includes("pageCheck")) return { required_empty: [], errors: [] };
      if (expr.includes("innerText || el.value")) return "Submit";
      return true;
    },
  };
  return { webContents: wc };
}
function setup() {
  const P = { url: "", page: "review", clicks: [], probes: 0, clickedAt: 0 };
  let pane = fakePane(P);
  const posts = [], progress = [];
  const deps = {
    noPolling: true,
    getPane: () => pane,
    openInPane: (u) => { P.url = u; P.page = "review"; },
    waitPaneLoad: async () => {},
    autofillCore: () => "", agentViewSource: () => "",
    runAutoApplyOnPane: async () => ({ ok: true, filled: 0, left: [] }),
    sendProgress: (info) => progress.push(JSON.parse(JSON.stringify(info))),
    acquireDebugger: () => null, frames: {},
    applyDecision: async () => ({ fill: true, click_submit: false }),
    isNewPage: () => true, attachResume: async () => false,
    getApp: async (p) => (p.startsWith("/api/autoapply/agent/parked") ? { ok: true, parked: [] }
      : p === "/api/autoapply/mode" ? { ok: true, mode: "assisted", telegram_asks: false }
      : { hosts: [HOST, "myworkdayjobs.com"] }),
    postApp: async (p, b) => {
      posts.push([p, JSON.parse(JSON.stringify(b || {}))]);
      if (p === "/api/autoapply/agent/step") return { ok: true, summary: "Done", actions: [], done: { outcome: "ready_to_submit" }, cost: 0.01 };
      if (p === "/api/autoapply/agent/critic") return { ok: true, issues: [] };
      if (p === "/api/autofill/applied") return { ok: true, matched: true, record_id: b.record_id };
      return { ok: true };
    },
  };
  const agent = createAgent(deps);
  return { agent, posts, progress, P, wc: () => pane.webContents, swapPane: () => { pane = fakePane(P); } };
}
const of = (posts, p_) => posts.filter(([p]) => p === p_).map(([, b]) => b);

(async () => {
  // The owner's case: Ready to send, the person presses Workday's own Submit, Workday goes to
  // the completed-application page.
  {
    const s = setup();
    const r = await s.agent.run(7, { url: APPLY, lane: "assisted", company: "Brightwater" });
    assert(r && r.ready === true, "Ready to send " + JSON.stringify(r));
    await delay(50);
    const w = s.agent._test.readyWatch();
    assert(w && w.recordId === 7, "the pane is watched from Ready to send on");
    assert(s.wc().listeners("did-navigate") > 0, "on every navigation of the pane");
    assert(s.agent.isReady(7), "still ready");
    assert(!(await w.tick()) && s.agent.isReady(7), "the review page itself is not sent");
    // the person presses the site's Submit (not the bar's Send): the page navigates
    s.P.url = DONE; s.P.page = "done";
    s.wc().emit("did-navigate");
    await delay(1500);
    const applied = of(s.posts, "/api/autofill/applied");
    eq(applied.length, 1, "recorded applied once");
    assert(applied[0].record_id === 7 && applied[0].page_url === DONE && !("by" in applied[0]), "as the person's own send " + JSON.stringify(applied[0]));
    assert(of(s.posts, "/api/autoapply/agent/ready").some((b) => b.clear && b.record_id === 7), "the Ready to send notice is cleared");
    const sent = s.progress.filter((x) => x.state === "submitted");
    eq(sent.length, 1, "one Sent line");
    assert(sent[0].record_id === 7 && sent[0].message === "Sent, and the site confirmed it." && sent[0].confirmed === true && sent[0].by === "person",
           "the bar's Sent state " + JSON.stringify(sent[0]));
    const last = sent[0].activity && sent[0].activity.events && sent[0].activity.events.slice(-1)[0];
    assert(last && last.verb === "Sent" && /Application Submitted/.test(last.detail || ""), "Steps names the site's words " + JSON.stringify(last));
    assert(!s.agent.isReady(7) && s.agent._test.readyWatch() === null, "no longer ready, the watch is gone");
    eq(s.P.clicks.length, 0, "the shell pressed nothing");
    assert(s.wc().listeners("did-navigate") === 0, "its listeners are removed");
    // the bar's Send now: nothing to send again
    const again = await s.agent.send(7);
    assert(again && again.ok === false, "Send after the person sent it does nothing more " + JSON.stringify(again));
  }
  // Candidate Home right after the person's Submit press (the completed page went by unseen)
  {
    const s = setup();
    await s.agent.run(8, { url: APPLY });
    await delay(50);                                       // the watch's first look, at Ready to send
    const w = s.agent._test.readyWatch();
    s.P.url = HOME; s.P.page = "home"; s.P.clickedAt = Date.now();
    assert(await w.tick(), "sent");
    assert(of(s.posts, "/api/autofill/applied").length === 1 && s.progress.some((x) => x.state === "submitted" && x.how === "home"), "Candidate Home after the press");
  }
  // Candidate Home with no Submit press: the person only looked; still ready
  {
    const s = setup();
    await s.agent.run(9, { url: APPLY });
    await delay(50);                                       // the watch's first look, at Ready to send
    const w = s.agent._test.readyWatch();
    s.P.url = HOME; s.P.page = "home"; s.P.clickedAt = 0;
    assert(!(await w.tick()), "not sent");
    assert(s.agent.isReady(9) && of(s.posts, "/api/autofill/applied").length === 0 && s.agent._test.readyWatch() === w, "still ready and watched");
  }
  // An open "Application Submitted" dialog (the recipe's marker, read in the page)
  {
    const s = setup();
    await s.agent.run(10, { url: APPLY });
    await delay(50);
    s.P.page = "dialog";
    assert(await s.agent._test.readyWatch().tick(), "sent by the dialog");
    assert(s.progress.some((x) => x.state === "submitted" && x.how === "marker" && x.phrase === "Application Submitted"), "says so");
  }
  // The bar's own Send: one applied, not two (the watch stands down), and the same Workday page
  // counts as the site's confirmation
  {
    const s = setup();
    await s.agent.run(11, { url: APPLY });
    const r = await s.agent.send(11);
    assert(r && r.submitted && r.confirmed, "sent and confirmed " + JSON.stringify(r));
    eq(of(s.posts, "/api/autofill/applied").length, 1, "one applied");
    assert(s.agent._test.readyWatch() === null, "the watch stood down");
    eq(s.progress.filter((x) => x.state === "submitted").length, 1, "one Sent line");
  }
  // Another run takes the pane: the watch stands down and never marks the old job
  {
    const s = setup();
    await s.agent.run(12, { url: APPLY });
    await delay(50);
    const w = s.agent._test.readyWatch();
    await s.agent.run(13, { url: APPLY.replace("R-101", "R-202") });
    assert(s.agent._test.readyWatch() !== w, "the old watch is gone");
    s.P.url = DONE; s.P.page = "done";
    assert(!(await w.tick()), "the old watch does nothing");
    const a = of(s.posts, "/api/autofill/applied");
    assert(a.every((b) => b.record_id !== 12), "job 12 is never marked from job 13's page " + JSON.stringify(a));
  }
  // The pane was torn down (a new pane since): the watch stands down
  {
    const s = setup();
    await s.agent.run(14, { url: APPLY });
    await delay(50);
    const w = s.agent._test.readyWatch();
    s.swapPane();
    s.P.url = DONE; s.P.page = "done";
    assert(!(await w.tick()) && s.agent._test.readyWatch() === null, "a gone pane is not watched");
  }

  // ---- 2. the pane and the Browser panel agree -----------------------------------------------
  // the layout rules
  {
    const slot = { x: 900, y: 80, width: 633, height: 616 };
    eq(paneView.paneLayout({ open: true, quiet: false, slot, zoom: 1 }), { rect: { x: 900, y: 80, width: 633, height: 616 }, visible: true }, "open with a slot");
    eq(paneView.paneLayout({ open: true, quiet: false, slot, zoom: 0.76 }).rect, { x: 684, y: 61, width: 481, height: 468 }, "scaled by the app's zoom");
    eq(paneView.paneLayout({ open: false, quiet: false, slot, zoom: 1 }), { rect: paneView.ZERO, visible: false }, "closed: no size");
    eq(paneView.paneLayout({ open: true, quiet: false, slot: { x: 0, y: 0, width: 0, height: 0 }, zoom: 1 }).visible, false, "a slot with no size");
    eq(paneView.paneLayout({ open: true, quiet: true, slot, zoom: 1 }), { rect: paneView.QUIET_BOUNDS, visible: false }, "a quiet run: parked");
    // the page shows its Browser panel again while a live pane is closed: open it again
    const zero = { x: 0, y: 0, width: 0, height: 0 };
    assert(paneView.reopenOnSlot({ before: zero, slot, hasPane: true, open: false, quiet: false }), "reopened");
    assert(!paneView.reopenOnSlot({ before: slot, slot: Object.assign({}, slot, { width: 700 }), hasPane: true, open: false, quiet: false }), "a resize is not a reopen");
    assert(!paneView.reopenOnSlot({ before: zero, slot, hasPane: false, open: false, quiet: false }), "no pane, nothing to open");
    assert(!paneView.reopenOnSlot({ before: zero, slot, hasPane: true, open: false, quiet: true }), "never a quiet run's pane");
    assert(!paneView.reopenOnSlot({ before: null, slot, hasPane: true, open: true, quiet: false }), "already open");
  }
  // the page's side
  {
    eq(panelOnState(false, { open: true }, false), "open", "the shell opens its pane: the Browser panel opens");
    eq(panelOnState(true, { open: false }, true), "close", "the shell closes its pane: the Browser panel closes");
    eq(panelOnState(true, { open: false }, false), "", "the panel shows the resume: left alone");
    eq(panelOnState(false, { open: false }, true), "", "already closed (the start, a quiet run): a panel the person opened stays");
  }
  // The owner's sequence, with a fake pane and a fake debugger wired the way main.js wires them:
  // the pane is shown, a size override is left on it, the shell closes it (the old sidebar
  // teardown), the page is told and closes its panel, the person opens the Browser panel again.
  {
    const page = { w: 633, h: 616, vis: "visible" };
    const dbg = { attached: true, sent: [], isAttached() { return this.attached; },
                  sendCommand: async (m) => { dbg.sent.push(m); if (m === "Emulation.clearDeviceMetricsOverride") { page.w = 633; page.h = 616; } return {}; } };
    const S = { pane: { bounds: null, setBounds(r) { this.bounds = r; page.vis = r.width > 0 ? "visible" : "hidden"; } },
                open: false, quiet: false, slot: null, shellOpen: false, panelOpen: false, states: [] };
    const sync = paneView.createPaneSync({ setBounds: (r) => S.pane.setBounds(r),
                                           onShown: () => { if (dbg.isAttached()) focus.show(dbg, { always: true }); } });
    const layout = () => sync.layout({ open: S.open, quiet: S.quiet, slot: S.slot, zoom: 1 });
    const push = () => {                                   // pushPaneState -> the page's onState
      const st = { open: S.open && !S.quiet };
      S.states.push(st.open);
      const act = panelOnState(S.shellOpen, st, S.panelOpen);
      S.shellOpen = st.open;
      if (act === "open") { S.panelOpen = true; setBounds({ x: 900, y: 80, width: 633, height: 616 }); }
      if (act === "close") { S.panelOpen = false; setBounds({ x: 0, y: 0, width: 0, height: 0 }); }
    };
    const setBounds = (rect) => {                          // ipcMain "pane:set-bounds"
      const before = S.slot;
      S.slot = rect;
      if (paneView.reopenOnSlot({ before, slot: rect, hasPane: true, open: S.open, quiet: S.quiet })) { S.open = true; layout(); push(); return; }
      layout();
    };
    // openInPane (the run opens the application)
    S.open = true; layout(); push();
    assert(S.panelOpen && page.vis === "visible" && S.pane.bounds.width === 633, "the run's page is on screen");
    eq(dbg.sent.filter((m) => m === "Emulation.clearDeviceMetricsOverride").length, 1, "shown: any size override is cleared");
    // a size override left on the visible pane (a quiet run's, say)
    page.w = 1280; page.h = 900;
    // closePane (pane:close from the old live-interview teardown)
    S.open = false; layout(); push();
    assert(!S.panelOpen, "the page closes its Browser panel: no black slot");
    assert(S.pane.bounds.width === 0 && page.vis === "hidden", "the pane is out of sight with it");
    // the person opens the Browser panel again: the same page, visible, at its own size
    S.panelOpen = true; setBounds({ x: 900, y: 80, width: 633, height: 616 });
    assert(S.open && S.states[S.states.length - 1] === true, "the shell opened its pane again and said so");
    assert(page.vis === "visible" && S.pane.bounds.width === 633, "visible at the slot");
    await delay(0);
    eq(dbg.sent.filter((m) => m === "Emulation.clearDeviceMetricsOverride").length, 2, "shown again: cleared again");
    eq([page.w, page.h], [633, 616], "its own size");
    // a resize of the open panel lays out without a second clear
    setBounds({ x: 880, y: 80, width: 653, height: 616 });
    eq(dbg.sent.filter((m) => m === "Emulation.clearDeviceMetricsOverride").length, 2, "a resize sends nothing");
    // a quiet run's pane never comes into view by itself
    S.quiet = true; S.open = true; layout();
    eq(S.pane.bounds, paneView.QUIET_BOUNDS, "parked");
  }
  // focus_emulation.show: only when it set a size, unless told always
  {
    const sent = [];
    const d = { sendCommand: async (m) => { sent.push(m); return {}; } };
    assert(await focus.show(d) === false && sent.length === 0, "nothing set, nothing sent");
    assert(await focus.show(d, { always: true }) === true && sent[0] === "Emulation.clearDeviceMetricsOverride", "always: sent");
  }

  // ---- the wiring in the real files (main.js and app.js run only in Electron) ----------------
  {
    const main = fs.readFileSync(path.join(SHELL, "main.js"), "utf8");
    assert(/paneView\.createPaneSync\(/.test(main) && /onShown: \(\) => paneShown\(\)/.test(main), "main.js lays the pane out through pane_view.js");
    assert(/focusEmulation\.show\(wc\.debugger, \{ always: true \}\)/.test(main), "main.js clears the size override when the pane is shown");
    const sb = main.slice(main.indexOf('ipcMain.on("pane:set-bounds"'), main.indexOf('ipcMain.on("pane:set-bounds"') + 700);
    assert(/reopenOnSlot\(/.test(sb) && /pushPaneState\(\)/.test(sb), "pane:set-bounds reopens a closed pane the panel shows, and says so");
    const app = fs.readFileSync(path.join(ROOT, "ui", "static", "app.js"), "utf8");
    const td = app.slice(app.indexOf("function teardownCvi()"), app.indexOf("async function endCvi("));
    assert(/if \(CVI_IN_PANE && window\.tailorShell\) window\.tailorShell\.closeBrowser\(\)/.test(td), "the interview teardown closes the pane only when the interview used it");
    assert(!/^\s*if \(window\.tailorShell\) window\.tailorShell\.closeBrowser\(\);/m.test(td), "never unconditionally");
    assert(/SJPaneSync\.panelOnState\(shellOpen, st, onBrowser\)/.test(app) && /else if \(act === "close"\) close\(\)/.test(app), "the panel follows pane:state both ways");
    const html = fs.readFileSync(path.join(ROOT, "ui", "templates", "index.html"), "utf8");
    assert(html.indexOf("pane_sync.js") > 0 && html.indexOf("pane_sync.js") < html.indexOf("filename='app.js'"), "pane_sync.js loads before app.js");
    const agent = fs.readFileSync(path.join(SHELL, "agent.js"), "utf8");
    assert(/armReadyWatch\(recordId, wc\);\s*\n\s*setTimeout\(\(\) => \{ pollParked/.test(agent), "the watch is armed at Ready to send");
    const DASHES = new RegExp("[" + String.fromCharCode(0x2013, 0x2014) + "]");
    for (const f of [path.join(SHELL, "sent_watch.js"), path.join(SHELL, "pane_view.js"), path.join(ROOT, "ui", "static", "pane_sync.js"), __filename]) {
      assert(!DASHES.test(fs.readFileSync(f, "utf8")), "no em or en dash in " + path.basename(f));
    }
  }
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
