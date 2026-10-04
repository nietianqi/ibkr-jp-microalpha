"""Offline evidence for the 2026-10-04 review; no adapter or broker calls."""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from ibkr_microalpha.domain import Quote, Side
from ibkr_microalpha.execution import ExecutionBook
from ibkr_microalpha.replay import Replay

T = datetime(2026, 10, 2, 1, tzinfo=timezone.utc)


def correction_after_sell_transmission():
    book = ExecutionBook()
    assert book.reconcile({}, [], [], T, complete=True, ownership_confirmed=True)
    buy = book.submit("buy", "S", Side.BUY, 100, D("3001"), T)
    book.drain_commands(T)
    book.fill("buy-1", buy.order_id, 100, D("3001"), T)
    assert book.reconcile({"S": 100}, [], [], T, complete=True, ownership_confirmed=True)
    sell = book.submit("sell", "S", Side.SELL, 100, D("2999"), T)
    book.drain_commands(T)
    book.status(sell.order_id, "Submitted", 0, 100, T)
    book.fill("buy-correction", buy.order_id, 50, D("3001"), T,
              correction_of="buy-1")
    result = {"held_after_correction": book.positions["S"].quantity,
              "submitted_sell_remaining": sell.possible_remaining,
              "state": sell.state.value, "commands": [c.kind for c in book.commands],
              "locks": sorted(book.lock_reasons), "reconciled": book.reconciled}
    assert result["held_after_correction"] == 50
    assert result["submitted_sell_remaining"] == 100
    assert result["commands"] == [] and result["locks"] == []
    book.fill("sell-1", sell.order_id, 100, D("2999"), T)
    result["held_after_sell_fills"] = book.positions["S"].quantity
    assert result["held_after_sell_fills"] == -50
    return result


def load_fixture():
    spec = importlib.util.spec_from_file_location("engine_audit_fixture", ROOT / "tests/test_engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    test = module.EngineIntegrationTests()
    test.setUp()
    test.fill_entry()
    return module, test


def invalid_exit_limit():
    module, test = load_fixture()
    engine = test.engine
    at = module.T + timedelta(seconds=14)
    quote = Quote(module.SYMBOL, at, D("0.5"), D("0.6"), 100, 100,
                  "low-price", bid_at=at, ask_at=at)
    engine.quotes[module.SYMBOL] = quote
    assert engine._valid_quote(module.SYMBOL, at) is not None
    try:
        engine._request_exit(module.SYMBOL, at, "STOP_LOSS", True)
    except ValueError as error:
        result = {"bid": str(quote.bid), "exit_slippage": str(engine.config.exit_slippage),
                  "exception": str(error), "sell_orders": len(test.sells()),
                  "exit_blocked_audit": sum(e["kind"] == "EXIT_BLOCKED" for e in engine.audit)}
    else:
        raise AssertionError("Expected current negative limit failure")
    assert result["sell_orders"] == 0 and result["exit_blocked_audit"] == 0
    return result


def correction_engine_exit_stalls():
    module, test = load_fixture()
    engine = test.engine
    at = module.T + timedelta(seconds=14)
    test.tick(14)
    engine._request_exit(module.SYMBOL, at, "ALPHA_EXIT", False)
    sell = test.sells()[0]
    engine.book.drain_commands(at)
    engine.book.status(sell.order_id, "Submitted", 0, 100, at)
    buy = next(o for o in engine.book.orders.values() if o.side == Side.BUY)
    engine.on_fill(exec_id="buy-correction", order_id=buy.order_id, qty=50,
                   price=D("3001"), at=at, correction_of="buy-exec")
    test.tick(17)  # Exit TTL has elapsed and quote remains a valid 3000 bid.
    result = {"holding": engine.book.positions[module.SYMBOL].quantity,
              "sell_remaining": sell.possible_remaining, "sell_limit": str(sell.limit_price),
              "current_bid": str(engine.quotes[module.SYMBOL].bid),
              "sell_state_after_exit_ttl": sell.state.value,
              "commands": [c.kind for c in engine.book.commands],
              "risk_locks": sorted(engine.risk.lock_reasons)}
    assert result["sell_remaining"] > result["holding"]
    assert result["commands"] == []
    return result


def request_dispatch_precedes_staleness_gate():
    spec = importlib.util.spec_from_file_location("engine_audit_fixture_requests", ROOT / "tests/test_engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    test = module.EngineIntegrationTests()
    test.setUp()
    for second in (0, 5, 10, 11, 12):
        test.tick(second)
    engine = test.engine
    buy = next(o for o in engine.book.orders.values() if o.side == Side.BUY)
    assert buy.submitted_at is None
    replay = Replay(engine)
    at = module.T + timedelta(seconds=15)
    assert engine._valid_quote(module.SYMBOL, at) is None
    replay.dispatch({"received_at": at.isoformat(), "sequence": 1,
                     "event_id": "request-after-market-silence", "type": "requests", "data": {}})
    result = {"quote_age_seconds": (at-engine.quotes[module.SYMBOL].at).total_seconds(),
              "allowed_max_quote_age_seconds": engine.quality.max_age_seconds,
              "returned_commands": [{"kind": c.kind, "order_id": c.order_id} for c in replay.last_commands],
              "order_submitted_at": buy.submitted_at.isoformat(),
              "order_state_after_poll": buy.state.value,
              "next_pending_commands": [c.kind for c in engine.book.commands],
              "market_state_after_poll": engine.market_state.value}
    assert result["returned_commands"] == [{"kind": "SUBMIT", "order_id": buy.order_id}]
    return result


def entry_metadata_not_in_journal():
    spec = importlib.util.spec_from_file_location("engine_audit_fixture_metadata", ROOT / "tests/test_engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    test = module.EngineIntegrationTests()
    test.setUp()
    buy = test.working_entry()
    submission_score = buy.entry_score
    test.tick(13, rs_60=12, rs_30=10)
    test.engine.on_fill(exec_id="metadata-fill", order_id=buy.order_id, qty=100,
                        price=D("3001"), at=module.T+timedelta(seconds=13))
    restored_snapshot = ExecutionBook.from_snapshot(test.engine.book.snapshot())
    restored_journal = ExecutionBook.from_journal(test.engine.book.journal)
    result = {"submission_score": submission_score, "current_score": buy.entry_score,
              "restored_snapshot_score": restored_snapshot.positions[module.SYMBOL].entry_score,
              "restored_journal_score": restored_journal.positions[module.SYMBOL].entry_score}
    assert result["restored_snapshot_score"] != result["restored_journal_score"]
    demo_snapshot = json.loads((ROOT / "runs/demo/execution.json").read_text(encoding="utf-8"))
    demo_journal = [json.loads(line) for line in (ROOT / "runs/demo/execution-journal.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    result["existing_demo_snapshot_order_score"] = demo_snapshot["orders"][0]["entry_score"]
    result["existing_demo_journal_submit_score"] = next(
        event["payload"]["order"]["entry_score"] for event in demo_journal if event["kind"] == "SUBMIT")
    return result


if __name__ == "__main__":
    result = {"correction_after_sell_transmission": correction_after_sell_transmission(),
              "correction_engine_exit_stalls": correction_engine_exit_stalls(),
              "invalid_exit_limit": invalid_exit_limit(),
              "request_dispatch_precedes_staleness_gate": request_dispatch_precedes_staleness_gate(),
              "entry_metadata_not_in_journal": entry_metadata_not_in_journal()}
    output = Path(__file__).with_name("evidence.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
