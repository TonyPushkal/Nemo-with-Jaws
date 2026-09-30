from pathlib import Path

import pytest

from nemo.profile import TEMPLATE, JobProfile, ProfileError, load_profile, parse_profile, search_queries

FIX = Path(__file__).parent / "fixtures" / "synthetic_profile.md"

MIN = """## Experience
Did things.
## Desired roles
- Engineer
## Must-haves
## Nice-to-haves
## Exclusions
"""


def test_fixture_parses_into_the_five_parts():
    p = load_profile(FIX)
    assert p.experience.startswith("Backend software engineer, 6 years.") and "\n" not in p.experience
    assert p.desired_roles == ("Senior Backend Engineer", "Platform Engineer")
    assert p.must_haves == ("Python is a main language of the role", "Remote or hybrid, based in Exampleland")
    assert p.nice_to_haves == ("PostgreSQL", "Mentoring other engineers")
    assert len(p.exclusions) == 2
    assert [c[0] for c in p.criteria()] == ["M1", "M2", "N1", "N2", "X1", "X2"]


def test_minimal_profile_with_empty_sections_and_aliases():
    p = parse_profile(MIN.replace("## Must-haves", "## Requirements:").replace("## Exclusions", "## Deal-breakers"))
    assert p.must_haves == () and p.exclusions == () and p.criteria() == []


def test_bullets_numbering_continuations_comments_and_dedup():
    text = MIN.replace("- Engineer", "1. Engineer\n2) Analyst\n* engineer\n<!-- hidden -->") \
              .replace("## Nice-to-haves", "## Nice-to-haves\n- Long preference that\n  continues here")
    p = parse_profile(text)
    assert p.desired_roles == ("Engineer", "Analyst") and p.nice_to_haves == ("Long preference that continues here",)


@pytest.mark.parametrize("text,msg", [
    (MIN.replace("## Exclusions\n", ""), "missing section(s): exclusions"),
    (MIN.replace("## Experience\nDid things.", "## Experience"), "Experience is empty"),
    (MIN.replace("- Engineer", ""), "Desired roles is empty"),
    (MIN + "## Hobbies\n- x\n", "unknown heading"),
    (MIN + "## Exclusions\n- x\n", "appears twice"),
    ("intro\n" + MIN, "text before the first"),
    (MIN.replace("## Must-haves", "## Must-haves\nPython"), "one item per bullet"),
    (MIN.replace("## Must-haves", "## Must-haves\n- Remote").replace("## Exclusions", "## Exclusions\n- remote"),
     "same item in must_haves and exclusions"),
    (MIN.replace("## Must-haves", "## Must-haves\n" + "".join(f"- item {i}\n" for i in range(7))), "at most 6"),
    (MIN.replace("## Must-haves", "## Must-haves\n- " + "x" * 200), "longer than"),
    (MIN.replace("## Desired roles", "### Desired roles"), "unknown heading"),
    (MIN.replace("Did things.", "x " * 590).replace("## Must-haves", "## Must-haves\n" + "".join(f"- {'y' * 150} {i}\n" for i in range(6)))
                              .replace("## Nice-to-haves", "## Nice-to-haves\n" + "".join(f"- {'z' * 150} {i}\n" for i in range(6))),
     "in total"),
])
def test_invalid_profiles_give_clear_errors(text, msg):
    with pytest.raises(ProfileError, match=msg.replace("(", r"\(").replace(")", r"\)")):
        parse_profile(text)


def test_utf8_bom_crlf_and_non_utf8(tmp_path):
    f = tmp_path / "p.md"
    f.write_bytes(("﻿" + MIN.replace("Did things.", "Zürich — naïve café")).replace("\n", "\r\n").encode("utf-8"))
    assert load_profile(f).experience == "Zürich — naïve café"
    f.write_bytes(MIN.replace("Did things.", "caf\xe9").encode("latin-1"))
    with pytest.raises(ProfileError, match="not valid UTF-8"):
        load_profile(f)
    with pytest.raises(ProfileError, match="cannot read"):
        load_profile(tmp_path / "missing.md")


def test_template_is_empty_and_rejected_until_filled():
    with pytest.raises(ProfileError, match="Experience is empty"):
        parse_profile(TEMPLATE)


def test_search_queries_are_one_per_role_on_job_view_urls():
    assert search_queries(load_profile(FIX)) == ["site:linkedin.com/jobs/view/ Senior Backend Engineer",
                                                 "site:linkedin.com/jobs/view/ Platform Engineer"]


def test_profile_is_frozen_and_hash_is_stable():
    p = load_profile(FIX)
    with pytest.raises(Exception):
        p.experience = "x"
    assert p.sha256() == load_profile(FIX).sha256() and p.sha256() != parse_profile(MIN).sha256()
    assert isinstance(p, JobProfile)
