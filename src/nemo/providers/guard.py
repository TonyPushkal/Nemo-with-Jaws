"""Wrappers that put every provider attempt through the budget / monetary guard.

Retry logic must wrap these (call the guarded provider once per attempt) so each
attempt is counted and reserved separately.
"""

from __future__ import annotations

from typing import Any

from nemo.budget import Budget
from nemo.providers.base import FetchResult, Hit, LLMProvider, LLMResult, PageFetcher, SearchProvider


class GuardedSearch:
    def __init__(self, inner: SearchProvider, budget: Budget):
        self.inner, self.budget = inner, budget

    def search(self, query: str, *, max_results: int = 10) -> list[Hit]:
        with self.budget.call("queries", self.inner.name, is_paid=self.inner.is_paid,
                              max_cost_usd=self.inner.max_cost_usd) as res:
            hits = self.inner.search(query, max_results=max_results)
            actual = getattr(self.inner, "last_call_cost_usd", None)
            if actual is not None:
                res.set_actual_usd(actual)
            return hits


class GuardedFetcher:
    def __init__(self, inner: PageFetcher, budget: Budget):
        self.inner, self.budget = inner, budget

    def fetch(self, url: str) -> FetchResult:
        with self.budget.call("pages", self.inner.name, is_paid=self.inner.is_paid,
                              max_cost_usd=self.inner.max_cost_usd) as res:
            result = self.inner.fetch(url)
            actual = getattr(self.inner, "last_call_cost_usd", None)
            if actual is not None:
                res.set_actual_usd(actual)
            return result


class GuardedLLM:
    def __init__(self, inner: LLMProvider, budget: Budget):
        self.inner, self.budget = inner, budget

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any]) -> LLMResult:
        with self.budget.call("llm_calls", self.inner.name, is_paid=self.inner.is_paid,
                              max_cost_usd=self.inner.max_cost_usd(system, prompt)) as res:
            result = self.inner.complete_json(system, prompt, schema)
            if result.cost_usd:
                res.set_actual_usd(result.cost_usd)
            return result
