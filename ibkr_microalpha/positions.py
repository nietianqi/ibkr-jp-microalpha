"""Held-position management: exits, cooldowns, residual alarms (review FLOW-04).

Hard exits (stop, maximum holding, planned session exits) need only a valuation
quote. Missing alpha features never force an exit by themselves: they reset the
signal-exit state and leave the hard exits in charge (review STR-02).
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .domain import MarketRegime, Regime, Side
from .market import JST, JapanSession
from .signals import AlphaDecayTracker


@dataclass
class ExitState:
    started_at: datetime
    reasons: list[str] = field(default_factory=list)
    emergency: bool = False

    @property
    def reason(self) -> str:
        """Cooldown/audit reason: a stop loss stays the governing reason once seen."""
        return 'STOP_LOSS' if 'STOP_LOSS' in self.reasons else self.reasons[-1]


class PositionManager:
    def __init__(self, engine):
        self.e = engine
        self.exits: dict[str, ExitState] = {}
        self.had_exposure: set[str] = set()
        self._low_score_since: dict[str, tuple[datetime, datetime]] = {}
        self._alarmed = set()

    def exit_reasons(self) -> dict[str, str]:
        return {symbol: state.reason for symbol, state in self.exits.items()}

    # ---------------------------------------------------------------- exits
    def request_exit(self, symbol, at, reason, emergency):
        e = self.e
        e._invalidate(symbol, at, reason)
        position = e.book.positions.get(symbol)
        active = e.book.active_orders(symbol)
        if (position is None or position.quantity <= 0) and not active:
            return
        state = self.exits.get(symbol)
        if state is None:
            state = self.exits[symbol] = ExitState(at, [reason], bool(emergency))
        else:
            if reason not in state.reasons:
                state.reasons.append(reason)
            state.emergency = state.emergency or bool(emergency)
        escalated = state.emergency or (at - state.started_at).total_seconds() >= e.config.exit_escalation_seconds
        if position is None or position.quantity <= 0:
            return
        quote = e.valuation_quote(symbol, at)
        if quote is None:
            e._note(at, 'EXIT_BLOCKED', symbol, 'no_reliable_exit_quote')
            return
        if not escalated and any(o.side == Side.BUY for o in active):
            return  # Ordinary exit waits for buy cancellation and fill reconciliation.
        sells = [o for o in active if o.side == Side.SELL]
        if escalated:
            for sell in sells:
                e.book.prioritize_risk(sell.order_id, at)
        available = e.book.sellable_quantity(symbol)
        if available <= 0:
            for order in sells:
                if ((at - state.started_at).total_seconds() >= e.config.exit_escalation_seconds
                        and order.limit_price > quote.bid):
                    e.book.cancel(order.order_id, at)
            return
        category = e.instruments[symbol].tick_category
        try:
            limit = e.ticks.move_ticks(quote.bid, -e.config.exit_slippage_ticks, at, category)
        except ValueError as error:
            # A price rule failure must not stop the risk loop for other symbols (EXE-03).
            e._note(at, 'EXIT_BLOCKED', symbol, f'exit price rule: {error}')
            e.risk.lock('exit_price_rule_invalid')
            return
        e._exit_sequence += 1
        try:
            order = e.book.submit(f'exit:{symbol}:{e._exit_sequence}', symbol, Side.SELL,
                                  available, limit, at, emergency=escalated)
        except ValueError as error:
            e._note(at, 'EXIT_BLOCKED', symbol, str(error))
            return
        e._clear_note('EXIT_BLOCKED', symbol)
        e.intents.link_exit(symbol, order.order_id)
        e.execution_quality.on_submit(order, quote, at)
        e._record(at, 'EXIT_REQUEST', symbol=symbol, order_id=order.order_id, reason=reason,
                  emergency=escalated, limit_price=str(limit))

    # --------------------------------------------------------------- timer
    def _residual_alarm(self, symbol, at, session, quantity, active):
        """Exit deadlines require flat controlled risk; otherwise alarm and lock."""
        half = 'MORNING' if session in (JapanSession.MORNING, JapanSession.LUNCH) else 'AFTERNOON'
        key = (symbol, at.astimezone(JST).date(), half)
        if key in self._alarmed:
            return
        self._alarmed.add(key)
        alarm = {'at': at.isoformat(), 'symbol': symbol, 'alarm': 'RESIDUAL_RISK_AFTER_EXIT_DEADLINE',
                 'session': half, 'position': quantity, 'active_orders': [o.order_id for o in active]}
        self.e.alarms.append(alarm)
        self.e.risk.lock('session_residual_risk')
        self.e._record(at, 'ALARM', **{k: v for k, v in alarm.items() if k != 'at'})

    def _flat(self, symbol, at):
        e = self.e
        state = self.exits.pop(symbol, None)
        reason = state.reason if state else 'normal'
        if reason == 'STOP_LOSS':
            e.consecutive_stops[symbol] += 1
            # Repeated stops lengthen the cooldown; the frozen limit disables the symbol.
            seconds = e.config.stop_cooldown_seconds * e.consecutive_stops[symbol]
            if e.consecutive_stops[symbol] >= e.config.max_consecutive_stops:
                e.disabled_symbols[symbol] = 'consecutive_stop_losses'
                e._record(at, 'SYMBOL_DISABLED', symbol=symbol, reason='consecutive_stop_losses',
                          count=e.consecutive_stops[symbol])
        else:
            if e.intents.had_fill(symbol, e.book):
                e.consecutive_stops.pop(symbol, None)
            seconds = e.config.ordinary_cooldown_seconds
        closed = e.intents.close(symbol, at, e.book, e.commissions)
        if closed is not None and closed.get('stress_exceeded'):
            e._record(at, 'STRESS_BUDGET_EXCEEDED', symbol=symbol, intent_id=closed['intent_id'],
                      net_amount=closed['net_amount'], stress_budget=closed['stress_budget'])
        e.cooldown_until[symbol] = at + timedelta(seconds=seconds)
        self.had_exposure.discard(symbol)
        e.decay_trackers.pop(symbol, None)
        self._low_score_since.pop(symbol, None)
        e._clear_note('EXIT_BLOCKED', symbol)
        e.risk.unblock(f'position_quote_invalid:{symbol}')
        e._record(at, 'COOLDOWN', symbol=symbol, until=e.cooldown_until[symbol].isoformat(), reason=reason)

    def manage(self, at, active_by_symbol):
        e = self.e
        deadline = e.calendar.planned_exit_deadline(at)
        session = e.calendar.session(at)
        exit_all = e.risk.locked or e.market_state == MarketRegime.MARKET_RISK_OFF
        for symbol in e.instruments:
            position = e.book.positions.get(symbol)
            active = active_by_symbol.get(symbol, [])
            held = position is not None and position.quantity > 0
            if (held or active) and ((deadline is not None and at >= deadline)
                    or (symbol in self.had_exposure and session in (
                        JapanSession.LUNCH, JapanSession.CLOSING_AUCTION, JapanSession.CLOSED))):
                self._residual_alarm(symbol, at, session, position.quantity if held else 0, active)
            if held or active:
                self.had_exposure.add(symbol)
            elif symbol in self.had_exposure:
                self._flat(symbol, at)
            if exit_all:
                self.request_exit(symbol, at, 'RISK_LOCK' if e.risk.locked else 'MARKET_RISK_OFF', True)
                continue
            e.entries.check_candidate(symbol, at)
            if not held:
                continue
            quote = e.valuation_quote(symbol, at)
            if quote is None:
                continue  # SOFT block raised by valuation; escalation is timer driven.
            if position.stop_price is not None and quote.bid <= position.stop_price:
                self.request_exit(symbol, at, 'STOP_LOSS', True)
                continue
            max_hold = position.max_holding_seconds or e.config.holding_seconds
            if position.entry_time is not None and (at - position.entry_time).total_seconds() >= max_hold:
                self.request_exit(symbol, at, 'MAX_HOLDING', True)
                continue
            if e.schedule.planned_exit_due(at):
                self.request_exit(symbol, at, 'PLANNED_EXIT', True)
                continue
            if symbol in self.exits:
                state = self.exits[symbol]
                self.request_exit(symbol, at, state.reasons[-1], state.emergency)

    # -------------------------------------------------------------- signals
    def _reset_signal_state(self, symbol):
        self.e.decay_trackers.pop(symbol, None)
        self._low_score_since.pop(symbol, None)

    def manage_signal(self, snapshot, regime, at, position):
        """Signal exits for a held position (hard exits run in ``manage``)."""
        e = self.e
        symbol = snapshot.symbol
        if not snapshot.valid or snapshot.version != e.config.score_version:
            # Feature warmup is not a valuation failure: never join counts across
            # the gap, keep stop/max-hold/planned exits in charge (T24).
            self._reset_signal_state(symbol)
            e._note(at, 'HELD_FEATURES_INVALID', symbol, snapshot.reason or 'version mismatch')
            return
        e._clear_note('HELD_FEATURES_INVALID', symbol)
        if regime == Regime.BEARISH:
            self.request_exit(symbol, at, 'BEARISH', False)
            return
        if regime == Regime.RISK_OFF:
            self.request_exit(symbol, at, 'RISK_OFF', True)
            return
        if e.decay_config is not None:
            if position.entry_score is None or position.entry_time is None:
                e.risk.lock('decay_entry_snapshot_invalid')
                self.request_exit(symbol, at, 'DATA_RISK', True)
                return
            tracker = e.decay_trackers.get(symbol)
            if tracker is None:
                tracker = AlphaDecayTracker(position.entry_score, position.score_version,
                                            position.entry_time, e.decay_config, e.alpha.scalers,
                                            e.alpha.weights)
                e.decay_trackers[symbol] = tracker
            decision = tracker.evaluate(snapshot, at)
            if decision.data_risk:
                e._note(at, 'HELD_FEATURES_INVALID', symbol, decision.reason)
            elif decision.exit_required:
                self.request_exit(symbol, at, 'ALPHA_DECAY', False)
            return
        try:
            score = e.alpha.score(snapshot)
        except (KeyError, ValueError):
            self._reset_signal_state(symbol)
            e._note(at, 'HELD_FEATURES_INVALID', symbol, 'score unavailable')
            return
        if score < e.alpha.config.exit_score:
            since, last = self._low_score_since.get(symbol, (snapshot.at, snapshot.at))
            if (snapshot.at - last).total_seconds() > e.alpha.config.max_snapshot_age_seconds:
                since = snapshot.at
            self._low_score_since[symbol] = (since, max(last, snapshot.at))
            if (snapshot.at - since).total_seconds() >= e.config.signal_exit_persistence_seconds:
                self.request_exit(symbol, at, 'ALPHA_EXIT', False)
        else:
            self._low_score_since.pop(symbol, None)
