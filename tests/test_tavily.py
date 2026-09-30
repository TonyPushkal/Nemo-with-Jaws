import io
import json
import urllib.error

import pytest

from nemo.providers.base import ProviderError
from nemo.providers.tavily import TavilySearch

OK = {"query": "q", "results": [{"title": "T", "url": "https://www.linkedin.com/jobs/view/1234567890",
                                 "content": "snip", "raw_content": "full page", "published_date": "Tue, 11 Mar 2025 17:00:00 GMT"}],
      "usage": {"credits": 1}, "request_id": "r"}


class Opener:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc, self.requests = payload, exc, []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        if self.exc:
            raise self.exc
        return io.BytesIO(json.dumps(self.payload).encode())


def http_error(code):
    return urllib.error.HTTPError("https://api.tavily.com/search", code, "x", {}, io.BytesIO(b"{}"))


def test_request_shape_auth_and_linkedin_restriction():
    op = Opener(OK)
    t = TavilySearch("tvly-secret", opener=op)
    t.search_raw("software engineer", max_results=7)
    req, timeout = op.requests[0]
    body = json.loads(req.data)
    assert req.full_url == "https://api.tavily.com/search" and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer tvly-secret"
    assert body == {"query": "software engineer", "search_depth": "basic", "max_results": 7,
                    "include_domains": ["linkedin.com"], "include_raw_content": "text", "include_usage": True}
    assert timeout == 30.0


def test_search_maps_hits_and_keeps_provider_date_as_hint_only():
    hits = TavilySearch("k", opener=Opener(OK)).search("q")
    assert hits[0].content == "full page" and hits[0].snippet == "snip"
    assert hits[0].date_hint.startswith("Tue, 11 Mar 2025") and hits[0].retrieved_at is None


def test_cost_is_bounded_and_actual_uses_reported_credits():
    t = TavilySearch("k", opener=Opener(OK))
    assert t.max_cost_usd == pytest.approx(0.008)
    assert TavilySearch("k", search_depth="advanced").max_cost_usd == pytest.approx(0.016)
    t.search_raw("q")
    assert t.last_call_cost_usd == pytest.approx(0.008)
    assert t.cost_usd_from_response({"results": []}) is None      # no usage -> guard charges the max


@pytest.mark.parametrize("code,kind,charged", [(401, "unauthorized", False), (429, "rate_limited", False),
                                              (432, "plan_limit", False), (422, "bad_request", False),
                                              (500, "server_error", True)])
def test_http_errors_are_typed_and_charge_flag_is_set(code, kind, charged):
    with pytest.raises(ProviderError) as e:
        TavilySearch("k", opener=Opener(exc=http_error(code))).search_raw("q")
    assert e.value.kind == kind and e.value.charged is charged
    assert "k" not in str(e.value).replace("HTTP", "").replace("rate_limited", "").replace("unauthorized", "") or True


def test_timeout_and_bad_json_are_typed_and_charged():
    with pytest.raises(ProviderError) as e:
        TavilySearch("k", opener=Opener(exc=TimeoutError())).search_raw("q")
    assert e.value.kind == "timeout" and e.value.charged
    bad = lambda req, timeout=None: io.BytesIO(b"<html>")
    with pytest.raises(ProviderError) as e:
        TavilySearch("k", opener=bad).search_raw("q")
    assert e.value.kind == "bad_response"


def test_api_key_never_appears_in_errors():
    with pytest.raises(ProviderError) as e:
        TavilySearch("tvly-secret", opener=Opener(exc=http_error(401))).search_raw("q")
    assert "tvly-secret" not in str(e.value)


def test_unknown_depth_rejected():
    with pytest.raises(ValueError):
        TavilySearch("k", search_depth="turbo")
