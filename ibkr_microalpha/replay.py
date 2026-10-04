"""Causal JSONL replay. This module performs disk I/O outside trading callbacks.

Each event is parsed and validated before it touches the engine; its identity
is committed only after application and evidence writes (reviews RPL-01/04).
An application, evidence or whole-file failure poisons the runner: the engine
must be rebuilt from the frozen config and verified input, never retried in
place. Identities are kept as digests and
raw input/audit can be streamed to disk, so memory does not hold every payload
(review RPL-02).
"""
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from bisect import bisect_left
import hashlib
import json
import math
import os
import shutil
import tempfile
import time

from .domain import FeatureSnapshot, Quote, aware
from .economics import CalibrationRow, CalibrationTable, Prediction
from .engine import Forecast
from .features import SameTimeProfile, Trade, VolumeBaseline, VolatilityBaseline
from .market import JST, CalendarAnnouncement, ScheduledWindow, TradingDay
from .reporting import layer_funnel
from .risk import AccountSnapshot

PACKAGE_ROOT = Path(__file__).resolve().parent
RUN_FILES = ('raw-input.jsonl', 'audit.jsonl', 'frozen-config.json', 'execution.json',
             'execution-journal.jsonl', 'report.json')


def publish_run(staging, output):
    """Publish closed artifacts, committing report last; roll back an older run.

    While replacement is in progress no success marker is visible. Backups are
    on the same filesystem and retained if rollback itself fails, so recovery
    never depends on a temporary directory that would be deleted on exit.
    """
    staging, output = Path(staging).resolve(), Path(output).resolve()
    names = [name for name in RUN_FILES if (staging/name).is_file()]
    if 'report.json' not in names:
        raise ValueError('cannot publish a run without its success report')
    output.mkdir(parents=True, exist_ok=True)
    for name in (*names, 'failure.json'):
        target = output/name
        if target.exists() and not target.is_file():
            raise ValueError(f'run artifact must be a file: {target}')
    backup = Path(tempfile.mkdtemp(prefix=f'.{output.name}-previous-', dir=output.parent)).resolve()
    # All recursively removed files are confined to this newly created backup.
    backup.relative_to(output.parent.resolve())
    moved, published = set(), []
    try:
        for name in ('report.json', 'failure.json'):
            target = output/name
            if target.exists() or target.is_symlink():
                os.replace(target, backup/name)
                moved.add(name)
        for name in names:
            if name == 'report.json':
                continue
            target = output/name
            if target.exists() or target.is_symlink():
                os.replace(target, backup/name)
                moved.add(name)
            os.replace(staging/name, target)
            published.append(name)
        os.replace(staging/'report.json', output/'report.json')
        published.append('report.json')
    except Exception as error:
        rollback_errors = []
        for name in reversed(published):
            if name == 'report.json':
                continue
            try:
                if name in moved:
                    os.replace(backup/name, output/name)
                    moved.remove(name)
                else:
                    (output/name).unlink(missing_ok=True)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        for name in sorted(moved - {'report.json'}):
            try:
                os.replace(backup/name, output/name)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        # An old report may be restored only after all of its artifacts are back.
        if not rollback_errors and 'report.json' in moved:
            try:
                os.replace(backup/'report.json', output/'report.json')
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            error.add_note(f'rollback incomplete; recover previous artifacts from {backup}: '
                           + '; '.join(rollback_errors))
        else:
            try:
                shutil.rmtree(backup)
            except OSError:
                pass  # Old artifacts are restored; a leftover backup is recoverable.
        raise
    else:
        try:
            shutil.rmtree(backup)
        except OSError:
            pass  # Publication is committed; cleanup cannot turn it into a failed run.
    return output/'report.json'


class _DurationHistogram:
    """192 fixed logarithmic bins, about 9% spacing; exact count and maximum."""
    BOUNDS = tuple(int(1000 * 2 ** (index / 8)) for index in range(192))

    def __init__(self):
        self.bins = [0] * len(self.BOUNDS)
        self.count = self.maximum = 0

    def add(self, elapsed):
        index = min(bisect_left(self.BOUNDS, elapsed), len(self.bins)-1)
        self.bins[index] += 1
        self.count += 1
        self.maximum = max(self.maximum, elapsed)

    def percentile(self, fraction):
        target, cumulative = max(1, math.ceil(self.count*fraction)), 0
        for index, count in enumerate(self.bins):
            cumulative += count
            if cumulative >= target:
                # Overflow observations share the final bin; maximum remains exact.
                return self.maximum if index == len(self.bins)-1 else min(self.maximum, self.BOUNDS[index])
        return self.maximum


def timestamp(value):
    return aware(datetime.fromisoformat(value))


def encode(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, 'value'):
        return value.value
    raise TypeError(f'cannot serialize {type(value).__name__}')


def canonical(event) -> str:
    return json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(event) -> str:
    return hashlib.sha256(canonical(event).encode('utf-8')).hexdigest()


def code_hash() -> str:
    sha = hashlib.sha256()
    for path in sorted(PACKAGE_ROOT.rglob('*.py')):
        sha.update(str(path.relative_to(PACKAGE_ROOT)).encode())
        sha.update(path.read_bytes())
    return sha.hexdigest()


class ReplayFailed(RuntimeError):
    """The runner aborted; rebuild and replay the original verified input."""


def _decimal_fields(data, *names):
    for name in names:
        if data.get(name) is not None:
            data[name] = Decimal(str(data[name]))


def _bool(data, name):
    if type(data.get(name)) is not bool:
        raise ValueError(f'{name} requires a boolean')
    return data[name]


class Replay:
    def __init__(self, engine, *, raw_sink=None, audit_sink=None):
        self.engine = engine
        self.last_key = None
        self.seen_digests: dict[str, str] = {}
        self.events_processed = 0
        self.events_applied = 0
        self.last_commands = []
        self.day = None
        self.failed = False
        self.raw_sink, self.audit_sink = raw_sink, audit_sink
        self.raw_lines: list[str] = []      # compact canonical lines when no sink is configured
        self._input_hash = hashlib.sha256()
        self._durations: dict[str, _DurationHistogram] = {}
        self._finished = False
        self._wall_ns = 0

    # ---------------------------------------------------------------- parse
    def _parse(self, kind, data, at, identity):
        """Return a closure applying the event; parsing never mutates the engine."""
        e = self.engine
        if kind == 'calendar':
            known_at = timestamp(data.pop('known_at'))
            if known_at > at:
                raise ValueError('calendar cannot be received before its known_at')
            record = TradingDay(date.fromisoformat(data.pop('day')), known_at=known_at, **data)
            return lambda: e.calendar.records.append(record)
        if kind == 'announcement':
            if data.get('announced_at'):
                data['announced_at'] = timestamp(data['announced_at'])
            record = CalendarAnnouncement(received_at=at, **data)
            return lambda: e.calendar.announcements.append(record)
        if kind == 'scheduled_window':
            for field in ('start', 'end', 'known_at'):
                data[field] = timestamp(data[field])
            if data['known_at'] > at:
                raise ValueError('future known event')
            record = ScheduledWindow(**data)
            return lambda: e.calendar.scheduled_windows.append(record)
        if kind == 'quote':
            _decimal_fields(data, 'bid', 'ask')
            for field in ('bid_at', 'ask_at', 'exchange_at'):
                if data.get(field):
                    data[field] = timestamp(data[field])
            quote = Quote(at=at, event_id=identity, **data)
            return lambda: e.on_quote(quote)
        if kind == 'trade':
            if data.get('volume_kind', 'TICK') != 'TICK':
                # Summed last-trade sizes are not volume (specification section 4);
                # sampled volume must arrive as cumulative_volume.
                raise ValueError('sampled volume must arrive as cumulative_volume, never as trades')
            _decimal_fields(data, 'price')
            if data.get('exchange_at'):
                data['exchange_at'] = timestamp(data['exchange_at'])
            trade = Trade(at=at, event_id=identity, **data)
            return lambda: e.on_trade(trade)
        if kind == 'cumulative_volume':
            symbol, total = data['symbol'], data['total']
            if isinstance(total, bool) or not isinstance(total, int):
                raise ValueError('cumulative volume total must be an integer')
            price = Decimal(str(data['last_price']))
            source = data.get('source')
            return lambda: e.on_cumulative_volume(symbol, at, total, price, identity, source)
        if kind == 'stream_health':
            symbol, healthy = data['symbol'], _bool(data, 'healthy')
            source = data.get('source', e.features.config.trade_source)
            return lambda: e.on_trade_stream_health(symbol, at, healthy, source)
        if kind == 'quote_stream_health':
            symbol, healthy = data['symbol'], _bool(data, 'healthy')
            source = data.get('source')
            return lambda: e.on_quote_stream_health(symbol, at, healthy, source)
        if kind in ('volume_baseline', 'volatility_baseline'):
            data['day'] = date.fromisoformat(data['day'])
            data['known_at'] = timestamp(data['known_at'])
            if data['known_at'] > at:
                raise ValueError('future volume baseline')
            if kind == 'volume_baseline':
                row = VolumeBaseline(**data)
                return lambda: e.features.add_volume_baseline(row)
            row = VolatilityBaseline(**data)
            return lambda: e.features.add_volatility_baseline(row)
        if kind == 'same_time_profile':
            data['known_at'] = timestamp(data['known_at'])
            if data['known_at'] > at:
                raise ValueError('future same-time profile')
            profile = SameTimeProfile(**data)
            return lambda: e.features.add_profile(profile)
        if kind == 'daily_vwap':
            _decimal_fields(data, 'cumulative_value')
            return lambda: e.features.set_daily_vwap(at=at, **data)
        if kind == 'data_reset':
            symbol, reason = data['symbol'], data.get('reason', 'DATA_RESET')

            def apply():
                e.features.reset(symbol, reason)
                e._invalidate(symbol, at, reason)
                if e.subscriptions is not None:
                    e.subscriptions.scheduler.reset(symbol, at)
                e.reevaluate(symbol, at)
            return apply
        if kind == 'subscription_failed':
            if e.subscriptions is None:
                raise ValueError('no enabled subscription scheduler')
            symbol = data['symbol']

            def apply():
                e.subscriptions.scheduler.subscription_failed(symbol)
                e.reevaluate(symbol, at)
            return apply
        if kind == 'market_snapshot':
            if e.config.market_source != 'external':
                raise ValueError('market snapshots are computed internally for this configuration')
            snapshot = FeatureSnapshot(at=at, **data)
            return lambda: e.set_market(snapshot, at)
        if kind == 'feature_snapshot':
            snapshot = FeatureSnapshot(at=at, **data)

            def apply():
                # Precomputed inputs must preserve receive chronology and frozen
                # feature version. They are separate from raw-feature evidence.
                e.poll(at)
                e.evaluate(snapshot, at)
            return apply
        if kind == 'forecast':
            symbol = data.pop('symbol')
            prediction_data = data.pop('prediction')
            _decimal_fields(prediction_data, 'mean_net_amount', 'lower_net_amount')
            for field in ('trained_until', 'valid_until'):
                data[field] = timestamp(data[field])
            _decimal_fields(data, 'reference_entry_price', 'max_entry_price')
            forecast = Forecast(Prediction(**prediction_data), received_at=at, **data)
            return lambda: e.set_forecast(symbol, forecast, at)
        if kind == 'calibration_table':
            known_at = timestamp(data['known_at'])
            rows = []
            for row in data['rows']:
                row = dict(row)
                _decimal_fields(row, 'mean_net_amount', 'lower_net_amount')
                rows.append(CalibrationRow(**row))
            table = CalibrationTable(rows, known_at=known_at, version=data['version'])
            return lambda: e.set_calibration(table, at)
        if kind == 'account_snapshot':
            _decimal_fields(data, 'available_funds', 'net_liquidation')
            if 'covered_order_ids' in data:
                if not isinstance(data['covered_order_ids'], list):
                    raise ValueError('covered_order_ids must be an array')
                data['covered_order_ids'] = tuple(data['covered_order_ids'])
            snapshot = AccountSnapshot(received_at=at, **data)
            return lambda: e.set_account(snapshot, at)
        if kind == 'exchange_status':
            normal = _bool(data, 'normal')
            reason = data.get('reason', '')
            return lambda: e.set_exchange_status(normal, at, reason)
        if kind == 'status':
            return lambda: e.book.status(at=at, **data)
        if kind == 'fill':
            _decimal_fields(data, 'price')
            if data.get('executed_at'):
                data['executed_at'] = timestamp(data['executed_at'])
            return lambda: e.on_fill(at=at, **data)
        if kind == 'commission':
            exec_id, amount = data['exec_id'], Decimal(str(data['amount']))
            return lambda: e.book.commission(exec_id, amount)
        if kind == 'reconcile':
            for flag in ('complete', 'ownership_confirmed'):
                _bool(data, flag)
            for execution in data.get('executions', []):
                if timestamp(execution['at']) > at:
                    raise ValueError('reconciliation cannot contain future executions')
            return lambda: e.book.reconcile(at=at, **data)
        if kind == 'requests':
            def apply():
                # Finish same-time decisions, refresh risk, then re-check every
                # queued entry at the actual send time (review EXE-02).
                e.flush(at)
                e.poll(at)
                self.last_commands = e.book.drain_commands(at, entry_validator=e.entry_still_valid)
                for command in self.last_commands:
                    e._record(at, 'REQUEST_DISPATCHED', command=command.kind,
                              order_id=command.order_id, payload=command.payload)
            return apply
        if kind == 'disconnect':
            return lambda: e.book.disconnect(at)
        if kind == 'reconnect':
            return lambda: e.book.reconnect(at)
        if kind == 'manual_unlock':
            return lambda: e.risk.manual_unlock(**data)
        if kind == 'reject':
            return lambda: e.book.reject(at=at, **data)
        if kind == 'ambiguous':
            return lambda: e.book.ambiguous(at=at, **data)
        if kind == 'kill_switch':
            reason = data.get('reason', 'KILL_SWITCH')
            return lambda: e.kill_switch(at, reason)
        if kind == 'timer':
            return lambda: None
        raise ValueError(f'unknown event type {kind}')

    # ------------------------------------------------------------- dispatch
    def _require_healthy(self):
        if self.failed:
            raise ReplayFailed('replay failed; rebuild the engine and replay verified input')

    def mark_failed(self):
        """Called by the output owner when closing or publishing evidence fails."""
        self.failed = True

    def dispatch(self, event):
        self._require_healthy()
        started = time.perf_counter_ns()
        if not isinstance(event, dict):
            raise ValueError('event must be an object')
        at = timestamp(event['received_at'])
        sequence = event['sequence']
        if type(sequence) is not int or sequence < 0:
            raise ValueError('sequence must be a nonnegative integer')
        key = (at, sequence)
        day = at.astimezone(JST).date()
        if self.day is not None and day != self.day:
            raise ValueError('replay one frozen account day per run; use separate runs for separate days')
        identity = event['event_id']
        if not isinstance(identity, str) or not identity:
            raise ValueError('event_id must be a nonempty string')
        line = canonical(event)
        fingerprint = hashlib.sha256(line.encode('utf-8')).hexdigest()
        known = self.seen_digests.get(identity)
        if known is not None:
            if known != fingerprint:
                raise ValueError('conflicting event identity; explicit correction is required')
            return
        if self.last_key is not None and key <= self.last_key:
            raise ValueError('input must be ordered by receive time and unique stable sequence')
        kind = event['type']
        data = json.loads(json.dumps(event.get('data', {})))  # detached copy; input stays unchanged
        apply = self._parse(kind, data, at, identity)
        e = self.engine
        try:
            if self.last_key is not None and at > self.last_key[0]:
                e.flush(self.last_key[0])
            apply()
            if kind not in ('quote', 'trade', 'cumulative_volume', 'feature_snapshot', 'market_snapshot',
                            'requests', 'stream_health', 'quote_stream_health', 'data_reset',
                            'subscription_failed', 'fill', 'account_snapshot', 'exchange_status'):
                e.poll(at)
            self.events_applied += 1
            if self.raw_sink is not None:
                self._write(self.raw_sink, line + '\n')
            else:
                self.raw_lines.append(line)
            self._drain_audit()
            self.seen_digests[identity] = fingerprint
            self.last_key, self.day = key, day
            self.events_processed += 1
            self._input_hash.update(line.encode('utf-8') + b'\n')
            self._finished = False
        except Exception:
            self.failed = True
            raise
        elapsed = time.perf_counter_ns() - started
        timings = self._durations.get(kind)
        if timings is None:
            timings = self._durations[kind] = _DurationHistogram()
        timings.add(elapsed)
        self._wall_ns += elapsed

    @staticmethod
    def _write(sink, text):
        written = sink.write(text)
        if written is not None and written != len(text):
            raise OSError('evidence sink did not accept the complete record')

    def _drain_audit(self):
        if self.audit_sink is not None and self.engine.audit:
            for record in self.engine.audit:
                self._write(self.audit_sink, json.dumps(record, default=encode, ensure_ascii=False) + '\n')
            self.engine.audit.clear()

    def _flush_sinks(self):
        for sink in (self.raw_sink, self.audit_sink):
            if sink is not None and not getattr(sink, 'closed', False):
                flush = getattr(sink, 'flush', None)
                if flush is not None:
                    flush()

    def finish(self):
        """Flush the final receive-time batch (idempotent)."""
        self._require_healthy()
        try:
            if self.last_key is not None and not self._finished:
                self.engine.flush(self.last_key[0])
            self._drain_audit()
            self._flush_sinks()
            self._finished = True
        except Exception:
            self.failed = True
            raise

    def run(self, source):
        self._require_healthy()
        try:
            with Path(source).open(encoding='utf-8') as stream:
                for line_number, line in enumerate(stream, 1):
                    if line.strip():
                        try:
                            self.dispatch(json.loads(line))
                        except (ValueError, KeyError, TypeError, ArithmeticError) as error:
                            raise ValueError(f'{source}:{line_number}: {error}') from error
            self.finish()
            return self.report()
        except Exception:
            self.failed = True
            raise

    # --------------------------------------------------------------- report
    def metrics(self):
        by_type = {kind: {'events': values.count, 'p50_ms': round(values.percentile(.5)/1e6, 4),
                          'p99_ms': round(values.percentile(.99)/1e6, 4),
                          'max_ms': round(values.maximum/1e6, 4)}
                   for kind, values in sorted(self._durations.items())}
        processing = self._wall_ns / 1e9
        return {'processing_seconds': round(processing, 4),
                'events_per_second': round(self.events_processed / processing, 1) if processing else None,
                'by_event_type': by_type,
                'retained': {'event_digests': len(self.seen_digests),
                             'audit_records_in_memory': len(self.engine.audit),
                             'journal_records': len(self.engine.book.journal),
                             'timing_bins': sum(len(values.bins) for values in self._durations.values())},
                'queue_lag': 'NOT_APPLICABLE: offline replay has no live receive clock',
                'note': 'Local dispatch wall-clock timing including evidence writes; excludes input reading, '
                        'final batch/flush and save; not broker or exchange latency. Percentiles are approximate '
                        '(192 logarithmic bins per event type; overflow uses maximum). Digests retain one '
                        'frozen trading day and still grow with unique events.'}

    def manifest(self):
        return {'code_sha256': code_hash(),
                'config_sha256': hashlib.sha256(json.dumps(self.engine.frozen_config, sort_keys=True,
                                                           ensure_ascii=False).encode()).hexdigest(),
                'input_sha256': self._input_hash.hexdigest(),
                'events': self.events_processed}

    def report(self):
        e = self.engine
        book = e.book
        layers, intents = layer_funnel(e)
        positions = {symbol: {'quantity': p.quantity, 'average_price': str(p.average_price),
                              'realized_price_pnl': str(p.realized_pnl), 'actual_fees': str(p.fees)}
                     for symbol, p in book.positions.items()}
        ready = e.subscriptions.ready_candidate_ratio if e.subscriptions is not None else None
        return {'events_processed': self.events_processed, 'mode': 'offline_replay',
                'profile': e.frozen_config.get('profile') if hasattr(e, 'frozen_config') else None,
                'funnel': dict(e.funnel), 'rejections': dict(e.rejections),
                'layer_funnel': layers, 'intents': intents,
                'execution_quality': e.execution_quality.summary(book),
                'alarms': list(e.alarms),
                'disabled_symbols': dict(e.disabled_symbols),
                'enhanced_ready_candidate_ratio': ready,
                'positions': positions, 'active_orders': len(book.active_orders()),
                'daily_net_pnl_estimate': str(e.daily_net_pnl_estimate)
                    if e.daily_net_pnl_estimate is not None else None,
                'unreported_fee_reserve': str(e.unreported_fee_reserve),
                'cash_source': e.valuation.cash_source,
                'risk_locks': sorted(e.risk.lock_reasons),
                'soft_blocks': sorted(e.risk.soft_blocks),
                'requests_sent': dict(book.sent_counts),
                'routine_requests_sent': book.routine_requests_sent,
                'entry_intents_today': e.risk.entry_intents_today,
                'broker_reconciled': book.reconciled,
                'live_readiness': 'NOT_VERIFIED',
                'manifest': self.manifest(),
                'metrics': self.metrics(),
                'interpretation': 'Replay safety evidence only; no profitability or broker execution claim.'}

    def save(self, output_directory, report=None):
        self._require_healthy()
        was_finished = self._finished
        self.finish()
        try:
            return self._save(output_directory, report if was_finished else None)
        except Exception:
            self.failed = True
            raise

    def _save(self, output_directory, report=None):
        path = Path(output_directory).resolve()
        path.mkdir(parents=True, exist_ok=True)
        report = report if report is not None else self.report()
        (path/'frozen-config.json').write_text(json.dumps(self.engine.frozen_config, indent=2,
            ensure_ascii=False), encoding='utf-8')
        if self.raw_sink is None:
            (path/'raw-input.jsonl').write_text(''.join(line + '\n' for line in self.raw_lines), encoding='utf-8')
        if self.audit_sink is None:
            with (path/'audit.jsonl').open('w', encoding='utf-8') as stream:
                for record in self.engine.audit:
                    stream.write(json.dumps(record, default=encode, ensure_ascii=False)+'\n')
        else:
            self._drain_audit()
        # A separate broker ledger aids diagnosis; engine recovery must replay the
        # complete input and frozen config, not just this execution snapshot.
        (path/'execution.json').write_text(json.dumps(self.engine.book.snapshot(), default=encode, indent=2), encoding='utf-8')
        with (path/'execution-journal.jsonl').open('w', encoding='utf-8') as stream:
            for record in self.engine.book.journal:
                stream.write(json.dumps(record, default=encode, ensure_ascii=False)+'\n')
        (path/'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False, default=encode),
                                        encoding='utf-8')
        return path/'report.json'
