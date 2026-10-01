#!/usr/bin/env python3
"""Exploratory shortlist from SAVED SerpApi Google Jobs data (no searches), matched locally.

    python scripts/exploratory_shortlist.py probe_out/serpapi/<timestamp> [--profile job-profile.md]

1. Takes only candidates whose Google Jobs `via` is LinkedIn and that have a LinkedIn /jobs/view link.
2. Consistency checks between the Google Jobs listing and the LinkedIn URL slug (the only LinkedIn-side
   data we have; linkedin.com is never fetched): title words, company words, and cities named in the
   description vs the Google location. Any contradiction -> quarantined, not matched.
3. Runs the rest through the local matcher (Ollama) with the job profile.
4. Writes shortlist.md / shortlist.json. Google's posting time is shown raw and labelled
   aggregator-reported; it is NOT verified and the production date gate is not applied or changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from nemo.linkedin import canonical_job_url, linkedin_job_id
from nemo.profile import ProfileError, load_profile
from nemo.providers.base import ProviderError
from nemo.providers.ollama import OllamaLLM
from nemo.tasks import assess_job

sys.path.insert(0, str(Path(__file__).parent))
from compare_serpapi import aggregator_time, request_time  # noqa: E402

from nemo.consistency import check, slug_parts  # noqa: E402,F401


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("--profile", default="job-profile.md")
    args = ap.parse_args(argv)
    run = Path(args.run)
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        print(f"invalid profile: {exc}", file=sys.stderr)
        return 2

    candidates, not_via_linkedin = [], []
    for f in sorted((run / "raw").glob("*.json")):
        raw = json.loads(f.read_text())
        if raw.get("search_parameters", {}).get("engine") != "google_jobs":
            continue
        for j in raw.get("jobs_results", []) or []:
            link = next((o["link"] for o in j.get("apply_options") or [] if linkedin_job_id(o.get("link", ""))), None)
            if not link:
                continue
            entry = {"job": j, "link": link, "job_id": linkedin_job_id(link), "request_time": request_time(raw),
                     "query": raw["search_parameters"].get("q"), "location_param": raw["search_parameters"].get("location")}
            (candidates if (j.get("via") or "").strip().casefold() == "linkedin" else not_via_linkedin).append(entry)

    llm = OllamaLLM()
    shortlisted, quarantined = [], []
    for c in candidates:
        j = c["job"]
        base = {"url": canonical_job_url(c["job_id"]), "linkedin_job_id": c["job_id"], "title": j.get("title"),
                "company": j.get("company_name"), "location": j.get("location"), "via": j.get("via"),
                "found_by": f"google_jobs q={c['query']!r} location={c['location_param']!r}",
                "aggregator_posted_at": aggregator_time(j, c["request_time"]),
                "description_chars": len(j.get("description") or "")}
        problems = check(j, c["link"])
        if problems:
            quarantined.append({**base, "problems": problems})
            print(f"QUARANTINED {c['job_id']}: {problems}")
            continue
        try:
            r = assess_job(llm, profile, j.get("description") or "")
        except ProviderError as exc:
            shortlisted.append({**base, "outcome": "assessment_failed", "error": str(exc)})
            continue
        shortlisted.append({**base, "outcome": r.outcome, "model_outcome": r.model_outcome, "note": r.note,
                            "explanation": r.explanation, "fit_quotes": r.fit_quotes, "mismatch_quotes": r.mismatch_quotes,
                            "criteria": [c_.__dict__ for c_ in r.criteria], "limitations": r.limitations,
                            "content_level": r.content_level, "job_truncated": r.job_truncated,
                            "latency_s": round(r.meta.get("total_ms", 0) / 1000, 1)})
        print(f"assessed {c['job_id']}: {r.outcome} ({r.meta.get('total_ms', 0) / 1000:.1f}s)", flush=True)

    order = {"strong_match": 0, "possible_match": 1, "insufficient_evidence": 2, "assessment_failed": 3, "not_relevant": 4}
    shortlisted.sort(key=lambda x: order.get(x["outcome"], 9))
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "source_run": str(run),
           "status": "EXPLORATORY — not verified last-24-hours results; dates are aggregator-reported and unverified; "
                     "production date gate not applied",
           "profile_sha256": profile.sha256(), "shortlisted": shortlisted, "quarantined": quarantined,
           "excluded_not_via_linkedin": [{"linkedin_job_id": e["job_id"], "title": e["job"].get("title"),
                                          "company": e["job"].get("company_name"), "via": e["job"].get("via")}
                                         for e in not_via_linkedin]}
    (run / "shortlist.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    (run / "shortlist.md").write_text(render(out))
    print(f"wrote {run}/shortlist.md and shortlist.json")
    return 0


def render(o: dict) -> str:
    L = ["# Exploratory shortlist (not verified)", "", f"> {o['status']}.", "",
         f"Source: `{o['source_run']}` (saved SerpApi Google Jobs responses; no new searches).", ""]
    for x in o["shortlisted"]:
        a = x["aggregator_posted_at"] or {}
        L += [f"## {x['title']} — {x['company']}", f"- Link: {x['url']}",
              f"- Location (Google): {x['location']} · via {x['via']}",
              f"- Google-reported posting time (aggregator, unverified): `{a.get('raw')}` "
              f"(field `{a.get('source')}`, request time {a.get('request_time')})",
              f"- Outcome: **{x['outcome']}**" + (f" — {x['note']}" if x.get("note") else ""),
              f"- Fit: {x.get('explanation', '')}"]
        for q in x.get("fit_quotes", []):
            L.append(f"  - quote: \"{q}\"")
        crit = x.get("criteria", [])
        met = [c for c in crit if c["status"] == "yes"]
        rest = [c for c in crit if c["status"] != "yes"]
        if met:
            L.append("- Met: " + "; ".join(f"{c['id']} {c['text']}" for c in met))
        if rest:
            L.append("- Unmet / unknown: " + "; ".join(f"{c['id']} {c['text']} → **{c['status']}**" for c in rest))
        if x.get("limitations"):
            L.append("- Limitations: " + "; ".join(x["limitations"]))
        L.append("")
    if o["quarantined"]:
        L += ["## Quarantined (contradictions between listing and LinkedIn URL)", ""]
        L += [f"- {x['url']} — {x['title']} / {x['company']}: " + "; ".join(x["problems"]) for x in o["quarantined"]]
        L.append("")
    if o["excluded_not_via_linkedin"]:
        L += ["## Not considered (Google listing not via LinkedIn)", ""]
        L += [f"- {x['linkedin_job_id']} — {x['title']} / {x['company']} (via {x['via']})" for x in o["excluded_not_via_linkedin"]]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
