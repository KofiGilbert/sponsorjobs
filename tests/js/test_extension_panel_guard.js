// The extension's assisted-apply panel (extension/autofill.js) on a page it does not trust:
// the panel lives in a CLOSED shadow root on its own host element, so the page cannot find its
// buttons; and every button acts only on a real press by the person (isTrusted), so a click the
// page dispatches never asks the app for the profile. Runs autofill.js in a tiny fake DOM.
// Everyone and every site here is made up.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = (c, m) => { if (!c) throw new Error(m); };

// ---- a tiny DOM: enough for buildPanel / setNote / the listeners -------------------------------
class El {
  constructor(tag, reg) {
    this.tagName = String(tag || "div").toUpperCase();
    this._reg = reg || {};          // class name -> element, shared by one tree
    this._ls = {}; this.children = []; this.style = {}; this.className = ""; this.textContent = "";
    this.disabled = false; this.shadowRoot = null; this._shadow = null; this._shadowMode = null;
    this.parentNode = null;
  }
  set innerHTML(html) {
    this._html = String(html);
    const re = /<(\w+)[^>]*\bclass="([^"]+)"/g;
    let m;
    while ((m = re.exec(this._html))) {
      const el = new El(m[1], this._reg);
      el.className = m[2];
      for (const c of m[2].split(/\s+/)) if (!this._reg[c]) this._reg[c] = el;
      this.children.push(el);
    }
  }
  get innerHTML() { return this._html || ""; }
  querySelector(sel) { return this._reg[String(sel).replace(/^\./, "")] || null; }
  querySelectorAll() { return []; }
  addEventListener(t, fn) { (this._ls[t] = this._ls[t] || []).push(fn); }
  dispatch(t, ev) { for (const fn of this._ls[t] || []) fn(ev); }
  appendChild(c) { c.parentNode = this; this.children.push(c); if (c.className) for (const k of c.className.split(/\s+/)) if (!this._reg[k]) this._reg[k] = c; return c; }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((x) => x !== this); this.parentNode = null; }
  attachShadow(opts) {
    const root = new El("#shadow-root", {});
    this._shadow = root; this._shadowMode = opts && opts.mode;
    this.shadowRoot = opts && opts.mode === "open" ? root : null;
    return root;
  }
}

function load() {
  const body = new El("body");
  const sent = [];
  const sandbox = {
    console,
    setTimeout: (fn) => 0, clearTimeout: () => {},
    location: { hostname: "careers.brightwater.example", href: "https://careers.brightwater.example/apply" },
    document: { body, createElement: (t) => new El(t) },
    MutationObserver: function () { this.observe = () => {}; },
    chrome: { runtime: { lastError: null, sendMessage: (msg, cb) => { sent.push(msg); cb({ ok: true, loaded: false, fields: {} }); } } },
    window: {},
  };
  // The fill engine: a page with an email and a first-name box, so the panel is offered.
  sandbox.window.TailorAutofill = {
    candidates: () => [{ k: "email" }, { k: "first_name" }],
    signature: (el) => el, categorize: (el) => el.k,
    answerableTextareas: () => [],
  };
  vm.createContext(sandbox);
  sandbox.window.window = sandbox.window;
  vm.runInContext("var window = this.window; var TailorAutofill = window.TailorAutofill;", sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "..", "extension", "autofill.js"), "utf8"), sandbox);
  return { body, sent };
}

(async () => {
  const { body, sent } = load();
  // the panel is offered, on its own host element, in a closed shadow root
  assert(body.children.length === 1, "one host element on the page: " + body.children.length);
  const host = body.children[0];
  assert(host._shadowMode === "closed", "the shadow root is closed: " + host._shadowMode);
  assert(host.shadowRoot === null, "the page cannot reach the panel through the host");
  assert(!/tailor-af-fill/.test(host.innerHTML) && host.className === "", "the host itself carries nothing");
  const fill = host._shadow.querySelector(".tailor-af-fill");
  assert(fill, "the Fill button is inside the shadow root");
  assert(/<style>/.test(host._shadow.innerHTML), "the panel brings its own styles");

  // a click the page made up does nothing
  fill.dispatch("click", { isTrusted: false });
  fill.dispatch("click", {});
  await new Promise((r) => setImmediate(r));
  assert(sent.length === 0, "a scripted click never asks for the profile: " + JSON.stringify(sent));
  // the draft and hide buttons too
  host._shadow.querySelector(".tailor-af-x").dispatch("click", { isTrusted: false });
  assert(body.children.length === 1, "a scripted click never hides the panel");

  // the person's own press works
  fill.dispatch("click", { isTrusted: true });
  await new Promise((r) => setImmediate(r));
  assert(sent.length === 1 && sent[0].type === "autofill", "a real press fills: " + JSON.stringify(sent));
  host._shadow.querySelector(".tailor-af-x").dispatch("click", { isTrusted: true });
  assert(body.children.length === 0, "a real press on Hide removes the panel");

  // no listener in the file acts without the isTrusted check
  const src = fs.readFileSync(path.join(__dirname, "..", "..", "extension", "autofill.js"), "utf8");
  assert(!/addEventListener\("click", (?!\(e\) => \{\s*\n\s*if \(!e \|\| e\.isTrusted !== true\))/.test(src), "every click listener checks isTrusted");
  assert(!/document\.body\.appendChild\(panel\)/.test(src), "the panel is never put straight into the page");
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
