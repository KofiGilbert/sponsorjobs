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
  const url = "http://127.0.0.1:" + srv.address().port + "/sapsf_privacy.html";
  const { chromium } = findPlaywright();
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
    const setup = async (decision) => {
      await page.goto(url);
      await page.evaluate(CORE);
      await page.evaluate(AGENT);
      await page.evaluate("window.TailorAgent.view({})");          // numbers the page's elements
      const posts = [];
      const wc = { getURL: () => page.url(), isDestroyed: () => false, on: () => {}, removeListener: () => {}, executeJavaScript: (expr) => page.evaluate(expr) };
      const agent = createAgent({
        noPolling: true, getPane: () => null, acquireDebugger: () => null, frames: {},
        autofillCore: () => CORE, agentViewSource: () => AGENT, sendProgress: () => {},
        getApp: async () => ({ ok: true }),
        postApp: async (p, b) => {
          posts.push([p, b]);
          if (p === "/api/autoapply/agent/terms" && b && b.check_only) return decision;
          if (p === "/api/autoapply/agent/terms") return { ok: false, reason: "parked" };
          return { ok: true };
        },
      });
      const run = { recordId: 30, company: "Brightwater", termsTries: new Map(), termsOk: new Map(), accepted: [],
                    st: { stop: false }, batch: true, telegram: false, parkInfo: () => ({}) };
      return { agent, wc, run, posts };
    };

    // the view lists it as a notice behind a button, and never the marketing link
    await page.goto(url);
    await page.evaluate(CORE); await page.evaluate(AGENT);
    await page.evaluate("window.TailorAgent.view({})");
    const items = await page.evaluate("window.TailorAgent.termsItems()");
    assert(items.length === 1 && items[0].kind === "opener" && /privacy statement/i.test(items[0].title), "the opener: " + JSON.stringify(items));
    const loose = await page.evaluate("window.TailorAgent.termsItems(true)");
    assert(loose.length === 1 && loose[0].kind === "opener", "loose adds no marketing link: " + JSON.stringify(loose));

    // consent to required notices on: opened once, the dialog's own Accept pressed, recorded
    {
      const { agent, wc, run, posts } = await setup({ ok: true, decision: "accept", via: "always" });
      const r = await agent._test.handleTerms(wc, run);
      const v = await page.evaluate("window.fxValues()");
      assert(r.accepted === 1, "accepted: " + JSON.stringify(r));
      assert(v.opened === 1 && v.accepted === true && v.declined === false && v.alerts === 0, "the dialog's Accept, nothing else: " + JSON.stringify(v));
      const rec = posts.find(([p]) => p === "/api/autoapply/agent/terms/accepted");
      assert(rec && rec[1].kind === "opener" && /privacy statement/i.test(rec[1].title) && rec[1].via === "always", "recorded: " + JSON.stringify(rec));
      assert(run.accepted.length === 1, "named at Ready to send");
      // once per site and notice in a run: never opened again
      await page.evaluate("window.TailorAgent.view({})");
      await agent._test.handleTerms(wc, run);
      assert((await page.evaluate("window.fxValues()")).opened === 1, "opened once");
    }
    // no standing consent, in a batch: nothing is pressed, the job parks on the question
    {
      const { agent, wc, run } = await setup({ ok: true, decision: "ask" });
      const r = await agent._test.handleTerms(wc, run);
      const v = await page.evaluate("window.fxValues()");
      assert(r.park && v.opened === 0 && v.accepted === false, "nothing pressed without consent: " + JSON.stringify([r, v]));
    }
  } finally {
    await browser.close();
    srv.close();
  }
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
