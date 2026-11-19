// The Answers section of an application's panel on Auto-apply (ui/static/answers_panel.js), under
// node: the lines to check come first, the counts ("N to check" leaves out an optional box left
// empty, as on the bar), a best guess reads "Best guess: <reason>. Check it." in the bar's own
// words (aaAnsFlag, read from app.js), a self-identification value the record does not keep reads
// as hidden, and a long list collapses to the lines to check plus "Show all N".
// tests/test_panel_answers.py runs it. Made-up companies only.
"use strict";
const path = require("path");
const fs = require("fs");
const P = require(path.join(__dirname, "..", "..", "ui", "static", "answers_panel.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const eq = (a, b, m) => assert(JSON.stringify(a) === JSON.stringify(b), `${m}: got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);

// The bar's own origin words and reason lines, straight from app.js (T returns the English).
const js = fs.readFileSync(path.join(__dirname, "..", "..", "ui", "static", "app.js"), "utf8");
const a = js.indexOf("  function aaAnsFrom(code) {");
const b = js.indexOf('  // "Role at Company"', a);
assert(a > 0 && b > a, "aaAnsFrom / aaAnsFlag moved in app.js");
// eslint-disable-next-line no-new-func
const W = new Function("T", js.slice(a, b) + "\nreturn { aaAnsFrom, aaAnsFlag };")((k, en) => en);
const opts = (extra) => Object.assign({
  from: W.aaAnsFrom,
  note: (r) => W.aaAnsFlag(r.flag, r.fact, r.value, r.evidence),
  fileKind: (f) => (f.kind === "cover_letter" ? "Cover letter" : "Resume"),
  fileNote: (f) => (f.flag === "other_resume" ? "Tailored for another application, not this job." : f.flag ? W.aaAnsFlag(f.flag) : ""),
  empty: "(empty)", held: "Hidden for privacy",
  page: (n) => "Page " + n, filesHead: "Files",
}, extra || {});

// A quiet (Auto) run's list as the engine keeps it (agent_ready.answers): page by page.
const list = {
  rows: [
    { page: 1, label: "Phone", value: "555 0100", from: "profile", flag: "" },
    { page: 1, label: "Are you legally authorized to work in the US?", value: "Yes", from: "workauth", flag: "" },
    { page: 1, label: "Have you ever been convicted of a felony?", value: "No", from: "best_guess", flag: "best_guess",
      evidence: "nothing you saved mentions one" },
    { page: 2, label: "Gender", value: "", from: "eeo", flag: "", self_id: true, held: true },
    { page: 2, label: "Field of Study", value: "Economics", from: "closest", flag: "closest" },
    { page: 2, label: "Portfolio", value: "", from: "left_empty", flag: "left_empty" },
    { page: 2, label: "Do you have a non-compete?", value: "No", from: "best_guess", flag: "best_guess" },
    { page: 3, label: "Why Harlow Press?", value: "Because I read it.", from: "drafted", flag: "drafted" },
  ],
  files: [
    { kind: "resume", name: "Sam Lee Resume.pdf", record_id: 4, flag: "" },
    { kind: "cover_letter", name: "Sam Lee Cover Letter.pdf", record_id: 9, flag: "other_resume" },
  ],
  flagged: 5,
};

// ---- the counts: every line, and the lines to check (an optional box left empty is not one)
{
  const m = P.model(list, opts({ expanded: true }));
  eq(m.total, 10, "every row and file");
  eq(m.check, 5, "to check: 2 best guesses, closest, drafted, the other application's file");
  eq(m.check, list.flagged, "the same count as the bar's Review answers");
  assert(!m.collapsed && m.more === 0 && m.shown === 10, "expanded: everything shows");

  // ---- the lines to check first, in page order, files after rows; then the rest by page
  eq(m.groups[0].head, "", "the lines to check lead, with no page heading");
  eq(m.groups[0].items.map((x) => x.q), ["Have you ever been convicted of a felony?", "Field of Study", "Do you have a non-compete?",
                                          "Why Harlow Press?", "Cover letter"], "flagged first");
  assert(m.groups[0].items.every((x) => x.flag && x.note), "each line to check carries its mark and reason");
  eq(m.groups.slice(1).map((g) => g.head), ["Page 1", "Page 2", "Files"], "the rest, page by page, files last");
  eq(m.groups[1].items.map((x) => x.q), ["Phone", "Are you legally authorized to work in the US?"], "page 1");

  // ---- the bar's words: origin, and the best guess with its reason
  const felony = m.items.find((x) => x.q.startsWith("Have you ever"));
  eq(felony.from, "Best guess", "origin label");
  eq(felony.note, "Best guess: nothing you saved mentions one. Check it.", "best-guess reason line");
  eq(m.items.find((x) => x.q.startsWith("Do you have a non")).note, "Best guess. Check it.", "best guess without a reason");
  eq(m.items.find((x) => x.q === "Field of Study").note, "Optional. Picked as the closest match on the list.", "closest");
  eq(m.items.find((x) => x.q === "Phone").from, "Your profile", "plain origin");

  // ---- an optional box left empty: marked like the bar, never counted
  const empty = m.items.find((x) => x.q === "Portfolio");
  assert(empty.flag && !empty.check && empty.note === "Left empty: optional." && empty.v === "(empty)", "left empty " + JSON.stringify(empty));

  // ---- a self-identification value the record does not keep
  const g = m.items.find((x) => x.q === "Gender");
  assert(g.held && g.v === "Hidden for privacy" && g.from === "Self-identification", "self-identification hidden " + JSON.stringify(g));
  // ...and shown when the shell's own list has it (agentReady)
  const local = { rows: [{ page: 2, label: "Gender", value: "Prefer to say", from: "eeo", flag: "", self_id: true }], files: [] };
  eq(P.model(local, opts()).items[0].v, "Prefer to say", "the shell's value shows");
}

// ---- a long list collapses to the lines to check plus "Show all N"
{
  const m = P.model(list, opts());
  assert(m.collapsed, "over 8 lines: collapsed");
  eq(m.groups.length, 1, "only the lines to check");
  eq(m.shown, 5, "five shown");
  eq(m.more, 5, "five behind Show all");
  eq(m.total, 10, "Show all 10");
  // a short list never collapses
  const short = { rows: list.rows.slice(0, 3), files: [] };
  const s = P.model(short, opts());
  assert(!s.collapsed && s.more === 0 && s.shown === 3, "a short list shows whole");
  eq(s.groups.map((x) => x.head), ["", "Page 1"], "short: the one to check, then page 1");
  // a long list with nothing to check: its first lines
  const plain = { rows: Array.from({ length: 12 }, (_, i) => ({ page: 1 + (i > 5 ? 1 : 0), label: "Q" + i, value: "A", from: "profile", flag: "" })), files: [] };
  const p = P.model(plain, opts());
  assert(p.collapsed && p.check === 0 && p.shown === P.LIMIT && p.more === 12 - P.LIMIT, "nothing to check: the first lines " + JSON.stringify([p.shown, p.more]));
  eq(p.groups.map((x) => x.head), ["Page 1", "Page 2"], "still by page");
}

// ---- nothing stored: an empty model, nothing to paint
{
  const m = P.model(null, opts());
  assert(m.total === 0 && m.check === 0 && m.groups.length === 0, "empty");
}

console.log("ok");
