"""Single event-loop coordinator: callbacks/account risk precede new entries.

This coordinator only emits requests to an offline execution book. It does not
connect to a broker. A timer must be supplied even when quotes stop changing.

Responsibilities are split (review FLOW-03):

* ``engine.StrategyEngine``  event routing, market state, priorities;
* ``entry.EntryPipeline``    one candidate stream -> sizing -> economics ->
  confirmation -> submit, plus the pre-send re-check;
* ``positions.PositionManager``  exits, cooldowns, residual alarms;
* ``valuation.RiskValuation``    valuation, fee and stress reserves.
"""
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import isfinite

from .domain import FeatureSnapshot, MarketRegime, Quote, Side, aware
from .economics import CalibrationTable, CommissionSchedule, Prediction
from .entry import EntryPipeline
from .market import QuoteQuality, TickTable, validate_quote
from .positions import PositionManager
from .reporting import ExecutionQuality, IntentLedger, RegimeTime
from .risk import AccountSnapshot, PortfolioRisk
from .valuation import RiskValuation

ZERO = Decimal(0)


@dataclass(frozen=True)
class Instrument:
    sector: str
    tick_category: str
    contract_verified: bool
    permission_verified: bool

    def __post_init__(self):
        if not self.sector or self.tick_category not in ('TOPIX500', 'OTHER'):
            raise ValueError('instrument requires verified sector and tick category')
        if type(self.contract_verified) is not bool or type(self.permission_verified) is not bool:
            raise ValueError('instrument verification flags must be booleans')


@dataclass(frozen=True)
class EngineConfig:
    policy_id: str
    model_version: str
    score_version: str
    holding_seconds: int
    min_samples: int
    forecast_max_age_seconds: float
    market_max_age_seconds: float
    submit_p99_seconds: float
    cancel_p99_seconds: float
    exit_buffer_seconds: float
    order_timeout_seconds: float
    exit_escalation_seconds: float
    ordinary_cooldown_seconds: float
    stop_cooldown_seconds: float
    signal_exit_persistence_seconds: float
    initial_cash: Decimal
    max_entry_quantity: int
    max_consecutive_stops: int
    economics_source: str
    market_source: str
    entry_limit_ticks: int
    entry_order_ttl_seconds: float
    min_stop_ticks: int
    stop_bps: Decimal
    stop_volatility_multiple: Decimal
    exit_slippage_ticks: int
    net_safety_margin_bps: Decimal
    gap_reserve_bps: Decimal
    max_soft_block_seconds: float
    budget_buffer_seconds: float

    def __post_init__(self):
        if not all((self.policy_id, self.model_version, self.score_version)):
            raise ValueError('all policy versions must be frozen')
        if self.holding_seconds not in (120, 300, 600, 1200):
            raise ValueError('holding period must be a specified research variant')
        if self.economics_source not in ('calibration', 'forecast'):
            raise ValueError('economics_source must be calibration or forecast')
        if self.market_source not in ('internal', 'external'):
            raise ValueError('market_source must be internal or external')
        if not isinstance(self.initial_cash, Decimal) or not self.initial_cash.is_finite() or self.initial_cash <= 0:
            raise ValueError('initial_cash must be finite and positive')
        for field in ('stop_bps', 'stop_volatility_multiple', 'net_safety_margin_bps', 'gap_reserve_bps'):
            value = getattr(self, field)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f'{field} must be finite and nonnegative')
        for field in ('min_samples', 'max_entry_quantity', 'max_consecutive_stops', 'min_stop_ticks'):
            if type(getattr(self, field)) is not int or getattr(self, field) <= 0:
                raise ValueError(f'{field} must be a positive integer')
        for field in ('entry_limit_ticks', 'exit_slippage_ticks'):
            if type(getattr(self, field)) is not int or getattr(self, field) < 0:
                raise ValueError(f'{field} must be a nonnegative integer')
        for field in ('forecast_max_age_seconds', 'market_max_age_seconds', 'submit_p99_seconds',
                      'cancel_p99_seconds', 'exit_buffer_seconds', 'order_timeout_seconds',
                      'exit_escalation_seconds', 'ordinary_cooldown_seconds', 'stop_cooldown_seconds',
                      'signal_exit_persistence_seconds', 'entry_order_ttl_seconds',
                      'max_soft_block_seconds'):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
                raise ValueError(f'{field} must be finite and positive')
        value = self.budget_buffer_seconds
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
            raise ValueError('budget_buffer_seconds must be finite and nonnegative')


@dataclass(frozen=True)
class Forecast:
    """Research override of the frozen calibration (economics_source='forecast')."""
    prediction: Prediction
    received_at: datetime
    trained_until: datetime
    valid_until: datetime
    reference_entry_price: Decimal
    max_entry_price: Decimal

    def __post_init__(self):
        for at in (self.received_at, self.trained_until, self.valid_until):
            aware(at)
        if self.trained_until >= self.received_at or self.valid_until <= self.received_at:
            raise ValueError('forecast must use past training and a finite future expiry')
        for value in (self.reference_entry_price, self.max_entry_price):
            if not value.is_finite() or value <= 0:
                raise ValueError('forecast prices must be finite and positive')


class StrategyEngine:
    def __init__(self, config, instruments, *, calendar, features, regime, alpha,
                 confirmation, market_regime, book, risk: PortfolioRisk,
                 commissions: CommissionSchedule, quality=None, decay_config=None,
                 subscription_scheduler=None, subscription_requirements=None,
                 plan_interval_seconds: float = 30):
        from .subscriptions import SubscriptionCoordinator
        self.config: EngineConfig = config
        self.instruments: dict[str, Instrument] = dict(instruments)
        self.calendar, self.features = calendar, features
        self.schedule = calendar.schedule
        self.regime, self.alpha = regime, alpha
        self.confirmation, self.market_regime_engine = confirmation, market_regime
        self.book, self.risk, self.commissions = book, risk, commissions
        self.quality: QuoteQuality = quality or QuoteQuality()
        self.decay_config = decay_config
        enhanced = self.confirmation.config.enhanced
        if enhanced and (subscription_scheduler is None or subscription_requirements is None):
            raise ValueError('enhanced version requires verified subscription quota and all feature windows')
        self.subscriptions = (SubscriptionCoordinator(self, subscription_scheduler, subscription_requirements,
                                                      plan_interval_seconds) if enhanced else None)
        self.ticks = TickTable()
        self.quotes: dict[str, Quote] = {}
        self.forecasts = {}
        self.calibration: CalibrationTable | None = None
        self.account_snapshot: AccountSnapshot | None = None
        self.exchange_normal = True
        self.market_snapshot: FeatureSnapshot | None = None
        self.market_state = MarketRegime.MARKET_UNKNOWN
        self.snapshots: dict[str, FeatureSnapshot] = {}
        self.snapshot_history = defaultdict(lambda: deque(maxlen=128))
        self.decay_trackers = {}
        self.cooldown_until: dict[str, datetime] = {}
        self.consecutive_stops: Counter = Counter()
        self.disabled_symbols: dict[str, str] = {}
        self.alarms: list[dict] = []
        self.audit: list[dict] = []
        self.rejections: Counter = Counter()          # raw callback counts
        self.rejection_seconds: Counter = Counter()   # distinct seconds per reason (funnel)
        self.funnel: Counter = Counter()
        self.daily_net_pnl_estimate = None
        self.unreported_fee_reserve = ZERO
        self.intents = IntentLedger()
        self.execution_quality = ExecutionQuality()
        self.regime_time = RegimeTime(regime.config.max_snapshot_age_seconds)
        self._last_at: datetime | None = None
        self._ever_reconciled = False
        self._exit_sequence = 0
        self._dirty: set[str] = set()
        self._market_dirty = False
        self._last_reject: dict[str, str] = {}
        self._reject_second: dict[tuple[str, str], datetime] = {}
        self._noted: dict[tuple[str, str], str] = {}
        self.valuation = RiskValuation(self)
        self.positions = PositionManager(self)
        self.entries = EntryPipeline(self)

    # ------------------------------------------------------------- recording
    def _record(self, at, kind, **data):
        self.audit.append({'at': at.isoformat(), 'kind': kind, **data})

    def _reject(self, at, symbol, reason):
        """Count every callback, but audit only reason changes (review RPT-03)."""
        self.rejections[reason] += 1
        second = at.replace(microsecond=0)
        if self._reject_second.get((symbol, reason)) != second:
            self._reject_second[(symbol, reason)] = second
            self.rejection_seconds[reason] += 1
        if self._last_reject.get(symbol) != reason:
            self._last_reject[symbol] = reason
            self._record(at, 'NO_TRADE', symbol=symbol, reason=reason)

    def _note(self, at, kind, symbol, reason):
        """Transition-only audit for repeated conditions (EXIT_BLOCKED and similar)."""
        if self._noted.get((kind, symbol)) != reason:
            self._noted[(kind, symbol)] = reason
            self._record(at, kind, symbol=symbol, reason=reason)

    def _clear_note(self, kind, symbol):
        self._noted.pop((kind, symbol), None)

    def _clock(self, at):
        aware(at)
        if self._last_at is not None and at < self._last_at:
            raise ValueError('replay must use causal receive time and stable sequence order')
        self._last_at = at

    # --------------------------------------------------------- compatibility
    @property
    def candidates(self):
        return self.alpha.active()

    @property
    def exit_reasons(self):
        return self.positions.exit_reasons()

    def _valid_quote(self, symbol, at):
        """Quote usable for entry decisions (fresh, synchronized, legal)."""
        return self._quote(symbol, at, 'entry')

    def valuation_quote(self, symbol, at):
        """Quote usable for valuation, stops and exit pricing (healthy stream)."""
        return self._quote(symbol, at, 'valuation')

    def _quote(self, symbol, at, purpose):
        quote = self.quotes.get(symbol)
        instrument = self.instruments.get(symbol)
        if quote is None or instrument is None or quote.source != self.features.config.quote_source:
            return None
        healthy = self.book.connected
        if purpose == 'valuation' and self.quality.require_quote_heartbeat:
            healthy = healthy and self.features.quote_stream_healthy(symbol, at)
        gate = validate_quote(quote, at, self.ticks, instrument.tick_category, self.quality,
                              stream_healthy=healthy, purpose=purpose, schedule=self.schedule)
        return quote if gate.allowed else None

    def _sync_risk(self, at):
        self.valuation.sync(at)

    def _fee_reserves(self):
        return self.valuation.fee_reserves()

    def _request_exit(self, symbol, at, reason, emergency):
        self.positions.request_exit(symbol, at, reason, emergency)

    # ----------------------------------------------------------------- inputs
    def set_market(self, snapshot, at):
        """External market snapshot (market_source='external' or explicit override)."""
        self._clock(at)
        if snapshot.at > at:
            raise ValueError('future market snapshot')
        self.market_snapshot = snapshot
        self.poll(at)

    def set_forecast(self, symbol, forecast, at):
        self._clock(at)
        if forecast.received_at != at:
            raise ValueError('forecast receive time must match replay time')
        previous = self.forecasts.get(symbol)
        if previous is None or forecast.received_at > previous.received_at:
            self.forecasts[symbol] = forecast

    def set_calibration(self, table: CalibrationTable, at):
        self._clock(at)
        if table.known_at > at:
            raise ValueError('calibration cannot be received before it is known')
        self.calibration = table
        self._record(at, 'CALIBRATION', version=table.version, rows=len(table.rows))

    def set_account(self, snapshot: AccountSnapshot, at):
        self._clock(at)
        if snapshot.received_at != at:
            raise ValueError('account snapshot receive time must match replay time')
        self.account_snapshot = snapshot
        self.poll(at)

    def set_exchange_status(self, normal: bool, at, reason=''):
        self._clock(at)
        if type(normal) is not bool:
            raise ValueError('exchange status must be a boolean')
        if normal != self.exchange_normal:
            self._record(at, 'EXCHANGE_STATUS', normal=normal, reason=reason)
        self.exchange_normal = normal
        self.poll(at)

    def on_quote(self, quote):
        self._clock(quote.at)
        if not self.features.on_quote(quote):
            raw = validate_quote(quote, quote.at, self.ticks, self.features.config.tick_category,
                                 self.quality, stream_healthy=self.book.connected, schedule=self.schedule)
            if not raw.allowed or quote.source != self.features.config.quote_source:
                self.quotes[quote.symbol] = quote
                if quote.symbol == self.features.benchmark_symbol:
                    self.market_snapshot = FeatureSnapshot('MARKET', quote.at, {}, False, reason='BENCHMARK_INVALID')
                self._invalidate(quote.symbol, quote.at, raw.reason or 'QUOTE_SOURCE_CHANGED')
                self.poll(quote.at)
            return
        self.quotes[quote.symbol] = quote
        self._mark_dirty(quote.symbol)
        self.poll(quote.at)

    def on_trade(self, trade):
        self._clock(trade.at)
        self.features.on_trade(trade)
        self._mark_dirty(trade.symbol)
        self.poll(trade.at)

    def on_cumulative_volume(self, symbol, at, total, last_price, event_id, source=None):
        self._clock(at)
        self.features.on_cumulative_volume(symbol, at, total, last_price, event_id,
                                           source=source or self.features.config.trade_source)
        self._mark_dirty(symbol)
        self.poll(at)

    def on_quote_stream_health(self, symbol, at, healthy, source=None):
        self._clock(at)
        ok = self.features.set_quote_stream_health(symbol, at, healthy, source)
        self._stream_health_changed(symbol, at, ok)

    def on_trade_stream_health(self, symbol, at, healthy, source='TBT'):
        self._clock(at)
        ok = self.features.set_trade_stream_health(symbol, at, healthy, source)
        self._stream_health_changed(symbol, at, ok)

    def _stream_health_changed(self, symbol, at, healthy):
        # A lost stream must stop working entries now (EXE-06); a heartbeat only
        # marks the symbol for the next batch evaluation.
        self._mark_dirty(symbol)
        if healthy:
            self.poll(at)
        else:
            self.reevaluate(symbol, at)

    def on_fill(self, *, exec_id, order_id, qty, price, at, correction_of=None, executed_at=None):
        self._clock(at)
        fill_time = aware(executed_at) if executed_at is not None else at
        if fill_time > at:
            self.risk.lock('future_execution_time')
            raise ValueError('execution time cannot exceed receipt time')
        new_execution = exec_id not in self.book.executions
        order = self.book.orders.get(order_id)
        first_fill = (new_execution and order is not None and order.side == Side.BUY
                      and not order.filled_quantity)
        entry = self._entry_snapshot(order, fill_time) if first_fill else None
        self.book.fill(exec_id, order_id, qty, price, fill_time, correction_of=correction_of)
        if first_fill:
            snapshot, score = entry
            # S_entry is the snapshot known at first fill; journaled so that a
            # journal restore and a snapshot restore agree (review EXE-04).
            self.book.set_entry_snapshot(order_id, score, snapshot.version if score is not None else None,
                                         source_at=snapshot.at if snapshot is not None else None, at=at)
            if score is None:
                self.risk.lock('first_fill_score_invalid')
        if order is not None and new_execution:
            self.execution_quality.on_fill(self.book.root_of(exec_id), order, fill_time,
                                           bust=Decimal(str(qty)) == 0)
        if order is not None:
            position = self.book.positions.get(order.symbol)
            if position is not None and position.quantity > 0 and position.stop_price is not None:
                rounded = self.ticks.round_price(position.stop_price, 'up', at,
                                                 self.instruments[order.symbol].tick_category)
                if rounded != position.stop_price:
                    self.book.tighten_stop(order.symbol, rounded, at)
        self.poll(at)

    def _entry_snapshot(self, order, fill_time):
        snapshot = next((s for s in reversed(self.snapshot_history[order.symbol])
                         if s.at <= fill_time), None)
        latest = self.snapshots.get(order.symbol)
        if latest is not None and latest.at <= fill_time and (snapshot is None or latest.at >= snapshot.at):
            snapshot = latest
        if (snapshot is None or not snapshot.valid or snapshot.version != self.config.score_version
                or not 0 <= (fill_time - snapshot.at).total_seconds() <= self.alpha.config.max_snapshot_age_seconds):
            return snapshot, None
        try:
            return snapshot, self.alpha.score(snapshot)
        except (KeyError, ValueError):
            return snapshot, None

    def kill_switch(self, at, reason='KILL_SWITCH'):
        self._clock(at)
        self.risk.lock(reason)
        self._record(at, 'KILL_SWITCH', reason=reason)
        self.poll(at)

    # ------------------------------------------------------------ scheduling
    def _mark_dirty(self, symbol):
        self._market_dirty = True
        if symbol in self.instruments:
            self._dirty.add(symbol)

    def flush(self, at=None):
        """Evaluate each changed symbol once per receive-time batch (review PERF-02).

        Same-time quote and trade updates therefore produce one snapshot, and
        market-layer features are computed once for the whole universe.
        """
        at = at or self._last_at
        if at is None:
            return
        self._clock(at)
        if self.config.market_source == 'internal' and self._market_dirty:
            self.market_snapshot = self.features.market_snapshot(at, self.instruments)
            self._market_dirty = False
            self.poll(at)
        dirty, self._dirty = sorted(self._dirty), set()
        for symbol in dirty:
            self.evaluate(self.features.snapshot(symbol, at), at)

    def reevaluate(self, symbol, at):
        """Data-health events re-check working entries immediately (review EXE-06)."""
        self.poll(at)
        if symbol in self.instruments:
            self._dirty.discard(symbol)
            self.evaluate(self.features.snapshot(symbol, at), at)

    def _refresh_market(self, at):
        snapshot = self.market_snapshot
        if not self.exchange_normal:
            self.market_state = MarketRegime.MARKET_RISK_OFF
        elif (snapshot is None or not snapshot.valid or
                not 0 <= (at - snapshot.at).total_seconds() <= self.config.market_max_age_seconds):
            # Missing or stale market data is unknown, not a market emergency.
            self.market_state = MarketRegime.MARKET_UNKNOWN
            self.risk.block('market_data_unavailable', at)
            return
        else:
            self.market_state = self.market_regime_engine.evaluate(snapshot, at)
        self.risk.unblock('market_data_unavailable')

    def has_exposure(self) -> bool:
        return any(p.quantity for p in self.book.positions.values()) or bool(self.book.active_orders())

    def poll(self, at):
        """Process this timer at least once per second, including absent quotes."""
        self._clock(at)
        self.book.check_timeouts(at, submit_timeout_seconds=self.config.order_timeout_seconds,
                                 cancel_timeout_seconds=self.config.order_timeout_seconds)
        self._refresh_market(at)
        self.valuation.sync(at)
        if self.has_exposure():
            # A persistent SOFT block only matters while risk is controlled.
            for reason in self.risk.escalate(at, self.config.max_soft_block_seconds):
                self._record(at, 'RISK_ESCALATED', reason=reason)
        self.execution_quality.observe(at, lambda symbol: self.valuation_quote(symbol, at),
                                       self.quality.max_age_seconds)
        self.positions.manage(at, self.book.active_by_symbol())

    # ------------------------------------------------------------- decisions
    def _invalidate(self, symbol, at, reason):
        if self.alpha.invalidate(symbol, at, reason) is not None:
            self._record(at, 'CANDIDATE_INVALIDATED', symbol=symbol, reason=reason)
        for order in self.book.active_orders(symbol):
            if order.side == Side.BUY:
                self.book.cancel(order.order_id, at)

    def entry_still_valid(self, order, at):
        return self.entries.still_valid(order, at)

    def evaluate(self, snapshot, at):
        self._clock(at)
        symbol = snapshot.symbol
        if symbol not in self.instruments:
            return
        self.snapshots[symbol] = snapshot
        history = self.snapshot_history[symbol]
        if not history or snapshot.at > history[-1].at:
            history.append(snapshot)
        elif snapshot.at == history[-1].at:
            history[-1] = snapshot
        enhanced_ready = self.subscriptions.ready(snapshot, at) if self.subscriptions else False
        regime = self.regime.evaluate(snapshot, at, market_regime=self.market_state,
                                      account_ok=not self.risk.locked)
        self.regime_time.observe(symbol, at, regime)
        self.entries.check_active(snapshot, regime, at, enhanced_ready)
        position = self.book.positions.get(symbol)
        if position is not None and position.quantity:
            self.positions.manage_signal(snapshot, regime, at, position)
            return
        self.entries.consider(snapshot, regime, at, enhanced_ready)
