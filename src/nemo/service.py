"""Minimal HTTP service over the discovery flow (Python standard library only; one process; localhost by default).

    GET  /, /app.js, /style.css   -> the static demo frontend (frontend/), served without auth
    GET  /quota                   -> {"remaining", "limit", "window_seconds", "retry_after_seconds"}: the shared rate limit
    POST /runs        {"cities": [...]?, "max_age_hours": 24?, "request_budget": 12?,
                        either "titles": [...]? (screens against the server's own job-profile.md)
                        or a custom profile: "experience", "desired_roles", "must_haves"?, "nice_to_haves"?, "exclusions"?}
                      -> 202 {"run_id", "state": "running", "status_url", "quota"}
                      -> 409 if a run is already in progress; 429 if the shared rate limit is used up
    GET  /runs/<id>   -> status, why collection stopped, request use, freshness counts and, when finished, the screened jobs

A request runs the same flow as `scripts/run_search.py --screen`: SerpApi Google Jobs searches under the existing spend guards, pages
followed until the request budget / end of results / repeated pages, screening against a job profile. Without a custom profile,
that's the file on the server (job-profile.md); a request that supplies one instead is screened against it (written to
runs/<id>/profile.md, never persisted elsewhere). One run at a time; a global NEMO_RATE_LIMIT_PER_HOUR (default 5) caps how many
runs anyone can start per hour, shared across all visitors, to bound paid-API spend on a public deployment. Set NEMO_SERVICE_TOKEN
to require `Authorization: Bearer <token>` on the API endpoints (required if you bind a non-loopback host; the static frontend
itself is always public).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from nemo.profile import JobProfile, render_profile
from nemo.queryplan import load_config

MAX_BODY = 10_000
RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CUSTOM_PROFILE_FIELDS = ("experience", "desired_roles", "must_haves", "nice_to_haves", "exclusions")
PUBLIC_PROFILE_SEARCH_CAP = 10   # a custom (visitor-supplied) profile is a public, unmetered-by-you request: keep its worst-case spend small
STATIC_DIR = Path(__file__).resolve().parents[2] / "frontend"
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}


class RequestError(ValueError):
    """A request the caller must fix (returned as HTTP 400)."""


def _as_str_list(body: dict, field: str) -> list[str]:
    v = body.get(field, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise RequestError(f"{field} must be a list of strings")
    return v


def _write_custom_profile(body: dict, profile_dir: Path) -> str:
    """A visitor-supplied profile (experience + desired_roles, optional must/nice/exclusions) -> a profile.md
    under this run's own directory, loaded the same way as the server's default profile."""
    if not isinstance(body.get("experience"), str):
        raise RequestError("experience must be a string")
    roles = _as_str_list(body, "desired_roles")
    must_haves, nice_to_haves, exclusions = (_as_str_list(body, f) for f in ("must_haves", "nice_to_haves", "exclusions"))
    try:
        prof = JobProfile(experience=body["experience"], desired_roles=tuple(roles), must_haves=tuple(must_haves),
                          nice_to_haves=tuple(nice_to_haves), exclusions=tuple(exclusions))
    except ValidationError as exc:
        msgs = [e["msg"].removeprefix("Value error, ") for e in exc.errors()]
        raise RequestError("; ".join(msgs)) from None
    path = profile_dir / "profile.md"
    path.write_text(render_profile(prof), encoding="utf-8")
    return str(path)


def build_argv(body: dict[str, Any], *, profile: str, runs_dir: str, db: str, config: dict, run_id: str,
               env_file: str = ".env", profile_dir: Path | None = None) -> list[str]:
    """Validate a request body and turn it into run_search arguments. Raises RequestError with an actionable message."""
    if not isinstance(body, dict):
        raise RequestError("body must be a JSON object")
    unknown = set(body) - {"titles", "cities", "max_age_hours", "request_budget", *CUSTOM_PROFILE_FIELDS}
    if unknown:
        raise RequestError(f"unknown field(s): {', '.join(sorted(unknown))}")
    custom = set(body) & set(CUSTOM_PROFILE_FIELDS)
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
    if custom:
        if titles:
            raise RequestError("titles cannot be combined with a custom profile; desired_roles in the profile are used instead")
        missing = {"experience", "desired_roles"} - custom
        if missing:
            raise RequestError(f"a custom profile needs: {', '.join(sorted(missing))}")
        if profile_dir is None:
            raise RequestError("this server does not accept a custom profile")
        profile = _write_custom_profile(body, profile_dir)
    budget_cap = PUBLIC_PROFILE_SEARCH_CAP if custom else 50
    hours = body.get("max_age_hours", 24)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 1 <= hours <= 720:
        raise RequestError("max_age_hours must be a number between 1 and 720")
    budget = body.get("request_budget", min(12, budget_cap))
    if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= budget_cap:
        raise RequestError(f"request_budget must be an integer between 1 and {budget_cap}")
    argv = ["--profile", profile, "--lookback", f"{int(hours)}h", "--gl", "in", "--max-searches", str(budget), "--out-dir", runs_dir,
            "--db", db, "--env-file", env_file, "--run-id", run_id, "--yes", "--screen"]
    for loc in locations:
        argv += ["--location", loc]
    if titles:
        argv += ["--titles", ",".join(t.strip() for t in titles)]
    return argv


class RateLimiter:
    """A global sliding-window cap shared by every visitor (not per-IP): simplest way to bound total paid search
    volume on a public demo. In-memory only; resets if the process restarts."""

    def __init__(self, limit: int = 5, window_seconds: float = 3600):
        self.limit, self.window = limit, window_seconds
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._hits and now - self._hits[0] >= self.window:
            self._hits.popleft()

    def _retry_after(self, now: float) -> int:
        return int(self.window - (now - self._hits[0])) + 1 if self._hits else 0

    def status(self) -> dict:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            remaining = self.limit - len(self._hits)
            return {"remaining": max(remaining, 0), "limit": self.limit, "window_seconds": int(self.window),
                    "retry_after_seconds": self._retry_after(now) if remaining <= 0 else 0}

    def try_consume(self) -> tuple[bool, dict]:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            if len(self._hits) >= self.limit:
                return False, {"remaining": 0, "limit": self.limit, "window_seconds": int(self.window),
                               "retry_after_seconds": self._retry_after(now)}
            self._hits.append(now)
            return True, {"remaining": self.limit - len(self._hits), "limit": self.limit, "window_seconds": int(self.window),
                          "retry_after_seconds": 0}

    def release(self) -> None:
        """Give back a slot consumed for a request that turned out invalid or could not start (not the visitor's fault)."""
        with self._lock:
            if self._hits:
                self._hits.pop()


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
            run_dir = self.runs / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            argv = build_argv(body, profile=self.profile, runs_dir=str(self.runs), db=self.db, config=self.config, run_id=run_id,
                              env_file=self.env_file, profile_dir=run_dir)
            (run_dir / "status.json").write_text(json.dumps({"state": "running", "run_id": run_id, "request": body}, indent=1))
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


def make_handler(collector: Collector, token: str | None, limiter: RateLimiter | None = None):
    limiter = limiter or RateLimiter()

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

        def _send_static(self, path: str) -> None:
            filename, content_type = STATIC_FILES[path]
            try:
                data = (STATIC_DIR / filename).read_bytes()
            except OSError:
                return self._send(404, {"error": "not found"})
            self.send_response(200)
            self.send_header("Content-Type", content_type)
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
            ok, quota = limiter.try_consume()
            if not ok:
                return self._send(429, {"error": f"rate limit reached: {quota['limit']} runs per hour across all visitors; "
                                                  f"try again in {quota['retry_after_seconds']}s", **quota})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    limiter.release()
                    return self._send(413, {"error": f"body larger than {MAX_BODY} bytes"})
                body = json.loads(self.rfile.read(length) or b"{}")
                run_id = collector.start(body)
            except RequestError as exc:
                limiter.release()
                return self._send(400, {"error": str(exc)})
            except ValueError:
                limiter.release()
                return self._send(400, {"error": "body is not valid JSON"})
            except RuntimeError as exc:
                limiter.release()
                return self._send(409, {"error": str(exc)})
            self._send(202, {"run_id": run_id, "state": "running", "status_url": f"/runs/{run_id}", "quota": quota})

        def do_GET(self):                               # noqa: N802
            parts = urlsplit(self.path)
            if parts.path in STATIC_FILES:
                return self._send_static(parts.path)
            if not self._authorised():
                return
            if parts.path == "/quota":
                return self._send(200, limiter.status())
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
    limit = int(os.environ.get("NEMO_RATE_LIMIT_PER_HOUR", "5"))
    limiter = RateLimiter(limit=limit, window_seconds=3600)
    return ThreadingHTTPServer((host, port), make_handler(Collector(**collector_args), token, limiter))
