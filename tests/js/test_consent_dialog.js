// A required privacy notice behind a button (2026-11-19, SAP SuccessFactors at Gainwell): "Read
// and accept the data privacy statement" opens a dialog with Accept and Decline, and Create
// Account is refused until it is accepted. Under the person's consent to required notices
// (accept_required_terms) the shell opens it once and presses the dialog's own Accept; a
// marketing link is never opened; without consent nothing is pressed. Headless Chromium with the
// real agent_view.js and the shell's handleTerms (tests/test_consent_dialog_and_dice.py runs it).
"use strict";
const fs = require("fs");
const http = require("http");
const path = require("path");
const ROOT = path.resolve(__dirname, "..", "..");
const { createAgent } = require(path.join(ROOT, "shell-electron", "agent.js"));
const AGENT = fs.readFileSync(path.join(ROOT, "extension", "agent_view.js"), "utf8");
const CORE = fs.readFileSync(path.join(ROOT, "extension", "autofill_core.js"), "utf8");
const assert = (c, m) => { if (!c) throw new Error(m); };

function findPlaywright() {
  const cands = [process.env.SJ_PLAYWRIGHT_DIR, path.join(ROOT, "shell-electron", "node_modules", "playwright"),
                 path.join(process.env.HOME || "", "sponsorjobs", "shell-electron", "node_modules", "playwright")];
  for (const c of cands) { if (c && fs.existsSync(path.join(c, "package.json"))) return require(c); }
  throw new Error("playwright not found");
}
function serve(dir) {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      const u = new URL(req.url, "http://x");
      const f = path.join(dir, path.normalize(decodeURIComponent(u.pathname)).replace(/^[/\\]+/, ""));
      if (!f.startsWith(dir) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); res.end(); return; }
      res.writeHead(200, { "Content-Type": f.endsWith(".html") ? "text/html; charset=utf-8" : "text/css" });
      fs.createReadStream(f).pipe(res);
    });
    srv.listen(0, "127.0.0.1", () => resolve(srv));
  });
}

(async () => {
  const srv = await serve(path.join(ROOT, "tests", "fixtures", "forms"));
  const base = "http://127.0.0.1:" + srv.address().port + "/sapsf_privacy.html";
  const { chromium } = findPlaywright();
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
    const load = async (q) => {
      await page.goto(base + (q || ""));
      await page.evaluate(CORE); await page.evaluate(AGENT);
      await page.evaluate("window.TailorAgent.view({})");          // numbers the page's elements
    };
    const setup = async (decision, opts) => {
      const o = opts || {};
      await load(o.q);
      const posts = [], gets = [];
      const wc = { getURL: () => page.url(), isDestroyed: () => false, on: () => {}, removeListener: () => {}, executeJavaScript: (expr) => page.evaluate(expr) };
      const agent = createAgent({
        noPolling: true, getPane: () => null, acquireDebugger: () => null, frames: {},
        autofillCore: () => CORE, agentViewSource: () => AGENT, sendProgress: () => {},
        getApp: async (p) => { gets.push(p); return p.startsWith("/api/profile/autofill") ? { ok: true, fields: o.country === undefined ? { country: "United States" } : o.country } : { ok: true }; },
        postApp: async (p, b) => {
          posts.push([p, b]);
          if (p === "/api/autoapply/agent/terms" && b && b.check_only) return decision;
          if (p === "/api/autoapply/agent/terms") return { ok: false, reason: "parked" };
          return { ok: true };
        },
      });
      const run = { recordId: 30, company: "Brightwater", termsTries: new Map(), termsOk: new Map(), accepted: [],
                    st: { stop: false }, batch: true, telegram: false, parkInfo: () => ({}) };
      return { agent, wc, run, posts, gets };
    };
    const fx = () => page.evaluate("window.fxValues()");
    const ACCEPT = { ok: true, decision: "accept", via: "always" };

    // the view lists the notice behind its button, never the marketing link
    await load();
    const items = await page.evaluate("window.TailorAgent.termsItems()");
    assert(items.length === 1 && items[0].kind === "opener" && /privacy statement/i.test(items[0].title), "the opener: " + JSON.stringify(items));
    const loose = await page.evaluate("window.TailorAgent.termsItems(true)");
    assert(loose.length === 1 && loose[0].kind === "opener", "loose adds no marketing link: " + JSON.stringify(loose));

    // the create-account form's required country-of-residence list, never a citizenship one,
    // set from the profile's country by the list's own option (the engine stands back on such a page)
    await load();
    await page.evaluate(`(() => { const s = document.createElement("select"); s.id = "cit"; s.required = true;
      s.innerHTML = '<option value="">Select</option><option value="GH">Ghana</option>';
      const l = document.createElement("label"); l.htmlFor = "cit"; l.textContent = "Country of citizenship*";
      document.getElementById("fbclc").append(l, s); })()`);
    await page.evaluate("window.TailorAgent.view({})");
    const lists = await page.evaluate("window.TailorAgent.residenceLists()");
    assert(lists.length === 1, "the residence list only: " + JSON.stringify(lists));
    assert(await page.evaluate(`window.TailorAgent.setCountry(${lists[0]}, "United States")`) === true && (await fx()).country === "US", "set to US");
    assert(await page.evaluate("document.getElementById('cit').value") === "", "citizenship untouched");

    // 1. consent on: the country of residence from the profile FIRST (the site opens nothing
    //    without it), then the press, the SAP container awaited, its Accept, the hidden box checked
    {
      const { agent, wc, run, posts } = await setup(ACCEPT);
      const r = await agent._test.handleTerms(wc, run);
      const v = await fx();
      assert(r.accepted === 1, "accepted: " + JSON.stringify([r, v]));
      assert(v.country === "US" && v.clicks === 1 && v.focusCountry === 0 && v.opened === 1, "country first, one press: " + JSON.stringify(v));
      assert(v.accepted && !v.declined && v.dpcs === "dpcs-1" && v.alerts === 0 && v.campaign === false, "Accept only, held by the form: " + JSON.stringify(v));
      const rec = posts.find(([p]) => p === "/api/autoapply/agent/terms/accepted");
      assert(rec && rec[1].kind === "opener" && /privacy statement/i.test(rec[1].title) && rec[1].via === "always", "recorded: " + JSON.stringify(rec));
      await page.evaluate("window.TailorAgent.view({})");
      await agent._test.handleTerms(wc, run);
      assert((await fx()).clicks === 1, "never pressed again once accepted");
    }
    // 2. the country list shows up only after the first press and takes the focus: filled, and
    //    the notice pressed ONCE more
    {
      const { agent, wc, run } = await setup(ACCEPT, { q: "?late" });
      const r = await agent._test.handleTerms(wc, run);
      const v = await fx();
      assert(r.accepted === 1 && v.clicks === 2 && v.focusCountry === 1 && v.country === "US" && v.dpcs === "dpcs-1", "late country: " + JSON.stringify([r, v]));
    }
    // 3. no country in the profile: one press, nothing opens, nothing recorded, no loop
    {
      const { agent, wc, run, posts } = await setup(ACCEPT, { q: "?late", country: {} });
      const r = await agent._test.handleTerms(wc, run);
      const v = await fx();
      assert(r.accepted === 0 && v.clicks === 1 && v.opened === 0 && !posts.some(([p]) => p === "/api/autoapply/agent/terms/accepted"), "no country: " + JSON.stringify([r, v]));
      await agent._test.handleTerms(wc, run);
      await agent._test.handleTerms(wc, run);
      assert((await fx()).clicks <= 2, "at most two presses in a run");
    }
    // 4. another required box still empty: the notice waits (the fill comes first); after the
    //    brain reports needs-consent (loose) it is pressed anyway
    {
      const { agent, wc, run } = await setup(ACCEPT, { q: "?empty" });
      let r = await agent._test.handleTerms(wc, run);
      assert(r.accepted === 0 && (await fx()).clicks === 0, "waits for the email box");
      r = await agent._test.handleTerms(wc, run);
      r = await agent._test.handleTerms(wc, run);
      assert(!r.stop, "waiting is not a try: " + JSON.stringify(r));
      r = await agent._test.handleTerms(wc, run, true);
      assert(r.accepted === 1 && (await fx()).clicks === 1, "loose: pressed " + JSON.stringify(r));
    }
    // 5. no standing consent, in a batch: nothing pressed, the job parks on the question
    {
      const { agent, wc, run } = await setup({ ok: true, decision: "ask" });
      const r = await agent._test.handleTerms(wc, run);
      const v = await fx();
      assert(r.park && v.clicks === 0 && v.accepted === false && v.country === "", "nothing touched without consent: " + JSON.stringify([r, v]));
    }
  } finally {
    await browser.close();
    srv.close();
  }
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
