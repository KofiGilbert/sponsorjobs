// The apply agent follows a link from the person's mail only on the site it is applying on
// (the same registrable domain; the same host on shared hosting) or on the same hiring system
// that site runs on. Every other link is refused and never widens the run's allowed hosts.
// Everyone and every site here is made up.
"use strict";
const path = require("path");
const { mailLinkOk, regDomain, atsFamily } = require(path.join(__dirname, "..", "..", "shell-electron", "agent.js"));
const assert = (c, m) => { if (!c) throw new Error(m); };
let n = 0;
const yes = (link, sites, why) => { n++; assert(mailLinkOk(link, sites) === true, "should pass: " + why + " " + link); };
const no = (link, sites, why) => { n++; assert(mailLinkOk(link, sites) === false, "should be refused: " + why + " " + link); };

// registrable domains
assert(regDomain("jobs.brightwater.example") === "brightwater.example", "plain");
assert(regDomain("careers.brightwater.co.uk") === "brightwater.co.uk", "co.uk");
assert(regDomain("careers.brightwater.com.au") === "brightwater.com.au", "com.au");
assert(regDomain("careers.brightwater.com.pl") === "brightwater.com.pl", "any registry label under a country code");
assert(regDomain("brightwater.wd5.myworkdayjobs.com") === "myworkdayjobs.com", "workday");
assert(atsFamily("brightwater.wd5.myworkdayjobs.com") === atsFamily("wd5.myworkday.com"), "one Workday family");
assert(atsFamily("eeho.fa.us2.oraclecloud.com") === "oracle-recruiting", "oracle recruiting shape");
assert(atsFamily("objectstorage.us-ashburn-1.oraclecloud.com") === "", "not any oraclecloud.com");
assert(atsFamily("brightwater.example") === "", "an employer site is no hiring system");

// on the site itself
yes("https://careers.brightwater.example/activate?t=1", ["careers.brightwater.example"], "same host");
yes("https://login.brightwater.example/verify?t=1", ["careers.brightwater.example"], "same registrable domain");
yes("https://www.brightwater.example/verify", ["careers.brightwater.example"], "www");
yes("https://id.brightwater.co.uk/verify", ["careers.brightwater.co.uk"], "co.uk sibling");
yes("https://careers.brightwater.example/verify", ["www.adzuna.com", "careers.brightwater.example"], "the page that asked counts");
// the same hiring system
yes("https://brightwater.wd5.myworkdayjobs.com/en-US/careers/activate/abc", ["brightwater.wd5.myworkdayjobs.com"], "Workday tenant");
yes("https://wd5.myworkday.com/brightwater/activate/abc", ["brightwater.wd5.myworkdayjobs.com"], "Workday family");
yes("https://career41.sapsf.com/verify?x=1", ["brightwater.successfactors.com"], "SuccessFactors family");
yes("https://my.greenhouse.io/users/confirmation?t=1", ["job-boards.greenhouse.io"], "Greenhouse");
yes("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/verify", ["eeho.fa.us2.oraclecloud.com"], "Oracle tenant");

// lookalikes and other sites
no("https://brightwater-careers.example-other.com/verify", ["careers.brightwater.example"], "lookalike name");
no("https://careers.brightwater.example.other-site.net/verify", ["careers.brightwater.example"], "site name as a subdomain");
no("https://myworkdayjobs.com.other-site.net/activate", ["brightwater.wd5.myworkdayjobs.com"], "ATS name as a subdomain");
no("https://workday.com.other-site.net/activate", ["brightwater.wd5.myworkdayjobs.com"], "workday.com as a subdomain");
no("https://careers.brightwater.example@other-site.net/verify", ["careers.brightwater.example"], "userinfo");
no("https://user:pw@careers.brightwater.example/verify", ["careers.brightwater.example"], "sign-in details in the link");
no("https://other-site.net\\@careers.brightwater.example/verify", ["careers.brightwater.example"], "backslash before @");
no("https://other-site.net/verify?next=https://careers.brightwater.example", ["careers.brightwater.example"], "unrelated");
no("javascript:alert(1)", ["careers.brightwater.example"], "not a web link");
no("data:text/html,hi", ["careers.brightwater.example"], "data link");
no("", ["careers.brightwater.example"], "empty");
no("https://careers.brightwater.example/verify", [], "no site");
no("https://jobs.lever.co/other/verify", ["brightwater.wd5.myworkdayjobs.com"], "another hiring system");
no("https://objectstorage.us-ashburn-1.oraclecloud.com/n/x/verify", ["eeho.fa.us2.oraclecloud.com"], "shared Oracle domain");
no("https://other.herokuapp.com/verify", ["brightwater.herokuapp.com"], "shared hosting sibling");
yes("https://brightwater.herokuapp.com/verify", ["brightwater.herokuapp.com"], "shared hosting, same host");
no("https://other.co.uk/verify", ["careers.brightwater.co.uk"], "a public suffix is not a site");

console.log("ok (" + n + " links)");
