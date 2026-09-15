from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cycle_performance as cp
import process_lock


def fill(side, qty, price, strategy='regular', **extra):
    return dict(event='ORDER_FILL', strategy=strategy, underlying='SPY', option_symbol='SPY260918C00500000',
                order_side=side, qty=qty, price=price, timestamp='2020-01-02T14:00:00+00:00', **extra)


class CyclePerformanceTests(unittest.TestCase):
    def test_lifetime_partial_fills_and_both_variants(self):
        events = [fill('buy', 2, 2), fill('sell', 1, 3), fill('buy', 1, 1, 'oasis')]
        result = cp.long_result(events, {'SPY260918C00500000': 2.5}, 25000)
        self.assertEqual(result['realized_pnl'], 100)
        self.assertEqual(result['unrealized_pnl'], 200)
        self.assertEqual(cp.snapshot('options_direct', result)['since_inception_return_pct'], 1.2)
        self.assertTrue(result['inception'].startswith('2020'))

    def test_no_trades_is_zero(self):
        result = cp.snapshot('options_direct', cp.long_result([], {}, 25000))
        self.assertEqual(result['since_inception_return_pct'], 0)

    def test_missing_quote_and_unmatched_sell_are_unknown(self):
        for events in ([fill('buy', 1, 2)], [fill('sell', 1, 2)]):
            result = cp.snapshot('options_direct', cp.long_result(events, {}, 25000))
            self.assertIsNone(result['since_inception_return_pct'])

    def test_broker_quantity_shortfall_is_not_a_fully_marked_position(self):
        result = cp.long_result([fill('buy', 2, 2)], {'SPY260918C00500000': 3}, 25000,
                                {'SPY260918C00500000': 1})
        self.assertIsNone(result['unrealized_pnl'])

    def test_missing_fill_price_is_not_a_zero_price_fill(self):
        result = cp.long_result([fill('buy', 1, None)], {}, 25000)
        self.assertIsNone(result['unrealized_pnl'])

    def test_missing_position_retains_cost_until_confirmed_exit(self):
        missing = dict(fill('buy', 1, 2), event='POSITION_MISSING')
        events = [fill('buy', 1, 2), missing]
        self.assertIsNone(cp.long_result(events, {missing['option_symbol']: 3}, 25000)['unrealized_pnl'])
        events.append(dict(fill('sell', 1, 0), event='EXPIRATION_CONFIRMED'))
        result = cp.snapshot('options_direct', cp.long_result(events, {}, 25000))
        self.assertEqual(result['since_inception_return_pct'], -0.8)

    def test_baseline_survives_restart_and_rejects_changed_allocation(self):
        first = cp.snapshot('options_direct', cp.long_result([], {}, 25000), now=datetime(2020, 1, 1, tzinfo=timezone.utc))
        new = cp.snapshot('options_direct', cp.long_result([], {}, 50000), first)
        self.assertEqual(new['starting_capital'], 25000)
        self.assertEqual(new['inception'], first['inception'])
        self.assertIsNone(new['since_inception_return_pct'])

    def test_dashboard_all_four_stale_missing_corrupt_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'options_direct'
            now = datetime.now(timezone.utc)
            result = cp.long_result([], {}, 25000)
            own = cp.snapshot('options_direct', result, now=now)
            peer = cp.snapshot('options_inverted', result, now=now - timedelta(minutes=10))
            path = Path(directory) / 'options_inverted/logs/since_inception.json'
            cp.atomic_json(path, peer)
            with patch.dict('os.environ', {'PERFORMANCE_REPORT_PATHS': '{}'}):
                lines = cp.dashboard(root, own, now=now)
                self.assertEqual(len(lines), 5)
                self.assertIn('STALE', lines[2])
                self.assertIn('no cycle report', lines[3])
                path.write_text('broken')
                self.assertIn('invalid', cp.dashboard(root, own)[2])
                peer['paper'] = False
                cp.atomic_json(path, peer)
                self.assertIn('invalid', cp.dashboard(root, own)[2])

    def test_calculation_failure_publishes_na_and_keeps_other_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'options_direct'
            log = Mock()
            cp.publish(root, 'options_direct', Mock(side_effect=RuntimeError('unavailable')), log)
            self.assertEqual(log.call_count, 5)
            row = json.loads((root / 'logs/since_inception.json').read_text())
            self.assertIsNone(row['since_inception_return_pct'])

    def test_runtime_adapter_uses_complete_history_and_propagates_unknown_marks(self):
        import runtime_performance as runtime
        captured = []
        def capture(root, bot, calculate, *args):
            captured.append(calculate())
        with patch.object(runtime, 'publish', side_effect=capture):
            if hasattr(runtime, 'read_events'):
                import options_trader
                symbol = 'SPY260918P00500000' if hasattr(options_trader, 'get_options_inverted_positions') else 'SPY260918C00500000'
                event = fill('buy', 1, 2)
                event['option_symbol'] = symbol
                position = SimpleNamespace(symbol=symbol, current_price=None, qty=1)
                with patch.object(runtime, 'read_events', return_value=[event]), patch.object(options_trader.trading_client, 'get_all_positions', return_value=[position]):
                    runtime.report_cycle()
            else:
                ledger = Mock()
                ledger.db.execute.return_value.fetchone.return_value = ['2020-01-02T14:00:00+00:00']
                metrics = dict(realized_pnl=100, unrealized_pnl=None)
                ledger.research_report.return_value = metrics
                ledger.report.return_value = {'metrics': metrics}
                settings = SimpleNamespace(virtual_starting_capital=25000, paper=True)
                bot = SimpleNamespace(ledger=ledger, settings=settings, cfg=settings)
                runtime.report_cycle(bot)
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0]['inception'].startswith('2020'))
        self.assertIsNone(captured[0]['unrealized_pnl'])

    def test_setup_check_rejects_live_mode_before_broker_access(self):
        import config
        import setup_check
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch('sys.argv', ['setup_check.py', '--broker']))
            stack.enter_context(patch.object(setup_check.Path, 'is_file', return_value=True))
            if hasattr(config, 'Settings'):
                stack.enter_context(patch.object(config.Settings, 'from_env', return_value=SimpleNamespace(paper=False, enable_entries=False)))
                stack.enter_context(patch.object(config, 'credentials', return_value=('test', 'test')))
            else:
                stack.enter_context(patch.object(config, 'ALPACA_PAPER', False))
                stack.enter_context(patch.object(config, 'require_alpaca_credentials', return_value=('test', 'test')))
            client = stack.enter_context(patch('alpaca.trading.client.TradingClient'))
            with self.assertRaises(SystemExit):
                setup_check.main()
            client.assert_not_called()

    def test_setup_broker_check_only_reads_account(self):
        import config
        import setup_check
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch('sys.argv', ['setup_check.py', '--broker']))
            stack.enter_context(patch.object(setup_check.Path, 'is_file', return_value=True))
            stack.enter_context(patch('sys.stdout', new_callable=io.StringIO))
            if hasattr(config, 'Settings'):
                stack.enter_context(patch.object(config.Settings, 'from_env', return_value=SimpleNamespace(paper=True, enable_entries=False)))
                stack.enter_context(patch.object(config, 'credentials', return_value=('test', 'test')))
            else:
                stack.enter_context(patch.object(config, 'ALPACA_PAPER', True))
                stack.enter_context(patch.object(config, 'ENABLE_NEW_ENTRIES', False))
                stack.enter_context(patch.object(config, 'require_alpaca_credentials', return_value=('test', 'test')))
            client = stack.enter_context(patch('alpaca.trading.client.TradingClient'))
            client.return_value.get_account.return_value = SimpleNamespace(status='ACTIVE', options_trading_level=2)
            setup_check.main()
            client.assert_called_once_with('test', 'test', paper=True)
            self.assertEqual([call[0] for call in client.return_value.method_calls], ['get_account'])

    def test_cycle_continue_reports_before_sleep_and_exception_reports_without_sleep(self):
        calls = []
        for _ in range(2):
            with cp.completed_cycle(lambda: calls.append('report'), lambda: calls.append('sleep')):
                calls.append('cycle')
                continue
        self.assertEqual(calls, ['cycle', 'report', 'sleep'] * 2)
        calls.clear()
        with self.assertRaises(RuntimeError):
            with cp.completed_cycle(lambda: calls.append('report'), lambda: calls.append('sleep')):
                raise RuntimeError('cycle failed')
        self.assertEqual(calls, ['report'])

    def test_windows_lock_uses_nonblocking_first_byte(self):
        backend = SimpleNamespace(LK_NBLCK=2, locking=Mock())
        with tempfile.TemporaryFile(mode='a+') as handle:
            with patch.object(process_lock.os, 'name', 'nt'), patch.dict('sys.modules', msvcrt=backend):
                process_lock.flock(handle)
                backend.locking.assert_called_once_with(handle.fileno(), 2, 1)
                backend.locking.side_effect = OSError('busy')
                with self.assertRaises(BlockingIOError):
                    process_lock.flock(handle)

    @unittest.skipIf(__import__('os').name == 'nt', 'POSIX backend test')
    def test_posix_lock_rejects_second_process_handle_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger.lock'
            with path.open('a') as first, path.open('a') as second:
                process_lock.flock(first)
                with self.assertRaises(BlockingIOError):
                    process_lock.flock(second)
            with path.open('a') as second:
                process_lock.flock(second)


if __name__ == '__main__':
    unittest.main()
