#!/usr/bin/env python3
"""Retrieve archived SerpApi searches from a comparison run, checking they consume no searches.

Reads the account usage (free Account API), retrieves ONE archive, reads usage again, and only
continues if `this_month_usage` and `total_searches_left` are unchanged. Saves redacted JSON.

    python scripts/serpapi_archive.py probe_out/serpapi/<timestamp>
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from nemo.envfile import load_env_file
from nemo.providers.base import ProviderError
from nemo.providers.serpapi import SerpApi

USAGE_KEYS = ("this_month_usage", "total_searches_left", "plan_searches_left", "last_hour_searches")


def usage(api: SerpApi) -> dict:
    a = api.account()
    return {k: a.get(k) for k in USAGE_KEYS}


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    run = Path(argv[0])
    load_env_file(".env")
    key = os.environ.get("SERPAPI_API_KEY")
    if not key:
        print("SERPAPI_API_KEY is not set.", file=sys.stderr)
        return 2
    api = SerpApi(key, usd_per_search=0.025)  # price unused: archive/account calls do not go through the guard
    log = json.loads((run / "comparison.json").read_text())["log"]
    ids = [(l["engine"], l["search_metadata"]["id"]) for l in log if l.get("search_metadata")]
    ids.sort(key=lambda x: x[0] != "google_jobs")  # google_jobs first
    out = run / "archive"
    out.mkdir(exist_ok=True)
    checks = []
    try:
        before = usage(api)
        for n, (engine, sid) in enumerate(ids):
            raw = api.archived(sid)
            (out / f"{engine}_{sid}.json").write_text(json.dumps(raw, indent=1, ensure_ascii=False))
            print(f"retrieved {engine} {sid}: keys {sorted(raw)}; error={raw.get('error')!r}")
            if n == 0:
                time.sleep(2)
                after = usage(api)
                checks.append({"before": before, "after_first_archive": after})
                print(f"usage before {before} / after first archive {after}")
                if (after["this_month_usage"], after["total_searches_left"]) != (before["this_month_usage"], before["total_searches_left"]):
                    print("Archive retrieval changed usage; stopping.", file=sys.stderr)
                    (out / "usage_check.json").write_text(json.dumps(checks, indent=1))
                    return 1
        checks.append({"after_all": usage(api)})
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    (out / "usage_check.json").write_text(json.dumps(checks, indent=1))
    print(f"usage after all {checks[-1]['after_all']}; saved to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
