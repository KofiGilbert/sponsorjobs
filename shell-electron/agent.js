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
// Asking the person (2026-10-31): an ask waits as long as the engine says (an hour by default,
// with one Telegram reminder), in long-polls of ASK_CHUNK_S so Stop still ends it within seconds;
// past the wait the job PARKS (the question stays open, the pane is released) and an answer
// re-runs it. A batch never waits: its asks park at once and go out in ONE digest.
const ASK_CHUNK_S = 20;
const PARK_POLL_MS = Number(process.env.SJ_AGENT_PARK_POLL_MS) || 4000;
const DIGEST_MS = Number(process.env.SJ_AGENT_DIGEST_MS) || 30 * 60 * 1000;
const PARK_REASONS = new Set(["captcha", "phone-code", "needs-person"]);
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
const withTimeout = (p, ms, what) => {
  let t = null;
  const cap = new Promise((_r, rej) => { t = setTimeout(() => rej(new Error(what + " timed out")), ms); });
  return Promise.race([p, cap]).finally(() => clearTimeout(t));
};

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
  const ready = new Map();            // recordId -> { url, lane, epoch } packages waiting for Send
  // Parked jobs this shell still remembers (recordId as a string -> { epoch, pageUrl, opts,
  // state, kind, opened }): a re-run carries on from the same page with its state when the pane
  // is still on it (paneEpoch: bumped each time a run or a Continue opens a page in the pane);
  // else (the pane moved, or the app restarted) it opens the job again.
  const parked = new Map();
  let paneEpoch = 0;
  let batchOn = null;                 // { stop } while a batch runs: resumes wait behind it

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
      // An element inside a same-origin frame (the view walks into those): its box is relative to
      // that frame, so each frame's own place on the page above it is added.
      const r = await wc.executeJavaScript(`(() => { const el = window.TailorAgent.element(${index}); if (!el) return null;
        el.scrollIntoView({ block: "center", inline: "nearest" }); const b = el.getBoundingClientRect();
        let x = b.left + b.width / 2, y = b.top + b.height / 2;
        let w = el.ownerDocument && el.ownerDocument.defaultView;
        for (let i = 0; w && w !== window && i < 8; i++) {
          const f = w.frameElement; if (!f) return null;
          const fb = f.getBoundingClientRect(); x += fb.left + f.clientLeft; y += fb.top + f.clientTop;
          w = f.ownerDocument && f.ownerDocument.defaultView;
        }
        if (w !== window) return null;
        return { x, y, w: b.width, h: b.height }; })()`, true);
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
      // Leave the box the way a person does (Tab away): Workday commits a textarea on blur.
      const now = await wc.executeJavaScript(`(() => { const el = window.TailorAgent.element(${index}); if (!el) return "";
        const val = String(el.value != null ? el.value : el.textContent || "");
        if (!/combobox/i.test(el.getAttribute("role") || "") && !el.getAttribute("aria-autocomplete")) { try { el.blur(); } catch (e) {} }
        return val; })()`, true);
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
    if (!ctx && a.op === "type" && typeof a.text === "string" && /<combobox\b|<textarea\b|aria-autocomplete|search=true/.test(line)) {
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

  // Every choice of a native <select> (the view prints only its first four), read from the
  // element itself, in its own frame. null when the field is not a select or cannot be read.
  async function selectChoices(wc, fr, v, label) {
    const idx = indexFor(v.text, label);
    if (!idx || !/^\*?\[\d+\]<select\b/.test(viewLine(v.text, idx))) return null;
    const ctx = v.frameOf.get(idx);
    const expr = `window.TailorAgent.selectOptions(${idx})`;
    let r = null;
    try { r = ctx ? (fr ? await fr.eval(ctx, expr) : null) : await wc.executeJavaScript(expr, true); } catch (e) { r = null; }
    if (!r || !Array.isArray(r.options) || !r.options.length) return null;
    return { options: r.options.map((o) => String(o)).filter(Boolean), partial: !!r.partial };
  }

  // A custom dropdown (role=combobox, a button that opens a listbox) shows its choices only
  // while open. Mark what is on screen, open it with a real click (unless it is open already),
  // read the popup THAT click opened (a "Loading..." list is waited out for up to 3 s), and
  // close it again. The person is then offered the page's own words. Main frame only; no
  // choices when it is not such a dropdown or nothing showed.
  async function dropdownOptions(wc, v, label) {
    const none = { options: [], partial: false };
    const idx = indexFor(v.text, label);
    if (!idx || v.frameOf.has(idx)) return none;
    if (!/^\*?\[\d+\]<combobox\b/.test(viewLine(v.text, idx))) return none;
    const js = (expr) => wc.executeJavaScript(expr, true);
    let read = null;
    try {
      // Another dropdown still open (Phenom keeps one open through Escape and blur) would cover
      // this control, so the click below would land on ITS list: press it shut first.
      await js(`window.TailorAgent.closeOthers(${idx})`).catch(() => 0);
      const was = !!(await js(`window.TailorAgent.popupOpen(${idx})`).catch(() => false));
      await js(`window.TailorAgent.markPopups(${idx}, ${!was})`).catch(() => null);
      if (!was) {
        const opened = await realClick(wc, idx);
        if (!opened) await withTimeout(js(`window.TailorAgent.act(${JSON.stringify({ op: "click", index: idx })})`), ACTION_MS, "open");
      }
      for (let i = 0; i < 8; i++) {
        await delay(i ? 350 : 500);
        read = await js(`window.TailorAgent.popupOptions(${idx})`).catch(() => null);
        if (!read || !read.busy) break;
      }
    } catch (e) { read = null; }
    await closeDropdown(wc, idx);
    if (!read || read.busy || !Array.isArray(read.options)) return none;
    return { options: read.options.map((o) => String(o)).filter(Boolean), partial: !!read.partial };
  }
  // Close what the read opened: ONE real Escape, and only while the popup is really open. An
  // Escape that reaches a closed control can close the dialog that holds the form, so a click
  // that opened nothing gets no key at all. A widget that ignores Escape is clicked shut or
  // blurred (TailorAgent.closePopup), never sent more Escapes; the in-page Escape is used only
  // when the real one could not be pressed. One that ignores both (Phenom's select at Lam
  // Research stays open, its list over the next field) is pressed shut from just outside it.
  async function closeDropdown(wc, idx) {
    const isOpen = () => wc.executeJavaScript(`window.TailorAgent.popupOpen(${idx})`, true).catch(() => false);
    const close = (how) => wc.executeJavaScript(`window.TailorAgent.closePopup(${idx}, ${JSON.stringify(how)})`, true).catch(() => false);
    if (!(await isOpen())) return true;
    const sent = await pressEscape(wc);
    await delay(150);
    if (!(await isOpen())) return true;
    await close(sent ? "blur" : "escape");
    await delay(150);
    if (!(await isOpen())) return true;
    if (!sent) {
      await close("blur");
      await delay(150);
      if (!(await isOpen())) return true;
    }
    await close("outside");
    await delay(200);
    return !(await isOpen());
  }

  // ---- the person's saved self-identification answers, put on the page by the shell ---------
  // The engine maps a saved answer to the page's own option in code (autoapply/eeo.py, through
  // the ask route's known_only check); the shell then picks THAT option itself with real clicks,
  // never through the model, and checks it stuck. The choice is never logged, never sent to the
  // broker or Telegram, and the view keeps printing the control as (filled).
  async function pickSaved(wc, v, label, option) {
    const idx = indexFor(v.text, label);
    if (!idx || v.frameOf.has(idx) || !option) return false;
    const js = (expr) => wc.executeJavaScript(expr, true);
    const want = JSON.stringify(String(option));
    const line = viewLine(v.text, idx);
    const chosen = async (i) => !!(await js(`window.TailorAgent.chosenIs(${i}, ${want})`).catch(() => false));
    const inPage = (a) => withTimeout(js(`window.TailorAgent.act(${JSON.stringify(a)})`), ACTION_MS, a.op).catch(() => null);
    try {
      if (/^\*?\[\d+\]<select\b/.test(line)) {
        await inPage({ op: "select", index: idx, option: String(option) });
        return await chosen(idx);
      }
      if (/^\*?\[\d+\]<radio\b/.test(line)) {
        // the radio of this group whose own words are the option
        const hit = linesFor(v.text, label).find((ln) => /^\*?\[\d+\]<radio\b/.test(ln)
          && unq((/option="((?:[^"\\]|\\.)*)"/.exec(ln) || [])[1] || "") === String(option));
        const ri = hit ? Number((/^\*?\[(\d+)\]/.exec(hit) || [])[1]) : 0;
        if (!ri) return false;
        if (!(await realClick(wc, ri))) await inPage({ op: "click", index: ri });
        await delay(200);
        return await chosen(ri);
      }
      if (!/^\*?\[\d+\]<combobox\b/.test(line)) return false;
      await js(`window.TailorAgent.closeOthers(${idx})`).catch(() => 0);
      const was = !!(await js(`window.TailorAgent.popupOpen(${idx})`).catch(() => false));
      await js(`window.TailorAgent.markPopups(${idx}, ${!was})`).catch(() => null);
      if (!was && !(await realClick(wc, idx))) await inPage({ op: "click", index: idx });
      let oi = null;
      for (let i = 0; i < 8 && !oi; i++) {
        await delay(i ? 350 : 400);
        oi = await js(`window.TailorAgent.optionIndex(${idx}, ${want})`).catch(() => null);
      }
      if (oi) {
        if (!(await realClick(wc, oi))) await inPage({ op: "click", index: oi });
        await delay(300);
      }
      await closeDropdown(wc, idx);
      if (await chosen(idx)) return true;
      // The engine's own widget path (open, filter, click, verify) as the second try.
      await inPage({ op: "select", index: idx, option: String(option) });
      await closeDropdown(wc, idx);
      return await chosen(idx);
    } catch (e) { return false; }
  }

  // Required self-identification questions still open on this page whose saved answer the
  // engine maps to one of the page's options: picked by the shell (pickSaved). Each question is
  // tried once per run (a widget that refuses goes on to the brain and, if need be, the person).
  // Returns the questions it filled; the caller logs only how many.
  async function fillSavedEeo(wc, fr, v, left, recordId, tried, st) {
    const qs = questionsFor(v.text, left).filter((q) => EEO_RE.test(q) && !tried.has(q));
    if (!qs.length) return [];
    const items = [];
    for (const q of qs) {
      if (st.stop) return [];
      tried.add(q);
      const got = (await selectChoices(wc, fr, v, q)) || optionsFor(v.text, q) || (await dropdownOptions(wc, v, q));
      const all = cleanOptions(got.options, 1000);
      if (!all.length) continue;                       // the page's own choices are needed to pick one
      items.push({ question: q, options: orderOptions(q, all), all_options: all.slice(0, 40),
                   partial: !!got.partial || all.length > 40, hint: noteAbove(v.text, q) });
    }
    if (!items.length) return [];
    const k = await deps.postApp("/api/autoapply/agent/ask", { questions: items, record_id: recordId, known_only: true }).catch(() => null);
    const pick = (k && k.select) || {};
    const done = [];
    for (const it of items) {
      if (st.stop) break;
      if (pick[it.question] && await pickSaved(wc, v, it.question, pick[it.question])) done.push(it.question);
    }
    return done;
  }

  // ---- terms a site requires to apply (a privacy notice, a data privacy agreement) ------------
  // The person decides: "always" in their answers accepts by itself; otherwise the first notice
  // on a site is asked on the bar and Telegram ("Accept for all applications", "Just this one",
  // "Stop"). Every acceptance is recorded on the application and named in the Ready to send
  // summary. Then the accept button is clicked (or the box ticked) for real. Stop ends the run
  // and nothing is accepted. Returns { stop: {reason, message} } to stop, else { accepted: n }.
  async function handleTerms(wc, run, loose) {
    let items = [];
    try { items = (await wc.executeJavaScript(`window.TailorAgent.termsItems(${loose ? "true" : "false"})`, true)) || []; } catch (e) { items = []; }
    let accepted = 0;
    for (const it of items) {
      if (run.st.stop) return { stop: { reason: "stopped", message: "Stopped." } };
      const url = wc.getURL() || "";
      const host = hostOf(url);
      const title = String(it.title || "Terms").slice(0, 80);
      const key = host + "|" + title.toLowerCase();
      const tries = (run.termsTries.get(key) || 0) + 1;
      run.termsTries.set(key, tries);
      if (tries > 2) {
        return { stop: { reason: "needs-person", message: "I could not accept " + possessive(run.company, title)
                         + " on this page. Have a look at it." } };
      }
      let via = run.termsOk.get(host) || "";
      if (!via) {
        const body = { record_id: run.recordId, title, url, kind: it.kind, company: run.company };
        const first = await deps.postApp("/api/autoapply/agent/terms", Object.assign({ check_only: true }, body)).catch(() => null);
        if (first && first.decision === "accept") via = first.via || "always";
        else {
          const id = Math.random().toString(16).slice(2, 10);
          const question = (first && first.question) || termsQuestion(run.company, title);
          const options = (first && first.options) || TERMS_CHOICES;
          const ask = { id, question, options, questions: [{ question, options }], kind: "terms" };
          // In a batch the person is away: never wait, park the job and move on (the digest
          // asks). A single run asks on the bar and Telegram and waits, then parks.
          if (!run.batch) progress(run.recordId, "ask", question, { ask });
          const r = await askLong("/api/autoapply/agent/terms", Object.assign({ id, park: run.parkInfo(), no_wait: !!run.batch }, body), run.st);
          dlog("terms ->", r && { ok: r.ok, decision: r.decision, reason: r.reason });
          if (r && r.reason === "stopped") return { stop: { reason: "stopped", message: "Stopped." } };
          if (r && r.reason === "parked") return { park: { kind: "terms", question: needsLine(run.company, title), ask } };
          if (!r || r.ok === false) {
            return { stop: { reason: "needs-consent", message: needsLine(run.company, title)
                             + " I did not accept it. Press Apply for me again to answer." } };
          }
          if (r.decision !== "accept") {
            return { stop: { reason: "terms-declined", message: "Stopped. You chose not to accept "
                             + possessive(run.company, title) + ", so I did not apply." } };
          }
          via = r.via || "once";
          progress(run.recordId, "running", "Thanks. Carrying on");
        }
        run.termsOk.set(host, via);
      }
      // The person said yes: click the accept button / tick the box like a person would.
      const before = url;
      let res = await realClick(wc, it.index);
      if (!res) res = await withTimeout(wc.executeJavaScript(`window.TailorAgent.act(${JSON.stringify({ op: "click", index: it.index })})`, true),
                                        ACTION_MS, "accept").catch(() => null);
      if (!(res && res.ok)) continue;                    // tried again on the next pass
      await settle(wc, before);
      try { await ensureScripts(wc); } catch (e) { /* the next step injects again */ }
      await deps.postApp("/api/autoapply/agent/terms/accepted", {
        record_id: run.recordId, title, url, kind: it.kind, via, company: run.company }).catch(() => null);
      if (!run.accepted.includes(title)) run.accepted.push(title);
      accepted++;
      dlog("terms", "accepted (" + it.kind + ", " + via + ")");
    }
    return { accepted };
  }
  async function pressEscape(wc) {
    const release = deps.acquireDebugger(wc);
    if (!release) return false;
    try {
      await wc.debugger.sendCommand("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
      await wc.debugger.sendCommand("Input.dispatchKeyEvent", { type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 });
      return true;
    } catch (e) { return false; }
    finally { release(); }
  }

  // ---- asking the person -------------------------------------------------------------------
  // One ask on the engine (/ask or /terms with ``park``): it answers within a chunk, or says
  // "waiting" and the shell asks again until the engine's wait is over (then "parked"). Stop
  // closes the ask between chunks. A missed reply is retried, so a hiccup never ends an hour.
  async function askLong(path, body, st) {
    const once = (p, b) => withTimeout(deps.postApp(p, b), (ASK_CHUNK_S + 60) * 1000, "ask").catch(() => null);
    let r = await once(path, Object.assign({ chunk_s: ASK_CHUNK_S }, body));
    let id = (r && r.id) || body.id;
    let misses = 0;
    while (!r || (r.ok === false && r.reason === "waiting")) {
      if (!r) { if (++misses > 3 || !id) return null; await delay(1000); } else misses = 0;
      if (st && st.stop) {
        await deps.postApp("/api/autoapply/agent/ask/close", { id }).catch(() => null);
        return { ok: false, reason: "stopped" };
      }
      r = await once("/api/autoapply/agent/ask/wait", { id, chunk_s: ASK_CHUNK_S });
      if (r && r.id) id = r.id;
    }
    return r;
  }
  const runOpts = (o) => ({ url: o.url || "", resume_path: o.resumePath || "", lane: o.lane || "assisted",
                            autonomous: !!o.autonomous, company: o.company || "" });
  const optsFrom = (r) => ({ url: (r && r.url) || "", resumePath: (r && r.resume_path) || "", lane: (r && r.lane) || "assisted",
                             autonomous: !!(r && r.autonomous), company: (r && r.company) || "" });
  const ridOf = (x) => (/^\d+$/.test(String(x)) ? Number(x) : x);

  // ---- the run -----------------------------------------------------------------------------
  // opts: { url, resumePath, lane, autonomous, company, mode?: "single" | "batch",
  //         resume?: { entry, state, samePage } }
  async function run(recordId, opts) {
    opts = opts || {};
    if (current) return { ok: false, reason: "busy" };
    if (batchOn && opts.mode !== "batch") return { ok: false, reason: "busy" };
    const batch = opts.mode === "batch";
    const R = opts.resume || null;
    const applyUrl = opts.url || "";
    if (!/^https?:/i.test(applyUrl)) return { ok: false, reason: "bad-url" };
    if (/(^|\.)linkedin\.com$/i.test(hostOf(applyUrl))) { deps.openInPane(applyUrl); return { ok: false, reason: "linkedin" }; }
    const decision = await deps.applyDecision(applyUrl, "");
    if (!decision.fill) { deps.openInPane(applyUrl); return { ok: false, reason: "fill-by-hand" }; }

    const st = { recordId, stop: false, disarmSigning: () => {} };
    current = st;
    if (!R) {
      // A fresh Apply for me on a parked job replaces the park (its question closes).
      parked.delete(String(recordId));
      await deps.postApp("/api/autoapply/agent/parked/" + encodeURIComponent(recordId) + "/take", { drop: true }).catch(() => null);
    }
    const S = (R && R.state) || null;     // the parked run's own state, when it carries on in place
    const hosts = await allowedHosts(applyUrl);
    if (S && S.hosts) for (const h of S.hosts) hosts.add(h);
    const history = (S && S.history) || [];
    const portals = (S && S.portals) || new Set();   // hosts whose portal account was ensured this run
    const startedAt = (S && S.startedAt) || Date.now() / 1000; // mail older than the run is never used
    let emailCode = "";                  // a code the mailbox handed over, for the next step only
    const runAnswers = (S && S.runAnswers) || {};    // question -> the person's answer this run (bar, Telegram)
    const knownSeen = (S && S.knownSeen) || new Set();  // questions answered from their material without asking
    const eeoTried = (S && S.eeoTried) || new Set();    // self-identification questions the shell tried to fill
    // Terms the site requires: per site, the person's yes this run (via "always" | "all" |
    // "once"), how often each notice was tried, and what was accepted (the Ready to send summary).
    const terms = (S && S.terms) || { recordId, company: String(opts.company || ""), termsTries: new Map(),
                                      termsOk: new Map(), accepted: [] };
    terms.st = st;
    terms.batch = batch;
    // What a re-run brings in: the consent (also kept on the record, or as the standing choice),
    // and the answers a parked question got (the saved ones come back from the person's material
    // anyway; a job's own answer, never saved, only from here).
    const resumeLeft = {};
    if (R && R.entry) {
      const e = R.entry;
      if (e.kind === "terms" && e.decision === "accept") {
        terms.termsOk.set(hostOf(e.page_url || applyUrl), e.via || "once");
        terms.termsTries = new Map();
      }
      for (const [q, a] of Object.entries(e.answers || {})) { runAnswers[q] = a; resumeLeft[q] = a; }
    }
    const hashes = [];
    const pageHashes = [];
    let failed = 0;
    let page = (S && S.page) || 1;
    let fr = null;
    let disarmSigning = () => {};
    let wcNow = null;
    const finish = (res) => { if (fr) { fr.close(); fr = null; } disarmSigning(); current = null; return res; };
    const stopped = (reason, message, extra) => {
      progress(recordId, "stopped", message, Object.assign({ reason, steps: history.length }, extra));
      return finish({ ok: false, reason, steps: history.length });
    };
    const where = () => (wcNow && !wcNow.isDestroyed() ? wcNow.getURL() || "" : "");
    terms.parkInfo = () => ({ page_url: where(), run: runOpts(opts), batch });
    const lead = batch && opts.batchAt ? "Application " + opts.batchAt.i + " of " + opts.batchAt.n + ": " : "";
    // Park the job: the pane is released for the next one, the question (if any) stays open, and
    // an answer (or Continue) re-runs it. ``kind`` "questions" | "terms" (the engine already parked
    // the ask) or "captcha" | "person" (parked here, nothing to answer).
    const park = async (kind, message, extra) => {
      const pageUrl = where();
      parked.set(String(recordId), { epoch: paneEpoch, pageUrl, opts: Object.assign({}, opts, { resume: null, mode: "single" }), kind,
                                     state: { history, portals, startedAt, runAnswers, knownSeen, eeoTried, terms, page, hosts: Array.from(hosts) } });
      if (kind === "captcha" || kind === "person") {
        await deps.postApp("/api/autoapply/agent/park", { record_id: recordId, kind, message, page_url: pageUrl, run: runOpts(opts), batch }).catch(() => null);
      }
      const onPage = kind === "captcha" || kind === "person";
      const said = batch ? lead + "Paused, it needs you: " + message + " Moving on."
        : (onPage ? "Paused. " : "Paused: waiting on your answer. ") + message
          + (onPage ? "" : " The question stays open: answer here or on Telegram any time and I carry on.");
      progress(recordId, "parked", said, Object.assign({ parked: { kind, batch }, steps: history.length }, extra || {}));
      dlog("parked", kind + " " + message);
      startPolling();
      return finish({ ok: false, parked: true, reason: "parked", kind, steps: history.length });
    };
    const parkFor = (reason, message, extra) => park(reason === "captcha" ? "captcha" : "person",
      reason === "captcha" ? "This page has a captcha. Solve it on the page, then press Continue."
        : reason === "phone-code" ? "The site wants a code from your phone. Enter it on the page, then press Continue."
          : String(message || "This page needs you.").replace(/ Have a look( at it| at the page)?\.?$/, "") + " Do the step on the page, then press Continue.", extra);

    try {
      let pane = null;
      if (R && R.samePage && deps.getPane()) {
        pane = deps.getPane();
        progress(recordId, "running", "Thanks. Carrying on where I paused");
        { disarmSigning = armSigning(pane.webContents, hosts); st.disarmSigning = disarmSigning; }
      } else {
        progress(recordId, "running", lead + (R ? "Thanks. Opening the job again" : "Opening the job"));
        paneEpoch++;
        deps.openInPane(applyUrl);
        { const p0 = deps.getPane(); if (p0) { disarmSigning = armSigning(p0.webContents, hosts); st.disarmSigning = disarmSigning; } }
        await delay(400);
        await deps.waitPaneLoad();
        pane = deps.getPane();
      }
      if (!pane) return stopped("no-page", "The browser pane is closed.");
      const wc = pane.webContents;
      wcNow = wc;
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
        if (sig.blocker === "captcha") return parkFor("captcha", "", { url });

        // 1. the deterministic engine first (not on a sign-in page: the portal account goes
        //    through the brain's placeholders, never the profile fill). At the first sign-in or
        //    create-account wall of a host, make sure a portal account exists for it (an existing
        //    one is returned as is, a new one gets a generated password, saved locally); the brain
        //    then fills either form with {{portal_email}} / {{portal_password}}. The engine
        //    refuses when the person has turned "Create portal accounts for me" off, and the
        //    brain reports blocked (needs-person) in that case.
        let filled = 0, left = [];
        // A notice the site needs accepted, left open by the last step's actions (a privacy
        // dialog over the form): the person's consent first, before anything is filled under it.
        {
          const t = await handleTerms(wc, terms);
          if (t.stop) return stopped(t.stop.reason, t.stop.message, { url });
          if (t.park) return park("terms", t.park.question, { url, ask: batch ? undefined : t.park.ask });
        }
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
        // The fill can open one too (Phenom's "Data Privacy Agreement" comes up when the resume
        // is attached).
        {
          const t = await handleTerms(wc, terms);
          if (t.stop) return stopped(t.stop.reason, t.stop.message, { url });
          if (t.park) return park("terms", t.park.question, { url, ask: batch ? undefined : t.park.ask });
        }

        // 2. the view
        if (fr) { fr.close(); fr = null; }
        fr = await openFrames(wc);
        let v = await view(wc, fr);
        dlog("step " + step + " url=" + url + " blocker=" + (sig.blocker || "") + " filled=" + filled + " view:", v.text.slice(0, 600));
        const pageHash = hostOf(url) + "|" + String(v.text.length) + "|" + v.text.slice(0, 400);
        pageHashes.push(pageHash);
        if (pageHashes.length >= 4 && pageHashes.slice(-4).every((h) => h === pageHash) && !filled) {
          return stopped("stuck", "I could not get past this page. Have a look at it.", { url });
        }

        // The engine's open list, plus the fields the page's own errors name: a value the engine
        // typed into a widget that ignored it reads as filled, while the site still says empty.
        for (const q of questionsFor(v.text, [], true)) if (!left.includes(q)) left.push(q);

        // Required self-identification questions with a saved answer: the shell picks them
        // itself, before the model is asked anything (it never sees or sends these choices).
        if (sig.blocker !== "auth") {
          const got = await fillSavedEeo(wc, fr, v, left, recordId, eeoTried, st);
          if (st.stop) return stopped("stopped", "Stopped.");
          if (got.length) {
            for (const q of got) knownSeen.add(q);
            left = left.filter((x) => !got.some((q) => sameLabel(typeof x === "string" ? x : (x && x.label) || "", q)));
            filled += got.length;
            dlog("step " + step + " eeo:", got.length + " filled from saved answers");
            progress(recordId, "running", "Filled from your saved answers", { step, filled });
            v = await view(wc, fr);
          }
        }

        // 3. the brain
        const nudges = [];
        const last = hashes.slice(-LOOP_WINDOW);
        const repeats = (h) => last.filter((x) => x === h).length;
        const worst = Math.max(0, ...last.map(repeats));
        if (worst >= 5) return stopped("stuck", "I kept repeating the same action without progress. Have a look.", { url });
        if (worst >= 3) nudges.push("You repeated the same action " + worst + " times and it is not working. Try something else or report blocked.");
        progress(recordId, "running", lead + (step === 1 && !R ? "Reading the job page" : "Page " + page + ": deciding what to click"),
                 { step, filled });
        let reply = null;
        try {
          reply = await withTimeout(deps.postApp("/api/autoapply/agent/step", {
            record_id: recordId, step, max_steps: MAX_STEPS, url, lane: opts.lane || "assisted",
            view: v.text, filled_this_step: filled, left, nudges, blocker: sig.blocker || "",
            history: history.slice(-12), email_code: emailCode, answers: runAnswers,
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
        if (done && done.outcome === "ready_to_submit") return handOver(recordId, wc, url, opts, step, terms.accepted);
        if (done && done.outcome === "blocked" && done.reason === "needs-consent") {
          // The model saw a notice to accept that the shell has not handled: the shell asks the
          // person and clicks, never the model.
          const t = await handleTerms(wc, terms, true);
          if (t.stop) return stopped(t.stop.reason, t.stop.message, { url });
          if (t.park) return park("terms", t.park.question, { url, ask: batch ? undefined : t.park.ask });
          if (!t.accepted) return stopped("needs-consent", blockedMessage("needs-consent"), { url });
          history.push({ step, evaluation: reply.evaluation || "", memory: reply.memory || "",
                         actions: ["(the person accepted the site's terms)"], results: ["accepted; continue"] });
          continue;
        }
        if (done && done.outcome === "submitted") {
          progress(recordId, "submitted", "Submitted.", { steps: step });
          await deps.postApp("/api/autofill/applied", { url: applyUrl, page_url: url, record_id: recordId });
          return finish({ ok: true, submitted: true, steps: step });
        }
        const asks = (done && done.outcome === "blocked" && done.reason === "needs-answers") ? questionsFor(v.text, left) : [];
        if (done && done.outcome === "blocked" && done.reason === "needs-answers" && !asks.length) {
          // Nothing required is actually open (an optional box, or a stale error over a box that
          // has since been filled): say so and let the brain continue instead of stopping.
          history.push({ step, evaluation: reply.evaluation || "", memory: reply.memory || "",
                         actions: ["(no required question is open)"], results: ["continue with the page's Next or Continue; leave optional boxes empty"] });
          continue;
        }
        if (asks.length) {
          // Ask the person, on the bar and on Telegram, and carry on with the answers (saved for
          // every later application). Every open question on the page goes in ONE message, with
          // the page's own choices: all of a native select's (read from the element), a radio
          // group's, or a custom dropdown's from its open popup. A list that was only partly
          // read says so, so an answer outside it still counts. What the person's material
          // already answers (the library, work authorization, the standing decline choice on
          // self-identification questions) is never asked.
          const items = [];
          let fromPark = 0;
          for (const q of asks) {
            if (st.stop) return stopped("stopped", "Stopped.");
            // The answer a parked question got, brought in by this re-run: used once.
            const k = Object.keys(resumeLeft).find((x) => x === q || sameLabel(x, q));
            if (k !== undefined) { runAnswers[q] = resumeLeft[k]; delete resumeLeft[k]; fromPark++; continue; }
            const got = (await selectChoices(wc, fr, v, q)) || optionsFor(v.text, q) || (await dropdownOptions(wc, v, q));
            const all = cleanOptions(got.options, 1000);
            items.push({ question: q, options: orderOptions(q, all), all_options: all.slice(0, 40),
                         partial: !!got.partial || all.length > 40, context: contextFor(v.text, q),
                         hint: noteAbove(v.text, q) });
          }
          // A question the run already took a known answer for, still open: that answer did not
          // fit, so the person is asked for real about THAT question; every other question still
          // gets the person's material first.
          const forced = items.filter((it) => knownSeen.has(it.question));
          const rest = items.filter((it) => !knownSeen.has(it.question));
          let open = forced.slice();
          if (rest.length) {
            const k = await deps.postApp("/api/autoapply/agent/ask", { questions: rest, record_id: recordId, known_only: true }).catch(() => null);
            const known = (k && k.answers) || {};
            const pick = (k && k.select) || {};
            applyReword(rest, k && k.reword);
            for (const [q, a] of Object.entries(known)) {
              knownSeen.add(q);
              // A self-identification choice the page lists: the shell picks it itself, and it
              // never travels on to the brain. Anything else goes to the brain as a placeholder.
              if (pick[q] && await pickSaved(wc, v, q, pick[q])) continue;
              runAnswers[q] = a;
            }
            open = open.concat(rest.filter((it) => !(it.question in known)));
          }
          let answered = items.length - open.length + fromPark;
          if (open.length) {
            const id = Math.random().toString(16).slice(2, 10);
            const shown = open.length === 1
              ? (open[0].say ? open[0].say
                 : (open[0].context ? "After “" + open[0].context + "”, the application asks: " : "The application asks: ") + open[0].question)
              : "The application asks " + open.length + " questions.";
            const ask = { id, question: open[0].question, options: open.length === 1 ? open[0].options : [], questions: open };
            if (!batch) progress(recordId, "ask", shown, { step, ask });
            const r = await askLong("/api/autoapply/agent/ask", {
              questions: open, record_id: recordId, id, force_questions: forced.map((it) => it.question),
              park: terms.parkInfo(), no_wait: batch }, st);
            dlog("step " + step + " ask ->", r && { ok: r.ok, reason: r.reason, n: open.length });
            if (r && r.reason === "stopped") return stopped("stopped", "Stopped.");
            if (r && r.reason === "parked") return park("questions", shown, { url, left, ask: batch ? undefined : ask });
            if (!(r && r.ok)) return stopped("needs-answers", blockedMessage("needs-answers", open.map((it) => it.question)), { url, left });
            for (const [q, a] of Object.entries(r.answers || {})) runAnswers[q] = a;
            answered += open.length;
          }
          if (!answered) return stopped("needs-answers", blockedMessage("needs-answers", stillOpen(left, runAnswers, knownSeen)), { url, left });
          history.push({ step, evaluation: reply.evaluation || "", memory: reply.memory || "",
                         actions: ["asked the person"], results: [answered + " answered"] });
          if (open.length || fromPark) progress(recordId, "running", "Thanks. Carrying on", { step });
          continue;
        }
        if (done && done.outcome === "blocked") {
          // What only the person can do on the page (a captcha, a phone code, a sign-in): the job
          // parks with Continue, and the next job goes on.
          if (PARK_REASONS.has(done.reason || "needs-person")) {
            return parkFor(done.reason || "needs-person", blockedMessage(done.reason, stillOpen(left, runAnswers, knownSeen)), { url, left });
          }
          return stopped(done.reason || "needs-person", blockedMessage(done.reason, stillOpen(left, runAnswers, knownSeen)), { url, left });
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
          if (!(r && r.ok)) { stepFailed = true; if (r && r.note === "submit-guard") return handOver(recordId, wc, url, opts, step, terms.accepted); continue; }
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

  // ---- a batch: "Apply for me" on several jobs while the person is away ---------------------
  // One job after another in the pane. Nothing waits on the person: a job that needs them parks
  // (its question stays open) and the next one starts. ONE digest goes to Telegram when the batch
  // ends (and every 30 minutes while it runs, when something new parked). jobs: [{ recordId, url,
  // resumePath, lane, autonomous, company }].
  async function batch(jobs) {
    if (current || batchOn) return { ok: false, reason: "busy" };
    const list = (jobs || []).filter((j) => j && j.recordId != null && /^https?:/i.test(j.url || ""));
    if (!list.length) return { ok: false, reason: "empty" };
    const b = batchOn = { stop: false };
    const digest = () => deps.postApp("/api/autoapply/agent/digest", { only_new: true }).catch(() => null);
    const timer = setInterval(() => { digest(); }, DIGEST_MS);
    if (timer.unref) timer.unref();
    const results = [];
    try {
      for (let i = 0; i < list.length; i++) {
        if (b.stop) break;
        const j = list[i];
        const r = await run(j.recordId, Object.assign({}, j, { mode: "batch", batchAt: { i: i + 1, n: list.length } }));
        results.push(Object.assign({ record_id: j.recordId }, r || { ok: false, reason: "error" }));
      }
    } finally {
      clearInterval(timer);
    }
    const d = await digest();
    batchOn = null;
    const n = (pred) => results.filter(pred).length;
    const readyN = n((r) => r.ready), sentN = n((r) => r.submitted), parkedN = n((r) => r.parked);
    const stoppedN = results.length - readyN - sentN - parkedN;
    const parts = [];
    if (sentN) parts.push(sentN + " sent");
    if (readyN) parts.push(readyN + " ready to send");
    if (parkedN) parts.push(parkedN + " paused, waiting on you");
    if (stoppedN) parts.push(stoppedN + " stopped");
    const msg = (b.stop ? "Stopped after " : "Done with ") + results.length + " application" + (results.length === 1 ? "" : "s")
      + (parts.length ? ": " + parts.join(", ") + "." : ".")
      + (parkedN ? " Answer them in Auto-apply, Needs you" + (d && d.sent ? " or on Telegram" : "") + ", and I carry on with each one." : "");
    progress(null, "batch-done", msg, { batch: { results: results.length, ready: readyN, sent: sentN, parked: parkedN, stopped: stoppedN } });
    if (parkedN) startPolling();
    setTimeout(() => { pollParked().catch(() => {}); }, 50);
    return { ok: true, results, digest: d || null };
  }

  // ---- parked jobs carry on by themselves ---------------------------------------------------
  // The engine says which parked jobs got their answer (GET /api/autoapply/agent/parked). One at a
  // time, never during another run or a batch: the job is taken and re-run, from the same page
  // when the pane is still on it, else from the job's link.
  let pollT = null;
  let polling = false;
  function startPolling() {
    if (pollT || deps.noPolling) return;
    pollT = setInterval(() => { pollParked().catch(() => {}); }, PARK_POLL_MS);
    if (pollT.unref) pollT.unref();
  }
  async function pollParked() {
    if (polling || current || batchOn) return { ok: false, reason: "busy" };
    polling = true;
    let resumed = 0;
    try {
      const r = await deps.getApp("/api/autoapply/agent/parked");
      for (const e of (r && r.parked) || []) {
        if (current || batchOn) break;
        if (e.state === "waiting") continue;
        const t = await deps.postApp("/api/autoapply/agent/parked/" + encodeURIComponent(e.record_id) + "/take", {}).catch(() => null);
        if (!t || !t.ok) continue;
        const ent = t.entry || e;
        const key = String(ent.record_id);
        const mem = parked.get(key);
        parked.delete(key);
        const rid = ridOf(ent.record_id);
        if (ent.state !== "answered") {
          progress(rid, "stopped", ent.state === "declined"
            ? "Stopped. You chose not to accept " + possessive(ent.company, ent.title) + ", so I did not apply."
            : "Stopped. The paused question was closed.", { reason: ent.state });
          continue;
        }
        resumed++;
        await resume(rid, ent, mem);
      }
    } finally {
      polling = false;
    }
    return { ok: true, resumed };
  }
  async function resume(rid, ent, mem) {
    const pane = deps.getPane();
    const wcp = pane && !pane.webContents.isDestroyed() ? pane.webContents : null;
    const here = !!(mem && wcp && mem.epoch === paneEpoch && (mem.opened || (wcp.getURL() || "") === (mem.pageUrl || ent.page_url)));
    const base = mem ? mem.opts : optsFrom(ent.run);
    dlog("resume", rid + " samePage=" + here);
    return run(rid, Object.assign({}, base, { mode: "single", resume: { entry: ent, state: here && !mem.opened ? mem.state : null, samePage: here } }));
  }
  // "Continue" on a job parked at a captcha or a step on the page. With the pane still on that
  // page: it carries on. Otherwise the page opens first, for the person to do the step, and the
  // next Continue carries on from it.
  async function continueParked(recordId) {
    const key = String(recordId);
    const r = await deps.getApp("/api/autoapply/agent/parked");
    const e = ((r && r.parked) || []).find((x) => String(x.record_id) === key);
    if (!e) return { ok: false, reason: "none" };
    if (e.kind === "questions" || e.kind === "terms") return { ok: false, reason: "answer" };
    const mem = parked.get(key);
    const pane = deps.getPane();
    const wcp = pane && !pane.webContents.isDestroyed() ? pane.webContents : null;
    const here = !!(mem && wcp && mem.epoch === paneEpoch && (mem.opened || (wcp.getURL() || "") === mem.pageUrl));
    if (!here) {
      if (current || batchOn) return { ok: false, reason: "busy" };
      const url = e.page_url || (e.run && e.run.url) || "";
      if (!/^https?:/i.test(url)) return { ok: false, reason: "bad-url" };
      paneEpoch++;
      deps.openInPane(url);
      parked.set(key, { epoch: paneEpoch, pageUrl: url, opened: true, kind: e.kind, state: null,
                        opts: (mem && mem.opts) || optsFrom(e.run) });
      progress(ridOf(recordId), "parked", e.kind === "captcha" ? "Solve the captcha on this page, then press Continue."
        : "Do the step on this page, then press Continue.", { parked: { kind: e.kind, opened: true } });
      return { ok: true, opened: true };
    }
    await deps.postApp("/api/autoapply/agent/parked/" + encodeURIComponent(key) + "/continue", {}).catch(() => null);
    const p = await pollParked();
    return { ok: true, resumed: !!(p && p.resumed), queued: !(p && p.resumed) };
  }

  // ---- Submit: the shell's decision, never the model's --------------------------------------
  async function handOver(recordId, wc, url, opts, step, accepted) {
    if (current && current.disarmSigning) current.disarmSigning();
    const now = await deps.applyDecision(opts.url || url, url);
    const auto = !!opts.autonomous && now.click_submit === true;
    ready.set(recordId, { url, lane: auto ? "auto" : "assisted", epoch: paneEpoch });
    const said = acceptedLine(opts.company || "", accepted);
    if (auto) {
      progress(recordId, "running", "Sending" + (said ? ". " + said : ""), { step });
      const r = await send(recordId);
      current = null;
      return r;
    }
    progress(recordId, "ready", (opts.batchAt ? "Application " + opts.batchAt.i + " of " + opts.batchAt.n + ": " : "")
             + "Ready to send. " + (said ? said + " " : "") + "Check it, then press Send.",
             { step, ready: true, accepted: (accepted || []).slice() });
    current = null;
    setTimeout(() => { pollParked().catch(() => {}); }, 50);     // a parked job may be waiting its turn
    return { ok: true, ready: true, steps: step };
  }

  // Press the site's Submit for a package the agent left ready. A real input event at the
  // button's centre (not the in-page click, which refuses Submit on purpose).
  async function send(recordId) {
    const pane = deps.getPane();
    const r = ready.get(recordId);
    if (!pane || !r) return { ok: false, reason: "not-ready" };
    // Another application took the pane since (a batch, a parked job carrying on): its page is gone.
    if (r.epoch !== paneEpoch) { ready.delete(recordId); return { ok: false, reason: "page-gone" }; }
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

  function stop() { if (current) current.stop = true; if (batchOn) batchOn.stop = true; }
  const isReady = (recordId) => ready.has(recordId);
  const busy = () => !!(current || batchOn);

  return { run, batch, send, stop, isReady, busy, pollParked, continueParked, startPolling, MAX_STEPS,
           _test: { askLong, parked, epoch: () => paneEpoch } };
}

// ---- reading the view for an ask (pure, so the tests reach them) -----------------------------
// Form furniture around a question (autoapply/answers.py clean_label): a validation error the
// page wrapped round its label (Phenom at Lam Research: "Error: The field What is your annual
// salary requirement? is required and must have a value."; Workday: "Errors Found
// Error-<question> The field <question> is required") and the required markers ("*",
// "(required)", "Required"). The question's own words are kept as written; "" when nothing but
// furniture is left. The bare "is" tail is what the engine leaves after cutting "required".
const ERR_TAIL = /\s+(?:is\s+(?:(?:required|mandatory)\s+)?and\s+must\s+have\s+a\s+value|is\s+(?:a\s+)?(?:required|mandatory)(?:\s+(?:field|question))?(?:\s+and\s+must\s+have\s+a\s+value)?|must\s+have\s+a\s+value|cannot\s+be\s+(?:left\s+)?(?:blank|empty)|is\s+(?:blank|empty|missing|invalid)|is)\s*[.!]*\s*$/i;
const THE_FIELD = /(?:^|(?<=[?.!:]\s))the field\s+/gi;
const THE_FIELD_ANY = /(?:^|(?<=\s))the field\s+/gi;
const REQ_SENTENCE = /\s*\b(?:this\s+)?(?:field|question|answer)\s+is\s+(?:required|mandatory)\W*$/i;
const REQ_PAREN = /\(\s*(?:required|mandatory)\s*\)/gi;
const REQ_TRAIL = /(?:(?<=[?*\u2731)\]])\s*|\s*[:*\u2731]\s*|\s+-\s+)(?:required|mandatory)[\s*\u2731.]*$/i;
const REQ_LEAD = /^(?:required|mandatory)\s*[:*\u2731-]+\s*/i;
const ONLY_FURNITURE = /^(?:is\s+)?(?:a\s+)?(?:required|mandatory)(?:\s+(?:field|question))?\W*$/i;
function cleanQuestion(text) {
  let s = String(text || "").replace(/\s+/g, " ").trim();
  if (!s) return "";
  let erred = false;
  for (;;) {                    // "Errors Found Error-": one error head after another
    // Workday's link reads "Error-What is..."; "Error-free code?" is a question (a capital after the hyphen).
    const m = /^[\s*\u2731]*(?:(?:\d+\s+)?errors?\s+found\b[\s:.!-]*|errors?\s*[:\u2013\u2014]+\s*|errors?\s+-\s*)/i.exec(s)
      || /^[\s*\u2731]*[Ee][Rr][Rr][Oo][Rr][Ss]?-(?=[A-Z0-9"'\u201c(])/.exec(s);
    if (!m || !m[0]) break;
    s = s.slice(m[0].length);
    erred = true;
  }
  let fields = [...s.matchAll(THE_FIELD)];
  // Workday's banner names the question twice with no stop between: "Error-Gender The field Gender is required".
  if (!fields.length && erred) fields = [...s.matchAll(THE_FIELD_ANY)];
  if (fields.length) {
    const last = fields[fields.length - 1];
    const tail = s.slice(last.index + last[0].length);
    const cut = tail.replace(ERR_TAIL, "");
    if (cut !== tail || (erred && last.index === 0)) { s = cut; erred = true; }
  }
  // "<question>? is required", "Gender is required", and the "Gender is" the engine leaves after cutting "required"
  s = s.replace(REQ_SENTENCE, "");
  const cut = s.replace(ERR_TAIL, "");
  if (cut.trim()) s = cut;
  s = s.replace(REQ_PAREN, " ").replace(REQ_TRAIL, "").replace(REQ_LEAD, "");
  s = s.replace(/^[\s*\u2731]+|[\s*\u2731]+$/g, "").replace(/\s+/g, " ").trim();
  return ONLY_FURNITURE.test(s) ? "" : s;
}

// One question reaches the shell in several lengths: the view cuts a label at 80 characters,
// the engine keeps 200, and a Workday "Error-" link cuts it at 74. Two labels name the same
// question when they are equal, or one is a cut of the other at least 70 characters long, first
// as the page wrote them (the view's 80-character cut of "Error: The field <question> is ..."
// is a cut of the engine's label, not of the question), then without the furniture round the
// question ("Error: The field X is required" is X). Case and spacing aside.
const squash = (s) => String(s || "").replace(/\s+/g, " ").trim().toLowerCase();
function cutOf(x, y) {
  if (!x || !y) return false;
  if (x === y) return true;
  const [s, l] = x.length <= y.length ? [x, y] : [y, x];
  return s.length >= 70 && l.startsWith(s);
}
// What the view's 80-character cut can leave of an error's tail after the question ("Error: The
// field <question> is  and must have a val"): never a word of a question.
const TAIL_WORDS = ["is required and must have a value", "is and must have a value", "is a required field",
                    "is required", "must have a value", "cannot be left blank", "cannot be blank"];
function cutTail(x, y) {
  const [s, l] = x.length <= y.length ? [x, y] : [y, x];
  if (s.length < 8 || !l.startsWith(s)) return false;
  const rest = l.slice(s.length).replace(/\s+/g, " ").trim();
  return !!rest && TAIL_WORDS.some((t) => t.startsWith(rest));
}
function sameLabel(a, b) {
  if (cutOf(squash(a), squash(b))) return true;
  const x = squash(cleanQuestion(a) || a), y = squash(cleanQuestion(b) || b);
  // the tail rule only for a label the view cut short (80 characters)
  return cutOf(x, y) || (Math.max(squash(a).length, squash(b).length) >= 75 && cutTail(x, y));
}
const unq = (s) => String(s || "").replace(/\\(["\\])/g, "$1");
// A view line's label ("" when it has none).
function lineLabel(line) {
  const m = /label="((?:[^"\\]|\\.)*)"/.exec(String(line || ""));
  return m ? unq(m[1]) : "";
}
// The view's element lines for a question.
function linesFor(viewText, label) {
  return String(viewText || "").split("\n").filter((ln) => /^\*?\[\d+\]</.test(ln) && sameLabel(lineLabel(ln), label));
}

// What to ask: the engine's open fields, else the fields the page's own errors name ("The
// field X is required"), else empty required boxes in the view. Labels only, never values,
// and without the furniture round them (cleanQuestion): the person is asked X, never "Error:
// The field X is required". One entry per question: a cut wording (an Error- link) folds into
// the fuller one.
function questionsFor(viewText, left, includeFilled) {
  const out = [];
  const text = String(viewText || "");
  const add = (q) => {
    q = cleanQuestion(q);
    if (!q) return;
    const i = out.findIndex((o) => sameLabel(o, q));
    if (i < 0) out.push(q);
    else if (q.length > out[i].length) out[i] = q;
  };
  // A person is asked only about a REQUIRED question: an optional box (LinkedIn URL, skills,
  // a cover letter) is left alone, and a file box is an upload, never a question.
  for (const x of left || []) {
    const q = typeof x === "string" ? x : (x && x.label) || "";
    const ln = linesFor(text, q)[0] || "";
    if (/^\*?\[\d+\]<file\b/.test(ln)) continue;
    if (ln && !/\brequired\b/.test(ln)) continue;
    add(q);
  }
  let m;
  // The page's own errors. Workday writes "Errors Found Error-<question> The field <question>
  // is required" in one banner and lists each one as an "Error-<question>" link button, so take
  // the named field, never the whole banner.
  const errRe = /Error: "([^"]*)"/g;
  while ((m = errRe.exec(text))) {
    const banner = m[1];
    const named = banner.match(/The field (.+?) (?:is required|must have a value|is a required field|cannot be blank)/g);
    if (named) {
      for (const n of named) add(n.replace(/^The field /, "").replace(/ (?:is required|must have a value|is a required field|cannot be blank)$/, ""));
    } else {
      const one = /^(.+?) (?:is required|must have a value|is a required field|cannot be blank)/.exec(banner);
      if (one && !/^Errors? Found\b/i.test(one[1])) add(one[1]);
    }
  }
  const linkRe = /<button>Error-([^<]+?)<\/button>/g;
  while ((m = linkRe.exec(text))) add(m[1]);
  if (!out.length) {
    const reqRe = /<(?:input|combobox|select|textarea)[^>]*label="((?:[^"\\]|\\.)*)"[^>]*value=""[^>]*required>/g;
    while ((m = reqRe.exec(text))) add(unq(m[1]));
  }
  // Never re-ask what is already in its box (a chosen option, typed text, a ticked box): a
  // stale "is required" error can outlive the fill.
  // A self-identification radio group says answered=true instead (its choice never leaves the page).
  const filled = (label) => linesFor(text, label).some((ln) => /\bvalue="(?:[^"\\]|\\.)+"|checked=true|answered=true/.test(ln));
  return (includeFilled ? out : out.filter((q) => !filled(q))).slice(0, 6);
}

// The question just above this one on the page, with its answer when shown: a box called
// "Please provide more information" only makes sense under "Will you need sponsorship? Yes".
function contextFor(viewText, label) {
  const lines = String(viewText || "").split("\n");
  const at = lines.findIndex((ln) => sameLabel(lineLabel(ln), label));
  for (let i = at - 1; i >= 0 && at > 0; i--) {
    const lab = lineLabel(lines[i]);
    if (!lab || sameLabel(lab, label)) continue;
    const val = /value="([^"]*)"/.exec(lines[i]);
    const v = val && val[1] && val[1] !== "(filled)" ? val[1] : "";
    return lab.slice(0, 90) + (v ? ": " + v : "");
  }
  return "";
}

// The page's own note right above a field (the text lines between it and the element before,
// a "Read more" button skipped): Lam Research explains its "Veteran" question in a paragraph
// about protected veterans there. Sent only to the local engine, as a hint for a saved
// self-identification answer; "" when there is none.
function noteAbove(viewText, label) {
  const lines = String(viewText || "").split("\n");
  const at = lines.findIndex((ln) => /^\*?\[\d+\]</.test(ln) && sameLabel(lineLabel(ln), label));
  const out = [];
  for (let i = at - 1; at > 0 && i >= 0 && out.length < 3; i--) {
    const ln = lines[i];
    if (/^\*?\[\d+\]<button>(read|show|see) (more|less)<\/button>$/i.test(ln)) continue;
    if (/^\*?\[\d+\]</.test(ln) || /^(URL:|Title:|\[Start of page\])/.test(ln)) break;
    if (/^Error: /.test(ln)) continue;
    out.unshift(ln);
  }
  return out.join(" ").slice(0, 400);
}

// The choices the view itself shows for a field: every radio option sharing its label, or a
// select's first four (partial: the shell reads a select's full list from the element first).
// null when the view shows none (free text, a typeahead, a custom dropdown).
function optionsFor(viewText, label) {
  const out = [];
  let partial = false;
  for (const line of linesFor(viewText, label)) {
    const first = /first="((?:[^"\\]|\\.)*)"/.exec(line);
    if (first) {
      const shown = unq(first[1]).split("|");
      for (const o of shown) if (o && !isPlaceholderOpt(o) && !out.includes(o)) out.push(o);
      const total = Number((/\boptions=(\d+)/.exec(line) || [])[1] || 0);
      if (total > shown.length || shown.some((o) => o.length >= 40)) partial = true;
    }
    const opt = /<radio [^>]*option="((?:[^"\\]|\\.)*)"/.exec(line);
    if (opt) { const o = unq(opt[1]); if (o && !out.includes(o)) out.push(o); }
  }
  return out.length ? { options: out, partial } : null;
}

// The view index of the field a question labels (0 when not found).
function indexFor(viewText, label) {
  for (const line of linesFor(viewText, label)) {
    const m = /^\*?\[(\d+)\]</.exec(line);
    if (m) return Number(m[1]);
  }
  return 0;
}

// Voluntary self-identification questions (the planner's _EEO) and the decline choice
// (autoapply/eeo.py DECLINE, with the federal form's "I do not want to answer" and Workday's
// "Not Declared"): on such a question the decline option is offered FIRST, never a guess.
const EEO_RE = /gender|\bsex\b|race|ethnic|hispanic|latin[oax]|veteran|disab|sexual orientation|transgender|pronoun/i;
const DECLINE_RE = /decline|prefer not|do not wish|don['\u2019]?t wish|not wish to|(?:do not|don['\u2019]?t) want to (?:answer|disclose|say|self[- ]?identify|provide)|choose not|rather not|not to (?:say|answer|disclose)|not declared/i;
// A dropdown's prompt entry, never a choice. Only a pure prompt: "Choose not to disclose" (a
// decline) and "Search engine" are real choices (autoapply/asks.py has the same rule).
const PROMPT_RE = /^[\s\-_*.\u2013\u2014]*(?:(?:please )?(?:select|choose)\b|please (?:enter|type|pick)\b|make a selection\b)|^-{2,}|^(?:none selected|no selection|nothing selected)\W*$|^(?:search|start typing|type to search)(?:\W*$|\s*(?:\.{3}|\u2026)|\s+(?:for|by|here)\b)/i;
function isPlaceholderOpt(t) {
  const s = String(t || "").replace(/\s+/g, " ").trim();
  return !!s && PROMPT_RE.test(s) && !DECLINE_RE.test(s);
}
// The page's choices, cleaned: no prompt entries, no repeats, at most `limit` (40).
function cleanOptions(options, limit) {
  const out = [];
  for (const o of options || []) {
    const t = String(o || "").replace(/\s+/g, " ").trim().slice(0, 120);
    if (t && !isPlaceholderOpt(t) && !out.includes(t)) out.push(t);
    if (out.length >= (limit || 40)) break;
  }
  return out;
}
// Up to 8 choices to offer, the decline choice first on a self-identification question (even
// when it sat beyond the first eight).
function orderOptions(question, options) {
  const out = cleanOptions(options, 1000);
  if (EEO_RE.test(String(question || ""))) {
    const dec = out.find((o) => DECLINE_RE.test(o));
    if (dec) return [dec].concat(out.filter((o) => o !== dec)).slice(0, 8);
  }
  return out.slice(0, 8);
}

// ---- terms a site requires: the words the person sees (pure, so the tests reach them) --------
const TERMS_CHOICES = ["Accept for all applications", "Just this one", "Stop"];
const possessive = (company, title) => (String(company || "").trim() ? String(company).trim() + "'s " : "the site's ") + String(title || "terms");
const needsLine = (company, title) => (String(company || "").trim() || "This site") + " needs you to accept its " + String(title || "terms") + " to apply.";
const termsQuestion = (company, title) => needsLine(company, title) + " Accept it?";
// The Ready to send summary's line: "Accepted Lam Research's Data Privacy Agreement for you."
function acceptedLine(company, titles) {
  const t = [...new Set((titles || []).map((x) => String(x || "").trim()).filter(Boolean))];
  if (!t.length) return "";
  const list = t.length === 1 ? t[0] : t.slice(0, -1).join(", ") + " and " + t[t.length - 1];
  return "Accepted " + possessive(company, list) + " for you.";
}

// (Phase 3, docs/applicant-brain.md) A question the engine put in its own words: two saved
// salaries that disagree become ONE question ("Forms say $90,000 (March); you said $120,000
// (Oct 28). Which one?") with the amounts as its choices; a typed amount still counts.
function applyReword(items, reword) {
  for (const it of items || []) {
    const r = reword && reword[it.question];
    if (!r) continue;
    it.say = String(r.say || "").slice(0, 300);
    const opts = Array.isArray(r.options) ? r.options.map(String).filter(Boolean) : [];
    if (opts.length) { it.options = opts.slice(0, 8); it.all_options = opts.slice(0, 40); it.partial = true; }
  }
  return items;
}

// The labels a stuck run still names to the person: never one already answered this run (on the
// bar, on Telegram, or from their saved material), so "needs answers only you can give" lists
// only what is really open.
function stillOpen(left, runAnswers, knownSeen) {
  const done = Object.keys(runAnswers || {}).concat(Array.from(knownSeen || []));
  return (left || []).filter((x) => {
    const lab = typeof x === "string" ? x : (x && x.label) || "";
    return !done.some((q) => sameLabel(lab, q));
  });
}

function blockedMessage(reason, left) {
  switch (reason) {
    case "needs-consent": return "This site needs you to accept its terms to apply, and I could not find where. Have a look at the page.";
    case "terms-declined": return "Stopped. You chose not to accept the site's terms, so I did not apply.";
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

module.exports = { createAgent, blockedMessage, applyReword, stillOpen, orderOptions, cleanOptions, sameLabel, questionsFor, cleanQuestion,
                   optionsFor, indexFor, contextFor, SUBMIT_RE, ATS_HOSTS, MAX_STEPS, EEO_RE,
                   TERMS_CHOICES, termsQuestion, acceptedLine, noteAbove };
