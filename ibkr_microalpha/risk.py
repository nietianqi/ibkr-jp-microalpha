"""Hard risk gates with atomic multi-symbol budget reservations.

Two lock levels (review STR-02):

* HARD locks (``lock_reasons``): daily loss, kill switch, ledger/reconciliation
  breaks, residual-risk alarms, escalated data failures. Only an explicit
  operator action can clear them; the coordinator exits controlled risk.
* SOFT blocks (``soft_blocks``): transient data or valuation gaps. They only
  stop new entries, clear automatically when the condition clears and are
  escalated to HARD by ``escalate`` once they persist past a frozen limit.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import isfinite
from threading import RLock
from typing import Callable, Mapping

from .domain import aware, finite_decimal


@dataclass(frozen=True)
class RiskConfig:
    capital: Decimal
    trade_risk_fraction: Decimal
    symbol_fraction: Decimal
    portfolio_fraction: Decimal
    daily_loss_fraction: Decimal
    sector_fraction: Decimal
    max_positions: int
    max_entry_intents_per_day: int
    max_quantity: int
    lot_size: int = 100
    # Sum of stress losses of held positions and working entries. The default is
    # the implied budget of the existing limits, never an unlimited value.
    portfolio_stress_fraction: Decimal | None = None
    # Routine (non-risk) broker requests per day; risk exits/cancels are never blocked.
    max_routine_requests_per_day: int | None = None
    require_account_snapshot: bool = False
    account_snapshot_max_age_seconds: float = 60

    def __post_init__(self):
        finite_decimal(self.capital, "capital", positive=True)
        for name in ("trade_risk_fraction", "symbol_fraction", "portfolio_fraction",
                     "daily_loss_fraction", "sector_fraction"):
            value = finite_decimal(getattr(self, name), name, positive=True)
            if value > 1:
                raise ValueError(f"{name} must be <= 1")
        for name in ("max_positions", "max_entry_intents_per_day", "max_quantity", "lot_size"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.portfolio_stress_fraction is None:
            object.__setattr__(self, "portfolio_stress_fraction",
                               self.trade_risk_fraction * self.max_positions)
        value = finite_decimal(self.portfolio_stress_fraction, "portfolio_stress_fraction", positive=True)
        if value > 1:
            raise ValueError("portfolio_stress_fraction must be <= 1")
        if self.max_routine_requests_per_day is not None and (
                type(self.max_routine_requests_per_day) is not int or self.max_routine_requests_per_day <= 0):
            raise ValueError("max_routine_requests_per_day must be a positive integer")
        if type(self.require_account_snapshot) is not bool:
            raise ValueError("require_account_snapshot must be a boolean")
        if (isinstance(self.account_snapshot_max_age_seconds, bool)
                or not isinstance(self.account_snapshot_max_age_seconds, (int, float))
                or not isfinite(self.account_snapshot_max_age_seconds)
                or self.account_snapshot_max_age_seconds <= 0):
            raise ValueError("account snapshot age must be finite and positive")


@dataclass(frozen=True)
class AccountSnapshot:
    """Broker-reported funds for the strategy account (review EXE-05)."""
    account_id: str
    currency: str
    available_funds: Decimal
    net_liquidation: Decimal
    received_at: datetime
    source: str

    def __post_init__(self):
        aware(self.received_at)
        if not self.account_id or not self.source or self.currency != "JPY":
            raise ValueError("account snapshot needs account, source and JPY funds")
        finite_decimal(self.available_funds, "available_funds")
        finite_decimal(self.net_liquidation, "net_liquidation")
        if self.available_funds < 0:
            raise ValueError("available funds cannot be negative")


@dataclass(frozen=True)
class Reservation:
    key: str
    symbol: str
    sector: str
    quantity: int
    notional: Decimal
    cash: Decimal
    stress_loss: Decimal


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    reservation: Reservation | None = None


class PortfolioRisk:
    def __init__(self, config: RiskConfig):
        self.config = config
        self._mutex = RLock()  # allocate() is atomic under concurrent callers (T15)
        self.reservations: dict[str, Reservation] = {}
        self.cash = Decimal(0)
        self.exposure: dict[str, Decimal] = {}
        self.stress: dict[str, Decimal] = {}
        self.sectors: dict[str, str] = {}
        self.lock_reasons: set[str] = set()
        self.soft_blocks: dict[str, datetime] = {}
        self.account_verified = False
        self.entry_intents_today = 0

    # ------------------------------------------------------------- lock levels
    @property
    def locked(self) -> bool:
        """HARD lock: operator action required, controlled risk is exited."""
        return bool(self.lock_reasons)

    @property
    def entries_blocked(self) -> bool:
        return bool(self.lock_reasons or self.soft_blocks)

    def lock(self, reason: str) -> None:
        with self._mutex:
            self.lock_reasons.add(reason)

    def block(self, reason: str, at: datetime) -> None:
        aware(at)
        with self._mutex:
            self.soft_blocks.setdefault(reason, at)

    def unblock(self, reason: str) -> None:
        with self._mutex:
            self.soft_blocks.pop(reason, None)

    def escalate(self, at: datetime, max_seconds: float) -> list[str]:
        """Turn SOFT blocks that outlived ``max_seconds`` into HARD locks."""
        aware(at)
        escalated = []
        with self._mutex:
            for reason, since in list(self.soft_blocks.items()):
                if (at - since).total_seconds() > max_seconds:
                    hard = f"escalated:{reason}"
                    if hard not in self.lock_reasons:
                        escalated.append(hard)
                    self.lock_reasons.add(hard)
        return escalated

    def verify_account(self, cash: Decimal, exposure: Mapping[str, Decimal],
                       sectors: Mapping[str, str], consistent: bool,
                       stress: Mapping[str, Decimal] | None = None) -> None:
        """Include external holdings and possible fills; cash is net of active orders."""
        with self._mutex:
            finite_decimal(cash, "cash")
            for symbol, value in exposure.items():
                finite_decimal(value, "exposure")
                if value < 0 or symbol not in sectors:
                    self.lock("unknown_account_exposure")
                    self.account_verified = False
                    return
            for value in (stress or {}).values():
                finite_decimal(value, "stress")
                if value < 0:
                    raise ValueError("stress loss cannot be negative")
            self.cash = cash
            self.exposure = dict(exposure)
            self.stress = dict(stress or {})
            self.sectors = dict(sectors)
            self.account_verified = consistent
            if not consistent or cash < 0:
                self.lock("account_unreconciled")

    def observe_daily_pnl(self, net_pnl: Decimal | None, at: datetime) -> None:
        """Unknown valuation is never zero: it blocks entries (SOFT) and escalates."""
        with self._mutex:
            if net_pnl is None or not net_pnl.is_finite():
                self.block("unvalued_daily_pnl", at)
                return
            self.unblock("unvalued_daily_pnl")
            if net_pnl <= -self.config.capital * self.config.daily_loss_fraction:
                self.lock("daily_loss")

    def manual_unlock(self, *, account_reconciled: bool, data_healthy: bool,
                      risk_approved: bool) -> None:
        """Explicit operator action only; daily-loss lock lasts the entire day."""
        with self._mutex:
            if any(type(flag) is not bool for flag in (account_reconciled, data_healthy, risk_approved)):
                raise ValueError('operator confirmations must be explicit booleans')
            if not (account_reconciled and data_healthy and risk_approved):
                raise ValueError("unlock requires verified account, data and operator approval")
            if "daily_loss" in self.lock_reasons:
                raise ValueError("daily loss cannot be unlocked within the trading day")
            self.lock_reasons.clear()

    def release(self, key: str) -> None:
        with self._mutex:
            self.reservations.pop(key, None)

    def note_entry_intent(self) -> None:
        with self._mutex:
            self.entry_intents_today += 1

    def allocate(self, key: str, symbol: str, sector: str, price: Decimal,
                 stop_distance: Decimal, exit_slippage: Decimal,
                 liquidity_quantity: int, requested_quantity: int,
                 fees: Callable[[int, Decimal], tuple[Decimal, Decimal]],
                 *, gap_reserve: Decimal = Decimal(0)) -> RiskDecision:
        """Re-evaluate minimum commissions at every legal lot; never enlarge size."""
        with self._mutex:
            if self.locked:
                return RiskDecision(False, "risk_locked")
            if self.soft_blocks:
                return RiskDecision(False, "entries_blocked")
            if not self.account_verified:
                return RiskDecision(False, "account_unverified")
            if key in self.reservations:
                previous = self.reservations[key]
                if previous.symbol != symbol or previous.sector != sector:
                    raise ValueError("reservation key collision")
                return RiskDecision(True, "already_reserved", previous)
            if not key or not symbol or not sector:
                return RiskDecision(False, "missing_ownership_or_sector")
            if self.entry_intents_today >= self.config.max_entry_intents_per_day:
                return RiskDecision(False, "entry_intent_limit")
            finite_decimal(price, "price", positive=True)
            finite_decimal(stop_distance, "stop_distance", positive=True)
            finite_decimal(exit_slippage, "exit_slippage")
            finite_decimal(gap_reserve, "gap_reserve")
            if exit_slippage < 0 or gap_reserve < 0 or stop_distance >= price:
                raise ValueError("invalid stress distance")
            reserved_symbols = {r.symbol for r in self.reservations.values()}
            occupied = {s for s, n in self.exposure.items() if n > 0} | reserved_symbols
            if symbol in occupied:
                return RiskDecision(False, "existing_position_or_intent")
            if len(occupied) >= self.config.max_positions:
                return RiskDecision(False, "position_limit")
            reserved_notional = sum((r.notional for r in self.reservations.values()), Decimal(0))
            used = sum(self.exposure.values(), Decimal(0)) + reserved_notional
            reserved_cash = sum((r.cash for r in self.reservations.values()), Decimal(0))
            sector_used = sum((n for s, n in self.exposure.items()
                               if self.sectors[s] == sector), Decimal(0))
            sector_used += sum((r.notional for r in self.reservations.values()
                                if r.sector == sector), Decimal(0))
            stress_used = (sum(self.stress.values(), Decimal(0))
                           + sum((r.stress_loss for r in self.reservations.values()), Decimal(0)))
            lot = self.config.lot_size
            upper = min(requested_quantity, liquidity_quantity, self.config.max_quantity)
            upper = (upper // lot) * lot
            c = self.config
            # Cash and notional bounds reduce enumeration even for large account inputs.
            max_notional = min(self.cash - reserved_cash, c.capital * c.symbol_fraction,
                               c.capital * c.portfolio_fraction - used,
                               c.capital * c.sector_fraction - sector_used)
            upper = min(upper, max(0, int(max_notional / price) // lot * lot))
            for quantity in range(upper, 0, -lot):
                entry_fee, exit_fee = fees(quantity, price)
                for value in (entry_fee, exit_fee):
                    finite_decimal(value, "fee")
                    if value < 0:
                        raise ValueError("negative fee reserve")
                notional = price * quantity
                cash = notional + entry_fee
                stress = quantity * (stop_distance + exit_slippage + gap_reserve) + entry_fee + exit_fee
                if cash > self.cash - reserved_cash:
                    continue
                if stress > c.capital * c.trade_risk_fraction:
                    continue
                if stress + stress_used > c.capital * c.portfolio_stress_fraction:
                    continue
                reservation = Reservation(key, symbol, sector, quantity, notional, cash, stress)
                self.reservations[key] = reservation
                return RiskDecision(True, "reserved", reservation)
            return RiskDecision(False, "less_than_one_legal_lot")
