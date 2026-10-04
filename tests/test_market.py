import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from ibkr_microalpha.domain import Quote
from ibkr_microalpha.features import (FeatureConfig, FeatureEngine, Trade, VolumeBaseline,
                                      VolatilityBaseline)
from ibkr_microalpha.market import (JST, CalendarAnnouncement, JapanCalendar, JapanSession,
                                    QuoteQuality, ScheduledWindow, TickTable, TradingDay,
                                    validate_quote)


DAY = date(2026, 10, 2)


def at(hour=9, minute=0, second=0):
    return datetime(2026, 10, 2, hour, minute, second, tzinfo=JST)


def quote(stamp, symbol="7203", price="3001", event_id=None, **kwargs):
    bid = Decimal(price)
    return Quote(symbol, stamp, bid, bid + 1, 200, 100,
                 event_id or f"{symbol}:{stamp.isoformat()}", bid_at=stamp, ask_at=stamp, **kwargs)


class MarketRulesTests(unittest.TestCase):
    def setUp(self):
        self.ticks = TickTable()
        self.calendar = JapanCalendar([TradingDay(DAY, True, at(8), "verified-calendar")])

    def test_t27_topix_tick_tiers_recomputed_for_target(self):
        for price in ("3001", "3009", "3000", "2999.5"):
            self.assertTrue(self.ticks.is_legal(Decimal(price), at()))
        self.assertFalse(self.ticks.is_legal(Decimal("3000.5"), at()))
        self.assertEqual(self.ticks.round_price(Decimal("3000.5"), "up", at()), Decimal("3001"))
        self.assertEqual(self.ticks.round_price(Decimal("3000.5"), "down", at()), Decimal("3000"))
        self.assertEqual(self.ticks.move_ticks(Decimal("3000"), 1, at()), Decimal("3001"))
        self.assertEqual(self.ticks.move_ticks(Decimal("3001"), -2, at()), Decimal("2999.5"))
        self.assertFalse(self.ticks.is_legal(Decimal("3001"), at(), "OTHER"))
        self.assertEqual(self.ticks.round_price(Decimal("3001"), "up", at(), "OTHER"), Decimal("3005"))

    def test_effective_date_unknown_category_fail_closed(self):
        self.assertFalse(self.ticks.is_legal(Decimal("3001"), date(2027, 3, 1)))
        self.assertFalse(self.ticks.is_legal(Decimal("3001"), date(2023, 6, 4)))
        self.assertFalse(self.ticks.is_legal(Decimal("3001"), at(), "UNKNOWN"))
        with self.assertRaises(ValueError):
            self.ticks.tick_size(Decimal("3001"), date(2027, 3, 1))

    def test_t14_precise_session_boundaries_and_warmup(self):
        expected = [(at(11, 29, 59), JapanSession.MORNING), (at(11, 30), JapanSession.LUNCH),
                    (at(12, 30), JapanSession.AFTERNOON), (at(15, 25), JapanSession.CLOSING_AUCTION),
                    (at(15, 30), JapanSession.CLOSED)]
        for stamp, session in expected:
            self.assertEqual(self.calendar.session(stamp), session)
        self.assertFalse(self.calendar.entry_gate(at(9, 9, 59), max_hold_seconds=120))
        self.assertTrue(self.calendar.entry_gate(at(9, 10), max_hold_seconds=120))
        self.assertFalse(self.calendar.entry_gate(at(12, 39, 59), max_hold_seconds=120))
        self.assertTrue(self.calendar.entry_gate(at(12, 40), max_hold_seconds=120))
        self.assertFalse(self.calendar.entry_gate(at(13), max_hold_seconds=120, market_status="HALTED"))

    def test_hold_wait_latency_buffer_budget_and_hard_cutoff(self):
        # 11:04 + 20 seconds wait + 1 second tail + 20 minute hold +
        # 40 second exit buffer would exceed the 11:25 planned deadline.
        result = self.calendar.entry_gate(at(11, 4), max_hold_seconds=1200,
                                         remaining_wait_seconds=20, submit_latency_seconds=1,
                                         exit_buffer_seconds=40)
        self.assertEqual(result.reason, "INSUFFICIENT_HOLD_TIME")
        self.assertTrue(self.calendar.entry_gate(at(11, 3, 59), max_hold_seconds=1200,
                                                remaining_wait_seconds=20, submit_latency_seconds=1,
                                                exit_buffer_seconds=40))
        self.assertEqual(self.calendar.entry_gate(at(11, 20), max_hold_seconds=120).reason, "ENTRY_CUTOFF")
        self.assertFalse(self.calendar.entry_gate(at(10), max_hold_seconds=float("nan")))

    def test_t20_calendar_missing_and_known_policy_day(self):
        self.assertEqual(JapanCalendar().entry_gate(at(10), max_hold_seconds=120).reason, "CALENDAR_UNKNOWN")
        calendar = JapanCalendar([TradingDay(DAY, True, at(8), "known", policy_meeting=True)])
        self.assertEqual(calendar.entry_gate(at(10), max_hold_seconds=120).reason, "POLICY_MEETING_DAY")
        future = JapanCalendar([TradingDay(DAY, True, at(11), "late-calendar")])
        self.assertFalse(future.entry_gate(at(10), max_hold_seconds=120))

    def test_t26_hindsight_date_and_actual_publication_cannot_rewrite_prior_decisions(self):
        self.calendar.records.append(TradingDay(DAY, True, at(10, 30), "late-policy", policy_meeting=True))
        self.assertTrue(self.calendar.entry_gate(at(10), max_hold_seconds=120))
        self.assertFalse(self.calendar.entry_gate(at(10, 30), max_hold_seconds=120))
        calendar = JapanCalendar(self.calendar.records[:1], announcements=[
            CalendarAnnouncement("announcement", at(10, 5), 120, "feed", announced_at=at(10))])
        self.assertTrue(calendar.entry_gate(at(10, 4, 59), max_hold_seconds=120))
        self.assertFalse(calendar.entry_gate(at(10, 5), max_hold_seconds=120))
        self.assertTrue(calendar.entry_gate(at(10, 7), max_hold_seconds=120))
        calendar.scheduled_windows.append(ScheduledWindow("late-window", at(10), at(10, 10), at(10, 6), "feed"))
        self.assertTrue(calendar.entry_gate(at(10, 4), max_hold_seconds=120))
        self.assertFalse(calendar.entry_gate(at(10, 8), max_hold_seconds=120))

    def test_t07_delayed_frozen_invalid_fields_block_quote(self):
        q = quote(at(10))
        self.assertTrue(validate_quote(q, at(10), self.ticks))
        for data_type in (2, 3, 4):
            self.assertFalse(validate_quote(replace(q, market_data_type=data_type), at(10), self.ticks))
        self.assertFalse(validate_quote(replace(q, bid_size=0), at(10), self.ticks))
        self.assertFalse(validate_quote(replace(q, bid_at=None), at(10), self.ticks))
        self.assertFalse(validate_quote(q, at(10), self.ticks, stream_healthy=False))
        self.assertFalse(validate_quote(replace(q, market_status="SPECIAL_QUOTE"), at(10), self.ticks))

    def test_causal_field_age_and_naive_future_times(self):
        q = quote(at(10))
        self.assertFalse(validate_quote(q, at(9, 59, 59), self.ticks))
        self.assertEqual(validate_quote(replace(q, bid_at=at(9, 59, 57)), at(10), self.ticks).reason, "STALE_QUOTE")
        self.assertEqual(validate_quote(replace(q, bid_at=at(9, 59, 59)), at(10), self.ticks).reason, "UNSYNCHRONIZED_QUOTE")
        self.assertEqual(validate_quote(replace(q, ask_at=at(10, 0, 1)), at(10), self.ticks).reason, "FUTURE_FIELD")
        with self.assertRaises(ValueError):
            validate_quote(q, datetime(2026, 10, 2, 10), self.ticks)
        with self.assertRaises(ValueError):
            replace(q, bid_at=datetime(2026, 10, 2, 10))


class FeatureTests(unittest.TestCase):
    def engine(self, required=("r_5",), **kwargs):
        return FeatureEngine("1306", FeatureConfig(required_features=required, rvol_days=1,
                                                   rvol_min_days=1, **kwargs))

    def feed(self, engine, duration, *, start=None, trades=True, side="BUY", benchmark=True):
        start = start or at()
        for index in range(duration + 1):
            stamp = start + timedelta(seconds=index)
            q = quote(stamp, price=str(3001 + index))
            engine.on_quote(q)
            if benchmark:
                engine.on_quote(quote(stamp, "1306"))
            if trades:
                price = q.ask if side == "BUY" else q.bid if side == "SELL" else q.bid + Decimal(".5")
                # Midpoint trades must themselves be on a legal grid. For
                # UNKNOWN use a delayed event timestamp rather than an illegal
                # fractional trade price above 3,000 yen.
                exchange = None if side != "UNKNOWN" else stamp - timedelta(seconds=1)
                if side == "UNKNOWN":
                    price = q.ask
                engine.on_trade(Trade("7203", stamp, price, 100, f"trade-{index}", exchange_at=exchange))

    def test_all_return_windows_benchmark_rs_and_distinct_updates(self):
        engine = self.engine(required=("r_600", "rs_600"))
        self.feed(engine, 600, trades=False)
        snap = engine.snapshot("7203", at(9, 10))
        self.assertTrue(snap.valid, snap.reason)
        self.assertGreater(snap.values["r_600"], 0)
        self.assertEqual(snap.values["rs_600"], snap.values["r_600"])
        for window in (5, 10, 30, 60, 120, 300, 600):
            self.assertIn(f"r_{window}", snap.values)
        duplicate = replace(engine._series["7203"].quotes[-1], at=at(9, 10, 1))
        self.assertFalse(engine.on_quote(duplicate))
        self.assertEqual(len(engine._series["7203"].quotes), 601)
        self.assertFalse(engine.on_quote(quote(at(9, 9, 59), event_id="late")))

    def test_t07_gap_and_data_type_recovery_needs_fresh_window(self):
        engine = self.engine()
        self.feed(engine, 5, trades=False)
        self.assertTrue(engine.snapshot("7203", at(9, 0, 5)).valid)
        engine.on_quote(quote(at(9, 0, 6), market_data_type=3))
        self.assertFalse(engine.snapshot("7203", at(9, 0, 6)).valid)
        self.feed(engine, 5, start=at(9, 0, 7), trades=False)
        self.assertTrue(engine.snapshot("7203", at(9, 0, 12)).valid)
        engine.on_quote(quote(at(9, 0, 20)))
        self.assertNotIn("r_5", engine.snapshot("7203", at(9, 0, 20)).values)

    def test_t14_lunch_resets_rolling_but_preserves_day_vwap(self):
        engine = self.engine(required=("vwap",))
        engine.on_quote(quote(at(11, 29, 59)))
        engine.set_daily_vwap("7203", at(11, 29, 59), Decimal("300100"), 100, source="TBT", complete=True)
        engine.set_trade_stream_health("7203", at(11, 29, 59), True)
        before = engine.snapshot("7203", at(11, 29, 59)).values["vwap"]
        engine.on_quote(quote(at(12, 30)))
        after = engine.snapshot("7203", at(12, 30))
        self.assertEqual(after.values["vwap"], before)
        self.assertNotIn("r_5", after.values)
        self.assertNotIn("vwap_slope_5", after.values)
        self.assertFalse(engine.on_quote(quote(at(15, 25))))

    def test_t17_t25_ti60_not_ready_after30_and_bid_trade_is_sell(self):
        engine = self.engine(required=("ti_10", "ti_60"))
        self.feed(engine, 30, side="SELL")
        snap = engine.snapshot("7203", at(9, 0, 30))
        self.assertFalse(snap.valid)
        self.assertNotIn("ti_60", snap.values)
        self.assertEqual(snap.values["ti_10"], -1)
        self.assertEqual(snap.values["classification_coverage_10"], 1)
        # Feed additional distinct receive-time events, not the old first half.
        for index in range(31, 61):
            stamp = at() + timedelta(seconds=index)
            q = quote(stamp, price=str(3001 + index))
            engine.on_quote(q)
            engine.on_trade(Trade("7203", stamp, q.bid, 100, f"trade-{index}"))
        self.assertTrue(engine.snapshot("7203", at(9, 1)).valid)
        self.assertEqual(engine.snapshot("7203", at(9, 1)).values["ti_60"], -1)

    def test_low_classification_coverage_and_unknown_volume_never_zero_ti(self):
        engine = self.engine(required=("ti_10",))
        self.feed(engine, 10, side="UNKNOWN")
        snap = engine.snapshot("7203", at(9, 0, 10))
        self.assertFalse(snap.valid)
        self.assertEqual(snap.values["classification_coverage_10"], 0)
        self.assertNotIn("ti_10", snap.values)
        empty = self.engine(required=("ti_10",))
        self.feed(empty, 10, trades=False)
        self.assertNotIn("ti_10", empty.snapshot("7203", at(9, 0, 10)).values)

    def test_rvol_only_past_known_same_time_same_source_baselines(self):
        engine = self.engine(required=("rvol_30",))
        self.feed(engine, 30)
        end = 9 * 3600 + 30
        for day, known, source in ((DAY, at(8), "TBT"), (date(2026, 10, 1), at(10), "TBT"),
                                   (date(2026, 10, 1), at(8), "OTHER")):
            engine.add_volume_baseline(VolumeBaseline("7203", day, end, 30, 1500, known, source))
        self.assertFalse(engine.snapshot("7203", at(9, 0, 30)).valid)
        engine.add_volume_baseline(VolumeBaseline("7203", date(2026, 10, 1), end, 30, 1500, at(8)))
        snap = engine.snapshot("7203", at(9, 0, 30))
        self.assertTrue(snap.valid)
        self.assertEqual(snap.values["rvol_30"], 2)

    def test_partial_day_requires_trusted_daily_vwap_seed(self):
        engine = self.engine(required=("vwap",))
        self.feed(engine, 5, start=at(10))
        self.assertFalse(engine.snapshot("7203", at(10, 0, 5)).valid)
        self.assertNotIn("vwap", engine.snapshot("7203", at(10, 0, 5)).values)
        engine.set_daily_vwap("7203", at(10, 0, 5), Decimal("600200"), 200, source="TBT", complete=True)
        self.assertTrue(engine.snapshot("7203", at(10, 0, 5)).valid)

    def test_volume_duplicate_correction_and_stream_gap_fail_closed(self):
        engine = self.engine(required=("vwap",))
        self.feed(engine, 5)
        state = engine._series["7203"]
        original = state.trades[-1][0]
        cumulative = state.cumulative_size
        self.assertFalse(engine.on_trade(replace(original, at=at(9, 0, 6))))
        self.assertEqual(state.cumulative_size, cumulative)
        self.assertFalse(engine.on_trade(replace(original, size=200)))
        self.assertFalse(engine.snapshot("7203", at(9, 0, 5)).valid)
        engine.on_quote(quote(at(9, 0, 6)))
        self.assertNotIn("vwap", engine.snapshot("7203", at(9, 0, 6)).values)

    def test_sampled_cumulative_volume_proxy_deduplicates_and_handles_reset(self):
        engine = self.engine(required=("vwap_proxy",), vwap_kind="SAMPLED", trade_source="L1_CUMULATIVE")
        engine.on_quote(quote(at()))
        self.assertFalse(engine.on_cumulative_volume("7203", at(), 100, Decimal("3001"), "v0"))
        engine.on_quote(quote(at(9, 0, 1)))
        self.assertTrue(engine.on_cumulative_volume("7203", at(9, 0, 1), 200, Decimal("3002"), "v1"))
        self.assertFalse(engine.on_cumulative_volume("7203", at(9, 0, 1), 200, Decimal("3002"), "v1"))
        snap = engine.snapshot("7203", at(9, 0, 1))
        self.assertTrue(snap.valid)
        self.assertEqual(snap.values["vwap_proxy"], 3002)
        self.assertNotIn("vwap", snap.values)
        self.assertFalse(engine.on_cumulative_volume("7203", at(9, 0, 2), 50, Decimal("3002"), "correction"))
        self.assertFalse(engine.snapshot("7203", at(9, 0, 2)).valid)

    def test_market_volatility_uses_known_past_same_time_history(self):
        engine = self.engine(market_volatility_window_seconds=5)
        self.feed(engine, 5, trades=False)
        engine.add_volatility_baseline(VolatilityBaseline("1306", DAY, 9 * 3600 + 5, 5, 1, at(8)))
        engine.add_volatility_baseline(VolatilityBaseline("1306", date(2026, 10, 1), 9 * 3600 + 5, 5, 1, at(10)))
        self.assertNotIn("rv_mkt", engine.market_snapshot(at(9, 0, 5), ["7203"]).values)
        engine.add_volatility_baseline(VolatilityBaseline("1306", date(2026, 10, 1), 9 * 3600 + 5, 5, 1, at(8)))
        market = engine.market_snapshot(at(9, 0, 5), ["7203"])
        self.assertEqual(market.values["rv_mkt"], 0)
        # Market-layer features are computed once per timestamp, not per stock snapshot.
        self.assertNotIn("rv_mkt", engine.snapshot("7203", at(9, 0, 5)).values)

    def test_snapshot_future_stale_and_naive_rejected(self):
        engine = self.engine()
        self.feed(engine, 5, trades=False)
        self.assertFalse(engine.snapshot("7203", at(9, 0, 4)).valid)
        self.assertFalse(engine.snapshot("7203", at(9, 0, 8)).valid)
        with self.assertRaises(ValueError):
            engine.snapshot("7203", datetime(2026, 10, 2, 9))


if __name__ == "__main__":
    unittest.main()
