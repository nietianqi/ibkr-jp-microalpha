"""Funnel, per-intent outcomes, execution quality, repeated stops and residual alarms."""
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from ibkr_microalpha.config import build_engine
from ibkr_microalpha.domain import Quote, Side
from ibkr_microalpha.market import TradingDay
from ibkr_microalpha.reporting import ExecutionQuality, layer_funnel
from tests import test_engine as base
from tests.test_engine import SYMBOL, T


class ReportingTests(unittest.TestCase):
    forecast = base.EngineIntegrationTests.forecast
    prepared_snapshot = base.EngineIntegrationTests.prepared_snapshot
    tick = base.EngineIntegrationTests.tick
    working_entry = base.EngineIntegrationTests.working_entry
    fill_entry = base.EngineIntegrationTests.fill_entry
    sells = base.EngineIntegrationTests.sells

    def setUp(self):
        self.engine = build_engine(base.fixture_document())
        self.engine.calendar.records.append(TradingDay(
            T.date(), True, T - timedelta(days=1), "synthetic-test-calendar"))
        self.assertTrue(self.engine.book.reconcile({}, [], [], T, complete=True,
                                                    ownership_confirmed=True))
        self.forecast(0)

    def flatten(self, second, price, *, bid=None, ask=None, exec_id="exit-exec"):
        if bid is not None:
            self.tick(second, bid=bid, ask=ask)
        else:
            self.tick(second)
            self.engine._request_exit(SYMBOL, T + timedelta(seconds=second), "SIGNAL_EXIT", False)
        sell = self.sells()[-1]
        at = T + timedelta(seconds=second)
        self.engine.book.drain_commands(at)
        self.engine.book.status(sell.order_id, "Submitted", 0, sell.quantity, at)
        self.engine.on_fill(exec_id=exec_id, order_id=sell.order_id, qty=sell.quantity,
                            price=D(price), at=at + timedelta(seconds=1))
        return sell

    def test_layer_funnel_and_closed_intent_net_include_all_child_fees(self):
        self.fill_entry()
        self.flatten(14, "3000")
        funnel, intents = layer_funnel(self.engine)
        self.assertEqual(funnel["intents"], 1)
        self.assertEqual(funnel["intents_with_fills"], 1)
        self.assertEqual(funnel["net_profitable_intents"], 0)
        self.assertIsNotNone(funnel["environment_long_time_share"])
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["status"], "CLOSED")
        self.assertEqual(intents[0]["valuation"], "realized")
        self.assertEqual(intents[0]["child_orders"], 2)
        # 300000 - 300100 - 240.08 (buy fee reserve) - 240 (sell fee reserve)
        self.assertEqual(D(intents[0]["net_amount"]), D("-580.08"))

    def test_open_intent_uses_conservative_residual_value_not_realized(self):
        self.fill_entry(40)
        _, intents = layer_funnel(self.engine)
        self.assertEqual(intents[0]["status"], "OPEN")
        self.assertEqual(intents[0]["valuation"], "conservative_residual")
        # 40 x bid 3000 - 120040 cost - 96.032 entry fee reserve - 96 exit fee.
        self.assertEqual(D(intents[0]["net_amount"]), D("-232.032"))

    def test_unfilled_intent_is_retained_with_zero_price_result(self):
        buy = self.working_entry()
        at = T + timedelta(seconds=13)
        self.engine.book.status(buy.order_id, "Cancelled", 0, 0, at)
        _, intents = layer_funnel(self.engine)
        self.assertEqual(intents[0]["status"], "UNFILLED")
        self.assertEqual(D(intents[0]["net_amount"]), 0)

    def test_arrival_cost_and_post_fill_drift_are_side_signed(self):
        self.fill_entry()
        self.tick(18, bid="3002", ask="3003")
        quality = self.engine.execution_quality.summary(self.engine.book)
        fill = quality["fills"][0]
        self.assertEqual(D(fill["arrival_mid"]), D("3000.5"))
        self.assertEqual(D(fill["price_cost"]), D("50.0"))
        drift = quality["post_fill_drift"]["BUY"]["5s"]
        self.assertEqual(drift["samples"], 1)
        self.assertGreater(drift["mean_signed_bps"], 0)
        self.assertEqual(quality["post_fill_drift"]["BUY"]["30s"]["samples"], 0)

    def test_drift_is_unavailable_rather_than_forward_filled_after_quote_gap(self):
        self.fill_entry()
        at = T + timedelta(seconds=30)
        self.engine.poll(at)  # No fresh quote: the 5 second horizon cannot be measured.
        drift = self.engine.execution_quality.summary(self.engine.book)["post_fill_drift"]["BUY"]["5s"]
        self.assertEqual(drift["samples"], 0)
        self.assertEqual(drift["unavailable"], 1)

    def test_RPT01_horizon_waits_for_first_quote_at_or_after_target(self):
        class Order:
            order_id, symbol, side, emergency = 1, "S", Side.BUY, False

        def quote(at, mid):
            return Quote("S", at, D(mid) - D("0.5"), D(mid) + D("0.5"), 100, 100, f"q{at}",
                         bid_at=at, ask_at=at)
        quality = ExecutionQuality(horizons=(5,))
        quality.on_submit(Order, quote(T, "100"), T)
        quality.on_fill("f1", Order, T)
        quality.observe(T, lambda s: quote(T, "100"), 2)
        target = T + timedelta(seconds=5)
        quality.observe(target, lambda s: quote(T + timedelta(seconds=4), "100"), 2)  # stale first
        quality.observe(target, lambda s: quote(target, "101"), 2)                    # then the real one
        result = quality.drifts()["f1"].results[5]
        self.assertAlmostEqual(result, 10000 * (__import__("math").log(101 / 100)))

    def test_RPT02_correction_replaces_cost_and_bust_removes_sample(self):
        buy = self.fill_entry()
        self.engine.on_fill(exec_id="buy-corrected", order_id=buy.order_id, qty=100, price=D("3002"),
                            at=T + timedelta(seconds=14), correction_of="buy-exec")
        quality = self.engine.execution_quality.summary(self.engine.book)
        self.assertEqual(len(quality["fills"]), 1)
        self.assertEqual(D(quality["total_price_cost_vs_arrival"]), D("150.0"))  # (3002-3000.5) x 100
        self.assertTrue(quality["fills"][0]["corrected"])
        self.engine.on_fill(exec_id="buy-bust", order_id=buy.order_id, qty=0, price=D("3002"),
                            at=T + timedelta(seconds=15), correction_of="buy-corrected")
        quality = self.engine.execution_quality.summary(self.engine.book)
        self.assertEqual(quality["fills"], [])
        self.assertEqual(D(quality["total_price_cost_vs_arrival"]), 0)
        self.assertEqual(self.engine.execution_quality.drifts(), {})

    def test_repeated_stop_extends_cooldown_then_disables_symbol(self):
        self.engine.consecutive_stops[SYMBOL] = 1  # A prior stop earlier the same day.
        self.fill_entry()
        self.flatten(14, "2996", bid="2996", ask="2997")
        self.assertEqual(self.engine.cooldown_until[SYMBOL], T + timedelta(seconds=615))
        self.assertEqual(self.engine.disabled_symbols[SYMBOL], "consecutive_stop_losses")
        self.engine.cooldown_until.pop(SYMBOL)
        self.forecast(700)
        for second in (700, 705, 710, 711, 712):
            self.tick(second)
        self.assertEqual(self.engine.funnel["intents"], 1)
        self.assertGreater(self.engine.rejections["symbol_disabled_after_stops"], 0)

    def test_filled_ordinary_exit_resets_stop_count(self):
        self.engine.consecutive_stops[SYMBOL] = 1
        self.fill_entry()
        self.flatten(14, "3005")
        self.assertNotIn(SYMBOL, self.engine.consecutive_stops)
        self.assertNotIn(SYMBOL, self.engine.disabled_symbols)

    def test_stress_budget_breach_is_reported_and_audited(self):
        self.fill_entry()
        self.flatten(14, "2900", bid="2990", ask="2991")  # A gap far beyond the stop budget.
        _, intents = layer_funnel(self.engine)
        self.assertTrue(intents[0]["stress_exceeded"])
        self.assertLess(D(intents[0]["net_amount"]), -D(intents[0]["stress_budget"]))
        self.assertIn("STRESS_BUDGET_EXCEEDED", [record["kind"] for record in self.engine.audit])

    def test_volatility_stop_distance_is_converted_to_jpy_per_share(self):
        self.engine.config = replace(self.engine.config, stop_volatility_multiple=D("10"))
        self.fill_entry()
        buy = next(iter(self.engine.book.orders.values()))
        # 10 x 8 bps x 3001 JPY = 24.008 JPY/share; stop is then rounded to a legal tick.
        self.assertEqual(buy.stop_distance, D("24.008"))
        self.assertEqual(self.engine.book.positions[SYMBOL].stop_price, D("2977"))

    def test_stop_uses_ticks_bps_and_volatility_not_fixed_jpy(self):
        quote = self.tick(0)
        instrument = self.engine.instruments[SYMBOL]
        snapshot = self.prepared_snapshot(0)
        # At 3001 JPY (1 JPY tick): max(5 ticks = 5, 15 bps = 4.5015) = 5 JPY.
        self.assertEqual(self.engine.entries.stop_distance(snapshot, quote, T, instrument), D("5"))
        cheap = replace(quote, bid=D("960.9"), ask=D("961"))
        # At 961 JPY (0.1 JPY tick): max(0.5, 15 bps = 1.4415) -> the bps term governs.
        self.assertEqual(self.engine.entries.stop_distance(snapshot, cheap, T, instrument), D("1.4415"))

    def test_missing_volatility_blocks_volatility_stop(self):
        self.engine.config = replace(self.engine.config, stop_volatility_multiple=D("10"))
        quote = self.tick(0)
        snapshot = self.prepared_snapshot(0)
        missing = replace(snapshot, values={k: v for k, v in snapshot.values.items()
                                            if k != "volatility_bps"})
        instrument = self.engine.instruments[SYMBOL]
        self.assertIsNone(self.engine.entries.stop_distance(missing, quote, T, instrument))
        self.assertEqual(self.engine.entries.stop_distance(snapshot, quote, T, instrument), D("24.008"))

    def test_residual_position_after_exit_deadline_alarms_and_locks(self):
        self.fill_entry()
        deadline = 85 * 60  # 11:25 JST from the 10:00 JST test origin.
        self.forecast(deadline)
        self.tick(deadline)
        self.assertEqual(len(self.engine.alarms), 1)
        self.assertEqual(self.engine.alarms[0]["position"], 100)
        self.assertIn("session_residual_risk", self.engine.risk.lock_reasons)
        self.tick(deadline + 1)
        self.assertEqual(len(self.engine.alarms), 1)


if __name__ == "__main__":
    unittest.main()
