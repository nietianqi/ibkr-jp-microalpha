"""Audit probe: malformed precomputed validity flags, using synthetic fixtures."""
import json
import sys
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ibkr_microalpha.domain import FeatureSnapshot, Quote, Side
from tests.test_engine import EngineIntegrationTests, SYMBOL, T


def scenario(flag):
    fixture = EngineIntegrationTests()
    fixture.setUp()
    engine = fixture.engine
    for second in (0, 5, 10, 11, 12):
        at = T + timedelta(seconds=second)
        engine.quotes[SYMBOL] = Quote(SYMBOL, at, D('3000'), D('3001'),
            900 + second, 100, f'flag-{second}', bid_at=at, ask_at=at)
        engine.set_market(FeatureSnapshot('MARKET', at,
            dict(rv_mkt=1, spread_bps=2, breadth=.8), True), at)
        engine.evaluate(replace(fixture.prepared_snapshot(second), valid=flag), at)
    return {'input_valid': flag, 'accepted_by_constructor': True,
        'buy_orders': len([o for o in engine.book.orders.values() if o.side == Side.BUY]),
        'regime': str(engine.regime._states.get(SYMBOL))}


if __name__ == '__main__':
    result = {'boolean_false': scenario(False), 'string_false': scenario('false')}
    target = Path(__file__).with_name('snapshot-flags.json')
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
