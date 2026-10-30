"""Rebuild today's saved callbacks locally; never connect or simulate broker fills."""
from collections import Counter, defaultdict
from datetime import datetime
from itertools import chain
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.paper import PaperSession, missing_profiles, preamble_events, validate_universe
from ibkr_microalpha.recording import CallbackRecorder, CaptureConfig
from ibkr_microalpha.replay import Replay, code_hash


class Discard:
    def write(self,text):return len(text)
    def flush(self):pass


def main():
    document=load_config('examples/jp7-q100-max200.json')
    engine=build_engine(document)
    capture=CaptureConfig.from_dict(json.loads((ROOT/'examples/capture-jp7.json').read_text(encoding='utf-8')))
    runner=Replay(engine,raw_sink=Discard(),audit_sink=Discard())
    session=PaperSession(runner,CallbackRecorder(capture,raw_sink=Discard(),events_sink=Discard()),
        session_id='offline-callback-verification',symbols=validate_universe(engine,capture),max_quantity=200)
    source=ROOT/'runs/live/20261030/callbacks.jsonl'
    preamble=preamble_events(ROOT/'runs/paper-hist/20261028/events.jsonl')
    valid,returns,spans,market=Counter(),Counter(),defaultdict(float),Counter()
    evaluate,market_snapshot=engine.evaluate,engine.features.market_snapshot

    def track_evaluate(snapshot,at):
        if snapshot.valid:valid[snapshot.symbol]+=1
        if 'r_300' in snapshot.values:returns[snapshot.symbol]+=1
        state=engine.features._series.get(snapshot.symbol)
        if state and state.quotes:
            spans[snapshot.symbol]=max(spans[snapshot.symbol],(state.quotes[-1].at-state.quotes[0].at).total_seconds())
        return evaluate(snapshot,at)

    def track_market(at,universe):
        snapshot=market_snapshot(at,universe)
        market[snapshot.reason or 'VALID']+=1
        return snapshot

    engine.evaluate=track_evaluate
    engine.features.market_snapshot=track_market
    with source.open(encoding='utf-8') as stream:
        first=json.loads(next(stream)); at=datetime.fromisoformat(first['received_at'])
        session.dispatch('calendar',dict(day=at.date().isoformat(),is_open=True,
            known_at=at.isoformat(),source='OFFLINE_RECONSTRUCTION_OF_EXISTING_PAPER_ASSERTION'),at)
        session.dispatch('reconcile',dict(positions={},open_orders=[],executions=[],next_order_id=1000,
            complete=True,ownership_confirmed=True),at)
        for kind,data in preamble:session.dispatch(kind,data,at)
        for callback in chain([first],map(json.loads,stream)):
            session.market(callback,datetime.fromisoformat(callback['received_at']))
    runner.finish()
    result=dict(code_sha256=code_hash(),source=str(source.relative_to(ROOT)),callbacks=session.recorder.callbacks,
        events=runner.events_processed,valid_snapshots=valid,ready_300_second_returns=returns,
        market_snapshot_reasons=market,max_quote_spans_seconds=spans,
        missing_preamble_profiles=missing_profiles(engine,preamble),
        latest_feature_reasons={s:v.reason for s,v in engine.snapshots.items()},
        broker_connects=0,broker_requests=0,
        note='Saved callback reconstruction with paper status assertions and demo calibration. '
             'No requests dispatched to any broker; this checks coverage, not profits or live readiness.')
    Path(__file__).with_name('capture-verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
    assert returns.get('6723',0)>0, 'liquid subscribed stock must accumulate its 300-second return'
    assert market.get('VALID',0)>0, 'market data must leave permanent UNKNOWN after warmup'


if __name__=='__main__':main()
