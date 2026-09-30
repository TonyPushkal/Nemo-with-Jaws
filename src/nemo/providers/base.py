"""Replaceable provider interfaces. No vendor is named here.

Any implementation (hosted API, self-hosted model behind an HTTP endpoint,
offline fake) satisfies these protocols. `is_paid` drives the paid-call guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class ProviderError(Exception):
    """Typed provider failure; `kind` feeds the run report (rate_limited, timeout, ...)."""

    def __init__(self, kind: str, message: str = "", *, charged: bool = True):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind
        # Failed requests are treated as potentially charged. Pass charged=False ONLY when
        # the provider's documentation states the failure is not billed.
        self.charged = charged


@dataclass(frozen=True)
class Hit:
    """One search result. `date_hint` is provider-supplied and may be a posting
    date, an update date or a crawl date, so it is never treated as a posting date."""

    url: str
    title: str
    snippet: str
    date_hint: str | None = None          # provider date: NEVER posting-time evidence
    content: str | None = None            # page content if the provider returns it
    retrieved_at: datetime | None = None  # only if the provider states when it fetched the page


class FetchStatus(str, Enum):
    OK = "ok"
    NOT_FOUND = "not_found"          # HTTP 404
    GONE = "gone"                    # HTTP 410
    BLOCKED = "blocked"              # 401/403 or anti-bot
    ROBOTS_DISALLOWED = "robots_disallowed"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: FetchStatus
    final_url: str | None = None
    http_status: int | None = None
    text: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class LLMResult:
    data: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)  # provider-specific timings/diagnostics


@runtime_checkable
class SearchProvider(Protocol):
    name: str
    is_paid: bool
    max_cost_usd: float  # worst-case cost of ONE call; paid providers must be > 0

    def search(self, query: str, *, max_results: int = 10) -> list[Hit]: ...


@runtime_checkable
class PageFetcher(Protocol):
    name: str
    is_paid: bool
    max_cost_usd: float

    def fetch(self, url: str) -> FetchResult: ...


@runtime_checkable
class LLMProvider(Protocol):
    """Structured-output completion. May be hosted or a self-hosted model behind
    an OpenAI-compatible endpoint; callers must not assume schema support and
    must validate `data` themselves."""

    name: str
    is_paid: bool
    def max_cost_usd(self, system: str, prompt: str) -> float:
        """Upper bound for one call (input + max output tokens at list price). 0 if free/self-hosted."""

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any]) -> LLMResult: ...


@dataclass
class CallLog:
    """Optional record kept by fakes/wrappers for tests and run reports."""

    calls: list[str] = field(default_factory=list)
