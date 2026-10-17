"""Minimal HTTP service over the discovery flow (Python standard library only; one process; localhost by default).

    POST /runs        {"titles": [...]?, "cities": [...]?, "max_age_hours": 24?, "request_budget": 12?}
                      -> 202 {"run_id", "state": "running", "status_url"}   (409 if a run is already in progress)
    GET  /runs/<id>   -> status, why collection stopped, request use, freshness counts and, when finished, the screened jobs

A request runs the same flow as `scripts/run_search.py --screen`: SerpApi Google Jobs searches under the existing spend guards, pages
followed until the request budget / end of results / repeated pages, screening against the server-side job profile. The profile
(experience, skills, preferences) is a file on the server, never part of a request. One run at a time. Set NEMO_SERVICE_TOKEN to
require `Authorization: Bearer <token>` (required if you bind a non-loopback host).
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from nemo.queryplan import load_config

MAX_BODY = 10_000
RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestError(ValueError):
    """A request the caller must fix (returned as HTTP 400)."""


def build_argv(body: dict[str, Any], *, profile: str, runs_dir: str, db: str, config: dict, run_id: str,
               env_file: str = ".env") -> list[str]:
    """Validate a request body and turn it into run_search arguments. Raises RequestError with an actionable message."""
    if not isinstance(body, dict):
        raise RequestError("body must be a JSON object")
    unknown = set(body) - {"titles", "cities", "max_age_hours", "request_budget"}
    if unknown:
        raise RequestError(f"unknown field(s): {', '.join(sorted(unknown))}")
    known = {c.lower(): (c, loc) for c, loc in (config.get("city_locations") or {}).items()}
    cities = body["cities"] if "cities" in body else list((config.get("city_locations") or {}).keys())
    if not isinstance(cities, list) or not cities or not all(isinstance(c, str) for c in cities):
        raise RequestError("cities must be a non-empty list of city names")
    locations = []
    for c in cities:
        if c.strip().lower() not in known:
            raise RequestError(f"city {c!r} is not configured; configured cities: {', '.join(v[0] for v in known.values())}")
        locations.append(known[c.strip().lower()][1])
    titles = body.get("titles")
    if titles is not None and (not isinstance(titles, list) or not titles or not all(isinstance(t, str) and t.strip() for t in titles)
                               or len(titles) > 10):
        raise RequestError("titles must be a list of 1-10 non-empty strings")
    hours = body.get("max_age_hours", 24)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 1 <= hours <= 720:
        raise RequestError("max_age_hours must be a number between 1 and 720")
    budget = body.get("request_budget", 12)
    if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 50:
        raise RequestError("request_budget must be an integer between 1 and 50")
    argv = ["--profile", profile, "--lookback", f"{int(hours)}h", "--gl", "in", "--max-searches", str(budget), "--out-dir", runs_dir,
            "--db", db, "--env-file", env_file, "--run-id", run_id, "--yes", "--screen"]
    for loc in locations:
        argv += ["--location", loc]
    if titles:
        argv += ["--titles", ",".join(t.strip() for t in titles)]
    return argv


class Collector:
    """Owns the single background run. Status lives in <runs>/<run_id>/status.json, written by the run itself."""

    def __init__(self, *, profile: str = "job-profile.md", runs_dir: str = "runs", db: str = "data/nemo.sqlite", env_file: str = ".env",
                 runner: Callable[[list[str]], int] | None = None, config: dict | None = None):
        self.profile, self.runs, self.db, self.env_file = profile, Path(runs_dir), db, env_file
        self.config = config if config is not None else load_config()
        self._runner = runner
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.recover()

    def recover(self) -> list[str]:
        """A run left 'running' by a process that died is marked 'interrupted' (its raw files and stored jobs remain)."""
        fixed = []
        for f in self.runs.glob("*/status.json"):
            try:
                st = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if st.get("state") == "running":
                f.write_text(json.dumps({**st, "state": "interrupted", "error": "service restarted while the run was in progress",
                                         "finished_at": datetime.now(timezone.utc).isoformat()}, indent=1))
                fixed.append(f.parent.name)
        return fixed

    def start(self, body: dict) -> str:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("a run is already in progress")
            run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
            argv = build_argv(body, profile=self.profile, runs_dir=str(self.runs), db=self.db, config=self.config, run_id=run_id,
                              env_file=self.env_file)
            (self.runs / run_id).mkdir(parents=True, exist_ok=True)
            (self.runs / run_id / "status.json").write_text(json.dumps({"state": "running", "run_id": run_id, "request": body}, indent=1))
            self._thread = threading.Thread(target=self._run, args=(run_id, argv), name=f"collect-{run_id}", daemon=True)
            self._thread.start()
            return run_id

    def _run(self, run_id: str, argv: list[str]) -> None:
        try:
            from nemo.runner import main
            code = (self._runner or main)(argv)
            st = self.status(run_id)
            if st and st.get("state") == "running":      # the runner returned without finishing its status (e.g. refused to start)
                self._write(run_id, {"state": "failed", "error": f"run refused to start (exit code {code}); check the server log, "
                                                                 ".env budget settings and SERPAPI_API_KEY"})
        except BaseException as exc:                      # noqa: BLE001
            st = self.status(run_id) or {}
            if st.get("state") in (None, "running"):
                self._write(run_id, {"state": "failed", "error": f"{type(exc).__name__}: {exc}"})

    def _write(self, run_id: str, extra: dict) -> None:
        st = self.status(run_id) or {"run_id": run_id}
        (self.runs / run_id / "status.json").write_text(json.dumps({**st, **extra, "finished_at": datetime.now(timezone.utc).isoformat()}, indent=1))

    def status(self, run_id: str) -> dict | None:
        f = self.runs / run_id / "status.json"
        try:
            return json.loads(f.read_text())
        except (OSError, ValueError):
            return None

    def result(self, run_id: str, limit: int = 50) -> dict | None:
        st = self.status(run_id)
        if st is None:
            return None
        out = dict(st)
        sc = self.runs / run_id / "screened.json"
        if st.get("state") in ("complete", "partial") and sc.exists():
            data = json.loads(sc.read_text())
            keep = ("title", "company", "location", "match", "stack_fit", "employment", "reported_age", "freshness", "experience_required",
                    "gaps_and_unknowns", "application_link", "application_kind", "all_application_links", "description_source", "readiness",
                    "review", "reasons", "job_key")
            slim = lambda xs: [{k: x.get(k) for k in keep} for x in xs[:limit]]
            out.update({"max_age": data.get("max_age"), "cities": data.get("cities"), "query_stops": data.get("query_stops"),
                        "fresh_shortlist": slim(data["fresh_shortlist"]), "age_unconfirmed": slim(data["age_unconfirmed"]),
                        "counts": {"fresh_shortlist": len(data["fresh_shortlist"]), "age_unconfirmed": len(data["age_unconfirmed"]),
                                   "older_than_max_age": len(data["older_than_max_age"]), "excluded": len(data["excluded"])},
                        "files": {"report": f"runs/{run_id}/report.md", "results": f"runs/{run_id}/results.json",
                                  "raw": f"runs/{run_id}/raw/"}})
        return out

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)


def make_handler(collector: Collector, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "nemo-collector"

        def log_message(self, fmt, *args):             # one line per request; never the body or headers (no secrets, no profile)
            print(f"{self.address_string()} {fmt % args}", flush=True)

        def _send(self, code: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self) -> bool:
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                self._send(401, {"error": "missing or wrong bearer token"})
                return False
            return True

        def do_POST(self):                              # noqa: N802
            if not self._authorised():
                return
            if urlsplit(self.path).path != "/runs":
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._send(413, {"error": f"body larger than {MAX_BODY} bytes"})
                body = json.loads(self.rfile.read(length) or b"{}")
                run_id = collector.start(body)
            except RequestError as exc:
                return self._send(400, {"error": str(exc)})
            except ValueError:
                return self._send(400, {"error": "body is not valid JSON"})
            except RuntimeError as exc:
                return self._send(409, {"error": str(exc)})
            self._send(202, {"run_id": run_id, "state": "running", "status_url": f"/runs/{run_id}"})

        def do_GET(self):                               # noqa: N802
            if not self._authorised():
                return
            parts = urlsplit(self.path)
            m = re.fullmatch(r"/runs/([A-Za-z0-9._-]{1,64})", parts.path)
            if not m:
                return self._send(404, {"error": "not found"})
            limit = min(max(int((parse_qs(parts.query).get("limit") or ["50"])[0]), 1), 500)
            out = collector.result(m.group(1), limit)
            self._send(200 if out else 404, out or {"error": f"unknown run {m.group(1)}"})
    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, **collector_args) -> ThreadingHTTPServer:
    token = os.environ.get("NEMO_SERVICE_TOKEN") or None
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SystemExit("refusing to listen on a non-loopback address without NEMO_SERVICE_TOKEN")
    from nemo.envfile import load_env_file
    load_env_file(collector_args.get("env_file", ".env"))
    token = token or os.environ.get("NEMO_SERVICE_TOKEN") or None
    return ThreadingHTTPServer((host, port), make_handler(Collector(**collector_args), token))
