// Runs on a job posting you're viewing (LinkedIn / Indeed / Greenhouse / Lever / Ashby).
// It reads the COMPANY NAME already on the page, asks your local SponsorJobs app whether that
// employer sponsors visas, and shows small badges. It only READS the page you opened:
// no auto-scrolling, no auto-apply, no submitting. Just you, browsing, with extra info.

(function () {
  const HOST = location.hostname;

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim().replace(/\s*·.*$/, "");
  const esc = (s) => String(s).replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const titleize = (s) => (s || "").replace(/[-_]+/g, " ").replace(/\b\w/g, c => c.toUpperCase()).trim();

  function atsCompanyFromUrl() {
    const seg = location.pathname.split("/").filter(Boolean)[0];
    if (!seg) return null;
    if (HOST.includes("greenhouse.io") || HOST.includes("lever.co") || HOST.includes("ashbyhq.com"))
      return titleize(seg);
    return null;
  }

  // The employer, from the page's own STRUCTURED data rather than its CSS.
  //
  // This exists because the extension silently did nothing on LinkedIn. It looked for class
  // names like ".job-details-jobs-unified-top-card__company-name", which are LinkedIn's
  // internal CSS and change whenever they redeploy. When none matched, pickTarget() returned
  // {} and no badge ever appeared: no error, no log, just silence, on the single most
  // important site we support. Written once, quietly dead ever since.
  //
  // schema.org JobPosting is the same data the page shows a search engine. It survives a
  // redesign, because the redesign is the CSS. Tried FIRST for that reason, with the class
  // names kept below only as a fallback.
  function companyFromJsonLd() {
    for (const el of document.querySelectorAll('script[type="application/ld+json"]')) {
      let data;
      try { data = JSON.parse(el.textContent || "{}"); } catch { continue; }
      // A page may ship one object, an array, or an @graph of them.
      const nodes = [].concat(data, data && data["@graph"] ? data["@graph"] : []).filter(Boolean);
      for (const n of nodes) {
        const t = n && n["@type"];
        const isJob = t === "JobPosting" || (Array.isArray(t) && t.includes("JobPosting"));
        if (!isJob) continue;
        const org = n.hiringOrganization;
        const name = typeof org === "string" ? org : (org && org.name);
        if (name) return { company: clean(name), location: locFromJsonLd(n) };
      }
    }
    return null;
  }

  // The role's location from the same structured data. It gates the visa badge (a US-only
  // instrument says nothing about a job in London), so it has to be as reliable as the name.
  function locFromJsonLd(job) {
    const l = job.jobLocation;
    const first = Array.isArray(l) ? l[0] : l;
    const a = first && first.address;
    if (!a) return "";
    const parts = [a.addressLocality, a.addressRegion, a.addressCountry]
      .map(x => (typeof x === "string" ? x : (x && x.name) || ""))
      .filter(Boolean);
    return parts.join(", ");
  }

  // The ROLE's location, which gates the badge. Sponsor data is per employer, so without
  // this a Spotify listing in London was badged "H-1B sponsor": true of Spotify, useless
  // for that job, and an invitation to spend an application on an impossibility. When we
  // cannot read a location the app withholds the badge rather than guess.
  function pickLocation() {
    let sels = [];
    if (HOST.includes("linkedin.com")) {
      sels = [".job-details-jobs-unified-top-card__primary-description-container .tvm__text",
              ".job-details-jobs-unified-top-card__bullet",
              ".jobs-unified-top-card__bullet",
              ".topcard__flavor--bullet"];
    } else if (HOST.includes("indeed.com")) {
      sels = ['[data-testid="inlineHeader-companyLocation"]',
              '[data-testid="job-location"]',
              '.jobsearch-JobInfoHeader-subtitle div:last-child'];
    } else {
      // ATS boards vary too much for a reliable selector; the app's own feed already has
      // the location for these, and a wrong guess here costs more than a missing badge.
      sels = ['[class*="location"]', '[data-ui="job-location"]'];
    }
    for (const s of sels) {
      const el = document.querySelector(s);
      const text = el && clean(el.textContent);
      if (text && text.length < 80) return text;
    }
    return "";
  }

  // Return {company, anchor}, the employer name and the element to place badges after.
  // The employer from LinkedIn's own URL SHAPE, not its CSS.
  //
  // On /jobs/search-results/ (where people actually browse) there is no structured data,
  // and the class names we relied on had changed, so the extension found nothing and did
  // nothing. A URL is a far more stable contract than a class name: LinkedIn cannot stop
  // linking a company to /company/<slug> without breaking their own product, whereas
  // ".job-details-jobs-unified-top-card__company-name" changes on a whim.
  //
  // We take the company link CLOSEST to the job title, because the page is full of other
  // /company/ links (the "people you can reach out to" cards, the hiring team, ads). The
  // one that shares the most DOM ancestry with the <h1> is the one this job belongs to.
  function linkedinCompany() {
    const links = [...document.querySelectorAll('a[href*="/company/"]')]
      .filter(a => clean(a.textContent));
    if (!links.length) return null;
    const h1 = document.querySelector("h1");
    if (!h1) return { el: links[0], name: clean(links[0].textContent) };
    // Depth of shared ancestry with the title: higher means "part of the same card".
    const ancestors = (n) => { const out = []; for (; n; n = n.parentElement) out.push(n); return out; };
    const titleLine = ancestors(h1);
    let best = null, bestScore = -1;
    for (const a of links) {
      const chain = ancestors(a);
      const shared = chain.findIndex(n => titleLine.includes(n));
      // Closer to the title (smaller hop count) wins; unrelated links score -1.
      const score = shared === -1 ? -1 : 1000 - shared;
      if (score > bestScore) { best = a; bestScore = score; }
    }
    if (!best || bestScore <= 0) return null;
    return { el: best, name: clean(best.textContent),
             location: linkedinLocationNear(best) };
  }

  // The role's location, from the same card as the employer.
  //
  // Without this the LinkedIn path sent an EMPTY location, and empty read as "not in the
  // US", so a Dallas job was badged "Sponsors in the US, not this role". Reading it matters
  // as much as reading the name: the location is what decides whether the visa applies.
  //
  // LinkedIn writes it as plain text near the title ("Dallas, TX · Reposted 2 weeks ago"),
  // so we look for a "City, ST" shape in the job card rather than for a class name, which is
  // the thing that keeps changing.
  function linkedinLocationNear(companyEl) {
    // Search up from the TITLE first, then from the company link.
    //
    // The first attempt only climbed from the company link, and on the real page the
    // location is a sibling of the <h1> ("Portfolio Manager" / "Dallas, TX · Reposted 2
    // weeks ago"), not of the company name. So it kept coming back empty and every badge
    // read "location unclear" on jobs whose location is in plain sight.
    //
    // Climbing from each in turn, smallest scope first, means we read the meta line of THIS
    // job rather than the first "City, ST" anywhere in a list of twenty other jobs.
    const h1 = document.querySelector("h1");
    for (const start of [h1, companyEl]) {
      const hit = locationInAncestors(start);
      if (hit) return hit;
    }
    return "";
  }

  // The host page's own text in `node`, without anything we injected. Our badges and the
  // sponsorship box sit right beside the company name; counted as page text they pushed the
  // card past the 400-character "this is still one job" limit before the climb reached
  // "Chicago, IL", and the badge said "location unclear" on a job that plainly says Chicago
  // (2026-10-10). The extension was tripping over its own words.
  function pageText(node) {
    let text = node.textContent || "";
    node.querySelectorAll && node.querySelectorAll(
      ".tailor-badges, .tailor-sponsor-profile, .tailor-match-wrap, .tailor-corner, [class^='tailor-'], [class*=' tailor-']"
    ).forEach(e => { const t = e.textContent; if (t) text = text.split(t).join(" "); });
    return text;
  }

  function locationInAncestors(startEl) {
    if (!startEl) return "";
    // The element we started from is the title, and textContent has no separators, so the
    // parent reads "Portfolio ManagerDallas, TX · Reposted...". Subtracting the title's own
    // text first is what turns "Manager Dallas, TX" into "Dallas, TX".
    const own = (startEl.textContent || "").trim();
    // Widen one level at a time and stop at the first match: the nearest enclosing box that
    // contains a location is this job's card, not the whole results list of other cities.
    let node = startEl;
    for (let i = 0; i < 6 && node; i++) {
      let text = pageText(node);
      // STOP once we have climbed out of this job's card. A card's meta line is short; a
      // thousand characters means we are reading the whole results list, and then the first
      // "City, ST" we find belongs to SOMEBODY ELSE'S job. Caught in testing: a role with no
      // location was handed "Fort Worth, TX" from a different listing further up the page.
      // That is the same crime as the Dallas lie, just better disguised: confidently wrong.
      if (text.length > 400) break;
      if (node !== startEl && own) text = text.split(own).join(" ");
      const hit = locationInText(text);
      if (hit) return hit;
      node = node.parentElement;
    }
    return "";
  }

  function locationInText(text) {
    if (!text) return "";
    const US_STATE = /\b(A[LKZR]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|P[A]|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\b/;
    // A city is at most a few capitalised words ("Dallas", "New York", "San Francisco"), so
    // bound it. An unbounded [A-Za-z ]+ before the comma swallows the sentence in front of
    // it: on this very card it produced "Bank Portfolio Manager Dallas, TX". That happened
    // to still read as US, which is exactly why it's worth fixing now, while it's visible.
    const CITY = "[A-Z][a-zA-Z.'-]+(?:[ -][A-Z][a-zA-Z.'-]+){0,2}";
    const m = text.match(new RegExp("(" + CITY + ",\\s*" + US_STATE.source + ")"))
      || text.match(/\b(Remote\s*[-,]?\s*(?:US|USA|United States))\b/i)
      // "London, England" / "Toronto, Ontario": read it so we can say NOT-US with confidence
      // rather than shrug, but only when it is as clearly shaped as the US case.
      || text.match(new RegExp("(" + CITY + ",\\s*[A-Z][a-zA-Z]{3,20})\\b"));
    if (!m) return "";
    const hit = clean(m[1]);
    // "Remote - US" has no city to trim, and trimming it produced "- US".
    if (!hit.includes(",")) return hit;
    // The card's text runs together ("...Portfolio Manager Dallas, TX"), so a capitalised
    // job title can be swallowed into the city. Keep the last two words before the comma:
    // enough for "New York" and "San Francisco", short enough to drop most of a title.
    const [head, ...tail] = hit.split(",");
    const city = head.trim().split(/\s+/).slice(-2).join(" ");
    return clean([city, ...tail].join(","));
  }

  function pickTarget() {
    let sels = [];
    if (HOST.includes("linkedin.com")) {
      // Try the URL shape FIRST on LinkedIn: it is the only thing here that has not moved.
      const li = linkedinCompany();
      // Pass the location through. Dropping it here is what produced the lie: an empty
      // location read as "not in the US" and badged a Dallas job as out of reach.
      if (li) return { company: li.name, anchor: li.el, location: li.location || "" };
      sels = [".job-details-jobs-unified-top-card__company-name a",
              ".job-details-jobs-unified-top-card__company-name",
              ".jobs-unified-top-card__company-name a",
              ".jobs-unified-top-card__company-name",
              ".topcard__org-name-link"];
    } else if (HOST.includes("indeed.com")) {
      sels = ['[data-testid="inlineHeader-companyName"] a',
              '[data-testid="inlineHeader-companyName"]',
              '.jobsearch-CompanyInfoContainer a',
              '[data-company-name="true"]'];
    }
    // 1. The page's own structured data. Survives a redesign; the class names below don't.
    const ld = companyFromJsonLd();
    if (ld && ld.company) {
      const anchor = sels.map(s => document.querySelector(s)).find(Boolean)
        || document.querySelector("h1")
        || document.body.firstElementChild;
      if (anchor) return { company: ld.company, anchor, location: ld.location || "" };
    }
    // 2. The site's CSS classes. Kept because they put the badge in exactly the right place
    //    when they DO match, but never trusted as the only way to read a company name.
    for (const s of sels) {
      const el = document.querySelector(s);
      const name = el && clean(el.textContent);
      if (name) return { company: name, anchor: el };
    }
    // 3. The URL, for ATS boards whose company IS the path (/lever.co/spotify).
    const ats = atsCompanyFromUrl();
    if (ats) return { company: ats, anchor: document.querySelector("h1, h2") || document.body.firstElementChild };
    return {};
  }

  // The names a person reads on a chip. The app shows the same three (H-1B, Green Card, STEM OPT);
  // here E-Verify is named outright, as the job screeners students compare us with do, with the
  // STEM-OPT meaning in the tooltip.
  const CHIP = { "H-1B": "H-1B", "GREEN-CARD": "Green Card", "STEM-OPT": "E-Verify", "CAP-EXEMPT": "Cap-exempt" };
  const CHIP_TIP = {
    "H-1B": "This employer has had H-1B petitions approved (public USCIS data).",
    "GREEN-CARD": "This employer has sponsored green cards (certified PERM cases, public DOL data).",
    "STEM-OPT": "Enrolled in E-Verify, so a STEM OPT extension is possible here.",
    "CAP-EXEMPT": "Likely cap-exempt: H-1B without the lottery.",
  };
  // The employer's record as chip codes. `visa` is only filled when the role is known to be in
  // the US; when the location could not be read, the record still exists, so read it from the
  // profile and say so in the tooltip rather than hiding it behind a grey "location unclear" chip.
  function employerCodes(data) {
    if (data.visa && data.visa.length) return data.visa.map(v => v.code);
    const p = data.profile || {};
    const codes = [];
    if (p.h1b_approvals > 0) codes.push("H-1B");
    if (p.perm_certs > 0) codes.push("GREEN-CARD");
    if (p.e_verify) codes.push("STEM-OPT");
    if (p.cap_exempt) codes.push("CAP-EXEMPT");
    return codes;
  }
  function chipHTML(code, extraTip) {
    const tip = (CHIP_TIP[code] || code) + (extraTip ? " " + extraTip : "");
    return `<span class="tailor-badge tb-${esc(code)}" title="${esc(tip)}">${esc(CHIP[code] || code)}</span>`;
  }

  function badgesHTML(data) {
    if (!data || data.ok === false) {
      if (data && data.error === "cant_reach_app")
        return `<span class="tailor-badge tb-off" title="Open the SponsorJobs app on your computer to see sponsor badges">Start SponsorJobs for visa badges</span>`;
      return "";
    }
    if (!data.matched)
      return `<span class="tailor-badge tb-none" title="No H-1B or green-card (PERM) sponsorship history found for this employer in USCIS/DOL data">No sponsor record</span>`;
    // The employer sponsors and we KNOW this role is elsewhere: the visas say nothing about it.
    // Only when role_in_us is exactly false; never on a location we merely failed to read.
    if (data.sponsor_employer && data.role_in_us === false)
      return `<span class="tailor-badge tb-none" title="This employer sponsors US visas, but this role is not in the US, so H-1B and PERM do not apply to it">Sponsors in the US, not this role</span>`;
    const unread = data.role_in_us == null;
    const chips = employerCodes(data).map(c => chipHTML(c,
      unread ? "We could not read this role's location; this is the employer's US record." : "")).join("");
    return chips +
      `<a class="tailor-badge tb-tailor" href="http://127.0.0.1:57000/" target="_blank" rel="noopener" title="Tailor your CV to this job in the SponsorJobs app">Tailor my CV ↗</a>`;
  }

  // The rich sponsorship PROFILE for the open posting: H-1B volume + how recent, green-card (PERM)
  // volume, E-Verify, cap-exempt. This is the depth that beats a yes/no checker, drawn entirely from
  // the public USCIS/DOL data the app already holds. Employer-level, with the honest caveat inline.
  function sponsorProfileHTML(data) {
    const p = data && data.profile;
    if (!p || !data.matched) return "";
    const rows = [];
    if (p.h1b_approvals > 0)
      rows.push(`<div class="tailor-sp-row"><span class="tailor-sp-k">H-1B</span> ${Number(p.h1b_approvals).toLocaleString()} approved petitions${p.fy_range ? " · " + esc(p.fy_range) : ""}</div>`);
    if (p.perm_certs > 0)
      rows.push(`<div class="tailor-sp-row"><span class="tailor-sp-k">Green card</span> ${Number(p.perm_certs).toLocaleString()} certified PERM cases</div>`);
    if (p.e_verify)
      rows.push(`<div class="tailor-sp-row"><span class="tailor-sp-k">E-Verify</span> enrolled (STEM-OPT capable)</div>`);
    if (p.cap_exempt)
      rows.push(`<div class="tailor-sp-row"><span class="tailor-sp-k">Cap-exempt</span> likely (no H-1B lottery)</div>`);
    if (!rows.length) return "";
    return `<div class="tailor-sponsor-profile"><div class="tailor-sp-hd">Sponsorship profile${data.matched_name ? " · " + esc(data.matched_name) : ""}</div>${rows.join("")}<div class="tailor-sp-foot">Historical public USCIS/DOL data, not a promise this role sponsors.</div></div>`;
  }

  // Where our block goes. On LinkedIn's job pane the header is a column of stacked rows:
  // [logo + company + title + location] [On-site · Full-time] [Apply · Save]. Our block becomes
  // one more row in that column, right after the first one, so it sits under the title and
  // cannot squeeze anything. Found from the Apply button up, not from class names (LinkedIn's
  // are obfuscated and change). Elsewhere: after the nearest block-level ancestor of the anchor.
  function linkedinHeader() {
    const apply = [...document.querySelectorAll("button, a")]
      .find(b => /^(easy )?apply(\s*on company website)?$/i.test((b.innerText || "").trim()));
    if (!apply) return null;
    for (let e = apply.parentElement, i = 0; e && i < 12; e = e.parentElement, i++) {
      const co = e.querySelector('a[href*="/company/"]');
      if (!co || !e.innerText || e.innerText.length > 1500) continue;
      const cs = getComputedStyle(e);
      if (cs.display === "flex" && cs.flexDirection.startsWith("column") && e.children.length >= 2) {
        const first = [...e.children].find(k => k.contains(co));
        if (first) return { column: e, after: first };
      }
    }
    return null;
  }
  function blockSlot(anchor) {
    let el = anchor;
    for (let i = 0; i < 10 && el.parentElement && el.parentElement !== document.body; i++) {
      const cs = getComputedStyle(el.parentElement);
      const rowish = cs.display === "contents" || cs.display.startsWith("inline") ||
        ((cs.display === "flex" || cs.display === "inline-flex") && !cs.flexDirection.startsWith("column")) ||
        (cs.display === "grid" && cs.gridTemplateColumns.split(" ").length > 1);
      if (!rowish) return el;
      el = el.parentElement;
    }
    return el;
  }
  function placeBlocks(anchor, blocks) {
    const li = HOST.includes("linkedin.com") ? linkedinHeader() : null;
    let at = li ? li.after : blockSlot(anchor);
    for (const b of blocks) { at.insertAdjacentElement("afterend", b); at = b; }
  }

  function render(anchor, data) {
    document.querySelectorAll(".tailor-badges, .tailor-sponsor-profile").forEach(e => e.remove());
    const html = badgesHTML(data);
    if (!html) return;
    const wrap = document.createElement("div");
    wrap.className = "tailor-badges";
    wrap.innerHTML = html;
    const blocks = [wrap];
    const prof = sponsorProfileHTML(data);          // posting-level detail; cards keep the compact badge
    if (prof) {
      const holder = document.createElement("div");
      holder.innerHTML = prof;
      blocks.push(holder.firstElementChild);
    }
    placeBlocks(anchor, blocks);
    LAST = { data, company: (data && data.matched_name) || "", stance: LAST.stance };
    paintCorner();
  }

  // ---- What THIS ad says about sponsorship (the role's own words outrank the employer's record) --
  // The badges above are the employer's public history. The ad can still say "This position is not
  // eligible for visa sponsorship" (U.S. Bank, 2026-10-10), and that sentence decides the role. Read
  // the whole description with the app's own reader (adstance_core.js) and put the answer FIRST in
  // the row, with the sentence on hover. Idempotent, so it catches a description that loads late.
  function readFullJD() {
    const sels = ['[class*="jobs-description"]', '[class*="job-details"] [class*="description"]',
                  '[class*="posting-description"]', '[data-testid="jobDescriptionText"]',
                  '#jobDescriptionText', '[class*="job-description"]', 'article', 'main', '#content'];
    for (const s of sels) {
      const el = document.querySelector(s);
      const t = el && el.innerText;
      if (t && t.replace(/\s+/g, " ").trim().length > 200) return t.slice(0, 40000);
    }
    return "";
  }
  let lastStanceKey = "";
  function mountStance() {
    const row = document.querySelector(".tailor-badges");
    const A = globalThis.TailorAdStance;
    if (!row || !A) return;
    const jd = readFullJD();
    const r = jd ? A.adStance(jd) : { stance: A.UNKNOWN, sentence: "" };
    const key = r.stance + "|" + r.sentence;
    if (key === lastStanceKey && row.querySelector(".tb-stance")) return;
    lastStanceKey = key;
    row.querySelectorAll(".tb-stance").forEach(e => e.remove());
    row.classList.toggle("tailor-role-no", r.stance === A.NOT_OFFERED);
    LAST.stance = r;
    paintCorner();
    if (r.stance === A.UNKNOWN) return;          // the ad says nothing: the employer badges stand alone
    // The role's own answer is the one that matters: a grey employer-level chip ("No sponsor
    // record", "location unclear") next to it only muddles it. Visa badges stay, dimmed.
    if (r.stance === A.NOT_OFFERED) row.querySelectorAll(".tb-none").forEach(e => e.remove());
    const chip = document.createElement("span");
    const no = r.stance === A.NOT_OFFERED;
    chip.className = `tailor-badge tb-stance ${no ? "tb-ad-no" : "tb-ad-yes"}`;
    chip.textContent = no ? "No sponsorship for this role" : "Ad offers sponsorship";
    chip.title = `The ad says: \u201c${r.sentence}\u201d` + (no
      ? " The badges after this are the employer's history on other roles, not this one."
      : " The ad does not name the visa; for a US new hire that usually means H-1B.");
    row.insertAdjacentElement("afterbegin", chip);
  }

  // ============================ P6: match score + skills + in-list + filters ============
  const P = () => window.TailorParity;   // pure decision logic (parity_core.js), loaded first
  const send = (msg) => new Promise((res) => {
    try { chrome.runtime.sendMessage(msg, (r) => { if (chrome.runtime.lastError) return res(null); res(r); }); }
    catch { res(null); }
  });

  // The JD text on the OPEN posting, for the profile-vs-JD match. Read from the description block;
  // bounded, and only when it's a real description (not a nav blurb).
  function readJD() {
    const sels = ['[class*="jobs-description"]', '[class*="job-details"] [class*="description"]',
                  '[class*="posting-description"]', '[data-testid="jobDescriptionText"]',
                  '#jobDescriptionText', '[class*="job-description"]', 'article', 'main', '#content'];
    for (const s of sels) {
      const el = document.querySelector(s);
      const t = el && el.textContent.replace(/\s+/g, " ").trim();
      if (t && t.length > 200) return t.slice(0, 6000);
    }
    return "";
  }

  function matchPillHTML(m) {
    if (!m) return "";
    if (m.needProfile)
      return `<a class="tailor-badge tb-off" href="http://127.0.0.1:57000/" target="_blank" rel="noopener" title="Open SponsorJobs and build your CV once, then your match shows here">${esc(m.text)}</a>`;
    return `<span class="tailor-match-pill tm-${esc(m.cls)}" title="${esc(m.note)} (${esc(m.skills)})">${esc(m.text)}</span>`;
  }

  function skillsPanelHTML(plan) {
    if (!plan) return "";
    const chip = (s, covered) =>
      `<span class="tailor-skill ${covered ? "sk-have" : "sk-miss"}" title="${covered ? "in your profile" : "not in your profile"}">${esc(s.term)}</span>`;
    const group = (label, items) => items.length
      ? `<div class="tailor-skills-row"><span class="tailor-skills-lab">${label}</span>${items.map(s => chip(s, s.covered)).join("")}</div>` : "";
    const req = group("Required", plan.required), opt = group("Optional", plan.optional);
    if (!req && !opt) return "";
    return `<div class="tailor-skills"><div class="tailor-skills-hd">JD skills, covered vs missing (keyword estimate)</div>${req}${opt}</div>`;
  }

  // Add the match pill + skills panel to the badge row on the open posting.
  async function enhancePosting() {
    const jd = readJD();
    if (!jd) return;
    const cov = await send({ type: "match", jd });
    if (!cov || !P()) return;
    const row = document.querySelector(".tailor-badges");
    if (!row) return;
    document.querySelectorAll(".tailor-match-wrap").forEach(e => e.remove());
    const m = P().matchBadge(cov);
    const plan = P().highlightTerms(cov);
    const html = matchPillHTML(m) + skillsPanelHTML(plan);
    if (!html) return;
    const wrap = document.createElement("div");
    wrap.className = "tailor-match-wrap";
    wrap.innerHTML = html;
    row.insertAdjacentElement("afterend", wrap);
  }

  // ---- In-list: badge each card in a results list (LinkedIn / Indeed) --------------------
  const isList = () => /linkedin\.com|indeed\.com|ziprecruiter\.com/.test(HOST);
  function cardEls() {
    let sel;
    if (HOST.includes("linkedin")) {
      // The 2026 results list: one lazy column whose children are the cards (plus a few promos
      // with no company line, which cardInfo rejects). Older markup kept as a fallback.
      const col = document.querySelector('[componentkey="SearchResultsMainContent"], [data-testid="lazy-column"]');
      const fresh = col ? [...col.children].filter(c => c.innerText && c.innerText.trim()) : [];
      if (fresh.length) return fresh;
      sel = 'li[data-occludable-job-id], div.job-card-container, li.jobs-search-results__list-item, [data-job-id]';
    }
    else if (HOST.includes("ziprecruiter"))
      // Verified live (2026-08-03): each result is <article id="job-card--...">, company is an
      // /co/ link, title an <h2>. ZipRecruiter's classes are hashed Tailwind, so we anchor on
      // the stable <article id> + href shapes, not class names.
      sel = 'article[id^="job-card"]';
    else
      sel = 'div.job_seen_beacon, td.resultContent, div.cardOutline, [data-jk]';   // indeed
    return [...document.querySelectorAll(sel)];
  }
  // LinkedIn's current card has no company link and no stable class: a logo beside a column of
  // lines, [title] [company] [location] [meta]. Read the lines by position, the way a person does.
  function linkedinCardInfo(card) {
    let col = null;
    for (const d of card.querySelectorAll("div")) {
      if (d.children.length >= 3 && getComputedStyle(d).flexDirection === "column"
          && (d.innerText || "").trim() && d.innerText.length < 600) { col = d; break; }
    }
    if (!col) return null;
    const lines = [...col.children].filter(k => !k.classList.contains("tailor-card-badges"))
      .map(k => clean(k.innerText || "")).filter(Boolean);
    if (lines.length < 2) return null;
    const company = lines[1].replace(/\s*\(Verified job\)\s*/i, "");
    const location = (lines[2] || "").replace(/·.*$/, "").trim();
    if (!company || company.length > 80 || /^\d+ (school|connections?)/i.test(company)) return null;
    return { company, location, jd: "", anchor: col, slot: col };
  }
  function cardInfo(card) {
    if (HOST.includes("linkedin")) {
      const li = linkedinCardInfo(card);
      if (li) return li;
    }
    // a[href*="/co/"] is ZipRecruiter's company link; /company/ is LinkedIn's. querySelector
    // with a list returns the first match in DOM order, so an unrelated board ignores the ones
    // that don't apply.
    const compEl = card.querySelector('[class*="company"] , a[href*="/company/"], a[href*="/co/"], [data-testid*="company"], [class*="companyName"]');
    const company = compEl && clean(compEl.textContent);
    const locEl = card.querySelector('[class*="location"], [class*="metadata"], [data-testid*="location"], [class*="companyLocation"], a[href*="location="]');
    const location = locEl ? clean(locEl.textContent) : "";
    const snipEl = card.querySelector('[class*="snippet"], [class*="job-snippet"], [class*="description"], ul');
    const jd = snipEl ? (snipEl.textContent || "").replace(/\s+/g, " ").trim().slice(0, 3000) : "";
    const anchor = card.querySelector('a[class*="title"], a[href*="/jobs/view"], a[href*="/job/"], a[data-jk], [class*="jobTitle"] a, h2') || compEl;
    return { company, location, jd, anchor: anchor || card };
  }
  async function badgeCard(card, info) {
    const data = await send({ type: "lookup", company: info.company, location: info.location });
    if (!P()) return;
    const b = P().sponsorBadge(data || { ok: false });
    // Per-card match only when the card carries a real snippet (a title-only "0%" would mislead).
    let m = null, pct = null;
    if (info.jd && info.jd.length > 120) {
      const cov = await send({ type: "match", jd: info.jd });
      m = cov && P().matchBadge(cov);
      if (m && typeof m.pct === "number") pct = m.pct;
    }
    // Remember the card's signals for the filter bar.
    card._tailor = { codes: b.codes || [], role_in_us: data && data.role_in_us, matchPct: pct };
    const parts = [];
    const codes = data && data.matched && data.role_in_us !== false ? employerCodes(data) : [];
    if (codes.length) parts.push(...codes.map(c => chipHTML(c)));
    else if (b.text && b.kind !== "off") parts.push(`<span class="tailor-badge tb-none">${esc(b.text)}</span>`);
    if (m && !m.needProfile) parts.push(`<span class="tailor-match-pill tm-${esc(m.cls)}" title="${esc(m.note)}">${esc(m.text)}</span>`);
    if (!parts.length) return;
    card.querySelectorAll(".tailor-card-badges").forEach(e => e.remove());
    const wrap = document.createElement("div");
    wrap.className = "tailor-badges tailor-card-badges";
    wrap.innerHTML = parts.join("");
    (info.slot || info.anchor.closest("li, div, td, article") || card).appendChild(wrap);
  }
  async function scanList() {
    if (!isList()) return;
    // A card is re-read when its text changes (LinkedIn recycles card elements as you scroll).
    const key = (c) => (c.innerText || "").replace(/\s+/g, " ").slice(0, 60);
    const cards = cardEls().filter(c => c.dataset.tailorScanned !== key(c)).slice(0, 40);
    for (const card of cards) {
      card.dataset.tailorScanned = key(card);
      const info = cardInfo(card);
      if (info.company) { try { await badgeCard(card, info); } catch {} }
    }
    if (cardEls().length) ensureFilterBar();
  }

  // ---- Filter bar: sponsorship chips + match-rate slider (client-side, honest gating) ----
  const FILTER = { chips: [], minMatch: 0 };
  function applyFilter() {
    if (!P()) return;
    for (const card of cardEls()) {
      const c = card._tailor;
      if (!c) continue;                                   // not scored yet, never hide it
      card.style.display = P().cardPasses(c, FILTER) ? "" : "none";
    }
  }
  function ensureFilterBar() {
    if (document.querySelector(".tailor-filterbar")) return;
    const bar = document.createElement("div");
    bar.className = "tailor-filterbar";
    bar.innerHTML =
      `<span class="tailor-fb-lab">SponsorJobs filters</span>` +
      ["H-1B", "E-VERIFY", "PERM"].map(c =>
        `<button class="tailor-fb-chip" data-chip="${c}" type="button">${c}</button>`).join("") +
      `<label class="tailor-fb-slider">Match &ge; <b class="tailor-fb-val">0%</b>` +
      `<input type="range" min="0" max="100" step="10" value="0" class="tailor-fb-range"></label>`;
    document.body.appendChild(bar);
    bar.querySelectorAll(".tailor-fb-chip").forEach(btn => btn.addEventListener("click", () => {
      const c = btn.dataset.chip, i = FILTER.chips.indexOf(c);
      if (i >= 0) { FILTER.chips.splice(i, 1); btn.classList.remove("on"); }
      else { FILTER.chips.push(c); btn.classList.add("on"); }
      applyFilter();
    }));
    const range = bar.querySelector(".tailor-fb-range"), val = bar.querySelector(".tailor-fb-val");
    range.addEventListener("input", () => { FILTER.minMatch = +range.value; val.textContent = range.value + "%"; applyFilter(); });
  }

  // ---- Our mark in the corner. A fixed button that never touches the page's layout; clicking
  // it opens a small panel with this job's sponsorship facts and a link to the app. ----
  let LAST = { data: null, stance: null, company: "" };
  function cornerPanelHTML() {
    const d = LAST.data, st = LAST.stance, A = globalThis.TailorAdStance;
    const rows = [];
    if (st && A && st.stance !== A.UNKNOWN) {
      const no = st.stance === A.NOT_OFFERED;
      rows.push(`<div class="tailor-cp-row ${no ? "cp-no" : "cp-yes"}"><b>${no ? "No sponsorship for this role" : "Ad offers sponsorship"}</b><div class="tailor-cp-q">\u201c${esc(st.sentence)}\u201d</div></div>`);
    }
    if (d && d.matched) {
      const p = d.profile || {};
      const facts = [];
      if (p.h1b_approvals > 0) facts.push(`<b>H-1B</b> ${Number(p.h1b_approvals).toLocaleString()} approved${p.fy_range ? ", " + esc(p.fy_range) : ""}`);
      if (p.perm_certs > 0) facts.push(`<b>Green card</b> ${Number(p.perm_certs).toLocaleString()} PERM cases`);
      if (p.e_verify) facts.push(`<b>E-Verify</b> enrolled (STEM OPT possible)`);
      if (p.cap_exempt) facts.push(`<b>Cap-exempt</b> likely`);
      rows.push(`<div class="tailor-cp-row"><div class="tailor-cp-co">${esc(d.matched_name || LAST.company)}</div>${facts.map(f => `<div>${f}</div>`).join("")}<div class="tailor-cp-foot">Public USCIS/DOL records, not a promise this role sponsors.</div></div>`);
    } else if (d && d.ok !== false) {
      rows.push(`<div class="tailor-cp-row">No H-1B or green-card record for this employer.</div>`);
    } else if (d && d.error === "cant_reach_app") {
      rows.push(`<div class="tailor-cp-row">Open the SponsorJobs app to see sponsor records.</div>`);
    }
    if (!rows.length) rows.push(`<div class="tailor-cp-row">Open a job to see its sponsorship facts.</div>`);
    return `<div class="tailor-cp-hd"><img src="${chrome.runtime.getURL("icons/icon48.png")}" alt=""> SponsorJobs</div>${rows.join("")}
      <a class="tailor-cp-open" href="http://127.0.0.1:57000/" target="_blank" rel="noopener">Open SponsorJobs ↗</a>`;
  }
  function paintCorner() {
    if (!document.body || !chrome.runtime || !chrome.runtime.getURL) return;
    let c = document.querySelector(".tailor-corner");
    if (!c) {
      c = document.createElement("div");
      c.className = "tailor-corner";
      c.innerHTML = `<button class="tailor-corner-btn" type="button" title="SponsorJobs: visa sponsorship facts for this job"><img src="${chrome.runtime.getURL("icons/icon48.png")}" alt="SponsorJobs"></button><div class="tailor-corner-panel" hidden></div>`;
      document.body.appendChild(c);
      c.querySelector(".tailor-corner-btn").addEventListener("click", () => {
        const p = c.querySelector(".tailor-corner-panel");
        p.hidden = !p.hidden;
        if (!p.hidden) p.innerHTML = cornerPanelHTML();
      });
    }
    const p = c.querySelector(".tailor-corner-panel");
    if (p && !p.hidden) p.innerHTML = cornerPanelHTML();
    const A = globalThis.TailorAdStance;
    c.classList.toggle("is-no", !!(LAST.stance && A && LAST.stance.stance === A.NOT_OFFERED));
    c.classList.toggle("is-yes", !!(LAST.stance && A && LAST.stance.stance === A.OFFERED));
  }

  let last = null;
  function run() {
    paintCorner();
    const { company, anchor, location: ldLoc } = pickTarget();
    if (company && anchor) {
      // Prefer the location from structured data (same source as the company, same
      // reliability); fall back to scraping the page for it.
      const location = ldLoc || pickLocation();
      const key = company + "|" + location;   // a new role at the same employer must re-ask
      if (!(key === last && document.querySelector(".tailor-badges"))) {
        last = key;
        try {
          chrome.runtime.sendMessage({ type: "lookup", company, location }, (data) => {
            if (chrome.runtime.lastError) return;   // worker asleep; next mutation retries
            render(anchor, data || { ok: false });
            lastStanceKey = ""; mountStance();
            // Match pill + skills first, then hang the referral action off that row.
            enhancePosting();
          });
        } catch (_) { /* extension context invalidated on reload */ }
      }
    }
    mountStance();                                   // the ad's own stance, as soon as its text is on the page
    scanList().catch(() => {});                      // P6: in-list badges + filter bar
  }

  let t = null;
  const debounced = () => { clearTimeout(t); t = setTimeout(run, 500); };
  new MutationObserver(debounced).observe(document.body, { childList: true, subtree: true });
  run();
})();
