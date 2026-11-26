// The guided ask (2026-11-17, docs/apply-agent.md section 27), under node with a fake pane, a fake
// brain and a small fake of the engine's ask (tests/test_agent_batch_exits.py runs it):
//  1. ASSISTED, the owner's Phoenix Contact form: three fields are open at the end. No question
//     list goes on the bar: ONE slim line "3 fields need you. 1 of 3". Each field is scrolled to
//     and outlined in order; a value appearing on the page (the person used the site's own
//     control) removes the outline, is handed to the engine and moves on; Next passes a field
//     without an answer (it stays flagged "Needs you"); typed text is taken only once the person
//     left the box. The run then reaches Ready to send and never asks again. Nothing on Telegram.
//  2. HYBRID: the same questions also go to Telegram; an answer from there is filled into the
//     form by the shell and counts the same.
//  3. AUTO (a quiet batch): nothing is outlined, nothing is asked: the job parks.
//  4. Stop during the guided ask closes it and removes the outline.
"use strict";
const os = require("os");
const fs = require("fs");
const path = require("path");
process.env.TMPDIR = fs.mkdtempSync(path.join(os.tmpdir(), "sj-guide-"));
process.env.SJ_AGENT_PANE_MS = "300";
process.env.SJ_AGENT_FILL_MS = "300";
process.env.SJ_AGENT_RETRY_MS = "20";
process.env.SJ_AGENT_GUIDE_MS = "5";
const { createAgent, pageValue, HL_ON, HL_OFF } = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };

const SMS = "By selecting Yes, you agree to receive mobile messages from us in relation to this job application";
const COV = "Are you subject to any restrictive covenants?";
const TXT = "Describe a project you are proud of";
const FIELDS = { 21: { q: SMS, kind: "select", options: ["Yes"] }, 22: { q: COV, kind: "select", options: ["Yes", "No"] }, 23: { q: TXT, kind: "textarea" } };

function fakePane(P) {
  const line = (i) => {
    const f = FIELDS[i], v = P.vals[i] || "";
    return f.kind === "select"
      ? `[${i}]<select label="${f.q}" value="${v}" options=${f.options.length} first="${f.options.join("|")}" required>`
      : `[${i}]<textarea label="${f.q}" value="${v ? "(filled)" : ""}" required>`;
  };
  const view = () => ["Apply for Analyst", ...P.fields.map(line), "[40]<button>Submit application</button>"].join("\n");
  const wc = {
    getURL: () => P.url, isDestroyed: () => false, on: () => {}, removeListener: () => {}, session: null,
    reload: () => {}, getZoomFactor: () => 1, sendInputEvent: () => {},
    navigationHistory: { canGoBack: () => false, goBack: () => {}, getActiveIndex: () => 0, getEntryAtIndex: () => ({ url: P.url }) },
    executeJavaScript: async (expr) => {
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet.at")) return "5000|complete";
      if (expr.includes("pageSignature")) return P.sent ? { labels: [], success: true } : { labels: ["form"] };
      if (expr.includes("/*sj-highlight*/")) {
        const i = Number(/element\((\d+)\)/.exec(expr)[1]);
        P.lit = i; P.log.push("hl " + i);
        if (P.onLight) await P.onLight(i);
        return true;
      }
      if (expr.includes("/*sj-unhighlight*/")) { if (P.lit) P.log.push("unhl " + P.lit); P.lit = 0; return 1; }
      if (expr.includes("/*sj-value*/")) {
        const i = Number(/element\((\d+)\)/.exec(expr)[1]);
        P.reads[i] = (P.reads[i] || 0) + 1;
        return { value: P.vals[i] || "", typing: !!(P.typing && P.typing(i, P.reads[i])) };
      }
      if (expr.includes("termsItems")) return [];
      if (expr.includes("crossOriginFrames")) return 0;
      if (expr.includes("resumeInputOpen") || expr.includes("hasResumeUpload")) return false;
      if (expr.includes("pageShape")) return { fields: 4, buttons: 1, skeleton: false, words: 80 };
      if (expr.includes("TailorAgent.view(")) return { text: "URL: " + P.url + "\n" + view() };
      if (expr.includes("TailorAutofill.leftover()")) return [];
      if (expr.includes("selectOptions(")) {
        const f = FIELDS[Number(/selectOptions\((\d+)/.exec(expr)[1])];
        return f && f.kind === "select" ? { options: ["Select..."].concat(f.options), partial: false } : null;
      }
      if (expr.includes("chosenIs(")) {
        const m = /chosenIs\((\d+), (".*")\)/.exec(expr);
        return !!m && P.vals[Number(m[1])] === JSON.parse(m[2]);
      }
      if (expr.includes("popupOpen(")) return false;
      if (expr.includes("pageCheck")) return { required_empty: [], errors: [] };
      if (expr.includes("innerText || el.value")) return "Submit application";
      if (expr.includes("TailorAgent.act(")) {
        const a = JSON.parse(expr.slice(expr.indexOf("(") + 1, expr.lastIndexOf(")")));
        P.acts.push(a);
        if (a.op === "select") P.vals[a.index] = a.option;
        if (a.op === "type") P.vals[a.index] = a.text;
        return { ok: true };
      }
      return true;
    },
  };
  return { webContents: wc };
}

// The engine's ask, as ui/app.py answers it: waiting until every question is answered or left.
function fakeEngine() {
  const E = { asks: [], answers: [], waits: 0, items: null, closed: false };
  E.post = (p, b) => {
    if (p === "/api/autoapply/agent/ask") {
      if (b.known_only) return { ok: false, answers: {}, origins: {}, select: {}, leave: [], open: (b.questions || []).map((x) => x.question) };
      E.asks.push(b);
      if (b.no_wait) return { ok: false, id: b.id, reason: "parked" };
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
      E.waits++;
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

function setup(o) {
  o = o || {};
  const P = { url: "https://job-boards.greenhouse.io/acme/jobs/1", fields: o.fields || [21, 22, 23], vals: {}, acts: [], log: [], reads: {}, lit: 0 };
  const pane = fakePane(P);
  const E = fakeEngine();
  const posts = [], progress = [];
  const mode = o.mode || "assisted";
  const empty = () => P.fields.filter((i) => !P.vals[i]).map((i) => FIELDS[i].q);
  const deps = {
    noPolling: true,
    getPane: () => pane, openInPane: () => {}, waitPaneLoad: async () => {},
    autofillCore: () => "", agentViewSource: () => "",
    runAutoApplyOnPane: async () => ({ ok: true, filled: 0, left: empty() }),
    sendProgress: (info) => { progress.push(JSON.parse(JSON.stringify(info))); P.log.push("progress " + info.state + " " + (info.message || "")); },
    setQuiet: () => {},
    acquireDebugger: () => null, frames: {},
    applyDecision: async () => ({ fill: true, click_submit: false }),
    isNewPage: () => true, attachResume: async () => false,
    getApp: async (p) => (p.startsWith("/api/autoapply/agent/parked") ? { ok: true, parked: [] }
      : p === "/api/autoapply/mode" ? { ok: true, mode, telegram_asks: mode === "hybrid" } : { hosts: ["job-boards.greenhouse.io"] }),
    postApp: async (p, b) => {
      posts.push([p, JSON.parse(JSON.stringify(b || {}))]);
      if (p === "/api/autoapply/agent/step") {
        P.log.push("step " + b.step);
        return { ok: true, summary: "Working on it", actions: [], done: { outcome: "ready_to_submit" }, cost: 0.01 };
      }
      if (p === "/api/autoapply/agent/critic") return { ok: true, issues: [] };
      const e = E.post(p, JSON.parse(JSON.stringify(b || {})));
      return e || { ok: true };
    },
  };
  const agent = createAgent(deps);
  return { agent, posts, progress, P, E, deps };
}
const guides = (s) => s.progress.filter((x) => x.state === "guide").map((x) => x.message);
const hl = (s) => s.P.log.filter((l) => /^(hl|unhl) /.test(l));

(async () => {
  // ---- helpers --------------------------------------------------------------------------------
  assert(pageValue(['[22]<select label="X" value="No" options=2 required>']) === "No", "a dropdown's choice");
  assert(pageValue(['[5]<radio label="X" option="Yes" checked=false>', '[6]<radio label="X" option="No" checked=true>']) === "No", "the ticked radio's words");
  assert(pageValue(['[23]<textarea label="X" value="(filled)" required>']) === "", "typed text is read in the page, not from the view");
  assert(HL_ON(7).includes("scrollIntoView") && HL_ON(7).includes("element(7)") && HL_OFF.includes("data-sj-hl"), "scroll, outline, and its removal");

  // ---- 1. ASSISTED: the owner's form ------------------------------------------------------------
  {
    const s = setup({ mode: "assisted" });
    // typed text: the box still has the keyboard for the first two looks
    s.P.typing = (i, reads) => i === 23 && reads <= 2;
    s.P.onLight = async (i) => {
      if (i === 21) setTimeout(() => s.agent.guideNext(), 15);            // the only-Yes consent: Next, not answered
      if (i === 22) setTimeout(() => { s.P.vals[22] = "No"; }, 15);       // picked on the page, the site's own control
      if (i === 23) setTimeout(() => { s.P.vals[23] = "The billing migration"; }, 15);
    };
    const r = await s.agent.run(1, { url: s.P.url, lane: "assisted", company: "Acme" });
    assert(r && r.ready === true, "reaches Ready to send " + JSON.stringify(r) + "\n" + s.P.log.join("\n"));
    assert(JSON.stringify(hl(s)) === JSON.stringify(["hl 21", "unhl 21", "hl 22", "unhl 22", "hl 23", "unhl 23"]), "each open field outlined in order, then cleared\n" + hl(s).join("\n"));
    assert(JSON.stringify(guides(s)) === JSON.stringify(["3 fields need you. 1 of 3", "3 fields need you. 2 of 3", "3 fields need you. 3 of 3"]), "ONE slim line per field " + JSON.stringify(guides(s)));
    assert(!s.progress.some((x) => x.state === "ask"), "no question list, no answer box on the bar");
    assert(s.E.asks.length === 1 && s.E.asks[0].batch === true && s.E.asks[0].telegram === false, "one ask, nothing on Telegram " + JSON.stringify(s.E.asks.map((a) => [a.batch, a.telegram])));
    const A = s.E.answers;
    assert(A.length === 3, "three hand-overs " + JSON.stringify(A));
    assert(A[0].index === 0 && A[0].action === "leave" && A[0].standing === false && A[0].question === SMS, "Next passes the field, deciding nothing for other forms " + JSON.stringify(A[0]));
    assert(A[1].index === 1 && A[1].answer === "No" && A[1].exact === true && A[1].on_page === true, "the choice made on the page " + JSON.stringify(A[1]));
    assert(A[2].index === 2 && A[2].answer === "The billing migration" && A[2].on_page === true, "the typed text, whole " + JSON.stringify(A[2]));
    assert(s.P.reads[23] >= 3, "typed text waited until the box lost the keyboard (" + s.P.reads[23] + " looks)");
    assert(s.P.acts.length === 0, "the shell changed nothing on the page: the person did " + JSON.stringify(s.P.acts));
    // never asked again, and the field passed with Next is flagged
    assert(s.posts.filter(([p, b]) => p === "/api/autoapply/agent/ask" && !b.known_only).length === 1, "asked once");
    const ready = s.progress.find((x) => x.state === "ready");
    const rows = (ready && ready.answers && ready.answers.rows) || [];
    const sms = rows.find((x) => x.label === SMS), cov = rows.find((x) => x.label === COV);
    assert(sms && sms.flag === "needs_you" && sms.value === "", "the passed field is flagged Needs you " + JSON.stringify(sms));
    assert(cov && cov.value === "No" && cov.from === "you", "the person's answer is in Review answers " + JSON.stringify(cov));
    const steps = s.posts.filter(([p]) => p === "/api/autoapply/agent/step").map(([, b]) => b);
    assert(steps.length <= 4, "no loop: " + steps.length + " brain steps");
    assert(steps.slice(1).some((b) => (b.nudges || []).some((nn) => /answered on the page itself/.test(nn))), "the brain is told to leave those fields alone");
  }

  // ---- only-Yes select answered on the page: taken as it is, nothing "could not tell" -----------
  {
    const s = setup({ mode: "assisted", fields: [21] });
    s.P.onLight = async () => { setTimeout(() => { s.P.vals[21] = "Yes"; }, 10); };
    const r = await s.agent.run(2, { url: s.P.url });
    assert(r && r.ready === true, "ready " + JSON.stringify(r));
    assert(JSON.stringify(guides(s)) === JSON.stringify(["1 field needs you. 1 of 1"]), "singular " + JSON.stringify(guides(s)));
    assert(s.E.answers.length === 1 && s.E.answers[0].answer === "Yes" && s.E.answers[0].exact === true, "Yes from the page " + JSON.stringify(s.E.answers));
    assert(!s.progress.some((x) => /could not tell/i.test(x.message || "")), "never a dead end");
  }

  // ---- 2. HYBRID: Telegram's answer is filled into the form ---------------------------------------
  {
    const s = setup({ mode: "hybrid", fields: [22, 23] });
    s.P.onLight = async (i) => {
      // the person answers both on Telegram while the first field is outlined
      if (i === 22) setTimeout(() => { s.E.items[0].answer = "No"; s.E.items[1].answer = "From my phone"; }, 15);
    };
    const r = await s.agent.run(3, { url: s.P.url });
    assert(r && r.ready === true, "ready " + JSON.stringify(r) + "\n" + s.P.log.join("\n"));
    assert(s.E.asks.length === 1 && s.E.asks[0].telegram === true, "the questions also went to Telegram");
    assert(s.E.asks[0].questions[0].all_options.join("|") === "Yes|No", "with the page's real choices " + JSON.stringify(s.E.asks[0].questions[0]));
    assert(s.P.acts.some((a) => a.op === "select" && a.index === 22 && a.option === "No"), "the choice was picked on the page by the shell " + JSON.stringify(s.P.acts));
    assert(s.P.acts.some((a) => a.op === "type" && a.index === 23 && a.text === "From my phone"), "the text was typed by the shell " + JSON.stringify(s.P.acts));
    assert(s.P.vals[22] === "No" && s.P.vals[23] === "From my phone", "both are in the form");
    assert(s.E.answers.length === 0, "nothing was read back as a second answer " + JSON.stringify(s.E.answers));
    assert(hl(s)[0] === "hl 22" && hl(s).filter((l) => /^hl /.test(l)).length === 1, "the second field never needed its outline " + JSON.stringify(hl(s)));
    assert(s.P.lit === 0, "no outline left behind");
  }
  {
    // whichever comes first wins: the page beats a later Telegram answer
    const s = setup({ mode: "hybrid", fields: [22] });
    s.P.onLight = async () => { setTimeout(() => { s.P.vals[22] = "Yes"; }, 10); };
    const r = await s.agent.run(4, { url: s.P.url });
    assert(r && r.ready && s.E.answers[0].answer === "Yes" && s.P.acts.length === 0, "the page's answer stands");
  }

  // ---- 3. AUTO: never outlined, never asked -------------------------------------------------------
  {
    const s = setup({ mode: "auto" });
    const r = await s.agent.batch([{ recordId: 5, url: s.P.url, lane: "assisted", company: "Acme" }], { quiet: true });
    // (2026-11-19, Auto never stops to ask) not parked: what was left goes over at Ready to send,
    // flagged "Needs you", and is never sent by the agent alone (tests/js/test_auto_never_stops.js)
    assert(r.ok && !r.results[0].parked && r.results[0].ready === true, "never set aside for a question " + JSON.stringify(r.results));
    assert(hl(s).filter((l) => /^hl /.test(l)).length === 0 && guides(s).length === 0, "nothing outlined, no line on the bar");
    assert(s.E.asks.length === 0, "nothing asked anywhere " + JSON.stringify(s.E.asks));
    assert(s.E.waits === 0 && s.E.answers.length === 0, "and nothing waited for");
  }

  // ---- 4. Stop (Skip this job) closes the ask and clears the outline ----------------------------
  {
    const s = setup({ mode: "assisted", fields: [22] });
    s.P.onLight = async () => { setTimeout(() => s.agent.stop(), 10); };
    const r = await s.agent.run(6, { url: s.P.url });
    assert(r && r.ok === false && r.reason === "stopped", "stopped " + JSON.stringify(r));
    assert(s.E.closed === true && s.P.lit === 0, "the ask is closed and the outline is gone");
    assert(s.agent.guideNext().ok === false, "Next does nothing once the run is over");
  }
  // ---- 5. (2026-11-26) the person answers and presses the site's own Submit during the ask: the
  // page turns into the confirmation, so the ask ends, the record is Applied and the bar says so
  {
    const s = setup({ mode: "assisted", fields: [22] });
    const announced = [];
    s.P.onLight = async () => { setTimeout(() => { s.P.sent = true; }, 10); };
    const deps0 = s.agent;
    // the shared submit signal (applied_watch.js) is told once
    s.agent = createAgent(Object.assign({}, s.deps, { announceSubmitted: (x) => announced.push(x) }));
    const r = await s.agent.run(7, { url: s.P.url });
    assert(deps0 && r && r.ok === true && r.submitted === true, "the run ends Submitted " + JSON.stringify(r) + "\n" + s.P.log.join("\n"));
    assert(s.E.closed === true && s.P.lit === 0, "the ask is closed and the outline is gone");
    assert(s.posts.some(([p, b]) => p === "/api/autofill/applied" && b.record_id === 7), "marked Applied " + JSON.stringify(s.posts.map((x) => x[0])));
    assert(announced.length === 1 && announced[0].recordId === 7 && announced[0].matched === true, "announced once " + JSON.stringify(announced));
    assert(s.progress.some((x) => x.state === "submitted"), "the bar says Submitted " + JSON.stringify(s.progress.map((x) => x.state)));
  }
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
