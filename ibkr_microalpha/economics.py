"""Research economics in JPY per complete intent, with explicit calibration gates.

Fees are supplied by the caller from the account's verified schedule.  No
commission plan or execution probability is assumed to be suitable for trading.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from copy import deepcopy
import hashlib
import json
from math import isfinite, sqrt
from typing import Iterable, Mapping

ZERO = Decimal("0")
BPS = Decimal("10000")


def _decimal(value: Decimal, name: str, *, nonnegative: bool = True) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if nonnegative and value < ZERO:
        raise ValueError(f"{name} must be nonnegative")


def _time(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone aware")


def policy_fingerprint(document: Mapping) -> str:
    """Identify the entire frozen executable configuration, excluding its citations.

    Provenance is excluded to avoid a self-referential training artifact hash.
    Fee schedule, execution, signal, risk and feature settings remain covered.
    """
    payload = {key: value for key, value in document.items() if key != 'provenance'}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _sha256(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def calibration_evidence_error(*, label_source: str, provenance: Mapping | None,
                               sample_days: int, sample_count: int, min_independent_days: int = 2,
                               allow_artificial: bool = False, policy_hash: str | None = None,
                               fee_version: str | None = None, as_of: datetime | None = None) -> str | None:
    """Fail-closed deployment contract; a sample count is never a day count."""
    if type(min_independent_days) is not int or min_independent_days < 2:
        raise ValueError('min_independent_days must be at least two')
    if type(allow_artificial) is not bool:
        raise ValueError('allow_artificial must be a boolean')
    if sample_days < min_independent_days or sample_count < sample_days:
        return 'insufficient independent calibration days'
    if not isinstance(provenance, Mapping):
        return 'calibration provenance unavailable'
    if label_source == 'ARTIFICIAL':
        if not allow_artificial or provenance.get('source') != 'ARTIFICIAL_FIXTURE':
            return 'artificial calibration is restricted to explicit demo fixtures'
        return None
    if label_source != 'VERIFIED_REPLAY':
        return 'full-policy replay labels required; quote baselines are not deployable'
    if any(provenance.get(name) is not True for name in ('complete_policy', 'includes_partial', 'fees_final')):
        return 'complete policy, partial branches and final fees must be verified'
    for name in ('policy_hash', 'labels_hash'):
        if not _sha256(provenance.get(name)):
            return f'calibration {name} unavailable'
    for name in ('input_hashes', 'code_hashes'):
        values = provenance.get(name)
        if not isinstance(values, (list, tuple)) or not values or any(not _sha256(v) for v in values):
            return f'calibration {name} unavailable'
    if not isinstance(provenance.get('fee_version'), str) or not provenance['fee_version']:
        return 'calibration fee version unavailable'
    if policy_hash is not None and provenance['policy_hash'] != policy_hash:
        return 'full policy fingerprint mismatch'
    if fee_version is not None and provenance['fee_version'] != fee_version:
        return 'calibration fee schedule mismatch'
    try:
        trained_until = datetime.fromisoformat(provenance['trained_until'])
        _time(trained_until)
        days = provenance['independent_days']
        if not isinstance(days, (list, tuple)) or any(not isinstance(day, str) for day in days):
            return 'independent day identities unavailable'
        parsed = [date.fromisoformat(day) for day in days]
        if len(parsed) != sample_days or len(set(parsed)) != sample_days:
            return 'independent day identities mismatch'
        # A trading day can finish on another UTC date; compare in its recorded timezone.
        if any(day > trained_until.date() for day in parsed):
            return 'training cutoff precedes independent sample days'
        if as_of is not None:
            _time(as_of)
            if trained_until >= as_of:
                return 'training cutoff must precede calibration availability'
    except (ValueError, KeyError, TypeError):
        return 'invalid training cutoff or independent day evidence'
    return None


@dataclass(frozen=True)
class CommissionSchedule:
    rate: Decimal
    minimum_per_order: Decimal
    additional_rate: Decimal
    version: str

    def __post_init__(self) -> None:
        for name in ("rate", "minimum_per_order", "additional_rate"):
            _decimal(getattr(self, name), name)
        if not self.version or self.rate > 1 or self.additional_rate > 1:
            raise ValueError("a verified, bounded fee version is required")

    def commission(self, filled_notional: Decimal) -> Decimal:
        """Minimum is applied once per billable order, not per fill callback."""
        _decimal(filled_notional, "filled_notional")
        if filled_notional == ZERO:
            return ZERO
        return max(self.minimum_per_order, self.rate * filled_notional) + (
            self.additional_rate * filled_notional
        )

    def orders_commission(self, notionals: Iterable[Decimal]) -> Decimal:
        return sum((self.commission(x) for x in notionals), ZERO)


@dataclass(frozen=True)
class Prediction:
    """A calibrated conditional mean and mean confidence bound, never a quantile.

    Policy identity includes entry, exit and holding-time rules; quantity and
    model version must match every submission and replacement.
    """
    policy_id: str
    version: str
    quantity: int
    sample_count: int
    mean_net_amount: Decimal
    lower_net_amount: Decimal
    reliable: bool
    calibrated: bool
    max_holding_seconds: int
    sample_days: int = 0
    label_source: str = 'UNVERIFIED'
    provenance: dict | None = None

    def __post_init__(self) -> None:
        _decimal(self.mean_net_amount, "mean_net_amount", nonnegative=False)
        _decimal(self.lower_net_amount, "lower_net_amount", nonnegative=False)
        if type(self.reliable) is not bool or type(self.calibrated) is not bool:
            raise ValueError("calibrated and reliable must be explicit booleans")
        if not self.policy_id or not self.version:
            raise ValueError("policy and calibration version are required")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("prediction quantity must be positive")
        if type(self.sample_count) is not int or self.sample_count < 0:
            raise ValueError("sample_count must be a nonnegative integer")
        if type(self.max_holding_seconds) is not int or self.max_holding_seconds <= 0:
            raise ValueError("max_holding_seconds must be positive")
        if self.lower_net_amount > self.mean_net_amount:
            raise ValueError("lower confidence bound cannot exceed the mean")
        if type(self.sample_days) is not int or not 0 <= self.sample_days <= self.sample_count:
            raise ValueError('sample_days must be an independent day count bounded by samples')
        if not isinstance(self.label_source, str):
            raise ValueError('label_source must be explicit')
        object.__setattr__(self, 'provenance', deepcopy(self.provenance))


@dataclass(frozen=True)
class EconomicGate:
    allowed: bool
    reason: str


def prediction_gate(
    prediction: Prediction | None, *, policy_id: str, version: str,
    quantity: int, min_samples: int, safety_margin: Decimal,
    max_holding_seconds: int | None = None,
    min_independent_days: int = 2, allow_artificial: bool = False,
    policy_hash: str | None = None, fee_version: str | None = None,
    as_of: datetime | None = None,
) -> EconomicGate:
    _decimal(safety_margin, "safety_margin")
    if type(min_samples) is not int or min_samples <= 0:
        raise ValueError("min_samples must be positive")
    if prediction is None:
        return EconomicGate(False, "prediction unavailable")
    if not prediction.reliable or not prediction.calibrated:
        return EconomicGate(False, "uncalibrated or unreliable prediction")
    if (prediction.policy_id, prediction.version, prediction.quantity) != (
        policy_id, version, quantity
    ):
        return EconomicGate(False, "policy, version or quantity mismatch")
    if max_holding_seconds is not None and prediction.max_holding_seconds != max_holding_seconds:
        return EconomicGate(False, "holding policy mismatch")
    if prediction.sample_count < min_samples:
        return EconomicGate(False, "insufficient independent calibration samples")
    evidence_error = calibration_evidence_error(
        label_source=prediction.label_source, provenance=prediction.provenance,
        sample_days=prediction.sample_days, sample_count=prediction.sample_count,
        min_independent_days=min_independent_days, allow_artificial=allow_artificial,
        policy_hash=policy_hash, fee_version=fee_version, as_of=as_of)
    if evidence_error:
        return EconomicGate(False, evidence_error)
    if prediction.lower_net_amount <= safety_margin:
        return EconomicGate(False, "net mean confidence bound below safety margin")
    return EconomicGate(True, "net mean confidence bound passed")


def adjust_prediction_for_price(prediction: Prediction, *, reference_price: Decimal,
                                limit_price: Decimal, commissions: CommissionSchedule) -> Prediction:
    """Charge the worst executable entry price against a calibrated value.

    The calibration is stated at ``reference_price``; paying ``limit_price``
    instead costs the price difference and any change in the entry commission.
    The expected exit is not assumed to move with the entry price.
    """
    for name, value in (("reference_price", reference_price), ("limit_price", limit_price)):
        _decimal(value, name)
        if value <= ZERO:
            raise ValueError(f"{name} must be positive")
    quantity = Decimal(prediction.quantity)
    drift = quantity * (limit_price - reference_price)
    fee_change = (commissions.commission(limit_price * quantity)
                  - commissions.commission(reference_price * quantity))
    return replace(prediction,
                   mean_net_amount=prediction.mean_net_amount - drift - fee_change,
                   lower_net_amount=prediction.lower_net_amount - drift - fee_change)


@dataclass(frozen=True)
class CalibrationRow:
    """One frozen research result: complete-policy net JPY per intent for a score bucket.

    Values are stated at the candidate's reference ask and already include all
    child-order fees, unfilled and partially filled branches (section 9/11).
    """
    policy_id: str
    version: str
    holding_seconds: int
    quantity: int
    score_low: float
    score_high: float | None
    sample_days: int
    sample_count: int
    mean_net_amount: Decimal
    lower_net_amount: Decimal
    max_chase_ticks: int
    label_source: str = 'UNVERIFIED'
    provenance: dict | None = None

    def __post_init__(self) -> None:
        if not self.policy_id or not self.version:
            raise ValueError("calibration policy and version are required")
        for name in ("holding_seconds", "quantity", "sample_days", "sample_count", "max_chase_ticks"):
            value = getattr(self, name)
            if type(value) is not int or value < 0 or (name in ("holding_seconds", "quantity") and value == 0):
                raise ValueError(f"{name} must be a nonnegative integer")
        if isinstance(self.score_low, bool) or not isinstance(self.score_low, (int, float)) or not isfinite(self.score_low):
            raise ValueError("score_low must be finite")
        if self.score_high is not None and (isinstance(self.score_high, bool) or not isinstance(self.score_high, (int, float))
                                            or not isfinite(self.score_high) or self.score_high <= self.score_low):
            raise ValueError("score_high must exceed score_low")
        _decimal(self.mean_net_amount, "mean_net_amount", nonnegative=False)
        _decimal(self.lower_net_amount, "lower_net_amount", nonnegative=False)
        if self.lower_net_amount > self.mean_net_amount:
            raise ValueError("lower confidence bound cannot exceed the mean")
        if self.sample_days > self.sample_count or (self.sample_count > 0 and self.sample_days == 0):
            raise ValueError('calibration samples require nonzero, bounded independent days')
        if not isinstance(self.label_source, str):
            raise ValueError('label_source must be explicit')
        object.__setattr__(self, 'provenance', deepcopy(self.provenance))

    def contains(self, score: float) -> bool:
        return self.score_low <= score and (self.score_high is None or score < self.score_high)

    def prediction(self) -> Prediction:
        verified = calibration_evidence_error(
            label_source=self.label_source, provenance=self.provenance,
            sample_days=self.sample_days, sample_count=self.sample_count,
            allow_artificial=self.label_source == 'ARTIFICIAL') is None
        if self.label_source == 'VERIFIED_REPLAY':
            verified = verified and self.provenance.get('max_chase_ticks') == self.max_chase_ticks
        return Prediction(self.policy_id, self.version, self.quantity, self.sample_count,
                          self.mean_net_amount, self.lower_net_amount, verified, verified, self.holding_seconds,
                          self.sample_days, self.label_source, self.provenance)


class CalibrationTable:
    """Day-start frozen calibration; replaces per-symbol online forecasts."""

    def __init__(self, rows: Iterable[CalibrationRow], *, known_at: datetime, version: str):
        _time(known_at)
        if not version:
            raise ValueError("calibration table version is required")
        self.known_at, self.version = known_at, version
        self.rows = tuple(rows)
        if not self.rows or any(not isinstance(row, CalibrationRow) for row in self.rows):
            raise ValueError("calibration table needs CalibrationRow entries")
        self._rows: dict[tuple, list[CalibrationRow]] = {}
        for row in self.rows:
            key = (row.policy_id, row.version, row.holding_seconds, row.quantity)
            bucket = self._rows.setdefault(key, [])
            if any(not (row.score_high is not None and row.score_high <= other.score_low
                        or other.score_high is not None and other.score_high <= row.score_low)
                   for other in bucket):
                raise ValueError("calibration score buckets overlap")
            bucket.append(row)

    def validate_for_profile(self, profile: str, *, min_independent_days: int = 2,
                             policy_hash: str | None = None, fee_version: str | None = None,
                             as_of: datetime | None = None) -> None:
        if profile not in ('demo', 'research', 'shadow'):
            raise ValueError('unknown calibration profile')
        for row in self.rows:
            if (row.label_source == 'VERIFIED_REPLAY' and (not isinstance(row.provenance, Mapping)
                    or type(row.provenance.get('max_chase_ticks')) is not int
                    or row.provenance['max_chase_ticks'] != row.max_chase_ticks)):
                raise ValueError('calibration chase cap differs from verified replay policy')
            error = calibration_evidence_error(
                label_source=row.label_source, provenance=row.provenance,
                sample_days=row.sample_days, sample_count=row.sample_count,
                min_independent_days=min_independent_days, allow_artificial=profile == 'demo',
                policy_hash=policy_hash, fee_version=fee_version, as_of=as_of or self.known_at)
            if error:
                raise ValueError(error)

    def quantities(self, policy_id: str, version: str, holding_seconds: int) -> tuple[int, ...]:
        return tuple(sorted({key[3] for key in self._rows
                             if key[:3] == (policy_id, version, holding_seconds)}))

    def lookup(self, *, policy_id: str, version: str, holding_seconds: int, quantity: int,
               score: float, at: datetime) -> CalibrationRow | None:
        _time(at)
        if at < self.known_at:
            return None
        for row in self._rows.get((policy_id, version, holding_seconds, quantity), ()):
            if row.contains(score):
                return row
        return None


def net_bps(net_amount: Decimal, frozen_target_notional: Decimal) -> Decimal:
    _decimal(net_amount, "net_amount", nonnegative=False)
    _decimal(frozen_target_notional, "frozen_target_notional")
    if frozen_target_notional <= ZERO:
        raise ValueError("intent reference notional must remain positive when unfilled")
    return BPS * net_amount / frozen_target_notional


def reference_sigma_bps(h_seconds: float, daily_sigma_bps: float,
                        continuous_day_seconds: float) -> float:
    """Illustrative square-root scaling diagnostic; h is explicitly in seconds."""
    for name, value in (("h_seconds", h_seconds), ("daily_sigma_bps", daily_sigma_bps),
                        ("continuous_day_seconds", continuous_day_seconds)):
        if not isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    return daily_sigma_bps * sqrt(h_seconds / continuous_day_seconds)


@dataclass(frozen=True)
class CostDiagnostic:
    mean_gross_bps: Decimal
    mean_net_bps: Decimal
    cost_bps: Decimal
    sigma_bps: Decimal
    k: Decimal | None


def cost_diagnostic(mean_gross_bps: Decimal, cost_bps: Decimal,
                    sigma_bps: Decimal, *, enough_samples: bool,
                    sigma_epsilon: Decimal) -> CostDiagnostic:
    _decimal(mean_gross_bps, "mean_gross_bps", nonnegative=False)
    for name, value in (("cost_bps", cost_bps), ("sigma_bps", sigma_bps),
                        ("sigma_epsilon", sigma_epsilon)):
        _decimal(value, name)
    if sigma_epsilon <= ZERO:
        raise ValueError("sigma_epsilon must be positive")
    k = cost_bps / sigma_bps if enough_samples and sigma_bps > sigma_epsilon else None
    return CostDiagnostic(mean_gross_bps, mean_gross_bps - cost_bps, cost_bps, sigma_bps, k)


@dataclass(frozen=True)
class ChannelBudget:
    ttl_seconds: float
    confirm_seconds: float
    submit_p99_seconds: float
    cancel_p99_seconds: float
    passive_wait_seconds: tuple[float, ...]
    max_requotes: int
    buffer_seconds: float

    def __post_init__(self) -> None:
        for name in ("ttl_seconds", "confirm_seconds", "submit_p99_seconds",
                     "cancel_p99_seconds", "buffer_seconds"):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.ttl_seconds <= 0 or self.submit_p99_seconds <= 0 or self.cancel_p99_seconds <= 0:
            raise ValueError("TTL and measured latency bounds must be positive")
        if type(self.max_requotes) is not int or self.max_requotes < 0:
            raise ValueError("max_requotes must be a nonnegative integer")
        if any(not isfinite(x) or x < 0 for x in self.passive_wait_seconds):
            raise ValueError("wait budgets must be finite and nonnegative")

    @property
    def required_seconds(self) -> float:
        return (self.confirm_seconds + self.submit_p99_seconds
                + sum(self.passive_wait_seconds)
                + self.max_requotes * (self.cancel_p99_seconds + self.submit_p99_seconds)
                + self.buffer_seconds)

    @property
    def feasible(self) -> bool:
        return self.required_seconds <= self.ttl_seconds

    def can_requote(self, *, remaining_seconds: float, requotes_used: int,
                    next_wait_seconds: float) -> bool:
        for value in (remaining_seconds, next_wait_seconds):
            if not isfinite(value) or value < 0:
                return False
        if type(requotes_used) is not int or requotes_used < 0:
            return False
        required = (self.cancel_p99_seconds + self.submit_p99_seconds
                    + next_wait_seconds + self.buffer_seconds)
        return (self.feasible and requotes_used < self.max_requotes
                and remaining_seconds >= required)


@dataclass(frozen=True)
class PathFill:
    execution_id: str
    order_id: str
    side: str
    quantity: int
    price: Decimal

    def __post_init__(self) -> None:
        if not self.execution_id or not self.order_id or self.side not in ("BUY", "SELL"):
            raise ValueError("stable fill/order IDs and BUY/SELL are required")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("fill quantity must be positive")
        _decimal(self.price, "price")
        if self.price <= ZERO:
            raise ValueError("fill price must be positive")


@dataclass(frozen=True)
class IntentPath:
    candidate_id: str
    policy_id: str
    quantity: int
    evaluation_at: datetime
    fills: tuple[PathFill, ...]
    applicable_fees: Decimal
    controlled_residual_quantity: int
    residual_bid: Decimal | None
    residual_markdown_per_share: Decimal
    remaining_exit_cost: Decimal | None
    state_reconciled: bool
    residual_bid_at: datetime | None = None
    max_residual_quote_age_seconds: float | None = None

    def __post_init__(self) -> None:
        _time(self.evaluation_at)
        if not self.candidate_id or not self.policy_id or type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("candidate, complete policy and target quantity are required")
        if type(self.controlled_residual_quantity) is not int or self.controlled_residual_quantity < 0:
            raise ValueError("controlled residual must be nonnegative")
        _decimal(self.applicable_fees, "applicable_fees")
        _decimal(self.residual_markdown_per_share, "residual_markdown_per_share")
        if self.residual_bid is not None:
            _decimal(self.residual_bid, "residual_bid")
        if self.remaining_exit_cost is not None:
            _decimal(self.remaining_exit_cost, "remaining_exit_cost")
        if self.residual_bid_at is not None:
            _time(self.residual_bid_at)
        if self.max_residual_quote_age_seconds is not None:
            if (not isfinite(self.max_residual_quote_age_seconds)
                    or self.max_residual_quote_age_seconds <= 0):
                raise ValueError("residual quote age bound must be finite and positive")


@dataclass(frozen=True)
class IntentValue:
    estimable: bool
    net_amount: Decimal | None
    realized_price_pnl: Decimal | None
    unrealized_price_pnl: Decimal | None
    transaction_fees: Decimal
    remaining_exit_cost: Decimal | None
    residual_terminal_value: Decimal | None
    reason: str


def intent_path_value(path: IntentPath) -> IntentValue:
    """Aggregate all child orders and include unfilled paths and residual risk.

    Causal moving-average entry cost divides realized/unrealized price PnL. Fees are shown
    separately; W is never presented as realized cash profit before flattening.
    """
    def unavailable(reason: str) -> IntentValue:
        return IntentValue(False, None, None, None, path.applicable_fees,
                           path.remaining_exit_cost, None, reason)

    if not path.state_reconciled:
        return unavailable("unreconciled path")
    seen: dict[str, PathFill] = {}
    for fill in path.fills:
        if fill.execution_id in seen and seen[fill.execution_id] != fill:
            return unavailable("unresolved fill correction")
        seen[fill.execution_id] = fill
    buys = [x for x in seen.values() if x.side == "BUY"]
    sells = [x for x in seen.values() if x.side == "SELL"]
    bought = sum(x.quantity for x in buys)
    sold = sum(x.quantity for x in sells)
    if sold > bought or bought > path.quantity or bought - sold != path.controlled_residual_quantity:
        return unavailable("quantity mismatch or uncontrolled short position")
    buy_amount = sum((x.price * x.quantity for x in buys), ZERO)
    sell_amount = sum((x.price * x.quantity for x in sells), ZERO)
    residual = bought - sold
    if residual:
        if (path.residual_bid is None or path.residual_bid <= ZERO
                or path.residual_markdown_per_share >= path.residual_bid
                or path.remaining_exit_cost is None
                or path.residual_bid_at is None
                or path.max_residual_quote_age_seconds is None
                or not 0 <= (path.evaluation_at - path.residual_bid_at).total_seconds()
                    <= path.max_residual_quote_age_seconds):
            return unavailable("residual value or remaining exit cost unavailable")
        terminal_value = Decimal(residual) * (path.residual_bid - path.residual_markdown_per_share)
        exit_cost = path.remaining_exit_cost
    else:
        terminal_value = ZERO
        exit_cost = ZERO
    inventory, inventory_cost, realized = 0, ZERO, ZERO
    for fill in seen.values():
        if fill.side == "BUY":
            inventory += fill.quantity
            inventory_cost += fill.price * fill.quantity
        else:
            if fill.quantity > inventory:
                return unavailable("path temporarily sells more than its controlled long position")
            allocated_cost = inventory_cost * Decimal(fill.quantity) / Decimal(inventory)
            realized += fill.price * fill.quantity - allocated_cost
            inventory -= fill.quantity
            inventory_cost -= allocated_cost
    unrealized = terminal_value - inventory_cost
    net = sell_amount - buy_amount + terminal_value - path.applicable_fees - exit_cost
    return IntentValue(True, net, realized, unrealized, path.applicable_fees,
                       exit_cost, terminal_value, "valued at common cutoff")


@dataclass(frozen=True)
class WeightedPath:
    probability: Decimal
    path: IntentPath

    def __post_init__(self) -> None:
        _decimal(self.probability, "probability")
        if self.probability > 1:
            raise ValueError("path probability exceeds one")


def expected_intent_value(paths: Iterable[WeightedPath]) -> Decimal | None:
    """Probabilities must cover all outcomes, including none/partial/full fills."""
    branches = tuple(paths)
    if not branches or sum((x.probability for x in branches), ZERO) != Decimal("1"):
        raise ValueError("complete branch probabilities must sum to one")
    key = (branches[0].path.candidate_id, branches[0].path.policy_id,
           branches[0].path.quantity, branches[0].path.evaluation_at)
    total = ZERO
    for branch in branches:
        if (branch.path.candidate_id, branch.path.policy_id,
                branch.path.quantity, branch.path.evaluation_at) != key:
            raise ValueError("branch candidate, policy, quantity and cutoff must agree")
        value = intent_path_value(branch.path)
        if not value.estimable:
            return None  # Never discard a failed outcome or assign zero to unknown risk.
        assert value.net_amount is not None
        total += branch.probability * value.net_amount
    return total


def late_midpoint_net_amount(*, alpha_late_per_share: Decimal, quantity: int,
                            remaining_loss_and_fees: Decimal) -> Decimal:
    """alpha_late starts at the late midpoint; arrival drift is only attribution."""
    _decimal(alpha_late_per_share, "alpha_late_per_share", nonnegative=False)
    _decimal(remaining_loss_and_fees, "remaining_loss_and_fees")
    if type(quantity) is not int or quantity <= 0:
        raise ValueError("quantity must be positive")
    return Decimal(quantity) * alpha_late_per_share - remaining_loss_and_fees


@dataclass(frozen=True)
class PairedAdvantage:
    candidate_policy_id: str
    baseline_policy_id: str
    quantity: int
    version: str
    sample_count: int
    mean_delta_amount: Decimal
    lower_delta_amount: Decimal
    reliable: bool
    calibrated: bool

    def __post_init__(self) -> None:
        _decimal(self.mean_delta_amount, "mean_delta_amount", nonnegative=False)
        _decimal(self.lower_delta_amount, "lower_delta_amount", nonnegative=False)
        if type(self.reliable) is not bool or type(self.calibrated) is not bool:
            raise ValueError("paired calibrated and reliable must be explicit booleans")
        if (not self.candidate_policy_id or not self.baseline_policy_id or not self.version
                or type(self.quantity) is not int or self.quantity <= 0
                or type(self.sample_count) is not int or self.sample_count < 0
                or self.lower_delta_amount > self.mean_delta_amount):
            raise ValueError("invalid paired calibration")


@dataclass(frozen=True)
class PolicyChoice:
    policy_id: str | None
    reason: str


def choose_execution_policy(*, aggressive: Prediction | None,
                            passive: Prediction | None,
                            paired: PairedAdvantage | None,
                            aggressive_policy_id: str, passive_policy_id: str,
                            version: str, quantity: int, min_samples: int,
                            net_safety_margin: Decimal,
                            execution_safety_margin: Decimal,
                            max_holding_seconds: int) -> PolicyChoice:
    _decimal(execution_safety_margin, "execution_safety_margin")
    args = dict(version=version, quantity=quantity, min_samples=min_samples,
                safety_margin=net_safety_margin, max_holding_seconds=max_holding_seconds)
    aggr_ok = prediction_gate(aggressive, policy_id=aggressive_policy_id, **args).allowed
    pass_ok = prediction_gate(passive, policy_id=passive_policy_id, **args).allowed
    if not aggr_ok and not pass_ok:
        return PolicyChoice(None, "no calibrated policy passes its net gate")
    if pass_ok and not aggr_ok:
        return PolicyChoice(passive_policy_id, "only passive policy passes")
    if aggr_ok and not pass_ok:
        return PolicyChoice(aggressive_policy_id, "only aggressive policy passes")
    comparable = (paired is not None and paired.reliable and paired.calibrated
                  and paired.sample_count >= min_samples
                  and (paired.candidate_policy_id, paired.baseline_policy_id,
                       paired.quantity, paired.version)
                  == (passive_policy_id, aggressive_policy_id, quantity, version))
    if comparable and paired.lower_delta_amount > execution_safety_margin:
        return PolicyChoice(passive_policy_id, "paired mean bound passes execution margin")
    return PolicyChoice(aggressive_policy_id, "retain calibrated aggressive baseline")


def common_valid_sample(variants: Mapping[str, Mapping[str, bool]]) -> tuple[str, ...]:
    """Frozen-option attribution uses the intersection, complete policies stay separate."""
    if not variants:
        return ()
    valid_sets = [{key for key, valid in samples.items() if valid}
                  for samples in variants.values()]
    return tuple(sorted(set.intersection(*valid_sets)))
