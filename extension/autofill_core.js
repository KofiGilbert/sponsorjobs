// The assisted-apply FILL ENGINE, shared by two carriers:
//   1. the browser extension (autofill.js adds the panel UI and talks to the app
//      through the background worker), loaded before autofill.js by the manifest;
//   2. the desktop shell (shell-electron injects this into its in-app browser pane
//      and calls TailorAutofill.fill(payload) directly).
// It only ever PRE-FILLS: empty fields, saved Yes/No screening answers, and the
// neutral EEO "decline" the person opted into. It never clicks submit, never
// uploads a file, never guesses free text (CLAUDE.md §7, assisted apply).
(function () {
  "use strict";
  if (window.TailorAutofill) return;   // one engine per page, whoever injects first

  // --- field categories -> where each maps in the /api/profile/autofill payload ----
  // Ordered most-specific first; the first category whose signal matches wins.
  const CATEGORIES = [
    { key: "email",      ac: ["email"],                 re: /e-?mail/ },
    { key: "phone",      ac: ["tel", "tel-national"],   re: /phone|mobile|\btel\b|telephone/ },
    { key: "first_name", ac: ["given-name"],            re: /first.?name|given.?name|\bfname\b|forename/ },
    { key: "last_name",  ac: ["family-name"],           re: /last.?name|family.?name|surname|\blname\b/ },
    { key: "linkedin",   ac: [],                        re: /linkedin/ },
    { key: "github",     ac: [],                        re: /github/ },
    { key: "website",    ac: ["url"],                   re: /portfolio|personal.?(web)?site|\bwebsite\b|\bblog\b|personal.?url/ },
    { key: "city",       ac: ["address-level2"],        re: /\bcity\b|\btown\b/ },
    { key: "state",      ac: ["address-level1"],        re: /\bstate\b|province|\bregion\b/ },
    { key: "zip",        ac: ["postal-code"],           re: /\bzip\b|postal|post.?code/ },
    { key: "street",     ac: ["street-address", "address-line1"], re: /street|address.?line|\baddress\b|\baddr\b/ },
    // full name last: only wins if a field says "name" but isn't first/last/user/company.
    { key: "name",       ac: ["name"],                  re: /full.?name|your.?name|legal.?name|\bname\b/ },
  ];
  // Fields we must never treat as a "name" (avoids filling username / company / referral).
  const NAME_VETO = /user|company|employer|referr|friend|contact.?name|emergency|search|coupon|reason/;

  const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ").trim();

  // Everything textual that hints what an input is for.
  function signature(el) {
    const bits = [el.name, el.id, el.placeholder, el.getAttribute("aria-label")];
    const al = el.getAttribute("aria-labelledby");
    if (al) al.split(/\s+/).forEach((id) => {
      const n = document.getElementById(id); if (n) bits.push(n.textContent);
    });
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) bits.push(lab.textContent);
    }
    let p = el.closest("label");                       // wrapping <label>
    if (p) bits.push(p.textContent);
    // Nearest preceding label-ish text in the same field group (Greenhouse/Ashby markup).
    const grp = el.closest("div,fieldset,li,section");
    if (grp) {
      const lab = grp.querySelector("label, .label, legend");
      if (lab) bits.push(lab.textContent);
    }
    return norm(bits.filter(Boolean).join(" · "));
  }

  function categorize(el, sig) {
    const ac = norm(el.getAttribute("autocomplete"));
    for (const c of CATEGORIES) {
      if (ac && c.ac.includes(ac)) {
        if (c.key === "name" && NAME_VETO.test(sig)) continue;
        return c.key;
      }
    }
    for (const c of CATEGORIES) {
      if (c.re.test(sig)) {
        if (c.key === "name" && NAME_VETO.test(sig)) continue;
        return c.key;
      }
    }
    return null;
  }

  const FILLABLE_TYPES = new Set(["text", "email", "tel", "url", "search", ""]);

  // Is this control one a PERSON could see and type into? Anything else is left alone,
  // and not only because filling it is pointless: an input with no size, parked off the
  // canvas, or drawn at opacity 0 is the classic anti-spam honeypot, and a value in it
  // gets the whole application binned as a bot submission. So the rule is strict on
  // purpose. (Visibility predicate adapted from AIHawk's snapshot code, MIT; see NOTICES.md.)
  function shown(el) {
    if (el.disabled || el.readOnly) return false;
    const rect = el.getBoundingClientRect() || {};
    if (!rect.width && !rect.height) return false;                        // no box at all
    if ((rect.right !== undefined && rect.right <= 0) ||
        (rect.bottom !== undefined && rect.bottom <= 0)) return false;    // parked off-canvas
    const gcs = (typeof window.getComputedStyle === "function") ? window.getComputedStyle(el) : null;
    if (gcs && (gcs.display === "none" || gcs.visibility === "hidden" ||
                gcs.opacity === "0")) return false;
    return true;
  }

  function candidates() {
    const out = [];
    document.querySelectorAll("input, textarea, select").forEach((el) => {
      if (!shown(el)) return;
      const tag = el.tagName.toLowerCase();
      if (tag === "input") {
        const t = (el.type || "text").toLowerCase();
        if (!FILLABLE_TYPES.has(t)) return;                // skip file/checkbox/radio/password/submit
      }
      out.push(el);
    });
    return out;
  }

  // React/Vue-safe value set: use the native setter, then fire input+change so the
  // framework's state updates (a plain el.value = x is ignored by controlled inputs).
  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
                : el.tagName === "SELECT"   ? HTMLSelectElement.prototype
                :                              HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function fillSelect(el, value) {
    const want = norm(value);
    for (const opt of el.options) {
      if (norm(opt.value) === want || norm(opt.textContent) === want ||
          norm(opt.textContent).startsWith(want)) {
        setNativeValue(el, opt.value);
        return true;
      }
    }
    return false;
  }

  function markFilled(el) {
    el.classList.add("tailor-filled");
    setTimeout(() => el.classList.remove("tailor-filled"), 2500);
  }

  // Plan the fill without applying it, used both to decide whether to offer the panel
  // and to run it. Returns [{el, key, value}].
  function planFill(fields) {
    const plan = [];
    for (const el of candidates()) {
      const sig = signature(el);
      if (!sig) continue;
      const key = categorize(el, sig);
      if (!key) continue;
      const value = fields[key];
      if (!value) continue;
      plan.push({ el, key, value });
    }
    return plan;
  }

  function applyFill(plan) {
    let filled = 0;
    for (const { el, value } of plan) {
      if (String(el.value || "").trim()) continue;      // never overwrite what you typed
      const ok = el.tagName === "SELECT" ? fillSelect(el, value)
                                         : (setNativeValue(el, value), true);
      if (ok) { markFilled(el); filled++; }
    }
    return filled;
  }

  // Which saved text answer (if any) a field's question label asks for. Pure + exported so
  // the matching is unit-testable without a DOM.
  function answerKeyForLabel(label) {
    const q = norm(label || "");
    if (!q) return null;
    for (const a of TEXT_ANSWERS) if (a.re.test(q)) return a.key;
    return null;
  }

  // Fill saved short text answers (years of experience, desired salary, earliest start)
  // into a matching EMPTY text/number field. Never a radio/select/textarea, never a value
  // you already typed, never a free-text essay we don't have an exact answer for.
  function fillTextAnswers(answers) {
    if (!answers) return 0;
    let filled = 0;
    for (const el of candidates()) {
      const tag = el.tagName.toLowerCase();
      if (tag !== "input") continue;
      const t = (el.type || "text").toLowerCase();
      if (!["text", "number", "tel"].includes(t)) continue;
      if (String(el.value || "").trim()) continue;          // never overwrite what you typed
      const key = answerKeyForLabel(rawLabel(el) || groupQuestion(el));
      if (!key) continue;
      const value = answers[key];
      if (!value) continue;
      setNativeValue(el, value);
      markFilled(el);
      filled++;
    }
    return filled;
  }

  // ---------------------------------------------------------- screening & EEO
  // Radio-group / dropdown questions with KNOWN answers you saved once (work
  // authorization, sponsorship need) or a neutral EEO "decline" choice. We only ever
  // pick Yes/No or a decline option, never a free-text guess, never a demographic value
  // you didn't choose.
  const SCREEN = [
    { key: "needs_sponsorship", re: /sponsor|require.*visa|visa.*sponsor|now or in the future.*(sponsor|visa)/ },
    { key: "work_authorized",   re: /authoriz(ed|ation) to work|legally.*(authoriz|work)|eligible to work|right to work|work authoriz/ },
    { key: "willing_to_relocate", re: /relocat|willing to move|open to relocat/ },
    { key: "over_18", re: /\b18\s*(years)?\s*(or older|and over|of age|\+)|at least 18|are you 18|18 years of age|over 18/ },
  ];

  // Short free-text answers saved once (years of experience, desired salary, earliest
  // start) matched to a text/number field by its question text. Same conservative rule as
  // everything else: only ever fill an EMPTY field, never overwrite what the person typed.
  const TEXT_ANSWERS = [
    { key: "years_experience", re: /years?\s*(of)?\s*(work|professional|relevant)?\s*experience|experience.*years|how many years/ },
    { key: "desired_salary",   re: /salary|compensation|desired pay|expected pay|pay expectation/ },
    { key: "earliest_start",   re: /start date|available to start|when can you (start|begin)|earliest start|notice period|availability/ },
    { key: "hear_about_us",    re: /how did you hear|where did you hear|hear about (us|this|the (job|role|position|opening))|referral source|how.*find (us|this|out about)/ },
  ];
  const EEO_TOPICS = /gender|\brace\b|ethnic|veteran|disab/;
  const DECLINE_RE = /decline|prefer not|not to (answer|identify|disclose)|don'?t (wish|want)|do not (wish|want)|i do not/;
  const YES_RE = /^\W*(yes|y|i am\b|i do\b|true)/;
  const NO_RE  = /^\W*(no|n|i am not|i do not|i'?m not|false)/;

  function radioOptionText(r) {
    if (r.id) { const l = document.querySelector(`label[for="${CSS.escape(r.id)}"]`); if (l) return norm(l.textContent); }
    const p = r.closest("label"); if (p) return norm(p.textContent);
    if (r.nextElementSibling) return norm(r.nextElementSibling.textContent);
    return norm(r.value);
  }

  // The QUESTION text for a control (legend / group label), distinct from an option's
  // own label. Used to route a group to a screening/EEO category.
  function groupQuestion(control) {
    const fs = control.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg) return norm(lg.textContent); }
    const grp = control.closest("div,li,section,fieldset");
    if (grp) {
      const lab = grp.querySelector("legend, label:not([for]), .label, [class*='question'], [class*='label']");
      if (lab && !lab.contains(control)) return norm(lab.textContent);
      const aria = grp.getAttribute("aria-label"); if (aria) return norm(aria);
    }
    return norm(control.getAttribute("aria-label") || "");
  }

  function radioGroups() {
    const groups = new Map();
    let n = 0;
    document.querySelectorAll('input[type="radio"]').forEach((r) => {
      if (r.disabled) return;
      const key = r.name || ("__anon" + (n++));
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(r);
    });
    return [...groups.values()];
  }

  // Set a radio via the native `checked` setter (bypassing React/Vue's value tracker,
  // which ignores a plain `r.checked = true`), then fire the events the framework binds.
  function setChecked(r) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "checked").set;
    setter.call(r, true);
    r.dispatchEvent(new Event("click", { bubbles: true }));
    r.dispatchEvent(new Event("input", { bubbles: true }));
    r.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function pickRadio(radios, matchFn) {
    if (radios.some((r) => r.checked)) return false;      // already answered, leave it
    for (const r of radios) {
      if (matchFn(radioOptionText(r))) {
        setChecked(r);
        markFilled(r.closest("label") || r.parentElement || r);
        return true;
      }
    }
    return false;
  }

  function pickSelectOption(sel, matchFn) {
    const cur = norm(sel.value);
    if (cur && !/^(select|choose|--|please)/.test(cur)) return false;   // already chosen
    for (const opt of sel.options) {
      if (!opt.value && !norm(opt.textContent)) continue;
      if (matchFn(norm(opt.textContent)) || matchFn(norm(opt.value))) {
        setNativeValue(sel, opt.value);
        markFilled(sel);
        return true;
      }
    }
    return false;
  }

  // Fill saved Yes/No screening answers into whichever radio group or select asks them.
  function fillScreening(screening) {
    if (!screening) return 0;
    let count = 0;
    const routeAndFill = (control, radiosOrNull) => {
      const q = groupQuestion(control) || signature(control);
      // A combined question ("authorized to work WITHOUT sponsorship?") matches both the
      // work-authorization AND sponsorship patterns, with OPPOSITE polarity, we can't
      // answer it safely, so leave it for the person rather than fill an inverted answer.
      if (SCREEN.filter((s) => s.re.test(q)).length > 1) return;
      for (const s of SCREEN) {
        if (!screening[s.key] || !s.re.test(q)) continue;
        const want = screening[s.key] === "Yes" ? YES_RE : NO_RE;
        const ok = radiosOrNull ? pickRadio(radiosOrNull, (t) => want.test(t))
                                : pickSelectOption(control, (t) => want.test(t));
        if (ok) count++;
        return;
      }
    };
    for (const radios of radioGroups()) routeAndFill(radios[0], radios);
    document.querySelectorAll("select").forEach((sel) => routeAndFill(sel, null));
    return count;
  }

  // Set EEO / demographic self-ID questions to the neutral "decline" option. Never
  // fills a gender/race/veteran/disability VALUE, only the decline choice you opted into.
  function fillEeo() {
    let count = 0;
    for (const radios of radioGroups()) {
      if (EEO_TOPICS.test(groupQuestion(radios[0])) && pickRadio(radios, (t) => DECLINE_RE.test(t))) count++;
    }
    document.querySelectorAll("select").forEach((sel) => {
      const q = groupQuestion(sel) || signature(sel);
      if (EEO_TOPICS.test(q) && pickSelectOption(sel, (t) => DECLINE_RE.test(t))) count++;
    });
    return count;
  }

  // A résumé/CV file input we can SEE but cannot fill (browsers block setting file inputs
  // from script). Surface it so the person attaches their tailored PDF themselves.
  function hasResumeUpload() {
    for (const f of document.querySelectorAll('input[type="file"]')) {
      if (f.disabled) continue;
      const sig = signature(f) + " " + norm(f.getAttribute("accept") || "");
      if (/resume|résumé|\bcv\b|cover.?letter|upload|attach|\.pdf|\.docx?/.test(sig)) return true;
      if (f.getBoundingClientRect().width > 0) return true;   // a visible file input on an apply form
    }
    return false;
  }

  // ---------------------------------------------------------- essay questions
  // The QUESTION prompt for a control, in its original case (for sending to the model
  // and displaying), unlike signature()/groupQuestion(), which lowercase for matching.
  function rawLabel(el) {
    const fs = el.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg) return lg.textContent.trim(); }
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return l.textContent.trim();
    }
    const p = el.closest("label"); if (p) return p.textContent.trim();
    const grp = el.closest("div,li,section,fieldset");
    if (grp) { const l = grp.querySelector("label, legend, .label, [class*='question']"); if (l) return l.textContent.trim(); }
    return (el.getAttribute("aria-label") || el.placeholder || "").trim();
  }

  function answerableTextareas() {
    const out = [];
    document.querySelectorAll("textarea").forEach((t) => {
      if (t.disabled || t.readOnly) return;
      if (t.getBoundingClientRect().width === 0) return;
      const q = rawLabel(t).replace(/\s+/g, " ").replace(/\*+$/, "").trim();
      if (!q || q.length < 5) return;                 // needs a real question to answer
      out.push({ el: t, question: q, cover: /cover.?letter/i.test(q) });
    });
    return out;
  }

  // Lightweight job context from the page (title + any description block) so drafted
  // answers reflect THIS role. The profile does the grounding; this just adds relevance.
  function pageJobContext() {
    const titleEl = document.querySelector(
      'h1, [class*="posting-headline"], [class*="job-title"], [data-testid*="title"]');
    const title = (titleEl ? titleEl.textContent : document.title || "").replace(/\s+/g, " ").trim();
    let desc = "";
    for (const s of ['[class*="job-description"]', '[class*="posting-description"]',
                     '[class*="opening"]', '[class*="description"]', "main", "#content"]) {
      const el = document.querySelector(s);
      if (el && el.textContent.trim().length > 200) { desc = el.textContent.replace(/\s+/g, " ").trim(); break; }
    }
    return (title + "\n\n" + desc).trim().slice(0, 4000);
  }

  // ---------------------------------------------------------- anti-bot wall
  // A captcha or auth/login wall means the site wants a HUMAN here. We NEVER try to get past it
  // (no evasion, no stealth, per CLAUDE.md §7): we detect it and drop the whole fill to the person.
  function pageBlocker() {
    for (const f of document.querySelectorAll("iframe")) {
      const src = norm(f.getAttribute && f.getAttribute("src"));
      if (/recaptcha|hcaptcha|\bcaptcha\b|turnstile|arkoselabs|funcaptcha|challenges\.cloudflare/.test(src))
        return "captcha";
    }
    const body = norm((document.body && document.body.textContent) || "").slice(0, 5000);
    if (/are you (a )?human|verify (that )?you.?re (a )?human|verify you are human|i'?m not a robot|complete the captcha|press (and|&) hold|unusual traffic|checking your browser before/.test(body))
      return "captcha";
    const pw = document.querySelector('input[type="password"]');
    if (pw && (!pw.getBoundingClientRect || pw.getBoundingClientRect().width > 0)) return "auth";
    if (/(please )?(sign in|log ?in) to (apply|continue|view this)|you must (sign in|log ?in|be logged in)|create an account to apply|sign in to your account to continue/.test(body))
      return "auth";
    return "";
  }

  // One-call orchestrator for the desktop shell: fill everything the payload supports and report
  // "filled X of Y" so the person knows exactly what to review. Never clicks submit.
  function fill(payload) {
    // Assisted apply must NEVER run on LinkedIn, browse-by-hand only (CLAUDE.md §7).
    if (/(^|\.)linkedin\.com$/i.test(location.hostname)) {
      return { ok: false, reason: "linkedin" };
    }
    // An AUTH wall means the form isn't reachable yet -- there is nothing to fill behind a
    // login, so stop and say so.
    const blocker = pageBlocker();
    if (blocker === "auth") {
      return { ok: false, reason: "blocked", blocker,
               message: "This page needs you to sign in first. Sign in, then click fill again "
                        + "and I'll take it from there." };
    }
    // A CAPTCHA is different, and treating it like an auth wall was a mistake. The captcha
    // gates SUBMISSION, not the form: typing a name into a text box is not getting past a
    // challenge, and the challenge still stands afterwards for the person to solve. Refusing
    // to fill left them re-typing 66 fields by hand on exactly the pages where the help is
    // worth most -- all of the effort, none of the evasion. So fill, and report the captcha
    // so the caller knows the submit click is unavoidably theirs (CLAUDE.md §7: we never
    // intercept, solve or bypass it).
    const fields = (payload && payload.fields) || {};
    const plan = planFill(fields);
    const filled = applyFill(plan);
    const screening = fillScreening(payload && payload.screening);
    const answers = fillTextAnswers(payload && payload.answers);
    const eeo = (payload && payload.eeo && payload.eeo.decline) ? fillEeo() : 0;
    const resume_upload = hasResumeUpload();
    // Y in "filled X of Y": the fields SponsorJobs RECOGNIZED it could help with (profile fields with a
    // saved value + the screening/text/EEO answers it set + a résumé upload it saw). X = what it set
    // now (a field you'd already typed counts toward Y but not X, so "X of Y" reads honestly).
    const total = plan.length + screening + answers + eeo + (resume_upload ? 1 : 0);
    return { ok: true, filled: filled + answers, total, screening, answers, eeo, resume_upload,
             // Present => the site requires a human to clear a challenge before submitting,
             // so the caller must hand the page back rather than click submit itself.
             captcha: blocker === "captcha" };
  }

  // ---------------------------------------------------------- AI-planned fill (autoapply)
  // The autonomous-applier upgrade over the keyword engine above: the desktop shell extracts the
  // form's STRUCTURE with extractForm(), the Python brain plans the fill (the person's real data
  // NEVER reaching the model), and applyOps() executes the returned plan here. Same conservative
  // rules: only EMPTY fields, highlight each, NEVER click submit, drop on the same captcha/auth
  // walls, and a file upload is handed back to the shell (script cannot set a file input; the shell
  // fulfils it via CDP). Reuses every helper above, so there is no second DOM engine.
  let _refN = 0;
  function fieldRef(el) {
    let r = el.getAttribute("data-tailor-ref");
    if (!r) { r = "t" + (_refN++); el.setAttribute("data-tailor-ref", r); }
    return r;
  }
  function fieldType(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === "textarea") return "textarea";
    if (tag === "select") return "select";
    const t = (el.type || "text").toLowerCase();
    if (t === "file") return "file";
    if (t === "checkbox") return "checkbox";
    return ["email", "tel", "url", "date", "number"].includes(t) ? t : "text";
  }

  // The interactable fields on the form, STRUCTURE ONLY (labels/types/options, never any value),
  // each with a stable ref the executor targets. This is exactly what goes to the brain.
  function extractForm() {
    const fields = [];
    document.querySelectorAll("input, textarea, select").forEach((el) => {
      if (el.disabled || el.readOnly) return;
      const raw = el.tagName.toLowerCase() === "input" ? (el.type || "text").toLowerCase() : el.tagName.toLowerCase();
      if (["radio", "hidden", "submit", "button", "reset", "password", "image"].includes(raw)) return;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0 && raw !== "file") return;   // truly hidden (files can be styled away)
      const label = rawLabel(el);
      if (!label && raw !== "file") return;                                  // need a question to answer
      const f = { ref: fieldRef(el), label: (label || "Résumé / CV").slice(0, 200),
                  type: fieldType(el), required: !!(el.required || /\*\s*$/.test(label || "")) };
      if (el.tagName.toLowerCase() === "select") {
        f.options = [...el.options].map((o) => (o.textContent || o.value).trim())
          .filter((t) => t && !/^(select|choose|--|please)/i.test(t)).slice(0, 40);
      }
      fields.push(f);
    });
    for (const radios of radioGroups()) {                                    // one field per radio group
      const q = groupQuestion(radios[0]);
      if (!q) continue;
      const ref = fieldRef(radios[0]);
      radios.forEach((r) => r.setAttribute("data-tailor-group", ref));
      fields.push({ ref, label: (rawLabel(radios[0]) || q).slice(0, 200), type: "radio",
                    options: radios.map((r) => radioOptionText(r)).filter(Boolean).slice(0, 40), required: false });
    }
    return fields;
  }

  const elByRef = (ref) => document.querySelector(`[data-tailor-ref="${CSS.escape(ref)}"]`);

  // Execute the brain's resolved ops. Returns {filled, uploads:[refs], skipped}. Upload ops are
  // returned for the shell to fulfil via CDP (a file input can't be set from script). Never submits,
  // never overwrites what the person already typed.
  function applyOps(ops) {
    let filled = 0, skipped = 0;
    const uploads = [];
    for (const op of (ops || [])) {
      const el = elByRef(op.ref);
      if (!el) { skipped++; continue; }
      if (op.op === "upload") { uploads.push(op.ref); continue; }
      const type = fieldType(el);
      const text = String(op.text == null ? "" : op.text);
      try {
        if (type === "radio") {
          const radios = [...document.querySelectorAll(`[data-tailor-group="${CSS.escape(op.ref)}"]`)];
          if (pickRadio(radios, (t) => norm(t) === norm(text) || norm(t).startsWith(norm(text)))) filled++; else skipped++;
        } else if (type === "select") {
          const cur = norm(el.value);
          if (cur && !/^(select|choose|--|please)/.test(cur)) { skipped++; continue; }
          if (fillSelect(el, text)) { markFilled(el); filled++; } else skipped++;
        } else if (type === "checkbox") {
          if (!el.checked && /^(yes|true|1|checked|on)$/i.test(text)) { setChecked(el); markFilled(el); filled++; } else skipped++;
        } else {
          if (String(el.value || "").trim()) { skipped++; continue; }        // never overwrite typed text
          setNativeValue(el, text); markFilled(el); filled++;
        }
      } catch (e) { skipped++; }
    }
    return { filled, uploads, skipped };
  }

  window.TailorAutofill = {
    fill, pageBlocker, planFill, applyFill, fillScreening, fillTextAnswers, answerKeyForLabel,
    fillEeo, hasResumeUpload, candidates, categorize, signature, setNativeValue, markFilled,
    rawLabel, answerableTextareas, pageJobContext,
    extractForm, applyOps, fieldRef,                                         // AI-planned autoapply
  };
})();
