"""Build the per-year H-1B history bundle (approvals AND denials per employer per fiscal year).

    python scripts/build_h1b_history_bundle.py [--years 10] [--db data/sponsors.db]
        [--out seed/h1b_history.csv.gz] [--from-store]

The committed seed (seed/sponsors_seed.csv.gz) carries one approval TOTAL per employer. The
year-by-year picture an applicant wants (is this employer still filing, and how often does
USCIS say no) needs the per-year rows, which USCIS published as one CSV per fiscal year
for FY2009 to FY2023 at

    https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-{fy}.csv

(2 to 5 MB each, an honest urllib download; see sourcing.sponsors.download_h1b_rows). This
script downloads the newest --years of those files, aggregates each into one row per
normalized employer and fiscal year (initial / continuing approvals and denials), writes
them into the local store's sponsor_h1b_year table, and distils the table into
seed/h1b_history.csv.gz, which ships with the app and loads on first run.

FY2024 onward has no file: those years enter the store through
scripts/import_h1b_export.py (the maintainer's Tableau export), which also keeps the
per-year rows and regenerates this bundle, so run that after this when newer exports exist.
--from-store skips the downloads and just rewrites the bundle from what the store holds.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sourcing.sponsors import (H1B_LAST_FILE_FY, H1B_YEAR_COLS, H1BYearUnavailable,  # noqa: E402
                               SponsorDB, download_h1b_rows)

DEFAULT_DB = ROOT / "data" / "sponsors.db"
DEFAULT_OUT = ROOT / "seed" / "h1b_history.csv.gz"


def write_history(db: SponsorDB, out, min_fy: int = 0) -> int:
    """Distil sponsor_h1b_year into the gzipped bundle. Returns the row count."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with gzip.open(out, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(H1B_YEAR_COLS)
        for r in db.h1b_year_rows(min_fy=min_fy):
            w.writerow([r[c] for c in H1B_YEAR_COLS])
            n += 1
    return n


def build(years: int = 10, db_path=DEFAULT_DB, out=DEFAULT_OUT, from_store: bool = False,
          fetch=None, log=print) -> dict:
    db = SponsorDB(db_path)
    try:
        loaded, unavailable = [], []
        if not from_store:
            have = set(db.h1b_year_fiscal_years())
            for fy in range(H1B_LAST_FILE_FY, H1B_LAST_FILE_FY - years, -1):
                if fy in have:
                    log(f"FY{fy}: already in the store, skipped")
                    continue
                try:
                    rows = download_h1b_rows(fy, fetch=fetch)
                except H1BYearUnavailable:
                    unavailable.append(fy)
                    log(f"FY{fy}: USCIS publishes no file")
                    continue
                # Year rows only: the approval TOTALS in sponsor_employer come from the seed /
                # snapshot and must not be added to again (that would double them).
                n = db.ingest_h1b_year_rows(_year_rows(rows))
                loaded.append(fy)
                log(f"FY{fy}: {len(rows):,} rows -> {n:,} employer-years")
        fys = db.h1b_year_fiscal_years()
        min_fy = fys[-years] if len(fys) > years else 0
        n = write_history(db, out, min_fy=min_fy)
    finally:
        db.close()
    size = Path(out).stat().st_size
    log(f"history bundle: {n:,} employer-years, fiscal years {fys[-years:] if fys else []} "
        f"-> {out} ({size / 1e6:.1f} MB)")
    return {"rows": n, "fiscal_years": fys, "loaded": loaded, "unavailable": unavailable,
            "bytes": size}


def _year_rows(rows):
    """USCIS Data Hub rows -> per (normalized employer, fy) rows in H1B_YEAR_COLS shape."""
    import re
    from sourcing.sponsors import _int, canonical_h1b_rows, normalize_employer
    agg: dict[tuple, list[int]] = {}
    for r in canonical_h1b_rows(rows):
        raw = (r.get("Employer") or "").strip()
        fy = _int(r.get("Fiscal Year"))
        if not raw or not fy:
            continue
        vals = [_int(r.get("Initial Approval")), _int(r.get("Initial Denial")),
                _int(r.get("Continuing Approval")), _int(r.get("Continuing Denial"))]
        for variant in re.split(r"\s+dba\s+", raw, flags=re.I):
            norm = normalize_employer(variant)
            if norm:
                y = agg.setdefault((norm, fy), [0, 0, 0, 0])
                for i in range(4):
                    y[i] += vals[i]
    for (norm, fy), v in agg.items():
        yield {"norm_name": norm, "fy": fy, "initial_approvals": v[0], "initial_denials": v[1],
               "continuing_approvals": v[2], "continuing_denials": v[3]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--from-store", action="store_true")
    a = ap.parse_args(argv)
    build(a.years, a.db, a.out, from_store=a.from_store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
