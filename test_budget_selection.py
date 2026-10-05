from dataclasses import replace
import unittest
from unittest.mock import patch
import test_buy_write
from risk import evaluate_candidate

class BudgetSelectionTests(unittest.TestCase):
    setUp = test_buy_write.BuyWriteTests.setUp
    tearDown = test_buy_write.BuyWriteTests.tearDown
    stock_order = test_buy_write.BuyWriteTests.stock_order

    def test_over_budget_underlying_does_not_scan_or_buy(self):
        self.broker.spot.return_value=300
        self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.broker.contracts.assert_not_called()
        self.broker.submit_stock.assert_not_called()
        self.broker.submit.assert_not_called()

    def test_50_volume_still_requires_full_collateral_and_quality(self):
        self.assertEqual(self.settings.min_volume,50)
        self.broker.volumes.return_value={self.symbol:50}
        self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.broker.submit_stock.assert_called_once()
        self.assertEqual(self.ledger.capital_state(self.settings)['pending_stock_reservation'],5000)
        self.broker.submit.assert_not_called()

    def test_missing_volume_retries_without_marking_signal_traded(self):
        self.broker.volumes.return_value={}
        with patch.object(self.bot,'reject') as rejected:
            self.bot.enter_qualified('ABC',self.state,self.now,self.today)
            self.assertEqual(rejected.call_args.args[2],'VOLUME_DATA_UNAVAILABLE')
        self.broker.submit_stock.assert_not_called()
        self.assertFalse(self.ledger.traded_bar('ABC',self.state['date']))
        self.broker.volumes.return_value={self.symbol:50}
        self.bot.enter_qualified('ABC',self.state,self.now,self.today)
        self.broker.submit_stock.assert_called_once()

    def test_unknown_interest_and_low_volume_are_distinct(self):
        snapshot=self.broker.snapshots.return_value[self.symbol]
        def check(volume):return evaluate_candidate(self.contract,snapshot,volume,50,50,self.today,self.now,self.settings)[1]
        self.assertEqual(check(0),'INSUFFICIENT_LIQUIDITY')
        self.assertEqual(check(None),'VOLUME_DATA_UNAVAILABLE')
        self.contract.open_interest=None
        self.assertEqual(check(1000),'OPEN_INTEREST_DATA_UNAVAILABLE')
