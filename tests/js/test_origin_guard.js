// The desktop shell's origin checks (shell-electron/origin_guard.js), under node
// (tests/test_shell_origin_guard.py runs it).
"use strict";
const path = require("path");
const G = require(path.join(__dirname, "..", "..", "shell-electron", "origin_guard.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const SERVER = "http://127.0.0.1:57000";

// ---- sameOrigin: the parsed origin, never a string prefix -------------------------------------
assert(G.sameOrigin(SERVER + "/", SERVER), "the app page");
assert(G.sameOrigin(SERVER + "/api/open?u=x#y", SERVER), "a path, query and fragment on the app");
for (const bad of [
  "http://127.0.0.1:57000@example.com/",          // user info: the real host is example.com
  "http://127.0.0.1:57000.example.com/",
  "http://127.0.0.1:570001/",
  "http://127.0.0.1:57010/",                      // another port
  "https://127.0.0.1:57000/",                     // another scheme
  "http://localhost:57000/",                      // another host name
  "http://127.0.0.1/",
  "javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "", null, undefined, 42,
]) assert(!G.sameOrigin(bad, SERVER), "not the app: " + String(bad));
assert(G.originOf("https://example.com/a") === "https://example.com", "an https origin");
assert(G.originOf("mailto:a@b.c") === null && G.originOf("chrome://settings") === null, "only http(s) has an origin here");

// ---- isFromApp: the sender's frame shows the app ------------------------------------------------
const ev = (url) => ({ senderFrame: { url } });
assert(G.isFromApp(ev(SERVER + "/"), SERVER), "the app window");
assert(!G.isFromApp(ev("https://example.com/"), SERVER), "another site");
assert(!G.isFromApp(ev("http://127.0.0.1:57000@example.com/"), SERVER), "the user-info trick");
assert(!G.isFromApp({ senderFrame: null }, SERVER), "a frame that is gone");
assert(!G.isFromApp({}, SERVER) && !G.isFromApp(null, SERVER), "no frame at all");
const gone = { senderFrame: { get url() { throw new Error("destroyed"); } } };
assert(!G.isFromApp(gone, SERVER), "a frame that throws");

// ---- guardIpc: every handler checks the sender ------------------------------------------------
(async () => {
  const handlers = {}, listeners = {};
  const fakeIpc = {
    handle(ch, fn) { handlers[ch] = fn; },
    on(ch, fn) { listeners[ch] = fn; },
  };
  const refused = [];
  G.guardIpc(fakeIpc, SERVER, (ch) => refused.push(ch));
  let ran = 0;
  fakeIpc.handle("x:get", async (_e, a) => { ran++; return a * 2; });
  fakeIpc.on("x:do", (_e, a) => { ran += a; });
  assert((await handlers["x:get"](ev(SERVER + "/"), 21)) === 42, "the app is answered");
  let threw = false;
  try { await handlers["x:get"](ev("https://example.com/"), 1); } catch (e) { threw = true; }
  assert(threw, "another page's invoke is refused");
  listeners["x:do"](ev(SERVER + "/"), 10);
  listeners["x:do"](ev("http://127.0.0.1:57000@example.com/"), 100);
  assert(ran === 11, "only the app's calls ran: " + ran);
  assert(refused.join(",") === "x:get,x:do", "refusals are reported: " + refused.join(","));

  // ---- debugPort: none in a release build unless asked for -------------------------------------
  assert(G.debugPort({ isPackaged: true, env: {} }) === null, "packaged: no debugging port");
  assert(G.debugPort({ isPackaged: true, env: { TAILOR_CDP_PORT: "" } }) === null, "packaged, empty variable: none");
  assert(G.debugPort({ isPackaged: true, env: { TAILOR_CDP_PORT: "abc" } }) === null, "packaged, junk: none");
  assert(G.debugPort({ isPackaged: true, env: { TAILOR_CDP_PORT: "0" } }) === null, "packaged, zero: none");
  assert(G.debugPort({ isPackaged: true, env: { TAILOR_CDP_PORT: "9240" } }) === "9240", "packaged, asked for: that port");
  assert(G.debugPort({ isPackaged: false, env: {} }) === "9223", "dev: the default port");
  assert(G.debugPort({ isPackaged: false, env: { TAILOR_CDP_PORT: "9233" } }) === "9233", "dev: its own port");

  // ---- the session cookie ---------------------------------------------------------------------
  const c = G.sessionCookie(SERVER, 57000, "abc");
  assert(c.url === SERVER && c.name === "sj_session_57000" && c.value === "abc", "cookie target " + JSON.stringify(c));
  assert(c.httpOnly === true && c.sameSite === "strict" && !("expirationDate" in c), "HttpOnly, Strict, ends with the session");
  console.log("origin_guard ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
