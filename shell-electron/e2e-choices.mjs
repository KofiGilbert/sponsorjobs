/* End-to-end test of the applicant brain's choices (docs/applicant-brain.md, Phase 5): "show one
 * of each depending on where I am applying", driving the REAL Electron app.
 *
 *   node e2e-choices.mjs
 *
 * Runs the engine with the test model (RESUME_AGENT_FAKE_LLM=1) in a throwaway data folder, so
 * nothing real is touched and nothing is sent anywhere. The dev app must be closed first (one
 * engine port, 57000, and one CDP port). The saved profile holds two day jobs that overlap in
 * 2020 and 2021 (Stanbic Bank and Viva) and a current one (BlackOrigin). Asserts on what the
 * window shows:
 *  1. a bank job's resume asks once whether to show both overlapping roles or one per job;
 *  2. switching Viva off on the edit page dims it, rebuilds, and ONE line asks "Hide Viva for
 *     other bank and finance jobs too?"; "Yes, bank and finance jobs" saves the choice;
 *  3. a second bank job hides Viva with no ask, and says why ("Hidden for bank and finance jobs.");
 *  4. a startup job shows Viva (the bank choice does not reach it);
 *  5. the choice is listed on Your answers, Resume choices, and Delete removes it;
 *  6. the next bank job shows the scorer's pick (Viva back on), and the overlap is not asked again.
 */
import { _electron as electron } from "playwright";
import { spawn, spawnSync } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";
import { fileURLToPath } from "url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.join(HERE, "..");
const PY = process.env.TAILOR_PYTHON || "/Users/kofigilbert/code/resume-agent/.venv/bin/python";
const BASE = "http://127.0.0.1:57000";
const results = [];
const check = (name, pass, detail = "") => {
  results.push({ name, pass, detail });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  - " + detail : ""}`);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const VIVA = "role:viva|data analyst";
const STANBIC = "role:stanbic bank|credit analyst";

// ---- 1. a throwaway data folder with the saved profile ----------------------------------------
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), "sj-choices-"));
const seed = spawnSync(PY, ["-c", `
import os, sys
sys.path.insert(0, ${JSON.stringify(REPO)})
os.environ["RESUME_AGENT_DATA_DIR"] = ${JSON.stringify(DATA)}
from pathlib import Path
from intake.memory import ConversationMemory
data = Path(${JSON.stringify(DATA)})
prof = {"identity": {"name": "Kofi Test", "email": "kofi.test@example.com", "phone": "312-555-0100", "location": "Chicago, IL"},
        "summary": "Analyst with banking and data experience. Founder of BlackOrigin, a job search startup.",
        "experience": [
          {"org": "BlackOrigin", "location": "Remote", "roles": [{"title": "Founder", "dates": "Jan 2022 - Present", "bullets": [
            "Built a job search product in Python and SQL used by 300 people.", "Ran product and growth for the startup from idea to paying users."]}]},
          {"org": "Stanbic Bank", "location": "Accra, Ghana", "roles": [{"title": "Credit Analyst", "dates": "Jan 2020 - Dec 2021", "bullets": [
            "Underwrote commercial loans worth $12M a year for the corporate bank.", "Built credit risk models in Excel and SQL for the lending team."]}]},
          {"org": "Viva", "location": "Accra, Ghana", "roles": [{"title": "Data Analyst", "dates": "Mar 2020 - Nov 2021", "bullets": [
            "Built SQL dashboards for the sales team every week.", "Cleaned customer data with Python and pandas."]}]}],
        "projects": [{"org": "Job Radar", "title": "Builder", "dates": "Jan 2023 - Mar 2023", "bullets": ["Built a SQL and Python job tracker with dashboards."]}],
        "education": [{"school": "University of Ghana", "degree": "BSc Economics", "date": "May 2019", "location": "Accra, Ghana"}],
        "skills": {"Data": "SQL, Python, Excel", "Finance": "Credit analysis, underwriting"},
        "extracurricular": [{"title": "Volunteer tutor", "date": "Jan 2018 - May 2019", "bullets": ["Taught math to 40 students."]}],
        "interests": "Chess, running"}
ConversationMemory(str(data / "resume_agent.db")).save("default", prof, {"identity": prof["identity"]}, [])
print("ok")
`], { encoding: "utf8" });
if (seed.status !== 0) { console.error(seed.stderr); process.exit(1); }
check("seeded a profile with two overlapping day jobs", /ok/.test(seed.stdout));

// ---- 2. the engine on the test model, then the real app --------------------------------------
const engineEnv = { ...process.env, RESUME_AGENT_DATA_DIR: DATA, RESUME_AGENT_DEV: "1", RESUME_AGENT_FAKE_LLM: "1",
                    RESUME_AGENT_CRED_FILE: path.join(DATA, "credentials.env"), TAILOR_SERVER_ONLY: "1" };
const engine = spawn(PY, ["-m", "ui.app"], { cwd: REPO, env: engineEnv, stdio: ["ignore", "ignore", "pipe"] });
let engineErr = "";
engine.stderr.on("data", (d) => { engineErr += d.toString(); });
let up = false;
for (let i = 0; i < 60 && !up; i++) {
  up = !!(await fetch(BASE + "/api/health").catch(() => null))?.ok;
  if (!up) await sleep(1000);
}
check("engine up on the test model", up, up ? "" : engineErr.slice(-400));
const stopEngine = () => { try { engine.kill(); } catch {} };
process.on("exit", stopEngine);
const brain = await fetch(BASE + "/api/brain/status").then((r) => r.json()).catch(() => null);
check("the brain is on", !!(brain && brain.active), JSON.stringify(brain && brain.choices));

const app = await electron.launch({
  args: [HERE],
  cwd: HERE,
  env: { ...process.env, ELECTRON_RUN_AS_NODE: undefined, PATH: "/opt/homebrew/bin:" + process.env.PATH,
         TAILOR_PYTHON: PY, RESUME_AGENT_DATA_DIR: DATA, RESUME_AGENT_DEV: "1", RESUME_AGENT_FAKE_LLM: "1",
         RESUME_AGENT_CRED_FILE: path.join(DATA, "credentials.env") },
});
const win = await app.firstWindow();
await win.waitForLoadState("domcontentloaded");
await win.waitForFunction(() => !!document.querySelector(".sidebar"), null, { timeout: 90_000 });
check("app window opens", true, await win.title());

// A build for one job: the session starts and builds from the saved profile (the same calls the
// Home box and "Tailor from my profile" make), then the builder opens on its edit page.
const build = async (jd) => {
  const st = await win.evaluate(async (jd) => {
    const post = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) }).then((r) => r.json());
    await post("/api/session/start", { jd });
    return post("/api/session/answer", { text: "Nothing new. Everything is in my saved profile. Build it." });
  }, jd);
  await win.click("[data-nav=dashboard]");             // Home refreshes the open-resume entry
  await win.waitForFunction(() => !!document.querySelector("#liveNavBtn"), null, { timeout: 60_000 });
  await win.evaluate(() => document.querySelector("#liveNavBtn").click());
  // the builder has restored THIS job's resume (not the one open before)
  await win.waitForFunction((co) => (document.querySelector("#builderRole")?.textContent || "").includes(co),
                            (st && st.company) || "", { timeout: 60_000 });
  await win.waitForSelector("#editToggle:not([hidden])", { timeout: 60_000 });
  if (await win.locator("#paperWrap[hidden]").count()) await win.click("#editToggle");
  await win.waitForSelector(`.cv-sw[data-show-id="${VIVA}"]`, { timeout: 30_000 });
  return st;
};
const sw = (id) => win.evaluate((id) => document.querySelector(`.cv-sw[data-show-id="${id}"]`)?.getAttribute("aria-checked"), id);
const why = (id) => win.evaluate((id) => document.querySelector(`.cv-sw[data-show-id="${id}"]`)?.closest(".cv-show")?.querySelector(".cv-why")?.textContent || "", id);
const line = () => win.evaluate(() => { const b = document.querySelector("#choiceLine"); return b && !b.hidden ? b.textContent.trim() : ""; });
const waitLine = async (re, ms) => {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { const t = await line(); if (re.test(t)) return t; await sleep(300); }
  return await line();
};

// ---- 3. the first bank job: one question about the overlap, then the switch ------------------
const BANK = "Credit Analyst at Acme Bank\nCommercial banking team. Underwriting, lending, credit analysis, loan portfolio, SQL.";
const st1 = await build(BANK);
check("the bank resume builds", st1 && st1.phase === "review", st1 && (st1.messages || []).join(" | "));
const ov = await waitLine(/same time/i, 10_000);
check("it asks once about the overlapping roles", /Stanbic Bank and Viva at the same time/.test(ov) && /one per job/i.test(ov), ov);
check("both overlapping roles show to start with", (await sw(VIVA)) === "true" && (await sw(STANBIC)) === "true");
await win.click(`.cv-sw[data-show-id="${VIVA}"]`);
const ask = await waitLine(/hide viva/i, 60_000);
check("switching Viva off asks ONE line", ask.startsWith("Hide Viva for other bank and finance jobs too?")
      && /Yes, bank and finance jobs/.test(ask) && /Only Acme Bank/.test(ask) && /Just this time/.test(ask), ask);
check("Viva is off and dimmed on the page", (await sw(VIVA)) === "false"
      && await win.evaluate((id) => !!document.querySelector(`.cv-sw[data-show-id="${id}"]`).closest(".cv-block").querySelector(".cv-body-in.is-off"), VIVA));
await win.locator("#choiceLine button", { hasText: "Yes, bank and finance jobs" }).click();
await win.waitForFunction(() => document.querySelector("#choiceLine").hidden, null, { timeout: 30_000 });
const rules1 = await fetch(BASE + "/api/brain/rules").then((r) => r.json());
check("the choice is saved only after the tap", (rules1.rules || []).map((r) => r.text).join() === "Bank and finance jobs: hide Viva.",
      JSON.stringify(rules1.rules));
await win.click("#acceptBtn");
await sleep(3000);

// ---- 4. a second bank job: hidden, no ask ---------------------------------------------------
await build("Loan Officer at First Lending Bank\nRetail bank lending role. Mortgage, loan origination, underwriting.");
await sleep(500);
check("a second bank job hides Viva", (await sw(VIVA)) === "false");
check("and says why", (await why(VIVA)).startsWith("Hidden for bank and finance jobs."), await why(VIVA));
check("with nothing asked", (await line()) === "", await line());

// ---- 5. a startup job: Viva can show --------------------------------------------------------
await build("Data Analyst at Rocketly\nWe are a seed-stage startup. SQL, Python, dashboards for our sales team.");
check("a startup job shows Viva", (await sw(VIVA)) === "true");

// ---- 6. the list on Your answers: delete it -------------------------------------------------
await win.click("[data-nav=autoapply]");
await win.waitForSelector("#aaAnsBtn", { timeout: 30_000 });
await win.click("#aaAnsBtn");
await win.waitForSelector("#ansChoiceList .ans-row", { timeout: 30_000 });
const row = await win.locator("#ansChoiceList .ans-row").first().textContent();
check("Your answers lists the choice in plain words", /Bank and finance jobs: hide Viva\./.test(row) && /Used on \d resume/.test(row), row);
await win.locator('#ansChoiceList [data-chc-act="delete"]').first().click();
await win.waitForFunction(() => !document.querySelector("#ansChoiceList .ans-row"), null, { timeout: 30_000 });
const rules2 = await fetch(BASE + "/api/brain/rules").then((r) => r.json());
check("Delete removes it", (rules2.rules || []).length === 0, JSON.stringify(rules2.rules));

// ---- 7. the next bank job shows the scorer's pick --------------------------------------------
await build("Credit Analyst at Third Bank\nCommercial banking team. Underwriting, lending, credit analysis.");
await sleep(500);
check("the next bank job shows the scorer's pick (Viva back on)", (await sw(VIVA)) === "true");
check("the overlap is not asked again", !/same time/i.test(await line()), await line());
const status = await fetch(BASE + "/api/brain/status").then((r) => r.json()).catch(() => ({}));
check("the saved resume is in the brain's history", ((status.choices || {}).applications || 0) >= 1, JSON.stringify(status.choices));

const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
await app.close();
stopEngine();
fs.rmSync(DATA, { recursive: true, force: true });
process.exit(failed.length ? 1 : 0);
