"""IBKR routing with separate paper and verified-live startup guards.

Every market callback and broker report enters one queue, is timestamped by the
single consumer thread, and is dispatched through ``Replay`` exactly like
offline input, so a paper session's ``raw-input.jsonl`` replays to the same
report. Orders leave only from ``Replay.last_commands`` after a ``requests``
event. Paper routing requires paper ports and DU accounts. Live routing requires
the named real account and the additional checks described below. Each order is
re-checked against the frozen instrument list and quantity cap.

Paper-only assumption: TSE per-symbol halt/status is not delivered on the
default IBKR feed, so during continuous sessions the session asserts
``halt=0`` and a short-lived CONTINUOUS status, labelled
``PAPER_SCHEDULE_ASSERTION`` in the raw callback evidence. That assertion is
never valid for real-money trading.

Live opt-in additionally requires verified non-demo parameters, broker JPY
funds at a reconciliation barrier, and an external expiring venue-status file.
The paper schedule assertion is never used for a live account.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from dataclasses import replace
from uuid import uuid4

from .market import JST

PAPER_PORTS = (4002, 7497)
PAPER_ACCOUNT_PREFIX = 'DU'
STATUS_SOURCE = 'PAPER_SCHEDULE_ASSERTION'
CONTINUOUS = ((9 * 3600, 11 * 3600 + 1800), (12 * 3600 + 1800, 15 * 3600 + 1500))
REQUEST_INTERVAL = 1.0
ORDER_ID_FLOOR = 1000  # keeps order IDs disjoint from market-data request IDs
STATUS_TTL = 3
LIVE_MAX_DAILY_LOSS = 3000
FUNDS_REQUEST_ID = 9002
STATUS_INTERVAL = 30


class PaperGuardError(RuntimeError):
    pass


def execution_time(value, default_timezone=None):
    """Parse broker time without guessing the TWS login timezone."""
    if not value:
        return None
    try:
        at = datetime.fromisoformat(value)
        if at.tzinfo is not None:
            return at
    except ValueError:
        pass
    parts = value.split()
    try:
        if len(parts) == 1 and '-' in value:
            return datetime.strptime(value, '%Y%m%d-%H:%M:%S').replace(tzinfo=timezone.utc)
        zone = parts[2] if len(parts) == 3 else default_timezone
        if len(parts) not in (2, 3) or not zone:
            return None  # receipt time remains explicit; exchange time is unknown
        tz = JST if zone in ('Asia/Tokyo', 'JST', 'Japan') else timezone.utc if zone in ('UTC', 'GMT') else ZoneInfo(zone)
        return datetime.strptime(' '.join(parts[:2]), '%Y%m%d %H:%M:%S').replace(tzinfo=tz)
    except (ValueError, KeyError) as error:
        raise PaperGuardError(f'invalid broker execution time {value!r}') from error


def read_market_status(path, at, symbols):
    """Read an atomically published external status document; no fabricated facts."""
    from .domain import aware
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    for fact in (document, *document.get('symbols', [])):
        source = fact.get('source', '')
        if (not isinstance(source, str) or not source.strip()
                or any(word in source.upper() for word in ('PAPER', 'ARTIFICIAL', 'SYNTHETIC', 'OPERATOR', 'DEMO'))):
            raise PaperGuardError('live venue facts require an external authoritative source')
        known = aware(datetime.fromisoformat(fact['known_at']))
        expiry = aware(datetime.fromisoformat(fact['valid_until']))
        if not known <= at < expiry:
            raise PaperGuardError('live venue facts are future or expired')
    if document['day'] != at.astimezone(JST).date().isoformat() or type(document.get('is_open')) is not bool:
        raise PaperGuardError('live calendar must explicitly describe the current day')
    rows = document.get('symbols', [])
    if len({r['symbol'] for r in rows}) != len(rows) or {r['symbol'] for r in rows} != set(symbols):
        raise PaperGuardError('live venue status must cover the exact frozen universe and benchmark')
    if any(r['status'] not in ('CONTINUOUS', 'UNKNOWN', 'HALT', 'SPECIAL_QUOTE') for r in rows):
        raise PaperGuardError('invalid external venue status')
    return document


def validate_universe(engine, capture):
    expected = set(engine.instruments) | {engine.features.benchmark_symbol} - {None}
    actual = [m.symbol for m in capture.instruments]
    if len(set(actual)) != len(actual) or set(actual) != expected:
        raise PaperGuardError(f'capture/strategy universe mismatch; missing={sorted(expected-set(actual))}, '
                             f'extra={sorted(set(actual)-expected)}')
    if any(not m.contract_verified for m in capture.instruments):
        raise PaperGuardError('every capture contract must be verified before connecting')
    if {m.symbol for m in capture.instruments if m.benchmark} != {engine.features.benchmark_symbol}:
        raise PaperGuardError('capture benchmark differs from strategy benchmark')
    if engine.features.config.sampled_subscription_coverage and capture.quote_mode != 'incremental':
        raise PaperGuardError('sampled subscription coverage requires incremental quote evidence')
    return expected


def check_live_configuration(engine, preamble, status_file, at):
    check_live_limits(engine.risk.config)
    if engine.frozen_config['profile'] == 'demo':
        raise PaperGuardError('live routing refuses demo parameters and artificial calibration; supply verified research/shadow config')
    engine.parameter_guard.validate_as_of(at)
    if not engine.risk.config.require_account_snapshot:
        raise PaperGuardError('live routing requires broker account snapshots')
    labels = [engine.config.model_version, engine.commissions.version]
    labels.extend(entry.get('source', '') for entry in engine.frozen_config.get('provenance', {}).values())
    if any(marker in label.upper() for label in labels
           for marker in ('SYNTHETIC', 'ILLUSTRATIVE', 'ARTIFICIAL', 'DEMO', 'FIXTURE')):
        raise PaperGuardError('live routing refuses synthetic models, illustrative fees and fixture provenance')
    if engine.config.economics_source != 'calibration':
        raise PaperGuardError('live routing requires verified replay calibration')
    if not any(kind == 'calibration_table' for kind, _ in preamble):
        raise PaperGuardError('live routing requires a calibration table')
    from .economics import CalibrationRow, CalibrationTable, policy_fingerprint
    for kind, data in preamble:
        if kind == 'calibration_table':
            rows = []
            for raw in data['rows']:
                row = dict(raw)
                for key in ('mean_net_amount', 'lower_net_amount'):
                    row[key] = Decimal(str(row[key]))
                rows.append(CalibrationRow(**row))
            table = CalibrationTable(rows, known_at=datetime.fromisoformat(data['known_at']), version=data['version'])
            if table.known_at > at:
                raise PaperGuardError('live calibration table is not yet available')
            table.validate_for_profile(engine.frozen_config['profile'],
                min_independent_days=engine.config.min_independent_days,
                policy_hash=policy_fingerprint(engine.frozen_config), fee_version=engine.commissions.version, as_of=at)
    missing = missing_profiles(engine, preamble)
    if missing:
        raise PaperGuardError(f'live preamble is missing independently supported profiles: {missing}')
    if not status_file:
        raise PaperGuardError('live routing requires --status-file with expiring external calendar and symbol facts')
    read_market_status(status_file, at, set(engine.instruments) | {engine.features.benchmark_symbol} - {None})


def missing_profiles(engine, preamble):
    available = {(data['symbol'], data['kind'], data['window_seconds'])
                 for kind, data in preamble if kind == 'same_time_profile'
                 and data['valid_days'] >= engine.features.config.rvol_min_days}
    required = {(s, 'volume', w) for s in engine.instruments for w in engine.features._rvol_windows}
    if engine.config.market_source == 'internal':
        required.add((engine.features.benchmark_symbol, 'volatility',
                      engine.features.config.market_volatility_window_seconds))
    return sorted(required - available)


def check_paper_target(port, accounts, live_account=None):
    """Refuse anything that is not provably an IBKR paper account (or the named live one)."""
    if live_account is not None:
        if (port not in (4001, 7496) or not isinstance(live_account, str)
                or not live_account.startswith('U')):
            raise PaperGuardError('live opt-in requires a real U* account on port 4001/7496')
        if live_account not in accounts:
            raise PaperGuardError(f'live account {live_account} is not managed by this gateway {accounts}')
        return
    if port not in PAPER_PORTS:
        raise PaperGuardError(f'port {port} is not a paper port {PAPER_PORTS}; refusing to route orders')
    if not accounts or any(not a.startswith(PAPER_ACCOUNT_PREFIX) for a in accounts):
        raise PaperGuardError(f'managed accounts {accounts} are not all paper (DU*) accounts; refusing')


def in_continuous_session(at):
    local = at.astimezone(JST)
    second = local.hour * 3600 + local.minute * 60 + local.second
    return local.weekday() < 5 and any(start <= second < end for start, end in CONTINUOUS)


def order_spec(payload, *, symbols, max_quantity):
    """Translate an engine SUBMIT payload to plain limit-order fields."""
    symbol, side, quantity = payload['symbol'], str(payload['side']), payload['quantity']
    if symbol not in symbols:
        raise PaperGuardError(f'order symbol {symbol} is not a frozen instrument')
    if side not in ('BUY', 'SELL'):
        raise PaperGuardError(f'unknown order side {side}')
    if type(quantity) is not int or not 0 < quantity <= max_quantity:
        raise PaperGuardError(f'order quantity {quantity} outside 1..{max_quantity}')
    limit = Decimal(str(payload['limit_price']))
    if not limit.is_finite() or limit <= 0:
        raise PaperGuardError('limit price must be positive')
    return dict(order_id=payload['order_id'], symbol=symbol, action=side,
                quantity=quantity, limit_price=float(limit))


class PaperSession:
    """Broker-agnostic core: sequencing, dispatch and command extraction."""

    def __init__(self, replay, recorder, *, session_id, symbols, max_quantity,
                 live=False, status_file=None):
        self.replay, self.recorder = replay, recorder
        self.session_id, self.symbols, self.max_quantity = session_id, set(symbols), max_quantity
        self.sequence = 0
        self.last_at = None
        self.live, self.status_file = live, status_file
        self.market_fact_error = None

    def now(self, at=None):
        at = at or datetime.now(timezone.utc)
        if self.last_at is not None and at < self.last_at:
            at = self.last_at  # the consumer clock never runs backwards
        self.last_at = at
        return at

    def dispatch(self, kind, data, at, *, broker_report=None):
        self.sequence += 1
        event = dict(event_id=f'paper-live:{self.session_id}:{self.sequence}',
                                  received_at=at.isoformat(), sequence=self.sequence,
                                  type=kind, data=data)
        if broker_report is not None:
            event.update(execution_source='BROKER_REPORTS',
                         source_record_id=f'ibkr:{self.session_id}:{self.sequence}', broker_report=broker_report)
        self.replay.dispatch(event)

    def market(self, callback, at):
        callback = dict(callback, received_at=at.isoformat())
        for event in self.recorder.process(callback):
            symbol = event['data'].get('symbol')
            if symbol is not None and symbol not in self.symbols:
                continue
            self.dispatch(event['type'], event['data'], at)

    def assert_status(self, at):
        """Paper-only halt/status assertion during scheduled continuous sessions."""
        if self.live:
            try:
                document = read_market_status(self.status_file, at, self.symbols)
                self.market_fact_error = None
                calendar = {k: document[k] for k in ('day', 'is_open', 'known_at', 'source')}
                self.dispatch('calendar', calendar, at)
                facts = {r['symbol']: r for r in document['symbols']}
                for request, metadata in self.recorder.metadata.items():
                    row = facts[metadata.symbol]
                    self.market(dict(callback='marketStatus', request_id=request,
                        data={k: row[k] for k in ('status', 'source', 'known_at', 'valid_until')}), at)
            except (OSError, ValueError, KeyError, PaperGuardError) as error:
                self.market_fact_error = str(error)
                for metadata in self.recorder.metadata.values():
                    self.recorder.states[metadata.request_id]['status'] = None
                    self.dispatch('quote_stream_health', dict(symbol=metadata.symbol, healthy=False,
                                                             source=self.recorder.config.quote_source), at)
            return
        if not in_continuous_session(at):
            return
        until = at + timedelta(seconds=STATUS_TTL)
        for request in self.recorder.metadata:
            for callback in (
                    dict(callback='tickGeneric', request_id=request, synthetic=STATUS_SOURCE,
                         data=dict(tick_type=49, value=0)),
                    dict(callback='marketStatus', request_id=request, synthetic=STATUS_SOURCE,
                         data=dict(status='CONTINUOUS', source=STATUS_SOURCE,
                                   known_at=at.isoformat(), valid_until=until.isoformat()))):
                self.market(callback, at)

    def requests(self, at):
        self.dispatch('requests', {}, at)
        orders = []
        for command in self.replay.last_commands:
            if command.kind == 'SUBMIT':
                orders.append(('SUBMIT', order_spec(command.payload, symbols=self.symbols,
                                                    max_quantity=self.max_quantity)))
            else:
                orders.append((command.kind, dict(order_id=command.order_id)))
        return orders

    def reconcile(self, data, at):
        data = dict(data)
        funds = data.pop('funds', None)
        if data.pop('changed_during_funds', False):
            raise PaperGuardError('broker orders changed during funds query; a new barrier is required')
        data['next_order_id'] = max(data.get('next_order_id') or 1, ORDER_ID_FLOOR)
        self.dispatch('reconcile', dict(data, complete=True, ownership_confirmed=True), at,
                      broker_report=data)
        book = self.replay.engine.book
        if not book.reconciled:
            raise PaperGuardError(f'broker reconciliation failed: {sorted(book.lock_reasons)}')
        if self.live:
            if (not funds or set(funds) != {'AvailableFunds', 'NetLiquidation'}):
                raise PaperGuardError('broker JPY funds are missing or have an unsupported base currency')
            self.dispatch('account_snapshot', dict(account_id=self.live, currency='JPY',
                available_funds=funds['AvailableFunds'], net_liquidation=funds['NetLiquidation'],
                source='IBKR_ACCOUNT_SUMMARY_JPY', ledger_sequence=book.journal[-1]['sequence'],
                covered_order_ids=[o.order_id for o in book.active_orders()
                                   if str(o.side) == 'BUY' and o.submitted_at is not None]), at)
            self.dispatch('account_refresh', dict(active=False), at)

    def flush_evidence(self):
        self.replay._flush_sinks()
        for sink in (self.recorder.raw_sink, self.recorder.events_sink):
            sink.flush()

    def status(self, at):
        engine = self.replay.engine
        return dict(at=at.isoformat(), market=str(engine.market_state),
            market_reason=engine.market_snapshot.reason if engine.market_snapshot else 'NO_MARKET_SNAPSHOT',
            cash_source=engine.valuation.cash_source,
            market_fact_error=self.market_fact_error,
            soft_blocks=sorted(engine.risk.soft_blocks), risk_locks=sorted(engine.risk.lock_reasons),
            funnel=dict(engine.funnel), orders_sent=dict(engine.book.sent_counts),
            features={s: dict(valid=v.valid, reason=v.reason) for s, v in engine.snapshots.items()},
            quote_spans={s: round((v.quotes[-1].at-v.quotes[0].at).total_seconds(), 3)
                         for s, v in engine.features._series.items() if v.quotes})


class IbkrPaperGateway:
    """Thin ibapi adapter; all callbacks go to one queue as (kind, data)."""

    def __init__(self, capture_config, *, host, port, client_id, queue, live_account=None,
                 execution_timezone=None):
        from ibapi.client import EClient
        from ibapi.contract import Contract
        from ibapi.execution import ExecutionFilter
        from ibapi.order import Order
        from ibapi.wrapper import EWrapper
        self._Contract, self._Order, self._Filter = Contract, Order, ExecutionFilter
        self.config, self.host, self.port, self.client_id = capture_config, host, port, client_id
        self.queue, self.live_account = queue, live_account
        self.accounts, self.next_id = [], None
        self.ready = Event()
        self.contracts = {m.symbol: m for m in capture_config.instruments}
        self.snapshot = None
        self.sent_order_ids = set()
        self.activity_revision = 0
        self.execution_timezone = execution_timezone
        gateway = self

        class Client(EWrapper, EClient):
            def __init__(self):
                EWrapper.__init__(self)
                EClient.__init__(self, self)

            def managedAccounts(self, accountsList):
                gateway.accounts = [a for a in accountsList.split(',') if a]
                if gateway.next_id is not None:
                    gateway.ready.set()

            def nextValidId(self, orderId):
                gateway.next_id = orderId
                if gateway.accounts:
                    gateway.ready.set()

            def error(self, reqId, *args):
                code, text = (args[1], args[2]) if len(args) >= 3 and isinstance(args[1], int) else (args[0], args[1])
                gateway.queue.put(('error', dict(request_id=reqId, code=code, message=str(text))))

            def connectionClosed(self):
                gateway.queue.put(('connectionClosed', {}))

            def tickPrice(self, reqId, tickType, price, attrib):
                gateway.queue.put(('market', dict(callback='tickPrice', request_id=reqId,
                                                  data=dict(tick_type=tickType, price=price))))

            def tickSize(self, reqId, tickType, size):
                gateway.queue.put(('market', dict(callback='tickSize', request_id=reqId,
                                                  data=dict(tick_type=tickType, size=str(size)))))

            def tickString(self, reqId, tickType, value):
                gateway.queue.put(('market', dict(callback='tickString', request_id=reqId,
                                                  data=dict(tick_type=tickType, value=value))))

            def tickGeneric(self, reqId, tickType, value):
                gateway.queue.put(('market', dict(callback='tickGeneric', request_id=reqId,
                                                  data=dict(tick_type=tickType, value=value))))

            def marketDataType(self, reqId, marketDataType):
                gateway.queue.put(('market', dict(callback='marketDataType', request_id=reqId,
                                                  data=dict(market_data_type=marketDataType))))

            def orderStatus(self, orderId, status, filled, remaining, *rest):
                gateway.activity_revision += 1
                gateway.queue.put(('status', dict(order_id=orderId, ib_status=status,
                                                  filled=int(filled), remaining=int(remaining))))

            def openOrder(self, orderId, contract, order, orderState):
                if gateway.snapshot is not None:
                    if 'funds_revision' in gateway.snapshot:
                        gateway.activity_revision += 1
                    gateway.snapshot['open_orders'].append(dict(
                        order_id=orderId, symbol=contract.symbol, side=order.action,
                        quantity=int(order.totalQuantity), status=orderState.status))

            def openOrderEnd(self):
                gateway._snapshot_part('orders')

            def position(self, account, contract, position, avgCost):
                if (gateway.snapshot is not None and contract.symbol in gateway.contracts
                        and account == (gateway.live_account or gateway.accounts[0])):
                    if ('funds_revision' in gateway.snapshot
                            and gateway.snapshot['positions'].get(contract.symbol, 0) != int(position)):
                        gateway.activity_revision += 1
                    if position:
                        gateway.snapshot['positions'][contract.symbol] = int(position)
                    else:
                        gateway.snapshot['positions'].pop(contract.symbol, None)

            def positionEnd(self):
                gateway._snapshot_part('positions')

            def execDetails(self, reqId, contract, execution):
                account = getattr(execution, 'acctNumber', gateway.live_account)
                if gateway.live_account and account != gateway.live_account:
                    return
                row = dict(exec_id=execution.execId, order_id=execution.orderId,
                           qty=int(execution.shares), price=str(execution.price),
                           side=execution.side, symbol=contract.symbol,
                           at=datetime.now(timezone.utc).isoformat(),
                           account=account, broker_time=getattr(execution, 'time', ''))
                try:
                    executed_at = execution_time(row['broker_time'], gateway.execution_timezone)
                except PaperGuardError as error:
                    gateway.queue.put(('adapter_error', dict(reason=str(error), broker_report=row)))
                    return
                if executed_at is not None:
                    row['executed_at'] = executed_at.isoformat()
                    row['received_at'] = row['at']
                    row['at'] = executed_at.isoformat()
                if reqId == -1:
                    gateway.activity_revision += 1
                    gateway.queue.put(('fill', row))
                elif gateway.snapshot is not None:
                    gateway.snapshot['executions'].append(row)

            def execDetailsEnd(self, reqId):
                gateway._snapshot_part('executions')

            def commissionReport(self, commissionReport):
                gateway.queue.put(('commission', dict(exec_id=commissionReport.execId,
                                                      amount=str(commissionReport.commission),
                                                      currency=getattr(commissionReport, 'currency', 'JPY'))))

            def accountSummary(self, reqId, account, tag, value, currency):
                if (reqId == FUNDS_REQUEST_ID and gateway.snapshot is not None
                        and account == gateway.live_account and currency == 'JPY'
                        and tag in ('AvailableFunds', 'NetLiquidation')):
                    gateway.snapshot['funds'][tag] = str(value)

            def accountSummaryEnd(self, reqId):
                if reqId == FUNDS_REQUEST_ID:
                    gateway._snapshot_part('funds')

        self.client = Client()

    def connect(self, timeout=15):
        self.client.connect(self.host, self.port, self.client_id)
        Thread(target=self.client.run, name='ibkr-paper-reader', daemon=True).start()
        if not self.ready.wait(timeout):
            raise PaperGuardError('paper gateway did not report accounts and next order ID')
        check_paper_target(self.port, self.accounts, self.live_account)

    def subscribe(self):
        self.client.reqMarketDataType(1)
        for m in self.config.instruments:
            self.client.reqMktData(m.request_id, self.contract(m.symbol),
                                   '233' if self.config.volume_mode == 'rtvolume' else '', False, False, [])

    def contract(self, symbol):
        m = self.contracts[symbol]
        c = self._Contract()
        c.conId, c.symbol, c.secType, c.currency = m.contract_id, m.symbol, m.sec_type, m.currency
        c.exchange, c.primaryExchange = m.exchange, m.primary_exchange
        return c

    def place(self, spec):
        check_paper_target(self.port, self.accounts, self.live_account)
        o = self._Order()
        o.action, o.orderType, o.tif = spec['action'], 'LMT', 'DAY'
        o.totalQuantity, o.lmtPrice = spec['quantity'], spec['limit_price']
        o.account = self.live_account or self.accounts[0]
        o.outsideRth = False
        o.eTradeOnly = o.firmQuoteOnly = False
        self.sent_order_ids.add(spec['order_id'])  # retain ownership even if the send outcome is ambiguous
        self.client.placeOrder(spec['order_id'], self.contract(spec['symbol']), o)

    def cancel(self, order_id):
        if order_id not in self.sent_order_ids:
            raise PaperGuardError(f'refusing to cancel unowned order {order_id}')
        self.client.cancelOrder(order_id)

    def start_snapshot(self):
        """Request one strategy-scoped barrier; completion arrives as ('reconcile', data)."""
        if self.snapshot is not None:
            return
        self.snapshot = dict(open_orders=[], positions={}, executions=[], parts=set(), funds={}, started=monotonic())
        self.client.reqOpenOrders()
        self.client.reqPositions()
        f = self._Filter()
        f.clientId = self.client_id
        if self.live_account:
            f.acctCode = self.live_account
        self.client.reqExecutions(9001, f)

    def _snapshot_part(self, part):
        if self.snapshot is None:
            return
        self.snapshot['parts'].add(part)
        if self.snapshot['parts'] >= {'orders', 'positions', 'executions'}:
            if self.live_account and 'funds' not in self.snapshot['parts']:
                if 'funds_revision' not in self.snapshot:
                    self.snapshot['funds_revision'] = self.activity_revision
                    self.client.reqAccountSummary(FUNDS_REQUEST_ID, 'All', 'AvailableFunds,NetLiquidation')
                return
            snap, self.snapshot = self.snapshot, None
            self.client.cancelPositions()
            if self.live_account:
                self.client.cancelAccountSummary(FUNDS_REQUEST_ID)
            symbols = set(self.contracts)
            self.queue.put(('reconcile', dict(
                positions=snap['positions'],
                open_orders=[r for r in snap['open_orders'] if r['symbol'] in symbols],
                executions=[r for r in snap['executions'] if r['symbol'] in symbols],
                next_order_id=self.next_id,
                **(dict(funds=snap['funds'], changed_during_funds=
                         self.activity_revision != snap['funds_revision']) if self.live_account else {}))))

    def disconnect(self):
        for m in self.config.instruments:
            self.client.cancelMktData(m.request_id)
        self.client.disconnect()


def check_live_limits(risk):
    """Real-money sessions run with one position and a small daily loss cap."""
    if risk.max_positions != 1:
        raise PaperGuardError('live session requires risk.max_positions == 1')
    if risk.capital * risk.daily_loss_fraction > LIVE_MAX_DAILY_LOSS:
        raise PaperGuardError(f'live session requires a daily loss limit <= {LIVE_MAX_DAILY_LOSS} JPY')


def preamble_events(source_events):
    """Copy frozen pre-open calibration and same-time profiles from a prior day's input."""
    rows = []
    with open(source_events, encoding='utf-8') as handle:
        for line in handle:
            event = json.loads(line)
            if event['type'] in ('quote', 'requests', 'cumulative_volume'):
                break
            if event['type'] in ('calibration_table', 'same_time_profile'):
                rows.append((event['type'], event['data']))
    if not any(kind == 'calibration_table' for kind, _ in rows):
        raise ValueError('preamble source has no calibration table')
    return rows


def run_paper(session, gateway, queue, *, until, preamble, flatten_seconds=120, log=print):
    """Single consumer loop. Returns when ``until`` passes and the book is flat."""
    at = session.now()
    if session.live:
        document = read_market_status(session.status_file, at, session.symbols)
        session.dispatch('calendar', {k: document[k] for k in ('day', 'is_open', 'known_at', 'source')}, at)
    else:
        session.dispatch('calendar', dict(day=at.astimezone(JST).date().isoformat(), is_open=True,
                                          known_at=at.isoformat(), source='OPERATOR_ASSERTED_PAPER_SESSION'), at)
    gateway.start_snapshot()
    reconciled = False
    while not reconciled:
        kind, data = queue.get(timeout=30)
        if kind == 'reconcile':
            if data.get('changed_during_funds'):
                gateway.start_snapshot()
                continue
            if data['positions'] or data['open_orders'] or data['executions']:
                raise PaperGuardError('new session requires a flat strategy book and no prior executions '
                                      'for this client ID; restore the prior journal before resuming')
            session.reconcile(data, session.now())
            reconciled = True
        elif kind == 'adapter_error':
            raise PaperGuardError(data['reason'])
        elif kind == 'error' and data['code'] not in (2104, 2106, 2158, 2107, 2119):
            log(f'startup error {data}')
    for kind, data in preamble:
        session.dispatch(kind, data, session.now())
    gateway.subscribe()
    next_tick, stop_at, killed = monotonic(), None, False
    next_status, next_funds = next_tick, next_tick + 15
    fills, commissions = set(), {}
    while True:
        try:
            kind, data = queue.get(timeout=max(0.0, next_tick - monotonic()))
        except Empty:
            kind = None
        if kind == 'market':
            session.market(data, session.now())
        elif kind in ('status', 'fill'):
            row = dict(data)
            if kind == 'fill':
                if row['order_id'] not in session.replay.engine.book.orders:
                    raise PaperGuardError(f'unowned execution received: {data}')
                for key in ('side', 'symbol', 'at', 'received_at', 'account', 'broker_time'):
                    row.pop(key, None)
                fills.add(row['exec_id'])
            elif row['order_id'] not in session.replay.engine.book.orders:
                continue  # another client's order on the same paper account
            session.dispatch(kind, row, session.now(), broker_report=data)
            if kind == 'fill' and row['exec_id'] in commissions:
                pending = commissions.pop(row['exec_id'])
                session.dispatch('commission', {k: pending[k] for k in ('exec_id', 'amount')},
                                 session.now(), broker_report=pending)
            log(f'{kind} {data}')
        elif kind == 'commission':
            if data.get('currency', 'JPY') != 'JPY':
                raise PaperGuardError(f'commission currency is not JPY: {data}')
            if data['exec_id'] in fills:
                session.dispatch('commission', {k: data[k] for k in ('exec_id', 'amount')},
                                 session.now(), broker_report=data)
                log(f'commission {data}')
            else:
                commissions[data['exec_id']] = data  # report can precede its execution
        elif kind == 'reconcile':
            if data.get('changed_during_funds'):
                gateway.start_snapshot()
                continue
            fills.update(row['exec_id'] for row in data['executions'])
            session.reconcile(data, session.now())
            for exec_id in list(commissions):
                if exec_id in fills:
                    pending = commissions.pop(exec_id)
                    session.dispatch('commission', {k: pending[k] for k in ('exec_id', 'amount')},
                                     session.now(), broker_report=pending)
        elif kind == 'error':
            code, request = data['code'], data['request_id']
            if request in session.recorder.metadata or code in (1100, 1101, 1102, 2103, 2104):
                session.market(dict(callback='error', request_id=request, data=dict(code=code)), session.now())
                if code == 1101:
                    gateway.subscribe()
                    session.recorder.requires_resubscribe = False
                if code in (1101, 1102):
                    gateway.start_snapshot()
            elif request >= ORDER_ID_FLOOR:
                if code == 201:
                    session.dispatch('reject', dict(order_id=request, reason=f'IBKR 201: {data["message"]}'),
                                     session.now())
                elif code not in (202, 399):
                    gateway.start_snapshot()  # outcome unknown: reconcile at a barrier
                log(f'order error {data}')
            elif code not in (2104, 2106, 2158, 2107, 2119, 2100):
                log(f'error {data}')
        elif kind == 'connectionClosed':
            session.market(dict(callback='connectionClosed', data={}), session.now())
            raise PaperGuardError('gateway connection closed')
        elif kind == 'adapter_error':
            raise PaperGuardError(data['reason'])
        if monotonic() >= next_tick:
            next_tick = monotonic() + REQUEST_INTERVAL
            at = session.now()
            session.assert_status(at)
            snapshot = getattr(gateway, 'snapshot', None)
            if snapshot is not None and monotonic() - snapshot.get('started', monotonic()) > 25:
                raise PaperGuardError('broker reconciliation/funds query timed out')
            if session.live and monotonic() >= next_funds and getattr(gateway, 'snapshot', None) is None:
                next_funds = monotonic() + 15
                session.dispatch('account_refresh', dict(active=True), at)
                gateway.start_snapshot()
            if not killed and at >= until:
                session.dispatch('kill_switch', dict(reason='PAPER_SESSION_END'), at)
                killed, stop_at = True, monotonic() + flatten_seconds
                log('session end: kill switch dispatched, flattening')
            for command, spec in session.requests(at):
                if command == 'SUBMIT':
                    gateway.place(spec)
                    log(f'SUBMIT {spec}')
                elif command == 'CANCEL':
                    gateway.cancel(spec['order_id'])
                    log(f'CANCEL {spec}')
                elif command == 'QUERY':
                    gateway.start_snapshot()
            session.flush_evidence()
            if monotonic() >= next_status:
                next_status = monotonic() + STATUS_INTERVAL
                status = session.status(at)
                log('STATUS ' + json.dumps(status, ensure_ascii=False))
                if getattr(session, 'output', None) is not None:
                    temporary = session.output/'status.tmp'
                    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
                    temporary.replace(session.output/'status.json')
            if killed:
                book = session.replay.engine.book
                flat = not book.active_orders() and not any(p.quantity for p in book.positions.values())
                if flat or monotonic() >= stop_at:
                    return flat


def cancel_owned_orders(session, gateway, log=print):
    """Best-effort cleanup of this session's submitted orders; never global cancel."""
    owned = getattr(gateway, 'sent_order_ids', set())
    result = dict(at=datetime.now(timezone.utc).isoformat(), attempted=[], errors=[], outcomes_verified=False,
                  residual_positions={s:p.quantity for s,p in session.replay.engine.book.positions.items() if p.quantity})
    for order in session.replay.engine.book.active_orders():
        if order.order_id in owned:
            result['attempted'].append(order.order_id)
            try:
                gateway.cancel(order.order_id)
                log(f'cleanup CANCEL owned order {order.order_id}')
            except Exception as error:
                result['errors'].append(dict(order_id=order.order_id, error=repr(error)))
                log(f'cleanup cancel failed for {order.order_id}: {error}')
    return result


def paper_command(args, log=print):
    """CLI entry: build engine, connect to the paper gateway, run, save evidence."""
    from pathlib import Path
    from .config import build_engine, load_config
    from .recording import CallbackRecorder, CaptureConfig
    from .replay import Replay, code_hash, encode
    live = getattr(args, 'live_account', None)
    if live is None and args.port not in PAPER_PORTS:
        raise PaperGuardError(f'port {args.port} is not a paper port {PAPER_PORTS}')
    output = Path(args.output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('paper session needs a new or empty output directory')
    engine = build_engine(load_config(args.config))
    capture = CaptureConfig.from_dict(json.loads(Path(args.capture_config).read_text(encoding='utf-8')))
    capture = replace(capture, session_id=f'{capture.session_id or "ibkr"}-{uuid4().hex}')
    preamble = preamble_events(args.preamble)
    symbols = validate_universe(engine, capture)
    if live is not None:
        check_live_configuration(engine, preamble, getattr(args, 'status_file', None), datetime.now(timezone.utc))
    elif missing_profiles(engine, preamble):
        log(f'paper baseline coverage missing (entries remain blocked for these features): {missing_profiles(engine, preamble)}')
    hour, minute = (int(x) for x in args.until.split(':'))
    today = datetime.now(JST)
    until = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if until <= today:
        raise PaperGuardError('--until must be later than the current JST time')
    max_quantity = engine.risk.config.max_quantity
    queue = Queue(maxsize=100000)
    output.mkdir(parents=True, exist_ok=True)
    (output/'frozen-config.json').write_text(json.dumps(engine.frozen_config, ensure_ascii=False, indent=2), encoding='utf-8')
    (output/'capture-config.json').write_text(json.dumps(capture.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
    (output/'session-manifest.json').write_text(json.dumps(dict(code_sha256=code_hash(),
        mode='live' if live else 'paper', status_file=getattr(args, 'status_file', None),
        execution_timezone=getattr(args, 'execution_timezone', None),
        created_at=datetime.now(timezone.utc).isoformat()), indent=2), encoding='utf-8')
    with (output/'raw-input.jsonl').open('w', encoding='utf-8', buffering=1) as raw, \
            (output/'audit.jsonl').open('w', encoding='utf-8', buffering=1) as audit, \
            (output/'callbacks.jsonl').open('w', encoding='utf-8') as callbacks, \
            (output/'capture-events.jsonl').open('w', encoding='utf-8') as captured:
        replay = Replay(engine, raw_sink=raw, audit_sink=audit)
        recorder = CallbackRecorder(capture, events_sink=captured, raw_sink=callbacks)
        session = PaperSession(replay, recorder, session_id=recorder.session_id,
                               symbols=symbols, max_quantity=max_quantity,
                               live=live or False, status_file=getattr(args, 'status_file', None))
        session.output = output
        gateway = IbkrPaperGateway(capture, host=args.host, port=args.port,
                                   client_id=args.client_id, queue=queue, live_account=live,
                                   execution_timezone=getattr(args, 'execution_timezone', None))
        flat = False
        try:
            gateway.connect()
            log(f'{"LIVE account " + live if live else "paper"} accounts {gateway.accounts}; next order id {gateway.next_id}; running until {until:%H:%M} JST')
            flat = run_paper(session, gateway, queue, until=until, preamble=preamble, log=log)
            replay.finish()
        except (Exception, KeyboardInterrupt) as error:
            log('session failed: cancelling owned orders; checking residual positions')
            try:
                (output/'failure.json').write_text(json.dumps(dict(error=repr(error),
                    at=datetime.now(timezone.utc).isoformat(), replay_failed=replay.failed,
                    status=session.status(session.now()), execution=engine.book.snapshot()),
                    default=encode, ensure_ascii=False, indent=2), encoding='utf-8')
            except Exception as evidence_error:
                error.add_note(f'failure evidence could not be saved: {evidence_error}')
            raise
        finally:
            if not flat:
                cleanup = cancel_owned_orders(session, gateway, log)
                try:
                    (output/'cleanup.json').write_text(json.dumps(cleanup, ensure_ascii=False, indent=2), encoding='utf-8')
                except OSError as evidence_error:
                    log(f'cleanup evidence write failed: {evidence_error}')
            try:
                session.flush_evidence()
            finally:
                gateway.disconnect()
        report = replay.report() if not replay.failed else None
    if report is not None:
        replay.save(output, report=report)
        log(f'flat={flat}; report {output/"report.json"}')
    return flat
