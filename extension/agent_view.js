// The apply agent's PAGE VIEW and in-page ACTIONS (docs/apply-agent.md, sections 3 and 6).
// Injected into the desktop shell's browser pane next to autofill_core.js, and into every
// cross-origin frame through shell-electron/frames.js. The model never sees the page itself:
// it reads the text this file builds, one numbered line per interactive element, and sends
// back "click 7" / "type 3 {{email}}" / "select 9 Yes". The numbers resolve to elements kept
// IN THE PAGE (window.__tailorAgentRefs), so nothing but text ever leaves the pane.
//
// Two hard lines, independent of whatever the model says:
//   1. No personal VALUE is ever in the view: a filled text box reads value="(filled)", a
//      password never shows its contents, a typed text is never echoed in a note.
//   2. act() never clicks Submit. The shell owns Submit (section 6); the in-page guard here
//      refuses the click outright and reports "submit-guard".
// Self-contained on purpose (a frame's isolated world may not have the engine yet); when
// window.TailorAutofill is present its label, value-setting and widget logic are reused so
// the view and the fill agree on what a field is called.
(function () {
  "use strict";
  if (window.TailorAgent) return;   // one agent per page, whoever injects first

  const T = () => window.TailorAutofill || null;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const norm = (s) => String(s || "").toLowerCase().replace(/\s+/g, " ").trim();
  const cleanText = (t) => String(t || "").replace(/\s+/g, " ").trim();
  const tagOf = (el) => String((el && el.tagName) || "").toUpperCase();
  const attrOf = (el, name) => (el && el.getAttribute ? el.getAttribute(name) : null);

  const MAX_CHARS = 12000;            // the whole view, so a long Workday page does not swamp the prompt
  const MAX_BLOCK_WORDS = 60;         // one block of ordinary page text
  const MAX_LABEL = 80;
  const INDEX_BASE = 1;
  // The shell owns Submit (section 6). Match the element's own text only, never a label near it,
  // so "Submit" on a nested span inside a Next button does not trip the guard by accident.
  const SUBMIT_RE = /^(submit( application)?|send( application)?|review and submit|complete application)$/i;
  const CAPTCHA_RE = /recaptcha|hcaptcha|\bcaptcha\b|turnstile|arkoselabs|funcaptcha|challenges\.cloudflare/i;
  const CLICK_ROLES = /^(button|link|option|menuitem|menuitemradio|menuitemcheckbox|tab|switch)$/;
  // The same custom-dropdown shapes the engine drives (its COMBO_SEL), so "select" lands on
  // Workday's listbox buttons and prompt boxes too, not only on role=combobox.
  const COMBO_SEL = "[role='combobox'], button[aria-haspopup='listbox'], [data-uxi-widget-type='selectinput']";
  const OPTION_SEL = "[role='option'], [id*='-option-'], [data-automation-id='promptOption'], [data-automation-id='promptLeafNode']";
  const PLACEHOLDER_RE = /^(select|choose|--|please|search|start typing|type to search|none selected|no selection)/;
  const TEXT_TYPES = new Set(["text", "email", "tel", "url", "search", "password", "number", "date", "datetime-local",
                              "month", "week", "time", "color", "range", ""]);
  const BUTTON_TYPES = new Set(["submit", "button", "reset", "image"]);
  const NO_TEXT_TAGS = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|SVG|SELECT|OPTION|TEXTAREA|LABEL|HEAD|TITLE|META|LINK)$/;

  // ------------------------------------------------------------------ deep DOM
  // The walk below crosses open shadow roots and same-origin frames in DOCUMENT ORDER, which
  // is what a reader expects of the numbering. A cross-origin frame cannot be entered from here;
  // the shell injects this same file there and offsets its indices (opts.indexBase).
  function viewOf(el) {
    try { return (el && el.ownerDocument && el.ownerDocument.defaultView) || window; } catch (e) { return window; }
  }
  function styleOf(el) {
    try { return viewOf(el).getComputedStyle(el); } catch (e) { return null; }
  }
  function rootOf(el) {
    try { const r = el && el.getRootNode && el.getRootNode(); if (r && r.querySelector) return r; } catch (e) { /* old DOM */ }
    return document;
  }
  function byId(el, id) {
    if (!id) return null;
    const r = rootOf(el);
    try { if (r.getElementById) { const n = r.getElementById(id); if (n) return n; } } catch (e) { /* fragment */ }
    try { return document.getElementById(id); } catch (e) { return null; }
  }
  function frameDoc(f) {
    let d = null;
    try { d = f.contentDocument; } catch (e) { d = null; }      // cross-origin: null or throws
    return d && d.documentElement ? d : null;
  }
  function captchaFrame(f) {
    return CAPTCHA_RE.test(String(attrOf(f, "src") || "") + " " + String(attrOf(f, "title") || ""));
  }
  // An ancestor test that crosses shadow boundaries (host -> shadow root -> child).
  function composedContains(outer, inner) {
    for (let n = inner; n; ) {
      if (n === outer) return true;
      n = n.parentNode || (n.host ? n.host : null);
      if (n && n.nodeType === 11 && n.host) n = n.host;         // a shadow root: continue at its host
    }
    return false;
  }

  // ------------------------------------------------------------------ visibility
  // "Hidden" for the view is what a person cannot see: display none, visibility hidden,
  // aria-hidden, the hidden attribute, an empty box, or parked off the left of the page. A
  // file input is the one exception, checked by the caller: upload buttons are built from a
  // file input at opacity 0 (or width 0) under a styled label, and the upload lands on the
  // input through CDP, so it stays listed however it is drawn.
  function selfHidden(el) {
    if (el.hidden || attrOf(el, "aria-hidden") === "true") return true;
    const cs = styleOf(el);
    if (cs && (cs.display === "none" || cs.visibility === "hidden")) return true;
    return false;
  }
  // A checkbox or radio is often the same trick: the real input sits at opacity 0 inside a
  // styled box the person sees (Workday's "I consent" box, 2026-10-28). It keeps its own
  // size, so it stays listed when it has a box, opacity or not; the click lands on the input.
  function boxShown(el, ignoreOpacity) {
    const r = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    if (!r || (!r.width && !r.height)) return false;
    const v = viewOf(el);
    if (r.right + Number(v.scrollX || 0) <= 0) return false;    // parked at left:-9999px
    const cs = styleOf(el);
    if (!ignoreOpacity && cs && cs.opacity === "0") return false;
    return true;
  }
  function inlineDisplay(el) {
    const cs = styleOf(el);
    const d = cs ? cs.display : "inline";
    return d === "inline" || d === "inline-block" || d === "inline-flex" || d === "inline-grid" || d === "contents";
  }

  // ------------------------------------------------------------------ what an element is
  function isFileInput(el) { return tagOf(el) === "INPUT" && String(el.type || "").toLowerCase() === "file"; }
  function isToggleInput(el) { return tagOf(el) === "INPUT" && /^(checkbox|radio)$/.test(String(el.type || "").toLowerCase()); }
  function isContentEditable(el) {
    const ce = attrOf(el, "contenteditable");
    return ce !== null && ce !== "false" && tagOf(el) !== "INPUT" && tagOf(el) !== "TEXTAREA";
  }
  function inCombobox(el) {
    for (let n = el.parentElement, i = 0; n && i < 3; n = n.parentElement, i++) {
      if (attrOf(n, "role") === "combobox") return true;
    }
    return false;
  }
  // The kind the view prints for an element, or null when it is not interactive. The role of
  // an ARIA widget wins over its tag (a div role=checkbox is a checkbox), except for a
  // role=combobox WRAPPER around a real input (ARIA 1.1), which the walk descends into instead.
  function kindOf(el) {
    const tag = tagOf(el);
    const role = norm(attrOf(el, "role"));
    if (tag === "INPUT") {
      const t = String(el.type || "text").toLowerCase();
      if (t === "hidden") return null;
      if (t === "file") return "file";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (BUTTON_TYPES.has(t)) return "button";
      if (role === "combobox" || el.hasAttribute("aria-autocomplete") || inCombobox(el)
          || attrOf(el, "data-uxi-widget-type") === "selectinput") return "combobox";
      if (TEXT_TYPES.has(t)) return "input";
      return "input";
    }
    if (tag === "TEXTAREA") return "textarea";
    if (tag === "SELECT") return "select";
    if (tag === "BUTTON") return (attrOf(el, "aria-haspopup") === "listbox") ? "combobox" : "button";
    if (role === "combobox") {
      // A wrapper holding the real text input: list the input, not the box around it.
      const inner = Array.from(el.querySelectorAll("input")).some((i) => String(i.type || "text") !== "hidden");
      return inner ? null : "combobox";
    }
    if (role === "listbox" && attrOf(el, "tabindex") !== null && !el.querySelector(OPTION_SEL)) return "combobox";
    if (role === "checkbox" || role === "switch") return "checkbox";
    if (role === "radio") return "radio";
    if (role === "textbox" || isContentEditable(el)) return "textarea";
    if (role === "button") return "button";
    if (role === "link") return "a";
    if (CLICK_ROLES.test(role)) return role;
    if (tag === "A") return (el.hasAttribute("href") || role) ? "a" : null;
    if (tag === "SUMMARY") return "button";
    return null;
  }

  // ------------------------------------------------------------------ labels
  // The engine's rawLabel when it is here (label[for], aria-labelledby, wrapping label, the
  // fieldset legend, the nearest label-ish text), else a compact copy of the same order.
  function labelledByText(el) {
    const al = attrOf(el, "aria-labelledby");
    if (!al) return "";
    return cleanText(al.split(/\s+/).map((id) => { const n = byId(el, id); return n ? n.textContent : ""; }).join(" "));
  }
  function labelFor(el) {
    if (!el || !el.id) return null;
    try { return rootOf(el).querySelector(`label[for="${CSS.escape(el.id)}"]`) || null; } catch (e) { return null; }
  }
  function cleanLabel(t) {
    return String(t || "").replace(/\s+/g, " ").replace(/\b(select one|required)\b/ig, "").replace(/[*✱]+/g, "").trim();
  }
  function ownLabel(el) {
    let out = "";
    const l = labelFor(el);
    if (l) out = cleanText(l.textContent);
    if (!out) out = labelledByText(el);
    if (!out) { const p = el.closest && el.closest("label"); if (p) out = cleanText(p.textContent); }
    return out;
  }
  function legendOf(el) {
    const fs = el.closest && el.closest("fieldset");
    const lg = fs && fs.querySelector("legend");
    return lg ? cleanText(lg.textContent) : "";
  }
  function fallbackLabel(el) {
    let out = ownLabel(el);
    if (!out) out = legendOf(el);
    if (!out) {
      const grp = el.closest && el.closest("div,li,section,fieldset");
      const l = grp && grp.querySelector("label, legend, .label, [class*='question']");
      if (l && !l.contains(el)) out = cleanText(l.textContent);
    }
    if (!out) out = cleanText(attrOf(el, "aria-label") || attrOf(el, "placeholder") || attrOf(el, "title") || "");
    return out;
  }
  function labelOf(el) {
    let t = "";
    const E = T();
    if (E && typeof E.rawLabel === "function") { try { t = E.rawLabel(el); } catch (e) { t = ""; } }
    if (!t) t = fallbackLabel(el);
    return cleanLabel(t).slice(0, MAX_LABEL);
  }
  // A radio or checkbox: its GROUP question (legend, radiogroup label) and its OWN option text.
  // The question is rarely a legend on real boards (Lever: a div.application-label above a list
  // of wrapped options), so after the legend and the group's ARIA name this climbs the box the
  // way the engine's groupQuestionText does: up to six ancestors, stopping at one that holds a
  // different control, taking the first label-ish text that is not an option's own label.
  const QUESTION_LABEL = "legend, label:not([for]), .label, .q, [class*='question'], [class*='label'], [class*='title'], [class*='prompt']";
  function groupLabel(el) {
    let t = legendOf(el);
    const g = el.closest && el.closest("[role='radiogroup'], [role='group']");
    if (!t && g) t = labelledByText(g) || cleanText(attrOf(g, "aria-label") || "");
    if (!t) {
      const type = tagOf(el) === "INPUT" ? String(el.type || "").toLowerCase() : "";
      const name = type && el.name ? el.name : null;
      let node = el.parentElement;
      for (let depth = 0; node && depth < 6 && !t; depth++, node = node.parentElement) {
        const tag = tagOf(node);
        if (tag === "FORM" || tag === "BODY" || tag === "HTML") break;
        const others = Array.from(node.querySelectorAll("input, select, textarea, [role='combobox'], [role='radiogroup'], [role='checkbox']"))
          .filter((o) => o !== el && !el.contains(o) && !o.contains(el) && String(o.type || "").toLowerCase() !== "hidden"
                      && !(name && String(o.type || "").toLowerCase() === type && o.name === name)
                      && !(g && g.contains(o)));
        if (others.length) break;                       // past this question's box
        for (const lab of Array.from(node.querySelectorAll(QUESTION_LABEL))) {
          if (lab.contains(el) || el.contains(lab) || lab.querySelector("input, select, textarea, [role='radio'], [role='checkbox']")) continue;
          const txt = cleanText(lab.textContent);
          if (txt) { t = txt; break; }
        }
        if (!t) { const aria = attrOf(node, "aria-label"); if (aria) t = cleanText(aria); }
      }
    }
    return cleanLabel(t).slice(0, MAX_LABEL);
  }
  function optionLabel(el) {
    if (tagOf(el) === "INPUT") {
      const t = ownLabel(el) || cleanText(attrOf(el, "aria-label") || "") || cleanText(el.value);
      return cleanLabel(t).slice(0, MAX_LABEL);
    }
    return cleanLabel(cleanText(el.textContent) || labelledByText(el) || attrOf(el, "aria-label") || "").slice(0, MAX_LABEL);
  }
  // The visible text of a button or link: its own text, else its aria-label / title / image alt.
  function textOf(el) {
    let t = "";
    if (tagOf(el) === "INPUT") t = cleanText(el.value || attrOf(el, "aria-label") || attrOf(el, "alt") || "");
    else t = cleanText(el.innerText != null ? el.innerText : el.textContent);
    if (!t) t = cleanText(attrOf(el, "aria-label") || attrOf(el, "title") || labelledByText(el));
    if (!t) { const img = el.querySelector && el.querySelector("img[alt], svg[aria-label]"); if (img) t = cleanText(attrOf(img, "alt") || attrOf(img, "aria-label")); }
    return t.slice(0, MAX_LABEL);
  }
  const q = (s) => '"' + String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';

  // ------------------------------------------------------------------ values
  // Text-like controls print "" or "(filled)" and nothing else. A chosen option's label is
  // fine (it is the site's word, not the person's). A combobox INPUT whose "value" is just what
  // was typed into it (an address typeahead) is treated as text: "(filled)".
  function selectValue(el) {
    const o = el.options && el.options[el.selectedIndex];
    if (!o) return "";
    if (!o.value && (el.selectedIndex === 0 || PLACEHOLDER_RE.test(norm(o.textContent)))) return "";
    return cleanText(o.textContent).slice(0, MAX_LABEL);
  }
  function comboValue(el) {
    const E = T();
    let v = "";
    if (E && typeof E.comboValue === "function") { try { v = E.comboValue(el) || ""; } catch (e) { v = ""; } }
    else if (tagOf(el) === "INPUT") v = norm(el.value);
    else {
      const t = norm(el.textContent || attrOf(el, "aria-label"));
      v = PLACEHOLDER_RE.test(t) ? "" : t;
    }
    if (!v) return "";
    if (tagOf(el) === "INPUT" && norm(el.value) === v) return "(filled)";
    return cleanText(v).slice(0, MAX_LABEL);
  }
  function isSiteSearch(el) {
    const E = T();
    if (E && typeof E.siteSearch === "function") { try { return !!E.siteSearch(el); } catch (e) { /* fall through */ } }
    if (!el.closest) return false;
    if (el.closest("[role='search'], search, header, nav")) return true;
    const form = el.form || el.closest("form");
    if (form && /search/i.test([form.getAttribute("action"), form.getAttribute("role"), form.id, String(form.className || "")].filter(Boolean).join(" "))) return true;
    return /search (for )?jobs|job search|keyword|search by (title|keyword)/i.test(
      [el.name, el.id, attrOf(el, "placeholder"), attrOf(el, "aria-label")].filter(Boolean).join(" "));
  }
  function isRequired(el) {
    return !!(el.required || attrOf(el, "aria-required") === "true");
  }
  function isDisabled(el) {
    return !!(el.disabled || attrOf(el, "aria-disabled") === "true");
  }

  // One line of the view for an interactive element (without the index).
  function describe(el, kind) {
    const flags = [];
    if (isRequired(el)) flags.push("required");
    if (isDisabled(el)) flags.push("disabled");
    const tail = flags.length ? " " + flags.join(" ") : "";
    if (kind === "button" || kind === "a" || CLICK_ROLES.test(kind)) {
      const text = textOf(el);
      const extra = (kind === "option" && attrOf(el, "aria-selected") === "true") ? " selected=true" : "";
      if (kind === "button" && tagOf(el) !== "BUTTON" && tagOf(el) !== "INPUT" && tagOf(el) !== "SUMMARY") return `<button role=button${extra}${tail}>${text}</button>`;
      return `<${kind}${extra}${tail}>${text}</${kind}>`;
    }
    if (kind === "input") {
      const type = String(el.type || "text").toLowerCase() || "text";
      const filled = String(el.value || "") !== "" ? "(filled)" : "";
      const search = isSiteSearch(el) ? " search=true" : "";
      return `<input type=${type} label=${q(labelOf(el))} value=${q(filled)}${search}${tail}>`;
    }
    if (kind === "textarea") {
      const ce = tagOf(el) !== "TEXTAREA";
      const text = ce ? cleanText(el.textContent) : String(el.value || "");
      return `<textarea${ce ? " contenteditable" : ""} label=${q(labelOf(el))} value=${q(text ? "(filled)" : "")}${tail}>`;
    }
    if (kind === "select") {
      const opts = Array.from(el.options || []);
      const first = opts.slice(0, 4).map((o) => cleanText(o.textContent).slice(0, 40)).join("|");
      return `<select label=${q(labelOf(el))} value=${q(selectValue(el))} options=${opts.length} first=${q(first)}${tail}>`;
    }
    if (kind === "combobox") {
      const expanded = attrOf(el, "aria-expanded") === "true";
      const search = isSiteSearch(el) ? " search=true" : "";
      return `<combobox label=${q(labelOf(el))} value=${q(comboValue(el))} expanded=${expanded}${search}${tail}>`;
    }
    if (kind === "checkbox") {
      const on = tagOf(el) === "INPUT" ? !!el.checked : attrOf(el, "aria-checked") === "true";
      const label = (tagOf(el) === "INPUT" ? ownLabel(el) : "") || labelOf(el);
      return `<checkbox label=${q(cleanLabel(label).slice(0, MAX_LABEL))} checked=${on}${tail}>`;
    }
    if (kind === "radio") {
      const on = tagOf(el) === "INPUT" ? !!el.checked : attrOf(el, "aria-checked") === "true";
      return `<radio label=${q(groupLabel(el))} option=${q(optionLabel(el))} checked=${on}${tail}>`;
    }
    if (kind === "file") {
      const chosen = el.files && el.files.length ? "chosen" : "none";
      const accept = attrOf(el, "accept") ? ` accept=${q(String(attrOf(el, "accept")).slice(0, 40))}` : "";
      return `<file label=${q(labelOf(el))} file=${chosen}${accept}${tail}>`;
    }
    return `<${kind}${tail}>${textOf(el)}</${kind}>`;
  }

  // ------------------------------------------------------------------ errors
  // Visible validation errors tell the model WHY Next did nothing. Three shapes cover the
  // ATSs: role=alert, aria-invalid controls with aria-describedby / aria-errormessage text, and
  // a block whose class or id says "error" and that has text.
  function errorBlock(el) {
    if (attrOf(el, "role") === "alert") return true;
    const idc = String(el.id || "") + " " + String(el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className || "");
    return /error|invalid/i.test(idc) && !/no-?error|error-?free/i.test(idc);
  }
  function describedErrors(el) {
    const ids = [attrOf(el, "aria-describedby"), attrOf(el, "aria-errormessage")].filter(Boolean).join(" ");
    const out = [];
    for (const id of ids.split(/\s+/).filter(Boolean)) {
      const n = byId(el, id);
      if (n && !selfHidden(n)) { const t = cleanText(n.textContent); if (t) out.push(n); }
    }
    return out;
  }
  function capWords(t, n) {
    const w = cleanText(t).split(" ");
    return w.length > n ? w.slice(0, n).join(" ") + " ..." : w.join(" ");
  }
  // Widget furniture is not page text: a dropdown's "Select..." placeholder, its "v" caret,
  // a lone glyph. The model has the control's line already.
  function noiseText(s) {
    if (!/[\p{L}\p{N}]{2}/u.test(s)) return true;
    return PLACEHOLDER_RE.test(norm(s)) && s.length <= 24;
  }
  // Ids named by any aria-labelledby on the page: their text is already a control's label.
  function labelIds(root, out, depth) {
    if (!root || depth > 8) return out;
    let els = [];
    try { els = Array.from(root.querySelectorAll("[aria-labelledby]")); } catch (e) { els = []; }
    for (const el of els) for (const id of String(attrOf(el, "aria-labelledby")).split(/\s+/)) if (id) out.add(id);
    let hosts = [];
    try { hosts = Array.from(root.querySelectorAll("*")).filter((e) => e.shadowRoot); } catch (e) { hosts = []; }
    for (const h of hosts) labelIds(h.shadowRoot, out, depth + 1);
    let frames = [];
    try { frames = Array.from(root.querySelectorAll("iframe, frame")); } catch (e) { frames = []; }
    for (const f of frames) { const d = frameDoc(f); if (d) labelIds(d, out, depth + 1); }
    return out;
  }

  // ------------------------------------------------------------------ the view
  let _seen = new WeakSet();          // elements listed in the previous view, for the `*` mark
  let _everViewed = false;
  let _base = INDEX_BASE;             // the index base of the last view, so element() can undo it
  window.__tailorAgentRefs = window.__tailorAgentRefs || [];

  function scrollInfo() {
    const d = document.documentElement || document.body;
    const h = Math.max((d && d.scrollHeight) || 0, (document.body && document.body.scrollHeight) || 0);
    const vh = window.innerHeight || 1;
    const y = window.scrollY || window.pageYOffset || 0;
    const below = Math.max(0, (h - vh - y) / vh), above = Math.max(0, y / vh);
    const bits = [];
    if (above > 0.05) bits.push(above.toFixed(1) + " pages above");
    if (below > 0.05) bits.push(below.toFixed(1) + " pages below");
    return bits.length ? " (" + bits.join(", ") + ")" : "";
  }

  function view(opts) {
    opts = opts || {};
    const base = Number.isFinite(Number(opts.indexBase)) ? Number(opts.indexBase) : INDEX_BASE;
    const refs = [];
    const lines = [];                   // {kind:"el"|"text", s, el?}
    const errorsDone = new Set();
    const seenNow = new WeakSet();
    const named = labelIds(document, new Set(), 0);
    let words = [];                     // the text block being gathered
    let lastText = "";

    const flush = () => {
      if (!words.length) return;
      const s = capWords(words.join(" "), MAX_BLOCK_WORDS);
      words = [];
      if (s && s !== lastText && !noiseText(s)) { lines.push({ kind: "text", s }); lastText = s; }
    };
    const pushLine = (s) => { flush(); if (s !== lastText) { lines.push({ kind: "text", s }); lastText = s; } };
    const pushError = (node, text) => {
      if (errorsDone.has(node)) return;
      errorsDone.add(node);
      const t = capWords(text, 40);
      if (t) pushLine("Error: " + q(t));
    };
    const pushEl = (el, kind) => {
      flush();
      const idx = base + refs.length;
      refs.push(el);
      seenNow.add(el);
      const star = _everViewed && !_seen.has(el) ? "*" : "";
      lines.push({ kind: "el", s: `${star}[${idx}]${describe(el, kind)}`, el });
      if (attrOf(el, "aria-invalid") === "true") {
        for (const n of describedErrors(el)) pushError(n, n.textContent);
      }
    };

    const walk = (node, depth, quiet) => {
      if (!node || depth > 60) return;
      if (node.nodeType === 3) {                        // a text node
        if (!quiet) { const t = cleanText(node.nodeValue); if (t) words.push(t); }
        return;
      }
      if (node.nodeType === 11) {                       // a shadow root / document fragment
        for (const c of Array.from(node.childNodes)) walk(c, depth + 1, quiet);
        return;
      }
      if (node.nodeType === 9) { walk(node.body || node.documentElement, depth + 1, quiet); return; }
      if (node.nodeType !== 1) return;
      const el = node;
      const tag = tagOf(el);
      if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|HEAD|TITLE|META|LINK)$/.test(tag)) return;
      if (tag === "IFRAME" || tag === "FRAME") {
        if (captchaFrame(el) || selfHidden(el)) return;       // never enter a captcha widget
        const d = frameDoc(el);
        if (d) { flush(); walk(d, depth + 1, quiet); flush(); }
        return;
      }
      const file = isFileInput(el);
      if (!file && selfHidden(el)) return;
      const kind = kindOf(el);
      if (kind) {
        if (file || boxShown(el, isToggleInput(el))) { pushEl(el, kind); return; }
        if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || tag === "BUTTON") return;
        // An empty-box role element (a wrapper that only sizes its children): walk on.
      }
      if (tag === "LABEL" || tag === "SELECT" || tag === "OPTION" || tag === "TEXTAREA" || tag === "SVG"
          || (el.id && named.has(el.id))) {
        // A label's text (or an aria-labelledby target's) is already on its control's line.
        for (const c of Array.from(el.childNodes)) if (c.nodeType === 1) walk(c, depth + 1, true);
        return;
      }
      if (/^H[1-4]$/.test(tag) || tag === "LEGEND") {
        const t = cleanText(el.innerText != null ? el.innerText : el.textContent);
        if (t) pushLine(capWords(t, MAX_BLOCK_WORDS));
        for (const c of Array.from(el.childNodes)) if (c.nodeType === 1) walk(c, depth + 1, true);
        return;
      }
      let q2 = quiet;
      if (!quiet && errorBlock(el) && !errorsDone.has(el)) {
        const t = cleanText(el.innerText != null ? el.innerText : el.textContent);
        if (t && t.length < 600) { pushError(el, t); q2 = true; }
      }
      const block = !inlineDisplay(el);
      if (block) flush();
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1, q2);
      for (const c of Array.from(el.childNodes)) walk(c, depth + 1, q2);
      if (block) flush();
    };

    walk(document, 0, false);
    flush();

    // Assemble under the cap. Element lines are what the model acts on, so the tail says
    // exactly how many of THOSE were cut (text after the cut is just dropped).
    let url = "";
    try { url = String(location.href || ""); } catch (e) { url = ""; }
    const head = `URL: ${url}\nTitle: ${cleanText(document.title).slice(0, 120)}\n[Start of page]\n`;
    const foot = `[End of page]${scrollInfo()}`;
    const out = [head];
    let size = head.length + foot.length + 40;
    let shownEls = 0, truncated = false;
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      if (size + ln.s.length + 1 > MAX_CHARS) { truncated = true; break; }
      out.push(ln.s + "\n");
      size += ln.s.length + 1;
      if (ln.kind === "el") shownEls++;
    }
    if (truncated) out.push(`(${refs.length - shownEls} more elements below)\n`);
    out.push(foot);

    window.__tailorAgentRefs = refs;
    _seen = seenNow;
    _everViewed = true;
    _base = base;
    return { text: out.join(""), count: refs.length, truncated, indexBase: base };
  }

  function element(index) {
    const refs = window.__tailorAgentRefs || [];
    const i = Number(index) - _base;
    return (Number.isInteger(i) && i >= 0 && i < refs.length) ? refs[i] : null;
  }

  // ------------------------------------------------------------------ actions
  function fire(el, type, init) {
    const v = viewOf(el);
    const C = /^(mouse|click|pointer)/.test(type) ? (v.MouseEvent || MouseEvent)
            : /^key/.test(type) ? (v.KeyboardEvent || KeyboardEvent) : (v.Event || Event);
    try {
      el.dispatchEvent(new C(type, Object.assign({ bubbles: true, cancelable: true, composed: true }, init || {})));
    } catch (e) { /* a detached node */ }
  }
  function centre(el) {
    const r = el.getBoundingClientRect();
    const v = viewOf(el);
    const x = Math.min(Math.max(r.left + r.width / 2, 0), (v.innerWidth || 1) - 1);
    const y = Math.min(Math.max(r.top + r.height / 2, 0), (v.innerHeight || 1) - 1);
    return { x, y };
  }
  function controlOfLabel(lab) {
    if (lab.control) return lab.control;
    const id = attrOf(lab, "for");
    if (id) { const n = byId(lab, id); if (n) return n; }
    return lab.querySelector("input, select, textarea, button, [role='checkbox'], [role='radio']");
  }
  function ownText(el) {
    return cleanText(tagOf(el) === "INPUT" ? (el.value || attrOf(el, "aria-label") || "")
                                           : (el.innerText != null ? el.innerText : el.textContent))
        || cleanText(attrOf(el, "aria-label") || "");
  }
  function submitLike(el) {
    return SUBMIT_RE.test(ownText(el));
  }
  // Is the element at its centre point the element, one of its parts, its container, or the
  // label/control it belongs to? Otherwise an overlay (a cookie banner, a modal) covers it.
  function covered(el) {
    const { x, y } = centre(el);
    let hit = null;
    try {
      const r = rootOf(el);
      hit = (r.elementFromPoint ? r.elementFromPoint(x, y) : null) || viewOf(el).document.elementFromPoint(x, y);
    } catch (e) { hit = null; }
    if (!hit) return false;                       // off screen: not a cover, the click still lands
    if (hit === el || composedContains(el, hit) || composedContains(hit, el)) return false;
    const lab = hit.closest && hit.closest("label");
    if (lab && (controlOfLabel(lab) === el)) return false;
    const myLab = labelFor(el) || (el.closest && el.closest("label"));
    if (myLab && composedContains(myLab, hit)) return false;
    return true;
  }
  // A cheap fingerprint to answer "did the page change": the URL, how many controls are
  // listed, and how much text there is. The shell's settle() is the real judge; this is the
  // hint that stops an action queue early.
  function fingerprint() {
    let url = "";
    try { url = String(location.href || ""); } catch (e) { url = ""; }
    let n = 0;
    try { n = document.querySelectorAll("input, select, textarea, button, a, [role]").length; } catch (e) { n = 0; }
    const t = (document.body && document.body.textContent) || "";
    return url + "|" + n + "|" + Math.round(t.length / 20);
  }
  function setValue(el, value) {
    const E = T();
    if (E && typeof E.setNativeValue === "function") { try { E.setNativeValue(el, value); return; } catch (e) { /* fall through */ } }
    const v = viewOf(el);
    const proto = tagOf(el) === "TEXTAREA" ? v.HTMLTextAreaElement.prototype
                : tagOf(el) === "SELECT"   ? v.HTMLSelectElement.prototype
                :                            v.HTMLInputElement.prototype;
    const d = Object.getOwnPropertyDescriptor(proto, "value");
    if (d && d.set) d.set.call(el, value); else el.value = value;
    fire(el, "input"); fire(el, "change");
  }
  function visibleOptions() {
    const out = [];
    const roots = [document];
    for (const h of Array.from(document.querySelectorAll("*")).filter((e) => e.shadowRoot)) roots.push(h.shadowRoot);
    for (const r of roots) for (const o of Array.from(r.querySelectorAll(OPTION_SEL))) {
      if (!selfHidden(o) && boxShown(o) && attrOf(o, "aria-disabled") !== "true" && cleanText(o.textContent)) out.push(o);
    }
    return out.filter((o) => !out.some((p) => p !== o && o.contains(p)));   // innermost
  }
  const optionList = (opts) => opts.slice(0, 12).map((o) => cleanText(o.textContent).slice(0, 40)).join(", ");

  async function doClick(el) {
    if (tagOf(el) === "LABEL") { const c = controlOfLabel(el); if (c) el = c; }
    if (tagOf(el) === "SELECT") return { ok: false, note: "use select", changed: false };
    if (isFileInput(el)) return { ok: false, note: "use upload", changed: false };
    if (submitLike(el)) return { ok: false, note: "submit-guard", changed: false };
    if (isDisabled(el)) return { ok: false, note: "disabled", changed: false };
    const before = fingerprint();
    try { if (el.scrollIntoView) el.scrollIntoView({ block: "center", inline: "nearest" }); } catch (e) { /* fine */ }
    await sleep(40);
    const cov = covered(el);
    const { x, y } = centre(el);
    const init = { button: 0, buttons: 1, clientX: x, clientY: y };
    try { if (el.focus) el.focus({ preventScroll: true }); } catch (e) { /* fine */ }
    fire(el, "pointerdown", init); fire(el, "mousedown", init);
    fire(el, "pointerup", Object.assign({}, init, { buttons: 0 })); fire(el, "mouseup", Object.assign({}, init, { buttons: 0 }));
    if (typeof el.click === "function") el.click(); else fire(el, "click", init);
    await sleep(200);
    return { ok: true, note: cov ? "covered" : "", changed: fingerprint() !== before };
  }

  async function doType(el, text) {
    if (tagOf(el) === "LABEL") { const c = controlOfLabel(el); if (c) el = c; }
    text = String(text == null ? "" : text);
    const tag = tagOf(el);
    const type = tag === "INPUT" ? String(el.type || "text").toLowerCase() : "";
    if (tag === "SELECT") return { ok: false, note: "use select", changed: false };
    if (type === "file") return { ok: false, note: "use upload", changed: false };
    if (type === "checkbox" || type === "radio" || /^(checkbox|radio|switch)$/.test(norm(attrOf(el, "role")))) return { ok: false, note: "use click", changed: false };
    if (isDisabled(el) || el.readOnly) return { ok: false, note: "disabled", changed: false };
    const before = fingerprint();
    try { if (el.scrollIntoView) el.scrollIntoView({ block: "center", inline: "nearest" }); } catch (e) { /* fine */ }
    try { if (el.focus) el.focus({ preventScroll: true }); } catch (e) { /* fine */ }
    if (tag !== "INPUT" && tag !== "TEXTAREA") {
      // contenteditable / role=textbox: no value property, the text IS the content.
      el.textContent = text;
      fire(el, "input");
      await sleep(60);
      const got = cleanText(el.textContent) === cleanText(text);
      return { ok: true, note: got ? "" : "value differs", changed: fingerprint() !== before };
    }
    // Every input type goes through the native setter plus input/change (date, time, color,
    // range and number take their value whole; a keystroke stream would be rejected).
    setValue(el, text);
    await sleep(60);
    const got = String(el.value) === text;
    return { ok: true, note: got ? "" : "value differs", changed: fingerprint() !== before };
  }

  async function doSelect(el, option) {
    if (tagOf(el) === "LABEL") { const c = controlOfLabel(el); if (c) el = c; }
    const want = String(option == null ? "" : option);
    const w = norm(want);
    const before = fingerprint();
    if (tagOf(el) === "SELECT") {
      const opts = Array.from(el.options || []);
      let hit = opts.find((o) => norm(o.textContent) === w || norm(o.value) === w)
             || opts.find((o) => w && norm(o.textContent).startsWith(w))
             || opts.find((o) => w.length >= 3 && norm(o.textContent).includes(w));
      if (!hit) return { ok: false, note: "no such option; options: " + optionList(opts), changed: false };
      setValue(el, hit.value);
      await sleep(40);
      const ok = el.value === hit.value;
      return { ok, note: ok ? "" : "did not stick", changed: fingerprint() !== before };
    }
    const type = tagOf(el) === "INPUT" ? String(el.type || "").toLowerCase() : "";
    if (type === "checkbox" || type === "radio" || /^(checkbox|radio|switch)$/.test(norm(attrOf(el, "role")))) {
      return { ok: false, note: "use click", changed: false };
    }
    const E = T();
    if (E && typeof E.pickCombo === "function" && typeof E.valueScorer === "function") {
      let r = null;
      try { r = await E.pickCombo(el, { text: want, score: E.valueScorer(want) }); } catch (e) { r = null; }
      if (r && r.ok) return { ok: true, note: "", changed: fingerprint() !== before };
      // "answered" means the engine will not touch a widget that already has a value; the
      // model asked for a change, so fall through to the plain path.
    }
    // The plain path: open it, wait for the menu, click the matching option.
    try { if (el.focus) el.focus({ preventScroll: true }); } catch (e) { /* fine */ }
    if (attrOf(el, "aria-expanded") !== "true") {
      fire(el, "pointerdown", { button: 0 }); fire(el, "mousedown", { button: 0 });
      fire(el, "pointerup", { button: 0 }); fire(el, "mouseup", { button: 0 });
      if (typeof el.click === "function") el.click(); else fire(el, "click", { button: 0 });
    }
    await sleep(300);
    let opts = visibleOptions();
    if (!opts.length && tagOf(el) === "INPUT") {
      // A type-to-search box shows options only after typing. Type the option text (it is
      // the site's option, not the person's data), then look again.
      setValue(el, want);
      await sleep(400);
      opts = visibleOptions();
    }
    const hit = opts.find((o) => norm(o.textContent) === w)
             || opts.find((o) => w && norm(o.textContent).startsWith(w))
             || opts.find((o) => w.length >= 3 && norm(o.textContent).includes(w));
    if (!hit) {
      fire(el, "keydown", { key: "Escape", code: "Escape", keyCode: 27, which: 27 });
      return { ok: false, note: opts.length ? "no such option; options: " + optionList(opts) : "no options shown", changed: false };
    }
    try { if (hit.scrollIntoView) hit.scrollIntoView({ block: "nearest" }); } catch (e) { /* fine */ }
    fire(hit, "mouseover"); fire(hit, "mousemove");
    fire(hit, "pointerdown", { button: 0 }); fire(hit, "mousedown", { button: 0 });
    fire(hit, "pointerup", { button: 0 }); fire(hit, "mouseup", { button: 0 });
    if (typeof hit.click === "function") hit.click(); else fire(hit, "click", { button: 0 });
    await sleep(150);
    return { ok: true, note: "", changed: fingerprint() !== before };
  }

  function scrollableAncestor(el) {
    for (let n = el; n && n !== document.body && n !== document.documentElement; n = n.parentElement || (n.parentNode && n.parentNode.host)) {
      const cs = styleOf(n);
      if (cs && /(auto|scroll)/.test(cs.overflowY) && n.scrollHeight > n.clientHeight + 4) return n;
    }
    return null;
  }
  async function doScroll(direction) {
    const sign = /^(up|top)$/i.test(String(direction || "")) ? -1 : 1;
    const step = Math.round((window.innerHeight || 800) * 0.8) * sign;
    const active = document.activeElement;
    const box = active && active !== document.body ? scrollableAncestor(active) : null;
    const y0 = box ? box.scrollTop : (window.scrollY || 0);
    if (box) box.scrollBy({ top: step, behavior: "auto" }); else window.scrollBy({ top: step, behavior: "auto" });
    await sleep(120);
    const y1 = box ? box.scrollTop : (window.scrollY || 0);
    return { ok: true, note: y0 === y1 ? "already at the " + (sign < 0 ? "top" : "bottom") : "", changed: false };
  }

  async function act(action) {
    action = action || {};
    const op = String(action.op || "").toLowerCase();
    if (op === "scroll") return doScroll(action.direction || action.text || "down");
    if (op === "wait") {
      const before = fingerprint();
      const s = Math.min(5, Math.max(0, Number(action.seconds != null ? action.seconds : action.text) || 1));
      await sleep(s * 1000);
      return { ok: true, note: "", changed: fingerprint() !== before };
    }
    const el = element(action.index);
    if (!el) return { ok: false, note: "no such index", changed: false };
    if (!el.isConnected) return { ok: false, note: "element gone", changed: false };
    try {
      if (op === "click") return await doClick(el);
      if (op === "type") return await doType(el, action.text);
      if (op === "select") return await doSelect(el, action.option != null ? action.option : action.text);
      if (op === "upload") return { ok: false, note: "upload is done by the shell", changed: false };
    } catch (e) {
      return { ok: false, note: "failed: " + String(e && e.message || e).slice(0, 80), changed: false };
    }
    return { ok: false, note: "unknown op", changed: false };
  }

  // ------------------------------------------------------------------ submit
  // The visible submit-like button, for the shell's Send path (section 6): a button whose
  // own text says Submit, else a type=submit inside a form that holds real fields (not the
  // site's search). Returns its index in the current refs, appending it when the last view
  // did not list it (cut by the cap, or never viewed), or null.
  function submitButton() {
    const refs = window.__tailorAgentRefs || [];
    const cands = [];
    const roots = [document];
    for (const h of Array.from(document.querySelectorAll("*")).filter((e) => e.shadowRoot)) roots.push(h.shadowRoot);
    for (const r of roots) for (const el of Array.from(r.querySelectorAll("button, input[type='submit'], input[type='button'], [role='button'], a"))) {
      if (selfHidden(el) || !boxShown(el) || isDisabled(el)) continue;
      let hiddenUp = false;
      for (let p = el.parentElement; p && !hiddenUp; p = p.parentElement) if (selfHidden(p)) hiddenUp = true;
      if (hiddenUp) continue;
      if (submitLike(el)) { cands.push({ el, score: 2 }); continue; }
      const isSubmitType = (tagOf(el) === "BUTTON" && String(el.type || "submit").toLowerCase() === "submit")
                        || (tagOf(el) === "INPUT" && String(el.type || "").toLowerCase() === "submit");
      if (!isSubmitType) continue;
      const form = el.form || (el.closest && el.closest("form"));
      if (!form) continue;
      if (/search/i.test([form.getAttribute("action"), form.getAttribute("role"), form.id, String(form.className || "")].filter(Boolean).join(" "))) continue;
      const fields = Array.from(form.querySelectorAll("input, select, textarea")).filter((f) => String(f.type || "").toLowerCase() !== "hidden");
      if (fields.length) cands.push({ el, score: 1 });
    }
    if (!cands.length) return null;
    cands.sort((a, b) => b.score - a.score);
    const el = cands[0].el;
    let i = refs.indexOf(el);
    if (i < 0) { refs.push(el); window.__tailorAgentRefs = refs; i = refs.length - 1; }
    return _base + i;
  }

  function rect(index) {
    const el = element(index);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.left, y: r.top, width: r.width, height: r.height };
  }

  // The choices an OPEN custom dropdown shows (the shell opens it with a real click first, then
  // closes it with Escape): the listbox the control names (aria-controls / aria-owns, on it or
  // on the input inside it), else the options visible on the page, else the items of a visible
  // listbox. Labels only (the site's words), placeholders dropped, at most 40.
  function popupOptions(index) {
    const el = element(index);
    if (!el) return [];
    const texts = [];
    const add = (o) => {
      const t = cleanText(o.textContent).slice(0, 80);
      if (t && !PLACEHOLDER_RE.test(norm(t)) && !texts.includes(t)) texts.push(t);
    };
    const ids = [];
    const named = [el].concat(el.querySelectorAll ? Array.from(el.querySelectorAll("[aria-controls], [aria-owns]")) : []);
    for (const n of named) for (const a of ["aria-controls", "aria-owns"]) ids.push(...String(attrOf(n, a) || "").split(/\s+/));
    for (const id of ids.filter(Boolean)) {
      const box = byId(el, id);
      if (!box) continue;
      const opts = Array.from(box.querySelectorAll(OPTION_SEL));
      for (const o of (opts.length ? opts : Array.from(box.querySelectorAll("li")))) if (!selfHidden(o)) add(o);
    }
    if (!texts.length) for (const o of visibleOptions()) add(o);
    if (!texts.length) {
      for (const lb of Array.from(document.querySelectorAll("[role='listbox'], ul[class*='listbox'], ul[class*='dropdown']"))) {
        if (boxShown(lb)) for (const li of Array.from(lb.querySelectorAll("li"))) add(li);
      }
    }
    return texts.slice(0, 40);
  }
  // Close a dropdown popupOptions read (the in-page fallback; the shell presses a real Escape).
  function closePopup(index) {
    const el = element(index);
    const target = document.activeElement || el;
    for (const t of [target, el]) if (t) { fire(t, "keydown", { key: "Escape", code: "Escape", keyCode: 27 }); fire(t, "keyup", { key: "Escape", code: "Escape", keyCode: 27 }); }
    return !!el && attrOf(el, "aria-expanded") !== "true";
  }

  window.TailorAgent = {
    view, element, act, submitButton, rect, popupOptions, closePopup,
    refs: () => window.__tailorAgentRefs || [],
    SUBMIT_RE,
  };
})();
