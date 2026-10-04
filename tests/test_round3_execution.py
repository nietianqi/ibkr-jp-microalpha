"""Round-3 regressions for EXE-12 and RPT-06 on the offline ledger."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from ibkr_microalpha.domain import Quote, Side
from ibkr_microalpha.economics import CommissionSchedule
from ibkr_microalpha.execution import ExecutionBook, ExecutionError
from ibkr_microalpha.reporting import IntentLedger

T = datetime(2026, 10, 2, 1, tzinfo=timezone.utc)
FEES = CommissionSchedule(D("0"), D("1"), D("0"), "round3")


class RequestRecoveryTests(unittest.TestCase):
    def populated(self):
        book = ExecutionBook(daily_request_budget=1)
        book.reconcile({}, [], [], T, complete=True, ownership_confirmed=True)
        buy = book.submit("first", "S", Side.BUY, 100, D("100"), T)
        book.drain_commands(T)
        book.fill("buy", buy.order_id, 100, D("100"), T)
        sell = book.submit("risk", "S", Side.SELL, 20, D("99"), T, emergency=True)
        book.drain_commands(T)
        book.cancel(sell.order_id, T)
        book.drain_commands(T)
        book._query(T)
        book.drain_commands(T)
        return book

    def reopen(self, book):
        self.assertFalse(book.connected)
        self.assertFalse(book.commands)
        book.reconnect(T)
        self.assertTrue(book.reconcile({"S": 100}, [], [], T, complete=True, ownership_confirmed=True))

    def check_budget(self, restored):
        self.reopen(restored)
        buy = restored.submit("blocked", "OTHER", Side.BUY, 100, D("100"), T)
        self.assertEqual(restored.drain_commands(T), [])
        self.assertIsNone(buy.submitted_at)
        risk = restored.submit("still-risk", "S", Side.SELL, 10, D("99"), T, emergency=True)
        self.assertEqual([c.order_id for c in restored.drain_commands(T)], [risk.order_id])

    def test_snapshot_and_journal_preserve_exhausted_budget_and_all_counts(self):
        original = self.populated()
        for restored in (ExecutionBook.from_snapshot(json.loads(json.dumps(original.snapshot()))),
                         ExecutionBook.from_journal(original.journal)):
            with self.subTest(kind=type(restored).__name__):
                self.assertEqual(restored.daily_request_budget, 1)
                self.assertEqual(restored.routine_requests_sent, 1)
                self.assertEqual(dict(restored.sent_counts), {"SUBMIT": 2, "CANCEL": 1, "QUERY": 1})
                self.assertEqual(restored.request_day, T.astimezone(timezone(timedelta(hours=9))).date())
                self.check_budget(restored)

    def legacy(self, original):
        state = json.loads(json.dumps(original.snapshot()))
        state["version"] = 1
        state.pop("request_policy", None)
        state["journal"] = [e for e in state["journal"] if e["kind"] not in ("REQUEST_POLICY", "REQUEST_DAY")]
        for sequence, event in enumerate(state["journal"], 1):
            event["sequence"] = sequence
            if event["kind"] == "COMMAND_SENT":
                event["payload"].pop("risk", None)
                event["payload"].pop("request_day", None)
        return state

    def test_legacy_restore_without_explicit_policy_blocks_routine_only(self):
        state = self.legacy(self.populated())
        for book in (ExecutionBook.from_snapshot(state), ExecutionBook.from_journal(state["journal"])):
            self.check_budget(book)

    def test_explicit_frozen_policy_migrates_complete_legacy_counts(self):
        state = self.legacy(self.populated())
        for book in (ExecutionBook.from_snapshot(state, daily_request_budget=1),
                     ExecutionBook.from_journal(state["journal"], daily_request_budget=1)):
            self.assertEqual(book.routine_requests_sent, 1)
            self.assertEqual(dict(book.sent_counts), {"SUBMIT": 2, "CANCEL": 1, "QUERY": 1})
            self.check_budget(book)

    def test_sent_events_record_risk_and_query_without_order_id(self):
        book = self.populated()
        events = [e["payload"] for e in book.journal if e["kind"] == "COMMAND_SENT"]
        self.assertEqual([e["risk"] for e in events], [False, True, True, True])
        self.assertIsNone(events[-1]["order_id"])

    def test_migrated_legacy_policy_survives_a_second_journal_restore(self):
        state = self.legacy(self.populated())
        migrated = ExecutionBook.from_snapshot(state, daily_request_budget=1)
        restored = ExecutionBook.from_journal(migrated.journal)
        self.assertEqual(restored.daily_request_budget, 1)
        self.assertEqual(restored.routine_requests_sent, 1)
        self.check_budget(restored)

    def test_corrupt_snapshot_counter_cannot_replenish_budget(self):
        state = self.populated().snapshot()
        state["request_policy"]["routine_requests_sent"] = 0
        with self.assertRaises(ExecutionError):
            ExecutionBook.from_snapshot(state)

    def test_new_trading_day_does_not_silently_refill_budget(self):
        book = self.populated()
        book.reconcile({"S": 100}, [], [], T, complete=True, ownership_confirmed=True)
        tomorrow = T + timedelta(days=1)
        routine = book.submit("next-day", "OTHER", Side.BUY, 100, D("100"), tomorrow)
        risk = book.submit("next-day-risk", "S", Side.SELL, 10, D("99"), tomorrow, emergency=True)
        self.assertEqual([c.order_id for c in book.drain_commands(tomorrow)], [risk.order_id])
        self.assertIsNone(routine.submitted_at)
        self.assertFalse(book.request_policy_verified)


class ReopenedIntentTests(unittest.TestCase):
    def setUp(self):
        self.book = ExecutionBook()
        self.book.reconcile({}, [], [], T, complete=True, ownership_confirmed=True)
        self.ledger = IntentLedger()
        self.sequence = 0

    def order(self, intent, side, quantity, price):
        self.sequence += 1
        at = T + timedelta(seconds=self.sequence)
        order = self.book.submit(f"order-{self.sequence}", "S", side, quantity, D(price), at,
                                 emergency=side == Side.SELL)
        if side == Side.BUY:
            self.ledger.open(intent, "S", order.order_id, at, quantity)
        else:
            self.ledger.link_exit("S", order.order_id, intent_id=intent)
        self.book.drain_commands(at)
        self.book.fill(f"fill-{self.sequence}", order.order_id, quantity, D(price), at)
        self.book.commission(f"fill-{self.sequence}", D("1"))
        self.ledger.reconcile(self.book, at)
        return order

    def closed(self):
        buy = self.order("old", Side.BUY, 100, "100")
        sell = self.order("old", Side.SELL, 100, "101")
        self.book.reconcile({}, [], [], T + timedelta(seconds=3), complete=True, ownership_confirmed=True)
        self.ledger.close("S", T + timedelta(seconds=3), self.book, FEES)
        return buy, sell

    def bust(self, sell):
        self.book.fill("bust", sell.order_id, 0, sell.limit_price, T + timedelta(seconds=10),
                       correction_of="fill-2")
        self.book.commission("bust", D("0"))
        # A busted terminal sell remains possible exposure until a complete
        # broker barrier proves it is absent. Reopening never releases it early.
        self.assertEqual(self.book.sellable_quantity("S"), self.book.positions["S"].quantity - 100)
        self.book.reconcile({"S": self.book.positions["S"].quantity}, [], [], T + timedelta(seconds=10),
                            complete=True, ownership_confirmed=True)
        self.ledger.reconcile(self.book, T + timedelta(seconds=10))

    def test_sell_bust_reopens_original_intent_and_links_repair_exit(self):
        _, sell = self.closed()
        self.bust(sell)
        self.assertEqual(self.ledger.order_to_intent[sell.order_id], "old")
        self.assertEqual(self.ledger.current("S").intent_id, "old")
        self.assertIsNone(self.ledger.intents["old"].closed_at)
        self.assertEqual(self.ledger.exit_allocations("S", 100, self.book), [("old", 100)])
        self.order("old", Side.SELL, 100, "102")
        self.ledger.close("S", T + timedelta(seconds=11), self.book, FEES)
        rows = self.ledger.outcomes(self.book, FEES, at=T + timedelta(seconds=11))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "CLOSED")
        self.assertEqual(D(rows[0]["net_amount"]), self.book.positions["S"].realized_net_pnl)

    def test_old_bust_and_later_intent_partition_confirmed_residuals(self):
        _, sell = self.closed()
        self.order("new", Side.BUY, 50, "103")
        self.bust(sell)
        allocations = self.ledger.exit_allocations("S", 150, self.book)
        self.assertEqual(allocations, [("old", 100), ("new", 50)])
        self.assertEqual(self.ledger.current("S").intent_id, "new")
        for identity, quantity in allocations:
            self.order(identity, Side.SELL, quantity, "100")
        self.ledger.close("S", T + timedelta(seconds=12), self.book, FEES)
        rows = self.ledger.outcomes(self.book, FEES, at=T + timedelta(seconds=12))
        self.assertEqual([r["status"] for r in rows], ["CLOSED", "CLOSED"])
        self.assertEqual([r["sold"] for r in rows], [100, 50])
        self.assertEqual(sum((D(r["net_amount"]) for r in rows), D(0)),
                         self.book.positions["S"].realized_net_pnl)

    def test_partial_existing_exit_only_allocates_unreserved_residual(self):
        buy = self.order("old", Side.BUY, 100, "100")
        sell = self.book.submit("working", "S", Side.SELL, 40, D("101"), T + timedelta(seconds=2), emergency=True)
        self.ledger.link_exit("S", sell.order_id, intent_id="old")
        self.book.drain_commands(T + timedelta(seconds=2))
        self.book.fill("partial", sell.order_id, 10, D("101"), T + timedelta(seconds=2))
        self.ledger.reconcile(self.book, T + timedelta(seconds=2))
        self.assertEqual(self.book.sellable_quantity("S"), 60)
        self.assertEqual(self.ledger.exit_allocations("S", 60, self.book), [("old", 60)])

    def test_buy_downward_correction_does_not_use_original_target_size(self):
        buy = self.order("old", Side.BUY, 100, "100")
        self.book.fill("buy-correction", buy.order_id, 40, D("100"), T + timedelta(seconds=2),
                       correction_of="fill-1")
        self.ledger.reconcile(self.book, T + timedelta(seconds=2))
        self.assertEqual(self.ledger.exit_allocations("S", 40, self.book), [("old", 40)])

    def test_recovery_rebuilds_ownership_before_bust_and_repair(self):
        _, sell = self.closed()
        self.book = ExecutionBook.from_snapshot(self.book.snapshot())
        self.book.reconnect(T + timedelta(seconds=4))
        self.book.reconcile({}, [], [], T + timedelta(seconds=4), complete=True, ownership_confirmed=True)
        self.ledger.order_to_intent.clear()  # reconstructed raw-input registrations remain
        self.ledger.reconcile(self.book, T + timedelta(seconds=4))
        self.bust(sell)
        self.order("old", Side.SELL, 100, "102")
        self.ledger.close("S", T + timedelta(seconds=11), self.book, FEES)
        rows = self.ledger.outcomes(self.book, FEES, at=T + timedelta(seconds=11))
        self.assertEqual([(r["intent_id"], r["status"]) for r in rows], [("old", "CLOSED")])

    def test_position_manager_creates_separately_owned_exit_children(self):
        from tests.test_review_fixes import fixture
        _, sell = self.closed()
        self.order("new", Side.BUY, 50, "103")
        self.bust(sell)
        test = fixture()
        engine = test.engine
        engine.book, engine.intents = self.book, self.ledger
        engine.instruments["S"] = next(iter(engine.instruments.values()))
        at = T + timedelta(seconds=10)
        engine.quotes["S"] = Quote("S", at, D("100"), D("100.1"), 200, 200, "partition",
                                   bid_at=at, ask_at=at)
        before = set(self.book.orders)
        engine._request_exit("S", at, "KILL_SWITCH", True)
        children = [o for identity, o in self.book.orders.items() if identity not in before]
        self.assertEqual([o.quantity for o in children], [100, 50])
        self.assertEqual([self.ledger.order_to_intent[o.order_id] for o in children], ["old", "new"])
        self.assertEqual(sum(o.quantity for o in children), 150)

    def test_net_account_flat_does_not_close_opposing_intent_residuals(self):
        buy, _ = self.closed()
        self.order("new", Side.BUY, 50, "103")
        self.book.fill("old-buy-correction", buy.order_id, 50, D("100"), T + timedelta(seconds=10),
                       correction_of="fill-1")
        self.ledger.reconcile(self.book, T + timedelta(seconds=10))
        self.assertEqual(self.book.positions["S"].quantity, 0)
        self.ledger.close("S", T + timedelta(seconds=11), self.book, FEES)
        self.assertIsNone(self.ledger.intents["old"].closed_at)
        self.assertIsNone(self.ledger.intents["new"].closed_at)
        rows = self.ledger.outcomes(self.book, FEES, at=T + timedelta(seconds=11))
        self.assertEqual([r["status"] for r in rows], ["OPEN", "OPEN"])


if __name__ == "__main__":
    unittest.main()
