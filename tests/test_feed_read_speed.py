"""The feed build's ad reading got faster (2026-10-21) without changing a single answer.

scripts/build_feed.py spent ~17 minutes reading ~150k ads (sponsorship stance + function tags).
Three changes: jobtags tries each pattern only where one of its leading words starts, adstance
skips the sentence split when the text cannot match, and a large build reads ads in forked
worker processes. Each is checked here against the plain, slow way of computing the same thing.
"""
from __future__ import annotations

import gzip
import json
import multiprocessing
import random

import pytest

from sourcing import adstance, feedfile
from sourcing.adstance import RESTRICTED_RE, ad_stance
from sourcing.jobtags import _COMPILED, TAGS, _count, _lowered, job_tags
from tests.test_adstance import GREEN_CARD_YES, NEITHER, NO, RESTRICTED_ADS, YES

# Words that sit right at the edges of the patterns: stems, word starts, \b, case, the odd
# Unicode letters that case-insensitive matching folds to ASCII, and non-breaking spaces.
_EDGE = ["AI", "maintain", "ai-driven", "PM II", "pm   iii", "Pm IV", "on-site service",
         "onsite service", "Pre-Sales", "presales", "ISO 9001", "iso9001", "CE mark", "full stack",
         "Full-Stack", "fullstack", "iOS", "bios", "loan officer", "LOAN  officer", "M&A", "P&C",
         "a/b testing", "TS/SCI", "ſales team", "dııstributed", "İOS developer",
         "Kafka", "Kubernetes", "back-end", "data-pipeline", "Product Manager", "x-ray",
         "research", "Researcher", "pre-salesforce", "u.s. citizen", "post-silicon", "ML", "html"]


def _corpus(n=400, seed=7):
    rng = random.Random(seed)
    words = list(_EDGE)
    for _, pat in TAGS:
        words += [w.replace("\\b", "").replace("\\s+", " ").replace("\\s*", "")
                  for w in pat.split("|") if "(" not in w and "[" not in w]
    filler = ["the", "and", "team", "with", "We", "build", ".", ",", "\n", "  ", "\t", "•", "é"]
    out = []
    for _ in range(n):
        parts = [rng.choice(words if rng.random() < 0.4 else filler) for _ in range(rng.randint(0, 80))]
        s = " ".join(parts)
        out.append(s.upper() if rng.random() < 0.1 else s)
    return out + _EDGE + RESTRICTED_ADS + GREEN_CARD_YES + NO + YES + NEITHER


def test_tag_counts_equal_plain_findall_on_every_pattern():
    for text in _corpus():
        low = _lowered(text)
        for order, (_, pat) in enumerate(_COMPILED):
            assert _count(order, text, low) == len(pat.findall(text)), (TAGS[order][0], text)


def _plain_job_tags(title, body, limit=9):
    from sourcing.jobtags import COMMON, TITLE_WEIGHT
    scored = []
    for order, (tag, pat) in enumerate(_COMPILED):
        t, b = len(pat.findall(title)), len(pat.findall(body))
        if not t and b < (3 if tag in COMMON else 2):
            continue
        scored.append((-(t * TITLE_WEIGHT + min(b, 20)), order, tag))
    scored.sort()
    return [x for _, _, x in scored[:limit]]


def test_job_tags_equal_the_plain_scan():
    texts = _corpus(200, seed=11)
    for i, body in enumerate(texts):
        title = texts[(i * 7) % len(texts)][:80]
        assert job_tags(title, body) == _plain_job_tags(title, body)


def _plain_ad_stance(text):
    no_sent = yes_sent = None
    for s in adstance._sentences(text):
        if RESTRICTED_RE.search(s):
            return {"stance": adstance.RESTRICTED, "sentence": s[:300]}
        if no_sent is not None or not (adstance._ABOUT.search(s) and adstance._CTX.search(s)):
            continue
        if any(p.search(s) for p in adstance._NO):
            no_sent = s[:300]
        elif yes_sent is None and any(p.search(s) for p in adstance._YES):
            yes_sent = s[:300]
    if no_sent:
        return {"stance": adstance.NOT_OFFERED, "sentence": no_sent}
    if yes_sent:
        return {"stance": adstance.OFFERED, "sentence": yes_sent}
    return {"stance": adstance.UNKNOWN, "sentence": ""}


def test_ad_stance_fast_exit_never_changes_the_answer():
    pieces = RESTRICTED_ADS + GREEN_CARD_YES + NO + YES + NEITHER
    rng = random.Random(3)
    texts = _corpus(150, seed=5)
    for i in range(300):
        picked = rng.sample(pieces, rng.randint(0, 3)) + rng.sample(texts, 2)
        rng.shuffle(picked)
        text = rng.choice(["\n", " ", ".  ", " \n"]).join(picked)
        if rng.random() < 0.2:
            text = text.replace(" ", "   ")
        assert ad_stance(text) == _plain_ad_stance(text), text


def test_the_restricted_prefilter_agrees_with_the_regex():
    for text in _corpus() + [s.upper() for s in RESTRICTED_ADS] + ["Top  Secret", "Q clearance"]:
        assert adstance._maybe_restricted(text) == (RESTRICTED_RE.search(text) is not None), text


def test_read_workers_small_builds_stay_in_one_process():
    assert feedfile.read_workers(10, env={}) == 1
    assert feedfile.read_workers(10, env={"FEED_READ_WORKERS": "3"}) == 3
    assert feedfile.read_workers(10 ** 6, env={"FEED_READ_WORKERS": "1"}) == 1
    assert feedfile.read_workers(10 ** 6, env={}, threads=2) == 1    # never fork beside a thread


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="needs fork")
def test_a_parallel_build_writes_the_same_feed_as_one_process(tmp_path, monkeypatch):
    bodies = _corpus(60, seed=9)
    rows, jd = [], {}
    for i in range(feedfile.PARALLEL_MIN_ROWS + 37):
        sid = f"greenhouse:acme:{i}"
        rows.append({"source_id": sid, "source": "greenhouse", "company": f"Co {i % 50}",
                     "title": bodies[i % len(bodies)][:60] or "Engineer", "location": "NY",
                     "url": f"https://x/{i}", "posted_at": "2026-10-20"})
        jd[sid] = bodies[(i * 3) % len(bodies)] + ("\nVisa sponsorship is available." if i % 5 == 0 else "")
    out = {}
    for workers in ("1", "3"):
        monkeypatch.setenv("FEED_READ_WORKERS", workers)
        d = tmp_path / workers
        m = feedfile.write_feed(d, rows, jd, generated_at="2026-10-21T00:00:00+00:00", part_bytes=20000)
        files = sorted(p for p in d.rglob("*") if p.is_file())
        out[workers] = (m, {p.relative_to(d).as_posix(): p.read_bytes() for p in files})
    assert out["1"] == out["3"]
    jobs = json.loads(gzip.decompress(out["1"][1][feedfile.JOBS_FILE]))["jobs"]
    assert any(r.get("ad_stance") == "offered" for r in jobs) and all("tags" in r for r in jobs)
