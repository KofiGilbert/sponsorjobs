// shell-electron/job_identity.js under node (audit case 35): a page the Browser reaches after an
// Autofill is the same job (keep the application) or another job (let it go). Made-up sites and ids.
// tests/test_job_identity.py runs it.
"use strict";
const path = require("path");
const J = require(path.join(__dirname, "..", "..", "shell-electron", "job_identity.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
let n = 0;
function same(filled, next, want, why, o) {
  const a = J.createAnchor();
  for (const f of [].concat(filled)) typeof f === "string" ? a.add(f) : a.add(f.url, f);
  const c = a.check(next, o);
  assert(c.same === want, `${why}: ${JSON.stringify([].concat(filled))} -> ${next}: got ${JSON.stringify(c)}`);
  n++;
}

// --- the live case: two jobs on one careers path, a link opened from the first ---------------
same("https://acme.careers/job?gh_jid=7000001", "https://acme.careers/job?gh_jid=7000002", false, "another posting's gh_jid");
same("https://acme.careers/job?gh_jid=7000001", "https://acme.careers/job?gh_jid=7000001&utm_source=x", true, "the same posting with a tracking tag");
same("https://acme.careers/job?gh_jid=7000001", "https://boards.greenhouse.io/embed/job_app?for=acme&token=7000001", true,
     "the careers page and its Greenhouse frame share the job id");
// --- one application's own pages ----------------------------------------------------------
same("https://job-boards.greenhouse.io/acme/jobs/7000001", "https://job-boards.greenhouse.io/acme/jobs/7000001#app", true, "the form's own anchor");
same("https://job-boards.greenhouse.io/acme/jobs/7000001", "https://job-boards.greenhouse.io/acme/jobs/7000002", false, "another Greenhouse job");
same("https://job-boards.greenhouse.io/acme/jobs/7000001", "https://job-boards.greenhouse.io/acme", true, "the board's home is no other job");
same("https://jobs.lever.co/acme/00000000-0000-4000-8000-000000000001/apply", "https://jobs.lever.co/acme/00000000-0000-4000-8000-000000000001/thanks", true, "Lever's thank-you page");
same("https://jobs.lever.co/acme/00000000-0000-4000-8000-000000000001/apply", "https://jobs.lever.co/acme/00000000-0000-4000-8000-000000000002", false, "another Lever posting");
same("https://jobs.ashbyhq.com/acme/00000000-0000-4000-8000-000000000003/application", "https://jobs.ashbyhq.com/acme/00000000-0000-4000-8000-000000000003", true, "Ashby's posting tab");
const WD = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Chicago-IL/Analyst_R0012345";
same(WD + "/apply/applyManually", WD + "/apply/applyManually?step=2", true, "a later Workday step");
same(WD + "/apply/applyManually", "https://acme.wd5.myworkdayjobs.com/en-US/External/login", true, "Workday's sign-in page");
same(WD + "/apply/applyManually", "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Engineer_R0099999", false, "another Workday requisition");
same("https://careers-acme.icims.com/jobs/12345/analyst/candidate?from=login", "https://careers-acme.icims.com/jobs/12345/analyst/login", true, "iCIMS steps");
same("https://careers-acme.icims.com/jobs/12345/analyst/candidate", "https://careers-acme.icims.com/jobs/67890/engineer/job", false, "another iCIMS job");
same("https://jobs.smartrecruiters.com/Acme/743999912345678-analyst", "https://jobs.smartrecruiters.com/Acme/743999987654321-engineer", false, "another SmartRecruiters posting");
// --- no ids: the path decides ------------------------------------------------------------------
same("https://acme.example.com/careers/data-analyst/apply", "https://acme.example.com/careers/data-analyst/thank-you", true, "the same slug's thank-you");
same("https://acme.example.com/careers/data-analyst/apply", "https://acme.example.com/careers/product-designer", false, "another slug under careers");
same("https://acme.example.com/careers/data-analyst/apply", "https://acme.example.com/privacy", true, "a privacy page is no job (kept)");
same("https://acme.example.com/careers/data-analyst/apply", "https://acme.example.com/careers/search?q=analyst", true, "the careers search is no job (kept)");
// --- other sites ---------------------------------------------------------------------------------
same("https://job-boards.greenhouse.io/acme/jobs/7000001", "https://jobs.lever.co/other/00000000-0000-4000-8000-000000000009", false, "another site");
same("https://job-boards.greenhouse.io/acme/jobs/7000001", "https://login.acme-sso.com/authorize", true, "a sign-in host is a step");
same("https://job-boards.greenhouse.io/acme/jobs/7000001", "https://acme.okta.com/app/x", true, "an SSO provider is a step");
same("https://job-boards.greenhouse.io/acme/jobs/7000001", "https://www.acme.com/thanks-for-applying", true, "a hand-off right after Submit", { afterSubmit: true });
same({ url: "https://www.acme.com/careers/job/4567", form: false }, "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Chicago-IL/Analyst_R0012345", true,
     "a job page with no form hands off to the hiring system's form");
same([{ url: "https://www.acme.com/careers/job/4567", form: false }, "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Chicago-IL/Analyst_R0012345/apply"],
     "https://jobs.lever.co/other/00000000-0000-4000-8000-000000000009", false, "after the form was filled, another site is another job");
// --- shapes --------------------------------------------------------------------------------------
assert(J.identity("https://acme.example.com/2026/11/careers").ids.length === 0, "a year in the path is no job id");
assert(J.identity("https://x.example.com/jobs?jobId=R-00123456").ids[0] === "123456", "an id reduced to its digits");
assert(!J.identity("about:blank").ok, "about:blank is no page");
same("https://acme.careers/job?gh_jid=7000001", "about:blank", true, "a blank page keeps the context");
assert(J.sameJob("https://acme.careers/job?gh_jid=1234567", "https://acme.careers/job?gh_jid=1234567#apply"), "sameJob: the same posting");
assert(!J.sameJob("https://acme.careers/job?gh_jid=1234567", "https://acme.careers/job?gh_jid=7654321"), "sameJob: another posting");
console.log("job identity ok (" + n + " cases)");
