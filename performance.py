"""Shared metric definitions for covered-call paper execution and simulation.

Trade statistics use option P/L plus stock movement DURING the call's lifetime.
Portfolio returns include all allocated stock movement, including between calls.
ROC uses average observed portfolio capital employed; per-trade ROC is separate.
"""
from statistics import mean


def metrics(starting, realized, unrealized, trades, samples):
    closed = [t for t in trades if t.get('closed')]
    pnl = [t['combined_pnl'] for t in closed if t.get('combined_pnl') is not None]
    winners, losers = [p for p in pnl if p > 0], [p for p in pnl if p < 0]
    capitals = [t['capital_employed'] for t in trades if t.get('capital_employed') is not None]
    deployed = [s['capital_employed'] for s in samples]
    peak, drawdown = starting, 0
    for sample in samples:
        if sample.get('equity') is None:
            continue
        peak = max(peak, sample['equity'])
        drawdown = max(drawdown, (peak - sample['equity']) / peak * 100)
    average = mean(deployed) if deployed else 0
    total = realized + unrealized if unrealized is not None else None
    count = len(closed)
    outcomes = {kind: sum(t.get('outcome') == kind for t in closed)
                for kind in ('assignment', 'expiration', 'buy_to_close')}
    premium = sum(t.get('premium_received', 0) for t in trades)
    return {
        'strategy': 'covered_call', 'starting_virtual_capital': starting,
        'ending_virtual_capital': starting + total if total is not None else None,
        'realized_pnl': realized, 'unrealized_pnl': unrealized,
        'total_return_pct': total / starting * 100 if total is not None else None,
        'return_on_capital_employed_pct': total / average * 100 if total is not None and average else None,
        'average_capital_employed_per_trade': mean(capitals) if capitals else None,
        'average_capital_employed': average,
        'maximum_capital_employed': max(deployed, default=0),
        'trade_count': len(trades), 'closed_trade_count': count,
        'trades_with_complete_pnl': len(pnl),
        'win_rate_pct': len(winners) / len(pnl) * 100 if pnl else None,
        'average_winner': mean(winners) if winners else None,
        'average_loser': mean(losers) if losers else None,
        'expectancy': mean(pnl) if pnl else None,
        'profit_factor': sum(winners) / abs(sum(losers)) if losers else None,
        'max_drawdown_pct': drawdown,
        'average_hold_days': mean([t['hold_days'] for t in closed]) if closed else None,
        'largest_winner': max(winners) if winners else None,
        'largest_loser': min(losers) if losers else None,
        'premium_received': premium,
        'assignment_rate_pct': outcomes['assignment'] / count * 100 if count else None,
        'expiration_rate_pct': outcomes['expiration'] / count * 100 if count else None,
        'buy_to_close_rate_pct': outcomes['buy_to_close'] / count * 100 if count else None,
        'average_collateral_committed': mean(capitals) if capitals else None,
        'premium_return_on_collateral_pct': premium / sum(capitals) * 100 if capitals and sum(capitals) else None,
        'total_combined_return': total,
    }
