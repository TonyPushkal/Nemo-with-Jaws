import json

import pytest

from nemo.budget import Budget, BudgetConfig, PaidCallsDisabled
from nemo.ledger import InMemoryLedger
from nemo.probe import FIXED_QUERIES, analyze_result, build_query, render_markdown, run_probe, scan_time_mentions, summarize
from nemo.providers.base import ProviderError

JOB = "https://www.linkedin.com/jobs/view/widget-engineer-at-acme-3812345678"


def result(url=JOB, raw="", content="snip", **extra):
    return {"url": url, "title": "T", "content": content, "raw_content": raw, **extra}


def test_scan_finds_kinds_and_qualifiers():
    text = ('Widget Engineer. Posted 3 days ago. Reposted 1 week ago. Updated 2025-01-05. '
            'Posted on March 3, 2026. {"datePosted": "2026-09-28T10:00:00Z"} Sign in to apply')
    kinds = {(m["kind"], m["qualifier"]) for m in scan_time_mentions(text)}
    assert ("relative", "posted") in kinds and ("relative", "reposted") in kinds
    assert ("iso_date", "updated") in kinds and ("long_date", "posted") in kinds
    assert ("structured_datePosted", None) in kinds
    iso_in_ld = [m for m in scan_time_mentions('{"datePosted": "2026-09-28"}') if m["kind"] == "iso_date"]
    assert iso_in_ld == []      # not double-counted


def test_analyze_flags_login_wall_similar_jobs_and_provider_date_is_only_recorded():
    a = analyze_result(result(raw="Sign in to see more. Similar jobs: Nurse 2 days ago", published_date="Tue, 11 Mar 2025 17:00:00 GMT"))
    assert a["job_id"] == "3812345678" and a["login_wall_text"] and a["similar_jobs_text"]
    assert a["provider_published_date"] and a["scanned_field"] == "raw_content"


def test_summary_buckets_and_non_job_urls():
    recs = [{"query": "q", "top_level_keys": ["results", "usage"], "credits": 1, "analysis": [
        analyze_result(result(raw='{"datePosted":"2026-09-28"}')),
        analyze_result(result(url="https://www.linkedin.com/jobs/view/1111111111", raw="Posted 2 days ago")),
        analyze_result(result(url="https://www.linkedin.com/jobs/view/2222222222", raw="Reposted 2 days ago")),
        analyze_result(result(url="https://www.linkedin.com/jobs/view/3333333333", raw="", content="no dates here")),
        analyze_result(result(url="https://www.linkedin.com/company/acme/jobs/", raw="x")),
    ]}]
    s = summarize(recs)
    assert s["job_view_urls"] == 4 and s["other_urls"] == 1
    assert s["job_evidence_buckets"] == {"absolute": 1, "absolute_candidate": 0, "relative_only": 1, "none": 2}
    assert s["job_with_only_nonposting_mentions"] == 1 and s["credits_reported"] == 1
    assert s["time_or_retrieval_like_keys"] == []


def test_time_like_response_keys_are_surfaced():
    recs = [{"query": "q", "top_level_keys": ["results", "crawled_at"], "analysis": [
        analyze_result(result(fetched_at="x"))]}]
    assert summarize(recs)["time_or_retrieval_like_keys"] == ["crawled_at", "fetched_at"]


class RawProvider:
    name, is_paid, max_cost_usd = "fake-raw", True, 0.01

    def __init__(self, fail_on=None, cost=0.01):
        self.calls, self.fail_on, self.cost = [], fail_on, cost

    def search_raw(self, query, *, max_results=10):
        self.calls.append(query)
        if query == self.fail_on:
            raise ProviderError("rate_limited", "HTTP 429", charged=False)
        return {"query": query, "results": [result(raw="Posted 2 hours ago")], "usage": {"credits": 1}}

    def cost_usd_from_response(self, raw):
        return self.cost


def paid(**kw):
    return Budget(BudgetConfig(paid_calls_enabled=True, max_usd_per_run="0.05", **kw), ledger=InMemoryLedger())


def test_run_probe_writes_files_and_continues_after_a_failed_query(tmp_path):
    p = RawProvider(fail_on=FIXED_QUERIES[1])
    s = run_probe(p, FIXED_QUERIES, paid(), tmp_path, meta={"provider": "fake-raw"})
    assert len(p.calls) == 5 and s["counts"]["queries_failed"] == 1 and s["counts"]["job_view_urls"] == 4
    assert json.loads((tmp_path / "summary.json").read_text())["counts"]["queries"] == 5
    assert (tmp_path / "raw" / "01.json").exists() and not (tmp_path / "raw" / "02.json").exists()
    md = (tmp_path / "summary.md").read_text()
    assert "FAILED" in md and "not a verdict" in md


def test_run_probe_refuses_paid_calls_without_budget(tmp_path):
    with pytest.raises(PaidCallsDisabled):
        run_probe(RawProvider(), FIXED_QUERIES, Budget(BudgetConfig()), tmp_path)
    assert list(tmp_path.iterdir()) == []      # a refused run leaves nothing behind


def test_run_probe_stops_at_cap_and_reports_it(tmp_path):
    p = RawProvider()
    s = run_probe(p, FIXED_QUERIES, Budget(BudgetConfig(max_queries=2, paid_calls_enabled=True, max_usd_per_run="1")), tmp_path)
    assert len(p.calls) == 2 and s["meta"]["stopped_by_cap"] == "max_queries"


def test_run_probe_stops_when_reservation_would_exceed_run_ceiling(tmp_path):
    p = RawProvider()      # 0.01 per call, ceiling 0.05 -> 5 calls fit exactly
    s = run_probe(p, FIXED_QUERIES + ["extra"], paid(), tmp_path)
    assert len(p.calls) == 5 and s["meta"]["stopped_by_cap"] == "max_usd_per_run"


def test_fixed_queries_are_generic_and_target_individual_postings():
    assert len(FIXED_QUERIES) == 5 and len(set(FIXED_QUERIES)) == 5
    assert all(q.startswith("site:linkedin.com/jobs/view/ ") for q in FIXED_QUERIES)
    assert [q.split("/ ", 1)[1] for q in FIXED_QUERIES] == ["software engineer", "registered nurse", "accountant",
                                                            "data analyst", "marketing manager"]
    assert build_query("nurse") == "site:linkedin.com/jobs/view/ nurse"
    assert build_query("site:linkedin.com/jobs/view/ nurse") == "site:linkedin.com/jobs/view/ nurse"


def test_render_is_stable():
    assert render_markdown({"meta": {}, "counts": summarize([]), "queries": []}).startswith("# Provider feasibility probe")
