"""Grow the committed watchlist (seed/watchlist_seed.csv) from the ranked sponsor seed.

Ranks the 100k+ employers in seed/sponsors_seed.csv.gz by H-1B approvals (recent filing
years weighted higher) plus certified PERM applications, likely cap-exempt employers
(universities, university hospitals, nonprofit research) boosted, skips the ones already on
the watchlist, and probes the public ATS endpoints for the top N (sourcing/growth.py +
sourcing/discover.py). A board is appended only when its identity is confirmed AND its live
feed returned at least one job; its evidence (URL, verified date, job count, cap-exempt) goes
to seed/watchlist_evidence.csv. GREEN lane only: one small public JSON request per probe,
per-host spacing, 429 back-off, each ATS API's robots.txt obeyed (an ATS it disallows is left
out), and a host that answers 401 / 403 / a challenge page is marked blocked and never asked
again. No scraping, no evasion.

Run it in bounded batches (the default is 300 employers):
    python scripts/grow_watchlist.py --limit 300 --max-minutes 60
    python scripts/grow_watchlist.py --limit 50 --count-us --dry-run   # a small proof run

Polite and resumable: progress is checkpointed per employer (data/grow_watchlist.jsonl, one
JSON line each), so stop it any time (--max-minutes, Ctrl-C) and rerun to continue — nothing
is probed twice. The seed file is rewritten sorted and deduped at the end of every run, so
the output is always a clean commit.

    python scripts/grow_watchlist.py [--limit 3000] [--max-minutes 90] [--workers 8]
                                     [--host-delay 0.25] [--timeout 8]
                                     [--checkpoint data/grow_watchlist.jsonl]
                                     [--seed seed/watchlist_seed.csv] [--dry-run]
                                     [--ats workday,greenhouse]   # probe only these ATSs
                                     [--ipv4]                     # if local IPv6 is broken
                                     [--cap-exempt-only] [--no-cap-boost]
                                     [--evidence seed/watchlist_evidence.csv]
                                     [--count-us]   # count each new board's US jobs (more requests)
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


def _prefer_ipv4() -> None:
    """Make every lookup in this process return IPv4 addresses only (--ipv4)."""
    import socket
    real = socket.getaddrinfo

    def v4(host, port, family=0, *a, **kw):
        return real(host, port, socket.AF_INET, *a, **kw)
    socket.getaddrinfo = v4


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=300,
                    help="how many top un-watched sponsors to probe this run (default 300)")
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
                    help="probe and checkpoint, but leave the seed and evidence files untouched")
    ap.add_argument("--evidence", default=None,
                    help="evidence CSV (default: watchlist_evidence.csv next to --seed)")
    ap.add_argument("--cap-exempt-only", action="store_true",
                    help="probe only likely cap-exempt employers (universities, hospitals, research)")
    ap.add_argument("--no-cap-boost", action="store_true",
                    help="rank cap-exempt employers by their filings alone (no boost)")
    ap.add_argument("--no-robots", action="store_true",
                    help="skip reading the ATS APIs' robots.txt (tests only)")
    ap.add_argument("--count-us", action="store_true",
                    help="after the run, read each NEW board's list once and count its US jobs")
    ap.add_argument("--ipv4", action="store_true",
                    help="resolve hosts to IPv4 only: on a network with broken IPv6 every request "
                         "to a dual-stack API (SmartRecruiters, Ashby) first waits out two IPv6 "
                         "timeouts, which made one employer take two minutes (seen 2026-10-12)")
    ap.add_argument("--ats", default=",".join(DISCOVER_ATS),
                    help="comma list of ATSs to probe (default: all of "
                         f"{', '.join(DISCOVER_ATS)}); e.g. --ats workday")
    args = ap.parse_args(argv)
    if args.evidence is None:
        args.evidence = str(Path(args.seed).with_name(growth.WATCHLIST_EVIDENCE.name))
    atss = tuple(a.strip().lower() for a in args.ats.split(",") if a.strip())
    unknown = [a for a in atss if a not in DISCOVER_ATS]
    if unknown or not atss:
        ap.error(f"--ats must name ATSs among {', '.join(DISCOVER_ATS)}; got {args.ats!r}")

    if args.ipv4:
        _prefer_ipv4()
    t0 = time.monotonic()
    before = growth.read_watchlist_seed(args.seed)
    before_ats = growth.per_ats_counts(before)
    checkpoint = growth.Checkpoint(args.checkpoint)
    sponsors = growth.load_sponsor_seed(args.sponsors)
    fetch = growth.PoliteFetch(timeout=args.timeout, host_delay=args.host_delay,
                               robots=not args.no_robots, blocked_hosts=checkpoint.blocked_hosts())
    if not args.no_robots and args.limit > 0:
        atss_ok, denied = fetch.allowed_atss(atss)
        for a, why in denied.items():
            print(f"robots.txt: leaving {a} out of this run ({why})")
        atss = tuple(atss_ok)
    rank_kw = {"cap_boost": 1.0 if args.no_cap_boost else growth.CAP_EXEMPT_BOOST,
               "cap_exempt_only": args.cap_exempt_only}
    ranked = (growth.candidates(sponsors, checkpoint, growth.watched_norms(before), args.limit,
                                atss, **rank_kw)
              if args.limit > 0 and atss else [])   # --limit 0: just flush checkpointed hits to the seed
    caps = sum(1 for r in ranked if r.get("cap_exempt_likely"))
    owed = sum(1 for r in ranked if r.get("atss"))
    print(f"Watchlist before: {len(before)} boards {before_ats}")
    print(f"Sponsor seed: {len(sponsors)} employers with H-1B approvals; "
          f"{len(checkpoint.records)} already checkpointed; probing the next {len(ranked)} "
          f"({owed} of them only on the ATSs a throttled run still owes) "
          f"across {', '.join(atss)} (workers={args.workers}, "
          f"host delay={args.host_delay}s, timeout={args.timeout}s, "
          f"cap={args.max_minutes:g} min){' [dry run]' if args.dry_run else ''}; "
          f"{caps} of them likely cap-exempt\n", flush=True)

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
    by_norm = {r["norm_name"]: r for r in sponsors}
    new_rows: list[dict] = []
    evidence: list[dict] = []
    for b in [*summary["new_boards"], *checkpoint.hits()]:
        key = (b["ats"], b["board_id"])
        if key not in known:
            known.add(key)
            company = b.get("company") or b["name"]
            new_rows.append({"company": company, "ats": b["ats"],
                             "board_id": b["board_id"], "jobs": int(b.get("jobs") or 0)})
            sponsor = by_norm.get(b.get("norm") or growth.normalize_employer(company)) or \
                {"display_name": company}
            evidence.append(growth.evidence_row({**b, "company": company}, sponsor))
    if args.count_us and evidence:
        print(f"\nCounting US jobs on the {len(evidence)} new boards (one list read each)...",
              flush=True)
        for ev in evidence:
            ev["us_jobs"] = growth.count_us_jobs(ev["ats"], ev["board_id"], ev["company"])
            print(f"  {ev['company']} [{ev['ats']}:{ev['board_id']}] jobs={ev['jobs']} "
                  f"us={ev['us_jobs']}{' cap-exempt' if ev['cap_exempt'] else ''}", flush=True)
    after = before + new_rows
    if not args.dry_run and new_rows:
        after = growth.write_watchlist_seed(args.seed, after)
        growth.write_evidence(args.evidence, evidence)
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
          f"{summary.get('server_errors')} x 5xx, "
          f"{summary['cooldowns']} host cooldowns, {summary['deferred']} requests deferred "
          f"because a host's queue was too long")
    print(f"found {summary['hits']} live, identity-confirmed boards with >=1 job this run; "
          f"{summary['duplicates']} were already watched; {len(new_rows)} new boards written "
          f"(this run plus any earlier interrupted run's checkpointed hits; "
          f"{jobs} live jobs across them)")
    print(f"new boards per ATS: {summary['per_ats']}; {sum(int(e['cap_exempt']) for e in evidence)} "
          f"of the new boards are likely cap-exempt employers")
    if args.count_us and evidence:
        us = [e["us_jobs"] for e in evidence if e.get("us_jobs") is not None]
        print(f"US jobs on the new boards: {sum(us)} (of {jobs} listed; "
              f"{len(evidence) - len(us)} boards could not be read)")
    if summary.get("robots_denied"):
        print(f"robots.txt disallowed: {summary['robots_denied']}")
    if summary.get("blocked"):
        print(f"blocked hosts ({len(summary['blocked'])}; 401 / 403 / challenge, never asked again):")
        for b in summary["blocked"][:20]:
            print(f"  - {b['company']}: {b['host']} ({b['why']})")
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
                               "after_ats": after_ats, "new_jobs": jobs,
                               "evidence": evidence}, indent=1))
    print(f"(summary written to {out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
