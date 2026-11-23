// The page's side of keeping the Browser panel and the shell's pane in step (the shell's side is
// shell-electron/pane_view.js). No DOM, so tests/js/test_after_submit.js drives it under node.
//
// 2026-11-19: after a Workday submit the Browser panel stayed open and showed solid black. The
// shell had closed its pane and said so (pane:state {open: false}), but the page only ever
// OPENED the panel on pane:state, never closed it. Now: the shell opening its pane opens the
// panel on the Browser tab; the shell closing a pane that was open closes the panel when it is
// showing the Browser tab. A state that was already closed (the shell's start, a quiet Auto run
// out of sight) changes nothing, so a panel the person opened themselves is left alone.
(function (root) {
  "use strict";

  /** "open" | "close" | "": what the Browser panel does on a pane:state.
   *  wasOpen: the shell's previous state said open. onBrowser: the panel shows the Browser tab now. */
  function panelOnState(wasOpen, st, onBrowser) {
    if (st && st.open) return "open";
    if (wasOpen && onBrowser) return "close";
    return "";
  }

  const api = { panelOnState };
  root.SJPaneSync = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
