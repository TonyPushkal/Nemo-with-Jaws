"""Phase 1 discovery run (SerpApi Google Jobs): target titles + cities + profile + max posting age + request budget -> jobs.

    .venv/bin/python scripts/run_search.py --profile job-profile.md --lookback 7d --gl in   # cities: config default_locations

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
import re
import sys
import uuid
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from nemo.budget import Budget, BudgetConfig, BudgetConfigError, BudgetExceeded, PaidCallsDisabled
from nemo.discovery import Step, freshness_bucket, merge, parse_google, parse_google_jobs, parse_lookback, plan
from nemo.envfile import load_env_file
from nemo.ledger import SqliteLedger
from nemo.profile import ProfileError, load_profile
from nemo.queryplan import build_plan, load_config, load_queries_file
from nemo.runreport import fresh_split, job_record, render, screen_run
from nemo.providers.base import ProviderError
from nemo.providers.serpapi import SerpApi
from nemo.store import Store

TRANSIENT = {"timeout", "rate_limited", "server_error", "connection_error"}
CSV_FIELDS = ["job_key", "linkedin_job_id", "url", "application_url", "application_kind", "description_source", "title", "company", "location", "reported_age", "reported_age_source",
              "reported_age_basis", "lookback_status", "description_level", "description_chars", "engines", "vias",
              "first_seen_at", "last_seen_at", "previously_seen", "assessment_outcome", "flags"]


def write_status(out: Path, data: dict) -> None:
    """Atomic status file a caller (or a service) can poll while the run is in progress."""
    tmp = out / "status.json.tmp"
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str))
    tmp.replace(out / "status.json")


def city_name_of(location: str) -> str:
    return location.split(',')[0].strip()


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
        print(f"  assessed {j['job_key']}: {j['assessment']['outcome']}", flush=True)
    return stats


def csv_row(j: dict) -> dict:
    b = j.get("best_reported_age") or {}
    return {"job_key": j["job_key"], "linkedin_job_id": j["linkedin_job_id"], "url": j["url"],
            "application_url": j["application_url"], "application_kind": j["application_kind"],
            "description_source": j["description_source"], "title": j["title"], "company": j["company"],
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
         f"- Jobs: **{s['jobs']}** unique jobs ({s['new_jobs']} new, {s['previously_seen']} seen before); "
         f"{s['excluded_mismatches']} LinkedIn links rejected as mismatched (jobs kept)",
         f"- Reported posting age vs lookback (unverified, Google-reported): {s['lookback_status']}",
         "", "> Dates are best-effort reports from Google/Google Jobs, not verified posting times. Nothing is filtered "
             "by date, fit or seniority. Discovery is best-effort, not exhaustive coverage.", "",
         "| # | Title | Company | Location | Reported age (source) | Lookback | Description | Link |", "|---|---|---|---|---|---|---|---|"]
    for n, j in enumerate(jobs, 1):
        b = j.get("best_reported_age") or {}
        age = f"{b.get('raw')} ({b.get('basis')}, via {b.get('via')})" if b else "unknown"
        L.append(f"| {n} | {j['title'] or '?'} | {j['company'] or '?'} | {j['location'] or '?'} | {age} | "
                 f"{j['lookback_status']} | {j['description_level']} ({len(j['description'])}) | {j['url']} |")
    if excluded:
        L += ["", "## LinkedIn links rejected as mismatched (the jobs are kept without that link)", ""]
        L += [f"- {e['linkedin_job_id']} — {e['title']} / {e['company']} (via {e['via']}): {'; '.join(e['problems'])}" for e in excluded]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="job-profile.md")
    ap.add_argument("--lookback", default="24h", help="maximum posting age, e.g. 24h (default), 7d, 2w (1h..30d)")
    ap.add_argument("--titles", help="comma-separated target titles; overrides the profile's desired roles for planning searches")
    ap.add_argument("--run-id", help="use this run id (a service passes one so it can report status before the run ends)")
    ap.add_argument("--no-follow-pages", action="store_true",
                    help="do not spend leftover request budget on later result pages")
    ap.add_argument("--location", action="append", default=[], help="SerpApi location for Google Jobs (repeatable)")
    ap.add_argument("--gl", help="Google country code, e.g. in")
    ap.add_argument("--search-config", help="role families and search terms (default config/search_terms.json)")
    ap.add_argument("--offset", type=int, default=0,
                    help="skip the first N planned searches; with --max-searches M, run plan items N+1..N+M (next batch)")
    ap.add_argument("--screen", action="store_true",
                    help="after collecting, screen this run's jobs (rules only) and write report.md + screened.json")
    ap.add_argument("--queries-file", help="explicit searches (JSON) instead of the planned ones, e.g. a diagnostic; "
                                           "web searches carry no freshness filter unless the item sets tbs")
    ap.add_argument("--plain-roles", action="store_true",
                    help="skip the search config: one search per desired role, as written in the profile")
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
        roles = [t.strip() for t in (args.titles or "").split(",") if t.strip()] or list(profile.desired_roles)
    except (ProfileError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.location and not (args.plain_roles or args.queries_file):
        try:
            args.location = list(load_config(args.search_config).get("default_locations") or [])
        except (OSError, ValueError):
            pass                                  # reported just below when the plan is built
    try:
        steps = (load_queries_file(args.queries_file, args.gl) if args.queries_file else
                 plan(roles, args.location, lookback, args.gl) if args.plain_roles else
                 build_plan(roles, args.location, lookback, args.gl, load_config(args.search_config)))
    except (OSError, ValueError) as exc:
        print(f"error: search config: {exc}", file=sys.stderr)
        return 2
    if args.queries_file and not args.location:               # the file carries its own locations: show them
        args.location = list(dict.fromkeys(st.params["location"] for st in steps if st.params.get("location")))
    try:
        cfg = BudgetConfig.from_env(os.environ)
    except BudgetConfigError as exc:
        print(f"budget configuration error: {exc}", file=sys.stderr)
        return 2
    affordable = int(cfg.max_usd_per_run // Decimal(str(args.usd_per_search))) if cfg.max_usd_per_run else 0   # exact, like the guard
    print(f"Profile {args.profile}: {len(roles)} roles; max posting age {args.lookback}; "
          f"{len(args.location)} location(s)")
    print(f"Plan: {len(steps)} searches (cap: {args.max_searches if args.max_searches is not None else 'none'}); dollar guard uses an ASSUMED "
          f"${args.usd_per_search}/search (not verified billing; worst case ${len(steps) * args.usd_per_search:.3f}). "
          f"Budget: paid calls {'ENABLED' if cfg.paid_calls_enabled else 'DISABLED'}, per-run ceiling "
          f"${cfg.max_usd_per_run} → up to {min(affordable, len(steps))} searches this run.")
    if not args.location:
        print("  (no --location: Google Jobs is skipped; it returned nothing without one in testing)")
    start = max(args.offset, 0)
    n_run = max(len(steps) - start, 0)
    if args.max_searches is not None:
        n_run = min(n_run, args.max_searches)
    if cfg.paid_calls_enabled:
        n_run = min(n_run, affordable)
    if args.dry_run:
        print(f"Locations: {', '.join(city_name_of(l) for l in args.location) or '(none)'}; planned requests in full plan: {len(steps)}")
        print(f"Request-count cap: {args.max_searches if args.max_searches is not None else 'none'} (--max-searches); offset {start}; "
              f"this batch runs plan items {start + 1}-{start + n_run}; fit within the configured cap: {n_run}"
              + ("" if cfg.paid_calls_enabled else " (paid calls disabled: none would run)"))
        print(f"Estimated cost of this batch (ASSUMED ${args.usd_per_search}/search, not verified billing): "
              f"${n_run * args.usd_per_search:.3f}; the dollar guard uses the same estimate.")
        for i, st in enumerate(steps, 1):
            where = st.params.get("location", "").split(",")[0] or "-"
            tag = "RUN" if start < i <= start + n_run else "before offset" if i <= start else "beyond cap"
            fresh = f"  [freshness: {st.params.get('tbs') or 'none'}]" if st.engine == "google" else ""
            nextp = f"  [page {st.page}: token from page {st.page - 1}; skipped, not replaced, if absent]" if st.page > 1 else ""
            print(f"  {i:>2}. [{tag}] {st.role:16} {st.engine:11} {st.params['q']}  @ {where}{fresh}{nextp}")
        if start + n_run < len(steps):
            print(f"Next batch: --offset {start + n_run}")
        return 0
    planned_total = len(steps)
    steps = steps[start: start + args.max_searches] if args.max_searches is not None else steps[start:]
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
    run_id = args.run_id or f"{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    out = Path(args.out_dir) / run_id
    (out / "raw").mkdir(parents=True, exist_ok=True)
    follow = not args.no_follow_pages
    request_budget = args.max_searches if args.max_searches is not None else len(steps)
    write_status(out, {"state": "running", "run_id": run_id, "started_at": started.isoformat(), "request_budget": request_budget,
                       "max_age": args.lookback, "cities": [city_name_of(l) for l in args.location]})
    api = SerpApi(key, usd_per_search=args.usd_per_search)
    budget = Budget(BudgetConfig(paid_calls_enabled=True, max_usd_per_run=cfg.max_usd_per_run,
                                 monthly_usd_cap=cfg.monthly_usd_cap,
                                 max_queries=request_budget if follow else len(steps)),    # retries and later pages count against it
                    run_id=f"search-{run_id}", ledger=SqliteLedger(args.db))
    store = Store(args.db)
    window_end = started
    window = (window_end - lookback, window_end)
    store.start_run(run_id, started.isoformat(), profile_sha256=profile.sha256(), lookback=args.lookback,
                    window_start=window[0].isoformat(), window_end=window[1].isoformat(),
                    params={"locations": args.location, "gl": args.gl, "usd_per_search": args.usd_per_search,
                            "offset": start, "request_cap": args.max_searches, "plan_total": planned_total})

    sightings, excluded, ok, failed, stopped, issues = [], [], 0, 0, None, []
    query_log, skipped_total, no_li_total, planned_done = [], 0, 0, 0
    tokens: dict[tuple, tuple[str, str]] = {}      # (q, location) -> (next_page_token, google_domain) from the latest page
    seen_keys: dict[tuple, set] = {}               # per query: job keys already seen on earlier pages
    ended: dict[tuple, str] = {}                   # per query: why its pages stopped (end_of_results | repeated_page | error)
    queue, next_pass, seq = deque(steps), [], start
    try:
        while queue:
            step = queue.popleft()
            seq += 1
            qid = (step.params["q"], step.params.get("location"))
            q = {"engine": step.engine, "role": step.role, "q": step.params["q"], "location": step.params.get("location")}
            row = {"seq": seq, "q": step.params["q"], "page": step.page, "city": (q["location"] or "-").split(",")[0],
                   "returned": 0, "kept": 0, "no_linkedin": 0, "rejected": 0, "skipped": 0}
            planned_done += step.planned
            params = step.params
            if step.page > 1:
                nxt = tokens.pop(qid, None)
                if not nxt:      # not replaced by any unplanned search; nothing is sent or spent
                    msg = f"page {step.page} not available (no next_page_token on the previous page)"
                    issues.append({"seq": seq, **q, "error": msg})
                    query_log.append({**row, "status": "unavailable"})
                    store.add_query(run_id, seq, **q, status="unavailable", kept=0, error=msg)
                    ended.setdefault(qid, "end_of_results")
                    print(f"  {seq:>2}. {step.engine:11} {step.role:30} NOT RUN: {msg}", flush=True)
                    continue
                params = {**step.params, "next_page_token": nxt[0], "google_domain": nxt[1]}   # keep q/location/gl/hl as the API requires
            try:
                raw = search_with_retry(api, budget, step.engine, params)
            except BudgetExceeded as exc:
                stopped = exc.cap
                queue.appendleft(step)
                planned_done -= step.planned
                break
            except PaidCallsDisabled as exc:
                print(exc, file=sys.stderr)
                return 2
            except ProviderError as exc:
                failed += 1
                issues.append({"seq": seq, **q, "error": str(exc)})
                query_log.append({**row, "status": "failed"})
                store.add_query(run_id, seq, **q, status="failed", kept=0, error=str(exc))
                ended[qid] = "error"
                print(f"  {seq:>2}. {step.engine:11} {step.role:30} FAILED: {exc}", flush=True)
                if not queue and next_pass:
                    queue, next_pass = deque(next_pass), []
                continue
            ok += 1
            (out / "raw" / f"{seq:02d}_{step.engine}{f'_p{step.page}' if step.page > 1 else ''}.json").write_text(
                json.dumps(raw, indent=1, ensure_ascii=False))
            tok = (raw.get("serpapi_pagination") or {}).get("next_page_token")
            if step.engine == "google_jobs" and tok:
                tokens[qid] = (tok, (raw.get("search_parameters") or {}).get("google_domain") or "google.com")
            no_li = skipped = 0
            if step.engine == "google_jobs":
                kept, bad, no_li, skipped = parse_google_jobs(raw, window, step.page)
                excluded += bad
            else:
                kept, _ = parse_google(raw, window)
                bad = []
            skipped_total += skipped
            no_li_total += no_li
            query_log.append({**row, "status": "empty" if raw.get("error") else "ok",
                              "returned": len(raw.get("jobs_results") or raw.get("organic_results") or []), "kept": len(kept),
                              "no_linkedin": no_li, "rejected": len(bad), "skipped": skipped})
            sightings += kept
            store.add_query(run_id, seq, **q, status="empty" if raw.get("error") else "ok", kept=len(kept),
                            search_id=(raw.get("search_metadata") or {}).get("id"))
            print(f"  {seq:>2}. {step.engine:11} {step.role:16} {step.params['q']:30} kept {len(kept)}"
                  + (f", skipped {skipped} (no identity/application link)" if skipped else ""), flush=True)
            if step.engine == "google_jobs":             # pagination bookkeeping: why does each query stop?
                keys = {k.job_id for k in kept}
                if step.page > 1 and keys <= seen_keys.get(qid, set()):
                    ended[qid] = "repeated_page"
                elif not tok:
                    ended[qid] = "end_of_results"
                seen_keys.setdefault(qid, set()).update(keys)
                explicit_next = any(s.params.get("q") == qid[0] and s.params.get("location") == qid[1] and s.page == step.page + 1 for s in queue)
                if tok and follow and qid not in ended and not explicit_next:
                    base = re.sub(r" \(page \d+\)$", "", step.role)
                    next_pass.append(Step(step.engine, f"{base} (page {step.page + 1})", step.params, step.page + 1, planned=False))
            if not queue and next_pass:
                queue, next_pass = deque(next_pass), []
    except BaseException as exc:
        write_status(out, {"state": "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", "run_id": run_id,
                           "error": f"{type(exc).__name__}: {exc}", "requests_used": budget.usage()["queries"],
                           "request_budget": request_budget, "finished_at": now().isoformat()})
        raise
    unrun_planned = sum(1 for s in queue if s.planned)
    reasons = set(ended.values())
    if stopped:
        stop_reason = "budget_exhausted" if stopped == "max_queries" else f"spend_guard:{stopped}"
        if unrun_planned:
            stop_reason += f" ({unrun_planned} planned search(es) not run)"
    elif not follow and any(ended.get(k) is None for k in tokens):
        stop_reason = "planned_requests_completed (later pages not followed)"
    elif "repeated_page" in reasons:
        stop_reason = "repeated_pages"
    elif reasons and reasons <= {"end_of_results"}:
        stop_reason = "end_of_results"
    elif "error" in reasons:
        stop_reason = "page_error"
    else:
        stop_reason = "planned_requests_completed"
    query_stops = {f"{k[0]} @ {(k[1] or '-').split(',')[0]}": v for k, v in ended.items()}
    partial = bool(stopped and (stopped != "max_queries" or unrun_planned))

    jobs = merge(sightings)
    seen_at = now().isoformat()
    store.upsert_jobs(run_id, seen_at, jobs)
    assess_stats = assess_all(jobs, profile, args.assess_max) if args.assess and jobs else None
    if assess_stats:
        store.upsert_jobs(run_id, seen_at, jobs)  # persist enrichment

    order = {"reported_within": 0, "ambiguous": 1, "unknown": 2, "reported_outside": 3}
    ranked = sorted(jobs.values(), key=lambda j: (order[j["lookback_status"]], j["description_level"] != "description"))
    status = "failed" if ok == 0 else "partial" if (partial or failed) else "complete"
    fresh_counts = {b: sum(freshness_bucket(j) == b for j in jobs.values()) for b in ("confirmed_within", "unconfirmed", "older")}
    summary = {"searches_planned": len(steps), "searches_ok": ok, "searches_failed": failed,
               "searches_not_run": len(steps) - planned_done, "jobs": len(jobs),
               "stop_reason": stop_reason, "query_stops": query_stops, "requests_used": budget.usage()["queries"],
               "request_budget": request_budget, "pages_followed": sum(1 for r in query_log if r["page"] > 1 and r["status"] != "unavailable"),
               "freshness": fresh_counts, "max_age": args.lookback,
               "new_jobs": sum(not j["previously_seen"] for j in jobs.values()),
               "previously_seen": sum(j["previously_seen"] for j in jobs.values()),
               "excluded_mismatches": len(excluded), "skipped_no_identity": skipped_total,
               "kept_without_linkedin_link": no_li_total, "offset": start, "request_cap": args.max_searches,
               "plan_total": planned_total,
               "with_description": sum(j["description_level"] == "description" for j in jobs.values()),
               "lookback_status": {k: sum(j["lookback_status"] == k for j in jobs.values()) for k in order},
               "assessment": assess_stats, "usage": budget.usage()}
    meta = {"run_id": run_id, "status": status, "stopped_by_cap": stopped if partial else None, "stop_reason": stop_reason, "lookback": args.lookback,
            "window_start": window[0].isoformat(), "window_end": window[1].isoformat(), "profile": args.profile,
            "summary": summary, "issues": issues,
            "notes": ["best-effort discovery, not exhaustive coverage",
                      "reported ages are Google/Google Jobs claims, not verified posting times",
                      "LinkedIn links are consistency-checked against the listing, not verified; a clearly mismatched link is "
                      "rejected and the job is kept without it",
                      "no filtering by date, fit or seniority at collection time (screening is a separate step)"]}
    (out / "results.json").write_text(json.dumps({**meta, "jobs": ranked, "excluded_mismatches": excluded},
                                                 indent=2, ensure_ascii=False))
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(csv_row(j) for j in ranked)
    (out / "summary.md").write_text(render_summary(meta, ranked, excluded))
    store.finish_run(run_id, now().isoformat(), status, summary)
    if args.screen:
        cities = [city_name_of(l) for l in args.location]
        ok_s, out_s = screen_run(jobs, profile, cities)
        rep = {"run_id": run_id, "status": status, "batch": (start + 1, start + len(steps)), "planned": planned_total,
               "cap": args.max_searches if args.max_searches is not None else "none", "searches_ok": ok, "searches_failed": failed,
               "new_jobs": summary["new_jobs"], "skipped_no_identity": skipped_total, "rejected_links": len(excluded),
               "stop_reason": stop_reason, "requests_used": budget.usage()["queries"], "request_budget": request_budget}
        (out / "report.md").write_text(render(rep, query_log, ok_s, out_s, len(jobs), args.lookback))
        fresh = fresh_split(ok_s)
        rec = lambda xs: [job_record(x) for x in xs]
        (out / "screened.json").write_text(json.dumps({
            "max_age": args.lookback, "cities": cities, "stop_reason": stop_reason, "query_stops": query_stops,
            "fresh_shortlist": rec(fresh["confirmed_within"]), "age_unconfirmed": rec(fresh["unconfirmed"]),
            "older_than_max_age": rec(fresh["older"]), "shortlist": rec(ok_s), "excluded": rec(out_s), "queries": query_log},
            indent=1, ensure_ascii=False, default=str))
    write_status(out, {"state": status, "run_id": run_id, "stop_reason": stop_reason, "requests_used": budget.usage()["queries"],
                       "request_budget": request_budget, "max_age": args.lookback, "freshness": fresh_counts, "jobs": len(jobs),
                       "finished_at": now().isoformat(), "screened": bool(args.screen)})
    store.close()

    print(f"\nStatus: {status}" + (f" (stopped by {stopped})" if partial else ""))
    print(f"Collection stopped because: {stop_reason}; {budget.usage()['queries']} of {request_budget} requests used")
    print(f"Searches: {ok} ok, {failed} failed, {summary['searches_not_run']} not run; spent ${budget.usage()['usd_committed']:.3f} (reserved rate)")
    print(f"Jobs: {len(jobs)} unique jobs ({summary['new_jobs']} new), {summary['with_description']} with descriptions, "
          f"{len(excluded)} LinkedIn links rejected, "
          f"{skipped_total} skipped (no identity/application link), {no_li_total} kept without a LinkedIn link")
    print(f"Reported age vs {args.lookback} (unverified): {summary['lookback_status']}")
    print("Files:")
    for name in ("summary.md", "results.csv", "results.json") + (("report.md", "screened.json") if args.screen else ()):
        print(f"  {out / name}")
    print(f"  {args.db} (history)")
    return 1 if status == "failed" else 0

