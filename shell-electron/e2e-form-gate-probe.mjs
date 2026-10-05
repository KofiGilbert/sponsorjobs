/* What KIND of captcha is on these forms, and does it actually stop filling?
 *
 * The guard currently refuses to fill any page that merely CONTAINS a captcha. But
 * filling a form is not getting past a captcha -- the challenge still stands, and the
 * person still solves it and presses submit. If these are invisible reCAPTCHA v3 (a
 * background score with nothing to solve), the app is refusing to do the one thing it is
 * both allowed and needed to do.
 */
import { _electron as electron } from "playwright";
import path from "path";
import { fileURLToPath } from "url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const app = await electron.launch({
  args: [HERE], cwd: HERE,
  env: { ...process.env, TAILOR_OWN_KEY: "1", ELECTRON_RUN_AS_NODE: undefined,
         PATH: "/opt/homebrew/bin:" + process.env.PATH },
});
const win = await app.firstWindow();
await win.waitForLoadState("domcontentloaded");
await win.waitForFunction(() => !!document.querySelector(".sidebar"), null, { timeout: 90_000 });
await win.click(".nav-item:has-text('Review')");
await win.waitForTimeout(2000);

const url = await win.locator("[data-apply]").first().getAttribute("data-apply");
console.log("form:", url, "\n");

// Open it in the pane WITHOUT going through the guard, so we can look at the real page.
await win.evaluate(u => window.open(u, "_blank"), url);
await win.waitForTimeout(9000);

const cdp = await fetch("http://127.0.0.1:9223/json/list").then(r => r.json());
const pane = cdp.find(t => t.type === "page" && t.url.includes("greenhouse"));
if (!pane) {
  console.log("form did not open in the pane");
  console.log(cdp.map(t => "  " + t.type + " " + t.url.slice(0, 80)).join("\n"));
  await app.close();
  process.exit(1);
}

const sock = new WebSocket(pane.webSocketDebuggerUrl);
await new Promise(r => sock.onopen = r);
let id = 0;
const send = (m, p = {}) => new Promise(res => {
  const i = ++id;
  const h = e => {
    const j = JSON.parse(e.data);
    if (j.id === i) { sock.removeEventListener("message", h); res(j.result); }
  };
  sock.addEventListener("message", h);
  sock.send(JSON.stringify({ id: i, method: m, params: p }));
});
const ev = async x =>
  (await send("Runtime.evaluate", { expression: x, returnByValue: true })).result?.value;

console.log(await ev(`(() => {
  const isCaptcha = s => /recaptcha|hcaptcha|turnstile/.test(s || "");
  const frames = [...document.querySelectorAll('iframe')];
  const captchaFrames = frames.filter(f => isCaptcha(f.src));
  // An INTERACTIVE challenge is a captcha frame actually laid out at a usable size.
  // Invisible v3 mounts a 0x0 (or offscreen badge) frame with nothing to solve.
  const interactive = captchaFrames.filter(f => {
    const r = f.getBoundingClientRect();
    return r.width > 40 && r.height > 40;
  }).map(f => f.src.slice(0, 70));
  const fields = [...document.querySelectorAll('input,textarea,select')]
    .filter(e => e.type !== 'hidden' && e.offsetParent !== null);
  return JSON.stringify({
    captchaFrames: captchaFrames.length,
    frameSizes: captchaFrames.map(f => {
      const r = f.getBoundingClientRect();
      return Math.round(r.width) + 'x' + Math.round(r.height);
    }),
    interactiveChallenges: interactive,
    invisible_v3_likely: captchaFrames.length > 0 && interactive.length === 0,
    fillableFields: fields.length,
    fileInputs: document.querySelectorAll('input[type=file]').length,
  }, null, 1);
})()`));

sock.close();
await app.close();
