/* The celebration layer (2026-11-27): a short confetti burst over the whole window, the app and
 * the Browser pane alike, when the site takes an application or a card moves to Interviewing or
 * Offer. It is a child window of its own: transparent, frameless, never focused and click-through
 * (setIgnoreMouseEvents), so it never blocks a click and never takes the keyboard. It loads the
 * engine's /confetti page, shows for about 1.5 s and is destroyed. One at a time. */
"use strict";

const SHOW_MS = 1600;

function createConfetti({ BrowserWindow, win, server }) {
  let layer = null;
  let timer = null;
  function close() {
    clearTimeout(timer); timer = null;
    const l = layer; layer = null;
    if (l && !l.isDestroyed()) { try { l.destroy(); } catch (e) { /* gone */ } }
  }
  function burst(size) {
    if (!win || win.isDestroyed() || !win.isVisible() || win.isMinimized()) return false;
    close();
    const b = win.getContentBounds();
    layer = new BrowserWindow({
      parent: win, x: b.x, y: b.y, width: b.width, height: b.height, show: false, frame: false, transparent: true,
      resizable: false, movable: false, focusable: false, skipTaskbar: true, hasShadow: false, fullscreenable: false,
      backgroundColor: "#00000000",
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
    });
    layer.setIgnoreMouseEvents(true);
    const wc = layer.webContents;
    wc.setWindowOpenHandler(() => ({ action: "deny" }));
    wc.on("will-navigate", (e) => e.preventDefault());
    wc.once("did-finish-load", () => { if (layer && !layer.isDestroyed()) layer.showInactive(); });
    wc.loadURL(server + "/confetti#" + (size === "big" ? "big" : "light"));
    timer = setTimeout(close, SHOW_MS + 600);       // the page's own 1.4 s, plus its load
    return true;
  }
  return { burst, close };
}

module.exports = { createConfetti, SHOW_MS };
