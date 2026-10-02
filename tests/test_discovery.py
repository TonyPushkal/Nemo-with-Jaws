from datetime import datetime, timedelta, timezone

import pytest

from nemo.discovery import (_split_google_title, estimate_interval, lookback_status, merge, parse_google,
                            parse_google_jobs, parse_lookback, plan)
from nemo.store import Store

RT = "2026-09-30 15:54:27 UTC"
T = datetime(2026, 9, 30, 15, 54, 27, tzinfo=timezone.utc)
WIN = (T - timedelta(days=7), T)
LI = "https://in.linkedin.com/jobs/view/{}?utm_campaign=google_jobs_apply"


def job(title, company, jid_slug, via="LinkedIn", ext=("2 days ago", "Full–time"), desc="d" * 800, loc="Bengaluru, Karnataka"):
    return {"title": title, "company_name": company, "location": loc, "via": via, "description": desc,
            "extensions": list(ext), "apply_options": [{"title": "X", "link": "https://x.example/1"},
                                                       {"title": "LinkedIn", "link": LI.format(jid_slug)}]}


def jobs_raw(*js):
    return {"search_metadata": {"processed_at": RT},
            "search_parameters": {"engine": "google_jobs", "q": "DevOps Engineer", "location": "Bengaluru,Karnataka,India"},
            "jobs_results": list(js)}


def test_parse_lookback():
    assert parse_lookback("24h") == timedelta(hours=24) and parse_lookback("7d") == timedelta(days=7)
    assert parse_lookback(" 2W ") == timedelta(weeks=2)
    for bad in ("7", "0h", "31d", "5w", "1y", ""):
        with pytest.raises(ValueError):
            parse_lookback(bad)


def test_plan_is_tiered_so_partial_budget_covers_every_role():
    locs = ["Bengaluru,Karnataka,India", "Hyderabad,Telangana,India"]
    steps = plan(("A", "B"), locs, timedelta(days=7), "in")
    assert [(s.engine, s.role, s.params.get("location")) for s in steps] == [
        ("google_jobs", "A", locs[0]), ("google_jobs", "B", locs[0]), ("google", "A", None), ("google", "B", None),
        ("google_jobs", "A", locs[1]), ("google_jobs", "B", locs[1])]
    assert steps[0].params == {"q": "A", "location": locs[0], "hl": "en", "gl": "in"}
    assert steps[2].params["q"] == 'site:linkedin.com/jobs/view "A" (Bengaluru OR Hyderabad)'
    assert steps[2].params["tbs"] == "qdr:w" and plan(("A",), [], timedelta(hours=24), None)[0].params["tbs"] == "qdr:d"
    assert [s.engine for s in plan(("A",), [], timedelta(days=7), None)] == ["google"]


def test_estimate_interval_and_status():
    e, l, _ = estimate_interval("2 days ago", T)
    assert (e, l) == (T - timedelta(days=3), T - timedelta(days=1))
    assert lookback_status(e, l, *WIN) == "reported_within"
    assert lookback_status(*estimate_interval("10 days ago", T)[:2], *WIN) == "reported_outside"
    assert lookback_status(*estimate_interval("7 days ago", T)[:2], *WIN) == "ambiguous"
    e, l, note = estimate_interval("30+ days ago", T)
    assert e is None and lookback_status(e, l, *WIN) == "reported_outside"
    e, l, note = estimate_interval("29 Aug 2026", T)
    assert "time zone unknown" in note and l - e == timedelta(hours=50)
    assert estimate_interval("2 days ago", None)[:2] == (None, None)
    assert lookback_status(None, None, *WIN) == "unknown"
    day = (T - timedelta(hours=24), T)
    assert lookback_status(*estimate_interval("1 day ago", T)[:2], *day) == "ambiguous"   # 2-day interval > 24h window


def test_google_jobs_keeps_linkedin_excludes_clear_mismatch_flags_others():
    kept, excluded, not_li = parse_google_jobs(jobs_raw(
        job("DevOps Engineer", "Acme", "devops-engineer-at-acme-4470000001"),
        job("AI Ops Engineer", "Circana", "ai-ops-engineer-at-skit-ai-4344432903", via="Careers At Circana"),
        job("DevOps Engineer", "Beta", "devops-engineer-at-beta-4470000002", desc="Based in Pune. " + "d" * 600),
        {"title": "No LinkedIn", "apply_options": [{"link": "https://indeed.example/1"}]}), WIN)
    assert not_li == 1 and [e["linkedin_job_id"] for e in excluded] == ["4344432903"]
    assert [k.job_id for k in kept] == ["4470000001", "4470000002"]
    assert kept[1].flags and "location mismatch" in kept[1].flags[0]       # flagged, not dropped
    r = kept[0].date_reports[0]
    assert r["raw"] == "2 days ago" and r["source"] == "google_jobs.extensions" and r["basis"] == "aggregator_reported"
    assert r["verified"] is False and r["lookback_status"] == "reported_within"


def test_google_search_title_split_and_parse():
    assert _split_google_title("SymphonyAI hiring Senior Platform Engineer in Bengaluru ...") == ("Senior Platform Engineer", "SymphonyAI")
    assert _split_google_title("Platform Engineer II at Entain India - Jobs") == ("Platform Engineer II", "Entain India")
    raw = {"search_metadata": {"processed_at": RT}, "search_parameters": {"engine": "google", "q": "site:..."},
           "organic_results": [{"link": LI.format("platform-engineer-at-entain-4458161268"), "title": "Platform Engineer II at Entain India - Jobs",
                                "snippet": "short", "date": "29 Aug 2026"},
                               {"link": "https://in.linkedin.com/jobs/platform-engineer-jobs", "title": "listing"}]}
    kept, other = parse_google(raw, WIN)
    assert other == 1 and kept[0].company == "Entain India" and kept[0].date_reports[0]["basis"] == "search_engine_date"
    assert kept[0].date_reports[0]["lookback_status"] == "reported_outside"


def test_merge_dedupes_by_job_id_and_prefers_linkedin_aggregator_time():
    a, _, _ = parse_google_jobs(jobs_raw(job("DevOps Engineer", "Acme", "devops-engineer-at-acme-4470000001", desc="long " * 300)), WIN)
    g, _ = parse_google({"search_metadata": {"processed_at": RT}, "search_parameters": {"engine": "google", "q": "s"},
                         "organic_results": [{"link": LI.format("devops-engineer-at-acme-4470000001"), "title": "Acme hiring DevOps Engineer in Bengaluru",
                                              "snippet": "short", "date": "20 days ago"}]}, WIN)
    jobs = merge(g + a)
    assert list(jobs) == ["4470000001"]
    j = jobs["4470000001"]
    assert len(j["sources"]) == 2 and len(j["reported_ages"]) == 2 and j["description_level"] == "description"
    assert j["best_reported_age"]["basis"] == "aggregator_reported" and j["lookback_status"] == "reported_within"
    assert j["url"] == "https://www.linkedin.com/jobs/view/4470000001"


def test_store_first_last_seen_and_history_does_not_hide(tmp_path):
    s = Store(tmp_path / "h.sqlite")
    a, _, _ = parse_google_jobs(jobs_raw(job("DevOps Engineer", "Acme", "devops-engineer-at-acme-4470000001")), WIN)
    j1 = merge(a)
    s.start_run("r1", "t1")
    s.upsert_jobs("r1", "2026-09-30T10:00:00+00:00", j1)
    assert j1["4470000001"]["previously_seen"] is False
    j2 = merge(a)
    s.start_run("r2", "t2")
    s.upsert_jobs("r2", "2026-10-01T10:00:00+00:00", j2)
    j = j2["4470000001"]
    assert j["previously_seen"] is True and j["first_seen_at"].startswith("2026-09-30") and j["last_seen_at"].startswith("2026-10-01")
    assert s.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert s.db.execute("SELECT count(*) FROM run_jobs").fetchone()[0] == 2
