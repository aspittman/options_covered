"""Durable order intents and incremental confirmed fills, isolated by account."""
import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from risk import enum_value, number, value
from research import ResearchLedger

TERMINAL = {'filled', 'canceled', 'expired', 'rejected', 'replaced'}


class Ledger(ResearchLedger):
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS option_trailing_marks (
                symbol TEXT PRIMARY KEY, entry_id TEXT NOT NULL, low_price REAL NOT NULL);

            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS orders (
                client_id TEXT PRIMARY KEY, broker_id TEXT, symbol TEXT, underlying TEXT,
                intent TEXT, qty REAL, limit_price REAL, signal_date TEXT, reason TEXT,
                created_at TEXT, status TEXT, filled_qty REAL DEFAULT 0,
                filled_notional REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY, client_id TEXT, symbol TEXT, underlying TEXT,
                intent TEXT, qty REAL, notional REAL, timestamp TEXT);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, timestamp TEXT, kind TEXT, details TEXT);
        ''')
        self.init_research()

    def bind_account(self, account_id, paper):
        identity = f'{account_id}:{paper}'
        row = self.db.execute("SELECT value FROM metadata WHERE key='account'").fetchone()
        if row and row['value'] != identity:
            raise RuntimeError('Ledger belongs to another account or environment; use another LEDGER_PATH')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('account', ?)", (identity,))

    def event(self, kind, **details):
        details.setdefault('strategy', 'covered_call')
        with self.db:
            self.db.execute('INSERT INTO events(timestamp,kind,details) VALUES (?,?,?)',
                            (datetime.now(timezone.utc).isoformat(), kind, json.dumps(details, default=str)))

    def prepare(self, symbol, underlying, intent, qty, price, signal_date='', reason='', context=None):
        client_id = f'covered_call_{underlying}_' + uuid4().hex[:20]
        with self.db:
            self.db.execute('''INSERT INTO orders
                (client_id,symbol,underlying,intent,qty,limit_price,signal_date,reason,created_at,status)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (client_id, symbol, underlying, intent, qty, price, signal_date, reason,
                 datetime.now(timezone.utc).isoformat(), 'submission_unknown'))
            self.db.execute('INSERT INTO order_context VALUES (?,?)', (client_id, json.dumps(context or {})))
        return client_id

    def update(self, client_id, order):
        row = self.db.execute('SELECT * FROM orders WHERE client_id=?', (client_id,)).fetchone()
        if value(order, 'client_order_id') != client_id or value(order, 'symbol') != row['symbol']:
            raise RuntimeError('Broker order identity mismatch')
        expected_side = 'sell' if row['intent'] == 'sell_to_open' else 'buy'
        if enum_value(value(order, 'side')) != expected_side:
            raise RuntimeError('Broker order side mismatch')
        filled = number(value(order, 'filled_qty') or 0)
        if filled < row['filled_qty'] or filled > row['qty']:
            raise RuntimeError('Invalid cumulative fill quantity')
        notional = filled * number(value(order, 'filled_avg_price') or 0)
        delta_qty = filled - row['filled_qty']
        delta_notional = notional - row['filled_notional']
        if delta_qty and delta_notional <= 0:
            raise RuntimeError('Invalid confirmed fill price')
        if not delta_qty and abs(delta_notional) > 1e-6:
            raise RuntimeError('Broker revised fill pricing; manual reconciliation required')
        with self.db:
            if delta_qty:
                self.db.execute('''INSERT INTO fills
                    (client_id,symbol,underlying,intent,qty,notional,timestamp) VALUES (?,?,?,?,?,?,?)''',
                    (client_id, row['symbol'], row['underlying'], row['intent'], delta_qty, delta_notional,
                     str(value(order, 'filled_at') or datetime.now(timezone.utc).isoformat())))
            self.db.execute('''UPDATE orders SET broker_id=?,status=?,filled_qty=?,filled_notional=?
                               WHERE client_id=?''',
                            (str(value(order, 'id')), enum_value(value(order, 'status')),
                             filled, notional, client_id))

    def orders(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM orders ORDER BY created_at')]

    def pending(self):
        return [r for r in self.orders() if r['status'] not in TERMINAL]

    def traded_bar(self, underlying, bar_date):
        return any(r['underlying'] == underlying and r['signal_date'] == bar_date
                   and r['intent'] == 'sell_to_open' for r in self.orders())

    def cooling_down(self, underlying, today, days):
        row = self.db.execute("SELECT MAX(timestamp) AS stamp FROM fills WHERE underlying=? AND intent='buy_to_close'",
                              (underlying,)).fetchone()
        return bool(row['stamp'] and (today - datetime.fromisoformat(row['stamp']).date()).days < days)

    def option_trailing_stop(self, symbol, entry_credit, ask, fraction):
        """Persist the lowest observed buyback price for this entry, not prior trades."""
        from math import isfinite
        if not all(isfinite(v) and v > 0 for v in (entry_credit, ask)) or not 0 < fraction < 1:
            raise ValueError('Invalid short-option trailing-stop input')
        entry = self.db.execute("SELECT client_id FROM fills WHERE symbol=? AND intent='sell_to_open' ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
        # A legacy position without an entry ID still gets a conservative entry-based stop.
        if not entry:
            return min(entry_credit, ask) * (1 + fraction)
        entry_id = entry[0]
        row = self.db.execute('SELECT entry_id,low_price FROM option_trailing_marks WHERE symbol=?', (symbol,)).fetchone()
        low = min(entry_credit, ask)
        if row and row['entry_id'] == entry_id:
            low = min(low, row['low_price'])
        if not row or row['entry_id'] != entry_id or low < row['low_price']:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO option_trailing_marks VALUES (?,?,?)', (symbol, entry_id, low))
        return low * (1 + fraction)

    def loss_blocked(self, underlying, today=None):
        from loss_guard import blocked, latest_loss_dates
        events = [dict(event='ORDER_FILL', strategy='', underlying=r['underlying'],
                       option_symbol=r['symbol'], qty=r['qty'], price=r['notional']/r['qty'],
                       order_side='sell' if r['intent']=='sell_to_open' else 'buy', timestamp=r['timestamp'])
                  for r in self.db.execute('SELECT * FROM fills ORDER BY id') if r['qty'] > 0]
        losses = latest_loss_dates(events, short=True)
        for row in self.db.execute('SELECT * FROM stock_dispositions WHERE price < cost_per_share'):
            day = datetime.fromisoformat(row['timestamp']).date()
            losses[row['underlying']] = max(losses.get(row['underlying'], day), day)
        from portfolio_loss_guard import portfolio_blocked
        return blocked(underlying, losses, today) or portfolio_blocked(underlying, today)

    def report(self, marks=None):
        lots, realized = {}, 0.0
        by_variant = {name: {'realized_option_pnl': 0.0, 'marked_unrealized_option_pnl': 0.0}
                      for name in ('regular', 'oasis')}
        for row in self.db.execute('SELECT * FROM fills ORDER BY id'):
            lot = lots.setdefault(row['symbol'], {'qty': 0, 'credit': 0, 'underlying': row['underlying']})
            if row['intent'] == 'sell_to_open':
                if lot['qty'] == 0:
                    lot['variant'] = self.context(row['client_id']).get('variant', 'regular')
                    lot['opened_at'] = row['timestamp']
                lot['qty'] += row['qty']
                lot['credit'] += row['notional']
            else:
                if row['qty'] > lot['qty']:
                    raise RuntimeError('Close fills exceed tracked short quantity')
                basis = lot['credit'] / lot['qty'] * row['qty']
                pnl = (basis - row['notional']) * 100
                realized += pnl
                by_variant.setdefault(lot.get('variant', 'regular'),
                    {'realized_option_pnl': 0.0, 'marked_unrealized_option_pnl': 0.0})['realized_option_pnl'] += pnl
                lot['qty'] -= row['qty']
                lot['credit'] -= basis
        lots = {s: lot for s, lot in lots.items() if lot['qty'] > 0}
        marks = marks or {}
        unrealized = sum((lot['credit'] - marks[s] * lot['qty']) * 100
                         for s, lot in lots.items() if s in marks)
        for symbol, lot in lots.items():
            if symbol in marks:
                by_variant[lot.get('variant', 'regular')]['marked_unrealized_option_pnl'] += (lot['credit'] - marks[symbol]*lot['qty'])*100
        return {'by_variant': by_variant, 'realized_option_pnl': round(realized, 2),
                'marked_unrealized_option_pnl': round(unrealized, 2),
                'unmarked_symbols': sorted(set(lots) - set(marks)), 'lots': lots,
                'pending_orders': len(self.pending())}

    def export_fills(self, path):
        rows = self.db.execute('SELECT * FROM fills ORDER BY id')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow([column[0] for column in rows.description])
            writer.writerows(rows)
