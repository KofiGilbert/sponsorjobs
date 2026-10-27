/* End-to-end: the in-app browser pane lands exactly on its slot at every app zoom.
 *
 * Written after the pane was cut off at 76% zoom (2026-10-27): the unit test pins the math
 * (tests/test_pane_geometry.py), this one drives the REAL Electron window, zooms it, opens the
 * pane and compares the native view's bounds with the slot the page draws. Run before every
 * release, and after any change to main.js layoutPane, preload, or the panel layout:
 *
 *   node e2e-pane-zoom.mjs
 */
import { _electron as electron } from "playwright";
import path from "path";
import { fileURLToPath } from "url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ZOOMS = [0.67, 0.76, 1, 1.25, 1.5];
let failed = 0;
const check = (name, pass, detail = "") => {
  if (!pass) failed++;
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  " + detail : ""}`);
};

const app = await electron.launch({
  args: [HERE], cwd: HERE,
  env: { ...process.env, TAILOR_OWN_KEY: "1", ELECTRON_RUN_AS_NODE: undefined,
         PATH: "/opt/homebrew/bin:" + process.env.PATH },
});
try {
  const page = await app.firstWindow();
  await page.waitForFunction(() => !!window.tailorShell, null, { timeout: 60000 });
  // A real https page: the pane only opens web addresses (data: and file: are refused).
  await page.evaluate(() => window.tailorShell.openBrowser("https://example.com/"));
  await page.waitForFunction(() => {
    const s = document.querySelector("#browserSlot");
    return s && s.getBoundingClientRect().width > 100;
  }, null, { timeout: 30000 });
  for (const z of ZOOMS) {
    await app.evaluate(({ BrowserWindow }, zoom) => BrowserWindow.getAllWindows()[0].webContents.setZoomFactor(zoom), z);
    await page.waitForTimeout(600);                       // resize -> sendBounds -> layoutPane
    const slot = await page.evaluate(() => {
      const r = document.querySelector("#browserSlot").getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height };
    });
    const pane = await app.evaluate(({ BrowserWindow }) => {
      const kids = BrowserWindow.getAllWindows()[0].contentView.children;
      return kids.length ? kids[kids.length - 1].getBounds() : null;
    });
    const want = Object.fromEntries(Object.entries(slot).map(([k, v]) => [k, Math.round(v * z)]));
    const ok = pane && ["x", "y", "width", "height"].every((k) => Math.abs(pane[k] - want[k]) <= 2);
    check(`pane on its slot at ${Math.round(z * 100)}% zoom`, ok,
          `pane=${JSON.stringify(pane)} slot*zoom=${JSON.stringify(want)}`);
  }
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.setZoomFactor(1));
} finally {
  await app.close();
}
process.exit(failed ? 1 : 0);
