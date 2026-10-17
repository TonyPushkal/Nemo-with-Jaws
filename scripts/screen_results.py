#!/usr/bin/env python3
"""Screen and rank jobs from SAVED search results (rules only; no network, no model, originals untouched).

    .venv/bin/python scripts/screen_results.py [--profile job-profile.md] [--runs runs] [--out runs]

Reads runs/*/results.json (and runs/*/raw/*.json for each job's application links), merges the same LinkedIn job id
across runs (keeping every source and its description provenance), applies nemo.screening, and writes a NEW folder
runs/screening-<UTC>/ with ranked.md and ranked.json. Excluded jobs are kept with their reasons.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from nemo.discovery import choose_application_link, job_key
from nemo.linkedin import linkedin_job_id
from nemo.profile import ProfileError, load_profile
from nemo.queryplan import load_config
from nemo.runreport import job_record, screening_sections
from nemo.screening import FULL_TIME_MONTHS, MAX_MIN_YEARS, TOTAL_MONTHS, rank, screen


def load_saved(runs: Path) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Merge jobs across runs by LinkedIn job id; collect application links from raw SerpApi responses."""
    jobs: dict[str, dict] = {}
    for f in sorted(glob.glob(str(runs / "*" / "results.json"))):
        data = json.loads(Path(f).read_text())
        for j in data.get("jobs", []):
            key = j.get("job_key") or j["linkedin_job_id"]          # older results.json files are LinkedIn-id only
            if key.startswith("url:") and j.get("application_url") and key == job_key(None, {"url": j["application_url"]}, None, legacy=True):
                key = job_key(None, {"url": j["application_url"]}, None)   # read-time alias to the current identity; files untouched
            cur = jobs.get(key)
            if cur is None:
                jobs[key] = {**j, "job_key": key, "seen_in_runs": [data["run_id"]], "sources": list(j["sources"])}
                continue
            cur["seen_in_runs"].append(data["run_id"])
            for s in j["sources"]:
                if s not in cur["sources"]:
                    cur["sources"].append(s)
            if len(j["description"]) > len(cur["description"]):
                cur["description"] = j["description"]
    links: dict[str, list[dict]] = {}
    for f in glob.glob(str(runs / "*" / "raw" / "*.json")):
        raw = json.loads(Path(f).read_text())
        for j in raw.get("jobs_results") or []:
            opts = [{"title": o.get("title"), "link": o.get("link")} for o in j.get("apply_options") or [] if o.get("link")]
            for o in opts:
                jid = linkedin_job_id(o["link"])
                if jid:
                    seen = {x["link"] for x in links.setdefault(jid, [])}
                    links[jid] += [x for x in opts if x["link"] not in seen]
    return jobs, links


def application_link(job: dict, links: list[dict]) -> str:
    """Newer results carry the chosen link; for older ones: the source's own apply link (matching `via`), else LinkedIn."""
    if job.get("application_url"):
        return job["application_url"]
    vias = {(s.get("via") or "").lower() for s in job["sources"]}
    for o in links:
        if (o["title"] or "").lower() in vias and "linkedin.com" not in o["link"]:
            return o["link"]
    return job["url"]


def provenance(job: dict) -> str:
    return ", ".join(sorted({f"{s.get('via')} via {s['engine']}" for s in job["sources"]}))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="job-profile.md")
    ap.add_argument("--runs", default="runs")
    ap.add_argument("--out", default="runs")
    args = ap.parse_args(argv)
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    jobs, links = load_saved(Path(args.runs))
    if not jobs:
        print("no saved results found", file=sys.stderr)
        return 1
    for j in jobs.values():       # relabel application links with the current rules (employer/ATS, job board, unknown)
        if j.get("application_options"):
            app = choose_application_link(j.get("company") or "", j.get("description_source"), j["application_options"])
            if app:
                j["application_url"], j["application_kind"] = app["url"], app["kind"]
        elif not j.get("application_url"):
            j["application_url"] = application_link(j, links.get(j["job_key"], []))
            j["application_kind"] = "linkedin" if "linkedin.com" in (j["application_url"] or "") else "unknown"
        j.setdefault("linkedin_url", j["url"] if j.get("linkedin_job_id") else None)
    cities = [c.split(",")[0] for c in load_config().get("default_locations", [])]       # chosen cities: screen the actual location
    ok, out = rank([screen(j, profile, chosen_cities=cities) for j in jobs.values()])
    out_dir = Path(args.out) / f"screening-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir.mkdir(parents=True)

    def rec(s):
        j = s.job
        return {**job_record(s), "linkedin_job_id": j.get("linkedin_job_id"), "description_provenance": provenance(j),
                "seen_in_runs": j["seen_in_runs"], "all_application_links": j.get("application_options") or links.get(j["job_key"], [])}
    (out_dir / "ranked.json").write_text(json.dumps({"profile": args.profile, "candidate_months": {"total": TOTAL_MONTHS,
        "full_time": FULL_TIME_MONTHS}, "max_min_years": MAX_MIN_YEARS, "shortlist": [rec(s) for s in ok],
        "excluded": [rec(s) for s in out]}, indent=1, ensure_ascii=False))
    L = [f"# Ranked shortlist ({len(ok)} eligible, {len(out)} excluded, {len(jobs)} unique jobs)", "",
         "Rules only. Descriptions, dates and application-link kinds are best-effort; LinkedIn links are consistency-checked, not verified.", ""]
    (out_dir / "ranked.md").write_text("\n".join(L + screening_sections(ok, out)) + "\n")
    print(f"{len(jobs)} unique jobs: {len(ok)} eligible, {len(out)} excluded\n{out_dir}/ranked.md\n{out_dir}/ranked.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
