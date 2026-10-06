/* SponsorJobs' Electron shell (the Cursor-style architecture, chosen 2026-07-16).
 *
 * What it owns:
 *  - the Python engine as a child process (spawned only if 127.0.0.1:57000 is not
 *    already serving, so dev servers are reused; the WHOLE process tree is killed on
 *    quit -- the five-orphan-servers lesson from the Amazon case study, baked in)
 *  - the app window (loads the local Flask UI)
 *  - the in-app browser pane: a native WebContentsView laid over the area the web UI
 *    reserves for it. Clicks on external links anywhere in the app (including inside
 *    the embedded CV-preview PDF, which routes them through /api/open) open HERE, so
 *    the app window is never carried away -- and the pane is CDP-debuggable, which is
 *    the assisted-apply takeover hook (agent fills, the person watches and clicks).
 */
const { app, BrowserWindow, WebContentsView, dialog, ipcMain, session, net, nativeTheme, shell } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");

const SERVER = "http://127.0.0.1:57000";
// The STATIC job feed (docs/feed.md): a scheduled GitHub Action crawls and uploads jobs.json.gz
// + JD shards to Cloudflare R2 behind our domain; the engine downloads the slim list hourly and
// filters locally, so every user sees the fresh, sponsor-tagged board with no server to keep
// alive. Override with TAILOR_FEED_URL (e.g. a staging bucket). The `.example` placeholder is an
// RFC 2606 reserved name the engine recognises as "not configured", so until the real domain is
// set the app simply runs on its local crawl instead of waiting on a lookup that cannot succeed.
const FEED_URL = process.env.TAILOR_FEED_URL || "https://feed.sponsorjobs.ai/feed";
// Fixed CDP port so the Python side (assisted apply) can attach to the pane.
app.commandLine.appendSwitch("remote-debugging-port", "9223");

let win = null;
let pane = null;           // WebContentsView for the in-app browser
let paneOpen = false;
let paneBounds = null;     // last bounds reported by the renderer's slot element
let pyChild = null;        // the Python engine, when this shell spawned it
let brokerChild = null;    // the managed-AI broker (dev only; hosted in production)

function ping(url) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => { res.resume(); resolve(res.statusCode === 200); });
    req.on("error", () => resolve(false));
    req.setTimeout(2000, () => { req.destroy(); resolve(false); });
  });
}

function resolvePython() {
  /* Find an interpreter that can actually run the engine.
   *
   * Spawning a bare "python" is wrong on two counts: on macOS and most Linux distros the
   * name is `python3` and `python` does not exist at all, and even where it does exist it
   * is the SYSTEM interpreter, which has none of the engine's dependencies. The repo venv
   * is the only one guaranteed to have Flask installed, so it is tried first. A failure
   * here is silent and baffling -- the shell just sits on "engine did not come up" -- so
   * it is worth being explicit rather than hoping PATH is friendly.
   */
  const venvBin = process.platform === "win32"
    ? path.join(__dirname, "..", ".venv", "Scripts", "python.exe")
    : path.join(__dirname, "..", ".venv", "bin", "python");
  const candidates = [process.env.TAILOR_PYTHON, venvBin,
                      process.platform === "win32" ? "python" : "python3", "python"];
  for (const candidate of candidates) {
    if (!candidate) continue;
    // Bare names are resolved by PATH at spawn time; only absolute paths are checkable here.
    if (!path.isAbsolute(candidate)) return candidate;
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return candidate;
    } catch (e) { /* try the next candidate */ }
  }
  return "python3";
}

async function ensureServer() {
  if (await ping(SERVER + "/")) {
    // Something already answers on the engine port. A dev checkout reuses it. An installed app
    // first checks it can actually serve the UI: on 2026-10-06 an engine left running from a
    // deleted checkout answered "/" but 404'd every stylesheet, and the installed app attached to
    // it and showed a bare, unstyled page.
    if (!app.isPackaged || await ping(SERVER + "/static/styles.css")) return;
    throw new Error("Another program is using SponsorJobs' port (127.0.0.1:57000) and is not " +
                    "working, most likely an older SponsorJobs engine that was left running. " +
                    "Restart your computer, then open SponsorJobs again.");
  }
  const env = { ...process.env, TAILOR_SERVER_ONLY: "1",
    // Pull the jobs board from the static feed (fresh, high-volume) unless the user pointed us
    // elsewhere. This is what turns "82 stale local roles" into the full central feed.
    JOBS_FEED_URL: process.env.JOBS_FEED_URL || FEED_URL };
  delete env.RESUME_AGENT_DEV;                        // real model only (CLAUDE.md 13)
  delete env.RESUME_AGENT_FAKE_LLM;
  if (app.isPackaged) {
    // Installed app: the engine is the PyInstaller sidecar shipped in resources/engine
    // (see the electron-builder extraResources config). One installer, one app.
    const engine = path.join(process.resourcesPath, "engine",
                             process.platform === "win32" ? "SponsorJobs.exe" : "SponsorJobs");
    pyChild = spawn(engine, [], { env, windowsHide: true, stdio: "ignore" });
  } else {
    pyChild = spawn(resolvePython(), ["-m", "ui.app"], {
      cwd: path.join(__dirname, ".."),
      env,
      windowsHide: true,
      stdio: "ignore",
    });
  }
  for (let i = 0; i < 60; i++) {
    if (await ping(SERVER + "/")) return;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error("SponsorJobs engine did not come up on " + SERVER);
}

function paneState() {
  const wc = pane && pane.webContents;
  return {
    open: paneOpen,
    url: wc ? wc.getURL() : "",
    title: wc ? wc.getTitle() : "",
    canGoBack: wc ? wc.navigationHistory.canGoBack() : false,
    canGoForward: wc ? wc.navigationHistory.canGoForward() : false,
  };
}

function pushPaneState() {
  if (win && !win.isDestroyed()) win.webContents.send("pane:state", paneState());
}

function layoutPane() {
  if (!pane || !win || win.isDestroyed()) return;
  if (!paneOpen || !paneBounds) {
    pane.setBounds({ x: 0, y: 0, width: 0, height: 0 });
    return;
  }
  const b = paneBounds;
  pane.setBounds({ x: Math.round(b.x), y: Math.round(b.y),
                   width: Math.round(b.width), height: Math.round(b.height) });
}

function ensurePane() {
  if (pane) return pane;
  pane = new WebContentsView();
  win.contentView.addChildView(pane);
  const wc = pane.webContents;
  // Popups inside the pane stay inside the pane.
  wc.setWindowOpenHandler(({ url }) => {
    if (opensInOwnBrowser(url)) shell.openExternal(url);
    else if (/^https?:/i.test(url)) wc.loadURL(url);
    return { action: "deny" };
  });
  for (const ev of ["did-navigate", "did-navigate-in-page", "page-title-updated", "did-finish-load"]) {
    wc.on(ev, pushPaneState);
  }
  // Stripe checkout AND Google sign-in run in the pane; both return to our own app URL. Don't load
  // the app INSIDE the pane -- close it and hand the result to the main window so it continues
  // cleanly there, instead of leaving the person staring at a mini-app in the pane.
  const send = (channel, data) => { if (win && !win.isDestroyed()) win.webContents.send(channel, data); };
  const onReturn = (e, url) => {
    if (!url || !url.startsWith(SERVER)) return;
    const co = /[?&]checkout=(success|cancel)\b/.exec(url);
    if (co) { e.preventDefault(); closePane(); send("checkout:return", co[1]); return; }
    const acc = /[?&]account=([^&#]+)/.exec(url);   // Google sign-in returns the bearer token
    if (acc) { e.preventDefault(); closePane(); send("signin:return", { token: decodeURIComponent(acc[1]) }); return; }
    if (/[?&]signin=error\b/.test(url)) { e.preventDefault(); closePane(); send("signin:return", { error: true }); }
  };
  wc.on("will-navigate", onReturn);
  wc.on("will-redirect", onReturn);
  return pane;
}

// Account and API-key pages open in the person's OWN browser, never the pane. Google refuses
// sign-in (passkeys included) inside embedded browsers, and RFC 8252 says native apps must not
// host sign-in in one: in 0.1.0 the Anthropic console opened in the pane and its Google sign-in
// hung on "Verifying it's you". Their own browser is also where they're already signed in.
const OWN_BROWSER_HOSTS = [
  "console.anthropic.com", "platform.claude.com", "claude.ai",
  "platform.openai.com", "auth.openai.com",
  "platform.tavus.io",
  "accounts.google.com", "appleid.apple.com", "login.microsoftonline.com", "login.live.com",
];

function opensInOwnBrowser(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return OWN_BROWSER_HOSTS.some((h) => host === h || host.endsWith("." + h));
  } catch (e) { return false; }
}

function openInPane(url) {
  if (!/^https?:/i.test(url)) return;
  if (opensInOwnBrowser(url)) { shell.openExternal(url); return; }
  ensurePane();
  paneOpen = true;
  pane.webContents.loadURL(url);
  layoutPane();
  pushPaneState();
}

function closePane() {
  paneOpen = false;
  layoutPane();
  pushPaneState();
}

async function createWindow() {
  win = new BrowserWindow({
    width: 1560,
    height: 980,
    backgroundColor: "#111111",
    title: "SponsorJobs",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.setMenuBarVisibility(false);

  // Camera + mic are needed in two places: Round 1 (the recorded screen) captures on the app's
  // OWN local origin (127.0.0.1), and Round 2 (the live avatar) runs on Tavus over Daily. Grant
  // media on BOTH and nowhere else the pane may browse (assisted apply visits arbitrary job sites,
  // which never need the camera).
  const MEDIA = ["media", "mediaKeySystem", "camera", "microphone", "audioCapture", "videoCapture"];
  const allowMedia = (host) => host === "127.0.0.1" || host === "localhost"
    || /(^|\.)(daily\.co|tavus\.io|tavusapi\.com)$/i.test(host || "");
  const hostOf = (s) => { try { return new URL(s).hostname; } catch (e) { return ""; } };
  session.defaultSession.setPermissionRequestHandler((wc, permission, callback) => {
    callback(MEDIA.includes(permission) && allowMedia(hostOf(wc && wc.getURL && wc.getURL())));
  });
  session.defaultSession.setPermissionCheckHandler((wc, permission, origin) => {
    const host = hostOf(origin) || hostOf(wc && wc.getURL && wc.getURL());
    return MEDIA.includes(permission) && allowMedia(host);
  });

  // Any /api/open?u=... request (the CV preview's rerouted links) opens the pane
  // instead of the system browser: cancel the request, keep the app where it is.
  session.defaultSession.webRequest.onBeforeRequest(
    { urls: [SERVER + "/api/open*"] },
    (details, callback) => {
      try {
        const u = new URL(details.url).searchParams.get("u");
        if (u) setImmediate(() => openInPane(u));
      } catch (e) { /* fall through: cancel regardless */ }
      callback({ cancel: true });
    });

  // External links from the app page itself also stay in-app.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url) && !url.startsWith(SERVER)) {
      openInPane(url);
      return { action: "deny" };
    }
    return { action: "allow" };
  });
  win.webContents.on("will-navigate", (e, url) => {
    if (!url.startsWith(SERVER)) { e.preventDefault(); openInPane(url); return; }
    // /api/open?u=... is a REROUTE INSTRUCTION, not a page to visit. It is same-origin,
    // so the check above lets it through, the main frame commits the navigation, and the
    // webRequest handler above then cancels it -- stranding the window on
    // chrome-error://chromewebdata with no sidebar, no browser bar and no way back
    // (exactly the "carried away" failure this shell exists to prevent). Intercept it
    // here, before the frame commits, and hand the target to the pane.
    if (url.startsWith(SERVER + "/api/open")) {
      e.preventDefault();
      try {
        const u = new URL(url).searchParams.get("u");
        if (u) openInPane(u);
      } catch (err) { /* malformed reroute: stay put rather than navigate */ }
    }
  });

  // Belt and braces: if the app frame ever does land on an error page, put it back.
  // A blank window with no chrome gives the person no way to recover on their own.
  win.webContents.on("did-fail-load", (e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || code === -3) return;         // -3 = aborted, normal on redirects
    if (url && url.startsWith(SERVER) && !url.startsWith(SERVER + "/api/open")) return;
    win.loadURL(SERVER + "/");
  });

  win.on("resize", layoutPane);
  await win.loadURL(SERVER + "/");
}

// ------------------------------------------------------------- assisted fill
// The drawer version of the extension's assisted apply: inject the SHARED fill
// engine (extension/autofill_core.js) into the pane and run it with the person's
// saved payload. Fill-only, empty fields only, never the submit click (§7).
const FILLED_CSS =
  ".tailor-filled{outline:2px solid #12b76a !important;" +
  "background-color:#ecfdf3 !important;" +
  "transition:outline-color .4s ease, background-color .4s ease;}";

let _coreSource = null;
function autofillCore() {
  if (_coreSource) return _coreSource;
  const p = app.isPackaged
    ? path.join(process.resourcesPath, "autofill_core.js")
    : path.join(__dirname, "..", "extension", "autofill_core.js");
  _coreSource = fs.readFileSync(p, "utf8");
  return _coreSource;
}

function fetchAutofillPayload() {
  // Same trust position as the extension's background worker: local shell code
  // talking to the local engine, so it sends the guarded-route header.
  return net.fetch(SERVER + "/api/profile/autofill",
                   { headers: { "X-Tailor-Extension": "1" } })
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);
}

function waitPaneLoad(timeoutMs = 25000) {
  const wc = pane.webContents;
  if (!wc.isLoading()) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => { clearTimeout(t); wc.removeListener("did-finish-load", done); resolve(); };
    const t = setTimeout(done, timeoutMs);
    wc.once("did-finish-load", done);
  });
}

ipcMain.handle("assist:fill", async (_e, url) => {
  if (!/^https?:/i.test(url || "")) return { ok: false, reason: "bad-url" };
  // LinkedIn is browse-by-hand only, never automated (CLAUDE.md §7).
  try {
    if (/(^|\.)linkedin\.com$/i.test(new URL(url).hostname)) {
      openInPane(url);                       // they can still browse and fill by hand
      return { ok: false, reason: "linkedin" };
    }
  } catch (e) { return { ok: false, reason: "bad-url" }; }

  openInPane(url);
  await new Promise((r) => setTimeout(r, 400));   // navigation kickoff
  await waitPaneLoad();
  await new Promise((r) => setTimeout(r, 900));   // let SPA forms hydrate

  const payload = await fetchAutofillPayload();
  if (!payload || payload.loaded === false) return { ok: false, reason: "no-profile" };

  const wc = pane.webContents;
  try {
    await wc.insertCSS(FILLED_CSS);
    await wc.executeJavaScript(autofillCore(), true);
    const report = await wc.executeJavaScript(
      `window.TailorAutofill.fill(${JSON.stringify(payload)})`, true);
    return report && typeof report === "object" ? report : { ok: false, reason: "no-report" };
  } catch (err) {
    return { ok: false, reason: "inject-failed" };
  }
});

// Attach a local file to the page's first file input. executeJavaScript cannot do this --
// scripts may not set a file input's files -- so it goes through CDP, the same mechanism a
// human's drag-and-drop uses. Without it every "applied" form still needs the résumé
// dragged in by hand, which is the one step that makes the rest pointless.
async function attachResume(wc, filePath) {
  if (!filePath || !fs.existsSync(filePath)) return false;
  let attached = false;
  try {
    wc.debugger.attach("1.3");
  } catch (e) { /* already attached is fine */ }
  try {
    const { root } = await wc.debugger.sendCommand("DOM.getDocument", { depth: -1 });
    const { nodeId } = await wc.debugger.sendCommand("DOM.querySelector", {
      nodeId: root.nodeId, selector: 'input[type=file]' });
    if (nodeId) {
      await wc.debugger.sendCommand("DOM.setFileInputFiles", { nodeId, files: [filePath] });
      attached = true;
    }
  } catch (e) {
    attached = false;
  } finally {
    try { wc.debugger.detach(); } catch (e) { /* nothing to detach */ }
  }
  return attached;
}

// A wall we are NOT allowed to walk through (CLAUDE.md 7): a captcha or a sign-in gate is
// the site saying it does not want an automated submission. Detected so the apply path can
// stop and hand back to the person, never so it can try to get around it.
const WALL_JS = `(() => {
  const captcha = document.querySelector(
    'iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="turnstile"],[data-sitekey]');
  const login = /sign in|log in|create an account/i.test(document.body.innerText || '')
    && !!document.querySelector('input[type=password]');
  return captcha ? 'captcha' : (login ? 'login' : '');
})()`;

const SUBMIT_JS = `(() => {
  const vis = e => e.offsetParent !== null && !e.disabled;
  const label = e => ((e.innerText || e.value || '') + ' ' + (e.getAttribute('aria-label') || ''))
    .trim().toLowerCase();
  const all = [...document.querySelectorAll('button,input[type=submit]')].filter(vis);
  // Deliberately strict: only a control that says it submits THIS application. A loose
  // match here clicks "Attach", "Dropbox" or "Autofill my application" and looks like a
  // successful submit while nothing was sent.
  const btn = all.find(e => /^(submit application|submit|send application)$/.test(label(e)))
           || all.find(e => /^apply( now)?$/.test(label(e)));
  if (!btn) return { clicked: false, seen: all.map(label).filter(Boolean).slice(0, 8) };
  // Is something drawn OVER the button? A cookie banner or consent modal sitting on top
  // of Submit swallows the click, and a click that lands on the banner looks exactly like
  // a click that landed on the button. So look at what is actually at that point first,
  // and report it rather than pretend. (Diagnosis adapted from AIHawk, MIT; NOTICES.md.)
  try { btn.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) { /* fine */ }
  const r = btn.getBoundingClientRect();
  const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  if (top && top !== btn && !btn.contains(top) && !top.contains(btn)) {
    const box = top.closest('[role=dialog],[aria-modal=true],[class*=cookie],[id*=cookie],'
      + '[class*=consent],[id*=consent],[class*=banner],[id*=banner],[class*=modal],[class*=overlay]') || top;
    const what = (box.getAttribute('aria-label') || box.id || String(box.className || '').split(/\s+/)[0]
      || box.tagName || '').toString().slice(0, 60);
    return { clicked: false, covered_by: what, seen: [label(btn)] };
  }
  btn.click();
  return { clicked: true, label: label(btn) };
})()`;

// Apply = fill, attach the résumé, and submit. The person's click on Apply IS the
// authorisation; they asked not to be shown a form they never wanted to read. Everything
// the guarded path forbids still holds: never LinkedIn, never past a captcha or login
// wall, and a failure to find a real submit control is reported rather than assumed.
ipcMain.handle("assist:apply", async (_e, opts) => {
  const { url, resumePath } = opts || {};
  if (!/^https?:/i.test(url || "")) return { ok: false, reason: "bad-url" };
  try {
    if (/(^|\.)linkedin\.com$/i.test(new URL(url).hostname)) {
      return { ok: false, reason: "linkedin" };
    }
  } catch (e) { return { ok: false, reason: "bad-url" }; }

  openInPane(url);
  await new Promise((r) => setTimeout(r, 400));
  await waitPaneLoad();
  await new Promise((r) => setTimeout(r, 1200));   // let the ATS form hydrate

  const payload = await fetchAutofillPayload();
  if (!payload || payload.loaded === false) return { ok: false, reason: "no-profile" };

  const wc = pane.webContents;
  let report;
  try {
    await wc.insertCSS(FILLED_CSS);
    await wc.executeJavaScript(autofillCore(), true);
    report = await wc.executeJavaScript(
      `window.TailorAutofill.fill(${JSON.stringify(payload)})`, true);
  } catch (err) {
    return { ok: false, reason: "inject-failed" };
  }

  if (report && report.ok === false) {
    return report;                       // auth wall: the form isn't reachable yet
  }

  const attached = await attachResume(wc, resumePath);
  await new Promise((r) => setTimeout(r, 500));

  // Fill happens either way; only SUBMISSION stops at the wall. The form is now complete
  // and waiting on the one thing only a person can do.
  const wall = (report && report.captcha) ? "captcha"
             : await wc.executeJavaScript(WALL_JS, true);
  if (wall) {
    return { ...report, ok: true, submitted: false, reason: wall, resume_attached: attached };
  }

  const click = await wc.executeJavaScript(SUBMIT_JS, true);
  if (!click || !click.clicked) {
    return { ...report, ok: true, submitted: false,
             reason: click && click.covered_by ? "submit-covered" : "no-submit-button",
             covered_by: click && click.covered_by, seen: click && click.seen,
             resume_attached: attached };
  }
  await new Promise((r) => setTimeout(r, 3500));    // let the POST land

  const confirmed = await wc.executeJavaScript(`(() => {
    const t = (document.body.innerText || '').toLowerCase();
    return /thank you|application (was )?(submitted|received)|we('| ha)?ve received/.test(t);
  })()`, true);

  return { ...report, ok: true, submitted: true, confirmed: !!confirmed,
           resume_attached: attached, clicked: click.label };
});

// Fill whatever page is CURRENTLY open in the pane, without navigating away.
// Multi-step ATS flows (Amazon, Workday) gate the form behind sign-in and several
// steps; the person walks there in the Browser tab, then fills in place.
ipcMain.handle("assist:fill-current", async () => {
  if (!pane) return { ok: false, reason: "no-page" };
  const wc = pane.webContents;
  const url = wc.getURL() || "";
  if (!/^https?:/i.test(url)) return { ok: false, reason: "no-page" };
  try {
    if (/(^|\.)linkedin\.com$/i.test(new URL(url).hostname)) {
      return { ok: false, reason: "linkedin" };
    }
  } catch (e) { return { ok: false, reason: "no-page" }; }
  const payload = await fetchAutofillPayload();
  if (!payload || payload.loaded === false) return { ok: false, reason: "no-profile" };
  try {
    await wc.insertCSS(FILLED_CSS);
    await wc.executeJavaScript(autofillCore(), true);
    const report = await wc.executeJavaScript(
      `window.TailorAutofill.fill(${JSON.stringify(payload)})`, true);
    return report && typeof report === "object" ? report : { ok: false, reason: "no-report" };
  } catch (err) {
    return { ok: false, reason: "inject-failed" };
  }
});

// ------------------------------------------------------------- AI autonomous fill (autoapply)
// The AI-planned upgrade of assisted apply: extract the open form's STRUCTURE, ask the engine's
// brain (POST /api/autoapply/plan) which data goes where -- the person's real data NEVER reaches the
// model, only field structure + placeholder keys -- then fill it in the pane and STOP for the person
// to review and submit. Résumé upload is fulfilled here via CDP (script can't set a file input).
// Never clicks submit; drops to the person on any captcha/auth wall or a prohibited site (section 7).
async function cdpUploadFiles(wc, refs, filePath) {
  const dbg = wc.debugger;
  let attached = false;
  try {
    if (!dbg.isAttached()) { dbg.attach("1.3"); attached = true; }
  } catch (e) { return 0; }              // devtools already attached -> leave the résumé for the person
  let done = 0;
  try {
    await dbg.sendCommand("DOM.enable");
    for (const ref of refs) {
      try {
        const sel = JSON.stringify('[data-tailor-ref="' + ref + '"]');
        const ev = await dbg.sendCommand("Runtime.evaluate", { expression: "document.querySelector(" + sel + ")" });
        const objectId = ev && ev.result && ev.result.objectId;
        if (!objectId) continue;
        const node = await dbg.sendCommand("DOM.requestNode", { objectId });
        await dbg.sendCommand("DOM.setFileInputFiles", { files: [filePath], nodeId: node.nodeId });
        done++;
      } catch (e) { /* skip this one input; the person can still attach it */ }
    }
  } finally {
    if (attached) { try { dbg.detach(); } catch (e) { /* ignore */ } }
  }
  return done;
}

async function runAutoApplyOnPane(recordId) {
  if (!pane) return { ok: false, reason: "no-page" };
  const wc = pane.webContents;
  const url = wc.getURL() || "";
  if (!/^https?:/i.test(url)) return { ok: false, reason: "no-page" };
  try {
    if (/(^|\.)linkedin\.com$/i.test(new URL(url).hostname)) return { ok: false, reason: "linkedin" };
  } catch (e) { return { ok: false, reason: "no-page" }; }

  try {
    await wc.insertCSS(FILLED_CSS);
    await wc.executeJavaScript(autofillCore(), true);
    const blocker = await wc.executeJavaScript("window.TailorAutofill.pageBlocker()", true);
    if (blocker) return { ok: false, reason: "blocked", blocker };          // never fight a captcha/auth wall
    const fields = await wc.executeJavaScript("window.TailorAutofill.extractForm()", true);
    if (!fields || !fields.length) return { ok: false, reason: "no-fields" };

    let plan = null;
    try {
      const resp = await net.fetch(SERVER + "/api/autoapply/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Tailor-Extension": "1" },
        body: JSON.stringify({ url, record_id: recordId, fields }),
      });
      plan = resp.ok ? await resp.json() : null;
    } catch (e) { plan = null; }
    if (!plan || !plan.ok) return { ok: false, reason: (plan && plan.reason) || "no-plan" };

    const ops = plan.ops || [];
    const report = await wc.executeJavaScript(
      `window.TailorAutofill.applyOps(${JSON.stringify(ops)})`, true);

    let uploaded = 0;
    const resumePath = (ops.find((o) => o.op === "upload") || {}).path || "";
    if (report && (report.uploads || []).length && resumePath) {
      uploaded = await cdpUploadFiles(wc, report.uploads, resumePath);
    }
    return { ok: true, filled: (report && report.filled) || 0, uploaded,
             pending_upload: Math.max(0, ((report && (report.uploads || []).length) || 0) - uploaded),
             skipped: (report && report.skipped) || 0 };
  } catch (err) {
    return { ok: false, reason: "inject-failed" };
  }
}

// Open the apply URL in the pane, let it settle, then AI-fill it.
ipcMain.handle("autoapply:run", async (_e, url, recordId) => {
  if (!/^https?:/i.test(url || "")) return { ok: false, reason: "bad-url" };
  try {
    if (/(^|\.)linkedin\.com$/i.test(new URL(url).hostname)) { openInPane(url); return { ok: false, reason: "linkedin" }; }
  } catch (e) { return { ok: false, reason: "bad-url" }; }
  openInPane(url);
  await new Promise((r) => setTimeout(r, 400));   // navigation kickoff
  await waitPaneLoad();
  await new Promise((r) => setTimeout(r, 900));   // let SPA forms hydrate
  return runAutoApplyOnPane(recordId);
});

// AI-fill whatever page is CURRENTLY open in the pane (after the person navigated / signed in).
ipcMain.handle("autoapply:fill-current", async (_e, recordId) => runAutoApplyOnPane(recordId));

ipcMain.on("pane:set-bounds", (_e, rect) => { paneBounds = rect; layoutPane(); });
ipcMain.on("pane:open", (_e, url) => openInPane(url));
ipcMain.on("pane:close", () => closePane());
ipcMain.on("pane:back", () => pane && pane.webContents.navigationHistory.goBack());
ipcMain.on("pane:forward", () => pane && pane.webContents.navigationHistory.goForward());
ipcMain.on("pane:reload", () => pane && pane.webContents.reload());
ipcMain.on("pane:navigate", (_e, url) => {
  if (opensInOwnBrowser(url)) { shell.openExternal(url); return; }
  if (pane && /^https?:/i.test(url)) pane.webContents.loadURL(url);
});
ipcMain.handle("pane:get-state", () => paneState());

// Dev only: start the managed-AI broker (avatar + bundled LLM) so the live interview works on
// launch with no manual setup, and put the dev user on a plan with minutes. In production the
// broker is a HOSTED service. Best-effort: if it fails to start (e.g. no venv python), the app
// still runs and the live interview just falls back / shows service-unavailable.
const BROKER = "http://127.0.0.1:57001";
function retireStaleBroker() {
  /* Stop a leftover broker that is holding the port while serving a fake model.
   *
   * Scoped deliberately narrowly: only a process whose command line runs THIS repo's
   * `backend.server` is touched, so an unrelated program that happens to hold 57001 is
   * left alone and we simply fail to start our own. Windows has no lsof; there the stale
   * broker is left in place and the client-side "[fake:" guard is the backstop.
   */
  if (process.platform === "win32") return Promise.resolve();
  return new Promise((resolve) => {
    const { execFile } = require("child_process");
    execFile("/bin/sh", ["-c",
      "for p in $(lsof -ti tcp:57001 2>/dev/null); do "
      + "ps -p $p -o command= | grep -q 'backend.server' && kill $p; done"],
      () => setTimeout(resolve, 800));
  });
}

function getJson(url) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      let body = "";
      res.on("data", (c) => { body += c; });
      res.on("end", () => { try { resolve(JSON.parse(body)); } catch (e) { resolve(null); } });
    });
    req.on("error", () => resolve(null));
    req.setTimeout(2000, () => { req.destroy(); resolve(null); });
  });
}

async function ensureBroker() {
  if (app.isPackaged) return;
  // A developer running their own broker on purpose (e.g. with a Telegram bot configured) sets
  // TAILOR_KEEP_BROKER=1 so the shell never retires it as "stale".
  if (process.env.TAILOR_KEEP_BROKER === "1") return;
  // Reuse a running broker ONLY if it is serving a real model. A broker left over from an
  // earlier session answers /health perfectly while returning FakeProvider placeholder
  // text -- which then flows into the person's resume looking like real writing. Adopting
  // it silently is how a stale process poisons every build for days. If the one holding
  // the port is fake, retire it and start a current one.
  const health = await getJson(BROKER + "/health");
  if (health && health.ok) {
    if (health.real_providers) return;
    await retireStaleBroker();
  }
  try {
    brokerChild = spawn(resolvePython(), ["-m", "backend.server"], {
      cwd: path.join(__dirname, ".."),
      env: { ...process.env, TAILOR_DEV_PLAN: process.env.TAILOR_DEV_PLAN || "pass30" },
      windowsHide: true, stdio: "ignore",
    });
  } catch (e) { return; }
  for (let i = 0; i < 20; i++) {                        // wait up to ~10s, never blocking the window
    if (await ping(BROKER + "/health")) return;
    await new Promise((r) => setTimeout(r, 500));
  }
}

app.whenReady().then(async () => {
  // Dark window chrome regardless of launcher or OS: opt Electron into Windows' immersive
  // dark title bar (fixes the white frame on native-Windows launches). Harmless no-op under
  // WSLg, where the Windows host already paints the frame dark. backgroundColor (below) only
  // themes the content, never the OS-drawn frame, which is why it wasn't enough on its own.
  nativeTheme.themeSource = "dark";
  try {
    await ensureServer();
  } catch (e) {
    // Say why and quit, rather than leaving a window-less app or a broken page behind.
    dialog.showErrorBox("SponsorJobs could not start", String((e && e.message) || e));
    app.quit();
    return;
  }
  ensureBroker();         // best-effort, in the background; do not block the window on it
  await createWindow();
});

app.on("window-all-closed", () => app.quit());
app.on("will-quit", () => {
  // Kill the WHOLE Python tree, not just the wrapper (the orphan-listener lesson).
  for (const child of [pyChild, brokerChild]) {
    if (!child || child.killed) continue;
    if (process.platform === "win32") {
      try { spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], { windowsHide: true }); }
      catch (e) { child.kill(); }
    } else {
      child.kill();
    }
  }
});
