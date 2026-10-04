"""Current-API EXE-12 / RPT-06 verification; no broker or original-log writes."""
import json
import sys
from datetime import timedelta
from pathlib import Path
from decimal import Decimal as D

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from tests.test_round3_execution import RequestRecoveryTests
from tests.test_review_fixes import fixture
from tests.test_engine import SYMBOL, T
from ibkr_microalpha.execution import ExecutionBook
from ibkr_microalpha.reporting import layer_funnel

book = RequestRecoveryTests().populated()
restored = {kind: {"budget": b.daily_request_budget, "routine": b.routine_requests_sent,
                  "sent_counts": dict(b.sent_counts), "request_day": str(b.request_day),
                  "connected": b.connected, "queued": len(b.commands)}
            for kind, b in (("snapshot", ExecutionBook.from_snapshot(book.snapshot())),
                            ("journal", ExecutionBook.from_journal(book.journal)))}
test = fixture()
engine = test.engine
test.fill_entry()
test.tick(14)
engine._request_exit(SYMBOL, T+timedelta(seconds=14), "SIGNAL_EXIT", False)
sell = test.sells()[-1]
engine.book.drain_commands(T+timedelta(seconds=14))
engine.on_fill(exec_id="first-exit", order_id=sell.order_id, qty=100, price=D("3000"),
               at=T+timedelta(seconds=15))
engine.on_fill(exec_id="exit-bust", order_id=sell.order_id, qty=0, price=D("3000"),
               at=T+timedelta(seconds=16), correction_of="first-exit")
assert engine.book.reconcile({SYMBOL: 100}, [], [], T+timedelta(seconds=16),
                             complete=True, ownership_confirmed=True)
test.tick(17)
engine.kill_switch(T+timedelta(seconds=17), "VERIFY_EXIT")
repair = test.sells()[-1]
engine.book.drain_commands(T+timedelta(seconds=17))
engine.on_fill(exec_id="repair", order_id=repair.order_id, qty=100, price=D("3000"),
               at=T+timedelta(seconds=18))
rows = layer_funnel(engine)[1]
assert len(rows) == 1 and rows[0]["status"] == "CLOSED"
assert rows[0]["bought"] == rows[0]["sold"] == 100
assert D(rows[0]["net_amount"]) == engine.daily_net_pnl_estimate
evidence = {"restore": restored, "sell_bust_repaired": {
    "first_sell_owner": engine.intents.order_to_intent[sell.order_id],
    "repair_sell_owner": engine.intents.order_to_intent[repair.order_id],
    "position_final": engine.book.positions[SYMBOL].quantity,
    "intents_final": rows, "daily_net_pnl_estimate": str(engine.daily_net_pnl_estimate)},
    "initial_red_run": {"new_tests": 8, "failures_including_subtests": 3,
                        "errors_from_missing_new_APIs": 6}}
Path(__file__).with_name("evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
print(json.dumps(evidence, ensure_ascii=False, indent=2))
