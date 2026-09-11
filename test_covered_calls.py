import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as Obj
from unittest.mock import patch

import numpy as np
import pandas as pd

from analytics import Ledger
from backtester import call_price, simulate
from config import Settings
from events import event_block
from options_trader import CoveredCallBot
from risk import candidate_score, exit_reason, free_contracts, limit_price, quote_prices
from strategy import signal, sideways

NOW = datetime.now(timezone.utc)
TODAY = NOW.date()
EXPIRY = TODAY + timedelta(days=35)
SYMBOL = f'SPY{EXPIRY:%y%m%d}C00105000'


def position(symbol='SPY', qty=100, **kwargs):
    return Obj(symbol=symbol, qty=str(qty), avg_entry_price='95', **kwargs)


def order(symbol=SYMBOL, side='sell', qty=1, filled_qty=0, **kwargs):
    return Obj(symbol=symbol, side=side, qty=str(qty), filled_qty=str(filled_qty), **kwargs)


def snapshot(bid=1, ask=1.05, delta=.25, stamp=NOW):
    return Obj(latest_quote=Obj(bid_price=bid, ask_price=ask, timestamp=stamp), greeks=Obj(delta=delta))


def contract(**kwargs):
    data = dict(symbol=SYMBOL, tradable=True, size='100', underlying_symbol='SPY',
                type='call', strike_price='105', open_interest='1000', expiration_date=EXPIRY)
    return Obj(**{**data, **kwargs})


def history(n=160, trend=False):
    close = np.linspace(80, 120, n) if trend else 100 + np.sin(np.arange(n) * .8) * 2
    return pd.DataFrame({'open': close, 'high': close + 1, 'low': close - 1, 'close': close},
                        index=pd.date_range('2025-01-01', periods=n, freq='B'))


class RiskTests(unittest.TestCase):
    def test_research_capital_constants(self):
        from config import (MAX_CONTRACTS_PER_TRADE, MAX_UNDERLYING_VALUE_PER_POSITION,
                            VIRTUAL_STARTING_CAPITAL)
        self.assertEqual((VIRTUAL_STARTING_CAPITAL, MAX_CONTRACTS_PER_TRADE,
                          MAX_UNDERLYING_VALUE_PER_POSITION), (25000, 1, 25000))

    def test_expanded_universe_contains_lower_notional_candidates(self):
        from config import Settings
        universe = Settings().underlyings
        for symbol in ('XLF', 'XLE', 'XBI', 'INTC', 'F', 'SOFI', 'OXY'):
            self.assertIn(symbol, universe)

    def test_whole_owned_lots_only(self):
        for shares, expected in [(99.99, 0), (100, 1), (250, 2), (-100, 0)]:
            self.assertEqual(free_contracts('SPY', [position(qty=shares)], []), expected)

    def test_external_short_and_pending_call_reserve_shares(self):
        self.assertEqual(free_contracts('SPY', [position(qty=300), position(SYMBOL, -1)], [order()]), 1)

    def test_partial_fill_not_double_counted(self):
        self.assertEqual(free_contracts('SPY', [position(qty=300), position(SYMBOL, -1)],
                                        [order(qty=2, filled_qty=1)]), 1)

    def test_pending_close_does_not_free_collateral(self):
        self.assertEqual(free_contracts('SPY', [position(), position(SYMBOL, -1)],
                                        [order(side='buy')]), 0)

    def test_stock_sale_reserved_and_purchase_not_counted(self):
        self.assertEqual(free_contracts('SPY', [position()], [order('SPY', 'sell', 1)]), 0)
        self.assertEqual(free_contracts('SPY', [], [order('SPY', 'buy', 100)]), 0)

    def test_available_zero_is_not_replaced_with_qty(self):
        self.assertEqual(free_contracts('SPY', [position(qty_available='0')], []), 0)

    def test_multi_leg_or_adjusted_contract_fails_closed(self):
        with self.assertRaises(ValueError):
            free_contracts('SPY', [position()], [order(legs=[Obj()])])
        with self.assertRaises(ValueError):
            free_contracts('SPY', [position(), position('SPY1261016C00100000', -1, asset_class='us_option')], [])

    def test_nonfinite_quantity_rejected(self):
        with self.assertRaises(ValueError):
            free_contracts('SPY', [position(qty='nan')], [])

    def test_quotes_fail_closed(self):
        for snap in (snapshot(2, 1), snapshot(stamp=NOW - timedelta(minutes=5)), snapshot(ask=float('nan'))):
            with self.assertRaises(ValueError):
                quote_prices(snap.latest_quote, NOW, 120)

    def test_zero_bid_can_close_but_cannot_open(self):
        snap = snapshot(0, .05)
        self.assertEqual(quote_prices(snap.latest_quote, NOW, 120), (0, .05))
        self.assertIsNone(candidate_score(contract(), snap, 500, 100, 95, TODAY, NOW, Settings()))

    def test_contract_filters(self):
        s = Settings()
        self.assertIsNotNone(candidate_score(contract(), snapshot(), 500, 100, 95, TODAY, NOW, s))
        for c, snap, vol, spot, basis in [
            (contract(size=10), snapshot(), 500, 100, 95),
            (contract(), snapshot(delta=.6), 500, 100, 95),
            (contract(), snapshot(), 0, 100, 95),
            (contract(), snapshot(), 500, 106, 95),
            (contract(), snapshot(), 500, 100, 110),
            (contract(), snapshot(bid=.1, ask=.15), 500, 100, 95),
        ]:
            self.assertIsNone(candidate_score(c, snap, vol, spot, basis, TODAY, NOW, s))

    def test_short_profit_and_stop_direction(self):
        self.assertEqual(exit_reason(1, .5, 20, Settings()), 'premium_profit_target')
        self.assertEqual(exit_reason(1, 2, 20, Settings()), 'short_call_stop')
        self.assertEqual(exit_reason(1, 1, 7, Settings()), 'expiration_management')
        self.assertEqual(exit_reason(1, 1, 20, Settings()), '')

    def test_tick_rounding(self):
        self.assertEqual(limit_price(1.03), 1)
        self.assertEqual(limit_price(1.03, closing=True), 1.05)
        self.assertEqual(limit_price(3.04, closing=True), 3.1)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'ledger.sqlite3'
        self.ledger = Ledger(self.path)

    def tearDown(self):
        self.ledger.db.close()
        self.temp.cleanup()

    def fill(self, client, qty, price, status='filled', side='sell'):
        self.ledger.update(client, order(side=side, qty=2, filled_qty=qty, filled_avg_price=price,
                                        id='broker-id', client_order_id=client, status=status))

    def test_incremental_partial_fills_and_restart(self):
        client = self.ledger.prepare(SYMBOL, 'SPY', 'sell_to_open', 2, 1)
        self.fill(client, 1, 1, 'partially_filled')
        self.fill(client, 1, 1, 'partially_filled')
        self.ledger.db.close()
        self.ledger = Ledger(self.path)
        self.fill(client, 2, 1.25)
        lot = self.ledger.report()['lots'][SYMBOL]
        self.assertEqual((lot['qty'], lot['credit']), (2, 2.5))
        client = self.ledger.prepare(SYMBOL, 'SPY', 'buy_to_close', 1, .5)
        self.fill(client, 1, .5, side='buy')
        report = self.ledger.report({SYMBOL: 1})
        self.assertEqual(report['realized_option_pnl'], 75)
        self.assertEqual(report['marked_unrealized_option_pnl'], 25)

    def test_partial_cancel_retains_filled_lot(self):
        client = self.ledger.prepare(SYMBOL, 'SPY', 'sell_to_open', 2, 1)
        self.fill(client, 1, 1, 'canceled')
        self.assertFalse(self.ledger.pending())
        self.assertEqual(self.ledger.report()['lots'][SYMBOL]['qty'], 1)

    def test_pending_cancel_stays_reserved(self):
        client = self.ledger.prepare(SYMBOL, 'SPY', 'sell_to_open', 2, 1)
        self.fill(client, 0, 0, 'pending_cancel')
        self.assertEqual(len(self.ledger.pending()), 1)

    def test_unknown_intent_survives_restart(self):
        self.ledger.prepare(SYMBOL, 'SPY', 'sell_to_open', 1, 1, '2026-09-04')
        self.ledger.db.close()
        self.ledger = Ledger(self.path)
        self.assertEqual(self.ledger.pending()[0]['status'], 'submission_unknown')
        self.assertTrue(self.ledger.traded_bar('SPY', '2026-09-04'))

    def test_cross_account_ledger_blocked(self):
        self.ledger.bind_account('one', True)
        self.ledger.bind_account('one', True)
        with self.assertRaises(RuntimeError):
            self.ledger.bind_account('one', False)

    def test_allocation_and_rejection_are_persistent(self):
        settings = Settings()
        self.ledger.allocate_shares('QQQ', 100, 100, 110, settings)
        state = self.ledger.capital_state(settings, {'QQQ': 110})
        self.assertEqual(state['capital_employed'], 20500 if 'SPY' in self.ledger.allocations() else 11000)
        self.ledger.reject(underlying='SPY', contract_symbol=SYMBOL,
                           rejection_reason='UNDERLYING_VALUE_OVER_LIMIT', signal_date='2026-09-09')
        self.ledger.export_rejections(self.path.parent / 'rejected.csv')
        rows = (self.path.parent / 'rejected.csv').read_text().splitlines()
        self.assertIn('UNDERLYING_VALUE_OVER_LIMIT', rows[-1])

    def test_allocation_rejects_share_value_over_virtual_capital(self):
        with self.assertRaisesRegex(ValueError, 'UNDERLYING_VALUE_OVER_LIMIT'):
            self.ledger.allocate_shares('QQQ', 100, 250, 251, Settings())


class FakeBroker:
    def __init__(self):
        self.holdings = [position()]
        self.orders = []
        self.submissions = []
        self.cancellations = []
        self.snap = snapshot()
        self.fail_submit = False
        self.fail_lookup = False

    def account(self):
        return Obj(id='test-account', trading_blocked=False, account_blocked=False, options_trading_level=1)

    def positions(self):
        return self.holdings

    def open_orders(self):
        return self.orders

    def snapshots(self, symbols):
        return {s: self.snap for s in symbols}

    def spot(self, symbol, now):
        return 100

    def contracts(self, symbol, spot, today):
        return [contract()]

    def volumes(self, symbols, today):
        return {s: 500 for s in symbols}

    def submit(self, client_id, symbol, qty, intent, price):
        self.submissions.append((symbol, qty, intent, price))
        if self.fail_submit:
            raise TimeoutError('Response lost after submission')
        submitted = order(symbol, 'sell' if intent == 'sell_to_open' else 'buy', qty,
                          id='order-id', client_order_id=client_id, status='new', filled_avg_price=None)
        self.orders.append(submitted)
        return submitted

    def order(self, row):
        if self.fail_lookup:
            raise TimeoutError('Broker unavailable')
        return next(o for o in self.orders if o.client_order_id == row['client_id'])

    def cancel(self, order_id):
        self.cancellations.append(order_id)


class BotTests(LedgerTests):
    def setUp(self):
        super().setUp()
        self.broker = FakeBroker()
        self.settings = Settings(dry_run=False, enable_new_entries=True)
        self.bot = CoveredCallBot(self.broker, self.ledger, self.settings)
        self.ledger.allocate_shares('SPY', 100, 95, 100, self.settings)
        self.bot.daily_signal = lambda *args: {'eligible': True, 'date': TODAY.isoformat()}

    def enter(self):
        with patch('options_trader.event_block', return_value=''):
            self.bot.enter('SPY', NOW, TODAY, TODAY)

    def test_entry_is_sell_to_open_and_no_stock_order(self):
        self.enter()
        self.assertEqual(self.broker.submissions, [(SYMBOL, 1, 'sell_to_open', 1)])
        self.enter()
        self.assertEqual(len(self.broker.submissions), 1)

    def test_insufficient_shares_prevent_order(self):
        self.broker.holdings = [position(qty=99)]
        self.enter()
        self.assertFalse(self.broker.submissions)

    def test_existing_external_option_not_adopted(self):
        self.broker.holdings.append(position(SYMBOL, -1))
        self.bot.manage(NOW, TODAY)
        self.assertFalse(self.broker.submissions)
        self.assertFalse(self.ledger.report()['lots'])

    def test_unknown_submit_retained_and_no_retry(self):
        self.broker.fail_submit = True
        with self.assertRaises(TimeoutError):
            self.enter()
        self.broker.fail_lookup = True
        self.assertFalse(self.bot.reconcile(NOW))
        self.enter()
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(len(self.ledger.pending()), 1)

    def seed_short(self):
        client = self.ledger.prepare(SYMBOL, 'SPY', 'sell_to_open', 1, 1)
        self.fill(client, 1, 1)
        self.broker.holdings.append(position(SYMBOL, -1))

    def test_profit_exit_buys_to_close_without_selling_shares(self):
        self.seed_short()
        self.broker.snap = snapshot(.4, .45)
        self.bot.manage(NOW, TODAY)
        self.assertEqual(self.broker.submissions, [(SYMBOL, 1.0, 'buy_to_close', .45)])

    def test_missing_position_does_not_fabricate_profit(self):
        self.seed_short()
        self.broker.holdings = [position()]
        self.assertFalse(self.bot.manage(NOW, TODAY))
        self.assertEqual(self.ledger.report()['realized_option_pnl'], 0)
        self.assertIn(SYMBOL, self.ledger.report()['lots'])

    def test_dry_run_submits_nothing_including_exits(self):
        self.bot.settings = replace(self.settings, dry_run=True)
        self.enter()
        self.seed_short()
        self.broker.snap = snapshot(.4, .45)
        self.bot.manage(NOW, TODAY)
        self.assertFalse(self.broker.submissions)

    def test_cancellation_waits_for_broker_confirmation(self):
        self.enter()
        self.bot.reconcile(NOW + timedelta(minutes=20))
        self.assertEqual(self.broker.cancellations, ['order-id'])
        self.assertEqual(len(self.ledger.pending()), 1)

    def test_stock_value_cap(self):
        self.bot.settings = replace(self.settings, max_covered_value=9999)
        self.enter()
        self.assertFalse(self.broker.submissions)


class StrategyAndBacktestTests(unittest.TestCase):
    def test_nonfinite_configuration_rejected(self):
        with self.assertRaises(ValueError):
            Settings(max_covered_value=float('nan'))

    def test_verified_events_cover_entire_call(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.json'
            entry = {'verified_on': TODAY.isoformat(), 'valid_through': EXPIRY.isoformat(),
                     'earnings': [], 'ex_dividend': []}
            path.write_text(json.dumps({'SPY': entry}))
            self.assertEqual(event_block(path, 'SPY', TODAY, EXPIRY), '')
            entry['ex_dividend'] = [(TODAY + timedelta(days=10)).isoformat()]
            path.write_text(json.dumps({'SPY': entry}))
            self.assertEqual(event_block(path, 'SPY', TODAY, EXPIRY), 'ex_dividend_before_expiration')
            entry['ex_dividend'] = []
            entry['valid_through'] = TODAY.isoformat()
            path.write_text(json.dumps({'SPY': entry}))
            self.assertEqual(event_block(path, 'SPY', TODAY, EXPIRY), 'event_calendar_does_not_cover_expiration')

    def test_sideways_metrics_and_trend_rejection(self):
        s = Settings()
        row = Obj(close=100, adx=15, rsi=50, slope=.002, distance=.01)
        self.assertTrue(sideways(pd.Series(vars(row)), s))
        row.adx = 40
        self.assertFalse(sideways(pd.Series(vars(row)), s))
        bars = history(trend=True)
        self.assertFalse(signal(bars, s, bars.index[-1].date())['eligible'])

    def test_incomplete_bar_ignored_and_stale_data_rejected(self):
        bars = history()
        expected = bars.index[-2].date()
        original = signal(bars, Settings(), expected)
        bars.iloc[-1] = 99999
        self.assertEqual(original, signal(bars, Settings(), expected))
        self.assertFalse(signal(bars.iloc[:-2], Settings(), expected)['eligible'])

    def test_backtest_includes_stock_losses(self):
        bars = history(trend=True).iloc[::-1].copy()
        bars.index = pd.date_range('2025-01-01', periods=len(bars), freq='B')
        summary, _, curve = simulate(bars, Settings(), 20000)
        self.assertLess(summary['total_pnl'], 0)
        self.assertAlmostEqual(summary['total_pnl'], summary['buy_hold_pnl'])
        self.assertTrue((curve.call_liability >= 0).all())

    def test_simulation_produces_calls_and_reconciles_equity(self):
        summary, trades, curve = simulate(history(), Settings(min_yield=.0001, min_credit=.01), 20000)
        self.assertGreater(len(trades), 0)
        self.assertAlmostEqual(summary['total_pnl'], summary['buy_hold_pnl'] + summary['option_pnl'])
        self.assertAlmostEqual(curve.call_liability.iloc[-1], 0)

    def test_not_enough_cash_for_100_shares(self):
        with self.assertRaises(ValueError):
            simulate(history(), Settings(), 100)

    def test_expired_call_value(self):
        self.assertEqual(call_price(110, 100, 0, .2), 10)
        self.assertEqual(call_price(90, 100, 0, .2), 0)

    def test_missing_event_calendar_blocks(self):
        self.assertEqual(event_block(Path('/tmp/nonexistent-options-covered-events.json'), 'SPY', TODAY, EXPIRY),
                         'event_calendar_unavailable')


class BrokerAdapterTests(unittest.TestCase):
    def test_stale_underlying_data_is_a_symbol_scoped_condition(self):
        from broker import AlpacaBroker, MarketDataUnavailable
        from unittest.mock import Mock
        adapter = AlpacaBroker.__new__(AlpacaBroker)
        adapter.settings = Settings(quote_age_seconds=120)
        adapter.stocks = Mock()
        adapter.stocks.get_stock_latest_trade.return_value = {
            'SPY': Obj(price=100, timestamp=NOW - timedelta(minutes=5))}
        with self.assertRaises(MarketDataUnavailable):
            adapter.spot('SPY', NOW)

    def test_only_covered_call_order_types_are_allowed(self):
        from unittest.mock import Mock
        from broker import AlpacaBroker
        adapter = AlpacaBroker.__new__(AlpacaBroker)
        adapter.trading = Mock()
        put_symbol = SYMBOL.replace('C', 'P', 1)
        for symbol, intent in ((put_symbol, 'sell_to_open'), (SYMBOL, 'buy_to_open'),
                               ('SPY', 'sell_to_open')):
            with self.assertRaises(ValueError):
                adapter.submit('covered_call_test', symbol, 1, intent, 1)

    def test_low_premium_terminal_marker(self):
        from io import StringIO
        from unittest.mock import patch
        from options_trader import highlight_low_premium
        with patch('sys.stdout', new_callable=StringIO) as stdout:
            self.assertTrue(highlight_low_premium(SYMBOL, 'SPY', 4.99, 4.9, 5.08))
        self.assertIn('LOW-PREMIUM COVERED CALL <= $500', stdout.getvalue())
        with patch('sys.stdout', new_callable=StringIO) as stdout:
            self.assertFalse(highlight_low_premium(SYMBOL, 'SPY', 5.01, 5, 5.02))
        self.assertEqual(stdout.getvalue(), '')

    def test_sdk_serializes_open_and_close_intents(self):
        from unittest.mock import Mock
        from broker import AlpacaBroker
        adapter = AlpacaBroker.__new__(AlpacaBroker)
        adapter.trading = Mock()
        for intent, side in [('sell_to_open', 'sell'), ('buy_to_close', 'buy')]:
            adapter.submit('oc-test', SYMBOL, 1, intent, 1.05)
            payload = adapter.trading.submit_order.call_args.args[0].to_request_fields()
            self.assertEqual(payload['side'], side)
            self.assertEqual(payload['position_intent'], intent)
            self.assertEqual(payload['time_in_force'], 'day')
            self.assertEqual(payload['type'], 'limit')

    def test_open_order_cap_fails_closed(self):
        from unittest.mock import Mock
        from broker import AlpacaBroker
        adapter = AlpacaBroker.__new__(AlpacaBroker)
        adapter.trading = Mock()
        adapter.trading.get_orders.return_value = [order()] * 500
        with self.assertRaises(RuntimeError):
            adapter.open_orders()

    def test_clock_retries_transient_alpaca_failure(self):
        from unittest.mock import Mock, patch
        from broker import AlpacaBroker
        adapter = AlpacaBroker.__new__(AlpacaBroker)
        adapter.trading = Mock()
        adapter.trading.get_clock.side_effect = [RuntimeError('500 Internal Server Error'),
                                                 RuntimeError('500 Internal Server Error'),
                                                 Obj(is_open=True)]
        with patch('broker.time.sleep') as sleep:
            self.assertTrue(adapter.clock().is_open)
        self.assertEqual(adapter.trading.get_clock.call_count, 3)
        self.assertEqual(sleep.call_args_list[0].args, (2,))
        self.assertEqual(sleep.call_args_list[1].args, (5,))

    def test_clock_raises_after_bounded_retries(self):
        from unittest.mock import Mock, patch
        from broker import AlpacaBroker
        adapter = AlpacaBroker.__new__(AlpacaBroker)
        adapter.trading = Mock()
        adapter.trading.get_clock.side_effect = RuntimeError('500 Internal Server Error')
        with patch('broker.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, '500'):
                adapter.clock()
        self.assertEqual(adapter.trading.get_clock.call_count, 3)

    def test_contract_pagination(self):
        from unittest.mock import Mock
        from broker import AlpacaBroker
        adapter = AlpacaBroker.__new__(AlpacaBroker)
        adapter.settings = Settings()
        adapter.trading = Mock()
        adapter.trading.get_option_contracts.side_effect = [
            Obj(option_contracts=[contract()], next_page_token='next'),
            Obj(option_contracts=[contract()], next_page_token=None)]
        self.assertEqual(len(adapter.contracts('SPY', 100, TODAY)), 2)
        self.assertEqual(adapter.trading.get_option_contracts.call_count, 2)


class StartupTests(unittest.TestCase):
    def test_invalid_expiration_reports_values(self):
        with self.assertRaisesRegex(ValueError, 'EXIT_DTE=30, MIN_DTE=30, MAX_DTE=45'):
            Settings(exit_dte=30)

    def test_cli_configuration_error_stops_before_broker_access(self):
        import main
        with patch('sys.argv', ['main.py']), \
                patch('main.Settings.from_env', side_effect=ValueError('Invalid expiration settings')), \
                patch('main.AlpacaBroker') as broker, patch('sys.stderr'):
            with self.assertRaises(SystemExit) as result:
                main.main()
            self.assertEqual(result.exception.code, 2)
            broker.assert_not_called()

    def test_launcher_does_not_retry_configuration_errors(self):
        import launcher
        with patch('launcher.subprocess.run', return_value=Obj(returncode=2)) as run, \
                patch('launcher.time.sleep') as sleep, patch('builtins.print'):
            with self.assertRaises(SystemExit) as result:
                launcher.main()
            self.assertEqual(result.exception.code, 2)
            run.assert_called_once()
            sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
