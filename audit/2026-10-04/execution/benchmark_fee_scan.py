"""Synthetic in-memory benchmark of retained order/fee scans, not live latency."""
import json
import statistics
import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.domain import OrderState, Side
from ibkr_microalpha.execution import Fill, Order

T = datetime(2026, 10, 2, 1, tzinfo=timezone.utc)
rows = []
for intent_count in (1, 10, 100, 1000):
    engine = build_engine(load_config(ROOT / "examples/research.json"))
    for order_id in range(1, intent_count * 2 + 1):
        side = Side.BUY if order_id % 2 else Side.SELL
        order = Order(order_id, f"benchmark-{order_id}", "STOCK_SYNTHETIC", side,
                      100, D("3001"), T, state=OrderState.FILLED,
                      filled_quantity=100, filled_notional=D("300100"),
                      reported_filled=100, reported_remaining=0, reconciled=True)
        exec_id = f"exec-{order_id}"
        fill = Fill(exec_id, order_id, 100, D("3001"), T, order_id)
        engine.book.orders[order_id] = order
        engine.book.executions[exec_id] = fill
        engine.book._current[exec_id] = exec_id
        engine.book._roots[exec_id] = exec_id
        engine.book.commissions[exec_id] = engine.commissions.commission(order.filled_notional)
    samples = []
    for _ in range(12):
        started = perf_counter_ns()
        assert engine._fee_reserves() == 0
        samples.append((perf_counter_ns()-started)/1e6)
    rows.append({"roundtrips": intent_count, "orders": intent_count * 2,
                 "executions": intent_count * 2,
                 "median_fee_reserve_ms": statistics.median(samples),
                 "min_fee_reserve_ms": min(samples),
                 "scope": "synthetic retained history; 1000 exceeds default 100 daily entries"})
result = {"measure": "only StrategyEngine._fee_reserves; host CPU timer; no broker/network",
          "samples_per_row": 12, "rows": rows}
Path(__file__).with_name("fee_scan_benchmark.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
