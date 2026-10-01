"""Bright Data adapter: request shape, error mapping, record mapping. All data is SYNTHETIC
(shaped after the documented schema; no live response has been captured yet)."""

import io
import json
import urllib.error
import urllib.parse

import pytest

from nemo.jobsource import CollectRequest
from nemo.providers.base import ProviderError
from nemo.providers.brightdata import BrightDataJobs, description_status

LONG = "Responsibilities: build and run services. " * 20


def synthetic(jid="4470000001", **kw):
    r = {"job_posting_id": jid, "url": f"https://in.linkedin.com/jobs/view/example-engineer-at-example-co-{jid}",
         "job_title": "Example Engineer", "company_name": "Example Co", "job_location": "Bengaluru, Karnataka, India",
         "job_description_formatted": LONG, "job_posted_date": "2026-09-30T08:00:00.000Z", "job_posted_time": "1 day ago"}
    r.update(kw)
    return r


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def opener_for(payload, seen=None, exc=None):
    def op(req, timeout):
        if seen is not None:
            seen.append(req)
        if exc:
            raise exc
        return Resp(json.dumps(payload).encode())
    return op


def test_launch_request_shape_and_no_secret_in_url():
    seen = []
    p = BrightDataJobs("SECRET", opener=opener_for({"snapshot_id": "sd_abc"}, seen))
    req = CollectRequest(roles=("A", "B"), locations=("India", "Pune, India"), max_records=10, time_range="Past week")
    assert p.launch(req) == "sd_abc"
    r = seen[0]
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(r.full_url).query)
    assert r.get_method() == "POST" and qs["type"] == ["discover_new"] and qs["discover_by"] == ["keyword"]
    assert "SECRET" not in r.full_url and r.get_header("Authorization") == "Bearer SECRET"
    body = json.loads(r.data)
    assert len(body["input"]) == 4 and body["limit_per_input"] == 3          # ceil(10/4)
    assert body["input"][0] == {"keyword": "A", "location": "India", "country": "IN", "time_range": "Past week"}


@pytest.mark.parametrize("code,kind", [(401, "unauthorized"), (400, "bad_request"), (429, "rate_limited"),
                                       (500, "server_error"), (404, "not_found")])
def test_http_errors_are_typed(code, kind):
    exc = urllib.error.HTTPError("u", code, "x", {}, None)
    with pytest.raises(ProviderError) as e:
        BrightDataJobs("k", opener=opener_for(None, exc=exc)).launch(CollectRequest(roles=("A",)))
    assert e.value.kind == kind


def test_timeout_is_typed_and_potentially_charged():
    with pytest.raises(ProviderError) as e:
        BrightDataJobs("k", opener=opener_for(None, exc=TimeoutError())).launch(CollectRequest(roles=("A",)))
    assert e.value.kind == "timeout" and e.value.charged is True


def test_launch_without_snapshot_id_is_an_error():
    with pytest.raises(ProviderError) as e:
        BrightDataJobs("k", opener=opener_for({"oops": 1})).launch(CollectRequest(roles=("A",)))
    assert e.value.kind == "bad_response"


def test_status_and_unknown_status():
    assert BrightDataJobs("k", opener=opener_for({"status": "running"})).status("sd_1")[0] == "running"
    with pytest.raises(ProviderError):
        BrightDataJobs("k", opener=opener_for({"status": "weird"})).status("sd_1")


def test_fetch_not_ready_object_raises_not_ready():
    with pytest.raises(ProviderError) as e:
        BrightDataJobs("k", opener=opener_for({"status": "building"})).fetch("sd_1")
    assert e.value.kind == "not_ready"


def test_normalize_full_record_keeps_raw_and_marks_unverified():
    j = BrightDataJobs("k").normalize(synthetic(), "T")
    assert (j.source, j.source_job_id, j.description_status, j.verification) == ("linkedin", "4470000001", "full", "provider_reported")
    assert j.raw["job_title"] == "Example Engineer" and j.posted_text == "1 day ago" and j.flags == []


def test_normalize_flags_missing_truncated_and_id_mismatch_and_skips_errors():
    p = BrightDataJobs("k")
    assert p.normalize(synthetic(job_description_formatted=""), "T").description_status == "missing"
    assert p.normalize(synthetic(job_description_formatted="Great role... "), "T").description_status == "truncated"
    j = p.normalize(synthetic(job_posting_id="4470000999"), "T")
    assert any(f.startswith("url_id_mismatch") for f in j.flags)
    assert p.normalize({"error": "dead_page", "error_code": "dead_page", "input": {}}, "T") is None
    assert description_status("short text") == "short"


def test_secret_redacted_from_fetched_payload():
    p = BrightDataJobs("SECRET", opener=opener_for([synthetic(job_title="x SECRET y")]))
    assert "SECRET" not in json.dumps(p.fetch("sd_1"))
