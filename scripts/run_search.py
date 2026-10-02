#!/usr/bin/env python3
"""Phase 1 discovery run: job profile + lookback -> all related jobs with individual LinkedIn URLs.

    .venv/bin/python scripts/run_search.py --profile job-profile.md --lookback 7d \\
        --location "Bengaluru,Karnataka,India" --location "Hyderabad,Telangana,India" \\
        --location "Visakhapatnam,Andhra Pradesh,India" --gl in

Collects broadly (no fit, seniority or date filtering), de-duplicates by LinkedIn job id, and saves
title, company, location, description, sources, Google-reported posting age (best effort, labelled
unverified) and first/last-seen times. Excludes only listing/link pairs whose company or title clearly
disagree with the LinkedIn URL. Local-model assessment is optional (--assess) and never filters.

Keys and budget come from .env (or the environment): SERPAPI_API_KEY, NEMO_PAID_CALLS_ENABLED=true,
NEMO_MAX_USD_PER_RUN, optional NEMO_MONTHLY_USD_CAP. Each search is reserved at --usd-per-search
(default 0.025, SerpApi Starter's rate) before it is sent; the run stops cleanly at the cap and keeps
what it collected. Results: runs/<timestamp>/ and data/nemo.sqlite.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from nemo.budget import Budget, BudgetConfig, BudgetConfigError, BudgetExceeded, PaidCallsDisabled
from nemo.discovery import merge, parse_google, parse_google_jobs, parse_lookback, plan
from nemo.envfile import load_env_file
from nemo.ledger import SqliteLedger
from nemo.profile import ProfileError, load_profile
from nemo.providers.base import ProviderError
from nemo.providers.serpapi import SerpApi
from nemo.store import Store

TRANSIENT = {"timeout", "rate_limited", "server_error", "connection_error"}
CSV_FIELDS = ["linkedin_job_id", "url", "title", "company", "location", "reported_age", "reported_age_source",
              "reported_age_basis", "lookback_status", "description_level", "description_chars", "engines", "vias",
              "first_seen_at", "last_seen_at", "previously_seen", "assessment_outcome", "flags"]


def now() -> datetime:
    return datetime.now(timezone.utc)


def search_with_retry(api: SerpApi, budget: Budget, engine: str, params: dict) -> dict:
    """One retry for transient failures; each attempt is reserved through the budget guard."""
    for attempt in (1, 2):
        try:
            with budget.call("queries", api.name, is_paid=True, max_cost_usd=api.max_cost_usd):
                return api.search_raw(engine, params)
        except ProviderError as exc:
            if attempt == 2 or exc.kind not in TRANSIENT:
                raise
    raise AssertionError("unreachable")


def assess_all(jobs: dict, profile, limit: int) -> dict:
    """Optional enrichment. Failures are recorded; nothing is filtered or hidden."""
    from nemo.providers.ollama import OllamaLLM
    from nemo.tasks import assess_job
    llm, stats = OllamaLLM(), {"assessed": 0, "failed": 0, "skipped": 0}
    try:
        llm.installed_models()
    except Exception as exc:  # noqa: BLE001
        print(f"  local model unavailable ({exc}); skipping assessment", flush=True)
        stats["skipped"] = len(jobs)
        return stats
    for j in sorted(jobs.values(), key=lambda j: -len(j["description"])):
        if stats["assessed"] + stats["failed"] >= limit or not j["description"]:
            stats["skipped"] += 1
            continue
        try:
            r = assess_job(llm, profile, j["description"])
            j["assessment"] = {"outcome": r.outcome, "explanation": r.explanation, "note": r.note,
                               "criteria": [c.__dict__ for c in r.criteria], "content_level": r.content_level,
                               "source": "local model (optional enrichment; not a filter)"}
            stats["assessed"] += 1
        except ProviderError as exc:
            j["assessment"] = {"outcome": "assessment_failed", "error": str(exc)}
            stats["failed"] += 1
        print(f"  assessed {j['linkedin_job_id']}: {j['assessment']['outcome']}", flush=True)
    return stats


def csv_row(j: dict) -> dict:
    b = j.get("best_reported_age") or {}
    return {"linkedin_job_id": j["linkedin_job_id"], "url": j["url"], "title": j["title"], "company": j["company"],
            "location": j["location"], "reported_age": b.get("raw"), "reported_age_source": b.get("source"),
            "reported_age_basis": b.get("basis"), "lookback_status": j["lookback_status"],
            "description_level": j["description_level"], "description_chars": len(j["description"]),
            "engines": ";".join(sorted({s["engine"] for s in j["sources"]})),
            "vias": ";".join(sorted({s["via"] or "" for s in j["sources"]})),
            "first_seen_at": j["first_seen_at"], "last_seen_at": j["last_seen_at"],
            "previously_seen": j["previously_seen"], "assessment_outcome": (j.get("assessment") or {}).get("outcome"),
            "flags": " | ".join(j["flags"])}


def render_summary(meta: dict, jobs: list[dict], excluded: list[dict]) -> str:
    s = meta["summary"]
    L = ["# Discovery run", "", f"- Status: **{meta['status']}**" + (f" (stopped by `{meta['stopped_by_cap']}`)" if meta["stopped_by_cap"] else ""),
         f"- Lookback: {meta['lookback']} → window {meta['window_start']} … {meta['window_end']}",
         f"- Searches: {s['searches_ok']} ok, {s['searches_failed']} failed, {s['searches_not_run']} not run (budget)",
         f"- Jobs: **{s['jobs']}** unique LinkedIn postings ({s['new_jobs']} new, {s['previously_seen']} seen before); "
         f"{s['excluded_mismatches']} listing/link mismatches excluded",
         f"- Reported posting age vs lookback (unverified, Google-reported): {s['lookback_status']}",
         "", "> Dates are best-effort reports from Google/Google Jobs, not verified posting times. Nothing is filtered "
             "by date, fit or seniority. Discovery is best-effort, not exhaustive LinkedIn coverage.", "",
         "| # | Title | Company | Location | Reported age (source) | Lookback | Description | Link |", "|---|---|---|---|---|---|---|---|"]
    for n, j in enumerate(jobs, 1):
        b = j.get("best_reported_age") or {}
        age = f"{b.get('raw')} ({b.get('basis')}, via {b.get('via')})" if b else "unknown"
        L.append(f"| {n} | {j['title'] or '?'} | {j['company'] or '?'} | {j['location'] or '?'} | {age} | "
                 f"{j['lookback_status']} | {j['description_level']} ({len(j['description'])}) | {j['url']} |")
    if excluded:
        L += ["", "## Excluded listing/link mismatches", ""]
        L += [f"- {e['linkedin_job_id']} — {e['title']} / {e['company']} (via {e['via']}): {'; '.join(e['problems'])}" for e in excluded]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="job-profile.md")
    ap.add_argument("--lookback", required=True, help="e.g. 24h, 7d, 2w (1h..30d)")
    ap.add_argument("--location", action="append", default=[], help="SerpApi location for Google Jobs (repeatable)")
    ap.add_argument("--gl", help="Google country code, e.g. in")
    ap.add_argument("--usd-per-search", type=float, default=0.025)
    ap.add_argument("--max-searches", type=int, help="stop after N searches even if budget remains")
    ap.add_argument("--assess", action="store_true", help="optional local-model enrichment (Ollama)")
    ap.add_argument("--assess-max", type=int, default=10)
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--db", default="data/nemo.sqlite")
    ap.add_argument("--out-dir", default="runs")
    ap.add_argument("--dry-run", action="store_true", help="show the plan and cost; no calls")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args(argv)

    load_env_file(args.env_file)
    try:
        profile = load_profile(args.profile)
        lookback = parse_lookback(args.lookback)
    except (ProfileError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    steps = plan(profile.desired_roles, args.location, lookback, args.gl)
    if args.max_searches is not None:
        steps = steps[: args.max_searches]
    try:
        cfg = BudgetConfig.from_env(os.environ)
    except BudgetConfigError as exc:
        print(f"budget configuration error: {exc}", file=sys.stderr)
        return 2
    affordable = int(float(cfg.max_usd_per_run) // args.usd_per_search) if cfg.max_usd_per_run else 0
    print(f"Profile {args.profile}: {len(profile.desired_roles)} roles; lookback {args.lookback}; "
          f"{len(args.location)} location(s)")
    print(f"Plan: {len(steps)} searches, worst case ${len(steps) * args.usd_per_search:.3f} at ${args.usd_per_search}/search. "
          f"Budget: paid calls {'ENABLED' if cfg.paid_calls_enabled else 'DISABLED'}, per-run ceiling "
          f"${cfg.max_usd_per_run} → up to {min(affordable, len(steps))} searches this run.")
    if not args.location:
        print("  (no --location: Google Jobs is skipped; it returned nothing without one in testing)")
    if args.dry_run:
        for i, s in enumerate(steps, 1):
            print(f"  {i:>2}. {s.engine:11} {s.params['q']}" + (f"  [location={s.params['location']}]" if "location" in s.params else ""))
        return 0
    key = os.environ.get("SERPAPI_API_KEY")
    if not key:
        print("SERPAPI_API_KEY is not set (.env or environment).", file=sys.stderr)
        return 2
    if not cfg.paid_calls_enabled:
        print("Paid calls are disabled: set NEMO_PAID_CALLS_ENABLED=true and NEMO_MAX_USD_PER_RUN in .env.", file=sys.stderr)
        return 2
    if not args.yes:
        if not sys.stdin.isatty() or input("Proceed? [y/N] ").strip().lower() != "y":
            print("Not confirmed; nothing sent.", file=sys.stderr)
            return 1

    started = now()
    run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    out = Path(args.out_dir) / run_id
    (out / "raw").mkdir(parents=True, exist_ok=True)
    api = SerpApi(key, usd_per_search=args.usd_per_search)
    budget = Budget(BudgetConfig(paid_calls_enabled=True, max_usd_per_run=cfg.max_usd_per_run,
                                 monthly_usd_cap=cfg.monthly_usd_cap, max_queries=2 * len(steps)),
                    run_id=f"search-{run_id}", ledger=SqliteLedger(args.db))
    store = Store(args.db)
    window_end = started
    window = (window_end - lookback, window_end)
    store.start_run(run_id, started.isoformat(), profile_sha256=profile.sha256(), lookback=args.lookback,
                    window_start=window[0].isoformat(), window_end=window[1].isoformat(),
                    params={"locations": args.location, "gl": args.gl, "usd_per_search": args.usd_per_search})

    sightings, excluded, ok, failed, stopped, issues = [], [], 0, 0, None, []
    for seq, step in enumerate(steps, 1):
        q = {"engine": step.engine, "role": step.role, "q": step.params["q"], "location": step.params.get("location")}
        try:
            raw = search_with_retry(api, budget, step.engine, step.params)
        except BudgetExceeded as exc:
            stopped = exc.cap
            break
        except PaidCallsDisabled as exc:
            print(exc, file=sys.stderr)
            return 2
        except ProviderError as exc:
            failed += 1
            issues.append({"seq": seq, **q, "error": str(exc)})
            store.add_query(run_id, seq, **q, status="failed", kept=0, error=str(exc))
            print(f"  {seq:>2}. {step.engine:11} {step.role:30} FAILED: {exc}", flush=True)
            continue
        ok += 1
        (out / "raw" / f"{seq:02d}_{step.engine}.json").write_text(json.dumps(raw, indent=1, ensure_ascii=False))
        if step.engine == "google_jobs":
            kept, bad, _ = parse_google_jobs(raw, window)
            excluded += bad
        else:
            kept, _ = parse_google(raw, window)
        sightings += kept
        store.add_query(run_id, seq, **q, status="empty" if raw.get("error") else "ok", kept=len(kept),
                        search_id=(raw.get("search_metadata") or {}).get("id"))
        print(f"  {seq:>2}. {step.engine:11} {step.role:30} kept {len(kept)}", flush=True)

    jobs = merge(sightings)
    seen_at = now().isoformat()
    store.upsert_jobs(run_id, seen_at, jobs)
    assess_stats = assess_all(jobs, profile, args.assess_max) if args.assess and jobs else None
    if assess_stats:
        store.upsert_jobs(run_id, seen_at, jobs)  # persist enrichment

    order = {"reported_within": 0, "ambiguous": 1, "unknown": 2, "reported_outside": 3}
    ranked = sorted(jobs.values(), key=lambda j: (order[j["lookback_status"]], j["description_level"] != "description"))
    status = "failed" if ok == 0 else "partial" if (stopped or failed) else "complete"
    summary = {"searches_planned": len(steps), "searches_ok": ok, "searches_failed": failed,
               "searches_not_run": len(steps) - ok - failed, "jobs": len(jobs),
               "new_jobs": sum(not j["previously_seen"] for j in jobs.values()),
               "previously_seen": sum(j["previously_seen"] for j in jobs.values()),
               "excluded_mismatches": len(excluded),
               "with_description": sum(j["description_level"] == "description" for j in jobs.values()),
               "lookback_status": {k: sum(j["lookback_status"] == k for j in jobs.values()) for k in order},
               "assessment": assess_stats, "usage": budget.usage()}
    meta = {"run_id": run_id, "status": status, "stopped_by_cap": stopped, "lookback": args.lookback,
            "window_start": window[0].isoformat(), "window_end": window[1].isoformat(), "profile": args.profile,
            "summary": summary, "issues": issues,
            "notes": ["best-effort discovery, not exhaustive LinkedIn coverage",
                      "reported ages are Google/Google Jobs claims, not verified posting times",
                      "no filtering by date, fit or seniority; only clear listing/link mismatches excluded"]}
    (out / "results.json").write_text(json.dumps({**meta, "jobs": ranked, "excluded_mismatches": excluded},
                                                 indent=2, ensure_ascii=False))
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(csv_row(j) for j in ranked)
    (out / "summary.md").write_text(render_summary(meta, ranked, excluded))
    store.finish_run(run_id, now().isoformat(), status, summary)
    store.close()

    print(f"\nStatus: {status}" + (f" (stopped by {stopped})" if stopped else ""))
    print(f"Searches: {ok} ok, {failed} failed, {summary['searches_not_run']} not run; spent ${budget.usage()['usd_committed']:.3f} (reserved rate)")
    print(f"Jobs: {len(jobs)} unique LinkedIn postings ({summary['new_jobs']} new), {summary['with_description']} with descriptions, "
          f"{len(excluded)} mismatches excluded")
    print(f"Reported age vs {args.lookback} (unverified): {summary['lookback_status']}")
    print("Files:")
    for name in ("summary.md", "results.csv", "results.json"):
        print(f"  {out / name}")
    print(f"  {args.db} (history)")
    return 1 if status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
