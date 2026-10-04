"""Separate quote-only baselines from verified full-policy replay labels.

A quote is not an execution acknowledgement. The restricted baseline assumes
immediate full fills at visible prices; environment exits, latency, cancel/
replace and partial fills require the actual coordinator. Formal labels rebuild
a Replay from verified inputs and use terminal intents with final reported fees.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import isfinite
import json
from typing import Iterable, Mapping, Sequence

from ..domain import Quote, aware
from ..economics import CommissionSchedule, policy_fingerprint
from ..market import JST, QuoteQuality, TickTable, validate_quote

BPS = Decimal(10000)
_VERIFIED = object()


@dataclass(frozen=True)
class LabelPolicy:
    """Parameters of QUOTE_BASELINE_V1 only, never a deployed policy identity."""
    holding_seconds: int
    entry_limit_ticks: int
    entry_order_ttl_seconds: float
    min_stop_ticks: int
    stop_bps: Decimal
    exit_slippage_ticks: int
    tick_category: str = 'TOPIX500'

    def __post_init__(self):
        for name in ('holding_seconds', 'min_stop_ticks'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be a positive integer')
        for name in ('entry_limit_ticks', 'exit_slippage_ticks'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f'{name} must be a nonnegative integer')
        if (isinstance(self.entry_order_ttl_seconds, bool)
                or not isinstance(self.entry_order_ttl_seconds, (int, float))
                or not isfinite(self.entry_order_ttl_seconds) or self.entry_order_ttl_seconds <= 0):
            raise ValueError('entry order TTL must be finite and positive')
        if not isinstance(self.stop_bps, Decimal) or not self.stop_bps.is_finite() or self.stop_bps < 0:
            raise ValueError('stop_bps must be a nonnegative Decimal')


@dataclass(frozen=True)
class QuoteBaselineLabel:
    net_amount: Decimal | None
    reason: str
    label_source: str = field(default='QUOTE_BASELINE', init=False)
    policy_id: str = field(default='QUOTE_BASELINE_V1', init=False)


def quote_baseline_label(quotes: Sequence[Quote], start: int, quantity: int, policy: LabelPolicy,
                         ticks: TickTable, commissions: CommissionSchedule, deadline: datetime,
                         *, quality: QuoteQuality | None = None) -> QuoteBaselineLabel:
    """Full-fill zero-latency baseline. Ambiguous paths are explicitly unknown.

    Insufficient visible size can cause a partial entry/exit, so those paths are
    unknown rather than zero or repeated quote fills. Slippage is a conservative
    price haircut, not another child order.
    """
    aware(deadline)
    if type(quantity) is not int or quantity <= 0:
        raise ValueError('quantity must be a positive integer')
    if type(start) is not int or not 0 <= start < len(quotes):
        raise ValueError('start must identify a decision quote')
    decision = quotes[start]
    relevant = quotes[start:]
    if deadline <= decision.at:
        return QuoteBaselineLabel(None, 'decision is past its exit deadline')
    if any(q.symbol != decision.symbol for q in relevant):
        return QuoteBaselineLabel(None, 'mixed quote symbols')
    if any(left.at >= right.at for left, right in zip(relevant, relevant[1:])):
        return QuoteBaselineLabel(None, 'quotes require strictly increasing receive times')
    if any(not validate_quote(q, q.at, ticks, policy.tick_category, quality).allowed for q in relevant):
        return QuoteBaselineLabel(None, 'invalid, delayed or unsynchronized quote data')
    cap = ticks.move_ticks(decision.ask, policy.entry_limit_ticks, decision.at, policy.tick_category)
    ttl_end = decision.at + timedelta(seconds=policy.entry_order_ttl_seconds)
    entry_index = None
    for index in range(start + 1, len(quotes)):
        q = quotes[index]
        if q.at > ttl_end:
            break
        if q.ask <= cap:
            if q.ask_size < quantity:
                return QuoteBaselineLabel(None, 'partial entry requires execution replay')
            entry_index = index
            break
    if entry_index is None:
        if quotes[-1].at < ttl_end:
            return QuoteBaselineLabel(None, 'entry TTL is not fully covered')
        return QuoteBaselineLabel(Decimal(0), 'covered unfilled baseline branch')
    entry = quotes[entry_index]
    buy = entry.ask * quantity
    tick = ticks.tick_size(entry.ask, entry.at, policy.tick_category)
    stop = entry.ask - max(tick * policy.min_stop_ticks, policy.stop_bps * entry.ask / BPS)
    end = min(entry.at + timedelta(seconds=policy.holding_seconds), deadline)
    for q in quotes[entry_index + 1:]:
        if q.bid > stop and q.at < end:
            continue
        if q.bid_size < quantity:
            return QuoteBaselineLabel(None, 'partial exit requires execution replay')
        sell_price = ticks.move_ticks(q.bid, -policy.exit_slippage_ticks, q.at, policy.tick_category)
        proceeds = sell_price * quantity
        net = proceeds - buy - commissions.commission(buy) - commissions.commission(proceeds)
        return QuoteBaselineLabel(net, 'full-fill quote baseline; deployment use prohibited')
    return QuoteBaselineLabel(None, 'exit coverage incomplete')


def label_intent(quotes: Sequence[Quote], start: int, quantity: int, policy: LabelPolicy,
                 ticks: TickTable, commissions: CommissionSchedule, deadline: datetime) -> Decimal | None:
    """Compatibility wrapper for the nondeployable quote baseline only."""
    return quote_baseline_label(quotes, start, quantity, policy, ticks, commissions, deadline).net_amount


@dataclass(frozen=True)
class ReplayIntentLabel:
    """Factory-issued complete intent label; uncertainty must not become a number."""
    intent_id: str
    day: date
    score: float
    net_amount: Decimal
    policy_id: str
    version: str
    holding_seconds: int
    quantity: int
    fee_version: str
    policy_hash: str
    code_hash: str
    input_hash: str
    completed_at: datetime
    bought: int
    sold: int
    actual_fees: Decimal
    label_source: str
    entry_cap_mode: str
    max_chase_ticks: int | None
    reference_entry_price: Decimal
    entry_max_price: Decimal
    _verification_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._verification_token is not _VERIFIED:
            raise ValueError('ReplayIntentLabel must be issued by replay_intent_labels')
        aware(self.completed_at)
        if (not self.net_amount.is_finite() or not self.actual_fees.is_finite()
                or not isfinite(self.score) or self.bought != self.sold):
            raise ValueError('complete, finite replay outcome required')


def replay_intent_labels(runner, *, verified_manifest: Mapping,
                         source_events: Iterable[Mapping | str] | None = None) -> tuple[ReplayIntentLabel, ...]:
    """Rebuild verified inputs and label terminal, reconciled intents.

    verified_manifest is the persisted Replay manifest. For a raw sink, supply
    original event records in source_events. One missing final fee, live order,
    residual position or unattributed intent rejects the whole artifact. Labels
    never use an externally mutated runner ledger. Demo output is ARTIFICIAL.
    """
    from ..config import build_engine
    from ..replay import Replay
    if not isinstance(runner, Replay) or runner.failed:
        raise ValueError('a successful Replay is required')
    runner.finish()
    manifest = runner.manifest()
    if dict(verified_manifest) != manifest or manifest['events'] <= 0:
        raise ValueError('verified replay manifest mismatch or empty input')
    events = runner.raw_lines if source_events is None else source_events
    rebuilt = Replay(build_engine(runner.engine.frozen_config))
    caps = {}

    def capture_entry_contracts():
        e = rebuilt.engine
        for order in e.book.orders.values():
            if order.side.value != 'BUY' or order.order_id in caps:
                continue
            candidate = e.alpha.current(order.symbol)
            if candidate is None or candidate.candidate_id != order.intent_key:
                raise ValueError('original entry candidate missing for cap verification')
            cap = e.entries._quantity_caps.get((candidate.candidate_id, order.quantity))
            if cap is None:
                raise ValueError('original immutable entry cap missing')
            if e.config.economics_source == 'calibration':
                row = e.calibration.lookup(policy_id=e.config.policy_id, version=e.config.model_version,
                    holding_seconds=e.config.holding_seconds, quantity=order.quantity,
                    score=candidate.entry_score, at=order.created_at)
                if row is None:
                    raise ValueError('original calibration cap contract missing')
                expected = e.ticks.move_ticks(candidate.reference_ask, row.max_chase_ticks,
                    order.created_at, e.instruments[order.symbol].tick_category)
                if expected != cap:
                    raise ValueError('original entry cap differs from calibrated chase ticks')
                caps[order.order_id] = ('RELATIVE_TICKS', row.max_chase_ticks, candidate.reference_ask, cap)
            else:
                caps[order.order_id] = ('ABSOLUTE_FORECAST', None, candidate.reference_ask, cap)

    for event in events:
        rebuilt.dispatch(json.loads(event) if isinstance(event, str) else dict(event))
        capture_entry_contracts()
    rebuilt.finish()
    capture_entry_contracts()
    if rebuilt.manifest() != manifest:
        raise ValueError('raw event input does not match verified replay manifest')
    e, book = rebuilt.engine, rebuilt.engine.book
    if not book.reconciled or book.active_orders() or any(p.quantity for p in book.positions.values()):
        raise ValueError('label replay requires reconciled terminal orders and zero residual inventory')
    rows = e.intents.outcomes(book, e.commissions, at=rebuilt.last_key[0])
    submissions = {record['payload']['order']['order_id']: record['payload']['order']
                   for record in book.journal if record['kind'] == 'SUBMIT'}
    labels = []
    source = 'ARTIFICIAL' if e.frozen_config['profile'] == 'demo' else 'VERIFIED_REPLAY'
    for row in rows:
        intent = e.intents.intents[row['intent_id']]
        if (intent.created_at is None or intent.target_quantity <= 0 or not intent.entry_orders
                or row['status'] not in ('CLOSED', 'UNFILLED') or row['bought'] != row['sold']):
            raise ValueError(f"unresolved or unattributed intent {row['intent_id']}")
        order_ids = set(intent.entry_orders + intent.exit_orders)
        for _, fill in book.current_executions():
            if fill.order_id in order_ids and fill.exec_id not in book.commissions:
                raise ValueError(f'final commission report missing for {fill.exec_id}')
        entry = submissions.get(intent.entry_orders[0])
        if entry is None or entry.get('entry_score') is None or not isfinite(entry['entry_score']):
            raise ValueError('original candidate score missing from submission journal')
        if (entry.get('score_version') != e.config.score_version
                or entry.get('max_holding_seconds') != e.config.holding_seconds
                or entry.get('exit_policy_version') != e.config.policy_id):
            raise ValueError('intent metadata does not match frozen policy')
        # Final reported fees are authoritative. Online conservative reserves
        # must not override a complete final report in a research label.
        fees = Decimal(row['actual_fees'])
        net = Decimal(row['sell_amount']) - Decimal(row['buy_amount']) - fees
        cap_mode, chase_ticks, reference, cap = caps[intent.entry_orders[0]]
        labels.append(ReplayIntentLabel(
            intent_id=intent.intent_id, day=intent.created_at.astimezone(JST).date(),
            score=float(entry['entry_score']), net_amount=net, policy_id=e.config.policy_id,
            version=e.config.model_version, holding_seconds=e.config.holding_seconds,
            quantity=intent.target_quantity, fee_version=e.commissions.version,
            policy_hash=policy_fingerprint(e.frozen_config), code_hash=manifest['code_sha256'],
            input_hash=manifest['input_sha256'], completed_at=rebuilt.last_key[0].astimezone(JST),
            bought=row['bought'], sold=row['sold'], actual_fees=fees, label_source=source,
            entry_cap_mode=cap_mode, max_chase_ticks=chase_ticks,
            reference_entry_price=reference, entry_max_price=cap,
            _verification_token=_VERIFIED))
    return tuple(labels)
