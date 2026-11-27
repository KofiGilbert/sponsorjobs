// Where a window the site opens goes (2026-11-26, audit case 9: Dropbox, Google Drive, "Apply with").
//
// Before, every site window was loaded INTO the Browser pane ("nothing opens a window"), so a
// person who pressed "Attach from Dropbox" on a half-filled form lost the form to the chooser.
// Now:
//   external  account and payment pages: the person's own browser, as before;
//   deny      the app itself is driving the page (a fill, the agent): a window its own click
//             opened is never followed; the app never uses another service to fill a form;
//   child     the pane holds an application the person is filling: the site's window opens as a
//             small window of its own above the app, so the form stays as it is, and the site's
//             chooser can still hand the file back to the page that opened it;
//   pane      anything else: loaded in the pane, as before.
//
// Pure (no Electron): tests/js/test_popup_policy.js runs it under node.
"use strict";

function decide(o) {
  o = o || {};
  if (!o.valid) return "deny";
  if (o.ownBrowser) return "external";
  if (o.appDriving) return "deny";
  if (o.formHeld) return "child";
  return "pane";
}

// The child window's options: small, above the app, no node, sandboxed. Same session as the pane
// (Electron gives a popup its opener's), so a site's chooser and the form share their sign-in.
function childOptions(parent) {
  return {
    parent: parent || undefined, modal: false, width: 560, height: 680, minWidth: 360, minHeight: 420,
    autoHideMenuBar: true, minimizable: false, fullscreenable: false, show: true,
    backgroundColor: "#ffffff",
    webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false },
  };
}

module.exports = { decide, childOptions };
