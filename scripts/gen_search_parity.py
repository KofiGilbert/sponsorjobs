#!/usr/bin/env python3
"""Write the search parity fixture: synthetic feed rows, the loader's SQL for them, and ~50 facet
mixes with the IDs the app's own filters answer, in order.

    python scripts/gen_search_parity.py          # rewrites worker/test/fixtures/search-parity.json

The app is the reference: expected IDs come from sourcing/filters.apply_facets (the /api/jobs
path, with ui/app._facets' parameter handling) and, for the candidates endpoint, from the
picker's narrowing (autoapply/picker.candidates: its facet filter, then _matches_focus on the
titles). worker/test/search-parity.test.ts loads the SQL into a local D1 and expects the Worker to
answer the same IDs in the same order. tests/test_search_index.py fails when this file is out of
date with the Python code, so a change to apply_facets cannot silently drift from the Worker.

The clock is pinned (NOW below) on both sides, so the date facet gives the same answer whenever the
tests run. The rows are made up: no real posting is committed.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.load_search_index import diff, meta_statement, statements  # noqa: E402
from sourcing import filters  # noqa: E402
from sourcing.feedfile import recency_key  # noqa: E402

OUT = ROOT / "worker" / "test" / "fixtures" / "search-parity.json"
NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
INDEX_VERSION = "parity-1"

COMPANIES = ["Acme Corp", "O'Reilly Media", 'Quote "Labs"', "Zürich Analytics", "NEAR Protocol",
             "Star*Co", "AND/OR Partners", "Initech", "Globex", "Data Dynamics", "Ångström AB",
             "Hooli", "c++ shop", "Engine Works", "Vandelay Industries"]
TITLES = ["Software Engineer", "Senior Software Engineer", "Data Analyst", "Senior Data Analyst",
          "BI Analyst", "Analyst, Data", "Data Analyst II", "Engineering Manager", "C++ Developer",
          "Nurse (RN)", "Ingénieur Logiciel", "Product Manager", "Machine Learning Engineer",
          "Database Administrator", "Analytics Engineer", "DataAnalyst Intern", "Sr. BI Analyst",
          'The "Best" Engineer', "Java*Script Dev", "Accountant"]
LOCATIONS = ["New York, NY", "Remote", "Remote - US", "San Francisco, CA", "Zürich", "",
             "Austin, Texas", "Boston, Massachusetts, United States", "Seattle, WA", "Brooklyn, NY"]
REMOTES = ["", "", "remote", "onsite", "hybrid", "Remote"]
SALARIES = ["", "", "$120k-$160k/yr", "$45-$60/hr", "$135k/yr", "80000", "$5,000/mo",
            "competitive", "$100k/yr", "$150,000"]
VISA = [("H-1B", "H-1B sponsor"), ("GREEN-CARD", "Green card"), ("STEM-OPT", "STEM OPT"),
        ("CAP-EXEMPT", "Cap exempt")]
NAT = [("E-3", "E-3"), ("TN", "TN"), ("H-1B1", "H-1B1")]


def _date(days_ago: float, style: int) -> str:
    d = NOW - timedelta(days=days_ago)
    if style == 0:
        return d.strftime("%Y-%m-%d")
    if style == 1:
        return d.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return d.strftime("%Y-%m-%d %H:%M:%S")


def rows(n: int = 420) -> list[dict]:
    rnd = random.Random(20261024)
    out = []
    for i in range(n):
        company = rnd.choice(COMPANIES)
        r = {"source_id": f"gh:{company[:4].lower()}:{i:04d}", "source": rnd.choice(["greenhouse", "lever", "careersite"]),
             "company": company, "title": rnd.choice(TITLES), "location": rnd.choice(LOCATIONS),
             "remote": rnd.choice(REMOTES), "url": f"https://jobs.example/{i}",
             "salary": rnd.choice(SALARIES), "us": True, "entry_level": rnd.random() < 0.3,
             "tags": ["Engineering"]}
        k = rnd.random()
        if k < 0.1:
            r["posted_at"] = ""                                  # undated
        elif k < 0.13:
            r["posted_at"] = "yesterday"                         # unreadable: kept by the date facet
        else:
            # Whole days plus a fixed hour, so no row sits on a facet's cut-off second.
            r["posted_at"] = _date(rnd.choice([0, 1, 2, 3, 6, 8, 13, 15, 29, 31, 44]) + 0.25, rnd.randrange(3))
        if i % 17 == 0 and out:                                  # exact recency ties: source_id decides
            r["posted_at"] = out[-1].get("posted_at", "")
        r["first_seen"] = "" if rnd.random() < 0.05 else _date(rnd.randrange(0, 10), 2)
        if i % 17 == 0 and out:
            r["first_seen"] = out[-1]["first_seen"]
        if rnd.random() < 0.7:
            r["visa"] = [{"code": c, "label": lab, "basis": f"{company} record {c}"}
                         for c, lab in VISA if rnd.random() < 0.5]
            r["nationality_visas"] = [{"code": c, "label": lab} for c, lab in NAT if rnd.random() < 0.4]
            r["sponsor"] = {"matched_name": company, "h1b_approvals": rnd.randrange(0, 500)}
        st = rnd.random()
        if st < 0.1:
            r["ad_stance"], r["ad_sentence"] = "offered", "We sponsor visas."
        elif st < 0.15:
            r["ad_stance"], r["ad_sentence"] = "not_offered", "No sponsorship."
        if rnd.random() < 0.2:
            r["sponsorship_stated"] = rnd.choice([True, False, None])
        out.append(r)
    out.sort(key=recency_key, reverse=True)                      # the feed's own order
    return out


SEARCH_CASES = [
    {}, {"sort": "pay"}, {"sort": "recent"},
    {"q": "engineer"}, {"q": "ENGINEER"}, {"q": "  data analyst  "}, {"q": "an"}, {"q": "c+"},
    {"q": "c++"}, {"q": "o'reilly"}, {"q": '"labs"'}, {"q": '"best"'}, {"q": "near"},
    {"q": "near protocol"}, {"q": "star*"}, {"q": "and/or"}, {"q": "zürich"}, {"q": "ångström"},
    {"q": "data AND analyst"}, {"q": "title:engineer"}, {"q": "analyst, data"}, {"q": "nomatchxyz"},
    {"loc": "remote"}, {"loc": "ny"}, {"loc": "New York"}, {"loc": "texas"}, {"loc": "zürich"},
    {"days": "7"}, {"days": "30"}, {"days": "-1"}, {"days": "3", "sort": "pay"},
    {"remote": "remote"}, {"remote": "hybrid"}, {"remote": "Remote"},
    {"visa": "H-1B"}, {"visa": "H-1B,STEM-OPT"}, {"visa": "SPONSORED"}, {"visa": "TN"},
    {"visa": "SPONSORED,E-3"}, {"level": "entry"}, {"pay": "100000"}, {"pay": "150000", "sort": "pay"},
    {"sponsored": "1"}, {"sponsored": "true"}, {"sponsored": "0"},
    {"q": "analyst", "loc": "ny", "sort": "pay"},
    {"q": "engineer", "visa": "H-1B", "days": "14", "level": "entry"},
    {"loc": "remote", "remote": "remote", "pay": "90000"},
    {"q": "data", "sponsored": "1", "visa": "GREEN-CARD"},
    {"q": "eng", "loc": "san", "days": "30", "sort": "pay", "pay": "50000"},
]
CANDIDATE_CASES = [
    {"titles": "Data Analyst|BI Analyst"}, {"titles": "engineer"}, {"titles": "c++ developer"},
    {"titles": "an"}, {"titles": "Software Engineer", "visa": "H-1B", "sort": "pay"},
    {"titles": "data analyst", "loc": "ny"}, {"titles": "analyst|nurse (rn)", "days": "30"},
]


def _search_expected(rs: list[dict], params: dict) -> list[dict]:
    # ui/app._facets: how /api/jobs hands the query string to apply_facets.
    a = params
    facets = dict(q=a.get("q", ""), loc=a.get("loc", ""), days=a.get("days", 0),
                  remote=a.get("remote", ""),
                  visa=[v for v in a.get("visa", "").split(",") if v], level=a.get("level", ""),
                  pay=a.get("pay", 0), sort=a.get("sort", ""), sponsored=a.get("sponsored", ""))
    return filters.apply_facets(rs, **facets)


def _candidates_expected(rs: list[dict], params: dict) -> list[dict]:
    from autoapply.picker import _matches_focus
    titles = [t for t in params.get("titles", "").split("|") if str(t).strip()]
    facets = {k: v for k, v in params.items() if k != "titles"}
    return [j for j in _search_expected(rs, facets) if _matches_focus(j, titles, [])]


class _FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def build() -> dict:
    rs = rows()
    upserts, removed, _ = diff(rs, {})
    sqls = statements(upserts, removed) + [meta_statement(INDEX_VERSION, NOW.isoformat())]
    cases = []
    with mock.patch.object(filters, "datetime", _FixedNow):
        for mode, group, fn in (("search", SEARCH_CASES, _search_expected),
                                ("candidates", CANDIDATE_CASES, _candidates_expected)):
            for params in group:
                got = fn(rs, params)
                cases.append({"mode": mode, "params": params, "ids": [j["source_id"] for j in got]})
    return {"now_ms": int(NOW.timestamp() * 1000), "index_version": INDEX_VERSION,
            "row_count": len(rs), "statements": sqls, "cases": cases,
            # One whole row with employer fields: the Worker must hand it back unchanged.
            "sample": next(r for r in rs if r.get("visa") and r.get("nationality_visas"))}


def render() -> str:
    return json.dumps(build(), ensure_ascii=False, indent=1) + "\n"


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(), encoding="utf-8")
    d = json.loads(OUT.read_text(encoding="utf-8"))
    print(f"wrote {OUT.relative_to(ROOT)}: {d['row_count']} rows, {len(d['cases'])} cases")
