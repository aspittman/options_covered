"""Research records stored in the existing per-bot SQLite ledger, not shared DBs."""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from performance import metrics
from risk import number, parse_option

REJECTION_FIELDS = ('timestamp', 'strategy', 'underlying', 'contract_symbol', 'call_or_put',
                    'long_or_short', 'strike', 'expiration', 'DTE', 'underlying_price',
                    'bid', 'ask', 'mid', 'spread_dollars', 'spread_percent', 'option_premium',
                    'required_capital', 'virtual_capital_available', 'rejection_reason',
                    'signal_score', 'market_regime', 'signal_date', 'details')


class ResearchLedger:
    def init_research(self):
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS stock_allocations (
                underlying TEXT PRIMARY KEY, shares REAL, cost_per_share REAL, allocated_at TEXT);
            CREATE TABLE IF NOT EXISTS stock_dispositions (
                activity_id TEXT PRIMARY KEY, underlying TEXT, shares REAL, price REAL,
                cost_per_share REAL, timestamp TEXT);
            CREATE TABLE IF NOT EXISTS order_context (
                client_id TEXT PRIMARY KEY, details TEXT);
            CREATE TABLE IF NOT EXISTS settlements (
                activity_id TEXT PRIMARY KEY, stock_activity_id TEXT UNIQUE, symbol TEXT,
                kind TEXT, qty REAL, timestamp TEXT);
            CREATE TABLE IF NOT EXISTS rejected_trades (
                key TEXT PRIMARY KEY, details TEXT);
            CREATE TABLE IF NOT EXISTS equity_samples (
                timestamp TEXT PRIMARY KEY, equity REAL, capital_employed REAL);
        ''')

    def bind_capital(self, starting):
        row = self.db.execute("SELECT value FROM metadata WHERE key='virtual_starting_capital'").fetchone()
        if row and float(row['value']) != starting:
            raise ValueError('Virtual starting capital is fixed for this ledger; use a separate LEDGER_PATH for comparisons')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('virtual_starting_capital', ?)", (str(starting),))

    def allocations(self):
        return {r['underlying']: dict(r) for r in self.db.execute('SELECT * FROM stock_allocations WHERE shares>0')}

    def allocate_shares(self, underlying, shares, cost_per_share, spot, settings):
        """Explicit virtual contribution of existing shares; never a stock order.

        The CLI checks broker holdings first. Persist cost even when no call is open,
        so closing a call cannot make its backing-stock capital available twice.
        """
        self.bind_capital(settings.virtual_starting_capital)
        shares, cost_per_share, spot = map(number, (shares, cost_per_share, spot))
        if shares != 100 or min(cost_per_share, spot) <= 0:
            raise ValueError('Allocate exactly 100 shares with positive cost and current price')
        if underlying in self.allocations() or any(r['symbol'] == underlying for r in self.pending_stock()):
            raise ValueError('This underlying already has an allocation or pending stock acquisition')
        state = self.capital_state(settings)
        required = max(cost_per_share, spot) * shares
        if required > settings.max_underlying_value_per_position:
            raise ValueError('UNDERLYING_VALUE_OVER_LIMIT')
        if required > state['virtual_capital_available'] or state['capital_employed'] + required > settings.max_covered_value:
            raise ValueError('MAX_STRATEGY_EXPOSURE_REACHED')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO stock_allocations VALUES (?,?,?,?)',
                            (underlying, shares, cost_per_share, datetime.now(timezone.utc).isoformat()))
        self.event('SHARES_ALLOCATED', underlying=underlying, shares=shares, cost_per_share=cost_per_share)

    def capital_state(self, settings, stock_marks=None):
        allocations = self.allocations()
        stock_marks = stock_marks or {}
        # Cost is a floor for risk sizing; gains never increase the fixed allocation.
        employed = sum(r['shares'] * max(r['cost_per_share'], stock_marks.get(s, r['cost_per_share']))
                       for s, r in allocations.items())
        reserved = self.stock_reserved()
        employed += reserved
        realized = self.report()['realized_option_pnl'] + self.realized_stock_pnl()
        budget = max(0, min(settings.virtual_starting_capital, settings.virtual_starting_capital + realized))
        return {'capital_employed': employed, 'pending_stock_reservation': reserved, 'budget': budget,
                'virtual_capital_available': max(0, budget - employed)}

    def realized_stock_pnl(self):
        return self.db.execute('SELECT COALESCE(SUM(shares*(price-cost_per_share)),0) FROM stock_dispositions').fetchone()[0]

    def reject(self, **details):
        row = {field: None for field in REJECTION_FIELDS}
        row.update(timestamp=datetime.now(timezone.utc).isoformat(), strategy='covered_call',
                   call_or_put='call', long_or_short='short')
        row.update(details)
        # One opportunity/reason per completed bar; repeated five-minute scans
        # must not inflate the research dataset. Distinct candidates remain distinct.
        key = json.dumps([row.get(k) for k in ('signal_date', 'underlying', 'contract_symbol', 'rejection_reason')])
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO rejected_trades VALUES (?,?)', (key, json.dumps(row, default=str)))

    def export_rejections(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=REJECTION_FIELDS)
            writer.writeheader()
            writer.writerows(json.loads(r[0]) for r in self.db.execute('SELECT details FROM rejected_trades ORDER BY rowid'))

    def context(self, client_id):
        row = self.db.execute('SELECT details FROM order_context WHERE client_id=?', (client_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def settle(self, activity, stock_activity=None):
        """Book only an exact, broker-confirmed short-call terminal event, once."""
        aid, symbol = activity['id'], activity['symbol']
        if self.db.execute('SELECT 1 FROM settlements WHERE activity_id=?', (aid,)).fetchone():
            return False
        parsed = parse_option(symbol)
        lot = self.report()['lots'].get(symbol)
        qty = number(activity['qty'])
        kind = {'OPASN': 'assignment', 'OPEXP': 'expiration'}.get(activity['activity_type'])
        if (not parsed or parsed['kind'] != 'C' or not lot or kind is None
                or activity.get('status') != 'executed' or qty != lot['qty'] or qty <= 0):
            raise ValueError('Ambiguous or foreign option settlement')
        if any(o['symbol'] == symbol for o in self.pending()):
            raise ValueError('Settlement has pending orders')
        stamp = activity['date'][:10]
        entry_dates = [r['timestamp'][:10] for r in self.db.execute(
            "SELECT timestamp FROM fills WHERE symbol=? AND intent='sell_to_open'", (symbol,))]
        if not entry_dates or stamp < max(entry_dates):
            raise ValueError('Settlement predates current entry')
        if kind == 'expiration' and stamp < parsed['expiration'].isoformat():
            raise ValueError('Expiration activity predates contract expiration')
        allocation = self.allocations().get(parsed['underlying'])
        if not allocation or allocation['shares'] < qty * 100:
            raise ValueError('Settlement lacks strategy-controlled shares')
        stock_id, exit_spot = None, None
        if kind == 'assignment':
            trade = stock_activity or {}
            if (trade.get('activity_type') != 'OPTRD' or trade.get('status') != 'executed'
                    or trade.get('symbol') != parsed['underlying'] or trade.get('date', '')[:10] != stamp
                    or number(trade.get('qty', 0)) != -qty * 100
                    or number(trade.get('price', 0)) != parsed['strike']):
                raise ValueError('Assignment lacks an exact matching stock disposition')
            stock_id, exit_spot = trade['id'], parsed['strike']
        with self.db:
            self.db.execute('INSERT INTO settlements VALUES (?,?,?,?,?,?)', (aid, stock_id, symbol, kind, qty, stamp))
            self.db.execute('INSERT INTO fills(client_id,symbol,underlying,intent,qty,notional,timestamp) VALUES (?,?,?,?,?,?,?)',
                            (aid, symbol, parsed['underlying'], 'buy_to_close', qty, 0, stamp))
            self.db.execute('INSERT INTO order_context VALUES (?,?)',
                            (aid, json.dumps({'outcome': kind, 'stock_price': exit_spot})))
            if stock_id:
                self.db.execute('INSERT INTO stock_dispositions VALUES (?,?,?,?,?,?)',
                                (stock_id, parsed['underlying'], qty * 100, exit_spot, allocation['cost_per_share'], stamp))
                self.db.execute('UPDATE stock_allocations SET shares=shares-? WHERE underlying=?',
                                (qty * 100, parsed['underlying']))
        return True

    def research_trades(self):
        """Replay cumulative-fill deltas into FIFO call lifecycles; no rejected trades."""
        trades, queues, by_id = [], {}, {}
        for fill in self.db.execute('SELECT * FROM fills ORDER BY id'):
            ctx = self.context(fill['client_id'])
            if fill['intent'] == 'sell_to_open':
                if fill['client_id'] not in by_id:
                    trade = {'client_id': fill['client_id'], 'symbol': fill['symbol'], 'qty': 0,
                             'remaining': 0, 'premium_received': 0, 'closing_debit': 0,
                             'opened_at': fill['timestamp'], 'capital_employed': ctx.get('capital_employed'),
                             'entry_stock_price': ctx.get('stock_price'), 'stock_movement': 0,
                             'stock_marks_complete': True, 'closed': False}
                    by_id[fill['client_id']] = trade
                    trades.append(trade)
                    queues.setdefault(fill['symbol'], []).append(trade)
                trade = by_id[fill['client_id']]
                trade['qty'] += fill['qty']
                trade['remaining'] += fill['qty']
                trade['premium_received'] += fill['notional'] * 100
            else:
                left = fill['qty']
                for trade in queues.get(fill['symbol'], []):
                    used = min(left, trade['remaining'])
                    if not used:
                        continue
                    trade['remaining'] -= used
                    left -= used
                    trade['closing_debit'] += fill['notional'] * 100 * used / fill['qty']
                    if ctx.get('stock_price') is not None and trade['entry_stock_price'] is not None:
                        trade['stock_movement'] += (ctx['stock_price'] - trade['entry_stock_price']) * used * 100
                    else:
                        trade['stock_marks_complete'] = False
                    if not trade['remaining']:
                        trade['closed'] = True
                        trade['outcome'] = ctx.get('outcome', 'buy_to_close')
                        trade['option_pnl'] = trade['premium_received'] - trade['closing_debit']
                        trade['combined_pnl'] = (trade['option_pnl'] + trade['stock_movement']
                                                 if trade['stock_marks_complete'] else None)
                        trade['hold_days'] = (datetime.fromisoformat(fill['timestamp']).date()
                                              - datetime.fromisoformat(trade['opened_at']).date()).days
        return trades

    def research_report(self, settings, option_marks=None, stock_marks=None, record=False):
        self.bind_capital(settings.virtual_starting_capital)
        options = self.report(option_marks)
        stocks = self.allocations()
        stock_marks = stock_marks or {}
        missing_stocks = sorted(set(stocks) - set(stock_marks))
        stock_unrealized = sum((stock_marks[s] - r['cost_per_share']) * r['shares']
                               for s, r in stocks.items() if s in stock_marks)
        realized_stock = self.realized_stock_pnl()
        unrealized = (stock_unrealized + options['marked_unrealized_option_pnl']
                      if not missing_stocks and not options['unmarked_symbols'] else None)
        realized = realized_stock + options['realized_option_pnl']
        cost = sum(r['cost_per_share'] * r['shares'] for r in stocks.values())
        equity = settings.virtual_starting_capital + realized + unrealized if unrealized is not None else None
        if record:
            with self.db:
                self.db.execute('INSERT INTO equity_samples VALUES (?,?,?)',
                                (datetime.now(timezone.utc).isoformat(), equity, cost))
        samples = [dict(r) for r in self.db.execute('SELECT * FROM equity_samples ORDER BY timestamp')]
        if not samples or not record:
            samples.append({'equity': equity, 'capital_employed': cost})
        result = metrics(settings.virtual_starting_capital, realized, unrealized, self.research_trades(), samples)
        called = self.db.execute('SELECT COALESCE(SUM(shares),0) FROM stock_dispositions').fetchone()[0]
        result.update(options, underlying_share_cost=cost, realized_stock_pnl=realized_stock,
                      unrealized_stock_pnl=stock_unrealized if not missing_stocks else None,
                      share_appreciation_depreciation=realized_stock + stock_unrealized if not missing_stocks else None,
                      shares_called_away=called, unmarked_stock_symbols=missing_stocks,
                      premium_income=options['realized_option_pnl'],
                      allocations=stocks, stock_lifecycle=self.stock_lifecycle(),
                      pending_stock_orders=self.pending_stock(), **self.capital_state(settings, stock_marks))
        return result
