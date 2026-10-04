"""Independent round-2 entry-policy probes. No production file is modified."""
import json
import sys
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tests.test_review_fixes import fixture, at
from tests.test_engine import fixture_document, SYMBOL, T
from ibkr_microalpha.domain import MarketRegime, Quote, Regime
from ibkr_microalpha.economics import CalibrationRow, CalibrationTable


def temporary_price_cap():
    test = fixture()
    e = test.engine
    e.forecasts[SYMBOL] = replace(e.forecasts[SYMBOL], max_entry_price=D('3000'))
    for second in (0, 5, 10):
        test.tick(second)
    candidate = e.alpha.current(SYMBOL)
    assert candidate is not None
    result = {'candidate_after_economic_rejection': candidate.candidate_id,
              'expires_at': candidate.expires_at.isoformat(),
              'price_cap': str(e.entries._caps[candidate.candidate_id])}
    e.poll(at(10))
    result['candidate_after_same_time_poll'] = e.alpha.current(SYMBOL)
    test.tick(11, bid='2999.5', ask='3000')
    result['candidate_after_price_recovers_in_ttl'] = e.alpha.current(SYMBOL)
    result['invalidations'] = [x for x in e.audit if x['kind'] == 'CANDIDATE_INVALIDATED']
    assert result['candidate_after_same_time_poll'] is None
    assert result['candidate_after_price_recovers_in_ttl'] is None
    return result


def quantity_specific_cap():
    doc = fixture_document()
    doc['engine'].update(economics_source='calibration', max_entry_quantity=200)
    doc['risk']['max_quantity'] = 100
    test = fixture(doc)
    e = test.engine
    c = e.config
    rows = [CalibrationRow(policy_id=c.policy_id, version=c.model_version,
                           holding_seconds=c.holding_seconds, quantity=q,
                           score_low=-100.0, score_high=None, sample_days=20,
                           sample_count=100, mean_net_amount=D('2000'),
                           lower_net_amount=D('1500'), max_chase_ticks=chase)
            for q, chase in ((100, 0), (200, 10))]
    e.set_calibration(CalibrationTable(rows, known_at=T-timedelta(days=1), version=c.model_version), T)
    test.tick(0)
    snapshot = test.prepared_snapshot(0)
    quote = Quote(SYMBOL, T, D('3000'), D('3001'), 1000, 1000, 'quantity-cap', bid_at=T, ask_at=T)
    e.market_state = MarketRegime.MARKET_OK
    candidate = e.alpha.evaluate(snapshot, quote, Regime.LONG, e.market_state, T)
    assert candidate is not None
    plan, reason = e.entries.plan(candidate, snapshot, quote, T)
    assert plan is not None, reason
    row = e.calibration.lookup(policy_id=c.policy_id, version=c.model_version,
                              holding_seconds=c.holding_seconds, quantity=plan.quantity,
                              score=candidate.entry_score, at=T)
    expected_cap = e.ticks.move_ticks(candidate.reference_ask, row.max_chase_ticks, T)
    result = {'requested_quantity': 200, 'allocated_quantity': plan.quantity,
              'selected_row_max_chase_ticks': row.max_chase_ticks,
              'selected_row_cap': str(expected_cap), 'actual_plan_cap': str(plan.max_price),
              'actual_limit': str(plan.limit_price)}
    assert plan.quantity == 100 and plan.max_price > expected_cap and plan.limit_price > expected_cap
    e.risk.release(plan.reservation_key)
    return result


if __name__ == '__main__':
    results = {'temporary_price_cap': temporary_price_cap(), 'quantity_specific_cap': quantity_specific_cap()}
    (Path(__file__).parent/'entry-evidence.json').write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(results, indent=2, ensure_ascii=False))
