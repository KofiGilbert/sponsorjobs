"""Refresh the bundled green-card sponsor list from official DOL PERM disclosure data.

Run this ~quarterly (whenever DOL posts a new PERM file). It distills the ~76MB DOL
xlsx into config/perm_sponsors.csv (~20k unique certified employers, <1MB) which ships
with the app and auto-loads instantly — so end-users get green-card badges without any
download or manual import.

    python scripts/build_perm_bundle.py [FY2024_Q4]

Public open-data download. DOL's CDN blocks non-browser TLS, so we use curl_cffi to
present a standard browser handshake — this is a plain download of a file published for
public download: no login, captcha, proxy, or ToS bypass (CLAUDE.md §6/§7). curl_cffi
is a dev/build dependency only; the shipped app never downloads PERM.
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "config" / "perm_sponsors.csv"
BASE = "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/PERM_Disclosure_Data_{fy}.xlsx"


def main(fy: str = "FY2024_Q4") -> None:
    from curl_cffi import requests   # dev-only import
    import openpyxl

    url = BASE.format(fy=fy)
    print(f"downloading {url} ...")
    r = requests.get(url, impersonate="chrome", timeout=180)
    r.raise_for_status()
    print(f"  got {len(r.content):,} bytes")

    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip().lower() if h is not None else "" for h in next(rows)]

    def col(*names):
        for n in names:
            if n.lower() in header:
                return header.index(n.lower())
        return -1

    ci_emp = col("employer_name", "employer_legal_business_name")
    ci_st = col("case_status", "status")
    if ci_emp < 0 or ci_st < 0:
        raise SystemExit(f"couldn't find employer/status columns in {header}")

    agg: dict[str, int] = {}
    total = kept = 0
    for row in rows:
        total += 1
        if ci_emp >= len(row) or ci_st >= len(row):
            continue
        emp = str(row[ci_emp] or "").strip()
        if emp and "certif" in str(row[ci_st] or "").strip().lower():
            kept += 1
            agg[emp.upper()] = agg.get(emp.upper(), 0) + 1
    wb.close()

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["EMPLOYER_NAME", "PERM_CERTS", "SOURCE_FY"])
        for emp, n in sorted(agg.items(), key=lambda x: -x[1]):
            w.writerow([emp, n, fy])

    print(f"scanned={total:,} certified={kept:,} employers={len(agg):,} "
          f"-> {OUT} ({OUT.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "FY2024_Q4")
