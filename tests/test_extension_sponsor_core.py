"""The extension's in-browser sponsor lookup (extension/sponsor_core.js) must answer EXACTLY like
the app's /api/sponsors/lookup, on the same data.

The extension now works with no SponsorJobs app installed: it reads a static, sharded index the
feed publishes (scripts/build_sponsor_index.py) and runs a JS port of sourcing/sponsors.py on it.
Two implementations of one matcher drift unless something holds them together, so these tests
build an index, run the Flask endpoint (Python) and the JS port (node) on the same names, and
require byte-for-byte equal answers. The tricky names are the ones that bit us before: "U.S.
Bank" (initials), "Walgreens" (alias), "Amazon" (curated brand prefix), "First United Bank"
(a generic parent must not lend its badge), single generic tokens like "Global".
"""

from __future__ import annotations

import gzip
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import build_sponsor_index as bsi
from sourcing.sponsors import SEED_COLS, SponsorDB

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")
CORE = Path("extension/sponsor_core.js").resolve()

HARNESS = r"""
const fs = require('fs'), path = require('path'), zlib = require('zlib');
const S = require(path.resolve(process.argv[2]));
const dir = process.argv[3];
const cases = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8'));
const shards = {};
const getShard = (k) => {
  if (!(k in shards)) {
    const doc = JSON.parse(zlib.gunzipSync(fs.readFileSync(path.join(dir, k + '.json.gz'))).toString('utf8'));
    shards[k] = S.prepareShard(doc.records);
  }
  return shards[k];
};
const out = cases.map(([c, l]) => S.lookupResponse(c, l, getShard, manifest.employers));
fs.writeFileSync(process.argv[5], JSON.stringify(out));
"""

# Seed-layout rows modelled on the real data (see the comments for what each one tests).
FIXTURE = [
    # U.S. Bank: filings under both spellings; the posting's "U.S. Bank" must find both.
    ("us bank national association", "U.S. Bank National Association", 1839, 2024, 2019, "522110", "", 0, 1, 122),
    ("u s bank national association", "U.S Bank National Association", 2, 2021, 2021, "52", "MN", 0, 0, 0),
    ("us bank", "Us Bank", 0, 0, 0, "", "", 0, 1, 0),
    # Walgreens -> "walgreen" by alias; the bare "walgreens" e-verify row must not win.
    ("walgreen", "Walgreen Co", 55, 2023, 2020, "44", "IL", 0, 1, 0),
    ("walgreens", "Walgreens", 0, 0, 0, "", "", 0, 1, 0),
    ("walgreens optioncare", "Walgreens - Optioncare", 0, 0, 0, "", "", 0, 1, 0),
    # Amazon: a curated brand, so the bare name aggregates its filing entities.
    ("amazon", "Amazon", 0, 0, 0, "", "", 0, 1, 0),
    ("amazon com services", "Amazon.Com Services Llc", 47486, 2024, 2009, "454110", "VA", 0, 0, 2129),
    ("amazon web services", "Amazon Web Services, Inc.", 8573, 2024, 2010, "45", "VA", 0, 0, 687),
    ("amazon data services", "Amazon Data Services, Inc.", 997, 2023, 2016, "51", "", 0, 0, 164),
    ("amazonia ventures", "Amazonia Ventures", 4, 2022, 2022, "54", "FL", 0, 0, 0),
    # Meta: alias to its filing entity.
    ("meta platforms", "Meta Platforms, Inc.", 7891, 2024, 2021, "51", "", 1, 0, 660),
    ("metadata partners", "Metadata Partners", 3, 2020, 2020, "54", "", 0, 0, 0),
    # JPMorgan: multi-token prefix aggregation.
    ("jpmorgan chase", "Jpmorgan Chase", 9452, 2024, 2009, "55", "", 0, 1, 0),
    ("jpmorgan chase and", "Jpmorgan Chase & Co.", 3, 2019, 2019, "55", "IL", 0, 1, 0),
    ("jpmorgan chase bank", "Jpmorgan Chase Bank, N.A.", 0, 0, 0, "", "", 0, 1, 0),
    # Generic single tokens: may match exactly, never lend downward.
    ("first", "First Co.", 3, 2022, 2022, "52", "TX", 0, 1, 0),
    ("first united bank and trust", "First United Bank & Trust", 2, 2020, 2020, "52", "OK", 0, 1, 0),
    ("first republic bank", "First Republic Bank", 350, 2023, 2012, "52", "", 0, 1, 8),
    ("global", "Global Incorporated", 6, 2021, 2019, "33", "NC", 0, 1, 0),
    ("global payments", "Global Payments Inc", 410, 2024, 2011, "52", "GA", 0, 0, 30),
    # The Home Depot: "the" stripped; several filing entities.
    ("home depot", "Home Depot U.S.A., Inc.", 147, 2024, 2012, "44", "GA", 0, 1, 14),
    ("home depot product authority", "Home Depot Product Authority Llc", 603, 2024, 2015, "42", "GA", 0, 0, 0),
    ("home depot management", "Home Depot Management Company, Llc", 299, 2024, 2014, "44", "GA", 0, 0, 64),
    ("home depot u s a", "Home Depot U S A Inc", 39, 2018, 2015, "44", "GA", 0, 0, 0),
    # Cap-exempt + a university prefix.
    ("university of illinois", "University Of Illinois", 900, 2024, 2009, "611310", "IL", 1, 1, 40),
    ("university of illinois at chicago", "University Of Illinois At Chicago", 700, 2024, 2009, "61", "IL", 1, 0, 12),
    # Tie on (h1b, perm): the name decides which entity lends its display name.
    ("acme widgets", "Acme Widgets", 10, 2022, 2020, "33", "OH", 0, 0, 0),
    ("acme widgets east", "Acme Widgets East", 10, 2023, 2021, "33", "NY", 0, 0, 0),
    # Display names the index stores as 0 (title of the key) vs verbatim.
    ("procogia", "Procogia", 8, 2023, 2020, "51", "WA", 0, 0, 0),
    ("0965688 bc", "0965688 Bc Ltd", 8, 2023, 2020, "51", "WA", 0, 1, 0),
    ("3m", "3M", 120, 2024, 2010, "32", "MN", 0, 1, 20),
    # Brand prefixes from the curated list.
    ("google", "Google Llc", 30000, 2024, 2009, "51", "CA", 0, 0, 5000),
    ("google public sector", "Google Public Sector Llc", 40, 2024, 2023, "51", "VA", 0, 0, 0),
    ("uber technologies", "Uber Technologies, Inc.", 2000, 2024, 2014, "48", "CA", 0, 0, 300),
    # No signal at all: dropped from the index. The app keeps the row, so this name is only
    # queried in the "dropped" test below, never in the parity list.
    ("nowhere partners", "Nowhere Partners", 0, 0, 0, "", "", 0, 0, 0),
]

NAMES = [
    "U.S. Bank", "US Bank", "U.S. Bank National Association", "u.s. bancorp", "U S Bank",
    "Walgreens", "Walgreens Boots Alliance", "WALGREEN CO.", "Amazon", "Amazon Web Services",
    "AWS", "amazon.com", "Amazonia", "Meta", "Facebook", "Metadata", "JPMorgan Chase",
    "JPMorgan Chase & Co.", "J.P. Morgan Chase", "JP Morgan", "First United Bank",
    "First United Bank & Trust", "First", "First Republic", "First Co", "Global",
    "Global Payments", "Global Payments Inc.", "The Home Depot", "Home Depot", "Home Depot, Inc.",
    "University of Illinois", "University of Illinois at Chicago", "University of Illinois Urbana",
    "Acme Widgets", "ProCogia", "0965688 B.C. Ltd.", "3M", "3M Company", "Google", "Alphabet",
    "Google LLC", "Uber", "Uber Eats", "", "   ", "Inc.", "U.S.", "The", "&", "Ünïcödé GmbH",
    "Société Générale", "Nobody Here Corp",
]
LOCATIONS = ["", "New York, NY", "London, UK", "Remote - US", "Dallas, TX", "Austin, tx",
             "São Paulo, Brazil", "Washington, D.C.", "U.S.", "Toronto, ON", "Bangalore"]


def _fixture_db(path) -> SponsorDB:
    db = SponsorDB(str(path))
    db.merge_aggregated_rows([dict(zip(SEED_COLS, r)) for r in FIXTURE])
    return db


def _app_answers(monkeypatch, db_path, cases) -> list[dict]:
    import ui.app as app
    monkeypatch.setattr(app, "_SPONSORS_DB", Path(db_path))
    app._SPONSORS.clear()
    c = app.app.test_client()
    out = []
    try:
        for company, location in cases:
            r = c.get("/api/sponsors/lookup", query_string={"company": company, "location": location},
                      headers={"X-Tailor-Extension": "1"})
            out.append(r.get_json())
    finally:
        app._SPONSORS.clear()
    return out


def _js_answers(tmp_path, index_dir, cases) -> list[dict]:
    harness, cases_f, out_f = tmp_path / "h.js", tmp_path / "cases.json", tmp_path / "out.json"
    harness.write_text(HARNESS, encoding="utf-8")
    cases_f.write_text(json.dumps(cases), encoding="utf-8")
    r = subprocess.run(["node", str(harness), str(CORE), str(index_dir), str(cases_f), str(out_f)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    return json.loads(out_f.read_text(encoding="utf-8"))


def _compare(py, js, cases):
    bad = []
    for (company, location), p, j in zip(cases, py, js):
        p = {k: v for k, v in p.items() if k != "employers_loaded"}
        j = {k: v for k, v in j.items() if k != "employers_loaded"}
        if p != j:
            bad.append((company, location, p, j))
    assert not bad, f"{len(bad)} disagreements, first: {bad[0]}"


@needs_node
def test_js_port_agrees_with_the_app_on_tricky_names(tmp_path, monkeypatch):
    db = _fixture_db(tmp_path / "s.db")
    bsi.write_index(db, tmp_path / "out")
    db.close()
    cases = [[n, loc] for n in NAMES for loc in LOCATIONS]
    py = _app_answers(monkeypatch, tmp_path / "s.db", cases)
    js = _js_answers(tmp_path, tmp_path / "out" / "sponsors", cases)
    _compare(py, js, cases)
    by = {(c, l): j for (c, l), j in zip(map(tuple, cases), js)}
    # Spot-check the cases that motivated the rules, so a both-sides-wrong change still fails.
    usb = by[("U.S. Bank", "New York, NY")]
    assert usb["matched"] and usb["profile"]["h1b_approvals"] == 1839 + 2
    assert by[("Walgreens", "")]["profile"]["h1b_approvals"] == 55
    assert by[("Amazon", "Dallas, TX")]["profile"]["h1b_approvals"] == 47486 + 8573 + 997
    fub = by[("First United Bank", "Dallas, TX")]                     # "first" must not lend
    assert fub["matched_name"] == "First United Bank & Trust" and fub["profile"]["h1b_approvals"] == 2
    assert by[("Global", "")]["matched_name"] == "Global Incorporated"    # exact only
    assert by[("Global Payments Inc.", "")]["profile"]["h1b_approvals"] == 410
    assert by[("The Home Depot", "")]["profile"]["h1b_approvals"] == 147 + 603 + 299 + 39
    assert by[("Meta", "London, UK")]["visa"] == [] and by[("Meta", "London, UK")]["role_in_us"] is False
    assert [b["code"] for b in by[("Meta", "Remote - US")]["visa"]] == ["H-1B", "GREEN-CARD", "CAP-EXEMPT"]
    assert by[("ProCogia", "")]["matched_name"] == "Procogia"


@needs_node
def test_no_signal_employers_are_left_out_of_the_index(tmp_path):
    db = _fixture_db(tmp_path / "s.db")
    m = bsi.write_index(db, tmp_path / "out")
    db.close()
    n_shard = json.loads(gzip.decompress((tmp_path / "out/sponsors/n.json.gz").read_bytes()))
    assert "nowhere partners" not in n_shard["records"]
    assert m["employers"] == len(FIXTURE) - 1
    js = _js_answers(tmp_path, tmp_path / "out" / "sponsors", [["Nowhere Partners", ""]])
    assert js[0]["matched"] is False


def test_manifest_lists_every_shard_with_its_size_and_is_deterministic(tmp_path):
    db = _fixture_db(tmp_path / "s.db")
    m1 = bsi.write_index(db, tmp_path / "a", generated_at="2026-10-10T00:00:00Z")
    m2 = bsi.write_index(db, tmp_path / "b", generated_at="2026-10-10T00:00:00Z")
    db.close()
    assert m1 == m2 and m1["version"]
    assert sorted(m1["shards"]) == sorted(bsi.SHARDS)
    for key, meta in m1["shards"].items():
        f = tmp_path / "a" / "sponsors" / meta["file"]
        assert f.stat().st_size == meta["bytes"]
        assert f.read_bytes() == (tmp_path / "b" / "sponsors" / meta["file"]).read_bytes()
        doc = json.loads(gzip.decompress(f.read_bytes()))
        assert doc["version"] == m1["version"] and doc["shard"] == key
        assert all(bsi.shard_of(k) == key for k in doc["records"])
    assert m1["total_bytes"] == sum(s["bytes"] for s in m1["shards"].values())
    on_disk = json.loads((tmp_path / "a/sponsors/manifest.json").read_text())
    assert on_disk == m1


def test_records_drop_derivable_display_names_and_trailing_defaults():
    row = {"display_name": "Procogia", "h1b_approvals": 8, "h1b_last_fy": 2023, "h1b_first_fy": 2020,
           "perm_certs": 0, "e_verify": 0, "cap_exempt": 0, "naics": "541511", "state": ""}
    assert bsi.encode_record("procogia", row) == [0, 8, 2023, 2020, 0, 0, 0, "54"]
    ev = dict(row, display_name="Us Bank", h1b_approvals=0, h1b_last_fy=0, h1b_first_fy=0,
              e_verify=1, naics=None)
    assert bsi.encode_record("us bank", ev) == [0, 0, 0, 0, 0, 1]
    assert bsi.encode_record("3m", dict(row, display_name="3M"))[0] == 0
    assert bsi.encode_record("jpmorgan chase and", dict(row, display_name="Jpmorgan Chase & Co."))[0] \
        == "Jpmorgan Chase & Co."


@needs_node
def test_js_port_agrees_with_the_app_on_the_real_bundled_data(tmp_path, monkeypatch):
    """The whole real index, on a seeded sample of real employer names plus the tricky ones:
    the same build a fresh app install does, published and read back by the JS port."""
    db = bsi.build_db(tmp_path / "real.db")
    m = bsi.write_index(db, tmp_path / "out")
    names = [r[0] for r in db._conn.execute(
        "SELECT display_name FROM sponsor_employer WHERE h1b_approvals>0 OR perm_certs>0 "
        "ORDER BY norm_name").fetchall()]
    db.close()
    assert m["total_bytes"] < 10_000_000, "the index must stay well under 10 MB gzipped"
    rng = random.Random(20261010)
    sample = rng.sample(names, 600)
    # Mangle some: brand-only first words and "U.S."-style initials, where matching gets subtle.
    sample += [n.split(" ")[0] for n in rng.sample(names, 200)]
    sample += [" ".join(n.split(" ")[:2]) for n in rng.sample(names, 200)]
    sample += NAMES
    cases = [[n, rng.choice(LOCATIONS)] for n in sample]
    py = _app_answers(monkeypatch, tmp_path / "real.db", cases)
    js = _js_answers(tmp_path, tmp_path / "out" / "sponsors", cases)
    _compare(py, js, cases)
    # And the real numbers for the headline cases.
    by = {c[0]: j for c, j in zip(cases, js)}
    assert by["U.S. Bank"]["profile"]["h1b_approvals"] >= 1839
    assert by["Walgreens"]["matched_name"] == "Walgreen Co"
    assert by["First United Bank"]["profile"]["h1b_approvals"] == 2      # not + "First Co"'s 3


def test_the_feed_workflow_publishes_the_index_next_to_the_feed():
    wf = Path(".github/workflows/feed.yml").read_text(encoding="utf-8")
    assert "scripts/build_sponsor_index.py" in wf
    build = wf.index("python scripts/build_feed.py")
    index = wf.index("scripts/build_sponsor_index.py")
    assert build < index < wf.index("Upload to R2") < wf.index("upload-pages-artifact")


def test_the_r2_upload_carries_the_index_shards_before_their_manifest(tmp_path):
    from scripts import upload_feed
    from sourcing.feedfile import JOBS_FILE, MANIFEST_FILE
    (tmp_path / JOBS_FILE).write_bytes(b"x")
    (tmp_path / MANIFEST_FILE).write_text("{}")
    before = [rel for _, rel in upload_feed.plan(tmp_path)]
    assert not any(r.startswith("sponsors/") for r in before)      # absent: plan unchanged
    db = _fixture_db(tmp_path / "s.db")
    bsi.write_index(db, tmp_path)
    db.close()
    rels = [rel for _, rel in upload_feed.plan(tmp_path)]
    spon = [r for r in rels if r.startswith("sponsors/")]
    assert spon[-1] == "sponsors/manifest.json" and len(spon) == len(bsi.SHARDS) + 1
    assert rels[-2:] == [JOBS_FILE, MANIFEST_FILE]                   # the feed's own order kept
    h = upload_feed.object_headers("sponsors/a.json.gz")
    assert h["ContentEncoding"] == "gzip"
    assert upload_feed.object_headers("sponsors/manifest.json")["CacheControl"] == "no-cache"


def test_the_extension_reads_the_index_and_falls_back_to_the_app():
    m = json.loads(Path("extension/manifest.json").read_text(encoding="utf-8"))
    assert "https://feed.sponsorjobs.ai/*" in m["host_permissions"]
    bg = Path("extension/background.js").read_text(encoding="utf-8")
    assert 'importScripts("sponsor_core.js")' in bg
    assert "https://feed.sponsorjobs.ai/feed/sponsors" in bg
    assert "/api/sponsors/lookup" in bg                              # the fallback stays
    # Privacy: the company name is never sent to the feed host, only shard file names.
    idx = bg[bg.index("async function indexLookup"):bg.index("async function lookup(")]
    assert "encodeURIComponent(company" not in idx


BG_HARNESS = r"""
// Runs extension/background.js in a node vm with fake chrome.*, fetch and no IndexedDB (the
// worker must cope without it), then drives messages through it.
const fs = require('fs'), path = require('path'), vm = require('vm');
const [bgPath, corePath, indexDir, mode] = process.argv.slice(2);
const urls = [];
const store = {};
let listener = null;
const ctx = {
  console, URL, TextDecoder, Blob, Response, DecompressionStream, Uint8Array, Promise, JSON, Date, Map,
  setTimeout,
  importScripts: (f) => vm.runInContext(fs.readFileSync(path.join(path.dirname(corePath), f), 'utf8'), ctx),
  chrome: {
    storage: { local: { get: async (k) => ({ [k]: store[k] }), set: async (o) => Object.assign(store, o) } },
    runtime: { onMessage: { addListener: (fn) => { listener = fn; } } },
  },
  fetch: async (url, opts) => {
    urls.push(String(url));
    if (url.startsWith('http://127.0.0.1')) {
      if (mode === 'app_up' && url.includes('/api/sponsors/lookup'))
        return new Response(JSON.stringify({ matched: true, matched_name: 'From App', visa: [] }), { status: 200 });
      if (mode === 'app_up') return new Response('{}', { status: 200 });
      throw new TypeError('connection refused');
    }
    if (mode !== 'index_up') throw new TypeError('offline');
    const name = url.split('/').pop();
    const f = path.join(indexDir, name);
    if (!fs.existsSync(f)) return new Response('', { status: 404 });
    return new Response(fs.readFileSync(f), { status: 200 });   // raw .gz bytes, like Pages
  },
};
ctx.globalThis = ctx; ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(bgPath, 'utf8'), ctx);
const ask = (msg) => new Promise((res) => listener(msg, {}, res));
(async () => {
  const a = await ask({ type: 'lookup', company: 'U.S. Bank', location: 'New York, NY' });
  const b = await ask({ type: 'lookup', company: 'Walgreens', location: '' });
  const m = await ask({ type: 'match', jd: 'some job description text' });
  const st = await ask({ type: 'index_status' });
  console.log(JSON.stringify({ a, b, m, st, urls }));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run_bg(tmp_path, index_dir, mode):
    h = tmp_path / f"bg_{mode}.js"
    h.write_text(BG_HARNESS, encoding="utf-8")
    r = subprocess.run(["node", str(h), str(Path("extension/background.js").resolve()), str(CORE),
                        str(index_dir), mode], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_node
def test_the_worker_answers_from_the_index_with_no_app(tmp_path, monkeypatch):
    db = _fixture_db(tmp_path / "s.db")
    bsi.write_index(db, tmp_path / "out")
    db.close()
    out = _run_bg(tmp_path, tmp_path / "out" / "sponsors", "index_up")
    a = out["a"]
    assert a["ok"] and a["source"] == "index" and a["matched"]
    assert a["profile"]["h1b_approvals"] == 1841 and [v["code"] for v in a["visa"]][0] == "H-1B"
    assert out["b"]["matched_name"] == "Walgreen Co"
    # The same answer the app gives.
    py = _app_answers(monkeypatch, tmp_path / "s.db", [["U.S. Bank", "New York, NY"]])[0]
    assert {k: a[k] for k in py if k != "employers_loaded"} == \
        {k: v for k, v in py.items() if k != "employers_loaded"}
    # App-only features say "no app", which the page turns into an install prompt.
    assert out["m"] == {"ok": False, "error": "cant_reach_app"}
    assert out["st"]["ok"] and out["st"]["employers"] == len(FIXTURE) - 1
    # Privacy: the feed host only ever sees shard files, never a company name.
    feed = [u for u in out["urls"] if "feed.sponsorjobs.ai" in u]
    assert feed and all(u.rsplit("/", 1)[1] in ("manifest.json", "u.json.gz", "w.json.gz") for u in feed)
    assert not any("bank" in u.lower() or "walgreen" in u.lower() for u in feed)
    assert feed.count("https://feed.sponsorjobs.ai/feed/sponsors/manifest.json") == 1   # cached


@needs_node
def test_the_worker_falls_back_to_the_app_only_when_the_index_is_unavailable(tmp_path):
    out = _run_bg(tmp_path, tmp_path, "app_up")
    assert out["a"]["ok"] and out["a"]["source"] == "app" and out["a"]["matched_name"] == "From App"
    down = _run_bg(tmp_path, tmp_path, "all_down")
    assert down["a"] == {"ok": False, "error": "cant_reach_app"}


def test_lookup_answer_does_not_depend_on_what_was_looked_up_before(tmp_path):
    """"U.S. Bank" (both spellings, merged) and "US Bank" (one spelling) share their first
    normalized form; the per-process cache was keyed on that alone, so whichever ran first
    decided the other's answer. Found by the JS parity test."""
    fresh = _fixture_db(tmp_path / "a.db")
    want = fresh.lookup("US Bank")
    fresh.close()
    db = _fixture_db(tmp_path / "b.db")
    db.lookup("U.S. Bank")
    assert db.lookup("US Bank") == want
    db.close()
