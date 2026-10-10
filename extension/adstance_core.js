// What a job ad SAYS about visa sponsorship, read from its own text. A line-for-line port of
// sourcing/adstance.py (the app's reader), so the extension and the app never disagree about the
// same ad; tests/test_extension_adstance.py runs both on the same sentences.
//
// Why this matters (Kofi, 2026-10-10): on a U.S. Bank posting the employer badges showed H-1B and
// PERM history while the ad itself said "This position is not eligible for visa sponsorship". The
// employer's record and the role's stance are different facts; a student needs both, and the
// role's own words decide. A NO anywhere outranks a YES anywhere. E-Verify is not sponsorship.
(function (root) {
  const OFFERED = "offered", NOT_OFFERED = "not_offered", UNKNOWN = "unknown";
  const SENT = /[^.\n!?•]+/g;
  const ABOUT = /\bsponsor\w*|\bh-?1b\b/i;
  const CTX = /visa|immigra|work authori[sz]|h-?1b|\bopt\b|\bcpt\b|green card|employment eligib|right to work|lawful|employer of record|work permit|legally authori[sz]/i;
  const NO = [
    /\b(not|never|unable|cannot|can ?not|can't|won't|will not|does ?not|doesn't|do ?not|don't|no longer|isn't|is not|are not|aren't)\b[^.\n]{0,60}?\b(offer|provid|sponsor|support|able|eligible|available|consider)/i,
    /without\s+(requiring\s+|the\s+need\s+for\s+|needing\s+)?(any\s+)?(current\s+or\s+future\s+)?(employer[- ])?(visa\s+|immigration\s+)?sponsorship/i,
    /sponsorship\s+(is\s+)?(not|un)\s*(available|offered|provided|possible)/i,
    /\bno\s+(visa\s+|immigration\s+|employment[- ]based\s+)?sponsorship/i,
    /do\s+not\s+apply[^.\n]{0,80}sponsor/i,
    /(must|should)\s+(be|have|possess|hold)[^.\n]{0,80}(authori[sz]ation|authori[sz]ed|eligib)[^.\n]{0,80}(not|without)[^.\n]{0,40}sponsor/i,
    /(will|would)\s+(now\s+or\s+in\s+the\s+future\s+)?(require|need)\s+(visa\s+|immigration\s+)?sponsorship[^.\n]{0,40}(not|ineligible|unable|cannot)/i,
  ];
  const YES = [
    /sponsorship\s+(is\s+|may\s+be\s+|will\s+be\s+)?(available|offered|provided|possible|considered)/i,
    /(eligible|open)\s+for\s+(visa\s+|immigration\s+|work\s+)?sponsorship/i,
    /\b(we|company|employer)\s+(do|does|will|can|may|are\s+able\s+to|are\s+willing\s+to|are\s+happy\s+to)\s+(offer|provide|sponsor|support)/i,
    /(offer|offers|offering|provide|provides|providing)\s+(visa\s+|immigration\s+|h-?1b\s+|work\s+)?(visa\s+)?sponsorship/i,
    /sponsorship\s+(for|of)\s+(work\s+)?visas?/i,
    /\bsponsor\s+(work\s+)?visas?\b/i,
    /(will|can|may)\s+sponsor\b/i,
    /h-?1b\s+(transfer|sponsorship)\s+(welcome|accepted|available|ok)/i,
  ];

  function* sentences(text) {
    for (const m of String(text || "").matchAll(SENT)) {
      const s = m[0].split(/\s+/).filter(Boolean).join(" ");
      if (s) yield s;
    }
  }

  function adStance(text) {
    let yes = null;
    for (const s of sentences(text)) {
      if (!(ABOUT.test(s) && CTX.test(s))) continue;
      if (NO.some(p => p.test(s))) return { stance: NOT_OFFERED, sentence: s.slice(0, 300) };
      if (yes === null && YES.some(p => p.test(s))) yes = s.slice(0, 300);
    }
    return yes ? { stance: OFFERED, sentence: yes } : { stance: UNKNOWN, sentence: "" };
  }

  const api = { adStance, OFFERED, NOT_OFFERED, UNKNOWN };
  root.TailorAdStance = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
