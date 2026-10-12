# Visa sponsorship data: where it comes from and how to keep it fresh

SponsorJobs puts a sponsorship badge next to an employer when public US government records show
that employer has sponsored before. This page explains what each record means, where the
newest H-1B numbers come from now that USCIS no longer publishes a yearly file, and the
quarterly routine a maintainer runs to update everyone's copy.

## 1. The four datasets

| Badge | Source | What a record actually means |
|---|---|---|
| H-1B sponsor | USCIS H-1B Employer Data Hub | USCIS **approved** (or denied) an H-1B petition this employer filed, by fiscal year. These are real sponsorship decisions: the employer paid for and filed a petition, and the government ruled on it. The strongest signal we have. |
| (not shown) | DOL LCA disclosure files | A Labor Condition Application is the **wage filing** an employer submits *before* it can file an H-1B petition. It shows intent and a prevailing-wage promise, not an approved worker. Many LCAs never become petitions. SponsorJobs does not badge on LCA data for that reason. |
| Green card | DOL PERM disclosure data | A **certified PERM** labor certification, the first step of an employment-based green card. Counted per employer. It means the employer has sponsored permanent residence, not just a temporary visa. |
| E-Verify (STEM-OPT) | USCIS E-Verify participating employers | The employer is **enrolled in E-Verify**. An F-1 student on STEM OPT may only work for an E-Verify employer, so this is a precondition, not proof of sponsorship. Enrollment can lapse. |
| Cap-exempt (likely) | derived | A guess from the employer's name and industry (universities, hospitals, research institutes do not go through the H-1B lottery). A heuristic, labelled as one. |

All of it is public, published by the US government, and public domain. SponsorJobs downloads or
bundles it; nothing is scraped and no login is involved.

### How the data gets into SponsorJobs

* **Installer**: `packaging/bundle_sponsor_db.py` builds the first-run database from the
  committed seed `seed/sponsors_seed.csv.gz` (about 340k employers, roughly 105k with H-1B
  approvals), plus the E-Verify bundle in `config/`. A fresh install has badges from the
  first minute.
* **Update visa data** (the button in Jobs, also run on the first visit): downloads the USCIS
  yearly H-1B files that still exist, merges SponsorJobs' quarterly snapshot if the app knows a
  host for it (see section 4), and loads the bundled PERM / E-Verify lists.
* **Import** (Jobs page): any official file the person downloaded themselves: a USCIS H-1B
  export, a DOL PERM `.xlsx`, or an E-Verify list.

## 2. What changed with the H-1B files

USCIS published the Employer Data Hub as one CSV per fiscal year, `h1b_datahubexport-{fy}.csv`,
for FY2009 through FY2023. Those URLs still work. **From FY2024 on there is no file.** The data
is only in a Tableau dashboard at
<https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub>, which a person
exports by hand.

So in SponsorJobs:

* A 404 for FY2024 or later is reported as "unavailable", not as an error. The refresh
  result says which years loaded, which were already in, and which USCIS does not publish.
* The newest years reach every installed SponsorJobs through a **quarterly snapshot** that a
  maintainer builds from the seed plus a dashboard export (sections 3 and 4). The snapshot is
  the authoritative H-1B total for every employer it lists; see the merge rules in section 4.
* If a database somehow has zero H-1B rows, the app loads them from the bundled seed on
  startup and on refresh. An install can no longer show "no H-1B records".

## 3. Quarterly: export the dashboard (about five minutes)

USCIS updates the dashboard roughly each quarter. Do this once per update.

1. Open <https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub> in a
   desktop browser. Wait for the Tableau dashboard to load fully.
2. In the dashboard, switch to the **Crosstab View** tab (the table view, not the map).
3. Set the filters to the data you want:
   * **Fiscal Year**: pick the new year (or years) you are importing. Leave the others
     unticked so the export stays small.
   * **State**, **NAICS**, **Employer**: leave on *All*.
4. Make sure the table shows these columns: Fiscal Year, Employer (Petitioner) Name,
   Initial Approval, Initial Denial, Continuing Approval, Continuing Denial, NAICS, Tax ID,
   State, City, ZIP. If the crosstab hides one, use the dashboard's column menu to show it.
   The import tolerates renamed columns ("Initial Approvals", "Petitioner Name", and so on)
   and different casing, but it does need the employer name and the two approval columns.
5. Click the **Download** icon in the Tableau toolbar at the bottom of the dashboard.
6. Choose **Crosstab**. In the dialog, select the crosstab sheet, choose **CSV** as the
   format, and click **Download**. (Excel also works; the import reads `.xlsx` too.)
7. Save the file somewhere you will find it, for example `~/Downloads/h1b_fy2026.csv`.
   If Tableau splits a large year into several files, keep all of them; the import takes
   more than one file.
8. Open the file once and confirm you see one employer per row with numbers in the approval
   columns. If the first row is a title line rather than headers, delete it and save.

If the dashboard layout changes, the import script prints how it understood each column
and stops if it cannot find the employer or approval columns, so you will know.

## 4. Quarterly: import, commit, upload

From the repo root, with the export file(s) you just saved:

```
python scripts/import_h1b_export.py ~/Downloads/h1b_fy2026.csv --version 2026-09
```

What you will see, in order:

1. It opens `data/sponsors.db` (your local store). If it has no H-1B rows, it merges the
   committed seed first so older years are kept.
2. For each file: how many rows, how each column was understood, and per fiscal year how
   many employers were ingested. A year that is already counted is skipped (adding it twice
   would double the numbers). Pass `--force` only after a deliberate reset. If the export
   has no Fiscal Year column, pass `--fy 2026`.
3. It rewrites `seed/sponsors_seed.csv.gz`. **Commit this file**: it is what the installer
   and the hosted feed are built from.
4. It writes `dist-data/h1b/latest.csv.gz` and `dist-data/h1b/latest.json`. These two files
   are the snapshot. `latest.json` looks like:

   ```json
   {"version": "2026-09", "fiscal_years": [2020, 2021, 2022, 2023, 2024, 2025, 2026], "rows": 350000}
   ```

Then publish the snapshot:

5. Upload both files to the data host so they are reachable at
   `{TAILOR_DATA_URL}/h1b/latest.csv.gz` and `{TAILOR_DATA_URL}/h1b/latest.json`. Any static
   host works (a bucket, Render static site, GitHub Pages). Keep the paths exactly as shown.
6. Installed apps that have `TAILOR_DATA_URL` set (for example
   `TAILOR_DATA_URL=https://tailor.example/data`) fetch `latest.json` on their next
   "Update visa data", skip it if that version is already in, otherwise download the CSV
   and merge it. An unreachable host is a quiet skip, never an error.
7. Rebuild the installer when convenient (`python packaging/bundle_sponsor_db.py`, then the
   usual packaging steps) so new installs start with the new year too.

### Rules the merge follows

* **The snapshot is authoritative for what it lists.** It is built from the committed seed
  plus the maintainer's exports, so it already contains every fiscal year it declares. For
  each employer it lists with an H-1B count, the app SETS `h1b_approvals`, `h1b_first_fy`
  and `h1b_last_fy` from the snapshot (it does not keep the larger local number), and it
  SETS `h1b_fys_ingested` to the snapshot's `fiscal_years`. Taking the MAX instead produced
  hybrid totals: a store that had added FY2019 on its own kept its larger number, missed the
  snapshot's newer years, and marked them ingested so nothing could ever add them.
* **Local years outside the snapshot's span are re-added, not lost.** Because
  `h1b_fys_ingested` becomes exactly the snapshot's span, a year the install downloaded
  itself (say FY2019 when the snapshot declares FY2020 to 2026) is no longer marked
  ingested; the next "Update visa data" downloads it again and adds it on top of the
  snapshot totals.
* **Employers the snapshot does not list are untouched**, and a listed row with no H-1B
  count makes no H-1B claim (the local H-1B columns stay as they are).
* **Never lower PERM or E-Verify.** `perm_certs`, `e_verify` and `cap_exempt` take the
  larger value; names, NAICS and state fill only where blank.
* **Never double count.** The store records which fiscal years are in
  (`h1b_fys_ingested`) and the yearly import skips those. The snapshot carries its own
  version (`h1b_snapshot_version`) and is applied once.
* **The bundled seed and yearly USCIS files still merge with MAX** (`merge_aggregated_rows`
  / `ingest_h1b_rows`); only the snapshot path replaces.

## 5. Checking it worked

* In the app, Jobs > the visa data line shows the fiscal-year span ("H-1B: FY2020 to 2026")
  and `GET /api/sponsors/status` returns `h1b_fiscal_years`, `h1b_source` and
  `h1b_snapshot_version`.
* `python packaging/bundle_sponsor_db.py` prints the H-1B / PERM / E-Verify counts of what
  it staged and which source it used (the seed, or a local database that has more H-1B
  employers).

## 6. Per-employer history (approvals AND denials by year, PERM by year, three flags)

The badge says "has sponsored". The history panel under a job (Jobs > open a role > "About
<employer>") says *how it has gone*: a column per fiscal year with approvals in green and
denials stacked on top, the PERM certifications of that year under the column, and chips
for the three flags. `GET /api/sponsors/<name>/history` returns the same thing
(`years[]` with `initial_approvals`, `initial_denials`, `continuing_approvals`,
`continuing_denials`, `approvals`, `denials`, `perm`; `approvals_5y`, `denials_5y`,
`denial_rate_5y`; `flags`).

| Piece | Source | Store table | Bundle that ships it |
|---|---|---|---|
| H-1B approvals and denials per fiscal year | USCIS H-1B Employer Data Hub yearly CSVs (`h1b_datahubexport-{fy}.csv`, FY2009 to FY2023, still downloadable) and the maintainer's dashboard export for newer years | `sponsor_h1b_year` | `seed/h1b_history.csv.gz` (committed, FY2014 to FY2023, 474,990 employer-years, 3.0 MB; loads in the background on first run) |
| PERM certifications per fiscal year of the decision | DOL PERM disclosure files (`PERM_Disclosure_Data_FY20xx_Q4.xlsx`; the FY2025 file renamed its columns to `emp_business_name`, `case_status`, `decision_date`, which both the bundle script and the import accept) | `sponsor_perm_year` | `config/perm_by_year.csv.gz` (committed, FY2024 and FY2025 decisions, 54,934 employer-years, 452 KB) |
| H-1B dependent, willful violator | the employer's own attestations on every DOL LCA (`H_1B_DEPENDENT`, `WILLFUL_VIOLATOR`: Yes / No / N/A) | `sponsor_employer.h1b_dependent`, `.willful_violator` | `dist-data/lca/flags.csv.gz` (see section 7) |
| Cap-exempt (likely) | the existing name and industry heuristic | `sponsor_employer.cap_exempt` | the seed |

The per-year rows are SET per employer and year, never added, so a year imported twice (or
the same year arriving from the seed and a download) cannot double. The approval TOTAL on
the badge is untouched and still comes from the seed / snapshot.

Maintainer routine:

```
python scripts/build_h1b_history_bundle.py --years 10        # downloads the USCIS yearly files it lacks,
                                                             # writes seed/h1b_history.csv.gz; commit it
python scripts/import_h1b_export.py ~/Downloads/h1b_fy2026.csv --version 2026-09
                                                             # (unchanged routine) now also keeps the
                                                             # export's per-year rows and rewrites the history bundle
python scripts/build_perm_bundle.py FY2025_Q4 FY2024_Q4      # totals from the FIRST release as before,
                                                             # plus config/perm_by_year.csv.gz from all of them
```

Observed on 2026-10-11: the USCIS FY2023 file holds 33,332 rows against 55,000 to 60,000 for
every other year, so FY2023 looks partial in the chart (Google shows 2,467 approvals in
FY2023 against 8,445 in FY2022). The dashboard export is the way to complete it.

The extension's sponsor index (`scripts/build_sponsor_index.py`) now appends three compact
facts to each record: approvals over the newest five fiscal years, the denial rate over them
in whole percent, and a flag bit set (1 = H-1B dependent, 2 = willful violator). Trailing
zeros are dropped, so the format stays additive; `extension/sponsor_core.js` decodes them as
`h1b_5y`, `denial_pct`, `h1b_dependent`, `willful_violator` (not shown in the popup yet).

## 7. LCA wages

**What it answers.** "What did this employer actually pay for this title in this city?"
Under a job, the About strip says: *This employer paid $158k to $340k (median $223k) for
Software Engineer in Mountain View, CA, 161 filings, mostly Level II. DOL LCA filings,
FY2025.* `GET /api/sponsors/<name>/wages?title=&city=&state=` (or `&location=Austin, TX`)
returns the numbers; `SponsorDB.wages()` narrows from the exact title in the exact city, to
the title anywhere in the state, the title anywhere, the title FAMILY (seniority and level
words stripped: "Senior Software Engineer II" and "Staff Software Engineer" are both
"software engineer") in the city, in the state, anywhere, and finally the employer as a whole;
`scope` and `scope_note` say which level answered.

**Source.** DOL OFLC "LCA Disclosure Data", one Excel workbook per fiscal quarter, listed at
<https://www.dol.gov/agencies/eta/foreign-labor/performance> under "LCA Programs (H-1B,
H-1B1, E-3)". URLs and sizes checked on 2026-10-11:

| Release | URL | Size |
|---|---|---|
| FY2024 Q1 to Q4 | `https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2024_Q{1,2,3,4}.xlsx` | about 80 MB each |
| FY2025 Q1 to Q4 | `https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2025_Q{1,2,3,4}.xlsx` | 79 to 88 MB each |
| FY2026 Q3 (Oct 2025 to Jun 2026, one cumulative file) | `https://www.dol.gov/media/LCA_Disclosure_Data_FY2026_Q3.xlsx` | 252 MB |
| Record layout | `.../pdfs/FY26Q3/LCA_Record_Layout_FY2026_Q3.pdf` (and `LCA_Record_Layout_FY2025_Q4.pdf`) | |

DOL publishes no CSV. Each FY2024 / FY2025 quarterly file holds that quarter's
determinations only (FY2025 Q4 = decisions dated 2025-07-01 to 2025-09-30), so one fiscal
year is four files; FY2026 switched to one growing file. DOL's CDN refuses a non-browser TLS
handshake, so `--download` uses `curl_cffi` exactly as `scripts/build_perm_bundle.py` does
(dev-only; the shipped app never downloads these). The raw files are never committed.

**The real columns** (FY2025 Q4 file, 98 columns, 563,690 sheet rows of which 118,580 are
real and the rest blank trailing rows): the importer reads `CASE_STATUS` (Certified,
Certified - Withdrawn, Denied, Withdrawn), `DECISION_DATE`, `VISA_CLASS` (H-1B, E-3
Australian, H-1B1 Chile, H-1B1 Singapore), `JOB_TITLE`, `SOC_CODE`, `EMPLOYER_NAME`,
`WORKSITE_CITY`, `WORKSITE_STATE`, `WAGE_RATE_OF_PAY_FROM`, `WAGE_RATE_OF_PAY_TO`,
`WAGE_UNIT_OF_PAY` (Year, Hour, Month, Week, Bi-Weekly), `PREVAILING_WAGE`,
`PW_UNIT_OF_PAY`, `PW_WAGE_LEVEL` (I, II, III, IV, N/A), `H_1B_DEPENDENT` and
`WILLFUL_VIOLATOR` (Yes, No, N/A). The record layout PDF spells the dependent column
`H-1B_DEPENDENT`; the workbook spells it `H_1B_DEPENDENT`; both are accepted. Full header,
in order: CASE_NUMBER, CASE_STATUS, RECEIVED_DATE, DECISION_DATE, ORIGINAL_CERT_DATE,
VISA_CLASS, JOB_TITLE, SOC_CODE, SOC_TITLE, FULL_TIME_POSITION, BEGIN_DATE, END_DATE,
TOTAL_WORKER_POSITIONS, NEW_EMPLOYMENT, CONTINUED_EMPLOYMENT, CHANGE_PREVIOUS_EMPLOYMENT,
NEW_CONCURRENT_EMPLOYMENT, CHANGE_EMPLOYER, AMENDED_PETITION, EMPLOYER_NAME, TRADE_NAME_DBA,
EMPLOYER_ADDRESS1, EMPLOYER_ADDRESS2, EMPLOYER_CITY, EMPLOYER_STATE, EMPLOYER_POSTAL_CODE,
EMPLOYER_COUNTRY, EMPLOYER_PROVINCE, EMPLOYER_PHONE, EMPLOYER_PHONE_EXT, EMPLOYER_FEIN,
NAICS_CODE, 14 EMPLOYER_POC_* columns, AGENT_REPRESENTING_EMPLOYER, 12 AGENT_ATTORNEY_*
columns, LAWFIRM_NAME_BUSINESS_NAME, LAWFIRM_BUSINESS_FEIN, STATE_OF_HIGHEST_COURT,
NAME_OF_HIGHEST_STATE_COURT, WORKSITE_WORKERS, SECONDARY_ENTITY,
SECONDARY_ENTITY_BUSINESS_NAME, WORKSITE_ADDRESS1, WORKSITE_ADDRESS2, WORKSITE_CITY,
WORKSITE_COUNTY, WORKSITE_STATE, WORKSITE_POSTAL_CODE, WAGE_RATE_OF_PAY_FROM,
WAGE_RATE_OF_PAY_TO, WAGE_UNIT_OF_PAY, PREVAILING_WAGE, PW_UNIT_OF_PAY, PW_TRACKING_NUMBER,
PW_WAGE_LEVEL, PW_OES_YEAR, PW_OTHER_SOURCE, PW_OTHER_YEAR, PW_SURVEY_PUBLISHER,
PW_SURVEY_NAME, TOTAL_WORKSITE_LOCATIONS, AGREE_TO_LC_STATEMENT, H_1B_DEPENDENT,
WILLFUL_VIOLATOR, SUPPORT_H1B, STATUTORY_BASIS, APPENDIX_A_ATTACHED, PUBLIC_DISCLOSURE,
PREPARER_LAST_NAME, PREPARER_FIRST_NAME, PREPARER_MIDDLE_INITIAL, PREPARER_BUSINESS_NAME,
PREPARER_EMAIL.

**How it is aggregated** (`sourcing/lca.py`). The workbook is streamed row by row
(openpyxl read-only; 118k rows in about 35 seconds, memory flat). Certified H-1B rows only
(E-3 and H-1B1 dropped, withdrawn and denied dropped). The wage is `WAGE_RATE_OF_PAY_FROM`
annualized by its unit (Hour x 2080, Week x 52, Bi-Weekly x 26, Month x 12) and kept
between $15,000 and $2,500,000. One cell per employer x fiscal year of the decision x
normalized title x worksite city x state: count, min / median / max wage, filings per
prevailing-wage level, and the median prevailing wage per level (`pw1`..`pw4`, the
employer's own level thresholds, which the offer check uses). Cells with fewer than two
filings are dropped. Flags are per employer: any "Yes" wins.

**Bundle.** `dist-data/lca/wages.csv.gz` + `flags.csv.gz` + `latest.json`
(`{version, fiscal_years, cells, employers, flag_rows, min_count, sources, bytes, ...}`),
committed (the `.gitignore` keeps `dist-data/` out except this folder) and loaded on first
run like the PERM bundle; also fetchable from `{TAILOR_DATA_URL}/lca/` once per version.
Built on 2026-10-11 from the nine real files above (FY2024 Q1 to Q4, FY2025 Q1 to Q4 and the
cumulative FY2026 Q3, 1,595,085 sheet rows, 1,519,548 certified H-1B filings with a wage):
**181,709 cells for 30,903 employers over FY2024 to FY2026, 5,814 flagged employers (5,791
H-1B dependent, 278 willful violators), 4.78 MB wages + 67 KB flags gzipped**, inside the
"tens of MB" cap. The whole run took about ten minutes on a laptop (36 seconds per 100k
rows, memory flat at about 650 MB for the in-memory aggregate). Each fiscal year's quarters
MUST be imported in the same run: a cell is replaced by key, so importing FY2025 Q4 alone
after Q1 to Q3 would overwrite the year's cells with the quarter's.

Maintainer routine (download the quarters you want from the performance page first, or
let the script fetch them):

```
python scripts/import_lca_disclosure.py ~/Downloads/LCA_Disclosure_Data_FY202[456]_Q*.xlsx --version 2026-10
python scripts/import_lca_disclosure.py --download FY2024_Q1 FY2024_Q2 FY2024_Q3 FY2024_Q4 \
    FY2025_Q1 FY2025_Q2 FY2025_Q3 FY2025_Q4 FY2026_Q3 --download-dir /tmp/lca --version 2026-10
```

(`--download` builds the FY2024 and FY2025 URLs under `.../ETA/oflc/pdfs/`; the FY2026
cumulative file lives at `https://www.dol.gov/media/LCA_Disclosure_Data_FY2026_Q3.xlsx`, see
`lca_release_url`.) The downloads are about 1 GB in all and are never committed.

It streams each file, writes the cells and flags into `data/sponsors.db` (cells replaced
by key, flags never lowered), and rewrites `dist-data/lca/` from the store keeping the
newest `--keep-fys` (3) fiscal years. Commit the three bundle files and, if a data host is
set, upload them next to the H-1B snapshot.

## 8. Lottery odds by wage level and the offer check

`sourcing/lottery.py` holds the current rule and the arithmetic, with sources in the
docstring: DHS's weighted-selection final rule (Federal Register 2025-23853, published
2025-12-29, effective 2026-02-27; Level IV registrations enter 4 times, III 3, II 2, I 1),
USCIS's published counts (FY2026: 343,981 eligible registrations, 120,141 selected; FY2027:
211,600 properly submitted registrations, selection complete 2026-03-31, selection count and
per-level split not published as of 2026-10-11), and DHS's own level mix from the rule's
impact analysis (28 / 55 / 12 / 5 percent). `lottery_odds(level, registrations=None,
selections=None)` solves for the per-entry draw probability at which expected selections
match the selection count and returns each level's probability (with the defaults: Level I
about 37%, II 60%, III 75%, IV 84%, and Level I about 18% of selections against 28% of
registrations). Override `selections=` when USCIS publishes the FY2027 number, and re-check
the USCIS registration page each cap season.

**Offer check** (Jobs > open a role > About > "Offer check"): type the offer, confirm the
title, city and state (pre-filled from the job), and the panel says where the offer sits in
this employer's filings for that title and place, the likely prevailing-wage level (the
highest level whose median prevailing wage in those filings the offer meets; when the offer
is under every level the employer files, "At most Level II" with the odds at that ceiling
instead of silence), the odds at that level with all four levels beside it, and three
negotiation points: the median and
range, the wage that would reach the next level and what it does to the lottery entries,
and the level the employer usually files at. `POST /api/sponsors/offer-check` with
`{company, title, city, state | location, offer}` ("125k" works) returns the same. Every
answer carries the one-line note that it is an estimate from public filings, not legal
advice.
