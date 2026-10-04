import math
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from ibkr_microalpha.domain import Candidate, Confirmation, FeatureSnapshot, MarketRegime, Quote, Regime
from ibkr_microalpha.signals import (
    AlphaConfig, AlphaDecayTracker, AlphaEngine, ConfirmationConfig, ConfirmationEngine,
    DecayConfig, FrozenRobustScaler, MarketRegimeConfig, MarketRegimeEngine,
    RegimeConfig, RegimeEngine, alpha_score,
)

NOW = datetime(2026, 10, 2, 1, tzinfo=timezone.utc)


def snapshot(seconds=0, **values):
    fields = dict(rs_30=2, rs_60=3, rvol_30=2, vwap_slope_60=1,
                  vwap_deviation_bps=5, spread_bps=10, volatility_bps=5,
                  r_300=3, rs_300=2, vwap_slope_120=1)
    fields.update(values)
    return FeatureSnapshot("7203", NOW + timedelta(seconds=seconds), fields, True, "l1-v1")


def quote(seconds=0, *, event=None, bid="1000", ask="1001", bid_size=400, ask_size=100):
    return Quote("7203", NOW + timedelta(seconds=seconds), D(bid), D(ask), bid_size,
                 ask_size, event or f"q{seconds}")


def scalers():
    return {name: FrozenRobustScaler(0, 1, .001)
            for name in ("rs_60", "rs_30", "ln_rvol_30", "vwap_slope_60")}


def alpha():
    config = AlphaConfig(20, 5, 1, 2, 2, .1, -.1, 0, 0, 1.5, 30, 20, 30)
    return AlphaEngine(config, scalers(), "l1-v1")


def candidate():
    return Candidate("c1", "7203", NOW, NOW + timedelta(seconds=20), 1,
                     "l1-v1", D("1000"), D("1001"))


CAP = D("1003")


def confirmation_config(**changes):
    base = ConfirmationConfig("l1-v1", False, 2, 3, 4, 1, .75, .1,
                              2, 10, 1, 1, .5, (), .1, .8)
    return replace(base, **changes)


class SignalTests(unittest.TestCase):
    def test_frozen_robust_baseline_weights_and_clip(self):
        s = scalers()
        expected = (.45 * 3 / 1.4826 + .25 * 2 / 1.4826
                    + .20 * math.log(2) / 1.4826 + .10 / 1.4826)
        self.assertAlmostEqual(alpha_score(snapshot(), s), expected)
        self.assertEqual(FrozenRobustScaler(0, 0, .1).transform(1), 3)
        self.assertEqual(FrozenRobustScaler(0, 0, .1).transform(-1), -3)
        self.assertEqual(FrozenRobustScaler.fit_training([1, 2, 3], .01).median, 2)

    def test_proxy_vwap_requires_separate_explicit_version(self):
        original = snapshot()
        values = dict(original.values)
        for key in ("vwap_slope_60", "vwap_slope_120", "vwap_deviation_bps"):
            values[key.replace("vwap_", "vwap_proxy_", 1)] = values.pop(key)
        proxy = replace(original, values=values, version="l1-proxy-v1")
        self.assertAlmostEqual(alpha_score(proxy, scalers()), alpha_score(original, scalers()))
        with self.assertRaises(KeyError):
            alpha_score(replace(proxy, version="l1-v1"), scalers())
        engine = AlphaEngine(alpha().config, scalers(), "l1-proxy-v1")
        self.assertIsNotNone(engine.evaluate(proxy, quote(), Regime.LONG, MarketRegime.MARKET_OK, NOW))
        config = RegimeConfig(5, 10, 2, 1, 1, .5, .1, .1, .1, -.1, -.1, -.1, 20, 30)
        self.assertEqual(RegimeEngine(config).evaluate(proxy, NOW), Regime.NEUTRAL)
        with self.assertRaises(TypeError):
            engine.scalers["rs_30"] = FrozenRobustScaler(0, 2, .001)

    def test_T01_repeat_market_event_keeps_one_fixed_candidate(self):
        engine = alpha()
        args = (snapshot(), quote(), Regime.LONG, MarketRegime.MARKET_OK)
        first = engine.evaluate(*args, NOW)
        self.assertIsNotNone(first)
        self.assertIs(engine.evaluate(*args, NOW), first)
        later = engine.evaluate(snapshot(1), quote(1), Regime.LONG, MarketRegime.MARKET_OK,
                                NOW + timedelta(seconds=1))
        self.assertIs(later, first)
        self.assertEqual(later.expires_at, first.expires_at)

    def test_candidate_is_a_pure_signal_without_invented_execution_inputs(self):
        first = alpha().evaluate(snapshot(), quote(), Regime.LONG, MarketRegime.MARKET_OK, NOW)
        self.assertFalse(hasattr(first, "quantity") or hasattr(first, "max_price"))
        self.assertEqual((first.reference_bid, first.reference_ask), (D("1000"), D("1001")))

    def test_candidate_requires_market_ok_unless_caution_raises_threshold(self):
        engine = alpha()
        for state in (MarketRegime.MARKET_RISK_OFF, MarketRegime.MARKET_UNKNOWN, MarketRegime.MARKET_CAUTION):
            self.assertIsNone(engine.evaluate(snapshot(), quote(), Regime.LONG, state, NOW))
        raised = alpha().evaluate(snapshot(), quote(), Regime.LONG, MarketRegime.MARKET_CAUTION, NOW,
                                  caution_floor=1.0)
        self.assertIsNotNone(raised)
        self.assertIsNone(alpha().evaluate(snapshot(), quote(), Regime.LONG, MarketRegime.MARKET_CAUTION,
                                           NOW, caution_floor=99.0))
        self.assertIsNone(alpha().evaluate(snapshot(spread_bps=999), quote(ask="1100"), Regime.LONG,
                                           MarketRegime.MARKET_OK, NOW))
        self.assertIsNone(alpha().evaluate(replace(snapshot(), valid=False), quote(), Regime.LONG,
                                           MarketRegime.MARKET_OK, NOW))

    def test_caution_policy_is_a_frozen_explicit_choice(self):
        base = dict(max_snapshot_age_seconds=2, caution_rv=2, risk_off_rv=3, caution_spread_bps=15,
                    risk_off_spread_bps=25, min_breadth=.4, require_positive_direction=False)
        self.assertIsNone(MarketRegimeConfig(**base).caution_floor)
        self.assertEqual(MarketRegimeConfig(**base, caution_policy="raise_threshold",
                                            caution_entry_score=1.5).caution_floor, 1.5)
        with self.assertRaises(ValueError):
            MarketRegimeConfig(**base, caution_policy="raise_threshold")
        with self.assertRaises(ValueError):
            MarketRegimeConfig(**base, caution_policy="maybe")

    def test_market_unknown_does_not_force_stock_risk_off(self):
        config = RegimeConfig(5, 10, 2, 1, 1, .5, .1, .1, .1, -.1, -.1, -.1, 20, 30)
        self.assertNotEqual(RegimeEngine(config).evaluate(snapshot(), NOW,
                            market_regime=MarketRegime.MARKET_UNKNOWN), Regime.RISK_OFF)

    def test_T08_environment_invalidates_without_shorting_and_enforces_cooldown(self):
        engine = alpha()
        def run(t, regime=Regime.LONG):
            return engine.evaluate(snapshot(t), quote(t), regime, MarketRegime.MARKET_OK,
                                   NOW + timedelta(seconds=t))
        self.assertIsNotNone(run(0))
        self.assertIsNone(run(1, Regime.NEUTRAL))
        self.assertIsNone(run(2))
        self.assertIsNotNone(run(6))
        self.assertIsNone(run(7, Regime.BEARISH))
        self.assertIsNone(engine.current("7203"))

    def test_T13_candidate_ttl_expires_without_renewal_and_price_cap_veto(self):
        engine = alpha()
        first = engine.evaluate(snapshot(), quote(), Regime.LONG, MarketRegime.MARKET_OK, NOW)
        self.assertIsNone(engine.evaluate(snapshot(20), quote(20), Regime.LONG,
                                         MarketRegime.MARKET_OK, first.expires_at))
        confirm = ConfirmationEngine(confirmation_config())
        result = confirm.evaluate(candidate(), snapshot(), quote(ask="1004"), NOW, max_price=CAP)
        self.assertEqual(result.status, Confirmation.VETO)

    def test_confirmation_needs_distinct_updates_and_elapsed_duration(self):
        engine = ConfirmationEngine(confirmation_config())
        c = candidate()
        for t in range(2):
            result = engine.evaluate(c, snapshot(t), quote(t, bid_size=400 + t), NOW + timedelta(seconds=t),
                                     max_price=CAP)
            self.assertEqual(result.status, Confirmation.WAIT)
        # Local replay of unchanged quote cannot supply a third market update.
        repeated = engine.evaluate(c, snapshot(2), quote(1, bid_size=401), NOW + timedelta(seconds=2),
                                   max_price=CAP)
        self.assertEqual(repeated.valid_updates, 2)
        result = engine.evaluate(c, snapshot(2), quote(2, bid_size=402), NOW + timedelta(seconds=2),
                                 max_price=CAP)
        self.assertEqual(result.status, Confirmation.CONFIRM)

    def test_new_ids_with_identical_quotes_do_not_create_persistence(self):
        engine = ConfirmationEngine(confirmation_config())
        for t in range(3):
            result = engine.evaluate(candidate(), snapshot(t), quote(t), NOW + timedelta(seconds=t),
                                     max_price=CAP)
        self.assertEqual(result.status, Confirmation.WAIT)
        self.assertEqual(result.valid_updates, 1)

    def test_confirmation_cannot_stitch_across_quote_age_gap(self):
        engine = ConfirmationEngine(confirmation_config())
        for t in (0, 1, 8):
            result = engine.evaluate(candidate(), snapshot(t), quote(t, bid_size=400 + t),
                                     NOW + timedelta(seconds=t), max_price=CAP)
        self.assertEqual(result.status, Confirmation.WAIT)
        self.assertEqual(result.valid_updates, 1)

    def test_enhanced_requires_all_ti_features_ready_and_coverage(self):
        c = replace(candidate(), score_version="enhanced-v1")
        config = confirmation_config(version="enhanced-v1", enhanced=True,
                                     required_ti_features=("ti_10", "ti_60"))
        s = replace(snapshot(ti_10=.3, classification_coverage_10=.9), version="enhanced-v1")
        engine = ConfirmationEngine(config)
        result = engine.evaluate(c, s, quote(), NOW, max_price=CAP, enhanced_ready=True)
        self.assertEqual(result.status, Confirmation.VETO)
        full = replace(s, values={**s.values, "ti_60": .2, "classification_coverage_60": .9})
        self.assertEqual(ConfirmationEngine(config).evaluate(c, full, quote(), NOW, max_price=CAP,
                                                             enhanced_ready=False).status, Confirmation.VETO)
        full_low = replace(full, values={**full.values, "classification_coverage_60": .1})
        self.assertEqual(ConfirmationEngine(config).evaluate(c, full_low, quote(), NOW, max_price=CAP,
                                                             enhanced_ready=True).status, Confirmation.VETO)

    def test_single_negative_imbalance_waits_but_persistent_reversal_vetoes(self):
        engine = ConfirmationEngine(confirmation_config(smoothing_updates=3))
        c = candidate()
        first = engine.evaluate(c, snapshot(0), quote(0, bid_size=400), NOW, max_price=CAP)
        self.assertEqual(first.status, Confirmation.WAIT)
        noisy = engine.evaluate(c, snapshot(1), quote(1, bid_size=100, ask_size=130),
                                NOW + timedelta(seconds=1), max_price=CAP)
        self.assertEqual(noisy.status, Confirmation.WAIT)
        self.assertEqual(noisy.reason, "transient negative imbalance")
        reversal = ConfirmationEngine(confirmation_config(smoothing_updates=3))
        for t in range(3):
            result = reversal.evaluate(c, snapshot(t), quote(t, bid_size=100, ask_size=130 + t),
                                       NOW + timedelta(seconds=t), max_price=CAP)
        self.assertEqual(result.status, Confirmation.VETO)
        self.assertEqual(result.reason, "smoothed quote direction reversal")

    def test_regime_entry_persistence_hysteresis_and_market_override(self):
        config = RegimeConfig(5, 10, 2, 1, 1, .5, .1, .1, .1, -.1, -.1, -.1, 20, 30)
        engine = RegimeEngine(config)
        for t in (0, 5):
            self.assertEqual(engine.evaluate(snapshot(t), NOW + timedelta(seconds=t)), Regime.NEUTRAL)
        self.assertEqual(engine.evaluate(snapshot(10), NOW + timedelta(seconds=10)), Regime.LONG)
        modest = snapshot(15, r_300=.2, rs_300=.2, vwap_slope_120=.2)
        self.assertEqual(engine.evaluate(modest, NOW + timedelta(seconds=15)), Regime.LONG)
        self.assertEqual(engine.evaluate(snapshot(16), NOW + timedelta(seconds=16),
                                         market_regime=MarketRegime.MARKET_RISK_OFF), Regime.RISK_OFF)

    def test_environment_cannot_stitch_unobserved_interval(self):
        config = RegimeConfig(5, 10, 2, 1, 1, .5, .1, .1, .1, -.1, -.1, -.1, 20, 30)
        engine = RegimeEngine(config)
        self.assertEqual(engine.evaluate(snapshot(), NOW), Regime.NEUTRAL)
        self.assertEqual(engine.evaluate(snapshot(20), NOW + timedelta(seconds=20)), Regime.NEUTRAL)

    def test_market_data_and_caution_fail_closed(self):
        engine = MarketRegimeEngine(MarketRegimeConfig(2, 2, 3, 15, 25, .4, False))
        s = snapshot(rv_mkt=1, breadth=.6)
        self.assertEqual(engine.evaluate(s, NOW), MarketRegime.MARKET_OK)
        self.assertEqual(engine.evaluate(snapshot(rv_mkt=2, breadth=.6), NOW), MarketRegime.MARKET_CAUTION)
        self.assertEqual(engine.evaluate(snapshot(rv_mkt=3, breadth=.6), NOW), MarketRegime.MARKET_RISK_OFF)
        self.assertEqual(engine.evaluate(replace(s, valid=False), NOW), MarketRegime.MARKET_RISK_OFF)

    def test_T23_centered_scores_never_divide_and_partial_recovery_still_counts(self):
        for entry in (0.0, -1.0, .00001):
            tracker = AlphaDecayTracker(entry, "l1-v1", NOW, DecayConfig(1, 3, 1, 1))
            for t, score in ((1, entry - 2), (2, entry - 1.5), (3, entry - 1)):
                result = tracker.evaluate(snapshot(t, score=score), NOW + timedelta(seconds=t))
            self.assertTrue(result.exit_required)
            self.assertEqual(result.count, 3)
            self.assertEqual(result.score_drop, 1)

    def test_T24_duplicates_versions_invalidity_reset_and_recovery(self):
        tracker = AlphaDecayTracker(-1, "l1-v1", NOW, DecayConfig(1, 2, 1, 1))
        first = tracker.evaluate(snapshot(1, score=-2), NOW + timedelta(seconds=1))
        self.assertEqual(first.count, 1)
        self.assertEqual(tracker.evaluate(snapshot(1, score=-2), NOW + timedelta(seconds=1)).count, 1)
        self.assertEqual(tracker.evaluate(snapshot(0, score=-2), NOW + timedelta(seconds=1)).count, 1)
        invalid = tracker.evaluate(replace(snapshot(2, score=-2), version="v2"), NOW + timedelta(seconds=2))
        self.assertTrue(invalid.data_risk)
        self.assertEqual(invalid.count, 0)
        recovered = tracker.evaluate(snapshot(3, score=-2), NOW + timedelta(seconds=3))
        self.assertEqual(recovered.count, 1)
        self.assertFalse(recovered.exit_required)
        improved = tracker.evaluate(snapshot(4, score=-1.5), NOW + timedelta(seconds=4))
        self.assertEqual(improved.count, 0)

    def test_stale_future_invalid_quote_and_score_fail_closed(self):
        args = (Regime.LONG, MarketRegime.MARKET_OK, NOW)
        self.assertIsNone(alpha().evaluate(snapshot(5), quote(), *args))
        self.assertIsNone(alpha().evaluate(snapshot(), replace(quote(), market_data_type=3), *args))
        self.assertIsNone(alpha().evaluate(replace(snapshot(), version="future-v2"), quote(), *args))


if __name__ == "__main__":
    unittest.main()
