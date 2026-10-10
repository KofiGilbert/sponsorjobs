"""Visa-sponsor overlay from PUBLIC U.S. government data (GREEN lane — CLAUDE.md §6).

The differentiator for international students: tag each sourced job by whether the
employer actually sponsors work visas, using only free public disclosure data — no
scraping, no paid feed. We invert the Migrate-Mate model (buy a huge feed, filter to
sponsors): we start from the free sponsor list and tag the jobs we already source.

Data sources (all official, public):
  * H-1B sponsor    : USCIS H-1B Employer Data Hub CSV (employer, approvals, NAICS).
  * Cap-exempt (≈)  : derived from NAICS 61 (higher ed) / higher-ed name patterns —
                      the "no-lottery" H-1B path. Heuristic, labelled as such.
  * E-Verify/STEM-OPT: schema is here, but USCIS publishes no clean bulk file (the
                      Employer Search is per-query), so it stays unpopulated until
                      wired — we never fake it.

Employer-name matching (job's company ↔ disclosure employer) is deliberately
CONSERVATIVE: a wrong "sponsor" badge is worse than a missing one, so we only match
on a normalized exact name or a whole-word prefix with a length guard.
"""

from __future__ import annotations

import csv
import datetime as _dt
import gzip
import io
import json
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# HONEST client identifier — same spirit as sourcing/ats.py. Never a browser masquerade:
# §7 forbids spoofing a UA / defeating bot detection to get through, even for public data.
_UA = "resume-agent/1.0 (personal job search; +local)"

_US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV "
    "NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
# A bare two-letter code is ambiguous across countries, so a state only counts after a
# comma ("Austin, TX"), which is how postings actually write one.
_US_WORDS = re.compile(
    r"\b(u\.?s\.?a?|united states|remote\s*[-,:]?\s*(us|usa|united states)|"
    r"anywhere in the us|us[- ]based|us remote|"
    r"new york|nyc|san francisco|los angeles|chicago|boston|seattle|austin|denver|"
    r"atlanta|dallas|houston|miami|philadelphia|phoenix|san jose|san diego|portland|"
    r"washington,? d\.?c\.?|silicon valley|bay area)\b", re.I)
_US_STATE_TAIL = re.compile(r",\s*(" + "|".join(_US_STATES) + r")\b(?!\w)")

# NAICS 2-digit sector -> plain-English industry, so the sponsor record's raw code can name the
# company's field in the "About" strip. Sectors that share a range (Manufacturing 31-33, Retail
# 44-45, Transport 48-49) map every prefix to one label. Unknown/blank -> "" (strip omits it).
_NAICS_SECTOR = {
    "11": "Agriculture, Forestry & Fishing", "21": "Mining, Oil & Gas",
    "22": "Utilities", "23": "Construction",
    "31": "Manufacturing", "32": "Manufacturing", "33": "Manufacturing",
    "42": "Wholesale Trade", "44": "Retail", "45": "Retail",
    "48": "Transportation & Warehousing", "49": "Transportation & Warehousing",
    "51": "Information & Media", "52": "Finance & Insurance",
    "53": "Real Estate", "54": "Professional, Scientific & Technical Services",
    "55": "Company Management", "56": "Administrative & Support Services",
    "61": "Education", "62": "Health Care & Social Assistance",
    "71": "Arts, Entertainment & Recreation", "72": "Accommodation & Food Services",
    "81": "Other Services", "92": "Public Administration",
}


def naics_industry(naics: str) -> str:
    """Plain-English industry from a NAICS code's leading two digits ('541511' -> 'Professional,
    Scientific & Technical Services'). '' when unknown, so the UI just omits the line."""
    digits = re.sub(r"\D", "", str(naics or ""))
    return _NAICS_SECTOR.get(digits[:2], "") if len(digits) >= 2 else ""


def looks_us(location: str) -> bool:
    """True when the ROLE sits in the United States.

    H-1B, PERM and E-Verify are US instruments and say nothing about a job in London, so a
    badge must not appear on one. Deliberately conservative: a location we can't read
    returns False, so an unclear posting loses a badge rather than carrying a promise we
    can't keep. The employer sponsor summary still rides along either way, so what's lost
    is the claim, not the information.
    """
    text = (location or "").strip()
    if not text:
        return False
    return bool(_US_WORDS.search(text) or _US_STATE_TAIL.search(text))


# Legal-suffix tokens stripped when normalizing an employer name so "Amazon Com
# Services LLC" and a posting's "Amazon" can align.
_SUFFIX = {
    "inc", "incorporated", "llc", "l.l.c", "corp", "corporation", "co", "company",
    "ltd", "limited", "lp", "llp", "plc", "pllc", "pc", "pa", "na", "usa", "us",
}
# Higher-ed / research name patterns → cap-exempt-likely (no H-1B lottery).
_CAP_EXEMPT_RE = re.compile(
    r"\b(univ|university|college|school district|board of regents|regents of|"
    r"institute of technology|research institute|medical center|health system|"
    r"national laboratory|national laboratories|children's hospital|childrens hospital|"
    r"cancer center|school of medicine|medical school|polytechnic|teaching hospital|"
    r"seminary)\b",
    re.I)

# Curated brand → filing-entity aliases: correct corporate facts for well-known names
# a posting shows as a bare brand but whose H-1B filings sit under a different/longer
# legal name (verified against the real USCIS data). Never a guess — only exact,
# checkable mappings. Keys and values are already NORMALIZED.
_ALIASES = {
    "meta": "meta platforms",
    "facebook": "meta platforms",
    "alphabet": "google",
    "aws": "amazon web services",
    "uber": "uber technologies",
    "walgreens": "walgreen",          # the brand files H-1Bs as "Walgreen Co" (55 approvals), not "Walgreens"
}

# Well-known brands whose BARE single-token name should still aggregate their many filing
# entities ("Amazon" -> "Amazon Web Services" / "Amazon Data Services" / ...). Every OTHER
# single-token company name is matched EXACTLY (no prefix expansion), so an unrelated
# employer like "Nowhere Partners" can never lend its badge to a job at "Nowhere Corp" —
# a wrong sponsor badge is worse than a missing one (CLAUDE.md §8). These are curated,
# distinctive brand words (low collision risk) that are verified major H-1B filers; keys
# are NORMALIZED (suffix-stripped, lowercase).
_BRAND_PREFIXES = {
    "amazon", "google", "microsoft", "oracle", "salesforce", "nvidia", "qualcomm",
    "deloitte", "accenture", "capgemini", "infosys", "wipro", "cognizant",
}


_MISS = object()   # cache sentinel: a real None result (no sponsor) is distinct from "not cached"


def _join_initials(toks: list[str]) -> list[str]:
    """Join a run of single letters into one word: "u s bank" -> "us bank", "j p morgan" ->
    "jp morgan". Punctuation removal splits "U.S. Bank" into lone letters, while the filings
    also spell it "US Bank": U.S. Bank's 1,839 approvals sat under "us bank ..." and a posting
    saying "U.S. Bank" matched a 2-petition entity instead (2026-10-10)."""
    out: list[str] = []
    run = ""
    for t in toks:
        if len(t) == 1 and t.isalpha():
            run += t
            continue
        if run:
            out.append(run)
            run = ""
        out.append(t)
    if run:
        out.append(run)
    return out


def _normalize(name: str, join_initials: bool) -> str:
    s = (name or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = [t for t in s.split() if t]
    if join_initials:
        toks = _join_initials(toks)
    while toks and toks[-1] in _SUFFIX:
        toks.pop()
    if toks and toks[0] == "the":
        toks = toks[1:]
    return " ".join(toks)


def normalize_employer(name: str) -> str:
    """Lowercase, drop punctuation, join initials ("U.S." -> "us"), and strip trailing
    legal-suffix tokens so the same company matches across its many filing entities and a
    job posting."""
    return _normalize(name, True)


def normalize_employer_variants(name: str) -> list[str]:
    """The current normal form, plus the pre-2026-10-10 one (initials left apart) so a sponsor
    database built before this change still matches until it is rebuilt."""
    out = [normalize_employer(name)]
    legacy = _normalize(name, False)
    if legacy and legacy not in out:
        out.append(legacy)
    return [n for n in out if n]


def _is_cap_exempt(naics: str, name: str) -> bool:
    return str(naics or "").strip().startswith("61") or bool(_CAP_EXEMPT_RE.search(name or ""))


# The ONE canonical caveat, surfaced everywhere a badge is shown (job list, package
# view, and the browser extension all read this). A badge means the EMPLOYER appears in
# historical public disclosure data — it is a propensity signal, never a promise about a
# specific opening. Saying so plainly is a trust feature: the whole category is built on
# the same government data, so honesty about its limits is where we differ. See CLAUDE.md
# §8 ("a wrong badge is worse than a missing one").
SPONSOR_DISCLAIMER = (
    "Visa badges come from an employer's PAST filings in public USCIS/DOL disclosure "
    "data. They show a company has sponsored before, not a guarantee this role sponsors, "
    "that they still sponsor, or that you'll qualify. Government data also lags live "
    "openings, so treat a badge as a signal to research, not a promise."
)


@dataclass
class SponsorRecord:
    display_name: str
    h1b_approvals: int
    h1b_last_fy: int
    naics: str
    state: str
    cap_exempt: bool
    e_verify: bool = False
    h1b_first_fy: int = 0
    perm_certs: int = 0

    def _fy_range(self) -> str:
        first, last = self.h1b_first_fy or self.h1b_last_fy, self.h1b_last_fy
        return f"FY{last}" if first == last else f"FY{first} to {last}"

    def badges(self) -> list[dict]:
        """Badge dicts for the UI/extension. Each carries an honest `basis` line stating
        exactly what the badge is drawn from and its limits, so the caveat travels WITH
        the badge rather than living only in a tooltip somewhere. `code`/`label`/`detail`
        are unchanged for back-compat; `basis` is additive."""
        out: list[dict] = []
        if self.h1b_approvals > 0:
            out.append({"code": "H-1B", "label": "H-1B sponsor",
                        "detail": f"{self.h1b_approvals} approvals ({self._fy_range()})",
                        "basis": (f"{self.h1b_approvals} approved H-1B petitions on record "
                                  f"({self._fy_range()}, USCIS Data Hub). A track record, "
                                  "not a promise this role sponsors.")})
        if self.perm_certs > 0:
            out.append({"code": "GREEN-CARD", "label": "Green-card sponsor (PERM)",
                        "detail": f"{self.perm_certs} certified PERM cases",
                        "basis": (f"{self.perm_certs} certified PERM (green-card) cases on "
                                  "record (DOL disclosure). Historical, not a guarantee.")})
        if self.e_verify:
            # E-Verify enrollment is REQUIRED for a STEM-OPT hire but is not sufficient on
            # its own, and enrollment can lapse — so the label states the fact (E-Verify)
            # and the basis explains the STEM-OPT link honestly, rather than asserting the
            # student is "eligible".
            out.append({"code": "STEM-OPT", "label": "E-Verify (STEM-OPT-capable)",
                        "detail": "Employer on record as E-Verify-enrolled",
                        "basis": ("E-Verify enrollment is required for a STEM-OPT hire, but "
                                  "on its own it doesn't mean they sponsor, and enrollment "
                                  "can lapse. Confirm before relying on it.")})
        if self.cap_exempt:
            out.append({"code": "CAP-EXEMPT", "label": "Cap-exempt (likely, no lottery)",
                        "basis": ("Guessed from the employer's name/industry (higher-ed or "
                                  "research = no H-1B lottery). A heuristic, not verified.")})
        return out

    def nationality_visas(self) -> list[dict]:
        """Visas an H-1B sponsor can typically ALSO support, gated on the CANDIDATE's
        nationality rather than the employer's approval history. E-3 and H-1B1 ride on the
        exact LCA machinery this employer already uses for H-1B; TN (USMCA professionals)
        needs only an employer letter. Kept SEPARATE from badges() — these are propensity
        signals with a nationality caveat, not approval-backed facts — and shown only when
        the employer has real H-1B history. Matches Migrate Mate's visa-category breadth
        (E-3/TN/H-1B1) honestly, without new data. See CLAUDE.md §8."""
        if self.h1b_approvals <= 0:
            return []
        return [
            {"code": "E-3", "label": "E-3 (Australia)",
             "basis": ("Australian nationals: E-3 uses the same LCA process this employer "
                       "already files for H-1B, so a willing sponsor can usually support it. "
                       "Nationality-based propensity, not a guarantee for this role.")},
            {"code": "H-1B1", "label": "H-1B1 (Chile / Singapore)",
             "basis": ("Chilean or Singaporean nationals: H-1B1 uses the same LCA process as "
                       "H-1B, so a willing H-1B sponsor can usually support it. "
                       "Nationality-based propensity, not a guarantee for this role.")},
            {"code": "TN", "label": "TN (Canada / Mexico)",
             "basis": ("Canadian or Mexican nationals in a USMCA profession: TN needs only an "
                       "employer letter (no LCA), so a willing sponsor can usually support it. "
                       "The role must be on the TN profession list. Not a guarantee.")},
        ]

def _merge_records(matches: list["SponsorRecord"]) -> "SponsorRecord":
    """One employer from its several filing entities: counts add up, the biggest entity lends its
    display name, NAICS and state. Tie-break on perm then name so the pick is DETERMINISTIC."""
    top = max(matches, key=lambda r: (r.h1b_approvals, r.perm_certs, r.display_name or ""))
    firsts = [r.h1b_first_fy for r in matches if r.h1b_first_fy]
    return SponsorRecord(
        display_name=top.display_name,
        h1b_approvals=sum(r.h1b_approvals for r in matches),
        h1b_last_fy=max(r.h1b_last_fy for r in matches),
        naics=top.naics, state=top.state,
        cap_exempt=any(r.cap_exempt for r in matches),
        e_verify=any(r.e_verify for r in matches),
        h1b_first_fy=min(firsts) if firsts else 0,
        perm_certs=sum(r.perm_certs for r in matches),
    )



class SponsorDB:
    """Local SQLite store of visa-sponsoring employers (aggregated per normalized
    name), plus an in-memory index for fast, conservative job tagging."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""CREATE TABLE IF NOT EXISTS sponsor_employer (
            norm_name TEXT PRIMARY KEY, display_name TEXT,
            h1b_approvals INTEGER DEFAULT 0, h1b_last_fy INTEGER DEFAULT 0,
            h1b_first_fy INTEGER DEFAULT 0, naics TEXT, state TEXT,
            cap_exempt INTEGER DEFAULT 0, e_verify INTEGER DEFAULT 0,
            perm_certs INTEGER DEFAULT 0)""")
        # Simple key/value meta (e.g. last-updated timestamps for auto-refresh).
        self._conn.execute("CREATE TABLE IF NOT EXISTS sponsor_meta (k TEXT PRIMARY KEY, v TEXT)")
        # Older DBs created before perm_certs existed get the column added.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(sponsor_employer)")}
        if "perm_certs" not in cols:
            self._conn.execute("ALTER TABLE sponsor_employer ADD COLUMN perm_certs INTEGER DEFAULT 0")
        self._conn.commit()
        # lookup() no longer holds all 717k employers in RAM (that ~290MB build was a multi-second
        # CPU spike on boot that starved the 1-CPU broker's health check -> Render restart loop).
        # Instead each lookup queries this SQLite table directly (indexed on the norm_name PRIMARY
        # KEY). The lock serialises the read cursor with concurrent ingests on the shared connection.
        self._index_lock = threading.Lock()
        # Per-process cache of lookup() results (norm -> record|None). A feed build tags thousands
        # of jobs and many share a company, so this turns most lookups into a dict hit. Bounded,
        # and cleared whenever the sponsor data changes (see _invalidate_index).
        self._lookup_cache: dict[str, SponsorRecord | None] = {}

    # -- meta (timestamps for auto-refresh) ---------------------------- #
    def get_meta(self, key: str) -> str:
        row = self._conn.execute("SELECT v FROM sponsor_meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else ""

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute("INSERT INTO sponsor_meta(k, v) VALUES(?,?) "
                            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
        self._conn.commit()

    # -- ingest --------------------------------------------------------- #
    def ingest_h1b_rows(self, rows) -> int:
        """Aggregate USCIS H-1B Data Hub rows (dicts with the file's headers) into the
        employer table. Sums approvals across a company's many entities/worksites, and
        also indexes a 'DBA' trade name so 'X LLC DBA ProCogia' matches 'ProCogia'.

        Accepts BOTH header dialects USCIS has used: the yearly CSV files ("Employer",
        "Initial Approval", ...) and the Tableau dashboard's crosstab export ("Employer
        (Petitioner) Name", "Initial Approvals", ...), see `canonical_h1b_rows`."""
        agg: dict[str, dict] = {}
        for r in canonical_h1b_rows(rows):
            raw = (r.get("Employer") or "").strip()
            if not raw:
                continue
            approvals = _int(r.get("Initial Approval")) + _int(r.get("Continuing Approval"))
            fy = _int(r.get("Fiscal Year"))
            naics, state = str(r.get("NAICS") or ""), str(r.get("State") or "")
            for variant in re.split(r"\s+dba\s+", raw, flags=re.I):
                norm = normalize_employer(variant)
                if not norm:
                    continue
                a = agg.setdefault(norm, {"display": variant.strip().title(), "appr": 0,
                                          "fy_max": 0, "fy_min": 0, "naics": naics,
                                          "state": state, "cap": False})
                a["appr"] += approvals
                a["fy_max"] = max(a["fy_max"], fy)
                a["fy_min"] = fy if not a["fy_min"] else min(a["fy_min"], fy)
                if _is_cap_exempt(naics, variant):
                    a["cap"] = True
        for norm, a in agg.items():
            self._conn.execute(
                "INSERT INTO sponsor_employer(norm_name, display_name, h1b_approvals, "
                "h1b_last_fy, h1b_first_fy, naics, state, cap_exempt) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(norm_name) DO UPDATE SET "
                "h1b_approvals=h1b_approvals+excluded.h1b_approvals, "
                "h1b_last_fy=MAX(h1b_last_fy, excluded.h1b_last_fy), "
                "h1b_first_fy=MIN(h1b_first_fy, excluded.h1b_first_fy), "
                "cap_exempt=MAX(cap_exempt, excluded.cap_exempt)",
                (norm, a["display"], a["appr"], a["fy_max"], a["fy_min"], a["naics"],
                 a["state"], int(a["cap"])))
        self._conn.commit()
        self._invalidate_index()
        return len(agg)

    def _upsert_flag(self, agg: dict, col: str, addvalue: bool = False) -> int:
        """Merge a per-employer flag/count (perm_certs or e_verify) into existing rows,
        creating a row for employers not yet seen. `agg`: norm -> {display, val}."""
        for norm, a in agg.items():
            if addvalue:
                self._conn.execute(
                    f"INSERT INTO sponsor_employer(norm_name, display_name, {col}) "
                    f"VALUES(?,?,?) ON CONFLICT(norm_name) DO UPDATE SET "
                    f"{col}={col}+excluded.{col}", (norm, a["display"], a["val"]))
            else:
                self._conn.execute(
                    f"INSERT INTO sponsor_employer(norm_name, display_name, {col}) "
                    f"VALUES(?,?,?) ON CONFLICT(norm_name) DO UPDATE SET "
                    f"{col}=MAX({col}, excluded.{col})", (norm, a["display"], a["val"]))
        self._conn.commit()
        self._invalidate_index()
        return len(agg)

    def ingest_perm_rows(self, rows) -> int:
        """Green-card sponsors from DOL PERM disclosure rows (dicts). Counts CERTIFIED
        cases per employer -> perm_certs. Column names vary by year, so we resolve the
        employer/status columns by header match."""
        agg: dict[str, dict] = {}
        for r in rows:
            emp = _pick(r, ("EMPLOYER_NAME", "EMPLOYER_LEGAL_BUSINESS_NAME", "Employer"))
            status = _pick(r, ("CASE_STATUS", "STATUS")).strip().lower()
            if not emp or "certif" not in status:   # keep Certified / Certified-Expired
                continue
            for variant in re.split(r"\s+dba\s+", emp, flags=re.I):
                norm = normalize_employer(variant)
                if not norm:
                    continue
                a = agg.setdefault(norm, {"display": variant.strip().title(), "val": 0})
                a["val"] += 1
        return self._upsert_flag(agg, "perm_certs", addvalue=True)

    def ingest_perm_counts(self, rows) -> int:
        """Ingest the compact BUNDLED green-card list (EMPLOYER_NAME, PERM_CERTS) that
        ships with the app — pre-distilled from DOL PERM so end-users get green-card
        badges automatically, with no 76MB download."""
        agg: dict[str, dict] = {}
        for r in rows:
            emp = _pick(r, ("EMPLOYER_NAME", "Employer"))
            try:
                n = int(_pick(r, ("PERM_CERTS", "CERTS")) or 0)
            except ValueError:
                n = 0
            if not emp or n <= 0:
                continue
            for variant in re.split(r"\s+dba\s+", emp, flags=re.I):
                norm = normalize_employer(variant)
                if norm:
                    a = agg.setdefault(norm, {"display": variant.strip().title(), "val": 0})
                    a["val"] += n
        return self._upsert_flag(agg, "perm_certs", addvalue=True)

    def ingest_everify_rows(self, rows) -> int:
        """E-Verify-enrolled employers (STEM-OPT eligible) from ANY official E-Verify
        employer file — the USCIS participating-employers export, the historic 2018
        list, or a FOIA release. Column names vary across those, so the employer-name
        column is detected by header. Sets e_verify=1 per employer."""
        rows = list(rows)
        if not rows:
            return 0
        col = _find_employer_col(rows[0].keys())
        if not col:
            return 0
        agg: dict[str, dict] = {}
        for r in rows:
            emp = str(r.get(col) or "").strip()
            if not emp:
                continue
            for variant in re.split(r"\s+dba\s+", emp, flags=re.I):
                norm = normalize_employer(variant)
                if norm:
                    agg.setdefault(norm, {"display": variant.strip().title(), "val": 1})
        return self._upsert_flag(agg, "e_verify", addvalue=False)

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM sponsor_employer").fetchone()[0]

    def stats(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*), "
            "SUM(h1b_approvals>0), SUM(perm_certs>0), SUM(e_verify>0) "
            "FROM sponsor_employer").fetchone()
        return {"employers": row[0] or 0, "h1b": row[1] or 0,
                "perm": row[2] or 0, "e_verify": row[3] or 0}

    def is_empty(self) -> bool:
        return self.count() == 0

    def rebuild_from_seed(self, path) -> int:
        """Load the compact committed seed (seed/sponsors_seed.csv.gz) straight into the
        employer table, so a hosted service can stand up the visa-sponsor overlay from the
        code itself — no 67MB binary upload. The seed already holds final aggregated columns,
        so this is a direct bulk load (INSERT OR REPLACE), not a re-aggregation. Returns the
        row count."""
        import csv as _csv
        import gzip as _gzip
        opener = _gzip.open if str(path).endswith(".gz") else open
        cols = ["norm_name", "display_name", "h1b_approvals", "h1b_last_fy", "h1b_first_fy",
                "naics", "state", "cap_exempt", "e_verify", "perm_certs"]
        ints = {"h1b_approvals", "h1b_last_fy", "h1b_first_fy", "cap_exempt", "e_verify",
                "perm_certs"}
        with opener(path, "rt", encoding="utf-8", newline="") as f:
            rows = []
            for r in _csv.DictReader(f):
                rows.append(tuple(int(r.get(c) or 0) if c in ints else (r.get(c) or "")
                                  for c in cols))
        self._conn.executemany(
            f"INSERT OR REPLACE INTO sponsor_employer({','.join(cols)}) "
            f"VALUES({','.join('?' * len(cols))})", rows)
        self._conn.commit()
        self._invalidate_index()
        return len(rows)

    def merge_aggregated_rows(self, rows, h1b_only: bool = False) -> int:
        """MERGE already-aggregated seed-layout rows (the columns of seed/sponsors_seed.csv.gz)
        into the employer table without ever lowering a number: counts and last-FY take
        MAX, first-FY takes the earliest non-zero year, flags OR together, and text fields
        fill only where the row we hold is blank. This is the safe way to bring in a NEWER
        snapshot of the same data (SponsorJobs's quarterly H-1B file, or the bundled seed when a
        database somehow has no H-1B rows): re-adding would double counts, replacing could
        erase a person's own newer import. `h1b_only` brings in just the H-1B columns (so a
        PERM/E-Verify load that already ran is left alone). Returns the rows merged."""
        batch = []
        for r in rows:
            vals = {c: (_int(r.get(c)) if c in SEED_INT_COLS else str(r.get(c) or ""))
                    for c in SEED_COLS}
            if not vals["norm_name"]:
                continue
            if h1b_only:
                if vals["h1b_approvals"] <= 0:
                    continue
                vals["e_verify"] = 0
                vals["perm_certs"] = 0
            batch.append(tuple(vals[c] for c in SEED_COLS))
        if not batch:
            return 0
        self._conn.executemany(
            f"INSERT INTO sponsor_employer({','.join(SEED_COLS)}) "
            f"VALUES({','.join('?' * len(SEED_COLS))}) "
            "ON CONFLICT(norm_name) DO UPDATE SET "
            "display_name=CASE WHEN COALESCE(display_name,'')='' THEN excluded.display_name "
            "ELSE display_name END, "
            "h1b_approvals=MAX(h1b_approvals, excluded.h1b_approvals), "
            "h1b_last_fy=MAX(h1b_last_fy, excluded.h1b_last_fy), "
            "h1b_first_fy=CASE WHEN h1b_first_fy=0 THEN excluded.h1b_first_fy "
            "WHEN excluded.h1b_first_fy=0 THEN h1b_first_fy "
            "ELSE MIN(h1b_first_fy, excluded.h1b_first_fy) END, "
            "naics=CASE WHEN COALESCE(naics,'')='' THEN excluded.naics ELSE naics END, "
            "state=CASE WHEN COALESCE(state,'')='' THEN excluded.state ELSE state END, "
            "cap_exempt=MAX(cap_exempt, excluded.cap_exempt), "
            "e_verify=MAX(e_verify, excluded.e_verify), "
            "perm_certs=MAX(perm_certs, excluded.perm_certs)", batch)
        self._conn.commit()
        self._invalidate_index()
        return len(batch)

    def apply_h1b_snapshot_rows(self, rows) -> int:
        """Apply SponsorJobs's quarterly H-1B snapshot (seed-layout rows) as the AUTHORITATIVE
        H-1B total for every employer it lists with an H-1B count: h1b_approvals,
        h1b_first_fy and h1b_last_fy are SET from the snapshot, not MAX-merged. The
        snapshot is built from the committed seed plus the maintainer's dashboard exports,
        so it already holds every year it declares; MAX-merging it against a local store
        that had added a year of its own produced hybrid totals that no later import could
        repair (the local number won, the missing years were marked ingested).

        Employers absent from the snapshot are untouched. A listed row with no H-1B count
        makes no H-1B claim and leaves the local H-1B columns alone. The other columns keep
        the merge rules: names / NAICS / state fill only where blank, and cap_exempt,
        e_verify and perm_certs are never lowered (they come from other sources).
        Returns the rows applied."""
        batch = []
        for r in rows:
            vals = {c: (_int(r.get(c)) if c in SEED_INT_COLS else str(r.get(c) or ""))
                    for c in SEED_COLS}
            if not vals["norm_name"]:
                continue
            batch.append(tuple(vals[c] for c in SEED_COLS))
        if not batch:
            return 0
        self._conn.executemany(
            f"INSERT INTO sponsor_employer({','.join(SEED_COLS)}) "
            f"VALUES({','.join('?' * len(SEED_COLS))}) "
            "ON CONFLICT(norm_name) DO UPDATE SET "
            "display_name=CASE WHEN COALESCE(display_name,'')='' THEN excluded.display_name "
            "ELSE display_name END, "
            "h1b_approvals=CASE WHEN excluded.h1b_approvals>0 THEN excluded.h1b_approvals "
            "ELSE h1b_approvals END, "
            "h1b_last_fy=CASE WHEN excluded.h1b_approvals>0 THEN excluded.h1b_last_fy "
            "ELSE h1b_last_fy END, "
            "h1b_first_fy=CASE WHEN excluded.h1b_approvals>0 THEN excluded.h1b_first_fy "
            "ELSE h1b_first_fy END, "
            "naics=CASE WHEN COALESCE(naics,'')='' THEN excluded.naics ELSE naics END, "
            "state=CASE WHEN COALESCE(state,'')='' THEN excluded.state ELSE state END, "
            "cap_exempt=MAX(cap_exempt, excluded.cap_exempt), "
            "e_verify=MAX(e_verify, excluded.e_verify), "
            "perm_certs=MAX(perm_certs, excluded.perm_certs)", batch)
        self._conn.commit()
        self._invalidate_index()
        return len(batch)

    def h1b_fiscal_years(self) -> list[int]:
        """The fiscal years the H-1B rows span, read from the data itself (earliest first-FY
        to latest last-FY). Used to record `h1b_fys_ingested` when the rows arrive already
        aggregated (seed or snapshot) rather than year by year."""
        row = self._conn.execute(
            "SELECT MIN(CASE WHEN h1b_first_fy>0 THEN h1b_first_fy END), MAX(h1b_last_fy) "
            "FROM sponsor_employer WHERE h1b_approvals>0").fetchone()
        lo, hi = row[0], row[1]
        if not lo or not hi or hi < lo:
            return []
        return list(range(int(lo), int(hi) + 1))

    def h1b_fys_ingested(self) -> set[int]:
        """The fiscal years already counted into h1b_approvals (meta `h1b_fys_ingested`).
        Year-by-year ingest is additive, so a caller must skip these or counts double."""
        try:
            return {int(x) for x in json.loads(self.get_meta("h1b_fys_ingested") or "[]")}
        except (ValueError, TypeError):
            return set()

    def set_h1b_fys_ingested(self, fys) -> None:
        self.set_meta("h1b_fys_ingested", json.dumps(sorted({int(x) for x in fys})))

    def top_sponsors(self, limit: int = 2000, min_approvals: int = 1) -> list[dict]:
        """The biggest H-1B sponsors, most approvals first — the seed list for board
        discovery (sourcing/discover.py). Ranking by approval volume puts the companies
        that hire the most (and are most likely to run a public ATS board) at the front,
        so a bounded crawl spends its budget where it pays off."""
        rows = self._conn.execute(
            "SELECT display_name, h1b_approvals FROM sponsor_employer "
            "WHERE h1b_approvals >= ? ORDER BY h1b_approvals DESC, display_name LIMIT ?",
            (int(min_approvals), int(limit))).fetchall()
        return [{"display_name": r[0], "h1b_approvals": r[1]} for r in rows]

    # -- lookup / tagging ---------------------------------------------- #
    def _invalidate_index(self) -> None:
        """Drop the lookup cache after an ingest/rebuild changes the sponsor data, so stale
        results aren't served. (There is no in-memory index anymore — lookup() reads SQLite —
        so the name is kept only for the existing call-sites and tests.)"""
        with self._index_lock:
            self._lookup_cache.clear()

    def lookup(self, company: str) -> SponsorRecord | None:
        """Conservative match that AGGREGATES a company's many filing entities. Exact
        normalized name plus whole-word prefixes in either direction ('Amazon' ↔ all
        'Amazon *' subsidiaries), guarded so short/generic single tokens only match
        exactly — never a risky partial. Approvals are summed across matches."""
        variants = [_ALIASES.get(n, n) for n in normalize_employer_variants(company)]
        if not variants:
            return None
        norm = variants[0]
        cached = self._lookup_cache.get(norm, _MISS)
        if cached is not _MISS:            # many jobs share a company; cache the SQL result
            return cached
        if len(variants) > 1:
            # "U.S. Bank": look under both spellings and add up what they find, since filings
            # use both ("US Bank" 1,839 approvals, "U.S. Bank" 2).
            found = [r for r in (self._lookup_one(v) for v in variants) if r]
            result = found[0] if len(found) == 1 else (_merge_records(found) if found else None)
            if len(self._lookup_cache) < 100_000:
                self._lookup_cache[norm] = result
            return result
        result = self._lookup_one(norm)
        if len(self._lookup_cache) < 100_000:
            self._lookup_cache[norm] = result
        return result

    def _lookup_one(self, norm: str) -> SponsorRecord | None:
        """One normalized spelling: the conservative candidate fetch and match below."""
        # Whole-word prefix expansion ("X" <-> "X Something") is where false positives
        # creep in -- a single generic token grabbing an unrelated employer that merely
        # starts with it. Allow it ONLY for a distinctive MULTI-token query ("University
        # of Illinois" -> "...at Chicago") or a curated well-known brand ("Amazon" -> its
        # many "Amazon *" entities). Every other single-token name must match EXACTLY.
        allow_prefix = (" " in norm) or (norm in _BRAND_PREFIXES)
        # Fetch ONLY the candidates the matching can possibly keep, straight from SQLite (norm_name
        # is the PRIMARY KEY, so each clause is an index hit) rather than scanning the whole
        # first-word bucket into RAM: the exact name, its whole-word PARENT prefixes ("new dynasty"
        # for "new dynasty construction"), and its CHILDREN ("new dynasty construction ..."). When
        # prefixes aren't allowed (a lone generic token) only the exact name can match, so it is a
        # single point lookup. Behaviour-identical to the old in-memory bucket + filter.
        if allow_prefix:
            parts = norm.split(" ")
            wanted = [norm] + [" ".join(parts[:i]) for i in range(1, len(parts))]   # exact + parents
            marks = ",".join("?" * len(wanted))
            with self._index_lock:                    # serialise the read cursor with ingests
                rows = self._conn.execute(
                    f"SELECT * FROM sponsor_employer WHERE norm_name IN ({marks}) "
                    "OR (norm_name >= ? AND norm_name < ?)",
                    (*wanted, norm + " ", norm + " \uffff")).fetchall()
        else:
            with self._index_lock:
                rows = self._conn.execute(
                    "SELECT * FROM sponsor_employer WHERE norm_name = ?", (norm,)).fetchall()
        index: dict[str, SponsorRecord] = {
            r["norm_name"]: SponsorRecord(
                r["display_name"], r["h1b_approvals"], r["h1b_last_fy"], r["naics"], r["state"],
                bool(r["cap_exempt"]), bool(r["e_verify"]), r["h1b_first_fy"], r["perm_certs"])
            for r in rows}
        return self._match(norm, allow_prefix, index)

    def _match(self, norm: str, allow_prefix: bool,
               index: dict[str, SponsorRecord]) -> SponsorRecord | None:
        """Conservative aggregation over a SMALL candidate set (exact + parents + children).
        Unchanged from the original bucket loop, just fed SQL-fetched candidates."""
        matches: list[SponsorRecord] = []
        for cand in index:
            if cand == norm:
                matches.append(index[cand])
                continue
            if not allow_prefix:
                continue
            # Query is the parent: "amazon" -> "amazon web services". Safe, because
            # allow_prefix already required the query to be multi-token or a curated brand.
            if cand.startswith(norm + " "):
                matches.append(index[cand])
                continue
            # Candidate is the parent: "first united bank" ends up here against a DB entry
            # whose norm is just "first". The old code accepted ANY such candidate, and the
            # guard above only ever examined the QUERY's shape — so "First Co" (norm:
            # "first", 3 approvals) lent its H-1B badge to First United Bank, and 59,151
            # single-token employers in the real data ("american", "global", "premier"...)
            # could each do the same to any name starting with their word. A candidate may
            # only lend downward if IT is distinctive too: multi-token, or a curated brand.
            if norm.startswith(cand + " ") and ((" " in cand) or (cand in _BRAND_PREFIXES)):
                matches.append(index[cand])
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        return _merge_records(matches)

    def tag_jobs(self, jobs: list[dict]) -> list[dict]:
        """Attach `visa` (badge list) and `sponsor` (summary) to each job in place.

        The badge is gated on the ROLE's location, not just the employer. H-1B, PERM and
        E-Verify are United States instruments: they say nothing about a job in London.

        Sponsor data is per COMPANY, so tagging every row from a matched employer put
        "H-1B sponsor" on Spotify's Stockholm listings and Dropbox's Remote-Poland ones.
        40 of 133 rows in a real feed. That is worse than showing nothing: it invites
        someone who needs sponsorship to spend an application on an impossibility. The
        employer summary still rides along on every row (Spotify genuinely does sponsor,
        which is worth knowing), but the badge only appears where it could actually apply.
        """
        for j in jobs:
            rec = self.lookup(j.get("company", ""))
            in_us = looks_us(j.get("location", ""))
            # Published so the feed can filter on it. Computed here, once, next to the
            # badge it gates, rather than re-derived in the UI from the same string.
            j["us"] = in_us
            j["visa"] = rec.badges() if (rec and in_us) else []
            # Nationality-based visas an H-1B sponsor can usually also support (E-3/H-1B1/TN).
            # Separate key so the core badge contract is unchanged; empty unless US + H-1B.
            j["nationality_visas"] = rec.nationality_visas() if (rec and in_us) else []
            j["sponsor"] = ({"h1b_approvals": rec.h1b_approvals, "cap_exempt": rec.cap_exempt,
                             "e_verify": rec.e_verify, "perm_certs": rec.perm_certs,
                             "fy_range": rec._fy_range() if rec.h1b_approvals > 0 else "",
                             "industry": naics_industry(rec.naics), "state": rec.state or "",
                             "matched_name": rec.display_name} if rec else None)
        return jobs

    def close(self) -> None:
        self._conn.close()


def _find_employer_col(headers):
    """Detect the employer/business-name column across the varied E-Verify file
    formats (USCIS export, 2018 historic list, FOIA release) by header text."""
    low = [(h, str(h).strip().lower()) for h in headers]
    for want in ("employer name", "business name", "company name", "legal business name",
                 "employer legal business name", "employer", "business", "company",
                 "organization name", "organization"):
        for h, l in low:
            if l == want:
                return h
    for h, l in low:   # contains employer/business/company, but not a person-name field
        if any(k in l for k in ("employer", "business", "company", "organization")) \
                and not any(s in l for s in ("first", "last", "middle", "contact", "agent", "file")):
            return h
    for h, l in low:
        if l == "name":
            return h
    return None


def _pick(row: dict, keys) -> str:
    """First non-empty value among `keys`, matched case-insensitively (disclosure
    files vary column casing/order across fiscal years)."""
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return str(v).strip()
    return ""


# -- official-file import (PERM / E-Verify: DOL & USCIS block automated bulk
# download, so the person supplies the file they legitimately downloaded) -- #

def parse_perm_xlsx(path) -> list[dict]:
    """Stream a DOL PERM disclosure .xlsx into row dicts (header row + values). Uses
    openpyxl read-only mode so a large file streams without loading fully into memory."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        header = [str(h).strip() if h is not None else "" for h in next(rows)]
    except StopIteration:
        return []
    out = []
    for r in rows:
        out.append({header[i]: r[i] for i in range(min(len(header), len(r)))})
    wb.close()
    return out


def parse_tabular_file(path) -> list[dict]:
    """Parse an official employer file (E-Verify export or similar) — .csv or .xlsx —
    into row dicts by header."""
    p = str(path).lower()
    if p.endswith(".xlsx"):
        return parse_perm_xlsx(path)   # same header+rows shape
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return list(csv.DictReader(f))


# -- download (kept separate from ingest so tests stay offline) ------------- #

H1B_HUB_URL = "https://www.uscis.gov/sites/default/files/document/data/h1b_datahubexport-{fy}.csv"
# USCIS published the Employer Data Hub as one plain CSV per fiscal year for FY2009..FY2023.
# From FY2024 on the data lives only in a Tableau dashboard (no file URL; a person exports a
# crosstab by hand). So FY2023 is the LAST year `download_h1b_rows` can fetch, and a 404 for
# a later year is the normal state of the world, not a failure. Newer years reach the app
# through SponsorJobs's quarterly snapshot (`sync_h1b_snapshot`, built by
# scripts/import_h1b_export.py from a maintainer's dashboard export).
H1B_LAST_FILE_FY = 2023
H1B_FIRST_FILE_FY = 2009
# What a refresh pulls by default: the newest five published files. Older years add little
# (an employer that sponsored in 2015 and never since is weak evidence) and each is ~2 MB.
H1B_DEFAULT_FYS = [2023, 2022, 2021, 2020, 2019]
H1B_DASHBOARD_URL = "https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub"
# Shown to the person when a refresh finds no newer file. Plain, no dashes (UI copy rule).
H1B_REFRESH_NOTE = ("USCIS stopped publishing yearly H-1B files after FY2023. Newer years "
                    "reach SponsorJobs through its quarterly data snapshot, which loads on its own.")


class H1BYearUnavailable(Exception):
    """USCIS publishes no file for this fiscal year (HTTP 404). Expected for FY2024+."""

    def __init__(self, fy: int, url: str = ""):
        super().__init__(f"USCIS publishes no H-1B file for FY{fy}")
        self.fy, self.url = int(fy), url


def current_fiscal_year(today: _dt.date | None = None) -> int:
    """The US federal fiscal year `today` falls in (FY starts 1 October)."""
    d = today or _dt.date.today()
    return d.year + (1 if d.month >= 10 else 0)


def h1b_probe_fiscal_years(today: _dt.date | None = None) -> list[int]:
    """Years newer than the last known file, newest first. A refresh tries these QUIETLY:
    if USCIS ever resumes the files they get picked up, and a 404 is reported as
    'unavailable', never as an error."""
    return list(range(current_fiscal_year(today), H1B_LAST_FILE_FY, -1))


def h1b_default_fiscal_years(today: _dt.date | None = None) -> list[int]:
    """Probe years first (newest data wins the person's attention), then the published ones."""
    return h1b_probe_fiscal_years(today) + list(H1B_DEFAULT_FYS)


def _http_get_bytes(url: str, timeout: int = 90) -> bytes:
    """GET a public open-data file with an HONEST client identifier: no login, no proxy, no
    browser-UA masquerade, no TLS-fingerprint workaround (CLAUDE.md §7). A 404 is passed
    through as urllib's HTTPError so callers can tell 'not published' from 'blocked'."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_get_text(url: str, timeout: int = 90) -> str:
    """Text variant of `_http_get_bytes`. If the host refuses our honest request (e.g. bot
    blocking) we do NOT evade it: we fail with a clear message pointing at the official-file
    import, the SAME path PERM/E-Verify already use. A 404 is re-raised untouched (it means
    'no such file', which `download_h1b_rows` turns into H1BYearUnavailable)."""
    try:
        return _http_get_bytes(url, timeout).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise
        raise RuntimeError(
            f"Couldn't download {url} with an honest client ({exc}). This host may block "
            "non-browser requests. Rather than masquerade as a browser to get past it, "
            "download the official file yourself and use the file-import path (the same way "
            "PERM/E-Verify data is imported).") from exc
    except Exception as exc:
        raise RuntimeError(
            f"Couldn't download {url} with an honest client ({exc}). This host may block "
            "non-browser requests. Rather than masquerade as a browser to get past it, "
            "download the official file yourself and use the file-import path (the same way "
            "PERM/E-Verify data is imported).") from exc


def download_h1b_rows(fy: int = 2023, fetch=None) -> list[dict]:
    """Fetch a USCIS H-1B Employer Data Hub CSV for a fiscal year and parse it into rows.
    `fetch(url) -> csv text` is injectable so this is testable offline. Raises
    H1BYearUnavailable when USCIS has no file for that year (404, i.e. FY2024 onward), so a
    caller can report 'not published' instead of 'download failed'."""
    url = H1B_HUB_URL.format(fy=fy)
    try:
        text = (fetch or _http_get_text)(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise H1BYearUnavailable(fy, url) from exc
        raise
    return list(csv.DictReader(io.StringIO(text)))


# -- header dialects --------------------------------------------------------- #

def _int(v) -> int:
    """'1,234' / ' 12 ' / None / 'FY 2024' -> int; anything unreadable -> 0."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    digits = re.sub(r"[^\d-]", "", str(v))
    try:
        return int(digits) if digits not in ("", "-") else 0
    except ValueError:
        return 0


def _squash(h) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(h or "").lower()).strip()


# Canonical name -> the squashed header variants USCIS has used for it (yearly CSV files,
# Tableau dashboard crosstab export, and the odd renamed column). Matching is on the
# squashed form: lowercased, punctuation collapsed to single spaces.
_H1B_HEADER_VARIANTS = {
    "Fiscal Year": ("fiscal year", "fy", "year", "fiscal yr"),
    "Employer": ("employer", "employer petitioner name", "petitioner name", "petitioner",
                 "employer name", "employer petitioner"),
    "Initial Approval": ("initial approval", "initial approvals", "initial approved",
                         "new employment approval", "new employment approvals"),
    "Initial Denial": ("initial denial", "initial denials", "initial denied"),
    "Continuing Approval": ("continuing approval", "continuing approvals", "continuing approved",
                            "continuation approval", "continuation approvals"),
    "Continuing Denial": ("continuing denial", "continuing denials", "continuing denied"),
    "NAICS": ("naics", "naics code", "industry naics code", "industry code"),
    "Tax ID": ("tax id", "taxid", "tax identification number"),
    "State": ("state", "petitioner state", "employer state"),
    "City": ("city", "petitioner city", "employer city"),
    "ZIP": ("zip", "zip code", "petitioner zip", "postal code"),
}
_H1B_SQUASHED = {v: k for k, vs in _H1B_HEADER_VARIANTS.items() for v in vs}


def h1b_header_map(headers) -> dict:
    """Map each header the file actually has to the canonical yearly-CSV name, or to itself
    when it isn't one we know (extra columns ride along untouched)."""
    return {h: _H1B_SQUASHED.get(_squash(h), h) for h in headers}


def canonical_h1b_rows(rows):
    """Yield rows re-keyed to the canonical USCIS CSV headers ("Fiscal Year", "Employer",
    "Initial Approval", ...), whatever dialect the file used. Rows already canonical pass
    through unchanged (no copy), so the yearly-file path costs nothing extra."""
    hmap = None
    for r in rows:
        if hmap is None:
            hmap = h1b_header_map(r.keys())
            identity = all(k == v for k, v in hmap.items())
        if identity:
            yield r
        else:
            yield {hmap.get(k, k): v for k, v in r.items()}


# -- quarterly snapshot (SponsorJobs's own host) ---------------------------------- #

SEED_COLS = ["norm_name", "display_name", "h1b_approvals", "h1b_last_fy", "h1b_first_fy",
             "naics", "state", "cap_exempt", "e_verify", "perm_certs"]
SEED_INT_COLS = {"h1b_approvals", "h1b_last_fy", "h1b_first_fy", "cap_exempt", "e_verify",
                 "perm_certs"}
SNAPSHOT_MANIFEST = "h1b/latest.json"
SNAPSHOT_FILE = "h1b/latest.csv.gz"
SNAPSHOT_VERSION_KEY = "h1b_snapshot_version"


def read_seed_rows(path_or_bytes):
    """Iterate the seed-layout CSV (gzipped or plain; a path or raw bytes) as dicts."""
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


def sync_h1b_snapshot(db: "SponsorDB", base_url: str, fetch=None) -> dict:
    """Apply SponsorJobs's quarterly H-1B snapshot from `base_url` ({base}/h1b/latest.json and
    {base}/h1b/latest.csv.gz) to `db`. The manifest is {"version", "fiscal_years", "rows"};
    a version already recorded in sponsor_meta is skipped.

    The snapshot is AUTHORITATIVE for the employers it lists and the fiscal-year span it
    declares. It is built by scripts/import_h1b_export.py from the committed seed plus the
    maintainer's dashboard exports, so it supersedes whatever per-year downloads this
    install added on its own: each listed employer's H-1B total and first/last FY are SET
    from the snapshot (SponsorDB.apply_h1b_snapshot_rows), and `h1b_fys_ingested` is SET to
    the snapshot's `fiscal_years`. Years outside that span are therefore no longer counted
    as ingested and the next refresh adds them back on top, so a local FY the snapshot
    does not carry is re-fetched rather than silently lost or double counted. Employers the
    snapshot does not list are untouched, and PERM / E-Verify columns are never lowered.

    An unreachable or malformed host is reported in the result, never raised: the snapshot
    is a convenience on top of the bundled data. `fetch(url) -> bytes` is injectable for
    offline tests."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return {"status": "disabled"}
    get = fetch or _http_get_bytes
    try:
        manifest = json.loads(get(f"{base}/{SNAPSHOT_MANIFEST}").decode("utf-8"))
        version = str(manifest.get("version") or "").strip()
        if not version:
            return {"status": "invalid", "reason": "manifest has no version"}
    except Exception as exc:                        # noqa: BLE001 - offline is normal
        return {"status": "unreachable", "reason": str(exc)[:200]}
    if db.get_meta(SNAPSHOT_VERSION_KEY) == version:
        return {"status": "up_to_date", "version": version}
    try:
        rows = read_seed_rows(get(f"{base}/{SNAPSHOT_FILE}"))
        merged = db.apply_h1b_snapshot_rows(rows)
    except Exception as exc:                        # noqa: BLE001
        return {"status": "unreachable", "reason": str(exc)[:200], "version": version}
    fys = {int(x) for x in (manifest.get("fiscal_years") or []) if str(x).strip()}
    if fys:
        db.set_h1b_fys_ingested(fys)       # the snapshot's span, exactly (not a union)
    db.set_meta(SNAPSHOT_VERSION_KEY, version)
    return {"status": "merged", "version": version, "rows": merged,
            "fiscal_years": sorted(fys)}
