"""Bright Data Web Scraper API adapter: LinkedIn "Discover jobs by keyword" dataset.

Endpoints and fields checked against docs.brightdata.com on 2026-10-01 (see
docs/phase1/08-job-data-provider-evaluation.md). NOT yet exercised against the live API:
the record shape below comes from the documented schema, so `normalize` is tolerant of
missing fields and the first live run must be compared with it.

    POST /datasets/v3/trigger?dataset_id=..&type=discover_new&discover_by=keyword&include_errors=true
         body {"input": [{keyword, location, country, time_range}], "limit_per_input": N} -> {"snapshot_id"}
    GET  /datasets/v3/progress/<snapshot_id>      -> {"status": starting|running|ready|failed|canceled, ...}
    GET  /datasets/v3/snapshot/<snapshot_id>?format=json   (kept 30 days)

Billing (published): 1 credit per record, 5,000 free credits/month, $1.50/1,000 pay-as-you-go,
failed records not charged. Only Bright Data is contacted; LinkedIn never is.
"""

from __future__ import annotations

import json
import math
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from nemo.jobsource import CollectRequest, JobRecord
from nemo.linkedin import canonical_job_url, linkedin_job_id
from nemo.providers.base import ProviderError

BASE = "https://api.brightdata.com/datasets/v3"
DATASET_JOBS_BY_KEYWORD = "gd_lpfll7v5hcqtkxl6l"
_KNOWN_STATES = {"starting", "running", "ready", "failed", "canceled"}
_ELLIPSIS_END = re.compile(r"(\.\.\.|…|show more|see more)\s*$", re.I)
MIN_FULL_DESCRIPTION_CHARS = 400


def description_status(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return "missing"
    if _ELLIPSIS_END.search(t):
        return "truncated"
    return "full" if len(t) >= MIN_FULL_DESCRIPTION_CHARS else "short"


def redact(obj: Any, secret: str) -> Any:
    if isinstance(obj, dict):
        return {k: redact(v, secret) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, secret) for v in obj]
    if isinstance(obj, str) and secret and secret in obj:
        return obj.replace(secret, "REDACTED")
    return obj


class BrightDataJobs:
    name = "brightdata"

    def __init__(self, api_key: str, *, usd_per_record: float = 0.0015, timeout: float = 60.0,
                 opener: Callable[..., Any] | None = None):
        if usd_per_record <= 0:
            raise ValueError("usd_per_record must be positive (published PAYG worst case is 0.0015)")
        self._key, self.max_cost_per_record_usd, self._timeout = api_key, float(usd_per_record), timeout
        self._open = opener or urllib.request.urlopen

    # ------------------------------------------------------------------ http
    def _call(self, method: str, path: str, params: dict[str, str] | None = None, body: Any = None) -> Any:
        url = f"{BASE}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self._timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            kind = {400: "bad_request", 401: "unauthorized", 404: "not_found", 429: "rate_limited"}.get(
                exc.code, "server_error" if exc.code >= 500 else "http_error")
            raise ProviderError(kind, f"HTTP {exc.code}", charged=False) from None
        except (TimeoutError, socket.timeout):
            raise ProviderError("timeout", "request timed out") from None
        except urllib.error.URLError as exc:
            kind = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "connection_error"
            raise ProviderError(kind, str(exc.reason)) from None
        try:
            return json.loads(text) if text.strip() else None
        except json.JSONDecodeError:
            return text  # snapshot may be JSON lines; caller decides

    # ------------------------------------------------------------------ interface
    def trigger_body(self, req: CollectRequest) -> dict[str, Any]:
        inputs = []
        for role in req.roles:
            for loc in req.locations:
                item = {"keyword": role, "location": loc, "country": req.country}
                if req.time_range:
                    item["time_range"] = req.time_range
                inputs.append(item)
        # ceil so the request can reach max_records; the collector still truncates storage at max_records
        return {"input": inputs, "limit_per_input": max(1, math.ceil(req.max_records / len(inputs)))}

    def launch(self, req: CollectRequest) -> str:
        """One POST, never retried here: a timeout may still have started a paid collection."""
        out = self._call("POST", "/trigger", {"dataset_id": DATASET_JOBS_BY_KEYWORD, "type": "discover_new",
                                              "discover_by": "keyword", "include_errors": "true"},
                         self.trigger_body(req))
        sid = out.get("snapshot_id") if isinstance(out, dict) else None
        if not sid:
            raise ProviderError("bad_response", "trigger returned no snapshot_id")
        return str(sid)

    def status(self, op_id: str) -> tuple[str, dict[str, Any]]:
        if not op_id.replace("_", "").isalnum():
            raise ValueError("bad snapshot id")
        raw = self._call("GET", f"/progress/{op_id}")
        raw = raw if isinstance(raw, dict) else {}
        state = str(raw.get("status", "")).lower()
        if state not in _KNOWN_STATES:
            raise ProviderError("bad_response", f"unknown progress status {state!r}")
        return state, redact(raw, self._key)

    def fetch(self, op_id: str) -> list[dict[str, Any]]:
        out = self._call("GET", f"/snapshot/{op_id}", {"format": "json"})
        if isinstance(out, str):  # JSON lines fallback
            out = [json.loads(l) for l in out.splitlines() if l.strip()]
        if isinstance(out, dict):  # e.g. {"status": "building"} or a single record
            if "status" in out and "job_posting_id" not in out and "url" not in out:
                raise ProviderError("not_ready", str(out.get("status")))
            out = [out]
        if not isinstance(out, list):
            raise ProviderError("bad_response", "snapshot was not a list")
        return [redact(r, self._key) for r in out if isinstance(r, dict)]

    def normalize(self, raw: dict[str, Any], retrieved_at: str) -> JobRecord | None:
        if raw.get("error") or raw.get("error_code"):
            return None
        url = str(raw.get("url") or raw.get("apply_link") or "")
        jid = str(raw.get("job_posting_id") or "") or (linkedin_job_id(url) or "")
        if not jid:
            return None
        flags: list[str] = []
        url_id = linkedin_job_id(url)
        if url_id and url_id != jid:
            flags.append(f"url_id_mismatch: url has {url_id}, record id {jid}")
        if not url_id:
            flags.append("source url is not an individual LinkedIn job URL")
        desc = str(raw.get("job_description_formatted") or raw.get("job_description") or "")
        status = description_status(desc)
        if status != "full":
            flags.append(f"description_{status}")
        return JobRecord(
            source="linkedin", source_job_id=jid, url=canonical_job_url(jid) if not url_id else url,
            title=raw.get("job_title"), company=raw.get("company_name"), location=raw.get("job_location"),
            description=desc, description_status=status, posted_at=raw.get("job_posted_date"),
            posted_text=raw.get("job_posted_time"), provider=self.name, retrieved_at=retrieved_at,
            flags=flags, raw=raw)
