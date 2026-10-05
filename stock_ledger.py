"""Durable paper stock intents, cash reservations and incremental owned fills."""
from datetime import datetime, timezone
import json
from uuid import uuid4
from risk import enum_value, number, value

STOCK_TERMINAL = {'filled', 'canceled', 'expired', 'rejected'}


class StockLedger:
    def init_stock_ledger(self):
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS stock_orders (
                client_id TEXT PRIMARY KEY, broker_id TEXT, symbol TEXT,
                qty REAL, limit_price REAL, created_at TEXT, status TEXT,
                filled_qty REAL DEFAULT 0, filled_notional REAL DEFAULT 0,
                context TEXT, call_state TEXT DEFAULT 'waiting');
            CREATE TABLE IF NOT EXISTS stock_fills (
                id INTEGER PRIMARY KEY, client_id TEXT, symbol TEXT,
                qty REAL, notional REAL, timestamp TEXT);
        ''')

    def stock_orders(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM stock_orders ORDER BY created_at')]

    def pending_stock(self):
        return [r for r in self.stock_orders() if r['status'] not in STOCK_TERMINAL]

    def stock_reserved(self):
        return sum((r['qty'] - r['filled_qty']) * r['limit_price'] for r in self.pending_stock())

    def prepare_stock(self, symbol, price, context):
        price = number(price)
        if price <= 0 or symbol in self.allocations() or any(r['symbol'] == symbol for r in self.pending_stock()):
            raise ValueError('Duplicate stock lot or invalid limit')
        # A repeated scan/restart must not retry the same stock signal, even after rejection.
        if any(r['symbol'] == symbol and json.loads(r['context']).get('date') == context.get('date')
               for r in self.stock_orders()):
            raise ValueError('Stock signal already attempted')
        cid = 'covered_stock_' + uuid4().hex[:24]
        with self.db:
            self.db.execute('''INSERT INTO stock_orders
                (client_id,symbol,qty,limit_price,created_at,status,context) VALUES (?,?,?,?,?,?,?)''',
                (cid, symbol, 100, price, datetime.now(timezone.utc).isoformat(), 'submission_unknown', json.dumps(context)))
        return cid

    def update_stock(self, client_id, order):
        row = self.db.execute('SELECT * FROM stock_orders WHERE client_id=?', (client_id,)).fetchone()
        if (not row or value(order, 'client_order_id') != client_id or value(order, 'symbol') != row['symbol']
                or enum_value(value(order, 'side')) != 'buy' or number(value(order, 'qty')) != 100):
            raise RuntimeError('Stock order identity mismatch')
        status = enum_value(value(order, 'status'))
        if status == 'replaced':
            raise RuntimeError('External stock replacement requires reconciliation')
        filled = number(value(order, 'filled_qty') or 0)
        average = number(value(order, 'filled_avg_price') or 0)
        notional = filled * average
        delta, spent = filled - row['filled_qty'], notional - row['filled_notional']
        if (filled != int(filled) or not row['filled_qty'] <= filled <= 100
                or status == 'filled' and filled != 100
                or delta and (spent <= 0 or notional > filled * row['limit_price'] + .01)
                or not delta and abs(spent) > .0001):
            raise RuntimeError('Invalid cumulative stock fill')
        allocation = self.allocations().get(row['symbol'])
        if delta:
            owned = allocation['shares'] if allocation else 0
            cost = owned * allocation['cost_per_share'] if allocation else 0
            if abs(owned - row['filled_qty']) > .0001 or abs(cost - row['filled_notional']) > .01:
                raise RuntimeError('Stock allocation changed outside the pending acquisition')
        stamp = str(value(order, 'filled_at') or datetime.now(timezone.utc).isoformat())
        with self.db:
            if delta:
                self.db.execute('INSERT INTO stock_fills(client_id,symbol,qty,notional,timestamp) VALUES (?,?,?,?,?)',
                                (client_id, row['symbol'], delta, spent, stamp))
                self.db.execute('INSERT OR REPLACE INTO stock_allocations VALUES (?,?,?,?)',
                                (row['symbol'], filled, notional / filled,
                                 allocation['allocated_at'] if allocation else stamp))
            self.db.execute('UPDATE stock_orders SET broker_id=?,status=?,filled_qty=?,filled_notional=? WHERE client_id=?',
                            (str(value(order, 'id')), status, filled, notional, client_id))

    def finish_stock_plan(self, client_id, state):
        if state not in {'submitted', 'retained'}:
            raise ValueError('Unknown stock plan outcome')
        with self.db:
            self.db.execute('UPDATE stock_orders SET call_state=? WHERE client_id=?', (state, client_id))

    def stock_lifecycle(self):
        lots, pending = self.report()['lots'], self.pending()
        acquisitions = {r['symbol'] for r in self.pending_stock()}
        result = {}
        for symbol, allocation in self.allocations().items():
            if symbol in acquisitions:
                state = 'stock_order_pending'
            elif allocation['shares'] < 100:
                state = 'partial_lot_review'
            elif any(l['underlying'] == symbol for l in lots.values()):
                state = 'covered'
            elif any(o['underlying'] == symbol and o['intent'] == 'sell_to_open' for o in pending):
                state = 'call_order_pending'
            else:
                state = 'uncovered_reusable'
            result[symbol] = dict(allocation, state=state)
        return result
