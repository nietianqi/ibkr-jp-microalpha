"""Regression tests for the observed live-session failures; no broker connection."""
import io
import importlib.util
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.domain import Quote
from ibkr_microalpha.features import FeatureConfig, FeatureEngine, SameTimeProfile
from ibkr_microalpha.market import validate_quote
from ibkr_microalpha.paper import (IbkrPaperGateway, PaperGuardError, PaperSession,
    cancel_owned_orders, check_live_configuration, execution_time, missing_profiles,
    check_paper_target, paper_command, preamble_events, read_market_status, validate_universe)
from ibkr_microalpha.recording import CallbackRecorder, CaptureConfig
from ibkr_microalpha.replay import Replay
from tests.test_round4_recording import BASE, metadata


class IncrementalCoverageTests(unittest.TestCase):
    def setUp(self):
        self.recorder = CallbackRecorder(CaptureConfig((metadata(),), quote_mode='incremental'),
            raw_sink=io.StringIO(), events_sink=io.StringIO())
        self.features = FeatureEngine('BENCH', FeatureConfig(version='l1-proxy-v1',
            required_features=('r_300', 'rvol_30', 'vwap_proxy_slope_120', 'volatility_bps'),
            trade_source='SAMPLED', vwap_kind='SAMPLED', sampled_subscription_coverage=True,
            baseline_bucket_seconds=300))
        self.features.add_profile(SameTimeProfile('volume', 'TEST', 'SAMPLED', 30,
            9*3600+300, 300, 100, 20, BASE-timedelta(days=1), 'TEST'))
        self.events = []
        self.send('marketDataType', 0, market_data_type=1)
        self.send('marketStatus', 0, status='CONTINUOUS', source='TEST_VENUE',
            known_at=BASE.isoformat(), valid_until=(BASE+timedelta(hours=1)).isoformat())
        self.send('tickPrice', 0, tick_type=1, price=1000)
        self.send('tickPrice', 0, tick_type=2, price=1001)
        self.send('tickPrice', 0, tick_type=4, price=1000)

    def send(self, kind, second, **data):
        at = BASE+timedelta(seconds=second)
        events = self.recorder.process(dict(callback=kind, request_id=1,
                                            received_at=at.isoformat(), data=data))
        for event in events:
            self.events.append(event)
            values = dict(event['data'])
            if event['type'] == 'quote_stream_health':
                self.features.set_quote_stream_health(values['symbol'], at, values['healthy'], values['source'])
            elif event['type'] == 'quote':
                for key in ('bid_at', 'ask_at', 'bid_observed_at', 'ask_observed_at'):
                    if key in values: values[key] = datetime.fromisoformat(values[key])
                for key in ('bid', 'ask'): values[key] = Decimal(values[key])
                self.features.on_quote(Quote(at=at, event_id=event['event_id'], **values))
            elif event['type'] == 'cumulative_volume':
                self.features.on_cumulative_volume('TEST', at, values['total'],
                    Decimal(values['last_price']), event['event_id'], 'SAMPLED')
        return events

    def accumulate(self):
        for second in range(361):
            self.send('tickSize', second, tick_type=0, size=500)
            self.send('tickSize', second+.1, tick_type=3, size=400)
            if second % 10 == 0:
                self.send('tickSize', second+.2, tick_type=8, size=1000+second*10)

    def test_unchanged_prices_build_full_windows_without_rewriting_price_time(self):
        self.accumulate()
        snapshot = self.features.snapshot('TEST', BASE+timedelta(seconds=360.2))
        self.assertTrue(snapshot.valid, snapshot.reason)
        self.assertIn('r_300', snapshot.values)
        self.assertIn('rvol_30', snapshot.values)
        last = next(e for e in reversed(self.events) if e['type']=='quote')['data']
        self.assertEqual(last['bid_at'], BASE.isoformat())
        self.assertGreater(datetime.fromisoformat(last['bid_observed_at']), BASE+timedelta(seconds=300))
        self.assertTrue(validate_quote(self.features._get('TEST').quotes[-1], BASE+timedelta(seconds=360.2)))

    def test_actual_gap_resets_history_and_silence_does_not_create_health(self):
        self.accumulate()
        self.assertFalse(self.features.quote_stream_healthy('TEST', BASE+timedelta(seconds=370)))
        self.send('tickSize', 370, tick_type=0, size=500)
        self.send('tickSize', 370.1, tick_type=3, size=400)
        self.assertNotIn('r_300', self.features.snapshot('TEST', BASE+timedelta(seconds=370.1)).values)

    def test_synthetic_status_and_other_symbol_do_not_refresh_quote_health(self):
        self.send('tickSize', .1, tick_type=0, size=500)
        self.send('tickSize', .2, tick_type=3, size=400)
        self.send('marketStatus', 20, status='CONTINUOUS', source='TEST_VENUE',
            known_at=BASE.isoformat(), valid_until=(BASE+timedelta(hours=1)).isoformat())
        self.assertFalse(self.features.quote_stream_healthy('TEST', BASE+timedelta(seconds=20)))
        self.assertFalse(self.features.quote_stream_healthy('OTHER', BASE+timedelta(seconds=20)))

    def test_halt_and_delayed_data_immediately_invalidate_coverage(self):
        self.send('tickSize', .1, tick_type=0, size=500)
        self.send('tickSize', .2, tick_type=3, size=400)
        self.send('tickGeneric', 1, tick_type=49, value=1)
        self.assertFalse(self.features.quote_stream_healthy('TEST', BASE+timedelta(seconds=1)))
        self.send('marketDataType', 2, market_data_type=3)
        self.assertFalse(self.features._get('TEST').quotes)

    def test_observation_timestamps_must_follow_price_and_precede_receipt(self):
        q = Quote('TEST', BASE, Decimal(1000), Decimal(1001), 100, 100, 'q', bid_at=BASE, ask_at=BASE)
        for observed in (BASE-timedelta(seconds=1), BASE+timedelta(seconds=1)):
            invalid = replace(q, bid_observed_at=observed, ask_observed_at=BASE)
            self.assertEqual(validate_quote(invalid, BASE).reason, 'INVALID_OBSERVATION_TIME')

    def test_volume_counter_correction_does_not_erase_quote_returns(self):
        self.accumulate()
        self.send('tickSize', 361, tick_type=0, size=500)
        self.send('tickSize', 361.1, tick_type=3, size=400)
        self.send('tickSize', 361.2, tick_type=8, size=10)
        snapshot=self.features.snapshot('TEST', BASE+timedelta(seconds=361.2))
        self.assertIn('r_300', snapshot.values)
        self.assertNotIn('rvol_30', snapshot.values)
        self.assertTrue(any(e['type']=='data_reset' and e['data'].get('scope')=='volume' for e in self.events))

    def test_transient_locked_book_is_not_emitted_as_invalid_price_history(self):
        self.send('tickSize', .1, tick_type=0, size=500)
        self.send('tickSize', .2, tick_type=3, size=400)
        before=sum(e['type']=='quote' for e in self.events)
        self.send('tickPrice', 1, tick_type=1, price=1001)
        self.send('tickSize', 1.1, tick_type=0, size=500)
        self.assertEqual(sum(e['type']=='quote' for e in self.events), before)


class SessionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.engine = build_engine(load_config('examples/research.json'))

    def test_demo_cannot_be_promoted_to_live_with_the_account_flag(self):
        engine = build_engine(load_config('examples/jp7-live-small.json'))
        with self.assertRaisesRegex(PaperGuardError, 'demo'):
            check_live_configuration(engine, [], None, BASE)

    def test_live_target_cannot_use_a_paper_account_or_paper_port(self):
        for port,account in ((4002,'U1'),(4001,'DU1')):
            with self.assertRaises(PaperGuardError):check_paper_target(port,[account],account)

    def test_missing_real_funds_is_rejected_before_calibration_or_connection(self):
        engine = build_engine(load_config('examples/jp7-live-small.json'))
        engine.frozen_config['profile'] = 'research'
        engine.risk.config = replace(engine.risk.config, require_account_snapshot=False)
        with self.assertRaisesRegex(PaperGuardError, 'account snapshots'):
            check_live_configuration(engine, [], None, BASE)

    def test_exact_universe_rejects_silent_extra_and_missing_symbols(self):
        capture = CaptureConfig((metadata(symbol='STOCK_SYNTHETIC', benchmark=False, contract_verified=True),
            metadata(request_id=2, symbol='BENCH_SYNTHETIC', contract_id=124, contract_verified=True)))
        self.assertEqual(len(validate_universe(self.engine, capture)), 2)
        extra = replace(capture, instruments=capture.instruments+(metadata(request_id=3, symbol='EXTRA', contract_id=125),))
        with self.assertRaisesRegex(PaperGuardError, 'extra'):
            validate_universe(self.engine, extra)

    def test_jp7_configs_include_every_subscribed_stock(self):
        capture = CaptureConfig.from_dict(json.loads(Path('examples/capture-jp7.json').read_text(encoding='utf-8')))
        for config in ('examples/jp7-live-small.json', 'examples/jp7-q100-max200.json'):
            self.assertIn('6723', validate_universe(build_engine(load_config(config)), capture))

    def test_cleanup_cancels_only_session_owned_active_orders(self):
        gateway = SimpleNamespace(sent_order_ids={1000}, cancel=Mock(), client=SimpleNamespace(reqGlobalCancel=Mock()))
        session = SimpleNamespace(replay=SimpleNamespace(engine=SimpleNamespace(book=SimpleNamespace(
            positions={},active_orders=lambda:[SimpleNamespace(order_id=1000), SimpleNamespace(order_id=2000)]))))
        cancel_owned_orders(session, gateway, log=lambda *_:None)
        gateway.cancel.assert_called_once_with(1000)
        gateway.client.reqGlobalCancel.assert_not_called()

    def test_broker_times_preserve_unknown_timezone_and_parse_explicit_zones(self):
        self.assertIsNone(execution_time('20261030 13:20:00'))
        self.assertEqual(execution_time('20261030 13:20:00 Asia/Tokyo').utcoffset(), timedelta(hours=9))
        self.assertEqual(execution_time('20261030-04:20:00').utcoffset(), timedelta(0))
        self.assertEqual(execution_time('20261030 13:20:00', 'Asia/Tokyo').utcoffset(), timedelta(hours=9))

    def test_external_facts_expire_and_paper_sources_cannot_authorize_live(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'status.json'
            fact = dict(source='TEST_EXTERNAL_VENUE', known_at=BASE.isoformat(),
                        valid_until=(BASE+timedelta(seconds=10)).isoformat())
            document = dict(fact, day=BASE.date().isoformat(), is_open=True,
                            symbols=[dict(fact, symbol='TEST', status='CONTINUOUS')])
            path.write_text(json.dumps(document), encoding='utf-8')
            self.assertTrue(read_market_status(path, BASE, {'TEST'})['is_open'])
            with self.assertRaisesRegex(PaperGuardError, 'expired'):
                read_market_status(path, BASE+timedelta(seconds=10), {'TEST'})
            document['source']='PAPER_SCHEDULE_ASSERTION'
            path.write_text(json.dumps(document), encoding='utf-8')
            with self.assertRaises(PaperGuardError):read_market_status(path, BASE, {'TEST'})

    def test_account_refresh_is_recorded_and_replays_identically(self):
        first=Replay(self.engine)
        for sequence, active in enumerate((True, False), 1):
            first.dispatch(dict(event_id=str(sequence), sequence=sequence, received_at=BASE.isoformat(),
                                type='account_refresh', data=dict(active=active)))
            self.assertEqual('account_refresh_in_progress' in self.engine.risk.soft_blocks, active)
        second=Replay(build_engine(load_config('examples/research.json')))
        for line in first.raw_lines: second.dispatch(json.loads(line))
        self.assertEqual(self.engine.risk.soft_blocks, second.engine.risk.soft_blocks)

    def test_live_session_never_synthesizes_market_status_on_missing_facts(self):
        capture=CaptureConfig((metadata(),),quote_mode='incremental')
        raw=io.StringIO()
        recorder=CallbackRecorder(capture,raw_sink=raw,events_sink=io.StringIO())
        session=PaperSession(Replay(self.engine),recorder,session_id='live',symbols={'TEST'},
                             max_quantity=100,live='U1',status_file='missing-status-facts.json')
        session.assert_status(BASE)
        self.assertEqual(raw.getvalue(),'')
        self.assertIsNotNone(session.market_fact_error)
        self.assertFalse(self.engine.features.quote_stream_healthy('TEST',BASE))

    def test_missing_jpy_funds_cannot_anchor_a_live_session(self):
        recorder=CallbackRecorder(CaptureConfig((metadata(),)),raw_sink=io.StringIO(),events_sink=io.StringIO())
        session=PaperSession(Replay(self.engine),recorder,session_id='live',symbols={'TEST'},max_quantity=100,live='U1')
        with self.assertRaisesRegex(PaperGuardError,'JPY funds'):
            session.reconcile(dict(positions={},open_orders=[],executions=[],next_order_id=1000),BASE)
        self.assertIsNone(self.engine.account_snapshot)


@unittest.skipUnless(importlib.util.find_spec('ibapi'), 'optional official ibapi SDK is not installed')
class AdapterTests(unittest.TestCase):
    def gateway(self, live=None):
        capture=CaptureConfig((metadata(),))
        gateway=IbkrPaperGateway(capture, host='127.0.0.1', port=4001 if live else 4002,
                                 client_id=81, queue=Queue(), live_account=live)
        gateway.accounts=[live or 'DU1']
        return gateway

    def test_execution_query_contains_receipt_time_and_passes_replay_parsing(self):
        gateway=self.gateway()
        gateway.snapshot=dict(open_orders=[],positions={},executions=[],parts=set())
        gateway.client.execDetails(9001, SimpleNamespace(symbol='TEST'), SimpleNamespace(
            execId='e1',orderId=1000,shares=100,price=1000,side='BOT',time='20261030 13:20:00 Asia/Tokyo'))
        row=gateway.snapshot['executions'][0]
        self.assertIsNotNone(datetime.fromisoformat(row['at']).tzinfo)
        self.assertIn('executed_at', row)
        runner=Replay(build_engine(load_config('examples/research.json')))
        apply=runner._parse('reconcile',dict(positions={},open_orders=[],executions=[row],
            complete=True,ownership_confirmed=True), datetime.now(timezone.utc), 'query')
        self.assertTrue(callable(apply))

    def test_live_summary_requires_jpy_and_exact_account(self):
        gateway=self.gateway('U1')
        gateway.snapshot=dict(open_orders=[],positions={},executions=[],parts={'orders','positions'},funds={})
        gateway.client.reqAccountSummary=Mock()
        gateway.client.cancelAccountSummary=Mock()
        gateway.client.cancelPositions=Mock()
        gateway._snapshot_part('executions')
        gateway.client.reqAccountSummary.assert_called_once()
        gateway.client.accountSummary(9002,'U2','AvailableFunds','9999999','JPY')
        gateway.client.accountSummary(9002,'U1','AvailableFunds','9999999','USD')
        self.assertEqual(gateway.snapshot['funds'],{})
        for tag,value in (('AvailableFunds','100000'),('NetLiquidation','200000')):
            gateway.client.accountSummary(9002,'U1',tag,value,'JPY')
        gateway.client.accountSummaryEnd(9002)
        kind, data=gateway.queue.get_nowait()
        self.assertEqual(kind,'reconcile')
        self.assertFalse(data['changed_during_funds'])
        engine=build_engine(load_config('examples/research.json'))
        recorder=CallbackRecorder(gateway.config, raw_sink=io.StringIO(), events_sink=io.StringIO())
        session=PaperSession(Replay(engine),recorder,session_id='funds',symbols={'TEST'},max_quantity=100,live='U1')
        session.reconcile(data,datetime.now(timezone.utc))
        self.assertEqual(engine.valuation.cash_source,'reconciled')
        self.assertEqual(engine.risk.cash,Decimal(100000))

    def test_order_activity_during_funds_query_invalidates_the_barrier(self):
        gateway=self.gateway('U1')
        gateway.snapshot=dict(open_orders=[],positions={},executions=[],parts={'orders','positions'},funds={})
        gateway.client.reqAccountSummary=Mock(); gateway.client.cancelAccountSummary=Mock()
        gateway.client.cancelPositions=Mock()
        gateway._snapshot_part('executions')
        gateway.client.orderStatus(1000,'Filled',100,0)
        gateway.client.accountSummaryEnd(9002)
        gateway.queue.get_nowait()  # order status precedes the completed snapshot
        _, data=gateway.queue.get_nowait()
        self.assertTrue(data['changed_during_funds'])

    def test_position_closed_during_funds_query_invalidates_barrier(self):
        gateway=self.gateway('U1')
        gateway.snapshot=dict(open_orders=[],positions={'TEST':100},executions=[],
            parts={'orders','positions','executions'},funds={},funds_revision=0)
        gateway.client.position('U1',SimpleNamespace(symbol='TEST'),0,0)
        self.assertNotIn('TEST',gateway.snapshot['positions'])
        self.assertEqual(gateway.activity_revision,1)

    def test_demo_live_cli_refuses_before_constructing_a_gateway(self):
        args=SimpleNamespace(config='examples/jp7-live-small.json',capture_config='examples/capture-jp7.json',
            preamble='runs/paper-hist/20261028/events.jsonl',output='runs/test-live-preflight-refused',
            host='127.0.0.1',port=4001,client_id=81,until='23:59',live_account='U1')
        if not Path(args.preamble).is_file():self.skipTest('local historical preamble is not available')
        with patch('ibkr_microalpha.paper.IbkrPaperGateway') as constructor:
            with self.assertRaisesRegex(PaperGuardError,'demo'):paper_command(args,log=lambda *_:None)
            constructor.assert_not_called()

    def test_gateway_refuses_to_cancel_an_unowned_order(self):
        gateway=self.gateway(); gateway.client.cancelOrder=Mock()
        with self.assertRaises(PaperGuardError):gateway.cancel(42)
        gateway.client.cancelOrder.assert_not_called()

    def test_failed_connect_leaves_frozen_config_and_failure_evidence(self):
        from ibkr_microalpha.demo import create_demo
        with TemporaryDirectory() as directory:
            path=Path(directory)
            events,_=create_demo('examples/research.json',path/'events.jsonl')
            capture=CaptureConfig((metadata(symbol='STOCK_SYNTHETIC',benchmark=False,contract_verified=True),
                metadata(request_id=2,symbol='BENCH_SYNTHETIC',contract_id=124,contract_verified=True)))
            (path/'capture.json').write_text(json.dumps(capture.to_dict()),encoding='utf-8')
            args=SimpleNamespace(config='examples/research.json',capture_config=path/'capture.json',
                preamble=events,output=path/'out',host='127.0.0.1',port=4002,client_id=81,
                until='23:59',live_account=None)
            gateway=Mock();gateway.connect.side_effect=PaperGuardError('test connect failure')
            with patch('ibkr_microalpha.paper.IbkrPaperGateway', return_value=gateway):
                with self.assertRaisesRegex(PaperGuardError,'test connect failure'):
                    paper_command(args,log=lambda *_:None)
            self.assertTrue((path/'out/frozen-config.json').is_file())
            self.assertIn('test connect failure',(path/'out/failure.json').read_text(encoding='utf-8'))
            gateway.client.reqGlobalCancel.assert_not_called()


if __name__=='__main__':
    unittest.main()
