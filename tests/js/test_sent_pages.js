// The confirmation pages of the 2026-11-26 runs count as page evidence through the fill engine's
// own markers (extension/autofill_core.js detectSuccess), the evidence the detector trusts alone:
// Greenhouse (Stripe, Vestmark, Axon, Meriton), Ashby (Applied Compute), Lever (CrossCountry), and
// Lam Research's Phenom window over the profile page. And the application form itself never does.
// Under node with a small stand-in page; tests/test_sent_detection.py runs it.
"use strict";
const fs = require("fs");
const path = require("path");
const assert = (c, m) => { if (!c) throw new Error(m); };
const FIX = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "fixtures", "sent", "confirmation_pages.json"), "utf8"));

const page = { text: "", dialog: "", host: "" };
const body = { get textContent() { return page.text; }, querySelectorAll: () => [], querySelector: () => null };
const dialogEl = { get textContent() { return page.dialog; }, getAttribute: () => null, ownerDocument: null };
global.window = { getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }), addEventListener() {} };
global.document = {
  body, documentElement: body, getElementById: () => null, readyState: "complete", addEventListener() {},
  querySelector: () => null,
  querySelectorAll: (sel) => (page.dialog && /dialog/.test(String(sel)) ? [dialogEl] : []),
};
global.location = { get hostname() { return page.host; }, href: "" };
global.CSS = { escape: (s) => String(s) };
global.HTMLInputElement = class {}; global.HTMLSelectElement = class {}; global.HTMLTextAreaElement = class {};
require(path.join(__dirname, "..", "..", "extension", "autofill_core.js"));
const F = window.TailorAutofill;

for (const p of FIX.pages) {
  page.text = p.text; page.dialog = p.dialog || ""; page.host = new URL(p.url).hostname;
  assert(F.detectSuccess(F.recipeFor(p.url), p.url) === true, `${p.name}: its confirmation counts as sent`);
}
// the form itself, before Submit: no marker
page.text = "Apply for this job First name Last name Email Phone Resume/CV Attach Submit application"; page.dialog = "";
page.host = "jobs.lever.co";
assert(F.detectSuccess(F.recipeFor("https://jobs.lever.co/acme/x/apply"), "https://jobs.lever.co/acme/x/apply") === false,
       "the application form is not a confirmation");
console.log("sent pages ok");
