"""Regression tests for the 2026-10-04 Claude x Codex review (CLAUDE_CODEX_协作文档.md).

Each test names the register ID whose acceptance criterion it encodes.
"""
import json
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory

from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.domain import FeatureSnapshot, MarketRegime, OrderState, Quote, Side
from ibkr_microalpha.economics import CalibrationRow, CalibrationTable, CommissionSchedule
from ibkr_microalpha.execution import ExecutionBook
from ibkr_microalpha.features import FeatureConfig, FeatureEngine, SameTimeProfile, VolumeBaseline
from ibkr_microalpha.market import JST, TickTable, TradingDay
from ibkr_microalpha.replay import Replay, ReplayFailed
from ibkr_microalpha.research import (LabelPolicy, build_calibration_rows, calibration_table_event,
                                      day_block_lower_bound, fit_scalers, label_intent, walk_forward)
from ibkr_microalpha.risk import AccountSnapshot
from tests import test_engine as base
from tests.test_engine import CONFIG, SYMBOL, T


def fixture(document=None):
    """Coordinator fixture helpers without re-running the fixture's own tests."""
    test = base.EngineIntegrationTests("runTest")
    if document is None:
        test.setUp()
        return test
    test.engine = build_engine(document)
    test.engine.calendar.records.append(TradingDay(T.date(), True, T - timedelta(days=1), "synthetic"))
    test.engine.book.reconcile({}, [], [], T, complete=True, ownership_confirmed=True)
    return test


def at(second):
    return T + timedelta(seconds=second)


class ExecutionRiskFixes(unittest.TestCase):
    def test_EXE01_transmitted_sell_above_corrected_holding_is_cancelled_and_locked(self):
        book = ExecutionBook()
        book.reconcile({}, [], [], T, complete=True, ownership_confirmed=True)
        buy = book.submit("b", "S", Side.BUY, 100, D("3001"), T)
        book.drain_commands(T)
        book.fill("x1", buy.order_id, 100, D("3001"), T)
        sell = book.submit("s", "S", Side.SELL, 100, D("2999"), T)
        book.drain_commands(T)
        book.status(sell.order_id, "Submitted", 0, 100, T)
        book.fill("x1c", buy.order_id, 50, D("3001"), T, correction_of="x1")
        self.assertEqual(sell.state, OrderState.CANCEL_PENDING)
        self.assertEqual(sell.possible_remaining, 100)  # exposure kept until terminal report
        self.assertIn("ledger: active sells exceed confirmed holding", book.lock_reasons)
        self.assertEqual([c.kind for c in book.commands], ["CANCEL", "QUERY"])
        restored = ExecutionBook.from_journal(book.journal)
        self.assertEqual(restored.positions["S"].quantity, 50)

    def test_EXE01_unsent_sell_is_dropped_locally_without_lock(self):
        book = ExecutionBook()
        book.reconcile({}, [], [], T, complete=True, ownership_confirmed=True)
        buy = book.submit("b", "S", Side.BUY, 100, D("3001"), T)
        book.drain_commands(T)
        book.fill("x1", buy.order_id, 100, D("3001"), T)
        sell = book.submit("s", "S", Side.SELL, 100, D("2999"), T)
        book.fill("x1c", buy.order_id, 0, D("3001"), T, correction_of="x1")
        self.assertEqual(sell.state, OrderState.CANCELLED)
        self.assertFalse(book.locked)
        self.assertEqual(book.drain_commands(T), [])

    def _queued_entry(self):
        test = fixture()
        for second in (0, 5, 10, 11, 12):
            test.tick(second)
        buy = next(o for o in test.engine.book.orders.values() if o.side == Side.BUY)
        self.assertIsNone(buy.submitted_at)
        return test, buy

    def _requests(self, test, second, sequence=1):
        replay = Replay(test.engine)
        replay.dispatch({"event_id": f"requests-{second}", "received_at": at(second).isoformat(),
                         "sequence": sequence, "type": "requests", "data": {}})
        return replay

    def test_EXE02_stale_quote_never_reaches_the_broker(self):
        test, buy = self._queued_entry()
        replay = self._requests(test, 15)  # quote and market data are 3 s old (limit 2 s)
        self.assertEqual(replay.last_commands, [])
        self.assertEqual(buy.state, OrderState.CANCELLED)
        self.assertIsNone(buy.submitted_at)

    def test_EXE02_valid_entry_is_sent_and_expired_forecast_is_not(self):
        test, buy = self._queued_entry()
        replay = self._requests(test, 12)
        self.assertEqual([c.kind for c in replay.last_commands], ["SUBMIT"])
        test2, buy2 = self._queued_entry()
        test2.engine.forecasts[SYMBOL] = replace(test2.engine.forecasts[SYMBOL], valid_until=at(12))
        replay2 = self._requests(test2, 12)
        self.assertEqual(replay2.last_commands, [])
        self.assertEqual(buy2.state, OrderState.CANCELLED)
        aborts = [e for e in test2.engine.book.journal if e["kind"] == "LOCAL_ABORT"]
        self.assertEqual(aborts[-1]["payload"]["reason"], "forecast_missing_or_stale")

    def test_EXE03_exit_price_rule_failure_is_recorded_not_raised(self):
        test = fixture()
        test.fill_entry()
        engine = test.engine
        quote = Quote(SYMBOL, at(14), D("0.1"), D("0.2"), 100, 100, "tiny", bid_at=at(14), ask_at=at(14))
        engine.quotes[SYMBOL] = quote
        engine._request_exit(SYMBOL, at(14), "STOP_LOSS", True)  # must not raise
        self.assertIn("exit_price_rule_invalid", engine.risk.lock_reasons)
        self.assertTrue(any(r["kind"] == "EXIT_BLOCKED" for r in engine.audit))
        self.assertEqual(test.sells(), [])

    def test_EXE04_entry_score_and_stop_restore_identically_from_journal_and_snapshot(self):
        test = fixture()
        buy = test.working_entry()
        test.tick(13, rs_60=12, rs_30=10)
        test.engine.on_fill(exec_id="fill", order_id=buy.order_id, qty=100, price=D("3001"), at=at(13))
        book = test.engine.book
        from_snapshot = ExecutionBook.from_snapshot(json.loads(json.dumps(book.snapshot())))
        from_journal = ExecutionBook.from_journal(book.journal)
        self.assertEqual(from_snapshot.positions[SYMBOL].entry_score, book.positions[SYMBOL].entry_score)
        self.assertEqual(from_journal.positions[SYMBOL].entry_score, book.positions[SYMBOL].entry_score)
        self.assertEqual(from_journal.positions[SYMBOL].stop_price, book.positions[SYMBOL].stop_price)
        self.assertIn("ENTRY_SNAPSHOT", [event["kind"] for event in book.journal])

    def test_EXE05_broker_funds_cap_modeled_cash_and_can_be_required(self):
        test = fixture()
        engine = test.engine
        test.tick(0)
        engine.set_account(AccountSnapshot("U1", "JPY", D("1000"), D("1000"), at(1), "broker",
                                          ledger_sequence=engine.book.journal[-1]['sequence']), at(1))
        self.assertEqual(engine.risk.cash, D("1000"))
        self.assertEqual(engine.valuation.cash_source, "reconciled")
        document = base.fixture_document()
        document["risk"]["require_account_snapshot"] = True
        strict = fixture(document)
        strict.tick(0)
        self.assertIn("account_snapshot_missing", strict.engine.risk.soft_blocks)
        for second in (5, 10, 11, 12):
            strict.tick(second)
        self.assertEqual(strict.engine.book.orders, {})

    def test_EXE06_lost_trade_stream_cancels_working_entry_immediately(self):
        test = fixture()
        buy = test.working_entry()
        test.engine.on_trade_stream_health(SYMBOL, at(12.5), False, "SAMPLED")
        self.assertEqual(buy.state, OrderState.CANCEL_PENDING)

    def test_EXE08_routine_request_budget_never_blocks_risk_exits(self):
        book = ExecutionBook(daily_request_budget=1)
        book.reconcile({}, [], [], T, complete=True, ownership_confirmed=True)
        first = book.submit("e1", "A", Side.BUY, 100, D("1000"), T)
        second = book.submit("e2", "B", Side.BUY, 100, D("1000"), T)
        sent = book.drain_commands(T)
        self.assertEqual([c.order_id for c in sent], [first.order_id])
        self.assertEqual(second.state, OrderState.CANCELLED)
        book.fill("f", first.order_id, 100, D("1000"), T)
        sell = book.submit("x", "A", Side.SELL, 100, D("990"), T, emergency=True)
        self.assertEqual([c.order_id for c in book.drain_commands(T)], [sell.order_id])
        self.assertEqual(book.sent_counts["SUBMIT"], 2)


class StrategyFixes(unittest.TestCase):
    def test_STR02_quiet_held_quote_is_soft_and_never_dumps_the_position(self):
        test = fixture()
        test.fill_entry()
        engine = test.engine
        # Market data refreshed; the held stock's quote is 3.5 s old and unchanged.
        engine.set_market(FeatureSnapshot("BENCH_SYNTHETIC", at(16.5),
                                          dict(rv_mkt=1, spread_bps=2, breadth=.8), True), at(16.5))
        engine.evaluate(replace(test.prepared_snapshot(16.5), valid=False, reason="MISSING:r_300"), at(16.5))
        self.assertFalse(engine.risk.locked)
        self.assertEqual(test.sells(), [])

    def test_STR02_market_data_lag_blocks_entries_without_emergency_exit(self):
        test = fixture()
        test.fill_entry()
        engine = test.engine
        engine.quotes[SYMBOL] = Quote(SYMBOL, at(15.5), D("3000"), D("3001"), 900, 100, "fresh",
                                      bid_at=at(15.5), ask_at=at(15.5))
        engine.poll(at(15.5))
        self.assertEqual(engine.market_state, MarketRegime.MARKET_UNKNOWN)
        self.assertIn("market_data_unavailable", engine.risk.soft_blocks)
        self.assertEqual(test.sells(), [])

    def test_STR02_soft_blocks_do_not_escalate_while_flat(self):
        test = fixture()
        engine = test.engine
        engine.poll(at(0))
        engine.poll(at(600))
        self.assertIn("market_data_unavailable", engine.risk.soft_blocks)
        self.assertFalse(engine.risk.locked)

    def test_STR03_entry_order_expires_after_frozen_ttl_without_requote(self):
        test = fixture()
        buy = test.working_entry()
        self.assertEqual(buy.limit_price, D("3002"))
        test.tick(14, bid="3001", ask="3002")
        self.assertEqual(buy.state, OrderState.CANCEL_PENDING)
        test.engine.book.status(buy.order_id, "Cancelled", 0, 0, at(14))
        test.tick(15)
        self.assertEqual(len([o for o in test.engine.book.orders.values() if o.side == Side.BUY]), 1)

    def test_FLOW02_calibration_table_drives_entry_and_cap_is_immutable(self):
        document = base.fixture_document()
        document["engine"]["economics_source"] = "calibration"
        test = fixture(document)
        engine = test.engine
        c = engine.config
        row = CalibrationRow(c.policy_id, c.model_version, c.holding_seconds, 100, 0.5, None, 40, 100,
                             D("2000"), D("1500"), 3, label_source='ARTIFICIAL',
                             provenance={'source':'ARTIFICIAL_FIXTURE'})
        engine.set_calibration(CalibrationTable([row], known_at=T - timedelta(hours=1), version="cal"), at(0))
        for second in (0, 5, 10, 11, 12):
            test.tick(second)
        buy = next(o for o in engine.book.orders.values() if o.side == Side.BUY)
        candidate = engine.alpha.current(SYMBOL)
        # Cap = candidate reference ask (3001 at 10 s) + 3 ticks, fixed for the candidate.
        self.assertEqual(engine.entries._caps[candidate.candidate_id], D("3004"))
        self.assertEqual(buy.limit_price, D("3002"))

    def test_FLOW02_missing_or_future_calibration_rejects_candidates(self):
        document = base.fixture_document()
        document["engine"]["economics_source"] = "calibration"
        test = fixture(document)
        c = test.engine.config
        row = CalibrationRow(c.policy_id, c.model_version, c.holding_seconds, 100, 0.5, None, 40, 100,
                             D("2000"), D("1500"), 3, label_source='ARTIFICIAL',
                             provenance={'source':'ARTIFICIAL_FIXTURE'})
        for second in (0, 5, 10):
            test.tick(second)
        self.assertGreater(test.engine.rejections["calibration_unavailable"], 0)
        future = CalibrationTable([row], known_at=at(100), version="late")
        with self.assertRaises(ValueError):
            test.engine.set_calibration(future, at(11))

    def test_FLOW02_calibration_buckets_must_not_overlap(self):
        a = CalibrationRow("p", "v", 120, 100, 0.0, 1.0, 1, 1, D("1"), D("0"), 0)
        b = CalibrationRow("p", "v", 120, 100, 0.5, None, 1, 1, D("1"), D("0"), 0)
        with self.assertRaises(ValueError):
            CalibrationTable([a, b], known_at=T, version="x")


class DataFixes(unittest.TestCase):
    def engine(self, **kwargs):
        return FeatureEngine("1306", FeatureConfig(required_features=("r_5",), rvol_days=1,
                                                   rvol_min_days=1, **kwargs))

    def quote(self, stamp, symbol="7203", price="3001", event_id=None):
        bid = D(price)
        return Quote(symbol, stamp, bid, bid + 1, 200, 100, event_id or f"{symbol}:{stamp.isoformat()}",
                     bid_at=stamp, ask_at=stamp)

    def test_DATA02_heartbeat_keeps_quiet_book_continuous(self):
        start = datetime(2026, 10, 2, 9, tzinfo=JST)
        covered, uncovered = self.engine(), self.engine()
        for second in range(12):
            stamp = start + timedelta(seconds=second)
            covered.set_quote_stream_health("7203", stamp, True)  # adapter heartbeat every second
            if second < 6:
                covered.on_quote(self.quote(stamp))
                uncovered.on_quote(self.quote(stamp))
        for engine in (covered, uncovered):
            engine.on_quote(self.quote(start + timedelta(seconds=12), price="3002"))
        self.assertEqual(len(covered._series["7203"].quotes), 7)       # no gap reset
        self.assertEqual(len(uncovered._series["7203"].quotes), 1)     # 7 s silence without proof resets
        self.assertIn("r_5", covered.snapshot("7203", start + timedelta(seconds=12)).values)
        covered.set_quote_stream_health("7203", start + timedelta(seconds=13), False)
        self.assertEqual(len(covered._series["7203"].quotes), 0)

    def test_DATA03_profile_lookup_is_bucketed_and_causal(self):
        engine = FeatureEngine("1306", FeatureConfig(required_features=("rvol_30",), baseline_bucket_seconds=300,
                                                     rvol_days=20, rvol_min_days=20))
        when = datetime(2026, 10, 2, 9, 7, tzinfo=JST)
        bucket = 9 * 3600 + 300
        engine.add_profile(SameTimeProfile("volume", "7203", "TBT", 30, bucket, 300, 2000, 20, when - timedelta(days=1), "v1"))
        engine.add_profile(SameTimeProfile("volume", "7203", "TBT", 30, bucket, 300, 1000, 20, when + timedelta(seconds=1), "v2"))
        self.assertEqual(engine._baseline_value("volume", "7203", "TBT", 30, when), 2000)
        self.assertEqual(engine._baseline_value("volume", "7203", "TBT", 30, when + timedelta(seconds=2)), 1000)
        engine.add_profile(SameTimeProfile("volume", "7203", "TBT", 30, bucket + 300, 300, 1000, 5, when, "v1"))
        self.assertIsNone(engine._baseline_value("volume", "7203", "TBT", 30, when + timedelta(minutes=5)))
        with self.assertRaises(ValueError):
            SameTimeProfile("volume", "7203", "TBT", 30, bucket + 1, 300, 1, 1, when, "v1")

    def test_DATA04_volatility_includes_jump_at_window_anchor(self):
        engine = FeatureEngine("BENCH", FeatureConfig(required_features=("volatility_bps",)))
        start = datetime(2026, 10, 2, 10, tzinfo=JST)
        for second in range(61):
            engine.on_quote(self.quote(start + timedelta(seconds=second), "A",
                                       "1000" if second == 0 else "2000", f"v{second}"))
        later = start + timedelta(seconds=60.5)
        engine.on_quote(self.quote(later, "A", "2000", "current"))
        snapshot = engine.snapshot("A", later)
        self.assertGreater(snapshot.values["volatility_bps"], 6000)

    def test_DATA05_invalid_recent_day_does_not_consume_a_valid_slot(self):
        engine = FeatureEngine("B", FeatureConfig(rvol_days=20, rvol_min_days=20))
        when = datetime(2026, 10, 2, 10, tzinfo=JST)
        for offset in range(1, 22):
            engine.add_volume_baseline(VolumeBaseline("A", when.date() - timedelta(days=offset), 36000, 30, 1000,
                                                      when - timedelta(days=1), valid=offset != 1))
        self.assertEqual(engine._baseline_value("volume", "A", "TBT", 30, when), 1000)

    def test_DATA06_example_does_not_require_unused_long_features(self):
        required = load_config(CONFIG)["features"]["required_features"]
        self.assertNotIn("r_600", required)
        self.assertNotIn("obi", required)

    def test_DATA07_sampled_trades_rejected_and_cumulative_volume_used(self):
        engine = build_engine(load_config(CONFIG))
        replay = Replay(engine)
        start = datetime(2026, 10, 2, 9, 1, tzinfo=JST)
        with self.assertRaisesRegex(ValueError, "cumulative_volume"):
            replay.dispatch({"event_id": "t", "received_at": start.isoformat(), "sequence": 1, "type": "trade",
                             "data": {"symbol": SYMBOL, "price": "3001", "size": 100, "source": "SAMPLED",
                                      "volume_kind": "SAMPLED"}})
        self.assertFalse(replay.failed)
        for second, total in ((1, 1000), (2, 1300)):
            replay.dispatch({"event_id": f"q{second}", "received_at": (start + timedelta(seconds=second)).isoformat(),
                             "sequence": 10 * second, "type": "quote", "data": {
                                 "symbol": SYMBOL, "bid": "3001", "ask": "3002", "bid_size": 200, "ask_size": 100,
                                 "source": "L1", "bid_at": (start + timedelta(seconds=second)).isoformat(),
                                 "ask_at": (start + timedelta(seconds=second)).isoformat()}})
            replay.dispatch({"event_id": f"v{second}", "received_at": (start + timedelta(seconds=second)).isoformat(),
                             "sequence": 10 * second + 1, "type": "cumulative_volume",
                             "data": {"symbol": SYMBOL, "total": total, "last_price": "3002"}})
        trade, _ = engine.features._series[SYMBOL].trades[-1]
        self.assertEqual(trade.size, 300)

    def test_DATA08_market_snapshot_computed_once_with_breadth_and_spread(self):
        engine = FeatureEngine("1306", FeatureConfig(required_features=("r_5",), rvol_days=1, rvol_min_days=1))
        start = datetime(2026, 10, 2, 9, tzinfo=JST)
        for second in range(301):
            stamp = start + timedelta(seconds=second)
            engine.on_quote(self.quote(stamp, "1306", "3001"))
            engine.on_quote(self.quote(stamp, "A", str(3001 + second)))
            engine.on_quote(self.quote(stamp, "B", str(4000 - second)))
        market = engine.market_snapshot(start + timedelta(seconds=300), ["A", "B"])
        self.assertEqual(market.values["breadth"], 0.5)
        self.assertIn("spread_bps", market.values)
        self.assertNotIn("breadth", engine.snapshot("A", start + timedelta(seconds=300)).values)

    def test_DATA01_enhanced_coordinator_ranks_subscribes_and_counts_ready_candidates(self):
        document = deepcopy(base.fixture_document())
        for group, key in (("engine", "score_version"), ("features", "version"), ("confirmation", "version")):
            document[group][key] = "enhanced-v1"
        document["features"].update(vwap_kind="TICK", trade_source="TBT")
        document["features"]["required_features"] = [n.replace("vwap_proxy_", "vwap_")
                                                     for n in document["features"]["required_features"]] + ["ti_10"]
        document["confirmation"].update(enhanced=True, required_ti_features=["ti_10"])
        document["subscriptions"] = {"quota": 2, "min_tenure_seconds": 120, "plan_interval_seconds": 30,
                                     "required_windows": {"ti_10": 10, "r_30": 30}}
        test = fixture(document)
        engine = test.engine
        values = dict(r_300=10, rs_300=8, rs_30=4, rs_60=5, rvol_30=3, vwap_slope_60=2, vwap_slope_120=3,
                      vwap_deviation_bps=10, spread_bps=3.33, volatility_bps=8, ti_10=.5,
                      classification_coverage_10=1, r_30=1)
        for second in range(0, 45):
            stamp = at(second)
            engine.quotes[SYMBOL] = Quote(SYMBOL, stamp, D("3000"), D("3001"), 900, 100, f"e{second}",
                                          bid_at=stamp, ask_at=stamp)
            engine.on_trade_stream_health(SYMBOL, stamp, True, "TBT")
            engine.set_market(FeatureSnapshot("M", stamp, dict(rv_mkt=1, spread_bps=2, breadth=.8), True), stamp)
            engine.evaluate(FeatureSnapshot(SYMBOL, stamp, values, True, "enhanced-v1"), stamp)
        self.assertIn(SYMBOL, engine.subscriptions.scheduler.active)
        self.assertGreater(engine.funnel["candidates"], 0)
        self.assertIsNotNone(engine.subscriptions.ready_candidate_ratio)


class ReplayConfigFixes(unittest.TestCase):
    def test_CFG01_string_validity_flag_is_rejected_everywhere(self):
        with self.assertRaisesRegex(ValueError, "boolean"):
            FeatureSnapshot(SYMBOL, T, {"r_300": 1.0}, "false")
        with self.assertRaises(ValueError):
            FeatureSnapshot(SYMBOL, T, {"r_300": True}, True)
        replay = Replay(build_engine(load_config(CONFIG)))
        with self.assertRaisesRegex(ValueError, "boolean"):
            replay.dispatch({"event_id": "s", "received_at": T.isoformat(), "sequence": 1,
                             "type": "feature_snapshot", "data": {"symbol": SYMBOL, "values": {},
                                                                  "valid": "false", "version": "l1-proxy-v1"}})
        self.assertEqual(replay.events_processed, 0)

    def test_CFG02_typed_config_fields_fail_at_validation_time(self):
        for group, field, value in (("features", "rvol_days", 20.5), ("engine", "entry_limit_ticks", 1.0),
                                    ("risk", "max_positions", "3"), ("quality", "require_field_times", "true")):
            document = deepcopy(load_config(CONFIG))
            document[group][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_engine(document)

    def test_CFG03_session_schedule_is_frozen_config(self):
        document = deepcopy(load_config(CONFIG))
        document["session"]["morning_entry_cutoff"] = "11:00"
        engine = build_engine(document)
        gate = engine.calendar.entry_gate(datetime(2026, 10, 2, 11, 5, tzinfo=JST), max_hold_seconds=120)
        self.assertEqual(gate.reason, "CALENDAR_UNKNOWN")  # calendar first; then the frozen cutoff applies
        engine.calendar.records.append(TradingDay(date(2026, 10, 2), True, T - timedelta(days=1), "s"))
        gate = engine.calendar.entry_gate(datetime(2026, 10, 2, 11, 5, tzinfo=JST), max_hold_seconds=120)
        self.assertEqual(gate.reason, "ENTRY_CUTOFF")
        document["session"]["morning_entry_cutoff"] = "11:40"
        with self.assertRaises(ValueError):
            build_engine(document)

    def test_CFG04_score_weights_are_frozen_with_matching_scalers(self):
        document = deepcopy(load_config(CONFIG))
        document["score_weights"] = {"rs_60": 1.0, "rs_30": 0, "ln_rvol_30": 0, "vwap_slope_60": 0}
        self.assertEqual(build_engine(document).alpha.weights["rs_60"], 1.0)
        document["score_weights"] = {"rs_60": 1.0}
        with self.assertRaises(ValueError):
            build_engine(document)

    def test_STR05_non_demo_profile_requires_provenance_and_sane_clip_rates(self):
        document = deepcopy(load_config(CONFIG))
        document["profile"] = "research"
        with self.assertRaisesRegex(ValueError, "provenance"):
            build_engine(document)
        entry = {"source": "walk-forward 2026-08", "trained_until": "2026-09-30T15:30:00+09:00", "code_hash": "abc"}
        document["provenance"] = {"scalers": {**entry, "clip_rates": {k: 0.01 for k in document["scalers"]}},
                                  "thresholds": entry, "economics": entry}
        build_engine(document)
        document["provenance"]["scalers"]["clip_rates"]["rs_60"] = 0.4
        with self.assertRaisesRegex(ValueError, "clips"):
            build_engine(document)

    def test_channel_budget_infeasible_config_is_rejected(self):
        document = deepcopy(load_config(CONFIG))
        document["engine"]["entry_order_ttl_seconds"] = 30
        with self.assertRaisesRegex(ValueError, "channel time budget"):
            build_engine(document)

    def test_RPL01_parse_failure_does_not_poison_or_commit_identity(self):
        replay = Replay(build_engine(load_config(CONFIG)))
        bad = {"event_id": "c", "received_at": T.isoformat(), "sequence": 1, "type": "calendar",
               "data": {"day": "2026-10-02", "is_open": True, "known_at": at(1).isoformat(), "source": "s"}}
        for _ in range(2):
            with self.assertRaises(ValueError):
                replay.dispatch(deepcopy(bad))
        self.assertFalse(replay.failed)
        self.assertNotIn("c", replay.seen_digests)

    def test_RPL01_apply_failure_poisons_runner(self):
        replay = Replay(build_engine(load_config(CONFIG)))
        replay.dispatch({"event_id": "r", "received_at": T.isoformat(), "sequence": 1, "type": "reconcile",
                         "data": {"positions": {}, "open_orders": [], "executions": [], "complete": True,
                                  "ownership_confirmed": True}})
        with self.assertRaises(ValueError):
            replay.dispatch({"event_id": "f", "received_at": at(1).isoformat(), "sequence": 2, "type": "fill",
                             "data": {"exec_id": "x", "order_id": 999, "qty": 100, "price": "3001"}})
        self.assertTrue(replay.failed)
        with self.assertRaises(ReplayFailed):
            replay.dispatch({"event_id": "t", "received_at": at(2).isoformat(), "sequence": 3, "type": "timer"})

    def test_RPL01_cli_writes_failure_artifact(self):
        from ibkr_microalpha.cli import _replay_file
        with TemporaryDirectory() as directory:
            source = Path(directory) / "bad.jsonl"
            source.write_text(json.dumps({"event_id": "x", "received_at": T.isoformat(), "sequence": "1",
                                          "type": "timer", "data": {}}) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                _replay_file(CONFIG, source, Path(directory) / "out")
            failure = json.loads((Path(directory) / "out" / "failure.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["status"], "FAILED")
            self.assertFalse((Path(directory) / "out" / "report.json").exists())

    def test_RPT03_no_trade_audit_records_only_reason_changes(self):
        test = fixture()
        engine = test.engine
        engine.risk.lock("KILL_SWITCH")
        for second in range(5):
            test.tick(second)
        records = [r for r in engine.audit if r["kind"] == "NO_TRADE"]
        self.assertEqual(len(records), 1)
        self.assertEqual(engine.rejections["risk_locked"], 5)

    def test_PERF02_same_time_quote_and_volume_produce_one_snapshot(self):
        engine = build_engine(load_config(CONFIG))
        calls = []
        original = engine.features.snapshot
        engine.features.snapshot = lambda symbol, when: calls.append(when) or original(symbol, when)
        replay = Replay(engine)
        start = datetime(2026, 10, 2, 9, 1, tzinfo=JST)
        sequence = 0
        for second in range(3):
            stamp = (start + timedelta(seconds=second)).isoformat()
            for kind, data in (("quote", {"symbol": SYMBOL, "bid": "3001", "ask": "3002", "bid_size": 200,
                                          "ask_size": 100, "source": "L1", "bid_at": stamp, "ask_at": stamp}),
                               ("cumulative_volume", {"symbol": SYMBOL, "total": 100 * (second + 1),
                                                      "last_price": "3002"})):
                sequence += 1
                replay.dispatch({"event_id": f"e{sequence}", "received_at": stamp, "sequence": sequence,
                                 "type": kind, "data": data})
        replay.finish()
        self.assertEqual(len(calls), 3)


class ResearchPipeline(unittest.TestCase):
    def quotes(self, asks, start=None, size=500):
        start = start or datetime(2026, 10, 2, 10, tzinfo=JST)
        return [Quote("A", start + timedelta(seconds=i), D(a) - 1, D(a), size, size, f"q{i}",
                      bid_at=start + timedelta(seconds=i), ask_at=start + timedelta(seconds=i))
                for i, a in enumerate(asks)]

    def policy(self):
        return LabelPolicy(holding_seconds=5, entry_limit_ticks=1, entry_order_ttl_seconds=2,
                           min_stop_ticks=5, stop_bps=D("0"), exit_slippage_ticks=1)

    def test_STR01_label_uses_only_later_quotes_fees_and_max_holding(self):
        quotes = self.quotes(["3001", "3001", "3003", "3004", "3005", "3006", "3007", "3008"])
        fees = CommissionSchedule(D("0.0008"), D("80"), D("0"), "test")
        deadline = quotes[-1].at + timedelta(hours=1)
        net = label_intent(quotes, 0, 100, self.policy(), TickTable(), fees, deadline)
        # Quote baseline: buy3001 at1s; bid3006 at6s with one-tick haircut to3005.
        # This restricted full-fill result cannot be deployed as a policy label.
        self.assertEqual(net, D("400") - D("240.08") - D("240.40"))

    def test_STR01_unfilled_intent_is_zero_and_unclosable_is_none(self):
        fees = CommissionSchedule(D("0.0008"), D("80"), D("0"), "test")
        policy = self.policy()
        deadline = datetime(2026, 10, 2, 11, tzinfo=JST)
        running_away = self.quotes(["3001", "3005", "3006", "3007"])
        self.assertEqual(label_intent(running_away, 0, 100, policy, TickTable(), fees, deadline), D(0))
        short = self.quotes(["3001", "3001", "3002"])
        self.assertIsNone(label_intent(short, 0, 100, policy, TickTable(), fees, deadline))

    def test_STR01_day_block_bound_is_deterministic_and_conservative(self):
        samples = {date(2026, 9, d): [D(10), D(12)] if d % 2 else [D(-5)] for d in range(1, 21)}
        first = day_block_lower_bound(samples, seed=3)
        self.assertEqual(first, day_block_lower_bound(samples, seed=3))
        self.assertLessEqual(first[1], first[0])

    def test_STR01_unverified_legacy_samples_cannot_become_deployment_rows(self):
        samples = [(date(2026, 9, d), 1.0 + (d % 3) * 0.1, D(100)) for d in range(1, 31)]
        samples += [(date(2026, 9, 1), 5.0, D(500))]
        with self.assertRaisesRegex(ValueError, 'ReplayIntentLabel'):
            build_calibration_rows(samples, [0.5, 2.0], policy_id="p", version="v", holding_seconds=120,
                                   quantity=100, max_chase_ticks=2, min_days=20, min_samples=20)

    def test_STR01_scaler_fit_reports_clip_rates(self):
        scalers, clip = fit_scalers({"rs_60": [0, 1, 2, 3, 100]}, {"rs_60": .1})
        self.assertEqual(scalers["rs_60"].median, 2)
        self.assertEqual(clip["rs_60"], 0.2)

    def test_STR01_walk_forward_has_embargo_and_untouched_holdout(self):
        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(120)]
        folds, holdout = walk_forward(days, train=40, validate=10, test=10, holdout=20, embargo=1)
        self.assertEqual(len(holdout), 20)
        self.assertTrue(folds)
        for fold in folds:
            self.assertLess(max(fold.train), min(fold.validate))
            self.assertLess(max(fold.validate), min(fold.test))
            self.assertGreater((min(fold.validate) - max(fold.train)).days, 1)
            self.assertTrue(set(fold.test).isdisjoint(holdout))


if __name__ == "__main__":
    unittest.main()
