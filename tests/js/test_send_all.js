// Send all's one confirmation (ui/static/send_all.js, docs/apply-agent.md section 31), under node:
// none when nothing holds a guessed legal answer; when something does, ONE confirmation naming
// each such application with its count, and the three choices: Review them first (sends
// nothing, opens the first), Send the others (only those without), Send all anyway.
// tests/test_auto_never_stops.py runs it. Made-up companies only.
"use strict";
const path = require("path");
const fs = require("fs");
const SA = require(path.join(__dirname, "..", "..", "ui", "static", "send_all.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
const eq = (a, b, m) => assert(JSON.stringify(a) === JSON.stringify(b), `${m}: got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);
const ids = (rows) => rows.map((r) => r.id);

const A = { id: 1, role: "Analyst", company: "Brightwater Freight", guessed: 2, guessed_legal: 2 };
const B = { id: 2, role: "Engineer", company: "Larkspur Labs", guessed: 1, guessed_legal: 0 };
const C = { id: 3, role: "Designer", company: "Pinecrest Studio" };
const D = { id: 4, role: "Writer", company: "Harlow Press", guessed: 1, guessed_legal: 1 };

// ---- no confirmation when nothing holds a guessed legal answer (a plain guess does not count)
assert(!SA.needsConfirm([B, C]) && !SA.needsConfirm([]), "no confirmation without a guessed legal answer");
eq(SA.decide("all", [B, C]).send.map((r) => r.id), [2, 3], "Send all sends them all");

// ---- the one confirmation: each held application, by company and role, with its count
const rows = [A, B, C, D];
assert(SA.needsConfirm(rows), "a confirmation when one holds a guessed legal answer");
const m = SA.confirmModel(rows);
eq(m.title, "2 applications have best guesses on legal questions", "title");
eq(m.items, [{ id: 1, text: "Analyst at Brightwater Freight", count: "2 guessed legal answers" },
             { id: 4, text: "Writer at Harlow Press", count: "1 guessed legal answer" }], "the list");
eq(m.choices.map((c) => c.label), ["Review them first", "Send the other 2, keep these", "Send all anyway"], "the three choices");
assert(m.choices[0].primary && !m.choices[1].disabled, "Review is the one primary");
eq(SA.confirmModel([A]).title, "1 application has best guesses on legal questions", "one");
assert(SA.confirmModel([A]).choices[1].disabled === true, "nothing else to send: that choice is off");
// through the translator
const keys = SA.confirmModel(rows, (k) => k);
assert(keys.title === "sendall.titleN" && keys.choices[2].label === "sendall.all", "every word goes through T");

// ---- the three paths
eq(SA.decide("review", rows), { send: [], review: 1 }, "Review them first: nothing sent, the first one opens");
eq(ids(SA.decide("others", rows).send), [2, 3], "Send the others: only those without a guessed legal answer");
eq(ids(SA.decide("all", rows).send), [1, 2, 3, 4], "Send all anyway: every one");
eq(SA.decide("", rows), { send: [], review: null }, "closed: nothing is sent");
eq(SA.decide("whatever", rows), { send: [], review: null }, "an unknown choice sends nothing");

// ---- no dashes in the words, and app.js asks before it sends
const src = fs.readFileSync(path.join(__dirname, "..", "..", "ui", "static", "send_all.js"), "utf8");
assert(!/[—–]/.test(src), "no em or en dash");
const app = fs.readFileSync(path.join(__dirname, "..", "..", "ui", "static", "app.js"), "utf8");
const body = app.slice(app.indexOf("async function aaSendAll()"), app.indexOf("$(\"#aaSendAll\")?.addEventListener"));
assert(body.indexOf("needsConfirm") > 0 && body.indexOf("needsConfirm") < body.indexOf("agentSendAll(rows"), "the confirmation comes before anything is sent");
console.log("ok");
