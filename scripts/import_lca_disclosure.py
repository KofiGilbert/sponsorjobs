"""Maintainer tool: turn DOL LCA disclosure workbooks into the wage bundle the app ships.

    python scripts/import_lca_disclosure.py ~/Downloads/LCA_Disclosure_Data_FY2025_Q*.xlsx
        [--db data/sponsors.db] [--out dist-data/lca] [--min-count 2] [--keep-fys 3]
        [--version 2026-10] [--fy 2026]
    python scripts/import_lca_disclosure.py --download FY2025_Q4 FY2025_Q3   # fetch first

Where the files come from: https://www.dol.gov/agencies/eta/foreign-labor/performance,
"LCA Programs (H-1B, H-1B1, E-3)", one .xlsx per fiscal quarter (about 80 MB each; FY2026
moved to one cumulative file). Download them by hand, or pass --download with release
names: that uses curl_cffi (a dev-only dependency, exactly as scripts/build_perm_bundle.py
does, because DOL's CDN refuses a non-browser TLS handshake) and saves the files in a
scratch directory. The raw files are never committed and never shipped.

What it does, in order:
  1. Streams every file (openpyxl read-only, one row at a time) into one aggregate: per
     employer x fiscal year x normalized title x worksite city/state, the certified H-1B
     filing count, min / median / max annualized wage, the prevailing-wage level split and
     the median prevailing wage per level; plus each employer's H-1B-dependent and
     willful-violator attestation. Cells with fewer than --min-count filings are dropped.
  2. Writes the cells and flags into the local store (data/sponsors.db) through the store
     API (cells are replaced by key, flags never lowered).
  3. Writes dist-data/lca/wages.csv.gz, flags.csv.gz and latest.json from the store, keeping
     the newest --keep-fys fiscal years. Upload the three files to the host behind
     TAILOR_DATA_URL (same as the H-1B snapshot) and copy them into the installer's
     dist-data/lca/ so new installs start with them.

Only the store API touches data/: no raw SQL here.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sourcing.lca import (LCA_PAGE, aggregate_files, lca_release_url,  # noqa: E402
                          write_lca_bundle)
from sourcing.sponsors import SponsorDB  # noqa: E402

DEFAULT_DB = ROOT / "data" / "sponsors.db"
DEFAULT_OUT = ROOT / "dist-data" / "lca"


def download_releases(names, dest_dir, log=print) -> list[Path]:
    """Fetch LCA_Disclosure_Data_<FY2025_Q4>.xlsx releases into `dest_dir` (dev-only curl_cffi)."""
    from curl_cffi import requests   # dev-only import, see the module docstring
    out = []
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        fy, q = name.upper().replace("FY", "").split("_Q")
        url = lca_release_url(int(fy), int(q))
        dest = dest_dir / f"LCA_Disclosure_Data_FY{fy}_Q{q}.xlsx"
        log(f"downloading {url} ...")
        with requests.Session() as s:
            r = s.get(url, impersonate="chrome", timeout=1800, stream=True)
            if r.status_code == 404:
                raise SystemExit(f"{url} is not there; check {LCA_PAGE} for the current name.")
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        log(f"  saved {dest} ({dest.stat().st_size / 1e6:.0f} MB)")
        out.append(dest)
    return out


def import_lca(files, db_path=DEFAULT_DB, out_dir=DEFAULT_OUT, min_count: int = 2,
               keep_fys: int = 3, version: str | None = None, fy_override: int = 0,
               log=print) -> dict:
    files = [Path(f) for f in files]
    version = version or _dt.date.today().strftime("%Y-%m")
    agg = aggregate_files(files, min_count=min_count, fy_override=fy_override, log=log)
    cells, flags = agg.cells(min_count), agg.flags()
    log(f"aggregate: {agg.rows_seen:,} rows read, {agg.rows_kept:,} certified H-1B filings, "
        f"{len(cells):,} cells with at least {min_count} filings, {len(flags):,} employers flagged "
        f"({sum(f['h1b_dependent'] for f in flags):,} H-1B dependent, "
        f"{sum(f['willful_violator'] for f in flags):,} willful violators)")
    db = SponsorDB(db_path)
    try:
        n_cells = db.ingest_lca_wage_rows(cells)
        n_flags = db.ingest_lca_flag_rows(flags)
        now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
        db.set_meta("lca_updated_at", now)
        db.set_meta("lca_bundle_version", version)
        stats = db.lca_stats()
        fys = stats["fiscal_years"]
        keep = fys[-keep_fys:] if keep_fys and fys else fys
        kept_cells = [dict(r) for r in db._conn.execute(
            "SELECT * FROM sponsor_lca_wage WHERE fy >= ? ORDER BY norm_name, fy, title, city, state",
            (keep[0] if keep else 0,))]
        kept_flags = [{"norm_name": r[0], "display_name": r[1] or "", "h1b_dependent": r[2],
                       "willful_violator": r[3], "fy": 0}
                      for r in db._conn.execute(
                          "SELECT norm_name, display_name, h1b_dependent, willful_violator "
                          "FROM sponsor_employer WHERE h1b_dependent>0 OR willful_violator>0 "
                          "ORDER BY norm_name")]
    finally:
        db.close()
    manifest = write_lca_bundle(out_dir, kept_cells, kept_flags, version, keep,
                                sources=[f.name for f in files], min_count=min_count)
    log(f"bundle {version}: {manifest['cells']:,} cells for {manifest['employers']:,} employers, "
        f"fiscal years {keep}, {manifest['bytes']['wages'] / 1e6:.2f} MB wages + "
        f"{manifest['bytes']['flags'] / 1e6:.2f} MB flags -> {Path(out_dir)}")
    return {"cells": n_cells, "flags": n_flags, "manifest": manifest, "stats": stats}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="*", help="LCA_Disclosure_Data_FY20xx_Qn.xlsx files")
    ap.add_argument("--download", nargs="*", metavar="FY2025_Q4",
                    help="release names to fetch first (dev-only curl_cffi)")
    ap.add_argument("--download-dir", help="where --download saves files (default: a temp dir)")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--min-count", type=int, default=2)
    ap.add_argument("--keep-fys", type=int, default=3, help="newest fiscal years in the bundle")
    ap.add_argument("--version", help="bundle version, default YYYY-MM (today)")
    ap.add_argument("--fy", type=int, default=0, help="fiscal year when DECISION_DATE is absent")
    a = ap.parse_args(argv)
    files = list(a.files)
    if a.download:
        dest = a.download_dir or tempfile.mkdtemp(prefix="lca-")
        files += [str(p) for p in download_releases(a.download, dest)]
    if not files:
        ap.error("give at least one .xlsx file or --download")
    import_lca(files, a.db, a.out, min_count=a.min_count, keep_fys=a.keep_fys,
               version=a.version, fy_override=a.fy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
