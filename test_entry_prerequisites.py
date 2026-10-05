from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import Mock
import tempfile
from pathlib import Path
import unittest

from config import Settings
from options_trader import CoveredCallBot


class EntryPrerequisiteTests(unittest.TestCase):
    def make_bot(self, settings, allocations):
        bot=CoveredCallBot.__new__(CoveredCallBot)
        bot.settings=settings
        bot.ledger=Mock()
        bot.ledger.traded_bar.return_value=False
        bot.ledger.loss_blocked.return_value=False
        bot.ledger.cooling_down.return_value=False
        bot.ledger.allocations.return_value=allocations
        bot.broker=Mock()
        bot.reject=Mock()
        return bot

    def test_no_allocated_shares_stops_before_chain_requests(self):
        bot=self.make_bot(Settings(),{})
        now=datetime.now(timezone.utc)
        bot.enter_qualified('SLV',{'date':now.date().isoformat()},now,now.date())
        self.assertEqual(bot.reject.call_args.args[2],'NO_STRATEGY_CONTROLLED_SHARES')
        bot.broker.contracts.assert_not_called()
        bot.broker.spot.assert_not_called()

    def test_missing_calendar_stops_once_before_chain_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            bot=self.make_bot(Settings(events_path=Path(directory)/'missing.json'),
                              {'SLV':{'shares':100,'cost_per_share':57.53}})
            now=datetime.now(timezone.utc)
            bot.enter_qualified('SLV',{'date':now.date().isoformat()},now,now.date())
            bot.reject.assert_called_once()
            self.assertEqual(bot.reject.call_args.args[2],'EVENT_CALENDAR_BLOCK')
            self.assertEqual(bot.reject.call_args.kwargs['details'],'event_calendar_unavailable')
            bot.broker.contracts.assert_not_called()
