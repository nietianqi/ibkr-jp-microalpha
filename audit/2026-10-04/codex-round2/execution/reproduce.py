"""Round-2 probes using only current offline APIs and current test fixtures."""
import json
import sys
from dataclasses import replace
from pathlib import Path
from decimal import Decimal as D

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from tests.test_review_fixes import fixture, at
from tests.test_engine import SYMBOL, T
from ibkr_microalpha.domain import Side
from ibkr_microalpha.execution import ExecutionBook
from ibkr_microalpha.replay import Replay
from ibkr_microalpha.risk import AccountSnapshot


def snapshot_funds_reused():
    test = fixture()
    e = test.engine
    e.set_account(AccountSnapshot("U1", "JPY", D("350000"), D("350000"), at(0), "broker"), at(0))
    test.fill_entry()
    e.instruments["SECOND"] = e.instruments[SYMBOL]
    # The broker funds snapshot is still fresh; its balance preceded the fill.
    modeled_delta = e.book.cash_flow - e.unreported_fee_reserve
    cash_if_snapshot_rebased = D("350000") + modeled_delta
    decision = e.risk.allocate("second-allocation", "SECOND", e.instruments["SECOND"].sector,
        D("3001"), D("5"), D("1"), 100, 100,
        lambda q, p: (e.commissions.commission(q*p), e.commissions.commission(q*p)))
    result = {"snapshot_funds": "350000", "cash_flow_since_snapshot": str(modeled_delta),
              "cash_if_rebased": str(cash_if_snapshot_rebased), "reported_risk_cash": str(e.risk.cash),
              "second_allocation_allowed": decision.allowed,
              "second_allocation_cash": str(decision.reservation.cash) if decision.allowed else None}
    assert decision.allowed and decision.reservation.cash > cash_if_snapshot_rebased
    return result


def queued_entry_ignores_new_funds():
    test = fixture()
    for second in (0, 5, 10, 11, 12):
        test.tick(second)
    e = test.engine
    buy = next(o for o in e.book.orders.values() if o.side == Side.BUY)
    assert buy.submitted_at is None
    e.set_account(AccountSnapshot("U1", "JPY", D("1000"), D("1000"), at(12.5), "broker"), at(12.5))
    reason = e.entry_still_valid(buy, at(12.5))
    replay = Replay(e)
    replay.dispatch({"event_id": "request-after-cash-cut", "received_at": at(12.5).isoformat(),
                     "sequence": 1, "type": "requests", "data": {}})
    result = {"verified_risk_cash": str(e.risk.cash), "entry_order_notional": str(buy.quantity*buy.limit_price),
              "entry_validation_reason": reason,
              "commands": [{"kind": c.kind, "order_id": c.order_id} for c in replay.last_commands],
              "submitted_at": buy.submitted_at.isoformat() if buy.submitted_at else None}
    assert replay.last_commands and replay.last_commands[0].kind == "SUBMIT"
    return result


def request_budget_lost_on_restore():
    book = ExecutionBook(daily_request_budget=1)
    book.reconcile({}, [], [], T, complete=True, ownership_confirmed=True)
    order = book.submit("first", "A", Side.BUY, 100, D("1000"), T)
    assert len(book.drain_commands(T)) == 1
    result = {"original": {"budget": book.daily_request_budget,
                           "routine_requests_sent": book.routine_requests_sent,
                           "sent_counts": dict(book.sent_counts)}, "restored": {}}
    for kind, restored in (("snapshot", ExecutionBook.from_snapshot(book.snapshot())),
                           ("journal", ExecutionBook.from_journal(book.journal))):
        restored.reconnect(T)
        restored.reconcile({}, [], [], T, complete=True, ownership_confirmed=True)
        new = restored.submit(f"new-{kind}", "B", Side.BUY, 100, D("1000"), T)
        dispatched = restored.drain_commands(T)
        result["restored"][kind] = {"budget": restored.daily_request_budget,
            "routine_requests_sent_before_new": restored.routine_requests_sent-len(dispatched),
            "new_submit_sent": any(c.order_id == new.order_id and c.kind == "SUBMIT" for c in dispatched)}
        assert result["restored"][kind]["new_submit_sent"]
    return result


def late_sell_after_cover_cancel_crashes_valuation():
    test = fixture()
    buy = test.fill_entry()
    e = test.engine
    test.tick(14)
    e._request_exit(SYMBOL, at(14), "KILL_SWITCH", True)
    sell = test.sells()[0]
    e.book.drain_commands(at(14))
    e.book.status(sell.order_id, "Submitted", 0, 100, at(14))
    e.on_fill(exec_id="buy-correction", order_id=buy.order_id, qty=50, price=D("3001"),
              at=at(15), correction_of="buy-exec")
    assert sell.state.value == "CANCEL_PENDING"
    try:
        e.on_fill(exec_id="late-sell", order_id=sell.order_id, qty=100, price=D("3000"), at=at(16))
    except ValueError as error:
        result = {"sell_was_cancel_pending": True, "position_after_late_fill": e.book.positions[SYMBOL].quantity,
                  "execution_recorded": "late-sell" in e.book.executions,
                  "exception": str(error), "locks": sorted(e.book.lock_reasons)}
    else:
        raise AssertionError("Current negative-position valuation did not fail as expected")
    assert result["position_after_late_fill"] == -50 and result["execution_recorded"]
    return result


def flat_quote_not_residual_alarm_until_next_poll():
    # Not a new defect assertion: exercise the known old fixes on current APIs.
    from tests.test_review_fixes import ExecutionRiskFixes
    test = ExecutionRiskFixes()
    for name in ("test_EXE01_transmitted_sell_above_corrected_holding_is_cancelled_and_locked",
                 "test_EXE01_unsent_sell_is_dropped_locally_without_lock",
                 "test_EXE02_stale_quote_never_reaches_the_broker",
                 "test_EXE02_valid_entry_is_sent_and_expired_forecast_is_not",
                 "test_EXE03_exit_price_rule_failure_is_recorded_not_raised",
                 "test_EXE04_entry_score_and_stop_restore_identically_from_journal_and_snapshot",
                 "test_EXE08_routine_request_budget_never_blocks_risk_exits"):
        getattr(test, name)()
    return {"old_fix_regressions_executed": 7, "result": "PASS"}


if __name__ == "__main__":
    result = {"snapshot_funds_reused": snapshot_funds_reused(),
              "queued_entry_ignores_new_funds": queued_entry_ignores_new_funds(),
              "request_budget_lost_on_restore": request_budget_lost_on_restore(),
              "late_sell_after_cover_cancel_crashes_valuation": late_sell_after_cover_cancel_crashes_valuation(),
              "confirmed_old_fixes": flat_quote_not_residual_alarm_until_next_poll()}
    Path(__file__).with_name("evidence.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                        encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
