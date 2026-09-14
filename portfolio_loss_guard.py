"""Read-only, conservative loss guard across sibling bot ledgers.

No tax-lot reporting or order submission. Default paths can be overridden with a
JSON object in LOSS_LEDGER_PATHS. Set LOSS_GUARD_SCOPE=bot for local-only guards.
"""
import csv
import json
import logging
import os
import re
import sqlite3
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
_cache = {}
DEFAULT_PATHS = {
    'options_direct': 'logs/trade_analytics.csv',
    'options_inverted': 'logs/trade_analytics.csv',
    'options_covered': 'logs/trades.sqlite3',
    'options_secured': 'logs/options_secured.sqlite3',
}


def _day(stamp):
    dt = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    return dt.astimezone(NY).date() if dt.tzinfo else dt.date()


def _losses_from_rows(rows, short=False):
    lots, losses = {}, {}
    for row in rows:
        key = (row.get('strategy', ''), row['underlying'], row['symbol'])
        if row.get('reset'):
            lots.pop(key, None)
            continue
        qty, price = float(row['qty']), float(row['price'])
        if qty <= 0:
            continue
        queue = lots.setdefault(key, deque())
        if row['side'] == ('sell' if short else 'buy'):
            queue.append([qty, price])
        else:
            while qty > 0 and queue:
                used = min(qty, queue[0][0])
                if price > queue[0][1] if short else price < queue[0][1]:
                    day = _day(row['timestamp'])
                    losses[key[1]] = max(losses.get(key[1], day), day)
                qty -= used
                queue[0][0] -= used
                if queue[0][0] <= 0:
                    queue.popleft()
    return losses


def read_loss_dates(name, path):
    path = Path(path)
    if not path.exists():
        return {}
    def signature(p):
        st = p.stat() if p.exists() else None
        return (st.st_ino, st.st_size, st.st_mtime_ns) if st else None
    signature_now = (signature(path), signature(Path(str(path)+'-wal')))
    cached = _cache.get(str(path))
    if cached and cached[0] == signature_now:
        return cached[1]
    if name in ('options_direct', 'options_inverted'):
        with path.open(newline='') as handle:
            rows = []
            for r in csv.DictReader(handle, strict=True):
                if r.get('event') not in {'ORDER_FILL', 'ORDER_PARTIAL_FILL', 'POSITION_MISSING'} or (name == 'options_inverted' and r.get('event') == 'POSITION_MISSING'):
                    continue
                rows.append(dict(strategy=r.get('strategy', ''), underlying=r['underlying'],
                                 symbol=r['option_symbol'], qty=r.get('qty') or 0,
                                 price=r.get('price') or 0, side=r.get('order_side'),
                                 timestamp=r['timestamp'], reset=r['event']=='POSITION_MISSING'))
        losses = _losses_from_rows(rows)
    else:
        db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            if name == 'options_covered':
                rows = [dict(underlying=r['underlying'], symbol=r['symbol'], qty=r['qty'],
                             price=r['notional']/r['qty'],
                             side='sell' if r['intent']=='sell_to_open' else 'buy', timestamp=r['timestamp'])
                        for r in db.execute('SELECT * FROM fills ORDER BY id') if r['qty'] > 0]
                losses = _losses_from_rows(rows, short=True)
                for row in db.execute('SELECT * FROM stock_dispositions WHERE price < cost_per_share'):
                    day = _day(row['timestamp'])
                    losses[row['underlying']] = max(losses.get(row['underlying'], day), day)
            else:
                losses = {}
                for row in db.execute('SELECT * FROM pnl WHERE realized < 0'):
                    match = re.fullmatch(r'([A-Z.]+)\d{6}[CP]\d{8}', row['symbol'])
                    if not match:
                        raise ValueError('Invalid loss contract symbol')
                    root, day = match.group(1), _day(row['timestamp'])
                    losses[root] = max(losses.get(root, day), day)
        finally:
            db.close()
    _cache[str(path)] = (signature_now, losses)
    return losses


def portfolio_blocked(underlying, today=None):
    scope = os.getenv('LOSS_GUARD_SCOPE', 'portfolio').lower()
    if scope == 'bot':
        return False
    today = today or datetime.now(NY).date()
    try:
        if scope != 'portfolio':
            raise ValueError('LOSS_GUARD_SCOPE must be portfolio or bot')
        here = Path(__file__).resolve().parent
        paths = {name: here.parent/name/relative for name, relative in DEFAULT_PATHS.items()}
        paths.update({name: Path(path).expanduser() for name, path in
                      json.loads(os.getenv('LOSS_LEDGER_PATHS', '{}')).items()})
        for name, path in paths.items():
            if name == here.name:
                continue  # The caller checks its active local ledger directly.
            losses = read_loss_dates(name, path)
            day = losses.get(underlying)
            if day is not None and 0 <= (today-day).days <= 30:
                return True
        return False
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, csv.Error) as exc:
        logging.getLogger(__name__).warning('Shared loss history unavailable; entries blocked: %s', exc)
        return True  # Never turn an unreadable configured ledger into permission to enter.
