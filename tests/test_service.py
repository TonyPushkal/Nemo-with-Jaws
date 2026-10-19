"""Minimal collection service: validation, one-run-at-a-time, status polling, recovery. Offline; SerpApi is faked."""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nemo.profile import load_profile
from nemo.providers.serpapi import SerpApi
from nemo.service import Collector, RateLimiter, RequestError, build_argv, make_handler, serve

CONFIG = {"city_locations": {"Bengaluru": "Bengaluru,Karnataka,India", "Hyderabad": "Hyderabad,Telangana,India"}}
RT = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
PROFILE = "## Experience\nEarly.\n\n## Desired roles\n- Backend Engineer\n\n## Must-haves\n\n## Nice-to-haves\n\n## Exclusions\n"


def call(base, method, path, body=None, token=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def server(tmp_path, monkeypatch):
    started = []

    def start(collector, token=None, limiter=None):
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(collector, token, limiter))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"
    yield start
    for s in started:
        s.shutdown()


def test_request_validation_gives_actionable_errors_and_maps_cities_to_search_locations():
    ok = build_argv({"cities": ["bengaluru", "Hyderabad"], "titles": ["Backend Engineer", " SDE "], "max_age_hours": 24, "request_budget": 12},
                    profile="p.md", runs_dir="runs", db="d.sqlite", config=CONFIG, run_id="r1")
    assert ["--location", "Bengaluru,Karnataka,India"] == ok[ok.index("--location"):ok.index("--location") + 2]
    assert "Hyderabad,Telangana,India" in ok and ok[ok.index("--lookback") + 1] == "24h" and ok[ok.index("--max-searches") + 1] == "12"
    assert ok[ok.index("--titles") + 1] == "Backend Engineer,SDE" and "--screen" in ok and "--yes" in ok and ok[ok.index("--run-id") + 1] == "r1"
    default = build_argv({}, profile="p.md", runs_dir="runs", db="d", config=CONFIG, run_id="r")
    assert default[default.index("--lookback") + 1] == "24h" and default[default.index("--max-searches") + 1] == "12"      # default max age 24h
    assert default.count("--location") == 2 and "--titles" not in default
    for bad, msg in (({"cities": ["Pune"]}, "not configured"), ({"cities": []}, "non-empty list"), ({"titles": []}, "titles must"),
                     ({"titles": ["x"] * 11}, "titles must"), ({"max_age_hours": 0}, "max_age_hours"), ({"max_age_hours": 9999}, "max_age_hours"),
                     ({"request_budget": 0}, "request_budget"), ({"request_budget": 51}, "request_budget"), ({"request_budget": 3.5}, "request_budget"),
                     ({"profile": "../../etc/passwd"}, "unknown field"), ({"request_budget": True}, "request_budget")):
        with pytest.raises(RequestError, match=msg):
            build_argv(bad, profile="p.md", runs_dir="runs", db="d", config=CONFIG, run_id="r")
    with pytest.raises(RequestError):
        build_argv([], profile="p.md", runs_dir="runs", db="d", config=CONFIG, run_id="r")


def test_end_to_end_with_the_real_flow_post_returns_a_run_id_and_get_reports_progress_stop_reason_and_fresh_jobs(tmp_path, monkeypatch, server):
    for k, v in (("SERPAPI_API_KEY", "k"), ("NEMO_PAID_CALLS_ENABLED", "true"), ("NEMO_MAX_USD_PER_RUN", "1.00")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NEMO_MONTHLY_USD_CAP", raising=False)
    (tmp_path / "p.md").write_text(PROFILE)
    seen = []

    def fake(self, engine, params):
        seen.append(dict(params))
        job = {"title": f"Backend Engineer {chr(64 + len(seen))}", "company_name": f"Co{len(seen)}", "location": "Bengaluru, Karnataka", "via": "Co Careers",
               "description": "Build REST APIs and microservices in Python. Requirements: proficiency in Python and SQL. Experience: 0-2 years. " * 3,
               "job_id": f"g{len(seen)}", "extensions": ["2 hours ago", "Full–time"],
               "apply_options": [{"title": "Co Careers", "link": f"https://careers.co{len(seen)}.example/jobs/{len(seen)}00000"}]}
        out = {"search_metadata": {"processed_at": RT}, "search_parameters": {"q": params["q"], "location_requested": params["location"], "google_domain": "google.com"},
               "jobs_results": [job]}
        if len(seen) == 1:
            out["serpapi_pagination"] = {"next_page_token": "TOK"}
        return out
    monkeypatch.setattr(SerpApi, "search_raw", fake)
    col = Collector(profile=str(tmp_path / "p.md"), runs_dir=str(tmp_path / "runs"), db=str(tmp_path / "d.sqlite"), env_file=str(tmp_path / "none"), config=CONFIG)
    base = server(col)
    code, body = call(base, "POST", "/runs", {"cities": ["Bengaluru"], "titles": ["Backend Engineer"], "request_budget": 6})
    assert code == 202 and body["state"] == "running" and body["status_url"] == f"/runs/{body['run_id']}"
    col.wait(30)
    code, res = call(base, "GET", body["status_url"])
    assert code == 200 and res["state"] == "complete" and res["request_budget"] == 6 and res["requests_used"] == 6
    assert res["stop_reason"] == "end_of_results"                                                       # every query ran out of pages: stopped, and said why
    assert res["max_age"] == "24h" and res["cities"] == ["Bengaluru"] and res["counts"]["fresh_shortlist"] == 6
    job = res["fresh_shortlist"][0]
    assert job["freshness"] == "confirmed_within" and job["application_link"].startswith("https://careers.co") and job["reported_age"].startswith("2 hours ago")
    assert [("next_page_token" in p) for p in seen] == [False] * 5 + [True] and seen[0]["q"] == "Backend Engineer" and seen[0]["location"] == "Bengaluru,Karnataka,India"      # 5 queries first, then page 2 of the one with a token
    assert (tmp_path / "runs" / body["run_id"] / "raw" / "06_google_jobs_p2.json").exists()          # raw responses preserved
    assert call(base, "GET", "/runs/nope")[0] == 404


def test_only_one_run_at_a_time_and_a_blocked_run_reports_running(tmp_path, server):
    gate = threading.Event()

    def slow(argv):
        gate.wait(10)
        run = argv[argv.index("--run-id") + 1]
        (tmp_path / "runs" / run / "status.json").write_text(json.dumps({"state": "complete", "run_id": run, "stop_reason": "end_of_results"}))
        return 0
    col = Collector(profile="p.md", runs_dir=str(tmp_path / "runs"), db="d", runner=slow, config=CONFIG)
    base = server(col)
    code, first = call(base, "POST", "/runs", {})
    assert code == 202
    assert call(base, "GET", f"/runs/{first['run_id']}")[1]["state"] == "running"
    code, busy = call(base, "POST", "/runs", {})
    assert code == 409 and "already in progress" in busy["error"]
    gate.set()
    col.wait(10)
    assert call(base, "GET", f"/runs/{first['run_id']}")[1]["state"] == "complete"
    assert call(base, "POST", "/runs", {})[0] == 202                                                  # free again


def test_errors_are_actionable_and_a_runner_that_refuses_or_crashes_is_reported_failed(tmp_path, server):
    col = Collector(profile="p.md", runs_dir=str(tmp_path / "runs"), db="d", runner=lambda argv: 2, config=CONFIG)
    base = server(col)
    assert call(base, "POST", "/runs", {"cities": ["Pune"]})[0] == 400
    assert call(base, "POST", "/runs", raw=b"{not json")[0] == 400
    assert call(base, "POST", "/runs", raw=b"x" * 20_000)[0] == 413
    assert call(base, "POST", "/other", {})[0] == 404
    code, r = call(base, "POST", "/runs", {})
    col.wait(10)
    st = call(base, "GET", f"/runs/{r['run_id']}")[1]
    assert st["state"] == "failed" and "refused to start" in st["error"] and "SERPAPI_API_KEY" in st["error"]
    def crash(argv):
        raise RuntimeError("boom")
    col2 = Collector(profile="p.md", runs_dir=str(tmp_path / "runs2"), db="d", runner=crash, config=CONFIG)
    base2 = server(col2)
    code, r = call(base2, "POST", "/runs", {})
    col2.wait(10)
    assert call(base2, "GET", f"/runs/{r['run_id']}")[1]["error"] == "RuntimeError: boom"


def test_token_is_required_when_configured_and_non_loopback_needs_one(tmp_path, server, monkeypatch):
    col = Collector(profile="p.md", runs_dir=str(tmp_path / "runs"), db="d", runner=lambda a: 0, config=CONFIG)
    base = server(col, token="s3cret")
    assert call(base, "POST", "/runs", {})[0] == 401 and call(base, "GET", "/runs/x")[0] == 401
    assert call(base, "POST", "/runs", {}, token="wrong")[0] == 401
    assert call(base, "POST", "/runs", {}, token="s3cret")[0] == 202
    monkeypatch.delenv("NEMO_SERVICE_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="non-loopback"):
        serve("0.0.0.0", 0, runs_dir=str(tmp_path / "r"), env_file=str(tmp_path / "none"))


def test_a_custom_profile_is_written_to_the_run_dir_and_screened_against_instead_of_the_servers_own(tmp_path):
    (tmp_path / "runs").mkdir()
    body = {"experience": "Two years building backend services in Python.", "desired_roles": ["Backend Engineer", "SDE"],
           "must_haves": ["Remote within India"], "nice_to_haves": ["Python"], "exclusions": ["Sales"]}
    argv = build_argv(body, profile="p.md", runs_dir=str(tmp_path / "runs"), db="d", config=CONFIG, run_id="r1",
                      profile_dir=tmp_path / "runs")
    written = argv[argv.index("--profile") + 1]
    assert written != "p.md" and Path(written) == tmp_path / "runs" / "profile.md"
    prof = load_profile(written)
    assert prof.experience.startswith("Two years") and prof.desired_roles == ("Backend Engineer", "SDE")
    assert prof.must_haves == ("Remote within India",) and prof.exclusions == ("Sales",)
    # request_budget is capped tighter for a visitor-supplied profile than the server's own default path
    with pytest.raises(RequestError, match="request_budget must be an integer between 1 and 10"):
        build_argv({**body, "request_budget": 20}, profile="p.md", runs_dir=str(tmp_path / "runs"), db="d", config=CONFIG,
                  run_id="r2", profile_dir=tmp_path / "runs")
    with pytest.raises(RequestError, match="titles cannot be combined"):
        build_argv({**body, "titles": ["x"]}, profile="p.md", runs_dir=str(tmp_path / "runs"), db="d", config=CONFIG,
                  run_id="r3", profile_dir=tmp_path / "runs")
    with pytest.raises(RequestError, match="needs: desired_roles"):
        build_argv({"experience": "x"}, profile="p.md", runs_dir=str(tmp_path / "runs"), db="d", config=CONFIG, run_id="r4",
                  profile_dir=tmp_path / "runs")
    with pytest.raises(RequestError, match="Desired roles is empty|desired_roles"):
        build_argv({**body, "desired_roles": []}, profile="p.md", runs_dir=str(tmp_path / "runs"), db="d", config=CONFIG,
                  run_id="r5", profile_dir=tmp_path / "runs")


def test_rate_limiter_is_a_shared_sliding_window_and_failed_requests_give_their_slot_back():
    rl = RateLimiter(limit=2, window_seconds=3600)
    ok1, q1 = rl.try_consume()
    ok2, q2 = rl.try_consume()
    ok3, q3 = rl.try_consume()
    assert (ok1, ok2, ok3) == (True, True, False) and q1["remaining"] == 1 and q2["remaining"] == 0 and q3["remaining"] == 0
    assert q3["retry_after_seconds"] > 0
    rl.release()
    ok4, q4 = rl.try_consume()
    assert ok4 is True and q4["remaining"] == 0


def test_the_http_layer_enforces_the_shared_rate_limit_and_exposes_it_at_slash_quota(tmp_path, server):
    col = Collector(profile="p.md", runs_dir=str(tmp_path / "runs"), db="d", runner=lambda a: 0, config=CONFIG)
    base = server(col, token="s3cret", limiter=RateLimiter(limit=2, window_seconds=3600))
    assert call(base, "GET", "/quota")[0] == 401                              # API endpoints still need the token
    assert call(base, "GET", "/quota", token="s3cret")[1]["remaining"] == 2
    assert call(base, "POST", "/runs", {}, token="s3cret")[0] == 202
    assert call(base, "GET", "/quota", token="s3cret")[1]["remaining"] == 1
    assert call(base, "POST", "/runs", {}, token="s3cret")[0] == 202
    code, body = call(base, "POST", "/runs", {}, token="s3cret")
    assert code == 429 and body["remaining"] == 0 and "rate limit" in body["error"]
    with urllib.request.urlopen(base + "/", timeout=10) as r:                 # the static frontend needs no token
        assert r.status == 200 and b"<html" in r.read()[:200]


def test_a_run_left_running_by_a_dead_process_is_marked_interrupted_on_start(tmp_path):
    d = tmp_path / "runs" / "old-run"; d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({"state": "running", "run_id": "old-run"}))
    done = tmp_path / "runs" / "done-run"; done.mkdir()
    (done / "status.json").write_text(json.dumps({"state": "complete", "run_id": "done-run"}))
    col = Collector(profile="p.md", runs_dir=str(tmp_path / "runs"), db="d", config=CONFIG)
    assert col.status("old-run")["state"] == "interrupted" and "restarted" in col.status("old-run")["error"]
    assert col.status("done-run")["state"] == "complete"
