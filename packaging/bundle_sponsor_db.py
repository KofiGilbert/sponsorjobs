"""Vet and stage the sponsor database for shipping inside the installer.

Run before pyinstaller:   python packaging/bundle_sponsor_db.py
Reads:                    seed/sponsors_seed.csv.gz  (the committed seed, always present)
                          data/sponsors.db           (the dev database, when it exists)
Writes:                   packaging/seed/sponsors.db

WHY THIS SHIPS AT ALL. A fresh install had ZERO visa data: H-1B arrived only when the person
clicked "Update visa data", and PERM/E-Verify only by manual file import. So a paying
customer's first LinkedIn session showed "No sponsor record" on every job, on the product
whose whole differentiator is sponsorship data. Shipping a snapshot fixes the first run, and
at scale it moves the download load onto OUR installer instead of a million fresh installs
each pulling files from a government website.

WHY IT BUILDS FROM THE SEED. The first version copied the developer's data/sponsors.db. That
database had PERM and E-Verify rows but NO H-1B rows (USCIS stopped publishing the yearly
files after FY2023, so the dev machine's refresh had never loaded any), and the installer
inherited the gap: a packaged SponsorJobs showed zero H-1B sponsors while the committed seed had
104,552 of them. So the seed is now the source of truth. A local data/sponsors.db is used
ONLY when it has MORE H-1B employers than the seed (a maintainer who just imported a newer
USCIS export), and it still has to pass the same vetting. This also means CI can build the
installer from a fresh checkout with no data/ directory at all.

This data is aggregated from public U.S. government disclosure files (USCIS H-1B Data Hub,
DOL PERM, E-Verify). U.S. government works are public domain: we may redistribute them.

WHY THE VETTING EXISTS. One source is a file inside a live data directory. Today it holds
only public employer records, but "copy something out of a data dir into a public
installer" is exactly the shape of a future privacy accident, so this refuses to stage
anything that contains more than the two known tables or suspiciously few rows.
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SEED = ROOT / "seed" / "sponsors_seed.csv.gz"
SOURCE = ROOT / "data" / "sponsors.db"
DEST = ROOT / "packaging" / "seed" / "sponsors.db"
EVERIFY = ROOT / "config" / "everify_employers.csv.gz"

# The ONLY tables a sponsor DB may contain (plus sqlite's own internals). Anything else
# means someone changed the schema or pointed this at the wrong file: refuse either way.
# sponsor_meta is a handful of ingest timestamps, and shipping it is a feature: the buyer's
# app then reports the snapshot's real age ("updated ...") instead of pretending it is fresh.
ALLOWED_TABLES = {"sponsor_employer", "sponsor_meta"}
# A real ingest is hundreds of thousands of employers; a nearly-empty file means the dev
# DB was reset and shipping it would recreate the exact bug this exists to fix.
MIN_EMPLOYERS = 50_000


def _counts(path: Path) -> dict:
    con = sqlite3.connect(str(path))
    try:
        row = con.execute(
            "SELECT COUNT(*), SUM(h1b_approvals>0), SUM(perm_certs>0), SUM(e_verify>0) "
            "FROM sponsor_employer").fetchone()
    finally:
        con.close()
    return {"employers": row[0] or 0, "h1b": row[1] or 0, "perm": row[2] or 0,
            "e_verify": row[3] or 0}


def vet(path: Path, min_employers: int = MIN_EMPLOYERS) -> str:
    """'' when `path` is safe to ship, else the reason it is not."""
    con = sqlite3.connect(str(path))
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        unexpected = tables - ALLOWED_TABLES
        if unexpected:
            return (f"unexpected tables {sorted(unexpected)}. A seed may only carry public "
                    "employer records.")
        if "sponsor_employer" not in tables:
            return "no sponsor_employer table."
        n = con.execute("SELECT COUNT(*) FROM sponsor_employer").fetchone()[0]
    finally:
        con.close()
    if n < min_employers:
        return (f"only {n:,} employers (need {min_employers:,}). This looks like a reset or "
                "partial DB; shipping it would recreate the empty-first-run bug.")
    return ""


def _build_from_seed(seed: Path, dest: Path, everify: Path) -> None:
    """Load the committed seed into a fresh database at `dest` through the store code, and
    record what it holds so the app reports the data's real vintage."""
    from sourcing.sponsors import SponsorDB
    if dest.exists():
        dest.unlink()
    db = SponsorDB(dest)
    try:
        db.rebuild_from_seed(seed)
        # The E-Verify bundle in config/ is the fuller list (the seed was distilled from an
        # older database). Its ingest only sets a flag (MAX), so layering it on is safe and
        # idempotent. PERM is NOT layered: its ingest adds counts, and the seed already holds
        # exactly the bundle's numbers, so a second pass would double them.
        if everify.exists():
            import csv
            import gzip
            with gzip.open(everify, "rt", encoding="utf-8-sig") as f:
                db.ingest_everify_rows(csv.DictReader(f))
        stamp = _dt.datetime.fromtimestamp(seed.stat().st_mtime, _dt.timezone.utc) \
            .replace(microsecond=0).isoformat()
        fys = db.h1b_fiscal_years()
        if fys:
            db.set_h1b_fys_ingested(fys)
        s = db.stats()
        for key, present in (("h1b_updated_at", s["h1b"]), ("perm_updated_at", s["perm"]),
                             ("everify_updated_at", s["e_verify"])):
            if present:
                db.set_meta(key, stamp)
        db.set_meta("h1b_source", "bundled seed")
    finally:
        db.close()


def build(seed: Path = SEED, source: Path = SOURCE, dest: Path = DEST,
          min_employers: int = MIN_EMPLOYERS, everify: Path = EVERIFY, log=print) -> dict:
    """Stage `dest`. Returns {"used": "seed"|"local", **counts}; raises RuntimeError when
    nothing shippable exists. Writes to a temp path first so a failed build never leaves a
    half-written file where the pyinstaller spec will pick it up."""
    if not seed.exists():
        raise RuntimeError(f"no {seed}: the committed seed is missing; nothing to stage.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".building.db")
    _build_from_seed(seed, tmp, everify)
    seed_counts = _counts(tmp)
    log(f"seed {seed.name}: {seed_counts['employers']:,} employers, "
        f"{seed_counts['h1b']:,} H-1B, {seed_counts['perm']:,} PERM, "
        f"{seed_counts['e_verify']:,} E-Verify")
    used, counts = "seed", seed_counts
    if source.exists():
        reason = vet(source, min_employers)
        if reason:
            log(f"ignoring {source}: {reason}")
        else:
            local_counts = _counts(source)
            log(f"local {source}: {local_counts['employers']:,} employers, "
                f"{local_counts['h1b']:,} H-1B, {local_counts['perm']:,} PERM, "
                f"{local_counts['e_verify']:,} E-Verify")
            if local_counts["h1b"] > seed_counts["h1b"]:
                shutil.copyfile(source, tmp)
                used, counts = "local", local_counts
            else:
                log("local DB has no more H-1B employers than the seed; using the seed.")
    reason = vet(tmp, min_employers)
    if reason:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"REFUSING to stage: {reason}")
    os.replace(tmp, dest)
    log(f"staged {dest} from the {used} ({counts['employers']:,} employers: "
        f"{counts['h1b']:,} H-1B, {counts['perm']:,} PERM, {counts['e_verify']:,} E-Verify, "
        f"{dest.stat().st_size / 1e6:.0f} MB)")
    return {"used": used, **counts}


def main() -> int:
    try:
        build()
    except RuntimeError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
