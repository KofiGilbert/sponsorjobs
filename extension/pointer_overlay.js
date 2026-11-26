// The visible pointer (2026-11-26): while Autofill or the agent works in the in-app browser pane, a
// drawn pointer moves to each field it fills or clicks, with a short label beside it ("Filling:
// First name", "Clicking: Next", "Uploading resume"). It is a picture over the page, never the
// real mouse: pointer-events none, in its own shadow root so the site's styles never reach it,
// placed just OUTSIDE the field (to its right, or under it when there is no room) so the field
// itself stays in view. Moves glide (about 0.3 s) unless the person asked for reduced motion,
// then they jump. Gone with hide(), and by itself after 10 s with nothing new.
//
// window.TailorPointer = { at(el, label), point(rect, label), hide(), state() }
(function () {
  "use strict";
  if (typeof window === "undefined" || window.TailorPointer) return;
  const IDLE_MS = 10000;
  const GAP = 10;                      // pointer tip to the field's edge
  const MIN_STEP_MS = 140;             // a burst of fills is shown one by one, this far apart
  const MAX_QUEUE = 6;                 // past this, the oldest moves are dropped (never lag behind)
  let host = null, root = null, wrap = null, tag = null, idle = null, timer = null;
  const queue = [];
  let lastAt = 0;
  let shown = null;                    // { x, y, label } now on screen

  const reduced = () => {
    try { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); } catch (e) { return false; }
  };
  const CSS = `
    :host { all: initial; }
    .p { position: fixed; left: 0; top: 0; display: flex; align-items: flex-start; gap: 6px;
         transform: translate(-200px, -200px); opacity: 0; will-change: transform;
         transition: transform 300ms cubic-bezier(.2,.7,.2,1), opacity 160ms ease; }
    .p.on { opacity: 1; }
    .p.still { transition: opacity 120ms ease; }
    svg { width: 18px; height: 22px; flex: none; filter: drop-shadow(0 1px 2px rgba(0,0,0,.35)); }
    .t { margin-top: 12px; max-width: 240px; padding: 3px 9px; border-radius: 999px;
         background: #16171a; color: #f4f4f5; border: 1px solid rgba(255,255,255,.14);
         font: 500 12px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
         box-shadow: 0 2px 8px rgba(0,0,0,.25); }
  `;
  function ensure() {
    if (host && host.isConnected) return true;
    const parent = document.body || document.documentElement;
    if (!parent) return false;
    host = document.createElement("div");
    host.setAttribute("data-sj-pointer", "");
    host.setAttribute("aria-hidden", "true");
    host.style.cssText = "position:fixed;left:0;top:0;width:0;height:0;z-index:2147483646;pointer-events:none;";
    root = host.attachShadow({ mode: "open" });
    const st = document.createElement("style");
    st.textContent = CSS;
    wrap = document.createElement("div");
    wrap.className = "p" + (reduced() ? " still" : "");
    wrap.innerHTML = '<svg viewBox="0 0 18 22" aria-hidden="true"><path d="M1.5 1.5 L1.5 17.5 L5.6 13.6 L8.6 20.2 L11.4 19 L8.5 12.5 L14.2 12.5 Z" fill="#16171a" stroke="#ffffff" stroke-width="1.4" stroke-linejoin="round"/></svg>';
    tag = document.createElement("span");
    tag.className = "t";
    wrap.appendChild(tag);
    root.append(st, wrap);
    parent.appendChild(host);
    return true;
  }
  // Where the tip goes for a field's box: right of it, centred on its height; under its left end
  // when the label would not fit on the right. Never inside the box.
  function spot(r) {
    const vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
    const room = vw - r.right - GAP;
    if (room >= 200) return { x: Math.round(r.right + GAP), y: Math.round(Math.max(4, Math.min(vh - 30, r.top + r.height / 2 - 6))), side: "right" };
    return { x: Math.round(Math.max(4, Math.min(vw - 220, r.left))), y: Math.round(Math.min(vh - 30, r.bottom + GAP / 2)), side: "below" };
  }
  function visible(r) {
    const vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
    return r && (r.width > 0 || r.height > 0) && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
  }
  function show(m) {
    if (!ensure()) return;
    wrap.classList.toggle("still", reduced());
    tag.textContent = String(m.label || "").slice(0, 60);
    wrap.style.transform = "translate(" + m.x + "px, " + m.y + "px)";
    wrap.classList.add("on");
    shown = { x: m.x, y: m.y, label: tag.textContent, side: m.side };
    lastAt = Date.now();
    clearTimeout(idle);
    idle = setTimeout(hide, IDLE_MS);
  }
  function drain() {
    timer = null;
    const m = queue.shift();
    if (!m) return;
    show(m);
    if (queue.length) timer = setTimeout(drain, MIN_STEP_MS);
  }
  function point(rect, label) {
    if (!visible(rect)) return false;
    const m = Object.assign(spot(rect), { label });
    queue.push(m);
    while (queue.length > MAX_QUEUE) queue.shift();
    if (!timer) {
      const wait = Math.max(0, MIN_STEP_MS - (Date.now() - lastAt));
      timer = setTimeout(drain, wait);
    }
    return true;
  }
  function at(el, label) {
    try {
      if (!el || !el.getBoundingClientRect) return false;
      let r = el.getBoundingClientRect();
      // a box inside a same-origin frame: add the frame's own place on the page
      let w = el.ownerDocument && el.ownerDocument.defaultView;
      let dx = 0, dy = 0;
      for (let i = 0; w && w !== window && i < 6; i++) {
        const f = w.frameElement; if (!f) return false;
        const fb = f.getBoundingClientRect(); dx += fb.left; dy += fb.top;
        w = f.ownerDocument && f.ownerDocument.defaultView;
      }
      if (dx || dy) r = { left: r.left + dx, right: r.right + dx, top: r.top + dy, bottom: r.bottom + dy, width: r.width, height: r.height };
      return point(r, label);
    } catch (e) { return false; }
  }
  function hide() {
    clearTimeout(idle); clearTimeout(timer); timer = null;
    queue.length = 0;
    shown = null;
    if (host && host.parentNode) host.parentNode.removeChild(host);
    host = root = wrap = tag = null;
    return true;
  }
  const state = () => (shown ? Object.assign({ queued: queue.length, reduced: reduced() }, shown) : null);
  window.TailorPointer = { at, point, hide, state };
})();
