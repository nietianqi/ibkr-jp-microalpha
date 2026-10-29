"""Supported same-day full-state recovery from verified complete raw input.

Execution snapshots alone are deliberately not an engine recovery format.
Rebuilt risk locks/counters survive; all old commands and quote permissions are
discarded until fresh broker/account reconciliation and fresh market data.
"""
import json
from .config import build_engine
from .replay import Replay


def restore_from_events(document, events, *, expected_manifest):
    runner=Replay(build_engine(document))
    for event in events:
        runner.dispatch(json.loads(event) if isinstance(event,str) else event)
    runner.finish()
    if runner.manifest()!=dict(expected_manifest):
        raise ValueError('recovery requires the complete input and matching code/config manifest')
    if runner.last_key is None:
        raise ValueError('empty input cannot establish risk state')
    engine=runner.engine
    at=runner.last_key[0]
    engine.book.disconnect(at)
    engine.book.commands.clear()
    engine.account_snapshot=None
    engine.valuation._anchor=None
    engine.quotes.clear()
    for symbol in list(engine.features._series):
        engine.features.reset(symbol,'RECOVERY_MARKET_REVALIDATION')
    for symbol in tuple(engine.candidates):
        engine._invalidate(symbol,at,'RECOVERY_CANDIDATE_INVALIDATION')
    engine.risk.account_verified=False
    engine.risk.block('recovery_pending_reconciliation',at)
    engine.recovery_started_at=at
    return engine
