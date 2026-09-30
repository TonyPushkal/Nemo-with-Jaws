"""Pure rules for closing jobs and for age filtering. Offline and unit-tested.

A job is `closed` only on sufficient evidence (docs/phase1/03-architecture.md §4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Literal

from nemo.models import JobDates, JobStatus


class Signal(str, Enum):
    # strong: one is enough
    EXPLICIT_CLOSED_TEXT = "explicit_closed_text"
    STRUCTURED_EXPIRED = "structured_expired"      # validThrough in the past
    HTTP_GONE = "http_gone"                        # 410
    ATS_LISTING_ABSENT = "ats_listing_absent"      # only emit when the listing was complete/authoritative
    # weak, fetch-based: need two observations >= 24h apart
    HTTP_NOT_FOUND = "http_not_found"
    REDIRECT_TO_LISTING = "redirect_to_listing"
    # never sufficient on their own
    NOT_IN_SEARCH_RESULTS = "not_in_search_results"
    NOT_SEEN_RECENT_RUNS = "not_seen_recent_runs"
    # positive evidence
    FETCHED_OPEN = "fetched_open"


STRONG = {Signal.EXPLICIT_CLOSED_TEXT, Signal.STRUCTURED_EXPIRED, Signal.HTTP_GONE, Signal.ATS_LISTING_ABSENT}
WEAK_FETCH = {Signal.HTTP_NOT_FOUND, Signal.REDIRECT_TO_LISTING}
NON_FETCH = {Signal.NOT_IN_SEARCH_RESULTS, Signal.NOT_SEEN_RECENT_RUNS}
MIN_WEAK_SPAN = timedelta(hours=24)


@dataclass(frozen=True)
class Observation:
    signal: Signal
    observed_at: datetime


@dataclass(frozen=True)
class ClosureDecision:
    status: JobStatus
    reason: str


def decide_status(observations: list[Observation]) -> ClosureDecision:
    """Signals older than the most recent successful open fetch are discarded
    (the state has since changed)."""
    obs = sorted(observations, key=lambda o: o.observed_at)
    last_open = max((o.observed_at for o in obs if o.signal is Signal.FETCHED_OPEN), default=None)
    if last_open is not None:
        obs = [o for o in obs if o.observed_at >= last_open]
    live = [o for o in obs if o.signal is not Signal.FETCHED_OPEN]
    if not live:
        if last_open is not None:
            return ClosureDecision(JobStatus.OPEN, "fetched open with no later closing signal")
        return ClosureDecision(JobStatus.UNKNOWN, "no observations")

    strong = [o for o in live if o.signal in STRONG]
    if strong:
        return ClosureDecision(JobStatus.CLOSED, f"strong signal: {strong[0].signal.value}")
    weak = [o.observed_at for o in live if o.signal in WEAK_FETCH]
    if len(weak) >= 2 and max(weak) - min(weak) >= MIN_WEAK_SPAN:
        return ClosureDecision(JobStatus.CLOSED, "weak fetch signals seen at least 24h apart")
    return ClosureDecision(
        JobStatus.POSSIBLY_CLOSED,
        "only weak or non-fetch signals: " + ", ".join(sorted({o.signal.value for o in live})),
    )


def age_verdict(dates: JobDates, today: date, max_age_days: int | None
                ) -> Literal["within_window", "stale", "posting_date_unknown"]:
    """A job cannot be posted after it was updated, so an old update date proves
    an old posting; a recent update date alone proves nothing about the posting."""
    if max_age_days is None:
        return "within_window"
    cutoff = today - timedelta(days=max_age_days)
    if dates.posted_at is not None:
        return "stale" if dates.posted_at < cutoff else "within_window"
    if dates.updated_at is not None and dates.updated_at < cutoff:
        return "stale"
    return "posting_date_unknown"
