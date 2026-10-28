// The apply agent's loop: "Apply for me" (docs/apply-agent.md).
//
// Drives the in-app browser pane the way a person would: opens the job, clicks Apply, signs in
// or creates the portal account, works through every page, uploads the resume, and STOPS at
// Submit. Each step: let the page settle, check the guards (captcha, sign-in, left the site,
// success), run the deterministic fill engine, serialize the page for the model, ask the brain
// (/api/autoapply/agent/step) what to click next, do it, record one history line.
//
// The model never presses Submit: it reports "ready_to_submit" and the SHELL decides (send()
// below): a verified auto-submit site with Autonomous submit on is pressed here; everywhere else
// the person presses Send. A captcha, a phone code or an email verification stops the run and
// says so. No evasion of any kind (CLAUDE.md section 7).
//
// Kept apart from main.js so the loop is readable on its own; main.js hands in the pane and the
// helpers it already has (`deps`), the same way frames.js takes a CDP adapter.
"use strict";

const MAX_STEPS = 40;
const MAX_FAILED_STEPS = 4;          // in a row
const STEP_BRAIN_MS = 60000;
const ACTION_MS = 25000;
const SETTLE_QUIET_MS = 600;
const SETTLE_CAP_MS = 8000;
const LOOP_WINDOW = 12;
const SUBMIT_RE = /^(submit( application)?|send( application)?|review and submit|complete application)$/i;
// Enough to keep running when the engine's allowed-hosts route is unreachable.
// Web Bot Auth (docs/apply-agent.md section 7, Cloudflare signed agents): while a run is on, the
// page requests the agent makes to the job site carry a signature that says "this is the
// SponsorJobs agent, acting for a signed-in person". The private key lives in the broker; the
// shell asks the engine for the three headers per document request and goes on unsigned when
// that is slow or unavailable. Honest identification, never evasion.
const SIGN_MS = 1500;
const SIGNED_TYPES = new Set(["mainFrame", "subFrame"]);
const ATS_HOSTS = ["greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com",
  "recruitee.com", "myworkdayjobs.com", "icims.com", "taleo.net", "successfactors.com", "jobvite.com",
  "bamboohr.com", "paylocity.com", "ultipro.com", "adp.com"];

const delay = (ms) => new Promise((r) => setTimeout(r, ms));
// SJ_AGENT_DEBUG=1 writes each step's view and reply to <tmp>/sj-agent.log (the e2e reads it).
const DEBUG = process.env.SJ_AGENT_DEBUG === "1";
const dlog = (what, obj) => {
  if (!DEBUG) return;
  try {
    require("fs").appendFileSync(require("path").join(require("os").tmpdir(), "sj-agent.log"),
      new Date().toISOString() + " " + what + " " + (typeof obj === "string" ? obj : JSON.stringify(obj)) + "\n");
  } catch (e) { /* debugging only */ }
};
const hostOf = (url) => { try { return (new URL(url).hostname || "").toLowerCase().replace(/^www\./, ""); } catch (e) { return ""; } };
const withTimeout = (p, ms, what) => Promise.race([p, delay(ms).then(() => { throw new Error(what + " timed out"); })]);

// A MutationObserver that stamps the last DOM change, so "the page went quiet" is measurable.
// Idempotent per document; a navigation drops it and the next view() puts it back.
const QUIET_JS = `(() => {
  if (window.__tailorQuiet) return true;
  window.__tailorQuiet = { at: Date.now() };
  new MutationObserver(() => { window.__tailorQuiet.at = Date.now(); })
    .observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
  return true;
})()`;
const QUIET_READ = "(window.__tailorQuiet ? Date.now() - window.__tailorQuiet.at : 0) + '|' + document.readyState";

function createAgent(deps) {
  // deps: { getPane, openInPane, waitPaneLoad, autofillCore, agentViewSource, runAutoApplyOnPane,
  //         postApp, getApp, sendProgress, acquireDebugger, frames, applyDecision, net, SERVER }
  let current = null;                 // the one run at a time: { recordId, stop, ready }
  const ready = new Map();            // recordId -> { url, lane } packages waiting for Send

  const progress = (recordId, state, message, extra) =>
    deps.sendProgress(Object.assign({ agent: true, record_id: recordId == null ? null : recordId,
                                      state, message }, extra || {}));

  // ---- the pane's scripts ------------------------------------------------------------------
  async function ensureScripts(wc) {
    const has = await wc.executeJavaScript("!!window.TailorAutofill && !!window.TailorAgent", true);
    if (!has) {
      await wc.executeJavaScript(deps.autofillCore(), true);
      await wc.executeJavaScript(deps.agentViewSource(), true);
    }
    await wc.executeJavaScript(QUIET_JS, true);
  }

  // Wait for the page to settle after an action: a navigation finished, or no DOM change for
  // SETTLE_QUIET_MS, or the cap. Returns which one fired. (browser-use has no such wait; a flat
  // nap is why the Workday page once looked blank.)
  async function settle(wc, urlBefore) {
    const t0 = Date.now();
    let navigated = false;
    const onNav = () => { navigated = true; };
    wc.on("did-start-navigation", onNav);
    try {
      while (Date.now() - t0 < SETTLE_CAP_MS) {
        await delay(150);
        if (wc.isDestroyed()) return "gone";
        if (navigated || (wc.getURL() || "") !== urlBefore) {
          try { await deps.waitPaneLoad(SETTLE_CAP_MS); } catch (e) { /* cap below */ }
          await delay(400);                                   // let the SPA hydrate
          return "navigated";
        }
        let q = "";
        try { q = await wc.executeJavaScript(QUIET_READ, true); } catch (e) { continue; }
        const [since, state] = String(q).split("|");
        if (state === "complete" && Number(since) >= SETTLE_QUIET_MS && Date.now() - t0 > 300) return "quiet";
      }
      return "cap";
    } finally {
      try { wc.removeListener("did-start-navigation", onNav); } catch (e) { /* gone */ }
    }
  }

  // ---- guards ------------------------------------------------------------------------------
  async function allowedHosts(applyUrl) {
    const host = hostOf(applyUrl);
    const parts = host.split(".");
    const base = new Set([host, parts.slice(-2).join("."), ...ATS_HOSTS]);
    try {
      const r = await deps.getApp("/api/autoapply/agent/allowed-hosts?url=" + encodeURIComponent(applyUrl));
      for (const h of (r && r.hosts) || []) base.add(String(h).toLowerCase());
    } catch (e) { /* the built-in list */ }
    base.delete("");
    return base;
  }
  const onSite = (hosts, url) => {
    const h = hostOf(url);
    if (!h) return url === "about:blank" || url === "";
    for (const a of hosts) if (h === a || h.endsWith("." + a)) return true;
    return false;
  };

  async function signature(wc) {
    try { return await wc.executeJavaScript("window.TailorAutofill.pageSignature()", true); } catch (e) { return null; }
  }

  // ---- the page view, main frame + cross-origin frames --------------------------------------
  async function openFrames(wc) {
    let count = 0;
    try { count = await wc.executeJavaScript("window.TailorAutofill.crossOriginFrames()", true); } catch (e) { count = 0; }
    if (!count) return null;
    const release = deps.acquireDebugger(wc);
    if (!release) return null;
    const cdp = deps.frames.electronCdp(wc.debugger);
    const close = () => { cdp.dispose(); release(); };
    try {
      const ctxs = [];
      for (const ctx of await deps.frames.crossOriginFrames(cdp)) {
        const d = await deps.applyDecision(ctx.url, "");
        if (!d.fill) continue;                              // a prohibited board's frame: hands off
        try {
          await deps.frames.injectEngine(cdp, ctx, deps.autofillCore());
          await deps.frames.injectEngine(cdp, ctx, deps.agentViewSource());
          ctxs.push(ctx);
        } catch (e) { /* the frame navigated away */ }
      }
      if (!ctxs.length) { close(); return null; }
      return { cdp, ctxs, close, eval: (ctx, expr) => deps.frames.evalIn(cdp, ctx, expr).catch(() => null) };
    } catch (e) { close(); return null; }
  }

  const indicesIn = (text) => {
    const out = [];
    const re = /^\*?\[(\d+)\]</gm;
    let m;
    while ((m = re.exec(text))) out.push(Number(m[1]));
    return out;
  };
  async function view(wc, fr) {
    const main = await wc.executeJavaScript("window.TailorAgent.view({ indexBase: 1 })", true);
    let text = (main && main.text) || "";
    const indices = new Set(indicesIn(text));
    const frameOf = new Map();                              // index -> ctx
    for (let i = 0; i < ((fr && fr.ctxs) || []).length; i++) {
      const ctx = fr.ctxs[i];
      const base = 1000 * (i + 1);
      const v = await fr.eval(ctx, `window.TailorAgent.view({ indexBase: ${base} })`);
      if (!v || !v.text) continue;
      text += "\n--- embedded frame " + (i + 1) + " ---\n" + v.text;
      for (const n of indicesIn(v.text)) { indices.add(n); frameOf.set(n, ctx); }
    }
    return { text: text.slice(0, 14000), indices, frameOf };
  }

  // The element's own words (to refuse a Submit click here as well as in the page) and the
  // file input an upload should land in: the element itself, a descendant, a sibling, or one
  // up to three ancestors away, else the page's first file input.
  const TEXT_JS = (i) => `(() => { const el = window.TailorAgent.element(${i});
    return el ? String(el.innerText || el.value || el.getAttribute("aria-label") || "").trim().slice(0, 80) : ""; })()`;
  const FILE_JS = (i) => `(() => {
    const isFile = (x) => x && x.tagName === "INPUT" && (x.type || "").toLowerCase() === "file";
    const el = window.TailorAgent.element(${i});
    if (!el) return null;
    if (isFile(el)) return el;
    const inside = el.querySelector && el.querySelector('input[type="file"]'); if (inside) return inside;
    let node = el;
    for (let up = 0; node && up < 3; up++) {
      node = node.parentElement; if (!node) break;
      const f = node.querySelector && node.querySelector('input[type="file"]'); if (f) return f;
    }
    return document.querySelector('input[type="file"]') || null; })()`;

  // ---- actions -----------------------------------------------------------------------------
  // Routed by index range to the main frame or the embedded frame it came from. Upload goes
  // through CDP (script cannot set a file input). The in-page act() refuses Submit-like buttons
  // on its own; this side refuses them too (belt and braces, docs section 6).
  function viewLine(viewText, index) {
    const re = new RegExp("^\\*?\\[" + Number(index) + "\\]<.*$", "m");
    const m = re.exec(String(viewText || ""));
    return m ? m[0] : "";
  }

  // A real pointer and keyboard for the main frame, through CDP: Workday's prompt widgets and
  // other React controls ignore a programmatic value and a synthetic click, and honour what a
  // person's mouse and keys do. The in-page act() stays the fallback (and the frame path).
  async function realClick(wc, index) {
    const release = deps.acquireDebugger(wc);
    if (!release) return null;
    try {
      const r = await wc.executeJavaScript(`(() => { const el = window.TailorAgent.element(${index}); if (!el) return null;
        el.scrollIntoView({ block: "center", inline: "nearest" }); const b = el.getBoundingClientRect();
        return { x: b.left + b.width / 2, y: b.top + b.height / 2, w: b.width, h: b.height }; })()`, true);
      if (!r || !(r.w > 0 && r.h > 0)) return null;
      const dbg = wc.debugger;
      await dbg.sendCommand("Input.dispatchMouseEvent", { type: "mouseMoved", x: r.x, y: r.y });
      await dbg.sendCommand("Input.dispatchMouseEvent", { type: "mousePressed", x: r.x, y: r.y, button: "left", clickCount: 1 });
      await dbg.sendCommand("Input.dispatchMouseEvent", { type: "mouseReleased", x: r.x, y: r.y, button: "left", clickCount: 1 });
      return { ok: true, note: "" };
    } catch (e) { return null; }
    finally { release(); }
  }
  async function realType(wc, index, text) {
    const release = deps.acquireDebugger(wc);
    if (!release) return null;
    try {
      const focused = await wc.executeJavaScript(`(() => { const el = window.TailorAgent.element(${index}); if (!el) return false;
        el.scrollIntoView({ block: "center", inline: "nearest" }); el.focus();
        if (typeof el.select === "function") { try { el.select(); } catch (e) {} }
        return document.activeElement === el || el.contains(document.activeElement); })()`, true);
      if (!focused) return null;
      const dbg = wc.debugger;
      // Replace whatever is there, then type for real: every key raises the events a person's would.
      await dbg.sendCommand("Input.dispatchKeyEvent", { type: "keyDown", key: "a", code: "KeyA", modifiers: process.platform === "darwin" ? 4 : 2, commands: ["selectAll"] });
      await dbg.sendCommand("Input.dispatchKeyEvent", { type: "keyUp", key: "a", code: "KeyA" });
      await dbg.sendCommand("Input.dispatchKeyEvent", { type: "keyDown", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 });
      await dbg.sendCommand("Input.dispatchKeyEvent", { type: "keyUp", key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 });
      for (const ch of String(text)) {
        await dbg.sendCommand("Input.dispatchKeyEvent", { type: "keyDown", key: ch, text: ch, unmodifiedText: ch });
        await dbg.sendCommand("Input.dispatchKeyEvent", { type: "keyUp", key: ch });
      }
      const now = await wc.executeJavaScript(`(() => { const el = window.TailorAgent.element(${index}); return el ? String(el.value != null ? el.value : el.textContent || "") : ""; })()`, true);
      return { ok: true, note: String(now || "") === String(text) ? "" : "value differs" };
    } catch (e) { return null; }
    finally { release(); }
  }

  async function act(wc, fr, v, a, resumePath) {
    const expr = `window.TailorAgent.act(${JSON.stringify(a)})`;
    const ctx = v.frameOf.get(a.index);
    if (a.op === "upload") return upload(wc, fr, ctx, a.index, resumePath);
    // Where a person's real pointer and keys matter: typeahead boxes (type for real so the
    // list opens) and the options they show (click for real). Plain fields and buttons keep
    // the in-page path, which is faster and already proven on the mock and live forms.
    const line = viewLine(v.text, a.index);
    if (!ctx && a.op === "click") {
      const label = await wc.executeJavaScript(TEXT_JS(a.index), true).catch(() => "");
      if (SUBMIT_RE.test(String(label || "").trim())) return { ok: false, note: "submit-guard" };
      if (/^\*?\[\d+\]<(option|menuitem|li|div|span)\b/.test(line) || /role=(option|menuitem)/.test(line)) {
        const real = await realClick(wc, a.index);
        if (real) return real;
      }
    }
    if (!ctx && a.op === "type" && typeof a.text === "string" && /<combobox\b|aria-autocomplete|search=true/.test(line)) {
      const real = await realType(wc, a.index, a.text);
      if (real) return real;
    }
    if (a.op === "click") {
      const label = await (ctx ? fr.eval(ctx, TEXT_JS(a.index))
                               : wc.executeJavaScript(TEXT_JS(a.index), true).catch(() => ""));
      if (SUBMIT_RE.test(String(label || "").trim())) return { ok: false, note: "submit-guard" };
    }
    const run = ctx ? fr.eval(ctx, expr) : wc.executeJavaScript(expr, true);
    let out;
    try { out = (await withTimeout(run, ACTION_MS, a.op)) || { ok: false, note: "no result" }; }
    catch (e) { out = { ok: false, note: String(e && e.message || e).slice(0, 120) }; }
    if (!ctx && a.op === "click" && out && /covered/.test(out.note || "")) {
      const real = await realClick(wc, a.index);              // the pointer lands where the eye does
      if (real) return real;
    }
    return out;
  }

  async function upload(wc, fr, ctx, index, resumePath) {
    if (!resumePath) return { ok: false, note: "no resume file" };
    const finder = FILE_JS(index);
    if (ctx) {
      try {
        const r = await fr.cdp.send("Runtime.evaluate", { expression: finder, contextId: ctx.contextId }, ctx.sessionId);
        const objectId = r && r.result && r.result.objectId;
        if (!objectId) return { ok: false, note: "no file input near " + index };
        await fr.cdp.send("DOM.setFileInputFiles", { files: [resumePath], objectId }, ctx.sessionId);
        return { ok: true, note: "uploaded" };
      } catch (e) { return { ok: false, note: "upload failed" }; }
    }
    const release = deps.acquireDebugger(wc);
    if (!release) return { ok: false, note: "devtools holds the debugger" };
    try {
      const dbg = wc.debugger;
      await dbg.sendCommand("DOM.enable");
      const ev = await dbg.sendCommand("Runtime.evaluate", { expression: finder });
      const objectId = ev && ev.result && ev.result.objectId;
      if (!objectId) return { ok: false, note: "no file input near " + index };
      await dbg.sendCommand("DOM.setFileInputFiles", { files: [resumePath], objectId });
      return { ok: true, note: "uploaded" };
    } catch (e) { return { ok: false, note: "upload failed" }; }
    finally { release(); }
  }

  // ---- request signing ---------------------------------------------------------------------
  function armSigning(wc, hosts) {
    if (!deps.sign) return () => {};
    const ses = wc.session;
    ses.webRequest.onBeforeSendHeaders({ urls: ["https://*/*"] }, (details, callback) => {
      if (!SIGNED_TYPES.has(details.resourceType) || !onSite(hosts, details.url)) { callback({}); return; }
      let u;
      try { u = new URL(details.url); } catch (e) { callback({}); return; }
      const req = { method: details.method || "GET", authority: u.host, path: u.pathname + (u.search || "") };
      withTimeout(deps.sign(req), SIGN_MS, "sign").then((h) => {
        if (!h || !h.signature || !h.signature_input) { callback({}); return; }
        const requestHeaders = Object.assign({}, details.requestHeaders, {
          "Signature-Input": h.signature_input, "Signature": h.signature,
          "Signature-Agent": h.signature_agent || '"https://sponsorjobs.ai"',
        });
        callback({ requestHeaders });
      }).catch(() => callback({}));
    });
    return () => { try { ses.webRequest.onBeforeSendHeaders(null); } catch (e) { /* gone */ } };
  }

  // What to ask: the engine's open fields, else the fields the page's own errors name ("The
  // field X is required"), else empty required boxes in the view. Labels only, never values.
  function questionsFor(viewText, left, includeFilled) {
    const out = [];
    const add = (q) => { q = String(q || "").trim(); if (q && !out.includes(q)) out.push(q); };
    for (const x of left || []) add(typeof x === "string" ? x : (x && x.label) || "");
    const text = String(viewText || "");
    let m;
    const errRe = /Error: "(?:The field )?(.+?) (?:is required|must have a value|is a required field|cannot be blank)/g;
    while ((m = errRe.exec(text))) add(m[1].replace(/^"|"$/g, ""));
    if (!out.length) {
      const reqRe = /<(?:input|combobox|select|textarea)[^>]*label="([^"]*)"[^>]*value=""[^>]*required>/g;
      while ((m = reqRe.exec(text))) add(m[1]);
    }
    // Never re-ask what is already in its box: a stale "is required" error can outlive the fill.
    const filled = (label) => {
      const esc = label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      return new RegExp('label="' + esc + '"[^\n]*(value="\\(filled\\)"|checked=true)').test(text);
    };
    return (includeFilled ? out : out.filter((q) => !filled(q))).slice(0, 3);
  }

  // The choices a page offers for a field, read off the view: a select's first options or the
  // radio options that share the label. Empty when the field is free text or a typeahead.
  function optionsFor(viewText, label) {
    const want = String(label || "").trim().toLowerCase();
    const out = [];
    for (const line of String(viewText || "").split("\n")) {
      const m = /label="([^"]*)"/.exec(line);
      if (!m || m[1].trim().toLowerCase() !== want) continue;
      const first = /first="([^"]*)"/.exec(line);
      if (first) { for (const o of first[1].split("|")) if (o && !/^select/i.test(o)) out.push(o); }
      const opt = /<radio [^>]*option="([^"]*)"/.exec(line);
      if (opt && !out.includes(opt[1])) out.push(opt[1]);
    }
    return out.slice(0, 8);
  }

  // ---- the run -----------------------------------------------------------------------------
  // opts: { url, resumePath, lane, autonomous }
  async function run(recordId, opts) {
    opts = opts || {};
    if (current) return { ok: false, reason: "busy" };
    const applyUrl = opts.url || "";
    if (!/^https?:/i.test(applyUrl)) return { ok: false, reason: "bad-url" };
    if (/(^|\.)linkedin\.com$/i.test(hostOf(applyUrl))) { deps.openInPane(applyUrl); return { ok: false, reason: "linkedin" }; }
    const decision = await deps.applyDecision(applyUrl, "");
    if (!decision.fill) { deps.openInPane(applyUrl); return { ok: false, reason: "fill-by-hand" }; }

    const st = { recordId, stop: false, disarmSigning: () => {} };
    current = st;
    const hosts = await allowedHosts(applyUrl);
    const history = [];
    const portals = new Set();           // hosts whose portal account was ensured this run
    const startedAt = Date.now() / 1000; // mail older than the run is never used
    let emailCode = "";                  // a code the mailbox handed over, for the next step only
    const hashes = [];
    const pageHashes = [];
    let failed = 0;
    let page = 1;
    let fr = null;
    let disarmSigning = () => {};
    const finish = (res) => { if (fr) { fr.close(); fr = null; } disarmSigning(); current = null; return res; };
    const stopped = (reason, message, extra) => {
      progress(recordId, "stopped", message, Object.assign({ reason, steps: history.length }, extra));
      return finish({ ok: false, reason, steps: history.length });
    };

    try {
      progress(recordId, "running", "Opening the job");
      deps.openInPane(applyUrl);
      { const p0 = deps.getPane(); if (p0) { disarmSigning = armSigning(p0.webContents, hosts); st.disarmSigning = disarmSigning; } }
      await delay(400);
      await deps.waitPaneLoad();
      const pane = deps.getPane();
      if (!pane) return stopped("no-page", "The browser pane is closed.");
      const wc = pane.webContents;
      wc.on("will-prevent-unload", (e) => e.preventDefault());    // "leave page?" never blocks the agent
      await settle(wc, "");

      for (let step = 1; step <= MAX_STEPS; step++) {
        if (st.stop) return stopped("stopped", "Stopped.");
        if (wc.isDestroyed() || deps.getPane() !== pane) return stopped("no-page", "The browser pane closed.");
        const url = wc.getURL() || "";
        if (!onSite(hosts, url)) return stopped("left-site", "The site sent me somewhere else (" + hostOf(url) + "). Stopped.");
        await ensureScripts(wc);
        const sig = (await signature(wc)) || {};
        if (sig.success) {
          progress(recordId, "submitted", "The site confirmed the application.", { steps: step });
          await deps.postApp("/api/autofill/applied", { url: applyUrl, page_url: url, record_id: recordId });
          return finish({ ok: true, submitted: true, steps: step });
        }
        if (sig.blocker === "captcha") return stopped("captcha", "This page has a captcha. Solve it, then press Apply for me again.");

        // 1. the deterministic engine first (not on a sign-in page: the portal account goes
        //    through the brain's placeholders, never the profile fill). At the first sign-in or
        //    create-account wall of a host, make sure a portal account exists for it (an existing
        //    one is returned as is, a new one gets a generated password, saved locally); the brain
        //    then fills either form with {{portal_email}} / {{portal_password}}. The engine
        //    refuses when the person has turned "Create portal accounts for me" off, and the
        //    brain reports blocked (needs-person) in that case.
        let filled = 0, left = [];
        if (sig.blocker === "auth") {
          const h = hostOf(url);
          if (!portals.has(h)) {
            portals.add(h);
            progress(recordId, "running", "Signing in", { step });
            await deps.postApp("/api/autoapply/agent/portal-account", { host: h, create: true });
          }
        } else {
          const rep = await deps.runAutoApplyOnPane(recordId, { page, fromWatch: true, quiet: true });
          dlog("step " + step + " fill:", rep);
          // "Could not plan this form" still names what is open: keep its left list either way.
          if (rep && rep.ok) filled = rep.filled || 0;
          left = (rep && rep.left) || [];
          if (rep && rep.uploaded) filled += 1;
          else if (opts.resumePath) {
            // The page has a resume box the plan did not reach (a hidden input behind an
            // Upload button): attach the file to it directly, as assisted apply does.
            let wants = false;
            try { wants = await wc.executeJavaScript("window.TailorAutofill.hasResumeUpload()", true); } catch (e) { wants = false; }
            if (wants && await deps.attachResume(wc, opts.resumePath)) filled += 1;
          }
        }
        if (st.stop) return stopped("stopped", "Stopped.");

        // 2. the view
        if (fr) { fr.close(); fr = null; }
        fr = await openFrames(wc);
        const v = await view(wc, fr);
        dlog("step " + step + " url=" + url + " blocker=" + (sig.blocker || "") + " filled=" + filled + " view:", v.text.slice(0, 600));
        const pageHash = hostOf(url) + "|" + String(v.text.length) + "|" + v.text.slice(0, 400);
        pageHashes.push(pageHash);
        if (pageHashes.length >= 4 && pageHashes.slice(-4).every((h) => h === pageHash) && !filled) {
          return stopped("stuck", "I could not get past this page. Have a look at it.", { url });
        }

        // The engine's open list, plus the fields the page's own errors name: a value the engine
        // typed into a widget that ignored it reads as filled, while the site still says empty.
        for (const q of questionsFor(v.text, [], true)) if (!left.includes(q)) left.push(q);

        // 3. the brain
        const nudges = [];
        const last = hashes.slice(-LOOP_WINDOW);
        const repeats = (h) => last.filter((x) => x === h).length;
        const worst = Math.max(0, ...last.map(repeats));
        if (worst >= 5) return stopped("stuck", "I kept repeating the same action without progress. Have a look.", { url });
        if (worst >= 3) nudges.push("You repeated the same action " + worst + " times and it is not working. Try something else or report blocked.");
        progress(recordId, "running", step === 1 ? "Reading the job page" : "Page " + page + ": deciding what to click",
                 { step, filled });
        let reply = null;
        try {
          reply = await withTimeout(deps.postApp("/api/autoapply/agent/step", {
            record_id: recordId, step, max_steps: MAX_STEPS, url, lane: opts.lane || "assisted",
            view: v.text, filled_this_step: filled, left, nudges, blocker: sig.blocker || "",
            history: history.slice(-12), email_code: emailCode,
          }), STEP_BRAIN_MS, "brain");
        } catch (e) { reply = null; }
        dlog("step " + step + " reply:", reply);
        if (!reply || reply.ok === false) {
          if (reply && reply.reason === "fill-by-hand") return stopped("fill-by-hand", "This site does not allow automated filling.");
          failed++;
          history.push({ step, evaluation: "", memory: "", actions: [], results: ["no reply from the brain"] });
          if (failed >= MAX_FAILED_STEPS) return stopped("stuck", "I could not work out this page. Have a look at it.", { url });
          continue;
        }

        // 4. done?
        const done = reply.done;
        if (done && done.outcome === "ready_to_submit") return handOver(recordId, wc, url, opts, step);
        if (done && done.outcome === "submitted") {
          progress(recordId, "submitted", "Submitted.", { steps: step });
          await deps.postApp("/api/autofill/applied", { url: applyUrl, page_url: url, record_id: recordId });
          return finish({ ok: true, submitted: true, steps: step });
        }
        const asks = (done && done.outcome === "blocked" && done.reason === "needs-answers") ? questionsFor(v.text, left) : [];
        if (asks.length) {
          // Ask the person, on the bar and on Telegram, and carry on with the answer (saved for
          // every later application). Up to three questions, five minutes each.
          const qs = asks;
          let answered = 0;
          for (const q of qs) {
            if (st.stop) return stopped("stopped", "Stopped.");
            const options = optionsFor(v.text, q);
            progress(recordId, "ask", "The application asks: " + q, { step, ask: { question: q, options } });
            const r = await withTimeout(deps.postApp("/api/autoapply/agent/ask", {
              question: q, options, timeout_s: 300, record_id: recordId }), 320000, "ask").catch(() => null);
            dlog("step " + step + " ask ->", r && { ok: r.ok, reason: r.reason });
            if (r && r.ok) answered++;
            else break;
          }
          if (!answered) return stopped("needs-answers", blockedMessage("needs-answers", left), { url, left });
          history.push({ step, evaluation: reply.evaluation || "", memory: reply.memory || "",
                         actions: ["asked the person"], results: [answered + " answered"] });
          progress(recordId, "running", "Thanks. Carrying on", { step });
          continue;
        }
        if (done && done.outcome === "blocked") {
          return stopped(done.reason || "needs-person", blockedMessage(done.reason, left), { url, left });
        }
        if (done && done.outcome === "await_email") {
          // The site emailed a code or a link. With a mailbox connected the engine watches the
          // person's own inbox for THIS site's message (inbox/await, read-only, consent given in
          // Settings); without one the run stops and says what to connect.
          progress(recordId, "running", "Waiting for the site's email", { step });
          const got = await deps.postApp("/api/inbox/await", {
            kind: "any", host: hostOf(url), company: opts.company || "", since: startedAt, timeout_s: 180 });
          dlog("step " + step + " await ->", got && { ok: got.ok, kind: got.kind, reason: got.reason, link: !!got.link, code: !!got.code });
          if (!got || got.ok === false) {
            if (got && got.reason === "not-connected") {
              return stopped("verify-email", "The site emailed you a " + (done.reason === "code" ? "code" : "link")
                + ". Connect your email in Settings so this finishes by itself, or do that step yourself and press Apply for me again.", { url });
            }
            return stopped("verify-email", "The site's email did not arrive in 3 minutes. Check your inbox, then press Apply for me again.", { url });
          }
          if (got.code) { emailCode = String(got.code); history.push({ step, evaluation: reply.evaluation || "", memory: reply.memory || "", actions: ["await email"], results: ["got the code"] }); continue; }
          if (got.link) {
            const before = wc.getURL() || "";
            hosts.add(hostOf(got.link));                   // the link belongs to this site's mail
            deps.openInPane(got.link);
            await delay(400);
            await settle(wc, before);
            history.push({ step, evaluation: reply.evaluation || "", memory: reply.memory || "", actions: ["await email"], results: ["opened the emailed link"] });
            continue;
          }
          return stopped("verify-email", "The site's email had no link or code I could use.", { url });
        }

        // 5. the actions, in order; the queue stops when the page changes
        const results = [];
        const names = [];
        let stepFailed = false;
        for (const a of reply.actions || []) {
          if (st.stop) return stopped("stopped", "Stopped.");
          const h = [a.op, a.index, a.sensitive ? "(secret)" : (a.text || a.option || a.direction || "")].join("|");
          hashes.push(h);
          names.push(a.op + " " + (a.index || "") + (a.sensitive ? " (secret)" : a.text ? " " + JSON.stringify(a.text).slice(0, 40) : a.option ? " " + a.option : ""));
          const before = wc.getURL() || "";
          const r = await act(wc, fr, v, a, opts.resumePath || "");
          if (a.placeholder === "email_code" && r && r.ok) emailCode = "";
          results.push(r && r.ok ? (r.note ? "ok, " + r.note : "ok") : "failed: " + ((r && r.note) || "?"));
          dlog("step " + step + " act " + h + " ->", r);
          if (!(r && r.ok)) { stepFailed = true; if (r && r.note === "submit-guard") return handOver(recordId, wc, url, opts, step); continue; }
          if (a.op === "click" || a.op === "wait") {
            const why = await settle(wc, before);
            if (why === "navigated" || (wc.getURL() || "") !== before) { results[results.length - 1] += ", page changed"; break; }
          }
        }
        if (!(reply.actions || []).length) stepFailed = true;
        failed = stepFailed ? failed + 1 : 0;
        if (failed >= MAX_FAILED_STEPS) return stopped("stuck", "I could not get past this page. Have a look at it.", { url });
        // A new page of the form (new URL, or the fields changed) advances the page counter.
        const after = (await signature(wc)) || {};
        if (sig.labels && after.labels && deps.isNewPage(sig.labels, after)) page++;
        else if ((wc.getURL() || "") !== url && (after.labels || []).length) page++;
        history.push({ step, evaluation: reply.evaluation || "", memory: reply.memory || "", actions: names, results });
      }
      return stopped("stuck", "I ran out of steps on this one. Have a look at it.", {});
    } catch (e) {
      console.error("apply agent:", e && e.stack || e);
      return stopped("error", "Something went wrong: " + String(e && e.message || e).slice(0, 100));
    }
  }

  // ---- Submit: the shell's decision, never the model's --------------------------------------
  async function handOver(recordId, wc, url, opts, step) {
    if (current && current.disarmSigning) current.disarmSigning();
    const now = await deps.applyDecision(opts.url || url, url);
    const auto = !!opts.autonomous && now.click_submit === true;
    ready.set(recordId, { url, lane: auto ? "auto" : "assisted" });
    if (auto) {
      progress(recordId, "running", "Sending", { step });
      const r = await send(recordId);
      current = null;
      return r;
    }
    progress(recordId, "ready", "Ready to send. Check it, then press Send.", { step, ready: true });
    current = null;
    return { ok: true, ready: true, steps: step };
  }

  // Press the site's Submit for a package the agent left ready. A real input event at the
  // button's centre (not the in-page click, which refuses Submit on purpose).
  async function send(recordId) {
    const pane = deps.getPane();
    const r = ready.get(recordId);
    if (!pane || !r) return { ok: false, reason: "not-ready" };
    const wc = pane.webContents;
    await ensureScripts(wc);
    const sig = (await signature(wc)) || {};
    if (sig.blocker === "captcha") return { ok: false, reason: "captcha" };
    const box = await wc.executeJavaScript(`(() => {
      const i = window.TailorAgent.submitButton(); if (!i) return null;
      const el = window.TailorAgent.element(i); el.scrollIntoView({ block: "center" });
      const b = el.getBoundingClientRect(); return { x: b.left + b.width / 2, y: b.top + b.height / 2, label: (el.innerText || el.value || "").trim() };
    })()`, true).catch(() => null);
    if (!box) return { ok: false, reason: "no-submit-button" };
    const zoom = wc.getZoomFactor ? wc.getZoomFactor() : 1;
    const x = Math.round(box.x * zoom), y = Math.round(box.y * zoom);
    const url = wc.getURL() || "";
    wc.sendInputEvent({ type: "mouseMove", x, y });
    wc.sendInputEvent({ type: "mouseDown", x, y, button: "left", clickCount: 1 });
    wc.sendInputEvent({ type: "mouseUp", x, y, button: "left", clickCount: 1 });
    ready.delete(recordId);
    await settle(wc, url);
    let confirmed = false;
    for (let i = 0; i < 6 && !confirmed; i++) {        // the site's "received" marker, up to ~8 s
      try { await ensureScripts(wc); confirmed = !!(await signature(wc) || {}).success; } catch (e) { confirmed = false; }
      if (!confirmed) await delay(1200);
    }
    const after = (await signature(wc)) || {};
    if (!confirmed && after.blocker === "captcha") {
      ready.set(recordId, r);
      progress(recordId, "ready", "The submit hit a captcha. Solve it on the page, then press Send again.", { ready: true });
      return { ok: false, reason: "captcha" };
    }
    await deps.postApp("/api/autofill/applied", { url: r.url, page_url: wc.getURL() || "", record_id: recordId });
    progress(recordId, "submitted", confirmed ? "Sent, and the site confirmed it." : "Sent. The page showed no confirmation, so check it once.",
             { confirmed, clicked: box.label });
    return { ok: true, submitted: true, confirmed };
  }

  function stop() { if (current) current.stop = true; }
  const isReady = (recordId) => ready.has(recordId);

  return { run, send, stop, isReady, MAX_STEPS };
}

function blockedMessage(reason, left) {
  switch (reason) {
    case "needs-answers": {
      const qs = (left || []).map((x) => (typeof x === "string" ? x : (x && x.label) || "")).filter(Boolean).slice(0, 3);
      const list = qs.length ? " " + qs.map((q) => "\u201c" + q.slice(0, 90) + "\u201d").join(", ") + "." : "";
      return "This application needs " + (qs.length === 1 ? "an answer" : "answers") + " only you can give:" + list
        + " Add " + (qs.length === 1 ? "it" : "them") + " under Settings, Applying, Your answers, then press Apply for me again.";
    }
    case "captcha": return "This page has a captcha. Solve it, then press Apply for me again.";
    case "verify-email": return "The site sent you a verification email. Click its link, then press Apply for me again.";
    case "phone-code": return "The site wants a code from your phone. Enter it, then press Apply for me again.";
    case "not-a-job": return "This link does not lead to an application.";
    case "left-site": return "The site sent me somewhere else. Stopped.";
    default: return "This one needs you. Have a look at the page.";
  }
}

module.exports = { createAgent, blockedMessage, SUBMIT_RE, ATS_HOSTS, MAX_STEPS };
