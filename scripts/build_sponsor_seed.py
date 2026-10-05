"""Distil a sponsors.db into the small, committable seed (seed/sponsors_seed.csv.gz).

The seed is what everything downstream builds from: the hosted feed service rebuilds its
visa-sponsor overlay from it (no 67MB upload), the installer stages its first-run database
from it (packaging/bundle_sponsor_db.py), and a Tailor without H-1B rows loads them from it
(ui/app.py `_load_bundled_h1b`). It ships only employers that carry a real signal (H-1B /
PERM / E-Verify / cap-exempt); everything else would never produce a badge.

    python scripts/build_sponsor_seed.py [path/to/sponsors.db] [out.csv.gz]
"""
from __future__ import annotations

import csv
import gzip
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "sponsors.db"
DEFAULT_OUT = ROOT / "seed" / "sponsors_seed.csv.gz"
COLS = ["norm_name", "display_name", "h1b_approvals", "h1b_last_fy", "h1b_first_fy",
        "naics", "state", "cap_exempt", "e_verify", "perm_certs"]
SIGNAL = "h1b_approvals>0 OR perm_certs>0 OR e_verify>0 OR cap_exempt>0"


def write_seed(db_path, out, where: str = SIGNAL) -> int:
    """Write the seed-layout CSV (gzipped when `out` ends in .gz) from `db_path`. Rows are
    ordered by name so two builds of the same data give the same file (clean git diffs).
    Returns the row count."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(db_path))
    opener = gzip.open if str(out).endswith(".gz") else open
    n = 0
    try:
        with opener(out, "wt", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(COLS)
            for r in src.execute(f"SELECT {','.join(COLS)} FROM sponsor_employer "
                                 f"WHERE {where} ORDER BY norm_name"):
                w.writerow(r)
                n += 1
    finally:
        src.close()
    return n


def main(argv: list[str]) -> int:
    db = Path(argv[0]) if argv else DEFAULT_DB
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    if not db.exists():
        print(f"no {db}: nothing to distil")
        return 1
    n = write_seed(db, out)
    print(f"wrote {n:,} rows -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
