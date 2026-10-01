"""SerpApi adapter (candidate provider for a comparison probe only).

Endpoint and parameters checked against serpapi.com on 2026-09-30:
`GET https://serpapi.com/search.json` with `engine` (`google` or `google_jobs`), `q`, `gl`, `hl`,
`api_key`; Google Search documents `site:`/`inurl:` operators in `q`. Billing: serpapi.com/pricing
states "Only successful searches are counted toward your monthly searches. Cached, errored, and
failed searches are not." — so errors here are `charged=False` (the one documented exception to
"failed requests are potentially charged"). The per-search USD price depends on your plan and is
passed in; the guard needs it to be positive. Only SerpApi is contacted; never linkedin.com.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from nemo.providers.base import ProviderError

URL = "https://serpapi.com/search.json"
NO_RESULTS = "hasn't returned any results"


def redact(obj: Any, secret: str) -> Any:
    """Remove the API key from anything we store (SerpApi metadata can echo request URLs)."""
    if isinstance(obj, dict):
        return {k: redact(v, secret) for k, v in obj.items() if k != "api_key"}
    if isinstance(obj, list):
        return [redact(v, secret) for v in obj]
    if isinstance(obj, str) and secret and secret in obj:
        return obj.replace(secret, "REDACTED")
    return obj


class SerpApi:
    name = "serpapi"
    is_paid = True

    def __init__(self, api_key: str, *, usd_per_search: float, timeout: float = 60.0,
                 opener: Callable[..., Any] | None = None):
        if usd_per_search <= 0:
            raise ValueError("usd_per_search must be positive (worst-case price per search on your plan)")
        self._key, self.max_cost_usd, self._timeout = api_key, float(usd_per_search), timeout
        self._open = opener or urllib.request.urlopen

    def search_raw(self, engine: str, params: dict[str, str]) -> dict[str, Any]:
        raw = self.get_json(URL, {"engine": engine, **params})
        err = raw.get("error")
        if err and NO_RESULTS not in str(err):
            raise ProviderError("api_error", str(err)[:200], charged=False)
        return raw

    def get_json(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """GET a SerpApi JSON endpoint (search, archive `/searches/<id>.json`, or `/account.json`)."""
        query = urllib.parse.urlencode({**(params or {}), "api_key": self._key})
        req = urllib.request.Request(f"{url}?{query}", method="GET")
        try:
            with self._open(req, timeout=self._timeout) as resp:
                raw = json.load(resp)
        except urllib.error.HTTPError as exc:
            kind = {401: "unauthorized", 429: "rate_limited"}.get(exc.code, "server_error" if exc.code >= 500 else "http_error")
            raise ProviderError(kind, f"HTTP {exc.code}", charged=False) from None
        except (TimeoutError, socket.timeout):
            raise ProviderError("timeout", "request timed out", charged=False) from None
        except urllib.error.URLError as exc:
            raise ProviderError("connection_error", str(exc.reason), charged=False) from None
        except json.JSONDecodeError:
            raise ProviderError("bad_response", "response was not JSON", charged=False) from None
        return redact(raw, self._key)

    def account(self) -> dict[str, Any]:
        """Free per serpapi.com/account-api: "will not be counted toward your monthly quota"."""
        return self.get_json("https://serpapi.com/account.json")

    def archived(self, search_id: str) -> dict[str, Any]:
        """Archived search JSON (kept 31 days). Credit use is NOT documented; measure with account()."""
        if not search_id.isalnum():
            raise ValueError("bad search id")
        return self.get_json(f"https://serpapi.com/searches/{search_id}.json")
