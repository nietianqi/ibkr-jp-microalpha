import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ibkr_microalpha.domain import OrderState, Side
from ibkr_microalpha.execution import ExecutionBook, ExecutionError, TokenBucket

D = Decimal
T = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.book = ExecutionBook()
        self.assertTrue(self.book.reconcile({}, [], [], T, complete=True,
                                            ownership_confirmed=True))

    def buy(self, qty=100, *, key="entry", **kwargs):
        return self.book.submit(key, "7203", Side.BUY, qty, D("3000"), T, **kwargs)

    def working(self, qty=100, **kwargs):
        order = self.buy(qty, **kwargs)
        self.book.drain_commands(T)
        self.book.status(order.order_id, "Submitted", 0, qty, T)
        return order

    def synchronize(self, qty, open_orders=(), **kwargs):
        return self.book.reconcile({"7203": qty} if qty else {}, open_orders, [],
                                   T + timedelta(seconds=10), complete=True,
                                   ownership_confirmed=True, **kwargs)

    def test_T01_status_totals_and_duplicate_execution_do_not_double_count(self):
        order = self.working()
        for _ in range(2):
            self.book.status(order.order_id, "Submitted", 30, 70, T)
        self.assertEqual(self.book.positions, {})
        for _ in range(2):
            self.book.fill("x1", order.order_id, 30, D("3000"), T)
        self.assertEqual(self.book.positions["7203"].quantity, 30)
        self.assertEqual(order.filled_quantity, 30)
        self.assertEqual(order.remaining_quantity, 70)
        self.assertEqual(self.book.cash_flow, D("-90000"))

    def test_idempotency_persists_and_parameter_conflict_is_rejected(self):
        order = self.buy()
        self.assertIs(self.buy(), order)
        self.assertEqual(len(self.book.commands), 1)
        with self.assertRaises(ExecutionError):
            self.buy(200)
        restored = ExecutionBook.from_snapshot(json.loads(json.dumps(self.book.snapshot())))
        self.assertEqual(restored.next_order_id, 2)
        self.assertEqual(restored.submit("entry", "7203", Side.BUY, 100, D("3000"), T).order_id, 1)
        self.assertFalse(restored.connected)
        self.assertTrue(restored.reconciling)
        self.assertEqual(list(restored.commands), [])

    def test_T02_explicit_correction_replaces_fill_and_fee(self):
        order = self.working()
        self.book.fill("x1", order.order_id, 30, D("3000"), T)
        self.book.commission("x1", D("30"))
        self.book.fill("x2", order.order_id, 20, D("3001"), T + timedelta(seconds=2),
                       correction_of="x1")
        pos = self.book.positions["7203"]
        self.assertEqual(pos.quantity, 20)
        self.assertEqual(pos.average_price, D("3001"))
        self.assertEqual(pos.entry_time, T)
        self.assertEqual(pos.fees, D("30"))
        self.book.commission("x2", D("20"))
        self.book.commission("x1", D("35"))  # Late obsolete revision cannot overwrite.
        self.assertEqual(self.book.positions["7203"].fees, D("20"))
        self.assertEqual(self.book.cash_flow, D("-60040"))

    def test_T02_conflicting_duplicate_locks_instead_of_counting_twice(self):
        order = self.working()
        self.book.fill("x1", order.order_id, 30, D("3000"), T)
        with self.assertRaises(ExecutionError):
            self.book.fill("x1", order.order_id, 50, D("3000"), T)
        self.assertTrue(self.book.locked)
        self.assertEqual(self.book.positions["7203"].quantity, 30)

    def test_T02_late_buy_and_sell_execution_order_rebuilds_accounting(self):
        order = self.working()
        self.book.fill("buy-late", order.order_id, 50, D("3000"), T + timedelta(seconds=5))
        self.book.cancel(order.order_id, T + timedelta(seconds=6))
        self.book.status(order.order_id, "Cancelled", 50, 0, T + timedelta(seconds=7))
        self.assertTrue(self.synchronize(50))
        sell = self.book.submit("exit", "7203", Side.SELL, 50, D("3010"), T + timedelta(seconds=11))
        self.book.fill("sell", sell.order_id, 50, D("3010"), T + timedelta(seconds=12))
        self.book.fill("buy-correction", order.order_id, 50, D("3001"),
                       T + timedelta(seconds=14), correction_of="buy-late")
        self.assertEqual(self.book.positions["7203"].quantity, 0)
        self.assertEqual(self.book.positions["7203"].realized_pnl, D("450"))

    def test_bust_can_reverse_an_execution_without_fabricating_another_fill(self):
        order = self.working()
        self.book.fill("x1", order.order_id, 30, D("3000"), T)
        self.book.fill("x2", order.order_id, 0, D("3000"), T, correction_of="x1")
        self.assertEqual(self.book.positions["7203"].quantity, 0)
        self.assertEqual(order.filled_quantity, 0)
        self.assertEqual(self.book.cash_flow, 0)

    def test_commission_can_precede_execution_and_never_creates_shares(self):
        order = self.working()
        self.book.commission("x1", D("50"))
        self.assertFalse(self.book.positions)
        self.book.fill("x1", order.order_id, 100, D("3000"), T)
        self.book.commission("x1", D("50"))
        self.assertEqual(self.book.positions["7203"].fees, D("50"))
        self.assertEqual(self.book.cash_flow, D("-300050"))

    def test_T03_expiry_cancels_unfilled_remainder_preserving_first_fill_risk(self):
        order = self.working(candidate_expires_at=T + timedelta(seconds=20),
                             entry_score=-0.5, score_version="score-v1",
                             stop_distance=D("10"), max_holding_seconds=300)
        self.book.fill("x1", order.order_id, 20, D("3000"), T + timedelta(seconds=2))
        self.book.fill("x2", order.order_id, 30, D("2995"), T + timedelta(seconds=5))
        self.book.check_timeouts(T + timedelta(seconds=20))
        self.assertEqual(order.state, OrderState.CANCEL_PENDING)
        pos = self.book.positions["7203"]
        self.assertEqual(pos.quantity, 50)
        self.assertEqual(pos.entry_time, T + timedelta(seconds=2))
        self.assertEqual(pos.stop_price, D("2990"))
        self.assertEqual(pos.max_holding_seconds, 300)
        self.assertEqual(pos.entry_score, -0.5)
        self.assertEqual([c.kind for c in self.book.commands], ["CANCEL"])

    def test_T04_cancel_pending_fill_keeps_exposure_and_blocks_replacement(self):
        order = self.working()
        self.book.cancel(order.order_id, T + timedelta(seconds=1))
        self.book.fill("x1", order.order_id, 40, D("3000"), T + timedelta(seconds=2))
        self.assertEqual(order.state, OrderState.CANCEL_PENDING)
        self.assertEqual(order.possible_remaining, 60)
        with self.assertRaises(ExecutionError):
            self.book.submit("replace", "7203", Side.BUY, 60, D("3000"),
                             T + timedelta(seconds=3), replaces=order.order_id)
        self.book.status(order.order_id, "Cancelled", 40, 0, T + timedelta(seconds=4))
        with self.assertRaises(ExecutionError):
            self.book.submit("replace", "7203", Side.BUY, 60, D("3000"),
                             T + timedelta(seconds=5), replaces=order.order_id)
        self.assertTrue(self.synchronize(40))
        replacement = self.book.submit("replace", "7203", Side.BUY, 60, D("3000"),
                                       T + timedelta(seconds=11), replaces=order.order_id)
        self.assertEqual(replacement.replaces, order.order_id)

    def test_cancelled_report_without_execution_snapshot_reserves_exposure(self):
        order = self.working()
        self.book.cancel(order.order_id, T + timedelta(seconds=1))
        self.book.status(order.order_id, "Cancelled", 0, 0, T + timedelta(seconds=2))
        self.assertEqual(order.possible_remaining, 100)
        self.assertEqual(self.book.active_orders(), [order])
        self.assertTrue(self.synchronize(0))
        self.assertFalse(self.book.active_orders())

    def test_T05_submit_timeout_never_resubmits(self):
        order = self.buy()
        self.book.drain_commands(T)
        self.assertEqual(self.book.check_timeouts(T + timedelta(seconds=6)), [order.order_id])
        self.assertEqual(order.state, OrderState.UNKNOWN)
        self.assertTrue(self.book.locked)
        self.assertEqual([c.kind for c in self.book.drain_commands(T + timedelta(seconds=6))], ["QUERY"])
        self.assertIs(self.buy(), order)
        self.assertEqual(self.book.check_timeouts(T + timedelta(seconds=30)), [])
        self.assertEqual(list(self.book.commands), [])

    def test_T05_cancel_timeout_remains_possible_exposure(self):
        order = self.working()
        self.book.cancel(order.order_id, T + timedelta(seconds=1))
        self.book.drain_commands(T + timedelta(seconds=1))
        self.book.check_timeouts(T + timedelta(seconds=7))
        self.assertEqual(order.state, OrderState.UNKNOWN)
        self.assertEqual(order.possible_remaining, 100)
        self.assertEqual([c.kind for c in self.book.commands], ["QUERY"])

    def test_T06_disconnect_then_reconnect_requires_complete_owned_snapshot(self):
        order = self.working()
        self.book.disconnect(T + timedelta(seconds=1))
        self.assertEqual(order.state, OrderState.UNKNOWN)
        self.book.reconnect(T + timedelta(seconds=2))
        self.assertTrue(self.book.reconciling)
        with self.assertRaises(ExecutionError):
            self.book.submit("new", "6758", Side.BUY, 100, D("1000"), T)
        self.assertFalse(self.book.reconcile({}, [], [], T, complete=True))
        self.assertTrue(self.book.reconciling)
        self.assertTrue(self.synchronize(0, next_order_id=100))
        self.assertFalse(self.book.reconciling)
        self.assertFalse(self.book.locked)
        self.assertEqual(self.book.next_order_id, 100)
        self.assertFalse(self.book.commands)

    def test_reconcile_order_totals_and_positions_must_both_match(self):
        order = self.working()
        self.book.fill("x1", order.order_id, 30, D("3000"), T)
        row = {"order_id": order.order_id, "filled": 40, "remaining": 60}
        self.assertFalse(self.synchronize(30, [row]))
        self.assertTrue(self.book.locked)
        row.update(filled=30, remaining=70)
        # A lower cumulative requires explicit reconciliation; stale status alone cannot lower it.
        self.assertFalse(self.synchronize(20, [row]))
        self.assertTrue(self.book.reconciling)

    def test_unowned_order_or_negative_broker_position_keeps_reconciliation_locked(self):
        self.assertFalse(self.book.reconcile({}, [{"order_id": 999}], [], T,
                                             complete=True, ownership_confirmed=True))
        self.assertTrue(self.book.locked)
        self.assertEqual(self.book.unmanaged_orders[0]["order_id"], 999)
        self.assertFalse(self.book.reconcile({"7203": -100}, [], [], T,
                                             complete=True, ownership_confirmed=True))

    def test_T11_emergency_exit_reserves_possible_sells_and_late_buys(self):
        order = self.working()
        self.book.fill("x1", order.order_id, 40, D("3000"), T)
        self.book.cancel(order.order_id, T + timedelta(seconds=1))
        with self.assertRaises(ExecutionError):
            self.book.submit("normal", "7203", Side.SELL, 40, D("2990"), T)
        sell = self.book.submit("risk-exit", "7203", Side.SELL, 40, D("2990"), T,
                                emergency=True)
        with self.assertRaises(ExecutionError):
            self.book.submit("double-exit", "7203", Side.SELL, 1, D("2990"), T,
                             emergency=True)
        self.book.fill("sell-x", sell.order_id, 20, D("2990"), T + timedelta(seconds=2))
        self.book.fill("buy-late", order.order_id, 30, D("3000"), T + timedelta(seconds=3))
        self.assertEqual(self.book.positions["7203"].quantity, 50)
        self.assertEqual(self.book.sellable_quantity("7203"), 30)
        self.book.cancel(sell.order_id, T + timedelta(seconds=4))
        self.assertEqual(sell.state, OrderState.CANCEL_PENDING)
        self.assertEqual(self.book.sellable_quantity("7203"), 30)

    def test_T11_cancel_pending_sell_remains_reserved_until_reconciled(self):
        order = self.working()
        self.book.fill("buy", order.order_id, 100, D("3000"), T)
        self.assertTrue(self.synchronize(100))
        sell = self.book.submit("exit", "7203", Side.SELL, 100, D("2990"), T)
        self.book.drain_commands(T + timedelta(seconds=11))
        self.book.cancel(sell.order_id, T + timedelta(seconds=12))
        self.book.fill("sell", sell.order_id, 40, D("2990"), T + timedelta(seconds=13))
        self.assertEqual(sell.state, OrderState.CANCEL_PENDING)
        self.assertEqual(self.book.sellable_quantity("7203"), 0)
        self.book.status(sell.order_id, "Cancelled", 40, 0, T + timedelta(seconds=14))
        self.assertEqual(self.book.sellable_quantity("7203"), 0)
        self.assertTrue(self.synchronize(60))
        self.assertEqual(self.book.sellable_quantity("7203"), 60)

    def test_T16_reserved_request_capacity_and_risk_request_priority(self):
        book = ExecutionBook(request_rate=1, request_capacity=3, reserved_risk_tokens=1)
        self.assertTrue(book.reconcile({}, [], [], T, complete=True,
                                       ownership_confirmed=True))
        orders = [book.submit(f"entry-{i}", str(i), Side.BUY, 100, D("1000"), T)
                  for i in range(3)]
        self.assertEqual(len(book.drain_commands(T)), 2)
        self.assertEqual(len(book.commands), 1)
        book.cancel(orders[0].order_id, T)
        commands = book.drain_commands(T)
        self.assertEqual([c.kind for c in commands], ["CANCEL"])
        self.assertEqual(len(book.commands), 1)
        self.assertEqual(book.drain_commands(T), [])
        self.assertEqual(len(book.drain_commands(T + timedelta(seconds=2))), 1)

    def test_T16_unresolved_inactive_is_unknown_definitive_reject_is_bounded(self):
        order = self.working()
        self.book.status(order.order_id, "Inactive", 0, 100, T)
        self.assertEqual(order.state, OrderState.UNKNOWN)
        self.assertTrue(self.book.locked)
        self.book.reject(order.order_id, T, "definitive broker rejection")
        self.assertEqual(order.state, OrderState.REJECTED)
        self.assertEqual(order.possible_remaining, 0)
        self.assertFalse(any(c.kind in {"SUBMIT", "CANCEL"} for c in self.book.commands))
        with self.assertRaises(ExecutionError):
            self.book.submit("later", "7203", Side.BUY, 100, D("3000"), T)

    def test_overfill_is_recorded_as_real_exposure_and_locks(self):
        order = self.working()
        self.book.fill("actual-overfill", order.order_id, 120, D("3000"), T)
        self.assertEqual(self.book.positions["7203"].quantity, 120)
        self.assertEqual(order.state, OrderState.UNKNOWN)
        self.assertTrue(self.book.locked)
        with self.assertRaises(ExecutionError):
            self.book.submit("risk", "7203", Side.SELL, 120, D("2990"), T,
                             emergency=True)

    def test_journal_and_snapshot_restore_corrected_ledger_without_request_replay(self):
        order = self.working(stop_distance=D("10"), max_holding_seconds=300)
        self.book.fill("x1", order.order_id, 50, D("3000"), T)
        self.book.fill("x2", order.order_id, 40, D("2998"), T + timedelta(seconds=1),
                       correction_of="x1")
        self.book.commission("x2", D("40"))
        self.book.cancel(order.order_id, T + timedelta(seconds=2))
        for restored in (ExecutionBook.from_snapshot(self.book.snapshot()),
                         ExecutionBook.from_journal(self.book.journal)):
            self.assertEqual(restored.positions["7203"].quantity, 40)
            self.assertEqual(restored.positions["7203"].average_price, D("2998"))
            self.assertEqual(restored.positions["7203"].stop_price, D("2990"))
            self.assertEqual(restored.positions["7203"].entry_time, T)
            self.assertEqual(restored.cash_flow, D("-119960"))
            self.assertEqual(restored.orders[order.order_id].state, OrderState.UNKNOWN)
            self.assertEqual(restored.next_order_id, 2)
            self.assertTrue(restored.locked)
            self.assertFalse(restored.connected)
            self.assertFalse(restored.commands)

    def test_unsent_expired_entry_never_becomes_a_broker_request(self):
        order = self.buy(candidate_expires_at=T + timedelta(seconds=2))
        self.assertEqual(self.book.drain_commands(T + timedelta(seconds=2)), [])
        self.assertEqual(order.state, OrderState.EXPIRED)
        self.assertEqual(order.possible_remaining, 0)
        self.assertIs(self.buy(candidate_expires_at=T + timedelta(seconds=2)), order)

    def test_validation_rejects_fractional_shares_nonfinite_and_naive_time(self):
        with self.assertRaises(ExecutionError):
            self.buy(D("0.5"))
        with self.assertRaises(ValueError):
            self.book.submit("entry", "7203", Side.BUY, 100, D("NaN"), T)
        with self.assertRaises(ValueError):
            self.book.submit("entry", "7203", Side.BUY, 100, D("3000"), T.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            TokenBucket(rate=float("inf"))
        with self.assertRaises(ExecutionError):
            self.buy(emergency=True)

    def test_startup_requires_complete_account_reconciliation(self):
        book = ExecutionBook()
        self.assertFalse(book.reconciled)
        with self.assertRaises(ExecutionError):
            book.submit("entry", "7203", Side.BUY, 100, D("3000"), T)
        self.assertTrue(book.reconcile({}, [], [], T, complete=True, ownership_confirmed=True))
        self.assertTrue(book.reconciled)

    def test_duplicate_receipt_timestamp_preserves_original_fill_time(self):
        order = self.working()
        original = self.book.fill("x1", order.order_id, 20, D("3000"), T)
        duplicate = self.book.fill("x1", order.order_id, 20, D("3000"),
                                   T + timedelta(seconds=5))
        self.assertIs(original, duplicate)
        self.assertEqual(self.book.positions["7203"].entry_time, T)
        self.assertEqual(order.filled_notional, D("60000"))

    def test_broker_execution_before_draining_queue_removes_stale_submit(self):
        order = self.buy()
        self.book.fill("x1", order.order_id, 20, D("3000"), T)
        self.assertFalse(self.book.commands)
        self.book.cancel(order.order_id, T + timedelta(seconds=1))
        self.assertEqual(order.state, OrderState.CANCEL_PENDING)
        self.assertEqual([c.kind for c in self.book.commands], ["CANCEL"])

    def test_position_discrepancy_forbids_unverified_emergency_sells(self):
        order = self.working()
        self.book.fill("x1", order.order_id, 100, D("3000"), T)
        self.assertFalse(self.synchronize(0))
        with self.assertRaises(ExecutionError):
            self.book.submit("risk-exit", "7203", Side.SELL, 100, D("2990"), T,
                             emergency=True)

    def test_late_earlier_execution_cannot_widen_stop_or_restart_timer(self):
        order = self.working(stop_distance=D("10"))
        self.book.fill("first-received", order.order_id, 20, D("3000"), T + timedelta(seconds=5))
        self.book.fill("earlier-late", order.order_id, 20, D("2990"), T + timedelta(seconds=2))
        self.assertEqual(self.book.positions["7203"].entry_time, T + timedelta(seconds=2))
        self.assertEqual(self.book.positions["7203"].stop_price, D("2990"))
        restored = ExecutionBook.from_snapshot(self.book.snapshot())
        self.assertEqual(restored.positions["7203"].stop_price, D("2990"))

    def test_malformed_broker_callbacks_lock_account(self):
        order = self.working()
        with self.assertRaises(ExecutionError):
            self.book.status(order.order_id, "Submitted", D("NaN"), 100, T)
        self.assertTrue(self.book.locked)
        self.assertEqual(order.state, OrderState.UNKNOWN)
        with self.assertRaises(ExecutionError):
            self.book.fill("bad", order.order_id, 20, D("NaN"), T)
        self.assertFalse(self.book.positions)

    def test_unexpected_broker_sell_quantity_blocks_additional_emergency_exit(self):
        order = self.working()
        self.book.fill("buy", order.order_id, 100, D("3000"), T)
        self.assertTrue(self.synchronize(100))
        sell = self.book.submit("sell", "7203", Side.SELL, 50, D("2990"), T)
        self.book.status(sell.order_id, "Submitted", 0, 100, T)
        with self.assertRaises(ExecutionError):
            self.book.submit("risk", "7203", Side.SELL, 50, D("2990"), T,
                             emergency=True)

    def test_queued_risk_sell_is_rechecked_after_correction_reduces_holdings(self):
        order = self.working()
        self.book.fill("buy", order.order_id, 100, D("3000"), T)
        self.assertTrue(self.synchronize(100))
        sell = self.book.submit("risk", "7203", Side.SELL, 100, D("2990"), T,
                                emergency=True)
        self.book.fill("corrected", order.order_id, 50, D("3000"), T,
                       correction_of="buy")
        self.assertFalse(any(command.kind == "SUBMIT" for command in
                             self.book.drain_commands(T + timedelta(seconds=11))))
        self.assertEqual(sell.state, OrderState.CANCELLED)
        self.assertTrue(sell.reconciled)
        self.assertEqual(self.book.sellable_quantity("7203"), 50)

    def test_known_fees_are_separate_per_child_order_and_replace_corrections(self):
        buy = self.working()
        self.book.fill("buy-x", buy.order_id, 100, D("3000"), T)
        self.book.commission("buy-x", D("200"))
        self.assertTrue(self.synchronize(100))
        sell = self.book.submit("exit", "7203", Side.SELL, 100, D("3010"), T)
        self.book.fill("sell-x", sell.order_id, 100, D("3010"), T)
        self.assertEqual(self.book.order_fees(buy.order_id), D("200"))
        self.assertEqual(self.book.order_fees(sell.order_id), D("0"))
        self.book.fill("sell-corrected", sell.order_id, 100, D("3009"), T,
                       correction_of="sell-x")
        self.book.commission("sell-x", D("80"))
        self.assertEqual(self.book.order_fees(sell.order_id), D("80"))
        self.book.commission("sell-corrected", D("70"))
        self.book.commission("sell-x", D("85"))
        self.assertEqual(self.book.order_fees(sell.order_id), D("70"))
        self.assertEqual(self.book.positions["7203"].fees, D("270"))
        with self.assertRaises(KeyError):
            self.book.order_fees(999)

    def test_forced_stop_promotes_queued_ordinary_sell_using_reserved_capacity(self):
        self.book = ExecutionBook(request_rate=1, request_capacity=3, reserved_risk_tokens=1)
        self.assertTrue(self.synchronize(0))
        buy = self.working()
        self.book.fill("buy", buy.order_id, 100, D("3000"), T)
        self.assertTrue(self.synchronize(100))
        self.book.submit("another-entry", "6758", Side.BUY, 100, D("1000"), T)
        sell = self.book.submit("ordinary-exit", "7203", Side.SELL, 100, D("2990"), T)
        dispatched = self.book.drain_commands(T)
        self.assertEqual(len(dispatched), 1)
        self.assertIsNone(sell.submitted_at)
        self.assertEqual(self.book.drain_commands(T), [])
        self.book.prioritize_risk(sell.order_id, T)
        self.book.prioritize_risk(sell.order_id, T)  # Repeat stop signal is idempotent.
        forced = self.book.drain_commands(T)
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0].order_id, sell.order_id)
        self.assertTrue(forced[0].risk)
        self.assertEqual(sell.quantity, 100)
        self.assertEqual(sell.limit_price, D("2990"))
        self.assertTrue(sell.emergency)
        self.assertEqual(sum(event["kind"] == "RISK_PRIORITY" for event in self.book.journal), 1)
        self.book.prioritize_risk(sell.order_id, T)
        self.assertFalse(self.book.commands)

    def test_risk_priority_cannot_promote_buy(self):
        order = self.buy()
        with self.assertRaises(ExecutionError):
            self.book.prioritize_risk(order.order_id, T)


if __name__ == "__main__":
    unittest.main()
