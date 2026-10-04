"""Offline audit probes; never sends broker commands or edits run evidence."""
import argparse
import cProfile
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import platform
import pstats
import statistics
import sys
import time
import tracemalloc
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from statistics import median
from types import MethodType
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).resolve().parent
from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.domain import Quote, Side
from ibkr_microalpha.market import JST
from ibkr_microalpha.replay import Replay
from ibkr_microalpha.reporting import ExecutionQuality


def engine():
    return build_engine(load_config(ROOT / 'runs/demo/frozen-config.json'))


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]


def memory():
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {'working_set_bytes': counters.WorkingSetSize,
            'peak_working_set_bytes': counters.PeakWorkingSetSize,
            'private_commit_bytes': counters.PagefileUsage}


def percentile(values, fraction):
    return sorted(values)[min(len(values)-1, int((len(values)-1)*fraction))]


def benchmark():
    source = ROOT / 'runs/demo/raw-input.jsonl'
    rows = [json.loads(line) for line in source.read_text(encoding='utf-8').splitlines() if line.strip()]
    expected = json.loads((ROOT / 'runs/demo/report.json').read_text(encoding='utf-8'))
    measurements = []
    for repeat in range(2):
        runner = Replay(engine())
        by_kind = defaultdict(list)
        started = time.perf_counter()
        for row in rows:
            tick = time.perf_counter_ns()
            runner.dispatch(row)
            by_kind[row['type']].append((time.perf_counter_ns()-tick)/1e6)
        seconds = time.perf_counter()-started
        measurements.append({'repeat': repeat+1, 'dispatch_wall_seconds': seconds,
                             'events_per_second': len(rows)/seconds,
                             'report_matches_stored': runner.report()==expected,
                             'memory': memory(),
                             'event_timing_ms': {kind: {'count': len(v), 'sum': sum(v),
                                 'p50': statistics.median(v), 'p95': percentile(v, .95),
                                 'p99': percentile(v, .99), 'max': max(v)} for kind,v in by_kind.items()}})
    timeline = [datetime.fromisoformat(r['received_at']) for r in rows]
    trace = [json.loads(line) for line in (ROOT/'runs/demo/audit.jsonl').read_text().splitlines()]
    journal = [json.loads(line) for line in (ROOT/'runs/demo/execution-journal.jsonl').read_text().splitlines()]
    identities = Counter(r['event_id'] for r in rows)
    evidence_hashes = {}
    for name in ['raw-input.jsonl','frozen-config.json','report.json','audit.jsonl','execution-journal.jsonl','execution.json']:
        evidence_hashes[name] = {run: hashlib.sha256((ROOT/f'runs/{run}/{name}').read_bytes()).hexdigest()
                                 for run in ['demo', 'replayed']}
    save('benchmark.json', {'python': sys.version, 'platform': platform.platform(),
         'method': 'In-memory parsed JSONL, sequential dispatch; includes perf-counter instrumentation; no disk save, no network.',
         'event_count':len(rows), 'event_types': dict(Counter(r['type'] for r in rows)),
         'start':min(timeline).isoformat(), 'end':max(timeline).isoformat(),
         'event_timeline_span_seconds': (max(timeline)-min(timeline)).total_seconds(),
         'quote_exchange_at_present': sum(bool(r['data'].get('exchange_at')) for r in rows if r['type']=='quote'),
         'trade_exchange_at_present': sum(bool(r['data'].get('exchange_at')) for r in rows if r['type']=='trade'),
         'duplicate_identities': sum(c-1 for c in identities.values()),
         'audit_count': len(trace), 'audit_kinds': dict(Counter(r['kind'] for r in trace)),
         'journal_count':len(journal), 'journal_kinds':dict(Counter(r['kind'] for r in journal)),
         'evidence_sha256': evidence_hashes, 'repeats':measurements})
    print(json.dumps(measurements, indent=2))


def profile():
    runner = Replay(engine())
    profiler = cProfile.Profile()
    started = time.perf_counter()
    profiler.enable()
    runner.run(ROOT/'runs/demo/raw-input.jsonl')
    profiler.disable()
    profiler.dump_stats(str(OUT/'replay.prof'))
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.sort_stats('cumulative').print_stats(45)
    (OUT/'profile.txt').write_text(stream.getvalue(), encoding='utf-8')
    top = sorted(stats.stats.items(), key=lambda item:item[1][3], reverse=True)[:45]
    save('profile.json', {'seconds':time.perf_counter()-started, 'total_calls':stats.total_calls,
         'primitive_calls':stats.prim_calls, 'total_self_seconds':stats.total_tt,
         'functions':[{'file':key[0], 'line':key[1], 'name':key[2], 'primitive_calls':v[0],
                       'calls':v[1], 'self_seconds':v[2], 'cumulative_seconds':v[3]} for key,v in top]})
    print(stream.getvalue())


def retention():
    # Unique no-op timer events isolate Replay.seen_events growth. They intentionally
    # share logical time so no new trading decisions or order/timeouts are created.
    runner = Replay(engine())
    at = '2026-10-02T08:55:00+09:00'
    tracemalloc.start()
    rows = []
    for n in range(1, 100001):
        runner.dispatch({'event_id':f'timer-audit-{n}', 'received_at':at,
                         'sequence':n, 'type':'timer', 'data':{}})
        if n in (10000,50000,100000):
            current, peak = tracemalloc.get_traced_memory()
            rows.append({'events':n,'traced_current_bytes':current,'traced_peak_bytes':peak,
                         'seen_events':len(runner.seen_events),'audit_count':len(runner.engine.audit),
                         'memory':memory()})
    tracemalloc.stop()
    save('memory-retention.json', {'method':'tracemalloc after engine construction; no-op unique timers at fixed causal time; 100000 events',
                                 'measurements':rows})
    print(json.dumps(rows, indent=2))


def indexed_probe():
    """Audit-only alternative: causal index on baseline keys, no production edit."""
    replay_engine=engine()
    feature_engine=replay_engine.features
    index=defaultdict(list)
    original_add=feature_engine.add_volume_baseline
    def add(self, baseline):
        original_add(baseline)
        index[(baseline.symbol,baseline.source,baseline.window_seconds,baseline.end_second)].append(baseline)
    def lookup(self,symbol,at,window):
        local=at.astimezone(JST)
        second=local.hour*3600+local.minute*60+local.second
        day=local.date()
        by_day={}
        records=index.get((symbol,self.config.trade_source,window,second),())
        candidates=[r for r in records if r.day<day and r.known_at<=at]
        for row in sorted(candidates,key=lambda r:r.known_at):
            by_day[row.day]=row
        rows=[by_day[d] for d in sorted(by_day,reverse=True)[:self.config.rvol_days]]
        volumes=[r.volume for r in rows if r.valid]
        if len(volumes)<self.config.rvol_min_days:
            return None
        denominator=median(volumes)
        return denominator if denominator>=self.config.rvol_min_denominator and denominator>0 else None
    feature_engine.add_volume_baseline=MethodType(add,feature_engine)
    feature_engine._rvol_baseline=MethodType(lookup,feature_engine)
    runner=Replay(replay_engine)
    started=time.perf_counter()
    report=runner.run(ROOT/'runs/demo/raw-input.jsonl')
    seconds=time.perf_counter()-started
    expected=json.loads((ROOT/'runs/demo/report.json').read_text())
    save('indexed-probe.json',{'method':'Audit-only monkeypatch; index on (symbol,source,window_seconds,end_second); exact original temporal and revision filters retained; no disk-save.',
                              'run_wall_seconds':seconds,'events_per_second':6786/seconds,
                              'same_report_as_stored':report==expected,'baseline_key_count':len(index)})
    print((OUT/'indexed-probe.json').read_text())


def defects():
    at = datetime(2026,10,2,10,tzinfo=JST)
    order = SimpleNamespace(order_id=1, symbol='STOCK_SYNTHETIC', side=Side.BUY, emergency=False)
    quote = Quote(order.symbol, at, D('99.9'),D('100.1'),100,100,'q0',bid_at=at,ask_at=at)
    q = ExecutionQuality()
    q.on_submit(order,quote,at)
    q.on_fill('original',order,100,D('101'),at)
    q.on_fill('correction',order,100,D('102'),at+timedelta(seconds=1),correction=True)
    correction_result = {'reported':q.summary()['total_price_cost_vs_arrival'],'correct':'200'}
    q2=ExecutionQuality()
    q2.on_submit(order,quote,at)
    q2.on_fill('original',order,100,D('101'),at)
    q2.on_fill('bust',order,0,D('101'),at+timedelta(seconds=1),correction=True)
    bust_result = {'reported':q2.summary()['total_price_cost_vs_arrival'],'correct':'0'}
    q3=ExecutionQuality(horizons=(5,))
    q3.on_submit(order,quote,at)
    q3.on_fill('drift',order,100,D('100'),at)
    q3.observe(at,lambda symbol:quote,2)
    t4=at+timedelta(seconds=4)
    q4=Quote(order.symbol,t4,D('99.9'),D('100.1'),100,100,'q4',bid_at=t4,ask_at=t4)
    q3.observe(at+timedelta(seconds=5),lambda symbol:q4,2)
    t5=at+timedelta(seconds=5)
    q5=Quote(order.symbol,t5,D('100.9'),D('101.1'),100,100,'q5',bid_at=t5,ask_at=t5)
    q3.observe(t5,lambda symbol:q5,2)
    drift_result={'reported':q3.summary()['post_fill_drift']['BUY']['5s'],
                  'correct_5s_quote_bps':10000*__import__('math').log(1.01)}
    r=Replay(engine())
    bad={'event_id':'bad','received_at':at.isoformat(),'sequence':1,'type':'calendar',
         'data':{'day':'2026-10-02','is_open':True,'known_at':(at+timedelta(seconds=1)).isoformat(),'source':'audit'}}
    first=None
    try:r.dispatch(bad)
    except ValueError as err:first=str(err)
    r.dispatch(deepcopy(bad))
    replay_result={'first_error':first,'second_same_invalid_event':'silently returned',
                   'events_processed':r.events_processed,'seen_events':len(r.seen_events),
                   'calendar_rows':len(r.engine.calendar.records)}
    config=load_config(ROOT/'runs/demo/frozen-config.json')
    config['features']['rvol_days']=20.5
    c=build_engine(config)
    accepted=False
    try:c.features._rvol_baseline('STOCK_SYNTHETIC',at,30)
    except TypeError as err:accepted=str(err)
    config_result={'invalid_rvol_days':20.5,'build_engine':'ACCEPTED',
                   'first_baseline_lookup_error':accepted}
    save('defects.json',{'correction_cost_double_count':correction_result,'bust_cost_not_removed':bust_result,
                         'drift_uses_prehorizon_quote_and_freezes':drift_result,
                         'replay_invalid_event_pollutes_identity':replay_result,
                         'config_accepts_noninteger_history_count':config_result})
    print((OUT/'defects.json').read_text(encoding='utf-8'))


if __name__=='__main__':
    mode=argparse.ArgumentParser()
    mode.add_argument('task',choices=('benchmark','profile','retention','defects','indexed_probe'))
    globals()[mode.parse_args().task]()
