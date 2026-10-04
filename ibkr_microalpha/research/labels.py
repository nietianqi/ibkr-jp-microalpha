"""Complete-policy intent labels on recorded quotes (specification section 15).

A label is the net JPY of one intent under the same fixed aggressive policy the
coordinator runs: capped marketable entry with a short order TTL, stop from
ticks/bps, maximum holding, planned exit deadline, marketable exit at the bid,
all child-order commissions. Only quotes received after the decision are used.
Unfilled intents stay in the sample with their (zero) result; paths that cannot
be closed inside the data are returned as None and must not be dropped silently.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from ..domain import Quote, aware
from ..economics import CommissionSchedule
from ..market import TickTable

BPS = Decimal(10000)


@dataclass(frozen=True)
class LabelPolicy:
    holding_seconds: int
    entry_limit_ticks: int
    entry_order_ttl_seconds: float
    min_stop_ticks: int
    stop_bps: Decimal
    exit_slippage_ticks: int
    tick_category: str = "TOPIX500"

    def __post_init__(self):
        for name in ("holding_seconds", "min_stop_ticks"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("entry_limit_ticks", "exit_slippage_ticks"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if not self.entry_order_ttl_seconds > 0:
            raise ValueError("entry order TTL must be positive")
        if not isinstance(self.stop_bps, Decimal) or not self.stop_bps.is_finite() or self.stop_bps < 0:
            raise ValueError("stop_bps must be a nonnegative Decimal")


def label_intent(quotes: Sequence[Quote], start: int, quantity: int, policy: LabelPolicy,
                 ticks: TickTable, commissions: CommissionSchedule, deadline: datetime) -> Decimal | None:
    """Net JPY of the intent decided at ``quotes[start]`` (receive-time ordered)."""
    aware(deadline)
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("quantity must be a positive integer")
    decision = quotes[start]
    category = policy.tick_category
    cap = ticks.move_ticks(decision.ask, policy.entry_limit_ticks, decision.at, category)
    entry_index = None
    for index in range(start + 1, len(quotes)):
        quote = quotes[index]
        if (quote.at - decision.at).total_seconds() > policy.entry_order_ttl_seconds:
            break
        if quote.ask <= cap and quote.ask_size >= quantity:   # size shortfall: unfilled (conservative)
            entry_index = index
            break
    if entry_index is None:
        return Decimal(0)  # Unfilled intent: no price PnL and no fees, but kept as a sample.
    entry = quotes[entry_index]
    buy = entry.ask * quantity
    tick = ticks.tick_size(entry.ask, entry.at, category)
    stop = entry.ask - max(tick * policy.min_stop_ticks, policy.stop_bps * entry.ask / BPS)
    end = min(entry.at + timedelta(seconds=policy.holding_seconds), deadline)
    remaining, proceeds, triggered = quantity, Decimal(0), False
    for quote in quotes[entry_index + 1:]:
        triggered = triggered or quote.bid <= stop or quote.at >= end
        if not triggered:
            continue
        taken = min(remaining, quote.bid_size)   # discrete quotes include the gap; size walks the book
        proceeds += quote.bid * taken
        remaining -= taken
        if remaining == 0:
            return proceeds - buy - commissions.commission(buy) - commissions.commission(proceeds)
    return None
