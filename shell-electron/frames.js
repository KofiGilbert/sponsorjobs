// Run the fill engine (extension/autofill_core.js) inside CROSS-ORIGIN frames of the in-app pane.
//
// Why: some application systems (iCIMS above all) draw the whole form inside an iframe served
// from another origin. The engine running in the top page can reach same-origin frames and open
// shadow roots by itself, but the browser forbids it from touching a cross-origin frame. The
// shell, which owns the pane, can: through the Chrome DevTools Protocol it creates an ISOLATED
// WORLD in that frame (the same mechanism a browser extension's content script uses: our own
// JavaScript globals, the page's DOM) and runs the very same engine there. A frame that lives in
// its own process (site isolation) is reached through its own CDP target session.
//
// This only FILLS, exactly as in the top page: it never clicks Next or Submit, never touches a
// captcha frame, and the caller only enters frames whose host the engine's policy allows filling
// (submit.policy.fill_permitted), so a LinkedIn or other prohibited frame is never filled.
//
// `cdp` is a small adapter so the same code runs on Electron's webContents.debugger and, in the
// tests, on a raw DevTools websocket:
//   { send(method, params, sessionId) -> Promise<result>, on(fn(method, params, sessionId)), off(fn) }
"use strict";

const WORLD = "sponsorjobs-fill";
const CAPTCHA_RE = /recaptcha|hcaptcha|captcha|turnstile|arkoselabs|funcaptcha|challenges\.cloudflare/i;

function originOf(url) {
  try { return new URL(url).origin; } catch (e) { return ""; }
}

// A stable per-frame prefix for field refs, so a plan's ops route back to the right frame.
function prefixFor(frameId) {
  return "f" + String(frameId || "").replace(/[^A-Za-z0-9]/g, "").slice(-8) + "-";
}

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

// Every frame whose origin differs from its parent's (the frames the parent's engine cannot
// enter), each with an isolated-world execution context to evaluate in.
async function crossOriginFrames(cdp) {
  const out = [];
  const seen = new Set();
  const iframeSessions = [];
  const listener = (method, params) => {
    if (method === "Target.attachedToTarget" && params && params.targetInfo
        && params.targetInfo.type === "iframe") iframeSessions.push(params.sessionId);
  };
  cdp.on(listener);
  try {
    try {
      await cdp.send("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: false, flatten: true });
      await delay(150);                       // attachedToTarget events for existing OOPIFs
    } catch (e) { /* no target domain: in-process frames only */ }

    const walk = async (sessionId, node, parentOrigin) => {
      const f = node && node.frame;
      if (!f) return;
      const origin = f.securityOrigin && f.securityOrigin !== "://" ? f.securityOrigin : originOf(f.url);
      const cross = parentOrigin !== null && origin !== parentOrigin;
      if (cross && /^https?:/i.test(f.url || "") && !CAPTCHA_RE.test(f.url) && !seen.has(f.id)) {
        try {
          const r = await cdp.send("Page.createIsolatedWorld", { frameId: f.id, worldName: WORLD }, sessionId);
          if (r && r.executionContextId) {
            seen.add(f.id);
            out.push({ frameId: f.id, url: f.url, sessionId, contextId: r.executionContextId,
                       prefix: prefixFor(f.id) });
          }
        } catch (e) { /* out of process: reached through its own session below */ }
      }
      for (const c of node.childFrames || []) await walk(sessionId, c, origin);
    };

    const top = await cdp.send("Page.getFrameTree", {});
    await walk(undefined, top && top.frameTree, null);
    for (const sid of iframeSessions) {
      try {
        const t = await cdp.send("Page.getFrameTree", {}, sid);
        // An out-of-process frame is cross-site to its parent by construction.
        await walk(sid, t && t.frameTree, "\u0000parent");
      } catch (e) { /* the frame went away */ }
    }
  } finally {
    cdp.off(listener);
  }
  return out;
}

async function evalIn(cdp, ctx, expression) {
  const r = await cdp.send("Runtime.evaluate", {
    expression, contextId: ctx.contextId, awaitPromise: true, returnByValue: true,
  }, ctx.sessionId);
  if (r && r.exceptionDetails) {
    throw new Error((r.exceptionDetails.exception && r.exceptionDetails.exception.description)
                    || r.exceptionDetails.text || "evaluate failed");
  }
  return r && r.result ? r.result.value : undefined;
}

// Load the engine into the frame's isolated world (once) and give its refs the frame's prefix.
async function injectEngine(cdp, ctx, coreSource) {
  const has = await evalIn(cdp, ctx, "typeof window.TailorAutofill === 'object'");
  if (!has) await evalIn(cdp, ctx, coreSource + "\n;0");
  await evalIn(cdp, ctx, "window.TailorAutofill.setRefPrefix(" + JSON.stringify(ctx.prefix) + ")");
}

// Attach a file to file inputs (by ref) inside a frame. Script cannot set a file input; CDP can.
async function uploadInFrame(cdp, ctx, refs, filePath) {
  let done = 0;
  for (const ref of refs || []) {
    try {
      const sel = JSON.stringify('[data-tailor-ref="' + ref + '"]');
      const r = await cdp.send("Runtime.evaluate", {
        expression: "window.TailorAutofill && window.TailorAutofill.applyOps ? "
                  + "(document.querySelector(" + sel + ") || null) : null",
        contextId: ctx.contextId,
      }, ctx.sessionId);
      const objectId = r && r.result && r.result.objectId;
      if (!objectId) continue;
      await cdp.send("DOM.setFileInputFiles", { files: [filePath], objectId }, ctx.sessionId);
      done++;
    } catch (e) { /* leave this one for the person */ }
  }
  return done;
}

// Which frame a plan op belongs to: frame refs carry the frame's prefix, top-page refs do not.
function routeOps(ops, ctxs) {
  const main = [];
  const byFrame = new Map();
  for (const op of ops || []) {
    const ctx = (ctxs || []).find((c) => String(op.ref || "").startsWith(c.prefix));
    if (!ctx) { main.push(op); continue; }
    if (!byFrame.has(ctx)) byFrame.set(ctx, []);
    byFrame.get(ctx).push(op);
  }
  return { main, byFrame };
}

// Electron's webContents.debugger as the adapter above.
function electronCdp(dbg) {
  const fns = new Set();
  const relay = (_e, method, params, sessionId) => { for (const fn of fns) fn(method, params, sessionId); };
  dbg.on("message", relay);
  return {
    send: (method, params, sessionId) => (sessionId ? dbg.sendCommand(method, params || {}, sessionId)
                                                    : dbg.sendCommand(method, params || {})),
    on: (fn) => fns.add(fn),
    off: (fn) => fns.delete(fn),
    dispose: () => { try { dbg.removeListener("message", relay); } catch (e) { /* gone */ } },
  };
}

module.exports = { WORLD, crossOriginFrames, evalIn, injectEngine, uploadInFrame, routeOps,
                   prefixFor, electronCdp, originOf };
