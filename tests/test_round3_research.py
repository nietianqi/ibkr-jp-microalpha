"""Regression contracts for labels, independent days and volatility endpoints."""
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from math import log
from pathlib import Path

from ibkr_microalpha.domain import Quote
from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.economics import (CalibrationRow, CalibrationTable, CommissionSchedule,
                                      policy_fingerprint, prediction_gate)
from ibkr_microalpha.features import FeatureConfig, FeatureEngine
from ibkr_microalpha.market import JST, TickTable
from ibkr_microalpha.replay import Replay
from ibkr_microalpha.research.calibration import (build_calibration_rows, calibration_table_event,
                                                day_block_lower_bound)
from ibkr_microalpha.research.labels import (LabelPolicy, ReplayIntentLabel, label_intent,
                                           replay_intent_labels)

NOW = datetime(2026, 10, 2, 10, tzinfo=JST)
FEES = CommissionSchedule(D('0'), D('80'), D('0'), 'verified-test-fees')
POLICY = LabelPolicy(2, 1, 2, 1, D('1'), 1)
CONFIG = Path(__file__).resolve().parents[1] / 'examples' / 'research.json'


def quote(second, bid='100', ask='100.1', *, bid_size=100, ask_size=100, **kwargs):
    at = NOW + timedelta(seconds=second)
    return Quote('S', at, D(bid), D(ask), bid_size, ask_size, f'q:{second}',
                 bid_at=at, ask_at=at, **kwargs)


def provenance(days=20):
    return dict(policy_hash='a' * 64, code_hashes=['b' * 64], input_hashes=['c' * 64],
                labels_hash='d' * 64, fee_version=FEES.version,
                max_chase_ticks=1,
                trained_until=(NOW - timedelta(days=1)).isoformat(),
                independent_days=[(NOW.date() - timedelta(days=i+2)).isoformat() for i in range(days)],
                complete_policy=True, includes_partial=True, fees_final=True)


_RECORDED_DEMO = None


def recorded_intent(*, start=NOW, filled=100, final_fees=True, profile='research'):
    """Full raw quote/volume Replay fixture; synthetic prices never establish alpha."""
    global _RECORDED_DEMO
    from tempfile import TemporaryDirectory
    from ibkr_microalpha.demo import create_demo
    import json
    if _RECORDED_DEMO is None:
        with TemporaryDirectory() as directory:
            _, original = create_demo(CONFIG, Path(directory)/'events.jsonl')
            _RECORDED_DEMO = [json.loads(line) for line in original.raw_lines]
    document = deepcopy(load_config(CONFIG))
    # Unit fixtures explicitly freeze the two-day floor. The shipped example
    # retains its stronger 20-day gate; do not mutate EngineConfig after hashing.
    document['engine']['min_independent_days'] = 2
    document['profile'] = profile
    if profile != 'demo':
        citation = dict(source='unit-recording', trained_until=(NOW-timedelta(days=50)).isoformat(),
                        code_hash='a'*64)
        document['provenance'] = dict(scalers={**citation, 'clip_rates':{k:0.01 for k in document['scalers']}},
                                      thresholds=citation, economics=citation)
    runner = Replay(build_engine(document))
    shift = start.date() - NOW.date()
    events = deepcopy(_RECORDED_DEMO)
    for event in events:
        event['received_at'] = (datetime.fromisoformat(event['received_at']) + shift).isoformat()
        event['event_id'] = event['event_id'].replace('artificial:', 'unit-recording:')
        data = event['data']
        for key in ('known_at', 'bid_at', 'ask_at'):
            if key in data:
                data[key] = (datetime.fromisoformat(data[key]) + shift).isoformat()
        if event['type'] == 'calendar':
            data['day'] = start.date().isoformat()
        if data.get('source') == 'ARTIFICIAL_FIXTURE' and profile != 'demo':
            data['source'] = 'unit-recording'
        if event['type'] == 'calibration_table' and profile != 'demo':
            row = data['rows'][0]
            row['label_source'] = 'VERIFIED_REPLAY'
            evidence = provenance(40)
            evidence.update(policy_hash=policy_fingerprint(document),
                            fee_version=runner.engine.commissions.version, max_chase_ticks=row['max_chase_ticks'])
            row['provenance'] = evidence
        if event['type'] == 'fill':
            if not filled:
                # The original successful SUBMIT and Submitted callback have
                # already run. A later cancellation gives a terminal zero branch.
                when = datetime.fromisoformat(event['received_at']) + timedelta(seconds=3)
                runner.dispatch(dict(event_id='unit-unfilled-cancel', received_at=when.isoformat(),
                    sequence=event['sequence']+1, type='status',
                    data=dict(order_id=data['order_id'], ib_status='Cancelled', filled=0, remaining=0)))
                break
            data['qty'] = filled
        if event['type'] == 'status' and filled:
            if data['ib_status'] == 'Filled':
                data['filled'] = filled
                if filled < 100 and data['order_id'] == 1:
                    data['ib_status'] = 'Cancelled'
            elif data['order_id'] == 2:
                data['remaining'] = filled
        if event['type'] == 'commission':
            if not final_fees:
                continue
            data['amount'] = '20'
        if event['type'] == 'account_snapshot':
            # A fixture's account sequence must follow this edited replay ledger.
            data['ledger_sequence'] = runner.engine.book.journal[-1]['sequence']
        runner.dispatch(event)
    final_at = runner.last_key[0] + timedelta(seconds=1)
    runner.dispatch(dict(event_id='unit-final-reconciliation', received_at=final_at.isoformat(),
                         sequence=100000, type='reconcile', data=dict(positions={}, open_orders=[],
                             executions=[], complete=True, ownership_confirmed=True)))
    runner.finish()
    return runner

class Round3ResearchTests(unittest.TestCase):
    def test_partial_quote_entry_cannot_be_silent_zero(self):
        quotes = [quote(0), quote(1, ask_size=40), quote(3, '90', '90.1')]
        self.assertIsNone(label_intent(quotes, 0, 100, POLICY, TickTable(), FEES,
                                       NOW + timedelta(seconds=20)))

    def test_delayed_and_partial_exit_quotes_are_unestimable(self):
        for quotes in ([quote(0, market_data_type=3), quote(1), quote(3, '105', '105.1')],
                       [quote(0), quote(1), quote(3, '90', '90.1', bid_size=40),
                        quote(4, '80', '80.1', bid_size=60)]):
            self.assertIsNone(label_intent(quotes, 0, 100, POLICY, TickTable(), FEES,
                                           NOW + timedelta(seconds=20)))

    def test_unfilled_requires_complete_entry_ttl_coverage(self):
        self.assertIsNone(label_intent([quote(0)], 0, 100, POLICY, TickTable(), FEES,
                                       NOW + timedelta(seconds=20)))
        self.assertEqual(label_intent([quote(0), quote(3, '200', '200.1')], 0, 100,
                                      POLICY, TickTable(), FEES, NOW + timedelta(seconds=20)), D(0))

    def test_untyped_quote_baseline_cannot_be_deployed_as_calibration(self):
        with self.assertRaisesRegex(ValueError, 'ReplayIntentLabel'):
            build_calibration_rows([(NOW.date(), 1.0, D('100'))], [0], policy_id='p',
                                   version='v', holding_seconds=2, quantity=100,
                                   max_chase_ticks=1, min_days=2, min_samples=1)

    def test_one_day_bootstrap_cannot_claim_independence(self):
        with self.assertRaises(ValueError):
            day_block_lower_bound({NOW.date(): [D(100)] * 100}, draws=20)

    def test_zero_days_with_samples_is_invalid(self):
        with self.assertRaises(ValueError):
            CalibrationRow('p', 'v', 2, 100, 0, None, 0, 100, D(100), D(100), 1)

    def test_formal_provenance_and_frozen_day_gate(self):
        row = CalibrationRow('p', 'v', 2, 100, 0, None, 20, 100, D(100), D(50), 1,
                             label_source='VERIFIED_REPLAY', provenance=provenance())
        args = dict(policy_id='p', version='v', quantity=100, min_samples=30,
                    safety_margin=D(0), min_independent_days=20, policy_hash='a'*64,
                    fee_version=FEES.version, as_of=NOW)
        self.assertTrue(prediction_gate(row.prediction(), **args).allowed)
        for changes in (dict(min_independent_days=21), dict(policy_hash='f'*64),
                        dict(fee_version='other'), dict(as_of=NOW-timedelta(days=2))):
            self.assertFalse(prediction_gate(row.prediction(), **{**args, **changes}).allowed)

    def test_artificial_labels_are_demo_only(self):
        row = CalibrationRow('p', 'v', 2, 100, 0, None, 20, 100, D(100), D(50), 1,
                             label_source='ARTIFICIAL', provenance={'source':'ARTIFICIAL_FIXTURE'})
        args = dict(policy_id='p', version='v', quantity=100, min_samples=30, safety_margin=D(0))
        self.assertFalse(prediction_gate(row.prediction(), **args).allowed)
        self.assertTrue(prediction_gate(row.prediction(), **args, allow_artificial=True).allowed)

    def test_nondividing_and_larger_grid_include_endpoint(self):
        for grid in (1, 7, 120):
            engine = FeatureEngine('BENCH', FeatureConfig(required_features=('volatility_bps',),
                                                         volatility_grid_seconds=grid))
            for second in range(61):
                at = NOW + timedelta(seconds=second)
                price = D(100 if second < 59 else 200)
                engine.on_quote(Quote('S', at, price-D('.1'), price+D('.1'), 100, 100, f'{grid}:{second}',
                                      bid_at=at, ask_at=at))
            value = engine._realized_volatility(engine._series['S'], NOW+timedelta(seconds=60), 60)
            self.assertAlmostEqual(value, 10000*log(2))

    def test_verified_factory_full_partial_unfilled_and_final_fees(self):
        for filled in (0, 40, 100):
            runner = recorded_intent(filled=filled)
            labels = replay_intent_labels(runner, verified_manifest=runner.manifest())
            self.assertEqual(len(labels), 1)
            label = labels[0]
            self.assertEqual((label.bought, label.sold, label.quantity), (filled, filled, 100))
            self.assertEqual(label.net_amount, -D('10.3')*filled-(D(40) if filled else D(0)))
            self.assertEqual(label.label_source, 'VERIFIED_REPLAY')
        missing = recorded_intent(final_fees=False)
        with self.assertRaisesRegex(ValueError, 'final commission'):
            replay_intent_labels(missing, verified_manifest=missing.manifest())

    def test_factory_verifies_raw_input_and_ignores_mutated_ledger(self):
        runner = recorded_intent(filled=40)
        manifest = runner.manifest()
        first = replay_intent_labels(runner, verified_manifest=manifest)
        runner.engine.book.commissions['b'] = D(999999)
        self.assertEqual(replay_intent_labels(runner, verified_manifest=manifest), first)
        with self.assertRaisesRegex(ValueError, 'manifest'):
            replay_intent_labels(runner, verified_manifest={**manifest, 'input_sha256':'f'*64})
        with self.assertRaisesRegex(ValueError, 'manifest'):
            replay_intent_labels(runner, verified_manifest=manifest, source_events=runner.raw_lines[:-1])
        self.assertEqual(replay_intent_labels(runner, verified_manifest=manifest,
                                              source_events=runner.raw_lines), first)

    def test_factory_rows_thin_buckets_and_artifact_roundtrip(self):
        labels = []
        for day_offset in (0, 1):
            runner = recorded_intent(start=NOW+timedelta(days=day_offset))
            labels.extend(replay_intent_labels(runner, verified_manifest=runner.manifest()))
        c = runner.engine.config
        args = dict(policy_id=c.policy_id, version=c.model_version, holding_seconds=c.holding_seconds,
                    quantity=100, max_chase_ticks=10, min_days=2, min_samples=2, draws=20)
        rows = build_calibration_rows(labels, [0, 100], **args)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].sample_count, rows[0].sample_days), (2, 2))
        self.assertTrue(rows[0].prediction().reliable)
        self.assertEqual(build_calibration_rows(labels, [0], **{**args, 'min_days':20}), [])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            build_calibration_rows(labels+[labels[0]], [0], **args)
        with self.assertRaisesRegex(ValueError, 'chase cap'):
            build_calibration_rows(labels, [0], **{**args, 'max_chase_ticks':11})
        changed_cap = replace(rows[0], max_chase_ticks=11)
        self.assertFalse(changed_cap.prediction().reliable)
        with self.assertRaisesRegex(ValueError, 'chase cap'):
            CalibrationTable([changed_cap], known_at=NOW+timedelta(days=3), version='invalid').validate_for_profile(
                'research', policy_hash=labels[0].policy_hash, fee_version=labels[0].fee_version)
        known = NOW+timedelta(days=3)
        target = Replay(build_engine(runner.engine.frozen_config))
        target.dispatch(dict(event_id='row', received_at=known.isoformat(), sequence=1,
                             type='calibration_table', data=calibration_table_event(rows, version='frozen', known_at=known)))
        self.assertEqual(target.engine.calibration.rows, tuple(rows))

    def test_demo_factory_stays_artificial(self):
        runner = recorded_intent(profile='demo')
        self.assertEqual(replay_intent_labels(runner, verified_manifest=runner.manifest())[0].label_source,
                         'ARTIFICIAL')


if __name__ == '__main__':
    unittest.main()
