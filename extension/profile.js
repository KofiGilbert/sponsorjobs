// LinkedIn profile REVIEW, the content-script carrier. On the person's OWN profile page
// (linkedin.com/in/<slug>, detected by the owner-only edit controls) it reads what is on the
// page as rendered, sends that to the LOCAL SponsorJobs app through the extension's
// background worker (127.0.0.1:57000, nowhere else), and shows a small corner card with the
// score. The fixes, the drafted headline and About live in the app; the person applies them
// on LinkedIn by hand.
//
// READ-ONLY, by rule (CLAUDE.md §7): this file never scrolls, never clicks or focuses anything
// on the page, never expands a section, never edits the profile, and does nothing at all on
// anyone else's profile. The only thing it adds to the page is its own card. The extraction
// logic itself is in profile_core.js (pure, unit-tested); tests/test_extension_profile.py
// guards this file for the rules above.

(function () {
  "use strict";

  if (!/^(www\.)?linkedin\.com$/i.test(location.hostname)) return;
  if (!/^\/in\/[^/]+/.test(location.pathname)) return;
  const Core = window.TailorProfileCore;
  if (!Core || !chrome.runtime || !chrome.runtime.id) return;

  const APP_URL = "http://127.0.0.1:57000/";
  const ATTEMPTS_MS = [1500, 4000, 8000, 15000];   // LinkedIn renders the page in pieces

  function ask(msg) {
    return new Promise((resolve) => {
      try { chrome.runtime.sendMessage(msg, (r) => resolve(chrome.runtime.lastError ? { ok: false, error: "cant_reach_app" } : (r || { ok: false }))); }
      catch { resolve({ ok: false, error: "cant_reach_app" }); }
    });
  }

  // ------------------------------------------------------------------ the corner card
  let card = null;
  function el(tagName, css, textValue) {
    const n = document.createElement(tagName);
    if (css) n.style.cssText = css;
    if (textValue != null) n.textContent = textValue;
    return n;
  }
  function showCard(title, body, linkText) {
    if (card) card.hidden = true;
    card = el("div",
      "position:fixed;right:18px;bottom:18px;z-index:2147483000;width:280px;box-sizing:border-box;" +
      "padding:14px 16px;border-radius:12px;background:#111214;color:#e7e7ea;font:13px/1.45 -apple-system," +
      "BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.35);" +
      "border:1px solid rgba(255,255,255,.08)");
    const head = el("div", "display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px");
    head.appendChild(el("div", "font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#9a9aa3", "SponsorJobs"));
    const hide = el("button", "all:unset;cursor:pointer;color:#9a9aa3;font-size:14px;padding:0 2px", "×");
    hide.setAttribute("aria-label", "Hide");
    hide.addEventListener("click", () => { card.hidden = true; });   // our own button, not the page's
    head.appendChild(hide);
    card.appendChild(head);
    card.appendChild(el("div", "font-size:16px;font-weight:650;margin-bottom:4px", title));
    card.appendChild(el("div", "color:#b9b9c1", body));
    const a = el("a", "display:inline-block;margin-top:10px;color:#fff;text-decoration:underline;font-weight:600", linkText);
    a.href = APP_URL; a.target = "_blank"; a.rel = "noopener";
    card.appendChild(a);
    document.body.appendChild(card);
  }

  // ------------------------------------------------------------------ one read, sent once
  let done = false;
  async function attempt(i) {
    if (done) return;
    const root = document.documentElement;
    if (!Core.isOwnProfile(root, location)) {       // not ours: do nothing, show nothing
      if (i + 1 < ATTEMPTS_MS.length) setTimeout(() => attempt(i + 1), ATTEMPTS_MS[i + 1] - ATTEMPTS_MS[i]);
      return;
    }
    done = true;
    const profile = Core.extract(root, location);
    if (!profile.own) return;
    const r = await ask({ type: "prefs_set", prefs: { event: { type: "linkedin_review", profile } } });
    if (!r || r.ok === false || !r.has_review) {
      showCard("Profile review", "Open SponsorJobs to review your profile.", "Open SponsorJobs");
      return;
    }
    showCard("Profile score " + r.score + "/100", "Open SponsorJobs for the fixes and a drafted headline and About.",
             "Open SponsorJobs");
  }

  setTimeout(() => attempt(0), ATTEMPTS_MS[0]);
})();
