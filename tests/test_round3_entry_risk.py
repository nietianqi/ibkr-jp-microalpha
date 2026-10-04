"""Round-three regressions for funds, send budgets, abnormal positions and caps."""
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from ibkr_microalpha.domain import MarketRegime, OrderState, Quote, Regime, Side
from ibkr_microalpha.economics import CalibrationRow, CalibrationTable
from ibkr_microalpha.replay import Replay
from ibkr_microalpha.risk import AccountSnapshot
from tests.test_engine import SYMBOL, T, fixture_document
from tests.test_review_fixes import at, fixture


def funds(engine, second, amount, *, covered=(), account='U1'):
    fields = {}
    if 'ledger_sequence' in AccountSnapshot.__dataclass_fields__:
        fields = {'ledger_sequence': engine.book.journal[-1]['sequence'],
                  'covered_order_ids': tuple(covered)}
    return AccountSnapshot(account, 'JPY', D(amount), D(amount), at(second), 'broker', **fields)


class FundsAndEntryRegressions(unittest.TestCase):
    def test_EXE09_snapshot_funds_cannot_be_used_again_after_a_buy(self):
        test = fixture()
        e = test.engine
        e.set_account(funds(e, 0, '350000'), at(0))
        test.fill_entry()
        e.instruments['SECOND'] = e.instruments[SYMBOL]
        decision = e.risk.allocate('second', 'SECOND', 'synthetic', D('3001'), D('5'), D('1'), 100, 100,
                                  lambda q,p:(e.commissions.commission(q*p), e.commissions.commission(q*p)))
        self.assertEqual(e.risk.cash, D('49659.9200'))
        self.assertFalse(decision.allowed)

    def test_EXE09_missing_or_wrong_barrier_never_claims_reconciled_funds(self):
        test = fixture()
        test.tick(0)
        e = test.engine
        e.set_account(AccountSnapshot('U1', 'JPY', D('350000'), D('350000'), at(1), 'broker'), at(1))
        self.assertTrue(e.risk.entries_blocked)
        self.assertNotEqual(e.valuation.cash_source, 'reconciled')

    def test_EXE09_covered_working_buy_is_not_reserved_twice(self):
        test = fixture()
        buy = test.working_entry()
        e = test.engine
        e.set_account(funds(e, 12, '49000', covered=(buy.order_id,)), at(12))
        self.assertEqual(e.risk.cash, D('49000'))
        e.on_fill(exec_id='covered-partial', order_id=buy.order_id, qty=40, price=D('3001'), at=at(13))
        # Its principal was already deducted by the broker. Only unreported fees
        # are conservatively reserved; cancellation does not invent cash credit.
        self.assertEqual(e.risk.cash, D('49000') - e.unreported_fee_reserve)

    def test_EXE09_account_change_and_uncovered_transmitted_buy_are_blocked(self):
        test = fixture()
        e = test.engine
        e.set_account(funds(e, 0, '350000'), at(0))
        buy = test.working_entry()
        e.set_account(funds(e, 12, '49000'), at(12))  # missing a broker commitment
        self.assertTrue(e.risk.entries_blocked)
        e.set_account(funds(e, 12, '49000', covered=(buy.order_id,), account='DIFFERENT'), at(12))
        self.assertTrue(e.risk.entries_blocked)

    def test_EXE09_cancelled_covered_reserve_is_released_only_by_new_funds_barrier(self):
        test = fixture()
        buy = test.working_entry()
        e = test.engine
        e.set_account(funds(e, 12, '49000', covered=(buy.order_id,)), at(12))
        e.book.cancel(buy.order_id, at(13))
        e.book.drain_commands(at(13))
        e.book.status(buy.order_id, 'Cancelled', 0, 0, at(13))
        e.book.reconcile({}, [], [], at(13), complete=True, ownership_confirmed=True)
        e.poll(at(13))
        self.assertEqual(e.risk.cash, D('49000'))
        e.set_account(funds(e, 13, '350000'), at(13))
        self.assertEqual(e.risk.cash, D('350000'))

    def test_EXE09_actual_fees_and_bust_do_not_reuse_spent_funds(self):
        test = fixture()
        e = test.engine
        e.set_account(funds(e, 0, '350000'), at(0))
        buy = test.fill_entry()
        e.book.commission('buy-exec', D('300'))
        e.poll(at(13))
        self.assertEqual(e.risk.cash, D('49600'))
        e.on_fill(exec_id='bust-buy', order_id=buy.order_id, qty=0, price=D('3001'),
                  at=at(14), correction_of='buy-exec')
        e.book.reconcile({}, [], [], at(14), complete=True, ownership_confirmed=True)
        e.poll(at(14))
        self.assertEqual(e.risk.cash, D('49600'))

    def test_EXE09_stale_sequence_is_not_a_funds_barrier(self):
        test = fixture()
        test.tick(0)
        e = test.engine
        snapshot = replace(funds(e, 1, '350000'), ledger_sequence=0)
        e.set_account(snapshot, at(1))
        self.assertIn('account_snapshot_unreconciled', e.risk.soft_blocks)

    def test_EXE10_new_balance_aborts_the_original_unsent_buy(self):
        test = fixture()
        for second in (0,5,10,11,12):
            test.tick(second)
        e = test.engine
        buy = next(o for o in e.book.orders.values() if o.side == Side.BUY)
        e.set_account(funds(e, 12.5, '1000'), at(12.5))
        runner = Replay(e)
        runner.dispatch({'event_id':'cash-cut-send', 'received_at':at(12.5).isoformat(),
                         'sequence':1, 'type':'requests', 'data':{}})
        self.assertFalse(any(c.kind == 'SUBMIT' and c.order_id == buy.order_id for c in runner.last_commands))
        self.assertIsNone(buy.submitted_at)
        self.assertEqual(buy.state, OrderState.CANCELLED)

    def test_EXE10_send_rechecks_portfolio_stress_without_changing_quantity(self):
        test = fixture()
        for second in (0,5,10,11,12):
            test.tick(second)
        e = test.engine
        buy = next(o for o in e.book.orders.values() if o.side == Side.BUY)
        e.instruments['SECOND'] = e.instruments[SYMBOL]
        other = e.book.submit('external', 'SECOND', Side.BUY, 100, D('3001'), at(12), stop_distance=D('200'))
        e.book.fill('other-fill', other.order_id, 100, D('3001'), at(12))
        e.quotes['SECOND'] = replace(e.quotes[SYMBOL], symbol='SECOND', event_id='other-quote')
        self.assertIsNotNone(e.entry_still_valid(buy, at(12)))
        self.assertEqual(buy.quantity, 100)

    def test_EXE09_EXE10_queued_buys_share_one_budget_and_abort_releases_local_commitment(self):
        for balance, expected_ids, expected_cash in (
                ('350000', [2], D('49559.8400')),
                ('650000', [1, 2], D('49119.6800'))):
            with self.subTest(balance=balance):
                test = fixture()
                for second in (0, 5, 10, 11, 12):
                    test.tick(second)
                e = test.engine
                first = next(o for o in e.book.orders.values() if o.side == Side.BUY)
                e.instruments['SECOND'] = e.instruments[SYMBOL]
                e.quotes['SECOND'] = replace(e.quotes[SYMBOL], symbol='SECOND', event_id='second-q')
                candidate = replace(e.alpha.current(SYMBOL), symbol='SECOND', candidate_id='second-intent')
                e.alpha._active['SECOND'] = candidate
                e.entries._caps[candidate.candidate_id] = e.entries._caps[first.intent_key]
                e.forecasts['SECOND'] = e.forecasts[SYMBOL]
                e.book.submit(candidate.candidate_id, 'SECOND', Side.BUY, first.quantity, first.limit_price,
                              at(12), stop_distance=first.stop_distance,
                              candidate_expires_at=first.candidate_expires_at)
                e.set_account(funds(e, 12, balance), at(12))
                commands = e.book.drain_commands(at(12), entry_validator=e.entry_still_valid)
                self.assertEqual([c.order_id for c in commands if c.kind == 'SUBMIT'], expected_ids)
                self.assertEqual(e.risk.cash, expected_cash)
                self.assertEqual(first.quantity, 100)
                self.assertFalse(e.risk.locked)
                if balance == '350000':
                    self.assertIsNone(first.submitted_at)
                    self.assertEqual(first.state, OrderState.CANCELLED)
                self.assertNotIn('entry_cash_overcommitted', e.risk.soft_blocks)

    def test_EXE11_late_sell_after_cover_cancel_keeps_risk_loop_alive(self):
        test = fixture()
        buy = test.fill_entry()
        e = test.engine
        test.tick(14)
        e._request_exit(SYMBOL, at(14), 'AUDIT_EXIT', True)
        sell = test.sells()[0]
        e.book.drain_commands(at(14))
        e.book.status(sell.order_id, 'Submitted', 0, 100, at(14))
        e.on_fill(exec_id='buy-correction', order_id=buy.order_id, qty=50, price=D('3001'),
                  at=at(15), correction_of='buy-exec')
        e.on_fill(exec_id='late-sell', order_id=sell.order_id, qty=100, price=D('3000'), at=at(16))
        self.assertEqual(e.book.positions[SYMBOL].quantity, -50)
        self.assertIn('late-sell', e.book.executions)
        self.assertTrue(e.risk.locked)
        self.assertIsNone(e.daily_net_pnl_estimate)
        alarms = [a for a in e.alarms if a['alarm'] == 'UNCONTROLLED_SHORT']
        self.assertEqual(len(alarms), 1)
        e.poll(at(16))
        self.assertEqual(len([a for a in e.alarms if a['alarm'] == 'UNCONTROLLED_SHORT']), 1)
        self.assertEqual([o.order_id for o in e.book.orders.values() if o.side == Side.SELL], [sell.order_id])

    def test_EXE11_unquoted_short_does_not_skip_another_positions_exit(self):
        test = fixture()
        test.tick(0)
        e = test.engine
        e.instruments['SECOND'] = e.instruments[SYMBOL]
        for symbol in (SYMBOL,'SECOND'):
            buy = e.book.submit('abnormal:'+symbol, symbol, Side.BUY, 100, D('3001'), at(0), stop_distance=D('5'))
            e.book.fill('b:'+symbol,buy.order_id,100,D('3001'),at(0))
        sell = e.book.submit('late-sell', SYMBOL, Side.SELL,100,D('3000'),at(0),emergency=True)
        e.book.fill('correction', 1,50,D('3001'),at(0),correction_of='b:'+SYMBOL)
        e.book.fill('late',sell.order_id,100,D('3000'),at(0))
        e.quotes.pop(SYMBOL)
        e.quotes['SECOND'] = Quote('SECOND',at(0),D('3000'),D('3001'),100,100,'fresh',bid_at=at(0),ask_at=at(0))
        e.poll(at(0))
        self.assertEqual(e.book.positions[SYMBOL].quantity,-50)
        self.assertIsNone(e.daily_net_pnl_estimate)
        # A locked inconsistent ledger may correctly refuse a new risk sell,
        # but the second symbol's risk exit must still be attempted/audited.
        self.assertIn('SECOND', e.positions.exits)

    def test_FLOW05_unsubmitted_candidate_can_recover_inside_original_ttl(self):
        test = fixture()
        e = test.engine
        e.forecasts[SYMBOL] = replace(e.forecasts[SYMBOL], max_entry_price=D('3000'))
        for second in (0,5,10):
            test.tick(second)
        candidate = e.alpha.current(SYMBOL)
        self.assertIsNotNone(candidate)
        e.poll(at(10))
        self.assertIs(e.alpha.current(SYMBOL), candidate)
        for second in (11,12,13):
            test.tick(second, bid='2999.5', ask='3000')
        self.assertEqual(e.funnel['intents'], 1)
        self.assertEqual(candidate.expires_at, at(30))

    def test_FLOW06_selected_quantity_uses_its_own_price_policy(self):
        doc = fixture_document()
        doc['engine'].update(economics_source='calibration', max_entry_quantity=200)
        doc['risk']['max_quantity'] = 100
        test = fixture(doc)
        e = test.engine
        c = e.config
        extra = {}
        if 'label_source' in CalibrationRow.__dataclass_fields__:
            extra = {'label_source':'ARTIFICIAL', 'provenance':{'source':'ARTIFICIAL_FIXTURE'}}
        rows = [CalibrationRow(c.policy_id,c.model_version,c.holding_seconds,q,-100,None,40,100,
                               D('2000'),D('1500'),chase, **extra) for q,chase in ((100,0),(200,10))]
        e.set_calibration(CalibrationTable(rows, known_at=T-timedelta(days=1), version='ARTIFICIAL-v1'), T)
        test.tick(0)
        snapshot = test.prepared_snapshot(0)
        quote = Quote(SYMBOL,T,D('3000'),D('3001'),1000,1000,'q',bid_at=T,ask_at=T)
        candidate = e.alpha.evaluate(snapshot,quote,Regime.LONG,MarketRegime.MARKET_OK,T)
        plan, reason = e.entries.plan(candidate,snapshot,quote,T)
        self.assertIsNotNone(plan, reason)
        self.assertEqual(plan.quantity, 100)
        self.assertEqual(plan.max_price, D('3001'))
        self.assertEqual(plan.limit_price, D('3001'))
        e.risk.release(plan.reservation_key)


if __name__ == '__main__':
    unittest.main()
