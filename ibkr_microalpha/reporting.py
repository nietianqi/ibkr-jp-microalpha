"""Daily layer funnel, per-intent outcomes and execution-quality diagnostics.

Everything here is derived from the causal coordinator state and the execution
ledger. It reports evidence; it never changes trading decisions.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from math import log

from .domain import Regime, Side
from .economics import IntentPath, PathFill, intent_path_value

ZERO = Decimal(0)
DRIFT_HORIZONS = (5, 30, 120)
_EPOCH = datetime.fromisoformat('2000-01-01T00:00:00+00:00')


@dataclass
class Intent:
    intent_id: str
    symbol: str
    created_at: datetime | None
    target_quantity: int
    stress_budget: Decimal | None = None
    entry_orders: list[int] = field(default_factory=list)
    exit_orders: list[int] = field(default_factory=list)
    closed_at: datetime | None = None


class IntentLedger:
    """One controlled entry intent per symbol; exit child orders attach to it."""

    def __init__(self):
        self.intents: dict[str, Intent] = {}
        self._current: dict[str, str] = {}
        # Stable ownership survives a temporary flat/closed state and later busts.
        self.order_to_intent: dict[int, str] = {}

    def _bind(self, order_id, intent):
        owner = self.order_to_intent.get(order_id)
        if owner is not None and owner != intent.intent_id:
            raise ValueError("order already belongs to another intent")
        self.order_to_intent[order_id] = intent.intent_id

    def open(self, intent_id, symbol, order_id, at, quantity, *, stress_budget=None):
        intent = self.intents.get(intent_id)
        if intent is None:
            intent = self.intents[intent_id] = Intent(intent_id, symbol, at, quantity, stress_budget)
        elif intent.symbol != symbol:
            raise ValueError("intent identity reused for another symbol")
        self._bind(order_id, intent)
        if order_id not in intent.entry_orders:
            intent.entry_orders.append(order_id)
        self._current[symbol] = intent_id

    def current(self, symbol) -> Intent | None:
        identity = self._current.get(symbol)
        return self.intents.get(identity) if identity else None

    def link_exit(self, symbol, order_id, *, intent_id=None):
        owner = self.order_to_intent.get(order_id)
        if owner is not None:
            if intent_id is not None and owner != intent_id:
                raise ValueError("exit order already belongs to another intent")
            intent = self.intents[owner]
        else:
            intent = self.intents.get(intent_id) if intent_id is not None else self.current(symbol)
        if intent is None:
            # Reconciled positions without a local intent stay visible as their own record.
            if intent_id is not None and intent_id != f'unattributed:{symbol}':
                raise ValueError("unknown exit intent")
            intent = Intent(intent_id or f'unattributed:{symbol}:{order_id}', symbol, None, 0)
            self.intents[intent.intent_id] = intent
            self._current[symbol] = intent.intent_id
        if intent.symbol != symbol:
            raise ValueError("exit symbol differs from its intent")
        self._bind(order_id, intent)
        if order_id not in intent.exit_orders:
            intent.exit_orders.append(order_id)

    @staticmethod
    def _residual(intent, book):
        return (sum(book.orders[o].filled_quantity for o in intent.entry_orders if o in book.orders)
                - sum(book.orders[o].filled_quantity for o in intent.exit_orders if o in book.orders))

    def reconcile(self, book, at):
        """Refresh intent state after fills, corrections or an account barrier.

        Ownership comes from order identities, never the most recent same-symbol
        candidate. Reopening an older intent does not displace a later live one.
        Full strategy restoration reconstructs this index from the raw inputs.
        """
        live = {}
        for intent in self.intents.values():
            for order_id in intent.entry_orders + intent.exit_orders:
                self._bind(order_id, intent)
            active = any(book.orders[o].active for o in intent.entry_orders + intent.exit_orders
                         if o in book.orders)
            if self._residual(intent, book) or active:
                intent.closed_at = None
                live.setdefault(intent.symbol, []).append(intent)
            elif intent.closed_at is None:
                intent.closed_at = at
        for symbol, intents in live.items():
            identities = {i.intent_id for i in intents}
            if self._current.get(symbol) not in identities:
                chosen = max(intents, key=lambda i: (i.created_at or _EPOCH, i.intent_id))
                self._current[symbol] = chosen.intent_id

    def exit_allocations(self, symbol, available, book):
        """Partition confirmed sellable shares into separately owned child orders."""
        if type(available) is not int or available < 0:
            raise ValueError("exit allocation needs nonnegative integral shares")
        remaining, result = min(available, book.sellable_quantity(symbol)), []
        intents = sorted((i for i in self.intents.values() if i.symbol == symbol),
                         key=lambda i: (i.created_at or _EPOCH, i.intent_id))
        for intent in intents:
            reserved = sum(book.orders[o].possible_remaining for o in intent.exit_orders
                           if o in book.orders and book.orders[o].active)
            quantity = min(remaining, max(0, self._residual(intent, book) - reserved))
            if quantity:
                result.append((intent.intent_id, quantity))
                remaining -= quantity
            if not remaining:
                break
        if remaining:
            # Never invent buys or attribute unknown inventory to the last trade.
            result.append((f'unattributed:{symbol}', remaining))
        return result

    def had_fill(self, symbol, book) -> bool:
        intent = self.current(symbol)
        return intent is not None and any(
            book.orders[o].filled_quantity for o in intent.entry_orders if o in book.orders)

    def close(self, symbol, at, book=None, commissions=None):
        intent = self.current(symbol)
        if book is not None:
            for owned in self.intents.values():
                if owned.symbol == symbol and not self._residual(owned, book) and not any(
                        book.orders[o].active for o in owned.entry_orders + owned.exit_orders if o in book.orders):
                    owned.closed_at = owned.closed_at or at
            # Net account flatness can conceal opposing residuals after a late
            # correction. It never proves each complete intent was flattened.
            if intent is not None and (self._residual(intent, book) or any(
                    book.orders[o].active for o in intent.entry_orders + intent.exit_orders if o in book.orders)):
                intent.closed_at = None
                return self._row(intent, book, commissions, None, at) if commissions is not None else None
        self._current.pop(symbol, None)
        if intent is None:
            return None
        if intent.closed_at is None:
            intent.closed_at = at
        if book is None or commissions is None:
            return None
        return self._row(intent, book, commissions, None, at)

    def _row(self, intent, book, commissions, valuation, at):
        orders = {o: book.orders[o] for o in intent.entry_orders + intent.exit_orders if o in book.orders}
        fills = sorted(((root, fill) for root, fill in book.current_executions()
                        if fill.order_id in orders and fill.quantity),
                       key=lambda item: (book.executions[item[0]].at, book.executions[item[0]].sequence))
        bought = sum(f.quantity for _, f in fills if orders[f.order_id].side == Side.BUY)
        sold = sum(f.quantity for _, f in fills if orders[f.order_id].side == Side.SELL)
        buy_amount = sum((f.price * f.quantity for _, f in fills if orders[f.order_id].side == Side.BUY), ZERO)
        sell_amount = sum((f.price * f.quantity for _, f in fills if orders[f.order_id].side == Side.SELL), ZERO)
        actual = sum((book.order_fees(o) for o in orders), ZERO)
        reserve = sum((max(ZERO, commissions.commission(order.filled_notional) - book.order_fees(o))
                       for o, order in orders.items() if order.filled_quantity), ZERO)
        residual = bought - sold
        active = any(order.active for order in orders.values())
        status = 'UNFILLED' if not bought and not sold else ('OPEN' if residual or active else 'CLOSED')
        quote = valuation(intent.symbol) if (valuation is not None and residual > 0) else None
        path = IntentPath(
            candidate_id=intent.intent_id, policy_id='controlled',
            quantity=max(intent.target_quantity, bought, 1), evaluation_at=at,
            fills=tuple(PathFill(f.exec_id, str(f.order_id), orders[f.order_id].side.value, f.quantity, f.price)
                        for _, f in fills),
            applicable_fees=actual + reserve, controlled_residual_quantity=max(0, residual),
            residual_bid=quote.bid if quote is not None else None, residual_markdown_per_share=ZERO,
            remaining_exit_cost=(commissions.commission(quote.bid * residual) if quote is not None else None),
            state_reconciled=book.reconciled or status == 'CLOSED',
            residual_bid_at=quote.at if quote is not None else None,
            max_residual_quote_age_seconds=3600.0 if quote is not None else None)
        value = intent_path_value(path)
        net = value.net_amount if value.estimable else None
        stress_exceeded = (status == 'CLOSED' and net is not None and intent.stress_budget is not None
                           and -net > intent.stress_budget)
        return {
            'intent_id': intent.intent_id, 'symbol': intent.symbol,
            'created_at': intent.created_at.isoformat() if intent.created_at else None,
            'target_quantity': intent.target_quantity, 'status': status,
            'bought': bought, 'sold': sold,
            'buy_amount': str(buy_amount), 'sell_amount': str(sell_amount),
            'actual_fees': str(actual), 'unreported_fee_reserve': str(reserve),
            # CLOSED: realized net. OPEN: conservative residual value at the valuation bid.
            'net_amount': str(net) if net is not None else None,
            'valuation': ('realized' if status != 'OPEN' else
                          'conservative_residual' if net is not None else 'not_estimable'),
            'valuation_reason': value.reason,
            'stress_budget': str(intent.stress_budget) if intent.stress_budget is not None else None,
            'stress_exceeded': stress_exceeded,
            'child_orders': len(orders)}

    def outcomes(self, book, commissions, *, valuation=None, at=None):
        """Rows valued at ``at`` (report time); residuals use ``valuation(symbol)``."""
        rows = []
        for intent in self.intents.values():
            when = at or intent.closed_at or intent.created_at or _EPOCH
            rows.append(self._row(intent, book, commissions, valuation, when))
        return rows


@dataclass
class _Drift:
    root: str
    symbol: str
    side: Side
    fill_at: datetime
    reference_mid: Decimal | None
    results: dict = field(default_factory=dict)
    observed: dict = field(default_factory=dict)


class ExecutionQuality:
    """Arrival-price cost per current execution and post-fill midpoint drift at 5/30/120 s.

    Costs are read from the ledger's current revisions at report time, so a
    correction replaces the original and a bust removes it (review RPT-02).
    """

    def __init__(self, horizons=DRIFT_HORIZONS):
        self.horizons = tuple(horizons)
        self.arrival: dict[int, Decimal] = {}
        self._pending: dict[str, _Drift] = {}
        self._done: dict[str, _Drift] = {}

    def on_submit(self, order, quote, at):
        self.arrival.setdefault(order.order_id, quote.mid)

    def on_fill(self, root, order, at, *, bust=False):
        if root is None:
            return
        if bust:
            self._pending.pop(root, None)
            self._done.pop(root, None)
            return
        if root in self._pending or root in self._done:
            return  # A quantity/price correction keeps the original fill-time sample.
        self._pending[root] = _Drift(root, order.symbol, order.side, at, None)

    def observe(self, at, valid_quote, max_age_seconds):
        for root, drift in list(self._pending.items()):
            quote = valid_quote(drift.symbol)
            if drift.reference_mid is None:
                if (quote is not None and drift.fill_at - timedelta(seconds=max_age_seconds) <= quote.at <= at
                        and (at - drift.fill_at).total_seconds() <= max_age_seconds):
                    drift.reference_mid = quote.mid
                elif (at - drift.fill_at).total_seconds() > max_age_seconds:
                    drift.results = {h: None for h in self.horizons}
            else:
                for horizon in self.horizons:
                    if horizon in drift.results:
                        continue
                    target = drift.fill_at + timedelta(seconds=horizon)
                    if at < target:
                        break
                    if (at - target).total_seconds() > max_age_seconds:
                        drift.results[horizon] = None  # Unavailable; never forward-filled.
                        continue
                    if quote is None or quote.at < target:
                        continue  # A same-time quote arriving later may still measure it.
                    sign = 1 if drift.side == Side.BUY else -1
                    drift.results[horizon] = sign * 10000 * log(float(quote.mid / drift.reference_mid))
                    drift.observed[horizon] = quote.at.isoformat()
            if len(drift.results) == len(self.horizons):
                self._done[root] = self._pending.pop(root)

    def drifts(self):
        return {**self._done, **self._pending}

    def summary(self, book):
        fills = []
        for root, fill in book.current_executions():
            if fill.quantity == 0:
                continue  # A bust leaves no execution cost.
            order = book.orders[fill.order_id]
            arrival = self.arrival.get(order.order_id)
            sign = 1 if order.side == Side.BUY else -1
            # Buy loss: (fill - arrival) x qty; sell loss: (arrival - fill) x qty.
            cost = sign * (fill.price - arrival) * fill.quantity if arrival is not None else None
            fills.append({'root': root, 'exec_id': fill.exec_id, 'order_id': order.order_id,
                          'symbol': order.symbol, 'side': order.side.value, 'qty': fill.quantity,
                          'price': str(fill.price),
                          'arrival_mid': str(arrival) if arrival is not None else None,
                          'price_cost': str(cost) if cost is not None else None,
                          'emergency': order.emergency, 'corrected': fill.exec_id != root})
        by_side = {}
        drifts = self.drifts()
        for side in (Side.BUY, Side.SELL):
            entries = [d for d in drifts.values() if d.side == side]
            stats = {}
            for horizon in self.horizons:
                values = [d.results[horizon] for d in entries if d.results.get(horizon) is not None]
                stats[f'{horizon}s'] = {'samples': len(values),
                                        'mean_signed_bps': sum(values) / len(values) if values else None,
                                        'unavailable': sum(1 for d in entries
                                                           if horizon in d.results and d.results[horizon] is None)}
            by_side[side.value] = stats
        total_cost = sum((Decimal(f['price_cost']) for f in fills if f['price_cost'] is not None), ZERO)
        return {'fills': fills, 'total_price_cost_vs_arrival': str(total_cost),
                'post_fill_drift': by_side,
                'drift_sign': 'positive is favourable to the filled side',
                'missed_opportunity_rate': 'NOT_COMPUTED: requires frozen realized-signal labels'}


class RegimeTime:
    """Seconds observed in each individual-stock regime; gaps beyond the frozen age are excluded."""

    def __init__(self, max_gap_seconds):
        self.max_gap = float(max_gap_seconds)
        self._last: dict[str, tuple[datetime, Regime]] = {}
        self.seconds: dict[str, dict[str, float]] = {}

    def observe(self, symbol, at, regime):
        last = self._last.get(symbol)
        if last is not None:
            gap = (at - last[0]).total_seconds()
            if 0 < gap <= self.max_gap:
                bucket = self.seconds.setdefault(symbol, {})
                bucket[last[1].value] = bucket.get(last[1].value, 0.0) + gap
        self._last[symbol] = (at, regime)

    def long_share(self):
        total = sum(sum(v.values()) for v in self.seconds.values())
        long = sum(v.get(Regime.LONG.value, 0.0) for v in self.seconds.values())
        return long / total if total else None


def layer_funnel(engine, at=None):
    """Section 15 funnel: LONG share -> candidates -> CONFIRM -> intents -> filled -> net profitable."""
    at = at or engine._last_at
    outcomes = engine.intents.outcomes(engine.book, engine.commissions,
                                       valuation=lambda symbol: engine.valuation_quote(symbol, at) if at else None,
                                       at=at)
    filled = [o for o in outcomes if o['bought']]
    profitable = [o for o in outcomes if o['status'] == 'CLOSED' and o['net_amount'] is not None
                  and Decimal(o['net_amount']) > 0]
    return {
        'environment_long_time_share': engine.regime_time.long_share(),
        'candidates': engine.funnel.get('candidates', 0),
        'economics_rejected_candidates': engine.funnel.get('economics_rejected', 0),
        'risk_rejected_candidates': engine.funnel.get('risk_rejected', 0),
        'confirmed': engine.funnel.get('confirmed', 0),
        'intents': engine.funnel.get('intents', 0),
        'intents_with_fills': len(filled),
        'net_profitable_intents': len(profitable),
        'rejection_reasons': dict(engine.rejections),
        'rejection_seconds': dict(engine.rejection_seconds),
    }, outcomes
