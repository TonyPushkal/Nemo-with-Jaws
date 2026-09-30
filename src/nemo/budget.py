"""Run budgets and the monetary guard.

Money rules (all enforced *before* a paid call, per attempt):
- paid calls are refused unless `paid_calls_enabled` and `max_usd_per_run` are configured;
- a paid provider must declare a positive worst-case cost per call (an unpriced
  paid provider is refused, so a 0 estimate can never bypass the ceiling);
- the worst-case cost is *reserved* before the call and counts against the run
  ceiling and the calendar-month cap (persisted in a ledger, so separate runs
  share the monthly cap);
- after the call the reservation is settled to the reported actual cost; a failed
  call is charged at the reserved maximum unless the provider says it was not
  charged; an actual cost above the reservation is recorded and stops the run;
- amounts are integer micro-USD, rounded up.
Retries must go through the guard once per attempt, so every attempt is counted.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Callable, Iterator, Mapping, Protocol

MICRO = 1_000_000


def usd_to_micro(usd) -> int:
    """Convert dollars to integer micro-dollars, rounding up (never under-count)."""
    return int((Decimal(str(usd)) * MICRO).to_integral_value(rounding=ROUND_CEILING))


class BudgetExceeded(Exception):
    """A per-run or monthly cap was hit; `cap` names it (e.g. "max_queries")."""

    def __init__(self, cap: str):
        super().__init__(f"budget cap reached: {cap}")
        self.cap = cap


class PaidCallsDisabled(Exception):
    def __init__(self) -> None:
        super().__init__(
            "paid calls are disabled: set NEMO_PAID_CALLS_ENABLED=true and "
            "NEMO_MAX_USD_PER_RUN (optionally NEMO_MONTHLY_USD_CAP) to allow them"
        )


class UnpricedPaidProvider(Exception):
    def __init__(self, provider: str):
        super().__init__(f"paid provider {provider!r} declares no positive worst-case cost per call; refusing")


class BudgetConfigError(ValueError):
    pass


def _money(value, name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise BudgetConfigError(f"{name} is not a number: {value!r}") from exc
    if not d.is_finite() or d < 0:
        raise BudgetConfigError(f"{name} must be a non-negative number: {value!r}")
    return d


@dataclass(frozen=True)
class BudgetConfig:
    max_queries: int = 15
    max_pages: int = 60
    max_llm_calls: int = 40
    max_wall_seconds: int = 600
    paid_calls_enabled: bool = False
    max_usd_per_run: Decimal | None = None
    monthly_usd_cap: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_usd_per_run", _money(self.max_usd_per_run, "max_usd_per_run"))
        object.__setattr__(self, "monthly_usd_cap", _money(self.monthly_usd_cap, "monthly_usd_cap"))
        if self.paid_calls_enabled and self.max_usd_per_run is None:
            raise BudgetConfigError("paid calls enabled but max_usd_per_run is not set")
        for name in ("max_queries", "max_pages", "max_llm_calls", "max_wall_seconds"):
            if getattr(self, name) < 0:
                raise BudgetConfigError(f"{name} must be >= 0")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "BudgetConfig":
        def flag(name: str) -> bool:
            return env.get(name, "").strip().lower() in {"1", "true", "yes"}
        kwargs = {}
        for field, var in (("max_queries", "NEMO_MAX_QUERIES"), ("max_pages", "NEMO_MAX_PAGES"),
                           ("max_llm_calls", "NEMO_MAX_LLM_CALLS"), ("max_wall_seconds", "NEMO_MAX_WALL_SECONDS")):
            if env.get(var):
                try:
                    kwargs[field] = int(env[var])
                except ValueError as exc:
                    raise BudgetConfigError(f"{var} is not an integer: {env[var]!r}") from exc
        return cls(paid_calls_enabled=flag("NEMO_PAID_CALLS_ENABLED"),
                   max_usd_per_run=env.get("NEMO_MAX_USD_PER_RUN") or None,
                   monthly_usd_cap=env.get("NEMO_MONTHLY_USD_CAP") or None, **kwargs)


class SpendLedger(Protocol):
    """Durable record of reserved/settled spend, shared across runs."""

    def reserve(self, *, run_id: str, provider: str, amount_micro: int, month: str,
                cap_micro: int | None) -> int:
        """Atomically add a reservation unless the month's total would exceed
        `cap_micro`; raises BudgetExceeded("monthly_usd_cap")."""

    def settle(self, entry_id: int, amount_micro: int) -> None: ...

    def month_total_micro(self, month: str) -> int: ...


@dataclass
class Reservation:
    max_micro: int = 0
    actual_micro: int | None = None

    def set_actual_usd(self, usd) -> None:
        self.actual_micro = usd_to_micro(usd)


_COUNTED = {"queries": "max_queries", "pages": "max_pages", "llm_calls": "max_llm_calls"}


class Budget:
    def __init__(self, cfg: BudgetConfig, *, run_id: str = "run", ledger: SpendLedger | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if cfg.monthly_usd_cap is not None and ledger is None:
            raise BudgetConfigError("monthly_usd_cap requires a spend ledger")
        self.cfg, self.run_id, self.ledger = cfg, run_id, ledger
        self._clock, self._now, self._start = clock, now, clock()
        self.used = {k: 0 for k in _COUNTED}
        self._committed_micro = 0  # reserved + settled spend in this run

    def check_time(self) -> None:
        if self._clock() - self._start >= self.cfg.max_wall_seconds:
            raise BudgetExceeded("max_wall_seconds")

    @contextmanager
    def call(self, kind: str, provider: str, *, is_paid: bool, max_cost_usd: float = 0.0) -> Iterator[Reservation]:
        """Wrap ONE attempt of an external call. Checks caps, reserves money
        for paid calls, and settles on exit (including on failure)."""
        self.check_time()
        cap_name = _COUNTED[kind]
        if self.used[kind] + 1 > getattr(self.cfg, cap_name):
            raise BudgetExceeded(cap_name)

        res, entry = Reservation(), None
        if is_paid:
            if not self.cfg.paid_calls_enabled or self.cfg.max_usd_per_run is None:
                raise PaidCallsDisabled()
            res.max_micro = usd_to_micro(max_cost_usd)
            if res.max_micro <= 0:
                raise UnpricedPaidProvider(provider)
            if self._committed_micro + res.max_micro > usd_to_micro(self.cfg.max_usd_per_run):
                raise BudgetExceeded("max_usd_per_run")
            if self.ledger is not None:
                monthly = self.cfg.monthly_usd_cap
                entry = self.ledger.reserve(
                    run_id=self.run_id, provider=provider, amount_micro=res.max_micro,
                    month=self._now().strftime("%Y-%m"),
                    cap_micro=usd_to_micro(monthly) if monthly is not None else None)
            self._committed_micro += res.max_micro

        self.used[kind] += 1
        try:
            yield res
        except BaseException as exc:
            if is_paid:  # conservative: a failed call costs the reserved max unless known not charged
                self._settle(entry, res, 0 if getattr(exc, "charged", True) is False else res.max_micro)
            raise
        if is_paid:
            amount = res.max_micro if res.actual_micro is None else res.actual_micro
            self._settle(entry, res, amount)
            if amount > res.max_micro:
                raise BudgetExceeded("cost_overrun")

    def _settle(self, entry: int | None, res: Reservation, amount: int) -> None:
        self._committed_micro += amount - res.max_micro
        if entry is not None and self.ledger is not None:
            self.ledger.settle(entry, amount)

    def usage(self) -> dict:
        return {**self.used, "usd_committed": self._committed_micro / MICRO}
