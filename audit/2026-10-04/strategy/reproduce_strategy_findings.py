"""Read-only audit reproductions; does not submit orders or change production files."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.domain import FeatureSnapshot, Quote
from ibkr_microalpha.features import FeatureConfig, FeatureEngine, VolumeBaseline
from ibkr_microalpha.market import JST, TickTable, TradingDay

T = datetime(2026, 10, 2, 10, tzinfo=JST)
SYMBOL = "STOCK_SYNTHETIC"

def enhanced_document():
    document = deepcopy(load_config(ROOT / "examples/research.json"))
    for group, key in (("engine", "score_version"), ("features", "version"), ("confirmation", "version")):
        document[group][key] = "enhanced-v1"
    document["features"].update(vwap_kind="TICK", trade_source="TBT")
    document["features"]["required_features"] = [name.replace("vwap_proxy_", "vwap_")
        for name in document["features"]["required_features"]] + ["ti_10", "ti_60"]
    document["confirmation"].update(enhanced=True, required_ti_features=["ti_10"])
    document["subscriptions"] = {"quota": 5, "min_tenure_seconds": 120,
        "required_windows": {"ti_10": 10, "ti_60": 60, "r_30": 30}}
    return document

def q(symbol, at, bid, event_id):
    bid = D(str(bid))
    ask = TickTable().move_ticks(bid, 1, at)
    return Quote(symbol, at, bid, ask, 900, 100, event_id, bid_at=at, ask_at=at)

def enhanced_opportunity_crash():
    engine = build_engine(enhanced_document())
    engine.calendar.records.append(TradingDay(T.date(), True, T-timedelta(days=1), "AUDIT"))
    engine.book.reconcile({}, [], [], T, complete=True, ownership_confirmed=True)
    # Synthetic prepared features isolate coordinator interface compatibility.
    values = dict(r_300=10, rs_300=8, rs_30=4, rs_60=5, rvol_30=3,
        vwap_slope_60=2, vwap_slope_120=3, vwap_deviation_bps=10,
        spread_bps=3.33, volatility_bps=8, obi=.8, ti_10=.5, ti_60=.5,
        classification_coverage_10=1, classification_coverage_60=1)
    for second in (0, 5, 10):
        at = T+timedelta(seconds=second)
        engine.quotes[SYMBOL] = q(SYMBOL, at, 3000, f"crash-q-{second}")
        engine.set_market(FeatureSnapshot("MARKET", at, dict(rv_mkt=1, spread_bps=2, breadth=.8), True), at)
        try:
            engine.evaluate(FeatureSnapshot(SYMBOL, at, values, True, "enhanced-v1"), at)
        except TypeError as error:
            return {"second": second, "error": str(error), "research_candidates": engine.funnel["research_candidates"]}
    return {"error": None}

def preallocation_dependency():
    engine = build_engine(enhanced_document())
    start = datetime(2026, 10, 2, 9, tzinfo=JST)
    for second in range(601):
        at = start + timedelta(seconds=second)
        quote = q(SYMBOL, at, D("900")+D(second)/10, f"pre-q-{second}")
        engine.features.on_quote(quote)
        engine.features.on_quote(q("BENCH_SYNTHETIC", at, 500, f"pre-b-{second}"))
        engine.quotes[SYMBOL] = quote
    snapshot = engine.features.snapshot(SYMBOL, at)
    engine.snapshots[SYMBOL] = snapshot
    ready = engine._enhanced_ready(snapshot, at)
    return {"positive_r_300": snapshot.values["r_300"], "positive_rs_300": snapshot.values["rs_300"],
        "missing_preallocation_inputs": [name for name in ("rvol_30", "vwap_slope_60") if name not in snapshot.values],
        "active_subscriptions": list(engine.subscription_scheduler.active), "ready": ready}

def volatility_boundary():
    features = FeatureEngine("BENCH", FeatureConfig(required_features=("volatility_bps",)))
    for second in range(61):
        at = T + timedelta(seconds=second)
        features.on_quote(q("A", at, 100 if second == 0 else 200, f"vol-q-{second}"))
    at = T+timedelta(seconds=60.5)
    features.on_quote(q("A", at, 200, "vol-q-current"))
    snapshot = features.snapshot("A", at)
    return {"snapshot_valid": snapshot.valid, "r_60": snapshot.values["r_60"],
        "volatility_bps": snapshot.values["volatility_bps"],
        "same_window_with_anchor_bps": features._realized_volatility(features._series["A"], at, 60)}

def baseline_valid_days():
    features = FeatureEngine("BENCH", FeatureConfig(rvol_days=20, rvol_min_days=20))
    second = T.hour*3600 + T.minute*60 + T.second
    for offset in range(1, 22):
        features.add_volume_baseline(VolumeBaseline("A", T.date()-timedelta(days=offset), second,
            30, 1000, T-timedelta(days=1), valid=offset != 1))
    return {"total_rows": 21, "valid_rows": 20, "denominator": features._rvol_baseline("A", T, 30)}

def dedup_retention():
    features = FeatureEngine("BENCH", FeatureConfig(required_features=("r_5",)))
    for second in range(3601):
        at = T+timedelta(seconds=second)
        features.on_quote(q("A", at, 200, f"dedup-{second}"))
    state = features._series["A"]
    result = {"quote_window_rows": len(state.quotes), "seen_before_reset": len(state.seen)}
    features.reset("A", reset_daily=True)
    result["seen_after_daily_reset"] = len(state.seen)
    return result

if __name__ == "__main__":
    results = {"enhanced_opportunity_crash": enhanced_opportunity_crash(),
        "preallocation_dependency": preallocation_dependency(),
        "volatility_boundary": volatility_boundary(), "baseline_valid_days": baseline_valid_days(),
        "dedup_retention": dedup_retention()}
    print(json.dumps(results, ensure_ascii=False, indent=2))
