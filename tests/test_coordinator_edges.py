import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.domain import OrderState
from tests import test_engine as fixtures


class CoordinatorEdgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EngineIntegrationTests('runTest')
        self.fixture.setUp()
        self.engine = self.fixture.engine

    def test_T07_new_delayed_mode_immediately_cancels_existing_buy(self):
        buy = self.fixture.working_entry()
        at = fixtures.T+timedelta(seconds=13)
        quote = replace(self.engine.quotes[fixtures.SYMBOL], at=at, bid_at=at, ask_at=at,
                        event_id='delayed-now', market_data_type=3)
        self.engine.on_quote(quote)
        self.assertEqual(buy.state, OrderState.CANCEL_PENDING)
        self.assertIsNone(self.engine._valid_quote(fixtures.SYMBOL, at))

    def test_known_execution_time_preserves_causal_first_fill_snapshot_and_timer(self):
        buy = self.fixture.working_entry()
        self.engine.on_fill(exec_id='received-late', order_id=buy.order_id, qty=100, price=D('3001'),
            at=fixtures.T+timedelta(seconds=14), executed_at=fixtures.T+timedelta(seconds=12))
        position = self.engine.book.positions[fixtures.SYMBOL]
        self.assertEqual(position.entry_time, fixtures.T+timedelta(seconds=12))
        self.assertIsNotNone(position.entry_score)
        self.assertFalse(self.engine.risk.locked)

    def test_duplicate_fill_receipt_does_not_inflate_ledger_or_restart_timer(self):
        buy = self.fixture.fill_entry()
        self.engine.on_fill(exec_id='buy-exec', order_id=buy.order_id, qty=100, price=D('3001'),
                            at=fixtures.T+timedelta(seconds=14))
        self.assertEqual(len(self.engine.book.executions), 1)
        self.assertEqual(self.engine.book.positions[fixtures.SYMBOL].quantity, 100)
        self.assertEqual(len(self.engine.execution_quality.drifts()), 1)
        self.assertEqual(self.engine.book.positions[fixtures.SYMBOL].entry_time,
                         fixtures.T+timedelta(seconds=13))

    def test_config_forbids_live_mode_and_truthy_permission_strings(self):
        document = load_config(fixtures.CONFIG)
        document['mode'] = 'live'
        with self.assertRaises(ValueError):
            build_engine(document)
        document['mode'] = 'replay'
        document['instruments'][fixtures.SYMBOL]['permission_verified'] = 'false'
        with self.assertRaisesRegex(ValueError, 'boolean'):
            build_engine(document)

    def enhanced_config(self):
        document = deepcopy(load_config(fixtures.CONFIG))
        for group,key in (('engine','score_version'), ('features','version'), ('confirmation','version')):
            document[group][key] = 'enhanced-v1'
        document['features'].update(vwap_kind='TICK', trade_source='TBT')
        document['features']['required_features'] = [name.replace('vwap_proxy_', 'vwap_')
            for name in document['features']['required_features']] + ['ti_10','ti_60']
        document['confirmation'].update(enhanced=True, required_ti_features=['ti_10'])
        document['subscriptions'] = {'quota': 5, 'min_tenure_seconds': 120, 'plan_interval_seconds': 30,
            'required_windows': {'ti_10': 10, 'ti_60': 60, 'r_30': 30}}
        return document

    def test_T25_enhanced_configuration_cannot_omit_main_signal_ti60_window(self):
        document = self.enhanced_config()
        engine = build_engine(document)
        self.assertEqual(engine.subscriptions.requirements.required_windows['ti_60'], 60)
        del document['subscriptions']['required_windows']['ti_60']
        with self.assertRaisesRegex(ValueError, 'every enabled TI'):
            build_engine(document)

    def test_enhanced_subscription_ready_requires_real_confirmation_window(self):
        document = self.enhanced_config()
        document['subscriptions']['required_windows']['r_30'] = 10
        with self.assertRaisesRegex(ValueError, '30 second'):
            build_engine(document)


if __name__ == '__main__':
    unittest.main()
