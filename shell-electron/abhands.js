// EXPERIMENT (default off): the apply agent's "eyes and hands" through vercel-labs/agent-browser
// instead of extension/agent_view.js + agent.js's own actions. SJ_HANDS=agent-browser turns it on;
// anything else (the default) leaves the agent exactly as it was. The brain (autoapply/brain.py),
// the planner and fill engine, the answer library, consent, the allowlist and the stop at Submit
// stay ours in both modes. See docs/agent-hands-eval.md for the method and the result.
//
// How it works, per step:
//   1. agent.js builds OUR view as usual (it refreshes window.__tailorAgentRefs, which the shell's
//      own helpers use: page check, saved answers, dropdown readers, read-only tools), and tags
//      each of our elements with data-sj-ix="<our index>".
//   2. `agent-browser --cdp <the shell's own CDP port> snapshot -i --json` of the Browser pane's tab
//      (never the app UI's tab) gives its accessibility snapshot with refs (@e1...).
//   3. One `agent-browser batch` reads data-sj-ix through each ref, so an element both sides see
//      keeps OUR index (the shell's helpers keep working on it) and an element only agent-browser
//      sees gets an index from 5001 up. The brain reads the snapshot, converted to our line grammar.
//   4. The brain's click / type / select / upload / scroll run as agent-browser commands on the
//      element's ref; its JSON result (or error) becomes the step result.
// The CDP port is the shell's own (127.0.0.1); nothing new is opened to the network. agent-browser
// is never told to close anything (its `close` would close the app's browser).
"use strict";

const { execFile } = require("child_process");

const ENABLED = String(process.env.SJ_HANDS || "").toLowerCase() === "agent-browser";
const BIN = process.env.SJ_AGENT_BROWSER_BIN || "agent-browser";
const SESSION = process.env.SJ_AGENT_BROWSER_SESSION || "sponsorjobs-hands";
const ONLY_BASE = 5001;              // indices for elements only agent-browser's snapshot has
const MAX_CHARS = 12000;             // the same cap as our own view
const SELF_ID = /gender|\bsex\b|\brac(e|ial)\b|ethnic|hispanic|latin[oax]|veteran|disab|sexual orientation|transgender|pronoun|self[- ]?identif|first[- ]generation/i;
const SUBMITISH = /\bsubmit\b|\bsend application\b|\breview and submit\b|\bcomplete application\b/i;
const FILEISH = /^(no file chosen|no file selected)$|\.(pdf|docx?|txt|rtf|odt)\s*$/i;

function cdpPort() { return String(Number(process.env.TAILOR_CDP_PORT) || 9223); }

// One agent-browser command; resolves to its parsed JSON ({success, data, error}), never rejects.
function ab(args, opts) {
  const o = opts || {};
  return new Promise((resolve) => {
    const child = execFile(BIN, ["--session", SESSION, "--cdp", cdpPort(), ...args, "--json"],
      { timeout: o.ms || 25000, maxBuffer: 16 * 1024 * 1024, env: Object.assign({}, process.env, { NO_COLOR: "1" }) },
      (err, stdout) => {
        let out = null;
        try { out = JSON.parse(String(stdout || "").trim().split("\n").filter(Boolean).pop() || "null"); } catch (e) { out = null; }
        if (!out) out = { success: false, data: null, error: String((err && (err.killed ? "agent-browser timed out" : err.message)) || "no reply").slice(0, 200) };
        resolve(out);
      });
    if (o.input != null) { child.stdin.end(String(o.input)); }
  });
}

// Make the Browser pane's tab agent-browser's active tab: the tab whose URL is the pane's, never
// the app window (the engine's own origin).
async function selectPaneTab(paneUrl, appOrigin) {
  const r = await ab(["tab", "list"]);
  const tabs = (r && r.data && r.data.tabs) || [];
  const notApp = tabs.filter((t) => t.type === "page" && !(appOrigin && String(t.url || "").startsWith(appOrigin)));
  const want = notApp.find((t) => t.url === paneUrl) || notApp.find((t) => (paneUrl || "").startsWith(String(t.url || "").split("#")[0])) || (notApp.length === 1 ? notApp[0] : null);
  if (!want) return { ok: false, note: "the pane's tab was not found (" + tabs.length + " tabs)" };
  if (!want.active) {
    const s = await ab(["tab", want.tabId]);
    if (!s.success) return { ok: false, note: "could not switch to the pane's tab: " + (s.error || "") };
  }
  return { ok: true, tabId: want.tabId };
}

// "- role "name" [a, b=c, ref=e1] hint="...": text"
const LINE_RE = /^\s*-\s+([\w-]+)(?:\s+"((?:[^"\\]|\\.)*)")?(?:\s+\[([^\]]*)\])?(?:\s+hint="((?:[^"\\]|\\.)*)")?(?::\s*(.*))?$/;
function parseSnapshot(text) {
  const out = [];
  for (const raw of String(text || "").split("\n")) {
    const m = LINE_RE.exec(raw);
    if (!m) continue;
    const attrs = {};
    for (const part of String(m[3] || "").split(/,\s*/)) {
      if (!part) continue;
      const eq = part.indexOf("=");
      if (eq < 0) attrs[part.trim()] = true; else attrs[part.slice(0, eq).trim()] = part.slice(eq + 1).trim();
    }
    out.push({ role: m[1], name: (m[2] || "").replace(/\\"/g, '"'), attrs, ref: attrs.ref || "", hint: (m[4] || "").replace(/\\"/g, '"'), text: (m[5] || "").trim() });
  }
  return out;
}

const esc = (s) => String(s || "").replace(/\s+/g, " ").trim().replace(/"/g, '\\"').slice(0, 80);
const hintWords = (h) => String(h || "").replace(/^(id|name|data-testid|testid|placeholder):/i, "").replace(/[_-]+/g, " ").trim();

// One snapshot line in our grammar (docs/apply-agent.md section 3). No typed value ever leaves:
// a text box reads value="" or value="(filled)"; a self-identification choice reads (filled).
// agent-browser's hint names an unlabeled control by the text around it ("in:Which location are
// you applying for?"); that is its label when the accessible name is empty (Lever's questions).
function hintLabel(h) {
  const m = /(?:^|;\s*)in:([^;]*)/.exec(String(h || ""));
  return m ? m[1].replace(/\s*[*\u2731]\s*$/, "").trim() : "";
}
function lineFor(n, ourLine) {
  const a = n.attrs, name = esc(n.name || hintLabel(n.hint));
  const req = a.required ? " required" : "";
  const dis = a.disabled ? " disabled" : "";
  const label = (s) => `label="${s}"`;
  switch (n.role) {
    case "textbox": case "searchbox": case "spinbutton": {
      const type = n.role === "searchbox" ? " search=true" : n.role === "spinbutton" ? " type=number" : "";
      return `<input${type} ${label(name)} value="${n.text ? "(filled)" : ""}"${req}${dis}>`;
    }
    case "combobox": case "listbox": {
      const tag = /^\*?\[\d+\]<select\b/.test(ourLine || "") ? "select" : "combobox";
      let val = n.text || (a.value != null ? String(a.value) : "");
      if (val && SELF_ID.test(n.name)) val = "(filled)";
      const exp = a.expanded != null ? ` expanded=${a.expanded}` : "";
      return `<${tag} ${label(name)} value="${esc(val)}"${exp}${req}${dis}>`;
    }
    case "checkbox": case "radio": case "switch": case "menuitemcheckbox": case "menuitemradio": {
      const kind = n.role === "radio" || n.role === "menuitemradio" ? "radio" : "checkbox";
      let checked = a.checked != null ? String(a.checked) : "false";
      if (SELF_ID.test(n.name)) checked = "hidden";
      return `<${kind} ${label(name)} checked=${checked}${req}${dis}>`;
    }
    case "button": {
      if (FILEISH.test(n.text) || /type:file/i.test(n.hint)) {
        const words = [n.name, hintWords(n.hint)].filter(Boolean).join(" ");
        return `<file ${label(esc(words))} file=${/^no file/i.test(n.text) ? "none" : "chosen"}${req}>`;
      }
      return `<button${dis}>${name}</button>`;
    }
    case "link": return `<a>${name}</a>`;
    case "option": case "menuitem": case "treeitem": return `<option${a.selected ? " selected=true" : ""}>${name}</option>`;
    case "tab": return `<tab${a.selected ? " selected=true" : ""}>${name}</tab>`;
    case "slider": return `<input type=range ${label(name)}${req}>`;
    default: return `<${n.role.replace(/[^a-z]/gi, "") || "el"}>${name}</${n.role.replace(/[^a-z]/gi, "") || "el"}>`;
  }
}

// The view the brain reads, from agent-browser's snapshot. ourText: our own view of the same
// page (for the select/combobox tag only). prev: the refs of the previous view (the * mark).
function buildView({ url, title, nodes, ourIx, ourText, prev }) {
  const ourLine = (i) => { const m = new RegExp("^\\*?\\[" + i + "\\]<.*$", "m").exec(ourText || ""); return m ? m[0] : ""; };
  const lines = [], refOf = new Map(), indices = new Set(), used = new Set();
  let next = ONLY_BASE;
  for (const n of nodes) {
    if (n.role === "heading") { if (n.name) lines.push({ kind: "text", s: n.name.slice(0, 200) }); continue; }
    if (!n.ref) continue;
    let idx = Number(ourIx.get(n.ref)) || 0;
    if (!idx || used.has(idx)) idx = next++;
    used.add(idx);
    refOf.set(idx, n.ref);
    indices.add(idx);
    const star = prev && prev.size && !prev.has(n.ref) ? "*" : "";
    lines.push({ kind: "el", s: `${star}[${idx}]${lineFor(n, idx < ONLY_BASE ? ourLine(idx) : "")}`, submit: n.role === "button" && SUBMITISH.test(n.name) });
  }
  const head = `URL: ${url}\nTitle: ${String(title || "").slice(0, 120)}\n[Start of page]\n`;
  const out = [head];
  let size = head.length + 60, cut = -1;
  for (let i = 0; i < lines.length; i++) {
    if (size + lines[i].s.length + 1 > MAX_CHARS) { cut = i; break; }
    out.push(lines[i].s + "\n");
    size += lines[i].s.length + 1;
  }
  if (cut >= 0) {
    const rest = lines.slice(cut).filter((l) => l.kind === "el");
    out.push(`(${rest.length} more elements below)\n`);
    const sub = rest.filter((l) => l.submit).slice(0, 2);
    if (sub.length) out.push("The page's submit, further down:\n" + sub.map((l) => l.s + "\n").join(""));
    out.push("[End of view]");
  } else out.push("[End of page]");
  return { text: out.join(""), indices, refOf, refs: new Set(nodes.map((n) => n.ref).filter(Boolean)) };
}

// The whole "eyes" step: pane tab, snapshot, ref -> our index, the converted view.
// Returns null when agent-browser cannot read the pane (the caller then uses our own view).
async function snapshotView({ url, title, appOrigin, ourText, prev }) {
  const t0 = Date.now();
  const tab = await selectPaneTab(url, appOrigin);
  if (!tab.ok) return { error: tab.note };
  const snap = await ab(["snapshot", "-i"], { ms: 30000 });
  if (!snap.success || !snap.data) return { error: "snapshot failed: " + (snap.error || "") };
  const nodes = parseSnapshot(snap.data.snapshot || "");
  const refs = nodes.map((n) => n.ref).filter(Boolean);
  const ourIx = new Map();
  if (refs.length) {
    const b = await ab(["batch"], { input: JSON.stringify(refs.map((r) => ["get", "attr", "@" + r, "data-sj-ix"])), ms: 30000 });
    const rows = Array.isArray(b) ? b : (b && Array.isArray(b.data) ? b.data : []);
    for (const row of rows) {
      const ref = String(((row && row.command) || [])[2] || "").replace(/^@/, "");
      const val = row && row.result && row.result.value;
      if (ref && val != null && /^\d+$/.test(String(val))) ourIx.set(ref, Number(val));
    }
  }
  const v = buildView({ url, title, nodes, ourIx, ourText, prev });
  v.stats = { nodes: nodes.length, matched: ourIx.size, ms: Date.now() - t0 };
  return v;
}

// The "hands": one brain action on an element's ref. Returns our step-result shape.
async function act(a, ref, line, info) {
  const fail = (r) => ({ ok: false, note: "agent-browser: " + String((r && r.error) || "no result").replace(/\s+/g, " ").slice(0, 140) });
  const ok = (note) => ({ ok: true, note: note || "" });
  if (a.op === "click") {
    const r = await ab(["click", "@" + ref]);
    return r.success ? ok() : fail(r);
  }
  if (a.op === "type") {
    const text = String(a.text == null ? "" : a.text);
    if (/^\*?\[\d+\]<(combobox|select)\b/.test(line) || /search=true/.test(line)) {
      // A typeahead: clear it, then real keystrokes so its list opens.
      await ab(["fill", "@" + ref, ""]);
      const r = await ab(["type", "@" + ref, text]);
      return r.success ? ok() : fail(r);
    }
    const r = await ab(["fill", "@" + ref, text]);
    return r.success ? ok() : fail(r);
  }
  if (a.op === "select") {
    const r = await ab(["select", "@" + ref, String(a.option || "")]);
    return r.success ? ok() : fail(r);
  }
  if (a.op === "upload") {
    const file = info && info.file;
    if (!file) return { ok: false, note: "the cover letter box takes a cover letter, never the resume; left empty" };
    const r = await ab(["upload", "@" + ref, file], { ms: 30000 });
    return r.success ? ok("uploaded") : fail(r);
  }
  return null;                                    // not an agent-browser action: the caller's own path
}

async function scroll(direction) {
  const r = await ab(["scroll", direction === "up" ? "up" : "down", "600"]);
  return r.success ? { ok: true, note: "" } : { ok: false, note: "agent-browser: " + String(r.error || "").slice(0, 120) };
}

// Our own view's elements tagged with their index, so a ref can be mapped back to it.
const TAG_JS = `(() => { const refs = window.__tailorAgentRefs || []; let n = 0;
  document.querySelectorAll("[data-sj-ix]").forEach((el) => { if (!refs.includes(el)) el.removeAttribute("data-sj-ix"); });
  refs.forEach((el, i) => { try { el.setAttribute("data-sj-ix", String(i + 1)); n++; } catch (e) {} });   // the main view's indexBase is 1
  return n; })()`;

module.exports = { ENABLED, ab, selectPaneTab, parseSnapshot, buildView, snapshotView, act, scroll, TAG_JS, ONLY_BASE };
