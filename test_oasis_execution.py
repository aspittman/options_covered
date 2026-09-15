from zoneinfo import ZoneInfo
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from analytics import Ledger
from config import Settings
from options_trader import CoveredCallBot


class CoveredOasisExecutionTests(unittest.TestCase):
    def test_confirmed_short_loss_persists_and_blocks_both_variants(self):
        now=datetime.now(timezone.utc)
        expiry=now.date()+timedelta(days=35)
        symbol=f'SPY{expiry:%y%m%d}C00105000'
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ledger.db';ledger=Ledger(path)
            for intent,price in [('sell_to_open',1),('buy_to_close',1.2)]:
                cid=ledger.prepare(symbol,'SPY',intent,1,price,context={'variant':'oasis'})
                ledger.update(cid,NS(client_order_id=cid,symbol=symbol,side='sell' if intent=='sell_to_open' else 'buy',filled_qty=1,filled_avg_price=price,id=cid,status='filled',filled_at=now.isoformat()))
            self.assertAlmostEqual(ledger.report()['by_variant']['oasis']['realized_option_pnl'],-20)
            ledger.db.close();ledger=Ledger(path)
            self.assertTrue(ledger.loss_blocked('SPY',now.astimezone(ZoneInfo("America/New_York")).date()+timedelta(days=30)))
            self.assertFalse(ledger.loss_blocked('SPY',now.astimezone(ZoneInfo("America/New_York")).date()+timedelta(days=31)))
            broker=MagicMock();broker.clock.return_value=NS(is_open=True,timestamp=now,next_close=now+timedelta(hours=2))
            bot=CoveredCallBot(broker,ledger,Settings(dry_run=False,enable_new_entries=True))
            for variant in ('regular','oasis'):
                self.assertFalse(bot.send(symbol,'SPY','sell_to_open',1,1,context={'variant':variant}))
            broker.submit.assert_not_called();ledger.db.close()

    def test_manage_uses_oasis_stop_and_closes_only_the_call(self):
        now=datetime.now(timezone.utc);expiry=now.date()+timedelta(days=35);symbol=f'SPY{expiry:%y%m%d}C00105000'
        ledger=MagicMock();ledger.report.return_value={'lots':{symbol:dict(underlying='SPY',qty=1,credit=1,variant='oasis',opened_at=now.isoformat())}}
        ledger.allocations.return_value={};ledger.pending.return_value=[];ledger.research_report.return_value={}
        broker=MagicMock();broker.positions.return_value=[NS(symbol=symbol,qty=-1),NS(symbol='SPY',qty=100)]
        broker.open_orders.return_value=[];broker.clock.return_value=NS(is_open=True,timestamp=now,next_close=now+timedelta(hours=2))
        broker.snapshots.return_value={symbol:NS(latest_quote=NS(bid_price=1.19,ask_price=1.2,timestamp=now))}
        bot=CoveredCallBot(broker,ledger,Settings())
        with patch.object(bot,'reconcile_settlements'),patch.object(bot,'send') as send:
            bot.manage(now,now.date())
            self.assertEqual(send.call_args.args[:3],(symbol,'SPY','buy_to_close'))
            self.assertEqual(send.call_args.kwargs['reason'],'oasis_20_percent_credit_stop')
