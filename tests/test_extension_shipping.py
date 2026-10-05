"""The extension has to actually REACH a user (2026-07-15).

The user: "where is the extension that is supposed to be installed alongside the local app,
did we build it yet?"

It was built, and complete, and shipped with nothing: the installer never mentioned it, so
it existed in the repo and reached nobody. That matters more than a normal missing feature,
because LinkedIn and Indeed forbid bots (§7) and the extension is the ONLY way we help
there. Without it the app is silent on the two sites people actually search.

Chrome cannot be made to install an unpacked extension from an installer (its own security
rule, not a gap here), so a manual step exists until the Web Store listing does. That makes
it the APP's job to notice the extension is missing and say so, rather than the person
browsing LinkedIn, seeing no badges, and concluding the product is broken.
"""

from __future__ import annotations

import json
from pathlib import Path

import ui.app as app


def test_the_installer_ships_the_extension():
    """The bug: the app installed and the extension didn't."""
    iss = Path("packaging/tailor.iss").read_text(encoding="utf-8")
    assert "extension" in iss.lower(), "the installer doesn't ship the extension at all"
    assert r"..\extension\*" in iss, "the extension folder is not in [Files]"
    assert "recursesubdirs" in iss


def test_the_extension_is_a_real_loadable_extension():
    """A folder we ship must be something Chrome will actually accept."""
    m = json.loads(Path("extension/manifest.json").read_text(encoding="utf-8"))
    assert m["manifest_version"] == 3          # Chrome rejects v2 now
    assert m.get("name") and m.get("version")
    for f in ("background.js", "content.js", "popup.html"):
        assert Path("extension", f).exists(), f"extension/{f} is missing"
    hosts = [h for cs in m.get("content_scripts", []) for h in cs.get("matches", [])]
    # The whole point: the two sites that forbid bots, where we can only help from inside
    # the person's own browser.
    assert any("linkedin.com" in h for h in hosts)
    assert any("indeed.com" in h for h in hosts)


def test_the_extension_covers_the_major_ats_and_boards():
    """Coverage parity with the big assisted-apply tools: our autofill matches fields by
    label (generic, no per-site config), so a host in the manifest is a host we can pre-fill
    and badge. This locks in the ATS/board breadth so a future manifest edit cannot silently
    shrink it back to LinkedIn + Indeed. Kept TARGETED to job domains, not all-sites."""
    m = json.loads(Path("extension/manifest.json").read_text(encoding="utf-8"))
    hosts = [h for cs in m.get("content_scripts", []) for h in cs.get("matches", [])]
    # A representative slice across the ATSes and boards a visa-seeking applicant meets most.
    for domain in ("workday", "greenhouse", "lever", "ashby", "icims", "smartrecruiters",
                   "workable", "adp.com", "bamboohr", "dayforcehcm", "successfactors",
                   "jobvite", "eightfold", "recruitee", "glassdoor", "joinhandshake",
                   "ziprecruiter"):
        assert any(domain in h for h in hosts), f"no manifest match covers {domain}"
    # And we must NOT have gone all-sites: no bare wildcard host that runs us everywhere.
    assert not any(h.startswith(("*://", "http://*/", "https://*/")) for h in hosts), \
        "the extension must stay scoped to job domains, never all sites"


def test_the_app_knows_when_the_extension_is_missing():
    """The app cannot inspect the browser, so the only honest signal is whether the
    extension has spoken to us. Asking the person "did you install it?" is a question the
    software should answer itself."""
    app._EXT_SEEN.clear()
    c = app.app.test_client()
    d = c.get("/api/extension/status").get_json()
    assert d["connected"] is False
    # And it must tell them WHERE to point Chrome, because "browse to your install folder"
    # is where a non-technical person gives up.
    assert d["folder"], "no folder to point 'Load unpacked' at"


def test_the_app_notices_when_the_extension_speaks():
    app._EXT_SEEN.clear()
    c = app.app.test_client()
    c.get("/api/sponsors/lookup?company=Google&location=New%20York,%20NY",
          headers={app.EXTENSION_HEADER: "1"})
    assert c.get("/api/extension/status").get_json()["connected"] is True


def test_sponsor_profile_exposes_the_full_record_for_the_rich_panel():
    """The lookup now returns a structured profile (H-1B volume + FY span, PERM, E-Verify,
    cap-exempt) -- the depth that beats a yes/no checker. Built straight from the sponsor record."""
    from sourcing.sponsors import SponsorRecord
    rec = SponsorRecord(display_name="Spotify", h1b_approvals=120, h1b_last_fy=2024, naics="5182",
                        state="NY", cap_exempt=False, e_verify=True, h1b_first_fy=2019, perm_certs=8)
    p = app._sponsor_profile(rec)
    assert p["h1b_approvals"] == 120 and p["fy_range"] == "FY2019 to 2024"
    assert p["perm_certs"] == 8 and p["e_verify"] is True and p["cap_exempt"] is False


def test_lookup_response_always_carries_the_profile_field():
    c = app.app.test_client()
    r = c.get("/api/sponsors/lookup?company=Google&location=New%20York,%20NY",
              headers={app.EXTENSION_HEADER: "1"}).get_json()
    assert "profile" in r                              # present (None when unmatched, dict when matched)
    if r["matched"]:
        assert isinstance(r["profile"]["h1b_approvals"], int)


def test_a_page_without_the_header_never_counts_as_the_extension():
    """The status must not be forgeable by any site the person visits: if a web page could
    mark the extension "connected", the app would stop telling them it is missing."""
    app._EXT_SEEN.clear()
    c = app.app.test_client()
    c.get("/api/sponsors/lookup?company=Google")          # no extension header
    assert c.get("/api/extension/status").get_json()["connected"] is False


def test_a_fresh_data_dir_gets_the_shipped_visa_snapshot(tmp_path, monkeypatch):
    """A fresh install had ZERO sponsor data: the founder's dev dir held 582,145 employers
    while the packaged app's own data dir held 0, so a buyer's first LinkedIn session read
    "No sponsor record" on every job. The installer now ships a vetted snapshot and first
    run copies it in."""
    import sqlite3
    seed_dir = tmp_path / "bundle" / "seed"
    seed_dir.mkdir(parents=True)
    con = sqlite3.connect(seed_dir / "sponsors.db")
    con.execute("CREATE TABLE sponsor_employer (norm_name TEXT PRIMARY KEY, display_name TEXT,"
                " h1b_approvals INTEGER DEFAULT 0, h1b_last_fy INTEGER DEFAULT 0,"
                " h1b_first_fy INTEGER DEFAULT 0, naics TEXT, state TEXT,"
                " cap_exempt INTEGER DEFAULT 0, e_verify INTEGER DEFAULT 0,"
                " perm_certs INTEGER DEFAULT 0)")
    con.execute("INSERT INTO sponsor_employer (norm_name, display_name, h1b_approvals,"
                " h1b_last_fy) VALUES ('google', 'Google', 5000, 2023)")
    con.commit(); con.close()

    monkeypatch.setattr(app, "ROOT", tmp_path / "bundle")
    monkeypatch.setattr(app, "_DATA", tmp_path / "userdata")
    app._SPONSORS.clear()
    db = app._sponsors()
    assert db.count() == 1, "the shipped snapshot was not seeded into a fresh data dir"


def test_an_EMPTY_existing_db_is_replaced_by_the_snapshot(tmp_path, monkeypatch):
    """The case that actually happened: the packaged app had already created a zero-row
    sponsors.db on first launch, so 'seed only if missing' would skip exactly the machines
    that need it."""
    import sqlite3
    from sourcing.sponsors import SponsorDB
    seed_dir = tmp_path / "bundle" / "seed"; seed_dir.mkdir(parents=True)
    con = sqlite3.connect(seed_dir / "sponsors.db")
    con.execute("CREATE TABLE sponsor_employer (norm_name TEXT PRIMARY KEY, display_name TEXT,"
                " h1b_approvals INTEGER DEFAULT 0, h1b_last_fy INTEGER DEFAULT 0,"
                " h1b_first_fy INTEGER DEFAULT 0, naics TEXT, state TEXT,"
                " cap_exempt INTEGER DEFAULT 0, e_verify INTEGER DEFAULT 0,"
                " perm_certs INTEGER DEFAULT 0)")
    con.execute("INSERT INTO sponsor_employer (norm_name, display_name) VALUES ('x','X')")
    con.commit(); con.close()
    userdata = tmp_path / "userdata"; userdata.mkdir()
    SponsorDB(userdata / "sponsors.db").close()          # empty DB already on disk

    monkeypatch.setattr(app, "ROOT", tmp_path / "bundle")
    monkeypatch.setattr(app, "_DATA", userdata)
    app._SPONSORS.clear()
    assert app._sponsors().count() == 1, "an empty existing DB was not replaced"


def test_a_POPULATED_db_is_never_overwritten_by_the_snapshot(tmp_path, monkeypatch):
    """The person's own imports always beat our snapshot: seeding must never clobber
    newer data they brought themselves."""
    import sqlite3
    from sourcing.sponsors import SponsorDB
    seed_dir = tmp_path / "bundle" / "seed"; seed_dir.mkdir(parents=True)
    con = sqlite3.connect(seed_dir / "sponsors.db"); con.execute("CREATE TABLE t (x)"); con.close()
    userdata = tmp_path / "userdata"; userdata.mkdir()
    theirs = SponsorDB(userdata / "sponsors.db")
    theirs.ingest_h1b_rows([{"Employer": "Their Company",
                             "Initial Approval": "10", "Fiscal Year": "2023"}])
    theirs.close()

    monkeypatch.setattr(app, "ROOT", tmp_path / "bundle")
    monkeypatch.setattr(app, "_DATA", userdata)
    app._SPONSORS.clear()
    db = app._sponsors()
    assert db.lookup("Their Company") is not None, "seeding clobbered the person's own data"
