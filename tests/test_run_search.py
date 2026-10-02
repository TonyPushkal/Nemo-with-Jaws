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
    args = ["--profile", str(tmp_path / "p.md"), "--lookback", "7d", "--location", "Bengaluru,Karnataka,India",
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
