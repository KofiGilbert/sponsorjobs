/* The Orb's pure parts, for the app page (app.js, "the Orb") and node tests (tests/test_orb_core.py).
 *
 *  snapshot(el)      a plain copy of an element's tree (tag, class, text, a few safe attributes),
 *                    the bubble's content: the Orb's page rebuilds it with textContent only.
 *  resolve(el, path) the original node a click on the copy points at.
 *  mood(bar)         what the Browser bar is saying: "needs" (the person is asked), "working"
 *                    (Autofill is filling), "done" (sent or submitted), "info", or "" (nothing).
 *  state(s)          the ONE state the ball shows, most important first.
 */
(function (root) {
  "use strict";
  const TAGS = new Set(["span", "div", "button", "ol", "ul", "li", "input", "strong", "em", "b", "i", "p", "small"]);
  const ATTRS = ["title", "aria-label", "aria-pressed", "aria-expanded", "placeholder"];
  const MAX_NODES = 400;
  const MAX_TEXT = 600;

  function snapshot(el) {
    let count = 0;
    function walk(n) {
      if (!n || count > MAX_NODES) return null;
      if (n.nodeType === 3) {
        const t = String(n.nodeValue || "");
        if (!t) return null;
        count += 1;
        return { t: t.slice(0, MAX_TEXT) };
      }
      if (n.nodeType !== 1) return null;
      const tag = String(n.tagName || "").toLowerCase();
      if (!TAGS.has(tag)) return null;
      count += 1;
      const o = { tag };
      const cls = typeof n.className === "string" ? n.className : "";
      if (cls) o.cls = cls.slice(0, 200);
      const a = {};
      for (const k of ATTRS) {
        const v = n.getAttribute ? n.getAttribute(k) : null;
        if (v != null) a[k] = String(v).slice(0, 300);
      }
      if (Object.keys(a).length) o.a = a;
      if (n.hidden) o.hidden = true;
      if (n.disabled) o.disabled = true;
      if (tag === "input") o.value = String(n.value || "").slice(0, MAX_TEXT);
      const kids = [];
      const list = n.childNodes || [];
      for (let i = 0; i < list.length; i++) kids.push(walk(list[i]));
      // keep the indexes: a path counts every child node, so a gap stays a gap
      if (kids.some(Boolean)) o.c = kids.map((k) => k || { t: "" });
      return o;
    }
    return walk(el);
  }

  function resolve(el, path) {
    if (!el || !Array.isArray(path)) return null;
    let n = el;
    for (const i of path) {
      if (!Number.isInteger(i) || i < 0 || !n.childNodes || i >= n.childNodes.length) return null;
      n = n.childNodes[i];
    }
    return n;
  }

  // The Browser bar's words and controls, read the way a person would.
  const NEEDS = /\b(need(s)? you|your turn|your answer|ready to send|check it|press submit|click submit|sign in|log in|captcha|verify|left for you|answer)\b/i;
  const WORKING = /^(filling|reading|opening|working|looking|checking|uploading|attaching|page \d+:? ?filling|carrying on|thanks\. carrying on)/i;
  const DONE = /^(submitted|sent)\b/i;
  function mood(bar) {
    if (!bar || bar.hidden) return "";
    const text = String(bar.text || "").trim();
    if (!text) return "";
    if (DONE.test(text)) return "done";
    if (bar.wall || bar.input || bar.primary) return "needs";
    if (bar.spin || WORKING.test(text)) return "working";
    if (NEEDS.test(text)) return "needs";
    return "info";
  }

  function state(s) {
    const x = s || {};
    if (x.offline) return "offline";
    if (x.celebrating) return "celebrating";
    if (x.thinking) return "thinking";
    if (x.mood === "needs") return "needs";
    if (x.mood === "working") return "working";
    if (x.attention) return "attention";
    if (x.sleeping) return "sleeping";
    return "idle";
  }

  const api = { snapshot, resolve, mood, state };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.OrbCore = api;
})(typeof window !== "undefined" ? window : globalThis);
