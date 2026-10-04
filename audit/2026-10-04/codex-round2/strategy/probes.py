"""Round-two, read-only probes using current interfaces. No broker connection."""
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from ibkr_microalpha.domain import Quote
from ibkr_microalpha.features import FeatureConfig, FeatureEngine, VolumeBaseline
from ibkr_microalpha.market import JST, TickTable
from ibkr_microalpha.economics import CalibrationRow, CommissionSchedule, prediction_gate
from ibkr_microalpha.research import LabelPolicy, label_intent, build_calibration_rows

T = datetime(2026, 10, 2, 10, tzinfo=JST)
FEES = CommissionSchedule(D("0.0008"), D("80"), D("0"), "audit")

def quote(second, bid="3000", *, size=100, kind=1, event=None):
    at = T+timedelta(seconds=second)
    bid = D(str(bid))
    ask = TickTable().move_ticks(bid, 1, at)
    return Quote("A", at, bid, ask, 100, size, event or f"q-{second}",
                 market_data_type=kind, bid_at=at, ask_at=at)

def volatility(grid, jump_second):
    f = FeatureEngine("BENCH", FeatureConfig(required_features=("volatility_bps",),
                                             volatility_grid_seconds=grid))
    for second in range(61):
        f.on_quote(quote(second, "100" if second < jump_second else "200"))
    s = f.snapshot("A", T+timedelta(seconds=60))
    return {"grid_seconds": grid, "jump_second": jump_second,
            "valid": s.valid, "r_60": s.values["r_60"], "volatility_bps": s.values["volatility_bps"]}

def label_probes():
    policy = LabelPolicy(holding_seconds=5, entry_limit_ticks=1, entry_order_ttl_seconds=2,
                         min_stop_ticks=5, stop_bps=D(0), exit_slippage_ticks=1)
    deadline = T+timedelta(hours=1)
    partial = [quote(0), quote(1, size=40), quote(2, size=40), quote(3, "2950")]
    # An actual partial entry of 40 at 3001 and exit at 2950 has a loss, not zero.
    controlled_partial_net = D(40)*(D(2950)-D(3001)) - FEES.commission(D(40)*D(3001)) - FEES.commission(D(40)*D(2950))
    delayed = [quote(s, str(3000 if s <= 1 else 3000+5*s), kind=3) for s in range(8)]
    partial_exit = [quote(0, "100"), quote(1, "100"), quote(2, "90"), quote(3, "80")]
    partial_exit[2] = Quote(**{**partial_exit[2].__dict__, "bid_size": 40})
    return {"partial_entry_label": str(label_intent(partial, 0, 100, policy, TickTable(), FEES, deadline)),
            "one_legal_partial_fill_path_net": str(controlled_partial_net),
            "all_delayed_quotes_label": str(label_intent(delayed, 0, 100, policy, TickTable(), FEES, deadline)),
            "partial_exit_label": str(label_intent(partial_exit, 0, 100, policy, TickTable(), FEES, deadline)),
            "first_exit_limit": "89.9", "next_assumed_fill_bid": "80",
            "same_two_child_exit_path_net_after_minimum_fees": "-1850.0",
            "unimplemented_rules": ["ordinary signal exits", "volatility stop", "submit latency", "cancel/replace", "partial entry"]}

def calibration_days():
    direct = CalibrationRow("p", "v", 120, 100, 0, None, 0, 100, D(100), D(100), 1)
    direct_gate = prediction_gate(direct.prediction(), policy_id="p", version="v", quantity=100,
                                  min_samples=30, safety_margin=D(1), max_holding_seconds=120)
    samples = [(date(2026, 10, 1), 1., D(100)) for _ in range(100)]
    generated = build_calibration_rows(samples, [0], policy_id="p", version="v", holding_seconds=120,
        quantity=100, max_chase_ticks=1, min_days=1, min_samples=30, draws=10)[0]
    return {"zero_days_row_allowed": direct_gate.allowed, "reason": direct_gate.reason,
            "one_day_generated_mean": str(generated.mean_net_amount),
            "one_day_generated_lower": str(generated.lower_net_amount),
            "one_day_sample_count": generated.sample_count,
            "one_day_prediction_reliable": generated.prediction().reliable}

def previous_fixes():
    f = FeatureEngine("B", FeatureConfig(rvol_days=20, rvol_min_days=20))
    for offset in range(1, 22):
        f.add_volume_baseline(VolumeBaseline("A", T.date()-timedelta(days=offset), 36000,
            30, 1000, T-timedelta(days=1), valid=offset != 1))
    return {"past_twenty_valid_days_denominator": f._baseline_value("volume", "A", "TBT", 30, T),
            "default_grid_anchor": volatility(1, 1)}

if __name__ == "__main__":
    result = {"new_grid_tail_omission": volatility(7, 59),
              "oversized_grid": volatility(120, 1), "labels": label_probes(),
              "independent_day_gate": calibration_days(), "previous_fixes": previous_fixes()}
    destination = Path(__file__).with_name("results.json")
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
