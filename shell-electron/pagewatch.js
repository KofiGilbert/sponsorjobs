// Pure helpers for multi-page application forms (main.js armPageWatch). Kept apart from the
// Electron code so the decisions are unit-tested on real pages (tests/test_form_widgets.py).
"use strict";

// Is this form a different page from the one last filled? New questions, AND most of what is
// on screen now was not there before. A conditional question appearing under an answer is the
// SAME page, and must not trigger a refill of what the person may have cleared on purpose.
function isNewPage(prevLabels, sig) {
  if (!sig || !Array.isArray(sig.labels)) return false;
  const prev = prevLabels instanceof Set ? prevLabels : new Set(prevLabels || []);
  const now = sig.labels;
  const fresh = now.filter((l) => !prev.has(l));
  if (!fresh.length) return false;
  const kept = now.length - fresh.length;
  return kept / Math.max(1, now.length) < 0.5;
}

function pageMessage(page, filled, left) {
  let m = "Page " + page + " filled: " + filled + " field" + (filled === 1 ? "" : "s") + ".";
  if (left && left.length) m += " " + left.length + " left for you.";
  return m;
}

function wallMessage(page) {
  return "Page " + page + " asks you to sign in or create an account. Do that yourself, "
       + "then I'll fill the next page.";
}

module.exports = { isNewPage, pageMessage, wallMessage };
