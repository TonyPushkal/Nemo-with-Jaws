#!/usr/bin/env python3
"""EXPLICIT OPT-IN live smoke test: one real Bright Data collection of 25-50 Indian LinkedIn jobs.

    NEMO_LIVE_SMOKE=1 .venv/bin/python scripts/live_smoke_brightdata.py --yes [--max-records 25] [--role "Platform Engineer"]

Refuses unless NEMO_LIVE_SMOKE=1, BRIGHTDATA_API_KEY, NEMO_PAID_CALLS_ENABLED=true and NEMO_MAX_USD_PER_RUN are set.
Hard cap 50 records (<= $0.075 even at pay-as-you-go; $0 if inside the free 5,000 credits). Before running, check the
Bright Data dashboard shows free credits left and that no funds are deposited / auto-recharge is off, otherwise the
overage would be billed. It prints measurements; the real cost must be read from the dashboard (no billing API is
used here), so `measured_usd` stays empty and the report states only the published-rate estimate.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from nemo.budget import Budget, BudgetConfig, BudgetConfigError
from nemo.collect import collect, get_run
from nemo.collect_store import CollectStore
from nemo.envfile import load_env_file
from nemo.jobsource import CollectRequest
from nemo.ledger import SqliteLedger
from nemo.providers.brightdata import BrightDataJobs

HARD_CAP = 50
INDIA_HINTS = ("india", "bengaluru", "bangalore", "hyderabad", "pune", "mumbai", "chennai", "delhi", "gurugram",
               "gurgaon", "noida", "kolkata", "ahmedabad", "visakhapatnam", "kochi", "coimbatore", "jaipur", "indore")


def validate_jobs(jobs: list[dict]) -> dict:
    """Checks on stored jobs; every figure is about provider-returned data, none is independent verification."""
    from nemo.linkedin import linkedin_job_id
    n = len(jobs)
    ids = [j["source_job_id"] for j in jobs]
    lens = sorted(len(j["description"] or "") for j in jobs)
    bad = [j["source_job_id"] for j in jobs if linkedin_job_id(j["url"]) != j["source_job_id"]]
    link_flagged = [j["source_job_id"] for j in jobs if any(f.startswith("link_check") for f in j["flags"])]
    return {"jobs": n, "unique_ids": len(set(ids)),
            "url_is_job_url_with_matching_id": n - len(bad), "url_id_problems": bad,
            "link_check_flagged": link_flagged,
            "description_full": sum(j["description_status"] == "full" for j in jobs),
            "description_short_or_truncated_or_missing": [j["source_job_id"] for j in jobs if j["description_status"] != "full"],
            "description_chars_min_median_max": (lens[0], lens[n // 2], lens[-1]) if n else None,
            "india_location": sum(any(h in (j["location"] or "").lower() for h in INDIA_HINTS) for j in jobs),
            "non_india_location": [(j["source_job_id"], j["location"]) for j in jobs
                                   if not any(h in (j["location"] or "").lower() for h in INDIA_HINTS)]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", action="append", default=[])
    ap.add_argument("--location", action="append", default=[])
    ap.add_argument("--max-records", type=int, default=25)
    ap.add_argument("--db", default="data/nemo.sqlite")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--validate-run", metavar="RUN_ID", help="only validate a stored run; no network, no spend")
    args = ap.parse_args(argv)
    load_env_file(args.env_file)
    if args.validate_run:
        import json
        st = CollectStore(args.db)
        try:
            print(json.dumps(validate_jobs(get_run(args.validate_run, st, limit=1000)["jobs"]), indent=1, default=str))
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        finally:
            st.close()
        return 0
    if os.environ.get("NEMO_LIVE_SMOKE") != "1" or not args.yes:
        print("Live smoke test is opt-in: set NEMO_LIVE_SMOKE=1 and pass --yes. Nothing sent.", file=sys.stderr)
        return 2
    key = os.environ.get("BRIGHTDATA_API_KEY")
    if not key:
        print("BRIGHTDATA_API_KEY is not set. Nothing sent.", file=sys.stderr)
        return 2
    if not 1 <= args.max_records <= HARD_CAP:
        print(f"--max-records must be 1..{HARD_CAP}", file=sys.stderr)
        return 2
    try:
        cfg = BudgetConfig.from_env(os.environ)
    except BudgetConfigError as exc:
        print(f"budget configuration error: {exc}", file=sys.stderr)
        return 2
    if not cfg.paid_calls_enabled:
        print("Set NEMO_PAID_CALLS_ENABLED=true and NEMO_MAX_USD_PER_RUN (>= 0.075 for 50 records).", file=sys.stderr)
        return 2
    req = CollectRequest(roles=tuple(args.role or ["Platform Engineer"]), locations=tuple(args.location or ["India"]),
                         max_records=args.max_records)
    store = CollectStore(args.db)
    budget = Budget(BudgetConfig(paid_calls_enabled=True, max_usd_per_run=cfg.max_usd_per_run,
                                 monthly_usd_cap=cfg.monthly_usd_cap, max_queries=1), run_id="live-smoke",
                    ledger=SqliteLedger(args.db))
    t0 = time.monotonic()
    run = collect(req, BrightDataJobs(key), store, budget)
    elapsed = time.monotonic() - t0
    jobs = get_run(run["run_id"], store, limit=HARD_CAP)["jobs"]
    full = sum(j["description_status"] == "full" for j in jobs)
    linkflag = sum(any(f.startswith(("link_check", "url_id_mismatch", "source url")) for f in j["flags"]) for j in jobs)
    print(f"status={run['status']} reason={run['reason']}")
    print(f"run_id={run['run_id']} provider_op_id={run['provider_op_id']}")
    print(f"latency_s={elapsed:.1f} records_received={run['records_received']} unique_jobs={len(jobs)} "
          f"error_records={run['error_records']}")
    print(f"full_description={full}/{len(jobs)}  url_or_description_flagged={linkflag}/{len(jobs)}")
    import json
    print(json.dumps(validate_jobs(jobs), indent=1, default=str))
    print(f"est_billed_usd={run['est_billed_usd']} (published rate x records; NOT measured). "
          "Read the actual credits used from the Bright Data dashboard and compare.")
    store.close()
    return 0 if run["status"] in {"succeeded", "no_results", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
