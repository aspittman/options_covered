"""Paper-only acquisition of a qualifying call's collateral, then revalidation."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import json

from events import event_block
from oasis import entry_window, get_oasis_signal_state
from risk import value, number, enum_value, parse_option, capital_rejection, evaluate_candidate
from stock_ledger import STOCK_TERMINAL


class BuyWrite:
    def reconcile_stock(self, now):
        reliable = True
        for row in self.ledger.pending_stock():
            try:
                order = self.broker.order(row)
                self.ledger.update_stock(row['client_id'], order)
                age = (now - datetime.fromisoformat(row['created_at'])).total_seconds() / 60
                context = json.loads(row['context'])
                clock = self.broker.clock()
                cancel = (age >= self.settings.entry_timeout_minutes or not self.settings.enable_new_entries
                          or not self.settings.auto_buy_shares
                          or self.ledger.loss_blocked(row['symbol']) or not clock.is_open
                          or context.get('variant') == 'oasis' and not entry_window(clock))
                if (enum_value(value(order, 'status')) not in STOCK_TERMINAL | {'pending_cancel'}
                        and cancel and not self.settings.dry_run):
                    self.broker.cancel(str(value(order, 'id')))
                    self.note('STOCK_CANCEL_REQUESTED', client_id=row['client_id'])
            except Exception as exc:
                reliable = False
                self.note('STOCK_RECONCILIATION_UNCERTAIN', client_id=row['client_id'], error=str(exc))
        return reliable

    def acquire_stock(self, underlying, state, contract, now, today):
        s = self.settings
        if not (s.auto_buy_shares and s.paper and s.enable_new_entries):
            return False
        if any(r['symbol'] == underlying and json.loads(r['context']).get('date') == str(state['date'])
               for r in self.ledger.stock_orders()):
            self.note('STOCK_ENTRY_BLOCKED', underlying=underlying, reason='stock_signal_already_attempted')
            return False
        self.account_ok(self.broker.account(), entry=True)
        clock = self.broker.clock()
        if not clock.is_open or state.get('variant') == 'oasis' and not entry_window(clock):
            return False
        # Stock ownership needs its own daily suitability check even for an intraday call.
        daily = self.daily_signal(underlying, now, self.broker.previous_session(today))
        if not daily['eligible'] or self.ledger.loss_blocked(underlying, today):
            self.note('STOCK_ENTRY_BLOCKED', underlying=underlying, reason='stock_signal_or_loss_guard')
            return False
        parsed = parse_option(value(contract, 'symbol'))
        if not parsed or parsed['kind'] != 'C' or parsed['underlying'] != underlying:
            raise ValueError('A qualifying standard call is required before stock acquisition')
        if event_block(s.events_path, underlying, today, parsed['expiration']):
            return False
        allocations = self.ledger.allocations()
        pending = self.ledger.pending_stock()
        symbols = set(allocations) | {r['symbol'] for r in pending}
        if underlying in symbols or len(symbols) >= s.max_contracts:
            self.note('STOCK_ENTRY_BLOCKED', underlying=underlying, reason='stock_lot_limit')
            return False
        positions, orders = self.broker.positions(), self.broker.open_orders()
        # Never merge another strategy's shares/options/orders into this stock lot.
        for item in list(positions) + list(orders):
            symbol = value(item, 'symbol')
            option = parse_option(symbol)
            if value(item, 'legs') or symbol == underlying or option and option['underlying'] == underlying:
                self.note('STOCK_ENTRY_BLOCKED', underlying=underlying, reason='existing_broker_exposure')
                return False
        bid, ask = self.broker.stock_quote(underlying, now)
        if bid <= 0 or (ask - bid) / ((ask + bid) / 2) > s.max_stock_spread:
            return False
        price = float(Decimal(str(ask)).quantize(Decimal('.01'), rounding=ROUND_CEILING))
        if s.above_cost_basis and price > parsed['strike']:
            return False
        if state.get('variant') == 'oasis' and not get_oasis_signal_state(underlying).get('bullish', False):
            return False
        symbol = value(contract, 'symbol')
        snapshot = self.broker.snapshots([symbol]).get(symbol)
        volumes = self.broker.volumes([symbol], today)
        _, rejected = evaluate_candidate(contract, snapshot, volumes.get(symbol),
                                         price, price, today, datetime.now(timezone.utc), s)
        if rejected:
            self.note('STOCK_ENTRY_BLOCKED', underlying=underlying, reason='call_recheck_' + rejected)
            return False
        marks = {symbol: self.broker.spot(symbol, now) for symbol in allocations}
        capital = self.ledger.capital_state(s, marks)
        reason = capital_rejection(price, price, capital['capital_employed'] + 100 * price, capital['budget'], s)
        if reason:
            self.note('STOCK_ENTRY_BLOCKED', underlying=underlying, reason=reason)
            return False
        # Use cash, not margin. Reserve other broker buy orders and short-put collateral.
        account = self.broker.account()
        available = min(number(value(account, 'cash')), number(value(account, 'buying_power')))
        for p in positions:
            option = parse_option(value(p, 'symbol'))
            if option and option['kind'] == 'P' and number(value(p, 'qty')) < 0:
                available -= abs(number(value(p, 'qty'))) * option['strike'] * 100
        for order in orders:
            remaining = max(0, number(value(order, 'qty')) - number(value(order, 'filled_qty') or 0))
            option = parse_option(value(order, 'symbol'))
            if enum_value(value(order, 'side')) == 'buy':
                limit = number(value(order, 'limit_price'))  # Unknown market-order cost fails closed.
                available -= remaining * limit * (100 if option else 1)
            elif option and option['kind'] == 'P':
                available -= remaining * option['strike'] * 100
        # Include locally unknown submissions absent from the broker's open-order response.
        broker_ids = {value(o, 'client_order_id') for o in orders}
        available -= sum((r['qty'] - r['filled_qty']) * r['limit_price'] for r in pending
                         if r['client_id'] not in broker_ids)
        if available < 100 * price + s.stock_cash_buffer:
            self.note('STOCK_ENTRY_BLOCKED', underlying=underlying, reason='cash_limit')
            return False
        context = dict(date=str(state['date']), variant=state.get('variant', 'regular'),
                       selected_call=value(contract, 'symbol'))
        if s.dry_run:
            self.note('DRY_RUN_STOCK_BUY', underlying=underlying, qty=100, limit_price=price,
                      selected_call=context['selected_call'])
            return False
        client_id = self.ledger.prepare_stock(underlying, price, context)
        try:
            order = self.broker.submit_stock(client_id, underlying, price)
            self.ledger.update_stock(client_id, order)
            self.note('STOCK_ORDER_SUBMITTED', client_id=client_id, underlying=underlying, qty=100)
        except Exception:
            self.note('STOCK_SUBMISSION_UNKNOWN', client_id=client_id,
                      reason='Reconcile by client ID; never submit a duplicate')
            raise
        return True

    def continue_buy_writes(self, now, today, previous_session):
        if not self.settings.enable_new_entries:
            return
        for row in self.ledger.stock_orders():
            if row['status'] not in STOCK_TERMINAL or row['call_state'] != 'waiting':
                continue
            if row['filled_qty'] != 100:
                self.ledger.finish_stock_plan(row['client_id'], 'retained')
                self.note('STOCK_LOT_RETAINED' if row['filled_qty'] else 'STOCK_ACQUISITION_ENDED',
                          underlying=row['symbol'], shares=row['filled_qty'],
                          reason='Partial lot requires review' if row['filled_qty'] else 'No shares filled')
                continue
            context = json.loads(row['context'])
            age = (now - datetime.fromisoformat(row['created_at'])).total_seconds() / 60
            allocation = self.ledger.allocations().get(row['symbol'])
            if not allocation or allocation['shares'] != 100:
                self.ledger.finish_stock_plan(row['client_id'], 'retained')
                continue
            eligible = self.daily_signal(row['symbol'], now, previous_session)['eligible']
            if context.get('variant') == 'oasis':
                signal = get_oasis_signal_state(row['symbol'])
                eligible = eligible and signal.get('bullish', False) and entry_window(self.broker.clock())
            if age > self.settings.entry_timeout_minutes or not eligible:
                self.ledger.finish_stock_plan(row['client_id'], 'retained')
                self.note('STOCK_LOT_RETAINED', underlying=row['symbol'], shares=100,
                          reason='Setup expired; retain shares for a later qualifying call')
                continue
            # Rerun chain selection, event checks, actual cost basis, quote/capital/coverage
            # checks. Never sell the previously selected call without revalidation.
            self.enter_qualified(row['symbol'], context, now, today)
            if self.ledger.traded_bar(row['symbol'], context['date']):
                self.ledger.finish_stock_plan(row['client_id'], 'submitted')
