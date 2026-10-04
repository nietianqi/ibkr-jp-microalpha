import json
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.market import JST
from ibkr_microalpha.replay import Replay, digest


START = datetime(2026, 10, 2, 10, tzinfo=JST)
EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "examples" / "research.json"


def event(kind="timer", sequence=1, *, received_at=START, event_id=None, data=None):
    return {"received_at": received_at.isoformat(), "sequence": sequence,
            "event_id": event_id or f"event-{sequence}", "type": kind,
            "data": {} if data is None else data}


def calendar_data(**overrides):
    result = {"day": "2026-10-02", "is_open": True, "known_at": START.isoformat(),
              "source": "synthetic-calendar", "version": "synthetic-v1"}
    result.update(overrides)
    return result


def forecast_data(**overrides):
    result = {
        "symbol": "STOCK_SYNTHETIC",
        "prediction": {"policy_id": "l1-proxy-h120-v1", "version": "synthetic-v1",
                       "quantity": 100, "sample_count": 100,
                       "mean_net_amount": "1000", "lower_net_amount": "500",
                       "reliable": True, "calibrated": True, "max_holding_seconds": 120},
        "trained_until": (START - timedelta(days=1)).isoformat(),
        "valid_until": (START + timedelta(seconds=30)).isoformat(),
        "reference_entry_price": "3002", "max_entry_price": "3009"}
    result.update(overrides)
    return result


def quote_data(stamp, **overrides):
    result = {"symbol": "STOCK_SYNTHETIC", "bid": "3001", "ask": "3002",
              "bid_size": 200, "ask_size": 100, "source": "L1", "market_data_type": 1,
              "market_status": "CONTINUOUS", "bid_at": stamp.isoformat(), "ask_at": stamp.isoformat()}
    result.update(overrides)
    return result


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.engine = build_engine(load_config(EXAMPLE_CONFIG))
        self.replay = Replay(self.engine)

    def test_duplicate_identity_is_idempotent_and_conflicts_rejected(self):
        original = event("calendar", data=calendar_data())
        self.replay.dispatch(original)
        self.replay.dispatch(deepcopy(original))
        self.assertEqual(self.replay.events_processed, 1)
        self.assertEqual(len(self.engine.calendar.records), 1)
        conflicting = deepcopy(original)
        conflicting["data"]["policy_meeting"] = True
        with self.assertRaisesRegex(ValueError, "conflicting event identity"):
            self.replay.dispatch(conflicting)
        self.assertFalse(self.engine.calendar.records[0].policy_meeting)

    def test_deeply_nested_forecast_input_is_unchanged_and_detached(self):
        original = event("forecast", data=forecast_data())
        frozen = deepcopy(original)
        self.replay.dispatch(original)
        self.assertEqual(original, frozen)
        # Only a digest of the canonical event is retained, never the payload.
        self.assertEqual(self.replay.seen_digests[original["event_id"]], digest(frozen))
        original["data"]["prediction"]["mean_net_amount"] = "99999"
        self.assertEqual(self.engine.forecasts["STOCK_SYNTHETIC"].prediction.mean_net_amount, Decimal("1000"))
        with self.assertRaisesRegex(ValueError, "conflicting event identity"):
            self.replay.dispatch(original)

    def test_quote_calendar_and_snapshot_inputs_are_immutable(self):
        for received in (
            event("calendar", sequence=1, data=calendar_data()),
            event("quote", sequence=2, data=quote_data(START)),
            event("feature_snapshot", sequence=3, data={"symbol": "STOCK_SYNTHETIC",
                "values": {"r_300": 1.0}, "valid": False, "version": "l1-proxy-v1"}),
        ):
            frozen = deepcopy(received)
            self.replay.dispatch(received)
            self.assertEqual(received, frozen)
        received["data"]["values"]["r_300"] = 999
        self.assertEqual(self.engine.snapshots["STOCK_SYNTHETIC"].values["r_300"], 1.0)
        self.assertEqual(self.engine.quotes["STOCK_SYNTHETIC"].bid, Decimal("3001"))

    def test_future_known_calendar_scheduled_window_and_baseline_rejected(self):
        future = (START + timedelta(seconds=1)).isoformat()
        inputs = [
            event("calendar", data=calendar_data(known_at=future)),
            event("scheduled_window", data={"event_id": "policy", "start": START.isoformat(),
                "end": (START + timedelta(minutes=5)).isoformat(), "known_at": future, "source": "synthetic"}),
            event("volume_baseline", data={"symbol": "STOCK_SYNTHETIC", "day": "2026-10-01",
                "end_second": 36000, "window_seconds": 30, "volume": 1000, "known_at": future,
                "source": "SAMPLED"})]
        for received in inputs:
            replay = Replay(build_engine(load_config(EXAMPLE_CONFIG)))
            with self.subTest(kind=received["type"]), self.assertRaises(ValueError):
                replay.dispatch(received)
            self.assertEqual(replay.events_processed, 0)
            self.assertEqual(len(replay.engine.calendar.records), 0)
            self.assertEqual(len(replay.engine.calendar.scheduled_windows), 0)

    def test_future_training_and_expired_forecast_rejected(self):
        for data in (forecast_data(trained_until=START.isoformat()),
                     forecast_data(trained_until=(START + timedelta(seconds=1)).isoformat()),
                     forecast_data(valid_until=START.isoformat())):
            replay = Replay(build_engine(load_config(EXAMPLE_CONFIG)))
            with self.assertRaisesRegex(ValueError, "forecast must use past training"):
                replay.dispatch(event("forecast", data=data))
            self.assertEqual(replay.events_processed, 0)
            self.assertEqual(replay.engine.forecasts, {})

    def test_received_time_then_stable_sequence_controls_order(self):
        self.replay.dispatch(event(sequence=1))
        self.replay.dispatch(event(sequence=2))
        self.assertEqual(self.replay.events_processed, 2)
        with self.assertRaisesRegex(ValueError, "ordered by receive time"):
            self.replay.dispatch(event(sequence=2, event_id="different-same-key"))
        with self.assertRaisesRegex(ValueError, "ordered by receive time"):
            self.replay.dispatch(event(sequence=3, received_at=START - timedelta(seconds=1)))
        with self.assertRaisesRegex(ValueError, "ordered by receive time"):
            self.replay.dispatch(event(sequence=0))

    def test_late_exchange_timestamp_does_not_move_received_trade_backwards(self):
        document = deepcopy(load_config(EXAMPLE_CONFIG))
        for group, key in (("engine", "score_version"), ("features", "version"), ("confirmation", "version")):
            document[group][key] = "tbt-v1"
        document["features"].update(trade_source="TBT", vwap_kind="TICK")
        document["features"]["required_features"] = [name.replace("vwap_proxy_", "vwap_")
                                                     for name in document["features"]["required_features"]]
        self.engine = build_engine(document)
        self.replay = Replay(self.engine)
        self.replay.dispatch(event("quote", sequence=1, data=quote_data(START)))
        later = START + timedelta(seconds=1)
        received = event("trade", sequence=2, received_at=later, data={"symbol": "STOCK_SYNTHETIC",
            "price": "3002", "size": 100, "source": "TBT", "volume_kind": "TICK",
            "market_data_type": 1, "exchange_at": (START - timedelta(seconds=10)).isoformat()})
        original = deepcopy(received)
        self.replay.dispatch(received)
        self.assertEqual(received, original)
        self.assertEqual(self.engine._last_at, later)
        trade, inferred = self.engine.features._series["STOCK_SYNTHETIC"].trades[-1]
        self.assertEqual(trade.at, later)
        self.assertEqual(trade.exchange_at, START - timedelta(seconds=10))
        self.assertEqual(inferred, "UNKNOWN")

    def test_malformed_sequence_and_naive_receive_time_rejected(self):
        for sequence in (-1, True, "1", 1.5, None):
            replay = Replay(build_engine(load_config(EXAMPLE_CONFIG)))
            with self.subTest(sequence=sequence), self.assertRaisesRegex(ValueError, "nonnegative integer"):
                replay.dispatch(event(sequence=sequence))
            self.assertEqual(replay.events_processed, 0)
        malformed = event()
        malformed["received_at"] = "2026-10-02T10:00:00"
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.replay.dispatch(malformed)

    def test_jsonl_rejection_reports_precise_source_line(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "bad.jsonl"
            source.write_text(json.dumps(event()) + "\n" +
                json.dumps(event(sequence="2")) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"bad\.jsonl:2: sequence"):
                self.replay.run(source)

    def test_exact_duplicate_after_newer_event_does_not_rewind_engine(self):
        original = event()
        self.replay.dispatch(original)
        later = START + timedelta(seconds=1)
        self.replay.dispatch(event(sequence=2, received_at=later))
        self.replay.dispatch(deepcopy(original))
        self.assertEqual(self.replay.events_processed, 2)
        self.assertEqual(self.engine._last_at, later)

    def test_raw_trade_health_is_independent_of_feature_warmup(self):
        features = self.engine.features
        self.assertFalse(features.trade_stream_healthy("STOCK_SYNTHETIC", START))
        features.set_trade_stream_health("STOCK_SYNTHETIC", START, True, "SAMPLED")
        self.assertTrue(features.trade_stream_healthy("STOCK_SYNTHETIC", START))
        self.assertFalse(features.snapshot("STOCK_SYNTHETIC", START).valid)
        self.assertFalse(features.trade_stream_healthy("STOCK_SYNTHETIC", START - timedelta(seconds=1)))
        self.assertFalse(features.trade_stream_healthy("STOCK_SYNTHETIC", START + timedelta(seconds=3)))
        features.set_trade_stream_health("STOCK_SYNTHETIC", START + timedelta(seconds=4), False, "SAMPLED")
        self.assertFalse(features.trade_stream_healthy("STOCK_SYNTHETIC", START + timedelta(seconds=4)))


if __name__ == "__main__":
    unittest.main()
