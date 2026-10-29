"""Predeclared holding-period experiments; simulated screens cannot deploy.

Confidence intervals describe a frozen conditional simulation and its observed
coverage. They are not upper bounds on every possible execution policy.
"""
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from math import isfinite
import random

from ..domain import Quote
from ..market import DEFAULT_SCHEDULE, JST, TickTable
from .simulation import DecisionRecord, SimulationPolicy, SimulationResult, simulate_intent


def json_value(value):
    if isinstance(value, (date,datetime,Decimal)):
        return str(value) if isinstance(value,Decimal) else value.isoformat()
    if hasattr(value,'__dataclass_fields__'):
        return asdict(value)
    raise TypeError(f'unsupported research value {type(value).__name__}')


def content_hash(value):
    encoder=json.JSONEncoder(sort_keys=True,default=json_value,ensure_ascii=False,
                             separators=(',', ':'),allow_nan=False)
    digest=hashlib.sha256()
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode('utf-8'))
    return digest.hexdigest()


def day_block_interval(by_day, *, alpha=.05, draws=2000, seed=7, block_days=1):
    days = sorted(day for day,values in by_day.items() if values)
    if len(days)<2 or type(draws) is not int or draws<20 or not 0<alpha<1:
        raise ValueError('two independent days, >=20 draws and alpha in (0,1) required')
    if type(block_days) is not int or not 1<=block_days<=len(days):
        raise ValueError('block_days must fit observed days')
    values = [v for day in days for v in by_day[day]]
    if any(not isinstance(v,Decimal) or not v.is_finite() for v in values):
        raise ValueError('finite Decimal outcomes required')
    rng=random.Random(seed)
    sums={day:sum(by_day[day],Decimal(0)) for day in days}
    counts={day:len(by_day[day]) for day in days}
    means=[]
    for _ in range(draws):
        sampled=[]
        while len(sampled)<len(days):
            start=rng.randrange(len(days))
            sampled.extend(days[(start+j)%len(days)] for j in range(block_days))
        sampled=sampled[:len(days)]
        means.append(sum((sums[day] for day in sampled),Decimal(0))/sum(counts[day] for day in sampled))
    means.sort()
    low=means[min(draws-1,int(alpha/2*draws))]
    high=means[min(draws-1,int((1-alpha/2)*draws))]
    return sum(values,Decimal(0))/len(values),low,high


def quotes_from_replay_events(events):
    quotes=[]
    last=None
    identities={}
    for raw in events:
        event=json.loads(raw) if isinstance(raw,str) else dict(raw)
        at=datetime.fromisoformat(event['received_at'])
        if at.tzinfo is None or at.utcoffset() is None or type(event['sequence']) is not int:
            raise ValueError('causal receive timestamps and integral sequence required')
        key=(at,event['sequence'])
        digest=content_hash(event)
        identity=event['event_id']
        if identity in identities:
            if identities[identity]!=digest:
                raise ValueError('conflicting input identity')
            continue
        if last is not None and key<=last:
            raise ValueError('research input must retain original receive order')
        last=key
        identities[identity]=digest
        if event['type']!='quote':
            continue
        data=dict(event['data'])
        for field in ('bid','ask'):
            data[field]=Decimal(str(data[field]))
        for field in ('bid_at','ask_at','exchange_at'):
            if data.get(field) is not None:
                data[field]=datetime.fromisoformat(data[field])
        quotes.append(Quote(at=at,event_id=identity,**data))
    return quotes


def scan_holding_periods(quotes, decisions, *, commissions, ticks=None, policy=None,
                         holding_periods=(120,600,1800,3600),score_edges=(-1e12,),
                         min_days=20,min_samples=30,alpha=.05,draws=2000,seed=7,
                         fee_multipliers=(1,1.5),slippage_ticks=(1,2),block_days=1,
                         deadline_by_day=None):
    policy,ticks=policy or SimulationPolicy(),ticks or TickTable()
    quotes,decisions=list(quotes),list(decisions)
    if type(min_days) is not int or min_days<2 or type(min_samples) is not int or min_samples<1:
        raise ValueError('freeze independent days >=2 and positive sample threshold')
    if not 0<alpha<1 or type(draws) is not int or draws<20:
        raise ValueError('invalid interval parameters')
    edges=tuple(score_edges)
    if not edges or any(type(e) not in (float,int) or not isfinite(e) for e in edges) or sorted(set(edges))!=list(edges):
        raise ValueError('finite strictly increasing score edges required')
    periods=tuple(holding_periods)
    slips=tuple(slippage_ticks)
    fees=tuple(Decimal(str(v)) for v in fee_multipliers)
    if not periods or any(type(h) is not int or h<=0 for h in periods) or len(set(periods))!=len(periods):
        raise ValueError('unique positive holding periods required')
    if not slips or any(type(v) is not int or v<0 for v in slips) or len(set(slips))!=len(slips):
        raise ValueError('unique nonnegative slippage ticks required')
    if not fees or any(not v.is_finite() or v<=0 for v in fees) or len(set(fees))!=len(fees):
        raise ValueError('unique positive fee multipliers required')
    if any(not isinstance(d,DecisionRecord) for d in decisions) or len({d.decision_id for d in decisions})!=len(decisions):
        raise ValueError('unique predeclared DecisionRecord identities required')
    groups=defaultdict(list)
    for d in decisions:
        if d.score>=edges[0]:
            bucket=bisect_right(edges,d.score)-1
            groups[(d.symbol,d.quantity,bucket)].append(d)
    trial_count=len(periods)*len(slips)*len(fees)*len(groups)
    if trial_count>4096:
        raise ValueError('predeclared grid exceeds 4096 cells; split and record the larger experiment family')
    adjusted_alpha=alpha/max(1,trial_count)
    by_symbol=defaultdict(list)
    for quote in quotes:
        by_symbol[quote.symbol].append(quote)
    for values in by_symbol.values():
        if any(a.at>b.at for a,b in zip(values,values[1:])):
            raise ValueError('quotes must retain receive chronology')
    stamps={symbol:[q.at for q in values] for symbol,values in by_symbol.items()}
    rows=[]
    for (symbol,quantity,bucket), records in sorted(groups.items()):
        records=sorted(records,key=lambda d:(d.at,d.decision_id))
        for holding in periods:
            for slip in slips:
                variant=replace(policy,holding_seconds=holding,exit_slippage_ticks=slip)
                outcomes=[]
                for d in records:
                    values=by_symbol[symbol]
                    times=stamps.get(symbol,[])
                    start=max(0,bisect_right(times,d.at)-1)
                    deadline=(deadline_by_day or {}).get(d.at.astimezone(JST).date())
                    deadline=deadline or DEFAULT_SCHEDULE.exit_deadline(d.at)
                    if deadline is not None and d.at+timedelta(seconds=holding+
                            variant.entry_ttl_seconds+2*variant.submit_latency_seconds+variant.exit_ttl_seconds)>deadline:
                        outcomes.append(SimulationResult(d.decision_id,d.symbol,d.at,'INELIGIBLE_SESSION',
                            'frozen holding policy does not fit its session exit budget',d.quantity,0,0,
                            Decimal(0),Decimal(0),Decimal(0),d.at,(),asdict(variant)))
                        continue
                    tail=(variant.exit_ttl_seconds+variant.cancel_latency_seconds+variant.ack_latency_seconds)*variant.max_exit_children
                    cutoff=d.at+timedelta(seconds=holding+variant.entry_ttl_seconds+variant.submit_latency_seconds*2+tail+5)
                    stop=bisect_right(times,cutoff)
                    outcome=simulate_intent(values[start:stop],d,policy=variant,commissions=commissions,
                                            ticks=ticks,deadline=deadline)
                    outcomes.append(outcome)
                for fee_multiplier in fees:
                    counts=Counter(o.status for o in outcomes)
                    nets=defaultdict(list)
                    known=[]
                    for o in outcomes:
                        if o.net_amount is not None:
                            net=o.net_amount-(fee_multiplier-1)*o.fees
                            nets[o.decision_at.astimezone(JST).date()].append(net)
                            known.append(net)
                    complete=counts['UNKNOWN']==0
                    enough=complete and len(nets)>=min_days and len(known)>=min_samples
                    mean=lower=upper=None
                    status='INCOMPLETE_COVERAGE' if not complete else 'INSUFFICIENT_INDEPENDENT_DATA'
                    if enough:
                        mean,lower,upper=day_block_interval(nets,alpha=adjusted_alpha,draws=draws,seed=seed,block_days=block_days)
                        status=('NEGATIVE_EXPECTATION_EVIDENCE' if upper<0 else
                                'POSITIVE_NET_EVIDENCE' if lower>0 else 'INCONCLUSIVE')
                        if draws*adjusted_alpha/2<5:
                            status='INCONCLUSIVE_BOOTSTRAP_RESOLUTION'
                    rows.append(dict(symbol=symbol,quantity=quantity,score_low=edges[bucket],
                        score_high=edges[bucket+1] if bucket+1<len(edges) else None,holding_seconds=holding,
                        fee_multiplier=str(fee_multiplier),exit_slippage_ticks=slip,status=status,
                        intents=len(outcomes),independent_days=len(nets),coverage=dict(counts),
                        session_rejected=counts['INELIGIBLE_SESSION'],
                        all_intents_mean=str(mean) if mean is not None else None,
                        lower=str(lower) if lower is not None else None,upper=str(upper) if upper is not None else None,
                        known_only_mean=str(sum(known,Decimal(0))/len(known)) if known else None,
                        unknown_intents=[o.decision_id for o in outcomes if o.net_amount is None]))
    return dict(schema_version=1,execution_source='SIMULATED',deployable=False,trial_count=trial_count,
        statistics=dict(method='CIRCULAR_DAY_BLOCK_PERCENTILE',family_alpha=alpha,cell_alpha=adjusted_alpha,
                        multiple_testing='BONFERRONI',draws=draws,seed=seed,block_days=block_days,
                        tail_draws=draws*adjusted_alpha/2,
                        resolution_warning=draws*adjusted_alpha/2<5),
        policy=asdict(policy),commission_schedule=asdict(commissions),rows=rows,
        decisions_sha256=content_hash(decisions),quotes_sha256=content_hash(quotes),
        note='Conditional single-intent scenarios; not a portfolio backtest, execution upper bound, or broker fills. '
             'Unknown paths invalidate all-intent inference. Negative lower bound alone does not reject a policy. '
             'Venue state, universe selection and simulator assumptions require separate validation.')
