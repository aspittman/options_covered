"""Covered-call lifecycle: owned shares -> sell to open -> buy to close."""
import logging
from datetime import datetime, timezone

from events import event_block
from risk import (candidate_score, evaluate_candidate, capital_rejection, enum_value,
                  exit_reason, free_contracts, limit_price, number, parse_option, quote_prices, value)
from strategy import signal

LOG = logging.getLogger('options_covered')
ALLOWED_COVERED_CALL_INTENTS = frozenset(('sell_to_open', 'buy_to_close'))
LOW_PREMIUM_LIMIT = 500.0


def highlight_low_premium(symbol, underlying, mid, bid, ask):
    """Print a visible terminal marker without adding ANSI escapes to file logs."""
    premium = mid * 100
    if premium > LOW_PREMIUM_LIMIT:
        return False
    print(
        f'\033[1;32m★ LOW-PREMIUM COVERED CALL <= $500: '
        f'{symbol} ({underlying}) mid=${mid:.2f}, bid=${bid:.2f}, ask=${ask:.2f}, '
        f'estimated premium=${premium:.2f}\033[0m',
        flush=True,
    )
    return True


class CoveredCallBot:
    def __init__(self, broker, ledger, settings):
        self.broker, self.ledger, self.settings = broker, ledger, settings
        self.signal_cache = {}
        self.ledger.bind_capital(settings.virtual_starting_capital)

    def note(self, kind, **details):
        LOG.info('covered_call %s %s', kind, details)
        self.ledger.event(kind, **details)

    def reconcile(self, now):
        reliable = True
        for row in self.ledger.pending():
            try:
                order = self.broker.order(row)
                previous_filled = row['filled_qty']
                self.ledger.update(row['client_id'], order)
                status = enum_value(value(order, 'status'))
                filled = number(value(order, 'filled_qty') or 0)
                fill_price = value(order, 'filled_avg_price')
                if (row['intent'] == 'sell_to_open' and filled > previous_filled
                        and fill_price is not None and number(fill_price) * 100 <= LOW_PREMIUM_LIMIT):
                    print(
                        f'\033[1;32m★ LOW-PREMIUM COVERED CALL FILLED <= $500: '
                        f"{row['symbol']} ({row['underlying']}) fill=${number(fill_price):.2f}, "
                        f'estimated premium=${number(fill_price) * 100:.2f}\033[0m',
                        flush=True,
                    )
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

    def send(self, symbol, underlying, intent, qty, price, bar_date='', reason='', context=None):
        parsed = parse_option(symbol)
        if not parsed or parsed['kind'] != 'C' or parsed['underlying'] != underlying:
            raise ValueError('CoveredCallBot can buy or sell covered calls only; puts and stock orders are forbidden')
        if intent not in ALLOWED_COVERED_CALL_INTENTS:
            raise ValueError('CoveredCallBot permits only sell_to_open and buy_to_close intents')
        context = dict(context or {})
        if intent == 'sell_to_open':
            rejection, spot = self.entry_guard(symbol, underlying, qty)
            if rejection:
                self.reject(underlying, {'date': bar_date}, rejection, symbol=symbol, spot=spot)
                return False
            allocation = self.ledger.allocations()[underlying]
            context.update(capital_employed=allocation['cost_per_share'] * 100,
                           stock_price=spot, share_cost=allocation['cost_per_share'])
        elif intent == 'buy_to_close':
            lot = self.ledger.report()['lots'].get(symbol)
            positions = {value(p, 'symbol'): p for p in self.broker.positions()}
            if (not lot or qty <= 0 or qty > lot['qty'] or symbol not in positions
                    or number(value(positions[symbol], 'qty')) != -lot['qty']):
                raise ValueError('Cannot close a foreign or mismatched short-call position')
            if any(value(o, 'symbol') == symbol for o in self.broker.open_orders()):
                return False
            try:
                context['stock_price'] = self.broker.spot(underlying, datetime.now(timezone.utc))
            except Exception:
                context['stock_price'] = None  # A missing research mark must not block a risk exit.
        else:
            raise ValueError('Invalid covered-call order intent')
        if self.settings.dry_run:
            self.note('DRY_RUN_ORDER', symbol=symbol, intent=intent, qty=qty, price=price, reason=reason)
            return False
        client_id = self.ledger.prepare(symbol, underlying, intent, qty, price, bar_date, reason, context)
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
        self.reconcile_settlements()
        lots = self.ledger.report()['lots']
        positions = {value(p, 'symbol'): p for p in self.broker.positions()}
        orders = self.broker.open_orders()
        snapshots = self.broker.snapshots(list(lots)) if lots else {}
        reliable = True
        marks = {}
        stock_marks = {}
        for underlying, allocation in self.ledger.allocations().items():
            stock = positions.get(underlying)
            if not stock or number(value(stock, 'qty')) < allocation['shares']:
                reliable = False
                self.note('STOCK_ALLOCATION_MISMATCH', underlying=underlying)
                continue
            try:
                stock_marks[underlying] = self.broker.spot(underlying, now)
            except Exception as exc:
                self.note('STOCK_MARK_UNAVAILABLE', underlying=underlying, error=str(exc))
        for symbol, lot in lots.items():
            p = positions.get(symbol)
            # Broker changes can mean assignment, expiry or external trading.
            # Do not fabricate a buyback fill or claim the missing liability as profit.
            if p is None or number(value(p, 'qty')) != -lot['qty']:
                reliable = False
                self.note('POSITION_MISMATCH', symbol=symbol, tracked_qty=lot['qty'],
                          reason='Inspect broker assignment/expiry/external activity; entries paused')
                continue
            try:
                _, ask = quote_prices(value(snapshots.get(symbol), 'latest_quote'), now,
                                      self.settings.quote_age_seconds)
                marks[symbol] = ask
                if any(value(o, 'symbol') == symbol or value(o, 'legs') for o in orders):
                    continue
                if any(o['symbol'] == symbol for o in self.ledger.pending()):
                    continue
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
        self.note('PERFORMANCE', **self.ledger.research_report(self.settings, marks, stock_marks, record=True))
        return reliable

    def reconcile_settlements(self):
        lots = self.ledger.report()['lots']
        positions = {value(p, 'symbol'): p for p in self.broker.positions()}
        missing = {s: lot for s, lot in lots.items() if s not in positions}
        if not missing:
            return
        since = min(r['created_at'][:10] for r in self.ledger.orders())
        if not hasattr(self.broker, 'option_activities'):
            # Test doubles and alternate brokers may not expose activity history;
            # absence is uncertainty, never evidence of assignment or profit.
            self.note('SETTLEMENT_DATA_UNAVAILABLE', since=since)
            return
        activities = self.broker.option_activities(since)
        for symbol, lot in missing.items():
            events = [a for a in activities if a.get('symbol') == symbol
                      and a.get('activity_type') in {'OPASN', 'OPEXP'} and a.get('status') == 'executed'
                      and not self.ledger.db.execute('SELECT 1 FROM settlements WHERE activity_id=?', (a['id'],)).fetchone()]
            if len(events) != 1:
                continue
            event = events[0]
            parsed = parse_option(symbol)
            stock_trade = None
            if event['activity_type'] == 'OPASN':
                matches = [a for a in activities if a.get('activity_type') == 'OPTRD'
                           and a.get('symbol') == lot['underlying'] and a.get('date') == event.get('date')
                           and a.get('status') == 'executed' and number(a.get('qty', 0)) == -lot['qty'] * 100
                           and number(a.get('price', 0)) == parsed['strike']]
                # A same-day long-put exercise can produce the same stock sale.
                # Require a unique option event as well as a unique stock activity.
                competing = [a for a in activities if a.get('activity_type') in {'OPASN', 'OPEXC'}
                             and a.get('date') == event.get('date')
                             and (parse_option(a.get('symbol')) or {}).get('underlying') == lot['underlying']
                             and (parse_option(a.get('symbol')) or {}).get('strike') == parsed['strike']]
                if len(matches) != 1 or len(competing) != 1:
                    continue
                stock_trade = matches[0]
            try:
                if self.ledger.settle(event, stock_trade):
                    self.note('OPTION_SETTLED', symbol=symbol, activity_id=event['id'], kind=event['activity_type'])
            except (ValueError, KeyError) as exc:
                self.note('SETTLEMENT_UNCERTAIN', symbol=symbol, error=str(exc))

    def reject(self, underlying, state, reason, symbol=None, spot=None, snapshot=None, details=''):
        parsed = parse_option(symbol) or {}
        quote = value(snapshot, 'latest_quote')
        def numeric(field):
            try:
                return number(value(quote, field))
            except (ValueError, TypeError):
                return None
        bid, ask = numeric('bid_price'), numeric('ask_price')
        mid = (bid + ask) / 2 if bid is not None and ask is not None else None
        spread = ask - bid if mid is not None else None
        capital = self.ledger.capital_state(self.settings)
        self.ledger.reject(
            underlying=underlying, contract_symbol=symbol, strike=parsed.get('strike'),
            expiration=parsed.get('expiration'),
            DTE=(parsed['expiration'] - datetime.now(timezone.utc).date()).days if parsed else None,
            underlying_price=spot, bid=bid, ask=ask, mid=mid, spread_dollars=spread,
            spread_percent=spread / mid * 100 if mid and mid > 0 else None,
            option_premium=mid * 100 if mid is not None else None,
            required_capital=spot * 100 if spot is not None else None,
            virtual_capital_available=capital['virtual_capital_available'],
            rejection_reason=reason, signal_score=state.get('indicators'),
            market_regime='sideways', signal_date=state.get('date'), details=details)
        self.note('TRADE_REJECTED', underlying=underlying, symbol=symbol, reason=reason, details=details)

    def entry_guard(self, symbol, underlying, qty):
        # Enforced again inside send(), including direct callers. Account equity and
        # buying power never appear in the strategy's permitted-capital calculation.
        s = self.settings
        if qty != 1 or qty > s.max_contracts_per_trade:
            return 'MAX_CONTRACTS_REACHED', None
        self.account_ok(self.broker.account(), entry=True)
        now = datetime.now(timezone.utc)
        spot = self.broker.spot(underlying, now)
        allocations = self.ledger.allocations()
        allocation = allocations.get(underlying)
        cost = allocation['cost_per_share'] if allocation else spot
        marks = {u: self.broker.spot(u, now) for u in allocations}
        capital = self.ledger.capital_state(s, marks)
        prospective = capital['capital_employed'] + (max(spot, cost) * 100 if not allocation else 0)
        rejection = capital_rejection(spot, cost, prospective, capital['budget'], s)
        if rejection:
            return rejection, spot
        if not allocation or allocation['shares'] < 100:
            return 'NO_STRATEGY_CONTROLLED_SHARES', spot
        lots = self.ledger.report()['lots']
        pending = self.ledger.pending()
        if any(o['status'] == 'submission_unknown' for o in pending):
            return 'OTHER', spot
        # Legacy calls lacking stock allocations may still be closed safely, but
        # cannot be treated as zero-capital exposure for new entries.
        if any(lot['underlying'] not in allocations for lot in lots.values()):
            return 'MAX_STRATEGY_EXPOSURE_REACHED', spot
        entries = [o for o in pending if o['intent'] == 'sell_to_open']
        count = sum(lot['qty'] for lot in lots.values()) + sum(o['qty'] - o['filled_qty'] for o in entries)
        if count >= s.max_contracts:
            return 'MAX_CONTRACTS_REACHED', spot
        if any(lot['underlying'] == underlying for lot in lots.values()) or any(o['underlying'] == underlying for o in entries):
            return 'DUPLICATE_POSITION', spot
        positions, orders = self.broker.positions(), self.broker.open_orders()
        # Other puts/calls may coexist. An identical option symbol cannot safely
        # mix long/short ownership in Alpaca's netted position model.
        if any(value(p, 'symbol') == symbol for p in positions) or any(value(o, 'symbol') == symbol for o in orders):
            return 'DUPLICATE_POSITION', spot
        if free_contracts(underlying, positions, orders) < 1:
            return 'INSUFFICIENT_COVERAGE', spot
        return '', spot

    def enter(self, underlying, now, today, previous_session):
        state = self.daily_signal(underlying, now, previous_session)
        if not state['eligible']:
            self.note('SKIP', underlying=underlying, **state)
            return
        try:
            self.enter_qualified(underlying, state, now, today)
        except Exception as exc:
            self.reject(underlying, state, 'OTHER', details=str(exc))
            raise  # Preserve fail-closed submission/reconciliation behavior.

    def enter_qualified(self, underlying, state, now, today):
        s = self.settings
        if self.ledger.traded_bar(underlying, state['date']):
            self.reject(underlying, state, 'DUPLICATE_POSITION', details='Daily signal already attempted')
            return
        if self.ledger.cooling_down(underlying, today, s.cooldown_days):
            self.reject(underlying, state, 'OTHER', details='Reentry cooldown')
            return
        allocation = self.ledger.allocations().get(underlying)
        spot = self.broker.spot(underlying, now)
        cost_basis = allocation['cost_per_share'] if allocation else spot
        contracts = self.broker.contracts(underlying, spot, today)
        symbols = [c.symbol for c in contracts]
        snapshots = self.broker.snapshots(symbols)
        volumes = self.broker.volumes(symbols, today)
        now = datetime.now(timezone.utc)
        ranked, rejected = [], []
        for c in contracts:
            if value(c, 'underlying_symbol') != underlying:
                continue
            blocked = event_block(s.events_path, underlying, today, c.expiration_date)
            score, reason = evaluate_candidate(c, snapshots.get(c.symbol), volumes.get(c.symbol, 0),
                                               spot, cost_basis, today, now, s)
            if blocked:
                rejected.append((c, 'OTHER', blocked))
            elif score is None:
                rejected.append((c, reason, 'Contract quality filter'))
            else:
                ranked.append((score, c))
        if not ranked:
            # Candidate-level records are distinct from executed trades. They
            # retain the reason rather than disguising liquidity failures as budget failures.
            for c, reason, details in rejected:
                self.reject(underlying, state, reason, c.symbol, spot, snapshots.get(c.symbol), details)
            if not rejected:
                self.reject(underlying, state, 'NO_VALID_CONTRACT', spot=spot)
            return
        _, contract = min(ranked, key=lambda pair: pair[0])
        # Selection above is unchanged by capital. Never substitute a cheaper or
        # inferior contract when the selected trade exceeds the strategy allocation.
        reason, spot = self.entry_guard(contract.symbol, underlying, 1)
        if reason:
            self.reject(underlying, state, reason, contract.symbol, spot, snapshots.get(contract.symbol))
            return
        snapshot = self.broker.snapshots([contract.symbol]).get(contract.symbol)
        now = datetime.now(timezone.utc)
        score, reason = evaluate_candidate(contract, snapshot, volumes.get(contract.symbol, 0),
                                           spot, cost_basis, today, now, s)
        if reason:
            self.reject(underlying, state, reason, contract.symbol, spot, snapshot, 'Final quote recheck')
            return
        bid, ask = quote_prices(value(snapshot, 'latest_quote'), now, s.quote_age_seconds)
        mid = (bid + ask) / 2
        highlight_low_premium(contract.symbol, underlying, mid, bid, ask)
        self.send(contract.symbol, underlying, 'sell_to_open', 1,
                  limit_price(mid), state['date'])

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
