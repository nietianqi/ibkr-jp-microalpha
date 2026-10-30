"""Paper routing: account guards, order translation and replayable session input."""
import io
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory

from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.demo import create_demo
from ibkr_microalpha.market import JST
from ibkr_microalpha.paper import (
    ORDER_ID_FLOOR, PaperGuardError, PaperSession, check_live_limits, check_paper_target, order_spec,
    preamble_events, run_paper,
)
from ibkr_microalpha.recording import CallbackRecorder, CaptureConfig
from ibkr_microalpha.replay import Replay
from tests.test_round4_recording import metadata

CONFIG = 'examples/research.json'


class GuardTests(unittest.TestCase):
    def test_live_port_refused(self):
        for port in (4001, 7496):
            with self.assertRaises(PaperGuardError):
                check_paper_target(port, ['DU123'])

    def test_non_paper_account_refused(self):
        for accounts in ([], ['U123'], ['DU1', 'U2']):
            with self.assertRaises(PaperGuardError):
                check_paper_target(4002, accounts)
        check_paper_target(4002, ['DU1', 'DU2'])

    def test_live_requires_named_managed_account_and_small_limits(self):
        check_paper_target(4001, ['U1'], live_account='U1')
        with self.assertRaises(PaperGuardError):
            check_paper_target(4001, ['U1'], live_account='U2')
        check_live_limits(build_engine(load_config('examples/jp7-live-small.json')).risk.config)
        with self.assertRaises(PaperGuardError):
            check_live_limits(build_engine(load_config('examples/jp7-q100-max200.json')).risk.config)

    def test_order_spec_rechecks_symbol_and_quantity(self):
        payload = dict(order_id=1001, symbol='A', side='BUY', quantity=100, limit_price='1000.5')
        self.assertEqual(order_spec(payload, symbols={'A'}, max_quantity=200),
                         dict(order_id=1001, symbol='A', action='BUY', quantity=100, limit_price=1000.5))
        for change in (dict(symbol='B'), dict(quantity=300), dict(quantity=0), dict(side='SHORT'),
                       dict(limit_price='0')):
            with self.assertRaises(PaperGuardError):
                order_spec(dict(payload, **change), symbols={'A'}, max_quantity=200)


class FakeClient:
    def __init__(self):
        self.global_cancels = 0

    def reqGlobalCancel(self):
        self.global_cancels += 1


class FakeGateway:
    def __init__(self, queue, *, positions=None):
        self.queue, self.positions = queue, positions or {}
        self.contracts = {'STOCK_SYNTHETIC': None, 'BENCH_SYNTHETIC': None}
        self.placed, self.cancelled, self.snapshots = [], [], 0
        self.client = FakeClient()

    def start_snapshot(self):
        self.snapshots += 1
        self.queue.put(('reconcile', dict(positions=dict(self.positions), open_orders=[],
                                          executions=[], next_order_id=7)))

    def subscribe(self):
        for callback, data in (('marketDataType', dict(market_data_type=1)),
                               ('tickPrice', dict(tick_type=1, price=1000.0)),
                               ('tickPrice', dict(tick_type=2, price=1001.0)),
                               ('tickSize', dict(tick_type=0, size='500')),
                               ('tickSize', dict(tick_type=3, size='600'))):
            self.queue.put(('market', dict(callback=callback, request_id=1, data=data)))

    def place(self, spec):
        self.placed.append(spec)

    def cancel(self, order_id):
        self.cancelled.append(order_id)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        events, _ = create_demo(CONFIG, Path(self.directory.name)/'demo.jsonl')
        self.preamble = preamble_events(events)

    def tearDown(self):
        self.directory.cleanup()

    def session(self):
        engine = build_engine(load_config(CONFIG))
        self.raw = io.StringIO()
        replay = Replay(engine, raw_sink=self.raw)
        capture = CaptureConfig((metadata(symbol='STOCK_SYNTHETIC', benchmark=False),
                                 metadata(request_id=2, symbol='BENCH_SYNTHETIC', contract_id=124)),
                                session_id='paper-test')
        recorder = CallbackRecorder(capture, events_sink=io.StringIO(), raw_sink=io.StringIO())
        return PaperSession(replay, recorder, session_id='paper-test',
                            symbols={'STOCK_SYNTHETIC', 'BENCH_SYNTHETIC'}, max_quantity=100)

    def test_engine_entry_and_exit_translate_to_limit_orders(self):
        session = self.session()
        specs = []
        with open(Path(self.directory.name)/'demo.jsonl', encoding='utf-8') as source:
            for line in source:
                event = json.loads(line)
                session.replay.dispatch(event)
                if event['type'] == 'requests':
                    specs += [order_spec(c.payload, symbols=session.symbols, max_quantity=100)
                              for c in session.replay.last_commands if c.kind == 'SUBMIT']
        self.assertEqual([(s['action'], s['quantity']) for s in specs], [('BUY', 100), ('SELL', 100)])
        self.assertTrue(all(isinstance(s['limit_price'], float) and s['limit_price'] > 0 for s in specs))

    def test_existing_strategy_position_refuses_start(self):
        queue = Queue()
        with self.assertRaises(PaperGuardError):
            run_paper(self.session(), FakeGateway(queue, positions={'STOCK_SYNTHETIC': 100}), queue,
                      until=datetime.now(JST), preamble=self.preamble, log=lambda *_: None)

    def test_session_input_replays_to_identical_report(self):
        queue, session = Queue(), self.session()
        gateway = FakeGateway(queue)
        flat = run_paper(session, gateway, queue, until=datetime.now(JST) - timedelta(minutes=1),
                         preamble=self.preamble, log=lambda *_: None)
        self.assertTrue(flat)
        self.assertEqual(gateway.placed, [])
        engine = session.replay.engine
        self.assertGreaterEqual(engine.book.next_order_id, ORDER_ID_FLOOR)
        self.assertIn('PAPER_SESSION_END', engine.risk.lock_reasons)
        session.replay.finish()
        live = session.replay.report()
        again = Replay(build_engine(load_config(CONFIG)))
        for line in self.raw.getvalue().splitlines():
            again.dispatch(json.loads(line))
        again.finish()
        strip = lambda r: {k: v for k, v in r.items() if k not in ('metrics', 'manifest')}
        self.assertEqual(json.loads(json.dumps(strip(again.report()), default=str)),
                         json.loads(json.dumps(strip(live), default=str)))


if __name__ == '__main__':
    unittest.main()
