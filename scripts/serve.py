#!/usr/bin/env python3
"""Run the minimal collection service (stdlib only). Localhost by default.

    .venv/bin/python scripts/serve.py [--port 8765] [--profile job-profile.md]

    curl -s -X POST localhost:8765/runs -d '{"cities": ["Bengaluru", "Hyderabad"], "max_age_hours": 24, "request_budget": 12}'
    curl -s localhost:8765/runs/<run_id>

Paid calls stay governed by .env (NEMO_PAID_CALLS_ENABLED, NEMO_MAX_USD_PER_RUN, optional NEMO_MONTHLY_USD_CAP).
"""

import argparse

from nemo.service import serve

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8765)
ap.add_argument("--profile", default="job-profile.md")
ap.add_argument("--runs-dir", default="runs")
ap.add_argument("--db", default="data/nemo.sqlite")
ap.add_argument("--env-file", default=".env")
args = ap.parse_args()
server = serve(args.host, args.port, profile=args.profile, runs_dir=args.runs_dir, db=args.db, env_file=args.env_file)
print(f"nemo collector listening on http://{args.host}:{args.port} (POST /runs, GET /runs/<id>)", flush=True)
try:
    server.serve_forever()
except KeyboardInterrupt:
    print("stopping")
