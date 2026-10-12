// The job boards, one adapter each. content.js used to be LinkedIn-shaped with Indeed bolted on;
// this table makes LinkedIn one adapter among several, so a new board is one object here and
// nothing in content.js. Shared by the content script (badges, stance chips, the "Sponsored
// only" toggle), the background worker (which board a host belongs to, for the per-board
// permission grant) and the popup (the "Enable on Indeed" prompt).
//
// An adapter only READS what is on screen (CLAUDE.md s7). Nothing here clicks, scrolls, focuses,
// fetches or dispatches an event; tests/test_extension_boards.py greps this file for exactly
// those and fails the build if one appears. Handshake in particular is login-gated and
// React-rendered, so its reader copes with lazy markup by being re-run on mutation, never by
// asking the page to do anything.
//
// Each adapter:
//   id, name            the board, as the popup names it
//   optional            true for a board the person grants separately (optional_host_permissions)
//   origins             the manifest match patterns for that grant
//   companyHref         the URL shape of the board's employer links (a far stabler contract than
//                       a class name: LinkedIn cannot stop linking to /company/<slug>)
//   matches(loc)        is this URL a page we read (results list or job page)?
//   cards(doc)          the result cards on a results page
//   cardInfo(card)      {company, title, location, jd, anchor, slot} read off one card
//   posting(doc)        {company, title, location, anchor} for the OPEN job (pane or page)
//   header(doc)         the element our chip row is mounted after, or null
//   description(doc)    the full JD text, for the ad-stance reader
//   evidence            how the selectors were verified, in plain words, for the next person
//
// Selectors are tolerant on purpose: several fallbacks per field, structural hooks (data-testid,
// data-test, data-hook, URL shapes, headings) before class names, class-prefix matches where a
// board uses CSS modules ("JobDetails_jobTitle__x7f3a" keeps its prefix across deploys).
(function (root) {
  "use strict";
  if (root.TailorBoards) return;

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim().replace(/\s*·.*$/, "");
  const textOf = (el) => clean((el && (el.innerText || el.textContent)) || "");

  // The first element among `sels` (in order) that has visible text; `root` may be a card.
  function firstEl(rootEl, sels, min = 1) {
    if (!rootEl || !rootEl.querySelector) return null;
    for (const s of sels) {
      let el = null;
      try { el = rootEl.querySelector(s); } catch { continue; }
      if (el && textOf(el).length >= min) return el;
    }
    return null;
  }
  function firstText(rootEl, sels, maxLen = 160) {
    const el = firstEl(rootEl, sels);
    const t = el ? textOf(el) : "";
    return t && t.length <= maxLen ? t : "";
  }
  function allEls(doc, sels) {
    if (!doc || !doc.querySelectorAll) return [];
    for (const s of sels) {
      let found = [];
      try { found = [...doc.querySelectorAll(s)]; } catch { continue; }
      if (found.length) return found;
    }
    return [];
  }
  // The description block: the first selector whose text is long enough to be a real JD and not
  // a nav blurb. Bounded, so a 200 KB page never reaches the reader.
  function longText(doc, sels, min = 200, max = 40000) {
    for (const s of sels) {
      let el = null;
      try { el = doc.querySelector(s); } catch { continue; }
      const t = el && (el.innerText || el.textContent);
      if (t && t.replace(/\s+/g, " ").trim().length > min) return t.slice(0, max);
    }
    return "";
  }
  // Computed style comes from the page (content.js hands it in), so the readers can run in a
  // plain node harness too.
  const ENV = { getComputedStyle: null };
  const setEnv = (env) => Object.assign(ENV, env || {});
  const styleOf = (el) => {
    const f = ENV.getComputedStyle || (typeof root.getComputedStyle === "function" ? root.getComputedStyle : null);
    try { return (f && f(el)) || {}; } catch { return {}; }
  };

  // A "City, ST" (or "Remote - US", or "London, England") shape in a run of text. The role's
  // location gates the visa badge, so it is read as a SHAPE, not as a class name.
  function locationInText(text) {
    if (!text) return "";
    const US_STATE = /\b(A[LKZR]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|P[A]|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\b/;
    const CITY = "[A-Z][a-zA-Z.'-]+(?:[ -][A-Z][a-zA-Z.'-]+){0,2}";
    const m = text.match(new RegExp("(" + CITY + ",\\s*" + US_STATE.source + ")"))
      || text.match(/\b(Remote\s*[-,]?\s*(?:US|USA|United States))\b/i)
      || text.match(new RegExp("(" + CITY + ",\\s*[A-Z][a-zA-Z]{3,20})\\b"));
    if (!m) return "";
    const hit = clean(m[1]);
    if (!hit.includes(",")) return hit;
    const [head, ...tail] = hit.split(",");
    const city = head.trim().split(/\s+/).slice(-2).join(" ");
    return clean([city, ...tail].join(","));
  }

  // The company link closest to the title: the page is full of other employer links (ads, the
  // hiring team, "similar jobs"), and the one that shares the most ancestry with the <h1> is the
  // one this job belongs to. `pane` narrows the search to the open job's pane when a board has one.
  function companyNearTitle(doc, href, pane) {
    const scope = pane || doc;
    const links = allEls(scope, [`a[href*="${href}"]`]).filter(a => textOf(a));
    if (!links.length) return null;
    const h1 = (pane && pane.querySelector && pane.querySelector("h1")) || doc.querySelector("h1");
    if (!h1) return links[0];
    const ancestors = (n) => { const out = []; for (; n; n = n.parentElement) out.push(n); return out; };
    const titleLine = ancestors(h1);
    let best = null, bestScore = -1;
    for (const a of links) {
      const chain = ancestors(a);
      const shared = chain.findIndex(n => titleLine.includes(n));
      // The nearest box holding both must be a card or a pane, not the whole page: on a results
      // list with no open job the only thing a card's link shares with the page title is <body>,
      // and that would badge the header with the first card's employer.
      const common = shared === -1 ? null : chain[shared];
      const span = common ? ((common.innerText || common.textContent || "")).length : Infinity;
      const score = shared === -1 || span > 4000 ? -1 : 1000 - shared;
      if (score > bestScore) { best = a; bestScore = score; }
    }
    return bestScore > 0 ? best : null;
  }

  // A generic card reader: company / title / location / snippet from the hooks the board gives
  // us, the card itself as the slot for the badge row.
  function readCard(card, hooks) {
    const compEl = firstEl(card, hooks.company);
    const company = compEl ? textOf(compEl) : "";
    if (!company || company.length > 80) return null;
    const titleEl = firstEl(card, hooks.title);
    const title = titleEl ? textOf(titleEl) : "";
    let location = firstText(card, hooks.location || [], 80);
    if (!location && hooks.locationLine) location = hooks.locationLine(card, compEl) || "";
    if (!location && hooks.locationFromText) location = locationInText(textOf(card));
    const jd = (firstEl(card, hooks.snippet || [], 40) || { textContent: "" }).textContent.replace(/\s+/g, " ").trim().slice(0, 3000);
    const anchor = titleEl || compEl;
    const slot = (hooks.slot && hooks.slot(card, titleEl, compEl)) || null;
    return { company, title, location, jd, anchor, slot };
  }

  // The host without "www."; `loc` is window.location or any {hostname, pathname, search}.
  const host = (loc) => ((loc && loc.hostname) || "").toLowerCase();
  const path = (loc) => (loc && loc.pathname) || "";
  const query = (loc) => (loc && loc.search) || "";

  // ================================ LinkedIn ===========================================
  const LINKEDIN = {
    id: "linkedin", name: "LinkedIn", optional: false,
    origins: ["https://www.linkedin.com/jobs/*"],
    companyHref: "/company/",
    evidence: "Live in the person's own browser since 2026-07 (jobs/view and jobs/search-results); " +
              "cards read by line position because the 2026 list has no company link and no stable class.",
    matches: (loc) => /(^|\.)linkedin\.com$/.test(host(loc)) && path(loc).startsWith("/jobs"),
    cards(doc) {
      // The 2026 results list: one lazy column whose children are the cards (plus a few promos
      // with no company line, which cardInfo rejects). Older markup kept as a fallback.
      const col = doc.querySelector('[componentkey="SearchResultsMainContent"], [data-testid="lazy-column"]');
      const fresh = col ? [...col.children].filter(c => c.innerText && c.innerText.trim()) : [];
      if (fresh.length) return fresh;
      return allEls(doc, ['li[data-occludable-job-id], div.job-card-container, li.jobs-search-results__list-item, [data-job-id]']);
    },
    // A logo beside a column of lines, [title] [company] [location] [meta]: read by position,
    // the way a person does.
    cardInfo(card) {
      let col = null;
      for (const d of card.querySelectorAll("div")) {
        if (d.children.length >= 3 && styleOf(d).flexDirection === "column"
            && (d.innerText || "").trim() && d.innerText.length < 600) { col = d; break; }
      }
      if (!col) return null;
      const lines = [...col.children].filter(k => !k.classList.contains("tailor-card-badges"))
        .map(k => clean(k.innerText || "")).filter(Boolean);
      if (lines.length < 2) return null;
      const company = lines[1].replace(/\s*\(Verified job\)\s*/i, "");
      const location = (lines[2] || "").replace(/·.*$/, "").trim();
      if (!company || company.length > 80 || /^\d+ (school|connections?)/i.test(company)) return null;
      return { company, title: lines[0], location, jd: "", anchor: col, slot: col };
    },
    // The open job's employer: the /company/ link nearest the <h1>. The location is left to
    // content.js's climb from the title (the "Dallas, TX · Reposted" line is a sibling of it).
    posting(doc) {
      const co = companyNearTitle(doc, "/company/");
      if (!co) return null;
      return { company: textOf(co), anchor: co, title: textOf(doc.querySelector("h1")), location: "" };
    },
    // LinkedIn's job pane header is a column of stacked rows: [logo + company + title +
    // location] [On-site · Full-time] [Apply · Save]. Our block becomes one more row, right
    // after the first, found from the Apply button up (the class names are obfuscated).
    header(doc) {
      const apply = [...doc.querySelectorAll("button, a")]
        .find(b => /^(easy )?apply(\s*on company website)?$/i.test((b.innerText || "").trim()));
      if (!apply) return null;
      for (let e = apply.parentElement, i = 0; e && i < 12; e = e.parentElement, i++) {
        const co = e.querySelector('a[href*="/company/"]');
        if (!co || !e.innerText || e.innerText.length > 1500) continue;
        const cs = styleOf(e);
        if (cs.display === "flex" && String(cs.flexDirection || "").startsWith("column") && e.children.length >= 2) {
          const first = [...e.children].find(k => k.contains(co));
          if (first) return first;
        }
      }
      return null;
    },
    description: (doc) => longText(doc, ['[class*="jobs-description"]', '[class*="job-details"] [class*="description"]',
                                         '[class*="description"]', 'article', 'main']),
    locationSelectors: [".job-details-jobs-unified-top-card__primary-description-container .tvm__text",
                        ".job-details-jobs-unified-top-card__bullet", ".jobs-unified-top-card__bullet", ".topcard__flavor--bullet"],
    companySelectors: [".job-details-jobs-unified-top-card__company-name a", ".job-details-jobs-unified-top-card__company-name",
                       ".jobs-unified-top-card__company-name a", ".jobs-unified-top-card__company-name", ".topcard__org-name-link"],
  };

  // ================================ Indeed ============================================
  const INDEED = {
    id: "indeed", name: "Indeed", optional: true,
    origins: ["https://*.indeed.com/*"],
    companyHref: "/cmp/",
    evidence: "curl 2026-10-11: both the results page and viewjob answer 403 with a Cloudflare " +
              "challenge, so the markup could not be fetched; hooks are Indeed's documented " +
              "data-testid names (job_seen_beacon, company-name, text-location, inlineHeader-*, " +
              "#jobDescriptionText), several per field.",
    matches: (loc) => /(^|\.)indeed\.com$/.test(host(loc)) && /^\/(jobs|viewjob|q-|m\/jobs|m\/viewjob)/.test(path(loc)),
    cards: (doc) => allEls(doc, ['div.job_seen_beacon', 'li [data-testid="slider_item"]', 'div.cardOutline', 'td.resultContent']),
    cardInfo: (card) => readCard(card, {
      company: ['[data-testid="company-name"]', '[class*="companyName"]', '[data-testid*="company"]', 'a[href*="/cmp/"]'],
      title: ['h2.jobTitle a', 'a[data-jk]', '[data-testid="jobTitle"]', 'h2 a', 'h2'],
      location: ['[data-testid="text-location"]', '[data-testid*="location"]', '[class*="companyLocation"]'],
      snippet: ['[data-testid="jobsnippet"]', '[class*="job-snippet"]', '[class*="snippet"]', 'ul'],
      slot: (card) => card.querySelector('td.resultContent') || card,
    }),
    posting(doc) {
      const co = firstEl(doc, ['[data-testid="inlineHeader-companyName"] a', '[data-testid="inlineHeader-companyName"]',
                               '[data-testid="jobsearch-CompanyInfoContainer"] a[href*="/cmp/"]', '.jobsearch-CompanyInfoContainer a',
                               '[data-company-name="true"]']);
      if (!co) return null;
      return { company: textOf(co), anchor: co,
               title: firstText(doc, ['h1[data-testid="jobsearch-JobInfoHeader-title"]', '.jobsearch-JobInfoHeader-title', 'h1']),
               location: firstText(doc, INDEED.locationSelectors, 80) };
    },
    header: (doc) => firstEl(doc, ['[data-testid="jobsearch-JobInfoHeader-subtitle"]', '.jobsearch-JobInfoHeader-subtitle',
                                   '[data-testid="inlineHeader-companyLocation"]']),
    description: (doc) => longText(doc, ['#jobDescriptionText', '[data-testid="jobDescriptionText"]', '[class*="jobDescription"]',
                                         '.jobsearch-JobComponent-description', '[class*="job-description"]', 'main']),
    locationSelectors: ['[data-testid="inlineHeader-companyLocation"]', '[data-testid="job-location"]',
                        '.jobsearch-JobInfoHeader-subtitle div:last-child'],
    companySelectors: ['[data-testid="inlineHeader-companyName"] a', '[data-testid="inlineHeader-companyName"]',
                       '.jobsearch-CompanyInfoContainer a', '[data-company-name="true"]'],
  };

  // ================================ Glassdoor =========================================
  const GLASSDOOR = {
    id: "glassdoor", name: "Glassdoor", optional: true,
    origins: ["https://*.glassdoor.com/*"],
    companyHref: "/Overview/",
    evidence: "curl 2026-10-11: the results page answers 403 (\"Security | Glassdoor\", a captcha " +
              "wall), so the markup could not be fetched; hooks are Glassdoor's documented " +
              "data-test names (jobListing, job-title, emp-location) and its CSS-module prefixes " +
              "(JobCard_*, EmployerProfile_*, JobDetails_*), which keep their prefix across deploys.",
    matches: (loc) => /(^|\.)glassdoor\.[a-z.]+$/.test(host(loc)) && /^\/(Job|job-listing|Jobs|partner)\b/i.test(path(loc)),
    cards: (doc) => allEls(doc, ['li[data-test="jobListing"]', '[data-test="jobListing"]', 'li[class*="JobsList_jobListItem"]',
                                 '[class*="JobCard_jobCardContainer"]']),
    cardInfo: (card) => readCard(card, {
      company: ['[class*="EmployerProfile_compactEmployerName"]', '[data-test="emp-name"]', '[class*="employerName"]',
                '[class*="EmployerProfile"] span', '[class*="EmployerProfile"]'],
      title: ['a[data-test="job-title"]', '[data-test="job-title"]', '[class*="JobCard_jobTitle"]', 'a[href*="/job-listing/"]'],
      location: ['[data-test="emp-location"]', '[class*="JobCard_location"]', '[class*="location"]'],
      snippet: ['[data-test="descSnippet"]', '[class*="JobCard_jobDescriptionSnippet"]', '[class*="snippet"]'],
      slot: (card, titleEl) => (titleEl && titleEl.parentElement) || card,
    }),
    posting(doc) {
      const pane = doc.querySelector('[class*="JobDetails_jobDetailsContainer"], [data-test="jobDetails"], [class*="JobDetails_jobDetails"]');
      const co = firstEl(pane || doc, ['[class*="EmployerProfile_employerNameHeading"] h4', '[class*="EmployerProfile_employerNameHeading"]',
                                       '[data-test="employerName"]', '[class*="JobDetails_jobDetailsHeader"] [class*="EmployerProfile"] h4',
                                       '[class*="JobDetails_jobDetailsHeader"] [class*="employerName"]'])
        || companyNearTitle(doc, "/Overview/", pane);
      if (!co) return null;
      return { company: textOf(co).replace(/\s*\d(\.\d)?\s*★?$/, ""), anchor: co,
               title: firstText(pane || doc, ['[id^="jd-job-title"]', 'h1[data-test="job-title"]', '[class*="JobDetails_jobTitle"]', 'h1']),
               location: firstText(pane || doc, GLASSDOOR.locationSelectors, 80) };
    },
    header(doc) {
      const t = firstEl(doc, ['[id^="jd-job-title"]', 'h1[data-test="job-title"]', '[class*="JobDetails_jobTitle"]']);
      return t ? (t.parentElement || t) : null;
    },
    description: (doc) => longText(doc, ['[class*="JobDetails_jobDescription"]', '[data-test="jobDesc"]', '#JobDescriptionContainer',
                                         '[class*="jobDescriptionContent"]', '[class*="JobDetails_jobDetailsContainer"]', 'main']),
    locationSelectors: ['[data-test="location"]', '[class*="JobDetails_location"]', '[class*="JobDetails_jobDetailsHeader"] [class*="location"]'],
    companySelectors: ['[class*="EmployerProfile_employerNameHeading"] h4', '[data-test="employerName"]'],
  };

  // ================================ Handshake =========================================
  // Login-gated (.edu), React-rendered: curl sees only "Log in or sign up". The reader therefore
  // leans on data-hook attributes (Handshake's own test hooks), the /employers/ URL shape and
  // headings, and is simply re-run when the page mutates. It NEVER clicks or scrolls to make the
  // page render more (CLAUDE.md s7); what the person has scrolled into view is what gets read.
  const HANDSHAKE = {
    id: "handshake", name: "Handshake", optional: true,
    origins: ["https://*.joinhandshake.com/*"],
    companyHref: "/employers/",
    evidence: "curl 2026-10-11: app.joinhandshake.com answers only the login page (React shell), " +
              "so nothing could be fetched; hooks are Handshake's data-hook attributes, its " +
              "/employers/<id> link shape and headings. Not verified live (needs a .edu login).",
    matches: (loc) => /(^|\.)joinhandshake\.com$/.test(host(loc)) && /^\/(job-search|jobs|stu\/jobs|stu\/postings|postings)\b/.test(path(loc)),
    cards: (doc) => allEls(doc, ['[data-hook="jobs-card"]', '[data-hook="job-result-card"]', '[data-hook*="search-result"]',
                                 '[data-testid*="job-card"]', '[class*="job-card"]', '[class*="JobCard"]']),
    cardInfo: (card) => readCard(card, {
      company: ['[data-hook*="employer-name"]', '[data-hook*="employer"]', 'a[href*="/employers/"]', '[class*="employer-name"]', '[class*="employer"]'],
      title: ['[data-hook*="job-title"]', '[data-hook*="title"]', 'h3', 'h2', 'a[href*="/jobs/"]'],
      location: ['[data-hook*="location"]', '[class*="location"]'],
      locationFromText: true,
      snippet: ['[data-hook*="description"]', '[class*="description"]'],
    }),
    posting(doc) {
      const pane = doc.querySelector('[data-hook="job-details"], [data-hook*="job-detail"], [data-hook="preview"], main');
      const co = firstEl(pane || doc, ['[data-hook*="employer-name"]', '[data-hook="employer-name-link"]'])
        || companyNearTitle(doc, "/employers/", pane);
      if (!co) return null;
      return { company: textOf(co), anchor: co,
               title: firstText(pane || doc, ['[data-hook*="job-title"]', 'h1']),
               location: firstText(pane || doc, HANDSHAKE.locationSelectors, 80) || locationInText(textOf(pane && pane.querySelector("h1") ? pane.querySelector("h1").parentElement : null)) };
    },
    // The pane's title, never a card's (both carry data-hook="job-title").
    header(doc) {
      const t = firstEl(doc, ['[data-hook="job-details"] [data-hook*="job-title"]', '[data-hook="job-details"] h1',
                              '[data-hook*="job-detail"] h1', 'main h1', 'h1']);
      return t ? (t.parentElement || t) : null;
    },
    description: (doc) => longText(doc, ['[data-hook*="description"]', '[data-hook="job-details"]', '[class*="description"]', 'main']),
    locationSelectors: ['[data-hook*="location"]', '[class*="location"]'],
    companySelectors: ['[data-hook*="employer-name"]', 'a[href*="/employers/"]'],
  };

  // Dice writes "Queens, New York" in full and never "NY": read a City, State line as such.
  const US_STATES_FULL = "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia";
  function diceLocationIn(el) {
    const t = (el && (el.innerText || el.textContent)) || "";
    const m = t.replace(/\s+/g, " ").match(new RegExp("([A-Z][a-zA-Z.'-]+(?:[ -][A-Z][a-zA-Z.'-]+){0,2}),\\s*(" + US_STATES_FULL + ")\\b"));
    return m ? clean(m[1].split(/\s+/).slice(-2).join(" ") + ", " + m[2]) : "";
  }

  // ================================ Dice ==============================================
  const DICE = {
    id: "dice", name: "Dice", optional: true,
    origins: ["https://www.dice.com/*"],
    companyHref: "/company-profile/",
    evidence: "curl 2026-10-11, live markup: the results page (/jobs?q=) is server-rendered with " +
              "34 [data-testid=\"job-card\"] cards, each with job-search-job-detail-link (title), " +
              "job-card-company-name (company), a /company-profile/ link and a plain <p> " +
              "\"Queens, New York • Today\" line; the job page (/job-detail/<guid>) has JSON-LD, " +
              "[data-testid=\"job-detail-header-card\"] with an <h1> and the /company-profile/ link.",
    matches: (loc) => /(^|\.)dice\.com$/.test(host(loc)) && /^\/(jobs|job-detail)\b/.test(path(loc)),
    cards: (doc) => allEls(doc, ['[data-testid="job-card"]', '[data-testid="job-search-serp-card"]', 'dhi-search-card', '[data-cy="card"]']),
    cardInfo: (card) => readCard(card, {
      company: ['[data-testid="job-card-company-name"]', 'a[href*="/company-profile/"] p', 'a[href*="/company-profile/"]', '[data-cy="search-result-company-name"]'],
      title: ['[data-testid="job-search-job-detail-link"]', 'a[href*="/job-detail/"][aria-label]', '[data-cy="card-title-link"]', 'h5 a', 'h2 a'],
      location: ['[data-testid*="location"]', '[data-cy="search-result-location"]'],
      // "Queens, New York • Today": the first <p> with a bullet that is not the company name.
      locationLine: (card, compEl) => {
        for (const p of card.querySelectorAll("p")) {
          const t = textOf(p);
          if (p !== compEl && /\u2022/.test(p.textContent || "") && t.length < 80) return (p.textContent || "").split("\u2022")[0].replace(/\s+/g, " ").trim();
        }
        return "";
      },
      locationFromText: true,
      snippet: ['[data-testid="job-card-summary"]', '[data-cy="card-summary"]', '[class*="summary"]'],
      // The column of lines beside the logo: title, company, location, tags. Our row goes last.
      slot: (card, titleEl) => (titleEl && titleEl.parentElement && titleEl.parentElement.parentElement) || card,
    }),
    posting(doc) {
      const pane = doc.querySelector('[data-testid="job-detail-header-card"], [data-testid="job-detail-pane"]');
      const co = firstEl(pane || doc, ['[data-testid="job-detail-header-card"] a[href*="/company-profile/"]',
                                       '[data-testid="job-detail-pane"] a[href*="/company-profile/"]', '[data-cy="companyNameLink"]'])
        || companyNearTitle(doc, "/company-profile/", pane);
      if (!co) return null;
      const head = doc.querySelector('[data-testid="job-detail-header-card"]');
      return { company: textOf(co), anchor: co,
               title: firstText(pane || doc, ['[data-testid="job-detail-header-card"] h1', 'h1']),
               location: firstText(pane || doc, DICE.locationSelectors, 80) || diceLocationIn(head) || locationInText(textOf(head)) };
    },
    // The header card is a grid of rows: [logo + h1] [company + apply] [location, tags] ...; our
    // row is inserted after the row that holds the company link.
    header(doc) {
      const head = doc.querySelector('[data-testid="job-detail-header-card"]');
      const co = head && head.querySelector('a[href*="/company-profile/"]');
      if (!co) return null;
      let row = co;
      while (row.parentElement && row.parentElement !== head && row.parentElement.parentElement !== head) row = row.parentElement;
      return row;
    },
    description: (doc) => longText(doc, ['[data-testid="jobDescriptionHtml"]', '#jobDescription', '[data-testid="job-detail-pane"] [class*="description"]',
                                         '[class*="job-description"]', '[data-testid="job-detail-pane"]', 'main']),
    // Not locationTypeBadge: that one says "Hybrid" / "Remote", which is a mode, not a place.
    locationSelectors: ['[data-testid="job-detail-header-card"] [data-testid="job-location"]', '[data-cy="location"]'],
    companySelectors: ['[data-testid="job-detail-header-card"] a[href*="/company-profile/"]'],
  };

  // ================================ Google Jobs =======================================
  // The jobs panel inside google.com/search (udm=8, or ibp=htl;jobs). Google serves curl only an
  // "enable JavaScript" shell, so the hooks are the panel's long-standing class names, which the
  // public scrapers all key on (iFjolb card, BjJfJf title, vNEEBe company, Qk80Jf location,
  // HBvzbc description, #tl_ditsc / .whazf detail pane), each with a role/heading fallback.
  // The manifest match is narrow (www.google.com/search*) and matches() is narrower still: a
  // plain web search is never read.
  const GOOGLE = {
    id: "google", name: "Google Jobs", optional: true,
    origins: ["https://www.google.com/search*"],
    companyHref: null,
    evidence: "curl 2026-10-11: google.com/search answers an \"enable JavaScript\" shell (no " +
              "jobs markup), so nothing could be fetched; hooks are the jobs panel's public, " +
              "long-standing class names used by every Google Jobs scraper, with role fallbacks.",
    matches: (loc) => /(^|\.)google\.[a-z.]+$/.test(host(loc)) && path(loc) === "/search"
                      && /[?&](udm=8|ibp=htl;jobs)(&|$)/.test(decodeURIComponent(query(loc) || "")),
    cards: (doc) => allEls(doc, ['li.iFjolb', 'div.iFjolb', '[jscontroller] li[data-ved] [role="heading"]']).map(el =>
      el.tagName === "LI" || el.tagName === "DIV" ? el : (el.closest("li") || el)),
    cardInfo: (card) => readCard(card, {
      company: ['.vNEEBe', '[class*="vNEEBe"]'],
      title: ['.BjJfJf', '[role="heading"]', 'h3', 'h2'],
      location: ['.Qk80Jf', '[class*="Qk80Jf"]'],
      locationFromText: true,
      snippet: ['.HBvzbc'],
    }),
    posting(doc) {
      const pane = doc.querySelector('#tl_ditsc, .whazf, [id*="jobs-detail"], [jsname="tgVlQ"]');
      if (!pane) return null;
      const co = firstEl(pane, ['.nJlQNd', '.vNEEBe', '[class*="nJlQNd"]']);
      if (!co) return null;
      return { company: textOf(co).split(/\s*·\s*/)[0], anchor: co,
               title: firstText(pane, ['.sMzDkb', 'h2', '[role="heading"]']),
               location: firstText(pane, GOOGLE.locationSelectors, 80) || locationInText(textOf(co)) };
    },
    header(doc) {
      const pane = doc.querySelector('#tl_ditsc, .whazf');
      const t = pane && firstEl(pane, ['.sMzDkb', 'h2', '[role="heading"]']);
      return t ? (t.parentElement || t) : null;
    },
    description: (doc) => longText(doc, ['#tl_ditsc .HBvzbc', '.whazf .HBvzbc', '.HBvzbc', '#tl_ditsc', '.whazf']),
    locationSelectors: ['#tl_ditsc .tJ9zfc', '.whazf .tJ9zfc'],
    companySelectors: ['#tl_ditsc .nJlQNd', '.whazf .nJlQNd'],
  };

  // ================================ ZipRecruiter ======================================
  const ZIP = {
    id: "ziprecruiter", name: "ZipRecruiter", optional: true,
    origins: ["https://*.ziprecruiter.com/*"],
    companyHref: "/co/",
    evidence: "Verified live 2026-08-03 (40/40 cards <article id=\"job-card--...\">, company an " +
              "/co/ link, title an <h2>); curl 2026-10-11 answers 403 \"Just a moment\" (Cloudflare), " +
              "so the hooks stay the stable id and href shapes, not the hashed Tailwind classes.",
    matches: (loc) => /(^|\.)ziprecruiter\.com$/.test(host(loc)) && /^\/(jobs-search|jobs|c\/|job\/|ojob\/|candidate\/search)/.test(path(loc)),
    cards: (doc) => allEls(doc, ['article[id^="job-card"]', '[data-testid="job-card"]', 'article[class*="job"]']),
    cardInfo: (card) => readCard(card, {
      company: ['a[href*="/co/"]', '[data-testid*="company"]', '[class*="company"]'],
      title: ['h2 a', 'h2', 'a[href*="/jobs/"]', 'a[href*="/c/"]'],
      location: ['[data-testid*="location"]', '[class*="location"]', 'a[href*="location="]'],
      locationFromText: true,
      snippet: ['[class*="snippet"]', '[class*="description"]', 'p'],
      slot: (card, titleEl) => (titleEl && (titleEl.closest("h2") || titleEl).parentElement) || card,
    }),
    posting(doc) {
      const co = firstEl(doc, ['.hiring_company a', '.hiring_company', '[class*="company_name"]', '[data-testid="company-name"]'])
        || companyNearTitle(doc, "/co/");
      if (!co) return null;
      return { company: textOf(co), anchor: co,
               title: firstText(doc, ['h1.job_title', '[class*="job_title"]', 'h1']),
               location: firstText(doc, ZIP.locationSelectors, 80) };
    },
    header(doc) {
      const t = firstEl(doc, ['h1.job_title', '[class*="job_title"]', 'h1']);
      return t ? (t.parentElement || t) : null;
    },
    description: (doc) => longText(doc, ['.job_description', '[class*="job_description"]', '[class*="jobDescription"]',
                                         '[class*="description"]', 'article', 'main']),
    locationSelectors: ['[class*="job_location"]', '[data-testid*="location"]', '.location'],
    companySelectors: ['.hiring_company a', '.hiring_company'],
  };

  const BOARDS = [LINKEDIN, INDEED, GLASSDOOR, HANDSHAKE, DICE, GOOGLE, ZIP];
  const byId = (id) => BOARDS.find(b => b.id === id) || null;
  // The adapter for a URL, or null (an ATS page, a Google web search, a LinkedIn profile).
  const detect = (loc) => BOARDS.find(b => { try { return b.matches(loc); } catch { return false; } }) || null;
  // The board a host belongs to, whatever the path: for the popup and the grant check.
  const forHost = (hostname) => BOARDS.find(b => b.origins.some(o => {
    const h = o.replace(/^https?:\/\//, "").split("/")[0];
    return h.startsWith("*.") ? (hostname === h.slice(2) || hostname.endsWith(h.slice(1))) : hostname === h;
  })) || null;

  const api = { BOARDS, byId, detect, forHost, locationInText, companyNearTitle, clean, setEnv };
  root.TailorBoards = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
