"""Round 2 evidence against current APIs; only writes inside this directory."""
import hashlib
import io
import json
import sys
import time
import tracemalloc
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
sys.path.insert(0, str(ROOT))
from ibkr_microalpha.cli import _replay_file
from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.demo import create_demo
from ibkr_microalpha.domain import FeatureSnapshot, Side
from ibkr_microalpha.replay import Replay, ReplayFailed
from ibkr_microalpha.reporting import layer_funnel
from tests.test_engine import EngineIntegrationTests, SYMBOL, T

CONFIG = ROOT/'examples/research.json'

def event(n=1, kind='timer', data=None, at=None):
    return {'event_id':f'r2-{n}', 'received_at':(at or T).isoformat(),
            'sequence':n, 'type':kind, 'data':data or {}}

def fixture():
    test=EngineIntegrationTests('runTest')
    test.setUp()
    return test

def write(name,data):
    (OUT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

def source_clobber():
    directory=OUT/'source-collision'
    directory.mkdir(exist_ok=True)
    source=directory/'raw-input.jsonl'
    original=json.dumps(event())+'\n'
    source.write_text(original,encoding='utf-8')
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    report,result=_replay_file(CONFIG,source,directory)
    write('source-collision.json',{'source':str(source),'before_bytes':len(original.encode()),
        'before_sha256':before,'after_bytes':source.stat().st_size,
        'events_reported':report['events_processed'],'success_report_created':result.exists(),
        'risk_locks':report['risk_locks'],'failure_created':(directory/'failure.json').exists()})

class BrokenSink:
    def write(self,line):
        raise OSError('AUDIT simulated disk full')

class NullSink:
    def write(self,line):
        return len(line)

def persistence_failure():
    runner=Replay(build_engine(load_config(CONFIG)),raw_sink=BrokenSink(),audit_sink=io.StringIO())
    error=None
    try:runner.dispatch(event())
    except OSError as err:error=str(err)
    state={'error':error,'failed':runner.failed,'events_processed':runner.events_processed,
           'identity_committed':'r2-1' in runner.seen_digests,'raw_lines':len(runner.raw_lines),
           'engine_last_at':runner.engine._last_at.isoformat()}
    # A retry skips the write that failed; runner also still accepts later events.
    runner.dispatch(event())
    state['exact_retry']='silently returned'
    runner.raw_sink=io.StringIO()
    runner.dispatch(event(n=2,at=T+timedelta(seconds=1)))
    state['later_event_accepted']=runner.events_processed==2
    state['saved_stream_contains_first_event']='r2-1' in runner.raw_sink.getvalue()
    write('persistence-failure.json',state)

def old_fixes():
    fixes={}
    runner=Replay(build_engine(load_config(CONFIG)))
    bad=event(kind='calendar',data={'day':'2026-10-02','is_open':True,
        'known_at':(T+timedelta(seconds=1)).isoformat(),'source':'r2'})
    errors=[]
    for _ in range(2):
        try:runner.dispatch(bad)
        except ValueError as err:errors.append(str(err))
    fixes['parse_failure']={'errors':errors,'failed':runner.failed,
        'digests':len(runner.seen_digests),'events':runner.events_processed}
    config=deepcopy(load_config(CONFIG))
    config['features']['rvol_days']=20.5
    try:build_engine(config)
    except ValueError as err:fixes['rvol_days_rejected']=str(err)
    try:FeatureSnapshot('S',T,{},'false')
    except ValueError as err:fixes['snapshot_false_rejected']=str(err)
    test=fixture()
    buy=test.fill_entry()
    test.engine.on_fill(exec_id='r2-corrected',order_id=buy.order_id,qty=100,price=D('3002'),
        at=T+timedelta(seconds=14),correction_of='buy-exec')
    fixes['correction']=test.engine.execution_quality.summary(test.engine.book)
    test.engine.on_fill(exec_id='r2-bust',order_id=buy.order_id,qty=0,price=D('3002'),
        at=T+timedelta(seconds=15),correction_of='r2-corrected')
    fixes['bust']=test.engine.execution_quality.summary(test.engine.book)
    fixes['bust_drift_count']=len(test.engine.execution_quality.drifts())
    write('old-fixes.json',fixes)

def reopened_intent():
    test=fixture()
    engine=test.engine
    test.fill_entry()
    test.tick(14)
    engine._request_exit(SYMBOL,T+timedelta(seconds=14),'SIGNAL_EXIT',False)
    sell=test.sells()[-1]
    engine.book.drain_commands(T+timedelta(seconds=14))
    engine.on_fill(exec_id='r2-exit',order_id=sell.order_id,qty=100,price=D('3000'),
                   at=T+timedelta(seconds=15))
    original=layer_funnel(engine)[1]
    engine.on_fill(exec_id='r2-exit-bust',order_id=sell.order_id,qty=0,price=D('3000'),
                   at=T+timedelta(seconds=16),correction_of='r2-exit')
    reconciled=engine.book.reconcile({SYMBOL:100},[],[],T+timedelta(seconds=16),
                                     complete=True,ownership_confirmed=True)
    test.tick(17)
    engine.kill_switch(T+timedelta(seconds=17),'AUDIT_EXIT')
    new_sell=test.sells()[-1]
    engine.book.drain_commands(T+timedelta(seconds=17))
    engine.on_fill(exec_id='r2-exit2',order_id=new_sell.order_id,qty=100,price=D('3000'),
                   at=T+timedelta(seconds=18))
    final=layer_funnel(engine)[1]
    write('reopened-intent.json',{'initial':original,'reconciled_after_bust':reconciled,
        'first_sell_id':sell.order_id,'new_sell_id':new_sell.order_id,
        'position_final':engine.book.positions[SYMBOL].quantity,'intents_final':final,
        'daily_net_pnl_estimate':str(engine.daily_net_pnl_estimate),'risk_locks':sorted(engine.risk.lock_reasons)})

def memory_retention():
    runner=Replay(build_engine(load_config(CONFIG)),raw_sink=NullSink(),audit_sink=NullSink())
    tracemalloc.start()
    samples=[]
    for n in range(1,50001):
        runner.dispatch(event(n=n))
        if n in (10000,25000,50000):
            current,peak=tracemalloc.get_traced_memory()
            samples.append({'events':n,'traced_bytes':current,'peak_bytes':peak,
                'digests':len(runner.seen_digests),'duration_samples':sum(len(v) for v in runner._durations.values()),
                'audit_records':len(runner.engine.audit),'raw_lines':len(runner.raw_lines)})
    tracemalloc.stop()
    write('memory-retention.json',{'method':'tracemalloc after engine construction; all events same-time unique timer; raw/audit NullSink; no original logs changed',
        'samples':samples})

def demo_verification():
    source,generating=create_demo(CONFIG,OUT/'demo-events.jsonl')
    expected=generating.report()
    started=time.perf_counter()
    observed,_=_replay_file(CONFIG,source,OUT/'replayed-demo')
    seconds=time.perf_counter()-started
    compare=lambda r:{k:v for k,v in r.items() if k not in ('metrics','manifest')}
    write('demo-verification.json',{'events':observed['events_processed'],
        'business_report_equal':compare(expected)==compare(observed),
        'input_manifest_equal':expected['manifest']['input_sha256']==observed['manifest']['input_sha256'],
        'observed_wall_seconds_including_build_run_save':seconds,
        'positions':observed['positions'],'intents':observed['intents'],
        'metrics':observed['metrics'],'manifest':observed['manifest']})

if __name__=='__main__':
    task=sys.argv[1] if len(sys.argv)>1 else 'all'
    checks=[source_clobber,persistence_failure,old_fixes,reopened_intent,memory_retention,demo_verification]
    for check in checks:
        if task not in ('all',check.__name__):continue
        try:
            check()
            print(check.__name__,'COMPLETE')
        except Exception as err:
            write(check.__name__+'-unexpected-error.json',{'error':str(err),'type':type(err).__name__})
            print(check.__name__,'FAILED',str(err))
