"""Tavily search adapter (candidate provider; selection is undecided).

Endpoint, Bearer auth, request fields and error codes were checked against
docs.tavily.com on 2026-09-30. Always treated as a paid provider: the
worst-case cost per call uses the highest listed pay-as-you-go rate per credit.
Only Tavily's own API is contacted; LinkedIn is never requested by this code.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable

from nemo.providers.base import Hit, ProviderError

URL = "https://api.tavily.com/search"
CREDITS_PER_CALL = {"basic": 1, "fast": 1, "ultra-fast": 1, "advanced": 2}
# 400/401/422/429/432/433 are rejections before search work; 5xx/timeouts may still bill.
_NOT_CHARGED = {400, 401, 422, 429, 432, 433}
_KIND = {400: "bad_request", 401: "unauthorized", 422: "bad_request", 429: "rate_limited",
         432: "plan_limit", 433: "plan_limit"}


class TavilySearch:
    name = "tavily"
    is_paid = True

    def __init__(self, api_key: str, *, search_depth: str = "basic",
                 include_domains: tuple[str, ...] = ("linkedin.com",),
                 include_raw_content: str | bool = "text", credit_usd: str = "0.008",
                 timeout: float = 30.0, opener: Callable[..., Any] | None = None):
        if search_depth not in CREDITS_PER_CALL:
            raise ValueError(f"unknown search_depth {search_depth!r}")
        self._key, self.search_depth = api_key, search_depth
        self.include_domains, self.include_raw_content = include_domains, include_raw_content
        self._credit_usd, self._timeout = float(credit_usd), timeout
        self._open = opener or urllib.request.urlopen
        self.last_call_cost_usd: float | None = None

    @property
    def max_cost_usd(self) -> float:
        return CREDITS_PER_CALL[self.search_depth] * self._credit_usd

    def request_body(self, query: str, max_results: int) -> dict[str, Any]:
        return {"query": query, "search_depth": self.search_depth, "max_results": max_results,
                "include_domains": list(self.include_domains),
                "include_raw_content": self.include_raw_content, "include_usage": True}

    def search_raw(self, query: str, *, max_results: int = 10) -> dict[str, Any]:
        req = urllib.request.Request(
            URL, data=json.dumps(self.request_body(query, max_results)).encode(), method="POST",
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        self.last_call_cost_usd = None
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise ProviderError(_KIND.get(exc.code, "server_error" if exc.code >= 500 else "http_error"),
                                f"HTTP {exc.code}", charged=exc.code not in _NOT_CHARGED) from None
        except (TimeoutError, socket.timeout):
            raise ProviderError("timeout", "request timed out") from None
        except urllib.error.URLError as exc:
            kind = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "connection_error"
            raise ProviderError(kind, str(exc.reason)) from None
        except json.JSONDecodeError:
            raise ProviderError("bad_response", "response was not JSON") from None
        self.last_call_cost_usd = self.cost_usd_from_response(raw)
        return raw

    def cost_usd_from_response(self, raw: dict[str, Any]) -> float | None:
        credits = (raw.get("usage") or {}).get("credits")
        return credits * self._credit_usd if isinstance(credits, (int, float)) else None

    def search(self, query: str, *, max_results: int = 10) -> list[Hit]:
        raw = self.search_raw(query, max_results=max_results)
        return [Hit(url=r.get("url", ""), title=r.get("title", ""), snippet=r.get("content", ""),
                    date_hint=r.get("published_date"), content=r.get("raw_content"))
                for r in raw.get("results", [])]
