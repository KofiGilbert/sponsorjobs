"""_job_from_feed resolves a job by source_id from the central feed when the local store misses
it (on-demand search results are never upserted). Regression: 'Tailor & apply' dead-ended with a
404 'job not found' for exactly those roles."""
from __future__ import annotations

import json
import urllib.request

import ui.app as app


def test_job_from_feed_none_without_env(monkeypatch):
    monkeypatch.delenv("JOBS_FEED_URL", raising=False)
    assert app._job_from_feed("greenhouse:acme:1") is None


def test_job_from_feed_none_for_blank_sid(monkeypatch):
    monkeypatch.setenv("JOBS_FEED_URL", "http://feed.local")
    assert app._job_from_feed("") is None


def test_job_from_feed_resolves_from_the_static_feed(monkeypatch, tmp_path):
    # The feed is a static jobs.json.gz + JD shards (docs/feed.md); the row comes from the cached
    # list and jd_text from its shard.
    import gzip

    from sourcing.feedfile import shard_for
    monkeypatch.setenv("JOBS_FEED_URL", "http://feed.local/")
    monkeypatch.setattr(app, "_DATA", tmp_path)
    app._STATIC_FEEDS.clear()
    row = {"source_id": "greenhouse:anthropic:1", "title": "Applied AI Engineer", "company": "Anthropic"}

    class _Resp:
        def __init__(self, body): self._b = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._b

    def _fake_urlopen(req, timeout=30):
        if req.full_url.endswith("/jobs.json.gz"):
            return _Resp(gzip.compress(json.dumps({"feed_version": 1, "jobs": [row]}).encode()))
        assert req.full_url.endswith(f"/jd/{shard_for(row['source_id'])}.json.gz")
        return _Resp(gzip.compress(json.dumps(
            {row["source_id"]: "Build forward-deployed AI systems for customers."}).encode()))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    job = app._job_from_feed("greenhouse:anthropic:1")
    assert job is not None
    assert job["title"] == "Applied AI Engineer"
    assert job["jd_text"] == "Build forward-deployed AI systems for customers."
    assert app._job_from_feed("greenhouse:anthropic:2") is None     # not in the feed -> None
