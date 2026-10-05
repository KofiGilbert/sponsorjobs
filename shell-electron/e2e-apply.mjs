/* End-to-end test of the assisted-apply path, driving the REAL Electron app.
 *
 * Written because every previous claim about this screen was made from unit-level
 * evidence (an endpoint returned the right URL, a handler returned ok:true) while the
 * thing the person actually does -- open the queue and press Apply -- was never once
 * exercised. This launches the app the way a user starts it and asserts on what the
 * window shows.
 *
 *   node e2e-apply.mjs           # fill + attach only, stops before submitting
 *   node e2e-apply.mjs --submit  # really submits the first queued application
 */
import { _electron as electron } from "playwright";
import path from "path";
import { fileURLToPath } from "url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SUBMIT = process.argv.includes("--submit");
const results = [];
const check = (name, pass, detail = "") => {
  results.push({ name, pass, detail });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
};

const app = await electron.launch({
  args: [HERE],
  cwd: HERE,
  env: { ...process.env, TAILOR_OWN_KEY: "1", ELECTRON_RUN_AS_NODE: undefined,
         PATH: "/opt/homebrew/bin:" + process.env.PATH },
});

const win = await app.firstWindow();
await win.waitForLoadState("domcontentloaded");
check("app window opens", true, await win.title());

// The engine can take a moment to come up on a cold start.
await win.waitForFunction(() => !!document.querySelector(".sidebar"), null, { timeout: 90_000 });

await win.click(".nav-item:has-text('Review')");
await win.waitForTimeout(2500);

const cards = await win.locator(".rvw-card").count();
check("review queue lists packages", cards > 0, `${cards} cards`);

const applyBtns = win.locator("[data-apply]");
const applyCount = await applyBtns.count();
check("assisted cards expose an Apply action", applyCount > 0, `${applyCount} of ${cards}`);

if (applyCount === 0) {
  console.log("\nnothing to apply with; stopping");
  await app.close();
  process.exit(1);
}

const first = applyBtns.first();
const url = await first.getAttribute("data-apply");
const cv = await first.getAttribute("data-cv");
check("Apply points at an application FORM, not the advert",
      /greenhouse\.io\/embed\/job_app|\/apply/.test(url || ""), url || "(none)");
check("Apply carries the tailored CV", !!cv && cv.endsWith(".pdf"), cv || "(none)");

// Drive the real handler. Without --submit we stop at the wall check so nothing is sent.
const report = await win.evaluate(async ({ url, cv, submit }) => {
  if (!window.tailorShell) return { error: "no shell bridge" };
  return submit ? await window.tailorShell.assistApply(url, cv)
                : await window.tailorShell.assistFill(url);
}, { url, cv, submit: SUBMIT });

console.log("\nhandler report:", JSON.stringify(report, null, 1));
check("form was filled from the profile", !!report && report.ok !== false && report.filled > 0,
      report && report.ok === false ? `reason=${report.reason}` : `filled ${report?.filled}/${report?.total}`);

// Look at the actual page in the pane, which is the only real evidence.
const pages = app.windows();
const paneState = await win.evaluate(async () => {
  const r = await fetch("http://127.0.0.1:9223/json/list").then(x => x.json());
  const p = r.find(t => t.type === "page" && !t.url.startsWith("http://127.0.0.1:57000"));
  return p ? { url: p.url, title: p.title } : null;
});
check("application form is open in the pane", !!paneState,
      paneState ? paneState.url.slice(0, 80) : "no external page");

if (SUBMIT) {
  check("submitted", !!report?.submitted,
        report?.submitted ? (report.confirmed ? "site confirmed" : "no confirmation text")
                          : `stopped: ${report?.reason}`);
  check("resume attached", !!report?.resume_attached);
}

const failed = results.filter(r => !r.pass);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
await app.close();
process.exit(failed.length ? 1 : 0);
