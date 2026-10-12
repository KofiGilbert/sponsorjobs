// Node harness for the fill engine's RECIPE layer (extension/autofill_core.js).
//
// A deliberately small DOM: just enough HTML parsing and CSS selecting to load a trimmed
// snippet of a REAL application page (tests/fixtures/recipes/*.html) and run the engine on
// it the way a browser would. Supports: tag, #id, .class, [attr], [attr=v], [attr*=v],
// [attr^=v], [attr$=v] (with the ` i` flag), :not(...), descendant and child combinators,
// and comma lists. No layout: every element is "shown" unless it says otherwise.
//
//   node recipe_harness.js <core.js> <recipes.json> <scenario.json>
//
// scenario.json: {fixture, url, payload, actions:[...]} -> prints a JSON report.
"use strict";
const fs = require("fs");

// ----------------------------------------------------------------- tiny DOM
const VOID = new Set(["input", "meta", "br", "img", "link", "hr"]);

global.HTMLInputElement = function () {};
global.HTMLTextAreaElement = function () {};
global.HTMLSelectElement = function () {};
global.HTMLElement = function () {};
global.Event = function (type, init) { this.type = type; this.bubbles = !!(init && init.bubbles); };

const clicks = [];
let listeners = [];                       // document-level listeners (capture + bubble)

function makeElement(tagName) {
  const tag = tagName.toLowerCase();
  const proto = tag === "input" ? HTMLInputElement.prototype
              : tag === "textarea" ? HTMLTextAreaElement.prototype
              : tag === "select" ? HTMLSelectElement.prototype : HTMLElement.prototype;
  const el = Object.create(proto);
  el.tagName = tag.toUpperCase();
  el.attrs = {};
  el.children = [];
  el.parentElement = null;
  el._text = "";
  return el;
}

function defineProps() {
  for (const P of [HTMLInputElement, HTMLTextAreaElement, HTMLSelectElement, HTMLElement]) {
    Object.defineProperty(P.prototype, "value", {
      configurable: true,
      get() {
        if (this.tagName === "SELECT") {
          const sel = this.options.find((o) => o.selected) || this.options[0];
          return this._value != null ? this._value : (sel ? sel.value : "");
        }
        return this._value != null ? this._value : (this.attrs.value || "");
      },
      set(v) { this._value = String(v); },
    });
    Object.defineProperty(P.prototype, "checked", {
      configurable: true,
      get() { return !!this._checked; },
      set(v) { this._checked = !!v; },
    });
    // Copy as DESCRIPTORS: Object.assign would evaluate the getters against the literal itself.
    Object.defineProperties(P.prototype, Object.getOwnPropertyDescriptors({
      getAttribute(a) { const k = a.toLowerCase(); return k in this.attrs ? this.attrs[k] : null; },
      setAttribute(a, v) { this.attrs[a.toLowerCase()] = String(v); },
      hasAttribute(a) { return a.toLowerCase() in this.attrs; },
      get id() { return this.attrs.id || ""; },
      get name() { return this.attrs.name || ""; },
      get type() { return this.tagName === "INPUT" ? (this.attrs.type || "text") : ""; },
      get placeholder() { return this.attrs.placeholder || ""; },
      get disabled() { return "disabled" in this.attrs; },
      get readOnly() { return "readonly" in this.attrs; },
      get required() { return "required" in this.attrs; },
      get className() { return this.attrs.class || ""; },
      get textContent() {
        if (this.tagName === "#TEXT") return this._text;
        return this.children.map((c) => c.textContent).join("");
      },
      set textContent(v) { this.children = [textNode(String(v))]; },
      get innerText() { return this.textContent; },
      get options() { return this.tagName === "SELECT" ? all(this).filter((e) => e.tagName === "OPTION") : []; },
      get selected() { return "selected" in this.attrs; },
      get nextElementSibling() {
        const p = this.parentElement; if (!p) return null;
        const sib = p.children.filter((c) => c.tagName !== "#TEXT");
        return sib[sib.indexOf(this) + 1] || null;
      },
      get classList() {
        const self = this;
        return {
          add(c) { const s = new Set((self.attrs.class || "").split(/\s+/).filter(Boolean)); s.add(c); self.attrs.class = [...s].join(" "); },
          remove(c) { const s = new Set((self.attrs.class || "").split(/\s+/).filter(Boolean)); s.delete(c); self.attrs.class = [...s].join(" "); },
          contains(c) { return (self.attrs.class || "").split(/\s+/).includes(c); },
        };
      },
      getBoundingClientRect() {
        const hidden = /display:\s*none/.test(this.attrs.style || "") || /\bvisually-hidden\b/.test(this.attrs.class || "");
        return hidden ? { width: 0, height: 0, right: 0, bottom: 0 } : { width: 120, height: 24, right: 300, bottom: 400 };
      },
      contains(other) { let n = other; while (n) { if (n === this) return true; n = n.parentElement; } return false; },
      closest(sel) { let n = this; while (n && n.tagName !== "#TEXT") { if (n.tagName !== "#DOC" && matches(n, sel)) return n; n = n.parentElement; } return null; },
      matches(sel) { return matches(this, sel); },
      querySelectorAll(sel) { return qsa(this, sel); },
      querySelector(sel) { return qsa(this, sel)[0] || null; },
      appendChild(c) { c.parentElement = this; this.children.push(c); return c; },
      insertAdjacentElement(_where, c) { return this.appendChild(c); },
      remove() { const p = this.parentElement; if (p) p.children = p.children.filter((c) => c !== this); },
      addEventListener() {},
      dispatchEvent(ev) {
        // A click EVENT on a radio/checkbox is how the engine tells React it set the control; a
        // click on anything that navigates or submits (button, link, submit input) is what the
        // tests forbid, so only those count.
        if (ev.type === "click" && !/^(radio|checkbox)$/.test(this.type)) clicks.push(this);
        for (const l of listeners) if (l.type === ev.type) l.fn({ type: ev.type, target: this });
        return true;
      },
      click() { clicks.push(this); },
    }));
  }
}
defineProps();

function textNode(t) { const n = Object.create(HTMLElement.prototype); n.tagName = "#TEXT"; n._text = t; n.children = []; n.attrs = {}; return n; }
function all(root) { const out = []; (function walk(n) { for (const c of n.children) { if (c.tagName !== "#TEXT") { out.push(c); walk(c); } } })(root); return out; }

function parseHTML(html) {
  const doc = makeElement("#doc"); doc.tagName = "#DOC";
  let cur = doc;
  const re = /<!--[\s\S]*?-->|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>|([^<]+)/g;
  let m;
  while ((m = re.exec(html))) {
    if (m[0].startsWith("<!--")) continue;
    if (m[1]) {                                             // closing tag
      let n = cur; const want = m[1].toUpperCase();
      while (n && n.tagName !== want) n = n.parentElement;
      if (n && n.parentElement) cur = n.parentElement;
      continue;
    }
    if (m[2]) {                                             // opening tag
      const el = makeElement(m[2]);
      const attrRe = /([^\s=\/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let a;
      while ((a = attrRe.exec(m[3] || ""))) {
        el.attrs[a[1].toLowerCase()] = a[2] != null ? a[2] : a[3] != null ? a[3] : a[4] != null ? a[4] : "";
      }
      cur.appendChild(el);
      if (!m[4] && !VOID.has(m[2].toLowerCase())) cur = el;
      continue;
    }
    if (m[5] && m[5].trim()) cur.appendChild(textNode(m[5].replace(/&#x27;/g, "'").replace(/&amp;/g, "&")));
  }
  return doc;
}

// ----------------------------------------------------------------- selectors
function splitTop(s, sep) {
  const out = []; let depth = 0, inq = null, cur = "";
  for (const ch of s) {
    if (inq) { cur += ch; if (ch === inq) inq = null; continue; }
    if (ch === '"' || ch === "'") { inq = ch; cur += ch; continue; }
    if (ch === "[" || ch === "(") depth++;
    if (ch === "]" || ch === ")") depth--;
    if (depth === 0 && ch === sep) { out.push(cur); cur = ""; continue; }
    cur += ch;
  }
  out.push(cur);
  return out.map((x) => x.trim()).filter(Boolean);
}

function parseCompound(c) {
  const parts = { tag: null, id: null, classes: [], attrs: [], nots: [] };
  const re = /\*|[a-zA-Z][\w-]*|#[\w-]+|\.[\w-]+|\[(?:"[^"]*"|'[^']*'|[^\]"'])+\]|:not\(([^)]*)\)/g;
  let m;
  while ((m = re.exec(c))) {
    const t = m[0];
    if (t === "*") continue;
    if (t.startsWith("#")) parts.id = t.slice(1);
    else if (t.startsWith(".")) parts.classes.push(t.slice(1));
    else if (t.startsWith("[")) {
      const am = /^\[\s*([\w-]+)\s*(?:([*^$|~]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\s\]]+))\s*(i)?)?\s*\]$/.exec(t);
      if (!am) throw new Error("bad attr selector " + t);
      parts.attrs.push({ name: am[1].toLowerCase(), op: am[2] || null, val: am[3] != null ? am[3] : am[4] != null ? am[4] : am[5] != null ? am[5] : null, ci: !!am[6] });
    } else if (t.startsWith(":not(")) parts.nots.push(parseCompound(m[1]));
    else parts.tag = t.toUpperCase();
  }
  return parts;
}

function matchCompound(el, p) {
  if (p.tag && el.tagName !== p.tag) return false;
  if (p.id && el.attrs.id !== p.id) return false;
  const cls = (el.attrs.class || "").split(/\s+/);
  for (const c of p.classes) if (!cls.includes(c)) return false;
  for (const a of p.attrs) {
    if (!(a.name in el.attrs)) return false;
    if (!a.op) continue;
    let have = el.attrs[a.name], want = a.val;
    if (a.ci) { have = have.toLowerCase(); want = want.toLowerCase(); }
    if (a.op === "=" && have !== want) return false;
    if (a.op === "*=" && !have.includes(want)) return false;
    if (a.op === "^=" && !have.startsWith(want)) return false;
    if (a.op === "$=" && !have.endsWith(want)) return false;
  }
  for (const n of p.nots) if (matchCompound(el, n)) return false;
  return true;
}

function parseComplex(sel) {
  // tokens: compound (combinator compound)*  ; combinators: ' ' (descendant) and '>' (child)
  const tokens = []; let cur = "", depth = 0, inq = null;
  const flush = () => { if (cur.trim()) tokens.push({ compound: parseCompound(cur.trim()) }); cur = ""; };
  for (const ch of sel) {
    if (inq) { cur += ch; if (ch === inq) inq = null; continue; }
    if (ch === '"' || ch === "'") { inq = ch; cur += ch; continue; }
    if (ch === "[" || ch === "(") depth++;
    if (ch === "]" || ch === ")") depth--;
    if (depth === 0 && (ch === " " || ch === ">")) {
      flush();
      if (ch === ">") tokens.push({ comb: ">" });
      else if (tokens.length && !tokens[tokens.length - 1].comb) tokens.push({ comb: " " });
      continue;
    }
    cur += ch;
  }
  flush();
  return tokens.filter((t, i, arr) => !(t.comb && (i === 0 || i === arr.length - 1 || arr[i - 1].comb)));
}

function matchComplex(el, tokens) {
  let i = tokens.length - 1;
  if (!matchCompound(el, tokens[i].compound)) return false;
  let node = el;
  i--;
  while (i >= 0) {
    const comb = tokens[i].comb; i--;
    const want = tokens[i].compound;
    if (comb === ">") {
      node = node.parentElement;
      if (!node || node.tagName === "#DOC" || !matchCompound(node, want)) return false;
    } else {
      node = node.parentElement;
      while (node && node.tagName !== "#DOC" && !matchCompound(node, want)) node = node.parentElement;
      if (!node || node.tagName === "#DOC") return false;
    }
    i--;
  }
  return true;
}

const cache = new Map();
function compile(sel) {
  if (!cache.has(sel)) cache.set(sel, splitTop(sel, ",").map(parseComplex));
  return cache.get(sel);
}
function matches(el, sel) { return compile(sel).some((tokens) => matchComplex(el, tokens)); }
function qsa(root, sel) { return all(root).filter((el) => matches(el, sel)); }

// ----------------------------------------------------------------- run
const [, , corePath, recipesPath, scenarioPath] = process.argv;
const core = fs.readFileSync(corePath, "utf8");
const recipes = JSON.parse(fs.readFileSync(recipesPath, "utf8"));
const sc = JSON.parse(fs.readFileSync(scenarioPath, "utf8"));
const html = sc.fixture ? fs.readFileSync(sc.fixture, "utf8") : (sc.html || "");
const doc = parseHTML(html);

const document = {
  querySelectorAll: (s) => qsa(doc, s),
  querySelector: (s) => qsa(doc, s)[0] || null,
  getElementById: (id) => all(doc).find((e) => e.attrs.id === id) || null,
  get body() { return doc; },
  get head() { return all(doc).find((e) => e.tagName === "HEAD") || doc; },
  createElement: (t) => makeElement(t),
  addEventListener: (type, fn) => listeners.push({ type, fn }),
  title: "",
};
const location = { href: sc.url || "", hostname: (() => { try { return new URL(sc.url).hostname; } catch (e) { return ""; } })() };
const CSS = { escape: (s) => String(s).replace(/(["\\])/g, "\\$1") };
const window = {};
new Function("window", "document", "location", "CSS", "Event", "setTimeout", "setInterval", "clearInterval", core)(
  window, document, location, CSS, Event, (f) => {}, () => 1, () => {});
const TA = window.TailorAutofill;

const out = {};
for (const act of sc.actions || []) {
  if (act.op === "recipeFor") {
    const r = TA.recipeFor();
    out[act.as || "recipe"] = r ? r.id : null;
  } else if (act.op === "fill") {
    const payload = Object.assign({ recipes }, sc.payload || {});
    out[act.as || "report"] = TA.fill(payload);
    TA.watchCorrections(null);                 // what both carriers do right after a fill
  } else if (act.op === "values") {
    const v = {};
    for (const [k, sel] of Object.entries(act.of || {})) {
      const el = qsa(doc, sel)[0];
      v[k] = el ? (el.tagName === "INPUT" && el.type === "radio" ? el.checked : el.value) : null;
    }
    out[act.as || "values"] = v;
  } else if (act.op === "classOf") {
    const el = qsa(doc, act.sel)[0];
    out[act.as || "class"] = el ? el.attrs.class || "" : null;
  } else if (act.op === "change") {
    const el = qsa(doc, act.sel)[0];
    if (el) { el.value = act.value; el.dispatchEvent(new Event("change", { bubbles: true })); }
  } else if (act.op === "takeCorrections") {
    out[act.as || "corrections"] = TA.takeCorrections();
  } else if (act.op === "detectSuccess") {
    out[act.as || "success"] = TA.detectSuccess();
  } else if (act.op === "remaining") {
    out[act.as || "remaining"] = TA.extractForm({ onlyEmpty: true }).map((f) => f.label);
  }
}
out.clicks = clicks.length;
console.log(JSON.stringify(out));
