"""DOL LCA disclosure data: streaming wage aggregation per employer, title and worksite.

Source (official, public domain): the Office of Foreign Labor Certification publishes one
"LCA Disclosure Data" workbook per fiscal quarter at
https://www.dol.gov/agencies/eta/foreign-labor/performance under "LCA Programs (H-1B,
H-1B1, E-3)". File names and sizes seen on 2026-10-11:

    https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2024_Q{1..4}.xlsx  (~80 MB each)
    https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2025_Q{1..4}.xlsx  (~80 MB each)
    https://www.dol.gov/media/LCA_Disclosure_Data_FY2026_Q3.xlsx                                 (252 MB, Oct 2025 to Jun 2026)

DOL publishes xlsx only (no CSV), so the reader streams the workbook with openpyxl's
read-only mode: one row at a time, never the whole file in memory. Each row is one LCA
(form ETA-9035): the employer, the job title and SOC code, the FIRST worksite's city and
state, the offered wage (WAGE_RATE_OF_PAY_FROM / _TO + WAGE_UNIT_OF_PAY), the prevailing
wage and its OEWS level (PW_WAGE_LEVEL I..IV), and the employer's H-1B-dependent /
willful-violator attestations. The record layout PDF next to each file describes the 98
columns; the real header spells the dependent flag H_1B_DEPENDENT (the PDF says
H-1B_DEPENDENT) and the flags read "Yes" / "No" / "N/A", so both spellings are accepted.

What the aggregate keeps, and why: ONE cell per employer x fiscal year x normalized job
title x worksite city x state, with the filing count, the min / median / max annualized
offered wage, how many filings sat at each prevailing-wage level, and the median prevailing
wage at each level (the offer check uses those as the employer's own level thresholds).
Cells with fewer than `min_count` filings are dropped (one filing is one person's pay, which
is not a statistic, and it keeps the bundle small). Only CERTIFIED H-1B rows count (a
withdrawn or denied LCA never paid anyone), and E-3 / H-1B1 rows are left out so the numbers
describe the H-1B track.

The raw files are never committed. The maintainer runs scripts/import_lca_disclosure.py
on files downloaded from DOL; the app ships the aggregate (dist-data/lca/).
"""

from __future__ import annotations

import csv
import datetime as _dt
import gzip
import io
import json
import re
from collections import Counter
from pathlib import Path

from sourcing.sponsors import normalize_employer

# Columns the aggregate needs, with the header spellings DOL has used. Everything else in
# the 98-column row is skipped at read time.
_COLS = {
    "status": ("CASE_STATUS",),
    "visa": ("VISA_CLASS",),
    "title": ("JOB_TITLE",),
    "soc": ("SOC_CODE",),
    "employer": ("EMPLOYER_NAME",),
    "city": ("WORKSITE_CITY",),
    "state": ("WORKSITE_STATE",),
    "wage_from": ("WAGE_RATE_OF_PAY_FROM",),
    "wage_to": ("WAGE_RATE_OF_PAY_TO",),
    "wage_unit": ("WAGE_UNIT_OF_PAY",),
    "pw": ("PREVAILING_WAGE",),
    "pw_unit": ("PW_UNIT_OF_PAY",),
    "pw_level": ("PW_WAGE_LEVEL",),
    "dependent": ("H_1B_DEPENDENT", "H-1B_DEPENDENT", "H1B_DEPENDENT"),
    "violator": ("WILLFUL_VIOLATOR",),
    "decided": ("DECISION_DATE",),
}
REQUIRED = ("employer", "title", "status", "wage_from", "wage_unit")

# Hours / weeks the DOL itself uses to annualize an hourly or weekly wage on the LCA.
_UNIT_FACTOR = {"year": 1.0, "hour": 2080.0, "week": 52.0, "bi-weekly": 26.0, "biweekly": 26.0,
                "month": 12.0}
_LEVELS = {"I": 1, "II": 2, "III": 3, "IV": 4, "1": 1, "2": 2, "3": 3, "4": 4}

# Sanity bounds on an annualized wage: a unit mistake on the form ("Hour" for a yearly
# salary) would otherwise put a $300,000,000 filing at the top of a range.
WAGE_MIN, WAGE_MAX = 15_000, 2_500_000

WAGE_COLS = ["norm_name", "fy", "title", "family", "city", "state", "soc_code", "n",
             "wage_min", "wage_median", "wage_max", "lvl1", "lvl2", "lvl3", "lvl4",
             "pw1", "pw2", "pw3", "pw4"]
WAGE_INT_COLS = {"fy", "n", "wage_min", "wage_median", "wage_max", "lvl1", "lvl2", "lvl3",
                 "lvl4", "pw1", "pw2", "pw3", "pw4"}
FLAG_COLS = ["norm_name", "display_name", "h1b_dependent", "willful_violator", "fy"]

LCA_URL = "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY{fy}_Q{q}.xlsx"
LCA_PAGE = "https://www.dol.gov/agencies/eta/foreign-labor/performance"


def lca_release_url(fy: int, quarter: int) -> str:
    """The quarterly workbook's URL for FY2024 and FY2025 releases. (FY2026 moved to
    /media/LCA_Disclosure_Data_FY2026_Q3.xlsx; check the performance page for a new year.)"""
    return LCA_URL.format(fy=int(fy), q=int(quarter))


# ---------------------------------------------------------------- titles

_SENIORITY = re.compile(
    r"\b(sr|senior|jr|junior|lead|principal|staff|chief|head|associate|assistant|entry level|"
    r"entry|mid level|intern|trainee|fellow|distinguished|executive|vp|vice president|director|"
    r"manager of|level|lvl|grade|tier|i{1,3}v?|iv|v|vi|[1-9])\b")
_NOISE = re.compile(r"\((?:[^)]*)\)|\[[^\]]*\]|\b(?:ft|pt|full time|part time|remote|hybrid)\b")


def normalize_title(title: str) -> str:
    """Lowercase, drop punctuation and bracketed asides, collapse spaces: 'Software Engineer
    II (Backend)' -> 'software engineer ii'. The exact-title key."""
    s = (title or "").lower().replace("&", " and ")
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(s.split())


def title_family(title: str) -> str:
    """The title without seniority and level words: 'senior software engineer ii' ->
    'software engineer'. Groups a job's title with the employer's adjacent levels when the
    exact title has too few filings. Falls back to the normalized title when stripping
    would leave nothing."""
    norm = normalize_title(title)
    fam = " ".join(t for t in _SENIORITY.sub(" ", norm).split() if t)
    fam = re.sub(r"\b(of|and|the)\b\s*$", "", fam).strip()
    return fam or norm


# ---------------------------------------------------------------- reading

def _cell(v) -> str:
    return "" if v is None else str(v).strip()


def _annual(amount, unit) -> int:
    """An annualized wage in whole dollars, or 0 when the amount or unit is unreadable or
    outside the sanity bounds."""
    try:
        x = float(str(amount).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return 0
    factor = _UNIT_FACTOR.get(_cell(unit).lower())
    if not factor or x <= 0:
        return 0
    val = int(round(x * factor))
    return val if WAGE_MIN <= val <= WAGE_MAX else 0


def fiscal_year_of(d) -> int:
    """US federal fiscal year of a date (or an ISO / 'MM/DD/YYYY' string); 0 if unreadable."""
    if d is None or d == "":
        return 0
    if isinstance(d, _dt.datetime):
        d = d.date()
    if not isinstance(d, _dt.date):
        s = str(d).strip()[:10]
        try:
            d = _dt.date.fromisoformat(s)
        except ValueError:
            m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
            if not m:
                return 0
            d = _dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    return d.year + (1 if d.month >= 10 else 0)


def header_index(header) -> dict:
    """{our field: column index} for the header row of a disclosure file (any year's
    spelling). Raises ValueError naming the missing required columns."""
    squashed = {re.sub(r"[^A-Z0-9]+", "_", _cell(h).upper()).strip("_"): i
                for i, h in enumerate(header)}
    idx = {}
    for field, names in _COLS.items():
        for n in names:
            key = re.sub(r"[^A-Z0-9]+", "_", n.upper()).strip("_")
            if key in squashed:
                idx[field] = squashed[key]
                break
    missing = [f for f in REQUIRED if f not in idx]
    if missing:
        raise ValueError(f"LCA file is missing columns for {missing}; header was "
                         f"{[_cell(h) for h in header][:30]}...")
    return idx


def iter_lca_rows(path):
    """Stream a disclosure file (.xlsx via openpyxl read-only, or .csv) as small dicts with
    just the fields the aggregate needs. Blank trailing rows (openpyxl reports them for the
    DOL workbooks) are skipped."""
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        try:
            ws = wb.active
            rows = ws.iter_rows(values_only=True)
            try:
                header = next(rows)
            except StopIteration:
                return
            idx = header_index(header)
            for r in rows:
                if r is None or idx["employer"] >= len(r) or r[idx["employer"]] is None:
                    continue
                yield {f: r[i] if i < len(r) else None for f, i in idx.items()}
        finally:
            wb.close()
        return
    with open(p, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return
        idx = header_index(header)
        for r in reader:
            if idx["employer"] >= len(r) or not r[idx["employer"]].strip():
                continue
            yield {f: r[i] if i < len(r) else None for f, i in idx.items()}


# ---------------------------------------------------------------- aggregating

def _median(xs: list[int]) -> int:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) // 2


class LCAAggregate:
    """Accumulates certified H-1B filings into wage cells and employer flags. Feed it rows
    from one or many files (any mix of quarters), then read `cells()` and `flags()`."""

    def __init__(self, fy_override: int = 0):
        self.fy_override = int(fy_override or 0)
        self._wages: dict[tuple, list[int]] = {}
        self._levels: dict[tuple, Counter] = {}
        self._pw: dict[tuple, dict[int, list[int]]] = {}
        self._soc: dict[tuple, Counter] = {}
        self._flags: dict[str, dict] = {}
        self.rows_seen = self.rows_kept = 0
        self.fiscal_years: set[int] = set()

    def add(self, r: dict) -> None:
        self.rows_seen += 1
        status = _cell(r.get("status")).lower()
        if not status.startswith("certified"):
            return
        visa = _cell(r.get("visa")).upper()
        if visa and (not visa.startswith("H-1B") or visa.startswith("H-1B1")):
            return          # E-3 / H-1B1 ride the LCA too, but this describes the H-1B track
        employer = _cell(r.get("employer"))
        norm = normalize_employer(employer)
        if not norm:
            return
        fy = self.fy_override or fiscal_year_of(r.get("decided"))
        if not fy:
            return
        # Flags are per employer and ride on every row; "N/A" (a non-dependent employer that
        # need not answer) counts as No.
        dep = _cell(r.get("dependent")).lower() in ("yes", "y")
        wv = _cell(r.get("violator")).lower() in ("yes", "y")
        fl = self._flags.setdefault(norm, {"display": employer.title(), "dep": False,
                                           "wv": False, "fy": 0})
        fl["dep"] = fl["dep"] or dep
        fl["wv"] = fl["wv"] or wv
        fl["fy"] = max(fl["fy"], fy)
        wage = _annual(r.get("wage_from"), r.get("wage_unit"))
        if not wage:
            return
        title = normalize_title(_cell(r.get("title")))
        if not title:
            return
        key = (norm, fy, title, _cell(r.get("city")).upper(), _cell(r.get("state")).upper()[:2])
        self._wages.setdefault(key, []).append(wage)
        lvl = _LEVELS.get(_cell(r.get("pw_level")).upper(), 0)
        self._levels.setdefault(key, Counter())[lvl] += 1
        if lvl:
            pw = _annual(r.get("pw"), r.get("pw_unit") or r.get("wage_unit"))
            if pw:
                self._pw.setdefault(key, {}).setdefault(lvl, []).append(pw)
        soc = _cell(r.get("soc"))
        if soc:
            self._soc.setdefault(key, Counter())[soc] += 1
        self.fiscal_years.add(fy)
        self.rows_kept += 1

    def cells(self, min_count: int = 2) -> list[dict]:
        """Wage cells (dicts in WAGE_COLS order) with at least `min_count` filings."""
        out = []
        for key, wages in self._wages.items():
            if len(wages) < min_count:
                continue
            norm, fy, title, city, state = key
            lv = self._levels.get(key, Counter())
            pw = self._pw.get(key, {})
            soc = self._soc.get(key)
            out.append({
                "norm_name": norm, "fy": fy, "title": title, "family": title_family(title),
                "city": city, "state": state,
                "soc_code": soc.most_common(1)[0][0] if soc else "",
                "n": len(wages), "wage_min": min(wages), "wage_median": _median(wages),
                "wage_max": max(wages),
                "lvl1": lv.get(1, 0), "lvl2": lv.get(2, 0), "lvl3": lv.get(3, 0), "lvl4": lv.get(4, 0),
                "pw1": _median(pw[1]) if pw.get(1) else 0, "pw2": _median(pw[2]) if pw.get(2) else 0,
                "pw3": _median(pw[3]) if pw.get(3) else 0, "pw4": _median(pw[4]) if pw.get(4) else 0,
            })
        out.sort(key=lambda c: (c["norm_name"], c["fy"], c["title"], c["city"], c["state"]))
        return out

    def flags(self) -> list[dict]:
        """One row per employer seen on a certified H-1B LCA: its dependent / willful-violator
        attestation (any Yes in the files wins) and the latest fiscal year it filed."""
        return [{"norm_name": n, "display_name": f["display"], "h1b_dependent": int(f["dep"]),
                 "willful_violator": int(f["wv"]), "fy": f["fy"]}
                for n, f in sorted(self._flags.items())]


def aggregate_files(paths, min_count: int = 2, fy_override: int = 0, log=None) -> LCAAggregate:
    """Stream every file into one LCAAggregate."""
    agg = LCAAggregate(fy_override=fy_override)
    for p in paths:
        before = agg.rows_seen
        for r in iter_lca_rows(p):
            agg.add(r)
        if log:
            log(f"{Path(p).name}: {agg.rows_seen - before:,} rows, "
                f"{agg.rows_kept:,} certified H-1B filings with a wage so far, "
                f"fiscal years {sorted(agg.fiscal_years)}")
    return agg


# ---------------------------------------------------------------- bundle files

WAGES_FILE = "lca/wages.csv.gz"
FLAGS_FILE = "lca/flags.csv.gz"
MANIFEST_FILE = "lca/latest.json"


def write_lca_bundle(out_dir, cells, flags, version: str, fiscal_years, sources=(),
                     min_count: int = 2) -> dict:
    """Write wages.csv.gz + flags.csv.gz + latest.json under `out_dir` and return the manifest."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with gzip.open(out / "wages.csv.gz", "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(WAGE_COLS)
        for c in cells:
            w.writerow([c[k] for k in WAGE_COLS])
    with gzip.open(out / "flags.csv.gz", "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FLAG_COLS)
        for r in flags:
            w.writerow([r[k] for k in FLAG_COLS])
    employers = len({c["norm_name"] for c in cells})
    manifest = {
        "version": version, "fiscal_years": sorted(int(y) for y in fiscal_years),
        "cells": len(cells), "employers": employers, "flag_rows": len(flags),
        "min_count": int(min_count), "sources": list(sources),
        "built_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "columns": WAGE_COLS, "flag_columns": FLAG_COLS,
        "bytes": {"wages": (out / "wages.csv.gz").stat().st_size,
                  "flags": (out / "flags.csv.gz").stat().st_size},
        "source_page": LCA_PAGE,
    }
    (out / "latest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def read_bundle_rows(path_or_bytes):
    """Iterate a bundle CSV (gzipped or plain; a path or raw bytes) as dicts."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        raw = bytes(path_or_bytes)
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        f = io.StringIO(raw.decode("utf-8-sig", errors="replace"), newline="")
    else:
        opener = gzip.open if str(path_or_bytes).endswith(".gz") else open
        f = opener(path_or_bytes, "rt", encoding="utf-8-sig", newline="")
    with f:
        yield from csv.DictReader(f)


def sync_lca_bundle(db, base_url: str, fetch=None) -> dict:
    """Apply the hosted LCA bundle from `base_url` ({base}/lca/latest.json, wages.csv.gz,
    flags.csv.gz), once per manifest version. Unreachable or malformed is reported, never
    raised. `fetch(url) -> bytes` is injectable for offline tests."""
    from sourcing.sponsors import _http_get_bytes
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return {"status": "disabled"}
    get = fetch or _http_get_bytes
    try:
        manifest = json.loads(get(f"{base}/{MANIFEST_FILE}").decode("utf-8"))
        version = str(manifest.get("version") or "").strip()
        if not version:
            return {"status": "invalid", "reason": "manifest has no version"}
    except Exception as exc:                        # noqa: BLE001 - offline is normal
        return {"status": "unreachable", "reason": str(exc)[:200]}
    if db.get_meta("lca_bundle_version") == version:
        return {"status": "up_to_date", "version": version}
    try:
        n = db.ingest_lca_wage_rows(read_bundle_rows(get(f"{base}/{WAGES_FILE}")))
        try:
            f = db.ingest_lca_flag_rows(read_bundle_rows(get(f"{base}/{FLAGS_FILE}")))
        except Exception:                           # noqa: BLE001 - flags are optional
            f = 0
    except Exception as exc:                        # noqa: BLE001
        return {"status": "unreachable", "reason": str(exc)[:200], "version": version}
    db.set_meta("lca_bundle_version", version)
    db.set_meta("lca_updated_at",
                _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat())
    return {"status": "merged", "version": version, "cells": n, "flag_rows": f,
            "fiscal_years": manifest.get("fiscal_years") or []}


def load_lca_bundle(db, bundle_dir) -> dict:
    """Load a bundle directory (wages.csv.gz, flags.csv.gz, latest.json) into a SponsorDB.
    Applied once per manifest version (sponsor_meta `lca_bundle_version`). Returns a status
    dict; a missing bundle is {"status": "absent"}."""
    d = Path(bundle_dir)
    manifest_path, wages_path, flags_path = d / "latest.json", d / "wages.csv.gz", d / "flags.csv.gz"
    if not manifest_path.exists() or not wages_path.exists():
        return {"status": "absent"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = str(manifest.get("version") or "")
    if version and db.get_meta("lca_bundle_version") == version:
        return {"status": "up_to_date", "version": version}
    n = db.ingest_lca_wage_rows(read_bundle_rows(wages_path))
    f = db.ingest_lca_flag_rows(read_bundle_rows(flags_path)) if flags_path.exists() else 0
    if version:
        db.set_meta("lca_bundle_version", version)
    db.set_meta("lca_updated_at",
                _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat())
    return {"status": "loaded", "version": version, "cells": n, "flag_rows": f,
            "fiscal_years": manifest.get("fiscal_years") or []}
