"""Coordinator regressions with real frozen config, ledger and portfolio gates.

Prepared feature snapshots isolate coordinator behavior from window computation,
which has its own tests. Positive forecasts here are synthetic test fixtures.
"""
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from copy import deepcopy

from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.domain import FeatureSnapshot, MarketRegime, OrderState, Quote, Side
from ibkr_microalpha.economics import Prediction
from ibkr_microalpha.engine import Forecast
from ibkr_microalpha.market import TradingDay

T = datetime(2026, 10, 2, 1, tzinfo=timezone.utc)  # 10:00 JST, after warmup.
SYMBOL = "STOCK_SYNTHETIC"
CONFIG = Path(__file__).resolve().parents[1] / "examples" / "research.json"


def fixture_document():
    """Coordinator fixture: research forecasts and externally injected market
    snapshots isolate the decision logic from the calibration/market pipelines."""
    document = deepcopy(load_config(CONFIG))
    document["engine"].update(economics_source="forecast", market_source="external")
    return document


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = build_engine(fixture_document())
        self.engine.calendar.records.append(TradingDay(
            T.date(), True, T - timedelta(days=1), "synthetic-test-calendar"))
        self.assertTrue(self.engine.book.reconcile({}, [], [], T, complete=True,
                                                    ownership_confirmed=True))
        self.forecast(0)

    def forecast(self, second, *, quantity=100, lower="1500", calibrated=True):
        at = T + timedelta(seconds=second)
        c = self.engine.config
        prediction = Prediction(c.policy_id, c.model_version, quantity, 100, D("2000"),
                                D(lower), True, calibrated, c.holding_seconds)
        self.engine.set_forecast(SYMBOL, Forecast(
            prediction, at, T - timedelta(days=1), at + timedelta(seconds=120),
            D("3001"), D("3008")), at)

    def prepared_snapshot(self, second, **overrides):
        values = dict(r_300=10, rs_300=8, rs_30=4, rs_60=5, rvol_30=3,
                      vwap_proxy_slope_60=2, vwap_proxy_slope_120=3,
                      vwap_proxy_deviation_bps=10, spread_bps=3.33,
                      volatility_bps=8, obi=.8)
        values.update(overrides)
        return FeatureSnapshot(SYMBOL, T + timedelta(seconds=second), values,
                               True, self.engine.config.score_version)

    def tick(self, second, *, bid="3000", ask="3001", rv_mkt=1,
             quote_valid=True, **features):
        at = T + timedelta(seconds=second)
        q = Quote(SYMBOL, at, D(bid), D(ask), 900 + second, 100,
                  f"quote-{second}", market_data_type=1 if quote_valid else 3,
                  bid_at=at, ask_at=at)
        self.engine.quotes[SYMBOL] = q
        market = FeatureSnapshot("BENCH_SYNTHETIC", at,
                                 dict(rv_mkt=rv_mkt, spread_bps=2, breadth=.8), True)
        self.engine.set_market(market, at)
        self.engine.evaluate(self.prepared_snapshot(second, **features), at)
        return q

    def working_entry(self):
        for second in (0, 5, 10, 11, 12):
            self.tick(second)
        buys = [o for o in self.engine.book.orders.values() if o.side == Side.BUY]
        self.assertEqual(len(buys), 1)
        buy = buys[0]
        self.engine.book.drain_commands(T + timedelta(seconds=12))
        self.engine.book.status(buy.order_id, "Submitted", 0, buy.quantity,
                                T + timedelta(seconds=12))
        return buy

    def fill_entry(self, quantity=100):
        buy = self.working_entry()
        self.engine.on_fill(exec_id="buy-exec", order_id=buy.order_id, qty=quantity,
                            price=D("3001"), at=T + timedelta(seconds=13))
        return buy

    def sells(self):
        return [o for o in self.engine.book.orders.values() if o.side == Side.SELL]

    def test_real_layers_require_persistence_and_one_intent(self):
        buy = self.working_entry()
        self.assertEqual(buy.quantity, 100)
        # Capped marketable limit: ask 3001 plus one frozen tick, below the 3008 cap.
        self.assertEqual(buy.limit_price, D("3002"))
        self.assertEqual(buy.candidate_expires_at, T + timedelta(seconds=14))
        self.assertEqual(self.engine.funnel["candidates"], 1)
        self.assertEqual(self.engine.funnel["intents"], 1)
        self.tick(13)
        self.assertEqual(len(self.engine.book.orders), 1)

    def test_missing_calibration_counts_candidate_then_rejects_it_without_order(self):
        self.engine.forecasts.clear()
        for second in (0, 5, 10, 11, 12):
            self.tick(second)
        # One candidate stream: the alpha candidate is counted, the economics
        # gate rejects it, and the rejection invalidates it (section 7).
        self.assertEqual(self.engine.funnel["candidates"], 1)
        self.assertEqual(self.engine.funnel["economics_rejected"], 1)
        self.assertEqual(self.engine.funnel["intents"], 0)
        self.assertEqual(self.engine.book.orders, {})
        self.assertEqual(self.engine.candidates, {})
        self.assertIsNone(self.engine.alpha.current(SYMBOL))
        self.assertGreater(self.engine.rejections["forecast_missing_or_stale"], 0)

    def test_existing_buy_is_cancelled_when_alpha_fails_while_environment_long(self):
        buy = self.working_entry()
        self.tick(13, rs_30=-10, rs_60=-10, rvol_30=1, vwap_proxy_slope_60=-3)
        self.assertEqual(buy.state, OrderState.CANCEL_PENDING)
        self.assertEqual(len(self.engine.book.orders), 1)

    def test_existing_buy_rechecks_expired_forecast(self):
        buy = self.working_entry()
        self.engine.forecasts[SYMBOL] = replace(self.engine.forecasts[SYMBOL],
                                                valid_until=T + timedelta(seconds=13))
        self.tick(13)
        self.assertEqual(buy.state, OrderState.CANCEL_PENDING)
        self.assertEqual(len(self.engine.book.orders), 1)

    def test_absolute_score_exit_requires_frozen_persistence(self):
        self.fill_entry()
        for second in (14, 15, 16):
            self.tick(second, rs_30=-10, rs_60=-10, rvol_30=1, vwap_proxy_slope_60=-3)
            self.assertEqual(self.sells(), [])
        self.tick(17, rs_30=-10, rs_60=-10, rvol_30=1, vwap_proxy_slope_60=-3)
        self.assertEqual(len(self.sells()), 1)
        self.assertEqual(self.engine.exit_reasons[SYMBOL], "ALPHA_EXIT")
        self.assertFalse(self.sells()[0].emergency)

    def test_T03_partial_fill_has_stop_and_original_timer_when_candidate_expires(self):
        buy = self.fill_entry(40)
        p = self.engine.book.positions[SYMBOL]
        first_time, stop = p.entry_time, p.stop_price
        self.assertEqual(first_time, T + timedelta(seconds=13))
        self.assertEqual(stop, D("2996"))
        self.tick(30)
        self.assertEqual(buy.state, OrderState.CANCEL_PENDING)
        self.assertEqual(buy.possible_remaining, 60)
        self.assertEqual(self.engine.book.positions[SYMBOL].quantity, 40)
        self.assertEqual(self.engine.book.positions[SYMBOL].entry_time, first_time)
        self.assertEqual(self.engine.book.positions[SYMBOL].stop_price, stop)
        self.assertEqual(len([o for o in self.engine.book.orders.values() if o.side == Side.BUY]), 1)

    def test_T09_stop_beats_strong_entry_and_exits_only_confirmed_shares(self):
        buy = self.fill_entry(40)
        self.tick(14, bid="2996", ask="2997")
        self.assertEqual(buy.state, OrderState.CANCEL_PENDING)
        self.assertEqual(self.engine.exit_reasons[SYMBOL], "STOP_LOSS")
        self.assertEqual(len(self.sells()), 1)
        self.assertEqual(self.sells()[0].quantity, 40)
        self.assertTrue(self.sells()[0].emergency)
        self.tick(15, bid="2996", ask="2997")
        self.assertEqual(len(self.sells()), 1)
        self.assertEqual(self.engine.funnel["intents"], 1)

    def test_holding_invalid_quote_is_soft_then_escalates_once(self):
        self.fill_entry()
        self.tick(14, quote_valid=False)
        # A transient valuation gap blocks entries but is not a day-ending lock.
        self.assertFalse(self.engine.risk.locked)
        self.assertIn("unvalued_daily_pnl", self.engine.risk.soft_blocks)
        self.assertTrue(self.engine.risk.entries_blocked)
        self.assertEqual(self.sells(), [])
        self.tick(15)
        self.assertNotIn("unvalued_daily_pnl", self.engine.risk.soft_blocks)
        self.assertEqual(self.sells(), [])
        # Persisting beyond the frozen limit while exposed escalates to HARD and exits once.
        for second in range(16, 16 + int(self.engine.config.max_soft_block_seconds) + 3):
            self.tick(second, quote_valid=False)
        self.assertTrue(any(r.startswith("escalated:") for r in self.engine.risk.lock_reasons))
        self.tick(60)
        self.tick(61)
        self.assertEqual(len(self.sells()), 1)
        self.assertEqual(self.sells()[0].quantity, 100)

    def test_T19_market_risk_off_cancels_long_candidate_and_working_order(self):
        buy = self.working_entry()
        self.tick(13, rv_mkt=3)
        self.assertEqual(self.engine.market_state, MarketRegime.MARKET_RISK_OFF)
        self.assertEqual(buy.state, OrderState.CANCEL_PENDING)
        self.assertIsNone(self.engine.alpha.current(SYMBOL))
        self.assertEqual(self.engine.funnel["intents"], 1)

    def test_policy_meeting_day_blocks_all_positive_signals(self):
        self.engine.calendar.records.append(TradingDay(
            T.date(), True, T, "synthetic-policy-calendar", policy_meeting=True))
        for second in (0, 5, 10, 11, 12):
            self.tick(second)
        self.assertEqual(self.engine.book.orders, {})
        self.assertGreater(self.engine.rejections["POLICY_MEETING_DAY"], 0)

    def test_T10_day_loss_and_kill_lock_cannot_be_overridden_by_new_signal(self):
        self.engine.risk.observe_daily_pnl(-self.engine.risk.config.capital
                                          * self.engine.risk.config.daily_loss_fraction, T)
        for second in (0, 5, 10, 11, 12):
            self.tick(second)
        self.assertEqual(self.engine.book.orders, {})
        self.assertIn("daily_loss", self.engine.risk.lock_reasons)
        self.engine.kill_switch(T + timedelta(seconds=13))
        self.tick(14)
        self.assertIn("KILL_SWITCH", self.engine.risk.lock_reasons)
        self.assertEqual(self.engine.book.orders, {})

    def test_first_fill_missing_score_never_drops_authoritative_execution(self):
        buy = self.working_entry()
        self.engine.snapshots[SYMBOL] = FeatureSnapshot(
            SYMBOL, T + timedelta(seconds=12), {}, True, self.engine.config.score_version)
        self.engine.on_fill(exec_id="actual-missing-score", order_id=buy.order_id, qty=40,
                            price=D("3001"), at=T + timedelta(seconds=13))
        self.assertEqual(buy.filled_quantity, 40)
        self.assertEqual(self.engine.book.positions[SYMBOL].quantity, 40)
        self.assertIn("actual-missing-score", self.engine.book.executions)
        self.assertIn("first_fill_score_invalid", self.engine.risk.lock_reasons)
        self.assertEqual(self.sells()[0].quantity, 40)

    def test_actual_commission_replaces_estimate_without_double_daily_loss(self):
        self.fill_entry()
        observed = []
        original = self.engine.risk.observe_daily_pnl
        def observe(value, at):
            observed.append(value)
            original(value, at)
        self.engine.risk.observe_daily_pnl = observe
        at = T + timedelta(seconds=13)
        self.engine._sync_risk(at)
        before_pnl, before_cash = observed[-1], self.engine.risk.cash
        actual_fee = self.engine.commissions.commission(D("300100"))
        self.engine.book.commission("buy-exec", actual_fee)
        self.engine._sync_risk(at)
        self.assertEqual(observed[-1], before_pnl)
        self.assertEqual(self.engine.risk.cash, before_cash)
        self.assertEqual(before_pnl, D("-580.08"))
        self.assertEqual(self.engine.book.positions[SYMBOL].fees, actual_fee)

    def test_high_entry_fee_does_not_hide_missing_exit_fee(self):
        self.fill_entry()
        self.engine.book.commission("buy-exec", D("500"))
        self.tick(14)
        self.engine._request_exit(SYMBOL, T + timedelta(seconds=14), "SIGNAL_EXIT", False)
        sell = self.sells()[0]
        self.engine.book.drain_commands(T + timedelta(seconds=14))
        self.engine.book.status(sell.order_id, "Submitted", 0, 100, T + timedelta(seconds=14))
        self.engine.on_fill(exec_id="exit-fee-pending", order_id=sell.order_id,
                            qty=100, price=D("3000"), at=T + timedelta(seconds=15))
        self.assertEqual(self.engine._fee_reserves(), D("240"))
        self.engine.book.commission("exit-fee-pending", D("240"))
        self.assertEqual(self.engine._fee_reserves(), D("0"))

    def test_normal_exit_waits_for_pending_buy_cancel_before_ttl(self):
        buy = self.fill_entry(40)
        self.tick(14)
        self.engine._request_exit(SYMBOL, T + timedelta(seconds=14), "SIGNAL_EXIT", False)
        self.assertEqual(buy.state, OrderState.CANCEL_PENDING)
        self.assertEqual(self.sells(), [])
        self.tick(15)
        self.assertEqual(self.sells(), [])

    def test_ordinary_flatten_starts_frozen_cooldown(self):
        self.fill_entry()
        self.tick(14)
        self.engine._request_exit(SYMBOL, T + timedelta(seconds=14), "SIGNAL_EXIT", False)
        sell = self.sells()[0]
        self.engine.book.drain_commands(T + timedelta(seconds=14))
        self.engine.book.status(sell.order_id, "Submitted", 0, 100, T + timedelta(seconds=14))
        self.engine.on_fill(exec_id="normal-exit-exec", order_id=sell.order_id,
                            qty=100, price=D("3000"), at=T + timedelta(seconds=15))
        self.assertEqual(self.engine.book.positions[SYMBOL].quantity, 0)
        self.assertEqual(self.engine.cooldown_until[SYMBOL], T + timedelta(seconds=45))
        self.tick(16)
        self.assertEqual(self.engine.funnel["intents"], 1)
        self.assertGreater(self.engine.rejections["cooldown"], 0)

    def test_stop_exit_uses_longer_stop_cooldown(self):
        self.fill_entry()
        self.tick(14, bid="2996", ask="2997")
        sell = self.sells()[0]
        self.engine.book.drain_commands(T + timedelta(seconds=14))
        self.engine.book.status(sell.order_id, "Submitted", 0, 100, T + timedelta(seconds=14))
        self.engine.on_fill(exec_id="stop-exit-exec", order_id=sell.order_id,
                            qty=100, price=D("2996"), at=T + timedelta(seconds=15))
        self.assertEqual(self.engine.cooldown_until[SYMBOL], T + timedelta(seconds=315))


if __name__ == "__main__":
    unittest.main()
