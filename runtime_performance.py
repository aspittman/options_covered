"""Publish fresh cycle marks and all recorded realized results."""
import logging
from pathlib import Path
from cycle_performance import publish


def report_cycle(bot):
    def calculate():
        marks, stocks = getattr(bot, 'cycle_marks', ({}, {}))
        report = bot.ledger.research_report(bot.settings, marks, stocks)
        first = bot.ledger.db.execute('SELECT MIN(stamp) FROM (SELECT timestamp AS stamp FROM fills UNION ALL SELECT allocated_at FROM stock_allocations UNION ALL SELECT timestamp FROM stock_dispositions)').fetchone()[0]
        return dict(starting_capital=bot.settings.virtual_starting_capital,
                    realized_pnl=report['realized_pnl'], unrealized_pnl=report['unrealized_pnl'],
                    inception=first, status='ok' if report['unrealized_pnl'] is not None else 'incomplete: missing marks or reconciliation required')
    publish(Path(__file__).resolve().parent, 'options_covered', calculate, logging.info, bot.settings.paper)
