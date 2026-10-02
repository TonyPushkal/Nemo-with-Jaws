"""SQLite history for discovery runs: runs, queries, and jobs de-duplicated by LinkedIn job id.

History never hides jobs: every run returns everything it collected; `previously_seen` is information.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT,
  profile_sha256 TEXT, lookback TEXT, window_start TEXT, window_end TEXT, params_json TEXT, summary_json TEXT);
CREATE TABLE IF NOT EXISTS run_queries (
  run_id TEXT NOT NULL, seq INTEGER NOT NULL, engine TEXT, role TEXT, q TEXT, location TEXT,
  status TEXT, kept INTEGER, error TEXT, search_id TEXT, PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS jobs (
  linkedin_job_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, company TEXT, location TEXT,
  description TEXT, first_seen_at TEXT NOT NULL, first_seen_run TEXT NOT NULL,
  last_seen_at TEXT NOT NULL, last_seen_run TEXT NOT NULL, latest_json TEXT);
CREATE TABLE IF NOT EXISTS run_jobs (
  run_id TEXT NOT NULL, linkedin_job_id TEXT NOT NULL, lookback_status TEXT, record_json TEXT,
  PRIMARY KEY (run_id, linkedin_job_id));
"""


class Store:
    def __init__(self, path: Path | str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def start_run(self, run_id: str, started_at: str, **fields: Any) -> None:
        with self.db:
            self.db.execute("INSERT INTO runs (id, started_at, profile_sha256, lookback, window_start, window_end, "
                            "params_json) VALUES (?,?,?,?,?,?,?)",
                            (run_id, started_at, fields.get("profile_sha256"), fields.get("lookback"),
                             fields.get("window_start"), fields.get("window_end"), json.dumps(fields.get("params"))))

    def add_query(self, run_id: str, seq: int, **q: Any) -> None:
        with self.db:
            self.db.execute("INSERT INTO run_queries VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (run_id, seq, q.get("engine"), q.get("role"), q.get("q"), q.get("location"),
                             q.get("status"), q.get("kept"), q.get("error"), q.get("search_id")))

    def upsert_jobs(self, run_id: str, seen_at: str, jobs: dict[str, dict]) -> None:
        """Insert or update each job; fills in first/last-seen and previously_seen on the dicts."""
        with self.db:
            for jid, j in jobs.items():
                row = self.db.execute("SELECT first_seen_at, first_seen_run FROM jobs WHERE linkedin_job_id=?",
                                      (jid,)).fetchone()
                if row:
                    j["first_seen_at"], j["first_seen_run"], j["previously_seen"] = row[0], row[1], row[1] != run_id
                    self.db.execute(
                        "UPDATE jobs SET url=?, title=COALESCE(?, title), company=COALESCE(?, company), "
                        "location=COALESCE(?, location), description=CASE WHEN length(?) > length(COALESCE(description,'')) "
                        "THEN ? ELSE description END, last_seen_at=?, last_seen_run=?, latest_json=? WHERE linkedin_job_id=?",
                        (j["url"], j["title"], j["company"], j["location"], j["description"], j["description"],
                         seen_at, run_id, json.dumps(j, ensure_ascii=False), jid))
                else:
                    j["first_seen_at"], j["first_seen_run"], j["previously_seen"] = seen_at, run_id, False
                    self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                    (jid, j["url"], j["title"], j["company"], j["location"], j["description"],
                                     seen_at, run_id, seen_at, run_id, json.dumps(j, ensure_ascii=False)))
                j["last_seen_at"], j["last_seen_run"] = seen_at, run_id
                self.db.execute("INSERT OR REPLACE INTO run_jobs VALUES (?,?,?,?)",
                                (run_id, jid, j.get("lookback_status"), json.dumps(j, ensure_ascii=False)))

    def finish_run(self, run_id: str, finished_at: str, status: str, summary: dict) -> None:
        with self.db:
            self.db.execute("UPDATE runs SET finished_at=?, status=?, summary_json=? WHERE id=?",
                            (finished_at, status, json.dumps(summary), run_id))

    def close(self) -> None:
        self.db.close()
