"""Claude review probes (2026-10-04), updated for the post-fix API. Offline only.

``probe-results.json`` holds the results measured on the reviewed (pre-fix)
code; ``probe-results-after.json`` holds the same scenarios after the fixes.

Run from the repository root:
    python audit/2026-10-04/claude/probes.py            # all probes, prints JSON lines
    python audit/2026-10-04/claude/probes.py gap touch  # selected probes
    python audit/2026-10-04/claude/probes.py --save     # writes probe-results-after.json here
"""
import collections
import json
import sys
import time
import tracemalloc
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from ibkr_microalpha.config import build_engine, load_config  # noqa: E402
from ibkr_microalpha.domain import FeatureSnapshot, Quote, Side  # noqa: E402
from ibkr_microalpha.execution import ExecutionBook  # noqa: E402
from ibkr_microalpha.features import FeatureConfig, FeatureEngine, SameTimeProfile, VolumeBaseline  # noqa: E402
from ibkr_microalpha.market import JST, TradingDay  # noqa: E402
from ibkr_microalpha.reporting import ExecutionQuality  # noqa: E402
from ibkr_microalpha.replay import Replay  # noqa: E402
from tests import test_engine as base  # noqa: E402


def harness():
    test = base.EngineIntegrationTests("runTest")
    test.setUp()
    return test


def probe_enhanced_crash():
    """DATA-01: the enhanced coordinator must survive its first candidate."""
    doc = deepcopy(base.fixture_document())
    for group, key in (("engine", "score_version"), ("features", "version"), ("confirmation", "version")):
        doc[group][key] = "enhanced-v1"
    doc["features"].update(vwap_kind="TICK", trade_source="TBT")
    doc["features"]["required_features"] = [n.replace("vwap_proxy_", "vwap_")
                                            for n in doc["features"]["required_features"]] + ["ti_10", "ti_60"]
    doc["confirmation"].update(enhanced=True, required_ti_features=["ti_10"])
    doc["subscriptions"] = {"quota": 5, "min_tenure_seconds": 120, "plan_interval_seconds": 30,
                            "required_windows": {"ti_10": 10, "ti_60": 60, "r_30": 30}}
    engine = build_engine(doc)
    T = base.T
    engine.calendar.records.append(TradingDay(T.date(), True, T - timedelta(days=1), "probe"))
    engine.book.reconcile({}, [], [], T, complete=True, ownership_confirmed=True)
    values = dict(r_300=10, rs_300=8, rs_30=4, rs_60=5, rvol_30=3, vwap_slope_60=2, vwap_slope_120=3,
                  vwap_deviation_bps=10, spread_bps=3.33, volatility_bps=8, obi=.8, ti_10=.5, ti_60=.5,
                  classification_coverage_10=1, classification_coverage_60=1)
    for second in (0, 5, 10, 11):
        at = T + timedelta(seconds=second)
        engine.quotes[base.SYMBOL] = Quote(base.SYMBOL, at, D("3000"), D("3001"), 900, 100, f"e{second}",
                                           bid_at=at, ask_at=at)
        engine.set_market(FeatureSnapshot("M", at, dict(rv_mkt=1, spread_bps=2, breadth=.8), True), at)
        try:
            engine.evaluate(FeatureSnapshot(base.SYMBOL, at, values, True, "enhanced-v1"), at)
        except TypeError as error:
            return {"crashed_at_second": second, "error": str(error)}
    return {"crashed_at_second": None, "candidates": engine.funnel["candidates"],
            "subscribed": sorted(engine.subscriptions.scheduler.active)}


def probe_quote_gap_sticky_lock(gap_seconds=2.5):
    """STR-02/DATA-02: a 2.5 s quiet period on a held stock (demo replay up to the first fill)."""
    events = [json.loads(line) for line in (ROOT / "runs/demo/raw-input.jsonl").open(encoding="utf-8")]
    runner = Replay(build_engine(load_config(ROOT / "runs/demo/frozen-config.json")))
    stop = next(i for i, e in enumerate(events) if e["type"] == "commission")
    for event in events[: stop + 1]:
        runner.dispatch(event)
    engine, symbol = runner.engine, "STOCK_SYNTHETIC"
    last = engine.quotes[symbol]
    held = engine.book.positions[symbol].quantity
    sequence, rows = 10_000_000, []
    for k in range(31):
        at = last.at + timedelta(seconds=gap_seconds + k)
        for symbol_, bid, ask in (("BENCH_SYNTHETIC", "500", "500.1"), (symbol, str(last.bid), str(last.ask))):
            sequence += 1
            runner.dispatch({"event_id": f"gap-{sequence}", "received_at": at.isoformat(), "sequence": sequence,
                             "type": "quote", "data": {"symbol": symbol_, "bid": bid, "ask": ask,
                                                       "bid_size": 1000, "ask_size": 200, "source": "L1",
                                                       "bid_at": at.isoformat(), "ask_at": at.isoformat()}})
        if k in (0, 30):
            runner.finish()
            snap = engine.snapshots[symbol]
            rows.append({"seconds_after_last_quote": gap_seconds + k, "snapshot_valid": snap.valid,
                         "snapshot_reason": snap.reason[:90], "risk_locks": sorted(engine.risk.lock_reasons),
                         "soft_blocks": sorted(engine.risk.soft_blocks),
                         "sell_orders": [{"order_id": o.order_id, "emergency": o.emergency, "qty": o.quantity,
                                          "reason": next((r["reason"] for r in engine.audit[::-1]
                                                          if r.get("order_id") == o.order_id
                                                          and r["kind"] == "EXIT_REQUEST"), None)}
                                         for o in engine.book.orders.values() if o.side == Side.SELL]})
    return {"held_before_gap": held, "same_prices_resumed": True, "after": rows}


def probe_market_snapshot_lag():
    """STR-02: only the market feed is late (stock quote fresh)."""
    test = harness()
    test.fill_entry()
    engine = test.engine
    at = base.T + timedelta(seconds=15.5)
    engine.quotes[base.SYMBOL] = Quote(base.SYMBOL, at, D("3000"), D("3001"), 900, 100, "fresh",
                                       bid_at=at, ask_at=at)
    engine.poll(at)
    return {"market_state": engine.market_state.value, "locks": sorted(engine.risk.lock_reasons),
            "soft_blocks": sorted(engine.risk.soft_blocks),
            "sells": [{"qty": o.quantity, "emergency": o.emergency, "limit": str(o.limit_price)}
                      for o in engine.book.orders.values() if o.side == Side.SELL]}


def probe_touch_limit_goes_passive():
    """STR-03: after the ask ticks up the entry must not rest as a passive bid."""
    test = harness()
    buy = test.working_entry()
    states = {}
    for second in range(13, 21):
        test.tick(second, bid="3001", ask="3002")
        states[second] = buy.state.value
    return {"limit": str(buy.limit_price), "order_expires_at": buy.candidate_expires_at.isoformat(),
            "quote_after_move": "3001/3002", "states": states}


def probe_single_update_veto():
    """STR-04: one L1 update with OBI -0.13 must only WAIT."""
    test = harness()
    engine = test.engine
    for second in (0, 5, 10):
        test.tick(second)
    at = base.T + timedelta(seconds=11)
    engine.quotes[base.SYMBOL] = Quote(base.SYMBOL, at, D("3000"), D("3001"), 100, 130, "neg-obi",
                                       bid_at=at, ask_at=at)
    engine.set_market(FeatureSnapshot("BENCH_SYNTHETIC", at, dict(rv_mkt=1, spread_bps=2, breadth=.8), True), at)
    engine.evaluate(test.prepared_snapshot(11), at)
    for second in (12, 13, 20, 30, 40):
        test.tick(second)
    return {"intents_after_40s": engine.funnel["intents"], "candidates": engine.funnel["candidates"],
            "veto": engine.rejections.get("quote direction reversal", 0)
            + engine.rejections.get("smoothed quote direction reversal", 0),
            "transient_waits": engine.rejections.get("transient negative imbalance", 0)}


def probe_oversell_after_correction():
    """EXE-01: a transmitted sell above the corrected holding is cancelled and locked."""
    T = base.T
    book = ExecutionBook()
    book.reconcile({}, [], [], T, complete=True, ownership_confirmed=True)
    buy = book.submit("b", "S", Side.BUY, 100, D("3001"), T)
    book.drain_commands(T)
    book.fill("x1", buy.order_id, 100, D("3001"), T)
    sell = book.submit("s", "S", Side.SELL, 100, D("2999"), T)
    book.drain_commands(T)
    book.status(sell.order_id, "Submitted", 0, 100, T)
    book.fill("x1c", buy.order_id, 50, D("3001"), T, correction_of="x1")
    return {"held": book.positions["S"].quantity, "sell_state": sell.state.value,
            "sell_possible_remaining": sell.possible_remaining,
            "locks": sorted(book.lock_reasons), "pending_commands": [c.kind for c in book.commands]}


def probe_reporting_bugs():
    """RPT-01/02 on the engine fixture: drift horizon and correction/bust costs."""
    T = base.T

    class Order:
        order_id, symbol, side, emergency = 1, "S", Side.BUY, False

    def quote(at, mid):
        return Quote("S", at, D(mid) - D("0.5"), D(mid) + D("0.5"), 100, 100, f"q{at}", bid_at=at, ask_at=at)

    drift = ExecutionQuality(horizons=(5,))
    drift.on_submit(Order, quote(T, "100"), T)
    drift.on_fill("f1", Order, T)
    drift.observe(T, lambda s: quote(T, "100"), 2)
    drift.observe(T + timedelta(seconds=5), lambda s: quote(T + timedelta(seconds=4), "100"), 2)
    drift.observe(T + timedelta(seconds=5), lambda s: quote(T + timedelta(seconds=5), "101"), 2)
    test = harness()
    buy = test.fill_entry()
    test.engine.on_fill(exec_id="c", order_id=buy.order_id, qty=100, price=D("3002"),
                        at=T + timedelta(seconds=14), correction_of="buy-exec")
    corrected = test.engine.execution_quality.summary(test.engine.book)["total_price_cost_vs_arrival"]
    return {"drift_5s_bps": drift.drifts()["f1"].results[5], "drift_5s_correct_bps": 99.5033,
            "corrected_cost_reported": corrected, "corrected_cost_correct": "150.0"}


def probe_rvol_invalid_day():
    """DATA-05: one invalid most-recent day must not remove RVOL for the whole day."""
    T = datetime(2026, 10, 2, 10, tzinfo=JST)
    features = FeatureEngine("B", FeatureConfig(rvol_days=20, rvol_min_days=20))
    for offset in range(1, 22):
        features.add_volume_baseline(VolumeBaseline("A", T.date() - timedelta(days=offset), 36000, 30, 1000,
                                                    T - timedelta(days=1), valid=offset != 1))
    return {"rows": 21, "valid_rows": 20, "denominator": features._baseline_value("volume", "A", "TBT", 30, T)}


def probe_snapshot_scaling():
    """PERF-02: per-snapshot cost must not grow with the universe (breadth moved to market snapshot)."""
    doc = load_config(ROOT / "examples/research.json")["features"]
    config = FeatureConfig(**{k: (tuple(v) if k == "required_features" else v) for k, v in doc.items()})
    start = datetime(2026, 10, 2, 9, 0, tzinfo=JST)
    rows = []
    for n in (1, 10, 40):
        features = FeatureEngine("BENCH", config)
        symbols = [f"S{i}" for i in range(n)]
        for second in range(650):
            at = start + timedelta(seconds=second)
            features.on_quote(Quote("BENCH", at, D("500"), D("500.1"), 1000, 1000, f"b{second}", bid_at=at, ask_at=at))
            for i, symbol in enumerate(symbols):
                bid = D("1000") + D(second % 7) + D(i)
                features.on_quote(Quote(symbol, at, bid, bid + 1, 500, 400, f"{symbol}-{second}", bid_at=at, ask_at=at))
        at = start + timedelta(seconds=649)
        repetitions = 30
        t0 = time.perf_counter()
        for _ in range(repetitions):
            features.snapshot("S0", at)
        per_snapshot = (time.perf_counter() - t0) / repetitions * 1000
        t1 = time.perf_counter()
        for _ in range(repetitions):
            features.market_snapshot(at, symbols)
        per_market = (time.perf_counter() - t1) / repetitions * 1000
        rows.append({"symbols": n, "snapshot_ms": round(per_snapshot, 3), "market_snapshot_ms": round(per_market, 3),
                     "cpu_ms_per_second_at_4hz_each": round(per_snapshot * n * 4 + per_market * 4, 1)})
    return rows


def probe_baseline_memory(rows_n=100_000):
    """DATA-03: legacy per-second rows vs one same-time profile row per bucket."""
    T = datetime(2026, 10, 2, 8, tzinfo=JST)
    tracemalloc.start()
    features = FeatureEngine("B", FeatureConfig())
    for k in range(rows_n):
        features.add_volume_baseline(VolumeBaseline("A", date(2026, 9, 1) + timedelta(days=k % 20),
                                                    32400 + k // 20, 30, 1000, T, source="TBT"))
    legacy, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    tracemalloc.start()
    profiles = FeatureEngine("B", FeatureConfig(baseline_bucket_seconds=300))
    buckets = [32400 + 300 * i for i in range(66)]   # 09:00-15:30 in 5-minute buckets
    for window in (5, 10, 30, 60, 120):
        for bucket in buckets:
            profiles.add_profile(SameTimeProfile("volume", "A", "TBT", window, bucket, 300, 1000.0, 20, T, "v1"))
    profile_bytes, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    per_row = legacy / rows_n
    full_day = 18_000 * 20 * 5
    return {"legacy_bytes_per_row": round(per_row), "legacy_est_MiB_one_symbol": round(per_row * full_day / 2**20),
            "legacy_est_GiB_50_symbols": round(per_row * full_day * 50 / 2**30, 1),
            "profile_rows_one_symbol": len(buckets) * 5,
            "profile_KiB_one_symbol": round(profile_bytes / 1024, 1),
            "profile_est_MiB_50_symbols": round(profile_bytes * 50 / 2**20, 2)}


def probe_demo_timing_and_counts():
    """Per-event wall time by type on the stored (post-fix) demo input, plus hot-call counts."""
    from ibkr_microalpha import engine as engine_module, features as feature_module, risk
    counts = collections.Counter()
    patched = []

    def wrap(owner, name, label=None):
        original = getattr(owner, name)

        def wrapper(*args, **kwargs):
            counts[label or name] += 1
            return original(*args, **kwargs)
        setattr(owner, name, wrapper)
        patched.append((owner, name, original))

    wrap(risk.PortfolioRisk, "allocate")
    wrap(feature_module.FeatureEngine, "snapshot")
    wrap(feature_module.FeatureEngine, "market_snapshot")
    wrap(feature_module.FeatureEngine, "_baseline_value")
    wrap(engine_module.StrategyEngine, "poll")
    try:
        events = [json.loads(line) for line in (ROOT / "runs/demo/raw-input.jsonl").open(encoding="utf-8")]
        runner = Replay(build_engine(load_config(ROOT / "runs/demo/frozen-config.json")))
        by_type = {}
        t_all = time.perf_counter()
        for event in events:
            t0 = time.perf_counter()
            runner.dispatch(event)
            by_type.setdefault(event["type"], []).append((time.perf_counter() - t0) * 1000)
        runner.finish()
        total = time.perf_counter() - t_all
    finally:
        for owner, name, original in patched:
            setattr(owner, name, original)

    def pct(values, p):
        values = sorted(values)
        return round(values[min(len(values) - 1, int(p * len(values)))], 3)
    return {"events": len(events), "wall_s_with_counters": round(total, 2),
            "ms_by_type": {k: {"n": len(v), "p50": pct(v, .5), "p99": pct(v, .99)} for k, v in by_type.items()},
            "call_counts": dict(counts)}


PROBES = {
    "enhanced": probe_enhanced_crash, "gap": probe_quote_gap_sticky_lock, "market_lag": probe_market_snapshot_lag,
    "touch": probe_touch_limit_goes_passive, "veto": probe_single_update_veto,
    "oversell": probe_oversell_after_correction, "reporting": probe_reporting_bugs,
    "rvol": probe_rvol_invalid_day, "scaling": probe_snapshot_scaling, "memory": probe_baseline_memory,
    "timing": probe_demo_timing_and_counts,
}

if __name__ == "__main__":
    save = "--save" in sys.argv
    selected = [a for a in sys.argv[1:] if a != "--save"] or list(PROBES)
    results = {}
    for name in selected:
        t0 = time.perf_counter()
        results[name] = PROBES[name]()
        results[name + "_probe_seconds"] = round(time.perf_counter() - t0, 2)
        print(name, json.dumps(results[name], ensure_ascii=False, default=str), flush=True)
    if save:
        (HERE / "probe-results-after.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str),
                                                       encoding="utf-8")
