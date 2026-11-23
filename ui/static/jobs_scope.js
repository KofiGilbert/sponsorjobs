// Which jobs the board shows (2026-11-20): "Sponsors only" or "All jobs". The pure parts, so
// tests/js/test_jobs_scope.js drives them under node; app.js paints the controls. The same rules
// live server-side in sourcing/filters.py (default_scope, effective_scope) for Home, the saved
// search counts and the Autopilot; the two are kept identical.
//
// The answer is the sign-up question "Do you need visa sponsorship to work in the US?", read
// from the work-authorization profile: "yes", "no", "unsure", or "" (not answered yet).
(function (root) {
  "use strict";

  const SCOPES = ["sponsors", "all"];

  /** The scope an answer implies: All jobs for No; Sponsors only for Yes, Not sure and no answer. */
  function defaultScope(answer) {
    return String(answer || "").trim().toLowerCase() === "no" ? "all" : "sponsors";
  }

  /** The person's own choice on the Jobs screen when they made one, else the answer's default. */
  function effectiveScope(manual, answer) {
    const m = String(manual || "").trim().toLowerCase();
    return SCOPES.includes(m) ? m : defaultScope(answer);
  }

  /** Auto-apply's "Visa sponsors only" chip. `stored` is the page's saved focus (or null): a
   *  chip the person clicked (sponsorSet), or one saved ON (it starts off, so ON was a click), is
   *  their choice; otherwise it follows the answer. */
  function aaSponsorOn(stored, answer) {
    if (stored && (stored.sponsorSet || stored.sponsor === true)) return !!stored.sponsor;
    return defaultScope(answer) === "sponsors";
  }

  /** The board's count line: "N live roles" when nothing narrows it, else "N of M roles", where M
   *  is every live job, so Sponsors only reads e.g. "298,956 of 341,210 roles". */
  function countText(shown, total, fmt) {
    const f = fmt || ((n) => Number(n || 0).toLocaleString("en-US"));
    const s = total === 1 ? "" : "s";
    return shown === total ? `${f(total)} live role${s}` : `${f(shown)} of ${f(total)} role${s}`;
  }

  const api = { SCOPES, defaultScope, effectiveScope, aaSponsorOn, countText };
  root.SJJobsScope = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
