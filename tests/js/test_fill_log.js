// The fill engine's log for the agent's Review answers list (extension/autofill_core.js
// takeFillLog), under node with a tiny fake DOM (tests/test_ready_answers.py runs it): the plan's
// ops are logged with their origin, the question and the value the control shows are read back,
// the log empties once read, and nothing is logged until the shell turns it on.
"use strict";
const path = require("path");
const assert = (c, m) => { if (!c) throw new Error(m); };

class FakeEl {
  constructor(tag, attrs, extra) {
    this.tagName = tag; this._a = Object.assign({}, attrs || {}); this.id = ""; this.parentElement = null;
    this.classList = { add() {}, remove() {} };
    this._value = "";
    Object.assign(this, extra || {});
  }
  getAttribute(k) { return k in this._a ? this._a[k] : null; }
  setAttribute(k, v) { this._a[k] = String(v); }
  closest() { return null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  dispatchEvent() { return true; }
  contains(x) { return x === this; }
}
class FakeInput extends FakeEl {}
Object.defineProperty(FakeInput.prototype, "value", { get() { return this._value; }, set(v) { this._value = String(v); }, configurable: true });
class FakeSelect extends FakeEl {}
Object.defineProperty(FakeSelect.prototype, "value", {
  get() { const o = this.options[this.selectedIndex]; return o ? o.value : ""; },
  set(v) { const i = this.options.findIndex((o) => o.value === v); if (i >= 0) this.selectedIndex = i; }, configurable: true });

const phone = new FakeInput("INPUT", { "aria-label": "Phone", "data-tailor-ref": "t1" }, { type: "tel" });
const study = new FakeInput("INPUT", { "aria-label": "Field of Study", "data-tailor-ref": "t2" }, { type: "text" });
const opts = ["Select one", "Bachelor's", "Master's"].map((t) => ({ value: t, textContent: t }));
const degree = new FakeSelect("SELECT", { "aria-label": "Degree", "data-tailor-ref": "t3" }, { options: opts, selectedIndex: 0 });
const all = [phone, study, degree];
global.window = {};
global.HTMLInputElement = FakeInput; global.HTMLSelectElement = FakeSelect; global.HTMLTextAreaElement = FakeInput;
global.Event = class { constructor(t) { this.type = t; } };
global.CSS = { escape: (s) => String(s) };
global.location = { hostname: "acme.example.com", href: "https://acme.example.com/apply" };
global.document = {
  querySelectorAll(sel) {
    const m = /data-tailor-ref="([^"]+)"/.exec(sel);
    return m ? all.filter((e) => e.getAttribute("data-tailor-ref") === m[1]) : [];
  },
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
  getElementById() { return null; },
};
require(path.join(__dirname, "..", "..", "extension", "autofill_core.js"));
const A = window.TailorAutofill;

(async () => {
  // the extension's own fill never logs: nothing to take
  A.applyOps([{ ref: "t1", op: "fill", text: "555 0100", from: "profile" }]);
  assert(A.takeFillLog().length === 0, "no log until the shell turns it on");
  phone.value = "";

  const rep = await A.applyOpsAsync([
    { ref: "t1", op: "fill", text: "555 0100", from: "profile" },
    { ref: "t2", op: "fill", text: "Agricultural Economics" },              // no origin: the AI's
    { ref: "t3", op: "select", text: "Master's", from: "library" },
  ]);
  assert(rep.filled === 3, "three filled " + JSON.stringify(rep));
  const log = A.takeFillLog();
  const by = Object.fromEntries(log.map((r) => [r.label, r]));
  assert(by.Phone && by.Phone.value === "555 0100" && by.Phone.from === "profile", "phone " + JSON.stringify(log));
  assert(by["Field of Study"] && by["Field of Study"].from === "ai", "an op with no origin is the AI's " + JSON.stringify(log));
  assert(by.Degree && by.Degree.value === "Master's" && by.Degree.from === "library", "the select reads back its option " + JSON.stringify(log));
  assert(A.takeFillLog().length === 0, "read once, then empty");
  console.log("ok");
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
