/* The Orb's own view (2026-11-26, owner approved): SponsorJobs' floating assistant.
 *
 * The in-app browser is a NATIVE view laid over the app page, so nothing the page draws can sit
 * above it. The Orb is therefore a small view of its own, transparent, added AFTER the pane (and
 * raised again whenever the pane is created), with tight bounds: the ball alone, growing only
 * while a bubble shows. Its page is the engine's /orb; it holds no data and is told what to show
 * by the app page (orb:set), and its clicks go back to the app page (orb:event).
 *
 * Where it sits: bottom right of its anchor, which the app page reports: the Browser pane while
 * that is showing (Autofill happens there), else the whole window. A quiet run's pane is out of
 * sight, so the page reports the window then. The person may drag it (a 4 px threshold tells a
 * click from a drag), the spot is kept per anchor, and a double click puts it back.
 *
 * orbRect() is pure so tests can pin it (tests/test_orb_view.py). */
"use strict";

const BOX = 72;              // the ball's box in page pixels: 56 px ball plus its glow
const MARGIN = 16;           // default gap from the anchor's bottom right corner

function num(v, d) { const n = Number(v); return Number.isFinite(n) ? n : d; }

// anchor: {x, y, width, height} in window points. offset: {r, b} points from the anchor's right
// and bottom edges to the ball's box. content: {w, h} the page's size in page pixels (ball and
// bubble). win: {width, height} the window's content size in points. zoom: the app's zoom.
// Returns the view's rect in points, where the bubble opens (side, vert) and the clamped offset.
function orbRect({ anchor, offset, content, win, zoom }) {
  const z = num(zoom, 1) > 0 ? num(zoom, 1) : 1;
  const W = Math.max(0, num(win && win.width, 0)), H = Math.max(0, num(win && win.height, 0));
  const a = anchor && num(anchor.width, 0) > 0 && num(anchor.height, 0) > 0
    ? { x: num(anchor.x, 0), y: num(anchor.y, 0), width: num(anchor.width, 0), height: num(anchor.height, 0) }
    : { x: 0, y: 0, width: W, height: H };
  const s = BOX * z;
  const def = MARGIN * z;
  const r0 = offset && Number.isFinite(Number(offset.r)) ? Number(offset.r) : def;
  const b0 = offset && Number.isFinite(Number(offset.b)) ? Number(offset.b) : def;
  // the ball's box, kept inside the window
  const right = Math.min(Math.max(a.x + a.width - r0, s), Math.max(s, W));
  const bottom = Math.min(Math.max(a.y + a.height - b0, s), Math.max(s, H));
  const side = right - s / 2 > W / 2 ? "right" : "left";     // right: the bubble opens to the left
  const vert = bottom - s / 2 > H / 2 ? "up" : "down";       // up: the bubble opens above
  const cw = Math.max(s, num(content && content.w, BOX) * z);
  const ch = Math.max(s, num(content && content.h, BOX) * z);
  let x = side === "right" ? right - cw : right - s;
  let y = vert === "up" ? bottom - ch : bottom - s;
  x = Math.max(0, Math.min(x, Math.max(0, W - cw)));
  y = Math.max(0, Math.min(y, Math.max(0, H - ch)));
  const rect = { x: Math.round(x), y: Math.round(y), width: Math.round(Math.min(cw, W || cw)), height: Math.round(Math.min(ch, H || ch)) };
  return { rect, side, vert, offset: { r: a.x + a.width - right, b: a.y + a.height - bottom } };
}

// The view itself. deps: { win, WebContentsView, server, preload, readPrefs, writePrefs }.
function createOrb(deps) {
  const { win, WebContentsView, server, preload, readPrefs, writePrefs } = deps;
  let view = null;
  let ready = false;
  let state = { visible: false, anchor: null, kind: "window" };
  let last = null;                    // the app's last payload, replayed when the page (re)loads
  let content = { w: BOX, h: BOX };
  let place = { side: "right", vert: "up" };
  let drag = null;                    // { kind, r, b } at the drag's start

  const zoom = () => { try { return win.webContents.getZoomFactor(); } catch (e) { return 1; } };
  const offsets = () => (readPrefs().orb || {});
  const offsetFor = (kind) => offsets()[kind] || null;
  function saveOffset(kind, off) {
    const p = readPrefs();
    p.orb = Object.assign({}, p.orb || {}, { [kind]: off ? { r: Math.round(off.r), b: Math.round(off.b) } : undefined });
    if (!off) delete p.orb[kind];
    writePrefs(p);
  }

  function ensure() {
    if (view) return view;
    view = new WebContentsView({
      webPreferences: { preload, contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: false },
    });
    view.setBackgroundColor("#00000000");
    view.setVisible(false);
    const wc = view.webContents;
    // The Orb's page never leaves /orb and never opens anything.
    wc.setWindowOpenHandler(() => ({ action: "deny" }));
    wc.on("will-navigate", (e) => e.preventDefault());
    wc.on("did-finish-load", () => { ready = true; send(); layout(); });
    wc.on("render-process-gone", () => { ready = false; setTimeout(() => { try { if (view) wc.reload(); } catch (e) { /* gone */ } }, 1500); });
    win.contentView.addChildView(view);
    wc.setZoomFactor(zoom());
    wc.loadURL(server + "/orb");
    return view;
  }

  function send() {
    if (!view || !ready || !last) return;
    try { view.webContents.send("orb:update", Object.assign({}, last, { place })); } catch (e) { /* gone */ }
  }

  function layout() {
    if (!view) return;
    if (!state.visible) { view.setVisible(false); return; }
    let size = { width: 0, height: 0 };
    try { const b = win.getContentBounds(); size = { width: b.width, height: b.height }; } catch (e) { return; }
    const z = zoom();
    try { if (view.webContents.getZoomFactor() !== z) view.webContents.setZoomFactor(z); } catch (e) { /* gone */ }
    const anchor = state.anchor ? { x: state.anchor.x * z, y: state.anchor.y * z, width: state.anchor.width * z, height: state.anchor.height * z } : null;
    const off = drag ? drag.now : offsetFor(state.kind);
    const out = orbRect({ anchor, offset: off, content, win: size, zoom: z });
    if (drag) drag.clamped = out.offset;
    view.setBounds(out.rect);
    view.setVisible(ready);
    if (out.side !== place.side || out.vert !== place.vert) { place = { side: out.side, vert: out.vert }; send(); }
  }

  return {
    // From the app page: { visible, anchor: {x,y,width,height} in page pixels, kind, ...what to show }.
    set(payload) {
      const p = payload || {};
      state = { visible: !!p.visible, anchor: p.anchor || null, kind: p.kind === "pane" ? "pane" : "window" };
      last = Object.assign({}, p);
      delete last.anchor;
      if (state.visible) ensure();
      send();
      layout();
    },
    // From the Orb's page: its natural size in page pixels.
    size(s) {
      const w = Math.max(BOX, Math.min(num(s && s.w, BOX), 520)), h = Math.max(BOX, Math.min(num(s && s.h, BOX), 640));
      if (w === content.w && h === content.h) return;
      content = { w, h };
      layout();
    },
    // A drag in screen points since it began. Saved when it ends.
    drag(dx, dy) {
      const z = zoom();
      if (!drag) {
        const cur = offsetFor(state.kind) || { r: MARGIN * z, b: MARGIN * z };
        drag = { kind: state.kind, start: cur, now: cur };
      }
      drag.now = { r: drag.start.r - num(dx, 0), b: drag.start.b - num(dy, 0) };
      layout();
    },
    dragEnd() {
      if (!drag) return;
      const d = drag;
      drag = null;
      saveOffset(d.kind, d.clamped || d.now);
      layout();
    },
    // Shift and the arrow keys: 10 points a press.
    move(dx, dy) {
      const z = zoom();
      const cur = offsetFor(state.kind) || { r: MARGIN * z, b: MARGIN * z };
      drag = { kind: state.kind, start: cur, now: { r: cur.r - num(dx, 0), b: cur.b - num(dy, 0) } };
      layout();
      this.dragEnd();
    },
    reset() { drag = null; saveOffset(state.kind, null); layout(); },
    // The pane was just created above us: come back on top.
    raise() { if (view) { try { win.contentView.addChildView(view); } catch (e) { /* window gone */ } } },
    layout,
    isOrb(wc) { return !!(view && wc === view.webContents); },
    destroy() {
      if (!view) return;
      try { win.contentView.removeChildView(view); } catch (e) { /* gone */ }
      try { view.webContents.close(); } catch (e) { /* gone */ }
      view = null; ready = false;
    },
  };
}

module.exports = { orbRect, createOrb, BOX, MARGIN };
