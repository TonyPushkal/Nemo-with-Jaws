from datetime import datetime, timezone
from decimal import Decimal

import pytest

from nemo.budget import (Budget, BudgetConfig, BudgetConfigError, BudgetExceeded, PaidCallsDisabled,
                         UnpricedPaidProvider, usd_to_micro)
from nemo.ledger import InMemoryLedger, SqliteLedger
from nemo.providers.base import (FetchResult, FetchStatus, Hit, LLMProvider, PageFetcher, ProviderError,
                                 SearchProvider)
from nemo.providers.fake import FakeFetcher, FakeLLM, FakeSearchProvider
from nemo.providers.guard import GuardedFetcher, GuardedLLM, GuardedSearch


class Clock:
    t = 0.0
    def __call__(self):
        return self.t


PAID = dict(paid_calls_enabled=True, max_usd_per_run="1.00")


def paid_budget(**kw):
    ledger = kw.pop("ledger", None)
    return Budget(BudgetConfig(**{**PAID, **kw}), ledger=ledger)


def test_counted_caps_stop_cleanly_and_name_the_cap():
    b = Budget(BudgetConfig(max_queries=2))
    for _ in range(2):
        with b.call("queries", "p", is_paid=False):
            pass
    with pytest.raises(BudgetExceeded) as e:
        with b.call("queries", "p", is_paid=False):
            pass
    assert e.value.cap == "max_queries" and b.used["queries"] == 2


def test_wall_clock_cap():
    clock = Clock()
    b = Budget(BudgetConfig(max_wall_seconds=10), clock=clock)
    with b.call("queries", "p", is_paid=False):
        pass
    clock.t = 10
    with pytest.raises(BudgetExceeded) as e:
        with b.call("queries", "p", is_paid=False):
            pass
    assert e.value.cap == "max_wall_seconds"


def test_config_rejects_paid_without_ceiling_and_bad_money():
    with pytest.raises(BudgetConfigError):
        BudgetConfig(paid_calls_enabled=True)
    with pytest.raises(BudgetConfigError):
        BudgetConfig(max_usd_per_run="-1")
    with pytest.raises(BudgetConfigError):
        BudgetConfig(max_usd_per_run="abc")
    with pytest.raises(BudgetConfigError):
        Budget(BudgetConfig(monthly_usd_cap="5"))  # monthly cap needs a ledger


def test_config_from_env_defaults_to_paid_disabled():
    cfg = BudgetConfig.from_env({})
    assert cfg.paid_calls_enabled is False and cfg.max_usd_per_run is None
    cfg = BudgetConfig.from_env({"NEMO_PAID_CALLS_ENABLED": "true", "NEMO_MAX_USD_PER_RUN": "0.5",
                                 "NEMO_MONTHLY_USD_CAP": "3", "NEMO_MAX_QUERIES": "4"})
    assert cfg.max_usd_per_run == Decimal("0.5") and cfg.monthly_usd_cap == 3 and cfg.max_queries == 4
    with pytest.raises(BudgetConfigError):
        BudgetConfig.from_env({"NEMO_PAID_CALLS_ENABLED": "true"})


def test_paid_calls_refused_by_default_and_provider_not_called():
    inner = FakeSearchProvider({"q": [Hit("u", "t", "s")]}, is_paid=True, max_cost_usd=0.01)
    with pytest.raises(PaidCallsDisabled):
        GuardedSearch(inner, Budget(BudgetConfig())).search("q")
    assert inner.queries == []


def test_unpriced_paid_provider_refused():  # a 0 estimate must not bypass the ceiling
    inner = FakeSearchProvider(is_paid=True, max_cost_usd=0.0)
    with pytest.raises(UnpricedPaidProvider):
        GuardedSearch(inner, paid_budget()).search("q")
    assert inner.queries == []


def test_run_ceiling_counts_reservations_and_blocks_before_the_call():
    inner = FakeSearchProvider({"q": []}, is_paid=True, max_cost_usd=0.40)
    g = GuardedSearch(inner, paid_budget())
    g.search("q"); g.search("q")                    # 0.80 committed at worst case
    with pytest.raises(BudgetExceeded) as e:
        g.search("q")                               # 1.20 > 1.00
    assert e.value.cap == "max_usd_per_run" and len(inner.queries) == 2


def test_actual_cost_below_reservation_frees_headroom():
    inner = FakeSearchProvider({"q": []}, is_paid=True, max_cost_usd=0.40, actual_cost_usd=0.10)
    b = paid_budget()
    g = GuardedSearch(inner, b)
    for _ in range(3):
        g.search("q")
    assert b.usage()["usd_committed"] == pytest.approx(0.30)


def test_failed_paid_call_is_charged_at_max_unless_known_uncharged():
    b = paid_budget()
    charged = FakeSearchProvider(is_paid=True, max_cost_usd=0.25, fail_with="timeout")
    with pytest.raises(ProviderError):
        GuardedSearch(charged, b).search("q")
    assert b.usage()["usd_committed"] == pytest.approx(0.25)   # spend recorded despite the failure
    rejected = FakeSearchProvider(is_paid=True, max_cost_usd=0.25, fail_with="unauthorized", charged=False)
    with pytest.raises(ProviderError):
        GuardedSearch(rejected, b).search("q")
    assert b.usage()["usd_committed"] == pytest.approx(0.25)   # unchanged


def test_each_retry_attempt_is_reserved_separately():
    b = paid_budget()
    flaky = FakeSearchProvider(is_paid=True, max_cost_usd=0.30, fail_with="timeout")
    g = GuardedSearch(flaky, b)
    attempts = 0
    with pytest.raises(BudgetExceeded):
        for _ in range(10):                          # naive retry loop
            attempts += 1
            try:
                g.search("q")
            except ProviderError:
                continue
    assert attempts == 4 and len(flaky.queries) == 3  # 3 x 0.30 fit; the 4th attempt is blocked


def test_cost_overrun_is_recorded_and_stops():
    inner = FakeSearchProvider({"q": []}, is_paid=True, max_cost_usd=0.10, actual_cost_usd=0.30)
    b = paid_budget()
    with pytest.raises(BudgetExceeded) as e:
        GuardedSearch(inner, b).search("q")
    assert e.value.cap == "cost_overrun" and b.usage()["usd_committed"] == pytest.approx(0.30)


def test_rounding_never_undercounts():
    assert usd_to_micro("0.0000001") == 1 and usd_to_micro(0.008) == 8000


def test_monthly_cap_is_shared_across_runs_via_ledger(tmp_path):
    db = tmp_path / "l.sqlite"
    now = lambda: datetime(2026, 10, 5, tzinfo=timezone.utc)
    cfg = BudgetConfig(paid_calls_enabled=True, max_usd_per_run="5", monthly_usd_cap="0.50")

    def run(run_id):
        return Budget(cfg, run_id=run_id, ledger=SqliteLedger(db), now=now)

    inner = FakeSearchProvider({"q": []}, is_paid=True, max_cost_usd=0.20)
    GuardedSearch(inner, run("r1")).search("q"); GuardedSearch(inner, run("r1")).search("q")
    with pytest.raises(BudgetExceeded) as e:                       # a NEW run still sees 0.40 spent
        GuardedSearch(inner, run("r2")).search("q")
    assert e.value.cap == "monthly_usd_cap"
    other_month = Budget(cfg, run_id="r3", ledger=SqliteLedger(db),
                         now=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    GuardedSearch(inner, other_month).search("q")                 # new calendar month resets


def test_unsettled_reservation_keeps_counting_after_a_crash(tmp_path):
    ledger = SqliteLedger(tmp_path / "l.sqlite")
    ledger.reserve(run_id="crashed", provider="p", amount_micro=400_000, month="2026-10", cap_micro=None)
    assert ledger.month_total_micro("2026-10") == 400_000
    with pytest.raises(BudgetExceeded):
        ledger.reserve(run_id="r", provider="p", amount_micro=200_000, month="2026-10", cap_micro=500_000)


def test_in_memory_ledger_matches():
    led = InMemoryLedger()
    i = led.reserve(run_id="r", provider="p", amount_micro=300, month="m", cap_micro=500)
    led.settle(i, 100)
    assert led.month_total_micro("m") == 100
    led.reserve(run_id="r", provider="p", amount_micro=400, month="m", cap_micro=500)
    with pytest.raises(BudgetExceeded):
        led.reserve(run_id="r", provider="p", amount_micro=1, month="m", cap_micro=500)


def test_free_providers_need_no_authorization_and_count_against_caps():
    budget = Budget(BudgetConfig(max_pages=1, max_llm_calls=1))
    f = GuardedFetcher(FakeFetcher({"u": FetchResult("u", FetchStatus.OK, text="x")}), budget)
    assert f.fetch("u").text == "x"
    with pytest.raises(BudgetExceeded):
        f.fetch("u")
    llm = GuardedLLM(FakeLLM(lambda s, p: {"ok": True}), budget)
    assert llm.complete_json("s", "p", {}).data == {"ok": True}
    with pytest.raises(BudgetExceeded):
        llm.complete_json("s", "p", {})


def test_paid_llm_reserves_its_declared_max_and_settles_actual():
    b = paid_budget()
    llm = GuardedLLM(FakeLLM(is_paid=True, max_cost=0.50, actual_cost_usd=0.05), b)
    llm.complete_json("s", "p", {})
    assert b.usage()["usd_committed"] == pytest.approx(0.05)


def test_fakes_satisfy_the_replaceable_interfaces():
    assert isinstance(FakeSearchProvider(), SearchProvider)
    assert isinstance(FakeFetcher(), PageFetcher)
    assert isinstance(FakeLLM(), LLMProvider)
