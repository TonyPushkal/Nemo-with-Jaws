"""SQLite persistence for provider-based collection runs (same file as the search collector's history).

Our run id (`collect_runs.run_id`) and the provider's operation id (`provider_op_id`) are separate.
Jobs are keyed by (source, source_job_id); every run keeps its own copy of the raw provider record.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from nemo.jobsource import JobRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS collect_runs (
  run_id TEXT PRIMARY KEY, provider TEXT NOT NULL, provider_op_id TEXT, status TEXT NOT NULL, reason TEXT,
  request_json TEXT, created_at TEXT NOT NULL, launched_at TEXT, finished_at TEXT,
  records_received INTEGER DEFAULT 0, error_records INTEGER DEFAULT 0, jobs_stored INTEGER DEFAULT 0,
  duplicates INTEGER DEFAULT 0, est_max_usd REAL, est_billed_usd REAL, measured_usd REAL, progress_json TEXT);
CREATE TABLE IF NOT EXISTS collect_jobs (
  source TEXT NOT NULL, source_job_id TEXT NOT NULL, url TEXT, title TEXT, company TEXT, location TEXT,
  description TEXT, description_status TEXT, posted_at TEXT, posted_text TEXT, provider TEXT,
  verification TEXT, flags_json TEXT, first_seen_at TEXT, first_seen_run TEXT, last_seen_at TEXT,
  last_seen_run TEXT, PRIMARY KEY (source, source_job_id));
CREATE TABLE IF NOT EXISTS collect_run_jobs (
  run_id TEXT NOT NULL, source TEXT NOT NULL, source_job_id TEXT NOT NULL, retrieved_at TEXT NOT NULL,
  flags_json TEXT, raw_json TEXT, PRIMARY KEY (run_id, source, source_job_id));
"""

FINAL = {"succeeded", "no_results", "partial", "failed"}


class CollectStore:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ---- runs
    def create_run(self, run_id: str, provider: str, request: dict, created_at: str, est_max_usd: float) -> None:
        with self.db:
            self.db.execute("INSERT INTO collect_runs (run_id, provider, status, request_json, created_at, est_max_usd) "
                            "VALUES (?,?,?,?,?,?)",
                            (run_id, provider, "launching", json.dumps(request), created_at, est_max_usd))

    def update_run(self, run_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v) if k == "progress_json" and not isinstance(v, str) else v for k, v in fields.items()]
        with self.db:
            self.db.execute(f"UPDATE collect_runs SET {cols} WHERE run_id=?", (*vals, run_id))

    def get_run_row(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM collect_runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def stale_runs(self) -> list[dict]:
        """Runs that never reached a final or resumable state (process died mid-flight)."""
        rows = self.db.execute("SELECT * FROM collect_runs WHERE status IN ('launching','running')").fetchall()
        return [dict(r) for r in rows]

    # ---- jobs (one transaction per job so results survive an interruption)
    def save_job(self, run_id: str, job: JobRecord, seen_at: str) -> bool:
        """Returns True if this job id was already stored by a previous run."""
        flags = json.dumps(job.flags)
        with self.db:
            prior = self.db.execute("SELECT 1 FROM collect_jobs WHERE source=? AND source_job_id=?",
                                    (job.source, job.source_job_id)).fetchone() is not None
            if prior:
                self.db.execute(
                    "UPDATE collect_jobs SET url=?, title=COALESCE(?, title), company=COALESCE(?, company), "
                    "location=COALESCE(?, location), description=?, description_status=?, posted_at=COALESCE(?, posted_at), "
                    "posted_text=COALESCE(?, posted_text), provider=?, verification=?, flags_json=?, last_seen_at=?, "
                    "last_seen_run=? WHERE source=? AND source_job_id=?",
                    (job.url, job.title, job.company, job.location, job.description, job.description_status,
                     job.posted_at, job.posted_text, job.provider, job.verification, flags, seen_at, run_id,
                     job.source, job.source_job_id))
            else:
                self.db.execute(
                    "INSERT INTO collect_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job.source, job.source_job_id, job.url, job.title, job.company, job.location, job.description,
                     job.description_status, job.posted_at, job.posted_text, job.provider, job.verification, flags,
                     seen_at, run_id, seen_at, run_id))
            self.db.execute("INSERT OR REPLACE INTO collect_run_jobs VALUES (?,?,?,?,?,?)",
                            (run_id, job.source, job.source_job_id, job.retrieved_at, flags,
                             json.dumps(job.raw, ensure_ascii=False)))
        return prior

    def run_jobs(self, run_id: str, limit: int, include_raw: bool = False) -> list[dict]:
        rows = self.db.execute(
            "SELECT j.*, r.retrieved_at, r.raw_json FROM collect_run_jobs r JOIN collect_jobs j "
            "ON j.source=r.source AND j.source_job_id=r.source_job_id WHERE r.run_id=? "
            "ORDER BY r.rowid LIMIT ?", (run_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["flags"] = json.loads(d.pop("flags_json") or "[]")
            raw = d.pop("raw_json")
            if include_raw:
                d["raw"] = json.loads(raw)
            out.append(d)
        return out

    def close(self) -> None:
        self.db.close()
