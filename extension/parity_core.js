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
        return { kind: "off", codes: [], text: "Start SponsorJobs for visa badges" };
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
  function matchBadge(cov) {
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

  // Client-side list filtering. `card` = { codes:[H-1B,...], role_in_us, matchPct:number|null }.
  // `filter` = { chips:[H-1B,...], minMatch:number }. Honest gating: sponsorship chips exclude a
  // role we KNOW is abroad; the match slider only filters cards we actually have a score for.
  function cardPasses(card, filter) {
    card = card || {};
    filter = filter || {};
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

  const api = { sponsorBadge, matchBadge, highlightTerms, cardPasses };
  if (typeof window !== "undefined") window.TailorParity = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
