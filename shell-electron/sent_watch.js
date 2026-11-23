// Did the site take the application? Read after the shell's own Send AND while a job waits at
// Ready to send, because the person may press the site's own Submit themselves.
//
// Measured on a real Workday tenant (2026-11-19): the person pressed the site's Submit during the
// Ready to send bar; Workday went to .../jobTasks/completed/application and showed a modal
// "Application Submitted ... You have no more tasks. Return to Home" over Candidate Home
// ("Welcome, <name>", "My Tasks", "My Applications"). The record stayed approved and the bar kept
// saying Ready to send, because nothing was watching after the agent stopped, and the Workday
// recipe knew none of those words.
//
// A page is counted as sent on a page signal only, never on an assumption:
//   * the recipe's success markers (autofill_core.js detectSuccess: words once the form is gone,
//     a selector, a URL, words in an open dialog);
//   * Workday's completed-application address on a Workday host;
//   * Candidate Home (My Applications) on the same host, with no Submit button left on the page,
//     right after a press of a Submit button on that site during this watch.
//
// Pure (no Electron): tests/js/test_after_submit.js runs it under node.
"use strict";

const HOME_AFTER_SUBMIT_MS = 5 * 60 * 1000;     // Candidate Home counts this long after a Submit press
const WORKDAY_HOST_RE = /(^|\.)(myworkdayjobs|myworkdaysite)\.com$/i;
const WORKDAY_DONE_RE = /\/jobTasks\/completed\/application(\/|$)/i;

// What the page says, read in the page. It also arms, once per document, a note of the moment a
// button whose words say Submit is pressed (kept in sessionStorage too, so a full navigation to
// Candidate Home on the same site still knows). Returns
// {url, success, phrase, home, submitShown, clickedAt}.
const SENT_PROBE_JS = `(() => {
  const SUBMIT_WORDS = /^(submit|submit (my |your )?application|send (my )?application)\\b/i;
  const words = (el) => String((el && (el.innerText || el.value || el.getAttribute("aria-label"))) || "").replace(/\\s+/g, " ").trim();
  if (!window.__sjSubmitWatch) {
    window.__sjSubmitWatch = true;
    const note = (e) => {
      try {
        const t = e.target && e.target.closest && e.target.closest("button, [role='button'], input[type='submit'], input[type='button']");
        if (!t || !SUBMIT_WORDS.test(words(t))) return;
        const at = String(Date.now());
        window.__sjSubmitAt = at;
        try { sessionStorage.setItem("__sjSubmitAt", at); } catch (e2) { /* storage off */ }
      } catch (e3) { /* never in the page's way */ }
    };
    document.addEventListener("pointerdown", note, true);
    document.addEventListener("click", note, true);
    document.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") note(e); }, true);
  }
  let at = Number(window.__sjSubmitAt || 0);
  try { at = Math.max(at, Number(sessionStorage.getItem("__sjSubmitAt") || 0)); } catch (e) { /* storage off */ }
  let success = false;
  try { success = !!(window.TailorAutofill && window.TailorAutofill.detectSuccess()); } catch (e) { success = false; }
  let phrase = "";
  for (const d of document.querySelectorAll("[role='dialog'], [role='alertdialog'], dialog[open], [aria-modal='true']")) {
    const t = words(d);
    if (/submitted|received|thank you/i.test(t)) { phrase = t.slice(0, 160); break; }
  }
  const text = String((document.body && document.body.innerText) || "").slice(0, 8000);
  const home = /\\bmy applications\\b/i.test(text) && /candidate home|\\bmy tasks\\b|\\bwelcome\\b/i.test(text);
  let submitShown = false;
  for (const b of document.querySelectorAll("button, [role='button'], input[type='submit']")) {
    const r = b.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && SUBMIT_WORDS.test(words(b))) { submitShown = true; break; }
  }
  return { url: location.href, success, phrase, home, submitShown, clickedAt: at };
})()`;

const hostOf = (url) => { try { return (new URL(url).hostname || "").toLowerCase().replace(/^www\./, ""); } catch (e) { return ""; } };

// p: the probe's answer. o: {host (the Ready to send page's host), since (when the watch began,
// ms), now}. {how: "marker"|"url"|"home", phrase} when the site took it, else null.
function sentVerdict(p, o) {
  if (!p || typeof p !== "object") return null;
  o = o || {};
  const phrase = String(p.phrase || "").slice(0, 160);
  if (p.success) return { how: "marker", phrase };
  const url = String(p.url || "");
  const host = hostOf(url);
  let path = "";
  try { path = new URL(url).pathname; } catch (e) { path = ""; }
  if (WORKDAY_HOST_RE.test(host) && WORKDAY_DONE_RE.test(path)) return { how: "url", phrase };
  const at = Number(p.clickedAt) || 0;
  const now = Number(o.now) || Date.now();
  const since = Number(o.since) || 0;
  const readyHost = String(o.host || "").toLowerCase().replace(/^www\./, "");
  if (p.home && !p.submitShown && host && host === readyHost
      && at && at >= since - 1000 && now - at <= HOME_AFTER_SUBMIT_MS) {
    return { how: "home", phrase };
  }
  return null;
}

module.exports = { SENT_PROBE_JS, sentVerdict, HOME_AFTER_SUBMIT_MS };
