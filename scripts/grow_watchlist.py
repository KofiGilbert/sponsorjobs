"""Grow the committed watchlist (seed/watchlist_seed.csv) from the ranked H-1B sponsor seed.

Ranks the 100k+ employers in seed/sponsors_seed.csv.gz by H-1B approvals (recent filing
years weighted higher), skips the ones already on the watchlist, and probes the public ATS
endpoints for the top N (sourcing/growth.py + sourcing/discover.py). A board is appended
only when its identity is confirmed AND its live feed returned at least one job. GREEN lane
only: one small public JSON request per probe, per-host spacing, 429 back-off, no scraping.

Polite and resumable: progress is checkpointed per employer (data/grow_watchlist.jsonl, one
JSON line each), so stop it any time (--max-minutes, Ctrl-C) and rerun to continue — nothing
is probed twice. The seed file is rewritten sorted and deduped at the end of every run, so
the output is always a clean commit.

    python scripts/grow_watchlist.py [--limit 3000] [--max-minutes 90] [--workers 8]
                                     [--host-delay 0.25] [--timeout 8]
                                     [--checkpoint data/grow_watchlist.jsonl]
                                     [--seed seed/watchlist_seed.csv] [--dry-run]
                                     [--ats workday,greenhouse]   # probe only these ATSs
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sourcing import growth  # noqa: E402
from sourcing.discover import DISCOVER_ATS  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=3000,
                    help="how many top un-watched sponsors to probe this run (default 3000)")
    ap.add_argument("--max-minutes", type=float, default=90,
                    help="stop submitting new probes after this long (default 90)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--host-delay", type=float, default=growth.HOST_DELAY,
                    help="minimum seconds between requests to one ATS host (all threads)")
    ap.add_argument("--timeout", type=int, default=growth.PROBE_TIMEOUT)
    ap.add_argument("--checkpoint", default=str(ROOT / "data" / "grow_watchlist.jsonl"))
    ap.add_argument("--seed", default=str(growth.WATCHLIST_SEED))
    ap.add_argument("--sponsors", default=str(growth.SPONSOR_SEED))
    ap.add_argument("--dry-run", action="store_true",
                    help="probe and checkpoint, but leave the seed file untouched")
    ap.add_argument("--ats", default=",".join(DISCOVER_ATS),
                    help="comma list of ATSs to probe (default: all of "
                         f"{', '.join(DISCOVER_ATS)}); e.g. --ats workday")
    args = ap.parse_args(argv)
    atss = tuple(a.strip().lower() for a in args.ats.split(",") if a.strip())
    unknown = [a for a in atss if a not in DISCOVER_ATS]
    if unknown or not atss:
        ap.error(f"--ats must name ATSs among {', '.join(DISCOVER_ATS)}; got {args.ats!r}")

    t0 = time.monotonic()
    before = growth.read_watchlist_seed(args.seed)
    before_ats = growth.per_ats_counts(before)
    checkpoint = growth.Checkpoint(args.checkpoint)
    sponsors = growth.load_sponsor_seed(args.sponsors)
    ranked = (growth.candidates(sponsors, checkpoint, growth.watched_norms(before), args.limit, atss)
              if args.limit > 0 else [])        # --limit 0: just flush checkpointed hits to the seed
    owed = sum(1 for r in ranked if r.get("atss"))
    print(f"Watchlist before: {len(before)} boards {before_ats}")
    print(f"Sponsor seed: {len(sponsors)} employers with H-1B approvals; "
          f"{len(checkpoint.records)} already checkpointed; probing the next {len(ranked)} "
          f"({owed} of them only on the ATSs a throttled run still owes) "
          f"across {', '.join(atss)} (workers={args.workers}, "
          f"host delay={args.host_delay}s, timeout={args.timeout}s, "
          f"cap={args.max_minutes:g} min){' [dry run]' if args.dry_run else ''}\n", flush=True)

    def on_progress(i, name, status, why):
        if status == "hit":
            print(f"  [{i}] HIT  {name}  ->  {why}", flush=True)
        elif status == "requeued":
            # Every lane this employer could ask is away (a 429 pause or cooldown): it goes back
            # on the queue and the crawl waits for the lane rather than moving on without asking.
            print(f"  [{i}] WAIT  {name}  ({why}; cooling: {fetch.cooling()})", flush=True)
        elif status in ("throttled", "partial"):
            # Not an HTTP 429 of its own: the employer still owes a probe on a host that was away
            # and is picked up by the next run. The 429 count is in the end-of-run summary.
            print(f"  [{i}] DEFER  {name}  ({why}; cooling: {fetch.cooling()})", flush=True)
        elif i % 50 == 0:
            print(f"  [{i}] ...{i} employers done, {time.monotonic() - t0:.0f}s", flush=True)

    def on_wait(seconds, cooling):
        print(f"  ... every lane is away; waiting {seconds:.0f}s for it (cooling: {cooling})",
              flush=True)

    fetch = growth.PoliteFetch(timeout=args.timeout, host_delay=args.host_delay)
    try:
        summary = growth.grow(ranked, checkpoint, fetch,
                              known_boards={(r["ats"], r["board_id"]) for r in before}, atss=atss,
                              workers=args.workers, max_seconds=args.max_minutes * 60,
                              on_progress=on_progress, on_wait=on_wait)
    except KeyboardInterrupt:
        print("\nInterrupted — progress is checkpointed; rerun to continue.")
        return 130

    # Every verified hit in the checkpoint — this run's AND any earlier run's that was stopped
    # before it could write (Ctrl-C, the time cap mid-flight) — goes into the seed now.
    known = {(r["ats"], r["board_id"]) for r in before}
    new_rows: list[dict] = []
    for b in [*summary["new_boards"], *checkpoint.hits()]:
        key = (b["ats"], b["board_id"])
        if key not in known:
            known.add(key)
            new_rows.append({"company": b.get("company") or b["name"], "ats": b["ats"],
                             "board_id": b["board_id"], "jobs": int(b.get("jobs") or 0)})
    after = before + new_rows
    if not args.dry_run and new_rows:
        after = growth.write_watchlist_seed(args.seed, after)
    after_ats = growth.per_ats_counts(after)
    mins = (time.monotonic() - t0) / 60
    jobs = sum(b["jobs"] for b in new_rows)

    print(f"\n== grow_watchlist summary ({mins:.1f} min, stopped: {summary['stopped']}) ==")
    if summary["stopped"] == "hosts throttled":
        print(f"STOPPED EARLY: every ATS lane this run can use asked to be left alone for longer "
              f"than the run's budget (a 429 with a long Retry-After, or a cooldown); cooling: "
              f"{fetch.cooling()}. Nothing was recorded as a miss -- rerun later to continue.")
    if summary.get("waited"):
        print(f"waited {summary['waited']:.0f}s in total for a lane to come back; "
              f"{summary['requeued']} probes were re-queued and asked again")
    print(f"probed {summary['probed']} employers ({summary['skipped']} skipped without a "
          f"request, {summary['throttled']} throttled on every host and left for the next run, "
          f"{summary['partial']} still owe a throttled ATS); "
          f"{summary['requests']} requests, {summary['rate_limited']} x 429, "
          f"{summary['cooldowns']} host cooldowns, {summary['deferred']} requests deferred "
          f"because a host's queue was too long")
    print(f"found {summary['hits']} live, identity-confirmed boards with >=1 job this run; "
          f"{summary['duplicates']} were already watched; {len(new_rows)} new boards written "
          f"(this run plus any earlier interrupted run's checkpointed hits; "
          f"{jobs} live jobs across them)")
    print(f"new boards per ATS: {summary['per_ats']}")
    print(f"watchlist: {len(before)} -> {len(after)} boards; per ATS {before_ats} -> {after_ats}")
    if summary["rejected"]:
        print(f"rejected live boards ({len(summary['rejected'])}):")
        for r in summary["rejected"][:40]:
            print(f"  - {r['company']}: {r['board']}")
        if len(summary["rejected"]) > 40:
            print(f"  ... and {len(summary['rejected']) - 40} more")
    out = Path(args.checkpoint).with_suffix(".summary.json")
    out.write_text(json.dumps({**summary, "minutes": round(mins, 1), "before": len(before),
                               "after": len(after), "before_ats": before_ats,
                               "after_ats": after_ats, "new_jobs": jobs}, indent=1))
    print(f"(summary written to {out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
