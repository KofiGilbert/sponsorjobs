// The visible pointer (2026-11-26): while Autofill or the agent works in the in-app browser pane, a
// drawn pointer moves to each field it fills or clicks, with a short label beside it ("Filling:
// First name", "Clicking: Next", "Uploading resume"). It is a picture over the page, never the
// real mouse: pointer-events none, in its own shadow root so the site's styles never reach it,
// placed just OUTSIDE the field (to its right, or under it when there is no room) so the field
// itself stays in view. Moves glide (about 0.3 s) unless the person asked for reduced motion,
// then they jump. Gone with hide(), and by itself after 10 s with nothing new.
//
// window.TailorPointer = { at(el, label, opts), point(rect, label, opts), hide(), state(), target(), coverOf(el), titleOf(modal), COLOR }
//
// (2026-11-26) One signature colour, ONE token (--sj-pointer): the arrow's fill and the label's
// background. The owner's pick (2026-11-26): violet, one shade deeper than #7C5CFF so the white label
// text passes 4.5:1 (4.58:1; the arrow's edge 4.58:1 on white, 4.09:1 on a dark page). Moves take MOVE_MS; a fill that paces itself to the pointer (the engine's fillAsync
// with the pointer on) passes { now: true } so each field is filled as the pointer arrives.
(function () {
  "use strict";
  if (typeof window === "undefined" || window.TailorPointer) return;
  const COLOR = "#7957FC";               // THE pointer token
  const MOVE_MS = 200;
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
    :host { all: initial; --sj-pointer: ${COLOR}; }
    .p { position: fixed; left: 0; top: 0; display: flex; align-items: flex-start; gap: 6px;
         transform: translate(-200px, -200px); opacity: 0; will-change: transform;
         transition: transform ${MOVE_MS}ms cubic-bezier(.2,.7,.2,1), opacity 160ms ease; }
    .p.on { opacity: 1; }
    .p.still { transition: opacity 120ms ease; }
    svg { width: 34px; height: 42px; flex: none; filter: drop-shadow(0 2px 3px rgba(0,0,0,.35)); }
    .t { margin-top: 24px; max-width: min(460px, calc(100vw - 60px)); padding: 4px 11px; border-radius: 999px;
         background: var(--sj-pointer); color: #ffffff; border: 1px solid rgba(255,255,255,.35);
         font: 600 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
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
    wrap.innerHTML = '<svg viewBox="0 0 18 22" aria-hidden="true"><path d="M1.5 1.5 L1.5 17.5 L5.6 13.6 L8.6 20.2 L11.4 19 L8.5 12.5 L14.2 12.5 Z" fill="var(--sj-pointer)" stroke="#ffffff" stroke-width="1.4" stroke-linejoin="round"/></svg>';
    tag = document.createElement("span");
    tag.className = "t";
    wrap.appendChild(tag);
    root.append(st, wrap);
    parent.appendChild(host);
    return true;
  }
  // Where the tip goes for a field's box: right of it, centred on its height; under its left end
  // when the label would not fit on the right. Never inside the box.
  function spot(r, on) {
    const vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
    if (on) {
      // (2026-11-26, the owner) the tip ON the button (its left centre); the label just past the
      // button's right edge when it fits, else under the button: never over it
      const x = Math.round(Math.max(4, r.left + Math.min(18, r.width / 4)));
      const y = Math.round(Math.max(4, Math.min(vh - 30, r.top + r.height / 2 - 4)));
      const right = vw - r.right - 12 >= 260;
      return { x, y, side: "on", shift: right ? Math.round(r.right - x + 12 - 40) : 0, drop: right ? 0 : Math.round(r.bottom - y + 6 - 24) };
    }
    const room = vw - r.right - GAP;
    if (room >= 330) return { x: Math.round(r.right + GAP), y: Math.round(Math.max(4, Math.min(vh - 30, r.top + r.height / 2 - 6))), side: "right" };
    // no room on the right (a narrow pane): the tip on the field's lower edge, near its left end, so
    // it plainly points at THIS field and not the one below; the label hangs under it
    return { x: Math.round(Math.max(4, Math.min(vw - 330, r.left + 10))), y: Math.round(Math.min(vh - 60, r.bottom - 8)), side: "below" };
  }
  function visible(r) {
    const vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
    return r && (r.width > 0 || r.height > 0) && r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
  }
  // (2026-11-26, Lam: "Filling: Disability" sat on top of the site's Data Privacy Agreement
  // window) A field under a site's modal window is never labelled: the pointer goes to the window
  // itself ("Reading: <its title>"), and a pointer resting on a field goes away when a window opens
  // over it. Covered = an open dialog that does not hold the field and is modal (aria-modal, a
  // modal <dialog>) or lies over the field's box; or, the field in view, another fixed layer drawn
  // at its centre (a site's own overlay without dialog roles).
  const MODAL_SEL = "[role='dialog'], [role='alertdialog'], dialog[open], [aria-modal='true']";
  const ours = (n) => !!(n && n.closest && n.closest("[data-sj-pointer], [data-sj-glow]"));
  function openRect(d) {
    try {
      if (d.getAttribute("aria-hidden") === "true") return null;
      const g = window.getComputedStyle(d);
      if (g.display === "none" || g.visibility === "hidden" || Number(g.opacity) === 0) return null;
      const r = d.getBoundingClientRect(), vh = window.innerHeight || 800;
      return r.width > 40 && r.height > 40 && r.bottom > 0 && r.top < vh ? r : null;
    } catch (e) { return null; }
  }
  const overlaps = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
  function coverOf(el) {
    try {
      if (!el || !el.isConnected || el.ownerDocument !== document || ours(el)) return null;
      const er = el.getBoundingClientRect();
      for (const d of document.querySelectorAll(MODAL_SEL)) {
        if (d.contains(el) || el.contains(d) || ours(d)) continue;
        const r = openRect(d);
        if (!r) continue;
        let modal = d.getAttribute("aria-modal") === "true";
        try { modal = modal || (d.tagName === "DIALOG" && d.matches(":modal")); } catch (e) { /* old engine */ }
        if (modal || ((er.width || er.height) && overlaps(r, er))) return d;
      }
      const vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
      const cx = er.left + er.width / 2, cy = er.top + er.height / 2;
      if (!(er.width || er.height) || cx < 0 || cy < 0 || cx > vw || cy > vh) return null;
      const hit = document.elementFromPoint(cx, cy);
      if (!hit || hit === el || el.contains(hit) || hit.contains(el) || ours(hit)) return null;
      for (let n = hit, i = 0; n && n !== document.body && i < 30; n = n.parentElement, i++) {
        if (n.contains(el)) return null;                  // the field's own wrapper, a label drawn over it
        const g = window.getComputedStyle(n);
        if (g.position === "fixed") {
          const r = n.getBoundingClientRect();
          return r.width * r.height >= 0.12 * vw * vh ? (n.querySelector(MODAL_SEL) || n) : null;
        }
      }
    } catch (e) { /* never in the page's way */ }
    return null;
  }
  // The site's own modal window open now (a privacy notice over the form), or null: an open dialog
  // that is modal (aria-modal, a modal <dialog>) or takes a large part of the view.
  function openModal() {
    try {
      const vw = window.innerWidth || 1200, vh = window.innerHeight || 800;
      for (const d of document.querySelectorAll(MODAL_SEL)) {
        if (ours(d)) continue;
        const r = openRect(d);
        if (!r) continue;
        let modal = d.getAttribute("aria-modal") === "true";
        try { modal = modal || (d.tagName === "DIALOG" && d.matches(":modal")); } catch (e) { /* old engine */ }
        if (modal || r.width * r.height >= 0.2 * vw * vh) return d;
      }
    } catch (e) { /* never in the page's way */ }
    return null;
  }
  function headOf(m) {
    try {
      const id = m.getAttribute("aria-labelledby");
      const byId = id && document.getElementById(id.split(/\s+/)[0]);
      const h = byId || m.querySelector("h1, h2, h3, h4, [role='heading']");
      return h && String(h.innerText || h.textContent || "").trim() ? h : null;
    } catch (e) { return null; }
  }
  function titleOf(m) {
    const h = headOf(m);
    const t = h ? (h.innerText || h.textContent) : (m.getAttribute("aria-label") || "");
    return String(t || "").replace(/\s+/g, " ").trim().slice(0, 48);
  }
  let stay = false;                    // a resting pointer (the end of a guided run) is never idled away
  // (2026-11-26) The pointer stays ON the element its label names: when the page scrolls or
  // resizes, it follows that element, and it goes away when the element leaves the view.
  let cur = null;                      // { el, label }
  function follow() {
    if (!cur || !shown || !wrap) return;
    if (!cur.el || !cur.el.isConnected) { hide(); return; }
    const r = cur.el.getBoundingClientRect();
    if (!visible(r) || (!cur.modal && coverOf(cur.el))) { wrap.classList.remove("on"); return; }
    const m = spot(r, cur.on);
    wrap.classList.add("still");                       // follow at once, no glide behind the page
    wrap.style.transform = "translate(" + m.x + "px, " + m.y + "px)";
    wrap.classList.add("on");
    shown.x = m.x; shown.y = m.y;
    requestAnimationFrame(() => { if (wrap) wrap.classList.toggle("still", reduced()); });
  }
  try { window.addEventListener("scroll", follow, true); window.addEventListener("resize", follow); } catch (e) { /* no window */ }
  // a window the site opens later (no scroll): looked for twice a second while a pointer is out
  try { setInterval(() => { if (cur && shown) follow(); }, 500); } catch (e) { /* no timers */ }
  function show(m) {
    if (!ensure()) return;
    wrap.classList.toggle("still", reduced());
    tag.textContent = String(m.label || "").slice(0, 100);
    // beside the button it points into (see spot), else the usual place
    tag.style.marginLeft = m.shift ? Math.max(0, m.shift) + "px" : "";
    tag.style.marginTop = m.side === "on" ? (m.shift ? "-6px" : Math.max(24, 24 + (m.drop || 0)) + "px") : "";
    wrap.style.transform = "translate(" + m.x + "px, " + m.y + "px)";
    wrap.classList.add("on");
    shown = { x: m.x, y: m.y, label: tag.textContent, side: m.side };
    lastAt = Date.now();
    clearTimeout(idle);
    if (!stay) idle = setTimeout(hide, IDLE_MS);
  }
  function drain() {
    timer = null;
    const m = queue.shift();
    if (!m) return;
    show(m);
    if (queue.length) timer = setTimeout(drain, MIN_STEP_MS);
  }
  function point(rect, label, opts) {
    if (!visible(rect)) return false;
    const m = Object.assign(spot(rect, !!(opts && opts.on)), { label });
    stay = !!(opts && opts.stay);
    if (opts && opts.now) { queue.length = 0; clearTimeout(timer); timer = null; show(m); return true; }
    queue.push(m);
    while (queue.length > MAX_QUEUE) queue.shift();
    if (!timer) {
      const wait = Math.max(0, MIN_STEP_MS - (Date.now() - lastAt));
      timer = setTimeout(drain, wait);
    }
    return true;
  }
  function at(el, label, opts) {
    try {
      if (!el || !el.getBoundingClientRect) return false;
      const cov = coverOf(el);
      if (cov) {                                         // the site's window, never the field under it
        const h = headOf(cov) || cov, t = titleOf(cov);
        const ok = point(h.getBoundingClientRect(), t ? "Reading: " + t : "Reading the window on the page", opts);
        if (ok) cur = { el: h, label: t, modal: true };
        return ok;
      }
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
      const ok = point(r, label, opts);
      if (ok) cur = dx || dy ? null : { el, label, on: !!(opts && opts.on) };      // a frame's element: placed once, not followed
      return ok;
    } catch (e) { return false; }
  }
  function hide() {
    stay = false;
    cur = null;
    clearTimeout(idle); clearTimeout(timer); timer = null;
    queue.length = 0;
    shown = null;
    if (host && host.parentNode) host.parentNode.removeChild(host);
    host = root = wrap = tag = null;
    return true;
  }
  const state = () => (shown ? Object.assign({ queued: queue.length, reduced: reduced() }, shown) : null);
  // (2026-11-27) the box of the element the pointer names now, so the app's Orb never covers it
  const target = () => {
    try { return shown && cur && cur.el && cur.el.isConnected ? cur.el.getBoundingClientRect() : null; } catch (e) { return null; }
  };
  window.TailorPointer = { at, point, hide, state, target, coverOf, titleOf, openModal, headOf, COLOR, MOVE_MS };
})();
