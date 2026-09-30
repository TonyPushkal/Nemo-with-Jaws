import json

from nemo.providers.base import LLMResult
from nemo.providers.fake import FakeLLM
from nemo.tasks import (ASSESS_SCHEMA, assess_job, content_level, extract_profile, finalize_outcome, fit_text,
                        ground_quotes, validate_profile)

RESUME = "Sam Example\nBackend engineer. Skills: Python, PostgreSQL, Kubernetes. 6 years experience. Springfield."
GOOD = {"titles": ["Backend Engineer"], "related_titles": ["Platform Engineer"], "skills": ["Python", "PostgreSQL", "Rust"],
        "seniority": "senior", "years_experience": 6, "domains": ["web services"], "location_in_resume": "Springfield",
        "search_queries": ["senior backend engineer python"]}
JOB = "Senior Backend Engineer. You will build Python services and tune PostgreSQL queries. 5+ years required."


def llm_returning(data):
    return FakeLLM(lambda s, p: data)


def test_profile_ok_and_flags_ungrounded_skills():
    r = extract_profile(llm_returning(GOOD), RESUME)
    assert r.problems == [] and r.profile["seniority"] == "senior"
    assert r.ungrounded_skills == ["Rust"]          # not in the résumé: reported, not silently kept as fact
    assert not r.resume_truncated


def test_profile_validation_reports_problems_and_repairs_types():
    bad = {"titles": "Engineer", "skills": ["Python", 3, ""], "seniority": "wizard", "years_experience": "six"}
    profile, problems, _ = validate_profile(bad, RESUME)
    assert profile["seniority"] == "unknown" and profile["years_experience"] is None and profile["skills"] == ["Python"]
    assert any("missing key" in p for p in problems) and any("no titles" in p for p in problems)


def test_resume_is_truncated_to_the_provider_room_and_reported():
    class Tiny(FakeLLM):
        def available_prompt_chars(self, system, schema):
            return 300
    llm = Tiny(lambda s, p: GOOD)
    r = extract_profile(llm, RESUME * 20)
    assert r.resume_truncated and len(llm.prompts[0]) <= 300


def test_ground_quotes_verbatim_after_whitespace_and_case_normalisation():
    kept, dropped = ground_quotes(["build  Python services", "tune postgres queries", "5+", "Senior Backend Engineer"], JOB)
    assert kept == ["build  Python services", "Senior Backend Engineer"]
    assert dropped == ["tune postgres queries", "5+"]     # a paraphrase, and a fragment too short to mean anything


def test_finalize_outcome_rules():
    assert finalize_outcome("strong_match", ["q"], [], "full_description")[0] == "strong_match"
    assert finalize_outcome("strong_match", ["q"], [], "snippet")[0] == "possible_match"
    assert finalize_outcome("possible_match", [], [], "full_description")[0] == "insufficient_evidence"
    assert finalize_outcome("not_relevant", [], [], "full_description")[0] == "insufficient_evidence"   # never irrelevance
    assert finalize_outcome("not_relevant", [], ["q"], "partial_description")[0] == "not_relevant"
    assert finalize_outcome("insufficient_evidence", ["q"], ["q"], "snippet")[0] == "insufficient_evidence"


def test_assess_grounded_match_keeps_quotes_and_level():
    llm = llm_returning({"outcome": "strong_match", "explanation": "Same work.", "fit_quotes": ["build Python services"],
                         "mismatch_quotes": [], "limitations": ["location not stated"]})
    long_job = JOB + " " + "We value clear writing, code review and steady on-call practice. " * 12
    r = assess_job(llm, GOOD, long_job)
    assert r.outcome == "strong_match" and r.fit_quotes == ["build Python services"]
    assert r.content_level == "partial_description" and r.limitations == ["location not stated"]


def test_assess_snippet_strong_is_capped_and_ungrounded_claims_are_dropped():
    llm = llm_returning({"outcome": "strong_match", "explanation": "x", "fit_quotes": ["build Python services", "made up sentence here"],
                         "mismatch_quotes": [], "limitations": []})
    r = assess_job(llm, GOOD, JOB)
    assert r.outcome == "possible_match" and "snippet" in r.note and r.dropped_quotes == ["made up sentence here"]


def test_assess_hallucinated_irrelevance_becomes_insufficient_evidence():
    llm = llm_returning({"outcome": "not_relevant", "explanation": "x", "fit_quotes": [],
                         "mismatch_quotes": ["requires nursing licence"], "limitations": []})
    r = assess_job(llm, GOOD, JOB)
    assert r.outcome == "insufficient_evidence" and r.model_outcome == "not_relevant"


def test_assess_unrecognised_outcome_and_missing_fields_do_not_crash():
    r = assess_job(llm_returning({"outcome": "great!"}), GOOD, JOB)
    assert r.outcome == "insufficient_evidence" and "unrecognised" in r.note
    assert assess_job(llm_returning({}), GOOD, JOB).outcome == "insufficient_evidence"


def test_job_text_is_truncated_to_fit_and_prompt_excludes_nothing_else():
    class Tiny(FakeLLM):
        def available_prompt_chars(self, system, schema):
            return 700
    llm = Tiny(lambda s, p: {"outcome": "insufficient_evidence", "explanation": "", "fit_quotes": [], "mismatch_quotes": [],
                             "limitations": []})
    r = assess_job(llm, GOOD, JOB * 50)
    assert r.job_truncated and len(llm.prompts[0]) <= 700 and llm.prompts[0].startswith("CANDIDATE PROFILE")


def test_content_level_thresholds():
    assert content_level("x" * 100) == "snippet" and content_level("x" * 800) == "partial_description"
    assert content_level("x" * 2500) == "full_description"


def test_fit_text():
    assert fit_text("  abc ", 10) == ("abc", False) and fit_text("abcdef", 3) == ("abc", True)
