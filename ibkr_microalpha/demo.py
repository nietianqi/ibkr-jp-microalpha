"""Deterministic artificial market fixture; never evidence of investment edge."""
from datetime import datetime, timedelta
from decimal import Decimal as D
import json
from pathlib import Path

from .config import build_engine, load_config
from .market import JST
from .replay import Replay

STOCK, BENCH = 'STOCK_SYNTHETIC', 'BENCH_SYNTHETIC'


def create_demo(config_path, destination):
    """Emit the inputs later consumed by replay, including simulated reports.

    Returns ``(events_path, runner)``: the generating runner already holds the
    complete result, so a second replay is only needed for verification.

    The tiny simulator fills marketable limits at visible ask/bid only, capped
    by quoted size. It never assumes a passive quote touch establishes a fill.
    The calibration row and profiles are artificial fixtures with explicit tags.
    """
    engine = build_engine(load_config(config_path))
    runner = Replay(engine)
    rows = []
    sequence = 0
    start = datetime(2026, 10, 2, 9, tzinfo=JST)

    def emit(at, kind, data):
        nonlocal sequence
        sequence += 1
        row = {'event_id': f'artificial:{sequence}', 'received_at': at.isoformat(),
               'sequence': sequence, 'type': kind, 'data': data}
        rows.append(row)
        runner.dispatch(row)

    preopen = start - timedelta(minutes=5)
    known = preopen.isoformat()
    emit(preopen, 'calendar', {'day': start.date().isoformat(), 'is_open': True,
                               'known_at': known, 'source': 'ARTIFICIAL_FIXTURE'})
    emit(preopen, 'reconcile', {'positions': {}, 'open_orders': [], 'executions': [],
                                'complete': True, 'ownership_confirmed': True, 'next_order_id': 1})
    c = engine.config
    emit(preopen, 'calibration_table', {'version': 'ARTIFICIAL-calibration-v1', 'known_at': known, 'rows': [
        {'policy_id': c.policy_id, 'version': c.model_version, 'holding_seconds': c.holding_seconds,
         'quantity': 100, 'score_low': 0.5, 'score_high': None, 'sample_days': 40,
         'sample_count': 100, 'mean_net_amount': '1000', 'lower_net_amount': '800',
         'max_chase_ticks': 10}]})
    # One same-time bucket row replaces 20 days x 1 row per second (review DATA-03).
    features = engine.features.config
    bucket = features.baseline_bucket_seconds
    for bucket_start in (9 * 3600 + 300, 9 * 3600 + 600):
        emit(preopen, 'same_time_profile', {
            'kind': 'volume', 'symbol': STOCK, 'source': features.trade_source, 'window_seconds': 30,
            'bucket_start_second': bucket_start, 'bucket_seconds': bucket, 'median_value': 3000,
            'valid_days': 20, 'known_at': known, 'version': 'ARTIFICIAL-profile-v1'})
        emit(preopen, 'same_time_profile', {
            'kind': 'volatility', 'symbol': BENCH, 'source': features.quote_source,
            'window_seconds': features.market_volatility_window_seconds,
            'bucket_start_second': bucket_start, 'bucket_seconds': bucket, 'median_value': 5,
            'valid_days': 20, 'known_at': known, 'version': 'ARTIFICIAL-profile-v1'})
    for second in range(751):
        at = start + timedelta(seconds=second)
        stamp = at.isoformat()
        if second % 30 == 0:
            emit(at, 'account_snapshot', {'account_id': 'ARTIFICIAL', 'currency': 'JPY',
                                          'available_funds': '5000000', 'net_liquidation': '5000000',
                                          'source': 'ARTIFICIAL_FIXTURE'})
        if second % 2 == 0:
            for symbol in (BENCH, STOCK):
                emit(at, 'quote_stream_health', {'symbol': symbol, 'healthy': True})
        # After entry, a controlled drop demonstrates priority of the hard stop.
        bid = D('900') + D(second) * D('.1') if second < 620 else D('950')
        for symbol, b, a in ((BENCH, D('500'), D('500.1')), (STOCK, bid, bid + D('.1'))):
            emit(at, 'quote', {'symbol': symbol, 'bid': str(b), 'ask': str(a),
                               'bid_size': 1000, 'ask_size': 200, 'source': 'L1',
                               'bid_at': stamp, 'ask_at': stamp})
        # Genuine cumulative volume (never summed last sizes): +200 shares per second.
        emit(at, 'cumulative_volume', {'symbol': STOCK, 'total': 200 * (second + 1),
                                       'last_price': str(bid + D('.1'))})
        emit(at, 'requests', {})
        for command in list(runner.last_commands):
            if command.order_id is None:
                continue
            order = engine.book.orders[command.order_id]
            if command.kind == 'CANCEL':
                emit(at, 'status', {'order_id': order.order_id, 'ib_status': 'Cancelled',
                                    'filled': order.filled_quantity, 'remaining': 0})
            elif command.kind == 'SUBMIT':
                emit(at, 'status', {'order_id': order.order_id, 'ib_status': 'Submitted',
                                    'filled': order.filled_quantity, 'remaining': order.remaining_quantity})
                quote = engine.quotes[order.symbol]
                price = quote.ask if order.side == 'BUY' else quote.bid
                marketable = order.limit_price >= price if order.side == 'BUY' else order.limit_price <= price
                available = quote.ask_size if order.side == 'BUY' else quote.bid_size
                if marketable and order.remaining_quantity <= available:
                    exec_id = f'simulation-fill:{order.order_id}'
                    emit(at, 'fill', {'exec_id': exec_id, 'order_id': order.order_id,
                                      'qty': order.remaining_quantity, 'price': str(price)})
                    emit(at, 'status', {'order_id': order.order_id, 'ib_status': 'Filled',
                                        'filled': order.filled_quantity, 'remaining': 0})
                    emit(at, 'commission', {'exec_id': exec_id,
                         'amount': str(engine.commissions.commission(price * order.filled_quantity))})
    runner.finish()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    return destination, runner
