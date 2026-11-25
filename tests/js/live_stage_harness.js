// The live interview (Round 2) on the recorded round's stage, in a real Chromium with a fake
// camera and microphone. NO real call is made: every /api answer is stubbed here, and the video
// SDK (vendor/daily-*.js) is replaced by a stand-in call object that draws an interviewer on a
// canvas and records what the page asked of it. It checks what the stage promises:
//   - focus mode from the intro to "All done": the app's chrome is hidden, and comes back on leave;
//   - the intro is Round 1's card: one-line title, a stat row of the engine's real numbers, three
//     points that are true for a live call, and ONE primary action;
//   - with no live interview left there are no plans and no prices on this screen: one line, one
//     primary that opens the plans panel, a quiet "Not now";
//   - the device check is Round 1's, the remembered camera and microphone open first, and the
//     ones chosen there are the ones handed to the call;
//   - the call: one large interviewer tile, a small self view in its corner, the control bar
//     centred under the video (microphone, camera, settings, end), a speaking outline, the end
//     button asks first; without a camera there is no dead camera control;
//   - "All done" on the same card, then the report.
//
// Usage: node live_stage_harness.js <baseUrl> <playwrightDir> [--shots <dir>]
"use strict";
const path = require("path");

const [, , BASE, PW_DIR, ...REST] = process.argv;
if (!BASE || !PW_DIR) { console.error("need <baseUrl> <playwrightDir>"); process.exit(2); }
const SHOTS = REST.includes("--shots") ? REST[REST.indexOf("--shots") + 1] : "";
const ONLY = (REST.find((a) => a.startsWith("--only=")) || "").slice(7);
const { chromium } = require(path.join(PW_DIR, "index.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const near = (a, b, tol) => Math.abs(a - b) <= (tol == null ? 1 : tol);

// Two cameras and two microphones over Chromium's fake device (as in the recorded round's walk).
const MULTI = (noCam) => {
  const md = navigator.mediaDevices, real = md.getUserMedia.bind(md);
  let list = [
    { kind: "audioinput", deviceId: "default", label: "Default - Built-in Microphone (Built-in)", groupId: "g1" },
    { kind: "audioinput", deviceId: "mic-built", label: "Built-in Microphone (Built-in)", groupId: "g1" },
    { kind: "audioinput", deviceId: "mic-head", label: "Headset Microphone (0b0e:24c7)", groupId: "g2" },
  ];
  if (!noCam) list.push({ kind: "videoinput", deviceId: "cam-built", label: "Built-in Camera (05ac:8514)", groupId: "g3" },
                        { kind: "videoinput", deviceId: "cam-usb", label: "USB Webcam (046d:085c)", groupId: "g4" });
  md.enumerateDevices = async () => list.map((d) => ({ ...d }));
  md.getUserMedia = async (c) => {
    const want = {}, plain = {};
    for (const [key, kind] of [["video", "videoinput"], ["audio", "audioinput"]]) {
      if (!c[key]) continue;
      if (key === "video" && noCam) { const e = new Error("none"); e.name = "NotFoundError"; throw e; }
      const id = c[key] !== true && c[key].deviceId && c[key].deviceId.exact;
      if (id && !list.some((d) => d.kind === kind && d.deviceId === id)) { const e = new Error("gone"); e.name = "OverconstrainedError"; throw e; }
      want[key] = id || (list.find((d) => d.kind === kind && d.deviceId !== "default") || {}).deviceId;
      plain[key] = true;
    }
    const s = await real(plain);
    for (const t of s.getTracks()) { const id = want[t.kind]; t.getSettings = () => ({ deviceId: id }); }
    return s;
  };
};

// The stand-in for the video SDK. It never touches the network.
const FAKE_DAILY = `(() => {
  function canvasTrack(draw) {
    const c = document.createElement("canvas"); c.width = 1280; c.height = 720;
    const g = c.getContext("2d"); let t = 0; draw(g, t); setInterval(() => draw(g, ++t), 120);
    return c.captureStream(8).getVideoTracks()[0];
  }
  // A drawn stand-in for the interviewer: a soft office backdrop and a seated figure.
  function interviewer(g, t) {
    const bg = g.createLinearGradient(0, 0, 0, 720); bg.addColorStop(0, "#d9d4cc"); bg.addColorStop(1, "#b9b2a7");
    g.fillStyle = bg; g.fillRect(0, 0, 1280, 720);
    g.fillStyle = "rgba(255,255,255,.35)"; g.fillRect(900, 90, 260, 330);
    g.fillStyle = "#8d8577"; for (let i = 0; i < 5; i++) g.fillRect(110 + i * 46, 150, 30, 150 - i * 12);
    g.fillStyle = "#a39a8c"; g.fillRect(90, 300, 300, 14);
    g.fillStyle = "#2e3a4f"; g.beginPath(); g.ellipse(640, 760, 330, 250, 0, Math.PI, 0); g.fill();
    g.fillStyle = "#f2efe9"; g.beginPath(); g.moveTo(590, 520); g.lineTo(640, 600); g.lineTo(690, 520); g.fill();
    g.fillStyle = "#b07d5b"; g.fillRect(608, 440, 64, 90);
    g.beginPath(); g.ellipse(640, 360, 98, 122, 0, 0, Math.PI * 2); g.fill();
    g.fillStyle = "#2b211c"; g.beginPath(); g.ellipse(640, 290, 104, 70, 0, Math.PI, 0); g.fill();
    g.fillStyle = "#2b211c"; const blink = t % 30 === 0 ? 2 : 9;
    g.fillRect(598, 352, 18, blink); g.fillRect(664, 352, 18, blink);
    g.fillStyle = "#7a4a3a"; g.fillRect(618, 420 + (t % 6 < 3 ? 0 : 2), 44, 6);
  }
  window.Daily = { createCallObject(opts) {
    window.__dailyOpts = JSON.parse(JSON.stringify(opts));
    const h = {}; let aud = true, vid = opts.videoSource !== false, local = null;
    const emit = (ev, e) => (h[ev] || []).forEach((f) => f(e));
    const me = () => ({ participant: { local: true, tracks: { video: vid && local ? { state: "playable", persistentTrack: local } : { state: "off" } } } });
    const call = {
      on(ev, fn) { (h[ev] = h[ev] || []).push(fn); return call; },
      async join(o) {
        window.__joined = o;
        if (vid) { try { local = (await navigator.mediaDevices.getUserMedia({ video: true })).getVideoTracks()[0]; } catch (e) { local = null; } }
        emit("track-started", me());
        if (!window.__holdInterviewer) setTimeout(() => call.arrive(), 300);
      },
      arrive() { emit("track-started", { participant: { local: false, tracks: { video: { state: "playable", persistentTrack: canvasTrack(interviewer) }, audio: { state: "off" } } } }); },
      speak(role, on) { emit("app-message", { data: { event_type: on ? "conversation.started_speaking" : "conversation.stopped_speaking", properties: { role } } }); },
      sendAppMessage(m) { (window.__sent = window.__sent || []).push(m); },
      localAudio() { return aud; }, setLocalAudio(v) { aud = v; },
      localVideo() { return vid; }, setLocalVideo(v) { vid = v; emit("track-started", me()); },
      async getInputDevices() { return { camera: { deviceId: opts.videoSource || "" }, mic: { deviceId: opts.audioSource || "" } }; },
      async setInputDevicesAsync(d) { (window.__setDev = window.__setDev || []).push(d); return {}; },
      leave() { window.__left = true; return Promise.resolve(); }, destroy() {},
    };
    window.__daily = call; return call;
  } };
})();`;

const REPORT = {
  score: 78, passed: true, threshold: 70, duration_s: 1140, round: 2, is_practice: true,
  why: "Clear stories with real numbers. Two answers ran long before the result.",
  improvements: ["Lead with the result. Say the outcome in your first sentence, then explain how.",
                 "Name your own part. You said we for most of the launch story.",
                 "Close each answer. End on what changed, not on the next task."],
  competencies: [{ name: "Ownership", score: 82, count: 2 }, { name: "Communication", score: 74, count: 2 }, { name: "Product sense", score: 71, count: 1 }],
  answers: [{ q: "Tell me about a product you launched.", transcript: "We launched the new onboarding flow and um it cut drop off by 18 percent.", competency: "Ownership", score: 80, feedback: "Good result. Say what you did yourself." },
            { q: "How do you decide what to build next?", transcript: "I look at the data first, then I talk to five customers.", competency: "Product sense", score: 71, feedback: "Give one real example." }],
  transcript: [{ role: "interviewer", content: "Tell me about a product you launched." }, { role: "candidate", content: "We launched the new onboarding flow." }],
  disclaimer: "This is a practice simulation, not a real interview or hiring decision.",
};

async function stub(page, o) {
  const json = (route, body, status) => route.fulfill({ status: status || 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/static/vendor/daily-*.js", (r) => r.fulfill({ status: 200, contentType: "application/javascript", body: FAKE_DAILY }));
  await page.route("**/api/preps", (r) => r.request().method() === "POST"
    ? json(r, { prep: { id: "p1", role: "Product Manager", company: "Acme Robotics Foundation", questions: [] } }) : r.continue());
  await page.route("**/api/interview/round2/status", (r) => json(r, o.status()));
  await page.route("**/api/billing/offers", (r) => json(r, { plans: [], passes: [], packs: [], current: null, reachable: true }));
  await page.route("**/api/interviews/cvi/start", (r) => { o.started = r.request().postDataJSON();
    return json(r, { interview_id: "iv1", join_url: "https://example.daily.co/room?t=tok", conversation_id: "c1", max_minutes: 20, source: "plan",
                     timing: { total: 1200, answer_nudge: 180, answer_step_in: 240, pause_wait: 15, last_question: 930, candidate_questions: 1110,
                               close_wait: 20, hard_end: 1230, think_grace: 10, answer_grace: 60, minutes: 20 }, role: "Product Manager", company: "Acme Robotics Foundation" }); });
  await page.route("**/api/interviews/cvi/heartbeat", (r) => json(r, { remaining: 900, stop: false, source: "plan" }));
  await page.route("**/api/interviews/cvi/end", async (r) => { await new Promise((ok) => { o.release = ok; }); await json(r, { report: REPORT, interview_id: "iv1", interview: { role: "Product Manager" } }); });
  await page.route("**/api/logo*", (r) => r.fulfill({ status: 404, body: "" }));
}

async function open(browser, { w, h, theme, status, noCam, saved }) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h } });
  const page = await ctx.newPage();
  const errors = []; page.on("pageerror", (e) => errors.push(String(e)));
  await page.addInitScript(([t, sv]) => { try {
    localStorage.setItem("tailor-theme", t); localStorage.setItem("tailor_onboarded_v1", "1");
    if (sv) { localStorage.setItem("sj.r1.cam", JSON.stringify(sv.cam)); localStorage.setItem("sj.r1.mic", JSON.stringify(sv.mic)); }
  } catch (e) {} }, [theme || "light", saved || null]);
  await page.addInitScript(MULTI, !!noCam);
  const o = { status: () => status };
  await stub(page, o);
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("load"); await page.waitForTimeout(300);
  await page.click('[data-nav="prep"]');
  await page.click("#tileRound2");
  await page.fill("#prepOpenRole", "Product Manager");
  await page.fill("#prepOpenJd", "Own the roadmap for our robotics platform.");
  await page.click("#prepStart");
  await page.waitForSelector("#r2Act .r1-btn");
  await page.waitForTimeout(250);
  return { ctx, page, o, errors };
}

const rect = (page, sel) => page.evaluate((s) => { const e = document.querySelector(s); if (!e) return null; const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height, r: r.right, b: r.bottom }; }, sel);
const shown = (page, sel) => page.evaluate((s) => { const e = document.querySelector(s); if (!e) return false; const r = e.getBoundingClientRect(); return getComputedStyle(e).display !== "none" && r.width > 0 && r.height > 0; }, sel);
const CHROME = [".sidebar", "#view-prep > .page-head", "#prepLive > .mock-top", ".privacy-line", "#prepPastLink"];
async function focusMode(page, step) { for (const sel of CHROME) assert(!(await shown(page, sel)), `${step}: ${sel} is hidden (focus mode)`); }
async function noSideScroll(page, step) {
  const d = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
  assert(d.sw <= d.cw + 1, `${step}: no sideways scroll (${JSON.stringify(d)})`);
}
// Titles stay on one line when they fit; nothing is cut.
async function oneLine(page, step) {
  const t = await page.evaluate(() => [...document.querySelectorAll("#liveStage .r1-h")].map((h) => {
    const r = document.createRange(); r.selectNodeContents(h);
    return { text: h.textContent.trim(), lines: new Set([...r.getClientRects()].filter((x) => x.width > 0).map((x) => Math.round(x.top))).size, cut: h.scrollWidth > h.clientWidth + 1 };
  }));
  for (const x of t) { assert(!x.cut, `${step}: title cut ${JSON.stringify(x)}`); if (x.text.length < 50) assert(x.lines === 1, `${step}: title on one line ${JSON.stringify(x)}`); }
}
let TAG = "";
async function shot(page, name) { if (!SHOTS) return; await page.waitForTimeout(300); await page.screenshot({ path: path.join(SHOTS, `${TAG}-${name}.png`) }); }
const primaries = (page) => page.evaluate(() => [...document.querySelectorAll("#liveStage .r1-btn-primary")].filter((b) => !b.closest("[hidden]")).length);
const cardText = (page) => page.evaluate(() => document.querySelector("#liveStage .r1-card").innerText);

const AVAILABLE = { available: true, reason: "ok", source: "plan", minutes_left: 40, can_buy: false, packs: [], tier: "standard_monthly", pass_until: 1799020800, offer: "packs" };
const NO_PLAN = { available: false, reason: "no_pass", source: null, minutes_left: null, can_buy: false, packs: [], tier: "free", pass_until: null, offer: "passes" };
const USED_UP = { available: false, reason: "no_minutes", source: null, minutes_left: 4, can_buy: true, packs: [{ id: "pack1", interviews: 1, seconds: 1200, price_label: "$9" }], tier: "standard_monthly", pass_until: 1799020800, offer: "packs" };
const DOWN = { available: false, reason: "unreachable", source: null, minutes_left: null, can_buy: false, packs: [], tier: null, pass_until: null, offer: null };

async function gates(browser, w, h, theme) {
  // No plan: one line, ONE primary that opens the plans panel, a quiet Not now. No prices.
  let { ctx, page, errors } = await open(browser, { w, h, theme, status: NO_PLAN });
  await focusMode(page, "paywall"); await oneLine(page, "paywall"); await noSideScroll(page, "paywall");
  assert((await page.textContent(".r1-card .r1-label")).trim().toLowerCase() === "round 2 · live interview", "the label names the round");
  assert((await page.textContent(".r1-card .r1-h")) === "Product Manager" && (await page.textContent(".r1-co")) === "Acme Robotics Foundation", "role is the title, company its own line");
  const stats = await page.evaluate(() => [...document.querySelectorAll("#r2Facts .r1-stat")].map((e) => [e.querySelector("b").textContent, e.querySelector("span").textContent]));
  assert(JSON.stringify(stats) === JSON.stringify([["20 min", "live, on video"], ["4 to 6", "questions, with follow-ups"], ["1", "scored report"]]), `the stat row is the engine's numbers (${JSON.stringify(stats)})`);
  assert(await page.evaluate(() => document.querySelectorAll(".r1-points li svg").length === 3), "three points with icons");
  const text = await cardText(page);
  assert(!/\$|€|£|Pass\b/.test(text), `no plan names or prices on the Round 2 screen (${JSON.stringify(text)})`);
  assert(!/(recordings|camera and mic|video) stays? on this computer/i.test(text), "nothing claims the call stays on this computer");
  assert((await primaries(page)) === 1, "one primary action");
  assert((await page.textContent("#r2Plans")).trim() === "See plans" && (await page.textContent(".r2-act-line")).trim() === "Live interviews are part of every plan.", "one line, then See plans");
  await shot(page, "1-paywall");
  await page.click("#r2Plans");
  assert(await page.evaluate(() => !document.querySelector("#upgModal").hidden), "See plans opens the plans panel (where every price lives)");
  await page.click("#upgClose");   // closing the panel checks again (r2Recheck), without errors
  await page.waitForTimeout(200);
  await page.click("#r2NotNow");
  await page.waitForTimeout(200);
  assert(!(await shown(page, "#liveStage .r1-stage")) && (await shown(page, ".sidebar")) && (await shown(page, "#prepList")), "Not now goes back to the Interview page, chrome back");
  assert(!errors.length, errors.join("\n"));
  await ctx.close();

  // Used up, extra interviews on sale: the refill date when the engine knows it, ONE "Buy more".
  ({ ctx, page, errors } = await open(browser, { w, h, theme, status: USED_UP }));
  const line = (await page.textContent(".r2-act-line")).trim();
  assert(/^You've used this period's live interviews\. They refill on .+2027\.$/.test(line) || /refill on/.test(line), `used up says when it refills (${line})`);
  assert((await page.textContent("#r2Plans")).trim() === "Buy more interviews" && (await primaries(page)) === 1, "one Buy more");
  assert(!/\$/.test(await cardText(page)), "no pack prices on this screen");
  await shot(page, "1b-used-up");
  await ctx.close();

  // Unreachable: Check again + Not now.
  ({ ctx, page, errors } = await open(browser, { w, h, theme, status: DOWN }));
  assert((await page.textContent("#r2Retry")).trim() === "Check again" && (await page.$("#r2NotNow")), "Check again and Not now");
  await shot(page, "1c-unreachable");
  await ctx.close();
}

async function flow(browser, w, h, theme) {
  const saved = { cam: { id: "cam-usb", label: "USB Webcam" }, mic: { id: "mic-head", label: "Headset Microphone" } };
  const { ctx, page, o, errors } = await open(browser, { w, h, theme, status: AVAILABLE, saved });
  await focusMode(page, "intro"); await oneLine(page, "intro"); await noSideScroll(page, "intro");
  assert((await page.textContent("#r2Left")).trim() === "2 live interviews left", "how many are left, one quiet line");
  assert((await page.textContent("#r2Begin")).trim() === "Get started" && (await page.$$("#liveStage .r1-card .r1-btn")).length === 1, "one action on the intro");
  const card = await rect(page, ".r1-card");
  assert(card.y >= 52 && card.b <= h && near(card.x + card.w / 2, w / 2, 2), `the intro card is whole and centred (${JSON.stringify(card)})`);
  await shot(page, "0-intro");

  // Device check: Round 1's card, the remembered devices open first.
  await page.click("#r2Begin");
  await page.waitForSelector('#liveStage #r1Stage[data-step="check"]'); await page.waitForTimeout(600);
  await focusMode(page, "device check"); await oneLine(page, "device check"); await noSideScroll(page, "device check");
  const names = await page.evaluate(() => [...document.querySelectorAll("#liveStage .r1-menu-v")].map((e) => e.textContent));
  assert(names[0] === "USB Webcam" && names[1] === "Headset Microphone", `the camera and microphone chosen before carry over (${JSON.stringify(names)})`);
  const strip = await rect(page, "#liveStage .r1-strip"), vid = await rect(page, "#liveStage .scr-video-wrap");
  assert(near(strip.b, vid.y, 1.5) && near(strip.w, vid.w, 2), "the strip with Join is attached to the video, as in Round 1");
  assert((await page.textContent("#liveBegin")).trim() === "Join interview", "Join interview at the end of the strip");
  await shot(page, "2-device-check");
  // Switch the microphone here: the call gets this one.
  await page.click('#liveStage .r1-menu[data-kind="mic"] [data-r1-menu]');
  await page.click('#liveStage [data-r1-opt="mic-built"]');
  await page.waitForTimeout(300);

  // The call (stand-in SDK, no network).
  await page.click("#liveBegin");
  await page.waitForSelector('#liveStage #r1Stage[data-step="call"]');
  await page.waitForFunction(() => document.querySelector("#cviWait") && document.querySelector("#cviWait").hidden, null, { timeout: 5000 });
  await page.waitForTimeout(500);
  await focusMode(page, "call"); await noSideScroll(page, "call");
  assert(JSON.stringify(o.started) === JSON.stringify({ prep_id: "p1", screen_id: "", skip_screen: true }), `the start call is unchanged (${JSON.stringify(o.started)})`);
  const opts = await page.evaluate(() => window.__dailyOpts), joined = await page.evaluate(() => window.__joined);
  assert(opts.videoSource === "cam-usb" && opts.audioSource === "mic-built" && opts.subscribeToTracksAutomatically === true, `the call uses the devices chosen on the check (${JSON.stringify(opts)})`);
  assert(joined.url === "https://example.daily.co/room" && joined.token === "tok", "the token is taken out of the join URL as before");
  const iv = await rect(page, "#cviIv"), me = await rect(page, "#cviMe"), bar = await rect(page, ".r2-ctrls"), pill = await rect(page, "#cviPill");
  assert(near(iv.w / iv.h, 16 / 9, 0.02) && iv.b <= h && iv.x >= 0 && iv.r <= w, `the interviewer tile is 16:9 and whole (${JSON.stringify(iv)})`);
  assert(iv.w >= Math.min(w - 48, (h - 52 - 100) * 16 / 9) * 0.9, `the interviewer tile uses the window (${Math.round(iv.w)} of ${w})`);
  assert(me.r <= iv.r && me.b <= iv.b && me.x > iv.x + iv.w / 2 && me.y > iv.y + iv.h / 2 && me.w < iv.w * 0.3, "the self view is a small tile in the bottom right corner");
  assert(near(bar.x + bar.w / 2, iv.x + iv.w / 2, 2) && bar.y >= iv.b && bar.y - iv.b <= 24 && bar.b <= h, "the controls are centred right under the video");
  assert(near(pill.x + pill.w / 2, w / 2, 2) && pill.b <= 52, "the Live interview pill is centred in the top bar");
  const ctrls = await page.evaluate(() => [...document.querySelectorAll(".r2-ctrls > .r2-ctrl, .r2-ctrls > .r2-ctrl-wrap > .r2-ctrl")].map((b) => b.id));
  assert(JSON.stringify(ctrls) === JSON.stringify(["cviMic", "cviCam", "cviGear", "cviEnd"]), `microphone, camera, settings, end (${JSON.stringify(ctrls)})`);
  assert(await page.evaluate(() => [...document.querySelectorAll(".r2-ctrl")].every((b) => b.getAttribute("aria-label") && !b.textContent.trim())), "icon buttons with labels for screen readers");
  // Who is speaking: a thin outline on that tile.
  await page.evaluate(() => window.__daily.speak("replica", true));
  assert(await page.evaluate(() => document.querySelector("#cviIv").classList.contains("is-speaking")), "the interviewer's tile is outlined while they speak");
  await shot(page, "3-in-call");
  await page.evaluate(() => { window.__daily.speak("replica", false); window.__daily.speak("user", true); });
  assert(await page.evaluate(() => document.querySelector("#cviMe").classList.contains("is-speaking") && !document.querySelector("#cviIv").classList.contains("is-speaking")), "and yours while you speak");
  await page.evaluate(() => window.__daily.speak("user", false));
  // Mute: the button turns red with the crossed microphone and says what a click does next.
  await page.click("#cviMic");
  assert(await page.evaluate(() => { const b = document.querySelector("#cviMic"); return b.classList.contains("is-off") && b.getAttribute("aria-pressed") === "true" && b.getAttribute("aria-label") === "Unmute"; }), "mute shows");
  await page.click("#cviMic");
  // Settings: the same menus, switched on the call and remembered.
  await page.click("#cviGear");
  await page.waitForSelector("#cviPop:not([hidden]) .r1-menu");
  await shot(page, "4-in-call-settings");
  await page.click('#cviPop .r1-menu[data-kind="cam"] [data-r1-menu]');
  await page.click('#cviPop [data-r1-opt="cam-built"]');
  await page.waitForTimeout(150);
  const setDev = await page.evaluate(() => window.__setDev), kept = await page.evaluate(() => JSON.parse(localStorage.getItem("sj.r1.cam")).id);
  assert(setDev && setDev[0].videoDeviceId === "cam-built" && kept === "cam-built", `the camera switches on the call and is remembered (${JSON.stringify(setDev)})`);
  await page.keyboard.press("Escape");
  assert(await page.evaluate(() => document.querySelector("#cviPop").hidden), "Escape closes settings");
  // Escape alone never ends the call: it asks.
  await page.keyboard.press("Escape");
  assert(await page.evaluate(() => !document.querySelector("#cviConfirm").hidden && !window.__left), "Escape asks, the call goes on");
  await page.keyboard.press("Escape");
  // The end button asks first, right above it.
  await page.click("#cviEnd");
  const conf = await rect(page, "#cviConfirm"), end = await rect(page, "#cviEnd");
  assert(conf.b <= end.y && conf.x >= 0 && conf.r <= w, "the question sits above the end button, on screen");
  assert(!(await page.evaluate(() => window.__left)), "asking does not end the call");
  await shot(page, "5-end-confirm");
  await page.click("#cviEndGo");
  await page.waitForSelector('#liveStage #r1Stage[data-step="done"]');
  await focusMode(page, "all done");
  assert(await page.evaluate(() => window.__left === true), "the call is left");
  assert((await page.textContent("#r2DoneSub")).trim() === "Scoring your interview. This takes a minute." && !(await page.$("#liveStage textarea, #liveStage input[type=text]")), "All done, scoring, no typing");
  await shot(page, "6-all-done");
  o.release();
  await page.waitForSelector("#prepLive.is-report .a2-title");
  assert(await shown(page, ".sidebar"), "the report is back in the app");
  assert(await page.evaluate(() => [...document.querySelectorAll(".a2-lab")].map((e) => e.textContent).join("|")) === "Fix these first|By skill", "the same report layout as Round 1");
  await noSideScroll(page, "report");
  await shot(page, "7-report");
  assert(!errors.length, errors.join("\n"));
  await ctx.close();
}

async function noCamera(browser, w, h, theme) {
  const { ctx, page, errors } = await open(browser, { w, h, theme, status: AVAILABLE, noCam: true });
  await page.click("#r2Begin");
  await page.waitForSelector('#liveStage #r1Stage[data-step="check"]'); await page.waitForTimeout(500);
  assert((await page.textContent("#liveStage .r1-dev-note")).trim() === "No camera found. You can still join with your voice.", "no camera is said out loud");
  await shot(page, "8-no-camera-check");
  await page.click("#liveBegin");
  await page.waitForSelector('#liveStage #r1Stage[data-step="call"]');
  await page.waitForFunction(() => document.querySelector("#cviWait").hidden, null, { timeout: 5000 });
  assert((await page.evaluate(() => window.__dailyOpts)).videoSource === false, "the call joins with the voice only");
  assert(!(await page.$("#cviCam")) && (await page.evaluate(() => document.querySelector("#cviMe").hidden)), "no dead camera control, no empty self view");
  await shot(page, "9-no-camera-call");
  assert(!errors.length, errors.join("\n"));
  await ctx.close();
}

(async () => {
  const browser = await chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"] });
  try {
    const sizes = SHOTS ? [[1440, 806, "light"], [1440, 806, "dark"], [2491, 1557, "light"], [2491, 1557, "dark"], [900, 806, "light"], [900, 806, "dark"]] : [[1440, 806, "light"], [900, 760, "dark"]];
    for (const [w, h, theme] of sizes) {
      TAG = `${w}x${h}-${theme}`;
      if (!ONLY || ONLY === "gates") await gates(browser, w, h, theme);
      if (!ONLY || ONLY === "flow") await flow(browser, w, h, theme);
      if (!ONLY || ONLY === "nocam") await noCamera(browser, w, h, theme);
    }
  } finally { await browser.close(); }
  console.log("ok");
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
