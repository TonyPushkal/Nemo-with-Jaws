#!/usr/bin/env python3
"""Offline check of a diagnostic run's raw SerpApi responses (no network, no spend).

    .venv/bin/python scripts/diagnostic_check.py runs/<run-id> --target-id 4473364096 --company safe

Per search: what was asked, how many results came back, whether the exact target LinkedIn job ID appears in any
returned link, and any other vacancies from the named company (reported separately). A miss is evidence about that
query only; it cannot show that a posting is absent from Google's or SerpApi's index.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

from nemo.linkedin import linkedin_job_id


def links_of(item: dict) -> list[str]:
    out = [o.get("link", "") for o in item.get("apply_options") or []]
    out += [item.get("link", ""), item.get("share_link", ""), item.get("source_link", "")]
    return [x for x in out if x]


def check(run_dir: Path, target_id: str, company: str) -> list[dict]:
    pat = re.compile(rf"\b{re.escape(company)}\b", re.I)
    rows = []
    for f in sorted(glob.glob(str(run_dir / "raw" / "*.json"))):
        raw = json.loads(Path(f).read_text())
        sp = raw.get("search_parameters") or {}
        items = raw.get("jobs_results") or raw.get("organic_results") or []
        row = {"file": Path(f).name, "engine": sp.get("engine"), "q": sp.get("q"),
               "location": sp.get("location_requested") or sp.get("location"), "freshness": sp.get("tbs") or "none",
               "returned": len(items), "error": raw.get("error"), "target": None, "similar": []}
        for it in items:
            ids = {linkedin_job_id(l) for l in links_of(it)} - {None}
            title = it.get("title") or it.get("job_title") or ""
            comp = it.get("company_name") or ""
            blob = " ".join([title, comp, " ".join(links_of(it))])
            rec = {"title": title, "company": comp, "location": it.get("location"), "via": it.get("via"),
                   "linkedin_ids": sorted(ids), "links": links_of(it)[:4],
                   "description_chars": len(it.get("description") or it.get("snippet") or "")}
            if target_id in ids or target_id in blob:
                row["target"] = rec
            elif pat.search(comp) or pat.search(re.sub(r"[-_/.]", " ", blob)):
                row["similar"].append(rec)
        rows.append(row)
    return rows


def render(rows: list[dict], target_id: str, company: str) -> str:
    L = [f"# Diagnostic check: target LinkedIn job {target_id}; similar vacancies from '{company}' listed separately", ""]
    for r in rows:
        L += [f"## {r['file']}: {r['engine']} · {r['q']}" + (f" · {r['location']}" if r["location"] else ""),
              f"- Freshness filter: {r['freshness']}; results returned: {r['returned']}" + (f"; provider message: {r['error']}" if r["error"] else ""),
              f"- Exact target ({target_id}): " + ("**FOUND**" if r["target"] else "not in these results")]
        if r["target"]:
            t = r["target"]
            L.append(f"  - {t['title']} · {t['company']} · {t['location']} · via {t['via']} · {t['description_chars']} chars · {t['links']}")
        L.append(f"- Other '{company}' vacancies: {len(r['similar'])}")
        L += [f"  - {s['title']} · {s['company']} · {s['location']} · via {s['via']} · LinkedIn IDs {s['linkedin_ids'] or 'none'}" for s in r["similar"]]
        L.append("")
    L.append("> Evidence, not proof: a query that does not return the target shows only that this query/day did not return it.")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--target-id", required=True)
    ap.add_argument("--company", required=True)
    args = ap.parse_args(argv)
    run = Path(args.run_dir)
    if not (run / "raw").is_dir():
        print(f"error: {run}/raw not found", file=sys.stderr)
        return 2
    text = render(check(run, args.target_id, args.company), args.target_id, args.company)
    (run / "diagnostic.md").write_text(text)
    print(text)
    print(f"saved {run / 'diagnostic.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
