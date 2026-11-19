// The three apply modes in the shell (docs/apply-agent.md section 26), under node with a fake
// pane and a fake brain (tests/test_apply_modes.py runs it):
//  1. AUTO (a quiet batch): the pane is put out of view and brought back (deps.setQuiet), every
//     progress line is marked quiet, nothing is asked on the bar or on Telegram (a job with an open
//     question parks at once), no digest and no Ready to send notice go out; a site the engine
//     lets it submit on is sent and reported as sent by the agent; every other site stops at
//     Ready to send with nothing pressed.
//  2. HYBRID: the run's one batch of questions also goes to Telegram; ASSISTED: it does not; the
//     batch digest goes out only in Hybrid.
//  3. SEND ALL: every ready application goes through the same Send as the bar's, one at a time,
//     as the person's own send, and a captcha stops the list.
"use strict";
const os = require("os");
const fs = require("fs");
const path = require("path");
process.env.TMPDIR = fs.mkdtempSync(path.join(os.tmpdir(), "sj-modes-"));
process.env.SJ_AGENT_PANE_MS = "300";
process.env.SJ_AGENT_FILL_MS = "300";
process.env.SJ_AGENT_RETRY_MS = "20";
const { createAgent } = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };

const Q = "Describe a project you are proud of";
const AUTO_HOST = "acme.recruitee.com";          // the engine says click_submit for this host only

// One page per job: a review page with a Submit button; job "ask" has one open required question.
function fakePane(P) {
  const view = () => (P.job === "ask" && !P.answered
    ? `Apply\n[22]<textarea label="${Q}" value="" required>\n[30]<button>Submit application</button>`
    : "Review\n[40]<button>Submit</button>");
  const wc = {
    getURL: () => P.url, isDestroyed: () => false, on: () => {}, removeListener: () => {}, session: null,
    reload: () => {}, getZoomFactor: () => 1,
    navigationHistory: { canGoBack: () => false, goBack: () => {}, getActiveIndex: () => 0, getEntryAtIndex: () => ({ url: P.url }) },
    sendInputEvent: (ev) => { if (ev.type === "mouseUp") { P.clicks.push(P.url); if (!P.captchaOn(P.url)) P.sent.add(P.url); } },
    executeJavaScript: async (expr) => {
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet.at")) return "5000|complete";
      if (expr.includes("pageSignature")) {
        if (P.clicks.includes(P.url) && P.captchaOn(P.url)) return { blocker: "captcha" };
        return P.sent.has(P.url) ? { success: true } : { labels: ["review"] };
      }
      if (expr.includes("submitButton()")) return { x: 100, y: 100, label: "Submit" };
      if (expr.includes("termsItems")) return [];
      if (expr.includes("crossOriginFrames")) return 0;
      if (expr.includes("resumeInputOpen") || expr.includes("hasResumeUpload")) return false;
      if (expr.includes("pageShape")) return { fields: 3, buttons: 1, skeleton: false, words: 60 };
      if (expr.includes("TailorAgent.view(")) return { text: "URL: " + P.url + "\n" + view() };
      if (expr.includes("TailorAutofill.leftover()")) return [];
      if (expr.includes("selectOptions(")) return null;
      if (expr.includes("pageCheck")) return { required_empty: [], errors: [] };
      if (expr.includes("innerText || el.value")) return "Submit";
      if (expr.includes("TailorAgent.act(")) { P.answered = true; return { ok: true, note: "", changed: false }; }
      return true;
    },
  };
  return { webContents: wc };
}

function setup(o) {
  o = o || {};
  const P = { url: "", job: "", answered: false, clicks: [], sent: new Set(), captchaOn: o.captchaOn || (() => false) };
  const pane = fakePane(P);
  const posts = [], progress = [], quiet = [], opened = [];
  const mode = o.mode || "assisted";
  const deps = {
    noPolling: true,
    getPane: () => pane,
    openInPane: (u) => { opened.push(u); P.url = u; P.job = /\/ask\//.test(u) ? "ask" : "plain"; P.answered = false; },
    waitPaneLoad: async () => {},
    autofillCore: () => "", agentViewSource: () => "",
    runAutoApplyOnPane: async () => ({ ok: true, filled: 0, left: [] }),
    sendProgress: (info) => progress.push(JSON.parse(JSON.stringify(info))),
    setQuiet: (on) => quiet.push(on),
    acquireDebugger: () => null, frames: {},
    applyDecision: async (u) => ({ fill: true, click_submit: u.includes(AUTO_HOST) }),
    isNewPage: () => true, attachResume: async () => false,
    getApp: async (p) => (p.startsWith("/api/autoapply/agent/parked") ? { ok: true, parked: [] }
      : p === "/api/autoapply/mode" ? (o.modeFails ? Promise.reject(new Error("engine down")) : { ok: true, mode, telegram_asks: mode === "hybrid", hidden: mode === "auto" })
      : { hosts: [AUTO_HOST, "jobs.example.com"] }),
    postApp: async (p, b) => {
      posts.push([p, JSON.parse(JSON.stringify(b || {}))]);
      if (p === "/api/autoapply/agent/step") {
        // (2026-11-19) Auto never stops to ask: told so (``auto``), the brain gives its best guess
        if (b.auto && P.job === "ask" && !P.answered) {
          return { ok: true, summary: "Answering with a best guess", done: null, cost: 0.01,
                   actions: [{ op: "type", index: 22, text: "Open to discuss", sensitive: false,
                               proof: { source: "best_guess", origin: "best_guess", evidence: "nothing saved answers this", class: "" } }] };
        }
        return P.job === "ask" && !P.answered
          ? { ok: true, summary: "Working on it", actions: [], done: { outcome: "blocked", reason: "needs-answers" }, cost: 0.01 }
          : { ok: true, summary: "Working on it", actions: [], done: { outcome: "ready_to_submit" }, cost: 0.01 };
      }
      if (p === "/api/autoapply/agent/ask") {
        if (b.known_only) return { ok: false, answers: {}, origins: {}, select: {}, open: (b.questions || []).map((x) => x.question) };
        if (b.no_wait) return { ok: false, id: b.id, reason: "parked" };
        P.answered = true;
        const got = {};
        for (const it of b.questions || []) got[it.question] = "The billing migration";
        return { ok: true, id: b.id, answers: got };
      }
      if (p === "/api/autoapply/agent/critic") return { ok: true, issues: [] };
      if (p === "/api/autoapply/agent/digest") return { ok: true, sent: true };
      return { ok: true };
    },
  };
  return { agent: createAgent(deps), posts, progress, quiet, opened, P };
}
const of = (posts, path_) => posts.filter(([p]) => p === path_).map(([, b]) => b);
const realAsks = (posts) => of(posts, "/api/autoapply/agent/ask").filter((b) => !b.known_only);

(async () => {
  // ---- 1. AUTO: a quiet batch --------------------------------------------------------------
  {
    const s = setup({ mode: "auto" });
    const r = await s.agent.batch([
      { recordId: 1, url: "https://" + AUTO_HOST + "/o/analyst", lane: "auto", autonomous: true, company: "Acme" },
      { recordId: 2, url: "https://jobs.example.com/job/2", lane: "assisted", autonomous: false, company: "Bolt" },
      { recordId: 3, url: "https://jobs.example.com/ask/3", lane: "assisted", autonomous: false, company: "Cove" },
      // the renderer said "autonomous" but the engine's decision for this host is no: never pressed
      { recordId: 4, url: "https://jobs.example.com/job/4", lane: "auto", autonomous: true, company: "Dune" },
    ], { quiet: true });
    assert(r.ok && r.results.length === 4, "all four ran " + JSON.stringify(r));
    assert(JSON.stringify(s.quiet) === "[true,false]", "the pane went out of view and came back " + JSON.stringify(s.quiet));
    assert(s.progress.length > 4 && s.progress.every((x) => x.quiet === true), "every progress line is quiet");
    assert(!s.progress.some((x) => x.state === "ask"), "no ask ever reaches the bar");
    const by = (id) => r.results.find((x) => x.record_id === id);
    assert(by(1).submitted === true, "the allowlisted site was sent " + JSON.stringify(by(1)));
    assert(by(2).ready === true && !by(2).submitted, "the other site stops at Ready to send " + JSON.stringify(by(2)));
    // (2026-11-19, the owner: Auto never stops to ask) the job with a question nothing answers is
    // NOT set aside: the brain's best guess goes in, flagged, and it waits at Ready to send
    assert(by(3).ready === true && !by(3).parked && !by(3).submitted, "the job with a question goes on to Ready to send " + JSON.stringify(by(3)));
    assert(of(s.posts, "/api/autoapply/agent/step").every((b) => b.auto === true), "every step of an Auto run says so");
    const g3 = s.progress.find((x) => x.state === "ready" && x.answers && x.answers.rows.some((r) => r.from === "best_guess"));
    assert(g3 && g3.answers.rows.find((r) => r.from === "best_guess").flag === "best_guess", "the guess is flagged in Review answers");
    assert(by(4).ready === true && !by(4).submitted, "the engine's no wins over the flag " + JSON.stringify(by(4)));
    assert(JSON.stringify(s.P.clicks) === JSON.stringify(["https://" + AUTO_HOST + "/o/analyst"]), "Submit pressed only on the allowlisted host " + JSON.stringify(s.P.clicks));
    const applied = of(s.posts, "/api/autofill/applied");
    assert(applied.length === 1 && applied[0].record_id === 1 && applied[0].by === "auto", "reported as sent by the agent " + JSON.stringify(applied));
    const asks = realAsks(s.posts);
    assert(asks.length === 0, "nothing is asked in Auto, on the bar or on Telegram " + JSON.stringify(asks));
    assert(of(s.posts, "/api/autoapply/agent/digest").length === 0, "no digest in Auto");
    const readies = of(s.posts, "/api/autoapply/agent/ready").filter((b) => !b.clear);
    assert(readies.length === 3 && readies.every((b) => !("notice" in b)), "no Ready to send notice in Auto " + JSON.stringify(readies));
    assert(readies.some((b) => b.guessed === 1 && b.guessed_legal === 0), "the guess is counted on the application " + JSON.stringify(readies));
    assert(s.agent.quiet() === false, "quiet ends with the batch");
  }

  // ---- 2. HYBRID sends the batch of questions to Telegram; ASSISTED does not -----------------
  for (const [mode, want] of [["hybrid", true], ["assisted", false]]) {
    const s = setup({ mode });
    const r = await s.agent.run(11, { url: "https://jobs.example.com/ask/11", lane: "assisted", company: "Cove" });
    assert(r && r.ready === true, mode + ": reaches Ready to send " + JSON.stringify(r));
    const asks = realAsks(s.posts);
    assert(asks.length === 1 && asks[0].batch === true, mode + ": one batch ask " + JSON.stringify(asks));
    assert(asks[0].telegram === want, mode + ": telegram " + asks[0].telegram);
    assert(!s.progress.some((x) => x.state === "ask"), mode + ": no question list on the bar (the guided ask, test_agent_guide.js)");
    assert(s.progress.every((x) => !x.quiet), mode + ": visible");
    assert(s.quiet.length === 0, mode + ": the pane is never hidden");
    assert(s.P.clicks.length === 0, mode + ": Submit is the person's");
    const readies = of(s.posts, "/api/autoapply/agent/ready").filter((b) => !b.clear);
    assert(readies.length === 1 && readies[0].notice, mode + ": the Ready to send notice is handed over");
    // a visible batch: the digest goes to Telegram only in Hybrid
    const b = setup({ mode });
    await b.agent.batch([{ recordId: 12, url: "https://jobs.example.com/ask/12" }]);
    assert(of(b.posts, "/api/autoapply/agent/digest").length === (want ? 1 : 0), mode + ": digest " + of(b.posts, "/api/autoapply/agent/digest").length);
    assert(b.progress.every((x) => !x.quiet) && b.quiet.length === 0, mode + ": a visible batch stays visible");
  }
  // the engine does not answer: nothing goes to Telegram
  {
    const s = setup({ mode: "hybrid", modeFails: true });
    const r = await s.agent.run(13, { url: "https://jobs.example.com/ask/13" });
    assert(r && r.ready, "ran " + JSON.stringify(r));
    assert(realAsks(s.posts)[0].telegram === false, "mode unknown: the bar only");
  }

  // ---- 3. SEND ALL ---------------------------------------------------------------------------
  {
    const s = setup({ mode: "auto" });
    const jobs = [1, 2, 3].map((n) => ({ recordId: n, url: "https://jobs.example.com/job/" + n, company: "Co" + n, sig: "" }));
    const r = await s.agent.sendAll(jobs);
    assert(r.ok && r.sent === 3 && r.left === 0 && r.captcha == null, "all three sent " + JSON.stringify(r));
    assert(JSON.stringify(s.opened) === JSON.stringify(jobs.map((j) => j.url)), "one at a time, in order");
    assert(s.P.clicks.length === 3, "each Submit pressed once");
    const applied = of(s.posts, "/api/autofill/applied");
    assert(applied.length === 3 && applied.every((b) => b.by !== "auto"), "each is the person's own send " + JSON.stringify(applied));
    assert(s.progress.every((x) => !x.quiet) && s.quiet.length === 0, "Send all runs in view");
    const done = s.progress.find((x) => x.state === "sendall-done");
    assert(done && done.message === "Sent 3 of 3.", "summary " + (done && done.message));
    assert(s.agent.busy() === false, "free again");
  }
  {
    // the second job's Submit lands on a captcha: the list stops there, the third is untouched
    const s = setup({ captchaOn: (u) => /job\/2$/.test(u) });
    const jobs = [1, 2, 3].map((n) => ({ recordId: n, url: "https://jobs.example.com/job/" + n, company: "Co" + n }));
    const r = await s.agent.sendAll(jobs);
    assert(r.ok && r.sent === 1 && r.captcha === 2 && r.left === 2, "stopped at the captcha " + JSON.stringify({ sent: r.sent, captcha: r.captcha, left: r.left }));
    assert(!s.opened.includes("https://jobs.example.com/job/3"), "the third was never opened");
    assert(of(s.posts, "/api/autofill/applied").length === 1, "only the first is marked sent");
    const done = s.progress.find((x) => x.state === "sendall-done");
    assert(done && /^Sent 1 of 3\. Stopped at a captcha on Co2: solve it on the page, then press Send\. The rest are still ready\.$/.test(done.message), "says so " + (done && done.message));
  }
  {
    // busy and empty
    const s = setup({});
    assert((await s.agent.sendAll([])).reason === "empty", "empty");
    const p = s.agent.sendAll([{ recordId: 1, url: "https://jobs.example.com/job/1" }]);
    assert((await s.agent.run(9, { url: "https://jobs.example.com/job/9" })).reason === "busy", "a run waits behind Send all");
    assert((await s.agent.batch([{ recordId: 8, url: "https://jobs.example.com/job/8" }])).reason === "busy", "so does a batch");
    await p;
  }
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
