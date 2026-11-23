// Which jobs the board shows (ui/static/jobs_scope.js), under node: the default from the sign-up
// answer (Yes / Not sure / unanswered -> Sponsors only, No -> All jobs), the person's own pick
// winning, Auto-apply's "Visa sponsors only" chip following the answer until it is clicked, and
// the count line staying honest. tests/test_all_jobs_scope.py runs it.
"use strict";
const path = require("path");
const S = require(path.join(__dirname, "..", "..", "ui", "static", "jobs_scope.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const eq = (a, b, m) => assert(a === b, `${m}: got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);

// ---- the default
eq(S.defaultScope("yes"), "sponsors", "Yes");
eq(S.defaultScope("unsure"), "sponsors", "Not sure");
eq(S.defaultScope(""), "sponsors", "unanswered");
eq(S.defaultScope(undefined), "sponsors", "missing");
eq(S.defaultScope("no"), "all", "No");
eq(S.defaultScope("No"), "all", "No, any case");

// ---- a manual pick wins, a junk one does not
for (const a of ["yes", "no", "unsure", ""]) {
  eq(S.effectiveScope("all", a), "all", "manual all over " + a);
  eq(S.effectiveScope("sponsors", a), "sponsors", "manual sponsors over " + a);
  eq(S.effectiveScope("", a), S.defaultScope(a), "no manual pick, " + a);
  eq(S.effectiveScope("whatever", a), S.defaultScope(a), "junk pick, " + a);
}

// ---- Auto-apply's chip
eq(S.aaSponsorOn(null, "yes"), true, "chip follows Yes");
eq(S.aaSponsorOn(null, "unsure"), true, "chip follows Not sure");
eq(S.aaSponsorOn(null, "no"), false, "chip follows No");
eq(S.aaSponsorOn({ sponsor: false }, "yes"), true, "an untouched OFF from an older save follows the answer");
eq(S.aaSponsorOn({ sponsor: true }, "no"), true, "saved ON was a click: kept");
eq(S.aaSponsorOn({ sponsor: false, sponsorSet: true }, "yes"), false, "a clicked OFF is kept");

// ---- the count line
eq(S.countText(298956, 298956), "298,956 live roles", "nothing narrows it");
eq(S.countText(587, 298956), "587 of 298,956 roles", "a filter narrows it");
eq(S.countText(1, 1), "1 live role", "one role");

console.log("ok");
