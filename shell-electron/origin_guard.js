// Small checks the desktop shell uses to tell its own app page apart from everything else.
//
// - sameOrigin: compares the parsed origin (scheme, host, port), never a string prefix, so an
//   address such as http://127.0.0.1:57000@example.com/ (whose real host is example.com) is
//   not mistaken for the engine.
// - isFromApp / guardIpc: every IPC message the shell answers must come from a frame showing
//   the app's own origin.
// - debugPort: the remote-debugging port is for development and the apply agent's dev tooling;
//   a packaged build turns it on only when TAILOR_CDP_PORT is set explicitly.
// - sessionCookie: the per-launch session cookie the engine expects (ui/app.py _session_gate).
"use strict";

function originOf(url) {
  if (typeof url !== "string" || !url) return null;
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return u.origin;
  } catch (e) { return null; }
}

function sameOrigin(url, server) {
  const a = originOf(url), b = originOf(server);
  return !!a && !!b && a === b;
}

function isFromApp(event, server) {
  const frame = event && event.senderFrame;
  let url = "";
  try { url = frame ? frame.url : ""; } catch (e) { return false; }   // frame already gone
  return sameOrigin(url, server);
}

// Wrap ipcMain.handle / ipcMain.on so every handler registered after this call first checks
// the sender. A refused invoke rejects; a refused message is dropped.
function guardIpc(ipcMain, server, onRefused) {
  const handle = ipcMain.handle.bind(ipcMain);
  const on = ipcMain.on.bind(ipcMain);
  const refuse = (channel, event) => {
    if (onRefused) { try { onRefused(channel, event); } catch (e) { /* logging only */ } }
  };
  ipcMain.handle = (channel, fn) => handle(channel, (event, ...args) => {
    if (!isFromApp(event, server)) { refuse(channel, event); throw new Error("Refused: not the app window."); }
    return fn(event, ...args);
  });
  ipcMain.on = (channel, fn) => on(channel, (event, ...args) => {
    if (!isFromApp(event, server)) { refuse(channel, event); return undefined; }
    return fn(event, ...args);
  });
  return ipcMain;
}

// The remote-debugging port, or null for none.
function debugPort({ isPackaged, env }) {
  const raw = env && env.TAILOR_CDP_PORT;
  const asked = Number(raw);
  const explicit = raw !== undefined && raw !== "" && Number.isInteger(asked) && asked > 0 && asked < 65536;
  if (explicit) return String(asked);
  if (isPackaged) return null;
  return "9223";
}

function sessionCookieName(port) {
  return "sj_session_" + String(port);
}

// The cookie details for session.cookies.set: HttpOnly (page scripts cannot read it),
// SameSite=Strict (never sent on a request another site starts), no expiry (gone on quit).
function sessionCookie(server, port, token) {
  return { url: server, name: sessionCookieName(port), value: token, httpOnly: true, sameSite: "strict", secure: false };
}

module.exports = { originOf, sameOrigin, isFromApp, guardIpc, debugPort, sessionCookieName, sessionCookie };
