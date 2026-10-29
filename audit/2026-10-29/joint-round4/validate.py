"""Joint-round acceptance. Synthetic fixtures only; no broker connection."""
from collections import Counter
from datetime import datetime
from decimal import Decimal
import compileall
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[3]
OUTPUT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from ibkr_microalpha.config import build_engine,load_config
from ibkr_microalpha.replay import code_hash
from ibkr_microalpha.research.scan import json_value
from ibkr_microalpha.market import TickTable


def cases(suite):
    for item in suite:
        if isinstance(item,unittest.TestSuite):yield from cases(item)
        else:yield item


def run(arguments,log):
    with (OUTPUT/log).open('w',encoding='utf-8') as stream:
        completed=subprocess.run([sys.executable,*arguments],cwd=ROOT,stdout=stream,
                                  stderr=subprocess.STDOUT,check=False)
    if completed.returncode:
        raise RuntimeError('acceptance failed; inspect '+log)


def save(name,value):
    (OUTPUT/name).write_text(json.dumps(value,default=json_value,ensure_ascii=False,indent=2),encoding='utf-8')


def main():
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    counts=Counter(test.id().split('.')[0] for test in cases(suite))
    with (OUTPUT/'test-results.txt').open('w',encoding='utf-8') as stream:
        result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    summary=dict(client_date_jst='2026-10-29',python=sys.version,tests_run=result.testsRun,
        failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),
        new_round4_tests=sum(count for name,count in counts.items() if name.startswith('test_round4_')),
        tests_by_module=dict(counts),successful=result.wasSuccessful(),broker_connection_performed=False,
        live_readiness='NOT_VERIFIED',sdk_integration='NOT_VERIFIED',data='SYNTHETIC_UNIT_FIXTURES_ONLY')
    save('validation-results.json',summary)
    if not result.wasSuccessful():raise RuntimeError('unit acceptance failed')
    assert compileall.compile_dir(str(ROOT/'ibkr_microalpha'),quiet=1)
    assert compileall.compile_dir(str(ROOT/'tests'),quiet=1)
    run(['-m','ibkr_microalpha','validate-config','examples/research.json'],'config-validation.txt')
    run(['-m','ibkr_microalpha','validate-config','examples/guarded-demo.json'],'guarded-config-validation.txt')
    run(['-m','ibkr_microalpha','demo','--events',str(OUTPUT/'demo/events.jsonl'),
         '--output',str(OUTPUT/'demo'),'--verify-replay'],'demo-validation.txt')
    report=json.loads((OUTPUT/'demo/report.json').read_text(encoding='utf-8'))
    verification=json.loads((OUTPUT/'demo/verify/report.json').read_text(encoding='utf-8'))
    business=lambda r:{k:v for k,v in r.items() if k not in ('manifest','metrics')}
    assert business(report)==business(verification)
    assert report['funnel']['intents']==1 and report['active_orders']==0
    assert all(value['quantity']==0 for value in report['positions'].values())
    decision=report['intents'][0]
    (OUTPUT/'decisions.jsonl').write_text(json.dumps(dict(decision_id='SYNTHETIC_SCREEN',
        symbol=decision['symbol'],at=decision['created_at'],score=1,quantity=100))+'\n',encoding='utf-8')
    plan=json.loads((ROOT/'examples/holding-scan.json').read_text(encoding='utf-8'))
    plan['dataset_id']='SYNTHETIC_JOINT_ROUND4_DEMO'
    plan['scan'].update(score_edges=[0],draws=2000)
    save('screen-plan.json',plan)
    run(['-m','ibkr_microalpha','scan-holdings',str(OUTPUT/'demo/events.jsonl'),
        '--decisions',str(OUTPUT/'decisions.jsonl'),'--config','examples/research.json',
        '--plan',str(OUTPUT/'screen-plan.json'),'--registry',str(OUTPUT/'registry'),
        '--output',str(OUTPUT/'holding-screen.json')],'holding-screen.txt')
    scan=json.loads((OUTPUT/'holding-screen.json').read_text(encoding='utf-8'))
    assert not scan['deployable'] and scan['execution_source']=='SIMULATED'
    assert all(row['status'] not in ('POSITIVE_NET_EVIDENCE','NEGATIVE_EXPECTATION_EVIDENCE') for row in scan['rows'])
    from tests.test_round4_pipeline import WalkForwardTests
    from tests.test_round4_recording import metadata,BASE
    from ibkr_microalpha.recording import CaptureConfig
    observations=WalkForwardTests().observations()
    (OUTPUT/'observations.jsonl').write_text(''.join(json.dumps(row,default=json_value)+'\n'
                                               for row in observations),encoding='utf-8')
    experiment=dict(dataset_id='SYNTHETIC_JOINT_ROUND4_FOLDS',epsilon={'x':.1},
        variants=[dict(variant_id=name,weights={'x':1.0},score_threshold=-100.0) for name in ('a','b')],
        split=dict(train=4,validate=2,test=2,holdout=3,embargo=1),
        statistics=dict(alpha=.05,draws=400,seed=7,block_days=1))
    save('walk-forward-plan.json',experiment)
    run(['-m','ibkr_microalpha','walk-forward',str(OUTPUT/'observations.jsonl'),
        '--plan',str(OUTPUT/'walk-forward-plan.json'),'--registry',str(OUTPUT/'walk-registry'),
        '--output',str(OUTPUT/'walk-forward.json')],'walk-forward.txt')
    walked=json.loads((OUTPUT/'walk-forward.json').read_text(encoding='utf-8'))
    assert walked['final_holdout']['status']=='SEALED_NOT_READ'
    assert all(row['selected_policy']=='a' for row in walked['folds'])
    save('capture-config.json',CaptureConfig((metadata(),),session_id='SYNTHETIC_JOINT_ROUND4').to_dict())
    callbacks=[('marketDataType',dict(market_data_type=1)),('tickGeneric',dict(tick_type=49,value=0)),
        ('marketStatus',dict(status='CONTINUOUS',source='UNIT_ONLY',known_at=BASE.isoformat(),
                             valid_until=(BASE.replace(hour=10)).isoformat())),
        ('tickPrice',dict(tick_type=1,price='1000')),('tickSize',dict(tick_type=0,size=100)),
        ('tickPrice',dict(tick_type=2,price='1001')),('tickSize',dict(tick_type=3,size=100))]
    (OUTPUT/'callbacks.jsonl').write_text(''.join(json.dumps(dict(callback=kind,received_at=BASE.isoformat(),
        request_id=1,data=data))+'\n' for kind,data in callbacks),encoding='utf-8')
    # Capture deliberately requires fresh output directories; use an independent
    # attempt folder for repeat validation and preserve earlier evidence.
    import uuid
    capture=OUTPUT/('capture-'+uuid.uuid4().hex[:8])
    run(['-m','ibkr_microalpha','capture-callbacks',str(OUTPUT/'callbacks.jsonl'),
        '--capture-config',str(OUTPUT/'capture-config.json'),'--output',str(capture)],'capture.txt')
    captured=json.loads((capture/'capture.json').read_text(encoding='utf-8'))
    assert captured['callbacks']==7 and captured['events']==1 and not captured['execution_enabled']
    engine=build_engine(load_config(ROOT/'examples/research.json'))
    ticks=TickTable()
    at=datetime.fromisoformat('2026-10-29T10:00:00+09:00')
    costs=[]
    for value in ('300','960.3','3000','5000'):
        ask=Decimal(value)
        bid=ticks.move_ticks(ask,-1,at)
        buy=ticks.move_ticks(ask,1,at)
        sell=ticks.sell_limit_price(bid,1,at)
        fees=engine.commissions.commission(buy*100)+engine.commissions.commission(sell*100)
        price_cost=(buy-sell)*100
        costs.append(dict(reference_ask=value,quantity=100,entry_worst_limit=str(buy),
            exit_worst_limit=str(sell),fees=str(fees),price_cost=str(price_cost),
            round_trip_bps=str((fees+price_cost)/(ask*100)*10000)))
    save('illustrative-costs.json',dict(assumptions='Frozen illustrative schedule; flat reference market, '
        'one tick spread, one entry and one exit slippage tick, TOPIX500. Not actual account fees.',rows=costs))
    summary.update(compileall='PASSED',configs='PASSED',demo=dict(events=report['events_processed'],
        intents=report['funnel']['intents'],active_orders=report['active_orders'],business_replay_identical=True,
        net=report['daily_net_pnl_estimate']),holding_screen=dict(cells=scan['trial_count'],
        status_counts=dict(Counter(row['status'] for row in scan['rows'])),deployable=False))
    summary.update(capture=dict(callbacks=7,events=1,output=str(capture.relative_to(ROOT))),
                   walk_forward=dict(folds=len(walked['folds']),holdout='SEALED_NOT_READ'))
    files=[*sorted((ROOT/'ibkr_microalpha').rglob('*.py')),*sorted((ROOT/'tests').glob('*.py')),
           *sorted((ROOT/'examples').glob('*.json'))]
    manifest=dict(base_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        code_sha256=code_hash(),files={path.relative_to(ROOT).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in files})
    assert report['manifest']['code_sha256']==manifest['code_sha256']==verification['manifest']['code_sha256']
    save('source-manifest.json',manifest)
    save('validation-results.json',summary)
    print(json.dumps(dict(tests=result.testsRun,new_round4_tests=summary['new_round4_tests'],
        demo_replay_identical=True,research_cells=scan['trial_count'],live_readiness='NOT_VERIFIED')))


if __name__=='__main__':main()
