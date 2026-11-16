// The accessibility tree, merged into the agent's page view, and the final form check before
// "Ready to send" (docs/apply-agent.md section 20).
//
// Why both: reading a live Greenhouse form through Chrome's accessibility tree (the tree screen
// readers use) showed what our DOM view missed: every box of a "How did you hear" group ticked,
// the resume in the Cover Letter box, a dropdown left open, three copies of the resume on
// Workday. The accessibility tree alone hides what the DOM view has (page text, error messages,
// react-select's chosen value, Lever's "✱" required marks, long labels). So the DOM view stays
// the base, and each of its control lines gets the states the accessibility tree adds.
//
//   enrich({ wc, fr, deps, guard, text })   the view text with the accessibility states added
//   readyGate({ ..., feed, seen, recordId }) the final check: { ok } | { nudge } | { ask } | { noted }
//   collect(cdp, scopes, text)               the merged entries (tests drive this on a raw CDP)
//   verifyForm(entries, opts)                pure: the blockers a merged view shows
//
// How: for every frame of every session (Page.getFrameTree), Accessibility.getFullAXTree, keeping
// the controls with their role, name, value and states, keyed by backendDOMNodeId. The view's own
// element list (window.__tailorAgentRefs, one element per [N]) is resolved to backendDOMNodeIds
// through DOM.describeNode, so each line and its accessibility node meet by id (by name when
// the node has no id of its own). Facts only the page knows (the file names a box shows, a
// "✱" required mark, which question a box belongs to) come from one in-page function.
//
// The role list and the property reading follow vercel-labs/agent-browser
// cli/src/native/snapshot.rs (INTERACTIVE_ROLES, extract_properties; Apache-2.0).
//
// Nothing personal goes to the model from here: a file name never appears on a line (only its
// kind, "resume" or "cover letter"), a text box's value is only ever "(filled)", and a
// self-identification choice is never added. Off unless SJ_AX=1 (DEFAULT_ON below).
"use strict";

const path = require("path");

// Default OFF: the mock end-to-end run (shell-electron/e2e-agent.mjs) has not been run with it
// on yet. Flip to true once it passes there.
const DEFAULT_ON = false;
const AX_MS = Math.max(500, Number(process.env.SJ_AX_MS) || 6000);
const MAX_FRAMES = 16;                // frames per session
const MAX_REFS = 600;                 // view elements resolved per frame
const GROUP = "sj-ax";

function on() {
  const v = String(process.env.SJ_AX || "").toLowerCase();
  if (v === "1" || v === "true" || v === "on") return true;
  if (v === "0" || v === "false" || v === "off") return false;
  return DEFAULT_ON;
}

// (agent-browser snapshot.rs INTERACTIVE_ROLES, plus Chrome's names for a native select.)
const CONTROL_ROLES = new Set(["button", "link", "textbox", "checkbox", "radio", "combobox", "listbox",
  "menuitem", "menuitemcheckbox", "menuitemradio", "option", "searchbox", "slider", "spinbutton",
  "switch", "tab", "treeitem", "PopUpButton", "MenuListPopup"]);
const SELF_ID_RE = /gender|\bsex\b|\brac(e|ial)\b|ethnic|hispanic|latin[oax]|veteran|disab|sexual orientation|transgender|pronoun|self[- ]?identif|first[- ]generation (professional|college|student)/i;
const FIELD_KINDS = new Set(["input", "textarea", "select", "combobox", "checkbox", "radio", "file"]);

// ------------------------------------------------------------------ the accessibility tree
const axVal = (v) => (v && v.value != null ? v.value : null);
const str = (v) => (v == null ? "" : String(v));
const norm = (s) => String(s || "").toLowerCase().replace(/[*✱]+/g, "").replace(/\s+/g, " ").trim();

function axControl(n) {
  if (!n || n.ignored || !n.backendDOMNodeId) return null;
  const role = str(axVal(n.role));
  const props = {};
  for (const p of n.properties || []) props[p.name] = p.value ? p.value.value : null;
  const invalid = props.invalid != null && String(props.invalid) !== "false" ? String(props.invalid) : "";
  const wanted = CONTROL_ROLES.has(role) || props.checked != null || props.expanded != null || !!props.required || !!invalid;
  if (!wanted) return null;
  return {
    backendId: n.backendDOMNodeId, frameId: n.frameId || "", role,
    name: str(axVal(n.name)).replace(/\s+/g, " ").trim(), value: str(axVal(n.value)),
    checked: props.checked == null ? null : String(props.checked),          // "true" | "false" | "mixed"
    expanded: props.expanded == null ? null : props.expanded === true || props.expanded === "true",
    required: props.required === true, disabled: props.disabled === true,
    invalid, focused: props.focused === true, hasPopup: str(props.hasPopup),
  };
}

function frameIdsOf(tree) {
  const out = [];
  const walk = (node) => {
    if (!node || out.length >= MAX_FRAMES) return;
    if (node.frame && node.frame.id) out.push(node.frame.id);
    for (const c of node.childFrames || []) walk(c);
  };
  walk(tree);
  return out;
}

// Every control node of every frame this session renders.
async function axNodesOf(cdp, sessionId) {
  const send = (m, p) => cdp.send(m, p || {}, sessionId);
  try { await send("Accessibility.enable"); } catch (e) { /* older targets: the tree call still works */ }
  let ids = [];
  try { ids = frameIdsOf((await send("Page.getFrameTree")).frameTree); } catch (e) { ids = []; }
  if (!ids.length) ids = [null];
  const out = [];
  for (const fid of ids) {
    let r = null;
    try { r = await send("Accessibility.getFullAXTree", fid ? { frameId: fid } : {}); } catch (e) { continue; }  // an out-of-process frame: its own session
    for (const n of (r && r.nodes) || []) { const c = axControl(n); if (c) out.push(c); }
  }
  try { await send("Accessibility.disable"); } catch (e) { /* fine */ }
  return out;
}

// ------------------------------------------------------------------ the view's own elements
// The backendDOMNodeId of each element behind [N] in this scope (index - base), via the page's
// own list of them.
async function refBackendIds(cdp, scope) {
  const send = (m, p) => cdp.send(m, p || {}, scope.sessionId);
  const ids = [];
  try {
    const ev = await send("Runtime.evaluate", Object.assign({ expression: "window.__tailorAgentRefs || []", objectGroup: GROUP },
                                                            scope.contextId ? { contextId: scope.contextId } : {}));
    const oid = ev && ev.result && ev.result.objectId;
    if (!oid) return ids;
    const props = await send("Runtime.getProperties", { objectId: oid, ownProperties: true });
    const jobs = [];
    for (const p of (props && props.result) || []) {
      if (!/^\d+$/.test(p.name) || !p.value || !p.value.objectId) continue;
      const i = Number(p.name);
      if (i >= MAX_REFS) continue;
      jobs.push(send("DOM.describeNode", { objectId: p.value.objectId })
        .then((d) => { if (d && d.node) ids[i] = d.node.backendNodeId; }).catch(() => {}));
    }
    await Promise.all(jobs);
  } catch (e) { /* the page went away: no ids, the label match below still works */ }
  finally { try { await send("Runtime.releaseObjectGroup", { objectGroup: GROUP }); } catch (e) { /* fine */ } }
  return ids;
}

// Runs IN THE PAGE (stringified): what only the page knows about each view element. Kept self
// contained. File names stay in the shell (verifyForm), never on a line the model reads.
function pageFacts(max) {
  const refs = (window.__tailorAgentRefs || []).slice(0, max);
  const E = window.TailorAutofill || null;
  const clean = (t) => String(t || "").replace(/\s+/g, " ").trim();
  const tag = (el) => String((el && el.tagName) || "").toUpperCase();
  const attr = (el, n) => (el && el.getAttribute ? el.getAttribute(n) : null);
  const FILE_RE = /^[^\\/:*?"<>|\r\n]{1,150}\.(pdf|docx?|rtf|txt|odt|pages|png|jpe?g)$/i;
  const STAR_RE = /[*✱]|\(required\)/i;
  const LETTER_RE = /cover\s*letter|letter of (interest|motivation)|motivation(al)? letter/i;
  const CONTROL_SEL = "input, select, textarea, [role='combobox'], [role='textbox'], [role='radio'], [role='checkbox'], [role='listbox'], [role='switch'], button[aria-haspopup='listbox']";
  const LABELISH = "label, legend, [class*='label' i], [class*='question' i], [id*='label' i], [data-automation-id*='label' i], [data-automation-id*='Label']";
  const isToggle = (el) => (tag(el) === "INPUT" && /^(checkbox|radio)$/i.test(el.type || "")) || /^(checkbox|radio|switch)$/i.test(attr(el, "role") || "");
  const typeOf = (el) => (tag(el) === "INPUT" ? String(el.type || "text").toLowerCase() : String(attr(el, "role") || tag(el)).toLowerCase());
  const shown = (el) => {
    try {
      if (!el || !el.getClientRects || !el.getClientRects().length) return false;
      const cs = el.ownerDocument.defaultView.getComputedStyle(el);
      return cs.visibility !== "hidden" && cs.display !== "none";
    } catch (e) { return false; }
  };
  const ignorable = (o) => {
    if (tag(o) === "INPUT" && /^(hidden|submit|button|reset|image)$/i.test(o.type || "")) return true;
    if (attr(o, "aria-hidden") === "true") return true;            // react-select's required mirror
    if (o.closest && o.closest("[aria-hidden='true']")) return true;
    return false;
  };
  const groupBox = (el) => (el.closest ? el.closest("fieldset, [role='radiogroup'], [role='group']") : null);
  const sameGroup = (el, o) => {
    if (o === el || el.contains(o) || o.contains(el)) return true;
    if (!isToggle(el) || !isToggle(o) || typeOf(el) !== typeOf(o)) return false;
    if (tag(el) === "INPUT" && el.name) return o.name === el.name;
    const g = groupBox(el);
    return !!g && g === groupBox(o);
  };
  // The field's own box: the widest ancestor (six at most) holding no control of another field.
  const fieldBox = (el) => {
    let box = el.parentElement || el;
    for (let node = el.parentElement, d = 0; node && d < 6; node = node.parentElement, d++) {
      if (/^(FORM|BODY|HTML)$/.test(tag(node))) break;
      const others = Array.from(node.querySelectorAll(CONTROL_SEL)).filter((o) => !ignorable(o) && !sameGroup(el, o));
      if (others.length) break;
      box = node;
    }
    return box;
  };
  const byId = (el, id) => {
    try { const r = el.getRootNode(); return (r && r.getElementById ? r : el.ownerDocument).getElementById(id); } catch (e) { return null; }
  };
  const labelledBy = (el) => String(attr(el, "aria-labelledby") || "").split(/\s+/).filter(Boolean).map((id) => byId(el, id)).filter(Boolean);
  const labelTexts = (el, box) => {
    const out = [];
    const add = (n) => { const t = n && String(n.textContent || ""); if (t && t.trim() && t.length < 300) out.push(t); };
    if (el.labels) for (const l of Array.from(el.labels)) add(l);
    for (const n of labelledBy(el)) add(n);
    const g = groupBox(el);
    if (g) for (const n of labelledBy(g)) add(n);
    for (const n of Array.from(box.querySelectorAll(LABELISH))) {
      if (n.querySelector && Array.from(n.querySelectorAll(CONTROL_SEL)).some((o) => o !== el && !ignorable(o))) continue;
      add(n);
    }
    if (el.closest && el.closest("label")) add(el.closest("label"));
    return out;
  };
  const question = (el, box) => {
    const g = groupBox(el);
    const lg = g && g.querySelector("legend");
    if (lg) return clean(lg.textContent);
    if (g) { const lb = labelledBy(g).map((n) => clean(n.textContent)).join(" "); if (lb) return lb; }
    for (const n of Array.from(box.querySelectorAll(LABELISH))) {
      if (n.querySelector && n.querySelector("input, [role='checkbox'], [role='radio']")) continue;
      const t = clean(n.textContent);
      if (t) return t;
    }
    return clean((g && attr(g, "aria-label")) || "");
  };
  const optionText = (el) => {
    const l = el.labels && el.labels[0];
    return clean((l && l.textContent) || attr(el, "aria-label") || (tag(el) === "INPUT" ? el.value : el.textContent) || "");
  };
  const fileNames = (el, box) => {
    const names = [];
    try {
      const doc = box.ownerDocument;
      const tw = doc.createTreeWalker(box, 4 /* NodeFilter.SHOW_TEXT */);
      for (let n = tw.nextNode(); n; n = tw.nextNode()) {
        const t = clean(n.nodeValue);
        if (!t || !FILE_RE.test(t)) continue;
        const p = n.parentElement;
        if (!p || !shown(p) || /^(OPTION|SELECT|SCRIPT|STYLE)$/.test(tag(p))) continue;
        names.push(t);
      }
    } catch (e) { /* no walker in this world */ }
    if (!names.length && el.files) for (const f of Array.from(el.files)) names.push(String(f.name || ""));
    return names.slice(0, 12);
  };
  const groups = new Map();
  let gseq = 0;
  return refs.map((el) => {
    try {
      if (!el || !el.isConnected) return null;
      const box = fieldBox(el);
      const texts = labelTexts(el, box);
      const toggle = isToggle(el);
      const f = { tag: tag(el), type: typeOf(el), star: texts.some((t) => STAR_RE.test(t)),
                  invalid: attr(el, "aria-invalid") === "true",
                  expanded: attr(el, "aria-expanded") === "true" || !!(el.querySelector && el.querySelector("[aria-expanded='true']")) };
      if (toggle) {
        f.on = tag(el) === "INPUT" ? !!el.checked : attr(el, "aria-checked") === "true";
        f.option = optionText(el).slice(0, 120);
        const key = (tag(el) === "INPUT" && el.name) ? "n:" + el.name + ":" + typeOf(el) : null;
        const owner = key || box;
        if (!groups.has(owner)) groups.set(owner, "g" + (++gseq));
        f.group = groups.get(owner);
        f.question = question(el, box).slice(0, 200);
        f.boxText = clean(box.innerText || box.textContent).slice(0, 400);
      }
      if (tag(el) === "INPUT" && String(el.type || "").toLowerCase() === "file") {
        f.files = fileNames(el, box);
        const words = clean(texts.join(" ") + " " + (box.innerText || "") + " " + (el.name || "") + " " + (el.id || ""));
        let letter = false;
        try { letter = E && E.isLetterBox ? !!E.isLetterBox(el) : LETTER_RE.test(words); } catch (e) { letter = LETTER_RE.test(words); }
        f.letterBox = letter;
        f.resumeBox = !letter && /resume|résumé|\bcv\b|curriculum/i.test(words);
        f.multiple = !!el.multiple;
      }
      return f;
    } catch (e) { return null; }
  });
}
const factsExpr = (max) => "(" + pageFacts.toString() + ")(" + Number(max || MAX_REFS) + ")";

async function evalValue(cdp, scope, expression) {
  const r = await cdp.send("Runtime.evaluate", Object.assign({ expression, returnByValue: true },
                                                              scope.contextId ? { contextId: scope.contextId } : {}), scope.sessionId);
  return r && r.result ? r.result.value : undefined;
}

// ------------------------------------------------------------------ merge
const LINE_RE = /^(\*?\[(\d+)\]<)(\w+)(.*)$/;
const attrIn = (rest, name) => {
  const m = new RegExp("\\b" + name + '="((?:[^"\\\\]|\\\\.)*)"').exec(rest);
  return m ? m[1].replace(/\\"/g, '"').replace(/\\\\/g, "\\") : null;
};

function scopeOf(scopes, index) {
  let best = null;
  for (const s of scopes) if (index >= s.base && (!best || s.base > best.base)) best = s;
  return best;
}

// The merged entries: one per control line of the view, with the DOM line's facts, the page's
// facts and the accessibility node's states. ``scopes``: [{ base, key, ids, facts }] (key: the
// CDP session the frame is rendered in, "" for the pane's own).
function merge(viewText, scopes, axBy, axByKey) {
  const used = new Set();
  const entries = [];
  for (const ln of String(viewText || "").split("\n")) {
    const m = LINE_RE.exec(ln);
    if (!m) continue;
    const index = Number(m[2]), kind = m[3], rest = m[4];
    const scope = scopeOf(scopes, index);
    const at = scope ? index - scope.base : -1;
    const facts = (scope && scope.facts && scope.facts[at]) || null;
    const bid = scope && scope.ids ? scope.ids[at] : undefined;
    const key = scope ? scope.key || "" : "";
    let ax = bid != null ? axBy.get(key + ":" + bid) || null : null;
    const label = attrIn(rest, "label") || "";
    if (!ax && label && FIELD_KINDS.has(kind)) {
      // No node of its own (a wrapper the tree skipped): the same name in the same frame.
      for (const c of axByKey.get(key) || []) {
        if (used.has(c) || norm(c.name) !== norm(label)) continue;
        if (!/textbox|searchbox|combobox|checkbox|radio|listbox|button|PopUpButton|spinbutton|switch/.test(c.role)) continue;
        ax = c; break;
      }
    }
    if (ax) used.add(ax);
    const hidden = /checked=hidden/.test(rest);
    const valueAttr = attrIn(rest, "value");
    entries.push({
      index, kind, line: ln, label, option: attrIn(rest, "option") || (facts && facts.option) || "",
      backendId: bid == null ? null : bid, frame: key, ax: !!ax,
      role: ax ? ax.role : "", name: ax ? ax.name : "", popup: ax ? ax.hasPopup : "",
      required: / required\b/.test(rest) || !!(ax && ax.required) || !!(facts && facts.star),
      disabled: / disabled\b/.test(rest) || !!(ax && ax.disabled),
      // aria-invalid on the element, or the tree's invalid (Chrome also reads an EMPTY required
      // native box as invalid: that one is "required and still empty", decided below, not twice).
      invalid: !!(facts && facts.invalid), axInvalid: !!(ax && ax.invalid === "true"),
      expanded: /expanded=true/.test(rest) || !!(ax && ax.expanded === true) || !!(facts && facts.expanded),
      focused: !!(ax && ax.focused),
      checked: facts && facts.on != null ? !!facts.on : (ax && ax.checked != null ? ax.checked === "true" : null),
      mixed: !!(ax && ax.checked === "mixed"),
      selfId: hidden || SELF_ID_RE.test(label) || SELF_ID_RE.test((facts && facts.question) || ""),
      search: /search=true/.test(rest),
      value: valueAttr, axValue: ax ? ax.value : "",
      file: /file=chosen/.test(rest) ? "chosen" : (/file=none/.test(rest) ? "none" : ""),
      files: (facts && facts.files) || [], letterBox: !!(facts && facts.letterBox), resumeBox: !!(facts && facts.resumeBox),
      group: (facts && facts.group) || "", question: (facts && facts.question) || "", boxText: (facts && facts.boxText) || "",
      inputType: (/<input type=(\w+)/.exec("<" + kind + rest) || [])[1] || "",
    });
  }
  for (const e of entries) {
    if (e.axInvalid && !e.invalid && !(e.required && !filledOf(e))) e.invalid = true;
    delete e.axInvalid;
  }
  return entries;
}

// Does a (non-toggle) control hold something? A file box counts the files it lists.
function filledOf(e) {
  if (e.kind === "file") return e.file === "chosen" || e.files.length > 0;
  if (e.kind === "select") return !!e.value;
  if (e.kind === "input" || e.kind === "textarea" || e.kind === "combobox") return !!e.value || !!e.axValue;
  return true;
}

// ------------------------------------------------------------------ the lines the model reads
const q = (s) => '"' + String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"';
// An accessible name may carry a file's name ("Remove Jane_Resume.pdf"): never on a line.
const noFiles = (s) => String(s || "").replace(/[^\s"]*\.(pdf|docx?|rtf|txt|odt|pages|png|jpe?g)\b/gi, "(file)");

function fileKind(name, opts) {
  const n = norm(name);
  const resume = norm(opts && opts.resumeName), letter = norm(opts && opts.letterName);
  if (resume && n === resume) return "resume";
  if (letter && n === letter) return "cover letter";
  if (/cover|letter|motivation/.test(n)) return "cover letter";
  if (/resume|résumé|\bcv\b|curriculum/.test(n)) return "resume";
  return "file";
}
// "resume x3" / "resume, cover letter": what a box holds, by kind only.
function holdsWords(names, opts) {
  const counts = new Map();
  for (const n of names) { const k = fileKind(n, opts); counts.set(k, (counts.get(k) || 0) + 1); }
  return Array.from(counts.entries()).map(([k, c]) => (c > 1 ? k + " x" + c : k)).join(", ");
}

// The view with each control line carrying what the accessibility tree (and the page) add:
// ``required`` (a "✱" or the tree's own), ``invalid``, ``expanded=true``, ``checked=mixed``, a
// text box the tree reads as filled, and the files a box shows (by kind). Nothing personal.
function annotate(viewText, entries, opts) {
  const byLine = new Map(entries.map((e) => [e.line, e]));
  return String(viewText || "").split("\n").map((ln) => {
    const e = byLine.get(ln);
    if (!e) return ln;
    const m = LINE_RE.exec(ln);
    let head = m[1] + m[3], rest = m[4];
    const field = FIELD_KINDS.has(e.kind);
    const add = [];
    if (field && e.required && !/ required\b/.test(rest)) add.push("required");
    if (e.invalid) add.push("invalid");
    if (e.expanded && !/expanded=true/.test(rest)) {
      if (/expanded=false/.test(rest)) rest = rest.replace("expanded=false", "expanded=true");
      else add.push("expanded=true");
    }
    if (field) {
      if (e.mixed && !e.selfId && /checked=false/.test(rest)) rest = rest.replace("checked=false", "checked=mixed");
      if ((e.kind === "input" || e.kind === "textarea" || e.kind === "combobox") && e.value === "" && e.axValue && e.inputType !== "file")
        rest = rest.replace('value=""', 'value="(filled)"');
      if (e.kind !== "radio" && e.kind !== "checkbox" && e.label === "" && e.name && /label=""/.test(rest))
        rest = rest.replace('label=""', "label=" + q(noFiles(e.name).slice(0, 80)));
      if (e.kind === "file" && e.files.length) {
        rest = rest.replace("file=none", "file=chosen");
        add.push("holds=" + q(holdsWords(e.files, opts)));
      }
    }
    if (!field && e.name && /^<?[^>]*>.{0,2}<\//.test(rest) && e.name.length > 2 && !/ name="/.test(rest)) add.push("name=" + q(noFiles(e.name).slice(0, 60)));
    if (!add.length) return head + rest;
    const extra = " " + add.join(" ");
    if (field && rest.endsWith(">")) return head + rest.slice(0, -1) + extra + ">";
    const gt = rest.indexOf(">");
    return gt < 0 ? head + rest + extra : head + rest.slice(0, gt) + extra + rest.slice(gt);
  }).join("\n");
}

// ------------------------------------------------------------------ the final check
const MULTI_RE = /all that apply|select all|check all|choose all|tick all|mark all|one or more|select (any|multiple)|choose (any|multiple)|multiple (answers|selections|choices)/i;
const SINGLE_RE = /\b(select|choose|pick|check|tick) (only )?one\b|\bonly one\b|\bone answer\b|single (answer|choice)/i;
const SOURCE_RE = /how did you (hear|find|learn|come across|discover)|where did you (hear|find|see|learn)|referral source|source of (your )?application/i;
const YESNO_RE = /^(yes|no|maybe|n\/?a|not sure|i don'?t know|unsure|prefer not to (say|answer)|decline to (answer|say))\b/i;
const EXCLUSIVE_RE = /^(none( of (the|these) (above|options))?|n\/?a|not applicable|prefer not to (say|answer|disclose)|i (do not|don'?t) wish\b.*|decline\b.*|i prefer not\b.*)$/i;

function allowsOne(question, options, boxText) {
  const words = String(question || "") + " " + String(boxText || "");
  if (MULTI_RE.test(words)) return false;
  if (SINGLE_RE.test(words)) return true;
  if (options.length >= 2 && options.every((o) => YESNO_RE.test(String(o || "").trim()))) return true;
  return SOURCE_RE.test(String(question || ""));
}
const shortLabel = (s) => String(s || "").replace(/[*\u2731]+/g, "").replace(/\(required\)/ig, "").replace(/\s+/g, " ").trim().slice(0, 70) || "a field";
const quoted = (s) => "“" + shortLabel(s) + "”";

// The blockers a merged view shows. Returns { ok, required, filled, problems: [{kind, index,
// label, text}], line }. ``opts``: { resumeName, letterName } (file basenames, to tell a resume
// from a cover letter when the file's name says neither).
function verifyForm(entries, opts) {
  const problems = [];
  let required = 0, empty = 0;
  const push = (kind, e, label, text) => problems.push({ kind, index: e ? e.index : null, label: shortLabel(label), text });
  const groups = new Map();
  for (const e of entries) {
    if (e.kind === "checkbox" || e.kind === "radio") {
      const g = e.group || (e.kind + ":" + norm(e.question || e.label));
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(e);
      continue;
    }
    if (!FIELD_KINDS.has(e.kind) && !e.expanded && !e.invalid) continue;
    if (e.expanded && (e.kind === "combobox" || e.kind === "select" || /combobox|listbox|PopUpButton/.test(e.role)
                       || (e.popup && e.popup !== "false" && e.popup !== "dialog")))
      push("left-open", e, e.label || e.name, "the dropdown " + quoted(e.label || e.name) + " is still open");
    if (e.invalid && !e.disabled) push("invalid", e, e.label || e.name, quoted(e.label || e.name) + " is marked invalid");
    if (e.kind === "file") {
      const kinds = e.files.map((n) => fileKind(n, opts));
      const seen = new Map();
      for (const n of e.files) seen.set(norm(n), (seen.get(norm(n)) || 0) + 1);
      const most = Math.max(0, ...seen.values());
      if (most > 1) push("duplicate-file", e, e.label, quoted(e.label || "the upload box") + " holds the same file " + most + " times");
      if (e.letterBox && kinds.includes("resume")) push("wrong-file", e, e.label, "the resume is in the cover letter box " + quoted(e.label || "Cover letter"));
      else if (e.resumeBox && !e.letterBox && kinds.length && kinds.every((k) => k === "cover letter"))
        push("wrong-file", e, e.label, "the cover letter is in the resume box " + quoted(e.label || "Resume"));
    }
    if (!FIELD_KINDS.has(e.kind) || !e.required || e.disabled || e.search) continue;
    required++;
    if (!filledOf(e)) { empty++; push("required-empty", e, e.label || e.name, quoted(e.label || e.name) + " is required and still empty"); }
  }
  for (const members of groups.values()) {
    const first = members[0];
    const qn = first.question || first.label || first.option;
    const req = members.some((m) => m.required && !m.disabled);
    const onCount = members.filter((m) => m.checked === true).length;
    const known = members.every((m) => m.checked != null);
    if (req) {
      required++;
      if (known && onCount === 0) { empty++; push("required-empty", first, qn, quoted(qn) + " is required and nothing is chosen"); }
    }
    if (!known || onCount < 2) continue;
    const options = members.map((m) => m.option || (m.kind === "checkbox" ? m.label : ""));
    const exclusive = members.some((m) => m.checked === true && EXCLUSIVE_RE.test(String(m.option || m.label || "").trim()));
    if (first.kind === "radio") push("many-ticked", first, qn, quoted(qn) + " has " + onCount + " choices on; it takes one");
    else if (members.length >= 3 && onCount === members.length) push("every-box", first, qn, "every box is ticked in " + quoted(qn));
    else if (allowsOne(qn, options, first.boxText)) push("many-ticked", first, qn, quoted(qn) + " takes one answer but " + onCount + " boxes are ticked");
    else if (exclusive) push("many-ticked", first, qn, quoted(qn) + " has a “none” answer ticked with others");
  }
  const ok = problems.length === 0;
  const line = ok
    ? (required ? "Checked " + required + " required field" + (required === 1 ? "" : "s") + ": all filled" : "Checked the form: nothing required is left empty")
    : "Checked " + required + " required field" + (required === 1 ? "" : "s") + ": " + problems.length + (problems.length === 1 ? " problem" : " problems");
  return { ok, required, filled: required - empty, problems, line };
}

// ------------------------------------------------------------------ collecting
// ``scopes``: [{ base, sessionId?, contextId? }] - the pane's own view (base 1, no session, the
// page's main world) and each cross-origin frame the shell viewed (base 1000 * (i + 1), its
// session if out of process, its isolated world).
async function collect(cdp, scopes, viewText) {
  const axBy = new Map(), axByKey = new Map();
  const sessions = Array.from(new Set(scopes.map((s) => s.sessionId || "")));
  for (const sid of sessions) {
    const nodes = await axNodesOf(cdp, sid || undefined);
    axByKey.set(sid, nodes);
    for (const n of nodes) axBy.set(sid + ":" + n.backendId, n);
  }
  const done = [];
  for (const s of scopes) {
    const sc = { base: s.base, key: s.sessionId || "", sessionId: s.sessionId || undefined, contextId: s.contextId || undefined };
    sc.ids = await refBackendIds(cdp, sc);
    try { sc.facts = await evalValue(cdp, sc, factsExpr()); } catch (e) { sc.facts = null; }
    done.push(sc);
  }
  return merge(viewText, done, axBy, axByKey);
}

// The same entries without CDP (DevTools holds the debugger): the page's facts only.
async function collectPageOnly(wc, fr, viewText) {
  const scopes = [];
  try { scopes.push({ base: 1, key: "", facts: await wc.executeJavaScript(factsExpr(), true) }); } catch (e) { if (e && e.stopped) throw e; }
  for (let i = 0; fr && i < (fr.ctxs || []).length; i++) {
    const facts = await fr.eval(fr.ctxs[i], factsExpr()).catch(() => null);
    scopes.push({ base: 1000 * (i + 1), key: fr.ctxs[i].sessionId || "", facts });
  }
  return merge(viewText, scopes, new Map(), new Map());
}

const capped = (p, ms, guard) => {
  if (guard) return guard(p, ms, "accessibility tree");
  let t = null;
  const cap = new Promise((_r, rej) => { t = setTimeout(() => rej(new Error("accessibility tree timed out")), ms); });
  return Promise.race([p, cap]).finally(() => clearTimeout(t));
};

// The live entries for the pane's current view. ``o``: { wc, fr, deps, guard, text }.
async function collectLive(o) {
  const { wc, fr, deps } = o;
  const scopes = [{ base: 1 }];
  for (let i = 0; fr && i < (fr.ctxs || []).length; i++) {
    scopes.push({ base: 1000 * (i + 1), sessionId: fr.ctxs[i].sessionId, contextId: fr.ctxs[i].contextId });
  }
  let cdp = fr && fr.cdp ? fr.cdp : null;
  let release = null, own = null;
  if (!cdp && deps && deps.acquireDebugger && deps.frames && deps.frames.electronCdp && wc) {
    // The same webContents object the rest of the shell passes (its use count is kept per object).
    release = deps.acquireDebugger(wc);
    if (release) { own = deps.frames.electronCdp(wc.debugger); cdp = own; }
  }
  try {
    if (!cdp) return await capped(collectPageOnly(wc, fr, o.text), AX_MS, o.guard);
    return await capped(collect(cdp, scopes, o.text), AX_MS, o.guard);
  } finally {
    if (own) own.dispose();
    if (release) release();
  }
}

async function enrich(o) {
  const entries = await collectLive(o);
  return annotate(o.text, entries, { resumeName: o.resumePath ? path.basename(o.resumePath) : "" });
}

// The final check before Ready to send (docs/apply-agent.md section 20). First time problems
// show: the brain gets them to fix (``nudge``). Second time: the person is asked once (``ask``,
// the run parks). After the person has looked and pressed Continue, the hand-over goes ahead with
// the problems named on Steps (``noted``). Clean: one Steps line "Checked N required fields: all
// filled". ``seen`` is the run's checkedPages set (it survives a park).
async function readyGate(o) {
  const entries = await collectLive({ wc: o.wc, fr: o.fr, deps: o.deps, guard: o.guard, text: o.viewText });
  const res = verifyForm(entries, { resumeName: o.resumePath ? path.basename(o.resumePath) : "", letterName: o.letterPath ? path.basename(o.letterPath) : "" });
  const feed = o.feed || null;
  if (res.ok) {
    if (feed) feed.push("formcheck", "Checked", res.line.replace(/^Checked /, ""), { state: "done" });
    return { ok: true, result: res };
  }
  const texts = res.problems.map((p) => p.text);
  const fixKey = "ax-fix|" + o.recordId, askKey = "ax-asked|" + o.recordId;
  const seen = o.seen || new Set();
  if (!seen.has(fixKey)) {
    seen.add(fixKey);
    if (feed) feed.push("formcheck", "Form check", res.line.replace(/^Checked /, ""), { state: "retry", detail: texts.slice(0, 4).join("; ") });
    const where = res.problems.slice(0, 6).map((p) => (p.index != null ? "[" + p.index + "] " : "") + p.text).join("; ");
    return { result: res, results: texts.slice(0, 3),
             nudge: "Before I hand over, the form check found: " + where + ". Fix these on this page (untick the extra boxes, "
                  + "remove a wrong or repeated file with its own remove button, close the open dropdown, fill what is required); "
                  + "never Submit. Then report ready_to_submit again." };
  }
  if (!seen.has(askKey)) {
    seen.add(askKey);
    if (feed) feed.push("formcheck", "Form check", "still " + res.problems.length + (res.problems.length === 1 ? " problem" : " problems") + ": asking you",
                        { state: "retry", detail: texts.slice(0, 4).join("; ") });
    return { result: res, ask: "Before I hand over: " + texts.slice(0, 2).join("; ") + (texts.length > 2 ? " (and " + (texts.length - 2) + " more)" : "")
                                + ". Fix it on the page, then press Continue." };
  }
  // You looked and pressed Continue: the hand-over goes ahead and Steps names each one ("Check noted").
  return { result: res, noted: texts.slice(0, 3) };
}

module.exports = { on, DEFAULT_ON, enrich, readyGate, collect, collectLive, merge, annotate, verifyForm, allowsOne,
                   fileKind, holdsWords, axControl, pageFacts, factsExpr };
