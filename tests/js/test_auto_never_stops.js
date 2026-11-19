// Auto never stops to ask (2026-11-19, docs/apply-agent.md section 31): the shell's part, without
// Electron (tests/test_auto_never_stops.py runs it).
//   1. A quiet (Auto) batch: a legal question nothing answers takes the engine's best guess, the
//      run goes on to Ready to send, the guess is flagged and counted, and the application is
//      NOT sent by the agent alone even on a site where it may send. Nothing is asked. No
//      "Set aside".
//   2. The same site with no guess IS sent alone, as before.
//   3. A required field that is never guessed (an SSN): that application is skipped with the
//      field named, and the batch moves on to the next job.
//   4. Assisted is unchanged: the same question is asked.
"use strict";
const os = require("os");
const fs = require("fs");
const path = require("path");
process.env.TMPDIR = fs.mkdtempSync(path.join(os.tmpdir(), "sj-auto-never-"));
process.env.SJ_AGENT_RETRY_MS = "5";
process.env.SJ_AGENT_ASK_REREAD_MS = "0";
process.env.SJ_AX = "0";
const A = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };

const LEGAL_Q = "Are you subject to a non-compete agreement?";
const SSN_Q = "Social Security Number";
const HOST = "jobs.brightwater.example";

function setup(o) {
  o = o || {};
  const P = { url: "", vals: {}, clicks: [], sent: new Set(), acts: [] };
  const form = () => (/\/ssn\//.test(P.url)
    ? [`[5]<input type=text label="${SSN_Q}" value="${P.vals[5] ? "(filled)" : ""}" required>`]
    : /\/plain\//.test(P.url) ? []
    : [`[3]<radio label="${LEGAL_Q}" option="Yes" checked=${P.vals[3] === "Yes"} required>`,
       `[4]<radio label="${LEGAL_Q}" option="No" checked=${P.vals[3] === "No"} required>`]);
  const view = () => ["URL: " + P.url, ...form(), "[9]<button>Submit application</button>"].join("\n");
  const left = () => (/\/ssn\//.test(P.url) ? (P.vals[5] ? [] : [SSN_Q]) : /\/plain\//.test(P.url) ? [] : (P.vals[3] ? [] : [LEGAL_Q]));
  const wc = {
    getURL: () => P.url, isDestroyed: () => false, on: () => {}, removeListener: () => {}, session: null, reload: () => {}, getZoomFactor: () => 1,
    navigationHistory: { canGoBack: () => false, goBack: () => {}, getActiveIndex: () => 0, getEntryAtIndex: () => ({ url: P.url }) },
    sendInputEvent: (ev) => { if (ev.type === "mouseUp") { P.clicks.push(P.url); P.sent.add(P.url); } },
    executeJavaScript: async (expr) => {
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet.at")) return "5000|complete";
      if (expr.includes("pageSignature")) return P.sent.has(P.url) ? { success: true } : { labels: ["q"] };
      if (expr.includes("submitButton()")) return { x: 100, y: 100, label: "Submit application" };
      if (expr.includes("termsItems")) return [];
      if (expr.includes("crossOriginFrames")) return 0;
      if (expr.includes("resumeInputOpen") || expr.includes("hasResumeUpload")) return false;
      if (expr.includes("pageShape")) return { fields: 3, buttons: 1, skeleton: false, words: 60 };
      if (expr.includes("TailorAgent.view(")) return { text: view() };
      if (expr.includes("TailorAutofill.leftover()")) return left();
      if (expr.includes("selectOptions(") || expr.includes("listOptions")) return null;
      if (expr.includes("chosenIs(")) return true;
      if (expr.includes("popupOpen(")) return false;
      if (expr.includes("pageCheck")) return { required_empty: left(), errors: [] };
      if (expr.includes("innerText || el.value")) return /element\(9\)/.test(expr) ? "Submit application" : "";
      if (expr.includes("TailorAgent.act(")) {
        const a = JSON.parse(expr.slice(expr.indexOf("(") + 1, expr.lastIndexOf(")")));
        P.acts.push(Object.assign({ url: P.url }, a));
        if (a.op === "click" && (a.index === 3 || a.index === 4)) P.vals[3] = a.index === 3 ? "Yes" : "No";
        if (a.op === "type") P.vals[a.index] = a.text;
        return { ok: true, note: "", changed: false };
      }
      return true;
    },
  };
  const pane = { webContents: wc };
  const posts = [], progress = [];
  const deps = {
    noPolling: true, getPane: () => pane,
    openInPane: (u) => { P.url = u; P.vals = {}; },
    waitPaneLoad: async () => {}, autofillCore: () => "", agentViewSource: () => "",
    runAutoApplyOnPane: async () => ({ ok: true, filled: 0, left: left() }),
    sendProgress: (info) => progress.push(JSON.parse(JSON.stringify(info))),
    setQuiet: () => {}, acquireDebugger: () => null, frames: {},
    applyDecision: async () => ({ fill: true, click_submit: true }),            // a site where the agent may send
    isNewPage: () => true, attachResume: async () => false,
    getApp: async (p) => (p.startsWith("/api/autoapply/agent/parked") ? { ok: true, parked: [] }
      : p === "/api/autoapply/mode" ? { ok: true, mode: o.mode || "auto", telegram_asks: false, hidden: (o.mode || "auto") === "auto" }
      : { hosts: [HOST] }),
    postApp: async (p, b) => {
      posts.push([p, JSON.parse(JSON.stringify(b || {}))]);
      if (p === "/api/autoapply/agent/step") {
        // the brain: it reports needs-answers for an open required question, else it is ready
        return left().length ? { ok: true, summary: "Looking", actions: [], done: { outcome: "blocked", reason: "needs-answers" }, cost: 0.01 }
                             : { ok: true, summary: "Ready", actions: [], done: { outcome: "ready_to_submit" }, cost: 0.01 };
      }
      if (p === "/api/autoapply/agent/ask") {
        if (b.known_only) {
          // the engine's known-only check (ui/app.py): a best guess only when Auto asks for one
          const out = { ok: false, known: true, answers: {}, select: {}, origins: {}, reasons: {}, classes: {}, never: [], open: [] };
          for (const q of (b.questions || []).map((x) => x.question)) {
            if (q === SSN_Q) { if (b.auto_guess) out.never.push(q); else out.open.push(q); }
            else if (q === LEGAL_Q && b.auto_guess) {
              out.answers[q] = "No"; out.select[q] = "No"; out.origins[q] = "best_guess"; out.classes[q] = "legal";
              out.reasons[q] = "most applicants are under no such agreement";
            } else out.open.push(q);
          }
          return out;
        }
        if (b.no_wait) return { ok: false, id: b.id, reason: "parked" };
        const got = {};
        for (const it of b.questions || []) got[it.question] = "No";
        return { ok: true, id: b.id, answers: got, onPage: [] };
      }
      if (p === "/api/autoapply/agent/critic") return { ok: true, issues: [] };
      return { ok: true };
    },
  };
  return { agent: A.createAgent(deps), posts, progress, P };
}
const of = (posts, p) => posts.filter(([q]) => q === p).map(([, b]) => b);

(async () => {
  // ---- 1 to 3: one quiet batch of three jobs -----------------------------------------------
  {
    const s = setup({ mode: "auto" });
    // pickSaved clicks the radio through the page: the fake pane's act records it
    const r = await s.agent.batch([
      { recordId: 1, url: `https://${HOST}/legal/1`, lane: "auto", autonomous: true, company: "Brightwater Freight" },
      { recordId: 2, url: `https://${HOST}/ssn/2`, lane: "auto", autonomous: true, company: "Brightwater Freight" },
      { recordId: 3, url: `https://${HOST}/plain/3`, lane: "auto", autonomous: true, company: "Brightwater Freight" },
    ], { quiet: true });
    assert(r.ok && r.results.length === 3, "all three were worked on " + JSON.stringify(r));
    const by = (id) => r.results.find((x) => x.record_id === id);
    const said = s.progress.map((x) => x.state + ":" + (x.message || "")).join("\n");
    // nothing asked, nothing set aside for a question
    assert(!of(s.posts, "/api/autoapply/agent/ask").some((b) => !b.known_only), "Auto asks nothing");
    assert(of(s.posts, "/api/autoapply/agent/ask").every((b) => b.auto_guess === true), "the known-only check is asked for a best guess");
    assert(of(s.posts, "/api/autoapply/agent/step").every((b) => b.auto === true), "every step says it is an Auto run");
    assert(!/Set aside|no saved answer/.test(said) && !s.progress.some((x) => x.state === "parked"), "never set aside for a question\n" + said);
    // 1. the legal question: guessed, flagged, counted, and held for the person's Send
    assert(by(1).ready === true && !by(1).submitted, "the guessed application waits at Ready to send " + JSON.stringify(by(1)));
    assert(s.P.acts.some((a) => /legal\/1/.test(a.url) && a.op === "click" && a.index === 4), "the engine's guess (No) went on the page " + JSON.stringify(s.P.acts));
    const ready = s.progress.find((x) => x.state === "ready" && x.answers);
    const row = ready.answers.rows.find((x) => x.label === LEGAL_Q);
    assert(row && row.from === "best_guess" && row.flag === "best_guess" && row.value === "No" && row.klass === "legal"
           && /most applicants/.test(row.evidence), "Review answers: best guess, with its reason and class " + JSON.stringify(row));
    assert(ready.answers.flagged === 1, "it is one to check");
    const rp = of(s.posts, "/api/autoapply/agent/ready").find((b) => b.record_id === 1 && !b.clear);
    assert(rp && rp.guessed === 1 && rp.guessed_legal === 1, "counted on the application " + JSON.stringify(rp));
    assert(!s.P.clicks.some((u) => /legal\/1/.test(u)), "Submit was never pressed on it by the agent");
    assert(/best guess, check it/.test(A.answersTelegram(ready.answers, "Analyst")), "the Telegram list says best guess");
    // 2. the never-guessed field: skipped, named, and the batch moved on
    assert(by(2).ok === false && by(2).reason === "skipped", "the SSN form is skipped " + JSON.stringify(by(2)));
    const sk = of(s.posts, "/api/autoapply/agent/skip");
    assert(sk.length === 1 && sk[0].record_id === 2 && sk[0].field === SSN_Q, "the engine is told which field " + JSON.stringify(sk));
    const sp = s.progress.find((x) => x.state === "skipped");
    assert(sp && sp.message.includes(SSN_Q) && sp.reason === "cannot_fill", "the reason names the field " + JSON.stringify(sp));
    assert(!s.P.acts.some((a) => a.index === 5), "nothing was ever typed into it");
    // 3. a form with no guess, on a site where the agent may send: sent alone, as before
    assert(by(3).submitted === true, "the clean application is sent " + JSON.stringify(by(3)));
    assert(JSON.stringify(s.P.clicks) === JSON.stringify([`https://${HOST}/plain/3`]), "Submit pressed only there " + JSON.stringify(s.P.clicks));
  }

  // ---- 4. Assisted is unchanged: the same question is asked, and never guessed ----------------
  {
    const s = setup({ mode: "assisted" });
    const r = await s.agent.run(11, { url: `https://${HOST}/legal/11`, lane: "assisted", company: "Brightwater Freight" });
    const asks = of(s.posts, "/api/autoapply/agent/ask");
    assert(asks.filter((b) => b.known_only).every((b) => !b.auto_guess), "no best guess is asked for outside Auto");
    assert(of(s.posts, "/api/autoapply/agent/step").every((b) => b.auto === false), "the brain is told the person is here");
    const real = asks.filter((b) => !b.known_only);
    assert(real.length >= 1 && real[0].questions.some((q) => q.question === LEGAL_Q), "Assisted asks the person " + JSON.stringify({ r, real }));
    assert(!s.progress.some((x) => x.answers && x.answers.rows.some((row) => row.from === "best_guess")), "and records no guess");
  }
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
