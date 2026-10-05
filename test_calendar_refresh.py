import copy
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from events import event_block
from refresh_events import refresh, parse_spdr, parse_ishares, SYMBOLS

TODAY=date(2026,10,1)
EXPIRY=date(2026,11,6)
# Fixtures are issuer PDFs captured during this repair, not synthetic market data.
SOURCES=Path(__file__).resolve().parent/'calendar_test_sources'

class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'events.json'
    def tearDown(self):self.tmp.cleanup()
    def refresh(self, **kwargs):
        return refresh(self.path,TODAY,fetcher=lambda p:p.encode(),extractor=lambda b:(SOURCES/(b.decode()+'.txt')).read_text(),**kwargs)
    def test_real_issuer_groups_and_ex_dates(self):
        self.refresh()
        data=json.loads(self.path.read_text())
        self.assertEqual(set(data),SYMBOLS['spdr']|SYMBOLS['ishares'])
        self.assertIn('2026-12-18',data['SPY']['ex_dividend'])
        self.assertIn('2026-12-21',data['XLV']['ex_dividend'])
        self.assertIn('2026-11-02',data['HYG']['ex_dividend'])
        self.assertNotIn('2026-11-05',data['HYG']['ex_dividend']) # payment date
        self.assertIn('2026-12-30',data['IWM']['ex_dividend']) # potential excise
        self.assertEqual(event_block(self.path,'XLV',TODAY,EXPIRY),'')
        self.assertEqual(event_block(self.path,'IWM',TODAY,EXPIRY),'')
        self.assertEqual(event_block(self.path,'HYG',TODAY,EXPIRY),'ex_dividend_before_expiration')
        self.assertEqual(event_block(self.path,'DIA',TODAY,EXPIRY),'ex_dividend_before_expiration')
        self.assertEqual(event_block(self.path,'AAPL',TODAY,EXPIRY),'event_calendar_unavailable')
    def test_network_failure_does_not_renew_verification(self):
        self.refresh();before=self.path.read_bytes()
        def offline(provider):raise OSError('offline')
        report=refresh(self.path,TODAY+timedelta(days=8),fetcher=offline)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertTrue(all(p['status']=='fetch_failed' for p in report['providers'].values()))
        self.assertEqual(event_block(self.path,'SPY',TODAY+timedelta(days=8),EXPIRY),'event_calendar_verification_older_than_7_days')
    def test_changed_document_invalidates_only_affected_provider(self):
        self.refresh()
        report=refresh(self.path,TODAY,fetcher=lambda p:p.encode(),extractor=lambda b:'changed format' if b==b'spdr' else (SOURCES/'ishares.txt').read_text())
        self.assertEqual(report['providers']['spdr']['status'],'review_required')
        self.assertEqual(event_block(self.path,'SPY',TODAY,EXPIRY),'event_calendar_source_requires_review')
        self.assertEqual(event_block(self.path,'IWM',TODAY,EXPIRY),'')
        self.refresh()
        self.assertEqual(event_block(self.path,'SPY',TODAY,EXPIRY),'')
    def test_partial_schedule_and_missing_membership_rejected(self):
        text=(SOURCES/'spdr.txt').read_text()
        with self.assertRaises(ValueError):parse_spdr(text.replace('(XLV)','(ZZZ)'),2026)
        with self.assertRaises(ValueError):parse_spdr(text.replace('Potential Excise Distribution','Missing date row'),2026)
        text=(SOURCES/'ishares.txt').read_text()
        with self.assertRaises(ValueError):parse_ishares(text.replace('IWM iShares','ZZZ iShares'),2026)
        with self.assertRaises(ValueError):parse_ishares(text.replace('30-Dec-26','30-Dec-25'),2026)
    def test_rollover_does_not_extrapolate_dates(self):
        with self.assertRaises(ValueError):parse_spdr((SOURCES/'spdr.txt').read_text(),2027)
    def test_preserves_unmanaged_calendar_rows(self):
        row={'verified_on':'2026-09-15','valid_through':'2026-10-31','earnings':[],'ex_dividend':[]}
        self.path.write_text(json.dumps({'SLV':row}));self.refresh()
        self.assertEqual(json.loads(self.path.read_text())['SLV'],row)
    def test_full_buffer_coverage_and_malformed_data(self):
        self.refresh();data=json.loads(self.path.read_text());row=data['SPY']
        row['valid_through']=EXPIRY.isoformat();self.path.write_text(json.dumps(data))
        self.assertEqual(event_block(self.path,'SPY',TODAY,EXPIRY),'event_calendar_does_not_cover_expiration')
        row['valid_through']=(EXPIRY+timedelta(days=1)).isoformat()
        row['ex_dividend']=[(EXPIRY+timedelta(days=1)).isoformat()];self.path.write_text(json.dumps(data))
        self.assertEqual(event_block(self.path,'SPY',TODAY,EXPIRY),'ex_dividend_before_expiration')
        row['ex_dividend']={};self.path.write_text(json.dumps(data))
        self.assertEqual(event_block(self.path,'SPY',TODAY,EXPIRY),'event_calendar_unavailable')
    def test_earnings_still_block_for_manually_verified_stocks(self):
        self.path.write_text(json.dumps({'AAPL':{'verified_on':TODAY.isoformat(),'valid_through':'2026-12-31','earnings':['2026-10-29'],'ex_dividend':[]}}))
        self.assertEqual(event_block(self.path,'AAPL',TODAY,EXPIRY),'earnings_before_expiration')

if __name__=='__main__':unittest.main()
