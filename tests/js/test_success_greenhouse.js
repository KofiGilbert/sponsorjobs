// Greenhouse's confirmation page counts as sent (Gusto, 2026-11-16: the submit worked and the pane
// landed on job-boards.greenhouse.io/<board>/jobs/<id>/confirmation with a "Track your application"
// card, but the bar said "the page showed no confirmation"). Under node with the fixture's text
// (tests/test_ready_answers.py runs it). Always a page signal: a URL or words on the page, never
// an assumption.
"use strict";
const fs = require("fs");
const path = require("path");
const assert = (c, m) => { if (!c) throw new Error(m); };
const html = fs.readFileSync(path.join(__dirname, "..", "fixtures", "forms", "greenhouse_confirmation.html"), "utf8");
const textOf = (h) => h.replace(/<!--[\s\S]*?-->/g, " ").replace(/<(script|style|title)[\s\S]*?<\/\1>/gi, " ").replace(/<[^>]+>/g, " ");
const page = { text: textOf(html) };
const body = { get textContent() { return page.text; }, querySelectorAll: () => [], querySelector: () => null };
global.window = { getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }), addEventListener() {} };
global.document = { body, documentElement: body, querySelectorAll: () => [], querySelector: () => null, getElementById: () => null,
                    readyState: "complete", addEventListener() {} };
global.location = { hostname: "job-boards.greenhouse.io", href: "" };
global.CSS = { escape: (s) => String(s) };
global.HTMLInputElement = class {}; global.HTMLSelectElement = class {}; global.HTMLTextAreaElement = class {};
require(path.join(__dirname, "..", "..", "extension", "autofill_core.js"));
const F = window.TailorAutofill;
const seen = (url) => F.detectSuccess(F.recipeFor(url), url);

const CONF = "https://job-boards.greenhouse.io/acme/jobs/1234567/confirmation";
assert(F.recipeFor(CONF) && F.recipeFor(CONF).id === "greenhouse", "the Greenhouse recipe");
assert(seen(CONF) === true, "the confirmation page");
assert(seen("https://boards.greenhouse.io/acme/jobs/1234567/confirmation?gh_src=x") === true, "the older host, with a query");
// the URL alone is a signal: the page may still be drawing its text
page.text = "";
assert(seen(CONF) === true, "the confirmation URL before its text");
assert(seen("https://job-boards.greenhouse.io/acme/jobs/1234567") === false, "the job page with nothing on it is not sent");
assert(seen("https://job-boards.greenhouse.io/acme/jobs/1234567/confirmation_policy") === false, "only a /confirmation path");
// the card's words alone, on a page whose form is gone
page.text = "Track your application Create a MyGreenhouse account";
assert(seen("https://job-boards.greenhouse.io/acme/jobs/1234567") === true, "the Track your application card");
page.text = "Apply for this job First Name Last Name Submit application";
assert(seen("https://job-boards.greenhouse.io/acme/jobs/1234567") === false, "the open form is not a confirmation");
console.log("ok");
