import csv
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch, MagicMock
import pandas as pd
import oasis
import loss_guard
import portfolio_loss_guard as portfolio


class OasisRulesTests(unittest.TestCase):
    def test_direction_cloud_and_momentum(self):
        bullish = oasis.OASIS_DIRECTION == 'bullish'
        close = pd.Series([100, 101, 103] if bullish else [100, 99, 97])
        ind = dict(fast=pd.Series([99, 100, 102] if bullish else [101, 100, 98]),
                   slow=pd.Series([98, 99, 100] if bullish else [102, 101, 100]),
                   rsi=pd.Series([55, 56, 60] if bullish else [45, 44, 40]),
                   hist=pd.Series([.1,.2,.3] if bullish else [-.1,-.2,-.3]))
        self.assertTrue(oasis.oasis_bullish_at(close, ind, 2))
        ind['hist'].iloc[-1] = .1 if bullish else -.1
        self.assertFalse(oasis.oasis_bullish_at(close, ind, 2))
        ind['hist'].iloc[-1] = .3 if bullish else -.3
        ind['rsi'].iloc[-1] = 80 if bullish else 20
        self.assertFalse(oasis.oasis_bullish_at(close, ind, 2))

    def test_fresh_transition_only(self):
        close = pd.Series(range(40), index=pd.date_range('2026-09-14 13:30',periods=40,freq='5min',tz='UTC'))
        with patch.object(oasis,'completed_oasis_close',return_value=close), patch.object(oasis,'oasis_indicators',return_value={}), patch.object(oasis,'oasis_bullish_at',side_effect=[True,False,True,True]):
            self.assertTrue(oasis.get_oasis_signal_state('ABC')['new_signal'])
            self.assertFalse(oasis.get_oasis_signal_state('ABC')['new_signal'])

    def test_completed_bars_and_stale_data(self):
        now=datetime(2026,9,14,18,tzinfo=timezone.utc)
        close=pd.Series(range(55),index=pd.date_range('2026-09-14 13:30',periods=55,freq='5min',tz='UTC'))
        with patch.object(oasis,'_history',{'ABC':close}):
            self.assertEqual(oasis.completed_oasis_close('ABC',now).index[-1],pd.Timestamp('2026-09-14 17:55Z'))
            self.assertIsNone(oasis.completed_oasis_close('ABC',now+timedelta(minutes=20)))
            self.assertIsNone(oasis.completed_oasis_close('ABC',now+timedelta(days=1)))

    def test_short_stop_exactly_twenty_percent_of_credit(self):
        with patch.object(oasis,'oasis_exit_signal',return_value=(False,'')):
            self.assertEqual(oasis.short_option_exit(1,1.2,35,7,'ABC'),'oasis_20_percent_credit_stop')
            self.assertEqual(oasis.short_option_exit(1,1.19,35,7,'ABC'),'')

    def test_shortened_session_cutoff_and_exit(self):
        now=datetime(2026,11,27,17,30,tzinfo=timezone.utc)
        clock=NS(is_open=True,timestamp=now,next_close=now+timedelta(minutes=30))
        self.assertFalse(oasis.entry_window(clock))
        self.assertEqual(oasis.session_exit(clock),'')
        clock.timestamp+=timedelta(minutes=15)
        self.assertEqual(oasis.session_exit(clock),'oasis_session_close')
        self.assertEqual(oasis.session_exit(clock,'2026-11-25T16:00:00+00:00'),'oasis_overnight_recovery')

    def test_batch_calendar_excludes_early_close_afterhours(self):
        client,calendar=MagicMock(),MagicMock()
        calendar.get_calendar.return_value=[NS(date=date(2026,11,27),open=datetime(2026,11,27,9,30),close=datetime(2026,11,27,13))]
        bars=[NS(timestamp=pd.Timestamp(t),close=100) for t in ('2026-11-27 14:25Z','2026-11-27 14:30Z','2026-11-27 17:55Z','2026-11-27 18:00Z')]
        client.get_stock_bars.return_value=NS(data={'ABC':bars,'XYZ':bars})
        with patch.object(oasis,'_history',{}),patch.object(oasis,'_last_refresh',None),patch.object(oasis,'_calendar_date',None),patch.object(oasis,'_session_dates',{}):
            now=datetime(2026,11,27,18,1,tzinfo=timezone.utc)
            oasis.refresh_oasis_data(['ABC','XYZ'],client,calendar,now)
            oasis.refresh_oasis_data(['ABC','XYZ'],client,calendar,now)
            self.assertEqual(len(oasis._history['ABC']),2)
            client.get_stock_bars.assert_called_once()


class SharedLossTests(unittest.TestCase):
    def test_long_and_short_losses_day_thirty_and_thirty_one(self):
        for short in (False,True):
            rows=[dict(event='ORDER_FILL',strategy='regular',underlying='ABC',option_symbol='contract',qty=1,price=1,
                       order_side='sell' if short else 'buy',timestamp='2026-08-01T10:00:00'),
                  dict(event='ORDER_FILL',strategy='regular',underlying='ABC',option_symbol='contract',qty=1,price=1.2 if short else .8,
                       order_side='buy' if short else 'sell',timestamp='2026-08-02T10:00:00')]
            losses=loss_guard.latest_loss_dates(rows,short=short)
            self.assertTrue(loss_guard.blocked('ABC',losses,date(2026,9,1)))
            self.assertFalse(loss_guard.blocked('ABC',losses,date(2026,9,2)))
            self.assertFalse(loss_guard.blocked('XYZ',losses,date(2026,8,2)))

    def test_profit_does_not_start_or_extend_block(self):
        rows=[dict(event='ORDER_FILL',strategy='oasis',underlying='ABC',option_symbol='contract',qty=1,price=p,
                   order_side=side,timestamp='2026-08-02T10:00:00') for p,side in ((1,'sell'),(.8,'buy'))]
        self.assertEqual(loss_guard.latest_loss_dates(rows,short=True),{})

    def test_portfolio_csv_block_crosses_bot_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'LOSS_GUARD_SCOPE':'portfolio','LOSS_LEDGER_PATHS':'{}'}):
            base=Path(tmp);source=base/'options_direct/logs/trade_analytics.csv';source.parent.mkdir(parents=True)
            fields=['event','strategy','underlying','option_symbol','qty','price','order_side','timestamp']
            with source.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
                for side,price,stamp in [('buy',1,'2026-08-01'),('sell',.8,'2026-08-02')]:
                    writer.writerow(dict(event='ORDER_FILL',strategy='oasis',underlying='ABC',option_symbol='ABC261218C00100000',qty=1,price=price,order_side=side,timestamp=stamp+'T10:00:00'))
            with patch.object(portfolio,'__file__',str(base/'options_covered/portfolio_loss_guard.py')):
                self.assertTrue(portfolio.portfolio_blocked('ABC',date(2026,9,1)))
                self.assertFalse(portfolio.portfolio_blocked('ABC',date(2026,9,2)))
                with patch.dict(os.environ,{'LOSS_GUARD_SCOPE':'bot'}):
                    self.assertFalse(portfolio.portfolio_blocked('ABC',date(2026,9,1)))

    def test_sqlite_short_credit_loss_and_stock_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'covered.sqlite3';db=sqlite3.connect(path)
            db.executescript('CREATE TABLE fills(id INTEGER PRIMARY KEY,underlying TEXT,symbol TEXT,qty REAL,notional REAL,intent TEXT,timestamp TEXT); CREATE TABLE stock_dispositions(underlying TEXT,price REAL,cost_per_share REAL,timestamp TEXT);')
            db.execute("INSERT INTO fills VALUES (1,'ABC','contract',1,1,'sell_to_open','2026-08-01')")
            db.execute("INSERT INTO fills VALUES (2,'ABC','contract',1,1.2,'buy_to_close','2026-08-02')")
            db.execute("INSERT INTO stock_dispositions VALUES ('XYZ',90,100,'2026-08-03')")
            db.commit();db.close()
            self.assertEqual(portfolio.read_loss_dates('options_covered',path),{'ABC':date(2026,8,2),'XYZ':date(2026,8,3)})
            path=Path(tmp)/'secured.sqlite3';db=sqlite3.connect(path)
            db.execute('CREATE TABLE pnl(symbol TEXT,realized REAL,timestamp TEXT)')
            db.execute("INSERT INTO pnl VALUES ('ABC261218P00100000',-20,'2026-08-04')")
            db.commit();db.close()
            self.assertEqual(portfolio.read_loss_dates('options_secured',path),{'ABC':date(2026,8,4)})

    def test_unreadable_configured_history_blocks_entry(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'LOSS_GUARD_SCOPE':'portfolio','LOSS_LEDGER_PATHS':'{}'}):
            path=Path(tmp)/'options_secured/logs/options_secured.sqlite3';path.parent.mkdir(parents=True);path.write_text('not a database')
            with patch.object(portfolio,'__file__',str(Path(tmp)/'options_direct/portfolio_loss_guard.py')):
                self.assertTrue(portfolio.portfolio_blocked('ABC'))
