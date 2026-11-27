// The guided ask and the guided walk on a real browser page (2026-11-27, the owner on InStride's
// Greenhouse form). Headless Chromium, a local copy of Greenhouse's job board form
// (tests/fixtures/forms/gh_react_select.html: InStride's and Stripe's question shapes in
// react-select lists that hide their search box once a choice is made), the real fill engine
// (extension/autofill_core.js), the real page view (extension/agent_view.js) and the real pointer
// (extension/pointer_overlay.js), driven by the real apply agent (shell-electron/agent.js) with a
// small fake of the engine's ask. Never a real site, nothing is submitted.
//   1. The person picks with the mouse (trusted clicks): each list closes, the ask moves on by
//      itself to the next question, and after the last one the pointer rests on Submit with
//      "All done". The choice is kept in the site's own words ("No", not "no").
//   2. Next on the bar: the pointer is on the next field in well under 600 ms, even with the
//      ask's own 1 s look at the page in between.
//   3. The guided walk's Next / Previous (needsStep): the page jumps, the pointer lands on the
//      field, all under 600 ms.
//   4. A pointer label near the pane's right edge stays whole inside the page.
// Prints one JSON line {ok, checks, failures}; tests/test_guide_replay.py runs it.
"use strict";
const path = require("path"), fs = require("fs"), os = require("os");
process.env.TMPDIR = fs.mkdtempSync(path.join(os.tmpdir(), "sj-guide-replay-"));
process.env.SJ_AGENT_PANE_MS = "300";
process.env.SJ_AGENT_FILL_MS = "300";
process.env.SJ_AGENT_RETRY_MS = "20";
process.env.SJ_AGENT_GUIDE_MS = "1000";          // the real look-again pace: Next must not wait for it
const ROOT = path.join(__dirname, "..", "..");
function findPlaywright() {
  for (const c of [process.env.SJ_PLAYWRIGHT_DIR, path.join(ROOT, "shell-electron", "node_modules", "playwright"),
                   path.join(process.env.HOME || "", "sponsorjobs", "shell-electron", "node_modules", "playwright")]) {
    if (c && fs.existsSync(path.join(c, "package.json"))) return require(c);
  }
  throw new Error("playwright not found");
}
const { chromium } = findPlaywright();
const { createAgent } = require(path.join(ROOT, "shell-electron", "agent.js"));
const SCRIPTS = ["autofill_core.js", "agent_view.js", "pointer_overlay.js"].map((f) => fs.readFileSync(path.join(ROOT, "extension", f), "utf8"));
const PAGE = "file://" + path.join(ROOT, "tests", "fixtures", "forms", "gh_react_select.html");
const Q = {
  friends: "Do you have any friends, relatives, or acquaintances working for Example Co?",
  remote: "If this role offers the option to work from a remote location, do you plan to work remotely?",
  privacy: "By submitting my application, I agree to Example Co's Job Applicant Privacy Policy",
};
const IDS = { question_101: "friends", question_102: "remote", question_103: "privacy" };
const checks = [], failures = [];
const check = (ok, what, more) => { checks.push(what); if (!ok) failures.push(what + (more ? ": " + more : "")); };

async function freshPage(browser, viewport) {
  const page = await browser.newPage({ viewport: viewport || { width: 900, height: 700 } });
  await page.goto(PAGE);
  for (const s of SCRIPTS) await page.addScriptTag({ content: s });
  return page;
}

// The engine's ask, as ui/app.py answers it (the same small fake as tests/js/test_agent_guide.js).
function fakeEngine() {
  const E = { asks: [], answers: [], items: null, closed: false };
  E.post = (p, b) => {
    if (p === "/api/autoapply/agent/ask") {
      if (b.known_only) return { ok: false, answers: {}, origins: {}, select: {}, leave: [], open: (b.questions || []).map((x) => x.question) };
      E.asks.push(b);
      E.items = (b.questions || []).map((x) => ({ question: x.question, answer: null, left: false }));
      return { ok: false, id: b.id, reason: "waiting" };
    }
    if (p === "/api/autoapply/agent/answer") {
      E.answers.push(b);
      const it = E.items[b.index];
      if (b.action === "leave") { it.answer = "Left empty"; it.left = true; } else { it.answer = b.answer; it.left = false; }
      return { ok: true, done: E.items.every((x) => x.answer) };
    }
    if (p === "/api/autoapply/agent/ask/wait") {
      if (E.closed) return { ok: false, reason: "stopped" };
      const got = {};
      for (const x of E.items) if (x.answer && !x.left) got[x.question] = x.answer;
      if (E.items.every((x) => x.answer)) return { ok: true, answers: got, left: E.items.filter((x) => x.left).map((x) => x.question) };
      return { ok: false, reason: "waiting", partial: got };
    }
    if (p === "/api/autoapply/agent/ask/close") { E.closed = true; return { ok: true }; }
    return null;
  };
  return E;
}

// The pane: the page itself for everything the guided ask reads or draws; a few shell-only
// questions answered the way a plain one-page form answers them.
function pane(page, P) {
  const wc = {
    getURL: () => "https://job-boards.greenhouse.io/example/jobs/1", isDestroyed: () => false, on: () => {}, removeListener: () => {},
    session: null, reload: () => {}, getZoomFactor: () => 1, sendInputEvent: () => {},
    navigationHistory: { canGoBack: () => false, goBack: () => {}, getActiveIndex: () => 0, getEntryAtIndex: () => ({ url: "" }) },
    executeJavaScript: async (expr) => {
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet.at")) return "5000|complete";
      if (expr.includes("__sjSubmitAt")) return { url: wc.getURL() };
      if (expr.includes("termsItems")) return [];
      if (expr.includes("crossOriginFrames")) return 0;
      if (expr.includes("resumeInputOpen") || expr.includes("hasResumeUpload")) return false;
      if (expr.includes("pageShape")) return { fields: 5, buttons: 1, skeleton: false, words: 80 };
      if (expr.includes("TailorAutofill.leftover()")) return [];
      if (expr.includes("pageCheck")) return { required_empty: [], errors: [] };
      if (expr.includes("innerText || el.value")) return "Submit application";
      if (expr.includes("TailorAgent.act(")) { P.acts.push(expr); return { ok: true }; }
      const real = /TailorAgent\.view\(|\/\*sj-(highlight|point|choice|value|unhighlight|tidy|all-done)\*\/|pageSignature/.test(expr);
      if (!real) return true;
      const out = await page.evaluate(expr);
      if (expr.includes("/*sj-highlight*/")) {
        const i = Number(/element\((\d+)\)/.exec(expr)[1]);
        const id = await page.evaluate(`(window.TailorAgent.element(${i}) || {}).id || ""`);
        P.lit.push({ at: Date.now(), field: IDS[id] || id });
        if (P.onLight) setTimeout(() => P.onLight(IDS[id] || id).catch((e) => failures.push("pick: " + e.message)), 30);
      }
      return out;
    },
  };
  return { webContents: wc };
}

function agentFor(page, P, E, progress) {
  const thePane = pane(page, P);
  return createAgent({
    noPolling: true,
    getPane: () => thePane, openInPane: () => {}, waitPaneLoad: async () => {},
    autofillCore: () => "", agentViewSource: () => "",
    runAutoApplyOnPane: async () => {
      const vals = await page.evaluate("window.fxValues()");
      return { ok: true, filled: 2, left: Object.keys(Q).filter((k) => !vals[k]).map((k) => Q[k]) };
    },
    sendProgress: (info) => progress.push(JSON.parse(JSON.stringify(info))),
    setQuiet: () => {}, acquireDebugger: () => null, frames: {},
    applyDecision: async () => ({ fill: true, click_submit: false }),
    isNewPage: () => true, attachResume: async () => false,
    getApp: async (p) => (p.startsWith("/api/autoapply/agent/parked") ? { ok: true, parked: [] }
      : p === "/api/autoapply/mode" ? { ok: true, mode: "assisted", telegram_asks: false } : { hosts: ["job-boards.greenhouse.io"] }),
    postApp: async (p, b) => {
      if (p === "/api/autoapply/agent/step") return { ok: true, summary: "Working on it", actions: [], done: { outcome: "ready_to_submit" }, cost: 0.01 };
      if (p === "/api/autoapply/agent/critic") return { ok: true, issues: [] };
      return E.post(p, JSON.parse(JSON.stringify(b || {}))) || { ok: true };
    },
  });
}

// The person's own pick: a real mouse press on the list, then on the choice (trusted events).
async function pick(page, id, choice) {
  await page.click(`#${id}`, { force: true });
  await page.waitForSelector(".select__option");
  await page.locator(".select__option", { hasText: choice }).first().click();
}

// A run that never moves on would wait an hour for the person: here it fails after 20 s instead.
async function capped(agent, id) {
  let t = null;
  const out = await Promise.race([
    agent.run(id, { url: "https://job-boards.greenhouse.io/example/jobs/1", lane: "assisted", company: "Example Co" }),
    new Promise((res) => { t = setTimeout(() => { try { agent.stop(); } catch (e) { /* fine */ } res({ ok: false, reason: "hung" }); }, 20000); }),
  ]);
  clearTimeout(t);
  return out;
}

const guides = (progress) => progress.filter((x) => x.state === "guide").map((x) => x.message);

(async () => {
  const browser = await chromium.launch();
  try {
    // ---- 1. picks with the mouse: each list closes and the ask moves on by itself ----------------
    {
      const page = await freshPage(browser);
      const P = { acts: [], lit: [] }, E = fakeEngine(), progress = [];
      const want = { friends: "No", remote: "Yes, I intend to work remotely.", privacy: "Yes" };
      const ids = { friends: "question_101", remote: "question_102", privacy: "question_103" };
      const menuAfter = {};
      P.onLight = async (field) => {
        if (!want[field] || menuAfter[field] !== undefined) return;
        menuAfter[field] = null;
        await pick(page, ids[field], want[field]);
        await page.waitForTimeout(80);
        menuAfter[field] = await page.evaluate("!!document.querySelector('.select__menu')");
      };
      const agent = agentFor(page, P, E, progress);
      const t0 = Date.now();
      const r = await capped(agent, 1);
      check(r && r.ready === true, "the run reaches Ready to send", JSON.stringify(r));
      check(Date.now() - t0 < 15000, "and does not hang", (Date.now() - t0) + " ms");
      const ans = Object.fromEntries(E.answers.map((a) => [a.question, a.answer]));
      check(ans[Q.friends] === "No" && ans[Q.remote] === "Yes, I intend to work remotely." && ans[Q.privacy] === "Yes",
            "every pick is taken, in the site's own words", JSON.stringify(E.answers));
      check(E.answers.every((a) => a.on_page === true && !a.action), "each one answered on the page, none passed with Next", JSON.stringify(E.answers));
      const g = guides(progress);
      check(g.length === 4 && /1 of 3/.test(g[0]) && /2 of 3/.test(g[1]) && /3 of 3/.test(g[2]) && /All done/.test(g[3]),
            "the walk moves on by itself, then All done", JSON.stringify(g));
      check(JSON.stringify(P.lit.map((x) => x.field).filter((f, i, a) => a.indexOf(f) === i)) === JSON.stringify(["friends", "remote", "privacy"]),
            "each field lit in order", JSON.stringify(P.lit));
      check(Object.values(menuAfter).every((x) => x === false), "each list closed after the pick", JSON.stringify(menuAfter));
      check(!(await page.evaluate("!!document.querySelector('.select__menu')")), "no list open at the end");
      const ptr = await page.evaluate("window.TailorPointer && window.TailorPointer.state()");
      check(ptr && /All done/.test(ptr.label), "the pointer rests on Submit with All done", JSON.stringify(ptr));
      check(!(await page.evaluate("window.__submits || 0")) && !P.acts.length, "nothing submitted, nothing changed by the shell");
      await page.close();
    }

    // ---- 2. Next on the bar: on to the next field at once -----------------------------------------
    {
      const page = await freshPage(browser);
      const P = { acts: [], lit: [] }, E = fakeEngine(), progress = [];
      let agent = null, nextAt = 0, done = false;
      P.onLight = async (field) => {
        if (field === "friends" && !nextAt) { await new Promise((r) => setTimeout(r, 1200)); nextAt = Date.now(); agent.guideNext(); }
        else if (field === "remote" && !done) { done = true; await pick(page, "question_102", "No, I intend to work from an office location."); }
        else if (field === "privacy") await pick(page, "question_103", "Yes");
      };
      agent = agentFor(page, P, E, progress);
      await capped(agent, 2);
      const lit2 = P.lit.find((x) => x.field === "remote" && x.at >= nextAt);
      const ms = lit2 ? lit2.at - nextAt : -1;
      check(ms >= 0 && ms < 600, "Next: the next field is lit in under 600 ms", ms + " ms");
      check(E.answers[0] && E.answers[0].action === "leave" && E.answers[0].question === Q.friends, "Next passes the field", JSON.stringify(E.answers[0]));
      await page.close();
    }

    // ---- 3. the guided walk's Next / Previous: the page jumps, the pointer lands, under 600 ms -----
    {
      const page = await freshPage(browser);
      const r = await page.evaluate(async () => {
        const T = window.TailorAutofill, P = window.TailorPointer;
        const refs = T.extractForm({ onlyEmpty: true }).filter((f) => f.required).map((f) => f.ref);
        refs.forEach((ref) => T.markNeeds(ref));
        window.scrollTo(0, 0);
        const out = [];
        for (const dir of [0, 1, 1, -1]) {
          const t0 = performance.now();
          const s = await T.needsStep(dir);
          const ms = performance.now() - t0;
          const el = document.activeElement;
          const box = (el.closest(".select__control") || el).getBoundingClientRect();
          const st = P.state();
          out.push({ ms: Math.round(ms), label: s.label, inView: box.top >= 0 && box.bottom <= innerHeight,
                     near: !!st && Math.abs(st.y - (box.top + box.height / 2)) < 60 && st.x >= box.left - 4 && st.x <= box.right + 360 });
        }
        return { n: refs.length, out };
      });
      check(r.n >= 3, "the walk has the three lists", JSON.stringify(r));
      for (const [i, s] of r.out.entries()) {
        check(s.ms < 600, `walk step ${i}: under 600 ms`, s.ms + " ms");
        check(s.inView && s.near, `walk step ${i}: the field in view and the pointer on it`, JSON.stringify(s));
      }
      await page.close();
    }

    // ---- 4. a pointer label at the pane's right edge stays whole --------------------------------
    {
      const page = await browser.newPage({ viewport: { width: 640, height: 480 } });
      await page.setContent(`<!doctype html><body style="margin:0"><div style="position:absolute;right:12px;bottom:40px">
        <button id="s" style="width:150px;height:36px">Submit application</button></div></body>`);
      await page.addScriptTag({ content: SCRIPTS[2] });
      const fits = async (label) => page.evaluate(async (label) => {
        window.TailorPointer.at(document.getElementById("s"), label, { now: true, stay: true, on: true });
        await new Promise((r) => setTimeout(r, 350));
        const host = document.querySelector("[data-sj-pointer]");
        const t = host.shadowRoot.querySelector(".t"), svg = host.shadowRoot.querySelector("svg");
        const r = t.getBoundingClientRect(), a = svg.getBoundingClientRect();
        const overlap = r.left < a.right && r.right > a.left && r.top < a.bottom && r.bottom > a.top;
        return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, whole: t.scrollWidth <= t.clientWidth + 1, overlap, vw: innerWidth, vh: innerHeight };
      }, label);
      // the Orb's probe (shell-electron/orb_view.js) sees where the pointer and its label come to rest
      const ORB = require(path.join(ROOT, "shell-electron", "orb_view.js"));
      await page.evaluate(() => window.TailorPointer.at(document.getElementById("s"), "Your turn: check and submit", { now: true, stay: true, on: true }));
      const seen = await page.evaluate(ORB.ORB_AVOID_JS);
      const lab = await page.evaluate(async () => { await new Promise((r) => setTimeout(r, 350));
        const t = document.querySelector("[data-sj-pointer]").shadowRoot.querySelector(".t").getBoundingClientRect(); return { x: t.x, y: t.y, w: t.width, h: t.height }; });
      check(seen.some((r) => Math.abs(r.x - lab.x) < 3 && Math.abs(r.y - lab.y) < 3 && Math.abs(r.width - lab.w) < 3), "the Orb's probe sees the label's resting place at once", JSON.stringify({ seen, lab }));
      check(seen.some((r) => r.x <= 640 && r.x + r.width >= 470 && r.y > 380), "and the site's Submit", JSON.stringify(seen));
      for (const label of ["Your turn: check and submit", "All done. Press Submit when you're happy"]) {
        const f = await fits(label);
        check(f.left >= 0 && f.right <= f.vw && f.top >= 0 && f.bottom <= f.vh && f.whole && !f.overlap,
              `the label "${label}" stays whole inside the page`, JSON.stringify(f));
      }
      await page.close();
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify({ ok: !failures.length, checks: checks.length, failures }) + "\n");
  process.exit(failures.length ? 1 : 0);
})().catch((e) => { process.stdout.write(JSON.stringify({ ok: false, checks: checks.length, failures: ["crash: " + (e && e.stack || e)] }) + "\n"); process.exit(1); });
