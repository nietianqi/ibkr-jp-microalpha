"""Replay the original fixture up to an active entry; inject lost trade health."""
import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.domain import Side
from ibkr_microalpha.replay import Replay


if __name__ == '__main__':
    runner = Replay(build_engine(load_config(ROOT / 'runs/demo/frozen-config.json')))
    with (ROOT / 'runs/demo/raw-input.jsonl').open(encoding='utf-8') as source:
        for line in source:
            event = json.loads(line)
            if event['type'] == 'fill':
                break
            runner.dispatch(event)
    engine = runner.engine
    buy = next(o for o in engine.book.active_orders() if o.side == Side.BUY)
    at = runner.last_key[0] + timedelta(milliseconds=100)
    before = str(buy.state)
    runner.dispatch({'event_id': 'audit-lost-stream-health',
        'received_at': at.isoformat(), 'sequence': 999999, 'type': 'stream_health',
        'data': {'symbol': buy.symbol, 'healthy': False, 'source': 'SAMPLED'}})
    result = {'original_fixture_events_before_injection': runner.events_processed - 1,
        'before': before, 'after': str(buy.state),
        'trade_stream_healthy': engine.features.trade_stream_healthy(buy.symbol, at),
        'fresh_snapshot_valid': engine.features.snapshot(buy.symbol, at).valid,
        'cached_snapshot_valid': engine.snapshots[buy.symbol].valid,
        'pending_commands': [c.kind for c in engine.book.commands],
        'risk_locks': sorted(engine.risk.lock_reasons),
        'book_locks': sorted(engine.book.lock_reasons)}
    Path(__file__).with_name('health-propagation.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
