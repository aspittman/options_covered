from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from analytics import Ledger
from broker import AlpacaBroker
from config import Settings
from options_trader import CoveredCallBot


class BuyWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.now = datetime.now(timezone.utc)
        self.today = self.now.date()
        expiry = self.today + timedelta(days=35)
        self.symbol = f'ABC{expiry:%y%m%d}C00055000'
        self.contract = NS(symbol=self.symbol, underlying_symbol='ABC', expiration_date=expiry,
                           strike_price=55, tradable=True, type='call', size=100, open_interest=1000)
        (self.path/'events.json').write_text(json.dumps({'ABC':dict(
            verified_on=self.today.isoformat(), valid_through=(self.today+timedelta(days=60)).isoformat(),
            earnings=[], ex_dividend=[])}))
        self.settings = Settings(auto_buy_shares=True, dry_run=False, enable_new_entries=True,
                                 ledger_path=self.path/'ledger.db', events_path=self.path/'events.json')
        self.ledger = Ledger(self.settings.ledger_path)
        self.broker = Mock()
        self.broker.account.return_value = NS(id='paper', cash=100000, buying_power=100000,
            trading_blocked=False, account_blocked=False, options_trading_level=2)
        self.broker.positions.return_value = []
        self.broker.open_orders.return_value = []
        self.broker.clock.return_value = NS(is_open=True, timestamp=self.now, next_close=self.now+timedelta(hours=3))
        self.broker.previous_session.return_value = self.today - timedelta(days=1)
        self.broker.stock_quote.return_value = (49.99,50)
        self.broker.spot.return_value = 50
        self.broker.contracts.return_value = [self.contract]
        self.broker.snapshots.return_value = {self.symbol:NS(
            latest_quote=NS(bid_price=1, ask_price=1.04, timestamp=self.now), greeks=NS(delta=.25))}
        self.broker.volumes.return_value = {self.symbol:1000}
        self.broker.submit_stock.side_effect = lambda cid,symbol,price: self.stock_order(cid)
        self.broker.submit.side_effect = lambda cid,symbol,qty,intent,price: NS(
            client_order_id=cid,symbol=symbol,side='sell',qty=qty,filled_qty=0,filled_avg_price=None,id='call',status='new')
        self.bot = CoveredCallBot(self.broker,self.ledger,self.settings)
        self.state = dict(eligible=True,date=(self.today-timedelta(days=1)).isoformat(),variant='regular')
        self.bot.daily_signal = Mock(return_value=self.state)

    def tearDown(self):
        self.ledger.db.close()
        self.temp.cleanup()

    def stock_order(self, cid, qty=0, status='new', price=50):
        return NS(client_order_id=cid,symbol='ABC',side='buy',qty=100,filled_qty=qty,
                  filled_avg_price=price if qty else None,id='stock',status=status,filled_at=self.now.isoformat())

    def start(self):
        self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.broker.submit_stock.assert_called_once()
        return self.ledger.stock_orders()[0]['client_id']

    def test_pair_selection_stock_fill_restart_then_covered_call(self):
        cid=self.start()
        self.broker.submit.assert_not_called()
        self.assertEqual(self.ledger.capital_state(self.settings)['pending_stock_reservation'],5000)
        self.ledger.db.close()
        self.ledger=Ledger(self.settings.ledger_path)
        self.bot=CoveredCallBot(self.broker,self.ledger,self.settings)
        self.bot.daily_signal=Mock(return_value=self.state)
        self.broker.order.return_value=self.stock_order(cid,100,'filled')
        self.broker.positions.return_value=[NS(symbol='ABC',qty=100,qty_available=100,asset_class='us_equity')]
        self.assertTrue(self.bot.reconcile_stock(self.now))
        self.assertEqual(self.ledger.allocations()['ABC']['shares'],100)
        self.bot.continue_buy_writes(self.now,self.today,self.today-timedelta(days=1))
        self.broker.submit.assert_called_once()
        self.assertEqual(self.ledger.stock_orders()[0]['call_state'],'submitted')
        self.bot.continue_buy_writes(self.now,self.today,self.today-timedelta(days=1))
        self.broker.submit.assert_called_once()
        self.assertEqual(len(self.ledger.stock_orders()),1)

    def test_partial_fills_are_reserved_marked_and_never_cover_a_call(self):
        cid=self.start()
        order=self.stock_order(cid,40,'partially_filled')
        self.ledger.update_stock(cid,order)
        self.ledger.update_stock(cid,order)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM stock_fills').fetchone()[0],1)
        capital=self.ledger.capital_state(self.settings)
        self.assertEqual(capital['capital_employed'],5000)
        self.assertEqual(capital['pending_stock_reservation'],3000)
        report=self.ledger.research_report(self.settings,{}, {'ABC':51})
        self.assertEqual(report['unrealized_pnl'],40)
        self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.broker.submit.assert_not_called()
        self.broker.submit_stock.assert_called_once()
        self.ledger.update_stock(cid,self.stock_order(cid,40,'pending_cancel'))
        self.assertEqual(self.ledger.stock_reserved(),3000)
        self.ledger.update_stock(cid,self.stock_order(cid,40,'canceled'))
        self.assertEqual(self.ledger.stock_reserved(),0)
        self.assertEqual(self.ledger.stock_lifecycle()['ABC']['state'],'partial_lot_review')
        self.assertEqual(self.ledger.capital_state(self.settings)['capital_employed'],2000)

    def test_missing_calendar_and_invalid_call_never_buy_stock(self):
        with patch('options_trader.event_block', return_value='event_calendar_unavailable'):
            self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.broker.submit_stock.assert_not_called()
        self.broker.snapshots.return_value={}
        self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.broker.submit_stock.assert_not_called()

    def test_unknown_submission_keeps_reservation_and_is_not_retried(self):
        self.broker.submit_stock.side_effect=TimeoutError('response lost')
        with self.assertRaises(TimeoutError):
            self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.assertEqual(self.ledger.stock_reserved(),5000)
        self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.broker.submit_stock.assert_called_once()
        self.broker.order.side_effect=TimeoutError('still unknown')
        self.assertFalse(self.bot.reconcile_stock(self.now))
        self.assertEqual(self.ledger.stock_reserved(),5000)

    def test_loss_or_disabled_entries_cancels_stock_but_waits_for_ack(self):
        self.bot.settings=replace(self.settings,enable_new_entries=False)
        self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.broker.submit_stock.assert_not_called()
        self.bot.settings=self.settings
        cid=self.start()
        self.broker.order.return_value=self.stock_order(cid)
        self.bot.settings=replace(self.settings,enable_new_entries=False)
        self.bot.reconcile_stock(self.now)
        self.broker.cancel.assert_called_once_with('stock')
        self.assertEqual(self.ledger.stock_reserved(),5000)

    def test_filled_shares_retained_if_signal_disappears(self):
        cid=self.start()
        self.ledger.update_stock(cid,self.stock_order(cid,100,'filled'))
        self.bot.daily_signal.return_value={'eligible':False}
        self.bot.continue_buy_writes(self.now,self.today,self.today-timedelta(days=1))
        self.broker.submit.assert_not_called()
        self.assertEqual(self.ledger.stock_orders()[0]['call_state'],'retained')
        self.assertEqual(self.ledger.stock_lifecycle()['ABC']['state'],'uncovered_reusable')

    def test_foreign_shares_and_insufficient_cash_block_purchase(self):
        self.broker.positions.return_value=[NS(symbol='ABC',qty=100,asset_class='us_equity')]
        self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.broker.submit_stock.assert_not_called()
        self.broker.positions.return_value=[]
        self.broker.account.return_value.cash=500
        self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.broker.submit_stock.assert_not_called()

    def test_pending_stock_reservation_blocks_another_budget_sized_purchase(self):
        cid=self.start()
        self.ledger.db.execute('UPDATE stock_orders SET limit_price=240 WHERE client_id=?',(cid,))
        self.ledger.db.commit()
        self.assertEqual(self.ledger.capital_state(self.settings)['virtual_capital_available'],1000)
        other_symbol=self.symbol.replace('ABC','DEF')
        other=NS(**vars(self.contract))
        other.symbol=other_symbol
        other.underlying_symbol='DEF'
        snapshot=self.broker.snapshots.return_value[self.symbol]
        self.broker.snapshots.return_value[other_symbol]=snapshot
        self.broker.volumes.return_value[other_symbol]=1000
        with patch('buy_write.event_block',return_value=''):
            self.assertFalse(self.bot.acquire_stock('DEF',self.state,other,self.now,self.today))
        self.broker.submit_stock.assert_called_once()

    def test_reject_wrong_identity_or_impossible_fill(self):
        cid=self.start()
        for order in (self.stock_order('foreign',100,'filled'),self.stock_order(cid,101,'filled'),self.stock_order(cid,40,'filled')):
            with self.assertRaises(RuntimeError):self.ledger.update_stock(cid,order)
        self.assertEqual(self.ledger.allocations(),{})

    def test_canceled_empty_buy_is_not_retried_for_the_same_signal(self):
        cid=self.start()
        self.ledger.update_stock(cid,self.stock_order(cid,0,'canceled'))
        self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.broker.submit_stock.assert_called_once()
        self.assertEqual(self.ledger.stock_reserved(),0)

    def test_manual_allocation_cannot_overlap_an_unknown_stock_purchase(self):
        self.start()
        with self.assertRaises(ValueError):
            self.ledger.allocate_shares('ABC',100,50,50,self.settings)

    def test_oasis_buy_requires_current_momentum_as_well_as_daily_stock_suitability(self):
        state=dict(self.state,variant='oasis')
        with patch('buy_write.get_oasis_signal_state',return_value={'bullish':False}):
            self.bot.enter_qualified('ABC',state,self.now,self.today)
        self.broker.submit_stock.assert_not_called()

    def test_dry_run_and_live_guard_never_submit_stock(self):
        self.bot.settings=replace(self.settings,dry_run=True)
        self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.broker.submit_stock.assert_not_called()
        self.assertEqual(self.ledger.stock_orders(),[])
        with self.assertRaises(ValueError):replace(self.settings,paper=False)
        broker=AlpacaBroker.__new__(AlpacaBroker)
        broker.settings=replace(self.settings,dry_run=True)
        broker.trading=Mock()
        with self.assertRaises(ValueError):broker.submit_stock('covered_stock_test','ABC',50)
        broker.trading.submit_order.assert_not_called()

    def test_broker_stock_path_is_100_share_day_limit_without_extended_hours(self):
        broker=AlpacaBroker.__new__(AlpacaBroker)
        broker.settings=self.settings
        broker.trading=Mock()
        broker.submit_stock('covered_stock_test','ABC',50)
        request=broker.trading.submit_order.call_args.args[0]
        self.assertEqual(request.qty,100)
        self.assertEqual(str(request.side.value),'buy')
        self.assertEqual(str(request.time_in_force.value),'day')
        self.assertEqual(request.limit_price,50)
        self.assertFalse(request.extended_hours)

    def test_stock_reconciliation_failure_blocks_entries_but_not_call_exit_management(self):
        self.bot.reconcile_stock=Mock(return_value=False)
        self.bot.reconcile=Mock(return_value=True)
        self.bot.manage=Mock(return_value=True)
        self.bot.cancel_blocked_entries=Mock(return_value=True)
        with patch('options_trader.refresh_oasis_data'):
            self.bot.cycle()
        self.bot.manage.assert_called_once()
        self.broker.submit_stock.assert_not_called()
        self.broker.submit.assert_not_called()

    def test_second_partial_fill_uses_incremental_cost_and_preserves_one_allocation(self):
        cid=self.start()
        self.ledger.update_stock(cid,self.stock_order(cid,40,'partially_filled',49))
        self.ledger.update_stock(cid,self.stock_order(cid,100,'filled',49.5))
        allocation=self.ledger.allocations()['ABC']
        self.assertEqual(allocation['shares'],100)
        self.assertEqual(allocation['cost_per_share'],49.5)
        self.assertEqual(self.ledger.stock_reserved(),0)
        fills=list(self.ledger.db.execute('SELECT qty,notional FROM stock_fills ORDER BY id'))
        self.assertEqual([tuple(r) for r in fills],[(40,1960),(60,2990)])


if __name__ == '__main__':
    unittest.main()
