"""collect()/resume()/get_run()/recover with a fake provider. All job data is SYNTHETIC."""

import pytest

from nemo.budget import Budget, BudgetConfig
from nemo.collect import collect, get_run, recover_stale, resume
from nemo.collect_store import CollectStore
from nemo.jobsource import CollectRequest
from nemo.providers.base import ProviderError
from nemo.providers.brightdata import BrightDataJobs
from test_brightdata import LONG, synthetic

NOSLEEP = dict(sleep=lambda s: None)


class Fake(BrightDataJobs):
    """Real mapping, scripted transport."""

    def __init__(self, states=("ready",), records=None, launch_exc=None, fetch_exc=None):
        super().__init__("k")
        self.states, self.records = list(states), records if records is not None else []
        self.launch_exc, self.fetch_exc, self.launches, self.polls = launch_exc, fetch_exc, 0, 0

    def launch(self, req):
        self.launches += 1
        if self.launch_exc:
            raise self.launch_exc
        return "sd_fake"

    def status(self, op_id):
        self.polls += 1
        s = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(s, Exception):
            raise s
        return s, {"status": s}

    def fetch(self, op_id):
        if self.fetch_exc:
            raise self.fetch_exc
        return self.records


@pytest.fixture
def env():
    store = CollectStore(":memory:")
    budget = Budget(BudgetConfig(paid_calls_enabled=True, max_usd_per_run="1", max_queries=5), run_id="t")
    yield store, budget
    store.close()


REQ = CollectRequest(roles=("Platform Engineer",), max_records=5, poll_timeout_s=100, poll_interval_s=1)


def clock_steps():
    t = {"v": 0.0}
    def clock():
        t["v"] += 30
        return t["v"]
    return clock


def test_success_maps_persists_and_separates_ids(env):
    store, budget = env
    run = collect(REQ, Fake(["starting", "running", "ready"], [synthetic("4470000001"), synthetic("4470000002")]),
                  store, budget, **NOSLEEP)
    assert run["status"] == "succeeded" and run["jobs_stored"] == 2
    assert run["run_id"] != run["provider_op_id"] == "sd_fake"
    assert [j["source_job_id"] for j in run["jobs"]] == ["4470000001", "4470000002"]
    full = get_run(run["run_id"], store, include_raw=True)
    assert full["jobs"][0]["raw"]["job_title"] == "Example Engineer" and full["est_billed_usd"] == pytest.approx(0.003)
    assert full["measured_usd"] is None                      # estimate only; never claimed as measured


def test_successful_empty_search_is_not_failure(env):
    store, budget = env
    run = collect(REQ, Fake(["ready"], []), store, budget, **NOSLEEP)
    assert run["status"] == "no_results" and run["jobs_stored"] == 0 and "0 jobs" in run["reason"]


def test_provider_failed_without_data_is_failed_and_with_data_partial(env):
    store, budget = env
    assert collect(REQ, Fake(["failed"], []), store, budget, **NOSLEEP)["status"] == "failed"
    run = collect(REQ, Fake(["failed"], [synthetic()]), store, budget, **NOSLEEP)
    assert run["status"] == "partial" and run["jobs_stored"] == 1


def test_error_records_make_run_partial(env):
    store, budget = env
    run = collect(REQ, Fake(["ready"], [synthetic(), {"error": "dead_page", "error_code": "dead_page"}]), store, budget, **NOSLEEP)
    assert run["status"] == "partial" and run["error_records"] == 1 and run["jobs_stored"] == 1


def test_dedup_by_source_id_not_title_and_bound_on_max_records(env):
    store, budget = env
    same_title = [synthetic("4470000001"), synthetic("4470000002"), synthetic("4470000001"), synthetic("4470000003")]
    run = collect(CollectRequest(roles=("A",), max_records=2), Fake(["ready"], same_title), store, budget, **NOSLEEP)
    assert run["jobs_stored"] == 2 and run["duplicates"] == 1
    assert {j["source_job_id"] for j in run["jobs"]} == {"4470000001", "4470000002"}  # same titles, different ids kept


def test_repeat_run_updates_not_duplicates(env):
    store, budget = env
    collect(REQ, Fake(["ready"], [synthetic()]), store, budget, **NOSLEEP)
    collect(REQ, Fake(["ready"], [synthetic(job_title="Renamed")]), store, budget, **NOSLEEP)
    assert store.db.execute("SELECT COUNT(*) FROM collect_jobs").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM collect_run_jobs").fetchone()[0] == 2


def test_missing_description_is_marked_not_hidden(env):
    store, budget = env
    run = collect(REQ, Fake(["ready"], [synthetic(job_description_formatted="")]), store, budget, **NOSLEEP)
    j = run["jobs"][0]
    assert j["description_status"] == "missing" and "description_missing" in j["flags"] and j["verification"] == "provider_reported"


def test_launch_unknown_is_not_retried(env):
    store, budget = env
    p = Fake(launch_exc=ProviderError("timeout", "request timed out"))
    run = collect(REQ, p, store, budget, **NOSLEEP)
    assert run["status"] == "launch_unknown" and p.launches == 1 and "NOT retried" in run["reason"]
    assert resume(run["run_id"], p, store)["status"] == "launch_unknown" and p.launches == 1


def test_rejected_launch_is_actionable_failure(env):
    store, budget = env
    run = collect(REQ, Fake(launch_exc=ProviderError("unauthorized", "HTTP 401", charged=False)), store, budget, **NOSLEEP)
    assert run["status"] == "failed" and "BRIGHTDATA_API_KEY" in run["reason"]


def test_budget_refusal_launches_nothing(env):
    store, _ = env
    p = Fake()
    off = Budget(BudgetConfig(), run_id="t")
    run = collect(REQ, p, store, off, **NOSLEEP)
    assert run["status"] == "failed" and p.launches == 0 and "nothing spent" in run["reason"]
    tiny = Budget(BudgetConfig(paid_calls_enabled=True, max_usd_per_run="0.001"), run_id="t")
    assert collect(REQ, p, store, tiny, **NOSLEEP)["status"] == "failed" and p.launches == 0


def test_poll_timeout_keeps_op_id_and_resume_completes(env):
    store, budget = env
    p = Fake(["running"], [synthetic()])
    run = collect(CollectRequest(roles=("A",), max_records=5, poll_timeout_s=50, poll_interval_s=1), p, store, budget,
                  clock=clock_steps(), **NOSLEEP)
    assert run["status"] == "timed_out" and run["provider_op_id"] == "sd_fake" and "not cancelled" in run["reason"]
    p.states = ["ready"]
    done = resume(run["run_id"], p, store, **NOSLEEP)
    assert done["status"] == "succeeded" and done["jobs_stored"] == 1 and p.launches == 1


def test_transient_poll_errors_retry_then_give_up(env):
    store, budget = env
    flaky = ProviderError("timeout", "t")
    ok = collect(REQ, Fake([flaky, "ready"], [synthetic()]), store, budget, **NOSLEEP)
    assert ok["status"] == "succeeded"
    dead = collect(REQ, Fake([flaky], [synthetic()]), store, budget, **NOSLEEP)
    assert dead["status"] == "timed_out" and "resume" in dead["reason"]
    auth = collect(REQ, Fake([ProviderError("unauthorized", "HTTP 401")]), store, budget, **NOSLEEP)
    assert auth["status"] == "failed"


def test_ready_but_download_fails_is_resumable(env):
    store, budget = env
    p = Fake(["ready"], [synthetic()], fetch_exc=ProviderError("timeout", "t"))
    run = collect(REQ, p, store, budget, **NOSLEEP)
    assert run["status"] == "timed_out" and "download failed" in run["reason"]
    p.fetch_exc = None
    assert resume(run["run_id"], p, store, **NOSLEEP)["status"] == "succeeded"


def test_interrupt_marks_run_interrupted_and_keeps_saved_jobs(env):
    store, budget = env

    class Boom(Fake):
        def fetch(self, op_id):
            raise KeyboardInterrupt()
    p = Boom(["ready"])
    with pytest.raises(KeyboardInterrupt):
        collect(REQ, p, store, budget, **NOSLEEP)
    row = store.db.execute("SELECT status, provider_op_id FROM collect_runs").fetchone()
    assert (row["status"], row["provider_op_id"]) == ("interrupted", "sd_fake")


def test_recover_stale_marks_dead_processes(env):
    store, _ = env
    store.create_run("r1", "brightdata", {}, "T", 0.1)                       # died before launch confirmed
    store.create_run("r2", "brightdata", {}, "T", 0.1)
    store.update_run("r2", provider_op_id="sd_x", status="running")           # died while polling
    assert sorted(recover_stale(store)) == ["r1", "r2"]
    assert store.get_run_row("r1")["status"] == "launch_unknown"
    assert store.get_run_row("r2")["status"] == "interrupted"


def test_validate_bounds(env):
    store, budget = env
    for bad in (CollectRequest(roles=()), CollectRequest(roles=("A",), max_records=0), CollectRequest(roles=("A",), max_records=500)):
        with pytest.raises(ValueError):
            collect(bad, Fake(), store, budget)


def test_get_run_unknown_and_bounded(env):
    store, budget = env
    with pytest.raises(KeyError):
        get_run("nope", store)
    run = collect(REQ, Fake(["ready"], [synthetic(str(4470000000 + i)) for i in range(5)]), store, budget, **NOSLEEP)
    assert len(get_run(run["run_id"], store, limit=2)["jobs"]) == 2


def test_cli_dry_run_refusals_and_get(tmp_path, monkeypatch, capsys):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("collect_cli", pathlib.Path(__file__).parent.parent / "scripts" / "collect.py")
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    for k in ("BRIGHTDATA_API_KEY", "NEMO_PAID_CALLS_ENABLED", "NEMO_MAX_USD_PER_RUN"):
        monkeypatch.delenv(k, raising=False)
    base = ["--env-file", str(tmp_path / "none"), "--db", str(tmp_path / "t.sqlite")]
    assert cli.main(base + ["run", "--role", "A", "--dry-run"]) == 0
    assert '"limit_per_input": 25' in capsys.readouterr().out
    assert cli.main(base + ["run", "--role", "A"]) == 2                       # no key
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "k")
    assert cli.main(base + ["run", "--role", "A"]) == 2                       # paid calls disabled
    assert cli.main(base + ["run", "--role", "A", "--max-records", "999"]) == 2
    assert cli.main(base + ["get", "nope"]) == 2
    monkeypatch.setenv("NEMO_PAID_CALLS_ENABLED", "true"); monkeypatch.setenv("NEMO_MAX_USD_PER_RUN", "1")
    fake = lambda key, **kw: Fake(["ready"], [synthetic()])
    assert cli.main(base + ["run", "--role", "A", "--max-records", "5", "--yes"], provider_factory=fake) == 0
    assert "status=succeeded" in capsys.readouterr().out


def test_live_smoke_refuses_without_opt_in(tmp_path, monkeypatch, capsys):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("smoke", pathlib.Path(__file__).parent.parent / "scripts" / "live_smoke_brightdata.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    monkeypatch.delenv("NEMO_LIVE_SMOKE", raising=False)
    assert m.main(["--env-file", str(tmp_path / "x"), "--yes"]) == 2
    monkeypatch.setenv("NEMO_LIVE_SMOKE", "1"); monkeypatch.delenv("BRIGHTDATA_API_KEY", raising=False)
    assert m.main(["--env-file", str(tmp_path / "x"), "--yes"]) == 2


def test_validate_jobs_reports_url_description_and_location_checks(tmp_path, capsys):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("smoke2", pathlib.Path(__file__).parent.parent / "scripts" / "live_smoke_brightdata.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    store = CollectStore(str(tmp_path / "v.sqlite"))
    budget = Budget(BudgetConfig(paid_calls_enabled=True, max_usd_per_run="1"), run_id="t")
    recs = [synthetic("4470000001"), synthetic("4470000002", job_description_formatted="short"),
            synthetic("4470000003", job_location="Austin, TX, United States")]
    run = collect(REQ, Fake(["ready"], recs), store, budget, **NOSLEEP)
    store.close()
    v = m.validate_jobs(get_run(run["run_id"], CollectStore(str(tmp_path / "v.sqlite")), limit=10)["jobs"])
    assert v["jobs"] == 3 and v["unique_ids"] == 3 and v["url_is_job_url_with_matching_id"] == 3
    assert v["description_full"] == 2 and v["india_location"] == 2 and v["non_india_location"][0][0] == "4470000003"
    assert m.main(["--db", str(tmp_path / "v.sqlite"), "--env-file", str(tmp_path / "x"), "--validate-run", run["run_id"]]) == 0
    assert m.main(["--db", str(tmp_path / "v.sqlite"), "--env-file", str(tmp_path / "x"), "--validate-run", "nope"]) == 2
