"""Account valuation, fee reserves and stress budgets for the risk engine.

Valuation uses valuation-grade quotes (healthy stream, frozen valuation age);
an unvaluable position raises a SOFT block that the coordinator escalates only
while controlled risk exists (review STR-02).
"""
from decimal import Decimal

from .domain import Side

ZERO = Decimal(0)
BPS = Decimal(10000)


class RiskValuation:
    def __init__(self, engine):
        self.e = engine
        self.cash_source = 'modeled'
        self._account_id = None
        self._anchor = None
        self._baseline_debits = {}
        self._baseline_fees = {}
        self._post_anchor_debits = {}
        self._short_alarms = set()

    def accept_account(self, snapshot, at) -> bool:
        """Anchor only a complete, explicitly covered ledger/account barrier.

        Covered orders' principal is already reserved in broker funds. Their
        fills/cancels never manufacture free cash; only a new funds barrier can
        credit sale proceeds, corrections or released broker reservations.
        """
        e = self.e
        sequence = e.book.journal[-1]['sequence'] if e.book.journal else 0
        transmitted = {o.order_id for o in e.book.active_orders()
                       if o.side == Side.BUY and o.submitted_at is not None}
        valid = (e.book.reconciled and snapshot.ledger_sequence == sequence
                 and set(snapshot.covered_order_ids) == transmitted
                 and (self._account_id is None or snapshot.account_id == self._account_id))
        self._anchor = snapshot if valid else None
        self._baseline_debits = {}
        self._baseline_fees = {}
        self._post_anchor_debits = {}
        if not valid:
            e.risk.block('account_snapshot_unreconciled', at)
            return False
        self._account_id = snapshot.account_id
        self._baseline_debits = {o.order_id: (o.filled_notional if o.side == Side.BUY else ZERO)
                                + e.book.order_fees(o.order_id) for o in e.book.orders.values()}
        self._baseline_fees = {o.order_id:e.book.order_fees(o.order_id) for o in e.book.orders.values()}
        e.risk.unblock('account_snapshot_unreconciled')
        return True

    def fee_reserves(self) -> Decimal:
        """Unreported commissions: minimum per child order, actual fees replace estimates."""
        e = self.e
        expected = ZERO
        for order in e.book.orders.values():
            if order.filled_quantity:
                expected += max(ZERO, e.commissions.commission(order.filled_notional)
                                - e.book.order_fees(order.order_id))
        return expected

    def exit_fee_reserve(self, symbol, quantity, bid) -> Decimal:
        e = self.e
        reserved = 0
        fees = ZERO
        for order in e.book.active_orders(symbol):
            if order.side == Side.SELL:
                shares = min(quantity - reserved, order.possible_remaining)
                if shares > 0:
                    fees += max(ZERO,
                        e.commissions.commission(order.filled_notional + bid * shares)
                        - max(e.commissions.commission(order.filled_notional),
                              e.book.order_fees(order.order_id)))
                    reserved += shares
        fees += e.commissions.commission(bid * (quantity - reserved))
        return fees

    def _reserve_per_share(self, symbol, price, at) -> Decimal:
        """Gap reserve plus frozen exit slippage, JPY/share at ``price``."""
        e = self.e
        category = e.instruments[symbol].tick_category if symbol in e.instruments else 'TOPIX500'
        try:
            tick = e.ticks.tick_size(price, at, category)
        except ValueError:
            tick = ZERO
        return e.config.gap_reserve_bps * price / BPS + tick * e.config.exit_slippage_ticks

    def sync(self, at):
        e = self.e
        if (not e._ever_reconciled and not e.book.reconciled
                and not e.book.positions and not e.book.orders):
            e.risk.account_verified = False
            return  # Startup WARMUP waits for its first complete account snapshot.
        e._ever_reconciled |= e.book.reconciled
        exposure, stress = {}, {}
        sectors = {s: i.sector for s, i in e.instruments.items()}
        pending_cash = ZERO
        uncovered_pending = ZERO
        pnl = ZERO
        valued = True
        account_consistent = True
        for symbol, position in e.book.positions.items():
            pnl += position.realized_pnl - position.fees
            if not position.quantity:
                e.risk.unblock(f'position_quote_invalid:{symbol}')
                continue
            if position.quantity < 0:
                valued = account_consistent = False
                e.risk.lock('uncontrolled_short')
                if symbol not in self._short_alarms:
                    self._short_alarms.add(symbol)
                    alarm = {'at':at.isoformat(), 'symbol':symbol, 'alarm':'UNCONTROLLED_SHORT',
                             'quantity':position.quantity}
                    e.alarms.append(alarm)
                    e._record(at, 'ALARM', symbol=symbol, alarm='UNCONTROLLED_SHORT', quantity=position.quantity)
                    e.book._query(at)
                # Account risk sees absolute exposure, but long-only exit code
                # never sells an abnormal short. Its PnL/stress is unknown.
                reference = e.quotes[symbol].ask if symbol in e.quotes else position.average_price
                exposure[symbol] = abs(position.quantity) * max(reference, position.average_price)
                stress[symbol] = exposure[symbol]
                continue
            self._short_alarms.discard(symbol)
            quote = e.valuation_quote(symbol, at)
            last_ask = e.quotes[symbol].ask if symbol in e.quotes else position.average_price
            exposure[symbol] = position.quantity * max(position.average_price, last_ask)
            if quote is None or symbol not in sectors:
                valued = False
                e.risk.block(f'position_quote_invalid:{symbol}', at)
                reference_bid = position.average_price
                exit_fees = e.commissions.commission(position.quantity * reference_bid)
            else:
                e.risk.unblock(f'position_quote_invalid:{symbol}')
                exit_fees = self.exit_fee_reserve(symbol, position.quantity, quote.bid)
                pnl += position.quantity * (quote.bid - position.average_price) - exit_fees
                reference_bid = quote.bid
            stop_loss = (max(ZERO, reference_bid - position.stop_price) if position.stop_price is not None
                         else reference_bid)
            stress[symbol] = (position.quantity * (stop_loss + self._reserve_per_share(symbol, reference_bid, at))
                              + exit_fees)
        for order in e.book.active_orders():
            if order.side == Side.BUY:
                amount = order.limit_price * order.possible_remaining
                exposure[order.symbol] = exposure.get(order.symbol, ZERO) + amount
                entry_fee = max(ZERO,
                    e.commissions.commission(order.filled_notional + amount)
                    - max(e.commissions.commission(order.filled_notional),
                          e.book.order_fees(order.order_id)))
                pending_cash += amount + entry_fee
                if self._anchor is None or order.order_id not in self._anchor.covered_order_ids:
                    uncovered_pending += amount + entry_fee
                distance = order.stop_distance if order.stop_distance is not None else order.limit_price
                stress[order.symbol] = stress.get(order.symbol, ZERO) + (
                    order.possible_remaining * (distance + self._reserve_per_share(order.symbol, order.limit_price, at))
                    + entry_fee + e.commissions.commission(amount))
        missing_fees = self.fee_reserves()
        e.unreported_fee_reserve = missing_fees
        e.daily_net_pnl_estimate = pnl - missing_fees if valued else None
        modeled_gross = e.config.initial_cash + e.book.cash_flow - missing_fees
        cash = modeled_gross - pending_cash
        gross = modeled_gross
        snapshot = e.account_snapshot
        fresh = (snapshot is not None and
                 0 <= (at - snapshot.received_at).total_seconds() <= e.risk.config.account_snapshot_max_age_seconds)
        if fresh:
            e.risk.unblock('account_snapshot_missing')
            if self._anchor is snapshot:
                for order in e.book.orders.values():
                    # No speculative credits from SELL proceeds or a BUY bust.
                    # Covered BUY principal was already deducted by the broker.
                    principal = (order.filled_notional if order.side == Side.BUY
                                 and order.order_id not in snapshot.covered_order_ids else ZERO)
                    baseline = self._baseline_debits.get(order.order_id, ZERO)
                    if order.side == Side.BUY and order.order_id in snapshot.covered_order_ids:
                        baseline = self._baseline_fees.get(order.order_id, ZERO)
                    debit = max(ZERO, principal + e.book.order_fees(order.order_id) - baseline)
                    self._post_anchor_debits[order.order_id] = max(
                        self._post_anchor_debits.get(order.order_id, ZERO), debit)
                broker_gross = snapshot.available_funds - sum(self._post_anchor_debits.values(), ZERO) - missing_fees
                gross = min(gross, broker_gross)
                cash = min(cash, broker_gross - uncovered_pending)
                self.cash_source = 'reconciled'
            else:
                cash = ZERO
                self.cash_source = 'unverified'
                e.risk.block('account_snapshot_unreconciled', at)
        else:
            self.cash_source = 'modeled'
            if e.risk.config.require_account_snapshot or snapshot is not None:
                e.risk.block('account_snapshot_missing', at)
        # A local unsent promise exceeding a nonnegative funds budget is an
        # entry block, not a fictional negative broker cash balance.
        if cash < 0 <= gross:
            e.risk.block('entry_cash_overcommitted', at)
            cash = ZERO
        else:
            e.risk.unblock('entry_cash_overcommitted')
        e.risk.verify_account(cash, exposure, sectors,
                             account_consistent and e.book.reconciled and not e.book.locked, stress=stress)
        if fresh and self._anchor is not snapshot:
            e.risk.account_verified = False
        e.risk.observe_daily_pnl(e.daily_net_pnl_estimate, at)
