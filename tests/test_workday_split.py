"""Workday boards past the 2,000-row list ceiling: split by the board's own facets
(sourcing/workday.py split_plan / list_postings). Offline: a fake cxs endpoint that pages,
wraps past the end, filters on appliedFacets (OR within a facet, AND across facets) and
returns conditional facet counts the way the live API does (a facet's own counts ignore its
own filter)."""

from __future__ import annotations

import pytest

from sourcing import workday

HOST, SITE = "cvshealth.wd1.myworkdayjobs.com", "CVS_Health_Careers"


class FacetedBoard:
    """postings: [{"id": n, "jobFamilyGroup": "fam", "timeType": "tt", "locations": "loc", ...}]."""

    PARAMS = ("jobFamilyGroup", "timeType", "locations", "locationCountry")

    def __init__(self, postings, ceiling=workday.LIST_CEILING, extra_facets=()):
        self.postings = postings
        self.ceiling = ceiling
        self.extra_facets = list(extra_facets)
        self.posts: list[dict] = []

    def _match(self, p, applied, skip=None):
        return all(p.get(k) in set(v) for k, v in applied.items() if k != skip)

    def __call__(self, url, timeout=20, data=None, raw=False):
        assert url.endswith("/jobs") and data is not None
        self.posts.append(data)
        applied = data.get("appliedFacets") or {}
        rows = [p for p in self.postings if self._match(p, applied)]
        offset, limit = int(data["offset"]), int(data["limit"])
        if offset >= min(len(rows), self.ceiling):
            offset = 0                                  # Workday wraps past the end / the ceiling
        page = rows[offset:offset + limit]
        facets = []
        for param in self.PARAMS:
            counts: dict[str, int] = {}
            for p in self.postings:
                if param in p and self._match(p, applied, skip=param):
                    counts[p[param]] = counts.get(p[param], 0) + 1
            if counts:
                facets.append({"facetParameter": param, "values": [
                    {"id": k, "descriptor": k.split(":", 1)[-1], "count": n}
                    for k, n in counts.items()]})
        return {"total": len(rows), "facets": facets + self.extra_facets,
                "jobPostings": [{"title": f"Job {p['id']}", "externalPath": f"/job/X/Job_R{p['id']}",
                                 "locationsText": "Austin, TX", "postedOn": "Posted Today",
                                 "bulletFields": [f"R{p['id']}"]} for p in page]}


def _ids(rows):
    return {p["externalPath"] for p in rows}


def _board(spec):
    """spec: [(count, {facet: value})] -> postings with ids, facet ids as the fake names them."""
    out, n = [], 0
    for count, attrs in spec:
        for _ in range(count):
            out.append({"id": n, **{k: f"{k}:{v}" for k, v in attrs.items()}})
            n += 1
    return out


def test_a_board_under_the_ceiling_is_read_as_one_query_whatever_the_cap():
    fake = FacetedBoard(_board([(150, {"jobFamilyGroup": "a", "timeType": "full"})]), ceiling=100)
    _, rows = workday.list_postings("cvshealth", SITE, HOST, fake, max_jobs=25000, sleep=lambda s: None)
    # 150 rows but a 100-row ceiling: split. With one family value and one time value there is no
    # facet with two values, so only the first 100 are reachable and no split query is made.
    assert len(rows) == 100 and all(p["appliedFacets"] == {} for p in fake.posts)


def test_a_giant_board_is_split_by_its_facets_and_every_posting_is_listed_once():
    # 330 rows, a 100-row ceiling (the real one is 2,000): families of 60 / 50 / 40 / 180; the
    # 180 family is split again by full / part time (90 + 90).
    spec = [(60, {"jobFamilyGroup": "pharm", "timeType": "full"}),
            (50, {"jobFamilyGroup": "tech", "timeType": "full"}),
            (40, {"jobFamilyGroup": "clin", "timeType": "part"}),
            (90, {"jobFamilyGroup": "retail", "timeType": "full"}),
            (90, {"jobFamilyGroup": "retail", "timeType": "part"})]
    fake = FacetedBoard(_board(spec), ceiling=100)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(workday, "LIST_CEILING", 100)
        total, rows = workday.list_postings("cvshealth", SITE, HOST, fake, max_jobs=25000,
                                            sleep=lambda s: None, split_seconds=999, rotation=0)
    assert total == 330 and len(rows) == 330 and len(_ids(rows)) == 330
    queries = {tuple(sorted((k, tuple(v)) for k, v in p["appliedFacets"].items())) for p in fake.posts}
    # small families are OR-ed into groups of at most 100 rows; retail (180) goes one deeper.
    assert (("jobFamilyGroup", ("jobFamilyGroup:retail",)), ("timeType", ("timeType:full",))) in queries
    assert (("jobFamilyGroup", ("jobFamilyGroup:retail",)), ("timeType", ("timeType:part",))) in queries
    assert any(k == (("jobFamilyGroup", ("jobFamilyGroup:pharm", "jobFamilyGroup:clin")),) or
               k == (("jobFamilyGroup", ("jobFamilyGroup:pharm", "jobFamilyGroup:tech")),) for k in queries)


def test_split_plan_packs_small_values_and_never_uses_a_huge_facet():
    vals = [("a", 1200), ("b", 700), ("c", 500), ("d", 300), ("e", 2500)]
    assert workday.pack_values([v for v in vals if v[1] <= 2000]) == [["a", "b"], ["c", "d"]]
    many = [{"facetParameter": "locations", "values": [{"id": f"l{i}", "count": 3} for i in range(5000)]}]
    assert workday.pick_split_facet(many, {}, 15000) is None
    country = [{"facetParameter": "locationCountry", "values": [{"id": "us", "count": 9000},
                                                                {"id": "ca", "count": 9000}]}]
    assert workday.pick_split_facet(country, {}, 18000) is None        # the US filter stays put
    plan = workday.split_plan({}, 5200, [{"facetParameter": "jobFamilyGroup",
                                          "values": [{"id": k, "count": n} for k, n in vals]}],
                              probe=lambda applied: {})
    assert plan[:2] == [{"jobFamilyGroup": ["a", "b"]}, {"jobFamilyGroup": ["c", "d"]}]
    assert plan[2] == {"jobFamilyGroup": ["e"]}                          # probe gave no facets: kept


def test_a_split_keeps_the_us_country_filter_and_marks_its_rows():
    spec = [(80, {"locationCountry": "us", "jobFamilyGroup": "a"}),
            (70, {"locationCountry": "us", "jobFamilyGroup": "b"}),
            (90, {"locationCountry": "in", "jobFamilyGroup": "a"})]
    fake = FacetedBoard(_board(spec), ceiling=100)
    orig = fake.__call__

    def call(url, **kw):                                # the US value's descriptor, as Workday spells it
        d = orig(url, **kw)
        for f in d["facets"]:
            for v in f["values"]:
                if v["id"] == "locationCountry:us":
                    v["descriptor"] = "United States of America"
        return d
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(workday, "LIST_CEILING", 100)
        _, rows = workday.list_postings("cvshealth", SITE, HOST, call, max_jobs=25000, us_only=True,
                                        sleep=lambda s: None, split_seconds=999, rotation=0)
    assert len(rows) == 150 and all(p.get("_us_facet") for p in rows)
    assert all(p["appliedFacets"].get("locationCountry") == ["locationCountry:us"]
               for p in fake.posts[1:])


def test_split_queries_stop_at_the_time_budget_and_rotate_between_runs():
    spec = [(60, {"jobFamilyGroup": f"f{i}"}) for i in range(6)]          # 360 rows, ceiling 100
    clock = {"t": 0.0}

    def tick(_s=0):
        clock["t"] += 1.0                              # every request / gap costs a second

    def run(rotation, budget):
        fake = FacetedBoard(_board(spec), ceiling=100)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(workday, "LIST_CEILING", 100)
            _, rows = workday.list_postings("cvshealth", SITE, HOST, fake, max_jobs=25000, sleep=tick,
                                            split_seconds=budget, rotation=rotation,
                                            clock=lambda: clock["t"])
        return fake, rows

    _, full = run(0, 10_000)
    assert len(full) == 360
    fake_a, a = run(0, 9)
    fake_b, b = run(1, 9)
    assert 100 < len(a) < 360 and 100 < len(b) < 360
    first_split = lambda f: next(p["appliedFacets"] for p in f.posts if p["appliedFacets"])  # noqa: E731
    assert first_split(fake_a) != first_split(fake_b)                   # a different query first


def test_the_desktop_default_cap_never_splits():
    fake = FacetedBoard(_board([(80, {"jobFamilyGroup": "a"}), (80, {"jobFamilyGroup": "b"})]), ceiling=100)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(workday, "LIST_CEILING", 100)
        _, rows = workday.list_postings("cvshealth", SITE, HOST, fake, max_jobs=100, sleep=lambda s: None)
    assert len(rows) == 100 and all(p["appliedFacets"] == {} for p in fake.posts)


def test_a_tenant_that_caps_total_itself_is_sized_by_its_facet_counts():
    # Bridgestone (live 2026-10-23) reports total 2000 while its facets count 3,088 US jobs.
    spec = [(70, {"jobFamilyGroup": "a"}), (70, {"jobFamilyGroup": "b"})]
    fake = FacetedBoard(_board(spec), ceiling=100)
    orig = fake.__call__

    def capped(url, **kw):
        d = orig(url, **kw)
        d["total"] = min(d["total"], 100)
        return d
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(workday, "LIST_CEILING", 100)
        total, rows = workday.list_postings("cvshealth", SITE, HOST, capped, max_jobs=25000,
                                            sleep=lambda s: None, split_seconds=999, rotation=0)
    assert total == 140 and len(rows) == 140


def test_a_failure_during_the_split_keeps_the_rows_already_listed():
    spec = [(70, {"jobFamilyGroup": "a"}), (70, {"jobFamilyGroup": "b"})]
    fake = FacetedBoard(_board(spec), ceiling=100)

    def flaky(url, **kw):
        if kw["data"]["appliedFacets"].get("jobFamilyGroup") == ["jobFamilyGroup:b"]:
            raise TimeoutError("The read operation timed out")    # what a slow tenant does live
        return fake(url, **kw)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(workday, "LIST_CEILING", 100)
        _, rows = workday.list_postings("cvshealth", SITE, HOST, flaky, max_jobs=25000,
                                        sleep=lambda s: None, split_seconds=999, rotation=0)
    assert len(rows) == 100                    # the base query's rows survive, the split stops
