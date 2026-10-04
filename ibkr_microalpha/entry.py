"""Entry pipeline in specification order (review FLOW-01/FLOW-02).

preconditions -> alpha candidate (TTL starts) -> sizing (stop, risk allocation,
immutable price cap) -> economics (frozen calibration or research forecast,
priced at the actual limit) -> confirmation -> submit.

The same economics are re-checked for working entries and immediately before a
queued entry is sent (``still_valid``, review EXE-02).
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .domain import Confirmation, MarketRegime, Regime, Side
from .economics import Prediction, adjust_prediction_for_price, prediction_gate
from .execution import ExecutionError

BPS = Decimal(10000)
# Plan failures raised by sizing/risk rather than by the economic gate.
RISK_REASONS = frozenset({'risk_locked', 'entries_blocked', 'account_unverified', 'missing_ownership_or_sector',
                          'entry_intent_limit', 'existing_position_or_intent', 'position_limit',
                          'less_than_one_legal_lot', 'stop_distance_unavailable'})


@dataclass(frozen=True)
class EntryPlan:
    candidate_id: str
    quantity: int
    max_price: Decimal
    limit_price: Decimal
    stop_distance: Decimal
    prediction: Prediction
    reservation_key: str
    stress_loss: Decimal


class EntryPipeline:
    def __init__(self, engine):
        self.e = engine
        self._caps: dict[str, Decimal] = {}            # candidate_id -> immutable chase cap
        self._known: set[str] = set()                  # candidates already counted
        self._rejected: set[str] = set()

    # ------------------------------------------------------------- helpers
    def caution_floor(self):
        return self.e.market_regime_engine.config.caution_floor

    def market_admits(self) -> bool:
        state = self.e.market_state
        return state == MarketRegime.MARKET_OK or (
            state == MarketRegime.MARKET_CAUTION and self.caution_floor() is not None)

    def stop_distance(self, snapshot, quote, at, instrument):
        """JPY/share = max(min ticks, stop_bps x price, k x known volatility x price)."""
        c, price = self.e.config, quote.ask
        tick = self.e.ticks.tick_size(price, at, instrument.tick_category)
        distance = max(tick * c.min_stop_ticks, c.stop_bps * price / BPS)
        if c.stop_volatility_multiple > 0:
            volatility = snapshot.values.get('volatility_bps')
            if volatility is None or volatility < 0:
                return None
            distance = max(distance, c.stop_volatility_multiple * Decimal(str(volatility)) / BPS * price)
        return distance if distance < price else None

    def exit_slippage(self, quote, at, instrument):
        tick = self.e.ticks.tick_size(quote.bid, at, instrument.tick_category)
        return tick * self.e.config.exit_slippage_ticks

    def _fresh_forecast(self, symbol, at):
        forecast = self.e.forecasts.get(symbol)
        if (forecast is None or forecast.received_at > at or at >= forecast.valid_until
                or (at - forecast.received_at).total_seconds() > self.e.config.forecast_max_age_seconds):
            return None
        return forecast

    def _calibrated_quantities(self, symbol, at):
        c = self.e.config
        if c.economics_source == 'forecast':
            forecast = self._fresh_forecast(symbol, at)
            return ((forecast.prediction.quantity,), None) if forecast else ((), 'forecast_missing_or_stale')
        table = self.e.calibration
        if table is None or at < table.known_at:
            return (), 'calibration_unavailable'
        quantities = table.quantities(c.policy_id, c.model_version, c.holding_seconds)
        return (quantities, None) if quantities else ((), 'calibration_unavailable')

    def _prediction(self, candidate, quantity, at):
        """(prediction at its reference price, reference price, max chase ticks or cap) or reason."""
        c = self.e.config
        if c.economics_source == 'forecast':
            forecast = self._fresh_forecast(candidate.symbol, at)
            if forecast is None:
                return None, 'forecast_missing_or_stale'
            if forecast.prediction.quantity != quantity:
                return None, 'calibrated_quantity_unavailable'
            return (forecast.prediction, forecast.reference_entry_price, forecast.max_entry_price), ''
        table = self.e.calibration
        if table is None:
            return None, 'calibration_unavailable'
        row = table.lookup(policy_id=c.policy_id, version=c.model_version, holding_seconds=c.holding_seconds,
                           quantity=quantity, score=candidate.entry_score, at=at)
        if row is None:
            return None, 'calibration_bucket_unavailable'
        return (row.prediction(), candidate.reference_ask, row.max_chase_ticks), ''

    def _cap(self, candidate, cap_source, at, category):
        cap = self._caps.get(candidate.candidate_id)
        if cap is None:
            if isinstance(cap_source, int):
                cap = self.e.ticks.move_ticks(candidate.reference_ask, cap_source, at, category)
            else:
                cap = self.e.ticks.round_price(cap_source, 'down', at, category)
            self._caps[candidate.candidate_id] = cap
        return cap

    def _gate(self, prediction, reference, limit, quantity):
        c = self.e.config
        adjusted = adjust_prediction_for_price(prediction, reference_price=reference, limit_price=limit,
                                               commissions=self.e.commissions)
        margin = c.net_safety_margin_bps * limit * quantity / BPS
        gate = prediction_gate(adjusted, policy_id=c.policy_id, version=c.model_version, quantity=quantity,
                               min_samples=c.min_samples, safety_margin=margin,
                               max_holding_seconds=c.holding_seconds)
        return adjusted, gate

    # ------------------------------------------------------------- pipeline
    def preconditions(self, symbol, quote, regime, at):
        e = self.e
        if e.risk.locked:
            return 'risk_locked'
        if e.risk.soft_blocks:
            return 'entries_blocked'
        if e.book.locked or not e.book.reconciled:
            return 'reconciliation_lock'
        if symbol in e.disabled_symbols:
            return 'symbol_disabled_after_stops'
        if at < e.cooldown_until.get(symbol, at):
            return 'cooldown'
        instrument = e.instruments[symbol]
        if not instrument.contract_verified or not instrument.permission_verified:
            return 'contract_or_permission_unknown'
        if quote is None:
            return 'quote_invalid'
        gate = e.calendar.entry_gate(at, max_hold_seconds=e.config.holding_seconds,
                                     remaining_wait_seconds=e.alpha.config.ttl_seconds,
                                     submit_latency_seconds=e.config.submit_p99_seconds,
                                     exit_buffer_seconds=e.config.exit_buffer_seconds,
                                     market_status=quote.market_status)
        if not gate.allowed:
            return gate.reason
        if regime != Regime.LONG:
            return 'environment_not_long'
        return None

    def plan(self, candidate, snapshot, quote, at):
        """Return (EntryPlan, '') with a held risk reservation, or (None, reason)."""
        e, c = self.e, self.e.config
        symbol = candidate.symbol
        instrument = e.instruments[symbol]
        category = instrument.tick_category
        quantities, reason = self._calibrated_quantities(symbol, at)
        if reason:
            return None, reason
        requested = max((q for q in quantities if q <= c.max_entry_quantity), default=None)
        if requested is None:
            return None, 'calibrated_quantity_unavailable'
        found, reason = self._prediction(candidate, requested, at)
        if found is None:
            return None, reason
        cap = self._cap(candidate, found[2], at, category)
        if quote.ask > cap:
            return None, 'economic_price_cap'
        limit = min(cap, e.ticks.move_ticks(quote.ask, c.entry_limit_ticks, at, category))
        stop = self.stop_distance(snapshot, quote, at, instrument)
        if stop is None:
            return None, 'stop_distance_unavailable'
        key = f'allocation:{symbol}'
        allocation = e.risk.allocate(
            key, symbol, instrument.sector, limit, stop, self.exit_slippage(quote, at, instrument),
            quote.ask_size, requested,
            lambda q, p: (e.commissions.commission(q * p), e.commissions.commission(q * p)),
            gap_reserve=c.gap_reserve_bps * limit / BPS)
        if not allocation.allowed:
            return None, allocation.reason
        quantity = allocation.reservation.quantity
        if quantity != requested:
            found, reason = self._prediction(candidate, quantity, at)
            if found is None:
                e.risk.release(key)
                return None, reason
        prediction, reference, _ = found
        adjusted, gate = self._gate(prediction, reference, limit, quantity)
        if not gate.allowed:
            e.risk.release(key)
            return None, gate.reason
        return EntryPlan(candidate.candidate_id, quantity, cap, limit, stop, adjusted, key,
                         allocation.reservation.stress_loss), ''

    def recheck_economics(self, candidate, order, at):
        """Economics of a working entry at its own quantity and limit price."""
        found, reason = self._prediction(candidate, order.quantity, at)
        if found is None:
            return reason
        prediction, reference, _ = found
        _, gate = self._gate(prediction, reference, order.limit_price, order.quantity)
        return None if gate.allowed else gate.reason

    def consider(self, snapshot, regime, at, enhanced_ready):
        e = self.e
        symbol = snapshot.symbol
        if e.book.active_orders(symbol):
            return  # A working entry or exit is managed elsewhere.
        quote = e._valid_quote(symbol, at)
        reason = self.preconditions(symbol, quote, regime, at)
        if reason:
            if reason == 'environment_not_long':
                e._invalidate(symbol, at, 'ENVIRONMENT_NOT_LONG')
            e._reject(at, symbol, reason)
            return
        candidate = e.alpha.evaluate(snapshot, quote, regime, e.market_state, at,
                                     caution_floor=self.caution_floor())
        if candidate is None:
            return  # No signal is not a rejection.
        if candidate.candidate_id not in self._known:
            self._known.add(candidate.candidate_id)
            e.funnel['candidates'] += 1
            e._record(at, 'CANDIDATE', symbol=symbol, candidate_id=candidate.candidate_id,
                      score=candidate.entry_score, expires_at=candidate.expires_at.isoformat())
            if e.subscriptions is not None:
                e.subscriptions.record_candidate(symbol, at)
        plan, reason = self.plan(candidate, snapshot, quote, at)
        if plan is None:
            if candidate.candidate_id not in self._rejected:
                self._rejected.add(candidate.candidate_id)
                e.funnel['risk_rejected' if reason in RISK_REASONS else 'economics_rejected'] += 1
            if reason != 'economic_price_cap':
                # Section 7: cost or risk rejection invalidates the candidate.
                e._invalidate(symbol, at, reason)
            e._reject(at, symbol, reason)
            return
        try:
            result = e.confirmation.evaluate(candidate, snapshot, quote, at, max_price=plan.max_price,
                                             enhanced_ready=enhanced_ready, net_advantage_positive=True)
            if result.status != Confirmation.CONFIRM:
                e._reject(at, symbol, result.reason)
                if result.status == Confirmation.VETO:
                    e._invalidate(symbol, at, result.reason)
                return
            e.funnel['confirmed'] += 1
            order_expiry = min(candidate.expires_at,
                               at + timedelta(seconds=e.config.entry_order_ttl_seconds))
            try:
                order = e.book.submit(candidate.candidate_id, symbol, Side.BUY, plan.quantity,
                                      plan.limit_price, at, entry_score=candidate.entry_score,
                                      score_version=candidate.score_version, stop_distance=plan.stop_distance,
                                      candidate_expires_at=order_expiry,
                                      max_holding_seconds=e.config.holding_seconds,
                                      exit_policy_version=e.config.policy_id)
            except ExecutionError as error:
                # The ledger refused the request (state changed since the gates ran).
                e._invalidate(symbol, at, 'submit_refused')
                e._reject(at, symbol, f'submit_refused: {error}')
                return
            e.risk.note_entry_intent()
            e.funnel['intents'] += 1
            e._last_reject.pop(symbol, None)
            e.intents.open(candidate.candidate_id, symbol, order.order_id, at, plan.quantity,
                           stress_budget=plan.stress_loss)
            e.execution_quality.on_submit(order, quote, at)
            e._record(at, 'ENTRY_REQUEST', symbol=symbol, order_id=order.order_id,
                      candidate_id=candidate.candidate_id, quantity=plan.quantity,
                      limit_price=str(plan.limit_price), max_price=str(plan.max_price),
                      ask=str(quote.ask), order_expires_at=order_expiry.isoformat())
        finally:
            e.risk.release(plan.reservation_key)
            e.valuation.sync(at)

    def check_active(self, snapshot, regime, at, enhanced_ready):
        e = self.e
        symbol = snapshot.symbol
        buys = [o for o in e.book.active_orders(symbol) if o.side == Side.BUY]
        if not buys:
            return
        candidate = e.alpha.current(symbol)
        quote = e._valid_quote(symbol, at)
        if candidate is None or quote is None or regime != Regime.LONG:
            e._invalidate(symbol, at, 'ACTIVE_ENTRY_INVALID')
            return
        if e.alpha.evaluate(snapshot, quote, regime, e.market_state, at,
                            caution_floor=self.caution_floor()) is None:
            e._invalidate(symbol, at, 'ACTIVE_ALPHA_INVALID')
            return
        cap = self._caps.get(candidate.candidate_id)
        reason = self.recheck_economics(candidate, buys[0], at)
        if reason or cap is None or quote.ask > cap:
            e._invalidate(symbol, at, reason or 'economic_price_cap')
            return
        confirmation = e.confirmation.evaluate(candidate, snapshot, quote, at, max_price=cap,
                                               enhanced_ready=enhanced_ready, net_advantage_positive=True)
        if confirmation.status == Confirmation.VETO:
            e._invalidate(symbol, at, confirmation.reason)

    def check_candidate(self, symbol, at):
        """Timer-driven expiry and validity of an active candidate."""
        e = self.e
        candidate = e.alpha.current(symbol)
        if candidate is None:
            return
        cap = self._caps.get(candidate.candidate_id)
        gate = e.calendar.entry_gate(at, max_hold_seconds=e.config.holding_seconds,
                                     remaining_wait_seconds=max(0, (candidate.expires_at - at).total_seconds()),
                                     submit_latency_seconds=e.config.submit_p99_seconds,
                                     exit_buffer_seconds=e.config.exit_buffer_seconds)
        quote = e._valid_quote(symbol, at)
        if (at >= candidate.expires_at or quote is None or not gate.allowed
                or (cap is not None and quote.ask > cap)):
            e._invalidate(symbol, at, 'CANDIDATE_EXPIRED_OR_INVALID')

    def still_valid(self, order, at):
        """Pre-send re-check of a queued entry at the actual send time (EXE-02)."""
        e = self.e
        if e.risk.entries_blocked:
            return 'entries_blocked'
        if e.book.locked or not e.book.reconciled:
            return 'reconciliation_lock'
        if not self.market_admits():
            return 'market_not_ok'
        candidate = e.alpha.current(order.symbol)
        if candidate is None or candidate.candidate_id != order.intent_key or at >= candidate.expires_at:
            return 'candidate_invalid_or_expired'
        quote = e._valid_quote(order.symbol, at)
        if quote is None:
            return 'quote_invalid'
        cap = self._caps.get(candidate.candidate_id)
        if cap is None or quote.ask > cap:
            return 'quote_above_cap'
        if quote.ask > order.limit_price:
            return 'not_marketable'
        remaining = 0 if order.candidate_expires_at is None else max(
            0, (order.candidate_expires_at - at).total_seconds())
        gate = e.calendar.entry_gate(at, max_hold_seconds=e.config.holding_seconds,
                                     remaining_wait_seconds=remaining,
                                     submit_latency_seconds=e.config.submit_p99_seconds,
                                     exit_buffer_seconds=e.config.exit_buffer_seconds,
                                     market_status=quote.market_status)
        if not gate.allowed:
            return gate.reason
        return self.recheck_economics(candidate, order, at)
