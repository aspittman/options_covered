"""Covered-call lifecycle: owned shares -> sell to open -> buy to close."""
import logging
from datetime import datetime, timezone

from events import event_block
from risk import (candidate_score, enum_value, exit_reason, free_contracts, limit_price,
                  number, parse_option, quote_prices, value)
from strategy import signal

LOG = logging.getLogger('options_covered')


class CoveredCallBot:
    def __init__(self, broker, ledger, settings):
        self.broker, self.ledger, self.settings = broker, ledger, settings
        self.signal_cache = {}

    def note(self, kind, **details):
        LOG.info('%s %s', kind, details)
        self.ledger.event(kind, **details)

    def reconcile(self, now):
        reliable = True
        for row in self.ledger.pending():
            try:
                order = self.broker.order(row)
                self.ledger.update(row['client_id'], order)
                status = enum_value(value(order, 'status'))
                from analytics import TERMINAL
                if status not in TERMINAL:
                    age = (now - datetime.fromisoformat(row['created_at'])).total_seconds() / 60
                    timeout = (self.settings.entry_timeout_minutes if row['intent'] == 'sell_to_open'
                               else self.settings.exit_timeout_minutes)
                    if age >= timeout and status != 'pending_cancel' and not self.settings.dry_run:
                        self.broker.cancel(str(value(order, 'id')))
                        # A cancellation request is not a terminal broker acknowledgment.
                        self.note('CANCEL_REQUESTED', client_id=row['client_id'])
            except Exception as exc:
                reliable = False
                self.note('RECONCILIATION_UNCERTAIN', client_id=row['client_id'], error=str(exc))
        return reliable

    def send(self, symbol, underlying, intent, qty, price, bar_date='', reason=''):
        if self.settings.dry_run:
            self.note('DRY_RUN_ORDER', symbol=symbol, intent=intent, qty=qty, price=price, reason=reason)
            return False
        client_id = self.ledger.prepare(symbol, underlying, intent, qty, price, bar_date, reason)
        try:
            order = self.broker.submit(client_id, symbol, qty, intent, price)
            self.ledger.update(client_id, order)
            self.note('ORDER_SUBMITTED', client_id=client_id, symbol=symbol, intent=intent)
            return True
        except Exception:
            self.note('SUBMISSION_UNKNOWN', client_id=client_id,
                      reason='Retain intent and look up by client ID; do not resubmit')
            raise

    def daily_signal(self, symbol, now, previous_session):
        key = (symbol, previous_session)
        if key not in self.signal_cache:
            result = signal(self.broker.bars(symbol, now), self.settings, previous_session)
            if 'date' in result:
                self.signal_cache[key] = result
            return result
        return self.signal_cache[key]

    def account_ok(self, account, entry=False):
        if value(account, 'trading_blocked', True) or value(account, 'account_blocked', True):
            raise RuntimeError('Account trading is blocked')
        if entry and number(value(account, 'options_trading_level', 0) or 0) < 1:
            raise RuntimeError('Account lacks covered-call options approval')

    def manage(self, now, today):
        lots = self.ledger.report()['lots']
        positions = {value(p, 'symbol'): p for p in self.broker.positions()}
        orders = self.broker.open_orders()
        snapshots = self.broker.snapshots(list(lots)) if lots else {}
        reliable = True
        marks = {}
        for symbol, lot in lots.items():
            p = positions.get(symbol)
            # Broker changes can mean assignment, expiry or external trading.
            # Do not fabricate a buyback fill or claim the missing liability as profit.
            if p is None or number(value(p, 'qty')) != -lot['qty']:
                reliable = False
                self.note('POSITION_MISMATCH', symbol=symbol, tracked_qty=lot['qty'],
                          reason='Inspect broker assignment/expiry/external activity; entries paused')
                continue
            if any(value(o, 'symbol') == symbol or value(o, 'legs') for o in orders):
                continue
            if any(o['symbol'] == symbol for o in self.ledger.pending()):
                continue
            try:
                _, ask = quote_prices(value(snapshots.get(symbol), 'latest_quote'), now,
                                      self.settings.quote_age_seconds)
                marks[symbol] = ask
                parsed = parse_option(symbol)
                if not parsed or parsed['kind'] != 'C':
                    raise ValueError('Tracked contract is not a standard call')
                dte = (parsed['expiration'] - today).days
                reason = exit_reason(lot['credit'] / lot['qty'], ask, dte, self.settings)
                # Lost collateral is urgent; buy back the call, never sell its stock.
                stock = positions.get(lot['underlying'])
                shares = max(0, number(value(stock, 'qty', 0)))
                calls = sum(abs(number(value(pos, 'qty'))) for pos in positions.values()
                            if (parse_option(value(pos, 'symbol')) or {}).get('underlying') == lot['underlying']
                            and (parse_option(value(pos, 'symbol')) or {}).get('kind') == 'C'
                            and number(value(pos, 'qty')) < 0)
                if shares < calls * 100:
                    reason = 'collateral_shortfall'
                    reliable = False
                if reason:
                    self.send(symbol, lot['underlying'], 'buy_to_close', lot['qty'],
                              limit_price(ask, closing=True), reason=reason)
            except (ValueError, TypeError) as exc:
                self.note('EXIT_DATA_UNAVAILABLE', symbol=symbol, error=str(exc))
        self.note('PERFORMANCE', **self.ledger.report(marks))
        return reliable

    def enter(self, underlying, now, today, previous_session):
        s = self.settings
        state = self.daily_signal(underlying, now, previous_session)
        if not state['eligible']:
            self.note('SKIP', underlying=underlying, **state)
            return
        if self.ledger.traded_bar(underlying, state['date']) or self.ledger.cooling_down(underlying, today, s.cooldown_days):
            return
        positions, orders = self.broker.positions(), self.broker.open_orders()
        if free_contracts(underlying, positions, orders) < 1:
            self.note('SKIP', underlying=underlying, reason='no_unreserved_100_share_lot')
            return
        if any((parse_option(value(p, 'symbol')) or {}).get('underlying') == underlying for p in positions):
            # Separate ownership of identical contracts cannot be proved in a netted account.
            self.note('SKIP', underlying=underlying, reason='existing_option_exposure')
            return
        if any(value(o, 'symbol') == underlying or
               (parse_option(value(o, 'symbol')) or {}).get('underlying') == underlying for o in orders):
            return
        stock = next(p for p in positions if value(p, 'symbol') == underlying)
        cost_basis = number(value(stock, 'avg_entry_price'))
        spot = self.broker.spot(underlying, now)
        contracts = self.broker.contracts(underlying, spot, today)
        symbols = [c.symbol for c in contracts]
        snapshots = self.broker.snapshots(symbols)
        volumes = self.broker.volumes(symbols, today)
        now = datetime.now(timezone.utc)
        ranked = []
        event_blocks = {}
        for c in contracts:
            if value(c, 'underlying_symbol') != underlying:
                continue
            blocked = event_block(s.events_path, underlying, today, c.expiration_date)
            if blocked:
                event_blocks[blocked] = event_blocks.get(blocked, 0) + 1
                continue
            score = candidate_score(c, snapshots.get(c.symbol), volumes.get(c.symbol, 0),
                                    spot, cost_basis, today, now, s)
            if score is not None:
                ranked.append((score, c))
        if not ranked:
            self.note('SKIP', underlying=underlying,
                      reason='no_contract_passes_liquidity_delta_events_and_strike_filters',
                      event_blocks=event_blocks)
            return
        _, contract = min(ranked, key=lambda pair: pair[0])
        # Recheck holdings, pending reservations, latest price and quote at submission.
        self.account_ok(self.broker.account(), entry=True)
        positions, orders = self.broker.positions(), self.broker.open_orders()
        pending = self.ledger.pending()
        if any(o['status'] == 'submission_unknown' for o in pending):
            raise RuntimeError('Unresolved submission blocks further entries')
        if any(value(o, 'symbol') == underlying or
               (parse_option(value(o, 'symbol')) or {}).get('underlying') == underlying for o in orders):
            return
        if any((parse_option(value(p, 'symbol')) or {}).get('underlying') == underlying for p in positions):
            return
        lots = self.ledger.report()['lots']
        pending_entries = [o for o in pending if o['intent'] == 'sell_to_open']
        count = sum(lot['qty'] for lot in lots.values()) + sum(o['qty'] - o['filled_qty'] for o in pending_entries)
        if count >= s.max_contracts:
            return
        if any(o['underlying'] == underlying for o in pending_entries):
            return
        stock = next((p for p in positions if value(p, 'symbol') == underlying), None)
        if stock is None:
            return
        spot = self.broker.spot(underlying, now)
        reserved_value = 0
        for symbol in {lot['underlying'] for lot in lots.values()} | {o['underlying'] for o in pending_entries}:
            qty = sum(lot['qty'] for lot in lots.values() if lot['underlying'] == symbol)
            qty += sum(o['qty'] - o['filled_qty'] for o in pending_entries if o['underlying'] == symbol)
            reserved_value += qty * 100 * self.broker.spot(symbol, now)
        if reserved_value + 100 * spot > s.max_covered_value:
            return
        if free_contracts(underlying, positions, orders) < 1:
            return
        snapshot = self.broker.snapshots([contract.symbol]).get(contract.symbol)
        now = datetime.now(timezone.utc)
        if candidate_score(contract, snapshot, volumes.get(contract.symbol, 0), spot,
                           number(value(stock, 'avg_entry_price')), today, now, s) is None:
            return
        bid, ask = quote_prices(value(snapshot, 'latest_quote'), now, s.quote_age_seconds)
        self.send(contract.symbol, underlying, 'sell_to_open', 1, limit_price((bid + ask) / 2), state['date'])

    def cycle(self):
        now = datetime.now(timezone.utc)
        account = self.broker.account()
        self.ledger.bind_account(str(value(account, 'id')), self.settings.paper)
        if not self.reconcile(now):
            self.note('CYCLE_PAUSED', reason='unresolved_order_state')
            return
        clock = self.broker.clock()
        if not clock.is_open:
            self.note('MARKET_CLOSED')
            return
        self.account_ok(account)
        from broker import NY
        today = now.astimezone(NY).date()
        if not self.manage(now, today):
            return
        if not self.settings.enable_new_entries:
            self.note('ENTRIES_DISABLED')
            return
        self.account_ok(account, entry=True)
        previous_session = self.broker.previous_session(today)
        if self.settings.market_filter:
            state = self.daily_signal(self.settings.market_symbol, now, previous_session)
            if not state['eligible']:
                self.note('MARKET_FILTER', **state)
                return
        for underlying in self.settings.underlyings:
            self.enter(underlying, now, today, previous_session)
        self.note('CYCLE_COMPLETE')
