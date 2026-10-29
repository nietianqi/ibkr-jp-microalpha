"""File-based research commands, with protected sources and explicit evidence."""
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile

from ..config import build_engine, load_config
from ..recording import CallbackRecorder, CaptureConfig, record_callbacks
from ..replay import code_hash
from .experiments import ExperimentRegistry, ResearchObservation, run_walk_forward
from .scan import content_hash, json_value, quotes_from_replay_events, scan_holding_periods
from .simulation import DecisionRecord, SimulationPolicy


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _lines(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _event_lines(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def _file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def _protect(sources, targets):
    for source in map(lambda p:Path(p).resolve(),sources):
        for target in map(lambda p:Path(p).resolve(),targets):
            if source==target or source.exists() and target.exists() and source.samefile(target):
                raise ValueError('research output conflicts with input: '+str(source))


def _save(path, value, sources=()):
    path=Path(path).resolve()
    _protect(sources,[path])
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix='.research-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,default=json_value,ensure_ascii=False,allow_nan=False,indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _manifest(paths):
    return dict(code_sha256=code_hash(),inputs={str(Path(path).resolve()):
        _file_hash(path) for path in paths},live_readiness='NOT_VERIFIED')


def _policy(document):
    document=dict(document)
    for field in ('stop_bps','participation_rate'):
        if field in document:
            document[field]=Decimal(str(document[field]))
    if 'quality' in document:
        from ..market import QuoteQuality
        document['quality']=QuoteQuality(**document['quality'])
    return SimulationPolicy(**document)


def run_command(args):
    if args.command in ('capture-callbacks','capture-ibkr'):
        config=CaptureConfig.from_dict(_read(args.capture_config))
        sources=[args.capture_config]
        if args.command=='capture-callbacks':
            sources.append(args.callbacks)
        output=Path(args.output).resolve()
        names=('raw-callbacks.jsonl','events.jsonl','latency.jsonl','capture-config.json','capture.json','failure.json')
        _protect(sources,[output/name for name in names])
        if output.exists() and any(output.iterdir()):
            raise ValueError('capture needs a new or empty output directory')
        output.mkdir(parents=True,exist_ok=True)
        recorder=None
        try:
            with ExitStack() as stack:
                raw=stack.enter_context((output/'raw-callbacks.jsonl').open('w',encoding='utf-8'))
                events=stack.enter_context((output/'events.jsonl').open('w',encoding='utf-8'))
                latency=stack.enter_context((output/'latency.jsonl').open('w',encoding='utf-8'))
                recorder=CallbackRecorder(config,raw_sink=raw,events_sink=events,latency_sink=latency)
                if args.command=='capture-callbacks':
                    result=record_callbacks(args.callbacks,recorder=recorder)
                else:
                    from ..ibkr_readonly import capture_market_data
                    result=capture_market_data(recorder,host=args.host,port=args.port,client_id=args.client_id,
                                               duration_seconds=args.duration)
            result['manifest']=_manifest(sources)
            result['session_id']=recorder.session_id
            _save(output/'capture-config.json',config.to_dict(),sources)
            _save(output/'capture.json',result,sources)
        except Exception as error:
            if recorder is not None:
                recorder.failed=True
            _save(output/'failure.json',dict(status='FAILED',error=str(error),
                capture=recorder.summary() if recorder is not None else None),sources)
            raise
        print(f'Captured {result["callbacks"]} callbacks; {result["events"]} normalized events. {output}')
        return 0
    if args.command=='scan-holdings':
        sources=[args.events,args.decisions,args.config,args.plan]
        _protect(sources,[args.output])
        document=load_config(args.config)
        engine=build_engine(document)
        plan=_read(args.plan)
        decisions=[]
        for row in _lines(args.decisions):
            row['at']=datetime.fromisoformat(row['at'])
            decisions.append(DecisionRecord(**row))
        if not decisions:
            raise ValueError('predeclared decisions are empty')
        for decision in decisions:
            engine.parameter_guard.validate_as_of(decision.at)
        policy=_policy(plan.get('simulation',{}))
        options=dict(plan.get('scan',{}))
        allowed={'holding_periods','score_edges','min_days','min_samples','alpha','draws','seed',
                 'fee_multipliers','slippage_ticks','block_days'}
        if set(options)-allowed:
            raise ValueError('unknown holding scan parameters')
        manifest=_manifest(sources)
        registration=dict(dataset_id=plan['dataset_id'],input_sha256=content_hash(manifest['inputs']),
            code_sha256=manifest['code_sha256'],variants=dict(policy=asdict(policy),scan=options),
            split={'mode':'CONDITIONAL_HOLDING_SCREEN'},statistics=options)
        identity=ExperimentRegistry(args.registry).register(registration)
        result=scan_holding_periods(quotes_from_replay_events(_event_lines(args.events)),decisions,
            commissions=engine.commissions,policy=policy,**options)
        result.update(experiment_id=identity,manifest=manifest)
        _save(args.output,result,sources)
        print(f'Research only: {result["trial_count"]} preregistered cells; {args.output}')
        return 0
    if args.command=='walk-forward':
        sources=[args.observations,args.plan]
        _protect(sources,[args.output])
        plan=_read(args.plan)
        observations=[]
        for row in _lines(args.observations):
            for field in ('at','completed_at'):
                row[field]=datetime.fromisoformat(row[field])
            observations.append(ResearchObservation(**row))
        result=run_walk_forward(observations,plan['variants'],epsilon=plan['epsilon'],
            dataset_id=plan['dataset_id'],registry=ExperimentRegistry(args.registry),
            use_final_holdout=args.use_final_holdout,**plan.get('split',{}),**plan.get('statistics',{}))
        result['manifest']=_manifest(sources)
        _save(args.output,result,sources)
        print(f'Walk-forward completed; holdout={result["final_holdout"]["status"]}. {args.output}')
        return 0
    raise ValueError('unknown research command')
