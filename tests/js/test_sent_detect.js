// shell-electron/sent_detect.js under node: each kind of evidence on its own, the way they
// combine, once per record, the click they depend on, and the one ask when the person moves on
// with no evidence. tests/test_sent_detection.py runs it. Made-up sites and ids.
"use strict";
const fs = require("fs");
const path = require("path");
const D = require(path.join(__dirname, "..", "..", "shell-electron", "sent_detect.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const eq = (a, b, m) => assert(JSON.stringify(a) === JSON.stringify(b), `${m}: got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);

// ------------------------------------------------------------------ the network classifier
const NET = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "fixtures", "sent", "network_patterns.json"), "utf8"));
for (const r of NET.sent) {
  const c = D.classifyRequest(r, new Set());
  assert(c && c.kind === "network-ats" && c.ats === r.ats, `${r.ats} submit is recognised: ${JSON.stringify(c)}`);
  assert(!/[?]/.test(c.path) && !/7000001|0000-4000/.test(c.path), `${r.ats}: only a path shape is kept, never the query or ids: ${c.path}`);
  eq(Object.keys(c).sort(), ["ats", "host", "kind", "method", "path", "status"], `${r.ats}: nothing but method, host, path shape, status`);
}
for (const r of NET.not_sent) {
  const sites = new Set(["example.com", "ashbyhq.com"]);
  const c = D.classifyRequest(r, sites);
  if (r.at_most === "network") assert(!c || c.kind === "network", `${r.why}: never a known submit`);
  else assert(c === null, `${r.why}: not an application being sent, got ${JSON.stringify(c)}`);
}
// a generic form post to the application's own site, and only to it
eq(D.classifyRequest({ method: "POST", url: "https://careers.example.com/apply/submit-form", status: 200, type: "Document" }, new Set(["example.com"])).kind,
   "network", "a form post to the application's own site");
eq(D.classifyRequest({ method: "POST", url: "https://careers.example.com/apply/submit-form", status: 200 }, new Set(["other.com"])),
   null, "the same post to a site the application is not on");
eq(D.classifyRequest({ method: "POST", url: "https://careers.example.com/a.png", status: 200, type: "Image" }, new Set(["example.com"])),
   null, "an image is not a form post");
eq(D.pathShape("/acme/jobs/7000001"), "/acme/jobs/:n", "numbers blanked");
eq(D.pathShape("/x/00000000-0000-4000-8000-000000000006/apply"), "/x/:id/apply", "ids blanked");

// ------------------------------------------------------------------------- the combiner
let clock = 10_000_000;
const fired = [], asked = [];
const mk = () => D.createDetector({ now: () => clock, onFire: (r) => fired.push(r), onAsk: (a) => asked.push(a) });
const GH = "https://job-boards.greenhouse.io/acme/jobs/7000001";

// (a) the thank-you page alone is enough: the recipe's marker, or the model's reading
let d = mk();
d.arm({ recordId: 1, startUrl: GH, fillUrl: GH });
d.signal({ kind: "page", how: "marker", pageUrl: GH + "/confirmation" });
eq(fired.length, 1, "a marker fires");
eq([fired[0].recordId, fired[0].how, fired[0].label], [1, "marker", "thank-you page"], "what fired");
d.signal({ kind: "page", how: "model", pageUrl: GH + "/confirmation" });
eq(fired.length, 1, "once per record: a second signal adds nothing");
d.arm({ recordId: 1, startUrl: GH, fillUrl: GH });
d.signal({ kind: "page", how: "marker" });
eq(fired.length, 1, "the same record re-armed in this session never fires twice");
d.forget(1);
d.arm({ recordId: 1, startUrl: GH, fillUrl: GH });
d.signal({ kind: "page", how: "model", res: { matched: true, record_id: 1 } });
eq([fired.length, fired[1].how, !!fired[1].res], [2, "model", true], "Fill again: a new send counts, the model's own reply rides along");

// a confirmation inside a frame is page evidence too
d = mk();
d.arm({ recordId: 2, startUrl: "https://www.example-careers.com/job/9", fillUrl: "https://www.example-careers.com/job/9" });
d.signal({ kind: "page", how: "marker", frame: true, pageUrl: "https://boards.greenhouse.io/embed/job_app?for=x" });
eq(fired[fired.length - 1].recordId, 2, "a frame's thank-you page fires");

// (b) + (c) the Instacart case: the form sits in a Greenhouse frame; after the person's click it
// posts to Greenhouse, then the frame resets to a blank form. Neither alone fires, together they do.
const INSTA = "https://instacart.careers/job/?gh_jid=7000007";
const FRAME = "https://boards.greenhouse.io/embed/job_app?for=instacart&token=7000007";
d = mk();
let n = fired.length;
d.arm({ recordId: 3, startUrl: INSTA, fillUrl: INSTA });
d.signal({ kind: "network", method: "POST", url: FRAME.replace("?", "/x?"), status: 200 });
eq(fired.length, n, "a post before any click of the person's is not evidence");
d.signal({ kind: "probe", frame: true, frameUrl: FRAME, url: FRAME, clickAt: clock - 1000, filledAtClick: 14, filledNow: 14, fieldsNow: 20 });
d.signal({ kind: "network", method: "POST", url: "https://boards.greenhouse.io/embed/job_app?for=instacart&token=7000007", status: 302 });
eq(fired.length, n, "the Greenhouse post alone (0.8) does not fire: a 200 with errors keeps the form");
assert(d.state().evidence.some((e) => e.label === "sent to Greenhouse"), "but it is kept as evidence");
clock += 3000;
d.signal({ kind: "probe", frame: true, frameUrl: FRAME, url: FRAME, clickAt: clock - 4000, filledAtClick: 14, filledNow: 0, fieldsNow: 20 });
eq(fired.length, n + 1, "post + the frame reset to blank fires");
const last = fired[fired.length - 1];
eq(last.how, "combined", "two kinds together");
eq(last.evidence.map((e) => e.kind), ["network-ats", "form-reset"], "the evidence, strongest first");
assert(last.confidence >= D.FIRE_AT, "over the bar");

// a reset alone never fires, and a generic post + reset does
d = mk(); n = fired.length;
d.arm({ recordId: 4, startUrl: "https://jobs.example.com/apply/1", fillUrl: "https://jobs.example.com/apply/1" });
d.signal({ kind: "probe", url: "https://jobs.example.com/apply/1", clickAt: clock - 500, filledAtClick: 9, filledNow: 9, fieldsNow: 12 });
d.signal({ kind: "probe", url: "https://jobs.example.com/apply/1", clickAt: clock - 500, filledAtClick: 9, filledNow: 0, fieldsNow: 12 });
eq(fired.length, n, "a cleared form alone is not proof (a site may clear on an error)");
d.signal({ kind: "network", method: "POST", url: "https://jobs.example.com/api/applications", status: 201, type: "Fetch" });
eq(fired.length, n + 1, "a form post to the site + the cleared form fires");
eq(fired[fired.length - 1].label, "sent to the site", "labelled by the strongest");

// a reset needs a form that was really filled, and the click
d = mk(); n = fired.length;
d.arm({ recordId: 5, startUrl: "https://jobs.example.com/a", fillUrl: "https://jobs.example.com/a" });
d.signal({ kind: "probe", url: "https://jobs.example.com/a", clickAt: clock - 100, filledAtClick: 1, filledNow: 0, fieldsNow: 3 });
eq(d.state().evidence.length, 0, "a job page's Apply button (nothing filled) is no reset");
d.signal({ kind: "form-reset", filledBefore: 8, filledAfter: 0 });
eq(d.state().evidence.length, 1, "the frame walk's own reset signal counts");

// a new click starts the click-bound evidence over: an earlier failed post never adds to a later one
d = mk(); n = fired.length;
d.arm({ recordId: 6, startUrl: GH, fillUrl: GH });
d.signal({ kind: "submit-click", at: clock - 100, filled: 12 });
d.signal({ kind: "network", method: "POST", url: GH, status: 200 });
clock += 60_000;
d.signal({ kind: "submit-click", at: clock, filled: 12 });
eq(d.state().evidence.length, 0, "the second click drops the first click's post");
d.signal({ kind: "probe", url: GH, clickAt: clock, filledAtClick: 12, filledNow: 0, fieldsNow: 0 });
eq(fired.length, n, "a reset after the second click, without its own post, does not fire");
// and evidence too long after the click is not about it
clock += D.CLICK_WINDOW_MS + 1000;
d.signal({ kind: "network", method: "POST", url: GH, status: 200 });
eq(fired.length, n, "a post minutes after the click is not about it");

// (d) a window the site opens right after the click is a hint, never enough alone
d = mk(); n = fired.length;
d.arm({ recordId: 7, startUrl: GH, fillUrl: GH });
d.signal({ kind: "submit-click", at: clock, filled: 10 });
d.signal({ kind: "popup", url: "https://job-boards.greenhouse.io/acme/thanks" });
eq([fired.length, d.state().evidence[0].kind], [n, "popup"], "a popup is kept, not fired on");

// (f) said yes
d.signal({ kind: "said-yes" });
eq(fired[fired.length - 1].how, "said-yes", "the person's yes decides, whatever hints came before");
eq(fired[fired.length - 1].label, "you said yes", "the strongest is the person's own word");

// a signal for another record never lands on this one
d = mk(); n = fired.length;
d.arm({ recordId: 8, startUrl: GH, fillUrl: GH });
d.signal({ kind: "page", how: "marker", recordId: 9 });
eq(fired.length, n, "another record's signal is ignored");

// ------------------------------------------------------------------- the last-resort ask
d = mk();
d.arm({ recordId: 10, startUrl: GH, fillUrl: GH });
eq(d.leave("timeout"), null, "the watch timing out lets go without asking (the Tracker asks later)");
d.arm({ recordId: 11, startUrl: GH, fillUrl: GH });
const a1 = d.leave("left-site");
eq([a1.recordId, a1.reason, a1.clicked], [11, "left-site", false], "leaving the site with no evidence asks");
eq(d.leave("closed"), null, "asked once: the watch is gone");
d.arm({ recordId: 12, startUrl: GH, fillUrl: GH });
d.signal({ kind: "submit-click", at: clock, filled: 10 });
d.arm({ recordId: 13, startUrl: GH, fillUrl: GH });
eq([asked[asked.length - 1].recordId, asked[asked.length - 1].reason, asked[asked.length - 1].clicked], [12, "switched", true],
   "starting another job asks about the one left behind, saying the person did press Submit");
d.arm({ recordId: 13, startUrl: GH, fillUrl: GH + "/page2" });
eq(asked[asked.length - 1].recordId, 12, "the next page of the same application is not a switch");
d.signal({ kind: "page", how: "marker" });
eq(d.leave("closed"), null, "a sent application is never asked about");
d.arm({ recordId: 14, startUrl: GH, fillUrl: GH });
eq(d.leave("agent"), null, "the agent recorded the send itself: no ask");

// ------------------------------------------------------------------- the page probe
assert(/isTrusted/.test(D.CLICK_PROBE_JS), "only the person's own click counts");
assert(!/\.value\s*\)\s*;?\s*return|JSON\.stringify\(\{[^}]*value/.test(D.CLICK_PROBE_JS), "the probe keeps counts, never a value");
assert(/sessionStorage/.test(D.CLICK_PROBE_JS), "the click survives a navigation or a frame reset");

// the shared detector is the one other shell code (the frame walk) feeds
D.configure({ onFire: () => {}, onAsk: () => {} });
assert(typeof D.signal === "function" && D.shared() === D.shared(), "one shared detector");

console.log("sent_detect ok");
