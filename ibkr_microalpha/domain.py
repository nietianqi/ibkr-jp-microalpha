from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from math import isfinite


class Regime(StrEnum):
    LONG = "LONG"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"
    RISK_OFF = "RISK_OFF"


class MarketRegime(StrEnum):
    MARKET_OK = "MARKET_OK"
    MARKET_CAUTION = "MARKET_CAUTION"
    MARKET_RISK_OFF = "MARKET_RISK_OFF"
    # Market data missing, stale or invalid: blocks entries (SOFT) and escalates
    # only through the risk timer; it is not evidence of a market emergency.
    MARKET_UNKNOWN = "MARKET_UNKNOWN"


class Confirmation(StrEnum):
    CONFIRM = "CONFIRM"
    WAIT = "WAIT"
    VETO = "VETO"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderState(StrEnum):
    LOCAL_CREATED = "LOCAL_CREATED"
    SUBMIT_PENDING = "SUBMIT_PENDING"
    WORKING = "WORKING"
    PART_FILLED = "PART_FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


def aware(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return at


def finite_decimal(value: Decimal, name: str, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Quote:
    symbol: str
    at: datetime
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    event_id: str
    source: str = "L1"
    market_data_type: int = 1
    market_status: str = "CONTINUOUS"
    bid_at: datetime | None = None
    ask_at: datetime | None = None
    exchange_at: datetime | None = None  # UNKNOWN when the source supplies none.

    def __post_init__(self):
        aware(self.at)
        for at in (self.bid_at, self.ask_at, self.exchange_at):
            if at is not None:
                aware(at)
        finite_decimal(self.bid, "bid")
        finite_decimal(self.ask, "ask")
        if not self.symbol or not self.event_id:
            raise ValueError("quote requires symbol and event_id")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class FeatureSnapshot:
    symbol: str
    at: datetime
    values: dict[str, float]
    valid: bool
    version: str = "l1-v1"
    reason: str = ""

    def __post_init__(self):
        aware(self.at)
        if not self.symbol or not isinstance(self.version, str) or not self.version:
            raise ValueError("snapshot requires symbol and version")
        if type(self.valid) is not bool:
            raise ValueError("snapshot valid flag must be a boolean")
        if not isinstance(self.values, dict) or any(
                not isinstance(name, str) or type(value) not in (int, float) or not isfinite(value)
                for name, value in self.values.items()):
            raise ValueError("feature values must be finite numbers")


@dataclass(frozen=True)
class Candidate:
    """A signal with a fixed TTL. Sizing, price cap and economics are attached
    later by the entry pipeline; they never extend or rewrite the signal."""
    candidate_id: str
    symbol: str
    created_at: datetime
    expires_at: datetime
    entry_score: float
    score_version: str
    reference_bid: Decimal
    reference_ask: Decimal

    def __post_init__(self):
        aware(self.created_at)
        aware(self.expires_at)
        if not self.candidate_id or not self.symbol or not self.score_version:
            raise ValueError("candidate identity and score version are required")
        if self.expires_at <= self.created_at:
            raise ValueError("invalid candidate TTL")
        if not isfinite(self.entry_score):
            raise ValueError("entry_score must be finite")
        for value in (self.reference_bid, self.reference_ask):
            finite_decimal(value, "candidate price", positive=True)
