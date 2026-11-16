# Auto-submit allowlist: evidence research

Research for issue #256. **This document changes nothing.** `submit/allowlist.py` is the single
source of truth and only a maintainer edits it, after verifying the evidence here
(CLAUDE.md section 7). Nothing below is a decision; it is the material for one.

- **Researched:** 2026-11-16. Every source was read on that date; each entry carries its URL.
- **Feed snapshot:** `data/feed_cache/jobs.json.gz`, generated 2026-11-16, **304,489 jobs**
  (219 rows had no parsable URL and are excluded from the counts).
- **Question for each candidate:** is there an official submission path an applicant-run app can
  call **without the employer's secret credential**? That is the only thing that makes a site
  AUTO-eligible. Everything else is ASSISTED.

---

## 1. The short answer

**No candidate researched here is AUTO-eligible.** Every platform with a documented application
endpoint gates it behind a credential the employer holds, a partner agreement, or both. The
2026-07-14 verdicts all still hold, and the ten platforms newly researched land the same way.

Two findings need a maintainer decision, and both are more consequential than any promotion
would have been:

1. **The AUTO lane currently covers nothing.** Recruitee, the one `auto_enabled` entry, appears
   in **0 of 304,489** jobs in the feed. See section 4.
2. **Recruitee's keyless claim no longer reads cleanly in its own docs.** Its authentication
   page and its endpoint reference now disagree. See section 3.1.

---

## 2. How much each platform actually matters

Apply-host family by share of the feed. This is what any allowlist change would be worth.

| Platform | Jobs | % of feed | Distinct employers |
|---|---:|---:|---:|
| Workday (`myworkdayjobs.com`) | 184,885 | 60.7% | 718 |
| Oracle Recruiting (`oraclecloud.com`) | 13,870 | 4.6% | 99 |
| Greenhouse | 13,405 | 4.4% | 738 |
| Amazon (own portal, `amazon.jobs`) | 5,282 | 1.7% | 46 |
| Taleo (`taleo.net`) | 2,544 | 0.8% | 15 |
| Ashby | 1,748 | 0.6% | 181 |
| Lever | 1,403 | 0.5% | 74 |
| iCIMS (incl. `jibeapply.com`) | 1,489 | 0.5% | 48 |
| Eightfold | 683 | 0.2% | 6 |
| SmartRecruiters | 303 | 0.1% | 9 |
| JazzHR (`applytojob.com`) | 251 | 0.1% | 17 |
| Workable | 159 | 0.1% | 7 |
| Jobvite | 40 | <0.1% | 7 |
| Breezy | 24 | <0.1% | 7 |
| BambooHR | 13 | <0.1% | 7 |
| **Recruitee (the only AUTO entry)** | **0** | **0%** | **0** |
| Teamtailor, Personio, SuccessFactors | 0 | 0% | 0 |

Two platforms carry two thirds of the feed between them, and both are the least open of the set.
The long tail of employer-own portals is section 5.

---

## 3. Per-platform evidence

Each entry: the submission endpoint, who holds the key, what the public crawl policy says, and
the verdict with its reason.

A note that applies throughout: **`robots.txt` is a crawling policy, not permission to submit an
application.** A permissive `robots.txt` says nothing about whether programmatic applying is
sanctioned, so it is recorded below as context and never as grounds for a verdict.

### 3.1 Recruitee — AUTO today, and the one entry that needs re-checking

| | |
|---|---|
| Endpoint | `POST https://{company}.recruitee.com/api/offers/{offer_slug}/candidates` |
| Auth model | **Disputed between two official pages, see below** |
| Verdict | **Keep ASSISTED-pending-review.** Do not rely on the current AUTO flag without a fresh check. |

The registry records "does not require authorization" (verified 2026-07-14). Read on 2026-11-16,
Recruitee's own documentation now says both things:

- The Careers Site API **authentication** page requires a token:
  > "Calls without the authorization header will return the 401 Unauthorized error"

  with an `X-Careers-Sites-Token` header, and the token is generated inside a company account:
  > "Each user can generate an API Token that will have the exact same scope of permissions as the user"

  [docs.recruitee.com/reference/authentication-1](https://docs.recruitee.com/reference/authentication-1), read 2026-11-16.

- The **endpoint reference** for the same API carries `security: []` in its OpenAPI definition,
  that is, no authentication for this operation, and repeats the original wording:
  > "creating a candidate via this API is like a candidate applying for a job and it will trigger all actions connected with it (like sending an application confirmation email)."

  [docs.recruitee.com/reference/offersoffer_idcandidates](https://docs.recruitee.com/reference/offersoffer_idcandidates) and
  [intro-to-careers-site-api](https://docs.recruitee.com/reference/intro-to-careers-site-api), read 2026-11-16.

Both readings are defensible: a public careers site has to POST applications from a browser, so an
unauthenticated apply endpoint inside an otherwise token-gated API is a normal design. But the
2026-07-14 evidence line no longer matches the page it cites, and the one entry that can trigger
unattended submission should not rest on an ambiguity.

**What I did not do:** confirm against a live Recruitee careers site. Testing it means POSTing to a
real employer's offer, which the issue forbids and which would put a fake application in front of a
real recruiter. A maintainer can settle it from a Recruitee sandbox tenant.

**Practical note:** with 0 jobs in the feed, nothing is lost by leaving this ASSISTED until checked.

### 3.2 Workday — 60.7% of the feed, no applicant-facing submission API

| | |
|---|---|
| Endpoints | SOAP Recruiting web service: `Put_Candidate`, `Import_Applicant`, `Submit_Recruiting_Agency_Candidate` |
| Auth model | Tenant integration credentials (not documented as public on the service page) |
| Verdict | **ASSISTED** |

The Recruiting web service "exposes Workday Human Capital Management Business Services data for
integration with Talent Management and Applicant Tracking systems": system-to-system integration
inside a customer tenant, with `Import_Applicant` described as a "high-volume web service task".
There is no documented public endpoint by which a member of the public submits an application to a
`myworkdayjobs.com` posting; the candidate-facing path is the hosted careers site itself.

A tenant's crawl policy is ordinary, e.g. `https://cisco.wd5.myworkdayjobs.com/robots.txt`:
`Allow: /Cisco_Careers/`, `Disallow: /refreshFacet/`. Again: a crawl permission, not an apply
permission.

Sources: [Recruiting Service Details v41.1](https://community.workday.com/sites/default/files/file-hosting/productionapi/Recruiting/v41.1/Recruiting.html),
[Put_Candidate](https://community.workday.com/sites/default/files/file-hosting/productionapi/Recruiting/v31.0/Put_Candidate.html), read 2026-11-16.

**Honest limit:** the public service page does not state its authentication model, so "tenant
credentials" is inferred from the operations' integration purpose rather than quoted. It does not
change the verdict: nothing there is documented as applicant-usable.

### 3.3 Oracle Recruiting / Taleo — partner-gated in writing

| | |
|---|---|
| Endpoint | `POST .../recruitingJobSiteJobApplications` (Fusion Cloud HCM); Taleo BE REST separately |
| Auth model | Oracle Cloud Marketplace partner access; Taleo BE: username + password + company code |
| Verdict | **ASSISTED** |

The Fusion job-application resource is restricted in the documentation itself:
> "Note that the service endpoints in this resource category can only be used by approved Oracle Cloud Marketplace partners."

Taleo Business Edition's REST API authenticates with a login call taking "username, password and
company code (unique Taleo customer value)", returning a session token: employer credentials.

Sources: [Create a job application](https://docs.oracle.com/en/cloud/saas/human-resources/farws/op-recruitingjobsitejobapplications-post.html),
[Taleo BE REST API Reference Guide](https://www.oracle.com/technetwork/documentation/tberestapiguide-v15b1-2665296.pdf), read 2026-11-16.

### 3.4 Greenhouse — re-verified, unchanged

| | |
|---|---|
| Endpoint | `POST https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{id}` |
| Auth model | `employer_api_key` (HTTP Basic) |
| Verdict | **ASSISTED** (unchanged) |

> "Please keep in mind that the HTTP Basic Auth API token is a secret key. Any form posts should be proxied by your own servers. Any direct post to the /applications POST method would reveal your secret key to anybody that views source--which would be a very bad thing."

`job-boards.greenhouse.io/robots.txt` imposes nothing (its only rules are commented out).

**Maintainer note:** the `doc_url` recorded in `submit/allowlist.py` now 301-redirects.
`developers.greenhouse.io/job-board.html` → [docs.greenhouse.io/job-board.html](https://docs.greenhouse.io/job-board.html).
Worth updating the entry so the evidence link stays direct. Read 2026-11-16.

### 3.5 Lever — re-verified, unchanged

> "To use the POST API, you need an API key, which a Super Admin of your company can generate from your integrations settings page."

`POST /v0/postings/{site}/{posting}?key=APIKEY`. An applicant cannot obtain a Super Admin key.
`jobs.lever.co/robots.txt` carries a Content-Signal line, `search=yes,ai-train=no,use=reference`,
and blocks several AI crawlers by name; it says nothing about applying.

Source: [lever/postings-api README](https://github.com/lever/postings-api/blob/master/README.md), read 2026-11-16. **ASSISTED** (unchanged).

### 3.6 Ashby — re-verified, unchanged

`POST https://api.ashbyhq.com/applicationForm.submit`, Basic Auth, and
> "Requires the `candidatesWrite` permission."

Notably, `jobs.ashbyhq.com/robots.txt` disallows the API path to crawlers outright:
`Disallow: /api/`.

Source: [developers.ashbyhq.com/reference/applicationformsubmit](https://developers.ashbyhq.com/reference/applicationformsubmit), read 2026-11-16. **ASSISTED** (unchanged).

### 3.7 Workable — re-verified, unchanged

`POST https://{subdomain}.workable.com/spi/v3/jobs/{shortcode}/candidates`:
> "Creates a candidate at the specified job. Required scope: `w_candidates`. Accessible with all token types."

The token is the employer account's. `apply.workable.com/robots.txt` disallows nothing and sets
`Content-Signal: search=yes, ai-input=yes, ai-train=no`.

Source: [workable.readme.io/reference/job-candidates-create](https://workable.readme.io/reference/job-candidates-create), read 2026-11-16. **ASSISTED** (unchanged).

### 3.8 SmartRecruiters — re-verified, unchanged

The public Posting API is read-only:
> "Posting API allows for access to a job posting that was previously made public using SmartRecruiters application."

Submitting needs employer or partner credentials:
> "One of authorization headers must be present in each request (Authentication). If you are using OAuth 2.0 authentication then `candidate_applications_manage` scope is required"

Sources: [posting-api](https://developers.smartrecruiters.com/docs/posting-api),
[partners-post-an-application](https://developers.smartrecruiters.com/docs/partners-post-an-application), read 2026-11-16. **ASSISTED** (unchanged).

### 3.9 Teamtailor — new, partner-gated

Two paths, neither applicant-usable:

- `POST https://api.teamtailor.com/v1/candidates` with `Authorization: Token token=...`. Keys are
  created in the employer's account (Settings → Integrations → API keys); creating job
  applications needs Admin write scope.
- The **Direct Apply API** (`POST /apply`) is for job boards, authenticated by an HMAC-SHA256
  `X-Api-Signature` computed from a partner API key, or by IP allowlisting, and gated behind a
  partnership: "If you are interested in integrating with us ... contact us at techpartnerships@teamtailor.com."

Sources: [partner.teamtailor.com/job_boards](https://partner.teamtailor.com/job_boards/),
[docs.teamtailor.com](https://docs.teamtailor.com/), read 2026-11-16. **ASSISTED.** (0 jobs in the feed.)

### 3.10 Personio — new

`POST https://api.personio.de/v2/recruiting/applications`, with a Bearer token from the employer's
Settings → Integrations → API Credentials → Recruiting API Key, an `X-Company-Id` header, and the
`personio:recruiting:write` scope.

Source: [developer.personio.de/reference/post_v2-recruiting-applications](https://developer.personio.de/reference/post_v2-recruiting-applications), read 2026-11-16. **ASSISTED.** (0 jobs in the feed.)

### 3.11 BambooHR — new

`POST https://{companyDomain}.bamboohr.com/api/v1/applicant_tracking/application`. The API key is
generated from a logged-in user's menu and sent over HTTP Basic Auth as the username; "the
authenticated caller must have access to ATS settings". Employer-held by construction.

Source: [documentation.bamboohr.com/reference/create-candidate](https://documentation.bamboohr.com/reference/create-candidate),
[getting-started](https://documentation.bamboohr.com/docs/getting-started), read 2026-11-16. **ASSISTED.**

### 3.12 JazzHR — new, and the vendor advises against the endpoint

The key is "the 32 digit alphanumeric key found in their JazzHR account ... Only Super
Administrators, Recruiting Administrators, or Developers have visibility of the API Key." Beyond
that, JazzHR steers integrators away from using it to apply at all: it does not recommend using
`POST /applicant` to submit candidate applications, recommending the `url` or `buttons` attributes
instead.

Source: [help.jazzhr.com API Overview](https://help.jazzhr.com/s/article/API-Overview),
[Integrate JazzHR with Your Career Page](https://help.jazzhr.com/s/article/Integrate-JazzHR-with-Your-Career-Page-Website-Advanced-Methods), read 2026-11-16. **ASSISTED.**

### 3.13 Breezy — new

`POST /v3/company/{companyId}/position/{positionId}/candidates`. All requests except `/v3/signin`
and `/v3/health` require an Authorization header; Personal Access Tokens are created by an admin in
their own user settings. The endpoint does have an `origin: applied` mode that "runs the candidate
through the same path as a public application", but reaching it still needs the employer's token.

Source: [developer.breezy.hr/reference/addcandidate](https://developer.breezy.hr/reference/addcandidate),
[authorization](https://developer.breezy.hr/reference/authorization), read 2026-11-16. **ASSISTED.**

### 3.14 Jobvite — new, and the apply page is required

API keys are contractual ("If included in your contract, you will be provided with API keys"), and
Jobvite states the API does not cover applying: "the API is not open 100% so you are still required
to use their apply page, which can be integrated into an iFrame or linked directly to it."

Source: [Jobvite Career Site Integration](http://careers.jobvite.com/careersite/integration.html),
[help.jobvite.com Integrations & API](https://help.jobvite.com/hc/en-us/sections/24681426999069-Integrations-API), read 2026-11-16. **ASSISTED.**

### 3.15 iCIMS — new, partner-gated with a sharing prohibition

OAuth 2.0 with a `client_id` and `client_secret` issued to a partner, and the credential policy is
explicit:
> "API credentials and user access shall be issued to the Partner and must not be shared with any third-party without ICIMS' express consent."

The documented candidate flow is the opposite direction: the "Application Complete" event notifies
a partner *after* a candidate applies on the client's portal.

Source: [developer-community.icims.com/getting-started/integrating-icims](https://developer-community.icims.com/getting-started/integrating-icims),
[Acceptable Use Policy](https://developer-community.icims.com/acceptable-use-policy), read 2026-11-16. **ASSISTED.**

### 3.16 SAP SuccessFactors — new

Job applications can be created through the OData API (`JobApplication`, `Candidate` entities
support CREATE), but authentication is OAuth 2.0 with a client application registered **in the
customer's SuccessFactors instance**: `company_id`, a registered `client_id`, and a SAML bearer
assertion.

Source: [JobApplication (OData V2)](https://help.sap.com/docs/successfactors-platform/sap-successfactors-api-reference-guide-odata-v2/jobapplication), read 2026-11-16. **ASSISTED.** (0 jobs in the feed.)

---

## 4. The finding that matters most

`submit/allowlist.py` has exactly one `auto_enabled` entry, Recruitee. **It matches 0 of the
304,489 jobs in the current feed.** Teamtailor, Personio and SuccessFactors are also absent.

So the AUTO lane is not merely narrow, it is unreachable: no job a person can currently find in
this product can be auto-submitted, and this research found nothing that would change that. The
honest framing for the product is that **auto-submit is a capability with no live surface today**,
and the "we never promise auto-apply to every job" language in CLAUDE.md section 7 is, if anything,
understated.

That is a product decision rather than a code one, which is why it is written here and not acted
on. Three directions a maintainer might weigh:

1. **Leave it.** The architecture is right and costs nothing while empty.
2. **Pursue a partnership.** Teamtailor, iCIMS and SmartRecruiters all have documented partner
   programmes. A signed agreement is the only route the vendors themselves describe, and it is a
   business step, not an engineering one.
3. **Say so plainly in the UI.** If effectively every application is assisted, the review queue
   saying so for every job is the truthful version of today's behaviour.

---

## 5. Employer-own portals in the feed

Top hosts that are not a recognised ATS family, by job count. "Sponsor-record jobs" counts rows
carrying an employer sponsorship record, which is the measure of how much they matter to the people
this product is for.

| Host | Jobs | Employers | Sponsor-record jobs |
|---|---:|---:|---:|
| careers.costco.com | 13,513 | 2 | 13,423 |
| careers.marriott.com | 5,086 | 7 | 4,563 |
| jobs.bayada.com | 2,762 | 1 | 2,762 |
| careers.bankofamerica.com | 2,321 | 1 | 2,321 |
| careers.questdiagnostics.com | 2,142 | 1 | 2,142 |
| careers.pruitthealth.com | 1,610 | 1 | 1,610 |
| careers.henryford.com | 1,599 | 1 | 1,599 |
| apply.careers.microsoft.com | 1,580 | 1 | 1,580 |
| google.com | 1,469 | 1 | 1,469 |
| accenture.com | 1,291 | 1 | 1,291 |
| careers.walmart.com | 1,168 | 1 | 1,168 |
| jobs.thermofisher.com | 1,062 | 1 | 1,062 |
| careers.appliedmaterials.com | 979 | 1 | 979 |
| jobs.walgreens.com | 931 | 1 | 931 |
| jobs.apple.com | 883 | 1 | 883 |

547 distinct unclassified hosts in total.

**Verdict for all of them: ASSISTED**, and the reason is structural rather than per-employer. A
vanity careers host is almost always a white-labelled ATS or a careers-site product (Phenom,
Eightfold, Avature and Jibe all appear in the feed under their own names too), so the submission
path is whichever platform sits behind it, and every platform in section 3 is employer-key-gated.
None of these employers publishes an applicant-callable submission API.

Two carry public crawl policies worth recording, neither of which addresses applying:
`careers.costco.com/robots.txt` is `Allow: /` with `crawl-delay: 5`;
`careers.marriott.com/robots.txt` lists only a sitemap.

Checking each of 547 hosts for an underlying platform would need per-host inspection that this
research deliberately did not do, and it would not change a verdict: an employer-key-gated backend
is employer-key-gated whatever hostname fronts it.

---

## 6. Method, and what was deliberately not done

**Done:** read each vendor's official developer documentation on 2026-11-16; quoted the
authentication requirement verbatim where the vendor states it; fetched `robots.txt` for the main
ATS hosts and the two largest employer portals; counted platform share from the local feed snapshot
with a read-only script.

**Not done, on purpose, per the issue's constraints:**

- No account was created on any ATS or employer portal.
- No application, test or otherwise, was submitted anywhere.
- Nothing behind a login was accessed, and no careers site was scraped; the only live requests were
  for public `robots.txt` files, sent with an identifying user agent.
- `submit/allowlist.py` was not touched. Promoting a site is a maintainer action, by design.

**Known limits of this research:**

- Workday's and Oracle's public pages do not state their authentication model for the operations in
  question, so those two auth models are inferred from the documented purpose rather than quoted.
  Neither inference affects the verdict.
- Vendor documentation is not the same as a contract. Where a vendor offers a partner programme,
  only a signed agreement settles what is permitted, and none exists.
- `robots.txt` governs crawling and is recorded as context only. It is never a basis for a verdict
  here, and should not become one.

## Maintainer note, 2026-11-16 (Claude Code)

Re-read Recruitee's authentication page: it lists `/offers/:offer_slug/candidates` under the
Careers Site token's scope and states "Deadline for introducing the token to calls is
10 February 2027". The token is generated in the employer's account, so from that date the path
is not applicant-usable. Decision: Recruitee stays AUTO until then (the apply operation still
declares no security today) and turns ASSISTED on 2027-02-10 by itself (`auto_until` in
`submit/allowlist.py`). Greenhouse's `doc_url` now points at docs.greenhouse.io.
