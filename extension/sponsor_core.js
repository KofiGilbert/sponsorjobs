// The visa-sponsor lookup, run INSIDE the extension from the static index the feed publishes
// (scripts/build_sponsor_index.py -> <feed>/sponsors/{shard}.json.gz). With it the badges work
// with no SponsorJobs app installed, and no company name ever leaves the browser: the extension
// downloads whole shards, never asks a server about one company.
//
// This is a line-for-line PORT of sourcing/sponsors.py (normalize_employer,
// normalize_employer_variants, SponsorDB.lookup / _lookup_one / _match, _merge_records,
// SponsorRecord.badges, looks_us, naics_industry) and of the response ui/app.py builds in
// /api/sponsors/lookup, so content.js reads the same shape from either source.
// tests/test_extension_sponsor_core.py runs both on the same index and requires identical
// answers; change the two together.
//
// Pure: no DOM, no chrome.*, no network. A caller hands in `getShard(key)` returning a shard
// prepared by prepareShard() (or null when it isn't loaded). Loads in a service worker
// (importScripts), a page, or node (require).
(function () {
  "use strict";
  const G = typeof globalThis !== "undefined" ? globalThis : self;
  if (G.SponsorCore) return;

  // ---- sourcing/sponsors.py constants (keep in step) -------------------------------------
  const US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV " +
    "NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split(" ");
  // Python's \w / \b are Unicode-aware on str patterns; JS's are ASCII-only even with /u, so the
  // word boundary is spelled out with Unicode classes to behave the same on "São Paulo" etc.
  const W = "[\\p{L}\\p{N}_]";
  const B = `(?:(?<=${W})(?!${W})|(?<!${W})(?=${W}))`;
  const US_WORDS = new RegExp(B + "(u\\.?s\\.?a?|united states|remote\\s*[-,:]?\\s*(us|usa|united states)|" +
    "anywhere in the us|us[- ]based|us remote|" +
    "new york|nyc|san francisco|los angeles|chicago|boston|seattle|austin|denver|" +
    "atlanta|dallas|houston|miami|philadelphia|phoenix|san jose|san diego|portland|" +
    "washington,? d\\.?c\\.?|silicon valley|bay area)" + B, "iu");
  const US_STATE_TAIL = new RegExp(",\\s*(" + US_STATES.join("|") + ")" + B + `(?!${W})`, "u");

  const NAICS_SECTOR = {
    "11": "Agriculture, Forestry & Fishing", "21": "Mining, Oil & Gas",
    "22": "Utilities", "23": "Construction",
    "31": "Manufacturing", "32": "Manufacturing", "33": "Manufacturing",
    "42": "Wholesale Trade", "44": "Retail", "45": "Retail",
    "48": "Transportation & Warehousing", "49": "Transportation & Warehousing",
    "51": "Information & Media", "52": "Finance & Insurance",
    "53": "Real Estate", "54": "Professional, Scientific & Technical Services",
    "55": "Company Management", "56": "Administrative & Support Services",
    "61": "Education", "62": "Health Care & Social Assistance",
    "71": "Arts, Entertainment & Recreation", "72": "Accommodation & Food Services",
    "81": "Other Services", "92": "Public Administration",
  };

  const SUFFIX = new Set(["inc", "incorporated", "llc", "l.l.c", "corp", "corporation", "co", "company",
    "ltd", "limited", "lp", "llp", "plc", "pllc", "pc", "pa", "na", "usa", "us"]);

  const ALIASES = {
    "meta": "meta platforms",
    "facebook": "meta platforms",
    "alphabet": "google",
    "aws": "amazon web services",
    "uber": "uber technologies",
    "walgreens": "walgreen",
  };

  const BRAND_PREFIXES = new Set(["amazon", "google", "microsoft", "oracle", "salesforce", "nvidia",
    "qualcomm", "deloitte", "accenture", "capgemini", "infosys", "wipro", "cognizant"]);

  // ---- normalization -----------------------------------------------------------------------
  function joinInitials(toks) {
    const out = [];
    let run = "";
    for (const t of toks) {
      if (t.length === 1 && t >= "a" && t <= "z") { run += t; continue; }
      if (run) { out.push(run); run = ""; }
      out.push(t);
    }
    if (run) out.push(run);
    return out;
  }

  function normalize(name, joinInits) {
    let s = String(name || "").toLowerCase();
    s = s.split("&").join(" and ");
    s = s.replace(/[^a-z0-9 ]+/g, " ");
    let toks = s.split(" ").filter(Boolean);
    if (joinInits) toks = joinInitials(toks);
    while (toks.length && SUFFIX.has(toks[toks.length - 1])) toks.pop();
    if (toks.length && toks[0] === "the") toks = toks.slice(1);
    return toks.join(" ");
  }

  const normalizeEmployer = (name) => normalize(name, true);

  function normalizeEmployerVariants(name) {
    const out = [normalizeEmployer(name)];
    const legacy = normalize(name, false);
    if (legacy && !out.includes(legacy)) out.push(legacy);
    return out.filter(Boolean);
  }

  // ---- location + industry -----------------------------------------------------------------
  function looksUs(location) {
    const text = String(location || "").trim();
    if (!text) return false;
    return US_WORDS.test(text) || US_STATE_TAIL.test(text);
  }

  function naicsIndustry(naics) {
    const digits = String(naics || "").replace(/\D/g, "");
    return digits.length >= 2 ? (NAICS_SECTOR[digits.slice(0, 2)] || "") : "";
  }

  // ---- the index ---------------------------------------------------------------------------
  function shardOf(norm) {
    const c = (norm || "_")[0];
    return (c >= "a" && c <= "z") || (c >= "0" && c <= "9") ? c : "_";
  }

  // scripts/build_sponsor_index.py title_key: a letter is upper-cased when the character before
  // it is not a letter. A record stores 0 as its display name when it is exactly this.
  function titleKey(norm) {
    let out = "", prevAlpha = false;
    for (const ch of norm) {
      const lo = ch.toLowerCase();
      const alpha = lo >= "a" && lo <= "z";
      out += alpha ? (prevAlpha ? lo : ch.toUpperCase()) : ch;
      prevAlpha = alpha;
    }
    return out;
  }

  // A shard's {norm: rec} plus its keys sorted once, so a lookup finds "x *" children with a
  // binary search instead of scanning the whole shard.
  function prepareShard(records) {
    const recs = records || {};
    return { records: recs, keys: Object.keys(recs).sort() };
  }

  function decode(norm, rec) {
    const r = rec || [];
    const num = (i) => Number(r[i] || 0);
    return {
      display_name: r[0] === 0 || r[0] === undefined ? titleKey(norm) : r[0],
      h1b_approvals: num(1), h1b_last_fy: num(2), h1b_first_fy: num(3), perm_certs: num(4),
      e_verify: !!r[5], cap_exempt: !!r[6], naics: r[7] || "", state: r[8] || "",
    };
  }

  // The shards a lookup of `company` reads (every spelling it tries, after aliases).
  function shardsFor(company) {
    const keys = [];
    for (const n of normalizeEmployerVariants(company)) {
      const s = shardOf(ALIASES[n] || n);
      if (!keys.includes(s)) keys.push(s);
    }
    return keys;
  }

  function lowerBound(arr, x) {
    let lo = 0, hi = arr.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] < x) lo = mid + 1; else hi = mid; }
    return lo;
  }

  // ---- SponsorDB.lookup --------------------------------------------------------------------
  function mergeRecords(matches) {
    let top = matches[0];
    const key = (r) => [r.h1b_approvals, r.perm_certs, r.display_name || ""];
    const gt = (a, b) => {               // Python tuple comparison, first max wins on a tie
      const x = key(a), y = key(b);
      for (let i = 0; i < x.length; i++) { if (x[i] > y[i]) return true; if (x[i] < y[i]) return false; }
      return false;
    };
    for (const r of matches.slice(1)) if (gt(r, top)) top = r;
    const firsts = matches.map((r) => r.h1b_first_fy).filter(Boolean);
    return {
      display_name: top.display_name,
      h1b_approvals: matches.reduce((s, r) => s + r.h1b_approvals, 0),
      h1b_last_fy: Math.max(...matches.map((r) => r.h1b_last_fy)),
      naics: top.naics, state: top.state,
      cap_exempt: matches.some((r) => r.cap_exempt),
      e_verify: matches.some((r) => r.e_verify),
      h1b_first_fy: firsts.length ? Math.min(...firsts) : 0,
      perm_certs: matches.reduce((s, r) => s + r.perm_certs, 0),
    };
  }

  // The conservative candidate set (exact + whole-word parents + children) and match, exactly as
  // SponsorDB._lookup_one / _match. Throws when the needed shard isn't loaded.
  function lookupOne(norm, getShard) {
    const shard = getShard(shardOf(norm));
    if (!shard) throw new Error("shard_missing:" + shardOf(norm));
    const recs = shard.records;
    const allowPrefix = norm.includes(" ") || BRAND_PREFIXES.has(norm);
    const cands = [];
    const add = (k) => { if (Object.prototype.hasOwnProperty.call(recs, k) && !cands.includes(k)) cands.push(k); };
    if (allowPrefix) {
      const parts = norm.split(" ");
      add(norm);
      for (let i = 1; i < parts.length; i++) add(parts.slice(0, i).join(" "));
      const lo = norm + " ";
      for (let i = lowerBound(shard.keys, lo); i < shard.keys.length && shard.keys[i].startsWith(lo); i++)
        add(shard.keys[i]);
      cands.sort();
    } else {
      add(norm);
    }
    const matches = [];
    for (const cand of cands) {
      if (cand === norm) { matches.push(decode(cand, recs[cand])); continue; }
      if (!allowPrefix) continue;
      if (cand.startsWith(norm + " ")) { matches.push(decode(cand, recs[cand])); continue; }
      if (norm.startsWith(cand + " ") && (cand.includes(" ") || BRAND_PREFIXES.has(cand)))
        matches.push(decode(cand, recs[cand]));
    }
    if (!matches.length) return null;
    if (matches.length === 1) return matches[0];
    return mergeRecords(matches);
  }

  function lookup(company, getShard) {
    const variants = normalizeEmployerVariants(company).map((n) => ALIASES[n] || n);
    if (!variants.length) return null;
    if (variants.length > 1) {
      const found = variants.map((v) => lookupOne(v, getShard)).filter(Boolean);
      return found.length === 1 ? found[0] : (found.length ? mergeRecords(found) : null);
    }
    return lookupOne(variants[0], getShard);
  }

  // ---- SponsorRecord / the endpoint's response ---------------------------------------------
  function fyRange(rec) {
    const first = rec.h1b_first_fy || rec.h1b_last_fy, last = rec.h1b_last_fy;
    return first === last ? `FY${last}` : `FY${first} to ${last}`;
  }

  function badges(rec) {
    const out = [];
    if (rec.h1b_approvals > 0)
      out.push({ code: "H-1B", label: "H-1B sponsor",
        detail: `${rec.h1b_approvals} approvals (${fyRange(rec)})`,
        basis: `${rec.h1b_approvals} approved H-1B petitions on record ` +
               `(${fyRange(rec)}, USCIS Data Hub). A track record, ` +
               "not a promise this role sponsors." });
    if (rec.perm_certs > 0)
      out.push({ code: "GREEN-CARD", label: "Green-card sponsor (PERM)",
        detail: `${rec.perm_certs} certified PERM cases`,
        basis: `${rec.perm_certs} certified PERM (green-card) cases on ` +
               "record (DOL disclosure). Historical, not a guarantee." });
    if (rec.e_verify)
      out.push({ code: "STEM-OPT", label: "E-Verify (STEM-OPT-capable)",
        detail: "Employer on record as E-Verify-enrolled",
        basis: "E-Verify enrollment is required for a STEM-OPT hire, but " +
               "on its own it doesn't mean they sponsor, and enrollment " +
               "can lapse. Confirm before relying on it." });
    if (rec.cap_exempt)
      out.push({ code: "CAP-EXEMPT", label: "Cap-exempt (likely, no lottery)",
        basis: "Guessed from the employer's name/industry (higher-ed or " +
               "research = no H-1B lottery). A heuristic, not verified." });
    return out;
  }

  function profile(rec) {
    return {
      h1b_approvals: rec.h1b_approvals,
      h1b_first_fy: rec.h1b_first_fy || rec.h1b_last_fy,
      h1b_last_fy: rec.h1b_last_fy,
      fy_range: rec.h1b_approvals > 0 ? fyRange(rec) : "",
      perm_certs: rec.perm_certs,
      e_verify: !!rec.e_verify,
      cap_exempt: !!rec.cap_exempt,
      state: rec.state || "",
      industry: naicsIndustry(rec.naics),
    };
  }

  // The same body ui/app.py /api/sponsors/lookup returns (minus the transport's `ok`).
  function lookupResponse(company, location, getShard, employersLoaded) {
    company = String(company || "").trim();
    location = String(location || "").trim();
    const rec = company ? lookup(company, getShard) : null;
    const inUs = location ? looksUs(location) : null;
    return {
      company,
      matched: !!rec,
      matched_name: rec ? rec.display_name : "",
      visa: rec && inUs ? badges(rec) : [],
      sponsor_employer: !!rec,
      profile: rec ? profile(rec) : null,
      role_in_us: inUs,
      employers_loaded: employersLoaded || 0,
    };
  }

  const api = {
    normalizeEmployer, normalizeEmployerVariants, looksUs, naicsIndustry, shardOf, shardsFor,
    titleKey, prepareShard, lookup, badges, profile, lookupResponse,
  };
  G.SponsorCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
