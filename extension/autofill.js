// Assisted apply, on a job APPLICATION form (Greenhouse / Lever / Ashby / Workday /
// Workable / SmartRecruiters / iCIMS), this offers to PRE-FILL the standard contact
// fields from your saved SponsorJobs profile. You review every field and click submit
// YOURSELF. It never clicks submit, never uploads a file on its own, and only touches
// empty fields. This is assisted apply, not a bot (CLAUDE.md §7).
//
// The fill ENGINE lives in autofill_core.js (shared with the desktop shell's in-app
// browser); this file is the extension carrier: the panel UI and the background-worker
// plumbing that fetches the payload and drafts long answers.

(function () {
  "use strict";

  // Assisted apply must NEVER run on LinkedIn, it's browse-by-hand only, no automation
  // of a logged-in LinkedIn account (CLAUDE.md §7). The read-only sponsor badges
  // (content.js) may still show; this fill panel bails out here entirely.
  if (/(^|\.)linkedin\.com$/i.test(location.hostname)) return;

  const TA = window.TailorAutofill;
  if (!TA) return;   // manifest loads autofill_core.js first; without it, do nothing

  // ------------------------------------------------------------------ the panel
  let panel = null, cachedFields = null, cachedResumes = [], cachedAppUrl = "http://127.0.0.1:57000/";

  function ask(msg) {
    return new Promise((resolve) => {
      try { chrome.runtime.sendMessage(msg, (r) => resolve(chrome.runtime.lastError ? { ok: false } : r)); }
      catch { resolve({ ok: false }); }
    });
  }

  function setNote(html, cls) {
    const n = panel.querySelector(".tailor-af-note");
    n.className = "tailor-af-note" + (cls ? " " + cls : "");
    n.innerHTML = html;
  }

  // HTML-escape a value before it lands in the panel's innerHTML.
  const escq = (s) => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  async function doFill() {
    const btn = panel.querySelector(".tailor-af-fill");
    btn.disabled = true;
    const r = await ask({ type: "autofill" });
    if (!r || r.ok === false) {
      setNote("Couldn't reach the SponsorJobs app, open it on your computer and try again.", "bad");
      btn.disabled = false; return;
    }
    cachedFields = r.fields || {};
    cachedResumes = Array.isArray(r.resumes) ? r.resumes : [];
    cachedAppUrl = r.app_url || cachedAppUrl;
    if (!r.loaded || !Object.keys(cachedFields).length) {
      setNote('No saved profile yet. Open SponsorJobs and build your CV once, then come back.', "warn");
      btn.disabled = false; return;
    }
    // Abort on a captcha / auth wall, hand it to the person, never evade (CLAUDE.md §7).
    const blocker = TA.pageBlocker ? TA.pageBlocker() : "";
    if (blocker) {
      setNote(blocker === "captcha"
        ? "This page has a captcha, so I've stopped, I won't try to get past it. Solve it, then click Autofill again."
        : "This page needs you to sign in first. Sign in, then click Autofill again and I'll take it from there.", "warn");
      btn.disabled = false; btn.textContent = "Autofill again"; return;
    }
    // The recipe table rides along with the payload (freshest copy from the app); the engine
    // falls back to its embedded copy without it. Order of play: this site's recipe, then the
    // label patterns. Nothing here clicks: continue is a hint, submit is outlined for you.
    if (r.recipes) TA.setRecipes(r.recipes);
    const recipe = TA.recipeFor();
    const plan = TA.planFill(cachedFields, { recipe });
    const nFields = TA.applyFill(plan);
    const nScreen = TA.fillScreening(r.screening);
    const nEeo = (r.eeo && r.eeo.decline) ? TA.fillEeo() : 0;
    const needResume = TA.hasResumeUpload();
    const filled = nFields + nScreen + nEeo;
    const total = plan.length + nScreen + nEeo + (needResume ? 1 : 0);   // Y in "filled X of Y"
    const submitHinted = TA.highlightSubmit(recipe);
    const nextPage = !!(recipe && recipe.multipage && TA.continueButton(recipe));
    armWatchers(recipe);

    const parts = [];
    if (nFields) parts.push(`<b>${nFields}</b> contact field${nFields === 1 ? "" : "s"}`);
    if (nScreen) parts.push(`<b>${nScreen}</b> screening answer${nScreen === 1 ? "" : "s"}`);
    if (nEeo) parts.push(`EEO set to &ldquo;decline&rdquo;`);
    let recipeNote = "";
    if (recipe) recipeNote += ` Recognised <b>${escq(recipe.name)}</b>.`;
    if (nextPage) recipeNote += " <b>Continue to the next page, then press Fill again.</b>";
    if (submitHinted) recipeNote += " The submit button is outlined in orange, that click is yours.";
    if (recipe && recipe.handoff) recipeNote += " This careers site hands the application to its own system: press Apply there, then fill again.";
    // Résumé/variant selector: which tailored CV to attach for THIS role. Contact fields are
    // shared across CVs, so this doesn't change the fill, it points the person at the right PDF.
    let resumeNote = "";
    if (needResume) {
      resumeNote = ' Browsers don’t let extensions upload files, so <b>attach your résumé</b> yourself.';
      if (cachedResumes.length) {
        resumeNote += ' Which CV? <select class="tailor-af-cv">' +
          cachedResumes.map(x => `<option>${escq((x.role || "CV") + (x.company ? " · " + x.company : ""))}</option>`).join("") +
          `</select> <a href="${escq(cachedAppUrl)}" target="_blank" rel="noopener">open SponsorJobs</a> to grab its PDF.`;
      } else {
        resumeNote += ` <a href="${escq(cachedAppUrl)}" target="_blank" rel="noopener">Open SponsorJobs</a> to grab the tailored PDF.`;
      }
    }
    if (!parts.length) {
      setNote("Nothing new to fill, these are already filled or this form asks things only you can answer." + resumeNote + recipeNote,
              "warn");
    } else {
      setNote(`Filled <b>${filled} of ${total}</b> fields I recognized: ` + parts.join(" · ") +
              ". <b>Review everything</b>, then submit it yourself. SponsorJobs never submits for you." + resumeNote + recipeNote, "ok");
    }
    btn.disabled = false;
    btn.textContent = "Autofill again";
  }

  // The background worker (owned with the manifest) only knows the prefs route, so the two
  // follow-ups ride on it as events the app routes: {type:"applied"} marks the job applied,
  // {type:"learn"} saves a correction to the profile. See profile_prefs_set in ui/app.py.
  const sendEvent = (event) => ask({ type: "prefs_set", prefs: { event } });

  // After a fill: (1) watch for the site's "application received" page, which only ever appears
  // after YOU submit, and tell the app so the job is marked applied; (2) watch the fields we
  // filled, and when you change one, offer to save the new value to your profile.
  let watchersArmed = false;
  function armWatchers(recipe) {
    TA.watchSuccess(async (info) => {
      const res = await sendEvent({ type: "applied", url: location.href, page_url: location.href,
                                    system: info && info.system });
      if (panel) {
        setNote(res && res.matched
          ? "The site confirmed your application. SponsorJobs marked it as applied."
          : "The site confirmed your application. No saved package matched this page, so mark it applied in SponsorJobs if you built one.", "ok");
      }
    }, { recipe });
    if (watchersArmed) return;
    watchersArmed = true;
    TA.watchCorrections((c) => offerToLearn(c));
  }

  const LEARN_LABELS = { first_name: "first name", last_name: "last name", name: "name", email: "email",
                         phone: "phone", address: "address", street: "street", city: "city", state: "state",
                         zip: "zip", linkedin: "LinkedIn URL", github: "GitHub URL", website: "website",
                         location: "location", country: "country" };
  function offerToLearn(c) {
    if (!panel || !c || !c.key || !LEARN_LABELS[c.key]) return;
    const old = panel.querySelector(".tailor-af-learn");
    if (old) old.remove();
    const row = document.createElement("div");
    row.className = "tailor-af-note tailor-af-learn";
    row.innerHTML = `You changed your ${escq(LEARN_LABELS[c.key])} to <b>${escq(c.value)}</b>. ` +
      `<button type="button" class="tailor-af-learn-yes">Save to my profile</button> ` +
      `<button type="button" class="tailor-af-learn-no">Not now</button>`;
    panel.appendChild(row);
    row.querySelector(".tailor-af-learn-no").addEventListener("click", () => row.remove());
    row.querySelector(".tailor-af-learn-yes").addEventListener("click", async () => {
      const fields = {}; fields[c.key] = c.value;
      const res = await sendEvent({ type: "learn", fields });
      row.innerHTML = res && res.ok
        ? `Saved. Your ${escq(LEARN_LABELS[c.key])} is now <b>${escq(c.value)}</b> in SponsorJobs.`
        : "Couldn't save it, is the SponsorJobs app running?";
      setTimeout(() => row.remove(), 5000);
    });
  }

  async function doDraft() {
    const btn = panel.querySelector(".tailor-af-draft");
    const areas = TA.answerableTextareas();
    if (!areas.length) return;
    const questions = areas.filter((a) => !a.cover).map((a) => a.question);
    const wantCover = areas.some((a) => a.cover);
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Drafting…";
    const r = await ask({ type: "draft", jd: TA.pageJobContext(), questions, cover_letter: wantCover });
    btn.disabled = false;
    btn.textContent = label;
    if (!r || r.ok === false) {
      setNote("Couldn't reach the SponsorJobs app to draft, open it on your computer and try again.", "bad");
      return;
    }
    if (!r.loaded) {
      setNote("No saved profile yet, build your CV in SponsorJobs once, then I can draft answers.", "warn");
      return;
    }
    let filled = 0;
    const qAreas = areas.filter((a) => !a.cover);
    (r.answers || []).forEach((a, i) => {
      const area = qAreas[i];
      if (area && a.answer && !String(area.el.value || "").trim()) {
        TA.setNativeValue(area.el, a.answer); TA.markFilled(area.el); filled++;
      }
    });
    if (r.cover_letter) {
      for (const a of areas) {
        if (a.cover && !String(a.el.value || "").trim()) {
          TA.setNativeValue(a.el, r.cover_letter); TA.markFilled(a.el); filled++;
        }
      }
    }
    if (!filled) {
      setNote("These long-answer boxes are already filled, or I couldn't match a question to answer.", "warn");
    } else {
      setNote(`Drafted <b>${filled}</b> answer${filled === 1 ? "" : "s"} from your profile. These are a ` +
              "<b>starting point</b>, read, edit, and make them yours before you submit.", "ok");
    }
    btn.textContent = "Draft again";
  }

  function updateDraftButton() {
    if (!panel) return;
    const btn = panel.querySelector(".tailor-af-draft");
    if (!btn) return;
    btn.style.display = TA.answerableTextareas().length ? "block" : "none";
  }

  function buildPanel() {
    if (panel) return;
    panel = document.createElement("div");
    panel.className = "tailor-af-panel";
    panel.innerHTML =
      '<div class="tailor-af-head"><span class="tailor-af-dot"></span>' +
      '<span class="tailor-af-title">SponsorJobs · Assisted apply</span>' +
      '<button class="tailor-af-x" title="Hide">×</button></div>' +
      '<button class="tailor-af-fill">Fill this application</button>' +
      '<button class="tailor-af-draft" style="display:none">Draft long answers</button>' +
      '<div class="tailor-af-note">Fills your contact details, links, and saved work-auth / ' +
      'sponsorship answers. You review and submit, SponsorJobs never submits for you.</div>';
    document.body.appendChild(panel);
    panel.querySelector(".tailor-af-fill").addEventListener("click", doFill);
    panel.querySelector(".tailor-af-draft").addEventListener("click", doDraft);
    panel.querySelector(".tailor-af-x").addEventListener("click", () => {
      panel.remove(); panel = null; dismissed = true;
    });
    updateDraftButton();
  }

  // Offer the panel only when the page really looks like an application form: at least
  // two recognizable contact fields present. Avoids popping up on plain job listings.
  let dismissed = false;
  function maybeOffer() {
    if (dismissed || panel) return;
    const keys = new Set();
    for (const el of TA.candidates()) {
      const k = TA.categorize(el, TA.signature(el));
      if (k) keys.add(k);
      if (keys.size >= 2 && (keys.has("email") || keys.has("first_name") || keys.has("name"))) {
        buildPanel();
        return;
      }
    }
  }

  let t = null;
  const debounced = () => { clearTimeout(t); t = setTimeout(() => { maybeOffer(); updateDraftButton(); }, 600); };
  new MutationObserver(debounced).observe(document.body, { childList: true, subtree: true });
  maybeOffer();
})();
