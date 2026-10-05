"""Distill a downloaded E-Verify employer file into the app's STEM-OPT list.

    python scripts/build_everify_bundle.py <e-verify-file.csv|.xlsx>

Input: an official E-Verify *participating-employers* file — the public 2018 baseline
(catalogs the enrolled employers most students target, since enrollment is sticky) or a
current FOIA release. USCIS publishes no live download, so you obtain the file once and
run this; the app then auto-loads config/everify_employers.csv.gz on startup and tags jobs
with STEM-OPT badges. Swap in a fresher file anytime (re-run, or use the Import button).

The distilled list is ~500k employers, so it's written GZIPPED (~3.6 MB vs ~13 MB raw) and
committed with the app — it ships as part of the product, no per-user download needed.
"""
from __future__ import annotations

import csv
import gzip
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sourcing.sponsors import (_find_employer_col, normalize_employer,  # noqa: E402
                               parse_tabular_file)

OUT = ROOT / "config" / "everify_employers.csv.gz"


def main(src: str) -> None:
    rows = parse_tabular_file(src)
    if not rows:
        raise SystemExit(f"no rows read from {src}")
    col = _find_employer_col(rows[0].keys())
    if not col:
        raise SystemExit(f"couldn't find an employer-name column in: {list(rows[0].keys())}")
    print(f"read {len(rows):,} rows; employer column = {col!r}")

    seen: dict[str, str] = {}   # normalized -> display name (dedup, keep DBA parts)
    for r in rows:
        emp = str(r.get(col) or "").strip()
        if not emp:
            continue
        import re
        for variant in re.split(r"\s+dba\s+", emp, flags=re.I):
            norm = normalize_employer(variant)
            if norm and norm not in seen:
                seen[norm] = variant.strip().title()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["EMPLOYER_NAME"])
        for disp in seen.values():
            w.writerow([disp])
    print(f"{len(seen):,} unique employers -> {OUT} ({OUT.stat().st_size/1024/1024:.1f} MB gzipped)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python scripts/build_everify_bundle.py <e-verify-file.csv|.xlsx>")
    main(sys.argv[1])
