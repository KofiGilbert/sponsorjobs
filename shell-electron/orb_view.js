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
 * It never covers what the person needs to see in the pane (2026-11-27): the pointer and the field
 * it names, the site's Submit and the field that needs them. main.js reads those from the page
 * (ORB_AVOID_JS) and hands them over (setAvoid); the Orb glides to the nearest free spot and back.
 *
 * orbRect() is pure so tests can pin it (tests/test_orb_js.py). */
"use strict";

const BOX = 72;              // the ball's box in page pixels: 56 px ball plus its glow
const MARGIN = 16;           // default gap from the anchor's bottom right corner

function num(v, d) { const n = Number(v); return Number.isFinite(n) ? n : d; }

// anchor: {x, y, width, height} in window points. offset: {r, b} points from the anchor's right
// and bottom edges to the ball's box. content: {w, h} the page's size in page pixels (ball and
// bubble). win: {width, height} the window's content size in points. zoom: the app's zoom.
// Returns the view's rect in points, where the bubble opens (side, vert) and the clamped offset.
function place({ anchor, offset, content, win, zoom }) {
  const z = num(zoom, 1) > 0 ? num(zoom, 1) : 1;
  const W = Math.max(0, num(win && win.width, 0)), H = Math.max(0, num(win && win.height, 0));
  const a = anchorOf(anchor, W, H);
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
  const ball = { x: right - s, y: bottom - s, width: s, height: s };
  return { rect, ball, side, vert, offset: { r: a.x + a.width - right, b: a.y + a.height - bottom } };
}

function anchorOf(anchor, W, H) {
  return anchor && num(anchor.width, 0) > 0 && num(anchor.height, 0) > 0
    ? { x: num(anchor.x, 0), y: num(anchor.y, 0), width: num(anchor.width, 0), height: num(anchor.height, 0) }
    : { x: 0, y: 0, width: W, height: H };
}

const PAD = 8;               // points kept clear around what the Orb must not cover
function hits(rect, avoid) {
  return (avoid || []).some((v) => v && num(v.width, 0) > 0 && num(v.height, 0) > 0
    && rect.x < v.x + v.width + PAD && rect.x + rect.width > v.x - PAD
    && rect.y < v.y + v.height + PAD && rect.y + rect.height > v.y - PAD);
}

// (2026-11-27, QA) What the Orb must never cover: the pointer and the field it names, the site's
// Submit, the field that needs the person (avoid: rects in window points). Where the person put
// it (or the default corner) while that spot is free; otherwise the nearest free spot among the
// anchor's corners and edge middles, ball and bubble together. moved: true when it stepped aside.
function orbRect(args) {
  const base = place(args);
  const avoid = (args && args.avoid) || [];
  if (!avoid.length || !hits(base.rect, avoid)) return Object.assign(base, { moved: false });
  const z = num(args.zoom, 1) > 0 ? num(args.zoom, 1) : 1;
  const W = Math.max(0, num(args.win && args.win.width, 0)), H = Math.max(0, num(args.win && args.win.height, 0));
  const a = anchorOf(args.anchor, W, H);
  const s = BOX * z, m = MARGIN * z;
  const far = { r: a.width - s - m, b: a.height - s - m };
  const mid = { r: (a.width - s) / 2, b: (a.height - s) / 2 };
  const spots = [
    { r: m, b: m }, { r: far.r, b: m }, { r: m, b: far.b }, { r: far.r, b: far.b },
    { r: m, b: mid.b }, { r: mid.r, b: m }, { r: far.r, b: mid.b }, { r: mid.r, b: far.b },
  ];
  const cx = base.ball.x + s / 2, cy = base.ball.y + s / 2;
  let best = null;
  for (const off of spots) {
    const p = place(Object.assign({}, args, { offset: off }));
    if (hits(p.rect, avoid)) continue;
    const d = Math.hypot(p.ball.x + s / 2 - cx, p.ball.y + s / 2 - cy);
    if (!best || d < best.d) best = { d, p };
  }
  if (!best) return Object.assign(base, { moved: false });   // nowhere free: stay where it was put
  return Object.assign(best.p, { moved: true, offset: base.offset });
}

// Read in the Browser pane's page (main.js, every 600 ms while the Orb is docked there): what the
// Orb must not cover, in the page's CSS pixels. The pointer (its arrow and label) and the element
// it names, the current needs-you ring and its Next chip, and the site's Submit.
const ORB_AVOID_JS = `(() => {
  const out = [], vw = innerWidth, vh = innerHeight;
  const add = (r) => {
    if (!r || !(r.width > 0) || !(r.height > 0) || r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) return;
    out.push({ x: r.left, y: r.top, width: r.width, height: r.height });
  };
  try {
    const h = document.querySelector("[data-sj-pointer]"), p = h && h.shadowRoot && h.shadowRoot.querySelector(".p.on");
    if (p) { const a = p.querySelector("svg"), t = p.querySelector(".t"); if (a) add(a.getBoundingClientRect()); if (t) add(t.getBoundingClientRect()); }
    const P = window.TailorPointer, el = P && P.target && P.target();
    if (el) add(el);
  } catch (e) { /* not ours */ }
  try {
    const g = document.querySelector("[data-sj-glow]"), sr = g && g.shadowRoot;
    if (sr) for (const n of sr.querySelectorAll(".r.cur, .chip")) if (!n.hidden && !n.classList.contains("off")) add(n.getBoundingClientRect());
  } catch (e) { /* not ours */ }
  try {
    const SUB = /^(submit( (my |your )?application)?|send( (my )?application)?|review and submit|complete application|apply( now)?)$/i;
    let subs = [...document.querySelectorAll(".tailor-submit-hint")];
    if (!subs.length) subs = [...document.querySelectorAll("button, input[type='submit'], [role='button']")]
      .filter((b) => SUB.test(String(b.innerText || b.value || b.getAttribute("aria-label") || "").replace(/\\s+/g, " ").trim()));
    for (const b of subs.slice(0, 4)) add(b.getBoundingClientRect());
  } catch (e) { /* not ours */ }
  return out.slice(0, 12);
})()`;

// The view itself. deps: { win, WebContentsView, server, preload, readPrefs, writePrefs }.
function createOrb(deps) {
  const { win, WebContentsView, server, preload, readPrefs, writePrefs } = deps;
  const onReady = deps.onReady || (() => {});   // tells the app page the Orb's page is up
  let view = null;
  let ready = false;
  let state = { visible: false, anchor: null, kind: "window" };
  let last = null;                    // the app's last payload, replayed when the page (re)loads
  let content = { w: BOX, h: BOX };
  let place = { side: "right", vert: "up" };
  let drag = null;                    // { kind, r, b } at the drag's start
  let avoid = [];                     // rects in window points the Orb must not cover (setAvoid)
  let shown = null;                   // the bounds set last, and the spot they came from
  let glideTimer = null;

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
      webPreferences: { preload, contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: false,
                        transparent: true },
    });
    view.setBackgroundColor("#00000000");
    view.setVisible(false);
    const wc = view.webContents;
    // The Orb's page never leaves /orb and never opens anything.
    wc.setWindowOpenHandler(() => ({ action: "deny" }));
    wc.on("will-navigate", (e) => e.preventDefault());
    // transparent: set again once the page is up, or the page's first paint brings an opaque ground
    wc.on("dom-ready", () => { try { view.setBackgroundColor("#00000000"); } catch (e) { /* gone */ } });
    wc.on("did-finish-load", () => { ready = true; send(); layout(); onReady(); });
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
    // while the person drags, it goes where their hand goes; else it steps aside when it must
    const out = orbRect({ anchor, offset: off, content, win: size, zoom: z, avoid: drag || state.kind !== "pane" ? [] : avoid });
    if (drag) drag.clamped = out.offset;
    const spot = Math.round(out.ball.x) + "," + Math.round(out.ball.y);
    // a new spot (stepping aside, or back): a short glide; a bubble growing in place: at once
    if (shown && shown.spot !== spot && !drag && shown.visible) glide(shown.rect, out.rect);
    else { clearInterval(glideTimer); glideTimer = null; view.setBounds(out.rect); }
    shown = { rect: out.rect, spot, visible: !!ready };
    view.setVisible(ready);
    if (out.side !== place.side || out.vert !== place.vert) { place = { side: out.side, vert: out.vert }; send(); }
  }

  // About 180 ms in ten steps (setBounds has no animation of its own on a view).
  function glide(from, to) {
    clearInterval(glideTimer);
    let i = 0;
    const N = 10;
    glideTimer = setInterval(() => {
      i += 1;
      const t = i / N, e = 1 - Math.pow(1 - t, 3);
      const r = { x: Math.round(from.x + (to.x - from.x) * e), y: Math.round(from.y + (to.y - from.y) * e),
                  width: Math.round(to.width), height: Math.round(to.height) };
      try { if (view) view.setBounds(i >= N ? to : r); } catch (err) { /* gone */ }
      if (i >= N) { clearInterval(glideTimer); glideTimer = null; }
    }, 18);
  }

  return {
    // From the app page: { visible, anchor: {x,y,width,height} in page pixels, kind, ...what to show }.
    set(payload) {
      const p = payload || {};
      state = { visible: !!p.visible, anchor: p.anchor || null, kind: p.kind === "pane" ? "pane" : "window" };
      last = Object.assign({}, p);
      delete last.anchor;
      if (state.visible) ensure();
      // the app page (re)loaded and says hello: the Orb's page is already up, say so
      if (p.boot && ready) onReady();
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
    // What the Orb must not cover in the Browser pane, in window points (main.js reads the page).
    setAvoid(list) {
      const next = (Array.isArray(list) ? list : []).slice(0, 16).map((r) => ({
        x: Math.round(num(r.x, 0)), y: Math.round(num(r.y, 0)), width: Math.round(num(r.width, 0)), height: Math.round(num(r.height, 0)) }));
      if (JSON.stringify(next) === JSON.stringify(avoid)) return;
      avoid = next;
      layout();
    },
    dockedToPane() { return !!(view && state.visible && state.kind === "pane"); },
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

module.exports = { orbRect, createOrb, BOX, MARGIN, ORB_AVOID_JS };
