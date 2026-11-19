// The hidden Auto run of 2026-11-19 (tests/test_queue_run_2026_11_19.py runs this): the
// navigation fence. Where a run that leaves its site went is logged and kept on the record; an
// aggregator's ad link adopts the employer site it lands on; a careers site's hand-off to its
// hiring system stays on the run; a third-party survey parks as a person step with Continue.
// Everyone and every site here is made up.
"use strict";
const path = require("path");
const { createAgent, offSiteVerdict, SURVEY_LINE } = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };

// ---- the verdict, pure ------------------------------------------------------------------------
{
  const agg = { aggregator: true };
  // the ad link redirected with no click by the agent: the landing site is adopted
  assert(offSiteVerdict({ host: "careers.brightwater.example", fence: agg, fromHost: "adzuna.com", clicks: [] }) === "adopt", "no click adopts");
  // one click on the ad's own Apply link
  assert(offSiteVerdict({ host: "careers.brightwater.example", fence: agg, fromHost: "adzuna.com", clicks: ["Apply for this job"] }) === "adopt", "Apply click adopts");
  // a click on anything else, or two clicks: no
  assert(offSiteVerdict({ host: "careers.brightwater.example", fence: agg, fromHost: "adzuna.com", clicks: ["Similar jobs"] }) === "left", "other click");
  assert(offSiteVerdict({ host: "careers.brightwater.example", fence: agg, fromHost: "adzuna.com", clicks: ["Apply", "Apply"] }) === "left", "two clicks");
  // once only: a second hop is not adopted
  assert(offSiteVerdict({ host: "elsewhere.example", fence: agg, adopted: true, fromHost: "adzuna.com", clicks: [] }) === "left", "once");
  // only from the aggregator itself
  assert(offSiteVerdict({ host: "elsewhere.example", fence: agg, fromHost: "careers.brightwater.example", clicks: [] }) === "left", "from the employer site");
  // never a prohibited site, however it got there
  for (const h of ["linkedin.com", "www.indeed.com".replace(/^www\./, ""), "uk.linkedin.com", "glassdoor.com"]) {
    assert(offSiteVerdict({ host: h, fence: agg, fromHost: "adzuna.com", clicks: [] }) === "left", "prohibited " + h);
  }
  // not an aggregator's link: never adopted
  assert(offSiteVerdict({ host: "elsewhere.example", fence: { aggregator: false }, fromHost: "jobs.brightwater.example", clicks: [] }) === "left", "not aggregator");
  // a survey host, or a click named like one, is the person's survey
  assert(offSiteVerdict({ host: "wotcgs.ey.com", fence: {}, fromHost: "brightwater.wd1.myworkdayjobs.com" }) === "survey", "wotc host");
  assert(offSiteVerdict({ host: "assess.vendor.example", fence: {}, fromHost: "brightwater.wd1.myworkdayjobs.com", lastClick: "Take Assessment" }) === "survey", "assessment click");
  assert(offSiteVerdict({ host: "linkedin.com", fence: {}, lastClick: "Take Assessment" }) === "left", "never LinkedIn");
  assert(offSiteVerdict({ host: "", fence: agg }) === "left", "no host");
  assert(/short survey on another site \(wotcgs\.ey\.com\)\. Do it yourself, then press Continue and I will finish\./.test(SURVEY_LINE("wotcgs.ey.com")), "survey line");
}

// ---- a run, end to end, with a fake pane ---------------------------------------------------------
function setup(o) {
  const posts = [], progress = [], opened = [];
  const wc = {
    url: "", getURL: () => wc.url, isDestroyed: () => false, on: () => {}, removeListener: () => {}, session: null,
    executeJavaScript: async (expr) => {
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet.at")) return "5000|complete";
      if (expr.includes("pageSignature")) return { success: true };
      return true;
    },
  };
  const pane = { webContents: wc };
  const deps = {
    noPolling: true,
    getPane: () => pane,
    openInPane: (u) => { opened.push(u); wc.url = (o.redirect && o.redirect[u]) || u; },
    waitPaneLoad: async () => {},
    autofillCore: () => "", agentViewSource: () => "",
    runAutoApplyOnPane: async () => ({ ok: true, filled: 0, left: [] }),
    sendProgress: (info) => progress.push(info),
    acquireDebugger: () => null, frames: {},
    applyDecision: async () => ({ fill: true }),
    isNewPage: () => false, attachResume: async () => false,
    getApp: async (p) => {
      if (p.startsWith("/api/autoapply/agent/parked")) return { ok: true, parked: [] };
      if (p === "/api/autoapply/mode") return { ok: true, mode: "auto", telegram_asks: false };
      if (p.startsWith("/api/autoapply/agent/allowed-hosts")) {
        const u = decodeURIComponent(p.split("url=")[1] || "");
        const h = new URL(u).hostname.replace(/^www\./, "");
        return { ok: true, hosts: [h, h.split(".").slice(-2).join(".")], aggregator: /adzuna\./.test(h) };
      }
      return { ok: true };
    },
    postApp: async (p, b) => { posts.push([p, b]); return { ok: true }; },
  };
  return { agent: createAgent(deps), posts, progress, opened, wc };
}
const leftPosts = (posts) => posts.filter(([p, b]) => p === "/api/autoapply/agent/left-site" && !b.clear).map(([, b]) => b);

(async () => {
  // Adzuna's ad link redirects to the employer's site with no click: adopted, the run goes on.
  {
    const ad = "https://www.adzuna.com/land/ad/1?se=x";
    const { agent, posts } = setup({ redirect: { [ad]: "https://careers.brightwater.example/apply/9" } });
    const r = await agent.run("28", { url: ad, mode: "batch", quiet: true });
    assert(r.ok && r.submitted, "adopted run went on: " + JSON.stringify(r));
    assert(!leftPosts(posts).length, "no left-site");
  }
  // A careers site that sends the run to an unrelated site: stopped, and WHERE is kept.
  {
    const job = "https://jobs.brightwater.example/job/1";
    const { agent, posts, progress } = setup({ redirect: { [job]: "https://shop.unrelated.example/cart?token=secret" } });
    const r = await agent.run("30", { url: job, mode: "batch", quiet: true });
    assert(!r.ok && r.reason === "left-site", "left: " + JSON.stringify(r));
    const lp = leftPosts(posts);
    assert(lp.length === 1 && lp[0].host === "shop.unrelated.example" && lp[0].from_host === "jobs.brightwater.example"
           && lp[0].kind === "left" && lp[0].step === 1, "recorded: " + JSON.stringify(lp));
    assert(!JSON.stringify(lp).includes("token"), "never the address with its tokens");
    const stop = progress.find((x) => x.state === "stopped" && x.reason === "left-site");
    assert(stop && stop.host === "shop.unrelated.example" && /shop\.unrelated\.example/.test(stop.message), "the stop names the host");
    assert(posts.some(([p, b]) => p === "/api/autoapply/agent/left-site" && b.clear), "a new run clears the old one");
  }
  // A SuccessFactors careers site handing off to sapsf.com (record 30's shape): stays on the run.
  {
    const job = "https://jobs.brightwater.example/job/2";
    const { agent, posts } = setup({ redirect: { [job]: "https://career41.sapsf.com/careers?company=brightwater" } });
    const r = await agent.run("31", { url: job, mode: "batch", quiet: true });
    assert(r.ok && r.submitted && !leftPosts(posts).length, "sapsf hand-off: " + JSON.stringify(r));
  }
  // An Oracle Recruiting Cloud tenant: allowed by its shape, not all of oraclecloud.com.
  {
    const job = "https://careers.brightwater.example/job/3";
    const { agent } = setup({ redirect: { [job]: "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/3" } });
    const r = await agent.run("32", { url: job, mode: "batch", quiet: true });
    assert(r.ok && r.submitted, "oracle hand-off: " + JSON.stringify(r));
    const job2 = "https://careers.brightwater.example/job/4";
    const s2 = setup({ redirect: { [job2]: "https://objectstorage.us-ashburn-1.oraclecloud.com/n/x" } });
    const r2 = await s2.agent.run("33", { url: job2, mode: "batch", quiet: true });
    assert(r2.reason === "left-site", "not any oraclecloud.com: " + JSON.stringify(r2));
  }
  // A required tax-credit survey on another site: parked as a person step with Continue, the
  // reason says so, and Continue opens the application's own page, never the survey.
  {
    const job = "https://brightwater.wd1.myworkdayjobs.com/en-US/Search/job/1/apply";
    const { agent, posts, progress } = setup({ redirect: { [job]: "https://wotcgs.ey.com/survey?id=1" } });
    const r = await agent.run("34", { url: job, mode: "batch", quiet: true, batchAt: { i: 1, n: 8 } });
    assert(r.parked && r.kind === "person", "parked: " + JSON.stringify(r));
    const pk = posts.find(([p]) => p === "/api/autoapply/agent/park");
    assert(pk && pk[1].kind === "person" && pk[1].message === SURVEY_LINE("wotcgs.ey.com"), "person park with the reason");
    assert(pk[1].page_url === job, "Continue opens the application page: " + pk[1].page_url);
    const lp = leftPosts(posts);
    assert(lp.length === 1 && lp[0].kind === "survey" && lp[0].host === "wotcgs.ey.com", "kept on the record");
    const said = progress.find((x) => x.state === "parked");
    assert(said && /Moving on\./.test(said.message) && /short survey/.test(said.message), "the batch moves on: " + (said && said.message));
    assert(!progress.some((x) => x.reason === "left-site"), "never left-site");
  }
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
