"""Read-only IBKR callback normalization; no order or network methods.

Receive clocks are local field clocks, not exchange timestamps. Farm health
does not prove a particular symbol's subscription or continuous trading state.
Every callback is archived before normalization; output failures poison capture.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
from math import isfinite
from time import perf_counter_ns
import uuid

from .domain import aware


class CaptureError(ValueError):
    pass


def _at(value):
    try:
        return aware(datetime.fromisoformat(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise CaptureError('timezone-aware receive time required') from error


def _decimal(value):
    if isinstance(value, bool):
        raise CaptureError('boolean is not a price or size')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise CaptureError('finite decimal required') from error
    if not number.is_finite():
        raise CaptureError('finite decimal required')
    return number


def _write(sink, text):
    written = sink.write(text)
    if written != len(text):
        raise OSError('short callback evidence write')


@dataclass(frozen=True)
class InstrumentMetadata:
    request_id: int
    symbol: str
    contract_id: int
    exchange: str
    currency: str
    known_at: datetime
    primary_exchange: str = 'TSEJ'
    sec_type: str = 'STK'
    local_symbol: str = ''
    benchmark: bool = False
    contract_verified: bool = False
    valid_until: datetime | None = None
    quote_size_multiplier: int | None = None
    tick8_volume_multiplier: int | None = None
    rtvolume_multiplier: int | None = None

    def __post_init__(self):
        aware(self.known_at)
        for name in ('request_id', 'contract_id'):
            if type(getattr(self,name)) is not int or getattr(self,name) <= 0:
                raise CaptureError('positive integral request and contract identities required')
        for name in ('symbol','exchange','currency','primary_exchange','sec_type'):
            if not isinstance(getattr(self,name),str) or not getattr(self,name).strip():
                raise CaptureError(f'{name} is required')
        if type(self.benchmark) is not bool or type(self.contract_verified) is not bool:
            raise CaptureError('metadata verification flags must be boolean')
        if self.valid_until is not None and aware(self.valid_until) <= self.known_at:
            raise CaptureError('metadata expiry must follow publication')
        for name in ('quote_size_multiplier','tick8_volume_multiplier','rtvolume_multiplier'):
            value = getattr(self,name)
            if value is not None and (type(value) is not int or value <= 0):
                raise CaptureError(f'{name} must be a positive integer or unknown')


@dataclass(frozen=True)
class CaptureConfig:
    instruments: tuple[InstrumentMetadata, ...]
    session_id: str = ''
    volume_mode: str = 'tick8'
    quote_source: str = 'L1'
    trade_source: str = 'SAMPLED'
    max_field_age_seconds: float = 2
    max_field_skew_seconds: float = .5
    quote_mode: str = 'strict'

    def __post_init__(self):
        if not isinstance(self.instruments,tuple) or not self.instruments or any(
                not isinstance(item,InstrumentMetadata) for item in self.instruments):
            raise CaptureError('verified instrument metadata tuple required')
        if len({m.request_id for m in self.instruments}) != len(self.instruments):
            raise CaptureError('duplicate request identities')
        if self.volume_mode not in ('tick8','rtvolume','none'):
            raise CaptureError('freeze exactly one cumulative volume source')
        if self.quote_mode not in ('strict', 'incremental'):
            raise CaptureError('quote_mode must be strict or incremental')
        if not self.quote_source or not self.trade_source:
            raise CaptureError('explicit quote and volume source required')
        for value in (self.max_field_age_seconds,self.max_field_skew_seconds):
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not isfinite(value) or value <= 0:
                raise CaptureError('positive finite field ages required')
        if not isinstance(self.session_id,str) or self.session_id and not self.session_id.strip():
            raise CaptureError('session identity must be nonempty text')
        if any(not isinstance(value,str) for value in (self.quote_source,self.trade_source)):
            raise CaptureError('sources must be explicit strings')

    def to_dict(self):
        result = asdict(self)
        for item in result['instruments']:
            item['known_at'] = item['known_at'].isoformat()
            if item['valid_until'] is not None:
                item['valid_until'] = item['valid_until'].isoformat()
        return result

    @classmethod
    def from_dict(cls, document):
        values = dict(document)
        rows = []
        for item in values.pop('instruments'):
            item = dict(item)
            item['known_at'] = _at(item['known_at'])
            if item.get('valid_until') is not None:
                item['valid_until'] = _at(item['valid_until'])
            rows.append(InstrumentMetadata(**item))
        return cls(tuple(rows),**values)


class CallbackRecorder:
    def __init__(self, config, *, events_sink, raw_sink, latency_sink=None):
        self.config = config
        self.events_sink, self.raw_sink, self.latency_sink = events_sink,raw_sink,latency_sink
        if events_sink is raw_sink:
            raise CaptureError('raw and normalized evidence need separate sinks')
        self.session_id = config.session_id or uuid.uuid4().hex
        self.metadata = {m.request_id:m for m in config.instruments}
        self.states = {key:dict(fields={},observed={},data_type=None,halt=None,status=None,total=None,last_price=None,last_price_at=None)
                       for key in self.metadata}
        self.failed = False
        self.callbacks = self.sequence = 0
        self.last_at = None
        self.requires_resubscribe = False
        self.processing_total = self.processing_max = 0

    def _event(self, at, kind, data, output):
        self.sequence += 1
        event = dict(event_id=f'capture:{self.session_id}:{self.sequence}',received_at=at.isoformat(),
                     sequence=self.sequence,type=kind,data=data)
        _write(self.events_sink,json.dumps(event,ensure_ascii=False,allow_nan=False)+'\n')
        output.append(event)

    def _reset(self, request, at, reason, output):
        state,m = self.states[request],self.metadata[request]
        state['fields'].clear()
        state['observed'].clear()
        if reason.startswith('IBKR_') or reason=='NON_REALTIME_DATA':
            state['last_price']=state['last_price_at']=state['total']=None
        self._event(at,'quote_stream_health',dict(symbol=m.symbol,healthy=False,source=self.config.quote_source),output)
        self._event(at,'data_reset',dict(symbol=m.symbol,reason=reason),output)

    def _quote(self, request, at, output):
        state,m = self.states[request],self.metadata[request]
        fields = state['fields']
        if state['data_type'] != 1 or m.quote_size_multiplier is None:
            return
        if any(key not in fields for key in ('bid','ask','bid_size','ask_size')):
            return
        incremental = self.config.quote_mode == 'incremental'
        if incremental:
            if any(key not in state['observed'] for key in ('bid', 'ask')):
                return
            times = [state['observed']['bid'], state['observed']['ask']]
        else:
            times = [stamp for _,stamp in fields.values()]
        if any(not 0 <= (at-stamp).total_seconds() <= self.config.max_field_age_seconds for stamp in times):
            return
        if (max(times)-min(times)).total_seconds() > self.config.max_field_skew_seconds:
            return
        bid,ask = fields['bid'][0],fields['ask'][0]
        if not 0 < bid < ask or min(fields['bid_size'][0],fields['ask_size'][0]) <= 0:
            return
        status = 'UNKNOWN'
        known = state['status']
        if state['halt'] not in (1, 2) and known is not None and known[1] <= at < known[2]:
            status = known[0]
        elif state['halt'] in (1,2):
            status = 'HALT'
        data = dict(symbol=m.symbol,bid=str(bid),ask=str(ask),
            bid_size=fields['bid_size'][0],ask_size=fields['ask_size'][0],
            bid_at=fields['bid'][1].isoformat(),ask_at=fields['ask'][1].isoformat(),
            market_data_type=1,market_status=status,source=self.config.quote_source)
        if incremental:
            data.update(bid_observed_at=times[0].isoformat(), ask_observed_at=times[1].isoformat())
        self._event(at, 'quote', data, output)

    def _volume(self, request, at, raw_total, price, multiplier, output):
        state,m = self.states[request],self.metadata[request]
        if multiplier is None or state['data_type'] != 1 or price is None:
            return
        total = _decimal(raw_total)*multiplier
        if total < 0 or total != total.to_integral_value() or price <= 0:
            self._reset(request,at,'INVALID_VOLUME',output)
            return
        total = int(total)
        if total == state['total']:
            return
        if state['total'] is not None and total < state['total']:
            # A cumulative-counter correction invalidates volume windows, not
            # independently observed bid/ask history.
            self._event(at, 'data_reset', dict(symbol=m.symbol, reason='VOLUME_RESET', scope='volume'), output)
        state['total'] = total
        self._event(at,'cumulative_volume',dict(symbol=m.symbol,total=total,last_price=str(price),
                                                source=self.config.trade_source),output)

    def process(self, callback):
        if self.failed:
            raise CaptureError('capture failed; restart from archived callbacks')
        started = perf_counter_ns()
        output = []
        try:
            _write(self.raw_sink,json.dumps(callback,ensure_ascii=False,allow_nan=False)+'\n')
            at = _at(callback['received_at'])
            if self.last_at is not None and at < self.last_at:
                raise CaptureError('out-of-order callbacks must not be retimestamped')
            self.last_at = at
            self.callbacks += 1
            kind,data = callback['callback'],callback.get('data',{})
            request = callback.get('request_id',-1)
            if kind in ('error','connectionClosed'):
                code = data.get('code')
                if kind == 'connectionClosed' or code in (1100,1101,1102,2103,2104):
                    for identity in self.states:
                        self._reset(identity,at,f'IBKR_{code or "CONNECTION_CLOSED"}',output)
                    if kind == 'connectionClosed' or code == 1100:
                        self._event(at,'disconnect',{},output)
                    if code in (1101,1102):
                        self._event(at,'reconnect',{},output)
                    if code == 1101:
                        self.requires_resubscribe = True
                elif request in self.states:
                    self._reset(request,at,f'IBKR_ERROR_{code}',output)
                return output
            if request not in self.states:
                return output
            m,state = self.metadata[request],self.states[request]
            if at < m.known_at or m.valid_until is not None and at >= m.valid_until:
                return output
            tick = data.get('tick_type')
            if kind == 'marketDataType':
                value = data['market_data_type']
                if type(value) is not int or value not in (1,2,3,4):
                    raise CaptureError('unknown market data type')
                state['data_type'] = value
                if value != 1:
                    self._reset(request,at,'NON_REALTIME_DATA',output)
            elif kind == 'marketStatus':
                status = data['status']
                if status not in ('CONTINUOUS','UNKNOWN','HALT','SPECIAL_QUOTE') or not data.get('source'):
                    raise CaptureError('independent venue status evidence required')
                known,expiry = _at(data['known_at']),_at(data['valid_until'])
                if known > at or expiry <= known:
                    raise CaptureError('invalid venue status availability')
                state['status'] = (status,known,expiry)
                if status != 'CONTINUOUS':
                    self._event(at, 'quote_stream_health', dict(symbol=m.symbol, healthy=False,
                                                               source=self.config.quote_source), output)
            elif kind == 'tickGeneric' and tick == 49:
                halt = _decimal(data['value'])
                if halt not in (-1,0,1,2):
                    raise CaptureError('unknown halted state')
                state['halt'] = int(halt)
                if halt != 0:
                    self._reset(request,at,'HALTED_OR_UNKNOWN',output)
            elif kind == 'tickPrice' and tick in (1,2,4):
                price = _decimal(data['price'])
                if price <= 0:
                    self._reset(request,at,'INVALID_PRICE',output)
                elif tick == 4:
                    state['last_price'] = price
                    state['last_price_at'] = at
                else:
                    field = 'bid' if tick == 1 else 'ask'
                    state['fields'][field] = (price,at)
                    state['fields'].pop(field+'_size',None)
                    state['observed'].pop(field, None)
            elif kind == 'tickSize' and tick in (0,3):
                multiplier = m.quote_size_multiplier
                if multiplier is None:
                    return output
                size = _decimal(data['size'])*multiplier
                if size < 0 or size != size.to_integral_value():
                    self._reset(request,at,'INVALID_QUOTE_SIZE',output)
                else:
                    field = 'bid_size' if tick == 0 else 'ask_size'
                    state['fields'][field] = (int(size),at)
                    side = 'bid' if tick == 0 else 'ask'
                    if size > 0 and side in state['fields']:
                        state['observed'][side] = at
                        known = state['status']
                        if (self.config.quote_mode == 'incremental' and state['data_type'] == 1
                                and state['halt'] not in (1, 2) and known is not None
                                and known[0] == 'CONTINUOUS' and known[1] <= at < known[2]
                                and all(k in state['fields'] for k in ('bid', 'ask', 'bid_size', 'ask_size'))
                                and 0 < state['fields']['bid'][0] < state['fields']['ask'][0]
                                and min(state['fields']['bid_size'][0], state['fields']['ask_size'][0]) > 0):
                            self._event(at, 'quote_stream_health', dict(symbol=m.symbol, healthy=True,
                                                                      source=self.config.quote_source), output)
                    elif size == 0:
                        self._reset(request, at, 'EMPTY_QUOTE_SIDE', output)
                    self._quote(request,at,output)
            elif kind == 'tickSize' and tick == 8 and self.config.volume_mode == 'tick8':
                if state['last_price_at'] is not None and (self.config.quote_mode == 'incremental'
                        or (at-state['last_price_at']).total_seconds()<=self.config.max_field_age_seconds):
                    self._volume(request,at,data['size'],state['last_price'],m.tick8_volume_multiplier,output)
            elif kind == 'tickString' and tick == 48 and self.config.volume_mode == 'rtvolume':
                fields = data['value'].split(';')
                if len(fields) != 6:
                    raise CaptureError('RTVolume requires its six documented fields')
                self._volume(request,at,fields[3],_decimal(fields[0]),m.rtvolume_multiplier,output)
            return output
        except Exception:
            self.failed = True
            raise
        finally:
            elapsed = perf_counter_ns()-started
            self.processing_total += elapsed
            self.processing_max = max(self.processing_max,elapsed)
            if self.latency_sink is not None and not self.failed:
                try:
                    _write(self.latency_sink,json.dumps(dict(callback=self.callbacks,processing_ns=elapsed,
                        clock='LOCAL_PERF_COUNTER',queue='UNKNOWN',send='UNKNOWN',ack='UNKNOWN'))+'\n')
                except Exception:
                    self.failed = True
                    raise

    def finish(self):
        if self.failed:
            raise CaptureError('capture failed; no successful finish')
        try:
            for sink in (self.raw_sink,self.events_sink,self.latency_sink):
                if sink is not None:
                    sink.flush()
        except Exception:
            self.failed = True
            raise
        return self.summary()

    def summary(self):
        unknown = lambda: {'status':'UNKNOWN'}
        return dict(callbacks=self.callbacks,events=self.sequence,failed=self.failed,
            execution_enabled=False,requires_resubscribe=self.requires_resubscribe,
            metadata=self.config.to_dict()['instruments'],
            latency=dict(processing={'status':'MEASURED','samples':self.callbacks,
                'max_ns':self.processing_max,'mean_ns':self.processing_total/max(1,self.callbacks)},
                queue=unknown(),decision=unknown(),send=unknown(),ack=unknown()),
            note='Farm/reference activity is not a per-symbol heartbeat. UNKNOWN venue state blocks strategy entries.')


def record_callbacks(source, *, recorder):
    stream = source
    owned = isinstance(source,(str,Path))
    if owned:
        stream = Path(source).open(encoding='utf-8-sig')
    try:
        for line in stream:
            if line.strip():
                recorder.process(json.loads(line))
        return recorder.finish()
    except Exception:
        recorder.failed = True
        raise
    finally:
        if owned:
            stream.close()
