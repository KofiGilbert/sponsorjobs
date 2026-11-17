// Never skip the right resume (docs/apply-agent.md section 21), under node without a browser
// (tests/test_upload_identity.py runs it). Every tailored resume leaves the app as
// "<Name> Resume.pdf", so a Workday draft that kept an EARLIER application's resume lists a file
// of the same name. A listed copy counts as ours only with the same name AND size (within the
// rounding the page shows), or when this run itself put that very file there.
"use strict";
const path = require("path");
const assert = (c, m) => { if (!c) throw new Error(m); };
const U = require(path.join(__dirname, "..", "..", "shell-electron", "uploads.js"));
const A = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));

// ---- 1. the decision, from what the box's area lists -------------------------------------
{
  const file = { name: "Avery Example Resume.pdf", path: "/w/uploads/9/Avery Example Resume.pdf", size: 108810 };
  const fresh = U.createUploadLog();
  // same name, same size: this file, nothing uploaded
  let d = U.uploadDecision({ copies: 1, same: 1, unsized: 0, other: 0 }, file, fresh);
  assert(!d.upload && d.how === "listed" && !d.earlier && !d.drop, "same name and size: skip " + JSON.stringify(d));
  d = U.uploadDecision({ copies: 3, same: 3, unsized: 0, other: 0 }, file, fresh);
  assert(!d.upload && d.drop === "extra_same", "three verified copies: keep one " + JSON.stringify(d));
  // same name, another size: an earlier application's file; ours goes in, theirs stays, flagged
  d = U.uploadDecision({ copies: 1, same: 0, unsized: 0, other: 1 }, file, fresh);
  assert(d.upload && d.earlier && !d.drop, "same name, different size: upload ours and flag " + JSON.stringify(d));
  // same name, no size shown: not verified; ours goes in, theirs stays, flagged
  d = U.uploadDecision({ copies: 1, same: 0, unsized: 1, other: 0 }, file, fresh);
  assert(d.upload && d.earlier && !d.drop, "no size shown: upload ours and flag " + JSON.stringify(d));
  // nothing listed: upload, no flag
  d = U.uploadDecision({ copies: 0, same: 0, unsized: 0, other: 0 }, file, fresh);
  assert(d.upload && !d.earlier, "empty box " + JSON.stringify(d));
  d = U.uploadDecision(null, file, fresh);
  assert(d.upload && !d.earlier, "no listing read: upload");

  // this run put this very file there (the page shows no size): ours, never a second copy
  const log = U.createUploadLog();
  U.noteUpload(log, file);
  d = U.uploadDecision({ copies: 1, same: 0, unsized: 1, other: 0 }, file, log);
  assert(!d.upload && d.own && !d.earlier && !d.drop, "this run's own copy: skip " + JSON.stringify(d));
  // ours plus an earlier one beside it: no upload, still flagged
  d = U.uploadDecision({ copies: 2, same: 0, unsized: 2, other: 0 }, file, log);
  assert(!d.upload && d.own && d.earlier, "own copy and an earlier one: skip, flag " + JSON.stringify(d));
  // this run uploaded ANOTHER file under that name: that copy (ours) is removed, this one goes in
  const other = U.createUploadLog();
  U.noteUpload(other, { name: file.name, path: "/w/uploads/7/Avery Example Resume.pdf", size: 99000 });
  d = U.uploadDecision({ copies: 1, same: 0, unsized: 1, other: 0 }, file, other);
  assert(d.upload && d.drop === "unsized_own" && !d.earlier, "replace this run's other file " + JSON.stringify(d));
  // a file the person's earlier application left is never removed
  for (const L of [{ unsized: 1 }, { other: 1 }, { unsized: 1, other: 1 }]) {
    const x = U.uploadDecision(L, file, fresh);
    assert(x.drop !== "unsized_own", "never removes a copy this run did not upload " + JSON.stringify(L));
  }
  // the log keeps one line per file
  U.noteUpload(log, file);
  assert(log.files.length === 1, "one line per file");
}

// ---- 2. the page's listing: names, sizes and rounding (autofill_core.js) -------------------
class Node {
  constructor(tag, attrs, text) {
    this.tagName = String(tag).toUpperCase(); this._a = Object.assign({}, attrs || {}); this.children = [];
    this.parentElement = null; this._text = text || ""; this.id = (attrs && attrs.id) || "";
    this.type = (attrs && attrs.type) || ""; this.disabled = false; this.files = null;
    this.classList = { add() {}, remove() {}, contains() { return false; } };
  }
  add(...kids) { for (const k of kids) { k.parentElement = this; this.children.push(k); } return this; }
  getAttribute(k) { return k in this._a ? this._a[k] : null; }
  setAttribute(k, v) { this._a[k] = String(v); }
  get textContent() { return [this._text, ...this.children.map((c) => c.textContent)].filter(Boolean).join(" "); }
  get innerText() { return this.textContent; }
  contains(x) { for (let n = x; n; n = n.parentElement) if (n === this) return true; return false; }
  descendants() { const out = []; const w = (n) => { for (const c of n.children) { out.push(c); w(c); } }; w(this); return out; }
  matches(sel) {
    return sel.split(",").map((s) => s.trim()).some((s) => {
      if (s === "*") return true;
      let m = /^\[data-tailor-ref="([^"]+)"\]$/.exec(s);
      if (m) return this.getAttribute("data-tailor-ref") === m[1];
      m = /^\[role='([a-z]+)'\]$/.exec(s);
      if (m) return this.getAttribute("role") === m[1];
      if (/^input\[type="file"\]$/.test(s)) return this.tagName === "INPUT" && this.type === "file";
      if (/^[a-z0-9]+$/i.test(s)) return this.tagName === s.toUpperCase();
      return false;
    });
  }
  querySelectorAll(sel) { return this.descendants().filter((n) => n.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  click() { if (this.onclick) this.onclick(); }
  dispatchEvent() { if (this.onclick) this.onclick(); return true; }
  focus() {}
  scrollIntoView() {}
  getBoundingClientRect() { return { left: 0, top: 0, width: 10, height: 10, right: 10, bottom: 10 }; }
  remove() { const p = this.parentElement; if (p) { p.children = p.children.filter((c) => c !== this); this.parentElement = null; } }
}
const body = new Node("body");
const doc = { body, documentElement: body, querySelectorAll: (s) => body.querySelectorAll(s),
              querySelector: (s) => body.querySelector(s), getElementById: () => null, readyState: "complete",
              addEventListener() {}, createElement: (t) => new Node(t) };
global.window = { getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }), addEventListener() {} };
global.document = doc;
global.getComputedStyle = window.getComputedStyle;
global.location = { hostname: "acme.example.com", href: "https://acme.example.com/apply" };
global.CSS = { escape: (s) => String(s) };
global.Event = class { constructor(t) { this.type = t; } };
global.MouseEvent = global.Event; global.PointerEvent = global.Event; global.KeyboardEvent = global.Event;
global.HTMLInputElement = Node; global.HTMLSelectElement = class {}; global.HTMLTextAreaElement = class {};
global.Node = { DOCUMENT_POSITION_FOLLOWING: 4 };
require(path.join(__dirname, "..", "..", "extension", "autofill_core.js"));
const F = window.TailorAutofill;

// sizes: "106.26 KB" stands for 108805..108815 bytes (1024) or 106255..106265 (1000)
{
  const r = F.sizeReadings("Avery Example Resume.pdf 106.26 KB Successfully Uploaded!");
  assert(r.length === 1, "one size " + JSON.stringify(r));
  assert(F.sizeMatches(r, 108810) && F.sizeMatches(r, 106260), "within the rounding, either unit");
  assert(!F.sizeMatches(r, 108900) && !F.sizeMatches(r, 107000), "a few hundred bytes off is another file");
  assert(F.sizeMatches(F.sizeReadings("106 KB"), 108700), "no decimals: within half a KB");
  assert(!F.sizeReadings("Upload a file (5MB max)").length && !F.sizeReadings("up to 5 MB").length, "a limit is not a size");
  assert(!F.sizeMatches([], 108810) && !F.sizeMatches(r, 0), "nothing to compare: no match");
}

// a Workday-like box: each listed file a row with its own Delete button
const NAME = "Avery Example Resume.pdf";
function box() {
  body.children = [];
  const group = new Node("div", { role: "group" });
  const input = new Node("input", { type: "file", "data-tailor-ref": "r1" });
  const list = new Node("div");
  group.add(new Node("h4", {}, "Resume/CV"), new Node("label", {}, "Upload a file (5MB max)"), new Node("div").add(input), list);
  body.add(group);
  const addRow = (name, size) => {
    const row = new Node("div");
    const del = new Node("button", { "aria-label": "Delete " + name }, "Delete");
    del.onclick = () => row.remove();
    row.add(new Node("span", {}, name), ...(size ? [new Node("span", {}, size)] : []), new Node("span", {}, "Successfully Uploaded!"), del);
    list.add(row);
    return row;
  };
  return { addRow, list };
}
{
  const b = box();
  let L = F.fileListing("r1", NAME, 108810);
  assert(L.copies === 0 && L.same === 0, "empty " + JSON.stringify(L));
  b.addRow(NAME, "106.26 KB");
  L = F.fileListing("r1", NAME, 108810);
  assert(L.copies === 1 && L.same === 1, "same name and size: verified " + JSON.stringify(L));
  assert(F.fileCopies(F.fileListing ? body.querySelector('[data-tailor-ref="r1"]') : null, NAME, 108810) === 1, "fileCopies counts verified copies");
  assert(F.fileListed("r1", NAME, 108810) === true, "listed");
  L = F.fileListing("r1", NAME, 512000);
  assert(L.same === 0 && L.other === 1, "same name, other size: another file " + JSON.stringify(L));
  assert(F.fileListed("r1", NAME, 512000) === false, "not listed as ours");
}
{
  const b = box();
  b.addRow(NAME, "");
  const L = F.fileListing("r1", NAME, 108810);
  assert(L.copies === 1 && L.unsized === 1 && L.same === 0, "no size shown: not verified " + JSON.stringify(L));
  assert(F.fileListed("r1", NAME, 108810) === false, "no size shown is never 'already there'");
  // the 5MB max beside the box is not read as the file's size
  assert(L.other === 0, "the limit text is not a size");
}
{
  // an earlier application's copy (another size) and ours: one each, ours verified
  const b = box();
  b.addRow(NAME, "97.10 KB");
  b.addRow(NAME, "106.26 KB");
  const L = F.fileListing("r1", NAME, 108810);
  assert(L.copies === 2 && L.same === 1 && L.other === 1, "one ours, one earlier " + JSON.stringify(L));
  assert(F.dropDuplicateFiles("r1", NAME, 108810) === 0, "the earlier file is never removed as a duplicate");
  b.addRow(NAME, "106.26 KB");
  b.addRow(NAME, "106.26 KB");
  assert(F.dropDuplicateFiles("r1", NAME, 108810) === 2, "extra verified copies removed");
  const after = F.fileListing("r1", NAME, 108810);
  assert(after.same === 1 && after.other === 1, "one of ours and the earlier one left " + JSON.stringify(after));
}
{
  const b = box();
  b.addRow("Other Person Resume.pdf", "106.26 KB");
  b.addRow(NAME, "");
  assert(F.dropUnsizedCopy("r1", NAME, 108810) === 1, "an unsized copy is removable on request");
  assert(F.fileListing("r1", NAME, 108810).copies === 0 && b.list.children.length === 1, "only that row went");
}

// ---- 3. the Review answers list and its Telegram lines flag an earlier file ---------------
{
  const ans = A.createAnswers();
  A.addFile(ans, { page: 2, kind: "resume", path: "/w/uploads/9/" + NAME, how: "attached", earlier: true });
  A.addFile(ans, { page: 3, kind: "resume", path: "/w/uploads/9/" + NAME, how: "attached" });   // later page, ours again
  const L = A.readyAnswers(ans, { recordId: 9 });
  assert(L.files.length === 1 && L.files[0].flag === "earlier_file", "flagged once, and it stays " + JSON.stringify(L.files));
  assert(L.flagged === 1, "counted");
  const tg = A.answersTelegram(L, "Analyst at Acme");
  assert(tg.includes("the form also holds an earlier file with the same name: check which one to keep"), tg);
  // another application's resume still reads as that, first
  const o = A.createAnswers();
  A.addFile(o, { kind: "resume", path: "/w/uploads/7/" + NAME, how: "attached", earlier: true });
  assert(A.readyAnswers(o, { recordId: 9 }).files[0].flag === "other_resume", "other_resume first");
}
console.log("ok");
