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
        pnl = ZERO
        valued = True
        for symbol, position in e.book.positions.items():
            pnl += position.realized_pnl - position.fees
            if not position.quantity:
                e.risk.unblock(f'position_quote_invalid:{symbol}')
                continue
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
                distance = order.stop_distance if order.stop_distance is not None else order.limit_price
                stress[order.symbol] = stress.get(order.symbol, ZERO) + (
                    order.possible_remaining * (distance + self._reserve_per_share(order.symbol, order.limit_price, at))
                    + entry_fee + e.commissions.commission(amount))
        missing_fees = self.fee_reserves()
        e.unreported_fee_reserve = missing_fees
        e.daily_net_pnl_estimate = pnl - missing_fees if valued else None
        cash = e.config.initial_cash + e.book.cash_flow - pending_cash - missing_fees
        snapshot = e.account_snapshot
        fresh = (snapshot is not None and
                 0 <= (at - snapshot.received_at).total_seconds() <= e.risk.config.account_snapshot_max_age_seconds)
        if fresh:
            # Broker available funds already net open-order commitments; the
            # strategy may never spend more than either source allows.
            cash = min(cash, snapshot.available_funds)
            self.cash_source = 'reconciled'
            e.risk.unblock('account_snapshot_missing')
        else:
            self.cash_source = 'modeled'
            if e.risk.config.require_account_snapshot:
                e.risk.block('account_snapshot_missing', at)
        e.risk.verify_account(cash, exposure, sectors, e.book.reconciled and not e.book.locked, stress=stress)
        e.risk.observe_daily_pnl(e.daily_net_pnl_estimate, at)
