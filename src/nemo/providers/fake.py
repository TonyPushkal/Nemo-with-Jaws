"""Offline fakes for tests and demos. They never touch the network."""

from __future__ import annotations

from typing import Any, Callable

from nemo.providers.base import FetchResult, FetchStatus, Hit, LLMResult, ProviderError


class FakeSearchProvider:
    def __init__(self, results: dict[str, list[Hit]] | None = None, *, is_paid: bool = False,
                 max_cost_usd: float = 0.0, fail_with: str | None = None, charged: bool = True,
                 actual_cost_usd: float | None = None):
        self.name = "fake-search"
        self.is_paid, self.max_cost_usd = is_paid, max_cost_usd
        self.results, self.fail_with, self.charged = results or {}, fail_with, charged
        self.last_call_cost_usd: float | None = None
        self._actual = actual_cost_usd
        self.queries: list[str] = []

    def search(self, query: str, *, max_results: int = 10) -> list[Hit]:
        self.queries.append(query)
        if self.fail_with:
            raise ProviderError(self.fail_with, "injected failure", charged=self.charged)
        self.last_call_cost_usd = self._actual
        return self.results.get(query, [])[:max_results]


class FakeFetcher:
    def __init__(self, pages: dict[str, FetchResult] | None = None, *, is_paid: bool = False,
                 max_cost_usd: float = 0.0):
        self.name = "fake-fetch"
        self.is_paid, self.max_cost_usd = is_paid, max_cost_usd
        self.pages = pages or {}
        self.urls: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        self.urls.append(url)
        return self.pages.get(url, FetchResult(url=url, status=FetchStatus.NOT_FOUND, http_status=404))


class FakeLLM:
    def __init__(self, respond: Callable[[str, str], dict[str, Any]] | None = None, *,
                 is_paid: bool = False, max_cost: float = 0.0, actual_cost_usd: float = 0.0):
        self.name = "fake-llm"
        self.is_paid, self._max_cost, self._actual = is_paid, max_cost, actual_cost_usd
        self.respond = respond or (lambda system, prompt: {})
        self.prompts: list[str] = []

    def max_cost_usd(self, system: str, prompt: str) -> float:
        return self._max_cost

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any]) -> LLMResult:
        self.prompts.append(prompt)
        return LLMResult(data=self.respond(system, prompt), cost_usd=self._actual)
