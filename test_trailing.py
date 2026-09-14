import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from analytics import Ledger
from config import Settings
import options_trader

COVERED = hasattr(options_trader,'CoveredCallBot')


class ShortTrailingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.db';self.ledger=Ledger(self.path)
        self.addCleanup(lambda:self.ledger.db.close())
        self.now=datetime.now(timezone.utc);self.expiry=self.now.date()+timedelta(days=35)
        self.symbol=f'ABC{self.expiry:%y%m%d}{"C" if COVERED else "P"}00100000'
        self.sequence=0

    def fill(self,side,price,variant='regular'):
        self.sequence+=1
        if COVERED:
            intent='sell_to_open' if side=='sell' else 'buy_to_close'
            cid=self.ledger.prepare(self.symbol,'ABC',intent,1,price,context={'variant':variant})
            self.ledger.update(cid,NS(id=cid,client_order_id=cid,symbol=self.symbol,side=side,
                                      filled_qty=1,filled_avg_price=price,status='filled',filled_at=self.now.isoformat()))
        else:
            candidate=options_trader.Candidate(self.symbol,'ABC',100,self.expiry,price,price+.01)
            cid=f'cash_secured_put_ABC_{self.sequence}'
            self.ledger.intent(cid,candidate,variant,side,1)
            self.ledger.reconcile(cid,cid,'filled',1,price)

    def test_default_enabled(self):
        self.assertEqual(Settings().option_trailing_stop_percent,.20)

    def test_lower_buyback_price_persists_and_never_ratchets_up(self):
        self.fill('sell',1)
        self.assertAlmostEqual(self.ledger.option_trailing_stop(self.symbol,1,.8,.20),.96)
        self.ledger.db.close();self.ledger=Ledger(self.path)
        self.assertAlmostEqual(self.ledger.option_trailing_stop(self.symbol,1,.9,.20),.96)
        self.assertAlmostEqual(self.ledger.option_trailing_stop(self.symbol,1,.7,.20),.84)
        self.assertAlmostEqual(self.ledger.option_trailing_stop(self.symbol,1,.85,.20),.84)

    def test_new_trade_in_same_contract_resets_low(self):
        self.fill('sell',1)
        self.ledger.option_trailing_stop(self.symbol,1,.8,.20)
        self.fill('buy',.9)
        self.fill('sell',1.1,'oasis')
        self.assertAlmostEqual(self.ledger.option_trailing_stop(self.symbol,1.1,1.1,.20),1.32)

    def test_invalid_quote_does_not_corrupt_low(self):
        self.fill('sell',1)
        self.ledger.option_trailing_stop(self.symbol,1,.8,.20)
        for price in (0,-1,float('nan'),float('inf')):
            with self.assertRaises(ValueError):
                self.ledger.option_trailing_stop(self.symbol,1,price,.20)
        self.assertAlmostEqual(self.ledger.option_trailing_stop(self.symbol,1,1,.20),.96)

    def test_oasis_trails_but_regular_keeps_original_credit_stop(self):
        for variant in ('regular','oasis'):
            with self.subTest(variant=variant):
                self.fill('sell',1,variant)
                self.ledger.option_trailing_stop(self.symbol,1,.8,.20)
                clock=NS(is_open=True,timestamp=self.now,next_close=self.now+timedelta(hours=2))
                broker=MagicMock();broker.positions.return_value=[NS(symbol=self.symbol,qty=-1),NS(symbol='ABC',qty=100)]
                quote=NS(bid_price=.95,ask_price=.96,timestamp=self.now)
                if COVERED:
                    broker.open_orders.return_value=[];broker.snapshots.return_value={self.symbol:NS(latest_quote=quote)}
                    broker.clock.return_value=clock
                    bot=options_trader.CoveredCallBot(broker,self.ledger,Settings())
                    with patch.object(bot,'send') as send,patch.object(bot,'reconcile_settlements'),patch.object(self.ledger,'research_report',return_value={}):
                        bot.manage(self.now,self.now.date())
                        if variant == 'oasis':
                            self.assertEqual(send.call_args.args[:3],(self.symbol,'ABC','buy_to_close'))
                            self.assertEqual(send.call_args.kwargs['reason'],'option_trailing_stop')
                        else:
                            send.assert_not_called()
                            quote.bid_price, quote.ask_price = 1.19, 1.2
                            bot.manage(self.now,self.now.date())
                            send.assert_not_called()
                            quote.bid_price, quote.ask_price = 1.99, 2.0
                            bot.manage(self.now,self.now.date())
                            self.assertEqual(send.call_args.kwargs['reason'],'short_call_stop')
                else:
                    broker.orders.return_value=[];broker.snapshot.return_value=NS(latest_quote=quote)
                    broker.trading.get_clock.return_value=clock
                    bot=options_trader.Trader(Settings(),broker,self.ledger)
                    with patch.object(bot,'submit') as submit,patch('options_trader.bearish_at',return_value=False),patch.object(self.ledger,'event') as event:
                        bot.manage_exits()
                        if variant == 'oasis':
                            self.assertEqual(submit.call_args.args[1:3],(variant,'buy'))
                            self.assertEqual(event.call_args.kwargs['reason'],'option_trailing_stop')
                        else:
                            submit.assert_not_called()
                            quote.bid_price, quote.ask_price = 1.19, 1.2
                            bot.manage_exits()
                            submit.assert_not_called()
                            quote.bid_price, quote.ask_price = 1.99, 2.0
                            bot.manage_exits()
                            self.assertEqual(event.call_args.kwargs['reason'],'credit_stop')
                self.fill('buy',.96,variant)
