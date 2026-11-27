// shell-electron/applied_watch.js under node: the model reads the Browser pane's page only when
// it changed after a navigation or a form submit, once per page, at most MAX_ASKS times, and
// stops once the pane has left the site. tests/test_tracker_lifecycle.py runs it. Made-up sites.
"use strict";
const path = require("path");
const W = require(path.join(__dirname, "..", "..", "shell-electron", "applied_watch.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const eq = (a, b, m) => assert(JSON.stringify(a) === JSON.stringify(b), `${m}: got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);

eq(W.siteOf("https://boards.greenhouse.io/x/jobs/1"), "greenhouse.io", "a board host is its site");
eq(W.siteOf("https://careers.example.co.uk/a"), "example.co.uk", "a two-part country ending");
eq(W.siteOf("not a url"), "", "nothing for a bad address");
assert(/innerText/.test(W.PAGE_PROBE_JS) && /submit/.test(W.PAGE_PROBE_JS), "the probe reads the visible text and notes a submit");

(async () => {
  let page = { url: "https://jobs.example.com/apply", title: "Apply", text: "First name Last name Email Submit", fields: 6, submitAt: 0 };
  const asked = [];
  let answer = { verdict: "no" };
  let clock = 1_000_000;
  const w = W.createModelWatch({
    startUrl: "https://jobs.example.com/job/1", fillUrl: "https://jobs.example.com/apply", recordId: 7,
    read: async () => page, ask: async (b) => { asked.push(b); return answer; }, now: () => clock,
  });

  eq(await w.tick(), null, "nothing happened on the page: no call");
  eq(asked.length, 0, "no model call without a change");

  w.navigated();
  eq(await w.tick(), null, "the form after a navigation: read once, it says no");
  eq(asked.length, 1, "one call");
  eq([asked[0].record_id, asked[0].page_url, asked[0].fields], [7, "https://jobs.example.com/apply", 6], "what is sent");
  w.navigated();
  await w.tick();
  eq(asked.length, 1, "the same page is never asked about twice");

  // a single-page site: the person presses Submit, the text changes without a navigation
  page = { ...page, text: "Thanks, your application is in.", fields: 0, submitAt: clock - 2000 };
  answer = { verdict: "yes", matched: true, record_id: 7 };
  const hit = await w.tick();
  assert(hit && hit.applied && hit.applied.matched, "a submit then a changed page: the model's yes marks it applied");
  eq(asked.length, 2, "a second call, for the new page");

  // leaving the site: the first page elsewhere is still read (a hand-off to a thank-you page), the
  // second one ends the watch
  const w2asked = [];
  let p2 = { url: "https://jobs.example.com/apply", text: "Form with plenty of fields here", fields: 5, submitAt: 0 };
  const w2 = W.createModelWatch({ startUrl: "https://jobs.example.com/job/2", fillUrl: "https://jobs.example.com/apply",
    read: async () => p2, ask: async (b) => { w2asked.push(b); return { verdict: "unsure" }; }, now: () => clock });
  p2 = { url: "https://www.acme-careers.test/thanks", text: "Some page on another site entirely", fields: 0, submitAt: 0 };
  w2.navigated();
  eq(await w2.tick(), null, "the first page on another site is read");
  eq(w2asked.length, 1, "and asked about");
  p2 = { url: "https://news.other.test/", text: "The person browsed on to something else", fields: 0, submitAt: 0 };
  w2.navigated();
  eq(await w2.tick(), { stop: true }, "the second page elsewhere ends the watch");
  eq(w2asked.length, 1, "without a call");

  // a cap on calls per fill
  let n = 0;
  const w3 = W.createModelWatch({ startUrl: "https://jobs.example.com/j", fillUrl: "https://jobs.example.com/j",
    read: async () => ({ url: "https://jobs.example.com/j#step" + n, text: "Step number " + n + " of a long form", fields: 4, submitAt: 0 }),
    ask: async () => ({ verdict: "no" }), now: () => clock });
  let stopped = null;
  for (let i = 0; i < W.MAX_ASKS + 3 && !stopped; i++) { n = i; w3.navigated(); const v = await w3.tick(); if (v && v.stop) stopped = i; }
  eq(stopped, W.MAX_ASKS, "after MAX_ASKS calls the watch stops asking");

  // a page still loading (no text yet) waits for the next look
  const w4asked = [];
  let p4 = { url: "https://jobs.example.com/a", text: "", fields: 0, submitAt: 0 };
  const w4 = W.createModelWatch({ startUrl: "https://jobs.example.com/a", fillUrl: "https://jobs.example.com/a",
    read: async () => p4, ask: async (b) => { w4asked.push(b); return { verdict: "no" }; }, now: () => clock });
  w4.navigated();
  await w4.tick();
  eq(w4asked.length, 0, "an empty page is not sent");
  p4 = { ...p4, text: "Loaded now with enough words" };
  await w4.tick();
  eq(w4asked.length, 1, "it is read once it has loaded");


  // (audit cases 12 and 26) right after the person's Submit the page shows an error: the bar is
  // told; a page that says the job closed is said at any time; neither marks anything sent
  {
    let p5 = { url: "https://jobs.example.com/f", text: "Bitte korrigieren Sie die markierten Felder", fields: 9, submitAt: 0 };
    let v5 = "error";
    const w5 = W.createModelWatch({ startUrl: "https://jobs.example.com/f", fillUrl: "https://jobs.example.com/f",
      read: async () => p5, ask: async () => ({ verdict: v5 }), now: () => clock });
    w5.navigated();
    eq(await w5.tick(), null, "an error read on a page the person did not submit is not a refusal");
    p5 = { ...p5, text: p5.text + " (2)", submitAt: clock - 1000 };
    eq(await w5.tick(), { problem: "error", res: { verdict: "error" } }, "after their Submit: the site did not take it");
    v5 = "closed";
    p5 = { url: "https://jobs.example.com/f2", text: "Cette offre n'est plus disponible", fields: 0, submitAt: 0 };
    w5.navigated();
    eq(await w5.tick(), { problem: "closed", res: { verdict: "closed" } }, "the job closed, in any language");
  }

  // the one shared event: every listener hears a confirmed submission, and can stop listening
  const heard = [];
  W.onSubmitted(() => { throw new Error("a bad listener"); });
  const off = W.onSubmitted((e) => heard.push(e));
  const ev = W.announceSubmitted({ recordId: 7, url: "https://jobs.example.com/job/1", pageUrl: "https://jobs.example.com/done", how: "model", matched: true });
  eq(W.SUBMITTED, "application:submitted", "the event's name");
  eq([heard.length, heard[0].recordId, heard[0].how, heard[0].matched], [1, 7, "model", true], "a listener hears it with the record id");
  assert(ev.at > 0, "with when");
  off();
  W.announceSubmitted({ recordId: 8 });
  eq(heard.length, 1, "a listener that stopped hears nothing more");

  console.log("ok applied watch");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
