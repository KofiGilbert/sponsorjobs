// Where the browser pane goes and whether the person can see it, so the shell's pane and the
// page's Browser panel always agree. Pure (no Electron): tests/js/test_after_submit.js drives it
// with a fake pane.
//
// Measured 2026-11-19 (a real Workday submit): the Browser panel stayed open but showed solid
// black. The pane's page was alive, but reported document.visibilityState "hidden" at its old
// 633 x 616: the shell had closed the pane (paneOpen false, bounds 0 x 0) while the page still
// showed the panel open. The page's live-interview teardown, run on EVERY sidebar click, sent
// pane:close whether or not an interview had used the pane, and the page ignored the pane:state
// {open: false} that followed. Three rules here keep the two in step:
//   * the pane is laid out at the slot's bounds whenever it is open and the slot has a size;
//   * each time the pane goes from out of sight to visible, onShown() runs (main.js clears any
//     device-size override there, so a visible pane always lays out to its own size);
//   * when the page shows the Browser panel again (its slot goes from no size to a real size)
//     while a live pane is closed, the pane is opened again (reopenOnSlot).
// The page's side (closing the panel when the shell closes the pane) is ui/static/pane_sync.js.
"use strict";

const { paneRect } = require("./panegeom");

const ZERO = Object.freeze({ x: 0, y: 0, width: 0, height: 0 });
// A quiet (Auto) run keeps the pane at a real size, parked past the window's left edge.
const QUIET_BOUNDS = Object.freeze({ x: -6000, y: 0, width: 1280, height: 900 });

const slotShown = (slot) => !!slot && Number(slot.width) > 0 && Number(slot.height) > 0;

// s: {open, quiet, slot, zoom} -> {rect, visible}
function paneLayout(s) {
  s = s || {};
  if (s.quiet && s.open) return { rect: QUIET_BOUNDS, visible: false };
  if (!s.open || !slotShown(s.slot)) return { rect: ZERO, visible: false };
  const rect = paneRect(s.slot, s.zoom);
  return { rect, visible: rect.width > 0 && rect.height > 0 };
}

// The page just showed its Browser panel (the slot went from no size to a real one) and the shell
// still holds a live pane it had closed: show that pane again. Never during a quiet run (its pane
// stays parked) and never for a slot that only changed size.
// s: {before, slot, hasPane, open, quiet}
function reopenOnSlot(s) {
  s = s || {};
  return !!s.hasPane && !s.open && !s.quiet && !slotShown(s.before) && slotShown(s.slot);
}

// d: {setBounds(rect), onShown()} -> {layout(state) -> {rect, visible}, reset()}
// reset(): a new pane (the last one was torn down) starts out of sight.
function createPaneSync(d) {
  let visible = false;
  return {
    layout(state) {
      const out = paneLayout(state);
      d.setBounds(out.rect);
      const was = visible;
      visible = out.visible;
      if (visible && !was && d.onShown) { try { d.onShown(); } catch (e) { /* the pane is gone */ } }
      return out;
    },
    reset() { visible = false; },
    visible: () => visible,
  };
}

module.exports = { paneLayout, reopenOnSlot, createPaneSync, slotShown, QUIET_BOUNDS, ZERO };
