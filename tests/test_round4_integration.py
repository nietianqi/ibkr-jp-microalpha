"""Integration gates added after the joint October review."""
import unittest
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from decimal import Decimal as D
from dataclasses import replace

from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.provenance import parameter_content_hash
from tests.test_engine import T

CONFIG = Path(__file__).resolve().parents[1] / 'examples/research.json'


def research_document():
    document = deepcopy(load_config(CONFIG))
    document['profile'] = 'research'
    citation = {'source': 'UNIT_FIXTURE', 'trained_until': (T-timedelta(days=2)).isoformat(),
                'available_at': (T-timedelta(days=1)).isoformat(), 'code_hash': 'a'*64}
    document['provenance'] = {name: {**citation, 'content_sha256': parameter_content_hash(document, name)}
                              for name in ('scalers', 'thresholds', 'economics')}
    document['provenance']['scalers']['clip_rates'] = {name:.01 for name in document['scalers']}
    return document


class ParameterAvailabilityTests(unittest.TestCase):
    def test_future_scalers_and_thresholds_never_reach_historical_decisions(self):
        for group in ('scalers', 'thresholds', 'economics'):
            with self.subTest(group=group):
                document = research_document()
                document['provenance'][group].update(
                    trained_until='2099-12-30T00:00:00+09:00', available_at='2099-12-31T00:00:00+09:00')
                engine = build_engine(document)
                with self.assertRaisesRegex(ValueError, 'not yet available'):
                    engine.poll(T)
                self.assertFalse(engine.book.orders)

    def test_changed_threshold_cannot_reuse_another_artifact_citation(self):
        document = research_document()
        document['alpha']['entry_score'] += 1
        with self.assertRaisesRegex(ValueError, 'content_sha256'):
            build_engine(document)

    def test_missing_availability_equal_cutoff_and_invalid_code_hash_are_rejected(self):
        for change in ('missing', 'equal', 'hash'):
            with self.subTest(change=change):
                document = research_document()
                entry = document['provenance']['thresholds']
                if change == 'missing':
                    del entry['available_at']
                elif change == 'equal':
                    entry['available_at'] = entry['trained_until']
                else:
                    entry['code_hash'] = 'abc'
                with self.assertRaises(ValueError):
                    build_engine(document)

    def test_availability_boundary_and_expiry_are_causal(self):
        document = research_document()
        expiry = T+timedelta(seconds=1)
        for entry in document['provenance'].values():
            entry['available_at'] = T.isoformat()
            entry['valid_until'] = expiry.isoformat()
        engine = build_engine(document)
        engine.poll(T)
        with self.assertRaisesRegex(ValueError, 'expired'):
            engine.poll(expiry)


class MarketFactIntegrationTests(unittest.TestCase):
    def engine(self):
        from tests.test_engine import fixture_document
        from tests.test_review_fixes import fixture
        document=fixture_document()
        document['market_controls']=dict(require_price_band=True,require_status=True,
            require_instrument_rule=True,upper_stop_multiple='2')
        test=fixture(document)
        test.forecast(0)
        return test

    def test_missing_market_facts_prevent_queued_entry(self):
        test=self.engine()
        for second in (0,5,10,11,12):test.tick(second)
        self.assertFalse(test.engine.book.orders)

    def test_replay_market_status_immediately_revalidates_queued_buy(self):
        from tests.test_engine import SYMBOL
        from tests.test_review_fixes import at
        from ibkr_microalpha.replay import Replay
        test=self.engine()
        replay=Replay(test.engine)
        facts=[('instrument_rule',dict(symbol=SYMBOL,effective_from=(T-timedelta(days=1)).isoformat(),
            effective_until=(T+timedelta(days=1)).isoformat(),known_at=T.isoformat(),tick_category='TOPIX500',
            venue='TSEJ',lot_size=100,source='UNIT_ONLY',version='r1')),
            ('price_band',dict(symbol=SYMBOL,day=T.astimezone(__import__('ibkr_microalpha.market',fromlist=['JST']).JST).date().isoformat(),
            base='3000',lower='2500',upper='3500',known_at=T.isoformat(),source='UNIT_ONLY',version='p1')),
            ('symbol_status',dict(symbol=SYMBOL,status='CONTINUOUS',known_at=T.isoformat(),
            valid_until=(T+timedelta(seconds=30)).isoformat(),source='UNIT_ONLY',version='s1'))]
        for index,(kind,data) in enumerate(facts):
            replay.dispatch(dict(event_id=f'fact:{index}',received_at=T.isoformat(),sequence=index,type=kind,data=data))
        for second in (0,5,10,11,12):test.tick(second)
        buy=next(iter(test.engine.book.orders.values()))
        self.assertIsNone(buy.submitted_at)
        replay=Replay(test.engine)  # the adapter callback starts at the current engine time
        replay.dispatch(dict(event_id='halt',received_at=at(12).isoformat(),sequence=4,type='symbol_status',
            data=dict(symbol=SYMBOL,status='HALT',known_at=at(12).isoformat(),valid_until=at(30).isoformat(),
                      source='UNIT_ONLY',version='halt1')))
        commands=test.engine.book.drain_commands(at(12),entry_validator=test.engine.entry_still_valid)
        self.assertFalse(any(c.kind=='SUBMIT' and c.order_id==buy.order_id for c in commands))


class BetaAndRecoveryTests(unittest.TestCase):
    def test_holding_scaled_stop_is_explicit_and_preserves_legacy_default(self):
        from tests.test_engine import fixture_document,SYMBOL
        from tests.test_review_fixes import fixture,at
        from ibkr_microalpha.domain import Quote
        document=fixture_document()
        document['engine'].update(stop_volatility_multiple='10',scale_stop_to_holding=True)
        test=fixture(document)
        quote=Quote(SYMBOL,T,D('3000'),D('3001'),100,100,'stop',bid_at=T,ask_at=T)
        snapshot=test.prepared_snapshot(0)
        scaled=test.engine.entries.stop_distance(snapshot,quote,T,test.engine.instruments[SYMBOL])
        legacy_document=fixture_document()
        legacy_document['engine']['stop_volatility_multiple']='10'
        legacy_engine=fixture(legacy_document).engine
        legacy=legacy_engine.entries.stop_distance(snapshot,quote,T,legacy_engine.instruments[SYMBOL])
        self.assertAlmostEqual(float(scaled/legacy),2**.5)

    def test_beta_is_past_only_shrunk_and_expires_without_old_fallback(self):
        from ibkr_microalpha.beta import fit_beta
        from ibkr_microalpha.features import FeatureEngine,FeatureConfig
        days=[(T-timedelta(days=i+2)).date() for i in range(20)]
        market={day:float(i) for i,day in enumerate(days)}
        stock={day:2*value for day,value in market.items()}
        estimate=fit_beta('S',stock,market,trained_until=T-timedelta(days=1),known_at=T,
                          valid_until=T+timedelta(seconds=2),source='UNIT_ONLY',version='b1')
        self.assertAlmostEqual(estimate.beta,1.5)
        engine=FeatureEngine('B',FeatureConfig(require_symbol_beta=True))
        engine.add_beta(estimate)
        self.assertIsNone(engine.beta_at('S',T-timedelta(seconds=1)))
        self.assertEqual(engine.beta_at('S',T),1.5)
        engine.add_beta(replace(estimate,beta=2.0,known_at=T+timedelta(seconds=1)))
        self.assertIsNone(engine.beta_at('S',T+timedelta(seconds=2)))

    def test_full_input_recovery_preserves_daily_risk_and_intent_counts(self):
        from tempfile import TemporaryDirectory
        from ibkr_microalpha.demo import create_demo
        from ibkr_microalpha.recovery import restore_from_events
        with TemporaryDirectory() as directory:
            _,runner=create_demo(CONFIG,Path(directory)/'events.jsonl')
        when=runner.last_key[0]+timedelta(seconds=1)
        runner.dispatch(dict(event_id='daily-loss-lock',received_at=when.isoformat(),sequence=99999,
                             type='kill_switch',data={'reason':'daily_loss'}))
        restored=restore_from_events(runner.engine.frozen_config,runner.raw_lines,expected_manifest=runner.manifest())
        self.assertIn('daily_loss',restored.risk.lock_reasons)
        self.assertEqual(restored.risk.entry_intents_today,runner.engine.risk.entry_intents_today)
        self.assertFalse(restored.book.connected)
        self.assertFalse(restored.book.commands)
        self.assertIn('recovery_pending_reconciliation',restored.risk.soft_blocks)
        self.assertEqual(restored.quotes,{})


if __name__ == '__main__':
    unittest.main()
