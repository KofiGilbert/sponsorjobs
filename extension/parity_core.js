// P6 (FrogHire parity): the PURE decision logic shared by the content script and the popup,
// separated out so it can be unit-tested in node with no DOM (tests/test_extension_parity.py).
// It decides WHAT to show from an API response; content.js does the DOM. Honest by construction:
// visa claims respect role_in_us gating, and the match % is labeled a keyword estimate, never a
// real ATS/employer score.
(function () {
  "use strict";
  if (typeof window !== "undefined" && window.TailorParity) return;

  // A sponsor lookup response -> a badge decision (data, not HTML), honoring the same role_in_us
  // gating the single-posting badge already uses: never claim a visa applies to a role we know is
  // abroad, or whose location we simply couldn't read.
  function sponsorBadge(data) {
    if (!data || data.ok === false) {
      if (data && data.error === "cant_reach_app")
        return { kind: "off", codes: [], text: "Visa data offline" };   // no index and no app
      return { kind: "error", codes: [], text: "" };
    }
    if (!data.matched) return { kind: "none", codes: [], text: "No sponsor record" };
    if (data.sponsor_employer && data.role_in_us === false)
      return { kind: "sponsor_not_role", codes: [], text: "Sponsors in the US, not this role" };
    if (data.sponsor_employer && data.role_in_us == null)
      return { kind: "sponsor_unclear", codes: [], text: "Sponsors US visas, location unclear" };
    const codes = (data.visa || []).map((v) => v.code);
    return { kind: "visa", codes, visa: data.visa || [], text: codes.join(" ") };
  }

  // A coverage response -> the match pill. No profile -> a clear "add your profile" prompt, never a
  // fabricated number. No score (e.g. empty JD) -> nothing.
  // No app installed (or not running): the match score lives in the app, so offer it rather than
  // show nothing or an error. The visa badges no longer need the app (sponsor_core.js).
  function matchBadge(cov) {
    if (cov && cov.ok === false && cov.error === "cant_reach_app")
      return { needApp: true, text: "Get the free app for your match score" };
    if (!cov || cov.ok === false) return null;
    if (cov.has_profile === false) return { needProfile: true, text: "Add your profile to see match" };
    if (cov.score == null) return null;
    const pct = Math.max(0, Math.min(100, Math.round(cov.score)));
    const cls = pct >= 70 ? "high" : pct >= 40 ? "mid" : "low";
    return {
      pct, cls, text: pct + "% match",
      skills: `${cov.skills_covered || 0}/${cov.skills_total || 0} skills`,
      note: cov.note || "Keyword coverage estimate, not a real ATS score.",
    };
  }

  // A coverage response -> the highlight plan for the open posting: which JD skills are Required
  // vs Optional, and which the profile covers vs is missing.
  function highlightTerms(cov) {
    const skills = (cov && cov.skills) || [];
    return {
      required: skills.filter((s) => s.required),
      optional: skills.filter((s) => !s.required),
      covered: skills.filter((s) => s.covered).map((s) => s.term),
      missing: skills.filter((s) => !s.covered).map((s) => s.term),
    };
  }

  // Does the employer have ANY sponsorship record (H-1B approvals, certified PERM cases, or
  // E-Verify enrollment)? true / false from a lookup answer; null when there is no answer yet
  // (worker asleep, data offline), which the "Sponsored only" toggle treats as "keep visible".
  function hasSponsorRecord(data) {
    if (!data || data.ok === false) return null;
    if (!data.matched) return false;
    const p = data.profile;
    if (p) return !!(p.h1b_approvals > 0 || p.perm_certs > 0 || p.e_verify);
    return (data.visa || []).length > 0 || !!data.sponsor_employer;   // an older app without `profile`
  }

  // Client-side list filtering. `card` = { codes:[H-1B,...], role_in_us, matchPct:number|null,
  // record:true|false|null }. `filter` = { chips:[H-1B,...], minMatch:number, sponsoredOnly:bool }.
  // Honest gating: sponsorship chips exclude a role we KNOW is abroad; the match slider only
  // filters cards we actually have a score for; "Sponsored only" hides a card only once its
  // lookup has answered "no record" (record === false), never one still waiting.
  function cardPasses(card, filter) {
    card = card || {};
    filter = filter || {};
    if (filter.sponsoredOnly && card.record === false) return false;
    const chips = filter.chips || [];
    if (chips.length) {
      if (card.role_in_us === false) return false;          // clearly not US -> visa chips exclude
      const codes = (card.codes || []).map((c) => String(c).toUpperCase());
      if (!chips.every((c) => codes.includes(String(c).toUpperCase()))) return false;
    }
    const min = Number(filter.minMatch) || 0;
    if (min > 0 && typeof card.matchPct === "number" && card.matchPct < min) return false;
    return true;
  }

  // The "Hiding N of M" count for a results list. `cards` = each card's signals (null while its
  // lookup is still out). A pending card is never counted as hidden.
  function listSummary(cards, filter) {
    const out = { total: 0, hidden: 0, pending: 0 };
    for (const c of cards || []) {
      out.total++;
      if (!c) { out.pending++; continue; }
      if (!cardPasses(c, filter)) out.hidden++;
    }
    return out;
  }

  const api = { sponsorBadge, matchBadge, highlightTerms, cardPasses, hasSponsorRecord, listSummary };
  if (typeof window !== "undefined") window.TailorParity = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
