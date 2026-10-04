"""Deterministic tick-by-tick allocation, request guard and version READY."""
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Mapping

from .domain import aware


@dataclass(frozen=True)
class VersionRequirements:
    version: str
    source: str
    required_windows: Mapping[str, float]
    minimum_coverage: float = .8
    max_age_seconds: float = 2

    def __post_init__(self):
        if not self.version or not self.source or not self.required_windows:
            raise ValueError("version, source, and required feature windows are required")
        if any(not isfinite(x) or x <= 0 for x in self.required_windows.values()):
            raise ValueError("required windows must be positive and finite")
        if not isfinite(self.minimum_coverage) or not 0 <= self.minimum_coverage <= 1:
            raise ValueError("invalid coverage")
        if not isfinite(self.max_age_seconds) or self.max_age_seconds < 0:
            raise ValueError("invalid readiness age")


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reason: str = ""

    def __bool__(self):
        return self.ready


@dataclass(frozen=True)
class SubscriptionPlan:
    subscribe: tuple[str, ...]
    unsubscribe: tuple[str, ...]
    active: tuple[str, ...]
    reasons: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Coverage:
    at: datetime
    source: str
    version: str
    feature_windows: dict[str, float]
    valid: bool
    coverage: float
    synchronized: bool


class SubscriptionScheduler:
    def __init__(self, quota: int, *, min_tenure_seconds: float = 120,
                 request_guard_seconds: float = 15, reserved_symbols=()):
        if isinstance(quota, bool) or not isinstance(quota, int) or quota < 0:
            raise ValueError("quota must be a nonnegative integer")
        if any(not isfinite(x) or x < 0 for x in (min_tenure_seconds, request_guard_seconds)):
            raise ValueError("tenure and request guard must be finite and nonnegative")
        # The provider's 15-second same-contract restriction is a hard minimum.
        if request_guard_seconds < 15:
            raise ValueError("same-contract request guard cannot be shorter than 15 seconds")
        self.quota = quota
        self.min_tenure_seconds = min_tenure_seconds
        self.request_guard_seconds = request_guard_seconds
        self.reserved_symbols = tuple(dict.fromkeys(reserved_symbols))
        if len(self.reserved_symbols) > quota:
            raise ValueError("reserved symbols exceed the verified quota")
        self.active: dict[str, datetime] = {}
        self.last_request: dict[str, datetime] = {}
        self._coverage: dict[tuple[str, str, str], _Coverage] = {}
        self._valid_since: dict[tuple[str, str, str], datetime | None] = {}
        self._reset_epochs: dict[str, datetime] = {}
        self._last_plan_at: datetime | None = None
        self.ready_candidates = 0
        self.total_candidates = 0

    @staticmethod
    def quota_from_lines(total_lines: int, verified_tick_quota: int | None) -> int:
        """An inferred 5% ceiling is not proof of the account's available quota."""
        if (isinstance(total_lines, bool) or not isinstance(total_lines, int) or total_lines < 0 or
                isinstance(verified_tick_quota, bool) or not isinstance(verified_tick_quota, int) or
                verified_tick_quota < 0):
            raise ValueError("actual account subscription quota must be verified")
        return min(total_lines // 20, verified_tick_quota)

    def plan(self, at: datetime, ranked_symbols, *, positions=(), orders=()) -> SubscriptionPlan:
        aware(at)
        if self._last_plan_at is not None and at < self._last_plan_at:
            raise ValueError("subscription plans must advance causally")
        self._last_plan_at = at
        pins = tuple(dict.fromkeys((*positions, *orders, *self.reserved_symbols)))
        reasons = {}
        if len(pins) > self.quota:
            # Changing the verification quota or accumulating too many external
            # positions is an operator error, not permission to evict a pin.
            return SubscriptionPlan((), (), tuple(self.active),
                                    {symbol: "PINNED_QUOTA_EXCEEDED" for symbol in pins})
        sticky = [symbol for symbol, started in self.active.items()
                  if symbol not in pins and (at - started).total_seconds() < self.min_tenure_seconds]
        # Pins may displace a young unpinned subscription; pins are never evicted.
        desired = list(pins)
        for symbol in (*sticky, *ranked_symbols):
            if symbol not in desired and len(desired) < self.quota:
                desired.append(symbol)
        # Guard before eviction. A temporarily unrequestable replacement must
        # not cause churn and create an empty slot.
        for index, symbol in enumerate(tuple(desired)):
            if symbol in self.active:
                continue
            last = self.last_request.get(symbol)
            if last is not None and (at - last).total_seconds() < self.request_guard_seconds:
                reasons[symbol] = "REQUEST_GUARD"
                desired.remove(symbol)
        for symbol in self.active:
            if len(desired) < self.quota and symbol not in desired:
                desired.append(symbol)
        removed = tuple(symbol for symbol in self.active if symbol not in desired)
        for symbol in removed:
            del self.active[symbol]
            self.reset(symbol, at)
        added = tuple(symbol for symbol in desired if symbol not in self.active)
        for symbol in added:
            self.active[symbol] = at
            self.last_request[symbol] = at
            self.reset(symbol, at)
        return SubscriptionPlan(added, removed, tuple(self.active), reasons)

    def subscription_failed(self, symbol: str):
        """Retain last_request on failure so retry also obeys provider guard."""
        self.active.pop(symbol, None)
        self.reset(symbol)

    def reset(self, symbol: str, at: datetime | None = None):
        recorded = [record.at for key, record in self._coverage.items() if key[0] == symbol]
        if at is not None:
            aware(at)
            recorded.append(at)
        if self._last_plan_at is not None:
            recorded.append(self._last_plan_at)
        if recorded:
            self._reset_epochs[symbol] = max(recorded)
        for key in tuple(self._coverage):
            if key[0] == symbol:
                del self._coverage[key]
                self._valid_since.pop(key, None)

    def record_data(self, symbol: str, at: datetime, *, source: str, version: str,
                    feature_windows: Mapping[str, float], valid: bool,
                    coverage: float, synchronized: bool):
        aware(at)
        key = (symbol, source, version)
        prior = self._coverage.get(key)
        if prior is not None and at < prior.at:
            return False
        if symbol not in self.active or at < self.active[symbol]:
            return False
        if not isfinite(coverage) or not 0 <= coverage <= 1:
            raise ValueError("coverage must be a finite ratio")
        if any(not isfinite(x) or x < 0 for x in feature_windows.values()):
            raise ValueError("feature coverage must be finite and nonnegative")
        if not valid or not synchronized:
            self._valid_since[key] = None
        elif key in self._valid_since and self._valid_since[key] is None:
            # First valid data after a failure starts a new contiguous epoch.
            self._valid_since[key] = at
        elif key not in self._valid_since:
            self._valid_since[key] = max(self.active[symbol], self._reset_epochs.get(symbol, self.active[symbol]))
        self._coverage[key] = _Coverage(at, source, version, dict(feature_windows),
                                        bool(valid), coverage, bool(synchronized))
        return True

    def readiness(self, symbol: str, at: datetime, requirements: VersionRequirements) -> Readiness:
        aware(at)
        if symbol not in self.active:
            return Readiness(False, "NOT_SUBSCRIBED")
        record = self._coverage.get((symbol, requirements.source, requirements.version))
        if record is None:
            return Readiness(False, "NO_VERSION_DATA")
        if record.at > at:
            return Readiness(False, "FUTURE_DATA")
        if not record.valid:
            return Readiness(False, "INVALID_DATA")
        if not record.synchronized:
            return Readiness(False, "UNSYNCHRONIZED_DATA")
        if record.coverage < requirements.minimum_coverage:
            return Readiness(False, "LOW_CLASSIFICATION_COVERAGE")
        if (at - record.at).total_seconds() > requirements.max_age_seconds:
            return Readiness(False, "STALE_DATA")
        valid_since = self._valid_since.get((symbol, requirements.source, requirements.version))
        if valid_since is None:
            return Readiness(False, "CONTIGUOUS_COVERAGE_UNKNOWN")
        age = (at - valid_since).total_seconds()
        for feature, window in requirements.required_windows.items():
            if age < window or record.feature_windows.get(feature, -1) < window:
                return Readiness(False, "MISSING_WINDOW:" + feature)
        return Readiness(True)

    def record_candidate(self, symbol: str, at: datetime, requirements: VersionRequirements) -> Readiness:
        result = self.readiness(symbol, at, requirements)
        self.total_candidates += 1
        self.ready_candidates += int(result.ready)
        return result

    @property
    def ready_candidate_ratio(self) -> float | None:
        return self.ready_candidates / self.total_candidates if self.total_candidates else None


PRE_SCORE_FEATURES = ("rs_60", "rs_30")


class SubscriptionCoordinator:
    """Enhanced-version tick-by-tick allocation driven by the coordinator.

    Pre-candidates are ranked only from L1 quote features that exist before a
    tick-by-tick subscription (review DATA-01): requiring trade-flow features
    for the ranking made unsubscribed symbols unrankable, so nothing could ever
    become READY. Plans run on a frozen interval (30-60 s), not per event.
    """

    def __init__(self, engine, scheduler: SubscriptionScheduler, requirements: VersionRequirements,
                 plan_interval_seconds: float):
        if not isfinite(plan_interval_seconds) or plan_interval_seconds <= 0:
            raise ValueError("plan interval must be finite and positive")
        self.e, self.scheduler, self.requirements = engine, scheduler, requirements
        self.plan_interval_seconds = float(plan_interval_seconds)
        self._last_plan: datetime | None = None

    def pre_score(self, snapshot) -> float | None:
        alpha = self.e.alpha
        keys = [key for key in PRE_SCORE_FEATURES if key in alpha.weights and key in snapshot.values]
        if len(keys) != len(PRE_SCORE_FEATURES):
            return None
        total = sum(alpha.weights[key] for key in keys)
        return sum(alpha.weights[key] * alpha.scalers[key].transform(snapshot.values[key]) for key in keys) / total

    def plan(self, at: datetime):
        e = self.e
        rankings = []
        for symbol, snapshot in e.snapshots.items():
            values = snapshot.values
            if (not 0 <= (at - snapshot.at).total_seconds() <= e.alpha.config.max_snapshot_age_seconds
                    or values.get("r_300", 0) <= 0 or values.get("rs_300", 0) <= 0
                    or "spread_bps" not in values or e._valid_quote(symbol, at) is None):
                continue
            score = self.pre_score(snapshot)
            if score is not None:
                rankings.append((score, -values["spread_bps"], symbol))
        plan = self.scheduler.plan(at, [symbol for _, _, symbol in sorted(rankings, reverse=True)],
                                   positions=[s for s, p in e.book.positions.items() if p.quantity],
                                   orders=[o.symbol for o in e.book.active_orders()])
        self._last_plan = at
        if plan.subscribe or plan.unsubscribe:
            e._record(at, "SUBSCRIPTION_PLAN", subscribe=plan.subscribe, unsubscribe=plan.unsubscribe)
        return plan

    def ready(self, snapshot, at: datetime) -> bool:
        from .features import FeatureEngine
        if self._last_plan is None or (at - self._last_plan).total_seconds() >= self.plan_interval_seconds:
            self.plan(at)
        requirements = self.requirements
        symbol = snapshot.symbol
        coverage = min((snapshot.values.get("classification_coverage_" + name.rsplit("_", 1)[-1], 0)
                        for name in requirements.required_windows if name.startswith("ti_")), default=0)
        self.scheduler.record_data(symbol, at, source=requirements.source, version=snapshot.version,
                                   feature_windows=FeatureEngine.feature_windows(snapshot),
                                   valid=self.e.features.trade_stream_healthy(symbol, at), coverage=coverage,
                                   synchronized=self.e._valid_quote(symbol, at) is not None)
        return self.scheduler.readiness(symbol, at, requirements).ready

    def record_candidate(self, symbol: str, at: datetime):
        return self.scheduler.record_candidate(symbol, at, self.requirements)

    @property
    def ready_candidate_ratio(self):
        return self.scheduler.ready_candidate_ratio
