// An upload through the page's own file chooser (2026-11-19, iCIMS: "Please upload your resume/CV
// (Max size: 5 MB)" was a styled button with no file box of its own until it was pressed, so the
// run ended as "no saved answer").
//
// The way a person does it: press the upload button, and the browser opens its file chooser.
// With the DevTools protocol the chooser is intercepted instead of shown
// (Page.setInterceptFileChooserDialog), the press is a real input event at the button's centre,
// and the file box the chooser was opened for gets the application's own resume
// (DOM.setFileInputFiles on Page.fileChooserOpened's backendNodeId). Only the file the run was
// given; nothing else is ever chosen, and the interception is switched off again at once.
//
// ``cdp``: { send(method, params) -> promise, on(fn(method, params)) -> off() }, so the Electron
// debugger (shell-electron/main.js) and a test's Playwright CDP session both drive it.
// ``pt``: { x, y } in CSS pixels of the viewport. Returns true when the file was set.
"use strict";

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

async function uploadViaChooser(cdp, pt, file, waitMs) {
  if (!cdp || !pt || !file || !Number.isFinite(pt.x) || !Number.isFinite(pt.y)) return false;
  let off = null;
  let done = null;
  const opened = new Promise((resolve) => {
    done = resolve;
    off = cdp.on((method, params) => { if (method === "Page.fileChooserOpened") resolve(params || {}); });
  });
  try {
    await cdp.send("Page.enable", {});
    await cdp.send("DOM.enable", {});
    await cdp.send("Page.setInterceptFileChooserDialog", { enabled: true });
    const at = { x: pt.x, y: pt.y, button: "left", clickCount: 1 };
    await cdp.send("Input.dispatchMouseEvent", Object.assign({ type: "mouseMoved" }, at, { button: "none", clickCount: 0 }));
    await cdp.send("Input.dispatchMouseEvent", Object.assign({ type: "mousePressed" }, at));
    await cdp.send("Input.dispatchMouseEvent", Object.assign({ type: "mouseReleased" }, at));
    const got = await Promise.race([opened, delay(waitMs || 4000).then(() => null)]);
    if (!got || !got.backendNodeId) return false;
    await cdp.send("DOM.setFileInputFiles", { files: [file], backendNodeId: got.backendNodeId });
    return true;
  } catch (e) {
    return false;
  } finally {
    if (done) done(null);
    if (off) { try { off(); } catch (e) { /* gone */ } }
    try { await cdp.send("Page.setInterceptFileChooserDialog", { enabled: false }); } catch (e) { /* gone */ }
  }
}

module.exports = { uploadViaChooser };
