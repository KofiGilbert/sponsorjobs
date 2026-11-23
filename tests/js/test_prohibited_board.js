// A job whose link lands on a board that forbids automated applications (2026-11-19: an Adzuna
// ad that went to dice.com). Said plainly, recorded with the board's name, and in Auto set aside
// without counting as a failure (tests/test_consent_dialog_and_dice.py runs it).
"use strict";
const path = require("path");
const { createAgent, BOARD_LINE, boardName } = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };

function setup(redirect) {
  const posts = [], progress = [];
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
    noPolling: true, getPane: () => pane, openInPane: (u) => { wc.url = redirect[u] || u; }, waitPaneLoad: async () => {},
    autofillCore: () => "", agentViewSource: () => "", runAutoApplyOnPane: async () => ({ ok: true, filled: 0, left: [] }),
    sendProgress: (info) => progress.push(info), acquireDebugger: () => null, frames: {},
    applyDecision: async () => ({ fill: true }), isNewPage: () => false, attachResume: async () => false,
    getApp: async (p) => {
      if (p.startsWith("/api/autoapply/agent/parked")) return { ok: true, parked: [] };
      if (p.startsWith("/api/autoapply/agent/allowed-hosts")) {
        const h = new URL(decodeURIComponent(p.split("url=")[1] || "")).hostname.replace(/^www\./, "");
        return { ok: true, hosts: [h], aggregator: /adzuna\./.test(h) };
      }
      return { ok: true, mode: "auto" };
    },
    postApp: async (p, b) => { posts.push([p, b]); return { ok: true }; },
  };
  return { agent: createAgent(deps), posts, progress };
}

(async () => {
  assert(boardName("dice.com") === "Dice" && boardName("uk.linkedin.com") === "LinkedIn" && boardName("www.indeed.com".replace(/^www\./, "")) === "Indeed", "names");
  assert(BOARD_LINE("Dice") === "This job is on Dice, which does not allow automated applications. Open it and apply yourself.", "the line");
  const ad = "https://www.adzuna.com/land/ad/1?se=x";
  const ok = "https://careers.brightwater.example/apply/2";
  const { agent, posts, progress } = setup({ [ad]: "https://www.dice.com/job-detail/abc" });
  const r = await agent.batch([{ recordId: 28, url: ad }, { recordId: 29, url: ok }], { quiet: true });
  assert(r === undefined || r === null || typeof r === "object", "batch ran");
  const stop = progress.find((x) => x.record_id === 28 && x.reason === "prohibited-board");
  assert(stop && /This job is on Dice, which does not allow automated applications\. Open it and apply yourself\./.test(stop.message) && stop.aside, "said plainly: " + JSON.stringify(stop));
  assert(!progress.some((x) => x.record_id === 28 && x.reason === "left-site"), "never the generic left-site");
  const rec = posts.find(([p, b]) => p === "/api/autoapply/agent/left-site" && b.record_id === 28 && !b.clear);
  assert(rec && rec[1].kind === "prohibited" && rec[1].board === "Dice" && rec[1].host === "dice.com", "recorded: " + JSON.stringify(rec));
  const done = progress.find((x) => x.state === "batch-done");
  assert(done && done.batch.aside === 1 && done.batch.stopped === 0 && /1 set aside/.test(done.message), "set aside, not a failure: " + JSON.stringify(done));
  console.log("ok");
})().catch((e) => { console.error(e); process.exit(1); });
