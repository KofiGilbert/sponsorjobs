// Home's "Today" strip: loading -> cards, loading -> empty, loading -> failed (issue #277).
//
// After a restart the page can paint before the engine has its feed. The request then fails and
// the strip used to blank itself, so it showed a heading, a lone "See all jobs" link and nothing
// else, which reads as "there are no roles" rather than "not yet". These three runs pin the
// states against the real app.js in a real Chromium: every /api/jobs?home=1 answer is stubbed
// here, so nothing depends on the engine having a feed.
//
// Usage: node home_today_harness.js <baseUrl> <playwrightDir>
"use strict";
const path = require("path");

const [, , BASE, PW_DIR] = process.argv;
if (!BASE || !PW_DIR) { console.error("need <baseUrl> <playwrightDir>"); process.exit(2); }
const { chromium } = require(path.join(PW_DIR, "index.js"));

const assert = (c, m) => { if (!c) throw new Error(m); };
const SEL = "#homeToday";

const JOB = {
  source_id: "demo::analyst", title: "Data Analyst", company: "Acme Robotics",
  location: "Boston, MA", salary: "$120k", visa: [], tags: [],
};

/** Load Home with /api/jobs?home=1 answered by `reply`, and report what the strip did. */
async function run(browser, reply, { hold = 150, settle = 2600 } = {}) {
  const page = await browser.newPage({ viewport: { width: 1893, height: 1183 } });
  const seen = { skeletonWhileLoading: false, calls: 0 };
  await page.route("**/api/jobs?home=1", async (route) => {
    seen.calls++;
    // Hold the answer long enough to look at what is on screen meanwhile.
    await new Promise((r) => setTimeout(r, hold));
    await reply(route, seen.calls);
  });
  await page.goto(BASE, { waitUntil: "domcontentloaded" });

  // While the request is in flight the strip must already show skeleton rows.
  for (let i = 0; i < 40 && !seen.skeletonWhileLoading; i++) {
    seen.skeletonWhileLoading = await page.evaluate(
      (s) => !!document.querySelector(`${s} .feed-skeleton`), SEL).catch(() => false);
    if (!seen.skeletonWhileLoading) await page.waitForTimeout(25);
  }
  await page.waitForTimeout(hold + settle);        // past the retry backoff
  const out = await page.evaluate((s) => {
    const el = document.querySelector(s);
    return { html: el ? el.innerHTML : "", text: el ? el.textContent.trim() : "",
             cards: el ? el.querySelectorAll(".today-card").length : -1,
             skeleton: el ? el.querySelectorAll(".feed-skeleton").length : -1 };
  }, SEL);
  await page.close();
  return { ...out, ...seen };
}

(async () => {
  const browser = await chromium.launch();

  // 1. loading -> cards
  {
    const r = await run(browser, (route) =>
      route.fulfill({ status: 200, contentType: "application/json",
                      body: JSON.stringify({ jobs: [JOB, JOB, JOB], fit_ranked: true }) }));
    assert(r.skeletonWhileLoading, "no skeleton while the request was in flight");
    assert(r.cards === 3, `expected 3 cards, got ${r.cards}: ${r.text.slice(0, 120)}`);
    assert(r.skeleton === 0, "the skeleton must be replaced once the cards land");
  }

  // 2. loading -> a real empty result keeps its own one-line message
  {
    const r = await run(browser, (route) =>
      route.fulfill({ status: 200, contentType: "application/json",
                      body: JSON.stringify({ jobs: [], fit_ranked: true }) }));
    assert(r.skeletonWhileLoading, "no skeleton before the empty answer");
    assert(r.cards === 0 && r.skeleton === 0, "empty must clear both");
    assert(/No roles yet/i.test(r.text), `expected the empty line, got: ${r.text.slice(0, 120)}`);
  }

  // 3. loading -> failed: it retries, then says so instead of going blank
  {
    // HOME_TODAY_BACKOFF is 600 + 1200 + 2400 ms, so give the whole chain room to finish.
    const r = await run(browser, (route) => route.abort("failed"), { hold: 0, settle: 6500 });
    assert(r.calls > 1, `a failure must be retried, saw ${r.calls} call(s)`);
    assert(r.cards === 0, "no cards from a failed load");
    assert(r.text.length > 0, "a failed load must not leave the strip blank, that is the bug");
    assert(/Could not load roles/i.test(r.text), `expected the failure line, got: ${r.text.slice(0, 120)}`);
  }

  await browser.close();
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || String(e)); process.exit(1); });
