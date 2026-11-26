// Did the site take the application? The model reads the page (2026-11-26).
//
// After an Autofill, main.js armSuccessWatch watches the Browser pane for ~30 minutes. The
// recipe's own markers (autofill_core.js detectSuccess) are checked first, as before. When they
// say nothing, this watch sends the page's VISIBLE TEXT to the engine once
// (/api/autofill/confirm-check), and the model decides from what the page means whether it
// confirms the application was sent. The owner's rule: the brain decides, never a word list.
//
// It costs a model call, so it asks only when it helps:
//   * only after the page changed: a navigation, or a form submit / Submit-type button press on
//     the page (single-page sites confirm without navigating);
//   * only for a page it has not asked about (the address and text are its key; the engine also
//     caches the answer);
//   * at most MAX_ASKS times per fill;
//   * it stops when the pane leaves the site: the first page on another site is still read (a
//     job board often hands off to the company's own "thank you" page), the second one ends it.
//
// Pure (no Electron): tests/js/test_applied_watch.js runs it under node.
"use strict";

const { EventEmitter } = require("events");

const MAX_ASKS = 12;

// THE one place that says "the site took this application" in the shell (2026-11-26). Every
// detector that confirms a submission (the recipe's markers or the model reading the page, both
// in main.js armSuccessWatch) calls announceSubmitted; anything that needs to know (the Tracker's
// auto Applied through the engine, a guided run that ends and saves its answers at submit time)
// listens with onSubmitted. One event, "application:submitted", with
// {recordId, url (the application's start page), pageUrl (the confirmation page), how
// ("marker" | "model"), matched (the engine found the record), at (ms)}.
const SUBMITTED = "application:submitted";
const bus = new EventEmitter();
bus.setMaxListeners(20);
function announceSubmitted(info) {
  const ev = Object.assign({ recordId: null, url: "", pageUrl: "", how: "", matched: false }, info || {}, { at: Date.now() });
  for (const l of bus.listeners(SUBMITTED)) {
    try { l(ev); } catch (e) { /* one listener's error never stops the others */ }
  }
  return ev;
}
function onSubmitted(cb) {
  bus.on(SUBMITTED, cb);
  return () => bus.removeListener(SUBMITTED, cb);
}
const SUBMIT_WINDOW_MS = 90 * 1000;       // a page change this soon after a submit is worth a look

// What the page shows, read in the page: its address, title, visible text, how many visible
// form fields it still has, and when a form was last submitted (noted by a listener this probe
// arms once per document, kept in sessionStorage so a full navigation still knows).
const PAGE_PROBE_JS = `(() => {
  if (!window.__sjFormSubmitWatch) {
    window.__sjFormSubmitWatch = true;
    const stamp = () => {
      const at = String(Date.now());
      window.__sjFormSubmitAt = at;
      try { sessionStorage.setItem("__sjFormSubmitAt", at); } catch (e) { /* storage off */ }
    };
    document.addEventListener("submit", stamp, true);
    document.addEventListener("click", (e) => {
      try {
        const b = e.target && e.target.closest && e.target.closest("button, input[type='submit']");
        if (b && (b.type === "submit" || (b.form && b.type !== "button" && b.type !== "reset"))) stamp();
      } catch (e2) { /* never in the page's way */ }
    }, true);
  }
  let at = Number(window.__sjFormSubmitAt || 0);
  try { at = Math.max(at, Number(sessionStorage.getItem("__sjFormSubmitAt") || 0)); } catch (e) { /* storage off */ }
  const text = String((document.body && document.body.innerText) || "").replace(/\\s+/g, " ").trim().slice(0, 4000);
  let fields = 0;
  for (const el of document.querySelectorAll("input:not([type='hidden']):not([type='submit']):not([type='button']), textarea, select")) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) fields++;
  }
  return { url: location.href, title: String(document.title || "").slice(0, 200), text, fields, submitAt: at };
})()`;

// "jobs.example.co.uk" -> "example.co.uk"; "boards.greenhouse.io" -> "greenhouse.io".
function siteOf(url) {
  let host = "";
  try { host = (new URL(url).hostname || "").toLowerCase(); } catch (e) { return ""; }
  const parts = host.replace(/^www\./, "").split(".").filter(Boolean);
  if (parts.length <= 2) return parts.join(".");
  const second = parts[parts.length - 2];
  const short = parts[parts.length - 1].length === 2 && /^(co|com|ac|org|net|gov|edu)$/.test(second);
  return parts.slice(short ? -3 : -2).join(".");
}

function pageKey(p) {
  let where = "";
  try { const u = new URL(p.url); where = u.host + u.pathname + u.hash; } catch (e) { where = String(p.url || ""); }
  return where + "\n" + String(p.text || "");
}

// o: {startUrl (the application's link), fillUrl (the page the fill ran on), recordId,
//     read() -> probe answer, ask(body) -> engine reply, now() -> ms}.
// tick({navigated}) -> null (nothing to do), {stop: true} (left the site, or too many asks), or
// {applied: engineReply} when the model said the page confirms the application.
function createModelWatch(o) {
  const now = o.now || (() => Date.now());
  const sites = new Set([siteOf(o.startUrl), siteOf(o.fillUrl)].filter(Boolean));
  const asked = new Set();
  const outside = new Set();
  let asks = 0;
  let changed = false;          // a navigation since the last look
  let lastSubmit = 0;
  let busy = false;
  return {
    navigated() { changed = true; },
    async tick() {
      if (busy) return null;
      busy = true;
      try {
        let p = null;
        try { p = await o.read(); } catch (e) { p = null; }
        if (!p || typeof p !== "object" || !p.url) return null;
        const site = siteOf(p.url);
        if (site && sites.size && !sites.has(site)) {
          outside.add(p.url.split("#")[0]);
          if (outside.size > 1) return { stop: true };
        }
        const submitAt = Number(p.submitAt) || 0;
        const fresh = submitAt > lastSubmit;
        const recent = submitAt && now() - submitAt <= SUBMIT_WINDOW_MS;
        if (!changed && !fresh && !recent) return null;     // nothing happened on the page
        const key = pageKey(p);
        if (asked.has(key)) { changed = false; return null; }
        if (String(p.text || "").length < 20) return null;  // still loading
        if (asks >= MAX_ASKS) return { stop: true };
        asked.add(key);
        asks++;
        changed = false;
        lastSubmit = Math.max(lastSubmit, submitAt);
        let res = null;
        try {
          res = await o.ask({ url: o.startUrl, page_url: p.url, record_id: o.recordId == null ? null : o.recordId,
                              title: p.title || "", text: p.text, fields: Number(p.fields) || 0 });
        } catch (e) { res = null; }
        if (res && res.verdict === "yes") return { applied: res };
        return null;
      } finally {
        busy = false;
      }
    },
  };
}

module.exports = { PAGE_PROBE_JS, createModelWatch, siteOf, MAX_ASKS, SUBMITTED, announceSubmitted, onSubmitted };
