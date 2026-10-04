"""Receive-time feature computation with explicit missing values and coverage.

Trade and quote stream health must be supplied separately: silence is not proof
of either zero volume or disconnection. Windows never span lunch, resets, or
source changes. With quote heartbeats a quiet but healthy book is not a gap.
"""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import isfinite, log, sqrt
from statistics import median

from .domain import FeatureSnapshot, Quote, aware, finite_decimal
from .market import (CONTINUOUS_SESSIONS, DEFAULT_SCHEDULE, JST, QuoteQuality, SessionSchedule,
                     TickTable, seconds_since_midnight, validate_quote)

RETURN_WINDOWS = (5, 10, 30, 60, 120, 300, 600)
TRADE_WINDOWS = (5, 10, 30, 60, 120)
VOLATILITY_WINDOW_SECONDS = 60
RETENTION_SECONDS = 610          # longest return window plus anchor margin
DEDUPE_HORIZON_SECONDS = 900     # full-day identities are kept by the ingress (Replay)


def _require_int(value, name, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _require_number(value, name, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")


@dataclass(frozen=True)
class Trade:
    symbol: str
    at: datetime
    price: Decimal
    size: int
    event_id: str
    source: str = "TBT"
    market_data_type: int = 1
    exchange_at: datetime | None = None
    volume_kind: str = "TICK"

    def __post_init__(self):
        aware(self.at)
        if self.exchange_at is not None:
            aware(self.exchange_at)
        finite_decimal(self.price, "trade price", positive=True)
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size <= 0:
            raise ValueError("trade size must be a positive integer")
        if not self.symbol or not self.event_id or not self.source:
            raise ValueError("trade identity is required")
        if self.volume_kind not in ("TICK", "SAMPLED"):
            raise ValueError("unknown trade volume kind")


@dataclass(frozen=True)
class VolumeBaseline:
    """Legacy per-day, per-second history row (research input)."""
    symbol: str
    day: date
    end_second: int
    window_seconds: int
    volume: int
    known_at: datetime
    source: str = "TBT"
    valid: bool = True

    def __post_init__(self):
        aware(self.known_at)
        _require_int(self.end_second, "end_second")
        _require_int(self.window_seconds, "window_seconds", minimum=1)
        _require_int(self.volume, "volume")
        if self.end_second >= 86400 or type(self.valid) is not bool:
            raise ValueError("invalid same-time volume baseline")


@dataclass(frozen=True)
class VolatilityBaseline:
    symbol: str
    day: date
    end_second: int
    window_seconds: int
    volatility_bps: float
    known_at: datetime
    source: str = "L1"
    valid: bool = True

    def __post_init__(self):
        aware(self.known_at)
        _require_int(self.end_second, "end_second")
        _require_int(self.window_seconds, "window_seconds", minimum=1)
        _require_number(self.volatility_bps, "volatility_bps", minimum=0)
        if self.end_second >= 86400 or type(self.valid) is not bool:
            raise ValueError("invalid same-time volatility baseline")


@dataclass(frozen=True)
class SameTimeProfile:
    """Offline median of the past D valid days for one intraday bucket.

    One row per (kind, symbol, source, window, bucket) replaces 18,000 daily
    per-second rows; revisions are selected by ``known_at`` at decision time.
    """
    kind: str
    symbol: str
    source: str
    window_seconds: int
    bucket_start_second: int
    bucket_seconds: int
    median_value: float
    valid_days: int
    known_at: datetime
    version: str

    def __post_init__(self):
        aware(self.known_at)
        if self.kind not in ("volume", "volatility") or not self.symbol or not self.source or not self.version:
            raise ValueError("profile requires kind, symbol, source and version")
        _require_int(self.window_seconds, "window_seconds", minimum=1)
        _require_int(self.bucket_seconds, "bucket_seconds", minimum=1)
        _require_int(self.bucket_start_second, "bucket_start_second")
        _require_int(self.valid_days, "valid_days")
        _require_number(self.median_value, "median_value", minimum=0)
        if self.bucket_start_second >= 86400 or self.bucket_start_second % self.bucket_seconds:
            raise ValueError("profile bucket must be aligned to its size")


@dataclass(frozen=True)
class FeatureConfig:
    version: str = "l1-v1"
    required_features: tuple[str, ...] = (
        "r_5", "r_10", "r_30", "r_60", "r_120", "r_300",
        "rs_30", "rs_60", "rs_300", "vwap_slope_60", "vwap_slope_120",
        "rvol_30", "vwap_deviation_bps", "spread_bps", "micro_bias")
    quote_source: str = "L1"
    trade_source: str = "TBT"
    tick_category: str = "TOPIX500"
    beta: float = 1.0
    historical_quote_max_age_seconds: float = 2
    max_quote_gap_seconds: float = 2
    max_trade_quote_age_seconds: float = 1
    max_trade_health_age_seconds: float = 2
    max_quote_health_age_seconds: float = 5
    min_classification_coverage: float = .8
    rvol_days: int = 20
    rvol_min_days: int = 20
    rvol_min_denominator: float = 1
    market_volatility_window_seconds: int = 300
    volatility_grid_seconds: float = 1
    baseline_bucket_seconds: int = 0
    vwap_kind: str = "TICK"
    quality: QuoteQuality = field(default_factory=QuoteQuality)

    def __post_init__(self):
        if not self.version or not isinstance(self.required_features, tuple) or any(
                not isinstance(name, str) or not name for name in self.required_features):
            raise ValueError("feature version and named required features are required")
        _require_number(self.beta, "beta")
        for name in ("historical_quote_max_age_seconds", "max_quote_gap_seconds",
                     "max_trade_quote_age_seconds", "max_trade_health_age_seconds",
                     "rvol_min_denominator"):
            _require_number(getattr(self, name), name, minimum=0)
        for name in ("max_quote_health_age_seconds", "volatility_grid_seconds"):
            _require_number(getattr(self, name), name, minimum=0)
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        _require_number(self.min_classification_coverage, "min_classification_coverage", minimum=0)
        if self.min_classification_coverage > 1:
            raise ValueError("invalid feature age or coverage")
        _require_int(self.rvol_days, "rvol_days", minimum=1)
        _require_int(self.rvol_min_days, "rvol_min_days", minimum=1)
        if self.rvol_days < self.rvol_min_days:
            raise ValueError("RVOL needs positive frozen history sample counts")
        if self.vwap_kind not in ("TICK", "SAMPLED"):
            raise ValueError("unknown VWAP kind")
        _require_int(self.market_volatility_window_seconds, "market_volatility_window_seconds", minimum=1)
        if self.market_volatility_window_seconds not in RETURN_WINDOWS:
            raise ValueError("market volatility needs a supported full window")
        _require_int(self.baseline_bucket_seconds, "baseline_bucket_seconds")
        if self.baseline_bucket_seconds and 3600 % self.baseline_bucket_seconds:
            raise ValueError("baseline buckets must divide one hour to stay session-aligned")
        if not isinstance(self.quality, QuoteQuality):
            raise ValueError("feature quality must be a QuoteQuality")


class _TimeSeries:
    """Time-ordered window with O(log n) lookups and amortized pruning."""
    __slots__ = ("items", "stamps", "values", "head")

    def __init__(self):
        self.items, self.stamps, self.values, self.head = [], [], [], 0

    def __len__(self):
        return len(self.items) - self.head

    def __bool__(self):
        return len(self.items) > self.head

    def __iter__(self):
        for index in range(self.head, len(self.items)):
            yield self.items[index]

    def __getitem__(self, index):
        size = len(self)
        if index < 0:
            index += size
        if not 0 <= index < size:
            raise IndexError("time series index out of range")
        return self.items[self.head + index]

    def append(self, stamp: float, item, value=None):
        self.items.append(item)
        self.stamps.append(stamp)
        self.values.append(value)

    def clear(self):
        self.items.clear()
        self.stamps.clear()
        self.values.clear()
        self.head = 0

    def keep_anchor_before(self, stamp: float):
        """Drop older records but keep the newest record before ``stamp`` as anchor."""
        index = bisect_left(self.stamps, stamp, self.head) - 1
        if index > self.head:
            self.head = index
            if self.head > 4096 and self.head * 2 > len(self.items):
                del self.items[:self.head], self.stamps[:self.head], self.values[:self.head]
                self.head = 0

    def index_at_or_before(self, stamp: float) -> int:
        index = bisect_right(self.stamps, stamp, self.head) - 1
        return index if index >= self.head else -1

    def between(self, start_exclusive: float, end_inclusive: float):
        low = bisect_right(self.stamps, start_exclusive, self.head)
        high = bisect_right(self.stamps, end_inclusive, self.head)
        return self.items[low:high]


@dataclass
class _Series:
    quotes: _TimeSeries = field(default_factory=_TimeSeries)       # Quote, float mid
    trades: _TimeSeries = field(default_factory=_TimeSeries)       # (Trade, direction)
    vwap_points: _TimeSeries = field(default_factory=_TimeSeries)  # (at, Decimal vwap), float vwap
    seen: dict = field(default_factory=dict)                       # identity -> (payload, received_at)
    day: date | None = None
    segment: tuple | None = None
    last_at: datetime | None = None
    revision: int = 0
    trade_start: datetime | None = None
    trade_health_at: datetime | None = None
    trade_healthy: bool = False
    cumulative_value: Decimal = Decimal(0)
    cumulative_size: int = 0
    day_stream_complete: bool = False
    last_daily_trade_health_at: datetime | None = None
    quote_healthy: bool = False
    quote_health_at: datetime | None = None
    quote_healthy_since: datetime | None = None
    invalid_reason: str = "WARMUP"


class FeatureEngine:
    def __init__(self, benchmark_symbol: str, config: FeatureConfig | None = None,
                 ticks: TickTable | None = None, *, schedule: SessionSchedule = DEFAULT_SCHEDULE):
        if not benchmark_symbol:
            raise ValueError("benchmark symbol is required")
        self.benchmark_symbol = benchmark_symbol
        self.config = config or FeatureConfig()
        self.ticks = ticks or TickTable()
        self.schedule = schedule
        self._series: dict[str, _Series] = {}
        self._volume_rows: dict[tuple, list[VolumeBaseline]] = {}
        self._volatility_rows: dict[tuple, list[VolatilityBaseline]] = {}
        self._profiles: dict[tuple, list[SameTimeProfile]] = {}
        self._cumulative: dict[tuple[str, str], tuple[datetime, int, str]] = {}
        self._historical_quality = replace(self.config.quality,
            max_age_seconds=self.config.historical_quote_max_age_seconds,
            valuation_max_age_seconds=max(self.config.historical_quote_max_age_seconds,
                                          self.config.quality.valuation_max_age_seconds))
        self._rvol_windows = tuple(sorted({int(name[5:]) for name in self.config.required_features
                                           if name.startswith("rvol_") and name[5:].isdigit()}))
        self._bench_key = None
        self._bench_returns: dict[int, float] = {}

    # ------------------------------------------------------------------ state
    def _get(self, symbol):
        return self._series.setdefault(symbol, _Series())

    def reset(self, symbol: str, reason="DATA_RESET", *, reset_daily=False, new_day=False):
        state = self._get(symbol)
        state.quotes.clear()
        state.trades.clear()
        state.vwap_points.clear()
        state.trade_start = None
        state.trade_health_at = None
        state.trade_healthy = False
        state.invalid_reason = reason
        state.revision += 1
        if reset_daily or new_day:
            state.cumulative_value = Decimal(0)
            state.cumulative_size = 0
            state.day_stream_complete = False
            state.last_daily_trade_health_at = None
        if new_day:
            # Identities and stream health never carry across trading days.
            state.quote_healthy = False
            state.quote_health_at = None
            state.quote_healthy_since = None
            state.seen.clear()

    def _segment(self, at):
        return (at.astimezone(JST).date(), self.schedule.session(at))

    def _advance(self, symbol, at):
        aware(at)
        state = self._get(symbol)
        if state.last_at is not None and at < state.last_at:
            return None
        local = at.astimezone(JST)
        segment = (local.date(), self.schedule.session(at))
        if state.day != local.date():
            self.reset(symbol, "DAY_WARMUP", new_day=True)
            state.day = local.date()
        if state.segment != segment:
            self.reset(symbol, "SESSION_WARMUP")
            state.segment = segment
        state.last_at = at
        return state

    def _prune(self, state, at):
        earliest = (at - timedelta(seconds=RETENTION_SECONDS)).timestamp()
        for series in (state.quotes, state.trades, state.vwap_points):
            series.keep_anchor_before(earliest)
        horizon = at - timedelta(seconds=DEDUPE_HORIZON_SECONDS)
        seen = state.seen
        while seen:
            key = next(iter(seen))
            if seen[key][1] >= horizon:
                break
            del seen[key]

    def _duplicate(self, state, symbol, identity, payload, reason):
        known = state.seen.get(identity)
        if known is None:
            return False
        if known[0] != payload:
            self.reset(symbol, reason)
        return True

    # ------------------------------------------------------------ stream health
    def set_quote_stream_health(self, symbol: str, at: datetime, healthy: bool,
                                source: str | None = None) -> bool:
        """Adapter heartbeat proving the quote subscription is alive."""
        if type(healthy) is not bool:
            raise ValueError("stream health must be a boolean")
        state = self._advance(symbol, at)
        if state is None:
            return False
        if not healthy or (source is not None and source != self.config.quote_source):
            state.quote_healthy = False
            state.quote_health_at = at
            state.quote_healthy_since = None
            self.reset(symbol, "QUOTE_STREAM_UNHEALTHY")
            return False
        if (not state.quote_healthy or state.quote_health_at is None or
                (at - state.quote_health_at).total_seconds() > self.config.max_quote_health_age_seconds):
            state.quote_healthy_since = at
        state.quote_healthy = True
        state.quote_health_at = at
        return True

    def quote_stream_healthy(self, symbol: str, at: datetime) -> bool:
        aware(at)
        state = self._series.get(symbol)
        return bool(state is not None and state.quote_healthy and state.quote_health_at is not None
                    and 0 <= (at - state.quote_health_at).total_seconds()
                    <= self.config.max_quote_health_age_seconds)

    def _quote_covered(self, state, start: datetime, end: datetime) -> bool:
        """A quiet interval is coverage only when heartbeats prove the stream lived."""
        return bool(state.quote_healthy and state.quote_healthy_since is not None
                    and state.quote_healthy_since <= start and state.quote_health_at is not None
                    and (end - state.quote_health_at).total_seconds()
                    <= self.config.max_quote_health_age_seconds)

    def set_trade_stream_health(self, symbol: str, at: datetime, healthy: bool,
                                source: str = "TBT") -> bool:
        state = self._advance(symbol, at)
        if state is None:
            return False
        if not healthy or source != self.config.trade_source:
            state.trades.clear()
            state.trade_start = None
            state.trade_healthy = False
            state.trade_health_at = at
            state.invalid_reason = "TRADE_STREAM_UNHEALTHY"
            state.day_stream_complete = False
            state.last_daily_trade_health_at = at
            state.revision += 1
            return False
        previous_daily = state.last_daily_trade_health_at
        if previous_daily is not None:
            elapsed = (at - previous_daily).total_seconds()
            if self.schedule.crosses_lunch(previous_daily, at):
                elapsed -= self.schedule.lunch_seconds()  # A scheduled break contains no missing trades.
            if elapsed > self.config.max_trade_health_age_seconds:
                state.day_stream_complete = False
        elif state.cumulative_size == 0:
            # A complete day needs a stream established no later than the official
            # open; later attachment needs an explicitly trusted cumulative seed.
            state.day_stream_complete = at.astimezone(JST).time() <= self.schedule.morning_open
        state.last_daily_trade_health_at = at
        if state.trade_start is None or (state.trade_health_at is not None and
                (at - state.trade_health_at).total_seconds() > self.config.max_trade_health_age_seconds):
            if state.trade_health_at is not None:
                state.day_stream_complete = False
            state.trades.clear()
            state.trade_start = at
        state.trade_health_at = at
        state.trade_healthy = True
        return True

    def trade_stream_healthy(self, symbol: str, at: datetime) -> bool:
        """Raw source health, independent of unfinished version feature windows."""
        aware(at)
        state = self._series.get(symbol)
        return bool(state is not None and state.trade_healthy and state.trade_start is not None
                    and state.last_at is not None and state.last_at <= at
                    and state.trade_health_at is not None
                    and 0 <= (at - state.trade_health_at).total_seconds() <= self.config.max_trade_health_age_seconds
                    and self._segment(at) == state.segment
                    and self.schedule.session(at) in CONTINUOUS_SESSIONS)

    # ------------------------------------------------------------------ inputs
    def on_quote(self, quote: Quote) -> bool:
        state = self._get(quote.symbol)
        identity = ("quote", quote.source, quote.event_id)
        payload = (quote.symbol, quote.bid, quote.ask, quote.bid_size, quote.ask_size,
                   quote.market_data_type, quote.market_status)
        if self._duplicate(state, quote.symbol, identity, payload, "CONFLICTING_MARKET_EVENT"):
            return False
        state = self._advance(quote.symbol, quote.at)
        if state is None:
            return False
        state.seen[identity] = (payload, quote.at)
        quality = validate_quote(quote, quote.at, self.ticks, self.config.tick_category,
                                 self.config.quality, schedule=self.schedule)
        reason = "QUOTE_SOURCE_CHANGED" if quote.source != self.config.quote_source else quality.reason
        if reason:
            self.reset(quote.symbol, reason)
            return False
        if state.quotes:
            previous = state.quotes[-1].at
            if ((quote.at - previous).total_seconds() > self.config.max_quote_gap_seconds
                    and not self._quote_covered(state, previous, quote.at)):
                self.reset(quote.symbol, "QUOTE_COVERAGE_GAP")
        stamp = quote.at.timestamp()
        state.quotes.append(stamp, quote, float(quote.mid))
        state.revision += 1
        state.invalid_reason = ""
        if state.cumulative_size:
            vwap = state.cumulative_value / state.cumulative_size
            state.vwap_points.append(stamp, (quote.at, vwap), float(vwap))
        self._prune(state, quote.at)
        return True

    def on_trade(self, trade: Trade) -> bool:
        state = self._get(trade.symbol)
        identity = ("trade", trade.source, trade.event_id)
        payload = (trade.symbol, trade.price, trade.size, trade.market_data_type,
                   trade.exchange_at, trade.volume_kind)
        if self._duplicate(state, trade.symbol, identity, payload, "TRADE_CORRECTION_REQUIRES_REBUILD"):
            if state.seen[identity][0] != payload:
                state.day_stream_complete = False
            return False
        state = self._advance(trade.symbol, trade.at)
        if state is None:
            return False
        state.seen[identity] = (payload, trade.at)
        if (trade.source != self.config.trade_source or trade.market_data_type != 1 or
                trade.volume_kind != self.config.vwap_kind or
                (trade.exchange_at is not None and trade.exchange_at > trade.at) or
                self.schedule.session(trade.at) not in CONTINUOUS_SESSIONS or
                not self.ticks.is_legal(trade.price, trade.at, self.config.tick_category)):
            self.reset(trade.symbol, "INVALID_TRADE_STREAM")
            state.day_stream_complete = False
            return False
        self.set_trade_stream_health(trade.symbol, trade.at, True, trade.source)
        direction = "UNKNOWN"
        quote = state.quotes[-1] if state.quotes else None
        if quote is not None:
            result = validate_quote(quote, trade.at, self.ticks, self.config.tick_category,
                                    self.config.quality, schedule=self.schedule)
            # Receive time, rather than retrospectively known exchange time,
            # determines which quote can participate in classification.
            if (result and (trade.at - quote.at).total_seconds() <= self.config.max_trade_quote_age_seconds
                    and max((trade.at - (quote.bid_at or quote.at)).total_seconds(),
                            (trade.at - (quote.ask_at or quote.at)).total_seconds()) <= self.config.max_trade_quote_age_seconds
                    and (trade.exchange_at is None or quote.at <= trade.exchange_at)):
                if trade.price >= quote.ask:
                    direction = "BUY"
                elif trade.price <= quote.bid:
                    direction = "SELL"
        stamp = trade.at.timestamp()
        state.trades.append(stamp, (trade, direction))
        state.cumulative_value += trade.price * trade.size
        state.cumulative_size += trade.size
        vwap = state.cumulative_value / state.cumulative_size
        state.vwap_points.append(stamp, (trade.at, vwap), float(vwap))
        state.revision += 1
        self._prune(state, trade.at)
        return True

    def on_cumulative_volume(self, symbol: str, at: datetime, total: int,
                             last_price: Decimal, event_id: str, source="L1_CUMULATIVE") -> bool:
        """Convert genuine cumulative changes to a named sampled VWAP proxy.

        Never call this with a repeated last-trade size. A reset or correction
        establishes a new baseline and invalidates volume windows.
        """
        aware(at)
        if self.config.vwap_kind != "SAMPLED" or source != self.config.trade_source:
            return False
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ValueError("invalid cumulative volume")
        key = (symbol, source)
        previous = self._cumulative.get(key)
        state = self._get(symbol)
        if state.last_at is not None and at < state.last_at:
            return False
        if previous is not None and (event_id == previous[2] or at < previous[0]):
            return False
        self._cumulative[key] = (at, total, event_id)
        if previous is None or at.astimezone(JST).date() != previous[0].astimezone(JST).date() or total < previous[1]:
            self.reset(symbol, "CUMULATIVE_VOLUME_BASELINE", reset_daily=True)
            self.set_trade_stream_health(symbol, at, True, source)
            return False
        self.set_trade_stream_health(symbol, at, True, source)
        delta = total - previous[1]
        if delta == 0:
            return False
        return self.on_trade(Trade(symbol, at, last_price, delta, event_id,
                                   source=source, volume_kind="SAMPLED"))

    def set_daily_vwap(self, symbol: str, at: datetime, cumulative_value: Decimal,
                       cumulative_size: int, *, source: str, complete: bool):
        """Seed a verified same-source daily cumulative value, never hindsight."""
        finite_decimal(cumulative_value, "cumulative value", positive=True)
        if isinstance(cumulative_size, bool) or not isinstance(cumulative_size, int) or cumulative_size <= 0:
            raise ValueError("cumulative size must be a positive integer")
        if type(complete) is not bool:
            raise ValueError("daily VWAP completeness must be a boolean")
        state = self._advance(symbol, at)
        if state is None or source != self.config.trade_source:
            return False
        state.cumulative_value = cumulative_value
        state.cumulative_size = cumulative_size
        state.day_stream_complete = complete
        state.vwap_points.clear()
        vwap = cumulative_value / cumulative_size
        state.vwap_points.append(at.timestamp(), (at, vwap), float(vwap))
        state.revision += 1
        return True

    # ------------------------------------------------------------- baselines
    def add_volume_baseline(self, baseline: VolumeBaseline):
        # A correction to the same historical slot supersedes it only once its
        # own known_at is reached during snapshot evaluation.
        key = (baseline.symbol, baseline.source, baseline.window_seconds, baseline.end_second)
        self._volume_rows.setdefault(key, []).append(baseline)

    def add_volatility_baseline(self, baseline: VolatilityBaseline):
        key = (baseline.symbol, baseline.source, baseline.window_seconds, baseline.end_second)
        self._volatility_rows.setdefault(key, []).append(baseline)

    def add_profile(self, profile: SameTimeProfile):
        key = (profile.kind, profile.symbol, profile.source, profile.window_seconds,
               profile.bucket_start_second)
        self._profiles.setdefault(key, []).append(profile)

    def _baseline_value(self, kind: str, symbol: str, source: str, window: int, at: datetime):
        second = seconds_since_midnight(at)
        bucket_seconds = self.config.baseline_bucket_seconds
        if bucket_seconds:
            bucket = second - second % bucket_seconds
            revisions = [row for row in self._profiles.get((kind, symbol, source, window, bucket), ())
                         if row.known_at <= at and row.bucket_seconds == bucket_seconds]
            row = max(reversed(revisions), key=lambda r: r.known_at, default=None)
            if row is None or row.valid_days < self.config.rvol_min_days:
                return None
            value = row.median_value
        else:
            rows = (self._volume_rows if kind == "volume" else self._volatility_rows).get(
                (symbol, source, window, second), ())
            day = at.astimezone(JST).date()
            by_day = {}
            for row in sorted((r for r in rows if r.day < day and r.known_at <= at),
                              key=lambda r: r.known_at):
                by_day[row.day] = row
            # "Past D valid days": invalid days never consume one of the D slots.
            valid = [by_day[d] for d in sorted(by_day, reverse=True) if by_day[d].valid]
            valid = valid[:self.config.rvol_days]
            if len(valid) < self.config.rvol_min_days:
                return None
            value = median(r.volume if kind == "volume" else r.volatility_bps for r in valid)
        if kind == "volume":
            return value if value >= self.config.rvol_min_denominator and value > 0 else None
        return value if value > 0 else None

    # --------------------------------------------------------------- features
    def _current_valid(self, state, quote, at) -> bool:
        if validate_quote(quote, at, self.ticks, self.config.tick_category,
                          self.config.quality, schedule=self.schedule):
            return True
        return self._quote_covered(state, quote.at, at) and bool(validate_quote(
            quote, at, self.ticks, self.config.tick_category, self.config.quality,
            purpose="valuation", schedule=self.schedule))

    def _return(self, state, at, window, *, current_valid=None):
        if not state or not state.quotes:
            return None
        current = state.quotes[-1]
        valid = self._current_valid(state, current, at) if current_valid is None else current_valid
        if not valid:
            return None
        cutoff = at - timedelta(seconds=window)
        index = state.quotes.index_at_or_before(cutoff.timestamp())
        if index < 0:
            return None
        history = state.quotes.items[index]
        covered = self._quote_covered(state, history.at, cutoff)
        if (cutoff - history.at).total_seconds() > self.config.historical_quote_max_age_seconds and not covered:
            return None
        if not validate_quote(history, history.at if covered else cutoff, self.ticks,
                              self.config.tick_category, self._historical_quality, schedule=self.schedule):
            return None
        return 10000 * log(state.quotes.values[-1] / state.quotes.values[index])

    def _benchmark_returns(self, at) -> dict[int, float]:
        bench = self._series.get(self.benchmark_symbol)
        # Heartbeats change coverage without changing the data revision.
        key = (at, id(bench), bench.revision if bench else -1, bench.quote_health_at if bench else None)
        if key != self._bench_key:
            results = {}
            if bench is not None and bench.quotes:
                valid = self._current_valid(bench, bench.quotes[-1], at)
                for window in RETURN_WINDOWS:
                    value = self._return(bench, at, window, current_valid=valid)
                    if value is not None:
                        results[window] = value
            self._bench_key, self._bench_returns = key, results
        return self._bench_returns

    def _realized_volatility(self, state, at, window):
        """Sum of squared mid log returns on a fixed time grid, anchored before the window.

        Grid sampling makes the scale independent of quote update frequency.
        """
        if self._return(state, at, window) is None:
            return None
        series, grid = state.quotes, self.config.volatility_grid_seconds
        start = (at - timedelta(seconds=window)).timestamp()
        end = at.timestamp()
        index = series.index_at_or_before(start)
        if index < 0:
            return None
        previous = series.values[index]
        total, step, last = 0.0, 1, len(series.stamps) - 1
        while True:
            # The last partial grid interval still belongs to the window. A grid
            # larger than the window must retain the anchor-to-end return too.
            point = min(start + step * grid, end)
            while index < last and series.stamps[index + 1] <= point:
                index += 1
            current = series.values[index]
            if current != previous:
                change = 10000 * log(current / previous)
                total += change * change
            previous = current
            if point >= end:
                break
            step += 1
        return sqrt(total)

    def snapshot(self, symbol: str, at: datetime) -> FeatureSnapshot:
        aware(at)
        state = self._series.get(symbol)
        values = {}
        if state is None or not state.quotes:
            return FeatureSnapshot(symbol, at, values, False, self.config.version, "QUOTE_WARMUP")
        if state.last_at > at:
            return FeatureSnapshot(symbol, at, values, False, self.config.version, "PAST_SNAPSHOT_UNSUPPORTED")
        quote = state.quotes[-1]
        if self._segment(at) != state.segment:
            return FeatureSnapshot(symbol, at, values, False, self.config.version, "SESSION_CHANGED")
        if not self._current_valid(state, quote, at):
            reason = validate_quote(quote, at, self.ticks, self.config.tick_category,
                                    self.config.quality, schedule=self.schedule).reason or "QUOTE_INVALID"
            return FeatureSnapshot(symbol, at, values, False, self.config.version, reason)
        mid = quote.mid
        spread = quote.ask - quote.bid
        obi = (quote.bid_size - quote.ask_size) / (quote.bid_size + quote.ask_size)
        values.update(mid=float(mid), spread=float(spread), spread_bps=float(10000 * spread / mid),
                      obi=obi, micro_bias=obi / 2,
                      microprice=float((quote.ask * quote.bid_size + quote.bid * quote.ask_size) /
                                       (quote.bid_size + quote.ask_size)))
        benchmark = self._benchmark_returns(at)
        for window in RETURN_WINDOWS:
            result = self._return(state, at, window, current_valid=True)
            reference = benchmark.get(window)
            if result is not None:
                values[f"r_{window}"] = result
            if reference is not None:
                values[f"r_mkt_{window}"] = reference
            if result is not None and reference is not None:
                values[f"rs_{window}"] = result - self.config.beta * reference
        if "r_30" in values and "r_120" in values:
            values["accel"] = values["r_30"] / 30 - values["r_120"] / 120
        volatility = self._realized_volatility(state, at, VOLATILITY_WINDOW_SECONDS)
        if volatility is not None:
            values["volatility_bps"] = volatility
        vwap_prefix = "vwap" if self.config.vwap_kind == "TICK" else "vwap_proxy"
        if state.cumulative_size and (state.day_stream_complete or self.config.vwap_kind == "SAMPLED"):
            vwap = state.cumulative_value / state.cumulative_size
            values[vwap_prefix] = float(vwap)
            values[f"{vwap_prefix}_deviation_bps"] = float(10000 * (mid / vwap - 1))
            current_vwap = float(vwap)
            for window in RETURN_WINDOWS:
                cutoff = at - timedelta(seconds=window)
                index = state.vwap_points.index_at_or_before(cutoff.timestamp())
                if index < 0:
                    continue
                past_at = state.vwap_points.items[index][0]
                if ((cutoff - past_at).total_seconds() <= self.config.historical_quote_max_age_seconds
                        or self._quote_covered(state, past_at, cutoff)):
                    values[f"{vwap_prefix}_slope_{window}"] = 10000 * log(current_vwap / state.vwap_points.values[index])
        healthy = (state.trade_start is not None and state.trade_healthy and state.trade_health_at is not None
                   and (at - state.trade_health_at).total_seconds() <= self.config.max_trade_health_age_seconds)
        end = at.timestamp()
        for window in TRADE_WINDOWS:
            cutoff = at - timedelta(seconds=window)
            if not healthy or state.trade_start > cutoff:
                continue
            trades = state.trades.between(cutoff.timestamp(), end)
            all_volume = buys = sells = 0
            for trade, direction in trades:
                all_volume += trade.size
                if direction == "BUY":
                    buys += trade.size
                elif direction == "SELL":
                    sells += trade.size
            values[f"volume_{window}"] = float(all_volume)
            classified = buys + sells
            if all_volume:
                coverage = classified / all_volume
                values[f"classification_coverage_{window}"] = coverage
                if classified and coverage >= self.config.min_classification_coverage:
                    values[f"ti_{window}"] = (buys - sells) / classified
            if window in self._rvol_windows:
                denominator = self._baseline_value("volume", symbol, self.config.trade_source, window, at)
                if denominator is not None:
                    values[f"rvol_{window}"] = all_volume / denominator
        missing = [name for name in self.config.required_features if name not in values]
        return FeatureSnapshot(symbol, at, values, not missing, self.config.version,
                               "MISSING:" + ",".join(missing) if missing else "")

    def market_snapshot(self, at: datetime, universe) -> FeatureSnapshot:
        """Market-layer features computed once per timestamp (not per stock)."""
        aware(at)
        values = {}
        for window, value in self._benchmark_returns(at).items():
            values[f"r_mkt_{window}"] = value
        window = self.config.market_volatility_window_seconds
        bench = self._series.get(self.benchmark_symbol)
        market_rv = self._realized_volatility(bench, at, window) if bench is not None else None
        normal = self._baseline_value("volatility", self.benchmark_symbol, self.config.quote_source, window, at)
        if market_rv is not None and normal is not None:
            values["rv_mkt"] = market_rv / normal
        returns, spreads = [], []
        segment = self._segment(at)
        for symbol in universe:
            state = self._series.get(symbol)
            if state is None or not state.quotes or state.segment != segment:
                continue
            quote = state.quotes[-1]
            if not self._current_valid(state, quote, at):
                continue
            spreads.append(float(10000 * (quote.ask - quote.bid) / quote.mid))
            value = self._return(state, at, 300, current_valid=True)
            if value is not None:
                returns.append(value)
        if returns:
            values["breadth"] = sum(value > 0 for value in returns) / len(returns)
        if spreads:
            values["spread_bps"] = median(spreads)
        missing = [name for name in ("rv_mkt", "spread_bps", "breadth") if name not in values]
        return FeatureSnapshot("MARKET", at, values, not missing, f"market-{self.config.version}",
                               "MISSING:" + ",".join(missing) if missing else "")

    @staticmethod
    def feature_windows(snapshot: FeatureSnapshot) -> dict[str, float]:
        """Validated feature coverage for subscription READY; absent stays absent."""
        result = {}
        for name in snapshot.values:
            tail = name.rsplit("_", 1)[-1]
            if tail.isdigit():
                result[name] = float(tail)
        return result
