"""CVRecords.merge_data is one write transaction (2026-11-23): a page request and a background
sweep (the board's open-or-closed recheck) merging into the same application at the same time
each keep the other's change. Before, each read the record, then wrote it back whole, and the
later write put back what it had read, losing the other's field (seen as the Ready to send
answers list disappearing in tests/test_panel_answers.py)."""
from __future__ import annotations

import json
import threading

from ui.records import CVRecords


def test_parallel_merges_from_separate_connections_keep_every_field(tmp_path):
    db = tmp_path / "records.db"
    first = CVRecords(db)
    rid = first.add("Analyst", "Example Co", 80, "", data={"status": "approved"})
    first.close()

    errors = []

    def worker(n):
        recs = CVRecords(db)
        try:
            for i in range(25):
                recs.merge_data(rid, {f"k{n}_{i}": i})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            recs.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    recs = CVRecords(db)
    try:
        data = json.loads(recs.get(rid)["data"])
    finally:
        recs.close()
    assert data["status"] == "approved"
    assert sum(1 for k in data if k.startswith("k")) == 6 * 25


def test_merge_into_a_missing_record_raises_and_leaves_no_transaction_open(tmp_path):
    recs = CVRecords(tmp_path / "records.db")
    try:
        try:
            recs.merge_data(999, {"x": 1})
        except KeyError:
            pass
        else:
            raise AssertionError("expected KeyError")
        assert not recs._conn.in_transaction
        rid = recs.add("Analyst", "Example Co", 80, "")
        assert recs.merge_data(rid, {"x": 1}) == {"x": 1}
    finally:
        recs.close()
