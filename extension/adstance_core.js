// What a job ad SAYS about visa sponsorship, read from its own text. A line-for-line port of
// sourcing/adstance.py (the app's reader), so the extension and the app never disagree about the
// same ad; tests/test_extension_adstance.py runs both on the same sentences.
//
// Why this matters (Kofi, 2026-10-10): on a U.S. Bank posting the employer badges showed H-1B and
// PERM history while the ad itself said "This position is not eligible for visa sponsorship". The
// employer's record and the role's stance are different facts; a student needs both, and the
// role's own words decide. A NO anywhere outranks a YES anywhere. E-Verify is not sponsorship.
//
// RESTRICTED (2026-10-11): the ad requires US citizenship, a green card / permanent residency, or
// a security clearance. Closed to a visa holder whatever the employer's record says, so it gets
// its own red chip with the ad's sentence. RESTRICTED is sourcing/adstance.py RESTRICTED_SRC,
// which sourcing/quality.py also uses to keep such roles off the board.
(function (root) {
  const OFFERED = "offered", NOT_OFFERED = "not_offered", RESTRICTED = "restricted", UNKNOWN = "unknown";
  const QUOTE_CHARS = 140;
  // A full stop after a lone letter is an abbreviation ("U.S. citizenship"), not a sentence end.
  const SENT = /(?:(?<![A-Za-z])[A-Za-z]\.|[^.\n!?•])+/g;
  const RESTRICTED_RE = /(u\.?s\.?\s*citizens?(hip)?\s*(is\s*)?(required|only|mandatory)|must\s+be\s+(a\s+|an\s+)?(u\.?s\.?\s*)?(citizen|green\s*card\s*holder|(lawful\s+)?permanent\s+resident)|sole\s+u\.?s\.?\s*citizen|(citizens?|citizenship)\s*(,|\/|\s+or\s+|\s+and\s+)\s*(a\s+)?(green\s*card(\s*holders?)?|(lawful\s+)?permanent\s+residen(ts?|cy))|(green\s*card(\s*holders?)?|(lawful\s+)?permanent\s+residen(ts?|cy))\s*(is\s+|are\s+)?(required|only|mandatory)|must\s+(hold|have|possess)\s+(a\s+|an\s+)?(valid\s+|current\s+)?(green\s*card|(lawful\s+)?permanent\s+residen(t|cy))|security\s+clearance|active\s+clearance|obtain\s+(a\s+)?(security\s+)?clearance|ts\/sci|top\s+secret|secret\s+clearance|dod\s+secret|polygraph|\bq\s+clearance\b)/i;
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

  // A bar anywhere outranks a yes anywhere; a citizenship / clearance bar outranks a plain no.
  function adStance(text) {
    let no = null, yes = null;
    for (const s of sentences(text)) {
      if (RESTRICTED_RE.test(s)) return { stance: RESTRICTED, sentence: s.slice(0, 300) };
      if (no !== null || !(ABOUT.test(s) && CTX.test(s))) continue;
      if (NO.some(p => p.test(s))) no = s.slice(0, 300);
      else if (yes === null && YES.some(p => p.test(s))) yes = s.slice(0, 300);
    }
    if (no) return { stance: NOT_OFFERED, sentence: no };
    return yes ? { stance: OFFERED, sentence: yes } : { stance: UNKNOWN, sentence: "" };
  }

  // The deciding sentence cut to about `limit` characters at a word boundary, for a chip's
  // tooltip and the grey line under the chips (sourcing/adstance.py short_sentence).
  function shortSentence(sentence, limit) {
    limit = limit || QUOTE_CHARS;
    const s = String(sentence || "").split(/\s+/).filter(Boolean).join(" ");
    if (s.length <= limit) return s;
    let cut = s.slice(0, limit + 1);
    const sp = cut.lastIndexOf(" ");
    cut = sp > Math.floor(limit / 2) ? cut.slice(0, sp) : s.slice(0, limit);
    return cut.replace(/[ ,;:]+$/, "") + "...";
  }

  // The chip for a stance: its label, the red/green class, and the tooltip's explanation.
  const CHIP = {
    [RESTRICTED]: { label: "Citizenship or clearance required", cls: "tb-ad-no tb-ad-restricted",
      note: "This role requires US citizenship, permanent residency, or a security clearance, so it is closed to a visa holder whatever the employer's record says." },
    [NOT_OFFERED]: { label: "No sponsorship for this role", cls: "tb-ad-no",
      note: "The badges after this are the employer's history on other roles, not this one." },
    [OFFERED]: { label: "Ad offers sponsorship", cls: "tb-ad-yes",
      note: "The ad does not name the visa; for a US new hire that usually means H-1B." },
  };
  function stanceChip(r) {
    const c = r && CHIP[r.stance];
    if (!c) return null;
    const quote = shortSentence(r.sentence);
    return { stance: r.stance, label: c.label, cls: c.cls, quote,
             title: (quote ? "The ad says: “" + quote + "” " : "") + c.note };
  }

  const api = { adStance, shortSentence, stanceChip, OFFERED, NOT_OFFERED, RESTRICTED, UNKNOWN, QUOTE_CHARS };
  root.TailorAdStance = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
