// The sign-in ladder and a refused create-account form (2026-11-18, two real Workday forms),
// without Electron (tests/test_portal_password.py runs it):
//   1. the create form says "Password must include: - A numeric character": no account exists,
//      so the shell asks the engine for ONE fresh password and the brain retries; it never
//      clicks Forgot password and never waits for a reset mail;
//   2. the same error again after the fresh password: a truthful park with the site's own words;
//   3. the engine refuses to rotate (the account worked before): the same truthful park;
//   4. "an account already exists for this email": the reset path stays open;
//   5. a create form is not a failed sign-in: landing on it never goes straight to Forgot
//      password (the second employer of 2026-11-18);
//   6. a sign-in that worked is confirmed to the engine (its password is never rotated later).
// Made-up values only.
"use strict";
const os = require("os");
const fs = require("fs");
const path = require("path");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "sj-portalpw-"));
process.env.TMPDIR = TMP;
process.env.SJ_AGENT_DEBUG = "1";
process.env.SJ_AGENT_PANE_MS = "300";
process.env.SJ_AGENT_FILL_MS = "300";
process.env.SJ_AGENT_RETRY_MS = "20";
const A = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const { createAgent, isSigninView, isCreateView, pageErrors, createFailure, resetAllowed, createBlockedReason, insideView } = A;
const assert = (c, m) => { if (!c) throw new Error(m); };

const HOST = "acme.wd1.myworkdayjobs.com";
const URL1 = "https://" + HOST + "/en-US/Careers/job/Remote/Analyst_R1/apply/applyManually";
const RULE = "Password must include: - A numeric character";          // the site's own words
const EXISTS = "An account already exists for this email address.";
const HEAD = "[Start of page]\n[1]<a>Skip to main content</a>\n[2]<a>careers home</a>\n[3]<button>Back to Job Posting</button>\nAnalyst\n";
const SIGNIN = "Title: Sign In\n" + HEAD + "Sign In\n[4]<input type=text label=\"Email Address\" value=\"\" required>\n"
  + "[5]<input type=password label=\"Password\" value=\"\" required>\n[6]<button role=button>Sign In</button>\nDon't have an account yet?\n"
  + "[7]<button>Create Account</button>\n[8]<button>Forgot your password?</button>\n[End of page]";
const create = (error) => "Title: Create Account\n" + HEAD + "Create Account\nPassword Requirements:\nA special character\nAn alphabetic character\n"
  + "An uppercase character\nA minimum of 8 characters\nA lowercase character\nA numeric character\n"
  + "[4]<input type=text label=\"Email Address\" value=\"" + (error ? "(filled)" : "") + "\" required>\n"
  + "[5]<input type=password label=\"Password\" value=\"" + (error ? "(filled)" : "") + "\" required" + (error ? " invalid" : "") + ">\n"
  + (error ? "Error: " + error + "\n" : "")
  + "[6]<input type=password label=\"Verify New Password\" value=\"" + (error ? "(filled)" : "") + "\" required>\n"
  + "[7]<button role=button>Create Account</button>\nAlready have an account?\n[8]<button>Sign In</button>\n[9]<button>Forgot your password?</button>\n[End of page]";
const FORGOT = "Title: Forgot Password\n" + HEAD + "Forgot Password\n[4]<input type=text label=\"Email Address\" value=\"\" required>\n"
  + "[5]<button role=button>Reset Password</button>\nAlready have an account?\n[6]<button>Sign In</button>\n[End of page]";
const INSIDE = "Title: My Information\n" + HEAD + "My Information\n[4]<input type=text label=\"First Name\" value=\"\" required>\n"
  + "[5]<input type=text label=\"Last Name\" value=\"\" required>\n[6]<input type=text label=\"Phone\" value=\"\" required>\n[7]<button>Save and Continue</button>\n[End of page]";

// A pane whose page is a small state machine: ``onClick(index, P)`` returns the next page.
function setup(first, onClick, brain, postOverride) {
  const P = { page: first, clicks: [] };
  const wc = {
    getURL: () => URL1, isDestroyed: () => false, on: () => {}, removeListener: () => {}, session: null,
    reload: () => {}, navigationHistory: { canGoBack: () => false },
    executeJavaScript: async (expr) => {
      if (expr.includes("!!window.TailorAutofill && !!window.TailorAgent")) return true;
      if (expr.includes("__tailorQuiet")) return "5000|complete|";
      if (expr.includes("pageSignature")) return { labels: ["Email"], blocker: /type=password/.test(P.page) ? "auth" : "" };
      if (expr.includes("termsItems")) return [];
      if (expr.includes("crossOriginFrames")) return 0;
      if (expr.includes("pageShape")) return { fields: 3, buttons: 3, skeleton: false, words: 80 };
      if (expr.includes("TailorAutofill.leftover()")) return null;
      if (expr.includes("readErrors")) return [];
      if (expr.includes("TailorAgent.view(")) return { text: "URL: " + URL1 + "\n" + P.page };
      if (expr.includes("resumeInputOpen")) return false;
      if (expr.includes("TailorAgent.act(")) {
        const a = JSON.parse(expr.slice(expr.indexOf("(") + 1, expr.lastIndexOf(")")));
        if (a.op === "click") { P.clicks.push(a.index + ":" + (/^Title: (.*)$/m.exec(P.page) || [])[1]); const next = onClick(a.index, P); if (next) P.page = next; }
        return { ok: true, note: "", changed: true };
      }
      return true;
    },
  };
  const posts = [], progress = [], pane = { webContents: wc };
  const deps = {
    noPolling: true, getPane: () => pane, openInPane: () => {}, waitPaneLoad: async () => {},
    autofillCore: () => "", agentViewSource: () => "",
    runAutoApplyOnPane: async () => ({ ok: true, filled: 0, left: [] }),
    sendProgress: (info) => progress.push(JSON.parse(JSON.stringify(info))),
    acquireDebugger: () => null, frames: {},
    applyDecision: async () => ({ fill: true, click_submit: false }),
    isNewPage: () => false, attachResume: async () => false,
    getApp: async (p) => (p.startsWith("/api/autoapply/agent/parked") ? { ok: true, parked: [] } : { hosts: [HOST] }),
    postApp: async (p, b) => {
      posts.push([p, b]);
      const o = postOverride ? postOverride(p, b, P) : undefined;
      if (o !== undefined) return o;
      if (p === "/api/autoapply/agent/step") return brain(P, b, posts);
      if (p === "/api/autoapply/agent/portal-account") return b.rotate ? { ok: true, rotated: true } : b.confirm ? { ok: true, confirmed: true } : { ok: true, created: true };
      if (p === "/api/autoapply/agent/ask") return { ok: false, reason: "stopped" };
      return { ok: true };
    },
  };
  return { agent: createAgent(deps), posts, progress, P };
}
const ok = (actions, extra) => Object.assign({ ok: true, summary: "Working on it", actions, done: null, cost: 0.01 }, extra || {});
const stop = () => ok([], { done: { outcome: "blocked", reason: "captcha" } });       // ends a run
const steps = (posts) => posts.filter(([p]) => p === "/api/autoapply/agent/step").map(([, b]) => b);
const portal = (posts, key) => posts.filter(([p, b]) => p === "/api/autoapply/agent/portal-account" && b && b[key]).map(([, b]) => b);
const parkMsg = (posts) => ((posts.filter(([p]) => p === "/api/autoapply/agent/park").pop() || [])[1] || {}).message || "";
const title = (P) => (/^Title: (.*)$/m.exec(P.page) || [])[1];
const TYPE3 = [{ op: "type", index: 4, placeholder: "portal_email", sensitive: true, text: "(secret)" },
               { op: "type", index: 5, placeholder: "portal_password", sensitive: true, text: "(secret)" },
               { op: "type", index: 6, placeholder: "portal_password", sensitive: true, text: "(secret)" },
               { op: "click", index: 7 }];
const log = () => fs.readFileSync(path.join(TMP, "sj-agent.log"), "utf8");

(async () => {
  // ---- 0. the pure decisions, on the real texts ----------------------------------------------
  {
    assert(isCreateView(create("")) && isCreateView(create(RULE)), "Workday's create form is a create view");
    assert(isSigninView(SIGNIN) && !isCreateView(SIGNIN), "the sign-in form is not");
    assert(!isCreateView(FORGOT) && !isCreateView(INSIDE), "nor the reset page or the application");
    assert(insideView(INSIDE) && !insideView(FORGOT) && !insideView(SIGNIN) && !insideView(create("")), "inside is the application's own form only");
    assert(JSON.stringify(pageErrors(create(RULE))) === JSON.stringify([RULE]), "the page's error line is read: " + JSON.stringify(pageErrors(create(RULE))));
    assert(pageErrors(create("")).length === 0, "no error, none read");
    assert(createFailure([RULE]).kind === "password-rule", "the real Workday text is a password rule");
    for (const e of ["Your password does not meet the requirements", "Password is too short", "An uppercase character", "Password must be at least 8 characters"])
      assert(createFailure([e]).kind === "password-rule", e);
    for (const e of [EXISTS, "This email is already registered", "Email already in use. Sign in instead."])
      assert(createFailure([e]).kind === "exists", e);
    for (const e of ["Passwords do not match", "Password is required", "Email must include @", "You must accept the terms"])
      assert(createFailure([e]).kind === "validation", e);
    assert(createFailure([RULE, EXISTS]).kind === "exists", "an existing account wins over a rule");
    assert(createFailure([]).kind === "", "no error, no failure");
    // reset: never with no account, never from a create form that has not said "already exists"
    assert(!resetAllowed({ noAccount: true }, SIGNIN) && !resetAllowed({ noAccount: true }, create(RULE)), "no account, no reset");
    assert(!resetAllowed({}, create("")) && !resetAllowed({}, create(RULE)), "a create form alone never resets");
    assert(resetAllowed({ exists: true }, create(EXISTS)), "already exists: reset is right");
    assert(resetAllowed({}, SIGNIN), "a failed sign-in to an account may reset");
    const why = createBlockedReason("Acme", RULE);
    assert(why === "I could not create an account on Acme: " + RULE + ". Create the account on the page yourself, then press Continue.", why);
    assert(!/[\u2013\u2014]/.test(why), "no dashes");
  }

  // ---- 1. a password rule: one fresh password, one retry, no reset ---------------------------
  {
    // Create Account fails with the rule until the engine has rotated the password, then works.
    let rotated = false;
    const onClick = (i, P) => (title(P) === "Create Account" && i === 7 ? (rotated ? INSIDE : create(RULE)) : title(P) === "Create Account" && i === 9 ? FORGOT : "");
    const brain = (P) => (title(P) === "My Information" ? stop() : ok(TYPE3));
    const { agent, posts, P } = setup(create(""), onClick, brain, (p, b) => {
      if (p === "/api/autoapply/agent/portal-account" && b.rotate) { rotated = true; return { ok: true, rotated: true }; }
      return undefined;
    });
    await agent.run(1, { url: URL1, company: "Acme" });
    const rot = portal(posts, "rotate");
    assert(rot.length === 1 && rot[0].host === HOST && rot[0].error === RULE, "one rotation, with the site's error: " + JSON.stringify(rot));
    assert(title(P) === "My Information", "the retry created the account: " + title(P));
    assert(P.clicks.filter((c) => c === "7:Create Account").length === 2, "created twice, no more: " + P.clicks.join(", "));
    assert(!P.clicks.some((c) => /Forgot/.test(c) || c.startsWith("9:")), "Forgot password was never clicked: " + P.clicks.join(", "));
    const nudged = steps(posts).find((b) => JSON.stringify(b.nudges || []).includes("A new password that meets the rule is saved"));
    assert(nudged, "the brain is told to type the new password");
    assert(!steps(posts).some((b) => JSON.stringify(b).includes("await_email")) && !/emailed a reset link|resetting the password/.test(log()), "no reset, no waiting for mail");
    assert(portal(posts, "confirm").length === 1, "the account that now works is confirmed: " + JSON.stringify(portal(posts, "confirm")));
  }

  // ---- 2. the rule error again after the fresh password: a truthful park ---------------------
  {
    const onClick = (i, P) => (title(P) === "Create Account" && i === 7 ? create(RULE) : title(P) === "Create Account" && i === 9 ? FORGOT : "");
    const { agent, posts, P } = setup(create(""), onClick, () => ok(TYPE3));
    const r = await agent.run(2, { url: URL1, company: "Acme" });
    assert(r.parked && r.kind === "person", "parks " + JSON.stringify(r));
    assert(parkMsg(posts) === createBlockedReason("Acme", RULE), "with the site's own error: " + parkMsg(posts));
    assert(portal(posts, "rotate").length === 1, "one rotation only");
    assert(P.clicks.filter((c) => c === "7:Create Account").length === 2 && !P.clicks.some((c) => c.startsWith("9:")), "two tries, no Forgot password: " + P.clicks.join(", "));
    assert(!/did not arrive|emailed a reset link|resetting the password/.test(log()), "never a wait for mail");
  }

  // ---- 3. the engine refuses to rotate (the account worked before): park, no retry -----------
  {
    const onClick = (i, P) => (title(P) === "Create Account" && i === 7 ? create(RULE) : "");
    const { agent, posts, P } = setup(create(""), onClick, () => ok(TYPE3), (p, b) =>
      (p === "/api/autoapply/agent/portal-account" && b.rotate ? { ok: false, reason: "account-in-use" } : undefined));
    const r = await agent.run(3, { url: URL1, company: "Acme" });
    assert(r.parked && r.kind === "person" && parkMsg(posts) === createBlockedReason("Acme", RULE), "parks with the reason " + parkMsg(posts));
    assert(P.clicks.filter((c) => c === "7:Create Account").length === 1, "no second try: " + P.clicks.join(", "));
  }

  // ---- 3b. the brain itself reaches for Forgot password after a rule error: held -------------
  {
    let n = 0;
    const onClick = (i, P) => (title(P) === "Create Account" && i === 7 ? create(RULE) : title(P) === "Create Account" && i === 9 ? FORGOT : "");
    const brain = () => (++n === 1 ? ok(TYPE3) : n === 2 ? ok([{ op: "click", index: 9 }]) : stop());
    const { agent, posts, P } = setup(create(""), onClick, brain);
    await agent.run(4, { url: URL1, company: "Acme" });
    assert(!P.clicks.some((c) => c.startsWith("9:")) && title(P) === "Create Account", "the click was held: " + P.clicks.join(", "));
    assert(steps(posts).some((b) => JSON.stringify(b.observations || []).includes("Forgot password is only for an account the site already has")), "and the brain is told why");
  }

  // ---- 4. "an account already exists": the reset path stays open -----------------------------
  {
    let n = 0;
    const onClick = (i, P) => (title(P) === "Create Account" && i === 7 ? create(EXISTS) : title(P) === "Create Account" && i === 9 ? FORGOT : "");
    const brain = (P) => (title(P) === "Forgot Password" ? stop() : ++n === 1 ? ok(TYPE3) : ok([{ op: "click", index: 9 }]));
    const { agent, posts, P } = setup(create(""), onClick, brain);
    await agent.run(5, { url: URL1, company: "Acme" });
    assert(portal(posts, "rotate").length === 0, "an existing account is never rotated: " + JSON.stringify(portal(posts, "rotate")));
    assert(title(P) === "Forgot Password" && P.clicks.includes("9:Create Account"), "Forgot password was allowed: " + P.clicks.join(", "));
    assert(!parkMsg(posts).startsWith("I could not create an account"), "not a create park: " + parkMsg(posts));
  }

  // ---- 5. landing on the create form is not a failed sign-in ---------------------------------
  {
    // The second employer: Create form -> "Sign In" link -> one sign-in that fails (no account
    // yet) -> the shell used to count two failures, click Create Account, then Forgot password
    // on the EMPTY create form and wait four minutes for a mail.
    let n = 0;
    const onClick = (i, P) => {
      const t = title(P);
      if (t === "Create Account") return i === 8 ? SIGNIN : i === 9 ? FORGOT : i === 7 ? INSIDE : "";
      if (t === "Sign In") return i === 6 ? SIGNIN.replace("Sign In\n[4]", "Sign In\nError: Invalid credentials\n[4]") : i === 7 ? create("") : i === 8 ? FORGOT : "";
      return "";
    };
    const SIGN2 = [{ op: "type", index: 4, placeholder: "portal_email", sensitive: true, text: "(secret)" },
                   { op: "type", index: 5, placeholder: "portal_password", sensitive: true, text: "(secret)" }, { op: "click", index: 6 }];
    const brain = (P) => {
      n++;
      if (title(P) === "My Information" || n > 12) return stop();
      if (title(P) === "Create Account") return n === 1 ? ok([{ op: "click", index: 8 }]) : ok(TYPE3);
      return ok(SIGN2);
    };
    const { agent, posts, P } = setup(create(""), onClick, brain);
    await agent.run(6, { url: URL1, company: "Acme" });
    assert(!P.clicks.some((c) => /^9:Create Account|^8:Sign In/.test(c)) && !/resetting the password|emailed a reset link/.test(log()),
      "never Forgot password: " + P.clicks.join(", "));
    assert(title(P) === "My Information", "the account was created on the create form: " + title(P) + " via " + P.clicks.join(", "));
    assert(P.clicks.filter((c) => c === "6:Sign In").length === 2, "two real sign-in failures before Create account, not one: " + P.clicks.join(", "));
  }

  // ---- 6. a sign-in that worked is confirmed once --------------------------------------------
  {
    const onClick = (i, P) => (title(P) === "Sign In" && i === 6 ? INSIDE : "");
    const brain = (P) => (title(P) === "My Information" ? stop() : ok([{ op: "click", index: 6 }]));
    const { agent, posts } = setup(SIGNIN, onClick, brain);
    await agent.run(7, { url: URL1, company: "Acme" });
    const c = portal(posts, "confirm");
    assert(c.length === 1 && c[0].host === HOST, "confirmed once: " + JSON.stringify(c));
  }
  console.log("ok");
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
