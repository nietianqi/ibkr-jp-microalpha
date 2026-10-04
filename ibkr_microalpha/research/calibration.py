"""Frozen calibration artifacts with day-block uncertainty (sections 7, 9, 15).

Seconds-level samples within a day are strongly dependent, so the confidence
bound of the mean resamples whole trading days, never individual intents.
"""
from datetime import date, datetime
from decimal import Decimal
import random
from typing import Iterable, Mapping, Sequence

from ..economics import CalibrationRow
from ..signals import FrozenRobustScaler


def day_block_lower_bound(net_by_day: Mapping[date, Sequence[Decimal]], *, alpha: float = 0.05,
                          draws: int = 2000, seed: int = 7) -> tuple[Decimal, Decimal]:
    """(mean, lower bound of the mean) per intent using a day-block bootstrap."""
    days = sorted(day for day, values in net_by_day.items() if values)
    if not days:
        raise ValueError("calibration needs at least one day with samples")
    if not 0 < alpha < 1 or type(draws) is not int or draws <= 0:
        raise ValueError("alpha must be in (0, 1) and draws positive")
    rng = random.Random(seed)

    def mean_of(sample_days):
        values = [value for day in sample_days for value in net_by_day[day]]
        return sum(values, Decimal(0)) / len(values)

    boots = sorted(mean_of([rng.choice(days) for _ in days]) for _ in range(draws))
    return mean_of(days), boots[int(alpha * draws)]


def build_calibration_rows(samples: Iterable[tuple[date, float, Decimal]], edges: Sequence[float], *,
                           policy_id: str, version: str, holding_seconds: int, quantity: int,
                           max_chase_ticks: int, min_days: int, min_samples: int, alpha: float = 0.05,
                           draws: int = 2000, seed: int = 7) -> list[CalibrationRow]:
    """One row per score bucket [edge_i, edge_i+1) with enough independent days.

    Buckets without enough days or samples are omitted: the coordinator then
    rejects candidates there (no calibration), it never extrapolates.
    """
    edges = list(edges)
    if not edges or edges != sorted(edges) or len(set(edges)) != len(edges):
        raise ValueError("bucket edges must be strictly increasing")
    buckets: dict[int, dict[date, list[Decimal]]] = {}
    for day, score, net in samples:
        if net is None:
            raise ValueError("unestimable labels must be resolved, not silently dropped")
        if score < edges[0]:
            continue
        index = max(i for i, edge in enumerate(edges) if score >= edge)
        buckets.setdefault(index, {}).setdefault(day, []).append(net)
    rows = []
    for index, by_day in sorted(buckets.items()):
        count = sum(len(values) for values in by_day.values())
        if len(by_day) < min_days or count < min_samples:
            continue
        mean, lower = day_block_lower_bound(by_day, alpha=alpha, draws=draws, seed=seed)
        rows.append(CalibrationRow(
            policy_id=policy_id, version=version, holding_seconds=holding_seconds, quantity=quantity,
            score_low=float(edges[index]), score_high=float(edges[index + 1]) if index + 1 < len(edges) else None,
            sample_days=len(by_day), sample_count=count,
            mean_net_amount=mean.quantize(Decimal("0.01")), lower_net_amount=min(lower, mean).quantize(Decimal("0.01")),
            max_chase_ticks=max_chase_ticks))
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
         "max_chase_ticks": r.max_chase_ticks} for r in rows]}
