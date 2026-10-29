"""Past-only per-security beta estimates with explicit shrinkage and expiry."""
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from .domain import aware
from .market import JST


@dataclass(frozen=True)
class BetaEstimate:
    symbol: str
    beta: float
    trained_until: datetime
    known_at: datetime
    valid_until: datetime
    sample_days: int
    shrinkage: float
    source: str
    version: str

    def __post_init__(self):
        for at in (self.trained_until,self.known_at,self.valid_until):
            aware(at)
        if not self.trained_until<self.known_at<self.valid_until:
            raise ValueError('beta training, availability and expiry must be causal')
        if not self.symbol or not self.source or not self.version:
            raise ValueError('beta identity and provenance required')
        if type(self.sample_days) is not int or self.sample_days<2:
            raise ValueError('beta requires independent paired days')
        if type(self.beta) not in (int,float) or not isfinite(self.beta):
            raise ValueError('finite beta required')
        if type(self.shrinkage) not in (int,float) or not 0<=self.shrinkage<=1:
            raise ValueError('shrinkage toward one must be frozen in [0,1]')


def fit_beta(symbol, stock_returns, benchmark_returns, *, trained_until,known_at,valid_until,
             shrinkage=.5,min_days=20,source,version):
    days=sorted(set(stock_returns)&set(benchmark_returns))
    if type(min_days) is not int or min_days<2 or len(days)<min_days:
        raise ValueError('insufficient independent paired days for beta')
    if any(type(day) is not date or day>trained_until.astimezone(JST).date() for day in days):
        raise ValueError('beta samples must precede the training cutoff')
    x=[benchmark_returns[day] for day in days]
    y=[stock_returns[day] for day in days]
    if any(type(v) not in (int,float) or not isfinite(v) for v in (*x,*y)):
        raise ValueError('finite paired returns required')
    xm,ym=sum(x)/len(x),sum(y)/len(y)
    denominator=sum((value-xm)**2 for value in x)
    if denominator<=0:
        raise ValueError('benchmark variance unavailable')
    raw=sum((a-xm)*(b-ym) for a,b in zip(x,y))/denominator
    return BetaEstimate(symbol,raw*(1-shrinkage)+shrinkage,trained_until,known_at,valid_until,
                        len(days),shrinkage,source,version)
