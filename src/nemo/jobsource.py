"""Small provider interface for on-demand job collection (launch -> poll -> fetch).

A provider discovers jobs for a request and returns them in the provider's own raw shape;
`normalize` maps one raw record to a `JobRecord`. Nothing here names a vendor. The older
search-based collector (`scripts/run_search.py`) is a separate option and does not use this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# Provider-reported operation states, normalised.
RUNNING_STATES = {"starting", "running"}
TERMINAL_STATES = {"ready", "failed", "canceled"}


@dataclass(frozen=True)
class CollectRequest:
    roles: tuple[str, ...]
    locations: tuple[str, ...] = ("India",)
    country: str = "IN"
    time_range: str | None = None         # provider vocabulary, e.g. "Past week"
    max_records: int = 25                 # hard bound on records requested AND stored
    poll_timeout_s: float = 600.0
    poll_interval_s: float = 5.0

    def inputs(self) -> int:
        return len(self.roles) * len(self.locations)


@dataclass
class JobRecord:
    source: str                           # e.g. "linkedin"
    source_job_id: str
    url: str
    title: str | None
    company: str | None
    location: str | None
    description: str
    description_status: str               # full | short | truncated | missing
    posted_at: str | None                 # provider-supplied, unverified
    posted_text: str | None
    provider: str
    retrieved_at: str
    flags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    verification: str = "provider_reported"   # never "verified" merely because a provider returned it


class JobProvider(Protocol):
    name: str
    max_cost_per_record_usd: float        # published worst-case price per record

    def launch(self, req: CollectRequest) -> str:
        """Start collection; return the provider's operation id. MUST NOT be retried blindly."""

    def status(self, op_id: str) -> tuple[str, dict[str, Any]]:
        """(normalised state, raw progress payload)."""

    def fetch(self, op_id: str) -> list[dict[str, Any]]:
        """All raw records for a finished operation (error records included)."""

    def normalize(self, raw: dict[str, Any], retrieved_at: str) -> JobRecord | None:
        """Map one raw record; None for provider error records (not jobs)."""
