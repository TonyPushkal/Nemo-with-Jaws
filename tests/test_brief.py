import pytest

from conftest import FILLED
from nemo.brief import TEMPLATE, BriefError, check_brief, parse_brief
from nemo.providers import AVAILABLE


def test_template_parses_and_is_empty():
    b = parse_brief(TEMPLATE)
    assert b.search.target_titles == [] and b.hard.locations == [] and b.preferred.skills == []
    assert b.about_me.summary is None and b.hard.min_salary is None
    assert b.budgets.paid_calls_enabled is False and b.budgets.max_usd is None
    assert b.providers.search is None and b.providers.llm is None


def test_hard_and_preferred_must_both_be_present():  # A1
    with pytest.raises(BriefError, match="hard"):
        parse_brief("version: 1\npreferred: {}\n")
    with pytest.raises(BriefError, match="preferred"):
        parse_brief("version: 1\nhard: {}\n")


def test_skill_in_both_hard_and_preferred_is_error():
    with pytest.raises(BriefError, match="both hard and preferred"):
        parse_brief("version: 1\nhard: {must_skills: [Python]}\npreferred: {skills: [python]}\n")


def test_unknown_keys_are_errors():
    with pytest.raises(BriefError, match="hrad"):
        parse_brief("version: 1\nhrad: {}\nhard: {}\npreferred: {}\n")


def test_bad_yaml_and_non_mapping():
    with pytest.raises(BriefError):
        parse_brief("a: [unclosed")
    with pytest.raises(BriefError, match="mapping"):
        parse_brief("- a\n- b\n")


def test_paid_calls_need_a_ceiling():  # A14
    with pytest.raises(BriefError, match="max_usd"):
        parse_brief("version: 1\nhard: {}\npreferred: {}\nbudgets: {paid_calls_enabled: true}\n")
    b = parse_brief("version: 1\nhard: {}\npreferred: {}\nbudgets: {paid_calls_enabled: true, max_usd: 5}\n")
    assert b.budgets.max_usd == 5


def test_check_reports_missing_titles_without_inventing_defaults():
    findings = check_brief(parse_brief(TEMPLATE), available_providers=AVAILABLE)
    assert any(f.level == "error" and "target_titles" in f.message for f in findings)
    assert any("paid calls are disabled" in f.message for f in findings)


def test_check_filled_brief_has_no_errors_and_flags_unimplemented_provider():
    b = parse_brief(FILLED + "providers: {search: acme-search}\n")
    findings = check_brief(b, available_providers=AVAILABLE)
    assert not [f for f in findings if f.level == "error"]
    assert any("not implemented yet" in f.message for f in findings)


def test_missing_resume_file_is_error(tmp_path):
    b = parse_brief(FILLED + f"about_me: {{resume_path: {tmp_path / 'nope.pdf'}}}\n")
    assert any(f.level == "error" and "resume_path" in f.message for f in check_brief(b))
