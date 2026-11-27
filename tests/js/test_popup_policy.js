// shell-electron/popup_policy.js under node (audit case 9): a site window never replaces a
// half-filled form, and a window the app's own click opened is never followed.
// tests/test_job_identity.py runs it.
"use strict";
const path = require("path");
const P = require(path.join(__dirname, "..", "..", "shell-electron", "popup_policy.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
assert(P.decide({ valid: false }) === "deny", "not a web address: never opened");
assert(P.decide({ valid: true, ownBrowser: true, formHeld: true }) === "external", "account pages: the person's own browser");
assert(P.decide({ valid: true, appDriving: true, formHeld: true }) === "deny", "the app's own click never opens another service");
assert(P.decide({ valid: true, formHeld: true }) === "child", "Dropbox over a filled form: its own window, the form stays");
assert(P.decide({ valid: true }) === "pane", "no application in the pane: the pane, as before");
const c = P.childOptions({ id: 1 });
assert(c.parent && c.parent.id === 1 && c.webPreferences.sandbox && !c.webPreferences.nodeIntegration && c.webPreferences.contextIsolation,
       "the child window is the app's, sandboxed, no node");
console.log("popup policy ok");
