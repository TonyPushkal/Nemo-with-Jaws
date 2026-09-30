#!/usr/bin/env python3
"""Minimal feasibility probe: fixed queries -> provider restricted to linkedin.com -> raw findings.

    python scripts/probe_provider.py --dry-run          # no network, no key needed
    NEMO_PAID_CALLS_ENABLED=true NEMO_MAX_USD_PER_RUN=0.20 TAVILY_API_KEY=... \
        python scripts/probe_provider.py

Paid calls are refused unless a budget is configured (see nemo.budget). The API key is read
from the environment only and is never written to disk.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from nemo.budget import Budget, BudgetConfig, BudgetConfigError, PaidCallsDisabled
from nemo.ledger import SqliteLedger
from nemo.probe import FIXED_QUERIES, run_probe
from nemo.providers.tavily import TavilySearch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=["tavily"], default="tavily")
    ap.add_argument("--depth", choices=["basic", "advanced"], default="basic")
    ap.add_argument("--max-results", type=int, default=10)
    ap.add_argument("--queries-file", help="one query per line; default = the fixed generic list")
    ap.add_argument("--out-dir", default="probe_out")
    ap.add_argument("--db", default="data/nemo.sqlite", help="SQLite file holding the spend ledger")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and worst-case cost; no calls")
    args = ap.parse_args(argv)

    queries = ([q.strip() for q in Path(args.queries_file).read_text().splitlines() if q.strip()]
               if args.queries_file else FIXED_QUERIES)
    provider = TavilySearch(os.environ.get("TAVILY_API_KEY", "dry-run-no-key"), search_depth=args.depth)
    worst = provider.max_cost_usd * len(queries)
    try:
        cfg = BudgetConfig.from_env(os.environ)
    except BudgetConfigError as exc:
        print(f"budget configuration error: {exc}", file=sys.stderr)
        return 2
    print(f"Probe: {len(queries)} queries via {provider.name} ({args.depth}), restricted to linkedin.com, "
          f"raw content requested.\nWorst-case cost: ${worst:.3f} "
          f"(per-call max ${provider.max_cost_usd:.3f}). Paid calls: "
          f"{'ENABLED, per-run ceiling $' + str(cfg.max_usd_per_run) if cfg.paid_calls_enabled else 'DISABLED'}")
    for i, q in enumerate(queries, 1):
        print(f"  {i}. {q}")
    if args.dry_run:
        return 0
    if "TAVILY_API_KEY" not in os.environ:
        print("TAVILY_API_KEY is not set.", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out_dir) / stamp
    ledger = SqliteLedger(args.db)
    budget = Budget(cfg, run_id=f"probe-{stamp}", ledger=ledger)
    meta = {"provider": provider.name, "started_at": stamp,
            "params": {"search_depth": args.depth, "include_domains": list(provider.include_domains),
                       "include_raw_content": provider.include_raw_content, "max_results": args.max_results}}
    try:
        summary = run_probe(provider, queries, budget, out_dir, max_results=args.max_results, meta=meta)
    except PaidCallsDisabled as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"\nWrote {out_dir}/summary.md, summary.json and raw/*.json")
    print(f"job-view URLs: {summary['counts']['job_view_urls']} of {summary['counts']['results_total']} results; "
          f"evidence buckets: {summary['counts']['job_evidence_buckets']}")
    return 1 if summary["counts"]["queries_failed"] == summary["counts"]["queries"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
