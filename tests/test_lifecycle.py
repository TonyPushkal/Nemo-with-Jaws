from datetime import date, datetime, timedelta

from nemo.lifecycle import Observation as O, Signal as S, age_verdict, decide_status
from nemo.models import DateBasis, JobDates, JobStatus

T0 = datetime(2026, 9, 1, 12)


def status(*obs):
    return decide_status(list(obs)).status


def test_no_observations_is_unknown():
    assert status() is JobStatus.UNKNOWN


def test_each_strong_signal_alone_closes():  # A13
    for s in (S.EXPLICIT_CLOSED_TEXT, S.STRUCTURED_EXPIRED, S.HTTP_GONE, S.ATS_LISTING_ABSENT):
        assert status(O(s, T0)) is JobStatus.CLOSED


def test_single_404_or_redirect_is_only_possibly_closed():
    assert status(O(S.HTTP_NOT_FOUND, T0)) is JobStatus.POSSIBLY_CLOSED
    assert status(O(S.REDIRECT_TO_LISTING, T0)) is JobStatus.POSSIBLY_CLOSED


def test_weak_signals_within_a_day_do_not_close():
    assert status(O(S.HTTP_NOT_FOUND, T0), O(S.REDIRECT_TO_LISTING, T0 + timedelta(hours=2))) is JobStatus.POSSIBLY_CLOSED


def test_weak_signals_24h_apart_close():
    assert status(O(S.HTTP_NOT_FOUND, T0), O(S.HTTP_NOT_FOUND, T0 + timedelta(hours=24))) is JobStatus.CLOSED


def test_absence_from_search_or_recent_runs_never_closes():
    obs = [O(S.NOT_IN_SEARCH_RESULTS, T0 + timedelta(days=i)) for i in range(10)]
    obs += [O(S.NOT_SEEN_RECENT_RUNS, T0 + timedelta(days=i)) for i in range(10)]
    assert status(*obs) is JobStatus.POSSIBLY_CLOSED


def test_later_open_fetch_discards_earlier_closing_signals():
    assert status(O(S.EXPLICIT_CLOSED_TEXT, T0), O(S.FETCHED_OPEN, T0 + timedelta(days=2))) is JobStatus.OPEN
    later_close = status(O(S.FETCHED_OPEN, T0), O(S.HTTP_GONE, T0 + timedelta(days=1)))
    assert later_close is JobStatus.CLOSED


TODAY = date(2026, 9, 30)


def test_age_uses_posted_date_only_when_known():  # A12
    old = JobDates(posted_at=date(2026, 1, 1), posted_basis=DateBasis.STRUCTURED, updated_at=TODAY)
    assert age_verdict(old, TODAY, 30) == "stale"
    new = JobDates(posted_at=date(2026, 9, 20), posted_basis=DateBasis.STRUCTURED)
    assert age_verdict(new, TODAY, 30) == "within_window"


def test_recent_update_alone_does_not_prove_recent_posting():  # A12
    d = JobDates(updated_at=date(2026, 9, 29), updated_basis=DateBasis.ATS_API)
    assert age_verdict(d, TODAY, 30) == "posting_date_unknown"


def test_old_update_proves_old_posting():
    d = JobDates(updated_at=date(2026, 1, 1), updated_basis=DateBasis.ATS_API)
    assert age_verdict(d, TODAY, 30) == "stale"


def test_no_dates_or_no_window():
    assert age_verdict(JobDates(), TODAY, 30) == "posting_date_unknown"
    assert age_verdict(JobDates(), TODAY, None) == "within_window"


def test_dates_are_separate_fields():
    d = JobDates(updated_at=date(2026, 9, 1))
    assert d.posted_at is None and d.posted_basis is DateBasis.UNKNOWN
