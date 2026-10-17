"""Rules-first screening. All jobs are SYNTHETIC."""

import importlib.util
import json
import pathlib

import pytest

from nemo.profile import parse_profile
from nemo.screening import parse_experience, rank, screen

PROFILE = parse_profile("## Experience\nEarly career.\n\n## Desired roles\n- Backend Engineer\n- Software Development Engineer\n- SDET\n- DevOps Engineer\n\n"
                        "## Must-haves\n\n## Nice-to-haves\n\n## Exclusions\n")
LONG = "Build services in Python with Kubernetes and CI/CD. " * 12


def job(title="Backend Engineer", desc=LONG, loc="Bengaluru, Karnataka", jid="4470000001", **kw):
    return {"linkedin_job_id": jid, "title": title, "company": "Example Co", "location": loc, "description": desc,
            "url": f"https://www.linkedin.com/jobs/view/{jid}", "sources": [{"engine": "google_jobs", "via": "LinkedIn"}],
            "flags": [], "seen_in_runs": ["r1"], **kw}


def test_ranges_use_minimum_and_plus_means_that_number():
    assert parse_experience("", "Experience: 2-4 years of experience in Python").required_min == 2
    assert parse_experience("", "4+ years of experience required").required_min == 4
    assert parse_experience("", "Experience 6 to 10 Years").required_min == 6
    assert parse_experience("QA (Fresher)", "Freshers welcome").required_min == 0


def test_required_and_preferred_years_are_separate():
    e = parse_experience("", "2+ years of experience required. 5+ years of experience is a plus.")
    assert e.required_min == 2 and e.preferred_mentions == ["5+ years (description)"]


def test_company_history_and_education_years_are_not_requirements():
    e = parse_experience("", "We have more than 30 years of experience in providing IT services. "
                             "Degree (16 years of formal education). Experience: 2-3 years.")
    assert e.required_min == 2


def test_two_years_is_borderline_above_two_is_excluded_ranges_by_minimum():
    def sc(txt): return screen(job(desc=LONG + txt), PROFILE)
    two, range24, range35 = sc(" 2 years of experience."), sc(" Experience: 2-4 years."), sc(" Experience: 3-5 years.")
    three, plus3 = sc(" 3 years of experience."), sc(" 3+ years of experience required.")
    assert two.eligible and two.borderline and any("BORDERLINE" in g and "3 short of your 21 total" in g for g in two.gaps)
    assert range24.eligible and range24.borderline
    for bad in (range35, three, plus3):
        assert not bad.eligible and "nothing above 2 years" in bad.exclusions[0]
    one = sc(" 1 year of experience.")
    assert one.eligible and not one.borderline and one.fit > two.fit


def test_decimal_years_are_not_misread():
    assert parse_experience("", "1.5 years of experience in Python").required_min == 1.5
    assert screen(job(desc=LONG + " 1.5 years of experience."), PROFILE).eligible


def test_early_career_terms_rank_above_plain_roles():
    plain = screen(job(), PROFILE)
    junior = screen(job(title="Junior Backend Engineer"), PROFILE)
    eng1 = screen(job(title="Backend Engineer I"), PROFILE)
    assoc = screen(job(title="Associate Software Engineer"), PROFILE)
    assert all(x.eligible and x.fit > plain.fit for x in (junior, eng1, assoc))


def test_preferred_years_do_not_exclude():
    s = screen(job(desc=LONG + " 1 year of experience required. 5+ years of experience is a plus."), PROFILE)
    assert s.eligible and any("preferred (not required): 5+ years" in g for g in s.gaps)


def test_unknown_experience_stays_eligible_and_is_flagged_not_penalised():
    s = screen(job(), PROFILE)
    base = screen(job(desc=LONG + " 1 year of experience."), PROFILE)
    assert s.eligible and any("experience requirement not stated" in g for g in s.gaps) and s.fit >= base.fit - 1.01


def test_senior_titles_and_non_software_roles_are_excluded_with_reasons():
    for t in ("Senior Backend Engineer", "SDE-4", "QA Lead", "Lab Quality Advisor", "Project Manager - Process Automation"):
        s = screen(job(title=t), PROFILE)
        assert not s.eligible and s.exclusions
    assert screen(job(title="Quality Assurance Specialist", desc="Audit suppliers and lab processes. " * 20), PROFILE).eligible is False


def test_qa_with_software_evidence_is_excluded_from_the_default_shortlist_but_kept_with_a_reason():
    s = screen(job(title="Quality Assurance Engineer", desc=LONG + " Selenium test automation in Python."), PROFILE)
    assert not s.eligible and s.discipline == "qa_testing" and s.exclusions[0].startswith("QA / testing role by title")


def test_outside_india_excluded_unknown_location_flagged():
    assert not screen(job(loc="Austin, TX, United States"), PROFILE).eligible
    s = screen(job(loc="Remote"), PROFILE)
    assert s.eligible and any("not recognised as India" in g for g in s.gaps)


def test_preferences_rank_only_internship_below_full_time_and_location_boost():
    full = screen(job(), PROFILE)
    intern = screen(job(title="Backend Engineer Intern"), PROFILE)
    other_city = screen(job(loc="Pune, Maharashtra"), PROFILE)
    assert intern.eligible and intern.fit < full.fit and other_city.eligible and other_city.fit < full.fit
    ranked, excluded = rank([intern, full, screen(job(title="Senior Backend Engineer"), PROFILE)])
    assert ranked[0] is full and len(excluded) == 1


def load_script():
    spec = importlib.util.spec_from_file_location("screen_results", pathlib.Path(__file__).parent.parent / "scripts" / "screen_results.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_script_merges_runs_keeps_provenance_and_does_not_touch_originals(tmp_path, capsys):
    m = load_script()
    for rid, via, desc in (("r1", "LinkedIn", LONG), ("r2", "Join", LONG + " Longer description.")):
        d = tmp_path / rid; (d / "raw").mkdir(parents=True)
        j = job(desc=desc); j["sources"] = [{"engine": "google_jobs", "query": "q", "location_param": None, "via": via, "returned_url": "u"}]
        (d / "results.json").write_text(json.dumps({"run_id": rid, "jobs": [j]}))
        (d / "raw" / "01_google_jobs.json").write_text(json.dumps({"jobs_results": [{"apply_options": [
            {"title": "Join", "link": "https://join.com/x?utm=1"}, {"title": "LinkedIn", "link": "https://in.linkedin.com/jobs/view/4470000001"}]}]}))
    before = {p: p.read_text() for p in tmp_path.rglob("*.json")}
    prof = tmp_path / "p.md"
    prof.write_text("## Experience\nEarly.\n\n## Desired roles\n- Backend Engineer\n\n## Must-haves\n\n## Nice-to-haves\n\n## Exclusions\n")
    assert m.main(["--profile", str(prof), "--runs", str(tmp_path), "--out", str(tmp_path)]) == 0
    out = next(tmp_path.glob("screening-*")) / "ranked.json"
    r = json.loads(out.read_text())["shortlist"]
    assert len(r) == 1 and r[0]["seen_in_runs"] == ["r1", "r2"]
    assert r[0]["application_link"] == "https://join.com/x?utm=1" and "Join via google_jobs" in r[0]["description_provenance"]
    assert {p: p.read_text() for p in before} == before


# Evaluation cases: SYNTHETIC reproductions of three independently found postings (not special cases in the code).
ENTERPRISE_BOT = ("Automation-Focused DevOps Engineer with 2-3 years of experience. Experience 2-3 years of experience in DevOps, "
                  "Cloud Engineering, SRE, or System Administration. Fresh graduates with strong Kubernetes-focused internships, "
                  "home labs, or academic projects are encouraged to apply. " + LONG)


def test_alternative_route_is_preserved_and_marked_for_review():
    s = screen(job(title="Backend Engineer - Kubernetes", desc=ENTERPRISE_BOT), PROFILE)
    assert s.eligible and s.borderline and s.review and s.match == "possible"
    assert s.experience.required_mentions == ["2-3 years (description)"]
    assert "Fresh graduates" in s.experience.alternatives[0]
    assert any("REVIEW" in g and "Fresh graduates" in g for g in s.gaps)
    # same text with a higher numeric requirement is reviewed, not excluded, because another route is stated
    hi = screen(job(title="Backend Engineer", desc=ENTERPRISE_BOT.replace("2-3 years", "3-5 years")), PROFILE)
    assert hi.eligible and hi.review and any("above your limit" in g for g in hi.gaps)
    # without an alternative route a 3-5 year requirement is excluded
    assert not screen(job(title="Backend Engineer", desc=LONG + " Experience: 3-5 years."), PROFILE).eligible


def test_non_internship_contract_case_gaps_are_separate_and_contract_ranks_lower():
    amazon = screen(job(title="SDE I FTC", loc="Hyderabad, Telangana, India", desc=LONG +
                        " Basic qualifications: 1+ year of non-internship professional software development experience."), PROFILE)
    assert amazon.eligible and amazon.employment == "contract" and amazon.experience.required_min == 1
    assert amazon.experience.excludes_internship and amazon.gap_full_time_months == 0 and amazon.gap_total_months is None
    assert any("1 year" in g or "within your 14 full-time months" in " ".join(amazon.reasons) for g in amazon.gaps + amazon.reasons)
    perm = screen(job(title="SDE I", loc="Hyderabad, Telangana, India", desc=amazon.job["description"]), PROFILE)
    assert perm.fit > amazon.fit


def test_separate_full_time_and_total_gaps():
    s = screen(job(desc=LONG + " 1.5 years of experience required."), PROFILE)       # 18 months
    assert (s.gap_full_time_months, s.gap_total_months) == (4, 0)
    s2 = screen(job(desc=LONG + " Minimum 2 years of experience."), PROFILE)         # 24 months
    assert (s2.gap_full_time_months, s2.gap_total_months) == (10, 3)
    assert any("10 short of your 14 full-time months, 3 short of your 21 total" in g for g in s2.gaps)


def test_zero_to_one_year_backend_sde_is_a_strong_match_but_passing_filters_is_not_one():
    hevo = screen(job(title="SDE I", desc="Build backend services and APIs. Candidates with 0-1 years of experience. " + LONG), PROFILE)
    assert hevo.eligible and hevo.match == "strong"
    plain = screen(job(title="Support Engineer", desc="Handle customer queries and tickets. " * 20 + " Experience: 1 year."), PROFILE)
    assert plain.eligible and plain.match == "weak"                                   # passes filters, not a strong match
    unknown = screen(job(), PROFILE)
    assert unknown.eligible and unknown.match == "possible"
    ranked, _ = rank([plain, unknown, hevo])
    assert [x.match for x in ranked] == ["strong", "possible", "weak"]


def test_qa_roles_are_excluded_by_default_and_non_software_quality_keeps_its_specific_reason():
    qa = screen(job(title="Quality Assurance Engineer", desc="Test the product, write test cases and report defects. " * 12), PROFILE)
    assert not qa.eligible and "default SDE/backend shortlist" in qa.exclusions[0]
    lab = screen(job(title="Quality Assurance Specialist", desc="Laboratory audits of supplier quality and GMP inspection. " * 12), PROFILE)
    assert not lab.eligible and "non-software signals" in lab.exclusions[0]


def test_non_linkedin_job_without_linkedin_url_is_screened_and_keeps_its_links(tmp_path):
    m = load_script()
    d = tmp_path / "r1"; (d / "raw").mkdir(parents=True)
    j = job(jid="url:abc"); j.pop("url"); j["linkedin_job_id"] = None; j["job_key"] = "url:abc"
    j.update({"url": "https://careers.example.com/j/1", "application_url": "https://careers.example.com/j/1", "application_kind": "employer",
              "application_options": [{"title": "Example Careers", "link": "https://careers.example.com/j/1"}, {"title": "Jooble", "link": "https://in.jooble.org/1"}],
              "description_source": "Example Careers", "sources": [{"engine": "google_jobs", "via": "Example Careers"}]})
    (d / "results.json").write_text(json.dumps({"run_id": "r1", "jobs": [j]}))
    prof = tmp_path / "p.md"
    prof.write_text("## Experience\nEarly.\n\n## Desired roles\n- Backend Engineer\n\n## Must-haves\n\n## Nice-to-haves\n\n## Exclusions\n")
    assert m.main(["--profile", str(prof), "--runs", str(tmp_path), "--out", str(tmp_path)]) == 0
    r = json.loads((next(tmp_path.glob("screening-*")) / "ranked.json").read_text())["shortlist"][0]
    assert r["linkedin_url"] is None and r["application_link"] == "https://careers.example.com/j/1"
    assert len(r["all_application_links"]) == 2 and r["description_source"] == "Example Careers"


# ---- correction pass: discipline, requirement sentences, mixed levels, link kinds, readiness, age, markdown

BETTERWORKS = ("What’ll Help You Be Successful 3-5 years of manual testing experience with strong test case design skills. "
               "0-1+ years of experience with test automation tools like Cypress, Playwright, or WebdriverIO. "
               "2+ years of hands-on API testing, and debugging experience. Our CEO, Doug Dennerline is a mission-driven leader "
               "with over 25 years of sales leadership experience, making a direct impact. " + LONG)


def test_ceo_and_company_history_years_are_ignored_betterworks_excluded_for_its_real_requirement():
    e = parse_experience("SDET I", BETTERWORKS)
    assert e.required_min == 3 and "25 years (description)" not in " ".join(e.required_mentions)
    assert any("3-5 years" in x["years"] and "manual testing experience" in x["text"] for x in e.excerpts)
    s = screen(job(title="SDET I", desc=BETTERWORKS), PROFILE)
    assert not s.eligible and "minimum of 3 years" in s.exclusions[0] and "25" not in s.exclusions[0]


def test_we_are_looking_for_still_counts_as_a_requirement_but_company_boasts_do_not():
    e = parse_experience("", "We are looking for an engineer with 2 years of experience. We have more than 30 years of experience in IT services.")
    assert e.required_min == 2


def test_sde_title_alone_is_not_strong_for_frontend_or_data_science_but_stays_visible():
    fe = screen(job(title="SDE I - Frontend Engineer", desc=LONG + " React and Angular UI."), PROFILE)
    ds = screen(job(title="Data Science Engineer (SDE I)", desc=LONG + " Machine learning models, pandas, statistics."), PROFILE)
    be = screen(job(title="SDE I", desc="Build backend APIs and microservices in Python with SQL databases and REST. " * 8 + "Requirements: proficiency in Python and SQL."), PROFILE)
    assert (fe.discipline, ds.discipline, be.discipline) == ("frontend", "data_science", "backend")
    assert fe.eligible and ds.eligible and fe.match == "adjacent" and ds.match == "adjacent" and be.match == "strong"
    ranked, _ = rank([fe, ds, be])
    assert ranked[0] is be and {x.match for x in ranked[1:]} == {"adjacent"}          # adjacent roles remain visible
    assert screen(job(title="SDE I", desc="Join our team."), PROFILE).discipline == "unspecified"


def test_mixed_level_title_is_review_unless_every_stated_requirement_is_incompatible():
    t = "Software Engineer (SDE I, II, III)"
    s = screen(job(title=t, desc=LONG), PROFILE)
    assert s.eligible and s.review and any("mixed-level" in g for g in s.gaps)
    spread = screen(job(title=t, desc=LONG + " SDE I: 0-1 years of experience. SDE III: 5+ years of experience."), PROFILE)
    assert spread.eligible and spread.review                                         # lowest stated requirement is fine
    all_high = screen(job(title=t, desc=LONG + " Minimum 4 years of experience. 6+ years of experience for SDE III."), PROFILE)
    assert not all_high.eligible and "every stated requirement" in all_high.exclusions[0]
    assert not screen(job(title="Senior SDE I, II", desc=LONG), PROFILE).eligible    # other senior words still exclude
    assert not screen(job(title="SDE 3 / Staff Engineer", desc=LONG), PROFILE).eligible


def test_thin_description_is_incomplete_and_never_a_strong_match():
    thin = screen(job(title="Backend SDE-I", desc="Python backend role."), PROFILE)
    full = screen(job(title="Backend SDE-I", desc=LONG + " Backend APIs in Python. Requirements: proficiency in Python."), PROFILE)
    assert thin.readiness == "thin" and thin.match == "possible" and thin.eligible
    assert any("THIN description" in g and "nothing inferred" in g for g in thin.gaps)
    assert thin.experience.required_min is None and full.readiness == "content_ok" and full.match == "strong"


def test_application_link_kinds_employer_ats_job_board_unknown():
    from nemo.discovery import classify_option as c
    assert c("HP", "Careers At HP", {"title": "Careers At HP", "link": "https://apply.hp.com/careers/job/1"}) == "employer_ats"
    assert c("Enterprise Bot", None, {"link": "https://join.com/companies/enterprisebot/1"}) == "employer_ats"
    assert c("Acme Robotics", None, {"link": "https://careers.acmerobotics.com/j/1"}) == "employer_ats"
    assert c("CloudSEK", None, {"link": "https://peerlist.io/company/cloudsek/careers/backend"}) == "job_board"     # token in path only
    assert c("Uniqus", None, {"link": "https://www.efinancialcareers.com/jobs/x"}) == "job_board"
    assert c("Foo", None, {"link": "https://random-site.example/job/9"}) == "unknown"
    assert c("X", None, {"link": "https://in.linkedin.com/jobs/view/slug-4470000001"}) == "linkedin"


def test_reported_age_is_shown_and_unknown_dates_are_stated_without_a_date_exclusion():
    from nemo.runreport import reported_age
    j = job(); j.update({"best_reported_age": {"raw": "8 days ago"}, "lookback_status": "reported_outside"})
    assert reported_age(j) == "8 days ago (OUTSIDE the look-back (reported))"
    assert reported_age(job()) == "date unknown"
    old = job(); old.update({"best_reported_age": {"raw": "22 days ago"}, "lookback_status": "reported_outside"})
    assert screen(old, PROFILE).eligible                                              # no hard date rule without a profile requirement


def test_markdown_tables_escape_pipes_and_split_by_freshness():
    from nemo.runreport import cell, screening_sections
    assert cell("SDE I - Frontend | Bangalore, IN") == "SDE I - Frontend \\| Bangalore, IN"
    fresh = job(title="Backend SDE-I | Remote", desc=LONG + " Backend APIs in Python. 2 years of experience."); fresh["lookback_status"] = "reported_within"
    unknown = job(title="Backend SDE-I", desc=LONG + " Backend APIs in Python. 2 years of experience."); unknown["lookback_status"] = "unknown"
    old = job(title="Backend SDE-I old", desc=LONG + " Backend APIs in Python."); old["lookback_status"] = "reported_outside"
    md = "\n".join(screening_sections([screen(fresh, PROFILE), screen(unknown, PROFILE), screen(old, PROFILE)], [], "24h"))
    assert "## Fresh shortlist: reported within 24h" in md and "## Age unconfirmed" in md and "## Older than 24h: excluded from the fresh shortlist (records kept)" in md
    fresh_part, rest = md.split("## Age unconfirmed")
    assert "Backend SDE-I \\| Remote" in fresh_part and "Backend SDE-I old" not in fresh_part                 # confirmed only; pipe escaped
    unconf_part, old_part = rest.split("## Older than 24h")
    assert "Backend SDE-I |" in unconf_part and "Backend SDE-I old" in old_part
    row = next(l for l in md.splitlines() if "Backend SDE-I \\| Remote" in l and l.startswith("|"))
    assert row.count(" | ") == 9                                                      # pipes inside cells did not add columns
    assert "## Experience statements (exact JD excerpts)" in md and "2 years" in md


def test_body_keywords_alone_do_not_make_a_strong_match():
    s = screen(job(title="Support Engineer", desc=LONG + " Experience: 1 year."), PROFILE)     # tech words in body, title says support
    assert s.eligible and s.match != "strong" and s.review


def test_generic_application_link_is_flagged_for_review_and_the_record_is_kept():
    j = job(); j.update({"job_key": "k1", "application_url": "https://in.talent.com/view", "application_kind": "job_board",
                         "application_options": [{"title": "Talent", "link": "https://in.talent.com/view"},
                                                 {"title": "Other", "link": "https://example.com/jobs/12345678"}]})
    s = screen(j, PROFILE)
    assert s.eligible and s.application_status == "needs_review"
    assert any("REVIEW: application link not structurally valid" in g and "https://example.com/jobs/12345678" in g for g in s.gaps)
    ok = job(); ok.update({"application_url": "https://in.talent.com/view?id=639659631666733986"})
    assert screen(ok, PROFILE).application_status == "structurally_valid"
    from nemo.runreport import job_record
    assert job_record(s)["application_status"] == "needs_review" and job_record(s)["application_link"] == "https://in.talent.com/view"


def test_visakhapatnam_is_not_a_preferred_city_but_is_still_recognised_as_india():
    blr = screen(job(loc="Bengaluru, Karnataka"), PROFILE)
    hyd = screen(job(loc="Hyderabad, Telangana"), PROFILE)
    vizag = screen(job(loc="Visakhapatnam, Andhra Pradesh"), PROFILE)
    assert blr.fit == hyd.fit and vizag.fit < blr.fit                    # no preferred-location boost
    assert vizag.eligible and not any("not recognised as India" in g for g in vizag.gaps)   # historical/home city not excluded


def test_screening_rekeys_old_url_records_to_the_current_identity_on_read(tmp_path):
    from nemo.discovery import job_key
    m = load_script()
    url = "https://in.talent.com/view?id=639659631666733986&utm_source=g"
    legacy, new = job_key(None, {"url": url}, None, legacy=True), job_key(None, {"url": url}, None)
    for rid, key in (("r1", legacy), ("r2", new)):                      # same vacancy: one run stored before the fix, one after
        d = tmp_path / rid; (d / "raw").mkdir(parents=True)
        j = job(); j.pop("url"); j.update({"job_key": key, "linkedin_job_id": None, "url": url, "application_url": url, "application_kind": "job_board",
                                           "application_options": [{"title": "Talent", "link": url}], "description_source": "Talent",
                                           "sources": [{"engine": "google_jobs", "via": "Talent"}]})
        (d / "results.json").write_text(json.dumps({"run_id": rid, "jobs": [j]}))
    jobs, _ = m.load_saved(tmp_path)
    assert list(jobs) == [new] and jobs[new]["seen_in_runs"] == ["r1", "r2"]     # one job, history of both runs, files untouched


# ---- SDE / backend focus: responsibilities, not titles alone

BACKEND_JD = ("Design and build REST APIs and microservices in Python. Work with PostgreSQL and MongoDB databases, write scalable services "
              "and participate in system design and code reviews. Deploy on AWS with Docker and Kubernetes. " * 3
              + "Requirements: proficiency in Python and SQL.")
OPS_JD = ("Own infrastructure operations: provision infrastructure with Terraform and Ansible, manage Kubernetes cluster upgrades, "
          "maintain CI/CD pipelines in Jenkins, set up monitoring with Prometheus and Grafana, and join the on-call rotation. " * 3)
QA_JD = "Write test cases and test plans, build Selenium and Playwright automation, and run regression testing and manual testing. " * 4


def test_backend_title_needs_backend_responsibilities_to_be_strong():
    full = screen(job(title="Junior Backend Engineer", desc=BACKEND_JD), PROFILE)
    assert full.eligible and full.discipline == "backend" and full.match == "strong"
    assert any(r.startswith("backend responsibilities: APIs") for r in full.reasons) and any("cloud infrastructure" in r for r in full.reasons)
    title_only = screen(job(title="Junior Backend Engineer", desc="We are hiring a backend engineer to join the team. " * 12), PROFILE)
    assert title_only.eligible and title_only.discipline == "backend" and title_only.match == "possible"       # title alone is not strong


def test_generic_sde_title_is_classified_by_responsibilities_and_keeps_cloud_and_ai_software_roles():
    sde = screen(job(title="SDE I", desc=BACKEND_JD), PROFILE)
    ai = screen(job(title="Software Engineer", desc=BACKEND_JD + " Build LLM and RAG powered features with embeddings."), PROFILE)
    assert sde.discipline == "backend" and sde.eligible and ai.discipline == "backend" and ai.eligible
    assert any("AI product work" in r for r in ai.reasons)


def test_infrastructure_devops_sre_platform_cloud_titles_are_excluded_from_the_default_shortlist_with_reasons():
    for t in ("DevOps Engineer", "Junior DevOps Engineer", "Site Reliability Engineer", "SRE", "Platform Engineer", "Cloud Engineer",
              "Infrastructure Engineer", "Release Engineer"):
        s = screen(job(title=t, desc=BACKEND_JD), PROFILE)
        assert not s.eligible and s.exclusions[0].startswith("primarily infrastructure") and "default SDE/backend shortlist" in s.exclusions[0], t
        assert s.job["title"] == t                                                       # record kept for the audit trail


def test_qa_and_testing_titles_are_excluded_by_default():
    for t in ("SDET I", "QA Automation Engineer", "Software Engineer in Test", "Test Engineer", "Quality Assurance Engineer"):
        s = screen(job(title=t, desc=BACKEND_JD), PROFILE)
        assert not s.eligible and s.discipline == "qa_testing" and s.exclusions[0].startswith("QA / testing"), t


def test_primarily_ops_or_qa_responsibilities_exclude_a_generic_software_title_but_mixed_ones_go_to_review():
    ops = screen(job(title="Software Engineer", desc=OPS_JD), PROFILE)
    assert not ops.eligible and ops.discipline == "infra_ops" and "primarily infrastructure-operations responsibilities" in ops.exclusions[0]
    qa = screen(job(title="SDE I", desc=QA_JD), PROFILE)
    assert not qa.eligible and qa.discipline == "qa_testing" and "primarily QA / testing responsibilities" in qa.exclusions[0]
    mixed = screen(job(title="Software Engineer", desc="Build REST APIs in Python with a SQL database. " + OPS_JD), PROFILE)
    assert mixed.eligible and mixed.review and any("REVIEW: ambiguous role" in g and "mix backend work" in g for g in mixed.gaps)
    assert mixed.match != "strong"


def test_titles_mixing_software_and_infrastructure_or_ai_are_flagged_for_review_not_guessed():
    for t in ("Backend Developer & DevOps Engineer", "API Platform & DevOps Engineer", "Software Engineer - DevOps"):
        s = screen(job(title=t, desc=BACKEND_JD), PROFILE)
        assert s.eligible and s.review and any("REVIEW: ambiguous role: title mixes infrastructure" in g for g in s.gaps), t
    ai = screen(job(title="AI Engineer", desc="Work on models and experiments. " * 20), PROFILE)
    assert ai.eligible and ai.review and ai.discipline == "ai_ml" and ai.match != "strong"
    ai_backend = screen(job(title="AI Engineer", desc=BACKEND_JD), PROFILE)                # same title, backend responsibilities
    assert ai_backend.discipline == "backend" and not ai_backend.review
    vague = screen(job(title="Software Engineer", desc="Join our fast growing team and make an impact on our customers. " * 12), PROFILE)
    assert vague.eligible and vague.discipline == "unspecified" and vague.review


def test_adjacent_roles_stay_visible_and_kubernetes_skill_alone_is_not_infrastructure_work():
    fe = screen(job(title="SDE I - Frontend Engineer", desc=BACKEND_JD), PROFILE)
    assert fe.eligible and fe.match == "adjacent"
    skills = screen(job(title="Backend Engineer", desc="Python backend APIs and microservices. Experience with Docker, Kubernetes and Terraform is a plus. " * 5), PROFILE)
    assert skills.eligible and skills.discipline == "backend"                          # skills mentioned, not operated as the job


def test_sre_is_not_mistaken_for_a_senior_title_but_sr_is():
    from nemo.screening import _SENIOR_TITLE
    assert not _SENIOR_TITLE.search("SRE") and not _SENIOR_TITLE.search("Sri Backend Engineer")
    assert _SENIOR_TITLE.search("Sr. Software Engineer") and _SENIOR_TITLE.search("Sr Backend Engineer")


def test_description_sufficiency_comes_from_content_not_length():
    from nemo.screening import description_content as dc
    ok = dc("Responsibilities: design and build REST APIs. Build services in Python. Maintain databases and review code. Requirements: 1 year of experience with Python and SQL, plus a degree in computer science.")
    assert ok["level"] == "content_ok" and not ok["missing"]
    responsibilities_only = dc("Design and build REST APIs. Build backend services. Maintain databases and write documentation for the platform team.")
    assert responsibilities_only["level"] == "partial" and responsibilities_only["missing"] == ["requirements"]
    requirements_only = dc("Requirements: proficiency in Python, knowledge of SQL, experience with Docker. Bachelor's degree in computer science. " * 2)
    assert requirements_only["level"] == "partial" and requirements_only["missing"] == ["responsibilities"]
    blurb = dc("Acme is a global leader in insurance technology with offices in many countries and thousands of happy customers. " * 30)   # 3,000+ chars
    assert blurb["level"] == "thin" and len("x" * 3000) > 500                                     # long boilerplate is not a sufficient JD
    cut = dc("Responsibilities: design and build REST APIs. Build services. Maintain databases. Requirements: Python and SQL and distributed sys...")
    assert cut["level"] == "partial" and cut["truncated"] and "text is cut off" in cut["missing"]
    assert dc("")["level"] == "thin" and dc("Python backend role.")["level"] == "thin"
    s = screen(job(title="Backend Engineer", desc="Acme is a leader in many fields and loves its people and customers. " * 30), PROFILE)
    assert s.readiness == "thin" and s.match != "strong" and any("THIN description" in g for g in s.gaps)


def _shortlist_module():
    spec = importlib.util.spec_from_file_location("shortlist", pathlib.Path(__file__).parent.parent / "scripts" / "shortlist.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def _write_runs(tmp_path, jobs):
    d = tmp_path / "r1"; (d / "raw").mkdir(parents=True)
    (d / "results.json").write_text(json.dumps({"run_id": "r1", "jobs": jobs}))
    prof = tmp_path / "p.md"
    prof.write_text("## Experience\nEarly.\n\n## Desired roles\n- Backend Engineer\n\n## Must-haves\n\n## Nice-to-haves\n\n## Exclusions\n")
    return prof


def _jb(key, title, company, loc, links, via="A", desc=None, schedule=None):
    j = job(title=title, desc=desc or (BACKEND_JD + " Experience: 0-2 years."), loc=loc, jid="4470000001"); j.pop("url")
    j.update({"job_key": key, "company": company, "linkedin_job_id": None, "url": links[0], "application_url": links[0], "application_kind": "job_board",
              "description_source": via, "application_options": [{"title": via, "link": l} for l in links], "sources": [{"engine": "google_jobs", "via": via}]})
    if schedule:
        j["schedule_type"] = schedule
    return j


def test_shortlist_merges_only_on_shared_identifiers_flags_possible_duplicates_separates_internships_and_drops_vizag(tmp_path):
    m = _shortlist_module()
    jobs = [_jb("k1", "Backend Engineer", "Acme", "Bengaluru, Karnataka", ["https://board-a.example/jobs/12345678"], schedule="Full–time"),
            _jb("k2", "Backend Engineer at Acme", "Acme", "Bengaluru, Karnataka", ["https://board-b.example/view?id=987654"], via="B"),   # same company/title/city only
            _jb("k6", "Backend Engineer", "Acme", "Bengaluru, Karnataka", ["https://board-c.example/x/abcdef12", "https://board-a.example/jobs/12345678?utm_source=g"], via="C"),  # shares k1's URL
            _jb("k3", "Backend Engineer", "Acme", "Hyderabad, Telangana", ["https://board-a.example/jobs/22222222"]),
            _jb("k4", "Backend Engineer Intern", "Beta", "Bengaluru, Karnataka", ["https://board-a.example/jobs/33333333"]),
            _jb("k5", "Backend Engineer", "Gamma", "Visakhapatnam, Andhra Pradesh", ["https://board-a.example/jobs/44444444"])]
    prof = _write_runs(tmp_path, jobs)
    assert m.main(["--profile", str(prof), "--runs", str(tmp_path), "--out", str(tmp_path)]) == 0
    text = next(tmp_path.glob("shortlist-*.md")).read_text()
    full, rest = text.split("## Internships")
    assert "merged 2 records that share a posting identifier" in full                                         # k1 + k6 share a URL: confident merge
    assert "https://board-c.example/x/abcdef12" in full                                                      # merged record keeps the other link
    assert full.count("### Backend Engineer") == 3                                                           # k1+k6 merged, k2 and k3 separate
    assert full.count("POSSIBLE DUPLICATE") >= 2 and "NOT merged" in full and "https://board-b.example/view?id=987654" in full
    assert "### Backend Engineer Intern" in rest and "Gamma" not in text                                      # internships apart, Vizag dropped
    assert "Human-reviewed:** not yet" in text and "Employment type:** full-time (Google-reported: Full–time; unverified)" in full
    assert "Employment type:** NOT STATED" in full                                                            # unknown type is explicit, never assumed
    assert "https://board-a.example/jobs/12345678 (unknown; link checked for structure only)" in full          # complete link, unaltered


def test_shortlist_reads_google_employment_type_from_saved_raw_responses(tmp_path):
    m = _shortlist_module()
    link = "https://board-a.example/jobs/12345678"
    prof = _write_runs(tmp_path, [_jb("k1", "Backend Engineer", "Acme", "Bengaluru, Karnataka", [link + "?utm_source=g"])])
    (tmp_path / "r1" / "raw" / "01_google_jobs.json").write_text(json.dumps({"jobs_results": [
        {"extensions": ["2 days ago", "Full–time"], "apply_options": [{"title": "A", "link": link}]}]}))
    assert m.main(["--profile", str(prof), "--runs", str(tmp_path), "--out", str(tmp_path)]) == 0
    assert "full-time (Google-reported: Full–time; unverified)" in next(tmp_path.glob("shortlist-*.md")).read_text()
    (tmp_path / "r1" / "raw" / "01_google_jobs.json").write_text(json.dumps({"jobs_results": [
        {"extensions": ["Internship"], "apply_options": [{"title": "A", "link": link}]}]}))
    for old in tmp_path.glob("shortlist-*.md"):
        old.unlink()
    assert m.main(["--profile", str(prof), "--runs", str(tmp_path), "--out", str(tmp_path)]) == 0
    assert "internship (Google-reported only; the title and description do not say so: confirm)" in next(tmp_path.glob("shortlist-*.md")).read_text()


# ---- stack fit, AI-title scope, employment type

def test_required_stack_fit_is_separate_from_role_fit_and_gaps_stay_visible_but_not_strong():
    py = screen(job(title="Backend Engineer", desc=BACKEND_JD + " Experience: 0-2 years."), PROFILE)
    flexible = screen(job(title="Backend Engineer", desc="Design and build REST APIs and microservices with SQL databases. Maintain scalable services. "
                          "Requirements: strong backend fundamentals, system design and 1 year of experience. " * 2), PROFILE)
    java = screen(job(title="Backend Engineer", desc="Design and build REST APIs and microservices on Spring Boot with Hibernate and SQL databases. "
                      "Maintain scalable services and review code. Requirements: Java, Spring Boot and 1 year of experience. " * 2), PROFILE)
    assert (py.stack_fit, flexible.stack_fit, java.stack_fit) == ("python", "flexible", "gap")
    assert java.eligible and java.discipline == "backend" and any("STACK GAP: requires Java/Spring" in g and "Python is not mentioned" in g for g in java.gaps)
    assert java.match == "possible" and py.match == "strong"                                    # same role fit, but a stack gap is never 'strong'
    ranked, _ = rank([java, flexible, py])
    assert [x.stack_fit for x in ranked[:3]] == ["python", "flexible", "gap"] or ranked[-1] is java   # Python-compatible and flexible first
    plus = screen(job(title="Backend Engineer", desc=BACKEND_JD.replace("Python", "Java") + " Experience with Python is a plus."), PROFILE)
    assert plus.stack_fit == "gap" and plus.stack_note == "requires Java/Spring; Python is only listed as a plus"      # 'a plus' is not required
    multi = screen(job(title="Backend Engineer", desc="Strong proficiency in Go, Python, or Node.js. Build REST APIs and microservices with SQL. Maintain services. " * 2
                       + "Requirements: 1 year of experience."), PROFILE)
    assert multi.stack_fit == "python" and "also lists" in multi.stack_note and "Go" in multi.stack_note


def test_ai_titled_role_stays_only_when_the_responsibilities_support_backend_scope():
    cohort = ("Junior / Intern AI Engineers. Responsibilities: Complete a structured curriculum covering LLMs, Retrieval-Augmented Generation (RAG), "
              "autonomous multi-agent systems and Python frameworks. Write clean Python code. Build and benchmark vector search workflows, "
              "AI agent pipelines, and microservices. Collaborate in code reviews. Qualifications: Python, REST APIs, data structures and system design.")
    s = screen(job(title="Junior Engineer-AI", desc=cohort), PROFILE)
    assert s.discipline == "ai_ml" and s.review and s.employment == "internship"
    assert any("centre on AI engineering" in g and "moved out of the default SDE/backend shortlist" in g for g in s.gaps)
    backend_ai = screen(job(title="AI Engineer", desc=BACKEND_JD + " Support an LLM feature."), PROFILE)            # one AI signal, strong backend evidence
    assert backend_ai.discipline == "backend" and not backend_ai.review
    assert screen(job(title="Software Engineer", desc=BACKEND_JD + " Build LLM and RAG powered features with agents and embeddings."), PROFILE).discipline == "backend"   # generic SDE title with AI product work stays in


def test_employment_type_uses_google_label_but_internship_or_contract_wording_wins_and_unknown_stays_unknown():
    from nemo.screening import employment_type as et
    assert et("Backend Engineer", "Build services.", "Full–time") == "full_time"
    assert et("Backend Engineer", "Build services.", None) == "unspecified" and et("Backend Engineer", "Build services.", "") == "unspecified"
    assert et("Backend Intern", "x", "Full–time") == "internship" and et("Backend Engineer", "x", "Internship") == "internship"
    assert et("SDE I FTC", "x", "Full–time") == "contract" and et("Backend Engineer", "x", "Contractor") == "contract"
    j = job(); j["schedule_type"] = "Full–time"
    assert screen(j, PROFILE).employment == "full_time" and screen(job(), PROFILE).employment == "unspecified"


def test_chosen_cities_screen_the_actual_location():
    from nemo.screening import location_fit
    cities = ("Bengaluru", "Hyderabad")
    assert location_fit("Bengaluru, Karnataka", cities)[0] == "chosen" and location_fit("Bangalore Urban, Karnataka", cities)[0] == "chosen"
    assert location_fit("Secunderabad, Telangana", cities)[0] == "chosen"                      # part of the Hyderabad metro
    assert location_fit("Hyderabad, Telangana (+1 other)", cities)[0] == "chosen"
    assert location_fit("Remote", cities)[0] == "remote" and location_fit("India", cities)[0] == "unknown" and location_fit("Karnataka", cities)[0] == "unknown"
    assert location_fit("Pune, Maharashtra", cities) == ("other", "pune") and location_fit("Gurugram, Haryana (+1 other)", cities)[0] == "other"
    pune = screen(job(loc="Pune, Maharashtra", desc=BACKEND_JD), PROFILE, chosen_cities=cities)
    assert not pune.eligible and "outside the chosen cities (Bengaluru, Hyderabad)" in pune.exclusions[0]
    assert screen(job(loc="Secunderabad, Telangana", desc=BACKEND_JD), PROFILE, chosen_cities=cities).eligible
    vague = screen(job(loc="India", desc=BACKEND_JD), PROFILE, chosen_cities=cities)
    assert vague.eligible and any("unknown: no city stated" in g for g in vague.gaps)              # kept and flagged, not guessed
    assert screen(job(loc="Pune, Maharashtra", desc=BACKEND_JD), PROFILE).eligible                  # no chosen cities supplied: rule is off
