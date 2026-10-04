"""Causal Tokyo market rules for offline research.

Calendar facts are supplied by the caller with the time they became known.  No
holiday or announcement is inferred from a weekday or a hindsight publication.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import StrEnum
from math import inf, isfinite

from .domain import Quote, aware, finite_decimal

# Fixed UTC+9 is intentional: Japan has no DST and this also works on Windows
# without an independently installed IANA timezone database.
JST = timezone(timedelta(hours=9), "Asia/Tokyo")


@dataclass(frozen=True)
class Gate:
    allowed: bool
    reason: str = ""

    def __bool__(self):
        return self.allowed


class JapanSession(StrEnum):
    PREOPEN = "PREOPEN"
    MORNING = "MORNING"
    LUNCH = "LUNCH"
    AFTERNOON = "AFTERNOON"
    CLOSING_AUCTION = "CLOSING_AUCTION"
    CLOSED = "CLOSED"


CONTINUOUS_SESSIONS = (JapanSession.MORNING, JapanSession.AFTERNOON)


def seconds_since_midnight(at: datetime) -> int:
    local = aware(at).astimezone(JST)
    return local.hour * 3600 + local.minute * 60 + local.second


@dataclass(frozen=True)
class SessionSchedule:
    """Exchange session rules and frozen strategy cut-offs in one place (JST).

    Official boundaries come from JPX; entry cut-offs, exit deadlines and the
    warmup are strategy choices that must be frozen with the configuration.
    """
    morning_open: time = time(9)
    morning_entry_cutoff: time = time(11, 20)
    morning_exit_deadline: time = time(11, 25)
    morning_close: time = time(11, 30)
    afternoon_open: time = time(12, 30)
    afternoon_entry_cutoff: time = time(15, 15)
    afternoon_exit_deadline: time = time(15, 20)
    continuous_end: time = time(15, 25)
    close: time = time(15, 30)
    warmup_seconds: float = 600

    def __post_init__(self):
        order = (self.morning_open, self.morning_entry_cutoff, self.morning_exit_deadline,
                 self.morning_close, self.afternoon_open, self.afternoon_entry_cutoff,
                 self.afternoon_exit_deadline, self.continuous_end, self.close)
        if any(not isinstance(value, time) for value in order) or any(
                left >= right for left, right in zip(order, order[1:])):
            raise ValueError("session times must be strictly increasing")
        if (isinstance(self.warmup_seconds, bool) or not isinstance(self.warmup_seconds, (int, float))
                or not isfinite(self.warmup_seconds) or self.warmup_seconds < 0):
            raise ValueError("warmup must be finite and nonnegative")

    @classmethod
    def from_mapping(cls, data):
        allowed = set(cls.__dataclass_fields__)
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"SessionSchedule unknown fields: {sorted(unknown)}")
        values = {}
        for name, value in data.items():
            if name == "warmup_seconds":
                values[name] = value
            elif isinstance(value, str):
                values[name] = time.fromisoformat(value)
            else:
                raise ValueError(f"session.{name} must be an HH:MM string")
        return cls(**values)

    def session(self, at: datetime) -> JapanSession:
        local = aware(at).astimezone(JST).time()
        if local < self.morning_open:
            return JapanSession.PREOPEN
        if local < self.morning_close:
            return JapanSession.MORNING
        if local < self.afternoon_open:
            return JapanSession.LUNCH
        if local < self.continuous_end:
            return JapanSession.AFTERNOON
        if local < self.close:
            return JapanSession.CLOSING_AUCTION
        return JapanSession.CLOSED

    def session_open(self, session: JapanSession) -> time:
        return self.morning_open if session == JapanSession.MORNING else self.afternoon_open

    def entry_cutoff(self, session: JapanSession) -> time:
        return self.morning_entry_cutoff if session == JapanSession.MORNING else self.afternoon_entry_cutoff

    def exit_deadline(self, at: datetime) -> datetime | None:
        local = aware(at).astimezone(JST)
        session = self.session(at)
        clock = (self.morning_exit_deadline if session == JapanSession.MORNING else
                 self.afternoon_exit_deadline if session == JapanSession.AFTERNOON else None)
        return datetime.combine(local.date(), clock, JST) if clock else None

    def planned_exit_due(self, at: datetime) -> bool:
        """Positions must be in planned exit from the entry cut-off onward."""
        session = self.session(at)
        if session not in CONTINUOUS_SESSIONS:
            return True
        return aware(at).astimezone(JST).time() >= self.entry_cutoff(session)

    def crosses_lunch(self, earlier: datetime, later: datetime) -> bool:
        first, second = earlier.astimezone(JST), later.astimezone(JST)
        return (first.date() == second.date() and first.time() < self.morning_close
                and second.time() >= self.afternoon_open)

    def lunch_seconds(self) -> float:
        start = datetime.combine(date(2000, 1, 1), self.morning_close)
        end = datetime.combine(date(2000, 1, 1), self.afternoon_open)
        return (end - start).total_seconds()


DEFAULT_SCHEDULE = SessionSchedule()


@dataclass(frozen=True)
class TradingDay:
    day: date
    is_open: bool
    known_at: datetime
    source: str
    version: str = "calendar-v1"
    policy_meeting: bool = False
    caution_tags: tuple[str, ...] = ()

    def __post_init__(self):
        aware(self.known_at)
        if type(self.is_open) is not bool or type(self.policy_meeting) is not bool:
            raise ValueError('calendar day flags must be booleans')
        if not self.source or not self.version:
            raise ValueError("calendar source and version are required")


@dataclass(frozen=True)
class ScheduledWindow:
    event_id: str
    start: datetime
    end: datetime
    known_at: datetime
    source: str

    def __post_init__(self):
        for value in (self.start, self.end, self.known_at):
            aware(value)
        if self.end <= self.start or not self.event_id or not self.source:
            raise ValueError("invalid scheduled calendar window")


@dataclass(frozen=True)
class CalendarAnnouncement:
    event_id: str
    received_at: datetime
    observation_seconds: float
    source: str
    announced_at: datetime | None = None  # Diagnostic only, never used to gate.

    def __post_init__(self):
        aware(self.received_at)
        if self.announced_at is not None:
            aware(self.announced_at)
        if not isfinite(self.observation_seconds) or self.observation_seconds < 0:
            raise ValueError("invalid announcement observation period")
        if not self.event_id or not self.source:
            raise ValueError("announcement source and identifier are required")


class JapanCalendar:
    def __init__(self, records=(), *, scheduled_windows=(), announcements=(),
                 blocked_caution_tags=(), opening_warmup_seconds=None, schedule=DEFAULT_SCHEDULE):
        self.records = list(records)
        self.scheduled_windows = list(scheduled_windows)
        self.announcements = list(announcements)
        self.blocked_caution_tags = frozenset(blocked_caution_tags)
        if not isinstance(schedule, SessionSchedule):
            raise ValueError("calendar requires a frozen session schedule")
        self.schedule = schedule
        warmup = schedule.warmup_seconds if opening_warmup_seconds is None else opening_warmup_seconds
        if not isfinite(warmup) or warmup < 0:
            raise ValueError("warmup must be finite and nonnegative")
        self.opening_warmup_seconds = float(warmup)
        self._sorted_announcements = 0

    def session(self, at: datetime) -> JapanSession:
        return self.schedule.session(at)

    def known_day(self, at: datetime) -> TradingDay | None:
        local = aware(at).astimezone(JST)
        available = [r for r in self.records if r.day == local.date() and r.known_at <= at]
        # Appended facts with the same receive timestamp preserve replay's
        # stable local event order: the most recently received fact wins.
        return max(reversed(available), key=lambda r: r.known_at, default=None)

    def planned_exit_deadline(self, at: datetime) -> datetime | None:
        return self.schedule.exit_deadline(at)

    def _announcements_in_receive_order(self):
        # Sorting once per newly appended announcement replaces a sort per gate call.
        if self._sorted_announcements != len(self.announcements):
            self.announcements.sort(key=lambda item: item.received_at)
            self._sorted_announcements = len(self.announcements)
        return self.announcements

    def entry_gate(self, at: datetime, *, max_hold_seconds: float,
                   remaining_wait_seconds: float = 0, submit_latency_seconds: float = 0,
                   exit_buffer_seconds: float = 0, market_status="CONTINUOUS") -> Gate:
        aware(at)
        durations = (max_hold_seconds, remaining_wait_seconds,
                     submit_latency_seconds, exit_buffer_seconds)
        if any(isinstance(x, bool) or not isinstance(x, (int, float, Decimal)) or
               not isfinite(x) or x < 0 for x in durations) or max_hold_seconds <= 0:
            return Gate(False, "INVALID_TIME_BUDGET")
        day = self.known_day(at)
        if day is None:
            return Gate(False, "CALENDAR_UNKNOWN")
        if not day.is_open:
            return Gate(False, "NOT_TRADING_DAY")
        if day.policy_meeting:
            return Gate(False, "POLICY_MEETING_DAY")
        if self.blocked_caution_tags.intersection(day.caution_tags):
            return Gate(False, "CALENDAR_CAUTION")
        for window in self.scheduled_windows:
            if window.known_at <= at and window.start <= at < window.end:
                return Gate(False, "KNOWN_EVENT_WINDOW")
        seen_announcements = set()
        for announcement in self._announcements_in_receive_order():
            if announcement.received_at > at:
                break
            if announcement.event_id in seen_announcements:
                continue
            seen_announcements.add(announcement.event_id)
            end = announcement.received_at + timedelta(seconds=announcement.observation_seconds)
            if announcement.received_at <= at < end:
                return Gate(False, "ANNOUNCEMENT_OBSERVATION")
        if market_status != "CONTINUOUS":
            return Gate(False, "NOT_CONTINUOUS")
        local = at.astimezone(JST)
        session = self.session(at)
        if session not in CONTINUOUS_SESSIONS:
            return Gate(False, "OUTSIDE_CONTINUOUS_SESSION")
        warmed = (datetime.combine(local.date(), self.schedule.session_open(session), JST)
                  + timedelta(seconds=self.opening_warmup_seconds))
        if local < warmed:
            return Gate(False, "SESSION_WARMUP")
        if local.time() >= self.schedule.entry_cutoff(session):
            return Gate(False, "ENTRY_CUTOFF")
        latest_exit = at + timedelta(seconds=sum(float(value) for value in durations))
        if latest_exit > self.planned_exit_deadline(at):
            return Gate(False, "INSUFFICIENT_HOLD_TIME")
        return Gate(True)


class TickTable:
    """JPX domestic shares table, reviewed 2026-10-03.

    The published change on 2027-03-01 is deliberately unsupported until a new
    verified table is installed. Security classification must come from contract
    metadata; an unknown class cannot silently receive the OTHER table.
    """
    valid_from = date(2023, 6, 5)
    valid_until = date(2027, 3, 1)
    _tables = {
        "TOPIX500": (("1000", ".1"), ("3000", ".5"), ("10000", "1"),
                     ("30000", "5"), ("100000", "10"), ("300000", "50"),
                     ("1000000", "100"), ("3000000", "500"), ("10000000", "1000"),
                     ("30000000", "5000"), ("Infinity", "10000")),
        "OTHER": (("3000", "1"), ("5000", "5"), ("30000", "10"),
                  ("50000", "50"), ("300000", "100"), ("500000", "500"),
                  ("3000000", "1000"), ("5000000", "5000"),
                  ("30000000", "10000"), ("50000000", "50000"),
                  ("Infinity", "100000")),
    }

    def tick_size(self, price: Decimal, at: datetime | date, category="TOPIX500") -> Decimal:
        finite_decimal(price, "price", positive=True)
        day = aware(at).astimezone(JST).date() if isinstance(at, datetime) else at
        if not self.valid_from <= day < self.valid_until:
            raise ValueError("tick table is not verified for this effective date")
        if category not in self._tables:
            raise ValueError("unknown security tick category")
        for ceiling, tick in self._tables[category]:
            if price <= Decimal(ceiling):
                return Decimal(tick)
        raise ValueError("no tick band")

    def is_legal(self, price: Decimal, at: datetime | date, category="TOPIX500") -> bool:
        try:
            return price % self.tick_size(price, at, category) == 0
        except (ValueError, ArithmeticError):
            return False

    def round_price(self, price: Decimal, direction: str, at: datetime | date,
                    category="TOPIX500") -> Decimal:
        tick = self.tick_size(price, at, category)
        if direction not in ("down", "up"):
            raise ValueError("rounding direction must be down or up")
        rounding = ROUND_FLOOR if direction == "down" else ROUND_CEILING
        result = (price / tick).to_integral_value(rounding=rounding) * tick
        # Crossing a tier can change legality. Recompute on the resulting price.
        while result > 0 and not self.is_legal(result, at, category):
            new_tick = self.tick_size(result, at, category)
            result = (result / new_tick).to_integral_value(rounding=rounding) * new_tick
        if result <= 0:
            raise ValueError("rounded price is nonpositive")
        return result

    def move_ticks(self, price: Decimal, count: int, at: datetime | date,
                   category="TOPIX500") -> Decimal:
        if not isinstance(count, int) or not self.is_legal(price, at, category):
            raise ValueError("tick movement requires a legal starting price and integer count")
        for _ in range(abs(count)):
            if count > 0:
                price = self.round_price(price + self.tick_size(price, at, category), "up", at, category)
            else:
                # Tick below an inclusive band boundary may be smaller.
                epsilon = Decimal("0.000000001")
                lower = price - self.tick_size(price - epsilon, at, category)
                if lower <= 0:
                    raise ValueError("price must be positive")
                price = self.round_price(lower, "down", at, category)
        return price


@dataclass(frozen=True)
class QuoteQuality:
    """Two explicit age limits: microstructure entry decisions need recently
    changed fields, while valuation and hard exits may use the last value of a
    still-healthy stream (no change is not a disconnection)."""
    max_age_seconds: float = 2
    max_field_skew_seconds: float = .5
    require_field_times: bool = True
    valuation_max_age_seconds: float = 30
    require_quote_heartbeat: bool = False

    def __post_init__(self):
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not isfinite(x) or x < 0
               for x in (self.max_age_seconds, self.max_field_skew_seconds, self.valuation_max_age_seconds)):
            raise ValueError("invalid quote age or skew")
        if self.valuation_max_age_seconds < self.max_age_seconds:
            raise ValueError("valuation age cannot be shorter than the entry age")
        for name in ("require_field_times", "require_quote_heartbeat"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")


def validate_quote(quote: Quote, now: datetime, ticks: "TickTable | None" = None,
                   category="TOPIX500", quality: QuoteQuality | None = None,
                   *, stream_healthy=True, purpose="entry",
                   schedule: SessionSchedule = DEFAULT_SCHEDULE) -> Gate:
    """Single quote validator for every layer. ``ticks=None`` skips the price
    grid check for callers that already received an engine-validated quote."""
    quality = quality or QuoteQuality()
    aware(now)
    if purpose not in ("entry", "valuation"):
        raise ValueError("quote purpose must be entry or valuation")
    if not stream_healthy:
        return Gate(False, "STREAM_UNHEALTHY")
    if quote.at > now:
        return Gate(False, "FUTURE_QUOTE")
    if quote.market_data_type != 1:
        return Gate(False, "NOT_REALTIME")
    if quote.market_status != "CONTINUOUS":
        return Gate(False, "NOT_CONTINUOUS")
    if quote.bid <= 0 or quote.ask <= quote.bid:
        return Gate(False, "INVALID_PRICES")
    if any(isinstance(x, bool) or not isinstance(x, int) or x <= 0
           for x in (quote.bid_size, quote.ask_size)):
        return Gate(False, "INVALID_SIZES")
    if ticks is not None and (not ticks.is_legal(quote.bid, now, category)
                              or not ticks.is_legal(quote.ask, now, category)):
        return Gate(False, "ILLEGAL_TICK")
    if quality.require_field_times and (quote.bid_at is None or quote.ask_at is None):
        return Gate(False, "FIELD_TIME_UNKNOWN")
    bid_at = quote.bid_at or quote.at
    ask_at = quote.ask_at or quote.at
    if bid_at > quote.at or ask_at > quote.at:
        return Gate(False, "FUTURE_FIELD")
    limit = quality.max_age_seconds if purpose == "entry" else quality.valuation_max_age_seconds
    if max((now - quote.at).total_seconds(), (now - bid_at).total_seconds(),
           (now - ask_at).total_seconds()) > limit:
        return Gate(False, "STALE_QUOTE")
    # An unchanged side is still current for valuation; synchronization only
    # matters for decisions that combine both fields at the same instant.
    if purpose == "entry" and abs((bid_at - ask_at).total_seconds()) > quality.max_field_skew_seconds:
        return Gate(False, "UNSYNCHRONIZED_QUOTE")
    if schedule.session(now) not in CONTINUOUS_SESSIONS:
        return Gate(False, "OUTSIDE_CONTINUOUS_SESSION")
    return Gate(True)


UNLIMITED_SKEW = inf
