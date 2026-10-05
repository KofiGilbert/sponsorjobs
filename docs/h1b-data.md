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
