"""Read-only profiling of captured data; never connects to or submits to IBKR."""
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
JST = timezone(timedelta(hours=9))


def profile(path):
    raw = path.read_bytes()  # Snapshot a growing file; count only complete lines.
    lines = raw.splitlines(keepends=True)
    incomplete = bool(lines and not lines[-1].endswith(b"\n"))
    if incomplete:
        lines.pop()
    kinds, statuses, data_types, errors = Counter(), Counter(), Counter(), Counter()
    ids, times, days, symbols, evidence = set(), [], set(), {}, []
    duplicates = out_of_order = 0
    previous = None
    for line in lines:
        row = json.loads(line)
        at = datetime.fromisoformat(row["received_at"])
        days.add(at.astimezone(JST).date().isoformat())
        times.append(at)
        if previous is not None and at < previous:
            out_of_order += 1
        previous = at
        identity = row.get("event_id")
        if identity is not None:
            duplicates += identity in ids
            ids.add(identity)
        kind = row.get("type", row.get("callback"))
        kinds[kind] += 1
        data = row.get("data", {})
        if kind == "error":
            errors[str(data.get("code"))] += 1
        if kind == "calibration_table":
            evidence.append(data)
        if kind == "quote":
            statuses[str(data.get("market_status"))] += 1
            data_types[str(data.get("market_data_type"))] += 1
            symbol = data["symbol"]
            state = symbols.setdefault(symbol, {"quotes": 0, "times": set(),
                                                 "first": at, "last": at})
            state["quotes"] += 1
            state["times"].add(at)
            state["first"] = min(state["first"], at)
            state["last"] = max(state["last"], at)
    first, last = (min(times), max(times)) if times else (None, None)
    return {
        "source": str(path.relative_to(ROOT)),
        "snapshot_bytes": len(raw),
        "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
        "complete_rows": len(lines), "excluded_incomplete_tail": incomplete,
        "types": dict(kinds), "duplicate_event_ids": duplicates,
        "out_of_order_received_at": out_of_order,
        "first_jst": first.astimezone(JST).isoformat() if first else None,
        "last_jst": last.astimezone(JST).isoformat() if last else None,
        "span_minutes": round((last-first).total_seconds()/60, 2) if first else 0,
        "observed_dates_jst": sorted(days),
        "quote_market_status": dict(statuses), "quote_data_type": dict(data_types),
        "error_codes": dict(errors), "calibration_tables": evidence,
        "symbols": {symbol: {"quotes": state["quotes"],
                     "distinct_quote_timestamps": len(state["times"]),
                     "first_jst": state["first"].astimezone(JST).isoformat(),
                     "last_jst": state["last"].astimezone(JST).isoformat()}
                    for symbol, state in sorted(symbols.items())},
    }


if __name__ == "__main__":
    paths = [ROOT/"runs"/directory/filename
             for directory in ("capture-jp7", "capture-jp7-pm")
             for filename in ("events.jsonl", "raw-callbacks.jsonl")]
    captures = [profile(path) for path in paths]
    events = [row for row in captures if row["source"].endswith("events.jsonl")]
    dates = sorted({day for row in events for day in row["observed_dates_jst"]})
    config = json.loads((ROOT/"runs/paper-jp7/config.json").read_text(encoding="utf-8"))
    paper = profile(ROOT/"runs/paper-jp7/events.jsonl")
    result = {"checked_at_jst": datetime.now(JST).isoformat(),
              "scope": "Local actual captures only; demo/audit fixtures excluded from real history",
              "actual_observed_dates": dates,
              "actual_capture_span_minutes_sum": round(sum(row["span_minutes"] for row in events), 2),
              "capture_profiles": captures,
              "paper_config": {"mode": config["mode"], "profile": config["profile"],
                  "min_samples": config["engine"]["min_samples"],
                  "min_independent_days": config["engine"]["min_independent_days"],
                  "rvol_min_days": config["features"]["rvol_min_days"],
                  "model_version": config["engine"]["model_version"],
                  "fee_version": config["commissions"]["version"]},
              "paper_input_types": paper["types"],
              "paper_calibration_tables": paper["calibration_tables"],
              "limitations": ["Partial intraday sessions; row counts are not independent samples",
                  "Normalized quotes are filtered emissions, not full exchange event coverage",
                  "No claim of positive net expectancy or live execution readiness"]}
    for row in events:
        assert sum(state["quotes"] for state in row["symbols"].values()) == row["types"].get("quote", 0)
        assert sum(row["quote_market_status"].values()) == row["types"].get("quote", 0)
    destination = Path(__file__).with_name("data-readiness.json")
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
