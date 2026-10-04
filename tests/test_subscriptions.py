import unittest
from datetime import datetime, timedelta

from ibkr_microalpha.market import JST
from ibkr_microalpha.subscriptions import SubscriptionScheduler, VersionRequirements


START = datetime(2026, 10, 2, 9, tzinfo=JST)


class SubscriptionTests(unittest.TestCase):
    def test_t18_quota_pins_tenure_and_same_contract_guard(self):
        scheduler = SubscriptionScheduler(2, min_tenure_seconds=120)
        self.assertEqual(scheduler.plan(START, ("A", "B", "C")).subscribe, ("A", "B"))
        sticky = scheduler.plan(START + timedelta(seconds=10), ("C", "D"))
        self.assertEqual(set(sticky.active), {"A", "B"})
        pinned = scheduler.plan(START + timedelta(seconds=11), ("D",), positions=("C",), orders=("A",))
        self.assertEqual(set(pinned.active), {"A", "C"})
        guarded = scheduler.plan(START + timedelta(seconds=12), ("B",), positions=("A",), orders=("B",))
        self.assertEqual(guarded.reasons["B"], "REQUEST_GUARD")
        self.assertEqual(set(guarded.active), {"A", "C"})
        eligible = scheduler.plan(START + timedelta(seconds=15), (), positions=("A",), orders=("B",))
        self.assertEqual(set(eligible.active), {"A", "B"})
        self.assertLessEqual(len(eligible.active), 2)

    def test_failed_subscription_retry_retains_guard(self):
        scheduler = SubscriptionScheduler(1)
        scheduler.plan(START, ("A",))
        scheduler.subscription_failed("A")
        result = scheduler.plan(START + timedelta(seconds=14), ("A",))
        self.assertEqual(result.subscribe, ())
        self.assertEqual(result.reasons["A"], "REQUEST_GUARD")
        self.assertEqual(scheduler.plan(START + timedelta(seconds=15), ("A",)).subscribe, ("A",))

    def test_t17_t25_readiness_is_per_version_full_feature_windows(self):
        scheduler = SubscriptionScheduler(1)
        scheduler.plan(START, ("A",))
        requirements = VersionRequirements("enhanced-ti60", "TBT", {"ti_10": 10, "ti_60": 60})
        scheduler.record_data("A", START + timedelta(seconds=30), source="TBT", version="enhanced-ti60",
                              feature_windows={"ti_10": 10, "ti_60": 30}, valid=True, coverage=1, synchronized=True)
        self.assertFalse(scheduler.readiness("A", START + timedelta(seconds=30), requirements))
        scheduler.record_data("A", START + timedelta(seconds=60), source="TBT", version="enhanced-ti60",
                              feature_windows={"ti_10": 10}, valid=True, coverage=1, synchronized=True)
        self.assertFalse(scheduler.readiness("A", START + timedelta(seconds=60), requirements))
        scheduler.record_data("A", START + timedelta(seconds=61), source="TBT", version="enhanced-ti60",
                              feature_windows={"ti_10": 10, "ti_60": 60}, valid=True, coverage=1, synchronized=True)
        self.assertTrue(scheduler.readiness("A", START + timedelta(seconds=61), requirements))
        self.assertFalse(scheduler.readiness("A", START + timedelta(seconds=61),
                                             VersionRequirements("another-version", "TBT", {"ti_10": 10})))
        self.assertFalse(scheduler.readiness("A", START + timedelta(seconds=61),
                                             VersionRequirements("enhanced-ti60", "OTHER", {"ti_10": 10})))

    def test_elapsed_time_without_coverage_never_ready(self):
        scheduler = SubscriptionScheduler(1)
        scheduler.plan(START, ("A",))
        requirements = VersionRequirements("micro", "TBT", {"ti_10": 10})
        self.assertFalse(scheduler.readiness("A", START + timedelta(minutes=5), requirements))
        scheduler.record_data("A", START + timedelta(minutes=5), source="TBT", version="micro",
                              feature_windows={}, valid=True, coverage=1, synchronized=True)
        self.assertFalse(scheduler.readiness("A", START + timedelta(minutes=5), requirements))

    def test_t07_invalid_stale_low_coverage_unsynchronized_reset_requires_new_proof(self):
        scheduler = SubscriptionScheduler(1)
        scheduler.plan(START, ("A",))
        requirements = VersionRequirements("micro", "TBT", {"ti_10": 10})
        stamp = START + timedelta(seconds=10)
        for valid, coverage, synchronized in ((False, 1, True), (True, .5, True), (True, 1, False)):
            scheduler.record_data("A", stamp, source="TBT", version="micro", feature_windows={"ti_10": 10},
                                  valid=valid, coverage=coverage, synchronized=synchronized)
            self.assertFalse(scheduler.readiness("A", stamp, requirements))
        scheduler.record_data("A", stamp, source="TBT", version="micro", feature_windows={"ti_10": 10},
                              valid=True, coverage=1, synchronized=True)
        self.assertFalse(scheduler.readiness("A", stamp, requirements))
        recovered = stamp + timedelta(seconds=10)
        scheduler.record_data("A", recovered, source="TBT", version="micro", feature_windows={"ti_10": 10},
                              valid=True, coverage=1, synchronized=True)
        self.assertTrue(scheduler.readiness("A", recovered, requirements))
        self.assertFalse(scheduler.readiness("A", stamp - timedelta(seconds=1), requirements))
        self.assertFalse(scheduler.readiness("A", recovered + timedelta(seconds=3), requirements))
        scheduler.reset("A")
        self.assertFalse(scheduler.readiness("A", recovered, requirements))
        scheduler.record_data("A", recovered + timedelta(seconds=1), source="TBT", version="micro",
                              feature_windows={"ti_10": 10}, valid=True, coverage=1, synchronized=True)
        self.assertFalse(scheduler.readiness("A", recovered + timedelta(seconds=1), requirements))

    def test_pinned_overflow_reported_without_eviction(self):
        scheduler = SubscriptionScheduler(1)
        scheduler.plan(START, ("A",))
        plan = scheduler.plan(START + timedelta(seconds=1), (), positions=("A", "B"))
        self.assertEqual(plan.active, ("A",))
        self.assertEqual(plan.subscribe, ())
        self.assertEqual(plan.reasons["B"], "PINNED_QUOTA_EXCEEDED")

    def test_verified_quota_causal_order_and_coverage_reporting(self):
        self.assertEqual(SubscriptionScheduler.quota_from_lines(100, 5), 5)
        self.assertEqual(SubscriptionScheduler.quota_from_lines(100, 3), 3)
        with self.assertRaises(ValueError):
            SubscriptionScheduler.quota_from_lines(100, None)
        with self.assertRaises(ValueError):
            SubscriptionScheduler(1, request_guard_seconds=14)
        scheduler = SubscriptionScheduler(1)
        scheduler.plan(START, ("A",))
        with self.assertRaises(ValueError):
            scheduler.plan(START - timedelta(seconds=1), ("A",))
        with self.assertRaises(ValueError):
            scheduler.plan(datetime(2026, 10, 2, 9), ("A",))
        requirements = VersionRequirements("micro", "TBT", {"ti_10": 10})
        scheduler.record_candidate("A", START, requirements)
        self.assertEqual(scheduler.ready_candidate_ratio, 0)


if __name__ == "__main__":
    unittest.main()
