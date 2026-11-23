// Which hosts are job application sites, for request signing (src/botauth.ts).
//
// A port of the lists the desktop app's navigation fence uses (submit/policy.py ATS_HOSTS,
// ATS_HOST_PATTERNS, SHARED_HOSTING and registrable_domain; submit/allowlist.py PROHIBITED_HOSTS).
// Keep them in step: the app only asks for signatures inside a run's fence, and the Worker
// refuses anything outside the same rules.

/** Hiring systems' own domains (and their sub-domains): signed without further question. */
export const ATS_HOSTS: readonly string[] = [
  "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "recruitee.com", "smartrecruiters.com",
  "myworkdayjobs.com", "icims.com", "taleo.net", "successfactors.com", "jobvite.com",
  "bamboohr.com", "paylocity.com", "ultipro.com", "adp.com",
  "sapsf.com", "sapsf.eu", "successfactors.eu", "myworkdaysite.com", "myworkday.com",
  "jibeapply.com", "phenompeople.com", "avature.net", "eightfold.ai", "brassring.com", "csod.com",
  "dayforcehcm.com", "paycomonline.net", "ukg.net", "applytojob.com", "breezy.hr", "teamtailor.com",
  "personio.de", "personio.com", "pinpointhq.com", "jazzhr.com", "applicantpro.com", "isolvedhire.com",
  "hrmdirect.com", "zohorecruit.com", "freshteam.com", "governmentjobs.com", "paradox.ai",
];

/** Oracle Recruiting Cloud tenants: only this shape of oraclecloud.com, which also hosts others. */
export const ATS_HOST_PATTERNS: readonly RegExp[] = [/(^|\.)fa(\.[a-z0-9-]+)?\.oraclecloud\.com$/];

/** Job boards whose terms forbid automation: never signed for (CLAUDE.md 7). */
export const PROHIBITED_HOSTS: readonly string[] = ["linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
  "monster.com", "dice.com"];

/** One registrable domain, many unrelated owners: only the very host is the site's own. */
export const SHARED_HOSTING: readonly string[] = [
  "oraclecloud.com", "amazonaws.com", "cloudfront.net", "azurewebsites.net", "azureedge.net",
  "windows.net", "herokuapp.com", "github.io", "gitlab.io", "netlify.app", "vercel.app",
  "pages.dev", "workers.dev", "web.app", "firebaseapp.com", "appspot.com",
  "googleusercontent.com", "blogspot.com", "wordpress.com", "wixsite.com", "squarespace.com",
  "sharepoint.com", "myshopify.com", "webflow.io", "notion.site", "onrender.com", "fly.dev",
];

const TWO_PART_SUFFIXES = new Set(["co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp",
  "co.in", "com.br", "com.mx", "co.za", "com.sg", "com.hk", "co.kr", "com.tr"]);
const REGISTRY_LABELS = new Set(["co", "com", "org", "net", "ac", "gov", "edu", "or", "ne"]);

/** "acme.com:8443" -> "acme.com"; a leading "www." is dropped, as the app does. */
export function bareHost(authority: string): string {
  const h = authority.replace(/:\d{1,5}$/, "").toLowerCase();
  return h.startsWith("www.") ? h.slice(4) : h;
}

/** acme.wd5.myworkdayjobs.com -> myworkdayjobs.com, careers.acme.co.uk -> acme.co.uk. */
export function registrableDomain(host: string): string {
  const labels = host.toLowerCase().split(".").filter(Boolean);
  if (labels.length <= 2) return labels.join(".");
  const last2 = labels.slice(-2).join(".");
  const twoPart = TWO_PART_SUFFIXES.has(last2) || (labels[labels.length - 1]!.length === 2 && REGISTRY_LABELS.has(labels[labels.length - 2]!));
  return labels.slice(twoPart ? -3 : -2).join(".");
}

const within = (h: string, list: readonly string[]) => list.some((a) => h === a || h.endsWith(`.${a}`));

export const isAtsHost = (host: string): boolean => {
  const h = bareHost(host);
  return within(h, ATS_HOSTS) || ATS_HOST_PATTERNS.some((re) => re.test(h));
};

export const isProhibitedHost = (host: string): boolean => within(bareHost(host), PROHIBITED_HOSTS);

/** The key a site is counted under per day: its registrable domain, or the host itself on shared
 * hosting (two tenants of one cloud domain are two sites). */
export function siteKey(site: string): string {
  const h = bareHost(site);
  const reg = registrableDomain(h);
  return SHARED_HOSTING.includes(reg) ? h : reg;
}

/** Is `authority` part of the application site `site` (the run's apply host)? The same host, or
 * the same registrable domain (on shared hosting: the same host or one below it). */
export function onSite(authority: string, site: string): boolean {
  const a = bareHost(authority);
  const s = bareHost(site);
  if (!a || !s) return false;
  const key = siteKey(s);
  return a === key || a.endsWith(`.${key}`);
}
