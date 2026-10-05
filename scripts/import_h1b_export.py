"""Maintainer tool: bring NEW H-1B fiscal years into Tailor from a USCIS dashboard export.

USCIS published the H-1B Employer Data Hub as one CSV per fiscal year up to FY2023. Since
then the data lives only in a Tableau dashboard, so a human exports it by hand (click by
click in docs/h1b-data.md) and runs this once a quarter:

    python scripts/import_h1b_export.py ~/Downloads/h1b_fy2026.csv [more files...]
        [--version 2026-09] [--fy 2026] [--force]
        [--db data/sponsors.db] [--seed seed/sponsors_seed.csv.gz] [--out dist-data/h1b]

What it does, in order:
  1. Opens the local sponsor store (data/sponsors.db) through the store API. If it holds no
     H-1B rows yet, the committed seed is merged in first, so the older years are never lost.
  2. Reads each export (CSV, TSV or XLSX; any header dialect USCIS has used), tells you how
     it mapped the columns, and ingests every fiscal year the store has not counted yet.
     Years already counted are skipped (ingest is additive, so re-adding would double them)
     unless --force.
  3. Regenerates seed/sponsors_seed.csv.gz, which ships inside the installer and backs the
     hosted feed. Commit it.
  4. Writes dist-data/h1b/latest.csv.gz + latest.json, the quarterly snapshot. Upload both
     to the host behind TAILOR_DATA_URL and every installed Tailor merges it on its next
     "Update visa data".

Only the store API touches data/: no raw SQL here.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_sponsor_seed import write_seed  # noqa: E402
from sourcing.sponsors import (SponsorDB, canonical_h1b_rows, h1b_header_map,  # noqa: E402
                               parse_perm_xlsx, read_seed_rows, _int)

DEFAULT_DB = ROOT / "data" / "sponsors.db"
DEFAULT_SEED = ROOT / "seed" / "sponsors_seed.csv.gz"
DEFAULT_OUT = ROOT / "dist-data" / "h1b"
REQUIRED = ("Employer", "Initial Approval", "Continuing Approval")


def _decode(raw: bytes) -> str:
    """Tableau exports are sometimes UTF-16 with tabs; the yearly files are UTF-8 with a BOM."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    text = raw.decode("utf-8-sig", errors="replace")
    if text.count("\x00") > len(text) // 10:       # UTF-16 without a BOM
        return raw.decode("utf-16", errors="replace")
    return text


def read_export_rows(path) -> list[dict]:
    """Rows of a USCIS export as dicts keyed by the file's own headers (.csv/.tsv/.txt/.xlsx).
    The delimiter is sniffed, so a tab-separated 'CSV' from Tableau reads fine."""
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        return parse_perm_xlsx(p)                  # header + rows, same shape
    text = _decode(p.read_bytes())
    head = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(head, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    return list(csv.DictReader(io.StringIO(text, newline=""), dialect=dialect))


def describe_mapping(headers) -> tuple[dict, list[str]]:
    """(header map, missing canonical columns) so the maintainer sees what was understood."""
    hmap = h1b_header_map(headers)
    have = set(hmap.values())
    return hmap, [c for c in REQUIRED if c not in have]


def import_exports(files, db_path=DEFAULT_DB, seed_path=DEFAULT_SEED, out_dir=DEFAULT_OUT,
                   version: str | None = None, fy_override: int | None = None,
                   force: bool = False, log=print) -> dict:
    db_path, seed_path, out_dir = Path(db_path), Path(seed_path), Path(out_dir)
    version = version or _dt.date.today().strftime("%Y-%m")
    db = SponsorDB(db_path)
    try:
        if not db.stats()["h1b"] and seed_path.exists():
            n = db.merge_aggregated_rows(read_seed_rows(seed_path))
            db.set_h1b_fys_ingested(db.h1b_fys_ingested() | set(db.h1b_fiscal_years()))
            log(f"store had no H-1B rows: merged {n:,} rows from {seed_path.name} first")
        done = db.h1b_fys_ingested()
        log(f"fiscal years already counted: {sorted(done) or 'none'}")
        loaded: dict[int, int] = {}
        skipped: set[int] = set()
        sources = []
        for f in files:
            f = Path(f)
            rows = read_export_rows(f)
            if not rows:
                log(f"{f.name}: no rows, skipped")
                continue
            hmap, missing = describe_mapping(rows[0].keys())
            renamed = ", ".join(f"{k!r} -> {v}" for k, v in hmap.items() if k != v)
            log(f"{f.name}: {len(rows):,} rows; columns understood as "
                + (renamed or "the standard USCIS headers"))
            if missing:
                raise SystemExit(f"{f.name}: could not find {missing} in headers "
                                 f"{list(rows[0].keys())}. Export the crosstab with those "
                                 "columns, or rename the headers, and run again.")
            canon = list(canonical_h1b_rows(rows))
            if fy_override:
                for r in canon:
                    if not _int(r.get("Fiscal Year")):
                        r["Fiscal Year"] = str(fy_override)
            by_fy = Counter(_int(r.get("Fiscal Year")) for r in canon)
            if by_fy.get(0):
                raise SystemExit(f"{f.name}: {by_fy[0]:,} rows have no fiscal year. Include "
                                 "the Fiscal Year column in the export or pass --fy.")
            for fy, n in sorted(by_fy.items()):
                if fy in done and not force:
                    skipped.add(fy)
                    log(f"  FY{fy}: {n:,} rows already counted, skipped (use --force to re-add)")
                    continue
                emp = db.ingest_h1b_rows(r for r in canon if _int(r.get("Fiscal Year")) == fy)
                done.add(fy)
                loaded[fy] = loaded.get(fy, 0) + emp
                log(f"  FY{fy}: {n:,} rows -> {emp:,} employers ingested")
            sources.append(f.name)
        db.set_h1b_fys_ingested(done)
        now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
        if loaded:
            db.set_meta("h1b_updated_at", now)
            db.set_meta("h1b_source", "uscis dashboard export")
        stats = db.stats()
    finally:
        db.close()

    n_seed = write_seed(db_path, seed_path)
    log(f"seed: {n_seed:,} rows -> {seed_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    n_snap = write_seed(db_path, out_dir / "latest.csv.gz")
    manifest = {"version": version, "fiscal_years": sorted(done), "rows": n_snap,
                "built_at": now, "sources": sources,
                "columns": ["norm_name", "display_name", "h1b_approvals", "h1b_last_fy",
                            "h1b_first_fy", "naics", "state", "cap_exempt", "e_verify",
                            "perm_certs"]}
    (out_dir / "latest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    log(f"snapshot {version}: {n_snap:,} rows -> {out_dir / 'latest.csv.gz'} + latest.json")
    log(f"store now: {stats['employers']:,} employers, {stats['h1b']:,} H-1B, "
        f"{stats['perm']:,} PERM, {stats['e_verify']:,} E-Verify; "
        f"H-1B fiscal years {sorted(done)}")
    return {"loaded": loaded, "skipped": sorted(skipped), "fiscal_years": sorted(done),
            "seed_rows": n_seed, "snapshot_rows": n_snap, "version": version, **stats}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+", help="USCIS dashboard export(s): .csv/.tsv/.xlsx")
    ap.add_argument("--version", help="snapshot version, default YYYY-MM (today)")
    ap.add_argument("--fy", type=int, help="fiscal year for rows whose Fiscal Year is blank")
    ap.add_argument("--force", action="store_true", help="re-add years already counted")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--seed", default=str(DEFAULT_SEED))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args(argv)
    import_exports(a.files, a.db, a.seed, a.out, version=a.version, fy_override=a.fy,
                   force=a.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
