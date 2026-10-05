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
  setBounds: (rect) => ipcRenderer.send("pane:set-bounds", rect),
  getState: () => ipcRenderer.invoke("pane:get-state"),
  onState: (cb) => ipcRenderer.on("pane:state", (_e, state) => cb(state)),
  // Stripe checkout finished in the pane and returned to our app; the shell closed the pane and
  // hands us the result ('success'|'cancel') so the main window can confirm cleanly.
  onCheckoutReturn: (cb) => ipcRenderer.on("checkout:return", (_e, status) => cb(status)),
  // 'Continue with Google' finished in the pane; the shell closed it and hands us {token} (or
  // {error:true}) so the main window can adopt the account and carry on.
  onSigninReturn: (cb) => ipcRenderer.on("signin:return", (_e, data) => cb(data)),
  // Assisted apply in the drawer: open the application page and pre-fill it from
  // the saved profile. Resolves to the fill report; the submit click stays human.
  assistFill: (url) => ipcRenderer.invoke("assist:fill", url),
  // Fill the page currently open in the pane (after you signed in / navigated there).
  assistFillCurrent: () => ipcRenderer.invoke("assist:fill-current"),
  // Apply: fill, attach the résumé and submit in one go. The person's click on Apply is
  // the authorisation, so they never have to read a form back. Stops at a captcha or
  // login wall instead of trying to pass it, and never runs on LinkedIn.
  assistApply: (url, resumePath) =>
    ipcRenderer.invoke("assist:apply", { url, resumePath }),
  // AI autonomous apply: open the application page and AI-fill it for one saved application
  // (record id). The person's data never reaches the model; the submit click stays human.
  autoApplyRun: (url, recordId) => ipcRenderer.invoke("autoapply:run", url, recordId),
  // AI-fill whatever page is currently open in the pane, for one saved application.
  autoApplyFillCurrent: (recordId) => ipcRenderer.invoke("autoapply:fill-current", recordId),
});
