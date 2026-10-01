"""Provider-based job collection: collect(request) -> run, resume(run_id), get_run(run_id).

Synchronous and bounded. The run row is written BEFORE the paid launch, the launch is never retried,
and jobs are saved one by one, so an interrupted process leaves a recoverable record:
  launching      row written, launch not confirmed        -> `launch_unknown` on recovery (check the provider dashboard)
  running        provider op id known, polling/fetching   -> resumable with resume(run_id)
  succeeded | no_results | partial | failed | timed_out | interrupted | launch_unknown
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from nemo.budget import Budget, BudgetExceeded, PaidCallsDisabled, UnpricedPaidProvider
from nemo.collect_store import CollectStore
from nemo.consistency import check
from nemo.jobsource import JobProvider, CollectRequest
from nemo.providers.base import ProviderError

MAX_RECORDS_LIMIT = 200
TRANSIENT = {"timeout", "rate_limited", "server_error", "connection_error"}
MAX_CONSECUTIVE_POLL_ERRORS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate(req: CollectRequest) -> None:
    if not req.roles or not all(r.strip() for r in req.roles):
        raise ValueError("at least one non-empty role is required")
    if not req.locations:
        raise ValueError("at least one location is required")
    if not 1 <= req.max_records <= MAX_RECORDS_LIMIT:
        raise ValueError(f"max_records must be 1..{MAX_RECORDS_LIMIT}")
    if req.poll_timeout_s <= 0 or req.poll_interval_s <= 0:
        raise ValueError("poll timeout and interval must be positive")


def collect(req: CollectRequest, provider: JobProvider, store: CollectStore, budget: Budget, *,
            sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Launch one collection and drive it to a result. Never raises for provider/budget problems:
    the returned dict has `status` and an actionable `reason`."""
    validate(req)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    worst = req.max_records * provider.max_cost_per_record_usd
    store.create_run(run_id, provider.name, {**req.__dict__, "roles": list(req.roles), "locations": list(req.locations)},
                     _now(), worst)
    try:
        # One reservation per launch attempt, worst case = max_records at the published per-record price.
        with budget.call("queries", provider.name, is_paid=True, max_cost_usd=worst):
            op_id = provider.launch(req)
    except (BudgetExceeded, PaidCallsDisabled, UnpricedPaidProvider) as exc:
        return _finish(store, run_id, "failed", f"not launched (nothing spent): {exc}")
    except ProviderError as exc:
        if exc.kind in TRANSIENT:  # request may have reached the provider and started a paid run
            return _finish(store, run_id, "launch_unknown",
                           f"launch outcome unknown ({exc}); NOT retried to avoid a duplicate paid run. "
                           "Check the provider dashboard for a new collection before launching again.")
        return _finish(store, run_id, "failed", f"launch rejected: {exc}" + _hint(exc))
    store.update_run(run_id, provider_op_id=op_id, status="running", launched_at=_now())
    return _drive(run_id, req, provider, store, sleep, clock)


def resume(run_id: str, provider: JobProvider, store: CollectStore, *, poll_timeout_s: float = 600.0,
           poll_interval_s: float = 5.0, sleep: Callable[[float], None] = time.sleep,
           clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Re-attach to a run that was interrupted or timed out. Never launches anything."""
    row = store.get_run_row(run_id)
    if row is None:
        raise KeyError(f"unknown run {run_id}")
    if not row["provider_op_id"]:
        if row["status"] in ("launching", "launch_unknown"):
            return _finish(store, run_id, "launch_unknown",
                           "no provider operation id was recorded; the launch may or may not have happened. "
                           "Check the provider dashboard; start a new run only if nothing is there.")
        return get_run(run_id, store)
    if row["status"] in ("succeeded", "no_results", "failed"):
        return get_run(run_id, store)
    d = json.loads(row["request_json"])
    req = CollectRequest(roles=tuple(d["roles"]), locations=tuple(d["locations"]), country=d["country"],
                         time_range=d.get("time_range"), max_records=d["max_records"],
                         poll_timeout_s=poll_timeout_s, poll_interval_s=poll_interval_s)
    store.update_run(run_id, status="running")
    return _drive(run_id, req, provider, store, sleep, clock)


def recover_stale(store: CollectStore) -> list[str]:
    """Mark runs whose process died: 'launching' -> launch_unknown; 'running' -> interrupted (resumable)."""
    fixed = []
    for r in store.stale_runs():
        if r["status"] == "launching" or not r["provider_op_id"]:
            _finish(store, r["run_id"], "launch_unknown", "process ended before the launch was confirmed; "
                    "check the provider dashboard before launching again")
        else:
            store.update_run(r["run_id"], status="interrupted", reason="process ended while collecting; use resume")
        fixed.append(r["run_id"])
    return fixed


def get_run(run_id: str, store: CollectStore, *, limit: int = 50, include_raw: bool = False) -> dict[str, Any]:
    row = store.get_run_row(run_id)
    if row is None:
        raise KeyError(f"unknown run {run_id}")
    return {**row, "jobs": store.run_jobs(run_id, limit, include_raw)}


# ---------------------------------------------------------------------- internals

def _hint(exc: ProviderError) -> str:
    return {"unauthorized": " (check BRIGHTDATA_API_KEY)", "not_found": " (dataset id or snapshot not found)",
            "bad_request": " (the provider rejected the input; check roles/locations/time_range)",
            "rate_limited": " (concurrency limit; wait and retry later)"}.get(exc.kind, "")


def _finish(store: CollectStore, run_id: str, status: str, reason: str | None) -> dict[str, Any]:
    store.update_run(run_id, status=status, reason=reason, finished_at=_now())
    return get_run(run_id, store)


def _drive(run_id: str, req: CollectRequest, provider: JobProvider, store: CollectStore,
           sleep: Callable[[float], None], clock: Callable[[], float]) -> dict[str, Any]:
    row = store.get_run_row(run_id)
    op_id = row["provider_op_id"]
    deadline, errors, state, progress = clock() + req.poll_timeout_s, 0, "", {}
    try:
        while True:
            try:
                state, progress = provider.status(op_id)
                errors = 0
            except ProviderError as exc:
                if exc.kind not in TRANSIENT:  # status/progress reads are idempotent, so transient ones may retry
                    return _finish(store, run_id, "failed", f"polling failed: {exc}" + _hint(exc))
                errors += 1
                if errors >= MAX_CONSECUTIVE_POLL_ERRORS:
                    store.update_run(run_id, status="timed_out", finished_at=_now(),
                                     reason=f"polling kept failing ({exc}); the provider operation {op_id} may still "
                                            "be running; use resume")
                    return get_run(run_id, store)
            store.update_run(run_id, progress_json=progress)
            if state in ("ready", "failed", "canceled"):
                break
            if clock() >= deadline:
                store.update_run(run_id, status="timed_out", finished_at=_now(),
                                 reason=f"still '{state}' after {req.poll_timeout_s:.0f}s; operation {op_id} was not "
                                        "cancelled and may still be billed; use resume to re-attach")
                return get_run(run_id, store)
            sleep(req.poll_interval_s)
        return _collect_results(run_id, req, provider, store, state, progress)
    except BaseException:
        cur = store.get_run_row(run_id)
        if cur and cur["status"] == "running":
            store.update_run(run_id, status="interrupted", reason="interrupted; use resume")
        raise


def _collect_results(run_id: str, req: CollectRequest, provider: JobProvider, store: CollectStore,
                     state: str, progress: dict) -> dict[str, Any]:
    try:
        raws = provider.fetch(store.get_run_row(run_id)["provider_op_id"])
    except ProviderError as exc:
        if state == "ready":
            store.update_run(run_id, status="timed_out", finished_at=_now(),
                             reason=f"collection is ready but download failed ({exc}); use resume")
            return get_run(run_id, store)
        raws = []  # provider failed/cancelled and nothing downloadable
    retrieved_at, received, errors, dupes, stored = _now(), 0, 0, 0, 0
    seen: set[str] = set()
    for raw in raws:
        job = provider.normalize(raw, retrieved_at)
        if job is None:
            errors += 1
            continue
        received += 1
        if job.source_job_id in seen:
            dupes += 1
            continue
        if stored >= req.max_records:
            continue  # bound what we keep, even if the provider returned more
        seen.add(job.source_job_id)
        problems = check({"company_name": job.company, "title": job.title, "location": job.location,
                          "description": job.description}, job.url)
        if not any(p.startswith("LinkedIn slug has no company") for p in problems):  # URL has no slug to compare
            job.flags += [f"link_check: {p}" for p in problems]
        store.save_job(run_id, job, retrieved_at)
        stored += 1
        store.update_run(run_id, jobs_stored=stored)  # incremental progress
    billed = received * getattr(provider, "max_cost_per_record_usd", 0.0)
    fields = dict(records_received=received, error_records=errors, duplicates=dupes, jobs_stored=stored,
                  est_billed_usd=billed)
    if state == "ready":
        if stored == 0:
            status, reason = "no_results", ("search succeeded but returned 0 jobs" +
                                            (f" ({errors} error records)" if errors else ""))
        elif errors:
            status, reason = "partial", f"{errors} record(s) failed at the provider"
        else:
            status, reason = "succeeded", ("truncated to max_records" if received > req.max_records else None)
    else:
        status = "partial" if stored else "failed"
        reason = f"provider reported '{state}'" + (f"; kept {stored} downloaded job(s)" if stored else "")
    store.update_run(run_id, **fields)
    return _finish(store, run_id, status, reason)
