# SponsorJobs: Visa Sponsor Badges + Assisted Apply (browser extension)

A browser extension for SponsorJobs. The sponsor badges work on their own, with no app
installed; the assisted-apply features use the local **SponsorJobs** app. It does two things:

1. **Sponsor badges.** As you browse job postings on **LinkedIn, Indeed, Glassdoor,
   Handshake, Dice, Google Jobs, ZipRecruiter**, or an ATS page (**Greenhouse, Lever,
   Ashby**, ...), it reads the employer name already on the page and shows small
   badges, **H-1B**, **green card (PERM)**, **cap-exempt**, **E-Verify**, sourced from
   official USCIS/DOL data. No app needed: see "Sponsor data without the app" below.
   Result lists get a compact badge per card; the open job gets the full row.
   - **What the ad itself says**, read from its own text with the same reader the app uses
     (`adstance_core.js`, a port of `sourcing/adstance.py`): a green **Ad offers sponsorship**
     chip, a red **No sponsorship for this role** chip, or a red **Citizenship or clearance
     required** chip when the ad requires US citizenship, a green card / permanent residency,
     or a security clearance. Each red chip quotes the ad's own sentence, in its tooltip and
     on a small grey line under the chips, so the proof is on the page.
   - **Chip meanings.** E-Verify: enrolled in E-Verify, so a STEM OPT extension is possible
     here. Cap-exempt: likely H-1B without the lottery (a university, nonprofit research, or
     hospital employer, from the public record and the employer's name). All of it is the
     employer's history, never a promise about this role.
   - **Sponsored only** (every board's result list): click our mark in the bottom-left
     corner and tick **Sponsored only** to hide results whose employer has no sponsorship
     record (no H-1B, no green card, no E-Verify). It shows "Hiding N of M", keeps results
     still being looked up visible, remembers your choice, and is pure styling on our side
     of the page: nothing is clicked, scrolled, or asked of the site.
   - **One adapter per board** (`boards_core.js`). Each board is a small object: which
     elements are the result cards, how to read a card's company / title / location, where the
     chip row is mounted on the open job, and where the JD text is for the ad-stance reader.
     LinkedIn is one adapter among seven; `content.js` is the same for all of them. Each
     adapter records its `evidence`: Dice was read from live, server-rendered markup
     (2026-10-11); Indeed, Glassdoor and ZipRecruiter answer `curl` with a bot wall, Google
     with an "enable JavaScript" shell, and Handshake with its login page, so those use each
     board's documented hooks (`data-testid` / `data-test` / `data-hook`, employer-link URL
     shapes, CSS-module prefixes) with several fallbacks per field, and are re-read whenever the
     page changes. Nothing is ever clicked or scrolled to make a page render more.
   - **Enabled per board.** LinkedIn is in the manifest outright. The other boards are
     listed under `optional_host_permissions`, and the popup has an **Enable on Indeed** (etc.)
     button per board; until the browser confirms the grant, the content script on that board
     reads nothing and the corner mark only points at the popup. One Chrome caveat, stated
     plainly: a host that is also in `content_scripts.matches` (which these are, since
     injecting on demand would need the `scripting` permission we deliberately do not take)
     is counted at install time, so on a fresh install Chrome reports every board as already
     granted and the prompt only appears when you have restricted the extension's site access
     yourself (chrome://extensions, "Site access"). The per-board switch is still the honest
     place to turn a board off and on.
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
5. Open a job on LinkedIn, Indeed, Glassdoor, Handshake, Dice, Google Jobs (the Jobs tab of
   a Google search) or ZipRecruiter: the badges appear under the title, and each result card
   in the list gets a compact one. Click the extension icon to check any employer by name, or
   to enable a board under **Job boards** if its row says "Enable on ...".
6. Open an application form (e.g. a Greenhouse/Lever/Ashby "Apply" page), the
   **Assisted apply** panel appears bottom-right. Click **Fill this application**,
   review what it filled (highlighted green), attach your resume, and submit yourself.

(When published, it installs from the Chrome Web Store in one click.)

## Compliance
Per the project rules, this extension is strictly the GREEN lane:
- It **reads only the job page you chose to open**, that's you browsing by hand.
- It **cannot be fingerprinted by the sites it runs on**: the manifest lists no
  `web_accessible_resources` (a page can probe those to detect an extension; our logo is an
  inline SVG instead) and asks for one permission, `storage`, plus host permissions for the
  feed and the local app only; the job boards beyond LinkedIn are optional host permissions,
  granted one at a time from the popup.
- On the login-gated boards (Handshake) it reads only what **you** have on screen, in your own
  session; it never logs in, clicks, or scrolls for you.
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
