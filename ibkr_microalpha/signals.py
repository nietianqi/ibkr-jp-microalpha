"""Frozen, causal signal gates for the v1.2 research specification."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from math import isfinite, log
from statistics import median
from types import MappingProxyType
from typing import Mapping, Sequence

from .domain import Candidate, Confirmation, FeatureSnapshot, MarketRegime, Quote, Regime, aware
from .market import QuoteQuality, validate_quote

_NO_SKEW_LIMIT = 86400.0
_QUALITY_CACHE: dict[tuple[float, float], QuoteQuality] = {}


def _number(value: float, name: str, *, positive: bool = False, nonnegative: bool = False) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)
            or (positive and value <= 0) or (nonnegative and value < 0)):
        raise ValueError(f"invalid finite configuration value: {name}")


def _feature(snapshot: FeatureSnapshot, key: str) -> float:
    if (snapshot.version.startswith("l1-proxy-")
            and key in ("vwap_slope_60", "vwap_slope_120", "vwap_deviation_bps")):
        # Proxy data is a separately frozen feature/score version. Do not allow
        # a proxy field to impersonate full-trade VWAP in any other version.
        proxy_key = key.replace("vwap_", "vwap_proxy_", 1)
        if proxy_key in snapshot.values:
            value = snapshot.values[proxy_key]
            if not isfinite(value):
                raise ValueError(f"invalid proxy feature: {proxy_key}")
            return value
    if key in snapshot.values:
        value = snapshot.values[key]
        if not isfinite(value):
            raise ValueError(f"invalid feature: {key}")
        return value
    aliases = {"rs_": "RS_", "rvol_": "RVOL_", "ti_": "TI_"}
    for prefix, replacement in aliases.items():
        if key.startswith(prefix) and replacement + key[len(prefix):] in snapshot.values:
            value = snapshot.values[replacement + key[len(prefix):]]
            if not isfinite(value):
                raise ValueError(f"invalid feature: {key}")
            return value
    raise KeyError(key)


def _snapshot_fresh(snapshot: FeatureSnapshot, now: datetime, age: float) -> bool:
    aware(now)
    return snapshot.valid and 0 <= (now - snapshot.at).total_seconds() <= age


def _quote_ok(quote: Quote, now: datetime, max_age: float, max_sync: float | None = None) -> bool:
    """Layer-specific age/sync limits on the shared validator (no second rule set)."""
    key = (float(max_age), _NO_SKEW_LIMIT if max_sync is None else float(max_sync))
    quality = _QUALITY_CACHE.get(key)
    if quality is None:
        quality = _QUALITY_CACHE[key] = QuoteQuality(key[0], key[1], False, key[0])
    return bool(validate_quote(quote, now, None, quality=quality))


def _spread_bps(quote: Quote) -> float:
    return float(Decimal("10000") * (quote.ask - quote.bid) / quote.mid)


@dataclass(frozen=True)
class FrozenRobustScaler:
    median: float
    mad: float
    epsilon: float

    def __post_init__(self) -> None:
        _number(self.median, "median")
        _number(self.mad, "mad", nonnegative=True)
        _number(self.epsilon, "epsilon", positive=True)

    @classmethod
    def fit_training(cls, samples: Sequence[float], epsilon: float) -> "FrozenRobustScaler":
        if not samples or any(not isfinite(x) for x in samples):
            raise ValueError("finite training samples are required")
        center = median(samples)
        return cls(center, median([abs(x - center) for x in samples]), epsilon)

    def transform(self, value: float) -> float:
        _number(value, "feature")
        return max(-3.0, min(3.0, (value - self.median) / max(1.4826 * self.mad, self.epsilon)))

    def clip_rate(self, samples: Sequence[float]) -> float:
        """Share of training samples at the +/-3 clip: a large share means a wrong scale."""
        if not samples:
            raise ValueError("clip rate needs samples")
        scale = max(1.4826 * self.mad, self.epsilon)
        return sum(abs((x - self.median) / scale) >= 3 for x in samples) / len(samples)


SCORE_WEIGHTS = MappingProxyType({"rs_60": .45, "rs_30": .25, "ln_rvol_30": .20, "vwap_slope_60": .10})


def validate_weights(weights: Mapping[str, float], scalers: Mapping[str, FrozenRobustScaler]) -> Mapping[str, float]:
    if not weights or set(weights) != set(scalers):
        raise ValueError("score weights must name exactly the frozen scaler features")
    for name, value in weights.items():
        _number(value, f"weight {name}")
    return MappingProxyType(dict(weights))


def alpha_score(snapshot: FeatureSnapshot, scalers: Mapping[str, FrozenRobustScaler],
                weights: Mapping[str, float] = SCORE_WEIGHTS) -> float:
    if not snapshot.valid or set(scalers) != set(weights):
        raise ValueError("complete valid features and frozen baseline scalers are required")
    values = {}
    for key in weights:
        if key.startswith("ln_"):
            raw = _feature(snapshot, key[3:])
            if raw <= 0:
                raise ValueError(f"{key[3:]} must be positive for a log feature")
            values[key] = log(raw)
        else:
            values[key] = _feature(snapshot, key)
    return sum(weights[key] * scalers[key].transform(value) for key, value in values.items())


@dataclass(frozen=True)
class RegimeConfig:
    eval_interval_seconds: float
    confirm_seconds: float
    max_snapshot_age_seconds: float
    enter_trend: float
    enter_rs: float
    enter_vwap: float
    exit_trend: float
    exit_rs: float
    exit_vwap: float
    bear_trend: float
    bear_rs: float
    bear_vwap: float
    max_spread_bps: float
    max_volatility_bps: float

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            _number(getattr(self, name), name)
        for name in ("eval_interval_seconds", "confirm_seconds", "max_snapshot_age_seconds",
                     "max_spread_bps", "max_volatility_bps"):
            _number(getattr(self, name), name, positive=True)
        for feature in ("trend", "rs", "vwap"):
            if (getattr(self, "enter_" + feature) < 0
                    or getattr(self, "exit_" + feature) > getattr(self, "enter_" + feature)
                    or getattr(self, "bear_" + feature) > 0):
                raise ValueError("require nonnegative entry, lower exit and nonpositive bearish thresholds")


class RegimeEngine:
    def __init__(self, config: RegimeConfig):
        self.config = config
        self._states: dict[str, Regime] = {}
        self._pending: dict[str, tuple[Regime, datetime]] = {}
        self._last: dict[str, datetime] = {}
        self._last_snapshot: dict[str, datetime] = {}
        self._versions: dict[str, str] = {}

    def evaluate(self, snapshot: FeatureSnapshot, now: datetime, *,
                 market_regime: MarketRegime = MarketRegime.MARKET_OK,
                 account_ok: bool = True) -> Regime:
        """MARKET_UNKNOWN (stale market data) blocks entries elsewhere but is not
        itself a reason to declare an individual stock RISK_OFF."""
        c, symbol = self.config, snapshot.symbol
        unsafe = (not account_ok or market_regime == MarketRegime.MARKET_RISK_OFF
                  or not _snapshot_fresh(snapshot, now, c.max_snapshot_age_seconds))
        try:
            trend = tuple(_feature(snapshot, key) for key in ("r_300", "rs_300", "vwap_slope_120"))
            spread, vol = (_feature(snapshot, key) for key in ("spread_bps", "volatility_bps"))
            unsafe |= spread < 0 or vol < 0 or spread > c.max_spread_bps or vol > c.max_volatility_bps
        except (KeyError, ValueError):
            unsafe = True
            trend = (0.0, 0.0, 0.0)
        old = self._states.get(symbol, Regime.NEUTRAL)
        if unsafe:
            self._states[symbol] = Regime.RISK_OFF
            self._pending.pop(symbol, None)
            return Regime.RISK_OFF
        if self._versions.get(symbol, snapshot.version) != snapshot.version:
            self._states[symbol] = Regime.NEUTRAL
            self._pending.pop(symbol, None)
            old = Regime.NEUTRAL
        self._versions[symbol] = snapshot.version
        last = self._last.get(symbol)
        if (last is not None and (now - last).total_seconds() < c.eval_interval_seconds
                or snapshot.at <= self._last_snapshot.get(symbol, datetime.min.replace(tzinfo=now.tzinfo))):
            return old
        if last is not None and (now - last).total_seconds() > c.eval_interval_seconds + c.max_snapshot_age_seconds:
            self._pending.pop(symbol, None)
            self._states[symbol] = old = Regime.NEUTRAL
        self._last[symbol], self._last_snapshot[symbol] = now, snapshot.at
        enter = (c.enter_trend, c.enter_rs, c.enter_vwap)
        retain = (c.exit_trend, c.exit_rs, c.exit_vwap)
        bear = (c.bear_trend, c.bear_rs, c.bear_vwap)
        if old == Regime.LONG and all(value > threshold for value, threshold in zip(trend, retain)):
            target = Regime.LONG
        elif all(value > max(0, threshold) for value, threshold in zip(trend, enter)):
            target = Regime.LONG
        elif all(value < threshold for value, threshold in zip(trend, bear)):
            target = Regime.BEARISH
        else:
            target = Regime.NEUTRAL
        if target == Regime.NEUTRAL:
            self._pending.pop(symbol, None)
            self._states[symbol] = target
            return target
        if old == target:
            self._pending.pop(symbol, None)
            return target
        pending = self._pending.get(symbol)
        if pending is None or pending[0] != target:
            self._pending[symbol] = (target, snapshot.at)
            self._states[symbol] = Regime.NEUTRAL
        elif (snapshot.at - pending[1]).total_seconds() >= c.confirm_seconds:
            self._states[symbol] = target
            self._pending.pop(symbol, None)
        return self._states[symbol]


@dataclass(frozen=True)
class MarketRegimeConfig:
    max_snapshot_age_seconds: float
    caution_rv: float
    risk_off_rv: float
    caution_spread_bps: float
    risk_off_spread_bps: float
    min_breadth: float
    require_positive_direction: bool
    # Specification section 6: under CAUTION either block new candidates or
    # require a higher frozen entry score. The choice is a frozen experiment.
    caution_policy: str = "block"
    caution_entry_score: float | None = None

    def __post_init__(self) -> None:
        for name in ("max_snapshot_age_seconds", "caution_rv", "risk_off_rv",
                     "caution_spread_bps", "risk_off_spread_bps"):
            _number(getattr(self, name), name, positive=True)
        if self.risk_off_rv <= self.caution_rv or self.risk_off_spread_bps <= self.caution_spread_bps:
            raise ValueError("market caution thresholds must be below risk-off thresholds")
        _number(self.min_breadth, "min_breadth")
        if not 0 <= self.min_breadth <= 1:
            raise ValueError("breadth threshold must be in [0, 1]")
        if type(self.require_positive_direction) is not bool:
            raise ValueError("require_positive_direction must be a boolean")
        if self.caution_policy not in ("block", "raise_threshold"):
            raise ValueError("caution_policy must be block or raise_threshold")
        if self.caution_policy == "raise_threshold":
            if self.caution_entry_score is None:
                raise ValueError("raise_threshold requires a frozen caution_entry_score")
            _number(self.caution_entry_score, "caution_entry_score")
        elif self.caution_entry_score is not None:
            raise ValueError("caution_entry_score is only used by raise_threshold")

    @property
    def caution_floor(self) -> float | None:
        """Raised entry score under CAUTION, or None when CAUTION blocks candidates."""
        return self.caution_entry_score if self.caution_policy == "raise_threshold" else None


class MarketRegimeEngine:
    def __init__(self, config: MarketRegimeConfig):
        self.config = config

    def evaluate(self, snapshot: FeatureSnapshot, now: datetime, *,
                 exchange_normal: bool = True) -> MarketRegime:
        c = self.config
        if not exchange_normal or not _snapshot_fresh(snapshot, now, c.max_snapshot_age_seconds):
            return MarketRegime.MARKET_RISK_OFF
        try:
            rv, spread, breadth = (_feature(snapshot, key) for key in ("rv_mkt", "spread_bps", "breadth"))
            direction = _feature(snapshot, "r_mkt_300") if c.require_positive_direction else 1
        except (KeyError, ValueError):
            return MarketRegime.MARKET_RISK_OFF
        if rv < 0 or spread < 0 or not 0 <= breadth <= 1 or rv >= c.risk_off_rv or spread >= c.risk_off_spread_bps:
            return MarketRegime.MARKET_RISK_OFF
        if rv >= c.caution_rv or spread >= c.caution_spread_bps or breadth < c.min_breadth or direction <= 0:
            return MarketRegime.MARKET_CAUTION
        return MarketRegime.MARKET_OK


@dataclass(frozen=True)
class AlphaConfig:
    ttl_seconds: float
    cooldown_seconds: float
    eval_interval_seconds: float
    max_snapshot_age_seconds: float
    max_quote_age_seconds: float
    entry_score: float
    exit_score: float
    min_rs_30: float
    min_rs_60: float
    min_rvol_30: float
    max_vwap_deviation_bps: float
    max_spread_bps: float
    max_volatility_bps: float

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            _number(getattr(self, name), name)
        for name in ("ttl_seconds", "eval_interval_seconds", "max_snapshot_age_seconds",
                     "max_quote_age_seconds", "min_rvol_30", "max_spread_bps", "max_volatility_bps"):
            _number(getattr(self, name), name, positive=True)
        _number(self.cooldown_seconds, "cooldown_seconds", nonnegative=True)
        _number(self.max_vwap_deviation_bps, "max_vwap_deviation_bps", nonnegative=True)
        if self.entry_score <= self.exit_score:
            raise ValueError("entry score must exceed independent exit threshold")


class AlphaEngine:
    """One candidate stream. A candidate is a signal with a fixed TTL; quantity,
    price cap and economics are attached afterwards by the entry pipeline."""

    def __init__(self, config: AlphaConfig, scalers: Mapping[str, FrozenRobustScaler],
                 score_version: str, weights: Mapping[str, float] | None = None):
        if not score_version:
            raise ValueError("complete frozen scalers and score version are required")
        self.weights = validate_weights(weights if weights is not None else SCORE_WEIGHTS, scalers)
        self.config, self.scalers, self.score_version = config, MappingProxyType(dict(scalers)), score_version
        self._active: dict[str, Candidate] = {}
        self._cooldown: dict[str, datetime] = {}
        self._last_eval: dict[str, datetime] = {}
        self._last_created_snapshot: dict[str, datetime] = {}

    def score(self, snapshot: FeatureSnapshot) -> float:
        return alpha_score(snapshot, self.scalers, self.weights)

    def current(self, symbol: str) -> Candidate | None:
        return self._active.get(symbol)

    def active(self) -> dict[str, Candidate]:
        return dict(self._active)

    def invalidate(self, symbol: str, now: datetime, reason: str) -> Candidate | None:
        aware(now)
        candidate = self._active.pop(symbol, None)
        if candidate is not None:
            self._cooldown[symbol] = now + timedelta(seconds=self.config.cooldown_seconds)
        return candidate

    def evaluate(self, snapshot: FeatureSnapshot, quote: Quote, regime: Regime,
                 market_regime: MarketRegime, now: datetime, *,
                 caution_floor: float | None = None) -> Candidate | None:
        """Return the active candidate, a new one, or None.

        Only MARKET_OK admits candidates, unless the frozen market policy raises
        the entry score under MARKET_CAUTION (``caution_floor``).
        """
        c, symbol = self.config, snapshot.symbol
        aware(now)
        active = self._active.get(symbol)
        reason = ""
        if market_regime == MarketRegime.MARKET_OK:
            score_floor = float("-inf")
        elif market_regime == MarketRegime.MARKET_CAUTION and caution_floor is not None:
            score_floor = caution_floor
        else:
            score_floor = None
        if regime != Regime.LONG or score_floor is None:
            reason = "environment gate"
        elif (quote.symbol != symbol or snapshot.version != self.score_version
              or not _snapshot_fresh(snapshot, now, c.max_snapshot_age_seconds)
              or not _quote_ok(quote, now, c.max_quote_age_seconds)):
            reason = "invalid, stale or mismatched data"
        elif active is not None and now >= active.expires_at:
            reason = "candidate TTL expired"
        try:
            score = self.score(snapshot)
            rs30, rs60 = _feature(snapshot, "rs_30"), _feature(snapshot, "rs_60")
            rvol = _feature(snapshot, "rvol_30")
            deviation = _feature(snapshot, "vwap_deviation_bps")
            vol = _feature(snapshot, "volatility_bps")
            if not reason and (_spread_bps(quote) > c.max_spread_bps or vol < 0
                               or vol > c.max_volatility_bps or deviation > c.max_vwap_deviation_bps):
                reason = "spread, volatility or chase gate"
            if active is not None and not reason and score < c.exit_score:
                reason = "alpha below exit score"
        except (KeyError, ValueError):
            score, rs30, rs60, rvol = 0.0, 0.0, 0.0, 0.0
            reason = reason or "required feature unavailable"
        if reason:
            self.invalidate(symbol, now, reason)
            return None
        if active is not None:
            return active  # A persistent signal never changes the original TTL.
        if now < self._cooldown.get(symbol, now):
            return None
        last = self._last_eval.get(symbol)
        if last is not None and (now - last).total_seconds() < c.eval_interval_seconds:
            return None
        self._last_eval[symbol] = now
        if snapshot.at <= self._last_created_snapshot.get(symbol, datetime.min.replace(tzinfo=now.tzinfo)):
            return None
        threshold = max(c.entry_score, score_floor)
        if (score < threshold or rs30 <= max(0, c.min_rs_30)
                or rs60 <= max(0, c.min_rs_60) or rvol < c.min_rvol_30):
            return None
        candidate = Candidate(
            candidate_id=f"{symbol}:{self.score_version}:{now.isoformat()}", symbol=symbol,
            created_at=now, expires_at=now + timedelta(seconds=c.ttl_seconds),
            entry_score=score, score_version=self.score_version,
            reference_bid=quote.bid, reference_ask=quote.ask,
        )
        self._active[symbol] = candidate
        self._last_created_snapshot[symbol] = snapshot.at
        return candidate


@dataclass(frozen=True)
class ConfirmationConfig:
    version: str
    enhanced: bool
    persistence_seconds: float
    min_updates: int
    window_updates: int
    smoothing_updates: int
    min_positive_fraction: float
    min_obi: float
    max_spread_multiple: float
    normal_spread_bps: float
    max_quote_age_seconds: float
    max_snapshot_age_seconds: float
    max_sync_seconds: float
    required_ti_features: tuple[str, ...]
    min_ti: float
    min_classification_coverage: float

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("confirmation version is required")
        if type(self.enhanced) is not bool:
            raise ValueError("enhanced must be a boolean")
        for name in ("persistence_seconds", "max_spread_multiple", "normal_spread_bps",
                     "max_quote_age_seconds", "max_snapshot_age_seconds", "max_sync_seconds"):
            _number(getattr(self, name), name, positive=True)
        for name in ("min_updates", "window_updates", "smoothing_updates"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.window_updates < self.min_updates or self.smoothing_updates > self.window_updates:
            raise ValueError("confirmation update window is too short")
        for name in ("min_positive_fraction", "min_obi", "min_ti", "min_classification_coverage"):
            value = getattr(self, name)
            _number(value, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.enhanced and (not self.required_ti_features or "ti_10" not in self.required_ti_features):
            raise ValueError("enhanced confirmation requires explicit TI_10 and coverage")
        if not self.enhanced and self.required_ti_features:
            raise ValueError("independent L1 version must not require unavailable TI")
        if any(not name.startswith("ti_") for name in self.required_ti_features):
            raise ValueError("TI feature names must use ti_seconds")


@dataclass(frozen=True)
class ConfirmationResult:
    status: Confirmation
    reason: str
    valid_updates: int = 0


class ConfirmationEngine:
    def __init__(self, config: ConfirmationConfig):
        self.config = config
        self._updates: dict[str, deque[tuple[datetime, float, Decimal, float]]] = {}
        self._seen: dict[str, set[str]] = {}
        self._last_signature: dict[str, tuple] = {}
        self._last_at: dict[str, datetime] = {}
        self._positive_since: dict[str, datetime] = {}
        self._vetoed: set[str] = set()
        self._expires_at: dict[str, datetime] = {}

    def evaluate(self, candidate: Candidate, snapshot: FeatureSnapshot, quote: Quote,
                 now: datetime, *, max_price: Decimal, enhanced_ready: bool = False,
                 net_advantage_positive: bool = True) -> ConfirmationResult:
        c, key = self.config, candidate.candidate_id
        aware(now)
        for expired_key in [k for k, expiry in self._expires_at.items() if expiry <= now]:
            for mapping in (self._updates, self._seen, self._last_signature, self._last_at,
                            self._positive_since, self._expires_at):
                mapping.pop(expired_key, None)
            self._vetoed.discard(expired_key)
        self._expires_at[key] = candidate.expires_at

        def veto(reason: str) -> ConfirmationResult:
            self._positive_since.pop(key, None)
            self._updates.pop(key, None)
            self._vetoed.add(key)
            return ConfirmationResult(Confirmation.VETO, reason)

        if key in self._vetoed:
            return ConfirmationResult(Confirmation.VETO, "candidate previously vetoed")
        if now < candidate.created_at or now >= candidate.expires_at:
            return veto("candidate TTL expired or future dated")
        if (snapshot.symbol != candidate.symbol or quote.symbol != candidate.symbol
                or snapshot.version != candidate.score_version or c.version != candidate.score_version
                or not _snapshot_fresh(snapshot, now, c.max_snapshot_age_seconds)
                or not _quote_ok(quote, now, c.max_quote_age_seconds, c.max_sync_seconds)):
            return veto("invalid, stale, unsynchronized or mismatched data")
        if not net_advantage_positive or quote.ask > max_price:
            return veto("net advantage or immutable chase cap failed")
        if _spread_bps(quote) > c.normal_spread_bps * c.max_spread_multiple:
            return veto("spread expansion")
        if c.enhanced:
            if not enhanced_ready:
                return veto("enhanced version not READY")
            try:
                for feature in c.required_ti_features:
                    ti = _feature(snapshot, feature)
                    coverage = _feature(snapshot, "classification_coverage_" + feature[3:])
                    if not -1 <= ti <= 1 or not 0 <= coverage <= 1 or coverage < c.min_classification_coverage:
                        return veto("TI classification coverage invalid")
                    if ti < 0:
                        return veto("trade direction reversal")
                    if ti <= 0 or ti < c.min_ti:
                        self._positive_since.pop(key, None)
                        return ConfirmationResult(Confirmation.WAIT, "TI threshold not met")
            except (KeyError, ValueError):
                return veto("required TI or coverage missing")
        seen = self._seen.setdefault(key, set())
        signature = (quote.bid, quote.ask, quote.bid_size, quote.ask_size)
        updates = self._updates.setdefault(key, deque(maxlen=c.window_updates))
        if (quote.event_id in seen or quote.at <= self._last_at.get(key, candidate.created_at - timedelta(microseconds=1))
                or signature == self._last_signature.get(key)):
            return ConfirmationResult(Confirmation.WAIT, "duplicate or out-of-order L1 update", len(updates))
        last_at = self._last_at.get(key)
        if last_at is not None and (quote.at - last_at).total_seconds() > c.max_quote_age_seconds:
            updates.clear()
            self._positive_since.pop(key, None)
        seen.add(quote.event_id)
        self._last_at[key], self._last_signature[key] = quote.at, signature
        obi = (quote.bid_size - quote.ask_size) / (quote.bid_size + quote.ask_size)
        raw_history = [item[3] for item in updates][-(c.smoothing_updates - 1):] if c.smoothing_updates > 1 else []
        smoothed = (sum(raw_history) + obi) / (len(raw_history) + 1)
        # Veto needs a persistent reversal over the frozen smoothing span; a single
        # noisy top-of-book imbalance only restarts the persistence clock.
        if len(raw_history) + 1 >= c.smoothing_updates and smoothed < -c.min_obi:
            return veto("smoothed quote direction reversal")
        bid_ok = (not updates or quote.bid >= updates[-1][2] or quote.bid >= candidate.reference_bid)
        updates.append((quote.at, smoothed, quote.bid, obi))
        if obi < -c.min_obi:
            self._positive_since.pop(key, None)
            return ConfirmationResult(Confirmation.WAIT, "transient negative imbalance", len(updates))
        proportion = sum(item[1] > c.min_obi for item in updates) / len(updates)
        if not bid_ok or proportion < c.min_positive_fraction:
            self._positive_since.pop(key, None)
            return ConfirmationResult(Confirmation.WAIT, "quote persistence not met", len(updates))
        since = self._positive_since.setdefault(key, quote.at)
        if len(updates) >= c.min_updates and (quote.at - since).total_seconds() >= c.persistence_seconds:
            return ConfirmationResult(Confirmation.CONFIRM, "distinct update persistence passed", len(updates))
        return ConfirmationResult(Confirmation.WAIT, "waiting for duration and distinct updates", len(updates))


@dataclass(frozen=True)
class DecayConfig:
    delta_score_min: float
    required_periods: int
    eval_interval_seconds: float
    max_snapshot_age_seconds: float

    def __post_init__(self) -> None:
        for name in ("delta_score_min", "eval_interval_seconds", "max_snapshot_age_seconds"):
            _number(getattr(self, name), name, positive=True)
        if type(self.required_periods) is not int or self.required_periods <= 0:
            raise ValueError("required_periods must be a positive integer")


@dataclass(frozen=True)
class DecayDecision:
    exit_required: bool
    data_risk: bool
    count: int
    score_drop: float | None
    reason: str


class AlphaDecayTracker:
    def __init__(self, entry_score: float, score_version: str, entry_at: datetime,
                 config: DecayConfig, scalers: Mapping[str, FrozenRobustScaler] | None = None,
                 weights: Mapping[str, float] | None = None):
        _number(entry_score, "entry_score")
        aware(entry_at)
        if not score_version:
            raise ValueError("entry score version required")
        self.entry_score, self.score_version, self.entry_at = entry_score, score_version, entry_at
        self.config, self.scalers = config, MappingProxyType(dict(scalers)) if scalers is not None else None
        self.weights = (validate_weights(weights if weights is not None else SCORE_WEIGHTS, self.scalers)
                        if self.scalers is not None else None)
        self.count = 0
        self._last_slot = 0
        self._last_snapshot = entry_at

    def evaluate(self, snapshot: FeatureSnapshot, now: datetime) -> DecayDecision:
        aware(now)
        c = self.config
        invalid = (snapshot.version != self.score_version
                   or not _snapshot_fresh(snapshot, now, c.max_snapshot_age_seconds)
                   or now < self.entry_at)
        try:
            score = (alpha_score(snapshot, self.scalers, self.weights) if self.scalers is not None
                     else _feature(snapshot, "score"))
        except (KeyError, ValueError):
            invalid, score = True, 0.0
        if invalid:
            self.count = 0
            self._last_slot = max(self._last_slot, int(max(0, (now - self.entry_at).total_seconds()) // c.eval_interval_seconds))
            self._last_snapshot = max(self._last_snapshot, min(now, snapshot.at))
            return DecayDecision(False, True, 0, None, "data/score version invalid; risk handling required")
        slot = int((now - self.entry_at).total_seconds() // c.eval_interval_seconds)
        drop = self.entry_score - score
        if slot <= self._last_slot or snapshot.at <= self._last_snapshot:
            return DecayDecision(False, False, self.count, drop, "duplicate or out-of-order evaluation")
        if slot > self._last_slot + 1:
            self.count = 0  # Missing fixed periods cannot be joined into sustained evidence.
        self._last_slot, self._last_snapshot = slot, snapshot.at
        self.count = self.count + 1 if drop >= c.delta_score_min else 0
        return DecayDecision(self.count >= c.required_periods, False, self.count, drop,
                             "score decline evaluated on frozen scale")
