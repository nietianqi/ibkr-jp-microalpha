"""Read-only callback capture: field clocks, volume provenance and fail-closed input."""
import io
import json
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from ibkr_microalpha.recording import (
    CallbackRecorder, CaptureConfig, CaptureError, InstrumentMetadata, record_callbacks,
)
from ibkr_microalpha.features import FeatureEngine


BASE = datetime.fromisoformat('2026-10-29T09:00:00+09:00')


def metadata(**overrides):
    values = dict(request_id=1, symbol='TEST', contract_id=123, exchange='TSEJ', currency='JPY',
                  known_at=BASE-timedelta(days=1), quote_size_multiplier=1,
                  tick8_volume_multiplier=1, rtvolume_multiplier=1, benchmark=True)
    values.update(overrides)
    return InstrumentMetadata(**values)


class RecordingTests(unittest.TestCase):
    def make(self, *, mode='tick8', instrument=None):
        self.raw, self.events, self.latency = io.StringIO(), io.StringIO(), io.StringIO()
        self.recorder = CallbackRecorder(
            CaptureConfig((instrument or metadata(),), session_id='test-session', volume_mode=mode),
            raw_sink=self.raw, events_sink=self.events, latency_sink=self.latency)
        self.n = 0
        return self.recorder

    def callback(self, callback, *, request_id=1, at=None, **data):
        self.n += 1
        return dict(callback=callback, received_at=(at or BASE+timedelta(milliseconds=self.n)).isoformat(),
                    request_id=request_id, data=data)

    def send(self, callback, **data):
        return self.recorder.process(self.callback(callback, **data))

    def ready(self, *, venue=True):
        self.send('marketDataType', market_data_type=1)
        self.send('tickGeneric', tick_type=49, value=0)
        if venue:
            self.send('marketStatus', status='CONTINUOUS', source='test-venue',
                      known_at=BASE.isoformat(), valid_until=(BASE+timedelta(hours=1)).isoformat())
        self.send('tickPrice', tick_type=1, price='1000')
        self.send('tickSize', tick_type=0, size='100')
        self.send('tickPrice', tick_type=2, price='1001')
        return self.send('tickSize', tick_type=3, size='200')

    def emitted(self, kind):
        return [json.loads(line) for line in self.events.getvalue().splitlines()
                if json.loads(line)['type'] == kind]

    def test_complete_quote_preserves_independent_receive_times_and_has_no_exchange_clock(self):
        self.make()
        self.ready()
        quote = self.emitted('quote')[-1]
        self.assertEqual(quote['data']['bid_at'], (BASE+timedelta(milliseconds=4)).isoformat())
        self.assertEqual(quote['data']['ask_at'], (BASE+timedelta(milliseconds=6)).isoformat())
        self.assertNotIn('exchange_at', quote['data'])
        self.assertEqual(quote['data']['market_status'], 'CONTINUOUS')
        self.assertFalse(any(e['data']['healthy'] for e in self.emitted('quote_stream_health')))

    def test_halt_zero_is_not_evidence_of_continuous_venue_state(self):
        self.make()
        self.ready(venue=False)
        self.assertEqual(self.emitted('quote')[-1]['data']['market_status'], 'UNKNOWN')

    def test_missing_or_stale_quote_fields_do_not_generate_quote(self):
        self.make()
        self.send('marketDataType', market_data_type=1)
        self.send('tickPrice', tick_type=1, price='1000')
        self.send('tickSize', tick_type=0, size=100)
        self.send('tickPrice', tick_type=2, price='1001', at=BASE+timedelta(seconds=4))
        self.send('tickSize', tick_type=3, size=100, at=BASE+timedelta(seconds=4,milliseconds=1))
        self.assertEqual(self.emitted('quote'), [])

    def test_price_change_requires_following_size_and_does_not_reuse_old_size(self):
        self.make()
        self.ready()
        count=len(self.emitted('quote'))
        self.send('tickPrice', tick_type=1, price='999')
        self.assertEqual(len(self.emitted('quote')), count)
        self.send('tickSize', tick_type=0, size=100)
        self.assertEqual(self.emitted('quote')[-1]['data']['bid'], '999')

    def test_invalid_size_invalidates_and_requires_complete_new_fields(self):
        self.make()
        self.ready()
        count=len(self.emitted('quote'))
        self.send('tickSize', tick_type=0, size='1.5')
        self.assertTrue(self.emitted('data_reset'))
        self.assertFalse(self.emitted('quote_stream_health')[-1]['data']['healthy'])
        self.send('tickSize', tick_type=0, size=100)
        self.assertEqual(len(self.emitted('quote')), count)

    def test_last_size_and_prior_close_are_never_added_as_trade_volume(self):
        self.make()
        self.send('marketDataType', market_data_type=1)
        self.send('tickPrice', tick_type=9, price='990')
        self.send('tickSize', tick_type=5, size=100)
        self.send('tickSize', tick_type=8, size=1000)
        self.assertEqual(self.emitted('cumulative_volume'), [])
        self.send('tickPrice', tick_type=4, price='1000')
        self.send('tickSize', tick_type=8, size=1001)
        self.assertEqual(self.emitted('cumulative_volume')[-1]['data']['total'],1001)
        self.assertEqual(self.emitted('trade'), [])

    def test_rtvolume_generic_233_is_callback_48_and_uses_only_selected_counter(self):
        self.make(mode='rtvolume')
        self.send('marketDataType', market_data_type=1)
        self.send('tickString', tick_type=48, value='1000;5;1793232000000;1000;999.5;true')
        self.send('tickPrice', tick_type=4, price='1000')
        self.send('tickSize', tick_type=5, size=5)
        self.send('tickSize', tick_type=8, size=999)
        self.send('tickString', tick_type=48, value='1000;5;1793232000001;1005;999.5;true')
        self.assertEqual([e['data']['total'] for e in self.emitted('cumulative_volume')],[1000,1005])
        self.assertEqual(self.emitted('trade'), [])

    def test_tick8_mode_does_not_merge_different_rtvolume_counter(self):
        self.make()
        self.send('marketDataType', market_data_type=1)
        self.send('tickPrice', tick_type=4, price='1000')
        self.send('tickSize', tick_type=8, size=1000)
        self.send('tickString', tick_type=48, value='1000;5;1793232000000;1010;999.5;true')
        self.assertEqual([e['data']['total'] for e in self.emitted('cumulative_volume')],[1000])

    def test_volume_reset_is_explicit_and_baselines_are_not_summed(self):
        self.make()
        self.send('marketDataType', market_data_type=1)
        self.send('tickPrice', tick_type=4, price='1000')
        self.send('tickSize', tick_type=8, size=1000)
        self.send('tickSize', tick_type=8, size=1000)
        self.send('tickSize', tick_type=8, size=900)
        self.assertEqual([e['data']['total'] for e in self.emitted('cumulative_volume')],[1000,900])
        self.assertIn('VOLUME_RESET',[e['data']['reason'] for e in self.emitted('data_reset')])

    def test_unknown_units_and_future_metadata_are_raw_only(self):
        self.make(instrument=metadata(quote_size_multiplier=None, tick8_volume_multiplier=None))
        self.ready()
        self.send('tickPrice', tick_type=4, price='1000')
        self.send('tickSize', tick_type=8, size=1000)
        self.assertFalse(self.emitted('quote'))
        self.assertFalse(self.emitted('cumulative_volume'))
        self.make(instrument=metadata(known_at=BASE+timedelta(days=1)))
        self.ready()
        self.assertFalse(self.emitted('quote'))

    def test_volume_units_are_explicitly_scaled(self):
        self.make(instrument=metadata(tick8_volume_multiplier=100))
        self.send('marketDataType', market_data_type=1)
        self.send('tickPrice', tick_type=4, price='1000')
        self.send('tickSize', tick_type=8, size=10)
        self.assertEqual(self.emitted('cumulative_volume')[0]['data']['total'],1000)

    def test_farm_restoration_does_not_prove_per_symbol_health(self):
        self.make()
        self.ready()
        self.send('error', request_id=-1, code=2103, message='farm disconnected:jfarm')
        count=len(self.emitted('quote'))
        self.send('error', request_id=-1, code=2104, message='farm OK:jfarm')
        self.send('tickSize', tick_type=0, size=100)
        self.assertEqual(len(self.emitted('quote')),count)
        self.assertFalse(any(e['data']['healthy'] for e in self.emitted('quote_stream_health')))

    def test_disconnect_and_restores_are_replayable_and_clear_cached_fields(self):
        self.make()
        self.ready()
        for code in (1100,1101,1102):
            self.send('error', request_id=-1, code=code, message='test connectivity')
        self.assertEqual(len(self.emitted('disconnect')),1)
        self.assertEqual(len(self.emitted('reconnect')),2)
        self.assertFalse(any(e['data']['healthy'] for e in self.emitted('quote_stream_health')))
        self.assertTrue(self.recorder.summary()['requires_resubscribe'])

    def test_delayed_data_and_halt_are_not_treated_as_live_quotes(self):
        self.make()
        self.ready()
        self.send('marketDataType', market_data_type=3)
        count=len(self.emitted('quote'))
        self.send('tickPrice', tick_type=66, price='1000')
        self.send('tickGeneric', tick_type=49, value=-1)
        self.assertEqual(len(self.emitted('quote')),count)
        self.assertTrue(self.emitted('data_reset'))

    def test_out_of_order_is_archived_and_poisoned_instead_of_retimestamped(self):
        self.make()
        self.send('marketDataType', market_data_type=1)
        with self.assertRaises(CaptureError):
            self.recorder.process(self.callback('tickPrice',tick_type=1,price='1000',at=BASE))
        self.assertEqual(len(self.raw.getvalue().splitlines()),2)
        self.assertTrue(self.recorder.failed)
        with self.assertRaises(CaptureError):self.recorder.finish()

    def test_output_failure_and_short_write_poison_capture(self):
        class ShortSink:
            def write(self,text):return len(text)-1
        recorder=CallbackRecorder(CaptureConfig((metadata(),),session_id='short'),
                                  raw_sink=ShortSink(),events_sink=io.StringIO())
        with self.assertRaises(OSError):recorder.process(dict(callback='noop',received_at=BASE.isoformat(),data={}))
        self.assertTrue(recorder.failed)
        with self.assertRaises(CaptureError):recorder.finish()

    def test_monotonic_pipeline_measurements_do_not_claim_unobserved_send_ack(self):
        self.make()
        self.recorder.process(self.callback('noop',timing={'enqueue_monotonic_ns':1,'send_ns':2,'ack_ns':3}))
        result=self.recorder.summary()['latency']
        self.assertGreaterEqual(result['processing']['samples'],1)
        for key in ('queue','decision','send','ack'):
            self.assertEqual(result[key]['status'],'UNKNOWN')

    def test_config_json_roundtrip_and_stream_input_are_deterministic(self):
        self.make()
        config=CaptureConfig.from_dict(self.recorder.config.to_dict())
        first=self.callback('marketDataType',market_data_type=1)
        result=record_callbacks(io.StringIO(json.dumps(first)+'\n'),recorder=self.recorder)
        self.assertEqual(result['callbacks'],1)
        self.assertEqual(config.instruments[0].contract_id,123)
        self.assertEqual(result['metadata'][0]['known_at'],metadata().known_at.isoformat())


class FeatureLookupTests(unittest.TestCase):
    def test_existing_symbol_lookup_does_not_construct_discarded_state(self):
        import ibkr_microalpha.features as features
        original=features._Series
        engine=FeatureEngine('BENCH')
        with patch.object(features,'_Series',wraps=original) as constructor:
            first=engine._get('TEST')
            for _ in range(10):self.assertIs(engine._get('TEST'),first)
        self.assertEqual(constructor.call_count,1)


if __name__=='__main__':unittest.main()
