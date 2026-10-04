"""Offline order state machine and execution ledger; never submits to a broker.

Callbacks change memory and enqueue commands only. A separate consumer must persist
the journal, validate broker capabilities and execute commands. Broker status totals
are evidence for reconciliation, never a second source of position increments.
"""
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
from math import isfinite
from threading import RLock
from typing import Iterable, Mapping

from .domain import OrderState, Side, aware, finite_decimal

ZERO = Decimal(0)
TERMINAL = frozenset({OrderState.FILLED, OrderState.CANCELLED,
                      OrderState.REJECTED, OrderState.EXPIRED})


class ExecutionError(ValueError):
    """A local request violates the order safety protocol."""


def _quantity(value, *, zero=False) -> int:
    if isinstance(value, bool):
        raise ExecutionError("quantity must be an integral number of shares")
    result = Decimal(str(value))
    if not result.is_finite() or result != result.to_integral_value():
        raise ExecutionError("quantity must be finite integral shares")
    if result < 0 or (not zero and result == 0):
        raise ExecutionError("quantity must be positive")
    return int(result)


def _encode(value):
    if isinstance(value, (Decimal, datetime)):
        return str(value) if isinstance(value, Decimal) else value.isoformat()
    if isinstance(value, (Side, OrderState)):
        return value.value
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode(v) for v in value]
    return value


@dataclass
class Order:
    order_id: int
    intent_key: str
    symbol: str
    side: Side
    quantity: int
    limit_price: Decimal
    created_at: datetime
    state: OrderState = OrderState.LOCAL_CREATED
    filled_quantity: int = 0
    filled_notional: Decimal = ZERO
    reported_filled: int = 0
    reported_remaining: int | None = None
    submitted_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    updated_at: datetime | None = None
    reconciled: bool = False
    replaces: int | None = None
    emergency: bool = False
    entry_score: float | None = None
    score_version: str | None = None
    stop_distance: Decimal | None = None
    candidate_expires_at: datetime | None = None
    max_holding_seconds: int | None = None
    exit_policy_version: str | None = None

    @property
    def remaining_quantity(self) -> int:
        return max(0, self.quantity - self.filled_quantity)

    @property
    def possible_remaining(self) -> int:
        # A terminal report cannot release exposure while executions are missing.
        if self.state in TERMINAL and self.reconciled:
            return 0
        reported = max(0, (self.reported_remaining or 0) -
                       max(0, self.filled_quantity - self.reported_filled))
        return max(self.remaining_quantity, reported)

    @property
    def active(self) -> bool:
        return self.state not in TERMINAL or self.possible_remaining > 0


@dataclass(frozen=True)
class Fill:
    exec_id: str
    order_id: int
    quantity: int
    price: Decimal
    at: datetime
    sequence: int
    correction_of: str | None = None


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    average_price: Decimal = ZERO
    entry_time: datetime | None = None
    entry_score: float | None = None
    score_version: str | None = None
    stop_price: Decimal | None = None
    max_holding_seconds: int | None = None
    exit_policy_version: str | None = None
    realized_pnl: Decimal = ZERO  # Gross realized amount; fees reported separately.
    fees: Decimal = ZERO

    @property
    def realized_net_pnl(self) -> Decimal:
        return self.realized_pnl - self.fees


@dataclass(frozen=True)
class RequestCommand:
    kind: str
    order_id: int | None
    created_at: datetime
    risk: bool
    payload: dict


class TokenBucket:
    """Request pacing with capacity that routine submissions cannot consume."""
    def __init__(self, rate=50.0, capacity=50, reserved_risk_tokens=5):
        if (not isfinite(float(rate)) or rate <= 0 or
                type(capacity) is not int or capacity <= 0 or
                type(reserved_risk_tokens) is not int or
                not 0 <= reserved_risk_tokens < capacity):
            raise ValueError("invalid finite request pacing configuration")
        self.rate = float(rate)
        self.capacity = capacity
        self.reserve = reserved_risk_tokens
        self.tokens = float(capacity)
        self.last_at: datetime | None = None

    def consume(self, at: datetime, risk=False) -> bool:
        aware(at)
        if self.last_at is not None:
            elapsed = (at - self.last_at).total_seconds()
            if elapsed < 0:
                raise ExecutionError("request pacing clock moved backwards")
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_at = at
        floor = 0 if risk else self.reserve
        if self.tokens - 1 < floor:
            return False
        self.tokens -= 1
        return True


class ExecutionBook:
    def __init__(self, next_order_id=1, *, connected=True, request_rate=50.0,
                 request_capacity=50, reserved_risk_tokens=5, daily_request_budget=None):
        if type(next_order_id) is not int or next_order_id < 1:
            raise ValueError("next_order_id must be a positive integer")
        self.next_order_id = next_order_id
        self.connected = bool(connected)
        self.reconciling = True  # Even a connected startup must establish account ownership.
        self.lock_reasons: set[str] = set()
        self.orders: dict[int, Order] = {}
        self.positions: dict[str, Position] = {}
        self.executions: dict[str, Fill] = {}
        self.commissions: dict[str, Decimal] = {}
        self.broker_positions: dict[str, int] = {}
        self.unmanaged_orders: list[dict] = []
        self.commands: deque[RequestCommand] = deque()
        self.journal: list[dict] = []
        self.cash_flow = ZERO
        self._intent_orders: dict[str, int] = {}
        self._roots: dict[str, str] = {}
        self._current: dict[str, str] = {}
        self._sequence = 0
        self._fill_sequence = 0
        self._mutex = RLock()
        self.pacing = TokenBucket(request_rate, request_capacity, reserved_risk_tokens)
        if daily_request_budget is not None and (type(daily_request_budget) is not int or daily_request_budget <= 0):
            raise ValueError("daily request budget must be a positive integer")
        self.daily_request_budget = daily_request_budget
        self.sent_counts: Counter = Counter()        # every transmitted request by kind
        self.routine_requests_sent = 0               # non-risk requests counted against the budget
        self._order_fees: dict[int, Decimal] = {}
        self._stops: dict[str, Decimal] = {}         # journaled tightened stops per open position

    @property
    def locked(self) -> bool:
        return bool(self.lock_reasons)

    @property
    def reconciled(self) -> bool:
        return self.connected and not self.reconciling and not self.locked

    def _record(self, kind: str, at: datetime | None = None, **payload):
        self._sequence += 1
        self.journal.append(_encode({"sequence": self._sequence, "kind": kind,
                                     "at": at, "payload": payload}))

    def _lock(self, reason: str, at: datetime | None = None):
        self.lock_reasons.add(reason)
        self.reconciling = True
        self._record("LOCK", at, reason=reason)

    def _query(self, at: datetime):
        if not any(c.kind == "QUERY" for c in self.commands):
            self.commands.append(RequestCommand("QUERY", None, at, True,
                                                 {"positions": True, "orders": True,
                                                  "executions": True}))

    def active_orders(self, symbol: str | None = None) -> list[Order]:
        with self._mutex:
            return [order for order in self.orders.values()
                    if order.active and (symbol is None or order.symbol == symbol)]

    def sellable_quantity(self, symbol: str) -> int:
        with self._mutex:
            held = self.positions.get(symbol, Position(symbol)).quantity
            reserved = sum(o.possible_remaining for o in self.active_orders(symbol)
                           if o.side == Side.SELL)
            return max(0, held - reserved)

    def order_fees(self, order_id: int) -> Decimal:
        """Known fees for one child order, replacing superseded corrections.

        Maintained by ``_rebuild`` (every fill, correction and fee report), so a
        lookup is O(1) instead of a scan over the day's executions.
        """
        with self._mutex:
            if order_id not in self.orders:
                raise KeyError(order_id)
            return self._order_fees.get(order_id, ZERO)

    def current_executions(self) -> list[tuple[str, "Fill"]]:
        """Current revision of every execution root (the canonical fill facts)."""
        with self._mutex:
            return [(root, self.executions[current]) for root, current in self._current.items()]

    def root_of(self, exec_id: str) -> str | None:
        return self._roots.get(exec_id)

    def active_by_symbol(self) -> dict[str, list["Order"]]:
        """One pass over the order table, reused by a whole poll."""
        with self._mutex:
            result: dict[str, list[Order]] = defaultdict(list)
            for order in self.orders.values():
                if order.active:
                    result[order.symbol].append(order)
            return result

    def set_entry_snapshot(self, order_id: int, score: float | None, version: str | None,
                           *, source_at: datetime | None, at: datetime) -> None:
        """Journaled S_entry at first fill (review EXE-04); replayed by from_journal."""
        aware(at)
        with self._mutex:
            order = self.orders[order_id]
            if score is not None and not isfinite(float(score)):
                raise ExecutionError("entry score must be finite")
            order.entry_score, order.score_version = score, version
            self._record("ENTRY_SNAPSHOT", at, order_id=order_id, score=score, version=version,
                         source_at=source_at)
            self._rebuild(None)

    def tighten_stop(self, symbol: str, price: Decimal, at: datetime) -> None:
        """Journaled stop change; a stop can only be tightened, never widened."""
        aware(at)
        finite_decimal(price, "stop price", positive=True)
        with self._mutex:
            position = self.positions.get(symbol)
            if position is None or position.quantity <= 0:
                return
            if position.stop_price is None or price > position.stop_price:
                position.stop_price = price
                self._stops[symbol] = price
                self._record("STOP_TIGHTENED", at, symbol=symbol, stop_price=price)

    def prioritize_risk(self, order_id: int, at: datetime) -> Order:
        """Promote an unsent sell to reserved pacing capacity without replacing it.

        Prices and quantities stay fixed; drain_commands still rechecks quantity.
        Already transmitted orders retain their broker identity and submission.
        """
        aware(at)
        with self._mutex:
            order = self.orders[order_id]
            if order.side != Side.SELL:
                raise ExecutionError("only exit sells can use reserved risk capacity")
            if order.submitted_at is not None:
                return order
            queued = any(c.kind == "SUBMIT" and c.order_id == order_id
                         for c in self.commands)
            if not queued or order.emergency:
                return order
            order.emergency = True
            self.commands = deque(replace(c, risk=True, payload=_encode(asdict(order)))
                                  if c.kind == "SUBMIT" and c.order_id == order_id else c
                                  for c in self.commands)
            self._record("RISK_PRIORITY", at, order_id=order_id)
            return order

    def submit(self, intent_key: str, symbol: str, side: Side, quantity: int,
               limit_price: Decimal, at: datetime, *, entry_score=None,
               score_version=None, stop_distance=None, emergency=False, replaces=None,
               candidate_expires_at=None, max_holding_seconds=None,
               exit_policy_version=None) -> Order:
        aware(at)
        side = Side(side)
        quantity = _quantity(quantity)
        finite_decimal(limit_price, "limit_price", positive=True)
        if not intent_key or not symbol:
            raise ExecutionError("symbol and intent key are required")
        if entry_score is not None and not isfinite(float(entry_score)):
            raise ExecutionError("entry score must be finite")
        if stop_distance is not None:
            finite_decimal(stop_distance, "stop_distance", positive=True)
            if stop_distance >= limit_price:
                raise ExecutionError("stop distance must be below entry price")
        if candidate_expires_at is not None:
            aware(candidate_expires_at)
        if emergency and side == Side.BUY:
            raise ExecutionError("reserved risk request capacity is only for exits")
        if max_holding_seconds is not None:
            if type(max_holding_seconds) is not int or max_holding_seconds <= 0:
                raise ExecutionError("maximum holding duration must be finite positive seconds")
        with self._mutex:
            if intent_key in self._intent_orders:
                old = self.orders[self._intent_orders[intent_key]]
                if (old.symbol, old.side, old.quantity, old.limit_price) != (
                        symbol, side, quantity, limit_price):
                    raise ExecutionError("idempotency key reused with different order parameters")
                return old
            if candidate_expires_at is not None and candidate_expires_at <= at:
                raise ExecutionError("candidate expired")
            if not self.connected:
                raise ExecutionError("connection unavailable")
            if (self.locked or self.reconciling) and not (emergency and side == Side.SELL):
                raise ExecutionError("account state requires reconciliation")
            old = self.orders.get(replaces) if replaces is not None else None
            if replaces is not None:
                if (old is None or old.symbol != symbol or old.side != side or
                        old.state not in TERMINAL or not old.reconciled or
                        old.filled_quantity != old.reported_filled):
                    raise ExecutionError("replacement requires terminal and reconciled old order")
                if quantity > old.remaining_quantity:
                    raise ExecutionError("replacement exceeds old unfilled target")
            active = self.active_orders(symbol)
            if side == Side.BUY:
                if active or any(o.symbol == symbol and o.state in TERMINAL and
                                 not o.reconciled and o.state != OrderState.REJECTED
                                 for o in self.orders.values()):
                    raise ExecutionError("existing order must be reconciled before entry")
                if self.positions.get(symbol, Position(symbol)).quantity and old is None:
                    raise ExecutionError("adding to an existing position is forbidden")
            else:
                if not emergency and any(o.side == Side.BUY for o in active):
                    raise ExecutionError("normal exit first cancels and reconciles entry orders")
                if quantity > self.sellable_quantity(symbol):
                    raise ExecutionError("sell exceeds confirmed holdings less possible active sells")
                if any(reason.startswith("ledger:") for reason in self.lock_reasons):
                    raise ExecutionError("ledger quantity cannot support a verified risk sell")
            order = Order(self.next_order_id, intent_key, symbol, side, quantity,
                          limit_price, at, state=OrderState.SUBMIT_PENDING,
                          updated_at=at, replaces=replaces, emergency=bool(emergency),
                          entry_score=entry_score, score_version=score_version,
                          stop_distance=stop_distance, candidate_expires_at=candidate_expires_at,
                          max_holding_seconds=max_holding_seconds,
                          exit_policy_version=exit_policy_version)
            self.next_order_id += 1
            self.orders[order.order_id] = order
            self._intent_orders[intent_key] = order.order_id
            self.commands.append(RequestCommand("SUBMIT", order.order_id, at,
                                                 bool(emergency), _encode(asdict(order))))
            self._record("SUBMIT", at, order=asdict(order))
            return order

    def cancel(self, order_id: int, at: datetime) -> Order:
        aware(at)
        with self._mutex:
            order = self.orders[order_id]
            if order.state in TERMINAL or order.state == OrderState.CANCEL_PENDING:
                return order
            if order.state == OrderState.UNKNOWN:
                self._query(at)
                return order
            unsent = order.submitted_at is None and any(
                c.kind == "SUBMIT" and c.order_id == order_id for c in self.commands)
            if unsent:
                self.commands = deque(c for c in self.commands if c.order_id != order_id)
                order.state = OrderState.CANCELLED
                order.reconciled = True
            else:
                order.state = OrderState.CANCEL_PENDING
                order.reconciled = False
                self.commands.append(RequestCommand("CANCEL", order_id, at, True, {}))
            order.updated_at = at
            self._record("CANCEL", at, order_id=order_id)
            return order

    def status(self, order_id: int, ib_status: str, filled, remaining, at: datetime) -> Order:
        aware(at)
        with self._mutex:
            try:
                filled, remaining = _quantity(filled, zero=True), _quantity(remaining, zero=True)
            except (ValueError, ArithmeticError):
                self._lock("ledger: invalid broker status quantity", at)
                self._query(at)
                if order_id in self.orders:
                    self.orders[order_id].state = OrderState.UNKNOWN
                raise ExecutionError("invalid broker status quantity")
            if order_id not in self.orders:
                self._lock("ledger: unowned order status", at)
                self._query(at)
                raise ExecutionError("unknown order ID")
            order = self.orders[order_id]
            if order.submitted_at is None:
                order.submitted_at = at  # A broker report proves transmission occurred.
            self.commands = deque(c for c in self.commands if not (
                c.order_id == order_id and c.kind == "SUBMIT"))
            self._record("STATUS", at, order_id=order_id, ib_status=ib_status,
                         filled=filled, remaining=remaining)
            if filled + remaining > order.quantity or filled > order.quantity:
                self.ambiguous(order_id, at, "ledger: broker status quantity exceeds order")
                return order
            previous = order.state
            order.reported_filled = max(order.reported_filled, filled)
            order.reported_remaining = remaining
            mapping = {"PendingSubmit": OrderState.SUBMIT_PENDING,
                       "ApiPending": OrderState.SUBMIT_PENDING,
                       "PreSubmitted": OrderState.WORKING,
                       "Submitted": OrderState.WORKING,
                       "PendingCancel": OrderState.CANCEL_PENDING,
                       "Cancelled": OrderState.CANCELLED,
                       "ApiCancelled": OrderState.CANCELLED,
                       "Filled": OrderState.FILLED}
            state = mapping.get(ib_status, OrderState.UNKNOWN)
            if state == OrderState.UNKNOWN:
                self.ambiguous(order_id, at, f"unresolved IBKR status {ib_status}")
                return order
            # Late working/pending reports cannot undo confirmed cancellation or
            # execution completion; cancel requests remain pending until terminal.
            if previous in TERMINAL and state not in TERMINAL:
                return order
            if previous == OrderState.CANCEL_PENDING and state not in TERMINAL:
                state = OrderState.CANCEL_PENDING
            if state == OrderState.WORKING and order.filled_quantity:
                state = OrderState.PART_FILLED
            order.state = state
            order.updated_at = at
            if state in TERMINAL:
                self.commands = deque(c for c in self.commands if c.order_id != order_id)
                order.reconciled = False
                if state == OrderState.FILLED and filled != order.quantity:
                    self.ambiguous(order_id, at, "Filled status with inconsistent total")
            return order

    def reject(self, order_id: int, at: datetime, reason: str) -> Order:
        """Only use after an adapter establishes a definitive broker rejection."""
        aware(at)
        with self._mutex:
            order = self.orders[order_id]
            self.commands = deque(c for c in self.commands if c.order_id != order_id)
            order.state = OrderState.REJECTED
            order.updated_at = at
            order.reconciled = not order.filled_quantity
            self._record("REJECT", at, order_id=order_id, reason=reason)
            if order.filled_quantity:
                self.ambiguous(order_id, at, "rejection after executions requires reconciliation")
            return order

    def fill(self, exec_id: str, order_id: int, qty: int, price: Decimal,
             at: datetime, *, correction_of: str | None = None) -> Fill:
        aware(at)
        with self._mutex:
            try:
                qty = _quantity(qty, zero=correction_of is not None)
                finite_decimal(price, "fill price", positive=True)
                if not exec_id:
                    raise ExecutionError("execution ID is required")
            except (ValueError, ArithmeticError):
                self._lock("ledger: invalid broker execution fields", at)
                self._query(at)
                raise ExecutionError("invalid broker execution fields")
            if exec_id in self.executions:
                old = self.executions[exec_id]
                if (old.order_id, old.quantity, old.price, old.correction_of) != (
                        order_id, qty, price, correction_of):
                    self._lock("ledger: conflicting duplicate execution ID", at)
                    self._query(at)
                    raise ExecutionError("changed execution must supply a correction ID")
                return old
            if order_id not in self.orders:
                self._lock("ledger: execution for unknown order", at)
                self._query(at)
                self._record("UNRESOLVED_FILL", at, exec_id=exec_id, order_id=order_id,
                             qty=qty, price=price, correction_of=correction_of)
                raise ExecutionError("execution cannot be attributed to a controlled order")
            if correction_of is not None:
                previous = self.executions.get(correction_of)
                if previous is None or previous.order_id != order_id:
                    self._lock("ledger: unresolved execution correction", at)
                    self._query(at)
                    raise ExecutionError("correction requires an identified prior execution")
                root = self._roots[correction_of]
                if self._current[root] != correction_of:
                    self._lock("ledger: correction does not reference current revision", at)
                    self._query(at)
                    raise ExecutionError("stale correction cannot replace a newer execution")
            else:
                root = exec_id
            self._fill_sequence += 1
            fill = Fill(exec_id, order_id, qty, price, at, self._fill_sequence, correction_of)
            self.executions[exec_id] = fill
            self._roots[exec_id] = root
            self._current[root] = exec_id
            self._record("FILL", at, exec_id=exec_id, order_id=order_id, qty=qty,
                         price=price, correction_of=correction_of)
            order = self.orders[order_id]
            if order.submitted_at is None:
                if order.state in TERMINAL:
                    self._lock("ledger: execution after locally unsent terminal order", at)
                order.submitted_at = at
            self.commands = deque(c for c in self.commands if not (
                c.order_id == order_id and c.kind == "SUBMIT"))
            self._rebuild(at)
            order = self.orders[order_id]
            if order.state in TERMINAL:
                order.reconciled = False
            elif order.state not in {OrderState.CANCEL_PENDING, OrderState.UNKNOWN}:
                order.state = (OrderState.FILLED if order.filled_quantity == order.quantity
                               else OrderState.PART_FILLED)
            order.updated_at = at
            if correction_of is not None:
                self._enforce_sell_cover(at)
            return fill

    def commission(self, exec_id: str, amount: Decimal) -> None:
        with self._mutex:
            try:
                finite_decimal(amount, "commission")
                if not exec_id:
                    raise ExecutionError("commission requires an execution ID")
            except ValueError:
                self._lock("invalid broker commission report")
                raise ExecutionError("invalid broker commission report")
            if self.commissions.get(exec_id) == amount:
                return
            self.commissions[exec_id] = amount
            self._record("COMMISSION", exec_id=exec_id, amount=amount)
            self._rebuild(None)

    def _effective_commission(self, fill: Fill) -> Decimal:
        # A correction report replaces, rather than adds to, the prior fee.
        current = fill
        while True:
            if current.exec_id in self.commissions:
                return self.commissions[current.exec_id]
            if current.correction_of is None:
                return ZERO
            current = self.executions[current.correction_of]

    def _rebuild(self, at: datetime | None):
        previous = self.positions
        positions: dict[str, Position] = {}
        self.cash_flow = ZERO
        fees_by_order: dict[int, Decimal] = {}
        for order in self.orders.values():
            order.filled_quantity = 0
            order.filled_notional = ZERO
        fills = sorted((self.executions[key] for key in self._current.values()),
                       key=lambda f: (self.executions[self._roots[f.exec_id]].at,
                                      self.executions[self._roots[f.exec_id]].sequence))
        for fill in fills:
            order = self.orders[fill.order_id]
            order.filled_quantity += fill.quantity
            order.filled_notional += fill.price * fill.quantity
            pos = positions.setdefault(order.symbol, Position(order.symbol))
            fee = self._effective_commission(fill)
            fees_by_order[fill.order_id] = fees_by_order.get(fill.order_id, ZERO) + fee
            pos.fees += fee
            self.cash_flow -= fee
            if fill.quantity == 0:
                continue  # A broker bust reverses exposure, retaining applicable fees.
            if order.side == Side.BUY:
                if pos.quantity == 0:
                    origin = self.executions[self._roots[fill.exec_id]]
                    pos.entry_time = origin.at
                    pos.entry_score = order.entry_score
                    pos.score_version = order.score_version
                    prices = [f.price for f in self.executions.values()
                              if self._roots[f.exec_id] == origin.exec_id]
                    pos.stop_price = (max(prices) - order.stop_distance
                                      if order.stop_distance is not None else None)
                    pos.max_holding_seconds = order.max_holding_seconds
                    pos.exit_policy_version = order.exit_policy_version
                new_quantity = pos.quantity + fill.quantity
                if new_quantity > 0:
                    pos.average_price = ((pos.average_price * pos.quantity +
                                          fill.price * fill.quantity) / new_quantity)
                pos.quantity = new_quantity
                self.cash_flow -= fill.price * fill.quantity
            else:
                pos.realized_pnl += (fill.price - pos.average_price) * fill.quantity
                pos.quantity -= fill.quantity
                self.cash_flow += fill.price * fill.quantity
                if pos.quantity < 0:
                    self._lock("ledger: execution produces an uncontrolled short position", at)
                if pos.quantity == 0:
                    pos.average_price = ZERO
                    pos.entry_time = None
                    pos.stop_price = None
                    pos.entry_score = None
                    pos.score_version = None
        # Fee updates and late partial fills never widen a previously fixed stop.
        for symbol, pos in positions.items():
            old = previous.get(symbol)
            if (old and old.quantity > 0 and pos.quantity > 0 and
                    old.entry_time is not None and pos.entry_time is not None and
                    pos.entry_time <= old.entry_time and old.stop_price is not None):
                pos.stop_price = max(old.stop_price, pos.stop_price or old.stop_price)
            if pos.quantity > 0 and symbol in self._stops:
                pos.stop_price = max(pos.stop_price or self._stops[symbol], self._stops[symbol])
            elif pos.quantity <= 0:
                self._stops.pop(symbol, None)
        self.positions = positions
        self._order_fees = fees_by_order
        for order in self.orders.values():
            if order.filled_quantity > order.quantity:
                order.state = OrderState.UNKNOWN
                self._lock("ledger: execution total exceeds order quantity", at)
        if at is not None and self.locked:
            self._query(at)

    def _enforce_sell_cover(self, at: datetime) -> None:
        """Invariant: possible remaining of active sells <= confirmed controlled holding.

        A correction or bust can lower the holding below sells already queued or
        transmitted (review EXE-01). Unsent sells are dropped locally; transmitted
        sells get a cancel request (their exposure stays reserved until a terminal
        report) and the ledger locks and queries the broker.
        """
        for symbol in sorted({o.symbol for o in self.orders.values() if o.side == Side.SELL and o.active}):
            held = self.positions.get(symbol, Position(symbol)).quantity
            sells = [o for o in self.active_orders(symbol) if o.side == Side.SELL]
            if sum(o.possible_remaining for o in sells) <= max(0, held):
                continue
            transmitted = False
            for order in sells:
                transmitted |= order.submitted_at is not None
                self.cancel(order.order_id, at)
            self._record("SELL_COVER_BREACH", at, symbol=symbol, held=held,
                         sells=[o.order_id for o in sells])
            if transmitted:
                self._lock("ledger: active sells exceed confirmed holding", at)
                self._query(at)

    def ambiguous(self, order_id: int, at: datetime, reason: str) -> None:
        aware(at)
        with self._mutex:
            self.orders[order_id].state = OrderState.UNKNOWN
            self.orders[order_id].reconciled = False
            # Unsent commands may be invalid after an unknown outcome; never replay.
            self.commands = deque(c for c in self.commands if c.order_id != order_id)
            self._lock(reason, at)
            self._query(at)
            self._record("AMBIGUOUS", at, order_id=order_id, reason=reason)

    def disconnect(self, at: datetime) -> None:
        aware(at)
        with self._mutex:
            self.connected = False
            self._lock("connection interrupted", at)
            for order in self.active_orders():
                order.state = OrderState.UNKNOWN
                order.reconciled = False
            self.commands.clear()
            self._record("DISCONNECT", at)

    def reconnect(self, at: datetime) -> None:
        aware(at)
        with self._mutex:
            self.connected = True
            self.reconciling = True
            self._query(at)
            self._record("RECONNECT", at)

    def drain_commands(self, at: datetime, max_count: int = 50, *,
                       entry_validator=None) -> list[RequestCommand]:
        """Return paced requests, risk requests first. Sending remains external.

        ``entry_validator(order, at) -> reason | None`` re-checks every unsent
        entry at the actual send time (review EXE-02); a failing entry is
        provably unsent and is aborted locally.
        """
        aware(at)
        if type(max_count) is not int or max_count < 0:
            raise ValueError("max_count must be a nonnegative integer")
        with self._mutex:
            if not self.connected:
                return []
            result = []
            pending = sorted(self.commands, key=lambda command: not command.risk)
            self.commands.clear()
            for command in pending:
                if command.kind == "SUBMIT":
                    order = self.orders[command.order_id]
                    if order.side == Side.BUY and entry_validator is not None:
                        reason = entry_validator(order, at)
                        if reason:
                            order.state = OrderState.CANCELLED
                            order.reconciled = True
                            self._record("LOCAL_ABORT", at, order_id=order.order_id, reason=reason)
                            continue
                    if order.candidate_expires_at and at >= order.candidate_expires_at:
                        order.state = OrderState.EXPIRED
                        order.reconciled = True
                        self._record("LOCAL_EXPIRED", at, order_id=order.order_id)
                        continue
                    if order.side == Side.SELL:
                        held = self.positions.get(order.symbol, Position(order.symbol)).quantity
                        other_sells = sum(other.possible_remaining
                                          for other in self.active_orders(order.symbol)
                                          if other.side == Side.SELL and
                                          other.order_id != order.order_id)
                        if (order.quantity > max(0, held - other_sells) or
                                any(reason.startswith("ledger:") for reason in self.lock_reasons)):
                            # Quantity changed after allocation but before dispatch.
                            # This request is provably unsent, so local cancellation
                            # releases it without inventing a broker acknowledgement.
                            order.state = OrderState.CANCELLED
                            order.reconciled = True
                            self._record("LOCAL_ABORT", at, order_id=order.order_id,
                                         reason="risk sell quantity is no longer verified")
                            continue
                        if not order.emergency and any(other.side == Side.BUY
                                                       for other in self.active_orders(order.symbol)):
                            self.commands.append(command)
                            self._query(at)
                            continue
                    if (self.locked or self.reconciling) and not order.emergency:
                        self.commands.append(command)
                        continue
                if (not command.risk and self.daily_request_budget is not None
                        and self.routine_requests_sent >= self.daily_request_budget):
                    # Routine requests stop at the frozen daily budget; risk requests
                    # (exits, cancels, queries) are never blocked by it.
                    if command.kind == "SUBMIT":
                        order = self.orders[command.order_id]
                        order.state = OrderState.CANCELLED
                        order.reconciled = True
                        self._record("LOCAL_ABORT", at, order_id=order.order_id,
                                     reason="routine request budget exhausted")
                        continue
                    self.commands.append(command)
                    continue
                if len(result) >= max_count or not self.pacing.consume(at, command.risk):
                    self.commands.append(command)
                    continue
                result.append(command)
                self.sent_counts[command.kind] += 1
                if not command.risk:
                    self.routine_requests_sent += 1
                if command.order_id is not None:
                    order = self.orders[command.order_id]
                    if command.kind == "SUBMIT":
                        order.submitted_at = at
                    elif command.kind == "CANCEL":
                        order.cancel_requested_at = at
                self._record("COMMAND_SENT", at, kind_sent=command.kind,
                             order_id=command.order_id)
            return result

    def check_timeouts(self, at: datetime, *, submit_timeout_seconds=5.0,
                       cancel_timeout_seconds=5.0) -> list[int]:
        aware(at)
        for value in (submit_timeout_seconds, cancel_timeout_seconds):
            if not isfinite(float(value)) or value <= 0:
                raise ValueError("timeouts must be finite and positive")
        timed_out = []
        with self._mutex:
            for order in self.orders.values():
                if order.candidate_expires_at and at >= order.candidate_expires_at:
                    if order.side == Side.BUY and order.active:
                        self.cancel(order.order_id, at)
                started = (order.cancel_requested_at if order.state == OrderState.CANCEL_PENDING
                           else order.submitted_at if order.state == OrderState.SUBMIT_PENDING
                           else None)
                timeout = (cancel_timeout_seconds if order.state == OrderState.CANCEL_PENDING
                           else submit_timeout_seconds)
                if started and (at - started).total_seconds() >= timeout:
                    timed_out.append(order.order_id)
                    self.ambiguous(order.order_id, at, "request timed out; outcome unknown")
            return timed_out

    def reconcile(self, positions: Mapping[str, int], open_orders: Iterable[Mapping],
                  executions: Iterable[Mapping], at: datetime, *, complete=False,
                  ownership_confirmed=False, next_order_id: int | None = None) -> bool:
        """Apply one complete, strategy-scoped broker snapshot at a query barrier.

        Absence from a partial open-order list cannot prove cancellation. The
        caller must establish completeness, ownership and snapshot consistency.
        Corrections in executions must identify their prior execution explicitly.
        """
        aware(at)
        with self._mutex:
            self.reconciling = True
            try:
                open_orders = [dict(row) for row in open_orders]
                executions = [dict(row) for row in executions]
                self.broker_positions = {}
                for symbol, qty in positions.items():
                    numeric = Decimal(str(qty))
                    if not numeric.is_finite() or numeric != numeric.to_integral_value():
                        raise ExecutionError("broker position must contain integral shares")
                    self.broker_positions[symbol] = int(numeric)
            except (ValueError, TypeError, ArithmeticError, AttributeError):
                self._lock("ledger: invalid broker snapshot fields", at)
                self._query(at)
                return False
            self._record("RECONCILE", at, positions=self.broker_positions,
                         open_orders=open_orders, executions=executions,
                         complete=complete, ownership_confirmed=ownership_confirmed,
                         next_order_id=next_order_id)
            issues = []
            if not self.connected or not complete or not ownership_confirmed:
                self._lock("ledger: snapshot completeness, ownership or connection is unverified", at)
                return False
            for row in executions:
                try:
                    fill_at = row["at"]
                    if isinstance(fill_at, str):
                        fill_at = datetime.fromisoformat(fill_at)
                    self.fill(row["exec_id"], int(row["order_id"]),
                              row.get("qty", row.get("quantity")),
                              Decimal(str(row["price"])), fill_at,
                              correction_of=row.get("correction_of"))
                    if row.get("commission") is not None:
                        self.commission(row["exec_id"], Decimal(str(row["commission"])))
                except (KeyError, ValueError, TypeError) as exc:
                    issues.append(f"ledger: execution snapshot unresolved: {exc}")
            seen = set()
            self.unmanaged_orders = []
            for row in open_orders:
                order_id = row.get("order_id")
                if order_id in seen:
                    issues.append("duplicate order in broker snapshot")
                    continue
                seen.add(order_id)
                order = self.orders.get(order_id)
                if order is None:
                    self.unmanaged_orders.append(_encode(row))
                    issues.append("ledger: broker snapshot contains an unowned order")
                    continue
                try:
                    if (row.get("symbol", order.symbol) != order.symbol or
                            Side(row.get("side", order.side)) != order.side or
                            _quantity(row.get("quantity", order.quantity)) != order.quantity):
                        issues.append("ledger: broker order identity or quantity differs")
                        continue
                    reported_filled = _quantity(row.get("filled", order.reported_filled), zero=True)
                    remaining = _quantity(row.get("remaining", order.quantity - reported_filled), zero=True)
                    if reported_filled + remaining != order.quantity:
                        issues.append("ledger: open order filled and remaining totals differ")
                        continue
                    if reported_filled == order.filled_quantity:
                        # A complete barrier can resolve a corrected cumulative
                        # total; ordinary out-of-order statuses cannot lower it.
                        order.reported_filled = reported_filled
                    self.status(order_id, row.get("status", "Submitted"),
                                reported_filled, remaining, at)
                    if order.state in TERMINAL or order.state == OrderState.UNKNOWN:
                        issues.append("open order snapshot reports a terminal or unknown state")
                    if order.filled_quantity != reported_filled:
                        issues.append("execution ledger differs from broker order cumulative fills")
                    order.reconciled = True
                except (ValueError, TypeError) as exc:
                    issues.append(f"open order snapshot unresolved: {exc}")
            for order in self.orders.values():
                if order.order_id not in seen:
                    corrected_down = sum(max(0, self.executions[root].quantity -
                                               self.executions[current].quantity)
                                         for root, current in self._current.items()
                                         if self.executions[current].order_id == order.order_id)
                    if order.reported_filled > order.filled_quantity + corrected_down:
                        issues.append("terminal order is missing broker executions")
                    else:
                        order.reported_filled = order.filled_quantity
                        order.reported_remaining = 0
                        if order.state not in {OrderState.REJECTED, OrderState.EXPIRED}:
                            order.state = (OrderState.FILLED if order.filled_quantity == order.quantity
                                           else OrderState.CANCELLED)
                        order.reconciled = True
            symbols = self.broker_positions.keys() | self.positions.keys()
            if any(self.broker_positions.get(symbol, 0) !=
                   self.positions.get(symbol, Position(symbol)).quantity for symbol in symbols):
                issues.append("ledger: strategy position differs from broker position")
            if any(order.filled_quantity > order.quantity for order in self.orders.values()):
                issues.append("ledger: broker executions exceed order quantity")
            if any(pos.quantity < 0 for pos in self.positions.values()):
                issues.append("ledger: broker executions produce a short position")
            if next_order_id is not None:
                if type(next_order_id) is not int or next_order_id < 1:
                    issues.append("invalid broker next order ID")
                else:
                    self.next_order_id = max(self.next_order_id, next_order_id,
                                             max(self.orders, default=0) + 1)
            if issues:
                for issue in issues:
                    self._lock(issue, at)
                self._query(at)
                return False
            self.lock_reasons.clear()
            self.reconciling = False
            # A successful barrier proves outcomes; no pre-disconnect request is replayed.
            self.commands.clear()
            self._record("RECONCILED", at)
            return True

    def snapshot(self) -> dict:
        with self._mutex:
            return _encode({"version": 1, "next_order_id": self.next_order_id,
                            "connected": self.connected, "reconciling": self.reconciling,
                            "lock_reasons": sorted(self.lock_reasons),
                            "orders": [asdict(o) for o in self.orders.values()],
                            "executions": [asdict(f) for f in self.executions.values()],
                            "commissions": self.commissions,
                            "broker_positions": self.broker_positions,
                            "unmanaged_orders": self.unmanaged_orders,
                            "position_metadata": {
                                symbol: {"stop_price": position.stop_price,
                                         "entry_time": position.entry_time}
                                for symbol, position in self.positions.items()},
                            "journal": self.journal,
                            "pacing": {"rate": self.pacing.rate, "capacity": self.pacing.capacity,
                                       "reserved_risk_tokens": self.pacing.reserve}})

    @classmethod
    def from_snapshot(cls, snapshot: Mapping) -> "ExecutionBook":
        if snapshot.get("version") != 1:
            raise ExecutionError("unsupported execution snapshot version")
        book = cls(snapshot["next_order_id"], connected=False,
                   request_rate=snapshot["pacing"]["rate"],
                   request_capacity=snapshot["pacing"]["capacity"],
                   reserved_risk_tokens=snapshot["pacing"]["reserved_risk_tokens"])
        for raw in snapshot["orders"]:
            row = dict(raw)
            for name in ("created_at", "submitted_at", "cancel_requested_at", "updated_at",
                         "candidate_expires_at"):
                if row[name] is not None:
                    row[name] = aware(datetime.fromisoformat(row[name]))
            row["limit_price"] = Decimal(row["limit_price"])
            row["filled_notional"] = Decimal(row.get("filled_notional", "0"))
            if row["stop_distance"] is not None:
                row["stop_distance"] = Decimal(row["stop_distance"])
            row["side"], row["state"] = Side(row["side"]), OrderState(row["state"])
            order = Order(**row)
            if order.order_id in book.orders or order.intent_key in book._intent_orders:
                raise ExecutionError("snapshot contains duplicate order or intent IDs")
            book.orders[order.order_id] = order
            book._intent_orders[order.intent_key] = order.order_id
        for raw in sorted(snapshot["executions"], key=lambda f: f["sequence"]):
            row = dict(raw)
            row["price"], row["at"] = Decimal(row["price"]), aware(datetime.fromisoformat(row["at"]))
            fill = Fill(**row)
            if fill.order_id not in book.orders or fill.exec_id in book.executions:
                raise ExecutionError("snapshot contains unresolved or duplicate executions")
            if fill.correction_of and fill.correction_of not in book._roots:
                raise ExecutionError("snapshot correction lineage is missing")
            book.executions[fill.exec_id] = fill
            root = book._roots.get(fill.correction_of, fill.exec_id)
            book._roots[fill.exec_id] = root
            book._current[root] = fill.exec_id
            book._fill_sequence = max(book._fill_sequence, fill.sequence)
        book.commissions = {key: Decimal(value) for key, value in snapshot["commissions"].items()}
        book.journal = [dict(event) for event in snapshot.get("journal", [])]
        book._sequence = max((event["sequence"] for event in book.journal), default=0)
        book.lock_reasons.update(snapshot.get("lock_reasons", []))
        book.broker_positions = dict(snapshot.get("broker_positions", {}))
        book.unmanaged_orders = list(snapshot.get("unmanaged_orders", []))
        book.next_order_id = max(book.next_order_id, max(book.orders, default=0) + 1)
        book._rebuild(None)
        for symbol, metadata in snapshot.get("position_metadata", {}).items():
            position = book.positions.get(symbol)
            if position and position.quantity > 0 and metadata.get("stop_price") is not None:
                persisted_stop = Decimal(metadata["stop_price"])
                finite_decimal(persisted_stop, "persisted stop", positive=True)
                position.stop_price = max(position.stop_price or persisted_stop, persisted_stop)
        for order in book.active_orders():
            order.state = OrderState.UNKNOWN
            order.reconciled = False
        book._lock("restored state requires broker reconciliation")
        return book

    @classmethod
    def from_journal(cls, journal: Iterable[Mapping]) -> "ExecutionBook":
        """Restore memory from a recorded event stream without sending requests."""
        book = cls()
        events = [dict(event) for event in journal]
        if any(b["sequence"] <= a["sequence"] for a, b in zip(events, events[1:])):
            raise ExecutionError("journal sequence must be strictly increasing")
        for event in events:
            kind, payload = event["kind"], event["payload"]
            at = datetime.fromisoformat(event["at"]) if event.get("at") else None
            if kind == "SUBMIT":
                row = dict(payload["order"])
                book.next_order_id = row["order_id"]
                options = {name: row.get(name) for name in (
                    "entry_score", "score_version", "emergency", "replaces",
                    "max_holding_seconds", "exit_policy_version")}
                options["stop_distance"] = (Decimal(row["stop_distance"])
                                            if row.get("stop_distance") else None)
                options["candidate_expires_at"] = (datetime.fromisoformat(row["candidate_expires_at"])
                                                   if row.get("candidate_expires_at") else None)
                book.submit(row["intent_key"], row["symbol"], Side(row["side"]),
                            row["quantity"], Decimal(row["limit_price"]), at, **options)
            elif kind == "CANCEL":
                book.cancel(payload["order_id"], at)
            elif kind == "STATUS":
                book.status(payload["order_id"], payload["ib_status"],
                            payload["filled"], payload["remaining"], at)
            elif kind == "FILL":
                book.fill(payload["exec_id"], payload["order_id"], payload["qty"],
                          Decimal(payload["price"]), at, correction_of=payload["correction_of"])
            elif kind == "COMMISSION":
                book.commission(payload["exec_id"], Decimal(payload["amount"]))
            elif kind == "COMMAND_SENT" and payload["order_id"] is not None:
                order = book.orders[payload["order_id"]]
                book.commands = deque(c for c in book.commands if not (
                    c.order_id == order.order_id and c.kind == payload["kind_sent"]))
                if payload["kind_sent"] == "SUBMIT":
                    order.submitted_at = at
                elif payload["kind_sent"] == "CANCEL":
                    order.cancel_requested_at = at
            elif kind == "REJECT":
                book.reject(payload["order_id"], at, payload["reason"])
            elif kind == "AMBIGUOUS":
                book.ambiguous(payload["order_id"], at, payload["reason"])
            elif kind == "DISCONNECT":
                book.disconnect(at)
            elif kind == "RECONNECT":
                book.reconnect(at)
            elif kind == "LOCK":
                book._lock(payload["reason"], at)
            elif kind == "LOCAL_EXPIRED":
                book.orders[payload["order_id"]].state = OrderState.EXPIRED
                book.orders[payload["order_id"]].reconciled = True
            elif kind == "ENTRY_SNAPSHOT":
                book.set_entry_snapshot(payload["order_id"], payload["score"], payload["version"],
                                        source_at=(datetime.fromisoformat(payload["source_at"])
                                                   if payload.get("source_at") else None), at=at)
            elif kind == "STOP_TIGHTENED":
                book.tighten_stop(payload["symbol"], Decimal(payload["stop_price"]), at)
            elif kind == "LOCAL_ABORT":
                book.orders[payload["order_id"]].state = OrderState.CANCELLED
                book.orders[payload["order_id"]].reconciled = True
                book.commands = deque(c for c in book.commands
                                      if c.order_id != payload["order_id"])
            elif kind == "RISK_PRIORITY":
                book.prioritize_risk(payload["order_id"], at)
            elif kind == "RECONCILE":
                book.reconcile(payload["positions"], payload["open_orders"],
                               payload["executions"], at, complete=payload["complete"],
                               ownership_confirmed=payload["ownership_confirmed"],
                               next_order_id=payload["next_order_id"])
        book.journal = events
        book._sequence = max((event["sequence"] for event in events), default=0)
        book.connected = False
        book.reconciling = True
        book.commands.clear()
        for order in book.active_orders():
            order.state = OrderState.UNKNOWN
            order.reconciled = False
        book._lock("journal replay requires broker reconciliation")
        return book
