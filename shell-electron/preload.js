/* Bridge between the SponsorJobs web UI and the Electron shell. The UI detects
 * window.tailorShell to know it is running inside the desktop shell (and shows
 * the in-app browser toolbar); without it, links fall back to /api/open's
 * system-browser behavior. */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("tailorShell", {
  openBrowser: (url) => ipcRenderer.send("pane:open", url),
  closeBrowser: () => ipcRenderer.send("pane:close"),
  navigate: (url) => ipcRenderer.send("pane:navigate", url),
  goBack: () => ipcRenderer.send("pane:back"),
  goForward: () => ipcRenderer.send("pane:forward"),
  reload: () => ipcRenderer.send("pane:reload"),
  // Hand the page in the pane to the person's own browser (Chrome, Safari...), for any site that
  // misbehaves in an embedded browser. Older shells lack this; the UI hides the button then.
  openExternal: () => ipcRenderer.send("pane:external"),
  // A newer installer is on the releases page: {version, url}. See checkForUpdate in main.js.
  onUpdateAvailable: (cb) => ipcRenderer.on("update:available", (_e, info) => cb(info)),
  openReleasePage: (url) => ipcRenderer.send("update:open", url),
  // Help and support: open the mail app to support@sponsorjobs.ai (main.js accepts nothing else).
  openSupportMail: (url) => ipcRenderer.send("support:mail", url),
  // Help and support: Gmail's compose screen in the person's own browser (main.js checks the URL).
  openSupportGmail: (url) => ipcRenderer.send("support:gmail", url),
  // The Terms of Use, in the person's own browser (the sign-in screen covers the in-app pane).
  openTerms: () => ipcRenderer.send("terms:open"),
  appVersion: () => ipcRenderer.invoke("app:version"),
  setBounds: (rect) => ipcRenderer.send("pane:set-bounds", rect),
  getState: () => ipcRenderer.invoke("pane:get-state"),
  // The page open in the Browser tab, read once ({url, title, html, text, canonical}); a LinkedIn
  // page answers only {url, linkedin: true} (2026-11-26, Autofill on a job the app does not know).
  readPage: () => ipcRenderer.invoke("pane:read-job"),
  onState: (cb) => ipcRenderer.on("pane:state", (_e, state) => cb(state)),
  // Stripe checkout finished in the pane and returned to our app; the shell closed the pane and
  // hands us the result ('success'|'cancel') so the main window can confirm cleanly.
  onCheckoutReturn: (cb) => ipcRenderer.on("checkout:return", (_e, status) => cb(status)),
  onReturned: (cb) => ipcRenderer.on("app:returned", () => cb()),
  // A sign-in that ended inside the pane: the shell closed it and hands us {error} so the main
  // window can say so. Sign-in itself finishes in the person's own browser.
  onSigninReturn: (cb) => ipcRenderer.on("signin:return", (_e, data) => cb(data)),
  // Learn from corrections: save the values the person changed after a fill to the profile.
  assistLearn: () => ipcRenderer.invoke("assist:learn"),
  // The fill engine saw the site's "application received" page after the person submitted;
  // the shell already told the app to mark the job applied, this is the UI's cue.
  onApplied: (cb) => ipcRenderer.on("assist:applied", (_e, info) => cb(info)),
  // (2026-11-26) The person left a filled application with no sign it was sent (left the site,
  // closed the Browser, started another job): {recordId, url, reason, clicked}. Asked ONCE.
  onAskSent: (cb) => ipcRenderer.on("assist:ask-sent", (_e, info) => cb(info)),
  onNeeds: (cb) => ipcRenderer.on("assist:needs", (_e, info) => cb(info)),
  // (2026-11-26) what the site did that the bar must say: the person left the application's job
  // ("left") or came back to it ("back"), the site opened its own window ("popup"), did not take
  // the Submit ("rejected", "server", "error") or the job closed ("closed")
  onSiteSaid: (cb) => ipcRenderer.on("assist:site-said", (_e, info) => cb(info)),
  // is the page open still the job a resume was tailored for? (job_identity.js)
  sameJob: (a, b) => ipcRenderer.invoke("pane:same-job", a, b),
  // open a job and wait for it to load, without filling (the country check reads it first)
  openAndWait: (url) => ipcRenderer.invoke("pane:open-wait", url),
  // the email-first step: "yes" (type the email) or "self"
  emailStep: (choice, recordId) => ipcRenderer.invoke("assist:email-step", choice, recordId),
  // The ONE apply path (Fill application, Review "Apply", Auto-apply "Open application"): open
  // the application in the pane and run the best fill (recipe, AI plan, resume upload).
  // opts = { url, recordId, resumePath, submit, here }. The site's submit is clicked ONLY when
  // the engine's policy says the site is on the verified AUTO allowlist (submit.policy
  // .apply_decision); everywhere else it stops and outlines the submit button for the person.
  // here: true fills the page already open in the pane (multi-page forms, after Next).
  // opts.finish (2026-11-26): then the agent finishes what the fill left (main.js assist:finish).
  assistApply: async (opts) => {
    const o = opts || {};
    const r = await ipcRenderer.invoke("assist:apply", o);
    return ipcRenderer.invoke("assist:finish", o, r);
  },
  // AI autonomous apply: open the application page and AI-fill it for one saved application
  // (record id). The person's data never reaches the model; the submit click stays human.
  autoApplyRun: (url, recordId) => ipcRenderer.invoke("autoapply:run", url, recordId),
  // The apply agent (docs/apply-agent.md): run one application, press Send, stop.
  agentRun: (opts) => ipcRenderer.invoke("agent:run", opts || {}),
  // (2026-11-26) Guided fill: Next (1) / Previous (-1) to each field that needs the person, "done".
  guideStep: (dir) => ipcRenderer.invoke("assist:guide-step", dir),
  agentSend: (recordId, opts) => ipcRenderer.invoke("agent:send", recordId, opts || null),
  agentPoll: () => ipcRenderer.invoke("agent:poll"),
  agentStop: () => ipcRenderer.invoke("agent:stop"),
  // The guided ask's Next: move on to the next field the person is asked to fill on the page.
  agentGuideNext: () => ipcRenderer.invoke("agent:guide-next"),
  // opts.quiet: Auto mode's background batch (out of sight, never asks).
  agentBatch: (jobs, opts) => ipcRenderer.invoke("agent:batch", jobs || [], opts || null),
  // Send all: every application left at Ready to send, one at a time; a captcha stops the list.
  agentSendAll: (jobs) => ipcRenderer.invoke("agent:send-all", jobs || []),
  agentContinue: (recordId) => ipcRenderer.invoke("agent:continue", recordId),
  agentBusy: () => ipcRenderer.invoke("agent:busy"),
  agentReady: (recordId) => ipcRenderer.invoke("agent:ready", recordId),
  // AI-fill whatever page is currently open in the pane, for one saved application.
  autoApplyFillCurrent: (recordId) => ipcRenderer.invoke("autoapply:fill-current", recordId),
  // Fill progress per page of a multi-page form: {record_id, page, filled, left:[labels], message,
  // wall?}. Sent after every fill, including the pages the shell fills by itself once the person
  // presses Next on the site (it never presses Next or Submit itself).
  onAutoApplyProgress: (cb) => ipcRenderer.on("autoapply:progress", (_e, info) => cb(info)),
  // Saved searches: "Open on other boards" hands a list of plain search URLs (LinkedIn, Indeed,
  // Glassdoor, Handshake, Google Jobs) to the person's OWN browser, one tab each. Nothing is
  // loaded in the pane and nothing is clicked or read on those sites; the person browses by hand.
  openLinksExternal: (urls) => ipcRenderer.send("links:external", urls),
  // A local desktop notification (the saved-search daily digest: "N new sponsor jobs"). Clicking
  // it brings the app window forward. No email, no network.
  notify: (title, body) => ipcRenderer.send("notify:show", { title, body }),
  // (2026-11-26) The Orb, the app's floating assistant (orb_view.js): what it shows and where,
  // and what the person did on it (its clicks, its bubble's buttons and answers, its menu).
  orbSet: (payload) => ipcRenderer.send("orb:set", payload || {}),
  onOrbEvent: (cb) => ipcRenderer.on("orb:event", (_e, data) => cb(data)),
  // Is the app window in front? (an OS notification is only for a window out of sight)
  isFocused: () => ipcRenderer.invoke("app:focused"),
});
