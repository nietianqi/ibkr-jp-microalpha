"""Read-only cross-review probes for root-owned funds/send/quarantine fixes."""
import json
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from ibkr_microalpha.domain import Side
from tests.test_review_fixes import at, fixture
from tests.test_engine import SYMBOL
from tests.test_round3_entry_risk import funds


def two_queued(balance):
    test = fixture()
    for second in (0, 5, 10, 11, 12):
        test.tick(second)
    e = test.engine
    first = next(o for o in e.book.orders.values() if o.side == Side.BUY)
    second_symbol = 'SECOND'
    e.instruments[second_symbol] = e.instruments[SYMBOL]
    e.quotes[second_symbol] = replace(e.quotes[SYMBOL], symbol=second_symbol, event_id='second-q')
    candidate = replace(e.alpha.current(SYMBOL), symbol=second_symbol, candidate_id='second-intent')
    e.alpha._active[second_symbol] = candidate
    e.entries._caps[candidate.candidate_id] = e.entries._caps[first.intent_key]
    e.forecasts[second_symbol] = e.forecasts[SYMBOL]
    second = e.book.submit(candidate.candidate_id, second_symbol, Side.BUY, first.quantity,
                           first.limit_price, at(12), stop_distance=first.stop_distance,
                           candidate_expires_at=first.candidate_expires_at)
    e.set_account(funds(e, 12, balance), at(12))
    before = {'cash': str(e.risk.cash), 'blocks': sorted(e.risk.soft_blocks)}
    commands = e.book.drain_commands(at(12), entry_validator=e.entry_still_valid)
    submits = [c.order_id for c in commands if c.kind == 'SUBMIT']
    return {'balance': balance, 'before': before, 'sent_buy_ids': submits,
            'states': {o.symbol: o.state.value for o in (first, second)},
            'cash_after': str(e.risk.cash)}


if __name__ == '__main__':
    scarce = two_queued('350000')
    ample = two_queued('650000')
    assert len(scarce['sent_buy_ids']) == 1, scarce
    assert len(ample['sent_buy_ids']) == 2, ample
    assert D(scarce['cash_after']) >= 0 and D(ample['cash_after']) >= 0
    evidence = {'two_untransmitted_buy_funds': [scarce, ample],
                'lowest_tick_exit': 'Existing price-rule failure is caught; no minimum-tick clamp was added.'}
    output = Path(__file__).with_name('cross_probe.json')
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
