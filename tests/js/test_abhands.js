// The agent-browser adapter's converter (shell-electron/abhands.js, an experiment behind
// SJ_HANDS=agent-browser; docs/agent-hands-eval.md). Pure functions only: no agent-browser, no page.
//   * a snapshot line parses into role, name, attributes, ref, hint and text;
//   * an element both sides see keeps OUR index, one only agent-browser sees gets 5001 and up;
//   * no typed value ever reaches the view: a filled text box reads value="(filled)", and a
//     self-identification choice reads (filled) / checked=hidden;
//   * a file box (agent-browser shows it as a button "No file chosen") becomes a <file> line whose
//     label carries the hint's words, so the brain's cover-letter rule still sees "cover letter";
//   * the * mark is set only for refs that were not in the previous view;
//   * the adapter is OFF unless SJ_HANDS=agent-browser.
"use strict";
const path = require("path");
delete process.env.SJ_HANDS;
const h = require(path.join(__dirname, "..", "..", "shell-electron", "abhands.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };

assert(h.ENABLED === false, "off by default");

const SNAP = [
  '- heading "Apply for this job" [level=2, ref=e5]',
  '- textbox "First Name" [required, ref=e16]: Jane',
  '- textbox "Preferred First Name" [ref=e18]',
  '- combobox "Country" [expanded=false, required, ref=e52]',
  '- button "Attach" [ref=e21] hint="id:resume": No file chosen',
  '- button "Attach" [ref=e26] hint="id:cover_letter": No file chosen',
  '- combobox "Veteran Status (please select one):" [expanded=false, required, ref=e63]: I am not a protected veteran',
  '- checkbox "Indeed" [checked=true, ref=e34]',
  '- button "Submit application" [ref=e14]',
].join("\n");
const nodes = h.parseSnapshot(SNAP);
assert(nodes.length === 9, "nine lines parsed: " + nodes.length);
assert(nodes[1].role === "textbox" && nodes[1].name === "First Name" && nodes[1].attrs.required && nodes[1].ref === "e16" && nodes[1].text === "Jane", "textbox line");
assert(nodes[4].hint === "id:resume" && nodes[4].text === "No file chosen", "hint and text");

const ourIx = new Map([["e16", 10], ["e18", 12], ["e52", 14], ["e21", 20], ["e26", 25], ["e14", 64]]);
const ourText = "[14]<select label=\"Country\" value=\"\" options=200 first=\"A|B\" required>\n";
const v = h.buildView({ url: "https://example.test/apply", title: "Apply", nodes, ourIx, ourText, prev: new Set(["e16", "e18"]) });
const t = v.text;
assert(/^\[10\]<input label="First Name" value="\(filled\)" required>$/m.test(t), "filled box masked, our index kept:\n" + t);
assert(!/Jane/.test(t), "no typed value in the view");
assert(/^\[12\]<input label="Preferred First Name" value="">$/m.test(t), "empty box");
assert(/^\*\[14\]<select label="Country"/m.test(t), "a native select stays a select (our tag), new ref marked *");
assert(/^\*\[20\]<file label="Attach resume" file=none>$/m.test(t), "resume file box");
assert(/^\*\[25\]<file label="Attach cover letter" file=none>$/m.test(t), "cover letter box keeps its words");
assert(/^\*\[5001\]<combobox label="Veteran Status \(please select one\):" value="\(filled\)"/m.test(t), "self-identification choice hidden, agent-browser-only index");
assert(!/protected veteran/.test(t), "the chosen self-identification option never shows");
assert(/^\*\[5002\]<checkbox label="Indeed" checked=true>$/m.test(t), "checkbox state kept");
assert(/^Apply for this job$/m.test(t), "a heading is a text line");
assert(v.refOf.get(10) === "e16" && v.refOf.get(5001) === "e63", "index -> ref map");
assert(v.indices.has(64) && /\[64\]<button>Submit application<\/button>/.test(t), "the submit is listed for the ready gate");
assert(/^URL: https:\/\/example\.test\/apply$/m.test(t) && /\[End of page\]$/.test(t), "header and footer");
console.log("ok");
