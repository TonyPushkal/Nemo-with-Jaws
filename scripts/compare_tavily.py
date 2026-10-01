#!/usr/bin/env python3
"""Small Tavily comparison on the user's own roles and locations (follow-up to the first probe).

The first probe (probe_out/20260930T153504Z) sent `site:linkedin.com/jobs/view/ <role>`; Tavily
echoed the query without the operator and 49/50 results were not postings. Tavily documents no
`site:` support but shows paths in `include_domains`, so this compares, with plain queries:

  A  basic,    include_domains=["linkedin.com/jobs/view"]
  B  basic,    same + country="india" (a documented boost, not a filter)
  C  advanced, same + country="india", chunks_per_source=3

No `time_range`: it filters on Tavily's own date estimate, which is never posting-time evidence
and could hide postings; it may be tried later only as a narrowing step.

One shared budget guard caps the WHOLE comparison at $0.10 (worst case: 3x1 + 3x1 + 3x2 = 12
credits = $0.096 at $0.008/credit). Requires TAVILY_API_KEY and NEMO_PAID_CALLS_ENABLED=true
(environment or .env). The profile file stays local; only role/location strings are sent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from nemo.budget import Budget, BudgetConfig, BudgetConfigError, PaidCallsDisabled
from nemo.envfile import load_env_file
from nemo.ledger import SqliteLedger
from nemo.probe import _LOGIN, _SIMILAR, NON_POSTING_QUALIFIERS, run_probe, scan_time_mentions
from nemo.profile import ProfileError, load_profile
from nemo.providers.tavily import TavilySearch

CAP_USD = Decimal("0.10")
CITIES = ["Bengaluru", "Hyderabad", "Visakhapatnam"]
PATH = ("linkedin.com/jobs/view",)
VARIANTS = [
    ("A_basic_path", dict(search_depth="basic", include_domains=PATH)),
    ("B_basic_path_country", dict(search_depth="basic", include_domains=PATH, country="india")),
    ("C_advanced_path_country", dict(search_depth="advanced", include_domains=PATH, country="india", chunks_per_source=3)),
]


def queries_for(roles: tuple[str, ...], n: int) -> list[str]:
    """Pair roles with cities round-robin; `n` queries in total."""
    return [f"{roles[i % len(roles)]} {CITIES[i % len(CITIES)]}" for i in range(n)]


def classify_dates(text: str) -> str:
    ms = [m for m in scan_time_mentions(text) if m["qualifier"] not in NON_POSTING_QUALIFIERS]
    kinds = {m["kind"] for m in ms}
    if {"structured_datePosted", "long_date"} & kinds:
        return "absolute"
    if "iso_date" in kinds:
        return "absolute_candidate"
    if "relative" in kinds:
        return "relative_only(no retrieval time)"
    if scan_time_mentions(text):
        return "only_reposted/updated"
    return "none"


def describe(result: dict) -> dict:
    from nemo.linkedin import linkedin_job_id
    raw, snip = result.get("raw_content") or "", result.get("content") or ""
    text = raw or snip
    usable = len(text) >= 500 and not (_LOGIN.search(text) and len(text) < 1500)
    return {"url": result.get("url"), "job_id": linkedin_job_id(result.get("url", "")), "title": result.get("title"),
            "raw_len": len(raw), "snippet_len": len(snip), "usable_description": usable,
            "login_wall": bool(_LOGIN.search(text)), "similar_jobs": bool(_SIMILAR.search(text)),
            "date_evidence": classify_dates(text), "provider_published_date": result.get("published_date"),
            "date_quotes": [m["context"] for m in scan_time_mentions(text)][:3]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="job-profile.md")
    ap.add_argument("--queries-per-variant", type=int, default=3)
    ap.add_argument("--max-results", type=int, default=10)
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--db", default="data/nemo.sqlite")
    ap.add_argument("--out-dir", default="probe_out/compare")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)

    load_env_file(args.env_file)
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        print(f"invalid profile: {exc}", file=sys.stderr)
        return 2
    queries = queries_for(profile.desired_roles, args.queries_per_variant)
    worst = sum(TavilySearch("x", **{k: v for k, v in kw.items()}).max_cost_usd for _, kw in VARIANTS) * len(queries)
    print(f"Queries ({len(queries)} per variant): {queries}")
    for name, kw in VARIANTS:
        print(f"  {name}: {kw}")
    print(f"Worst case ${worst:.3f}; hard cap for the whole comparison ${CAP_USD}.")
    if Decimal(str(worst)) > CAP_USD:
        print("Planned worst case exceeds the cap; reduce --queries-per-variant.", file=sys.stderr)
        return 2
    if args.dry_run:
        return 0
    if not os.environ.get("TAVILY_API_KEY"):
        print("TAVILY_API_KEY is not set (environment or .env).", file=sys.stderr)
        return 2
    try:
        env_cfg = BudgetConfig.from_env(os.environ)
    except BudgetConfigError as exc:
        print(f"budget configuration error: {exc}", file=sys.stderr)
        return 2
    if not env_cfg.paid_calls_enabled:
        print("Paid calls are disabled (NEMO_PAID_CALLS_ENABLED).", file=sys.stderr)
        return 2
    if not args.yes and (not sys.stdin.isatty() or input("Proceed? [y/N] ").strip().lower() != "y"):
        print("Not confirmed; nothing sent.", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(args.out_dir) / stamp
    cap = min(CAP_USD, env_cfg.max_usd_per_run)
    budget = Budget(BudgetConfig(paid_calls_enabled=True, max_usd_per_run=cap, monthly_usd_cap=env_cfg.monthly_usd_cap,
                                 max_queries=len(VARIANTS) * len(queries)),
                    run_id=f"compare-{stamp}", ledger=SqliteLedger(args.db))
    report = {"queries": queries, "cap_usd": str(cap), "variants": {}}
    for name, kw in VARIANTS:
        provider = TavilySearch(os.environ["TAVILY_API_KEY"], **kw)
        try:
            s = run_probe(provider, queries, budget, root / name, max_results=args.max_results,
                          meta={"provider": "tavily", "params": {k: list(v) if isinstance(v, tuple) else v for k, v in kw.items()}})
        except PaidCallsDisabled as exc:
            print(exc, file=sys.stderr)
            return 2
        results = []
        for i in range(1, len(queries) + 1):
            f = root / name / "raw" / f"{i:02d}.json"
            if f.exists():
                raw = json.loads(f.read_text())
                results += [{"query": queries[i - 1], "echoed_query": raw.get("query"), **describe(r)}
                            for r in raw.get("results", [])]
        jobs = [r for r in results if r["job_id"]]
        uniq = {r["job_id"] for r in jobs}
        report["variants"][name] = {
            "params": kw if not isinstance(kw.get("include_domains"), tuple) else {**kw, "include_domains": list(kw["include_domains"])},
            "queries_failed": s["counts"]["queries_failed"], "stopped_by_cap": s["meta"]["stopped_by_cap"],
            "credits": s["counts"]["credits_reported"], "results": len(results),
            "individual_posting_urls": len(jobs), "unique_postings": len(uniq),
            "with_usable_description": sum(r["usable_description"] for r in jobs),
            "with_raw_content": sum(r["raw_len"] > 0 for r in jobs),
            "date_evidence": {k: sum(r["date_evidence"] == k for r in jobs) for k in sorted({r["date_evidence"] for r in jobs})},
            "echoed_queries": sorted({r["echoed_query"] for r in results if r["echoed_query"]}),
            "items": results}
        v = report["variants"][name]
        print(f"{name}: results {v['results']}, posting URLs {v['individual_posting_urls']} (unique {v['unique_postings']}), "
              f"usable descriptions {v['with_usable_description']}, raw content {v['with_raw_content']}, "
              f"dates {v['date_evidence']}, credits {v['credits']}", flush=True)
    report["usage"] = budget.usage()
    (root / "comparison.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"usage {report['usage']}; wrote {root}/comparison.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
