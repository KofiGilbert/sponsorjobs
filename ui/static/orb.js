/* The Orb's page (shell-electron/orb_view.js). It draws the ball and its bubble, and reports what
   the person did. It holds no data and decides nothing: the app page sends what to show
   (window.orbShell.onUpdate) and is told about every click (window.orbShell.event).

   The bubble is a copy of an element in the app page (the Browser bar's status line during
   Autofill, or a nudge line), sent as a small tree of plain nodes (tag, class, text, a few safe
   attributes). It is rebuilt here with createElement and textContent only, never as markup, so a
   job site's words can never become code. A click, a typed answer or Enter on the copy is sent
   back with the node's path, and the app page plays it on the original. */
(() => {
  "use strict";
  const S = window.orbShell;
  const $ = (s) => document.querySelector(s);
  const root = $("#orbRoot"), ball = $("#orb"), bubble = $("#orbBubble"), menu = $("#orbMenu"), badge = $("#orbBadge");
  if (!S || !root) return;

  const TAGS = new Set(["span", "div", "button", "ol", "ul", "li", "input", "strong", "em", "b", "i", "p", "small"]);
  const ATTRS = ["title", "aria-label", "aria-pressed", "aria-expanded", "placeholder"];
  let data = { labels: {} };
  let treeKey = "";
  let chimeSeen = 0;
  let talkTimer = null;

  // ---------------------------------------------------------------- the bubble
  function build(node, path) {
    if (!node) return null;
    if (typeof node.t === "string") return document.createTextNode(node.t);
    if (!TAGS.has(node.tag)) return null;
    const el = document.createElement(node.tag);
    if (node.cls) el.className = String(node.cls);
    el.dataset.p = path.join(".");
    for (const a of ATTRS) if (node.a && typeof node.a[a] === "string") el.setAttribute(a, node.a[a]);
    if (node.hidden) el.hidden = true;
    if (node.disabled) el.disabled = true;
    if (node.tag === "button") el.type = "button";
    if (node.tag === "input") { el.type = "text"; el.value = node.value || ""; }
    (node.c || []).forEach((ch, i) => { const k = build(ch, path.concat(i)); if (k) el.appendChild(k); });
    return el;
  }

  function renderBubble(tree) {
    const key = tree ? JSON.stringify(tree) : "";
    if (key === treeKey) return;
    // keep the person's place: a box they are typing in keeps its words, caret and focus
    const act = document.activeElement;
    const keep = act && bubble.contains(act) && act.dataset ? { p: act.dataset.p, v: act.value, s: act.selectionStart, e: act.selectionEnd } : null;
    const before = bubble.textContent;
    treeKey = key;
    bubble.textContent = "";
    if (!tree) { bubble.hidden = true; return; }
    const el = build(tree, []);
    if (el) bubble.appendChild(el);
    bubble.hidden = false;
    if (keep && keep.p) {
      const again = bubble.querySelector(`[data-p="${CSS.escape(keep.p)}"]`);
      if (again) {
        if (again.tagName === "INPUT" && keep.v != null) again.value = keep.v;
        again.focus();
        try { if (again.setSelectionRange && keep.s != null) again.setSelectionRange(keep.s, keep.e); } catch { /* fine */ }
      }
    }
    // new words: the mouth moves while they arrive
    if (bubble.textContent !== before) talk();
  }

  function talk() {
    root.classList.add("is-talking");
    clearTimeout(talkTimer);
    talkTimer = setTimeout(() => root.classList.remove("is-talking"), 1200);
  }

  const pathOf = (el) => (el && el.dataset && el.dataset.p != null ? el.dataset.p.split(".").filter(Boolean).map(Number) : null);
  bubble.addEventListener("click", (e) => {
    const t = e.target.closest("button");
    if (!t || !bubble.contains(t) || t.disabled) return;
    S.event({ kind: "act", op: "click", p: pathOf(t), label: t.textContent || "" });
  });
  bubble.addEventListener("input", (e) => {
    const t = e.target;
    if (t && t.tagName === "INPUT") S.event({ kind: "act", op: "input", p: pathOf(t), value: t.value });
  });
  bubble.addEventListener("keydown", (e) => {
    const t = e.target;
    if (t && t.tagName === "INPUT" && e.key === "Enter") { e.preventDefault(); S.event({ kind: "act", op: "key", key: "Enter", p: pathOf(t) }); }
    if (e.key === "Escape") ball.focus();
  });

  // ---------------------------------------------------------------- sound: a soft two-note chime
  let audio = null;
  function chime() {
    if (data.muted) return;
    try {
      audio = audio || new AudioContext();
      const t0 = audio.currentTime;
      [[880, 0], [1318.5, 0.11]].forEach(([f, at]) => {
        const o = audio.createOscillator(), g = audio.createGain();
        o.type = "sine"; o.frequency.value = f;
        g.gain.setValueAtTime(0, t0 + at);
        g.gain.linearRampToValueAtTime(0.045, t0 + at + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + at + 0.32);
        o.connect(g).connect(audio.destination);
        o.start(t0 + at); o.stop(t0 + at + 0.34);
      });
    } catch { /* no audio: fine */ }
  }

  // ---------------------------------------------------------------- what to show
  function L(k, f) { return (data.labels && data.labels[k]) || f; }
  function apply(d) {
    data = Object.assign({ labels: {} }, d || {});
    document.documentElement.setAttribute("data-theme", data.theme === "light" ? "light" : "dark");
    if (data.lang) document.documentElement.lang = data.lang;
    const place = data.place || {};
    root.dataset.side = place.side === "left" ? "left" : "right";
    root.dataset.vert = place.vert === "down" ? "down" : "up";
    root.dataset.state = data.state || "idle";
    const n = Number(data.count) || 0;
    badge.hidden = !n || !!data.bubble;
    badge.textContent = n > 9 ? "9+" : String(n);
    const name = L("assistant", "Assistant");
    ball.setAttribute("aria-label", n ? name + ", " + L("waiting", "{n} things wait on you").replace("{n}", n) : name);
    ball.title = name;
    $("#orbMute").textContent = data.muted ? L("unmute", "Turn sounds on") : L("mute", "Mute sounds");
    $("#orbOff").textContent = L("off", "Turn off the assistant");
    menu.setAttribute("aria-label", name);
    renderBubble(data.bubble || null);
    if (data.chime && data.chime !== chimeSeen) { chimeSeen = data.chime; chime(); }
    report();
  }
  S.onUpdate(apply);

  // the page's size: the shell keeps the view this big and no bigger
  let sizeKey = "";
  function report() {
    requestAnimationFrame(() => {
      const r = root.getBoundingClientRect();
      const w = Math.ceil(r.width), h = Math.ceil(r.height);
      const key = w + "x" + h;
      if (key === sizeKey) return;
      sizeKey = key;
      S.size(w, h);
    });
  }
  new ResizeObserver(report).observe(root);

  // ---------------------------------------------------------------- the ball: click, drag, keys
  // A press that moves 4 px or more is a drag (the view follows, the spot is kept); less is a
  // click. One click opens the chat; a double click puts the Orb back in its corner.
  let press = null, dragged = false, clickTimer = null, frame = 0;
  ball.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    press = { x: e.screenX, y: e.screenY, id: e.pointerId };
    dragged = false;
    try { ball.setPointerCapture(e.pointerId); } catch { /* fine */ }
  });
  ball.addEventListener("pointermove", (e) => {
    if (!press || e.pointerId !== press.id) return;
    const dx = e.screenX - press.x, dy = e.screenY - press.y;
    if (!dragged && Math.hypot(dx, dy) < 4) return;
    if (!dragged) { dragged = true; closeMenu(); }
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => S.drag(dx, dy));
  });
  const endPress = (e) => {
    if (!press || (e && e.pointerId !== press.id)) return;
    press = null;
    try { ball.releasePointerCapture(e.pointerId); } catch { /* fine */ }
    if (dragged) { cancelAnimationFrame(frame); S.dragEnd(); S.event({ kind: "moved" }); }
  };
  ball.addEventListener("pointerup", endPress);
  ball.addEventListener("pointercancel", endPress);
  ball.addEventListener("click", (e) => {
    if (dragged) { dragged = false; e.preventDefault(); return; }
    if (e.detail === 0) { S.event({ kind: "open", how: "key" }); return; }     // Enter or Space
    if (e.detail >= 2) { clearTimeout(clickTimer); clickTimer = null; S.reset(); S.event({ kind: "reset" }); return; }
    clearTimeout(clickTimer);
    clickTimer = setTimeout(() => { clickTimer = null; S.event({ kind: "open", how: "click" }); }, 240);
  });
  ball.addEventListener("keydown", (e) => {
    if (e.shiftKey && /^Arrow/.test(e.key)) {
      e.preventDefault();
      const step = 10;
      S.move(e.key === "ArrowLeft" ? -step : e.key === "ArrowRight" ? step : 0,
             e.key === "ArrowUp" ? -step : e.key === "ArrowDown" ? step : 0);
      return;
    }
    if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) { e.preventDefault(); openMenu(); }
    if (e.key === "Escape") closeMenu();
  });
  ball.addEventListener("contextmenu", (e) => { e.preventDefault(); menu.hidden ? openMenu() : closeMenu(); });

  function openMenu() {
    menu.hidden = false;
    report();
    $("#orbMute").focus();
  }
  function closeMenu() { if (!menu.hidden) { menu.hidden = true; report(); } }
  menu.addEventListener("keydown", (e) => {
    const items = [...menu.querySelectorAll(".orb-mi")];
    const i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { closeMenu(); ball.focus(); }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus(); }
  });
  $("#orbMute").addEventListener("click", () => { closeMenu(); S.event({ kind: "menu", id: data.muted ? "unmute" : "mute" }); });
  $("#orbOff").addEventListener("click", () => { closeMenu(); S.event({ kind: "menu", id: "off" }); });
  window.addEventListener("blur", closeMenu);

  S.event({ kind: "ready" });
})();
