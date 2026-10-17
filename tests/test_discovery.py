import collections
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def test_google_jobs_keeps_all_jobs_rejects_mismatched_linkedin_link_and_flags_others():
    kept, rejected, no_li, skipped = parse_google_jobs(jobs_raw(
        job("DevOps Engineer", "Acme", "devops-engineer-at-acme-4470000001"),
        job("AI Ops Engineer", "Circana", "ai-ops-engineer-at-skit-ai-4344432903", via="Careers At Circana"),
        job("DevOps Engineer", "Beta", "devops-engineer-at-beta-4470000002", desc="Based in Pune. " + "d" * 600),
        {"title": "No LinkedIn", "company_name": "Gamma", "apply_options": [{"title": "Gamma Careers", "link": "https://careers.gamma.example/jobs/9"}]}), WIN)
    assert [r["linkedin_job_id"] for r in rejected] == ["4344432903"]            # LinkedIn link rejected, recorded
    assert len(kept) == 4 and no_li == 2 and skipped == 0                          # but the jobs are kept
    assert kept[0].job_id == "4470000001" and kept[0].linkedin_job_id == "4470000001"
    assert kept[1].linkedin_job_id is None and kept[1].job_id.startswith("url:") and "LinkedIn link rejected" in kept[1].flags[0]
    assert kept[2].flags and "location mismatch" in kept[2].flags[0]            # flagged, not dropped
    assert kept[3].application["kind"] == "employer_ats" and kept[3].application["url"].startswith("https://careers.gamma")
    r = kept[0].date_reports[0]
    assert r["raw"] == "2 days ago" and r["source"] == "google_jobs.extensions" and r["basis"] == "aggregator_reported"
    assert r["verified"] is False and r["lookback_status"] == "reported_within"


def gjob(title, company, links, via="Acme Careers", gid=None, desc="d" * 800):
    return {"title": title, "company_name": company, "location": "Bengaluru, Karnataka", "via": via, "description": desc,
            "extensions": ["2 days ago"], "job_id": gid, "apply_options": [{"title": t, "link": l} for t, l in links]}


def test_non_linkedin_jobs_prefer_employer_link_keep_all_options_and_description_source():
    kept, _, no_li, _ = parse_google_jobs(jobs_raw(gjob("Backend Engineer", "Acme Robotics", [
        ("Jooble", "https://in.jooble.org/jdp/1"), ("Acme Careers", "https://careers.acmerobotics.com/job/77?utm_source=google"),
        ("Shine", "https://www.shine.com/jobs/x/1")])), WIN)
    j = merge(kept)[kept[0].job_id]
    assert no_li == 1 and j["linkedin_job_id"] is None and j["linkedin_url"] is None
    assert j["application_url"].startswith("https://careers.acmerobotics.com/") and j["application_kind"] == "employer_ats"
    assert len(j["application_options"]) == 3 and j["description_source"] == "Acme Careers" and j["url"] == j["application_url"]


def test_identity_ignores_tracking_params_never_merges_by_title_and_survives_missing_ids():
    same_a = gjob("Backend Engineer", "Acme", [("Acme Careers", "https://careers.acme.example/j/5?utm_source=a")])
    same_b = gjob("Backend Engineer", "Acme", [("Acme Careers", "https://careers.acme.example/j/5?utm_source=b&utm_medium=x")])
    other = gjob("Backend Engineer", "Acme", [("Acme Careers", "https://careers.acme.example/j/6")])        # same title, other posting
    no_links = gjob("Backend Engineer", "Acme", [], gid="google-abc")
    unidentifiable = gjob("Backend Engineer", "Acme", [])
    kept, _, _, skipped = parse_google_jobs(jobs_raw(same_a, same_b, other, no_links, unidentifiable), WIN)
    assert skipped == 1                                                       # no link and no Google job id: counted
    jobs = merge(kept)
    assert len(kept) == 4 and len(jobs) == 3                                   # a+b merge; other, google-id stay separate
    assert sum(k.startswith("gj:") for k in jobs) == 1
    assert sorted(len(j["sources"]) for j in jobs.values()) == [1, 1, 2]


def test_merge_attaches_linkedin_link_when_another_sighting_has_it():
    kept, _, _, _ = parse_google_jobs(jobs_raw(job("DevOps Engineer", "Acme", "devops-engineer-at-acme-4470000001", via="Acme Careers")), WIN)
    j = merge(kept)["4470000001"]
    assert j["url"] == "https://www.linkedin.com/jobs/view/4470000001" and j["linkedin_url"] == j["url"]


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
    a, _, _, _ = parse_google_jobs(jobs_raw(job("DevOps Engineer", "Acme", "devops-engineer-at-acme-4470000001", desc="long " * 300)), WIN)
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
    a, _, _, _ = parse_google_jobs(jobs_raw(job("DevOps Engineer", "Acme", "devops-engineer-at-acme-4470000001")), WIN)
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


def test_search_plan_round_robins_families_and_locations_under_a_small_budget():
    from nemo.profile import parse_profile
    from nemo.queryplan import DEFAULT_CONFIG, build_plan, load_config, select_families
    prof = parse_profile("## Experience\nx\n\n## Desired roles\n- Backend Engineer\n- Software Development Engineer\n- Quantum Gardener\n\n"
                         "## Must-haves\n\n## Nice-to-haves\n\n## Exclusions\n")
    cfg = load_config(DEFAULT_CONFIG)
    assert [f for f, _ in select_families(prof.desired_roles, cfg)] == ["backend", "sde", "other"]
    locs = ["Bengaluru,Karnataka,India", "Hyderabad,Telangana,India"]
    steps = build_plan(prof.desired_roles, locs, timedelta(days=7), "in", cfg)
    assert [s.role for s in steps[:3]] == ["backend", "sde", "other"]                                # every family before repeats
    assert len({s.params["location"] for s in steps[:2]}) == 2                                       # not all one city
    pairs = [(s.params["q"], s.params["location"]) for s in steps]
    assert len(pairs) == len(set(pairs)) == len({q for q, _ in pairs}) * 2                           # every query in every city, once
    qs = {q for q, _ in pairs}
    assert {"Backend Engineer", "Software Engineer", "Quantum Gardener"} <= qs                       # plain-role searches retained
    assert {"SDE I", "Software Development Engineer I", "Junior Backend Engineer"} <= qs             # natural early-career aliases
    assert not any("junior Backend" in q or "Backend Engineer fresher" in q for q in qs)             # no mechanical prefix/suffix
    assert all(s.engine == "google_jobs" for s in steps)                                              # site search is off by default
    with_site = build_plan(prof.desired_roles, locs, timedelta(days=7), "in", {**cfg, "include_site_search": True})
    assert any(s.engine == "google" for s in with_site) and with_site[0].engine == "google_jobs"


def test_search_config_is_validated(tmp_path):
    from nemo.queryplan import load_config
    (tmp_path / "c.json").write_text('{"families": {"x": {"roles": [], "queries": []}}}')
    with pytest.raises(ValueError):
        load_config(tmp_path / "c.json")


BENCH_LOCS = ["Bengaluru,Karnataka,India", "Hyderabad,Telangana,India", "Visakhapatnam,Andhra Pradesh,India"]


def bench_plan(locs=BENCH_LOCS):
    from nemo.profile import parse_profile
    from nemo.queryplan import build_plan, load_config
    prof = parse_profile("## Experience\nx\n\n## Desired roles\n- Backend Engineer\n- Software Development Engineer\n\n"
                         "## Must-haves\n\n## Nice-to-haves\n\n## Exclusions\n")
    cfg = {**load_config(), "priority": [{"q": "SDE I", "city": "Bengaluru"}, {"q": "Backend Engineer", "city": "Hyderabad"},
                                          {"q": "Python Backend Engineer", "city": "Bengaluru"}, {"q": "SDE", "city": "Hyderabad"}]}
    return build_plan(prof.desired_roles, locs, timedelta(days=7), "in", cfg)


def test_pinned_queries_run_first_the_plan_is_deterministic_and_pins_only_reorder():
    a, b = bench_plan(), bench_plan()
    first = [(s.params["q"], s.params["location"].split(",")[0]) for s in a[:4]]
    assert first == [("SDE I", "Bengaluru"), ("Backend Engineer", "Hyderabad"), ("Python Backend Engineer", "Bengaluru"), ("SDE", "Hyderabad")]
    assert [s.params for s in a] == [s.params for s in b]                       # repeated capped runs run the same first queries
    rest = {(s.params["q"], s.params["location"].split(",")[0]) for s in a[4:]}
    assert ("SDE I", "Visakhapatnam") in rest and ("Software Engineer", "Bengaluru") in rest   # configured cities keep everything
    assert not any(q == "SDE I" and c == "Bengaluru" for q, c in rest)                         # pinned pair not repeated
    no_hyd = bench_plan(["Bengaluru,Karnataka,India", "Visakhapatnam,Andhra Pradesh,India"])
    assert [s.params["q"] for s in no_hyd[:2]] == ["SDE I", "Python Backend Engineer"]         # pins for unconfigured cities are skipped


def test_queries_file_gives_explicit_steps_and_web_searches_have_no_freshness(tmp_path):
    import json
    from nemo.queryplan import load_queries_file
    f = tmp_path / "q.json"
    f.write_text(json.dumps([
        {"label": "a", "engine": "google_jobs", "q": "Software Development Engineer I", "location": "Bengaluru,Karnataka,India"},
        {"label": "b", "engine": "google", "q": "site:linkedin.com/jobs/view/4473364096"},
        {"label": "c", "engine": "google", "q": "x", "tbs": "qdr:m"}]))
    a, b, c = load_queries_file(f, "in")
    assert a.params == {"q": "Software Development Engineer I", "location": "Bengaluru,Karnataka,India", "hl": "en", "gl": "in"}
    assert b.params == {"q": "site:linkedin.com/jobs/view/4473364096", "num": "10", "hl": "en", "gl": "in"} and "tbs" not in b.params
    assert c.params["tbs"] == "qdr:m" and [s.role for s in (a, b, c)] == ["a", "b", "c"]
    for bad in ('[]', '[{"engine": "bing", "q": "x"}]', '[{"engine": "google_jobs", "q": "x"}]', '[{"engine": "google", "q": " "}]'):
        f.write_text(bad)
        with pytest.raises(ValueError):
            load_queries_file(f, None)


def test_location_attribution_uses_location_requested():
    raw = jobs_raw(job("DevOps Engineer", "Acme", "devops-engineer-at-acme-4470000001"))
    raw["search_parameters"] = {"engine": "google_jobs", "q": "DevOps Engineer", "location_requested": "Hyderabad,Telangana,India",
                                "location_used": "Hyderabad,Telangana,India"}              # SerpApi's real key; no plain "location"
    kept, _, _, _ = parse_google_jobs(raw, WIN)
    assert kept[0].location_param == "Hyderabad,Telangana,India"
    assert merge(kept)["4470000001"]["sources"][0]["location_param"] == "Hyderabad,Telangana,India"
    legacy = jobs_raw(job("DevOps Engineer", "Acme", "devops-engineer-at-acme-4470000001"))      # older shape still works
    assert parse_google_jobs(legacy, WIN)[0][0].location_param == "Bengaluru,Karnataka,India"


def test_application_url_status_flags_generic_links_but_not_id_in_query_or_path():
    from nemo.discovery import application_url_status as st
    for ok in ("https://in.talent.com/view?id=639659631666733986&utm_campaign=x", "https://www.levels.fyi/jobs?jobId=94599434129547974",
               "https://in.indeed.com/viewjob?jk=4cf8ee5eef91949c", "https://alion.io/job/arintra-sde-i",
               "https://sanofi.wd3.myworkdayjobs.com/SanofiCareers/job/Hyderabad/Integration-Platform-Engineer_R2872422/apply",
               "https://careers.kula.ai/digantara/40106/", "https://jobs.example.com/12345678"):
        assert st(ok)[0] == "structurally_valid", ok
    for bad in ("https://in.talent.com/view", "https://www.levels.fyi/jobs", "https://example.com/", "https://example.com/careers",
                "https://in.talent.com/view?utm_source=google_jobs_apply&utm_medium=organic", "", "not a url"):
        assert st(bad)[0] == "needs_review", bad


def test_job_id_query_parameters_are_part_of_identity_tracking_only_differences_are_not():
    from nemo.discovery import job_key, normalize_url
    k = lambda u: job_key(None, {"url": u}, None)
    # different job ids stay separate, whatever the host puts the id in
    for a, b in (("https://in.talent.com/view?id=1", "https://in.talent.com/view?id=2"),
                 ("https://in.indeed.com/viewjob?jk=aaa111", "https://in.indeed.com/viewjob?jk=bbb222"),
                 ("https://www.glassdoor.co.in/job-listing/jv?jl=1010", "https://www.glassdoor.co.in/job-listing/jv?jl=2020"),
                 ("https://www.levels.fyi/jobs?jobId=11", "https://www.levels.fyi/jobs?jobId=22"),
                 ("https://careers.mcafee.com/jobs/1762?lang=en-us", "https://careers.mcafee.com/jobs/1763?lang=en-us")):
        assert k(a) != k(b), (a, b)
    # tracking-only differences, parameter order, www, trailing slash and fragment do not change identity
    base = "https://in.talent.com/view?id=1"
    for same in ("https://in.talent.com/view?id=1&utm_campaign=google_jobs_apply&utm_source=g&utm_medium=organic",
                 "https://in.talent.com/view?utm_source=x&id=1&gclid=abc&fbclid=z", "https://www.in.talent.com/view/?id=1#top"):
        assert k(same) == k(base), same
    # unknown parameters are retained conservatively (different value -> different identity)
    assert normalize_url("https://x.example/j?ref=A") != normalize_url("https://x.example/j?ref=B")
    assert normalize_url("https://x.example/j?a=1&b=2") == normalize_url("https://x.example/j?b=2&a=1")
    assert normalize_url("https://x.example/j?utm_source=a") == "x.example/j"


def test_two_jobs_on_one_board_path_stay_separate_through_parse_and_merge():
    one = gjob("Backend Engineer", "Acme", [("Talent", "https://in.talent.com/view?id=111&utm_source=g")])
    two = gjob("Backend Engineer", "Acme", [("Talent", "https://in.talent.com/view?id=222&utm_source=g")])
    again = gjob("Backend Engineer", "Acme", [("Talent", "https://in.talent.com/view?id=111&utm_medium=other")])
    kept, _, _, _ = parse_google_jobs(jobs_raw(one, two, again), WIN)
    jobs = merge(kept)
    assert len(kept) == 3 and len(jobs) == 2 and sorted(len(j["sources"]) for j in jobs.values()) == [1, 2]


def test_history_compatibility_legacy_key_links_old_rows_without_touching_them(tmp_path):
    """Rows stored before the fix are keyed by the old (query-less) hash. A re-seen job gets the new key, inherits first-seen
    history through its legacy key, and the old row is left in place."""
    from nemo.discovery import job_key
    url = "https://in.talent.com/view?id=639659631666733986&utm_source=g"
    legacy, new = job_key(None, {"url": url}, None, legacy=True), job_key(None, {"url": url}, None)
    assert legacy != new
    s = Store(tmp_path / "h.sqlite")
    s.start_run("r1", "t1")
    old = {"job_key": legacy, "url": url, "title": "T", "company": "C", "location": "L", "description": "d", "lookback_status": "unknown"}
    s.upsert_jobs("r1", "2026-10-01T10:00:00+00:00", {legacy: dict(old)})
    kept, _, _, _ = parse_google_jobs(jobs_raw(gjob("Backend Engineer", "Acme", [("Talent", url)])), WIN)
    j = merge(kept)
    assert list(j) == [new] and j[new]["legacy_key"] == legacy
    s.start_run("r2", "t2")
    s.upsert_jobs("r2", "2026-10-03T10:00:00+00:00", j)
    assert j[new]["previously_seen"] is True and j[new]["first_seen_at"].startswith("2026-10-01") and j[new]["first_seen_run"] == "r1"
    rows = {r[0]: r for r in s.db.execute("SELECT linkedin_job_id, first_seen_run, last_seen_run FROM jobs")}
    assert set(rows) == {legacy, new} and rows[legacy][2] == "r1"          # old row untouched, new row added
    plain = job_key(None, {"url": "https://alion.io/job/arintra-sde-i?utm_source=g"}, None)      # no query string: key unchanged
    assert plain == job_key(None, {"url": "https://alion.io/job/arintra-sde-i"}, None, legacy=True)


def test_search_config_targets_only_sde_and_backend_and_the_next_batch_fits_twelve_attempts():
    import json
    from nemo.queryplan import DEFAULT_CONFIG, build_plan, load_config, load_queries_file
    from nemo.profile import parse_profile
    cfg = load_config(DEFAULT_CONFIG)
    assert set(cfg["families"]) == {"sde", "backend"} and cfg["priority"] == []
    qs = [q["q"] for fam in cfg["families"].values() for q in fam["queries"]]
    banned = ("devops", "sre", "site reliability", "platform", "cloud", "sdet", "qa", "test", "ai ", "ml ")
    assert not [q for q in qs if any(b in q.lower() + " " for b in banned)]
    assert {"SDE I", "SDE", "Software Engineer", "Software Engineer I", "Software Development Engineer I", "Backend Engineer",
            "Backend Developer", "Python Backend Engineer"} <= set(qs)
    prof = parse_profile("## Experience\nx\n\n## Desired roles\n- Backend Engineer\n- Software Development Engineer\n- Software Engineer\n\n"
                         "## Must-haves\n\n## Nice-to-haves\n\n## Exclusions\n")
    plan = build_plan(prof.desired_roles, cfg["default_locations"], timedelta(days=7), "in", cfg)
    assert {s.role for s in plan} == {"sde", "backend"} and len(plan) == len(qs) * 2          # no full role x city explosion beyond 2 cities
    batch = load_queries_file(Path(__file__).parent.parent / "diagnostics" / "batch-sde-backend-12.json", "in")
    assert len(batch) == 12 and sum(s.page == 1 for s in batch) == 10 and sum(s.page == 2 for s in batch) == 2
    first = [s for s in batch if s.page == 1]
    assert collections.Counter(s.params["location"].split(",")[0] for s in first) == {"Bengaluru": 5, "Hyderabad": 5}
    assert all("Visakhapatnam" not in s.params["location"] for s in batch)
    pairs = [(s.params["q"], s.params["location"]) for s in first]
    assert len(set(pairs)) == 10                                                                 # distinct first-page requests
    assert [(batch[i].params["q"], batch[i].page) for i in (0, 1)] == [("Backend Engineer 0-2 years", 1), ("Backend Engineer 0-2 years", 2)]
    assert [(batch[i].params["q"], batch[i].page) for i in (2, 3)] == [("Backend Engineer", 1), ("Backend Engineer", 2)]   # pages on the promising queries
    qualified = {s.params["q"] for s in first if re.search(r"0-2|junior|SDE I$", s.params["q"], re.I)}
    assert len(qualified) >= 3 and {"Backend Engineer", "Software Engineer", "SDE"} <= {s.params["q"] for s in first}   # broad and experience-qualified mix


def test_google_employment_type_is_captured_from_extensions_and_kept_through_merge():
    j1 = job("Backend Engineer", "Acme", "backend-engineer-at-acme-4470000001", ext=("2 days ago", "Full–time"))
    j2 = job("Backend Intern", "Beta", "backend-intern-at-beta-4470000002", ext=("3 days ago", "Internship"))
    j3 = job("Backend Engineer", "Gamma", "backend-engineer-at-gamma-4470000003", ext=("3 days ago", "Work from home"))
    kept, _, _, _ = parse_google_jobs(jobs_raw(j1, j2, j3), WIN)
    assert [k.schedule for k in kept] == ["Full–time", "Internship", None]                      # unknown stays None, never assumed
    merged = merge(kept)
    assert merged["4470000001"]["schedule_type"] == "Full–time" and merged["4470000003"]["schedule_type"] is None
