"""Frozen calibration artifacts with day-block uncertainty (sections 7, 9, 15).

Seconds-level samples within a day are strongly dependent, so the confidence
bound of the mean resamples whole trading days, never individual intents.
"""
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from math import isfinite
import random
from typing import Iterable, Mapping, Sequence

from ..economics import CalibrationRow
from ..signals import FrozenRobustScaler
from .labels import ReplayIntentLabel


def day_block_lower_bound(net_by_day: Mapping[date, Sequence[Decimal]], *, alpha: float = 0.05,
                          draws: int = 2000, seed: int = 7) -> tuple[Decimal, Decimal]:
    """(mean, lower bound of the mean) per intent using a day-block bootstrap."""
    days = sorted(day for day, values in net_by_day.items() if values)
    if len(days) < 2:
        raise ValueError("calibration requires at least two independent days")
    if any(type(day) is not date for day in days) or any(
            not isinstance(value, Decimal) or not value.is_finite()
            for day in days for value in net_by_day[day]):
        raise ValueError('day-block samples require date identities and finite Decimal outcomes')
    if not 0 < alpha < 1 or type(draws) is not int or draws <= 0:
        raise ValueError("alpha must be in (0, 1) and draws positive")
    rng = random.Random(seed)

    def mean_of(sample_days):
        values = [value for day in sample_days for value in net_by_day[day]]
        return sum(values, Decimal(0)) / len(values)

    boots = sorted(mean_of([rng.choice(days) for _ in days]) for _ in range(draws))
    return mean_of(days), boots[int(alpha * draws)]


def build_calibration_rows(samples: Iterable[ReplayIntentLabel], edges: Sequence[float], *,
                           policy_id: str, version: str, holding_seconds: int, quantity: int,
                           max_chase_ticks: int, min_days: int, min_samples: int, alpha: float = 0.05,
                           draws: int = 2000, seed: int = 7) -> list[CalibrationRow]:
    """One row per score bucket [edge_i, edge_i+1) with enough independent days.

    Buckets without enough days or samples are omitted: the coordinator then
    rejects candidates there (no calibration), it never extrapolates.
    """
    if type(min_days) is not int or min_days < 2 or type(min_samples) is not int or min_samples <= 0:
        raise ValueError('min_days must be at least two and min_samples positive')
    edges = list(edges)
    if (not edges or any(type(edge) not in (int, float) or not isfinite(edge) for edge in edges)
            or edges != sorted(edges) or len(set(edges)) != len(edges)):
        raise ValueError("bucket edges must be strictly increasing")
    buckets: dict[int, list[ReplayIntentLabel]] = {}
    identities = set()
    contract = None
    for label in samples:
        if not isinstance(label, ReplayIntentLabel):
            raise ValueError('formal calibration requires factory-issued ReplayIntentLabel, not quote baselines')
        if (label.policy_id, label.version, label.holding_seconds, label.quantity) != (
                policy_id, version, holding_seconds, quantity):
            raise ValueError('replay label policy, version, holding or quantity mismatch')
        if label.entry_cap_mode != 'RELATIVE_TICKS' or label.max_chase_ticks != max_chase_ticks:
            raise ValueError('replay label chase cap does not match the requested calibration policy')
        current_contract = (label.policy_hash, label.fee_version, label.label_source, label.max_chase_ticks)
        if contract is not None and contract != current_contract:
            raise ValueError('cannot pool different executable policies, fees or label sources')
        contract = current_contract
        identity = (label.input_hash, label.intent_id)
        if identity in identities:
            raise ValueError('duplicate replay intent label')
        identities.add(identity)
        score = label.score
        if score < edges[0]:
            continue
        index = max(i for i, edge in enumerate(edges) if score >= edge)
        buckets.setdefault(index, []).append(label)
    rows = []
    for index, labels in sorted(buckets.items()):
        by_day = {}
        for label in labels:
            by_day.setdefault(label.day, []).append(label.net_amount)
        count = len(labels)
        if len(by_day) < min_days or count < min_samples:
            continue
        mean, lower = day_block_lower_bound(by_day, alpha=alpha, draws=draws, seed=seed)
        label_hash = hashlib.sha256(json.dumps([
            {'input': label.input_hash, 'intent': label.intent_id, 'day': label.day.isoformat(),
             'score': label.score, 'net': str(label.net_amount), 'fees': str(label.actual_fees),
             'max_chase_ticks': label.max_chase_ticks, 'reference_entry_price': str(label.reference_entry_price),
             'entry_max_price': str(label.entry_max_price),
             'completed_at': label.completed_at.isoformat()} for label in sorted(
                 labels, key=lambda value: (value.day, value.input_hash, value.intent_id))],
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        evidence = dict(policy_hash=labels[0].policy_hash, fee_version=labels[0].fee_version,
                        code_hashes=sorted({label.code_hash for label in labels}),
                        input_hashes=sorted({label.input_hash for label in labels}), labels_hash=label_hash,
                        trained_until=max(label.completed_at for label in labels).isoformat(),
                        independent_days=[day.isoformat() for day in sorted(by_day)],
                        max_chase_ticks=max_chase_ticks,
                        complete_policy=True, includes_partial=True, fees_final=True)
        if labels[0].label_source == 'ARTIFICIAL':
            evidence['source'] = 'ARTIFICIAL_FIXTURE'
        rows.append(CalibrationRow(
            policy_id=policy_id, version=version, holding_seconds=holding_seconds, quantity=quantity,
            score_low=float(edges[index]), score_high=float(edges[index + 1]) if index + 1 < len(edges) else None,
            sample_days=len(by_day), sample_count=count,
            mean_net_amount=mean.quantize(Decimal("0.01")), lower_net_amount=min(lower, mean).quantize(Decimal("0.01")),
            max_chase_ticks=max_chase_ticks, label_source=labels[0].label_source,
            provenance=evidence))
    return rows


def fit_scalers(training: Mapping[str, Sequence[float]], epsilon: Mapping[str, float]
                ) -> tuple[dict[str, FrozenRobustScaler], dict[str, float]]:
    """Training-only robust scalers and the share of training samples at the clip."""
    if set(training) != set(epsilon):
        raise ValueError("every feature needs an explicit epsilon")
    scalers, clip_rates = {}, {}
    for name, samples in training.items():
        scaler = FrozenRobustScaler.fit_training(list(samples), epsilon[name])
        scalers[name] = scaler
        clip_rates[name] = scaler.clip_rate(list(samples))
    return scalers, clip_rates


def calibration_table_event(rows: Sequence[CalibrationRow], *, version: str, known_at: datetime) -> dict:
    """Replay ``calibration_table`` event payload for the frozen rows."""
    return {"version": version, "known_at": known_at.isoformat(), "rows": [
        {"policy_id": r.policy_id, "version": r.version, "holding_seconds": r.holding_seconds,
         "quantity": r.quantity, "score_low": r.score_low, "score_high": r.score_high,
         "sample_days": r.sample_days, "sample_count": r.sample_count,
         "mean_net_amount": str(r.mean_net_amount), "lower_net_amount": str(r.lower_net_amount),
         "max_chase_ticks": r.max_chase_ticks, "label_source": r.label_source,
         "provenance": r.provenance} for r in rows]}
