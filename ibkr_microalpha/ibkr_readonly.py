"""Optional official-ibapi market-data capture. This module exposes no trading API.

No connection occurs on import. Install the official IBKR Python API separately;
configure TWS/Gateway read-only API access and verified contracts/permissions.
The collector cannot infer TSE special quotes or per-symbol quiet heartbeats.
"""
from datetime import datetime, timezone
from queue import Queue, Empty, Full
from threading import Event, Thread
from time import monotonic
from math import isfinite

from .recording import CaptureError


def capture_market_data(recorder, *, host='127.0.0.1', port=7497, client_id=71,
                        duration_seconds=60, queue_capacity=10000):
    if type(port) is not int or not 1<=port<=65535 or type(client_id) is not int or client_id<0:
        raise ValueError('valid port and nonnegative client ID required')
    if type(duration_seconds) not in (int,float) or not isfinite(duration_seconds) or not 0<duration_seconds<=86400:
        raise ValueError('capture duration must be positive and at most one day')
    if type(queue_capacity) is not int or queue_capacity<=0:
        raise ValueError('positive bounded callback queue required')
    if any(not metadata.contract_verified for metadata in recorder.config.instruments):
        raise CaptureError('verify every contract and data permission before connecting the read-only collector')
    try:
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper
        from ibapi.contract import Contract
    except ImportError as error:
        raise RuntimeError('Official IBKR ibapi SDK is required for read-only capture; no connection was made') from error
    queue=Queue(maxsize=queue_capacity)
    ready,overflow=Event(),Event()

    class MarketDataClient(EWrapper,EClient):
        def __init__(self):
            EWrapper.__init__(self)
            EClient.__init__(self,self)

        def emit(self,callback,request_id=-1,**data):
            event=dict(callback=callback,request_id=request_id,
                received_at=datetime.now(timezone.utc).isoformat(),data=data)
            try:
                queue.put_nowait(event)
            except Full:
                overflow.set()  # never silently drop and continue trading research

        def nextValidId(self,orderId):
            # This is only the connection-ready callback; its order ID is unused.
            ready.set()

        def tickPrice(self,reqId,tickType,price,attrib):
            self.emit('tickPrice',reqId,tick_type=tickType,price=str(price))

        def tickSize(self,reqId,tickType,size):
            self.emit('tickSize',reqId,tick_type=tickType,size=str(size))

        def tickGeneric(self,reqId,tickType,value):
            self.emit('tickGeneric',reqId,tick_type=tickType,value=str(value))

        def tickString(self,reqId,tickType,value):
            self.emit('tickString',reqId,tick_type=tickType,value=value)

        def marketDataType(self,reqId,marketDataType):
            self.emit('marketDataType',reqId,market_data_type=marketDataType)

        def error(self,reqId,*args):
            # Current SDKs add errorTime before errorCode; older SDKs do not.
            if len(args)>=3 and isinstance(args[1],int):
                code,message=args[1],args[2]
            elif len(args)>=2:
                code,message=args[0],args[1]
            else:
                code,message=-1,str(args)
            self.emit('error',reqId,code=code,message=str(message))

        def connectionClosed(self):
            self.emit('connectionClosed')

    client=MarketDataClient()
    reader=None
    subscriptions=[]
    deadline=monotonic()+duration_seconds
    try:
        client.connect(host,port,client_id)
        reader=Thread(target=client.run,name='ibkr-readonly-reader',daemon=True)
        reader.start()
        if not ready.wait(min(10,duration_seconds)):
            raise CaptureError('read-only API connection did not become ready')
        client.reqMarketDataType(1)
        for metadata in recorder.config.instruments:
            if datetime.now(timezone.utc)<metadata.known_at:
                raise CaptureError('future contract metadata cannot request market data')
            contract=Contract()
            contract.conId=metadata.contract_id
            contract.symbol=metadata.symbol
            contract.localSymbol=metadata.local_symbol
            contract.secType=metadata.sec_type
            contract.exchange=metadata.exchange
            contract.primaryExchange=metadata.primary_exchange
            contract.currency=metadata.currency
            generic='233' if recorder.config.volume_mode=='rtvolume' else ''
            client.reqMktData(metadata.request_id,contract,generic,False,False,[])
            subscriptions.append(metadata.request_id)
            # Conservative subscription pacing; never infer account quota.
            if len(subscriptions)>1:
                overflow.wait(.2)
        while monotonic()<deadline:
            if overflow.is_set():
                raise CaptureError('bounded callback queue overflowed; capture is incomplete')
            try:
                callback=queue.get(timeout=min(.2,max(.001,deadline-monotonic())))
            except Empty:
                continue
            recorder.process(callback)
        if overflow.is_set():
            raise CaptureError('bounded callback queue overflowed')
        # A bounded final drain records already received events only.
        for _ in range(queue_capacity):
            try:
                recorder.process(queue.get_nowait())
            except Empty:
                break
        result=recorder.finish()
        result['transport']='OFFICIAL_IBAPI_READ_ONLY_MARKET_DATA'
        return result
    except Exception:
        recorder.failed=True
        raise
    finally:
        for request_id in subscriptions:
            client.cancelMktData(request_id)
        client.disconnect()
        if reader is not None:
            reader.join(timeout=2)
