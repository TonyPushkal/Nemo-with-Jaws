#!/usr/bin/env python3
"""Provider-based job collection (Bright Data LinkedIn Jobs). The search-based collector
`scripts/run_search.py` is separate and unchanged.

    collect.py run --role "Platform Engineer" --role "DevOps Engineer" [--location India ...] [--max-records 25] [--dry-run]
    collect.py get <run_id> [--limit 50] [--raw]
    collect.py resume <run_id>        # re-attach to an interrupted/timed-out run; never launches a new one
    collect.py recover                # mark runs whose process died (launching -> launch_unknown, running -> interrupted)

Needs BRIGHTDATA_API_KEY, NEMO_PAID_CALLS_ENABLED=true and NEMO_MAX_USD_PER_RUN (env or .env). The per-run
ceiling is checked against the WORST-CASE price (max_records x --usd-per-record, default the published $1.50/1,000),
even though the first 5,000 records/month are free: free-tier use is not assumed.
Exit: 0 succeeded/no_results/partial, 1 failed/timed_out/interrupted/launch_unknown, 2 usage/config.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from nemo.budget import Budget, BudgetConfig, BudgetConfigError
from nemo.collect import collect, get_run, recover_stale, resume
from nemo.collect_store import CollectStore
from nemo.envfile import load_env_file
from nemo.jobsource import CollectRequest
from nemo.ledger import SqliteLedger
from nemo.providers.brightdata import BrightDataJobs

OK = {"succeeded", "no_results", "partial"}


def summarize(run: dict) -> str:
    lines = [f"run {run['run_id']}  status={run['status']}  provider_op_id={run['provider_op_id']}",
             f"  jobs_stored={run['jobs_stored']} records_received={run['records_received']} "
             f"error_records={run['error_records']} duplicates={run['duplicates']}",
             f"  est_max_usd={run['est_max_usd']} est_billed_usd={run['est_billed_usd']} "
             f"measured_usd={run['measured_usd']}  (estimates from published price; measured needs the dashboard)"]
    if run.get("reason"):
        lines.append(f"  reason: {run['reason']}")
    for j in run.get("jobs", [])[:20]:
        lines.append(f"  - {j['source_job_id']} [{j['description_status']}] {j['title']} | {j['company']} | "
                     f"{j['location']} | {j['url']}" + (f"  FLAGS: {'; '.join(j['flags'])}" if j["flags"] else ""))
    return "\n".join(lines)


def main(argv=None, *, provider_factory=BrightDataJobs) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--db", default="data/nemo.sqlite")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--role", action="append", required=True)
    r.add_argument("--location", action="append", default=[])
    r.add_argument("--country", default="IN")
    r.add_argument("--time-range", help='provider vocabulary, e.g. "Past week"')
    r.add_argument("--max-records", type=int, default=25)
    r.add_argument("--usd-per-record", type=float, default=0.0015)
    r.add_argument("--poll-timeout", type=float, default=600)
    r.add_argument("--poll-interval", type=float, default=5)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--yes", action="store_true")
    g = sub.add_parser("get")
    g.add_argument("run_id")
    g.add_argument("--limit", type=int, default=50)
    g.add_argument("--raw", action="store_true")
    rs = sub.add_parser("resume")
    rs.add_argument("run_id")
    rs.add_argument("--poll-timeout", type=float, default=600)
    sub.add_parser("recover")
    args = ap.parse_args(argv)

    load_env_file(args.env_file)
    store = CollectStore(args.db)
    try:
        if args.cmd == "get":
            try:
                print(json.dumps(get_run(args.run_id, store, limit=args.limit, include_raw=args.raw), indent=2,
                                 ensure_ascii=False, default=str))
            except KeyError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            return 0
        if args.cmd == "recover":
            print("recovered:", recover_stale(store) or "nothing to recover")
            return 0
        key = os.environ.get("BRIGHTDATA_API_KEY")
        if args.cmd == "resume":
            if not key:
                print("BRIGHTDATA_API_KEY is not set (.env or environment).", file=sys.stderr)
                return 2
            try:
                run = resume(args.run_id, provider_factory(key), store, poll_timeout_s=args.poll_timeout)
            except KeyError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(summarize(run))
            return 0 if run["status"] in OK else 1
        # run
        req = CollectRequest(roles=tuple(args.role), locations=tuple(args.location or ["India"]),
                             country=args.country, time_range=args.time_range, max_records=args.max_records,
                             poll_timeout_s=args.poll_timeout, poll_interval_s=args.poll_interval)
        try:
            from nemo.collect import validate
            validate(req)
            cfg = BudgetConfig.from_env(os.environ)
        except (ValueError, BudgetConfigError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        worst = req.max_records * args.usd_per_record
        print(f"Plan: {req.inputs()} search input(s) {list(req.roles)} x {list(req.locations)} (country {req.country}); "
              f"up to {req.max_records} records; worst case ${worst:.4f} at ${args.usd_per_record}/record "
              f"(published; free-tier credits not assumed). Paid calls "
              f"{'ENABLED' if cfg.paid_calls_enabled else 'DISABLED'}, ceiling ${cfg.max_usd_per_run}.")
        if args.dry_run:
            print(json.dumps(provider_factory("DRY-RUN", usd_per_record=args.usd_per_record).trigger_body(req), indent=1))
            return 0
        if not key:
            print("BRIGHTDATA_API_KEY is not set (.env or environment).", file=sys.stderr)
            return 2
        if not cfg.paid_calls_enabled:
            print("Paid calls are disabled: set NEMO_PAID_CALLS_ENABLED=true and NEMO_MAX_USD_PER_RUN.", file=sys.stderr)
            return 2
        if not args.yes and (not sys.stdin.isatty() or input("Launch this collection? [y/N] ").strip().lower() != "y"):
            print("Not confirmed; nothing sent.", file=sys.stderr)
            return 1
        budget = Budget(BudgetConfig(paid_calls_enabled=True, max_usd_per_run=cfg.max_usd_per_run,
                                     monthly_usd_cap=cfg.monthly_usd_cap, max_queries=1),
                        run_id="collect", ledger=SqliteLedger(args.db))
        run = collect(req, provider_factory(key, usd_per_record=args.usd_per_record), store, budget)
        print(summarize(run))
        return 0 if run["status"] in OK else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
