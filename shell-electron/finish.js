// "Autofill, then the agent finishes" (docs/apply-agent.md section 35): the rules, kept apart from
// main.js so they are tested without Electron (tests/js/test_autofill_finish.js).
//
//   needsFinish(report, extra)  whether the agent steps in after the fast Autofill, and why
//   planKey / unplannedFields / notePlanned
//                               the AI plan runs only for fields it has not been asked about on
//                               this page yet (an agent step on a page that did not change asks
//                               the model nothing)
"use strict";

// Never handed to the agent: the person's own step (a sign-in, a captcha), a site we do not fill,
// or no page at all.
const HAND_BACK = new Set(["blocked", "linkedin", "fill-by-hand", "bad-url", "no-page", "no-viewport", "inject-failed", "stalled"]);

// report: what runAutoApplyOnPane (or assist:apply) said; extra: { invalid } (fields the page
// itself marks invalid after the fill). Returns { go, why } with why one of:
//   "job-page"  Autofill landed on the job or overview page instead of the form;
//   "empty"     required fields are still empty;
//   "invalid"   the page says a filled field is wrong;
//   "pages"     the form goes on over further pages.
// Otherwise { go: false, why: "" }: Autofill alone finished the page, as today.
function needsFinish(report, extra) {
  const r = report || {};
  const x = extra || {};
  if (!report) return { go: false, why: "" };
  if (r.reason === "not-a-form") return { go: true, why: "job-page" };
  if (HAND_BACK.has(r.reason) || r.blocker === "captcha" || r.captcha || r.wall) return { go: false, why: "" };
  if (r.submitted) return { go: false, why: "" };
  const left = (r.left || []).filter((l) => String(typeof l === "string" ? l : (l && l.label) || "").trim());
  if (left.length) return { go: true, why: "empty" };
  if (Number(x.invalid) > 0) return { go: true, why: "invalid" };
  if (r.next_page) return { go: true, why: "pages" };
  return { go: false, why: "" };
}

// One sentence for the bar when the agent steps in.
const WHY_WORDS = {
  "job-page": "Autofill opened the job page, so I am opening the form",
  "empty": "Some required fields are still empty",
  "invalid": "The site says a field needs fixing",
  "pages": "The form has more pages",
};

function planKey(recordId, url) {
  let where = String(url || "");
  try { const u = new URL(where); where = u.host + u.pathname; } catch (e) { /* as it is */ }
  return String(recordId == null ? "" : recordId) + "|" + where;
}
// A field as the plan sees it: its words, kind, whether it is required, and its choices.
function fieldSig(f) {
  const o = (f && f.options) || [];
  return [String((f && f.label) || "").trim().toLowerCase(), String((f && f.type) || ""), f && f.required ? 1 : 0,
          o.length, o.slice(0, 8).map((x) => String(typeof x === "string" ? x : (x && (x.label || x.text)) || "").toLowerCase()).join("|")].join("~");
}
function unplannedFields(seen, fields) {
  const s = seen || new Set();
  return (fields || []).filter((f) => !s.has(fieldSig(f)));
}
function notePlanned(seen, fields) {
  const s = new Set(seen || []);
  for (const f of fields || []) s.add(fieldSig(f));
  return s;
}

module.exports = { needsFinish, WHY_WORDS, planKey, fieldSig, unplannedFields, notePlanned, HAND_BACK };
