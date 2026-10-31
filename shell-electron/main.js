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
const { paneRect } = require("./panegeom");
const { app, BrowserWindow, WebContentsView, dialog, ipcMain, session, net, nativeTheme, shell, Notification } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");
const frames = require("./frames");
const { isNewPage, pageMessage, wallMessage } = require("./pagewatch");
const { createAgent } = require("./agent");
const { isSupportMailto } = require("./links");

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

// Tiny per-machine preferences file for the shell (window state). Not user data.
const PREFS_PATH = () => path.join(app.getPath("userData"), "shell-prefs.json");
function readPrefs() {
  try { return JSON.parse(fs.readFileSync(PREFS_PATH(), "utf8")) || {}; } catch (e) { return {}; }
}
function writePrefs(p) {
  try { fs.writeFileSync(PREFS_PATH(), JSON.stringify(p)); } catch (e) { /* best effort */ }
}

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
  if (app.isPackaged) {
    // The official installer is the "SponsorJobs" edition: AI runs on the company service, so
    // nobody needs a key (docs/editions in memory: decided 2026-10-07). Running from source is
    // the open-source Community edition, bring-your-own-key.
    env.TAILOR_EDITION = env.TAILOR_EDITION || "official";
    env.TAILOR_BROKER_URL = env.TAILOR_BROKER_URL || "https://api.sponsorjobs.ai";
  }
  // macOS keeps every large block the engine frees in a per-process cache, and that cache counts
  // against the app's memory: reading the job descriptions left ~250 MB of freed blocks parked
  // there (measured 2026-10-21). Off, a freed block goes straight back to the system. Other
  // platforms ignore the variable.
  if (process.platform === "darwin" && env.MallocLargeCache === undefined) env.MallocLargeCache = "0";
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
  // CSS pixels from the page, scaled to window points by the app's zoom (panegeom.js).
  pane.setBounds(paneRect(paneBounds, win.webContents.getZoomFactor()));
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
    // Google sign-in returns the bearer token: #token=... on the app's callback page (current
    // broker), or ?account=... (older broker).
    const acc = /[#&]token=([^&#]+)/.exec(url) || /[?&]account=([^&#]+)/.exec(url);
    if (acc) { e.preventDefault(); closePane(); send("signin:return", { token: decodeURIComponent(acc[1]) }); return; }
    const gerr = /[#&]error=([a-z_]+)/.exec(url);   // #error=cancelled | #error=signin_failed
    if (gerr) { e.preventDefault(); closePane(); send("signin:return", { error: gerr[1] }); return; }
    if (/[?&]signin=error\b/.test(url)) { e.preventDefault(); closePane(); send("signin:return", { error: "signin_failed" }); }
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
  // Payment pages open in the person's own browser (saved cards, password managers, a real
  // address bar), never in the in-app pane behind the Passes window (Kofi, 2026-10-23).
  "checkout.stripe.com", "billing.stripe.com",
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
  clearTimeout(paneReap);
  paneReap = null;
  ensurePane();
  paneOpen = true;
  pane.webContents.loadURL(url);
  layoutPane();
  pushPaneState();
}

// A closed pane is hidden at once, and its page is torn down a minute later: a hidden job site
// (Workday, iCIMS, ...) kept its whole renderer process, often several hundred MB, alive and
// running for the rest of the session (2026-10-21, 8 GB laptops). The minute keeps a quick
// close-and-reopen exactly as before; the next open after that starts a fresh pane.
const PANE_REAP_MS = 60 * 1000;
let paneReap = null;

function closePane() {
  paneOpen = false;
  layoutPane();
  pushPaneState();
  clearTimeout(paneReap);
  paneReap = setTimeout(destroyPane, PANE_REAP_MS);
}

function destroyPane() {
  clearTimeout(paneReap);
  paneReap = null;
  if (!pane || paneOpen) return;
  disarmSuccessWatch();
  disarmPageWatch();
  const p = pane;
  pane = null;
  try { if (win && !win.isDestroyed()) win.contentView.removeChildView(p); } catch (e) { /* window gone */ }
  try { if (!p.webContents.isDestroyed()) p.webContents.close(); } catch (e) { /* already closed */ }
  pushPaneState();
}

async function createWindow() {
  // Open in macOS native full screen, like Word: the window fills the display and the menu bar
  // hides until the pointer reaches the top edge (Kofi, 2026-10-09). Native full screen (not a
  // kiosk) so the green button, Mission Control and Cmd+Ctrl+F all behave as on any Mac app, and
  // the person can leave it. Remembered per machine: leaving full screen once sticks.
  const prefs = readPrefs();
  const startFullscreen = prefs.fullscreen !== false;
  win = new BrowserWindow({
    width: 1560,
    height: 980,
    fullscreen: startFullscreen,
    // No grey title bar, like Word: the traffic lights float over the top of the rail and the
    // page runs to the window's edge. In native full screen they hide altogether. The rail's
    // top strip is marked draggable in CSS (`-webkit-app-region: drag`) so the window can
    // still be moved when it is not full screen.
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 14, y: 16 },
    backgroundColor: "#111111",
    title: "SponsorJobs",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      // The live interview's timekeeper runs on a one-second timer in this page; a throttled
      // background window would deliver its notes to the interviewer late.
      backgroundThrottling: false,
    },
  });
  win.setMenuBarVisibility(false);
  win.on("leave-full-screen", () => writePrefs({ ...readPrefs(), fullscreen: false }));
  win.on("enter-full-screen", () => writePrefs({ ...readPrefs(), fullscreen: true }));

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
    if (isSupportMailto(url)) { shell.openExternal(url); return { action: "deny" }; }
    if (/^https?:/i.test(url) && !url.startsWith(SERVER)) {
      openInPane(url);
      return { action: "deny" };
    }
    return { action: "allow" };
  });
  win.webContents.on("will-navigate", (e, url) => {
    if (isSupportMailto(url)) { e.preventDefault(); shell.openExternal(url); return; }
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
  win.webContents.on("zoom-changed", () => setTimeout(layoutPane, 50));
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

// The apply agent's page serializer and in-page actions (extension/agent_view.js), injected
// into the pane the same way as the fill engine.
let _agentViewSource = null;
function agentViewSource() {
  if (_agentViewSource) return _agentViewSource;
  const p = app.isPackaged
    ? path.join(process.resourcesPath, "agent_view.js")
    : path.join(__dirname, "..", "extension", "agent_view.js");
  _agentViewSource = fs.readFileSync(p, "utf8");
  return _agentViewSource;
}

function getApp(path) {
  return net.fetch(SERVER + path, { headers: { "X-Tailor-Extension": "1" } })
    .then((r) => (r.ok ? r.json() : null)).catch(() => null);
}

function fetchAutofillPayload() {
  // Same trust position as the extension's background worker: local shell code
  // talking to the local engine, so it sends the guarded-route header. The payload
  // carries the fill-recipe table too (also served at /api/autofill/recipes).
  return net.fetch(SERVER + "/api/profile/autofill",
                   { headers: { "X-Tailor-Extension": "1" } })
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);
}

function postApp(path, body) {
  return net.fetch(SERVER + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Tailor-Extension": "1" },
    body: JSON.stringify(body || {}),
  }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
}

// After a fill, watch the pane for the site's "application received" marker. The PERSON
// submits in their own time; when the recipe's success marker appears (or the generic wording
// does), the app marks the matching package applied and the UI is told. One watch at a time,
// re-armed by every fill, dropped after 30 minutes or when the pane closes.
let _successWatch = null;
function armSuccessWatch(wc, startUrl, recordId) {
  disarmSuccessWatch();
  const started = Date.now();
  let done = false;
  const check = async () => {
    if (done || !pane || pane.webContents !== wc || wc.isDestroyed()) return;
    if (Date.now() - started > 30 * 60 * 1000) { disarmSuccessWatch(); return; }
    let hit = false;
    try {
      const has = await wc.executeJavaScript("!!window.TailorAutofill", true);
      if (!has) await wc.executeJavaScript(autofillCore(), true);
      hit = await wc.executeJavaScript("window.TailorAutofill.detectSuccess()", true);
      if (!hit) hit = await frameSuccess(wc);              // iCIMS confirms inside its iframe
    } catch (e) { hit = false; }
    if (!hit) return;
    done = true;
    disarmSuccessWatch();
    const pageUrl = wc.getURL() || "";
    const res = await postApp("/api/autofill/applied",
                              { url: startUrl, page_url: pageUrl, record_id: recordId || null });
    if (win && !win.isDestroyed()) {
      win.webContents.send("assist:applied", Object.assign({ url: startUrl, page_url: pageUrl }, res || { ok: false }));
    }
  };
  const timer = setInterval(check, 2500);
  const onNav = () => setTimeout(check, 1500);
  for (const ev of ["did-navigate", "did-navigate-in-page", "did-finish-load"]) wc.on(ev, onNav);
  _successWatch = { wc, timer, onNav };
}
// One CDP attachment shared by everything that needs it (file uploads, the frame fill, the frame
// success check): attached on first use, detached when the last user is done, and never torn
// down under another user's feet. If DevTools already holds the debugger, CDP is unavailable
// and each caller falls back (the person attaches the resume; frames are left to them).
const _dbgUsers = new WeakMap();      // webContents -> {n, owned}
function acquireDebugger(wc) {
  const dbg = wc.debugger;
  let st = _dbgUsers.get(wc);
  if (!st || st.n === 0) {
    st = { n: 0, owned: false };
    try {
      if (!dbg.isAttached()) { dbg.attach("1.3"); st.owned = true; }
    } catch (e) { return null; }
    _dbgUsers.set(wc, st);
  }
  st.n++;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    st.n--;
    if (st.n === 0 && st.owned) { try { dbg.detach(); } catch (e) { /* gone */ } }
  };
}

// The "application received" marker inside a cross-origin frame (read only, nothing is filled).
async function frameSuccess(wc) {
  let count = 0;
  try { count = await wc.executeJavaScript("window.TailorAutofill.crossOriginFrames()", true); } catch (e) { count = 0; }
  if (!count) return false;
  const release = acquireDebugger(wc);
  if (!release) return false;
  const cdp = frames.electronCdp(wc.debugger);
  try {
    for (const ctx of await frames.crossOriginFrames(cdp)) {
      try {
        await frames.injectEngine(cdp, ctx, autofillCore());
        if (await frames.evalIn(cdp, ctx, "window.TailorAutofill.detectSuccess()")) return true;
      } catch (e) { /* frame gone */ }
    }
    return false;
  } catch (e) {
    return false;
  } finally {
    cdp.dispose();
    release();
  }
}
function disarmSuccessWatch() {
  if (!_successWatch) return;
  clearInterval(_successWatch.timer);
  try {
    for (const ev of ["did-navigate", "did-navigate-in-page", "did-finish-load"]) {
      _successWatch.wc.removeListener(ev, _successWatch.onNav);
    }
  } catch (e) { /* pane gone */ }
  _successWatch = null;
}

// Learn from corrections: the engine remembers what it filled; whatever the person changed
// since is offered back to the profile (only on their click in the app).
ipcMain.handle("assist:learn", async () => {
  if (!pane) return { ok: false, reason: "no-page" };
  const wc = pane.webContents;
  let corrections = {};
  try {
    corrections = await wc.executeJavaScript(
      "window.TailorAutofill ? window.TailorAutofill.takeCorrections() : {}", true);
  } catch (e) { return { ok: false, reason: "inject-failed" }; }
  if (!corrections || !Object.keys(corrections).length) return { ok: true, learned: [] };
  const fields = {}, screening = {};
  for (const [k, v] of Object.entries(corrections)) {
    if (k === "work_auth_us" || k === "work_authorized" || k === "needs_sponsorship") screening[k] = v;
    else fields[k] = v;
  }
  const res = await postApp("/api/profile/autofill", { fields, screening });
  return res || { ok: false, reason: "app-unreachable" };
});

function waitPaneLoad(timeoutMs = 25000) {
  const wc = pane.webContents;
  if (!wc.isLoading()) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => { clearTimeout(t); wc.removeListener("did-finish-load", done); resolve(); };
    const t = setTimeout(done, timeoutMs);
    wc.once("did-finish-load", done);
  });
}

async function armCorrections(wc) {
  try { await wc.executeJavaScript("window.TailorAutofill.watchCorrections(null)", true); }
  catch (e) { /* the page may have navigated; the next fill re-arms it */ }
}

// Attach a local file to the page's first file input. executeJavaScript cannot do this --
// scripts may not set a file input's files -- so it goes through CDP, the same mechanism a
// human's drag-and-drop uses. Without it every "applied" form still needs the résumé
// dragged in by hand, which is the one step that makes the rest pointless.
async function attachResume(wc, filePath) {
  if (!filePath || !fs.existsSync(filePath)) return false;
  let attached = false;
  const release = acquireDebugger(wc);
  if (!release) return false;
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
    release();
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

// Find the site's own final submit control. With click=false (every site NOT on the verified
// AUTO allowlist) it only scrolls to the button and outlines it so the person sees where THEIR
// click goes; with click=true (allowlisted sites only, decided by the engine) it clicks it.
function submitScript(click) {
  return `(() => {
  const doClick = ${click ? "true" : "false"};
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
  if (!doClick) {
    try { btn.classList.add('tailor-submit-hint'); } catch (e) { /* non-element */ }
    if (!document.getElementById('tailor-submit-hint-css')) {
      const st = document.createElement('style'); st.id = 'tailor-submit-hint-css';
      st.textContent = '.tailor-submit-hint{outline:3px solid #f79009 !important;outline-offset:3px !important}';
      (document.head || document.documentElement).appendChild(st);
    }
    return { clicked: false, highlighted: true, label: label(btn) };
  }
  btn.click();
  return { clicked: true, label: label(btn) };
})()`;
}

// ------------------------------------------------------------- AI autonomous fill (autoapply)
// The AI-planned upgrade of assisted apply: extract the open form's STRUCTURE, ask the engine's
// brain (POST /api/autoapply/plan) which data goes where -- the person's real data NEVER reaches the
// model, only field structure + placeholder keys -- then fill it in the pane and STOP for the person
// to review and submit. Résumé upload is fulfilled here via CDP (script can't set a file input).
// Never clicks submit; drops to the person on any captcha/auth wall or a prohibited site (section 7).
async function cdpUploadFiles(wc, refs, filePath) {
  const dbg = wc.debugger;
  const release = acquireDebugger(wc);
  if (!release) return 0;                // devtools already attached -> leave the résumé for the person
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
    release();
  }
  return done;
}

// The frames of the pane the top page's engine cannot enter (cross-origin: iCIMS and friends),
// opened through CDP (shell-electron/frames.js). Only frames on hosts the engine's policy lets
// SponsorJobs fill are returned. Returns null when there are none or the debugger is busy
// (DevTools open): then the top page alone is filled, as before.
async function openFrameSession(wc) {
  let count = 0;
  try { count = await wc.executeJavaScript("window.TailorAutofill.crossOriginFrames()", true); } catch (e) { count = 0; }
  if (!count) return null;
  const release = acquireDebugger(wc);
  if (!release) return null;
  const cdp = frames.electronCdp(wc.debugger);
  const close = () => { cdp.dispose(); release(); };
  try {
    const ctxs = [];
    for (const ctx of await frames.crossOriginFrames(cdp)) {
      const d = await applyDecision(ctx.url, "");
      if (!d.fill) continue;                               // a prohibited board's frame: hands off
      try {
        await frames.injectEngine(cdp, ctx, autofillCore());
        await frames.evalIn(cdp, ctx, `(() => { if (document.getElementById("tailor-filled-css")) return;
          const st = document.createElement("style"); st.id = "tailor-filled-css";
          st.textContent = ${JSON.stringify(FILLED_CSS)};
          (document.head || document.documentElement).appendChild(st); })()`);
        ctxs.push(ctx);
      } catch (e) { /* the frame navigated away mid-way */ }
    }
    if (!ctxs.length) { close(); return null; }
    return { cdp, ctxs, close,
             eval: (ctx, expr) => frames.evalIn(cdp, ctx, expr).catch(() => null) };
  } catch (e) {
    close();
    return null;
  }
}

// What one fill run did on one page, summed over the top page and its frames.
function sumPre(list) {
  const out = { filled: 0, system: "", system_name: "", next_page: false, continue_hint: "",
                submit_highlighted: false, captcha: false };
  for (const p of list) {
    if (!p || !p.ok) continue;
    out.filled += (p.filled || 0) + (p.screening || 0) + (p.eeo || 0);
    out.system = out.system || p.system || "";
    out.system_name = out.system_name || p.system_name || "";
    out.next_page = out.next_page || !!p.next_page;
    out.continue_hint = out.continue_hint || p.continue_hint || "";
    out.submit_highlighted = out.submit_highlighted || !!p.submit_highlighted;
    out.captcha = out.captcha || !!p.captcha;
  }
  return out;
}

// opts: { page, fromWatch }. Fills the page open in the pane: the top document (with its open
// shadow roots and same-origin frames) and every cross-origin frame it is allowed to fill; then
// reports {filled, left:[labels]} for this page, saves that on the application record, and keeps
// watching for the person's Next so the following page fills itself (never clicking Next or
// Submit itself).
async function runAutoApplyOnPane(recordId, opts) {
  opts = opts || {};
  if (!pane) return { ok: false, reason: "no-page" };
  const wc = pane.webContents;
  const url = wc.getURL() || "";
  if (!/^https?:/i.test(url)) return { ok: false, reason: "no-page" };
  try {
    if (/(^|\.)linkedin\.com$/i.test(new URL(url).hostname)) return { ok: false, reason: "linkedin" };
  } catch (e) { return { ok: false, reason: "no-page" }; }

  let fr = null;
  try {
    await wc.insertCSS(FILLED_CSS);
    await wc.executeJavaScript(autofillCore(), true);
    const page = opts.page || pageNumberFor(wc, recordId, await mainSignature(wc));
    const blocker = await wc.executeJavaScript("window.TailorAutofill.pageBlocker()", true);
    if (blocker === "auth") {                              // never fight an auth wall
      sendProgress({ record_id: recordId || null, page, wall: "auth", filled: 0, left: [],
                     message: wallMessage(page) });
      if (!opts.fromWatch) {
        armPageWatch(wc, recordId, page - 1, await mainSignature(wc));
        if (_pageWatch) _pageWatch.wallSaid = true;      // already said above
      }
      return { ok: false, reason: "blocked", blocker, page };
    }
    // A job description page is not a form: its only boxes are the site's own job search,
    // and a fill once typed the person's city into Amazon's. Checked on the first page only
    // (later pages of a form are reached through it), and only when no cross-origin frame
    // could be holding the real form (iCIMS), which the top page can't see into.
    if (!opts.fromWatch) {
      const form = await wc.executeJavaScript(
        "window.TailorAutofill.applicationForm() || window.TailorAutofill.crossOriginFrames() > 0", true);
      if (!form) {
        const apply = await wc.executeJavaScript("window.TailorAutofill.applyButtonLabel()", true);
        return { ok: false, reason: "not-a-form", apply, page };
      }
    }
    // Order of play: 1. the site's fill recipe, 2. the label patterns, 3. custom widgets (all
    // inside fillAsync), 4. the model plan for whatever is STILL empty. Deterministic first, the
    // model last, and the model only ever sees field structure, never the person's data.
    const payload = await fetchAutofillPayload();
    const havePayload = !!(payload && payload.loaded !== false);
    const fillExpr = havePayload ? `window.TailorAutofill.fillAsync(${JSON.stringify(payload)})` : "null";
    const pres = [];
    if (havePayload) {
      try { pres.push(await wc.executeJavaScript(fillExpr, true)); } catch (e) { /* fill what we can below */ }
    }
    fr = await openFrameSession(wc);
    const liveFrames = [];
    for (const ctx of (fr ? fr.ctxs : [])) {
      const b = await fr.eval(ctx, "window.TailorAutofill.pageBlocker()");
      if (b === "auth") continue;                          // a sign-in inside the frame: theirs
      liveFrames.push(ctx);
      if (havePayload) pres.push(await fr.eval(ctx, fillExpr));
    }
    const pre = sumPre(pres);

    const extract = "window.TailorAutofill.extractFormAsync({ onlyEmpty: true })";
    let fields = (await wc.executeJavaScript(extract, true)) || [];
    for (const ctx of liveFrames) fields = fields.concat((await fr.eval(ctx, extract)) || []);

    let applied = { filled: 0, uploaded: 0, pending_upload: 0, skipped: 0 };
    let planReason = "";
    if (fields.length) {
      let plan = null;
      try {
        const resp = await net.fetch(SERVER + "/api/autoapply/plan", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Tailor-Extension": "1" },
          body: JSON.stringify({ url, record_id: recordId, fields }),
        });
        plan = resp.ok ? await resp.json() : null;
      } catch (e) { plan = null; }
      if (plan && plan.ok) {
        const ops = plan.ops || [];
        const routed = frames.routeOps(ops, liveFrames);
        const resumePath = (ops.find((o) => o.op === "upload") || {}).path || "";
        const runOps = (list) => `window.TailorAutofill.applyOpsAsync(${JSON.stringify(list)})`;
        const reports = [];
        if (routed.main.length) {
          const rep = await wc.executeJavaScript(runOps(routed.main), true);
          reports.push(rep);
          if (rep && (rep.uploads || []).length && resumePath) {
            applied.uploaded += await cdpUploadFiles(wc, rep.uploads, resumePath);
          }
        }
        for (const [ctx, list] of routed.byFrame) {
          const rep = await fr.eval(ctx, runOps(list));
          reports.push(rep);
          if (rep && (rep.uploads || []).length && resumePath) {
            applied.uploaded += await frames.uploadInFrame(fr.cdp, ctx, rep.uploads, resumePath);
          }
        }
        for (const rep of reports) {
          applied.filled += (rep && rep.filled) || 0;
          applied.skipped += (rep && rep.skipped) || 0;
          applied.pending_upload += ((rep && rep.uploads) || []).length;
        }
        applied.pending_upload = Math.max(0, applied.pending_upload - applied.uploaded);
      } else {
        planReason = (plan && plan.reason) || "no-plan";
      }
    }

    // What is still blank and needs the person, across the page and its frames.
    let left = (await wc.executeJavaScript("window.TailorAutofill.leftover()", true)) || [];
    for (const ctx of liveFrames) left = left.concat((await fr.eval(ctx, "window.TailorAutofill.leftover()")) || []);
    left = [...new Set(left)].slice(0, 60);
    const filled = pre.filled + applied.filled;

    await armCorrections(wc); armSuccessWatch(wc, url, recordId);
    if (fr) { fr.close(); fr = null; }
    const sig = await mainSignature(wc);
    if (!opts.fromWatch) armPageWatch(wc, recordId, page, sig);
    if (!filled && !applied.uploaded && !fields.length && !left.length) {
      // A whole-page bot check instead of the form (SmartRecruiters' DataDome page): say so, so
      // the person clears it themselves; we never touch the challenge (CLAUDE.md section 7).
      if (blocker === "captcha") return { ok: false, reason: "blocked", blocker, page };
      return { ok: false, reason: "no-fields", page };
    }
    if (!filled && !applied.uploaded && planReason) return { ok: false, reason: planReason, page, left };
    const report = { page, url: wc.getURL() || url, filled, left, system: pre.system };
    recordFillReport(recordId, report);
    if (!opts.quiet) sendProgress(Object.assign({ record_id: recordId || null, message: pageMessage(page, filled, left) }, report));
    return { ok: true, filled, uploaded: applied.uploaded, pending_upload: applied.pending_upload,
             skipped: applied.skipped, left, page,
             system: pre.system, system_name: pre.system_name, next_page: pre.next_page,
             continue_hint: pre.continue_hint, submit_highlighted: pre.submit_highlighted,
             captcha: pre.captcha };
  } catch (err) {
    return { ok: false, reason: "inject-failed" };
  } finally {
    if (fr) fr.close();
  }
}

// ------------------------------------------------------------- multi-page forms
// Workday, SmartRecruiters, Taleo and iCIMS split an application over several pages. After a
// fill, the shell watches the pane; when the PERSON presses Next/Continue and a new page of
// questions appears (a URL change, a frame navigating, or the form's fields changing in a
// single-page app), it fills that page too and says so ("Page 2 filled: 9 fields"). It never
// presses Next or Submit itself, and at a sign-in / create-account wall it stops and says so;
// once the person is past it, the next page fills as usual. One watch at a time, 30 minutes max.
function sendProgress(info) {
  if (win && !win.isDestroyed()) win.webContents.send("autoapply:progress", info);
}
function recordFillReport(recordId, report) {
  if (recordId === undefined || recordId === null || recordId === "") return;
  postApp("/api/autoapply/fill-report", Object.assign({ record_id: recordId }, report));
}

async function mainSignature(wc) {
  try {
    const has = await wc.executeJavaScript("!!window.TailorAutofill", true);
    if (!has) await wc.executeJavaScript(autofillCore(), true);
    return await wc.executeJavaScript("window.TailorAutofill.pageSignature()", true);
  } catch (e) { return null; }
}


let _pageWatch = null;
function pageNumberFor(wc, recordId, sig) {
  const w = _pageWatch;
  if (!w || w.wc !== wc || w.recordId !== recordId) return 1;
  return isNewPage(w.labels, sig) ? w.page + 1 : Math.max(1, w.page);
}

function armPageWatch(wc, recordId, page, sig) {
  disarmPageWatch();
  const st = { wc, recordId, page, labels: new Set((sig && sig.labels) || []),
               url: wc.getURL() || "", started: Date.now(), busy: false, wallSaid: false,
               timer: null, debounce: null, handlers: [] };
  const check = async (reason) => {
    if (st.busy || _pageWatch !== st || !pane || pane.webContents !== wc || wc.isDestroyed()) return;
    if (Date.now() - st.started > 30 * 60 * 1000) { disarmPageWatch(); return; }
    st.busy = true;
    try {
      const s = await mainSignature(wc);
      if (!s) return;
      if (s.success) { disarmPageWatch(); return; }      // the success watch takes it from here
      if (s.blocker === "auth") {
        if (!st.wallSaid) {
          st.wallSaid = true;
          sendProgress({ record_id: recordId || null, page: st.page + 1, wall: "auth", filled: 0,
                         left: [], message: wallMessage(st.page + 1) });
        }
        return;
      }
      const urlNow = wc.getURL() || "";
      const framed = reason === "frame" && s.cross_frames > 0;
      if (!isNewPage(st.labels, s) && !framed) {
        if (urlNow !== st.url && s.labels.length) { st.url = urlNow; }
        return;
      }
      if (!framed && !s.empty) { st.labels = new Set(s.labels); return; }   // nothing to fill
      const next = st.page + 1;
      const res = await runAutoApplyOnPane(recordId, { page: next, fromWatch: true });
      const after = await mainSignature(wc);
      if (framed && !(res && res.ok)) return;            // an ad or widget frame reloading
      st.page = next;
      st.labels = new Set((after && after.labels) || s.labels);
      st.url = wc.getURL() || urlNow;
      st.wallSaid = false;
    } finally {
      st.busy = false;
    }
  };
  const soon = (reason) => () => {
    clearTimeout(st.debounce);
    st.debounce = setTimeout(() => check(reason), 1200);     // let the new page render first
  };
  for (const [ev, reason] of [["did-navigate", "nav"], ["did-navigate-in-page", "nav"],
                              ["did-finish-load", "nav"], ["did-frame-navigate", "frame"]]) {
    const h = ev === "did-frame-navigate"
      ? (_e, _url, _code, _status, isMainFrame) => { if (!isMainFrame) soon(reason)(); }
      : soon(reason);
    wc.on(ev, h);
    st.handlers.push([ev, h]);
  }
  st.timer = setInterval(() => check("poll"), 1500);         // single-page apps change no URL
  _pageWatch = st;
}
function disarmPageWatch() {
  const st = _pageWatch;
  if (!st) return;
  _pageWatch = null;
  clearInterval(st.timer);
  clearTimeout(st.debounce);
  try { for (const [ev, h] of st.handlers) st.wc.removeListener(ev, h); } catch (e) { /* pane gone */ }
}

// ------------------------------------------------------------- the ONE apply path
// Every "Fill application" / "Apply" / "Open application" button in the app comes here, so the
// behaviour is the same everywhere: open the application in the in-app pane, run the best fill
// (site recipe, then the AI plan, then the resume upload), then ask the ENGINE whether this site
// may be submitted by SponsorJobs (submit.policy.apply_decision). Only a site on the verified
// AUTO allowlist is ever clicked; everywhere else the form is filled and left for the person,
// with the site's submit button scrolled into view and outlined (CLAUDE.md section 7). LinkedIn
// and the other prohibited boards are never filled. A captcha or sign-in wall always stops it.
async function applyDecision(url, pageUrl) {
  try {
    const q = "?url=" + encodeURIComponent(url || "") + "&page_url=" + encodeURIComponent(pageUrl || "");
    const resp = await net.fetch(SERVER + "/api/submit/decision" + q);
    if (!resp.ok) return { fill: false, click_submit: false };
    const d = await resp.json();
    return { fill: d.fill === true, click_submit: d.click_submit === true };
  } catch (e) {
    return { fill: false, click_submit: false };   // engine unreachable: never submit, never fill
  }
}

// opts: { url, recordId, resumePath, submit, here }
//   here   -> fill the page already open in the pane (after the person pressed Next / signed in)
//   submit -> the caller is an Apply button; the engine still has the final say per site
ipcMain.handle("assist:apply", async (_e, opts) => {
  const { url, recordId, resumePath, submit, here } = opts || {};
  let startUrl = url || "";
  if (here) {
    if (!pane) return { ok: false, reason: "no-page" };
    startUrl = pane.webContents.getURL() || "";
  }
  if (!/^https?:/i.test(startUrl)) return { ok: false, reason: here ? "no-page" : "bad-url" };
  try {
    if (/(^|\.)linkedin\.com$/i.test(new URL(startUrl).hostname)) {
      if (!here) openInPane(startUrl);           // they can still browse and apply by hand
      return { ok: false, reason: "linkedin" };
    }
  } catch (e) { return { ok: false, reason: "bad-url" }; }

  const before = await applyDecision(startUrl, "");
  if (!before.fill) {
    if (!here) openInPane(startUrl);
    return { ok: false, reason: "fill-by-hand" };
  }

  if (!here) {
    openInPane(startUrl);
    await new Promise((r) => setTimeout(r, 400));   // navigation kickoff
    await waitPaneLoad();
    await new Promise((r) => setTimeout(r, 1200));  // let the ATS form hydrate
  }
  if (!pane) return { ok: false, reason: "no-page" };
  const wc = pane.webContents;

  const report = await runAutoApplyOnPane(recordId);
  const filledOk = !!(report && report.ok !== false);
  if (report && report.reason === "blocked") return report;   // never fight an auth wall
  let attached = !!(report && report.uploaded);
  if (!attached && resumePath) attached = await attachResume(wc, resumePath);
  if (!filledOk && !attached) return report || { ok: false, reason: "inject-failed" };
  await new Promise((r) => setTimeout(r, 500));

  const base = Object.assign({}, report && report.ok !== false ? report : { filled: 0 },
                             { ok: true, resume_attached: attached });
  const wall = (report && report.captcha) ? "captcha" : await wc.executeJavaScript(WALL_JS, true);
  if (wall) return { ...base, submitted: false, reason: wall };

  // Ask again with the page that is open NOW: a redirect to another host is judged on its own.
  const now = await applyDecision(startUrl, wc.getURL() || "");
  const mayClick = !!submit && now.click_submit === true;
  const res = await wc.executeJavaScript(submitScript(mayClick), true);
  if (!mayClick || !res || !res.clicked) {
    return { ...base, submitted: false,
             reason: res && res.covered_by ? "submit-covered"
                   : !mayClick ? "your-click" : "no-submit-button",
             submit_highlighted: !!(res && res.highlighted) || !!base.submit_highlighted,
             covered_by: res && res.covered_by, seen: res && res.seen };
  }
  await new Promise((r) => setTimeout(r, 3500));    // let the POST land
  const confirmed = await wc.executeJavaScript(`(() => {
    const t = (document.body.innerText || '').toLowerCase();
    return /thank you|application (was )?(submitted|received)|we('| ha)?ve received/.test(t);
  })()`, true);
  return { ...base, submitted: true, confirmed: !!confirmed, clicked: res.label };
});


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

// ------------------------------------------------------------- the apply agent
// "Apply for me" (docs/apply-agent.md): the agent drives the pane itself, from the job page to
// the review page, and stops at Submit; Send presses it (auto-submit sites with Autonomous on
// are pressed without asking). One run at a time.
const agent = createAgent({
  getPane: () => pane, openInPane, waitPaneLoad, autofillCore, agentViewSource,
  runAutoApplyOnPane, postApp, getApp, sendProgress, acquireDebugger, frames, applyDecision,
  isNewPage, attachResume,
  sign: (req) => postApp("/api/autoapply/agent/sign", req),
});
ipcMain.handle("agent:run", async (_e, opts) => {
  const o = opts || {};
  return agent.run(o.recordId, { url: o.url, resumePath: o.resumePath, lane: o.lane, autonomous: !!o.autonomous,
                                 company: o.company || "" });
});
ipcMain.handle("agent:send", async (_e, recordId) => agent.send(recordId));
ipcMain.handle("agent:stop", async () => { agent.stop(); return { ok: true }; });
// Several jobs, one after another; a job that needs the person parks and the next one goes on.
ipcMain.handle("agent:batch", async (_e, jobs) => agent.batch((Array.isArray(jobs) ? jobs : []).map((o) => ({
  recordId: o.recordId, url: o.url, resumePath: o.resumePath, lane: o.lane, autonomous: !!o.autonomous, company: o.company || "" }))));
// "Continue" on a job parked at a captcha or a step on the page.
ipcMain.handle("agent:continue", async (_e, recordId) => agent.continueParked(recordId));
ipcMain.handle("agent:busy", async () => ({ busy: agent.busy() }));
// Parked jobs carry on by themselves once answered, also after a restart (the engine keeps them).
agent.startPolling();
ipcMain.handle("agent:ready", async (_e, recordId) => ({ ready: agent.isReady(recordId) }));

// AI-fill whatever page is CURRENTLY open in the pane (after the person navigated / signed in).
ipcMain.handle("autoapply:fill-current", async (_e, recordId) => runAutoApplyOnPane(recordId));

ipcMain.on("pane:set-bounds", (_e, rect) => { paneBounds = rect; layoutPane(); });
ipcMain.on("pane:open", (_e, url) => openInPane(url));
ipcMain.on("pane:close", () => { disarmSuccessWatch(); disarmPageWatch(); closePane(); });
ipcMain.on("pane:back", () => pane && pane.webContents.navigationHistory.goBack());
ipcMain.on("pane:forward", () => pane && pane.webContents.navigationHistory.goForward());
ipcMain.on("pane:reload", () => pane && pane.webContents.reload());
// The download page opens in the person's own browser. Only our releases page is accepted here.
ipcMain.on("update:open", (_e, url) => {
  if (typeof url === "string" && url.startsWith("https://github.com/KofiGilbert/sponsorjobs/releases/")) {
    shell.openExternal(url);
  }
});
// "Help and support" in the account menu: open the person's own mail app, addressed to our
// support inbox. Exactly mailto:support@sponsorjobs.ai (with an optional subject and body) is
// accepted; anything else is dropped (links.js).
ipcMain.on("support:mail", (_e, url) => {
  if (isSupportMailto(url)) shell.openExternal(url);
});
ipcMain.handle("app:version", () => app.getVersion());
ipcMain.on("pane:external", () => {
  const url = pane && pane.webContents.getURL();
  if (url && /^https?:/i.test(url)) shell.openExternal(url);
});
// Typing an address creates the pane if needed. It used to require an existing pane, so the
// first address typed into a freshly opened Browser tab was silently dropped (2026-10-08).
ipcMain.on("pane:navigate", (_e, url) => openInPane(url));

// ---- Saved searches: open the same search on the other boards, in the person's own browser.
// One tab per URL, http(s) only, capped so a bad payload cannot spray windows. These are plain
// search pages the person reads themselves; the shell never loads them in the pane, never
// clicks, never reads them (CLAUDE.md section 7: LinkedIn and Handshake are browse-by-hand).
ipcMain.on("links:external", (_e, urls) => {
  const list = Array.isArray(urls) ? urls : [];
  for (const u of list.slice(0, 10)) {
    if (typeof u === "string" && /^https?:\/\//i.test(u)) shell.openExternal(u);
  }
});

// ---- Local desktop notification (saved-search digest). Click = bring the app forward.
ipcMain.on("notify:show", (_e, msg) => {
  try {
    if (!Notification.isSupported()) return;
    const title = String((msg && msg.title) || "SponsorJobs").slice(0, 120);
    const body = String((msg && msg.body) || "").slice(0, 400);
    const n = new Notification({ title, body, silent: false });
    n.on("click", () => { if (win && !win.isDestroyed()) { if (win.isMinimized()) win.restore(); win.show(); win.focus(); } });
    n.show();
  } catch (e) { /* a notification is best-effort; the badge in the app still shows the count */ }
});
ipcMain.handle("pane:get-state", () => paneState());

// Dev only: start the managed-AI broker (avatar + bundled LLM) so the live interview works on
// launch with no manual setup, and put the dev user on a plan with minutes. In production the
// broker is a HOSTED service. Best-effort: if it fails to start (e.g. no venv python), the app
// still runs and the live interview just falls back / shows service-unavailable.
const BROKER = "http://127.0.0.1:57001";
function isLocalBroker(url) {
  if (!url) return true;                                // unset: the engine defaults to the local one
  try { return ["127.0.0.1", "localhost", "[::1]"].includes(new URL(url).hostname); }
  catch (e) { return true; }
}
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
  // Dev against a remote broker (TAILOR_LIVE_BROKER=1 in dev-start.sh sets TAILOR_BROKER_URL to
  // https://api.sponsorjobs.ai): the engine talks to that one, so there is no local broker to run.
  if (!isLocalBroker(process.env.TAILOR_BROKER_URL)) return;
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

// Tell the person when a newer version is out. The app is not code-signed yet, and macOS only
// lets a signed app replace itself, so this is a notice with a download link, not a silent
// auto-update (switch to electron-updater once the builds are signed). One anonymous request to
// GitHub's public releases API per launch; nothing about the person is sent.
const RELEASES_API = "https://api.github.com/repos/KofiGilbert/sponsorjobs/releases/latest";

function newerVersion(latest, current) {
  const parse = (v) => String(v).replace(/^v/, "").split("-")[0].split(".").map((n) => parseInt(n, 10) || 0);
  const a = parse(latest), b = parse(current);
  for (let i = 0; i < 3; i++) if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) > (b[i] || 0);
  return false;
}

async function checkForUpdate() {
  if (!app.isPackaged) return;
  try {
    const res = await net.fetch(RELEASES_API, { headers: { Accept: "application/vnd.github+json" } });
    if (!res.ok) return;
    const rel = await res.json();
    if (rel.draft || rel.prerelease || !newerVersion(rel.tag_name, app.getVersion())) return;
    const info = { version: String(rel.tag_name).replace(/^v/, ""), url: rel.html_url };
    if (win && !win.isDestroyed()) win.webContents.send("update:available", info);
  } catch (e) { /* offline or rate-limited: try again next launch */ }
}

// sponsorjobs:// links bring the app to the front (Kofi, 2026-10-11). The browser pages that
// finish a sign-in or a payment end with a link to sponsorjobs://..., the way Zoom, Slack and
// VS Code hand you back to their apps: the browser asks once "Open SponsorJobs?" (with an
// "always allow" box), then the app comes forward by itself. A browser tab we did not open by
// script cannot be closed by the page, so the tab stays behind, saying you can close it.
app.setAsDefaultProtocolClient("sponsorjobs");
function bringToFront() {
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  win.show();
  app.focus({ steal: true });
  win.focus();
  win.webContents.send("app:returned");
}
app.on("open-url", (e) => { e.preventDefault(); bringToFront(); });   // macOS
app.on("second-instance", (_e, argv) => {                            // Windows / Linux
  if ((argv || []).some(a => String(a).startsWith("sponsorjobs://"))) bringToFront();
  else bringToFront();
});

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
  setTimeout(checkForUpdate, 5000);   // after the window is up; never delays launch
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
