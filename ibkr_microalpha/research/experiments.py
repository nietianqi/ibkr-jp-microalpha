"""Preregistered walk-forward research and one-use final holdout.

Only simulated/recorded research observations are accepted. This module never
produces deployable calibration rows or accesses a broker. Test and holdout
outcomes cannot enter parameter fitting or validation-based policy selection.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
import json
import os
from math import isfinite
from pathlib import Path

from ..domain import aware
from ..market import JST
from .calibration import fit_scalers
from .scan import content_hash, day_block_interval, json_value
from .splits import walk_forward


class ExperimentRegistry:
    def __init__(self, directory):
        self.directory=Path(directory).resolve()

    def register(self, plan):
        required={'dataset_id','input_sha256','code_sha256','variants','split','statistics'}
        if not isinstance(plan,dict) or not required.issubset(plan) or not plan['dataset_id']:
            raise ValueError('preregister dataset, hashes, full grid, split and statistics')
        for field in ('input_sha256','code_sha256'):
            value=plan[field]
            if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError('input and code require lowercase SHA-256')
        identity=content_hash(plan)
        self.directory.mkdir(parents=True,exist_ok=True)
        path=self.directory/(identity+'.json')
        text=json.dumps(plan,sort_keys=True,default=json_value,ensure_ascii=False,indent=2)
        try:
            with path.open('x',encoding='utf-8') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if json.loads(path.read_text(encoding='utf-8'))!=json.loads(text):
                raise ValueError('conflicting registered experiment')
        return identity

    def claim_holdout(self, experiment_id, days):
        path=self.directory/(experiment_id+'.json')
        plan=json.loads(path.read_text(encoding='utf-8'))
        if content_hash(plan)!=experiment_id:
            raise ValueError('registered experiment was modified')
        identity=content_hash({'dataset':plan['dataset_id'],'days':[day.isoformat() for day in days]})
        target=self.directory/('holdout-'+identity+'.used')
        # Exclusive creation is also the concurrent claim boundary. A failed
        # evaluation remains consumed: it must not turn into another look.
        with target.open('x',encoding='utf-8') as stream:
            stream.write(json.dumps(dict(experiment_id=experiment_id,dataset_id=plan['dataset_id'],
                days=[d.isoformat() for d in days],status='CONSUMED_BEFORE_EVALUATION')))
            stream.flush()
            os.fsync(stream.fileno())


@dataclass(frozen=True)
class ResearchObservation:
    decision_id: str
    at: datetime
    completed_at: datetime
    features: dict
    outcomes: dict
    execution_source: str = 'SIMULATED'

    def __post_init__(self):
        aware(self.at)
        aware(self.completed_at)
        if not self.decision_id or self.completed_at<self.at:
            raise ValueError('causal decision and label completion required')
        if self.execution_source not in ('SIMULATED','BROKER_REPORTS','ARTIFICIAL'):
            raise ValueError('explicit research execution source required')
        if not isinstance(self.features,dict) or not isinstance(self.outcomes,dict):
            raise ValueError('predecision features and predeclared policy outcomes required')


def run_walk_forward(observations, variants, *, epsilon, registry, dataset_id,
                     train=40,validate=10,test=10,holdout=20,embargo=1,
                     alpha=.05,draws=2000,seed=7,block_days=1,use_final_holdout=False):
    observations=list(observations)
    variants=list(variants)
    if type(use_final_holdout) is not bool:
        raise ValueError('use_final_holdout must be an explicit boolean')
    if not observations or len({o.decision_id for o in observations})!=len(observations):
        raise ValueError('unique research observations required')
    if not variants or len({v['variant_id'] for v in variants})!=len(variants):
        raise ValueError('unique predeclared variants required')
    for variant in variants:
        if set(variant)!= {'variant_id','weights','score_threshold'} or set(variant['weights'])!=set(epsilon):
            raise ValueError('every variant freezes matching feature weights and score threshold')
        numbers=[variant['score_threshold'],*variant['weights'].values()]
        if any(type(v) not in (int,float) or not isfinite(v) for v in numbers):
            raise ValueError('finite frozen weights and score thresholds required')
        if any(o.outcomes.get(variant['variant_id']) is None for o in observations):
            # Never drop an adverse/unknown label after seeing its outcome.
            raise ValueError('all predeclared policy paths need complete outcomes before inference')
    days=sorted({o.at.astimezone(JST).date() for o in observations})
    folds,final_days=walk_forward(days,train=train,validate=validate,test=test,holdout=holdout,embargo=embargo)
    if not folds:
        raise ValueError('insufficient independent days for the registered split')
    from ..replay import code_hash
    plan=dict(dataset_id=dataset_id,input_sha256=content_hash(observations),code_sha256=code_hash(),
        variants=variants,epsilon=epsilon,split=dict(train=train,validate=validate,test=test,
        holdout=holdout,embargo=embargo),statistics=dict(alpha=alpha,draws=draws,seed=seed,
        block_days=block_days,multiple_testing='BONFERRONI',method='CIRCULAR_DAY_BLOCK_PERCENTILE'))
    identity=registry.register(plan)
    family_alpha=alpha/(len(variants)*(len(folds)+1))

    def subset(selected):
        selected=set(selected)
        return [o for o in observations if o.at.astimezone(JST).date() in selected]

    def fit(rows, as_of):
        if any(o.completed_at>=as_of for o in rows):
            raise ValueError('training labels were unavailable at validation/test time')
        scalers,clips=fit_scalers({name:[o.features[name] for o in rows] for name in epsilon},epsilon)
        return scalers,clips

    def evaluate(rows, variant, scalers):
        by_day={day:[] for day in sorted({o.at.astimezone(JST).date() for o in rows})}
        for o in rows:
            score=sum(variant['weights'][name]*scalers[name].transform(o.features[name]) for name in epsilon)
            value=Decimal(str(o.outcomes[variant['variant_id']])) if score>=variant['score_threshold'] else Decimal(0)
            if not value.is_finite():
                raise ValueError('finite complete outcomes required')
            by_day[o.at.astimezone(JST).date()].append(value)
        daily={day:[sum(values,Decimal(0))] for day,values in by_day.items()}
        if len(daily)<2:
            return dict(days=len(daily),mean_daily_net=None,lower=None,upper=None,status='INSUFFICIENT_DAYS')
        mean,lower,upper=day_block_interval(daily,alpha=family_alpha,draws=draws,seed=seed,block_days=block_days)
        return dict(days=len(daily),mean_daily_net=str(mean),lower=str(lower),upper=str(upper),
                    status='RESEARCH_ONLY',selected_policy=variant['variant_id'])

    fold_reports=[]
    validation_totals={v['variant_id']:Decimal(0) for v in variants}
    for index,fold in enumerate(folds):
        training,validation,testing=subset(fold.train),subset(fold.validate),subset(fold.test)
        validation_start=min(o.at for o in validation)
        scalers,clips=fit(training,validation_start)
        if any(o.completed_at>=min(x.at for x in testing) for o in validation):
            raise ValueError('validation labels were unavailable before outer test')
        scores={v['variant_id']:evaluate(validation,v,scalers) for v in variants}
        if any(row['mean_daily_net'] is None for row in scores.values()):
            raise ValueError('validation needs at least two days')
        selected=max(variants,key=lambda v:(Decimal(scores[v['variant_id']]['mean_daily_net']),v['variant_id']))
        for v in variants:
            validation_totals[v['variant_id']]+=Decimal(scores[v['variant_id']]['mean_daily_net'])
        fold_reports.append(dict(index=index,split=asdict(fold),
            fitted_scalers={name:asdict(value) for name,value in scalers.items()},clip_rates=clips,
            training_sha256=content_hash(training),validation=scores,
            selected_policy=selected['variant_id'],outer_test=evaluate(testing,selected,scalers)))
    holdout_report=dict(status='SEALED_NOT_READ',days=[d.isoformat() for d in final_days])
    if use_final_holdout:
        if not final_days:
            raise ValueError('no final holdout was registered')
        registry.claim_holdout(identity,final_days)
        final_rows=subset(final_days)
        boundary=min(o.at for o in final_rows)
        usable_days={day for fold in folds for day in (*fold.train,*fold.validate,*fold.test)}
        training=subset(usable_days-set(final_days))
        scalers,clips=fit(training,boundary)
        selected=max(variants,key=lambda v:(validation_totals[v['variant_id']],v['variant_id']))
        holdout_report=evaluate(final_rows,selected,scalers)
        holdout_report.update(status='CONSUMED_ONCE',clip_rates=clips,selection='PAST_VALIDATION_ONLY')
    return dict(experiment_id=identity,preregistered_plan=plan,folds=fold_reports,
        final_holdout=holdout_report,deployable=False,
        note='Offline conditional research only. All tried variants are registered. '
             'No deflated-Sharpe/SPA claim; simultaneous intervals are Bonferroni day-block estimates. '
             'Raw data truth, dependent regimes and portfolio/execution validity require separate evidence.')
