/* Survey EVERY queued application: which ones can actually be filled and submitted,
 * and which the site itself blocks. Fills only -- never submits -- so it is safe to run.
 *
 * The point is to stop guessing. "Assisted, your click" is shown on every card, but the
 * lanes are not equal: some forms fill in full, some sit behind a captcha the app is
 * forbidden to touch. Whatever this prints is what the queue should be telling the person
 * up front instead of after they press a button.
 */
import { _electron as electron } from "playwright";
import path from "path";
import { fileURLToPath } from "url";

const HERE = path.dirname(fileURLToPath(import.meta.url));

const app = await electron.launch({
  args: [HERE],
  cwd: HERE,
  env: { ...process.env, TAILOR_OWN_KEY: "1", ELECTRON_RUN_AS_NODE: undefined,
         PATH: "/opt/homebrew/bin:" + process.env.PATH },
});

const win = await app.firstWindow();
await win.waitForLoadState("domcontentloaded");
await win.waitForFunction(() => !!document.querySelector(".sidebar"), null, { timeout: 90_000 });
await win.click(".nav-item:has-text('Review')");
await win.waitForTimeout(2500);

const jobs = await win.evaluate(() =>
  [...document.querySelectorAll("[data-apply]")].map(b => ({
    url: b.dataset.apply,
    cv: b.dataset.cv,
    role: (b.closest(".rvw-card").querySelector("h3") || {}).innerText.replace(/\s+/g, " ").trim(),
  })));

console.log(`surveying ${jobs.length} queued applications (fill only, nothing submitted)\n`);

const rows = [];
for (const job of jobs) {
  const r = await win.evaluate(
    async ({ url }) => await window.tailorShell.assistFill(url), { url: job.url });
  const host = new URL(job.url).host;
  const verdict = !r ? "no report"
    : r.ok === false ? `BLOCKED (${r.blocker || r.reason})`
    : `filled ${r.filled}/${r.total}${r.resume_upload ? " +resume slot" : ""}`;
  rows.push({ role: job.role.slice(0, 44), host, verdict, ok: !!r && r.ok !== false });
  console.log(`${r && r.ok !== false ? "OK   " : "BLOCK"} ${job.role.slice(0, 46).padEnd(48)} ${verdict}`);
  await win.waitForTimeout(1200);
}

const fillable = rows.filter(r => r.ok).length;
console.log(`\n${fillable} of ${rows.length} can be filled automatically; `
          + `${rows.length - fillable} are blocked by the site.`);
await app.close();
