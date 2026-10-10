# SponsorJobs: Visa Sponsor Badges + Assisted Apply (browser extension)

A browser extension for SponsorJobs. The sponsor badges work on their own, with no app
installed; the assisted-apply features use the local **SponsorJobs** app. It does two things:

1. **Sponsor badges.** As you browse job postings on **LinkedIn, Indeed, Greenhouse,
   Lever, or Ashby**, it reads the employer name already on the page and shows small
   badges, **H-1B**, **green card (PERM)**, **cap-exempt**, **STEM-OPT**, sourced from
   official USCIS/DOL data. No app needed: see "Sponsor data without the app" below.
2. **Assisted apply.** On a job **application form** (Greenhouse, Lever, Ashby, Workday,
   Workable, SmartRecruiters, iCIMS), a small panel offers to **pre-fill**:
   - **Contact fields**, name, email, phone, address (split into street/city/state/zip),
     LinkedIn/GitHub, from your saved SponsorJobs profile.
   - **Screening answers**, "authorized to work in the US?" and "need visa sponsorship?", from answers you save **once** in the extension popup (great for F-1/OPT students).
   - **EEO / demographic questions**, only if you tick the box, and only ever the neutral
     **"Decline to self-identify"** option. It never guesses a gender/race/veteran value.
   - It also **flags the resume upload** (browsers block extensions from setting file
     inputs, so you attach your tailored PDF yourself).
   - **Long-answer questions**, for free-text boxes ("Why do you want this role?",
     "Describe a challenge…") and a cover-letter box, a **Draft long answers** button
     drafts each one from your saved profile (grounded in your real material, using the
     app's model). They're a **starting point to edit**, never a finished submission.

   **You review every field and click submit yourself**, the extension never submits for
   you and never touches a field you already filled.

   Set your saved answers in the popup under **Application answers**.

It is **read-only for browsing** and **fill-only for applications**. It never scrolls,
scrapes in bulk, logs in, or submits anything (see the compliance note below).

## How it fits together
- **This extension** (in your browser) shows the badges on the job boards you already use,
  from its own copy of the public sponsor data (below).
- **The SponsorJobs app** (on your computer) adds the rest: your profile, the CV tailoring
  engine, the match score, and form-fill (via `http://127.0.0.1:57000/api/profile/autofill`).
  Without it those spots show a "get the free app" link instead.

### Sponsor data without the app
The feed publishes the H-1B / PERM / E-Verify data as a static index next to the job feed
(`scripts/build_sponsor_index.py` -> `https://feed.sponsorjobs.ai/feed/sponsors/`): one gzipped
shard per first letter of the employer name (about 7.7 MB in all) plus a `manifest.json`. The
extension downloads only the shards it needs, caches them in IndexedDB, re-checks the manifest
at most once a day, and matches names itself with `sponsor_core.js`, a port of the app's
matcher (`sourcing/sponsors.py`) that `tests/test_extension_sponsor_core.py` holds to the app's
exact answers. **No company name ever leaves your browser**: the feed host sees only which
letter-shards were downloaded. If the index can't be downloaded (first use while offline), the
extension asks the local app instead, as before.

## Install (developer / unpacked)
1. (Optional, for form-fill, match score and tailoring) start the SponsorJobs app
   (`python -m ui.app`). The badges work without it.
2. Open `chrome://extensions` (or `edge://extensions`).
3. Turn on **Developer mode** (top-right).
4. Click **Load unpacked** and select this `extension/` folder.
5. Open a job on LinkedIn/Indeed, the badges appear under the company name. Or click
   the extension icon to check any employer by name.
6. Open an application form (e.g. a Greenhouse/Lever/Ashby "Apply" page), the
   **Assisted apply** panel appears bottom-right. Click **Fill this application**,
   review what it filled (highlighted green), attach your resume, and submit yourself.

(When published, it installs from the Chrome Web Store in one click.)

## Compliance
Per the project rules, this extension is strictly the GREEN lane:
- It **reads only the job page you chose to open**, that's you browsing by hand.
- Assisted apply **only fills a form you opened, and only empty fields**, it never
  clicks submit, never uploads a file on its own, and never touches what you typed.
  You review everything and submit yourself.
- **No** logged-in automation, **no** bulk scraping, **no** auto-apply, **no**
  submitting on your behalf. True auto-submit only where a site's official API permits it.

## Roadmap
- **E-Verify (STEM-OPT)** live per-employer lookup against the USCIS tool.
- Assisted apply: broaden field coverage (custom screening questions, EEO) and
  resume-file guidance per ATS.
- Pass the job's description straight into the CV tailoring flow ("Tailor my CV").
