"""Refresh the bundled green-card sponsor list from official DOL PERM disclosure data.

Run this ~quarterly (whenever DOL posts a new PERM file). It distills the ~80MB DOL
xlsx into config/perm_sponsors.csv (~20k unique certified employers, <1MB) which ships
with the app and auto-loads instantly — so end-users get green-card badges without any
download or manual import.

    python scripts/build_perm_bundle.py [FY2025_Q4 [FY2024_Q4 ...]] [--file path.xlsx ...]

With several releases (or local --file workbooks) it also writes the per-year list
config/perm_by_year.csv.gz (EMPLOYER_NAME, FY, PERM_CERTS: certified cases by the fiscal
year of the decision date), which feeds the employer history chart. The totals file keeps
its layout and is built from the FIRST release named, so an existing seed stays consistent.

Public open-data download. DOL's CDN blocks non-browser TLS, so we use curl_cffi to
present a standard browser handshake — this is a plain download of a file published for
public download: no login, captcha, proxy, or ToS bypass (CLAUDE.md §6/§7). curl_cffi
is a dev/build dependency only; the shipped app never downloads PERM.
"""
from __future__ import annotations

import csv
import datetime as _dt
import gzip
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "config" / "perm_sponsors.csv"
OUT_YEARS = ROOT / "config" / "perm_by_year.csv.gz"
BASE = "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/PERM_Disclosure_Data_{fy}.xlsx"


def _fiscal_year(d) -> int:
    if isinstance(d, _dt.datetime):
        d = d.date()
    if isinstance(d, _dt.date):
        return d.year + (1 if d.month >= 10 else 0)
    s = str(d or "").strip()[:10]
    try:
        d = _dt.date.fromisoformat(s)
    except ValueError:
        return 0
    return d.year + (1 if d.month >= 10 else 0)


def scan(source, label: str, agg, years: dict, log=print) -> None:
    """Stream one PERM workbook (bytes or a path) into the employer totals (when `agg` is a
    dict) and the per-year counts."""
    import openpyxl
    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip().lower() if h is not None else "" for h in next(rows)]

    def col(*names):
        for n in names:
            if n.lower() in header:
                return header.index(n.lower())
        return -1

    ci_emp = col("employer_name", "employer_legal_business_name", "emp_business_name")
    ci_st = col("case_status", "status")
    ci_dt = col("decision_date", "case_received_date", "received_date")
    if ci_emp < 0 or ci_st < 0:
        raise SystemExit(f"couldn't find employer/status columns in {header}")

    total = kept = 0
    for row in rows:
        total += 1
        if ci_emp >= len(row) or ci_st >= len(row):
            continue
        emp = str(row[ci_emp] or "").strip()
        if emp and "certif" in str(row[ci_st] or "").strip().lower():
            kept += 1
            if agg is not None:
                agg[emp.upper()] = agg.get(emp.upper(), 0) + 1
            fy = _fiscal_year(row[ci_dt]) if 0 <= ci_dt < len(row) else 0
            if fy:
                years[(emp.upper(), fy)] = years.get((emp.upper(), fy), 0) + 1
    wb.close()
    log(f"{label}: scanned={total:,} certified={kept:,}")


def write_outputs(agg: dict, years: dict, source_fy: str, out=OUT, out_years=OUT_YEARS,
                  log=print) -> None:
    if agg:
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["EMPLOYER_NAME", "PERM_CERTS", "SOURCE_FY"])
            for emp, n in sorted(agg.items(), key=lambda x: -x[1]):
                w.writerow([emp, n, source_fy])
        log(f"employers={len(agg):,} -> {out} ({Path(out).stat().st_size / 1024:.0f} KB)")
    if years:
        with gzip.open(out_years, "wt", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["EMPLOYER_NAME", "FY", "PERM_CERTS"])
            for (emp, fy), n in sorted(years.items()):
                w.writerow([emp, fy, n])
        fys = sorted({fy for _, fy in years})
        log(f"employer-years={len(years):,} fiscal years {fys} -> {out_years} "
            f"({Path(out_years).stat().st_size / 1024:.0f} KB)")


def main(releases, files=(), log=print) -> None:
    agg: dict[str, int] = {}
    years: dict[tuple, int] = {}
    sources = []
    first = True
    for fy in releases:
        from curl_cffi import requests   # dev-only import
        url = BASE.format(fy=fy)
        log(f"downloading {url} ...")
        r = requests.get(url, impersonate="chrome", timeout=600)
        r.raise_for_status()
        log(f"  got {len(r.content):,} bytes")
        scan(io.BytesIO(r.content), fy, agg if first else None, years, log)
        sources.append(fy)
        first = False
    for f in files:
        scan(str(f), Path(f).name, agg if first else None, years, log)
        sources.append(Path(f).stem.replace("PERM_Disclosure_Data_", ""))
        first = False
    if not sources:
        raise SystemExit("name at least one release (FY2025_Q4) or --file")
    write_outputs(agg, years, sources[0], log=log)


if __name__ == "__main__":
    args = sys.argv[1:]
    local = []
    while "--file" in args:
        i = args.index("--file")
        local.append(args[i + 1])
        del args[i:i + 2]
    main(args or ([] if local else ["FY2024_Q4"]), local)
