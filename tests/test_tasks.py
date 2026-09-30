from nemo.profile import parse_profile
from nemo.providers.fake import FakeLLM
from nemo.tasks import (CriterionResult, assess_job, content_level, finalize_outcome, fit_text, ground_quotes,
                        render_profile, resolve_criteria)

PROFILE = parse_profile("""## Experience
Backend engineer, 6 years, Python and PostgreSQL.
## Desired roles
- Backend Engineer
## Must-haves
- Python is a main language
- Remote
## Nice-to-haves
- PostgreSQL
## Exclusions
- Crypto industry
""")
JOB = "Senior Backend Engineer. You will build Python services and tune PostgreSQL queries. 5+ years required."
LONG_JOB = JOB + " " + "We value clear writing, code review and steady on-call practice. " * 12


def llm(data):
    return FakeLLM(lambda s, p: data)


def answer(outcome="strong_match", fit=("build Python services",), mismatch=(), criteria=None, **kw):
    return {"outcome": outcome, "explanation": "x", "fit_quotes": list(fit), "mismatch_quotes": list(mismatch),
            "criteria": criteria if criteria is not None else [], "limitations": ["location not stated"], **kw}


def C(cid, kind, status, quote=""):
    return CriterionResult(cid, kind, "t", status, quote)


def test_render_profile_lists_every_criterion_id():
    r = render_profile(PROFILE)
    assert "M1 (must-have): Python is a main language" in r and "N1 (nice-to-have): PostgreSQL" in r
    assert "X1 (exclusion): Crypto industry" in r and "DESIRED ROLES: Backend Engineer" in r


def test_missing_or_unquoted_criteria_are_unknown():
    raw = [{"id": "m1", "status": "yes", "quote": "build Python services"},
           {"id": "M2", "status": "yes", "quote": "fully remote team"},      # not in the text -> unknown
           {"id": "X1", "status": "maybe", "quote": ""}]                      # bad status -> unknown; N1 missing
    res, dropped = resolve_criteria(PROFILE, raw, JOB)
    by = {c.id: c for c in res}
    assert by["M1"].status == "yes" and by["M2"].status == "unknown" and "without a verifiable quote" in by["M2"].note
    assert by["N1"].status == "unknown" and by["N1"].note == "not answered" and by["X1"].status == "unknown"
    assert dropped == ["fully remote team"]
    assert resolve_criteria(PROFILE, "garbage", JOB)[0][0].status == "unknown"


def test_finalize_blocking_rules():
    assert finalize_outcome("strong_match", ["q"], [], [C("M1", "must_have", "no", "q")], "full_description") == \
        ("not_relevant", "blocked by M1")
    assert finalize_outcome("strong_match", ["q"], [], [C("X1", "exclusion", "yes", "q")], "full_description")[0] == "not_relevant"
    # unknown must-have / exclusion never blocks:
    assert finalize_outcome("possible_match", ["q"], [], [C("M1", "must_have", "unknown"), C("X1", "exclusion", "unknown")],
                            "full_description")[0] == "possible_match"


def test_finalize_caps_and_insufficient_evidence():
    assert finalize_outcome("strong_match", ["q"], [], [C("M1", "must_have", "unknown")], "full_description") == \
        ("possible_match", "strong_match capped at possible_match: unknown must-have(s) M1")
    assert finalize_outcome("strong_match", ["q"], [], [], "snippet")[0] == "possible_match"
    assert finalize_outcome("strong_match", ["q"], [], [C("M1", "must_have", "yes", "q")], "full_description")[0] == "strong_match"
    assert finalize_outcome("possible_match", [], [], [], "full_description")[0] == "insufficient_evidence"
    assert finalize_outcome("possible_match", [], [], [C("M1", "must_have", "yes", "q")], "full_description")[0] == "possible_match"
    assert finalize_outcome("not_relevant", [], [], [], "full_description")[0] == "insufficient_evidence"   # never irrelevance
    assert finalize_outcome("not_relevant", [], ["q"], [], "partial_description")[0] == "not_relevant"
    assert finalize_outcome("insufficient_evidence", ["q"], ["q"], [], "snippet")[0] == "insufficient_evidence"


def test_assess_end_to_end_with_unknown_must_have_caps_strong():
    crit = [{"id": "M1", "status": "yes", "quote": "build Python services"},
            {"id": "M2", "status": "unknown", "quote": ""},
            {"id": "N1", "status": "yes", "quote": "tune PostgreSQL queries"},
            {"id": "X1", "status": "no", "quote": ""}]                     # 'no' without quote -> unknown
    r = assess_job(llm(answer(criteria=crit)), PROFILE, LONG_JOB)
    assert r.outcome == "possible_match" and "M2" in r.note and r.content_level == "partial_description"
    assert [c.status for c in r.criteria] == ["yes", "unknown", "yes", "unknown"]


def test_assess_verified_exclusion_blocks():
    crit = [{"id": "X1", "status": "yes", "quote": "tune PostgreSQL queries"}]
    assert assess_job(llm(answer(criteria=crit)), PROFILE, LONG_JOB).outcome == "not_relevant"


def test_assess_hallucinated_irrelevance_becomes_insufficient_evidence():
    r = assess_job(llm(answer("not_relevant", fit=(), mismatch=("requires nursing licence",))), PROFILE, JOB)
    assert r.outcome == "insufficient_evidence" and r.dropped_quotes == ["requires nursing licence"]


def test_assess_unrecognised_outcome_and_empty_output_do_not_crash():
    r = assess_job(llm({"outcome": "great!"}), PROFILE, JOB)
    assert r.outcome == "insufficient_evidence" and "unrecognised" in r.note and len(r.criteria) == 4
    assert assess_job(llm({}), PROFILE, JOB).outcome == "insufficient_evidence"


def test_job_text_is_truncated_to_fit():
    class Tiny(FakeLLM):
        def available_prompt_chars(self, system, schema):
            return 900
    fake = Tiny(lambda s, p: answer("insufficient_evidence", fit=()))
    r = assess_job(fake, PROFILE, JOB * 50)
    assert r.job_truncated and len(fake.prompts[0]) <= 900 and fake.prompts[0].startswith("JOB PROFILE")


def test_helpers():
    assert ground_quotes(["build  Python services", "tune MySQL queries", "5+"], JOB) == (["build  Python services"], ["tune MySQL queries", "5+"])
    assert content_level("x" * 100) == "snippet" and content_level("x" * 800) == "partial_description"
    assert content_level("x" * 2500) == "full_description"
    assert fit_text("  abc ", 10) == ("abc", False) and fit_text("abcdef", 3) == ("abc", True)
