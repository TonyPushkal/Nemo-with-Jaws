import csv
import importlib.util
import json
from pathlib import Path

import pytest

from nemo.providers.base import ProviderError
from nemo.providers.serpapi import SerpApi

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("run_search", ROOT / "scripts" / "run_search.py")
rs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rs)

PROFILE = """## Experience
Engineer.
## Desired roles
- DevOps Engineer
- Platform Engineer
## Must-haves
## Nice-to-haves
## Exclusions
"""
from datetime import datetime, timezone

RT = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fake_responses(engine, params):
    role = params["q"] if engine == "google_jobs" else params["q"].split('"')[1]
    slug = role.lower().replace(" ", "-")
    md = {"search_metadata": {"id": "abc", "processed_at": RT}, "search_parameters": {"engine": engine, **params}}
    if engine == "google_jobs":
        return {**md, "jobs_results": [
            {"title": role, "company_name": "Acme", "location": "Bengaluru, Karnataka", "via": "LinkedIn",
             "description": "x" * 900, "extensions": ["2 days ago"],
             "apply_options": [{"title": "LinkedIn", "link": f"https://in.linkedin.com/jobs/view/{slug}-at-acme-44700000{len(slug):02d}"}]},
            {"title": "Other", "company_name": "Circana", "via": "Careers",
             "apply_options": [{"link": "https://in.linkedin.com/jobs/view/ai-ops-engineer-at-skit-ai-4344432903"}]}]}
    return {**md, "organic_results": [
        {"link": f"https://in.linkedin.com/jobs/view/{slug}-at-acme-44700000{len(slug):02d}", "title": f"Acme hiring {role} in Bengaluru",
         "snippet": "short snippet"},
        {"link": "https://in.linkedin.com/jobs/view/some-other-at-zeta-4460000099", "title": "Zeta hiring SRE", "snippet": "s"}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "p.md").write_text(PROFILE)
    for v in ("SERPAPI_API_KEY", "NEMO_PAID_CALLS_ENABLED", "NEMO_MAX_USD_PER_RUN", "NEMO_MONTHLY_USD_CAP"):
        monkeypatch.delenv(v, raising=False)
    calls = []

    def fake(self, engine, params):
        calls.append((engine, params))
        return fake_responses(engine, params)
    monkeypatch.setattr(SerpApi, "search_raw", fake)
    args = ["--profile", str(tmp_path / "p.md"), "--lookback", "7d", "--plain-roles", "--location", "Bengaluru,Karnataka,India",
            "--gl", "in", "--env-file", str(tmp_path / ".env"), "--db", str(tmp_path / "d.sqlite"),
            "--out-dir", str(tmp_path / "runs"), "--yes"]
    return tmp_path, args, calls, monkeypatch


def enable(monkeypatch, usd="1.00"):
    monkeypatch.setenv("SERPAPI_API_KEY", "k")
    monkeypatch.setenv("NEMO_PAID_CALLS_ENABLED", "true")
    monkeypatch.setenv("NEMO_MAX_USD_PER_RUN", usd)


def test_full_run_collects_dedupes_saves_and_prints_paths(env, capsys):
    tmp, args, calls, mp = env
    enable(mp)
    assert rs.main(args) == 0
    out = capsys.readouterr().out
    assert len(calls) == 4 and "Status: complete" in out and "summary.md" in out and "results.csv" in out
    run = next((tmp / "runs").iterdir())
    res = json.loads((run / "results.json").read_text())
    ids = [j["linkedin_job_id"] for j in res["jobs"]]
    assert len(ids) == len(set(ids)) == 3                       # 2 roles deduped across engines + 1 extra from Google
    assert [e["linkedin_job_id"] for e in res["excluded_mismatches"]] == ["4344432903", "4344432903"]
    top = res["jobs"][0]
    assert top["best_reported_age"]["raw"] == "2 days ago" and top["best_reported_age"]["verified"] is False
    assert {s["engine"] for s in top["sources"]} == {"google_jobs", "google"} and top["first_seen_at"]
    rows = list(csv.DictReader(open(run / "results.csv")))
    assert len(rows) == 3 and rows[-1]["lookback_status"] == "unknown"    # kept, not filtered
    assert top["lookback_status"] == "reported_within"
    assert len(list((run / "raw").iterdir())) == 4
    # second run: same jobs are returned again and marked previously seen
    assert rs.main(args) == 0
    runs = list((tmp / "runs").iterdir())
    assert len(runs) == 2
    res2 = json.loads((next(r for r in runs if r != run) / "results.json").read_text())
    assert len(res2["jobs"]) == 3 and all(j["previously_seen"] for j in res2["jobs"])
    assert res2["summary"]["new_jobs"] == 0


def test_budget_stop_keeps_partial_results(env, capsys):
    tmp, args, calls, mp = env
    enable(mp, usd="0.05")                                        # 2 searches at 0.025
    assert rs.main(args) == 0
    out = capsys.readouterr().out
    assert len(calls) == 2 and "Status: partial (stopped by max_usd_per_run)" in out
    res = json.loads((next((tmp / "runs").iterdir()) / "results.json").read_text())
    assert res["summary"]["searches_not_run"] == 2 and len(res["jobs"]) == 2


def test_failed_search_is_reported_not_fatal(env, capsys, monkeypatch):
    tmp, args, calls, mp = env
    enable(mp)
    n = {"i": 0}

    def flaky(self, engine, params):
        n["i"] += 1
        if engine == "google":
            raise ProviderError("unauthorized", "HTTP 401", charged=False)
        return fake_responses(engine, params)
    monkeypatch.setattr(SerpApi, "search_raw", flaky)
    assert rs.main(args) == 0
    res = json.loads((next((tmp / "runs").iterdir()) / "results.json").read_text())
    assert res["status"] == "partial" and res["summary"]["searches_failed"] == 2 and len(res["jobs"]) == 2


def test_refusals_make_no_calls(env, capsys):
    tmp, args, calls, mp = env
    assert rs.main(args) == 2                                     # no key
    mp.setenv("SERPAPI_API_KEY", "k")
    assert rs.main(args) == 2                                     # paid calls disabled
    assert rs.main(args[:3] + ["bad"] + args[4:]) == 2            # bad lookback
    assert calls == [] and not (tmp / "runs").exists()


def test_dry_run_and_env_file_loading(env, capsys):
    tmp, args, calls, mp = env
    (tmp / ".env").write_text("SERPAPI_API_KEY=k\nNEMO_PAID_CALLS_ENABLED=true\nNEMO_MAX_USD_PER_RUN=0.10\n")
    assert rs.main(args + ["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "4 searches" in out and "ENABLED" in out and "up to 4 searches" in out and calls == []


def test_optional_assessment_failure_does_not_hide_jobs(env, capsys, monkeypatch):
    tmp, args, calls, mp = env
    enable(mp)
    from nemo.providers import ollama
    monkeypatch.setattr(ollama.OllamaLLM, "installed_models", lambda self: ["qwen3.5:4b"])

    def boom(self, *a, **k):
        raise ProviderError("timeout", "slow")
    monkeypatch.setattr(ollama.OllamaLLM, "complete_json", boom)
    assert rs.main(args + ["--assess"]) == 0
    res = json.loads((next((tmp / "runs").iterdir()) / "results.json").read_text())
    assert len(res["jobs"]) == 3 and res["summary"]["assessment"]["failed"] == 3


def test_default_dry_run_shows_order_locations_count_and_cap(env, capsys):
    tmp, args, calls, mp = env
    args = [a for a in args if a != "--plain-roles"] + ["--location", "Hyderabad,Telangana,India", "--dry-run", "--max-searches", "5"]
    (tmp / "p.md").write_text(PROFILE.replace("- DevOps Engineer\n- Platform Engineer", "- Backend Engineer\n- DevOps Engineer\n- SDET"))
    enable(mp)
    assert rs.main(args) == 0
    out = capsys.readouterr().out
    assert "Locations: Bengaluru, Hyderabad" in out and "fit within the configured cap: 5" in out and calls == []
    lines = [l for l in out.splitlines() if "[RUN]" in l or "[beyond cap]" in l]
    assert sum("[RUN]" in l for l in lines) == 5 and "[beyond cap]" in lines[5]
    assert {"backend", "devops_platform", "testing"} <= {l.split()[2] for l in lines[:3]}
    assert "@ Bengaluru" in lines[0] and "@ Hyderabad" in lines[1]


def test_run_keeps_a_job_with_only_an_employer_link(env, capsys, monkeypatch):
    tmp, args, calls, mp = env
    enable(mp)
    def fake(self, engine, params):
        return {"search_metadata": {"processed_at": RT}, "search_parameters": {"engine": engine, **params}, "jobs_results": [
            {"title": "Backend Engineer", "company_name": "Acme", "location": "Bengaluru, Karnataka", "via": "Acme Careers",
             "description": "x" * 900, "extensions": ["2 days ago"],
             "apply_options": [{"title": "Acme Careers", "link": "https://careers.acme.example/j/1?utm=1"}]}]}
    monkeypatch.setattr(SerpApi, "search_raw", fake)
    assert rs.main([a for a in args] + ["--max-searches", "1"]) == 0
    res = json.loads((next((tmp / "runs").iterdir()) / "results.json").read_text())
    j = res["jobs"][0]
    assert j["linkedin_job_id"] is None and j["job_key"].startswith("url:") and j["application_kind"] == "employer_ats"
    rows = list(csv.DictReader(open(next((tmp / "runs").iterdir()) / "results.csv")))
    assert rows[0]["application_url"].startswith("https://careers.acme.example") and rows[0]["description_source"] == "Acme Careers"


def test_offset_and_cap_select_the_next_batch_and_dry_run_shows_cap_estimate_and_next_offset(env, capsys):
    tmp, args, calls, mp = env
    base = [a for a in args if a != "--plain-roles"] + ["--location", "Hyderabad,Telangana,India", "--location",
                                                        "Visakhapatnam,Andhra Pradesh,India"]
    (tmp / "p.md").write_text(PROFILE.replace("- DevOps Engineer\n- Platform Engineer", "- Backend Engineer\n- DevOps Engineer\n- SDET\n- AI Platform Engineer"))
    enable(mp)
    assert rs.main(base + ["--dry-run", "--max-searches", "4"]) == 0
    first = capsys.readouterr().out
    assert "Request-count cap: 4 (--max-searches); offset 0; this batch runs plan items 1-4" in first
    assert "Estimated cost of this batch (ASSUMED $0.025/search, not verified billing): $0.100" in first
    assert "Next batch: --offset 4" in first and any("[RUN]" in l and "SDE I" in l for l in first.splitlines())
    assert rs.main(base + ["--dry-run", "--max-searches", "4"]) == 0
    assert capsys.readouterr().out == first                                    # same first queries every time
    assert rs.main(base + ["--dry-run", "--max-searches", "4", "--offset", "4"]) == 0
    second = capsys.readouterr().out
    assert "plan items 5-8" in second and "[before offset]" in second and "Next batch: --offset 8" in second
    # a real capped run executes exactly the selected window, numbered by plan position
    assert rs.main(base + ["--max-searches", "2", "--offset", "4"]) == 0
    assert [c[1]["q"] for c in calls] == [rs_plan_q(base, i) for i in (4, 5)]


def rs_plan_q(base, i):
    from datetime import timedelta
    from nemo.profile import parse_profile
    from nemo.queryplan import build_plan, load_config
    prof = parse_profile(Path(base[1]).read_text())
    locs = [base[j + 1] for j, a in enumerate(base) if a == "--location"]
    return build_plan(prof.desired_roles, locs, timedelta(days=7), "in", load_config())[i].params["q"]


def test_skipped_jobs_are_counted_and_screen_flag_writes_per_query_report(env, capsys, monkeypatch):
    tmp, args, calls, mp = env
    enable(mp)
    def fake(self, engine, params):
        return {"search_metadata": {"processed_at": RT}, "search_parameters": {"engine": engine, **params}, "jobs_results": [
            {"title": "Backend Engineer", "company_name": "Acme", "location": "Bengaluru, Karnataka", "via": "Acme Careers",
             "description": "Python backend services. " * 30, "extensions": ["2 days ago"],
             "apply_options": [{"title": "Acme Careers", "link": "https://careers.acme.example/j/1"}]},
            {"title": "Nameless", "company_name": "Ghost", "apply_options": []}]}      # no link, no Google job id
    monkeypatch.setattr(SerpApi, "search_raw", fake)
    assert rs.main(args + ["--max-searches", "1", "--screen"]) == 0
    out = capsys.readouterr().out
    assert "1 skipped (no identity/application link)" in out
    run = next((tmp / "runs").iterdir())
    assert json.loads((run / "results.json").read_text())["summary"]["skipped_no_identity"] == 1
    rep = (run / "report.md").read_text()
    assert "Skipped for missing identity/application link: **1**" in rep and "consistency-checked" in rep
    assert "| 1 | DevOps Engineer | Bengaluru | ok | 2 | 1 | 1 | 0 | 1 |" in rep and "careers.acme.example" in rep
    assert "No claim that relevance improved" in rep
    sc = json.loads((run / "screened.json").read_text())
    assert len(sc["queries"]) == 1 and sc["shortlist"][0]["application_kind"] == "employer_ats"


def test_queries_file_dry_run_shows_exact_requests_and_no_freshness(env, capsys, tmp_path):
    tmp, args, calls, mp = env
    q = tmp / "q.json"
    q.write_text(json.dumps([{"label": "t", "engine": "google_jobs", "q": "Software Development Engineer I", "location": "Bengaluru,Karnataka,India"},
                             {"label": "w", "engine": "google", "q": "site:linkedin.com/jobs/view/4473364096"}]))
    enable(mp)
    assert rs.main([a for a in args if a != "--plain-roles"] + ["--queries-file", str(q), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "planned requests in full plan: 2" in out and "site:linkedin.com/jobs/view/4473364096" in out
    assert "[freshness: none]" in out and calls == []


def test_diagnostic_check_finds_exact_id_and_lists_other_company_vacancies_separately(tmp_path, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("diag", ROOT / "scripts" / "diagnostic_check.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    run = tmp_path / "r"; (run / "raw").mkdir(parents=True)
    (run / "raw" / "01.json").write_text(json.dumps({"search_parameters": {"engine": "google_jobs", "q": "Safe SDE", "location_requested": "Bengaluru"}, "jobs_results": [
        {"title": "SDE I", "company_name": "Safe Security", "location": "Bengaluru", "via": "LinkedIn", "description": "d" * 600,
         "apply_options": [{"title": "LinkedIn", "link": "https://in.linkedin.com/jobs/view/sde-i-at-safe-security-4473364096"}]},
        {"title": "SDE II", "company_name": "Safe Security", "location": "Bengaluru", "via": "Greenhouse",
         "apply_options": [{"title": "Greenhouse", "link": "https://boards.greenhouse.io/safe/1"}]},
        {"title": "Other", "company_name": "Acme", "apply_options": []}]}))
    (run / "raw" / "02.json").write_text(json.dumps({"search_parameters": {"engine": "google", "q": "site:x"}, "organic_results": [
        {"title": "Unrelated", "link": "https://in.linkedin.com/jobs/view/foo-at-acme-4400000001", "snippet": "s"}]}))
    assert m.main([str(run), "--target-id", "4473364096", "--company", "safe"]) == 0
    rows = m.check(run, "4473364096", "safe")
    assert rows[0]["target"]["title"] == "SDE I" and [s["title"] for s in rows[0]["similar"]] == ["SDE II"]
    assert rows[1]["target"] is None and rows[1]["freshness"] == "none" and rows[1]["returned"] == 1
    assert "FOUND" in (run / "diagnostic.md").read_text() and "Evidence, not proof" in (run / "diagnostic.md").read_text()


def test_retries_count_against_the_request_cap(env, capsys, monkeypatch):
    tmp, args, calls, mp = env
    enable(mp)
    attempts = []

    def flaky(self, engine, params):
        attempts.append(params["q"])
        if len(attempts) == 1:
            raise ProviderError("timeout", "slow", charged=False)       # first attempt fails, one retry is allowed
        return fake_responses(engine, params)
    monkeypatch.setattr(SerpApi, "search_raw", flaky)
    assert rs.main(args + ["--max-searches", "2"]) == 0                  # plain-roles plan: 4 searches, cap 2
    out = capsys.readouterr().out
    assert len(attempts) == 2 and "stopped by max_queries" in out         # attempt 1 + its retry = the whole cap


def test_dry_run_affordable_count_is_exact_at_a_ceiling_of_0_30(env, capsys):
    tmp, args, calls, mp = env
    enable(mp, usd="0.30")
    assert rs.main(args + ["--dry-run"]) == 0
    assert "up to 4 searches this run" in capsys.readouterr().out          # 4-search plan: all fit
    q = tmp / "q.json"
    q.write_text(json.dumps([{"engine": "google_jobs", "q": f"Role {i}", "location": "Bengaluru,Karnataka,India"} for i in range(13)]))
    assert rs.main([a for a in args if a != "--plain-roles"] + ["--queries-file", str(q), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "fit within the configured cap: 12" in out and "[beyond cap] testing" not in out and out.count("[RUN]") == 12


def test_default_locations_come_from_config_when_no_location_is_given(env, capsys):
    tmp, args, calls, mp = env
    enable(mp)
    a = [x for x in args if x != "--plain-roles"]
    i = a.index("--location"); del a[i:i + 2]                               # drop the explicit location
    assert rs.main(a + ["--dry-run", "--max-searches", "6"]) == 0
    out = capsys.readouterr().out
    assert "Locations: Bengaluru, Hyderabad;" in out and "Visakhapatnam" not in out and calls == []
