"""Strict, frozen research configuration. There is intentionally no live mode."""
from copy import deepcopy
from dataclasses import fields
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from typing import get_type_hints

from .economics import ChannelBudget, CommissionSchedule
from .engine import EngineConfig, Instrument, StrategyEngine
from .features import FeatureConfig, FeatureEngine
from .market import JapanCalendar, QuoteQuality, SessionSchedule
from .risk import PortfolioRisk, RiskConfig

REQUIRED_GROUPS = {'mode', 'profile', 'benchmark_symbol', 'engine', 'risk', 'commissions', 'instruments',
                   'features', 'regime', 'alpha', 'confirmation', 'market_regime', 'scalers', 'quality'}
OPTIONAL_GROUPS = {'decay', 'subscriptions', 'session', 'score_weights', 'provenance'}
PROFILES = ('demo', 'research', 'shadow')
PROVENANCE_KEYS = ('scalers', 'thresholds', 'economics')
MAX_SCALER_CLIP_RATE = 0.02


def _construct(cls, data, decimal_fields=(), tuple_fields=()):
    """Build a frozen dataclass with types enforced from its annotations (CFG-02)."""
    if not isinstance(data, dict):
        raise ValueError(f'{cls.__name__} must be a JSON object')
    allowed = {f.name for f in fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f'{cls.__name__} unknown fields: {sorted(unknown)}')
    values = dict(data)
    for key in decimal_fields:
        if key in values and values[key] is not None:
            raw = values[key]
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                raise ValueError(f'{cls.__name__}.{key} must be a decimal string or number')
            values[key] = Decimal(str(raw))
    for key in tuple_fields:
        if key in values:
            if not isinstance(values[key], list):
                raise ValueError(f'{cls.__name__}.{key} must be a list')
            values[key] = tuple(values[key])
    hints = get_type_hints(cls)
    for name, value in values.items():
        expected = hints.get(name)
        if expected is bool and type(value) is not bool:
            raise ValueError(f'{cls.__name__}.{name} must be a boolean')
        if expected is int and type(value) is not int:
            raise ValueError(f'{cls.__name__}.{name} must be an integer')
        if expected is float and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError(f'{cls.__name__}.{name} must be a number')
        if expected is str and not isinstance(value, str):
            raise ValueError(f'{cls.__name__}.{name} must be a string')
    return cls(**values)


def _check_provenance(document):
    """Non-demo profiles must show where every frozen parameter came from (STR-05)."""
    provenance = document.get('provenance')
    if not isinstance(provenance, dict):
        raise ValueError(f'profile {document["profile"]!r} requires provenance for frozen parameters')
    for key in PROVENANCE_KEYS:
        entry = provenance.get(key)
        if not isinstance(entry, dict):
            raise ValueError(f'provenance.{key} is required')
        for field in ('source', 'trained_until', 'code_hash'):
            if not isinstance(entry.get(field), str) or not entry[field]:
                raise ValueError(f'provenance.{key}.{field} is required')
        cutoff = datetime.fromisoformat(entry['trained_until'])
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError(f'provenance.{key}.trained_until requires a timezone')
    clip_rates = provenance['scalers'].get('clip_rates')
    if not isinstance(clip_rates, dict) or set(clip_rates) != set(document['scalers']):
        raise ValueError('provenance.scalers.clip_rates must report every frozen scaler')
    for name, rate in clip_rates.items():
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= MAX_SCALER_CLIP_RATE:
            raise ValueError(f'scaler {name} clips {rate!r} of training samples; scale is not data-supported')


def load_config(path):
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    return validate_document(document)


def validate_document(document):
    if not isinstance(document, dict):
        raise ValueError('configuration must be a JSON object')
    if document.get('mode') != 'replay':
        raise ValueError('only offline replay mode is implemented')
    if set(document) - (REQUIRED_GROUPS | OPTIONAL_GROUPS):
        raise ValueError(f'unknown top-level configuration field: {sorted(set(document) - REQUIRED_GROUPS - OPTIONAL_GROUPS)}')
    if REQUIRED_GROUPS - set(document):
        raise ValueError(f'missing configuration groups: {sorted(REQUIRED_GROUPS - set(document))}')
    if document['profile'] not in PROFILES:
        raise ValueError(f'profile must be one of {PROFILES}')
    minimum_days = document['engine'].get('min_independent_days')
    if type(minimum_days) is not int or minimum_days < 2:
        raise ValueError('engine.min_independent_days must be explicitly frozen at two or more')
    if document['profile'] != 'demo':
        _check_provenance(document)
    return document


def build_engine(document):
    from .execution import ExecutionBook
    from .signals import (AlphaConfig, AlphaEngine, ConfirmationConfig, ConfirmationEngine,
                          DecayConfig, FrozenRobustScaler, MarketRegimeConfig, MarketRegimeEngine,
                          RegimeConfig, RegimeEngine)
    validate_document(document)
    schedule = SessionSchedule.from_mapping(document.get('session', {}))
    engine_config = _construct(EngineConfig, document['engine'],
        ('initial_cash', 'stop_bps', 'stop_volatility_multiple', 'net_safety_margin_bps', 'gap_reserve_bps'))
    risk_config = _construct(RiskConfig, document['risk'],
        ('capital', 'trade_risk_fraction', 'symbol_fraction', 'portfolio_fraction',
         'daily_loss_fraction', 'sector_fraction', 'portfolio_stress_fraction'))
    if engine_config.initial_cash > risk_config.capital:
        raise ValueError('initial cash cannot exceed frozen strategy capital')
    commissions = _construct(CommissionSchedule, document['commissions'],
                             ('rate', 'minimum_per_order', 'additional_rate'))
    quality = _construct(QuoteQuality, document['quality'])
    feature_values = dict(document['features'])
    feature_values['quality'] = quality
    feature_config = _construct(FeatureConfig, feature_values, tuple_fields=('required_features',))
    if feature_config.version != engine_config.score_version:
        raise ValueError('feature version and score version must match')
    if feature_config.vwap_kind == 'SAMPLED':
        if not feature_config.version.startswith('l1-proxy-'):
            raise ValueError('sampled VWAP requires an independent l1-proxy-* strategy version')
        if any(name.startswith('vwap_') and not name.startswith('vwap_proxy_')
               for name in feature_config.required_features):
            raise ValueError('sampled VWAP must retain proxy field names')
    elif any(name.startswith('vwap_proxy_') for name in feature_config.required_features):
        raise ValueError('complete-trade VWAP cannot require proxy names')
    alpha_config = _construct(AlphaConfig, document['alpha'])
    confirmation_config = _construct(ConfirmationConfig, document['confirmation'],
                                     tuple_fields=('required_ti_features',))
    if confirmation_config.version != engine_config.score_version:
        raise ValueError('confirmation version mismatch')
    if not isinstance(document['scalers'], dict):
        raise ValueError('scalers must be an object')
    scalers = {key: _construct(FrozenRobustScaler, value) for key, value in document['scalers'].items()}
    weights = document.get('score_weights')
    if weights is not None and (not isinstance(weights, dict) or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) for v in weights.values())):
        raise ValueError('score_weights must map features to numbers')
    if not isinstance(document['instruments'], dict):
        raise ValueError('instruments must be an object')
    instruments = {symbol: _construct(Instrument, data) for symbol, data in document['instruments'].items()}
    if not instruments or not document['benchmark_symbol']:
        raise ValueError('instrument universe and benchmark are required')
    if any(i.tick_category != feature_config.tick_category for i in instruments.values()):
        raise ValueError('this feature engine requires one verified tick category per configured universe')
    budget = ChannelBudget(alpha_config.ttl_seconds, confirmation_config.persistence_seconds,
                           engine_config.submit_p99_seconds, engine_config.cancel_p99_seconds,
                           (engine_config.entry_order_ttl_seconds,), 0, engine_config.budget_buffer_seconds)
    if not budget.feasible:
        raise ValueError(f'channel time budget {budget.required_seconds}s exceeds candidate TTL '
                         f'{alpha_config.ttl_seconds}s (specification section 3)')
    scheduler = requirements = None
    plan_interval = 30.0
    if confirmation_config.enhanced:
        from .subscriptions import SubscriptionScheduler, VersionRequirements
        settings = document.get('subscriptions')
        if not settings:
            raise ValueError('enhanced configuration requires subscriptions')
        expected = {'quota', 'min_tenure_seconds', 'required_windows', 'plan_interval_seconds'}
        if set(settings) != expected:
            raise ValueError('subscriptions must freeze quota, min_tenure_seconds, required_windows '
                             'and plan_interval_seconds')
        required_ti = {name for name in feature_config.required_features if name.startswith('ti_')}
        required_ti.update(confirmation_config.required_ti_features)
        if not required_ti.issubset(settings['required_windows']):
            raise ValueError('READY must include every enabled TI feature')
        if any(float(settings['required_windows'][name]) < int(name.rsplit('_', 1)[-1])
               for name in required_ti):
            raise ValueError('READY feature window shorter than required feature')
        if settings['required_windows'].get('r_30', 0) < 30:
            raise ValueError('enhanced version requires complete 30 second confirmation window')
        if feature_config.trade_source != 'TBT' or feature_config.vwap_kind != 'TICK':
            raise ValueError('enhanced version requires the independently configured TBT source')
        plan_interval = settings['plan_interval_seconds']
        if isinstance(plan_interval, bool) or not isinstance(plan_interval, (int, float)) or not 30 <= plan_interval <= 60:
            raise ValueError('pre-candidate ranking runs every 30-60 seconds (specification section 4)')
        scheduler = SubscriptionScheduler(settings['quota'], min_tenure_seconds=settings['min_tenure_seconds'])
        requirements = VersionRequirements(engine_config.score_version, feature_config.trade_source,
            {k: float(v) for k, v in settings['required_windows'].items()},
            minimum_coverage=confirmation_config.min_classification_coverage,
            max_age_seconds=quality.max_age_seconds)
    engine = StrategyEngine(
        engine_config, instruments, calendar=JapanCalendar(schedule=schedule),
        features=FeatureEngine(document['benchmark_symbol'], feature_config, schedule=schedule),
        regime=RegimeEngine(_construct(RegimeConfig, document['regime'])),
        alpha=AlphaEngine(alpha_config, scalers, engine_config.score_version, weights),
        confirmation=ConfirmationEngine(confirmation_config),
        market_regime=MarketRegimeEngine(_construct(MarketRegimeConfig, document['market_regime'])),
        book=ExecutionBook(daily_request_budget=risk_config.max_routine_requests_per_day),
        risk=PortfolioRisk(risk_config), commissions=commissions, quality=quality,
        decay_config=_construct(DecayConfig, document['decay']) if document.get('decay') is not None else None,
        subscription_scheduler=scheduler, subscription_requirements=requirements,
        plan_interval_seconds=plan_interval)
    engine.frozen_config = deepcopy(document)
    return engine
