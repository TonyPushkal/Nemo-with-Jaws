#!/usr/bin/env python3
"""Minimal probe: SerpApi Google Search vs Google Jobs for individual LinkedIn postings.

For each desired role in the job profile (one search per engine, interleaved so a cap stop keeps
the comparison balanced):
  google      q = site:linkedin.com/jobs/view "<role>" (Bengaluru OR Hyderabad OR Visakhapatnam), gl=in
  google_jobs q = <role> India, gl=in   (kept only when an apply link is a LinkedIn /jobs/view URL)

Only individual LinkedIn posting URLs are retained. Date information is kept apart by source:
  aggregator_posted_at    Google Jobs `detected_extensions.posted_at` (+ raw `extensions`) and the
                          SerpApi request time; Google's claim, NOT a verified source date
  search_engine_date      Google Search result `date` field; a provider date, never evidence
  returned_text_mentions  time phrases found in text SerpApi returned (snippet/description), raw
  verified_source_date    only an absolute date with explicit time zone in text attributed to the
                          posting; expected to be empty
Nothing here changes the production date gate.

Keys and limits (environment or .env): SERPAPI_API_KEY, NEMO_PAID_CALLS_ENABLED=true,
NEMO_MAX_USD_PER_RUN. --usd-per-search is your plan's worst-case price (default 0.025 = the
Starter plan's $25/1,000; the free plan's 250 searches/month cost $0, but the guard needs a
positive price). 8 roles x 2 engines = 16 searches = $0.40 at the default price.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from nemo.budget import Budget, BudgetConfig, BudgetConfigError, BudgetExceeded, PaidCallsDisabled
from nemo.envfile import load_env_file
from nemo.ledger import SqliteLedger
from nemo.linkedin import canonical_job_url, linkedin_job_id
from nemo.probe import NON_POSTING_QUALIFIERS, scan_time_mentions
from nemo.profile import ProfileError, load_profile
from nemo.providers.base import ProviderError
from nemo.providers.serpapi import SerpApi

CITIES = ("Bengaluru", "Hyderabad", "Visakhapatnam")


def plan(roles, *, jobs_only: bool = False, location: str | None = None) -> list[tuple[str, str, dict]]:
    """Default: both engines per role. With `location`, Google Jobs gets the explicit SerpApi
    `location` parameter and the query is the bare role (no "India")."""
    out = []
    for role in roles:
        if not jobs_only:
            out.append(("google", role, {"q": f'site:linkedin.com/jobs/view "{role}" ({" OR ".join(CITIES)})',
                                         "gl": "in", "hl": "en", "num": "10"}))
        jobs = {"q": role, "location": location} if location else {"q": f"{role} India"}
        out.append(("google_jobs", role, {**jobs, "gl": "in", "hl": "en"}))
    return out


def mentions(text: str, where: str) -> list[dict]:
    return [{"where": where, "kind": m["kind"], "raw": m["text"], "qualifier": m["qualifier"],
             "counts_as_posting_phrase": m["qualifier"] not in NON_POSTING_QUALIFIERS, "context": m["context"]}
            for m in scan_time_mentions(text or "")]


def verified(ms: list[dict]) -> list[dict]:
    """Absolute date-time with explicit offset only; bare dates and relative text do not qualify."""
    return [m for m in ms if m["kind"] == "structured_datePosted" and any(c in m["raw"] for c in "Z+")]


def request_time(raw: dict) -> str | None:
    md = raw.get("search_metadata") or {}
    return md.get("processed_at") or md.get("created_at")


def from_google(raw: dict, role: str) -> tuple[list[dict], int]:
    kept, dropped = [], 0
    for r in raw.get("organic_results", []) or []:
        jid = linkedin_job_id(r.get("link", ""))
        if not jid:
            dropped += 1
            continue
        text = r.get("snippet") or ""
        ms = mentions(r.get("snippet", ""), "google.snippet")
        kept.append({"engine": "google", "role": role, "job_id": jid, "url": canonical_job_url(jid),
                     "returned_url": r.get("link"), "title": r.get("title"), "company": None, "location": None,
                     "description_chars": len(text), "description_excerpt": (r.get("snippet") or "")[:300],
                     "aggregator_posted_at": None,
                     "search_engine_date": {"source": "google.organic_results.date", "raw": r["date"]} if r.get("date") else None,
                     "returned_text_mentions": ms, "verified_source_date": verified(ms),
                     "request_time": request_time(raw)})
    return kept, dropped


_AGO = re.compile(r"^\s*\d+\+?\s*(?:second|minute|hour|day|week|month|year)s?\s+ago\s*$", re.I)


def aggregator_time(job: dict, request_time_value: str | None) -> dict | None:
    """Google Jobs' posting-time claim. SerpApi returns it as `detected_extensions.posted_at` in its docs,
    but the 2026-09-30 responses had no `detected_extensions`; the phrase was only in `extensions`."""
    de = job.get("detected_extensions") or {}
    if de.get("posted_at"):
        raw, source = de["posted_at"], "google_jobs.detected_extensions.posted_at"
    else:
        raw = next((e for e in job.get("extensions") or [] if isinstance(e, str) and _AGO.match(e)), None)
        source = "google_jobs.extensions"
    if not raw:
        return None
    return {"source": source, "raw": raw, "raw_extensions": job.get("extensions"),
            "request_time": request_time_value, "status": "aggregator_reported_unverified"}


def from_jobs(raw: dict, role: str) -> tuple[list[dict], int]:
    kept, dropped = [], 0
    for j in raw.get("jobs_results", []) or []:
        links = [o.get("link", "") for o in j.get("apply_options", []) or []]
        ids = [linkedin_job_id(u) for u in links]
        jid = next((i for i in ids if i), None)
        if not jid:
            dropped += 1
            continue
        desc = j.get("description") or ""
        ms = mentions(desc, "google_jobs.description")
        kept.append({"engine": "google_jobs", "role": role, "job_id": jid, "url": canonical_job_url(jid),
                     "returned_url": links[ids.index(jid)], "title": j.get("title"), "company": j.get("company_name"),
                     "location": j.get("location"), "via": j.get("via"),
                     "description_chars": len(desc), "description_excerpt": desc[:300],
                     "aggregator_posted_at": aggregator_time(j, request_time(raw)),
                     "via_apply_title": next((o.get("title") for o in j.get("apply_options") or []
                                              if linkedin_job_id(o.get("link", "")) == jid), None),
                     "search_engine_date": None, "returned_text_mentions": ms, "verified_source_date": verified(ms),
                     "request_time": request_time(raw)})
    return kept, dropped


def summarize(items: list[dict], dropped: int, searches: int, failed: int) -> dict:
    uniq = {i["job_id"] for i in items}
    return {"searches": searches, "failed": failed, "non_linkedin_or_non_posting_dropped": dropped,
            "posting_urls": len(items), "unique_postings": len(uniq),
            "usable_description(>=500 chars)": sum(i["description_chars"] >= 500 for i in items),
            "aggregator_posted_at": sum(bool(i["aggregator_posted_at"]) for i in items),
            "search_engine_date": sum(bool(i["search_engine_date"]) for i in items),
            "returned_text_posting_phrases": sum(any(m["counts_as_posting_phrase"] for m in i["returned_text_mentions"]) for i in items),
            "verified_source_date": sum(bool(i["verified_source_date"]) for i in items)}


def reparse(run: Path) -> int:
    per: dict[str, dict] = {e: {"items": [], "dropped": 0, "searches": 0} for e in ("google", "google_jobs")}
    for f in sorted((run / "raw").glob("*.json")):
        raw = json.loads(f.read_text())
        engine = raw.get("search_parameters", {}).get("engine")
        role = raw.get("search_parameters", {}).get("q", "").removesuffix(" India")
        items, dropped = (from_google if engine == "google" else from_jobs)(raw, role)
        per[engine]["items"] += items
        per[engine]["dropped"] += dropped
        per[engine]["searches"] += 1
    old = json.loads((run / "comparison.json").read_text()) if (run / "comparison.json").exists() else {}
    report = {**old, "reparsed_offline_at": datetime.now(timezone.utc).isoformat(),
              "summary": {e: summarize(v["items"], v["dropped"], v["searches"], 0) for e, v in per.items()},
              "items": per["google"]["items"] + per["google_jobs"]["items"]}
    (run / "comparison.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report["summary"], indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="job-profile.md")
    ap.add_argument("--usd-per-search", type=float, default=0.025)
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--db", default="data/nemo.sqlite")
    ap.add_argument("--out-dir", default="probe_out/serpapi")
    ap.add_argument("--jobs-only", action="store_true", help="Google Jobs searches only")
    ap.add_argument("--location", help='explicit SerpApi location for Google Jobs, e.g. "Bengaluru,Karnataka,India"')
    ap.add_argument("--roles-limit", type=int, help="use only the first N desired roles")
    ap.add_argument("--max-usd", type=float, help="cap for this run (the lower of this and NEMO_MAX_USD_PER_RUN)")
    ap.add_argument("--reparse", help="rebuild comparison.json offline from <run>/raw/*.json (no API calls)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)

    if args.reparse:
        return reparse(Path(args.reparse))
    load_env_file(args.env_file)
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        print(f"invalid profile: {exc}", file=sys.stderr)
        return 2
    roles = profile.desired_roles[: args.roles_limit] if args.roles_limit else profile.desired_roles
    steps = plan(roles, jobs_only=args.jobs_only, location=args.location)
    print(f"{len(steps)} searches ({len(roles)} roles, {'Google Jobs only' if args.jobs_only else '2 engines'}), worst case "
          f"${len(steps) * args.usd_per_search:.3f} at ${args.usd_per_search}/search:")
    for engine, _, params in steps:
        print(f"  {engine:12} {params['q']}" + (f"  [location={params['location']}]" if params.get("location") else ""))
    if args.dry_run:
        return 0
    key = os.environ.get("SERPAPI_API_KEY")
    if not key:
        print("SERPAPI_API_KEY is not set (environment or .env).", file=sys.stderr)
        return 2
    try:
        cfg = BudgetConfig.from_env(os.environ)
    except BudgetConfigError as exc:
        print(f"budget configuration error: {exc}", file=sys.stderr)
        return 2
    if not cfg.paid_calls_enabled:
        print("Paid calls are disabled (NEMO_PAID_CALLS_ENABLED).", file=sys.stderr)
        return 2
    cap = min(cfg.max_usd_per_run, Decimal(str(args.max_usd))) if args.max_usd is not None else cfg.max_usd_per_run
    print(f"Run cap ${cap} allows {int(float(cap) // args.usd_per_search)} searches.")
    if not args.yes and (not sys.stdin.isatty() or input("Proceed? [y/N] ").strip().lower() != "y"):
        print("Not confirmed; nothing sent.", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out_dir) / stamp
    (out / "raw").mkdir(parents=True, exist_ok=True)
    budget = Budget(BudgetConfig(paid_calls_enabled=True, max_usd_per_run=cap,
                                 monthly_usd_cap=cfg.monthly_usd_cap, max_queries=len(steps)),
                    run_id=f"serpapi-{stamp}", ledger=SqliteLedger(args.db))
    provider = SerpApi(key, usd_per_search=args.usd_per_search)
    per = {"google": {"items": [], "dropped": 0, "searches": 0, "failed": 0},
           "google_jobs": {"items": [], "dropped": 0, "searches": 0, "failed": 0}}
    log, stopped = [], None
    for engine, role, params in steps:
        try:
            with budget.call("queries", provider.name, is_paid=True, max_cost_usd=provider.max_cost_usd):
                raw = provider.search_raw(engine, params)
        except BudgetExceeded as exc:
            stopped = exc.cap
            break
        except ProviderError as exc:
            per[engine]["failed"] += 1
            log.append({"engine": engine, "q": params["q"], "error": str(exc)})
            continue
        except PaidCallsDisabled as exc:
            print(exc, file=sys.stderr)
            return 2
        per[engine]["searches"] += 1
        # full redacted response, including any "no results" error text, for later diagnosis
        (out / "raw" / f"{len(log) + 1:02d}_{engine}.json").write_text(json.dumps(raw, indent=1, ensure_ascii=False))
        items, dropped = (from_google if engine == "google" else from_jobs)(raw, role)
        per[engine]["items"] += items
        per[engine]["dropped"] += dropped
        log.append({"engine": engine, "q": params["q"], "kept": len(items), "dropped": dropped,
                    "error_text": raw.get("error"), "result_keys": sorted(raw),
                    "search_metadata": raw.get("search_metadata")})
        print(f"  {engine:12} {role:30} kept {len(items)}, dropped {dropped}", flush=True)

    report = {"note": "Aggregator-reported posting times are Google's claim, reported separately from verified "
                      "source dates. Nothing here is used by the production date gate.",
              "stopped_by_cap": stopped, "usage": budget.usage(), "log": log,
              "summary": {e: summarize(v["items"], v["dropped"], v["searches"], v["failed"]) for e, v in per.items()},
              "items": per["google"]["items"] + per["google_jobs"]["items"]}
    both = {i["job_id"] for i in per["google"]["items"]} & {i["job_id"] for i in per["google_jobs"]["items"]}
    report["summary"]["found_by_both"] = sorted(both)
    (out / "comparison.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report["summary"], indent=1))
    print(f"stopped_by_cap={stopped}; usage {report['usage']}; wrote {out}/comparison.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
