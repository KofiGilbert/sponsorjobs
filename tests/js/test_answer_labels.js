// Review answers labels and values (Gusto on Greenhouse, 2026-11-16), under node without a
// browser (tests/test_ready_answers.py runs it):
// 1. a question keeps its own words: "(please select one)" is part of Greenhouse's question, and
//    stripping "select one" as a placeholder left "(please )?";
// 2. a ticked box in a group lists the box's name ("Indeed"), never "Checked".
// The same corrected label and value reach the Telegram text.
"use strict";
const fs = require("fs");
const path = require("path");
const assert = (c, m) => { if (!c) throw new Error(m); };
const A = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));

class Node {
  constructor(tag, attrs, text) {
    this.tagName = String(tag).toUpperCase(); this._a = Object.assign({}, attrs || {}); this.children = [];
    this.parentElement = null; this._text = text || ""; this.id = (attrs && attrs.id) || "";
    this.type = (attrs && attrs.type) || ""; this.checked = !!(attrs && attrs.checked); this.value = (attrs && attrs.value) || "";
    this.disabled = false; this.classList = { add() {}, remove() {}, contains() { return false; } };
  }
  add(...kids) { for (const k of kids) { k.parentElement = this; this.children.push(k); } return this; }
  get nextElementSibling() { const p = this.parentElement; if (!p) return null; const i = p.children.indexOf(this); return p.children[i + 1] || null; }
  getAttribute(k) { return k in this._a ? this._a[k] : null; }
  setAttribute(k, v) { this._a[k] = String(v); }
  get textContent() { return [this._text, ...this.children.map((c) => c.textContent)].filter(Boolean).join(" "); }
  get innerText() { return this.textContent; }
  contains(x) { for (let n = x; n; n = n.parentElement) if (n === this) return true; return false; }
  descendants() { const out = []; const w = (n) => { for (const c of n.children) { out.push(c); w(c); } }; w(this); return out; }
  matches(sel) { return sel.split(",").map((s) => s.trim()).some((s) => /^[a-z]+$/i.test(s) && this.tagName === s.toUpperCase()); }
  querySelectorAll(sel) { return this.descendants().filter((n) => n.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { for (let n = this; n; n = n.parentElement) if (n.matches(sel)) return n; return null; }
  getRootNode() { return doc; }
}
const body = new Node("body");
const doc = { body, documentElement: body, querySelectorAll: (s) => body.querySelectorAll(s), querySelector: (s) => body.querySelector(s),
              getElementById: () => null, readyState: "complete", addEventListener() {} };
global.window = { getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }), addEventListener() {} };
global.document = doc;
global.location = { hostname: "acme.example.com", href: "https://acme.example.com/apply" };
global.CSS = { escape: (s) => String(s) };
global.Event = class { constructor(t) { this.type = t; } };
global.HTMLInputElement = Node; global.HTMLSelectElement = class {}; global.HTMLTextAreaElement = class {};
require(path.join(__dirname, "..", "..", "extension", "autofill_core.js"));
const F = window.TailorAutofill;

// ---- 1. cleanLabel keeps the question's words (the engine's and the agent view's copy) ------
const viewSrc = fs.readFileSync(path.join(__dirname, "..", "..", "extension", "agent_view.js"), "utf8");
const m = /function cleanLabel\(t\) \{[\s\S]*?\n  \}\n/.exec(viewSrc);
assert(m, "agent_view.js cleanLabel found");
const viewClean = new Function(m[0] + "; return cleanLabel;")();
const CASES = [
  ["How would you describe your gender identity (please select one)?", "How would you describe your gender identity (please select one)?"],
  ["Are you Hispanic/Latino? (please select one) *", "Are you Hispanic/Latino? (please select one)"],
  ["Is a visa required to work here?", "Is a visa required to work here?"],
  ["Gender Select One Required", "Gender"],
  ["Gender*", "Gender"],
  ["Phone (Required)", "Phone"],
  ["Country (Select one)", "Country"],
  ["Required: Degree", "Degree"],
  ["Select One", ""],
  ["  How did   you hear about us? ✱", "How did you hear about us?"],
];
for (const [inp, want] of CASES) {
  assert(F.cleanLabel(inp) === want, "engine cleanLabel " + JSON.stringify(inp) + " -> " + JSON.stringify(F.cleanLabel(inp)));
  assert(viewClean(inp) === want, "view cleanLabel " + JSON.stringify(inp) + " -> " + JSON.stringify(viewClean(inp)));
}

// ---- 2. a ticked box in a group: the question, and the box's own name as the value ----------
{
  const indeed = new Node("input", { type: "checkbox", checked: true, value: "4021" });
  const linkedin = new Node("input", { type: "checkbox", value: "4022" });
  const fsBox = new Node("fieldset").add(new Node("legend", {}, "How did you hear about this opportunity? (select all that apply)"),
    new Node("label").add(indeed, new Node("span", {}, "Indeed")), new Node("label").add(linkedin, new Node("span", {}, "LinkedIn")));
  body.add(fsBox);
  const got = F.readBack(indeed);
  assert(got && got[0] === "How did you hear about this opportunity? (select all that apply)" && got[1] === "Indeed",
         "the box's name, never Checked " + JSON.stringify(got));
  // a lone consent box keeps "Checked"
  const agree = new Node("input", { type: "checkbox", checked: true });
  body.add(new Node("div").add(new Node("label").add(agree, new Node("span", {}, "I agree to the privacy notice"))));
  const lone = F.readBack(agree);
  assert(lone && lone[0] === "I agree to the privacy notice" && lone[1] === "Checked", "a lone box " + JSON.stringify(lone));
}

// ---- 3. the brain's own tick: the question above the boxes, the box's name ------------------
{
  const view = [
    "How did you hear about this opportunity? (select all that apply)",
    "[11]<checkbox label=\"Indeed\" checked=false>",
    "[12]<checkbox label=\"LinkedIn\" checked=false>",
    "[13]<checkbox label=\"I agree to the privacy notice\" checked=false>",
  ].join("\n");
  const a = A.brainAnswer(view, { op: "click", index: 12 }, {});
  assert(a && a.label === "How did you hear about this opportunity? (select all that apply)" && a.value === "LinkedIn",
         "brain tick " + JSON.stringify(a));
  const alone = A.brainAnswer("Terms\n[3]<button>Back</button>\n[4]<checkbox label=\"I agree\" checked=false>", { op: "click", index: 4 }, {});
  assert(alone && alone.label === "I agree" && alone.value === "Checked", "a lone box " + JSON.stringify(alone));

  // the list and the Telegram text carry the corrected words
  const ans = A.createAnswers();
  A.addAnswer(ans, { page: 1, label: "How would you describe your gender identity (please select one)?", value: "Prefer not", from: "eeo" });
  A.addAnswer(ans, Object.assign({ page: 1 }, a));
  const L = A.readyAnswers(ans, { recordId: 1 });
  assert(L.rows[0].label === "How would you describe your gender identity (please select one)?", "label kept " + JSON.stringify(L.rows));
  const tg = A.answersTelegram(L, "Analyst at Acme");
  assert(tg.includes("How would you describe your gender identity (please select one)?: (self-identification, shown only in the app)"), tg);
  assert(tg.includes("How did you hear about this opportunity? (select all that apply): LinkedIn (AI pick, check it)"), tg);
  assert(!tg.includes(": Checked"), "no Checked for a group " + tg);
  const n = A.readyNotice(L, "1");
  assert(n.lines.some((x) => x.includes(": LinkedIn (AI pick")), "notice line " + JSON.stringify(n.lines));
}
console.log("ok");
