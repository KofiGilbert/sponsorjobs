"""Tests for the visa-sponsor overlay (GREEN lane, public gov data).

Ingestion, aggregation across a company's many filing entities, the CONSERVATIVE
name matching (no false-positive sponsor badges), cap-exempt derivation, and job
tagging — all offline against canned rows shaped like the USCIS H-1B Data Hub CSV.
"""

from __future__ import annotations

from sourcing.sponsors import SponsorDB, normalize_employer

# Rows shaped exactly like the USCIS H-1B Employer Data Hub CSV headers.
def _row(employer, initial=0, cont=0, naics="54", state="CA", fy=2023):
    return {"Fiscal Year": str(fy), "Employer": employer, "Initial Approval": str(initial),
            "Initial Denial": "0", "Continuing Approval": str(cont), "Continuing Denial": "0",
            "NAICS": naics, "Tax ID": "1234", "State": state, "City": "X", "ZIP": "00000"}


H1B_ROWS = [
    _row("AMAZON COM SERVICES LLC", 2, 30),
    _row("AMAZON DATA SERVICES INC", 4, 100),
    _row("AMAZON WEB SERVICES INC", 10, 200),
    _row("GOOGLE LLC", 50, 400),
    _row("IBM CORPORATION", 20, 80),
    _row("META PLATFORMS INC", 30, 120),
    _row("UNIVERSITY OF ILLINOIS AT CHICAGO", 5, 40, naics="61", state="IL"),
    _row("FREEMAN HEALTH SYSTEM", 1, 0, naics="62", state="MO"),
    _row("0965688 BC LTD DBA PROCOGIA", 0, 1, naics="51", state="WA"),
]


def _db():
    db = SponsorDB(":memory:")
    db.ingest_h1b_rows(H1B_ROWS)
    return db


def test_normalize_strips_legal_suffixes():
    assert normalize_employer("Amazon Com Services LLC") == "amazon com services"
    assert normalize_employer("Google LLC") == "google"
    assert normalize_employer("IBM Corporation") == "ibm"
    assert normalize_employer("The Foo Inc.") == "foo"
    assert normalize_employer("A&B Partners, LLP") == "a and b partners"


def test_ingest_aggregates_entities():
    db = _db()
    # Amazon files under 3 entities -> one aggregated record summing all approvals.
    amazon = db.lookup("Amazon")
    assert amazon is not None
    assert amazon.h1b_approvals == (2 + 30) + (4 + 100) + (10 + 200)   # 346
    assert any(b["code"] == "H-1B" for b in amazon.badges())


def test_exact_and_prefix_matching():
    db = _db()
    assert db.lookup("Amazon Web Services").h1b_approvals == 210   # exact entity
    assert db.lookup("Google").h1b_approvals == 450                # GOOGLE LLC -> google
    # DBA trade name is indexed, so the posting's brand matches.
    assert db.lookup("ProCogia") is not None


def test_conservative_no_false_positives():
    db = _db()
    assert db.lookup("Some Random Cafe LLC") is None
    assert db.lookup("Amazonia Travel") is None       # not a whole-word prefix of Amazon*
    # A short single token that isn't an exact entity or a known brand alias must NOT
    # risk a partial match -- conservative by design.
    assert db.lookup("Met") is None                   # too short/partial -> no match
    assert db.lookup("Goo") is None                   # not a prefix of "google" at a word boundary
    assert db.lookup("Meta Platforms").h1b_approvals == 150   # the full name still matches


def test_single_token_query_does_not_borrow_an_unrelated_prefix():
    """A single-token company name must match EXACTLY — it can't inherit an unrelated
    employer's badge just because that employer's name starts with the same word.
    'Nowhere Corp' -> 'nowhere' must NOT match the real, distinct 'Nowhere Partners'."""
    db = SponsorDB(":memory:")
    db.ingest_h1b_rows([_row("NOWHERE PARTNERS LLC", 3, 0)])
    assert db.lookup("Nowhere Corp") is None          # single token, not a curated brand
    assert db.lookup("Nowhere") is None
    assert db.lookup("Nowhere Partners") is not None  # the real employer still matches (exact)

    # The precision guard keeps the legitimate expansions working:
    db.ingest_h1b_rows(H1B_ROWS)
    assert db.lookup("Amazon").h1b_approvals == 346               # curated brand -> subsidiaries
    assert db.lookup("University of Illinois").cap_exempt is True  # multi-token -> "...at Chicago"


def test_cap_exempt_from_naics_and_name():
    db = _db()
    uni = db.lookup("University of Illinois")
    assert uni.cap_exempt is True                     # NAICS 61 + name
    assert any(b["code"] == "CAP-EXEMPT" for b in uni.badges())
    assert db.lookup("Freeman Health System").cap_exempt is True   # name pattern
    assert db.lookup("Google").cap_exempt is False


def test_cap_exempt_recall_covers_labs_hospitals_and_med_schools():
    """Cap-exempt (no H-1B lottery) also covers national labs, children's hospitals, cancer
    centers, and med schools, while staying conservative on ordinary companies."""
    from sourcing.sponsors import _is_cap_exempt
    for name in ("Argonne National Laboratory", "Boston Children's Hospital",
                 "MD Anderson Cancer Center", "Stanford School of Medicine",
                 "Rensselaer Polytechnic Institute"):
        assert _is_cap_exempt("54", name) is True, name
    for name in ("Google", "DataLab Inc", "Innovation Academy", "Health Gadgets"):
        assert _is_cap_exempt("54", name) is False, name      # no false positives


def test_every_badge_carries_an_honest_basis():
    """A badge must never read as a present-tense promise. Every badge the UI/extension
    renders carries a `basis` line stating it's drawn from PAST public filings and is not a
    guarantee — the caveat travels WITH the badge, not just in some tooltip elsewhere."""
    db = _db()
    for company in ("Google", "University of Illinois", "Amazon"):
        rec = db.lookup(company)
        assert rec and rec.badges(), f"expected badges for {company}"
        for b in rec.badges():
            assert b.get("basis"), f"{company} badge {b['code']} has no basis line"
            assert "not a" in b["basis"].lower() or "heuristic" in b["basis"].lower() \
                or "can lapse" in b["basis"].lower(), \
                f"{company} badge {b['code']} basis doesn't disclaim: {b['basis']!r}"


def test_stem_opt_badge_does_not_overclaim_eligibility():
    """E-Verify enrollment is REQUIRED for a STEM-OPT hire but not sufficient, so the badge
    states the fact (E-Verify) and never asserts the student is "eligible"."""
    from sourcing.sponsors import SponsorRecord
    rec = SponsorRecord("X", 0, 0, "54", "CA", cap_exempt=False, e_verify=True)
    stem = [b for b in rec.badges() if b["code"] == "STEM-OPT"]
    assert stem and "eligible" not in stem[0]["label"].lower()


def test_canonical_disclaimer_exists_and_is_honest():
    from sourcing.sponsors import SPONSOR_DISCLAIMER
    assert "not a guarantee" in SPONSOR_DISCLAIMER.lower()
    assert "past" in SPONSOR_DISCLAIMER.lower()


def test_tag_jobs_attaches_badges():
    db = _db()
    jobs = [
        {"company": "Google", "title": "SWE", "location": "Mountain View, CA"},
        {"company": "University of Illinois at Chicago", "title": "Data Analyst",
         "location": "Chicago, IL"},
        {"company": "Some Random Cafe", "title": "Barista", "location": "Chicago, IL"},
    ]
    db.tag_jobs(jobs)
    assert [b["code"] for b in jobs[0]["visa"]] == ["H-1B"]
    assert set(b["code"] for b in jobs[1]["visa"]) == {"H-1B", "CAP-EXEMPT"}
    assert jobs[2]["visa"] == [] and jobs[2]["sponsor"] is None


def test_a_us_visa_badge_never_lands_on_a_non_us_role():
    """Sponsor data is per COMPANY, so tagging every row from a matched employer put
    "H-1B sponsor" on Spotify's Stockholm listings and Dropbox's Remote-Poland ones: 40 of
    133 rows in a real feed. H-1B, PERM and E-Verify are US instruments and mean nothing in
    London, and a badge there is worse than none, because it invites someone who needs
    sponsorship to spend an application on an impossibility."""
    db = _db()
    jobs = [
        {"company": "Google", "title": "SWE", "location": "London"},
        {"company": "Google", "title": "SWE", "location": "Remote - Poland"},
        {"company": "Google", "title": "SWE", "location": "New York, NY"},
    ]
    db.tag_jobs(jobs)
    assert jobs[0]["visa"] == [], "H-1B badge on a London role"
    assert jobs[1]["visa"] == [], "H-1B badge on a Poland role"
    assert [b["code"] for b in jobs[2]["visa"]] == ["H-1B"]
    # The employer fact is NOT lost, only the claim about this role: Google does sponsor,
    # which is worth knowing even on a London posting.
    assert all(j["sponsor"] and j["sponsor"]["h1b_approvals"] > 0 for j in jobs)


def test_extension_lookup_gates_the_badge_on_the_role_location(tmp_path, monkeypatch):
    """The same per-company badge bug as the feed (#73), but live on LinkedIn, which is
    where people actually browse. /api/sponsors/lookup took only ?company=, so opening a
    Spotify listing in London showed an "H-1B sponsor" badge on it."""
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    client = app.app.test_client()
    hdr = {"X-Tailor-Extension": "1"}

    london = client.get("/api/sponsors/lookup?company=Google&location=London",
                        headers=hdr).get_json()
    ny = client.get("/api/sponsors/lookup?company=Google&location=New%20York,%20NY",
                    headers=hdr).get_json()
    assert london["visa"] == [], "H-1B badge on a London role"
    assert london["role_in_us"] is False
    assert ny["role_in_us"] is True
    # The employer-level fact survives on both, so the London view can still say something
    # true ("sponsors in the US, not this role") rather than nothing at all.
    assert london["matched"] == ny["matched"]
    assert london["sponsor_employer"] == ny["sponsor_employer"]


def test_extension_lookup_without_a_location_withholds_the_badge(tmp_path, monkeypatch):
    """An unread location can't be judged, so it loses the badge rather than guessing.

    role_in_us is None here, not False. This test used to assert False and was therefore
    encoding the bug: False is a CLAIM ("this job is not in the US") and it reached a user
    as "Sponsors in the US, not this role" on a job in Dallas. Withholding a badge and
    asserting a job is abroad are different acts, and only one of them is honest here.
    """
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    d = app.app.test_client().get("/api/sponsors/lookup?company=Google",
                                  headers={"X-Tailor-Extension": "1"}).get_json()
    assert d["visa"] == []              # still no badge: we are not sure
    assert d["role_in_us"] is None      # ...but we do not pretend to know it is abroad


def test_tag_jobs_publishes_the_us_flag_for_filtering():
    """The feed filters on this, so it's computed once here next to the badge it gates,
    not re-derived in the UI from the same location string."""
    db = _db()
    jobs = [{"company": "Google", "title": "SWE", "location": "New York, NY"},
            {"company": "Google", "title": "SWE", "location": "London"}]
    db.tag_jobs(jobs)
    assert jobs[0]["us"] is True and jobs[1]["us"] is False


def test_an_unreadable_location_loses_the_badge_rather_than_guessing():
    """This module's own rule: a wrong sponsor badge is worse than a missing one."""
    db = _db()
    jobs = [{"company": "Google", "title": "SWE", "location": ""},
            {"company": "Google", "title": "SWE", "location": "Somewhere Fictional"}]
    db.tag_jobs(jobs)
    assert all(j["visa"] == [] for j in jobs)


def test_brand_aliases_resolve_to_filing_entity():
    """A posting's bare brand ('Meta', 'AWS') maps to the entity that actually files,
    so it isn't missed by the conservative matcher."""
    db = _db()
    assert db.lookup("Meta").h1b_approvals == 150            # META -> meta platforms
    assert db.lookup("Facebook").h1b_approvals == 150        # brand -> meta platforms
    assert db.lookup("AWS").h1b_approvals == 210             # -> amazon web services


def test_multi_year_ingest_aggregates_and_tracks_fy_range():
    """Ingesting several fiscal years sums approvals and records the FY span."""
    db = SponsorDB(":memory:")
    db.ingest_h1b_rows([_row("GOOGLE LLC", 10, 40, fy=2021)])
    db.ingest_h1b_rows([_row("GOOGLE LLC", 20, 80, fy=2023)])
    g = db.lookup("Google")
    assert g.h1b_approvals == 10 + 40 + 20 + 80              # summed across years
    assert g.h1b_first_fy == 2021 and g.h1b_last_fy == 2023
    assert "FY2021 to 2023" in g.badges()[0]["detail"]   # dash-free copy (no en-dash range)


def test_perm_ingest_adds_green_card_badge():
    """DOL PERM certified cases -> perm_certs -> a Green-card badge, aggregated onto
    the same employer that already has H-1B history."""
    db = _db()   # has GOOGLE LLC from H-1B rows
    perm = [
        {"EMPLOYER_NAME": "GOOGLE LLC", "CASE_STATUS": "Certified"},
        {"EMPLOYER_NAME": "GOOGLE LLC", "CASE_STATUS": "Certified-Expired"},
        {"EMPLOYER_NAME": "GOOGLE LLC", "CASE_STATUS": "Denied"},          # not counted
        {"EMPLOYER_NAME": "NEWCO PERM ONLY LLC", "CASE_STATUS": "Certified"},
    ]
    db.ingest_perm_rows(perm)
    g = db.lookup("Google")
    assert g.perm_certs == 2                                    # only the 2 certified
    codes = [b["code"] for b in g.badges()]
    assert "H-1B" in codes and "GREEN-CARD" in codes           # both flags on one card
    # An employer seen only in PERM still becomes a green-card sponsor.
    nc = db.lookup("Newco Perm Only")
    assert nc and nc.perm_certs == 1 and nc.h1b_approvals == 0


def test_bundled_perm_counts_ingest():
    """The pre-aggregated bundled green-card list (EMPLOYER_NAME, PERM_CERTS) that ships
    with the app ingests straight into perm_certs."""
    db = _db()   # has GOOGLE LLC
    n = db.ingest_perm_counts([
        {"EMPLOYER_NAME": "GOOGLE LLC", "PERM_CERTS": "1200"},
        {"EMPLOYER_NAME": "SOME BUNDLED CO LLC", "PERM_CERTS": "3"},
        {"EMPLOYER_NAME": "IGNORED", "PERM_CERTS": "0"},   # zero -> skipped
    ])
    assert n == 2
    assert db.lookup("Google").perm_certs == 1200
    assert any(b["code"] == "GREEN-CARD" for b in db.lookup("Google").badges())


def test_everify_ingest_adds_stem_opt_badge():
    db = _db()
    db.ingest_everify_rows([{"Employer Name": "GOOGLE LLC"},
                            {"Employer Name": "Freeman Health System"}])
    assert db.lookup("Google").e_verify is True
    assert any(b["code"] == "STEM-OPT" for b in db.lookup("Google").badges())


def test_everify_import_detects_varied_employer_columns():
    """The E-Verify import must handle whatever the official file uses — the public
    2018 list ('Business Name'), a FOIA release, etc. — by detecting the name column."""
    from sourcing.sponsors import _find_employer_col
    assert _find_employer_col(["Business Name", "State", "Workforce Size"]) == "Business Name"
    assert _find_employer_col(["First Name", "Last Name", "Company Name"]) == "Company Name"
    assert _find_employer_col(["EMPLOYER_LEGAL_BUSINESS_NAME", "CITY"]) == "EMPLOYER_LEGAL_BUSINESS_NAME"
    assert _find_employer_col(["foo", "bar"]) is None   # nothing name-like -> no false pick
    # 2018-Kaggle-shaped rows ingest and tag STEM-OPT.
    db = _db()
    db.ingest_everify_rows([
        {"Business Name": "GOOGLE LLC", "Federal Contractor": "No", "State": "CA"},
        {"Business Name": "SOME LOCAL BAKERY LLC", "Federal Contractor": "No", "State": "IL"},
    ])
    assert db.lookup("Google").e_verify is True
    assert db.lookup("Some Local Bakery").e_verify is True


def test_stats_counts_by_flag():
    db = _db()
    db.ingest_perm_rows([{"EMPLOYER_NAME": "GOOGLE LLC", "CASE_STATUS": "Certified"}])
    db.ingest_everify_rows([{"Employer Name": "Meta Platforms Inc"}])
    s = db.stats()
    assert s["employers"] > 0 and s["h1b"] > 0 and s["perm"] == 1 and s["e_verify"] == 1


def test_parse_perm_xlsx_roundtrip(tmp_path):
    """The .xlsx parser reads header + rows the way ingest_perm_rows expects."""
    import openpyxl
    from sourcing.sponsors import parse_perm_xlsx
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["EMPLOYER_NAME", "CASE_STATUS"])
    ws.append(["ACME CORP", "Certified"])
    ws.append(["ACME CORP", "Denied"])
    p = tmp_path / "PERM_test.xlsx"; wb.save(p)
    rows = parse_perm_xlsx(p)
    assert rows[0]["EMPLOYER_NAME"] == "ACME CORP" and rows[0]["CASE_STATUS"] == "Certified"
    db = SponsorDB(":memory:")
    db.ingest_perm_rows(rows)
    assert db.lookup("Acme").perm_certs == 1     # certified only


def test_download_parser_offline():
    """download_h1b_rows uses an injectable fetch, so it parses CSV text offline."""
    from sourcing.sponsors import download_h1b_rows
    csv_text = ('"Fiscal Year",Employer,"Initial Approval","Initial Denial",'
                '"Continuing Approval","Continuing Denial",NAICS,"Tax ID",State,City,ZIP\n'
                '2023,"GOOGLE LLC",50,0,400,0,54,1234,CA,MOUNTAIN VIEW,94043\n')
    rows = download_h1b_rows(2023, fetch=lambda url: csv_text)
    assert rows[0]["Employer"] == "GOOGLE LLC" and rows[0]["Initial Approval"] == "50"


def test_an_unread_location_is_unknown_not_a_claim_that_the_job_is_abroad(tmp_path, monkeypatch):
    """The badge told the user "Sponsors in the US, not this role" about a job in DALLAS.

    looks_us() returns False for BOTH "London" and "" (unreadable). That is correct for
    GATING a badge (withhold unless sure) and wrong as something to SAY: it turned "I
    couldn't read a location" into "this job is not in the US". That is worse than silence.
    It talks someone out of a job they could actually be sponsored for, which is the exact
    opposite of this product's point.

    None means unknown. Only an actual foreign location may be reported as one.
    """
    import ui.app as app
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "m.db"))
    # Seed our OWN sponsor DB rather than depending on ambient data/ contents (which don't exist
    # on a clean checkout). Point the endpoint's store at a temp file via the module-level seam,
    # clear the cache so it's actually used, and ingest one certified PERM row for Google.
    monkeypatch.setattr(app, "_SPONSORS_DB", tmp_path / "sponsors.db")
    app._SPONSORS.clear()
    seed = SponsorDB(str(tmp_path / "sponsors.db"))
    seed.ingest_perm_rows([{"EMPLOYER_NAME": "GOOGLE LLC", "CASE_STATUS": "Certified"}])
    seed.close()
    c = app.app.test_client()
    hdr = {"X-Tailor-Extension": "1"}

    unknown = c.get("/api/sponsors/lookup?company=Google", headers=hdr).get_json()
    assert unknown["role_in_us"] is None, "an unread location must be unknown, not 'not US'"

    abroad = c.get("/api/sponsors/lookup?company=Google&location=London", headers=hdr).get_json()
    assert abroad["role_in_us"] is False, "a real foreign location IS a claim we can make"

    us = c.get("/api/sponsors/lookup?company=Google&location=Dallas,%20TX",
               headers=hdr).get_json()
    assert us["role_in_us"] is True
    assert us["visa"], "a US job at a sponsoring employer must still get its badge"


def test_a_single_token_shell_never_lends_its_badge_downward():
    """Found live: "First Co" normalizes to just "first" (Co is a stripped legal suffix),
    and the prefix guard only examined the QUERY's shape, so this 3-approval shell lent its
    H-1B badge to "First United Bank". The real DB holds 59,151 single-token employers
    ("american", "global", "premier"...), each able to do the same to any multi-word name
    starting with its word. A candidate may only lend downward if IT is distinctive too:
    multi-token, or a curated brand."""
    db = _db()
    db.ingest_h1b_rows([{"Employer": "First Co", "Initial Approval": "3",
                         "Fiscal Year": "2021"}])
    assert db.lookup("First United Bank") is None, \
        "a single-token shell lent its badge to an unrelated multi-word employer"


def test_the_real_parent_entity_still_matches_and_the_shell_is_not_summed_in():
    """The truth under the bug: the honest match existed all along. The old code found
    "First United Bank And Trust" AND summed the "First Co" shell into it (2+3=5), then
    displayed the shell's name because it had more approvals."""
    db = _db()
    db.ingest_h1b_rows([
        {"Employer": "First Co", "Initial Approval": "3", "Fiscal Year": "2021"},
        {"Employer": "First United Bank And Trust", "Initial Approval": "2",
         "Fiscal Year": "2021"},
    ])
    r = db.lookup("First United Bank")
    assert r is not None
    assert r.display_name == "First United Bank And Trust"
    assert r.h1b_approvals == 2, f"the shell was summed in: {r.h1b_approvals}"


def test_a_curated_brand_may_still_lend_downward():
    """"Amazon Web Services" must still aggregate a bare "amazon" record: that is what the
    curated _BRAND_PREFIXES allowlist is FOR. The guard blocks generic tokens, not brands."""
    db = _db()
    before = db.lookup("Amazon Web Services")
    baseline = before.h1b_approvals if before else 0    # the fixture pre-loads AWS entities
    db.ingest_h1b_rows([{"Employer": "Amazon Inc", "Initial Approval": "100",
                         "Fiscal Year": "2021"}])
    r = db.lookup("Amazon Web Services")
    assert r is not None
    assert r.h1b_approvals == baseline + 100, \
        "the bare-brand record was not aggregated into the multi-token query"


def test_dotted_initials_match_the_spelled_out_filings():
    """A posting says "U.S. Bank"; the filings say "US Bank National Association" (1,839) and
    "U.S. Bank National Association" (2). Both spellings are one employer (2026-10-10)."""
    from sourcing.sponsors import normalize_employer_variants
    assert normalize_employer("U.S. Bank") == normalize_employer("US Bank") == "us bank"
    assert normalize_employer("J.P. Morgan") == "jp morgan"
    assert normalize_employer_variants("U.S. Bank") == ["us bank", "u s bank"]
    db = SponsorDB(":memory:")
    db.ingest_h1b_rows([_row("US BANK NATIONAL ASSOCIATION", 1839, 0),
                        _row("U.S. BANK NATIONAL ASSOCIATION", 2, 0)])
    r = db.lookup("U.S. Bank")
    assert r and r.h1b_approvals == 1841


def test_a_database_built_before_the_change_still_matches_both_spellings():
    """Databases already on people's machines hold the old form ("u s bank ..."). Until they are
    rebuilt, a lookup tries both forms and adds them up."""
    db = SponsorDB(":memory:")
    for norm, name, n in (("us bank national association", "Us Bank National Association", 1839),
                          ("u s bank national association", "U.S. Bank National Association", 2)):
        db._conn.execute("INSERT INTO sponsor_employer (norm_name, display_name, h1b_approvals, "
                         "h1b_last_fy) VALUES (?, ?, ?, 2023)", (norm, name, n))
    db._invalidate_index()
    r = db.lookup("U.S. Bank")
    assert r and r.h1b_approvals == 1841 and r.display_name == "Us Bank National Association"
