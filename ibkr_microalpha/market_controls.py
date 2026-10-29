"""Causal per-security market facts; no broker or exchange facts are inferred.

Daily limits must be supplied by an authoritative source, including special
expansions and adjusted bases. Static configuration is never promoted to a
historical instrument classification. Restricted windows block entries only;
known trading status also controls whether a BBO may price a risk exit.
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from .domain import aware, finite_decimal
from .market import JST


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _time(value):
    if not isinstance(value, datetime):
        raise ValueError("market fact times must be timezone-aware datetimes")
    return aware(value)


def _provenance(record):
    for name in ('symbol', 'source', 'version'):
        _text(getattr(record, name), name)
    _time(record.known_at)


@dataclass(frozen=True)
class PriceBand:
    symbol: str
    day: date
    base: Decimal
    lower: Decimal
    upper: Decimal
    known_at: datetime
    source: str
    version: str
    valid_until: datetime | None = None

    def __post_init__(self):
        _provenance(self)
        if type(self.day) is not date:
            raise ValueError("price-band day must be a date")
        for name in ('base', 'lower', 'upper'):
            finite_decimal(getattr(self, name), name, positive=True)
        if not self.lower <= self.base <= self.upper:
            raise ValueError("price band must enclose its authoritative base")
        if self.valid_until is not None and _time(self.valid_until) <= self.known_at:
            raise ValueError("price-band expiry must follow its publication")


@dataclass(frozen=True)
class InstrumentRule:
    symbol: str
    effective_from: datetime
    effective_until: datetime
    known_at: datetime
    tick_category: str
    venue: str
    lot_size: int
    source: str
    version: str

    def __post_init__(self):
        _provenance(self)
        if _time(self.effective_from) >= _time(self.effective_until):
            raise ValueError("instrument rule needs a nonempty effective interval")
        _text(self.tick_category, 'tick_category')
        _text(self.venue, 'venue')
        if type(self.lot_size) is not int or self.lot_size <= 0:
            raise ValueError("instrument lot must be a positive integer")


@dataclass(frozen=True)
class SymbolStatus:
    symbol: str
    status: str
    known_at: datetime
    valid_until: datetime
    source: str
    version: str

    def __post_init__(self):
        _provenance(self)
        if self.status not in {'CONTINUOUS', 'UNKNOWN', 'HALT', 'SPECIAL_QUOTE'}:
            raise ValueError("unknown symbol trading status")
        if _time(self.valid_until) <= self.known_at:
            raise ValueError("status expiry must follow its publication")


@dataclass(frozen=True)
class RestrictedWindow:
    symbol: str
    start: datetime
    end: datetime
    known_at: datetime
    source: str
    version: str
    reason: str = 'event_window'

    def __post_init__(self):
        _provenance(self)
        if _time(self.start) >= _time(self.end):
            raise ValueError("restricted window must have positive duration")
        _text(self.reason, 'reason')


class MarketControls:
    def __init__(self, *, require_price_band=True, require_status=True,
                 require_instrument_rule=True):
        for value in (require_price_band, require_status, require_instrument_rule):
            if type(value) is not bool:
                raise ValueError("market control requirements must be explicit booleans")
        self.require_price_band = require_price_band
        self.require_status = require_status
        self.require_instrument_rule = require_instrument_rule
        self._bands = {}
        self._rules = {}
        self._statuses = {}
        self._windows = {}

    @staticmethod
    def _add(store, record, identity):
        rows = store.setdefault(record.symbol, [])
        for old in rows:
            if identity(old) == identity(record):
                if old != record:
                    raise ValueError("conflicting market facts at the same publication identity")
                return
        rows.append(record)

    def add_price_band(self, record: PriceBand, ticks, *, tick_category):
        if not isinstance(record, PriceBand):
            raise ValueError("PriceBand required")
        if not all(ticks.is_legal(price, record.day, tick_category)
                   for price in (record.base, record.lower, record.upper)):
            raise ValueError("authoritative price-band levels must be legal effective ticks")
        self._add(self._bands, record, lambda r:(r.day, r.known_at))

    def add_instrument_rule(self, record: InstrumentRule, ticks):
        if not isinstance(record, InstrumentRule):
            raise ValueError("InstrumentRule required")
        ticks.minimum_price(record.effective_from, record.tick_category)
        self._add(self._rules, record, lambda r:(r.effective_from, r.known_at))

    def add_status(self, record: SymbolStatus):
        if not isinstance(record, SymbolStatus):
            raise ValueError("SymbolStatus required")
        self._add(self._statuses, record, lambda r:r.known_at)

    def add_window(self, record: RestrictedWindow):
        if not isinstance(record, RestrictedWindow):
            raise ValueError("RestrictedWindow required")
        self._add(self._windows, record, lambda r:(r.start,r.end,r.known_at))

    def resolve_band(self, symbol, at):
        _time(at)
        rows = [r for r in self._bands.get(symbol, ())
                if r.day == at.astimezone(JST).date() and r.known_at <= at]
        latest = max(rows, key=lambda r:r.known_at, default=None)
        # An expired newer publication never resurrects an older reference.
        return latest if latest is not None and (latest.valid_until is None or at < latest.valid_until) else None

    def resolve_rule(self, symbol, at):
        _time(at)
        rows = [r for r in self._rules.get(symbol, ())
                if r.known_at <= at and r.effective_from <= at]
        latest = max(rows, key=lambda r:(r.effective_from,r.known_at), default=None)
        return latest if latest is not None and at < latest.effective_until else None

    def resolve_status(self, symbol, at):
        _time(at)
        latest = max((r for r in self._statuses.get(symbol, ()) if r.known_at <= at),
                     key=lambda r:r.known_at, default=None)
        return latest if latest is not None and at < latest.valid_until else None

    def _context(self, symbol, at, ticks, tick_category):
        rule = self.resolve_rule(symbol, at)
        if rule is None and self.require_instrument_rule:
            return None, 'instrument_rule_unknown_or_expired'
        if rule is not None and rule.venue not in {'TSE', 'TSEJ'}:
            return None, 'instrument_venue_unsupported'
        if rule is not None and rule.tick_category != tick_category:
            return None, 'instrument_rule_mismatch'
        status = self.resolve_status(symbol, at)
        if status is None and self.require_status:
            return None, 'symbol_status_unknown_or_expired'
        if status is not None and status.status != 'CONTINUOUS':
            return None, f'symbol_not_continuous:{status.status}'
        band = self.resolve_band(symbol, at)
        if band is None and self.require_price_band:
            return None, 'price_band_unknown_or_expired'
        if band is not None and not all(ticks.is_legal(p,at,tick_category)
                                       for p in (band.base,band.lower,band.upper)):
            return None, 'price_band_tick_mismatch'
        return band, None

    def entry_gate(self, symbol, at, price, stop_distance, *, ticks, tick_category,
                   upper_stop_multiple=Decimal('1'), quantity=None):
        finite_decimal(price, 'entry price', positive=True)
        finite_decimal(stop_distance, 'stop distance', positive=True)
        finite_decimal(upper_stop_multiple, 'upper stop multiple', positive=True)
        band, reason = self._context(symbol,at,ticks,tick_category)
        if reason:
            return reason
        if not ticks.is_legal(price,at,tick_category):
            return 'entry_price_tick_invalid'
        rule = self.resolve_rule(symbol,at)
        if quantity is not None and (type(quantity) is not int or quantity <= 0
                                     or (rule is not None and quantity % rule.lot_size)):
            return 'instrument_lot_mismatch'
        for window in self._windows.get(symbol, ()):
            if window.known_at <= at and window.start <= at < window.end:
                return f'restricted_window:{window.reason}'
        if band is not None:
            if not band.lower <= price <= band.upper:
                return 'price_outside_band'
            if band.upper - price < upper_stop_multiple * stop_distance:
                return 'upper_limit_stop_buffer'
        return None

    def quote_gate(self, symbol, at, bid, ask, *, ticks, tick_category):
        """A stale/unsupported context cannot create an account valuation."""
        band, reason = self._context(symbol,at,ticks,tick_category)
        if reason:
            return reason
        if band is not None and not band.lower <= bid <= ask <= band.upper:
            return 'quote_outside_authoritative_band'
        return None

    def exit_price(self, symbol, at, bid, slippage_ticks, *, ticks, tick_category):
        band, reason = self._context(symbol,at,ticks,tick_category)
        if reason:
            return None, reason
        if band is not None and not band.lower <= bid <= band.upper:
            return None, 'price_outside_band'
        try:
            price = ticks.sell_limit_price(bid,slippage_ticks,at,tick_category,
                                           lower=band.lower if band is not None else None,
                                           upper=band.upper if band is not None else None)
        except ValueError:
            return None, 'exit_price_tick_invalid'
        return price, None
