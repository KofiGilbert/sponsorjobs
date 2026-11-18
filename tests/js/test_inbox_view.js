// The Inbox screen's pure parts (ui/static/inbox_view.js), under node: label names and chip
// classes, the list and reader times, the row a message becomes, the list request, the empty
// states, "N messages", what a link in a mail may do, and the page a mail body is shown in.
// tests/test_inbox_screen.py runs it. Made-up people and companies only.
"use strict";
const path = require("path");
const V = require(path.join(__dirname, "..", "..", "ui", "static", "inbox_view.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const eq = (a, b, m) => assert(JSON.stringify(a) === JSON.stringify(b), `${m}: got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);

// ---- labels: our wording, never "Rejected"; an unknown label reads as Other ----
eq(V.LABELS, ["received", "rejected", "interview", "action", "other"], "label order");
eq(V.LABELS.map(l => V.labelName(l)), ["Application received", "Not this time", "Interview request", "Action needed", "Other"], "label names");
eq(V.labelName("spam"), "Other", "unknown label");
eq(V.labelName("rejected", (k, en) => k), "mbox.label.rejected", "label key goes through the translator");
eq(V.chipClass("interview"), "chip ib-chip ib-chip-interview", "chip class");
eq(V.chipClass("<x>"), "chip ib-chip ib-chip-other", "an unknown label cannot reach a class name");

// ---- times (en-US so the test reads the same on any machine) ----
const at = (y, mo, d, h, mi) => Math.floor(new Date(y, mo - 1, d, h, mi).getTime() / 1000);
const NOW = new Date(2026, 10, 18, 15, 0).getTime();          // Nov 18, 2026, 3:00 PM local
eq(V.listTime(at(2026, 11, 18, 9, 5), NOW, "en-US"), "9:05 AM", "today is a time");
eq(V.listTime(at(2026, 11, 17, 23, 59), NOW, "en-US"), "Nov 17", "yesterday is a date");
eq(V.listTime(at(2025, 12, 31, 8, 0), NOW, "en-US"), "Dec 31, 2025", "another year carries its year");
eq(V.listTime(0, NOW, "en-US"), "", "no time");
eq(V.listTime("abc", NOW, "en-US"), "", "a bad time");
eq(V.fullTime(at(2026, 11, 12, 15, 4), NOW, "en-US"), "Nov 12, 3:04 PM", "the reader's time");
eq(V.fullTime(at(2025, 3, 2, 8, 30), NOW, "en-US"), "Mar 2, 2025, 8:30 AM", "the reader's time, another year");
eq(V.fullTime(null, NOW, "en-US"), "", "no time in the reader");

// ---- the row model ----
const M = { id: 7, sender: "jobs@acmerobotics.example", sender_name: "Acme Robotics Recruiting", subject: " Interview with Acme ",
            snippet: "Hi Zoe, we would like to invite you", at: at(2026, 11, 18, 9, 5), read: false, label: "interview",
            company: "Acme Robotics", has_attachments: true };
eq(V.rowModel(M, NOW, null, "en-US"), { id: 7, unread: true, from: "Acme Robotics Recruiting", subject: "Interview with Acme",
   snippet: "Hi Zoe, we would like to invite you", time: "9:05 AM", label: "interview", labelText: "Interview request",
   chip: "chip ib-chip ib-chip-interview", company: "Acme Robotics" }, "row");
const bare = V.rowModel({ id: 8, sender: "no-reply@example.com", sender_name: "", subject: "", read: true, label: "nope" }, NOW, null, "en-US");
eq([bare.from, bare.subject, bare.unread, bare.label, bare.company, bare.time], ["no-reply@example.com", "(no subject)", false, "other", "", ""], "a bare row");
eq(V.rowModel({ id: 9 }, NOW).from, "Unknown sender", "no sender at all");
eq(V.senderName({ sender: "a@b.example", sender_name: "  " }), "", "a blank name is not a name");

// ---- the list request ----
eq(V.listQuery({}), "page=1&per_page=50", "default request");
eq(V.listQuery({ label: "action", unread: true, q: " code & link ", archived: true, job: "careers::acme de", page: 3, per: 20 }),
   "label=action&unread=1&q=code%20%26%20link&archived=1&job_id=careers%3A%3Aacme%20de&page=3&per_page=20", "full request");
eq(V.listQuery({ label: "bogus", page: -2, per: 0 }), "page=1&per_page=50", "junk is dropped");
eq([V.isFiltered({}), V.isFiltered({ q: "  " }), V.isFiltered({ unread: true }), V.isFiltered({ label: "other" }),
    V.isFiltered({ archived: true }), V.isFiltered({ job: "x" }), V.isFiltered({ label: "bogus" })],
   [false, false, true, true, true, true, false], "filtered or not");

// ---- counts and empty states ----
eq(V.totalOf({ received: 2, rejected: 1, interview: 0, action: 3, other: 4 }), 10, "total");
eq(V.totalOf(null), 0, "total of nothing");
eq(V.emptyKind({ rows: 3, filter: {}, alias: null }), "", "rows: not empty");
eq(V.emptyKind({ rows: 0, filter: {}, alias: null }), "noaddr", "no address yet");
eq(V.emptyKind({ rows: 0, filter: {}, alias: "zoe.muller@sponsorjobs.ai" }), "nomail", "an address, no mail");
eq(V.emptyKind({ rows: 0, filter: { q: "acme" }, alias: "zoe.muller@sponsorjobs.ai" }), "noresults", "search finds nothing");
eq(V.emptyKind({ rows: 0, filter: { unread: true }, alias: null }), "noresults", "a filter wins over no address");
eq([V.countText(1), V.countText(3), V.countText(0)], ["1 message", "3 messages", "0 messages"], "N messages");
eq(V.countText(4, (k, en) => (k === "mbox.countN" ? "{n} Nachrichten" : en)), "4 Nachrichten", "translated count");

// ---- links in a message ----
eq(V.linkAction("https://calendly.com/acme-robotics/phone-screen"), { kind: "open", url: "https://calendly.com/acme-robotics/phone-screen" }, "a web link opens");
eq(V.linkAction("mailto:priya%40acmerobotics.example?subject=Hi"), { kind: "copy", text: "priya@acmerobotics.example" }, "a mail address is copied");
for (const bad of ["javascript:alert(1)", "data:text/html,<b>x</b>", "file:///etc/passwd", "//evil.example", "/api/erase", "", null])
  eq(V.linkAction(bad), { kind: "none" }, "nothing else acts: " + bad);

// ---- the page a mail body is shown in ----
const doc = V.frameDoc("<p>Hello <b>Zoe</b></p>");
assert(doc.startsWith("<!doctype html>"), "a full document");
assert(doc.includes("<p>Hello <b>Zoe</b></p>"), "the body is in it");
assert(doc.includes(`http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"`), "nothing may load");
assert(!/script-src|img-src|connect-src|font-src|frame-src/.test(V.FRAME_CSP), "no source list is opened up");
assert(doc.includes('<base target="_blank">'), "a stray link cannot load inside the frame");
assert(/html\{background:#fff/.test(doc) && /body\{[^}]*background:#fff;color:#1b1b1f/.test(doc), "a white page with dark text in both themes");
assert(!/<script|<img|<link|url\(|@import|https?:\/\//i.test(V.frameDoc("")), "the frame's own page loads nothing");
eq(V.frameDoc(null).includes("<body></body>"), true, "no body is an empty page");

console.log("inbox view: all checks passed");
