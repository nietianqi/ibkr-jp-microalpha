"""Read existing session evidence and replay locally. Never connect or route orders.

Run from the repository root:
    python audit/2026-10-30/no-fill-review/diagnose.py
The input snapshot is kept in ignored runs/; summary evidence is kept here.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from queue import Queue
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from ibkr_microalpha.config import build_engine
from ibkr_microalpha.paper import IbkrPaperGateway
from ibkr_microalpha.recording import CallbackRecorder, CaptureConfig
from ibkr_microalpha.replay import Replay, encode


class DiscardSink:
    def write(self, text):
        return len(text)


def read_snapshot(path):
    # A growing file can end inside its last line. Freeze complete lines only.
    raw = path.read_bytes()
    complete = raw[:raw.rfind(b'\n') + 1]
    return complete, dict(path=str(path.relative_to(ROOT)), bytes=len(complete),
                         sha256=sha256(complete).hexdigest(),
                         excluded_trailing_bytes=len(raw) - len(complete))


def event_summary(raw):
    counts, symbols, first, last = Counter(), defaultdict(Counter), {}, {}
    positive_health = 0
    for line in raw.splitlines():
        row = json.loads(line)
        kind = row.get('type', row.get('callback'))
        counts[kind] += 1
        data = row.get('data', {})
        symbol = data.get('symbol')
        if symbol is not None:
            symbols[symbol][kind] += 1
        first.setdefault(kind, row.get('received_at'))
        last[kind] = row.get('received_at')
        if kind == 'quote_stream_health' and data.get('healthy') is True:
            positive_health += 1
    return dict(counts=counts, by_symbol=symbols, first_at=first, last_at=last,
                positive_quote_health=positive_health)


def diagnose_replay(document, raw):
    engine = build_engine(document)
    runner = Replay(engine, raw_sink=DiscardSink())
    resets, snapshot_reasons = defaultdict(Counter), defaultdict(Counter)
    spans, market_reasons = defaultdict(float), Counter()
    soft_at_requests = Counter()
    request_ticks = 0
    original_reset = engine.features.reset
    original_evaluate = engine.evaluate
    original_market = engine.features.market_snapshot

    def reset(symbol, reason='DATA_RESET', **kwargs):
        resets[symbol][reason] += 1
        return original_reset(symbol, reason, **kwargs)

    def evaluate(snapshot, at):
        snapshot_reasons[snapshot.symbol][snapshot.reason or 'VALID'] += 1
        return original_evaluate(snapshot, at)

    def market_snapshot(at, universe):
        result = original_market(at, universe)
        market_reasons[result.reason or 'VALID'] += 1
        return result

    engine.features.reset = reset
    engine.evaluate = evaluate
    engine.features.market_snapshot = market_snapshot
    for line in raw.splitlines():
        event = json.loads(line)
        runner.dispatch(event)
        for symbol, series in engine.features._series.items():
            if series.quotes:
                span = (series.quotes[-1].at - series.quotes[0].at).total_seconds()
                spans[symbol] = max(spans[symbol], span)
        if event['type'] == 'requests':
            request_ticks += 1
            soft_at_requests.update(list(engine.risk.soft_blocks) or ['NONE'])
    runner.finish()
    return dict(report=runner.report(), resets=resets,
                snapshot_reasons=snapshot_reasons,
                max_retained_quote_span_seconds=spans,
                market_snapshot_reasons=market_reasons,
                request_ticks=request_ticks, soft_blocks_at_request_ticks=soft_at_requests,
                audit=engine.audit,
                latest_snapshots={s: vars(v) for s, v in engine.snapshots.items()},
                latest_market_snapshot=vars(engine.market_snapshot) if engine.market_snapshot else None)


def diagnose_capture(capture, raw):
    recorder = CallbackRecorder(capture, events_sink=DiscardSink(), raw_sink=DiscardSink())
    reasons = defaultdict(Counter)
    original_quote = recorder._quote

    def quote(request, at, output):
        state, metadata = recorder.states[request], recorder.metadata[request]
        fields = state['fields']
        if state['data_type'] != 1 or metadata.quote_size_multiplier is None:
            reason = 'TYPE_OR_UNITS_UNKNOWN'
        elif any(k not in fields for k in ('bid', 'ask', 'bid_size', 'ask_size')):
            reason = 'MISSING_FIELDS'
        else:
            times = [stamp for _, stamp in fields.values()]
            if any(not 0 <= (at - stamp).total_seconds() <= capture.max_field_age_seconds for stamp in times):
                reason = 'FIELD_AGE'
            elif (max(times) - min(times)).total_seconds() > capture.max_field_skew_seconds:
                reason = 'FIELD_SKEW'
            elif not 0 < fields['bid'][0] <= fields['ask'][0] or min(fields['bid_size'][0], fields['ask_size'][0]) <= 0:
                reason = 'PRICE_OR_SIZE'
            else:
                reason = 'EMITTED'
        reasons[metadata.symbol][reason] += 1
        return original_quote(request, at, output)

    recorder._quote = quote
    for line in raw.splitlines():
        recorder.process(json.loads(line))
    return dict(quote_attempts_by_symbol=reasons, callbacks=recorder.callbacks,
                normalized_events=recorder.sequence)


def reproduce_execution_snapshot_bug(document, capture):
    # Constructing the adapter only allocates EClient; no connect() is called.
    gateway = IbkrPaperGateway(capture, host='127.0.0.1', port=4002,
                              client_id=9999, queue=Queue())
    gateway.snapshot = dict(open_orders=[], positions={}, executions=[], parts=set())
    symbol = next(iter(document['instruments']))
    gateway.client.execDetails(9001, SimpleNamespace(symbol=symbol),
        SimpleNamespace(execId='review-only-execution', orderId=1000,
                        shares=100, price=1000, side='BOT',
                        time='20261030 13:20:00 Asia/Tokyo'))
    execution = gateway.snapshot['executions'][0]
    runner = Replay(build_engine(document))
    try:
        runner.dispatch(dict(event_id='review-reconcile', sequence=1,
            received_at=datetime.now(timezone.utc).isoformat(), type='reconcile',
            data=dict(positions={}, open_orders=[], executions=[execution],
                      complete=True, ownership_confirmed=True, next_order_id=1000)))
    except KeyError as error:
        return dict(reproduced=True, adapter_execution=execution,
                    exception=repr(error), connects=0, broker_requests=0)
    raise AssertionError('Expected the current adapter execution to lack required at')


def main():
    session = ROOT / 'runs/live/20261030'
    snapshots = ROOT / 'runs/no-fill-review-20261030'
    snapshots.mkdir(parents=True, exist_ok=True)
    config_raw = (ROOT / 'examples/jp7-live-small.json').read_bytes()
    document = json.loads(config_raw)
    capture_document = json.loads((ROOT / 'examples/capture-jp7.json').read_text(encoding='utf-8'))
    capture = CaptureConfig.from_dict(capture_document)
    summaries, manifests, frozen = {}, {}, {}
    for name in ('raw-input.jsonl', 'capture-events.jsonl', 'callbacks.jsonl'):
        raw, manifest = read_snapshot(session / name)
        frozen[name] = raw
        manifests[name] = manifest
        summaries[name] = event_summary(raw)
    (snapshots / 'raw-input.snapshot.jsonl').write_bytes(frozen['raw-input.jsonl'])
    (snapshots / 'config.snapshot.json').write_bytes(config_raw)
    result = dict(created_at=datetime.now(timezone.utc).isoformat(),
        scope='Local evidence replay only; no live process/config/order changes',
        limitation='Files were read sequentially while session was running; each complete prefix has its own hash. Current on-disk config was used because startup did not persist a frozen config.',
        source_prefixes=manifests,
        config_sha256=sha256(config_raw).hexdigest(), summaries=summaries,
        configured_symbols=sorted(document['instruments']),
        captured_but_excluded_symbols=sorted(set(m.symbol for m in capture.instruments)
            - set(document['instruments']) - {document['benchmark_symbol']}),
        replay=diagnose_replay(document, frozen['raw-input.jsonl']),
        capture_diagnostics=diagnose_capture(capture, frozen['callbacks.jsonl']),
        execution_snapshot_bug=reproduce_execution_snapshot_bug(document, capture),
        validation=dict(command='python -m unittest discover -q', tests=331, result='OK',
                        note='Existing suite run separately; ResourceWarning for an unclosed test input.'))
    target = Path(__file__).with_name('evidence.json')
    target.write_text(json.dumps(result, ensure_ascii=False, default=encode, indent=2), encoding='utf-8')
    print(json.dumps(dict(evidence=str(target.relative_to(ROOT)),
        last_event=summaries['raw-input.jsonl']['last_at']['requests'],
        replay_events=result['replay']['report']['events_processed'],
        candidates=result['replay']['report']['layer_funnel']['candidates'],
        orders_sent=result['replay']['report']['requests_sent'],
        request_ticks=result['replay']['request_ticks'],
        soft_blocks=result['replay']['soft_blocks_at_request_ticks'],
        max_quote_spans=result['replay']['max_retained_quote_span_seconds'],
        reconciliation_bug=result['execution_snapshot_bug']), ensure_ascii=False))


if __name__ == '__main__':
    main()
