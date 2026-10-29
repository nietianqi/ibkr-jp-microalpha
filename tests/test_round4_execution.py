"""Round-four execution and causal market-control regressions."""
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from ibkr_microalpha.domain import OrderState, Quote, Side
from ibkr_microalpha.execution import ExecutionBook
from ibkr_microalpha.market import JST, TickTable
from tests.test_engine import SYMBOL, T
from tests.test_review_fixes import at, fixture


class ReconcileBoundaryTests(unittest.TestCase):
    def test_confirmation_flags_cannot_use_truthy_nonbooleans(self):
        for field in ('complete', 'ownership_confirmed'):
            for invalid in ('false', 'true', 1, 0, None, [], {}):
                with self.subTest(field=field, invalid=invalid):
                    book = ExecutionBook()
                    fields = {'complete': True, 'ownership_confirmed': True}
                    fields[field] = invalid
                    self.assertFalse(book.reconcile({}, [], [], T, **fields))
                    self.assertFalse(book.reconciled)
                    self.assertTrue(book.locked)

    def test_noninteger_identity_metadata_does_not_unlock_or_mutate_fills(self):
        for invalid in (True, '2', 2.0, 0, -1):
            with self.subTest(next_order_id=invalid):
                book = ExecutionBook()
                self.assertFalse(book.reconcile({}, [], [], T, complete=True,
                                                ownership_confirmed=True, next_order_id=invalid))
                self.assertFalse(book.reconciled)
        for invalid in (True, D('1.5'), 'nan'):
            book = ExecutionBook()
            self.assertFalse(book.reconcile({'S': invalid}, [], [], T,
                                            complete=True, ownership_confirmed=True))
        for invalid in (True,'1',1.0):
            book=ExecutionBook()
            book.reconcile({},[],[],T,complete=True,ownership_confirmed=True)
            order=book.submit('intent','S',Side.BUY,100,D('100'),T)
            execution={'exec_id':'bad-id','order_id':invalid,'qty':100,'price':'100','at':T}
            self.assertFalse(book.reconcile({'S':100},[],[execution],T,
                                            complete=True,ownership_confirmed=True))
            self.assertNotIn('bad-id',book.executions)
            self.assertIsNone(order.submitted_at)


class LowestLegalExitTests(unittest.TestCase):
    def test_minimum_tick_is_verified_for_date_and_security_category(self):
        ticks = TickTable()
        self.assertEqual(ticks.minimum_price(T, 'TOPIX500'), D('.1'))
        self.assertEqual(ticks.minimum_price(T, 'OTHER'), D('1'))
        with self.assertRaises(ValueError):
            ticks.minimum_price(T, 'UNKNOWN')
        with self.assertRaises(ValueError):
            ticks.minimum_price(T.replace(year=2030), 'TOPIX500')

    def test_sell_limit_clamps_only_to_legal_positive_verified_bounds(self):
        ticks = TickTable()
        self.assertEqual(ticks.sell_limit_price(D('.1'), 4, T), D('.1'))
        self.assertEqual(ticks.sell_limit_price(D('1000.5'), 2, T), D('999.9'))
        self.assertEqual(ticks.sell_limit_price(D('90'), 10, T, lower=D('89.9'), upper=D('110')), D('89.9'))
        for kwargs in ({'lower':D('89.95')}, {'upper':D('80')}, {'lower':D('91')}):
            with self.assertRaises(ValueError):
                ticks.sell_limit_price(D('90'), 2, T, **kwargs)

    def test_stop_at_minimum_legal_bid_creates_covered_sell(self):
        test = fixture()
        test.fill_entry()
        e = test.engine
        e.quotes[SYMBOL] = Quote(SYMBOL, at(14), D('.1'), D('.2'), 100, 100,
                                 'minimum', bid_at=at(14), ask_at=at(14))
        e._request_exit(SYMBOL, at(14), 'STOP_LOSS', True)
        sell = test.sells()[0]
        self.assertEqual((sell.quantity, sell.limit_price), (100, D('.1')))
        self.assertNotIn('exit_price_rule_invalid', e.risk.lock_reasons)
        e._request_exit(SYMBOL, at(14), 'STOP_LOSS', True)
        self.assertEqual(len(test.sells()), 1)

    def test_halt_does_not_create_sell_even_at_legal_minimum(self):
        test = fixture()
        test.fill_entry()
        e = test.engine
        e.quotes[SYMBOL] = Quote(SYMBOL, at(14), D('.1'), D('.2'), 100, 100,
                                 'halt', market_status='HALT', bid_at=at(14), ask_at=at(14))
        e._request_exit(SYMBOL, at(14), 'STOP_LOSS', True)
        self.assertFalse(test.sells())
        self.assertEqual(e.book.positions[SYMBOL].quantity, 100)

    def test_minimum_price_does_not_relax_sell_cover_or_cancel_barrier(self):
        test=fixture(); test.fill_entry(); e=test.engine
        e.quotes[SYMBOL]=Quote(SYMBOL,at(14),D('.1'),D('.2'),100,100,
                               'floor',bid_at=at(14),ask_at=at(14))
        e._request_exit(SYMBOL,at(14),'STOP_LOSS',True)
        sell=test.sells()[0]
        e.book.drain_commands(at(14))
        e.book.cancel(sell.order_id,at(14))
        e.book.drain_commands(at(14))
        e.book.status(sell.order_id,'Cancelled',0,0,at(14))
        e._request_exit(SYMBOL,at(14),'STOP_LOSS',True)
        self.assertEqual(len(test.sells()),1)
        self.assertEqual(e.book.sellable_quantity(SYMBOL),0)
        self.assertTrue(e.book.reconcile({SYMBOL:100},[],[],at(14),complete=True,
                                         ownership_confirmed=True))
        e._request_exit(SYMBOL,at(14),'STOP_LOSS',True)
        self.assertEqual(len(test.sells()),2)
        self.assertEqual(test.sells()[1].limit_price,D('.1'))


class MarketControlTests(unittest.TestCase):
    def setUp(self):
        from ibkr_microalpha.market_controls import MarketControls, PriceBand, InstrumentRule, SymbolStatus
        self.controls = MarketControls()
        self.ticks = TickTable()
        self.band = PriceBand(SYMBOL,T.astimezone(JST).date(),D('100'),D('90'),D('110'),
                              T-timedelta(hours=1),'authoritative-fixture','v1')
        self.rule = InstrumentRule(SYMBOL,T-timedelta(days=1),T+timedelta(days=1),
                                   T-timedelta(days=2),'TOPIX500','TSE',100,'fixture','r1')
        self.status = SymbolStatus(SYMBOL,'CONTINUOUS',T-timedelta(seconds=1),
                                   T+timedelta(seconds=30),'exchange-fixture','s1')
        self.controls.add_price_band(self.band,self.ticks,tick_category='TOPIX500')
        self.controls.add_instrument_rule(self.rule,self.ticks)
        self.controls.add_status(self.status)

    def gate(self, price='100', stop='2', now=T):
        return self.controls.entry_gate(SYMBOL,now,D(price),D(stop),ticks=self.ticks,
                                         tick_category='TOPIX500',upper_stop_multiple=D('2'))

    def test_invalid_band_quote_cannot_be_used_for_account_valuation(self):
        self.assertEqual(self.controls.quote_gate(SYMBOL,T,D('89'),D('100'),ticks=self.ticks,
            tick_category='TOPIX500'),'quote_outside_authoritative_band')
        self.assertIsNone(self.controls.quote_gate(SYMBOL,T,D('99'),D('100'),ticks=self.ticks,
            tick_category='TOPIX500'))

    def test_daily_bands_are_causal_and_expire_without_carrying_yesterday(self):
        self.assertIsNone(self.controls.resolve_band(SYMBOL,self.band.known_at-timedelta(seconds=1)))
        self.assertEqual(self.controls.resolve_band(SYMBOL,T),self.band)
        self.assertIsNone(self.controls.resolve_band(SYMBOL,T+timedelta(days=1)))
        with self.assertRaises(ValueError):
            self.controls.add_price_band(replace(self.band,lower=D('90.05')),self.ticks,tick_category='TOPIX500')
        with self.assertRaises(ValueError):
            self.controls.add_price_band(replace(self.band,source=''),self.ticks,tick_category='TOPIX500')

    def test_future_band_revision_cannot_rewrite_an_earlier_gate(self):
        future = replace(self.band,upper=D('120'),known_at=T+timedelta(seconds=1),version='v2')
        self.controls.add_price_band(future,self.ticks,tick_category='TOPIX500')
        self.assertEqual(self.gate(price='107'),'upper_limit_stop_buffer')
        self.assertIsNone(self.gate(price='107',now=T+timedelta(seconds=1)))

    def test_unknown_expired_status_price_bounds_and_upper_buffer_block_entries(self):
        self.assertIsNone(self.gate())
        self.assertEqual(self.gate(price='111'),'price_outside_band')
        self.assertEqual(self.gate(price='107'),'upper_limit_stop_buffer')
        self.assertIsNone(self.gate(price='106'))
        self.assertEqual(self.gate(now=T+timedelta(seconds=30)),'symbol_status_unknown_or_expired')
        self.controls.add_status(replace(self.status,status='SPECIAL_QUOTE',known_at=T,version='special'))
        self.assertEqual(self.gate(),'symbol_not_continuous:SPECIAL_QUOTE')

    def test_event_window_applies_only_after_known_time_and_inside_window(self):
        from ibkr_microalpha.market_controls import RestrictedWindow
        self.controls.add_window(RestrictedWindow(SYMBOL,T,T+timedelta(seconds=10),
                                                  T+timedelta(seconds=1),'issuer','w1','earnings'))
        self.assertIsNone(self.gate())
        self.assertEqual(self.gate(now=T+timedelta(seconds=1)),'restricted_window:earnings')
        self.assertIsNone(self.gate(now=T+timedelta(seconds=10)))

    def test_instrument_rule_uses_effective_interval_and_known_time(self):
        future = replace(self.rule,effective_from=T,known_at=T+timedelta(seconds=1),
                         tick_category='OTHER',lot_size=10,version='r2')
        self.controls.add_instrument_rule(future,self.ticks)
        self.assertEqual(self.controls.resolve_rule(SYMBOL,T),self.rule)
        self.assertEqual(self.controls.resolve_rule(SYMBOL,T+timedelta(seconds=1)),future)
        self.assertEqual(self.gate(now=T+timedelta(seconds=1)),'instrument_rule_mismatch')

    def test_expired_new_facts_do_not_resurrect_old_coverage(self):
        self.controls.add_price_band(replace(self.band,known_at=T,valid_until=T+timedelta(seconds=1),
                                             version='short-reference'),self.ticks,tick_category='TOPIX500')
        self.assertIsNone(self.controls.resolve_band(SYMBOL,T+timedelta(seconds=1)))
        self.controls.add_status(replace(self.status,known_at=T,valid_until=T+timedelta(seconds=1),
                                         version='short-status'))
        self.assertIsNone(self.controls.resolve_status(SYMBOL,T+timedelta(seconds=1)))
        self.controls.add_instrument_rule(replace(self.rule,effective_from=T,known_at=T,
                                                  effective_until=T+timedelta(seconds=1),version='short-rule'),self.ticks)
        self.assertIsNone(self.controls.resolve_rule(SYMBOL,T+timedelta(seconds=1)))

    def test_lot_venue_and_conflicting_publications_fail_closed(self):
        self.assertEqual(self.controls.entry_gate(SYMBOL,T,D('100'),D('2'),ticks=self.ticks,
                                                  tick_category='TOPIX500',quantity=10),'instrument_lot_mismatch')
        with self.assertRaises(ValueError):
            self.controls.add_status(replace(self.status,status='HALT'))
        with self.assertRaises(ValueError):
            self.controls.add_price_band(replace(self.band,upper=D('120')),self.ticks,tick_category='TOPIX500')
        self.controls.add_instrument_rule(replace(self.rule,effective_from=T,known_at=T,
                                                  venue='UNVERIFIED',version='unsupported'),self.ticks)
        self.assertEqual(self.gate(),'instrument_venue_unsupported')

    def test_explicit_tsej_is_supported_but_smart_is_not_a_verified_venue(self):
        self.controls.add_instrument_rule(replace(self.rule,effective_from=T,known_at=T,
                                                  venue='TSEJ',version='ibkr-venue'),self.ticks)
        self.assertIsNone(self.gate())
        self.controls.add_instrument_rule(replace(self.rule,effective_from=T,known_at=T+timedelta(seconds=1),
                                                  venue='SMART',version='route-unknown'),self.ticks)
        self.assertEqual(self.gate(now=T+timedelta(seconds=1)),'instrument_venue_unsupported')

    def test_exit_uses_band_floor_and_halt_retains_unfilled_risk(self):
        self.assertEqual(self.controls.exit_price(SYMBOL,T,D('90'),10,ticks=self.ticks,
                                                  tick_category='TOPIX500'),(D('90'),None))
        self.assertEqual(self.controls.exit_price(SYMBOL,T,D('89.9'),1,ticks=self.ticks,
                                                  tick_category='TOPIX500'),(None,'price_outside_band'))
        self.controls.add_status(replace(self.status,status='HALT',known_at=T,version='halt'))
        self.assertEqual(self.controls.exit_price(SYMBOL,T,D('90'),1,ticks=self.ticks,
                                                  tick_category='TOPIX500'),(None,'symbol_not_continuous:HALT'))

    def test_position_manager_observes_controls_and_retains_residual_alarm(self):
        test = fixture(); test.fill_entry(); e=test.engine
        e.market_controls=self.controls
        e.quotes[SYMBOL]=Quote(SYMBOL,at(14),D('90'),D('90.1'),100,100,'floor',bid_at=at(14),ask_at=at(14))
        e._request_exit(SYMBOL,at(14),'STOP_LOSS',True)
        sell=test.sells()[0]
        self.assertEqual(sell.limit_price,D('90'))
        self.assertEqual(e.book.positions[SYMBOL].quantity,100)  # limit is not a fill guarantee

    def test_status_blocks_exit_once_and_event_windows_never_block_risk_exit(self):
        from ibkr_microalpha.market_controls import RestrictedWindow
        self.controls.add_window(RestrictedWindow(SYMBOL,T,T+timedelta(hours=1),T,'issuer','w1','earnings'))
        self.assertEqual(self.controls.exit_price(SYMBOL,T,D('90'),1,ticks=self.ticks,
                                                  tick_category='TOPIX500'),(D('90'),None))
        test=fixture(); test.fill_entry(); e=test.engine; e.market_controls=self.controls
        self.controls.add_status(replace(self.status,status='HALT',known_at=T,version='halt'))
        e.quotes[SYMBOL]=Quote(SYMBOL,at(14),D('90'),D('90.1'),100,100,'halt-control',bid_at=at(14),ask_at=at(14))
        e._request_exit(SYMBOL,at(14),'STOP_LOSS',True)
        e._request_exit(SYMBOL,at(14),'STOP_LOSS',True)
        self.assertFalse(test.sells())
        alarms=[a for a in e.alarms if a['alarm']=='RESIDUAL_RISK_EXIT_BLOCKED']
        self.assertEqual(len(alarms),1)
        self.assertEqual(alarms[0]['reason'],'symbol_not_continuous:HALT')


if __name__ == '__main__':
    unittest.main()
