"""Conservative, versioned quote simulator. Results are SIMULATED, never broker fills.

This is a conditional single-intent experiment, not a portfolio backtest. Quotes
cannot establish queue priority, market impact, or trades between observations.
Unknown coverage/residuals remain unknown and must be reported by the caller.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from math import isfinite, sqrt
from typing import Sequence

from ..domain import Quote, aware
from ..economics import CommissionSchedule
from ..market import QuoteQuality, TickTable, validate_quote

ZERO = Decimal(0)


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    symbol: str
    at: datetime
    score: float
    quantity: int
    volatility_bps: float | None = None
    volatility_window_seconds: int = 60

    def __post_init__(self):
        aware(self.at)
        if not self.decision_id or not self.symbol or isinstance(self.score,bool) or not isfinite(self.score):
            raise ValueError('decision requires identity, symbol and finite predeclared score')
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError('positive integer decision quantity required')
        if self.volatility_bps is not None and (not isfinite(self.volatility_bps) or self.volatility_bps < 0):
            raise ValueError('volatility must be finite and nonnegative')
        if type(self.volatility_window_seconds) is not int or self.volatility_window_seconds <= 0:
            raise ValueError('positive volatility window required')


@dataclass(frozen=True)
class SimulationPolicy:
    version: str = 'conservative-quote-v1'
    holding_seconds: int = 120
    submit_latency_seconds: float = 1
    ack_latency_seconds: float = 1
    cancel_latency_seconds: float = 1
    entry_ttl_seconds: float = 2
    exit_ttl_seconds: float = 3
    entry_limit_ticks: int = 1
    exit_slippage_ticks: int = 1
    min_stop_ticks: int = 5
    stop_bps: Decimal = Decimal('15')
    stop_volatility_multiple: float = 0
    participation_rate: Decimal = Decimal('.25')
    passive_entry: bool = False
    max_exit_children: int = 3
    tick_category: str = 'TOPIX500'
    max_quote_gap_seconds: float = 2
    quality: QuoteQuality = field(default_factory=QuoteQuality)

    def __post_init__(self):
        if not self.version:
            raise ValueError('simulator version must be frozen')
        for key in ('holding_seconds','min_stop_ticks','max_exit_children'):
            if type(getattr(self,key)) is not int or getattr(self,key) <= 0:
                raise ValueError(f'{key} must be a positive integer')
        for key in ('submit_latency_seconds','ack_latency_seconds','cancel_latency_seconds',
                    'entry_ttl_seconds','exit_ttl_seconds','stop_volatility_multiple','max_quote_gap_seconds'):
            value=getattr(self,key)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not isfinite(value) or value < 0:
                raise ValueError(f'{key} must be finite and nonnegative')
        if self.entry_ttl_seconds <= 0 or self.exit_ttl_seconds <= 0:
            raise ValueError('positive child-order TTL required')
        if self.max_quote_gap_seconds<=0 or not isinstance(self.quality,QuoteQuality):
            raise ValueError('positive quote coverage gap and typed QuoteQuality required')
        for key in ('entry_limit_ticks','exit_slippage_ticks'):
            if type(getattr(self,key)) is not int or getattr(self,key) < 0:
                raise ValueError(f'{key} must be nonnegative integer')
        if (not isinstance(self.stop_bps,Decimal) or not self.stop_bps.is_finite() or self.stop_bps < 0
                or not isinstance(self.participation_rate,Decimal) or not self.participation_rate.is_finite()
                or not ZERO < self.participation_rate <= 1 or type(self.passive_entry) is not bool):
            raise ValueError('invalid frozen stop, participation or entry mode')


@dataclass(frozen=True)
class SimulatedFill:
    child_id: str
    side: str
    quantity: int
    price: Decimal
    at: datetime
    quote_event_id: str


@dataclass(frozen=True)
class SimulationResult:
    decision_id: str
    symbol: str
    decision_at: datetime
    status: str
    reason: str
    target_quantity: int
    bought: int
    sold: int
    fees: Decimal
    net_amount: Decimal | None
    cash_flow: Decimal
    completed_at: datetime | None
    fills: tuple[SimulatedFill,...]
    policy: dict
    execution_source: str = 'SIMULATED'
    value_basis: str = 'REPLAY_POLICY_NET'


class LiquidityBudget:
    """Consume a visible side once per quote identity, shared across child orders."""
    def __init__(self):
        self.used = {}
        self.quotes = {}

    def take(self,quote,side,wanted,rate):
        key=(quote.symbol,quote.event_id)
        previous=self.quotes.setdefault(key,quote)
        if previous != quote:
            raise ValueError('conflicting quote identity in liquidity budget')
        available=int((quote.ask_size if side=='BUY' else quote.bid_size)*rate)
        # A timestamp/identity refresh of an unchanged displayed level is not
        # proof of replenishment after our simulated consumption. Reusing a
        # level is deliberately conservative until price or visible size changes.
        ledger=(quote.symbol,side,quote.ask if side=='BUY' else quote.bid,
                quote.ask_size if side=='BUY' else quote.bid_size)
        taken=max(0,min(wanted,available-self.used.get(ledger,0)))
        self.used[ledger]=self.used.get(ledger,0)+taken
        return taken


def simulate_intent(quotes: Sequence[Quote], decision: DecisionRecord, *, policy: SimulationPolicy,
                    commissions: CommissionSchedule, ticks: TickTable | None = None,
                    deadline: datetime | None = None, liquidity: LiquidityBudget | None = None) -> SimulationResult:
    ticks,liquidity=ticks or TickTable(),liquidity or LiquidityBudget()
    relevant=[quote for quote in quotes if quote.symbol==decision.symbol]
    if any(a.at>b.at for a,b in zip(relevant,relevant[1:])):
        raise ValueError('quotes must be ordered by receive time')
    identities={}
    unique=[]
    for quote in relevant:
        if quote.event_id in identities:
            if identities[quote.event_id] != quote:
                raise ValueError('conflicting quote identity')
            continue
        identities[quote.event_id]=quote
        unique.append(quote)
    relevant=unique
    if deadline is not None:
        aware(deadline)
    fills=[]
    child_notionals={}
    bought=sold=0
    first_fill=None
    entry_awareness=None
    stop=None
    exit_trigger=None
    exit_order=None
    exit_children=0
    entry_cancel_at=decision.at+timedelta(seconds=policy.entry_ttl_seconds+policy.cancel_latency_seconds)
    entry_arrives=decision.at+timedelta(seconds=policy.submit_latency_seconds)
    entry_ack=entry_arrives+timedelta(seconds=policy.ack_latency_seconds)
    entry_cancel_ack=entry_cancel_at+timedelta(seconds=policy.ack_latency_seconds)
    entry_limit=None
    invalid_data=False

    def result(status,reason,at=None):
        fees=sum((commissions.commission(value) for value in child_notionals.values()),ZERO)
        cash=sum((fill.price*fill.quantity*(1 if fill.side=='SELL' else -1) for fill in fills),ZERO)-fees
        return SimulationResult(decision.decision_id,decision.symbol,decision.at,status,reason,
            decision.quantity,bought,sold,fees,cash if status!='UNKNOWN' else None,cash,at,
            tuple(fills),asdict(policy))

    known=[quote for quote in relevant if quote.at<=decision.at]
    if not known or not validate_quote(known[-1],decision.at,ticks,policy.tick_category,policy.quality).allowed:
        return result('UNKNOWN','no valid decision-time quote')
    if deadline is not None and decision.at>=deadline:
        return result('UNKNOWN','decision is outside its exit deadline')
    if policy.stop_volatility_multiple and decision.volatility_bps is None:
        return result('UNKNOWN','volatility stop requires predecision realized volatility')
    initial=known[-1]
    previous_at = initial.at
    entry_limit=(initial.bid if policy.passive_entry else
                 ticks.move_ticks(initial.ask,policy.entry_limit_ticks,decision.at,policy.tick_category))

    def fill(side,child,quantity,price,quote):
        if quantity:
            fills.append(SimulatedFill(child,side,quantity,price,quote.at,quote.event_id))
            child_notionals[child]=child_notionals.get(child,ZERO)+price*quantity

    for quote in relevant:
        if quote.at<=decision.at:
            continue
        if (quote.at-previous_at).total_seconds() > policy.max_quote_gap_seconds:
            invalid_data=True
        previous_at=quote.at
        if not validate_quote(quote,quote.at,ticks,policy.tick_category,policy.quality).allowed:
            invalid_data=True
            continue
        if quote.at>=entry_arrives and quote.at<entry_cancel_at and bought<decision.quantity:
            marketable=quote.ask<entry_limit if policy.passive_entry else quote.ask<=entry_limit
            if marketable:
                taken=liquidity.take(quote,'BUY',decision.quantity-bought,policy.participation_rate)
                fill('BUY','entry:1',taken,quote.ask,quote)
                bought+=taken
                if taken and first_fill is None:
                    first_fill=quote.at
                    entry_awareness=quote.at+timedelta(seconds=policy.ack_latency_seconds)
                    distance=max(ticks.tick_size(quote.ask,quote.at,policy.tick_category)*policy.min_stop_ticks,
                                 policy.stop_bps*quote.ask/10000)
                    if policy.stop_volatility_multiple:
                        vol=Decimal(str(decision.volatility_bps*sqrt(policy.holding_seconds/decision.volatility_window_seconds)))
                        distance=max(distance,Decimal(str(policy.stop_volatility_multiple))*vol*quote.ask/10000)
                    stop=quote.ask-distance
        if bought==0:
            if quote.at>=max(entry_ack,entry_cancel_ack):
                return result('UNKNOWN' if invalid_data else 'UNFILLED',
                              'invalid data in entry window' if invalid_data else 'cancel acknowledged without fill',quote.at)
            continue
        if quote.at<entry_awareness:
            continue
        due=first_fill+timedelta(seconds=policy.holding_seconds)
        if deadline is not None:
            due=min(due,deadline)
        if exit_trigger is None and (quote.bid<=stop or quote.at>=due):
            exit_trigger=quote.at
            if bought<decision.quantity:
                # The entry remains executable during the cancellation latency.
                entry_cancel_at=min(entry_cancel_at,quote.at+timedelta(seconds=policy.cancel_latency_seconds))
                entry_cancel_ack=entry_cancel_at+timedelta(seconds=policy.ack_latency_seconds)
        if exit_trigger is None:
            continue
        entry_final=(quote.at>=entry_cancel_ack or
                     bought==decision.quantity and quote.at>=max(entry_ack,entry_awareness))
        if not entry_final:
            continue
        if exit_order is not None and quote.at>=exit_order['cancel_ack']:
            exit_order=None
        if exit_order is None and exit_children<policy.max_exit_children:
            exit_children+=1
            arrival=quote.at+timedelta(seconds=policy.submit_latency_seconds)
            try:
                limit=ticks.sell_limit_price(quote.bid,policy.exit_slippage_ticks,quote.at,policy.tick_category)
            except ValueError:
                return result('UNKNOWN','no legal exit limit')
            exit_order=dict(child=f'exit:{exit_children}',arrival=arrival,limit=limit,
                cancel_at=quote.at+timedelta(seconds=policy.exit_ttl_seconds+policy.cancel_latency_seconds),
                cancel_ack=quote.at+timedelta(seconds=policy.exit_ttl_seconds+policy.cancel_latency_seconds+policy.ack_latency_seconds))
        if (exit_order is not None and quote.at>=exit_order['arrival']
                and quote.at<exit_order['cancel_at'] and quote.bid>=exit_order['limit']):
            taken=liquidity.take(quote,'SELL',bought-sold,policy.participation_rate)
            fill('SELL',exit_order['child'],taken,quote.bid,quote)
            sold+=taken
            if sold==bought:
                return result('UNKNOWN' if invalid_data else 'COMPLETE',
                              'invalid data can change execution path' if invalid_data else 'terminal filled path',quote.at)
    return result('UNKNOWN','coverage ends before terminal execution/cancellation',relevant[-1].at if relevant else None)
