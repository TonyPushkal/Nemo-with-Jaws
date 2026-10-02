import importlib.util
import io
import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from nemo.providers.base import ProviderError
from nemo.providers.serpapi import SerpApi, redact

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cmp", ROOT / "scripts" / "compare_serpapi.py")
cmp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cmp)

META = {"id": "x", "status": "Success", "created_at": "2026-09-30 16:00:00 UTC", "processed_at": "2026-09-30 16:00:01 UTC",
        "google_url": "https://www.google.com/search?q=x", "json_endpoint": "https://serpapi.com/searches/x.json?api_key=SECRET"}


class Opener:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc, self.reqs = payload, exc, []

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        if self.exc:
            raise self.exc
        return io.BytesIO(json.dumps(self.payload).encode())


def test_request_and_key_redaction():
    op = Opener({"search_metadata": META, "organic_results": []})
    raw = SerpApi("SECRET", usd_per_search=0.025, opener=op).search_raw("google", {"q": 'site:linkedin.com/jobs/view "x"', "gl": "in"})
    url = op.reqs[0].full_url
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith("https://serpapi.com/search.json?") and qs["engine"] == ["google"]
    assert qs["q"] == ['site:linkedin.com/jobs/view "x"'] and qs["gl"] == ["in"]
    assert "SECRET" not in json.dumps(raw)
    assert redact({"api_key": "k", "a": ["k in text"]}, "k") == {"a": ["REDACTED in text"]}


def test_errors_are_documented_as_not_charged_and_no_results_is_empty():
    with pytest.raises(ProviderError) as e:
        SerpApi("k", usd_per_search=0.025, opener=Opener(exc=urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO()))).search_raw("google", {})
    assert e.value.kind == "unauthorized" and e.value.charged is False
    with pytest.raises(ProviderError) as e:
        SerpApi("k", usd_per_search=0.025, opener=Opener({"error": "Invalid API key"})).search_raw("google", {})
    assert e.value.kind == "api_error"
    ok = SerpApi("k", usd_per_search=0.025, opener=Opener({"error": "Google hasn't returned any results for this query."}))
    assert ok.search_raw("google", {})["error"]
    with pytest.raises(ValueError):
        SerpApi("k", usd_per_search=0)


def test_plan_interleaves_engines_and_uses_site_operator_only_for_google():
    steps = cmp.plan(("Platform Engineer", "DevOps Engineer"))
    assert [s[0] for s in steps] == ["google", "google_jobs", "google", "google_jobs"]
    assert steps[0][2]["q"] == 'site:linkedin.com/jobs/view "Platform Engineer" (Bengaluru OR Hyderabad OR Visakhapatnam)'
    assert steps[1][2]["q"] == "Platform Engineer India"


def test_plan_jobs_only_with_explicit_location_drops_india_from_query():
    steps = cmp.plan(("Platform Engineer", "DevOps Engineer"), jobs_only=True, location="Bengaluru,Karnataka,India")
    assert [s[0] for s in steps] == ["google_jobs", "google_jobs"]
    assert steps[0][2] == {"q": "Platform Engineer", "location": "Bengaluru,Karnataka,India", "gl": "in", "hl": "en"}


def test_google_keeps_only_individual_postings_and_separates_dates():
    raw = {"search_metadata": META, "organic_results": [
        {"link": "https://in.linkedin.com/jobs/view/platform-engineer-at-acme-4300000001", "title": "Platform Engineer - Acme",
         "snippet": "Posted 3 days ago. Build Kubernetes platforms.", "date": "3 days ago"},
        {"link": "https://in.linkedin.com/jobs/platform-engineer-jobs-bengaluru", "title": "listing"},
        {"link": "https://www.linkedin.com/company/acme", "title": "company"}]}
    items, dropped = cmp.from_google(raw, "Platform Engineer")
    assert dropped == 2 and len(items) == 1
    it = items[0]
    assert it["url"] == "https://www.linkedin.com/jobs/view/4300000001"
    assert it["search_engine_date"] == {"source": "google.organic_results.date", "raw": "3 days ago"}
    assert it["aggregator_posted_at"] is None and it["verified_source_date"] == []
    assert it["returned_text_mentions"][0]["raw"] == "3 days ago" and it["request_time"] == META["processed_at"]


def test_jobs_keeps_only_linkedin_apply_links_and_labels_aggregator_time():
    raw = {"search_metadata": META, "jobs_results": [
        {"title": "DevOps Engineer", "company_name": "Acme", "location": "Hyderabad", "via": "LinkedIn",
         "description": "x" * 600, "extensions": ["2 days ago", "Full-time"], "detected_extensions": {"posted_at": "2 days ago"},
         "apply_options": [{"title": "Acme careers", "link": "https://acme.example/jobs/1"},
                           {"title": "LinkedIn", "link": "https://in.linkedin.com/jobs/view/devops-engineer-at-acme-4300000002?utm_source=g"}]},
        {"title": "Other", "apply_options": [{"title": "Indeed", "link": "https://in.indeed.com/viewjob?jk=1"}],
         "detected_extensions": {"posted_at": "1 day ago"}}]}
    items, dropped = cmp.from_jobs(raw, "DevOps Engineer")
    assert dropped == 1 and len(items) == 1
    a = items[0]["aggregator_posted_at"]
    assert a["raw"] == "2 days ago" and a["status"] == "aggregator_reported_unverified"
    assert a["raw_extensions"] == ["2 days ago", "Full-time"] and a["request_time"] == META["processed_at"]
    assert items[0]["verified_source_date"] == [] and items[0]["search_engine_date"] is None
    s = cmp.summarize(items, dropped, 1, 0)
    assert s["aggregator_posted_at"] == 1 and s["verified_source_date"] == 0 and s["usable_description(>=500 chars)"] == 1


def test_verified_requires_structured_absolute_with_offset():
    ms = cmp.mentions('"datePosted": "2026-09-29T10:00:00Z" posted 2026-09-28 and 2 days ago', "t")
    assert [m["raw"] for m in cmp.verified(ms)] == ['"datePosted": "2026-09-29T10:00:00Z"']


def test_dry_run_and_refusals(monkeypatch, capsys, tmp_path):
    prof = tmp_path / "p.md"
    prof.write_text("## Experience\nx\n## Desired roles\n- A\n- B\n## Must-haves\n## Nice-to-haves\n## Exclusions\n")
    base = ["--profile", str(prof), "--env-file", str(tmp_path / "none"), "--db", str(tmp_path / "l.sqlite")]
    assert cmp.main(base + ["--dry-run"]) == 0 and "4 searches" in capsys.readouterr().out
    for v in ("SERPAPI_API_KEY", "NEMO_PAID_CALLS_ENABLED", "NEMO_MAX_USD_PER_RUN"):
        monkeypatch.delenv(v, raising=False)
    assert cmp.main(base) == 2
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    assert cmp.main(base) == 2
    assert not (tmp_path / "l.sqlite").exists()


def test_aggregator_time_falls_back_to_extensions_and_is_labelled():
    j = {"extensions": ["1 day ago", "Full\u2013time"], "apply_options": [
        {"title": "LinkedIn", "link": "https://in.linkedin.com/jobs/view/x-at-y-4471270529?utm_campaign=google_jobs_apply"}],
         "description": "d" * 600}
    items, _ = cmp.from_jobs({"search_metadata": META, "jobs_results": [j]}, "r")
    a = items[0]["aggregator_posted_at"]
    assert a == {"source": "google_jobs.extensions", "raw": "1 day ago", "raw_extensions": ["1 day ago", "Full\u2013time"],
                 "request_time": META["processed_at"], "status": "aggregator_reported_unverified"}
    assert items[0]["url"] == "https://www.linkedin.com/jobs/view/4471270529" and items[0]["via_apply_title"] == "LinkedIn"
    assert cmp.aggregator_time({"extensions": ["Full\u2013time", "Health insurance"]}, None) is None
