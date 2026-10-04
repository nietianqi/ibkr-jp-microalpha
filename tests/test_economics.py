import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from ibkr_microalpha.economics import (
    ChannelBudget, CommissionSchedule, IntentPath, PairedAdvantage, PathFill,
    Prediction, WeightedPath, choose_execution_policy, common_valid_sample,
    cost_diagnostic, expected_intent_value, intent_path_value,
    late_midpoint_net_amount, net_bps, prediction_gate, reference_sigma_bps,
)

NOW = datetime(2026, 10, 2, 1, tzinfo=timezone.utc)


def prediction(policy="aggr", lower="40", *, calibrated=True, quantity=100, n=50):
    days = min(n, 40)
    evidence = dict(policy_hash='a'*64, labels_hash='b'*64, input_hashes=['c'*64],
                    code_hashes=['d'*64], fee_version='verified-test-fees',
                    trained_until=(NOW-timedelta(days=1)).isoformat(),
                    independent_days=[(NOW.date()-timedelta(days=i+2)).isoformat() for i in range(days)],
                    complete_policy=True, includes_partial=True, fees_final=True)
    return Prediction(policy, "cal-v1", quantity, n, D("100"), D(lower), True, calibrated, 120,
                      days, 'VERIFIED_REPLAY', evidence)


def path(*, fills=(), fees="0", residual=0, bid=None, exit_cost="0", policy="pass"):
    return IntentPath("c1", policy, 100, NOW, fills, D(fees), residual,
                      D(bid) if bid is not None else None, D("0"),
                      D(exit_cost) if exit_cost is not None else None, True,
                      NOW if bid is not None else None, 2 if bid is not None else None)


class EconomicsTests(unittest.TestCase):
    def test_verified_commission_applies_minimum_per_child_order(self):
        fixed = CommissionSchedule(D("0.0008"), D("80"), D("0"), "example-research")
        self.assertEqual(fixed.commission(D("100000")), D("80"))
        self.assertEqual(fixed.commission(D("300000")), D("240"))
        self.assertEqual(fixed.orders_commission([D("50000"), D("50000")]), D("160"))
        self.assertEqual(fixed.commission(D("0")), D("0"))
        with self.assertRaises(ValueError):
            CommissionSchedule(D("NaN"), D("80"), D("0"), "bad")

    def test_T21_time_budget_cannot_extend_original_ttl(self):
        budget = ChannelBudget(20, 3, 1, 1, (5, 5), 2, 2)
        self.assertEqual(budget.required_seconds, 20)
        self.assertTrue(budget.feasible)
        self.assertTrue(budget.can_requote(remaining_seconds=9, requotes_used=1, next_wait_seconds=5))
        self.assertFalse(budget.can_requote(remaining_seconds=8, requotes_used=1, next_wait_seconds=5))
        self.assertFalse(budget.can_requote(remaining_seconds=20, requotes_used=2, next_wait_seconds=0))
        self.assertFalse(replace(budget, submit_p99_seconds=2).feasible)

    def test_T22_seconds_scaling(self):
        self.assertAlmostEqual(reference_sigma_bps(120, 150, 325 * 60), 11.766968108291042)
        self.assertAlmostEqual(reference_sigma_bps(120, 150, 325 * 60),
                               reference_sigma_bps(2 * 60, 150, 325 * 60))
        with self.assertRaises(ValueError):
            reference_sigma_bps(0, 150, 19500)

    def test_T28_cost_ratio_is_diagnostic_not_an_admission_gate(self):
        result = cost_diagnostic(D("30"), D("23.6"), D("5"),
                                 enough_samples=True, sigma_epsilon=D("0.01"))
        self.assertEqual(result.k, D("4.72"))
        self.assertEqual(result.mean_net_bps, D("6.4"))
        result2 = cost_diagnostic(D("30"), D("23.6"), D("0"),
                                  enough_samples=True, sigma_epsilon=D("0.01"))
        self.assertIsNone(result2.k)
        self.assertTrue(prediction_gate(prediction(), policy_id="aggr", version="cal-v1",
                                       quantity=100, min_samples=30, safety_margin=D("20"),
                                       max_holding_seconds=120).allowed)

    def test_prediction_fails_closed_when_uncalibrated_or_context_changed(self):
        args = dict(policy_id="aggr", version="cal-v1", quantity=100, min_samples=30,
                    safety_margin=D("20"), max_holding_seconds=120)
        for p in (None, prediction(calibrated=False), prediction(quantity=200), prediction(n=2),
                  replace(prediction(), max_holding_seconds=300), prediction(lower="20")):
            self.assertFalse(prediction_gate(p, **args).allowed)

    def test_truthy_calibration_strings_are_rejected(self):
        paired = PairedAdvantage("pass", "aggr", 100, "cal-v1", 50, D("30"), D("15"), True, True)
        for field in ("reliable", "calibrated"):
            for invalid_flag in ("false", "true", 1, None):
                with self.assertRaises(ValueError):
                    replace(prediction(), **{field: invalid_flag})
                with self.assertRaises(ValueError):
                    replace(paired, **{field: invalid_flag})

    def test_T29_partial_paths_include_all_fills_fees_and_residual_value(self):
        fills = (PathFill("e1", "buy1", "BUY", 40, D("1000")),
                 PathFill("e2", "buy2", "BUY", 60, D("1001")),
                 PathFill("e3", "sell1", "SELL", 70, D("1005")))
        p = path(fills=fills, fees="240", residual=30, bid="1004", exit_cost="80")
        result = intent_path_value(p)
        self.assertTrue(result.estimable)
        self.assertEqual(result.realized_price_pnl, D("308"))
        self.assertEqual(result.unrealized_price_pnl, D("102"))
        self.assertEqual(result.net_amount, D("90"))
        # Duplicate exec IDs never manufacture cash flow or exposure.
        self.assertEqual(intent_path_value(replace(p, fills=fills + (fills[0],))).net_amount, D("90"))
        self.assertFalse(intent_path_value(replace(p, residual_bid=None)).estimable)
        self.assertFalse(intent_path_value(replace(p, remaining_exit_cost=None)).estimable)
        self.assertFalse(intent_path_value(replace(p, state_reconciled=False)).estimable)
        self.assertFalse(intent_path_value(replace(p, residual_bid_at=None)).estimable)
        self.assertFalse(intent_path_value(replace(p, residual_bid_at=NOW-timedelta(seconds=3))).estimable)
        self.assertFalse(intent_path_value(replace(p, residual_bid_at=NOW+timedelta(seconds=1))).estimable)

    def test_T29_unfilled_intents_keep_fees_and_positive_reference_notional(self):
        self.assertEqual(intent_path_value(path(fees="10")).net_amount, D("-10"))
        self.assertEqual(net_bps(D("-10"), D("100000")), D("-1"))
        with self.assertRaises(ValueError):
            net_bps(D("0"), D("0"))

    def test_partial_exit_cost_allocation_remains_causal_with_late_buy(self):
        fills = (PathFill("b1", "buy1", "BUY", 40, D("1000")),
                 PathFill("s1", "sell1", "SELL", 40, D("1005")),
                 PathFill("b2", "buy2", "BUY", 60, D("1001")))
        result = intent_path_value(path(fills=fills, residual=60, bid="1005"))
        self.assertEqual(result.realized_price_pnl, D("200"))
        self.assertEqual(result.unrealized_price_pnl, D("240"))
        self.assertEqual(result.net_amount, D("440"))
        inverted = path(fills=(fills[1], fills[0], fills[2]), residual=60, bid="1005")
        self.assertFalse(intent_path_value(inverted).estimable)

    def test_T29_none_partial_full_branches_are_not_dropped(self):
        full = path(fills=(PathFill("b", "b1", "BUY", 100, D("1000")),
                           PathFill("s", "s1", "SELL", 100, D("1005"))), fees="160")
        partial = path(fills=(PathFill("p", "p1", "BUY", 40, D("1000")),),
                       fees="80", residual=40, bid="1004", exit_cost="80")
        none = path(fees="10")
        branches = (WeightedPath(D("0.2"), none), WeightedPath(D("0.3"), partial),
                    WeightedPath(D("0.5"), full))
        self.assertEqual(expected_intent_value(branches), D("168"))
        unknown = WeightedPath(D("0.3"), replace(partial, residual_bid=None))
        self.assertIsNone(expected_intent_value((branches[0], unknown, branches[2])))
        with self.assertRaises(ValueError):
            expected_intent_value((WeightedPath(D("1"), none), branches[1]))

    def test_T29_late_alpha_does_not_recharge_waiting_drift(self):
        self.assertEqual(late_midpoint_net_amount(alpha_late_per_share=D("2"), quantity=100,
                                                 remaining_loss_and_fees=D("80")), D("120"))

    def test_paired_switch_requires_mean_bound_and_verified_models(self):
        paired = PairedAdvantage("pass", "aggr", 100, "cal-v1", 50, D("30"), D("15"), True, True)
        args = dict(aggressive=prediction(), passive=prediction("pass"), paired=paired,
                    aggressive_policy_id="aggr", passive_policy_id="pass", version="cal-v1",
                    quantity=100, min_samples=30, net_safety_margin=D("20"),
                    execution_safety_margin=D("10"), max_holding_seconds=120)
        self.assertEqual(choose_execution_policy(**args).policy_id, "pass")
        self.assertEqual(choose_execution_policy(**{**args, "paired": None}).policy_id, "aggr")
        self.assertIsNone(choose_execution_policy(**{**args, "aggressive": None,
                                                     "passive": prediction("pass", calibrated=False)}).policy_id)

    def test_T30_common_effect_samples_are_intersection(self):
        variants = {"h120": {"a": True, "b": True}, "h1200": {"a": False, "b": True},
                    "enhanced": {"b": True, "c": True}, "l1": {"a": True, "b": True, "c": False}}
        self.assertEqual(common_valid_sample(variants), ("b",))


if __name__ == "__main__":
    unittest.main()
