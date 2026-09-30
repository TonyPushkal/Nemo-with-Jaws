import pytest

from nemo.linkedin import canonical_job_url, linkedin_job_id, path_shape


@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/jobs/view/3812345678", "3812345678"),
    ("https://www.linkedin.com/jobs/view/3812345678/?trk=abc", "3812345678"),
    ("https://uk.linkedin.com/jobs/view/widget-engineer-at-acme-3812345678", "3812345678"),
    ("http://linkedin.com/jobs/view/widget-engineer-3812345678?refId=x", "3812345678"),
])
def test_accepts_individual_job_urls(url, expected):
    assert linkedin_job_id(url) == expected


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/jobs/search/?keywords=engineer",
    "https://www.linkedin.com/jobs/collections/recommended/",
    "https://www.linkedin.com/company/acme/jobs/",
    "https://www.linkedin.com/posts/someone_activity-12345678901",
    "https://www.linkedin.com/jobs/view/",
    "https://www.linkedin.com/jobs/view/123",              # too short to be an id
    "https://notlinkedin.com/jobs/view/3812345678",
    "https://linkedin.com.evil.example/jobs/view/3812345678",
    "https://example.com/jobs/view/3812345678",
])
def test_rejects_everything_else(url):
    assert linkedin_job_id(url) is None


def test_canonical_and_shape():
    assert canonical_job_url("3812345678") == "https://www.linkedin.com/jobs/view/3812345678"
    assert path_shape("https://www.linkedin.com/jobs/view/widget-at-acme-3812345678") == "linkedin.com/jobs/view/<slug>-<n>"
    assert path_shape("https://www.linkedin.com/jobs/view/3812345678") == "linkedin.com/jobs/view/<n>"
    assert path_shape("https://www.linkedin.com/company/acme/jobs/") == "linkedin.com/company/acme/jobs"
